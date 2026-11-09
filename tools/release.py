#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def canon(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_key(raw: str) -> Ed25519PrivateKey:
    raw = raw.strip()
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None)
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))


def pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def keygen():
    key = Ed25519PrivateKey.generate()
    secret = base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                serialization.NoEncryption())).decode()
    print("HIDEVP_RELEASE_KEY (secret):")
    print(secret)
    print("public key:")
    print(pub_b64(key))


def build(zip_path: str, notes: str, out_dir: Path, key: Ed25519PrivateKey) -> dict:
    pub = pub_b64(key)
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(td)
        vers = list(Path(td).rglob("VERSION"))
        if not vers:
            sys.exit("В архиве нет файла VERSION")
        root = vers[0].parent
        version = (root / "VERSION").read_text().strip()
        agent = re.search(r'^VERSION = "(.+?)"', (root / "node" / "agent.py").read_text(), re.M)
        (root / "master" / "app" / "release_key.pub").write_text(pub + "\n")
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"hidevp-panel-{version}.zip"
        out = out_dir / name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    z.write(f, "hidevp-panel/" + str(f.relative_to(root)))
    data = out.read_bytes()
    manifest = {"app": "hidevp-panel", "version": version, "agent_version": agent.group(1) if agent else "",
                "file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "created": int(time.time()), "notes": notes.strip()[:2000]}
    manifest["signature"] = base64.b64encode(key.sign(canon(manifest))).decode()
    (out_dir / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"manifest": manifest, "pubkey": pub}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip")
    ap.add_argument("--notes-file", default="")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--expect-version", default="")
    ap.add_argument("--keygen", action="store_true")
    a = ap.parse_args()
    if a.keygen:
        return keygen()
    if not a.zip:
        sys.exit("--zip required")
    raw = os.environ.get("HIDEVP_RELEASE_KEY", "")
    if not raw.strip():
        sys.exit("Секрет HIDEVP_RELEASE_KEY не задан: Settings → Secrets and variables → Actions → New repository secret")
    notes = Path(a.notes_file).read_text(encoding="utf-8") if a.notes_file and Path(a.notes_file).exists() else ""
    res = build(a.zip, notes, Path(a.out), load_key(raw))
    m = res["manifest"]
    if a.expect_version and a.expect_version.lstrip("v") != m["version"]:
        sys.exit(f"Тег {a.expect_version} не совпадает с файлом VERSION ({m['version']}) — исправьте VERSION или тег")
    shutil.copyfile(ROOT / "get.sh", Path(a.out) / "install.sh")
    print(f"version={m['version']}\nagent={m['agent_version']}\nsha256={m['sha256']}\npubkey={res['pubkey']}")


if __name__ == "__main__":
    main()
