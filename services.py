"""Shared business logic used by both the web app (app.py) and the Telegram bot.

Keeps payment, fulfillment, trial, referral and balance logic in one place so the
web UI and the bot behave identically.
"""
import json
import math
import secrets
import uuid
from datetime import datetime, timezone, timedelta

import database
import money
from panel_client import PanelClient, PanelClientError
from platega_client import PlategaClient, PlategaClientError


def now():
    """Canonical clock: tz-aware UTC, never naive (D-08 / FOUND-05)."""
    return datetime.now(timezone.utc)


def now_iso():
    """ISO 8601 rendering of the canonical clock, always +00:00 suffixed."""
    return now().isoformat()


def utcnow():
    """Compatibility alias for existing call sites; returns the tz-aware now()."""
    return now()


def log_event(conn, user_id: str, event: str):
    """Record a funnel event on the caller's existing transaction connection."""
    conn.execute(
        "INSERT INTO funnel_events (user_id, event, ts) VALUES (?, ?, ?)",
        (user_id, event, now_iso()),
    )


def get_panel_client():
    return PanelClient(
        database.get_setting("panel_url", "http://127.0.0.1:5000"),
        database.get_setting("panel_token", ""),
    )


def get_platega_client():
    if get_setting_bool("platega_test_mode", False):
        return PlategaClient(
            database.get_setting("platega_test_merchant_id", ""),
            database.get_setting("platega_test_secret", ""),
            base_url=database.get_setting("platega_test_base_url", "https://sandbox.platega.io"),
        )
    return PlategaClient(
        database.get_setting("platega_merchant_id", ""),
        database.get_setting("platega_secret", ""),
    )


