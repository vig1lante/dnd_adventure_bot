"""Секреты из окружения не доходят до выводов, ошибок, журналов и ответов API.

В окружение кладутся узнаваемые значения четырёх секретов проекта; затем снаружи проверяется всё,
что видят люди и другие процессы: вывод `dnd-generate` и файлы приключения, ответы внутреннего
API, тексты ошибок адаптеров вместе с трассировкой, окружение дочернего `claude` на каждом
запуске и журналы. Отдельные проверки секретов есть и в тестах модулей; здесь — сквозные.
"""

import json
import shlex
import sys
import traceback
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import openai
import pytest
from pydantic import BaseModel

from dnd_server.app import create_app
from dnd_server.authoring.cli import main
from dnd_server.config import Settings
from dnd_server.llm.anthropic_api import AnthropicProvider
from dnd_server.llm.base import JsonRequest, ProviderError, ToolSchema, TurnRequest
from dnd_server.llm.claude_code_cli import ClaudeCodeCliProvider
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider

DEMO_SCENARIO = Path(__file__).resolve().parent / "scenarios" / "generator_demo.yaml"
FAKE_CLAUDE = Path(__file__).resolve().parent / "llm" / "fake_claude.py"
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-LEAK-anthropic",
    "OPENAI_COMPAT_API_KEY": "sk-compat-LEAK-openai",
    "SESSION_SECRET": "session-LEAK-secret",
    "TELEGRAM_BOT_TOKEN": "123456:LEAK-telegram",
}
CHECK_TOOL = ToolSchema("request_check", "Проверка.", {"type": "object", "properties": {}})
CLI_ARGS = ["--pitch", "Колокол звонит.", "--hours", "1", "--modules", "bg3_flavor,campfire"]


@pytest.fixture
def secret_env(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return SECRETS


def leaked(text: str) -> list[str]:
    """Какие из секретов встречаются в тексте."""
    return [name for name, value in SECRETS.items() if value in text]


# --- dnd-generate ---


def test_a_successful_generation_prints_and_writes_no_secret(
    secret_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(DEMO_SCENARIO))
    monkeypatch.setenv("APP_ENV", "dev")  # тестовый мастер собирается только в dev
    out = tmp_path / "chapel"

    code = main([*CLI_ARGS, "--out", str(out)])

    printed = capsys.readouterr()
    assert code == 0
    assert leaked(printed.out + printed.err) == []
    written = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert written and leaked(written) == []


def test_invalid_environment_is_reported_by_field_name_without_values(
    secret_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("APP_ENV", "staging")

    code = main([*CLI_ARGS, "--out", str(tmp_path / "out")])

    printed = capsys.readouterr()
    assert code == 2
    assert "app_env" in printed.err
    assert "staging" not in printed.err
    assert leaked(printed.out + printed.err) == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "provider",
    ["openai_compatible", "claude_code_cli", "fake", "no-such-provider"],
)
def test_configuration_errors_of_the_cli_print_no_secret(secret_env, tmp_path, capsys, provider):
    # Секреты заданы все; у выбранного провайдера не хватает настроек (или он закрыт в prod).
    code = main([*CLI_ARGS, "--provider", provider, "--out", str(tmp_path / "out")])

    printed = capsys.readouterr()
    assert code == 2
    assert printed.err.startswith("Ошибка:")
    assert leaked(printed.out + printed.err) == []


# --- внутренний API ---


async def test_internal_api_answers_and_the_rest_of_the_app_expose_no_secret():
    settings = Settings(
        app_env="dev",
        **{name.lower(): value for name, value in SECRETS.items()},
    )
    app = create_app(settings)

    async def executor(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        return "результат", False

    token = app.state.turn_tokens.issue(executor, (CHECK_TOOL,))
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    foreign = httpx.ASGITransport(app=app, client=("203.0.113.9", 4000))
    requests = [
        (local, "GET", f"/internal/dm-tools/{token}/tools", None),
        (local, "POST", f"/internal/dm-tools/{token}/call", {"name": "request_check", "args": {}}),
        (local, "POST", f"/internal/dm-tools/{token}/call", {"unexpected": "body"}),
        (local, "GET", "/internal/dm-tools/unknown/tools", None),
        (foreign, "GET", f"/internal/dm-tools/{token}/tools", None),
        (local, "GET", "/health", None),
        (local, "GET", "/openapi.json", None),
        (local, "GET", "/docs", None),
    ]

    seen = []
    for transport, method, path, body in requests:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.request(method, path, json=body)
        seen.append(f"{response.status_code} {response.headers} {response.text}")

    assert any(line.startswith("200") for line in seen)
    assert any(line.startswith("403") for line in seen)
    assert leaked("\n".join(seen)) == []


# --- адаптеры API ---


class Weaving(BaseModel):
    hooks: list[str] = []


EMPTY_TURN = TurnRequest(system="система", adventure_brief="сводка", context="контекст", tools=())
JSON_REQUEST = JsonRequest(system="система", prompt="запрос", purpose="weaving")


def anthropic_failing(message: str) -> AnthropicProvider:
    body = {"type": "error", "error": {"type": "authentication_error", "message": message}}
    client = anthropic.AsyncAnthropic(
        api_key=SECRETS["ANTHROPIC_API_KEY"],
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(401, json=body))
        ),
    )
    return AnthropicProvider(client, "claude-test", "medium")


