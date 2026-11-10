import asyncio
import base64
import urllib.error
import urllib.request
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import uuid as uuidlib
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

VERSION = "1.2.1"

BASE = Path(os.environ.get("NODE_HOME", "/opt/hidevp-panel/node"))
DATA = BASE / "data"
XRAY_BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG = Path(os.environ.get("XRAY_CONFIG", "/usr/local/etc/xray/config.json"))
ACCESS_LOG = Path(os.environ.get("XRAY_ACCESS_LOG", "/var/log/xray/access.log"))
ERROR_LOG = os.environ.get("XRAY_ERROR_LOG", "/var/log/xray/error.log")
XRAY_SERVICE = os.environ.get("XRAY_SERVICE", "xray")
API_PORT = 10085
ONLINE_WINDOW = 180

STATE_FILE = DATA / "state.json"
HASH_FILE = DATA / "config.sha256"
STRUCT_FILE = DATA / "config.struct.sha256"

PENDING: dict[str, dict[str, int]] = {}
SEEN: dict[str, dict[str, float]] = {}
LOG_POS = 0

LOG_RE = re.compile(
    r"from (?:tcp:|udp:)?\[?([0-9a-fA-F.:]+?)\]?:\d+ accepted .*?email: (\S+)"
)


def node_conf() -> dict:
    return json.loads((DATA / "node.json").read_text())


def diag_uuid() -> str:
    path = DATA / "node.json"
    conf = json.loads(path.read_text())
    if not conf.get("diag_uuid"):
        conf["diag_uuid"] = str(uuidlib.uuid4())
        path.write_text(json.dumps(conf, indent=2))
    return conf["diag_uuid"]


CLEAN_ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=CLEAN_ENV)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


XRAY_PROTOCOL = {"vless": "vless", "trojan": "trojan", "ss": "shadowsocks", "hy2": "hysteria"}
SS_METHOD = "2022-blake3-aes-128-gcm"
HY2_MIN_XRAY = (26, 3, 27)


def xray_semver() -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", xray_version())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def hy2_supported() -> bool:
    return xray_semver() >= HY2_MIN_XRAY


def ss_user_key(server_key: str, user_uuid: str) -> str:
    return base64.b64encode(hmac.new(server_key.encode(), user_uuid.encode(), hashlib.sha256).digest()[:16]).decode()


def inbound_settings(ib: dict, ids: list[tuple[str, str]]) -> dict:
    proto = ib.get("protocol") or "vless"
    if proto == "trojan":
        return {"clients": [{"password": uid, "email": e} for e, uid in ids]}
    if proto == "ss":
        return {"method": SS_METHOD, "password": ib["server_key"], "network": "tcp,udp",
                "clients": [{"password": ss_user_key(ib["server_key"], uid), "email": e} for e, uid in ids]}
    if proto == "hy2":
        return {"version": 2, "clients": [{"auth": uid, "email": e} for e, uid in ids]}
    flow = "xtls-rprx-vision" if ib.get("transport", "tcp") == "tcp" else ""
    return {"clients": [{"id": uid, "email": e, **({"flow": flow} if flow else {})} for e, uid in ids],
            "decryption": "none"}


def _transport(stream: dict, ib: dict) -> dict:
    t = ib.get("transport") or "tcp"
    if t == "grpc":
        stream["grpcSettings"] = {"serviceName": ib.get("service_name") or "grpc"}
    elif t == "xhttp":
        stream["xhttpSettings"] = {"path": ib.get("path") or "/", "mode": "auto"}
    return stream


def _hy2_extra(stream: dict, ib: dict) -> dict:
    if ib.get("obfs"):
        stream["finalmask"] = {"udp": [{"type": "salamander", "settings": {"password": ib["obfs"]}}]}
    return stream


def server_stream(ib: dict) -> dict:
    if (ib.get("protocol") or "vless") == "hy2":
        return _hy2_extra({
            "network": "hysteria", "security": "tls",
            "tlsSettings": {"alpn": ["h3"], "certificates": [{"certificate": ib["cert_pem"].strip().splitlines(),
                                                              "key": ib["key_pem"].strip().splitlines()}]},
            "hysteriaSettings": {"version": 2, "masquerade": {"type": "proxy", "url": f"https://{ib['sni']}",
                                                              "rewriteHost": True}},
        }, ib)
    return _transport({
        "network": ib.get("transport") or "tcp", "security": "reality",
        "realitySettings": {"show": False, "dest": ib["dest"], "xver": 0, "serverNames": [ib["sni"]],
                            "privateKey": ib["private_key"], "shortIds": [ib["short_id"]]},
    }, ib)


