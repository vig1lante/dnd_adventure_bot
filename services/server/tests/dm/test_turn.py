"""Атомарный ход мастера (план B7.3, спека §8.4 и §12).

Настоящий движок, демо-приключение `demo-crypt` (копия лежит в игре, как после создания игры),
временная SQLite после миграций и `FakeProvider` по сценариям. Заглушки провайдеров нужны только
там, где надо управлять временем и сбоями (тайм-аут, исключения, остановка до конца ожидания).
Живых моделей нет; кубы — `SeededRng` или `ScriptedRng`, их подставляют через конструктор `Deps`.

Что держат тесты:
- ход идёт в три приёма: резерв под блокировкой, модель без блокировки и без соединения с базой,
  коммит под блокировкой с проверкой версии;
- коммит один: снимок, сообщения, ходы, фаза, журнал и статусы ходов либо есть, либо нет;
- любая ошибка — неудачная попытка: копия выброшена, фаза `dm_failed`, игра не зависает;
- тайное видит только адресат, секреты мастера и ключи не попадают игрокам и в журнал.
"""

import ast
import asyncio
import copy
import dataclasses
import json
import logging
import re
import shlex
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml
from adventure_secrets import hidden_texts
from lore_trap import trap_lore
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy import text as raw_sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import connect

from dnd_engine.adventure.loader import adventure_from_data, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.journal import open_journal
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState, GameTime, RulesRef, Scene, SceneType, Spotlight
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.characters import service as characters_service
from dnd_server.characters.previews import RandomPreviews
from dnd_server.characters.service import new_character_id
from dnd_server.config import HOST, PORT, Settings
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
from dnd_server.dm import turn as turn_module
from dnd_server.dm.provider import GameProviders, bridge_url, game_provider
from dnd_server.dm.spotlight import SPOTLIGHT_HINT_AFTER, spotlight_hints, update_spotlight
from dnd_server.dm.turn import (
    BROKEN_COPY,
    Deps,
    TurnOutcome,
    failed_turn_refusal,
    failure_text,
    regenerate_last_turn,
    run_dm_turn,
)
from dnd_server.games import service as games_service
from dnd_server.games.service import Conflict, GameLocks, NotFound
from dnd_server.llm.anthropic_api import AnthropicProvider
from dnd_server.llm.base import (
    ProviderError,
    ProviderRefusal,
    ToolLoopExceeded,
    TurnRequest,
    TurnResult,
    Usage,
)
from dnd_server.llm.factory import ConfigError
from dnd_server.llm.fake import FakeProvider
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider
from dnd_server.realtime.views import snapshot_for
from dnd_server.turns.flow import scene_follow_up

# tests/dm/test_turn.py -> dm -> tests -> server -> services -> корень репозитория
REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_ADVENTURE_DIR = REPO_ROOT / "content" / "adventures" / "demo-crypt"
SERVER_SRC = REPO_ROOT / "services" / "server" / "src" / "dnd_server"
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
FAKE_CLAUDE = Path(__file__).resolve().parents[1] / "llm" / "fake_claude.py"
SESSION_SECRET = "turn-session-secret-0123456789-abcdefghij"
API_KEY = "sk-ant-api-SECRET-0123456789"
BOT_TOKEN = "123456789:AAF-turn-bot-token-0123456789_abcdef"
OPENAI_KEY = "sk-openai-SECRET-0123456789"
SECRETS = (SESSION_SECRET, API_KEY, BOT_TOKEN, OPENAI_KEY)
PLAYERS = (  # (ключ, имя, класс, раса)
    ("alice", "Алиса", "fighter", "human"),
    ("boris", "Борис", "rogue", "elf"),
    ("vera", "Вера", "cleric", "dwarf"),
)
TIMEOUT_S = 5


# --- помощники сценариев FakeProvider ---


def tool(tool_name: str, /, **args: Any) -> dict[str, Any]:
    return {"tool": tool_name, "args": args}


def say(text: str) -> dict[str, Any]:
    return {"text": text}


def scenario_turn(*steps: dict[str, Any], repeat: bool = False) -> dict[str, Any]:
    return {"steps": list(steps), "repeat": repeat} if repeat else {"steps": list(steps)}


def check(character: str = "$pc1", **overrides: Any) -> dict[str, Any]:
    args = {"character_id": character, "ability": "dex", "skill": "stealth", "dc": 15}
    return tool("request_check", **{**args, "reason": "крадётся", **overrides})


# --- мир теста: игра, игроки, зависимости ---


@dataclass(frozen=True)
class Player:
    key: str
    user_id: str
    character_id: str
    name: str


class RecordingBroadcaster:
    """Хаб вместо сокетов: помнит рассылку и то, что в базе в момент каждой отправки."""

    def __init__(self, db: async_sessionmaker[AsyncSession], game_id: str) -> None:
        self._db = db
        self._game_id = game_id
        self.sent: list[tuple[dict[str, Any], str]] = []
        self.versions_at_send: list[int] = []

    async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
        assert game_id == self._game_id
        async with self._db() as session:
            self.versions_at_send.append((await session.get(Game, game_id)).state_version)
        self.sent.append((payload, audience))

    def of_type(self, event_type: str) -> list[tuple[dict[str, Any], str]]:
        return [(payload, who) for payload, who in self.sent if payload["type"] == event_type]


@dataclass
class World:
    db: async_sessionmaker[AsyncSession]
    deps: Deps
    broadcaster: RecordingBroadcaster
    game_id: str
    turn_id: str
    players: dict[str, Player]
    adventure: Adventure
    tmp_path: Path

    @property
    def alice(self) -> Player:
        return self.players["alice"]

    @property
    def boris(self) -> Player:
        return self.players["boris"]

    @property
    def vera(self) -> Player:
        return self.players["vera"]

    @property
    def pool(self) -> Any:
        return self.db.kw["bind"].pool

    def use_scenario(self, *turns: dict[str, Any]) -> Path:
        """Игра ведётся по сценарию FakeProvider из этих ходов (очередь сначала)."""
        path = self.tmp_path / "scenario.yaml"
        path.write_text(yaml.safe_dump({"turns": list(turns)}, allow_unicode=True), "utf-8")
        self.use_scenario_file(path)
        return path

    def use_scenario_file(self, path: Path) -> None:
        settings = self.deps.settings.model_copy(update={"fake_llm_scenario": path})
        self.deps = dataclasses.replace(self.deps, settings=settings, providers=GameProviders())

    def with_rng(self, rng: Any) -> None:
        self.deps = dataclasses.replace(self.deps, rng=rng)

    async def run(self, mode: TurnKind = TurnKind.ROUND, **kwargs: Any) -> TurnOutcome:
        return await run_dm_turn(self.game_id, mode=mode, deps=self.deps, **kwargs)

    async def regenerate(self) -> TurnOutcome:
        return await regenerate_last_turn(self.game_id, deps=self.deps)

    async def act(
        self,
        key: str,
        text: str = "",
        *,
        secret: bool = False,
        inspiration: bool = False,
        status: ActionStatus | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Ход игрока в открытом ходу (по умолчанию сданный; пустой текст — пас)."""
        if status is None:
            status = ActionStatus.SUBMITTED if text else ActionStatus.PASS
        async with self.db() as session:
            session.add(
                PlayerAction(
                    turn_id=turn_id or await self.current_turn_id(),
                    user_id=self.players[key].user_id,
                    text=text,
                    secret=secret,
                    inspiration=inspiration,
                    status=status,
                )
            )
            await session.commit()

    async def current_turn_id(self) -> str:
        return (await self.game()).current_turn_id

    async def game(self) -> Game:
        async with self.db() as session:
            return await session.get(Game, self.game_id)

    async def state(self, version: int | None = None) -> GameState:
        async with self.db() as session:
            if version is None:
                version = (await session.get(Game, self.game_id)).state_version
            return (await session.get(GameStateSnapshot, (self.game_id, version))).to_state()

    async def turns(self) -> list[Turn]:
        async with self.db() as session:
            rows = await session.scalars(
                select(Turn)
                .where(Turn.game_id == self.game_id)
                .order_by(Turn.number, Turn.created_at)
            )
            return list(rows)

    async def messages(self, *, with_superseded: bool = True) -> list[Message]:
        async with self.db() as session:
            query = select(Message).where(Message.game_id == self.game_id).order_by(Message.id)
            if not with_superseded:
                query = query.where(Message.superseded.is_(False))
            return list(await session.scalars(query))

    async def logs(self) -> list[DmTurnLog]:
        async with self.db() as session:
            rows = await session.scalars(
                select(DmTurnLog).where(DmTurnLog.game_id == self.game_id).order_by(DmTurnLog.id)
            )
            return list(rows)

    async def snapshot_versions(self) -> list[int]:
        async with self.db() as session:
            rows = await session.scalars(
                select(GameStateSnapshot.version)
                .where(GameStateSnapshot.game_id == self.game_id)
                .order_by(GameStateSnapshot.version)
            )
            return list(rows)

    async def actions(self, turn_id: str) -> dict[str, PlayerAction]:
        async with self.db() as session:
            rows = await session.scalars(
                select(PlayerAction).where(PlayerAction.turn_id == turn_id)
            )
            return {row.user_id: row for row in rows}

    async def feed_of(self, key: str) -> list[dict[str, Any]]:
        async with self.db() as session:
            user = await session.get(User, self.players[key].user_id)
            return (await snapshot_for(session, user, self.game_id, srd=self.deps.srd))["feed"]

    async def sheet_of(self, key: str) -> dict[str, Any]:
        """Лист героя игрока в снимке, который тот получает при подключении."""
        async with self.db() as session:
            user = await session.get(User, self.players[key].user_id)
            return (await snapshot_for(session, user, self.game_id, srd=self.deps.srd))["character"]


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


def new_state(game_id: str, adventure: Adventure, party: dict[str, Character]) -> GameState:
    state = GameState(
        game_id=game_id,
        adventure_id=adventure.id,
        rules=RulesRef(modules=list(adventure.rules.modules)),
        party=party,
        location_id=adventure.start_location,
        turn=1,
        version=1,
    )
    state.world.visited_locations = [adventure.start_location]
    return state


async def build_world(
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    srd: SrdContent,
    adventure: Adventure,
    tmp_path: Path,
    *,
    opening: bool = False,
) -> World:
    """Игра в обсуждении (версия состояния 1, открытый ход 1) или перед открытием игры."""
    game_id, turn_id = new_id(), new_id()
    players: dict[str, Player] = {}
    heroes: dict[str, Character] = {}
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
        hero.player_id = user_id
        heroes[hero.id] = hero
        players[key] = Player(key, user_id, hero.id, name)
    state = new_state(game_id, adventure, heroes)
    async with db() as session:
        session.add_all(
            User(id=player.user_id, dev_login=f"dev-{number}", name=player.name)
            for number, player in enumerate(players.values(), start=1)
        )
        await session.flush()
        session.add(
            Game(
                id=game_id,
                code="ABC123",
                adventure_id=adventure.id,
                host_user_id=players["alice"].user_id,
                adventure_data=adventure.model_dump(mode="json"),
                phase=GamePhase.WEAVING if opening else GamePhase.PLAYING,
                turn_phase=None if opening else TurnPhase.DISCUSSION,
                state_version=1,
                current_turn_id=None if opening else turn_id,
                collect_seconds=120,
                hurry_seconds=60,
                camp_seconds=300,
            )
        )
        await session.flush()
        session.add_all(
            PlayerCharacter(
                id=player.character_id,
                game_id=game_id,
                user_id=player.user_id,
                data=heroes[player.character_id].model_dump(mode="json"),
            )
            for player in players.values()
        )
        await session.flush()
        session.add_all(
            GamePlayer(
                game_id=game_id,
                user_id=player.user_id,
                character_id=player.character_id,
                is_host=player.key == "alice",
                ready=True,
                joined_order=number,
            )
            for number, player in enumerate(players.values(), start=1)
        )
        session.add(GameStateSnapshot.from_state(state))
        if not opening:
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
    broadcaster = RecordingBroadcaster(db, game_id)
    deps = Deps(
        sessionmaker=db,
        locks=GameLocks(),
        broadcaster=broadcaster,
        settings=settings.model_copy(
            update={
                "anthropic_api_key": SecretStr(API_KEY),
                "openai_compat_api_key": SecretStr(OPENAI_KEY),
                "telegram_bot_token": SecretStr(BOT_TOKEN),
                "session_secret": SecretStr(SESSION_SECRET),
                "content_dir": tmp_path / "нет-такой-папки",  # приключение — только из копии в базе
            }
        ),
        turn_tokens=None,
        srd=srd,
        rng=SeededRng(7),
        providers=GameProviders(),
    )
    return World(db, deps, broadcaster, game_id, turn_id, players, adventure, tmp_path)


@pytest.fixture
async def world(db, settings, srd, adventure, tmp_path) -> World:
    return await build_world(db, settings, srd, adventure, tmp_path)


@pytest.fixture
async def opening_world(db, settings, srd, adventure, tmp_path) -> World:
    return await build_world(db, settings, srd, adventure, tmp_path, opening=True)


# --- заглушки провайдеров ---


def narration(text: str = "Рассказ мастера.", *, stop_reason: str = "end_turn") -> TurnResult:
    return TurnResult(text=text, trace=(), usage=Usage(11, 7, 5, 3), stop_reason=stop_reason)


class StubProvider:
    """Провайдер, которым управляет тест: готовые ответы или исключения, остановка на события."""

    name = "stub"

    def __init__(
        self,
        *outcomes: TurnResult | Exception,
        gate: asyncio.Event | None = None,
        calls: tuple[tuple[str, dict[str, Any]], ...] = (),
        cli_version: str | None = None,
    ) -> None:
        self._outcomes = list(outcomes) or [narration()]
        self._gate = gate
        self._calls = calls
        self.requests: list[TurnRequest] = []
        self.started = asyncio.Event()
        self.cancelled = False
        self._cli_version = cli_version
        self.cli_version: str | None = None  # адаптер узнаёт версию CLI только при первом запуске

    async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
        self.requests.append(req)
        self.started.set()
        self.cli_version = self._cli_version
        try:
            if self._gate is not None:
                await self._gate.wait()
            for name, args in self._calls:
                await tools(name, args)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def complete_json(self, req: Any, schema: Any) -> Any:
        raise AssertionError("ход мастера не просит JSON")


class Factory:
    """Подмена `make_provider`: отдаёт заглушку и запоминает аргументы каждого вызова."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    def __call__(self, settings: Settings, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.provider


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> Factory:
    factory = Factory(provider)
    monkeypatch.setattr(provider_module, "make_provider", factory)
    return factory


def error_of(log: DmTurnLog) -> str:
    assert log.error is not None
    return log.error


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def use_fake_claude(world: World, env: dict[str, str] | None = None, **settings: Any) -> None:
    """Игра ведётся настоящим адаптером `claude_code_cli`, но вместо `claude` — имитатор из тестов.

    `env` — переменные имитатора (`FAKE_CLAUDE_*`), `settings` — правки настроек сервера (модель
    подписки и т. п.). Без `FAKE_CLAUDE_REPLIES` имитатор отвечает рассказом и называет в
    `modelUsage` модель из `--model` (или `FAKE_CLAUDE_MODEL`)."""
    lines = [f"python={shlex.quote(sys.executable)}", f"script={shlex.quote(str(FAKE_CLAUDE))}"]
    lines += [f"export {name}={shlex.quote(value)}" for name, value in (env or {}).items()]
    (world.tmp_path / "fake_claude.env").write_text("\n".join(lines) + "\n")
    wrapper = world.tmp_path / "claude"
    wrapper.symlink_to(FAKE_CLAUDE.with_name("fake_claude.sh"))
    changes = {"llm_provider": "claude_code_cli", "claude_cli_path": str(wrapper), **settings}
    world.deps = dataclasses.replace(
        world.deps,
        settings=world.deps.settings.model_copy(update=changes),
        turn_tokens=TurnTokens(),
        providers=GameProviders(),
    )


# --- провайдер игры и адрес моста (F3) ---


def test_the_address_of_the_bridge_is_built_from_host_and_port_of_the_config():
    from dnd_server import __main__ as entrypoint

    assert (entrypoint.HOST, entrypoint.PORT) == (HOST, PORT)
    assert bridge_url() == f"http://{HOST}:{PORT}"


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            modules |= {f"{node.module}.{alias.name}" for alias in node.names}
    return modules


def test_no_service_imports_the_module_that_starts_the_server():
    importers = [
        str(path.relative_to(SERVER_SRC))
        for path in sorted(SERVER_SRC.rglob("*.py"))
        if path.name != "__main__.py" and "dnd_server.__main__" in imported_modules(path)
    ]

    assert importers == []


def test_the_provider_of_a_game_is_assembled_in_one_function(monkeypatch, settings):
    factory = use_provider(monkeypatch, object())
    tokens = object()

    game_provider(settings, provider="fake", model="m", pc_ids=("pc_1",), turn_tokens=tokens)

    assert factory.calls == [
        {
            "provider": "fake",
            "model": "m",
            "pc_ids": ("pc_1",),
            "turn_tokens": tokens,
            "internal_url": bridge_url(),
        }
    ]


def test_the_lobby_check_asks_the_same_function_for_the_provider(monkeypatch, settings):
    factory = use_provider(monkeypatch, object())
    tokens = object()

    games_service.check_provider(settings, "claude_code_cli", None, tokens)

    assert factory.calls == [
        {
            "provider": "claude_code_cli",
            "model": None,
            "pc_ids": (),
            "turn_tokens": tokens,
            "internal_url": bridge_url(),
        }
    ]


def test_the_lobby_check_refuses_with_the_config_error_of_the_function(monkeypatch, settings):
    def broken(_settings: Settings, **_kwargs: Any) -> Any:
        raise ConfigError("не задан ANTHROPIC_API_KEY")

    monkeypatch.setattr(provider_module, "make_provider", broken)

    with pytest.raises(games_service.Invalid, match="ANTHROPIC_API_KEY"):
        games_service.check_provider(settings, "anthropic_api", None, None)


def test_character_help_asks_the_same_function_for_the_provider(monkeypatch, settings, srd):
    factory = use_provider(monkeypatch, object())
    tokens = object()
    deps = characters_service.Deps(
        locks=GameLocks(),
        model_calls=games_service.ModelCalls(),
        broadcaster=None,  # type: ignore[arg-type]
        settings=settings,
        turn_tokens=tokens,  # type: ignore[arg-type]
        srd=srd,
        rng=SeededRng(1),
        previews=RandomPreviews(),
    )
    game = type("GameOutStub", (), {"provider": "fake", "model": None})()

    characters_service._provider_for(deps, game)

    assert factory.calls == [
        {
            "provider": "fake",
            "model": None,
            "pc_ids": (),
            "turn_tokens": tokens,
            "internal_url": bridge_url(),
        }
    ]


async def test_the_turn_asks_the_same_function_with_the_characters_of_the_game(monkeypatch, world):
    factory = use_provider(monkeypatch, StubProvider())
    world.deps = dataclasses.replace(world.deps, turn_tokens=object())  # type: ignore[arg-type]

    await world.run()

    (call,) = factory.calls
    assert call["pc_ids"] == tuple(player.character_id for player in world.players.values())
    assert call["turn_tokens"] is world.deps.turn_tokens
    assert call["internal_url"] == bridge_url()


async def test_one_provider_serves_all_turns_of_a_game_so_the_scenario_queue_goes_on(
    monkeypatch, world
):
    factory = use_provider(monkeypatch, StubProvider())

    await world.run()
    await world.run()

    assert len(factory.calls) == 1


async def test_a_config_error_at_the_start_of_a_turn_is_dm_failed_without_secrets(world):
    # Провайдер openai_compatible без адреса: ошибка настройки называет переменную, но не значения.
    settings = world.deps.settings.model_copy(update={"llm_provider": "openai_compatible"})
    world.deps = dataclasses.replace(world.deps, settings=settings)

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    (log,) = await world.logs()
    assert "OPENAI_COMPAT_BASE_URL" in error_of(log)
    game = await world.game()
    assert game.turn_phase == TurnPhase.DM_FAILED
    everything = dump([log.error, log.request, log.trace, outcome.error, world.broadcaster.sent])
    assert not any(secret in everything for secret in SECRETS)


async def test_a_config_error_is_not_remembered_so_the_host_can_repeat_after_fixing_it(world):
    broken = world.deps.settings.model_copy(update={"llm_provider": "openai_compatible"})
    world.deps = dataclasses.replace(world.deps, settings=broken)
    failed = await world.run()
    fixed = world.deps.settings.model_copy(update={"llm_provider": "fake"})
    world.deps = dataclasses.replace(world.deps, settings=fixed)
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Итог.")))

    retry = await world.run()

    assert (failed.status, retry.status) == (TurnStatus.FAILED, TurnStatus.DONE)
    assert [log.attempt for log in await world.logs()] == [1, 2]


# --- успешный ход ---


BASIC_ROUND = SCENARIOS_DIR / "basic_round.yaml"


async def test_a_successful_turn_commits_state_messages_turns_and_game_in_one_step(world):
    world.use_scenario(
        scenario_turn(
            check("$pc1"),
            tool("private_message", character_id="$pc2", text="Вы чуете холод."),
            tool("move_party", location_id="cemetery"),
            tool("chronicle", entry="Отряд вышел на кладбище."),
            say("Туман расступается."),
        )
    )
    await world.act("alice", "Крадусь вперёд")
    await world.act("boris", "")

    outcome = await world.run()

    assert (outcome.status, outcome.state_version, outcome.attempts) == (TurnStatus.DONE, 2, 1)
    game = await world.game()
    assert (game.phase, game.turn_phase, game.state_version) == (
        GamePhase.PLAYING,
        TurnPhase.DISCUSSION,
        2,
    )
    assert game.deadline_at is None
    state = await world.state()
    assert state.version == 2
    assert state.location_id == "cemetery"
    assert state.chronicle.entries == ["Отряд вышел на кладбище."]
    assert (await world.snapshot_versions()) == [1, 2]  # прежняя версия цела
    done, following = await world.turns()
    assert (done.id, done.status, done.kind) == (world.turn_id, TurnStatus.DONE, TurnKind.ROUND)
    assert (done.state_version_before, done.state_version_after) == (1, 2)
    assert (following.number, following.kind, following.status) == (
        2,
        TurnKind.ROUND,
        TurnStatus.OPEN,
    )
    assert following.state_version_before == 2
    assert game.current_turn_id == following.id


async def test_messages_follow_the_events_and_the_narration_comes_last(world):
    world.use_scenario(
        scenario_turn(
            check("$pc1"),
            tool("private_message", character_id="$pc2", text="Вы чуете холод."),
            tool("move_party", location_id="cemetery"),
            tool("chronicle", entry="Отряд вышел на кладбище."),
            say("Туман расступается."),
        )
    )

    await world.run()

    messages = await world.messages()
    assert [(m.kind, m.audience) for m in messages] == [
        (MessageKind.ROLL, "all"),
        (MessageKind.PRIVATE, world.boris.user_id),
        (MessageKind.SYSTEM, "all"),
        (MessageKind.NARRATION, "all"),
    ]
    assert [m.text for m in messages][1] == "Вы чуете холод."
    assert messages[-1].text == "Туман расступается."
    assert all(m.turn_id == world.turn_id and m.superseded is False for m in messages)


async def test_the_shipped_basic_round_scenario_runs_a_whole_round(world):
    world.use_scenario_file(BASIC_ROUND)
    await world.act("alice", "Подкрадываюсь к склепу")

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (roll, story) = await world.messages()
    assert (roll.kind, story.kind) == (MessageKind.ROLL, MessageKind.NARRATION)
    assert story.text == "Тени скрывают вас…"


async def test_events_are_broadcast_after_the_commit_by_the_view_builders(world):
    world.use_scenario(
        scenario_turn(check("$pc1"), tool("chronicle", entry="Запись."), say("Итог."))
    )

    await world.run()

    sent = world.broadcaster
    assert sent.versions_at_send  # рассылка была
    assert set(sent.versions_at_send[1:]) == {2}  # всё, кроме «мастер думает», — после коммита
    assert sent.sent[0][0]["type"] == "phase"
    first_phase = sent.sent[0][0]["phase"]
    assert first_phase["turn_phase"] == "resolving"
    last_phase = sent.of_type("phase")[-1][0]["phase"]
    assert (last_phase["phase"], last_phase["turn_phase"], last_phase["deadline_at"]) == (
        "playing",
        "discussion",
        None,
    )
    feed = sent.of_type("feed_item")
    assert [payload["item"]["kind"] for payload, _ in feed] == ["roll", "narration"]
    assert {who for _, who in feed} == {"all"}
    assert len(sent.of_type("party")) == len(sent.of_type("combat")) == 1
    sheets = sent.of_type("character")
    assert {who for _, who in sheets} == {p.user_id for p in world.players.values()}
    statuses = sent.of_type("player_status")
    everybody = {p.user_id for p in world.players.values()}
    assert {payload["user_id"] for payload, _ in statuses} == everybody
    assert {payload["status"] for payload, _ in statuses} == {"idle"}  # новый ход: никто не пишет


async def test_a_roll_of_a_secret_turn_goes_only_to_its_author(world):
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Тайно крадусь", secret=True)

    await world.run()

    roll = next(m for m in await world.messages() if m.kind == MessageKind.ROLL)
    assert roll.audience == world.alice.user_id
    (roll_event, who) = world.broadcaster.of_type("feed_item")[0]
    assert (roll_event["item"]["kind"], who) == ("roll", world.alice.user_id)
    assert any(item["kind"] == "roll" for item in await world.feed_of("alice"))
    for key in ("boris", "vera"):
        assert all(item["kind"] != "roll" for item in await world.feed_of(key))


async def test_a_public_roll_goes_to_everybody_with_a_card_of_the_dice(world):
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    world.with_rng(ScriptedRng([13]))

    await world.run()

    roll = next(m for m in await world.messages() if m.kind == MessageKind.ROLL)
    assert roll.audience == "all"
    assert roll.data["total"] == 13 + roll.data["modifier"]
    assert roll.data["natural"] == 13
    assert roll.data["actor"] == "Алиса"
    assert roll.data["dc"] == 15
    assert "target_ac" not in roll.data
    assert "Алиса" in roll.text


async def test_a_private_message_goes_only_to_its_addressee_and_data_holds_what_he_sees(world):
    world.use_scenario(
        scenario_turn(
            tool("private_message", character_id="$pc2", text="ЛИЧНОЕ-boris: в кармане ключ."),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )

    await world.run()

    private = next(m for m in await world.messages() if m.kind == MessageKind.PRIVATE)
    assert private.audience == world.boris.user_id
    assert private.text == "ЛИЧНОЕ-boris: в кармане ключ."
    assert private.data is None
    for key, sees in (("alice", False), ("boris", True), ("vera", False)):
        feed = await world.feed_of(key)
        assert any("ЛИЧНОЕ-boris" in item["text"] for item in feed) is sees
    assert ("private", world.boris.user_id) in [
        (p["item"]["kind"], who) for p, who in world.broadcaster.of_type("feed_item")
    ]


async def test_a_found_item_of_a_secret_turn_goes_only_to_its_author(world):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )
    await world.act("boris", "Тайно обыскиваю Марту", secret=True)

    await world.run()

    line = next(m for m in await world.messages() if m.kind == MessageKind.SYSTEM)
    assert (line.audience, "Запасной ключ Ульриха" in line.text) == (world.boris.user_id, True)
    assert all("Запасной ключ" not in i["text"] for i in await world.feed_of("alice"))
    assert any("Запасной ключ" in i["text"] for i in await world.feed_of("boris"))


async def test_a_found_item_of_a_public_turn_is_in_the_common_feed(world):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )
    await world.act("boris", "Прошу ключ у Марты")

    await world.run()

    line = next(m for m in await world.messages() if m.kind == MessageKind.SYSTEM)
    assert line.audience == "all"
    assert "Борис" in line.text


