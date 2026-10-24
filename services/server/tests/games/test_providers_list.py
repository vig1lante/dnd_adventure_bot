"""Список провайдеров и моделей для создателей игр (план B8.s0, решение владельца R4).

Форма «Создать игру» в Mini App показывает создателям выбор нейронки для игры. `GET /api/providers`
отдаёт варианты: какие провайдеры есть, как они называются, какая модель у них по умолчанию и
настроены ли они. Секретов в ответе нет вовсе, а `claude_code_cli` и `fake` показываются только
в dev. «Доступен» значит то же, что «игру с этим провайдером создать получится»: оба ответа берутся
из одной проверки (`games.service.check_provider`). Настоящие модели не вызываются, а ключи в тестах
выдуманные строки.
"""

import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.games.providers import CATALOG
from dnd_server.games.schemas import CreateGameIn
from dnd_server.llm.factory import PROVIDERS

# tests/games/test_providers_list.py -> games -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "basic_round.yaml"
SESSION_SECRET = "providers-session-secret-0123456789-abcdef"
BOT_TOKEN = "123456789:AAF-providers-bot-token-0123456789_abcdef"
CREATOR_TG_ID = 700
OUTSIDER_TG_ID = 701
ANTHROPIC_KEY = "sk-ant-LEAK-providers-0001"
COMPAT_KEY = "sk-compat-LEAK-providers-0002"
COMPAT_URL = "http://leaky-user:leaky-pass@llm.example.test:8080/v1"
ENTRY_FIELDS = {
    "id",
    "title",
    "available",
    "default_model",
    "models",
    "accepts_model",
    "is_default",
}
RUSSIAN = re.compile("[а-яё]", re.IGNORECASE)
REAL = ["anthropic_api", "openai_compatible"]
DEV_ONLY = ["claude_code_cli", "fake"]


class Player(NamedTuple):
    id: str
    headers: dict[str, str]


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    shutil.copytree(DEMO_ADVENTURE_DIR, directory / "demo-crypt")
    return directory


@pytest.fixture
def settings(settings: Settings, content_dir: Path) -> Settings:
    """Dev-настройки из conftest без ключей и сценария: каждый тест настраивает своё."""
    return settings.model_copy(
        update={
            "content_dir": content_dir,
            "session_secret": SecretStr(SESSION_SECRET),
            "telegram_bot_token": SecretStr(BOT_TOKEN),
            "game_creator_tg_ids": (CREATOR_TG_ID,),
            "fake_llm_scenario": None,
        }
    )


@asynccontextmanager
async def serve(settings: Settings, **changes: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings.model_copy(update=changes))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield http


async def add_player(
    db: async_sessionmaker[AsyncSession],
    *,
    dev_login: str | None = None,
    tg_id: int | None = None,
) -> Player:
    async with db() as session:
        user = await session.scalar(
            select(User).where(
                User.tg_id == tg_id if tg_id is not None else User.dev_login == dev_login
            )
        )
        if user is None:  # один и тот же игрок в нескольких приложениях теста — одна строка
            user = User(dev_login=dev_login, tg_id=tg_id, name="Игрок")
            session.add(user)
            await session.commit()
    token = SessionSigner(SESSION_SECRET).issue(user.id)
    return Player(user.id, {"Authorization": f"Bearer {token}"})


async def listing(
    http: httpx.AsyncClient, db: async_sessionmaker[AsyncSession]
) -> dict[str, dict[str, Any]]:
    """Список, как его видит создатель: записи по id провайдера."""
    creator = await add_player(db, tg_id=CREATOR_TG_ID)
    response = await http.get("/api/providers", headers=creator.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"providers"}
    return {entry["id"]: entry for entry in body["providers"]}


# --- доступ ---


async def test_the_list_needs_a_login(settings, db):
    async with serve(settings) as http:
        response = await http.get("/api/providers")

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


async def test_only_game_creators_get_the_list(settings, db):
    async with serve(settings) as http:
        outsider = await add_player(db, tg_id=OUTSIDER_TG_ID)
        creator = await add_player(db, tg_id=CREATOR_TG_ID)
        dev = await add_player(db, dev_login="dev-1")

        refused = await http.get("/api/providers", headers=outsider.headers)
        allowed = [await http.get("/api/providers", headers=who.headers) for who in (creator, dev)]

    assert refused.status_code == 403
    assert set(refused.json()) == {"detail"} and "списка владельца" in refused.json()["detail"]
    assert [r.status_code for r in allowed] == [200, 200]


async def test_a_dev_login_found_in_the_prod_database_is_not_a_creator(settings, db):
    async with serve(settings, app_env="prod") as http:
        dev = await add_player(db, dev_login="dev-1")

        response = await http.get("/api/providers", headers=dev.headers)

    assert response.status_code == 403


