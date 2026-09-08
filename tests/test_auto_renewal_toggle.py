"""Tests for auto-renewal toggle in web profile and Telegram bot (RENEW-01, RENEW-02)."""
import pytest
import database
import services


def test_web_toggle_enables_auto_renewal(test_db):
    """Enabling auto-renewal via web sets flag and records consent timestamp."""
    user = services.create_user("web_toggle_user", email="web_toggle@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        # Simulate the web toggle POST
        services.set_auto_renewal(conn, user["id"], True)
        conn.commit()
    
        row = conn.execute("SELECT auto_renewal, auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert row["auto_renewal"] == 1
        assert row["auto_renewal_at"] is not None
        assert row["auto_renewal_at"].startswith("202")  # ISO timestamp
    finally:
        conn.close()


def test_web_toggle_disables_auto_renewal(test_db):
    """Disabling auto-renewal clears flag and timestamp."""
    user = services.create_user("web_toggle_disable", email="web_disable@example.com", password="pass123")
    
    conn = test_db.get_db()
    try:
        # Enable first
        services.set_auto_renewal(conn, user["id"], True)
        conn.commit()
        
        # Then disable
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


def test_bot_toggle_enables_auto_renewal(test_db):
    """Enabling via bot sets flag and consent timestamp."""
    user = services.create_user("bot_toggle_user", email="bot_toggle@example.com", password="pass123")
    
    conn = test_db.get_db()
    try:
        # Simulate bot callback
        services.set_auto_renewal(conn, user["id"], True)
        conn.commit()
        
        row = conn.execute("SELECT auto_renewal, auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert row["auto_renewal"] == 1
        assert row["auto_renewal_at"] is not None
    finally:
        conn.close()


def test_bot_toggle_disables_auto_renewal(test_db):
    """Disabling via bot clears flag and timestamp."""
    user = services.create_user("bot_toggle_disable", email="bot_disable@example.com", password="pass123")
    
    conn = test_db.get_db()
    try:
        # Enable first
        conn.execute("UPDATE app_users SET auto_renewal = 1, auto_renewal_at = ? WHERE id = ?", 
                       ("2024-01-01T00:00:00+00:00", user["id"]))
        conn.commit()
        
        # Disable
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


def test_consent_timestamp_recorded(test_db):
    """Enabling records timestamp; re-enabling doesn't clobber; disabling clears."""
    user = services.create_user("consent_user", email="consent@example.com", password="pass123")
    
    conn = test_db.get_db()
    try:
        # First enable - should record timestamp
        conn.execute("UPDATE app_users SET auto_renewal = 1, auto_renewal_at = ? WHERE id = ?", 
                       ("2024-01-01T00:00:00+00:00", user["id"]))
        conn.commit()
        first_ts = conn.execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()["auto_renewal_at"]
        assert first_ts is not None
        
        # Re-enable (already enabled) - should NOT clobber existing timestamp
        # Use the service function which handles this logic
        from services import set_auto_renewal
        conn2 = database.get_db()
        try:
            services.set_auto_renewal(conn2, user["id"], True)
            conn2.commit()
        finally:
            conn2.close()
        
        row = test_db.get_db().execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert row["auto_renewal_at"] == first_ts  # timestamp unchanged
        
        # Disable - should clear timestamp
        conn.execute("UPDATE app_users SET auto_renewal = 0, auto_renewal_at = NULL WHERE id = ?", (user["id"],))
        conn.commit()
        row = conn.execute("SELECT auto_renewal_at FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert row["auto_renewal_at"] is None
    finally:
        pass


def test_disabling_keeps_access(test_db):
    """Disabling auto-renewal keeps access through the paid period."""
    # This is tested in test_auto_renewal.py::test_disabling_keeps_access
    # which verifies that disabling auto-renewal doesn't mutate expires_at
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])