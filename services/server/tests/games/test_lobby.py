"""Игры и лобби (план B6.3): создание, вход по коду, готовность, старт.

Приложение работает на настоящей SQLite после миграций (`db`, `settings` из `tests/conftest.py`),
с выполненным стартом (`lifespan` грузит каталог приключений из временной копии `demo-crypt`).
Рассылка по сокетам и запуск вплетения подменены регистраторами: хаб появится в B6.5, мастер —
в B7.8. Персонажей создаёт B6.4, поэтому тесты вставляют строки `characters` напрямую. Настоящие
модели не вызываются: фабрика провайдеров только проверяет настройки, а ключ в тестах — выдуманная
строка.
"""

import asyncio
import json
import logging
import re
import shutil
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from lore_trap import trap_lore
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import adventure_from_data, dump_adventure, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import load_srd
from dnd_engine.creation import quick_character
from dnd_server.adventures import load_catalog
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.models import (
    Game,
    GamePhase,
    GamePlayer,
    PlayerCharacter,
    User,
)
from dnd_server.games import service

# tests/games/test_lobby.py -> games -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "basic_round.yaml"
SESSION_SECRET = "test-session-secret-0123456789-abcdefghij"
CREATOR_TG_ID = 700
ANTHROPIC_TEST_KEY = "sk-ant-test-not-a-real-key"
CODE_FORMAT = re.compile(r"[A-Z0-9]{6}")
# Решение владельца B6.security: 10 неудачных попыток войти по коду за 10 минут.
MAX_FAILED_JOINS = 10
FAILED_JOIN_WINDOW_S = 600
GAME_FIELDS = {
    "id",
    "code",
    "adventure_id",
    "adventure_title",
    "phase",
    "turn_phase",
    "provider",
    "model",
    "collect_seconds",
    "hurry_seconds",
    "camp_seconds",
    "short_rest_seconds",
    "profanity",
    "players",
}
PLAYER_FIELDS = {"user_id", "name", "is_host", "ready", "has_character"}


# --- приложение, регистраторы, игроки ---


class RecordingBroadcaster:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
        self.sent.append((game_id, payload, audience))


class RecordingHooks:
    """Запоминает запуски игры и то, какой была фаза игры в базе в момент вызова."""

    def __init__(self, db: async_sessionmaker[AsyncSession]) -> None:
        self._db = db
        self.started: list[str] = []
        self.phase_seen: list[GamePhase] = []

    async def on_game_started(self, game_id: str) -> None:
        async with self._db() as session:
            game = await session.get(Game, game_id)
        self.started.append(game_id)
        self.phase_seen.append(game.phase)


class FailingBroadcaster:
    async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
        raise RuntimeError("хаб недоступен")


class FailingHooks:
    async def on_game_started(self, game_id: str) -> None:
        raise RuntimeError("мастер недоступен")


@dataclass
class Lobby:
    app: Any
    http: httpx.AsyncClient
    broadcaster: RecordingBroadcaster
    hooks: RecordingHooks
    settings: Settings
    db: async_sessionmaker[AsyncSession]
    players_added: int = field(default=0)


class Player(NamedTuple):
    id: str
    name: str
    headers: dict[str, str]


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    shutil.copytree(DEMO_ADVENTURE_DIR, directory / "demo-crypt")
    return directory


@pytest.fixture
def settings(settings: Settings, content_dir: Path) -> Settings:
    """Dev-настройки из conftest: каталог приключений, секрет сессий, сценарий `fake`, создатель."""
    return settings.model_copy(
        update={
            "content_dir": content_dir,
            "session_secret": SecretStr(SESSION_SECRET),
            "fake_llm_scenario": SCENARIO,
            "game_creator_tg_ids": (CREATOR_TG_ID,),
        }
    )


@asynccontextmanager
async def start_lobby(
    settings: Settings, db: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Lobby]:
    app = create_app(settings)
    broadcaster = RecordingBroadcaster()
    hooks = RecordingHooks(db)
    # Хаб (B6.5) и мастер (B7.8) подменят заглушки по умолчанию так же: через `app.state`.
    app.state.broadcaster = broadcaster
    app.state.game_hooks = hooks
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield Lobby(app, http, broadcaster, hooks, settings, db)


@pytest.fixture
async def lobby(settings: Settings, db: async_sessionmaker[AsyncSession]) -> AsyncIterator[Lobby]:
    async with start_lobby(settings, db) as running:
        yield running


async def add_player(
    db: async_sessionmaker[AsyncSession],
    name: str,
    *,
    dev_login: str | None = None,
    tg_id: int | None = None,
) -> Player:
    async with db() as session:
        user = User(dev_login=dev_login, tg_id=tg_id, name=name)
        session.add(user)
        await session.commit()
    token = SessionSigner(SESSION_SECRET).issue(user.id)
    return Player(user.id, name, {"Authorization": f"Bearer {token}"})


async def add_host(lobby: Lobby) -> Player:
    """Игрок, которому можно создавать игры: dev-логин (в dev-режиме)."""
    return await add_player(lobby.db, "Хост", dev_login="dev-1")


async def add_friend(lobby: Lobby, name: str | None = None) -> Player:
    """Обычный игрок: Telegram-id вне списка создателей."""
    lobby.players_added += 1
    number = lobby.players_added
    return await add_player(lobby.db, name or f"Игрок {number}", tg_id=1000 + number)


async def create_game(lobby: Lobby, host: Player, **body: Any) -> httpx.Response:
    return await lobby.http.post(
        "/api/games", headers=host.headers, json={"adventure_id": "demo-crypt", **body}
    )


async def new_game(lobby: Lobby, host: Player, **body: Any) -> dict[str, Any]:
    response = await create_game(lobby, host, **body)
    assert response.status_code == 201, response.text
    return response.json()


async def join(lobby: Lobby, player: Player, code: str) -> httpx.Response:
    return await lobby.http.post("/api/games/join", headers=player.headers, json={"code": code})


async def joined_friend(lobby: Lobby, game: dict[str, Any]) -> Player:
    friend = await add_friend(lobby)
    response = await join(lobby, friend, game["code"])
    assert response.status_code == 200, response.text
    return friend


async def give_character(
    db: async_sessionmaker[AsyncSession], game_id: str, player: Player
) -> None:
    """Вставляет персонажа игроку напрямую: создание персонажей — B6.4."""
    hero = quick_character(
        "fighter",
        "human",
        load_srd(),
        name=player.name,
        concept="Бывший стражник",
        character_id=f"pc-{player.id}",
    )
    async with db() as session:
        session.add(
            PlayerCharacter(
                id=hero.id, game_id=game_id, user_id=player.id, data=hero.model_dump(mode="json")
            )
        )
        await session.flush()
        (await session.get(GamePlayer, (game_id, player.id))).character_id = hero.id
        await session.commit()


async def mark_ready(lobby: Lobby, game_id: str, player: Player, ready: bool = True):
    return await lobby.http.post(
        f"/api/games/{game_id}/ready", headers=player.headers, json={"ready": ready}
    )


async def ready_with_character(lobby: Lobby, game_id: str, player: Player) -> None:
    await give_character(lobby.db, game_id, player)
    response = await mark_ready(lobby, game_id, player)
    assert response.status_code == 200, response.text


async def ready_lobby(
    lobby: Lobby, friends: int = 2
) -> tuple[dict[str, Any], Player, list[Player]]:
    """Игра, где хост и `friends` игроков имеют персонажей и готовы."""
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    others = [await joined_friend(lobby, game) for _ in range(friends)]
    for player in (host, *others):
        await ready_with_character(lobby, game["id"], player)
    return game, host, others


