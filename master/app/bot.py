import asyncio
import contextvars
import hmac
import html
import json
import logging
import re
import time

from . import botui, core, payments, promo, tg
from .db import ex, ex_count, get_setting, q1, set_setting
from .events import request_sync
from .tg import TelegramError

log = logging.getLogger("bot")

MONTHS = {
    "ru": {1: "1 месяц", 2: "2 месяца", 3: "3 месяца", 6: "6 месяцев", 9: "9 месяцев", 12: "12 месяцев", 24: "24 месяца"},
    "en": {1: "1 month", 2: "2 months", 3: "3 months", 6: "6 months", 9: "9 months", 12: "12 months", 24: "24 months"},
}
RU_LANGS = ("ru", "uk", "be", "kk", "uz", "ky", "tg", "hy", "az")


def L(u) -> str:
    return core.user_lang(u)


def T(u, ru: str, en: str) -> str:
    return en if L(u) == "en" else ru


def TA(ru: str, en: str) -> str:
    return en if get_setting("lang", "ru") == "en" else ru


def mword(u, m: int) -> str:
    return MONTHS[L(u)].get(m, f"{m} " + T(u, "мес.", "mo."))


class Bot:
    def __init__(self, token: str):
        self.token = token

    async def call(self, method: str, _http_timeout: float = 20, **params):
        return await asyncio.to_thread(tg.call, self.token, method, params, _http_timeout)


UI_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ui_ctx", default=None)