# --- предметы приключения в листе героя: ход мастера, событие, снимок (проверка B8, fix 5) ---

KEY_LINE = {"item_id": "chest-key", "qty": 1, "equipped": False, "name": "Запасной ключ Ульриха"}


def live_sheets(world: World) -> dict[str, dict[str, Any]]:
    """Последний лист, который получил каждый игрок событием `character` (id игрока -> лист)."""
    return {who: payload["character"] for payload, who in world.broadcaster.of_type("character")}


async def assert_snapshot_sheets_equal_live_ones(world: World) -> None:
    sheets = live_sheets(world)
    for key, player in world.players.items():
        assert await world.sheet_of(key) == sheets[player.user_id], key


async def test_an_item_the_master_gives_reaches_the_sheet_of_its_hero_in_the_event_and_the_snapshot(
    world,
):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )
    await world.act("boris", "Прошу ключ у Марты")

    await world.run()

    sheets = live_sheets(world)
    assert sheets[world.boris.user_id]["inventory"][-1] == KEY_LINE
    for key in ("alice", "vera"):
        assert "chest-key" not in dump(sheets[world.players[key].user_id]), key
    assert [line for line in (await world.sheet_of("boris"))["inventory"] if "name" in line] == [
        KEY_LINE
    ]
    await assert_snapshot_sheets_equal_live_ones(world)


async def test_an_item_the_master_passes_on_in_the_next_turn_moves_between_the_sheets(world):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Один."),
        ),
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc1"),
            tool("chronicle", entry="з"),
            say("Два."),
        ),
    )
    await world.run()
    await world.act("alice", "Прошу ключ у Бориса")
    world.broadcaster.sent.clear()

    await world.run()

    sheets = live_sheets(world)
    assert sheets[world.alice.user_id]["inventory"][-1] == KEY_LINE
    assert "chest-key" not in dump(sheets[world.boris.user_id])
    await assert_snapshot_sheets_equal_live_ones(world)


async def test_an_item_the_master_takes_back_leaves_the_sheet_in_the_event_and_the_snapshot(world):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Один."),
        ),
        scenario_turn(
            tool("take_item", item_id="chest-key", character_id="$pc2", to="gone"),
            tool("chronicle", entry="з"),
            say("Два."),
        ),
    )
    await world.run()
    assert KEY_LINE in (await world.sheet_of("boris"))["inventory"]
    await world.act("boris", "Отдаю ключ Марте")
    world.broadcaster.sent.clear()

    await world.run()

    assert all("chest-key" not in dump(sheet) for sheet in live_sheets(world).values())
    await assert_snapshot_sheets_equal_live_ones(world)


async def test_regenerate_takes_the_item_back_from_the_sheets_in_the_rolled_back_state(world):
    world.use_scenario(
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc2"),
            tool("chronicle", entry="з"),
            say("Старый рассказ."),
        ),
        scenario_turn(
            tool("give_item", item_id="chest-key", character_id="$pc3"),
            tool("chronicle", entry="з"),
            say("Новый рассказ."),
        ),
    )
    await world.run()
    world.broadcaster.sent.clear()

    await world.regenerate()

    sheets = world.broadcaster.of_type("character")
    rolled_back, renewed = sheets[:3], sheets[3:]
    assert len(renewed) == 3  # откат и итог новой попытки — по листу на каждого игрока
    assert all("chest-key" not in dump(payload) for payload, _ in rolled_back)
    assert [who for payload, who in renewed if "chest-key" in dump(payload)] == [world.vera.user_id]
    await assert_snapshot_sheets_equal_live_ones(world)


async def test_findings_flags_and_damage_leave_no_lines_in_the_feed(world):
    # У `reveal` и `set_flag` нет автора, а `hp_after` — точные HP: строкой в ленту они не идут,
    # игроки узнают об этом из рассказа и блоков отряда и боя.
    world.use_scenario(
        scenario_turn(
            tool("reveal", ref="feature:board"),
            tool("set_flag", name="met_marta", value=True),
            tool("damage", character_id="$pc2", dice="1d4", damage_type="fire", reason="искра"),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )
    await world.act("boris", "Тайно ковыряюсь", secret=True)

    await world.run()

    kinds = [m.kind for m in await world.messages()]
    assert MessageKind.SYSTEM not in kinds
    damage_roll = [m for m in await world.messages() if m.kind == MessageKind.ROLL]
    assert [m.audience for m in damage_roll] == [world.boris.user_id]  # бросок тайного автора


async def test_the_journal_has_the_trace_the_output_the_usage_and_the_duration(monkeypatch, world):
    provider = StubProvider(
        narration("Итог."),
        calls=(("chronicle", {"entry": "Запись."}), ("ghost", {})),
        cli_version="9.9.9 (Claude Code)",
    )
    use_provider(monkeypatch, provider)
    await world.act("alice", "Иду вперёд")

    await world.run()

    (log,) = await world.logs()
    assert (log.game_id, log.turn_id, log.attempt, log.provider, log.error) == (
        world.game_id,
        world.turn_id,
        1,
        "stub",
        None,
    )
    assert log.output == "Итог."
    assert [(c["name"], c["is_error"]) for c in log.trace] == [
        ("chronicle", False),
        ("ghost", True),
    ]
    assert log.usage["input_tokens"] == 11
    assert log.usage["stop_reason"] == "end_turn"
    assert log.duration_ms is not None and log.duration_ms >= 0
    assert log.request["mode"] == "round"
    assert log.request["cli_version"] == "9.9.9 (Claude Code)"
    assert "Контекст хода мастера" in log.request["context"]


async def test_a_failed_attempt_is_logged_with_its_ids_but_without_secrets_or_player_texts(
    monkeypatch, world, caplog
):
    caplog.set_level(logging.WARNING, logger="dnd_server.dm.turn")
    use_provider(monkeypatch, StubProvider(RuntimeError(f"сбой с ключом {API_KEY}")))
    await world.act("alice", "ТАЙНА-текст хода игрока", secret=True)

    await world.run()

    assert world.game_id in caplog.text and world.turn_id in caplog.text
    assert "RuntimeError" in caplog.text
    assert API_KEY not in caplog.text
    assert "ТАЙНА-текст хода игрока" not in caplog.text


@pytest.mark.parametrize(
    ("provider_name", "override", "expected"),
    [
        ("anthropic_api", None, "claude-test-default"),
        ("anthropic_api", "claude-test-override", "claude-test-override"),
        ("openai_compatible", None, "local-default"),
        ("openai_compatible", "local-override", "local-override"),
        ("fake", None, None),  # у провайдера без своей модели в журнале пусто
    ],
)
async def test_the_journal_names_the_model_of_the_provider_that_answered(
    monkeypatch, world, provider_name, override, expected
):
    world.use_scenario(scenario_turn(say("Итог.")))  # сценарий нужен провайдеру `fake`
    settings = world.deps.settings.model_copy(
        update={
            "anthropic_api_key": SecretStr(API_KEY),
            "llm_model": "claude-test-default",
            "openai_compat_base_url": "http://models.example.test/v1",
            "openai_compat_model": "local-default",
        }
    )
    world.deps = dataclasses.replace(world.deps, settings=settings, providers=GameProviders())
    async with world.db() as session:
        await session.execute(
            update(Game)
            .where(Game.id == world.game_id)
            .values(provider=provider_name, model=override)
        )
        await session.commit()

    async def answer(_self: Any, _request: Any, _tools: Any) -> TurnResult:
        return narration()

    monkeypatch.setattr(AnthropicProvider, "run_turn", answer)
    monkeypatch.setattr(OpenAICompatibleProvider, "run_turn", answer)
    monkeypatch.setattr(FakeProvider, "run_turn", answer)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    assert log.model == expected


async def test_a_turn_that_could_not_build_its_provider_has_no_model_in_the_journal(world):
    # Мастер не отвечал: модель в журнале не называется, как бы ни была настроена игра.
    settings = world.deps.settings.model_copy(update={"anthropic_api_key": None})
    world.deps = dataclasses.replace(world.deps, settings=settings, providers=GameProviders())
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(provider="anthropic_api")
        )  # ключа в настройках нет: провайдер не собирается
        await session.commit()

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    (log,) = await world.logs()
    assert log.model is None