async def test_the_route_only_reads(settings, db):
    async with serve(settings) as http:
        creator = await add_player(db, tg_id=CREATOR_TG_ID)

        response = await http.post("/api/providers", headers=creator.headers, json={})

    assert response.status_code == 405


# --- какие провайдеры показываются ---


async def test_prod_lists_only_the_real_providers(settings, db):
    # Даже настроенные `fake` и `claude_code_cli` в prod не показываются: их там не принимает игра.
    async with serve(
        settings, app_env="prod", fake_llm_scenario=SCENARIO, llm_provider="anthropic_api"
    ) as http:
        found = await listing(http, db)

    assert list(found) == REAL


async def test_dev_also_lists_claude_code_cli_and_fake(settings, db):
    async with serve(settings) as http:
        found = await listing(http, db)

    assert list(found) == [*REAL, *DEV_ONLY]


def test_the_catalog_knows_exactly_the_providers_of_the_factory():
    # Новый провайдер фабрики без записи в списке (или наоборот) остановит этот тест.
    assert {provider.id for provider in CATALOG} == set(PROVIDERS)
    assert {provider.id for provider in CATALOG if provider.dev_only} == set(DEV_ONLY)


async def test_every_entry_has_the_documented_shape(settings, db):
    async with serve(settings) as http:
        found = await listing(http, db)

    for entry in found.values():
        assert set(entry) == ENTRY_FIELDS, entry["id"]
        assert isinstance(entry["available"], bool) and isinstance(entry["is_default"], bool)
        assert isinstance(entry["accepts_model"], bool)
        assert RUSSIAN.search(entry["title"]), entry["title"]
        assert isinstance(entry["models"], list)


# --- доступен ли провайдер ---


async def test_nothing_is_available_without_keys_except_the_dev_bridge(settings, db):
    async with serve(settings) as http:
        found = await listing(http, db)

    assert {i: e["available"] for i, e in found.items()} == {
        "anthropic_api": False,
        "openai_compatible": False,
        "claude_code_cli": True,  # ключей не требует; собирается в dev
        "fake": False,  # нет сценария
    }


async def test_the_anthropic_key_makes_anthropic_available(settings, db):
    async with serve(settings, anthropic_api_key=SecretStr(ANTHROPIC_KEY)) as http:
        found = await listing(http, db)

    assert found["anthropic_api"]["available"] is True
    assert found["openai_compatible"]["available"] is False


async def test_an_empty_key_is_the_same_as_no_key(settings, db):
    async with serve(settings, anthropic_api_key=SecretStr("")) as http:
        found = await listing(http, db)

    assert found["anthropic_api"]["available"] is False


@pytest.mark.parametrize(
    ("changes", "available"),
    [
        pytest.param({}, False, id="ничего"),
        pytest.param(
            {"openai_compat_base_url": COMPAT_URL, "openai_compat_model": "local-model"},
            True,
            id="адрес и модель",
        ),
        pytest.param({"openai_compat_base_url": COMPAT_URL}, False, id="нет модели"),
        pytest.param({"openai_compat_model": "local-model"}, False, id="нет адреса"),
        pytest.param(
            {"openai_compat_base_url": "ftp://example.test", "openai_compat_model": "m"},
            False,
            id="адрес не http",
        ),
    ],
)
async def test_an_openai_compatible_server_needs_an_address_and_a_model(
    settings, db, changes, available
):
    async with serve(settings, **changes) as http:
        found = await listing(http, db)

    assert found["openai_compatible"]["available"] is available


async def test_fake_is_available_with_a_scenario(settings, db):
    async with serve(settings, fake_llm_scenario=SCENARIO) as http:
        found = await listing(http, db)

    assert found["fake"]["available"] is True


CONFIGS = {
    "ничего": {},
    "anthropic": {"anthropic_api_key": SecretStr(ANTHROPIC_KEY)},
    "openai": {"openai_compat_base_url": COMPAT_URL, "openai_compat_model": "local-model"},
    "сценарий": {"fake_llm_scenario": SCENARIO},
    "всё": {
        "anthropic_api_key": SecretStr(ANTHROPIC_KEY),
        "openai_compat_base_url": COMPAT_URL,
        "openai_compat_model": "local-model",
        "fake_llm_scenario": SCENARIO,
    },
}


