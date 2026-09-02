"""Shared business logic used by both the web app (app.py) and the Telegram bot.

Keeps payment, fulfillment, trial, referral and balance logic in one place so the
web UI and the bot behave identically.
"""
import json
import secrets
import uuid
from datetime import datetime, timedelta

import database
from panel_client import PanelClient, PanelClientError
from platega_client import PlategaClient, PlategaClientError


def now_iso():
    return datetime.utcnow().isoformat()


def utcnow():
    return datetime.utcnow()


def get_panel_client():
    return PanelClient(
        database.get_setting("panel_url", "http://127.0.0.1:5000"),
        database.get_setting("panel_token", ""),
    )


def get_platega_client():
    return PlategaClient(
        database.get_setting("platega_merchant_id", ""),
        database.get_setting("platega_secret", ""),
    )


def get_setting_bool(key, default=True):
    val = database.get_setting(key, "True" if default else "False")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def get_setting_float(key, default):
    try:
        return float(database.get_setting(key, default) or default)
    except (TypeError, ValueError):
        return default


# ---------------- Users ----------------

def get_user_by_id(user_id: str) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM app_users WHERE username = ?", (username.strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_telegram(telegram_id) -> dict | None:
    if telegram_id is None:
        return None
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM app_users WHERE telegram_id = ?", (str(telegram_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def attach_telegram(user_id: str, telegram_id) -> None:
    conn = database.get_db()
    try:
        conn.execute(
            "UPDATE app_users SET telegram_id = ? WHERE id = ?", (str(telegram_id), user_id)
        )
        conn.commit()
    finally:
        conn.close()


def create_user(username: str, email: str = "", password: str = "") -> dict:
    """Create a new local shop user (used by the bot). Password may be empty."""
    from auth import hash_password
    conn = database.get_db()
    try:
        new_id = str(uuid.uuid4())
        referral_code = generate_referral_code(conn)
        pwd = hash_password(password) if password else ""
        conn.execute(
            "INSERT INTO app_users (id, username, email, telegram_id, role, enabled, created_at, password_hash, referral_code)"
            " VALUES (?, ?, ?, '', 'user', 1, ?, ?, ?)",
            (new_id, username.strip(), email, now_iso(), pwd, referral_code),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM app_users WHERE id = ?", (new_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def generate_referral_code(conn) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(100):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        existing = conn.execute("SELECT id FROM app_users WHERE referral_code = ?", (code,)).fetchone()
        if not existing:
            return code
    raise RuntimeError("Не удалось сгенерировать реферальный код")


def get_user_by_referral_code(code: str) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM app_users WHERE referral_code = ?", (code.upper().strip(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------- Balance ----------------

def get_balance(user_id: str) -> float:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user_id,)).fetchone()
        return float(row["balance"] if row else 0)
    finally:
        conn.close()


def add_balance(conn, user_id: str, amount: float, kind: str, ref_order_id: str = "", note: str = ""):
    conn.execute("UPDATE app_users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    conn.execute(
        "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, kind, ref_order_id, note, now_iso()),
    )


def list_balance_transactions(user_id: str, limit: int = 100):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()]
    finally:
        conn.close()


# ---------------- Plans ----------------

def list_active_plans():
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order, id"
        ).fetchall()]
    finally:
        conn.close()


