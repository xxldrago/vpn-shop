"""Telegram Login Widget: hash verification + link-or-login callback.

Covers services.verify_telegram_login (valid/tampered/stale/missing) and the
GET /auth/telegram route: first login creates + links the user, repeat login
resolves the SAME row (so site subscriptions show up in the bot), username
collisions get a unique suffix.
"""
import hashlib
import hmac
import time

import app as app_module
import database
import services

TOKEN = "test-bot-token-123"


def _sign(params: dict) -> dict:
    check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    signed = dict(params)
    signed["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return signed


def _fresh(**over):
    p = {"id": "771234", "first_name": "Ivan", "username": "ivan_tg",
         "auth_date": str(int(time.time()))}
    p.update(over)
    return _sign(p)


def test_verify_ok():
    data = services.verify_telegram_login(_fresh(), TOKEN)
    assert data is not None and data["id"] == "771234"


def test_verify_tampered():
    p = _fresh()
    p["username"] = "mallory"
    assert services.verify_telegram_login(p, TOKEN) is None


def test_verify_stale():
    p = _fresh(auth_date=str(int(time.time()) - 90000))
    assert services.verify_telegram_login(p, TOKEN) is None


def test_verify_missing_hash():
    p = _fresh()
    del p["hash"]
    assert services.verify_telegram_login(p, TOKEN) is None


def _use_test_token():
    database.set_setting("telegram_bot_token", TOKEN)


def test_callback_creates_and_links(test_db):
    _use_test_token()
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    resp = client.get("/auth/telegram", params=_fresh(), follow_redirects=False)
    assert resp.status_code == 303, f"got {resp.status_code}: {resp.text[:200]}"
    user = services.get_user_by_telegram("771234")
    assert user is not None
    assert user["username"] == "ivan_tg"
    # session logged in: dashboard shows the username
    dash = client.get("/dashboard")
    assert dash.status_code == 200 and "ivan_tg" in dash.text


def test_callback_same_tg_logs_into_same_user(test_db):
    _use_test_token()
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    client.get("/auth/telegram", params=_fresh(), follow_redirects=False)
    first = services.get_user_by_telegram("771234")
    client.get("/auth/telegram", params=_fresh(), follow_redirects=False)
    second = services.get_user_by_telegram("771234")
    assert first["id"] == second["id"]
    conn = database.get_db()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM app_users").fetchone()["c"]
    finally:
        conn.close()
    assert n == 2  # admin seed + the one linked user, no duplicates


def test_callback_username_collision_gets_suffix(test_db):
    services.create_user(username="ivan_tg", email="")
    _use_test_token()
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    resp = client.get("/auth/telegram", params=_fresh(), follow_redirects=False)
    assert resp.status_code == 303
    user = services.get_user_by_telegram("771234")
    assert user is not None and user["username"] != "ivan_tg"
    assert user["username"].startswith("ivan_tg")


def test_get_telegram_bot_username_cached(test_db):
    database.set_setting("telegram_bot_username", "threeSet_bot")
    assert services.get_telegram_bot_username() == "threeSet_bot"


def test_get_telegram_bot_username_no_token(test_db):
    database.set_setting("telegram_bot_token", "")
    database.set_setting("telegram_bot_username", "")
    assert services.get_telegram_bot_username() == ""
