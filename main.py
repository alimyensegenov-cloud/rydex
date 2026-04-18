import asyncio
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

async def main():
    from bot import dp as client_dp, bot as client_bot, init_db as client_init_db
    from driver_bot import dp as driver_dp, bot as driver_bot, init_db as driver_init_db

    # Инициализируем БД для обоих ботов
    await client_init_db()
    await driver_init_db()
    print("✅ Both DBs initialized")
    print("🚀 Starting both bots...")

    await asyncio.gather(
        client_dp.start_polling(client_bot),
        driver_dp.start_polling(driver_bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