def client_outbound(ib: dict, uid: str, host: str = "127.0.0.1") -> dict:
    proto = ib.get("protocol") or "vless"
    port = int(ib["port"])
    if proto == "ss":
        return {"protocol": "shadowsocks", "settings": {"servers": [{
            "address": host, "port": port, "method": SS_METHOD,
            "password": f"{ib['server_key']}:{ss_user_key(ib['server_key'], uid)}"}]}}
    if proto == "hy2":
        return {"protocol": "hysteria", "settings": {"version": 2, "address": host, "port": port},
                "streamSettings": _hy2_extra({
                    "network": "hysteria", "security": "tls",
                    "tlsSettings": {"serverName": ib["sni"], "alpn": ["h3"],
                                    "pinnedPeerCertSha256": hashlib.sha256(_pem_der(ib["cert_pem"])).hexdigest()},
                    "hysteriaSettings": {"version": 2, "auth": uid}}, ib)}
    stream = _transport({"network": ib.get("transport") or "tcp", "security": "reality",
                         "realitySettings": {"serverName": ib["sni"], "fingerprint": "chrome",
                                             "publicKey": ib["public_key"], "shortId": ib["short_id"],
                                             "spiderX": "/"}}, ib)
    if proto == "trojan":
        return {"protocol": "trojan", "settings": {"servers": [{"address": host, "port": port, "password": uid}]},
                "streamSettings": stream}
    user = {"id": uid, "encryption": "none"}
    if (ib.get("transport") or "tcp") == "tcp":
        user["flow"] = "xtls-rprx-vision"
    return {"protocol": "vless", "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
            "streamSettings": stream}


def _pem_der(pem: str) -> bytes:
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    return base64.b64decode(body)


def port_protos(ib: dict) -> list[str]:
    proto = ib.get("protocol") or "vless"
    return ["udp"] if proto == "hy2" else (["tcp", "udp"] if proto == "ss" else ["tcp"])


def torrent_rule(emails: list[str]) -> dict:
    return {"type": "field", "ruleTag": "torrent", "user": emails, "protocol": ["bittorrent"], "outboundTag": "block"}


