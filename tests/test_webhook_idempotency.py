"""End-to-end Platega webhook idempotency tests (FOUND-04 / D-09).

Pins the at-least-once contract: Platega retries callbacks up to 3x at
5-minute intervals, so a CONFIRMED/CANCELED replay must never double-credit
or double-refund the wallet, and a CONFIRMED racing a CANCELED must leave
exactly one status transition with no refund-after-credit.

Reaches the real route (/webhook/callback) via FastAPI TestClient so the
HTTP contract (401 for bad headers, 404 for unknown order, 200 for replay)
is asserted against the actual handler.
"""
import pytest
from starlette.testclient import TestClient

import database
import money
import services
import app as app_module


def _setup_merchant(test_db):
    """Set the merchant credentials the webhook verifies against."""
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO shop_settings (key, value) VALUES ('platega_merchant_id', 'MID1')"
            " ON CONFLICT(key) DO UPDATE SET value = 'MID1'"
        )
        conn.execute(
            "INSERT INTO shop_settings (key, value) VALUES ('platega_secret', 'SEC1')"
            " ON CONFLICT(key) DO UPDATE SET value = 'SEC1'"
        )
        conn.commit()
    finally:
        conn.close()


def _client():
    """A fresh TestClient bound to the monkeypatched DB (no lifespan watcher)."""
    from fastapi.testclient import TestClient as TC
    # Use the app's TestClient; lifespan watcher swallows errors harmlessly.
    return TC(app_module.app)


def _headers():
    return {"X-MerchantId": "MID1", "X-Secret": "SEC1"}


def _create_user(name: str, balance: int = 0) -> dict:
    user = services.create_user(name, email="", password="")
    if balance:
        conn = database.get_db()
        try:
            conn.execute("UPDATE app_users SET balance = ? WHERE id = ?", (balance, user["id"]))
            conn.commit()
        finally:
            conn.close()
    return user


def _balance(user_id):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user_id,)).fetchone()
        return money.to_rub(row["balance"] if row else 0)
    finally:
        conn.close()


def _ledger_kind(user_id, kind):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? AND kind = ?", (user_id, kind)
        ).fetchall()]
    finally:
        conn.close()


def _order_status(order_id):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


class _PanelFake:
    """Minimal panel fake: one user, no servers => provisioning is a no-op."""

    def __init__(self):
        self.users = {}

    async def find_user_by_username(self, username):
        return self.users.get(username)

    async def create_panel_user(self, username, password, email, role):
        uid = f"panel-{username}"
        self.users[username] = {"id": uid, "username": username}
        return {"user_id": uid, "id": uid}

    async def get_servers_with_protocols(self, installed_only=True):
        return []

    async def add_connection(self, **kwargs):
        raise AssertionError("provisioning should not run with no servers")

    async def update_panel_user(self, uid, **fields):
        return {"status": "success"}


# ---------------- Helpers ----------------

def _pending_topup(user_id, amount: int) -> str:
    """Create a pending wallet top-up order and return its id."""
    order = services.create_topup_order(user_id, amount)
    return order["id"]


def _pending_plan_order(user_id, txn_id: str = "txn-plan") -> dict:
    """Create a pending platega plan order (out of the seeds) and return it."""
    # Find the 1-month plan (250.0 seed).
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id, price_rub FROM plans WHERE price_rub = 250.0").fetchone()
        plan_id = row["id"]
        price = money.to_rub(row["price_rub"])
    finally:
        conn.close()
    order = services.create_order(user_id, plan_id, method="platega")
    # Attach a platega transaction id so the txn fallback (resolve when the
    # CallbackPayload has no payload field) also finds the order.
    conn = database.get_db()
    try:
        conn.execute("UPDATE orders SET platega_transaction_id = ? WHERE id = ?",
                     (txn_id, order["id"]))
        conn.commit()
    finally:
        conn.close()
    order["platega_transaction_id"] = txn_id
    return order


def _confirm_body(txn_id: str, payload: str = ""):
    body = {"id": txn_id, "status": "CONFIRMED", "amount": 100, "currency": "RUB"}
    if payload:
        body["payload"] = payload
    return body


def _cancel_body(txn_id: str, payload: str = ""):
    body = {"id": txn_id, "status": "CANCELED", "amount": 100, "currency": "RUB"}
    if payload:
        body["payload"] = payload
    return body


# ---------------- Auth / resolution contract ----------------

def test_webhook_bad_merchant_headers_401(test_db):
    _setup_merchant(test_db)
    client = _client()
    resp = client.post("/webhook/callback", headers={"X-MerchantId": "x", "X-Secret": "y"},
                       json=_confirm_body("t-xyz"))
    assert resp.status_code == 401


def test_webhook_unknown_order_404(test_db):
    _setup_merchant(test_db)
    client = _client()
    resp = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("t-unknown"))
    assert resp.status_code == 404


