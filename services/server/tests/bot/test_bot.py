"""Telegram-бот (план B6.6): запуск polling, `/start`, `/newgame`, ссылка-приглашение, уведомления.

Настоящий Telegram и сеть не нужны: у `Bot` стоит сессия-заглушка `RecordingSession`, которая
запоминает методы Bot API и отвечает на них заготовленными объектами. Логика `/newgame` и
построение ссылки — функции без сети, их тесты вызывают их напрямую; обработчики проверяются через
`Dispatcher.feed_update` с обновлениями, собранными вручную. Приложение работает на настоящей SQLite
после миграций (фикстуры `settings`, `db` из `tests/conftest.py`).
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, GetMe, GetUpdates, SendMessage, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from multidict import CIMultiDict, CIMultiDictProxy
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

from dnd_engine.content import load_srd
from dnd_server.adventures import Catalog, CatalogEntry, load_catalog
from dnd_server.app import create_app
from dnd_server.bot import bot as bot_module
from dnd_server.bot.bot import build_dispatcher
from dnd_server.bot.handlers import (
    BotDeps,
    NEWGAME_PREFIX,
    adventure_id_from_callback,
    invite_link,
    newgame_keyboard,
    open_lobby,
    webapp_keyboard,
)
from dnd_server.bot.notify import (
    Notifier,
    collecting_text,
    combat_turn_text,
    game_continues_text,
)
from dnd_server.config import Settings
from dnd_server.db.models import Game, GamePhase, GamePlayer
from dnd_server.db.models import User as DbUser

# tests/bot/test_bot.py -> bot -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "basic_round.yaml"
BOT_TOKEN = "123456789:AAF-test-token-for-bot-tests_0123456789"
BOT_USERNAME = "dnd_test_bot"
BOT_ID = 123456789
WEBAPP_URL = "https://app.example.test"
CREATOR_TG_ID = 700
OUTSIDER_TG_ID = 701
INVITE = f"https://t.me/{BOT_USERNAME}?startapp="


# --- заглушки Bot API ---


class RecordingSession(BaseSession):
    """Сессия `Bot` без сети: запоминает вызванные методы и отвечает на них заготовками."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Бот", username=BOT_USERNAME)
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.01)  # длинный опрос: без паузы цикл не отдавал бы управление
            return []
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    def sent(self) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [r for r in self.requests if isinstance(r, AnswerCallbackQuery)]


class FailingSendSession(RecordingSession):
    """Telegram отказывает в отправке (например, игрок заблокировал бота)."""

    def __init__(self, error: Callable[[TelegramMethod[Any]], Exception]) -> None:
        super().__init__()
        self._error = error

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.requests.append(method)
            raise self._error(method)
        return await super().make_request(bot, method, timeout)


def make_test_bot(session: RecordingSession | None = None) -> tuple[Bot, RecordingSession]:
    session = session or RecordingSession()
    return Bot(BOT_TOKEN, session=session), session


class FakeAiohttp:
    """Вместо `aiohttp.ClientSession`: отвечает на `getMe` и `getUpdates`, а `getUpdates` роняет.

    Через неё идёт настоящая `AiohttpSession` aiogram: ошибка aiohttp оборачивается в
    `TelegramNetworkError` так же, как при сбое сети или прокси.
    """

    def __init__(self, updates_error: Exception) -> None:
        self._updates_error = updates_error

    def post(self, url: str, **_options: Any) -> "FakeAiohttp._Reply":
        if url.endswith("/getMe"):
            user = {"id": BOT_ID, "is_bot": True, "first_name": "Бот", "username": "b_bot"}
            return self._Reply(body=json.dumps({"ok": True, "result": user}))
        return self._Reply(error=self._updates_error)

    class _Reply:
        status = 200

        def __init__(self, body: str = "", error: Exception | None = None) -> None:
            self._body = body
            self._error = error

        async def __aenter__(self) -> "FakeAiohttp._Reply":
            if self._error is not None:
                raise self._error
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def text(self) -> str:
            return self._body


