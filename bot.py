import asyncio
import logging
import math
import uuid
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

import db
from config import (
    ADMIN_ID, CANCEL_BAN_HOURS, CANCEL_WINDOW_MINUTES, DRIVER_DELAY_NOTIFY_MINUTES,
    FLOOD_INTERVAL_SECONDS, MAX_CANCELS_IN_WINDOW, MIN_PRICE, PROMO_CODES,
    SEARCH_TIMEOUT, USD_TO_EUR, USD_TO_TRY, USDT_ADDRESS, ZONE, BOT_TOKEN,
)
from middleware import DbBanMiddleware, FloodAndTempBanMiddleware
from texts import CLIENT_TEXTS as TEXTS, DRIVER_TEXTS
from utils import safe_send

logger = logging.getLogger(__name__)

# ── in-memory state ───────────────────────────────────────────────────────────
cancel_times:    dict = {}
temp_bans:       dict = {}
user_state:      dict = {}
user_data:       dict = {}
user_ratings:    dict = {}
user_lang:       dict = {}
processed_ratings: set  = set()
pending_payments: dict  = {}
active_orders:   dict = {}
driver_active_order: dict = {}
driver_locations:    dict = {}

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── translation helper ────────────────────────────────────────────────────────
def t(user_id, key, **kwargs):
    lang = user_lang.get(user_id, "en")
    text = TEXTS[lang].get(key, TEXTS["en"].get(key, key))
    if kwargs:
        text = text.format(**kwargs)
    return text


# ── geo helpers ───────────────────────────────────────────────────────────────
def in_zone(lat, lon):
    return (ZONE["min_lat"] <= lat <= ZONE["max_lat"] and
            ZONE["min_lon"] <= lon <= ZONE["max_lon"])


def get_distance(lat1, lon1, lat2, lon2):
    return math.sqrt((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) * 111


def is_same_location(p1, p2):
    return get_distance(p1[0], p1[1], p2[0], p2[1]) < 0.1


def estimate_time_km(distance):
    return max(1, int((distance / 40) * 60))


def calc_price(distance):
    return max(MIN_PRICE, round(2 + distance * 0.45, 2))


# ── cancel / temp-ban helpers (still called from handler) ────────────────────
def record_cancel(user_id):
    now          = datetime.now()
    window_start = now - timedelta(minutes=CANCEL_WINDOW_MINUTES)
    cancel_times.setdefault(user_id, [])
    cancel_times[user_id] = [t_ for t_ in cancel_times[user_id] if t_ > window_start]
    cancel_times[user_id].append(now)
    if len(cancel_times[user_id]) >= MAX_CANCELS_IN_WINDOW:
        temp_bans[user_id] = now + timedelta(hours=CANCEL_BAN_HOURS)
        cancel_times[user_id] = []
        return True
    return False


def get_cancel_count_in_window(user_id):
    now          = datetime.now()
    window_start = now - timedelta(minutes=CANCEL_WINDOW_MINUTES)
    return len([t_ for t_ in cancel_times.get(user_id, []) if t_ > window_start])


# ── keyboards ─────────────────────────────────────────────────────────────────
async def get_start_kb(user_id):
    last_pickup, last_dest = await db.db_get_last_order(user_id)
    rows = [[KeyboardButton(text=t(user_id, "find_driver"))]]
    if last_pickup and last_dest:
        rows.append([KeyboardButton(text=t(user_id, "repeat_order"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_yes_no_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "yes")),
                   KeyboardButton(text=t(user_id, "no"))]],
        resize_keyboard=True,
    )


def get_location_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "send_location"), request_location=True)]],
        resize_keyboard=True,
    )


def get_cancel_kb(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(user_id, "cancel"))]],
        resize_keyboard=True,
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
    InlineKeyboardButton(text="🇹🇷 Türkçe",  callback_data="lang_tr"),
]])


def get_cancel_reason_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "reason_late"),    callback_data="cancel_reason_late")],
        [InlineKeyboardButton(text=t(user_id, "reason_changed"), callback_data="cancel_reason_changed")],
        [InlineKeyboardButton(text=t(user_id, "reason_other"),   callback_data="cancel_reason_other")],
    ])


async def get_favorites_kb(user_id, mode="pickup"):
    favs = await db.db_get_favorites(user_id)
    if not favs:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📍 {name}", callback_data=f"fav_{mode}_{name}")]
        for name in favs
    ])


# ── background tasks ──────────────────────────────────────────────────────────
async def notify_offline_drivers(online_ids: set):
    approved = await db.db_get_approved_driver_ids()
    for did in approved:
        if did in online_ids:
            continue
        driver = await db.db_get_driver(did)
        if not driver:
            continue
        lang = driver["lang"] or "en"
        await safe_send(bot, did,
                        DRIVER_TEXTS[lang]["offline_missed"],
                        parse_mode="HTML")


async def check_driver_delay(order_id: str, client_id: int):
    await asyncio.sleep(DRIVER_DELAY_NOTIFY_MINUTES * 60)
    order = active_orders.get(order_id)
    if order and order.get("taken") and not order.get("trip_start_time"):
        await safe_send(bot, client_id, t(client_id, "driver_delay"), parse_mode="HTML")


