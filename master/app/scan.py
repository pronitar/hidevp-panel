import asyncio
import html
import json
import logging
import time

from .db import ex, get_setting, q, q1, set_setting

log = logging.getLogger("scan")

WINDOW = 600
THRESHOLD = 8
BAN_DAYS = 7
HITS: dict[str, dict] = {}
SAFE_IPS: dict[str, float] = {}


def on() -> bool:
    return get_setting("scan_detect", "1") == "1"


def trust(ip: str):
    SAFE_IPS[ip] = time.time()


def banned(ip: str) -> bool:
    if get_setting("scan_ban", "0") != "1" or ip in SAFE_IPS:
        return False
    r = q1("SELECT banned_until FROM scanners WHERE ip=?", (ip,))
    return bool(r and r["banned_until"] > time.time())


def _prune(t: float):
    if len(HITS) > 20000:
        for k in [k for k, v in HITS.items() if t - v["at"] > WINDOW]:
            HITS.pop(k, None)


def miss(ip: str, path: str, ua: str = ""):
    if not on() or not ip or ip in SAFE_IPS or ip in ("127.0.0.1", "::1"):
        return
    t = time.time()
    _prune(t)
    h = HITS.get(ip)
    if not h or t - h["at"] > WINDOW:
        h = HITS[ip] = {"at": t, "paths": set()}
    h["paths"].add(path[:120])
    if len(h["paths"]) < THRESHOLD:
        return
    paths = sorted(h["paths"])[:20]
    HITS.pop(ip, None)
    ban = t + BAN_DAYS * 86400 if get_setting("scan_ban", "0") == "1" else 0
    row = q1("SELECT * FROM scanners WHERE ip=?", (ip,))
    if row:
        old = json.loads(row["paths"] or "[]")
        merged = (old + [p for p in paths if p not in old])[-30:]
        ex("UPDATE scanners SET last=?, hits=hits+?, paths=?, ua=?, banned_until=MAX(banned_until, ?) WHERE ip=?",
           (t, len(paths), json.dumps(merged), ua[:200], ban, ip))
    else:
        ex("INSERT INTO scanners(ip,first,last,hits,paths,ua,banned_until) VALUES(?,?,?,?,?,?,?)",
           (ip, t, t, len(paths), json.dumps(paths), ua[:200], ban))
    log.info("scanner %s: %d paths", ip, len(paths))


def listing(days: int = 30) -> list[dict]:
    t = time.time()
    out = []
    for r in q("SELECT * FROM scanners WHERE last>? ORDER BY last DESC LIMIT 200", (t - days * 86400,)):
        r["paths"] = json.loads(r["paths"] or "[]")
        r["banned"] = r["banned_until"] > t
        out.append(r)
    return out


def unban(ip: str):
    ex("UPDATE scanners SET banned_until=0 WHERE ip=?", (ip,))


def forget(ip: str = ""):
    if ip:
        ex("DELETE FROM scanners WHERE ip=?", (ip,))
    else:
        ex("DELETE FROM scanners")


def TA(ru: str, en: str) -> str:
    return en if get_setting("lang", "ru") == "en" else ru


async def digest(bot):
    t = time.time()
    last = float(get_setting("scan_digest_at", "0") or 0)
    if get_setting("scan_digest", "0") != "1" or t - last < 86400:
        return
    set_setting("scan_digest_at", str(t))
    rows = q("SELECT * FROM scanners WHERE first>? ORDER BY hits DESC", (max(last, t - 86400),))
    if not rows:
        return
    lines = [f"<code>{html.escape(r['ip'])}</code> — {r['hits']}" for r in rows[:15]]
    more = len(rows) - 15
    await bot.notify_admins(TA("🕵️ Сканеры за сутки: ", "🕵️ Scanners in the last day: ") + str(len(rows)) + "\n"
                            + "\n".join(lines) + (TA(f"\n…и ещё {more}", f"\n…and {more} more") if more > 0 else ""))


async def loop(bot):
    await asyncio.sleep(120)
    while True:
        try:
            ex("DELETE FROM scanners WHERE last<? AND banned_until<?", (time.time() - 90 * 86400, time.time()))
            await digest(bot)
        except Exception:
            log.exception("scan loop")
        await asyncio.sleep(1800)