def aiohttp_error_with_url(url: str) -> aiohttp.ClientResponseError:
    """Ошибка aiohttp, в тексте которой стоит адрес запроса (так бывает при сбое прокси)."""
    info = aiohttp.RequestInfo(URL(url), "POST", CIMultiDictProxy(CIMultiDict()), URL(url))
    return aiohttp.ClientResponseError(
        info, (), status=407, message="Proxy Authentication Required"
    )


# --- обновления Telegram ---


def telegram_user(tg_id: int, first_name: str = "Борис") -> User:
    return User(id=tg_id, is_bot=False, first_name=first_name)


def message_update(text: str, tg_id: int, first_name: str = "Борис") -> Update:
    command = text.split()[0]
    entities = [MessageEntity(type="bot_command", offset=0, length=len(command))]
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=tg_id, type="private"),
            from_user=telegram_user(tg_id, first_name),
            text=text,
            entities=entities,
        ),
    )


def callback_update(data: str | None, tg_id: int, first_name: str = "Борис") -> Update:
    keyboard_message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=tg_id, type="private"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Бот"),
        text="Выберите приключение",
    )
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=telegram_user(tg_id, first_name),
            chat_instance="chat-instance",
            message=keyboard_message,
            data=data,
        ),
    )


# --- приложение, каталог, игроки ---


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    shutil.copytree(DEMO_ADVENTURE_DIR, directory / "demo-crypt")
    return directory


@pytest.fixture
def settings(settings: Settings, content_dir: Path) -> Settings:
    """Dev-настройки из conftest: бот с именем, один создатель игр, `fake` со сценарием."""
    return settings.model_copy(
        update={
            "content_dir": content_dir,
            "telegram_bot_username": BOT_USERNAME,
            "game_creator_tg_ids": (CREATOR_TG_ID,),
            "webapp_public_url": WEBAPP_URL,
            "fake_llm_scenario": SCENARIO,
        }
    )


@pytest.fixture
def catalog(content_dir: Path) -> Catalog:
    return load_catalog(content_dir, load_srd())


@pytest.fixture
def deps(settings: Settings, db: async_sessionmaker[AsyncSession], catalog: Catalog) -> BotDeps:
    return BotDeps(settings=settings, sessionmaker=db, catalog=catalog, turn_tokens=None)


async def count(db: async_sessionmaker[AsyncSession], model: type) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def lobby_reply(deps: BotDeps, tg_id: int = CREATOR_TG_ID, adventure_id: str = "demo-crypt"):
    async with deps.sessionmaker() as session:
        return await open_lobby(session, deps, tg_id=tg_id, name="Борис", adventure_id=adventure_id)


def keyboard_of(method: SendMessage) -> list[Any]:
    markup = method.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button for row in markup.inline_keyboard for button in row]


async def feed(deps: BotDeps, update: Update) -> RecordingSession:
    bot, session = make_test_bot()
    await build_dispatcher(deps).feed_update(bot, update)
    return session


# --- ссылка-приглашение ---


def test_invite_link_opens_the_mini_app_of_the_bot_with_the_game_code(settings):
    assert invite_link(settings, "ABC123") == f"{INVITE}ABC123"


def test_without_a_bot_username_there_is_no_link_instead_of_a_broken_one(settings):
    nameless = settings.model_copy(update={"telegram_bot_username": None})

    assert invite_link(nameless, "ABC123") is None


# --- кнопка Mini App для /start ---


def test_start_button_opens_the_mini_app_by_its_public_address():
    markup = webapp_keyboard(WEBAPP_URL)

    [[button]] = markup.inline_keyboard
    assert button.web_app.url == WEBAPP_URL


@pytest.mark.parametrize(
    "url", ["http://localhost:5173", "ftp://x.example", "", "app.example.test"]
)
def test_no_web_app_button_for_an_address_telegram_would_refuse(url):
    # Telegram открывает Mini App только по https; кнопка с другим адресом не отправится вовсе.
    assert webapp_keyboard(url) is None


# --- кнопки выбора приключения ---