# ── order creation ────────────────────────────────────────────────────────────
async def create_order(user_id, message_or_callback):
    user_state[user_id] = "searching"
    order_id = str(uuid.uuid4())[:8]
    data = user_data[user_id]
    pre_order_msgs = data.get("order_messages", [])

    active_orders[order_id] = {
        "client_id":       user_id,
        "pickup":          data["pickup"],
        "destination":     data["destination"],
        "price":           data["price"],
        "taken":           False,
        "driver_id":       None,
        "messages":        [],
        "all_messages":    {},
        "client_messages": list(pre_order_msgs),
        "client_username": getattr(message_or_callback.from_user, "username", None) or "no_username",
    }

    asyncio.create_task(search_timeout(order_id, user_id))

    price     = data["price"]
    eur       = round(price * USD_TO_EUR)
    try_      = round(price * USD_TO_TRY)
    username  = getattr(message_or_callback.from_user, "username", None) or "no_username"
    full_name = message_or_callback.from_user.full_name
    promo     = data.get("promo_code", "")

    await db.db_save_last_order(user_id, data["pickup"], data["destination"])

    if ADMIN_ID:
        promo_line = f"\n🎟 Promo: <b>{promo}</b>" if promo else ""
        await safe_send(
            bot, ADMIN_ID,
            f"🚕 <b>New Order!</b>\n\n"
            f"👤 {full_name} · @{username}\n"
            f"💰 <b>${price:.2f}</b> USD\n   €{eur} EUR\n   ₺{try_} TRY{promo_line}\n"
            f"🆔 {order_id}",
            parse_mode="HTML",
        )

    online_drivers = await db.db_get_online_drivers()
    online_ids     = {d["user_id"] for d in online_drivers}

    drivers_with_distance = []
    for d in online_drivers:
        did = d["user_id"]
        dist = (get_distance(driver_locations[did][0], driver_locations[did][1],
                             data["pickup"][0], data["pickup"][1])
                if did in driver_locations else 9999)
        drivers_with_distance.append((d, dist))
    drivers_with_distance.sort(key=lambda x: x[1])

    for drv, dist in drivers_with_distance:
        did    = drv["user_id"]
        d_lang = drv.get("lang", "en") or "en"
        dt     = DRIVER_TEXTS[d_lang]
        pickup_lat, pickup_lon = data["pickup"]
        dest_lat,   dest_lon   = data["destination"]
        nav_pickup_url = f"https://maps.google.com/?q={pickup_lat},{pickup_lon}"
        nav_dest_url   = f"https://maps.google.com/?q={dest_lat},{dest_lon}"

        d_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=dt["order_take"],    callback_data=f"take_{order_id}"),
             InlineKeyboardButton(text=dt["order_decline"], callback_data=f"decline_{order_id}")],
            [InlineKeyboardButton(text=dt["nav_to_pickup"], url=nav_pickup_url)],
        ])
        dist_text = dt["dist_to_client"].format(dist=dist) if dist < 9999 else ""
        driver_text = (
            f"{dt['order_request']}\n\n"
            f"👤 {full_name} · @{username}\n\n"
            f"💰 ${price:.2f} USD\n   €{eur} EUR\n   ₺{try_} TRY\n"
            f"{dist_text}"
        )
        try:
            msg = await bot.send_message(did, driver_text, reply_markup=d_kb)
            active_orders[order_id]["messages"].append((did, msg.message_id))
            active_orders[order_id]["all_messages"][did] = [msg.message_id]
            m1 = await bot.send_message(did, dt["pickup_label"])
            active_orders[order_id]["all_messages"][did].append(m1.message_id)
            m2 = await bot.send_location(did, *data["pickup"])
            active_orders[order_id]["all_messages"][did].append(m2.message_id)
            m3 = await bot.send_message(did, dt["dest_label"])
            active_orders[order_id]["all_messages"][did].append(m3.message_id)
            m4 = await bot.send_location(did, *data["destination"])
            active_orders[order_id]["all_messages"][did].append(m4.message_id)
            active_orders[order_id]["nav_dest_url"] = nav_dest_url
        except Exception as e:
            logger.warning("Could not send order to driver %s: %s", did, e)

    search_msg = await bot.send_message(
        user_id, t(user_id, "searching"),
        reply_markup=get_cancel_kb(user_id), parse_mode="HTML")
    active_orders[order_id]["client_messages"].append(search_msg.message_id)
    asyncio.create_task(notify_offline_drivers(online_ids))
    return order_id


async def search_timeout(order_id, client_id):
    await asyncio.sleep(SEARCH_TIMEOUT)
    order = active_orders.get(order_id)
    if order and not order["taken"]:
        for d, msg_ids in order.get("all_messages", {}).items():
            for mid in msg_ids:
                try:
                    await bot.delete_message(d, mid)
                except Exception:
                    pass
        active_orders.pop(order_id, None)
        user_state[client_id] = "start"
        await bot.send_message(client_id, t(client_id, "no_drivers"),
                               reply_markup=await get_start_kb(client_id), parse_mode="HTML")


