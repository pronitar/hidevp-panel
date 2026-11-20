import calendar
import datetime
import html
import json
import shutil
import time

from . import core
from .db import DATA, ex, get_setting, q1

STATE: dict = {}
GB = core.GB


def TA(ru: str, en: str) -> str:
    return en if get_setting("lang", "ru") == "en" else ru


def _num(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)) or default)
    except ValueError:
        return default


def period_start(period: str, day: int, t: float | None = None) -> float:
    t = t or time.time()
    d = datetime.datetime.fromtimestamp(t)
    today = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return today.timestamp()
    day = max(1, min(31, int(day or 1)))
    y, m = today.year, today.month
    cand = today.replace(day=min(day, calendar.monthrange(y, m)[1]))
    if cand > today:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        cand = today.replace(year=y, month=m, day=min(day, calendar.monthrange(y, m)[1]))
    return cand.timestamp()


def next_period(period: str, day: int, start: float) -> float:
    d = datetime.datetime.fromtimestamp(start)
    if period == "day":
        return (d + datetime.timedelta(days=1)).timestamp()
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m, day=min(max(1, int(day or 1)), calendar.monthrange(y, m)[1])).timestamp()


async def _say(text: str):
    from .bot import manager as bot
    if get_setting("mon_on", "1") == "1":
        await bot.notify_admins(text)


def _delta(node: dict, res: dict) -> int:
    mode = node.get("tl_mode") or "both"
    net = res.get("net")
    if isinstance(net, dict) and "rx" in net:
        try:
            last = json.loads(node.get("net_last") or "{}")
        except ValueError:
            last = {}
        cur = {"rx": int(net.get("rx") or 0), "tx": int(net.get("tx") or 0), "boot": str(net.get("boot") or "")}
        ex("UPDATE nodes SET net_last=? WHERE id=?", (json.dumps(cur), node["id"]))
        if not last:
            return 0
        if last.get("boot") != cur["boot"] or cur["rx"] < last.get("rx", 0) or cur["tx"] < last.get("tx", 0):
            rx, tx = cur["rx"], cur["tx"]
        else:
            rx, tx = cur["rx"] - last.get("rx", 0), cur["tx"] - last.get("tx", 0)
        return tx if mode == "out" else rx + tx
    total = sum(int(d.get("up", 0)) + int(d.get("down", 0)) for d in (res.get("traffic") or {}).values())
    return total if mode == "out" else 2 * total


async def account(node: dict, res: dict):
    t = time.time()
    period = node.get("tl_period") or "month"
    start = period_start(period, node.get("tl_day") or 1, t)
    delta = _delta(node, res)
    limit = float(node.get("tl_gb") or 0)
    if (node.get("tl_start") or 0) != start:
        was_over = node.get("tl_over")
        ex("UPDATE nodes SET tl_start=?, tl_used=?, tl_over=0 WHERE id=?", (start, max(0, delta), node["id"]))
        if was_over and node.get("tl_start"):
            await _say(TA(f"🔄 Сервер «{html.escape(node['name'])}»: начался новый период трафика, сервер снова в подписках.",
                          f"🔄 Server “{html.escape(node['name'])}”: a new traffic period started, the server is back in subscriptions."))
        return
    used = int(node.get("tl_used") or 0) + max(0, delta)
    ex("UPDATE nodes SET tl_used=? WHERE id=?", (used, node["id"]))
    if limit <= 0:
        if node.get("tl_over"):
            ex("UPDATE nodes SET tl_over=0 WHERE id=?", (node["id"],))
        return
    if used >= limit * GB and not node.get("tl_over"):
        ex("UPDATE nodes SET tl_over=1 WHERE id=?", (node["id"],))
        hidden = node["id"] in core.hidden_nodes()
        end = core.fmt_date(next_period(period, node.get("tl_day") or 1, start))
        await _say(TA(f"⛔ Сервер «{html.escape(node['name'])}»: израсходован лимит трафика {limit:g} ГБ. "
                      + ("Сервер убран из подписок" if hidden else "Других серверов нет, поэтому он остаётся в подписках")
                      + f" до {end}.",
                      f"⛔ Server “{html.escape(node['name'])}”: the {limit:g} GB traffic limit is used up. "
                      + ("The server is removed from subscriptions" if hidden else "There are no other servers, so it stays in subscriptions")
                      + f" until {end}."))
    elif used < limit * GB and node.get("tl_over"):
        ex("UPDATE nodes SET tl_over=0 WHERE id=?", (node["id"],))


