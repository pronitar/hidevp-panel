import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .db import DATA, get_setting, set_setting

log = logging.getLogger("update")

GITHUB_URL = "https://github.com/pronitar/hidevp-panel/releases/latest/download/latest.json"
MIRROR_URL = "https://hidevp.ru/updates/latest.json"
DEFAULT_UPDATE_URLS = [GITHUB_URL, MIRROR_URL]
APP_DIR = Path(__file__).resolve().parent
PUBKEY_FILE = APP_DIR / "release_key.pub"
RELEASES = DATA / "releases"
DOWNLOADS = DATA / "updates"
ROLLBACK = APP_DIR.parents[1] / "rollback"
KEEP_PACKAGES = 5
VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")
KEY_DIR = Path(os.environ.get("HIDEVP_RELEASE_KEYS", "/root/.hidevp-release"))
CHECK_EVERY = 6 * 3600
UPDATE_LOG = Path("/var/log/hidevp-update.log")

_RELEASES = {"at": 0, "items": [], "error": ""}
STATE = {"checked_at": 0, "latest": None, "error": "", "available": False, "updating": "", "updating_since": 0,
         "source": ""}


class UpdateError(Exception):
    pass


def vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3]) or (0,)


def root_dir() -> Path:
    r = APP_DIR.parents[1]
    return r / "src" if (r / "src" / "VERSION").exists() else r


def installed_version() -> str:
    p = root_dir() / "VERSION"
    return p.read_text().strip() if p.exists() else "0"


def update_urls() -> list[str]:
    custom = (get_setting("update_url") or "").strip()
    return [custom] if custom else list(DEFAULT_UPDATE_URLS)


def update_url() -> str:
    return update_urls()[0]


def _canon(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_key() -> Ed25519PublicKey | None:
    if not PUBKEY_FILE.exists():
        return None
    raw = base64.b64decode(PUBKEY_FILE.read_text().strip())
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_manifest(m: dict):
    pk = public_key()
    if pk is None:
        raise UpdateError("В этой установке нет ключа подписи релизов — обновите панель вручную один раз")
    try:
        pk.verify(base64.b64decode(m.get("signature", "")), _canon(m))
    except (InvalidSignature, ValueError):
        raise UpdateError("Подпись обновления не прошла проверку — обновление отклонено")
    if m.get("app") != "hidevp-panel":
        raise UpdateError("Неверный манифест обновления")


def key_from_b64(raw: str) -> Ed25519PrivateKey:
    raw = raw.strip()
    if raw.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(raw.encode(), password=None)
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))


def key_to_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                              serialization.NoEncryption())).decode()


