import asyncio
import html
import logging
import time

from .db import ex, q, q1

log = logging.getLogger("notices")

KINDS = {"info": "ℹ️", "maintenance": "🛠", "warning": "⚠️"}
AUDIENCES = ("all", "active", "expired")


def fits(n: dict, u: dict) -> bool:
    from . import core
    if n["audience"] == "all":
        return True
    st = core.user_status(u)
    return st == "active" if n["audience"] == "active" else st == "expired"


class NoticeError(Exception):
    pass


def clean(d: dict) -> dict:
    text = str(d.get("text") or "").strip()
    if not text:
        raise NoticeError("Введите текст уведомления")
    kind = d.get("kind") if d.get("kind") in KINDS else "info"
    audience = d.get("audience") if d.get("audience") in AUDIENCES else "all"
    try:
        starts = float(d.get("starts_at") or 0) or time.time()
        ends = float(d.get("ends_at") or 0)
    except (TypeError, ValueError):
        raise NoticeError("Неверная дата")
    if ends and ends <= starts:
        raise NoticeError("Окончание должно быть позже начала")
    return {"text": text[:1500], "text_en": str(d.get("text_en") or "").strip()[:1500], "kind": kind,
            "starts_at": starts, "ends_at": ends, "send_tg": 1 if d.get("send_tg", True) else 0,
            "audience": audience, "enabled": 1 if d.get("enabled", True) else 0}


def create(d: dict) -> dict:
    c = clean(d)
    nid = ex("INSERT INTO notices(text,text_en,kind,starts_at,ends_at,send_tg,audience,enabled,created,sent_at,sent_count)"
             " VALUES(?,?,?,?,?,?,?,?,?,0,0)",
             (c["text"], c["text_en"], c["kind"], c["starts_at"], c["ends_at"], c["send_tg"], c["audience"], c["enabled"],
              time.time()))
    return get(nid)


def update(nid: int, d: dict) -> dict:
    if not get(nid):
        raise NoticeError("Уведомление не найдено")
    c = clean(d)
    ex("UPDATE notices SET text=?,text_en=?,kind=?,starts_at=?,ends_at=?,send_tg=?,audience=?,enabled=? WHERE id=?",
       (c["text"], c["text_en"], c["kind"], c["starts_at"], c["ends_at"], c["send_tg"], c["audience"], c["enabled"], nid))
    return get(nid)


def get(nid: int):
    return q1("SELECT * FROM notices WHERE id=?", (nid,))


def state(n: dict, now: float | None = None) -> str:
    now = now or time.time()
    if not n["enabled"]:
        return "off"
    if n["starts_at"] > now:
        return "scheduled"
    if n["ends_at"] and n["ends_at"] <= now:
        return "ended"
    return "active"


def list_all() -> list[dict]:
    now = time.time()
    return [{**n, "state": state(n, now)} for n in q("SELECT * FROM notices ORDER BY starts_at DESC, id DESC LIMIT 200")]


def active(u: dict | None = None) -> list[dict]:
    now = time.time()
    rows = q("SELECT * FROM notices WHERE enabled=1 AND starts_at<=? AND (ends_at=0 OR ends_at>?) ORDER BY starts_at DESC",
             (now, now))
    if u is not None:
        rows = [n for n in rows if fits(n, u)]
    return rows


def text_for(n: dict, lang: str) -> str:
    return (n["text_en"] if lang == "en" and n.get("text_en") else n["text"])


def public(u: dict, lang: str) -> list[dict]:
    return [{"id": n["id"], "kind": n["kind"], "icon": KINDS[n["kind"]], "text": n["text"], "text_en": n.get("text_en") or "",
             "ends_at": n["ends_at"]} for n in active(u)]


def announce(u: dict, lang: str) -> str:
    items = [f"{KINDS[n['kind']]} {text_for(n, lang)}" for n in active(u)]
    return " · ".join(items)[:200]


def tg_text(n: dict, lang: str) -> str:
    return f"{KINDS[n['kind']]} {html.escape(text_for(n, lang))}"


async def broadcast(bot, n: dict) -> int:
    from . import core
    users = q("SELECT * FROM users WHERE tg_id IS NOT NULL AND tg_id!=0")
    sent = 0
    for u in users:
        if not fits(n, u):
            continue
        lang = core.user_lang(u)
        text = tg_text(n, lang)
        for attempt in range(2):
            try:
                res = await bot.bot.call("sendMessage", chat_id=u["tg_id"], text=text, parse_mode="HTML",
                                         disable_web_page_preview=True)
                if isinstance(res, dict) and res.get("message_id"):
                    ex("INSERT INTO notice_msgs(notice_id,chat_id,message_id,lang) VALUES(?,?,?,?)",
                       (n["id"], u["tg_id"], int(res["message_id"]), lang))
                sent += 1
                break
            except Exception as e:
                msg = str(e)
                if "Too Many Requests" in msg and attempt == 0:
                    await asyncio.sleep(5)
                    continue
                log.info("notice %s to %s: %s", n["id"], u["tg_id"], msg)
                break
        await asyncio.sleep(0.05)
    ex("UPDATE notices SET sent_count=? WHERE id=?", (sent, n["id"]))
    return sent


async def _each_msg(rows, fn):
    done = 0
    for r in rows:
        for attempt in range(2):
            try:
                await fn(r)
                done += 1
                break
            except Exception as e:
                if "Too Many Requests" in str(e) and attempt == 0:
                    await asyncio.sleep(5)
                    continue
                log.info("notice msg %s/%s: %s", r["chat_id"], r["message_id"], e)
                break
        await asyncio.sleep(0.05)
    return done


async def delete_sent(bot, nid: int) -> int:
    rows = q("SELECT * FROM notice_msgs WHERE notice_id=?", (nid,))
    ex("DELETE FROM notice_msgs WHERE notice_id=?", (nid,))
    if not rows or not bot.bot:
        return 0
    n = await _each_msg(rows, lambda r: bot.bot.call("deleteMessage", chat_id=r["chat_id"], message_id=r["message_id"]))
    log.info("notice %s: deleted %s of %s messages", nid, n, len(rows))
    return n


async def edit_sent(bot, n: dict) -> int:
    rows = q("SELECT * FROM notice_msgs WHERE notice_id=?", (n["id"],))
    if not rows or not bot.bot:
        return 0

    async def edit(r):
        try:
            await bot.bot.call("editMessageText", chat_id=r["chat_id"], message_id=r["message_id"],
                               text=tg_text(n, r["lang"]), parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    return await _each_msg(rows, edit)


def sent_messages(nid: int) -> int:
    return (q1("SELECT COUNT(*) c FROM notice_msgs WHERE notice_id=?", (nid,)) or {}).get("c", 0)


async def loop(bot):
    await asyncio.sleep(20)
    while True:
        try:
            if bot.bot:
                now = time.time()
                for n in q("SELECT * FROM notices WHERE enabled=1 AND send_tg=1 AND sent_at=0 AND starts_at<=? "
                           "AND (ends_at=0 OR ends_at>?)", (now, now)):
                    ex("UPDATE notices SET sent_at=? WHERE id=?", (now, n["id"]))
                    cnt = await broadcast(bot, n)
                    log.info("notice %s sent to %s users", n["id"], cnt)
        except Exception:
            log.exception("notices")
        await asyncio.sleep(60)