async def stored_game(db: async_sessionmaker[AsyncSession], game_id: str) -> Game:
    async with db() as session:
        return await session.get(Game, game_id)


async def stored_players(db: async_sessionmaker[AsyncSession], game_id: str) -> list[GamePlayer]:
    async with db() as session:
        rows = await session.scalars(
            select(GamePlayer)
            .where(GamePlayer.game_id == game_id)
            .order_by(GamePlayer.joined_order)
        )
        return list(rows)


async def count_games(db: async_sessionmaker[AsyncSession]) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(Game))


# --- создание игры ---


async def test_creator_makes_a_game_and_becomes_its_host(lobby):
    host = await add_host(lobby)

    response = await create_game(lobby, host)

    assert response.status_code == 201
    game = response.json()
    assert set(game) == GAME_FIELDS
    assert CODE_FORMAT.fullmatch(game["code"])
    assert (game["phase"], game["turn_phase"]) == ("lobby", None)
    assert game["adventure_id"] == "demo-crypt"
    assert game["adventure_title"] == load_adventure(DEMO_ADVENTURE_DIR).title
    [me] = game["players"]
    assert set(me) == PLAYER_FIELDS
    assert (me["user_id"], me["is_host"], me["ready"], me["has_character"]) == (
        host.id,
        True,
        False,
        False,
    )
    stored = await stored_game(lobby.db, game["id"])
    assert (stored.host_user_id, stored.code) == (host.id, game["code"])
    [row] = await stored_players(lobby.db, game["id"])
    assert (row.user_id, row.is_host, row.joined_order) == (host.id, True, 1)


async def test_game_needs_a_login(lobby):
    response = await lobby.http.post("/api/games", json={"adventure_id": "demo-crypt"})

    assert response.status_code == 401
    assert await count_games(lobby.db) == 0


async def test_user_outside_the_creator_list_gets_403_before_anything_else_is_checked(lobby):
    outsider = await add_friend(lobby)

    # Ни неизвестное приключение, ни неверный провайдер не должны проявиться раньше отказа в праве.
    response = await create_game(
        lobby, outsider, adventure_id="no-such-adventure", provider="no-such-provider"
    )

    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)
    # Отказ в праве идёт и перед проверкой формы тела: посторонний не узнаёт пределов.
    malformed = await create_game(lobby, outsider, collect_seconds=1)
    assert malformed.status_code == 403
    assert await count_games(lobby.db) == 0


async def test_creator_from_the_telegram_list_may_create_games(lobby):
    creator = await add_player(lobby.db, "Создатель", tg_id=CREATOR_TG_ID)

    response = await create_game(lobby, creator)

    assert response.status_code == 201


async def test_unknown_adventure_is_404(lobby):
    host = await add_host(lobby)

    response = await create_game(lobby, host, adventure_id="no-such-adventure")

    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
    assert await count_games(lobby.db) == 0


def add_blocked_adventure(content_dir: Path) -> None:
    """Копия демо, где скрытый выход ведёт в пустоту: загрузка проходит, валидатор видит ошибку."""
    folder = shutil.copytree(DEMO_ADVENTURE_DIR, content_dir / "blocked-crypt")
    manifest = folder / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("id: demo-crypt", "id: blocked-crypt", 1),
        encoding="utf-8",
    )
    cemetery = folder / "locations" / "cemetery.yaml"
    cemetery.write_text(
        cemetery.read_text(encoding="utf-8").replace("to: crypt", "to: nowhere", 1),
        encoding="utf-8",
    )


async def test_adventure_with_blocking_errors_is_422_and_the_creator_gets_the_report(
    lobby, content_dir
):
    add_blocked_adventure(content_dir)
    lobby.app.state.adventures = load_catalog(content_dir, load_srd())
    host = await add_host(lobby)

    response = await create_game(lobby, host, adventure_id="blocked-crypt")

    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], str)
    # Отчёт с id скрытого выхода получает только создатель игры (решение владельца B6).
    assert any("to-crypt" in problem for problem in body["problems"])
    assert await count_games(lobby.db) == 0


async def test_game_keeps_its_own_copy_of_the_adventure(lobby, content_dir):
    host = await add_host(lobby)
    original = load_adventure(DEMO_ADVENTURE_DIR)
    game = await new_game(lobby, host)
    friend = await joined_friend(lobby, game)

    # Приключение убрали и из папки, и из кеша: игра идёт по своей копии.
    shutil.rmtree(content_dir / "demo-crypt")
    lobby.app.state.adventures.clear()

    view = await lobby.http.get(f"/api/games/{game['id']}", headers=host.headers)
    assert view.status_code == 200
    assert view.json()["adventure_title"] == original.title
    assert (await join(lobby, friend, game["code"])).status_code == 200
    for player in (host, friend):
        await ready_with_character(lobby, game["id"], player)
    started = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)
    assert started.status_code == 200
    stored = await stored_game(lobby.db, game["id"])
    assert Adventure.model_validate(stored.adventure_data) == original
    # А новую игру на убранном приключении уже не создать.
    assert (await create_game(lobby, host)).status_code == 404


async def test_provider_and_model_are_checked_by_the_factory_and_saved(lobby, settings):
    lobby.app.state.settings = settings.model_copy(
        update={"anthropic_api_key": SecretStr(ANTHROPIC_TEST_KEY)}
    )
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="anthropic_api", model="claude-test-1")

    assert response.status_code == 201
    assert ANTHROPIC_TEST_KEY not in response.text
    game = response.json()
    assert (game["provider"], game["model"]) == ("anthropic_api", "claude-test-1")
    stored = await stored_game(lobby.db, game["id"])
    assert (stored.provider, stored.model) == ("anthropic_api", "claude-test-1")


async def test_without_overrides_the_game_uses_the_environment_provider(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host)

    assert (game["provider"], game["model"]) == (None, None)
    stored = await stored_game(lobby.db, game["id"])
    assert (stored.provider, stored.model) == (None, None)


@pytest.mark.parametrize(
    ("body", "settings_update", "expected_in_detail"),
    [
        pytest.param({"provider": "anthropic_api"}, {}, "ANTHROPIC_API_KEY", id="нет ключа"),
        pytest.param({"provider": "no_such_provider"}, {}, "неизвестный провайдер", id="чужое имя"),
        pytest.param({}, {"fake_llm_scenario": None}, "FAKE_LLM_SCENARIO", id="env без сценария"),
        pytest.param(
            {"provider": "openai_compatible"}, {}, "OPENAI_COMPAT_BASE_URL", id="нет адреса"
        ),
    ],
)
async def test_misconfigured_provider_is_422_with_a_reason_and_makes_no_game(
    lobby, settings, body, settings_update, expected_in_detail
):
    lobby.app.state.settings = settings.model_copy(update=settings_update)
    host = await add_host(lobby)

    response = await create_game(lobby, host, **body)

    assert response.status_code == 422
    assert expected_in_detail in response.json()["detail"]
    assert await count_games(lobby.db) == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"provider": "x" * 33}, id="длинное имя провайдера"),
        pytest.param({"provider": "Fake Provider"}, id="пробел в имени провайдера"),
        pytest.param({"model": "m" * 101}, id="длинное имя модели"),
        pytest.param({"model": "bad model"}, id="пробел в имени модели"),
        pytest.param({"model": "model\nnext"}, id="перевод строки в модели"),
        pytest.param({"model": 7}, id="модель не строка"),
    ],
)
async def test_provider_and_model_names_are_limited(lobby, body):
    host = await add_host(lobby)

    response = await create_game(lobby, host, **body)

    assert response.status_code == 422
    assert await count_games(lobby.db) == 0


