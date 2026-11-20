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
    "yookassa": {"on": False, "shop_id": "", "secret": "", "receipt": False, "vat": 1, "email": ""},
    "yoomoney": {"on": False, "wallet": "", "secret": "", "type": "AC"},
    "platega": {"on": False, "merchant": "", "secret": "", "method": ""},
}
METHOD_NAMES = {
    "stars": ("⭐ Звёзды Telegram", "⭐ Telegram Stars"),
    "card": ("💳 Картой", "💳 By card"),
    "yookassa": ("💳 Картой или СБП", "💳 Card or SBP"),
    "platega": ("⚡ СБП или картой", "⚡ SBP or card"),
    "yoomoney": ("💳 Картой через ЮMoney", "💳 Card via YooMoney"),
    "crypto": ("💎 Криптовалютой", "💎 Crypto"),
    "manual": ("🏦 Переводом", "🏦 Bank transfer"),
}
RUB_ONLY = ("yookassa", "yoomoney", "platega")
EXT = RUB_ONLY
YK_API = "https://api.yookassa.ru/v3"
PLATEGA_API = "https://app.platega.io"
YOOMONEY_QUICKPAY = "https://yoomoney.ru/quickpay/confirm"
PLATEGA_METHODS = ("", "2", "11", "12", "13")


class PayError(Exception):
    pass


def load() -> dict:
    try:
        raw = json.loads(get_setting("pay_methods") or "{}")
    except ValueError:
        raw = {}
    cfg = json.loads(json.dumps(DEFAULTS))
    for m in METHOD_NAMES:
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
    if "yookassa" in data:
        c = data["yookassa"]
        shop = str(c.get("shop_id", cur["yookassa"]["shop_id"])).strip()
        secret = str(c.get("secret", cur["yookassa"]["secret"])).strip()
        email = str(c.get("email", cur["yookassa"]["email"])).strip()
        if shop and not re.fullmatch(r"\d{3,12}", shop):
            raise PayError("ЮKassa: shopId — это число из личного кабинета")
        if secret and not re.fullmatch(r"(live|test)_[A-Za-z0-9_\-]{10,200}", secret):
            raise PayError("ЮKassa: секретный ключ начинается с live_ или test_")
        if email and not re.fullmatch(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}", email):
            raise PayError("ЮKassa: неверный e-mail для чеков")
        try:
            vat = int(c.get("vat", cur["yookassa"]["vat"]))
        except (TypeError, ValueError):
            vat = 1
        cur["yookassa"].update(on=bool(c.get("on")), shop_id=shop, secret=secret, receipt=bool(c.get("receipt")),
                               vat=vat if 1 <= vat <= 12 else 1, email=email)
        if cur["yookassa"]["on"] and not (shop and secret):
            raise PayError("ЮKassa: укажите shopId и секретный ключ")
    if "yoomoney" in data:
        c = data["yoomoney"]
        wallet = re.sub(r"\s", "", str(c.get("wallet", cur["yoomoney"]["wallet"])))
        secret = str(c.get("secret", cur["yoomoney"]["secret"])).strip()
        if wallet and not re.fullmatch(r"\d{11,20}", wallet):
            raise PayError("ЮMoney: номер кошелька — 11–20 цифр")
        if secret and len(secret) > 200:
            raise PayError("ЮMoney: слишком длинный секрет")
        kind = c.get("type") if c.get("type") in ("AC", "PC") else "AC"
        cur["yoomoney"].update(on=bool(c.get("on")), wallet=wallet, secret=secret, type=kind)
        if cur["yoomoney"]["on"] and not (wallet and secret):
            raise PayError("ЮMoney: укажите номер кошелька и секрет для уведомлений")
    if "platega" in data:
        c = data["platega"]
        merchant = str(c.get("merchant", cur["platega"]["merchant"])).strip()
        secret = str(c.get("secret", cur["platega"]["secret"])).strip()
        if merchant and not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", merchant):
            raise PayError("Platega: неверный Merchant ID")
        if secret and not re.fullmatch(r"[A-Za-z0-9_\-.]{8,200}", secret):
            raise PayError("Platega: неверный секретный ключ")
        meth = str(c.get("method", cur["platega"]["method"]) or "")
        cur["platega"].update(on=bool(c.get("on")), merchant=merchant, secret=secret,
                              method=meth if meth in PLATEGA_METHODS else "")
        if cur["platega"]["on"] and not (merchant and secret):
            raise PayError("Platega: укажите Merchant ID и секретный ключ")
    for m in RUB_ONLY:
        if cur[m]["on"] and cur["currency"] != "RUB":
            raise PayError("ЮKassa, ЮMoney и Platega принимают только рубли — выберите валюту RUB")
    if not any(cur[m]["on"] for m in METHOD_NAMES):
        raise PayError("Включите хотя бы один способ оплаты")
    set_setting("pay_methods", json.dumps(cur, ensure_ascii=False))
    return cur