def get_setting_bool(key, default=True):
    val = database.get_setting(key, "True" if default else "False")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def get_setting_float(key, default):
    try:
        value = database.get_setting(key, default)
        return float(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        return default


def get_setting_int(key, default):
    """Read an integer setting from the DB (whole rubles per D-01/D-02)."""
    try:
        value = database.get_setting(key, default)
        return money.to_rub(default if value is None or value == "" else value)
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
        log_event(conn, new_id, "registered")
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

def get_balance(user_id: str) -> int:
    """Current wallet balance in whole rubles (transition: REAL rows floor at read)."""
    conn = database.get_db()
    try:
        row = conn.execute("SELECT balance FROM app_users WHERE id = ?", (user_id,)).fetchone()
        return money.to_rub(row["balance"] if row else 0)
    finally:
        conn.close()


def add_balance_int(conn, user_id: str, amount: int, kind: str, ref_order_id: str = "", note: str = ""):
    """Credit the wallet by an INTEGER amount of rubles (D-01).

    The int guard protects the money invariant: callers must pass
    money.to_rub() output. A float here is a programming error, not input.
    """
    if not isinstance(amount, int):
        raise TypeError(f"add_balance_int requires int amount, got {type(amount).__name__}")
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


def apply_promo_price(base_price, promo):
    """Thin delegate to money.apply_promo_price_rub (integer, floor per D-02)."""
    return money.apply_promo_price_rub(money.to_rub(base_price), promo)


# ---------------- Orders ----------------

def create_order(user_id: str, plan_id: int, promo_code: str = "", method: str = "platega", quantity: int = 1) -> dict:
    """Create a pending order with an explicit payment method.

    method="balance": pays the FULL price from the personal wallet. Debited at
        order creation (reserved); caller must fulfil (mark paid) promptly.
    method="platega": pays the FULL price via Platega. Balance is NOT touched
        (no automatic partial discount — the wallet is opt-in).

    quantity: number of users/connections (default 1). Price multiplies.

    Returns order record dict. Raise services.OrderError (.message) on failure.
    """
    plan = get_plan(plan_id, active_only=True)
    if not plan:
        raise OrderError("Тариф не найден")
    promo, promo_err = resolve_promo(promo_code)
    if promo_code.strip() and promo_err:
        raise OrderError(promo_err)

    qty = max(1, int(quantity))
    unit_price = money.to_rub(plan["price_rub"])
    base_price = money.apply_promo_price_rub(unit_price, promo)
    price = money.apply_promo_price_rub(unit_price * qty, promo)
    order_id = str(uuid.uuid4())

    method = "platega" if method != "balance" else "balance"

    if method == "balance":
        # Guarded atomic debit: the balance gate lives in the UPDATE's WHERE
        # clause inside one BEGIN IMMEDIATE transaction — never a separate
        # read-then-write check (FOUND-02). On insufficient funds the whole
        # tx rolls back: no order row, no ledger row, balance untouched.
        with database.tx() as conn:
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, promo_code_id, amount_rub, original_price_rub, balance_used_rub, status, created_at, quantity)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (order_id, user_id, plan_id, plan["name"], promo["id"] if promo else None,
                 0, price, price, now_iso(), qty),
            )
            cur = conn.execute(
                "UPDATE app_users SET balance = balance - ? WHERE id = ? AND balance >= ?",
                (price, user_id, price),
            )
            if cur.rowcount != 1:
                raise OrderError("Недостаточно средств на балансе для оплаты")
            conn.execute(
                "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
                " VALUES (?, ?, 'spend', ?, ?, ?)",
                (user_id, -price, order_id,
                  f"Оплата тарифа «{plan['name']}» ×{qty} с баланса", now_iso()),
            )
            log_event(conn, user_id, "order_started")
        balance_used = price
        payable = 0
    else:
        balance_used = 0
        payable = price
        conn = database.get_db()
        try:
            conn.execute(
                "INSERT INTO orders (id, user_id, plan_id, plan_name, promo_code_id, amount_rub, original_price_rub, balance_used_rub, status, created_at, quantity)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (order_id, user_id, plan_id, plan["name"], promo["id"] if promo else None,
                 payable, price, balance_used, now_iso(), qty),
            )
            log_event(conn, user_id, "order_started")
            conn.commit()
        finally:
            conn.close()

    return {
        "id": order_id,
        "user_id": user_id,
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "quantity": qty,
        "base_price": base_price,
        "price": price,
        "payable": payable,
        "balance_used": balance_used,
        "promo_code_id": promo["id"] if promo else None,
    }


def quote_order(plan_id: int, promo_code: str = "", quantity: int = 1) -> dict:
    """Price preview for an order without creating it. Returns price after promo."""
    plan = get_plan(plan_id, active_only=True)
    if not plan:
        raise OrderError("Тариф не найден")
    promo, promo_err = resolve_promo(promo_code)
    if promo_code.strip() and promo_err:
        raise OrderError(promo_err)
    qty = max(1, int(quantity))
    unit_price = money.to_rub(plan["price_rub"])
    base_price = money.apply_promo_price_rub(unit_price, promo)
    price = money.apply_promo_price_rub(unit_price * qty, promo)
    return {
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "base_price": base_price,
        "quantity": qty,
        "price": price,
        "promo_code_id": promo["id"] if promo else None,
    }


def create_topup_order(user_id: str, amount) -> dict:
    """Create a wallet top-up order (payable via Platega). No plan involved."""
    try:
        amount = money.to_rub(amount)
    except (TypeError, ValueError):
        raise OrderError("Укажите корректную сумму пополнения")
    if amount < 1:
        raise OrderError("Минимальная сумма пополнения — 1 ₽")
    if amount > 100000:
        raise OrderError("Слишком большая сумма пополнения")
    order_id = str(uuid.uuid4())
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_name, amount_rub, original_price_rub,"
            " balance_used_rub, status, created_at)"
            " VALUES (?, ?, NULL, 'Пополнение баланса', ?, ?, 0, 'pending', ?)",
            (order_id, user_id, amount, amount, now_iso()),
        )
        log_event(conn, user_id, "order_started")
        conn.commit()
    finally:
        conn.close()
    return {"id": order_id, "user_id": user_id, "amount_rub": amount, "type": "topup"}


