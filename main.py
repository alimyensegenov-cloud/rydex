import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run_with_restart(coro_factory, name: str):
    """Run a coroutine factory in an infinite retry loop with exponential back-off."""
    delay = 5
    while True:
        try:
            logger.info("Starting %s…", name)
            await coro_factory()
            logger.info("%s stopped cleanly.", name)
            break
        except asyncio.CancelledError:
            logger.info("%s cancelled.", name)
            raise
        except Exception as exc:
            logger.error("%s crashed: %r — restarting in %ds…", name, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def main():
    import db
    from bot import dp as client_dp, bot as client_bot, main as client_main
    from driver_bot import dp as driver_dp, bot as driver_bot, main as driver_main

    await db.init_pool()
    await db.create_tables()
    logger.info("✅ Database ready")
    logger.info("🚀 Launching both bots…")

    await asyncio.gather(
        run_with_restart(client_main, "client-bot"),
        run_with_restart(driver_main, "driver-bot"),
    )


if __name__ == "__main__":
    asyncio.run(main())
