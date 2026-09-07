"""Rehearsal and golden tests for the one-time money migration."""

import sqlite3

import database
import jobs


def _legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(database.SCHEMA)
    conn.executemany(
        "INSERT INTO plans (name, price_rub, duration_days) VALUES (?, ?, ?)",
        [
            ("fractional", 150.7, 15),
            ("half", 82.5, 30),
            ("whole", 250.0, 90),
            ("tiny", 0.335, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_codes (code, discount_amount_rub) VALUES (?, ?)",
        [("P250", 250.0), ("P99", 99.5), ("P0", 0.335)],
    )
    conn.execute(
        "INSERT INTO app_users (id, username, balance, created_at) VALUES (?, ?, ?, ?)",
        ("u-negative", "negative", -62.5, "2026-09-07T01:02:03"),
    )
    conn.execute(
        "INSERT INTO balance_transactions (user_id, amount, kind, created_at) VALUES (?, ?, ?, ?)",
        ("u-negative", 0.335, "deposit", "2026-09-07T01:02:03"),
    )
    conn.execute(
        "INSERT INTO orders (id, user_id, amount_rub, original_price_rub, balance_used_rub, created_at, paid_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "order-1",
            "u-negative",
            150.7,
            82.5,
            250.0,
            "2026-09-07T01:02:03",
            "2026-09-07T01:02:03",
        ),
    )
    conn.commit()
    conn.close()
    return path


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {row[1]: row[2].upper() for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_verify_only_builds_integer_backfill_without_drop(tmp_path, capsys):
    path = _legacy_db(tmp_path)

    jobs.migrate_money(db_path=str(path), verify_only=True)

    output = capsys.readouterr().out
    assert "plans.price_rub" in output
    plan_columns = _columns(path, "plans")
    assert plan_columns["price_rub"] == "REAL"
    assert plan_columns["price_rub_rub"] == "INTEGER"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT price_rub_rub FROM plans ORDER BY id").fetchall() == [
            (150,),
            (82,),
            (250,),
            (0,),
        ]
    finally:
        conn.close()


def test_verify_only_handles_negative_floor_and_promo_column(tmp_path):
    path = _legacy_db(tmp_path)

    jobs.migrate_money(db_path=str(path), verify_only=True)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT balance_rub FROM app_users").fetchone() == (-63,)
        assert conn.execute(
            "SELECT discount_amount_rub_rub FROM promo_codes ORDER BY id"
        ).fetchall() == [(250,), (99,), (0,)]
    finally:
        conn.close()


def test_dry_run_does_not_touch_legacy_schema(tmp_path, capsys):
    path = _legacy_db(tmp_path)

    jobs.migrate_money(db_path=str(path), dry_run=True)

    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "plans.price_rub" in output
    assert "price_rub_rub" not in _columns(path, "plans")