def confirm_topup(order_id: str) -> bool:
    """Credit the wallet for a paid top-up order. Idempotent. Returns True if credited.

    Claim-then-effect: an UPDATE guarded by status='pending' whose rowcount
    gates the credit, so a replayed CONFIRMED delivery (Platega retries up to
    3x) cannot double-credit (FOUND-04 / D-09).
    """
    with database.tx() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return False
        amount = money.to_rub(order["amount_rub"] or 0)
        cur = conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), order_id),
        )
        if cur.rowcount != 1:
            return False  # already claimed — idempotent no-op
        if amount > 0:
            add_balance_int(conn, order["user_id"], amount, "topup",
                            ref_order_id=order_id, note="Пополнение баланса")
        log_event(conn, order["user_id"], "topup_paid")
        return True


async def create_platega_payment(order_id: str, description: str, amount: float) -> str:
    """Create a Platega payment and return payment URL. Raises OrderError."""
    shop_url = database.get_setting("shop_public_url", "http://127.0.0.1:8080").rstrip("/")
    return_url = f"{shop_url}/payment/success?order={order_id}"
    failed_url = f"{shop_url}/payment/fail"
    pg = get_platega_client()
    try:
        result = await pg.create_payment(
            amount=money.to_rub(amount),
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


def _refund_order_balance_tx(conn, order_id: str) -> bool:
    """Claim-then-effect refund inside an open transaction (FOUND-03 / D-09).

    SELECTs balance_used_rub BEFORE the zeroing UPDATE (P1 ordering detail
    — pitfall 6), then claims via a guarded UPDATE whose rowcount == 1 gates
    the wallet credit + ledger INSERT. Replays/races hit rowcount == 0 and
    return False with zero effects — idempotent by construction.
    """
    order = conn.execute(
        "SELECT user_id, balance_used_rub FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if not order:
        return False
    used = money.to_rub(order["balance_used_rub"] or 0)
    cur = conn.execute(
        "UPDATE orders SET status = 'cancelled', balance_used_rub = 0"
        " WHERE id = ? AND status = 'pending' AND balance_used_rub > 0",
        (order_id,),
    )
    if used <= 0:
        cur = conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (order_id,),
        )
    if cur.rowcount != 1:
        return False  # already claimed — idempotent no-op
    conn.execute(
        "UPDATE app_users SET balance = balance + ? WHERE id = ?", (used, order["user_id"])
    )
    conn.execute(
        "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
        " VALUES (?, ?, 'refund', ?, 'Возврат резерва по неподтверждённому заказу', ?)",
        (order["user_id"], used, order_id, now_iso()),
    )
    return True


def refund_order_balance(order_id: str) -> bool:
    """Refund the discount balance reserved for a pending order.

    Claim-then-effect on ONE transaction: SELECT the reserved value before
    zeroing it, then an UPDATE guarded by status='pending' AND
    balance_used_rub > 0 whose rowcount gates the credit. Returns True if a
    credit happened, False if nothing to refund (idempotent no-op on replay).
    """
    with database.tx() as conn:
        return _refund_order_balance_tx(conn, order_id)


def cancel_pending_order(order_id: str) -> bool:
    """Cancel a pending order and refund its balance reservation in one tx.

    Claim + credit in the SAME transaction (no second-connection refund),
    so a cancellation can never refund without atomically claiming first.
    """
    with database.tx() as conn:
        return _refund_order_balance_tx(conn, order_id)


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

def claim_apply_referral(conn, order: dict):
    """Credit referral bonuses / commissions using rowcount-gated flag claims.

    Idempotent by construction: the order's `referral_applied` transition is
    claimed with a guarded UPDATE (WHERE referral_applied=0); a replay finds
    rowcount==0 and returns with zero effects (FOUND-04 / D-09, T-01-10).
    On the first win, a referred user's first deposit (>= threshold) claims
    `referred_paid` (WHERE referred_paid=0) to gate the one-time referee +
    referrer rewards; every subsequent deposit on a referred user earns the
    referrer a set % commission (floored per D-02).
    """
    order_ref = conn.execute(
        "SELECT referral_applied FROM orders WHERE id = ?", (order["id"],)
    ).fetchone()
    if not order_ref or order_ref["referral_applied"]:
        return  # already applied this order — idempotent no-op
    cur = conn.execute(
        "UPDATE orders SET referral_applied = 1 WHERE id = ? AND referral_applied = 0",
        (order["id"],),
    )
    if cur.rowcount != 1:
        return  # lost the claim — another delivery won
    if not get_setting_bool("referral_enabled", True):
        return
    app_row = conn.execute(
        "SELECT * FROM app_users WHERE id = ?", (order["user_id"],)
    ).fetchone()
    app_user = dict(app_row) if app_row else {}
    referrer_id = app_user.get("referrer_id")
    if not referrer_id or order.get("is_trial"):
        return
    deposit = money.to_rub(order.get("amount_rub") or 0)
    if deposit <= 0:
        return

    threshold = get_setting_int("referral_threshold", 100)
    commission_percent = get_setting_int("referral_commission_percent", 25)

    if not app_user.get("referred_paid"):
        # First deposit of a referred user: one-time reward, gated on the
        # referred_paid claim so a replay of the same delivery cannot re-pay.
        if deposit >= threshold:
            referee_bonus = get_setting_int("referral_bonus_referee", 100)
            referrer_bonus = get_setting_int("referral_bonus_referrer", 100)
            cur2 = conn.execute(
                "UPDATE app_users SET referred_paid = 1 WHERE id = ? AND referred_paid = 0",
                (order["user_id"],),
            )
            if cur2.rowcount == 1:
                if referee_bonus > 0:
                    add_balance_int(conn, app_user["id"], referee_bonus, "referral_bonus",
                                    ref_order_id=order["id"],
                                    note="Бонус за первый депозит по реферальной программе")
                if referrer_bonus > 0:
                    add_balance_int(conn, referrer_id, referrer_bonus, "referral_reward",
                                    ref_order_id=order["id"],
                                    note="Вознаграждение за приглашение")
        return

    # Subsequent deposit — % commission to the referrer (floored per D-02).
    if commission_percent > 0 and deposit > 0:
        commission = math.floor(deposit * commission_percent / 100)
        if commission > 0:
            add_balance_int(conn, referrer_id, commission, "referral_commission",
                            ref_order_id=order["id"],
                            note=f"Комиссия {commission_percent}% от пополнения")


def apply_referral(conn, order: dict, app_user: dict):
    """Legacy idempotent referral credit; delegates to the claim-based helper.

    Kept for callers that pass a pre-fetched app_user (e.g. fulfill_order);
    the claim semantics (referral_applied / referred_paid) live in
    claim_apply_referral.
    """
    return claim_apply_referral(conn, order)


async def _fulfill_order_side_effects(conn, order: dict):
    """Provision a previously claimed paid order after its claim commits."""
    current = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
    if current:
        order = dict(current)
    app_user_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (order["user_id"],)).fetchone()
    if not app_user_row:
        app_user_row = conn.execute("SELECT * FROM app_users WHERE username = ?", (order["user_id"],)).fetchone()
    if not app_user_row:
        conn.execute(
            "UPDATE orders SET provisioning_error = ? WHERE id = ?",
            ("Shop user not found", order["id"]),
        )
        conn.commit()
        return
    app_user = dict(app_user_row)

    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (order["plan_id"],)).fetchone()
    days = plan["duration_days"] if plan else 0

    provisioning_error = ""
    existing_connections = []
    try:
        existing_connections = json.loads(order.get("panel_user_connections") or "[]")
    except json.JSONDecodeError:
        existing_connections = []
    try:
        panel_user = await ensure_panel_user(app_user)
        connections = existing_connections or await provision_connections(
            panel_user["id"], app_user["username"]
        )
        if not connections:
            raise RuntimeError("Панель не выдала VPN-конфигурации")
    except Exception as e:
        panel_user = None
        connections = []
        provisioning_error = str(e)

    expires_at = order.get("expires_at")
    if panel_user and plan and days and not expires_at:
        # Renewal: if the user already has an active subscription, extend from
        # its current expiry (or now, whichever is later) instead of starting fresh.
        active = conn.execute(
            "SELECT expires_at FROM orders WHERE user_id = ? AND status = 'paid'"
            " AND expires_at IS NOT NULL AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (app_user["id"], now_iso()),
        ).fetchone()
        base = utcnow()
        if active and active["expires_at"]:
            try:
                base = datetime.fromisoformat(active["expires_at"])
                if base < utcnow():
                    base = utcnow()
            except ValueError:
                base = utcnow()
        expires_at = (base + timedelta(days=days)).isoformat()
        panel = get_panel_client()
        try:
            await panel.update_panel_user(panel_user["id"], expiration_date=expires_at)
        except Exception as e:
            provisioning_error = (provisioning_error + " | " if provisioning_error else "") + f"expiration update: {e}"
            expires_at = None
    elif not expires_at:
        expires_at = None

    with_configs = [c for c in connections if c["config"]]
    conn.execute(
        "UPDATE orders SET expires_at = ?, panel_user_connections = ?,"
        " panel_user_created = ?, provisioning_error = ? WHERE id = ?",
        (expires_at, json.dumps(with_configs, ensure_ascii=False),
         1 if panel_user or order.get("panel_user_created") else 0,
         provisioning_error or None, order["id"]),
    )
    conn.commit()


