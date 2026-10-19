"""В тестах бот выключен по умолчанию (план B6.6, финальное покрытие блока).

Приложение с `telegram_bot_token` поднимает в `lifespan` задачу опроса Telegram. Автоматическая
фикстура `no_telegram_bot` (`tests/conftest.py`) подменяет `make_bot` на «бота нет», чтобы ни один
тест не пошёл на api.telegram.org. Здесь проверяется сама подмена, её маркер-исключение и то, что
настоящий `Bot` без подмены запретом на сеть всё равно не пускается наружу.
"""

import asyncio
import contextlib

import pytest
from aiogram import Bot, Dispatcher
from pydantic import SecretStr

from dnd_server.app import create_app
from dnd_server.bot import bot as bot_module
from dnd_server.config import Settings

# Токен верного формата: `Bot` строится без сети, а запросы идут только при первом вызове метода.
VALID_TOKEN = "123456789:AAF-no-bot-test-token-0123456789"


def with_token(settings: Settings) -> Settings:
    return settings.model_copy(update={"telegram_bot_token": SecretStr(VALID_TOKEN)})


def test_make_bot_says_there_is_no_bot_even_for_a_valid_token(settings):
    assert bot_module.make_bot(with_token(settings)) is None


async def test_an_app_with_a_valid_token_starts_no_polling_and_no_task(settings, monkeypatch):
    polling: list[object] = []

    async def forbidden(self, *bots, **options):
        polling.append(bots)

    monkeypatch.setattr(Dispatcher, "start_polling", forbidden)
    app = create_app(with_token(settings))
    tasks_before = asyncio.all_tasks()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        assert app.state.notifier.bot is None
        assert asyncio.all_tasks() == tasks_before

    assert polling == []
    await app.state.engine.dispose()


@pytest.mark.real_make_bot
def test_the_marker_gives_the_real_make_bot_back(settings):
    # Настоящая сборка: токен верного формата даёт `Bot`, ничего не отправляя в сеть.
    bot = bot_module.make_bot(with_token(settings))

    assert isinstance(bot, Bot)
    assert bot.token == VALID_TOKEN


@pytest.mark.real_make_bot
def test_the_real_make_bot_still_says_no_bot_without_a_token(settings):
    assert bot_module.make_bot(settings) is None


@pytest.mark.real_make_bot
async def test_a_real_bot_cannot_reach_telegram_from_a_test(settings, live_call_attempts):
    # Даже при настоящей сборке бота запрет сети не пускает наружу, а попытка записывается: aiogram
    # и aiohttp могли бы проглотить отказ, но тест всё равно упал бы при завершении.
    bot = bot_module.make_bot(with_token(settings))
    try:
        with contextlib.suppress(Exception):
            await bot.get_me()
    finally:
        await bot.session.close()

    assert any("getaddrinfo" in attempt or "connect" in attempt for attempt in live_call_attempts)
    live_call_attempts.clear()
