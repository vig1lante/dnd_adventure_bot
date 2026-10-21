"""Тесты адаптера `claude_code_cli`, внутреннего API инструментов и его подключения (план B5.4).

Настоящий `claude` здесь не запускается никогда (запрет проекта): вместо него скрипт-обёртка
вызывает имитатор `fake_claude.py`, который пишет в журнал `argv`, окружение и `cwd` и печатает
заданный JSON. Внутренний API проверяется через `httpx.ASGITransport` с заданным адресом клиента;
сквозной сценарий «CLI → внутренний API → исполнитель → трасса» идёт через настоящий HTTP на
локальном порту. Живых моделей и внешней сети нет.
"""

import asyncio
import json
import os
import shlex
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import BaseModel, ConfigDict, Field

from dnd_server.app import create_app
from dnd_server.config import Settings
from dnd_server.llm.base import (
    MAX_TRACE_ARGS_CHARS,
    JsonRequest,
    LLMProvider,
    ProviderError,
    ToolLoopExceeded,
    ToolSchema,
    TurnRequest,
    Usage,
)
from dnd_server.llm.claude_code_cli import ClaudeCodeCliProvider, ToolRecorder
from dnd_server.llm.internal_api import TurnTokens

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
FAKE_CLAUDE_LAUNCHER = FAKE_CLAUDE.with_name("fake_claude.sh")
REPO_ROOT = Path(__file__).resolve().parents[4]
LOCALHOST = ("127.0.0.1", 50000)
FORWARDING_HEADERS = ["X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP"]
SECRET_ENV = {
    "ANTHROPIC_API_KEY": "sentinel-anthropic-key",
    "TELEGRAM_BOT_TOKEN": "sentinel-telegram-token",
    "SESSION_SECRET": "sentinel-session-secret",
    "OPENAI_COMPAT_API_KEY": "sentinel-openai-key",
    "AWS_SECRET_ACCESS_KEY": "sentinel-aws-secret",
}
CHECK_TOOL = ToolSchema(
    name="request_check",
    description="Запросить проверку навыка.",
    input_schema={"type": "object", "properties": {"dc": {"type": "integer"}}},
)


@pytest.fixture(autouse=True)
def only_loopback(monkeypatch):
    """Подстраховка: в этих тестах разрешены соединения только с локальной машиной."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise AssertionError(f"тест пытался выйти в сеть: {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


# --- помощники: имитатор CLI и провайдер ---


def make_cli(tmp_path: Path, **env: str) -> tuple[str, Path]:
    """`claude`, который запускает имитатор с заданным поведением; журнал — рядом.

    Это ссылка на общую обёртку `fake_claude.sh` и её переменные в `fake_claude.env`: свой
    исполняемый файл на каждый тест macOS проверял бы при первом запуске (~0.2 с)."""
    log = tmp_path / "fake_claude.log"
    variables = {"FAKE_CLAUDE_LOG": str(log), **env}
    lines = [f"python={shlex.quote(sys.executable)}", f"script={shlex.quote(str(FAKE_CLAUDE))}"]
    lines += [f"export {name}={shlex.quote(value)}" for name, value in variables.items()]
    (tmp_path / "fake_claude.env").write_text("\n".join(lines) + "\n")
    wrapper = tmp_path / "claude"
    wrapper.unlink(missing_ok=True)
    wrapper.symlink_to(FAKE_CLAUDE_LAUNCHER)
    return str(wrapper), log


class Harness:
    """Провайдер, настроенный на имитатор CLI, его реестр токенов и журнал запусков."""

    def __init__(
        self,
        tmp_path: Path,
        internal_url: str = "http://127.0.0.1:8000",
        stop_grace_s: float = 0.5,
        tokens: TurnTokens | None = None,
        **cli_env: str,
    ) -> None:
        cli_path, self.log = make_cli(tmp_path, **cli_env)
        self.tokens = tokens if tokens is not None else TurnTokens()
        self.internal_url = internal_url
        self.provider = ClaudeCodeCliProvider(
            self.tokens, internal_url, cli_path=cli_path, stop_grace_s=stop_grace_s
        )

    def entries(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def prompt_runs(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries() if "-p" in entry["argv"]]

    def only_run(self) -> dict[str, Any]:
        (run,) = self.prompt_runs()
        return run


def turn_request(**overrides: Any) -> TurnRequest:
    fields: dict[str, Any] = {
        "system": "Ты мастер подземелья.",
        "adventure_brief": "Сводка: склеп под старой мельницей.",
        "context": "Игроки входят в склеп.",
        "tools": (CHECK_TOOL,),
        "timeout_s": 30.0,
    }
    return TurnRequest(**{**fields, **overrides})


class RecordingTools:
    """Исполнитель инструментов мастера вместо движка: запоминает вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return f"результат {name}", False


def token_of(run: dict[str, Any]) -> str:
    url = run["mcp_config"]["mcpServers"]["dm"]["args"][-1]
    return url.rsplit("/", 1)[1]


def option_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def reply(**overrides: Any) -> str:
    """JSON-ответ `claude -p --output-format json` для очереди ответов имитатора."""
    fields: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Тени скрывают вас…",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return json.dumps({**fields, **overrides}, ensure_ascii=False)


