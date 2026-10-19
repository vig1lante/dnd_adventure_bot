"""Сквозной запрет живых вызовов в тестах сервера (спека §13, инвариант «и»).

Запрет стоит в автоматической фикстуре `live_call_attempts` (`conftest.py`) и действует на каждый
тест: ни сети, ни живых провайдеров, ни настоящего `claude`. Здесь проверяется сам запрет: что он
срабатывает, что записывает попытки, которые SDK или адаптер проглотили, и что не мешает
разрешённому (loopback, имитатор CLI, мост MCP, `dnd-generate`). Настоящий `claude` не запускается:
его роль играет скрипт-ловушка во временной папке.
"""

import asyncio
import socket
import stat
import subprocess
import sys
from pathlib import Path

import anthropic
import openai
import pytest

from dnd_server.llm.anthropic_api import AnthropicProvider
from dnd_server.llm.base import ProviderError, TurnRequest
from dnd_server.llm.claude_code_cli import ClaudeCodeCliProvider
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider

FAKE_CLAUDE = Path(__file__).resolve().parent / "llm" / "fake_claude.py"
EXTERNAL_IP = "203.0.113.7"  # адрес из блока документации: никуда не ведёт
EMPTY_TURN = TurnRequest(system="", adventure_brief="", context="", tools=())


def write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_claude_wrapper(folder: Path) -> Path:
    """Обёртка, как в тестах адаптера: скрипт `claude`, который запускает имитатор."""
    return write_script(folder / "claude", f'exec "{sys.executable}" "{FAKE_CLAUDE}" "$@"')


@pytest.fixture
def planted_claude(tmp_path, monkeypatch):
    """«Настоящий claude» на PATH: скрипт-ловушка, который при запуске оставляет метку."""
    folder = tmp_path / "bin"
    folder.mkdir()
    marker = tmp_path / "claude-was-started"
    write_script(folder / "claude", f'echo started > "{marker}"')
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return marker


# --- сеть ---


def test_connect_to_an_external_address_is_refused_and_recorded(live_call_attempts):
    with socket.socket() as client, pytest.raises(AssertionError, match="connect"):
        client.connect((EXTERNAL_IP, 443))

    assert len(live_call_attempts) == 1
    live_call_attempts.clear()


def test_name_resolution_of_an_external_host_is_refused_and_recorded(live_call_attempts):
    with pytest.raises(AssertionError, match="getaddrinfo"):
        socket.getaddrinfo("api.anthropic.com", 443)
    with pytest.raises(AssertionError, match="gethostbyname"):
        socket.gethostbyname("api.openai.com")

    assert len(live_call_attempts) == 2
    live_call_attempts.clear()


async def test_asyncio_connections_are_refused_too(live_call_attempts):
    loop = asyncio.get_running_loop()

    with pytest.raises(AssertionError, match="getaddrinfo"):
        await loop.getaddrinfo("api.anthropic.com", 443)
    with pytest.raises(AssertionError):
        await asyncio.open_connection(EXTERNAL_IP, 443)

    assert len(live_call_attempts) == 2
    live_call_attempts.clear()


async def test_loopback_connections_stay_possible(live_call_attempts):
    async def hello(reader, writer):
        writer.write(b"hello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(hello, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("localhost", port)
        assert await reader.read() == b"hello"
        writer.close()

    assert live_call_attempts == []


async def test_a_sdk_that_swallows_the_refusal_still_fails_the_test(
    live_call_attempts, monkeypatch
):
    """SDK превращает сбой соединения в `ProviderError` (или пропускает исключение запрета):
    попытка записана в обоих случаях, поэтому тест с настоящим клиентом без заглушки не пройдёт."""
    # Прокси владельца в окружении увёл бы запрос на локальный адрес, который запрет пропускает.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    anthropic_client = anthropic.AsyncAnthropic(
        api_key="sk-ant-guard-test", base_url="https://llm.example.test", max_retries=0
    )
    compat_client = openai.AsyncOpenAI(
        base_url="https://llm.example.test/v1", api_key="sk-guard-test", max_retries=0
    )
    providers = [
        AnthropicProvider(anthropic_client, "claude-test", "medium"),
        OpenAICompatibleProvider(compat_client, "model-test"),
    ]

    for provider in providers:
        with pytest.raises((ProviderError, AssertionError)):
            await provider.run_turn(EMPTY_TURN, _no_tools)

    assert len(live_call_attempts) >= 2, "попытки выйти в сеть должны были записаться"
    live_call_attempts.clear()


async def _no_tools(name, args):
    raise AssertionError("инструменты не нужны: запрос не должен дойти до модели")


# --- процессы ---


async def test_claude_found_on_path_is_never_started(planted_claude, live_call_attempts):
    with pytest.raises(AssertionError, match="процесс"):
        await asyncio.create_subprocess_exec("claude", "-p", "привет")
    with pytest.raises(AssertionError, match="процесс"):
        subprocess.run(["claude", "--version"], check=False)

    assert not planted_claude.exists()
    assert len(live_call_attempts) == 2
    live_call_attempts.clear()


async def test_default_cli_path_of_the_provider_cannot_start_the_real_claude(
    planted_claude, live_call_attempts
):
    # `cli_path="claude"` по умолчанию: без запрета тест нашёл бы на PATH настоящий CLI.
    provider = ClaudeCodeCliProvider(TurnTokens(), "http://127.0.0.1:8000")

    with pytest.raises(AssertionError):
        await provider.run_turn(EMPTY_TURN, _no_tools)

    assert not planted_claude.exists()
    live_call_attempts.clear()


@pytest.mark.parametrize(
    "command",
    [
        ["/bin/echo", "привет"],
        [sys.executable, "-c", "print('любой код')"],
        ["/bin/sh", "-c", "echo привет"],
    ],
    ids=["чужая-программа", "интерпретатор-с--c", "оболочка-с--c"],
)
def test_other_programs_are_refused(command, live_call_attempts):
    with pytest.raises(AssertionError, match="процесс"):
        subprocess.run(command, check=False)

    assert len(live_call_attempts) == 1
    live_call_attempts.clear()


def test_shell_commands_are_refused(live_call_attempts):
    with pytest.raises(AssertionError, match="оболочк"):
        subprocess.run("echo привет", shell=True, check=False)

    live_call_attempts.clear()


async def test_the_fake_claude_wrapper_is_allowed(tmp_path, live_call_attempts):
    wrapper = fake_claude_wrapper(tmp_path)

    process = await asyncio.create_subprocess_exec(
        str(wrapper), "--version", stdout=subprocess.PIPE
    )
    stdout, _ = await process.communicate()

    assert process.returncode == 0
    assert "Claude Code" in stdout.decode()
    assert live_call_attempts == []


def test_the_mcp_bridge_process_is_allowed(live_call_attempts):
    result = subprocess.run(
        [sys.executable, "-m", "dnd_server.llm.mcp_bridge", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0
    assert "--url" in result.stdout
    assert live_call_attempts == []


def test_a_missing_program_is_left_to_the_operating_system(tmp_path, live_call_attempts):
    # Запускаться нечему: тесты адаптера сами проверяют понятную ошибку «не удалось запустить».
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(tmp_path / "no-such-claude")], check=False)

    assert live_call_attempts == []
