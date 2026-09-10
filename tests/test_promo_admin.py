"""Admin promo form field persistence + PROMO-04 report (P-12 / T-05-01 / T-05-02).

Pins the exact INSERT shape `admin_promos_add` now executes (with the
first_purchase_only column) against the isolated `test_db`: a code created
with first_purchase_only=1 reads back as 1 with the default used_per_user '{}'.
Direct DB asserts per 03-PATTERNS.md recommendation (a) — no TestClient,
session auth not needed for a schema/persistence check. Also pins the pure
datetime helpers `_parse_promo_datetime` / `_promo_window_valid` and the
lossless UTC persistence of the converted window bounds (T-03-10/T-03-11).

The report golden test (`test_promo_admin_report`) seeds paid orders via
both payment methods and asserts the corrected arithmetic (original − paid
discount, paid revenue) plus unused-code zeros and the pending-order
exclusion. The unauth route gate (`test_report_route_admin_only`) is added
in 03-04 Task 2.
"""
from datetime import datetime, timedelta

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


def test_promo_admin_form_fields(test_db):
    """Route-shaped INSERT with first_purchase_only=1 persists flag + used_per_user default."""
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, discount_amount_rub, max_uses, first_purchase_only, valid_from, valid_until, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            ("ADMINFP", 15, 0, None, 1, None, None, services.now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT first_purchase_only, used_per_user FROM promo_codes WHERE code = ?",
            ("ADMINFP",),
        ).fetchone()
        assert row is not None
        assert row["first_purchase_only"] == 1
        assert row["used_per_user"] == "{}"
    finally:
        conn.close()


def test_promo_admin_form_default_is_zero(test_db):
    """The unchecked checkbox case (Form default 0) stores first_purchase_only=0."""
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, discount_amount_rub, max_uses, first_purchase_only, valid_from, valid_until, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            ("ADMINDEF", 10, 0, None, 0, None, None, services.now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT first_purchase_only FROM promo_codes WHERE code = ?", ("ADMINDEF",)
        ).fetchone()
        assert row is not None
        assert row["first_purchase_only"] == 0
    finally:
        conn.close()


def test_promo_admin_max_uses_per_user_persistence(test_db):
    """max_uses_per_user=3 persists as 3; 0 converts to NULL via `or None` (P-03).

    Route-shaped INSERT: the bound value mirrors admin_promos_add's
    `max_uses_per_user or None` conversion — a set value stays a set value,
    zero/empty means unlimited (NULL), never a 0 that would block everyone.
    """
    conn = database.get_db()
    try:
        for code, raw in (("ADMINPU3", 3), ("ADMINPU0", 0)):
            conn.execute(
                "INSERT INTO promo_codes (code, discount_percent, discount_amount_rub, max_uses,"
                " first_purchase_only, max_uses_per_user, valid_from, valid_until, is_active, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (code, 15, 0, None, 0, raw or None, None, None, services.now_iso()),
            )
        conn.commit()
        row3 = conn.execute(
            "SELECT max_uses_per_user FROM promo_codes WHERE code = ?", ("ADMINPU3",)
        ).fetchone()
        row0 = conn.execute(
            "SELECT max_uses_per_user FROM promo_codes WHERE code = ?", ("ADMINPU0",)
        ).fetchone()
        assert row3 is not None and row3["max_uses_per_user"] == 3
        assert row0 is not None and row0["max_uses_per_user"] is None
    finally:
        conn.close()


def test_promo_parse_datetime_converts_to_utc(test_db):
    """datetime-local input is interpreted as server-local and shifted to UTC +00:00.

    `_parse_promo_datetime("2026-12-01T10:30")` returns a string parseable by
    datetime.fromisoformat that is tz-aware with a +00:00 offset; empty input → None.
    """
    parsed = app_module._parse_promo_datetime("2026-12-01T10:30")
    assert parsed is not None
    assert parsed.endswith("+00:00")
    dt = datetime.fromisoformat(parsed)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)

    # Empty / whitespace → None (no bound).
    assert app_module._parse_promo_datetime("") is None
    assert app_module._parse_promo_datetime("   ") is None