def get_plan(plan_id, active_only: bool = True):
    conn = database.get_db()
    try:
        if active_only:
            row = conn.execute("SELECT * FROM plans WHERE id = ? AND is_active = 1", (plan_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------- Subscriptions ----------------

def get_active_subscription(user_id: str) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status = 'paid' AND expires_at IS NOT NULL"
            " AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now_iso()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def has_used_trial(user_id: str) -> bool:
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT id FROM orders WHERE user_id = ? AND is_trial = 1 AND status = 'paid' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_user_orders(user_id: str):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


def get_order(order_id: str) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_site_url() -> str:
    return database.get_setting("shop_public_url", "http://127.0.0.1:8080").rstrip("/")


# ---------------- Promo ----------------

def resolve_promo(code: str) -> dict | None:
    """Return (promo_row, error). Promo_row is None if invalid."""
    code = code.strip()
    if not code:
        return None, ""
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1"
            " AND (valid_from IS NULL OR valid_from <= ?) AND (valid_until IS NULL OR valid_until >= ?)",
            (code, now_iso(), now_iso()),
        ).fetchone()
        if not row:
            return None, "Промокод недействителен"
        row = dict(row)
        if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
            return None, "Промокод исчерпан"
        return row, ""
    finally:
        conn.close()


def apply_promo_price(base_price: float, promo: dict | None) -> float:
    price = base_price
    if promo:
        if promo.get("discount_percent"):
            price = price * (100 - promo["discount_percent"]) / 100
        elif promo.get("discount_amount_rub"):
            price = max(0, price - promo["discount_amount_rub"])
    return price


# ---------------- Orders ----------------

def create_order(user_id: str, plan_id: int, promo_code: str = "") -> dict:
    """Create a pending order applying promo + discount balance.

    Returns order record (with payable = amount_rub). Balance used is reserved
    (debited). If a later step fails, call refund_order_balance() to restore it.
    Raises services.OrderError with .message on user-facing failures.
    """
    plan = get_plan(plan_id, active_only=True)
    if not plan:
        raise OrderError("Тариф не найден")
    promo, promo_err = resolve_promo(promo_code)
    if promo_code.strip() and promo_err:
        raise OrderError(promo_err)

    price = apply_promo_price(plan["price_rub"], promo)
    order_id = str(uuid.uuid4())
    original_price = round(price, 2)
    balance = get_balance(user_id)
    balance_used = min(balance, original_price)
    payable = round(original_price - balance_used, 2)

    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, promo_code_id, amount_rub, original_price_rub, balance_used_rub, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (order_id, user_id, plan_id, plan["name"], promo["id"] if promo else None,
             payable, original_price, balance_used, now_iso()),
        )
        if balance_used > 0:
            conn.execute("UPDATE app_users SET balance = balance - ? WHERE id = ?", (balance_used, user_id))
            conn.execute(
                "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
                " VALUES (?, ?, 'spend', ?, ?, ?)",
                (user_id, -balance_used, order_id,
                 f"Оплата тарифа «{plan['name']}» из скидочного баланса", now_iso()),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "id": order_id,
        "user_id": user_id,
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "price": original_price,
        "payable": payable,
        "balance_used": balance_used,
        "promo_code_id": promo["id"] if promo else None,
    }


async def create_platega_payment(order_id: str, description: str, amount: float) -> str:
    """Create a Platega payment and return payment URL. Raises OrderError."""
    shop_url = database.get_setting("shop_public_url", "http://127.0.0.1:8080").rstrip("/")
    return_url = f"{shop_url}/payment/success?order={order_id}"
    failed_url = f"{shop_url}/payment/fail"
    pg = get_platega_client()
    try:
        result = await pg.create_payment(
            amount=round(float(amount), 2),
            description=description,
            return_url=return_url,
            failed_url=failed_url,
            payload=order_id,
        )
    except PlategaClientError as e:
        raise OrderError(f"Не удалось создать платёж: {e}")
    transaction_id = result.get("transactionId")
    conn = database.get_db()
    try:
        if transaction_id:
            conn.execute(
                "UPDATE orders SET platega_transaction_id = ? WHERE id = ?", (transaction_id, order_id)
            )
            conn.commit()
    finally:
        conn.close()
    payment_url = result.get("url") or result.get("redirect")
    if not payment_url:
        raise OrderError("Не удалось получить платёжную ссылку")
    return payment_url


def refund_order_balance(order_id: str):
    """Refund the discount balance reserved for a pending order."""
    conn = database.get_db()
    try:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return
        order = dict(order)
        used = float(order.get("balance_used_rub") or 0)
        if used <= 0:
            return
        conn.execute(
            "UPDATE app_users SET balance = balance + ? WHERE id = ?", (used, order["user_id"])
        )
        conn.execute(
            "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
            " VALUES (?, ?, 'refund', ?, 'Возврат резерва по неподтверждённому заказу', ?)",
            (order["user_id"], used, order_id, now_iso()),
        )
        conn.execute("DELETE FROM balance_transactions WHERE ref_order_id = ? AND kind = 'spend'",
                     (order_id,))
        conn.commit()
    finally:
        conn.close()


def cancel_pending_order(order_id: str):
    """Cancel a pending order and refund its balance reservation."""
    conn = database.get_db()
    try:
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (order_id,))
        conn.commit()
    finally:
        conn.close()
    refund_order_balance(order_id)


