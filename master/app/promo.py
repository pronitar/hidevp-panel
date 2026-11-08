import time

from .db import ex, q1

ERR = {
    "not_found": ("Промокод не найден.", "Promo code not found."),
    "disabled": ("Промокод отключён.", "Promo code is disabled."),
    "expired": ("Срок действия промокода истёк.", "The promo code has expired."),
    "exhausted": ("Промокод уже использован максимальное число раз.", "The promo code has reached its usage limit."),
    "used": ("Вы уже использовали этот промокод.", "You have already used this promo code."),
    "new_only": ("Промокод только для новых пользователей.", "The promo code is for new users only."),
    "plan": ("Промокод не действует на этот тариф.", "The promo code does not apply to this plan."),
}


def plan_list(p: dict) -> list[int]:
    return [int(x) for x in str(p.get("months") or "").split(",") if x.strip().isdigit()]


def applies_to(p: dict, months: int) -> bool:
    pl = plan_list(p)
    return not pl or months in pl


def check(p: dict | None, user: dict, months: int | None = None) -> str | None:
    if not p:
        return "not_found"
    if not p["enabled"]:
        return "disabled"
    if p["expires_at"] and p["expires_at"] < time.time():
        return "expired"
    if p["max_uses"] and p["used"] >= p["max_uses"]:
        return "exhausted"
    if not p["reusable"] and q1("SELECT id FROM promo_uses WHERE promo_id=? AND user_id=?", (p["id"], user["id"])):
        return "used"
    if p["new_only"] and q1("SELECT id FROM payments WHERE user_id=? LIMIT 1", (user["id"],)):
        return "new_only"
    if months is not None and not applies_to(p, months):
        return "plan"
    return None


def find(code: str) -> dict | None:
    return q1("SELECT * FROM promos WHERE code=? COLLATE NOCASE", (code.strip(),))


def get(pid: int) -> dict | None:
    return q1("SELECT * FROM promos WHERE id=?", (pid,)) if pid else None


def price(base: int, p: dict | None, months: int) -> int:
    if not p or not applies_to(p, months):
        return base
    return max(0, round(base * (100 - int(p["discount"])) / 100))


def record_use(p: dict, user_id: int, payment_id: int, months: int, amount: int, full: int, currency: str = "XTR"):
    ex("INSERT INTO promo_uses(promo_id,user_id,payment_id,months,amount,full_amount,created,currency) VALUES(?,?,?,?,?,?,?,?)",
       (p["id"], user_id, payment_id, months, amount, full, time.time(), currency))
    ex("UPDATE promos SET used=used+1 WHERE id=?", (p["id"],))
    ex("UPDATE users SET promo_id=0 WHERE id=?", (user_id,))


def stats(pid: int) -> dict:
    r = q1("SELECT COUNT(*) c, COUNT(DISTINCT user_id) u, COALESCE(SUM(full_amount-amount),0) saved, "
           "COALESCE(SUM(amount),0) paid FROM promo_uses WHERE promo_id=?", (pid,))
    return r or {"c": 0, "u": 0, "saved": 0, "paid": 0}
