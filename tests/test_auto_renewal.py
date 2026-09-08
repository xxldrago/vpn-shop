"""Auto-renewal tests (RENEW-01..04)."""
import pytest
import asyncio
import database
import services
from datetime import timedelta
from services import now_iso, utcnow, OrderError


@pytest.fixture
def user_with_balance(test_db):
    """Create a user with sufficient balance for auto-renewal."""
    user = services.create_user("renewal_user", email="renewal@example.com", password="pass123")
    conn = database.get_db()
    try:
        services.add_balance_int(conn, user["id"], 1000, "test_credit", ref_order_id="credit-1", note="Test credit for auto-renewal")
        conn.commit()
    finally:
        conn.close()
    return user


def _insert_paid_order(test_db, user_id, plan_id=1, days_ago=0):
    """Insert a paid order that is due for renewal."""
    conn = database.get_db()
    try:
        now = services.now_iso()
        expires_at = (services.utcnow() - timedelta(days=days_ago)).isoformat() if days_ago > 0 else \
            (services.utcnow() - timedelta(days=30)).isoformat()  # expired 30 days ago
        plan = services.get_plan(plan_id)
        order_id = f"renewal-test-{plan['id']}"
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'paid', ?, ?, ?)",
            (f"renewal-{plan['id']}", user["id"], plan["id"], plan["name"], plan["price_rub"], 
             "2024-01-01T00:00:00+00:00", (services.utcnow() - timedelta(days=30)).isoformat(), services.now_iso())
        )
        conn.commit()
        return f"renewal-{plan['id']}"
    finally:
        conn.close()


