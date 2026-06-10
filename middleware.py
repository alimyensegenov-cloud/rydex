import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

import db
from config import FLOOD_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


class DbBanMiddleware(BaseMiddleware):
    """Block permanently banned users before any handler runs."""

    def __init__(self, user_lang: dict, texts: dict):
        self._user_lang = user_lang
        self._texts = texts

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        user_id = event.from_user.id
        if await db.db_is_banned(user_id):
            lang = self._user_lang.get(user_id, "en")
            await event.answer(self._texts[lang]["banned"], parse_mode="HTML")
            return
        return await handler(event, data)


class FloodAndTempBanMiddleware(BaseMiddleware):
    """
    Two checks combined to avoid two middleware round-trips:
    1. Temporary cancel-spam ban (in-memory, managed by bot.py).
    2. Flood control for text messages (not applied in certain FSM states).
    """

    def __init__(
        self,
        temp_bans: dict,
        user_state: dict,
        user_lang: dict,
        texts: dict,
        interval: float = FLOOD_INTERVAL_SECONDS,
    ):
        self._temp_bans  = temp_bans
        self._user_state = user_state
        self._user_lang  = user_lang
        self._texts      = texts
        self._interval   = interval
        self._last_time: Dict[int, datetime] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = event.from_user.id
        now     = datetime.now()

        # ── temp-ban check ────────────────────────────────────────────────────
        ban_until = self._temp_bans.get(user_id)
        if ban_until:
            if now < ban_until:
                remaining = max(1, int((ban_until - now).total_seconds() / 60))
                lang = self._user_lang.get(user_id, "en")
                await event.answer(
                    self._texts[lang]["temp_banned"].format(minutes=remaining),
                    parse_mode="HTML",
                )
                return
            else:
                del self._temp_bans[user_id]

        # ── flood check (text only, skip certain states) ──────────────────────
        exempt_states = ("searching", "low_rating_reason", "saving_favorite", "promo")
        if event.text and self._user_state.get(user_id) not in exempt_states:
            last = self._last_time.get(user_id)
            if last and (now - last).total_seconds() < self._interval:
                return  # silently drop
            self._last_time[user_id] = now

        return await handler(event, data)
