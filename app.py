import json
import hmac
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import BASE_DIR, SESSION_SECRET
import database
import money as money_utils
import services
from services import now_iso, utcnow
from panel_client import PanelClient, PanelClientError
from platega_client import PlategaClient, PlategaClientError
from auth import hash_password, verify_password


APP_VERSION = "0.1.0"

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _header_balance(request: Request):
    """Fresh wallet balance for the topbar (only for logged-in users)."""
    sess_user = request.session.get("user") if request.session else None
    if not sess_user:
        return None
    try:
        return services.get_balance(sess_user["id"])
    except Exception:
        return sess_user.get("balance", 0)


templates.env.globals["header_balance"] = _header_balance


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield
    # expiry moved to vpnshop-expiry.timer — enable timers on VPS BEFORE deploying this commit (Pitfall 7)


app = FastAPI(title="VPN Shop", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ---------------- Helpers ----------------

def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_panel_client():
    return PanelClient(
        database.get_setting("panel_url", "http://127.0.0.1:5000"),
        database.get_setting("panel_token", ""),
    )


def get_platega_client():
    return services.get_platega_client()


def get_current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_support(request: Request) -> dict:
    user = require_user(request)
    if user["role"] not in ("admin", "support"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def render(request, template, **ctx):
    ctx.setdefault("user", get_current_user(request))
    ctx.setdefault("brand", _brand())
    return templates.TemplateResponse(request, template, ctx)


def _brand():
    return {
        "name": database.get_setting("brand_name", "VPN Service"),
        "emoji": database.get_setting("brand_emoji", "🛡️"),
        "subtitle": database.get_setting("brand_subtitle", "Быстрый и безопасный VPN"),
    }


def money(amount):
    if amount is None:
        return "—"
    try:
        return money_utils.fmt_rub(money_utils.to_rub(amount))
    except (TypeError, ValueError):
        return "—"


templates.env.globals["money"] = money


def format_dt(value):
    dt = parse_iso(value)
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def email_valid(email):
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or "") is not None


def find_or_create_local_user(username: str, email: str = "", telegram_id: str = ""):
    """Find an app user by username or create one (used for registration/login sync)."""
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM app_users WHERE username = ?", (username,)).fetchone()
        if row:
            return dict(row)
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO app_users (id, username, email, telegram_id, role, enabled, created_at)"
            " VALUES (?, ?, ?, ?, 'user', 1, ?)",
            (new_id, username, email, telegram_id, now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM app_users WHERE id = ?", (new_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


# ---------------- Pages: Public ----------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return render(request, "index.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM app_users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    if not row or not verify_password(password, row["password_hash"]) or not row["enabled"]:
        return render(request, "login.html", error="Неверный логин или пароль", status_code=401)
    user = dict(row)
    request.session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "telegram_id": user["telegram_id"],
        "role": user["role"],
    }
    redirect_to = request.query_params.get("next", "/dashboard")
    return RedirectResponse(url=redirect_to, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    # Capture referral from ?ref=CODE and store in a 30-day cookie
    ref = (request.query_params.get("ref") or "").strip().upper()
    resp = templates.TemplateResponse(request, "register.html",
                                      {"user": get_current_user(request), "brand": _brand(), "ref": ref})
    if ref:
        resp.set_cookie("shop_ref", ref, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp


@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    password2: str = Form(...),
):
    username = username.strip()
    if len(username) < 3:
        return render(request, "register.html", error="Логин слишком короткий", status_code=400)
    if not re.match(r"^[A-Za-z0-9_.-]+$", username):
        return render(request, "register.html", error="Логин может содержать только латиницу, цифры, _ . -", status_code=400)
    if email and not email_valid(email):
        return render(request, "register.html", error="Некорректный email", status_code=400)
    if len(password) < 6:
        return render(request, "register.html", error="Пароль слишком короткий (мин. 6 символов)", status_code=400)
    if password != password2:
        return render(request, "register.html", error="Пароли не совпадают", status_code=400)

    ref_code = (request.cookies.get("shop_ref") or "").upper().strip()

    conn = database.get_db()
    try:
        exists = conn.execute("SELECT id FROM app_users WHERE username = ?", (username,)).fetchone()
        if exists:
            return render(request, "register.html", error="Пользователь с таким логином уже существует", status_code=400)
        new_id = str(uuid.uuid4())
        referral_code = services.generate_referral_code(conn)

        referrer_id = None
        if ref_code:
            referrer = services.get_user_by_referral_code(ref_code)
            if referrer and referrer["id"] != new_id:
                referrer_id = referrer["id"]

        conn.execute(
            "INSERT INTO app_users (id, username, email, role, enabled, created_at, password_hash, referral_code, referrer_id)"
            " VALUES (?, ?, ?, 'user', 1, ?, ?, ?, ?)",
            (new_id, username, email, now_iso(), hash_password(password), referral_code, referrer_id),
        )
        services.log_event(conn, new_id, "registered")
        conn.commit()
    finally:
        conn.close()

    request.session["user"] = {
        "id": new_id, "username": username, "email": email,
        "telegram_id": "", "role": "user",
    }
    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.delete_cookie("shop_ref")
    return resp


# ---------------- Orders / Payment ----------------

@app.post("/orders/create")
async def create_order(request: Request, plan_id: int = Form(...), promo: str = Form(""), method: str = Form(""), quantity: int = Form(1)):
    user = require_user(request)
    balance = services.get_balance(user["id"])
    qty = max(1, int(quantity))

    if not method:
        # First step: let the user choose how to pay (unless balance is empty).
        quote = services.quote_order(plan_id, promo, qty, user_id=user["id"])
        if balance <= 0:
            method = "platega"
        else:
            return render(
                request, "checkout.html",
                plan_id=plan_id, promo=promo, balance=balance,
                plan_name=quote["plan_name"], price=quote["price"],
                quantity=quote["quantity"],
                pay_via_balance=balance >= quote["price"],
            )

    if method == "platega":
        try:
            order = services.create_order(user["id"], plan_id, promo, method="platega", quantity=qty)
            plan_name = order["plan_name"]
            payment_url = await services.create_platega_payment(
                order["id"], f"Оплата тарифа «{plan_name}» ×{qty}", order["payable"]
            )
        except services.OrderError as e:
            if "order" in locals() and order.get("id"):
                services.refund_order_balance(order["id"])
            return render(request, "payment_fail.html", error=str(e))
        return RedirectResponse(url=payment_url, status_code=303)

    # method == "balance"
    if balance <= 0:
        return render(request, "payment_fail.html", error="На балансе нет средств")
    try:
        order = services.create_order(user["id"], plan_id, promo, method="balance", quantity=qty)
    except services.OrderError as e:
        return render(request, "payment_fail.html", error=str(e))
    conn = database.get_db()
    try:
        await services.fulfill_order(conn, order)
    finally:
        conn.close()
    return RedirectResponse(f"/payment/success?order={order['id']}", status_code=303)


def _balance_ctx(user_id: str):
    conn = database.get_db()
    try:
        app_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,)).fetchone()
        app_user = dict(app_row) if app_row else {}
        transactions = [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 100",
            (user_id,),
        ).fetchall()]
    finally:
        conn.close()
    return float(app_user.get("balance") or 0), transactions


