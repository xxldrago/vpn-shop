import asyncio
import os
import sqlite3
from contextlib import contextmanager

from config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS

MONEY_COLUMNS = [
    ("plans", "price_rub"),
    ("promo_codes", "discount_amount_rub"),
    ("orders", "amount_rub"),
    ("orders", "original_price_rub"),
    ("orders", "balance_used_rub"),
    ("app_users", "balance"),
    ("balance_transactions", "amount"),
]

TIMESTAMP_COLUMNS = [
    ("orders", "paid_at"),
    ("orders", "created_at"),
    ("orders", "expires_at"),
    ("balance_transactions", "created_at"),
    ("app_users", "created_at"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    price_rub REAL NOT NULL,
    currency TEXT DEFAULT 'RUB',
    duration_days INTEGER NOT NULL,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    discount_percent INTEGER,
    discount_amount_rub REAL,
    max_uses INTEGER,
    used_count INTEGER DEFAULT 0,
    valid_from TEXT,
    valid_until TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan_id INTEGER,
    plan_name TEXT,
    promo_code_id INTEGER,
    amount_rub REAL NOT NULL,
    original_price_rub REAL,
    balance_used_rub REAL DEFAULT 0,
    referral_applied INTEGER DEFAULT 0,
    is_trial INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    platega_transaction_id TEXT,
    panel_user_created INTEGER DEFAULT 0,
    panel_user_connections TEXT,
    provisioning_error TEXT,
    paid_at TEXT,
    expires_at TEXT,
    created_at TEXT,
    quantity INTEGER DEFAULT 1,
    FOREIGN KEY (plan_id) REFERENCES plans(id),
    FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender_id TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT,
    FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
);

CREATE TABLE IF NOT EXISTS shop_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS app_users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE,
    email TEXT,
    telegram_id TEXT,
    password_hash TEXT,
    role TEXT DEFAULT 'user',
    enabled INTEGER DEFAULT 1,
    balance REAL DEFAULT 0,
    referral_code TEXT UNIQUE,
    referrer_id TEXT,
    referred_paid INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS balance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    ref_order_id TEXT,
    note TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funnel_user_event ON funnel_events(user_id, event);

CREATE TABLE IF NOT EXISTS job_runs (
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT
);
"""

DEFAULT_PLANS = [
    ("15 дней", "Быстрый доступ с 15 дней", 150.0, 15, 1),
    ("1 месяц", "Полноценная поддержка на месяц", 250.0, 30, 2),
    ("3 месяца", "Популярный тариф на 3 месяца", 600.0, 90, 3),
    ("6 месяцев", "Выгодно на полгода", 1000.0, 180, 4),
    ("12 месяцев", "Максимальная выгода на год", 1800.0, 365, 5),
]


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_write_db():
    """Connection for explicit write transactions (isolation_level=None).

    isolation_level=None disables the implicit transaction BEGIN that
    sqlite3 opens before DML, so an explicit BEGIN IMMEDIATE in tx() can
    never hit "cannot start a transaction within a transaction".
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx(conn=None):
    """Write transaction: BEGIN IMMEDIATE, commit on success, rollback on error.

    Opens a fresh write connection when conn is None; closes it (and the
    caller's conn, when provided) in finally. BEGIN IMMEDIATE serializes
    writers so a guarded `UPDATE ... WHERE balance >= ?` is race-free.
    """
    own_conn = False
    if conn is None:
        conn = get_write_db()
        own_conn = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_defaults(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Add columns that may be missing on databases created by older versions."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_runs ("
        "job_name TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, "
        "ok INTEGER NOT NULL, error TEXT)"
    )
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    for col, ddl in (
        ("original_price_rub", "REAL"),
        ("balance_used_rub", "REAL DEFAULT 0"),
        ("referral_applied", "INTEGER DEFAULT 0"),
    ):
        if col not in columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")

    ucolumns = {r["name"] for r in conn.execute("PRAGMA table_info(app_users)").fetchall()}
    for col, ddl in (
        ("balance", "REAL DEFAULT 0"),
        ("referral_code", "TEXT"),
        ("referrer_id", "TEXT"),
        ("referred_paid", "INTEGER DEFAULT 0"),
        ("auto_renewal", "INTEGER DEFAULT 0"),
        ("auto_renewal_at", "TEXT"),
    ):
        if col not in ucolumns:
            conn.execute(f"ALTER TABLE app_users ADD COLUMN {col} {ddl}")



def _seed_defaults(conn):
    # Lazy import: services.py imports this module at top level, so a
    # module-level `from services import ...` here would create a cycle.
    from services import now_iso

    existing = conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()["c"]
    if existing == 0:
        for name, desc, price, days, sort in DEFAULT_PLANS:
            conn.execute(
                "INSERT INTO plans (name, description, price_rub, duration_days, sort_order, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, desc, price, days, sort, now_iso()),
            )

    for key, value in DEFAULT_SETTINGS.items():
        row = conn.execute("SELECT value FROM shop_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO shop_settings (key, value) VALUES (?, ?)", (key, str(value)))

    # Seed a default admin account if no admins exist
    admin_count = conn.execute("SELECT COUNT(*) AS c FROM app_users WHERE role = 'admin'").fetchone()["c"]
    if admin_count == 0:
        from auth import hash_password
        import uuid
        conn.execute(
            "INSERT INTO app_users (id, username, email, telegram_id, password_hash, role, enabled, created_at)"
            " VALUES (?, 'admin', '', '', ?, 'admin', 1, ?)",
            (str(uuid.uuid4()), hash_password("admin"), now_iso()),
        )

    # Ensure every user has a unique referral code
    import secrets as _secrets
    for row in conn.execute("SELECT id FROM app_users WHERE referral_code IS NULL OR referral_code = ''").fetchall():
        for _ in range(100):
            code = "".join(_secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            if not conn.execute("SELECT id FROM app_users WHERE referral_code = ?", (code,)).fetchone():
                conn.execute("UPDATE app_users SET referral_code = ? WHERE id = ?", (code, row["id"]))
                break


def get_setting(key, default=None):
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM shop_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO shop_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()
