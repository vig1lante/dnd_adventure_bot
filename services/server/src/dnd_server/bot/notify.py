"""Уведомления игрокам в личные сообщения Telegram (план B6.6).

`Notifier.notify(user_id, text)` — единственный вход для мастера (B7): он знает id пользователя
сервера, а Telegram-id берёт из базы. Без бота (нет `TELEGRAM_BOT_TOKEN`) и для пользователей без
Telegram-id (dev-вход) вызов ничего не делает. Сбой отправки — бот заблокирован, нет сети, Telegram
ответил ошибкой — пишется в журнал и вызывающего не касается: уведомление необязательно, а ход
мастера из-за него падать не должен.

Тексты уведомлений строят функции ниже: вызывающий передаёт только данные (сколько секунд осталось).
"""

import logging

from aiogram import Bot
from aiogram.exceptions import AiogramError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_server.db.models import User

logger = logging.getLogger(__name__)


def collecting_text(seconds_left: int) -> str:
    return f"Хост собирает ходы — осталось {max(seconds_left, 0)} с"


def combat_turn_text() -> str:
    return "Твой ход в бою"


def game_continues_text() -> str:
    return "Игра продолжается"


class Notifier:
    """Отправка уведомлений от имени бота; `bot=None` — бот выключен, уведомления не уходят."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], bot: Bot | None = None):
        self.sessionmaker = sessionmaker
        self.bot = bot

    async def notify(self, user_id: str, text: str) -> None:
        if self.bot is None:
            return
        try:
            tg_id = await self._telegram_id(user_id)
            if tg_id is not None:
                await self.bot.send_message(chat_id=tg_id, text=text)
        except AiogramError as error:
            # Ожидаемые отказы Telegram и сети (игрок заблокировал бота, тайм-аут): трассировка
            # не нужна. Текст ошибки aiogram может содержать адрес запроса, поэтому бот ставит на
            # этот журнал фильтр токена (`bot.hide_token_in_logs`).
            logger.warning(
                "Уведомление игроку %s не отправлено: %s: %s", user_id, type(error).__name__, error
            )
        except Exception:
            logger.exception("Уведомление игроку %s не отправлено из-за сбоя.", user_id)

    async def _telegram_id(self, user_id: str) -> int | None:
        async with self.sessionmaker() as session:
            return await session.scalar(select(User.tg_id).where(User.id == user_id))
