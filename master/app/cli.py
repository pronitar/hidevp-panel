import argparse
import secrets
import string
import sys

from . import core
from .db import ex, get_setting, init_db, q1, set_setting

ALPH = string.ascii_letters + string.digits


def rnd(n: int, alphabet=ALPH) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def panel_url() -> str:
    pub = get_setting("public_url").rstrip("/")
    return f"{pub}/{get_setting('web_path')}/"


def new_admin():
    login = "admin_" + rnd(6, string.ascii_lowercase + string.digits)
    pw = rnd(16)
    ex("DELETE FROM admins")
    ex("DELETE FROM sessions")
    ex("INSERT INTO admins(username, pass_hash) VALUES(?,?)", (login, core.hash_password(pw)))
    return login, pw


def T(ru, en):
    return en if get_setting("lang", "ru") == "en" else ru


def print_creds(login, pw):
    line = "=" * 64
    print(line)
    print(T("  Панель управления: ", "  Control panel: ") + panel_url())
    print(T("  Логин:  ", "  Login:    ") + login)
    print(T("  Пароль: ", "  Password: ") + pw)
    print(line)
    print(T("  Сохраните эти данные! Пароль больше нигде не показывается.", "  Save these credentials! The password is not shown anywhere else."))
    print(T("  Забыли: hidevp → Изменить данные → Сгенерировать новые", "  Forgot: hidevp → Change credentials → Generate new"))
    print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hidevp-panel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--port", type=int, required=True)
    i.add_argument("--listen", default="0.0.0.0")
    i.add_argument("--public-url", required=True)
    i.add_argument("--lang", default="")
    sl = sub.add_parser("set-lang")
    sl.add_argument("lang", choices=["ru", "en"])
    sub.add_parser("info")
    sub.add_parser("reset-admin")
    sp = sub.add_parser("set-port")
    sp.add_argument("port", type=int)
    pp = sub.add_parser("set-path")
    pp.add_argument("path", nargs="?", default="")
    sa = sub.add_parser("set-admin")
    sa.add_argument("--login", default="")
    sa.add_argument("--password", default="")
    st = sub.add_parser("set-title")
    st.add_argument("title")
    sub.add_parser("get")
    rl = sub.add_parser("release")
    rl.add_argument("zip")
    rl.add_argument("--notes", default="")
    sub.add_parser("release-key")
    mi = sub.add_parser("mirror")
    mi.add_argument("state", choices=["on", "off"])
    pu = sub.add_parser("set-url")
    pu.add_argument("url")
    a = ap.parse_args(argv)

    init_db()
    if a.cmd == "init":
        set_setting("web_port", a.port)
        set_setting("listen_host", a.listen)
        set_setting("public_url", a.public_url.rstrip("/"))
        if a.lang in ("ru", "en"):
            set_setting("lang", a.lang)
        if not get_setting("web_path"):
            set_setting("web_path", rnd(14))
        if q1("SELECT id FROM admins"):
            print(T("Панель уже инициализирована. Адрес: ", "Panel already initialized. URL: ") + panel_url())
            return
        print_creds(*new_admin())
    elif a.cmd == "info":
        a1 = q1("SELECT username FROM admins")
        print(T("Панель управления: ", "Control panel: ") + panel_url())
        print(T("Логин: ", "Login: ") + (a1["username"] if a1 else "—"))
        print(T("Порт (внутренний): ", "Port (internal): ") + get_setting("web_port"))
    elif a.cmd == "reset-admin":
        print_creds(*new_admin())
    elif a.cmd == "set-port":
        if not 1 <= a.port <= 65535:
            print("Порт должен быть от 1 до 65535"); return 1
        old = get_setting("web_port")
        set_setting("web_port", a.port)
        pub = get_setting("public_url")
        if get_setting("listen_host", "0.0.0.0") != "127.0.0.1" and pub.endswith(":" + old):
            set_setting("public_url", pub[: -len(old)] + str(a.port))
        print(T("Порт изменён. Адрес панели: ", "Port changed. Panel URL: ") + panel_url())
    elif a.cmd == "set-path":
        path = a.path.strip("/ ") or rnd(14)
        if not all(c in ALPH + "-_" for c in path) or len(path) < 4:
            print("Путь: латинские буквы, цифры, - и _, не короче 4 символов"); return 1
        set_setting("web_path", path)
        print(T("Новый адрес панели: ", "New panel URL: ") + panel_url())
    elif a.cmd == "set-admin":
        adm = q1("SELECT * FROM admins")
        if not adm:
            print("Администратор не создан. Выполните: hidevp-panel reset-admin"); return 1
        login = a.login.strip() or adm["username"]
        if a.password and len(a.password) < 8:
            print("Пароль должен быть не короче 8 символов"); return 1
        ex("UPDATE admins SET username=?, pass_hash=? WHERE id=?",
           (login, core.hash_password(a.password) if a.password else adm["pass_hash"], adm["id"]))
        if a.password:
            ex("DELETE FROM sessions")
        print(T("Логин: ", "Login: ") + login + (T("\nПароль изменён", "\nPassword changed") if a.password else ""))
    elif a.cmd == "set-lang":
        set_setting("lang", a.lang)
        print("Language: " + a.lang)
    elif a.cmd == "set-title":
        set_setting("panel_title", a.title.strip()[:40])
        print(T("Название сервиса: ", "Service name: ") + get_setting("panel_title"))
    elif a.cmd == "release":
        from . import update
        res = update.publish(a.zip, a.notes)
        m = res["manifest"]
        url = (get_setting("public_url") or "").rstrip("/") + "/updates/latest.json"
        print("=" * 64)
        print(T("  Релиз опубликован: ", "  Release published: ") + m["version"] + T(" (агент ", " (agent ") + m["agent_version"] + ")")
        print(T("  Пакет: ", "  Package: ") + res["package"] + f"  ({m['size'] // 1024} KB)")
        print(T("  Адрес обновлений: ", "  Update URL: ") + url)
        print(T("  Ключ подписи: ", "  Signing key: ") + str(update.KEY_DIR / "ed25519.key") + T("  — сохраните его копию!", "  — keep a backup copy!"))
        print("=" * 64)
        if url != update.GITHUB_URL:
            print(T("  Внимание: панели по умолчанию проверяют ", "  Note: panels check ") + update.GITHUB_URL +
                  T(" — адрес отличается.", " by default — the URL differs."))
        base = url.rsplit("/", 1)[0]
        print(T("  Все панели HideVP увидят обновление в течение 6 часов (или сразу по кнопке «Проверить обновления»).",
                "  All HideVP panels will see the update within 6 hours (or immediately via “Check for updates”)."))
        print(T("  Установка для новых пользователей:", "  Install for new users:"))
        print(f"    bash <(curl -fsSL {base}/install.sh)")
    elif a.cmd == "release-key":
        from . import update
        key = update._load_or_create_key()
        pub = update._pub_b64(key)
        (update.KEY_DIR / "ed25519.pub").write_text(pub + "\n")
        print("=" * 64)
        print(T("  Ключ подписи релизов HideVP (хранится в ", "  HideVP release signing key (stored in ") + str(update.KEY_DIR) + ")")
        print()
        print(T("  1) Секрет для GitHub — скопируйте строку ниже целиком:", "  1) GitHub secret — copy the whole line below:"))
        print(T("     github.com/pronitar/hidevp-panel → Settings → Secrets and variables → Actions →",
                "     github.com/pronitar/hidevp-panel → Settings → Secrets and variables → Actions →"))
        print(T("     New repository secret, имя: HIDEVP_RELEASE_KEY", "     New repository secret, name: HIDEVP_RELEASE_KEY"))
        print()
        print("     " + update.key_to_b64(key))
        print()
        print(T("  2) Публичный ключ (его можно показывать): ", "  2) Public key (safe to share): ") + pub)
        print()
        print(T("  Никому не передавайте строку из пункта 1 и сохраните копию папки ", "  Never share the line from step 1 and keep a backup of ") + str(update.KEY_DIR))
        print("=" * 64)
    elif a.cmd == "mirror":
        set_setting("update_mirror", "1" if a.state == "on" else "0")
        print(T("Зеркало обновлений включено: новые релизы с GitHub будут раздаваться по адресу ",
                "Update mirror enabled: new GitHub releases will be served at ") +
              (get_setting("public_url") or "").rstrip("/") + "/updates/" if a.state == "on"
              else T("Зеркало обновлений выключено", "Update mirror disabled"))
        print(T("Применится после перезапуска: systemctl restart hidevp-panel", "Takes effect after: systemctl restart hidevp-panel"))
    elif a.cmd == "get":
        a1 = q1("SELECT username FROM admins")
        for k, v in (("url", panel_url()), ("public_url", get_setting("public_url")),
                     ("login", a1["username"] if a1 else ""), ("port", get_setting("web_port")),
                     ("listen", get_setting("listen_host", "0.0.0.0")), ("path", get_setting("web_path")),
                     ("title", get_setting("panel_title")), ("lang", get_setting("lang", "ru")),
                     ("update_url", get_setting("update_url"))):
            print(f"{k}={v}")
    elif a.cmd == "set-url":
        set_setting("public_url", a.url.rstrip("/"))
        print("Публичный адрес: " + a.url)


if __name__ == "__main__":
    sys.exit(main())
