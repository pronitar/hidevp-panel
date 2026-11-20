import asyncio
import logging
import re
import secrets
import time

from . import core
from .db import ex, ex_count, get_setting, q, q1

log = logging.getLogger("growth")

SOURCE_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")
CHURN = {
    "price": ("💸 Дорого", "💸 Too expensive"),
    "quality": ("🐢 Плохо работало", "🐢 Didn't work well"),
    "noneed": ("🙅 Больше не нужен", "🙅 Don't need it anymore"),
    "other_vpn": ("🔀 Выбрал другой сервис", "🔀 Chose another service"),
    "other": ("💬 Другая причина", "💬 Other reason"),
}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default)) or default))
    except ValueError:
        return default


def T(u, ru: str, en: str) -> str:
    return en if core.user_lang(u) == "en" else ru


def TA(ru: str, en: str) -> str:
    return en if get_setting("lang", "ru") == "en" else ru


def trial_days() -> int:
    return max(0, min(90, _int("trial_days", 0)))


def trial_needs_phone() -> bool:
    return get_setting("trial_phone", "0") == "1"


def grant_trial(u: dict, phone: str = "") -> int:
    days = trial_days()
    if not days or u.get("trial_at") or u.get("forever"):
        return 0
    if q1("SELECT id FROM payments WHERE user_id=? LIMIT 1", (u["id"],)):
        return 0
    if trial_needs_phone():
        if not phone or q1("SELECT id FROM users WHERE trial_phone=? AND id!=?", (phone, u["id"])):
            return 0
    if not ex_count("UPDATE users SET trial_at=?, trial_phone=? WHERE id=? AND trial_at=0", (time.time(), phone, u["id"])):
        return 0
    core.extend_user(u["id"], days)
    return days


def parse_start(arg: str) -> tuple[str, str]:
    if arg.startswith("r_") and arg[2:].isdigit():
        return "ref", arg[2:]
    if arg.startswith("s_") and SOURCE_RE.fullmatch(arg[2:]):
        return "src", arg[2:]
    return "", arg


def attach(u: dict, kind: str, value: str):
    if kind == "ref" and get_setting("ref_on", "0") == "1":
        rid = int(value)
        if rid != u["id"] and q1("SELECT id FROM users WHERE id=?", (rid,)):
            ex("UPDATE users SET ref_by=? WHERE id=? AND ref_by=0", (rid, u["id"]))
            if not u.get("source"):
                ex("UPDATE users SET source=? WHERE id=?", ("ref", u["id"]))
    elif kind == "src":
        ex("UPDATE users SET source=? WHERE id=? AND source=''", (value.lower(), u["id"]))


def ref_link(bot_username: str, u: dict) -> str:
    return f"https://t.me/{bot_username}?start=r_{u['id']}" if bot_username else ""


def ref_stats(uid: int) -> dict:
    r = q1("SELECT COUNT(*) c, COALESCE(SUM(ref_rewarded),0) p FROM users WHERE ref_by=?", (uid,)) or {}
    return {"invited": r.get("c", 0), "paid": r.get("p", 0), "days": r.get("p", 0) * _int("ref_days", 7)}


async def on_payment(bot, uid: int, amount: int):
    if amount <= 0 or get_setting("ref_on", "0") != "1":
        return
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u or not u.get("ref_by") or u.get("ref_rewarded"):
        return
    if not ex_count("UPDATE users SET ref_rewarded=1 WHERE id=? AND ref_rewarded=0", (uid,)):
        return
    inviter = q1("SELECT * FROM users WHERE id=?", (u["ref_by"],))
    bonus, extra = _int("ref_days", 7), _int("ref_new_days", 0)
    if inviter and bonus > 0 and not inviter.get("forever"):
        core.extend_user(inviter["id"], bonus)
        if inviter.get("tg_id"):
            await bot.notify(inviter["tg_id"], T(inviter, f"🎉 Ваш друг оплатил подписку — вам начислено {bonus} дн.",
                                                 f"🎉 Your friend paid for a subscription — you got {bonus} days."))
    if extra > 0 and not u.get("forever"):
        core.extend_user(uid, extra)
        if u.get("tg_id"):
            await bot.notify(u["tg_id"], T(u, f"🎁 Бонус за приглашение: +{extra} дн. к подписке.",
                                           f"🎁 Referral bonus: +{extra} days added to your subscription."))


def _code() -> str:
    for _ in range(30):
        c = "BACK-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not q1("SELECT id FROM promos WHERE code=?", (c,)):
            return c
    raise RuntimeError("code")


def _paid_before(uid: int) -> bool:
    return bool(q1("SELECT id FROM payments WHERE user_id=? AND amount>0 LIMIT 1", (uid,)))