def _flag(st: dict, key: str, on: bool) -> str:
    was = st.get(key, False)
    st[key] = on
    return "up" if on and not was else ("down" if was and not on else "")


async def health(node: dict, res: dict | None):
    t = time.time()
    st = STATE.setdefault(node["id"], {})
    name = html.escape(node["name"])
    if res is None:
        since = st.setdefault("down_since", t)
        if not st.get("down_alerted") and t - since >= _num("mon_down_min", 3) * 60:
            st["down_alerted"] = True
            err = html.escape((q1("SELECT error FROM nodes WHERE id=?", (node["id"],)) or {}).get("error") or "")[:200]
            await _say(TA(f"🔴 Сервер «{name}» недоступен уже {int((t - since) // 60)} мин." + (f"\n{err}" if err else ""),
                          f"🔴 Server “{name}” has been unreachable for {int((t - since) // 60)} min." + (f"\n{err}" if err else "")))
        return
    since = st.pop("down_since", None)
    if st.pop("down_alerted", False):
        mins = int((t - (since or t)) // 60)
        await _say(TA(f"🟢 Сервер «{name}» снова на связи (был недоступен {mins} мин).",
                      f"🟢 Server “{name}” is back online (was unreachable for {mins} min)."))
    if "xray_running" in res and get_setting("mon_xray", "1") == "1":
        ch = _flag(st, "xray", res["xray_running"] is False)
        if ch == "up":
            await _say(TA(f"⚠️ Сервер «{name}»: Xray остановлен — VPN на нём не работает.",
                          f"⚠️ Server “{name}”: Xray is stopped — VPN on it is down."))
        elif ch == "down":
            await _say(TA(f"✅ Сервер «{name}»: Xray снова работает.", f"✅ Server “{name}”: Xray is running again."))
    disk = res.get("disk_pct")
    if isinstance(disk, (int, float)):
        lim = _num("mon_disk", 90)
        ch = _flag(st, "disk", disk >= lim or (st.get("disk") and disk >= lim - 3))
        if ch == "up":
            await _say(TA(f"💾 Сервер «{name}»: диск заполнен на {disk:.0f}%.", f"💾 Server “{name}”: disk is {disk:.0f}% full."))
    mem = res.get("mem_pct")
    if isinstance(mem, (int, float)):
        lim = _num("mon_mem", 95)
        high = mem >= lim
        st["mem_n"] = st.get("mem_n", 0) + 1 if high else 0
        ch = _flag(st, "mem", st["mem_n"] >= 3 or (st.get("mem") and mem >= lim - 5))
        if ch == "up":
            await _say(TA(f"🧠 Сервер «{name}»: память занята на {mem:.0f}% уже несколько минут.",
                          f"🧠 Server “{name}”: memory has been {mem:.0f}% full for several minutes."))


async def master_health():
    st = STATE.setdefault("master", {})
    try:
        du = shutil.disk_usage(DATA)
    except OSError:
        return
    pct = du.used / du.total * 100 if du.total else 0
    lim = _num("mon_disk", 90)
    if _flag(st, "disk", pct >= lim or (st.get("disk") and pct >= lim - 3)) == "up":
        await _say(TA(f"💾 Master-панель: диск заполнен на {pct:.0f}%.", f"💾 Master panel: disk is {pct:.0f}% full."))


async def observe(results):
    for node, res in results:
        node = q1("SELECT * FROM nodes WHERE id=?", (node["id"],)) or node
        if res is not None:
            await account(node, res)
        await health(node, res)
    await master_health()


def traffic_out(node: dict) -> dict:
    limit = float(node.get("tl_gb") or 0)
    period = node.get("tl_period") or "month"
    start = node.get("tl_start") or period_start(period, node.get("tl_day") or 1)
    return {"limit_gb": limit, "used": int(node.get("tl_used") or 0), "period": period, "day": int(node.get("tl_day") or 1),
            "mode": node.get("tl_mode") or "both", "over": bool(node.get("tl_over")), "start": start,
            "next": next_period(period, node.get("tl_day") or 1, start)}
