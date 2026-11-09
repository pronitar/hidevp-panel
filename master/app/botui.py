import json
import re
import secrets

from .db import get_setting, set_setting

BUILTIN = {
    "buy":     ("💳 Купить / продлить", "💳 Buy / renew"),
    "page":    ("🌐 Страница подписки", "🌐 Subscription page"),
    "me":      ("👤 Моя подписка", "👤 My subscription"),
    "regen":   ("🔄 Перевыпустить ссылки", "🔄 Reissue links"),
    "promo":   ("🎟 Промокод", "🎟 Promo code"),
    "help":    ("📱 Как подключиться", "📱 How to connect"),
    "phone":   ("☎️ Привязать номер для входа на сайт", "☎️ Link phone for website login"),
    "support": ("💬 Поддержка", "💬 Support"),
    "lang":    ("🌐 English", "🌐 Русский"),
}
BUILTIN_HINT = {
    "buy": "тарифы и оплата", "page": "ссылка на личную страницу (если задан публичный адрес)",
    "me": "статус, трафик, ссылка подписки", "regen": "новые ссылки подписки и личной страницы, старые перестают работать", "promo": "ввод промокода", "help": "текст «Как подключиться»",
    "phone": "видна, пока номер не привязан", "support": "ссылка на контакт поддержки из настроек",
    "lang": "переключение RU/EN",
}

DEFAULT_BUTTONS = [
    {"id": "buy"}, {"id": "page"}, {"id": "me"},
    {"id": "promo"}, {"id": "help", "same_row": True},
    {"id": "phone"}, {"id": "support", "on": False}, {"id": "regen", "on": False}, {"id": "lang"},
]

DEFAULT_TEXTS = {
    "welcome_ru": "👋 Добро пожаловать в <b>{title}</b>!\n\nВы зарегистрированы. Ваша личная страница — статус подписки, "
                  "трафик, QR-код и кнопки для Happ и INCY:\n{page}\n\nБез ограничений по трафику, доступ ко всем серверам. "
                  "Чтобы начать — выберите тариф.",
    "welcome_en": "👋 Welcome to <b>{title}</b>!\n\nYou are registered. Your personal page — subscription status, "
                  "traffic, QR code and buttons for Happ and INCY:\n{page}\n\nUnlimited traffic, access to all servers. "
                  "To start, choose a plan.",
    "menu_ru": "👋 Добро пожаловать в <b>{title}</b>!\n\nБез ограничений по трафику, доступ ко всем серверам.\n"
               "Статус подписки: <b>{status}</b>\nДействует до: <b>{expires}</b>\n\nВаша личная страница:\n{page}",
    "menu_en": "👋 Welcome to <b>{title}</b>!\n\nUnlimited traffic, access to all servers.\n"
               "Subscription status: <b>{status}</b>\nValid until: <b>{expires}</b>\n\nYour personal page:\n{page}",
    "help_ru": "<b>Как подключиться</b>\n\n1. Установите одно из приложений:\n"
               "• <b>Happ</b> — App Store, Google Play, Windows/macOS: github.com/Happ-proxy/happ-desktop/releases\n"
               "• <b>INCY</b> — App Store, Google Play, Windows/macOS: github.com/INCY-DEV/incy-platforms/releases\n\n"
               "2. Откройте личную страницу, выберите приложение и нажмите «Добавить в Happ» или «Добавить в INCY». "
               "Можно и вручную: скопируйте ссылку подписки и в приложении нажмите «+» → «Вставить из буфера».\n\n"
               "3. Выберите сервер и нажмите кнопку подключения.\n\nЛичная страница: {page}\nПоддержка: {support}",
    "help_en": "<b>How to connect</b>\n\n1. Install one of the apps:\n"
               "• <b>Happ</b> — App Store, Google Play, Windows/macOS: github.com/Happ-proxy/happ-desktop/releases\n"
               "• <b>INCY</b> — App Store, Google Play, Windows/macOS: github.com/INCY-DEV/incy-platforms/releases\n\n"
               "2. Open your personal page, choose the app and tap “Add to Happ” or “Add to INCY”. "
               "Or manually: copy the subscription link and in the app tap “+” → “Paste from clipboard”.\n\n"
               "3. Choose a server and tap connect.\n\nPersonal page: {page}\nSupport: {support}",
}
DEFAULT_OPTIONS = {"ask_phone": True, "max_row": 2, "clean_chat": False}
PLACEHOLDERS = ("title", "name", "status", "expires", "page", "support")
MAX_BUTTONS = 24


