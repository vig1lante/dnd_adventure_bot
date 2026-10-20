"""Запуск бота вместе с приложением (план B6.6).

Бот получает команды опросом Telegram (polling) фоновой задачей, пока работает приложение:
`running_bot` вызывается из `lifespan`. Без `TELEGRAM_BOT_TOKEN` бот выключен — задачи нет, а
`Notifier` ничего не отправляет, так что тесты и агенты работают без токена и сети.

С одним токеном опрашивать Telegram может только один экземпляр: второй получит от Telegram
конфликт (409) и будет повторять запрос, а обновления будут доставаться то одному, то другому.
Поэтому dev- и prod-серверы с одним токеном одновременно не запускают (README).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator

from aiogram import Bot, Dispatcher
from aiogram.utils.token import TokenValidationError

from dnd_server.bot.handlers import BotDeps, build_router
from dnd_server.bot.notify import Notifier
from dnd_server.config import Settings
from dnd_server.dm.text import safe_traceback

logger = logging.getLogger(__name__)

# Сколько секунд ждём остановки polling при выключении сервера.
STOP_TIMEOUT_S = 5.0
# Журналы, куда может попасть адрес запроса к Telegram вместе с токеном: aiogram пишет текст ошибки
# сети, а в тексте ошибки aiohttp (например, сбой прокси) стоит `…/bot<токен>/getUpdates`.
PROTECTED_LOGGERS = (
    "aiogram.dispatcher",
    "aiogram.event",
    "aiogram.middlewares",
    "aiogram.webhook",
    "aiogram.scene",
    __name__,
    "dnd_server.bot.notify",
)


MASK = "***"


class TokenFilter(logging.Filter):
    """Заменяет токен бота на `***` в сообщении и в трассировке записи журнала."""

    def __init__(self, token: str) -> None:
        super().__init__()
        # В адресе запроса токен идёт целиком (`bot<число>:<секрет>`); секретная часть прячется и
        # отдельно, если она встретится без числа.
        self._secrets = tuple(part for part in (token, token.partition(":")[2]) if part)

    def _hide(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if (hidden := self._hide(message)) != message:
            record.msg, record.args = hidden, None
        if record.exc_info:
            trace = logging.Formatter().formatException(record.exc_info)
            if (hidden := self._hide(trace)) != trace:
                # Готовый текст вместо исключения: форматтер возьмёт `exc_text` как есть.
                record.exc_text, record.exc_info = hidden, None
        return True


@contextlib.contextmanager
def hide_token_in_logs(token: str) -> Iterator[None]:
    """Пока идёт `with`, записи журналов бота и aiogram не содержат токен."""
    token_filter = TokenFilter(token)
    loggers = [logging.getLogger(name) for name in PROTECTED_LOGGERS]
    for logger_ in loggers:
        logger_.addFilter(token_filter)
    try:
        yield
    finally:
        for logger_ in loggers:
            logger_.removeFilter(token_filter)


def make_bot(settings: Settings) -> Bot | None:
    """`Bot` по токену из настроек; `None` — токена нет или он неверного формата, бот выключен."""
    token = settings.telegram_bot_token
    if token is None:
        return None
    try:
        return Bot(token.get_secret_value())
    except TokenValidationError:
        # Текст ошибки aiogram токена не содержит, но и мы его не повторяем.
        logger.error(
            "TELEGRAM_BOT_TOKEN неверного формата (нужен <число>:<секрет> от BotFather): "
            "бот выключен, остальной сервер работает."
        )
        return None


def build_dispatcher(deps: BotDeps) -> Dispatcher:
    dispatcher = Dispatcher(disable_fsm=True, deps=deps)
    dispatcher.include_router(build_router())
    return dispatcher


async def poll(dispatcher: Dispatcher, bot: Bot, settings: Settings) -> None:
    """Опрос Telegram до остановки. Сбой не уходит дальше: бот — удобство, а не основа сервера."""
    try:
        # Сигналами процесса управляет uvicorn: перехват aiogram отнял бы у него SIGINT и SIGTERM.
        await dispatcher.start_polling(bot, handle_signals=False)
    except Exception as exc:
        logger.error(
            "Telegram-бот остановился из-за ошибки: сервер продолжает работать, "
            "но команды бота не принимаются.\n%s",
            safe_traceback(exc, settings),
        )


async def stop_polling(dispatcher: Dispatcher, task: asyncio.Task[None]) -> None:
    """Останавливает опрос и ждёт задачу. Просто отменять её нельзя: опрос ведут дочерние задачи
    aiogram, и без `stop_polling` они остались бы работать с закрытой сессией."""
    try:
        async with asyncio.timeout(STOP_TIMEOUT_S):
            try:
                await dispatcher.stop_polling()
            except RuntimeError:
                task.cancel()  # опрос ещё не начался (сервер сразу остановили): детей у задачи нет
            await asyncio.wait({task})
    except TimeoutError:
        logger.warning("Опрос Telegram не остановился за %s с.", STOP_TIMEOUT_S)


@contextlib.asynccontextmanager
async def running_bot(deps: BotDeps) -> AsyncIterator[Notifier]:
    """Бот на время `async with`; отдаёт `Notifier` (без токена — тот, что ничего не отправляет)."""
    bot = make_bot(deps.settings)
    if bot is None:
        yield Notifier(deps.sessionmaker, settings=deps.settings)
        return
    with hide_token_in_logs(bot.token):
        dispatcher = build_dispatcher(deps)
        polling = poll(dispatcher, bot, deps.settings)
        task = asyncio.create_task(polling, name="telegram-bot-polling")
        try:
            yield Notifier(deps.sessionmaker, bot, deps.settings)
        finally:
            await stop_polling(dispatcher, task)
            await bot.session.close()
