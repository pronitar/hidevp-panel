import contextvars
import re

LANG: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default="ru")

EXACT = {
    "Требуется вход": "Login required",
    "Слишком много попыток. Подождите 10 минут.": "Too many attempts. Wait 10 minutes.",
    "Неверный логин или пароль": "Wrong login or password",
    "Текущий пароль неверен": "Current password is wrong",
    "Пароль должен быть не короче 8 символов": "Password must be at least 8 characters",
    "Этот сервер уже подключён": "This server is already connected",
    "Сервер не найден": "Server not found",
    "На сервере старая версия work-панели — обновите её: bash install-node.sh":
        "The server runs an old work panel — update it: hidevp → Update",
    "Выберите подключённый сервер": "Select a connected server",
    "Порт должен быть от 1 до 65535": "Port must be between 1 and 65535",
    "Этот порт занят work-панелью сервера": "This port is used by the server's work panel",
    "Неизвестный транспорт": "Unknown transport",
    "Неизвестный отпечаток": "Unknown fingerprint",
    "Укажите домен для маскировки": "Enter a camouflage domain (SNI)",
    "VPN не найден": "VPN not found",
    "Пользователь не найден": "User not found",
    "Неверный номер телефона": "Invalid phone number",
    "У пользователя нет Telegram": "The user has no Telegram",
    "Неизвестное действие": "Unknown action",
    "Публичный адрес должен начинаться с http:// или https://": "Public URL must start with http:// or https://",
    "Неизвестный способ подключения к Telegram": "Unknown Telegram connection mode",
    "Прокси: формат socks5://host:port или http://host:port": "Proxy format: socks5://host:port or http://host:port",
    "Неверный формат ключа. Скопируйте ключ целиком (начинается с NODE-).":
        "Invalid key format. Copy the whole key (starts with NODE-).",
    "Отпечаток сертификата узла не совпадает с ключом — подключение отклонено.":
        "Node certificate fingerprint does not match the key — connection rejected.",
    "Токен бота не задан": "Bot token is not set",
    "напрямую": "direct",
    "Подключение к Telegram занимает слишком долго — проверьте способ подключения":
        "Connecting to Telegram takes too long — check the connection mode",
    "Не указан адрес прокси для Telegram": "Telegram proxy address is not set",
    "Сервер для связи с Telegram не выбран или удалён (Настройки → Telegram-бот)":
        "Server for Telegram access is not selected or was removed (Settings → Telegram bot)",
    "Время на сервере совпадает с master": "Server time matches the master",
    "Промокод: 3–32 символа, латинские буквы, цифры, - и _": "Promo code: 3–32 characters, Latin letters, digits, - and _",
    "Такой промокод уже есть": "This promo code already exists",
    "Скидка должна быть от 1 до 100%": "Discount must be between 1 and 100%",
    "Промокод не найден": "Promo code not found",
    "Файл слишком большой": "The file is too large",
    "В архиве нет panel.db — это не резервная копия HideVP": "The archive has no panel.db — this is not a HideVP backup",
    "Повреждённый архив": "Corrupted archive",
    "Неизвестный формат файла. Нужен .zip, скачанный в разделе «Резервная копия»": "Unknown file format. Use the .zip downloaded in the “Backup” section",
    "Файл базы повреждён": "The database file is corrupted",
    "Это не база HideVP (нет нужных таблиц)": "This is not a HideVP database (required tables missing)",
    "Неизвестный режим восстановления": "Unknown restore mode",
    "Резервная копия не найдена": "Backup not found",
    "Выберите файл резервной копии": "Choose a backup file",
    "В этой установке нет ключа подписи релизов — обновите панель вручную один раз": "This installation has no release signing key — update the panel manually once",
    "Подпись обновления не прошла проверку — обновление отклонено": "Update signature verification failed — update rejected",
    "Неверный манифест обновления": "Invalid update manifest",
    "Файл обновления слишком большой": "The update file is too large",
    "Нет информации об обновлении": "No update information",
    "Контрольная сумма пакета не совпала — обновление отклонено": "Package checksum mismatch — update rejected",
    "Небезопасный путь в пакете": "Unsafe path in the package",
    "В пакете нет установщика": "The package has no installer",
    "Адрес обновлений должен начинаться с http:// или https://": "The update URL must start with http:// or https://",
    "Неизвестный период": "Unknown period",
    "Хранить можно от 1 до 100 копий": "You can keep from 1 to 100 copies",
    "Неверные данные": "Invalid data",
    "Неизвестный тип кнопки": "Unknown button type",
    "У своей кнопки должна быть подпись": "A custom button needs a label",
    "Ссылка кнопки должна начинаться с https:// или tg://": "The button link must start with https:// or tg://",
    "Укажите текст, который бот отправит по кнопке": "Enter the text the bot sends for the button",
    "Кнопки повторяются": "Duplicate buttons",
    "Цена должна быть числом": "The price must be a number",
    "Токен провайдера: скопируйте его из @BotFather → Payments целиком": "Provider token: copy it in full from @BotFather → Payments",
    "Укажите токен платёжного провайдера": "Enter the payment provider token",
    "Токен CryptoBot: скопируйте его из @CryptoBot → Crypto Pay → Мои приложения": "CryptoBot token: copy it from @CryptoBot → Crypto Pay → My Apps",
    "Укажите токен CryptoBot": "Enter the CryptoBot token",
    "Включите хотя бы один способ оплаты": "Enable at least one payment method",
    "Включите хотя бы один тариф": "Enable at least one plan",
    "Неизвестная валюта": "Unknown currency",
    "Укажите реквизиты для перевода": "Enter the transfer details",
    "Заявка уже обработана": "The request has already been processed",
    "CryptoBot: неверный токен. Токен из @CryptoBot работает без флажка «Тестовая сеть», токен из @CryptoTestnetBot — только с ним":
        "CryptoBot: invalid token. A token from @CryptoBot works without the “Testnet” checkbox, a token from @CryptoTestnetBot — only with it",
    "Введите текст уведомления": "Enter the notice text",
    "Неверная дата": "Invalid date",
    "Окончание должно быть позже начала": "The end must be later than the start",
    "Уведомление не найдено": "Notice not found",
    "Не указан ни один Telegram ID администратора": "No admin Telegram ID is set",
    "Откройте бота с этого аккаунта и нажмите «Старт» (/start) — без этого бот не может писать": "Open the bot from this account and press “Start” (/start) — otherwise the bot cannot write",
    "Бот не запущен — проверьте токен": "The bot is not running — check the token",
    "Тариф недоступен.": "This plan is not available.",
    "Бот не запущен": "The bot is not running",
    "Не удалось создать счёт. Попробуйте другой способ оплаты или позже.": "Could not create the invoice. Try another payment method or later.",
    "Обновление уже выполняется": "An update is already running",
    "Нет связи с work-панелью — нажмите «Обновить» на карточке сервера": "No connection to the work panel — press “Refresh” on the server card",
    "Введите номер телефона полностью": "Enter the full phone number",
    "Слишком много запросов. Попробуйте через час.": "Too many requests. Try again in an hour.",
    "Слишком много кодов для этого номера. Попробуйте через час.": "Too many codes for this number. Try again in an hour.",
    "Не удалось отправить код в Telegram. Откройте бота и нажмите /start.": "Could not send the code to Telegram. Open the bot and press /start.",
    "Код устарел. Запросите новый.": "The code has expired. Request a new one.",
    "Слишком много неверных попыток. Запросите новый код.": "Too many wrong attempts. Request a new code.",
    "Включите синхронизацию времени: timedatectl set-ntp true": "Enable time sync: timedatectl set-ntp true",
    "Порт закрыт снаружи: откройте его в фаерволе хостера (security groups) и в ufw, "
    "или IP сервера блокируется — попробуйте другой порт/транспорт":
        "The port is closed from outside: open it in the hoster's firewall (security groups) and ufw, "
        "or the server IP is blocked — try another port/transport",
}

