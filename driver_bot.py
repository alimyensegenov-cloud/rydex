import asyncio
import json
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from dotenv import load_dotenv
import asyncpg

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.getenv("DRIVER_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()

db_pool: asyncpg.Pool = None
driver_state = {}
driver_reg = {}
driver_lang = {}

USD_TO_EUR = 0.86
USD_TO_TRY = 58.17

TEXTS = {
    "en": {
        "welcome": "🚗 Driver Bot\n\nChoose your language:",
        "register": "🚗 Register",
        "go_online": "🟢 Go Online",
        "go_offline": "🔴 Go Offline",
        "my_stats": "📊 My Stats",
        "online_status": "🟢 Online",
        "offline_status": "🔴 Offline",
        "status_msg": "🚗 Driver Bot\nStatus: {status}",
        "now_online": "🟢 You are now <b>Online</b>.\n\nYou will receive ride requests.",
        "now_offline": "🔴 You are now <b>Offline</b>.\n\nYou won't receive orders.",
        "already_registered": "✅ You are already registered.",
        "enter_name": (
            "📌 <b>Full name</b>\n\n"
            "Enter your full name <b>exactly as in your passport</b>, in <b>English</b>.\n\n"
            "<i>Example: John Lennon</i>"
        ),
        "invalid_name": "❌ Please enter first and last name in English",
        "enter_dob": "📅 <b>Date of birth</b>\n\nEnter your date of birth:\n\n<i>Format: YYYY-MM-DD\nExample: 1990-05-21</i>",
        "must_be_18": "❌ You must be 18 or older",
        "enter_car": (
            "🚘 <b>Car model</b>\n\n"
            "Enter your car model in <b>English</b>.\n\n"
            "<i>Example: Toyota Camry</i>"
        ),
        "enter_color": (
            "🎨 <b>Car color</b>\n\n"
            "Enter your car color in <b>English</b>.\n\n"
            "<i>Example: White, Black, Red, Silver</i>"
        ),
        "enter_plate": (
            "🔢 <b>License plate</b>\n\n"
            "Enter your license plate number.\n\n"
            "<i>Example: BK 271</i>"
        ),
        "send_car_photo": "📸 <b>Car photo</b>\n\nSend a clear photo of your car.",
        "send_passport": (
            "🪪 <b>Step 1 of 2 — Passport</b>\n\n"
            "Please send a clear photo of your <b>passport</b>.\n\n"
            "📋 Make sure:\n"
            "• Your full name is visible\n"
            "• Photo is not blurry\n"
            "• All corners are visible"
        ),
        "send_license": (
            "🪪 <b>Step 2 of 2 — Driver's License</b>\n\n"
            "Please send a clear photo of your <b>driver's license</b>.\n\n"
            "📋 Make sure:\n"
            "• Your name and license number are visible\n"
            "• Photo is not blurry\n"
            "• Both sides if needed"
        ),
        "no_photo": "❌ Please send a photo",
        "waiting_approval": (
            "⏳ <b>Application submitted!</b>\n\n"
            "Your documents are under review.\n"
            "We'll notify you within 24 hours.\n\n"
            "📋 Submitted:\n"
            "✅ Personal info\n"
            "✅ Car details\n"
            "✅ Car photo\n"
            "✅ Passport\n"
            "✅ Driver's license"
        ),
        "approved": "✅ <b>Approved!</b>\n\nYou can now go online and receive orders.",
        "rejected": "❌ Your application was not approved.\n\nContact @tvunp for more info.",
        "stats": (
            "📊 <b>My Stats</b>\n\n"
            "⭐ Rating: {rating:.1f} ({count} reviews)\n"
            "🚕 Total trips: {trips}\n"
            "💵 Total earned:\n"
            "   ${total:.2f} USD\n"
            "   €{total_eur}\n"
            "   ₺{total_try}\n"
            "💰 Today:\n"
            "   ${today:.2f} USD\n"
            "   €{today_eur}\n"
            "   ₺{today_try}\n\n"
            "🕓 Last trips:{history}"
        ),
        "no_trips": " —",
        "language_changed": "🇬🇧 Language set to English",
        "choose_language": "🌍 Choose language:",
    },
    "ru": {
        "welcome": "🚗 Бот для водителей\n\nВыберите язык:",
        "register": "🚗 Зарегистрироваться",
        "go_online": "🟢 Начать работу",
        "go_offline": "🔴 Закончить работу",
        "my_stats": "📊 Моя статистика",
        "online_status": "🟢 Онлайн",
        "offline_status": "🔴 Офлайн",
        "status_msg": "🚗 Бот для водителей\nСтатус: {status}",
        "now_online": "🟢 Вы <b>онлайн</b>.\n\nВы будете получать заказы.",
        "now_offline": "🔴 Вы <b>офлайн</b>.\n\nЗаказы не будут поступать.",
        "already_registered": "✅ Вы уже зарегистрированы.",
        "enter_name": (
            "📌 <b>Полное имя</b>\n\n"
            "Введите имя и фамилию <b>как в паспорте</b>, на <b>английском языке</b>.\n\n"
            "<i>Пример: John Lennon</i>"
        ),
        "invalid_name": "❌ Введите имя и фамилию на английском",
        "enter_dob": "📅 <b>Дата рождения</b>\n\nВведите дату рождения:\n\n<i>Формат: ГГГГ-ММ-ДД\nПример: 1990-05-21</i>",
        "must_be_18": "❌ Необходимо быть старше 18 лет",
        "enter_car": (
            "🚘 <b>Модель автомобиля</b>\n\n"
            "Введите модель автомобиля на <b>английском языке</b>.\n\n"
            "<i>Пример: Toyota Camry</i>"
        ),
        "enter_color": (
            "🎨 <b>Цвет автомобиля</b>\n\n"
            "Введите цвет автомобиля на <b>английском языке</b>.\n\n"
            "<i>Пример: White, Black, Red, Silver</i>"
        ),
        "enter_plate": (
            "🔢 <b>Номерной знак</b>\n\n"
            "Введите номерной знак автомобиля.\n\n"
            "<i>Пример: BK 271</i>"
        ),
        "send_car_photo": "📸 <b>Фото автомобиля</b>\n\nОтправьте чёткое фото вашего автомобиля.",
        "send_passport": (
            "🪪 <b>Шаг 1 из 2 — Паспорт</b>\n\n"
            "Пожалуйста, отправьте чёткое фото вашего <b>паспорта</b>.\n\n"
            "📋 Убедитесь что:\n"
            "• Полное имя читаемо\n"
            "• Фото не размыто\n"
            "• Все углы видны"
        ),
        "send_license": (
            "🪪 <b>Шаг 2 из 2 — Водительское удостоверение</b>\n\n"
            "Пожалуйста, отправьте чёткое фото вашего <b>водительского удостоверения</b>.\n\n"
            "📋 Убедитесь что:\n"
            "• Имя и номер прав читаемы\n"
            "• Фото не размыто\n"
            "• Обе стороны если необходимо"
        ),
        "no_photo": "❌ Пожалуйста, отправьте фото",
        "waiting_approval": (
            "⏳ <b>Заявка отправлена!</b>\n\n"
            "Ваши документы на проверке.\n"
            "Мы уведомим вас в течение 24 часов.\n\n"
            "📋 Отправлено:\n"
            "✅ Личные данные\n"
            "✅ Данные автомобиля\n"
            "✅ Фото автомобиля\n"
            "✅ Паспорт\n"
            "✅ Водительское удостоверение"
        ),
        "approved": "✅ <b>Одобрено!</b>\n\nТеперь вы можете выйти онлайн и получать заказы.",
        "rejected": "❌ Ваша заявка не была одобрена.\n\nСвяжитесь с @tvunp для уточнения.",
        "stats": (
            "📊 <b>Моя статистика</b>\n\n"
            "⭐ Рейтинг: {rating:.1f} ({count} отзывов)\n"
            "🚕 Всего поездок: {trips}\n"
            "💵 Всего заработано:\n"
            "   ${total:.2f} USD\n"
            "   €{total_eur}\n"
            "   ₺{total_try}\n"
            "💰 Сегодня:\n"
            "   ${today:.2f} USD\n"
            "   €{today_eur}\n"
            "   ₺{today_try}\n\n"
            "🕓 Последние поездки:{history}"
        ),
        "no_trips": " —",
        "language_changed": "🇷🇺 Язык изменён на русский",
        "choose_language": "🌍 Выберите язык:",
    },
    "tr": {
        "welcome": "🚗 Sürücü Botu\n\nDil seçin:",
        "register": "🚗 Kayıt Ol",
        "go_online": "🟢 Çevrimiçi Ol",
        "go_offline": "🔴 Çevrimdışı Ol",
        "my_stats": "📊 İstatistiklerim",
        "online_status": "🟢 Çevrimiçi",
        "offline_status": "🔴 Çevrimdışı",
        "status_msg": "🚗 Sürücü Botu\nDurum: {status}",
        "now_online": "🟢 <b>Çevrimiçisiniz</b>.\n\nSipariş alacaksınız.",
        "now_offline": "🔴 <b>Çevrimdışısınız</b>.\n\nSipariş almayacaksınız.",
        "already_registered": "✅ Zaten kayıtlısınız.",
        "enter_name": (
            "📌 <b>Tam ad</b>\n\n"
            "Adınızı ve soyadınızı <b>pasaportunuzdaki gibi</b>, <b>İngilizce</b> girin.\n\n"
            "<i>Örnek: John Lennon</i>"
        ),
        "invalid_name": "❌ Ad ve soyadınızı İngilizce girin",
        "enter_dob": "📅 <b>Dogum tarihi</b>\n\nDogum tarihinizi girin:\n\n<i>Format: YYYY-AA-GG\nÖrnek: 1990-05-21</i>",
        "must_be_18": "❌ 18 yaşından büyük olmalısınız",
        "enter_car": (
            "🚘 <b>Araç modeli</b>\n\n"
            "Araç modelini <b>İngilizce</b> girin.\n\n"
            "<i>Örnek: Toyota Camry</i>"
        ),
        "enter_color": (
            "🎨 <b>Araç rengi</b>\n\n"
            "Araç rengini <b>İngilizce</b> girin.\n\n"
            "<i>Örnek: White, Black, Red, Silver</i>"
        ),
        "enter_plate": (
            "🔢 <b>Plaka numarası</b>\n\n"
            "Plaka numaranızı girin.\n\n"
            "<i>Örnek: BK 271</i>"
        ),
        "send_car_photo": "📸 <b>Araç fotoğrafı</b>\n\nAracınızın net bir fotoğrafını gönderin.",
        "send_passport": (
            "🪪 <b>Adım 1/2 — Pasaport</b>\n\n"
            "Lütfen <b>pasaportunuzun</b> net bir fotoğrafını gönderin.\n\n"
            "📋 Dikkat edin:\n"
            "• Tam adınız görünür olsun\n"
            "• Fotoğraf bulanık olmasın\n"
            "• Tüm köşeler görünsün"
        ),
        "send_license": (
            "🪪 <b>Adım 2/2 — Sürücü Belgesi</b>\n\n"
            "Lütfen <b>sürücü belgenizin</b> net bir fotoğrafını gönderin.\n\n"
            "📋 Dikkat edin:\n"
            "• Adınız ve lisans numaranız görünsün\n"
            "• Fotoğraf bulanık olmasın\n"
            "• Gerekirse her iki taraf"
        ),
        "no_photo": "❌ Lütfen fotoğraf gönderin",
        "waiting_approval": (
            "⏳ <b>Başvuru gönderildi!</b>\n\n"
            "Belgeleriniz inceleniyor.\n"
            "24 saat içinde bildirim alacaksınız.\n\n"
            "📋 Gönderildi:\n"
            "✅ Kişisel bilgiler\n"
            "✅ Araç bilgileri\n"
            "✅ Araç fotoğrafı\n"
            "✅ Pasaport\n"
            "✅ Sürücü belgesi"
        ),
        "approved": "✅ <b>Onaylandı!</b>\n\nArtık çevrimiçi olup sipariş alabilirsiniz.",
        "rejected": "❌ Başvurunuz onaylanmadı.\n\nBilgi için @tvunp ile iletişime geçin.",
        "stats": (
            "📊 <b>İstatistiklerim</b>\n\n"
            "⭐ Puan: {rating:.1f} ({count} yorum)\n"
            "🚕 Toplam yolculuk: {trips}\n"
            "💵 Toplam kazanç:\n"
            "   ${total:.2f} USD\n"
            "   €{total_eur}\n"
            "   ₺{total_try}\n"
            "💰 Bugün:\n"
            "   ${today:.2f} USD\n"
            "   €{today_eur}\n"
            "   ₺{today_try}\n\n"
            "🕓 Son yolculuklar:{history}"
        ),
        "no_trips": " —",
        "language_changed": "🇹🇷 Dil Türkçe olarak ayarlandı",
        "choose_language": "🌍 Dil seçin:",
    }
}

def t(user_id, key, **kwargs):
    lang = driver_lang.get(user_id, "en")
    text = TEXTS[lang].get(key, TEXTS["en"][key])
    if kwargs:
        text = text.format(**kwargs)
    return text

language_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
    InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="lang_tr"),
]])

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

