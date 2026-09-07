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