@app.post("/balance/topup")
async def balance_topup(request: Request, amount: float = Form(...)):
    user = require_user(request)
    balance, transactions = _balance_ctx(user["id"])
    if amount <= 0:
        return render(request, "user/balance.html", balance=balance, transactions=transactions,
                      error="Укажите сумму больше нуля")
    try:
        order = services.create_topup_order(user["id"], amount)
    except services.OrderError as e:
        return render(request, "user/balance.html", balance=balance, transactions=transactions, error=str(e))
    try:
        payment_url = await services.create_platega_payment(
            order["id"], "Пополнение баланса", order["amount_rub"]
        )
    except services.OrderError as e:
        return render(request, "user/balance.html", balance=balance, transactions=transactions, error=str(e))
    return RedirectResponse(url=payment_url, status_code=303)


@app.get("/payment/success", response_class=HTMLResponse)
async def payment_success(request: Request, order: str = ""):
    user = get_current_user(request)
    order_row = None
    connections = []
    if order:
        conn = database.get_db()
        try:
            order_row = conn.execute("SELECT * FROM orders WHERE id = ?", (order,)).fetchone()
            if order_row:
                order_row = dict(order_row)
                raw = order_row.get("panel_user_connections") or "[]"
                connections = json.loads(raw)
        finally:
            conn.close()
    return render(request, "payment_success.html", order=order_row, connections=connections)


