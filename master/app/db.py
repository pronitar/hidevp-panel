import json
import os
import sqlite3
import time
from pathlib import Path

BASE = Path(os.environ.get("PANEL_HOME", "/opt/hidevp-panel/master"))
DATA = BASE / "data"
DB_PATH = DATA / "panel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE, pass_hash TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, admin_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY, name TEXT, host TEXT, port INTEGER, api_key TEXT,
    cert_pem TEXT, ip TEXT, country_code TEXT DEFAULT '', country TEXT DEFAULT '',
    city TEXT DEFAULT '', status TEXT DEFAULT 'unknown', error TEXT DEFAULT '',
    last_seen REAL DEFAULT 0, info TEXT DEFAULT '{}', created REAL);
CREATE TABLE IF NOT EXISTS vpns (
    id INTEGER PRIMARY KEY, name TEXT, node_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    port INTEGER, transport TEXT, sni TEXT, dest TEXT, fingerprint TEXT,
    private_key TEXT, public_key TEXT, short_id TEXT, path TEXT DEFAULT '',
    service_name TEXT DEFAULT '', enabled INTEGER DEFAULT 1, sort INTEGER DEFAULT 0,
    created REAL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, tg_id INTEGER UNIQUE, tg_username TEXT DEFAULT '',
    name TEXT DEFAULT '', uuid TEXT, token TEXT UNIQUE, created REAL,
    expires_at REAL DEFAULT 0, traffic_limit INTEGER DEFAULT 0,
    used_up INTEGER DEFAULT 0, used_down INTEGER DEFAULT 0,
    device_limit INTEGER DEFAULT 0, block_torrent INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1, online_ips TEXT DEFAULT '[]', online_at REAL DEFAULT 0,
    suspended_until REAL DEFAULT 0, note TEXT DEFAULT '',
    notified_3d INTEGER DEFAULT 0, notified_exp INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY, user_id INTEGER, tg_charge_id TEXT, months INTEGER,
    amount INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS crypto_invoices (
    id INTEGER PRIMARY KEY, invoice_id INTEGER UNIQUE, user_id INTEGER, months INTEGER, amount INTEGER,
    full_amount INTEGER, currency TEXT, promo_id INTEGER DEFAULT 0, status TEXT, url TEXT, chat_id INTEGER DEFAULT 0,
    created REAL);
CREATE TABLE IF NOT EXISTS manual_payments (
    id INTEGER PRIMARY KEY, user_id INTEGER, months INTEGER, amount INTEGER, full_amount INTEGER, currency TEXT,
    promo_id INTEGER DEFAULT 0, status TEXT, created REAL, claimed REAL DEFAULT 0, decided_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY, text TEXT, text_en TEXT DEFAULT '', kind TEXT DEFAULT 'info', starts_at REAL, ends_at REAL DEFAULT 0,
    send_tg INTEGER DEFAULT 1, audience TEXT DEFAULT 'all', enabled INTEGER DEFAULT 1, created REAL, sent_at REAL DEFAULT 0,
    sent_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notice_msgs (
    id INTEGER PRIMARY KEY, notice_id INTEGER, chat_id INTEGER, message_id INTEGER, lang TEXT DEFAULT 'ru');
CREATE INDEX IF NOT EXISTS notice_msgs_n ON notice_msgs(notice_id);
CREATE TABLE IF NOT EXISTS promos (
    id INTEGER PRIMARY KEY, code TEXT UNIQUE COLLATE NOCASE, discount INTEGER DEFAULT 10,
    months TEXT DEFAULT '', expires_at REAL DEFAULT 0, max_uses INTEGER DEFAULT 0,
    used INTEGER DEFAULT 0, reusable INTEGER DEFAULT 0, new_only INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1, note TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS promo_uses (
    id INTEGER PRIMARY KEY, promo_id INTEGER, user_id INTEGER, payment_id INTEGER,
    months INTEGER, amount INTEGER, full_amount INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, hwid TEXT,
    os TEXT DEFAULT '', model TEXT DEFAULT '', app TEXT DEFAULT '', ip TEXT DEFAULT '',
    first_seen REAL, last_seen REAL, UNIQUE(user_id, hwid));
CREATE TABLE IF NOT EXISTS ext_payments (
    id INTEGER PRIMARY KEY, provider TEXT, ext_id TEXT DEFAULT '', user_id INTEGER, months INTEGER,
    amount INTEGER, full_amount INTEGER, currency TEXT, promo_id INTEGER DEFAULT 0, status TEXT,
    url TEXT DEFAULT '', chat_id INTEGER DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS ext_payments_s ON ext_payments(status);
CREATE TABLE IF NOT EXISTS scanners (
    ip TEXT PRIMARY KEY, first REAL, last REAL, hits INTEGER DEFAULT 0, paths TEXT DEFAULT '[]',
    ua TEXT DEFAULT '', banned_until REAL DEFAULT 0, reported INTEGER DEFAULT 0);
"""

DEFAULT_SETTINGS = {
    "panel_title": "VPN",
    "public_url": "",
    "bot_token": "",
    "admin_tg_ids": "",
    "prices": json.dumps({"1": 200, "3": 600, "6": 1200, "12": 2400}),
    "support": "",
    "default_traffic_limit_gb": "0",
    "default_device_limit": "0",
    "auto_select": "0",
    "client_fragment": "0",
    "stub_on": "0",
    "stub_kind": "personal",
    "stub_title": "",
    "stub_html": "",
    "cabinet_path": "",
    "hide_node_api": "1",
    "device_mode": "ip",
    "hwid_ttl_days": "30",
    "hwid_strict": "0",
    "sub_hide_offline": "0",
    "trial_days": "0",
    "trial_phone": "0",
    "ref_on": "0",
    "ref_days": "7",
    "ref_new_days": "0",
    "wb_on": "0",
    "wb_days": "3",
    "wb_discount": "20",
    "wb_valid": "7",
    "churn_on": "0",
    "scan_detect": "1",
    "scan_ban": "0",
    "scan_digest": "0",
    "mon_on": "1",
    "mon_down_min": "3",
    "mon_disk": "90",
    "mon_mem": "95",
    "mon_xray": "1",
    "app_title": "",
    "app_desc": "",
    "app_interval": "6",
    "app_web": "1",
    "app_email": "",
    "app_announce": "",
    "app_announce_url": "",
    "app_provider": "",
    "app_ping": "",
    "app_sort": "",
    "app_autoconnect": "",
    "app_ping_open": "0",
    "app_pin": "0",
    "app_expand": "0",
    "app_hide": "0",
    "app_expire": "1",
    "app_hide_url": "0",
    "tg_route": "direct",
    "tg_node_id": "",
    "tg_proxy": "",
    "lang": "ru",
    "backup_auto": "week",
    "backup_keep": "10",
    "bot_ui": "",
    "pay_methods": "",
}

_conn: sqlite3.Connection | None = None


def _secure(d: Path):
    try:
        os.chmod(d, 0o700)
        for f in d.glob("panel.db*"):
            os.chmod(f, 0o600)
    except OSError:
        pass


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA.mkdir(parents=True, exist_ok=True)
        _secure(DATA)
        _conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False,
                                isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


MIGRATIONS = [
    ("users", "phone", "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''"),
    ("users", "forever", "ALTER TABLE users ADD COLUMN forever INTEGER DEFAULT 0"),
    ("users", "lang", "ALTER TABLE users ADD COLUMN lang TEXT DEFAULT ''"),
    ("users", "promo_id", "ALTER TABLE users ADD COLUMN promo_id INTEGER DEFAULT 0"),
    ("payments", "promo_id", "ALTER TABLE payments ADD COLUMN promo_id INTEGER DEFAULT 0"),
    ("payments", "full_amount", "ALTER TABLE payments ADD COLUMN full_amount INTEGER DEFAULT 0"),
    ("payments", "method", "ALTER TABLE payments ADD COLUMN method TEXT DEFAULT 'stars'"),
    ("users", "ui_msgs", "ALTER TABLE users ADD COLUMN ui_msgs TEXT DEFAULT ''"),
    ("manual_payments", "code", "ALTER TABLE manual_payments ADD COLUMN code TEXT DEFAULT ''"),
    ("payments", "currency", "ALTER TABLE payments ADD COLUMN currency TEXT DEFAULT 'XTR'"),
    ("promo_uses", "currency", "ALTER TABLE promo_uses ADD COLUMN currency TEXT DEFAULT 'XTR'"),
    ("vpns", "protocol", "ALTER TABLE vpns ADD COLUMN protocol TEXT DEFAULT 'vless'"),
    ("vpns", "server_key", "ALTER TABLE vpns ADD COLUMN server_key TEXT DEFAULT ''"),
    ("vpns", "obfs", "ALTER TABLE vpns ADD COLUMN obfs TEXT DEFAULT ''"),
    ("vpns", "cert_pem", "ALTER TABLE vpns ADD COLUMN cert_pem TEXT DEFAULT ''"),
    ("vpns", "key_pem", "ALTER TABLE vpns ADD COLUMN key_pem TEXT DEFAULT ''"),
    ("vpns", "cert_pin", "ALTER TABLE vpns ADD COLUMN cert_pin TEXT DEFAULT ''"),
    ("vpns", "upstream_id", "ALTER TABLE vpns ADD COLUMN upstream_id INTEGER DEFAULT 0"),
    ("vpns", "bridge_uuid", "ALTER TABLE vpns ADD COLUMN bridge_uuid TEXT DEFAULT ''"),
    ("vpns", "descr", "ALTER TABLE vpns ADD COLUMN descr TEXT DEFAULT ''"),
    ("users", "source", "ALTER TABLE users ADD COLUMN source TEXT DEFAULT ''"),
    ("users", "ref_by", "ALTER TABLE users ADD COLUMN ref_by INTEGER DEFAULT 0"),
    ("users", "ref_rewarded", "ALTER TABLE users ADD COLUMN ref_rewarded INTEGER DEFAULT 0"),
    ("users", "trial_at", "ALTER TABLE users ADD COLUMN trial_at REAL DEFAULT 0"),
    ("users", "trial_phone", "ALTER TABLE users ADD COLUMN trial_phone TEXT DEFAULT ''"),
    ("users", "wb_sent", "ALTER TABLE users ADD COLUMN wb_sent REAL DEFAULT 0"),
    ("users", "churn_asked", "ALTER TABLE users ADD COLUMN churn_asked REAL DEFAULT 0"),
    ("users", "churn_reason", "ALTER TABLE users ADD COLUMN churn_reason TEXT DEFAULT ''"),
    ("users", "churn_at", "ALTER TABLE users ADD COLUMN churn_at REAL DEFAULT 0"),
    ("promos", "user_id", "ALTER TABLE promos ADD COLUMN user_id INTEGER DEFAULT 0"),
    ("promos", "kind", "ALTER TABLE promos ADD COLUMN kind TEXT DEFAULT ''"),
    ("nodes", "mt_port", "ALTER TABLE nodes ADD COLUMN mt_port INTEGER DEFAULT 0"),
    ("nodes", "mt_domain", "ALTER TABLE nodes ADD COLUMN mt_domain TEXT DEFAULT ''"),
    ("nodes", "mt_key", "ALTER TABLE nodes ADD COLUMN mt_key TEXT DEFAULT ''"),
    ("nodes", "tl_gb", "ALTER TABLE nodes ADD COLUMN tl_gb REAL DEFAULT 0"),
    ("nodes", "tl_period", "ALTER TABLE nodes ADD COLUMN tl_period TEXT DEFAULT 'month'"),
    ("nodes", "tl_day", "ALTER TABLE nodes ADD COLUMN tl_day INTEGER DEFAULT 1"),
    ("nodes", "tl_mode", "ALTER TABLE nodes ADD COLUMN tl_mode TEXT DEFAULT 'both'"),
    ("nodes", "tl_used", "ALTER TABLE nodes ADD COLUMN tl_used INTEGER DEFAULT 0"),
    ("nodes", "tl_start", "ALTER TABLE nodes ADD COLUMN tl_start REAL DEFAULT 0"),
    ("nodes", "tl_over", "ALTER TABLE nodes ADD COLUMN tl_over INTEGER DEFAULT 0"),
    ("nodes", "net_last", "ALTER TABLE nodes ADD COLUMN net_last TEXT DEFAULT ''"),
    ("nodes", "socks_port", "ALTER TABLE nodes ADD COLUMN socks_port INTEGER DEFAULT 0"),
    ("nodes", "socks_user", "ALTER TABLE nodes ADD COLUMN socks_user TEXT DEFAULT ''"),
    ("nodes", "socks_pass", "ALTER TABLE nodes ADD COLUMN socks_pass TEXT DEFAULT ''"),
]


def init_db():
    conn().executescript(SCHEMA)
    for table, col, sql in MIGRATIONS:
        cols = {r["name"] for r in q(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn().execute(sql)
    conn().execute("CREATE INDEX IF NOT EXISTS users_phone ON users(phone)")
    conn().execute("CREATE INDEX IF NOT EXISTS users_ref ON users(ref_by)")
    for k, v in DEFAULT_SETTINGS.items():
        conn().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    return conn().execute(sql, args).lastrowid


def ex_count(sql, args=()) -> int:
    return conn().execute(sql, args).rowcount


def get_setting(key, default=""):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def prices() -> dict[int, int]:
    try:
        return {int(k): int(v) for k, v in json.loads(get_setting("prices")).items()}
    except Exception:
        return {1: 200, 3: 600, 6: 1200, 12: 2400}


def now() -> float:
    return time.time()
