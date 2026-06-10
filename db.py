import asyncpg
import logging
from datetime import datetime
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    logger.info("DB pool created")


def get_pool() -> asyncpg.Pool:
    return _pool


async def create_tables():
    async with _pool.acquire() as conn:
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
        # migrations
        await conn.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS passport_id TEXT")
        await conn.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS license_id TEXT")
    logger.info("Database tables ready")


# ── clients ──────────────────────────────────────────────────────────────────

async def db_get_client(user_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM clients WHERE user_id=$1", user_id)


async def db_save_client_lang(user_id: int, lang: str):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, lang) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET lang=$2
        """, user_id, lang)


async def db_save_last_order(user_id: int, pickup: tuple, dest: tuple):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, last_pickup_lat, last_pickup_lon, last_dest_lat, last_dest_lon)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                last_pickup_lat=$2, last_pickup_lon=$3,
                last_dest_lat=$4, last_dest_lon=$5
        """, user_id, pickup[0], pickup[1], dest[0], dest[1])


async def db_get_last_order(user_id: int):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_pickup_lat, last_pickup_lon, last_dest_lat, last_dest_lon "
            "FROM clients WHERE user_id=$1", user_id)
        if row and row["last_pickup_lat"]:
            return (row["last_pickup_lat"], row["last_pickup_lon"]), \
                   (row["last_dest_lat"],  row["last_dest_lon"])
        return None, None


async def db_is_promo_used(user_id: int, code: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT used_promos FROM clients WHERE user_id=$1", user_id)
        if row:
            return code in (row["used_promos"] or [])
        return False


async def db_mark_promo_used(user_id: int, code: str):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO clients (user_id, used_promos) VALUES ($1, ARRAY[$2])
            ON CONFLICT (user_id) DO UPDATE SET used_promos = array_append(clients.used_promos, $2)
        """, user_id, code)


async def db_save_client_trip(user_id: int, price: float, date: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO client_trips (user_id, price, date) VALUES ($1, $2, $3)",
            user_id, price, date)


async def db_get_client_trips(user_id: int):
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT price, date FROM client_trips WHERE user_id=$1 "
            "ORDER BY created_at DESC LIMIT 5", user_id)
        return [dict(r) for r in rows]


async def db_get_all_client_ids():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM clients WHERE lang IS NOT NULL")
        return [r["user_id"] for r in rows]


# ── bans ──────────────────────────────────────────────────────────────────────

async def db_is_banned(user_id: int) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM banned WHERE user_id=$1", user_id)
        return row is not None


async def db_ban(user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO banned (user_id, date) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id, datetime.now().strftime("%d.%m.%Y"))


async def db_unban(user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM banned WHERE user_id=$1", user_id)


async def db_get_banned_list():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, date FROM banned")


# ── favorites ─────────────────────────────────────────────────────────────────

async def db_get_favorites(user_id: int):
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, lat, lon FROM favorites WHERE user_id=$1", user_id)
        return {r["name"]: {"lat": r["lat"], "lon": r["lon"]} for r in rows}


async def db_save_favorite(user_id: int, name: str, lat: float, lon: float):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO favorites (user_id, name, lat, lon) VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, name) DO UPDATE SET lat=$3, lon=$4
        """, user_id, name, lat, lon)


# ── drivers ───────────────────────────────────────────────────────────────────

async def db_get_driver(user_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", user_id)


async def db_register_driver(user_id: int, data: dict, lang: str = "en"):
    """Full driver registration (from driver bot) — includes passport & license."""
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO drivers
                (user_id, name, car, color, plate, photo_id, passport_id, license_id, lang, approved, online)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, FALSE, FALSE)
            ON CONFLICT (user_id) DO UPDATE SET
                name=$2, car=$3, color=$4, plate=$5, photo_id=$6,
                passport_id=$7, license_id=$8, lang=$9, approved=FALSE
        """, user_id,
            data.get("name"), data.get("car"), data.get("color"),
            data.get("plate"), data.get("photo"),
            data.get("passport"), data.get("license"), lang)


async def db_approve_driver(user_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET approved=TRUE WHERE user_id=$1", user_id)


async def db_set_driver_online(user_id: int, online: bool):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET online=$2 WHERE user_id=$1", user_id, online)


async def db_get_online_drivers():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM drivers WHERE approved=TRUE AND online=TRUE")
        return [dict(r) for r in rows]


async def db_get_approved_driver_ids():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM drivers WHERE approved=TRUE")
        return {r["user_id"] for r in rows}


async def db_get_all_drivers():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM drivers WHERE approved=TRUE")
        return [dict(r) for r in rows]


async def db_update_driver_rating(driver_id: int, stars: int, price: float, date: str):
    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE drivers SET
                rating_count = rating_count + 1,
                rating_sum   = rating_sum + $2,
                rating       = ROUND(CAST((rating_sum + $2) / (rating_count + 1) AS NUMERIC), 1),
                total_earned = total_earned + $3
            WHERE user_id=$1
        """, driver_id, float(stars), price)
        await conn.execute(
            "INSERT INTO driver_trips (driver_id, price, date) VALUES ($1, $2, $3)",
            driver_id, price, date)


async def db_add_driver_trip(driver_id: int, price: float, date: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE drivers SET total_earned=total_earned+$2 WHERE user_id=$1", driver_id, price)
        await conn.execute(
            "INSERT INTO driver_trips (driver_id, price, date) VALUES ($1, $2, $3)",
            driver_id, price, date)


async def db_get_driver_trips(driver_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT price, date FROM driver_trips WHERE driver_id=$1 "
            "ORDER BY created_at DESC LIMIT 5", driver_id)


async def db_set_driver_lang(driver_id: int, lang: str):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET lang=$2 WHERE user_id=$1", driver_id, lang)


async def db_delete_driver(driver_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM drivers WHERE user_id=$1", driver_id)
        await conn.execute("DELETE FROM driver_trips WHERE driver_id=$1", driver_id)


# ── stats ─────────────────────────────────────────────────────────────────────

async def db_record_completed(price: float):
    today = datetime.now().strftime("%d.%m.%Y")
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO stats (date, completed, revenue) VALUES ($1, 1, $2)
            ON CONFLICT (date) DO UPDATE SET
                completed = stats.completed + 1,
                revenue   = stats.revenue + $2
        """, today, price)


async def db_record_cancelled(reason: str = "other"):
    today = datetime.now().strftime("%d.%m.%Y")
    col = {"late": "cancel_late", "changed": "cancel_changed"}.get(reason, "cancel_other")
    async with _pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO stats (date, cancelled, {col}) VALUES ($1, 1, 1)
            ON CONFLICT (date) DO UPDATE SET
                cancelled = stats.cancelled + 1,
                {col} = stats.{col} + 1
        """, today)


async def db_get_stats_today():
    today = datetime.now().strftime("%d.%m.%Y")
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM stats WHERE date=$1", today)


async def db_get_stats_all():
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT SUM(completed) as total_completed, SUM(revenue) as total_revenue FROM stats")