def assert_process_gone(pid: int) -> None:
    """Процесс завершён и забран родителем: зомби `os.kill(pid, 0)` ещё нашёл бы."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@asynccontextmanager
async def live_server(app):
    """Приложение на настоящем локальном порту: так запрос приходит из реального сокета."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


# --- внутренний API инструментов ---


def dev_app():
    app = create_app(Settings(app_env="dev"))
    return app, app.state.turn_tokens


def api_client(app, client: tuple[str, int] = LOCALHOST) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=client)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")


def issue_token(tokens: TurnTokens, executor=None) -> str:
    return tokens.issue(executor or RecordingTools(), (CHECK_TOOL,))


async def test_internal_api_lists_tool_schemas_of_the_turn():
    app, tokens = dev_app()
    token = issue_token(tokens)

    async with api_client(app) as client:
        response = await client.get(f"/internal/dm-tools/{token}/tools")

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "request_check",
            "description": "Запросить проверку навыка.",
            "input_schema": CHECK_TOOL.input_schema,
        }
    ]


async def test_internal_api_call_runs_executor_and_returns_result():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = issue_token(tokens, tools)

    async with api_client(app) as client:
        response = await client.post(
            f"/internal/dm-tools/{token}/call",
            json={"name": "request_check", "args": {"dc": 15}},
        )

    assert response.status_code == 200
    assert response.json() == {"result": "результат request_check", "is_error": False}
    assert tools.calls == [("request_check", {"dc": 15})]


async def test_internal_api_call_passes_tool_error_flag():
    async def failing_tool(name, args):
        return "Ошибка: нет такого персонажа", True

    app, tokens = dev_app()
    token = issue_token(tokens, failing_tool)

    async with api_client(app) as client:
        response = await client.post(f"/internal/dm-tools/{token}/call", json={"name": "heal"})

    assert response.json() == {"result": "Ошибка: нет такого персонажа", "is_error": True}