def configured(method: str, cfg: dict) -> bool:
    c = cfg[method]
    return {"stars": True, "card": bool(c.get("token")), "crypto": bool(c.get("token")),
            "manual": bool(c.get("details")), "yookassa": bool(c.get("shop_id") and c.get("secret")),
            "yoomoney": bool(c.get("wallet") and c.get("secret")),
            "platega": bool(c.get("merchant") and c.get("secret"))}[method]


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
    if method in RUB_ONLY and cfg["currency"] != "RUB":
        return 0
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


EXT_TTL = 3600


class _NoRedirect(__import__("urllib.request").request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method: str, url: str, headers: dict, body: bytes | None = None, timeout: float = 20) -> tuple[int, bytes, dict]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "Mozilla/5.0", **headers})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b"", dict(e.headers or {})
    except (urllib.error.URLError, OSError) as e:
        raise PayError(f"нет связи: {getattr(e, 'reason', e)}")


def _json(provider: str, status: int, raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        snippet = re.sub(r"<[^>]+>|\s+", " ", raw.decode("utf-8", "ignore"))[:160].strip()
        raise PayError(f"{provider}: неожиданный ответ (HTTP {status})" + (f": {snippet}" if snippet else ""))
    if status >= 400:
        msg = data.get("description") or data.get("message") or data.get("error") or ""
        if status in (401, 403):
            msg = "неверные ключи доступа" + (f" ({msg})" if msg else "")
        raise PayError(f"{provider}: {msg or 'HTTP ' + str(status)}")
    return data if isinstance(data, dict) else {}


def _yk_auth(c: dict) -> dict:
    import base64
    tok = base64.b64encode(f"{c['shop_id']}:{c['secret']}".encode()).decode()
    return {"Authorization": "Basic " + tok, "Content-Type": "application/json"}


def _pl_auth(c: dict) -> dict:
    return {"X-MerchantId": c["merchant"], "X-Secret": c["secret"], "Content-Type": "application/json"}


def _rub(amount: int) -> str:
    return f"{amount / 100:.2f}"


def _yk_create(c: dict, order: dict, u: dict, title: str, return_url: str) -> tuple[str, str]:
    import uuid
    body = {"amount": {"value": _rub(order["amount"]), "currency": "RUB"}, "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": title[:128], "metadata": {"order": str(order["id"])}}
    if c.get("receipt"):
        phone = (u.get("phone") or "").strip()
        customer = {"phone": phone} if phone else ({"email": c["email"]} if c.get("email") else None)
        if not customer:
            raise PayError("ЮKassa: для чека нужен e-mail в настройках или привязанный телефон пользователя")
        body["receipt"] = {"customer": customer, "items": [{
            "description": title[:128], "quantity": "1.00", "amount": {"value": _rub(order["amount"]), "currency": "RUB"},
            "vat_code": int(c.get("vat") or 1), "payment_mode": "full_payment", "payment_subject": "service"}]}
    st, raw, _ = _http("POST", f"{YK_API}/payments", {**_yk_auth(c), "Idempotence-Key": str(uuid.uuid4())},
                       json.dumps(body).encode())
    d = _json("ЮKassa", st, raw)
    url = (d.get("confirmation") or {}).get("confirmation_url") or ""
    if not d.get("id") or not url:
        raise PayError("ЮKassa: пустой ответ при создании платежа")
    return str(d["id"]), url


def _yk_status(c: dict, ext_id: str) -> tuple[str, int]:
    st, raw, _ = _http("GET", f"{YK_API}/payments/{ext_id}", _yk_auth(c))
    d = _json("ЮKassa", st, raw)
    amt = int(round(float((d.get("amount") or {}).get("value") or 0) * 100))
    s = d.get("status")
    return ("paid" if s == "succeeded" and d.get("paid") else "canceled" if s == "canceled" else "pending"), amt


def _pl_create(c: dict, order: dict, title: str, return_url: str) -> tuple[str, str]:
    body = {"paymentDetails": {"amount": round(order["amount"] / 100, 2), "currency": "RUB"},
            "description": title[:200], "payload": f"order:{order['id']}", "return": return_url, "failedUrl": return_url}
    path = "/v2/transaction/process"
    if c.get("method"):
        body["paymentMethod"] = int(c["method"])
        path = "/transaction/process"
    st, raw, _ = _http("POST", PLATEGA_API + path, _pl_auth(c), json.dumps(body).encode())
    d = _json("Platega", st, raw)
    tid = d.get("transactionId") or d.get("id") or ""
    url = d.get("redirect") or d.get("url") or ""
    if not tid or not url:
        raise PayError("Platega: пустой ответ при создании платежа" + (f": {d.get('message')}" if d.get("message") else ""))
    return str(tid), url


def _pl_status(c: dict, ext_id: str) -> tuple[str, int]:
    st, raw, _ = _http("GET", f"{PLATEGA_API}/transaction/{ext_id}", _pl_auth(c))
    d = _json("Platega", st, raw)
    s = str(d.get("status") or "").upper()
    amt = int(round(float(d.get("amount") or 0) * 100))
    if s == "CONFIRMED":
        return "paid", amt
    if s in ("FAILED", "CANCELED", "EXPIRED", "CHARGEBACKED"):
        return "canceled", amt
    return "pending", amt


def _ym_create(c: dict, order: dict, title: str, return_url: str) -> tuple[str, str]:
    import secrets
    from urllib.parse import urlencode
    label = f"hv{order['id']}-{secrets.token_hex(4)}"
    form = {"receiver": c["wallet"], "quickpay-form": "button", "paymentType": c.get("type") or "AC",
            "sum": _rub(order["amount"]), "label": label, "targets": title[:150]}
    if return_url:
        form["successURL"] = return_url
    data = urlencode(form)
    try:
        st, _, hdr = _http("POST", YOOMONEY_QUICKPAY, {"Content-Type": "application/x-www-form-urlencoded"}, data.encode(), 15)
        loc = hdr.get("Location") or hdr.get("location") or ""
        if 300 <= st < 400 and loc:
            from urllib.parse import urljoin
            return label, urljoin(YOOMONEY_QUICKPAY, loc)
    except PayError:
        pass
    return label, YOOMONEY_QUICKPAY + "?" + data


def ym_sign_string(f: dict) -> str:
    from urllib.parse import quote
    return "&".join(f"{k}={quote(str(f[k]), safe='-_.~')}" for k in sorted(f) if k != "sign")


def ym_verify(form: dict, secret: str) -> bool:
    import hashlib
    import hmac
    if not secret:
        return False
    if form.get("sign"):
        calc = hmac.new(secret.encode(), ym_sign_string(form).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, str(form["sign"]).lower())
    if form.get("sha1_hash"):
        s = "&".join([form.get("notification_type", ""), form.get("operation_id", ""), form.get("amount", ""),
                      form.get("currency", ""), form.get("datetime", ""), form.get("sender", ""),
                      form.get("codepro", ""), secret, form.get("label", "")])
        return hmac.compare_digest(hashlib.sha1(s.encode()).hexdigest(), str(form["sha1_hash"]).lower())
    return False


def create_ext(provider: str, u: dict, months: int, amount: int, full: int, p: dict | None, title: str,
               chat_id: int | None, return_url: str) -> dict:
    cfg = load()
    c = cfg[provider]
    if provider not in EXT or not configured(provider, cfg):
        raise PayError("Способ оплаты не настроен")
    oid = ex("INSERT INTO ext_payments(provider,user_id,months,amount,full_amount,currency,promo_id,status,chat_id,created)"
             " VALUES(?,?,?,?,?,?,?,?,?,?)", (provider, u["id"], months, amount, full, "RUB", p["id"] if p else 0,
                                              "new", chat_id or 0, time.time()))
    order = {"id": oid, "amount": amount}
    try:
        if provider == "yookassa":
            ext_id, url = _yk_create(c, order, u, title, return_url)
        elif provider == "platega":
            ext_id, url = _pl_create(c, order, title, return_url)
        else:
            ext_id, url = _ym_create(c, order, title, return_url)
    except Exception:
        ex("UPDATE ext_payments SET status='error' WHERE id=?", (oid,))
        raise
    ex("UPDATE ext_payments SET ext_id=?, url=?, status='pending' WHERE id=?", (ext_id, url, oid))
    return {"id": oid, "url": url, "ext_id": ext_id}


async def ext_mark_paid(row: dict, amount: int, on_paid):
    if amount and amount + 1 < row["amount"]:
        log.warning("%s order %s: paid %s < %s", row["provider"], row["id"], amount, row["amount"])
        ex("UPDATE ext_payments SET status='underpaid' WHERE id=?", (row["id"],))
        return False
    if ex_count("UPDATE ext_payments SET status='paid' WHERE id=? AND status IN ('pending','new')", (row["id"],)):
        await on_paid(row)
        return True
    return False


async def ext_loop(on_paid):
    await asyncio.sleep(20)
    while True:
        try:
            await _ext_tick(on_paid)
        except Exception:
            log.exception("ext poll")
        await asyncio.sleep(20)


async def _ext_tick(on_paid):
    now = time.time()
    ex("UPDATE ext_payments SET status='expired' WHERE status='pending' AND created<?", (now - 2 * EXT_TTL,))
    rows = q("SELECT * FROM ext_payments WHERE status='pending' AND provider IN ('yookassa','platega') ORDER BY id LIMIT 50")
    if not rows:
        return
    cfg = load()
    for r in rows:
        c = cfg.get(r["provider"]) or {}
        if not configured(r["provider"], cfg):
            continue
        try:
            fn = _yk_status if r["provider"] == "yookassa" else _pl_status
            st, amt = await asyncio.to_thread(fn, c, r["ext_id"])
        except PayError as e:
            log.warning("%s status %s: %s", r["provider"], r["ext_id"], e)
            continue
        if st == "paid":
            await ext_mark_paid(r, amt, on_paid)
        elif st == "canceled":
            ex("UPDATE ext_payments SET status='canceled' WHERE id=? AND status='pending'", (r["id"],))


def check_ext(provider: str, cfg: dict) -> str:
    c = cfg[provider]
    if provider == "yookassa":
        st, raw, _ = _http("GET", f"{YK_API}/me", _yk_auth(c))
        d = _json("ЮKassa", st, raw)
        return str(d.get("account_id") or d.get("status") or "OK")
    if provider == "platega":
        st, raw, _ = _http("GET", f"{PLATEGA_API}/transaction/00000000-0000-0000-0000-000000000000", _pl_auth(c))
        if st in (401, 403):
            raise PayError("Platega: неверные ключи доступа")
        return "OK"
    raise PayError("Для ЮMoney проверка не нужна — платёж подтверждается уведомлением")
