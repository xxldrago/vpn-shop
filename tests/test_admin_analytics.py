"""Admin analytics golden tests: revenue report + subscriber lists (ANAL-01/02).

Revenue arithmetic follows the locked phase decisions:
  D-01  revenue rows: status='paid' AND is_trial=0 AND plan_id IS NOT NULL
        (top-ups and trial orders excluded)
  D-02  per-order paid amount = amount_rub + balance_used_rub
  D-05  day/month buckets = substr(paid_at,1,10) / substr(paid_at,1,7) in UTC
  D-07  payment method binary: platega (amount_rub>0) / balance (else)
  D-09  active subscribers: one row per user, latest active paid order
  D-10  expiring list: paid plan orders with expires_at in (now, now+7d]
  D-11  list fields: username, email, telegram_id, plan_name, expires_at,
        days_until_expiry (computed in SQL via julianday — never parsed in
        Python, so legacy naive timestamps cannot raise TypeError)
  D-16  /admin is admin-only; no financial content for unauthenticated GETs

Direct DB asserts per the test_promo_admin.py pattern; TestClient only for
the route gate. No panel/Platega involvement — tests bypass provisioning
with direct DB updates.
"""
from datetime import timedelta

import app as app_module
import database
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


def _mark_paid_at(order_id: str, paid_at: str):
    """Mark an order paid with an explicit fixed paid_at timestamp."""
    conn = database.get_db()
    try:
        conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
            (paid_at, order_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_paid(order_id: str):
    """Mark an order paid at the canonical current time."""
    _mark_paid_at(order_id, services.now_iso())


def _set_expires_at(order_id: str, expires_at: str):
    """Set an order's expires_at directly (bypasses provisioning)."""
    conn = database.get_db()
    try:
        conn.execute("UPDATE orders SET expires_at = ? WHERE id = ?", (expires_at, order_id))
        conn.commit()
    finally:
        conn.close()


# ---------------- ANAL-01: revenue report ----------------

def test_revenue_report_golden(test_db):
    """Two paid orders on the same plan (one per method) → exact totals.

    250 ₽ plan paid twice: user A via platega (amount_rub=250,
    balance_used_rub=0), user B via balance (amount_rub=0,
    balance_used_rub=250). total_rub == today_rub == 500 (D-02);
    by_method has exactly the two D-07 rows and their revenues sum to the
    total; by_plan shows the single plan with revenue == total. Every money
    value is an int (money boundary, D-01).
    """
    plan_id = _plan_id_by_price(250)
    user_a = services.create_user("rev_a", email="", password="")
    user_b = services.create_user("rev_b", email="", password="")

    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = ? WHERE id = ?", (1000, user_b["id"]))
        conn.commit()
    finally:
        conn.close()

    order_a = services.create_order(user_a["id"], plan_id, method="platega")
    _mark_paid(order_a["id"])
    order_b = services.create_order(user_b["id"], plan_id, method="balance")
    _mark_paid(order_b["id"])

    report = services.get_revenue_report()
    assert report["total_rub"] == 500
    assert report["today_rub"] == 500

    by_method = {r["method"]: r for r in report["by_method"]}
    assert set(by_method) == {"platega", "balance"}  # D-07: exactly two rows
    assert by_method["platega"]["revenue"] == 250
    assert by_method["balance"]["revenue"] == 250
    assert sum(r["revenue"] for r in report["by_method"]) == report["total_rub"]

    assert len(report["by_plan"]) == 1
    assert report["by_plan"][0]["revenue"] == 500
    assert report["by_plan"][0]["orders_count"] == 2

    # Money-boundary ints, never floats.
    assert isinstance(report["total_rub"], int)
    assert isinstance(report["today_rub"], int)
    for row in report["by_day"] + report["by_month"] + report["by_plan"] + report["by_method"]:
        assert isinstance(row["revenue"], int)
        assert isinstance(row["orders_count"], int)


def test_revenue_day_bucketing(test_db):
    """Cross-month UTC boundary: each paid order lands in exactly one day bucket.

    paid_at '2026-08-31T23:59:59.000000+00:00' → day 2026-08-31 / month 2026-08;
    '2026-09-01T00:00:01.000000+00:00' → day 2026-09-01 / month 2026-09 (D-05).
    The by-day SUM equals total_rub — no row is lost or double-counted.
    """
    plan_id = _plan_id_by_price(250)
    user_a = services.create_user("bucket_a", email="", password="")
    user_b = services.create_user("bucket_b", email="", password="")
    order_a = services.create_order(user_a["id"], plan_id, method="platega")
    _mark_paid_at(order_a["id"], "2026-08-31T23:59:59.000000+00:00")
    order_b = services.create_order(user_b["id"], plan_id, method="platega")
    _mark_paid_at(order_b["id"], "2026-09-01T00:00:01.000000+00:00")

    report = services.get_revenue_report()
    assert report["total_rub"] == 500

    by_day = {r["day"]: r for r in report["by_day"]}
    assert "2026-08-31" in by_day
    assert "2026-09-01" in by_day
    assert by_day["2026-08-31"]["revenue"] == 250
    assert by_day["2026-09-01"]["revenue"] == 250
    assert sum(r["revenue"] for r in report["by_day"]) == report["total_rub"]

    by_month = {r["month"]: r for r in report["by_month"]}
    assert by_month["2026-08"]["revenue"] == 250
    assert by_month["2026-09"]["revenue"] == 250
    assert sum(r["revenue"] for r in report["by_month"]) == report["total_rub"]


def test_revenue_excludes_trial_and_topup(test_db):
    """Trial orders (is_trial=1) and top-up orders (plan_id NULL) are NOT revenue.

    A normal paid order IS revenue. D-01 filter verbatim — Pitfall 6 gate:
    dropping plan_id IS NOT NULL would let wallet top-ups pollute revenue.
    """
    plan_id = _plan_id_by_price(250)
    user = services.create_user("excl_user", email="", password="")

    order = services.create_order(user["id"], plan_id, method="platega")
    _mark_paid(order["id"])

    # Trial-shaped order (mirrors services activate_trial INSERT: plan_id NULL).
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, is_trial, status, paid_at, created_at)"
            " VALUES (?, ?, NULL, 'Тестовая подписка', 0, 1, 'paid', ?, ?)",
            ("trial-order-1", user["id"], services.now_iso(), services.now_iso()),
        )
        # Top-up order: plan_id NULL, paid.
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, original_price_rub,"
            " balance_used_rub, status, paid_at, created_at)"
            " VALUES (?, ?, NULL, 'Пополнение баланса', 300, 300, 0, 'paid', ?, ?)",
            ("topup-order-1", user["id"], services.now_iso(), services.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    report = services.get_revenue_report()
    assert report["total_rub"] == 250  # only the normal plan order counts
    assert report["total_rub"] != 250 + 300  # top-up excluded
    assert sum(r["revenue"] for r in report["by_plan"]) == 250


def test_revenue_report_route_admin_only(test_db):
    """GET /admin without admin auth returns 302/401/403, no financial content (D-16)."""
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    resp = client.get("/admin")
    assert resp.status_code in (302, 401, 403), f"expected redirect or auth error, got {resp.status_code}"
    assert "Выручка" not in resp.text