async def get_driver(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", user_id)

async def save_driver(user_id, data, lang="en"):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO drivers (user_id, name, car, color, plate, photo_id, passport_id, license_id, lang, approved, online)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, FALSE, FALSE)
            ON CONFLICT (user_id) DO UPDATE SET
                name=$2, car=$3, color=$4, plate=$5, photo_id=$6,
                passport_id=$7, license_id=$8, lang=$9, approved=FALSE
        """, user_id, data.get("name"), data.get("car"), data.get("color"),
            data.get("plate"), data.get("photo"), data.get("passport"), data.get("license"), lang)

async def approve_driver(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET approved=TRUE WHERE user_id=$1", user_id)

async def set_online(user_id, online):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET online=$2 WHERE user_id=$1", user_id, online)

async def set_lang(user_id, lang):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE drivers SET lang=$2 WHERE user_id=$1",
            user_id, lang)

async def get_trips(driver_id):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT price, trip_date as date FROM driver_trips WHERE driver_id=$1 ORDER BY created_at DESC LIMIT 5",
            driver_id)

def get_driver_kb(user_id, online):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(user_id, "go_offline") if online else t(user_id, "go_online"))],
            [KeyboardButton(text=t(user_id, "my_stats"))]
        ],
        resize_keyboard=True
    )

def is_valid_name(name):
    return len(name.strip().split()) >= 2

def is_adult(dob):
    try:
        birth = datetime.strptime(dob, "%Y-%m-%d")
        return (datetime.now() - birth).days / 365 >= 18
    except:
        return False

@dp.message()
async def handler(message: types.Message):
    user_id = message.from_user.id
    driver = await get_driver(user_id)

    if driver and driver["lang"]:
        driver_lang[user_id] = driver["lang"]

    if message.text == "/start":
        if driver and driver["approved"]:
            online = driver.get("online", False)
            status = t(user_id, "online_status") if online else t(user_id, "offline_status")
            await message.answer(t(user_id, "status_msg", status=status), reply_markup=get_driver_kb(user_id, online))
        elif driver and not driver["approved"]:
            await message.answer(t(user_id, "waiting_approval"), parse_mode="HTML")
        else:
            await message.answer(
                "🚗 Driver Bot\n\nChoose your language / Выберите язык / Dil seçin:",
                reply_markup=language_kb
            )
        return

    if message.text == "/language":
        await message.answer(t(user_id, "choose_language"), reply_markup=language_kb)
        return

    if message.text and message.text.startswith("/deletedriver") and message.from_user.id == ADMIN_ID:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Usage: /deletedriver USER_ID")
            return
        target_id = int(args[1].strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM drivers WHERE user_id=$1", target_id)
            await conn.execute("DELETE FROM driver_trips WHERE driver_id=$1", target_id)
        await message.answer(f"✅ Driver <code>{target_id}</code> deleted.", parse_mode="HTML")
        return

    if message.text in [t(user_id, "go_online"), t(user_id, "go_offline")] and driver and driver["approved"]:
        new_status = message.text == t(user_id, "go_online")
        await set_online(user_id, new_status)
        await message.answer(
            t(user_id, "now_online") if new_status else t(user_id, "now_offline"),
            reply_markup=get_driver_kb(user_id, new_status), parse_mode="HTML")
        return

    if message.text == t(user_id, "my_stats") and driver and driver["approved"]:
        trips = await get_trips(user_id)
        total = driver["total_earned"] or 0
        rating = driver["rating"] or 5.0
        count = driver["rating_count"] or 0

        history_text = ""
        for trip in trips:
            history_text += f"\n🗓 {trip['date']} — ${trip['price']:.2f} / €{round(trip['price']*USD_TO_EUR)} / ₺{round(trip['price']*USD_TO_TRY)}"

        today = datetime.now().strftime("%d.%m.%Y")
        today_earned = sum(tr["price"] for tr in trips if tr.get("date") == today)

        await message.answer(
            t(user_id, "stats",
              rating=rating, count=count, trips=len(trips),
              total=total, total_eur=round(total*USD_TO_EUR), total_try=round(total*USD_TO_TRY),
              today=today_earned, today_eur=round(today_earned*USD_TO_EUR), today_try=round(today_earned*USD_TO_TRY),
              history=history_text if history_text else t(user_id, "no_trips")),
            parse_mode="HTML"
        )
        return

    if message.text == t(user_id, "register"):
        if driver:
            await message.answer(t(user_id, "already_registered"))
            return
        driver_state[user_id] = "name"
        driver_reg[user_id] = {}
        await message.answer(t(user_id, "enter_name"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "name":
        if not is_valid_name(message.text):
            await message.answer(t(user_id, "invalid_name"))
            return
        driver_reg[user_id]["name"] = message.text
        driver_state[user_id] = "dob"
        await message.answer(t(user_id, "enter_dob"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "dob":
        if not is_adult(message.text):
            await message.answer(t(user_id, "must_be_18"))
            return
        driver_reg[user_id]["dob"] = message.text
        driver_state[user_id] = "car"
        await message.answer(t(user_id, "enter_car"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "car":
        driver_reg[user_id]["car"] = message.text
        driver_state[user_id] = "color"
        await message.answer(t(user_id, "enter_color"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "color":
        driver_reg[user_id]["color"] = message.text
        driver_state[user_id] = "plate"
        await message.answer(t(user_id, "enter_plate"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "plate":
        driver_reg[user_id]["plate"] = message.text
        driver_state[user_id] = "car_photo"
        await message.answer(t(user_id, "send_car_photo"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "car_photo":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["photo"] = message.photo[-1].file_id
        driver_state[user_id] = "passport"
        await message.answer(t(user_id, "send_passport"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "passport":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["passport"] = message.photo[-1].file_id
        driver_state[user_id] = "license"
        await message.answer(t(user_id, "send_license"), parse_mode="HTML")
        return

    if driver_state.get(user_id) == "license":
        if not message.photo:
            await message.answer(t(user_id, "no_photo"))
            return
        driver_reg[user_id]["license"] = message.photo[-1].file_id
        data = driver_reg[user_id]
        lang = driver_lang.get(user_id, "en")
        await save_driver(user_id, data, lang)

        caption = (
            f"🚗 <b>Driver Request</b>\n\n"
            f"👤 {data['name']}\n"
            f"🚘 {data['car']} · {data['color']}\n"
            f"🔢 {data['plate']}\n"
            f"🆔 <code>{user_id}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{user_id}")
        ]])

        from aiogram.types import InputMediaPhoto
        media = [
            InputMediaPhoto(media=data["photo"], caption=f"🚗 Car — {data['name']}"),
            InputMediaPhoto(media=data["passport"], caption="🪪 Passport"),
            InputMediaPhoto(media=data["license"], caption="🪪 Driver's License"),
        ]
        await bot.send_media_group(ADMIN_ID, media)
        await bot.send_message(ADMIN_ID, caption, reply_markup=kb, parse_mode="HTML")
        await message.answer(t(user_id, "waiting_approval"), parse_mode="HTML")
        driver_state[user_id] = "pending"

@dp.callback_query()
async def callbacks(callback: types.CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    if data.startswith("lang_"):
        lang = data.split("_")[1]
        driver_lang[user_id] = lang
        await set_lang(user_id, lang)
        try:
            await callback.message.delete()
        except:
            pass
        start_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=TEXTS[lang]["register"])]],
            resize_keyboard=True
        )
        await bot.send_message(user_id, TEXTS[lang]["language_changed"], reply_markup=start_kb)
        await callback.answer()
        return

    if data.startswith("approve_"):
        driver_id = int(data.split("_")[1])
        await approve_driver(driver_id)
        lang = driver_lang.get(driver_id, "en")
        await bot.send_message(
            driver_id,
            TEXTS[lang]["approved"],
            reply_markup=get_driver_kb(driver_id, False),
            parse_mode="HTML"
        )
        try:
            await callback.message.edit_reply_markup()
        except:
            pass
        await callback.answer("✅ Approved!")

    elif data.startswith("reject_"):
        driver_id = int(data.split("_")[1])
        lang = driver_lang.get(driver_id, "en")
        await bot.send_message(driver_id, TEXTS[lang]["rejected"], parse_mode="HTML")
        try:
            await callback.message.edit_reply_markup()
        except:
            pass
        await callback.answer("❌ Rejected")

    await callback.answer()

async def main():
    if db_pool is None:
        await init_db()
    print("🚗 Driver bot started (PostgreSQL)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
