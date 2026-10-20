"""Команды бота (план B6.6): `/start`, `/newgame` и кнопки выбора приключения.

Логика отделена от сети: `invite_link`, `newgame_keyboard`, `adventure_id_from_callback`,
`alert_text` и `open_lobby` ничего не отправляют (последняя ходит только в базу), поэтому тесты
вызывают их напрямую. Обработчики aiogram ниже тонкие: достают данные из сообщения, зовут эти
функции и отвечают в чат.

Создавать игры может только тот, чей Telegram-id есть в `GAME_CREATOR_TG_IDS` (решение владельца
B6). Это проверяется до показа списка приключений и ещё раз при нажатии кнопки: кнопка видна всем
в чате и может быть переслана, а `callback_data` приходит от клиента и ничему не доверяется.
Игру создаёт `games.service.create_game`, поэтому правила те же, что у REST: каталог, копия
приключения, длительности отсчётов, проверка провайдера.
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_server.adventures import Catalog
from dnd_server.auth.deps import find_or_create_user, is_game_creator_tg_id
from dnd_server.config import Settings
from dnd_server.games.schemas import CreateGameIn, GameOut
from dnd_server.games.service import GameError, create_game
from dnd_server.llm.internal_api import TurnTokens

logger = logging.getLogger(__name__)

# Префикс `callback_data` кнопок выбора приключения. Весь `callback_data` Telegram ограничивает
# 64 байтами, поэтому префикс короткий, а кнопка приключения с длинным id не создаётся.
NEWGAME_PREFIX = "ng:"
MAX_CALLBACK_DATA_BYTES = 64
# Предел Bot API на текст `answerCallbackQuery`: ответ длиннее Telegram отвергает целиком, и кнопка
# остаётся «крутиться» без объяснения.
MAX_ALERT_CHARS = 200

OPEN_GAME_BUTTON = "Открыть игру"
START_TEXT = (
    "Я нейро-мастер настольной игры по мотивам D&D 5e: играет компания в одной комнате, "
    "а числа и броски считает код. Нажмите кнопку, чтобы открыть игру. Войти в чужую игру "
    "можно по ссылке-приглашению от хоста."
)
NO_HTTPS_TEXT = "Кнопку приложения показать нельзя: Telegram открывает его только по https-адресу."
NOT_CREATOR_TEXT = (
    "Создавать игры могут только игроки из списка владельца сервера. "
    "Чтобы сыграть, попросите у хоста ссылку-приглашение."
)
NO_ADVENTURES_TEXT = "На сервере нет приключений, на которых можно начать игру."
CHOOSE_ADVENTURE_TEXT = "Выберите приключение для новой игры:"
STALE_BUTTON_TEXT = "Эта кнопка устарела: приключения больше нет. Вызовите /newgame ещё раз."


@dataclass(frozen=True)
class BotDeps:
    """Всё, что нужно обработчикам; собирается при старте приложения, когда каталог уже загружен."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    catalog: Catalog
    turn_tokens: TurnTokens | None


class LobbyReply(NamedTuple):
    """Ответ создателю: текст, создана ли игра и ссылка-приглашение (если её можно собрать)."""

    text: str
    created: bool
    link: str | None = None


# --- чистые части ---


def invite_link(settings: Settings, code: str) -> str | None:
    """Ссылка на Mini App с кодом игры; `None`, пока не задано имя бота.

    Без имени ссылку собрать не из чего: подставленная заглушка вела бы в чужого бота.
    """
    if settings.telegram_bot_username is None:
        return None
    return f"https://t.me/{settings.telegram_bot_username}?startapp={code}"