def openai_failing(message: str) -> OpenAICompatibleProvider:
    error = {"message": message, "type": "invalid_request_error", "param": None, "code": None}
    client = openai.AsyncOpenAI(
        base_url="http://compat.invalid/v1",
        api_key=SECRETS["OPENAI_COMPAT_API_KEY"],
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(401, json={"error": error})
            )
        ),
    )
    return OpenAICompatibleProvider(client, "compat-model")


def whole_story(error: BaseException) -> str:
    """Всё, что покажет журнал с трассировкой: текст, repr и вся цепочка исключений."""
    return f"{error} {error!r} " + "".join(traceback.format_exception(error))


@pytest.mark.parametrize("call", ["run_turn", "complete_json"])
@pytest.mark.parametrize(
    ("make", "secret_name"),
    [
        (anthropic_failing, "ANTHROPIC_API_KEY"),
        (openai_failing, "OPENAI_COMPAT_API_KEY"),
    ],
    ids=["anthropic_api", "openai_compatible"],
)
async def test_a_gateway_that_echoes_the_key_does_not_put_it_into_any_error(
    make, secret_name, call
):
    provider = make(f"Incorrect API key provided: {SECRETS[secret_name]}")

    async def no_tools(name, args):
        raise AssertionError("инструменты не нужны")

    with pytest.raises(ProviderError) as caught:
        if call == "run_turn":
            await provider.run_turn(EMPTY_TURN, no_tools)
        else:
            await provider.complete_json(JSON_REQUEST, Weaving)

    assert "401" in str(caught.value)
    assert leaked(whole_story(caught.value)) == []
    assert SECRETS[secret_name] not in repr(provider)


# --- claude_code_cli ---


def cli_with_log(tmp_path: Path, **env: str) -> tuple[ClaudeCodeCliProvider, Path]:
    """Провайдер, чей `claude` — обёртка над имитатором; журнал имитатора лежит рядом."""
    log = tmp_path / "fake_claude.log"
    # Ссылка на общую обёртку, а не свой скрипт: новый исполняемый файл macOS долго проверяет.
    lines = [f"python={shlex.quote(sys.executable)}", f"script={shlex.quote(str(FAKE_CLAUDE))}"]
    lines += [
        f"export {name}={shlex.quote(value)}"
        for name, value in {"FAKE_CLAUDE_LOG": str(log), **env}.items()
    ]
    (tmp_path / "fake_claude.env").write_text("\n".join(lines) + "\n")
    wrapper = tmp_path / "claude"
    wrapper.symlink_to(FAKE_CLAUDE.with_name("fake_claude.sh"))
    provider = ClaudeCodeCliProvider(
        TurnTokens(), "http://127.0.0.1:8000", cli_path=str(wrapper), stop_grace_s=0.5
    )
    return provider, log


def launches(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


async def test_every_launch_of_claude_gets_an_environment_without_the_server_secrets(
    secret_env, tmp_path
):
    provider, log = cli_with_log(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps(['{"result": "{}"}']))

    await provider.complete_json(JSON_REQUEST, Weaving)  # запуск `--version` и запуск с `-p`

    runs = launches(log)
    assert any("--version" in run["argv"] for run in runs)
    assert any("-p" in run["argv"] for run in runs)
    for run in runs:
        assert not set(SECRETS) & set(run["env"]), run["argv"]
        assert leaked(json.dumps(run, ensure_ascii=False)) == []


async def test_a_failing_turn_leaves_the_turn_token_out_of_errors_and_logs(
    secret_env, tmp_path, caplog
):
    provider, log = cli_with_log(tmp_path, FAKE_CLAUDE_EXIT="1", FAKE_CLAUDE_STDERR="сбой {url}")

    async def executor(name, args):
        return "ок", False

    request = TurnRequest(
        system="система", adventure_brief="сводка", context="контекст", tools=(CHECK_TOOL,)
    )
    with caplog.at_level("DEBUG"), pytest.raises(ProviderError) as caught:
        await provider.run_turn(request, executor)

    run = next(run for run in launches(log) if "-p" in run["argv"])
    token = run["mcp_config"]["mcpServers"]["dm"]["args"][-1].rsplit("/", 1)[1]
    assert token and token not in whole_story(caught.value)
    assert token not in caplog.text
    assert leaked(caplog.text + whole_story(caught.value)) == []
