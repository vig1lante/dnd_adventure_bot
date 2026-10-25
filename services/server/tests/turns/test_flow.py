"""Обсуждение, сбор ходов и отсчёты (план B7.4, спека §8.2 и §12).

Настоящее приложение (`create_app` и его `lifespan`), временная SQLite после миграций, настоящий
движок и демо-приключение `demo-crypt` в копии игры. Время отсчётов ведёт `ManualScheduler`: тест
сам решает, сколько секунд прошло. Модель — `FakeProvider` по сценарию или заглушка, которой
управляет тест (тайм-аут, сбой, остановка на событии); живых моделей нет. Кубы — `SeededRng`.

Что держат тесты:
- до нажатия хоста таймеров нет: обсуждение длится сколько угодно;
- «Собрать ходы» без готовых ходов запускает отсчёт, при готовых ходах сразу идёт разбор мастера;
- время вышло: непустой черновик сдаётся сам, пустой и отсутствующий становятся пасом;
- пауза замораживает отсчёт и время игры, продолжение возвращает остаток;
- двойное нажатие, кнопка против отсчёта, сдача последним игроком — ровно один разбор;
- после перезапуска отсчёты восстанавливаются из базы, а застрявший разбор становится `dm_failed`;
- тайный ход видит только автор, чужие права на кнопки хоста и правку в `resolving` — отказ.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveCondition
from dnd_engine.combat.model import Combat, Combatant, Zone
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.rng import SeededRng
from dnd_engine.state import GameState, RulesRef, Scene, SceneType
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.bot.notify import game_continues_text, game_started_text
from dnd_server.characters.service import new_character_id
from dnd_server.config import Settings
from dnd_server.db.models import (
    ActionStatus,
    DmTurnLog,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    Turn,
    TurnKind,
    TurnPhase,
    TurnStatus,
    User,
    new_id,
)
from dnd_server.dm import provider as provider_module
from dnd_server.dm.provider import GameProviders
from dnd_server.dm.turn import Deps, run_dm_turn
from dnd_server.games.service import Conflict, NotFound
from dnd_server.llm.base import ProviderError, TurnRequest, TurnResult, Usage
from dnd_server.realtime.views import snapshot_for
from dnd_server.turns import flow as flow_module
from dnd_server.turns.flow import MASTER_LOST, Flow, FlowHooks, start_turn
from dnd_server.turns.timers import ManualScheduler, Scheduler, TaskSet

T0 = datetime(2030, 5, 1, 12, 0, 0, tzinfo=UTC)
FAST = 0.02  # секунды настоящего ожидания в проверках живого планировщика


# --- ручной планировщик ---


async def test_the_manual_scheduler_starts_at_the_given_moment_and_moves_only_on_advance():
    scheduler = ManualScheduler(T0)

    assert scheduler.now() == T0
    await scheduler.advance(90)
    assert scheduler.now() == T0 + timedelta(seconds=90)


async def test_the_manual_scheduler_without_a_start_takes_the_real_time():
    before = datetime.now(UTC)
    scheduler = ManualScheduler()

    assert before <= scheduler.now() <= datetime.now(UTC)


async def test_an_action_fires_when_its_moment_comes_and_not_a_second_earlier():
    scheduler = ManualScheduler(T0)
    fired: list[str] = []

    async def action() -> None:
        fired.append("сработало")

    scheduler.schedule("g", T0 + timedelta(seconds=120), action)
    await scheduler.advance(119)
    assert fired == []
    await scheduler.advance(1)

    assert fired == ["сработало"]
    assert scheduler.pending() == {}  # сработавший отсчёт больше не ждёт
    await scheduler.advance(1000)
    assert fired == ["сработало"]  # и второй раз не срабатывает


async def test_actions_fire_in_the_order_of_their_moments_then_of_scheduling():
    scheduler = ManualScheduler(T0)
    fired: list[str] = []

    def at(name: str):
        async def action() -> None:
            fired.append(name)

        return action

    scheduler.schedule("поздно", T0 + timedelta(seconds=50), at("поздно"))
    scheduler.schedule("рано", T0 + timedelta(seconds=10), at("рано"))
    scheduler.schedule("вместе-1", T0 + timedelta(seconds=30), at("вместе-1"))
    scheduler.schedule("вместе-2", T0 + timedelta(seconds=30), at("вместе-2"))
    await scheduler.advance(100)

    assert fired == ["рано", "вместе-1", "вместе-2", "поздно"]


async def test_cancel_removes_the_action_and_scheduling_a_key_again_replaces_it():
    scheduler = ManualScheduler(T0)
    fired: list[str] = []

    def at(name: str):
        async def action() -> None:
            fired.append(name)

        return action

    scheduler.schedule("a", T0 + timedelta(seconds=10), at("отменено"))
    scheduler.schedule("b", T0 + timedelta(seconds=10), at("старое"))
    scheduler.schedule("b", T0 + timedelta(seconds=40), at("новое"))
    scheduler.cancel("a")
    scheduler.cancel("нет-такого")  # отмена несуществующего отсчёта безвредна

    assert scheduler.pending() == {"b": T0 + timedelta(seconds=40)}
    await scheduler.advance(20)
    assert fired == []
    await scheduler.advance(20)
    assert fired == ["новое"]


async def test_an_action_scheduled_in_the_past_fires_on_the_next_advance():
    scheduler = ManualScheduler(T0)
    fired: list[int] = []

    async def action() -> None:
        fired.append(1)

    scheduler.schedule("g", T0 - timedelta(seconds=5), action)
    await scheduler.advance(0)

    assert fired == [1]


async def test_an_action_may_schedule_the_next_one_and_it_fires_in_the_same_advance():
    scheduler = ManualScheduler(T0)
    fired: list[str] = []

    async def second() -> None:
        fired.append("второе")

    async def first() -> None:
        fired.append("первое")
        scheduler.schedule("g", scheduler.now() + timedelta(seconds=5), second)

    scheduler.schedule("g", T0 + timedelta(seconds=10), first)
    await scheduler.advance(60)

    assert fired == ["первое", "второе"]


async def test_every_action_sees_the_clock_at_its_own_moment_and_the_clock_ends_at_the_target():
    scheduler = ManualScheduler(T0)
    seen: list[datetime] = []

    async def action() -> None:
        seen.append(scheduler.now())

    scheduler.schedule("a", T0 + timedelta(seconds=10), action)
    scheduler.schedule("b", T0 + timedelta(seconds=30), action)
    await scheduler.advance(60)

    assert seen == [T0 + timedelta(seconds=10), T0 + timedelta(seconds=30)]
    assert scheduler.now() == T0 + timedelta(seconds=60)


async def test_a_failing_action_is_not_swallowed_by_the_manual_scheduler():
    scheduler = ManualScheduler(T0)

    async def broken() -> None:
        raise RuntimeError("действие упало")

    scheduler.schedule("g", T0 + timedelta(seconds=1), broken)

    with pytest.raises(RuntimeError, match="упало"):
        await scheduler.advance(5)


async def test_closing_the_manual_scheduler_drops_every_action():
    scheduler = ManualScheduler(T0)
    fired: list[int] = []

    async def action() -> None:
        fired.append(1)

    scheduler.schedule("g", T0 + timedelta(seconds=1), action)
    await scheduler.close()
    await scheduler.advance(10)

    assert fired == [] and scheduler.pending() == {}


# --- живой планировщик ---


async def test_the_live_scheduler_fires_after_the_wait_and_forgets_the_action():
    scheduler = Scheduler()
    fired: list[int] = []

    async def action() -> None:
        fired.append(1)

    scheduler.schedule("g", scheduler.now() + timedelta(seconds=FAST), action)
    assert list(scheduler.pending()) == ["g"]
    await asyncio.sleep(FAST * 5)

    assert fired == [1]
    assert scheduler.pending() == {}


async def test_the_live_scheduler_cancel_and_replace_work():
    scheduler = Scheduler()
    fired: list[str] = []

    def at(name: str):
        async def action() -> None:
            fired.append(name)

        return action

    soon = scheduler.now() + timedelta(seconds=FAST)
    scheduler.schedule("a", soon, at("отменено"))
    scheduler.schedule("b", soon, at("старое"))
    scheduler.schedule("b", soon + timedelta(seconds=FAST), at("новое"))
    scheduler.cancel("a")
    await asyncio.sleep(FAST * 6)

    assert fired == ["новое"]


async def test_a_fired_action_may_cancel_its_own_key_without_cancelling_itself():
    scheduler = Scheduler()
    finished: list[int] = []

    async def action() -> None:
        scheduler.cancel("g")  # так делает закрытие сбора: оно снимает отсчёт своей же игры
        await asyncio.sleep(FAST)  # и продолжает работу после снятия
        finished.append(1)

    scheduler.schedule("g", scheduler.now(), action)
    await asyncio.sleep(FAST * 6)

    assert finished == [1]


async def test_the_live_scheduler_waits_again_when_it_wakes_up_early():
    # Часы стены и таймер цикла событий расходятся: проснуться чуть раньше срока можно. Отсчёт
    # не должен пропасть, он досыпает остаток.
    readings = iter([T0, T0 + timedelta(seconds=FAST), T0 + timedelta(seconds=FAST * 3)])
    calls: list[datetime] = []

    def clock() -> datetime:
        calls.append(next(readings))
        return calls[-1]

    scheduler = Scheduler(clock=clock)
    fired: list[int] = []

    async def action() -> None:
        fired.append(1)

    scheduler.schedule("g", T0 + timedelta(seconds=FAST * 2), action)
    await asyncio.sleep(FAST * 8)

    assert fired == [1]
    assert len(calls) == 3


async def test_a_failing_action_is_logged_and_the_scheduler_goes_on(caplog):
    scheduler = Scheduler()
    fired: list[int] = []

    async def broken() -> None:
        raise RuntimeError("действие упало")

    async def fine() -> None:
        fired.append(1)

    with caplog.at_level(logging.ERROR):
        scheduler.schedule("a", scheduler.now(), broken)
        scheduler.schedule("b", scheduler.now(), fine)
        await asyncio.sleep(FAST * 4)

    assert fired == [1]
    assert "a" in caplog.text and "действие упало" in caplog.text


async def test_closing_the_live_scheduler_cancels_what_still_waits():
    scheduler = Scheduler()
    fired: list[int] = []

    async def action() -> None:
        fired.append(1)

    scheduler.schedule("g", scheduler.now() + timedelta(seconds=FAST * 4), action)
    await scheduler.close()
    await asyncio.sleep(FAST * 8)

    assert fired == [] and scheduler.pending() == {}


# --- фоновые задачи ---


async def test_the_task_set_keeps_tasks_until_they_end_and_join_waits_for_them():
    tasks = TaskSet()
    order: list[str] = []
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()
        order.append("конец")

    tasks.spawn(work())
    waiter = asyncio.create_task(tasks.join())
    await asyncio.sleep(FAST)
    assert not waiter.done()
    gate.set()
    await waiter

    assert order == ["конец"]
    assert len(tasks) == 0


async def test_the_task_set_logs_an_unexpected_failure_but_not_a_refusal_of_the_game(caplog):
    tasks = TaskSet()

    async def crash() -> None:
        raise RuntimeError("разбор упал")

    async def refused() -> None:
        raise Conflict("уже разбирается")

    async def missing() -> None:
        raise NotFound("нет игры")

    with caplog.at_level(logging.ERROR):
        for work in (crash(), refused(), missing()):
            tasks.spawn(work)
        await tasks.join()

    assert caplog.text.count("Фоновая задача") == 1
    assert "разбор упал" in caplog.text


async def test_the_task_set_names_the_failing_task_with_its_own_text_or_the_turn_by_default(
    caplog,
):
    tasks = TaskSet()

    async def crash() -> None:
        raise RuntimeError("упало")

    with caplog.at_level(logging.ERROR):
        tasks.spawn(crash())
        tasks.spawn(crash(), failure_text="Фоновая задача конструктора завершилась ошибкой.")
        await tasks.join()

    assert caplog.text.count("Фоновая задача хода завершилась ошибкой.") == 1
    assert caplog.text.count("Фоновая задача конструктора завершилась ошибкой.") == 1


async def test_the_task_set_logs_a_failure_without_the_input_of_the_error(caplog, leak):
    tasks = TaskSet()

    async def crash() -> None:
        raise leak.error

    with caplog.at_level(logging.ERROR):
        tasks.spawn(crash())
        await tasks.join()

    [record] = caplog.records
    assert record.exc_info is None  # трассировку формирует сервер, а не logging
    assert "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_the_live_scheduler_logs_a_failing_action_without_the_input_of_the_error(
    caplog, leak
):
    scheduler = Scheduler()

    async def crash() -> None:
        raise leak.error

    with caplog.at_level(logging.ERROR):
        scheduler.schedule("g", scheduler.now() + timedelta(seconds=FAST), crash)
        await asyncio.sleep(FAST * 5)

    [record] = caplog.records
    assert record.exc_info is None and "g" in record.getMessage()
    assert "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_the_app_gives_its_settings_to_the_scheduler_and_the_task_set(settings, caplog):
    # Их журнал вырезает значения секретов из текста ошибки: настройки они берут у приложения.
    key = "sk-ant-api-SECRET-0123456789"
    app = create_app(settings.model_copy(update={"anthropic_api_key": SecretStr(key)}))

    async def crash() -> None:
        raise RuntimeError(f"сбой с ключом {key}")

    with caplog.at_level(logging.ERROR):
        app.state.turn_tasks.spawn(crash())
        await app.state.turn_tasks.join()
        scheduler = app.state.scheduler
        scheduler.schedule("g", scheduler.now() + timedelta(seconds=FAST), crash)
        await asyncio.sleep(FAST * 5)

    assert len(caplog.records) == 2  # по записи от задачи и от отсчёта
    assert caplog.text.count("***") == 2 and key not in caplog.text
    await scheduler.close()
    await app.state.engine.dispose()


async def test_the_task_set_shutdown_cancels_running_tasks_and_waits_for_their_cleanup():
    tasks = TaskSet()
    cleaned: list[int] = []

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(FAST)  # уборка после отмены завершается до возврата shutdown
            cleaned.append(1)
            raise

    tasks.spawn(work())
    await asyncio.sleep(0)
    await tasks.shutdown()

    assert cleaned == [1] and len(tasks) == 0


# --- стенд: приложение, игра, игроки ---

# tests/turns/test_flow.py -> turns -> tests -> server -> services -> корень репозитория
REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_ADVENTURE_DIR = REPO_ROOT / "content" / "adventures" / "demo-crypt"
SESSION_SECRET = "turns-session-secret-0123456789-abcdefghij"
TIMEOUT_S = 5
PLAYERS = (  # (ключ, имя, класс, вид); первый — хост
    ("alice", "Алиса", "fighter", "human"),
    ("boris", "Борис", "rogue", "elf"),
    ("vera", "Вера", "cleric", "dwarf"),
)
COLLECT_SECONDS = 120
SCENARIO = {
    "turns": [
        {
            "repeat": True,
            "steps": [
                {"tool": "chronicle", "args": {"entry": "Отряд идёт дальше."}},
                {"text": "Отряд идёт дальше, и впереди что-то шевелится."},
            ],
        }
    ],
    # Вплетение без зацепок: ответ подходит схеме, а связывать героев не с чем.
    "json": {"weaving": {"hooks": []}},
}


@dataclass(frozen=True)
class Seat:
    key: str
    user_id: str
    character_id: str
    name: str
    token: str


@dataclass(frozen=True)
class Setup:
    """Что лежит в базе до старта приложения: игроки, id игры и открытого хода."""

    seats: dict[str, Seat]
    game_id: str
    turn_id: str
    created: datetime


class RecordingBroadcaster:
    """Хаб вместо сокетов: помнит каждое событие и его адресата."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
        self.sent.append((game_id, payload, audience))

    def of_type(self, event_type: str) -> list[tuple[dict[str, Any], str]]:
        return [(p, who) for _game, p, who in self.sent if p["type"] == event_type]


