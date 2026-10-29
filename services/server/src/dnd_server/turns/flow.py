"""Обсуждение, сбор ходов и отсчёты (план B7.4, спека §8.2, §8.4 и §12).

Игроки обсуждают ход вживую и пишут черновики: без таймера, сколько угодно. Хост нажимает «Собрать
ходы»: если все уже сдали ход или спасовали, мастер разбирает раунд сразу, иначе начинается
`collecting` с отсчётом (`games.collect_seconds`). Сбор закрывается кнопкой хоста, по концу отсчёта
или когда последний игрок сдал ход; тогда непустой черновик сдаётся сам (`auto_submitted`), пустой и
отсутствующий становятся пасом (`auto_pass`), и запускается разбор мастера (`dm.turn`).

Всё, что меняет игру, идёт под блокировкой игры (`GameLocks`) в короткой сессии и коммитом, а
события (`phase`, `player_status`, `feed_item`) уходят после коммита построителями `realtime.views`:
- сданный не тайный ход попадает в ленту всем (`action`), тайный — только автору; правка сданного
  хода, пас и смена «Тайно» снимают строку ленты надгробием, как «Перегенерировать»;
- статус игрока для всех — «пишет», «готов» или «пас», тайный ход и автоматическая сдача от ручной
  не отличаются.

Закрытие сбора не держит блокировку, пока мастер думает: оно закрывает ходы под блокировкой,
отпускает её и просит разбор (`start_turn`). Кто успел зарезервировать ход первым, тот и
разбирает; остальные получают отказ (`Conflict`, ответ 409), а отсчёт и «последний сдал» молча
отступают. Так двойное нажатие, кнопка против отсчёта и сдача последним игроком запускают мастера
ровно один раз. Строка хода игрока после закрытия (`auto_*`) правке не поддаётся: сбор для неё
уже закрыт. Путь, отпустивший блокировку до разбора (сдача, закрытие сбора, отсчёты, кнопки
«Собрать ходы» и «Закончить привал»), называет ход, который видел (`start_turn(turn_id=…)`), и
запоздавший запрос не разберёт уже следующий открытый ход: резерв отвечает ему `Conflict`.

Разбор мастера идёт фоновой задачей (`TaskSet`), а запрос хоста отвечает, когда ход зарезервирован
(фаза `resolving` уже в базе, `dm.turn.run_dm_turn(on_reserved=...)`), не дожидаясь рассказа: разбор
занимает десятки секунд, и туннелю незачем держать запрос открытым. Отмена запроса (клиент
закрыл вкладку) разбор не обрывает.

Пауза хоста (`pause`/`resume`) останавливает отсчёт и время игры. Остаток отсчёта и время игры
выводятся из `games.clock_at` (см. `Game.clock_at`). Паузу нельзя поставить, пока мастер думает:
иначе срок, который ставит конец разбора, попал бы на уже стоящие часы.

После перезапуска сервера `restore_games` ставит отсчёты заново из `games.deadline_at` и переводит
игры, застрявшие в `resolving`, в `dm_failed` с сообщением хосту (решение D3): ход сам не
перезапускается, хост повторяет его кнопкой «Повторить». Так же кнопка принимает игру в `resolving`
без живого разбора в памяти (`dm.turn.LiveRuns`): когда не записалась ни неудача хода, ни короткая
отметка `dm_failed`, перезапуск сервера не нужен.

Бой (план B7.5, спека §8.5) идёт той же машиной с другой фазой. После хода мастера в сцене боя игра
в `combat_wait`: ход пишет только игрок, чей герой ходит по инициативе, и сразу после «Готов» или
«Пас» мастер разбирает его ход вместе с ходами монстров за ним (кнопка «Собрать ходы» не нужна, она
в бою отказывает). Таймера в бою нет, пока хост не нажмёт «Поторопить» (`hurry`): тот же отсчёт
игры (ключ планировщика — игра) на `hurry_seconds`; когда он выйдет, ход героя сдаётся сам — его
черновик или «Уклоняется» (`close_hurry`). Если после хода мастера ходит монстр (бой начался с
монстра или герои не могут действовать), мастера зовёт сам сервер, подряд не больше `MAX_AUTO_TURNS`
раз; остановленный бой хост продолжает кнопкой «Повторить».

Привал (план B7.6, спека §5.5) идёт той же машиной с фазой `camp`. Когда мастер начал привал
(`start_camp`), `scene_follow_up` ведёт игру в `camp`, `dm.turn` ставит срок привала в транзакции
хода, а `_announce_next` ставит отсчёт (ключ планировщика — игра, как у сбора и боя) и зовёт
игроков. Игроки оставляют реплики героев (`camp_line`), хост продлевает привал (`camp_extend`)
или заканчивает его (`camp_end`). Конец привала — только отсчёт (`on_deadline`) или хост: оба идут в
`close_camp`, который просит ход мастера «конец привала»; отдых входит в этот ход (`dm.turn`
и `dm.camp_flow`). Когда ход зарезервирован, отсчёт игры снимается: срок в базе уже обнулён.

Привалы по кнопке хоста (DND-2 §13, блок B9). Хост в обсуждении вне боя начинает короткий или
долгий привал (`start_rest`) без хода мастера: под блокировкой игры движок меняет сцену
(`campfire.begin_rest`), и одной транзакцией пишутся новая версия состояния, открытый ход
«конец привала», фаза `camp` и срок вида привала (`games.short_rest_seconds` или
`games.camp_seconds`). Привал любого вида начинается кадром `rest_started` всем, за ним фоновой
задачей идут личные идеи для рассказа (`dm.rest_ideas`). На коротком привале герой тратит кости
хитов (`spend_hit_die`): бросает генератор сервера, новая версия состояния, карточка броска всем.
Дальше привал идёт так же, как привал мастера, и кончается тем же ходом «конец привала»; вид
отдыха движок берёт из сцены, поэтому перезапуск сервера сохраняет и вид, и остаток срока.

Начало игры (план B7.8, спека §8.1 и §10) идёт той же машиной. `FlowHooks` — `GameHooks` приложения:
после «Начать» (игра в `weaving`) он запускает вплетение героев и открытие игры (`start_weaving`,
`dm.weaving.begin_weaving`) фоновой задачей и возвращается, когда вплетение зарезервировано, а за
удавшимся открытием идёт то же, что за любым ходом мастера (`_follow`), с сообщением бота «Игра
началась». Сорвавшееся вплетение оставляет игру в `weaving` с фазой `dm_failed` и без хода: хост
нажимает «Повторить» (`retry` выбирает вплетение, пока хода нет, и ход, когда он есть). Вплетение
после перезапуска сервера само не продолжается (решение D3): `restore_games` отмечает такие игры
`dm_failed` с сообщением хосту, а продолжает «Повторить» с места остановки.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from dnd_engine import rest as rest_rules
from dnd_engine.character import Character
from dnd_engine.events import RollEvent
from dnd_engine.modules import build_ruleset
from dnd_engine.modules.campfire import begin_rest
from dnd_engine.state import GameState, SceneType
from dnd_server.bot.notify import (
    Notifier,
    camp_text,
    collecting_text,
    combat_turn_text,
    game_continues_text,
    game_started_text,
    hurry_text,
)
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
)
from dnd_server.dm.camp_flow import (
    CAMP_EXTENSION,
    CAMP_LINES_PER_HERO,
    RestKind,
    is_short_rest,
    rest_kind,
    rest_seconds,
)
from dnd_server.dm.combat_flow import (
    DODGE_TEXT,
    MAX_AUTO_TURNS,
    master_moves_next,
    waiting_hero,
)
from dnd_server.dm.rest_ideas import IDEAS_TASK_FAILED, offer_rest_ideas
from dnd_server.dm.text import safe_traceback
from dnd_server.dm.turn import (
    Deps,
    TurnOutcome,
    failed_turn_refusal,
    failure_text,
    game_adventure,
    regenerate_last_turn,
    run_dm_turn,
)
from dnd_server.dm.weaving import begin_weaving
from dnd_server.games.hooks import send_to_game
from dnd_server.games.service import Conflict, Forbidden, GameError, Invalid, NotFound
from dnd_server.realtime.views import (
    action_auto_submitted_event,
    can_regenerate,
    character_event,
    feed_entries,
    feed_item_event,
    feed_withdrawn_event,
    party_event,
    phase_event,
    player_status_event,
    public_status,
    rest_started_event,
)
from dnd_server.turns.timers import Action, Scheduler, TaskSet

logger = logging.getLogger(__name__)

# «+1 минута» хоста (спека §8.2).
COLLECT_EXTENSION = timedelta(seconds=60)
# Статусы хода, по которым игрок «готов» для досрочного закрытия сбора, и статусы закрытого сбора.
READY_STATUSES = (ActionStatus.SUBMITTED, ActionStatus.PASS)
SHOWN_STATUSES = (ActionStatus.SUBMITTED, ActionStatus.AUTO_SUBMITTED)
CLOSED_STATUSES = (ActionStatus.AUTO_SUBMITTED, ActionStatus.AUTO_PASS)

# Подпись карточки броска кости хитов (дополнение контракта B9, §3).
HIT_DIE_LABEL = "Кость хитов"

SERVER_RESTARTED = "Сервер перезапущен во время хода: попытка прервана"
MASTER_LOST = "У разбора нет мастера: не записались ни результат, ни неудача — попытка прервана"

Delivery = tuple[dict[str, Any], str]  # событие и его адресат (`all` или id пользователя)


def scene_follow_up(state: GameState) -> tuple[TurnPhase, TurnKind]:
    """Фаза и ход после удавшегося хода мастера по сцене (шов `Deps.follow_up`).

    Идёт бой — `combat_wait`: ход ждёт героя по инициативе (если ходит монстр, мастера зовёт
    сервер, см. `_master`). Идёт привал — `camp`, а открытый ход — «конец привала»: его разберёт
    мастер, когда привал кончится (срок привала ставит `dm.turn`, отсчёт — `_announce_next`)."""
    if state.scene.type == SceneType.COMBAT and state.combat is not None:
        return TurnPhase.COMBAT_WAIT, TurnKind.COMBAT
    if state.scene.type == SceneType.CAMP:
        return TurnPhase.CAMP, TurnKind.CAMP_END
    return TurnPhase.DISCUSSION, TurnKind.ROUND


@dataclass(frozen=True)
class Flow:
    """Зависимости сбора ходов: всё, что нужно мастеру (`Deps`), плюс отсчёты, фоновые задачи и бот.

    Собирается из приложения при каждом обращении (`from_app`), поэтому подмена частей
    `app.state` в тестах подхватывается. `app` хранится затем, чтобы отсчёт, сработав, взял свежие
    зависимости, а не те, что были при его постановке."""

    dm: Deps
    scheduler: Scheduler
    notifier: Notifier
    tasks: TaskSet
    app: FastAPI

    @classmethod
    def from_app(cls, app: FastAPI) -> Flow:
        state = app.state
        return cls(Deps.from_app(app), state.scheduler, state.notifier, state.turn_tasks, app)


# --- общие помощники ---


@asynccontextmanager
async def _locked(flow: Flow, game_id: str) -> AsyncIterator[AsyncSession]:
    """Короткая сессия под блокировкой игры: читать, проверять и менять игру можно только так."""
    async with flow.dm.locks.lock(game_id), flow.dm.sessionmaker() as session:
        yield session


async def _send(flow: Flow, game_id: str, deliveries: Iterable[Delivery]) -> None:
    for payload, audience in deliveries:
        await send_to_game(flow.dm.broadcaster, game_id, payload, audience)


async def _state_of(session: AsyncSession, game: Game) -> GameState | None:
    row = await session.get(GameStateSnapshot, (game.id, game.state_version))
    return None if row is None else row.to_state()


async def _phase_delivery(session: AsyncSession, game: Game) -> Delivery:
    """Событие `phase` для всех: с признаком «можно перегенерировать» по ходам игры в базе."""
    regenerable = await can_regenerate(session, game)
    state = await _state_of(session, game)
    return phase_event(game, state, can_regenerate=regenerable), AUDIENCE_ALL


async def _member(session: AsyncSession, game_id: str, user_id: str) -> tuple[Game, GamePlayer]:
    """Игра и строка игрока; чужая игра и несуществующая отвечают одинаково."""
    game = await session.get(Game, game_id)
    player = await session.get(GamePlayer, (game_id, user_id))
    if game is None or player is None:
        raise NotFound("Игра не найдена.")
    return game, player


async def _host(session: AsyncSession, game_id: str, user_id: str) -> Game:
    game, player = await _member(session, game_id, user_id)
    if not player.is_host:
        raise Forbidden("Эта кнопка доступна только хосту.")
    return game


def _require_playing(game: Game, *, weaving_too: bool = False) -> None:
    """Игра идёт; `weaving_too` пускает и игру в начале (открытие игры ещё в `weaving`)."""
    if game.phase == GamePhase.PAUSED:
        raise Conflict("Игра на паузе: хост должен нажать «Продолжить».")
    allowed = (GamePhase.WEAVING, GamePhase.PLAYING) if weaving_too else (GamePhase.PLAYING,)
    if game.phase not in allowed:
        raise Conflict("Игра сейчас не идёт.")


async def _current_turn(session: AsyncSession, game: Game) -> Turn:
    turn = await session.get(Turn, game.current_turn_id) if game.current_turn_id else None
    if turn is None or turn.game_id != game.id:
        raise Conflict("Текущий ход игры не найден.")
    return turn


async def _players(session: AsyncSession, game_id: str) -> list[GamePlayer]:
    rows = await session.scalars(
        select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.joined_order)
    )
    return list(rows)


async def _actions(session: AsyncSession, turn_id: str) -> dict[str, PlayerAction]:
    """Ходы игроков хода по id пользователя."""
    rows = await session.scalars(select(PlayerAction).where(PlayerAction.turn_id == turn_id))
    return {row.user_id: row for row in rows}


async def _everyone_ready(session: AsyncSession, game: Game, turn: Turn) -> bool:
    """Все игроки игры сдали ход или спасовали."""
    rows = await _actions(session, turn.id)
    players = await _players(session, game.id)
    return all(
        player.user_id in rows and rows[player.user_id].status in READY_STATUSES
        for player in players
    )


async def _player_of(session: AsyncSession, game_id: str, character_id: str) -> GamePlayer | None:
    return await session.scalar(
        select(GamePlayer).where(
            GamePlayer.game_id == game_id, GamePlayer.character_id == character_id
        )
    )


def _still_writing(rows: dict[str, PlayerAction], user_id: str) -> bool:
    """Игрок ещё пишет: строки хода нет или это черновик."""
    row = rows.get(user_id)
    return row is None or row.status == ActionStatus.DRAFT


def _status_delivery(user_id: str, row: PlayerAction) -> Delivery:
    """Публичный статус игрока по его строке хода — всем (`realtime.views.public_status`)."""
    return player_status_event(user_id, row), AUDIENCE_ALL


def _arm(flow: Flow, game: Game) -> None:
    """Ставит отсчёт на срок игры: когда он выйдет, закроется сбор именно этого хода."""
    assert game.deadline_at is not None and game.current_turn_id is not None
    flow.scheduler.schedule(
        game.id, game.deadline_at, _on_expiry(flow.app, game.id, game.current_turn_id)
    )


def _on_expiry(app: FastAPI, game_id: str, turn_id: str) -> Action:
    async def fire() -> None:
        await on_deadline(Flow.from_app(app), game_id, turn_id)

    return fire


async def _notify(flow: Flow, user_ids: Iterable[str], text: str) -> None:
    """Уведомления бота: необязательны, сбой одного не мешает остальным и игре."""
    results = await asyncio.gather(
        *(flow.notifier.notify(user_id, text) for user_id in user_ids), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Уведомление игроку не отправлено: %s", type(result).__name__)


# --- ход игрока ---


async def _turn_for_action(
    session: AsyncSession, game_id: str, user_id: str
) -> tuple[Game, Turn, GamePlayer]:
    """Игра, открытый ход и игрок, если сейчас игрок может писать, сдавать и пасовать."""
    game, player = await _member(session, game_id, user_id)
    _require_playing(game)
    match game.turn_phase:
        case TurnPhase.DISCUSSION | TurnPhase.COLLECTING:
            pass
        case TurnPhase.COMBAT_WAIT:
            await _require_acting_hero(session, game, player)
        case TurnPhase.RESOLVING:
            raise Conflict("Мастер разбирает ход: сейчас ходы не принимаются.")
        case TurnPhase.DM_FAILED:
            turn = await _current_turn(session, game)
            raise Conflict(failed_turn_refusal(turn.kind))
        case TurnPhase.CAMP:
            raise Conflict("Сейчас привал: ходы не принимаются.")
        case _:
            raise Conflict("Сейчас ходы не принимаются.")
    return game, await _current_turn(session, game), player


async def _require_acting_hero(session: AsyncSession, game: Game, player: GamePlayer) -> None:
    """В бою пишет только игрок, чей герой ходит по инициативе и может действовать.

    «Чей ход» решает `waiting_hero`, как у бота, «Поторопить» и «Повторить»: парализованный
    текущий герой ходить не может, ход монстров запускает хост, и «Готов» его игрока не должен
    сделать то же за ним (платный вызов мастера)."""
    state = await _state_of(session, game)
    hero = None if state is None else waiting_hero(state)
    if hero is None or hero.id != player.character_id:
        raise Conflict("Сейчас не ваша очередь: ваш герой не ходит.")


async def _has_inspiration(session: AsyncSession, game: Game, player: GamePlayer) -> bool:
    state = await _state_of(session, game)
    hero = None if state is None else state.party.get(player.character_id or "")
    return hero is not None and hero.inspiration


async def _sync_feed(
    session: AsyncSession, game: Game, turn: Turn, player: GamePlayer, row: PlayerAction
) -> list[Delivery]:
    """Строка хода в ленте соответствует сданному ходу: есть, пока он сдан, и снимается надгробием
    при правке, пасе и смене «Тайно». Тайный ход виден только автору, остальные — всем."""
    character_id = player.character_id or ""
    shown = row.status in SHOWN_STATUSES
    audience = row.user_id if row.secret else AUDIENCE_ALL
    lines = await session.scalars(
        select(Message).where(
            Message.turn_id == turn.id,
            Message.kind == MessageKind.ACTION,
            Message.superseded.is_(False),
        )
    )
    deliveries: list[Delivery] = []
    keep: Message | None = None
    for line in lines:
        if (line.data or {}).get("actor_id") != character_id:
            continue
        if shown and keep is None and (line.text, line.audience) == (row.text, audience):
            keep = line
            continue
        line.superseded = True
        deliveries.append((feed_withdrawn_event(line.id), line.audience))
    if shown and keep is None:
        state = await _state_of(session, game)
        hero = None if state is None else state.party.get(character_id)
        line = Message(
            game_id=game.id,
            turn_id=turn.id,
            audience=audience,
            kind=MessageKind.ACTION,
            text=row.text,
            data={
                "actor_id": character_id,
                "actor": hero.name if hero is not None else character_id,
                "secret": row.secret,
            },
        )
        session.add(line)
        await session.flush()
        deliveries.append((feed_item_event(line), audience))
    return deliveries


async def save_draft(
    flow: Flow, game_id: str, user_id: str, *, text: str, secret: bool, inspiration: bool
) -> PlayerAction:
    """Черновик хода. Любая правка возвращает сданный ход или пас в черновик.

    «Вдохновение» без вдохновения у героя тихо снимается: клиент мог показать галочку по
    устаревшим данным. Статус игрока всем уходит, только если публичный статус сменился («не
    пишет» ↔ «пишет», сданный или пас → черновик): правка текста черновика событий не даёт."""
    async with _locked(flow, game_id) as session:
        game, turn, player = await _turn_for_action(session, game_id, user_id)
        row = await session.get(PlayerAction, (turn.id, user_id))
        if row is not None and row.status in CLOSED_STATUSES:
            raise Conflict("Сбор ходов закрыт: мастер уже разбирает раунд.")
        inspiration = inspiration and await _has_inspiration(session, game, player)
        deliveries: list[Delivery] = []
        shown_before = public_status(row)
        if row is None:
            row = PlayerAction(
                turn_id=turn.id,
                user_id=user_id,
                text=text,
                secret=secret,
                inspiration=inspiration,
                status=ActionStatus.DRAFT,
            )
            session.add(row)
        else:
            was_writing = row.status == ActionStatus.DRAFT
            row.text, row.secret, row.inspiration = text, secret, inspiration
            row.status = ActionStatus.DRAFT
            if not was_writing:
                deliveries += await _sync_feed(session, game, turn, player, row)
        if public_status(row) != shown_before:
            deliveries.append(_status_delivery(user_id, row))
        await session.commit()
        await _send(flow, game_id, deliveries)
    return row


async def submit_action(flow: Flow, game_id: str, user_id: str) -> PlayerAction:
    """«Готов»: сдать написанный ход. Без текста сдавать нечего (`Invalid`, 422)."""
    async with _locked(flow, game_id) as session:
        game, turn, player = await _turn_for_action(session, game_id, user_id)
        row = await session.get(PlayerAction, (turn.id, user_id))
        if row is not None and row.status in CLOSED_STATUSES:
            raise Conflict("Сбор ходов закрыт: мастер уже разбирает раунд.")
        if row is None or not row.text.strip():
            raise Invalid("Напишите ход или нажмите «Пас».")
        deliveries: list[Delivery] = []
        row.text = row.text.strip()
        if row.status != ActionStatus.SUBMITTED:
            row.status = ActionStatus.SUBMITTED
            deliveries.append(_status_delivery(user_id, row))
        deliveries += await _sync_feed(session, game, turn, player, row)
        in_combat = game.turn_phase == TurnPhase.COMBAT_WAIT
        last_one = game.turn_phase == TurnPhase.COLLECTING and await _everyone_ready(
            session, game, turn
        )
        await session.commit()
        await _send(flow, game_id, deliveries)
    if in_combat:
        await _play_hero_turn(flow, game_id, turn.id)
    elif last_one:
        await _close_when_ready(flow, game_id, turn.id)
    return row


async def pass_action(flow: Flow, game_id: str, user_id: str) -> PlayerAction:
    """«Пас»: герой выжидает. Черновик остаётся, чтобы «Изменить» вернул его."""
    async with _locked(flow, game_id) as session:
        game, turn, player = await _turn_for_action(session, game_id, user_id)
        row = await session.get(PlayerAction, (turn.id, user_id))
        if row is not None and row.status in CLOSED_STATUSES:
            raise Conflict("Сбор ходов закрыт: мастер уже разбирает раунд.")
        deliveries: list[Delivery] = []
        if row is None:
            row = PlayerAction(turn_id=turn.id, user_id=user_id, text="", status=ActionStatus.PASS)
            session.add(row)
            deliveries.append(_status_delivery(user_id, row))
        elif row.status != ActionStatus.PASS:
            row.status = ActionStatus.PASS
            deliveries += await _sync_feed(session, game, turn, player, row)
            deliveries.append(_status_delivery(user_id, row))
        in_combat = game.turn_phase == TurnPhase.COMBAT_WAIT
        last_one = game.turn_phase == TurnPhase.COLLECTING and await _everyone_ready(
            session, game, turn
        )
        await session.commit()
        await _send(flow, game_id, deliveries)
    if in_combat:
        await _play_hero_turn(flow, game_id, turn.id)
    elif last_one:
        await _close_when_ready(flow, game_id, turn.id)
    return row


async def _play_hero_turn(flow: Flow, game_id: str, turn_id: str) -> None:
    """Игрок сдал ход `turn_id` в бою: отсчёт «Поторопить» снимается, мастер разбирает ход героя и
    ходы монстров за ним. Если другой путь успел первым (отсчёт, «Перегенерировать»), ход игрока уже
    записан, и отказ не ошибка."""
    flow.scheduler.cancel(game_id)
    try:
        await start_turn(flow, game_id, mode=TurnKind.COMBAT, turn_id=turn_id)
    except GameError as refusal:
        logger.debug("Боевой ход игры %s не запущен: %s", game_id, refusal)


async def _close_when_ready(flow: Flow, game_id: str, turn_id: str) -> None:
    """Последний игрок сдал ход: сбор закрывается раньше срока. Если закрытие уже идёт другим
    путём (кнопка, отсчёт), это не ошибка: игрок свой ход сдал."""
    try:
        await close_collecting(flow, game_id, turn_id=turn_id)
    except GameError as refusal:
        logger.debug("Досрочное закрытие сбора игры %s не понадобилось: %s", game_id, refusal)


# --- разбор мастера ---


async def start_turn(
    flow: Flow,
    game_id: str,
    *,
    mode: TurnKind | None,
    acting: list[str] | None = None,
    regenerate: bool = False,
    turn_id: str | None = None,
) -> None:
    """Запускает разбор мастера фоновой задачей и возвращается, когда ход зарезервирован.

    Отказ (`NotFound`, `Conflict`: игра не идёт, мастер уже разбирает, ход разобран) поднимается
    вызывающему, и ничего не запущено. Дальше мастер идёт сам; его итог (рассказ или `dm_failed`)
    приходит игрокам событиями, а сбой разбора пишет `dm.turn`. `turn_id` — ход, который видел
    вызывающий, когда решил позвать мастера: пути, отпустившие блокировку игры до этого вызова
    (сдача, закрытие сбора, отсчёты), называют его, и разобран будет только он."""
    await _spawn_until_reserved(
        flow,
        lambda reserved: _master(
            flow,
            game_id,
            mode=mode,
            acting=acting,
            regenerate=regenerate,
            turn_id=turn_id,
            reserved=reserved,
        ),
    )


async def start_weaving(flow: Flow, game_id: str) -> None:
    """Запускает вплетение героев и открытие игры (`dm.weaving`) так же, как `start_turn` разбор.

    Возвращается, когда игра уже «разбирается» (вплетение зарезервировано), остальное идёт фоновой
    задачей. Отказ (`NotFound`, `Conflict`: игра не в начале, вплетение уже идёт) поднимается
    вызывающему, и ничего не запущено."""
    await _spawn_until_reserved(flow, lambda reserved: _weave(flow, game_id, reserved))


async def _spawn_until_reserved(
    flow: Flow, work: Callable[[asyncio.Event], Coroutine[Any, Any, None]]
) -> None:
    """Фоновая задача `work(reserved)`; возврат, когда она сообщила о резерве или кончилась."""
    reserved = asyncio.Event()
    task = flow.tasks.spawn(work(reserved))
    waiter = asyncio.ensure_future(reserved.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task.done() and not reserved.is_set():
        task.result()  # отказ до резерва: исключение уходит тому, кто просил


async def _master(
    flow: Flow,
    game_id: str,
    *,
    mode: TurnKind | None,
    acting: list[str] | None,
    regenerate: bool,
    turn_id: str | None,
    reserved: asyncio.Event,
) -> None:
    def on_reserved() -> None:
        # Резерв обнулил срок игры в базе, поэтому отсчёт, что шёл (привал, который закончили или
        # откатили «Перегенерировать», «Поторопить»), больше не нужен: иначе он сработал бы зря.
        flow.scheduler.cancel(game_id)
        reserved.set()

    if regenerate:
        outcome = await regenerate_last_turn(game_id, deps=flow.dm, on_reserved=on_reserved)
    else:
        assert mode is not None
        outcome = await run_dm_turn(
            game_id,
            mode=mode,
            deps=flow.dm,
            acting=acting,
            on_reserved=on_reserved,
            expected_turn_id=turn_id,
        )
    await _follow(flow, game_id, outcome)


async def _weave(flow: Flow, game_id: str, reserved: asyncio.Event) -> None:
    """Вплетение, открытие игры и то, что идёт за удавшимся ходом, как после любого хода мастера."""
    outcome = await begin_weaving(game_id, deps=flow.dm, on_reserved=reserved.set)
    if outcome is not None:  # `None`: вплетение сорвалось, хост получил сообщение и повторит
        await _follow(flow, game_id, outcome, opening=True)


async def _follow(flow: Flow, game_id: str, outcome: TurnOutcome, *, opening: bool = False) -> None:
    """После хода мастера: позвать игроков, а если в бою ходят монстры, сразу запустить мастера."""
    chained = 0
    while outcome.status == TurnStatus.DONE and await _announce_next(
        flow, game_id, started=opening and chained == 0
    ):
        # Ходят монстры: игрока ждать не нужно, мастера зовёт сервер (бой начался с монстра или
        # герои не могут действовать). Предел защищает от бесконечной цепочки платных вызовов.
        if chained >= MAX_AUTO_TURNS:
            logger.warning(
                "Игра %s: мастер сыграл %d боевых ходов подряд без игрока, цепочка остановлена.",
                game_id,
                MAX_AUTO_TURNS,
            )
            return
        chained += 1
        outcome = await run_dm_turn(game_id, mode=TurnKind.COMBAT, deps=flow.dm)


async def _announce_next(flow: Flow, game_id: str, *, started: bool = False) -> bool:
    """Зовёт игроков после удавшегося хода мастера; `True` — в бою ходят монстры и мастера надо
    запустить снова. `started` — это открытие игры: игрокам сообщают, что игра началась.

    В бою «Твой ход в бою» получает один игрок, чей герой ходит; остальные не получают ничего,
    чтобы бой не засыпал чат уведомлениями. Начался привал — отсчёт ставится на срок из базы, а
    игроков зовут к костру. Иначе «Игра продолжается» идёт всем игрокам."""
    async with flow.dm.sessionmaker() as session:
        game = await session.get(Game, game_id)
        state = None if game is None else await _state_of(session, game)
        players = await _players(session, game_id)
    if game is not None and game.turn_phase == TurnPhase.CAMP and state is not None:
        await _begin_camp(flow, game, [player.user_id for player in players], rest_kind(state))
        return False
    if game is not None and game.turn_phase == TurnPhase.COMBAT_WAIT and state is not None:
        hero = waiting_hero(state)
        if hero is None:
            return master_moves_next(state)
        whose = [player.user_id for player in players if player.character_id == hero.id]
        await _notify(flow, whose, combat_turn_text())
        return False
    text = game_started_text() if started else game_continues_text()
    await _notify(flow, [player.user_id for player in players], text)
    return False


async def _begin_camp(flow: Flow, game: Game, user_ids: list[str], kind: RestKind) -> None:
    """Мастер начал привал: отсчёт на срок игры и зов к костру. Если хост успел поставить игру на
    паузу, отсчёта нет (его вернёт «Продолжить»), а звать к костру рано."""
    if game.phase != GamePhase.PLAYING or game.deadline_at is None:
        return
    _arm(flow, game)
    await _call_to_camp(flow, game, user_ids, kind)


async def _call_to_camp(flow: Flow, game: Game, user_ids: list[str], kind: RestKind) -> None:
    """Зов к костру: сколько осталось и что делать на привале этого вида."""
    assert game.deadline_at is not None
    left = round((game.deadline_at - flow.scheduler.now()).total_seconds())
    logger.info("Игра %s: привал (%s), отсчёт %d с.", game.id, kind, rest_seconds(game, kind))
    await _notify(flow, user_ids, camp_text(left, short=kind == "short"))


async def phase_now(flow: Flow, game_id: str) -> dict[str, Any]:
    """Фаза игры, как она сейчас в базе (ответ кнопок хоста)."""
    async with flow.dm.sessionmaker() as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        return (await _phase_delivery(session, game))[0]["phase"]


# --- кнопки хоста ---


async def collect(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Собрать ходы»: разбор сразу, если все готовы, иначе сбор с отсчётом."""
    run_now: TurnKind | None = None
    to_call: list[str] = []
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase == TurnPhase.COLLECTING:
            raise Conflict("Сбор ходов уже идёт.")
        if game.turn_phase != TurnPhase.DISCUSSION:
            raise Conflict("Собирать ходы можно, пока игроки обсуждают их.")
        game_adventure(game)  # битая копия — отказ до сбора: ход по ней не начнётся
        turn = await _current_turn(session, game)
        if await _everyone_ready(session, game, turn):
            run_now = turn.kind
        else:
            game.set_turn_phase(TurnPhase.COLLECTING, flow.scheduler.now())
            game.deadline_at = flow.scheduler.now() + timedelta(seconds=game.collect_seconds)
            turn.status = TurnStatus.COLLECTING
            rows = await _actions(session, turn.id)
            to_call = [
                player.user_id
                for player in await _players(session, game_id)
                if player.user_id != user_id and _still_writing(rows, player.user_id)
            ]
            await session.commit()
            _arm(flow, game)
            await _send(flow, game_id, [await _phase_delivery(session, game)])
            logger.info("Игра %s: сбор ходов, отсчёт %d с.", game_id, game.collect_seconds)
    if run_now is not None:
        await start_turn(flow, game_id, mode=run_now, turn_id=turn.id)
    else:
        await _notify(flow, to_call, collecting_text(game.collect_seconds))
    return await phase_now(flow, game_id)