PATTERNS = [
    (r"^Неверный код\. Осталось попыток: (\d+)$", r"Wrong code. Attempts left: \1"),
    (r"^Порт (\d+) занят: VPN «(.*)»$", r"Port \1 is used by VPN “\2”"),
    (r"^Порт (\d+) занят: work-панель$", r"Port \1 is used by the work panel"),
    (r"^Порт (\d+) занят: (.*)$", r"Port \1 is used by \2"),
    (r"^CryptoBot недоступен: (.*)$", r"CryptoBot is unavailable: \1"),
    (r"^CryptoBot: неожиданный ответ \(HTTP (\d+)\)", r"CryptoBot: unexpected response (HTTP \1)"),
    (r" — запрос заблокирован на стороне CryptoBot \(Cloudflare\)\. Обновите панель на всех серверах; если не помогло, выберите для Telegram другой маршрут \(через зарубежный сервер\)",
     " — the request was blocked on the CryptoBot side (Cloudflare). Update the panel on all servers; if that does not help, choose another Telegram route (via a foreign server)"),
    (r"\. Ответ: ", ". Response: "),
    (r"^Не больше (\d+) кнопок$", r"No more than \1 buttons"),
    (r"^Не удалось проверить обновления: (.*)$", r"Could not check for updates: \1"),
    (r"^Не удалось запустить обновление: (.*)$", r"Could not start the update: \1"),
    (r"^Не удалось проверить порт (\d+) на сервере — попробуйте ещё раз$", r"Could not test port \1 on the server — try again"),
    (r"^Порт (\d+) недоступен снаружи \(проверено с: (.*)\)$", r"Port \1 is not reachable from outside (checked from: \2)"),
    (r"^Порт (\d+) на сервере занят программой «(.*?)» — выберите другой порт \(например 2053, 2083, 9443\)$",
     r"Port \1 on the server is used by “\2” — choose another port (e.g. 2053, 2083, 9443)"),
    (r"^Порт (\d+) на этом сервере уже используется другим VPN$", r"Port \1 is already used by another VPN on this server"),
    (r"^Не удалось перезапустить Xray: (.*)$", r"Failed to restart Xray: \1"),
    (r"^Не удалось подключиться к (\S+) — (.*)$", r"Cannot connect to \1 — \2"),
    (r"^через сервер «(.*)»$", r"via server “\1”"),
    (r"^через прокси (.*)$", r"via proxy \1"),
    (r"^Telegram отклонил токен: (.*)$", r"Telegram rejected the token: \1"),
    (r"^Нет связи с Telegram \((.*?)\): (.*)$", None),
    (r"^Ошибка связи с Telegram \((.*?)\): (.*)$", None),
    (r"не удалось запустить обновление: ", "could not start the update: "),
    (r"контрольная сумма пакета не совпала", "package checksum mismatch"),
    (r"небезопасный путь в пакете", "unsafe path in the package"),
    (r"в пакете нет install-node\.sh", "the package has no install-node.sh"),
    (r"^Узел «(.*?)»: на узле старая версия work-панели — обновите её \(install-node\.sh\)$",
     r"Node “\1”: old work panel version — update it"),
    (r"^Узел «(.*?)»: (.*)$", r"Node “\1”: \2"),
    (r"^расхождение (\d+) с$", r"difference \1 s"),
    (r"^Порт (\d+) доступен с master-сервера \(как для клиентов из России\)$",
     r"Port \1 is reachable from the master server (as for clients in Russia)"),
    (r"^(\d+) мс$", r"\1 ms"),
    (r"^Xray не запустился после применения настроек\.(.*)$", r"Xray failed to start after applying the config.\1"),
    (r" Порт занят другой программой: (.*?)\.", r" Port is used by another program: \1."),
]


def tr(msg: str, lang: str | None = None) -> str:
    lang = lang or LANG.get()
    if lang != "en" or not msg:
        return msg
    if msg in EXACT:
        return EXACT[msg]
    out = msg
    for pat, rep in PATTERNS:
        m = re.search(pat, out, re.S)
        if not m:
            continue
        if rep is None:
            head = "No connection to Telegram" if out.startswith("Нет") else "Telegram connection error"
            out = f"{head} ({tr(m.group(1), lang)}): {m.group(2)}"
        else:
            out = re.sub(pat, rep, out, count=1, flags=re.S)
    return out
