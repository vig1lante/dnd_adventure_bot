"""Тесты фабрики провайдеров (план B5.5, спека §2, §7, §14).

Живых моделей, сети и процессов `claude` здесь нет: фабрика только собирает адаптеры, а любой
сетевой вызов или запуск процесса при сборке — ошибка теста. Адаптеры и SDK-клиенты подменяются
шпионами, которые передают вызов настоящему классу и запоминают именованные аргументы: так
проверяется стык «настройки -> конструктор адаптера», который и есть работа фабрики.
"""

import asyncio
import inspect
import socket
import traceback
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from pydantic import SecretStr

from dnd_server.config import Settings
from dnd_server.llm import factory
from dnd_server.llm.base import TurnRequest
from dnd_server.llm.claude_code_cli import ClaudeCodeCliProvider
from dnd_server.llm.factory import KEYLESS_API_KEY, ConfigError, make_provider
from dnd_server.llm.fake import FakeProvider
from dnd_server.llm.internal_api import TurnTokens

ANTHROPIC_KEY = "sk-ant-api03-factory-secret"
COMPAT_KEY = "sk-compat-factory-secret"
SESSION_SECRET = "session-factory-secret"
BOT_TOKEN = "123456:bot-factory-secret"
ALL_SECRETS = (ANTHROPIC_KEY, COMPAT_KEY, SESSION_SECRET, BOT_TOKEN)
INTERNAL_URL = "http://127.0.0.1:8000"
CLAUDE_DEV_ONLY = "claude_code_cli доступен только в dev-режиме"

SCENARIO = """\
turns:
  - steps:
      - tool: request_check
        args: {character_id: "$pc2"}
      - text: "Тени скрывают вас…"
"""


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    """Сборка провайдера не ходит в сеть и не запускает процессы: иначе это ошибка теста."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"фабрика вышла за пределы памяти: {args}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)


OWNER_MARK = "owner-label"  # метка в значениях окружения владельца: чужому серверу её видеть нельзя
COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "llama-test",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Рассказ.", "refusal": None},
        }
    ],
}
MESSAGE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-test-model",
    "content": [{"type": "text", "text": "Рассказ."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class Wire:
    """HTTP-слой вместо сети: запоминает запросы, которые клиент SDK отправил бы серверу."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body = COMPLETION if request.url.path.endswith("/chat/completions") else MESSAGE
        return httpx2.Response(200, json=body)

    @property
    def only_request_text(self) -> str:
        """Адрес, имена и значения заголовков единственного запроса, одним текстом без регистра."""
        [request] = self.requests
        headers = "\n".join(f"{name}: {value}" for name, value in request.headers.items())
        return f"{request.url}\n{headers}".lower()


@pytest.fixture
def wire(monkeypatch) -> Wire:
    """Клиенты SDK фабрики получают транспорт-заглушку, остальные аргументы остаются её."""
    wire = Wire()
    real_openai, real_anthropic = factory.AsyncOpenAI, factory.AsyncAnthropic

    def openai_client(**kwargs: Any) -> openai.AsyncOpenAI:
        client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wire.handle))
        return real_openai(**kwargs, http_client=client)

    def anthropic_client(**kwargs: Any) -> anthropic.AsyncAnthropic:
        client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wire.handle))
        return real_anthropic(**kwargs, http_client=client)

    monkeypatch.setattr(factory, "AsyncOpenAI", openai_client)
    monkeypatch.setattr(factory, "AsyncAnthropic", anthropic_client)
    return wire


class Spy:
    """Подмена класса в `factory`: создаёт настоящий объект и запоминает переданные аргументы.

    Значения по умолчанию не подставляются: тест видит только то, что фабрика задала явно."""

    def __init__(self, real: type) -> None:
        self._real = real
        self.calls: list[dict[str, Any]] = []
        self.created: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        bound = inspect.signature(self._real).bind(*args, **kwargs)
        self.calls.append(dict(bound.arguments))
        instance = self._real(*args, **kwargs)
        self.created.append(instance)
        return instance

    @property
    def only(self) -> dict[str, Any]:
        [call] = self.calls
        return call


@pytest.fixture
def spy(monkeypatch):
    def install(name: str) -> Spy:
        spied = Spy(getattr(factory, name))
        monkeypatch.setattr(factory, name, spied)
        return spied

    return install


def make_settings(**values: Any) -> Settings:
    return Settings(**values)