async def test_claude_code_cli_is_allowed_in_dev_and_launches_nothing(lobby):
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="claude_code_cli")

    assert response.status_code == 201
    assert response.json()["provider"] == "claude_code_cli"


async def test_claude_code_cli_is_422_in_prod(settings, db, content_dir):
    prod = settings.model_copy(update={"app_env": "prod"})
    async with start_lobby(prod, db) as lobby:
        creator = await add_player(db, "Создатель", tg_id=CREATOR_TG_ID)

        response = await create_game(lobby, creator, provider="claude_code_cli")

        assert response.status_code == 422
        assert "dev" in response.json()["detail"]
        assert await count_games(db) == 0


@pytest.mark.parametrize(
    ("body", "default"),
    [
        pytest.param({"provider": "claude_code_cli"}, "fake", id="переопределение игры"),
        pytest.param({}, "claude_code_cli", id="LLM_PROVIDER"),
    ],
)
async def test_in_prod_claude_code_cli_is_refused_as_dev_only_before_its_models_are_named(
    settings, db, body, default
):
    # Сначала dev-ворота провайдера, потом модель: в prod отказ говорит «только dev» и не
    # перечисляет модели CLI вместе с CLAUDE_CLI_MODEL — GET /api/providers их в prod тоже не
    # показывает.
    prod = settings.model_copy(
        update={"app_env": "prod", "llm_provider": default, "claude_cli_model": "my-private-model"}
    )
    async with start_lobby(prod, db) as lobby:
        creator = await add_player(db, "Создатель", tg_id=CREATOR_TG_ID)

        response = await create_game(lobby, creator, model="opus2", **body)

        assert response.status_code == 422
        assert "dev" in response.json()["detail"]
        assert "my-private-model" not in response.text
        assert await count_games(db) == 0


# --- модель подписки claude_code_cli (DND-2 B1.2, спека §11) ---


@pytest.mark.parametrize("model", ["opus", "sonnet", "haiku"])
async def test_claude_code_cli_takes_a_model_from_its_list_and_saves_it(lobby, model):
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="claude_code_cli", model=model)

    assert response.status_code == 201, response.text
    assert response.json()["model"] == model
    assert (await stored_game(lobby.db, response.json()["id"])).model == model


async def test_claude_code_cli_takes_the_model_of_the_environment_even_with_brackets(
    lobby, settings
):
    # Значение CLAUDE_CLI_MODEL входит в список формы, даже если общий вид имени его не пустил бы.
    lobby.app.state.settings = settings.model_copy(update={"claude_cli_model": "sonnet[1m]"})
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="claude_code_cli", model="sonnet[1m]")

    assert response.status_code == 201, response.text
    assert response.json()["model"] == "sonnet[1m]"


# «x:y» и «qwen2.5:14b» CLI не примет: у сохранённой игры фабрика такую модель пропускает, но
# создать с ней новую игру нельзя — модель берётся только из списка формы.
@pytest.mark.parametrize(
    "model", ["claude-opus-5-5", "opus2", "sonnet[1m]", "-opus", "x:y", "qwen2.5:14b"]
)
async def test_claude_code_cli_refuses_a_model_outside_its_list(lobby, model):
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="claude_code_cli", model=model)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert await count_games(lobby.db) == 0


@pytest.mark.parametrize("model", ["opus2", "x:y"])
async def test_in_dev_a_model_outside_the_list_is_refused_with_the_list(lobby, model):
    # Провайдер проверяется первым, но в dev он собирается, и отказ по-прежнему называет список.
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="claude_code_cli", model=model)

    assert response.status_code == 422
    assert "одна из списка: opus, sonnet, haiku" in response.json()["detail"]


async def test_the_model_list_applies_when_claude_code_cli_is_the_default_provider(lobby, settings):
    lobby.app.state.settings = settings.model_copy(update={"llm_provider": "claude_code_cli"})
    host = await add_host(lobby)

    refused = await create_game(lobby, host, model="claude-opus-5-5")
    accepted = await create_game(lobby, host, model="haiku")

    assert refused.status_code == 422
    assert accepted.status_code == 201, accepted.text


async def test_brackets_in_a_model_name_stay_refused_for_other_providers(lobby, settings):
    lobby.app.state.settings = settings.model_copy(
        update={"anthropic_api_key": SecretStr(ANTHROPIC_TEST_KEY), "claude_cli_model": "x[1m]"}
    )
    host = await add_host(lobby)

    response = await create_game(lobby, host, provider="anthropic_api", model="x[1m]")

    assert response.status_code == 422
    assert await count_games(lobby.db) == 0


@pytest.mark.parametrize("default", ["anthropic_api", "fake"])
async def test_brackets_stay_refused_when_another_provider_is_the_default(lobby, settings, default):
    # Зеркало случая «claude_code_cli по умолчанию»: без `provider` в запросе модель проверяется
    # по провайдеру окружения, и CLAUDE_CLI_MODEL со скобками ему не подходит.
    lobby.app.state.settings = settings.model_copy(
        update={
            "llm_provider": default,
            "anthropic_api_key": SecretStr(ANTHROPIC_TEST_KEY),
            "claude_cli_model": "sonnet[1m]",
        }
    )
    host = await add_host(lobby)

    refused = await create_game(lobby, host, model="sonnet[1m]")
    accepted = await create_game(lobby, host, model="sonnet")

    assert refused.status_code == 422
    assert "квадратных скобок" in refused.json()["detail"]
    assert accepted.status_code == 201, accepted.text


async def test_the_subscription_settings_do_not_open_claude_code_cli_in_prod(settings, db):
    # Модель из списка формы проходит проверку модели, но провайдер в prod по-прежнему закрыт.
    prod = settings.model_copy(
        update={
            "app_env": "prod",
            "claude_cli_model": "opus",
            "claude_cli_effort": "high",
            "claude_cli_cheap_model": "haiku",
        }
    )
    async with start_lobby(prod, db) as lobby:
        creator = await add_player(db, "Создатель", tg_id=CREATOR_TG_ID)

        response = await create_game(lobby, creator, provider="claude_code_cli", model="opus")

        assert response.status_code == 422
        assert "dev" in response.json()["detail"]
        assert await count_games(db) == 0


# --- мат и грубая речь (DND-2 B1.2, решение Р1) ---


async def test_a_new_game_allows_profanity_by_default(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host)

    assert game["profanity"] is True
    assert (await stored_game(lobby.db, game["id"])).profanity is True


async def test_the_host_turns_profanity_off_and_everybody_in_the_game_sees_it(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host, profanity=False)
    friend = await joined_friend(lobby, game)

    assert game["profanity"] is False
    assert (await stored_game(lobby.db, game["id"])).profanity is False
    summary = await lobby.http.get(f"/api/games/{game['id']}", headers=friend.headers)
    assert summary.json()["profanity"] is False
    mine = await lobby.http.get("/api/games", headers=friend.headers)
    assert [g["profanity"] for g in mine.json()] == [False]
    _game_id, payload, _audience = lobby.broadcaster.sent[-1]
    assert payload["type"] == "lobby" and payload["game"]["profanity"] is False


@pytest.mark.parametrize("value", ["false", 0, 1, None, "да"])
async def test_profanity_must_be_true_or_false(lobby, value):
    host = await add_host(lobby)

    response = await create_game(lobby, host, profanity=value)

    assert response.status_code == 422
    assert await count_games(lobby.db) == 0


# --- длительности отсчётов ---


