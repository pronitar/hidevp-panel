import asyncio
import json
import logging
import re
import time

from . import promo, tg
from .db import ex, ex_count, get_setting, prices, q, q1, set_setting

log = logging.getLogger("payments")

CURRENCIES = {"RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "UAH": "₴", "BYN": "Br"}
CRYPTO_ASSETS = ("USDT", "TON", "BTC", "ETH", "LTC", "BNB", "TRX", "USDC")
CRYPTO_HOST = {False: "pay.crypt.bot", True: "testnet-pay.crypt.bot"}
INVOICE_TTL = 3600
PLAN_OPTIONS = (1, 2, 3, 6, 9, 12, 24)

DEFAULTS = {
    "currency": "RUB",
    "plans": [1, 3, 6, 12],
    "fiat_prices": {"1": 199, "3": 549, "6": 999, "12": 1899},
    "stars": {"on": True},
    "card": {"on": False, "token": "", "receipt": False},
    "crypto": {"on": False, "token": "", "testnet": False, "assets": ["USDT", "TON"]},
    "manual": {"on": False, "details": "", "unique_code": False},
}
METHOD_NAMES = {
    "stars": ("⭐ Звёзды Telegram", "⭐ Telegram Stars"),
    "card": ("💳 Картой", "💳 By card"),
    "crypto": ("💎 Криптовалютой", "💎 Crypto"),
    "manual": ("🏦 Переводом", "🏦 Bank transfer"),
}


class PayError(Exception):
    pass


def load() -> dict:
    try:
        raw = json.loads(get_setting("pay_methods") or "{}")
    except ValueError:
        raw = {}
    cfg = json.loads(json.dumps(DEFAULTS))
    for m in ("stars", "card", "crypto", "manual"):
        if isinstance(raw.get(m), dict):
            cfg[m].update({k: v for k, v in raw[m].items() if k in cfg[m]})
    old = (raw.get("card") or {}) if (raw.get("card") or {}).get("prices") else (raw.get("crypto") or {})
    cfg["currency"] = raw.get("currency") or old.get("currency") or old.get("fiat") or "RUB"
    if cfg["currency"] not in CURRENCIES:
        cfg["currency"] = "RUB"
    if isinstance(raw.get("fiat_prices"), dict):
        cfg["fiat_prices"] = raw["fiat_prices"]
    elif isinstance(old.get("prices"), dict):
        cfg["fiat_prices"] = old["prices"]
    if isinstance(raw.get("plans"), list) and raw["plans"]:
        cfg["plans"] = sorted({int(m) for m in raw["plans"] if int(m) in PLAN_OPTIONS})
    return cfg


def plans(cfg: dict | None = None) -> list[int]:
    return (cfg or load())["plans"]


def _prices(d, plan_list) -> dict:
    out = {}
    for m in plan_list:
        try:
            v = round(float(str((d or {}).get(str(m), (d or {}).get(m, 0)) or 0).replace(",", ".")), 2)
        except ValueError:
            raise PayError("Цена должна быть числом")
        if v < 0 or v > 1_000_000:
            raise PayError("Цена должна быть числом")
        if v > 0:
            out[str(m)] = v
    return out


def save(data: dict) -> dict:
    cur = load()
    if "plans" in data:
        pl = sorted({int(m) for m in data.get("plans") or [] if str(m).isdigit() and int(m) in PLAN_OPTIONS})
        if not pl:
            raise PayError("Включите хотя бы один тариф")
        cur["plans"] = pl
    if "currency" in data:
        if data["currency"] not in CURRENCIES:
            raise PayError("Неизвестная валюта")
        cur["currency"] = data["currency"]
    if "fiat_prices" in data:
        cur["fiat_prices"] = _prices(data["fiat_prices"], PLAN_OPTIONS)
    if "stars" in data:
        cur["stars"]["on"] = bool(data["stars"].get("on", True))
    if "card" in data:
        c = data["card"]
        token = str(c.get("token", cur["card"]["token"])).strip()
        if token and not re.fullmatch(r"\d+:[\w:\-]+", token):
            raise PayError("Токен провайдера: скопируйте его из @BotFather → Payments целиком")
        cur["card"].update(on=bool(c.get("on")), token=token, receipt=bool(c.get("receipt")))
        if cur["card"]["on"] and not token:
            raise PayError("Укажите токен платёжного провайдера")
    if "crypto" in data:
        c = data["crypto"]
        token = str(c.get("token", cur["crypto"]["token"])).strip()
        if token and not re.fullmatch(r"\d+:[A-Za-z0-9_\-]+", token):
            raise PayError("Токен CryptoBot: скопируйте его из @CryptoBot → Crypto Pay → Мои приложения")
        assets = [a for a in (c.get("assets") or []) if a in CRYPTO_ASSETS] or ["USDT"]
        cur["crypto"].update(on=bool(c.get("on")), token=token, testnet=bool(c.get("testnet")), assets=assets)
        if cur["crypto"]["on"] and not token:
            raise PayError("Укажите токен CryptoBot")
    if "manual" in data:
        c = data["manual"]
        details = str(c.get("details", cur["manual"]["details"])).strip()[:2000]
        cur["manual"].update(on=bool(c.get("on")), details=details,
                             unique_code=bool(c.get("unique_code", cur["manual"].get("unique_code"))))
        if cur["manual"]["on"] and not details:
            raise PayError("Укажите реквизиты для перевода")
    if not any(cur[m]["on"] for m in METHOD_NAMES):
        raise PayError("Включите хотя бы один способ оплаты")
    set_setting("pay_methods", json.dumps(cur, ensure_ascii=False))
    return cur


def configured(method: str, cfg: dict) -> bool:
    c = cfg[method]
    return {"stars": True, "card": bool(c.get("token")), "crypto": bool(c.get("token")),
            "manual": bool(c.get("details"))}[method]


def enabled(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load()
    out = []
    for m in METHOD_NAMES:
        if cfg[m]["on"] and configured(m, cfg) and any(base_price(m, mm, cfg) for mm in cfg["plans"]):
            out.append(m)
    return out


def currency(method: str, cfg: dict | None = None) -> str:
    return "XTR" if method == "stars" else (cfg or load())["currency"]


def base_price(method: str, months: int, cfg: dict | None = None) -> int:
    cfg = cfg or load()
    if months not in cfg["plans"]:
        return 0
    if method == "stars":
        return int(prices().get(months) or 0)
    v = cfg["fiat_prices"].get(str(months)) or 0
    return int(round(float(v) * 100))


def plan_months(cfg: dict | None = None) -> list[int]:
    cfg = cfg or load()
    meths = enabled(cfg)
    return [mm for mm in cfg["plans"] if any(base_price(m, mm, cfg) for m in meths)]


def fmt(amount: int, cur: str) -> str:
    if cur == "XTR":
        return f"{amount} ⭐"
    v = amount / 100
    s = f"{v:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return f"{s} {CURRENCIES.get(cur, cur)}"


def quote(method: str, months: int, p: dict | None, cfg: dict | None = None) -> tuple[int, int]:
    base = base_price(method, months, cfg)
    return base, (promo.price(base, p, months) if base else 0)


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_code() -> str:
    import secrets
    for _ in range(50):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if not q1("SELECT id FROM manual_payments WHERE code=? AND created>?", (code, time.time() - 180 * 86400)):
            return code
    raise PayError("Не удалось создать код")


def create_manual(u: dict, months: int, amount: int, full: int, p: dict | None) -> dict:
    cfg = load()
    code = new_code() if cfg["manual"].get("unique_code") else ""
    oid = ex("INSERT INTO manual_payments(user_id,months,amount,full_amount,currency,promo_id,status,created,code)"
             " VALUES(?,?,?,?,?,?,?,?,?)",
             (u["id"], months, amount, full, cfg["currency"], p["id"] if p else 0, "new", time.time(), code))
    return q1("SELECT * FROM manual_payments WHERE id=?", (oid,))


def order_ref(o: dict) -> str:
    return o.get("code") or str(o["id"])


def manual_text(order: dict, lang: str) -> str:
    import html
    details = html.escape(load()["manual"]["details"])
    amount = fmt(order["amount"], order["currency"])
    ref = order_ref(order)
    if order.get("code"):
        if lang == "en":
            return (f"🏦 <b>Bank transfer</b>\n\nAmount: <b>{amount}</b>\n\n{details}\n\n"
                    f"🔑 Your payment code: <code>{ref}</code>\nBe sure to put this code in the payment comment — "
                    "this is how we find your payment.\n\nAfter paying, tap “✅ I have paid”.")
        return (f"🏦 <b>Оплата переводом</b>\n\nСумма: <b>{amount}</b>\n\n{details}\n\n"
                f"🔑 Ваш код платежа: <code>{ref}</code>\nОбязательно укажите этот код в комментарии к переводу — "
                "по нему мы найдём ваш платёж.\n\nПосле оплаты нажмите «✅ Я оплатил».")
    if lang == "en":
        return (f"🏦 <b>Bank transfer</b>, order №{order['id']}\n\nAmount: <b>{amount}</b>\n\n{details}\n\n"
                f"In the payment comment specify: <code>{ref}</code>\nAfter paying, tap “✅ I have paid” — "
                "the administrator will check and activate the subscription.")
    return (f"🏦 <b>Оплата переводом</b>, заказ №{order['id']}\n\nСумма: <b>{amount}</b>\n\n{details}\n\n"
            f"В комментарии к переводу укажите: <code>{ref}</code>\nПосле оплаты нажмите «✅ Я оплатил» — "
            "администратор проверит поступление и активирует подписку.")


def manual_pending() -> list[dict]:
    return q("SELECT m.*, u.name, u.tg_username, u.tg_id FROM manual_payments m LEFT JOIN users u ON u.id=m.user_id "
             "WHERE m.status IN ('claimed','new') AND m.created>? ORDER BY m.status='claimed' DESC, m.id DESC LIMIT 100",
             (time.time() - 7 * 86400,))


def crypto_call(method: str, params: dict | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    c = cfg["crypto"]
    if not c["token"]:
        raise PayError("Укажите токен CryptoBot")
    try:
        status, raw = tg.post(f"/api/{method}", json.dumps(params or {}).encode(), timeout=20,
                              host=CRYPTO_HOST[bool(c["testnet"])], headers={"Crypto-Pay-API-Token": c["token"]})
    except tg.TransportError as e:
        msg = str(e)
        if "bad path" in msg:
            msg = "на сервере для связи с Telegram старая work-панель — обновите её (hidevp → Обновить панель)"
        raise PayError(f"CryptoBot недоступен: {msg}")
    try:
        res = json.loads(raw)
    except ValueError:
        snippet = re.sub(r"<[^>]+>|\s+", " ", raw.decode("utf-8", "ignore"))[:160].strip()
        log.warning("CryptoBot HTTP %s: %s", status, snippet)
        hint = ""
        if status in (403, 429, 503) or "cloudflare" in snippet.lower():
            hint = " — запрос заблокирован на стороне CryptoBot (Cloudflare). Обновите панель на всех серверах; " \
                   "если не помогло, выберите для Telegram другой маршрут (через зарубежный сервер)"
        raise PayError(f"CryptoBot: неожиданный ответ (HTTP {status}){hint}" + (f". Ответ: {snippet}" if snippet else ""))
    if not res.get("ok"):
        err = res.get("error") or {}
        name = err.get("name") if isinstance(err, dict) else str(err)
        if name == "UNAUTHORIZED":
            raise PayError("CryptoBot: неверный токен. Токен из @CryptoBot работает без флажка «Тестовая сеть», "
                           "токен из @CryptoTestnetBot — только с ним")
        raise PayError(f"CryptoBot: {name or 'HTTP ' + str(status)}")
    return res["result"]


def crypto_check(cfg: dict | None = None) -> dict:
    me = crypto_call("getMe", {}, cfg)
    return {"name": me.get("name", ""), "app_id": me.get("app_id")}


def create_crypto_invoice(u: dict, months: int, amount: int, full: int, p: dict | None, title: str,
                          bot_username: str, chat_id: int | None) -> dict:
    cfg = load()
    c = cfg["crypto"]
    payload = f"cr:{months}:{u['id']}:{amount}:{p['id'] if p else 0}"
    params = {"currency_type": "fiat", "fiat": cfg["currency"], "amount": f"{amount / 100:.2f}",
              "accepted_assets": ",".join(c["assets"]), "description": title[:1024], "payload": payload,
              "expires_in": INVOICE_TTL, "allow_comments": False, "allow_anonymous": True}
    if bot_username:
        params.update(paid_btn_name="openBot", paid_btn_url=f"https://t.me/{bot_username}")
    inv = crypto_call("createInvoice", params, cfg)
    url = inv.get("bot_invoice_url") or inv.get("pay_url") or inv.get("mini_app_invoice_url") or ""
    ex("INSERT INTO crypto_invoices(invoice_id,user_id,months,amount,full_amount,currency,promo_id,status,url,chat_id,created)"
       " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
       (int(inv["invoice_id"]), u["id"], months, amount, full, cfg["currency"], p["id"] if p else 0, "active", url,
        chat_id or 0, time.time()))
    return {"url": url, "invoice_id": inv["invoice_id"]}


async def crypto_loop(on_paid):
    await asyncio.sleep(15)
    while True:
        try:
            await _crypto_tick(on_paid)
        except Exception:
            log.exception("crypto poll")
        await asyncio.sleep(20)


async def _crypto_tick(on_paid):
    now = time.time()
    ex("UPDATE crypto_invoices SET status='expired' WHERE status='active' AND created<?", (now - INVOICE_TTL - 600,))
    rows = q("SELECT * FROM crypto_invoices WHERE status='active' ORDER BY id LIMIT 200")
    if not rows or not load()["crypto"]["token"]:
        return
    res = await asyncio.to_thread(crypto_call, "getInvoices",
                                  {"invoice_ids": ",".join(str(r["invoice_id"]) for r in rows), "count": 200})
    items = {int(i["invoice_id"]): i for i in (res.get("items") if isinstance(res, dict) else res) or []}
    for r in rows:
        inv = items.get(int(r["invoice_id"]))
        if not inv:
            continue
        st = inv.get("status")
        if st == "paid":
            if ex_count("UPDATE crypto_invoices SET status='paid' WHERE id=? AND status='active'", (r["id"],)):
                await on_paid(r, inv)
        elif st == "expired":
            ex("UPDATE crypto_invoices SET status='expired' WHERE id=?", (r["id"],))