def anthropic_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "anthropic_api",
        "llm_model": "claude-test-model",
        "llm_effort": "high",
        "anthropic_api_key": ANTHROPIC_KEY,
    }
    return make_settings(**(values | overrides))


def compat_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "openai_compatible",
        "openai_compat_base_url": "http://localhost:11434/v1",
        "openai_compat_model": "llama-test",
        "openai_compat_api_key": COMPAT_KEY,
    }
    return make_settings(**(values | overrides))


def cli_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "claude_code_cli",
        "app_env": "dev",
        "claude_cli_path": "/opt/claude",
    }
    return make_settings(**(values | overrides))


def fake_settings(tmp_path, text: str = SCENARIO, **overrides: Any) -> Settings:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(text, encoding="utf-8")
    return make_settings(**({"llm_provider": "fake", "fake_llm_scenario": scenario} | overrides))


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return "ok", False


EMPTY_TURN = TurnRequest(system="", adventure_brief="", context="", tools=())


# --- fake ---


async def test_fake_plays_scenario_with_pc_ids_of_the_game(tmp_path):
    provider = make_provider(fake_settings(tmp_path), pc_ids=["pc-a", "pc-b"])
    tools = RecordingTools()

    result = await provider.run_turn(EMPTY_TURN, tools)

    assert isinstance(provider, FakeProvider)
    assert provider.name == "fake"
    assert tools.calls == [("request_check", {"character_id": "pc-b"})]
    assert result.text == "Тени скрывают вас…"


async def test_fake_providers_of_different_games_have_own_turn_queues(tmp_path):
    settings = fake_settings(tmp_path)
    first = make_provider(settings, pc_ids=["a1", "a2"])
    second = make_provider(settings, pc_ids=["b1", "b2"])
    first_tools, second_tools = RecordingTools(), RecordingTools()

    await first.run_turn(EMPTY_TURN, first_tools)
    await second.run_turn(EMPTY_TURN, second_tools)

    assert first_tools.calls == [("request_check", {"character_id": "a2"})]
    assert second_tools.calls == [("request_check", {"character_id": "b2"})]


def test_fake_without_scenario_is_config_error():
    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO"):
        make_provider(make_settings(llm_provider="fake"))


def test_fake_with_missing_scenario_file_is_config_error(tmp_path):
    settings = make_settings(llm_provider="fake", fake_llm_scenario=tmp_path / "нет.yaml")

    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO"):
        make_provider(settings)


@pytest.mark.parametrize(
    "text",
    [
        "turns: [это не закрыто",  # битый YAML
        "turns: []\nlost_field: 1\n",  # поле вне схемы сценария
        "",  # пустой файл
    ],
)
def test_fake_with_broken_scenario_is_config_error(tmp_path, text):
    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO"):
        make_provider(fake_settings(tmp_path, text))


SCENARIO_SECRET = "sk-LEAK-scenario"
# Файл, на который FAKE_LLM_SCENARIO мог указать по ошибке (например, на .env или конфиг с ключом):
# сценарий не читается, а секрет рядом с ошибкой не должен попасть ни в текст, ни в цепочку.
MISLEADING_SCENARIO_FILES = [
    pytest.param(
        f"APP_ENV=dev\nANTHROPIC_API_KEY={SCENARIO_SECRET}\n".encode(),
        id="строки KEY=value (не отображение)",
    ),
    pytest.param(f"turns: [{SCENARIO_SECRET}".encode(), id="битый YAML, секрет у ошибки"),
    pytest.param(
        f"turns: []\napi_key: {SCENARIO_SECRET}\n".encode(), id="лишнее поле с секретом в значении"
    ),
    pytest.param(f"turns: {SCENARIO_SECRET}\n".encode(), id="секрет вместо списка ходов"),
    pytest.param(f"turns: []\n!{SCENARIO_SECRET} 1\n".encode(), id="секрет в имени тега YAML"),
    pytest.param(b"\xff\xfe" + SCENARIO_SECRET.encode(), id="не UTF-8"),
]


@pytest.mark.parametrize("content", MISLEADING_SCENARIO_FILES)
def test_scenario_file_content_never_reaches_the_config_error(tmp_path, content):
    scenario = tmp_path / "scenario.yaml"
    scenario.write_bytes(content)

    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO") as error:
        make_provider(make_settings(llm_provider="fake", fake_llm_scenario=scenario))

    story = "".join(traceback.format_exception(error.value)) + repr(error.value)
    assert SCENARIO_SECRET not in story
    # Исходное исключение несёт содержимое файла: ни причины, ни контекста у ошибки быть не должно.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