async def test_the_cli_version_of_the_subscription_provider_reaches_the_journal(world):
    # Настоящий адаптер `claude_code_cli`, но вместо `claude` — имитатор из тестов: версию CLI он
    # узнаёт при первом запуске, то есть уже после начала хода.
    use_fake_claude(world)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    assert log.provider == "claude_code_cli"
    assert log.request["cli_version"] == "9.9.9 (Claude Code)"
    assert (log.usage["input_tokens"], log.usage["output_tokens"]) == (11, 7)
    assert log.output == "Тени скрывают вас…"
    assert log.model == "claude-fake-subscription"  # модель «подписки» имитатора из modelUsage


async def test_the_journal_names_the_model_the_subscription_cli_actually_used(world):
    # Спека §11: журнал пишет фактическую модель из `modelUsage` ответа CLI, а не псевдоним из
    # настроек: имитатор «разворачивает» `opus` в полное имя, как это делает настоящий CLI.
    use_fake_claude(world, {"FAKE_CLAUDE_MODEL": "claude-opus-test-9"}, claude_cli_model="opus")

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    assert log.model == "claude-opus-test-9"


async def test_the_model_of_the_game_reaches_the_cli_and_beats_the_environment(world):
    # Модель, выбранная создателем игры, идёт из строки игры через провайдера игры в `--model`
    # и сильнее CLAUDE_CLI_MODEL; дешёвая модель ход не ведёт. Имитатор называет в `modelUsage`
    # ровно то, что получил в `--model`.
    use_fake_claude(world, claude_cli_model="opus", claude_cli_cheap_model="haiku")
    async with world.db() as session:
        await session.execute(update(Game).where(Game.id == world.game_id).values(model="sonnet"))
        await session.commit()

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    assert (log.provider, log.model) == ("claude_code_cli", "sonnet")


@pytest.mark.parametrize(
    ("provider_name", "game_model", "cli_model", "expected"),
    [
        # Игру создали без провайдера при LLM_PROVIDER=openai_compatible,
        # потом владелец включил CLI.
        pytest.param(None, "qwen2.5:14b", "opus", "opus", id="модель другого провайдера"),
        pytest.param(None, "claude-opus-4@x", None, "claude-fake-subscription", id="без env"),
        # Игра DND-1 с provider=claude_code_cli и моделью, присланной прямо в API.
        pytest.param("claude_code_cli", "x:y", "sonnet", "sonnet", id="игра CLI из DND-1"),
    ],
)
async def test_a_saved_game_with_a_model_the_cli_refuses_still_plays_on_the_model_of_the_env(
    world, provider_name, game_model, cli_model, expected
):
    # Спека §11: модель игры → CLAUDE_CLI_MODEL → модель CLI по умолчанию. Модель игры, которую CLI
    # не примет, пропускается: сохранённая игра продолжается, а не падает на каждом ходу.
    use_fake_claude(world, claude_cli_model=cli_model)
    async with world.db() as session:
        await session.execute(
            update(Game)
            .where(Game.id == world.game_id)
            .values(provider=provider_name, model=game_model)
        )
        await session.commit()

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE, outcome.error
    (log,) = await world.logs()
    assert (log.provider, log.model, log.error) == ("claude_code_cli", expected, None)


async def test_a_failed_turn_after_a_good_one_names_no_model_in_the_journal(world):
    # Провайдер игры один на все её ходы: модель удачного хода не достаётся журналу следующего,
    # неудачного (спека §11 — журнал пишет модель, которой CLI провёл именно этот ход).
    good = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Тени скрывают вас…",
        "modelUsage": {"claude-opus-test-9": {"outputTokens": 7}},
    }
    failed = {"type": "result", "subtype": "error_during_execution", "is_error": True}
    use_fake_claude(
        world,
        {
            "FAKE_CLAUDE_LOG": str(world.tmp_path / "fake_claude.log"),  # по нему идёт очередь
            "FAKE_CLAUDE_REPLIES": json.dumps([json.dumps(good), json.dumps(failed)]),
        },
    )

    first = await world.run()
    second = await world.run()

    assert (first.status, second.status) == (TurnStatus.DONE, TurnStatus.FAILED)
    done, *failures = await world.logs()
    assert done.model == "claude-opus-test-9"
    assert failures and [log.model for log in failures] == [None] * len(failures)


SILENT_REPLY = {  # CLI ответил, но без рассказа: попытка неудачна, а автоповтор допустим
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "",
    "modelUsage": {"claude-first-test-1": {"outputTokens": 3}},
}


@pytest.mark.parametrize(
    ("second_reply", "models"),
    [
        pytest.param(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Тени скрывают вас…",
                "modelUsage": {"claude-second-test-2": {"outputTokens": 7}},
            },
            ["claude-first-test-1", "claude-second-test-2"],
            id="повтор удался",
        ),
        pytest.param(
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
            ["claude-first-test-1", None],
            id="повтор упал",
        ),
    ],
)
async def test_each_attempt_of_a_retried_turn_names_its_own_model_in_the_journal(
    world, second_reply, models
):
    # Спека §11: модель в журнале — та, которой CLI провёл именно эту попытку. При автоповторе
    # строка первой попытки не получает модель второй (или пустоту, если вторая упала).
    use_fake_claude(
        world,
        {
            "FAKE_CLAUDE_LOG": str(world.tmp_path / "fake_claude.log"),  # по нему идёт очередь
            "FAKE_CLAUDE_REPLIES": json.dumps([json.dumps(SILENT_REPLY), json.dumps(second_reply)]),
        },
    )

    outcome = await world.run()

    assert outcome.attempts == 2
    assert [log.model for log in await world.logs()] == models


async def test_a_retry_on_a_provider_with_one_model_names_it_in_every_attempt(monkeypatch, world):
    # Провайдеры API (`anthropic_api`, `openai_compatible`) держат одну модель на все запуски:
    # запись модели по попыткам не должна её терять ни у оборванной, ни у удавшейся попытки.
    provider = StubProvider(
        narration("Оборвалось на полусло", stop_reason="max_tokens"), narration()
    )
    provider.model = "stub-model-1"
    use_provider(monkeypatch, provider)

    outcome = await world.run()

    assert (outcome.status, outcome.attempts) == (TurnStatus.DONE, 2)
    assert [log.model for log in await world.logs()] == ["stub-model-1", "stub-model-1"]


async def test_a_cancelled_turn_names_the_model_its_provider_knows(monkeypatch, world):
    # Отмена прерывает вызов модели: строка журнала называет модель, которую знает провайдер игры
    # (у провайдеров API — модель запроса), как и до записи модели по попыткам.
    provider = StubProvider(gate=asyncio.Event())
    provider.model = "stub-model-1"
    use_provider(monkeypatch, provider)
    task = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [log.model for log in await world.logs()] == ["stub-model-1"]


@pytest.mark.parametrize(("profanity", "line"), [(True, "Мат: разрешён"), (False, "Мат: запрещён")])
async def test_the_context_of_the_turn_carries_the_profanity_flag_of_the_game(
    monkeypatch, world, profanity, line
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(profanity=profanity)
        )
        await session.commit()

    await world.run()

    (request,) = provider.requests
    assert line in request.context.splitlines()
    (log,) = await world.logs()
    assert line in log.request["context"].splitlines()  # журнал хранит тот же контекст


@pytest.mark.parametrize("profanity", [True, False])
async def test_the_profanity_flag_changes_neither_the_dice_nor_who_sees_a_secret_roll(
    world, profanity
):
    # Флажок мата — только стиль речи: в обоих режимах один и тот же бросок движка с тем же
    # итогом и Сл, а карточку броска тайного хода видит только его автор.
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(profanity=profanity)
        )
        await session.commit()
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    world.with_rng(ScriptedRng([13]))
    await world.act("alice", "Тайно крадусь", secret=True)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    roll = next(m for m in await world.messages() if m.kind == MessageKind.ROLL)
    assert (roll.audience, roll.data["natural"], roll.data["dc"]) == (world.alice.user_id, 13, 15)
    assert roll.data["total"] == 13 + roll.data["modifier"]
    for key in ("boris", "vera"):
        assert all(item["kind"] != "roll" for item in await world.feed_of(key))


async def test_the_journal_never_holds_the_keys_of_the_server(monkeypatch, world):
    # Ключи в настройках, а провайдер к тому же цитирует ключ в тексте своей ошибки.
    provider = StubProvider(RuntimeError(f"401 для ключа {API_KEY} и токена {BOT_TOKEN}"))
    use_provider(monkeypatch, provider)

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    (log,) = await world.logs()
    messages = [(m.text, m.data) for m in await world.messages()]
    everything = dump([log.error, log.request, log.trace, log.output, log.usage, messages])
    everything += dump(world.broadcaster.sent) + (outcome.error or "")
    assert not any(secret in everything for secret in SECRETS)
    assert "***" in error_of(log)


async def test_the_dice_come_from_the_rng_of_the_deps(world):
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    world.with_rng(ScriptedRng([20]))

    await world.run()

    roll = next(m for m in await world.messages() if m.kind == MessageKind.ROLL)
    assert roll.data["natural"] == 20


def test_the_game_path_has_no_random_module_and_takes_the_rng_from_the_deps():
    game_path = (
        "dm/turn.py",
        "dm/provider.py",
        "dm/spotlight.py",
        "dm/chronicle.py",
        "dm/executor.py",
        "dm/context.py",
    )
    forbidden_names = {"SeededRng", "ScriptedRng", "SystemRng"}
    for name in game_path:
        tree = ast.parse((SERVER_SRC / name).read_text(encoding="utf-8"))
        imported = imported_modules(SERVER_SRC / name)
        assert not {m for m in imported if m == "random" or m.startswith("random.")}, name
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & forbidden_names, name
    turn_tree = ast.parse((SERVER_SRC / "dm/turn.py").read_text(encoding="utf-8"))
    rng_reads = [
        node
        for node in ast.walk(turn_tree)
        if isinstance(node, ast.Attribute) and node.attr == "rng"
    ]
    assert rng_reads  # генератор берётся из `deps.rng`, а не создаётся в ходе