def adventure_buttons(catalog: Catalog) -> list[Any]:
    markup = newgame_keyboard(catalog)
    return [button for row in markup.inline_keyboard for button in row]


def test_every_playable_adventure_gets_a_button_with_its_title(catalog):
    [button] = adventure_buttons(catalog)

    assert button.text == catalog["demo-crypt"].adventure.title
    assert adventure_id_from_callback(button.callback_data, catalog) == "demo-crypt"


def test_adventures_with_blocking_errors_get_no_button(content_dir):
    broken = shutil.copytree(DEMO_ADVENTURE_DIR, content_dir / "blocked-crypt")
    manifest = broken / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("id: demo-crypt", "id: blocked-crypt", 1),
        encoding="utf-8",
    )
    cemetery = broken / "locations" / "cemetery.yaml"
    cemetery.write_text(
        cemetery.read_text(encoding="utf-8").replace("to: crypt", "to: nowhere", 1),
        encoding="utf-8",
    )
    catalog = load_catalog(content_dir, load_srd())

    assert catalog["blocked-crypt"].report.blocking  # условие теста: приключение в каталоге есть
    buttons = adventure_buttons(catalog)
    assert [adventure_id_from_callback(b.callback_data, catalog) for b in buttons] == ["demo-crypt"]
    # Подделанная кнопка с id приключения с ошибками тоже не проходит.
    forged = buttons[0].callback_data.replace("demo-crypt", "blocked-crypt")
    assert adventure_id_from_callback(forged, catalog) is None


def test_callback_data_of_a_button_fits_the_telegram_limit(catalog):
    long_id = "a" * 64  # допустимый id схемы, но с префиксом он не помещается в 64 байта
    adventure = catalog["demo-crypt"].adventure.model_copy(update={"id": long_id})
    catalog = {**catalog, long_id: CatalogEntry(adventure, catalog["demo-crypt"].report)}

    buttons = adventure_buttons(catalog)

    assert all(1 <= len(button.callback_data.encode()) <= 64 for button in buttons)
    assert [adventure_id_from_callback(b.callback_data, catalog) for b in buttons] == ["demo-crypt"]


def test_no_playable_adventures_means_no_keyboard():
    assert newgame_keyboard({}) is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "demo-crypt",
        NEWGAME_PREFIX,
        NEWGAME_PREFIX + "unknown",
        NEWGAME_PREFIX + "demo-crypt:extra",
        NEWGAME_PREFIX + "a" * 500,
        "xx:demo-crypt",
    ],
)
def test_callback_data_is_untrusted_and_must_name_a_catalog_adventure(catalog, data):
    assert adventure_id_from_callback(data, catalog) is None


# --- /newgame: создание игры от имени пользователя Telegram ---


async def test_a_creator_gets_a_game_in_the_lobby_and_an_invite_link(deps, db):
    reply = await lobby_reply(deps)

    async with db() as session:
        game = await session.scalar(select(Game))
        host = await session.scalar(select(DbUser))
        players = list(await session.scalars(select(GamePlayer)))
    assert game.phase == GamePhase.LOBBY
    assert game.adventure_id == "demo-crypt"
    assert game.host_user_id == host.id
    assert host.tg_id == CREATOR_TG_ID and host.name == "Борис"
    assert [(p.user_id, p.is_host) for p in players] == [(host.id, True)]
    assert reply.link == f"{INVITE}{game.code}"
    assert game.code in reply.text and reply.link in reply.text


async def test_the_game_follows_the_same_rules_as_a_game_made_by_the_app(deps, db):
    await lobby_reply(deps)

    async with db() as session:
        game = await session.scalar(select(Game))
    # Копия приключения и длительности из env: это делает `games.service.create_game`.
    assert game.adventure_data == deps.catalog["demo-crypt"].adventure.model_dump(mode="json")
    assert (game.collect_seconds, game.hurry_seconds, game.camp_seconds) == (
        deps.settings.collect_seconds,
        deps.settings.hurry_seconds,
        deps.settings.camp_seconds,
    )