# Конструкторы значений PyYAML на чужом вводе бросают не только `YAMLError`: `!!int ""` даёт
# `IndexError`, `!!bool x` — `KeyError`, `!!timestamp x` — `AttributeError`, длинное число и
# несуществующая дата — `ValueError`; в тексте последних значение из файла (секрет в маркере).
# Сервер при старте обязан ответить `ConfigError`, а не трассировкой, и не унести значение.
UNBUILDABLE_VALUES = {
    "число-длиннее-4300-цифр": "9" * 4301,
    "несуществующая-дата": "2023-02-30",
    "несуществующее-время": "2023-02-28 25:61:00",
    "int-пустая-строка": '!!int ""',
    "float-пустая-строка": '!!float ""',
    "bool-не-литерал": "!!bool sk-LEAK-bool",
    "timestamp-не-дата": "!!timestamp sk-LEAK-time",
    "int-не-число": "!!int sk-LEAK-int",
    "float-не-число": "!!float sk-LEAK-float",
    "смещение-пояса-вне-суток": "2001-12-14t21:59:43.10+99:00",
    "глубокая-вложенность": "[" * 5000 + "]" * 5000,
}


@pytest.mark.parametrize("value", UNBUILDABLE_VALUES.values(), ids=UNBUILDABLE_VALUES.keys())
def test_a_value_yaml_cannot_build_is_a_config_error_without_the_value(tmp_path, value):
    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO") as error:
        make_provider(fake_settings(tmp_path, f"turns: {value}\n"))

    story = "".join(traceback.format_exception(error.value)) + repr(error.value)
    assert "sk-leak" not in story.lower()
    # Исходное исключение несёт значение из файла: ни причины, ни контекста у ошибки быть не должно.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("turns: []\napi_key: 1\n", r"api_key: Extra inputs are not permitted"),
        ("turns:\n  - steps: [\n", r"ParserError, строка \d+, столбец \d+"),
        ("turns: []\n\tjson: {}\n", r"ScannerError, строка 2, столбец 1"),
    ],
)
def test_scenario_errors_say_where_the_problem_is_without_quoting_the_file(
    tmp_path, text, expected
):
    with pytest.raises(ConfigError, match=expected):
        make_provider(fake_settings(tmp_path, text))


# --- anthropic_api ---


def test_anthropic_builds_provider_from_settings(spy):
    clients, providers = spy("AsyncAnthropic"), spy("AnthropicProvider")
    settings = anthropic_settings(llm_cheap_model="claude-cheap-test")

    provider = make_provider(settings)

    assert provider is providers.created[0]
    assert provider.name == "anthropic_api"
    assert clients.only["api_key"] == ANTHROPIC_KEY
    assert providers.only == {
        "client": clients.created[0],
        "model": "claude-test-model",
        "effort": "high",
        "cheap_model": "claude-cheap-test",
    }


def test_anthropic_client_has_explicit_timeout_and_retries(spy):
    clients = spy("AsyncAnthropic")

    make_provider(anthropic_settings())

    # Тайм-аут запроса не больше тайм-аута хода (180 с), повторы 429/5xx делает SDK (спека §12).
    assert 0 < clients.only["timeout"] <= 180
    assert clients.only["max_retries"] == 2


async def test_anthropic_request_goes_to_anthropic_with_own_key_only(wire, monkeypatch):
    # SDK сам читает адрес из ANTHROPIC_BASE_URL (ключ ушёл бы на другой сервер), а токен
    # ANTHROPIC_AUTH_TOKEN добавил бы второе средство входа: ничего из этого фабрика не задаёт.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://{OWNER_MARK}.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", f"token-{OWNER_MARK}")
    provider = make_provider(anthropic_settings())

    await provider.run_turn(EMPTY_TURN, RecordingTools())

    [request] = wire.requests
    assert request.url.host == "api.anthropic.com"
    assert request.headers["x-api-key"] == ANTHROPIC_KEY
    assert "authorization" not in request.headers
    assert OWNER_MARK not in wire.only_request_text


@pytest.mark.parametrize(
    ("configured", "override"),
    [("anthropic_api", None), ("fake", "anthropic_api")],
    ids=["настройки", "переопределение игры"],
)
def test_anthropic_refuses_headers_from_environment_without_quoting_them(
    monkeypatch, configured, override
):
    # Такие заголовки SDK добавил бы к каждому запросу, а `x-api-key` из них заменил бы ключ.
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", f"x-api-key: {OWNER_MARK}\nX-Owner: {OWNER_MARK}"
    )

    with pytest.raises(ConfigError, match="ANTHROPIC_CUSTOM_HEADERS") as error:
        make_provider(anthropic_settings(llm_provider=configured), provider=override)

    assert OWNER_MARK not in f"{error.value} {error.value!r}"