async def test_internal_api_call_with_arguments_nested_too_deep_is_rejected_unexecuted():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = issue_token(tokens, tools)
    deep = '{"name": "request_check", "args": {"x": ' + "[" * 100_000 + "]" * 100_000 + "}}"

    async with api_client(app) as client:
        response = await client.post(
            f"/internal/dm-tools/{token}/call",
            content=deep.encode(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert tools.calls == []


async def test_internal_api_call_rejects_malformed_body():
    app, tokens = dev_app()
    token = issue_token(tokens)

    async with api_client(app) as client:
        response = await client.post(f"/internal/dm-tools/{token}/call", json={"args": {}})

    assert response.status_code == 422


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_internal_api_accepts_local_clients(host):
    app, tokens = dev_app()
    token = issue_token(tokens)

    async with api_client(app, client=(host, 50000)) as client:
        response = await client.get(f"/internal/dm-tools/{token}/tools")

    assert response.status_code == 200


@pytest.mark.parametrize("path_suffix", ["tools", "call"])
async def test_internal_api_forbids_non_local_clients_even_with_valid_token(path_suffix):
    app, tokens = dev_app()
    tools = RecordingTools()
    token = issue_token(tokens, tools)

    async with api_client(app, client=("203.0.113.1", 1)) as client:
        if path_suffix == "tools":
            response = await client.get(f"/internal/dm-tools/{token}/tools")
        else:
            response = await client.post(
                f"/internal/dm-tools/{token}/call", json={"name": "request_check", "args": {}}
            )

    assert response.status_code == 403
    assert tools.calls == []


async def test_internal_api_forbids_non_local_client_before_token_lookup():
    # Чужой клиент не должен отличать неизвестный токен от настоящего: всегда 403, не 404.
    app, _ = dev_app()

    async with api_client(app, client=("203.0.113.1", 1)) as client:
        response = await client.get("/internal/dm-tools/unknown-token/tools")

    assert response.status_code == 403


async def test_internal_api_forbids_request_without_client_address():
    app, tokens = dev_app()
    token = issue_token(tokens)
    transport = httpx.ASGITransport(app=app, client=None)

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.get(f"/internal/dm-tools/{token}/tools")

    assert response.status_code == 403


@pytest.mark.parametrize("header", FORWARDING_HEADERS)
@pytest.mark.parametrize("path_suffix", ["tools", "call"])
async def test_internal_api_forbids_forwarded_requests_from_localhost(header, path_suffix):
    # Трафик туннеля приходит с 127.0.0.1 и несёт заголовки пересылки: это не локальный запрос.
    app, tokens = dev_app()
    tools = RecordingTools()
    token = issue_token(tokens, tools)

    async with api_client(app) as client:
        if path_suffix == "tools":
            response = await client.get(
                f"/internal/dm-tools/{token}/tools", headers={header: "198.51.100.7"}
            )
        else:
            response = await client.post(
                f"/internal/dm-tools/{token}/call",
                json={"name": "request_check", "args": {}},
                headers={header: "198.51.100.7"},
            )

    assert response.status_code == 403
    assert tools.calls == []


async def test_internal_api_forbids_non_local_client_before_reading_body():
    app, tokens = dev_app()
    token = issue_token(tokens)

    async with api_client(app, client=("203.0.113.1", 1)) as client:
        response = await client.post(
            f"/internal/dm-tools/{token}/call",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize("path_suffix", ["tools", "call"])
async def test_internal_api_unknown_token_is_404_even_from_localhost(path_suffix):
    app, _ = dev_app()

    async with api_client(app) as client:
        if path_suffix == "tools":
            response = await client.get("/internal/dm-tools/unknown-token/tools")
        else:
            response = await client.post(
                "/internal/dm-tools/unknown-token/call", json={"name": "x", "args": {}}
            )

    assert response.status_code == 404


async def test_internal_api_revoked_token_is_404():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = issue_token(tokens, tools)
    tokens.revoke(token)

    async with api_client(app) as client:
        listed = await client.get(f"/internal/dm-tools/{token}/tools")
        called = await client.post(
            f"/internal/dm-tools/{token}/call", json={"name": "request_check", "args": {}}
        )

    assert (listed.status_code, called.status_code) == (404, 404)
    assert tools.calls == []


def test_turn_tokens_are_unique_unguessable_and_revoke_is_idempotent():
    tokens = TurnTokens()

    first, second = issue_token(tokens), issue_token(tokens)

    assert first != second
    assert len(first) >= 32
    tokens.revoke(first)
    tokens.revoke(first)  # повторный отзыв и отзыв неизвестного токена не ошибка
    tokens.revoke("never-issued")
    assert tokens.get(first) is None
    assert tokens.get(second) is not None


async def test_internal_router_is_hidden_from_openapi_schema():
    app, tokens = dev_app()
    token = issue_token(tokens)

    async with api_client(app) as client:
        schema = (await client.get("/openapi.json")).json()

    assert not any("internal" in path for path in schema["paths"])
    assert token not in json.dumps(schema)


async def test_internal_api_is_absent_in_prod():
    app = create_app(Settings(app_env="prod"))

    async with api_client(app) as client:
        listed = await client.get("/internal/dm-tools/any-token/tools")
        called = await client.post(
            "/internal/dm-tools/any-token/call", json={"name": "x", "args": {}}
        )

    assert (listed.status_code, called.status_code) == (404, 404)
    assert not hasattr(app.state, "turn_tokens")


async def test_dev_app_keeps_health_endpoint():
    app, _ = dev_app()

    async with api_client(app) as client:
        response = await client.get("/health")

    assert response.json() == {"status": "ok", "env": "dev"}


# --- запись вызовов инструментов хода ---


async def test_recorder_records_calls_with_bounded_trace():
    tools = RecordingTools()
    recorder = ToolRecorder(tools, max_calls=5)
    huge_args = {"entry": "я" * (MAX_TRACE_ARGS_CHARS * 2)}

    result = await recorder("chronicle", huge_args)

    assert result == ("результат chronicle", False)
    assert tools.calls == [("chronicle", huge_args)]  # инструмент получил аргументы целиком
    (call,) = recorder.trace
    assert call.name == "chronicle"
    assert "_truncated" in call.args  # а в трассе они обрезаны общей функцией make_trace_call
    assert call.is_error is False


async def test_recorder_refuses_calls_over_limit_and_marks_exhausted():
    tools = RecordingTools()
    recorder = ToolRecorder(tools, max_calls=2)

    for _ in range(2):
        await recorder("request_check", {})
    text, is_error = await recorder("request_check", {})

    assert is_error is True
    assert "лимит" in text
    assert recorder.exhausted is True
    assert len(tools.calls) == 2  # лишний вызов не исполнен
    assert len(recorder.trace) == 2


async def test_recorder_keeps_first_failure_and_refuses_later_calls():
    calls = 0

    async def broken_tool(name, args):
        nonlocal calls
        calls += 1
        raise RuntimeError("баг движка")

    recorder = ToolRecorder(broken_tool, max_calls=5)

    with pytest.raises(RuntimeError, match="баг движка"):
        await recorder("heal", {})
    text, is_error = await recorder("heal", {})  # после сбоя ход прерван: инструменты закрыты

    assert isinstance(recorder.failure, RuntimeError)
    assert is_error is True
    assert calls == 1


async def test_recorder_runs_parallel_calls_one_at_a_time():
    active = 0
    max_active = 0

    async def slow_tool(name, args):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok", False

    recorder = ToolRecorder(slow_tool, max_calls=5)

    await asyncio.gather(recorder("a", {}), recorder("b", {}), recorder("c", {}))

    assert max_active == 1  # броски и изменения состояния идут в порядке прихода
    assert [call.name for call in recorder.trace] == ["a", "b", "c"]


async def test_closed_recorder_refuses_calls_without_running_them():
    tools = RecordingTools()
    recorder = ToolRecorder(tools, max_calls=5)

    recorder.close()
    text, is_error = await recorder("request_check", {})

    assert is_error is True
    assert "завершён" in text
    assert tools.calls == []  # после конца хода инструмент не исполняется
    assert recorder.trace == []
    assert recorder.failure is None and recorder.exhausted is False  # это не сбой хода


async def test_recorder_refuses_call_that_waited_in_queue_while_it_was_closed():
    started, release = asyncio.Event(), asyncio.Event()
    executed: list[str] = []

    async def slow_tool(name, args):
        executed.append(name)
        started.set()
        await release.wait()
        return "ok", False

    recorder = ToolRecorder(slow_tool, max_calls=5)
    first = asyncio.create_task(recorder("first", {}))
    await started.wait()
    second = asyncio.create_task(recorder("second", {}))
    await asyncio.sleep(0)  # второй вызов уже стоит в очереди за замком первого

    recorder.close()
    release.set()

    assert await first == ("ok", False)  # начатый до закрытия вызов не прерывается
    text, is_error = await second
    assert is_error is True and "завершён" in text
    assert executed == ["first"]
    assert [call.name for call in recorder.trace] == ["first"]


# --- запуск CLI ---


async def test_run_turn_starts_cli_with_exact_flags(tmp_path):
    harness = Harness(tmp_path)
    context = 'Игрок: "Атакую!"\nи ещё ; rm -rf / $HOME'

    await harness.provider.run_turn(turn_request(context=context), RecordingTools())

    run = harness.only_run()
    argv, cwd = run["argv"], Path(run["cwd"])
    assert argv[:2] == ["-p", context]  # контекст — один аргумент, без оболочки
    assert argv[2:] == [
        "--system-prompt-file",
        argv[3],
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        argv[8],
        "--allowedTools",
        "mcp__dm__*",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "json",
    ]
    assert Path(argv[3]).resolve() == (cwd / "system.md").resolve()
    assert Path(argv[8]).resolve() == (cwd / "mcp.json").resolve()
    for forbidden in ("--bare", "--resume", "--continue", "--disallowedTools", "--model"):
        assert forbidden not in argv


# Тексты, которые CLI разобрал бы как свои опции, если бы они стояли в argv после `-p` как есть
# (в контекст хода и промпты генератора попадает текст игроков). Это данные теста, а не вызовы.
OPTION_LIKE_PROMPTS = [
    "--dangerously-skip-permissions --tools bash",
    "--mcp-config=/tmp/evil.json",
    "-x",
    "-",
    "--",
]


def flags_of(argv: list[str]) -> list[str]:
    """Аргументы после промпта; пути временной папки сведены к именам файлов."""
    return [Path(arg).name if arg.startswith("/") else arg for arg in argv[2:]]


@pytest.mark.parametrize("attack", OPTION_LIKE_PROMPTS)
async def test_run_turn_context_that_looks_like_an_option_is_not_passed_as_one(tmp_path, attack):
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(context="Обычный контекст."), RecordingTools())
    await harness.provider.run_turn(turn_request(context=attack), RecordingTools())

    plain, attacked = harness.prompt_runs()
    assert attacked["argv"][0] == "-p"
    assert not attacked["argv"][1].startswith("-")
    assert attacked["argv"][1].lstrip() == attack  # текст не изменён, добавлен только отступ
    assert flags_of(attacked["argv"]) == flags_of(plain["argv"])  # набор флагов прежний


@pytest.mark.parametrize(
    "text", ["Игроки входят.", " --с отступом", "\n-- с переводом строки", "—"]
)
async def test_run_turn_context_that_is_not_option_like_goes_through_unchanged(tmp_path, text):
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(context=text), RecordingTools())

    assert harness.only_run()["argv"][1] == text


async def test_run_turn_writes_system_prompt_and_mcp_config(tmp_path):
    harness = Harness(tmp_path, internal_url="http://127.0.0.1:8123")

    await harness.provider.run_turn(turn_request(), RecordingTools())

    run = harness.only_run()
    assert run["system_prompt"] == "Ты мастер подземелья.\n\nСводка: склеп под старой мельницей."
    token = token_of(run)
    assert run["mcp_config"] == {
        "mcpServers": {
            "dm": {
                "command": sys.executable,
                "args": [
                    "-m",
                    "dnd_server.llm.mcp_bridge",
                    "--url",
                    f"http://127.0.0.1:8123/internal/dm-tools/{token}",
                ],
            }
        }
    }


async def test_run_turn_passes_only_allowlisted_environment(tmp_path, monkeypatch):
    for name, value in SECRET_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DND_STRAY_VARIABLE", "stray-value")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(), RecordingTools())

    env = harness.only_run()["env"]
    for name in SECRET_ENV:
        assert name not in env
    assert "DND_STRAY_VARIABLE" not in env  # allowlist, а не «всё кроме секретов»
    dumped = json.dumps(env)
    assert not any(value in dumped for value in SECRET_ENV.values())
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == str(tmp_path)
    assert env["LC_ALL"] == "ru_RU.UTF-8"


async def test_run_turn_overrides_claude_mds_variable_from_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "0")
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(), RecordingTools())

    assert harness.only_run()["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_run_turn_uses_empty_temp_dir_outside_repo_and_removes_it(tmp_path):
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(), RecordingTools())

    run = harness.only_run()
    cwd = Path(run["cwd"]).resolve()
    assert not cwd.is_relative_to(REPO_ROOT)
    assert run["cwd_files"] == ["mcp.json", "system.md"]  # больше в папке ничего нет
    assert not cwd.exists()