async def close(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Закрыть сейчас»: закрыть сбор и отдать ходы мастеру."""
    async with _locked(flow, game_id) as session:
        await _host(session, game_id, user_id)
    await close_collecting(flow, game_id)
    return await phase_now(flow, game_id)


async def close_collecting(
    flow: Flow, game_id: str, *, turn_id: str | None = None, due: bool = False
) -> None:
    """Закрывает сбор ходов и просит разбор. Отказ — `GameError`: сбора нет, ход уже другой,
    другой путь закрыл сбор первым.

    `turn_id` — какой ход закрывает вызывающий (отсчёт и «последний сдал» закрывают только свой
    ход); `due` — закрывает отсчёт: если срок игры успели отодвинуть («+1 минута»), он ничего не
    закрывает."""
    async with _locked(flow, game_id) as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        _require_playing(game)
        if game.turn_phase != TurnPhase.COLLECTING:
            raise Conflict("Сбор ходов не идёт.")
        if turn_id is not None and game.current_turn_id != turn_id:
            raise Conflict("Это уже другой ход.")
        if due and game.deadline_at is not None and game.deadline_at > flow.scheduler.now():
            raise Conflict("Срок отсчёта ещё не вышел.")
        # Битая копия — отказ до закрытия: сбор, закрытый без хода, не открыть заново.
        game_adventure(game)
        turn = await _current_turn(session, game)
        deliveries = await _finish_actions(session, game, turn)
        await session.commit()
        flow.scheduler.cancel(game_id)
        await _send(flow, game_id, deliveries)
        mode = turn.kind
    logger.info("Игра %s: сбор ходов закрыт, ход %s.", game_id, turn.id)
    await start_turn(flow, game_id, mode=mode, turn_id=turn.id)


async def _finish_actions(session: AsyncSession, game: Game, turn: Turn) -> list[Delivery]:
    """Закрытие сбора: непустой черновик сдаётся сам, пустой — пас, игроку без строки хода
    сервер создаёт пас (мастер не видит игрока без строки). Автор сданного черновика получает
    «Ход сдан» (`action_auto_submitted`) — только он и после коммита, как всё из этого списка."""
    rows = await _actions(session, turn.id)
    deliveries: list[Delivery] = []
    for player in await _players(session, game.id):
        row = rows.get(player.user_id)
        if row is None:
            row = PlayerAction(
                turn_id=turn.id, user_id=player.user_id, text="", status=ActionStatus.AUTO_PASS
            )
            session.add(row)
        elif row.status == ActionStatus.DRAFT:
            filled = bool(row.text.strip())
            row.status = ActionStatus.AUTO_SUBMITTED if filled else ActionStatus.AUTO_PASS
        else:
            continue
        deliveries.append(_status_delivery(player.user_id, row))
        if row.status == ActionStatus.AUTO_SUBMITTED:
            deliveries += await _sync_feed(session, game, turn, player, row)
            deliveries.append((action_auto_submitted_event(), player.user_id))
    return deliveries


# --- привал ---


async def camp_line(flow: Flow, game_id: str, user_id: str, text: str) -> Message:
    """Реплика героя у костра: сообщение ленты `camp_line` для всех и вход хода «конец привала».

    Любой игрок, пока привал идёт. Число реплик героя за привал ограничено
    (`CAMP_LINES_PER_HERO`), длину ограничивает тело запроса (`CAMP_LINE_MAX_CHARS`). Мастер
    реплику не разбирает: он ответит на все реплики сразу, в ходе «конец привала»."""
    async with _locked(flow, game_id) as session:
        game, player = await _member(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase != TurnPhase.CAMP:
            raise Conflict("Сейчас нет привала: реплики у костра не принимаются.")
        line = text.strip()
        if not line:
            raise Invalid("Напишите реплику своего героя.")
        state = await _state_of(session, game)
        hero = None if state is None else state.party.get(player.character_id or "")
        if hero is None:
            raise Conflict("У вас нет героя в этой игре.")
        spoken = await session.scalars(
            select(Message.data).where(
                Message.turn_id == game.current_turn_id,
                Message.kind == MessageKind.CAMP_LINE,
                Message.superseded.is_(False),
            )
        )
        said = sum(1 for data in spoken if (data or {}).get("actor_id") == hero.id)
        if said >= CAMP_LINES_PER_HERO:
            raise Conflict("Вы уже сказали у костра достаточно: подождите утра.")
        message = Message(
            game_id=game_id,
            turn_id=game.current_turn_id,
            audience=AUDIENCE_ALL,
            kind=MessageKind.CAMP_LINE,
            text=line,
            data={"actor_id": hero.id, "actor": hero.name},
        )
        session.add(message)
        await session.flush()
        delivery = (feed_item_event(message), AUDIENCE_ALL)
        await session.commit()
        await _send(flow, game_id, [delivery])
    return message


async def start_rest(flow: Flow, game_id: str, user_id: str, kind: RestKind) -> dict[str, Any]:
    """«Короткий привал» или «Долгий привал» хоста: привал начинается сразу, без хода мастера.

    Только хост, только в обсуждении идущей игры вне боя и без уже отправленных ходов игроков:
    иначе отказ (`Conflict`, 409). Остальные отказы — правила движка (`RestError`: бой в состоянии,
    уже привал, предел коротких привалов приключения). Всё под блокировкой игры одной
    транзакцией: новая версия состояния со сценой `camp` и видом привала, открытый ход становится
    «концом привала», фаза `camp`, срок вида. После коммита — фаза и `rest_started` всем, отсчёт,
    зов к костру и фоновая задача идей для рассказа."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase == TurnPhase.CAMP:
            raise Conflict("Привал уже идёт: второй начать нельзя.")
        if game.turn_phase == TurnPhase.COMBAT_WAIT:
            raise Conflict("В бою привал невозможен: сначала закончите бой.")
        if game.turn_phase != TurnPhase.DISCUSSION:
            raise Conflict("Начать привал можно, пока игроки обсуждают ход.")
        turn = await _current_turn(session, game)
        rows = await _actions(session, turn.id)
        if any(row.status == ActionStatus.SUBMITTED for row in rows.values()):
            raise Conflict("Игроки уже отправили ходы: сначала разберите раунд.")
        state = await _state_of(session, game)
        if state is None:
            raise Conflict("У игры ещё нет состояния: привал начать нельзя.")
        adventure = game_adventure(game)
        try:
            camped, _events = begin_rest(
                state, kind, build_ruleset(state.rules), content=flow.dm.srd, adventure=adventure
            )
        except rest_rules.RestError as refusal:
            raise Conflict(str(refusal)) from None
        await _save_state(session, game, camped)
        turn.kind = TurnKind.CAMP_END
        game.set_turn_phase(TurnPhase.CAMP, flow.scheduler.now())
        game.deadline_at = flow.scheduler.now() + timedelta(seconds=rest_seconds(game, kind))
        game.rest_ideas = None
        await session.commit()
        _arm(flow, game)
        started = (rest_started_event(game, camped), AUDIENCE_ALL)
        await _send(flow, game_id, [await _phase_delivery(session, game), started])
        user_ids = [player.user_id for player in await _players(session, game_id)]
    logger.info("Игра %s: хост начал привал (%s).", game_id, kind)
    flow.tasks.spawn(
        offer_rest_ideas(game_id, turn.id, deps=flow.dm), failure_text=IDEAS_TASK_FAILED
    )
    await _call_to_camp(flow, game, user_ids, kind)
    return await phase_now(flow, game_id)


async def spend_hit_die(flow: Flow, game_id: str, user_id: str) -> Character:
    """«Потратить кость хитов»: герой игрока тратит одну кость на коротком привале.

    Бросает генератор сервера (`Deps.rng`), правила считает движок (`rest.spend_hit_die`); клиент
    чисел не присылает. Под блокировкой игры одной транзакцией: новая версия состояния и карточка
    броска для всех (сообщение вне хода мастера: «Перегенерировать» её не снимает, а сама
    «Перегенерировать» после неё отказывает). После коммита — карточка и отряд всем, лист героя его
    игроку. Отказ (409): не короткий привал, нет героя, герой погиб, костей не осталось."""
    async with _locked(flow, game_id) as session:
        game, player = await _member(session, game_id, user_id)
        _require_playing(game)
        state = await _state_of(session, game)
        if game.turn_phase != TurnPhase.CAMP or state is None or not is_short_rest(state):
            raise Conflict("Кости хитов тратят только на коротком привале.")
        hero = state.party.get(player.character_id or "")
        if hero is None:
            raise Conflict("У вас нет героя в этой игре.")
        adventure = game_adventure(game)  # битая копия — отказ до броска, а не после
        try:
            rested, rolled = rest_rules.spend_hit_die(hero, flow.dm.srd, flow.dm.rng)
        except rest_rules.RestError as refusal:
            raise Conflict(str(refusal)) from None
        after = state.model_copy(deep=True)
        after.party[hero.id] = rested
        await _save_state(session, game, after)
        card = RollEvent(audience=AUDIENCE_ALL, actor_id=hero.id, label=HIT_DIE_LABEL, roll=rolled)
        entries = feed_entries(
            [card],
            before=state,
            after=after,
            adventure=adventure,
            srd=flow.dm.srd,
            secret_actors=(),
        )
        messages = [
            Message(
                game_id=game_id,
                turn_id=None,
                audience=entry.audience,
                kind=entry.kind,
                text=entry.text,
                data=entry.data,
            )
            for entry in entries
        ]
        session.add_all(messages)
        await session.flush()
        await session.commit()
        deliveries: list[Delivery] = [(feed_item_event(m), m.audience) for m in messages]
        deliveries.append((party_event(after, flow.dm.srd), AUDIENCE_ALL))
        deliveries.append((character_event(rested, flow.dm.srd, adventure, after), user_id))
        await _send(flow, game_id, deliveries)
    logger.info("Игра %s: герой %s потратил кость хитов.", game_id, hero.id)
    return rested


async def _save_state(session: AsyncSession, game: Game, state: GameState) -> None:
    """Новая версия состояния игры вне хода мастера (привал по кнопке, кость хитов).

    Версии растут только вперёд: следующая — наибольшая в базе плюс один, как у хода мастера.
    Открытый ход хранит прежнюю версию начала: так «Перегенерировать» видит изменения вне хода."""
    newest = await session.scalar(
        select(func.max(GameStateSnapshot.version)).where(GameStateSnapshot.game_id == game.id)
    )
    state.version = (newest or game.state_version) + 1
    session.add(GameStateSnapshot.from_state(state))
    game.state_version = state.version


async def camp_end(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Закончить привал»: хост не дожидается отсчёта (спека §5.5)."""
    async with _locked(flow, game_id) as session:
        await _host(session, game_id, user_id)
    await close_camp(flow, game_id)
    return await phase_now(flow, game_id)


async def close_camp(
    flow: Flow, game_id: str, *, turn_id: str | None = None, due: bool = False
) -> None:
    """Привал кончился: просит у мастера ход «конец привала» (отдых по виду привала и рассказ).

    Конец привала решает только сервер: отсчёт или хост, модель привал не заканчивает. Отказ —
    `GameError`: привала нет, ход уже другой, срок отодвинули, мастера успел позвать другой путь
    (кнопка и отсчёт сразу: кто зарезервировал ход первым, тот и разбирает). `turn_id` и `due` —
    как у `close_collecting`."""
    async with _locked(flow, game_id) as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        _require_playing(game)
        if game.turn_phase != TurnPhase.CAMP:
            raise Conflict("Привал не идёт.")
        if turn_id is not None and game.current_turn_id != turn_id:
            raise Conflict("Это уже другой ход.")
        if due and game.deadline_at is not None and game.deadline_at > flow.scheduler.now():
            raise Conflict("Срок отсчёта ещё не вышел.")
        camp_turn = game.current_turn_id
    logger.info("Игра %s: привал закончен, ход «конец привала».", game_id)
    await start_turn(flow, game_id, mode=TurnKind.CAMP_END, turn_id=camp_turn)


# --- бой: «Поторопить» ---


async def hurry(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Поторопить» (спека §8.5): отсчёт `hurry_seconds` для героя, который думает над ходом в бою.

    Таймера в бою нет, пока хост не нажмёт кнопку. Отсчёт один на игру (тот же ключ планировщика,
    что у сбора ходов); героя предупреждает бот."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase != TurnPhase.COMBAT_WAIT:
            raise Conflict("Поторопить можно только героя, который думает над ходом в бою.")
        if game.deadline_at is not None:
            raise Conflict("Отсчёт уже идёт.")
        state = await _state_of(session, game)
        hero = None if state is None else waiting_hero(state)
        if hero is None:
            raise Conflict("Сейчас ходит мастер: торопить некого.")
        player = await _player_of(session, game_id, hero.id)
        game.deadline_at = flow.scheduler.now() + timedelta(seconds=game.hurry_seconds)
        await session.commit()
        _arm(flow, game)
        delivery = await _phase_delivery(session, game)
        await _send(flow, game_id, [delivery])
    logger.info("Игра %s: «Поторопить», отсчёт %d с.", game_id, game.hurry_seconds)
    if player is not None:
        await _notify(flow, [player.user_id], hurry_text(game.hurry_seconds))
    return delivery[0]["phase"]


async def close_hurry(flow: Flow, game_id: str, *, turn_id: str) -> None:
    """Срок «Поторопить» вышел: ход героя сдаётся сам, затем мастер его разбирает.

    Сдаётся написанный черновик, а без него — «Уклоняется». Ход, уже сданный или спасованный,
    остаётся как есть. Отказ — `GameError`: герой уже не думает, ход другой, срок отодвинули,
    другой путь закрыл ход первым."""
    async with _locked(flow, game_id) as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        _require_playing(game)
        if game.turn_phase != TurnPhase.COMBAT_WAIT:
            raise Conflict("Герой уже не думает над ходом в бою.")
        if game.current_turn_id != turn_id:
            raise Conflict("Это уже другой ход.")
        if game.deadline_at is None or game.deadline_at > flow.scheduler.now():
            raise Conflict("Срок отсчёта ещё не вышел.")
        state = await _state_of(session, game)
        hero = None if state is None else waiting_hero(state)
        player = None if hero is None else await _player_of(session, game_id, hero.id)
        if player is None:
            raise Conflict("Сейчас ходит мастер: торопить некого.")
        game_adventure(game)  # битая копия — ход героя не сдаётся без мастера
        deliveries = await _submit_for(session, game, await _current_turn(session, game), player)
        await session.commit()
        flow.scheduler.cancel(game_id)
        await _send(flow, game_id, deliveries)
    logger.info("Игра %s: время героя в бою вышло, ход сдан сам.", game_id)
    await start_turn(flow, game_id, mode=TurnKind.COMBAT, turn_id=turn_id)


async def _submit_for(
    session: AsyncSession, game: Game, turn: Turn, player: GamePlayer
) -> list[Delivery]:
    """Сдаёт ход героя за игрока: черновик как есть, пустой или отсутствующий — «Уклоняется».

    «Ход сдан» (`action_auto_submitted`) получает только автор черновика с текстом: «Уклоняется»
    вместо пустого или отсутствующего хода игрок не писал (дополнение B11 §1 п. 5)."""
    row = await session.get(PlayerAction, (turn.id, player.user_id))
    written = row is not None and row.status == ActionStatus.DRAFT and bool(row.text.strip())
    if row is None:
        row = PlayerAction(
            turn_id=turn.id,
            user_id=player.user_id,
            text=DODGE_TEXT,
            secret=False,
            inspiration=False,
            status=ActionStatus.AUTO_SUBMITTED,
        )
        session.add(row)
    elif row.status == ActionStatus.DRAFT:
        row.text = row.text if row.text.strip() else DODGE_TEXT
        row.status = ActionStatus.AUTO_SUBMITTED
    else:
        return []  # ход уже сдан или спасован: мастер разберёт его как есть
    deliveries = [
        _status_delivery(player.user_id, row),
        *await _sync_feed(session, game, turn, player, row),
    ]
    if written:
        deliveries.append((action_auto_submitted_event(), player.user_id))
    return deliveries


async def on_deadline(flow: Flow, game_id: str, turn_id: str) -> None:
    """Срок отсчёта вышел: закрывается то, чей отсчёт шёл, — сбор ходов, «Поторопить» в бою или
    привал. Устаревший или опоздавший отсчёт ничего не закрывает."""
    try:
        async with flow.dm.sessionmaker() as session:
            game = await session.get(Game, game_id)
        phase = None if game is None else game.turn_phase
        if phase == TurnPhase.COMBAT_WAIT:
            await close_hurry(flow, game_id, turn_id=turn_id)
        elif phase == TurnPhase.CAMP:
            await close_camp(flow, game_id, turn_id=turn_id, due=True)
        else:
            await close_collecting(flow, game_id, turn_id=turn_id, due=True)
    except GameError as refusal:
        logger.debug("Отсчёт игры %s ничего не закрыл: %s", game_id, refusal)


async def extend(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«+1 минута»: сдвигает срок идущего сбора ходов."""
    refusal = "Продлить можно только идущий отсчёт сбора ходов."
    return await _extend(flow, game_id, user_id, TurnPhase.COLLECTING, COLLECT_EXTENSION, refusal)


async def camp_extend(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«+1 минута» на привале (спека §5.5): сдвигает срок идущего привала."""
    refusal = "Продлить можно только идущий привал."
    return await _extend(flow, game_id, user_id, TurnPhase.CAMP, CAMP_EXTENSION, refusal)


async def _extend(
    flow: Flow,
    game_id: str,
    user_id: str,
    phase: TurnPhase,
    extension: timedelta,
    refusal: str,
) -> dict[str, Any]:
    """Сдвигает срок отсчёта фазы `phase` от большего из срока и «сейчас»: срок, что уже прошёл,
    но не сработал (гонка), не уходит в прошлое."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase != phase or game.deadline_at is None:
            raise Conflict(refusal)
        game.deadline_at = max(game.deadline_at, flow.scheduler.now()) + extension
        await session.commit()
        _arm(flow, game)
        delivery = await _phase_delivery(session, game)
        await _send(flow, game_id, [delivery])
    return delivery[0]["phase"]


async def pause(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Пауза»: отсчёт и время игры стоят, пока хост не продолжит."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game)
        if game.turn_phase == TurnPhase.RESOLVING:
            raise Conflict("Мастер разбирает ход: паузу можно поставить, когда он закончит.")
        now = flow.scheduler.now()
        game.active_seconds = game.active_time(now)
        game.clock_at = now
        game.phase = GamePhase.PAUSED
        await session.commit()
        flow.scheduler.cancel(game_id)
        delivery = await _phase_delivery(session, game)
        await _send(flow, game_id, [delivery])
    logger.info("Игра %s: пауза.", game_id)
    return delivery[0]["phase"]


async def resume(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Продолжить»: часы идут снова, отсчёт получает прежний остаток."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        if game.phase != GamePhase.PAUSED:
            raise Conflict("Игра не на паузе.")
        now = flow.scheduler.now()
        remaining = game.paused_remaining()
        game.phase = GamePhase.PLAYING
        game.clock_at = now
        if remaining is not None:
            game.deadline_at = now + remaining
        await session.commit()
        if remaining is not None:
            _arm(flow, game)
        delivery = await _phase_delivery(session, game)
        await _send(flow, game_id, [delivery])
    logger.info("Игра %s: пауза снята.", game_id)
    return delivery[0]["phase"]


async def retry(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Повторить»: после `dm_failed` тот же ход разбирается заново в том же режиме и с теми же
    бойцами (из журнала попытки). Продолжает и остановленный бой, где ходят одни монстры: цепочка
    ходов мастера оборвалась или процесс умер между ходами.

    В начале игры (фаза `weaving`, хода ещё нет) повторяется вплетение героев: оно продолжает с
    места остановки (`begin_weaving`), поэтому уже записанные зацепки модель заново не пишет."""
    async with _locked(flow, game_id) as session:
        game = await _host(session, game_id, user_id)
        _require_playing(game, weaving_too=True)  # открытие игры тоже можно повторить
        if game.turn_phase == TurnPhase.RESOLVING and game_id not in flow.dm.live:
            # Мастера нет, а игра «разбирается»: запись неудачи не прошла (диск, база). Как и
            # после перезапуска, такая игра считается прерванной: сервер перезапускать не нужно.
            logger.warning(
                "Игра %s: в «мастер думает» нет живого разбора, «Повторить» принимает её как "
                "прерванную.",
                game_id,
            )
            await _write_interrupted(flow, session, game, cause=MASTER_LOST, note_text=None)
            await session.commit()
        weaving = _weaving_not_running(game)
        if not weaving:
            mode, acting = await _what_to_retry(session, game)
    if weaving:
        await start_weaving(flow, game_id)
    else:
        await start_turn(flow, game_id, mode=mode, acting=acting)
    return await phase_now(flow, game_id)


def _weaving_not_running(game: Game) -> bool:
    """Игра в начале, хода ещё нет, а вплетение не идёт: оно упало (`dm_failed`) или ещё не
    начиналось (после «Начать» мастера не звали: сервер умер в тот же миг)."""
    return (
        game.phase == GamePhase.WEAVING
        and game.current_turn_id is None
        and game.turn_phase in (None, TurnPhase.DM_FAILED)
    )


async def _what_to_retry(session: AsyncSession, game: Game) -> tuple[TurnKind, list[str] | None]:
    """Режим и бойцы повторяемого хода; нечего повторять — `Conflict`.

    Записи восстановления после падения процесса бойцов не называют (`acting: null`), и боевой ход
    берёт их из состояния боя (`dm.turn`)."""
    if game.turn_phase == TurnPhase.DM_FAILED:
        turn = await _current_turn(session, game)
        last = await session.scalar(
            select(DmTurnLog.request)
            .where(DmTurnLog.turn_id == turn.id)
            .order_by(DmTurnLog.id.desc())
            .limit(1)
        )
        return turn.kind, (last or {}).get("acting")
    if game.turn_phase == TurnPhase.COMBAT_WAIT:
        state = await _state_of(session, game)
        if state is not None and master_moves_next(state):
            return TurnKind.COMBAT, None
    raise Conflict("Повторять нечего: мастер не падал.")


async def regenerate(flow: Flow, game_id: str, user_id: str) -> dict[str, Any]:
    """«Перегенерировать»: откат последнего разобранного хода и новая попытка."""
    async with _locked(flow, game_id) as session:
        _require_playing(await _host(session, game_id, user_id))
    await start_turn(flow, game_id, mode=None, regenerate=True)
    return await phase_now(flow, game_id)


# --- после перезапуска сервера ---


async def restore_games(flow: Flow) -> None:
    """Отсчёты из базы и игры, прерванные перезапуском (спека §1 п. 5, решение D3).

    Отсчёты ставятся заново для идущих игр (срок, что уже прошёл, сработает сразу); отсчёт игры на
    паузе не ставится, остаток вернёт «Продолжить». Игра, застрявшая в `resolving`, потому что
    процесс умер во время хода или вплетения героев, переходит в `dm_failed`: ход сам не
    перезапускается, хост видит сообщение и повторяет его. То же с игрой, у которой вплетение
    записано, а открытие не начиналось (`_interrupted`). Сбой чтения базы (например, нет
    миграций) пишется в журнал и не останавливает сервер."""
    try:
        async with flow.dm.sessionmaker() as session:
            interrupted = list(await session.scalars(select(Game.id).where(_interrupted())))
            countdowns = list(
                await session.execute(
                    select(Game.id, Game.current_turn_id, Game.deadline_at).where(
                        Game.phase == GamePhase.PLAYING,
                        Game.deadline_at.is_not(None),
                        Game.current_turn_id.is_not(None),
                    )
                )
            )
    except SQLAlchemyError as exc:
        logger.error(
            "Не удалось восстановить отсчёты и разборы игр после перезапуска: "
            "база недоступна или в ней нет таблиц (выполнены ли миграции?).\n%s",
            safe_traceback(exc, flow.dm.settings),
        )
        return
    for game_id in interrupted:
        try:
            await _fail_interrupted(flow, game_id)
        except Exception as exc:
            # Одна неисправная игра не должна остановить сервер и остальные игры.
            logger.error(
                "Не удалось отметить прерванный разбор игры %s.\n%s",
                game_id,
                safe_traceback(exc, flow.dm.settings),
            )
    for game_id, turn_id, deadline in countdowns:
        flow.scheduler.schedule(game_id, deadline, _on_expiry(flow.app, game_id, turn_id))
    if interrupted or countdowns:
        logger.info(
            "Восстановлено после перезапуска: отсчётов %d, прерванных разборов %d.",
            len(countdowns),
            len(interrupted),
        )


def _interrupted() -> ColumnElement[bool]:
    """Игры, прерванные перезапуском: мастер «разбирал» ход или вплетал героев (`resolving`), либо
    вплетение записано (есть состояние), а открытие игры не начиналось (хода нет). Игра в начале
    без состояния и без фазы хода прерванной не считается: мастера для неё ещё не звали (хост
    повторяет её кнопкой). Единственное определение: им выбирают игры при старте и перепроверяют
    каждую под блокировкой."""
    opening_never_begun = and_(
        Game.phase == GamePhase.WEAVING,
        Game.turn_phase.is_(None),
        Game.current_turn_id.is_(None),
        Game.state_version >= 1,
    )
    return and_(
        Game.phase.in_((GamePhase.WEAVING, GamePhase.PLAYING)),
        or_(Game.turn_phase == TurnPhase.RESOLVING, opening_never_begun),
    )


async def _fail_interrupted(flow: Flow, game_id: str) -> None:
    """Игра без живого разбора после перезапуска: попытка пишется неудачной, хост получает
    сообщение (`_write_interrupted`) и повторяет кнопкой «Повторить»."""
    async with _locked(flow, game_id) as session:
        game = await session.scalar(select(Game).where(Game.id == game_id, _interrupted()))
        if game is None:
            return  # игру успели изменить: она уже не прервана
        if game.current_turn_id is None:  # вплетение шло или открытие не начиналось
            note_text = failure_text(
                TurnKind.OPENING, "сервер был перезапущен во время вплетения героев в историю"
            )
        else:
            turn = await _current_turn(session, game)
            note_text = failure_text(turn.kind, "сервер был перезапущен во время разбора")
        note = await _write_interrupted(
            flow, session, game, cause=SERVER_RESTARTED, note_text=note_text
        )
        await session.commit()
        assert note is not None
        await _send(
            flow,
            game_id,
            [(feed_item_event(note), note.audience), await _phase_delivery(session, game)],
        )
    logger.warning("Игра %s: разбор прерван перезапуском, игра ждёт «Повторить».", game_id)


async def _write_interrupted(
    flow: Flow, session: AsyncSession, game: Game, *, cause: str, note_text: str | None
) -> Message | None:
    """Игра без мастера: `dm_failed`, ход `failed` с записью попытки в журнале (`cause`) и, если
    дан `note_text`, сообщение хосту. Под блокировкой игры; коммит — на вызывающем.

    В начале игры хода ещё нет (вплетение шло или открытие не началось): запись журнала невозможна
    (она привязана к ходу), остаётся фаза и сообщение хосту."""
    turn = None if game.current_turn_id is None else await _current_turn(session, game)
    game.set_turn_phase(TurnPhase.DM_FAILED, flow.scheduler.now())
    game.deadline_at = None
    note = None
    if note_text is not None:
        note = Message(
            game_id=game.id,
            turn_id=None if turn is None else turn.id,
            audience=game.host_user_id,
            kind=MessageKind.SYSTEM,
            text=note_text,
        )
        session.add(note)
    if turn is not None:
        attempts = await session.scalar(
            select(DmTurnLog.attempt)
            .where(DmTurnLog.turn_id == turn.id)
            .order_by(DmTurnLog.attempt.desc())
            .limit(1)
        )
        turn.status = TurnStatus.FAILED
        session.add(
            DmTurnLog(
                game_id=game.id,
                turn_id=turn.id,
                attempt=(attempts or 0) + 1,
                provider=game.provider or flow.dm.settings.llm_provider,
                model=None,
                request={"mode": turn.kind.value, "acting": None},
                trace=[],
                error=cause,
            )
        )
    return note


# --- запуск мастера после «Начать» ---


class FlowHooks:
    """`GameHooks` игры: после старта из лобби вплетает героев и открывает игру (план B7.8).

    Вызывается после ответа на «Начать» (`games.api.start_game`) и возвращается, когда вплетение
    зарезервировано; дальше оно идёт фоновой задачей набора `turn_tasks`, который останавливается
    вместе с сервером. Зависимости (`Flow`) собираются при каждом вызове, поэтому подмена частей
    `app.state` в тестах подхватывается."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def on_game_started(self, game_id: str) -> None:
        await start_weaving(Flow.from_app(self._app), game_id)
