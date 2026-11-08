import argparse
import base64
import hashlib
import json
import os
import secrets
import ssl
from pathlib import Path

BASE = Path(os.environ.get("NODE_HOME", "/opt/hidevp-panel/node"))
DATA = BASE / "data"
CONF = DATA / "node.json"
CERT = DATA / "cert.pem"


def fingerprint() -> str:
    der = ssl.PEM_cert_to_DER_cert(CERT.read_text())
    return hashlib.sha256(der).hexdigest()


def connection_key(conf: dict) -> str:
    payload = {"h": conf["host"], "p": conf["port"], "k": conf["api_key"], "f": fingerprint()}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return "NODE-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--host", required=True)
    i.add_argument("--port", type=int, required=True)
    sub.add_parser("key")
    sub.add_parser("rotate")
    a = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    if a.cmd == "init":
        conf = json.loads(CONF.read_text()) if CONF.exists() else {}
        conf.update(host=a.host, port=a.port)
        conf.setdefault("api_key", secrets.token_urlsafe(32))
        CONF.write_text(json.dumps(conf, indent=2))
        os.chmod(CONF, 0o600)
        print(connection_key(conf))
    elif a.cmd == "rotate":
        conf = json.loads(CONF.read_text())
        conf["api_key"] = secrets.token_urlsafe(32)
        CONF.write_text(json.dumps(conf, indent=2))
        print(connection_key(conf))
    else:
        print(connection_key(json.loads(CONF.read_text())))


if __name__ == "__main__":
    main()
