import logging
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

logger = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs):
    """Send a message, silently swallowing user-blocked / chat-not-found errors."""
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        logger.warning("safe_send: bot blocked by user %s", chat_id)
    except TelegramBadRequest as e:
        logger.warning("safe_send: bad request to %s — %s", chat_id, e)
    except Exception as e:
        logger.error("safe_send: unexpected error sending to %s — %s", chat_id, e)
    return None