def test_anthropic_accepts_empty_headers_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "")

    assert make_provider(anthropic_settings()).name == "anthropic_api"


def test_anthropic_game_override_replaces_provider_and_model(spy):
    providers = spy("AnthropicProvider")
    settings = compat_settings(anthropic_api_key=ANTHROPIC_KEY, llm_model="claude-default")

    provider = make_provider(settings, provider="anthropic_api", model="claude-host-choice")

    assert provider.name == "anthropic_api"
    assert providers.only["model"] == "claude-host-choice"


def test_anthropic_without_key_is_config_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_provider(anthropic_settings(anthropic_api_key=None))


def test_anthropic_with_empty_key_is_config_error():
    # Пустая строка в env настройки уже снимает; пустой SecretStr фабрика отвергает сама.
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_provider(anthropic_settings(anthropic_api_key=SecretStr("")))


def test_anthropic_game_override_without_key_is_config_error():
    settings = make_settings(llm_provider="fake")

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_provider(settings, provider="anthropic_api")


def test_anthropic_unknown_effort_is_config_error():
    with pytest.raises(ConfigError, match="LLM_EFFORT"):
        make_provider(anthropic_settings(llm_effort="turbo"))


# --- openai_compatible ---


def test_openai_compatible_builds_provider_from_settings(spy):
    clients, providers = spy("AsyncOpenAI"), spy("OpenAICompatibleProvider")

    provider = make_provider(compat_settings())

    assert provider is providers.created[0]
    assert provider.name == "openai_compatible"
    assert clients.only["base_url"] == "http://localhost:11434/v1"
    assert clients.only["api_key"] == COMPAT_KEY
    assert providers.only["client"] is clients.created[0]
    assert providers.only["model"] == "llama-test"


def test_openai_compatible_client_has_explicit_timeout_and_retries(spy):
    clients = spy("AsyncOpenAI")

    make_provider(compat_settings())

    assert 0 < clients.only["timeout"] <= 180
    assert clients.only["max_retries"] == 2


def test_openai_compatible_does_not_get_anthropic_cheap_model(spy):
    providers = spy("OpenAICompatibleProvider")

    make_provider(compat_settings(llm_cheap_model="claude-haiku-test"))

    assert providers.only.get("cheap_model") is None


def test_openai_compatible_does_not_use_anthropic_model_setting(spy):
    providers = spy("OpenAICompatibleProvider")

    make_provider(compat_settings(llm_model="claude-default"))

    assert providers.only["model"] == "llama-test"


def test_openai_compatible_game_override_model_wins(spy):
    providers = spy("OpenAICompatibleProvider")

    make_provider(compat_settings(), model="qwen-host-choice")

    assert providers.only["model"] == "qwen-host-choice"


def test_openai_compatible_game_override_model_replaces_missing_setting(spy):
    providers = spy("OpenAICompatibleProvider")

    make_provider(compat_settings(openai_compat_model=None), model="qwen-host-choice")

    assert providers.only["model"] == "qwen-host-choice"


def test_openai_compatible_game_override_provider_uses_compat_settings(spy):
    providers = spy("OpenAICompatibleProvider")
    settings = compat_settings(llm_provider="anthropic_api")

    provider = make_provider(settings, provider="openai_compatible")

    assert provider.name == "openai_compatible"
    assert providers.only["model"] == "llama-test"


def test_openai_compatible_without_key_uses_placeholder_not_environment(spy, monkeypatch):
    # Локальный сервер (Ollama, LM Studio) ключа не требует. Без своего значения SDK взял бы
    # OPENAI_API_KEY из окружения и отправил его чужому серверу.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-openai-key")
    clients = spy("AsyncOpenAI")

    make_provider(compat_settings(openai_compat_api_key=None))

    assert clients.only["api_key"]
    assert clients.only["api_key"] != "sk-owner-openai-key"