def _load_or_create_key() -> Ed25519PrivateKey:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    kf = KEY_DIR / "ed25519.key"
    if kf.exists():
        return serialization.load_pem_private_key(kf.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    os.chmod(kf, 0o600)
    return key


def _pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def publish(zip_path: str, notes: str = "", out_dir: Path | None = None, key: Ed25519PrivateKey | None = None) -> dict:
    if key is None:
        key = _load_or_create_key()
        (KEY_DIR / "ed25519.pub").write_text(_pub_b64(key) + "\n")
    pub = _pub_b64(key)
    out_dir = Path(out_dir) if out_dir else RELEASES
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(td)
        vers = list(Path(td).rglob("VERSION"))
        if not vers:
            raise UpdateError("В архиве нет файла VERSION — это не пакет HideVP")
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
    manifest["signature"] = base64.b64encode(key.sign(_canon(manifest))).decode()
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    (out_dir / "latest.json").write_text(text)
    (out_dir / _manifest_name(version)).write_text(text)
    _keep_last(out_dir)
    return {"manifest": manifest, "pubkey": pub, "package": str(out)}


def _manifest_name(version: str) -> str:
    return f"hidevp-panel-{version}.json"


def _keep_last(out_dir: Path, n: int = KEEP_PACKAGES):
    zips = sorted(out_dir.glob("hidevp-panel-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[n:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def releases_index(out_dir: Path = RELEASES) -> dict:
    out = []
    for f in out_dir.glob("hidevp-panel-*.json"):
        try:
            m = json.loads(f.read_text())
            if (out_dir / m["file"]).exists():
                out.append({"version": m["version"], "agent_version": m.get("agent_version", ""), "created": m.get("created", 0)})
        except (ValueError, KeyError, OSError):
            continue
    return {"app": "hidevp-panel", "releases": sorted(out, key=lambda r: vt(r["version"]), reverse=True)}


def _get(url: str, timeout=20, limit=60 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hidevp-panel/" + installed_version()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("Файл обновления слишком большой")
    return data


def check() -> dict:
    errors = []
    for url in update_urls():
        try:
            m = json.loads(_get(url, timeout=15))
            verify_manifest(m)
            STATE.update(latest=m, error="", source=url, available=vt(m["version"]) > vt(installed_version()))
            break
        except UpdateError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {e}")
    else:
        own = [e for e in errors if e.startswith(("В этой", "Подпись", "Неверный"))]
        STATE.update(error=own[0] if own else "Не удалось проверить обновления: " + "; ".join(errors))
    STATE["checked_at"] = time.time()
    return state()


def state() -> dict:
    m = STATE.get("latest") or {}
    st = {k: v for k, v in STATE.items() if k != "latest"}
    st.update(installed=installed_version(), latest_version=m.get("version", ""), latest_agent=m.get("agent_version", ""),
              notes=m.get("notes", ""), created=m.get("created", 0), size=m.get("size", 0),
              url=update_url(), custom_url=(get_setting("update_url") or "").strip(), default_url=GITHUB_URL,
              source=STATE.get("source", ""), signed=public_key() is not None,
              available=bool(m) and vt(m.get("version")) > vt(installed_version()))
    if st["updating"] and time.time() - STATE["updating_since"] > 600:
        STATE["updating"] = ""
        st["updating"] = ""
    return st


def _fetch_manifest(url: str, version: str = "") -> dict | None:
    try:
        m = json.loads(_get(url, timeout=15))
        verify_manifest(m)
        if version and m.get("version") != version:
            return None
        return m
    except Exception as e:
        log.info("manifest %s: %s", url, e)
        return None


def _remote_candidates() -> tuple[list[tuple[str, str]], list[str]]:
    found, errors = [], []
    for url in update_urls():
        base = url.rsplit("/", 1)[0]
        try:
            if url.startswith("https://github.com/") and "/releases/" in url:
                repo = "/".join(url.split("/")[3:5])
                data = json.loads(_get(f"https://api.github.com/repos/{repo}/releases?per_page=30", timeout=15))
                for r in data:
                    v = (r.get("tag_name") or "").lstrip("vV")
                    asset = next((a for a in r.get("assets") or [] if a.get("name") == "latest.json"), None)
                    if not r.get("draft") and asset and VERSION_RE.fullmatch(v):
                        found.append((v, asset["browser_download_url"]))
            else:
                data = json.loads(_get(f"{base}/releases.json", timeout=15))
                for r in data.get("releases") or []:
                    v = str(r.get("version") or "")
                    if VERSION_RE.fullmatch(v):
                        found.append((v, f"{base}/{_manifest_name(v)}"))
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {e}")
    return found, errors


def _cached() -> dict:
    out = {}
    for j in DOWNLOADS.glob("hidevp-panel-*.json"):
        try:
            m = json.loads(j.read_text())
            verify_manifest(m)
            z = DOWNLOADS / Path(m["file"]).name
            if z.exists() and z.stat().st_size == m.get("size", z.stat().st_size):
                out[m["version"]] = (z, m)
        except Exception:
            continue
    return out


def _tar_versions(path: Path) -> tuple[str, str]:
    with tarfile.open(path) as t:
        ver = agent = ""
        for mem in t.getmembers():
            parts = Path(mem.name).parts
            if len(parts) == 2 and parts[1] == "VERSION":
                ver = t.extractfile(mem).read().decode().strip()
            elif parts[1:] == ("node", "agent.py"):
                m = re.search(r'^VERSION = "(.+?)"', t.extractfile(mem).read().decode(errors="ignore"), re.M)
                agent = m.group(1) if m else ""
        return ver, agent


def local_copies() -> dict:
    out = {}
    for f in ROLLBACK.glob("hidevp-panel-*.tar.gz"):
        try:
            ver, agent = _tar_versions(f)
            if VERSION_RE.fullmatch(ver):
                out[ver] = {"path": f, "agent_version": agent, "created": int(f.stat().st_mtime)}
        except (OSError, tarfile.TarError, IndexError, AttributeError):
            continue
    return out


def releases(force: bool = False) -> dict:
    if not force and time.time() - _RELEASES["at"] < 600:
        return {"items": _RELEASES["items"], "error": _RELEASES["error"]}
    found, errors = _remote_candidates()
    items = {}
    latest = STATE.get("latest")
    if latest:
        items[latest["version"]] = {"version": latest["version"], "agent_version": latest.get("agent_version", ""),
                                    "created": latest.get("created", 0), "notes": latest.get("notes", ""), "sources": ["online"]}
    todo, seen = [], set(items)
    for v, url in sorted(found, key=lambda x: vt(x[0]), reverse=True):
        if v not in seen and len(todo) < 15:
            seen.add(v)
            todo.append((v, url))
    with ThreadPoolExecutor(6) as pool:
        for m in pool.map(lambda t: _fetch_manifest(t[1], t[0]), todo):
            if m:
                items.setdefault(m["version"], {"version": m["version"], "agent_version": m.get("agent_version", ""),
                                                "created": m.get("created", 0), "notes": m.get("notes", ""), "sources": ["online"]})
    for v, (_, m) in _cached().items():
        it = items.setdefault(v, {"version": v, "agent_version": m.get("agent_version", ""), "created": m.get("created", 0),
                                  "notes": m.get("notes", ""), "sources": []})
        it["sources"].append("cache")
    for v, c in local_copies().items():
        it = items.setdefault(v, {"version": v, "agent_version": c["agent_version"], "created": c["created"], "notes": "",
                                  "sources": []})
        it["sources"].append("local")
    out = sorted(items.values(), key=lambda i: vt(i["version"]), reverse=True)
    _RELEASES.update(at=time.time(), items=out, error="" if out or not errors else "; ".join(errors))
    return {"items": out, "error": _RELEASES["error"]}


def _download(m: dict, bases: list[str]) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    path = DOWNLOADS / Path(m["file"]).name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == m["sha256"]:
        return path
    data = None
    for base in filter(None, bases):
        try:
            data = _get(f"{base}/{m['file']}", timeout=180)
            if hashlib.sha256(data).hexdigest() == m["sha256"]:
                break
        except Exception as e:
            log.warning("download %s: %s", base, e)
    if data is None:
        raise UpdateError("Не удалось скачать пакет обновления")
    if hashlib.sha256(data).hexdigest() != m["sha256"]:
        raise UpdateError("Контрольная сумма пакета не совпала — обновление отклонено")
    path.write_bytes(data)
    path.with_suffix(".json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    _keep_last(DOWNLOADS)
    return path


def package(version: str | None = None) -> Path:
    m = STATE.get("latest")
    if not m and not version:
        check()
        m = STATE.get("latest")
    if not version:
        if not m:
            raise UpdateError(STATE.get("error") or "Нет информации об обновлении")
        version = m["version"]
    if not VERSION_RE.fullmatch(version or ""):
        raise UpdateError("Неверная версия")
    cached = _cached().get(version)
    if cached:
        return cached[0]
    if m and m["version"] == version:
        src = STATE.get("source")
        return _download(m, [u.rsplit("/", 1)[0] for u in [src] + [x for x in update_urls() if x != src] if u])
    local = local_copies().get(version)
    if local:
        return local["path"]
    for v, url in _remote_candidates()[0]:
        if v == version:
            mm = _fetch_manifest(url, version)
            if mm:
                return _download(mm, [url.rsplit("/", 1)[0]])
    raise UpdateError(f"Версия {version} недоступна для установки")


def _safe_names(names):
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts:
            raise UpdateError("Небезопасный путь в пакете")


def _extract(pkg: Path) -> tuple[Path, Path]:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dst = Path(tempfile.mkdtemp(prefix="src-", dir=DOWNLOADS))
    try:
        if pkg.name.endswith(".tar.gz"):
            with tarfile.open(pkg) as t:
                members = [mm for mm in t.getmembers() if mm.isfile() or mm.isdir()]
                _safe_names(mm.name for mm in members)
                t.extractall(dst, members=members)
        else:
            with zipfile.ZipFile(pkg) as z:
                _safe_names(z.namelist())
                z.extractall(dst)
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        raise
    roots = [p.parent for p in dst.rglob("install-master.sh")]
    if not roots:
        shutil.rmtree(dst, ignore_errors=True)
        raise UpdateError("В пакете нет установщика")
    return dst, roots[0]


def target_check(target: str, current: str, rollback: bool):
    if not VERSION_RE.fullmatch(target or ""):
        raise UpdateError("Неверная версия")
    if not target or vt(target) == vt(current):
        raise UpdateError("Эта версия уже установлена")
    if vt(target) < vt(current) and not rollback:
        raise UpdateError("Это более старая версия — для отката подтвердите риски")


def start_master_update(version: str | None = None, rollback: bool = False) -> dict:
    if STATE["updating"]:
        raise UpdateError("Обновление уже выполняется")
    if not version:
        m = STATE.get("latest")
        if not m or vt(m["version"]) <= vt(installed_version()):
            raise UpdateError("Нет информации об обновлении")
        version = m["version"]
    target_check(version, installed_version(), rollback)
    pkg = package(version)
    from . import backup
    try:
        backup.auto_backup("before-rollback" if vt(version) < vt(installed_version()) else "before-update")
    except Exception as e:
        log.warning("backup before update: %s", e)
    tmp, src = _extract(pkg)
    lf = APP_DIR.parents[1] / "lang"
    lang = "en" if lf.exists() and lf.read_text().strip() == "en" else "ru"
    node = (f"HV_LANG={lang} bash install-node.sh </dev/null >> {UPDATE_LOG} 2>&1; "
            if (APP_DIR.parents[1] / "node" / "agent.py").exists() else "")
    script = (f"cd {src} && : > {UPDATE_LOG} && {node}HV_LANG={lang} DOMAIN= bash install-master.sh </dev/null >> {UPDATE_LOG} 2>&1; "
              f"rm -rf {tmp}")
    r = subprocess.run(["systemd-run", "--unit", f"hidevp-update-{int(time.time())}", "--collect",
                        "--property=Type=oneshot", "bash", "-c", script], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError("Не удалось запустить обновление: " + (r.stderr or r.stdout).strip()[-300:])
    STATE.update(updating=version, updating_since=time.time())
    return {"started": True, "version": version}


def agent_payload(version: str | None = None) -> dict:
    pkg = package(version)
    if pkg.name.endswith(".tar.gz"):
        buf = io.BytesIO()
        with tarfile.open(pkg) as t, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for mm in t.getmembers():
                if mm.isfile():
                    z.writestr(mm.name, t.extractfile(mm).read())
        data = buf.getvalue()
    else:
        data = pkg.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next((n for n in z.namelist() if n.endswith("node/agent.py")), None)
        mm = re.search(r'^VERSION = "(.+?)"', z.read(name).decode(errors="ignore"), re.M) if name else None
    return {"data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
            "version": mm.group(1) if mm else ""}


def read_log(lines=40) -> str:
    try:
        return "\n".join(UPDATE_LOG.read_text(errors="ignore").splitlines()[-lines:])
    except OSError:
        return ""


def mirror_sync():
    if get_setting("update_mirror") != "1":
        return
    m = STATE.get("latest")
    if not m or not STATE.get("source", "").startswith("https://github.com/"):
        return
    cur = RELEASES / "latest.json"
    try:
        if cur.exists() and json.loads(cur.read_text()).get("sha256") == m["sha256"]:
            return
    except ValueError:
        pass
    pkg = package()
    RELEASES.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pkg, RELEASES / pkg.name)
    text = json.dumps(m, ensure_ascii=False, indent=2)
    (RELEASES / _manifest_name(m["version"])).write_text(text)
    tmp = RELEASES / "latest.json.tmp"
    tmp.write_text(text)
    tmp.replace(cur)
    _keep_last(RELEASES)
    log.info("mirror: %s", m["version"])


async def loop(notify):
    await asyncio.sleep(30)
    while True:
        try:
            st = await asyncio.to_thread(check)
            try:
                await asyncio.to_thread(mirror_sync)
            except Exception:
                log.exception("mirror")
            if st["available"] and get_setting("update_notified") != st["latest_version"]:
                set_setting("update_notified", st["latest_version"])
                await notify(st)
        except Exception:
            log.exception("update check")
        await asyncio.sleep(CHECK_EVERY)