async def test_run_turn_parses_result_usage_and_empty_trace(tmp_path):
    answer = reply(
        result="  Свечи гаснут одна за другой.  ",
        usage={
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 900,
            "service_tier": "standard",
        },
    )
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([answer]))

    result = await harness.provider.run_turn(turn_request(), RecordingTools())

    assert result.text == "Свечи гаснут одна за другой."
    assert result.usage == Usage(
        input_tokens=120, output_tokens=45, cache_read_tokens=900, cache_write_tokens=30
    )
    assert result.stop_reason == "end_turn"
    assert result.trace == ()


async def test_run_turn_without_usage_reports_zero_usage(tmp_path):
    harness = Harness(
        tmp_path,
        FAKE_CLAUDE_REPLIES=json.dumps([json.dumps({"type": "result", "result": "Тихо."})]),
    )

    result = await harness.provider.run_turn(turn_request(), RecordingTools())

    assert (result.text, result.usage) == ("Тихо.", Usage())


async def test_run_turn_returns_empty_text_when_cli_gave_none(tmp_path):
    # Пустой рассказ — не ошибка адаптера: автоповтор хода без итогового текста решает мастер.
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([reply(result="")]))

    result = await harness.provider.run_turn(turn_request(), RecordingTools())

    assert result.text == ""


