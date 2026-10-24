"""Атомарный ход мастера (спека §8.4 и §12, план B7.3).

Ход идёт в три приёма, чтобы ожидание модели не держало ни игру, ни базу:

1. Резерв (под блокировкой игры, короткая сессия). Проверяются игра и ход, собирается всё, что нужно
   модели (состояние, приключение из копии игры, ходы игроков, прошлые рассказы), фаза хода
   становится `resolving`, версия состояния фиксируется. Второй запрос в это время получает отказ
   (`Conflict`) сразу, а не встаёт в очередь.
2. Модель (без блокировки и без сессии). Ход разбирается на глубокой копии состояния; ошибка,
   тайм-аут или пустой рассказ выбрасывают копию. Пустой или оборванный рассказ получает один
   автоповтор с нуля на новой копии, любая другая ошибка сразу делает попытку неудачной. В бою
   после ответа модели ход передаётся дальше на той же копии (`dm.combat_flow.settle_combat`):
   броски передачи хода (спасброски от смерти лежащих героев) входят в этот же ход и откатываются
   с ним.
3. Коммит (снова под блокировкой, одна транзакция). Версия состояния в базе должна остаться той,
   что зарезервирована (иначе это конфликт, спека §12). Пишутся: новый снимок, сообщения, статус
   хода и версии, фаза игры, следующий открытый ход, статусы ходов игроков и журнал попыток.
   События уходят игрокам после коммита, построителями `realtime.views`.

Ход «конец привала» (`TurnKind.CAMP_END`, план B7.6) начинается с длинного отдыха: сервер проводит
его на копии до модели (`dm.camp_flow.end_camp`), и мастер видит утро и полное здоровье. Реплики
героев у костра лежат в ленте как `camp_line` этого хода и идут в контекст вместо ходов раунда.

После коммита любого хода (`_keep_chronicle_short`) летопись, доросшая до порога, сжимается фоновой
задачей (`dm.chronicle`, план B7.7): задача идёт в `Deps.tasks` (на сервере это `turn_tasks`, его
останавливает остановка сервера) и ход мастера не задерживает.

Внимание (`dm.spotlight`) считается по событиям, которые вызвала модель своими инструментами, а не
по тому, что сделал сервер: длинный отдых в конце привала и передача хода в бою (спасброски от
смерти лежащих героев) героев в фокус не ставят.

Неудача не меняет состояние игры: пишется журнал, ход получает статус `failed`, игра переходит в
`dm_failed`, хост видит «Мастер не справился» и повторяет (`run_dm_turn` на том же ходу).
Блокировка и сессия не держатся во время модели, поэтому запросы к игре не ждут мастера: ход
игрока (черновик, «Готов», «Пас»), пауза и повторный запуск сразу получают 409, а снимок и сокеты
работают. Если задачу хода отменили (остановка сервера), игра тоже уходит в
`dm_failed`, а не остаётся в `resolving`. Если не записалась и сама неудача (диск полон, база
занята), игра выходит из `resolving` короткой отметкой без журнала и сообщения хосту
(`_record_failure`); не прошла и она — сбой в журнале сервера, а «Повторить» принимает игру без
живого разбора (`LiveRuns`).

Рассылка после коммита (`announcing`) итог хода не меняет: запись уже сделана, сбой построителя
события пишется в журнал сервера, а клиент увидит всё в снимке.

«Перегенерировать» (`regenerate_last_turn`) — тот же ход с откатом в резерве: рассказ и броски
последнего хода снимаются (`superseded`), открытый следующий ход тоже, состояние возвращается к
версии до хода. Версии состояния растут только вперёд (следующая — наибольшая в базе плюс один):
снимок отменённого хода остаётся в базе для разбора, а номера не повторяются.

Броски делает только сервер: генератор приходит в `Deps.rng` (в игре `SystemRng`, в тестах
подставляют `SeededRng`/`ScriptedRng` через конструктор), а исполнитель и инструменты берут его из
контекста. Ключи и токены в журнал не попадают: тексты ошибок проходят через `safe_error_text`
(`dm.text`: замена значений секретов из настроек и `printable`), а ошибка базы не несёт значений
запроса (`hide_parameters` движка), то есть ни тайных ходов, ни снимка состояния.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.events import Event
from dnd_engine.modules import build_ruleset
from dnd_engine.modules.base import RuleSet
from dnd_engine.rng import Rng
from dnd_engine.state import GameState
from dnd_engine.tools import build_registry
from dnd_engine.tools.combat import event_lines
from dnd_engine.tools.registry import ToolContext, ToolRegistry
from dnd_server.config import Settings
from dnd_server.db.models import (
    AUDIENCE_ALL,
    ActionStatus,
    DmTurnLog,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    Turn,
    TurnKind,
    TurnPhase,
    TurnStatus,
    utcnow,
)
from dnd_server.dm.camp_flow import end_camp
from dnd_server.dm.chronicle import Compressions, compress_chronicle, compression_due
from dnd_server.dm.combat_flow import combat_acting, settle_combat
from dnd_server.dm.context import (
    MAX_RECENT_TURNS,
    Pacing,
    PlayerActionView,
    RecentTurn,
    adventure_brief,
    system_prompt,
    turn_context,
)
from dnd_server.dm.executor import DmToolExecutor
from dnd_server.dm.provider import GameProviders
from dnd_server.dm.spotlight import update_spotlight
from dnd_server.dm.text import (
    REASON_CANCELLED,
    REASON_CONFLICT,
    REASON_EMPTY,
    REASON_INTERNAL,
    REASON_TRUNCATED,
    ModelTimeout,
    describe_error,
    failure_reason,
    safe_error_text,
    safe_traceback,
)
from dnd_server.games.hooks import Broadcaster, send_to_game
from dnd_server.games.service import Conflict, GameLocks, NotFound
from dnd_server.llm.base import (
    MAX_TRACE_RESULT_CHARS,
    ToolCallTrace,
    TurnRequest,
    TurnResult,
    Usage,
    make_trace_call,
    printable,
)
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.realtime.views import (
    FeedEntry,
    feed_entries,
    feed_item_event,
    feed_withdrawn_event,
    phase_event,
    player_status_event,
    state_events,
)
from dnd_server.turns.timers import TaskSet

logger = logging.getLogger(__name__)

# Пределы одного хода (спека §8.4): итераций цикла инструментов и время на весь ход. Адаптеры
# применяют их сами; ход ждёт ещё `TIMEOUT_GRACE_S`, чтобы адаптер успел остановиться чисто, и
# только потом сам отменяет вызов.
MAX_TOOL_ITERATIONS = 20
TURN_TIMEOUT_S = 180.0
TIMEOUT_GRACE_S = 5.0
MAX_ATTEMPTS = 2  # первая попытка и один автоповтор пустого или оборванного рассказа
SERVER_END_TURN = "server_end_turn"  # запись журнала о передаче хода, которую делает сервер
SERVER_END_CAMP = "server_end_camp"  # запись журнала о длинном отдыхе в конце привала

# Из каких фаз допустима «Перегенерировать»: игроки обсуждают следующий ход, сбор ещё не начался.
REGENERATE_FROM = (TurnPhase.DISCUSSION, TurnPhase.COMBAT_WAIT, TurnPhase.CAMP)
# Сообщения, которые создаёт сам ход мастера. Ходы игроков (`action`) и реплики у костра
# (`camp_line`) — его входные данные, и при перегенерации они остаются.
OUTPUT_KINDS = (
    MessageKind.NARRATION,
    MessageKind.PRIVATE,
    MessageKind.ROLL,
    MessageKind.SYSTEM,
)
PASS_STATUSES = (ActionStatus.PASS, ActionStatus.AUTO_PASS)


def discussion_follow_up(_state: GameState) -> tuple[TurnPhase, TurnKind]:
    """После хода мастера игроки обсуждают следующий раунд: фаза `discussion`, ход `round`."""
    return TurnPhase.DISCUSSION, TurnKind.ROUND


class LiveRuns:
    """Игры, где мастер работает прямо сейчас: резерв, модель и запись результата.

    Игра в `resolving`, которой здесь нет, осталась без мастера: разбор кончился, а запись неудачи
    не прошла (диск полон, база занята). «Повторить» принимает такую игру как прерванную
    (`turns.flow.retry`), и хозяину не нужно перезапускать сервер. Учёт живёт в памяти процесса,
    как блокировки игр. Разборы одной игры вкладываются друг в друга (вплетение зовёт открытие) и
    запрашиваются параллельно (второй получит отказ), поэтому места считаются, а не занимаются."""

    def __init__(self) -> None:
        self._runs: Counter[str] = Counter()

    @contextmanager
    def running(self, game_id: str) -> Iterator[None]:
        self._runs[game_id] += 1
        try:
            yield
        finally:
            self._runs[game_id] -= 1
            if not self._runs[game_id]:
                del self._runs[game_id]

    def __contains__(self, game_id: object) -> bool:
        return game_id in self._runs


@contextmanager
def announcing(deps: Deps, game_id: str, turn_id: str | None) -> Iterator[None]:
    """Рассылка после коммита. Запись уже сделана, поэтому сбой рассылки (например, построителя
    события) не отменяет её и не делает удавшийся ход неудачным: он пишется в журнал сервера с id
    игры и хода, а клиент, не получивший событие, видит всё в снимке при переподключении.
    Трассировка идёт через `safe_traceback`: текст ошибки не должен нести куски данных игры."""
    try:
        yield
    except Exception as exc:
        logger.error(
            "Рассылка после записи не удалась (игра %s, ход %s): запись сделана, клиенты "
            "увидят её в снимке.\n%s",
            game_id,
            turn_id,
            safe_traceback(exc, deps.settings),
        )


@dataclass(frozen=True)
class Deps:
    """Что нужно ходу мастера от приложения; собирается из `app.state` (`from_app`).

    `rng` — единственный источник бросков хода: в игре `SystemRng`, в тестах подмена через
    конструктор. `providers` — кеш провайдеров игр (`dm.provider.GameProviders`). `follow_up` —
    какая фаза и какой ход идут за удавшимся: решает по сцене поток боя и привала
    (`app.state.turn_follow_up`), по умолчанию — обсуждение следующего раунда. Выбор попадает в
    ту же транзакцию, что и ход: клиенты не видят промежуточной фазы. `clock` — «сейчас» для срока
    новой фазы (у привала, `_countdown`): в игре это часы планировщика отсчётов, чтобы срок в базе и
    отсчёт шли по одним часам. `tasks` — фоновые задачи после хода (сжатие летописи): в игре это
    `app.state.turn_tasks`, который останавливается вместе с сервером, а тесты ждут его `join`.
    `compressions` — какие игры сейчас сжимают летопись (одно сжатие на игру). `live` — игры, где
    мастер работает сейчас (`LiveRuns`)."""

    sessionmaker: async_sessionmaker[AsyncSession]
    locks: GameLocks
    broadcaster: Broadcaster
    settings: Settings
    turn_tokens: TurnTokens | None
    srd: SrdContent
    rng: Rng
    providers: GameProviders = field(default_factory=GameProviders)
    follow_up: Callable[[GameState], tuple[TurnPhase, TurnKind]] = discussion_follow_up
    clock: Callable[[], datetime] = utcnow
    tasks: TaskSet = field(default_factory=TaskSet)
    compressions: Compressions = field(default_factory=Compressions)
    live: LiveRuns = field(default_factory=LiveRuns)

    @classmethod
    def from_app(cls, app: FastAPI) -> Deps:
        """Зависимости из частей приложения; читаются при каждом вызове, поэтому подмена
        `app.state.settings`, `broadcaster` или `rng` (тесты) подхватывается."""
        state = app.state
        providers = getattr(state, "game_providers", None)
        if providers is None:
            providers = state.game_providers = GameProviders()
        compressions = getattr(state, "chronicle_compressions", None)
        if compressions is None:
            compressions = state.chronicle_compressions = Compressions()
        live = getattr(state, "live_runs", None)
        if live is None:
            live = state.live_runs = LiveRuns()
        return cls(
            sessionmaker=state.sessionmaker,
            locks=state.game_locks,
            broadcaster=state.broadcaster,
            settings=state.settings,
            turn_tokens=getattr(state, "turn_tokens", None),
            srd=load_srd(),
            rng=state.rng,
            providers=providers,
            follow_up=getattr(state, "turn_follow_up", discussion_follow_up),
            clock=getattr(getattr(state, "scheduler", None), "now", utcnow),
            tasks=state.turn_tasks,
            compressions=compressions,
            live=live,
        )


@dataclass(frozen=True)
class TurnOutcome:
    """Итог попытки разобрать ход. `error` — причина для хоста (по-русски, без подробностей)."""

    turn_id: str
    status: TurnStatus  # DONE или FAILED
    state_version: int  # версия состояния игры после попытки; при неудаче прежняя
    attempts: int
    error: str | None = None


# --- запуск и перегенерация ---


async def run_dm_turn(
    game_id: str,
    *,
    mode: TurnKind,
    deps: Deps,
    acting: list[str] | None = None,
    on_reserved: Callable[[], None] | None = None,
    expected_turn_id: str | None = None,
) -> TurnOutcome:
    """Разбирает текущий ход игры мастером.

    Возвращает итог (`DONE` или `FAILED`: ход удался или попытка неудачная и записана); если ход
    даже не начался, поднимает `NotFound` (нет игры) или `Conflict` (игра не идёт, ход уже
    разбирается, ход уже разобран): тогда в игре ничего не менялось. `acting` — id бойцов, за
    которых мастер ходит в боевом вызове. `on_reserved` вызывается один раз, когда ход
    зарезервирован (фаза `resolving` уже в базе), и до обращения к модели: так запрос хоста
    отвечает сразу после резерва, не дожидаясь рассказа (`turns.flow`). `expected_turn_id` — ход,
    для которого просят разбор: запрос, отпустивший блокировку игры до резерва, не должен разобрать
    уже следующий открытый ход (защита в глубину, `Conflict`); без него разбирается текущий."""
    return await _run(
        game_id,
        deps=deps,
        mode=mode,
        acting=acting,
        regenerate=False,
        on_reserved=on_reserved,
        expected_turn_id=expected_turn_id,
    )


async def regenerate_last_turn(
    game_id: str, *, deps: Deps, on_reserved: Callable[[], None] | None = None
) -> TurnOutcome:
    """«Перегенерировать»: откат последнего разобранного хода и новая попытка тем же режимом.

    Права хоста проверяет вызывающий. Отказ (`Conflict`) не меняет игру. Сообщения хода снимаются
    до начала новой попытки; если она не удастся, игра остаётся откатанной в `dm_failed`, и хост
    повторяет ход обычным образом. `on_reserved` — как у `run_dm_turn`."""
    return await _run(
        game_id,
        deps=deps,
        mode=None,
        acting=None,
        regenerate=True,
        on_reserved=on_reserved,
        expected_turn_id=None,
    )


async def _run(
    game_id: str,
    *,
    deps: Deps,
    mode: TurnKind | None,
    acting: list[str] | None,
    regenerate: bool,
    on_reserved: Callable[[], None] | None,
    expected_turn_id: str | None,
) -> TurnOutcome:
    # Игра учитывается с самого начала: к моменту, когда другой запрос увидит её в `resolving`
    # (резерв держит блокировку до коммита), мастер уже записан живым.
    with deps.live.running(game_id):
        plan = await _reserve(
            game_id,
            deps,
            mode=mode,
            acting=acting,
            regenerate=regenerate,
            expected_turn_id=expected_turn_id,
        )
        if on_reserved is not None:
            try:
                on_reserved()
            except Exception as exc:
                # Сбой оповещения не должен оставить ход зарезервированным без модели.
                logger.error(
                    "Сбой on_reserved (игра %s, ход %s).\n%s",
                    plan.game_id,
                    plan.turn_id,
                    safe_traceback(exc, deps.settings),
                )
        try:
            attempts = await _think(deps, plan)
        except asyncio.CancelledError:
            # Задачу хода отменили (остановка сервера): игра не должна зависнуть в `resolving`.
            await _record_cancellation(deps, plan)
            raise
        final = attempts[-1]
        if final.ok:
            try:
                return await _commit_success(deps, plan, attempts)
            except _VersionConflict as conflict:
                final.fail(REASON_CONFLICT, f"конфликт версий состояния: {conflict}")
            except Exception as exc:
                # Запись не удалась целиком (транзакция откатилась): ход тоже неудачный, а игра не
                # остаётся в `resolving`.
                _fail_with(final, exc, deps, plan)
        for attempt in attempts:
            if not attempt.ok:
                logger.warning(
                    "Ход мастера (игра %s, ход %s, попытка %d) не удался: %s",
                    plan.game_id,
                    plan.turn_id,
                    attempt.number,
                    attempt.error,
                )
        return await _record_failure(deps, plan, attempts)


# --- резерв ---


@dataclass
class _Plan:
    """Всё, что нужно ходу после резерва; обычные данные, без сессии базы."""

    game_id: str
    turn_id: str
    turn_number: int
    mode: TurnKind
    acting: list[str] | None
    version: int
    state: GameState
    adventure: Adventure
    provider_name: str | None
    model: str | None
    profanity: bool  # флажок мата игры: строка «Мат: …» в контексте хода
    pc_ids: tuple[str, ...]
    host_user_id: str
    user_ids: tuple[str, ...]
    actions: list[PlayerActionView]
    recent: list[RecentTurn]
    pacing: Pacing
    previous_attempts: int = 0  # попытки этого хода в журнале до текущего запуска
    rewound: bool = False  # ход откатан перегенерацией
    withdrawn: list[tuple[int, str]] = field(default_factory=list)  # снятые записи ленты
    # Заполняются, когда провайдер и контекст собраны; нужны журналу попыток.
    context_text: str | None = None
    provider: Any = None

    @property
    def secret_actors(self) -> set[str]:
        return {
            action.character_id for action in self.actions if action.secret and not action.passed
        }

    @property
    def submitted(self) -> set[str]:
        return {action.character_id for action in self.actions if not action.passed}

    @property
    def inspired(self) -> set[str]:
        return {
            action.character_id
            for action in self.actions
            if action.inspiration
            and not action.passed
            and self.state.party[action.character_id].inspiration
        }


async def _reserve(
    game_id: str,
    deps: Deps,
    *,
    mode: TurnKind | None,
    acting: list[str] | None,
    regenerate: bool,
    expected_turn_id: str | None,
) -> _Plan:
    async with deps.locks.lock(game_id), deps.sessionmaker() as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        if game.phase not in (GamePhase.WEAVING, GamePhase.PLAYING):
            raise Conflict("Игра сейчас не идёт: мастер ход не разбирает.")
        if expected_turn_id is not None and game.current_turn_id != expected_turn_id:
            raise Conflict("Это уже другой ход.")
        if game.turn_phase == TurnPhase.RESOLVING:
            raise Conflict("Мастер уже разбирает ход: дождитесь рассказа.")
        withdrawn: list[tuple[int, str]] = []
        if regenerate:
            turn, mode, acting, withdrawn = await _rewind(session, game)
        else:
            assert mode is not None
            turn = await _turn_to_resolve(session, game, mode)
        plan = await _plan(session, game, turn, mode, acting, deps.clock())
        plan.rewound, plan.withdrawn = regenerate, withdrawn
        turn.status = TurnStatus.RESOLVING
        turn.state_version_after = None
        if turn.state_version_before is None:
            turn.state_version_before = game.state_version
        game.turn_phase = TurnPhase.RESOLVING
        game.deadline_at = None
        await session.commit()
        with announcing(deps, game_id, plan.turn_id):
            await _announce_reserve(deps, game, plan)
    return plan


async def _turn_to_resolve(session: AsyncSession, game: Game, mode: TurnKind) -> Turn:
    """Ход, который разбирается: текущий открытый или, если его ещё нет (открытие игры), новый."""
    if game.current_turn_id is None:
        last = await session.scalar(select(func.max(Turn.number)).where(Turn.game_id == game.id))
        turn = Turn(
            game_id=game.id,
            number=(last or 0) + 1,
            kind=mode,
            status=TurnStatus.OPEN,
            state_version_before=game.state_version,
        )
        session.add(turn)
        await session.flush()
        game.current_turn_id = turn.id
        return turn
    turn = await session.get(Turn, game.current_turn_id)
    if turn is None or turn.game_id != game.id:
        raise Conflict("Текущий ход игры не найден.")
    if turn.status in (TurnStatus.DONE, TurnStatus.SUPERSEDED):
        raise Conflict("Этот ход уже разобран.")
    # Привал и раунд не подменяют друг друга: запоздавший конец привала (кнопка против отсчёта)
    # не должен превратить следующий раунд в «конец привала», а раунд — оборвать идущий привал.
    if mode == TurnKind.CAMP_END and turn.kind != TurnKind.CAMP_END:
        raise Conflict("Сейчас нет привала: заканчивать нечего.")
    if mode != TurnKind.CAMP_END and turn.kind == TurnKind.CAMP_END:
        raise Conflict("Сейчас привал: ход разбирается после его конца.")
    turn.kind = mode
    return turn


async def _rewind(
    session: AsyncSession, game: Game
) -> tuple[Turn, TurnKind, list[str] | None, list[tuple[int, str]]]:
    """Откат последнего разобранного хода: возвращает его, режим, бойцов и снятые записи ленты."""
    if game.turn_phase not in REGENERATE_FROM:
        raise Conflict("Перегенерировать ответ можно, пока игроки обсуждают следующий ход.")
    last = await session.scalar(
        select(Turn)
        .where(Turn.game_id == game.id, Turn.status == TurnStatus.DONE)
        .order_by(Turn.number.desc(), Turn.created_at.desc())
        .limit(1)
    )
    if last is None:
        raise Conflict("Мастеру пока нечего перегенерировать: ни один ход не разобран.")
    following = await session.get(Turn, game.current_turn_id) if game.current_turn_id else None
    next_is_untouched = (
        following is not None
        and following.status == TurnStatus.OPEN
        and following.number == last.number + 1
    )
    if not next_is_untouched:
        raise Conflict("Следующий ход уже начат: перегенерировать нельзя.")
    version_before = last.state_version_before
    previous = (
        None
        if version_before is None
        else await session.get(GameStateSnapshot, (game.id, version_before))
    )
    if previous is None:
        raise Conflict("Состояния до этого хода нет: перегенерировать нельзя.")
    last_log = await session.scalar(
        select(DmTurnLog).where(DmTurnLog.turn_id == last.id).order_by(DmTurnLog.id.desc()).limit(1)
    )
    acting = None if last_log is None else last_log.request.get("acting")

    rows = await session.scalars(
        select(Message).where(
            Message.game_id == game.id,
            Message.superseded.is_(False),
            ((Message.turn_id == last.id) & Message.kind.in_(OUTPUT_KINDS))
            | (Message.turn_id == following.id),
        )
    )
    withdrawn = []
    for message in rows:
        message.superseded = True
        withdrawn.append((message.id, message.audience))
    following.status = TurnStatus.SUPERSEDED
    game.state_version = version_before
    game.current_turn_id = last.id
    return last, last.kind, acting, withdrawn


async def _plan(
    session: AsyncSession,
    game: Game,
    turn: Turn,
    mode: TurnKind,
    acting: list[str] | None,
    now: datetime,
) -> _Plan:
    row = await session.get(GameStateSnapshot, (game.id, game.state_version))
    if row is None:
        raise Conflict("У игры ещё нет состояния: мастеру нечего разбирать.")
    state = row.to_state()
    if mode == TurnKind.COMBAT and acting is None:
        # «Повторить» после падения процесса: в записи восстановления бойцов нет, их называет бой.
        acting = combat_acting(state)
    players = list(
        await session.scalars(
            select(GamePlayer)
            .where(GamePlayer.game_id == game.id)
            .order_by(GamePlayer.joined_order)
        )
    )
    character_of = {hero.player_id: hero.id for hero in state.party.values() if hero.player_id}
    adventure = Adventure.model_validate(game.adventure_data)
    return _Plan(
        game_id=game.id,
        turn_id=turn.id,
        turn_number=turn.number,
        mode=mode,
        acting=acting,
        version=game.state_version,
        state=state,
        adventure=adventure,
        provider_name=game.provider,
        model=game.model,
        profanity=game.profanity,
        pc_ids=tuple(p.character_id for p in players if p.character_id),
        host_user_id=game.host_user_id,
        user_ids=tuple(p.user_id for p in players),
        actions=await _turn_inputs(session, turn, character_of, players),
        recent=await _recent_turns(session, game.id, turn.number, character_of, players),
        pacing=Pacing.of(adventure, game.active_time(now)),
        previous_attempts=await session.scalar(
            select(func.count()).select_from(DmTurnLog).where(DmTurnLog.turn_id == turn.id)
        )
        or 0,
    )


async def _turn_inputs(
    session: AsyncSession,
    turn: Turn,
    character_of: dict[str, str],
    players: list[GamePlayer],
) -> list[PlayerActionView]:
    """Что игроки подали в ход: ходы раунда, а в ходе «конец привала» — реплики у костра."""
    if turn.kind == TurnKind.CAMP_END:
        return await _camp_lines(session, turn.id)
    return await _action_views(session, turn.id, character_of, players)


async def _camp_lines(session: AsyncSession, turn_id: str) -> list[PlayerActionView]:
    """Реплики героев у костра по порядку, как они были сказаны. Это текст игроков, контекст
    отдаёт его мастеру только в блоке данных. Пометок у реплик нет: тайных, пасов и вдохновения
    на привале не бывает."""
    rows = await session.scalars(
        select(Message)
        .where(
            Message.turn_id == turn_id,
            Message.kind == MessageKind.CAMP_LINE,
            Message.superseded.is_(False),
        )
        .order_by(Message.id)
    )
    return [
        PlayerActionView(row.data["actor_id"], row.text)
        for row in rows
        if row.data and row.data.get("actor_id")
    ]


async def _action_views(
    session: AsyncSession,
    turn_id: str,
    character_of: dict[str, str],
    players: list[GamePlayer],
) -> list[PlayerActionView]:
    """Ходы игроков хода так, как их видит мастер, в порядке входа игроков в игру.

    Черновик, не доведённый до сдачи, читается как сдача (есть текст) или пас (текста нет): так
    его и закрывает коммит. Игрок, которого нет в отряде, мастеру неизвестен."""
    rows = {
        action.user_id: action
        for action in await session.scalars(
            select(PlayerAction).where(PlayerAction.turn_id == turn_id)
        )
    }
    views = []
    for player in players:
        action, character_id = rows.get(player.user_id), character_of.get(player.user_id)
        if action is None or character_id is None:
            continue
        passed = action.status in PASS_STATUSES or (
            action.status == ActionStatus.DRAFT and not action.text.strip()
        )
        views.append(
            PlayerActionView(
                character_id,
                "" if passed else action.text,
                secret=action.secret,
                inspiration=action.inspiration,
                passed=passed,
            )
        )
    return views


async def _recent_turns(
    session: AsyncSession,
    game_id: str,
    number: int,
    character_of: dict[str, str],
    players: list[GamePlayer],
) -> list[RecentTurn]:
    """Последние разобранные ходы до этого: рассказ мастера и ходы игроков, от старых к новым."""
    done = await session.scalars(
        select(Turn)
        .where(Turn.game_id == game_id, Turn.status == TurnStatus.DONE, Turn.number < number)
        .order_by(Turn.number.desc())
        .limit(MAX_RECENT_TURNS)
    )
    recent = []
    for turn in reversed(list(done)):
        story = await session.scalar(
            select(Message.text)
            .where(
                Message.turn_id == turn.id,
                Message.kind == MessageKind.NARRATION,
                Message.superseded.is_(False),
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        actions = await _turn_inputs(session, turn, character_of, players)
        recent.append(RecentTurn(turn.number, story or "", tuple(actions)))
    return recent


async def _announce_reserve(deps: Deps, game: Game, plan: _Plan) -> None:
    """«Мастер думает»; при перегенерации — ещё откат ленты и состояния игроков."""
    await send_to_game(deps.broadcaster, game.id, phase_event(game, plan.state))
    for message_id, audience in plan.withdrawn:
        await send_to_game(deps.broadcaster, game.id, feed_withdrawn_event(message_id), audience)
    if plan.rewound:
        for event, audience in state_events(plan.state, deps.srd, plan.adventure):
            await send_to_game(deps.broadcaster, game.id, event, audience)


# --- модель ---


@dataclass
class _Attempt:
    """Одна попытка хода: рабочая копия и события (у удавшейся) или причина неудачи."""

    number: int
    ok: bool = False
    retry: bool = False  # пустой или оборванный рассказ: допустим автоповтор
    reason: str | None = None  # для хоста
    error: str | None = None  # для журнала
    text: str = ""
    result: TurnResult | None = None
    trace: list[ToolCallTrace] = field(default_factory=list)
    context: ToolContext | None = None
    # События, которые вызвала сама модель: без того, что сервер сделал до неё (длинный отдых) и
    # после неё (передача хода). По ним считается внимание к героям.
    story_events: list[Event] = field(default_factory=list)
    duration_ms: int = 0
    # Модель, которую провайдер назвал сразу после вызова этой попытки (для журнала): у
    # `claude_code_cli` она своя на каждый запуск, и после автоповтора провайдер помнит уже другую.
    model: str | None = None

    def fail(self, reason: str, error: str, *, retry: bool = False) -> None:
        self.ok, self.retry, self.reason, self.error = False, retry, reason, error

    @property
    def events(self) -> list[Event]:
        return [] if self.context is None else self.context.events


@dataclass(frozen=True)
class _Prepared:
    """Запрос к модели, общий для всех попыток хода: промпт, сводка, контекст и инструменты."""

    ruleset: RuleSet
    registry: ToolRegistry
    system: str
    brief: str
    context: str
    # Состояние, с которого мастер начинает ход, и события, что сервер сделал до него. Обычно это
    # состояние резерва без событий; в ходе «конец привала» — утро после длинного отдыха.
    base: GameState
    base_events: tuple[Event, ...] = ()


def _prepare(deps: Deps, plan: _Plan) -> _Prepared:
    ruleset = build_ruleset(plan.state.rules)
    base, base_events = plan.state, []
    if plan.mode == TurnKind.CAMP_END:
        # Длинный отдых проводит сервер до модели (план B7.6): мастер рассказывает утро уже после
        # него, а копия состояния, на которой он идёт, и контекст берутся из одного места.
        base, base_events = end_camp(
            plan.state, plan.turn_number, ruleset, content=deps.srd, adventure=plan.adventure
        )
    return _Prepared(
        ruleset=ruleset,
        registry=build_registry(ruleset),
        system=system_prompt(ruleset),
        brief=adventure_brief(plan.adventure),
        context=turn_context(
            state=base,
            adventure=plan.adventure,
            srd=deps.srd,
            mode=plan.mode,
            actions=plan.actions,
            recent=plan.recent,
            pacing=plan.pacing,
            acting=plan.acting,
            profanity=plan.profanity,
        ),
        base=base,
        base_events=tuple(base_events),
    )


async def _think(deps: Deps, plan: _Plan) -> list[_Attempt]:
    """Попытки хода (вне блокировки и сессии): последняя в списке — итог."""
    attempts: list[_Attempt] = []
    try:
        prepared = _prepare(deps, plan)
        provider = deps.providers.of(
            plan.game_id,
            deps.settings,
            provider=plan.provider_name,
            model=plan.model,
            pc_ids=plan.pc_ids,
            turn_tokens=deps.turn_tokens,
        )
    except Exception as exc:
        attempt = _Attempt(1)
        _fail_with(attempt, exc, deps, plan)
        return [attempt]
    plan.context_text, plan.provider = prepared.context, provider
    for number in range(1, MAX_ATTEMPTS + 1):
        attempt = await _attempt(deps, plan, prepared, provider, number)
        attempts.append(attempt)
        if attempt.ok or not attempt.retry:
            break
    return attempts


async def _attempt(
    deps: Deps, plan: _Plan, prepared: _Prepared, provider: Any, number: int
) -> _Attempt:
    attempt = _Attempt(number)
    started = time.monotonic()
    # Копия глубокая: инструменты меняют её, а неудачная попытка просто выбрасывает.
    work = prepared.base.model_copy(deep=True)
    work.turn = plan.turn_number
    ctx = ToolContext(
        state=work,
        adventure=plan.adventure,
        content=deps.srd,
        rules=prepared.ruleset,
        rng=deps.rng,
        events=list(prepared.base_events),
        secret_actors=plan.secret_actors,
        inspired=plan.inspired,
        acting=tuple(plan.acting or ()),
    )
    executor = DmToolExecutor(ctx, prepared.registry)
    request = TurnRequest(
        system=prepared.system,
        adventure_brief=prepared.brief,
        context=prepared.context,
        tools=executor.schemas(),
        max_tool_iterations=MAX_TOOL_ITERATIONS,
        timeout_s=TURN_TIMEOUT_S,
    )
    settled: list[Event] = []
    try:
        result = await _call_model(provider, request, executor)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _fail_with(attempt, exc, deps, plan)
    else:
        attempt.result = result
        text = result.text.strip()
        if not text:
            attempt.fail(REASON_EMPTY, "нет итогового текста: модель не дала рассказа", retry=True)
        elif result.stop_reason == "max_tokens":
            attempt.fail(
                REASON_TRUNCATED,
                "рассказ оборван лимитом токенов (stop_reason=max_tokens)",
                retry=True,
            )
        else:
            try:
                # Передача хода в бою и выход из него: на рабочей копии, до коммита (спека §8.4).
                settled = settle_combat(ctx, plan.mode)
            except Exception as exc:
                _fail_with(attempt, exc, deps, plan)
            else:
                attempt.ok, attempt.text, attempt.context = True, text, ctx
                attempt.story_events = ctx.events[
                    len(prepared.base_events) : len(ctx.events) - len(settled)
                ]
    attempt.model = getattr(provider, "model", None)
    attempt.trace = list(executor.trace)
    if attempt.ok and prepared.base_events:
        # Длинный отдых сделал сервер, не модель: в журнале он стоит отдельной записью.
        lines = json.dumps({"events": event_lines(prepared.base_events)}, ensure_ascii=False)
        attempt.trace.insert(0, make_trace_call(SERVER_END_CAMP, {}, lines, False))
    if attempt.ok and settled:
        # Броски передачи хода делает сам сервер, не модель: в журнале они стоят отдельной записью.
        lines = json.dumps({"events": event_lines(settled)}, ensure_ascii=False)
        attempt.trace.append(make_trace_call(SERVER_END_TURN, {}, lines, False))
    attempt.duration_ms = round((time.monotonic() - started) * 1000)
    return attempt


async def _call_model(provider: Any, request: TurnRequest, executor: DmToolExecutor) -> TurnResult:
    """`run_turn` с запасным тайм-аутом: адаптер ограничивает себя сам, но ход не должен
    зависеть от этого (заглушка или сбой адаптера)."""
    deadline = asyncio.timeout(request.timeout_s + TIMEOUT_GRACE_S)
    try:
        async with deadline:
            return await provider.run_turn(request, executor)
    except TimeoutError:
        if not deadline.expired():
            raise  # тайм-аут самого исполнителя или адаптера: это его ошибка, не наш предел
        raise ModelTimeout(f"тайм-аут хода: больше {request.timeout_s:g} с") from None


def _fail_with(attempt: _Attempt, exc: Exception, deps: Deps, plan: _Plan) -> None:
    """Исключение хода -> причина для хоста и текст для журнала (тип и безопасный текст ошибки)."""
    reason = failure_reason(exc)
    attempt.fail(reason, describe_error(exc, deps.settings))
    if reason == REASON_INTERNAL:
        # Трассировка нужна для поиска бага; значения секретов и входа pydantic в ней нет.
        logger.error(
            "Непредвиденная ошибка хода мастера (игра %s, ход %s, попытка %d):\n%s",
            plan.game_id,
            plan.turn_id,
            attempt.number,
            safe_traceback(exc, deps.settings),
        )


# --- коммит ---


class _VersionConflict(Exception):
    """Версия состояния игры за время хода изменилась: двойной коммит, ход отклоняется."""


async def _commit_success(deps: Deps, plan: _Plan, attempts: list[_Attempt]) -> TurnOutcome:
    """Одна транзакция хода. Версия состояния в базе должна остаться зарезервированной, иначе
    `_VersionConflict`; любая ошибка записи откатывает транзакцию целиком."""
    final = attempts[-1]
    assert final.context is not None
    async with deps.locks.lock(plan.game_id), deps.sessionmaker() as session:
        game = await session.get(Game, plan.game_id, populate_existing=True)
        turn = await session.get(Turn, plan.turn_id, populate_existing=True)
        if (
            game.state_version != plan.version
            or game.current_turn_id != plan.turn_id
            or game.turn_phase != TurnPhase.RESOLVING
        ):
            raise _VersionConflict(
                f"ожидалась версия {plan.version}, в базе {game.state_version}"
                if game.state_version != plan.version
                else "ход или фаза игры сменились"
            )
        newest = await session.scalar(
            select(func.max(GameStateSnapshot.version)).where(
                GameStateSnapshot.game_id == plan.game_id
            )
        )
        work = final.context.state
        work.version = (newest or plan.version) + 1
        work.spotlight = update_spotlight(work, plan.submitted, final.story_events)
        entries = feed_entries(
            final.events,
            before=plan.state,
            after=work,
            adventure=plan.adventure,
            srd=deps.srd,
            secret_actors=plan.secret_actors,
        )
        entries.append(FeedEntry(AUDIENCE_ALL, MessageKind.NARRATION, final.text))
        messages = [_message(plan, entry) for entry in entries]
        next_phase, next_kind = deps.follow_up(work)
        following = Turn(
            game_id=plan.game_id,
            number=turn.number + 1,
            kind=next_kind,
            status=TurnStatus.OPEN,
            state_version_before=work.version,
        )
        session.add_all([GameStateSnapshot.from_state(work), following, *messages])
        await session.flush()
        turn.status = TurnStatus.DONE
        turn.state_version_after = work.version
        if game.phase == GamePhase.WEAVING:  # открытие игры; пауза хоста остаётся паузой
            game.phase = GamePhase.PLAYING
        game.turn_phase = next_phase
        game.deadline_at = _countdown(deps, game, next_phase)
        game.state_version = work.version
        game.current_turn_id = following.id
        await _finish_actions(session, plan.turn_id)
        session.add_all(_log_rows(deps, plan, attempts))
        await session.commit()
        with announcing(deps, plan.game_id, plan.turn_id):
            await _announce_commit(deps, game, plan, work, messages)
    _keep_chronicle_short(deps, plan, work)
    return TurnOutcome(plan.turn_id, TurnStatus.DONE, work.version, len(attempts))


def _keep_chronicle_short(deps: Deps, plan: _Plan, state: GameState) -> None:
    """После коммита хода: летопись доросла до порога — сжатие идёт фоновой задачей (план B7.7).

    Общий хук всех ходов: раунд, бой, «конец привала», открытие. Задача сама проверяет, что можно
    записать результат (версия не ушла вперёд, мастер не разбирает ход), поэтому здесь только
    запуск; провайдер тот же, что вёл ход."""
    if not compression_due(state):
        return
    deps.tasks.spawn(
        compress_chronicle(
            plan.game_id,
            sessionmaker=deps.sessionmaker,
            locks=deps.locks,
            provider=plan.provider,
            redact=lambda text: safe_error_text(text, deps.settings),
            running=deps.compressions,
        )
    )


def _countdown(deps: Deps, game: Game, phase: TurnPhase) -> datetime | None:
    """Срок новой фазы игры. Привал идёт `games.camp_seconds` от «сейчас» по часам приложения и
    ставится в той же транзакции, что и фаза: клиенты не видят привала без срока. У остальных фаз
    своего таймера нет: сбор ходов и «Поторопить» запускает хост."""
    if phase != TurnPhase.CAMP:
        return None
    return deps.clock() + timedelta(seconds=game.camp_seconds)


def _message(plan: _Plan, entry: FeedEntry) -> Message:
    return Message(
        game_id=plan.game_id,
        turn_id=plan.turn_id,
        audience=entry.audience,
        kind=entry.kind,
        text=entry.text,
        data=entry.data,
    )


async def _finish_actions(session: AsyncSession, turn_id: str) -> None:
    """Черновики разобранного хода закрываются: с текстом — автосдача, без — автопас."""
    rows = await session.scalars(
        select(PlayerAction).where(
            PlayerAction.turn_id == turn_id, PlayerAction.status == ActionStatus.DRAFT
        )
    )
    for action in rows:
        action.status = (
            ActionStatus.AUTO_SUBMITTED if action.text.strip() else ActionStatus.AUTO_PASS
        )


async def _announce_commit(
    deps: Deps, game: Game, plan: _Plan, work: GameState, messages: list[Message]
) -> None:
    """Рассылка после коммита: записи ленты, отряд, бой, листы, фаза и статусы нового хода."""
    for message in messages:
        await send_to_game(deps.broadcaster, game.id, feed_item_event(message), message.audience)
    for event, audience in state_events(work, deps.srd, plan.adventure):
        await send_to_game(deps.broadcaster, game.id, event, audience)
    await send_to_game(deps.broadcaster, game.id, phase_event(game, work))
    for user_id in plan.user_ids:
        status = player_status_event(user_id, ActionStatus.DRAFT)
        await send_to_game(deps.broadcaster, game.id, status)


def _log_rows(deps: Deps, plan: _Plan, attempts: list[_Attempt]) -> list[DmTurnLog]:
    """Журнал попыток (спека §8.8): запрос без ключей, трасса, расход, длительность и ошибка."""
    request: dict[str, Any] = {
        "mode": plan.mode.value,
        "acting": plan.acting,
        "limits": {"max_tool_iterations": MAX_TOOL_ITERATIONS, "timeout_s": TURN_TIMEOUT_S},
    }
    if plan.context_text is not None:
        request["context"] = plan.context_text
    # Версию CLI провайдер узнаёт при первом запуске, поэтому читается после попыток, а не до них.
    if (cli_version := getattr(plan.provider, "cli_version", None)) is not None:
        request["cli_version"] = cli_version
    if plan.provider is not None:
        provider_label = plan.provider.name
    else:
        provider_label = plan.provider_name or deps.settings.llm_provider
    # Модель называет собранный провайдер после каждой попытки (`_Attempt.model`): правило выбора
    # живёт в фабрике, журнал его не повторяет. `claude_code_cli` называет модель запуска по
    # `modelUsage` ответа; у `fake`, у упавшего запуска CLI и у хода, где провайдер не собрался,
    # модели нет.
    return [
        DmTurnLog(
            game_id=plan.game_id,
            turn_id=plan.turn_id,
            attempt=plan.previous_attempts + attempt.number,
            provider=provider_label,
            model=attempt.model,
            request=request,
            trace=_trace_json(attempt.trace),
            output=attempt.text if attempt.ok else None,
            usage=_usage_json(attempt.result),
            duration_ms=attempt.duration_ms,
            error=attempt.error,
        )
        for attempt in attempts
    ]


def _trace_json(trace: list[ToolCallTrace]) -> list[dict[str, Any]]:
    """Вызовы инструментов для журнала. Тексты ошибок несут ключи и имена от модели: они проходят
    через `printable`, как и все тексты, которые потом печатаются в терминал."""
    return [
        {
            "name": call.name,
            "args": call.args,
            "result": (
                printable(call.result, MAX_TRACE_RESULT_CHARS, keep_newlines=True)
                if call.is_error
                else call.result
            ),
            "is_error": call.is_error,
        }
        for call in trace
    ]


def _usage_json(result: TurnResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    usage: Usage = result.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "stop_reason": result.stop_reason,
    }


# С чем мастер не справился, если у хода нет ходов игроков: у открытия игры их ещё нет, а на привале
# есть только реплики у костра. Для остальных видов хода игроки ходы уже сдали.
_NO_PLAYER_MOVES = {
    TurnKind.OPENING: "с началом игры",
    TurnKind.CAMP_END: "с концом привала",
}


def failure_text(mode: TurnKind, reason: str) -> str:
    """Сообщение хосту о сбое хода вида `mode`. У открытия игры и конца привала ходов игроков нет,
    поэтому говорить, что они сохранены, нельзя. Общий для сбоя хода, сбоя вплетения героев
    (`dm.weaving`) и хода, прерванного перезапуском сервера (`turns.flow`)."""
    subject = _NO_PLAYER_MOVES.get(mode)
    if subject is not None:
        return f"Мастер не справился {subject}: {reason}. Нажмите «Повторить»."
    return f"Мастер не справился с ходом: {reason}. Ходы игроков сохранены — нажмите «Повторить»."


def failed_turn_refusal(mode: TurnKind) -> str:
    """Отказ игроку, который сдаёт ход, пока мастер не справился с ходом вида `mode`: о ходах
    игроков говорит так же, как `failure_text`."""
    subject = _NO_PLAYER_MOVES.get(mode)
    if subject is not None:
        return f"Мастер не справился {subject}: хост может нажать «Повторить»."
    return "Мастер не справился с ходом: ходы сохранены, хост может нажать «Повторить»."


async def _commit_failure(deps: Deps, plan: _Plan, attempts: list[_Attempt]) -> TurnOutcome:
    """Неудачный ход: журнал и `dm_failed`; состояние игры, снимки и ходы игроков не меняются."""
    last = attempts[-1]
    reason = last.reason or REASON_INTERNAL
    async with deps.locks.lock(plan.game_id), deps.sessionmaker() as session:
        game = await session.get(Game, plan.game_id, populate_existing=True)
        turn = await session.get(Turn, plan.turn_id, populate_existing=True)
        session.add_all(_log_rows(deps, plan, attempts))
        note = None
        if game.turn_phase == TurnPhase.RESOLVING and game.current_turn_id == plan.turn_id:
            game.turn_phase = TurnPhase.DM_FAILED
            game.deadline_at = None
            turn.status = TurnStatus.FAILED
            note = Message(
                game_id=plan.game_id,
                turn_id=plan.turn_id,
                audience=game.host_user_id,
                kind=MessageKind.SYSTEM,
                text=failure_text(plan.mode, reason),
            )
            session.add(note)
        await session.commit()
        if note is not None:
            with announcing(deps, plan.game_id, plan.turn_id):
                await _announce_failure(deps, game, note)
    return TurnOutcome(plan.turn_id, TurnStatus.FAILED, game.state_version, len(attempts), reason)


async def _record_failure(deps: Deps, plan: _Plan, attempts: list[_Attempt]) -> TurnOutcome:
    """Записывает неудачу хода и не оставляет игру в `resolving`, даже если запись не удалась.

    Неудача не записывается там же, где не записался результат: диск полон, база занята. Тогда игра
    выходит из `resolving` короткой отметкой `dm_failed` (`_mark_dm_failed`): без журнала попыток и
    сообщения хосту, но хост видит сбой и может повторить. Не прошла и она — это ERROR с id игры и
    хода, а «Повторить» примет игру без живого разбора (`LiveRuns`)."""
    try:
        return await _commit_failure(deps, plan, attempts)
    except Exception as exc:
        logger.error(
            "Не удалось записать неудачу хода мастера (игра %s, ход %s): игра выходит из "
            "«мастер думает» короткой отметкой.\n%s",
            plan.game_id,
            plan.turn_id,
            safe_traceback(exc, deps.settings),
        )
    try:
        await _mark_dm_failed(deps, plan)
    except Exception as exc:
        logger.error(
            "Не удалось отметить неудачу хода мастера даже коротко (игра %s, ход %s): игра "
            "осталась в «мастер думает», хост может нажать «Повторить».\n%s",
            plan.game_id,
            plan.turn_id,
            safe_traceback(exc, deps.settings),
        )
    reason = attempts[-1].reason or REASON_INTERNAL
    return TurnOutcome(plan.turn_id, TurnStatus.FAILED, plan.version, len(attempts), reason)


async def _mark_dm_failed(deps: Deps, plan: _Plan) -> None:
    """Короткая отметка неудачи: только фаза `dm_failed` и статус хода `failed`.

    Журнала попыток и сообщения хосту здесь нет: сбой записи неудачи — это, как правило, сбой
    именно их записи, а игре нужно выйти из `resolving`. Только если игра ещё в `resolving` на этом
    же ходе (иначе её уже изменил другой путь)."""
    async with deps.locks.lock(plan.game_id), deps.sessionmaker() as session:
        game = await session.get(Game, plan.game_id, populate_existing=True)
        turn = await session.get(Turn, plan.turn_id, populate_existing=True)
        if game.turn_phase != TurnPhase.RESOLVING or game.current_turn_id != plan.turn_id:
            return
        game.turn_phase = TurnPhase.DM_FAILED
        game.deadline_at = None
        turn.status = TurnStatus.FAILED
        await session.commit()
        with announcing(deps, plan.game_id, plan.turn_id):
            await _announce_failure(deps, game, None)


async def _announce_failure(deps: Deps, game: Game, note: Message | None) -> None:
    """Рассылка после записи неудачи: сообщение хосту (если оно записано) и новая фаза игры."""
    if note is not None:
        await send_to_game(deps.broadcaster, game.id, feed_item_event(note), note.audience)
    row = await _state_of(deps, game)
    await send_to_game(deps.broadcaster, game.id, phase_event(game, row))


async def _state_of(deps: Deps, game: Game) -> GameState | None:
    async with deps.sessionmaker() as session:
        row = await session.get(GameStateSnapshot, (game.id, game.state_version))
    return None if row is None else row.to_state()


async def _record_cancellation(deps: Deps, plan: _Plan) -> None:
    """Ход отменён: попытка пишется как неудачная, игра выходит из `resolving`."""
    attempt = _Attempt(1)
    attempt.fail(REASON_CANCELLED, "CancelledError: задача хода отменена")
    # Прерванный вызов модели: провайдер называет ту модель, что знает сейчас (у CLI — никакую).
    attempt.model = getattr(plan.provider, "model", None)
    await asyncio.shield(_record_failure(deps, plan, [attempt]))
