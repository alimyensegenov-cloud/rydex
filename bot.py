import asyncio
import math
import uuid
import json
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import *
from aiogram.filters import Command
from dotenv import load_dotenv
import asyncpg

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
USDT_ADDRESS = "TJnJXC7arrvDVDSqybHcs8MkkdwUncZ1Tp"
DATABASE_URL = os.getenv("DATABASE_URL")
MIN_PRICE = 3.5

PROMO_CODES = {
    "RYDEX10": {"discount": 1.0},
    "WELCOME": {"discount": 1.5},
}

MAX_CANCELS_IN_WINDOW = 5
CANCEL_WINDOW_MINUTES = 15
CANCEL_BAN_HOURS = 2
FLOOD_INTERVAL_SECONDS = 3
DRIVER_DELAY_NOTIFY_MINUTES = 10

last_message_time = {}
cancel_times = {}
temp_bans = {}

db_pool: asyncpg.Pool = None

# ===== DB INIT =====
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id BIGINT PRIMARY KEY,
                lang TEXT DEFAULT 'en',
                last_pickup_lat DOUBLE PRECISION,
                last_pickup_lon DOUBLE PRECISION,
                last_dest_lat DOUBLE PRECISION,
                last_dest_lon DOUBLE PRECISION,
                used_promos TEXT[] DEFAULT '{}',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS client_trips (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                price DOUBLE PRECISION,
                date TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                car TEXT,
                color TEXT,
                plate TEXT,
                photo_id TEXT,
                passport_id TEXT,
                license_id TEXT,
                approved BOOLEAN DEFAULT FALSE,
                online BOOLEAN DEFAULT FALSE,
                rating DOUBLE PRECISION DEFAULT 5.0,
                rating_count INT DEFAULT 0,
                rating_sum DOUBLE PRECISION DEFAULT 0,
                total_earned DOUBLE PRECISION DEFAULT 0,
                lang TEXT DEFAULT 'en',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS driver_trips (
                id SERIAL PRIMARY KEY,
                driver_id BIGINT,
                price DOUBLE PRECISION,
                date TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id BIGINT PRIMARY KEY,
                date TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                name TEXT,
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                UNIQUE(user_id, name)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                date TEXT PRIMARY KEY,
                completed INT DEFAULT 0,
                cancelled INT DEFAULT 0,
                revenue DOUBLE PRECISION DEFAULT 0,
                cancel_late INT DEFAULT 0,
                cancel_changed INT DEFAULT 0,
                cancel_other INT DEFAULT 0
            )
        """)
    # Добавляем новые колонки если их нет (миграция)
    async with db_pool.acquire() as conn:
        await conn.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS passport_id TEXT")
        await conn.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS license_id TEXT")
    print("✅ Database initialized")

# ===== DB HELPERS =====
async def db_get_client(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM clients WHERE user_id=$1", user_id)

async def db_save_client_lang(user_id: int, lang: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, lang) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET lang=$2
        """, user_id, lang)

async def db_save_last_order(user_id: int, pickup: tuple, dest: tuple):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, last_pickup_lat, last_pickup_lon, last_dest_lat, last_dest_lon)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                last_pickup_lat=$2, last_pickup_lon=$3,
                last_dest_lat=$4, last_dest_lon=$5
        """, user_id, pickup[0], pickup[1], dest[0], dest[1])

async def db_get_last_order(user_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_pickup_lat, last_pickup_lon, last_dest_lat, last_dest_lon FROM clients WHERE user_id=$1", user_id)
        if row and row["last_pickup_lat"]:
            return (row["last_pickup_lat"], row["last_pickup_lon"]), (row["last_dest_lat"], row["last_dest_lon"])
        return None, None

async def db_is_promo_used(user_id: int, code: str) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT used_promos FROM clients WHERE user_id=$1", user_id)
        if row:
            return code in (row["used_promos"] or [])
        return False

async def db_mark_promo_used(user_id: int, code: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, used_promos) VALUES ($1, ARRAY[$2])
            ON CONFLICT (user_id) DO UPDATE SET used_promos = array_append(clients.used_promos, $2)
        """, user_id, code)

async def db_save_client_trip(user_id: int, price: float, date: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO client_trips (user_id, price, date) VALUES ($1, $2, $3)",
            user_id, price, date)

async def db_get_client_trips(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT price, date FROM client_trips WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user_id)
        return [dict(r) for r in rows]

async def db_get_all_client_ids():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM clients WHERE lang IS NOT NULL")
        return [r["user_id"] for r in rows]

