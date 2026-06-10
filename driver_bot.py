import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup,
)

import db
from config import ADMIN_ID, DRIVER_BOT_TOKEN, USD_TO_EUR, USD_TO_TRY
from texts import DRIVER_TEXTS as TEXTS

logger = logging.getLogger(__name__)

bot = Bot(token=DRIVER_BOT_TOKEN)
dp  = Dispatcher()

driver_state: dict = {}
driver_reg:   dict = {}
driver_lang:  dict = {}


# ── translation helper ────────────────────────────────────────────────────────
def t(user_id, key, **kwargs):
    lang = driver_lang.get(user_id, "en")
    text = TEXTS[lang].get(key, TEXTS["en"][key])
    if kwargs:
        text = text.format(**kwargs)
    return text


# ── keyboards ─────────────────────────────────────────────────────────────────
language_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
    InlineKeyboardButton(text="🇹🇷 Türkçe",  callback_data="lang_tr"),
]])


def get_driver_kb(user_id, online):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(user_id, "go_offline") if online else t(user_id, "go_online"))],
            [KeyboardButton(text=t(user_id, "my_stats"))],
        ],
        resize_keyboard=True,
    )


# ── validation helpers ────────────────────────────────────────────────────────
def is_valid_name(name: str) -> bool:
    return len(name.strip().split()) >= 2


def is_adult(dob: str) -> bool:
    try:
        birth = datetime.strptime(dob, "%Y-%m-%d")
        return (datetime.now() - birth).days / 365 >= 18
    except Exception:
        return False


# ── commands ──────────────────────────────────────────────────────────────────
@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer("🏓 Pong!")