@pytest.mark.parametrize("app_env", ["dev", "prod"])
@pytest.mark.parametrize("config", CONFIGS)
async def test_available_means_the_game_can_be_created_with_it(settings, db, app_env, config):
    async with serve(settings, app_env=app_env, **CONFIGS[config]) as http:
        found = await listing(http, db)
        creator = await add_player(db, tg_id=CREATOR_TG_ID)

        for provider_id, entry in found.items():
            created = await http.post(
                "/api/games",
                headers=creator.headers,
                json={"adventure_id": "demo-crypt", "provider": provider_id},
            )
            assert created.status_code == (201 if entry["available"] else 422), (
                provider_id,
                created.text,
            )


# --- модели ---


async def test_models_come_from_the_settings(settings, db):
    async with serve(
        settings,
        llm_model="claude-test-model-1",
        openai_compat_model="local-model-2",
        openai_compat_base_url=COMPAT_URL,
    ) as http:
        found = await listing(http, db)

    assert found["anthropic_api"]["default_model"] == "claude-test-model-1"
    assert found["anthropic_api"]["models"] == ["claude-test-model-1"]
    assert found["openai_compatible"]["default_model"] == "local-model-2"
    assert found["openai_compatible"]["models"] == ["local-model-2"]
    assert (
        found["anthropic_api"]["accepts_model"],
        found["openai_compatible"]["accepts_model"],
    ) == (True, True)


async def test_an_unset_compatible_model_is_none_and_the_list_is_empty(settings, db):
    async with serve(settings) as http:
        found = await listing(http, db)

    assert found["openai_compatible"]["default_model"] is None
    assert found["openai_compatible"]["models"] == []


async def test_the_scenario_stub_ignores_the_model_and_does_not_offer_one(settings, db):
    async with serve(settings, llm_model="claude-test-model-1") as http:
        found = await listing(http, db)

    entry = found["fake"]
    assert (entry["default_model"], entry["models"], entry["accepts_model"]) == (None, [], False)


# --- модель подписки claude_code_cli (DND-2 B1.2, спека §11) ---

CLI_ALIASES = ["opus", "sonnet", "haiku"]


async def test_claude_code_cli_offers_the_aliases_of_the_cli_and_no_default(settings, db):
    async with serve(settings, llm_model="claude-test-model-1") as http:
        found = await listing(http, db)

    entry = found["claude_code_cli"]
    assert (entry["accepts_model"], entry["models"], entry["default_model"]) == (
        True,
        CLI_ALIASES,
        None,  # модель выберет сам CLI по подписке
    )


@pytest.mark.parametrize(
    ("cli_model", "models"),
    [
        pytest.param("sonnet[1m]", [*CLI_ALIASES, "sonnet[1m]"], id="другая модель"),
        pytest.param("sonnet", CLI_ALIASES, id="один из псевдонимов"),
    ],
)
async def test_claude_code_cli_offers_the_model_of_the_environment_as_its_default(
    settings, db, cli_model, models
):
    async with serve(settings, claude_cli_model=cli_model) as http:
        found = await listing(http, db)

    entry = found["claude_code_cli"]
    assert (entry["models"], entry["default_model"]) == (models, cli_model)


@pytest.mark.parametrize("cli_model", ["qwen2.5:14b", "[1m]", ".opus", "opus now"])
async def test_a_model_of_the_environment_the_cli_refuses_is_not_on_offer(settings, db, cli_model):
    # Такой CLAUDE_CLI_MODEL — ошибка настройки: провайдер недоступен, а в списке формы и в модели
    # по умолчанию его нет — адаптер всё равно не пустит его в командную строку. Остаются только
    # псевдонимы CLI, а модель по умолчанию пуста, как без CLAUDE_CLI_MODEL.
    async with serve(settings, claude_cli_model=cli_model) as http:
        found = await listing(http, db)

    entry = found["claude_code_cli"]
    assert (entry["available"], entry["models"], entry["default_model"]) == (
        False,
        CLI_ALIASES,
        None,
    )


async def test_every_model_on_offer_for_claude_code_cli_creates_a_game(settings, db):
    async with serve(settings, claude_cli_model="opus[1m]") as http:
        found = await listing(http, db)
        creator = await add_player(db, tg_id=CREATOR_TG_ID)

        statuses = {
            model: (
                await http.post(
                    "/api/games",
                    headers=creator.headers,
                    json={
                        "adventure_id": "demo-crypt",
                        "provider": "claude_code_cli",
                        "model": model,
                    },
                )
            ).status_code
            for model in found["claude_code_cli"]["models"]
        }

    assert statuses == {model: 201 for model in [*CLI_ALIASES, "opus[1m]"]}


