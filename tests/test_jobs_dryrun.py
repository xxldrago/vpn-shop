"""Behavior tests for the flat scheduled-job CLI (FOUND-07)."""

from datetime import timedelta

import pytest

import jobs
import services
from panel_client import PanelClientError


def _user(test_db, username):
    return services.create_user(username, email="", password="")


def _insert_expired_order(test_db, user_id, order_id="expired-order"):
    with test_db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, status, "
            "paid_at, expires_at, created_at) VALUES (?, ?, 1, 'Expired', 150, 'paid', ?, ?, ?)",
            (
                order_id,
                user_id,
                services.now_iso(),
                (services.utcnow() - timedelta(days=1)).isoformat(),
                services.now_iso(),
            ),
        )
    return order_id


def _insert_pending(test_db, user_id, order_id, *, age_minutes, transaction_id, balance_used=0):
    with test_db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (id, user_id, amount_rub, balance_used_rub, status, "
            "platega_transaction_id, created_at) VALUES (?, ?, 500, ?, 'pending', ?, ?)",
            (
                order_id,
                user_id,
                balance_used,
                transaction_id,
                (services.utcnow() - timedelta(minutes=age_minutes)).isoformat(),
            ),
        )
    return order_id


class _Panel:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.users = {}

    async def find_user_by_username(self, username):
        return self.users.get(username)

    async def update_panel_user(self, uid, **fields):
        if self.fail:
            raise PanelClientError("panel unavailable")
        self.users[uid].update(fields)


class _Platega:
    def __init__(self, status):
        self.status = status

    async def get_payment_status(self, transaction_id):
        return {"status": self.status}


def _job_runs(test_db):
    conn = test_db.get_db()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM job_runs").fetchall()]
    finally:
        conn.close()


def test_expiry_dry_run_prints_plan_and_writes_nothing(test_db, capsys, monkeypatch):
    user = _user(test_db, "dry-expiry")
    order_id = _insert_expired_order(test_db, user["id"])
    monkeypatch.setattr(jobs, "get_panel_client", lambda: _Panel())

    assert jobs.run_job("expiry", dry_run=True) == 0

    output = capsys.readouterr().out
    assert order_id in output
    assert _job_runs(test_db) == []
    conn = test_db.get_db()
    try:
        assert conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()["status"] == "paid"
    finally:
        conn.close()


def test_expiry_writes_terminal_success_run(test_db, monkeypatch):
    user = _user(test_db, "live-expiry")
    order_id = _insert_expired_order(test_db, user["id"])
    panel = _Panel()
    panel.users[user["username"]] = {"id": user["username"], "username": user["username"]}
    monkeypatch.setattr(jobs, "get_panel_client", lambda: panel)

    assert jobs.run_job("expiry") == 0

    conn = test_db.get_db()
    try:
        assert conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()["status"] == "expired"
    finally:
        conn.close()
    rows = _job_runs(test_db)
    assert len(rows) == 1
    assert rows[0]["job_name"] == "expiry"
    assert rows[0]["ok"] == 1
    assert rows[0]["error"] is None


def test_reconcile_confirmed_claim_is_exactly_once(test_db, monkeypatch):
    user = _user(test_db, "reconcile-confirmed")
    order_id = _insert_pending(
        test_db,
        user["id"],
        "confirmed-order",
        age_minutes=30,
        transaction_id="txn-confirmed",
    )
    monkeypatch.setattr(jobs, "get_platega_client", lambda: _Platega("CONFIRMED"))

    assert jobs.run_job("reconcile") == 0
    assert jobs.run_job("reconcile") == 0

    conn = test_db.get_db()
    try:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert row["status"] == "paid"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM balance_transactions WHERE ref_order_id = ?",
            (order_id,),
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT balance FROM app_users WHERE id = ?", (user["id"],)
        ).fetchone()["balance"] == 500
    finally:
        conn.close()
    assert len(_job_runs(test_db)) == 2
    assert all(row["ok"] == 1 for row in _job_runs(test_db))


def test_job_failure_persists_error_and_returns_nonzero(test_db, monkeypatch):
    user = _user(test_db, "failed-expiry")
    _insert_expired_order(test_db, user["id"], order_id="failed-order")
    monkeypatch.setattr(
        jobs,
        "get_panel_client",
        lambda: (_ for _ in ()).throw(PanelClientError("panel unavailable")),
    )

    with pytest.raises(SystemExit) as exc_info:
        jobs.run_job("expiry")

    assert exc_info.value.code == 1
    rows = _job_runs(test_db)
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert "panel unavailable" in rows[0]["error"]


def test_dry_run_filter_boundaries_are_write_free(test_db, capsys, monkeypatch):
    user = _user(test_db, "boundaries")
    expired_id = _insert_expired_order(test_db, user["id"], order_id="boundary-expired")
    stale_id = _insert_pending(
        test_db,
        user["id"],
        "boundary-stale",
        age_minutes=90,
        transaction_id="txn-stale",
        balance_used=50,
    )
    fresh_id = _insert_pending(
        test_db,
        user["id"],
        "boundary-fresh",
        age_minutes=5,
        transaction_id="txn-fresh",
        balance_used=50,
    )
    monkeypatch.setattr(jobs, "get_panel_client", lambda: _Panel())
    monkeypatch.setattr(jobs, "get_platega_client", lambda: _Platega("NOT_FOUND"))

    assert jobs.run_job("expiry", dry_run=True) == 0
    expiry_output = capsys.readouterr().out
    assert expired_id in expiry_output
    assert stale_id in expiry_output
    assert fresh_id not in expiry_output

    assert jobs.run_job("reconcile", dry_run=True) == 0
    reconcile_output = capsys.readouterr().out
    assert stale_id in reconcile_output
    assert fresh_id not in reconcile_output
    assert _job_runs(test_db) == []
