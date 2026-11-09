import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid as uuidlib
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .db import ex, get_setting, q, q1

GB = 1024 ** 3
DAY = 86400

STATUS_TEXT_EN = {
    "active": "Active", "new": "No subscription", "expired": "Expired",
    "traffic": "Traffic limit reached", "suspended": "Too many devices", "disabled": "Blocked",
}

STATUS_TEXT = {
    "active": "Активна", "new": "Нет подписки", "expired": "Истекла",
    "traffic": "Лимит трафика", "suspended": "Превышено число устройств",
    "disabled": "Заблокирован",
}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def normalize_phone(raw: str) -> str:
    d = "".join(c for c in str(raw or "") if c.isdigit())
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    elif len(d) == 10 and d.startswith("9"):
        d = "7" + d
    return d if 10 <= len(d) <= 15 else ""


def fmt_phone(d: str) -> str:
    if len(d) == 11 and d.startswith("7"):
        return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"
    return "+" + d if d else ""


def set_user_phone(user_id: int, phone: str):
    ex("UPDATE users SET phone='' WHERE phone=? AND id!=?", (phone, user_id))
    ex("UPDATE users SET phone=? WHERE id=?", (phone, user_id))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def reality_keypair() -> tuple[str, str]:
    pk = X25519PrivateKey.generate()
    priv = pk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    pub = pk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64u(priv), b64u(pub)


def user_status(u: dict, t: float | None = None) -> str:
    t = t or time.time()
    if not u["enabled"]:
        return "disabled"
    if not u.get("forever"):
        if not u["expires_at"]:
            return "new"
        if u["expires_at"] < t:
            return "expired"
    if u["traffic_limit"] and (u["used_up"] + u["used_down"]) >= u["traffic_limit"]:
        return "traffic"
    if u["suspended_until"] and u["suspended_until"] > t:
        return "suspended"
    return "active"


def create_user(tg_id=None, tg_username="", name="", months=0) -> dict:
    t = time.time()
    tl = int(float(get_setting("default_traffic_limit_gb", "0") or 0) * GB)
    dl = int(get_setting("default_device_limit", "0") or 0)
    uid = ex("""INSERT INTO users(tg_id,tg_username,name,uuid,token,created,expires_at,
                traffic_limit,device_limit,block_torrent) VALUES(?,?,?,?,?,?,?,?,?,1)""",
             (tg_id, tg_username or "", name or "", str(uuidlib.uuid4()),
              secrets.token_urlsafe(18), t, t + months * 30 * DAY if months else 0, tl, dl))
    return q1("SELECT * FROM users WHERE id=?", (uid,))


def extend_user(user_id: int, days: float):
    u = q1("SELECT * FROM users WHERE id=?", (user_id,))
    base = max(time.time(), u["expires_at"] or 0)
    ex("UPDATE users SET expires_at=?, notified_3d=0, notified_exp=0 WHERE id=?",
       (base + days * DAY, user_id))


def public_url() -> str:
    return (get_setting("public_url") or "").rstrip("/")


def user_page_url(u) -> str:
    return f"{public_url()}/u/{u['token']}"


def sub_url(u) -> str:
    return f"{public_url()}/sub/{u['token']}"


def regen_user(uid: int):
    ex("UPDATE users SET uuid=?, token=? WHERE id=?", (str(uuidlib.uuid4()), secrets.token_urlsafe(18), uid))


def happ_link(u) -> str:
    return "happ://add/" + sub_url(u)


def incy_link(u) -> str:
    return "incy://import/" + sub_url(u)


def flag(cc: str) -> str:
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)


def vless_link(u: dict, vpn: dict, node: dict) -> str:
    host = node["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    p = {"type": vpn["transport"], "security": "reality", "encryption": "none",
         "pbk": vpn["public_key"], "fp": vpn["fingerprint"], "sni": vpn["sni"],
         "sid": vpn["short_id"], "spx": "/"}
    if vpn["transport"] == "tcp":
        p["flow"] = "xtls-rprx-vision"
    elif vpn["transport"] == "grpc":
        p["serviceName"] = vpn["service_name"] or "grpc"
        p["mode"] = "gun"
    elif vpn["transport"] == "xhttp":
        p["path"] = vpn["path"] or "/"
        p["mode"] = "auto"
    title = f"{flag(node['country_code'])} {vpn['name']}"
    return f"vless://{u['uuid']}@{host}:{vpn['port']}?{urlencode(p, quote_via=quote)}#{quote(title)}"


def user_links(u: dict) -> list[str]:
    rows = q("""SELECT v.*, n.host AS n_host, n.country_code AS n_cc FROM vpns v
                JOIN nodes n ON n.id=v.node_id WHERE v.enabled=1 ORDER BY v.sort, v.id""")
    return [vless_link(u, r, {"host": r["n_host"], "country_code": r["n_cc"]}) for r in rows]


def fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.2f} {unit}"
        n /= 1024


def user_lang(u: dict | None) -> str:
    from .db import get_setting
    lang = (u or {}).get("lang") or get_setting("lang", "ru") or "ru"
    return "en" if lang == "en" else "ru"


def status_text(u: dict, lang: str = "ru") -> str:
    st = user_status(u)
    return (STATUS_TEXT_EN if lang == "en" else STATUS_TEXT)[st]


def expires_text(u: dict, lang: str = "ru") -> str:
    if u.get("forever"):
        return "Lifetime" if lang == "en" else "Бессрочно"
    return fmt_date(u["expires_at"])


def fmt_date(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


def user_public(u: dict) -> dict:
    d = dict(u)
    d["status"] = user_status(u)
    d["status_text"] = STATUS_TEXT[d["status"]]
    d["online_ips"] = json.loads(u.get("online_ips") or "[]")
    d["used"] = u["used_up"] + u["used_down"]
    d["phone_fmt"] = fmt_phone(u.get("phone") or "")
    d["expires_text"] = "Бессрочно" if u.get("forever") else fmt_date(u["expires_at"])
    d["page_url"] = user_page_url(u)
    d["sub_url"] = sub_url(u)
    return d