async def test_the_provider_gets_the_context_the_tools_and_the_limits_of_the_turn(
    monkeypatch, world
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await world.act("alice", "Иду к трактиру")
    await world.act("boris", "Тайно читаю свиток", secret=True)

    await world.run()

    (request,) = provider.requests
    assert request.context.startswith("# Контекст хода мастера (режим: round")
    assert "Иду к трактиру" in request.context
    assert '"marks": ["тайно"]' in request.context
    assert world.alice.user_id not in request.context  # id игроков мастеру не нужны
    names = {schema.name for schema in request.tools}
    assert {"request_check", "private_message", "chronicle", "grant_inspiration"} <= names
    assert request.max_tool_iterations == turn_module.MAX_TOOL_ITERATIONS == 20
    assert request.timeout_s == turn_module.TURN_TIMEOUT_S == 180.0
    assert "Склеп должника" in request.adventure_brief
    assert "Сводка приключения" in request.adventure_brief
    assert request.system.startswith("# Ты — мастер настольной игры")


async def test_the_adventure_of_the_turn_is_the_copy_saved_in_the_game(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        data = dict(game.adventure_data, title="Склеп из копии игры")
        game.adventure_data = data
        await session.commit()

    await world.run()

    assert "Склеп из копии игры" in provider.requests[0].adventure_brief
    assert not world.deps.settings.content_dir.exists()


async def test_the_next_turn_remembers_the_previous_story_and_its_secret_actions(
    monkeypatch, world
):
    provider = StubProvider(narration("Первый рассказ про склеп."), narration("Второй рассказ."))
    use_provider(monkeypatch, provider)
    await world.act("alice", "Тайно проверяю замок", secret=True)
    await world.run()
    await world.act("boris", "Иду дальше")

    await world.run()

    context = provider.requests[1].context
    assert "Прошлые ходы" in context
    assert "Первый рассказ про склеп." in context
    assert "Тайно проверяю замок" in context
    assert '"marks": ["тайно"]' in context


async def test_player_actions_are_finished_when_the_turn_is_done(world):
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Иду вперёд", status=ActionStatus.DRAFT)
    await world.act("boris", "", status=ActionStatus.DRAFT)
    await world.act("vera", "Жду", status=ActionStatus.SUBMITTED)

    await world.run()

    actions = await world.actions(world.turn_id)
    assert actions[world.alice.user_id].status == ActionStatus.AUTO_SUBMITTED
    assert actions[world.boris.user_id].status == ActionStatus.AUTO_PASS
    assert actions[world.vera.user_id].status == ActionStatus.SUBMITTED
    # Мастер разобрал черновик Алисы как сданный ход: «Ход сдан» — только ей и после коммита.
    told = [(p, who) for p, who in world.broadcaster.sent if p["type"] == "action_auto_submitted"]
    assert told == [({"type": "action_auto_submitted"}, world.alice.user_id)]


async def test_the_spotlight_counts_submitted_turns_and_the_focus_of_the_events(world):
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Крадусь")
    await world.act("boris", "Стою рядом")
    await world.act("vera", "")  # пас

    await world.run()

    spotlight = (await world.state()).spotlight
    alice, boris, vera = (world.players[k].character_id for k in ("alice", "boris", "vera"))
    assert spotlight[alice] == Spotlight(submitted=1, focused=1, turns_since_focus=0)
    assert spotlight[boris] == Spotlight(submitted=1, focused=0, turns_since_focus=1)
    assert spotlight[vera] == Spotlight(submitted=0, focused=0, turns_since_focus=1)


async def test_the_opening_turn_creates_its_own_turn_and_starts_the_play(opening_world):
    world = opening_world
    world.use_scenario(
        scenario_turn(tool("chronicle", entry="Игра началась."), say("Вы в деревне."))
    )

    outcome = await world.run(TurnKind.OPENING)

    assert outcome.status == TurnStatus.DONE
    game = await world.game()
    assert (game.phase, game.turn_phase, game.state_version) == (
        GamePhase.PLAYING,
        TurnPhase.DISCUSSION,
        2,
    )
    opening, following = await world.turns()
    assert (opening.kind, opening.number, opening.status) == (
        TurnKind.OPENING,
        1,
        TurnStatus.DONE,
    )
    assert (following.number, following.kind, following.status) == (
        2,
        TurnKind.ROUND,
        TurnStatus.OPEN,
    )
    assert game.current_turn_id == following.id


async def test_a_game_paused_while_the_master_thinks_stays_paused(monkeypatch, world):
    class PauseDuringTurn(StubProvider):
        async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
            async with world.db() as session:
                await session.execute(
                    update(Game).where(Game.id == world.game_id).values(phase=GamePhase.PAUSED)
                )
                await session.commit()
            return await super().run_turn(req, tools)

    use_provider(monkeypatch, PauseDuringTurn())

    outcome = await world.run()

    game = await world.game()
    assert outcome.status == TurnStatus.DONE
    assert (game.phase, game.turn_phase, game.state_version) == (
        GamePhase.PAUSED,
        TurnPhase.DISCUSSION,
        2,
    )


async def test_the_follow_up_of_the_deps_chooses_the_phase_and_the_kind_of_the_next_turn(
    monkeypatch, world
):
    seen: list[GameState] = []

    def combat_follow_up(state: GameState) -> tuple[TurnPhase, TurnKind]:
        seen.append(state)
        return TurnPhase.COMBAT_WAIT, TurnKind.COMBAT

    use_provider(monkeypatch, StubProvider())
    world.deps = dataclasses.replace(world.deps, follow_up=combat_follow_up)

    await world.run()

    game = await world.game()
    assert game.turn_phase == TurnPhase.COMBAT_WAIT
    assert seen[0].version == 2  # решает по состоянию после хода
    assert (await world.turns())[-1].kind == TurnKind.COMBAT
    assert world.broadcaster.of_type("phase")[-1][0]["phase"]["turn_phase"] == "combat_wait"


async def test_the_tool_context_gets_the_secret_authors_the_inspired_and_the_acting_fighters(
    monkeypatch, world
):
    created: list[dict[str, Any]] = []
    real_context = turn_module.ToolContext

    def spy(**kwargs: Any) -> Any:
        created.append(kwargs)
        return real_context(**kwargs)

    monkeypatch.setattr(turn_module, "ToolContext", spy)
    use_provider(monkeypatch, StubProvider())
    async with world.db() as session:  # вдохновение есть только у Алисы
        snapshot = await session.get(GameStateSnapshot, (world.game_id, 1))
        data = json.loads(json.dumps(snapshot.data))
        data["party"][world.alice.character_id]["inspiration"] = True
        snapshot.data = data
        await session.commit()
    await world.act("alice", "Тайно толкаю дверь", secret=True, inspiration=True)
    await world.act("boris", "Иду следом", inspiration=True)  # отметил, но вдохновения нет
    await world.act("vera", "")

    await world.run(TurnKind.COMBAT, acting=[world.alice.character_id])

    (context,) = created
    assert context["secret_actors"] == {world.alice.character_id}
    assert context["inspired"] == {world.alice.character_id}
    assert context["acting"] == (world.alice.character_id,)
    assert context["rng"] is world.deps.rng
    assert context["state"].turn == 1  # номер хода виден инструментам с самого начала


async def test_the_acting_fighters_are_in_the_journal_request(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())

    await world.run(TurnKind.COMBAT, acting=[world.alice.character_id, "goblin_warrior-1"])

    (log,) = await world.logs()
    assert log.request["acting"] == [world.alice.character_id, "goblin_warrior-1"]
    assert log.request["mode"] == "combat"


# --- тайна мастера не доходит до игроков ---


def windows(text: str, size: int) -> list[str]:
    """Все куски текста длиной `size` (скользящее окно по всему тексту, а не по началу)."""
    flat = " ".join(text.split())
    return [flat[i : i + size] for i in range(max(len(flat) - size + 1, 1))]


def leaked(haystack: str, hidden: list[str], size: int = 24) -> list[str]:
    flat = " ".join(haystack.split())
    return [w for text in hidden for w in windows(text, size) if len(w) == size and w in flat]


def test_the_leak_search_finds_a_piece_from_the_middle_of_a_secret():
    secret = "Это она сказала Фабиану искать работу на кладбище, лишь бы он убрался из трактира"

    assert leaked("рассказ: искать работу на кладбище, лишь бы он", [secret])
    assert not leaked("совсем другой рассказ про деревню", [secret])


async def test_nothing_the_master_read_reaches_the_players(world):
    hidden = hidden_texts(world.adventure.model_dump(mode="json"))
    world.use_scenario(
        scenario_turn(
            tool("get_location", location_id="village"),
            tool("get_npc", npc_id="marta"),
            tool("search_lore", query="Ульрих"),
            tool("chronicle", entry="Отряд узнал о пропаже."),
            say("Марта вытирает стойку и просит вас проверить кладбище."),
        )
    )

    await world.run()

    (log,) = await world.logs()
    assert leaked(dump(log.trace), hidden), "в журнале мастера заметки должны быть"
    seen = [m.text + dump(m.data) for m in await world.messages()]
    seen += [dump(payload) for payload, _ in world.broadcaster.sent]
    for key in world.players:
        seen.append(dump(await world.feed_of(key)))
    assert leaked(" ".join(seen), hidden) == []


LORE_MARK = "МИР-МАСТЕРА-"


async def test_the_journal_has_the_lore_around_the_party_and_the_players_get_none_of_it(
    db, settings, srd, adventure, tmp_path
):
    # Каждый текст мира, богов, фракций, связей и тайн мест в копии игры несёт метку. У Марты (она
    # в деревне, где отряд) есть и тайная связь: мастер должен видеть её с пометкой.
    data = adventure.model_dump(mode="json")
    data["relations"].append(
        {
            "id": "marta-kin-fabian",
            "a": "npc:marta",
            "b": "npc:fabian",
            "kind": "kin",
            "secret": True,
        }
    )
    trapped = adventure_from_data(trap_lore(data, LORE_MARK))
    world = await build_world(db, settings, srd, trapped, tmp_path)
    world.use_scenario(
        scenario_turn(
            tool("get_world"),
            tool("get_relations", ref="npc:marta"),
            tool("chronicle", entry="Отряд расспросил Марту о старых богах."),
            say("Марта пожимает плечами и наливает всем по кружке."),
        )
    )
    await world.act("alice", "Расспрашиваю Марту о старых богах")

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    # Журнал хода (`dm_turn_logs`) хранит контекст мастера с разделом мира и ответы инструментов.
    lore = log.request["context"].split("## Мир приключения\n", 1)[1].split("\n## ", 1)[0]
    for text in ("история-мира", "name-veyla", "name-regulars", "связь-fabian-debtor-marta"):
        assert f"{LORE_MARK}{text}" in lore
    (hidden,) = [line for line in lore.splitlines() if "связь-marta-kin-fabian" in line]
    assert "тайная: игроки не знают" in hidden
    assert f"{LORE_MARK}слух-" in dump(log.trace)  # get_world
    # Игроку из этого не уходит ничего: ни рассказ, ни события, ни снимок, ни API игры.
    seen = [message.text + dump(message.data) for message in await world.messages()]
    seen += [dump(payload) for payload, _ in world.broadcaster.sent]
    journals = []
    async with world.db() as session:
        for player in world.players.values():
            user = await session.get(User, player.user_id)
            snapshot = await snapshot_for(session, user, world.game_id, srd=srd)
            journals.append(snapshot.pop("journal"))
            seen.append(dump(snapshot))
    assert "Марта пожимает плечами" in " ".join(seen)
    assert LORE_MARK not in " ".join(seen)
    # Дневник (блок B8) называет только то, что отряд знает со старта (`known_at_start`): имя бога и
    # края и сам слух. Других меток мира в нём нет.
    known = (
        {
            f"{LORE_MARK}name-{deity.id}"
            for deity in trapped.deities.values()
            if deity.known_at_start
        }
        | {
            f"{LORE_MARK}край-{region.id}"
            for region in trapped.world.regions
            if region.known_at_start
        }
        | {
            rumor.text
            for rumor in trapped.world.rumors
            if rumor.known_at_start and not rumor.public
        }
    )
    for journal in journals:
        assert set(re.findall(rf"{LORE_MARK}[\w-]+", dump(journal))) == known


# Значение из битой копии: ни в тексте ошибки, ни в журнале сервера его быть не должно.
BROKEN_VALUE = "ЗНАЧЕНИЕ-ИЗ-КОПИИ"


async def test_a_broken_adventure_copy_stops_the_turn_before_the_reserve_and_changes_nothing(
    world, caplog
):
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        data = copy.deepcopy(game.adventure_data)  # новый объект: иначе SQLAlchemy не увидит правки
        data["world"]["history"] = {"текст": BROKEN_VALUE}
        data["relations"].append(
            {"id": "broken", "a": f"npc:{BROKEN_VALUE}", "b": "npc:marta", "kind": BROKEN_VALUE}
        )
        game.adventure_data = data
        await session.commit()
    await world.act("alice", "Иду вперёд")
    messages_before = [message.id for message in await world.messages()]

    with (
        caplog.at_level(logging.ERROR, logger="dnd_server.dm.turn"),
        pytest.raises(Conflict) as failure,
    ):
        await world.run()

    # Ход не начался: резерв откатился целиком, игра ждёт хоста в той же фазе.
    game = await world.game()
    assert (game.turn_phase, game.state_version, game.current_turn_id) == (
        TurnPhase.DISCUSSION,
        1,
        world.turn_id,
    )
    assert await world.snapshot_versions() == [1]
    assert [(turn.id, turn.status) for turn in await world.turns()] == [
        (world.turn_id, TurnStatus.OPEN)
    ]
    assert [message.id for message in await world.messages()] == messages_before
    assert await world.logs() == [] and world.broadcaster.sent == []
    # Хост получает понятный отказ (409, а не 500), журнал сервера — где беда, без значений копии.
    assert failure.value.message == BROKEN_COPY
    [record] = [r for r in caplog.records if r.name == "dnd_server.dm.turn"]
    assert world.game_id in record.getMessage() and "world" in record.getMessage()
    for text in (str(failure.value), caplog.text):
        assert BROKEN_VALUE not in text and "input_value" not in text


# --- неудачная попытка ---


async def failed_turn_leaves_the_game_as_it_was(world: World, outcome: TurnOutcome) -> None:
    assert outcome.status == TurnStatus.FAILED
    assert outcome.state_version == 1
    game = await world.game()
    assert (game.phase, game.turn_phase, game.state_version) == (
        GamePhase.PLAYING,
        TurnPhase.DM_FAILED,
        1,
    )
    assert game.current_turn_id == world.turn_id
    assert await world.snapshot_versions() == [1]
    (failed,) = await world.turns()
    assert (failed.status, failed.state_version_after) == (TurnStatus.FAILED, None)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("баг в нашем коде"), "внутренняя ошибка"),
        (ProviderError("упал адаптер"), "ошибка провайдера"),
        (ProviderRefusal("отказ"), "отказалась"),
        (ToolLoopExceeded("слишком много"), "слишком много вызовов"),
        (ValueError("неожиданное"), "внутренняя ошибка"),
    ],
    ids=["RuntimeError", "ProviderError", "ProviderRefusal", "ToolLoopExceeded", "ValueError"],
)
async def test_any_exception_is_a_failed_attempt_and_the_phase_does_not_hang(
    monkeypatch, world, error, reason
):
    use_provider(monkeypatch, StubProvider(error))
    await world.act("alice", "Иду вперёд")

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert reason in (outcome.error or "")
    (log,) = await world.logs()
    assert (log.attempt, log.output) == (1, None)
    assert type(error).__name__ in error_of(log)
    actions = await world.actions(world.turn_id)
    assert actions[world.alice.user_id].status == ActionStatus.SUBMITTED  # ходы игроков целы


async def test_an_unexpected_error_of_the_turn_is_logged_with_the_game_and_the_turn(
    monkeypatch, world, caplog
):
    use_provider(monkeypatch, StubProvider(RuntimeError("баг в нашем коде")))
    await world.act("alice", "Иду вперёд")

    await world.run()

    # ERROR с трассировкой сразу называет игру, ход и попытку; по соседним записям искать не надо.
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert world.game_id in message and world.turn_id in message and "попытка 1" in message
    assert "RuntimeError: баг в нашем коде" in message


def errors_logged(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def assert_log_has_no_input_of(leak, caplog, *, at_least: int = 1) -> None:
    """В журнале сервера есть ошибка (с типом и без трассировки на `exc_info`), но нет значений."""
    records = errors_logged(caplog)
    assert len(records) >= at_least
    assert all(r.exc_info is None for r in records)  # трассировку формирует сервер, а не logging
    assert "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_an_unexpected_error_with_the_input_of_pydantic_is_logged_and_journaled_without_it(
    monkeypatch, world, caplog, leak
):
    use_provider(monkeypatch, StubProvider(leak.error))
    await world.act("alice", "Иду вперёд")

    await world.run()

    assert_log_has_no_input_of(leak, caplog)
    (log,) = await world.logs()
    assert leak.value not in dump([log.error, log.trace]) and "ValidationError" in error_of(log)


async def test_a_failed_record_of_the_failure_is_logged_without_the_input_of_the_error(
    monkeypatch, world, caplog, leak
):
    use_provider(monkeypatch, StubProvider(ProviderError("модель недоступна")))

    async def broken(*_args: Any) -> None:
        raise leak.error

    monkeypatch.setattr(turn_module, "_commit_failure", broken)
    monkeypatch.setattr(turn_module, "_mark_dm_failed", broken)

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    assert_log_has_no_input_of(leak, caplog, at_least=2)  # запись неудачи и короткая отметка


async def test_a_failing_on_reserved_is_logged_without_the_input_of_the_error(
    monkeypatch, world, caplog, leak
):
    use_provider(monkeypatch, StubProvider())

    def broken() -> None:
        raise leak.error

    outcome = await world.run(on_reserved=broken)

    assert outcome.status == TurnStatus.DONE
    assert_log_has_no_input_of(leak, caplog)


async def test_a_failing_announcement_is_logged_without_the_input_and_the_turn_stays_done(
    monkeypatch, world, caplog, leak
):
    use_provider(monkeypatch, StubProvider())

    def broken(*_args: Any, **_options: Any) -> None:
        raise leak.error

    monkeypatch.setattr(turn_module, "phase_event", broken)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    assert_log_has_no_input_of(leak, caplog)
    assert world.game_id in caplog.text and world.turn_id in caplog.text


async def test_the_host_hears_that_the_master_failed_and_everybody_sees_the_phase(
    monkeypatch, world
):
    use_provider(monkeypatch, StubProvider(RuntimeError("баг")))

    await world.run()

    (note,) = [m for m in await world.messages() if m.kind == MessageKind.SYSTEM]
    assert note.audience == world.alice.user_id
    assert "Мастер не справился" in note.text
    assert "Повторить" in note.text
    phases = world.broadcaster.of_type("phase")
    assert phases[-1][0]["phase"]["turn_phase"] == "dm_failed"
    assert phases[-1][1] == "all"
    feed = world.broadcaster.of_type("feed_item")
    assert [(p["item"]["kind"], who) for p, who in feed] == [("system", world.alice.user_id)]
    assert all("Мастер не справился" not in i["text"] for i in await world.feed_of("boris"))


async def test_the_note_of_a_failed_round_says_that_the_moves_of_the_players_are_kept(
    monkeypatch, world
):
    use_provider(monkeypatch, StubProvider(RuntimeError("баг")))

    await world.run()

    (note,) = [m for m in await world.messages() if m.kind == MessageKind.SYSTEM]
    assert "Ходы игроков сохранены" in note.text


async def test_the_note_of_a_failed_opening_speaks_of_the_start_and_not_of_player_moves(
    monkeypatch, opening_world
):
    # У открытия игры ходов игроков нет: «ходы сохранены» только сбило бы хоста с толку.
    use_provider(monkeypatch, StubProvider(RuntimeError("баг")))

    outcome = await opening_world.run(TurnKind.OPENING)

    assert outcome.status == TurnStatus.FAILED
    (note,) = [m for m in await opening_world.messages() if m.kind == MessageKind.SYSTEM]
    assert note.audience == opening_world.alice.user_id  # только хост
    assert note.text == (
        "Мастер не справился с началом игры: внутренняя ошибка сервера. Нажмите «Повторить»."
    )
    assert "игроков" not in note.text and "ходы" not in note.text.lower()


@pytest.mark.parametrize(
    ("kind", "note", "refusal"),
    [
        (
            TurnKind.OPENING,
            "Мастер не справился с началом игры: причина. Нажмите «Повторить».",
            "Мастер не справился с началом игры: хост может нажать «Повторить».",
        ),
        (
            TurnKind.CAMP_END,
            "Мастер не справился с концом привала: причина. Нажмите «Повторить».",
            "Мастер не справился с концом привала: хост может нажать «Повторить».",
        ),
        (
            TurnKind.ROUND,
            "Мастер не справился с ходом: причина. Ходы игроков сохранены — нажмите «Повторить».",
            "Мастер не справился с ходом: ходы сохранены, хост может нажать «Повторить».",
        ),
        (
            TurnKind.COMBAT,
            "Мастер не справился с ходом: причина. Ходы игроков сохранены — нажмите «Повторить».",
            "Мастер не справился с ходом: ходы сохранены, хост может нажать «Повторить».",
        ),
    ],
    ids=["открытие игры", "конец привала", "раунд", "бой"],
)
def test_the_texts_of_a_failure_speak_of_player_moves_only_where_there_are_some(
    kind, note, refusal
):
    # У открытия игры и конца привала ходов игроков нет (на привале только реплики у костра):
    # «ходы сохранены» там было бы неправдой. Ход вида `kind` объясняют два текста: сообщение хосту
    # и отказ игроку, который пытается сдать ход.
    assert failure_text(kind, "причина") == note
    assert failed_turn_refusal(kind) == refusal


