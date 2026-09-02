"""Telegram shop bot for the VPN-shop.

Runs as a standalone process (`python bot.py`) alongside the web app. Reuses all
business logic from services.py, so the bot and the web shop behave identically.

Flow: /start -> registration (or login by telegram_id) -> main menu:
  - Магазин / Тарифы  (выбрать тариф, промокод, получить ссылку на оплату)
  - Мои подписки       (активная + история)
  - Продлить подписку
  - Тестовый период    (активация)
  - Баланс             (бонусы + операции)
  - Тикеты             (создать, мои обращения, чат)
  - Реферальная система (код + ссылка)
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import database
import services

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_bot: Bot = None
REGISTER_SECRET = None  # set from settings at start

dp = Dispatcher()


# ---------------- Callback factories ----------------

class MenuCB(CallbackData, prefix="m"):
    action: str


class PlanCB(CallbackData, prefix="p"):
    id: int


class TicketCB(CallbackData, prefix="t"):
    id: int


class OrderCB(CallbackData, prefix="o"):
    id: str
    action: str = "pay"


# ---------------- FSM states ----------------

class RegisterState(StatesGroup):
    username = State()
    referrer = State()


class BuyState(StatesGroup):
    promo = State()


class TicketState(StatesGroup):
    subject = State()
    message = State()
    chat = State()


# ---------------- Helpers ----------------

def _markup(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def menu_btn():
    return [InlineKeyboardButton(text="◀️ Меню", callback_data=MenuCB(action="menu").pack())]


def main_menu():
    return _markup(
        [InlineKeyboardButton(text="🛒 Магазин / Тарифы", callback_data=MenuCB(action="plans").pack())],
        [InlineKeyboardButton(text="📋 Мои подписки", callback_data=MenuCB(action="subs").pack()),
         InlineKeyboardButton(text="🔁 Продлить", callback_data=MenuCB(action="renew").pack())],
        [InlineKeyboardButton(text="🎁 Тестовый период", callback_data=MenuCB(action="trial").pack())],
        [InlineKeyboardButton(text="💰 Баланс", callback_data=MenuCB(action="balance").pack()),
         InlineKeyboardButton(text="🎧 Тикеты", callback_data=MenuCB(action="tickets").pack())],
        [InlineKeyboardButton(text="🔗 Реферальная система", callback_data=MenuCB(action="referral").pack())],
    )


def money(value) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def resolve_user(telegram_id) -> dict | None:
    return services.get_user_by_telegram(telegram_id)


def require_user(message: Message) -> dict | None:
    return services.get_user_by_telegram(message.from_user.id)


# ---------------- Registration ----------------

async def _start_registration(message: Message, state: FSMContext):
    await state.set_state(RegisterState.username)
    await message.answer(
        "Вас нет в системе. Создадим аккаунт?\n\n"
        "Придумайте <b>логин</b> (латиница, 3+ символа):"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = resolve_user(message.from_user.id)
    if user:
        await message.answer(
            f"Здравствуйте, <b>{user['username']}</b>! Добро пожаловать в магазин VPN 🛡️",
            reply_markup=main_menu(),
        )
        return
    await _start_registration(message, state)


@dp.message(F.text, StateFilter(RegisterState.username))
async def reg_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3 or not all(c.isalnum() or c in "._-" for c in username):
        await message.answer("Логин должен содержать 3+ символа: латиница, цифры, _ . -")
        return
    if services.get_user_by_username(username):
        await message.answer("Этот логин уже занят. Выберите другой:")
        return
    await state.update_data(username=username)
    await state.set_state(RegisterState.referrer)
    await message.answer(
        "Отлично! Если у вас есть реферальный код друга — введите его.\n"
        "Если нет, нажмите «Пропустить».",
        reply_markup=_markup([InlineKeyboardButton(text="Пропустить ⏭", callback_data=MenuCB(action="reg_skip").pack())]),
    )


@dp.callback_query(MenuCB.filter(F.action == "reg_skip"))
async def reg_skip(cb: CallbackQuery, state: FSMContext, ):
    await _finish_registration(cb.message, state, "")


@dp.message(F.text, StateFilter(RegisterState.referrer))
async def reg_referrer(message: Message, state: FSMContext):
    await _finish_registration(message, state, message.text.strip())


async def _finish_registration(message, state: FSMContext, ref_code: str):
    data = await state.get_data()
    username = data.get("username")
    referrer_id = None
    if ref_code:
        ref = services.get_user_by_referral_code(ref_code)
        if ref:
            referrer_id = ref["id"]

    user = services.create_user(username=username, email="")
    services.attach_telegram(user["id"], message.from_user.id)
    if referrer_id:
        conn = database.get_db()
        try:
            conn.execute("UPDATE app_users SET referrer_id = ? WHERE id = ?", (referrer_id, user["id"]))
            conn.commit()
        finally:
            conn.close()

    await state.clear()
    uid = message.from_user.id
    await get_bot().send_message(uid,
        f"✅ Аккаунт <b>{user['username']}</b> создан!\n"
        f"Ваш реферальный код: <b>{user['referral_code']}</b>\n\n"
        f"Выберите действие:", reply_markup=main_menu())


# ---------------- Main menu callback ----------------

@dp.callback_query(MenuCB.filter(F.action == "menu"))
async def m_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Главное меню:", reply_markup=main_menu())


@dp.callback_query(MenuCB.filter(F.action == "plans"))
async def m_plans(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    await _show_plans(cb.message, state, renew=False)


@dp.callback_query(MenuCB.filter(F.action == "renew"))
async def m_renew(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    await _show_plans(cb.message, state, renew=True)


async def _show_plans(message, state: FSMContext, renew: bool):
    plans = services.list_active_plans()
    if not plans:
        await message.answer("Пока нет доступных тарифов.")
        return
    if renew:
        await state.update_data(renew=True)
        head = "Выберите тариф для <b>продления</b>:"
    else:
        await state.update_data(renew=False)
        head = "Выберите тариф <b>Магазин</b>:"
    buttons = [
        InlineKeyboardButton(
            text=f"{p['name']} — {money(p['price_rub'])} / {p['duration_days']} дн.",
            callback_data=PlanCB(id=p["id"]).pack(),
        )
        for p in plans
    ]
    kb = _markup(*[[b] for b in buttons], menu_btn())
    await message.answer(head, reply_markup=kb)


@dp.callback_query(PlanCB.filter())
async def on_plan(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    plan_id = PlanCB.unpack(cb.data).id
    plan = services.get_plan(plan_id, active_only=True)
    if not plan:
        await cb.answer("Тариф недоступен", show_alert=True)
        return
    data = await state.get_data()
    renew = data.get("renew", False)
    await state.update_data(plan_id=plan_id, renew=renew)
    await state.set_state(BuyState.promo)
    await cb.message.answer(
        f"✅ <b>{plan['name']}</b> — {money(plan['price_rub'])} / {plan['duration_days']} дн.\n\n"
        f"Введите промокод, если есть. Если нет — напишите <b>«нет»</b> или нажмите кнопку.",
        reply_markup=_markup([InlineKeyboardButton(text="Без промокода ⏭", callback_data=MenuCB(action="no_promo").pack())]),
    )


@dp.callback_query(MenuCB.filter(F.action == "no_promo"))
async def no_promo(cb: CallbackQuery, state: FSMContext):
    await _buy(cb.message, state, "")


@dp.message(F.text, StateFilter(BuyState.promo))
async def buy_promo(message: Message, state: FSMContext):
    promo = message.text.strip()
    if promo.lower() in ("нет", "no", "-", "skip", "пропустить"):
        promo = ""
    await _buy(message, state, promo)


async def _buy(message, state: FSMContext, promo: str):
    user = resolve_user(message.from_user.id)
    data = await state.get_data()
    plan_id = data.get("plan_id")
    renew = data.get("renew", False)
    if not user or not plan_id:
        await message.answer("Ошибка оформления. Начните заново.", reply_markup=main_menu())
        return
    try:
        order = services.create_order(user["id"], plan_id, promo)
        pay_url = await services.create_platega_payment(
            order["id"], f"Оплата тарифа «{order['plan_name']}»", order["payable"]
        )
    except services.OrderError as e:
        if "order" in locals():
            services.refund_order_balance(order["id"])
        await message.answer(f"❌ {e}", reply_markup=main_menu())
        return

    await state.clear()
    txt = (
        f"💳 <b>Заказ создан</b>\n"
        f"Тариф: <b>{order['plan_name']}</b>\n"
        f"Стоимость: {money(order['price'])}\n"
    )
    if order["balance_used"]:
        txt += f"Списано с баланса: {money(order['balance_used'])}\n"
    txt += f"К оплате: <b>{money(order['payable'])}</b>\n\n"
    if order["payable"] <= 0:
        # fully covered by balance -> pay immediately via webhook-less path
        txt += "Оплата полностью покрыта балансом. Подписка активируется автоматически."
        await message.answer(txt, reply_markup=_markup(
            [InlineKeyboardButton(text="✅ Подтвердить оплату балансом", callback_data=OrderCB(id=order["id"], action="confirm_balance").pack())],
            menu_btn(),
        ))
        return
    txt += "Нажмите Оплатить, чтобы перейти к оплате:"
    await message.answer(txt, reply_markup=_markup(
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        menu_btn(),
    ))


@dp.callback_query(OrderCB.filter(F.action == "confirm_balance"))
async def confirm_balance(cb: CallbackQuery, state: FSMContext):
    order_id = OrderCB.unpack(cb.data).id
    user = resolve_user(cb.from_user.id)
    order = services.get_order(order_id)
    if not order or order["user_id"] != user["id"]:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] == "paid":
        await cb.answer("Уже оплачено", show_alert=True)
        return
    if float(order.get("amount_rub") or 0) > 0 and not order.get("synthetic"):
        await cb.answer("Этот заказ требует оплаты через платёжную систему.", show_alert=True)
        return
    # Fulfill using services (balance already debited at creation)
    await _fulfill_and_ack(cb, order)


async def _fulfill_and_ack(cb_or_msg, order):
    conn = database.get_db()
    try:
        await services.fulfill_order(conn, dict(order))
    finally:
        conn.close()
    updated = services.get_order(order["id"])
    subs = services.get_active_subscription(order["user_id"])
    txt = f"✅ <b>Оплата подтверждена!</b>\nТариф: {order['plan_name']}\n"
    if subs and subs.get("expires_at"):
        txt += f"Подписка активна до: <b>{subs['expires_at'][:10]}</b>\n"
    txt += "\nКонфиги доступны в ЛК: посмотрите «Мои подписки»."
    if isinstance(cb_or_msg, CallbackQuery):
        await cb_or_msg.message.edit_text(txt, reply_markup=main_menu())
    else:
        await cb_or_msg.answer(txt, reply_markup=main_menu())


# ---------------- Subscriptions ----------------

@dp.callback_query(MenuCB.filter(F.action == "subs"))
async def m_subs(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    active = services.get_active_subscription(user["id"])
    orders = services.list_user_orders(user["id"])[:10]
    lines = ["<b>📋 Мои подписки</b>\n"]
    if active:
        lines.append(f"✅ <b>Активна:</b> {active['plan_name']} до {active['expires_at'][:10]} (всего {active.get('expires_at','')[:10]})")
    else:
        lines.append("Активных подписок нет.")
    if orders:
        lines.append("\n<b>История:</b>")
        for o in orders:
            st = {"paid": "✅", "pending": "⏳", "cancelled": "❌", "expired": "⏰"}.get(o["status"], "•")
            lines.append(f"{st} {o.get('plan_name')} — {o['status']} ({o.get('created_at','')[:10]})")
    await cb.message.answer("\n".join(lines), reply_markup=_markup(menu_btn()))


# ---------------- Trial ----------------

@dp.callback_query(MenuCB.filter(F.action == "trial"))
async def m_trial(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    if services.has_used_trial(user["id"]):
        await cb.message.answer("Тестовая подписка уже была использована.", reply_markup=main_menu())
        return
    await cb.message.answer(
        "🎁 Активировать <b>тестовый период</b>? Количество дней задаёт администратор.",
        reply_markup=_markup(
            [InlineKeyboardButton(text="✅ Активировать", callback_data=MenuCB(action="trial_do").pack())],
            menu_btn(),
        ),
    )


@dp.callback_query(MenuCB.filter(F.action == "trial_do"))
async def trial_do(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    try:
        res = await services.activate_trial(user["id"])
    except services.OrderError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=main_menu())
        return
    days = services.get_setting_float("test_subscription_days", 3)
    await cb.message.answer(
        f"🎉 <b>Тестовая подписка активирована!</b>\nДо {res['expires_at'][:10]}.\n"
        f"Конфиги уже созданы — смотрите «Мои подписки».",
        reply_markup=main_menu(),
    )


# ---------------- Balance ----------------

@dp.callback_query(MenuCB.filter(F.action == "balance"))
async def m_balance(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    bal = services.get_balance(user["id"])
    tx = services.list_balance_transactions(user["id"], limit=10)
    lines = [f"<b>💰 Баланс:</b> {money(bal)}\n"]
    if tx:
        lines.append("<b>Последние операции:</b>")
        for t in tx:
            sign = "+" if t["amount"] >= 0 else ""
            lines.append(f"{sign}{money(t['amount'])} — {t.get('note') or t['kind']} ({t.get('created_at','')[:10]})")
    else:
        lines.append("Операций пока нет.")
    await cb.message.answer("\n".join(lines), reply_markup=_markup(menu_btn()))


# ---------------- Referral ----------------

@dp.callback_query(MenuCB.filter(F.action == "referral"))
async def m_referral(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    if not services.get_setting_bool("referral_enabled", True):
        await cb.message.answer("Реферальная программа выключена.", reply_markup=main_menu())
        return
    site = services.get_site_url()
    link = f"{site}/register?ref={user['referral_code']}"
    await cb.message.answer(
        f"🔗 <b>Реферальная система</b>\n\n"
        f"Ваш код: <code>{user['referral_code']}</code>\n"
        f"Ваша ссылка: <code>{link}</code>\n\n"
        f"<b>Как это работает:</b>\n"
        f"• За первого клиента (депозит от порога) вы получаете бонус.\n"
        f"• За каждое последующее пополнение — % комиссии на баланс.\n\n"
        f"Поделитесь ссылкой или кодом с друзьями!",
        reply_markup=_markup(menu_btn()),
    )


# ---------------- Tickets ----------------

@dp.callback_query(MenuCB.filter(F.action == "tickets"))
async def m_tickets(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    tickets = services.list_user_tickets(user["id"])
    lines = ["<b>🎧 Ваши обращения</b>\n"]
    buttons = []
    if tickets:
        for t in tickets:
            st = "🟢" if t["status"] == "open" else "🔒"
            lines.append(f"{st} #{t['id']} {t['subject']} ({t['status']})")
            buttons.append([InlineKeyboardButton(
                text=f"#{t['id']} {t['subject']}", callback_data=TicketCB(id=t["id"]).pack())])
    else:
        lines.append("У вас пока нет обращений.")
    buttons.append([InlineKeyboardButton(text="➕ Создать обращение", callback_data=MenuCB(action="ticket_new").pack())])
    buttons.append(menu_btn())
    await cb.message.answer("\n".join(lines), reply_markup=_markup(*buttons))


@dp.callback_query(MenuCB.filter(F.action == "ticket_new"))
async def ticket_new(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    if not user:
        await _need_account(cb)
        return
    await state.set_state(TicketState.subject)
    await cb.message.answer("✍️ Опишите тему обращения одной строкой:")


@dp.message(F.text, StateFilter(TicketState.subject))
async def ticket_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(TicketState.message)
    await message.answer("📝 Теперь подробно опишите проблему (несколько строк):")


@dp.message(F.text, StateFilter(TicketState.message))
async def ticket_message(message: Message, state: FSMContext):
    user = resolve_user(message.from_user.id)
    data = await state.get_data()
    subject = data.get("subject", "Обращение")
    services.create_ticket(user["id"], subject, message.text.strip())
    await state.clear()
    await message.answer("✅ Обращение создано! Наши специалисты ответят в ближайшее время.",
                         reply_markup=main_menu())


@dp.callback_query(TicketCB.filter())
async def ticket_open(cb: CallbackQuery, state: FSMContext):
    user = resolve_user(cb.from_user.id)
    tid = TicketCB.unpack(cb.data).id
    ticket = services.get_ticket(tid)
    if not ticket or ticket["user_id"] != user["id"]:
        await cb.answer("Обращение не найдено", show_alert=True)
        return
    msgs = services.list_ticket_messages(tid)
    lines = [f"<b>#{tid} {ticket['subject']}</b> ({ticket['status']})\n"]
    for m in msgs:
        who = "Вы" if m["sender_role"] == "user" else "Поддержка"
        lines.append(f"<b>{who}:</b> {m['message']}")
    lines.append("")
    if ticket["status"] == "open":
        await state.update_data(ticket_id=tid)
        await state.set_state(TicketState.chat)
        await cb.message.answer("\n".join(lines), reply_markup=_markup(
            [InlineKeyboardButton(text="Написать сообщение…", callback_data=MenuCB(action="ticket_reply").pack())],
            menu_btn(),
        ))
    else:
        await cb.message.answer("\n".join(lines), reply_markup=_markup(menu_btn()))


@dp.message(F.text, StateFilter(TicketState.chat))
async def ticket_chat(message: Message, state: FSMContext):
    user = resolve_user(message.from_user.id)
    data = await state.get_data()
    tid = data.get("ticket_id")
    if not tid:
        await state.clear()
        return
    services.add_ticket_message(tid, user["id"], "user", message.text.strip())
    await message.answer("Сообщение отправлено в поддержку.")


@dp.callback_query(MenuCB.filter(F.action == "ticket_reply"))
async def ticket_reply_hint(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите текст сообщения (или нажмите /start для выхода из чата):")


# ---------------- Need account / not found ----------------

async def _need_account(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "Нужно <b>зарегистрироваться</b> или войти в существующий ЛК.\n"
        "Если у вас уже есть аккаунт в магазине — укажите в нём в профиле ваш Telegram ID "
        "(кнопка «Войти», раздел «Профиль»).\n\n"
        "Нажмите /start, чтобы создать новый аккаунт прямо в боте.",
        reply_markup=_markup(menu_btn()),
    )


@dp.message(F.text)
async def fallback(message: Message):
    await message.answer("Используйте меню — нажмите /start", reply_markup=main_menu())


# ---------------- Payment watcher ----------------

async def payment_watcher():
    """Notify the Telegram user when one of their paid (non-trial) orders was
    fulfilled by the Platega webhook. Uses balance_transactions kind='bot_notify'
    as a per-order marker so we never double-notify.
    """
    while True:
        try:
            conn = database.get_db()
            try:
                rows = conn.execute(
                    "SELECT id, user_id, plan_name, expires_at FROM orders"
                    " WHERE status='paid' AND is_trial=0 AND paid_at IS NOT NULL"
                    " ORDER BY paid_at DESC LIMIT 50"
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                order = dict(r)
                user = services.get_user_by_id(order["user_id"])
                if not user or not user.get("telegram_id"):
                    continue
                conn = database.get_db()
                try:
                    done = conn.execute(
                        "SELECT id FROM balance_transactions WHERE ref_order_id=? AND kind='bot_notify'",
                        (order["id"],),
                    ).fetchone()
                finally:
                    conn.close()
                if done:
                    continue
                try:
                    msg = f"✅ <b>Оплата получена!</b>\nТариф: {order['plan_name']}\n"
                    if order.get("expires_at"):
                        msg += f"Подписка активна до <b>{order['expires_at'][:10]}</b>."
                    await get_bot().send_message(user["telegram_id"], msg, reply_markup=main_menu())
                except Exception:
                    pass
                conn = database.get_db()
                try:
                    conn.execute(
                        "INSERT INTO balance_transactions (user_id, amount, kind, ref_order_id, note, created_at)"
                        " VALUES (?, 0, 'bot_notify', ?, 'Уведомление об оплате', ?)",
                        (order["user_id"], order["id"], services.now_iso()),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning("watcher error: %s", e)
        await asyncio.sleep(15)


# ---------------- Main ----------------

async def main():
    global _bot
    database.init_db()
    token = database.get_setting("telegram_bot_token", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан. Укажите его в Админ → Настройки → Telegram-бот.")
        return
    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    logger.info("Bot started")
    asyncio.create_task(payment_watcher())
    await dp.start_polling(_bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
