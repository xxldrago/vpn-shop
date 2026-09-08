"""Tests for reminder scan and auto-renewal failure recording (RENEW-04)."""
import pytest
import asyncio
import database
import services
import jobs
import bot
from datetime import timedelta


def test_reminder_sent_3_days_before(test_db, monkeypatch):
    """A user with auto_renewal ON, a telegram_id, and an order ~3 days out
    gets a notifications row kind='reminder' and the bot send is attempted."""
    user = services.create_user("reminder_user", email="reminder@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET auto_renewal = 1, telegram_id = ? WHERE id = ?",
                     ("123456789", user["id"]))
        conn.commit()
        
        # Order expiring in 3 days (within the dedup window)
        expires_at = (services.utcnow() + timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("reminder-order", user["id"], "2024-01-01T00:00:00+00:00", expires_at, services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    sends = []
    async def mock_send(telegram_id, text):
        sends.append((telegram_id, text))
        return True
    monkeypatch.setattr(bot, "send_message_to_user", mock_send)
    
    asyncio.run(jobs.run_reminder_scan())
    
    # Web notification written
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND kind = 'reminder'",
            (user["id"],)
        ).fetchone()
        assert row is not None
        assert row["title"] == "Скоро истекает подписка"
        assert row["order_id"] == "reminder-order"
    finally:
        conn.close()
    
    # Telegram push attempted
    assert len(sends) == 1
    assert sends[0][0] == "123456789"


def test_reminder_not_duplicated(test_db, monkeypatch):
    """Running the reminder scan twice for the same order produces exactly one reminder row."""
    user = services.create_user("reminder_dedup", email="dedup@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET auto_renewal = 1, telegram_id = ? WHERE id = ?",
                     ("987654321", user["id"]))
        conn.commit()
        
        expires_at = (services.utcnow() + timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("dedup-order", user["id"], "2024-01-01T00:00:00+00:00", expires_at, services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    sends = []
    async def mock_send(telegram_id, text):
        sends.append((telegram_id, text))
        return True
    monkeypatch.setattr(bot, "send_message_to_user", mock_send)
    
    # First scan
    asyncio.run(jobs.run_reminder_scan())
    # Second scan
    asyncio.run(jobs.run_reminder_scan())
    
    conn = database.get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND kind = 'reminder'",
            (user["id"],)
        ).fetchall()
        assert len(rows) == 1  # exactly one reminder, not duplicated
    finally:
        conn.close()
    
    # Only one send (deduped)
    assert len(sends) == 1


def test_reminder_both_channels(test_db, monkeypatch):
    """Reminder writes web notification row AND sends Telegram push (dual-channel)."""
    user = services.create_user("reminder_both", email="both@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET auto_renewal = 1, telegram_id = ? WHERE id = ?",
                     ("555123456", user["id"]))
        conn.commit()
        
        expires_at = (services.utcnow() + timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("both-order", user["id"], "2024-01-01T00:00:00+00:00", expires_at, services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    sends = []
    async def mock_send(telegram_id, text):
        sends.append((telegram_id, text))
        return True
    monkeypatch.setattr(bot, "send_message_to_user", mock_send)
    
    asyncio.run(jobs.run_reminder_scan())
    
    # Web notification
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND kind = 'reminder'",
            (user["id"],)
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
    
    # Telegram push
    assert len(sends) == 1
    assert sends[0][0] == "555123456"


def test_auto_renewal_failure_records_notification(test_db, monkeypatch):
    """Insufficient balance -> OrderError -> notification row written, no debit, no extension."""
    user = services.create_user("renewal_fail", email="fail@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        # Zero balance
        conn.execute("UPDATE app_users SET balance = 0, auto_renewal = 1 WHERE id = ?", (user["id"],))
        conn.commit()
        
        # Due order
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("fail-order", user["id"], "2024-01-01T00:00:00+00:00",
             (services.utcnow() - timedelta(days=1)).isoformat(), services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    # Mock panel client
    class PanelMock:
        users = {}
        async def find_user_by_username(self, username):
            return {"id": username, "username": username}
        async def update_panel_user(self, uid, **fields):
            pass
    monkeypatch.setattr(services, "get_panel_client", lambda: PanelMock())
    
    # run_auto_renewal should not throw
    asyncio.run(jobs.run_auto_renewal())
    
    # Notification recorded
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND kind = 'auto_renewal_failed'",
            (user["id"],)
        ).fetchone()
        assert row is not None
        assert "Недостаточно средств" in row["message"]
    finally:
        conn.close()
    
    # No debit, no extension
    conn2 = database.get_db()
    try:
        balance = conn2.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert balance["balance"] == 0
        
        tx = conn2.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'auto_renewal'",
            (user["id"],)
        ).fetchone()
        assert tx is None
    finally:
        conn2.close()


def test_auto_renewal_dry_run_is_write_free(test_db, monkeypatch):
    """run_job("auto-renewal", dry_run=True) prints candidates and writes nothing."""
    user = services.create_user("dryrun_user", email="dryrun@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = 1000, auto_renewal = 1 WHERE id = ?", (user["id"],))
        conn.commit()
        
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("dryrun-order", user["id"], "2024-01-01T00:00:00+00:00",
             (services.utcnow() - timedelta(days=1)).isoformat(), services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    # Mock panel client
    class PanelMock:
        users = {}
        async def find_user_by_username(self, username):
            return {"id": username, "username": username}
        async def update_panel_user(self, uid, **fields):
            pass
    monkeypatch.setattr(services, "get_panel_client", lambda: PanelMock())
    
    assert jobs.run_job("auto-renewal", dry_run=True) == 0
    
    # No job_runs written
    conn = database.get_db()
    try:
        runs = conn.execute("SELECT * FROM job_runs WHERE job_name = 'auto-renewal'").fetchall()
        assert len(runs) == 0
        
        # No debit
        balance = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert balance["balance"] == 1000
        
        # No notification
        rows = conn.execute("SELECT * FROM notifications WHERE user_id = ?", (user["id"],)).fetchall()
        assert len(rows) == 0
    finally:
        conn.close()


def test_expiry_dry_run_does_not_renew(test_db, monkeypatch):
    """run_job("expiry", dry_run=True) does NOT perform auto-renewal (dry-run passed through)."""
    user = services.create_user("expiry_dryrun", email="expirydry@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = 1000, auto_renewal = 1 WHERE id = ?", (user["id"],))
        conn.commit()
        
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("expiry-dryrun-order", user["id"], "2024-01-01T00:00:00+00:00",
             (services.utcnow() - timedelta(days=1)).isoformat(), services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    # Mock panel client
    class PanelMock:
        users = {}
        async def find_user_by_username(self, username):
            return {"id": username, "username": username}
        async def update_panel_user(self, uid, **fields):
            pass
    monkeypatch.setattr(services, "get_panel_client", lambda: PanelMock())
    
    assert jobs.run_job("expiry", dry_run=True) == 0
    
    # No job_runs written
    conn = database.get_db()
    try:
        runs = conn.execute("SELECT * FROM job_runs WHERE job_name = 'expiry'").fetchall()
        assert len(runs) == 0
        
        # No debit, no extension
        balance = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert balance["balance"] == 1000
    finally:
        conn.close()


def test_auto_renewal_job_renews_due_order(test_db, monkeypatch):
    """run_job("auto-renewal") renews due order with sufficient balance."""
    user = services.create_user("job_renew", email="job@example.com", password="pass123")
    
    conn = database.get_db()
    try:
        conn.execute("UPDATE app_users SET balance = 1000, auto_renewal = 1 WHERE id = ?", (user["id"],))
        conn.commit()
        
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, paid_at, expires_at, created_at) "
            "VALUES (?, ?, 1, '15 дней', 150, 'paid', ?, ?, ?)",
            ("job-order", user["id"], "2024-01-01T00:00:00+00:00",
             (services.utcnow() - timedelta(days=1)).isoformat(), services.now_iso())
        )
        conn.commit()
    finally:
        conn.close()
    
    # Mock panel client
    class PanelMock:
        users = {}
        async def find_user_by_username(self, username):
            return {"id": username, "username": username}
        async def update_panel_user(self, uid, **fields):
            pass
    monkeypatch.setattr(services, "get_panel_client", lambda: PanelMock())
    
    assert jobs.run_job("auto-renewal") == 0
    
    conn = database.get_db()
    try:
        # Balance debited
        balance = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        assert balance["balance"] == 850
        
        # Auto-renewal ledger row
        tx = conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = 'auto_renewal'",
            (user["id"],)
        ).fetchone()
        assert tx is not None
        assert tx["amount"] == -150
        
        # job_runs recorded
        runs = conn.execute("SELECT * FROM job_runs WHERE job_name = 'auto-renewal' AND ok = 1").fetchall()
        assert len(runs) == 1
    finally:
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])