@pytest.mark.parametrize(
    "stdout",
    ["не JSON", "[1, 2]", json.dumps({"type": "result"}), json.dumps({"result": 5})],
    ids=["not-json", "not-object", "no-result", "result-not-text"],
)
async def test_run_turn_rejects_unparsable_cli_output(tmp_path, stdout):
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([stdout]))

    with pytest.raises(ProviderError, match="разобрать"):
        await harness.provider.run_turn(turn_request(), RecordingTools())


async def test_run_turn_output_nested_too_deep_is_a_provider_error(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([deep]))

    with pytest.raises(ProviderError, match="не удалось разобрать ответ claude"):
        await harness.provider.run_turn(turn_request(), RecordingTools())


async def test_run_turn_rejects_error_result_even_with_zero_exit(tmp_path):
    answer = reply(is_error=True, subtype="error_during_execution", result="Credit balance low")
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([answer]))

    with pytest.raises(ProviderError, match="error_during_execution"):
        await harness.provider.run_turn(turn_request(), RecordingTools())


async def test_run_turn_nonzero_exit_is_provider_error_with_stderr_tail(tmp_path):
    stderr = "\n".join(f"строка {number}" for number in range(1, 31))
    harness = Harness(tmp_path, FAKE_CLAUDE_EXIT="3", FAKE_CLAUDE_STDERR=stderr)

    with pytest.raises(ProviderError) as caught:
        await harness.provider.run_turn(turn_request(), RecordingTools())

    message = str(caught.value)
    assert "3" in message
    assert "строка 30" in message
    assert "строка 1\n" not in message  # только последние строки stderr
    assert not Path(harness.only_run()["cwd"]).exists()


async def test_run_turn_error_text_never_contains_turn_token(tmp_path):
    harness = Harness(
        tmp_path,
        FAKE_CLAUDE_EXIT="1",
        FAKE_CLAUDE_STDERR="MCP server dm failed: {url}",
    )

    with pytest.raises(ProviderError) as caught:
        await harness.provider.run_turn(turn_request(), RecordingTools())

    token = token_of(harness.only_run())
    assert token not in str(caught.value)
    assert "MCP server dm failed" in str(caught.value)


FIXED_TOKEN = "turn-token-0123456789-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def fixed_turn_token(monkeypatch) -> str:
    """Токен хода известен заранее: ответ имитатора может его процитировать, как мог бы CLI."""
    monkeypatch.setattr("dnd_server.llm.internal_api.secrets.token_urlsafe", lambda _n: FIXED_TOKEN)
    return FIXED_TOKEN


@pytest.mark.parametrize(
    "stdout",
    [
        f"сбой моста: {FIXED_TOKEN}",
        reply(is_error=True, subtype="error_during_execution", result=f"MCP {FIXED_TOKEN} упал"),
    ],
    ids=["unparsable-stdout", "error-result"],
)
async def test_run_turn_error_text_from_stdout_never_contains_turn_token(
    tmp_path, fixed_turn_token, stdout
):
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([stdout]))

    with pytest.raises(ProviderError) as caught:
        await harness.provider.run_turn(turn_request(), RecordingTools())

    assert token_of(harness.only_run()) == fixed_turn_token  # токен действительно тот же
    assert fixed_turn_token not in str(caught.value)
    assert "***" in str(caught.value)


class SpyTokens(TurnTokens):
    """Реестр, который помнит выданные токены: нужен там, где процесс CLI не успел их показать."""

    def __init__(self) -> None:
        super().__init__()
        self.issued: list[str] = []

    def issue(self, executor, schemas) -> str:
        token = super().issue(executor, schemas)
        self.issued.append(token)
        return token


async def test_run_turn_missing_cli_is_provider_error_and_token_is_revoked(tmp_path):
    tokens = SpyTokens()
    provider = ClaudeCodeCliProvider(
        tokens, "http://127.0.0.1:8000", cli_path=str(tmp_path / "no-such-claude")
    )

    with pytest.raises(ProviderError, match="не удалось запустить"):
        await provider.run_turn(turn_request(), RecordingTools())

    (token,) = tokens.issued
    assert tokens.get(token) is None
    assert provider.cli_version is None