# ── admin commands ────────────────────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    today   = await db.db_get_stats_today()
    alltime = await db.db_get_stats_all()

    completed = today["completed"]   if today   else 0
    cancelled = today["cancelled"]   if today   else 0
    revenue   = today["revenue"]     if today   else 0
    late      = today["cancel_late"] if today   else 0
    changed   = today["cancel_changed"] if today else 0
    other     = today["cancel_other"]   if today else 0
    total_completed = alltime["total_completed"] or 0 if alltime else 0
    total_revenue   = alltime["total_revenue"]   or 0 if alltime else 0

    online_drivers = await db.db_get_online_drivers()
    all_drivers    = await db.db_get_all_drivers()
    all_clients    = await db.db_get_all_client_ids()
    active_temp    = sum(1 for until in temp_bans.values() if datetime.now() < until)

    cancel_detail = ""
    if cancelled:
        cancel_detail = (f"\n   └ 🕐 Too long: {late}"
                         f"\n   └ 🔄 Changed mind: {changed}"
                         f"\n   └ ✏️ Other: {other}")

    await message.answer(
        f"📊 <b>Stats — {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
        f"🚕 Orders: <b>{completed + cancelled}</b>\n"
        f"✅ Completed: <b>{completed}</b>\n"
        f"❌ Cancelled: <b>{cancelled}</b>{cancel_detail}\n"
        f"⏳ Temp banned: <b>{active_temp}</b>\n"
        f"💰 Revenue: <b>${revenue:.2f}</b> · €{round(revenue*USD_TO_EUR)} · ₺{round(revenue*USD_TO_TRY)}\n\n"
        f"🟢 Online: <b>{len(online_drivers)}</b> / {len(all_drivers)} drivers\n"
        f"👥 Total clients: <b>{len(all_clients)}</b>\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 <b>All time</b>\n"
        f"✅ Rides: <b>{int(total_completed)}</b>\n"
        f"💰 Revenue: <b>${total_revenue:.2f}</b> · "
        f"€{round(total_revenue*USD_TO_EUR)} · ₺{round(total_revenue*USD_TO_TRY)}",
        parse_mode="HTML",
    )


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text[len("/broadcast"):].strip()
    if not text:
        await message.answer(t(ADMIN_ID, "broadcast_usage"))
        return
    client_ids = await db.db_get_all_client_ids()
    sent = 0
    for cid in client_ids:
        if await safe_send(bot, cid, text, parse_mode="HTML"):
            sent += 1
        await asyncio.sleep(0.05)
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
    await db.db_ban(target_id)
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
    await db.db_unban(target_id)
    await message.answer(f"✅ User <b>{target_id}</b> unbanned.", parse_mode="HTML")


@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = await db.db_get_banned_list()
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


@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer("🏓 Pong!")


@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    user_id = message.from_user.id
    trips = await db.db_get_client_trips(user_id)
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
    client  = await db.db_get_client(user_id)
    if client and client["lang"]:
        user_lang[user_id]  = client["lang"]
        user_state[user_id] = "start"
        await message.answer(
            "👋 Welcome back to <b>Rydex</b>!\n\n💡 Use /help to see how it works.",
            reply_markup=await get_start_kb(user_id), parse_mode="HTML",
        )
    else:
        user_state[user_id] = "start"
        await message.answer(
            "👋 Welcome to <b>Rydex</b>\n\nChoose your language / Выберите язык / Dil seçin:",
            reply_markup=language_kb, parse_mode="HTML",
        )


@dp.message(Command("language"))
async def change_language(message: types.Message):
    await message.answer(
        t(message.from_user.id, "choose_language"),
        reply_markup=language_kb, parse_mode="HTML",
    )


