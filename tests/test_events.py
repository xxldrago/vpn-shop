"""Funnel event persistence tests for FOUND-06."""

import asyncio

import database
import services


def _events(user_id):
    conn = database.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT user_id, event, ts FROM funnel_events WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()]
    finally:
        conn.close()


def _plan_id(test_db):
    conn = database.get_db()
    try:
        return conn.execute("SELECT id FROM plans WHERE price_rub = 250.0").fetchone()["id"]
    finally:
        conn.close()


def test_create_user_logs_registered(test_db):
    user = services.create_user("event-user")

    assert _events(user["id"]) == [
        {"user_id": user["id"], "event": "registered", "ts": user["created_at"]},
    ]


def test_create_order_and_topup_log_order_started(test_db):
    user = services.create_user("event-orders")
    plan_id = _plan_id(test_db)

    order = services.create_order(user["id"], plan_id, method="platega")
    topup = services.create_topup_order(user["id"], 500)

    assert [row["event"] for row in _events(user["id"])] == [
        "registered",
        "order_started",
        "order_started",
    ]
    assert order["user_id"] == topup["user_id"] == user["id"]


def test_claims_log_paid_events_once(test_db, monkeypatch):
    user = services.create_user("event-paid")
    plan_id = _plan_id(test_db)
    order = services.create_order(user["id"], plan_id, method="platega")
    topup = services.create_topup_order(user["id"], 500)

    monkeypatch.setattr(services, "fulfill_order_side_effects", lambda order: _done())
    conn = database.get_db()
    try:
        assert asyncio.run(services.fulfill_order(conn, order)) is True
    finally:
        conn.close()

    assert services.confirm_topup(topup["id"]) is True
    assert services.confirm_topup(topup["id"]) is False

    assert [row["event"] for row in _events(user["id"])] == [
        "registered",
        "order_started",
        "order_started",
        "order_paid",
        "topup_paid",
    ]


async def _done():
    return None


def test_funnel_events_schema_and_index(test_db):
    conn = database.get_db()
    try:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(funnel_events)")]
        indexes = [row["name"] for row in conn.execute("PRAGMA index_list(funnel_events)")]
    finally:
        conn.close()

    assert columns == ["id", "user_id", "event", "ts"]
    assert "idx_funnel_user_event" in indexes
