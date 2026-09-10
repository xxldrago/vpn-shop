"""PROMO-01 vertical slice tests: first-purchase promo validation (P-01/P-06).

Proves the migrate → validate → claim architecture end-to-end against the
isolated `test_db` fixture: a first-purchase-only code is rejected at apply
time for a user with any paid non-trial order and accepted for a fresh user;
the per-user claim is a race-free guarded UPDATE (P-02/D-09) with exactly one
winner under a threading.Barrier; promo orders store the pre-promo total in
original_price_rub (PROMO-04 discount math depends on it); and per-user
usage increments inside used_per_user.
"""
import json
import threading

import database
import money
import services


def _plan_id_by_price(price) -> int:
    """Look up the seeded plan id (seeds: 150/250/600/1000/1800 rubles)."""
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id FROM plans WHERE price_rub = ?", (price,)).fetchone()
        assert row is not None, f"no seeded plan at price {price}"
        return row["id"]
    finally:
        conn.close()


def _promo_code_full(code: str, percent: int, *, max_uses_per_user=None,
                     first_purchase_only=0, valid_from=None, valid_until=None,
                     used_per_user="{}", is_active=1, max_uses=None) -> str:
    """Insert a promo code with the Phase 3 columns and return its code."""
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, max_uses, max_uses_per_user,"
            " first_purchase_only, valid_from, valid_until, used_per_user, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, percent, max_uses, max_uses_per_user, first_purchase_only,
             valid_from, valid_until, used_per_user, is_active, services.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return code


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


def _promo_id(code: str) -> int:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id FROM promo_codes WHERE code = ?", (code,)).fetchone()
        assert row is not None, f"promo {code} not found"
        return row["id"]
    finally:
        conn.close()


def _mark_paid(order_id: str):
    """Mark an order paid directly (bypasses panel provisioning)."""
    conn = database.get_db()
    try:
        conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
            (services.now_iso(), order_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------- First-purchase (P-01) ----------------

def test_promo_first_purchase_only(test_db):
    """First-purchase-only code: history user rejected, fresh user accepted."""
    promo = _promo_code_full("FIRSTONLY", 20, first_purchase_only=1, max_uses_per_user=1)

    # User A: has a paid non-trial order (history via orders at apply time).
    user_a = _create_funded_user("histuser", 1000)
    plan_id = _plan_id_by_price(250.0)
    order = services.create_order(user_a["id"], plan_id, method="balance")
    _mark_paid(order["id"])

    # User B: fresh, no order history.
    user_b = services.create_user("freshuser", email="", password="")

    row, err = services.resolve_promo(promo, user_a["id"])
    assert row is None
    assert err == "Промокод только для первой покупки"

    row, err = services.resolve_promo(promo, user_b["id"])
    assert row is not None
    assert err == ""


def test_promo_first_purchase_race(test_db):
    """Two threads race the per-user claim on max_uses_per_user=1: one winner.

    BEGIN IMMEDIATE serializes writers; the guarded UPDATE
    (coalesce(json_extract(...), 0) < ?) makes the losing claim match
    zero rows → rowcount 0 → OrderError (P-02 claim-then-effect).
    """
    promo = _promo_code_full("RACEPROMO", 20, max_uses_per_user=1)
    promo_id = _promo_id(promo)
    user = services.create_user("racer2", email="", password="")
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        barrier.wait()
        try:
            with database.tx() as tx_conn:
                services.claim_promo_usage(
                    tx_conn, {"promo_code_id": promo_id, "user_id": user["id"]}
                )
            results.append("ok")
        except services.OrderError:
            results.append("limit")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1
    assert results.count("limit") == 1

    # The winner's count persists exactly once.
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT used_per_user, used_count FROM promo_codes WHERE id = ?", (promo_id,)
        ).fetchone()
        assert json.loads(row["used_per_user"]) == {user["id"]: 1}
        assert row["used_count"] == 1
    finally:
        conn.close()


# ---------------- Pre-promo original_price_rub ----------------

def test_promo_order_stores_original_price(test_db):
    """Promo orders store unit_price * qty in original_price_rub, never the discount."""
    user = services.create_user("origprice", email="", password="")
    promo = _promo_code_full("ORIG250", 20)
    plan_id = _plan_id_by_price(250.0)

    order = services.create_order(user["id"], plan_id, promo, method="platega")
    row = services.get_order(order["id"])

    assert money.to_rub(row["original_price_rub"]) == 250  # pre-promo total
    assert money.to_rub(row["amount_rub"]) == 200          # 250 * 80 // 100, floor per D-02
    assert money.to_rub(row["original_price_rub"]) > money.to_rub(row["amount_rub"])


# ---------------- Per-user usage increment (P-02/P-03) ----------------

def test_promo_claim_increments_used_per_user(test_db):
    """Claiming twice on an unlimited code increments used_per_user[user_id] to 2."""
    promo = _promo_code_full("UNLTD", 20)
    promo_id = _promo_id(promo)
    user = services.create_user("claimcnt", email="", password="")

    for _ in range(2):
        with database.tx() as tx_conn:
            services.claim_promo_usage(
                tx_conn, {"promo_code_id": promo_id, "user_id": user["id"]}
            )

    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT used_per_user, used_count FROM promo_codes WHERE id = ?", (promo_id,)
        ).fetchone()
        used = json.loads(row["used_per_user"] or "{}")
        assert used[user["id"]] == 2
        assert row["used_count"] == 2
    finally:
        conn.close()