async def db_is_banned(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM banned WHERE user_id=$1", user_id)
        return row is not None

async def db_ban(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO banned (user_id, date) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id, datetime.now().strftime("%d.%m.%Y"))

async def db_unban(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM banned WHERE user_id=$1", user_id)

async def db_get_banned_list():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, date FROM banned")

async def db_get_favorites(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, lat, lon FROM favorites WHERE user_id=$1", user_id)
        return {r["name"]: {"lat": r["lat"], "lon": r["lon"]} for r in rows}

async def db_save_favorite(user_id: int, name: str, lat: float, lon: float):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO favorites (user_id, name, lat, lon) VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, name) DO UPDATE SET lat=$3, lon=$4
        """, user_id, name, lat, lon)

async def db_get_driver(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", user_id)

async def db_save_driver(user_id: int, data: dict, lang: str = "en"):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO drivers (user_id, name, car, color, plate, photo_id, lang, approved, online)
            VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE, FALSE)
            ON CONFLICT (user_id) DO UPDATE SET
                name=$2, car=$3, color=$4, plate=$5, photo_id=$6, lang=$7
        """, user_id, data.get("name"), data.get("car"), data.get("color"),
            data.get("plate"), data.get("photo"), lang)

async def db_approve_driver(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET approved=TRUE WHERE user_id=$1", user_id)

async def db_set_driver_online(user_id: int, online: bool):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET online=$2 WHERE user_id=$1", user_id, online)

async def db_get_online_drivers():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM drivers WHERE approved=TRUE AND online=TRUE")
        return [dict(r) for r in rows]

async def db_get_approved_driver_ids():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM drivers WHERE approved=TRUE")
        return {r["user_id"] for r in rows}

async def db_get_all_drivers():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM drivers WHERE approved=TRUE")
        return [dict(r) for r in rows]

async def db_update_driver_rating(driver_id: int, stars: int, price: float, date: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE drivers SET
                rating_count = rating_count + 1,
                rating_sum = rating_sum + $2,
                rating = ROUND(CAST((rating_sum + $2) / (rating_count + 1) AS NUMERIC), 1),
                total_earned = total_earned + $3
            WHERE user_id=$1
        """, driver_id, float(stars), price)
        await conn.execute(
            "INSERT INTO driver_trips (driver_id, price, date) VALUES ($1, $2, $3)",
            driver_id, price, date)

async def db_add_driver_trip(driver_id: int, price: float, date: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET total_earned=total_earned+$2 WHERE user_id=$1", driver_id, price)
        await conn.execute(
            "INSERT INTO driver_trips (driver_id, price, date) VALUES ($1, $2, $3)",
            driver_id, price, date)

async def db_get_driver_trips(driver_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT price, date FROM driver_trips WHERE driver_id=$1 ORDER BY created_at DESC LIMIT 5",
            driver_id)

async def db_set_driver_lang(driver_id: int, lang: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET lang=$2 WHERE user_id=$1", driver_id, lang)

async def db_record_completed(price: float):
    today = datetime.now().strftime("%d.%m.%Y")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO stats (date, completed, revenue) VALUES ($1, 1, $2)
            ON CONFLICT (date) DO UPDATE SET
                completed = stats.completed + 1,
                revenue = stats.revenue + $2
        """, today, price)

async def db_record_cancelled(reason: str = "other"):
    today = datetime.now().strftime("%d.%m.%Y")
    col = {"late": "cancel_late", "changed": "cancel_changed"}.get(reason, "cancel_other")
    async with db_pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO stats (date, cancelled, {col}) VALUES ($1, 1, 1)
            ON CONFLICT (date) DO UPDATE SET
                cancelled = stats.cancelled + 1,
                {col} = stats.{col} + 1
        """, today)

async def db_get_stats_today():
    today = datetime.now().strftime("%d.%m.%Y")
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM stats WHERE date=$1", today)

async def db_get_stats_all():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT SUM(completed) as total_completed, SUM(revenue) as total_revenue FROM stats")

# ===== ЗАЩИТА =====
def is_temp_banned(user_id):
    if user_id in temp_bans:
        if datetime.now() < temp_bans[user_id]:
            return True
        del temp_bans[user_id]
    return False

def get_temp_ban_remaining(user_id):
    if user_id in temp_bans:
        return max(1, int((temp_bans[user_id] - datetime.now()).total_seconds() / 60))
    return 0

def record_cancel(user_id):
    now = datetime.now()
    window_start = now - timedelta(minutes=CANCEL_WINDOW_MINUTES)
    cancel_times.setdefault(user_id, [])
    cancel_times[user_id] = [t for t in cancel_times[user_id] if t > window_start]
    cancel_times[user_id].append(now)
    if len(cancel_times[user_id]) >= MAX_CANCELS_IN_WINDOW:
        temp_bans[user_id] = now + timedelta(hours=CANCEL_BAN_HOURS)
        cancel_times[user_id] = []
        return True
    return False

def get_cancel_count_in_window(user_id):
    now = datetime.now()
    window_start = now - timedelta(minutes=CANCEL_WINDOW_MINUTES)
    return len([t for t in cancel_times.get(user_id, []) if t > window_start])

def is_flood(user_id):
    now = datetime.now()
    if user_id in last_message_time:
        if (now - last_message_time[user_id]).total_seconds() < FLOOD_INTERVAL_SECONDS:
            return True
    last_message_time[user_id] = now
    return False

bot = Bot(token=TOKEN)
dp = Dispatcher()

TEXTS = {
    "en": {
        "welcome": "👋 Welcome to <b>Rydex</b> — your taxi in Northern Cyprus.\n\nChoose your language to get started:",
        "find_driver": "🚕 Order a Ride",
        "repeat_order": "🔁 Repeat Last Ride",
        "continue": "📍 Ready to order a ride?",
        "yes": "✅ Yes",
        "no": "❌ No",
        "send_location": "📍 Share Location",
        "cancel": "✖ Cancel Ride",
        "send_pickup": "📍 Where should we pick you up?\n\nShare your location or choose a saved place below.",
        "send_destination": "🏁 Where are you going?\n\nShare your location or choose a saved place below.",
        "not_in_zone": "😔 Sorry, we don't operate in this area yet.\n\nWe're available in Northern Cyprus.",
        "same_location": "⚠️ Pickup and destination can't be the same place.",
        "searching": "🔍 Looking for a driver nearby...\n\nThis usually takes under a minute.",
        "searching_wait": "⏳ Still searching for a driver. Please wait...",
        "order_cancelled": "✖ Ride cancelled.",
        "client_cancelled": "✖ Client cancelled the ride.",
        "no_drivers": "😔 No drivers available right now.\n\nPlease try again in a few minutes.",
        "active_order": "⏳ You already have an active ride in progress.",
        "cancelled": "Cancelled.",
        "thanks_feedback": "🙏 Thank you for your feedback!",
        "order_again": "✅ Your ride is complete. You can order again anytime!",
        "driver_found": (
            "🎉 <b>Driver found!</b>\n\n"
            "👤 <b>{name}</b>\n"
            "⭐ {rating} · 📱 @{username}\n"
            "🚘 {car} · {color} · 🔢 {plate}\n\n"
            "🛣 Driver is on the way\n"
            "⏱ ETA: <b>~{eta} min</b>"
        ),
        "driver_arrived": "📍 <b>Your driver has arrived!</b>\n\nPlease head to the pickup point.",
        "trip_started": "🚗 <b>Your ride has started.</b>\n\nSit back and enjoy the trip!",
        "trip_finished": (
            "✅ <b>Ride complete!</b>\n\n"
            "💵 <b>${price:.2f} USD</b>\n💶 €{price_eur}\n💴 ₺{price_try}\n"
            "⏱ Duration: {minutes}\n\n"
            "👤 Driver: <b>{name}</b>\n"
            "📱 @{username}\n\n"
            "How was your ride?"
        ),
        "thanks_rating": "⭐ Thanks for rating! See you next time.",
        "thanks_skip": "👍 Thanks! See you next time.",
        "trip_info": (
            "🚕 <b>Ride Summary</b>\n\n"
            "📏 Distance: <b>{distance:.2f} km</b>\n"
            "⏱ Est. time: <b>~{time} min</b>\n\n"
            "💵 <b>${usd:.2f} USD</b>\n"
            "💶 €{eur}\n💴 ₺{try_}\n\n"
            "Confirm your ride?"
        ),
        "trip_info_promo": (
            "🚕 <b>Ride Summary</b>\n\n"
            "📏 Distance: <b>{distance:.2f} km</b>\n"
            "⏱ Est. time: <b>~{time} min</b>\n\n"
            "~~${original:.2f}~~ → <b>${usd:.2f} USD</b> 🎉\n"
            "💶 €{eur}\n💴 ₺{try_}\n"
            "🎟 Promo: <b>{promo}</b> (−${discount:.2f})\n\n"
            "Confirm your ride?"
        ),
        "order_taken": "✖ Sorry, this ride was already taken by another driver.",
        "language_changed": "🇬🇧 Language set to <b>English</b>",
        "choose_language": "🌍 Choose your language:",
        "cancel_reason": "Why are you cancelling?",
        "reason_late": "🕐 Driver is taking too long",
        "reason_changed": "🔄 Changed my mind",
        "reason_other": "✏️ Other reason",
        "cancel_notify": "✖ Client cancelled the ride.\n📝 Reason: {reason}",
        "stop_live": "🔴 <b>Trip finished!</b>\n\nPlease stop sharing your Live Location.",
        "duration_unknown": "—",
        "pay_crypto": (
            "💎 <b>Pay with USDT (TRC20)</b>\n\n"
            "💰 Amount: <b>{amount:.2f} USDT</b>\n\n"
            "📋 Send to this address:\n"
            "<code>{address}</code>\n\n"
            "After sending, tap the button below."
        ),
        "pay_cash": "💵 Pay with Cash",
        "i_paid": "✅ I've Sent the Payment",
        "choose_payment": "💳 <b>How would you like to pay?</b>",
        "payment_pending": "⏳ Payment sent to admin for verification.\n\nPlease wait a moment...",
        "payment_confirmed": "✅ <b>Payment confirmed!</b>\n\nLooking for your driver...",
        "payment_rejected": "❌ Payment could not be verified.\n\nPlease try again or pay with cash.",
        "admin_payment": "💰 <b>Payment to verify</b>\n\n👤 @{username}\n💵 Amount: ${amount:.2f} USDT\n\nConfirm this payment?",
        "banned": "🚫 Your account has been suspended.\n\nContact support if you believe this is a mistake.",
        "temp_banned": "⏳ You've cancelled too many rides recently.\n\n🚫 Ordering is paused for <b>{minutes} min</b>.",
        "cancel_warning": "⚠️ You've cancelled <b>{count}/{max}</b> rides in the last {window} min.\n\nOne more cancellation will pause your account for {hours} hours.",
        "save_favorite": "⭐ Save this place",
        "favorite_name": "📝 Enter a name for this place:\n\n<i>Examples: Home, Work, Gym</i>",
        "favorite_saved": "⭐ Saved as <b>'{name}'</b>!",
        "use_favorite": "⭐ <b>Your saved places:</b>",
        "low_rating_prompt": "✏️ What went wrong? Your feedback helps us improve.\n\n<i>Send a message or type /skip</i>",
        "enter_promo": "🎟 Enter promo code (or tap Skip):",
        "promo_skip": "⏭ Skip",
        "promo_valid": "🎉 Promo code <b>{code}</b> applied!\nDiscount: <b>−${discount:.2f}</b>",
        "promo_invalid": "❌ Invalid promo code. Try again or skip.",
        "promo_used": "❌ This promo code has already been used.",
        "driver_delay": "🕐 Your driver is on the way but taking a bit longer than expected.\n\nThank you for your patience!",
        "history_empty": "📋 You don't have any completed rides yet.",
        "history_header": "📋 <b>Your recent rides:</b>\n",
        "history_item": "🗓 {date} — <b>${price:.2f}</b>",
        "broadcast_usage": "Usage: /broadcast Your message here",
        "broadcast_sent": "✅ Broadcast sent to <b>{count}</b> clients.",
        "help": (
            "🚖 <b>Rydex — How it works</b>\n\n"
            "1️⃣ Press <b>Order a Ride</b>\n"
            "2️⃣ Share your <b>pickup location</b>\n"
            "3️⃣ Share your <b>destination</b>\n"
            "4️⃣ Confirm the price\n"
            "5️⃣ Choose payment: <b>Cash</b> or <b>USDT</b>\n"
            "6️⃣ Wait for a driver — usually under 2 min!\n\n"
            "💰 <b>Pricing</b>\n"
            "Base fare: $3.50\n"
            "Per km: $0.45\n\n"
            "💳 <b>Payment</b>\n"
            "• Cash — pay the driver directly\n"
            "• USDT TRC20 — crypto payment\n\n"
            "⭐ <b>Saved places</b>\n"
            "After each ride you can save the destination for quick ordering.\n\n"
            "📋 Use /history to see your past rides.\n\n"
            "🆘 <b>Support</b>\n"
            "Contact: @rydex_support\n\n"
            "Use /language to change language."
        ),
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в <b>Rydex</b> — ваше такси на Северном Кипре.\n\nВыберите язык:",
        "find_driver": "🚕 Заказать поездку",
        "repeat_order": "🔁 Повторить последний маршрут",
        "continue": "📍 Готовы заказать поездку?",
        "yes": "✅ Да",
        "no": "❌ Нет",
        "send_location": "📍 Отправить геолокацию",
        "cancel": "✖ Отменить поездку",
        "send_pickup": "📍 Откуда вас забрать?\n\nОтправьте геолокацию или выберите сохранённое место.",
        "send_destination": "🏁 Куда едем?\n\nОтправьте геолокацию или выберите сохранённое место.",
        "not_in_zone": "😔 К сожалению, мы пока не работаем в этом районе.\n\nМы доступны на Северном Кипре.",
        "same_location": "⚠️ Место посадки и назначения не могут совпадать.",
        "searching": "🔍 Ищем водителя поблизости...\n\nОбычно это занимает меньше минуты.",
        "searching_wait": "⏳ Поиск продолжается. Пожалуйста, подождите...",
        "order_cancelled": "✖ Поездка отменена.",
        "client_cancelled": "✖ Клиент отменил поездку.",
        "no_drivers": "😔 Сейчас нет доступных водителей.\n\nПопробуйте через несколько минут.",
        "active_order": "⏳ У вас уже есть активная поездка.",
        "cancelled": "Отменено.",
        "thanks_feedback": "🙏 Спасибо за отзыв!",
        "order_again": "✅ Поездка завершена. Приятно было везти вас!",
        "driver_found": (
            "🎉 <b>Водитель найден!</b>\n\n"
            "👤 <b>{name}</b>\n"
            "⭐ {rating} · 📱 @{username}\n"
            "🚘 {car} · {color} · 🔢 {plate}\n\n"
            "🛣 Водитель едет к вам\n"
            "⏱ Примерно: <b>~{eta} мин</b>"
        ),
        "driver_arrived": "📍 <b>Водитель прибыл!</b>\n\nВыходите к месту посадки.",
        "trip_started": "🚗 <b>Поездка началась.</b>\n\nПриятной дороги!",
        "trip_finished": (
            "✅ <b>Поездка завершена!</b>\n\n"
            "💵 <b>${price:.2f} USD</b>\n💶 €{price_eur}\n💴 ₺{price_try}\n"
            "⏱ Время в пути: {minutes}\n\n"
            "👤 Водитель: <b>{name}</b>\n"
            "📱 @{username}\n\n"
            "Как прошла поездка?"
        ),
        "thanks_rating": "⭐ Спасибо за оценку! До встречи!",
        "thanks_skip": "👍 Спасибо! До встречи!",
        "trip_info": (
            "🚕 <b>Детали поездки</b>\n\n"
            "📏 Расстояние: <b>{distance:.2f} км</b>\n"
            "⏱ Время: <b>~{time} мин</b>\n\n"
            "💵 <b>${usd:.2f} USD</b>\n"
            "💶 €{eur}\n💴 ₺{try_}\n\n"
            "Подтвердить поездку?"
        ),
        "trip_info_promo": (
            "🚕 <b>Детали поездки</b>\n\n"
            "📏 Расстояние: <b>{distance:.2f} км</b>\n"
            "⏱ Время: <b>~{time} мин</b>\n\n"
            "~~${original:.2f}~~ → <b>${usd:.2f} USD</b> 🎉\n"
            "💶 €{eur}\n💴 ₺{try_}\n"
            "🎟 Промокод: <b>{promo}</b> (−${discount:.2f})\n\n"
            "Подтвердить поездку?"
        ),
        "order_taken": "✖ Этот заказ уже принят другим водителем.",
        "language_changed": "🇷🇺 Язык изменён на <b>Русский</b>",
        "choose_language": "🌍 Выберите язык:",
        "cancel_reason": "Почему вы отменяете?",
        "reason_late": "🕐 Водитель долго едет",
        "reason_changed": "🔄 Передумал",
        "reason_other": "✏️ Другая причина",
        "cancel_notify": "✖ Клиент отменил поездку.\n📝 Причина: {reason}",
        "stop_live": "🔴 <b>Поездка завершена!</b>\n\nПожалуйста, остановите трансляцию геолокации.",
        "duration_unknown": "—",
        "pay_crypto": (
            "💎 <b>Оплата USDT (TRC20)</b>\n\n"
            "💰 Сумма: <b>{amount:.2f} USDT</b>\n\n"
            "📋 Отправьте на адрес:\n"
            "<code>{address}</code>\n\n"
            "После отправки нажмите кнопку ниже."
        ),
        "pay_cash": "💵 Оплата наличными",
        "i_paid": "✅ Я отправил оплату",
        "choose_payment": "💳 <b>Как хотите оплатить?</b>",
        "payment_pending": "⏳ Запрос на проверку оплаты отправлен.\n\nПодождите немного...",
        "payment_confirmed": "✅ <b>Оплата подтверждена!</b>\n\nИщем водителя...",
        "payment_rejected": "❌ Оплата не подтверждена.\n\nПопробуйте снова или выберите наличные.",
        "admin_payment": "💰 <b>Проверка оплаты</b>\n\n👤 @{username}\n💵 Сумма: ${amount:.2f} USDT\n\nПодтвердить?",
        "banned": "🚫 Ваш аккаунт заблокирован.\n\nСвяжитесь с поддержкой, если считаете это ошибкой.",
        "temp_banned": "⏳ Вы слишком часто отменяли поездки.\n\n🚫 Заказы приостановлены на <b>{minutes} мин</b>.",
        "cancel_warning": "⚠️ Вы отменили <b>{count}/{max}</b> поездок за {window} мин.\n\nЕщё одна отмена приостановит ваш аккаунт на {hours} часа.",
        "save_favorite": "⭐ Сохранить это место",
        "favorite_name": "📝 Введите название места:\n\n<i>Например: Дом, Работа, Спортзал</i>",
        "favorite_saved": "⭐ Сохранено как <b>'{name}'</b>!",
        "use_favorite": "⭐ <b>Сохранённые места:</b>",
        "low_rating_prompt": "✏️ Что пошло не так? Ваш отзыв поможет нам стать лучше.\n\n<i>Напишите сообщение или /skip</i>",
        "enter_promo": "🎟 Введите промокод (или нажмите Пропустить):",
        "promo_skip": "⏭ Пропустить",
        "promo_valid": "🎉 Промокод <b>{code}</b> применён!\nСкидка: <b>−${discount:.2f}</b>",
        "promo_invalid": "❌ Неверный промокод. Попробуйте ещё раз или пропустите.",
        "promo_used": "❌ Этот промокод уже использован.",
        "driver_delay": "🕐 Водитель едет к вам, но задерживается чуть дольше обычного.\n\nСпасибо за ожидание!",
        "history_empty": "📋 У вас пока нет завершённых поездок.",
        "history_header": "📋 <b>Ваши последние поездки:</b>\n",
        "history_item": "🗓 {date} — <b>${price:.2f}</b>",
        "broadcast_usage": "Использование: /broadcast Ваше сообщение",
        "broadcast_sent": "✅ Рассылка отправлена <b>{count}</b> клиентам.",
        "help": (
            "🚖 <b>Rydex — Как это работает</b>\n\n"
            "1️⃣ Нажмите <b>Заказать поездку</b>\n"
            "2️⃣ Отправьте <b>место посадки</b>\n"
            "3️⃣ Отправьте <b>место назначения</b>\n"
            "4️⃣ Подтвердите цену\n"
            "5️⃣ Выберите оплату: <b>Наличные</b> или <b>USDT</b>\n"
            "6️⃣ Ждите водителя — обычно до 2 минут!\n\n"
            "💰 <b>Тарифы</b>\n"
            "Базовая стоимость: $3.50\n"
            "За километр: $0.45\n\n"
            "💳 <b>Оплата</b>\n"
            "• Наличными — оплата водителю\n"
            "• USDT TRC20 — криптоплатёж\n\n"
            "⭐ <b>Сохранённые места</b>\n"
            "После каждой поездки можно сохранить место для быстрого заказа.\n\n"
            "📋 Используйте /history для просмотра истории поездок.\n\n"
            "🆘 <b>Поддержка</b>\n"
            "Контакт: @rydex_support\n\n"
            "Используйте /language для смены языка."
        ),
    },
    "tr": {
        "welcome": "👋 <b>Rydex</b>'e hoş geldiniz — Kuzey Kıbrıs'ın taksi hizmeti.\n\nDilini seç:",
        "find_driver": "🚕 Yolculuk Sipariş Et",
        "repeat_order": "🔁 Son Rotayı Tekrarla",
        "continue": "📍 Yolculuk sipariş etmeye hazır mısınız?",
        "yes": "✅ Evet",
        "no": "❌ Hayır",
        "send_location": "📍 Konum Gönder",
        "cancel": "✖ Yolculuğu İptal Et",
        "send_pickup": "📍 Sizi nereden alalım?\n\nKonumunuzu gönderin veya kayıtlı yer seçin.",
        "send_destination": "🏁 Nereye gidiyorsunuz?\n\nKonumunuzu gönderin veya kayıtlı yer seçin.",
        "not_in_zone": "😔 Üzgünüz, bu bölgede henüz hizmet vermiyoruz.\n\nKuzey Kıbrıs genelinde hizmet veriyoruz.",
        "same_location": "⚠️ Alış ve varış noktaları aynı olamaz.",
        "searching": "🔍 Yakınlarda sürücü aranıyor...\n\nBu genellikle bir dakikadan az sürer.",
        "searching_wait": "⏳ Hâlâ aranıyor. Lütfen bekleyin...",
        "order_cancelled": "✖ Yolculuk iptal edildi.",
        "client_cancelled": "✖ Müşteri yolculuğu iptal etti.",
        "no_drivers": "😔 Şu anda müsait sürücü yok.\n\nBirkaç dakika sonra tekrar deneyin.",
        "active_order": "⏳ Zaten aktif bir yolculuğunuz var.",
        "cancelled": "İptal edildi.",
        "thanks_feedback": "🙏 Geri bildiriminiz için teşekkürler!",
        "order_again": "✅ Yolculuk tamamlandı. İyi günler!",
        "driver_found": (
            "🎉 <b>Sürücü bulundu!</b>\n\n"
            "👤 <b>{name}</b>\n"
            "⭐ {rating} · 📱 @{username}\n"
            "🚘 {car} · {color} · 🔢 {plate}\n\n"
            "🛣 Sürücü yolda\n"
            "⏱ Tahmini: <b>~{eta} dk</b>"
        ),
        "driver_arrived": "📍 <b>Sürücünüz geldi!</b>\n\nLütfen alış noktasına gidin.",
        "trip_started": "🚗 <b>Yolculuk başladı.</b>\n\nİyi yolculuklar!",
        "trip_finished": (
            "✅ <b>Yolculuk tamamlandı!</b>\n\n"
            "💵 <b>${price:.2f} USD</b>\n💶 €{price_eur}\n💴 ₺{price_try}\n"
            "⏱ Süre: {minutes}\n\n"
            "👤 Sürücü: <b>{name}</b>\n"
            "📱 @{username}\n\n"
            "Yolculuğunuz nasıldı?"
        ),
        "thanks_rating": "⭐ Değerlendirmeniz için teşekkürler! Görüşürüz.",
        "thanks_skip": "👍 Teşekkürler! Görüşürüz.",
        "trip_info": (
            "🚕 <b>Yolculuk Özeti</b>\n\n"
            "📏 Mesafe: <b>{distance:.2f} km</b>\n"
            "⏱ Tahmini süre: <b>~{time} dk</b>\n\n"
            "💵 <b>${usd:.2f} USD</b>\n"
            "💶 €{eur}\n💴 ₺{try_}\n\n"
            "Yolculuğu onaylıyor musunuz?"
        ),
        "trip_info_promo": (
            "🚕 <b>Yolculuk Özeti</b>\n\n"
            "📏 Mesafe: <b>{distance:.2f} km</b>\n"
            "⏱ Tahmini süre: <b>~{time} dk</b>\n\n"
            "~~${original:.2f}~~ → <b>${usd:.2f} USD</b> 🎉\n"
            "💶 €{eur}\n💴 ₺{try_}\n"
            "🎟 Promo: <b>{promo}</b> (−${discount:.2f})\n\n"
            "Yolculuğu onaylıyor musunuz?"
        ),
        "order_taken": "✖ Bu sipariş başka bir sürücü tarafından alındı.",
        "language_changed": "🇹🇷 Dil <b>Türkçe</b> olarak ayarlandı",
        "choose_language": "🌍 Dil seçin:",
        "cancel_reason": "Neden iptal ediyorsunuz?",
        "reason_late": "🕐 Sürücü çok geç kalıyor",
        "reason_changed": "🔄 Vazgeçtim",
        "reason_other": "✏️ Diğer sebep",
        "cancel_notify": "✖ Müşteri yolculuğu iptal etti.\n📝 Neden: {reason}",
        "stop_live": "🔴 <b>Yolculuk bitti!</b>\n\nLütfen canlı konum paylaşımını durdurun.",
        "duration_unknown": "—",
        "pay_crypto": (
            "💎 <b>USDT ile Ödeme (TRC20)</b>\n\n"
            "💰 Tutar: <b>{amount:.2f} USDT</b>\n\n"
            "📋 Bu adrese gönderin:\n"
            "<code>{address}</code>\n\n"
            "Gönderdikten sonra aşağıdaki butona basın."
        ),
        "pay_cash": "💵 Nakit Ödeme",
        "i_paid": "✅ Ödemeyi Gönderdim",
        "choose_payment": "💳 <b>Nasıl ödemek istersiniz?</b>",
        "payment_pending": "⏳ Ödeme doğrulama için gönderildi.\n\nBir an bekleyin...",
        "payment_confirmed": "✅ <b>Ödeme onaylandı!</b>\n\nSürücü aranıyor...",
        "payment_rejected": "❌ Ödeme doğrulanamadı.\n\nTekrar deneyin veya nakit seçin.",
        "admin_payment": "💰 <b>Ödeme doğrulaması</b>\n\n👤 @{username}\n💵 Tutar: ${amount:.2f} USDT\n\nOnaylıyor musunuz?",
        "banned": "🚫 Hesabınız askıya alındı.\n\nHata olduğunu düşünüyorsanız destek ile iletişime geçin.",
        "temp_banned": "⏳ Çok fazla yolculuk iptal ettiniz.\n\n🚫 Sipariş verme <b>{minutes} dk</b> süreyle durduruldu.",
        "cancel_warning": "⚠️ Son {window} dakikada <b>{count}/{max}</b> yolculuk iptal ettiniz.\n\nBir iptal daha hesabınızı {hours} saat duraklatacak.",
        "save_favorite": "⭐ Bu yeri kaydet",
        "favorite_name": "📝 Bu yer için bir isim girin:\n\n<i>Örnek: Ev, İş, Spor salonu</i>",
        "favorite_saved": "⭐ <b>'{name}'</b> olarak kaydedildi!",
        "use_favorite": "⭐ <b>Kayıtlı yerleriniz:</b>",
        "low_rating_prompt": "✏️ Ne yanlış gitti? Geri bildiriminiz bizi geliştirmemize yardımcı olur.\n\n<i>Mesaj gönderin veya /skip yazın</i>",
        "enter_promo": "🎟 Promosyon kodu girin (veya Atla'ya basın):",
        "promo_skip": "⏭ Atla",
        "promo_valid": "🎉 <b>{code}</b> kodu uygulandı!\nİndirim: <b>−${discount:.2f}</b>",
        "promo_invalid": "❌ Geçersiz kod. Tekrar deneyin veya atlayın.",
        "promo_used": "❌ Bu promosyon kodu zaten kullanıldı.",
        "driver_delay": "🕐 Sürücünüz yolda ancak beklenenden biraz daha uzun sürüyor.\n\nSabırlı olduğunuz için teşekkürler!",
        "history_empty": "📋 Henüz tamamlanmış yolculuğunuz yok.",
        "history_header": "📋 <b>Son yolculuklarınız:</b>\n",
        "history_item": "🗓 {date} — <b>${price:.2f}</b>",
        "broadcast_usage": "Kullanım: /broadcast Mesajınız",
        "broadcast_sent": "✅ Mesaj <b>{count}</b> müşteriye gönderildi.",
        "help": (
            "🚖 <b>Rydex — Nasıl Çalışır</b>\n\n"
            "1️⃣ <b>Yolculuk Sipariş Et</b> butonuna basın\n"
            "2️⃣ <b>Alış konumunuzu</b> gönderin\n"
            "3️⃣ <b>Varış noktanızı</b> gönderin\n"
            "4️⃣ Fiyatı onaylayın\n"
            "5️⃣ Ödeme seçin: <b>Nakit</b> veya <b>USDT</b>\n"
            "6️⃣ Sürücü bekleyin — genellikle 2 dakika!\n\n"
            "💰 <b>Fiyatlandırma</b>\n"
            "Temel ücret: $3.50\n"
            "Km başına: $0.45\n\n"
            "💳 <b>Ödeme</b>\n"
            "• Nakit — sürücüye ödeyin\n"
            "• USDT TRC20 — kripto ödeme\n\n"
            "⭐ <b>Kayıtlı Yerler</b>\n"
            "Her yolculuktan sonra varış noktasını kaydedebilirsiniz.\n\n"
            "📋 Geçmiş yolculuklar için /history kullanın.\n\n"
            "🆘 <b>Destek</b>\n"
            "İletişim: @rydex_support\n\n"
            "Dil değiştirmek için /language kullanın."
        ),
    }
}

def t(user_id, key, **kwargs):
    lang = user_lang.get(user_id, "en")
    text = TEXTS[lang].get(key, TEXTS["en"].get(key, key))
    if kwargs:
        text = text.format(**kwargs)
    return text

user_state = {}
user_data = {}
user_ratings = {}
user_lang = {}
processed_ratings = set()
pending_payments = {}
active_orders = {}
driver_active_order = {}
driver_locations = {}
driver_reg = {}

USD_TO_EUR = 0.86
USD_TO_TRY = 58.17
SEARCH_TIMEOUT = 120
ZONE = {"min_lat": 35.10, "max_lat": 35.45, "min_lon": 33.60, "max_lon": 34.20}

def in_zone(lat, lon):
    return ZONE["min_lat"] <= lat <= ZONE["max_lat"] and ZONE["min_lon"] <= lon <= ZONE["max_lon"]

def get_distance(lat1, lon1, lat2, lon2):
    return math.sqrt((lat1 - lat2)**2 + (lon1 - lon2)**2) * 111

def is_same_location(p1, p2):
    return get_distance(p1[0], p1[1], p2[0], p2[1]) < 0.1

def estimate_time_km(distance):
    return max(1, int((distance / 40) * 60))

def calc_price(distance):
    return max(MIN_PRICE, round(2 + distance * 0.45, 2))

# ===== KEYBOARDS =====
async def get_start_kb(user_id):
    last_pickup, last_dest = await db_get_last_order(user_id)
    rows = [[KeyboardButton(text=t(user_id, "find_driver"))]]
    if last_pickup and last_dest:
        rows.append([KeyboardButton(text=t(user_id, "repeat_order"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def get_yes_no_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "yes")), KeyboardButton(text=t(user_id, "no"))]],
        resize_keyboard=True
    )

def get_location_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "send_location"), request_location=True)]],
        resize_keyboard=True
    )

def get_cancel_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "cancel"))]],
        resize_keyboard=True
    )

def get_payment_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 USDT (TRC20)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text=t(user_id, "pay_cash"), callback_data="pay_cash")],
    ])

def get_promo_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "promo_skip"), callback_data="promo_skip")]
    ])

language_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
    InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="lang_tr"),
]])

def get_cancel_reason_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "reason_late"), callback_data="cancel_reason_late")],
        [InlineKeyboardButton(text=t(user_id, "reason_changed"), callback_data="cancel_reason_changed")],
        [InlineKeyboardButton(text=t(user_id, "reason_other"), callback_data="cancel_reason_other")],
    ])

async def get_favorites_kb(user_id, mode="pickup"):
    favs = await db_get_favorites(user_id)
    if not favs:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📍 {name}", callback_data=f"fav_{mode}_{name}")]
        for name in favs
    ])

# ===== NOTIFY OFFLINE DRIVERS =====
async def notify_offline_drivers(online_ids: set):
    approved = await db_get_approved_driver_ids()
    OFFLINE_TEXTS = {
        "en": "\U0001f4a4 <b>You missed a ride!</b>\n\nA client ordered while you were offline.\n\nGo online to receive orders! \U0001f446",
        "ru": "\U0001f4a4 <b>\u0412\u044b \u043f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u043b\u0438 \u0437\u0430\u043a\u0430\u0437!</b>\n\n\u041a\u043b\u0438\u0435\u043d\u0442 \u0441\u0434\u0435\u043b\u0430\u043b \u0437\u0430\u043a\u0430\u0437 \u043f\u043e\u043a\u0430 \u0432\u044b \u0431\u044b\u043b\u0438 \u043e\u0444\u043b\u0430\u0439\u043d.\n\n\u0412\u044b\u0439\u0434\u0438\u0442\u0435 \u043e\u043d\u043b\u0430\u0439\u043d! \U0001f446",
        "tr": "\U0001f4a4 <b>Bir sipari\u015fi ka\u00e7\u0131rd\u0131n\u0131z!</b>\n\n\u00c7evrimd\u0131\u015f\u0131yken m\u00fc\u015fteri sipari\u015f verdi.\n\n\u00c7evrimici olun! \U0001f446",
    }
    for did in approved:
        if did in online_ids:
            continue
        driver = await db_get_driver(did)
        if not driver:
            continue
        lang = driver["lang"] or "en"
        try:
            await bot.send_message(did, OFFLINE_TEXTS.get(lang, OFFLINE_TEXTS["en"]), parse_mode="HTML")
        except:
            pass

# ===== DRIVER DELAY =====
async def check_driver_delay(order_id: str, client_id: int):
    await asyncio.sleep(DRIVER_DELAY_NOTIFY_MINUTES * 60)
    order = active_orders.get(order_id)
    if order and order.get("taken") and not order.get("trip_start_time"):
        try:
            await bot.send_message(client_id, t(client_id, "driver_delay"), parse_mode="HTML")
        except:
            pass

# ===== CREATE ORDER =====
async def create_order(user_id, message_or_callback):
    user_state[user_id] = "searching"
    order_id = str(uuid.uuid4())[:8]
    data = user_data[user_id]
    pre_order_msgs = data.get("order_messages", [])

    active_orders[order_id] = {
        "client_id": user_id,
        "pickup": data["pickup"],
        "destination": data["destination"],
        "price": data["price"],
        "taken": False,
        "driver_id": None,
        "messages": [],
        "all_messages": {},
        "client_messages": list(pre_order_msgs),
        "client_username": getattr(message_or_callback.from_user, "username", None) or "no_username",
    }

    asyncio.create_task(search_timeout(order_id, user_id))

    price = data["price"]
    eur = round(price * USD_TO_EUR)
    try_ = round(price * USD_TO_TRY)
    username = getattr(message_or_callback.from_user, "username", None) or "no_username"
    full_name = message_or_callback.from_user.full_name
    promo = data.get("promo_code", "")

    await db_save_last_order(user_id, data["pickup"], data["destination"])

    if ADMIN_ID:
        try:
            promo_line = f"\n🎟 Promo: <b>{promo}</b>" if promo else ""
            await bot.send_message(
                ADMIN_ID,
                f"🚕 <b>New Order!</b>\n\n"
                f"👤 {full_name} · @{username}\n"
                f"💰 <b>${price:.2f}</b> USD\n   €{eur} EUR\n   ₺{try_} TRY{promo_line}\n"
                f"🆔 {order_id}",
                parse_mode="HTML"
            )
        except:
            pass

    DRIVER_TEXTS = {
        "en": {"pickup": "📍 Pickup:", "dest": "🏁 Destination:", "order": "🚕 NEW RIDE REQUEST",
               "dist": "📏 Distance to client: {dist:.2f} km", "take": "✅ Accept", "decline": "❌ Decline",
               "nav_pickup": "🗺 Navigate to Pickup"},
        "ru": {"pickup": "📍 Посадка:", "dest": "🏁 Назначение:", "order": "🚕 НОВЫЙ ЗАКАЗ",
               "dist": "📏 До клиента: {dist:.2f} км", "take": "✅ Принять", "decline": "❌ Отклонить",
               "nav_pickup": "🗺 Навигация к клиенту"},
        "tr": {"pickup": "📍 Alış:", "dest": "🏁 Varış:", "order": "🚕 YENİ SİPARİŞ",
               "dist": "📏 Müşteriye mesafe: {dist:.2f} km", "take": "✅ Kabul Et", "decline": "❌ Reddet",
               "nav_pickup": "🗺 Müşteriye Git"},
    }

    online_drivers = await db_get_online_drivers()
    online_ids = {d["user_id"] for d in online_drivers}

    drivers_with_distance = []
    for d in online_drivers:
        did = d["user_id"]
        if did in driver_locations:
            d_lat, d_lon = driver_locations[did]
            dist = get_distance(d_lat, d_lon, data["pickup"][0], data["pickup"][1])
            drivers_with_distance.append((d, dist))
        else:
            drivers_with_distance.append((d, 9999))

    drivers_with_distance.sort(key=lambda x: x[1])

    for drv, dist in drivers_with_distance:
        did = drv["user_id"]
        try:
            d_lang = drv.get("lang", "en") or "en"
            dt = DRIVER_TEXTS.get(d_lang, DRIVER_TEXTS["en"])
            pickup_lat, pickup_lon = data["pickup"]
            dest_lat, dest_lon = data["destination"]
            nav_pickup_url = f"https://maps.google.com/?q={pickup_lat},{pickup_lon}"
            nav_dest_url = f"https://maps.google.com/?q={dest_lat},{dest_lon}"

            d_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=dt["take"], callback_data=f"take_{order_id}"),
                 InlineKeyboardButton(text=dt["decline"], callback_data=f"decline_{order_id}")],
                [InlineKeyboardButton(text=dt["nav_pickup"], url=nav_pickup_url)],
            ])
            dist_text = dt["dist"].format(dist=dist) if dist < 9999 else ""
            driver_text = (
                f"{dt['order']}\n\n"
                f"👤 {full_name} · @{username}\n\n"
                f"💰 ${price:.2f} USD\n   €{eur} EUR\n   ₺{try_} TRY\n"
                f"{dist_text}"
            )
            msg = await bot.send_message(did, driver_text, reply_markup=d_kb)
            active_orders[order_id]["messages"].append((did, msg.message_id))
            active_orders[order_id]["all_messages"][did] = [msg.message_id]
            m1 = await bot.send_message(did, dt["pickup"])
            active_orders[order_id]["all_messages"][did].append(m1.message_id)
            m2 = await bot.send_location(did, *data["pickup"])
            active_orders[order_id]["all_messages"][did].append(m2.message_id)
            m3 = await bot.send_message(did, dt["dest"])
            active_orders[order_id]["all_messages"][did].append(m3.message_id)
            m4 = await bot.send_location(did, *data["destination"])
            active_orders[order_id]["all_messages"][did].append(m4.message_id)
            active_orders[order_id]["nav_dest_url"] = nav_dest_url
        except Exception as e:
            print(f"ERROR sending to driver {did}:", e)

    search_msg = await bot.send_message(user_id, t(user_id, "searching"), reply_markup=get_cancel_kb(user_id), parse_mode="HTML")
    active_orders[order_id]["client_messages"].append(search_msg.message_id)
    asyncio.create_task(notify_offline_drivers(online_ids))
    return order_id

# ===== SEARCH TIMEOUT =====
async def search_timeout(order_id, client_id):
    await asyncio.sleep(SEARCH_TIMEOUT)
    order = active_orders.get(order_id)
    if order and not order["taken"]:
        for d, msg_ids in order.get("all_messages", {}).items():
            for mid in msg_ids:
                try:
                    await bot.delete_message(d, mid)
                except:
                    pass
        active_orders.pop(order_id, None)
        user_state[client_id] = "start"
        await bot.send_message(client_id, t(client_id, "no_drivers"), reply_markup=await get_start_kb(client_id), parse_mode="HTML")

# ===== ADMIN COMMANDS =====
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    today = await db_get_stats_today()
    alltime = await db_get_stats_all()
    completed = today["completed"] if today else 0
    cancelled = today["cancelled"] if today else 0
    revenue = today["revenue"] if today else 0
    late = today["cancel_late"] if today else 0
    changed = today["cancel_changed"] if today else 0
    other = today["cancel_other"] if today else 0
    total_completed = alltime["total_completed"] or 0 if alltime else 0
    total_revenue = alltime["total_revenue"] or 0 if alltime else 0

    online_drivers = await db_get_online_drivers()
    all_drivers = await db_get_all_drivers()
    all_clients = await db_get_all_client_ids()
    active_temp_bans = sum(1 for until in temp_bans.values() if datetime.now() < until)

    cancel_detail = ""
    if cancelled:
        cancel_detail = f"\n   └ 🕐 Too long: {late}\n   └ 🔄 Changed mind: {changed}\n   └ ✏️ Other: {other}"

    await message.answer(
        f"📊 <b>Stats — {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
        f"🚕 Orders: <b>{completed + cancelled}</b>\n"
        f"✅ Completed: <b>{completed}</b>\n"
        f"❌ Cancelled: <b>{cancelled}</b>{cancel_detail}\n"
        f"⏳ Temp banned: <b>{active_temp_bans}</b>\n"
        f"💰 Revenue: <b>${revenue:.2f}</b> · €{round(revenue*USD_TO_EUR)} · ₺{round(revenue*USD_TO_TRY)}\n\n"
        f"🟢 Online: <b>{len(online_drivers)}</b> / {len(all_drivers)} drivers\n"
        f"👥 Total clients: <b>{len(all_clients)}</b>\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 <b>All time</b>\n"
        f"✅ Rides: <b>{int(total_completed)}</b>\n"
        f"💰 Revenue: <b>${total_revenue:.2f}</b> · €{round(total_revenue*USD_TO_EUR)} · ₺{round(total_revenue*USD_TO_TRY)}",
        parse_mode="HTML"
    )

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text[len("/broadcast"):].strip()
    if not text:
        await message.answer(t(ADMIN_ID, "broadcast_usage"))
        return
    client_ids = await db_get_all_client_ids()
    sent = 0
    for cid in client_ids:
        try:
            await bot.send_message(cid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(t(ADMIN_ID, "broadcast_sent", count=sent), parse_mode="HTML")

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /ban USER_ID")
        return
    target_id = int(args[1].strip())
    await db_ban(target_id)
    await message.answer(f"🚫 User <b>{target_id}</b> banned.", parse_mode="HTML")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /unban USER_ID")
        return
    target_id = int(args[1].strip())
    await db_unban(target_id)
    await message.answer(f"✅ User <b>{target_id}</b> unbanned.", parse_mode="HTML")

@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = await db_get_banned_list()
    if not rows:
        await message.answer("No banned users.")
        return
    text = "🚫 <b>Banned users:</b>\n\n"
    for row in rows:
        text += f"• <code>{row['user_id']}</code> — {row['date']}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(t(message.from_user.id, "help"), parse_mode="HTML")

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    user_id = message.from_user.id
    trips = await db_get_client_trips(user_id)
    if not trips:
        await message.answer(t(user_id, "history_empty"), parse_mode="HTML")
        return
    text = t(user_id, "history_header")
    for trip in trips:
        text += "\n" + t(user_id, "history_item", date=trip["date"], price=trip["price"])
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    if await db_is_banned(user_id):
        await message.answer(t(user_id, "banned"), parse_mode="HTML")
        return
    client = await db_get_client(user_id)
    if client and client["lang"]:
        user_lang[user_id] = client["lang"]
        user_state[user_id] = "start"
        await message.answer(
            "👋 Welcome back to <b>Rydex</b>!\n\n💡 Use /help to see how it works.",
            reply_markup=await get_start_kb(user_id), parse_mode="HTML"
        )
    else:
        user_state[user_id] = "start"
        await message.answer(
            "👋 Welcome to <b>Rydex</b>\n\nChoose your language / Выберите язык / Dil seçin:",
            reply_markup=language_kb, parse_mode="HTML"
        )

@dp.message(Command("language"))
async def change_language(message: types.Message):
    await message.answer(t(message.from_user.id, "choose_language"), reply_markup=language_kb, parse_mode="HTML")

# ===== MAIN HANDLER =====
@dp.message()
async def handler(message: types.Message):
    user_id = message.from_user.id

    if await db_is_banned(user_id):
        await message.answer(t(user_id, "banned"), parse_mode="HTML")
        return

    if is_temp_banned(user_id):
        await message.answer(t(user_id, "temp_banned", minutes=get_temp_ban_remaining(user_id)), parse_mode="HTML")
        return

    if message.text and user_state.get(user_id) not in ("searching", "low_rating_reason", "saving_favorite", "promo"):
        if is_flood(user_id):
            return

    if message.location and user_id in driver_locations:
        driver_locations[user_id] = (message.location.latitude, message.location.longitude)

    if message.location and user_id in driver_active_order:
        order_id = driver_active_order[user_id]
        order = active_orders.get(order_id)
        if order:
            await bot.send_location(order["client_id"], message.location.latitude, message.location.longitude)
        return

    if user_state.get(user_id) == "low_rating_reason":
        reason = message.text.strip() if message.text != "/skip" else "—"
        user_ratings[user_id]["low_rating_reason"] = reason
        stars = user_ratings[user_id].get("stars")
        rating_info = user_ratings.get(user_id, {})
        driver_id = rating_info.get("driver_id")
        price = rating_info.get("price", 0)
        if driver_id and stars:
            await db_update_driver_rating(driver_id, stars, price, datetime.now().strftime("%d.%m.%Y"))
        if ADMIN_ID:
            try:
                stars_display = "⭐" * stars if stars else "—"
                await bot.send_message(ADMIN_ID,
                    f"📋 <b>Trip Report</b>\n\n"
                    f"👤 Client: @{rating_info.get('client_username', '—')}\n"
                    f"🚗 Driver: @{rating_info.get('driver_username', '—')} · {rating_info.get('driver_name', '—')}\n\n"
                    f"📏 Distance: {rating_info.get('km', '—')} km\n"
                    f"💰 ${rating_info.get('price', 0):.2f} USD\n   €{round(rating_info.get('price', 0)*USD_TO_EUR)} EUR\n   ₺{round(rating_info.get('price', 0)*USD_TO_TRY)} TRY\n"
                    f"⏱ Duration: {rating_info.get('minutes', '—')}\n\n"
                    f"⭐ Rating: {stars_display}\n"
                    f"💬 Reason: {reason}\n\n"
                    f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}", parse_mode="HTML")
            except:
                pass
        user_state[user_id] = "start"
        await message.answer(t(user_id, "order_again"), reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    if user_state.get(user_id) == "saving_favorite":
        name = message.text.strip() if message.text else ""
        if name and user_data.get(user_id, {}).get("last_destination"):
            lat, lon = user_data[user_id]["last_destination"]
            await db_save_favorite(user_id, name, lat, lon)
            await message.answer(t(user_id, "favorite_saved", name=name), reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        user_state[user_id] = "start"
        return

    if user_state.get(user_id) == "promo":
        code = message.text.strip().upper() if message.text else ""
        if code in PROMO_CODES:
            if await db_is_promo_used(user_id, code):
                await message.answer(t(user_id, "promo_used"), parse_mode="HTML")
                return
            discount = PROMO_CODES[code]["discount"]
            original = user_data[user_id]["price"]
            new_price = max(MIN_PRICE, round(original - discount, 2))
            user_data[user_id]["price"] = new_price
            user_data[user_id]["promo_code"] = code
            await db_mark_promo_used(user_id, code)
            await message.answer(t(user_id, "promo_valid", code=code, discount=discount), parse_mode="HTML")
            dist = user_data[user_id].get("distance", 0)
            promo_msg = await message.answer(
                t(user_id, "trip_info_promo", distance=dist, time=estimate_time_km(dist),
                  original=original, usd=new_price,
                  eur=round(new_price*USD_TO_EUR), try_=round(new_price*USD_TO_TRY),
                  promo=code, discount=discount),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML"
            )
            user_data[user_id].setdefault("order_messages", []).append(promo_msg.message_id)
            user_state[user_id] = "final"
        else:
            await message.answer(t(user_id, "promo_invalid"), parse_mode="HTML")
        return

    if message.text == t(user_id, "cancel"):
        has_order = any(o["client_id"] == user_id for o in active_orders.values())
        if has_order:
            user_state[user_id] = "cancelling"
            await message.answer(t(user_id, "cancel_reason"), reply_markup=get_cancel_reason_kb(user_id), parse_mode="HTML")
        else:
            user_state[user_id] = "start"
            await message.answer(t(user_id, "order_cancelled"), reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    if user_state.get(user_id) == "searching":
        await message.answer(t(user_id, "searching_wait"), parse_mode="HTML")
        return

    if user_state.get(user_id) in ("cancelling", "promo"):
        return

    if message.text == t(user_id, "repeat_order"):
        last_pickup, last_dest = await db_get_last_order(user_id)
        if last_pickup and last_dest:
            for order in active_orders.values():
                if order["client_id"] == user_id:
                    await message.answer(t(user_id, "active_order"), parse_mode="HTML")
                    return
            distance = get_distance(last_dest[0], last_dest[1], last_pickup[0], last_pickup[1])
            price = calc_price(distance)
            user_data[user_id] = {"pickup": last_pickup, "destination": last_dest, "last_destination": last_dest, "price": price, "distance": distance, "order_messages": []}
            trip_msg = await message.answer(
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance), usd=price,
                  eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML"
            )
            user_data[user_id]["order_messages"].append(trip_msg.message_id)
            user_state[user_id] = "final"
        return

    if message.text == t(user_id, "find_driver"):
        for order in active_orders.values():
            if order["client_id"] == user_id:
                await message.answer(t(user_id, "active_order"), parse_mode="HTML")
                return
        user_state[user_id] = "confirm"
        user_data[user_id] = {"order_messages": []}
        msg = await message.answer(t(user_id, "continue"), reply_markup=get_yes_no_kb(user_id), parse_mode="HTML")
        user_data[user_id]["order_messages"].append(msg.message_id)
        return

    if message.text == t(user_id, "no"):
        await message.answer(t(user_id, "cancelled"), reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    if message.text == t(user_id, "yes") and user_state.get(user_id) == "confirm":
        user_state[user_id] = "pickup"
        msg = await message.answer(t(user_id, "send_pickup"), reply_markup=get_location_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
        favs = await db_get_favorites(user_id)
        if favs:
            fav_msg = await message.answer(t(user_id, "use_favorite"), reply_markup=await get_favorites_kb(user_id, "pickup"), parse_mode="HTML")
            user_data[user_id]["order_messages"].append(fav_msg.message_id)
        return

    if message.location:
        lat, lon = message.location.latitude, message.location.longitude
        if not in_zone(lat, lon):
            await message.answer(t(user_id, "not_in_zone"), parse_mode="HTML")
            return
        if user_state.get(user_id) == "pickup":
            user_data[user_id]["pickup"] = (lat, lon)
            user_state[user_id] = "destination"
            msg = await message.answer(t(user_id, "send_destination"), reply_markup=get_location_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
            favs = await db_get_favorites(user_id)
            if favs:
                fav_msg = await message.answer(t(user_id, "use_favorite"), reply_markup=await get_favorites_kb(user_id, "dest"), parse_mode="HTML")
                user_data[user_id]["order_messages"].append(fav_msg.message_id)
            return
        elif user_state.get(user_id) == "destination":
            pickup = user_data[user_id]["pickup"]
            if is_same_location(pickup, (lat, lon)):
                await message.answer(t(user_id, "same_location"), parse_mode="HTML")
                return
            distance = get_distance(lat, lon, pickup[0], pickup[1])
            price = calc_price(distance)
            user_data[user_id].update({"destination": (lat, lon), "last_destination": (lat, lon), "price": price, "distance": distance})
            trip_msg = await message.answer(
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance), usd=price,
                  eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML"
            )
            user_data[user_id].setdefault("order_messages", []).append(trip_msg.message_id)
            user_state[user_id] = "final"
            return

    if message.text == t(user_id, "yes") and user_state.get(user_id) == "final":
        user_state[user_id] = "promo"
        promo_msg = await message.answer(t(user_id, "enter_promo"), reply_markup=get_promo_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(promo_msg.message_id)
        return

# ===== CALLBACKS =====
@dp.callback_query()
async def callbacks(callback: types.CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        user_lang[user_id] = lang
        user_state[user_id] = "start"
        await db_save_client_lang(user_id, lang)
        try:
            await callback.message.delete()
        except:
            pass
        await bot.send_message(user_id, TEXTS[lang]["language_changed"], reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        await callback.answer()
        return

    if data == "promo_skip":
        user_state[user_id] = "payment"
        try:
            await callback.message.delete()
        except:
            pass
        pay_msg = await bot.send_message(user_id, t(user_id, "choose_payment"), reply_markup=get_payment_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(pay_msg.message_id)
        await callback.answer()
        return

    if data.startswith("fav_pickup_"):
        name = data[len("fav_pickup_"):]
        favs = await db_get_favorites(user_id)
        if name in favs:
            lat, lon = favs[name]["lat"], favs[name]["lon"]
            user_data[user_id]["pickup"] = (lat, lon)
            user_state[user_id] = "destination"
            try:
                await callback.message.delete()
            except:
                pass
            msg = await bot.send_message(user_id, t(user_id, "send_destination"), reply_markup=get_location_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
            favs2 = await db_get_favorites(user_id)
            if favs2:
                fav_msg = await bot.send_message(user_id, t(user_id, "use_favorite"), reply_markup=await get_favorites_kb(user_id, "dest"), parse_mode="HTML")
                user_data[user_id]["order_messages"].append(fav_msg.message_id)
        await callback.answer()
        return

    if data.startswith("fav_dest_"):
        name = data[len("fav_dest_"):]
        favs = await db_get_favorites(user_id)
        if name in favs:
            lat, lon = favs[name]["lat"], favs[name]["lon"]
            pickup = user_data[user_id]["pickup"]
            if is_same_location(pickup, (lat, lon)):
                await callback.answer("⚠️ Same as pickup!", show_alert=True)
                return
            distance = get_distance(lat, lon, pickup[0], pickup[1])
            price = calc_price(distance)
            user_data[user_id].update({"destination": (lat, lon), "last_destination": (lat, lon), "price": price, "distance": distance})
            try:
                await callback.message.delete()
            except:
                pass
            trip_msg = await bot.send_message(user_id,
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance), usd=price,
                  eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(trip_msg.message_id)
            user_state[user_id] = "final"
        await callback.answer()
        return

    if data == "save_favorite":
        user_state[user_id] = "saving_favorite"
        await bot.send_message(user_id, t(user_id, "favorite_name"), parse_mode="HTML")
        await callback.answer()
        return

    if data == "pay_cash":
        try:
            await callback.message.delete()
        except:
            pass
        await callback.answer()
        await create_order(user_id, callback)
        return

    if data == "pay_crypto":
        price = user_data[user_id]["price"]
        try:
            await callback.message.delete()
        except:
            pass
        crypto_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(user_id, "i_paid"), callback_data="crypto_paid")],
            [InlineKeyboardButton(text=t(user_id, "pay_cash"), callback_data="pay_cash")],
        ])
        crypto_msg = await bot.send_message(user_id, t(user_id, "pay_crypto", amount=price, address=USDT_ADDRESS), reply_markup=crypto_kb, parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(crypto_msg.message_id)
        await callback.answer()
        return

    if data == "crypto_paid":
        price = user_data[user_id]["price"]
        username = callback.from_user.username or "no_username"
        pending_payments[user_id] = {"price": price, "username": username}
        try:
            await callback.message.delete()
        except:
            pass
        pending_msg = await bot.send_message(user_id, t(user_id, "payment_pending"), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(pending_msg.message_id)
        if ADMIN_ID:
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Confirm", callback_data=f"payment_confirm_{user_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"payment_reject_{user_id}")
            ]])
            await bot.send_message(ADMIN_ID, t(user_id, "admin_payment", username=username, amount=price), reply_markup=admin_kb, parse_mode="HTML")
        await callback.answer()
        return

    if data.startswith("payment_confirm_"):
        client_id = int(data.split("_")[2])
        try:
            await callback.message.delete()
        except:
            pass
        confirmed_msg = await bot.send_message(client_id, t(client_id, "payment_confirmed"), parse_mode="HTML")
        user_data[client_id].setdefault("order_messages", []).append(confirmed_msg.message_id)
        pending_payments.pop(client_id, None)
        await create_order(client_id, callback)
        await callback.answer("✅ Payment confirmed")
        return

    if data.startswith("payment_reject_"):
        client_id = int(data.split("_")[2])
        try:
            await callback.message.delete()
        except:
            pass
        pending_payments.pop(client_id, None)
        user_state[client_id] = "payment"
        await bot.send_message(client_id, t(client_id, "payment_rejected"), parse_mode="HTML")
        pay_msg = await bot.send_message(client_id, t(client_id, "choose_payment"), reply_markup=get_payment_kb(client_id), parse_mode="HTML")
        user_data[client_id].setdefault("order_messages", []).append(pay_msg.message_id)
        await callback.answer("❌ Payment rejected")
        return

    if data.startswith("cancel_reason_"):
        reason_key = data.split("cancel_reason_")[1]
        reason_map = {"late": t(user_id, "reason_late"), "changed": t(user_id, "reason_changed"), "other": t(user_id, "reason_other")}
        reason = reason_map.get(reason_key, "—")
        await db_record_cancelled(reason_key)
        try:
            await callback.message.delete()
        except:
            pass
        for oid, order in list(active_orders.items()):
            if order["client_id"] == user_id:
                if order["taken"]:
                    d = order["driver_id"]
                    for mid in order["all_messages"].get(d, []):
                        try:
                            await bot.delete_message(d, mid)
                        except:
                            pass
                    await bot.send_message(d, t(user_id, "cancel_notify", reason=reason), parse_mode="HTML")
                    driver_active_order.pop(d, None)
                else:
                    for dd, msg_ids in order.get("all_messages", {}).items():
                        for mid in msg_ids:
                            try:
                                await bot.delete_message(dd, mid)
                            except:
                                pass
                for mid in order.get("client_messages", []):
                    try:
                        await bot.delete_message(user_id, mid)
                    except:
                        pass
                active_orders.pop(oid)

        limit_reached = record_cancel(user_id)
        if limit_reached:
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID,
                        f"⚠️ <b>Cancel limit reached</b>\n👤 @{callback.from_user.username or user_id}\n"
                        f"Temp banned for {CANCEL_BAN_HOURS}h", parse_mode="HTML")
                except:
                    pass
            await bot.send_message(user_id, t(user_id, "temp_banned", minutes=CANCEL_BAN_HOURS * 60), parse_mode="HTML")
        else:
            count = get_cancel_count_in_window(user_id)
            if count == MAX_CANCELS_IN_WINDOW - 1:
                await bot.send_message(user_id,
                    t(user_id, "cancel_warning", count=count, max=MAX_CANCELS_IN_WINDOW,
                      window=CANCEL_WINDOW_MINUTES, hours=CANCEL_BAN_HOURS),
                    parse_mode="HTML")
            await bot.send_message(user_id, t(user_id, "order_cancelled"), reply_markup=await get_start_kb(user_id), parse_mode="HTML")

        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, f"✖ <b>Ride cancelled</b>\n👤 @{callback.from_user.username or 'no_username'}\n📝 Reason: {reason}", parse_mode="HTML")
            except:
                pass
        user_state[user_id] = "start"
        await callback.answer()
        return

    if data.startswith("take_"):
        order_id = data.split("_")[1]
        driver_id = callback.from_user.id
        order = active_orders.get(order_id)
        if not order or order["taken"]:
            await callback.answer("✖ Already taken", True)
            return
        order["taken"] = True
        order["driver_id"] = driver_id
        order["driver_username"] = callback.from_user.username or "no_username"
        driver_active_order[driver_id] = order_id
        for d, msg_id in order["messages"]:
            if d == driver_id:
                continue
            try:
                for mid in order["all_messages"].get(d, []):
                    try:
                        await bot.delete_message(d, mid)
                    except:
                        pass
                await bot.send_message(d, t(user_id, "order_taken"), parse_mode="HTML")
            except:
                pass

        drv = await db_get_driver(driver_id)
        driver_name = drv["name"] if drv else "Driver"
        driver_car = drv["car"] if drv else "Car"
        driver_color = drv["color"] if drv else ""
        driver_plate = drv["plate"] if drv else "Unknown"
        driver_rating = drv["rating"] if drv else 5.0
        d_lang = drv["lang"] if drv else "en"

        username = callback.from_user.username or "no_username"
        client_id = order["client_id"]
        eta = 0
        if driver_id in driver_locations:
            d_lat, d_lon = driver_locations[driver_id]
            p_lat, p_lon = order["pickup"]
            eta = estimate_time_km(get_distance(d_lat, d_lon, p_lat, p_lon))

        for mid in order.get("client_messages", []):
            try:
                await bot.delete_message(client_id, mid)
            except:
                pass
        order["client_messages"] = []

        found_msg = await bot.send_message(client_id,
            t(client_id, "driver_found", name=driver_name, rating=driver_rating,
              username=username, car=driver_car, color=driver_color, plate=driver_plate,
              eta=eta if eta > 0 else "5"),
            parse_mode="HTML")
        order["client_messages"].append(found_msg.message_id)

        asyncio.create_task(check_driver_delay(order_id, client_id))

        TRIP_TEXTS = {
            "en": {"arrived": "📍 Arrived at Pickup", "start": "🚗 Start Ride", "finish": "✅ Finish Ride",
                   "client": "📱 Contact Client", "controls": "🚕 <b>Ride Controls</b>", "nav_dest": "🗺 Navigate to Destination"},
            "ru": {"arrived": "📍 Прибыл", "start": "🚗 Начать поездку", "finish": "✅ Завершить",
                   "client": "📱 Связь с клиентом", "controls": "🚕 <b>Управление поездкой</b>", "nav_dest": "🗺 Навигация к назначению"},
            "tr": {"arrived": "📍 Ulaştım", "start": "🚗 Yolculuğu Başlat", "finish": "✅ Bitir",
                   "client": "📱 Müşteri İletişim", "controls": "🚕 <b>Yolculuk Kontrolleri</b>", "nav_dest": "🗺 Varışa Git"},
        }
        tt = TRIP_TEXTS.get(d_lang, TRIP_TEXTS["en"])
        nav_dest_url = order.get("nav_dest_url", f"https://maps.google.com/?q={order['destination'][0]},{order['destination'][1]}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tt["arrived"], callback_data=f"arrived_{order_id}"),
             InlineKeyboardButton(text=tt["start"], callback_data=f"start_{order_id}")],
            [InlineKeyboardButton(text=tt["finish"], callback_data=f"finish_{order_id}")],
            [InlineKeyboardButton(text=tt["client"], callback_data=f"client_{order_id}"),
             InlineKeyboardButton(text=tt["nav_dest"], url=nav_dest_url)],
        ])
        trip_msg = await bot.send_message(driver_id, tt["controls"], reply_markup=kb, parse_mode="HTML")
        order["all_messages"].setdefault(driver_id, []).append(trip_msg.message_id)
        await callback.answer("✅ Ride accepted!")

    elif data.startswith("client_"):
        order_id = data.split("_")[1]
        order = active_orders.get(order_id)
        if order:
            await callback.answer(f"📱 @{order.get('client_username', 'no_username')}", show_alert=True)
        else:
            await callback.answer("✖ Order not found", True)

    elif data.startswith("decline_"):
        order_id = data.split("_")[1]
        driver_id = callback.from_user.id
        order = active_orders.get(order_id)
        if not order or order["taken"]:
            await callback.answer("✖ No longer available", True)
            return
        for mid in order["all_messages"].get(driver_id, []):
            try:
                await bot.delete_message(driver_id, mid)
            except:
                pass
        order["messages"] = [(d, mid) for d, mid in order["messages"] if d != driver_id]
        order["all_messages"].pop(driver_id, None)
        await callback.answer("Declined")

    elif data.startswith("arrived_"):
        order = active_orders.get(data.split("_")[1])
        if order:
            client_id = order["client_id"]
            arrived_msg = await bot.send_message(client_id, t(client_id, "driver_arrived"), reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            order["client_messages"].append(arrived_msg.message_id)

    elif data.startswith("start_"):
        order = active_orders.get(data.split("_")[1])
        if order:
            client_id = order["client_id"]
            order["trip_start_time"] = datetime.now()
            started_msg = await bot.send_message(client_id, t(client_id, "trip_started"), parse_mode="HTML")
            order["client_messages"].append(started_msg.message_id)

    elif data.startswith("finish_"):
        order_id = data.split("_")[1]
        order = active_orders.get(order_id)
        if order:
            client_id = order["client_id"]
            driver_id = order["driver_id"]
            for mid in order["all_messages"].get(driver_id, []):
                try:
                    await bot.delete_message(driver_id, mid)
                except:
                    pass
            for mid in order.get("client_messages", []):
                try:
                    await bot.delete_message(client_id, mid)
                except:
                    pass

            drv = await db_get_driver(driver_id)
            d_lang = drv["lang"] if drv else "en"
            STOP_LIVE = {"en": "🔴 <b>Trip finished!</b>\n\nPlease stop sharing your Live Location.",
                         "ru": "🔴 <b>Поездка завершена!</b>\n\nПожалуйста, остановите трансляцию геолокации.",
                         "tr": "🔴 <b>Yolculuk bitti!</b>\n\nLütfen canlı konum paylaşımını durdurun."}
            await bot.send_message(driver_id, STOP_LIVE.get(d_lang, STOP_LIVE["en"]), parse_mode="HTML")

            minutes_text = "—"
            if order.get("trip_start_time"):
                delta = datetime.now() - order["trip_start_time"]
                minutes = int(delta.total_seconds() / 60)
                duration_map = {"en": f"{minutes} min", "ru": f"{minutes} мин", "tr": f"{minutes} dk"}
                minutes_text = duration_map.get(user_lang.get(client_id, "en"), f"{minutes} min")

            driver_name = drv["name"] if drv else "Driver"
            driver_username = order.get("driver_username", "no_username")
            await db_record_completed(order["price"])
            await db_save_client_trip(client_id, order["price"], datetime.now().strftime("%d.%m.%Y"))

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 1", callback_data="rate_1"),
                 InlineKeyboardButton(text="⭐ 2", callback_data="rate_2"),
                 InlineKeyboardButton(text="⭐ 3", callback_data="rate_3")],
                [InlineKeyboardButton(text="⭐ 4", callback_data="rate_4"),
                 InlineKeyboardButton(text="⭐ 5", callback_data="rate_5"),
                 InlineKeyboardButton(text="Skip", callback_data="rate_skip")],
                [InlineKeyboardButton(text=t(client_id, "save_favorite"), callback_data="save_favorite")],
            ])
            await bot.send_message(client_id,
                t(client_id, "trip_finished", price=order["price"],
                  price_eur=round(order["price"]*USD_TO_EUR), price_try=round(order["price"]*USD_TO_TRY),
                  minutes=minutes_text, name=driver_name, username=driver_username),
                reply_markup=kb, parse_mode="HTML")
            user_state[client_id] = "rating"
            trip_km = get_distance(order["pickup"][0], order["pickup"][1], order["destination"][0], order["destination"][1])
            user_ratings[client_id] = {
                "driver_id": driver_id, "price": order["price"], "km": round(trip_km, 2),
                "minutes": minutes_text, "client_username": order.get("client_username", "no_username"),
                "driver_username": driver_username, "driver_name": driver_name,
            }
            driver_active_order.pop(driver_id, None)
            active_orders.pop(order_id)

    elif data.startswith("rate_"):
        if user_id in processed_ratings:
            await callback.answer("Already rated")
            return
        processed_ratings.add(user_id)
        try:
            await callback.message.edit_reply_markup()
        except:
            pass
        rating_info = user_ratings.get(user_id, {})
        driver_id = rating_info.get("driver_id")
        price = rating_info.get("price", 0)

        if data == "rate_skip":
            stars = None
            await bot.send_message(user_id, t(user_id, "thanks_skip"), parse_mode="HTML")
            if driver_id:
                await db_add_driver_trip(driver_id, price, datetime.now().strftime("%d.%m.%Y"))
        else:
            stars = int(data.split("_")[1])
            await bot.send_message(user_id, t(user_id, "thanks_rating"), parse_mode="HTML")
            if stars <= 3:
                user_state[user_id] = "low_rating_reason"
                user_ratings[user_id]["stars"] = stars
                await bot.send_message(user_id, t(user_id, "low_rating_prompt"), parse_mode="HTML")
                return
            if driver_id:
                await db_update_driver_rating(driver_id, stars, price, datetime.now().strftime("%d.%m.%Y"))

        if ADMIN_ID:
            try:
                stars_display = "⭐" * stars if stars else "—"
                await bot.send_message(ADMIN_ID,
                    f"📋 <b>Trip Report</b>\n\n"
                    f"👤 Client: @{rating_info.get('client_username', '—')}\n"
                    f"🚗 Driver: @{rating_info.get('driver_username', '—')} · {rating_info.get('driver_name', '—')}\n\n"
                    f"📏 Distance: {rating_info.get('km', '—')} km\n"
                    f"💰 ${rating_info.get('price', 0):.2f} USD\n   €{round(rating_info.get('price', 0)*USD_TO_EUR)} EUR\n   ₺{round(rating_info.get('price', 0)*USD_TO_TRY)} TRY\n"
                    f"⏱ Duration: {rating_info.get('minutes', '—')}\n\n"
                    f"⭐ Rating: {stars_display if stars else 'Skipped'}\n"
                    f"💬 Reason: {rating_info.get('low_rating_reason', '—')}\n\n"
                    f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}", parse_mode="HTML")
            except:
                pass
        user_state[user_id] = "start"
        await bot.send_message(user_id, t(user_id, "order_again"), reply_markup=await get_start_kb(user_id), parse_mode="HTML")

    await callback.answer()

async def main():
    if db_pool is None:
        await init_db()
    print("🚀 Rydex BOT STARTED (PostgreSQL)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