async def test_countdowns_come_from_the_environment_by_default(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host)

    defaults = lobby.settings
    assert (game["collect_seconds"], game["hurry_seconds"], game["camp_seconds"]) == (
        defaults.collect_seconds,
        defaults.hurry_seconds,
        defaults.camp_seconds,
    )
    stored = await stored_game(lobby.db, game["id"])
    assert (stored.collect_seconds, stored.hurry_seconds, stored.camp_seconds) == (
        defaults.collect_seconds,
        defaults.hurry_seconds,
        defaults.camp_seconds,
    )


async def test_host_sets_the_countdowns_at_creation(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host, collect_seconds=45, hurry_seconds=20, camp_seconds=600)

    assert (game["collect_seconds"], game["hurry_seconds"], game["camp_seconds"]) == (45, 20, 600)
    stored = await stored_game(lobby.db, game["id"])
    assert (stored.collect_seconds, stored.hurry_seconds, stored.camp_seconds) == (45, 20, 600)


async def test_the_short_rest_comes_from_the_environment_or_from_the_host(lobby):
    # Короткий привал (блок B9): без поля — `SHORT_REST_SECONDS`, с полем — выбор хоста.
    host = await add_host(lobby)

    by_default = await new_game(lobby, host)
    chosen = await new_game(lobby, host, short_rest_seconds=90)

    assert by_default["short_rest_seconds"] == lobby.settings.short_rest_seconds
    assert chosen["short_rest_seconds"] == 90
    assert (await stored_game(lobby.db, by_default["id"])).short_rest_seconds == (
        lobby.settings.short_rest_seconds
    )
    assert (await stored_game(lobby.db, chosen["id"])).short_rest_seconds == 90


async def test_one_countdown_may_be_set_while_the_others_stay_default(lobby):
    host = await add_host(lobby)

    game = await new_game(lobby, host, hurry_seconds=30)

    assert game["hurry_seconds"] == 30
    assert game["collect_seconds"] == lobby.settings.collect_seconds


COUNTDOWN_LIMITS = [
    ("collect_seconds", 30, 600),
    ("hurry_seconds", 15, 300),
    ("camp_seconds", 30, 900),
    ("short_rest_seconds", 30, 600),
]


@pytest.mark.parametrize(("name", "low", "high"), COUNTDOWN_LIMITS)
async def test_countdown_limits_are_inclusive(lobby, name, low, high):
    host = await add_host(lobby)

    at_low = await create_game(lobby, host, **{name: low})
    at_high = await create_game(lobby, host, **{name: high})

    assert (at_low.status_code, at_high.status_code) == (201, 201)
    assert (at_low.json()[name], at_high.json()[name]) == (low, high)


@pytest.mark.parametrize(("name", "low", "high"), COUNTDOWN_LIMITS)
@pytest.mark.parametrize("offset", [-1, +1])
async def test_countdown_outside_the_limits_is_422(lobby, name, low, high, offset):
    host = await add_host(lobby)
    value = low - 1 if offset < 0 else high + 1

    response = await create_game(lobby, host, **{name: value})

    assert response.status_code == 422
    assert name in response.json()["detail"]
    assert await count_games(lobby.db) == 0


@pytest.mark.parametrize("name", ["collect_seconds", "short_rest_seconds"])
@pytest.mark.parametrize("value", [0, -5, 2.5, "120", "много", None, True])
async def test_countdown_must_be_a_whole_number(lobby, name, value):
    host = await add_host(lobby)

    response = await create_game(lobby, host, **{name: value})

    # `None` значит «значение из env», остальное — ошибка.
    assert response.status_code == (201 if value is None else 422)


# --- код игры ---


def test_game_codes_are_six_characters_of_capitals_and_digits():
    codes = {service.new_game_code() for _ in range(300)}

    assert all(CODE_FORMAT.fullmatch(code) for code in codes)
    assert len(codes) > 250  # коды случайны, а не один и тот же


async def test_clashing_game_code_is_replaced_with_a_new_one(lobby, monkeypatch):
    host = await add_host(lobby)
    first = await new_game(lobby, host)
    codes = iter([first["code"], first["code"], "ZZ9ZZ9"])
    monkeypatch.setattr(service, "new_game_code", lambda: next(codes))

    second = await new_game(lobby, host)

    assert second["code"] == "ZZ9ZZ9"
    assert second["id"] != first["id"]
    assert await count_games(lobby.db) == 2


async def test_game_is_not_made_when_every_code_clashes(lobby, monkeypatch):
    host = await add_host(lobby)
    first = await new_game(lobby, host)
    monkeypatch.setattr(service, "new_game_code", lambda: first["code"])

    with pytest.raises(service.GameCodeUnavailable):
        await create_game(lobby, host)

    assert await count_games(lobby.db) == 1


# --- вход по коду ---


