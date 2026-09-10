"""Golden floor suite for the integer money module (FOUND-01 / D-01 / D-02 / D-03).

Pins the whole-ruble integer policy: to_rub floors toward -inf, promo math
floors per D-02, rendering uses integer thousands separators. These are the
foundation every later money test builds on.
"""
import pytest

import money


# ---------------- to_rub: floor toward -inf ----------------

def test_to_rub_floors_fractional_positive():
    assert money.to_rub(150.7) == 150


def test_to_rub_floors_half_down():
    assert money.to_rub(82.5) == 82


def test_to_rub_accepts_string():
    assert money.to_rub("250") == 250


def test_to_rub_floors_negative_toward_minus_inf():
    assert money.to_rub(-62.5) == -63


def test_to_rub_floor_boundary_0_335():
    """Fractional-ruble intermediates floor to 0 (CONTEXT whole-ruble space)."""
    assert money.to_rub(0.335) == 0


def test_to_rub_floor_boundary_2_675():
    assert money.to_rub(2.675) == 2


def test_to_rub_integer_input():
    assert money.to_rub(1800 * 12) == 21600


def test_to_rub_rejects_none():
    with pytest.raises(ValueError):
        money.to_rub(None)


def test_to_rub_rejects_bool():
    with pytest.raises(ValueError):
        money.to_rub(True)


# ---------------- apply_promo_price_rub (D-02 floor) ----------------

def test_apply_promo_percent_floors():
    """25% off 750 floors to 562 (floor(750*0.75)==562)."""
    assert money.apply_promo_price_rub(250 * 3, {"discount_percent": 25}) == 562


def test_apply_promo_amount_never_negative():
    assert money.apply_promo_price_rub(100, {"discount_amount_rub": 150.9}) == 0


def test_apply_promo_amount_floors():
    assert money.apply_promo_price_rub(750, {"discount_amount_rub": 100.9}) == 650


def test_apply_promo_no_promo_returns_base():
    assert money.apply_promo_price_rub(750, None) == 750


# ---------------- fmt_rub (integer renderer) ----------------

def test_fmt_rub_thousands_separator():
    assert money.fmt_rub(21600) == "21 600 ₽"


def test_fmt_rub_small_amount():
    assert money.fmt_rub(0) == "0 ₽"

# ---------------- Services level: Task 2 integer quote/settings/topup ----------------
import database
import services
import bot


def _plan_id_by_price(price) -> int:
    """Look up the seeded plan id (seeds: 150/250/600/1000/1800 rubles)."""
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id FROM plans WHERE price_rub = ?", (price,)).fetchone()
        assert row is not None, f"no seeded plan at price {price}"
        return row["id"]
    finally:
        conn.close()


def _promo_code(percent: int) -> str:
    """Insert and return a promo code with a given discount_percent."""
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, is_active, created_at)"
            " VALUES (?, ?, 1, ?)",
            (f"PROMO{percent}", percent, services.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return f"PROMO{percent}"


def test_quote_order_returns_integer_price(test_db):
    """quote_order for a 250-ruble plan ×3 returns price 750 as int, never float."""
    plan_id = _plan_id_by_price(250.0)
    quote = services.quote_order(plan_id, quantity=3)
    assert quote["price"] == 750
    assert isinstance(quote["price"], int)
    assert quote["base_price"] == 250
    assert isinstance(quote["base_price"], int)


def test_quote_order_applies_promo_with_floor(test_db):
    """25% off 750 → 562 (int); floor per D-02."""
    plan_id = _plan_id_by_price(250.0)
    promo = _promo_code(25)
    quote = services.quote_order(plan_id, promo_code=promo, quantity=3)
    assert quote["price"] == 562
    assert isinstance(quote["price"], int)


def test_get_setting_int_reads_integer(test_db):
    """DB stores settings as strings; get_setting_int must return ints."""
    assert services.get_setting_int("referral_commission_percent", 25) == 25
    assert isinstance(services.get_setting_int("referral_commission_percent", 25), int)
    assert services.get_setting_int("referral_threshold", 100) == 100
    assert isinstance(services.get_setting_int("referral_threshold", 100), int)


def test_apply_promo_price_thin_delegate(test_db):
    """apply_promo_price now a thin delegate to money.apply_promo_price_rub."""
    user = services.create_user("promo_user", email="", password="")
    promo, err = services.resolve_promo(_promo_code(25), user["id"])
    assert err == ""
    result = services.apply_promo_price(750, promo)
    assert result == 562
    assert isinstance(result, int)


def test_create_topup_order_integer_amount(test_db):
    """create_topup_order with form amount '300' stores int 300."""
    user = services.create_user("topper", email="", password="")
    order = services.create_topup_order(user["id"], "300")
    assert order["amount_rub"] == 300
    assert isinstance(order["amount_rub"], int)


def test_bot_money_renderer_matches_fmt_rub():
    assert bot.money(21600) == money.fmt_rub(21600) == "21 600 ₽"


def test_no_round_in_money_paths():
    """Structural gate: no round() inside quote_order/apply_promo_price/create_order/create_topup_order."""
    src = open("services.py", encoding="utf-8").read()
    for name in ("quote_order", "apply_promo_price", "create_order", "create_topup_order"):
        i = src.find(f"def {name}(")
        assert i != -1, f"{name} not found in services.py"
        j = src.find("\ndef ", i + 1)
        body = src[i : len(src) if j == -1 else j]
        assert "round(" not in body, f"round( remains inside {name} body"
