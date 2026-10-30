"""Летопись мастера: фоновое сжатие записей в сводку.

Мастер в конце каждого хода дописывает в летопись (`GameState.chronicle.entries`) одну-две строки.
Когда записей набирается `CHRONICLE_COMPRESS_AT`, после коммита хода запускается фоновая задача
(`compress_chronicle`): отдельный дешёвый вызов `complete_json(purpose="chronicle", cheap=True)`
сливает прежнюю сводку и записи в новую сводку, и версия состояния с этой сводкой и пустыми
записями пишется следом за ходом. Хук ставит `dm.turn` после коммита любого хода, в том числе
«конец привала». Игроки летопись не видят: сводка нигде не рассылается и в ленту не попадает.

Задача не мешает игре:
- пока она ждёт модель, она не держит ни блокировку игры, ни соединение с базой: блокировка берётся
  коротко на чтение состояния и ещё раз коротко на запись;
- запись требует, чтобы версия состояния не ушла вперёд и мастер не разбирал ход: ход, зарезерви-
  рованный на версии N, при коммите требует, чтобы она оставалась N, и сжатие сломало бы его
  конфликтом версий. Иначе результат отбрасывается, а сжатие повторится после следующего хода уже
  на свежей летописи;
- на игру идёт не больше одного сжатия (`Compressions`), у вызова модели свой тайм-аут (у
  `complete_json` общего нет), игра, где идёт ход, до вызова вообще пропускается: платный вызов,
  результат которого нельзя записать, не нужен;
- любой сбой (тайм-аут, отказ провайдера, ответ не по схеме) оставляет летопись несжатой и пишется в
  журнал одной строкой без текстов летописи и без секретов; игру он не трогает;
- сбой не повторяется платным вызовом на каждом ходу: после `CHRONICLE_MAX_FAILURES` неудач подряд
  вызовы встают на паузу, пока записей не станет на `CHRONICLE_RETRY_AFTER` больше (`Compressions`).
  Пока записи копятся, контекст хода несёт только последние (`dm.context.MAX_CHRONICLE_ENTRIES`).

Ответ модели — недоверенный текст (в нём могут быть чужие слова из записей, управляющие знаки): он
приводится к одной печатной строке и ограничивается `SUMMARY_MAX_CHARS`, а записи и прежняя сводка
уходят в запрос только строками JSON, как данные.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.state import Chronicle, GameState
from dnd_server.db.models import Game, GameStateSnapshot, Turn, TurnPhase
from dnd_server.dm.text import json_line, printable_line
from dnd_server.games.service import GameLocks
from dnd_server.llm.base import JsonRequest, LLMProvider, ProviderError, printable
from dnd_server.prompts import load

logger = logging.getLogger(__name__)

CHRONICLE_COMPRESS_AT = 20  # записей, с которых летопись сжимается
CHRONICLE_MAX_FAILURES = 3  # неудач сжатия подряд, после которых платные вызовы встают на паузу
CHRONICLE_RETRY_AFTER = CHRONICLE_COMPRESS_AT  # на сколько записей больше пауза, потом одна проба
CHRONICLE_TIMEOUT_S = 60.0  # на вызов модели; дешёвая модель отвечает за секунды, это запас
SUMMARY_MAX_CHARS = 2000  # сводка идёт в контекст каждого хода, поэтому коротка
SUMMARY_ASKED_CHARS = 1500  # столько просим у модели, остальное — запас до жёсткого предела
MAX_REASON_CHARS = 300  # причина сбоя в журнале


class ChronicleOut(BaseModel):
    """Ответ летописца: одна сводка. Лишние поля запрещены, пустая и слишком длинная — отказ."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)


def compression_due(state: GameState) -> bool:
    return len(state.chronicle.entries) >= CHRONICLE_COMPRESS_AT


def compression_request(chronicle: Chronicle) -> JsonRequest:
    """Запрос на сжатие: роль летописца из пакета промптов; сводка и записи — строки JSON в одну
    строку каждая, как данные; предел длины сводки — подстановка из `SUMMARY_ASKED_CHARS`."""
    prompt = "\n".join(
        [
            "Слей прежнюю сводку и записи летописи в одну новую сводку "
            f"не длиннее {SUMMARY_ASKED_CHARS} знаков.",
            "Прежняя сводка (строка JSON; пустая строка — сводки ещё нет):",
            json_line(chronicle.summary),
            "Записи летописи от старых к новым (массив строк JSON):",
            json_line(chronicle.entries),
        ]
    )
    return JsonRequest(system=load("chronicler"), prompt=prompt, purpose="chronicle", cheap=True)