async def test_run_turn_timeout_stops_process_and_cleans_up(tmp_path):
    harness = Harness(tmp_path, FAKE_CLAUDE_HANG="1")

    started = time.monotonic()
    with pytest.raises(ProviderError, match="тайм-аут"):
        await harness.provider.run_turn(turn_request(timeout_s=1.0), RecordingTools())

    assert time.monotonic() - started < 10
    run = harness.only_run()
    assert_process_gone(run["pid"])
    assert not Path(run["cwd"]).exists()
    assert harness.tokens.get(token_of(run)) is None


async def test_run_turn_timeout_kills_process_that_ignores_sigint(tmp_path):
    harness = Harness(tmp_path, stop_grace_s=0.3, FAKE_CLAUDE_HANG="ignore-sigint")

    with pytest.raises(ProviderError, match="тайм-аут"):
        await harness.provider.run_turn(turn_request(timeout_s=1.0), RecordingTools())

    assert_process_gone(harness.only_run()["pid"])


async def test_run_turn_cancellation_stops_process_and_revokes_token(tmp_path):
    harness = Harness(tmp_path, FAKE_CLAUDE_HANG="1")
    turn = asyncio.create_task(harness.provider.run_turn(turn_request(), RecordingTools()))
    async with asyncio.timeout(10):
        while not harness.prompt_runs():
            await asyncio.sleep(0.02)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    run = harness.only_run()
    assert_process_gone(run["pid"])
    assert not Path(run["cwd"]).exists()
    assert harness.tokens.get(token_of(run)) is None