class RecordingNotifier:
    """Бот вместо Telegram: помнит, кому и что отправили; `broken` имитирует сбой отправки."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.broken = False

    async def notify(self, user_id: str, text: str) -> None:
        if self.broken:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((user_id, text))

    def texts_for(self, user_id: str) -> list[str]:
        return [text for who, text in self.sent if who == user_id]


class StubProvider:
    """Модель, которой управляет тест: готовые ответы или ошибки, остановка на событии."""

    name = "stub"

    def __init__(self, *outcomes: TurnResult | Exception, gate: asyncio.Event | None = None):
        self._outcomes = list(outcomes) or [narration()]
        self._gate = gate
        self.requests: list[TurnRequest] = []
        self.started = asyncio.Event()

    async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
        self.requests.append(req)
        self.started.set()
        if self._gate is not None:
            await self._gate.wait()
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def complete_json(self, req: Any, schema: Any) -> Any:
        raise AssertionError("ход мастера не просит JSON")


def narration(text: str = "Рассказ мастера.") -> TurnResult:
    return TurnResult(text=text, trace=(), usage=Usage(11, 7, 5, 3), stop_reason="end_turn")


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> list[dict[str, Any]]:
    """Подменяет сборку провайдеров игры; возвращает аргументы каждой сборки."""
    calls: list[dict[str, Any]] = []

    def factory(_settings: Settings, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return provider

    monkeypatch.setattr(provider_module, "make_provider", factory)
    return calls


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """Dev-настройки из корневого conftest: секрет сессий и сценарий `FakeProvider`."""
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(SCENARIO, allow_unicode=True), encoding="utf-8")
    return settings.model_copy(
        update={
            "session_secret": SecretStr(SESSION_SECRET),
            "fake_llm_scenario": scenario,
            "content_dir": tmp_path / "content",
        }
    )


async def seed_game(
    db: async_sessionmaker[AsyncSession],
    srd: SrdContent,
    adventure: Adventure,
    **game_fields: Any,
) -> Setup:
    """Игра в разгаре: три игрока с персонажами, состояние версии 1, открытый ход 1 (обсуждение).

    У Алисы есть вдохновение. `game_fields` переопределяют поля игры (фаза, отсчёт и так далее)."""
    created = datetime.now(UTC)
    game_id, turn_id = new_id(), new_id()
    signer = SessionSigner(SESSION_SECRET)
    seats: dict[str, Seat] = {}
    heroes = {}
    for key, name, class_id, species in PLAYERS:
        user_id = new_id()
        hero = quick_character(
            class_id,
            species,
            srd,
            name=name,
            concept="Бывший стражник",
            character_id=new_character_id(),
        )
        hero = hero.model_copy(update={"player_id": user_id, "inspiration": key == "alice"})
        heroes[hero.id] = hero
        seats[key] = Seat(key, user_id, hero.id, name, signer.issue(user_id))
    state = GameState(
        game_id=game_id,
        adventure_id=adventure.id,
        rules=RulesRef(modules=list(adventure.rules.modules)),
        party=heroes,
        location_id=adventure.start_location,
        turn=1,
        version=1,
    )
    state.world.visited_locations = [adventure.start_location]
    fields: dict[str, Any] = {
        "phase": GamePhase.PLAYING,
        "turn_phase": TurnPhase.DISCUSSION,
        "state_version": 1,
        "current_turn_id": turn_id,
        "collect_seconds": COLLECT_SECONDS,
        "hurry_seconds": 60,
        "camp_seconds": 300,
        "started_at": created,
        "clock_at": created,
    } | game_fields
    async with db() as session:
        session.add_all(
            User(id=seat.user_id, dev_login=f"dev-{number}", name=seat.name)
            for number, seat in enumerate(seats.values(), start=1)
        )
        await session.flush()
        session.add(
            Game(
                id=game_id,
                code="ABC123",
                adventure_id=adventure.id,
                host_user_id=seats["alice"].user_id,
                adventure_data=adventure.model_dump(mode="json"),
                **fields,
            )
        )
        await session.flush()
        session.add_all(
            PlayerCharacter(
                id=seat.character_id,
                game_id=game_id,
                user_id=seat.user_id,
                data=heroes[seat.character_id].model_dump(mode="json"),
            )
            for seat in seats.values()
        )
        await session.flush()
        session.add_all(
            GamePlayer(
                game_id=game_id,
                user_id=seat.user_id,
                character_id=seat.character_id,
                is_host=seat.key == "alice",
                ready=True,
                joined_order=number,
            )
            for number, seat in enumerate(seats.values(), start=1)
        )
        session.add(GameStateSnapshot.from_state(state))
        session.add(
            Turn(
                id=turn_id,
                game_id=game_id,
                number=1,
                kind=TurnKind.ROUND,
                status=TurnStatus.OPEN,
                state_version_before=1,
            )
        )
        await session.commit()
    return Setup(seats, game_id, turn_id, created)


@dataclass
class Table:
    """Запущенное приложение с игрой: запросы игроков, часы и записи рассылки."""

    app: FastAPI
    http: httpx.AsyncClient
    scheduler: ManualScheduler
    broadcaster: RecordingBroadcaster
    notifier: RecordingNotifier
    db: async_sessionmaker[AsyncSession]
    settings: Settings
    srd: SrdContent
    setup: Setup

    @property
    def seats(self) -> dict[str, Seat]:
        return self.setup.seats

    @property
    def game_id(self) -> str:
        return self.setup.game_id

    def user_id(self, key: str) -> str:
        return self.seats[key].user_id

    # --- запросы ---

    async def call(self, key: str | None, method: str, path: str, **options: Any) -> httpx.Response:
        headers = {} if key is None else {"Authorization": f"Bearer {self.seats[key].token}"}
        return await self.http.request(
            method, f"/api/games/{self.game_id}{path}", headers=headers, **options
        )

    async def draft(
        self,
        key: str,
        text: str = "",
        *,
        secret: bool = False,
        inspiration: bool = False,
    ) -> httpx.Response:
        body = {"text": text, "secret": secret, "inspiration": inspiration}
        return await self.call(key, "PUT", "/action", json=body)

    async def submit(self, key: str, text: str | None = None, **flags: bool) -> httpx.Response:
        if text is not None:
            assert (await self.draft(key, text, **flags)).status_code == 200
        return await self.call(key, "POST", "/action/submit")

    async def pass_(self, key: str) -> httpx.Response:
        return await self.call(key, "POST", "/action/pass")

    async def host(self, name: str, key: str = "alice") -> httpx.Response:
        return await self.call(key, "POST", f"/host/{name}")

    async def settle(self) -> None:
        """Ждёт, пока кончатся все разборы мастера, запущенные приложением."""
        await asyncio.wait_for(self.app.state.turn_tasks.join(), TIMEOUT_S)

    # --- что лежит в базе ---

    async def game(self) -> Game:
        async with self.db() as session:
            return await session.get(Game, self.game_id)

    async def turns(self) -> list[Turn]:
        async with self.db() as session:
            rows = await session.scalars(
                select(Turn).where(Turn.game_id == self.game_id).order_by(Turn.number)
            )
            return list(rows)

    async def actions(self, turn_id: str | None = None) -> dict[str, PlayerAction]:
        """Ходы игроков хода (по умолчанию первого) по ключу игрока."""
        key_of = {seat.user_id: seat.key for seat in self.seats.values()}
        async with self.db() as session:
            rows = await session.scalars(
                select(PlayerAction).where(PlayerAction.turn_id == (turn_id or self.setup.turn_id))
            )
            return {key_of[row.user_id]: row for row in rows}

    async def messages(self, kind: MessageKind | None = None) -> list[Message]:
        async with self.db() as session:
            query = select(Message).where(Message.game_id == self.game_id).order_by(Message.id)
            if kind is not None:
                query = query.where(Message.kind == kind)
            return list(await session.scalars(query))

    async def logs(self) -> list[DmTurnLog]:
        async with self.db() as session:
            rows = await session.scalars(
                select(DmTurnLog).where(DmTurnLog.game_id == self.game_id).order_by(DmTurnLog.id)
            )
            return list(rows)

    async def state(self) -> GameState:
        async with self.db() as session:
            game = await session.get(Game, self.game_id)
            row = await session.get(GameStateSnapshot, (self.game_id, game.state_version))
            return row.to_state()

    async def update_game(self, **values: Any) -> None:
        if not values:
            return
        async with self.db() as session:
            await session.execute(update(Game).where(Game.id == self.game_id).values(**values))
            await session.commit()

    async def feed_of(self, key: str) -> list[dict[str, Any]]:
        async with self.db() as session:
            user = await session.get(User, self.user_id(key))
            return (await snapshot_for(session, user, self.game_id, srd=self.srd))["feed"]

    # --- рассылка ---

    def statuses_sent(self) -> list[tuple[str, str]]:
        """Рассылка `player_status`: (ключ игрока, статус) по порядку; адресат всегда «все»."""
        key_of = {seat.user_id: seat.key for seat in self.seats.values()}
        events = self.broadcaster.of_type("player_status")
        assert all(who == "all" for _payload, who in events)
        return [(key_of[p["user_id"]], p["status"]) for p, _who in events]

    def phases_sent(self) -> list[tuple[str | None, str | None]]:
        return [
            (p["phase"]["phase"], p["phase"]["turn_phase"])
            for p, _who in self.broadcaster.of_type("phase")
        ]


@asynccontextmanager
async def running_table(
    settings: Settings,
    db: async_sessionmaker[AsyncSession],
    srd: SrdContent,
    setup: Setup,
    *,
    scheduler: ManualScheduler | None = None,
    before_start: Callable[[FastAPI], None] | None = None,
) -> AsyncIterator[Table]:
    """Приложение на базе теста, как после запуска сервера (`lifespan` восстанавливает игры)."""
    app = create_app(settings)
    scheduler = scheduler or ManualScheduler(setup.created)
    broadcaster = RecordingBroadcaster()
    app.state.scheduler = scheduler
    app.state.broadcaster = broadcaster
    app.state.rng = SeededRng(7)
    if before_start is not None:
        before_start(app)
    async with app.router.lifespan_context(app):
        # Бот без токена ничего не шлёт; регистратор встаёт на его место после старта.
        notifier = RecordingNotifier()
        app.state.notifier = notifier
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield Table(app, http, scheduler, broadcaster, notifier, db, settings, srd, setup)


@pytest.fixture
async def setup(db, srd, adventure) -> Setup:
    return await seed_game(db, srd, adventure)


@pytest.fixture
async def table(settings, db, srd, setup) -> AsyncIterator[Table]:
    async with running_table(settings, db, srd, setup) as running:
        yield running


def round_actions(context: str) -> dict[str, dict[str, Any]]:
    """Ходы раунда из контекста мастера по id персонажа: `{"id", "name", "text", "marks"}`."""
    section = context.split("## Ходы этого раунда", 1)[1].split("\n## ", 1)[0]
    lines = (line.rstrip(",") for line in section.splitlines() if line.startswith('{"id"'))
    return {row["id"]: row for row in map(json.loads, lines)}


def action_fields(response: httpx.Response) -> tuple[str, str, bool, bool]:
    body = response.json()
    return body["status"], body["text"], body["secret"], body["inspiration"]


# --- черновик хода ---


async def test_a_draft_is_saved_on_the_server_and_comes_back_as_a_draft(table):
    response = await table.draft("alice", "Осмотреть дверь", secret=True, inspiration=True)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "text": "Осмотреть дверь",
        "secret": True,
        "inspiration": True,  # у Алисы вдохновение есть
        "status": "draft",
    }
    [row] = (await table.actions()).values()
    assert (row.user_id, row.text, row.secret, row.inspiration, row.status) == (
        table.user_id("alice"),
        "Осмотреть дверь",
        True,
        True,
        ActionStatus.DRAFT,
    )


async def test_a_draft_can_be_rewritten_and_stays_one_row(table):
    await table.draft("boris", "Подкрасться")
    response = await table.draft("boris", "Подкрасться и поднять ключ", secret=True)

    assert action_fields(response) == ("draft", "Подкрасться и поднять ключ", True, False)
    assert len(await table.actions()) == 1


async def test_a_draft_announces_nothing_because_everybody_is_writing_anyway(table):
    await table.draft("boris", "Подкрасться")

    assert table.broadcaster.sent == []


async def test_the_inspiration_flag_is_dropped_when_the_hero_has_no_inspiration(table):
    response = await table.draft("boris", "Подкрасться", inspiration=True)  # у Бориса его нет

    assert response.status_code == 200
    assert response.json()["inspiration"] is False
    assert (await table.actions())["boris"].inspiration is False


async def test_an_empty_draft_is_still_a_draft_and_not_an_error(table):
    # Пустой текст — тоже черновик (игрок стёр написанное), а не ошибка.
    response = await table.draft("alice", "")

    assert action_fields(response) == ("draft", "", False, False)


async def test_a_draft_needs_a_login_and_a_seat_in_the_game(table, db):
    async with db() as session:
        stranger = User(dev_login="dev-9", name="Чужой")
        session.add(stranger)
        await session.commit()
    foreign = {"Authorization": f"Bearer {SessionSigner(SESSION_SECRET).issue(stranger.id)}"}
    body = {"text": "Я тут", "secret": False, "inspiration": False}
    url = f"/api/games/{table.game_id}/action"

    assert (await table.http.put(url, json=body)).status_code == 401
    assert (await table.http.put(url, json=body, headers=foreign)).status_code == 404
    assert (await table.http.put("/api/games/нет-такой/action", json=body)).status_code == 401
    assert await table.actions() == {}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "я" * 3001},
        {"text": "ход", "secret": "yes"},
        {"text": "ход", "inspiration": 1},
        {"text": "ход", "лишнее": True},
        {"text": 5},
    ],
    ids=["длинный", "тайно не булево", "вдохновение не булево", "лишнее поле", "текст не строка"],
)
async def test_a_malformed_draft_is_422_with_a_russian_detail_and_changes_nothing(table, body):
    response = await table.call("alice", "PUT", "/action", json=body)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert await table.actions() == {}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"turn_phase": TurnPhase.RESOLVING}, "разбирает"),
        ({"turn_phase": TurnPhase.DM_FAILED}, "Повторить"),
        ({"turn_phase": TurnPhase.CAMP}, "привал"),
        ({"phase": GamePhase.PAUSED}, "пауз"),
        ({"phase": GamePhase.LOBBY, "turn_phase": None}, "не идёт"),
    ],
    ids=["мастер разбирает", "мастер не справился", "привал", "пауза", "лобби"],
)
async def test_nothing_is_accepted_while_the_game_cannot_take_turns(table, values, expected):
    await table.update_game(**values)

    for response in (
        await table.draft("alice", "Ход"),
        await table.call("alice", "POST", "/action/submit"),
        await table.pass_("alice"),
    ):
        assert response.status_code == 409
        assert expected in response.json()["detail"]
    assert await table.actions() == {}


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        (
            TurnKind.ROUND,
            "Мастер не справился с ходом: ходы сохранены, хост может нажать «Повторить».",
        ),
        (
            TurnKind.COMBAT,
            "Мастер не справился с ходом: ходы сохранены, хост может нажать «Повторить».",
        ),
        (
            TurnKind.CAMP_END,
            "Мастер не справился с концом привала: хост может нажать «Повторить».",
        ),
    ],
    ids=["раунд", "бой", "конец привала"],
)
async def test_a_player_is_told_what_the_failed_turn_kept_and_after_the_camp_end_no_moves(
    table, kind, detail
):
    # У конца привала ходов игроков нет, только реплики у костра: «ходы сохранены» там неправда.
    await table.update_game(turn_phase=TurnPhase.DM_FAILED)
    async with table.db() as session:
        await session.execute(update(Turn).where(Turn.id == table.setup.turn_id).values(kind=kind))
        await session.commit()

    for response in (
        await table.draft("alice", "Ход"),
        await table.call("alice", "POST", "/action/submit"),
        await table.pass_("alice"),
    ):
        assert response.status_code == 409
        assert response.json()["detail"] == detail
    assert await table.actions() == {}


async def put_in_combat(table: Table, current: str, *, paralyzed: tuple[str, ...] = ()) -> None:
    """Игра в бою: порядок Алиса, гоблин, Борис; ходит `current` («alice» или «boris»), а герои из
    `paralyzed` не могут действовать."""
    state = await table.state()
    alice_id, boris_id = table.seats["alice"].character_id, table.seats["boris"].character_id
    for key in paralyzed:
        hero = state.party[table.seats[key].character_id]
        hero.conditions.append(ActiveCondition(id="paralyzed"))
    state.scene = Scene(type=SceneType.COMBAT)
    state.combat = Combat(
        encounter_id=None,
        zones=[Zone(id="hall", name="Зал")],
        links=[],
        combatants={
            alice_id: Combatant(
                id=alice_id, kind="pc", side="party", ref=alice_id, name="Алиса", zone_id="hall"
            ),
            boris_id: Combatant(
                id=boris_id, kind="pc", side="party", ref=boris_id, name="Борис", zone_id="hall"
            ),
            "goblin-1": Combatant(
                id="goblin-1",
                kind="monster",
                side="enemies",
                ref="goblin_warrior",
                name="Гоблин",
                zone_id="hall",
                hp=7,
                hp_max=7,
                ac=15,
            ),
        },
        order=[alice_id, "goblin-1", boris_id],
        turn_index=2 if current == "boris" else 0,
    )
    async with table.db() as session:
        row = await session.get(GameStateSnapshot, (table.game_id, 1))
        row.data = state.model_dump(mode="json")
        await session.commit()
    await table.update_game(turn_phase=TurnPhase.COMBAT_WAIT)


async def test_in_combat_only_the_player_whose_hero_acts_may_write(table):
    await put_in_combat(table, "boris")

    mine = await table.draft("boris", "Ударить гоблина")
    other = await table.draft("alice", "Я тоже хочу")
    third = await table.draft("vera", "И я")

    assert mine.status_code == 200
    assert (other.status_code, third.status_code) == (409, 409)
    assert "очередь" in other.json()["detail"]
    assert list(await table.actions()) == ["boris"]


async def test_a_hero_who_cannot_act_cannot_write_submit_or_pass_and_the_master_is_not_called(
    monkeypatch, table
):
    # Очередь стоит на парализованном герое: бот никого не зовёт, «ходят монстры», и платный ход
    # мастера не должен запускаться «Готов» его игрока (это работа «Повторить» хоста).
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await put_in_combat(table, "boris", paralyzed=("boris",))

    for response in (
        await table.draft("boris", "Хочу ударить"),
        await table.call("boris", "POST", "/action/submit"),
        await table.pass_("boris"),
    ):
        assert response.status_code == 409
        assert "очередь" in response.json()["detail"]
    await table.settle()

    assert provider.requests == []  # платного вызова не было
    assert await table.actions() == {}
    assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT


async def test_the_host_retry_still_continues_a_fight_where_the_current_hero_cannot_act(
    monkeypatch, table
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await put_in_combat(table, "boris", paralyzed=("boris",))

    response = await table.host("retry")
    await table.settle()

    assert response.status_code == 200, response.text
    assert len(provider.requests) == 1  # платный ход запустил хост, а не игрок парализованного
    assert (await table.turns())[0].status == TurnStatus.DONE


# --- сдача хода и пас ---


async def test_submitting_marks_the_player_ready_and_puts_the_action_into_the_common_feed(table):
    await table.draft("alice", "Осмотреть дверь")

    response = await table.call("alice", "POST", "/action/submit")

    assert action_fields(response) == ("submitted", "Осмотреть дверь", False, False)
    assert table.statuses_sent() == [("alice", "ready")]
    [(item, audience)] = table.broadcaster.of_type("feed_item")
    assert audience == "all"
    assert item["item"]["kind"] == "action"
    assert item["item"]["text"] == "Осмотреть дверь"
    assert item["item"]["private"] is False
    assert item["item"]["data"] == {
        "actor_id": table.seats["alice"].character_id,
        "actor": "Алиса",
        "secret": False,
    }
    [stored] = await table.messages(MessageKind.ACTION)
    assert (stored.audience, stored.text, stored.superseded) == ("all", "Осмотреть дверь", False)


async def test_a_secret_action_reaches_only_its_author_and_the_others_never_see_it(table):
    await table.draft("boris", "Украсть ключ у стражника", secret=True)

    await table.call("boris", "POST", "/action/submit")

    [(item, audience)] = table.broadcaster.of_type("feed_item")
    assert audience == table.user_id("boris")
    assert item["item"]["text"] == "Украсть ключ у стражника" and item["item"]["private"] is True
    assert item["item"]["data"]["secret"] is True
    # Статус для всех — просто «готов»: тайный ход он не выдаёт.
    assert table.statuses_sent() == [("boris", "ready")]
    assert [e["text"] for e in await table.feed_of("boris")] == ["Украсть ключ у стражника"]
    for key in ("alice", "vera"):
        assert await table.feed_of(key) == []
    assert "Украсть ключ" not in json.dumps(
        [p for _g, p, who in table.broadcaster.sent if who == "all"], ensure_ascii=False
    )


async def test_a_submit_without_text_is_422_and_leaves_the_player_writing(table):
    nothing = await table.call("alice", "POST", "/action/submit")
    await table.draft("alice", "   \n")
    blank = await table.call("alice", "POST", "/action/submit")

    assert (nothing.status_code, blank.status_code) == (422, 422)
    assert "Пас" in blank.json()["detail"]
    assert (await table.actions())["alice"].status == ActionStatus.DRAFT
    assert table.broadcaster.sent == []


async def test_passing_marks_the_player_as_passed_without_a_line_in_the_feed(table):
    response = await table.pass_("vera")

    assert action_fields(response) == ("pass", "", False, False)
    assert table.statuses_sent() == [("vera", "pass")]
    assert table.broadcaster.of_type("feed_item") == []
    assert (await table.actions())["vera"].status == ActionStatus.PASS


async def test_a_second_submit_of_the_same_action_changes_and_announces_nothing(table):
    await table.submit("alice", "Осмотреть дверь")
    table.broadcaster.sent.clear()

    again = await table.call("alice", "POST", "/action/submit")

    assert again.status_code == 200 and again.json()["status"] == "submitted"
    assert table.broadcaster.sent == []
    assert len(await table.messages(MessageKind.ACTION)) == 1


async def test_editing_a_submitted_action_takes_it_back_to_a_draft_and_withdraws_the_line(table):
    await table.submit("alice", "Осмотреть дверь")
    [first] = await table.messages(MessageKind.ACTION)
    table.broadcaster.sent.clear()

    response = await table.draft("alice", "Выбить дверь")

    assert action_fields(response) == ("draft", "Выбить дверь", False, False)
    assert table.statuses_sent() == [("alice", "writing")]
    [(tombstone, audience)] = table.broadcaster.of_type("feed_item")
    assert tombstone == {"type": "feed_item", "item": {"id": first.id, "superseded": True}}
    assert audience == "all"
    [stored] = await table.messages(MessageKind.ACTION)
    assert stored.superseded is True
    assert await table.feed_of("boris") == []


async def test_submitting_again_after_an_edit_puts_one_new_line_into_the_feed(table):
    await table.submit("alice", "Осмотреть дверь")
    await table.submit("alice", "Выбить дверь")

    lines = [m for m in await table.messages(MessageKind.ACTION) if not m.superseded]

    assert [m.text for m in lines] == ["Выбить дверь"]
    assert [e["text"] for e in await table.feed_of("boris")] == ["Выбить дверь"]


async def test_a_withdrawn_secret_line_is_withdrawn_from_its_author_only(table):
    await table.submit("boris", "Украсть ключ", secret=True)
    table.broadcaster.sent.clear()

    await table.draft("boris", "Украсть ключ", secret=False)  # снял галочку «Тайно»

    [(tombstone, audience)] = table.broadcaster.of_type("feed_item")
    assert tombstone["item"].get("superseded") is True
    assert audience == table.user_id("boris")


async def test_passing_after_a_submit_withdraws_the_line_and_keeps_the_draft_text(table):
    await table.submit("alice", "Осмотреть дверь")

    response = await table.pass_("alice")

    assert response.json()["status"] == "pass"
    assert response.json()["text"] == "Осмотреть дверь"  # черновик цел: «Изменить» вернёт его
    assert [m.superseded for m in await table.messages(MessageKind.ACTION)] == [True]
    assert await table.feed_of("boris") == []


async def test_a_submitted_action_is_readable_by_its_author_in_the_snapshot(table):
    await table.submit("alice", "Осмотреть дверь", secret=True, inspiration=True)

    async with table.db() as session:
        user = await session.get(User, table.user_id("alice"))
        snapshot = await snapshot_for(session, user, table.game_id, srd=table.srd)

    assert snapshot["action"] == {
        "text": "Осмотреть дверь",
        "secret": True,
        "inspiration": True,
        "status": "submitted",
    }
    assert snapshot["statuses"][table.user_id("alice")] == "ready"
    assert snapshot["statuses"][table.user_id("boris")] == "writing"


# --- сбор ходов ---


async def test_there_is_no_countdown_until_the_host_presses_collect(table):
    await table.submit("alice", "Осмотреть дверь")
    await table.scheduler.advance(3 * 3600)  # обсуждение может длиться сколько угодно

    game = await table.game()
    assert (game.turn_phase, game.deadline_at) == (TurnPhase.DISCUSSION, None)
    assert table.scheduler.pending() == {}
    assert [t.status for t in await table.turns()] == [TurnStatus.OPEN]


async def test_collect_with_everybody_ready_starts_the_master_at_once_without_a_countdown(table):
    await table.submit("alice", "Осмотреть дверь")
    await table.submit("boris", "Украсть ключ", secret=True)
    await table.pass_("vera")

    response = await table.host("collect")
    await table.settle()

    assert response.status_code == 200, response.text
    first, second = await table.turns()
    assert (first.status, first.kind, second.status) == (
        TurnStatus.DONE,
        TurnKind.ROUND,
        TurnStatus.OPEN,
    )
    game = await table.game()
    assert (game.turn_phase, game.deadline_at, game.state_version) == (
        TurnPhase.DISCUSSION,
        None,
        2,
    )
    assert table.scheduler.pending() == {}
    assert ("playing", "collecting") not in table.phases_sent()  # сбора как фазы не было
    assert ("playing", "resolving") in table.phases_sent()
    [log] = await table.logs()
    acts = round_actions(log.request["context"])
    assert acts[table.seats["alice"].character_id]["text"] == "Осмотреть дверь"
    assert acts[table.seats["boris"].character_id]["marks"] == ["тайно"]
    assert acts[table.seats["vera"].character_id]["marks"] == ["пас"]


async def test_collect_without_all_ready_starts_the_countdown_and_calls_the_players(table):
    await table.submit("boris", "Украсть ключ")
    await table.draft("alice", "Я ещё пишу")  # хост тоже ещё пишет, но уведомлять себя не надо
    now = table.scheduler.now()

    response = await table.host("collect")

    assert response.status_code == 200, response.text
    deadline = now + timedelta(seconds=COLLECT_SECONDS)
    body = response.json()
    assert (body["phase"], body["turn_phase"]) == ("playing", "collecting")
    assert datetime.fromisoformat(body["deadline_at"]) == deadline
    game = await table.game()
    assert (game.turn_phase, game.deadline_at) == (TurnPhase.COLLECTING, deadline)
    assert [t.status for t in await table.turns()] == [TurnStatus.COLLECTING]
    assert table.scheduler.pending() == {table.game_id: deadline}
    # Событие фазы несёт срок; позвали тех, кто ещё пишет, кроме самого хоста.
    [(event, audience)] = table.broadcaster.of_type("phase")
    assert audience == "all"
    assert datetime.fromisoformat(event["phase"]["deadline_at"]) == deadline
    assert table.notifier.sent == [
        (table.user_id("vera"), f"Хост собирает ходы — осталось {COLLECT_SECONDS} с")
    ]


async def test_the_length_of_the_countdown_is_the_one_set_for_the_game(table):
    await table.update_game(collect_seconds=45)
    now = table.scheduler.now()

    await table.host("collect")

    assert table.scheduler.pending() == {table.game_id: now + timedelta(seconds=45)}


async def test_a_notifier_that_breaks_does_not_break_the_collect(table):
    table.notifier.broken = True

    response = await table.host("collect")

    assert response.status_code == 200
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING


async def test_collect_is_for_the_host_only_and_only_while_the_players_discuss(table):
    friend = await table.host("collect", "boris")
    assert friend.status_code == 403
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION

    assert (await table.host("collect")).status_code == 200
    again = await table.host("collect")
    assert again.status_code == 409 and "идёт" in again.json()["detail"]
    for values in ({"turn_phase": TurnPhase.RESOLVING}, {"turn_phase": TurnPhase.DM_FAILED}):
        await table.update_game(**values)
        assert (await table.host("collect")).status_code == 409


async def test_a_stranger_and_an_unknown_game_get_404_on_every_host_button(table, db):
    async with db() as session:
        stranger = User(dev_login="dev-9", name="Чужой")
        session.add(stranger)
        await session.commit()
    headers = {"Authorization": f"Bearer {SessionSigner(SESSION_SECRET).issue(stranger.id)}"}

    for name in (
        "collect",
        "extend",
        "close",
        "pause",
        "resume",
        "retry",
        "regenerate",
        "hurry",
        "camp-extend",
        "camp-end",
    ):
        url = f"/api/games/{table.game_id}/host/{name}"
        assert (await table.http.post(url, headers=headers)).status_code == 404, name
        assert (await table.http.post(url)).status_code == 401, name
        missing = await table.http.post(f"/api/games/нет-такой/host/{name}", headers=headers)
        assert missing.status_code == 404, name


async def test_every_host_button_is_403_for_a_player_who_is_not_the_host(table):
    for name in (
        "collect",
        "extend",
        "close",
        "pause",
        "resume",
        "retry",
        "regenerate",
        "hurry",
        "camp-extend",
        "camp-end",
    ):
        response = await table.host(name, "boris")
        assert response.status_code == 403, name
        assert isinstance(response.json()["detail"], str)
    game = await table.game()
    assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.DISCUSSION)
    assert table.broadcaster.sent == []


# --- конец отсчёта ---


async def test_when_the_time_is_up_drafts_are_submitted_and_the_rest_pass(table):
    await table.draft("alice", "Осмотреть дверь")  # черновик с текстом
    await table.draft("vera", "")  # пустой черновик
    # у Бориса строки хода нет вовсе
    await table.host("collect")
    table.broadcaster.sent.clear()

    await table.scheduler.advance(COLLECT_SECONDS - 1)
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    await table.scheduler.advance(1)
    await table.settle()

    acts = await table.actions()
    assert {key: row.status for key, row in acts.items()} == {
        "alice": ActionStatus.AUTO_SUBMITTED,
        "boris": ActionStatus.AUTO_PASS,  # строку создал сам сервер
        "vera": ActionStatus.AUTO_PASS,
    }
    assert (await table.turns())[0].status == TurnStatus.DONE
    # Игроки видят новые статусы, а сданный автоматически ход попал в общую ленту.
    assert table.statuses_sent()[:3] == [("alice", "ready"), ("boris", "pass"), ("vera", "pass")]
    assert [m.text for m in await table.messages(MessageKind.ACTION)] == ["Осмотреть дверь"]
    # Мастер получил ход Алисы и пасы остальных, в том числе того, у кого строки не было.
    [log] = await table.logs()
    marks = {i: a["marks"] for i, a in round_actions(log.request["context"]).items()}
    assert marks == {
        table.seats["alice"].character_id: [],
        table.seats["boris"].character_id: ["пас"],
        table.seats["vera"].character_id: ["пас"],
    }
    assert table.scheduler.pending() == {}


async def test_a_draft_written_while_collecting_is_submitted_when_the_time_is_up(table):
    await table.host("collect")
    await table.draft("boris", "Украсть ключ")  # игрок успел дописать уже во время отсчёта

    await table.scheduler.advance(COLLECT_SECONDS)
    await table.settle()

    assert (await table.actions())["boris"].status == ActionStatus.AUTO_SUBMITTED
    assert [m.text for m in await table.messages(MessageKind.ACTION)] == ["Украсть ключ"]


async def test_a_secret_draft_submitted_by_the_countdown_goes_only_to_its_author(table):
    await table.draft("boris", "Украсть ключ", secret=True)
    await table.host("collect")

    await table.scheduler.advance(COLLECT_SECONDS)
    await table.settle()

    assert (await table.actions())["boris"].status == ActionStatus.AUTO_SUBMITTED
    [message] = await table.messages(MessageKind.ACTION)
    assert message.audience == table.user_id("boris")
    for key in ("alice", "vera"):
        assert [e for e in await table.feed_of(key) if e["kind"] == "action"] == []


async def test_the_last_player_to_submit_closes_the_collect_early(table):
    await table.submit("alice", "Осмотреть дверь")
    await table.host("collect")
    await table.submit("boris", "Украсть ключ")
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING  # Вера ещё не готова

    await table.pass_("vera")
    await table.settle()

    assert (await table.turns())[0].status == TurnStatus.DONE
    assert table.scheduler.pending() == {}
    await table.scheduler.advance(10 * COLLECT_SECONDS)
    await table.settle()
    assert len(await table.logs()) == 1  # второго разбора отсчёт не запустил


async def test_the_last_player_to_press_ready_closes_the_collect_early_too(table):
    await table.pass_("alice")
    await table.pass_("boris")
    await table.host("collect")
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING  # Вера ещё пишет

    await table.submit("vera", "Прикрыть отряд")
    await table.settle()

    assert (await table.turns())[0].status == TurnStatus.DONE
    assert table.scheduler.pending() == {}


@pytest.mark.parametrize("status", [ActionStatus.AUTO_SUBMITTED, ActionStatus.AUTO_PASS])
async def test_a_row_that_the_close_has_already_finished_cannot_be_changed(table, status):
    # Закрытие уже отметило строки, но разбор ещё не зарезервирован: правка в этом промежутке
    # не должна подменить ход, который мастер увидит.
    await table.draft("boris", "Украсть ключ")
    await table.host("collect")
    async with table.db() as session:
        row = await session.get(PlayerAction, (table.setup.turn_id, table.user_id("boris")))
        row.status = status
        await session.commit()

    for response in (
        await table.draft("boris", "Передумал"),
        await table.call("boris", "POST", "/action/submit"),
        await table.pass_("boris"),
    ):
        assert response.status_code == 409
        assert "закрыт" in response.json()["detail"]
    row = (await table.actions())["boris"]
    assert (row.text, row.status) == ("Украсть ключ", status)


async def test_close_runs_the_master_now_and_the_countdown_is_gone(table):
    await table.draft("alice", "Осмотреть дверь")
    await table.host("collect")
    await table.scheduler.advance(30)

    response = await table.host("close")
    await table.settle()

    assert response.status_code == 200, response.text
    assert (await table.turns())[0].status == TurnStatus.DONE
    assert (await table.actions())["alice"].status == ActionStatus.AUTO_SUBMITTED
    assert table.scheduler.pending() == {}
    await table.scheduler.advance(10 * COLLECT_SECONDS)
    await table.settle()
    assert len(await table.logs()) == 1


async def test_close_is_refused_when_nothing_is_being_collected(table):
    response = await table.host("close")

    assert response.status_code == 409 and "не идёт" in response.json()["detail"]
    assert (await table.turns())[0].status == TurnStatus.OPEN


async def test_extend_adds_a_minute_to_the_deadline_and_moves_the_timer(table):
    await table.host("collect")
    await table.scheduler.advance(100)
    old = table.scheduler.now() + timedelta(seconds=COLLECT_SECONDS - 100)
    table.broadcaster.sent.clear()

    response = await table.host("extend")

    assert response.status_code == 200, response.text
    new = old + timedelta(seconds=60)
    assert datetime.fromisoformat(response.json()["deadline_at"]) == new
    assert (await table.game()).deadline_at == new
    assert table.scheduler.pending() == {table.game_id: new}
    [(event, _audience)] = table.broadcaster.of_type("phase")
    assert datetime.fromisoformat(event["phase"]["deadline_at"]) == new
    await table.scheduler.advance(79)  # прежний срок (120 с) прошёл, новый (180 с) — нет
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    await table.scheduler.advance(1)
    await table.settle()
    assert (await table.turns())[0].status == TurnStatus.DONE


async def test_extend_works_only_while_collecting(table):
    early = await table.host("extend")
    await table.update_game(turn_phase=TurnPhase.RESOLVING)
    late = await table.host("extend")

    assert (early.status_code, late.status_code) == (409, 409)
    assert table.scheduler.pending() == {}


async def test_the_live_scheduler_closes_the_collect_by_itself(settings, db, srd, adventure):
    setup = await seed_game(db, srd, adventure, collect_seconds=1)
    async with running_table(settings, db, srd, setup, scheduler=Scheduler()) as table:
        await table.draft("alice", "Осмотреть дверь")
        await table.host("collect")
        assert list(table.scheduler.pending()) == [setup.game_id]

        async with asyncio.timeout(TIMEOUT_S):
            while (await table.game()).turn_phase == TurnPhase.COLLECTING:
                await asyncio.sleep(0.05)
        await table.settle()

        assert (await table.turns())[0].status == TurnStatus.DONE
        assert (await table.actions())["alice"].status == ActionStatus.AUTO_SUBMITTED
        assert table.scheduler.pending() == {}


async def test_the_journal_of_the_server_names_the_game_but_never_the_text_of_an_action(
    table, caplog
):
    caplog.set_level(logging.DEBUG, logger="dnd_server")
    secret = "Украсть ключ у стражника ПОЛНОЧЬ"
    await table.draft("boris", secret, secret=True)
    await table.host("collect")
    await table.scheduler.advance(COLLECT_SECONDS)
    await table.settle()
    await table.host("pause")
    await table.host("resume")

    assert "ПОЛНОЧЬ" not in caplog.text and secret not in caplog.text
    assert table.game_id in caplog.text and "сбор ходов" in caplog.text


# --- пауза ---


async def test_pause_freezes_the_countdown_and_resume_gives_back_what_was_left(table):
    await table.host("collect")
    await table.scheduler.advance(50)
    paused_at = table.scheduler.now()
    deadline = paused_at + timedelta(seconds=COLLECT_SECONDS - 50)
    table.broadcaster.sent.clear()

    paused = await table.host("pause")

    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert (body["phase"], body["turn_phase"]) == ("paused", "collecting")
    assert (body["deadline_at"], body["remaining_seconds"]) == (None, 70)
    game = await table.game()
    assert (game.phase, game.deadline_at, game.clock_at) == (GamePhase.PAUSED, deadline, paused_at)
    assert table.scheduler.pending() == {}
    assert table.phases_sent() == [("paused", "collecting")]
    # Игрок, пришедший на паузе, получает снимок с остатком, а не с идущим сроком.
    async with table.db() as session:
        user = await session.get(User, table.user_id("boris"))
        snapshot = await snapshot_for(session, user, table.game_id, srd=table.srd)
    assert (snapshot["phase"]["deadline_at"], snapshot["phase"]["remaining_seconds"]) == (None, 70)

    await table.scheduler.advance(10_000)  # время идёт, отсчёт стоит
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    table.broadcaster.sent.clear()
    resumed_at = table.scheduler.now()
    resumed = await table.host("resume")

    new_deadline = resumed_at + timedelta(seconds=70)
    assert resumed.status_code == 200, resumed.text
    assert datetime.fromisoformat(resumed.json()["deadline_at"]) == new_deadline
    assert resumed.json()["remaining_seconds"] is None
    game = await table.game()
    assert (game.phase, game.deadline_at, game.clock_at) == (
        GamePhase.PLAYING,
        new_deadline,
        resumed_at,
    )
    assert table.scheduler.pending() == {table.game_id: new_deadline}
    assert table.phases_sent() == [("playing", "collecting")]
    await table.scheduler.advance(69)
    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    await table.scheduler.advance(1)
    await table.settle()
    assert (await table.turns())[0].status == TurnStatus.DONE


async def test_pause_without_a_countdown_just_stops_and_resume_starts_the_game_again(table):
    paused = await table.host("pause")
    resumed = await table.host("resume")

    assert paused.json()["phase"] == "paused" and paused.json()["remaining_seconds"] is None
    assert resumed.json()["phase"] == "playing" and resumed.json()["deadline_at"] is None
    assert table.scheduler.pending() == {}
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


async def test_pause_stops_the_time_of_the_game_and_resume_starts_it_again(table):
    await table.scheduler.advance(100)
    await table.host("pause")
    after_pause = await table.game()
    assert after_pause.active_seconds == 100

    await table.scheduler.advance(500)  # пауза: время игры не идёт
    resumed_at = table.scheduler.now()
    await table.host("resume")

    game = await table.game()
    assert (game.active_seconds, game.clock_at) == (100, resumed_at)
    assert game.active_time(resumed_at + timedelta(seconds=30)) == 130


async def test_pause_while_the_master_is_resolving_is_refused(table):
    await table.update_game(turn_phase=TurnPhase.RESOLVING)

    response = await table.host("pause")

    assert response.status_code == 409 and "разбирает" in response.json()["detail"]
    assert (await table.game()).phase == GamePhase.PLAYING


@pytest.mark.parametrize(
    ("button", "values"),
    [
        ("pause", {"phase": GamePhase.LOBBY, "turn_phase": None}),
        ("resume", {}),  # игра идёт, а не стоит
        ("resume", {"phase": GamePhase.LOBBY, "turn_phase": None}),
    ],
    ids=["пауза в лобби", "продолжить идущую", "продолжить из лобби"],
)
async def test_pause_and_resume_need_the_right_state_of_the_game(table, button, values):
    await table.update_game(**values)

    assert (await table.host(button)).status_code == 409


async def test_pausing_twice_is_409_the_second_time(table):
    assert (await table.host("pause")).status_code == 200
    assert (await table.host("pause")).status_code == 409


async def test_a_paused_game_takes_no_turns_and_no_host_commands_but_resume(table):
    await table.host("pause")

    for response in (
        await table.draft("alice", "Ход"),
        await table.call("alice", "POST", "/action/submit"),
        await table.pass_("alice"),
        await table.host("collect"),
        await table.host("extend"),
        await table.host("close"),
        await table.host("retry"),
        await table.host("regenerate"),
        await table.host("camp-extend"),
        await table.host("camp-end"),
        await table.call("alice", "POST", "/camp/line", json={"text": "Подкину дров."}),
    ):
        assert response.status_code == 409
        assert "пауз" in response.json()["detail"]
    assert await table.actions() == {}
    assert (await table.host("resume")).status_code == 200


# --- разбор мастера просит именно тот ход, который видел запросивший (защита в глубину) ---


@pytest.fixture
def asked_turns(monkeypatch) -> list[str | None]:
    """Для какого хода каждый запуск мастера, начатый потоком игры, просил разбор."""
    asked: list[str | None] = []
    run = flow_module.run_dm_turn

    async def spy(game_id: str, **kwargs: Any):
        asked.append(kwargs.get("expected_turn_id"))
        return await run(game_id, **kwargs)

    monkeypatch.setattr(flow_module, "run_dm_turn", spy)
    use_provider(monkeypatch, StubProvider())
    return asked


async def test_the_host_collect_with_everyone_ready_asks_for_the_turn_it_saw(table, asked_turns):
    await ready_round(table)

    await table.host("collect")
    await table.settle()

    assert asked_turns == [table.setup.turn_id]


async def test_the_closing_of_the_collection_asks_for_its_own_turn(table, asked_turns):
    await table.host("collect")

    await table.scheduler.advance(COLLECT_SECONDS)
    await table.settle()

    assert asked_turns == [table.setup.turn_id]


async def test_the_last_player_to_submit_closes_the_collection_for_his_own_turn(table, asked_turns):
    await table.submit("alice", "Осмотреть дверь")
    await table.submit("boris", "Украсть ключ")
    await table.host("collect")
    await table.submit("vera", "Подождать")  # последний сдал: сбор закрывается раньше срока
    await table.settle()

    assert asked_turns == [table.setup.turn_id]


async def test_the_hero_who_ends_his_combat_move_asks_for_the_turn_it_saw(table, asked_turns):
    await put_in_combat(table, "boris")

    await table.submit("boris", "Ударить гоблина")
    await table.settle()

    assert asked_turns == [table.setup.turn_id]


async def test_the_end_of_the_hurry_countdown_asks_for_the_turn_it_closes(table, asked_turns):
    await put_in_combat(table, "boris")
    await table.host("hurry")

    await table.scheduler.advance(60)
    await table.settle()

    assert asked_turns == [table.setup.turn_id]


async def test_the_end_of_the_camp_asks_for_the_turn_it_saw(table, asked_turns):
    async with table.db() as session:
        await session.execute(
            update(Turn).where(Turn.id == table.setup.turn_id).values(kind=TurnKind.CAMP_END)
        )
        await session.commit()
    await table.update_game(turn_phase=TurnPhase.CAMP)

    response = await table.host("camp-end")
    await table.settle()

    assert response.status_code == 200, response.text
    assert asked_turns == [table.setup.turn_id]


async def test_a_start_for_a_turn_that_has_moved_on_is_refused_and_the_master_is_not_called(
    monkeypatch, table
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)

    with pytest.raises(Conflict, match="другой ход"):
        await start_turn(
            Flow.from_app(table.app), table.game_id, mode=TurnKind.ROUND, turn_id="устаревший-ход"
        )
    await table.settle()

    assert provider.requests == []
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


# --- разбор мастера: повтор, перегенерация, правка во время разбора ---


async def ready_round(table: Table) -> None:
    """Все игроки сдали ход (или спасовали): хост может собирать."""
    await table.submit("alice", "Осмотреть дверь")
    await table.submit("boris", "Украсть ключ")
    await table.pass_("vera")


async def test_edits_during_the_resolving_phase_are_409_and_the_master_runs_on(monkeypatch, table):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    await ready_round(table)

    started = await table.host("collect")
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)
    assert started.status_code == 200 and (await table.game()).turn_phase == TurnPhase.RESOLVING

    for response in (
        await table.draft("alice", "Передумала"),
        await table.call("alice", "POST", "/action/submit"),
        await table.pass_("boris"),
    ):
        assert response.status_code == 409
        assert "разбирает" in response.json()["detail"]
    assert (await table.actions())["alice"].text == "Осмотреть дверь"  # ход не изменился
    gate.set()
    await table.settle()
    assert (await table.turns())[0].status == TurnStatus.DONE


async def test_the_request_of_the_host_returns_while_the_master_is_still_thinking(
    monkeypatch, table
):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    await ready_round(table)

    response = await asyncio.wait_for(table.host("collect"), TIMEOUT_S)  # модель ещё стоит

    assert response.status_code == 200
    assert response.json()["turn_phase"] == "resolving"
    gate.set()
    await table.settle()
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


async def test_a_failed_turn_is_repeated_by_the_host_with_the_same_actions(monkeypatch, table):
    provider = StubProvider(ProviderError("модель недоступна"), narration("Всё получилось."))
    use_provider(monkeypatch, provider)
    await ready_round(table)
    await table.host("collect")
    await table.settle()
    failed = await table.game()
    assert failed.turn_phase == TurnPhase.DM_FAILED
    host_id = table.user_id("alice")
    notes = [m for m in await table.messages(MessageKind.SYSTEM) if m.audience == host_id]
    assert len(notes) == 1 and "Повторить" in notes[0].text

    forbidden = await table.host("retry", "boris")
    response = await table.host("retry")
    await table.settle()

    assert forbidden.status_code == 403
    assert response.status_code == 200, response.text
    game = await table.game()
    assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 2)
    logs = await table.logs()
    assert [(log.attempt, log.error is None) for log in logs] == [(1, False), (2, True)]
    # Мастер увидел ровно те же ходы, что и в первый раз.
    first, second = (round_actions(log.request["context"]) for log in logs)
    assert first == second and len(second) == 3
    assert [m.text for m in await table.messages(MessageKind.NARRATION)] == ["Всё получилось."]


async def test_retry_repeats_the_turn_with_its_mode_and_the_fighters_of_the_journal(
    monkeypatch, table
):
    provider = StubProvider(ProviderError("модель недоступна"), narration())
    use_provider(monkeypatch, provider)
    fighters = [table.seats["alice"].character_id]
    deps = Deps.from_app(table.app)
    failed = await run_dm_turn(table.game_id, mode=TurnKind.COMBAT, deps=deps, acting=fighters)
    assert failed.status == TurnStatus.FAILED

    response = await table.host("retry")
    await table.settle()

    assert response.status_code == 200, response.text
    logs = await table.logs()
    assert [(log.request["mode"], log.request["acting"]) for log in logs] == [
        ("combat", fighters),
        ("combat", fighters),
    ]
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


async def test_retry_is_refused_when_the_master_did_not_fail(table):
    response = await table.host("retry")

    assert response.status_code == 409
    assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


async def test_retry_takes_a_game_resolving_without_a_living_master_as_interrupted(
    monkeypatch, table
):
    # В базе «мастер думает», а разбора в памяти нет: не записалась даже короткая отметка неудачи.
    # Сервер перезапускать не нужно: «Повторить» считает такую игру прерванной, как при старте.
    use_provider(monkeypatch, StubProvider(narration("Мастер вернулся.")))
    async with table.db() as session:
        await session.execute(
            update(Game).where(Game.id == table.game_id).values(turn_phase=TurnPhase.RESOLVING)
        )
        await session.commit()
    assert table.game_id not in Deps.from_app(table.app).live

    response = await table.host("retry")
    await table.settle()

    assert response.status_code == 200, response.text
    game = await table.game()
    assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 2)
    lost, done = await table.logs()
    assert (lost.error, lost.output) == (MASTER_LOST, None)
    assert done.error is None and done.output == "Мастер вернулся."
    assert await table.messages(MessageKind.SYSTEM) == []  # хост сам нажал кнопку: ему не пишут


async def test_the_host_regenerates_the_last_story_and_the_old_one_is_withdrawn(table):
    await ready_round(table)
    await table.host("collect")
    await table.settle()
    [old] = await table.messages(MessageKind.NARRATION)
    table.broadcaster.sent.clear()

    forbidden = await table.host("regenerate", "vera")
    response = await table.host("regenerate")
    await table.settle()

    assert forbidden.status_code == 403
    assert response.status_code == 200, response.text
    stories = await table.messages(MessageKind.NARRATION)
    assert [(m.id == old.id, m.superseded) for m in stories] == [(True, True), (False, False)]
    assert (await table.game()).state_version == 3  # номера версий растут только вперёд
    items = [payload["item"] for payload, _who in table.broadcaster.of_type("feed_item")]
    assert old.id in {item["id"] for item in items if item.get("superseded")}
    assert len(await table.logs()) == 2


async def test_regenerate_is_409_when_there_is_nothing_to_regenerate(table):
    response = await table.host("regenerate")

    assert response.status_code == 409
    assert (await table.game()).state_version == 1


async def test_the_host_buttons_that_start_the_master_are_409_while_it_is_thinking(
    monkeypatch, table
):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    await ready_round(table)
    await table.host("collect")
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    responses = [await table.host(name) for name in ("collect", "close", "retry", "regenerate")]

    assert [r.status_code for r in responses] == [409, 409, 409, 409]
    assert len(provider.requests) == 1
    gate.set()
    await table.settle()


# --- гонки: ровно один разбор ---


async def test_two_simultaneous_closes_start_the_master_once(monkeypatch, table):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await table.draft("alice", "Осмотреть дверь")
    await table.host("collect")

    first, second = await asyncio.gather(table.host("close"), table.host("close"))
    await table.settle()

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert len(provider.requests) == 1
    assert [t.status for t in await table.turns()] == [TurnStatus.DONE, TurnStatus.OPEN]
    assert len(await table.logs()) == 1


async def test_two_simultaneous_collects_with_everybody_ready_start_the_master_once(
    monkeypatch, table
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await ready_round(table)

    first, second = await asyncio.gather(table.host("collect"), table.host("collect"))
    await table.settle()

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert len(provider.requests) == 1
    assert (await table.game()).state_version == 2


async def test_the_countdown_and_the_close_button_at_the_same_moment_start_the_master_once(
    monkeypatch, table
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await table.draft("alice", "Осмотреть дверь")
    await table.host("collect")

    _timer, button = await asyncio.gather(
        table.scheduler.advance(COLLECT_SECONDS), table.host("close")
    )
    await table.settle()

    assert button.status_code in (200, 409)
    assert len(provider.requests) == 1
    assert len(await table.logs()) == 1
    assert [t.status for t in await table.turns()] == [TurnStatus.DONE, TurnStatus.OPEN]


async def test_the_last_submit_and_the_close_button_at_the_same_moment_start_the_master_once(
    monkeypatch, table
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await table.submit("alice", "Осмотреть дверь")
    await table.submit("boris", "Украсть ключ")
    await table.host("collect")
    await table.draft("vera", "Прикрыть отряд")

    submit, button = await asyncio.gather(
        table.call("vera", "POST", "/action/submit"), table.host("close")
    )
    await table.settle()

    assert submit.status_code in (200, 409) and button.status_code in (200, 409)
    assert len(provider.requests) == 1
    assert [t.status for t in await table.turns()] == [TurnStatus.DONE, TurnStatus.OPEN]


async def test_a_stale_timer_of_an_old_turn_closes_nothing_in_the_next_one(table):
    from dnd_server.turns.flow import Flow, on_deadline

    await table.draft("alice", "Осмотреть дверь")
    await table.host("collect")
    await table.host("close")
    await table.settle()
    await table.host("collect")  # следующий ход со своим отсчётом
    table.scheduler.cancel(table.game_id)
    await table.update_game(deadline_at=table.scheduler.now() - timedelta(seconds=1))  # и он вышел

    await on_deadline(Flow.from_app(table.app), table.game_id, table.setup.turn_id)

    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    assert len(await table.logs()) == 1


async def test_a_timer_that_fires_before_the_moved_deadline_closes_nothing(table):
    from dnd_server.turns.flow import Flow, on_deadline

    await table.host("collect")
    await table.host("extend")  # срок ушёл вперёд, а старая задача отсчёта уже стартовала
    turn_id = (await table.game()).current_turn_id

    await on_deadline(Flow.from_app(table.app), table.game_id, turn_id)

    assert (await table.game()).turn_phase == TurnPhase.COLLECTING
    assert await table.logs() == []


async def test_the_game_goes_round_after_round_by_every_way_of_closing_the_collect(table):
    # Раунд 1: все готовы, хост собирает ходы.
    await ready_round(table)
    await table.host("collect")
    await table.settle()
    # Раунд 2: сбор закрывает отсчёт.
    await table.draft("alice", "Идти дальше")
    await table.host("collect")
    await table.scheduler.advance(COLLECT_SECONDS)
    await table.settle()
    # Раунд 3: сбор закрывает хост.
    await table.draft("boris", "Осмотреться")
    await table.host("collect")
    await table.host("close")
    await table.settle()

    turns = await table.turns()
    assert [(t.number, t.status) for t in turns] == [
        (1, TurnStatus.DONE),
        (2, TurnStatus.DONE),
        (3, TurnStatus.DONE),
        (4, TurnStatus.OPEN),
    ]
    game = await table.game()
    assert (game.turn_phase, game.state_version, game.current_turn_id) == (
        TurnPhase.DISCUSSION,
        4,
        turns[-1].id,
    )
    assert len(await table.logs()) == 3
    # Ходы каждого раунда остались при своём ходе, а следующий раунд начался чистым.
    assert (await table.actions(turns[1].id))["alice"].status == ActionStatus.AUTO_SUBMITTED
    assert await table.actions(turns[3].id) == {}


# --- перезапуск сервера ---


async def test_the_countdown_is_restored_from_the_database_after_a_restart(
    settings, db, srd, setup
):
    async with running_table(settings, db, srd, setup) as first:
        await first.draft("alice", "Осмотреть дверь")
        await first.host("collect")
        await first.scheduler.advance(30)
        deadline = (await first.game()).deadline_at
        assert first.scheduler.pending() == {setup.game_id: deadline}

    # Сервер остановили: прежний планировщик не помнит ничего, но срок лежит в базе.
    resumed = ManualScheduler(setup.created + timedelta(seconds=40))
    async with running_table(settings, db, srd, setup, scheduler=resumed) as second:
        assert second.scheduler.pending() == {setup.game_id: deadline}
        assert (await second.game()).turn_phase == TurnPhase.COLLECTING
        await second.scheduler.advance(79)
        assert (await second.game()).turn_phase == TurnPhase.COLLECTING
        await second.scheduler.advance(1)
        await second.settle()

        assert (await second.turns())[0].status == TurnStatus.DONE
        assert (await second.actions())["alice"].status == ActionStatus.AUTO_SUBMITTED


async def test_a_countdown_that_ran_out_while_the_server_was_down_fires_at_once(
    settings, db, srd, setup
):
    async with running_table(settings, db, srd, setup) as first:
        await first.host("collect")

    late = ManualScheduler(setup.created + timedelta(hours=2))
    async with running_table(settings, db, srd, setup, scheduler=late) as second:
        await second.scheduler.advance(0)
        await second.settle()

        assert (await second.turns())[0].status == TurnStatus.DONE
        assert {row.status for row in (await second.actions()).values()} == {ActionStatus.AUTO_PASS}


async def test_a_game_on_pause_gets_no_timer_and_resumes_with_the_saved_remainder(
    settings, db, srd, setup
):
    async with running_table(settings, db, srd, setup) as first:
        await first.host("collect")
        await first.scheduler.advance(50)
        await first.host("pause")

    later = ManualScheduler(setup.created + timedelta(hours=1))
    async with running_table(settings, db, srd, setup, scheduler=later) as second:
        assert second.scheduler.pending() == {}
        assert (await second.game()).phase == GamePhase.PAUSED
        resumed = await second.host("resume")

        assert resumed.status_code == 200
        assert second.scheduler.pending() == {
            setup.game_id: later.now() + timedelta(seconds=COLLECT_SECONDS - 50)
        }


async def test_a_game_that_cannot_be_marked_after_a_restart_is_logged_without_the_input(
    monkeypatch, settings, db, srd, setup, caplog, leak
):
    async with db() as session:  # процесс умер посреди хода: игра осталась в «мастер думает»
        await session.execute(
            update(Game).where(Game.id == setup.game_id).values(turn_phase=TurnPhase.RESOLVING)
        )
        await session.commit()

    async def broken(*_args: Any) -> None:
        raise leak.error

    monkeypatch.setattr(flow_module, "_fail_interrupted", broken)
    with caplog.at_level(logging.ERROR):
        async with running_table(settings, db, srd, setup):
            pass  # сервер поднялся, несмотря на неисправную игру

    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert setup.game_id in record.getMessage() and record.exc_info is None
    assert "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_a_game_in_discussion_has_no_timer_after_a_restart(settings, db, srd, setup):
    async with running_table(settings, db, srd, setup) as table:
        assert table.scheduler.pending() == {}
        assert (await table.game()).turn_phase == TurnPhase.DISCUSSION


async def test_a_turn_that_was_resolving_when_the_process_died_becomes_dm_failed(
    monkeypatch, settings, db, srd, adventure
):
    calls = use_provider(monkeypatch, StubProvider())
    setup = await seed_game(db, srd, adventure, turn_phase=TurnPhase.RESOLVING)
    async with db() as session:
        turn = await session.get(Turn, setup.turn_id)
        turn.status = TurnStatus.RESOLVING
        await session.commit()

    async with running_table(settings, db, srd, setup) as table:
        game = await table.game()
        turn = (await table.turns())[0]
        # Ход сам не перезапускается (решение D3): модель не вызывалась, хост видит отказ.
        assert (game.turn_phase, game.deadline_at, turn.status) == (
            TurnPhase.DM_FAILED,
            None,
            TurnStatus.FAILED,
        )
        assert calls == [] and table.scheduler.pending() == {}
        [note] = await table.messages(MessageKind.SYSTEM)
        assert note.audience == table.user_id("alice")
        assert "перезапу" in note.text and "Повторить" in note.text
        [log] = await table.logs()
        assert (log.attempt, log.output) == (1, None)
        assert "перезапу" in log.error
        assert await table.feed_of("boris") == []  # остальным игрокам записи нет

        # Хост повторяет ход обычной кнопкой.
        await table.host("retry")
        await table.settle()
        assert (await table.game()).turn_phase == TurnPhase.DISCUSSION
        assert [log.attempt for log in await table.logs()] == [1, 2]


async def test_the_opening_turn_that_was_resolving_is_dm_failed_and_stays_in_weaving(
    settings, db, srd, adventure
):
    setup = await seed_game(
        db,
        srd,
        adventure,
        phase=GamePhase.WEAVING,
        turn_phase=TurnPhase.RESOLVING,
        started_at=datetime.now(UTC),
    )
    async with db() as session:
        (await session.get(Turn, setup.turn_id)).kind = TurnKind.OPENING
        (await session.get(Turn, setup.turn_id)).status = TurnStatus.RESOLVING
        await session.commit()

    async with running_table(settings, db, srd, setup) as table:
        game = await table.game()
        assert (game.phase, game.turn_phase) == (GamePhase.WEAVING, TurnPhase.DM_FAILED)
        retry = await table.host("retry")
        await table.settle()
        assert retry.status_code == 200
        assert (await table.game()).phase == GamePhase.PLAYING  # открытие прошло с повтора


# --- вплетение героев и открытие игры (план B7.8, решение D3) ---


async def begin_game(
    db: async_sessionmaker[AsyncSession],
    setup: Setup,
    *,
    turn_phase: TurnPhase | None,
    keep_state: bool = False,
    phase: GamePhase = GamePhase.WEAVING,
) -> None:
    """Игра в самом начале: ни хода, ни (по умолчанию) состояния, как сразу после «Начать»."""
    async with db() as session:
        await session.execute(
            update(Game)
            .where(Game.id == setup.game_id)
            .values(
                phase=phase,
                turn_phase=turn_phase,
                current_turn_id=None,
                state_version=1 if keep_state else 0,
                deadline_at=None,
            )
        )
        await session.execute(delete(Turn).where(Turn.game_id == setup.game_id))
        if not keep_state:
            await session.execute(
                delete(GameStateSnapshot).where(GameStateSnapshot.game_id == setup.game_id)
            )
        await session.commit()


class WeavingStub(StubProvider):
    """Модель вплетения: ждёт ворота, если они есть, и связывает каждого героя с деревней."""

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        super().__init__()
        self.json_gate = gate
        self.json_started = asyncio.Event()
        self.json_requests = 0

    async def complete_json(self, req: Any, schema: Any) -> Any:
        self.json_requests += 1
        self.json_started.set()
        if self.json_gate is not None:
            await self.json_gate.wait()
        lines = [line for line in req.prompt.splitlines() if line.startswith('{"id": "pc_')]
        heroes = [json.loads(line)["id"] for line in lines]
        links = [{"location_id": "village", "note": "здесь вы выросли"}]
        hooks = [{"character_id": hero, "location_links": links} for hero in heroes]
        return schema.model_validate({"hooks": hooks})


@pytest.mark.parametrize(
    "turn_phase", [TurnPhase.DM_FAILED, None], ids=["dm_failed", "вплетение не начиналось"]
)
async def test_the_host_repeats_a_weaving_that_has_no_state_with_the_retry_button(
    settings, db, srd, adventure, turn_phase
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=turn_phase)

    async with running_table(settings, db, srd, setup) as table:
        response = await table.host("retry")
        await table.settle()

        assert response.status_code == 200, response.text
        assert response.json()["phase"] in ("weaving", "playing")
        game = await table.game()
        assert (game.phase, game.turn_phase, game.state_version) == (
            GamePhase.PLAYING,
            TurnPhase.DISCUSSION,
            2,
        )
        [opening, following] = await table.turns()
        assert (opening.kind, following.kind) == (TurnKind.OPENING, TurnKind.ROUND)


async def test_players_are_told_by_the_bot_that_the_game_has_begun_not_that_it_goes_on(
    settings, db, srd, adventure
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=TurnPhase.DM_FAILED)

    async with running_table(settings, db, srd, setup) as table:
        await table.host("retry")
        await table.settle()

        for key in table.seats:
            assert table.notifier.texts_for(table.user_id(key)) == [game_started_text()]

        # Следующий ход мастера — обычное «Игра продолжается».
        await table.submit("alice", "Осматриваю деревню")
        for key in ("boris", "vera"):
            await table.pass_(key)
        await table.host("collect")
        await table.settle()
        for key in table.seats:
            assert table.notifier.texts_for(table.user_id(key)) == [
                game_started_text(),
                game_continues_text(),
            ]


async def test_a_weaving_that_is_going_on_cannot_be_started_a_second_time(
    settings, db, srd, adventure, monkeypatch
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None)
    gate = asyncio.Event()
    stub = WeavingStub(gate=gate)
    use_provider(monkeypatch, stub)

    async with running_table(settings, db, srd, setup) as table:
        first = await table.host("retry")
        await asyncio.wait_for(stub.json_started.wait(), TIMEOUT_S)
        assert (await table.game()).turn_phase == TurnPhase.RESOLVING

        second = await table.host("retry")
        gate.set()
        await table.settle()

        assert first.status_code == 200
        assert second.status_code == 409  # вплетение уже идёт: повторять нечего
        assert stub.json_requests == 1
        assert (await table.game()).phase == GamePhase.PLAYING


async def test_only_the_host_can_repeat_a_weaving(settings, db, srd, adventure, monkeypatch):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=TurnPhase.DM_FAILED)
    stub = WeavingStub()
    use_provider(monkeypatch, stub)

    async with running_table(settings, db, srd, setup) as table:
        response = await table.host("retry", key="boris")

        assert response.status_code == 403
        assert stub.json_requests == 0
        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED


async def test_a_weaving_cut_short_by_a_restart_is_dm_failed_and_the_host_repeats_it(
    settings, db, srd, adventure
):
    # Сервер умер, пока модель вплетала героев: ни хода, ни состояния ещё нет.
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=TurnPhase.RESOLVING)

    async with running_table(settings, db, srd, setup) as table:
        game = await table.game()
        assert (game.phase, game.turn_phase, game.deadline_at) == (
            GamePhase.WEAVING,
            TurnPhase.DM_FAILED,
            None,
        )
        [note] = await table.messages(MessageKind.SYSTEM)
        assert (note.audience, note.turn_id) == (table.user_id("alice"), None)
        assert note.text == (
            "Мастер не справился с началом игры: сервер был перезапущен во время вплетения "
            "героев в историю. Нажмите «Повторить»."
        )
        assert await table.logs() == [] and await table.turns() == []  # хода не было
        assert await table.feed_of("boris") == []  # остальным игрокам записи нет
        # Часы и отсчёты не тронуты: игра сама не пошла.
        assert table.scheduler.pending() == {}

        retry = await table.host("retry")
        await table.settle()

        assert retry.status_code == 200
        assert (await table.game()).phase == GamePhase.PLAYING


async def cut_by_a_restart(db, setup, *, kind: TurnKind, phase: GamePhase) -> None:
    """Сервер умер, пока мастер разбирал ход вида `kind`: игра `resolving`, ход открыт."""
    async with db() as session:
        await session.execute(
            update(Game)
            .where(Game.id == setup.game_id)
            .values(phase=phase, turn_phase=TurnPhase.RESOLVING, deadline_at=None)
        )
        await session.execute(
            update(Turn)
            .where(Turn.id == setup.turn_id)
            .values(kind=kind, status=TurnStatus.RESOLVING)
        )
        await session.commit()


@pytest.mark.parametrize(
    ("kind", "phase", "text"),
    [
        (
            TurnKind.OPENING,
            GamePhase.WEAVING,
            "Мастер не справился с началом игры: сервер был перезапущен во время разбора. "
            "Нажмите «Повторить».",
        ),
        (
            TurnKind.ROUND,
            GamePhase.PLAYING,
            "Мастер не справился с ходом: сервер был перезапущен во время разбора. "
            "Ходы игроков сохранены — нажмите «Повторить».",
        ),
        (
            TurnKind.COMBAT,
            GamePhase.PLAYING,
            "Мастер не справился с ходом: сервер был перезапущен во время разбора. "
            "Ходы игроков сохранены — нажмите «Повторить».",
        ),
        (
            TurnKind.CAMP_END,
            GamePhase.PLAYING,
            "Мастер не справился с концом привала: сервер был перезапущен во время разбора. "
            "Нажмите «Повторить».",
        ),
    ],
    ids=["открытие игры", "раунд", "бой", "конец привала"],
)
async def test_the_note_after_a_restart_depends_on_the_kind_of_the_cut_short_turn(
    settings, db, srd, adventure, kind, phase, text
):
    # У открытия игры ходов игроков нет: о них говорят только после ходов, где они есть.
    setup = await seed_game(db, srd, adventure)
    await cut_by_a_restart(db, setup, kind=kind, phase=phase)

    async with running_table(settings, db, srd, setup) as table:
        [note] = await table.messages(MessageKind.SYSTEM)

        assert note.audience == table.user_id("alice")
        assert note.text == text
        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED


async def test_a_player_cannot_write_while_the_opening_is_failed_and_is_not_told_about_moves(
    settings, db, srd, adventure
):
    # Пока открытие не удалось, игра в `weaving`, а не в `playing`: ходы не принимаются, и
    # причина названа без «ходы сохранены» (их у открытия нет).
    setup = await seed_game(db, srd, adventure)
    await cut_by_a_restart(db, setup, kind=TurnKind.OPENING, phase=GamePhase.WEAVING)

    async with running_table(settings, db, srd, setup) as table:
        response = await table.draft("boris", "Иду вперёд")

        assert response.status_code == 409
        assert "сохранен" not in response.json()["detail"].lower()
        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED


async def test_a_game_with_the_weaving_written_but_the_opening_not_begun_is_dm_failed(
    settings, db, srd, adventure, tmp_path
):
    # Сервер умер между вплетением и открытием: состояние версии 1 есть, хода нет.
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None, keep_state=True)
    no_weaving = tmp_path / "no_weaving.yaml"  # вплетения в сценарии нет: его просить нельзя
    no_weaving.write_text(yaml.safe_dump({"turns": SCENARIO["turns"]}, allow_unicode=True), "utf-8")
    quiet = settings.model_copy(update={"fake_llm_scenario": no_weaving})

    async with running_table(quiet, db, srd, setup) as table:
        game = await table.game()
        assert (game.phase, game.turn_phase, game.state_version) == (
            GamePhase.WEAVING,
            TurnPhase.DM_FAILED,
            1,
        )
        [note] = await table.messages(MessageKind.SYSTEM)
        assert note.audience == table.user_id("alice") and "Повторить" in note.text

        retry = await table.host("retry")
        await table.settle()

        assert retry.status_code == 200
        game = await table.game()
        assert (game.phase, game.turn_phase, game.state_version) == (
            GamePhase.PLAYING,
            TurnPhase.DISCUSSION,
            2,
        )


@pytest.mark.parametrize(
    ("phase", "turn_phase", "has_turn", "version", "interrupted"),
    [
        (GamePhase.PLAYING, TurnPhase.RESOLVING, True, 2, True),  # мастер думал над ходом
        (GamePhase.WEAVING, TurnPhase.RESOLVING, False, 0, True),  # вплетение шло
        (GamePhase.WEAVING, TurnPhase.RESOLVING, True, 1, True),  # открытие шло
        (GamePhase.WEAVING, None, False, 1, True),  # вплетение записано, открытие не начиналось
        (GamePhase.WEAVING, None, False, 0, False),  # после «Начать» мастера ещё не звали
        (GamePhase.WEAVING, TurnPhase.DM_FAILED, False, 0, False),  # хост уже видит «Повторить»
        (GamePhase.WEAVING, None, True, 1, False),  # у открытия есть ход: им занимается ход
        (GamePhase.PLAYING, TurnPhase.DISCUSSION, True, 2, False),
        (GamePhase.LOBBY, None, False, 0, False),
        (GamePhase.FINISHED, TurnPhase.RESOLVING, True, 5, False),
    ],
)
async def test_which_games_a_restart_has_cut_short(
    settings, db, srd, adventure, phase, turn_phase, has_turn, version, interrupted
):
    setup = await seed_game(db, srd, adventure)
    async with db() as session:
        await session.execute(
            update(Game)
            .where(Game.id == setup.game_id)
            .values(
                phase=phase,
                turn_phase=turn_phase,
                current_turn_id=setup.turn_id if has_turn else None,
                state_version=version,
                deadline_at=None,
            )
        )
        await session.commit()

    async with running_table(settings, db, srd, setup) as table:
        game = await table.game()

        # Прерванная игра переведена в `dm_failed` и хост получил сообщение, остальные как были.
        assert game.turn_phase == (TurnPhase.DM_FAILED if interrupted else turn_phase)
        assert len(await table.messages(MessageKind.SYSTEM)) == int(interrupted)


async def test_a_game_that_has_only_been_started_is_left_alone_by_the_restore(
    settings, db, srd, adventure
):
    # Старт записан, мастера ещё не звали (так выглядит игра после «Начать» в test_restart):
    # восстановление её не трогает, отсчётов и записей не создаёт.
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None)

    async with running_table(settings, db, srd, setup) as table:
        game = await table.game()

        assert (game.phase, game.turn_phase, game.state_version) == (GamePhase.WEAVING, None, 0)
        assert await table.messages() == [] and table.scheduler.pending() == {}


async def test_the_game_hooks_of_the_app_start_the_weaving_after_the_start_of_the_lobby(
    settings, db, srd, adventure
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None, phase=GamePhase.LOBBY)
    async with db() as session:  # лобби: игроки готовы, но игру ещё не начали
        await session.execute(
            update(Game).where(Game.id == setup.game_id).values(started_at=None, clock_at=None)
        )
        await session.commit()

    async with running_table(settings, db, srd, setup) as table:
        assert isinstance(table.app.state.game_hooks, FlowHooks)

        started = await table.call("alice", "POST", "/start")
        await table.settle()

        assert started.status_code == 200, started.text
        assert started.json()["phase"] == "weaving"
        game = await table.game()
        assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.DISCUSSION)
        story = await table.messages(MessageKind.NARRATION)
        assert [m.audience for m in story] == ["all"]
        assert [item["kind"] for item in await table.feed_of("boris")] == ["narration"]


async def test_the_hook_returns_after_the_weaving_is_reserved_and_the_rest_goes_on_in_the_set(
    settings, db, srd, adventure, monkeypatch
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None)
    gate = asyncio.Event()
    stub = WeavingStub(gate=gate)
    use_provider(monkeypatch, stub)

    async with running_table(settings, db, srd, setup) as table:
        await asyncio.wait_for(table.app.state.game_hooks.on_game_started(table.game_id), TIMEOUT_S)

        # Хук вернулся, пока модель ещё думает: игра уже «разбирается», а задача — в наборе.
        assert (await table.game()).turn_phase == TurnPhase.RESOLVING
        assert len(table.app.state.turn_tasks) == 1
        gate.set()
        await table.settle()

        assert (await table.game()).phase == GamePhase.PLAYING


async def test_a_stopping_server_leaves_a_weaving_failed_and_not_resolving(
    settings, db, srd, adventure, monkeypatch
):
    setup = await seed_game(db, srd, adventure)
    await begin_game(db, setup, turn_phase=None)
    stub = WeavingStub(gate=asyncio.Event())
    use_provider(monkeypatch, stub)

    async with running_table(settings, db, srd, setup) as table:
        await table.app.state.game_hooks.on_game_started(table.game_id)
        await asyncio.wait_for(stub.json_started.wait(), TIMEOUT_S)

        await table.app.state.turn_tasks.shutdown()  # то же, что делает остановка сервера

        game = await table.game()
        assert (game.phase, game.turn_phase) == (GamePhase.WEAVING, TurnPhase.DM_FAILED)


async def test_a_game_that_was_not_resolving_is_not_touched_by_the_restore(
    settings, db, srd, setup
):
    async with running_table(settings, db, srd, setup) as table:
        before = await table.game()
    async with running_table(settings, db, srd, setup) as table:
        after = await table.game()

        assert (after.phase, after.turn_phase, after.state_version, after.deadline_at) == (
            before.phase,
            before.turn_phase,
            before.state_version,
            before.deadline_at,
        )
        assert await table.messages() == [] and await table.logs() == []


async def test_a_database_without_the_schema_does_not_stop_the_server(settings, tmp_path, caplog):
    # Файл базы есть, таблиц нет (миграции не выполнены): сервер стартует, восстановление
    # пишет понятную ошибку, а первый запрос игры ответит обычным отказом.
    path = tmp_path / "data" / "bare.sqlite3"
    path.parent.mkdir()
    path.write_bytes(b"")
    bare = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{path}"})
    app = create_app(bare)
    app.state.scheduler = ManualScheduler()

    with caplog.at_level(logging.ERROR):
        async with app.router.lifespan_context(app):
            pass

    assert "восстанов" in caplog.text.lower()


# --- сборка приложения и остановка ---


def test_the_app_holds_the_parts_of_the_flow_in_its_state(settings):
    from dnd_server.turns.flow import scene_follow_up

    app = create_app(settings)

    assert isinstance(app.state.scheduler, Scheduler)
    assert isinstance(app.state.turn_tasks, TaskSet)
    assert isinstance(app.state.game_providers, GameProviders)
    assert app.state.turn_follow_up is scene_follow_up


def test_every_route_of_the_flow_is_mounted(settings, route_table):
    paths = {(route.method, route.path) for route in route_table(create_app(settings))}

    expected = {
        ("PUT", "/api/games/{game_id}/action"),
        ("POST", "/api/games/{game_id}/action/submit"),
        ("POST", "/api/games/{game_id}/action/pass"),
        *(
            ("POST", f"/api/games/{{game_id}}/host/{name}")
            for name in (
                "collect",
                "extend",
                "close",
                "pause",
                "resume",
                "retry",
                "regenerate",
                "hurry",
                "camp-extend",
                "camp-end",
            )
        ),
        ("POST", "/api/games/{game_id}/camp/line"),
    }
    assert expected <= paths


@pytest.mark.parametrize(
    ("scene", "in_combat", "expected"),
    [
        (SceneType.EXPLORATION, False, (TurnPhase.DISCUSSION, TurnKind.ROUND)),
        (SceneType.SOCIAL, False, (TurnPhase.DISCUSSION, TurnKind.ROUND)),
        # Бой идёт: ход у героя по инициативе, ходы пишет только он (B7.5).
        (SceneType.COMBAT, True, (TurnPhase.COMBAT_WAIT, TurnKind.COMBAT)),
        # Сцена «бой» без самого боя — ошибка данных; игра не должна встать в ожидание героя.
        (SceneType.COMBAT, False, (TurnPhase.DISCUSSION, TurnKind.ROUND)),
        # Привал (B7.6): фаза camp; следующий ход — «конец привала», его запускает отсчёт или хост.
        (SceneType.CAMP, False, (TurnPhase.CAMP, TurnKind.CAMP_END)),
    ],
    ids=["исследование", "разговор", "бой", "бой без боя", "привал"],
)
def test_the_next_phase_after_the_master_is_chosen_by_the_scene(scene, in_combat, expected):
    from dnd_server.turns.flow import scene_follow_up

    combat = (
        Combat(encounter_id=None, zones=[Zone(id="hall", name="Зал")], links=[], combatants={})
        if in_combat
        else None
    )
    state = GameState(
        game_id="g", adventure_id="a", rules=RulesRef(), party={}, location_id="l"
    ).model_copy(update={"scene": Scene(type=scene), "combat": combat})

    assert scene_follow_up(state) == expected


async def test_stopping_the_server_drops_the_timers_and_stops_the_thinking_master(
    monkeypatch, settings, db, srd, setup
):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    async with running_table(settings, db, srd, setup) as table:
        await ready_round(table)
        await table.host("collect")
        await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)
    # Остановка отменила разбор: ход записан неудачным, игра не осталась в `resolving`.

    async with db() as session:
        game = await session.get(Game, setup.game_id)
        turn = await session.get(Turn, setup.turn_id)
    assert (game.turn_phase, turn.status) == (TurnPhase.DM_FAILED, TurnStatus.FAILED)


async def test_stopping_the_server_cancels_the_pending_timers(settings, db, srd, setup):
    scheduler = ManualScheduler(setup.created)
    async with running_table(settings, db, srd, setup, scheduler=scheduler) as table:
        await table.host("collect")
        assert scheduler.pending()

    assert scheduler.pending() == {}


class FakeSocket:
    """Сокет игрока для настоящего хаба: складывает принятые кадры."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        self.frames.append(json.loads(data))