def clean_summary(text: str) -> str:
    """Сводка от модели как одна печатная строка не длиннее `SUMMARY_MAX_CHARS`.

    Все пробельные знаки, в том числе переводы строк и разрывы U+0085, U+2028, U+2029, сворачиваются
    в один пробел: чужая строка не нарисует заголовок в контексте мастера. Непечатаемое (ESC, BEL,
    смена направления письма, нулевая ширина, одинокие суррогаты) заменяется видимой записью
    `\\x1b`, `\\u202e`: спрятать в сводке нечего."""
    return printable_line(text, SUMMARY_MAX_CHARS)


@dataclass
class _Failures:
    """Неудачи сжатия одной игры подряд и число записей, с которого пауза кончается."""

    count: int = 0
    retry_at: int = 0


class Compressions:
    """Учёт сжатий летописи по играм: какие идут сейчас и как часто они не удаются.

    Одно сжатие на игру (вызов модели платный): место занимается и освобождается синхронно, поэтому
    две задачи не пройдут обе; освобождается при любом выходе: и при ошибке, и при остановке
    сервера.

    Пауза. Неудача сжатия оставляет записи несжатыми, и без неё каждый следующий ход снова платил бы
    за вызов, который опять не удастся (неверная дешёвая модель, сводка длиннее предела). После
    `CHRONICLE_MAX_FAILURES` неудач подряд вызовы встают на паузу, пока записей не станет на
    `CHRONICLE_RETRY_AFTER` больше, чем при последней неудаче; тогда идёт одна проба, и удача
    снимает паузу, а неудача ставит её снова. Неудача — отказ или тайм-аут модели, ответ не по
    схеме и неожиданная ошибка вызова. Ответ, который не удалось записать (ход уже идёт), неудачей
    не считается: модель исправна. Учёт живёт в памяти процесса, как блокировки игр: перезапуск
    сервера даёт новые попытки."""

    def __init__(self) -> None:
        self._running: set[str] = set()
        self._failures: dict[str, _Failures] = {}

    @contextmanager
    def claim(self, game_id: str) -> Iterator[bool]:
        """Занимает игру; `False` — сжатие уже идёт, занимать нечего (и освобождать тоже)."""
        if game_id in self._running:
            yield False
            return
        self._running.add(game_id)
        try:
            yield True
        finally:
            self._running.discard(game_id)

    def paused(self, game_id: str, entries: int) -> bool:
        """Платный вызов сейчас не нужен: неудач подряд набралось, а порога пробы ещё нет."""
        failures = self._failures.get(game_id)
        return (
            failures is not None
            and failures.count >= CHRONICLE_MAX_FAILURES
            and entries < failures.retry_at
        )

    def failed(self, game_id: str, entries: int) -> bool:
        """Записывает неудачу при `entries` записях; `True` — теперь игра на паузе."""
        failures = self._failures.setdefault(game_id, _Failures())
        failures.count += 1
        failures.retry_at = entries + CHRONICLE_RETRY_AFTER
        return failures.count >= CHRONICLE_MAX_FAILURES

    def succeeded(self, game_id: str) -> None:
        """Модель ответила: неудачи и пауза забываются."""
        self._failures.pop(game_id, None)


class _Unavailable(Exception):
    """Летописец не дал сводку: тайм-аут или отказ провайдера."""


class _Postponed(Exception):
    """Результат писать нельзя, сжатие повторится после следующего хода."""


@dataclass(frozen=True)
class _Seen:
    """Что видела задача перед вызовом модели: версия состояния и само состояние на ней. Снимок
    версии не меняется, поэтому, пока версия в базе прежняя, состояние для записи то же самое."""

    version: int
    state: GameState


async def compress_chronicle(
    game_id: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    locks: GameLocks,
    provider: LLMProvider,
    redact: Callable[[str], str],
    running: Compressions,
) -> bool:
    """Сжимает летопись игры; `True` — новая версия состояния записана.

    Фоновая задача: исключения наружу не выходят (кроме отмены при остановке сервера). `redact`
    убирает из текста причины значения секретов (его даёт вызывающий: настройки знает он)."""
    with running.claim(game_id) as mine:
        if not mine:
            return False
        try:
            return await _compress(game_id, sessionmaker, locks, provider, running)
        except asyncio.CancelledError:
            raise
        except _Postponed as why:
            logger.info("Сжатие летописи игры %s отложено до следующего хода: %s.", game_id, why)
        except _Unavailable as problem:
            reason = printable(redact(str(problem)), MAX_REASON_CHARS)
            logger.warning(
                "Сжатие летописи игры %s не удалось: %s. Летопись остаётся несжатой.",
                game_id,
                reason,
            )
        except Exception as exc:
            # Текст неожиданной ошибки не пишем: у ошибок базы он несёт значения запроса, а там
            # вся летопись и тайны игры. Тип подскажет, где искать.
            logger.warning(
                "Сжатие летописи игры %s прервано неожиданной ошибкой %s. Летопись остаётся "
                "несжатой.",
                game_id,
                type(exc).__name__,
            )
        return False