@pytest.mark.parametrize(
    ("changes", "variable"),
    [
        pytest.param({"claude_cli_effort": "turbo-LEAK"}, "CLAUDE_CLI_EFFORT", id="усилия"),
        pytest.param(
            {"claude_cli_cheap_model": "haiku LEAK"}, "CLAUDE_CLI_CHEAP_MODEL", id="дешёвая"
        ),
    ],
)
async def test_a_wrong_subscription_setting_makes_claude_code_cli_unavailable_as_its_games(
    settings, db, changes, variable
):
    # «Доступен» и «игру создать получится» — одна проверка и с новыми CLAUDE_CLI_*: неверное
    # значение закрывает провайдера, игра с ним — 422 с именем переменной, но без значения.
    async with serve(settings, **changes) as http:
        found = await listing(http, db)
        creator = await add_player(db, tg_id=CREATOR_TG_ID)
        created = await http.post(
            "/api/games",
            headers=creator.headers,
            json={"adventure_id": "demo-crypt", "provider": "claude_code_cli", "model": "opus"},
        )

    assert found["claude_code_cli"]["available"] is False
    assert created.status_code == 422
    assert variable in created.json()["detail"]
    assert "LEAK" not in created.text and "LEAK" not in str(found)


async def test_claude_code_cli_is_not_offered_in_prod_whatever_its_models(settings, db):
    async with serve(settings, app_env="prod", claude_cli_model="opus") as http:
        found = await listing(http, db)

    assert "claude_code_cli" not in found


# Модель по умолчанию — одна из предложенных при любом CLAUDE_CLI_MODEL, в том числе таком, что
# адаптер CLI отвергает (тогда её нет ни в списке, ни в модели по умолчанию).
@pytest.mark.parametrize("cli_model", ["sonnet[1m]", "qwen2.5:14b"])
async def test_the_models_on_offer_pass_the_check_of_game_creation(settings, db, cli_model):
    async with serve(
        settings,
        llm_model="claude-test-model-1",
        openai_compat_model="local.model:7b",
        openai_compat_base_url=COMPAT_URL,
        claude_cli_model=cli_model,
    ) as http:
        found = await listing(http, db)

    for provider_id, entry in found.items():
        for model in entry["models"]:
            CreateGameIn(adventure_id="demo-crypt", provider=provider_id, model=model)
        assert entry["default_model"] in (None, *entry["models"])


# --- какой провайдер по умолчанию ---


@pytest.mark.parametrize("default", REAL)
async def test_the_servers_default_provider_is_marked(settings, db, default):
    async with serve(settings, llm_provider=default) as http:
        found = await listing(http, db)

    assert [i for i, e in found.items() if e["is_default"]] == [default]


async def test_the_default_provider_of_a_fresh_dev_server_is_fake(settings, db):
    async with serve(settings) as http:
        found = await listing(http, db)

    assert [i for i, e in found.items() if e["is_default"]] == ["fake"]


async def test_in_prod_a_default_that_is_not_listed_marks_nothing(settings, db):
    # Заводской `LLM_PROVIDER=fake` в prod не показывается, а значит и не помечается.
    async with serve(settings, app_env="prod") as http:
        found = await listing(http, db)

    assert not any(entry["is_default"] for entry in found.values())


# --- секреты ---


async def test_no_secret_reaches_the_answer_or_the_log(settings, db, caplog):
    secrets = [
        ANTHROPIC_KEY,
        COMPAT_KEY,
        "leaky-pass",
        "leaky-user",
        "llm.example.test",
        SESSION_SECRET,
        BOT_TOKEN,
        BOT_TOKEN.partition(":")[2],
    ]
    caplog.set_level("DEBUG")
    for app_env in ("dev", "prod"):
        async with serve(
            settings,
            app_env=app_env,
            anthropic_api_key=SecretStr(ANTHROPIC_KEY),
            openai_compat_api_key=SecretStr(COMPAT_KEY),
            openai_compat_base_url=COMPAT_URL,
            openai_compat_model="local-model",
            fake_llm_scenario=SCENARIO,
        ) as http:
            creator = await add_player(db, tg_id=CREATOR_TG_ID)

            response = await http.get("/api/providers", headers=creator.headers)

        assert response.status_code == 200
        seen = response.text + str(response.headers)
        assert [secret for secret in secrets if secret in seen] == []
    assert [secret for secret in secrets if secret in caplog.text] == []


async def test_the_reason_a_provider_is_unavailable_is_not_spelled_out(settings, db):
    # Тексты ошибок настройки называют переменные окружения и пути: в список они не идут.
    async with serve(settings) as http:
        creator = await add_player(db, tg_id=CREATOR_TG_ID)

        text = (await http.get("/api/providers", headers=creator.headers)).text

    for word in ("ANTHROPIC_API_KEY", "OPENAI_COMPAT", "FAKE_LLM_SCENARIO", "ConfigError"):
        assert word not in text