async def test_a_failed_turn_can_be_repeated_and_the_scenario_goes_on(world):
    world.use_scenario_file(SCENARIOS_DIR / "failing.yaml")
    await world.act("alice", "Иду вперёд")

    first = await world.run()
    retry = await world.run()

    assert (first.status, first.attempts) == (TurnStatus.FAILED, 2)
    assert (retry.status, retry.state_version) == (TurnStatus.DONE, 2)
    game = await world.game()
    assert game.turn_phase == TurnPhase.DISCUSSION
    logs = await world.logs()
    assert [(log.attempt, log.error is None) for log in logs] == [
        (1, False),
        (2, False),
        (3, True),
    ]
    done, _following = await world.turns()
    assert (done.id, done.status) == (world.turn_id, TurnStatus.DONE)


async def test_the_loop_scenario_exceeds_the_tool_limit_and_fails_the_turn(monkeypatch, world):
    monkeypatch.setattr(turn_module, "MAX_TOOL_ITERATIONS", 5)
    world.use_scenario_file(SCENARIOS_DIR / "loop.yaml")

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert "слишком много вызовов" in (outcome.error or "")
    (log,) = await world.logs()
    assert "ToolLoopExceeded" in error_of(log) or "вызовов инструментов" in error_of(log)


async def test_a_context_that_cannot_be_built_is_a_failed_attempt(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    async with world.db() as session:
        snapshot = await session.get(GameStateSnapshot, (world.game_id, 1))
        data = json.loads(json.dumps(snapshot.data))
        data["location_id"] = "нет-такой-локации"
        snapshot.data = data
        await session.commit()

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    (log,) = await world.logs()
    assert log.trace == [] and log.output is None


async def test_text_that_is_cut_by_the_token_limit_is_a_failed_attempt_with_one_retry(
    monkeypatch, world
):
    provider = StubProvider(
        narration("Рассказ оборвался на полусло", stop_reason="max_tokens"),
        narration("Целый рассказ."),
    )
    use_provider(monkeypatch, provider)

    outcome = await world.run()

    assert (outcome.status, outcome.attempts) == (TurnStatus.DONE, 2)
    logs = await world.logs()
    assert "лимит" in error_of(logs[0])
    assert logs[0].output is None  # оборванный рассказ в журнал как итог не идёт
    assert logs[1].error is None
    story = [m for m in await world.messages() if m.kind == MessageKind.NARRATION]
    assert [m.text for m in story] == ["Целый рассказ."]


async def test_text_cut_twice_fails_the_turn(monkeypatch, world):
    cut = narration("Опять оборвалось", stop_reason="max_tokens")
    use_provider(monkeypatch, StubProvider(cut))

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert outcome.attempts == 2
    assert len(await world.logs()) == 2


async def test_an_empty_text_is_retried_once_from_scratch_on_a_new_copy(world):
    world.use_scenario(
        scenario_turn(tool("chronicle", entry="Первая попытка.")),
        scenario_turn(
            tool("chronicle", entry="Вторая попытка."), say("Рассказ со второй попытки.")
        ),
    )

    outcome = await world.run()

    assert (outcome.status, outcome.attempts) == (TurnStatus.DONE, 2)
    state = await world.state()
    assert state.chronicle.entries == ["Вторая попытка."]  # первая копия выброшена
    first, second = await world.logs()
    assert (first.attempt, second.attempt) == (1, 2)
    assert "рассказа" in error_of(first)
    assert first.trace[0]["args"] == {"entry": "Первая попытка."}
    assert (second.error, second.output) == (None, "Рассказ со второй попытки.")
    assert len(await world.messages()) == 1


async def test_two_empty_texts_in_a_row_fail_the_turn(world):
    world.use_scenario(
        scenario_turn(tool("chronicle", entry="Раз.")),
        scenario_turn(tool("chronicle", entry="Два.")),
    )

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert outcome.attempts == 2
    assert [log.attempt for log in await world.logs()] == [1, 2]


async def test_a_failure_while_writing_the_commit_rolls_everything_back_and_fails_the_turn(
    monkeypatch, world
):
    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("диск переполнен")

    monkeypatch.setattr(turn_module, "_finish_actions", broken)  # уже после снимка и сообщений
    world.use_scenario(
        scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")),
    )
    await world.act("alice", "Иду вперёд", status=ActionStatus.DRAFT)

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    # В ленте остаётся только сообщение хосту о неудаче.
    assert [m.kind for m in await world.messages()] == [MessageKind.SYSTEM]
    assert (await world.actions(world.turn_id))[world.alice.user_id].status == ActionStatus.DRAFT
    (log,) = await world.logs()
    assert "RuntimeError" in error_of(log)
    assert log.output is None


async def test_a_database_error_of_the_commit_keeps_the_row_values_out_of_the_log_and_the_journal(
    world, caplog
):
    # Текст ошибки SQLAlchemy несёт связанные параметры запроса: тексты личных сообщений, тайных
    # ходов и снимок состояния. Журнал сервера и `dm_turn_logs.error` их получать не должны.
    private_line = "ЛИЧНОЕ-ТАЙНОЕ-boris: ключ лежит под ковром."
    world.use_scenario(
        scenario_turn(
            tool("private_message", character_id="$pc2", text=private_line),
            tool("chronicle", entry="з"),
            say("Итог."),
        )
    )
    await world.act("boris", "Тайно прячу ключ", secret=True)
    async with world.db() as session:  # база отказывает на записи личных сообщений
        await session.execute(
            raw_sql(
                "CREATE TRIGGER no_private BEFORE INSERT ON messages WHEN NEW.kind = 'private' "
                "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
            )
        )
        await session.commit()
    caplog.set_level(logging.DEBUG)

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    (log,) = await world.logs()
    assert "IntegrityError" in error_of(log) and "disk I/O error" in error_of(log)  # причина видна
    # Журнал сервера (без отладочных записей драйвера, которые ведёт владелец сам): причина есть,
    # значений строк нет.
    server_log = "\n".join(
        logging.Formatter().format(record)
        for record in caplog.records
        if record.name.startswith("dnd_server")
    )
    assert "IntegrityError" in server_log
    assert private_line not in error_of(log)
    assert private_line not in server_log


# --- сбой самой записи неудачи и рассылки после коммита ---


async def break_the_journal_table(world: World) -> None:
    """Таблица журнала отказывает на вставке: не пишутся ни ход, ни запись неудачи (обе несут строки
    журнала), а короткая отметка `dm_failed` проходит."""
    async with world.db() as session:
        await session.execute(
            raw_sql(
                "CREATE TRIGGER no_journal BEFORE INSERT ON dm_turn_logs "
                "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
            )
        )
        await session.commit()


async def test_when_even_the_record_of_the_failure_fails_the_game_still_leaves_resolving(
    world, caplog
):
    world.use_scenario(scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Иду вперёд")
    await break_the_journal_table(world)

    outcome = await world.run()

    # Хост видит `dm_failed` и может повторять; журнала и сообщения хосту нет (записать их нельзя).
    assert outcome.status == TurnStatus.FAILED
    game = await world.game()
    assert (game.turn_phase, game.deadline_at, game.state_version) == (TurnPhase.DM_FAILED, None, 1)
    (failed,) = await world.turns()
    assert (failed.status, failed.state_version_after) == (TurnStatus.FAILED, None)
    assert await world.logs() == [] and await world.snapshot_versions() == [1]
    phases = world.broadcaster.of_type("phase")
    assert phases[-1][0]["phase"]["turn_phase"] == "dm_failed"  # игроки видят сбой
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(world.game_id in m and world.turn_id in m for m in errors), errors


async def test_a_game_whose_failure_record_failed_can_be_repeated_at_once(world):
    world.use_scenario(
        scenario_turn(check("$pc1"), tool("chronicle", entry="з"), say("Итог.")),
        scenario_turn(tool("chronicle", entry="з"), say("Итог со второго раза.")),
    )
    await world.act("alice", "Иду вперёд")
    await break_the_journal_table(world)
    await world.run()
    async with world.db() as session:
        await session.execute(raw_sql("DROP TRIGGER no_journal"))
        await session.commit()

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    assert (await world.game()).turn_phase == TurnPhase.DISCUSSION


async def test_the_game_is_known_to_have_a_live_master_only_while_a_turn_runs(monkeypatch, world):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    assert world.game_id not in world.deps.live

    task = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)
    assert world.game_id in world.deps.live  # мастер думает
    gate.set()
    await asyncio.wait_for(task, TIMEOUT_S)

    assert world.game_id not in world.deps.live


@pytest.mark.parametrize(
    "ending", ["failure", "refusal", "cancellation"], ids=["failure", "refusal", "cancellation"]
)
async def test_the_live_master_is_forgotten_however_the_turn_ends(monkeypatch, world, ending):
    gate = asyncio.Event()
    provider = StubProvider(RuntimeError("баг"), gate=gate)
    use_provider(monkeypatch, provider)
    if ending == "refusal":
        async with world.db() as session:  # игра уже не идёт: резерв откажет
            await session.execute(update(Game).values(phase=GamePhase.PAUSED))
            await session.commit()
    task = asyncio.create_task(world.run())
    if ending == "cancellation":
        await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)
        task.cancel()
    else:
        gate.set()

    await asyncio.gather(task, return_exceptions=True)

    assert world.game_id not in world.deps.live


async def test_a_broken_announcement_after_the_commit_does_not_turn_a_good_turn_into_a_failure(
    monkeypatch, world, caplog
):
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Иду вперёд")

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("построитель события сломан")

    monkeypatch.setattr(turn_module, "state_events", broken)

    outcome = await world.run()

    # Запись хода уже есть: рассылка её не отменяет. Ход удался, журнал один и без ошибки.
    assert outcome.status == TurnStatus.DONE
    game = await world.game()
    assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 2)
    (log,) = await world.logs()
    assert log.error is None and log.output == "Итог."
    assert [m.kind for m in await world.messages() if m.kind == MessageKind.SYSTEM] == []
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(world.game_id in m and world.turn_id in m for m in errors), errors


async def test_a_broken_announcement_of_the_reserve_does_not_leave_the_game_resolving(
    monkeypatch, world, caplog
):
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Итог.")))
    await world.act("alice", "Иду вперёд")
    real_phase_event = turn_module.phase_event
    calls: list[int] = []

    def broken_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:  # «Мастер думает» уходит сразу после коммита резерва
            raise RuntimeError("построитель события сломан")
        return real_phase_event(*args, **kwargs)

    monkeypatch.setattr(turn_module, "phase_event", broken_once)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE  # мастер всё равно позван и ответил
    assert (await world.game()).turn_phase == TurnPhase.DISCUSSION
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(world.game_id in m and world.turn_id in m for m in errors), errors


async def test_a_broken_announcement_of_the_failure_keeps_the_failure_record(
    monkeypatch, world, caplog
):
    use_provider(monkeypatch, StubProvider(RuntimeError("баг")))
    await world.act("alice", "Иду вперёд")

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("построитель события сломан")

    monkeypatch.setattr(turn_module, "feed_item_event", broken)

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert len(await world.logs()) == 1  # запись неудачи есть и не дублируется
    [note] = [m for m in await world.messages() if m.kind == MessageKind.SYSTEM]
    assert note.audience == world.alice.user_id
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(world.game_id in m for m in errors), errors


async def test_an_engine_error_inside_a_tool_fails_the_turn_and_drops_the_copy(monkeypatch, world):
    def broken(_registry: Any, _ctx: Any, _name: str, _raw_args: Any) -> Any:
        raise RuntimeError("баг движка")

    monkeypatch.setattr("dnd_engine.tools.registry.ToolRegistry.call", broken)
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Итог.")))

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert (await world.state()).chronicle.entries == []


async def test_an_unknown_tool_is_an_error_for_the_model_and_the_turn_goes_on(world):
    world.use_scenario(
        scenario_turn(tool("ghost", x=1), tool("chronicle", entry="з"), say("Итог."))
    )

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    (log,) = await world.logs()
    assert log.trace[0]["is_error"] is True
    assert "Неизвестный инструмент" in log.trace[0]["result"]


async def test_the_text_of_a_bad_call_is_made_safe_before_it_reaches_the_journal(world):
    hostile = "\x1b[2J\u202eфальшивая строка"
    world.use_scenario(
        scenario_turn(
            tool("request_check", **{hostile: 1}), tool("chronicle", entry="з"), say("Итог.")
        )
    )

    await world.run()

    (log,) = await world.logs()
    result = log.trace[0]["result"]
    assert log.trace[0]["is_error"] is True
    assert "\x1b" not in result and "\u202e" not in result
    assert "\\u202e" in result  # знак записан видимым текстом, а не действует на терминал


async def test_a_provider_that_does_not_answer_in_time_fails_the_turn_and_is_stopped(
    monkeypatch, world
):
    monkeypatch.setattr(turn_module, "TURN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(turn_module, "TIMEOUT_GRACE_S", 0.05)
    provider = StubProvider(gate=asyncio.Event())  # ворота никто не откроет
    use_provider(monkeypatch, provider)

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    assert "время ожидания" in (outcome.error or "")
    assert provider.cancelled is True
    assert "тайм-аут" in error_of((await world.logs())[0])


async def test_cancelling_the_turn_does_not_leave_the_game_in_resolving(monkeypatch, world):
    provider = StubProvider(gate=asyncio.Event())
    use_provider(monkeypatch, provider)
    task = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    game = await world.game()
    assert game.turn_phase == TurnPhase.DM_FAILED
    assert [log.error is not None for log in await world.logs()] == [True]


# --- блокировка, соединение и конфликт версий (F13) ---


async def test_neither_the_lock_nor_a_connection_is_held_while_the_model_thinks(monkeypatch, world):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)

    task = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    assert world.pool.checkedout() == 0  # пул свободен
    assert not world.deps.locks.lock(world.game_id).locked()  # игра не заперта
    game = await world.game()
    assert game.turn_phase == TurnPhase.RESOLVING
    async with world.db() as session:  # чужая запись проходит сразу
        await session.execute(
            update(GamePlayer).where(GamePlayer.user_id == world.vera.user_id).values(ready=False)
        )
        await session.commit()
    gate.set()
    outcome = await asyncio.wait_for(task, TIMEOUT_S)
    assert outcome.status == TurnStatus.DONE
    assert world.pool.checkedout() == 0


async def test_a_second_request_while_the_master_is_resolving_is_refused_at_once(
    monkeypatch, world
):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    first = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    with pytest.raises(Conflict, match="уже"):
        await asyncio.wait_for(world.run(), TIMEOUT_S)
    with pytest.raises(Conflict):
        await asyncio.wait_for(world.regenerate(), TIMEOUT_S)

    assert len(provider.requests) == 1  # модель вызвана один раз
    gate.set()
    assert (await first).status == TurnStatus.DONE


async def test_two_parallel_turns_run_one_after_another_and_never_together(monkeypatch, world):
    provider = StubProvider(gate=None)
    use_provider(monkeypatch, provider)

    results = await asyncio.gather(world.run(), world.run(), return_exceptions=True)

    done = [r for r in results if isinstance(r, TurnOutcome)]
    refused = [r for r in results if isinstance(r, Conflict)]
    assert (len(done), len(refused)) == (1, 1)
    assert (await world.game()).state_version == 2
    # Следом идёт следующий ход игры: ходы одной игры разбираются по очереди.
    again = await world.run()
    assert (again.status, again.state_version) == (TurnStatus.DONE, 3)
    assert [turn.status for turn in await world.turns()] == [
        TurnStatus.DONE,
        TurnStatus.DONE,
        TurnStatus.OPEN,
    ]


async def test_a_turn_waits_for_the_lock_of_the_game_before_it_starts(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    lock = world.deps.locks.lock(world.game_id)
    await lock.acquire()
    task = asyncio.create_task(world.run())
    await asyncio.sleep(0.05)
    assert not provider.started.is_set()  # резерв ждёт блокировку, модель ещё не вызвана
    assert world.pool.checkedout() == 0  # ждущий резерв не держит соединение пула

    lock.release()
    outcome = await asyncio.wait_for(task, TIMEOUT_S)

    assert outcome.status == TurnStatus.DONE


async def test_the_commit_waits_for_the_lock_of_the_game(monkeypatch, world):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    task = asyncio.create_task(world.run())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)
    lock = world.deps.locks.lock(world.game_id)
    await lock.acquire()  # игру заняли за время ожидания модели

    gate.set()
    await asyncio.sleep(0.05)

    assert not task.done()  # ответ модели готов, но коммит ждёт блокировку
    assert world.pool.checkedout() == 0  # ждущий коммит не держит соединение пула
    assert (await world.game()).state_version == 1
    lock.release()
    outcome = await asyncio.wait_for(task, TIMEOUT_S)
    assert (outcome.status, outcome.state_version) == (TurnStatus.DONE, 2)


async def test_a_state_version_that_moved_during_the_wait_is_a_conflict_and_is_not_committed(
    monkeypatch, world
):
    class RivalCommit(StubProvider):
        async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
            async with world.db() as session:  # чужой коммит за время ожидания модели
                game = await session.get(Game, world.game_id)
                game.state_version = 5
                await session.commit()
            return await super().run_turn(req, tools)

    use_provider(monkeypatch, RivalCommit())

    outcome = await world.run()

    assert outcome.status == TurnStatus.FAILED
    assert "изменилось" in (outcome.error or "")
    assert await world.snapshot_versions() == [1]  # новый снимок не записан
    # В ленте остаётся только сообщение хосту о неудаче.
    assert [m.kind for m in await world.messages()] == [MessageKind.SYSTEM]
    (log,) = await world.logs()
    assert "конфликт" in error_of(log)
    assert (await world.game()).state_version == 5  # чужую версию ход не трогает