async def winback(bot):
    if get_setting("wb_on", "0") != "1":
        return
    t = time.time()
    days, disc, valid = max(1, _int("wb_days", 3)), max(1, min(100, _int("wb_discount", 20))), max(1, _int("wb_valid", 7))
    rows = q("""SELECT * FROM users WHERE tg_id IS NOT NULL AND enabled=1 AND forever=0 AND expires_at>0
                AND expires_at<? AND expires_at>? AND wb_sent<expires_at""", (t - days * 86400, t - (days + 30) * 86400))
    for u in rows:
        if not _paid_before(u["id"]):
            continue
        ex("UPDATE users SET wb_sent=? WHERE id=?", (t, u["id"]))
        code = _code()
        pid = ex("""INSERT INTO promos(code,discount,months,expires_at,max_uses,used,reusable,new_only,enabled,note,created,user_id,kind)
                    VALUES(?,?,?,?,1,0,0,0,1,?,?,?,'winback')""",
                 (code, disc, "", t + valid * 86400, f"winback #{u['id']}", t, u["id"]))
        ex("UPDATE users SET promo_id=? WHERE id=?", (pid, u["id"]))
        until = core.fmt_date(t + valid * 86400)
        await bot.notify(u["tg_id"], T(u, f"👋 Мы скучаем! Для вас персональная скидка <b>{disc}%</b> на продление — "
                                          f"промокод <code>{code}</code> уже применён и действует до {until}.",
                                          f"👋 We miss you! Here is a personal <b>{disc}%</b> discount on renewal — "
                                          f"promo code <code>{code}</code> is already applied and valid until {until}."),
                         reply_markup={"inline_keyboard": [[{"text": T(u, "💳 Продлить со скидкой", "💳 Renew with discount"),
                                                             "callback_data": "buy"}]]})


async def churn(bot):
    if get_setting("churn_on", "0") != "1":
        return
    t = time.time()
    rows = q("""SELECT * FROM users WHERE tg_id IS NOT NULL AND enabled=1 AND forever=0 AND expires_at>0
                AND expires_at<? AND expires_at>? AND churn_asked<expires_at""", (t - 86400, t - 15 * 86400))
    for u in rows:
        if not _paid_before(u["id"]):
            continue
        ex("UPDATE users SET churn_asked=? WHERE id=?", (t, u["id"]))
        kb = [[{"text": T(u, *v), "callback_data": f"churn:{k}"}] for k, v in CHURN.items()]
        await bot.notify(u["tg_id"], T(u, "Подписка закончилась. Подскажите, почему вы не продлили? Это поможет нам стать лучше.",
                                       "Your subscription has ended. Could you tell us why you didn't renew? It helps us improve."),
                         reply_markup={"inline_keyboard": kb})


def save_churn(u: dict, key: str) -> bool:
    if key not in CHURN:
        return False
    ex("UPDATE users SET churn_reason=?, churn_at=? WHERE id=?", (key, time.time(), u["id"]))
    return True


def report() -> dict:
    sources = q("""SELECT CASE WHEN source='' THEN '-' ELSE source END AS src, COUNT(*) users,
                   SUM(CASE WHEN EXISTS(SELECT 1 FROM payments p WHERE p.user_id=users.id AND p.amount>0) THEN 1 ELSE 0 END) paid
                   FROM users GROUP BY src ORDER BY users DESC LIMIT 50""")
    rev = {}
    for r in q("""SELECT CASE WHEN u.source='' THEN '-' ELSE u.source END AS src, p.currency cur, SUM(p.amount) s
                  FROM payments p JOIN users u ON u.id=p.user_id WHERE p.amount>0 GROUP BY src, cur"""):
        rev.setdefault(r["src"], []).append({"currency": r["cur"], "sum": r["s"]})
    for s in sources:
        s["revenue"] = rev.get(s["src"], [])
    top = q("""SELECT r.id, r.name, r.tg_username, COUNT(u.id) invited, COALESCE(SUM(u.ref_rewarded),0) paid
               FROM users u JOIN users r ON r.id=u.ref_by GROUP BY r.id ORDER BY paid DESC, invited DESC LIMIT 20""")
    churn_rows = q("SELECT churn_reason k, COUNT(*) c FROM users WHERE churn_reason!='' GROUP BY churn_reason")
    wb = q1("""SELECT COUNT(*) sent, COALESCE(SUM(used),0) used FROM promos WHERE kind='winback'""") or {}
    trial = q1("""SELECT COUNT(*) c, SUM(CASE WHEN EXISTS(SELECT 1 FROM payments p WHERE p.user_id=users.id AND p.amount>0)
                  THEN 1 ELSE 0 END) paid FROM users WHERE trial_at>0""") or {}
    return {"sources": sources, "referrers": top, "churn": {r["k"]: r["c"] for r in churn_rows},
            "winback": {"sent": wb.get("sent", 0), "used": wb.get("used", 0)},
            "trial": {"users": trial.get("c", 0) or 0, "paid": trial.get("paid", 0) or 0}}


async def loop(bot):
    await asyncio.sleep(90)
    while True:
        try:
            if bot.bot:
                await churn(bot)
                await winback(bot)
        except Exception:
            log.exception("growth loop")
        await asyncio.sleep(1800)
