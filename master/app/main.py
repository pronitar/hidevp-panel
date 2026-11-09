import asyncio
import contextlib
import base64
import json
import re
import logging
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import hmac
import random

from . import backup, botui, core, nodes, notices, payments, promo, qr, stats, update
from .i18n import LANG, tr
from .bot import manager as bot
from .db import ex, get_setting, init_db, prices, q, q1, set_setting
from .events import request_sync
from .sync import LAST_RUN, active_users, push_state, sync_loop

log = logging.getLogger("panel")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"
SESSION_TTL = 30 * core.DAY
COOKIE = "vp_s"
FAILS: dict[str, list[float]] = {}

TRANSPORTS = ("tcp", "xhttp", "grpc")
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized")


class ApiError(Exception):
    def __init__(self, msg, code=400, extra=None):
        super().__init__(msg)
        self.code = code
        self.extra = extra or {}


def ok(data=None, **kw):
    return JSONResponse(data if data is not None else {"ok": True, **kw})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def current_admin(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    s = q1("SELECT * FROM sessions WHERE token=?", (tok,))
    if not s or s["created"] + SESSION_TTL < time.time():
        return None
    return q1("SELECT id, username FROM admins WHERE id=?", (s["admin_id"],))


def api(handler, auth=True):
    async def wrapper(request: Request):
        LANG.set("en" if request.headers.get("x-lang", "") == "en" else "ru")
        if auth and not current_admin(request):
            return JSONResponse({"error": tr("Требуется вход")}, status_code=401)
        try:
            return await handler(request)
        except ApiError as e:
            return JSONResponse({"error": tr(str(e)), **e.extra}, status_code=e.code)
        except nodes.NodeError as e:
            return JSONResponse({"error": tr(str(e))}, status_code=502)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": ("Invalid data: " if LANG.get() == "en" else "Некорректные данные: ") + str(e)},
                                status_code=400)
    return wrapper


def bot_status() -> dict:
    st = bot.status()
    st["error"] = tr(st.get("error", ""))
    st["route"] = tr(st.get("route", ""))
    return st