async def test_the_real_hub_delivers_the_events_of_the_flow_and_hides_the_secret(table, caplog):
    hub = table.app.state.hub
    table.app.state.broadcaster = hub  # настоящий хаб вместо регистратора: события идут в JSON
    sockets = {key: FakeSocket() for key in table.seats}
    connections = [hub.connect(table.game_id, table.user_id(k), sockets[k]) for k in sockets]
    for connection in connections:
        connection.start()
    secret = "Украсть ключ у стражника ПОЛНОЧЬ"

    with caplog.at_level(logging.ERROR):
        await table.submit("boris", secret, secret=True)
        await table.submit("alice", "Осмотреть дверь")
        await table.host("collect")
        await table.host("pause")
        await table.host("resume")
        await table.host("close")
        await table.settle()
        for _ in range(50):
            await asyncio.sleep(0)

    for connection in connections:
        hub.disconnect(connection)
    assert caplog.records == []  # хаб не отверг ни одно событие: все они сериализуются
    seen = {key: json.dumps(socket.frames, ensure_ascii=False) for key, socket in sockets.items()}
    assert "ПОЛНОЧЬ" in seen["boris"]
    assert "ПОЛНОЧЬ" not in seen["alice"] and "ПОЛНОЧЬ" not in seen["vera"]
    for key, socket in sockets.items():
        kinds = [frame["type"] for frame in socket.frames]
        assert {"phase", "player_status", "feed_item"} <= set(kinds), key
    paused = [f["phase"] for f in sockets["vera"].frames if f["type"] == "phase"]
    assert any(p["phase"] == "paused" and p["remaining_seconds"] for p in paused)