def build_config(state: dict) -> dict:
    users = state.get("users", [])
    inbounds = [{
        "tag": "api", "listen": "127.0.0.1", "port": API_PORT,
        "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"},
    }]
    hy2 = hy2_supported()
    for ib in state.get("inbounds", []):
        proto = ib.get("protocol") or "vless"
        if proto == "hy2" and not hy2:
            continue
        ids = [("diag", diag_uuid())] + [(u["email"], u["uuid"]) for u in users]
        inbounds.append({
            "tag": f"in-{ib['id']}", "listen": "0.0.0.0", "port": int(ib["port"]),
            "protocol": XRAY_PROTOCOL[proto],
            "settings": inbound_settings(ib, ids),
            **({"streamSettings": server_stream(ib)} if proto != "ss" else {}),
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        })

    rules = [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
    torrent_users = [u["email"] for u in users if u.get("block_torrent")]
    if torrent_users:
        rules.append(torrent_rule(torrent_users))
    rules.append({"type": "field", "ip": ["geoip:private"], "outboundTag": "block"})

    return {
        "log": {"loglevel": "warning", "access": str(ACCESS_LOG), "error": ERROR_LOG},
        "api": {"tag": "api", "services": ["StatsService", "HandlerService", "RoutingService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True,
                             "handshake": 4, "connIdle": 300}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": inbounds,
        "outbounds": [{"protocol": "freedom", "tag": "direct"},
                      {"protocol": "blackhole", "tag": "block"}],
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }


def xray_running() -> bool:
    return run(["systemctl", "is-active", "--quiet", XRAY_SERVICE]).returncode == 0


def xray_version() -> str:
    r = run([XRAY_BIN, "version"], timeout=10)
    return (r.stdout.splitlines() or ["не установлен"])[0] if r.returncode == 0 else "не установлен"


def collect_stats():
    r = run([XRAY_BIN, "api", "statsquery", f"--server=127.0.0.1:{API_PORT}", "-reset"], timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return
    for s in data.get("stat") or []:
        parts = s.get("name", "").split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            value = int(s.get("value", 0) or 0)
            if value:
                d = PENDING.setdefault(parts[1], {"up": 0, "down": 0})
                d["up" if parts[3] == "uplink" else "down"] += value


def scan_log():
    global LOG_POS
    if not ACCESS_LOG.exists():
        return
    size = ACCESS_LOG.stat().st_size
    if size < LOG_POS:
        LOG_POS = 0
    with open(ACCESS_LOG, "rb") as f:
        f.seek(LOG_POS)
        chunk = f.read().decode("utf-8", "ignore")
        LOG_POS = f.tell()
    now = time.time()
    for m in LOG_RE.finditer(chunk):
        SEEN.setdefault(m.group(2), {})[m.group(1)] = now
    if size > 20 * 1024 * 1024:
        open(ACCESS_LOG, "w").close()
        LOG_POS = 0
    with contextlib.suppress(OSError):
        if os.path.getsize(ERROR_LOG) > 20 * 1024 * 1024:
            open(ERROR_LOG, "w").close()


def online_ips() -> dict[str, list[str]]:
    now = time.time()
    out = {}
    for email, ips in list(SEEN.items()):
        fresh = {ip: t for ip, t in ips.items() if now - t <= ONLINE_WINDOW}
        if fresh:
            SEEN[email] = fresh
            out[email] = sorted(fresh)
        else:
            SEEN.pop(email, None)
    return out


def ensure_log_perms():
    import pwd
    user = (run(["systemctl", "show", "-p", "User", "--value", XRAY_SERVICE]).stdout or "").strip() or "nobody"
    if user == "root":
        return
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return
    for path in {ACCESS_LOG.parent, Path(ERROR_LOG).parent}:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, pw.pw_uid, pw.pw_gid)
            os.chmod(path, 0o750)
        except OSError:
            pass
    for f in (ACCESS_LOG, Path(ERROR_LOG)):
        try:
            f.touch(exist_ok=True)
            os.chown(f, pw.pw_uid, pw.pw_gid)
            os.chmod(f, 0o640)
        except OSError:
            pass


def ufw_allowed(status: str, port: int, proto: str = "tcp") -> bool:
    return re.search(rf"^{int(port)}(/{proto})?\s+ALLOW", status, re.M) is not None


def ufw_allow(ports) -> list[int]:
    st = run(["ufw", "status"])
    if st.returncode != 0 or "Status: active" not in st.stdout:
        return []
    opened = []
    for item in ports:
        p, proto = (item, "tcp") if isinstance(item, int) or str(item).isdigit() else item
        if not ufw_allowed(st.stdout, int(p), proto):
            if run(["ufw", "allow", f"{int(p)}/{proto}"]).returncode == 0:
                opened.append(int(p))
    return opened


TEMP: dict[int, socket.socket] = {}


def release(ports=None):
    for p in list(TEMP if ports is None else ports):
        sock = TEMP.pop(int(p), None)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def temp_listen(ports: list[int], seconds: float = 10, open_fw: bool = True) -> dict:
    owners = _port_owners()
    res = {"listening": [], "busy": {}, "ufw_opened": []}
    free = []
    for p in ports:
        p = int(p)
        if p in TEMP:
            res["listening"].append(p)
            continue
        if p in owners:
            res["busy"][str(p)] = owners[p]
            continue
        free.append(p)
    if open_fw:
        res["ufw_opened"] = ufw_allow(free)
    deadline = time.time() + min(max(seconds, 3), 30)
    for p in free:
        srv = None
        for _ in range(6):
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("0.0.0.0", p))
                srv.listen(16)
                srv.settimeout(0.5)
                break
            except OSError as e:
                srv.close()
                srv = None
                err = str(e)[:80]
                time.sleep(0.3)
        if srv is None:
            res["busy"][str(p)] = err
            continue

        TEMP[p] = srv

        def serve(sock=srv, port=p):
            while time.time() < deadline and TEMP.get(port) is sock:
                try:
                    c, _ = sock.accept()
                    c.close()
                except socket.timeout:
                    pass
                except OSError:
                    break
            if TEMP.get(port) is sock:
                release([port])
        threading.Thread(target=serve, daemon=True).start()
        res["listening"].append(p)
    return res


def probe(host: str, ports: list[int], timeout: float = 3) -> dict:
    out = {}

    def one(p):
        t0 = time.time()
        try:
            with socket.create_connection((host, int(p)), timeout=timeout):
                out[str(p)] = {"ok": True, "ms": round((time.time() - t0) * 1000)}
        except Exception as e:
            out[str(p)] = {"ok": False, "error": str(e)[:80]}
    ths = [threading.Thread(target=one, args=(p,)) for p in ports[:12]]
    [t.start() for t in ths]
    [t.join(timeout + 1) for t in ths]
    return out


def struct_digest(cfg: dict) -> str:
    c = json.loads(json.dumps(cfg))
    for ib in c["inbounds"]:
        if "clients" in ib.get("settings", {}):
            ib["settings"]["clients"] = []
    c["routing"]["rules"] = [r for r in c["routing"]["rules"] if r.get("ruleTag") != "torrent"]
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()


def _api(*args, files: dict | None = None) -> str:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, data in (files or {}).items():
            fp = Path(td) / name
            fp.write_text(json.dumps(data))
            paths.append(str(fp))
        r = run([XRAY_BIN, "api", args[0], f"--server=127.0.0.1:{API_PORT}", "-t", "5", *args[1:], *paths],
                timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(out[-500:])
    return out


def _count(out: str, word: str) -> int:
    m = re.search(rf"{word} (\d+) user", out)
    return int(m.group(1)) if m else -1


def hot_apply(prev: dict, state: dict, cfg: dict):
    old = {u["email"]: u for u in prev.get("users", [])}
    new = {u["email"]: u for u in state.get("users", [])}
    gone = sorted(e for e, u in old.items() if e not in new or new[e]["uuid"] != u["uuid"])
    came = sorted(e for e, u in new.items() if e not in old or old[e]["uuid"] != u["uuid"])
    for ib in cfg["inbounds"]:
        if ib["tag"] == "api":
            continue
        if gone:
            out = _api("rmu", f"-tag={ib['tag']}", *gone)
            if _count(out, "Removed") != len(gone):
                raise RuntimeError(out[-300:])
        if came:
            part = json.loads(json.dumps(ib))
            part["settings"]["clients"] = [c for c in part["settings"]["clients"] if c.get("email") in came]
            out = _api("adu", files={"in.json": {"inbounds": [part]}})
            if _count(out, "Added") != len(came):
                raise RuntimeError(out[-300:])
    t_old = sorted(e for e, u in old.items() if u.get("block_torrent"))
    t_new = sorted(e for e, u in new.items() if u.get("block_torrent"))
    if t_old != t_new:
        if t_old:
            _api("rmrules", "torrent")
        if t_new:
            _api("adrules", "-append", files={"r.json": {"routing": {"rules": [torrent_rule(t_new)]}}})


def apply_state(state: dict) -> dict:
    force = bool(state.pop("force", False))
    cfg = build_config(state)
    text = json.dumps(cfg, indent=2, ensure_ascii=False)
    digest = hashlib.sha256(text.encode()).hexdigest()
    struct = struct_digest(cfg)
    try:
        prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
    except ValueError:
        prev = None
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    running = xray_running()
    if not force and HASH_FILE.exists() and HASH_FILE.read_text() == digest and running:
        return {"changed": False}
    if (not force and running and prev is not None and STRUCT_FILE.exists()
            and STRUCT_FILE.read_text() == struct):
        try:
            collect_stats()
            hot_apply(prev, state, cfg)
            XRAY_CONFIG.write_text(text)
            HASH_FILE.write_text(digest)
            return {"changed": True, "restarted": False}
        except Exception as e:
            print("hot apply failed, restarting xray:", e, flush=True)
    collect_stats()
    XRAY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = XRAY_CONFIG.with_name("config.new.json")
    tmp.write_text(text)
    test = run([XRAY_BIN, "run", "-test", "-c", str(tmp)])
    if test.returncode != 0:
        raise ValueError("Xray отклонил конфигурацию: " + (test.stdout + test.stderr)[-1500:])
    os.replace(tmp, XRAY_CONFIG)
    HASH_FILE.write_text(digest)
    STRUCT_FILE.write_text(struct)
    ensure_log_perms()
    ufw_allow([(int(i["port"]), pr) for i in state.get("inbounds", []) for pr in port_protos(i)])
    run(["systemctl", "restart", XRAY_SERVICE])
    time.sleep(1.5)
    if not xray_running():
        tail = run(["journalctl", "-u", XRAY_SERVICE, "-n", "15", "--no-pager", "-o", "cat"]).stdout
        busy = [f"{p} ({who})" for p, who in {**_port_owners("udp"), **_port_owners()}.items()
                if p in {int(i["port"]) for i in state.get("inbounds", [])} and "xray" != who]
        msg = "Xray не запустился после применения настроек."
        if busy:
            msg += " Порт занят другой программой: " + ", ".join(busy) + "."
        raise ValueError(msg + " " + tail[-600:])
    return {"changed": True, "restarted": True}


DIAG_LANG = {"v": "ru"}


def L(ru: str, en: str) -> str:
    return en if DIAG_LANG["v"] == "en" else ru


def _check(name, ok, detail="", hint="", warn=False):
    return {"name": name, "status": "ok" if ok else ("warn" if warn else "fail"), "detail": detail, "hint": hint}


def _port_owners(proto: str = "tcp") -> dict[int, str]:
    r = run(["ss", "-lunpH" if proto == "udp" else "-ltnpH"])
    owners = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                port = int(parts[3].rsplit(":", 1)[1])
            except ValueError:
                continue
            m = re.search(r'users:\(\("([^"]+)"', line)
            owners.setdefault(port, m.group(1) if m else "?")
    return owners


def _tls_probe(host: str, timeout=6) -> tuple[bool, str]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                ver = s.version()
        return ver == "TLSv1.3", f"{ver}"
    except Exception as e:
        return False, str(e)[:200]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _selftest(ib: dict) -> tuple[bool, str]:
    proto = ib.get("protocol") or "vless"
    if proto in ("vless", "trojan") and not ib.get("public_key"):
        return False, L("нет публичного ключа (обновите master-панель)", "no public key (update the master panel)")
    port = _free_port()
    cfg = {"log": {"loglevel": "warning"},
           "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
           "outbounds": [client_outbound(ib, diag_uuid())]}
    path = DATA / f"selftest-{secrets.token_hex(3)}.json"
    path.write_text(json.dumps(cfg))
    proc = subprocess.Popen([XRAY_BIN, "run", "-c", str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(1.2)
        if proc.poll() is not None:
            return False, L("клиент Xray не запустился: ", "Xray client failed to start: ") + (proc.stdout.read() or "")[-300:]
        r = run(["curl", "-s", "-m", "12", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--socks5-hostname", f"127.0.0.1:{port}", "https://www.google.com/generate_204"], timeout=20)
        out = (r.stdout or "").split()
        if r.returncode == 0 and out and out[0] in ("204", "200"):
            return True, L(f"ответ за {float(out[1]):.2f} с", f"response in {float(out[1]):.2f} s")
        return False, f"curl {L('код', 'code')} {r.returncode}, HTTP {out[0] if out else '—'}"
    finally:
        proc.kill()
        path.unlink(missing_ok=True)


def diagnose(lang: str = "ru") -> dict:
    DIAG_LANG["v"] = "en" if lang == "en" else "ru"
    checks = []
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"inbounds": [], "users": []}
    inbounds = state.get("inbounds", [])

    ver = xray_version()
    checks.append(_check(L("Xray установлен", "Xray is installed"), ver != "не установлен",
                         ver if ver != "не установлен" else L("не установлен", "not installed"),
                         L("Переустановите work-панель: hidevp → Переустановить", "Reinstall the work panel: hidevp → Reinstall")))
    running = xray_running()
    err_tail = ""
    try:
        err_tail = "\n".join(Path(ERROR_LOG).read_text(errors="ignore").splitlines()[-12:])
    except Exception:
        pass
    if not running:
        jt = run(["journalctl", "-u", XRAY_SERVICE, "-n", "6", "--no-pager", "-o", "cat"]).stdout.strip()
        err_tail = (jt or err_tail)
    checks.append(_check(L("Xray запущен", "Xray is running"), running,
                         "" if running else (err_tail[-500:] or L("служба не активна", "service is not active")),
                         L("Нажмите «Перезагрузить панель» — права на логи исправятся автоматически. "
                           "Если не помогло — пришлите лог: journalctl -u xray -n 50",
                           "Press “Reload panel” — log permissions are fixed automatically. "
                           "If it doesn't help, check the log: journalctl -u xray -n 50")))
    if XRAY_CONFIG.exists():
        t = run([XRAY_BIN, "run", "-test", "-c", str(XRAY_CONFIG)])
        checks.append(_check(L("Конфигурация Xray корректна", "Xray config is valid"), t.returncode == 0,
                             "" if t.returncode == 0 else (t.stdout + t.stderr)[-400:]))

    checks.append(_check(L("VPN-подключения на сервере", "VPN connections on the server"), bool(inbounds),
                         L(f"{len(inbounds)} шт., пользователей: {len(state.get('users', []))}",
                           f"{len(inbounds)}, users: {len(state.get('users', []))}"),
                         L("Создайте VPN во вкладке «VPN» и выберите этот сервер",
                           "Create a VPN in the “VPN” tab and select this server"), warn=True))

    try:
        socket.getaddrinfo("www.google.com", 443)
        dns_ok, dns_d = True, ""
    except Exception as e:
        dns_ok, dns_d = False, str(e)
    checks.append(_check(L("DNS на сервере работает", "DNS works on the server"), dns_ok, dns_d,
                         L("Проверьте /etc/resolv.conf (например, nameserver 1.1.1.1)",
                           "Check /etc/resolv.conf (e.g. nameserver 1.1.1.1)")))
    r = run(["curl", "-4", "-s", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com/generate_204"], timeout=15)
    checks.append(_check(L("Сервер выходит в интернет", "Server has internet access"), r.stdout.strip() in ("204", "200"),
                         f"HTTP {r.stdout.strip() or '—'}",
                         L("У сервера нет доступа в интернет — проверьте сеть у хостера",
                           "The server has no internet access — check the hoster's network")))

    owners = _port_owners()
    owners_udp = _port_owners("udp")
    ufw = run(["ufw", "status"])
    ufw_active = ufw.returncode == 0 and "Status: active" in ufw.stdout
    names = {"vless": "VLESS", "trojan": "Trojan", "ss": "Shadowsocks", "hy2": "Hysteria2"}
    for ib in inbounds:
        p = int(ib["port"])
        proto = ib.get("protocol") or "vless"
        desc = names[proto] + (f" {ib.get('transport') or 'tcp'}" if proto in ("vless", "trojan") else "")
        label = L(f"порт {p}", f"port {p}") + f" ({desc}{', ' + ib['sni'] if ib.get('sni') else ''})"
        if proto == "hy2" and not hy2_supported():
            checks.append(_check(L(f"Hysteria2 на {label}", f"Hysteria2 on {label}"), False,
                                 L(f"нужен Xray {'.'.join(map(str, HY2_MIN_XRAY))} или новее", f"needs Xray {'.'.join(map(str, HY2_MIN_XRAY))} or newer"),
                                 L("Обновите Xray: hidevp → Xray → Переустановить", "Update Xray: hidevp → Xray → Reinstall")))
            continue
        who = (owners_udp if proto == "hy2" else owners).get(p)
        if who is None:
            checks.append(_check(L(f"Xray слушает {label}", f"Xray listens on {label}"), False,
                                 L("порт никто не слушает", "nothing listens on the port"),
                                 L("Xray не поднял этот порт — смотрите лог Xray ниже",
                                   "Xray did not open this port — see the Xray log below")))
        else:
            checks.append(_check(L(f"Xray слушает {label}", f"Xray listens on {label}"), who in ("xray", "?"),
                                 L(f"порт занят процессом «{who}»", f"port is used by process “{who}”") if who not in ("xray", "?") else "",
                                 L("Порт занят другой программой (x-ui, nginx…). Остановите её "
                                   "(для x-ui: x-ui stop; systemctl disable x-ui) или выберите другой порт для этого VPN",
                                   "The port is used by another program (x-ui, nginx…). Stop it "
                                   "(for x-ui: x-ui stop; systemctl disable x-ui) or choose another port for this VPN")))
        if ufw_active:
            for pr in port_protos(ib):
                checks.append(_check(L(f"Фаервол ufw пропускает порт {p}/{pr}", f"ufw firewall allows port {p}/{pr}"),
                                     ufw_allowed(ufw.stdout, p, pr), "",
                                     L(f"Выполните на сервере: ufw allow {p}/{pr}", f"Run on the server: ufw allow {p}/{pr}")))
        if ib.get("sni"):
            ok, d = _tls_probe(ib["sni"])
            checks.append(_check(L(f"Сайт-маскировка {ib['sni']} доступен (TLS 1.3)", f"Camouflage site {ib['sni']} is reachable (TLS 1.3)"), ok, d,
                                 L("Выберите другой сайт для маскировки (SNI) в настройках VPN",
                                   "Choose another camouflage site (SNI) in the VPN settings")))
        ok, d = _selftest(ib) if running else (False, L("Xray не запущен", "Xray is not running"))
        checks.append(_check(L(f"Самопроверка VPN на {label}: выход в интернет через туннель",
                               f"VPN self-test on {label}: internet through the tunnel"), ok, d,
                             L("Сервер не пропускает трафик через VPN — см. лог Xray ниже",
                               "The server does not pass traffic through the VPN — see the Xray log below")))
    return {"checks": checks, "time": time.time(), "error_log": err_tail, "version": VERSION}


def sysinfo() -> dict:
    info = {"version": VERSION, "xray": xray_version(), "xray_running": xray_running(), "hy2": hy2_supported(),
            "cpus": os.cpu_count(),
            "busy_ports": {str(p): who for p, who in _port_owners().items() if who not in ("xray", "?") and p not in TEMP}}
    try:
        info["load"] = [round(x, 2) for x in os.getloadavg()]
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
        info["mem_total"] = mem.get("MemTotal", 0)
        info["mem_used"] = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
        info["uptime"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        import shutil
        du = shutil.disk_usage("/")
        info["disk_total"], info["disk_used"] = du.total, du.used
        def cpu():
            v = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
            return sum(v), v[3] + (v[4] if len(v) > 4 else 0)
        t1, i1 = cpu(); time.sleep(0.3); t2, i2 = cpu()
        info["cpu_pct"] = round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)
    except Exception:
        pass
    return info


def authorized(request: Request) -> bool:
    key = request.headers.get("x-api-key", "")
    return hmac.compare_digest(key, node_conf()["api_key"])


def guard(handler):
    async def wrapper(request: Request):
        if not authorized(request):
            await asyncio.sleep(1)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await handler(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return wrapper


@guard
async def ping(request):
    return JSONResponse({"ok": True, "version": VERSION})


@guard
async def info(request):
    return JSONResponse(await asyncio.to_thread(sysinfo))


@guard
async def put_state(request):
    state = await request.json()
    return JSONResponse(await asyncio.to_thread(apply_state, state))


@guard
async def stats(request):
    def work():
        collect_stats()
        scan_log()
        traffic = dict(PENDING)
        PENDING.clear()
        return {"traffic": traffic, "online": online_ips()}
    return JSONResponse(await asyncio.to_thread(work))


RELAY_HOSTS = {"api.telegram.org": "/bot", "pay.crypt.bot": "/api/", "testnet-pay.crypt.bot": "/api/"}
RELAY_HEADERS = {"crypto-pay-api-token"}


def tg_relay(path: str, body: bytes, timeout: float, host: str = "api.telegram.org", headers: dict | None = None):
    prefix = RELAY_HOSTS.get(host)
    if not prefix or not path.startswith(prefix) or ".." in path:
        raise ValueError("bad path")
    hdrs = {"Content-Type": "application/json", "User-Agent": "hidevp-node/1.0 (+https://github.com/pronitar/hidevp-panel)"}
    hdrs.update({k: str(v) for k, v in (headers or {}).items() if k.lower() in RELAY_HEADERS})
    req = urllib.request.Request(f"https://{host}{path}", data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@guard
async def tg(request):
    d = await request.json()
    timeout = min(float(d.get("timeout") or 20), 90)
    try:
        status, raw = await asyncio.to_thread(tg_relay, str(d.get("path", "")),
                                              base64.b64decode(d.get("body") or ""), timeout,
                                              str(d.get("host") or "api.telegram.org"), d.get("headers") or {})
    except ValueError:
        raise
    except Exception as e:
        return JSONResponse({"error": f"узел не достучался до Telegram: {e}"}, status_code=502)
    return JSONResponse({"status": status, "body": base64.b64encode(raw).decode()})


@guard
async def port_listen(request):
    d = await request.json()
    return JSONResponse(await asyncio.to_thread(temp_listen, [int(p) for p in d.get("ports", [])][:12],
                                                float(d.get("seconds") or 10), bool(d.get("open_fw", True))))


@guard
async def port_release(request):
    d = await request.json()
    await asyncio.to_thread(release, [int(p) for p in d.get("ports", [])] or None)
    return JSONResponse({"ok": True})


@guard
async def port_probe(request):
    d = await request.json()
    return JSONResponse({"results": await asyncio.to_thread(probe, str(d["host"]), [int(p) for p in d.get("ports", [])],
                                                              float(d.get("timeout") or 3))})


@guard
async def diag(request):
    return JSONResponse(await asyncio.to_thread(diagnose, request.query_params.get("lang", "ru")))


@guard
async def restart(request):
    await asyncio.to_thread(ensure_log_perms)
    r = await asyncio.to_thread(run, ["systemctl", "restart", XRAY_SERVICE])
    return JSONResponse({"ok": r.returncode == 0, "error": r.stderr[-500:]})


UPDATE_LOG = "/var/log/hidevp-node-update.log"


def self_update(data_b64: str, sha: str) -> dict:
    import shutil
    import tempfile
    import zipfile
    data = base64.b64decode(data_b64 or "")
    if not data or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("контрольная сумма пакета не совпала")
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="update-", dir=DATA))
    try:
        pkg = tmp / "pkg.zip"
        pkg.write_bytes(data)
        with zipfile.ZipFile(pkg) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise ValueError("небезопасный путь в пакете")
            z.extractall(tmp / "x")
        roots = [p.parent for p in (tmp / "x").rglob("install-node.sh")]
        if not roots:
            raise ValueError("в пакете нет install-node.sh")
        src = roots[0]
        ver = (src / "VERSION").read_text().strip() if (src / "VERSION").exists() else "?"
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    lf = Path("/opt/hidevp-panel/lang")
    lang = "en" if lf.exists() and lf.read_text().strip() == "en" else "ru"
    script = (f"cd {src} && HV_LANG={lang} bash install-node.sh </dev/null > {UPDATE_LOG} 2>&1; "
              f"echo \"__HV_DONE__ $?\" >> {UPDATE_LOG}; rm -rf {tmp}")
    r = run(["systemd-run", "--unit", f"hidevp-node-update-{int(time.time())}", "--collect",
             "--property=Type=oneshot", "bash", "-c", script])
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("не удалось запустить обновление: " + (r.stderr or r.stdout).strip()[-300:])
    return {"ok": True, "started": True, "version": ver}


@guard
async def update_log(request):
    try:
        text = Path(UPDATE_LOG).read_text(errors="ignore")
    except OSError:
        text = ""
    return JSONResponse({"log": "\n".join(text.splitlines()[-60:]), "version": VERSION})


@guard
async def self_update_route(request):
    d = await request.json()
    return JSONResponse(await asyncio.to_thread(self_update, d.get("data", ""), d.get("sha256", "")))


async def log_watcher():
    while True:
        try:
            await asyncio.to_thread(scan_log)
        except Exception:
            pass
        await asyncio.sleep(15)


@contextlib.asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(DATA, 0o700)
        for f in ("node.json", "key.pem"):
            if (DATA / f).exists():
                os.chmod(DATA / f, 0o600)
    await asyncio.to_thread(ensure_log_perms)
    if not xray_running() and XRAY_CONFIG.exists():
        await asyncio.to_thread(run, ["systemctl", "restart", XRAY_SERVICE])
    task = asyncio.create_task(log_watcher())
    yield
    task.cancel()


app = Starlette(routes=[
    Route("/api/ping", ping),
    Route("/api/update-log", update_log),
    Route("/api/info", info),
    Route("/api/state", put_state, methods=["PUT"]),
    Route("/api/stats", stats),
    Route("/api/xray/restart", restart, methods=["POST"]),
    Route("/api/tg", tg, methods=["POST"]),
    Route("/api/diag", diag),
    Route("/api/port/listen", port_listen, methods=["POST"]),
    Route("/api/port/probe", port_probe, methods=["POST"]),
    Route("/api/port/release", port_release, methods=["POST"]),
    Route("/api/self-update", self_update_route, methods=["POST"]),
], lifespan=lifespan)