async def test_the_same_creator_is_found_not_duplicated(deps, db):
    await lobby_reply(deps)
    await lobby_reply(deps)

    assert await count(db, DbUser) == 1
    assert await count(db, Game) == 2


async def test_a_user_outside_the_list_is_refused_and_not_even_created(deps, db):
    reply = await lobby_reply(deps, tg_id=OUTSIDER_TG_ID)

    assert reply.link is None
    assert "только" in reply.text  # понятный отказ: создавать игры может не каждый
    assert await count(db, DbUser) == 0
    assert await count(db, Game) == 0


async def test_an_adventure_that_is_not_in_the_catalog_is_refused_in_words(deps, db):
    reply = await lobby_reply(deps, adventure_id="no-such-adventure")

    assert reply.link is None and reply.text
    assert await count(db, Game) == 0


async def test_a_provider_that_cannot_be_built_is_reported_not_raised(deps, db):
    unusable = deps.settings.model_copy(update={"llm_provider": "anthropic_api"})  # ключа нет
    reply = await lobby_reply(BotDeps(unusable, deps.sessionmaker, deps.catalog, None))

    assert reply.link is None
    assert "Провайдер" in reply.text
    assert await count(db, Game) == 0


async def test_without_a_bot_username_the_reply_gives_the_code_instead_of_a_link(deps, db):
    nameless = deps.settings.model_copy(update={"telegram_bot_username": None})

    reply = await lobby_reply(BotDeps(nameless, deps.sessionmaker, deps.catalog, None))

    async with db() as session:
        game = await session.scalar(select(Game))
    assert reply.link is None
    assert game.code in reply.text
    assert "t.me" not in reply.text


# --- обработчики: /start, /newgame, нажатие кнопки ---


async def test_start_greets_and_offers_the_mini_app_button(deps):
    session = await feed(deps, message_update("/start", CREATOR_TG_ID))

    [greeting] = session.sent()
    [button] = keyboard_of(greeting)
    assert greeting.text
    assert button.web_app.url == WEBAPP_URL


async def test_start_ignores_the_deep_link_argument(deps, db):
    # Вход в игру идёт через `startapp` Mini App, а не через `/start <код>`.
    session = await feed(deps, message_update("/start ABC123", OUTSIDER_TG_ID))

    [greeting] = session.sent()
    assert [b.web_app.url for b in keyboard_of(greeting)] == [WEBAPP_URL]
    assert "ABC123" not in greeting.text
    assert await count(db, DbUser) == 0 and await count(db, Game) == 0


async def test_start_without_an_https_address_still_answers_without_a_button(deps):
    http_only = deps.settings.model_copy(update={"webapp_public_url": "http://localhost:5173"})

    session = await feed(
        BotDeps(http_only, deps.sessionmaker, deps.catalog, None),
        message_update("/start", CREATOR_TG_ID),
    )

    [greeting] = session.sent()
    assert greeting.reply_markup is None and greeting.text


async def test_newgame_offers_the_adventures_to_a_creator(deps):
    session = await feed(deps, message_update("/newgame", CREATOR_TG_ID))

    [offer] = session.sent()
    assert [b.text for b in keyboard_of(offer)] == [deps.catalog["demo-crypt"].adventure.title]


async def test_newgame_refuses_everyone_outside_the_list_before_creating_anyone(deps, db):
    session = await feed(deps, message_update("/newgame", OUTSIDER_TG_ID))

    [refusal] = session.sent()
    assert refusal.reply_markup is None
    assert await count(db, DbUser) == 0


async def test_pressing_a_button_creates_the_game_and_sends_the_invite(deps, db):
    [button] = adventure_buttons(deps.catalog)

    session = await feed(deps, callback_update(button.callback_data, CREATOR_TG_ID))

    async with db() as session_db:
        game = await session_db.scalar(select(Game))
    [invite] = session.sent()
    [open_button] = keyboard_of(invite)
    assert game.phase == GamePhase.LOBBY
    assert open_button.url == f"{INVITE}{game.code}"
    assert len(session.answers()) == 1  # кнопка перестала «крутиться»


