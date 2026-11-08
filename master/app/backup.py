import io
import os
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from .db import DATA, SCHEMA, MIGRATIONS, conn, get_setting

BACKUP_DIR = DATA / "backups"
KEEP_AUTO = 10
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}
ACCESS_SETTINGS = ("web_port", "web_path", "listen_host", "public_url")
MAX_SIZE = 100 * 1024 * 1024
PARTS = {
    "users": ("users",),
    "servers": ("nodes",),
    "vpns": ("vpns",),
    "stats": (),
    "payments": ("payments", "crypto_invoices", "manual_payments"),
    "promos": ("promos", "promo_uses"),
    "notices": ("notices", "notice_msgs"),
    "settings": ("settings",),
}
ALL_PARTS = tuple(PARTS)
TRAFFIC_COLS = ("used_up", "used_down")


class BackupError(Exception):
    pass


def _version() -> str:
    root = Path(__file__).resolve().parents[2]
    for p in (root / "src" / "VERSION", root / "VERSION"):
        if p.exists():
            return p.read_text().strip()
    return "?"


def counts(c: sqlite3.Connection) -> dict:
    out = {}
    for t in ("users", "nodes", "vpns", "promos", "payments"):
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = 0
    return out


def clean_parts(parts) -> tuple:
    if not parts:
        return ALL_PARTS
    if isinstance(parts, str):
        parts = parts.split(",")
    sel = {p.strip() for p in parts if p and p.strip() in PARTS}
    if not sel:
        raise BackupError("Выберите, что сохранить в копии")
    if "vpns" in sel:
        sel.add("servers")
    return tuple(p for p in ALL_PARTS if p in sel)


def _strip(c: sqlite3.Connection, parts: tuple):
    keep = {t for p in parts for t in PARTS[p]}
    if "stats" in parts:
        keep.add("users")
    for t in {t for ts in PARTS.values() for t in ts} - keep:
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM sessions")
    c.execute("DELETE FROM admins")
    if "users" not in parts and "stats" in parts:
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")
                if r[1] not in ("id", "tg_id", "uuid", "token", *TRAFFIC_COLS)]
        c.execute(f"UPDATE users SET {', '.join(col + '=NULL' for col in cols)}")
    elif "users" in parts and "stats" not in parts:
        c.execute("UPDATE users SET used_up=0, used_down=0")
    c.commit()


def make_zip(parts=None) -> bytes:
    parts = clean_parts(parts)
    with tempfile.TemporaryDirectory() as td:
        dst_path = Path(td) / "panel.db"
        dst = sqlite3.connect(dst_path)
        conn().backup(dst)
        if parts != ALL_PARTS:
            _strip(dst, parts)
            dst.execute("VACUUM")
        n = counts(dst)
        dst.close()
        meta = {"app": "hidevp-panel", "version": _version(), "created": time.time(),
                "public_url": get_setting("public_url"), "counts": n, "parts": list(parts)}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dst_path, "panel.db")
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return buf.getvalue()