async def test_a_game_that_is_not_running_or_unknown_refuses_to_start_a_turn(world):
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(phase=GamePhase.PAUSED)
        )
        await session.commit()
    with pytest.raises(Conflict):
        await world.run()
    with pytest.raises(NotFound):
        await run_dm_turn("нет-такой-игры", mode=TurnKind.ROUND, deps=world.deps)


async def test_a_turn_that_is_already_done_is_not_resolved_again(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    await world.run()
    async with world.db() as session:  # текущим снова назвали уже разобранный ход
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(current_turn_id=world.turn_id)
        )
        await session.commit()

    with pytest.raises(Conflict, match="уже разобран"):
        await world.run()

    assert (await world.game()).state_version == 2


async def test_a_game_without_a_state_has_nothing_for_the_master_to_resolve(world):
    async with world.db() as session:
        await session.execute(update(Game).where(Game.id == world.game_id).values(state_version=7))
        await session.commit()

    with pytest.raises(Conflict, match="нет состояния"):
        await world.run()

    assert (await world.game()).turn_phase == TurnPhase.DISCUSSION  # резерв не оставил следов


async def test_a_request_for_a_turn_that_is_no_longer_open_is_refused_at_the_reserve(
    monkeypatch, world
):
    # Защита в глубину: запрос, сделанный для одного хода, не должен разобрать уже следующий.
    provider = StubProvider()
    use_provider(monkeypatch, provider)

    with pytest.raises(Conflict, match="другой ход"):
        await world.run(expected_turn_id="ход-который-уже-закрыт")

    assert provider.requests == []  # мастера не звали
    game = await world.game()
    assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 1)
    assert await world.logs() == [] and world.broadcaster.sent == []


async def test_the_turn_the_request_names_is_resolved_as_usual(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())

    outcome = await world.run(expected_turn_id=world.turn_id)

    assert (outcome.turn_id, outcome.status) == (world.turn_id, TurnStatus.DONE)


async def test_a_refused_start_changes_nothing(world):
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(phase=GamePhase.LOBBY)
        )
        await session.commit()

    with pytest.raises(Conflict):
        await world.run()

    assert world.broadcaster.sent == []
    assert await world.logs() == []


async def test_reconnecting_a_socket_does_not_start_a_turn(monkeypatch, world, settings, db):
    factory = use_provider(monkeypatch, StubProvider())
    async with db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(turn_phase=TurnPhase.DM_FAILED)
        )
        await session.commit()
    app_settings = settings.model_copy(update={"session_secret": SecretStr(SESSION_SECRET)})
    app = create_app(app_settings)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    token = SessionSigner(SESSION_SECRET).issue(world.alice.user_id)
    try:
        for _ in range(2):  # подключение и повторное подключение
            url = f"ws://127.0.0.1:{port}/ws/games/{world.game_id}?token={token}"
            async with connect(url) as ws:
                snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
                assert snapshot["type"] == "snapshot"
                assert snapshot["phase"]["turn_phase"] == "dm_failed"
    finally:
        server.should_exit = True
        await task
        await app.state.engine.dispose()

    assert factory.calls == []
    assert (await world.game()).turn_phase == TurnPhase.DM_FAILED
    assert await world.logs() == []


# --- «Перегенерировать» ---


async def test_regenerate_supersedes_the_old_output_rolls_back_the_state_and_writes_a_new_result(
    world,
):
    world.use_scenario(
        scenario_turn(
            check("$pc1"),
            tool("private_message", character_id="$pc2", text="Старая тайна."),
            tool("chronicle", entry="Старая запись."),
            say("Старый рассказ."),
        ),
        scenario_turn(tool("chronicle", entry="Новая запись."), say("Новый рассказ.")),
    )
    world.with_rng(ScriptedRng([5, 17]))
    await world.act("alice", "Крадусь вперёд")
    await world.run()
    old_next = (await world.game()).current_turn_id
    await world.act(
        "boris", "Черновик следующего раунда", turn_id=old_next, status=ActionStatus.DRAFT
    )
    world.broadcaster.sent.clear()

    outcome = await world.regenerate()

    assert (outcome.status, outcome.state_version) == (TurnStatus.DONE, 3)
    messages = await world.messages()
    old = [m for m in messages if m.superseded]
    assert sorted(m.kind.value for m in old) == ["narration", "private", "roll"]
    assert all(m.text != "Старый рассказ." for m in messages if not m.superseded)
    live = [m for m in messages if not m.superseded]
    assert [m.kind for m in live] == [MessageKind.NARRATION]
    assert live[0].text == "Новый рассказ."
    state = await world.state()
    assert state.version == 3
    assert state.chronicle.entries == ["Новая запись."]  # старая запись откатилась вместе с ходом
    assert await world.snapshot_versions() == [1, 2, 3]
    game = await world.game()
    assert (game.state_version, game.turn_phase) == (3, TurnPhase.DISCUSSION)
    turns = await world.turns()
    assert [(t.status, t.state_version_before, t.state_version_after) for t in turns[:1]] == [
        (TurnStatus.DONE, 1, 3)
    ]
    superseded_turn = next(t for t in turns if t.id == old_next)
    assert superseded_turn.status == TurnStatus.SUPERSEDED
    new_open = next(t for t in turns if t.id == game.current_turn_id)
    assert (new_open.number, new_open.status, new_open.state_version_before) == (
        2,
        TurnStatus.OPEN,
        3,
    )
    logs = await world.logs()
    assert [(log.turn_id, log.attempt) for log in logs] == [(world.turn_id, 1), (world.turn_id, 2)]


async def test_regenerate_leaves_the_state_rolled_back_while_the_new_attempt_is_running(
    monkeypatch, world
):
    use_provider(monkeypatch, StubProvider(narration("Старый рассказ.")))
    await world.run()
    gate = asyncio.Event()
    provider = StubProvider(narration("Новый рассказ."), gate=gate)
    use_provider(monkeypatch, provider)
    world.deps = dataclasses.replace(world.deps, providers=GameProviders())

    task = asyncio.create_task(world.regenerate())
    await asyncio.wait_for(provider.started.wait(), TIMEOUT_S)

    game = await world.game()
    assert (game.state_version, game.turn_phase, game.current_turn_id) == (
        1,
        TurnPhase.RESOLVING,
        world.turn_id,
    )
    gate.set()
    await task


async def test_regenerate_withdraws_the_old_feed_and_sends_the_rolled_back_state(world):
    world.use_scenario(
        scenario_turn(
            tool("private_message", character_id="$pc2", text="Старая тайна."),
            tool("chronicle", entry="з"),
            say("Старый рассказ."),
        ),
        scenario_turn(tool("chronicle", entry="з"), say("Новый рассказ.")),
    )
    await world.run()
    old = [m for m in await world.messages()]
    world.broadcaster.sent.clear()

    await world.regenerate()

    withdrawn = [
        (payload["item"], who)
        for payload, who in world.broadcaster.of_type("feed_item")
        if payload["item"].get("superseded")
    ]
    expected = {(m.id, "all" if m.kind == MessageKind.NARRATION else m.audience) for m in old}
    assert {(item["id"], who) for item, who in withdrawn} == expected
    assert all(set(item) == {"id", "superseded"} for item, _ in withdrawn)
    assert world.broadcaster.sent[0][0]["type"] == "phase"  # сначала «мастер думает» с откатом
    assert len(world.broadcaster.of_type("party")) >= 2  # откат и итог новой попытки


async def test_regenerate_keeps_the_actions_of_the_players_and_the_acting_fighters(world):
    world.use_scenario(
        scenario_turn(tool("chronicle", entry="з"), say("Один.")),
        scenario_turn(tool("chronicle", entry="з"), say("Два.")),
    )
    await world.act("alice", "Иду вперёд")
    async with world.db() as session:
        session.add(
            Message(
                game_id=world.game_id,
                turn_id=world.turn_id,
                audience="all",
                kind=MessageKind.ACTION,
                text="Алиса идёт вперёд",
            )
        )
        await session.commit()
    await world.run(TurnKind.COMBAT, acting=[world.alice.character_id])

    await world.regenerate()

    actions = await world.actions(world.turn_id)
    assert actions[world.alice.user_id].status == ActionStatus.SUBMITTED
    kept = [m for m in await world.messages(with_superseded=False) if m.kind == MessageKind.ACTION]
    assert [m.text for m in kept] == ["Алиса идёт вперёд"]
    first, second = await world.logs()
    assert first.request["acting"] == second.request["acting"] == [world.alice.character_id]
    assert first.request["mode"] == second.request["mode"] == "combat"  # тот же режим хода
    done, _ = (await world.turns())[:2]
    assert done.kind == TurnKind.COMBAT


@pytest.mark.parametrize("phase", [TurnPhase.COLLECTING, TurnPhase.DM_FAILED, TurnPhase.RESOLVING])
async def test_regenerate_is_refused_while_the_next_round_is_collected_or_failed(
    monkeypatch, world, phase
):
    use_provider(monkeypatch, StubProvider())
    await world.run()
    async with world.db() as session:
        await session.execute(update(Game).where(Game.id == world.game_id).values(turn_phase=phase))
        await session.commit()

    with pytest.raises(Conflict):
        await world.regenerate()

    assert (await world.game()).state_version == 2


async def test_regenerate_is_refused_once_the_next_round_has_started(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    await world.run()
    following = (await world.game()).current_turn_id
    async with world.db() as session:  # фаза ещё «обсуждение», но сам ход уже собирают
        turn = await session.get(Turn, following)
        turn.status = TurnStatus.COLLECTING
        await session.commit()

    with pytest.raises(Conflict, match="уже начат"):
        await world.regenerate()

    assert (await world.game()).state_version == 2


async def test_regenerate_is_refused_before_any_turn_is_done(world):
    with pytest.raises(Conflict, match="нечего"):
        await world.regenerate()


async def test_a_failed_regenerate_keeps_the_rollback_and_lets_the_host_retry(monkeypatch, world):
    use_provider(monkeypatch, StubProvider(narration("Старый рассказ."), RuntimeError("сбой")))
    await world.run()

    outcome = await world.regenerate()

    assert outcome.status == TurnStatus.FAILED
    game = await world.game()
    assert (game.state_version, game.turn_phase, game.current_turn_id) == (
        1,
        TurnPhase.DM_FAILED,
        world.turn_id,
    )
    retry = await world.run()
    assert retry.status == TurnStatus.FAILED  # заглушка всё ещё падает
    narrations = [m for m in await world.messages() if m.kind == MessageKind.NARRATION]
    assert [m.superseded for m in narrations] == [True]


# --- внимание (чистые функции) ---


def party_ids(state: GameState) -> list[str]:
    return list(state.party)


def test_the_spotlight_threshold_lives_in_the_module_of_the_spotlight_only():
    assert SPOTLIGHT_HINT_AFTER == 3
    defined = [
        str(path.relative_to(SERVER_SRC))
        for path in sorted(SERVER_SRC.rglob("*.py"))
        if any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SPOTLIGHT_HINT_AFTER"
                for target in node.targets
            )
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert defined == ["dm/spotlight.py"]


def test_update_spotlight_counts_submitted_focused_and_waiting_heroes(srd, adventure):
    from dnd_engine.events import (
        ConditionEvent,
        DamageEvent,
        ItemEvent,
        MoveEvent,
        PrivateMessageEvent,
        RollEvent,
    )

    heroes = {
        name: quick_character("fighter", "human", srd, name=name, concept="к", character_id=name)
        for name in ("a", "b", "c", "d", "e")
    }
    state = new_state("g", adventure, heroes)
    state.spotlight = {"a": Spotlight(submitted=2, focused=1, turns_since_focus=2)}
    roll = SeededRng(1)
    from dnd_engine.dice import roll as roll_dice

    events = [
        RollEvent(actor_id="a", label="x", roll=roll_dice("1d20", rng=roll, reason="р")),
        DamageEvent(target_id="b", amount=3, damage_type="fire", hp_after=4),
        ItemEvent(item_id="chest-key", holder_from="npc:marta", holder_to="character:c"),
        PrivateMessageEvent(audience="d", text="тайна"),
        ConditionEvent(target_id="e", condition="helped", added=False),  # конец срока — не фокус
        MoveEvent(location_id="cemetery"),
        RollEvent(actor_id="goblin-1", label="x", roll=roll_dice("1d20", rng=roll, reason="р")),
    ]

    result = update_spotlight(state, ["a", "b"], events)

    assert result["a"] == Spotlight(submitted=3, focused=2, turns_since_focus=0)
    assert result["b"] == Spotlight(submitted=1, focused=1, turns_since_focus=0)
    assert result["c"].focused == 1 and result["d"].focused == 1
    assert result["e"] == Spotlight(submitted=0, focused=0, turns_since_focus=1)
    assert set(result) == set(heroes)  # монстры в счёт не идут
    assert state.spotlight == {"a": Spotlight(submitted=2, focused=1, turns_since_focus=2)}


def test_spotlight_hints_go_from_the_longest_waiting_hero_and_start_at_the_threshold(
    srd, adventure
):
    heroes = {
        name: quick_character("fighter", "human", srd, name=name, concept="к", character_id=name)
        for name in ("a", "b", "c", "d")
    }
    state = new_state("g", adventure, heroes)
    state.spotlight = {
        "a": Spotlight(turns_since_focus=SPOTLIGHT_HINT_AFTER - 1),
        "b": Spotlight(turns_since_focus=SPOTLIGHT_HINT_AFTER),
        "c": Spotlight(turns_since_focus=SPOTLIGHT_HINT_AFTER + 4),
        "d": Spotlight(turns_since_focus=SPOTLIGHT_HINT_AFTER),
        "ghost": Spotlight(turns_since_focus=99),  # героя нет в отряде
    }

    assert spotlight_hints(state) == [
        ("c", SPOTLIGHT_HINT_AFTER + 4),
        ("b", SPOTLIGHT_HINT_AFTER),
        ("d", SPOTLIGHT_HINT_AFTER),
    ]


# --- привал: швы хода (B7.6) ---

CAMP_LINES = (
    ("alice", "Подкину дров в костёр."),
    ("boris", "Расскажу байку про сторожа."),
    ("alice", "А ты, Вера, молись потише."),
)
CAMP_MOMENT = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)


async def make_camp(world: World, lines: tuple[tuple[str, str], ...] = CAMP_LINES) -> None:
    """Отряд у костра: сцена `camp`, день 2 вечер, Алиса ранена, у Веры потрачены ячейки. Открытый
    ход — «конец привала», в нём лежат реплики героев (так их оставляет игровой поток)."""
    async with world.db() as session:
        row = await session.get(GameStateSnapshot, (world.game_id, 1))
        state = row.to_state()
        state.scene = Scene(type=SceneType.CAMP, started_turn=1)
        state.time = GameTime(day=2, part="evening")
        state.party[world.alice.character_id].hp = 1
        vera = state.party[world.vera.character_id]
        vera.spell_slots = {level: 0 for level in vera.spell_slots_max}
        row.data = GameStateSnapshot.from_state(state).data
        game = await session.get(Game, world.game_id)
        game.turn_phase, game.deadline_at = TurnPhase.CAMP, CAMP_MOMENT + timedelta(seconds=300)
        turn = await session.get(Turn, game.current_turn_id)
        turn.kind = TurnKind.CAMP_END
        for key, text in lines:
            speaker = world.players[key]
            session.add(
                Message(
                    game_id=world.game_id,
                    turn_id=turn.id,
                    audience="all",
                    kind=MessageKind.CAMP_LINE,
                    text=text,
                    data={"actor_id": speaker.character_id, "actor": speaker.name},
                )
            )
        await session.commit()


def camp_lines_of(request: TurnRequest) -> list[str]:
    """Тексты блока «реплики у костра» из контекста, который увидел мастер."""
    start = request.context.index("<<<ДАННЫЕ ИГРОКОВ: реплики у костра>>>")
    block = request.context[start:].split("\n", 1)[1].split("\n<<<КОНЕЦ", 1)[0]
    return [row["text"] for row in json.loads(block)]


async def test_a_turn_that_ends_in_the_camp_gets_the_camp_deadline_by_the_clock_of_the_deps(world):
    world.use_scenario(
        scenario_turn(
            tool("start_camp", reason="Отряд выбился из сил."),
            tool("chronicle", entry="Отряд встал на привал."),
            say("Костёр трещит, пора поговорить."),
        )
    )
    async with world.db() as session:
        await session.execute(update(Game).where(Game.id == world.game_id).values(camp_seconds=420))
        await session.commit()
    world.deps = dataclasses.replace(
        world.deps, follow_up=scene_follow_up, clock=lambda: CAMP_MOMENT
    )

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    game = await world.game()
    assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.CAMP)
    assert game.deadline_at == CAMP_MOMENT + timedelta(seconds=420)  # camp_seconds игры
    opened = (await world.turns())[-1]
    assert (opened.kind, opened.status) == (TurnKind.CAMP_END, TurnStatus.OPEN)
    assert game.current_turn_id == opened.id
    shown = world.broadcaster.of_type("phase")[-1][0]["phase"]
    assert shown["turn_phase"] == "camp"
    assert shown["deadline_at"] == "2030-05-01T12:07:00+00:00"
    assert shown["scene"]["type"] == "camp"


