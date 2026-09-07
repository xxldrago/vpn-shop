"""End-to-end claims for database.tx() and the guarded atomic debit (FOUND-02).

The tracer's real verify: a balance-funded order debits atomically inside
one BEGIN IMMEDIATE transaction, never goes negative (race test), and on
insufficient funds leaves zero residue — no order row, no ledger row.
"""
import sqlite3
import threading

import pytest

import database
import services


def _create_funded_user(name: str, balance) -> dict:
    """Create a shop user and top-up their balance via a direct UPDATE."""
    user = services.create_user(name, email="", password="")
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = ? WHERE id = ?", (balance, user["id"]))
        conn.commit()
    finally:
        conn.close()
    return user


def _plan_id_by_price(price) -> int:
    """Look up the seeded plan id for a given price_rub (seeds: 150/250/600/1000/1800)."""
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id FROM plans WHERE price_rub = ?", (price,)).fetchone()
        assert row is not None, f"no seeded plan at price {price}"
        return row["id"]
    finally:
        conn.close()


def _insert_plan(price) -> int:
    """Insert a custom plan (used for race-test pricing not in the seeds)."""
    conn = database.get_db()
    try:
        cur = conn.execute(
            "INSERT INTO plans (name, description, price_rub, duration_days, sort_order, created_at)"
            " VALUES (?, ?, ?, 30, 99, ?)",
            (f"Race {price}", "race test plan", price, services.now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _balance(user_id):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0
    finally:
        conn.close()


def _spend_rows(user_id):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'spend'", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


def _ledger_kind(user_id, kind):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = ?", (user_id, kind)
        ).fetchall()]
    finally:
        conn.close()


def _order_count(user_id):
    conn = database.get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM orders WHERE user_id = ?", (user_id,)).fetchone()["c"]
    finally:
        conn.close()


# ---------------- Guarded atomic debit ----------------

def test_debit_guarded_success(test_db):
    """Funded balance pays: balance drops by exactly the price, one spend row."""
    user = _create_funded_user("payer", 1000)
    plan_id = _plan_id_by_price(150.0)

    order = services.create_order(user["id"], plan_id, method="balance")

    assert order["price"] == 150
    assert isinstance(order["price"], int)
    assert order["payable"] == 0
    assert order["balance_used"] == 150
    assert _balance(user["id"]) == 850

    spend = _spend_rows(user["id"])
    assert len(spend) == 1
    assert spend[0]["amount"] == -150
    assert spend[0]["ref_order_id"] == order["id"]
    assert "с баланса" in spend[0]["note"]


def test_debit_insufficient_no_negative(test_db):
    """Balance 100 vs price 150: OrderError, balance untouched, zero residue."""
    user = _create_funded_user("poor", 100)
    plan_id = _plan_id_by_price(150.0)

    with pytest.raises(services.OrderError) as exc:
        services.create_order(user["id"], plan_id, method="balance")
    assert str(exc.value) == "Недостаточно средств на балансе для оплаты"

    assert _balance(user["id"]) == 100
    assert _order_count(user["id"]) == 0
    assert _spend_rows(user["id"]) == []


# ---------------- tx() BEGIN IMMEDIATE ----------------

def test_tx_begin_immediate_no_nested_error(test_db):
    """Prior DML on a fresh write conn must not break BEGIN IMMEDIATE.

    The trapped bug: a legacy implicit-transaction conn (isolation_level="")
    would leave a txn open after DML and BEGIN IMMEDIATE would raise
    OperationalError. get_write_db() uses isolation_level=None, so prior DML
    auto-commits and tx() starts clean (plan FOUND-02 edge probe).
    """
    conn = database.get_write_db()
    try:
        conn.execute("UPDATE shop_settings SET value = '1' WHERE key = 'test_subscription_days'")
        with database.tx(conn):
            conn.execute("UPDATE shop_settings SET value = '3' WHERE key = 'test_subscription_days'")
    finally:
        conn.close()
    # If BEGIN IMMEDIATE had raised, the with-block would have failed here.


def test_tx_rollback_on_exception(test_db):
    """An exception inside tx() rolls back all writes in the transaction."""
    user = _create_funded_user("rollbacker", 500)
    plan_id = _plan_id_by_price(150.0)

    with pytest.raises(RuntimeError):
        with database.tx() as conn:
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, original_price_rub, balance_used_rub, status, created_at)"
                " VALUES ('tx-test-order', ?, ?, 'Тест', 150, 150, 150, 'pending', ?)",
                (user["id"], plan_id, services.now_iso()),
            )
            raise RuntimeError("boom")

    # The inserted order must have been rolled back.
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = 'tx-test-order'").fetchone()
        assert row is None
    finally:
        conn.close()


# ---------------- Concurrency (double-spend pin) ----------------

def test_concurrent_debits_never_negative(test_db):
    """Two threads race create_order(balance 100, price 60): never negative.

    BEGIN IMMEDIATE serializes writers; the guarded UPDATE makes the second
    debit fail. At most one order succeeds and balance stays >= 0.
    """
    user = _create_funded_user("racer", 100)
    plan_id = _insert_plan(60.0)
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        barrier.wait()
        try:
            services.create_order(user["id"], plan_id, method="balance")
            results.append("ok")
        except services.OrderError:
            results.append("insufficient")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") <= 1
    balance = _balance(user["id"])
    assert balance >= 0
    assert balance == 100 - 60 * results.count("ok")


def test_no_nested_transaction_error_on_default_conn(test_db):
    """The literal trap: BEGIN IMMEDIATE on a legacy implicit-txn conn raises.

    A plain sqlite3.connect (isolation_level default) that has run DML holds
    an implicit transaction open — BEGIN IMMEDIATE must raise, which is why
    get_write_db() sets isolation_level=None. Pins the reason the helper differs.
    """
    legacy = sqlite3.connect(database.DB_PATH, timeout=30)
    try:
        legacy.execute("UPDATE shop_settings SET value = '1' WHERE key = 'test_subscription_days'")
        with pytest.raises(sqlite3.OperationalError):
            legacy.execute("BEGIN IMMEDIATE")
    finally:
        legacy.rollback()
        legacy.close()

# ---------------- Task 3: add_balance_int + get_balance transition window ----------------

def test_add_balance_int_credits_exactly_and_logs(test_db):
    """add_balance_int credits exactly 500 and inserts an int ledger row."""
    user = services.create_user("cr", email="", password="")
    conn = database.get_db()
    try:
        services.add_balance_int(conn, user["id"], 500, "topup", note="Пополнение баланса")
        conn.commit()
    finally:
        conn.close()
    assert _balance(user["id"]) == 500
    rows = [dict(r) for r in _spend_rows(user["id"]) + _ledger_kind(user["id"], "topup")]
    assert any(r["amount"] == 500 and isinstance(r["amount"], int) for r in rows)


def test_add_balance_int_rejects_float_input(test_db):
    """The helper itself asserts int — a float is a programming error (D-01)."""
    user = services.create_user("cr2", email="", password="")
    conn = database.get_db()
    try:
        with pytest.raises(TypeError):
            services.add_balance_int(conn, user["id"], 500.0, "topup")
    finally:
        conn.close()


def test_get_balance_floors_real_at_read(test_db):
    """Transition window: seeded REAL 250.5 is floored to 250 (int) at read."""
    user = services.create_user("cr3", email="", password="")
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = 250.5 WHERE id = ?", (user["id"],))
        conn.commit()
    finally:
        conn.close()
    bal = services.get_balance(user["id"])
    assert bal == 250
    assert isinstance(bal, int)