async def test_a_forged_button_from_a_user_outside_the_list_creates_nothing(deps, db):
    [button] = adventure_buttons(deps.catalog)

    session = await feed(deps, callback_update(button.callback_data, OUTSIDER_TG_ID))

    assert await count(db, DbUser) == 0 and await count(db, Game) == 0
    assert session.sent() == []
    [answer] = session.answers()
    assert answer.show_alert is True  # отказ виден нажавшему, а чат не засоряется


@pytest.mark.parametrize("name", ["unknown-adventure", "x" * 300])
async def test_a_forged_button_naming_no_adventure_creates_nothing_and_is_answered(deps, db, name):
    session = await feed(deps, callback_update(NEWGAME_PREFIX + name, CREATOR_TG_ID))

    assert await count(db, Game) == 0
    assert session.sent() == []
    [answer] = session.answers()
    assert answer.show_alert is True


@pytest.mark.parametrize("data", [None, "garbage"])
async def test_a_callback_that_is_not_ours_is_ignored(deps, db, data):
    session = await feed(deps, callback_update(data, CREATOR_TG_ID))

    assert await count(db, DbUser) == 0 and await count(db, Game) == 0
    assert session.requests == []


# --- уведомления ---


def test_notification_texts():
    assert collecting_text(45) == "Хост собирает ходы — осталось 45 с"
    assert collecting_text(-3) == "Хост собирает ходы — осталось 0 с"
    assert combat_turn_text() == "Твой ход в бою"
    assert game_continues_text() == "Игра продолжается"


async def add_user(db, *, tg_id: int | None, dev_login: str | None = None) -> str:
    async with db() as session:
        user = DbUser(tg_id=tg_id, dev_login=dev_login, name="Игрок")
        session.add(user)
        await session.commit()
        return user.id


async def test_notify_sends_exactly_one_message_to_the_telegram_chat_of_the_user(db):
    user_id = await add_user(db, tg_id=555)
    bot, session = make_test_bot()

    await Notifier(db, bot).notify(user_id, game_continues_text())

    [sent] = session.requests
    assert isinstance(sent, SendMessage)
    assert (sent.chat_id, sent.text) == (555, "Игра продолжается")


async def test_notify_without_a_bot_does_nothing_and_does_not_even_look_in_the_database():
    def forbidden_sessionmaker() -> None:
        raise AssertionError("без бота база не нужна")

    await Notifier(forbidden_sessionmaker).notify("any-user", "текст")


async def test_notify_skips_users_without_a_telegram_id_and_unknown_users(db, caplog):
    dev_user = await add_user(db, tg_id=None, dev_login="dev-1")
    bot, session = make_test_bot()
    notifier = Notifier(db, bot)

    with caplog.at_level(logging.DEBUG):
        await notifier.notify(dev_user, "текст")
        await notifier.notify("no-such-user", "текст")

    assert session.requests == []
    # Тихо: dev-игрок без Telegram — не сбой, и отправку для него даже не пытались собрать.
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    "error",
    [
        lambda method: TelegramForbiddenError(method, "Forbidden: bot was blocked by the user"),
        lambda method: TelegramNetworkError(method, "Request timeout error"),
        lambda method: RuntimeError("непредвиденный сбой"),
    ],
    ids=["бот заблокирован", "сеть", "непредвиденное"],
)
async def test_a_failed_send_is_logged_and_does_not_stop_the_caller(db, caplog, error):
    user_id = await add_user(db, tg_id=555)
    bot, _session = make_test_bot(FailingSendSession(error))

    with caplog.at_level(logging.WARNING, logger="dnd_server.bot.notify"):
        await Notifier(db, bot).notify(user_id, "текст")  # не бросает

    assert user_id in caplog.text  # по записи видно, кому не дошло


# --- запуск в lifespan ---


async def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def with_token(settings: Settings) -> Settings:
    return settings.model_copy(update={"telegram_bot_token": SecretStr(BOT_TOKEN)})