@app.get("/payment/fail", response_class=HTMLResponse)
async def payment_fail(request: Request):
    return render(request, "payment_fail.html")


@app.get("/orders/{order_id}/config/{idx}/download")
async def download_config(request: Request, order_id: str, idx: int):
    user = require_user(request)
    conn = database.get_db()
    try:
        order = conn.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
    finally:
        conn.close()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    try:
        connections = json.loads(order["panel_user_connections"] or "[]")
    except json.JSONDecodeError:
        raise HTTPException(status_code=404, detail="Конфигурации не найдены")
    if idx < 0 or idx >= len(connections):
        raise HTTPException(status_code=404, detail="Конфигурация не найдена")
    c = connections[idx]
    filename = f"{c.get('name') or 'config'}.conf"
    return Response(
        content=c.get("config", ""),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename.encode().decode("latin-1")}"'},
    )


def _get_order_connection(request, order_id: str, idx: int):
    """Helper: load a user's connection dict by order + index."""
    user = require_user(request)
    conn = database.get_db()
    try:
        order = conn.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
    finally:
        conn.close()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    try:
        connections = json.loads(order["panel_user_connections"] or "[]")
    except json.JSONDecodeError:
        raise HTTPException(status_code=404, detail="Конфигурации не найдены")
    if idx < 0 or idx >= len(connections):
        raise HTTPException(status_code=404, detail="Конфигурация не найдена")
    return dict(order), connections[idx]


@app.get("/orders/{order_id}/config/{idx}/qr/{chunk_idx}")
async def qr_chunk(request: Request, order_id: str, idx: int, chunk_idx: int):
    """Render one QR frame of an Amnezia config as an SVG."""
    _, c = _get_order_connection(request, order_id, idx)
    chunks = c.get("vpn_qr_chunks") or []
    if chunk_idx < 0 or chunk_idx >= len(chunks):
        raise HTTPException(status_code=404, detail="Фрагмент не найден")
    try:
        import segno
        import io
        qr = segno.make(chunks[chunk_idx], error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="svg", scale=8, border=2)
        svg = buf.getvalue().decode("utf-8")
    except Exception:
        svg = ""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/orders/{order_id}/config/{idx}/qr")
async def config_qr_page(request: Request, order_id: str, idx: int):
    """Full-screen Amnezia-style connect page: sequential QR frames + vpn link."""
    order, c = _get_order_connection(request, order_id, idx)
    chunks = c.get("vpn_qr_chunks") or []
    return render(request, "user/config_qr.html",
                  order_id=order_id, idx=idx, conn=c, chunks=chunks,
                  vpn_link=c.get("vpn_link") or "")


@app.post("/payment/trial")
async def activate_trial(request: Request):
    user = require_user(request)
    try:
        await services.activate_trial(user["id"])
    except services.OrderError as e:
        return render(request, "payment_fail.html", error=str(e))
    return RedirectResponse(url="/dashboard", status_code=303)


async def ensure_panel_user(user: dict) -> dict:
    """Look up / create the corresponding panel user, returning its dict."""
    panel = get_panel_client()
    try:
        p_user = await panel.find_user_by_username(user["username"])
    except Exception:
        # panel unreachable or lookup failed — attempt creation below anyway
        p_user = None
    if p_user:
        return p_user
    # create
    pw = secrets.token_urlsafe(12)
    resp = await panel.create_panel_user(
        username=user["username"], password=pw, email=user.get("email") or "", role="user"
    )
    uid = resp.get("user_id")
    if not uid:
        raise HTTPException(status_code=500, detail="Не удалось создать пользователя в панели")
    return {"id": uid}


# ---------------- Webhook: Platega ----------------

