"""Правила лобби (план B6.3): создание игры, вход по коду, готовность, старт.

Слой не знает про HTTP: он принимает сессию базы, пользователя и зависимости, возвращает `GameOut`
и отказывает исключениями `GameError` (`NotFound`, `Forbidden`, `Conflict`, `Invalid`). Как они
превращаются в коды ответов, решает `api.py`.

Изменение игры делается под блокировкой этой игры (`GameLocks`): под ней читаются строки, проверяется
правило (мест не больше шести, игра ещё в лобби, все готовы) и пишется результат вместе с коммитом.
Без блокировки два одновременных входа оба видели бы пять игроков и записали бы седьмого, а два
одновременных старта оба запустили бы мастера. Блокировка живёт в одном процессе: сервер так и
запускается (один процесс `uvicorn`, SQLite). В памяти того же процесса идёт и учёт пределов
(`ModelCalls`, `JoinAttempts`): при нескольких процессах каждый считал бы свою долю.

Чужая игра для пользователя не отличается от несуществующей (`NotFound`): отказ не говорит, что
игра есть. Сообщения об изменении лобби уходят после коммита; сбой рассылки изменение не откатывает.
"""

import asyncio
import math
import secrets
import string
import time
import weakref
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_server.__main__ import HOST, PORT
from dnd_server.adventures import Catalog, blocking_problems
from dnd_server.config import Settings
from dnd_server.db.models import Game, GamePhase, GamePlayer, User, new_id, utcnow
from dnd_server.games.hooks import Broadcaster, send_to_game
from dnd_server.games.schemas import CreateGameIn, GameOut, PlayerOut
from dnd_server.llm.factory import ConfigError, make_provider
from dnd_server.llm.internal_api import TurnTokens

# Не больше шести игроков в игре (спека §8): стол и отряд рассчитаны на шестерых.
MAX_PLAYERS = 6
# Код приглашения: 6 символов A-Z0-9, около двух миллиардов вариантов; выбирается через `secrets`.
CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 6
# Сколько раз подряд придумывают код, если он уже занят. Занятый код — редкость, а не норма.
CODE_ATTEMPTS = 5
# Предел платных вызовов модели из лобби на игрока в игре (решение владельца B6.security).
MAX_MODEL_CALLS = 20
# Предел неудачных попыток войти по коду на пользователя (решение владельца B6.security): после
# десяти отказов за десять минут новые попытки получают 429, пока старые не выйдут из окна.
MAX_FAILED_JOINS = 10
FAILED_JOIN_WINDOW_S = 600


class GameError(Exception):
    """Лобби отказало. `message` — текст игроку по-русски, `problems` — подробности создателю."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []


class NotFound(GameError):
    """Нет такого приключения, игры или кода, либо игра чужая."""


class Forbidden(GameError):
    """Игрок в игре, но это действие ему не доступно (старт — только хосту)."""


class Conflict(GameError):
    """Действие не подходит текущему состоянию игры: нет мест, игра идёт, не все готовы."""


class Invalid(GameError):
    """Запрос верен по форме, но так игру создать нельзя (приключение с ошибками, провайдер)."""


class TooManyRequests(GameError):
    """Слишком часто: у игрока уже идёт вызов модели или исчерпан предел попыток (ответ 429)."""


class GameCodeUnavailable(RuntimeError):
    """Свободный код приглашения не нашёлся за `CODE_ATTEMPTS` попыток: это сбой, а не отказ."""


class GameLocks:
    """По одной блокировке asyncio на игру. Блокировка живёт, пока её кто-то держит или ждёт.

    Ключ приходит от клиента (id игры в пути), поэтому записи не копятся: слабая ссылка убирает
    блокировку, как только ею не пользуются.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, game_id: str) -> asyncio.Lock:
        lock = self._locks.get(game_id)
        if lock is None:
            lock = self._locks[game_id] = asyncio.Lock()
        return lock


