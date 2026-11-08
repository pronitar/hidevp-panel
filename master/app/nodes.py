import asyncio
import base64
import hashlib
import json
import socket
import ssl
import urllib.request

_ctx_cache: dict[int, ssl.SSLContext] = {}


class NodeError(Exception):
    pass


def parse_key(key: str) -> dict:
    key = key.strip()
    if key.startswith("NODE-"):
        key = key[5:]
    try:
        data = json.loads(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))
        return {"host": data["h"], "port": int(data["p"]), "api_key": data["k"], "fp": data["f"]}
    except Exception:
        raise NodeError("Неверный формат ключа. Скопируйте ключ целиком (начинается с NODE-).")


def fetch_cert(host: str, port: int, fp: str) -> str:
    try:
        pem = ssl.get_server_certificate((host, port), timeout=10)
    except Exception as e:
        raise NodeError(f"Не удалось подключиться к {host}:{port} — {e}")
    real = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if real != fp.lower():
        raise NodeError("Отпечаток сертификата узла не совпадает с ключом — подключение отклонено.")
    return pem


def _ctx(node: dict) -> ssl.SSLContext:
    c = _ctx_cache.get(node["id"])
    if c is None:
        c = ssl.create_default_context(cadata=node["cert_pem"])
        c.check_hostname = False
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            c.verify_flags &= ~ssl.VERIFY_X509_STRICT
        _ctx_cache[node["id"]] = c
    return c


def forget(node_id: int):
    _ctx_cache.pop(node_id, None)


def _call(node: dict, method: str, path: str, body=None, timeout=25):
    host = node["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://{host}:{node['port']}{path}", data=data, method=method,
                                 headers={"X-API-Key": node["api_key"],
                                          "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=_ctx(node)))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise NodeError(msg)
    except Exception as e:
        raise NodeError(str(e))


async def call(node: dict, method: str, path: str, body=None, timeout=25):
    return await asyncio.to_thread(_call, node, method, path, body, timeout)


def resolve_ip(host: str) -> str:
    try:
        return socket.getaddrinfo(host, None)[0][4][0]
    except Exception:
        return host


def _geo(ip: str) -> dict:
    url = f"http://ip-api.com/json/{ip}?fields=status,countryCode,country,city&lang=ru"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read())
        if d.get("status") == "success":
            return {"country_code": d.get("countryCode", ""), "country": d.get("country", ""),
                    "city": d.get("city", "")}
    except Exception:
        pass
    return {}


async def geo(host: str) -> dict:
    ip = await asyncio.to_thread(resolve_ip, host)
    d = await asyncio.to_thread(_geo, ip)
    d["ip"] = ip
    return d