# ---------------- Panel provisioning ----------------

async def provision_connections(user_id: str, conn_name_prefix: str):
    panel = get_panel_client()
    servers = await panel.get_servers_with_protocols(installed_only=True)
    created = []
    for serv in servers:
        for proto in serv["protocols"]:
            name = f"{conn_name_prefix} {serv['server'].get('name', serv['server_id'])} {proto['display_name']}"
            try:
                resp = await panel.add_connection(
                    server_id=serv["server_id"],
                    protocol=proto["key"],
                    name=name,
                    user_id=user_id,
                )
                created.append({
                    "server_id": serv["server_id"],
                    "protocol": proto["key"],
                    "client_id": resp.get("client_id"),
                    "name": name,
                    "config": resp.get("config", ""),
                    "vpn_link": resp.get("vpn_link", ""),
                    "vpn_name": resp.get("vpn_name", name),
                    "vpn_qr_chunks": resp.get("vpn_qr_chunks", []),
                })
            except Exception:
                continue
    return created


async def ensure_panel_user(user: dict) -> dict:
    panel = get_panel_client()
    try:
        p_user = await panel.find_user_by_username(user["username"])
    except Exception:
        p_user = None
    if p_user:
        return p_user
    pw = secrets.token_urlsafe(12)
    resp = await panel.create_panel_user(
        username=user["username"], password=pw, email=user.get("email") or "", role="user"
    )
    uid = resp.get("user_id")
    if not uid:
        raise RuntimeError("Не удалось создать пользователя в панели")
    return {"id": uid}


# ---------------- Referral ----------------

def apply_referral(conn, order: dict, app_user: dict):
    """Credit referral bonuses / commissions. Called once per paid order."""
    if not get_setting_bool("referral_enabled", True):
        return
    referrer_id = app_user.get("referrer_id")
    if not referrer_id or order.get("is_trial"):
        return
    deposit = float(order.get("amount_rub") or 0)
    if deposit <= 0:
        return

    threshold = get_setting_float("referral_threshold", 100.0)
    commission_percent = get_setting_float("referral_commission_percent", 25.0)
    already_paid = bool(app_user.get("referred_paid"))

    if not already_paid:
        if deposit >= threshold:
            referee_bonus = get_setting_float("referral_bonus_referee", 100.0)
            referrer_bonus = get_setting_float("referral_bonus_referrer", 100.0)
            if referee_bonus > 0:
                add_balance(conn, app_user["id"], referee_bonus, "referral_bonus",
                            ref_order_id=order["id"], note="Бонус за первый депозит по реферальной программе")
            if referrer_bonus > 0:
                add_balance(conn, referrer_id, referrer_bonus, "referral_reward",
                            ref_order_id=order["id"], note="Вознаграждение за приглашение")
            conn.execute("UPDATE app_users SET referred_paid = 1 WHERE id = ?", (app_user["id"],))
    else:
        if commission_percent > 0 and deposit > 0:
            commission = round(deposit * commission_percent / 100.0, 2)
            if commission > 0:
                add_balance(conn, referrer_id, commission, "referral_commission",
                            ref_order_id=order["id"], note=f"Комиссия {commission_percent}% от пополнения")


async def fulfill_order(conn, order: dict):
    """Fulfill a paid order: referral rewards + panel provisioning + mark paid."""
    app_user_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (order["user_id"],)).fetchone()
    if not app_user_row:
        app_user_row = conn.execute("SELECT * FROM app_users WHERE username = ?", (order["user_id"],)).fetchone()
    if not app_user_row:
        conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ?, provisioning_error = ? WHERE id = ?",
            (now_iso(), "Shop user not found", order["id"]),
        )
        conn.commit()
        return
    app_user = dict(app_user_row)

    if not order.get("referral_applied"):
        apply_referral(conn, order, app_user)
        conn.execute("UPDATE orders SET referral_applied = 1 WHERE id = ?", (order["id"],))
        conn.commit()

    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (order["plan_id"],)).fetchone()
    days = plan["duration_days"] if plan else 0

    provisioning_error = ""
    try:
        panel_user = await ensure_panel_user(app_user)
        connections = await provision_connections(panel_user["id"], app_user["username"])
    except Exception as e:
        panel_user = None
        connections = []
        provisioning_error = str(e)

    if panel_user and plan and days:
        expires_at = (utcnow() + timedelta(days=days)).isoformat()
        panel = get_panel_client()
        try:
            await panel.update_panel_user(panel_user["id"], expiration_date=expires_at)
        except Exception as e:
            provisioning_error = (provisioning_error + " | " if provisioning_error else "") + f"expiration update: {e}"
    else:
        expires_at = None

    with_configs = [c for c in connections if c["config"]]
    conn.execute(
        "UPDATE orders SET status = 'paid', paid_at = ?, expires_at = ?, panel_user_connections = ?,"
        " panel_user_created = ?, provisioning_error = ? WHERE id = ?",
        (now_iso(), expires_at, json.dumps(with_configs, ensure_ascii=False),
         1 if panel_user else 0, provisioning_error or None, order["id"]),
    )
    if order["promo_code_id"]:
        conn.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?",
                     (order["promo_code_id"],))
    conn.commit()