class ModelCalls:
    """Учёт платных вызовов модели из действий игрока в лобби: ключ — игра и игрок.

    Предел задаёт владелец: у игрока в игре одновременно не больше одного вызова и не больше
    `MAX_MODEL_CALLS` за лобби. Учёт живёт в памяти процесса, как `GameLocks`: перезапуск сервера
    обнуляет счётчики, но перезапускает его только владелец, а игрок так предел не обойдёт. Ключи
    берутся только из проверенного участия в игре, поэтому счётчики (по паре чисел на игрока игры)
    копятся лишь у участников настоящих игр и держатся до перезапуска.
    """

    def __init__(self) -> None:
        self._running: set[tuple[str, str]] = set()
        self._made: Counter[tuple[str, str]] = Counter()

    @contextmanager
    def running(self, game_id: str, user_id: str) -> Iterator[None]:
        """Занимает единственное место игрока под вызов; занято — `TooManyRequests`.

        Место занимается и освобождается синхронно, без ожидания между проверкой и занятием, поэтому
        два параллельных запроса не пройдут оба. Освобождается и при ошибке, и при отмене запроса."""
        key = (game_id, user_id)
        if key in self._running:
            raise TooManyRequests(
                "Нейронка ещё отвечает на ваш прошлый запрос: дождитесь ответа и повторите."
            )
        self._running.add(key)
        try:
            yield
        finally:
            self._running.discard(key)

    def charge(self, game_id: str, user_id: str) -> int | None:
        """Учитывает вызов: его номер (1…`MAX_MODEL_CALLS`) или `None`, если предел исчерпан."""
        key = (game_id, user_id)
        if self._made[key] >= MAX_MODEL_CALLS:
            return None
        self._made[key] += 1
        return self._made[key]


