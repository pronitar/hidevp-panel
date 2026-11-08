import asyncio
import json
import logging
import time

from . import core, nodes
from .bot import manager as bot
from .db import ex, q
from .events import sync_event

log = logging.getLogger("sync")

INTERVAL = 60
SUSPEND_SECONDS = 600
LAST_RUN = {"at": 0.0, "took": 0.0}


async def poll_stats(node):
    try:
        res = await nodes.call(node, "GET", "/api/stats")
        return node, res
    except nodes.NodeError as e:
        mark(node, "offline", str(e))
        return node, None


def mark(node, status, error=""):
    if status == "online":
        ex("UPDATE nodes SET status='online', error='', last_seen=? WHERE id=?", (time.time(), node["id"]))
    else:
        ex("UPDATE nodes SET status=?, error=? WHERE id=?", (status, error[:500], node["id"]))


def desired_state(node, users) -> dict:
    vpns = q("SELECT * FROM vpns WHERE node_id=? AND enabled=1", (node["id"],))
    return {
        "inbounds": [{k: v[k] for k in ("id", "port", "transport", "sni", "dest", "private_key", "public_key",
                                         "short_id", "path", "service_name")} for v in vpns],
        "users": [{"email": f"u{u['id']}", "uuid": u["uuid"], "block_torrent": bool(u["block_torrent"])}
                  for u in users],
    }


async def push_state(node, users, force=False):
    state = desired_state(node, users)
    if force:
        state["force"] = True
    try:
        await nodes.call(node, "PUT", "/api/state", state, timeout=40)
        mark(node, "online")
        return None
    except nodes.NodeError as e:
        mark(node, "error" if "Xray" in str(e) else "offline", str(e))
        return str(e)


def active_users():
    now = time.time()
    return [u for u in q("SELECT * FROM users") if core.user_status(u, now) == "active"]


async def sync_all():
    t0 = time.time()
    all_nodes = q("SELECT * FROM nodes")

    results = await asyncio.gather(*(poll_stats(n) for n in all_nodes))
    traffic, online = {}, {}
    for _node, res in results:
        if not res:
            continue
        for email, d in (res.get("traffic") or {}).items():
            t = traffic.setdefault(email, [0, 0])
            t[0] += int(d.get("up", 0))
            t[1] += int(d.get("down", 0))
        for email, ips in (res.get("online") or {}).items():
            online.setdefault(email, set()).update(ips)

    for email, (up, down) in traffic.items():
        if email.startswith("u") and email[1:].isdigit():
            ex("UPDATE users SET used_up=used_up+?, used_down=used_down+? WHERE id=?",
               (up, down, int(email[1:])))

    now = time.time()
    users = q("SELECT * FROM users")
    for u in users:
        ips = sorted(online.get(f"u{u['id']}", set()))
        if ips:
            ex("UPDATE users SET online_ips=?, online_at=? WHERE id=?", (json.dumps(ips), now, u["id"]))
        elif u["online_ips"] != "[]":
            ex("UPDATE users SET online_ips='[]' WHERE id=?", (u["id"],))
        if (u["device_limit"] and len(ips) > u["device_limit"]
                and core.user_status(u, now) == "active"):
            ex("UPDATE users SET suspended_until=? WHERE id=?", (now + SUSPEND_SECONDS, u["id"]))
            en = core.user_lang(u) == "en"
            await bot.notify(u["tg_id"],
                             (f"⚠️ Devices connected: {len(ips)}, allowed: {u['device_limit']}. "
                              f"Access suspended for {SUSPEND_SECONDS // 60} minutes.") if en else
                             (f"⚠️ Подключено устройств: {len(ips)}, разрешено: {u['device_limit']}. "
                              f"Доступ приостановлен на {SUSPEND_SECONDS // 60} минут."))

    for u in q("SELECT * FROM users WHERE tg_id IS NOT NULL AND expires_at>0 AND enabled=1 AND forever=0"):
        left = u["expires_at"] - now
        if 0 < left < 3 * core.DAY and not u["notified_3d"]:
            ex("UPDATE users SET notified_3d=1 WHERE id=?", (u["id"],))
            await bot.notify(u["tg_id"], (f"⏳ Your subscription ends {core.fmt_date(u['expires_at'])}. Renew: /buy")
                             if core.user_lang(u) == "en" else
                             (f"⏳ Подписка заканчивается {core.fmt_date(u['expires_at'])}. Продлить: /buy"))
        elif left <= 0 and not u["notified_exp"]:
            ex("UPDATE users SET notified_exp=1 WHERE id=?", (u["id"],))
            await bot.notify(u["tg_id"], "❌ Your subscription has ended. Renew: /buy" if core.user_lang(u) == "en"
                             else "❌ Подписка закончилась. Продлить: /buy")

    active = [u for u in q("SELECT * FROM users") if core.user_status(u, now) == "active"]
    await asyncio.gather(*(push_state(n, active) for n in all_nodes))

    LAST_RUN.update(at=time.time(), took=round(time.time() - t0, 2))


async def sync_loop():
    ev = sync_event()
    while True:
        ev.clear()
        try:
            await sync_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sync failed")
        try:
            await asyncio.wait_for(ev.wait(), timeout=INTERVAL)
            await asyncio.sleep(1.5)
        except asyncio.TimeoutError:
            pass