def test_promo_window_ordering_validation(test_db):
    """valid_from <= valid_until; reversed is invalid; either NULL is always valid (P-12)."""
    valid_from = "2026-12-01T00:00:00+00:00"
    valid_until = "2026-12-31T00:00:00+00:00"
    assert app_module._promo_window_valid(valid_from, valid_until) is True
    assert app_module._promo_window_valid(valid_until, valid_from) is False
    # Either/NULL bound → no ordering constraint.
    assert app_module._promo_window_valid(None, valid_until) is True
    assert app_module._promo_window_valid(valid_from, None) is True
    assert app_module._promo_window_valid(None, None) is True


def test_promo_admin_form_datetime_persists_utc(test_db):
    """A converted +00:00 bound persists losslessly (not the raw server-local string).

    Route-shaped INSERT binding `_parse_promo_datetime(...)`: the stored value
    equals the converted UTC string, so gating compares against the canonical
    now_iso() format (T-03-10).
    """
    converted = app_module._parse_promo_datetime("2026-12-01T10:30")
    assert converted is not None
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, discount_amount_rub, max_uses, first_purchase_only, valid_from, valid_until, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            ("ADMINUTC", 15, 0, None, 0, converted, None, services.now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT valid_from FROM promo_codes WHERE code = ?", ("ADMINUTC",)
        ).fetchone()
        assert row is not None
        assert row["valid_from"] == converted
        assert row["valid_from"].endswith("+00:00")
    finally:
        conn.close()


def test_promo_admin_report(test_db):
    """Golden arithmetic: SUM20 paid both methods → 2/100/400; NEVERUSED → 0/0/0.

    Plan 250 ₽, SUM20 (20% off, floor per D-02) → paid 200. User A pays via
    platega (amount_rub=200, balance_used_rub=0); user B via balance
    (amount_rub=0, balance_used_rub=200). total_discount = (250−200) +
    (250−200) = 100; revenue_after_promo = 200+200 = 400. A pending (unpaid)
    promo order must NOT change those numbers — the JOIN restricts
    status='paid'. NEVERUSED has no orders so the LEFT JOIN yields zeros.
    """
    plan_id = _plan_id_by_price(250)
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, max_uses_per_user, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            ("SUM20", 20, None, services.now_iso()),
        )
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, is_active, created_at)"
            " VALUES (?, ?, 1, ?)",
            ("NEVERUSED", 10, services.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    user_a = services.create_user("report_a", email="", password="")
    user_b = services.create_user("report_b", email="", password="")
    user_pending = services.create_user("report_pending", email="", password="")
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = ? WHERE id = ?", (1000, user_b["id"]))
        conn.commit()
    finally:
        conn.close()

    order_a = services.create_order(user_a["id"], plan_id, "SUM20", method="platega")
    _mark_paid(order_a["id"])
    order_b = services.create_order(user_b["id"], plan_id, "SUM20", method="balance")
    _mark_paid(order_b["id"])
    # Pending promo order — must not be counted.
    services.create_order(user_pending["id"], plan_id, "SUM20", method="platega")

    report = {r["code"]: r for r in services.get_promo_report()}
    assert "SUM20" in report
    assert "NEVERUSED" in report
    assert report["SUM20"]["orders_count"] == 2
    assert report["SUM20"]["total_discount"] == 100
    assert report["SUM20"]["revenue_after_promo"] == 400
    assert report["NEVERUSED"]["orders_count"] == 0
    assert report["NEVERUSED"]["total_discount"] == 0
    assert report["NEVERUSED"]["revenue_after_promo"] == 0
    # Money-boundary ints, never floats.
    assert isinstance(report["SUM20"]["total_discount"], int)
    assert isinstance(report["SUM20"]["revenue_after_promo"], int)


def test_report_route_admin_only(test_db):
    """GET /admin/promos/report without admin auth returns 302/401/403, no report data.

    The route starts with `require_admin`, so unauthenticated or non-admin
    users are denied; the response should not contain the report title or
    data. This pins the gate.
    """
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    resp = client.get("/admin/promos/report")
    # Unauthenticated → redirect (302) or unauthorized (401/403)
    assert resp.status_code in (302, 401, 403), f"expected redirect or auth error, got {resp.status_code}"
    # Verify the response does NOT contain report content
    assert "Отчёт по промокодам" not in resp.text
    assert "Скидка" not in resp.text
