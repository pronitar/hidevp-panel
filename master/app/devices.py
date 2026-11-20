import re
import time

from .db import ex, ex_count, get_setting, q, q1

HWID_RE = re.compile(r"[A-Za-z0-9._:\-]{4,128}")


def mode() -> str:
    m = get_setting("device_mode", "ip")
    return m if m in ("ip", "hwid", "both") else "ip"


def hwid_on() -> bool:
    return mode() in ("hwid", "both")


def ttl() -> float:
    try:
        return max(1, int(get_setting("hwid_ttl_days", "30") or 30)) * 86400
    except ValueError:
        return 30 * 86400


def _clean(v: str, n: int = 80) -> str:
    return re.sub(r"[\x00-\x1f<>]", "", (v or "").strip())[:n]


def prune(user_id: int | None = None):
    edge = time.time() - ttl()
    if user_id:
        ex("DELETE FROM devices WHERE user_id=? AND last_seen<?", (user_id, edge))
    else:
        ex("DELETE FROM devices WHERE last_seen<?", (edge,))


def list_for(user_id: int) -> list[dict]:
    prune(user_id)
    return q("SELECT id, os, model, app, ip, first_seen, last_seen FROM devices WHERE user_id=? ORDER BY last_seen DESC",
             (user_id,))


def remove(user_id: int, dev_id: int) -> bool:
    return ex_count("DELETE FROM devices WHERE id=? AND user_id=?", (dev_id, user_id)) > 0


def clear(user_id: int):
    ex("DELETE FROM devices WHERE user_id=?", (user_id,))


def limit_for(u: dict) -> int:
    return int(u.get("device_limit") or 0)


def check(u: dict, headers: dict, ip: str) -> str:
    if not hwid_on():
        return "ok"
    hwid = (headers.get("x-hwid") or "").strip()
    if not hwid or not HWID_RE.fullmatch(hwid):
        return "no_hwid" if get_setting("hwid_strict", "0") == "1" else "ok"
    t = time.time()
    meta = (_clean(headers.get("x-device-os", "") + " " + headers.get("x-ver-os", ""), 60),
            _clean(headers.get("x-device-model", ""), 80), _clean(headers.get("user-agent", ""), 80), ip[:64])
    if q1("SELECT id FROM devices WHERE user_id=? AND hwid=?", (u["id"], hwid)):
        ex("""UPDATE devices SET os=COALESCE(NULLIF(?,''),os), model=COALESCE(NULLIF(?,''),model),
              app=COALESCE(NULLIF(?,''),app), ip=?, last_seen=? WHERE user_id=? AND hwid=?""", (*meta, t, u["id"], hwid))
        return "ok"
    prune(u["id"])
    lim = limit_for(u)
    cap = lim if lim > 0 else 50
    have = (q1("SELECT COUNT(*) c FROM devices WHERE user_id=?", (u["id"],)) or {})["c"]
    if have >= cap:
        return "limit"
    ex("INSERT OR IGNORE INTO devices(user_id,hwid,os,model,app,ip,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)",
       (u["id"], hwid, *meta, t, t))
    return "ok"