async def test_the_other_phases_get_no_deadline_from_the_follow_up(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    world.deps = dataclasses.replace(world.deps, follow_up=scene_follow_up)

    await world.run()

    assert (await world.game()).deadline_at is None


async def test_the_camp_end_turn_gives_the_master_the_lines_and_the_morning_after_the_long_rest(
    monkeypatch, world
):
    provider = StubProvider(narration("Утро наступило."))
    use_provider(monkeypatch, provider)
    await make_camp(world)
    alice, vera = world.alice.character_id, world.vera.character_id

    outcome = await world.run(TurnKind.CAMP_END)

    assert outcome.status == TurnStatus.DONE
    [request] = provider.requests
    assert camp_lines_of(request) == [text for _key, text in CAMP_LINES]  # по порядку, все
    # Длинный отдых уже проведён: мастер видит утро следующего дня, исследование и полное здоровье.
    assert "День 3, утро. Сцена: исследование." in request.context
    full = (await world.state()).party[alice].hp_max
    assert f"HP {full}/{full}" in request.context
    assert "HP 1/" not in request.context
    state = await world.state()
    assert state.scene.type == SceneType.EXPLORATION
    assert state.time == GameTime(day=3, part="morning")
    assert state.party[alice].hp == state.party[alice].hp_max
    assert state.party[vera].spell_slots == state.party[vera].spell_slots_max
    assert state.short_rests_since_long == 0


async def test_the_rest_is_a_system_line_and_the_next_phase_is_the_discussion(monkeypatch, world):
    use_provider(monkeypatch, StubProvider(narration("Утро наступило.")))
    world.deps = dataclasses.replace(world.deps, follow_up=scene_follow_up)
    await make_camp(world)

    await world.run(TurnKind.CAMP_END)

    game = await world.game()
    assert (game.turn_phase, game.deadline_at) == (TurnPhase.DISCUSSION, None)
    opened = (await world.turns())[-1]
    assert (opened.kind, opened.status) == (TurnKind.ROUND, TurnStatus.OPEN)
    texts = [(m.kind, m.text) for m in await world.messages() if m.kind != MessageKind.CAMP_LINE]
    assert texts == [
        (MessageKind.SYSTEM, "Длинный отдых: отряд восстановил силы."),
        (MessageKind.NARRATION, "Утро наступило."),
    ]


async def test_the_camp_lines_stay_in_the_feed_when_the_master_answers(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    await make_camp(world)

    await world.run(TurnKind.CAMP_END)

    lines = [m for m in await world.messages() if m.kind == MessageKind.CAMP_LINE]
    assert [m.superseded for m in lines] == [False, False, False]
    feed = await world.feed_of("vera")
    assert [i["text"] for i in feed if i["kind"] == "camp_line"] == [t for _k, t in CAMP_LINES]


async def test_the_long_rest_is_made_on_the_copy_so_a_failed_camp_end_turn_keeps_the_camp(
    monkeypatch, world
):
    provider = StubProvider(ProviderError("сеть упала"), narration("Утро наступило."))
    use_provider(monkeypatch, provider)
    await make_camp(world)
    alice = world.alice.character_id

    failed = await world.run(TurnKind.CAMP_END)

    assert failed.status == TurnStatus.FAILED
    assert await world.snapshot_versions() == [1]  # нового снимка нет
    kept = await world.state()
    assert kept.scene.type == SceneType.CAMP and kept.party[alice].hp == 1
    assert (await world.game()).turn_phase == TurnPhase.DM_FAILED
    notes = [m for m in await world.messages() if m.kind != MessageKind.CAMP_LINE]
    assert [m.kind for m in notes] == [MessageKind.SYSTEM]  # только «Мастер не справился»

    retried = await world.run(TurnKind.CAMP_END)  # «Повторить»

    assert retried.status == TurnStatus.DONE
    rested = await world.state()
    assert rested.party[alice].hp == rested.party[alice].hp_max
    assert await world.snapshot_versions() == [1, 2]  # отдых записан один раз
    assert camp_lines_of(provider.requests[-1]) == [text for _key, text in CAMP_LINES]


async def test_regenerating_the_camp_end_turn_rolls_the_rest_back_and_makes_it_again(
    monkeypatch, world
):
    provider = StubProvider(narration("Первое утро."), narration("Второе утро."))
    use_provider(monkeypatch, provider)
    await make_camp(world)
    alice = world.alice.character_id
    await world.run(TurnKind.CAMP_END)
    first_rest = [m.id for m in await world.messages() if "Длинный отдых" in m.text]

    outcome = await world.regenerate()

    assert outcome.status == TurnStatus.DONE
    visible = await world.messages(with_superseded=False)
    [shown] = [m for m in visible if m.kind == MessageKind.NARRATION]
    assert shown.text == "Второе утро."
    rests = [m for m in visible if "Длинный отдых" in m.text]
    assert len(rests) == 1 and rests[0].id not in first_rest  # отдых один: старая строка снята
    state = await world.state()
    assert state.party[alice].hp == state.party[alice].hp_max
    assert state.time == GameTime(day=3, part="morning")  # день не прибавился дважды
    assert camp_lines_of(provider.requests[-1]) == [text for _key, text in CAMP_LINES]
    kept = [m for m in await world.messages() if m.kind == MessageKind.CAMP_LINE]
    assert [m.superseded for m in kept] == [False, False, False]  # реплики — вход хода, остаются
    assert (await world.state(1)).scene.type == SceneType.CAMP  # снимок «до» не тронут


async def test_the_camp_end_turn_cannot_be_started_when_there_is_no_camp(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    before = [(t.id, t.kind, t.status) for t in await world.turns()]

    with pytest.raises(Conflict, match="привал"):
        await world.run(TurnKind.CAMP_END)

    assert provider.requests == []
    assert (await world.game()).turn_phase == TurnPhase.DISCUSSION
    assert [(t.id, t.kind, t.status) for t in await world.turns()] == before


async def test_a_round_cannot_be_resolved_while_the_camp_goes_and_the_camp_stays(
    monkeypatch, world
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await make_camp(world)

    with pytest.raises(Conflict, match="привал"):
        await world.run(TurnKind.ROUND)

    assert provider.requests == []
    game = await world.game()
    assert game.turn_phase == TurnPhase.CAMP and game.deadline_at is not None
    assert (await world.turns())[0].kind == TurnKind.CAMP_END  # ход не стал раундом


async def test_a_line_that_was_withdrawn_does_not_reach_the_master(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await make_camp(world)
    async with world.db() as session:
        first = await session.scalar(
            select(Message).where(Message.kind == MessageKind.CAMP_LINE).order_by(Message.id)
        )
        first.superseded = True
        await session.commit()

    await world.run(TurnKind.CAMP_END)

    assert camp_lines_of(provider.requests[0]) == [text for _key, text in CAMP_LINES[1:]]


async def test_the_long_rest_of_the_server_is_a_separate_entry_of_the_journal(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await make_camp(world)

    await world.run(TurnKind.CAMP_END)

    [log] = await world.logs()
    [rest] = [call for call in log.trace if call["name"] == "server_end_camp"]
    assert log.trace[0]["name"] == "server_end_camp"  # отдых стоит первым: он был до модели
    assert rest["is_error"] is False
    assert json.loads(rest["result"])["events"] == [{"rest": "long"}, {"scene": "exploration"}]


async def test_the_next_round_remembers_what_the_heroes_said_at_the_fire(monkeypatch, world):
    provider = StubProvider(narration("Утро."), narration("День."))
    use_provider(monkeypatch, provider)
    await make_camp(world)
    await world.run(TurnKind.CAMP_END)

    await world.run(TurnKind.ROUND)

    for _key, text in CAMP_LINES:
        assert text in provider.requests[-1].context  # реплики лежат в «прошлых ходах»


async def test_the_camp_end_turn_with_no_lines_still_gets_the_rest_and_an_empty_block(
    monkeypatch, world
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    await make_camp(world, lines=())

    outcome = await world.run(TurnKind.CAMP_END)

    assert outcome.status == TurnStatus.DONE
    assert camp_lines_of(provider.requests[0]) == []
    assert (await world.state()).scene.type == SceneType.EXPLORATION


async def test_a_scene_that_is_not_the_camp_gets_no_rest_instead_of_a_stuck_game(
    monkeypatch, world, caplog
):
    # Игра в фазе camp, а сцена другая — ошибка данных. Ход не должен падать при каждом повторе:
    # отдыха нет, рассказ идёт, игра идёт дальше.
    use_provider(monkeypatch, StubProvider())
    await make_camp(world)
    async with world.db() as session:
        row = await session.get(GameStateSnapshot, (world.game_id, 1))
        state = row.to_state()
        state.scene = Scene(type=SceneType.EXPLORATION)
        row.data = GameStateSnapshot.from_state(state).data
        await session.commit()

    with caplog.at_level(logging.WARNING):
        outcome = await world.run(TurnKind.CAMP_END)

    assert outcome.status == TurnStatus.DONE
    assert (await world.state()).party[world.alice.character_id].hp == 1  # отдыха не было
    assert any("привал" in record.getMessage().lower() for record in caplog.records)


# --- приложение собирает зависимости хода ---


def test_deps_are_taken_from_the_parts_of_the_app(settings):
    app = create_app(settings)

    deps = Deps.from_app(app)

    assert deps.sessionmaker is app.state.sessionmaker
    assert deps.locks is app.state.game_locks
    assert deps.broadcaster is app.state.broadcaster
    assert deps.settings is app.state.settings
    assert deps.turn_tokens is app.state.turn_tokens
    assert deps.rng is app.state.rng
    assert deps.providers is Deps.from_app(app).providers  # один кеш провайдеров на приложение
    # Сжатие летописи идёт после хода в том же наборе задач, что и разбор мастера: его останавливает
    # остановка сервера. Учёт «одно сжатие на игру» тоже один на приложение.
    assert deps.tasks is app.state.turn_tasks
    assert deps.compressions is Deps.from_app(app).compressions
    assert deps.compressions is app.state.chronicle_compressions
    # Срок привала считается по часам планировщика: отсчёт и срок в базе идут по одним часам.
    assert deps.clock == app.state.scheduler.now
    # Приложение само кладёт выбор следующей фазы по сцене (B7.4): B7.5 и B7.6 заменяют его ветки.
    assert app.state.turn_follow_up is scene_follow_up
    assert deps.follow_up is scene_follow_up
    assert deps.providers is app.state.game_providers
    app.state.turn_follow_up = lambda _state: (TurnPhase.CAMP, TurnKind.ROUND)
    assert Deps.from_app(app).follow_up is app.state.turn_follow_up


def test_a_provider_is_built_once_per_game(monkeypatch, settings):
    factory = use_provider(monkeypatch, object())
    cache = GameProviders()
    kwargs = {"provider": None, "model": None, "pc_ids": ("a",), "turn_tokens": None}

    first = cache.of("g1", settings, **kwargs)
    again = cache.of("g1", settings, **kwargs)
    other = cache.of("g2", settings, **kwargs)

    assert first is again is other  # заглушка одна, но собрана она по разу на игру
    assert len(factory.calls) == 2


def test_a_config_error_is_not_cached_and_the_next_call_builds_again(monkeypatch, settings):
    attempts: list[int] = []

    def flaky(_settings: Settings, **_kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConfigError("не задан ANTHROPIC_API_KEY")
        return object()

    monkeypatch.setattr(provider_module, "make_provider", flaky)
    cache = GameProviders()
    kwargs = {"provider": None, "model": None, "pc_ids": (), "turn_tokens": None}

    with pytest.raises(ConfigError):
        cache.of("g1", settings, **kwargs)

    assert cache.of("g1", settings, **kwargs) is cache.of("g1", settings, **kwargs)
    assert len(attempts) == 2


# --- швы для сбора ходов (B7.4) ---


async def test_on_reserved_is_called_after_the_reserve_and_before_the_model(monkeypatch, world):
    order: list[str] = []

    class Ordered(StubProvider):
        async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
            order.append("model")
            return await super().run_turn(req, tools)

    use_provider(monkeypatch, Ordered())

    outcome = await world.run(on_reserved=lambda: order.append("reserved"))

    assert outcome.status == TurnStatus.DONE
    assert order == ["reserved", "model"]


async def test_a_failing_on_reserved_does_not_leave_the_turn_reserved_without_the_model(
    monkeypatch, world
):
    use_provider(monkeypatch, StubProvider())

    def broken() -> None:
        raise RuntimeError("оповещение сломалось")

    outcome = await world.run(on_reserved=broken)

    assert outcome.status == TurnStatus.DONE
    assert (await world.game()).turn_phase == TurnPhase.DISCUSSION


async def test_on_reserved_sees_the_game_in_resolving_while_the_model_thinks(monkeypatch, world):
    gate = asyncio.Event()
    provider = StubProvider(gate=gate)
    use_provider(monkeypatch, provider)
    reserved = asyncio.Event()
    task = asyncio.create_task(world.run(on_reserved=reserved.set))

    await asyncio.wait_for(reserved.wait(), TIMEOUT_S)

    assert (await world.game()).turn_phase == TurnPhase.RESOLVING
    gate.set()
    assert (await task).status == TurnStatus.DONE


async def test_on_reserved_is_not_called_when_the_turn_is_refused(world):
    called: list[str] = []
    async with world.db() as session:
        await session.execute(
            update(Game).where(Game.id == world.game_id).values(phase=GamePhase.LOBBY)
        )
        await session.commit()

    with pytest.raises(Conflict):
        await world.run(on_reserved=lambda: called.append("reserved"))
    with pytest.raises(NotFound):
        await run_dm_turn(
            "нет-такой-игры",
            mode=TurnKind.ROUND,
            deps=world.deps,
            on_reserved=lambda: called.append("reserved"),
        )

    assert called == []


async def test_regenerate_calls_on_reserved_after_the_rollback_too(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    await world.run()
    called: list[str] = []

    outcome = await regenerate_last_turn(
        world.game_id, deps=world.deps, on_reserved=lambda: called.append("reserved")
    )

    assert outcome.status == TurnStatus.DONE
    assert called == ["reserved"]


async def test_the_pacing_of_the_context_counts_the_time_after_the_clock_started(
    monkeypatch, world
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        game.active_seconds = 3600
        game.clock_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()

    await world.run()

    assert "Темп: прошло 1 ч 10 мин из" in provider.requests[0].context


async def test_the_pacing_of_the_context_goes_by_the_clock_of_the_deps_like_the_camp_deadline(
    monkeypatch, world
):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    moment = datetime.now(UTC) + timedelta(days=3)  # часы приложения (планировщика) ушли вперёд
    world.deps = dataclasses.replace(world.deps, clock=lambda: moment)
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        game.active_seconds = 3600
        game.clock_at = moment - timedelta(minutes=10)
        await session.commit()

    await world.run()

    assert "Темп: прошло 1 ч 10 мин из" in provider.requests[0].context


async def test_the_pacing_of_a_game_without_a_clock_is_the_saved_active_time(monkeypatch, world):
    provider = StubProvider()
    use_provider(monkeypatch, provider)
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        game.active_seconds, game.clock_at = 3600, None
        await session.commit()

    await world.run()

    assert "Темп: прошло 1 ч из" in provider.requests[0].context


# --- дневник отряда (блок B8, дополнение контракта §5–§6) ---
# Записи дневника пишет движок на рабочей копии хода; сервер сохраняет их коммитом хода и после него
# шлёт `journal_entry` каждой новой или дополненной записи её адресату. Откат хода шлёт каждому
# игроку его дневник целиком (`journal`). Состояние мира теста записано без дневника — как у игры,
# начатой до блока B8: дневник открывает первый же ход мастера.

LEARNED = "Завсегдатаи держатся Марты и не любят чужаков."
MORE = "Они же первыми заметили, что Фабиан исчез."
SECRET_LEARNED = "ЛИЧНОЕ-boris: Фабиан задолжал половине деревни."


def entry_frames(world: World) -> list[tuple[str, str, int]]:
    """Кадры `journal_entry`: (id записи, адресат, ревизия) по порядку рассылки."""
    return [
        (payload["entry"]["id"], who, payload["entry"]["revision"])
        for payload, who in world.broadcaster.of_type("journal_entry")
    ]


async def journal_of(world: World, key: str) -> list[dict[str, Any]]:
    """Дневник в снимке, который игрок получает при подключении."""
    async with world.db() as session:
        user = await session.get(User, world.players[key].user_id)
        return (await snapshot_for(session, user, world.game_id, srd=world.deps.srd))["journal"]


async def test_an_old_game_saves_the_starting_journal_with_the_first_master_turn(world, adventure):
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Рассказ.")))
    await world.act("alice", "Осматриваюсь")
    before = await world.state()

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    assert before.journal == ()
    saved = await world.state()
    assert saved.journal == open_journal(before, adventure).journal
    assert {"intro", f"location:{adventure.start_location}"} <= {e.ref for e in saved.journal}
    # Стартовые записи игроки уже видят в снимке: событиями они не повторяются.
    assert entry_frames(world) == []
    assert await journal_of(world, "vera") == [
        entry.model_dump(mode="json") for entry in saved.journal
    ]


async def test_an_entry_of_the_master_reaches_every_player_as_an_event_after_the_commit(world):
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )
    await world.act("alice", "Расспрашиваю завсегдатаев")

    await world.run()

    state = await world.state()
    (entry,) = [e for e in state.journal if e.ref == "faction:regulars"]
    assert (entry.audience, entry.revision, entry.turn) == ("all", 1, 1)
    assert LEARNED in entry.text
    frames = world.broadcaster.of_type("journal_entry")
    assert frames == [({"type": "journal_entry", "entry": entry.model_dump(mode="json")}, "all")]
    index = next(
        i for i, (p, _) in enumerate(world.broadcaster.sent) if p["type"] == "journal_entry"
    )
    assert world.broadcaster.versions_at_send[index] == state.version  # рассылка после коммита
    for key in world.players:
        assert entry.model_dump(mode="json") in await journal_of(world, key)


async def test_a_repeated_entry_is_added_to_and_never_doubled(world):
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        ),
        scenario_turn(
            tool("journal_add", ref="faction:regulars"),
            tool("journal_add", ref="faction:regulars", note=MORE),
            tool("chronicle", entry="з"),
            say("Ещё рассказ."),
        ),
    )
    await world.act("alice", "Расспрашиваю завсегдатаев")
    await world.run()
    world.broadcaster.sent.clear()
    await world.act("alice", "Спрашиваю про Фабиана")

    await world.run()

    state = await world.state()
    (entry,) = [e for e in state.journal if e.ref == "faction:regulars"]
    assert entry.revision == 2 and entry.text.endswith(f"{LEARNED}\n\n{MORE}")
    assert entry_frames(world) == [("faction:regulars@all", "all", 2)]
    for key in world.players:
        journal = await journal_of(world, key)
        assert [e["ref"] for e in journal].count("faction:regulars") == 1


async def test_a_secret_finding_of_one_hero_reaches_only_his_player(world):
    # Тайный ход Бориса: мастер записывает, что тот узнал (`journal_add` с `for_hero`), и находку
    # улики (`reveal` с `for_hero`). Обе записи — только Борису: в снимке и в кадрах.
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="npc:fabian", note=SECRET_LEARNED, for_hero="$pc2"),
            tool("reveal", ref="clue:clue-marta", for_hero="$pc2"),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )
    await world.act("boris", "Тайно расспрашиваю Марту о Фабиане", secret=True)
    await world.act("alice", "Заказываю ужин")

    await world.run()

    boris = world.boris
    personal = [e for e in (await world.state()).journal if e.audience == boris.character_id]
    assert {e.ref for e in personal} == {"npc:fabian", "clue:clue-marta"}
    assert entry_frames(world) == [
        (f"npc:fabian@{boris.character_id}", boris.user_id, 1),
        (f"clue:clue-marta@{boris.character_id}", boris.user_id, 1),
    ]
    mine = await journal_of(world, "boris")
    assert {e["ref"] for e in mine} >= {"npc:fabian", "clue:clue-marta"}
    for key in ("alice", "vera"):
        journal = await journal_of(world, key)
        assert {e["audience"] for e in journal} == {"all"}
        assert "clue:clue-marta" not in {e["ref"] for e in journal}
        assert SECRET_LEARNED not in dump(journal)
    to_others = [p for p, who in world.broadcaster.sent if who != boris.user_id]
    assert SECRET_LEARNED not in dump(to_others) and "clue-marta" not in dump(to_others)


async def test_the_same_finding_in_an_open_turn_is_for_the_whole_party(world):
    world.use_scenario(
        scenario_turn(
            tool("reveal", ref="clue:clue-marta", for_hero="$pc2"),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )
    await world.act("boris", "Расспрашиваю Марту о Фабиане")

    await world.run()

    assert entry_frames(world) == [("clue:clue-marta@all", "all", 1)]


async def test_a_finding_of_an_open_hero_in_a_round_with_a_secret_turn_stays_common(world):
    # Дополнение §12: тайные авторы — это авторы всего раунда, а не вызова. Находка открытого героя
    # с `for_hero` остаётся общей, даже если кто-то в этом раунде ходит тайно.
    world.use_scenario(
        scenario_turn(
            tool("reveal", ref="clue:clue-marta", for_hero="$pc1"),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )
    await world.act("alice", "Расспрашиваю Марту о Фабиане")
    await world.act("boris", "Тайно осматриваю задний двор", secret=True)

    await world.run()

    assert entry_frames(world) == [("clue:clue-marta@all", "all", 1)]


async def test_a_finding_without_its_hero_in_a_round_with_a_secret_turn_is_refused_and_kept_out(
    world,
):
    # Без `for_hero` движок отказывает до изменений: улика не найдена и в дневник не попала, а ход
    # мастера всё равно удался (ошибка инструмента — ответ модели, а не сбой хода).
    world.use_scenario(
        scenario_turn(
            tool("reveal", ref="clue:clue-marta"),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )
    await world.act("boris", "Тайно расспрашиваю Марту", secret=True)

    outcome = await world.run()

    assert outcome.status == TurnStatus.DONE
    state = await world.state()
    assert "clue:clue-marta" not in state.world.revealed
    assert "clue:clue-marta" not in {entry.ref for entry in state.journal}
    assert entry_frames(world) == []
    (call,) = [c for c in (await world.logs())[-1].trace if c["name"] == "reveal"]
    assert call["is_error"] and "for_hero" in call["result"]


async def test_a_regenerated_turn_takes_its_entry_back_from_every_player(world):
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("journal_add", ref="npc:fabian", note=SECRET_LEARNED, for_hero="$pc2"),
            tool("chronicle", entry="з"),
            say("Старый рассказ."),
        ),
        scenario_turn(tool("chronicle", entry="з"), say("Новый рассказ.")),
    )
    await world.act("boris", "Тайно расспрашиваю Марту", secret=True)
    await world.run()
    assert len(await journal_of(world, "boris")) == len(await journal_of(world, "alice")) + 1
    world.broadcaster.sent.clear()

    outcome = await world.regenerate()

    assert outcome.status == TurnStatus.DONE
    state = await world.state()
    assert {"faction:regulars", "npc:fabian"}.isdisjoint(e.ref for e in state.journal)
    blocks = world.broadcaster.of_type("journal")
    assert sorted(who for _, who in blocks) == sorted(p.user_id for p in world.players.values())
    expected = [entry.model_dump(mode="json") for entry in state.journal]
    assert all(payload["entries"] == expected for payload, _ in blocks)
    assert entry_frames(world) == []  # новая попытка записей не добавила
    for key in world.players:
        assert await journal_of(world, key) == expected


async def test_the_journal_block_of_the_rollback_comes_with_the_rolled_back_state(world):
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("chronicle", entry="з"),
            say("Старый рассказ."),
        ),
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=MORE),
            tool("chronicle", entry="з"),
            say("Новый рассказ."),
        ),
    )
    await world.run()
    world.broadcaster.sent.clear()

    await world.regenerate()

    kinds = [payload["type"] for payload, _ in world.broadcaster.sent]
    assert kinds.index("journal") < kinds.index("journal_entry")
    for payload, _ in world.broadcaster.of_type("journal"):
        assert "faction:regulars" not in {e["ref"] for e in payload["entries"]}
    assert entry_frames(world) == [("faction:regulars@all", "all", 1)]
    (entry,) = [e for e in (await world.state()).journal if e.ref == "faction:regulars"]
    assert MORE in entry.text and LEARNED not in entry.text


async def test_the_context_of_the_master_lists_the_journal_by_refs_without_texts(world):
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("journal_add", ref="npc:fabian", note=SECRET_LEARNED, for_hero="$pc2"),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        ),
        scenario_turn(tool("chronicle", entry="з"), say("Ещё рассказ.")),
    )
    await world.act("boris", "Тайно расспрашиваю Марту", secret=True)
    await world.run()
    await world.act("alice", "Идём дальше")

    await world.run()

    context = (await world.logs())[-1].request["context"]
    (line,) = [line for line in context.splitlines() if line.startswith("Дневник отряда:")]
    assert "faction:regulars" in line
    assert f"npc:fabian (лично {world.boris.character_id})" in line
    assert LEARNED not in context and SECRET_LEARNED not in context
    assert world.adventure.journal_intro.split()[0] not in line


