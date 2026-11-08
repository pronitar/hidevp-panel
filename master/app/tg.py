import base64
import http.client
import json
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request

from . import nodes
from .db import get_setting, q1

API_HOST = "api.telegram.org"


class TelegramError(Exception):
    pass


class TransportError(Exception):
    pass


def _socks5_connect(proxy: urllib.parse.SplitResult, host: str, port: int, timeout: float) -> socket.socket:
    s = socket.create_connection((proxy.hostname, proxy.port or 1080), timeout=timeout)
    try:
        user = urllib.parse.unquote(proxy.username or "")
        pw = urllib.parse.unquote(proxy.password or "")
        s.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        _ver, method = s.recv(2)
        if method == 0x02:
            s.sendall(b"\x01" + bytes([len(user)]) + user.encode() + bytes([len(pw)]) + pw.encode())
            if s.recv(2)[1:2] != b"\x00":
                raise TransportError("SOCKS5: неверный логин или пароль прокси")
        elif method != 0x00:
            raise TransportError("SOCKS5: прокси не принял способ авторизации")
        h = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        rep = s.recv(4)
        if len(rep) < 2 or rep[1] != 0:
            raise TransportError(f"SOCKS5: прокси не смог подключиться к {host} (код {rep[1] if len(rep) > 1 else '?'})")
        atyp = rep[3]
        skip = {1: 4, 4: 16}.get(atyp)
        if skip is None:
            skip = s.recv(1)[0]
        s.recv(skip + 2)
        return s
    except Exception:
        s.close()
        raise


class _SocksHTTPS(http.client.HTTPSConnection):
    def __init__(self, proxy, *a, **kw):
        self._proxy = proxy
        super().__init__(*a, **kw)

    def connect(self):
        raw = _socks5_connect(self._proxy, self.host, self.port, self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)


def _post_direct(path: str, body: bytes, timeout: float, proxy: str = "", host: str = API_HOST,
                 headers: dict | None = None) -> tuple[int, bytes]:
    hdrs = {"Content-Type": "application/json", "User-Agent": "hidevp-panel/1.0 (+https://github.com/pronitar/hidevp-panel)",
            **(headers or {})}
    if proxy.startswith(("socks5://", "socks5h://", "socks://")):
        p = urllib.parse.urlsplit(proxy)
        conn = _SocksHTTPS(p, host, 443, timeout=timeout)
        try:
            conn.request("POST", path, body, hdrs)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()
    handlers = [urllib.request.ProxyHandler({"https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(f"https://{host}{path}", data=body, headers=hdrs)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def route() -> dict:
    mode = get_setting("tg_route", "direct") or "direct"
    if mode == "node":
        node = q1("SELECT * FROM nodes WHERE id=?", (int(get_setting("tg_node_id", "0") or 0),))
        if not node:
            raise TransportError("Сервер для связи с Telegram не выбран или удалён (Настройки → Telegram-бот)")
        return {"mode": "node", "node": node}
    if mode == "proxy":
        proxy = get_setting("tg_proxy", "").strip()
        if not proxy:
            raise TransportError("Не указан адрес прокси для Telegram")
        return {"mode": "proxy", "proxy": proxy}
    return {"mode": "direct"}


def describe() -> str:
    try:
        r = route()
    except TransportError as e:
        return str(e)
    if r["mode"] == "node":
        return f"через сервер «{r['node']['name']}»"
    if r["mode"] == "proxy":
        p = urllib.parse.urlsplit(r["proxy"])
        return f"через прокси {p.scheme}://{p.hostname}:{p.port or ''}"
    return "напрямую"


def post(path: str, body: bytes, timeout: float = 20, host: str = API_HOST, headers: dict | None = None) -> tuple[int, bytes]:
    r = route()
    try:
        if r["mode"] == "node":
            req = {"path": path, "body": base64.b64encode(body).decode(), "timeout": timeout}
            if host != API_HOST:
                req.update(host=host, headers=headers or {})
            res = nodes._call(r["node"], "POST", "/api/tg", req, timeout=timeout + 15)
            if "status" not in res:
                raise TransportError("Ответ узла не распознан")
            return res["status"], base64.b64decode(res["body"])
        return _post_direct(path, body, timeout, r.get("proxy", ""), host, headers)
    except nodes.NodeError as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            msg = "на узле старая версия work-панели — обновите её (install-node.sh)"
        raise TransportError(f"Узел «{r['node']['name']}»: {msg}")
    except TransportError:
        raise
    except Exception as e:
        raise TransportError(f"Нет связи с Telegram ({describe()}): {e}")


def call(token: str, method: str, params: dict, timeout: float = 20) -> dict:
    status, raw = post(f"/bot{token}/{method}", json.dumps(params).encode(), timeout)
    try:
        res = json.loads(raw)
    except Exception:
        raise TransportError(f"Telegram вернул не JSON (HTTP {status})")
    if not res.get("ok"):
        raise TelegramError(res.get("description", f"HTTP {status}"))
    return res["result"]