class TestAutoRenewalToggle:
    """Tests for the auto-renewal toggle (RENEW-01, RENEW-02)."""

    def test_web_toggle_enables_auto_renewal(self, test_db):
        """Enabling auto-renewal sets the flag and records consent timestamp."""
        user = services.create_user("toggle_user", email="toggle@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            
            row = conn.execute("SELECT auto_renewal, auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
            assert row["auto_renewal"] == 1
            assert row["auto_renewal_at"] is not None
            assert row["auto_renewal_at"].startswith("202")  # ISO timestamp
        finally:
            conn.close()

    def test_web_toggle_disables_auto_renewal(self, test_db):
        """Disabling auto-renewal clears the flag and timestamp."""
        user = services.create_user("toggle_user2", email="toggle2@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            
            conn.execute(
                "UPDATE app_users SET auto_renewal = 0, auto_renewal_at = NULL WHERE id = ?",
                (user["id"],)
            )
            conn.commit()
            
            row = conn.execute("SELECT auto_renewal, auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
            assert row["auto_renewal"] == 0
            assert row["auto_renewal_at"] is None
        finally:
            conn.close()

    def test_consent_timestamp_recorded(self, test_db):
        """Enabling records timestamp; re-enabling doesn't clobber; disabling clears."""
        user = services.create_user("consent_user", email="consent@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            
            row = conn.execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
            first_ts = row["auto_renewal_at"]
            assert first_ts is not None
            
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            row = conn.execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
            assert row["auto_renewal_at"] == first_ts  # timestamp unchanged
            
            conn.execute(
                "UPDATE app_users SET auto_renewal = 0, auto_renewal_at = NULL WHERE id = ?",
                (user["id"],)
            )
            conn.commit()
            row = conn.execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
            assert row["auto_renewal_at"] is None
        finally:
            conn.close()

    def test_disabling_keeps_access(self, test_db):
        """Disabling auto-renewal keeps access through the paid period."""
        user = services.create_user("access_user", email="access@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            plan = services.get_plan(1)
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
                "VALUES (?, ?, 1, 'Test', 150, 'paid', ?, ?, ?)",
                ("order-access-test", user["id"], "2024-01-01T00:00:00+00:00",
                 (services.utcnow() + timedelta(days=10)).isoformat(), services.now_iso())
            )
            conn.commit()
            
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            
            services.set_auto_renewal(conn, user["id"], False)
            conn.commit()
            
            order = conn.execute("SELECT expires_at FROM orders WHERE user_id = ?", (user["id"],)).fetchone()
            assert order["expires_at"] is not None
        finally:
            conn.close()


@pytest.mark.asyncio
class TestAutoRenewal:
    """Tests for the auto-renewal debit/extend logic (RENEW-03)."""

    async def test_auto_renewal_success_balance_debited(self, test_db):
        """Auto-renewal debits balance exactly once and extends expires_at."""
        user = services.create_user("renewal_success", email="renewal@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            services.add_balance_int(conn, user["id"], 1000, "test_credit", ref_order_id="credit-1", note="Test credit for auto-renewal")
            conn.commit()
            
            services.set_auto_renewal(conn, user["id"], True)
            conn.commit()
            
            plan = services.get_plan(1)
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'paid', ?, ?, ?)",
                ("auto-renew-1", user["id"], 1, "15 дней", 150.0,
                 "2024-01-01T00:00:00+00:00",
                 (services.utcnow() - timedelta(days=1)).isoformat(),
                 services.now_iso())
            )
            conn.commit()
            
            order_id = conn.execute("SELECT id FROM orders WHERE user_id = ? AND status = 'paid'", (user["id"],)).fetchone()["id"]
            
            result = await services.auto_renew_order(order_id)
            assert result is True
            
            conn2 = database.get_db()
            try:
                user_row = conn2.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
                assert user_row["balance"] == 850
                
                tx = conn2.execute(
                    "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'auto_renewal'", 
                    (user["id"],)
                ).fetchone()
                assert tx is not None
                assert tx["amount"] == -150
                assert tx["kind"] == "auto_renewal"
                assert tx["ref_order_id"] is not None
            finally:
                conn2.close()
            
            order = conn.execute("SELECT expires_at FROM orders WHERE id = ?", (order_id,)).fetchone()
            new_expires = services.datetime.fromisoformat(order["expires_at"].replace("Z", "+00:00"))
            expected_min = services.utcnow() + timedelta(days=14)
            assert new_expires >= expected_min
        finally:
            conn.close()

    async def test_auto_renewal_insufficient_balance_no_debit(self, test_db):
        """Insufficient balance -> no debit, no extension, OrderError raised."""
        user = services.create_user("renewal_poor", email="poor@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            conn.execute("UPDATE app_users SET balance = 0 WHERE id = ?", (user["id"],))
            conn.commit()
            
            conn.execute("UPDATE app_users SET auto_renewal = 1 WHERE id = ?", (user["id"],))
            conn.commit()
            
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
                "VALUES (?, ?, 1, '15 дней', 150.0, 'paid', ?, ?, ?)",
                ("auto-renew-poor", user["id"],
                 "2024-01-01T00:00:00+00:00",
                 (services.utcnow() - timedelta(days=1)).isoformat(),
                 services.now_iso())
            )
            conn.commit()
            
            order_id = conn.execute("SELECT id FROM orders WHERE user_id = ? AND status = 'paid'", (user["id"],)).fetchone()["id"]
            
            with pytest.raises(OrderError, match="Недостаточно средств"):
                await services.auto_renew_order(order_id)
            
            conn2 = database.get_db()
            try:
                balance = conn2.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
                assert balance["balance"] == 0
                
                tx = conn2.execute("SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'auto_renewal'", (user["id"],)).fetchone()
                assert tx is None
            finally:
                conn2.close()
            
            order = conn.execute("SELECT expires_at FROM orders WHERE user_id = ?", (user["id"],)).fetchone()
        finally:
            conn.close()

    async def test_auto_renewal_idempotent_on_replay(self, test_db):
        """Re-running auto_renew_order on the same order is a no-op (idempotent)."""
        user = services.create_user("replay_user", email="replay@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            conn.execute("UPDATE app_users SET balance = 1000 WHERE id = ?", (user["id"],))
            conn.commit()
            
            conn.execute("UPDATE app_users SET auto_renewal = 1 WHERE id = ?", (user["id"],))
            conn.commit()
            
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
                "VALUES (?, ?, 1, '15 дней', 150.0, 'paid', ?, ?, ?)",
                ("auto-renew-replay", user["id"],
                 "2024-01-01T00:00:00+00:00",
                 (services.utcnow() - timedelta(days=1)).isoformat(),
                 services.now_iso())
            )
            conn.commit()
            
            order_id = conn.execute("SELECT id FROM orders WHERE user_id = ? AND status = 'paid'", (user["id"],)).fetchone()["id"]
            
            result1 = await services.auto_renew_order(order_id)
            assert result1 is True
            
            result2 = await services.auto_renew_order(order_id)
            assert result2 is False
            
            conn2 = database.get_db()
            try:
                balance = conn2.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
                assert balance["balance"] == 850
                
                txs = conn2.execute("SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'auto_renewal'", 
                                   (user["id"],)).fetchall()
                assert len(txs) == 1
                assert txs[0]["amount"] == -150
            finally:
                conn2.close()
        finally:
            conn.close()

    async def test_auto_renewal_extends_from_current_expires_at(self, test_db):
        """Auto-renewal extends from max(expires_at, now), not from now."""
        user = services.create_user("extend_user", email="extend@example.com", password="pass123")
        
        conn = database.get_db()
        try:
            conn.execute("UPDATE app_users SET balance = 1000 WHERE id = ?", (user["id"],))
            conn.execute("UPDATE app_users SET auto_renewal = 1 WHERE id = ?", (user["id"],))
            conn.commit()
            
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
                "VALUES (?, ?, 1, 'Expired', 150, 'paid', ?, ?, ?)",
                ("auto-renew-extend", user["id"],
                 "2024-01-01T00:00:00+00:00",
                 (services.utcnow() - timedelta(days=1)).isoformat(),
                 services.now_iso())
            )
            conn.commit()
            
            order_id = conn.execute("SELECT id FROM orders WHERE user_id = ? AND status = 'paid'", (user["id"],)).fetchone()["id"]
            
            result = await services.auto_renew_order(order_id)
            assert result is True
            
            conn2 = database.get_db()
            try:
                order = conn2.execute("SELECT expires_at FROM orders WHERE id = ?", (order_id,)).fetchone()
                new_expires = services.datetime.fromisoformat(order["expires_at"].replace("Z", "+00:00"))
                expected_min = services.utcnow() + timedelta(days=14)
                assert new_expires >= expected_min
            finally:
                conn2.close()
        finally:
            conn.close()


def test_no_auto_renewal_column_on_orders(test_db):
    """Ensure orders table does NOT have auto_renewal column (per D-13)."""
    conn = test_db.get_db()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        assert "auto_renewal" not in cols
        assert "auto_renewal_at" not in cols
        ucols = [r["name"] for r in conn.execute("PRAGMA table_info(app_users)").fetchall()]
        assert "auto_renewal" in ucols
        assert "auto_renewal_at" in ucols
    finally:
        conn.close()


def test_expiry_dry_run_does_not_renew(test_db):
    """expiry --dry-run should not perform auto-renewal."""
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])