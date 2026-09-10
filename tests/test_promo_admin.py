"""Admin promo form field persistence (P-12 / T-05-02).

Pins the exact INSERT shape `admin_promos_add` now executes (with the
first_purchase_only column) against the isolated `test_db`: a code created
with first_purchase_only=1 reads back as 1 with the default used_per_user '{}'.
Direct DB asserts per 03-PATTERNS.md recommendation (a) — no TestClient,
session auth not needed for a schema/persistence check.
"""
import database
import services


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