async def test_player_joins_by_code_and_everybody_sees_the_lobby_change(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await add_friend(lobby, "Анна")

    response = await join(lobby, friend, game["code"])

    assert response.status_code == 200
    joined = response.json()
    assert joined["id"] == game["id"]
    assert [(p["user_id"], p["name"], p["is_host"]) for p in joined["players"]] == [
        (host.id, "Хост", True),
        (friend.id, "Анна", False),
    ]
    [(game_id, payload, audience)] = lobby.broadcaster.sent
    assert (game_id, audience) == (game["id"], "all")
    assert payload == {"type": "lobby", "game": joined}


async def test_code_is_accepted_in_any_case_with_spaces_around(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await add_friend(lobby)

    response = await join(lobby, friend, f"  {game['code'].lower()} ")

    assert response.status_code == 200
    assert response.json()["id"] == game["id"]


async def test_joining_twice_is_idempotent(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await add_friend(lobby)

    first = await join(lobby, friend, game["code"])
    second = await join(lobby, friend, game["code"])
    host_again = await join(lobby, host, game["code"])

    assert [first.status_code, second.status_code, host_again.status_code] == [200, 200, 200]
    assert first.json() == second.json()
    rows = await stored_players(lobby.db, game["id"])
    assert [(row.user_id, row.joined_order) for row in rows] == [(host.id, 1), (friend.id, 2)]
    # Повторный вход ничего не изменил, значит и рассылать нечего.
    assert len(lobby.broadcaster.sent) == 1


async def test_seventh_player_is_409(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    for _ in range(5):
        await joined_friend(lobby, game)
    latecomer = await add_friend(lobby)

    response = await join(lobby, latecomer, game["code"])

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert len(await stored_players(lobby.db, game["id"])) == 6


async def test_a_full_lobby_still_lets_its_members_in_again(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friends = [await joined_friend(lobby, game) for _ in range(5)]

    assert (await join(lobby, friends[-1], game["code"])).status_code == 200


async def test_parallel_joins_never_exceed_six_players(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    crowd = [await add_friend(lobby) for _ in range(10)]

    responses = await asyncio.gather(*(join(lobby, player, game["code"]) for player in crowd))

    assert Counter(response.status_code for response in responses) == {200: 5, 409: 5}
    rows = await stored_players(lobby.db, game["id"])
    assert len(rows) == 6
    assert [row.joined_order for row in rows] == [1, 2, 3, 4, 5, 6]
    assert len({row.user_id for row in rows}) == 6


async def test_the_same_player_joining_in_parallel_makes_one_row(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await add_friend(lobby)

    responses = await asyncio.gather(*(join(lobby, friend, game["code"]) for _ in range(6)))

    assert [response.status_code for response in responses] == [200] * 6
    assert len(await stored_players(lobby.db, game["id"])) == 2


async def test_unknown_code_is_404_and_a_malformed_code_is_422(lobby):
    friend = await add_friend(lobby)

    unknown = await join(lobby, friend, "AAAAAA")
    too_short = await join(lobby, friend, "ABC")
    not_alphanumeric = await join(lobby, friend, "ABC-12")

    assert unknown.status_code == 404
    assert isinstance(unknown.json()["detail"], str)
    assert [too_short.status_code, not_alphanumeric.status_code] == [422, 422]


async def test_joining_needs_a_login(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    response = await lobby.http.post("/api/games/join", json={"code": game["code"]})

    assert response.status_code == 401


async def test_newcomer_cannot_join_a_game_that_has_started_but_its_members_can(lobby):
    game, host, others = await ready_lobby(lobby)
    started = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)
    assert started.status_code == 200
    newcomer = await add_friend(lobby)

    refused = await join(lobby, newcomer, game["code"])
    returned = await join(lobby, others[0], game["code"])

    assert refused.status_code == 409
    assert returned.status_code == 200
    assert returned.json()["phase"] == "weaving"
    assert len(await stored_players(lobby.db, game["id"])) == 3


# --- предел неудачных попыток войти по коду (решение владельца B6.security) ---


class FakeClock:
    """Часы окна попыток: время стоит, пока тест его не сдвинет."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(lobby: Lobby) -> FakeClock:
    clock = FakeClock()
    lobby.app.state.join_attempts = service.JoinAttempts(clock=clock)
    return clock


def unknown_code(number: int) -> str:
    """Код нужного вида, которого в игре нет (коды игр случайные: совпадение ничтожно)."""
    return f"ZZZZ{number:02d}"


async def guesses_of(lobby: Lobby, kind: str) -> Callable[[int], str]:
    """Чем неудачно входит чужак: номер попытки -> код. Три вида отказа, все подтверждают игру."""
    if kind == "unknown":
        return unknown_code
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    if kind == "full":
        for _ in range(5):
            await joined_friend(lobby, game)
    else:  # "started"
        await ready_with_character(lobby, game["id"], host)
        started = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)
        assert started.status_code == 200, started.text
    return lambda _number: game["code"]


REFUSAL_KINDS = [
    pytest.param("unknown", 404, id="нет такой игры"),
    pytest.param("started", 409, id="игра уже началась"),
    pytest.param("full", 409, id="мест нет"),
]


def test_the_failed_join_limit_is_the_one_the_owner_chose():
    assert service.MAX_FAILED_JOINS == MAX_FAILED_JOINS
    assert service.FAILED_JOIN_WINDOW_S == FAILED_JOIN_WINDOW_S


@pytest.mark.parametrize(("kind", "refusal"), REFUSAL_KINDS)
async def test_the_11th_failed_attempt_in_ten_minutes_is_429(lobby, kind, refusal):
    guess = await guesses_of(lobby, kind)
    intruder = await add_friend(lobby)

    first = [await join(lobby, intruder, guess(number)) for number in range(MAX_FAILED_JOINS)]
    blocked = await join(lobby, intruder, guess(MAX_FAILED_JOINS))

    assert [response.status_code for response in first] == [refusal] * MAX_FAILED_JOINS
    assert blocked.status_code == 429, blocked.text
    assert set(blocked.json()) == {"detail"}
    assert re.search("[а-яё]", blocked.json()["detail"], re.IGNORECASE)


async def test_a_blocked_player_cannot_join_even_an_open_lobby_until_the_window_ends(lobby, clock):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    intruder = await add_friend(lobby)
    for number in range(MAX_FAILED_JOINS):
        assert (await join(lobby, intruder, unknown_code(number))).status_code == 404

    refused = await join(lobby, intruder, game["code"])

    assert refused.status_code == 429
    assert len(await stored_players(lobby.db, game["id"])) == 1


async def test_other_players_are_not_affected_by_a_block(lobby, clock):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    intruder = await add_friend(lobby)
    honest = await add_friend(lobby)
    for number in range(MAX_FAILED_JOINS + 1):
        await join(lobby, intruder, unknown_code(number))

    unknown = await join(lobby, honest, unknown_code(99))
    joined = await join(lobby, honest, game["code"])

    assert unknown.status_code == 404
    assert joined.status_code == 200


async def test_members_coming_back_are_neither_counted_nor_blocked(lobby, clock):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await joined_friend(lobby, game)
    for _ in range(MAX_FAILED_JOINS + 5):  # успешный и повторный вход не тратят попытки
        assert (await join(lobby, friend, game["code"])).status_code == 200

    wrong = [await join(lobby, friend, unknown_code(n)) for n in range(MAX_FAILED_JOINS)]
    assert [response.status_code for response in wrong] == [404] * MAX_FAILED_JOINS
    assert (await join(lobby, friend, unknown_code(99))).status_code == 429
    # Заблокированный игрок всё равно возвращается в свою игру: чужой код ему не нужен.
    assert (await join(lobby, friend, game["code"])).status_code == 200
    assert (await join(lobby, host, game["code"])).status_code == 200


async def test_a_malformed_code_is_not_an_attempt(lobby, clock):
    intruder = await add_friend(lobby)
    for _ in range(MAX_FAILED_JOINS + 5):
        assert (await join(lobby, intruder, "ABC")).status_code == 422

    wrong = [await join(lobby, intruder, unknown_code(n)) for n in range(MAX_FAILED_JOINS)]

    assert [response.status_code for response in wrong] == [404] * MAX_FAILED_JOINS


async def test_attempts_expire_with_the_window_and_a_blocked_attempt_does_not_extend_it(
    lobby, clock
):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    intruder = await add_friend(lobby)
    for number in range(MAX_FAILED_JOINS):
        assert (await join(lobby, intruder, unknown_code(number))).status_code == 404

    clock.now += FAILED_JOIN_WINDOW_S - 1
    assert (await join(lobby, intruder, unknown_code(50))).status_code == 429
    clock.now += 1  # ровно десять минут с первых попыток
    allowed = await join(lobby, intruder, game["code"])

    assert allowed.status_code == 200
    # Отказы во время блокировки не продлили её: попытки сгорели вместе со своими временами.
    clock.now += 1
    assert (await join(lobby, intruder, unknown_code(51))).status_code == 404


async def test_the_window_slides_with_the_attempts(lobby, clock):
    intruder = await add_friend(lobby)
    for number in range(5):
        assert (await join(lobby, intruder, unknown_code(number))).status_code == 404
    clock.now += FAILED_JOIN_WINDOW_S / 2
    for number in range(5, MAX_FAILED_JOINS):
        assert (await join(lobby, intruder, unknown_code(number))).status_code == 404
    assert (await join(lobby, intruder, unknown_code(60))).status_code == 429

    clock.now += FAILED_JOIN_WINDOW_S / 2  # первые пять попыток вышли из окна, ещё пять остались
    after = [await join(lobby, intruder, unknown_code(70 + n)) for n in range(6)]

    assert [response.status_code for response in after] == [404] * 5 + [429]


async def test_the_refusal_tells_how_many_minutes_are_left(lobby, clock):
    intruder = await add_friend(lobby)
    for number in range(MAX_FAILED_JOINS):
        await join(lobby, intruder, unknown_code(number))

    at_once = await join(lobby, intruder, unknown_code(50))
    clock.now += 301
    later = await join(lobby, intruder, unknown_code(51))

    assert "10 мин" in at_once.json()["detail"]
    assert "5 мин" in later.json()["detail"]


@pytest.mark.parametrize(("kind", "refusal"), REFUSAL_KINDS)
async def test_a_burst_of_parallel_guesses_gets_no_more_than_the_limit(lobby, kind, refusal):
    guess = await guesses_of(lobby, kind)
    intruder = await add_friend(lobby)

    responses = await asyncio.gather(
        *(join(lobby, intruder, guess(number)) for number in range(3 * MAX_FAILED_JOINS))
    )

    assert Counter(response.status_code for response in responses) == {
        refusal: MAX_FAILED_JOINS,
        429: 2 * MAX_FAILED_JOINS,
    }


# --- чужие игры и мои игры ---


async def test_game_is_visible_to_its_members_only(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await joined_friend(lobby, game)
    stranger = await add_friend(lobby)

    for member in (host, friend):
        view = await lobby.http.get(f"/api/games/{game['id']}", headers=member.headers)
        assert view.status_code == 200
        assert set(view.json()) == GAME_FIELDS
        assert len(view.json()["players"]) == 2
    hidden = await lobby.http.get(f"/api/games/{game['id']}", headers=stranger.headers)
    missing = await lobby.http.get("/api/games/no-such-game", headers=stranger.headers)

    # Чужая игра отвечает так же, как несуществующая: 404 не говорит, что игра есть.
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


async def test_a_stranger_gets_404_on_ready_and_start_and_changes_nothing(lobby):
    game, _host, _others = await ready_lobby(lobby)
    stranger = await add_friend(lobby)

    ready = await mark_ready(lobby, game["id"], stranger, False)
    start = await lobby.http.post(f"/api/games/{game['id']}/start", headers=stranger.headers)
    nothing = await mark_ready(lobby, "no-such-game", stranger, False)

    assert [ready.status_code, start.status_code] == [404, 404]
    assert ready.json() == nothing.json()
    assert (await stored_game(lobby.db, game["id"])).phase == GamePhase.LOBBY
    assert len(await stored_players(lobby.db, game["id"])) == 3


async def test_game_view_needs_a_login(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    assert (await lobby.http.get(f"/api/games/{game['id']}")).status_code == 401
    assert (await lobby.http.get("/api/games")).status_code == 401


async def test_my_games_lists_only_the_games_i_am_in_newest_first(lobby):
    host = await add_host(lobby)
    other_host = await add_player(lobby.db, "Другой хост", dev_login="dev-2")
    older = await new_game(lobby, host)
    newer = await new_game(lobby, host)
    foreign = await new_game(lobby, other_host)
    friend = await joined_friend(lobby, older)

    hosts_games = await lobby.http.get("/api/games", headers=host.headers)
    friends_games = await lobby.http.get("/api/games", headers=friend.headers)
    nobodys_games = await lobby.http.get("/api/games", headers=(await add_friend(lobby)).headers)

    assert [game["id"] for game in hosts_games.json()] == [newer["id"], older["id"]]
    assert [game["id"] for game in friends_games.json()] == [older["id"]]
    assert len(friends_games.json()[0]["players"]) == 2
    assert foreign["id"] not in hosts_games.text
    assert nobodys_games.json() == []


# --- готовность ---


async def test_ready_needs_a_character(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    refused = await mark_ready(lobby, game["id"], host)

    assert refused.status_code == 409
    assert isinstance(refused.json()["detail"], str)
    [row] = await stored_players(lobby.db, game["id"])
    assert row.ready is False
    assert lobby.broadcaster.sent == []


async def test_player_with_a_character_marks_ready_and_takes_it_back(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    await give_character(lobby.db, game["id"], host)

    ready = await mark_ready(lobby, game["id"], host)
    not_ready = await mark_ready(lobby, game["id"], host, False)

    assert ready.status_code == 200
    [me] = ready.json()["players"]
    assert (me["ready"], me["has_character"]) == (True, True)
    assert not_ready.json()["players"][0]["ready"] is False
    assert [payload["game"]["players"][0]["ready"] for _, payload, _ in lobby.broadcaster.sent] == [
        True,
        False,
    ]


async def test_not_ready_is_always_allowed_even_without_a_character(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    response = await mark_ready(lobby, game["id"], host, False)

    assert response.status_code == 200


async def test_ready_must_be_a_boolean(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    response = await lobby.http.post(
        f"/api/games/{game['id']}/ready", headers=host.headers, json={"ready": "возможно"}
    )

    assert response.status_code == 422


async def test_ready_cannot_be_changed_after_the_start(lobby):
    game, host, _others = await ready_lobby(lobby)
    await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    response = await mark_ready(lobby, game["id"], host, False)

    assert response.status_code == 409
    assert all(row.ready for row in await stored_players(lobby.db, game["id"]))


# --- старт ---


async def test_start_moves_the_game_to_weaving_and_calls_the_hook_after_the_commit(lobby):
    game, host, _others = await ready_lobby(lobby)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert response.status_code == 200
    assert response.json()["phase"] == "weaving"
    stored = await stored_game(lobby.db, game["id"])
    assert stored.phase == GamePhase.WEAVING
    assert stored.started_at is not None
    assert stored.clock_at == stored.started_at  # время игры без пауз считается со старта
    assert lobby.hooks.started == [game["id"]]
    # В момент вызова хука изменение уже было в базе.
    assert lobby.hooks.phase_seen == [GamePhase.WEAVING]
    last_game, last_payload, last_audience = lobby.broadcaster.sent[-1]
    assert (last_game, last_audience) == (game["id"], "all")
    assert last_payload == {"type": "lobby", "game": response.json()}


async def test_single_ready_host_may_start_alone(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    await ready_with_character(lobby, game["id"], host)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert response.status_code == 200
    assert lobby.hooks.started == [game["id"]]


async def test_start_by_a_player_who_is_not_the_host_is_403(lobby):
    game, _host, others = await ready_lobby(lobby)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=others[0].headers)

    assert response.status_code == 403
    assert (await stored_game(lobby.db, game["id"])).phase == GamePhase.LOBBY
    assert lobby.hooks.started == []


async def test_start_without_readiness_is_409(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert (await stored_game(lobby.db, game["id"])).phase == GamePhase.LOBBY
    assert lobby.hooks.started == []


async def test_start_waits_for_a_player_who_took_the_ready_mark_back(lobby):
    game, host, others = await ready_lobby(lobby)
    await mark_ready(lobby, game["id"], others[1], False)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert response.status_code == 409
    assert (await stored_game(lobby.db, game["id"])).phase == GamePhase.LOBBY
    assert lobby.hooks.started == []


async def test_start_waits_for_a_newcomer_without_a_character(lobby):
    game, host, _others = await ready_lobby(lobby)
    await joined_friend(lobby, game)

    response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert response.status_code == 409
    assert lobby.hooks.started == []


async def test_game_cannot_be_started_twice(lobby):
    game, host, _others = await ready_lobby(lobby)

    first = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)
    second = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    assert [first.status_code, second.status_code] == [200, 409]
    assert lobby.hooks.started == [game["id"]]


async def test_parallel_starts_give_exactly_one_success(lobby):
    game, host, _others = await ready_lobby(lobby)

    responses = await asyncio.gather(
        *(lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers) for _ in range(5))
    )

    assert Counter(response.status_code for response in responses) == {200: 1, 409: 4}
    assert lobby.hooks.started == [game["id"]]


async def test_a_failing_hook_does_not_fail_the_start_and_is_logged(settings, db, caplog):
    async with start_lobby(settings, db) as lobby:
        lobby.app.state.game_hooks = FailingHooks()
        game, host, _others = await ready_lobby(lobby)

        with caplog.at_level(logging.ERROR):
            response = await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

        assert response.status_code == 200
        assert (await stored_game(db, game["id"])).phase == GamePhase.WEAVING
        assert game["id"] in caplog.text


async def test_a_failing_broadcaster_does_not_fail_the_change_and_is_logged(settings, db, caplog):
    async with start_lobby(settings, db) as lobby:
        lobby.app.state.broadcaster = FailingBroadcaster()
        host = await add_host(lobby)
        game = await new_game(lobby, host)
        friend = await add_friend(lobby)

        with caplog.at_level(logging.ERROR):
            response = await join(lobby, friend, game["code"])

        assert response.status_code == 200
        assert len(await stored_players(db, game["id"])) == 2
        assert game["id"] in caplog.text


# --- выход и исключение из лобби (решение владельца S5, D2) ---


async def give_choice(db: async_sessionmaker[AsyncSession], game_id: str, player: Player) -> None:
    """Выбор игрока при создании героя (B8.s3): он хранится вместе со строкой персонажа."""
    async with db() as session:
        row = await session.scalar(
            select(PlayerCharacter).where(
                PlayerCharacter.game_id == game_id, PlayerCharacter.user_id == player.id
            )
        )
        row.choices = {"mode": "random", "choices": None}
        await session.commit()


async def leave(lobby: Lobby, game_id: str, player: Player) -> httpx.Response:
    return await lobby.http.post(f"/api/games/{game_id}/leave", headers=player.headers)


async def kick(lobby: Lobby, game_id: str, by: Player, target_id: str) -> httpx.Response:
    return await lobby.http.post(
        f"/api/games/{game_id}/players/{target_id}/kick", headers=by.headers
    )


async def stored_characters(db: async_sessionmaker[AsyncSession], game_id: str) -> set[str]:
    async with db() as session:
        rows = await session.scalars(
            select(PlayerCharacter.user_id).where(PlayerCharacter.game_id == game_id)
        )
        return set(rows)


async def stored_choices(db: async_sessionmaker[AsyncSession], game_id: str) -> set[str]:
    """Игроки, у чьих героев в базе лежит выбор."""
    async with db() as session:
        rows = await session.scalars(
            select(PlayerCharacter.user_id).where(
                PlayerCharacter.game_id == game_id, PlayerCharacter.choices.is_not(None)
            )
        )
        return set(rows)


async def test_a_player_leaves_the_lobby_with_his_character_and_his_choice(lobby):
    game, host, (leaver, stayer) = await ready_lobby(lobby)
    await give_choice(lobby.db, game["id"], leaver)
    await give_choice(lobby.db, game["id"], stayer)
    lobby.broadcaster.sent.clear()

    response = await leave(lobby, game["id"], leaver)

    assert response.status_code == 204
    assert response.content == b""
    rows = await stored_players(lobby.db, game["id"])
    assert [row.user_id for row in rows] == [host.id, stayer.id]
    assert all(row.ready for row in rows)  # отметки остальных не тронуты
    assert await stored_characters(lobby.db, game["id"]) == {host.id, stayer.id}
    assert await stored_choices(lobby.db, game["id"]) == {stayer.id}
    # Остальные игроки узнают о выходе событием `lobby` с новым составом.
    [(game_id, payload, audience)] = lobby.broadcaster.sent
    assert (game_id, audience, payload["type"]) == (game["id"], "all", "lobby")
    assert [p["user_id"] for p in payload["game"]["players"]] == [host.id, stayer.id]


async def test_the_one_who_left_no_longer_sees_the_game_and_may_come_back_by_the_code(lobby):
    game, _host, (leaver, _stayer) = await ready_lobby(lobby)

    await leave(lobby, game["id"], leaver)

    gone = await lobby.http.get(f"/api/games/{game['id']}", headers=leaver.headers)
    assert gone.status_code == 404
    again = await join(lobby, leaver, game["code"])
    assert again.status_code == 200
    assert leaver.id in [p["user_id"] for p in again.json()["players"]]
    # Новый вход — без прежнего персонажа и без готовности.
    [me] = [p for p in again.json()["players"] if p["user_id"] == leaver.id]
    assert (me["has_character"], me["ready"]) == (False, False)


async def test_a_player_without_a_character_may_leave_too(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friend = await joined_friend(lobby, game)

    response = await leave(lobby, game["id"], friend)

    assert response.status_code == 204
    assert [row.user_id for row in await stored_players(lobby.db, game["id"])] == [host.id]


async def test_the_host_cannot_leave_his_lobby(lobby):
    game, host, _others = await ready_lobby(lobby)
    lobby.broadcaster.sent.clear()

    response = await leave(lobby, game["id"], host)

    assert response.status_code == 409
    assert "хост" in response.json()["detail"].lower()
    assert len(await stored_players(lobby.db, game["id"])) == 3
    assert host.id in await stored_characters(lobby.db, game["id"])
    assert lobby.broadcaster.sent == []


async def test_nobody_leaves_after_the_start(lobby):
    game, host, (friend, _other) = await ready_lobby(lobby)
    await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    response = await leave(lobby, game["id"], friend)

    assert response.status_code == 409
    assert len(await stored_players(lobby.db, game["id"])) == 3
    assert friend.id in await stored_characters(lobby.db, game["id"])


async def test_a_stranger_or_an_unknown_game_gives_404_on_leave(lobby):
    game, _host, _others = await ready_lobby(lobby)
    stranger = await add_friend(lobby)

    assert (await leave(lobby, game["id"], stranger)).status_code == 404
    assert (await leave(lobby, "no-such-game", stranger)).status_code == 404
    assert (await lobby.http.post(f"/api/games/{game['id']}/leave")).status_code == 401


async def test_leaving_twice_is_404_the_second_time(lobby):
    game, _host, (friend, _other) = await ready_lobby(lobby)

    first = await leave(lobby, game["id"], friend)
    second = await leave(lobby, game["id"], friend)

    assert [first.status_code, second.status_code] == [204, 404]


async def test_leaving_races_with_the_start_under_the_game_lock(lobby):
    game, host, (friend, _other) = await ready_lobby(lobby)

    start, left = await asyncio.gather(
        lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers),
        leave(lobby, game["id"], friend),
    )

    stored = await stored_game(lobby.db, game["id"])
    members = [row.user_id for row in await stored_players(lobby.db, game["id"])]
    if start.status_code == 200:  # старт успел раньше: выйти уже нельзя
        assert (left.status_code, stored.phase, friend.id in members) == (
            409,
            GamePhase.WEAVING,
            True,
        )
    else:  # выход успел раньше: игра осталась в лобби без вышедшего
        assert (left.status_code, stored.phase, friend.id in members) == (
            204,
            GamePhase.LOBBY,
            False,
        )


async def test_the_host_kicks_a_player_with_his_character_and_his_choice(lobby):
    game, host, (target, stayer) = await ready_lobby(lobby)
    await give_choice(lobby.db, game["id"], target)
    await give_choice(lobby.db, game["id"], stayer)
    lobby.broadcaster.sent.clear()

    response = await kick(lobby, game["id"], host, target.id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == GAME_FIELDS
    assert [p["user_id"] for p in body["players"]] == [host.id, stayer.id]
    rows = await stored_players(lobby.db, game["id"])
    assert [row.user_id for row in rows] == [host.id, stayer.id]
    assert all(row.ready for row in rows)
    assert await stored_characters(lobby.db, game["id"]) == {host.id, stayer.id}
    assert await stored_choices(lobby.db, game["id"]) == {stayer.id}
    [(game_id, payload, audience)] = lobby.broadcaster.sent
    assert (game_id, audience, payload) == (game["id"], "all", {"type": "lobby", "game": body})
    gone = await lobby.http.get(f"/api/games/{game['id']}", headers=target.headers)
    assert gone.status_code == 404


async def test_only_the_host_may_kick(lobby):
    game, host, (friend, other) = await ready_lobby(lobby)

    response = await kick(lobby, game["id"], friend, other.id)
    against_host = await kick(lobby, game["id"], friend, host.id)

    assert (response.status_code, against_host.status_code) == (403, 403)
    assert len(await stored_players(lobby.db, game["id"])) == 3
    assert len(await stored_characters(lobby.db, game["id"])) == 3


async def test_the_host_cannot_kick_himself(lobby):
    game, host, _others = await ready_lobby(lobby)

    response = await kick(lobby, game["id"], host, host.id)

    assert response.status_code == 409
    assert len(await stored_players(lobby.db, game["id"])) == 3


async def test_kicking_somebody_who_is_not_in_the_game_is_404(lobby):
    game, host, _others = await ready_lobby(lobby)
    stranger = await add_friend(lobby)

    assert (await kick(lobby, game["id"], host, stranger.id)).status_code == 404
    assert (await kick(lobby, game["id"], host, "no-such-user")).status_code == 404
    assert len(await stored_players(lobby.db, game["id"])) == 3


async def test_a_stranger_cannot_kick_and_the_game_looks_like_it_does_not_exist(lobby):
    game, _host, (friend, _other) = await ready_lobby(lobby)
    stranger = await add_friend(lobby)

    assert (await kick(lobby, game["id"], stranger, friend.id)).status_code == 404
    assert (await kick(lobby, "no-such-game", stranger, friend.id)).status_code == 404
    unauthenticated = await lobby.http.post(f"/api/games/{game['id']}/players/{friend.id}/kick")
    assert unauthenticated.status_code == 401
    assert len(await stored_players(lobby.db, game["id"])) == 3


async def test_nobody_is_kicked_after_the_start(lobby):
    game, host, (friend, _other) = await ready_lobby(lobby)
    await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)

    response = await kick(lobby, game["id"], host, friend.id)

    assert response.status_code == 409
    assert len(await stored_players(lobby.db, game["id"])) == 3
    assert friend.id in await stored_characters(lobby.db, game["id"])


async def test_a_kicked_player_may_join_again_by_the_code_as_a_newcomer(lobby):
    game, host, (target, _other) = await ready_lobby(lobby)
    await kick(lobby, game["id"], host, target.id)

    again = await join(lobby, target, game["code"])

    assert again.status_code == 200
    [me] = [p for p in again.json()["players"] if p["user_id"] == target.id]
    assert (me["has_character"], me["ready"]) == (False, False)


async def test_a_kick_frees_a_seat_for_a_seventh_player(lobby):
    host = await add_host(lobby)
    game = await new_game(lobby, host)
    friends = [await joined_friend(lobby, game) for _ in range(5)]
    late = await add_friend(lobby)
    assert (await join(lobby, late, game["code"])).status_code == 409

    await kick(lobby, game["id"], host, friends[0].id)

    assert (await join(lobby, late, game["code"])).status_code == 200


class ListeningSocket:
    """Сокет игрока для настоящего хаба: складывает принятые кадры."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        self.frames.append(json.loads(data))


def listen(lobby: Lobby, game_id: str, player: Player) -> tuple[ListeningSocket, Any]:
    """Подключает игрока к настоящему хабу приложения (как это делает WS-эндпоинт)."""
    socket = ListeningSocket()
    connection = lobby.app.state.hub.connect(game_id, player.id, socket)
    connection.start()
    return socket, connection


async def test_the_one_who_left_is_cut_off_from_the_events_of_the_game(lobby):
    game, _host, (leaver, stayer) = await ready_lobby(lobby)
    hub = lobby.app.state.hub
    leaver_socket, _ = listen(lobby, game["id"], leaver)
    stayer_socket, _ = listen(lobby, game["id"], stayer)
    assert hub.connections(game["id"]) == 2

    assert (await leave(lobby, game["id"], leaver)).status_code == 204
    await hub.send(game["id"], {"type": "phase", "phase": {"phase": "playing"}})
    for _ in range(50):
        await asyncio.sleep(0)

    assert hub.connections(game["id"]) == 1
    assert leaver_socket.frames == []  # ни события лобби о его выходе, ни дальнейших
    assert [f["type"] for f in stayer_socket.frames] == ["phase"]


async def test_a_kicked_player_is_cut_off_from_the_events_of_the_game(lobby):
    game, host, (target, stayer) = await ready_lobby(lobby)
    hub = lobby.app.state.hub
    target_socket, _ = listen(lobby, game["id"], target)
    stayer_socket, _ = listen(lobby, game["id"], stayer)

    assert (await kick(lobby, game["id"], host, target.id)).status_code == 200
    await hub.send(game["id"], {"type": "phase", "phase": {"phase": "playing"}})
    for _ in range(50):
        await asyncio.sleep(0)

    assert hub.connections(game["id"]) == 1
    assert target_socket.frames == []
    assert [f["type"] for f in stayer_socket.frames] == ["phase"]  # лобби идёт через регистратор


async def test_a_refused_leave_or_kick_keeps_the_sockets_of_the_player(lobby):
    game, host, (friend, other) = await ready_lobby(lobby)
    hub = lobby.app.state.hub
    listen(lobby, game["id"], host)
    listen(lobby, game["id"], friend)

    assert (await leave(lobby, game["id"], host)).status_code == 409  # хост не выходит
    assert (await kick(lobby, game["id"], friend, other.id)).status_code == 403  # не хост
    assert (await kick(lobby, game["id"], host, host.id)).status_code == 409  # себя нельзя

    assert hub.connections(game["id"]) == 2


# --- мир приключения (данные мастера формата v2, спека DND-2 §8 «Видимость») ---

LORE_MARK = "МИР-МАСТЕРА-"


async def test_no_answer_of_the_catalog_and_the_lobby_carries_the_world_of_the_adventure(
    settings, db
):
    # В каталоге — демо, где каждый текст мира, богов, фракций, связей и тайн мест несёт метку.
    folder = settings.content_dir / "demo-crypt"
    data = trap_lore(load_adventure(folder).model_dump(mode="json"), LORE_MARK)
    shutil.rmtree(folder)
    dump_adventure(adventure_from_data(data), folder)
    async with start_lobby(settings, db) as lobby:
        host = await add_host(lobby)
        answers = [await lobby.http.get("/api/adventures", headers=host.headers)]
        answers.append(await create_game(lobby, host))
        game = answers[-1].json()
        friend = await add_friend(lobby)
        answers.append(await join(lobby, friend, game["code"]))
        for player in (host, friend):
            await give_character(db, game["id"], player)
            answers.append(await mark_ready(lobby, game["id"], player))
        answers.append(await lobby.http.get("/api/games", headers=friend.headers))
        answers.append(await lobby.http.get(f"/api/games/{game['id']}", headers=friend.headers))
        answers.append(
            await lobby.http.post(f"/api/games/{game['id']}/start", headers=host.headers)
        )
        stored = await stored_game(db, game["id"])

    assert [answer.status_code for answer in answers] == [200, 201, 200, 200, 200, 200, 200, 200]
    assert LORE_MARK in json.dumps(stored.adventure_data, ensure_ascii=False)  # игра на этой копии
    seen = [answer.text for answer in answers]
    seen += [json.dumps(payload, ensure_ascii=False) for _, payload, _ in lobby.broadcaster.sent]
    assert LORE_MARK not in " ".join(seen)