@pytest.fixture
def polling_calls(monkeypatch) -> list[dict[str, Any]]:
    """Запоминает аргументы каждого запуска polling, не меняя его работу."""
    calls: list[dict[str, Any]] = []
    real = Dispatcher.start_polling

    async def spy(self, *bots, **options):
        calls.append(options)
        return await real(self, *bots, **options)

    monkeypatch.setattr(Dispatcher, "start_polling", spy)
    return calls


@pytest.mark.real_make_bot  # проверяется настоящая `make_bot`: без токена она не строит `Bot`
async def test_without_a_token_the_bot_is_off_and_notify_does_nothing(
    settings, monkeypatch, polling_calls
):
    def no_bot_expected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("без токена Bot создавать нельзя")

    monkeypatch.setattr(bot_module, "Bot", no_bot_expected)
    app = create_app(settings)
    tasks_before = asyncio.all_tasks()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        await app.state.notifier.notify("any-user", "текст")  # не падает и ничего не шлёт
        assert asyncio.all_tasks() == tasks_before  # фоновых задач нет

    assert polling_calls == []
    await app.state.engine.dispose()


async def test_the_app_has_a_notifier_even_before_it_starts(settings):
    app = create_app(settings)

    await app.state.notifier.notify("any-user", "текст")  # заглушка: не падает

    await app.state.engine.dispose()


async def test_with_a_token_polling_runs_while_the_app_runs_and_stops_with_it(
    settings, monkeypatch, polling_calls
):
    bot, session = make_test_bot()
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: bot)
    app = create_app(with_token(settings))
    tasks_before = asyncio.all_tasks()

    async with app.router.lifespan_context(app):
        await wait_for(lambda: any(isinstance(r, GetUpdates) for r in session.requests))
        assert app.state.notifier.bot is bot

    assert session.closed
    assert asyncio.all_tasks() == tasks_before  # задача polling завершилась
    # Сигналами процесса управляет uvicorn: aiogram не должен перехватывать SIGINT и SIGTERM.
    assert [call["handle_signals"] for call in polling_calls] == [False]
    await app.state.engine.dispose()


async def test_a_real_server_starts_the_bot_and_stops_it_on_shutdown(settings, monkeypatch):
    # Тот же путь, что у `uv run dnd-server`: uvicorn сам запускает и останавливает lifespan.
    bot, session = make_test_bot()
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: bot)
    app = create_app(with_token(settings))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_config=None)
    )
    tasks_before = asyncio.all_tasks()

    serving = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started)
    await wait_for(lambda: any(isinstance(r, GetUpdates) for r in session.requests))
    server.should_exit = True
    await asyncio.wait_for(serving, timeout=10)

    assert session.closed
    assert asyncio.all_tasks() == tasks_before
    await app.state.engine.dispose()


async def test_an_app_stopped_right_after_the_start_leaves_no_tasks_behind(settings, monkeypatch):
    bot, _session = make_test_bot()
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: bot)
    app = create_app(with_token(settings))
    tasks_before = asyncio.all_tasks()

    async with app.router.lifespan_context(app):
        pass

    assert asyncio.all_tasks() == tasks_before
    await app.state.engine.dispose()


@pytest.mark.real_make_bot  # неверный формат распознаёт настоящая `make_bot`, а не заглушка
async def test_a_token_in_the_wrong_format_switches_the_bot_off_and_the_server_starts(
    settings, caplog
):
    bad = settings.model_copy(update={"telegram_bot_token": SecretStr("no-colon-here")})
    app = create_app(bad)

    with caplog.at_level(logging.ERROR):
        async with app.router.lifespan_context(app):
            assert app.state.notifier.bot is None

    assert "TELEGRAM_BOT_TOKEN" in caplog.text
    assert "no-colon-here" not in caplog.text
    await app.state.engine.dispose()


async def test_a_polling_crash_is_logged_and_does_not_stop_the_server(
    settings, monkeypatch, caplog
):
    class RevokedTokenSession(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetMe):
                raise RuntimeError("токен отозван")
            return await super().make_request(bot, method, timeout)

    session = RevokedTokenSession()
    monkeypatch.setattr(bot_module, "make_bot", lambda _s: Bot(BOT_TOKEN, session=session))
    app = create_app(with_token(settings))

    with caplog.at_level(logging.ERROR, logger="dnd_server.bot.bot"):
        async with app.router.lifespan_context(app):
            await wait_for(lambda: "Telegram-бот остановился" in caplog.text)

    assert BOT_TOKEN not in caplog.text
    await app.state.engine.dispose()