@app.post("/webhook/callback")
async def platega_callback(request: Request):
    merchant_id = request.headers.get("X-MerchantId")
    secret = request.headers.get("X-Secret")
    # Accept both production and test credentials so callbacks work in either mode
    expected_mid = database.get_setting("platega_merchant_id", "")
    expected_secret = database.get_setting("platega_secret", "")
    test_mid = database.get_setting("platega_test_merchant_id", "")
    test_secret = database.get_setting("platega_test_secret", "")
    valid = (
        (bool(expected_mid and expected_secret)
         and hmac.compare_digest(merchant_id or "", expected_mid)
         and hmac.compare_digest(secret or "", expected_secret or ""))
        or (bool(test_mid and test_secret)
            and hmac.compare_digest(merchant_id or "", test_mid)
            and hmac.compare_digest(secret or "", test_secret))
    )
    if not valid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    status = body.get("status")
    txn_id = body.get("id") or body.get("transactionId")
    payload = body.get("payload", "")

    conn = database.get_db()
    try:
        order = None
        if payload:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (payload,)).fetchone()
        if not order and txn_id:
            order = conn.execute(
                "SELECT * FROM orders WHERE platega_transaction_id = ?", (txn_id,)
            ).fetchone()
        if not order:
            return JSONResponse({"error": "Order not found"}, status_code=404)
        order = dict(order)
    finally:
        conn.close()

    if status == "CONFIRMED":
        with database.tx() as conn:
            claim = conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'pending'",
                (now_iso(), order["id"]),
            )
            if claim.rowcount != 1:
                existing = conn.execute(
                    "SELECT * FROM orders WHERE id = ?", (order["id"],)
                ).fetchone()
                if not existing or existing["status"] != "paid" or not existing["provisioning_error"]:
                    return Response(status_code=200)
                claimed_order = dict(existing)
            else:
                claimed_order = dict(
                    conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
                )
                if claimed_order.get("plan_id") is None:
                    services.add_balance_int(
                        conn,
                        claimed_order["user_id"],
                        money_utils.to_rub(claimed_order["amount_rub"] or 0),
                        "topup",
                        ref_order_id=claimed_order["id"],
                        note="Пополнение баланса",
                    )
                    services.log_event(conn, claimed_order["user_id"], "topup_paid")
                else:
                    services.log_event(conn, claimed_order["user_id"], "order_paid")
                services.claim_promo_usage(conn, claimed_order)
                services.claim_apply_referral(conn, claimed_order)
        if claimed_order.get("plan_id") is not None:
            await services.fulfill_order_side_effects(claimed_order)
    elif status == "CANCELED":
        with database.tx() as conn:
            services._refund_order_balance_tx(conn, order["id"])

    return Response(status_code=200)


# ---------------- Referral rewards ----------------

async def fulfill_order(conn, order: dict):
    return await services.fulfill_order(conn, order)