async def fulfill_order_side_effects(order: dict):
    """Run panel provisioning for an order whose paid claim already committed."""
    conn = database.get_db()
    try:
        await _fulfill_order_side_effects(conn, order)
    finally:
        conn.close()


def claim_promo_usage(conn, order: dict):
    """Count a promo exactly once when the payment claim wins."""
    if order.get("promo_code_id"):
        conn.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?",
            (order["promo_code_id"],),
        )


async def fulfill_order(conn, order: dict):
    """Claim a pending order, apply referral effects, then provision it."""
    with database.tx(conn) as tx_conn:
        cur = tx_conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), order["id"]),
        )
        if cur.rowcount != 1:
            return False
        claimed = tx_conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
        order = dict(claimed)
        log_event(tx_conn, order["user_id"], "order_paid")
        claim_promo_usage(tx_conn, order)
        claim_apply_referral(tx_conn, order)
    await _fulfill_order_side_effects(conn, order)
    return True


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
            " VALUES (?, ?, NULL, ?, 0, 1, 'paid', ?, ?, ?, ?)",
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


def extend_subscription(conn, user_id: str, plan_days: int) -> str:
    """Compute the new expires_at by extending from the current active subscription's
    expiry (or now, whichever is later). Returns ISO 8601 string with +00:00 suffix.
    """
    active = conn.execute(
        "SELECT expires_at FROM orders WHERE user_id = ? AND status = 'paid'"
        " AND expires_at IS NOT NULL AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
        (user_id, now_iso()),
    ).fetchone()
    base = utcnow()
    if active and active["expires_at"]:
        try:
            base = datetime.fromisoformat(active["expires_at"])
            if base < utcnow():
                base = utcnow()
        except ValueError:
            base = utcnow()
    return (base + timedelta(days=plan_days)).isoformat()


