"""PROMO-03 edge-case tests: date-window activation and single-promo invariant.

Proves the date-bounds behavior of resolve_promo end-to-end against the
isolated `test_db` fixture: codes activate and deactivate on their UTC
`valid_from`/`valid_until` with INCLUSIVE bounds (`now >= valid_from AND now
<= valid_until`, P-04) and NULL = no bound, returning the specific per-side
Russian message (P-06 step 2). Also pins the single-promo-per-order invariant
(P-07/P-10) as a schema + flow fact: exactly one `promo_code_id` column,
one `promo_code` parameter end-to-end, and a promo'd order carries exactly
one non-NULL promo id.
"""
import inspect
from datetime import timedelta

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


# ---------------- Date window (PROMO-03 / P-04, inclusive bounds) ----------------

def test_promo_date_window(test_db, monkeypatch):
    """Inclusive edges at both bounds, NULL = unbounded, single-moment window.

    The canonical clock is frozen at FROZEN so "exactly at the boundary" is
    deterministic: resolve_promo's internal now_iso() == FROZEN, so
    `now > valid_until` / `now < valid_from` use the same instants as the
    stored bounds. A code valid_from == valid_until == FROZEN IS valid at
    that single moment (inclusive `>=`/`<=`, P-04).
    """
    from datetime import datetime, timezone

    FROZEN = datetime(2026, 12, 1, 10, 30, 0, 0, tzinfo=timezone.utc)
    FROZEN_ISO = FROZEN.isoformat()  # ends with +00:00 — canonical format
    monkeypatch.setattr(services, "now_iso", lambda: FROZEN_ISO)

    plan_id = _plan_id_by_price(250.0)
    user = services.create_user("winuser", email="", password="")

    # (a) valid_until = now exactly → valid (inclusive upper edge).
    #     now(FROZEN) > valid_until(FROZEN) is FALSE → not expired.
    _promo_code_full("UNTILNOW", 10, valid_from=None, valid_until=FROZEN_ISO)
    row, err = services.resolve_promo("UNTILNOW", user["id"])
    assert row is not None
    assert err == ""

    # (b) valid_until = now - 1 second → expired with the specific message.
    until_past = (FROZEN - timedelta(seconds=1)).isoformat()
    _promo_code_full("UNTILPAST", 10, valid_from=None, valid_until=until_past)
    row, err = services.resolve_promo("UNTILPAST", user["id"])
    assert row is None
    assert err == "Срок действия промокода истёк"

    # (c) valid_from = now exactly → valid (inclusive lower edge).
    #     now(FROZEN) < valid_from(FROZEN) is FALSE → not "not yet active".
    _promo_code_full("FROMNOW", 10, valid_from=FROZEN_ISO, valid_until=None)
    row, err = services.resolve_promo("FROMNOW", user["id"])
    assert row is not None
    assert err == ""

    # (d) valid_from = now + 1 second → not yet active with the specific message.
    from_future = (FROZEN + timedelta(seconds=1)).isoformat()
    _promo_code_full("FROMFUT", 10, valid_from=from_future, valid_until=None)
    row, err = services.resolve_promo("FROMFUT", user["id"])
    assert row is None
    assert err == "Промокод ещё не активен"

    # (e) both NULL → valid (no bound).
    _promo_code_full("NOLIMITS", 10, valid_from=None, valid_until=None)
    row, err = services.resolve_promo("NOLIMITS", user["id"])
    assert row is not None
    assert err == ""

    # (f) valid_from == valid_until == now → valid (single-moment window).
    _promo_code_full("MOMENT", 10, valid_from=FROZEN_ISO, valid_until=FROZEN_ISO)
    row, err = services.resolve_promo("MOMENT", user["id"])
    assert row is not None
    assert err == ""


# ---------------- Single-promo invariant (P-07 / P-10) ----------------

def test_promo_stack_rejected(test_db):
    """P-07 contract: one promo_code_id column, one promo_code param, one id stored."""
    # 1. The orders schema has EXACTLY ONE promo_code_id column (a single INTEGER).
    conn = database.get_db()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        promo_cols = [c for c in cols if c == "promo_code_id"]
        assert len(promo_cols) == 1, f"expected exactly one promo_code_id column, got {promo_cols}"
    finally:
        conn.close()

    # 2. No second-code parameter exists in the order-facing functions.
    create_params = list(inspect.signature(services.create_order).parameters)
    quote_params = list(inspect.signature(services.quote_order).parameters)
    for params in (create_params, quote_params):
        promo_codes = [p for p in params if p.startswith("promo_code")]
        assert len(promo_codes) == 1, f"expected a single promo code param, got {promo_codes}"
        assert promo_codes[0] == "promo_code"

    # 3. A promo'd order stores exactly one non-NULL promo_code_id.
    user = services.create_user("stackuser", email="", password="")
    plan_id = _plan_id_by_price(250.0)
    _promo_code_full("SUM25", 25)
    order = services.create_order(user["id"], plan_id, promo_code="SUM25", method="platega")
    assert order["promo_code_id"] is not None
    row = services.get_order(order["id"])
    assert row["promo_code_id"] is not None