# --- дневник: сбой хода, автоповтор и «Повторить» (финальное покрытие блока B8) ---
# Попытка хода пишет дневник на своей рабочей копии. Неудачная попытка копию выбрасывает: в базе
# дневник прежний и кадров дневника нет, а следующая попытка начинает с чистой копии — запись не
# накапливается от попытки к попытке.


async def test_failed_attempts_leave_the_journal_alone_and_the_repeat_writes_the_entry_once(
    world, adventure
):
    # Две попытки без рассказа (автоповтор), каждая успела вызвать `journal_add`; затем «Повторить».
    def silent() -> dict[str, Any]:
        return scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED), tool("chronicle", entry="з")
        )

    world.use_scenario(
        silent(),
        silent(),
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        ),
    )
    await world.act("alice", "Расспрашиваю завсегдатаев")
    before = await world.state()

    failed = await world.run()

    assert (failed.status, failed.attempts) == (TurnStatus.FAILED, 2)
    assert await world.snapshot_versions() == [1]
    assert (await world.state()).journal == before.journal == ()  # старая игра: дневника нет
    sent = {payload["type"] for payload, _ in world.broadcaster.sent}
    assert not sent & {"journal_entry", "journal"}
    for key in world.players:  # снимок по-прежнему открывает стартовый дневник только в памяти
        assert "faction:regulars" not in {e["ref"] for e in await journal_of(world, key)}

    repeated = await world.run()

    assert repeated.status == TurnStatus.DONE
    state = await world.state()
    (entry,) = [e for e in state.journal if e.ref == "faction:regulars"]
    assert (entry.revision, entry.text.count(LEARNED)) == (1, 1)
    assert entry_frames(world) == [("faction:regulars@all", "all", 1)]
    starting = open_journal(before, adventure).journal
    assert state.journal == (*starting, entry)  # стартовый дневник сохранён тем же коммитом


async def test_an_exception_after_a_personal_entry_leaves_no_trace_of_it_for_the_players(
    monkeypatch, world
):
    # Модель записала тайное знание Бориса и упала: ход не удался, запись выброшена вместе с
    # копией, и ни в кадрах, ни в снимке её нет ни у кого, даже у самого Бориса.
    boris = world.boris.character_id
    call = ("journal_add", {"ref": "npc:fabian", "note": SECRET_LEARNED, "for_hero": boris})
    use_provider(monkeypatch, StubProvider(ProviderError("упал адаптер"), calls=(call,)))
    await world.act("boris", "Тайно расспрашиваю Марту", secret=True)

    outcome = await world.run()

    await failed_turn_leaves_the_game_as_it_was(world, outcome)
    (log,) = await world.logs()
    # Вызов был и удался: запись успела лечь в рабочую копию хода.
    assert [(c["name"], c["is_error"]) for c in log.trace] == [("journal_add", False)]
    assert (await world.state()).journal == ()
    assert SECRET_LEARNED not in dump(world.broadcaster.sent)
    for key in world.players:
        assert SECRET_LEARNED not in dump(await journal_of(world, key))
        assert SECRET_LEARNED not in dump(await world.feed_of(key))


async def test_the_first_turn_of_an_old_game_shows_the_master_its_starting_journal(
    world, adventure
):
    # Старой игре дневник открывает резерв хода: мастер уже в первом ходе видит, что записано со
    # старта, и не станет записывать это заново.
    world.use_scenario(scenario_turn(tool("chronicle", entry="з"), say("Рассказ.")))
    await world.act("alice", "Осматриваюсь")
    before = await world.state()

    await world.run()

    context = (await world.logs())[-1].request["context"]
    (line,) = [line for line in context.splitlines() if line.startswith("Дневник отряда:")]
    refs = [entry.ref for entry in open_journal(before, adventure).journal]
    assert before.journal == () and refs
    assert line == f"Дневник отряда: {', '.join(refs)}."


# --- номер раунда игроков (дополнение контракта B8 §11, п. 5–6) ---
# «Узнали в раунде N» в дневнике — номер раунда игроков (`GameState.party_round`), а не номер хода
# мастера. Его поднимает только ход вида «раунд игроков», считая от базового состояния хода:
# «Перегенерировать» и «Повторить» дают тот же номер. Открытие, ходы боя и конец привала номер не
# меняют.


async def set_party_round(world: World, number: int) -> None:
    """Текущее состояние игры теста — в раунде игроков `number`."""
    async with world.db() as session:
        game = await session.get(Game, world.game_id)
        row = await session.get(GameStateSnapshot, (world.game_id, game.state_version))
        state = row.to_state()
        state.party_round = number
        row.data = GameStateSnapshot.from_state(state).data
        await session.commit()


def quiet_round(text: str) -> dict[str, Any]:
    return scenario_turn(tool("chronicle", entry="з"), say(text))


async def test_a_round_of_the_players_counts_its_number_from_the_base_state(world):
    world.use_scenario(quiet_round("Первый."), quiet_round("Второй."), quiet_round("Заново."))

    await world.run()
    first = await world.state()
    await world.run()
    second = await world.state()
    await world.regenerate()

    assert (first.party_round, second.party_round) == (1, 2)
    assert (await world.state()).party_round == 2  # перегенерация считает от той же базы


async def test_a_retried_round_gets_the_same_number(monkeypatch, world):
    use_provider(monkeypatch, StubProvider(ProviderError("сеть упала"), narration("Рассказ.")))

    failed = await world.run()
    kept = await world.state()
    retried = await world.run()  # «Повторить»

    assert failed.status == TurnStatus.FAILED and kept.party_round == 0
    assert retried.status == TurnStatus.DONE
    assert (await world.state()).party_round == 1


async def test_an_entry_of_a_round_gets_the_number_of_that_round(world):
    await set_party_round(world, 4)
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="faction:regulars", note=LEARNED),
            tool("chronicle", entry="з"),
            say("Рассказ."),
        )
    )

    await world.run()

    (entry,) = [e for e in (await world.state()).journal if e.ref == "faction:regulars"]
    assert entry.turn == 5


async def test_a_combat_turn_keeps_the_number_of_the_round(monkeypatch, world):
    use_provider(monkeypatch, StubProvider())
    await set_party_round(world, 4)

    await world.run(TurnKind.COMBAT, acting=[world.alice.character_id])

    assert (await world.state()).party_round == 4


async def test_the_camp_end_keeps_the_number_of_the_round(monkeypatch, world):
    use_provider(monkeypatch, StubProvider(narration("Утро.")))
    await make_camp(world)
    await set_party_round(world, 4)

    await world.run(TurnKind.CAMP_END)

    assert (await world.state()).party_round == 4


async def test_the_opening_keeps_the_round_of_the_start(opening_world):
    opening_world.use_scenario(quiet_round("Вы в деревне."))

    await opening_world.run(TurnKind.OPENING)

    assert (await opening_world.state()).party_round == 0


# --- дневник после открытия игры (дополнение контракта B8 §11, п. 7) ---


async def test_after_the_opening_every_player_gets_the_whole_journal(opening_world):
    # Сокеты, открытые ещё в лобби, получили снимок без дневника: блок `journal` их догоняет.
    world = opening_world
    world.use_scenario(quiet_round("Вы в деревне."))

    await world.run(TurnKind.OPENING)

    state = await world.state()
    expected = [entry.model_dump(mode="json") for entry in state.journal]
    blocks = world.broadcaster.of_type("journal")
    assert expected
    assert sorted(who for _, who in blocks) == sorted(p.user_id for p in world.players.values())
    assert all(payload["entries"] == expected for payload, _ in blocks)
    assert world.broadcaster.of_type("journal_entry") == []
    sent = [payload["type"] for payload, _ in world.broadcaster.sent]
    assert sent.index("journal") > sent.index("character") and sent[-1] == "player_status"


async def test_a_personal_entry_of_the_opening_reaches_only_its_player_in_the_block(opening_world):
    world = opening_world
    world.use_scenario(
        scenario_turn(
            tool("journal_add", ref="npc:fabian", note=SECRET_LEARNED, for_hero="$pc2"),
            tool("chronicle", entry="з"),
            say("Вы в деревне."),
        )
    )

    await world.run(TurnKind.OPENING)

    blocks = {who: payload["entries"] for payload, who in world.broadcaster.of_type("journal")}
    assert SECRET_LEARNED in dump(blocks[world.boris.user_id])
    for key in ("alice", "vera"):
        assert SECRET_LEARNED not in dump(blocks[world.players[key].user_id])