# ---------------- User Dashboard ----------------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = require_user(request)
    subscription = services.get_active_subscription(user["id"])
    conn = database.get_db()
    try:
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()]
        app_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        app_user = dict(app_row) if app_row else {}
        referred_users = [dict(r) for r in conn.execute(
            "SELECT username, created_at FROM app_users WHERE referrer_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()]
        referred_paid_count = conn.execute(
            "SELECT COUNT(*) AS c FROM app_users WHERE referrer_id = ? AND referred_paid = 1",
            (user["id"],),
        ).fetchone()["c"]
        plans = [dict(r) for r in conn.execute(
            "SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order, id"
        ).fetchall()]
    finally:
        conn.close()

    connections = []
    if subscription:
        try:
            connections = json.loads(subscription.get("panel_user_connections") or "[]")
        except json.JSONDecodeError:
            connections = []

    used_trial = services.has_used_trial(user["id"])
    trial_days = database.get_setting("test_subscription_days", "3")
    shop_url = database.get_setting("shop_public_url", "http://127.0.0.1:8080").rstrip("/")
    return render(
        request, "user/dashboard.html",
        subscription=subscription, orders=orders, connections=connections, plans=plans,
        used_trial=used_trial, trial_days=trial_days,
        balance=float(app_user.get("balance") or 0),
        referral_code=app_user.get("referral_code") or "",
        referral_enabled=database.get_setting("referral_enabled", "True"),
        referral_link=f"{shop_url}/register?ref={app_user.get('referral_code') or ''}" if app_user.get("referral_code") else "",
        referred_users=referred_users,
        referred_paid_count=referred_paid_count,
    )


@app.get("/dashboard/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = require_user(request)
    conn = database.get_db()
    try:
        app_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        app_user = dict(app_row) if app_row else {}
        notifications = [dict(r) for r in conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (user["id"],)
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "user/profile.html", user=user, app_user=app_user, notifications=notifications)


@app.post("/dashboard/profile/auto-renewal/toggle")
async def auto_renewal_toggle(request: Request, enabled: str = Form("")):
    user = require_user(request)
    enabled = enabled in ("1", "true", "on", "yes")
    conn = database.get_db()
    try:
        services.set_auto_renewal(conn, user["id"], enabled)
        conn.commit()
        # update session
        request.session["user"] = {**user, "auto_renewal": 1 if enabled else 0, "auto_renewal_at": now_iso() if enabled else None}
    finally:
        conn.close()
    return RedirectResponse(url="/dashboard/profile", status_code=303)


@app.post("/dashboard/profile/notifications/read")
async def mark_notification_read(request: Request, notification_id: int = Form(0), all: int = Form(0)):
    user = require_user(request)
    conn = database.get_db()
    try:
        if all:
            conn.execute(
                "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
                (user["id"],)
            )
        elif notification_id:
            conn.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
                (notification_id, user["id"])
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/dashboard/profile", status_code=303)


async def _balance_page(request: Request):
    user = require_user(request)
    conn = database.get_db()
    try:
        app_row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        app_user = dict(app_row) if app_row else {}
        transactions = [dict(r) for r in conn.execute(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 100",
            (user["id"],),
        ).fetchall()]
    finally:
        conn.close()
    return render(
        request, "user/balance.html",
        balance=float(app_user.get("balance") or 0),
        transactions=transactions,
    )


@app.get("/balance", response_class=HTMLResponse)
async def balance_alias(request: Request):
    return await _balance_page(request)


@app.get("/dashboard/balance", response_class=HTMLResponse)
async def balance_page(request: Request):
    return await _balance_page(request)


@app.post("/dashboard/profile/save")
async def profile_save(
    request: Request,
    email: str = Form(""),
    telegram_id: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
):
    user = require_user(request)
    if email and not email_valid(email):
        return render(request, "user/profile.html", user=user, error="Некорректный email")
    if new_password and len(new_password) < 6:
        return render(request, "user/profile.html", user=user, error="Новый пароль слишком короткий")

    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM app_users WHERE id = ?", (user["id"],)).fetchone()
        if new_password:
            if not row or not verify_password(current_password, row["password_hash"]):
                return render(request, "user/profile.html", user=user, error="Текущий пароль неверен")
            conn.execute(
                "UPDATE app_users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user["id"]),
            )
        conn.execute(
            "UPDATE app_users SET email = ?, telegram_id = ? WHERE id = ?",
            (email, telegram_id, user["id"]),
        )
        conn.commit()
        # update session
        request.session["user"] = {**user, "email": email, "telegram_id": telegram_id}

        # sync to panel
        try:
            panel = get_panel_client()
            p_user = await panel.find_user_by_username(user["username"])
            if p_user:
                update_fields = {}
                if email:
                    update_fields["email"] = email
                if telegram_id:
                    update_fields["telegramId"] = telegram_id
                if new_password:
                    update_fields["password"] = new_password
                if update_fields:
                    await panel.update_panel_user(p_user["id"], **update_fields)
        except Exception:
            pass
    finally:
        conn.close()

    return RedirectResponse(url="/dashboard/profile", status_code=303)


@app.get("/dashboard/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    user = require_user(request)
    conn = database.get_db()
    try:
        rows = conn.execute(
            "SELECT o.*, p.name AS plan_name FROM orders o LEFT JOIN plans p ON o.plan_id = p.id"
            " WHERE o.user_id = ? ORDER BY o.created_at DESC",
            (user["id"],),
        ).fetchall()
        orders = [dict(r) for r in rows]
    finally:
        conn.close()
    return render(request, "user/orders.html", orders=orders)


@app.post("/dashboard/orders/{order_id}/delete")
async def delete_order(request: Request, order_id: str):
    user = require_user(request)
    conn = database.get_db()
    try:
        # Check ownership and status
        order = conn.execute(
            "SELECT status FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user["id"])
        ).fetchone()
        if not order:
            return JSONResponse({"error": "Заказ не найден"}, status_code=404)
        if order["status"] == 'paid':
            return JSONResponse({"error": "Нельзя удалить оплаченный заказ"}, status_code=400)
        if order["status"] == "pending":
            if not services.cancel_pending_order(order_id):
                return JSONResponse({"error": "Заказ уже обрабатывается"}, status_code=409)
        else:
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


# ---------------- Support (user) ----------------

@app.get("/dashboard/support", response_class=HTMLResponse)
async def support_page(request: Request):
    user = require_user(request)
    conn = database.get_db()
    try:
        tickets = [dict(r) for r in conn.execute(
            "SELECT * FROM support_tickets WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "user/support.html", tickets=tickets)


@app.post("/dashboard/support/create")
async def support_create(request: Request, subject: str = Form(...), message: str = Form(...)):
    user = require_user(request)
    if not subject.strip() or not message.strip():
        return JSONResponse({"error": "Заполните тему и сообщение"}, status_code=400)
    conn = database.get_db()
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO support_tickets (user_id, subject, status, created_at, updated_at)"
            " VALUES (?, ?, 'open', ?, ?)",
            (user["id"], subject, now, now),
        )
        ticket_id = cur.lastrowid
        conn.execute(
            "INSERT INTO support_messages (ticket_id, sender_id, sender_role, message, created_at)"
            " VALUES (?, ?, 'user', ?, ?)",
            (ticket_id, user["id"], message, now),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/dashboard/support/{ticket_id}", status_code=303)


@app.get("/dashboard/support/{ticket_id}", response_class=HTMLResponse)
async def support_ticket(request: Request, ticket_id: int):
    user = require_user(request)
    conn = database.get_db()
    try:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE id = ? AND user_id = ?", (ticket_id, user["id"])
        ).fetchone()
        if not ticket:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        messages = [dict(r) for r in conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "user/ticket_chat.html", ticket=dict(ticket), messages=messages)


@app.post("/dashboard/support/{ticket_id}/message")
async def support_ticket_message(request: Request, ticket_id: int, message: str = Form(...)):
    user = require_user(request)
    if not message.strip():
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO support_messages (ticket_id, sender_id, sender_role, message, created_at)"
            " VALUES (?, ?, 'user', ?, ?)",
            (ticket_id, user["id"], message, now_iso()),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now_iso(), ticket_id)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/dashboard/support/{ticket_id}", status_code=303)


# ---------------- Admin ----------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    require_admin(request)
    conn = database.get_db()
    try:
        total_revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_rub),0) AS s FROM orders WHERE status='paid' AND is_trial=0"
        ).fetchone()["s"]
        today_start = (utcnow().replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
        today_revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_rub),0) AS s FROM orders WHERE status='paid' AND is_trial=0 AND paid_at >= ?",
            (today_start,),
        ).fetchone()["s"]
        paid_orders = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status='paid'").fetchone()["c"]
        pending_orders = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status='pending'").fetchone()["c"]
        active_subs = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status='paid' AND expires_at > ?", (now_iso(),)
        ).fetchone()["c"]
        expiring_soon = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE status='paid' AND expires_at > ?"
            " AND expires_at <= ? ORDER BY expires_at",
            (now_iso(), (utcnow() + timedelta(days=3)).isoformat()),
        ).fetchall()]
        open_tickets = conn.execute("SELECT COUNT(*) AS c FROM support_tickets WHERE status='open'").fetchone()["c"]
    finally:
        conn.close()

    try:
        servers = await get_panel_client().get_servers_with_protocols(installed_only=True)
    except Exception:
        servers = []
    return render(
        request, "admin/dashboard.html",
        total_revenue=total_revenue, today_revenue=today_revenue,
        paid_orders=paid_orders, pending_orders=pending_orders,
        active_subs=active_subs, expiring_soon=expiring_soon,
        open_tickets=open_tickets, servers=servers,
    )


