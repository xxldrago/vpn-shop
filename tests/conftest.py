"""Shared pytest fixtures for the vpn-shop test suite.

Wave 0 infrastructure: every later plan's golden tests build on these
fixtures (isolated test database, Amnezia panel mock, Platega mock).
"""
import pytest

import database
from panel_client import PanelClientError
from platega_client import PlategaClientError


def pytest_sessionfinish(session, exitstatus):
    """Treat an empty suite as success (exit 0), not "no tests collected" (5).

    Wave 0 ships no test_*.py files until later plans land; an empty run
    must still exit 0 so "fresh pytest run exits 0" (framework installed
    and importable) holds as a CI acceptance gate. Real failures keep
    their exit codes — only the zero-tests-collected code is rewritten.
    """
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK


# ---------------- Database ----------------

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Isolated SQLite database (schema + seeds) for one test.

    monkeypatch.setattr(database, "DB_PATH", ...) rebinds every get_db()
    call to a fresh temp database (verified isolation pattern — probe
    L1/L2): init_db() seeds the 5 default plans, default settings and the
    admin account into the temp file. WAL sidecars live under tmp_path and
    are auto-cleaned by pytest. Yields the database module.
    """
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    yield database


# ---------------- Amnezia Panel ----------------

@pytest.fixture
def panel_mock():
    """In-memory fake of the Amnezia Web Panel client API.

    Mirrors the PanelClient surface used by services.py
    (find_user_by_username, update_panel_user) and the mock_panel.py user
    shape (id, username, role, ...) for later job/fulfillment tests.
    """
    class PanelMock:
        def __init__(self):
            self.users = {}  # panel user id -> user dict

        def add_user(self, username, **extra):
            """Seed a panel user for tests to look up."""
            user = {"id": username, "username": username, "role": "user", **extra}
            self.users[user["id"]] = user
            return user

        async def find_user_by_username(self, username):
            for user in self.users.values():
                if user.get("username") == username:
                    return user
            return None

        async def update_panel_user(self, uid, **fields):
            if uid not in self.users:
                raise PanelClientError(f"Panel error 404: user {uid} not found")
            self.users[uid].update(fields)
            return {"status": "success"}

    return PanelMock()


# ---------------- Platega ----------------

@pytest.fixture
def platega_client_mock():
    """Stub of the Platega.io client for reconcile tests.

    get_payment_status returns {"status": "NOT_FOUND"} by default
    (matching platega_client.py's 404 behavior); tests can override the
    per-transaction status via set_status() or force an error via
    set_error().
    """
    class PlategaMock:
        def __init__(self):
            self.statuses = {}  # transaction_id -> status dict
            self.error = None   # exception to raise, if set

        def set_status(self, transaction_id, status):
            self.statuses[transaction_id] = status

        def set_error(self, exc=None):
            self.error = exc if exc is not None else PlategaClientError("Platega status error")

        async def get_payment_status(self, transaction_id):
            if self.error is not None:
                raise self.error
            return self.statuses.get(transaction_id, {"status": "NOT_FOUND"})

    return PlategaMock()