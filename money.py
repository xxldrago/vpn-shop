"""Integer whole-ruble money math (D-01/D-02/D-03, FOUND-01).

All money in the shop is an INTEGER number of whole rubles — never
float/REAL and never kopecks. Fractional results FLOOR toward -inf
(D-02), and the same floor policy applies to external (Platega/form)
amounts at the boundary (D-03). Units/prices computed as ints end-to-end.

This module is the single source of truth for money conversion,
promo math and rendering. The DB columns stay REAL until the plan-07
storage migration; callers convert at read/write time with money.to_rub().
"""
import math
from decimal import Decimal, ROUND_FLOOR


def to_rub(x) -> int:
    """Floor any incoming amount (float/str/Decimal/int) to whole rubles.

    Raises ValueError on bool/None (per RESEARCH) — a bool is not a money
    value and silently converting it would mask programming errors.
    """
    if isinstance(x, bool) or x is None:
        raise ValueError("invalid money value")
    return int(Decimal(str(x)).to_integral_value(rounding=ROUND_FLOOR))


def fmt_rub(n: int) -> str:
    """Render an integer ruble amount with a space thousands separator."""
    return f"{n:,}".replace(",", " ") + " ₽"


def apply_promo_price_rub(base_price: int, promo: dict | None) -> int:
    """Apply a promo discount to an integer price, flooring per D-02.

    base_price is already an int (whole rubles); the returned price is an
    int. Percent discounts floor the discounted price (250*3 @ 25% ->
    floor(750*0.75) == 562); fixed-amount discounts never go below 0.
    """
    price = base_price
    if promo:
        if promo.get("discount_percent"):
            price = math.floor(base_price * (100 - promo["discount_percent"]) / 100)
        elif promo.get("discount_amount_rub"):
            price = max(0, base_price - to_rub(promo["discount_amount_rub"]))
    return price