# --- уведомления бота ---


async def test_the_players_are_told_that_the_game_goes_on_after_the_story(table):
    await ready_round(table)
    table.notifier.sent.clear()

    await table.host("collect")
    await table.settle()

    assert sorted(table.notifier.sent) == sorted(
        (table.user_id(key), "Игра продолжается") for key in ("alice", "boris", "vera")
    )


async def test_nobody_is_told_that_the_game_goes_on_when_the_master_failed(monkeypatch, table):
    use_provider(monkeypatch, StubProvider(ProviderError("модель недоступна")))
    await ready_round(table)
    table.notifier.sent.clear()

    await table.host("collect")
    await table.settle()

    assert table.notifier.sent == []


async def test_a_broken_notifier_does_not_break_the_turn(table):
    await ready_round(table)
    table.notifier.broken = True

    await table.host("collect")
    await table.settle()

    assert (await table.turns())[0].status == TurnStatus.DONE


# --- часы игры ---


def test_the_time_of_a_running_game_is_the_saved_time_plus_what_passed_after_the_mark():
    mark = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)
    game = Game(phase=GamePhase.PLAYING, active_seconds=100, clock_at=mark)

    assert game.active_time(mark + timedelta(seconds=45)) == 145
    assert game.active_time(mark - timedelta(seconds=45)) == 100  # назад время не идёт
    assert game.paused_remaining() is None


def test_the_time_of_a_paused_or_unstarted_game_stands_still():
    mark = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)
    later = mark + timedelta(hours=1)

    assert Game(phase=GamePhase.PAUSED, active_seconds=100, clock_at=mark).active_time(later) == 100
    assert Game(phase=GamePhase.LOBBY, active_seconds=0, clock_at=None).active_time(later) == 0
    assert Game(phase=GamePhase.PLAYING, active_seconds=7, clock_at=None).active_time(later) == 7


def test_the_remainder_of_a_paused_countdown_is_the_deadline_minus_the_pause_moment():
    paused_at = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)
    game = Game(
        phase=GamePhase.PAUSED, deadline_at=paused_at + timedelta(seconds=70), clock_at=paused_at
    )

    assert game.paused_remaining() == timedelta(seconds=70)
    game.deadline_at = paused_at - timedelta(seconds=5)  # срок успел пройти
    assert game.paused_remaining() == timedelta(0)
    game.deadline_at = None
    assert game.paused_remaining() is None