class BotUiError(Exception):
    pass


def defaults() -> dict:
    return {"buttons": [dict(b) for b in DEFAULT_BUTTONS], "texts": dict(DEFAULT_TEXTS), **DEFAULT_OPTIONS}


def load() -> dict:
    try:
        raw = json.loads(get_setting("bot_ui") or "{}")
    except ValueError:
        raw = {}
    cfg = defaults()
    if isinstance(raw.get("buttons"), list) and raw["buttons"]:
        cfg["buttons"] = raw["buttons"]
    for k in DEFAULT_TEXTS:
        v = (raw.get("texts") or {}).get(k)
        if isinstance(v, str) and v.strip():
            cfg["texts"][k] = v
    for k, v in DEFAULT_OPTIONS.items():
        if k in raw:
            cfg[k] = type(v)(raw[k])
    have = {b.get("id") for b in cfg["buttons"]}
    for bid in BUILTIN:
        if bid not in have:
            cfg["buttons"].append({"id": bid, "on": False})
    return cfg


def _clean_button(b: dict) -> dict:
    bid = str(b.get("id") or "")
    out = {"id": bid, "on": bool(b.get("on", True)), "same_row": bool(b.get("same_row", False)),
           "ru": str(b.get("ru") or "").strip()[:64], "en": str(b.get("en") or "").strip()[:64]}
    if bid in BUILTIN:
        return out
    if not re.fullmatch(r"c_[a-z0-9]{4,12}", bid):
        bid = out["id"] = "c_" + secrets.token_hex(3)
    kind = b.get("type")
    if kind not in ("url", "text"):
        raise BotUiError("Неизвестный тип кнопки")
    if not (out["ru"] or out["en"]):
        raise BotUiError("У своей кнопки должна быть подпись")
    out["type"] = kind
    if kind == "url":
        url = str(b.get("url") or "").strip()
        if not re.match(r"^(https?://|tg://)\S+$", url):
            raise BotUiError("Ссылка кнопки должна начинаться с https:// или tg://")
        out["url"] = url[:500]
    else:
        out["text_ru"] = str(b.get("text_ru") or "").strip()[:3500]
        out["text_en"] = str(b.get("text_en") or "").strip()[:3500]
        if not (out["text_ru"] or out["text_en"]):
            raise BotUiError("Укажите текст, который бот отправит по кнопке")
    return out


def save(data: dict) -> dict:
    if not data:
        set_setting("bot_ui", "")
        return load()
    buttons = data.get("buttons") or []
    if len(buttons) > MAX_BUTTONS:
        raise BotUiError(f"Не больше {MAX_BUTTONS} кнопок")
    clean = [_clean_button(b) for b in buttons if isinstance(b, dict)]
    ids = [b["id"] for b in clean]
    if len(ids) != len(set(ids)):
        raise BotUiError("Кнопки повторяются")
    texts = {}
    for k in DEFAULT_TEXTS:
        v = str((data.get("texts") or {}).get(k) or "").strip()
        if v and v != DEFAULT_TEXTS[k]:
            texts[k] = v[:3500]
    max_row = int(data.get("max_row", DEFAULT_OPTIONS["max_row"]) or 2)
    cfg = {"buttons": clean, "texts": texts, "ask_phone": bool(data.get("ask_phone", True)),
           "max_row": max(1, min(4, max_row)), "clean_chat": bool(data.get("clean_chat", False))}
    set_setting("bot_ui", json.dumps(cfg, ensure_ascii=False))
    return load()


def render(template: str, values: dict) -> str:
    out = []
    for line in template.split("\n"):
        keys = re.findall(r"\{(" + "|".join(PLACEHOLDERS) + r")\}", line)
        if any(not values.get(k) for k in keys):
            continue
        for k in keys:
            line = line.replace("{" + k + "}", values[k])
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def label(b: dict, lang: str) -> str:
    own = b.get("en" if lang == "en" else "ru") or b.get("ru" if lang == "en" else "en")
    if own:
        return own
    ru, en = BUILTIN.get(b["id"], ("?", "?"))
    return en if lang == "en" else ru


def keyboard(cfg: dict, lang: str, visible) -> dict:
    rows = []
    for b in cfg["buttons"]:
        if not b.get("on", True):
            continue
        tb = visible(b)
        if not tb:
            continue
        if b.get("same_row") and rows and len(rows[-1]) < cfg.get("max_row", 2):
            rows[-1].append(tb)
        else:
            rows.append([tb])
    return {"inline_keyboard": rows}


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