async def test_run_turn_revokes_token_after_success_and_failure(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    ok = Harness(tmp_path / "ok")
    bad = Harness(tmp_path / "bad", FAKE_CLAUDE_EXIT="1")
    await ok.provider.run_turn(turn_request(), RecordingTools())

    with pytest.raises(ProviderError):
        await bad.provider.run_turn(turn_request(), RecordingTools())

    for harness in (ok, bad):
        assert harness.tokens.get(token_of(harness.only_run())) is None


async def test_each_turn_is_a_new_process_with_its_own_token(tmp_path):
    harness = Harness(tmp_path)

    await harness.provider.run_turn(turn_request(), RecordingTools())
    await harness.provider.run_turn(turn_request(), RecordingTools())

    first, second = harness.prompt_runs()
    assert first["pid"] != second["pid"]
    assert token_of(first) != token_of(second)
    assert first["cwd"] != second["cwd"]


async def test_run_turn_records_tool_calls_made_through_internal_api(tmp_path):
    app, tokens = dev_app()
    tools = RecordingTools()
    long_entry = "я" * (MAX_TRACE_ARGS_CHARS * 2)
    calls = [
        {"name": "request_check", "args": {"dc": 15}},
        {"name": "chronicle", "args": {"entry": long_entry}},
    ]
    async with live_server(app) as base_url:
        harness = Harness(
            tmp_path, internal_url=base_url, tokens=tokens, FAKE_CLAUDE_TOOL_CALLS=json.dumps(calls)
        )

        result = await harness.provider.run_turn(turn_request(), tools)

    # Трасса строится из вызовов внутреннего API, а не из вывода CLI.
    assert [call.name for call in result.trace] == ["request_check", "chronicle"]
    assert result.trace[0].args == {"dc": 15}
    assert result.trace[0].result == "результат request_check"
    assert "_truncated" in result.trace[1].args
    assert tools.calls == [("request_check", {"dc": 15}), ("chronicle", {"entry": long_entry})]


async def test_run_turn_rejects_tool_calls_over_iteration_limit(tmp_path):
    app, tokens = dev_app()
    tools = RecordingTools()
    calls = [{"name": "request_check", "args": {"dc": 15}}] * 3
    async with live_server(app) as base_url:
        harness = Harness(
            tmp_path, internal_url=base_url, tokens=tokens, FAKE_CLAUDE_TOOL_CALLS=json.dumps(calls)
        )

        with pytest.raises(ToolLoopExceeded):
            await harness.provider.run_turn(turn_request(max_tool_iterations=2), tools)

    assert len(tools.calls) == 2


async def test_run_turn_propagates_tool_executor_failure(tmp_path):
    async def broken_tool(name, args):
        raise RuntimeError("баг движка")

    app, tokens = dev_app()
    calls = [{"name": "request_check", "args": {}}]
    async with live_server(app) as base_url:
        harness = Harness(
            tmp_path, internal_url=base_url, tokens=tokens, FAKE_CLAUDE_TOOL_CALLS=json.dumps(calls)
        )

        # Сбой исполнителя рвёт ход (копия состояния выбрасывается), а не прячется за текстом.
        with pytest.raises(RuntimeError, match="баг движка"):
            await harness.provider.run_turn(turn_request(), broken_tool)


async def test_run_turn_fails_even_if_cli_finishes_after_executor_failure(tmp_path):
    async def broken_tool(name, args):
        raise RuntimeError("баг движка")

    app, tokens = dev_app()
    calls = [{"name": "request_check", "args": {}}]
    async with live_server(app) as base_url:
        harness = Harness(
            tmp_path,
            internal_url=base_url,
            tokens=tokens,
            FAKE_CLAUDE_TOOL_CALLS=json.dumps(calls),
            FAKE_CLAUDE_IGNORE_TOOL_ERRORS="1",
        )

        # Модель получила ошибку, «обошла» её и дала рассказ, но ход с упавшим инструментом
        # нельзя считать успешным: состояние могло измениться наполовину.
        with pytest.raises(RuntimeError, match="баг движка"):
            await harness.provider.run_turn(turn_request(), broken_tool)


async def test_token_works_during_the_turn_and_not_after(tmp_path):
    app, tokens = dev_app()
    calls = [{"name": "request_check", "args": {"dc": 10}}]
    async with live_server(app) as base_url:
        harness = Harness(
            tmp_path, internal_url=base_url, tokens=tokens, FAKE_CLAUDE_TOOL_CALLS=json.dumps(calls)
        )
        # Вызов во время хода прошёл (иначе имитатор упал бы на ответе 404 и ход бы не удался).
        await harness.provider.run_turn(turn_request(), RecordingTools())

        async with api_client(app) as client:
            after = await client.get(f"/internal/dm-tools/{token_of(harness.only_run())}/tools")

    assert after.status_code == 404


class LateCall:
    """POST …/call, у которого тело приходит позже: токен он уже прошёл и ждёт тело запроса."""

    def __init__(self, app, token: str, name: str = "request_check") -> None:
        self._app = app
        self._path = f"/internal/dm-tools/{token}/call"
        self._body = json.dumps({"name": name, "args": {"dc": 15}}).encode()
        self.waiting_for_body = asyncio.Event()
        self.body_may_arrive = asyncio.Event()

    async def run(self) -> tuple[int, dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "root_path": "",
            # Host нужен, как у любого настоящего клиента: без него проверка Host вернёт 400.
            "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8000")],
            "client": LOCALHOST,
            "server": ("127.0.0.1", 8000),
        }

        async def receive() -> dict[str, Any]:
            self.waiting_for_body.set()
            await self.body_may_arrive.wait()
            return {"type": "http.request", "body": self._body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await self._app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(body)


@pytest.mark.parametrize("ending", ["cancel", "timeout"], ids=["отмена хода", "тайм-аут хода"])
async def test_internal_api_call_that_outlives_the_turn_is_not_executed(tmp_path, ending):
    # Запрос прошёл проверку токена до конца хода, а тело дошло после: исполнять его уже нельзя,
    # иначе он изменил бы состояние, которое оркестратор уже выбросил как неудачную попытку.
    app, tokens = dev_app()
    issued: list[str] = []
    issue = tokens.issue

    def spy_issue(executor, schemas):
        issued.append(issue(executor, schemas))
        return issued[-1]

    tokens.issue = spy_issue
    tools = RecordingTools()
    harness = Harness(tmp_path, tokens=tokens, FAKE_CLAUDE_HANG="1")
    request = turn_request(timeout_s=2.0 if ending == "timeout" else 30.0)
    turn = asyncio.create_task(harness.provider.run_turn(request, tools))
    async with asyncio.timeout(10):
        while not issued:
            await asyncio.sleep(0.01)
    late = LateCall(app, issued[0])
    late_task = asyncio.create_task(late.run())
    async with asyncio.timeout(10):
        await late.waiting_for_body.wait()

    if ending == "cancel":
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
    else:
        with pytest.raises(ProviderError, match="тайм-аут"):
            await turn
    late.body_may_arrive.set()
    status, answer = await late_task

    assert status == 200
    assert answer["is_error"] is True
    assert tools.calls == []


# --- версия CLI ---


async def test_cli_version_is_recorded_once_without_running_a_model(tmp_path, caplog):
    harness = Harness(tmp_path)
    assert harness.provider.cli_version is None

    with caplog.at_level("INFO", logger="dnd_server.llm.claude_code_cli"):
        await harness.provider.run_turn(turn_request(), RecordingTools())
        await harness.provider.run_turn(turn_request(), RecordingTools())

    assert harness.provider.cli_version == "9.9.9 (Claude Code)"
    version_runs = [e for e in harness.entries() if e["argv"] == ["--version"]]
    assert len(version_runs) == 1  # версия читается один раз за жизнь провайдера
    assert "9.9.9 (Claude Code)" in caplog.text
    for secret in SECRET_ENV.values():
        assert secret not in caplog.text
    assert "CLAUDE_CODE_DISABLE_CLAUDE_MDS" in version_runs[0]["env"]  # то же урезанное окружение
    assert not Path(version_runs[0]["cwd"]).is_relative_to(REPO_ROOT)


async def test_cli_version_failure_does_not_fail_the_turn(tmp_path):
    harness = Harness(tmp_path, FAKE_CLAUDE_VERSION_FAIL="1")

    result = await harness.provider.run_turn(turn_request(), RecordingTools())

    assert result.text == "Тени скрывают вас…"
    assert harness.provider.cli_version is None


# --- complete_json ---


class Hook(BaseModel):
    title: str
    danger: int = Field(ge=1, le=5)


class StrictHook(BaseModel):
    """Схема, которая отвергает лишние поля: имя лишнего ключа попадает в текст ошибки."""

    model_config = ConfigDict(extra="forbid")

    title: str


# Имена лишних ключей из находки X1 security review: запись в буфер обмена (OSC 52) и стирание
# строки возвратом каретки. Модель выбирает их сама.
HOSTILE_KEYS = {
    "osc52-буфер-обмена": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "\x1b[2K\rПриключение записано",
}


async def test_complete_json_runs_cli_without_mcp_and_validates(tmp_path):
    answer = reply(result=json.dumps({"title": "Склеп", "danger": 3}))
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([answer]))

    hook = await harness.provider.complete_json(
        JsonRequest(system="Сочинитель.", prompt="Придумай зацепку.", purpose="weaving"), Hook
    )

    assert hook == Hook(title="Склеп", danger=3)
    run = harness.only_run()
    argv = run["argv"]
    assert argv[0] == "-p"
    assert "Придумай зацепку." in argv[1]
    assert '"danger"' in argv[1]  # схема ответа входит в промпт
    for flag in ("--mcp-config", "--allowedTools"):
        assert flag not in argv
    for flag in ("--strict-mcp-config", "--tools", "--system-prompt-file"):
        assert flag in argv
    assert option_value(argv, "--tools") == ""
    assert run["mcp_config"] is None
    assert run["system_prompt"] == "Сочинитель."
    assert run["cwd_files"] == ["system.md"]
    assert run["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


@pytest.mark.parametrize("attack", OPTION_LIKE_PROMPTS)
async def test_complete_json_prompt_that_looks_like_an_option_is_not_passed_as_one(
    tmp_path, attack
):
    # Первая попытка не проходит схему, вторая проходит: запрет касается каждого запуска CLI.
    bad = reply(result=json.dumps({"title": "Склеп", "danger": 987}))
    good = reply(result=json.dumps({"title": "Склеп", "danger": 4}))
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([bad, good]))

    await harness.provider.complete_json(
        JsonRequest(system="s", prompt=attack, purpose="weaving"), Hook
    )

    first, second = harness.prompt_runs()
    for run in (first, second):
        assert run["argv"][0] == "-p"
        assert not run["argv"][1].startswith("-")
        assert run["argv"][1].lstrip().startswith(attack)
    assert flags_of(first["argv"]) == flags_of(second["argv"])


