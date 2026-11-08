#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PANEL_HOME", tempfile.mkdtemp())
sys.path.insert(0, str(ROOT / "master"))
from app import update


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--notes-file", default="")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--expect-version", default="")
    a = ap.parse_args()
    raw = os.environ.get("HIDEVP_RELEASE_KEY", "")
    if not raw.strip():
        sys.exit("Секрет HIDEVP_RELEASE_KEY не задан: Settings → Secrets and variables → Actions → New repository secret")
    key = update.key_from_b64(raw)
    notes = Path(a.notes_file).read_text(encoding="utf-8") if a.notes_file and Path(a.notes_file).exists() else ""
    res = update.publish(a.zip, notes, out_dir=Path(a.out), key=key)
    m = res["manifest"]
    if a.expect_version and a.expect_version.lstrip("v") != m["version"]:
        sys.exit(f"Тег {a.expect_version} не совпадает с файлом VERSION ({m['version']}) — исправьте VERSION или тег")
    shutil.copyfile(ROOT / "get.sh", Path(a.out) / "install.sh")
    print(f"version={m['version']}\nagent={m['agent_version']}\nsha256={m['sha256']}\npubkey={res['pubkey']}")


if __name__ == "__main__":
    main()