def confirm_order_paid(order_id: str):
    """Handle a 'paid' event for an order (from webhook or bot). Idempotent."""
    conn = database.get_db()
    try:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return False
        order = dict(order)
        if order["status"] == "paid":
            return False  # already paid
        # not a real fulfillment — just mark paid without panel? We'll fulfill properly.
        return True
    finally:
        conn.close()


# ---------------- Trial ----------------

async def activate_trial(user_id: str) -> dict:
    """Activate the one-time trial subscription. Returns the created order dict.

    Raises OrderError on failure (unknown user, already used, panel unreachable).
    """
    user = get_user_by_id(user_id)
    if not user:
        raise OrderError("Пользователь не найден")
    if has_used_trial(user_id):
        raise OrderError("Тестовая подписка уже была использована")

    try:
        trial_days = max(1, int(database.get_setting("test_subscription_days", "3") or 3))
    except ValueError:
        trial_days = 3

    try:
        panel_user = await ensure_panel_user(user)
        connections = await provision_connections(panel_user["id"], user["username"])
    except Exception:
        raise OrderError("Не удалось связаться с Amnezia Panel. Попробуйте позже.")

    expires_at = (utcnow() + timedelta(days=trial_days)).isoformat()
    panel = get_panel_client()
    try:
        await panel.update_panel_user(panel_user["id"], expiration_date=expires_at)
    except Exception:
        pass

    order_id = str(uuid.uuid4())
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, is_trial, status, paid_at, expires_at, panel_user_connections, created_at)"
            " VALUES (?, ?, NULL, ?, 0.0, 1, 'paid', ?, ?, ?, ?)",
            (order_id, user_id, "Тестовая подписка", now_iso(), expires_at,
             json.dumps([c for c in connections if c["config"]], ensure_ascii=False), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": order_id,
        "expires_at": expires_at,
        "connections": [c for c in connections if c["config"]],
    }


class OrderError(Exception):
    pass


# ---------------- Support tickets ----------------

def create_ticket(user_id: str, subject: str, message: str) -> dict:
    conn = database.get_db()
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO support_tickets (user_id, subject, status, created_at, updated_at)"
            " VALUES (?, ?, 'open', ?, ?)",
            (user_id, subject, now, now),
        )
        ticket_id = cur.lastrowid
        conn.execute(
            "INSERT INTO support_messages (ticket_id, sender_id, sender_role, message, created_at)"
            " VALUES (?, ?, 'user', ?, ?)",
            (ticket_id, user_id, message, now),
        )
        conn.commit()
        return {"id": ticket_id, "subject": subject}
    finally:
        conn.close()


def list_user_tickets(user_id: str):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM support_tickets WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> dict | None:
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_ticket_messages(ticket_id: int):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
        ).fetchall()]
    finally:
        conn.close()


def add_ticket_message(ticket_id: int, sender_id: str, sender_role: str, message: str):
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO support_messages (ticket_id, sender_id, sender_role, message, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ticket_id, sender_id, sender_role, message, now_iso()),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now_iso(), ticket_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------- Renewal ----------------

def renewal_days(order: dict) -> int:
    """Days of the subscription that produced a given paid order (or trial)."""
    plan = get_plan(order.get("plan_id"), active_only=False) if order.get("plan_id") else None
    if plan:
        return int(plan["duration_days"])
    return 0
