#!/usr/bin/env python3
import base64
import hashlib
import json
import re
import sys
import urllib.request

VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")


def _json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hidevp-panel"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read(5 * 1024 * 1024))


def list_versions(urls):
    found = {}
    for url in urls:
        try:
            if url.startswith("https://github.com/") and "/releases/" in url:
                repo = "/".join(url.split("/")[3:5])
                for r in _json(f"https://api.github.com/repos/{repo}/releases?per_page=30"):
                    v = (r.get("tag_name") or "").lstrip("vV")
                    a = [x for x in r.get("assets") or [] if x.get("name") == "latest.json"]
                    if not r.get("draft") and not r.get("prerelease") and a and VERSION_RE.fullmatch(v):
                        found.setdefault(v, a[0]["browser_download_url"])
        except Exception:
            continue
    key = lambda v: tuple(int(x) for x in v.split("."))
    for v in sorted(found, key=key, reverse=True):
        print(v, found[v])
    return 0


def main():
    if sys.argv[1:2] == ["--list"]:
        return list_versions(sys.argv[2:])
    manifest, pub, pkg = sys.argv[1:4]
    m = json.load(open(manifest, encoding="utf-8"))
    if hashlib.sha256(open(pkg, "rb").read()).hexdigest() != m.get("sha256"):
        print("sha256 mismatch"); return 1
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        print("no cryptography module"); return 2
    body = json.dumps({k: v for k, v in m.items() if k != "signature"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode()
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(open(pub).read().strip()))
        key.verify(base64.b64decode(m.get("signature", "")), body)
    except (InvalidSignature, ValueError):
        print("bad signature"); return 1
    print("ok"); return 0


if __name__ == "__main__":
    sys.exit(main())