async def body(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


DUMMY_HASH = core.hash_password(secrets.token_hex(8))


async def login(request: Request):
    ip = client_ip(request)
    now = time.time()
    _prune(FAILS, 600)
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    if len(FAILS[ip]) >= 5:
        raise ApiError("Слишком много попыток. Подождите 10 минут.", 429)
    d = await body(request)
    a = q1("SELECT * FROM admins WHERE username=?", (str(d.get("username", "")).strip(),))
    good = core.check_password(str(d.get("password", "")), a["pass_hash"] if a else DUMMY_HASH)
    if not a or not good:
        FAILS[ip].append(now)
        await asyncio.sleep(1)
        raise ApiError("Неверный логин или пароль", 401)
    tok = secrets.token_urlsafe(32)
    ex("INSERT INTO sessions(token,admin_id,created) VALUES(?,?,?)", (tok, a["id"], now))
    ex("DELETE FROM sessions WHERE created<?", (now - SESSION_TTL,))
    resp = ok()
    resp.set_cookie(COOKIE, tok, max_age=int(SESSION_TTL), httponly=True, samesite="strict",
                    path=request.app.state.base + "/", secure=request.url.scheme == "https")
    return resp


async def logout(request: Request):
    ex("DELETE FROM sessions WHERE token=?", (request.cookies.get(COOKIE, ""),))
    resp = ok()
    resp.delete_cookie(COOKIE, path=request.app.state.base + "/")
    return resp


async def me(request: Request):
    a = current_admin(request)
    if not a:
        raise ApiError("Требуется вход", 401)
    return ok({"username": a["username"], "title": get_setting("panel_title", "VPN"),
               "lang": get_setting("lang", "ru") or "ru", "version": panel_versions()["master"]})


async def change_admin(request: Request):
    a = current_admin(request)
    d = await body(request)
    full = q1("SELECT * FROM admins WHERE id=?", (a["id"],))
    if not core.check_password(str(d.get("current", "")), full["pass_hash"]):
        raise ApiError("Текущий пароль неверен")
    username = str(d.get("username") or a["username"]).strip()
    pw = str(d.get("password") or "")
    if pw and len(pw) < 8:
        raise ApiError("Пароль должен быть не короче 8 символов")
    ex("UPDATE admins SET username=?, pass_hash=? WHERE id=?",
       (username, core.hash_password(pw) if pw else full["pass_hash"], a["id"]))
    if pw:
        ex("DELETE FROM sessions WHERE admin_id=? AND token!=?", (a["id"], request.cookies.get(COOKIE, "")))
    return ok()


async def overview(request: Request):
    users = q("SELECT * FROM users")
    t = time.time()
    st = {}
    for u in users:
        s = core.user_status(u, t)
        st[s] = st.get(s, 0) + 1
    pays = q("""SELECT p.*, u.name, u.tg_username, pr.code AS promo_code FROM payments p
                LEFT JOIN users u ON u.id=p.user_id LEFT JOIN promos pr ON pr.id=p.promo_id
                ORDER BY p.id DESC LIMIT 15""")
    month_ago = t - 30 * core.DAY
    return ok({
        "users_total": len(users),
        "users_by_status": st,
        "online": sum(1 for u in users if u["online_ips"] != "[]"),
        "traffic_total": sum(u["used_up"] + u["used_down"] for u in users),
        "stars_total": (q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE currency='XTR'") or {})["s"],
        "stars_30d": (q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE currency='XTR' AND created>?", (month_ago,)) or {})["s"],
        "revenue_30d": [{"currency": r["currency"], "sum": r["s"], "text": payments.fmt(r["s"], r["currency"])} for r in
                        q("SELECT currency, SUM(amount) s FROM payments WHERE created>? AND amount>0 GROUP BY currency", (month_ago,))],
        "revenue_total": [{"currency": r["currency"], "sum": r["s"], "count": r["c"], "text": payments.fmt(r["s"], r["currency"])} for r in
                          q("SELECT currency, SUM(amount) s, COUNT(*) c FROM payments WHERE amount>0 GROUP BY currency ORDER BY currency='XTR' DESC, s DESC")],
        "nodes": q("SELECT id,name,host,country_code,status FROM nodes"),
        "vpns": (q1("SELECT COUNT(*) c FROM vpns") or {})["c"],
        "payments": pays,
        "bot": bot_status(),
        "sync": LAST_RUN,
    })


_VER_CACHE: dict = {}


def panel_versions() -> dict:
    if _VER_CACHE.get("at", 0) > time.time() - 60:
        return _VER_CACHE
    from pathlib import Path
    import re as _re
    import socket
    root = Path(__file__).resolve().parents[2]
    src = root / "src" if (root / "src" / "VERSION").exists() else root
    ver = (src / "VERSION").read_text().strip() if (src / "VERSION").exists() else "?"
    agent = ""
    try:
        m = _re.search(r'^VERSION = "(.+?)"', (src / "node" / "agent.py").read_text(), _re.M)
        agent = m.group(1) if m else ""
    except OSError:
        pass
    host = _re.sub(r"^https?://", "", core.public_url()).split("/")[0].split(":")[0].strip("[]")
    ips = set()
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(host, None)} if host else set()
    except OSError:
        pass
    _VER_CACHE.update(at=time.time(), master=ver, agent=agent, master_ips=ips)
    return _VER_CACHE


def _vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def node_out(n: dict) -> dict:
    d = {k: v for k, v in n.items() if k not in ("api_key", "cert_pem")}
    d["info"] = json.loads(n.get("info") or "{}")
    d["error"] = tr(n.get("error") or "")
    d["flag"] = core.flag(n["country_code"])
    d["vpn_count"] = (q1("SELECT COUNT(*) c FROM vpns WHERE node_id=?", (n["id"],)) or {})["c"]
    pv = panel_versions()
    d["is_master"] = bool(n.get("ip") and n["ip"] in pv["master_ips"]) or n.get("host") in pv["master_ips"]
    d["master_version"] = pv["master"] if d["is_master"] else ""
    nv = d["info"].get("version", "")
    rel = update.state()
    latest = max([v for v in (pv["agent"], rel.get("latest_agent", "")) if v] or [""], key=_vt)
    d["agent_outdated"] = bool(latest and nv and _vt(nv) < _vt(latest))
    d["agent_latest"] = latest
    d["can_update"] = d["agent_outdated"] and bool(nv) and bool(rel.get("latest_agent")) \
        and _vt(nv) < _vt(rel["latest_agent"])
    return d


async def refresh_node(node_id: int):
    n = q1("SELECT * FROM nodes WHERE id=?", (node_id,))
    try:
        info = await nodes.call(n, "GET", "/api/info", timeout=15)
        ex("UPDATE nodes SET info=?, status='online', error='', last_seen=? WHERE id=?",
           (json.dumps(info), time.time(), node_id))
    except nodes.NodeError as e:
        ex("UPDATE nodes SET status='offline', error=? WHERE id=?", (str(e)[:500], node_id))
    g = await nodes.geo(n["host"])
    if g.get("country_code"):
        ex("UPDATE nodes SET country_code=?, country=?, city=?, ip=? WHERE id=?",
           (g["country_code"], g["country"], g["city"], g["ip"], node_id))
    elif g.get("ip"):
        ex("UPDATE nodes SET ip=? WHERE id=?", (g["ip"], node_id))


async def _live_info(n: dict, timeout: float = 6) -> bool:
    try:
        info = await asyncio.wait_for(nodes.call(n, "GET", "/api/info", timeout=timeout), timeout + 1)
        ex("UPDATE nodes SET info=?, status=CASE WHEN status='offline' THEN 'online' ELSE status END, last_seen=? WHERE id=?",
           (json.dumps(info), time.time(), n["id"]))
        return True
    except (nodes.NodeError, asyncio.TimeoutError):
        return False


async def nodes_list(request: Request):
    all_nodes = q("SELECT * FROM nodes ORDER BY id")
    if request.query_params.get("live") == "1":
        await asyncio.gather(*(_live_info(n) for n in all_nodes))
        all_nodes = q("SELECT * FROM nodes ORDER BY id")
    return ok({"nodes": [node_out(n) for n in all_nodes],
               "master_version": panel_versions()["master"], "agent_latest": panel_versions()["agent"]})


async def nodes_add(request: Request):
    d = await body(request)
    k = nodes.parse_key(str(d.get("key", "")))
    if q1("SELECT id FROM nodes WHERE host=? AND port=?", (k["host"], k["port"])):
        raise ApiError("Этот сервер уже подключён")
    pem = await asyncio.to_thread(nodes.fetch_cert, k["host"], k["port"], k["fp"])
    probe = {"id": -1, "host": k["host"], "port": k["port"], "api_key": k["api_key"], "cert_pem": pem}
    try:
        await nodes.call(probe, "GET", "/api/ping", timeout=10)
    finally:
        nodes.forget(-1)
    g = await nodes.geo(k["host"])
    name = str(d.get("name") or "").strip() or (
        ", ".join(x for x in (g.get("country"), g.get("city")) if x) or k["host"])
    nid = ex("""INSERT INTO nodes(name,host,port,api_key,cert_pem,ip,country_code,country,city,status,
                last_seen,created) VALUES(?,?,?,?,?,?,?,?,?,'online',?,?)""",
             (name, k["host"], k["port"], k["api_key"], pem, g.get("ip", k["host"]),
              g.get("country_code", ""), g.get("country", ""), g.get("city", ""), time.time(), time.time()))
    await refresh_node(nid)
    request_sync()
    return ok(node_out(q1("SELECT * FROM nodes WHERE id=?", (nid,))))


async def node_one(request: Request):
    nid = int(request.path_params["id"])
    n = q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise ApiError("Сервер не найден", 404)
    if request.method == "DELETE":
        try:
            await nodes.call(n, "PUT", "/api/state", {"inbounds": [], "users": []}, timeout=15)
        except nodes.NodeError:
            pass
        ex("DELETE FROM vpns WHERE node_id=?", (nid,))
        ex("DELETE FROM nodes WHERE id=?", (nid,))
        nodes.forget(nid)
        return ok()
    if request.method == "PATCH":
        d = await body(request)
        if "name" in d:
            ex("UPDATE nodes SET name=? WHERE id=?", (str(d["name"]).strip()[:60], nid))
        if d.get("country_code"):
            ex("UPDATE nodes SET country_code=? WHERE id=?", (str(d["country_code"]).upper()[:2], nid))
        if d.get("key"):
            k = nodes.parse_key(d["key"])
            pem = await asyncio.to_thread(nodes.fetch_cert, k["host"], k["port"], k["fp"])
            ex("UPDATE nodes SET host=?, port=?, api_key=?, cert_pem=? WHERE id=?",
               (k["host"], k["port"], k["api_key"], pem, nid))
            nodes.forget(nid)
        request_sync()
    if request.method == "GET" and request.query_params.get("live") == "1":
        alive = await _live_info(n, timeout=5)
        return ok({**node_out(q1("SELECT * FROM nodes WHERE id=?", (nid,))), "live": alive})
    return ok(node_out(q1("SELECT * FROM nodes WHERE id=?", (nid,))))


async def node_action(request: Request):
    nid = int(request.path_params["id"])
    n = q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise ApiError("Сервер не найден", 404)
    act = request.path_params["action"]
    if act == "refresh":
        await refresh_node(nid)
        request_sync()
    elif act == "restart":
        r = await nodes.call(n, "POST", "/api/xray/restart")
        if not r.get("ok"):
            raise ApiError("Не удалось перезапустить Xray: " + r.get("error", ""))
    return ok(node_out(q1("SELECT * FROM nodes WHERE id=?", (nid,))))


def _tcp_check(host: str, port: int, timeout=4) -> tuple[bool, str]:
    import socket
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{(time.time() - t0) * 1000:.0f} мс"
    except Exception as e:
        return False, str(e)[:150]


async def node_diag(request: Request):
    nid = int(request.path_params["id"])
    n = q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise ApiError("Сервер не найден", 404)
    await push_state(n, active_users())
    try:
        res = await nodes.call(n, "GET", "/api/diag?lang=" + LANG.get(), timeout=120)
    except nodes.NodeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            raise ApiError("На сервере старая версия work-панели — обновите её: bash install-node.sh")
        raise
    checks = res.get("checks", [])
    drift = abs(time.time() - float(res.get("time") or time.time()))
    checks.insert(0, {"name": "Время на сервере совпадает с master", "status": "ok" if drift < 60 else "warn",
                      "detail": f"расхождение {drift:.0f} с", "hint": "Включите синхронизацию времени: timedatectl set-ntp true"})
    for v in q("SELECT * FROM vpns WHERE node_id=? AND enabled=1", (nid,)):
        okk, d = await asyncio.to_thread(_tcp_check, n["host"], int(v["port"]))
        checks.append({"name": f"Порт {v['port']} доступен с master-сервера (как для клиентов из России)",
                       "status": "ok" if okk else "fail", "detail": d,
                       "hint": "Порт закрыт снаружи: откройте его в фаерволе хостера (security groups) и в ufw, "
                               "или IP сервера блокируется — попробуйте другой порт/транспорт"})
    for ch in checks:
        for k in ("name", "detail", "hint"):
            ch[k] = tr(ch.get(k, ""))
    return ok({"checks": checks, "error_log": res.get("error_log", ""), "node": node_out(n)})


async def reload_all(request: Request):
    users = active_users()
    results = []
    for n in q("SELECT * FROM nodes ORDER BY id"):
        await refresh_node(n["id"])
        err = await push_state(n, users, force=True)
        results.append({"id": n["id"], "name": n["name"], "flag": core.flag(n["country_code"]), "country_code": n["country_code"],
                        "ok": err is None, "error": tr(err or "")})
    try:
        await asyncio.wait_for(asyncio.shield(bot.start()), timeout=25)
    except asyncio.TimeoutError:
        bot.error = "Подключение к Telegram занимает слишком долго — проверьте способ подключения"
    request_sync()
    return ok({"nodes": results, "bot": bot_status(), "users": len(users)})


def vpn_out(v):
    n = q1("SELECT id,name,host,country_code,status FROM nodes WHERE id=?", (v["node_id"],)) or {}
    d = {k: val for k, val in v.items() if k != "private_key"}
    d["node"] = {**n, "flag": core.flag(n.get("country_code", ""))} if n else None
    return d


PORT_CANDIDATES = [443, 8443, 2053, 2083, 2087, 2096, 9443, 10443]


def _ver_ok(info: dict, need: str) -> bool:
    return bool(info.get("version")) and _vt(info["version"]) >= _vt(need)


async def _release(n: dict, ports: list[int]):
    if ports:
        try:
            await nodes.call(n, "POST", "/api/port/release", {"ports": ports}, timeout=8)
        except nodes.NodeError:
            pass


async def _reach(n: dict, ports: list[int]) -> dict | None:
    info = json.loads(n.get("info") or "{}")
    if not _ver_ok(info, "1.0.0"):
        return None
    try:
        lst = await nodes.call(n, "POST", "/api/port/listen", {"ports": ports, "seconds": 12, "open_fw": True}, timeout=15)
    except nodes.NodeError:
        return None
    testable = set(lst.get("listening", [])) | {int(p) for p, who in (lst.get("busy") or {}).items() if who == "xray"}
    if not testable:
        return {}
    tports = sorted(testable)
    probers = [x for x in q("SELECT * FROM nodes WHERE id!=? AND status='online'", (n["id"],))
               if _ver_ok(json.loads(x.get("info") or "{}"), "1.0.0")][:3]
    pv = panel_versions()
    same_server = (n.get("ip") in pv["master_ips"]) or (n.get("host") in pv["master_ips"])

    async def by_node(pr):
        try:
            r = await nodes.call(pr, "POST", "/api/port/probe", {"host": n["host"], "ports": tports, "timeout": 3}, timeout=10)
            return pr["name"], {int(k): v.get("ok") for k, v in (r.get("results") or {}).items()}
        except nodes.NodeError:
            return pr["name"], None

    async def by_master():
        res = await asyncio.gather(*(asyncio.to_thread(_tcp_check, n["host"], p, 3) for p in tports))
        return "master", {p: r[0] for p, r in zip(tports, res)}

    jobs = [by_node(pr) for pr in probers] + ([] if same_server else [by_master()])
    if not jobs:
        await _release(n, lst.get("listening", []))
        return None
    out = {p: {"ok": 0, "total": 0, "from": []} for p in tports}
    results = await asyncio.gather(*jobs)
    await _release(n, lst.get("listening", []))
    for name, res in results:
        if res is None:
            continue
        for p in tports:
            if p in res:
                out[p]["total"] += 1
                if res[p]:
                    out[p]["ok"] += 1
                out[p]["from"].append(name)
    return out


async def port_guard(node_id: int, port: int, vid: int | None, force: bool = False) -> dict:
    n = q1("SELECT * FROM nodes WHERE id=?", (node_id,))
    if not n:
        raise ApiError("Выберите подключённый сервер")
    try:
        info = await nodes.call(n, "GET", "/api/info", timeout=10)
        ex("UPDATE nodes SET info=? WHERE id=?", (json.dumps(info), node_id))
        n["info"] = json.dumps(info)
    except nodes.NodeError:
        info = json.loads(n.get("info") or "{}")
    busy = {int(p): who for p, who in (info.get("busy_ports") or {}).items()}
    busy[int(n["port"])] = "work-панель"
    for v in q("SELECT port, name FROM vpns WHERE node_id=? AND id!=?", (node_id, vid or 0)):
        busy[int(v["port"])] = f"VPN «{v['name']}»"
    free = [p for p in PORT_CANDIDATES if p not in busy and p != port]

    if port in busy:
        r = await _reach(n, free[:5]) if free else None
        sugg = [p for p in free if r is None or (p in r and (r[p]["ok"] > 0 or r[p]["total"] == 0))][:4] or free[:4]
        raise ApiError(f"Порт {port} занят: {busy[port]}", extra={"code": "port_busy", "suggest": sugg})

    if force:
        return {"checked": False}
    r = await _reach(n, [port] + free[:4])
    if r is None:
        return {"checked": False}
    pr = r.get(port)
    if pr is None:
        raise ApiError(f"Не удалось проверить порт {port} на сервере — попробуйте ещё раз",
                       extra={"code": "port_unreachable", "suggest": [p for p in free[:4] if p in r and r[p]["ok"] > 0],
                              "can_force": True})
    if pr and pr["total"] > 0 and pr["ok"] == 0:
        sugg = [p for p in free[:4] if p in r and r[p]["ok"] > 0]
        raise ApiError(f"Порт {port} недоступен снаружи (проверено с: {', '.join(pr['from'])})",
                       extra={"code": "port_unreachable", "suggest": sugg, "can_force": True})
    return {"checked": bool(pr and pr["total"]), "from": pr["from"] if pr else []}


def validate_vpn(d: dict, node_id: int, vid: int | None = None) -> dict:
    out = {}
    n = q1("SELECT * FROM nodes WHERE id=?", (node_id,))
    if not n:
        raise ApiError("Выберите подключённый сервер")
    port = int(d.get("port") or 443)
    if not 1 <= port <= 65535:
        raise ApiError("Порт должен быть от 1 до 65535")
    transport = d.get("transport", "tcp")
    if transport not in TRANSPORTS:
        raise ApiError("Неизвестный транспорт")
    fp = d.get("fingerprint", "chrome")
    if fp not in FINGERPRINTS:
        raise ApiError("Неизвестный отпечаток")
    sni = str(d.get("sni") or "www.microsoft.com").strip().lower()
    sni = sni.replace("https://", "").replace("http://", "").split("/")[0]
    if not sni or " " in sni:
        raise ApiError("Укажите домен для маскировки")
    path = str(d.get("path") or "").strip()
    if transport == "xhttp" and not path:
        path = "/" + secrets.token_hex(6)
    if path and not path.startswith("/"):
        path = "/" + path
    service = str(d.get("service_name") or "").strip() or (secrets.token_hex(5) if transport == "grpc" else "")
    out.update(node_id=node_id, port=port, transport=transport, fingerprint=fp, sni=sni,
               dest=f"{sni}:443", path=path, service_name=service,
               name=str(d.get("name") or "").strip()[:60] or f"{n['name']} · {transport.upper()}",
               enabled=1 if d.get("enabled", True) else 0, sort=int(d.get("sort") or 0))
    return out


async def vpns_list(request: Request):
    if request.method == "POST":
        d = await body(request)
        v = validate_vpn(d, int(d.get("node_id") or 0))
        await port_guard(v["node_id"], v["port"], None, bool(d.get("force")))
        priv, pub = core.reality_keypair()
        vid = ex("""INSERT INTO vpns(name,node_id,port,transport,sni,dest,fingerprint,private_key,public_key,
                    short_id,path,service_name,enabled,sort,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (v["name"], v["node_id"], v["port"], v["transport"], v["sni"], v["dest"], v["fingerprint"],
                  priv, pub, secrets.token_hex(4), v["path"], v["service_name"], v["enabled"], v["sort"],
                  time.time()))
        request_sync()
        return ok(vpn_out(q1("SELECT * FROM vpns WHERE id=?", (vid,))))
    return ok([vpn_out(v) for v in q("SELECT * FROM vpns ORDER BY sort, id")])


async def vpn_one(request: Request):
    vid = int(request.path_params["id"])
    v = q1("SELECT * FROM vpns WHERE id=?", (vid,))
    if not v:
        raise ApiError("VPN не найден", 404)
    if request.method == "DELETE":
        ex("DELETE FROM vpns WHERE id=?", (vid,))
        request_sync()
        return ok()
    if request.method == "PATCH":
        d = await body(request)
        merged = {**v, **d}
        nv = validate_vpn(merged, int(merged["node_id"]), vid)
        if nv["port"] != v["port"] or nv["node_id"] != v["node_id"] or (nv["enabled"] and not v["enabled"]):
            await port_guard(nv["node_id"], nv["port"], vid, bool(d.get("force")))
        ex("""UPDATE vpns SET name=?,node_id=?,port=?,transport=?,sni=?,dest=?,fingerprint=?,path=?,
              service_name=?,enabled=?,sort=? WHERE id=?""",
           (nv["name"], nv["node_id"], nv["port"], nv["transport"], nv["sni"], nv["dest"], nv["fingerprint"],
            nv["path"], nv["service_name"], nv["enabled"], nv["sort"], vid))
        if d.get("regen_keys"):
            priv, pub = core.reality_keypair()
            ex("UPDATE vpns SET private_key=?, public_key=?, short_id=? WHERE id=?",
               (priv, pub, secrets.token_hex(4), vid))
        request_sync()
    return ok(vpn_out(q1("SELECT * FROM vpns WHERE id=?", (vid,))))


async def users_list(request: Request):
    if request.method == "POST":
        d = await body(request)
        u = core.create_user(name=str(d.get("name") or "").strip()[:80])
        if d.get("forever"):
            ex("UPDATE users SET forever=1 WHERE id=?", (u["id"],))
        days = float(d.get("days") or 0)
        if days:
            core.extend_user(u["id"], days)
        ex("UPDATE users SET traffic_limit=?, device_limit=?, note=? WHERE id=?",
           (int(float(d.get("traffic_limit_gb") or 0) * core.GB), int(d.get("device_limit") or 0),
            str(d.get("note") or ""), u["id"]))
        request_sync()
        return ok(core.user_public(q1("SELECT * FROM users WHERE id=?", (u["id"],))))
    return ok([core.user_public(u) for u in q("SELECT * FROM users ORDER BY id DESC")])


async def user_one(request: Request):
    uid = int(request.path_params["id"])
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise ApiError("Пользователь не найден", 404)
    if request.method == "DELETE":
        ex("DELETE FROM users WHERE id=?", (uid,))
        request_sync()
        return ok()
    if request.method == "PATCH":
        d = await body(request)
        fields = {}
        if "name" in d:
            fields["name"] = str(d["name"]).strip()[:80]
        if "phone" in d:
            ph = core.normalize_phone(d["phone"]) if str(d["phone"]).strip() else ""
            if str(d["phone"]).strip() and not ph:
                raise ApiError("Неверный номер телефона")
            if ph:
                core.set_user_phone(uid, ph)
            else:
                fields["phone"] = ""
        if "note" in d:
            fields["note"] = str(d["note"])[:500]
        if "expires_at" in d:
            fields["expires_at"] = float(d["expires_at"] or 0)
            fields["notified_3d"] = fields["notified_exp"] = 0
        if "traffic_limit_gb" in d:
            fields["traffic_limit"] = int(float(d["traffic_limit_gb"] or 0) * core.GB)
        if "device_limit" in d:
            fields["device_limit"] = max(0, int(d["device_limit"] or 0))
        if d.get("lang") in ("ru", "en"):
            fields["lang"] = d["lang"]
        if "forever" in d:
            fields["forever"] = 1 if d["forever"] else 0
            fields["notified_3d"] = fields["notified_exp"] = 0
        if "block_torrent" in d:
            fields["block_torrent"] = 1 if d["block_torrent"] else 0
        if "enabled" in d:
            fields["enabled"] = 1 if d["enabled"] else 0
        if "used_gb" in d:
            fields["used_up"] = 0
            fields["used_down"] = int(float(d["used_gb"] or 0) * core.GB)
        if fields:
            ex(f"UPDATE users SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), uid))
        request_sync()
    d = core.user_public(q1("SELECT * FROM users WHERE id=?", (uid,)))
    d["links"] = core.user_links(d)
    d["payments"] = q("SELECT p.*, pr.code AS promo_code FROM payments p LEFT JOIN promos pr ON pr.id=p.promo_id "
                      "WHERE p.user_id=? ORDER BY p.id DESC", (uid,))
    return ok(d)


async def user_action(request: Request):
    uid = int(request.path_params["id"])
    if not q1("SELECT id FROM users WHERE id=?", (uid,)):
        raise ApiError("Пользователь не найден", 404)
    act = request.path_params["action"]
    d = await body(request)
    if act == "reset_traffic":
        ex("UPDATE users SET used_up=0, used_down=0 WHERE id=?", (uid,))
    elif act == "extend":
        core.extend_user(uid, float(d.get("days") or 30))
    elif act == "regen":
        core.regen_user(uid)
    elif act == "unsuspend":
        ex("UPDATE users SET suspended_until=0 WHERE id=?", (uid,))
    elif act == "message":
        u = q1("SELECT tg_id FROM users WHERE id=?", (uid,))
        if not u["tg_id"]:
            raise ApiError("У пользователя нет Telegram")
        await bot.notify(u["tg_id"], str(d.get("text", ""))[:3500])
    else:
        raise ApiError("Неизвестное действие")
    request_sync()
    return ok()


def promo_out(p: dict) -> dict:
    d = dict(p)
    d["plans"] = promo.plan_list(p)
    d["stats"] = promo.stats(p["id"])
    d["bot_link"] = f"https://t.me/{bot.username}?start={p['code']}" if bot.username else ""
    t = time.time()
    d["state"] = ("disabled" if not p["enabled"] else "expired" if p["expires_at"] and p["expires_at"] < t
                  else "exhausted" if p["max_uses"] and p["used"] >= p["max_uses"] else "active")
    return d


def validate_promo(d: dict, pid: int = 0) -> dict:
    code = str(d.get("code", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", code):
        raise ApiError("Промокод: 3–32 символа, латинские буквы, цифры, - и _")
    if q1("SELECT id FROM promos WHERE code=? COLLATE NOCASE AND id!=?", (code, pid)):
        raise ApiError("Такой промокод уже есть")
    discount = int(d.get("discount") or 0)
    if not 1 <= discount <= 100:
        raise ApiError("Скидка должна быть от 1 до 100%")
    plans = sorted({int(m) for m in d.get("plans") or [] if int(m) in payments.PLAN_OPTIONS})
    max_uses = max(0, int(d.get("max_uses") or 0))
    return {"code": code, "discount": discount, "months": ",".join(map(str, plans)),
            "expires_at": float(d.get("expires_at") or 0), "max_uses": max_uses,
            "reusable": 1 if d.get("reusable") else 0, "new_only": 1 if d.get("new_only") else 0,
            "enabled": 0 if d.get("enabled") is False else 1, "note": str(d.get("note") or "")[:200]}


async def promos_list(request: Request):
    if request.method == "POST":
        v = validate_promo(await body(request))
        pid = ex("INSERT INTO promos(code,discount,months,expires_at,max_uses,reusable,new_only,enabled,note,created) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (*v.values(), time.time()))
        return ok(promo_out(promo.get(pid)))
    return ok([promo_out(p) for p in q("SELECT * FROM promos ORDER BY id DESC")])


async def promo_one(request: Request):
    pid = int(request.path_params["id"])
    p = promo.get(pid)
    if not p:
        raise ApiError("Промокод не найден", 404)
    if request.method == "DELETE":
        ex("DELETE FROM promos WHERE id=?", (pid,))
        ex("UPDATE users SET promo_id=0 WHERE promo_id=?", (pid,))
        return ok()
    if request.method == "PATCH":
        d = await body(request)
        v = validate_promo({**p, "plans": promo.plan_list(p), **d}, pid)
        ex(f"UPDATE promos SET {', '.join(f'{k}=?' for k in v)} WHERE id=?", (*v.values(), pid))
        if d.get("reset_used"):
            ex("UPDATE promos SET used=0 WHERE id=?", (pid,))
    out = promo_out(promo.get(pid))
    out["uses"] = q("""SELECT pu.*, u.name, u.tg_username FROM promo_uses pu LEFT JOIN users u ON u.id=pu.user_id
                       WHERE pu.promo_id=? ORDER BY pu.id DESC LIMIT 100""", (pid,))
    return ok(out)


def _zip_response(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/zip",
                    headers={"content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})


async def after_restore():
    nodes._ctx_cache.clear()
    request_sync()
    asyncio.create_task(bot.start())


async def backup_download(request: Request):
    try:
        parts = backup.clean_parts(request.query_params.get("parts") or "")
        data = await asyncio.to_thread(backup.make_zip, parts)
    except backup.BackupError as e:
        raise ApiError(str(e))
    suffix = "" if parts == backup.ALL_PARTS else "-partial"
    return _zip_response(data, f"hidevp-backup{suffix}-{time.strftime('%Y%m%d-%H%M')}.zip")


async def backup_restore(request: Request):
    mode = request.query_params.get("mode", "merge")
    data = await request.body()
    if not data:
        raise ApiError("Выберите файл резервной копии")
    try:
        res = await asyncio.to_thread(backup.restore, data, mode)
    except backup.BackupError as e:
        raise ApiError(str(e))
    await after_restore()
    return ok(res)


async def backups_list(request: Request):
    return ok(backup.list_backups())


async def backup_schedule(request: Request):
    if request.method == "PUT":
        d = await body(request)
        if "auto" in d:
            if d["auto"] not in ("off", *backup.PERIODS):
                raise ApiError("Неизвестный период")
            set_setting("backup_auto", d["auto"])
        if "keep" in d:
            try:
                keep = int(d["keep"])
            except (TypeError, ValueError):
                keep = 0
            if not 1 <= keep <= 100:
                raise ApiError("Хранить можно от 1 до 100 копий")
            set_setting("backup_keep", str(keep))
    return ok(backup.schedule())


async def backup_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if backup.scheduled_due():
                p = await asyncio.to_thread(backup.auto_backup, "scheduled")
                log.info("scheduled backup %s", p.name)
        except Exception:
            log.exception("scheduled backup")
        await asyncio.sleep(600)


def _pay_out(cfg: dict) -> dict:
    return {"config": cfg, "enabled": payments.enabled(cfg), "currencies": payments.CURRENCIES,
            "assets": list(payments.CRYPTO_ASSETS), "stars_prices": {str(k): v for k, v in prices().items()},
            "plan_options": list(payments.PLAN_OPTIONS),
            "methods": {m: {"ru": n[0], "en": n[1], "ready": payments.configured(m, cfg)} for m, n in payments.METHOD_NAMES.items()},
            "pending": payments.manual_pending(), "bot": bot_status(), "admins": admin_notify_state()}


async def pay_manual(request: Request):
    oid, act = int(request.path_params["id"]), request.path_params["action"]
    if act not in ("confirm", "reject"):
        raise ApiError("Неизвестное действие")
    a = current_admin(request)
    st = await bot.manual_decide(oid, act == "confirm", "admin:" + a["username"])
    if not st:
        raise ApiError("Заявка уже обработана")
    return ok({"status": st, "pending": payments.manual_pending()})


async def pay_settings(request: Request):
    if request.method == "PUT":
        try:
            cfg = payments.save(await body(request))
        except payments.PayError as e:
            raise ApiError(str(e))
    else:
        cfg = payments.load()
    return ok(_pay_out(cfg))


async def pay_check_crypto(request: Request):
    d = await body(request)
    cfg = payments.load()
    if d.get("token"):
        cfg["crypto"].update(token=str(d["token"]).strip(), testnet=bool(d.get("testnet")))
    try:
        return ok(await asyncio.to_thread(payments.crypto_check, cfg))
    except payments.PayError as e:
        raise ApiError(str(e))


async def user_pay(request: Request):
    u = find_user_by_token(request.path_params["token"])
    if not u:
        raise ApiError("Пользователь не найден", 404)
    if not _rate("pay:" + u["token"], 10, 3600):
        raise ApiError("Слишком много запросов. Попробуйте через час.", 429)
    d = await body(request)
    method, months = str(d.get("method", "")), int(d.get("months") or 0)
    if method not in payments.enabled() or months not in payments.plans() or u.get("forever"):
        raise ApiError("Тариф недоступен.")
    LANG.set(core.user_lang(u))
    try:
        url = await bot.invoice_link(u, months, method)
    except payments.PayError as e:
        if str(e) == "free":
            return ok({"free": True, "bot": bot_link()})
        raise ApiError(str(e))
    except Exception as e:
        log.warning("site payment %s: %s", method, e)
        raise ApiError("Не удалось создать счёт. Попробуйте другой способ оплаты или позже.", 502)
    return ok({"url": url})


def site_plans(u: dict) -> list:
    cfg = payments.load()
    p = promo.get(u.get("promo_id") or 0)
    if p and promo.check(p, u):
        p = None
    out = []
    for m in payments.plan_months(cfg):
        opts = []
        for meth in payments.enabled(cfg):
            base, cost = payments.quote(meth, m, p, cfg)
            if base:
                cur = payments.currency(meth, cfg)
                opts.append({"method": meth, "price": payments.fmt(cost, cur), "full": payments.fmt(base, cur),
                             "discount": cost != base})
        out.append({"months": m, "options": opts})
    return out


async def notices_api(request: Request):
    if request.method == "POST":
        try:
            n = notices.create(await body(request))
        except notices.NoticeError as e:
            raise ApiError(str(e))
        return ok(n)
    return ok({"items": notices.list_all(), "kinds": notices.KINDS, "bot": bot_status(),
               "users": (q1("SELECT COUNT(*) c FROM users WHERE tg_id IS NOT NULL AND tg_id!=0") or {})["c"]})


async def notice_one(request: Request):
    nid = int(request.path_params["id"])
    if request.method == "DELETE":
        ex("DELETE FROM notices WHERE id=?", (nid,))
        cnt = notices.sent_messages(nid)
        if cnt:
            asyncio.create_task(notices.delete_sent(bot, nid))
        return ok({"tg_deleting": cnt})
    old = notices.get(nid)
    try:
        n = notices.update(nid, await body(request))
    except notices.NoticeError as e:
        raise ApiError(str(e))
    if old and (old["text"], old["text_en"], old["kind"]) != (n["text"], n["text_en"], n["kind"]) and notices.sent_messages(nid):
        asyncio.create_task(notices.edit_sent(bot, n))
    return ok(n)


async def notice_send(request: Request):
    n = notices.get(int(request.path_params["id"]))
    if not n:
        raise ApiError("Уведомление не найдено")
    if not bot.bot:
        raise ApiError("Бот не запущен")
    ex("UPDATE notices SET sent_at=? WHERE id=?", (time.time(), n["id"]))
    asyncio.create_task(notices.broadcast(bot, n))
    return ok({"started": True})


async def stats_api(request: Request):
    return ok(await asyncio.to_thread(stats.report, stats.parse(request.query_params)))


async def stats_csv(request: Request):
    data = await asyncio.to_thread(stats.export_csv, stats.parse(request.query_params))
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"content-disposition": f'attachment; filename="hidevp-payments-{time.strftime("%Y%m%d")}.csv"'})


def _tg_hint(err: str) -> str:
    e = err.lower()
    if not err:
        return ""
    if "chat not found" in e or "initiate conversation" in e or "blocked" in e or "deactivated" in e:
        return "Откройте бота с этого аккаунта и нажмите «Старт» (/start) — без этого бот не может писать"
    if "бот не запущен" in e:
        return "Бот не запущен — проверьте токен"
    return err[:200]


def admin_notify_state() -> dict:
    try:
        last = json.loads(get_setting("admin_notify") or "{}")
    except ValueError:
        last = {}
    ids = sorted(bot.admin_ids())
    res = last.get("results") or {}
    return {"ids": ids, "ts": last.get("ts", 0),
            "results": [{"id": i, "ok": res.get(str(i)) == "", "known": str(i) in res,
                         "error": _tg_hint(res.get(str(i), ""))} for i in ids]}


async def admin_link(request: Request):
    if not bot.username:
        raise ApiError("Бот не запущен")
    code = secrets.token_hex(4).upper()
    set_setting("admin_link", json.dumps({"code": code, "until": time.time() + 900}))
    return ok({"code": code, "url": f"https://t.me/{bot.username}?start=admin_{code}", "bot": bot.username, "ttl": 900})


async def admin_test(request: Request):
    if not bot.admin_ids():
        raise ApiError("Не указан ни один Telegram ID администратора")
    await bot.notify_admins(TA_test())
    return ok(admin_notify_state())


def TA_test() -> str:
    return ("🔔 Проверка: уведомления администратору HideVP приходят." if get_setting("lang", "ru") != "en"
            else "🔔 Test: HideVP admin notifications are delivered.")


async def admin_notify_get(request: Request):
    return ok(admin_notify_state())


async def bot_ui(request: Request):
    if request.method == "PUT":
        try:
            cfg = botui.save(await body(request))
        except botui.BotUiError as e:
            raise ApiError(str(e))
        except (TypeError, ValueError):
            raise ApiError("Неверные данные")
    else:
        cfg = botui.load()
    return ok({"config": cfg, "defaults": botui.defaults(), "builtin": botui.BUILTIN, "hints": botui.BUILTIN_HINT,
               "placeholders": botui.PLACEHOLDERS, "bot": bot_status(),
               "title": get_setting("panel_title", "VPN"), "support": get_setting("support"),
               "public_url": core.public_url()})


async def backup_create(request: Request):
    d = await body(request)
    try:
        parts = backup.clean_parts(d.get("parts") or "")
        p = await asyncio.to_thread(backup.auto_backup, "manual", parts)
    except backup.BackupError as e:
        raise ApiError(str(e))
    return ok({"name": p.name})


async def backup_file(request: Request):
    name = request.path_params["name"]
    try:
        data = backup.read_backup(name)
    except backup.BackupError as e:
        raise ApiError(str(e), 404)
    if request.method == "POST":
        try:
            res = await asyncio.to_thread(backup.restore, data, request.query_params.get("mode", "replace"))
        except backup.BackupError as e:
            raise ApiError(str(e))
        await after_restore()
        return ok(res)
    if request.method == "DELETE":
        (backup.BACKUP_DIR / Path(name).name).unlink(missing_ok=True)
        return ok()
    return _zip_response(data, name)


async def update_state(request: Request):
    if request.method == "POST":
        return ok(await asyncio.to_thread(update.check))
    return ok(update.state())


async def update_master(request: Request):
    d = await body(request)
    try:
        res = await asyncio.to_thread(update.start_master_update, str(d.get("version") or "") or None,
                                      bool(d.get("rollback")))
    except update.UpdateError as e:
        raise ApiError(str(e))
    return ok(res)


async def update_log(request: Request):
    return ok({"log": update.read_log(), **update.state()})


async def node_update(request: Request):
    nid = int(request.path_params["id"])
    n = q1("SELECT * FROM nodes WHERE id=?", (nid,))
    if not n:
        raise ApiError("Сервер не найден", 404)
    info = json.loads(n.get("info") or "{}")
    if not info.get("version"):
        raise ApiError("Нет связи с work-панелью — нажмите «Обновить» на карточке сервера")
    d = await body(request)
    version, rollback = str(d.get("version") or "") or None, bool(d.get("rollback"))
    try:
        payload = await asyncio.to_thread(update.agent_payload, version)
        if version:
            update.target_check(payload["version"], info.get("version", ""), rollback)
        elif not payload["version"] or _vt(payload["version"]) <= _vt(info.get("version", "")):
            raise update.UpdateError("Нет информации об обновлении")
    except update.UpdateError as e:
        raise ApiError(str(e))
    try:
        r = await nodes.call(n, "POST", "/api/self-update", payload, timeout=180)
    except nodes.NodeError as e:
        raise ApiError(f"Узел «{n['name']}»: {e}")
    ex("UPDATE nodes SET info=? WHERE id=?", (json.dumps({**info, "updating": payload["version"]}), nid))
    return ok({"started": True, "version": payload["version"], **(r or {})})


async def updates_overview(request: Request):
    force = request.query_params.get("force") == "1"
    st = await asyncio.to_thread(update.check) if force or not update.STATE["checked_at"] else update.state()
    rel = await asyncio.to_thread(update.releases, force)
    pv = panel_versions()
    items = []
    for n in q("SELECT * FROM nodes ORDER BY id"):
        d = node_out(n)
        items.append({"id": n["id"], "name": n["name"], "host": n["host"], "flag": d["flag"], "status": n["status"],
                      "version": d["info"].get("version", ""), "updating": d["info"].get("updating", ""),
                      "is_master": d["is_master"], "error": d["error"]})
    return ok({"master": {"installed": pv["master"], "agent_files": pv["agent"]}, "update": st,
               "releases": rel["items"], "releases_error": rel["error"], "nodes": items})


async def notify_update(st: dict):
    await bot.notify_admins(
        (f"⬆️ Доступна новая версия HideVP: <b>{st['latest_version']}</b> (у вас {st['installed']}).\n"
         "Обновить: админка → Обновления.") if get_setting("lang", "ru") != "en" else
        (f"⬆️ A new HideVP version is available: <b>{st['latest_version']}</b> (you have {st['installed']}).\n"
         "Update: admin panel → Updates."))


SETTING_KEYS = ("panel_title", "public_url", "bot_token", "admin_tg_ids", "support",
                "default_traffic_limit_gb", "default_device_limit", "tg_route", "tg_node_id", "tg_proxy", "lang")
TG_KEYS = ("bot_token", "tg_route", "tg_node_id", "tg_proxy")


async def settings(request: Request):
    if request.method == "PUT":
        d = await body(request)
        old_tg = [get_setting(k) for k in TG_KEYS]
        for k in SETTING_KEYS:
            if k in d:
                v = str(d[k]).strip()
                if k == "public_url":
                    v = v.rstrip("/")
                    if v and not v.startswith(("http://", "https://")):
                        raise ApiError("Публичный адрес должен начинаться с http:// или https://")
                if k == "lang" and v not in ("ru", "en"):
                    raise ApiError("Неизвестное действие")
                if k == "tg_route" and v not in ("direct", "node", "proxy"):
                    raise ApiError("Неизвестный способ подключения к Telegram")
                if k == "tg_proxy" and v and not v.startswith(("socks5://", "socks5h://", "http://", "https://")):
                    raise ApiError("Прокси: формат socks5://host:port или http://host:port")
                set_setting(k, v)
        if "prices" in d:
            pr = {str(int(m)): int(p) for m, p in d["prices"].items() if int(p) > 0}
            set_setting("prices", json.dumps(pr))
        if [get_setting(k) for k in TG_KEYS] != old_tg or d.get("restart_bot"):
            try:
                await asyncio.wait_for(asyncio.shield(bot.start()), timeout=25)
            except asyncio.TimeoutError:
                bot.error = "Подключение к Telegram занимает слишком долго — проверьте способ подключения"
    out = {k: get_setting(k) for k in SETTING_KEYS}
    out["prices"] = {str(k): v for k, v in prices().items()}
    out["bot"] = bot_status()
    out["nodes"] = [{"id": n["id"], "name": n["name"], "flag": core.flag(n["country_code"]), "country_code": n["country_code"],
                     "status": n["status"], "info": json.loads(n["info"] or "{}")}
                    for n in q("SELECT * FROM nodes ORDER BY id")]
    out["web_port"] = get_setting("web_port")
    out["web_path"] = get_setting("web_path")
    return ok(out)


def find_user_by_token(token: str):
    return q1("SELECT * FROM users WHERE token=?", (token,)) if token else None


async def subscription(request: Request):
    u = find_user_by_token(request.path_params["token"])
    if not u:
        return PlainTextResponse("Not found", status_code=404)
    links = core.user_links(u) if core.user_status(u) == "active" else []
    title = get_setting("panel_title", "VPN")
    headers = {
        "profile-title": "base64:" + base64.b64encode(title.encode()).decode(),
        "profile-update-interval": "6",
        "subscription-userinfo": f"upload={u['used_up']}; download={u['used_down']}; "
                                 f"total={u['traffic_limit']}; expire={0 if u.get('forever') else int(u['expires_at'])}",
        "profile-web-page-url": core.user_page_url(u),
        "content-disposition": f'attachment; filename="{title}"'.encode("ascii", "ignore").decode(),
        "cache-control": "no-store",
    }
    sup = get_setting("support")
    if sup.startswith(("http://", "https://")):
        headers["support-url"] = sup
    ann = notices.announce(u, core.user_lang(u))
    if ann:
        headers["announce"] = "base64:" + base64.b64encode(ann.encode()).decode()
    if core.user_status(u) != "active":
        headers["announce"] = "base64:" + base64.b64encode(
            f"Подписка неактивна: {core.STATUS_TEXT[core.user_status(u)]}".encode()).decode()
    payload = base64.b64encode("\n".join(links).encode()).decode()
    return Response(payload, media_type="text/plain; charset=utf-8", headers=headers)


async def user_page(request: Request):
    u = find_user_by_token(request.path_params["token"])
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    st = core.user_status(u)
    data = {
        "title": get_setting("panel_title", "VPN"),
        "name": u["name"] or (("@" + u["tg_username"]) if u["tg_username"] else f"Пользователь #{u['id']}"),
        "status": st, "status_text": core.STATUS_TEXT[st],
        "expires": core.expires_text(u), "expires_ts": 0 if u.get("forever") else u["expires_at"],
        "forever": bool(u.get("forever")),
        "days_left": max(0, int((u["expires_at"] - time.time()) // core.DAY)) if u["expires_at"] else 0,
        "used": u["used_up"] + u["used_down"], "up": u["used_up"], "down": u["used_down"],
        "limit": u["traffic_limit"], "device_limit": u["device_limit"],
        "devices": len(json.loads(u["online_ips"] or "[]")),
        "sub_url": core.sub_url(u), "happ": core.happ_link(u), "incy": core.incy_link(u), "qr": qr.svg(core.sub_url(u)),
        "logged_in": request.cookies.get(USER_COOKIE) == u["token"],
        "servers": [{"name": r["name"], "flag": core.flag(r["cc"]), "cc": (r["cc"] or "").lower() if re.fullmatch(r"[A-Za-z]{2}", r["cc"] or "") else ""} for r in
                    q("SELECT v.name, n.country_code cc FROM vpns v JOIN nodes n ON n.id=v.node_id "
                      "WHERE v.enabled=1 ORDER BY v.sort, v.id")],
        "bot": bot.username, "support": get_setting("support"), "lang": core.user_lang(u),
        "plans": [] if u.get("forever") else site_plans(u),
        "notices": notices.public(u, core.user_lang(u)),
        "promo": (lambda p: {"code": p["code"], "discount": p["discount"]} if p and not promo.check(p, u) else None)(
            promo.get(u.get("promo_id") or 0)),
    }
    page = (STATIC / "user.html").read_text(encoding="utf-8")
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    return HTMLResponse(page, headers={"cache-control": "no-store"})


async def user_lang(request: Request):
    u = find_user_by_token(request.path_params["token"])
    d = await body(request)
    if u and d.get("lang") in ("ru", "en"):
        ex("UPDATE users SET lang=? WHERE id=?", (d["lang"], u["id"]))
    return ok()


async def not_found(request: Request):
    return PlainTextResponse("Not found", status_code=404)


USER_COOKIE = "hv_u"
CODE_TTL = 300
CODES: dict[str, dict] = {}
AUTH_HITS: dict[str, list[float]] = {}


def _rate(key: str, limit: int, window: int) -> bool:
    now = time.time()
    hits = [t for t in AUTH_HITS.get(key, []) if now - t < window]
    AUTH_HITS[key] = hits
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def bot_link() -> str:
    return f"https://t.me/{bot.username}" if bot.username else ""


async def home(request: Request):
    tok = request.cookies.get(USER_COOKIE)
    if tok and find_user_by_token(tok):
        return RedirectResponse(f"/u/{tok}", status_code=302)
    data = {"title": get_setting("panel_title", "VPN"), "bot": bot_link(), "support": get_setting("support"),
            "lang": get_setting("lang", "ru") or "ru"}
    page = (STATIC / "login.html").read_text(encoding="utf-8")
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    return HTMLResponse(page, headers={"cache-control": "no-store"})


async def auth_request(request: Request):
    d = await body(request)
    phone = core.normalize_phone(d.get("phone", ""))
    if not phone:
        raise ApiError("Введите номер телефона полностью")
    ip = client_ip(request)
    _prune(AUTH_HITS, 3600)
    _prune(CODES, CODE_TTL)
    if not _rate("ip:" + ip, 10, 3600) or not _rate("all", 300, 3600):
        raise ApiError("Слишком много запросов. Попробуйте через час.", 429)
    u = q1("SELECT * FROM users WHERE phone=? AND tg_id IS NOT NULL", (phone,))
    if not u:
        return ok({"status": "not_found", "bot": bot_link()})
    prev = CODES.get(phone)
    if prev and time.time() - prev["created"] < 60:
        return ok({"status": "sent", "wait": int(60 - (time.time() - prev["created"]))})
    if not _rate("phone:" + phone, 5, 3600):
        raise ApiError("Слишком много кодов для этого номера. Попробуйте через час.", 429)
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    if not await bot.send_login_code(u["tg_id"], code, core.user_lang(u)):
        raise ApiError("Не удалось отправить код в Telegram. Откройте бота и нажмите /start.", 503)
    CODES[phone] = {"code": code, "user_id": u["id"], "created": time.time(), "attempts": 0}
    return ok({"status": "sent", "wait": 60})


async def auth_verify(request: Request):
    d = await body(request)
    phone = core.normalize_phone(d.get("phone", ""))
    entry = CODES.get(phone)
    if not entry or time.time() - entry["created"] > CODE_TTL:
        CODES.pop(phone, None)
        raise ApiError("Код устарел. Запросите новый.")
    entry["attempts"] += 1
    if entry["attempts"] > 5:
        CODES.pop(phone, None)
        raise ApiError("Слишком много неверных попыток. Запросите новый код.")
    if not hmac.compare_digest(str(d.get("code", "")).strip(), entry["code"]):
        raise ApiError(f"Неверный код. Осталось попыток: {5 - entry['attempts']}")
    CODES.pop(phone, None)
    u = q1("SELECT * FROM users WHERE id=?", (entry["user_id"],))
    if not u:
        raise ApiError("Пользователь не найден")
    resp = ok({"redirect": f"/u/{u['token']}"})
    resp.set_cookie(USER_COOKIE, u["token"], max_age=180 * 86400, httponly=True, samesite="lax",
                    path="/", secure=request.url.scheme == "https")
    return resp


async def user_logout(request: Request):
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(USER_COOKIE, path="/")
    return resp


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def _host_only(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class SecurityMiddleware:

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and "/api/" in path:
            origin = headers.get("origin", "")
            if origin and origin != "null":
                from urllib.parse import urlsplit
                if _host_only(urlsplit(origin).netloc) != _host_only(headers.get("host", "")):
                    resp = JSONResponse({"error": "Forbidden (origin)"}, status_code=403)
                    return await resp(scope, receive, send)
            elif origin == "null":
                resp = JSONResponse({"error": "Forbidden (origin)"}, status_code=403)
                return await resp(scope, receive, send)
        https = scope.get("scheme") == "https"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                h = list(message.get("headers", []))
                have = {k.lower() for k, _ in h}
                extra = {b"x-content-type-options": b"nosniff", b"x-frame-options": b"DENY",
                         b"referrer-policy": b"no-referrer", b"content-security-policy": CSP.encode(),
                         b"permissions-policy": b"camera=(), microphone=(), geolocation=()"}
                if https:
                    extra[b"strict-transport-security"] = b"max-age=15552000"
                h += [(k, v) for k, v in extra.items() if k not in have]
                message["headers"] = h
            await send(message)
        return await self.app(scope, receive, send_wrapper)


def _prune(d: dict, window: int):
    if len(d) > 5000:
        now = time.time()
        for k in [k for k, v in d.items() if not v or now - (v[-1] if isinstance(v, list) else v.get("created", 0)) > window]:
            d.pop(k, None)


def build_app() -> Starlette:
    init_db()
    base = "/" + get_setting("web_path").strip("/")

    async def admin_page(request: Request):
        if not request.url.path.endswith("/"):
            return RedirectResponse(base + "/")
        page = (STATIC / "admin.html").read_text(encoding="utf-8")
        page = page.replace("__PANEL_LANG__", "en" if get_setting("lang", "ru") == "en" else "ru")
        return HTMLResponse(page, headers={"cache-control": "no-store"})

    routes = [
        Route(base, admin_page), Route(base + "/", admin_page),
        Route(base + "/api/login", api(login, auth=False), methods=["POST"]),
        Route(base + "/api/logout", api(logout, auth=False), methods=["POST"]),
        Route(base + "/api/me", api(me, auth=False)),
        Route(base + "/api/admin", api(change_admin), methods=["POST"]),
        Route(base + "/api/overview", api(overview)),
        Route(base + "/api/nodes", api(nodes_list)),
        Route(base + "/api/nodes", api(nodes_add), methods=["POST"]),
        Route(base + "/api/nodes/{id:int}", api(node_one), methods=["GET", "PATCH", "DELETE"]),
        Route(base + "/api/nodes/{id:int}/diag", api(node_diag), methods=["POST"]),
        Route(base + "/api/nodes/{id:int}/update", api(node_update), methods=["POST"]),
        Route(base + "/api/nodes/{id:int}/{action}", api(node_action), methods=["POST"]),
        Route(base + "/api/reload", api(reload_all), methods=["POST"]),
        Route(base + "/api/vpns", api(vpns_list), methods=["GET", "POST"]),
        Route(base + "/api/vpns/{id:int}", api(vpn_one), methods=["GET", "PATCH", "DELETE"]),
        Route(base + "/api/users", api(users_list), methods=["GET", "POST"]),
        Route(base + "/api/users/{id:int}", api(user_one), methods=["GET", "PATCH", "DELETE"]),
        Route(base + "/api/users/{id:int}/{action}", api(user_action), methods=["POST"]),
        Route(base + "/api/settings", api(settings), methods=["GET", "PUT"]),
        Route(base + "/api/promos", api(promos_list), methods=["GET", "POST"]),
        Route(base + "/api/update", api(update_state), methods=["GET", "POST"]),
        Route(base + "/api/update/master", api(update_master), methods=["POST"]),
        Route(base + "/api/update/log", api(update_log)),
        Route(base + "/api/updates", api(updates_overview)),
        Route(base + "/api/backup", api(backup_download)),
        Route(base + "/api/backup/restore", api(backup_restore), methods=["POST"]),
        Route(base + "/api/backups", api(backups_list)),
        Route(base + "/api/backup/schedule", api(backup_schedule), methods=["GET", "PUT"]),
        Route(base + "/api/bot/ui", api(bot_ui), methods=["GET", "PUT"]),
        Route(base + "/api/bot/admins", api(admin_notify_get)),
        Route(base + "/api/stats", api(stats_api)),
        Route(base + "/api/stats/payments.csv", api(stats_csv)),
        Route(base + "/api/bot/admins/link", api(admin_link), methods=["POST"]),
        Route(base + "/api/bot/admins/test", api(admin_test), methods=["POST"]),
        Route(base + "/api/notices", api(notices_api), methods=["GET", "POST"]),
        Route(base + "/api/notices/{id:int}", api(notice_one), methods=["PUT", "DELETE"]),
        Route(base + "/api/notices/{id:int}/send", api(notice_send), methods=["POST"]),
        Route(base + "/api/payments", api(pay_settings), methods=["GET", "PUT"]),
        Route(base + "/api/payments/check-crypto", api(pay_check_crypto), methods=["POST"]),
        Route(base + "/api/payments/manual/{id:int}/{action}", api(pay_manual), methods=["POST"]),
        Route(base + "/api/backups/create", api(backup_create), methods=["POST"]),
        Route(base + "/api/backups/{name}", api(backup_file), methods=["GET", "POST", "DELETE"]),
        Route(base + "/api/promos/{id:int}", api(promo_one), methods=["GET", "PATCH", "DELETE"]),
        Route("/sub/{token}", subscription),
        Route("/u/{token}", user_page),
        Route("/u/{token}/lang", user_lang, methods=["POST"]),
        Route("/u/{token}/pay", api(user_pay, auth=False), methods=["POST"]),
        Route("/", home),
        Mount("/assets/flags", StaticFiles(directory=STATIC / "flags"), name="flags"),
        Route("/api/auth/request", api(auth_request, auth=False), methods=["POST"]),
        Route("/api/auth/verify", api(auth_verify, auth=False), methods=["POST"]),
        Route("/logout", user_logout),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        bot_task = asyncio.create_task(bot.start())
        task = asyncio.create_task(sync_loop())
        upd_task = asyncio.create_task(update.loop(notify_update))
        bk_task = asyncio.create_task(backup_loop())
        cr_task = asyncio.create_task(payments.crypto_loop(bot.on_crypto_paid))
        nt_task = asyncio.create_task(notices.loop(bot))
        yield
        task.cancel()
        upd_task.cancel()
        bk_task.cancel()
        cr_task.cancel()
        nt_task.cancel()
        bot_task.cancel()
        await bot.stop()

    from starlette.middleware import Middleware
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityMiddleware)])
    app.state.base = base
    return app