async def test_complete_json_accepts_markdown_fenced_answer(tmp_path):
    fenced = "```json\n" + json.dumps({"title": "Склеп", "danger": 2}) + "\n```"
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([reply(result=fenced)]))

    hook = await harness.provider.complete_json(
        JsonRequest(system="s", prompt="p", purpose="weaving"), Hook
    )

    assert hook.danger == 2


async def test_complete_json_retries_twice_with_validation_errors(tmp_path):
    bad = reply(result=json.dumps({"title": "Склеп", "danger": 987}))
    good = reply(result=json.dumps({"title": "Склеп", "danger": 4}))
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([bad, bad, good]))

    hook = await harness.provider.complete_json(
        JsonRequest(system="s", prompt="p", purpose="weaving"), Hook
    )

    assert hook.danger == 4
    first, second, _third = harness.prompt_runs()
    assert "не прошёл проверку" not in first["argv"][1]
    assert "danger" in second["argv"][1] and "не прошёл проверку" in second["argv"][1]
    assert "987" not in second["argv"][1]  # значения ответа в повторный запрос не попадают


async def test_complete_json_gives_up_after_two_retries(tmp_path):
    bad = reply(result="это не JSON")
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([bad]))

    with pytest.raises(ProviderError, match="weaving"):
        await harness.provider.complete_json(
            JsonRequest(system="s", prompt="p", purpose="weaving"), Hook
        )

    assert len(harness.prompt_runs()) == 3  # первая попытка и не больше двух повторов


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
async def test_extra_key_with_terminal_escapes_is_escaped_in_the_error_and_the_retry_prompts(
    tmp_path, key
):
    hostile = reply(result=json.dumps({"title": "Склеп", key: 1}))
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([hostile]))

    with pytest.raises(ProviderError) as error:
        await harness.provider.complete_json(
            JsonRequest(system="s", prompt="p", purpose="weaving"), StrictHook
        )

    assert str(error.value).isprintable()  # ни ESC, ни BEL, ни возврата каретки
    assert "\\x1b" in str(error.value)  # запись видна глазами, а не потеряна
    _first, *retries = harness.prompt_runs()
    for run in retries:  # промпт повтора строится из того же текста
        assert not {"\x1b", "\x07", "\r"} & set(run["argv"][1])


async def test_complete_json_answer_nested_too_deep_is_retried_as_invalid_schema(tmp_path):
    deep = reply(result="[" * 100_000 + "]" * 100_000)
    harness = Harness(tmp_path, FAKE_CLAUDE_REPLIES=json.dumps([deep]))

    with pytest.raises(ProviderError, match="weaving"):
        await harness.provider.complete_json(
            JsonRequest(system="s", prompt="p", purpose="weaving"), Hook
        )

    assert len(harness.prompt_runs()) == 3  # первая попытка и не больше двух повторов


def test_provider_is_named_after_its_config_value(tmp_path):
    provider: LLMProvider = Harness(tmp_path).provider

    assert provider.name == "claude_code_cli"