class JoinAttempts:
    """Неудачные попытки войти по коду за скользящее окно `FAILED_JOIN_WINDOW_S`, по пользователям.

    Код из 36^6 вариантов перебирают по ответам «нет такой игры» и «игра уже началась». Учёт в
    памяти процесса, как у `GameLocks`: перезапуск сервера сбрасывает его, но перезапускает сервер
    только владелец. Часы подставляются в тестах. Ключи — пользователи с настоящей сессией, и
    запись исчезает, когда её попытки вышли из окна.

    Проверка и запись неудачи вызываются подряд без ожидания между ними (так их вызывает
    `join_game`), поэтому пачка параллельных запросов не получит больше `MAX_FAILED_JOINS` попыток.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def check(self, user_id: str) -> None:
        """Отказывает (`TooManyRequests`), если неудач в окне уже `MAX_FAILED_JOINS`."""
        failures = self._in_window(user_id)
        if len(failures) >= MAX_FAILED_JOINS:
            minutes = math.ceil((failures[0] + FAILED_JOIN_WINDOW_S - self._clock()) / 60)
            raise TooManyRequests(
                f"Слишком много неверных кодов подряд. Подождите {minutes} мин и попробуйте снова."
            )

    def record_failure(self, user_id: str) -> None:
        failures = self._in_window(user_id)
        failures.append(self._clock())
        self._failures[user_id] = failures

    def _in_window(self, user_id: str) -> deque[float]:
        """Времена неудач пользователя, не старше окна (граница окна уже вне его)."""
        failures = self._failures.get(user_id, deque())
        cutoff = self._clock() - FAILED_JOIN_WINDOW_S
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(user_id, None)
        return failures


def new_game_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def lobby_event(game: GameOut) -> dict[str, Any]:
    """Событие `lobby` для рассылки: полная сводка игры, чтобы клиент заменил ею своё лобби."""
    return {"type": "lobby", "game": game.model_dump(mode="json")}


# --- чтение ---


async def _players(session: AsyncSession, game_id: str) -> list[GamePlayer]:
    rows = await session.scalars(
        select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.joined_order)
    )
    return list(rows)


async def _game_outs(session: AsyncSession, games: Sequence[Game]) -> list[GameOut]:
    """Сводки игр одним запросом игроков на всех."""
    players_by_game: dict[str, list[PlayerOut]] = defaultdict(list)
    if games:
        rows = await session.execute(
            select(GamePlayer, User.name)
            .join(User, User.id == GamePlayer.user_id)
            .where(GamePlayer.game_id.in_([game.id for game in games]))
            .order_by(GamePlayer.joined_order)
        )
        for player, name in rows:
            players_by_game[player.game_id].append(
                PlayerOut(
                    user_id=player.user_id,
                    name=name,
                    is_host=player.is_host,
                    ready=player.ready,
                    has_character=player.character_id is not None,
                )
            )
    return [
        GameOut(
            id=game.id,
            code=game.code,
            adventure_id=game.adventure_id,
            # Название берётся из копии игры, а не из каталога: папку могли уже убрать.
            adventure_title=game.adventure_data["title"],
            phase=game.phase,
            turn_phase=game.turn_phase,
            provider=game.provider,
            model=game.model,
            collect_seconds=game.collect_seconds,
            hurry_seconds=game.hurry_seconds,
            camp_seconds=game.camp_seconds,
            players=players_by_game[game.id],
        )
        for game in games
    ]


async def _membership(session: AsyncSession, game_id: str, user: User) -> tuple[Game, GamePlayer]:
    """Игра и строка игрока в ней; чужая и несуществующая игры отвечают одинаково."""
    row = (
        await session.execute(
            select(Game, GamePlayer)
            .join(GamePlayer, GamePlayer.game_id == Game.id)
            .where(Game.id == game_id, GamePlayer.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFound("Игра не найдена.")
    game, player = row
    return game, player


async def get_game(session: AsyncSession, user: User, game_id: str) -> GameOut:
    game, _player = await _membership(session, game_id, user)
    return (await _game_outs(session, [game]))[0]


async def list_games(session: AsyncSession, user: User) -> list[GameOut]:
    """Игры, где пользователь играет или ведёт, новые первыми."""
    games = await session.scalars(
        select(Game)
        .join(GamePlayer, GamePlayer.game_id == Game.id)
        .where(GamePlayer.user_id == user.id)
        .order_by(Game.created_at.desc())
    )
    return await _game_outs(session, list(games))


# --- создание ---


def check_provider(
    settings: Settings, provider: str | None, model: str | None, turn_tokens: TurnTokens | None
) -> None:
    """Отказывает (`Invalid`), если провайдер игры (переопределение хоста или значение env) не собрать.

    Фабрика нужна только для проверки: игре экземпляр не хранится. Мастер (B7) собирает его сам,
    с id персонажей, которых при создании игры ещё нет. `claude_code_cli` фабрика собирает лишь
    в dev; адрес моста для него тот же, по которому слушает сервер.
    """
    try:
        make_provider(
            settings,
            provider=provider,
            model=model,
            pc_ids=(),
            turn_tokens=turn_tokens,
            internal_url=f"http://{HOST}:{PORT}",
        )
    except ConfigError as error:
        raise Invalid(f"Провайдер мастера не подходит: {error}.") from None


async def _free_game_code(session: AsyncSession) -> str:
    for _attempt in range(CODE_ATTEMPTS):
        code = new_game_code()
        if await session.scalar(select(Game.id).where(Game.code == code)) is None:
            return code
    raise GameCodeUnavailable(f"не нашёлся свободный код игры за {CODE_ATTEMPTS} попыток")


async def create_game(
    session: AsyncSession,
    host: User,
    body: CreateGameIn,
    *,
    settings: Settings,
    catalog: Catalog,
    turn_tokens: TurnTokens | None,
) -> GameOut:
    """Игра в лобби с хостом `host`. Приключение копируется в игру целиком (решение владельца)."""
    entry = catalog.get(body.adventure_id)
    if entry is None:
        raise NotFound("Приключение не найдено: возможно, его убрали с сервера.")
    if entry.report.blocking:
        raise Invalid(
            "Это приключение нельзя использовать: в нём есть блокирующие ошибки.",
            blocking_problems(entry.report),
        )
    check_provider(settings, body.provider, body.model, turn_tokens)
    game = Game(
        id=new_id(),
        code=await _free_game_code(session),
        adventure_id=entry.adventure.id,
        host_user_id=host.id,
        adventure_data=entry.adventure.model_dump(mode="json"),
        provider=body.provider,
        model=body.model,
        # Длительности проверены схемой (не меньше 15), поэтому `or` берёт env только вместо `None`.
        collect_seconds=body.collect_seconds or settings.collect_seconds,
        hurry_seconds=body.hurry_seconds or settings.hurry_seconds,
        camp_seconds=body.camp_seconds or settings.camp_seconds,
    )
    session.add_all(
        [game, GamePlayer(game_id=game.id, user_id=host.id, is_host=True, joined_order=1)]
    )
    await session.commit()
    return (await _game_outs(session, [game]))[0]


# --- лобби ---


def _join_refusal(game: Game, players: Sequence[GamePlayer]) -> GameError | None:
    """Почему чужак не может войти в игру сейчас; `None` — может."""
    if game.phase != GamePhase.LOBBY:
        return Conflict("Игра уже началась: войти можно только до старта.")
    if len(players) >= MAX_PLAYERS:
        return Conflict(f"В игре уже {MAX_PLAYERS} игроков: мест нет.")
    return None


async def join_game(
    session: AsyncSession,
    user: User,
    code: str,
    *,
    locks: GameLocks,
    broadcaster: Broadcaster,
    attempts: JoinAttempts,
) -> GameOut:
    """Вход по коду. Состоящий в игре входит «повторно» без изменений в любой фазе игры.

    Попыткой считается каждый отказ чужаку: нет такой игры, игра началась, мест нет. Участник,
    вернувшийся в свою игру, и удачный вход не считаются и от блокировки не зависят. Заблокированному
    чужаку отказ 429 приходит и там, где вход удался бы, и там, где он получил бы 404 или 409: ответ
    не говорит, есть ли такая игра. Код неверного вида (422) до сюда не доходит и попыткой не
    считается: он ничего не угадывает и не обращается к базе."""
    game_id = await session.scalar(select(Game.id).where(Game.code == code))
    if game_id is None:
        attempts.check(user.id)
        attempts.record_failure(user.id)
        raise NotFound("Игра с таким кодом не найдена.")
    async with locks.lock(game_id):
        game = await session.get(Game, game_id)
        players = await _players(session, game_id)
        if any(player.user_id == user.id for player in players):
            return (await _game_outs(session, [game]))[0]
        attempts.check(user.id)
        refusal = _join_refusal(game, players)
        if refusal is not None:
            attempts.record_failure(user.id)
            raise refusal
        session.add(
            GamePlayer(
                game_id=game_id,
                user_id=user.id,
                joined_order=max(player.joined_order for player in players) + 1,
            )
        )
        await session.commit()
        return await _announce(session, game, broadcaster)


async def set_ready(
    session: AsyncSession,
    user: User,
    game_id: str,
    ready: bool,
    *,
    locks: GameLocks,
    broadcaster: Broadcaster,
) -> GameOut:
    """Отметка «готов». Поставить её можно только с созданным персонажем, снять — всегда."""
    async with locks.lock(game_id):
        game, player = await _membership(session, game_id, user)
        if game.phase != GamePhase.LOBBY:
            raise Conflict("Игра уже началась: готовность менять нельзя.")
        if ready and player.character_id is None:
            raise Conflict("Сначала создайте персонажа.")
        player.ready = ready
        await session.commit()
        return await _announce(session, game, broadcaster)


async def start_game(
    session: AsyncSession, user: User, game_id: str, *, locks: GameLocks, broadcaster: Broadcaster
) -> GameOut:
    """Старт: игру ведёт хост, все игроки с персонажами и готовы. Игра переходит в `weaving`.

    Хост — тоже игрок, поэтому игроков всегда не меньше одного. Мастера запускает вызывающий,
    после ответа (`GameHooks.on_game_started`).
    """
    async with locks.lock(game_id):
        game, player = await _membership(session, game_id, user)
        if not player.is_host:
            raise Forbidden("Начать игру может только хост.")
        if game.phase != GamePhase.LOBBY:
            raise Conflict("Игра уже началась.")
        players = await _players(session, game_id)
        if not all(p.ready and p.character_id is not None for p in players):
            raise Conflict(
                "Не все игроки готовы: у каждого должен быть персонаж и отметка «Готов»."
            )
        game.phase = GamePhase.WEAVING
        game.started_at = utcnow()
        await session.commit()
        return await _announce(session, game, broadcaster)


async def _announce(session: AsyncSession, game: Game, broadcaster: Broadcaster) -> GameOut:
    """Сводка игры после коммита и её рассылка; вызывается под блокировкой игры, чтобы события
    лобби уходили в том порядке, в каком менялась игра."""
    out = (await _game_outs(session, [game]))[0]
    await send_to_game(broadcaster, game.id, lobby_event(out))
    return out
