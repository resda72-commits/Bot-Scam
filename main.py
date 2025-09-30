# -*- coding: utf-8 -*-
"""
Telegram bot (aiogram v3.7) — обновлённая сборка под ваши правки.

Правки по задаче:
- Профиль: красивое форматирование, убраны «Баланс» и «Вайтлист»,
  вместо «Реферал» — «Количество рефералов».
- Информация: добавлены поддержка и отзывы — @HarikCVV и @RepHarik.
- Админка: убраны кнопки «Баланс пользователя» и «Выдать тариф»,
  добавлена кнопка «📊 Статистика (PDF)» — бот отдаёт PDF,
  где сначала пользователи с активной подпиской, затем остальные.
- Выдача тарифа остаётся по команде /grant <user_id> <days>.
- Токены и ID оставлены без изменений.

PDF формируется через fpdf2 (если есть) или через reportlab. Если их нет —
бот подскажет, что установить.
"""

import logging
import datetime
import asyncio
import aiohttp
import sqlite3
from typing import Optional, List, Tuple
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery, ContentType, FSInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext

# --- Ваши токены и ID --- (оставлены без изменений)
API_TOKEN = "8456531907:AAGJ9r2YgApKF9NxagtN8Dd_P3QLpHRiz8c"
CRYPTOBOT_TOKEN = "467152:AAkDbtPn11mAXdlxUzoxO22ErVrSBpwWwdM"
ADMIN_IDS = [7048494685]
PAYMENT_GROUP_ID = -1002970919697  # группа/канал для чеков и репортов

# --- Логи ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Инициализация бота/Диспетчера ---
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- База данных ---
DB = "sn0ser_safe.db"