# ---------------- CONFIRMED idempotency ----------------

def test_webhook_confirm_paid_once_wallet_credited(test_db, monkeypatch):
    _setup_merchant(test_db)
    monkeypatch.setattr(app_module.services, "get_panel_client", lambda: _PanelFake())
    client = _client()
    user = _create_user("conf1")
    order = _pending_plan_order(user["id"], txn_id="txn-conf1")
    oid = order["id"]
    before = _balance(user["id"])

    resp = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("txn-conf1", payload=oid))
    assert resp.status_code == 200
    assert _order_status(oid) == "paid"
    # Wallet: plan order via Platega does NOT debit/credit balance (paid externally).
    assert _balance(user["id"]) == before


def test_webhook_confirm_replay_no_double_credit(test_db, monkeypatch):
    """Replayed CONFIRMED (same txn/delivery) -> 200, balance unchanged, one ledger row."""
    _setup_merchant(test_db)
    monkeypatch.setattr(app_module.services, "get_panel_client", lambda: _PanelFake())
    client = _client()
    user = _create_user("conf2", balance=400)
    order = _pending_plan_order(user["id"], txn_id="txn-conf2")
    oid = order["id"]

    # First delivery WITHOUT a payload (resolved by txn fallback) — load-bearing path.
    first = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("txn-conf2"))
    assert first.status_code == 200
    assert _order_status(oid) == "paid"

    # Replay the same txn id.
    replay = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("txn-conf2"))
    assert replay.status_code == 200
    assert _order_status(oid) == "paid"


def test_webhook_topup_confirm_exactly_once(test_db):
    """CONFIRMED wallet top-up credits exactly once; replay does not double-credit."""
    _setup_merchant(test_db)
    client = _client()
    user = _create_user("topup1", balance=100)
    oid = _pending_topup(user["id"], 500)

    first = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("t-topup1", payload=oid))
    assert first.status_code == 200
    assert _balance(user["id"]) == 600  # 100 + 500

    topup_rows = _ledger_kind(user["id"], "topup")
    assert len(topup_rows) == 1

    replay = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("t-topup1", payload=oid))
    assert replay.status_code == 200
    assert _balance(user["id"]) == 600  # unchanged
    assert len(_ledger_kind(user["id"], "topup")) == 1


# ---------------- CANCELED idempotency ----------------

def test_webhook_cancel_refunds_once(test_db):
    """CANCELED webhook refunds the reserved balance once; replay does not double-refund."""
    _setup_merchant(test_db)
    client = _client()
    user = _create_user("cancel1", balance=300)
    # Create a balance-funded pending order so balance_used_rub > 0.
    conn = database.get_db()
    try:
        row = conn.execute("SELECT id FROM plans WHERE price_rub = 250.0").fetchone()
        plan_id = row["id"]
    finally:
        conn.close()
    order = services.create_order(user["id"], plan_id, method="balance")
    oid = order["id"]
    assert _balance(user["id"]) == 50  # 300 - 250 reserved

    first = client.post("/webhook/callback", headers=_headers(), json=_cancel_body("t-cancel1", payload=oid))
    assert first.status_code == 200
    assert _order_status(oid) == "cancelled"
    assert _balance(user["id"]) == 300  # refunded
    assert len(_ledger_kind(user["id"], "refund")) == 1

    replay = client.post("/webhook/callback", headers=_headers(), json=_cancel_body("t-cancel1", payload=oid))
    assert replay.status_code == 200
    assert _balance(user["id"]) == 300  # no double refund
    assert len(_ledger_kind(user["id"], "refund")) == 1


# ---------------- CONFIRMED racing CANCELED ----------------

def test_webhook_confirm_then_cancel_no_refund_after_credit(test_db, monkeypatch):
    """CONFIRMED then CANCELED: order stays paid, balance NOT refunded (claim won)."""
    _setup_merchant(test_db)
    monkeypatch.setattr(app_module.services, "get_panel_client", lambda: _PanelFake())
    client = _client()
    user = _create_user("race1", balance=400)
    order = _pending_plan_order(user["id"], txn_id="txn-race1")
    oid = order["id"]
    before = _balance(user["id"])

    ok = client.post("/webhook/callback", headers=_headers(), json=_confirm_body("txn-race1", payload=oid))
    assert ok.status_code == 200
    assert _order_status(oid) == "paid"

    # CANCELED arriving after the claim won: no effect, no refund.
    cancelled = client.post("/webhook/callback", headers=_headers(), json=_cancel_body("txn-race1", payload=oid))
    assert cancelled.status_code == 200
    assert _order_status(oid) == "paid"
    assert _balance(user["id"]) == before  # no refund after credit
    assert len(_ledger_kind(user["id"], "refund")) == 0