@pytest.mark.parametrize("key", [COMPAT_KEY, None], ids=["ключ из настроек", "без ключа: заглушка"])
async def test_openai_compatible_request_carries_nothing_from_owner_environment(
    wire, monkeypatch, key
):
    # SDK сам читает эти переменные: организация и проект ушли бы заголовками чужому серверу,
    # OPENAI_BASE_URL увёл бы запрос на другой адрес, OPENAI_API_KEY подменил бы ключ.
    monkeypatch.setenv("OPENAI_ORG_ID", f"org-{OWNER_MARK}")
    monkeypatch.setenv("OPENAI_PROJECT_ID", f"proj-{OWNER_MARK}")
    monkeypatch.setenv("OPENAI_API_KEY", f"sk-{OWNER_MARK}")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://{OWNER_MARK}.invalid/v1")
    provider = make_provider(compat_settings(openai_compat_api_key=key))

    await provider.run_turn(EMPTY_TURN, RecordingTools())

    [request] = wire.requests
    assert request.url.host == "localhost"
    assert request.headers["authorization"] == f"Bearer {key or KEYLESS_API_KEY}"
    assert OWNER_MARK not in wire.only_request_text


@pytest.mark.parametrize(
    ("configured", "override"),
    [("openai_compatible", None), ("fake", "openai_compatible")],
    ids=["настройки", "переопределение игры"],
)
def test_openai_compatible_refuses_headers_from_environment_without_quoting_them(
    monkeypatch, configured, override
):
    # SDK добавил бы эти заголовки к каждому запросу, а `Authorization` из них заменил бы ключ.
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", f"Authorization: Bearer {OWNER_MARK}\nX-Owner: {OWNER_MARK}"
    )

    with pytest.raises(ConfigError, match="OPENAI_CUSTOM_HEADERS") as error:
        make_provider(compat_settings(llm_provider=configured), provider=override)

    assert OWNER_MARK not in f"{error.value} {error.value!r}"


def test_openai_compatible_accepts_empty_headers_variable(monkeypatch):
    # Пустая переменная заголовков не добавляет, и отказывать нечему.
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "")

    assert make_provider(compat_settings()).name == "openai_compatible"