def auto_backup(reason: str = "auto", parts=None) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    path = BACKUP_DIR / f"hidevp-{reason}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.zip"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(make_zip(parts))
    tmp.replace(path)
    keep = KEEP_AUTO
    if reason == "scheduled":
        try:
            keep = max(1, min(100, int(get_setting("backup_keep") or 10)))
        except ValueError:
            pass
    files = sorted(BACKUP_DIR.glob(f"hidevp-{reason}-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return path


def schedule() -> dict:
    period = get_setting("backup_auto") or "off"
    files = sorted(BACKUP_DIR.glob("hidevp-scheduled-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if BACKUP_DIR.exists() else []
    last = files[0].stat().st_mtime if files else 0
    nxt = (last + PERIODS[period] if last else time.time()) if period in PERIODS else 0
    return {"auto": period, "keep": int(get_setting("backup_keep") or 10), "last": last, "next": nxt,
            "count": len(files)}


def scheduled_due() -> bool:
    st = schedule()
    return st["auto"] in PERIODS and time.time() >= st["next"]


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("hidevp-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {}
        try:
            with zipfile.ZipFile(p) as z:
                meta = json.loads(z.read("meta.json"))
        except Exception:
            pass
        out.append({"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime,
                    "version": meta.get("version", "?"), "counts": meta.get("counts", {}),
                    "parts": meta.get("parts") or list(ALL_PARTS)})
    return out


def _open_upload(data: bytes, td: str) -> tuple[sqlite3.Connection, dict]:
    if len(data) > MAX_SIZE:
        raise BackupError("Файл слишком большой")
    path = Path(td) / "restore.db"
    meta = {}
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "panel.db" not in z.namelist():
                    raise BackupError("В архиве нет panel.db — это не резервная копия HideVP")
                path.write_bytes(z.read("panel.db"))
                if "meta.json" in z.namelist():
                    meta = json.loads(z.read("meta.json"))
        except zipfile.BadZipFile:
            raise BackupError("Повреждённый архив")
    elif data[:16] == b"SQLite format 3\x00":
        path.write_bytes(data)
    else:
        raise BackupError("Неизвестный формат файла. Нужен .zip, скачанный в разделе «Резервная копия»")
    try:
        src = sqlite3.connect(path)
        src.row_factory = sqlite3.Row
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        raise BackupError("Файл базы повреждён")
    if not {"settings", "users", "nodes"} <= tables:
        raise BackupError("Это не база HideVP (нет нужных таблиц)")
    src.executescript(SCHEMA)
    for table, col, sql in MIGRATIONS:
        cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            src.execute(sql)
    src.commit()
    return src, meta


def _cols(c, table) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def restore(data: bytes, mode: str) -> dict:
    if mode not in ("replace", "merge"):
        raise BackupError("Неизвестный режим восстановления")
    with tempfile.TemporaryDirectory() as td:
        src, meta = _open_upload(data, td)
        parts = clean_parts(meta.get("parts")) if meta.get("parts") else ALL_PARTS
        before = auto_backup("before-restore")
        dst = conn()
        keep_settings = {k: get_setting(k) for k in ACCESS_SETTINGS}
        admins = [dict(r) for r in dst.execute("SELECT * FROM admins")]
        sessions = [dict(r) for r in dst.execute("SELECT * FROM sessions")]
        if mode == "replace" and parts != ALL_PARTS:
            stats = _replace_parts(src, dst, parts)
        elif mode == "replace":
            src.backup(dst)
            dst.execute("DELETE FROM admins")
            dst.execute("DELETE FROM sessions")
            for a in admins:
                dst.execute("INSERT INTO admins(id,username,pass_hash) VALUES(?,?,?)", (a["id"], a["username"], a["pass_hash"]))
            for se in sessions:
                dst.execute("INSERT OR IGNORE INTO sessions(token,admin_id,created) VALUES(?,?,?)", (se["token"], se["admin_id"], se["created"]))
            stats = counts(dst)
        else:
            stats = _merge(src, dst, parts)
        for k, v in keep_settings.items():
            if v:
                dst.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        src.close()
    return {"mode": mode, "parts": list(parts), "stats": stats, "auto_backup": before.name, "from_version": meta.get("version", "?"),
            "from_created": meta.get("created", 0)}


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str):
    have = set(_cols(src, table))
    cols = [c for c in _cols(dst, table) if c in have]
    rows = [tuple(r) for r in src.execute(f"SELECT {', '.join(cols)} FROM {table}")]
    if rows:
        dst.executemany(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", rows)


def _find_user(dst: sqlite3.Connection, r: dict):
    ex = None
    if r.get("tg_id"):
        ex = dst.execute("SELECT id FROM users WHERE tg_id=?", (r["tg_id"],)).fetchone()
    if not ex:
        ex = dst.execute("SELECT id FROM users WHERE uuid=? OR token=?", (r["uuid"], r["token"])).fetchone()
    return ex


def _traffic_only(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    n = 0
    for r in src.execute("SELECT * FROM users"):
        ex = _find_user(dst, dict(r))
        if ex:
            dst.execute("UPDATE users SET used_up=?, used_down=? WHERE id=?", (r["used_up"] or 0, r["used_down"] or 0, ex[0]))
            n += 1
    return n


def _replace_parts(src: sqlite3.Connection, dst: sqlite3.Connection, parts: tuple) -> dict:
    dst.execute("PRAGMA foreign_keys=OFF")
    dst.execute("BEGIN")
    try:
        traffic = []
        if "users" in parts and "stats" not in parts:
            traffic = dst.execute("SELECT used_up, used_down, id FROM users").fetchall()
        marks = ",".join("?" * len(ACCESS_SETTINGS))
        for part in parts:
            for t in PARTS[part]:
                if t == "settings":
                    dst.execute(f"DELETE FROM settings WHERE key NOT IN ({marks})", ACCESS_SETTINGS)
                    for r in src.execute(f"SELECT key, value FROM settings WHERE key NOT IN ({marks})", ACCESS_SETTINGS):
                        dst.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (r["key"], r["value"]))
                    continue
                dst.execute(f"DELETE FROM {t}")
                _copy_table(src, dst, t)
        dst.executemany("UPDATE users SET used_up=?, used_down=? WHERE id=?", [tuple(r) for r in traffic])
        if "stats" in parts and "users" not in parts:
            _traffic_only(src, dst)
        dst.execute("DELETE FROM vpns WHERE node_id NOT IN (SELECT id FROM nodes)")
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    finally:
        dst.execute("PRAGMA foreign_keys=ON")
    return counts(dst)


def _merge(src: sqlite3.Connection, dst: sqlite3.Connection, parts: tuple = ALL_PARTS) -> dict:
    st = {"settings": 0, "nodes_new": 0, "nodes_upd": 0, "vpns_new": 0, "vpns_upd": 0, "users_new": 0,
          "users_upd": 0, "promos_new": 0, "promos_upd": 0, "payments_new": 0, "traffic_upd": 0}

    def upsert(table, row: dict, existing_id):
        cols = [c for c in _cols(dst, table) if c != "id" and c in row]
        if existing_id:
            dst.execute(f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                        (*[row[c] for c in cols], existing_id))
            return existing_id
        cur = dst.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                          [row[c] for c in cols])
        return cur.lastrowid

    dst.execute("BEGIN")
    try:
        for r in src.execute("SELECT key, value FROM settings"):
            if r["key"] in ACCESS_SETTINGS:
                continue
            dst.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (r["key"], r["value"]))
            st["settings"] += 1
        node_map = {}
        for r in src.execute("SELECT * FROM nodes"):
            r = dict(r)
            ex = dst.execute("SELECT id FROM nodes WHERE host=? AND port=?", (r["host"], r["port"])).fetchone()
            node_map[r["id"]] = upsert("nodes", r, ex[0] if ex else None)
            st["nodes_upd" if ex else "nodes_new"] += 1
        for r in src.execute("SELECT * FROM vpns"):
            r = dict(r)
            nid = node_map.get(r["node_id"])
            if not nid:
                continue
            r["node_id"] = nid
            ex = dst.execute("SELECT id FROM vpns WHERE node_id=? AND port=?", (nid, r["port"])).fetchone()
            upsert("vpns", r, ex[0] if ex else None)
            st["vpns_upd" if ex else "vpns_new"] += 1
        promo_map = {}
        for r in src.execute("SELECT * FROM promos"):
            r = dict(r)
            ex = dst.execute("SELECT id FROM promos WHERE code=? COLLATE NOCASE", (r["code"],)).fetchone()
            promo_map[r["id"]] = upsert("promos", r, ex[0] if ex else None)
            st["promos_upd" if ex else "promos_new"] += 1
        user_map = {}
        for r in (src.execute("SELECT * FROM users") if "users" in parts else []):
            r = dict(r)
            ex = _find_user(dst, r)
            if "stats" not in parts:
                for c in TRAFFIC_COLS:
                    r.pop(c, None)
            if r.get("phone"):
                dst.execute("UPDATE users SET phone='' WHERE phone=? AND id!=?", (r["phone"], ex[0] if ex else -1))
            r["promo_id"] = promo_map.get(r.get("promo_id") or 0, 0)
            user_map[r["id"]] = upsert("users", r, ex[0] if ex else None)
            st["users_upd" if ex else "users_new"] += 1
        if "stats" in parts and "users" not in parts:
            st["traffic_upd"] = _traffic_only(src, dst)
        pay_map = {}
        for r in src.execute("SELECT * FROM payments"):
            r = dict(r)
            ex = dst.execute("SELECT id FROM payments WHERE tg_charge_id=?", (r["tg_charge_id"],)).fetchone()
            if ex:
                pay_map[r["id"]] = ex[0]
                continue
            r["user_id"] = user_map.get(r["user_id"], r["user_id"])
            r["promo_id"] = promo_map.get(r.get("promo_id") or 0, 0)
            pay_map[r["id"]] = upsert("payments", r, None)
            st["payments_new"] += 1
        for r in src.execute("SELECT * FROM promo_uses"):
            r = dict(r)
            pid = pay_map.get(r["payment_id"])
            if pid and not dst.execute("SELECT id FROM promo_uses WHERE payment_id=?", (pid,)).fetchone():
                r.update(payment_id=pid, user_id=user_map.get(r["user_id"], r["user_id"]),
                         promo_id=promo_map.get(r["promo_id"], r["promo_id"]))
                upsert("promo_uses", r, None)
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    return st


def read_backup(name: str) -> bytes:
    p = BACKUP_DIR / Path(name).name
    if not p.exists() or not p.name.startswith("hidevp-"):
        raise BackupError("Резервная копия не найдена")
    return p.read_bytes()
