"""Бот и сервер вместе (план B6.6): команда из Telegram доходит до игры, которую видит REST.

Тесты бота (`tests/bot/`) кормят обработчики обновлениями напрямую. Здесь обновления приходят так,
как у владельца: настоящий опрос Telegram (`getUpdates`) внутри `lifespan` сервера, настоящие
обработчики и база, а вместо сети — сессия `Bot`, которая отдаёт заготовленные обновления и
записывает ответы бота. После этого игра, созданная командой, видна по REST, а друг входит в неё
по коду из ссылки-приглашения.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, GetUpdates, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from dnd_server.bot import bot as bot_module
from dnd_server.bot.notify import collecting_text

BOT_TOKEN = "123456789:AAF-flows-bot-token-0123456789_abcdef"
CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
OUTSIDER_TG_ID = 701
INVITE_PREFIX = "https://t.me/dnd_flows_bot?startapp="


class PollingSession(BaseSession):
    """Сессия `Bot` без сети: отдаёт обновления через `getUpdates` и записывает ответы бота."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[Update] = []
        self.sent: list[SendMessage] = []
        self.answers: list[AnswerCallbackQuery] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        if isinstance(method, GetMe):
            return User(id=123456789, is_bot=True, first_name="Бот", username="dnd_flows_bot")
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.01)  # длинный опрос: без паузы цикл не отдавал бы управление
            updates, self.pending = self.pending, []
            return updates
        if isinstance(method, SendMessage):
            self.sent.append(method)
            chat = Chat(id=int(method.chat_id), type="private")
            return Message(
                message_id=len(self.sent), date=datetime.now(UTC), chat=chat, text=method.text
            )
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
        return True


def command_update(number: int, text: str, tg_id: int, name: str) -> Update:
    command = text.split()[0]
    return Update(
        update_id=number,
        message=Message(
            message_id=number,
            date=datetime.now(UTC),
            chat=Chat(id=tg_id, type="private"),
            from_user=User(id=tg_id, is_bot=False, first_name=name),
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(command))],
        ),
    )


def button_update(number: int, data: str, tg_id: int, name: str) -> Update:
    shown = Message(
        message_id=100 + number,
        date=datetime.now(UTC),
        chat=Chat(id=tg_id, type="private"),
        from_user=User(id=123456789, is_bot=True, first_name="Бот"),
        text="Выберите приключение",
    )
    callback = CallbackQuery(
        id=f"callback-{number}",
        from_user=User(id=tg_id, is_bot=False, first_name=name),
        chat_instance="chat",
        message=shown,
        data=data,
    )
    return Update(update_id=number, callback_query=callback)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


async def test_commands_through_the_polling_loop_make_a_game_that_rest_sees_and_a_friend_joins(
    monkeypatch, serve
):
    session = PollingSession()
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: Bot(BOT_TOKEN, session=session))

    async with serve() as stand:
        # /newgame от создателя: бот предлагает приключение, нажатие кнопки создаёт игру.
        session.pending.append(command_update(1, "/newgame", CREATOR_TG_ID, "Создатель"))
        await until(lambda: len(session.sent) == 1)
        [button] = [b for row in session.sent[0].reply_markup.inline_keyboard for b in row]
        session.pending.append(button_update(2, button.callback_data, CREATOR_TG_ID, "Создатель"))
        await until(lambda: len(session.sent) == 2)
        [open_button] = [b for row in session.sent[1].reply_markup.inline_keyboard for b in row]
        assert open_button.url.startswith(INVITE_PREFIX)
        code = open_button.url.removeprefix(INVITE_PREFIX)

        creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
        [game] = (await stand.call(creator, "GET", "/api/games")).json()
        assert (game["code"], game["phase"], game["adventure_id"]) == (code, "lobby", "demo-crypt")
        assert [p["name"] for p in game["players"]] == ["Создатель"]
        friend = await stand.dev_player(2)
        assert (await stand.join(friend, code))["id"] == game["id"]

        # Посторонний не из списка: бот отказывает словами и не заводит игру.
        session.pending.append(command_update(3, "/newgame", OUTSIDER_TG_ID, "Посторонний"))
        await until(lambda: len(session.sent) == 3)
        assert session.sent[2].reply_markup is None
        outsider = await stand.telegram_player(OUTSIDER_TG_ID, "Посторонний")
        assert (await stand.call(outsider, "GET", "/api/games")).json() == []
        assert len((await stand.call(creator, "GET", "/api/games")).json()) == 1
        stand.assert_clean()

    assert session.closed  # остановка сервера закрывает и бота


async def test_a_notification_goes_to_the_telegram_chat_of_a_user_who_logged_in_through_rest(
    monkeypatch, serve
):
    session = PollingSession()
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: Bot(BOT_TOKEN, session=session))

    async with serve() as stand:
        telegram_user = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
        dev_user = await stand.dev_player(1)
        notifier = stand.app.state.notifier

        await notifier.notify(telegram_user.id, collecting_text(30))
        await notifier.notify(dev_user.id, collecting_text(30))  # у dev-входа нет Telegram-id
        await notifier.notify("no-such-user", collecting_text(30))

        assert [(m.chat_id, m.text) for m in session.sent] == [
            (CREATOR_TG_ID, "Хост собирает ходы — осталось 30 с")
        ]
        stand.assert_clean()