class BotManager:
    def __init__(self):
        self.promo_fails: dict[int, list[float]] = {}
        self.bot: Bot | None = None
        self.task: asyncio.Task | None = None
        self.username = ""
        self.error = ""
        self.awaiting_promo: set[int] = set()

    async def start(self):
        await self.stop()
        token = get_setting("bot_token").strip()
        if not token:
            self.error = "Токен бота не задан"
            return
        self.bot = Bot(token)
        try:
            me = await self.bot.call("getMe")
            self.username = me.get("username", "")
            await self.bot.call("setMyCommands", commands=[
                {"command": "start", "description": "Main menu"},
                {"command": "buy", "description": "Buy / renew subscription"},
                {"command": "me", "description": "My subscription"},
                {"command": "promo", "description": "Enter a promo code"},
                {"command": "help", "description": "How to connect"},
                {"command": "phone", "description": "Link phone for website login"},
                {"command": "lang", "description": "Language / Язык"},
            ])
            await self.bot.call("setMyCommands", language_code="ru", commands=[
                {"command": "start", "description": "Главное меню"},
                {"command": "buy", "description": "Купить / продлить подписку"},
                {"command": "me", "description": "Моя подписка"},
                {"command": "promo", "description": "Ввести промокод"},
                {"command": "help", "description": "Как подключиться"},
                {"command": "phone", "description": "Привязать номер для входа на сайт"},
                {"command": "lang", "description": "Язык / Language"},
            ])
            self.error = ""
        except TelegramError as e:
            self.error = f"Telegram отклонил токен: {e}"
            self.bot = None
            return
        except Exception as e:
            self.error = f"Нет связи с Telegram ({tg.describe()}): {e}"
            self.bot = None
            return
        self.task = asyncio.create_task(self._poll())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.bot = None
        self.username = ""

    def status(self) -> dict:
        return {"running": bool(self.task and not self.task.done()),
                "username": self.username, "error": self.error, "route": tg.describe()}

    async def notify(self, chat_id, text: str, **kw) -> str:
        if not self.bot or not chat_id:
            return "бот не запущен" if not self.bot else "нет chat_id"
        try:
            await self.bot.call("sendMessage", chat_id=chat_id, text=text,
                                parse_mode="HTML", disable_web_page_preview=True, **kw)
            return ""
        except Exception as e:
            log.warning("notify %s: %s", chat_id, e)
            return str(e)

    @staticmethod
    def admin_ids() -> set[int]:
        return {int(x.strip()) for x in get_setting("admin_tg_ids").replace(";", ",").split(",")
                if x.strip().lstrip("-").isdigit()}

    async def notify_admins(self, text: str, kb: dict | None = None) -> dict:
        res = {}
        for aid in self.admin_ids():
            res[str(aid)] = await self.notify(aid, text, **({"reply_markup": kb} if kb else {}))
        if res:
            set_setting("admin_notify", json.dumps({"ts": time.time(), "results": res}, ensure_ascii=False))
        else:
            log.warning("admin notification not sent: no admin Telegram IDs in settings")
        return res

    async def _poll(self):
        offset = 0
        while True:
            try:
                updates = await self.bot.call("getUpdates", _http_timeout=55, timeout=40, offset=offset,
                                              allowed_updates=["message", "callback_query", "pre_checkout_query"])
                for upd in updates:
                    offset = upd["update_id"] + 1
                    try:
                        await self.handle(upd)
                    except Exception:
                        log.exception("handle update")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"Ошибка связи с Telegram ({tg.describe()}): {e}"
                await asyncio.sleep(5)
            else:
                self.error = ""

    @staticmethod
    def clean_mode() -> bool:
        return bool(botui.load().get("clean_chat"))

    @staticmethod
    def _ui_ids(chat) -> list[int]:
        r = q1("SELECT ui_msgs FROM users WHERE tg_id=?", (chat,))
        try:
            return [int(x) for x in json.loads((r or {}).get("ui_msgs") or "[]")]
        except (ValueError, TypeError):
            return []

    @staticmethod
    def _ui_save(chat, ids: list[int]):
        ex("UPDATE users SET ui_msgs=? WHERE tg_id=?", (json.dumps(ids[-20:]), chat))

    def track(self, chat, res):
        if self.clean_mode() and isinstance(res, dict) and res.get("message_id"):
            self._ui_save(chat, self._ui_ids(chat) + [int(res["message_id"])])

    async def _delete(self, chat, mid):
        try:
            await self.bot.call("deleteMessage", chat_id=chat, message_id=mid)
        except Exception:
            pass

    async def cleanup(self, chat, keep: int | None = None):
        ids = self._ui_ids(chat)
        for mid in ids:
            if mid != keep:
                await self._delete(chat, mid)
        self._ui_save(chat, [keep] if keep else [])

    async def handle(self, upd: dict):
        token = UI_CTX.set(None)
        try:
            await self._handle(upd)
        finally:
            UI_CTX.reset(token)

    async def _handle(self, upd: dict):
        if "pre_checkout_query" in upd:
            return await self.on_pre_checkout(upd["pre_checkout_query"])
        clean = self.clean_mode()
        if "callback_query" in upd:
            cq = upd["callback_query"]
            await self.bot.call("answerCallbackQuery", callback_query_id=cq["id"])
            m = cq.get("message") or {}
            data = cq.get("data") or ""
            if clean and m.get("chat", {}).get("type") == "private" and not data.startswith(("mpa:", "noop")):
                UI_CTX.set({"chat": m["chat"]["id"], "edit": m.get("message_id")})
                await self.cleanup(m["chat"]["id"], keep=m.get("message_id"))
            return await self.on_callback(cq)
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return
        if clean:
            await self._delete(msg["chat"]["id"], msg.get("message_id"))
            await self.cleanup(msg["chat"]["id"])
        if "successful_payment" in msg:
            return await self.on_paid(msg)
        text = (msg.get("text") or "").strip()
        user = self.ensure_user(msg["from"])
        chat = msg["chat"]["id"]
        if "contact" in msg:
            return await self.on_contact(msg, user)
        start_arg = text[7:].strip() if text.startswith("/start ") else ""
        if start_arg.startswith("admin_"):
            return await self.link_admin(chat, user, start_arg[6:])
        if text.startswith("/admin "):
            return await self.link_admin(chat, user, text[7:].strip())
        if user.get("_new"):
            await self.send_welcome(chat, user)
            if start_arg and start_arg != "site":
                await self.apply_promo(chat, user, start_arg)
            return
        if text.startswith("/"):
            self.awaiting_promo.discard(user["id"])
        if start_arg and start_arg != "site":
            await self.apply_promo(chat, user, start_arg)
        elif text.startswith("/promo"):
            code = text[6:].strip()
            if code:
                await self.apply_promo(chat, user, code)
            else:
                await self.ask_promo(chat, user)
        elif text.startswith("/lang"):
            await self.send_lang(chat, user)
        elif text.startswith("/phone"):
            await self.ask_phone(chat, user)
        elif text.startswith("/buy"):
            await self.send_tariffs(chat, user)
        elif text.startswith("/me"):
            await self.send_status(chat, user)
        elif text.startswith("/help"):
            await self.send_help(chat, user)
        elif user["id"] in self.awaiting_promo and text and not text.startswith("/"):
            self.awaiting_promo.discard(user["id"])
            await self.apply_promo(chat, user, text)
        else:
            await self.send_menu(chat, user)

    def ensure_user(self, frm: dict) -> dict:
        u = q1("SELECT * FROM users WHERE tg_id=?", (frm["id"],))
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        if u is None:
            lc = (frm.get("language_code") or "").lower()
            lang = "ru" if lc.split("-")[0] in RU_LANGS else ("en" if lc else get_setting("lang", "ru"))
            u = core.create_user(tg_id=frm["id"], tg_username=frm.get("username", ""), name=name)
            ex("UPDATE users SET lang=? WHERE id=?", (lang, u["id"]))
            u["lang"] = lang
            u["_new"] = True
            asyncio.create_task(self.notify_admins(
                TA("🆕 Новый пользователь: ", "🆕 New user: ") + f"<b>{html.escape(name)}</b> "
                f"(@{html.escape(frm.get('username') or '—')}), id {u['id']}"))
        elif frm.get("username", "") != u["tg_username"]:
            ex("UPDATE users SET tg_username=? WHERE id=?", (frm.get("username", ""), u["id"]))
        return u

    def reload(self, u) -> dict:
        return q1("SELECT * FROM users WHERE id=?", (u["id"],))

    def menu_kb(self, u: dict | None = None):
        cfg = botui.load()
        lang = L(u)

        def visible(b):
            text = botui.label(b, lang)
            bid = b["id"]
            if bid == "page":
                return {"text": text, "url": core.user_page_url(u)} if u and core.public_url() else None
            if bid == "phone":
                return {"text": text, "callback_data": "phone"} if u and not u.get("phone") else None
            if bid == "support":
                url = self.support_url()
                return {"text": text, "url": url} if url else None
            if bid == "lang":
                return {"text": text, "callback_data": "lang:" + ("ru" if lang == "en" else "en")}
            if bid in botui.BUILTIN:
                return {"text": text, "callback_data": bid}
            if b.get("type") == "url":
                return {"text": text, "url": b["url"]}
            return {"text": text, "callback_data": bid}
        return botui.keyboard(cfg, lang, visible)

    @staticmethod
    def support_url() -> str:
        sup = (get_setting("support") or "").strip()
        if sup.startswith("@"):
            return "https://t.me/" + sup[1:]
        return sup if re.match(r"^(https?|tg)://", sup) else ""

    def values(self, u) -> dict:
        lang = L(u)
        return {"title": html.escape(get_setting("panel_title", "VPN")),
                "name": html.escape((u or {}).get("name") or ""),
                "status": core.status_text(u, lang) if u else "",
                "expires": core.expires_text(u, lang) if u and (u.get("expires_at") or u.get("forever")) else "",
                "page": html.escape(core.user_page_url(u)) if u and core.public_url() else "",
                "support": html.escape((get_setting("support") or "").strip())}

    def text(self, key: str, u) -> str:
        cfg = botui.load()
        return botui.render(cfg["texts"][f"{key}_{L(u)}"], self.values(u))

    async def send(self, chat, text, kb=None):
        ctx = UI_CTX.get()
        if ctx and ctx.get("chat") == chat and ctx.get("edit"):
            mid = ctx.pop("edit")
            try:
                await self.bot.call("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                                    disable_web_page_preview=True, reply_markup=kb or {"inline_keyboard": []})
                return
            except TelegramError as e:
                if "not modified" in str(e).lower():
                    return
                await self._delete(chat, mid)
                self._ui_save(chat, [x for x in self._ui_ids(chat) if x != mid])
        self.track(chat, await self._send(chat, text, kb))

    async def _send(self, chat, text, kb=None):
        try:
            return await self.bot.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                                       disable_web_page_preview=True, reply_markup=kb or {"inline_keyboard": []})
        except TelegramError as e:
            s = str(e).lower()
            if "parse entities" in s or "can't parse" in s:
                return await self.bot.call("sendMessage", chat_id=chat, text=botui.strip_html(text),
                                           disable_web_page_preview=True, reply_markup=kb or {"inline_keyboard": []})
            if kb and ("url" in s or "button" in s):
                rows = [[b for b in row if "url" not in b] for row in (kb or {}).get("inline_keyboard", [])]
                return await self.bot.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                                           disable_web_page_preview=True,
                                           reply_markup={"inline_keyboard": [r for r in rows if r]})
            else:
                raise

    async def send_menu(self, chat, u):
        u = self.reload(u)
        text = self.text("menu", u)
        from . import notices
        act = [notices.tg_text(n, L(u)) for n in notices.active(u)]
        if act:
            text = "\n".join(act) + "\n\n" + text
        p = promo.get(u.get("promo_id") or 0)
        if p:
            text += T(u, f"\n🎟 Промокод <b>{html.escape(p['code'])}</b>: скидка {p['discount']}%",
                         f"\n🎟 Promo code <b>{html.escape(p['code'])}</b>: {p['discount']}% off")
        await self.send(chat, text, self.menu_kb(u))

    async def send_welcome(self, chat, u):
        await self.send(chat, self.text("welcome", u), self.menu_kb(u))
        if botui.load().get("ask_phone", True):
            await self.ask_phone(chat, u)

    async def send_lang(self, chat, u):
        await self.send(chat, "🌐 Language / Язык", {"inline_keyboard": [[
            {"text": "English", "callback_data": "lang:en"}, {"text": "Русский", "callback_data": "lang:ru"}]]})

    async def ask_phone(self, chat, u):
        site = core.public_url()
        text = T(u, "☎️ Поделитесь номером телефона кнопкой ниже — по нему можно входить на сайт"
                    + (f" {html.escape(site)}" if site else "") + ". Код для входа будет приходить сюда, в этот чат.",
                    "☎️ Share your phone number with the button below — you can use it to sign in on the website"
                    + (f" {html.escape(site)}" if site else "") + ". Login codes will be sent to this chat.")
        self.track(chat, await self.bot.call(
            "sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True,
            reply_markup={"keyboard": [[{"text": T(u, "📱 Поделиться номером", "📱 Share phone number"),
                                         "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}))

    async def on_contact(self, msg, u):
        c = msg["contact"]
        chat = msg["chat"]["id"]
        if c.get("user_id") != msg["from"]["id"]:
            return await self.send(chat, T(u, "Отправьте, пожалуйста, <b>свой</b> номер — кнопкой «📱 Поделиться номером».",
                                           "Please send <b>your own</b> number with the “📱 Share phone number” button."))
        phone = core.normalize_phone(c.get("phone_number", ""))
        if not phone:
            return await self.send(chat, T(u, "Не удалось распознать номер.", "Could not recognize the number."))
        core.set_user_phone(u["id"], phone)
        self.track(chat, await self.bot.call("sendMessage", chat_id=chat, parse_mode="HTML", reply_markup={"remove_keyboard": True},
                            text=T(u, f"✅ Номер {core.fmt_phone(phone)} привязан. Теперь можно входить на сайт по нему.",
                                   f"✅ Number {core.fmt_phone(phone)} linked. You can now sign in on the website with it.")))
        await self.send_menu(chat, u)

    async def send_login_code(self, chat_id, code: str, lang: str = "ru") -> bool:
        if not self.bot:
            return False
        u = {"lang": lang}
        try:
            res = await self.bot.call("sendMessage", chat_id=chat_id, parse_mode="HTML",
                                text=T(u, f"🔐 Код для входа на сайт: <code>{code}</code>\n\nДействует 5 минут. "
                                          "Никому его не сообщайте. Если вы не запрашивали код — просто проигнорируйте это сообщение.",
                                          f"🔐 Website login code: <code>{code}</code>\n\nValid for 5 minutes. "
                                          "Do not share it with anyone. If you did not request it, just ignore this message."))
            self.track(chat_id, res)
            return True
        except Exception as e:
            log.warning("login code %s: %s", chat_id, e)
            return False

    async def ask_promo(self, chat, u):
        self.awaiting_promo.add(u["id"])
        await self.send(chat, T(u, "🎟 Отправьте промокод одним сообщением.", "🎟 Send the promo code in one message."),
                        {"inline_keyboard": [[{"text": T(u, "« Назад", "« Back"), "callback_data": "menu"}]]})

    async def apply_promo(self, chat, u, code: str):
        code = code.strip()[:40]
        now = time.time()
        fails = [t for t in self.promo_fails.get(u["id"], []) if now - t < 3600]
        self.promo_fails[u["id"]] = fails
        if len(fails) >= 10:
            return await self.send(chat, T(u, "❌ Слишком много попыток. Попробуйте через час.",
                                           "❌ Too many attempts. Try again in an hour."))
        p = promo.find(code) if re.fullmatch(r"[\w\-]+", code) else None
        err = promo.check(p, u)
        if err:
            if err == "not_found":
                fails.append(now)
            ru, en = promo.ERR[err]
            return await self.send(chat, "❌ " + T(u, ru, en),
                                   {"inline_keyboard": [[{"text": T(u, "🎟 Другой промокод", "🎟 Another code"), "callback_data": "promo"},
                                                         {"text": T(u, "« Меню", "« Menu"), "callback_data": "menu"}]]})
        ex("UPDATE users SET promo_id=? WHERE id=?", (p["id"], u["id"]))
        plans = promo.plan_list(p)
        where = (", ".join(mword(u, m) for m in plans) if plans else T(u, "все тарифы", "all plans"))
        text = T(u, f"✅ Промокод <b>{html.escape(p['code'])}</b> применён: скидка <b>{p['discount']}%</b> ({where}).",
                    f"✅ Promo code <b>{html.escape(p['code'])}</b> applied: <b>{p['discount']}% off</b> ({where}).")
        if p["expires_at"]:
            text += T(u, f"\nДействует до {core.fmt_date(p['expires_at'])}.", f"\nValid until {core.fmt_date(p['expires_at'])}.")
        await self.send(chat, text)
        await self.send_tariffs(chat, self.reload(u))

    def active_promo(self, u) -> dict | None:
        p = promo.get(u.get("promo_id") or 0)
        if p and promo.check(p, u):
            ex("UPDATE users SET promo_id=0 WHERE id=?", (u["id"],))
            return None
        return p

    async def send_tariffs(self, chat, u=None):
        u = self.reload(u)
        if u.get("forever"):
            return await self.send(chat, T(u, "♾ У вас бессрочная подписка — продлевать ничего не нужно.",
                                           "♾ You have a lifetime subscription — nothing to renew."), self.menu_kb(u))
        cfg = payments.load()
        methods = payments.enabled(cfg)
        p = self.active_promo(u)
        rows = []
        for m in payments.plan_months(cfg):
            parts, disc = [], False
            for meth in methods:
                base, cost = payments.quote(meth, m, p, cfg)
                if not base:
                    continue
                disc = disc or cost != base
                parts.append(T(u, "бесплатно", "free") if cost == 0 else payments.fmt(cost, payments.currency(meth, cfg)))
            label = f"{mword(u, m)} — " + " / ".join(dict.fromkeys(parts))
            if disc:
                label += f" (−{p['discount']}%)"
            rows.append([{"text": label, "callback_data": f"plan:{m}"}])
        if p:
            rows.append([{"text": T(u, f"✖ Убрать промокод {p['code']}", f"✖ Remove promo {p['code']}"), "callback_data": "unpromo"}])
        else:
            rows.append([{"text": T(u, "🎟 Ввести промокод", "🎟 Enter a promo code"), "callback_data": "promo"}])
        rows.append([{"text": T(u, "« Назад", "« Back"), "callback_data": "menu"}])
        names = ", ".join(T(u, *payments.METHOD_NAMES[m]) for m in methods) or "—"
        await self.send(chat, T(u, f"Выберите тариф.\nСпособы оплаты: {names}", f"Choose a plan.\nPayment methods: {names}"),
                        {"inline_keyboard": rows})

    def _offer(self, u, months: int, method: str):
        p = self.active_promo(u)
        if p and not promo.applies_to(p, months):
            p = None
        base, amount = payments.quote(method, months, p)
        return base, amount, p

    async def buy(self, chat, u, months: int):
        u = self.reload(u)
        cfg = payments.load()
        methods = [m for m in payments.enabled(cfg) if payments.base_price(m, months, cfg)]
        if not methods:
            return await self.send(chat, T(u, "Тариф недоступен.", "This plan is not available."))
        base, amount, p = self._offer(u, months, methods[0])
        if base and amount == 0:
            return await self.activate_free(chat, u, months, p, methods[0])
        if len(methods) == 1:
            return await self.pay(chat, u, months, methods[0])
        rows = []
        for m in methods:
            _, a, _ = self._offer(u, months, m)
            rows.append([{"text": f"{T(u, *payments.METHOD_NAMES[m])} — {payments.fmt(a, payments.currency(m, cfg))}",
                          "callback_data": f"pay:{m}:{months}"}])
        rows.append([{"text": T(u, "« Назад", "« Back"), "callback_data": "buy"}])
        await self.send(chat, T(u, f"<b>{mword(u, months)}</b>. Выберите способ оплаты:",
                                f"<b>{mword(u, months)}</b>. Choose a payment method:"), {"inline_keyboard": rows})

    async def activate_free(self, chat, u, months, p, method="stars"):
        cur = payments.currency(method)
        base = payments.base_price(method, months)
        pid = ex("INSERT INTO payments(user_id,tg_charge_id,months,amount,created,promo_id,full_amount,method,currency)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (u["id"], f"promo:{p['id']}:{u['id']}:{int(time.time())}", months, 0, time.time(), p["id"], base, "promo", cur))
        promo.record_use(p, u["id"], pid, months, 0, base, cur)
        return await self.activated(chat, u["id"], months, 0, p, cur)

    def invoice_params(self, u, months: int, method: str) -> dict:
        cfg = payments.load()
        base, amount, p = self._offer(u, months, method)
        if not base:
            raise payments.PayError("Тариф недоступен.")
        title = get_setting("panel_title", "VPN")
        desc = T(u, f"Подписка на {mword(u, months)}. Без лимита трафика, все серверы.",
                    f"{mword(u, months)} subscription. Unlimited traffic, all servers.")
        if p:
            desc += T(u, f" Промокод {p['code']}: −{p['discount']}%.", f" Promo {p['code']}: −{p['discount']}%.")
        prefix = "plan" if method == "stars" else "card"
        params = {"title": f"{title}: {mword(u, months)}"[:32], "description": desc[:255],
                  "payload": f"{prefix}:{months}:{u['id']}:{amount}:{p['id'] if p else 0}",
                  "currency": payments.currency(method, cfg), "prices": [{"label": mword(u, months), "amount": amount}]}
        if method == "card":
            c = cfg["card"]
            params["provider_token"] = c["token"]
            if c.get("receipt"):
                params.update(need_email=True, send_email_to_provider=True, provider_data=json.dumps({"receipt": {"items": [{
                    "description": params["title"], "quantity": "1.00",
                    "amount": {"value": f"{amount / 100:.2f}", "currency": params["currency"]}, "vat_code": 1}]}}))
        return params

    async def pay(self, chat, u, months: int, method: str):
        u = self.reload(u)
        base, amount, p = self._offer(u, months, method)
        if base and amount == 0:
            return await self.activate_free(chat, u, months, p, method)
        try:
            if method == "manual":
                return await self.send_manual(chat, u, payments.create_manual(u, months, amount, base, p))
            if method == "crypto":
                inv = await asyncio.to_thread(payments.create_crypto_invoice, u, months, amount, base, p,
                                              f"{get_setting('panel_title', 'VPN')}: {mword(u, months)}", self.username, chat)
                cur = payments.currency("crypto")
                return await self.send(chat, T(u, f"💎 Счёт на <b>{payments.fmt(amount, cur)}</b> ({mword(u, months)}) создан.\n"
                                                  "Оплатите в @CryptoBot в течение часа — подписка продлится автоматически.",
                                                  f"💎 Invoice for <b>{payments.fmt(amount, cur)}</b> ({mword(u, months)}) created.\n"
                                                  "Pay in @CryptoBot within an hour — the subscription renews automatically."),
                                       {"inline_keyboard": [[{"text": T(u, "💎 Оплатить", "💎 Pay"), "url": inv["url"]}],
                                                            [{"text": T(u, "« Меню", "« Menu"), "callback_data": "menu"}]]})
            ctx = UI_CTX.get()
            if ctx and ctx.get("edit"):
                await self._delete(chat, ctx.pop("edit"))
            self.track(chat, await self.bot.call("sendInvoice", chat_id=chat, **self.invoice_params(u, months, method)))
        except (payments.PayError, TelegramError, tg.TransportError) as e:
            log.warning("payment %s: %s", method, e)
            text = T(u, "❌ Не удалось создать счёт. Попробуйте другой способ оплаты или позже.",
                     "❌ Could not create the invoice. Try another payment method or later.")
            if u.get("tg_id") in self.admin_ids():
                text += "\n\n<i>" + T(u, "Причина (видна только администратору): ", "Reason (admins only): ") + html.escape(str(e)) + "</i>"
            await self.send(chat, text)
            await self.notify_admins(TA("⚠️ Ошибка оплаты ", "⚠️ Payment error ") + f"({method}): {html.escape(str(e))}")

    async def link_admin(self, chat, u, code: str):
        try:
            want = json.loads(get_setting("admin_link") or "{}")
        except ValueError:
            want = {}
        if not code or not want.get("code") or time.time() > want.get("until", 0) \
                or not hmac.compare_digest(code.upper(), str(want["code"])):
            return await self.send(chat, T(u, "Код не подошёл или устарел. Получите новый в админке: Telegram-бот → «Подключить мой Telegram».",
                                           "The code is wrong or expired. Get a new one in the admin panel: Telegram bot → “Link my Telegram”."))
        ids = self.admin_ids() | {int(chat)}
        set_setting("admin_tg_ids", ", ".join(str(i) for i in sorted(ids)))
        set_setting("admin_link", "")
        await self.send(chat, T(u, "✅ Этот Telegram подключён к уведомлениям администратора: оплаты, переводы, новые пользователи, ошибки.",
                                "✅ This Telegram now receives admin notifications: payments, transfers, new users, errors."))

    async def send_manual(self, chat, u, order: dict):
        await self.send(chat, payments.manual_text(order, L(u)), {"inline_keyboard": [
            [{"text": T(u, "✅ Я оплатил", "✅ I have paid"), "callback_data": f"mp:{order['id']}"}],
            [{"text": T(u, "« Меню", "« Menu"), "callback_data": "menu"}]]})

    async def manual_claim(self, chat, u, oid: int):
        o = q1("SELECT * FROM manual_payments WHERE id=? AND user_id=?", (oid, u["id"]))
        if not o or o["status"] != "new":
            return await self.send(chat, T(u, "Эта заявка уже обработана.", "This request has already been processed."))
        ex("UPDATE manual_payments SET status='claimed', claimed=? WHERE id=?", (time.time(), oid))
        await self.send(chat, T(u, "⏳ Спасибо! Заявка отправлена администратору — подписка продлится после проверки оплаты.",
                                "⏳ Thank you! Your request was sent to the administrator — the subscription renews once the payment is checked."),
                        self.menu_kb(u))
        who = html.escape(u["name"] or "") + (f" @{html.escape(u['tg_username'])}" if u.get("tg_username") else "")
        code_line = (TA("\n🔑 Код в комментарии к переводу: ", "\n🔑 Code in the payment comment: ") + f"<code>{o['code']}</code>") \
            if o.get("code") else (TA("\nВ комментарии — номер заказа: ", "\nThe comment holds the order number: ") + f"<code>{oid}</code>")
        await self.notify_admins(
            TA("🏦 Перевод: заказ №", "🏦 Transfer: order #") + f"{oid} — {who}, {mword(None, o['months'])}, "
            f"<b>{payments.fmt(o['amount'], o['currency'])}</b>" + code_line + "\n"
            + TA("Сверьте поступление в банке.", "Check the incoming payment in your bank."),
            {"inline_keyboard": [[{"text": TA("✅ Подтвердить", "✅ Confirm"), "callback_data": f"mpa:ok:{oid}"},
                                  {"text": TA("❌ Отклонить", "❌ Reject"), "callback_data": f"mpa:no:{oid}"}]]})

    async def manual_decide(self, oid: int, approve: bool, by: str) -> str:
        status = "confirmed" if approve else "rejected"
        if not ex_count("UPDATE manual_payments SET status=?, decided_by=? WHERE id=? AND status IN ('new','claimed')",
                        (status, by[:60], oid)):
            return ""
        o = q1("SELECT * FROM manual_payments WHERE id=?", (oid,))
        u = q1("SELECT * FROM users WHERE id=?", (o["user_id"],))
        if not u:
            return status
        if approve:
            await self.record_payment(u.get("tg_id"), u["id"], o["months"], o["amount"], o["promo_id"], "manual",
                                      o["currency"], f"manual:{oid}")
        elif u.get("tg_id"):
            sup = (get_setting("support") or "").strip()
            await self.notify(u["tg_id"], (T(u, f"❌ Перевод с кодом {o['code']} не найден.", f"❌ The transfer with code {o['code']} was not found.") if o.get("code")
                               else T(u, f"❌ Оплата по заказу №{oid} не найдена.", f"❌ Payment for order #{oid} was not found."))
                              + (T(u, " Напишите в поддержку: ", " Contact support: ") + html.escape(sup) if sup else ""))
        return status

    async def invoice_link(self, u, months: int, method: str) -> str:
        u = self.reload(u)
        if not self.bot:
            raise payments.PayError("Бот не запущен")
        base, amount, p = self._offer(u, months, method)
        if not base:
            raise payments.PayError("Тариф недоступен.")
        if amount == 0:
            raise payments.PayError("free")
        if method == "crypto":
            inv = await asyncio.to_thread(payments.create_crypto_invoice, u, months, amount, base, p,
                                          f"{get_setting('panel_title', 'VPN')}: {mword(u, months)}", self.username, u.get("tg_id"))
            return inv["url"]
        if method == "manual":
            if not u.get("tg_id"):
                raise payments.PayError("Бот не запущен")
            await self.send_manual(u["tg_id"], u, payments.create_manual(u, months, amount, base, p))
            return f"https://t.me/{self.username}"
        return await self.bot.call("createInvoiceLink", **self.invoice_params(u, months, method))

    async def send_status(self, chat, u):
        u = self.reload(u)
        used = core.fmt_bytes(u["used_up"] + u["used_down"])
        limit = core.fmt_bytes(u["traffic_limit"]) if u["traffic_limit"] else T(u, "без ограничений", "unlimited")
        text = T(u, f"<b>Моя подписка</b>\n\nСтатус: <b>{core.status_text(u, 'ru')}</b>\n"
                    f"Действует до: <b>{core.expires_text(u, 'ru')}</b>\nТрафик: <b>{used}</b> (лимит: {limit})\n",
                    f"<b>My subscription</b>\n\nStatus: <b>{core.status_text(u, 'en')}</b>\n"
                    f"Valid until: <b>{core.expires_text(u, 'en')}</b>\nTraffic: <b>{used}</b> (limit: {limit})\n")
        if core.public_url():
            text += (T(u, "\nСтраница подписки:\n", "\nSubscription page:\n") + html.escape(core.user_page_url(u)) +
                     T(u, "\n\nСсылка подписки (Happ, INCY):\n", "\n\nSubscription link (Happ, INCY):\n") + f"<code>{html.escape(core.sub_url(u))}</code>")
        await self.send(chat, text, self.menu_kb(u))

    async def send_help(self, chat, u):
        await self.send(chat, self.text("help", u), self.menu_kb(u))

    async def send_custom(self, chat, u, bid: str):
        b = next((x for x in botui.load()["buttons"] if x["id"] == bid and x.get("type") == "text"), None)
        if not b:
            return await self.send_menu(chat, u)
        raw = b.get("text_en" if L(u) == "en" else "text_ru") or b.get("text_ru") or b.get("text_en")
        await self.send(chat, botui.render(raw, self.values(u)), self.menu_kb(u))

    async def on_callback(self, cq):
        u = self.ensure_user(cq["from"])
        chat = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        if data != "promo":
            self.awaiting_promo.discard(u["id"])
        if data == "buy":
            await self.send_tariffs(chat, u)
        elif data == "me":
            await self.send_status(chat, u)
        elif data == "help":
            await self.send_help(chat, u)
        elif data == "phone":
            await self.ask_phone(chat, u)
        elif data == "promo":
            await self.ask_promo(chat, u)
        elif data == "unpromo":
            ex("UPDATE users SET promo_id=0 WHERE id=?", (u["id"],))
            await self.send_tariffs(chat, u)
        elif data.startswith("lang:"):
            lang = "en" if data.endswith("en") else "ru"
            ex("UPDATE users SET lang=? WHERE id=?", (lang, u["id"]))
            await self.send_menu(chat, u)
        elif data == "menu":
            await self.send_menu(chat, u)
        elif data.startswith("c_"):
            await self.send_custom(chat, u, data)
        elif data.startswith("plan:"):
            await self.buy(chat, u, int(data.split(":")[1]))
        elif data.startswith("mp:"):
            await self.manual_claim(chat, u, int(data[3:]))
        elif data.startswith("mpa:"):
            _, act, oid = data.split(":")
            if cq["from"]["id"] not in self.admin_ids():
                return
            st = await self.manual_decide(int(oid), act == "ok", "tg:" + str(cq["from"]["id"]))
            res = {"confirmed": TA("✅ Подтверждено", "✅ Confirmed"), "rejected": TA("❌ Отклонено", "❌ Rejected")}.get(
                st, TA("Заявка уже обработана", "Already processed"))
            try:
                await self.bot.call("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"],
                                    reply_markup={"inline_keyboard": [[{"text": res, "callback_data": "noop"}]]})
            except Exception:
                await self.notify(chat, res)
        elif data.startswith("pay:"):
            _, method, m = data.split(":")
            if method in payments.enabled():
                await self.pay(chat, u, int(m), method)

    @staticmethod
    def parse_payload(payload: str):
        parts = payload.split(":")
        method = {"plan": "stars", "card": "card", "cr": "crypto"}[parts[0]]
        months, uid, amount = int(parts[1]), int(parts[2]), int(parts[3])
        pid = int(parts[4]) if len(parts) > 4 else 0
        return method, months, uid, amount, pid

    async def on_pre_checkout(self, pq):
        ok, err = True, None
        try:
            method, months, uid, amount, pid = self.parse_payload(pq["invoice_payload"])
            u = q1("SELECT * FROM users WHERE id=?", (uid,))
            base = payments.base_price(method, months)
            p = promo.get(pid) if pid else None
            if not u:
                ok, err = False, "User not found, press /start / Пользователь не найден, нажмите /start."
            elif method not in payments.enabled() or pq.get("currency") != payments.currency(method):
                ok, err = False, T(u, "Этот способ оплаты отключён — откройте тарифы заново.",
                                   "This payment method is disabled — open the plans again.")
            elif pid and (not p or promo.check(p, u, months)):
                ok, err = False, T(u, "Промокод больше не действует — откройте тарифы заново.",
                                   "The promo code is no longer valid — open the plans again.")
            elif not base or promo.price(base, p, months) != amount or pq["total_amount"] != amount:
                ok, err = False, T(u, "Цена изменилась, откройте тарифы заново.", "The price has changed, open the plans again.")
        except Exception:
            ok, err = False, "Invalid invoice / Некорректный счёт."
        params = {"pre_checkout_query_id": pq["id"], "ok": ok}
        if err:
            params["error_message"] = err
        await self.bot.call("answerPreCheckoutQuery", **params)

    async def on_paid(self, msg):
        sp = msg["successful_payment"]
        method, months, uid, _, pid = self.parse_payload(sp["invoice_payload"])
        cur = sp.get("currency")
        if method == "crypto" or cur != ("XTR" if method == "stars" else payments.currency(method)):
            log.warning("payment ignored: method %s currency %s", method, cur)
            return
        charge = sp.get("telegram_payment_charge_id", "")
        if q1("SELECT id FROM payments WHERE tg_charge_id=?", (charge,)):
            return
        await self.record_payment(msg["chat"]["id"], uid, months, sp["total_amount"], pid, method, cur, charge,
                                  provider_charge=sp.get("provider_payment_charge_id", ""))

    async def record_payment(self, chat, uid, months, total, pid, method, cur, charge, provider_charge=""):
        base = payments.base_price(method, months) or total
        pay_id = ex("INSERT INTO payments(user_id,tg_charge_id,months,amount,created,promo_id,full_amount,method,currency)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (uid, charge, months, total, time.time(), pid, base, method, cur))
        p = promo.get(pid) if pid else None
        if p:
            promo.record_use(p, uid, pay_id, months, total, base, cur)
        await self.activated(chat, uid, months, total, p, cur)

    async def on_crypto_paid(self, row: dict, inv: dict):
        charge = f"crypto:{row['invoice_id']}"
        if q1("SELECT id FROM payments WHERE tg_charge_id=?", (charge,)):
            return
        u = q1("SELECT * FROM users WHERE id=?", (row["user_id"],))
        if not u:
            return
        await self.record_payment(row["chat_id"] or u.get("tg_id"), u["id"], row["months"], row["amount"],
                                  row["promo_id"], "crypto", row["currency"], charge)

    async def activated(self, chat, uid: int, months: int, amount: int, p=None, cur: str = "XTR"):
        core.extend_user(uid, months * 30)
        ex("UPDATE users SET suspended_until=0 WHERE id=?", (uid,))
        request_sync()
        u = q1("SELECT * FROM users WHERE id=?", (uid,))
        if chat:
            head = T(u, "✅ Оплата получена!", "✅ Payment received!") if amount else T(u, "✅ Подписка активирована по промокоду!", "✅ Subscription activated with a promo code!")
            try:
                await self._send(chat, f"{head} " + T(u, "Подписка активна до ", "Subscription active until ") +
                                 f"<b>{core.expires_text(u, L(u))}</b>.")
                await self.send_status(chat, u)
            except Exception as e:
                log.warning("notify payer: %s", e)
        promo_txt = f" ({TA('промокод', 'promo')} {p['code']}, −{p['discount']}%)" if p else ""
        await self.notify_admins(
            TA("💰 Оплата: ", "💰 Payment: ") + f"{html.escape(u['name'] or str(u['tg_id']))} — "
            f"{MONTHS[get_setting('lang', 'ru') if get_setting('lang', 'ru') in MONTHS else 'ru'].get(months, months)} "
            + TA("за", "for") + f" {payments.fmt(amount, cur)}{promo_txt}")


manager = BotManager()