@app.get("/admin/plans", response_class=HTMLResponse)
async def admin_plans(request: Request):
    require_admin(request)
    conn = database.get_db()
    try:
        plans = [dict(r) for r in conn.execute(
            "SELECT * FROM plans ORDER BY sort_order, id"
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "admin/plans.html", plans=plans)


@app.post("/admin/plans/add")
async def admin_plans_add(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    price_rub: float = Form(...),
    duration_days: int = Form(...),
):
    require_admin(request)
    price_rub = money_utils.to_rub(price_rub)
    if price_rub < 0:
        return JSONResponse({"error": "Цена не может быть отрицательной"}, status_code=400)
    conn = database.get_db()
    try:
        max_sort = conn.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM plans").fetchone()["m"]
        conn.execute(
            "INSERT INTO plans (name, description, price_rub, duration_days, sort_order, created_at, is_active)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (name, description, price_rub, duration_days, max_sort + 1, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/plans", status_code=303)


@app.post("/admin/plans/{plan_id}/update")
async def admin_plans_update(
    request: Request, plan_id: int,
    name: str = Form(...),
    description: str = Form(""),
    price_rub: float = Form(...),
    duration_days: int = Form(...),
    is_active: int = Form(1),
):
    require_admin(request)
    price_rub = money_utils.to_rub(price_rub)
    if price_rub < 0:
        return JSONResponse({"error": "Цена не может быть отрицательной"}, status_code=400)
    conn = database.get_db()
    try:
        conn.execute(
            "UPDATE plans SET name=?, description=?, price_rub=?, duration_days=?, is_active=? WHERE id=?",
            (name, description, price_rub, duration_days, is_active, plan_id),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/plans", status_code=303)


@app.post("/admin/plans/{plan_id}/delete")
async def admin_plans_delete(request: Request, plan_id: int):
    require_admin(request)
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/plans", status_code=303)


def _parse_promo_datetime(value: str) -> str | None:
    """Parse a datetime-local form value into a tz-aware UTC ISO string.

    Per P-04 ("No user timezone — server time only") the naive datetime-local
    string is interpreted as SERVER-LOCAL time and shifted to UTC, so the
    stored bound always ends with +00:00 (canonical now_iso() format, D-08).
    Empty/whitespace → None (no bound). A malformed string raises ValueError,
    caught by the admin route into a 400 — the helper stays pure.
    """
    value = (value or "").strip()
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def _promo_window_valid(valid_from: str | None, valid_until: str | None) -> bool:
    """True unless both bounds are set and valid_from > valid_until (P-12).

    Either NULL bound is always valid (no ordering constraint). String
    comparison is safe because both sides are the uniform UTC ISO format
    (+00:00) produced by _parse_promo_datetime.
    """
    if valid_from and valid_until:
        return valid_from <= valid_until
    return True


@app.get("/admin/promos", response_class=HTMLResponse)
async def admin_promos(request: Request):
    require_admin(request)
    conn = database.get_db()
    try:
        promos = [dict(r) for r in conn.execute("SELECT * FROM promo_codes ORDER BY id DESC").fetchall()]
    finally:
        conn.close()
    return render(request, "admin/promos.html", promos=promos)


@app.post("/admin/promos/add")
async def admin_promos_add(
    request: Request,
    code: str = Form(...),
    discount_percent: int = Form(0),
    discount_amount_rub: float = Form(0),
    max_uses: int = Form(0),
    first_purchase_only: int = Form(0),
    max_uses_per_user: int = Form(0),
    valid_from: str = Form(""),
    valid_until: str = Form(""),
):
    require_admin(request)
    code = code.strip().upper()
    if not code:
        return JSONResponse({"error": "Введите код"}, status_code=400)
    discount_amount_rub = money_utils.to_rub(discount_amount_rub)
    if discount_amount_rub < 0:
        return JSONResponse({"error": "Скидка не может быть отрицательной"}, status_code=400)
    if max_uses_per_user < 0:
        return JSONResponse({"error": "Лимит на пользователя не может быть отрицательным"}, status_code=400)
    try:
        valid_from_utc = _parse_promo_datetime(valid_from)
        valid_until_utc = _parse_promo_datetime(valid_until)
    except ValueError:
        return JSONResponse({"error": "Дата указана в неверном формате"}, status_code=400)
    if not _promo_window_valid(valid_from_utc, valid_until_utc):
        return JSONResponse({"error": "Дата начала не может быть позже даты окончания"}, status_code=400)
    conn = database.get_db()
    try:
        exists = conn.execute("SELECT id FROM promo_codes WHERE code = ?", (code,)).fetchone()
        if exists:
            return JSONResponse({"error": "Такой промокод уже существует"}, status_code=400)
        conn.execute(
            "INSERT INTO promo_codes (code, discount_percent, discount_amount_rub, max_uses, first_purchase_only, max_uses_per_user, valid_from, valid_until, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (code, discount_percent, discount_amount_rub, max_uses or None,
             1 if first_purchase_only else 0,
             max_uses_per_user or None,
             valid_from_utc, valid_until_utc, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/promos", status_code=303)


@app.post("/admin/promos/{promo_id}/toggle")
async def admin_promos_toggle(request: Request, promo_id: int):
    require_admin(request)
    conn = database.get_db()
    try:
        conn.execute("UPDATE promo_codes SET is_active = 1 - is_active WHERE id = ?", (promo_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/promos", status_code=303)


@app.post("/admin/promos/{promo_id}/delete")
async def admin_promos_delete(request: Request, promo_id: int):
    require_admin(request)
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/promos", status_code=303)


@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders(request: Request, status: str = ""):
    require_admin(request)
    conn = database.get_db()
    try:
        query = ("SELECT o.*, p.name AS plan_name, u.username FROM orders o"
                 " LEFT JOIN plans p ON o.plan_id = p.id LEFT JOIN app_users u ON o.user_id = u.id")
        params = []
        if status:
            query += " WHERE o.status = ?"
            params.append(status)
        query += " ORDER BY o.created_at DESC"
        orders = [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
    return render(request, "admin/orders.html", orders=orders, status=status)


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    require_admin(request)
    conn = database.get_db()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM shop_settings").fetchall()}
    finally:
        conn.close()
    return render(request, "admin/settings.html", settings=settings)


@app.post("/admin/settings/save")
async def admin_settings_save(request: Request):
    require_admin(request)
    form = await request.form()
    immutable_keys = {"brand_name", "brand_emoji", "brand_subtitle", "panel_url", "panel_token",
                      "platega_merchant_id", "platega_secret", "shop_public_url", "test_subscription_days",
                      "platega_test_mode", "platega_test_merchant_id", "platega_test_secret",
                      "platega_test_base_url",
                      "referral_enabled", "referral_threshold", "referral_bonus_referee",
                      "referral_bonus_referrer", "referral_commission_percent",
                      "telegram_bot_token"}
    conn = database.get_db()
    try:
        # Handle checkboxes when unchecked (no value sent)
        for cb in ("referral_enabled", "platega_test_mode"):
            if cb not in form:
                conn.execute(
                    "INSERT INTO shop_settings (key, value) VALUES (?, 'False')"
                    " ON CONFLICT(key) DO UPDATE SET value = 'False'",
                    (cb,),
                )
        for key, value in form.items():
            if key in immutable_keys:
                conn.execute(
                    "INSERT INTO shop_settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/settings", status_code=303)


# ---------------- Admin support ----------------

@app.get("/admin/support", response_class=HTMLResponse)
async def admin_support(request: Request):
    require_support(request)
    conn = database.get_db()
    try:
        tickets = [dict(r) for r in conn.execute(
            "SELECT t.*, u.username FROM support_tickets t LEFT JOIN app_users u ON t.user_id = u.id"
            " ORDER BY t.updated_at DESC"
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "admin/support.html", tickets=tickets)


@app.get("/admin/support/{ticket_id}", response_class=HTMLResponse)
async def admin_support_ticket(request: Request, ticket_id: int):
    require_support(request)
    conn = database.get_db()
    try:
        ticket = conn.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        messages = [dict(r) for r in conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()]
    finally:
        conn.close()
    return render(request, "admin/ticket_chat.html", ticket=dict(ticket), messages=messages)


@app.post("/admin/support/{ticket_id}/message")
async def admin_support_ticket_message(request: Request, ticket_id: int, message: str = Form(...)):
    user = require_support(request)
    if not message.strip():
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO support_messages (ticket_id, sender_id, sender_role, message, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ticket_id, user["id"], user["role"], message, now_iso()),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now_iso(), ticket_id)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin/support/{ticket_id}", status_code=303)


@app.post("/admin/support/{ticket_id}/close")
async def admin_support_ticket_close(request: Request, ticket_id: int):
    require_support(request)
    conn = database.get_db()
    try:
        row = conn.execute("SELECT status FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row:
            new_status = "open" if row["status"] == "closed" else "closed"
            conn.execute(
                "UPDATE support_tickets SET status=?, updated_at=? WHERE id=?",
                (new_status, now_iso(), ticket_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/admin/support", status_code=303)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("SHOP_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