def db_connect():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        registration_date TEXT,
        subscription_end TEXT,
        whitelist_end TEXT,
        last_action_ts INTEGER DEFAULT 0,
        balance REAL DEFAULT 0,
        referrer INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        user_id INTEGER,
        days INTEGER,
        price_rub REAL,
        price_usd REAL,
        paid INTEGER DEFAULT 0,
        type TEXT,
        invoice_id TEXT,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS card_info (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        card_text TEXT
    )""")
    conn.commit()
    # ensure card_info row exists
    c.execute("INSERT OR IGNORE INTO card_info (id, card_text) VALUES (1, 'Карта: нет данных. Обратитесь к админу.')")
    conn.commit()
    conn.close()

db_init()

# --- FSM состояния ---
class Form(StatesGroup):
    admin_broadcast = State()
    admin_set_card = State()

    report_waiting_target = State()
    report_waiting_proof = State()

# --- Вспомогательные функции БД ---
def get_user(user_id: int):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def create_user(user_id: int, name: str, username: Optional[str], ref: Optional[int]=0):
    conn = db_connect()
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, name, username, registration_date, referrer) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, username or "", now, ref or 0)
    )
    conn.commit()
    conn.close()

def update_user_subscription(user_id: int, sub_end: Optional[str]=None, whitelist_end: Optional[str]=None):
    conn = db_connect()
    c = conn.cursor()
    if sub_end:
        c.execute("UPDATE users SET subscription_end = ? WHERE user_id = ?", (sub_end, user_id))
    if whitelist_end is not None:
        c.execute("UPDATE users SET whitelist_end = ? WHERE user_id = ?", (whitelist_end, user_id))
    conn.commit()
    conn.close()

def adjust_balance(user_id: int, amount: float):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def set_user_referrer(user_id: int, referrer_id: int):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE users SET referrer = ? WHERE user_id = ?", (referrer_id, user_id))
    conn.commit()
    conn.close()

def get_card_text():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT card_text FROM card_info WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return row["card_text"] if row else "Карта: нет данных."

def set_card_text(text: str):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE card_info SET card_text = ? WHERE id = 1", (text,))
    conn.commit()
    conn.close()

def get_referrals_count(user_id: int) -> int:
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM users WHERE referrer = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return int(row["cnt"] if row else 0)

# --- Платежи (БД) ---
def create_payment(payment_id: str, user_id: int, days: int, price_rub: float, price_usd: float, pay_type: str, invoice_id: Optional[str]=None):
    conn = db_connect()
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""INSERT INTO payments (payment_id, user_id, days, price_rub, price_usd, paid, type, invoice_id, created_at)
                 VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
              (payment_id, user_id, days, price_rub, price_usd, pay_type, invoice_id, now))
    conn.commit()
    conn.close()

def set_payment_invoice_id(payment_id: str, invoice_id: str):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE payments SET invoice_id = ? WHERE payment_id = ?", (invoice_id, payment_id))
    conn.commit()
    conn.close()

def get_payment(payment_id: str):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))
    row = c.fetchone()
    conn.close()
    return row

def mark_payment_paid(payment_id: str):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE payments SET paid = 1 WHERE payment_id = ?", (payment_id,))
    conn.commit()
    conn.close()

# --- Anti-abuse / тайминги ---
REPORT_COOLDOWN = 30*60  # 30 минут

def can_report(user_id: int) -> (bool, int):
    user = get_user(user_id)
    now = int(datetime.datetime.now().timestamp())
    last = user["last_action_ts"] if user else 0
    if now - last >= REPORT_COOLDOWN:
        return True, 0
    else:
        return False, REPORT_COOLDOWN - (now - last)

def update_last_action_ts(user_id: int):
    conn = db_connect()
    c = conn.cursor()
    now = int(datetime.datetime.now().timestamp())
    c.execute("UPDATE users SET last_action_ts = ? WHERE user_id = ?", (now, user_id))
    conn.commit()
    conn.close()

# --- Меню/кнопки ---
def main_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"), InlineKeyboardButton(text="💳 Купить доступ", callback_data="buy_access")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals"), InlineKeyboardButton(text="💀 Снести Жертву", callback_data="report_user")],
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")]
    ])
    return kb

def admin_menu():
    # Убраны «Баланс пользователя» и «Выдать тариф». Добавлена «Статистика (PDF)».
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast"), InlineKeyboardButton(text="💳 Изменить данные карты", callback_data="admin_set_card")],
        [InlineKeyboardButton(text="📊 Статистика (PDF)", callback_data="admin_stats_pdf")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])
    return kb

def plan_buttons(payment_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить CryptoBot", callback_data=f"pay_crypto_{payment_id}")],
        [InlineKeyboardButton(text="Оплатить картой", callback_data=f"pay_card_{payment_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="buy_access")]
    ])

# --- Хэндлеры ---
@dp.message(F.text.startswith("/start"))
async def cmd_start(message: Message):
    parts = message.text.split()
    ref = 0
    if len(parts) > 1 and parts[1].isdigit():
        ref = int(parts[1])
    create_user(message.from_user.id, message.from_user.first_name, message.from_user.username, ref)
    if ref and ref != message.from_user.id:
        set_user_referrer(message.from_user.id, ref)
    await message.answer(f"👋 Привет, {message.from_user.first_name}!\nДобро пожаловать.", reply_markup=main_menu())

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    sub_end = user["subscription_end"] if user else None
    reg = user["registration_date"] if user else None
    uname = (user["username"] if user else cb.from_user.username) or "-"
    refs = get_referrals_count(cb.from_user.id)

    # Красивое форматирование профиля без баланса/вайтлиста
    def human_sub_status(sub: Optional[str]) -> str:
        if not sub:
            return "Нет"
        try:
            dt = datetime.datetime.strptime(sub, "%Y-%m-%d %H:%M:%S")
            if dt >= datetime.datetime.now():
                days_left = (dt - datetime.datetime.now()).days
                return f"Активна до <b>{dt:%d.%m.%Y %H:%M}</b> (осталось ≈ {max(days_left,0)} дн.)"
            else:
                return f"Истекла <b>{dt:%d.%m.%Y %H:%M}</b>"
        except Exception:
            return sub

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{cb.from_user.id}</code>\n"
        f"👤 Имя: {user['name'] if user else cb.from_user.first_name}\n"
        f"🔗 Юзернейм: @{uname}\n"
        f"📅 Регистрация: {reg or '—'}\n"
        f"💼 Подписка: {human_sub_status(sub_end)}\n"
        f"👥 Количество рефералов: <b>{refs}</b>\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меню", callback_data="menu")]])
    if cb.from_user.id in ADMIN_IDS:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="Админка", callback_data="admin_panel")])
    await safe_edit(cb.message.chat.id, cb.message.message_id, text, kb)
    await cb.answer()

@dp.callback_query(F.data == "referrals")
async def cb_referrals(cb: CallbackQuery):
    try:
        me = await bot.get_me()
        bot_username = me.username or "your_bot"
    except Exception:
        bot_username = "your_bot"
    link = f"https://t.me/{bot_username}?start={cb.from_user.id}"
    refs = get_referrals_count(cb.from_user.id)
    text = (
        "👥 <b>Реферальная программа</b>\n\n"
        f"🔗 Ваша ссылка: {link}\n"
        f"👥 Приведено пользователей: <b>{refs}</b>\n"
        "📈 За каждого оплатившего друга вы получаете 10% от его платежа."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меню", callback_data="menu")]])
    await safe_edit(cb.message.chat.id, cb.message.message_id, text, kb)
    await cb.answer()

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Доступ только для админов", show_alert=True)
        return
    await safe_edit(cb.message.chat.id, cb.message.message_id, "👮‍♂️ Админ-панель", admin_menu())
    await cb.answer()

@dp.callback_query(F.data == "buy_access")
async def cb_buy_access(cb: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 200₽ / 3$", callback_data="plan_1")],
        [InlineKeyboardButton(text="3 дня - 350₽ / 4$", callback_data="plan_3")],
        [InlineKeyboardButton(text="Навсегда - 500₽ / 6$", callback_data="plan_forever")],
        [InlineKeyboardButton(text="Меню", callback_data="menu")]
    ])
    await safe_edit(cb.message.chat.id, cb.message.message_id, "💳 Выберите тариф:", kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def cb_select_plan(cb: CallbackQuery, state: FSMContext):
    key = cb.data[len("plan_"):]
    plans = {
        "1": {"days":1, "price_rub":200, "price_usd":3},
        "3": {"days":3, "price_rub":350, "price_usd":4},
        "forever": {"days":9999, "price_rub":500, "price_usd":6}
    }
    plan = plans.get(key)
    if not plan:
        await cb.answer("Неизвестный тариф")
        return
    payment_id = f"sub_{cb.from_user.id}_{int(datetime.datetime.now().timestamp())}"
    create_payment(payment_id, cb.from_user.id, plan["days"], plan["price_rub"], plan["price_usd"], "subscription")
    await state.update_data(payment_id=payment_id, plan=plan)
    await safe_edit(cb.message.chat.id, cb.message.message_id, f"Вы выбрали тариф на {plan['days']} дней — {plan['price_rub']}₽ / {plan['price_usd']}$", plan_buttons(payment_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("pay_crypto_"))
async def cb_pay_crypto(cb: CallbackQuery, state: FSMContext):
    payment_id = cb.data[len("pay_crypto_"):]
    data = await state.get_data()
    selected_plan = data.get("plan")
    payment = get_payment(payment_id)
    if payment and not selected_plan:
        selected_plan = {"days": payment["days"], "price_rub": payment["price_rub"], "price_usd": payment["price_usd"]}
    if not selected_plan:
        await cb.answer("Ошибка: тариф не найден")
        return
    # создать счёт в CryptoBot
    async with aiohttp.ClientSession() as session:
        headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
        payload = {
            "amount": selected_plan["price_usd"],
            "asset": "USDT",
            "description": f"Оплата подписки {selected_plan['days']} дней",
            "payload": payment_id,
            "expires_in": 600
        }
        try:
            async with session.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload, timeout=15) as resp:
                result = await resp.json()
        except Exception as e:
            logger.error("Crypto createInvoice error: %s", e)
            await cb.answer("Ошибка при создании счёта", show_alert=True)
            return
    if result.get("ok"):
        inv_url = result["result"]["pay_url"]
        inv_id = result["result"]["invoice_id"]
        set_payment_invoice_id(payment_id, inv_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Перейти к оплате", url=inv_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data=f"check_{payment_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data="menu")]
        ])
        await safe_edit(cb.message.chat.id, cb.message.message_id, "Счёт создан. Перейдите по ссылке для оплаты.", kb)
        await cb.answer()
    else:
        await cb.answer("Не удалось создать счёт", show_alert=True)

@dp.callback_query(F.data.startswith("check_"))
async def cb_check_payment(cb: CallbackQuery):
    payment_id = cb.data[len("check_"):]
    payment = get_payment(payment_id)
    if not payment:
        await cb.answer("Платёж не найден")
        return
    inv_id = payment["invoice_id"]
    if not inv_id:
        await cb.answer("Счёт ещё не создан для этого платежа", show_alert=True)
        return
    async with aiohttp.ClientSession() as session:
        headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
        try:
            async with session.get("https://pay.crypt.bot/api/getInvoices", headers=headers, params={"invoice_ids": inv_id}, timeout=15) as resp:
                result = await resp.json()
        except Exception as e:
            logger.error("Crypto getInvoices error: %s", e)
            await cb.answer("Ошибка при проверке оплаты", show_alert=True)
            return
    if result.get("ok") and result["result"].get("items"):
        invoice = result["result"]["items"][0]
        status = invoice.get("status")
        if status == "paid":
            mark_payment_paid(payment_id)
            payment = get_payment(payment_id)
            # referral payout 10%
            try:
                ref_row = get_user(payment['user_id'])
                referrer = ref_row['referrer'] if ref_row else 0
                if referrer and payment['price_rub']:
                    reward = float(payment['price_rub']) * 0.10
                    adjust_balance(referrer, reward)
            except Exception:
                pass
            # activate subscription
            user_id = payment["user_id"]
            days = payment["days"]
            now = datetime.datetime.now()
            end_date = now + datetime.timedelta(days=days) if days < 9999 else now + datetime.timedelta(days=36500)
            update_user_subscription(user_id, end_date.strftime("%Y-%m-%d %H:%M:%S"))
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меню", callback_data="menu")]])
            await safe_edit(cb.message.chat.id, cb.message.message_id, "✅ Платёж подтверждён. Подписка активирована.", kb)
            await cb.answer()
            return
        elif status == "active":
            await cb.answer("Счёт не оплачен", show_alert=True); return
        else:
            await cb.answer("Счёт не активен или истёк", show_alert=True); return
    else:
        await cb.answer("Платёж не найден или не оплачен", show_alert=True)

@dp.callback_query(F.data.startswith("pay_card_"))
async def cb_pay_card(cb: CallbackQuery, state: FSMContext):
    payment_id = cb.data[len("pay_card_"):]
    payment = get_payment(payment_id)
    if not payment:
        await cb.answer("Платёж не найден")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меню", callback_data="menu")]])
    await safe_edit(
        cb.message.chat.id,
        cb.message.message_id,
        f"{get_card_text()}\n\n📎 После оплаты отправьте чек оплаты @HarikCVV",
        kb
    )
    await cb.answer()

# --- Админка: рассылка и изменение карты ---
@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Доступ только для админов", show_alert=True)
        return
    await state.set_state(Form.admin_broadcast)
    await safe_edit(cb.message.chat.id, cb.message.message_id, "Отправьте текст для рассылки.")
    await cb.answer()

@dp.message(Form.admin_broadcast, F.text)
async def msg_admin_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ только для админов")
        await state.clear()
        return
    text = message.text
    conn = db_connect(); c = conn.cursor(); c.execute("SELECT user_id FROM users"); rows = c.fetchall(); conn.close()
    sent = 0
    for row in rows:
        try:
            await bot.send_message(row["user_id"], text)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception:
            continue
    await message.answer(f"Рассылка отправлена ({sent}).")
    await state.clear()

@dp.callback_query(F.data == "admin_set_card")
async def cb_admin_set_card(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Доступ только для админов", show_alert=True)
        return
    await state.set_state(Form.admin_set_card)
    await safe_edit(cb.message.chat.id, cb.message.message_id, "Отправьте новые реквизиты карты (текст).")
    await cb.answer()

@dp.message(Form.admin_set_card, F.text)
async def msg_admin_set_card(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ только для админов")
        await state.clear()
        return
    set_card_text(message.text.strip())
    await message.answer("Данные карты обновлены.")
    await state.clear()

# --- Статистика (PDF) ---
def _fetch_users_for_stats() -> Tuple[List[sqlite3.Row], List[sqlite3.Row]]:
    """Возвращает (users_with_active_sub, other_users)"""
    conn = db_connect(); c = conn.cursor()
    # Активная подписка: дата >= сейчас
    c.execute(
        """
        SELECT * FROM users
        WHERE subscription_end IS NOT NULL AND subscription_end <> '' AND subscription_end >= datetime('now')
        ORDER BY datetime(subscription_end) DESC
        """
    )
    with_sub = c.fetchall()

    c.execute(
        """
        SELECT * FROM users
        WHERE (subscription_end IS NULL OR subscription_end = '' OR subscription_end < datetime('now'))
        ORDER BY datetime(registration_date) DESC
        """
    )
    others = c.fetchall()
    conn.close()
    return with_sub, others

def _build_stats_lines(group_title: str, rows: List[sqlite3.Row]) -> List[str]:
    lines = [group_title]
    if not rows:
        lines.append("  — нет данных —")
        return lines
    for i, r in enumerate(rows, start=1):
        uid = r["user_id"]
        nm = r["name"] or "-"
        un = (r["username"] or "-")
        reg = r["registration_date"] or "-"
        sub = r["subscription_end"] or "-"
        lines.append(f"{i}. ID:{uid} | {nm} (@{un}) | Рег: {reg} | Подписка до: {sub}")
    return lines

def generate_stats_pdf(filename: str) -> bool:
    """Создаёт PDF. Возвращает True/False, удалось ли построить."""
    with_sub, others = _fetch_users_for_stats()
    header = [
        "Статистика бота",
        f"Сгенерировано: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
    ]
    part1 = _build_stats_lines("[Пользователи с активной подпиской]", with_sub)
    part2 = _build_stats_lines("[Остальные пользователи]", others)
    all_lines = header + part1 + [""] + part2

    # 1) Попытка через fpdf (fpdf2)
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_font("Arial", size=12)
        for line in all_lines:
            pdf.multi_cell(0, 8, txt=line)
        pdf.output(filename)
        return True
    except Exception as e:
        logger.warning("FPDF недоступен или ошибка генерации: %s", e)

    # 2) Попытка через reportlab
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm

        c = canvas.Canvas(filename, pagesize=A4)
        width, height = A4
        x_margin, y_margin = 15 * mm, 15 * mm
        y = height - y_margin
        c.setFont("Helvetica", 12)
        for line in all_lines:
            if y < y_margin:
                c.showPage(); c.setFont("Helvetica", 12); y = height - y_margin
            c.drawString(x_margin, y, line)
            y -= 14
        c.save()
        return True
    except Exception as e:
        logger.error("ReportLab недоступен или ошибка генерации: %s", e)
        return False

@dp.callback_query(F.data == "admin_stats_pdf")
async def cb_admin_stats_pdf(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Доступ только для админов", show_alert=True)
        return
    fname = f"bot_stats_{int(datetime.datetime.now().timestamp())}.pdf"
    ok = generate_stats_pdf(fname)
    if ok and os.path.exists(fname):
        try:
            await bot.send_document(cb.message.chat.id, FSInputFile(fname), caption="Статистика бота (PDF)")
        finally:
            try:
                os.remove(fname)
            except Exception:
                pass
    else:
        await cb.message.answer(
            "Не удалось сформировать PDF. Установите одну из библиотек на сервере: \n"
            "• pip install fpdf2\n• pip install reportlab"
        )
    await cb.answer()

# --- Report («Снести Жертву») ---
@dp.callback_query(F.data == "report_user")
async def cb_report_user(cb: CallbackQuery, state: FSMContext):
    user = get_user(cb.from_user.id)
    if not user or not user["subscription_end"]:
        await cb.answer("Купите тариф, чтобы использовать функцию.", show_alert=True)
        return
    try:
        if datetime.datetime.strptime(user["subscription_end"], "%Y-%m-%d %H:%M:%S") < datetime.datetime.now():
            await cb.answer("Срок подписки истёк. Купите новый тариф.", show_alert=True)
            return
    except Exception:
        pass
    ok, wait = can_report(cb.from_user.id)
    if not ok:
        await cb.answer(f"Подождите {int(wait/60)} минут перед следующим использованием.", show_alert=True)
        return
    await state.set_state(Form.report_waiting_target)
    await safe_edit(cb.message.chat.id, cb.message.message_id, "Введите юзернейм или ID «жертвы».")
    await cb.answer()

@dp.message(Form.report_waiting_target, F.text)
async def msg_report_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    await state.set_state(Form.report_waiting_proof)
    await message.answer("Прикрепите доказательства (фото/файл) или опишите ситуацию. Напишите «нет», если доказательств нет.")

@dp.message(Form.report_waiting_proof, F.content_type.in_({ContentType.DOCUMENT, ContentType.PHOTO, ContentType.TEXT}))
async def msg_report_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    caption = (f"Новый репорт\nОт: @{message.from_user.username or '-'} (ID: {message.from_user.id})\n"
               f"Цель: {target}\n"
               f"Дата: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    try:
        if message.content_type == ContentType.TEXT:
            text = caption + "\nОписание:\n" + message.text
            await bot.send_message(PAYMENT_GROUP_ID, text)
        elif message.content_type == ContentType.PHOTO:
            await bot.send_photo(PAYMENT_GROUP_ID, message.photo[-1].file_id, caption=caption)
        elif message.content_type == ContentType.DOCUMENT:
            await bot.send_document(PAYMENT_GROUP_ID, message.document.file_id, caption=caption)
    except Exception as e:
        logger.error("Failed to forward report: %s", e)
    update_last_action_ts(message.from_user.id)
    await message.answer("Репорт отправлен админам. Ожидайте ответа.", reply_markup=main_menu())
    # Прогресс-бар (визуальный)
    progress_msg = await message.answer("Начинается операция...\n[▒▒▒▒▒▒▒▒▒▒] 0%")
    steps = 10
    for i in range(1, steps + 1):
        bar = "█" * i + "▒" * (steps - i)
        percent = int(i / steps * 100)
        try:
            await bot.edit_message_text(chat_id=progress_msg.chat.id, message_id=progress_msg.message_id, text=f"[{bar}] {percent}%")
        except Exception:
            pass
        await asyncio.sleep(0.5)
    await bot.edit_message_text(chat_id=progress_msg.chat.id, message_id=progress_msg.message_id, text="✅ Операция завершена, ожидайте обратной связи от админов.")
    await state.clear()

# --- Информация ---
@dp.callback_query(F.data == "info")
async def cb_info(cb: CallbackQuery):
    info_text = (
        "ℹ️ <b>Информация</b>\n\n"
        "Поддержка — @HarikCVV\n"
        "Отзывы — @RepHarik"
    )
    await safe_edit(cb.message.chat.id, cb.message.message_id, info_text, main_menu())
    await cb.answer()

# --- Меню и прочее ---
@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await safe_edit(cb.message.chat.id, cb.message.message_id, "Главное меню:", main_menu())
    await cb.answer()

# --- Безопасная правка сообщения ---
async def safe_edit(chat_id: int, message_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup]=None):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        msg = str(e).lower()
        if "message is not modified" in msg or "message not modified" in msg:
            logger.debug("Message not modified (ignored).")
            return
        logger.warning("safe_edit error: %s", e)

# --- Выдача тарифа — только по команде ---
@dp.message(F.text.startswith("/grant"))
async def cmd_grant_simple(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("Доступ только для админов.")
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.reply("Использование: /grant <user_id> <days>")
        return
    try:
        uid = int(parts[1]); days = int(parts[2])
    except Exception:
        await message.reply("Неверные данные.")
        return
    now = datetime.datetime.now()
    end = now + datetime.timedelta(days=days) if days < 9999 else now + datetime.timedelta(days=36500)
    update_user_subscription(uid, end.strftime("%Y-%m-%d %H:%M:%S"))
    await message.reply(f"✅ Пользователю {uid} выдан тариф на {days} дн. До: {end.strftime('%Y-%m-%d %H:%M:%S')}")

# --- Запуск ---
async def main():
    logger.info("Bot started (updated)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