async def _compress(
    game_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    locks: GameLocks,
    provider: LLMProvider,
    running: Compressions,
) -> bool:
    started = time.monotonic()
    seen = await _read(game_id, sessionmaker, locks)
    if seen is None:
        return False
    entries = len(seen.state.chronicle.entries)
    if running.paused(game_id, entries):
        return False  # молча: платного вызова нет, а причину пауза уже записала в журнал
    try:
        summary = await _summarize(provider, seen.state.chronicle)
    except Exception:
        if running.failed(game_id, entries):
            logger.warning(
                "Сжатие летописи игры %s приостановлено после %d неудач подряд: следующая проба "
                "при %d записях (сейчас %d).",
                game_id,
                CHRONICLE_MAX_FAILURES,
                entries + CHRONICLE_RETRY_AFTER,
                entries,
            )
        raise
    running.succeeded(game_id)
    version = await _write(game_id, sessionmaker, locks, seen, summary)
    logger.info(
        "Летопись игры %s сжата: %d записей в сводку на %d знаков за %d мс (версия %d → %d).",
        game_id,
        entries,
        len(summary),
        round((time.monotonic() - started) * 1000),
        seen.version,
        version,
    )
    return True


async def _read(
    game_id: str, sessionmaker: async_sessionmaker[AsyncSession], locks: GameLocks
) -> _Seen | None:
    """Летопись на текущей версии; `None`, если сжимать нечего или записать результат всё равно
    нельзя (идёт ход мастера). Блокировка и сессия держатся только на время чтения."""
    async with locks.lock(game_id), sessionmaker() as session:
        game = await session.get(Game, game_id)
        if game is None or game.turn_phase == TurnPhase.RESOLVING:
            return None
        version = game.state_version
        row = await session.get(GameStateSnapshot, (game_id, version))
        state = None if row is None else row.to_state()
    if state is None or not compression_due(state):
        return None
    return _Seen(version, state)


async def _summarize(provider: LLMProvider, chronicle: Chronicle) -> str:
    """Один вызов дешёвой модели со своим тайм-аутом; ответ приведён к безопасной строке."""
    deadline = asyncio.timeout(CHRONICLE_TIMEOUT_S)
    try:
        async with deadline:
            answer = await provider.complete_json(compression_request(chronicle), ChronicleOut)
    except TimeoutError:
        # Свой тайм-аут отличаем от тайм-аута, который поднял сам провайдер.
        if deadline.expired():
            raise _Unavailable(f"тайм-аут: больше {CHRONICLE_TIMEOUT_S:g} с") from None
        raise _Unavailable("тайм-аут провайдера") from None
    except ProviderError as error:
        raise _Unavailable(f"{type(error).__name__}: {error}") from None
    return clean_summary(answer.summary)


async def _write(
    game_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    locks: GameLocks,
    seen: _Seen,
    summary: str,
) -> int:
    """Новая версия состояния со сводкой вместо записей; возвращает её номер.

    Под блокировкой игры, одной транзакцией. Версия должна остаться той, с которой читали, и ход
    мастера не должен идти: его коммит ждёт ту же версию. Следующий открытый ход начинается теперь
    с сжатой версии, чтобы «Перегенерировать» вернулась к ней, а не к несжатой."""
    async with locks.lock(game_id), sessionmaker() as session:
        game = await session.get(Game, game_id, populate_existing=True)
        if game is None:
            raise _Postponed("игра не найдена")
        if game.turn_phase == TurnPhase.RESOLVING:
            raise _Postponed("мастер разбирает ход")
        if game.state_version != seen.version:
            raise _Postponed("появилась новая версия состояния")
        newest = await session.scalar(
            select(func.max(GameStateSnapshot.version)).where(GameStateSnapshot.game_id == game_id)
        )
        squeezed = seen.state.model_copy(deep=True)
        squeezed.chronicle = Chronicle(summary=summary)
        squeezed.version = (newest or seen.version) + 1
        session.add(GameStateSnapshot.from_state(squeezed))
        game.state_version = squeezed.version
        turn = await session.get(Turn, game.current_turn_id) if game.current_turn_id else None
        if turn is not None and turn.state_version_before == seen.version:
            turn.state_version_before = squeezed.version
        await session.commit()
    return squeezed.version
