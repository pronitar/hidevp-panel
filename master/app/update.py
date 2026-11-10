import asyncio
import base64
import hashlib
import io
import json
import logging
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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .db import DATA, get_setting, set_setting

log = logging.getLogger("update")

GITHUB_REPO = "pronitar/hidevp-panel"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}/releases/latest/download/latest.json"
APP_DIR = Path(__file__).resolve().parent
PUBKEY_FILE = APP_DIR / "release_key.pub"
DOWNLOADS = DATA / "updates"
ROLLBACK = APP_DIR.parents[1] / "rollback"
KEEP_PACKAGES = 5
VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")
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
    for p in (APP_DIR.parents[1] / "src" / "VERSION", APP_DIR.parent / "VERSION", APP_DIR.parents[1] / "VERSION"):
        if p.exists() and p.read_text().strip():
            return p.read_text().strip()
    return "0"


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


def _keep_last(out_dir: Path, n: int = KEEP_PACKAGES):
    zips = sorted(out_dir.glob("hidevp-panel-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[n:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def _get(url: str, timeout=20, limit=60 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hidevp-panel/" + installed_version()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("Файл обновления слишком большой")
    return data


def check() -> dict:
    try:
        m = json.loads(_get(GITHUB_URL, timeout=15))
        verify_manifest(m)
        STATE.update(latest=m, error="", source=GITHUB_URL, available=vt(m["version"]) > vt(installed_version()))
    except UpdateError as e:
        STATE.update(error=str(e))
    except Exception as e:
        STATE.update(error=f"Не удалось проверить обновления на GitHub: {e}")
    STATE["checked_at"] = time.time()
    return state()


def state() -> dict:
    m = STATE.get("latest") or {}
    st = {k: v for k, v in STATE.items() if k != "latest"}
    st.update(installed=installed_version(), latest_version=m.get("version", ""), latest_agent=m.get("agent_version", ""),
              notes=m.get("notes", ""), created=m.get("created", 0), size=m.get("size", 0),
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
    found = []
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30", timeout=15))
    except Exception as e:
        return found, [f"GitHub: {e}"]
    for r in data:
        v = (r.get("tag_name") or "").lstrip("vV")
        asset = next((a for a in r.get("assets") or [] if a.get("name") == "latest.json"), None)
        if not r.get("draft") and not r.get("prerelease") and asset and VERSION_RE.fullmatch(v):
            found.append((v, asset["browser_download_url"]))
    return found, []


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
        return _download(m, [GITHUB_URL.rsplit("/", 1)[0]])
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
    store = APP_DIR.parents[1] / "src"
    script = (f"cd {src} && : > {UPDATE_LOG} && {node}HV_LANG={lang} DOMAIN= bash install-master.sh </dev/null >> {UPDATE_LOG} 2>&1; "
              f"rc=$?; if [ $rc -ne 0 ] && [ -f {store}/install-master.sh ]; then echo __HV_RESTORE__ >> {UPDATE_LOG}; "
              f"(cd {store} && HV_LANG={lang} DOMAIN= bash install-master.sh </dev/null >> {UPDATE_LOG} 2>&1) "
              f"&& echo __HV_RESTORED__ >> {UPDATE_LOG}; fi; echo \"__HV_DONE__ $rc\" >> {UPDATE_LOG}; rm -rf {tmp}")
    r = subprocess.run(["systemd-run", "--no-block", "--unit", f"hidevp-update-{int(time.time())}", "--collect",
                        "--property=Type=oneshot", "bash", "-c", script], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError(f"Не удалось запустить обновление (systemd-run, код {r.returncode}): "
                          + ((r.stderr or "") + (r.stdout or "")).strip()[-300:])
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


def read_log(lines=60) -> str:
    try:
        return "\n".join(UPDATE_LOG.read_text(errors="ignore").splitlines()[-lines:])
    except OSError:
        return ""


async def loop(notify):
    await asyncio.sleep(30)
    while True:
        try:
            st = await asyncio.to_thread(check)
            if st["available"] and get_setting("update_notified") != st["latest_version"]:
                set_setting("update_notified", st["latest_version"])
                await notify(st)
        except Exception:
            log.exception("update check")
        await asyncio.sleep(CHECK_EVERY)