# ── main message handler ──────────────────────────────────────────────────────
@dp.message()
async def handler(message: types.Message):
    user_id = message.from_user.id
    driver  = await db.db_get_driver(user_id)

    if driver and driver["lang"]:
        driver_lang[user_id] = driver["lang"]

    # ── /start ────────────────────────────────────────────────────────────────
    if message.text == "/start":
        if driver and driver["approved"]:
            online = driver.get("online", False)
            status = t(user_id, "online_status") if online else t(user_id, "offline_status")
            await message.answer(t(user_id, "status_msg", status=status),
                                 reply_markup=get_driver_kb(user_id, online))
        elif driver and not driver["approved"]:
            await message.answer(t(user_id, "waiting_approval"), parse_mode="HTML")
        else:
            await message.answer(
                "🚗 Driver Bot\n\nChoose your language / Выберите язык / Dil seçin:",
                reply_markup=language_kb,
            )
        return

    # ── /language ─────────────────────────────────────────────────────────────
    if message.text == "/language":
        await message.answer(t(user_id, "choose_language"), reply_markup=language_kb)
        return

    # ── /deletedriver (admin only) ────────────────────────────────────────────
    if message.text and message.text.startswith("/deletedriver") and user_id == ADMIN_ID:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Usage: /deletedriver USER_ID")
            return
        target_id = int(args[1].strip())
        await db.db_delete_driver(target_id)
        await message.answer(f"✅ Driver <code>{target_id}</code> deleted.", parse_mode="HTML")
        return

    # ── online / offline toggle ────────────────────────────────────────────────
    if message.text in [t(user_id, "go_online"), t(user_id, "go_offline")] \
            and driver and driver["approved"]:
        new_status = message.text == t(user_id, "go_online")
        await db.db_set_driver_online(user_id, new_status)
        await message.answer(
            t(user_id, "now_online") if new_status else t(user_id, "now_offline"),
            reply_markup=get_driver_kb(user_id, new_status), parse_mode="HTML")
        return

    # ── stats ─────────────────────────────────────────────────────────────────
    if message.text == t(user_id, "my_stats") and driver and driver["approved"]:
        trips        = await db.db_get_driver_trips(user_id)
        total        = driver["total_earned"] or 0
        rating       = driver["rating"]       or 5.0
        count        = driver["rating_count"] or 0
        today_str    = datetime.now().strftime("%d.%m.%Y")
        today_earned = sum(tr["price"] for tr in trips if tr.get("date") == today_str)
        history_text = "".join(
            f"\n🗓 {tr['date']} — ${tr['price']:.2f} "
            f"/ €{round(tr['price']*USD_TO_EUR)} "
            f"/ ₺{round(tr['price']*USD_TO_TRY)}"
            for tr in trips
        )
        await message.answer(
            t(user_id, "stats",
              rating=rating, count=count, trips=len(trips),
              total=total,
              total_eur=round(total * USD_TO_EUR),
              total_try=round(total * USD_TO_TRY),
              today=today_earned,
              today_eur=round(today_earned * USD_TO_EUR),
              today_try=round(today_earned * USD_TO_TRY),
              history=history_text if history_text else t(user_id, "no_trips")),
            parse_mode="HTML",
        )
        return

    # ── register ──────────────────────────────────────────────────────────────
    if message.text == t(user_id, "register"):
        if driver:
            await message.answer(t(user_id, "already_registered"))
            return
        driver_state[user_id] = "name"
        driver_reg[user_id]   = {}
        await message.answer(t(user_id, "enter_name"), parse_mode="HTML")
        return

    # ── registration flow ─────────────────────────────────────────────────────
    if driver_state.get(user_id) == "name":
        if not is_valid_name(message.text or ""):
            await message.answer(t(user_id, "invalid_name"))
            return
        driver_reg[user_id]["name"] = message.text
        driver_state[user_id]       = "dob"
        await message.answer(t(user_id, "enter_dob"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "dob":
        if not is_adult(message.text or ""):
            await message.answer(t(user_id, "must_be_18"))
            return
        driver_reg[user_id]["dob"] = message.text
        driver_state[user_id]      = "car"
        await message.answer(t(user_id, "enter_car"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "car":
        driver_reg[user_id]["car"] = message.text
        driver_state[user_id]      = "color"
        await message.answer(t(user_id, "enter_color"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "color":
        driver_reg[user_id]["color"] = message.text
        driver_state[user_id]        = "plate"
        await message.answer(t(user_id, "enter_plate"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "plate":
        driver_reg[user_id]["plate"] = message.text
        driver_state[user_id]        = "car_photo"
        await message.answer(t(user_id, "send_car_photo"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "car_photo":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["photo"] = message.photo[-1].file_id
        driver_state[user_id]        = "passport"
        await message.answer(t(user_id, "send_passport"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "passport":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["passport"] = message.photo[-1].file_id
        driver_state[user_id]           = "license"
        await message.answer(t(user_id, "send_license"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "license":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["license"] = message.photo[-1].file_id
        data = driver_reg[user_id]
        lang = driver_lang.get(user_id, "en")
        await db.db_register_driver(user_id, data, lang)

        caption = (
            f"🚗 <b>Driver Request</b>\n\n"
            f"👤 {data['name']}\n"
            f"🚘 {data['car']} · {data['color']}\n"
            f"🔢 {data['plate']}\n"
            f"🆔 <code>{user_id}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Reject",  callback_data=f"reject_{user_id}"),
        ]])
        media = [
            InputMediaPhoto(media=data["photo"],    caption=f"🚗 Car — {data['name']}"),
            InputMediaPhoto(media=data["passport"], caption="🪪 Passport"),
            InputMediaPhoto(media=data["license"],  caption="🪪 Driver's License"),
        ]
        await bot.send_media_group(ADMIN_ID, media)
        await bot.send_message(ADMIN_ID, caption, reply_markup=kb, parse_mode="HTML")
        await message.answer(t(user_id, "waiting_approval"), parse_mode="HTML")
        driver_state[user_id] = "pending"


# ── callback handler ──────────────────────────────────────────────────────────
@dp.callback_query()
async def callbacks(callback: types.CallbackQuery):
    data    = callback.data
    user_id = callback.from_user.id

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        driver_lang[user_id] = lang
        await db.db_set_driver_lang(user_id, lang)
        try:
            await callback.message.delete()
        except Exception:
            pass
        start_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=TEXTS[lang]["register"])]],
            resize_keyboard=True,
        )
        await bot.send_message(user_id, TEXTS[lang]["language_changed"],
                               reply_markup=start_kb)
        await callback.answer()
        return

    if data.startswith("approve_"):
        driver_id = int(data.split("_")[1])
        await db.db_approve_driver(driver_id)
        lang = driver_lang.get(driver_id, "en")
        await bot.send_message(driver_id, TEXTS[lang]["approved"],
                               reply_markup=get_driver_kb(driver_id, False),
                               parse_mode="HTML")
        try:
            await callback.message.edit_reply_markup()
        except Exception:
            pass
        await callback.answer("✅ Approved!")

    elif data.startswith("reject_"):
        driver_id = int(data.split("_")[1])
        lang = driver_lang.get(driver_id, "en")
        await bot.send_message(driver_id, TEXTS[lang]["rejected"], parse_mode="HTML")
        try:
            await callback.message.edit_reply_markup()
        except Exception:
            pass
        await callback.answer("❌ Rejected")

    await callback.answer()


# ── entry point ───────────────────────────────────────────────────────────────
async def main():
    logger.info("🚗 Driver bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(main())