# ── main message handler ──────────────────────────────────────────────────────
@dp.message()
async def handler(message: types.Message):
    user_id = message.from_user.id

    # location forwarding for active driver
    if message.location and user_id in driver_locations:
        driver_locations[user_id] = (message.location.latitude, message.location.longitude)

    if message.location and user_id in driver_active_order:
        order_id = driver_active_order[user_id]
        order    = active_orders.get(order_id)
        if order:
            await bot.send_location(order["client_id"],
                                    message.location.latitude, message.location.longitude)
        return

    # low-rating reason
    if user_state.get(user_id) == "low_rating_reason":
        reason      = message.text.strip() if message.text != "/skip" else "—"
        rating_info = user_ratings.get(user_id, {})
        driver_id   = rating_info.get("driver_id")
        price       = rating_info.get("price", 0)
        stars       = rating_info.get("stars")
        if driver_id and stars:
            await db.db_update_driver_rating(driver_id, stars, price,
                                             datetime.now().strftime("%d.%m.%Y"))
        if ADMIN_ID:
            stars_display = "⭐" * stars if stars else "—"
            await safe_send(
                bot, ADMIN_ID,
                f"📋 <b>Trip Report</b>\n\n"
                f"👤 Client: @{rating_info.get('client_username', '—')}\n"
                f"🚗 Driver: @{rating_info.get('driver_username', '—')} · {rating_info.get('driver_name', '—')}\n\n"
                f"📏 Distance: {rating_info.get('km', '—')} km\n"
                f"💰 ${rating_info.get('price', 0):.2f} USD\n"
                f"   €{round(rating_info.get('price', 0)*USD_TO_EUR)} EUR\n"
                f"   ₺{round(rating_info.get('price', 0)*USD_TO_TRY)} TRY\n"
                f"⏱ Duration: {rating_info.get('minutes', '—')}\n\n"
                f"⭐ Rating: {stars_display}\n"
                f"💬 Reason: {reason}\n\n"
                f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                parse_mode="HTML",
            )
        user_state[user_id] = "start"
        await message.answer(t(user_id, "order_again"),
                             reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    # saving favorite name
    if user_state.get(user_id) == "saving_favorite":
        name = message.text.strip() if message.text else ""
        if name and user_data.get(user_id, {}).get("last_destination"):
            lat, lon = user_data[user_id]["last_destination"]
            await db.db_save_favorite(user_id, name, lat, lon)
            await message.answer(t(user_id, "favorite_saved", name=name),
                                 reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        user_state[user_id] = "start"
        return

    # promo code entry
    if user_state.get(user_id) == "promo":
        code = message.text.strip().upper() if message.text else ""
        if code in PROMO_CODES:
            if await db.db_is_promo_used(user_id, code):
                await message.answer(t(user_id, "promo_used"), parse_mode="HTML")
                return
            discount = PROMO_CODES[code]["discount"]
            original = user_data[user_id]["price"]
            new_price = max(MIN_PRICE, round(original - discount, 2))
            user_data[user_id]["price"]      = new_price
            user_data[user_id]["promo_code"] = code
            await db.db_mark_promo_used(user_id, code)
            await message.answer(t(user_id, "promo_valid", code=code, discount=discount),
                                 parse_mode="HTML")
            dist = user_data[user_id].get("distance", 0)
            promo_msg = await message.answer(
                t(user_id, "trip_info_promo",
                  distance=dist, time=estimate_time_km(dist),
                  original=original, usd=new_price,
                  eur=round(new_price*USD_TO_EUR), try_=round(new_price*USD_TO_TRY),
                  promo=code, discount=discount),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML",
            )
            user_data[user_id].setdefault("order_messages", []).append(promo_msg.message_id)
            user_state[user_id] = "final"
        else:
            await message.answer(t(user_id, "promo_invalid"), parse_mode="HTML")
        return

    # cancel button
    if message.text == t(user_id, "cancel"):
        has_order = any(o["client_id"] == user_id for o in active_orders.values())
        if has_order:
            user_state[user_id] = "cancelling"
            await message.answer(t(user_id, "cancel_reason"),
                                 reply_markup=get_cancel_reason_kb(user_id), parse_mode="HTML")
        else:
            user_state[user_id] = "start"
            await message.answer(t(user_id, "order_cancelled"),
                                 reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    if user_state.get(user_id) == "searching":
        await message.answer(t(user_id, "searching_wait"), parse_mode="HTML")
        return

    if user_state.get(user_id) in ("cancelling", "promo"):
        return

    # repeat last order
    if message.text == t(user_id, "repeat_order"):
        last_pickup, last_dest = await db.db_get_last_order(user_id)
        if last_pickup and last_dest:
            for order in active_orders.values():
                if order["client_id"] == user_id:
                    await message.answer(t(user_id, "active_order"), parse_mode="HTML")
                    return
            distance = get_distance(last_dest[0], last_dest[1], last_pickup[0], last_pickup[1])
            price    = calc_price(distance)
            user_data[user_id] = {
                "pickup": last_pickup, "destination": last_dest,
                "last_destination": last_dest, "price": price,
                "distance": distance, "order_messages": [],
            }
            trip_msg = await message.answer(
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance),
                  usd=price, eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML",
            )
            user_data[user_id]["order_messages"].append(trip_msg.message_id)
            user_state[user_id] = "final"
        return

    # start new order
    if message.text == t(user_id, "find_driver"):
        for order in active_orders.values():
            if order["client_id"] == user_id:
                await message.answer(t(user_id, "active_order"), parse_mode="HTML")
                return
        user_state[user_id] = "confirm"
        user_data[user_id]  = {"order_messages": []}
        msg = await message.answer(t(user_id, "continue"),
                                   reply_markup=get_yes_no_kb(user_id), parse_mode="HTML")
        user_data[user_id]["order_messages"].append(msg.message_id)
        return

    if message.text == t(user_id, "no"):
        await message.answer(t(user_id, "cancelled"),
                             reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        return

    if message.text == t(user_id, "yes") and user_state.get(user_id) == "confirm":
        user_state[user_id] = "pickup"
        msg = await message.answer(t(user_id, "send_pickup"),
                                   reply_markup=get_location_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
        favs = await db.db_get_favorites(user_id)
        if favs:
            fav_msg = await message.answer(
                t(user_id, "use_favorite"),
                reply_markup=await get_favorites_kb(user_id, "pickup"), parse_mode="HTML")
            user_data[user_id]["order_messages"].append(fav_msg.message_id)
        return

    # location handling
    if message.location:
        lat, lon = message.location.latitude, message.location.longitude
        if not in_zone(lat, lon):
            await message.answer(t(user_id, "not_in_zone"), parse_mode="HTML")
            return
        if user_state.get(user_id) == "pickup":
            user_data[user_id]["pickup"] = (lat, lon)
            user_state[user_id] = "destination"
            msg = await message.answer(t(user_id, "send_destination"),
                                       reply_markup=get_location_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
            favs = await db.db_get_favorites(user_id)
            if favs:
                fav_msg = await message.answer(
                    t(user_id, "use_favorite"),
                    reply_markup=await get_favorites_kb(user_id, "dest"), parse_mode="HTML")
                user_data[user_id]["order_messages"].append(fav_msg.message_id)
            return
        elif user_state.get(user_id) == "destination":
            pickup = user_data[user_id]["pickup"]
            if is_same_location(pickup, (lat, lon)):
                await message.answer(t(user_id, "same_location"), parse_mode="HTML")
                return
            distance = get_distance(lat, lon, pickup[0], pickup[1])
            price    = calc_price(distance)
            user_data[user_id].update({
                "destination": (lat, lon), "last_destination": (lat, lon),
                "price": price, "distance": distance,
            })
            trip_msg = await message.answer(
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance),
                  usd=price, eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML",
            )
            user_data[user_id].setdefault("order_messages", []).append(trip_msg.message_id)
            user_state[user_id] = "final"
            return

    if message.text == t(user_id, "yes") and user_state.get(user_id) == "final":
        user_state[user_id] = "promo"
        promo_msg = await message.answer(t(user_id, "enter_promo"),
                                         reply_markup=get_promo_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(promo_msg.message_id)
        return


# ── callback handler ──────────────────────────────────────────────────────────
@dp.callback_query()
async def callbacks(callback: types.CallbackQuery):
    data    = callback.data
    user_id = callback.from_user.id

    # language change
    if data.startswith("lang_"):
        lang = data.split("_")[1]
        user_lang[user_id]  = lang
        user_state[user_id] = "start"
        await db.db_save_client_lang(user_id, lang)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_message(user_id, TEXTS[lang]["language_changed"],
                               reply_markup=await get_start_kb(user_id), parse_mode="HTML")
        await callback.answer()
        return

    # promo skip
    if data == "promo_skip":
        user_state[user_id] = "payment"
        try:
            await callback.message.delete()
        except Exception:
            pass
        pay_msg = await bot.send_message(user_id, t(user_id, "choose_payment"),
                                         reply_markup=get_payment_kb(user_id), parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(pay_msg.message_id)
        await callback.answer()
        return

    # favorite pickup
    if data.startswith("fav_pickup_"):
        name = data[len("fav_pickup_"):]
        favs = await db.db_get_favorites(user_id)
        if name in favs:
            lat, lon = favs[name]["lat"], favs[name]["lon"]
            user_data[user_id]["pickup"] = (lat, lon)
            user_state[user_id] = "destination"
            try:
                await callback.message.delete()
            except Exception:
                pass
            msg = await bot.send_message(user_id, t(user_id, "send_destination"),
                                         reply_markup=get_location_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(msg.message_id)
            favs2 = await db.db_get_favorites(user_id)
            if favs2:
                fav_msg = await bot.send_message(
                    user_id, t(user_id, "use_favorite"),
                    reply_markup=await get_favorites_kb(user_id, "dest"), parse_mode="HTML")
                user_data[user_id]["order_messages"].append(fav_msg.message_id)
        await callback.answer()
        return

    # favorite destination
    if data.startswith("fav_dest_"):
        name = data[len("fav_dest_"):]
        favs = await db.db_get_favorites(user_id)
        if name in favs:
            lat, lon = favs[name]["lat"], favs[name]["lon"]
            pickup   = user_data[user_id]["pickup"]
            if is_same_location(pickup, (lat, lon)):
                await callback.answer("⚠️ Same as pickup!", show_alert=True)
                return
            distance = get_distance(lat, lon, pickup[0], pickup[1])
            price    = calc_price(distance)
            user_data[user_id].update({
                "destination": (lat, lon), "last_destination": (lat, lon),
                "price": price, "distance": distance,
            })
            try:
                await callback.message.delete()
            except Exception:
                pass
            trip_msg = await bot.send_message(
                user_id,
                t(user_id, "trip_info", distance=distance, time=estimate_time_km(distance),
                  usd=price, eur=round(price*USD_TO_EUR), try_=round(price*USD_TO_TRY)),
                reply_markup=get_yes_no_kb(user_id), parse_mode="HTML")
            user_data[user_id].setdefault("order_messages", []).append(trip_msg.message_id)
            user_state[user_id] = "final"
        await callback.answer()
        return

    # save favorite
    if data == "save_favorite":
        user_state[user_id] = "saving_favorite"
        await bot.send_message(user_id, t(user_id, "favorite_name"), parse_mode="HTML")
        await callback.answer()
        return

    # payment: cash
    if data == "pay_cash":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        await create_order(user_id, callback)
        return

    # payment: crypto
    if data == "pay_crypto":
        price = user_data[user_id]["price"]
        try:
            await callback.message.delete()
        except Exception:
            pass
        crypto_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(user_id, "i_paid"),    callback_data="crypto_paid")],
            [InlineKeyboardButton(text=t(user_id, "pay_cash"),  callback_data="pay_cash")],
        ])
        crypto_msg = await bot.send_message(
            user_id,
            t(user_id, "pay_crypto", amount=price, address=USDT_ADDRESS),
            reply_markup=crypto_kb, parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(crypto_msg.message_id)
        await callback.answer()
        return

    # crypto paid — awaiting admin confirm
    if data == "crypto_paid":
        price    = user_data[user_id]["price"]
        username = callback.from_user.username or "no_username"
        pending_payments[user_id] = {"price": price, "username": username}
        try:
            await callback.message.delete()
        except Exception:
            pass
        pending_msg = await bot.send_message(user_id, t(user_id, "payment_pending"),
                                             parse_mode="HTML")
        user_data[user_id].setdefault("order_messages", []).append(pending_msg.message_id)
        if ADMIN_ID:
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Confirm",
                                     callback_data=f"payment_confirm_{user_id}"),
                InlineKeyboardButton(text="❌ Reject",
                                     callback_data=f"payment_reject_{user_id}"),
            ]])
            await safe_send(bot, ADMIN_ID,
                            t(user_id, "admin_payment", username=username, amount=price),
                            reply_markup=admin_kb, parse_mode="HTML")
        await callback.answer()
        return

    if data.startswith("payment_confirm_"):
        client_id = int(data.split("_")[2])
        try:
            await callback.message.delete()
        except Exception:
            pass
        confirmed_msg = await bot.send_message(client_id, t(client_id, "payment_confirmed"),
                                               parse_mode="HTML")
        user_data[client_id].setdefault("order_messages", []).append(confirmed_msg.message_id)
        pending_payments.pop(client_id, None)
        await create_order(client_id, callback)
        await callback.answer("✅ Payment confirmed")
        return

    if data.startswith("payment_reject_"):
        client_id = int(data.split("_")[2])
        try:
            await callback.message.delete()
        except Exception:
            pass
        pending_payments.pop(client_id, None)
        user_state[client_id] = "payment"
        await bot.send_message(client_id, t(client_id, "payment_rejected"), parse_mode="HTML")
        pay_msg = await bot.send_message(client_id, t(client_id, "choose_payment"),
                                         reply_markup=get_payment_kb(client_id), parse_mode="HTML")
        user_data[client_id].setdefault("order_messages", []).append(pay_msg.message_id)
        await callback.answer("❌ Payment rejected")
        return

    # cancel reason
    if data.startswith("cancel_reason_"):
        reason_key = data.split("cancel_reason_")[1]
        reason_map = {
            "late":    t(user_id, "reason_late"),
            "changed": t(user_id, "reason_changed"),
            "other":   t(user_id, "reason_other"),
        }
        reason = reason_map.get(reason_key, "—")
        await db.db_record_cancelled(reason_key)
        try:
            await callback.message.delete()
        except Exception:
            pass
        for oid, order in list(active_orders.items()):
            if order["client_id"] == user_id:
                if order["taken"]:
                    d = order["driver_id"]
                    for mid in order["all_messages"].get(d, []):
                        try:
                            await bot.delete_message(d, mid)
                        except Exception:
                            pass
                    await bot.send_message(d, t(user_id, "cancel_notify", reason=reason),
                                           parse_mode="HTML")
                    driver_active_order.pop(d, None)
                else:
                    for dd, msg_ids in order.get("all_messages", {}).items():
                        for mid in msg_ids:
                            try:
                                await bot.delete_message(dd, mid)
                            except Exception:
                                pass
                for mid in order.get("client_messages", []):
                    try:
                        await bot.delete_message(user_id, mid)
                    except Exception:
                        pass
                active_orders.pop(oid)

        limit_reached = record_cancel(user_id)
        if limit_reached:
            if ADMIN_ID:
                await safe_send(
                    bot, ADMIN_ID,
                    f"⚠️ <b>Cancel limit reached</b>\n"
                    f"👤 @{callback.from_user.username or user_id}\n"
                    f"Temp banned for {CANCEL_BAN_HOURS}h",
                    parse_mode="HTML",
                )
            await bot.send_message(user_id,
                                   t(user_id, "temp_banned", minutes=CANCEL_BAN_HOURS * 60),
                                   parse_mode="HTML")
        else:
            count = get_cancel_count_in_window(user_id)
            if count == MAX_CANCELS_IN_WINDOW - 1:
                await bot.send_message(
                    user_id,
                    t(user_id, "cancel_warning", count=count, max=MAX_CANCELS_IN_WINDOW,
                      window=CANCEL_WINDOW_MINUTES, hours=CANCEL_BAN_HOURS),
                    parse_mode="HTML",
                )
            await bot.send_message(user_id, t(user_id, "order_cancelled"),
                                   reply_markup=await get_start_kb(user_id), parse_mode="HTML")

        if ADMIN_ID:
            await safe_send(
                bot, ADMIN_ID,
                f"✖ <b>Ride cancelled</b>\n"
                f"👤 @{callback.from_user.username or 'no_username'}\n"
                f"📝 Reason: {reason}",
                parse_mode="HTML",
            )
        user_state[user_id] = "start"
        await callback.answer()
        return

    # driver takes order
    if data.startswith("take_"):
        order_id  = data.split("_")[1]
        driver_id = callback.from_user.id
        order     = active_orders.get(order_id)
        if not order or order["taken"]:
            await callback.answer("✖ Already taken", True)
            return
        order["taken"]           = True
        order["driver_id"]       = driver_id
        order["driver_username"] = callback.from_user.username or "no_username"
        driver_active_order[driver_id] = order_id

        for d, msg_id in order["messages"]:
            if d == driver_id:
                continue
            try:
                for mid in order["all_messages"].get(d, []):
                    try:
                        await bot.delete_message(d, mid)
                    except Exception:
                        pass
                await bot.send_message(d, t(user_id, "order_taken"), parse_mode="HTML")
            except Exception:
                pass

        drv          = await db.db_get_driver(driver_id)
        driver_name  = drv["name"]   if drv else "Driver"
        driver_car   = drv["car"]    if drv else "Car"
        driver_color = drv["color"]  if drv else ""
        driver_plate = drv["plate"]  if drv else "Unknown"
        driver_rating= drv["rating"] if drv else 5.0
        d_lang       = drv["lang"]   if drv else "en"

        username  = callback.from_user.username or "no_username"
        client_id = order["client_id"]
        eta = 0
        if driver_id in driver_locations:
            d_lat, d_lon = driver_locations[driver_id]
            p_lat, p_lon = order["pickup"]
            eta = estimate_time_km(get_distance(d_lat, d_lon, p_lat, p_lon))

        for mid in order.get("client_messages", []):
            try:
                await bot.delete_message(client_id, mid)
            except Exception:
                pass
        order["client_messages"] = []

        found_msg = await bot.send_message(
            client_id,
            t(client_id, "driver_found",
              name=driver_name, rating=driver_rating, username=username,
              car=driver_car, color=driver_color, plate=driver_plate,
              eta=eta if eta > 0 else "5"),
            parse_mode="HTML")
        order["client_messages"].append(found_msg.message_id)
        asyncio.create_task(check_driver_delay(order_id, client_id))

        dt  = DRIVER_TEXTS[d_lang]
        nav_dest_url = order.get(
            "nav_dest_url",
            f"https://maps.google.com/?q={order['destination'][0]},{order['destination'][1]}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=dt["trip_arrived_btn"], callback_data=f"arrived_{order_id}"),
             InlineKeyboardButton(text=dt["trip_start_btn"],   callback_data=f"start_{order_id}")],
            [InlineKeyboardButton(text=dt["trip_finish_btn"],  callback_data=f"finish_{order_id}")],
            [InlineKeyboardButton(text=dt["contact_client"],   callback_data=f"client_{order_id}"),
             InlineKeyboardButton(text=dt["nav_to_dest"],      url=nav_dest_url)],
        ])
        trip_msg = await bot.send_message(driver_id, dt["trip_controls"],
                                          reply_markup=kb, parse_mode="HTML")
        order["all_messages"].setdefault(driver_id, []).append(trip_msg.message_id)
        await callback.answer("✅ Ride accepted!")

    elif data.startswith("client_"):
        order_id = data.split("_")[1]
        order    = active_orders.get(order_id)
        if order:
            await callback.answer(f"📱 @{order.get('client_username', 'no_username')}",
                                  show_alert=True)
        else:
            await callback.answer("✖ Order not found", True)

    elif data.startswith("decline_"):
        order_id  = data.split("_")[1]
        driver_id = callback.from_user.id
        order     = active_orders.get(order_id)
        if not order or order["taken"]:
            await callback.answer("✖ No longer available", True)
            return
        for mid in order["all_messages"].get(driver_id, []):
            try:
                await bot.delete_message(driver_id, mid)
            except Exception:
                pass
        order["messages"]    = [(d, mid) for d, mid in order["messages"] if d != driver_id]
        order["all_messages"].pop(driver_id, None)
        await callback.answer("Declined")

    elif data.startswith("arrived_"):
        order = active_orders.get(data.split("_")[1])
        if order:
            client_id = order["client_id"]
            arrived_msg = await bot.send_message(
                client_id, t(client_id, "driver_arrived"),
                reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            order["client_messages"].append(arrived_msg.message_id)

    elif data.startswith("start_"):
        order = active_orders.get(data.split("_")[1])
        if order:
            client_id = order["client_id"]
            order["trip_start_time"] = datetime.now()
            started_msg = await bot.send_message(
                client_id, t(client_id, "trip_started"), parse_mode="HTML")
            order["client_messages"].append(started_msg.message_id)

    elif data.startswith("finish_"):
        order_id = data.split("_")[1]
        order    = active_orders.get(order_id)
        if order:
            client_id = order["client_id"]
            driver_id = order["driver_id"]
            for mid in order["all_messages"].get(driver_id, []):
                try:
                    await bot.delete_message(driver_id, mid)
                except Exception:
                    pass
            for mid in order.get("client_messages", []):
                try:
                    await bot.delete_message(client_id, mid)
                except Exception:
                    pass

            drv    = await db.db_get_driver(driver_id)
            d_lang = drv["lang"] if drv else "en"
            await bot.send_message(driver_id,
                                   DRIVER_TEXTS[d_lang]["stop_live"],
                                   parse_mode="HTML")

            minutes_text = "—"
            if order.get("trip_start_time"):
                delta   = datetime.now() - order["trip_start_time"]
                minutes = int(delta.total_seconds() / 60)
                dur_map = {"en": f"{minutes} min", "ru": f"{minutes} мин", "tr": f"{minutes} dk"}
                minutes_text = dur_map.get(user_lang.get(client_id, "en"), f"{minutes} min")

            driver_name     = drv["name"] if drv else "Driver"
            driver_username = order.get("driver_username", "no_username")
            await db.db_record_completed(order["price"])
            await db.db_save_client_trip(
                client_id, order["price"], datetime.now().strftime("%d.%m.%Y"))

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 1", callback_data="rate_1"),
                 InlineKeyboardButton(text="⭐ 2", callback_data="rate_2"),
                 InlineKeyboardButton(text="⭐ 3", callback_data="rate_3")],
                [InlineKeyboardButton(text="⭐ 4", callback_data="rate_4"),
                 InlineKeyboardButton(text="⭐ 5", callback_data="rate_5"),
                 InlineKeyboardButton(text="Skip",  callback_data="rate_skip")],
                [InlineKeyboardButton(text=t(client_id, "save_favorite"),
                                      callback_data="save_favorite")],
            ])
            await bot.send_message(
                client_id,
                t(client_id, "trip_finished",
                  price=order["price"],
                  price_eur=round(order["price"]*USD_TO_EUR),
                  price_try=round(order["price"]*USD_TO_TRY),
                  minutes=minutes_text, name=driver_name, username=driver_username),
                reply_markup=kb, parse_mode="HTML")
            user_state[client_id] = "rating"
            trip_km = get_distance(order["pickup"][0], order["pickup"][1],
                                   order["destination"][0], order["destination"][1])
            user_ratings[client_id] = {
                "driver_id":       driver_id,
                "price":           order["price"],
                "km":              round(trip_km, 2),
                "minutes":         minutes_text,
                "client_username": order.get("client_username", "no_username"),
                "driver_username": driver_username,
                "driver_name":     driver_name,
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
        except Exception:
            pass
        rating_info = user_ratings.get(user_id, {})
        driver_id   = rating_info.get("driver_id")
        price       = rating_info.get("price", 0)

        if data == "rate_skip":
            stars = None
            await bot.send_message(user_id, t(user_id, "thanks_skip"), parse_mode="HTML")
            if driver_id:
                await db.db_add_driver_trip(
                    driver_id, price, datetime.now().strftime("%d.%m.%Y"))
        else:
            stars = int(data.split("_")[1])
            await bot.send_message(user_id, t(user_id, "thanks_rating"), parse_mode="HTML")
            if stars <= 3:
                user_state[user_id]         = "low_rating_reason"
                user_ratings[user_id]["stars"] = stars
                await bot.send_message(user_id, t(user_id, "low_rating_prompt"),
                                       parse_mode="HTML")
                return
            if driver_id:
                await db.db_update_driver_rating(
                    driver_id, stars, price, datetime.now().strftime("%d.%m.%Y"))

        if ADMIN_ID:
            stars_display = "⭐" * stars if stars else "—"
            await safe_send(
                bot, ADMIN_ID,
                f"📋 <b>Trip Report</b>\n\n"
                f"👤 Client: @{rating_info.get('client_username', '—')}\n"
                f"🚗 Driver: @{rating_info.get('driver_username', '—')} · "
                f"{rating_info.get('driver_name', '—')}\n\n"
                f"📏 Distance: {rating_info.get('km', '—')} km\n"
                f"💰 ${rating_info.get('price', 0):.2f} USD\n"
                f"   €{round(rating_info.get('price', 0)*USD_TO_EUR)} EUR\n"
                f"   ₺{round(rating_info.get('price', 0)*USD_TO_TRY)} TRY\n"
                f"⏱ Duration: {rating_info.get('minutes', '—')}\n\n"
                f"⭐ Rating: {stars_display if stars else 'Skipped'}\n"
                f"💬 Reason: {rating_info.get('low_rating_reason', '—')}\n\n"
                f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                parse_mode="HTML",
            )
        user_state[user_id] = "start"
        await bot.send_message(user_id, t(user_id, "order_again"),
                               reply_markup=await get_start_kb(user_id), parse_mode="HTML")

    await callback.answer()


# ── entry point ───────────────────────────────────────────────────────────────
async def main():
    dp.message.middleware(DbBanMiddleware(user_lang, TEXTS))
    dp.message.middleware(FloodAndTempBanMiddleware(temp_bans, user_state, user_lang, TEXTS,
                                                    FLOOD_INTERVAL_SECONDS))
    logger.info("🚀 Rydex client bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(main())
