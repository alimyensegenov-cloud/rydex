"""
Скрипт миграции данных из JSON файлов в PostgreSQL.
Запусти ОДИН РАЗ перед запуском нового бота:
    python migrate.py
"""
import asyncio
import json
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def migrate():
    print("🔄 Starting migration...")
    conn = await asyncpg.connect(DATABASE_URL)

    # clients.json
    try:
        with open("clients.json", "r") as f:
            clients = json.load(f)
        for uid, data in clients.items():
            lang = data.get("lang", "en")
            last_pickup = data.get("last_pickup")
            last_dest = data.get("last_destination")
            used_promos = data.get("used_promos", [])
            await conn.execute("""
                INSERT INTO clients (user_id, lang, last_pickup_lat, last_pickup_lon,
                    last_dest_lat, last_dest_lon, used_promos)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id) DO NOTHING
            """, int(uid), lang,
                last_pickup[0] if last_pickup else None,
                last_pickup[1] if last_pickup else None,
                last_dest[0] if last_dest else None,
                last_dest[1] if last_dest else None,
                used_promos)
            # история поездок
            for trip in data.get("trips", []):
                await conn.execute(
                    "INSERT INTO client_trips (user_id, price, date) VALUES ($1, $2, $3)",
                    int(uid), trip["price"], trip["date"])
        print(f"✅ Migrated {len(clients)} clients")
    except FileNotFoundError:
        print("⚠️ clients.json not found, skipping")

    # drivers_data.json
    try:
        with open("drivers_data.json", "r") as f:
            drivers = json.load(f)
        # drivers.txt — одобренные
        approved = set()
        try:
            with open("drivers.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        approved.add(int(line))
        except:
            pass
        for uid, data in drivers.items():
            await conn.execute("""
                INSERT INTO drivers (user_id, name, car, color, plate, lang,
                    approved, online, rating, rating_count, rating_sum, total_earned)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (user_id) DO NOTHING
            """, int(uid), data.get("name", ""), data.get("car", ""),
                data.get("color", ""), data.get("plate", ""),
                data.get("lang", "en"),
                int(uid) in approved,
                False,  # все офлайн при старте
                data.get("rating", 5.0),
                data.get("rating_count", 0),
                data.get("rating_sum", 0.0),
                data.get("total_earned", 0.0))
            for trip in data.get("trips", []):
                await conn.execute(
                    "INSERT INTO driver_trips (driver_id, price, date) VALUES ($1, $2, $3)",
                    int(uid), trip["price"], trip["date"])
        print(f"✅ Migrated {len(drivers)} drivers")
    except FileNotFoundError:
        print("⚠️ drivers_data.json not found, skipping")

    # banned.json
    try:
        with open("banned.json", "r") as f:
            banned = json.load(f)
        for uid, data in banned.items():
            await conn.execute(
                "INSERT INTO banned (user_id, date) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                int(uid), data.get("date", ""))
        print(f"✅ Migrated {len(banned)} banned users")
    except FileNotFoundError:
        print("⚠️ banned.json not found, skipping")

    # favorites.json
    try:
        with open("favorites.json", "r") as f:
            favorites = json.load(f)
        count = 0
        for uid, places in favorites.items():
            for name, coords in places.items():
                await conn.execute("""
                    INSERT INTO favorites (user_id, name, lat, lon)
                    VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING
                """, int(uid), name, coords["lat"], coords["lon"])
                count += 1
        print(f"✅ Migrated {count} favorites")
    except FileNotFoundError:
        print("⚠️ favorites.json not found, skipping")

    # stats.json
    try:
        with open("stats.json", "r") as f:
            stats = json.load(f)
        for date, data in stats.items():
            reasons = data.get("cancel_reasons", {})
            await conn.execute("""
                INSERT INTO stats (date, completed, cancelled, revenue,
                    cancel_late, cancel_changed, cancel_other)
                VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING
            """, date, data.get("completed", 0), data.get("cancelled", 0),
                data.get("revenue", 0.0),
                reasons.get("late", 0), reasons.get("changed", 0), reasons.get("other", 0))
        print(f"✅ Migrated {len(stats)} days of stats")
    except FileNotFoundError:
        print("⚠️ stats.json not found, skipping")

    await conn.close()
    print("\n✅ Migration complete!")

asyncio.run(migrate())