def webapp_keyboard(url: str) -> InlineKeyboardMarkup | None:
    """Кнопка открытия Mini App; `None`, если адрес не https (Telegram такую кнопку отвергнет)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return None
    button = InlineKeyboardButton(text=OPEN_GAME_BUTTON, web_app=WebAppInfo(url=url))
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def newgame_keyboard(catalog: Catalog) -> InlineKeyboardMarkup | None:
    """Кнопка на каждое приключение без блокирующих ошибок; `None`, если кнопок нет."""
    rows = []
    for entry in catalog.values():
        if entry.report.blocking:
            continue
        data = NEWGAME_PREFIX + entry.adventure.id
        if len(data.encode()) > MAX_CALLBACK_DATA_BYTES:
            logger.warning(
                "Приключению %r не хватило места в кнопке: id слишком длинный.", entry.adventure.id
            )
            continue
        rows.append([InlineKeyboardButton(text=entry.adventure.title, callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def adventure_id_from_callback(data: str | None, catalog: Catalog) -> str | None:
    """Id приключения из `callback_data` или `None`: данные не наши, id нет в каталоге, ошибки.

    Значение приходит от клиента, поэтому годится только id из каталога без блокирующих ошибок.
    """
    if data is None or not data.startswith(NEWGAME_PREFIX):
        return None
    entry = catalog.get(data.removeprefix(NEWGAME_PREFIX))
    if entry is None or entry.report.blocking:
        return None
    return entry.adventure.id


def lobby_text(game: GameOut, link: str | None) -> str:
    created = f"Игра «{game.adventure_title}» создана. Код приглашения: {game.code}."
    if link is not None:
        return f"{created}\nСсылка для друзей: {link}"
    return (
        f"{created}\nСсылку собрать не из чего: на сервере не задан TELEGRAM_BOT_USERNAME. "
        "Откройте приложение командой /start и передайте друзьям код."
    )


def alert_text(text: str) -> str:
    """Текст для окна-уведомления: длиннее `MAX_ALERT_CHARS` обрезается и заканчивается «…»."""
    if len(text) <= MAX_ALERT_CHARS:
        return text
    return text[: MAX_ALERT_CHARS - 1] + "…"


# --- создание игры ---


async def open_lobby(
    session: AsyncSession, deps: BotDeps, *, tg_id: int, name: str, adventure_id: str
) -> LobbyReply:
    """Игра в лобби от пользователя Telegram `tg_id`; отказ возвращается текстом, а не исключением.

    Не из списка создателей — отказ до заведения пользователя: чужой человек не попадает в базу.
    Остальное решает `create_game`: нет приключения, ошибки в нём или неподходящий провайдер.
    """
    if not is_game_creator_tg_id(deps.settings, tg_id):
        return LobbyReply(NOT_CREATOR_TEXT, created=False)
    host = await find_or_create_user(session, name=name, tg_id=tg_id)
    try:
        game = await create_game(
            session,
            host,
            CreateGameIn(adventure_id=adventure_id),
            settings=deps.settings,
            catalog=deps.catalog,
            turn_tokens=deps.turn_tokens,
        )
    except GameError as refusal:
        return LobbyReply(refusal.message, created=False)
    link = invite_link(deps.settings, game.code)
    return LobbyReply(lobby_text(game, link), created=True, link=link)


# --- обработчики aiogram ---


async def on_start(message: Message, deps: BotDeps) -> None:
    # Аргумент `/start <что-то>` не читается: вход в игру идёт через `startapp` Mini App.
    keyboard = webapp_keyboard(deps.settings.webapp_public_url)
    text = START_TEXT if keyboard else f"{START_TEXT}\n\n{NO_HTTPS_TEXT}"
    await message.answer(text, reply_markup=keyboard)


async def on_newgame(message: Message, deps: BotDeps) -> None:
    sender = message.from_user
    if sender is None or not is_game_creator_tg_id(deps.settings, sender.id):
        await message.answer(NOT_CREATOR_TEXT)
        return
    keyboard = newgame_keyboard(deps.catalog)
    if keyboard is None:
        await message.answer(NO_ADVENTURES_TEXT)
        return
    await message.answer(CHOOSE_ADVENTURE_TEXT, reply_markup=keyboard)


async def on_adventure_chosen(callback: CallbackQuery, deps: BotDeps) -> None:
    adventure_id = adventure_id_from_callback(callback.data, deps.catalog)
    if adventure_id is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    async with deps.sessionmaker() as session:
        reply = await open_lobby(
            session,
            deps,
            tg_id=callback.from_user.id,
            name=callback.from_user.first_name,
            adventure_id=adventure_id,
        )
    if not reply.created:
        # Отказ показывается нажавшему, а не всему чату: кнопку видят и те, кому игры не создать.
        # В группе полный текст слать нельзя (в нём пути и настройки сервера), поэтому окно
        # урезается, а причина целиком остаётся в журнале владельца: в конце длинного текста как
        # раз причина.
        shown = alert_text(reply.text)
        if shown != reply.text:
            logger.warning("Отказ создать игру не поместился в окно Telegram: %s", reply.text)
        await callback.answer(shown, show_alert=True)
        return
    keyboard = None
    if reply.link is not None:
        button = InlineKeyboardButton(text=OPEN_GAME_BUTTON, url=reply.link)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[button]])
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await callback.bot.send_message(chat_id=chat_id, text=reply.text, reply_markup=keyboard)
    await callback.answer()


def build_router() -> Router:
    """Свой роутер на каждый диспетчер: роутер можно подключить только к одному родителю."""
    router = Router(name="dnd")
    router.message.register(on_start, CommandStart())
    router.message.register(on_newgame, Command("newgame"))
    router.callback_query.register(on_adventure_chosen, F.data.startswith(NEWGAME_PREFIX))
    return router