def test_openai_headers_variable_does_not_stop_other_providers(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", f"X-Owner: {OWNER_MARK}")

    assert make_provider(anthropic_settings()).name == "anthropic_api"
    assert make_provider(fake_settings(tmp_path)).name == "fake"


@pytest.mark.parametrize(
    ("missing", "variable"),
    [
        ("openai_compat_base_url", "OPENAI_COMPAT_BASE_URL"),
        ("openai_compat_model", "OPENAI_COMPAT_MODEL"),
    ],
)
def test_openai_compatible_requires_base_url_and_model(missing, variable):
    with pytest.raises(ConfigError, match=variable):
        make_provider(compat_settings(**{missing: None}))


@pytest.mark.parametrize("base_url", ["localhost:11434/v1", "ftp://llm.example.test", "http://"])
def test_openai_compatible_rejects_base_url_without_http_address(base_url):
    with pytest.raises(ConfigError, match="OPENAI_COMPAT_BASE_URL"):
        make_provider(compat_settings(openai_compat_base_url=base_url))


def test_openai_compatible_ignores_anthropic_effort_setting():
    provider = make_provider(compat_settings(llm_effort="turbo"))

    assert provider.name == "openai_compatible"


# --- claude_code_cli ---


def test_claude_cli_builds_provider_in_dev(spy):
    providers = spy("ClaudeCodeCliProvider")
    tokens = TurnTokens()

    provider = make_provider(cli_settings(), turn_tokens=tokens, internal_url=INTERNAL_URL)

    assert isinstance(provider, ClaudeCodeCliProvider)
    assert provider.name == "claude_code_cli"
    assert providers.only["tokens"] is tokens
    assert providers.only["internal_url"] == INTERNAL_URL
    assert providers.only["cli_path"] == "/opt/claude"


def test_claude_cli_game_override_works_in_dev():
    settings = anthropic_settings(app_env="dev")

    provider = make_provider(
        settings, provider="claude_code_cli", turn_tokens=TurnTokens(), internal_url=INTERNAL_URL
    )

    assert provider.name == "claude_code_cli"


def test_claude_cli_is_refused_in_prod():
    settings = cli_settings(app_env="prod")

    with pytest.raises(ConfigError) as error:
        make_provider(settings, turn_tokens=TurnTokens(), internal_url=INTERNAL_URL)

    assert str(error.value) == CLAUDE_DEV_ONLY


def test_claude_cli_is_refused_in_prod_by_default_environment():
    # APP_ENV не задан: сервер работает как prod (config.py), и claude_code_cli закрыт.
    settings = make_settings(llm_provider="claude_code_cli")

    with pytest.raises(ConfigError, match=CLAUDE_DEV_ONLY):
        make_provider(settings, turn_tokens=TurnTokens(), internal_url=INTERNAL_URL)


def test_claude_cli_game_override_is_refused_in_prod():
    settings = anthropic_settings(app_env="prod")

    with pytest.raises(ConfigError, match=CLAUDE_DEV_ONLY):
        make_provider(
            settings,
            provider="claude_code_cli",
            turn_tokens=TurnTokens(),
            internal_url=INTERNAL_URL,
        )


def test_claude_cli_without_turn_tokens_is_config_error():
    with pytest.raises(ConfigError, match="claude_code_cli"):
        make_provider(cli_settings(), internal_url=INTERNAL_URL)


@pytest.mark.parametrize("internal_url", [None, ""])
def test_claude_cli_without_internal_url_is_config_error(internal_url):
    with pytest.raises(ConfigError, match="claude_code_cli"):
        make_provider(cli_settings(), turn_tokens=TurnTokens(), internal_url=internal_url)


def test_claude_cli_ignores_model_effort_and_cheap_model():
    # Модель для CLI задаёт подписка: неверный LLM_EFFORT и модель игры сборку не ломают.
    settings = cli_settings(llm_model="claude-x", llm_effort="turbo", llm_cheap_model="claude-y")

    provider = make_provider(
        settings, model="host-model", turn_tokens=TurnTokens(), internal_url=INTERNAL_URL
    )

    assert provider.name == "claude_code_cli"


# --- общие правила ---


def test_unknown_provider_in_settings_is_config_error():
    with pytest.raises(ConfigError, match="неизвестный провайдер 'gpt-magic'"):
        make_provider(make_settings(llm_provider="gpt-magic"))


def test_unknown_provider_in_game_override_is_config_error():
    with pytest.raises(ConfigError, match="неизвестный провайдер 'gpt-magic'"):
        make_provider(make_settings(llm_provider="fake"), provider="gpt-magic")


@pytest.mark.parametrize("provider", ["anthropic_api", "openai_compatible", "claude_code_cli"])
def test_creation_makes_no_network_calls_or_processes(provider):
    # Ловушки сети и процессов стоят в автоматической фикстуре `no_io`: любая попытка
    # выйти наружу при сборке роняет тест. Провайдеры собираются из полного набора настроек.
    settings = make_settings(
        app_env="dev",
        anthropic_api_key=ANTHROPIC_KEY,
        openai_compat_base_url="https://llm.example.test/v1",
        openai_compat_model="m",
        openai_compat_api_key=COMPAT_KEY,
    )

    built = make_provider(
        settings, provider=provider, turn_tokens=TurnTokens(), internal_url=INTERNAL_URL
    )

    assert built.name == provider


def secret_settings(**overrides: Any) -> Settings:
    """Все четыре секрета заданы, чтобы любая ветка ошибки могла бы их вывести."""
    secrets = {
        "session_secret": SESSION_SECRET,
        "telegram_bot_token": BOT_TOKEN,
        "anthropic_api_key": ANTHROPIC_KEY,
        "openai_compat_api_key": COMPAT_KEY,
    }
    return make_settings(**(secrets | overrides))


@pytest.mark.parametrize(
    "settings",
    [
        secret_settings(llm_provider="anthropic_api", anthropic_api_key=None),
        secret_settings(llm_provider="anthropic_api", llm_effort="turbo"),
        secret_settings(llm_provider="openai_compatible", openai_compat_base_url="http://x/v1"),
        secret_settings(
            llm_provider="openai_compatible", openai_compat_base_url="oops", openai_compat_model="m"
        ),
        secret_settings(llm_provider="claude_code_cli", app_env="prod"),
        secret_settings(llm_provider="claude_code_cli", app_env="dev"),
        secret_settings(llm_provider="fake"),
        secret_settings(llm_provider="nope"),
    ],
)
def test_config_errors_never_contain_secrets(settings):
    with pytest.raises(ConfigError) as error:
        make_provider(settings)

    text = f"{error.value} {error.value!r}"
    assert not any(secret in text for secret in ALL_SECRETS)


@pytest.mark.parametrize("settings", [anthropic_settings(), compat_settings()])
def test_provider_repr_does_not_contain_api_keys(settings):
    provider = make_provider(settings)

    assert not any(secret in repr(provider) for secret in ALL_SECRETS)
