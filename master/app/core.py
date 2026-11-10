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


PROTOCOLS = ("vless", "trojan", "ss", "hy2")
REALITY_PROTOCOLS = ("vless", "trojan")
SS_METHOD = "2022-blake3-aes-128-gcm"
HY2_MIN_XRAY = "26.3.27"


def ss_server_key() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode()


def ss_user_key(server_key: str, user_uuid: str) -> str:
    return base64.b64encode(hmac.new(server_key.encode(), user_uuid.encode(), hashlib.sha256).digest()[:16]).decode()


def self_signed_cert(name: str) -> tuple[str, str, str]:
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    pin = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


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


def _host(node: dict) -> str:
    host = node["host"]
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _reality_params(vpn: dict) -> dict:
    p = {"type": vpn["transport"], "security": "reality", "pbk": vpn["public_key"], "fp": vpn["fingerprint"],
         "sni": vpn["sni"], "sid": vpn["short_id"], "spx": "/"}
    if vpn["transport"] == "grpc":
        p["serviceName"] = vpn["service_name"] or "grpc"
        p["mode"] = "gun"
    elif vpn["transport"] == "xhttp":
        p["path"] = vpn["path"] or "/"
        p["mode"] = "auto"
    return p


def vpn_link(u: dict, vpn: dict, node: dict) -> str:
    proto = vpn.get("protocol") or "vless"
    host, port = _host(node), vpn["port"]
    title = quote(f"{flag(node['country_code'])} {vpn['name']}")
    if proto == "trojan":
        return f"trojan://{quote(u['uuid'])}@{host}:{port}?{urlencode(_reality_params(vpn), quote_via=quote)}#{title}"
    if proto == "ss":
        password = f"{vpn['server_key']}:{ss_user_key(vpn['server_key'], u['uuid'])}"
        info = base64.urlsafe_b64encode(f"{SS_METHOD}:{password}".encode()).decode().rstrip("=")
        return f"ss://{info}@{host}:{port}#{title}"
    if proto == "hy2":
        p = {"sni": vpn["sni"], "insecure": "1", "pinSHA256": vpn["cert_pin"]}
        if vpn.get("obfs"):
            p.update({"obfs": "salamander", "obfs-password": vpn["obfs"]})
        return f"hy2://{quote(u['uuid'])}@{host}:{port}/?{urlencode(p, quote_via=quote)}#{title}"
    p = {"encryption": "none", **_reality_params(vpn)}
    if vpn["transport"] == "tcp":
        p["flow"] = "xtls-rprx-vision"
    return f"vless://{u['uuid']}@{host}:{port}?{urlencode(p, quote_via=quote)}#{title}"


def _vpn_rows():
    return q("""SELECT v.*, n.host AS n_host, n.country_code AS n_cc FROM vpns v
                JOIN nodes n ON n.id=v.node_id WHERE v.enabled=1 ORDER BY v.sort, v.id""")


def user_links(u: dict) -> list[str]:
    return [vpn_link(u, r, {"host": r["n_host"], "country_code": r["n_cc"]}) for r in _vpn_rows()]


AUTO_PROBE = "https://www.gstatic.com/generate_204"


def auto_select_on() -> bool:
    return get_setting("auto_select", "0") == "1"


def _client_stream(vpn: dict) -> dict:
    t = vpn["transport"] or "tcp"
    stream = {"network": t, "security": "reality",
              "realitySettings": {"serverName": vpn["sni"], "fingerprint": vpn["fingerprint"] or "chrome",
                                  "publicKey": vpn["public_key"], "shortId": vpn["short_id"], "spiderX": "/"}}
    if t == "grpc":
        stream["grpcSettings"] = {"serviceName": vpn["service_name"] or "grpc", "multiMode": False}
    elif t == "xhttp":
        stream["xhttpSettings"] = {"path": vpn["path"] or "/", "mode": "auto"}
    return stream


def client_outbound(u: dict, vpn: dict, host: str, tag: str) -> dict:
    proto = vpn.get("protocol") or "vless"
    port = int(vpn["port"])
    if proto == "ss":
        return {"tag": tag, "protocol": "shadowsocks", "settings": {"servers": [{
            "address": host, "port": port, "method": SS_METHOD,
            "password": f"{vpn['server_key']}:{ss_user_key(vpn['server_key'], u['uuid'])}"}]}}
    if proto == "hy2":
        stream = {"network": "hysteria", "security": "tls",
                  "tlsSettings": {"serverName": vpn["sni"], "alpn": ["h3"],
                                  "pinnedPeerCertSha256": vpn["cert_pin"]},
                  "hysteriaSettings": {"version": 2, "auth": u["uuid"]}}
        if vpn.get("obfs"):
            stream["finalmask"] = {"udp": [{"type": "salamander", "settings": {"password": vpn["obfs"]}}]}
        return {"tag": tag, "protocol": "hysteria", "settings": {"version": 2, "address": host, "port": port},
                "streamSettings": stream}
    if proto == "trojan":
        return {"tag": tag, "protocol": "trojan",
                "settings": {"servers": [{"address": host, "port": port, "password": u["uuid"]}]},
                "streamSettings": _client_stream(vpn)}
    user = {"id": u["uuid"], "encryption": "none"}
    if (vpn["transport"] or "tcp") == "tcp":
        user["flow"] = "xtls-rprx-vision"
    return {"tag": tag, "protocol": "vless", "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
            "streamSettings": _client_stream(vpn)}


def _client_config(remarks: str, proxies: list[dict], balanced: bool, description: str = "") -> dict:
    rules = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]
    cfg = {
        "remarks": remarks,
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
             "settings": {"udp": True, "auth": "noauth"},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}},
            {"tag": "http", "listen": "127.0.0.1", "port": 10809, "protocol": "http", "settings": {}},
        ],
        "outbounds": proxies + [{"tag": "direct", "protocol": "freedom"},
                                {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
    if description:
        cfg["meta"] = {"serverDescription": description}
    if balanced:
        cfg["burstObservatory"] = {"subjectSelector": ["proxy-"], "pingConfig": {
            "destination": AUTO_PROBE, "connectivity": "", "interval": "1m", "sampling": 3, "timeout": "5s"}}
        cfg["routing"]["balancers"] = [{"tag": "auto", "selector": ["proxy-"],
                                        "strategy": {"type": "leastPing"}, "fallbackTag": proxies[0]["tag"]}]
        rules.append({"type": "field", "network": "tcp,udp", "balancerTag": "auto"})
        cfg["stats"] = {}
    else:
        rules.append({"type": "field", "network": "tcp,udp", "outboundTag": proxies[0]["tag"]})
    return cfg


def user_json(u: dict, lang: str = "ru") -> list[dict]:
    rows = _vpn_rows()
    out = []
    auto = [r for r in rows if (r.get("protocol") or "vless") != "hy2"]
    if auto and auto_select_on():
        proxies = [client_outbound(u, r, _host({"host": r["n_host"]}).strip("[]"), f"proxy-{i + 1}")
                   for i, r in enumerate(auto)]
        out.append(_client_config("⚡ Auto" if lang == "en" else "⚡ Автовыбор", proxies, True,
                                  "Fastest available server" if lang == "en" else "Самый быстрый доступный сервер"))
    for r in rows:
        name = f"{flag(r['n_cc'])} {r['name']}"
        out.append(_client_config(name, [client_outbound(u, r, _host({"host": r["n_host"]}).strip("[]"), "proxy")],
                                  False))
    return out


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