# --- токен бота не попадает в журналы ---


async def test_the_bot_token_never_reaches_the_logs_even_when_aiohttp_prints_the_request_url(
    settings, monkeypatch, caplog
):
    # aiohttp пишет адрес запроса в текст ошибки (`url='https://api.telegram.org/bot<токен>/…'`),
    # aiogram оборачивает её в `TelegramNetworkError` и пишет в журнал при каждом сбое опроса.
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    http = FakeAiohttp(aiohttp_error_with_url(url))
    session = AiohttpSession()

    async def create_session() -> FakeAiohttp:
        return http

    monkeypatch.setattr(session, "create_session", create_session)
    monkeypatch.setattr(bot_module, "make_bot", lambda _s: Bot(BOT_TOKEN, session=session))
    app = create_app(with_token(settings))

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            await wait_for(lambda: "Failed to fetch updates" in caplog.text)

    assert "Failed to fetch updates" in caplog.text  # ошибка действительно записана
    assert BOT_TOKEN not in caplog.text
    assert BOT_TOKEN.partition(":")[2] not in caplog.text
    await app.state.engine.dispose()


async def test_a_failed_notification_does_not_leak_the_token_either(
    settings, db, monkeypatch, caplog
):
    user_id = await add_user(db, tg_id=555)

    def leaky(method: TelegramMethod[Any]) -> Exception:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        return TelegramNetworkError(method, f"ClientResponseError: url='{url}'")

    bot, _session = make_test_bot(FailingSendSession(leaky))
    monkeypatch.setattr(bot_module, "make_bot", lambda _s: bot)
    app = create_app(with_token(settings))

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            await Notifier(db, bot).notify(user_id, "текст")

    assert user_id in caplog.text
    assert BOT_TOKEN not in caplog.text
    await app.state.engine.dispose()


def test_the_log_filter_works_only_while_the_bot_runs():
    names = bot_module.PROTECTED_LOGGERS
    before = [list(logging.getLogger(name).filters) for name in names]

    with bot_module.hide_token_in_logs(BOT_TOKEN):
        assert all(len(logging.getLogger(name).filters) == 1 for name in names)

    assert [list(logging.getLogger(name).filters) for name in names] == before


def log_record(message: str, *args: object, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc else None
    return logging.LogRecord("t", logging.ERROR, __file__, 1, message, args, exc_info)


@pytest.mark.parametrize(
    "token_text",
    [BOT_TOKEN, BOT_TOKEN.partition(":")[2]],
    ids=["токен целиком", "только секретная часть"],
)
def test_the_token_filter_hides_the_token_in_the_message_its_arguments_and_the_traceback(
    token_text,
):
    token_filter = bot_module.TokenFilter(BOT_TOKEN)
    try:
        raise RuntimeError(f"url=/bot{token_text}/getMe")
    except RuntimeError as error:
        failure = error

    in_message = log_record(f"адрес /bot{token_text}/x")
    in_args = log_record("адрес %s", f"/bot{token_text}/x")
    in_trace = log_record("сбой", exc=failure)
    for record in (in_message, in_args, in_trace):
        assert token_filter.filter(record) is True  # запись не отбрасывается, а чистится
        assert token_text not in logging.Formatter().format(record)
    assert "***" in logging.Formatter().format(in_trace)


def test_the_token_filter_leaves_records_without_the_token_alone():
    token_filter = bot_module.TokenFilter(BOT_TOKEN)
    try:
        raise RuntimeError("обычная ошибка")
    except RuntimeError as error:
        failure = error
    record = log_record("процент %d%%", 5, exc=failure)

    token_filter.filter(record)

    assert (record.msg, record.args) == ("процент %d%%", (5,))
    assert record.exc_info is not None and record.exc_text is None