def set_auto_renewal(conn, user_id: str, enabled: bool) -> dict:
    """Set the user's auto-renewal flag and consent timestamp.
    
    When enabled=True: sets auto_renewal=1 and records consent timestamp (now_iso)
    ONLY if transitioning from disabled to enabled (preserves original consent timestamp on re-enable).
    When enabled=False: clears auto_renewal and auto_renewal_at (per RENEW-02).
    Returns the updated user dict.
    """
    if enabled:
        # Only set timestamp when transitioning from disabled to enabled (preserve original consent timestamp on re-enable)
        conn.execute(
            "UPDATE app_users SET auto_renewal = 1, auto_renewal_at = COALESCE(auto_renewal_at, ?) WHERE id = ?",
            (now_iso(), user_id),
        )
    else:
        conn.execute(
            "UPDATE app_users SET auto_renewal = 0, auto_renewal_at = NULL WHERE id = ?",
            (user_id,),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


async def auto_renew_order(order_id: str) -> bool:
    """Attempt to auto-renew a single due subscription.
    
    Claim-then-effect: within a single tx, check the per-user flag, claim the renewal,
    debit the wallet, extend the term, and on success update the panel expiration_date.
    Returns True on success, False if the order is not due/already renewed/not eligible,
    raises OrderError on insufficient balance (so the tx rolls back).
    
    Per D-13/D-15/D-16: auto-renewal flag lives on app_users (per-user), not on orders.
    The panel expiration_date is updated (not re-issuing configs/keys) so the service stays active.
    """
    with database.tx() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return False
        order = dict(order)
        
        # Valid candidate: paid, has plan, not trial, has expires_at, deadline reached
        if not (order["status"] == "paid" and order["plan_id"] is not None 
                and not order.get("is_trial") 
                and order.get("expires_at") 
                and order["expires_at"] <= now_iso()):
            return False
        
        # Check per-user auto-renewal flag (D-13: flag lives on app_users, NOT orders)
        user = conn.execute("SELECT * FROM app_users WHERE id = ?", (order["user_id"],)).fetchone()
        if not user or not user["auto_renewal"]:
            return False
        
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (order["plan_id"],)).fetchone()
        if not plan:
            return False
        price = money.to_rub(plan["price_rub"])
        
        # Compute new expires_at from max(active_expires_at, now) + plan duration
        new_expires = extend_subscription(conn, order["user_id"], plan["duration_days"])
        
        # Claim the renewal: guarded UPDATE on the order (no auto_renewal in WHERE - it's on app_users)
        claim = conn.execute(
            "UPDATE orders SET expires_at = ? WHERE id = ? AND status = 'paid' "
            "AND is_trial = 0 AND plan_id IS NOT NULL AND expires_at IS NOT NULL AND expires_at <= ?",
            (new_expires, order_id, now_iso()),
        )
        if claim.rowcount != 1:
            return False  # already renewed / not due / not eligible — idempotent no-op
        
        # Guarded atomic debit (claim-then-effect): balance >= amount guard
        user = conn.execute("SELECT * FROM app_users WHERE id = ?", (order["user_id"],)).fetchone()
        debit = conn.execute(
            "UPDATE app_users SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (price, order["user_id"], price),
        )
        if debit.rowcount != 1:
            # Insufficient balance — raise to roll back the extend claim
            raise OrderError("Недостаточно средств для автопродления")
        
        # Record the debit ledger row
        conn.execute(
            "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
            " VALUES (?, ?, 'auto_renewal', ?, ?, ?)",
            (order["user_id"], -price, order_id, f"Автопродление тарифа {plan['name']}", now_iso()),
        )
        
        # Capture data needed for post-commit panel call (tx closes connection on exit)
        user_dict = dict(user)
        
        conn.commit()
    
    # Post-commit: extend panel user's expiration_date (service stays active, no config re-issue)
    panel = get_panel_client()
    panel_user = await ensure_panel_user(user_dict)
    await panel.update_panel_user(panel_user["id"], expiration_date=new_expires)
    
    return True
