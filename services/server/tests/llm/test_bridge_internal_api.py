"""Мост MCP и настоящий внутренний API: стык, который по отдельности не виден (план B5.4).

Тесты моста идут против поддельного API, тесты API — против `httpx`. Здесь мост (`build_server`)
ходит в настоящее приложение `create_app(dev)` через ASGI-транспорт в памяти, а схемы
инструментов берутся из реестра движка: именно их увидит `claude`. Сети и процессов нет.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError

from dnd_engine.modules import build_ruleset
from dnd_engine.state import RulesRef
from dnd_engine.tools import build_registry
from dnd_server.app import create_app
from dnd_server.config import Settings
from dnd_server.llm.base import ToolSchema
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.llm.mcp_bridge import build_server

LOCAL_CLIENT = ("127.0.0.1", 50000)
FOREIGN_CLIENT = ("203.0.113.9", 4000)
CHECK_TOOL = ToolSchema(
    name="request_check",
    description="Запросить проверку навыка.",
    input_schema={"type": "object", "properties": {"dc": {"type": "integer"}}},
)


class RecordingTools:
    """Исполнитель хода вместо оркестратора: запоминает вызовы, отвечает заданным."""

    def __init__(self, result: str = "Бросок: 17 против Сл 15", is_error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answer = (result, is_error)

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return self._answer


def dev_app() -> tuple[Any, TurnTokens]:
    app = create_app(Settings(app_env="dev"))
    return app, app.state.turn_tokens


def engine_tool_schemas() -> tuple[ToolSchema, ...]:
    registry = build_registry(build_ruleset(RulesRef(modules=["bg3_flavor", "campfire"])))
    return tuple(ToolSchema(s.name, s.description, s.json_schema()) for s in registry.specs())


@asynccontextmanager
async def bridge_to(app, token: str, client=LOCAL_CLIENT) -> AsyncIterator[Client]:
    """MCP-клиент (роль `claude`) -> мост -> настоящее приложение по адресу хода."""
    transport = httpx.ASGITransport(app=app, client=client, raise_app_exceptions=False)
    base_url = f"http://127.0.0.1:8000/internal/dm-tools/{token}"
    async with (
        httpx.AsyncClient(transport=transport, base_url=base_url) as http,
        Client(build_server(http)) as mcp,
    ):
        yield mcp


async def test_claude_sees_the_engine_tools_exactly_as_the_turn_issued_them():
    app, tokens = dev_app()
    issued = engine_tool_schemas()
    token = tokens.issue(RecordingTools(), issued)

    async with bridge_to(app, token) as mcp:
        listed = (await mcp.list_tools()).tools

    assert [tool.name for tool in listed] == [tool.name for tool in issued]
    assert [tool.description for tool in listed] == [tool.description for tool in issued]
    assert [tool.input_schema for tool in listed] == [tool.input_schema for tool in issued]


async def test_a_call_reaches_the_executor_of_the_turn_and_its_result_comes_back():
    app, tokens = dev_app()
    tools = RecordingTools(result="Бросок: 17 против Сл 15. Ёж — 🦔")
    token = tokens.issue(tools, (CHECK_TOOL,))

    async with bridge_to(app, token) as mcp:
        result = await mcp.call_tool("request_check", {"dc": 15, "reason": "крадётся"})

    assert tools.calls == [("request_check", {"dc": 15, "reason": "крадётся"})]
    assert result.is_error is False
    assert [block.text for block in result.content] == ["Бросок: 17 против Сл 15. Ёж — 🦔"]


async def test_a_call_without_arguments_reaches_the_executor_with_empty_args():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = tokens.issue(tools, (CHECK_TOOL,))

    async with bridge_to(app, token) as mcp:
        await mcp.call_tool("short_rest", None)

    assert tools.calls == [("short_rest", {})]


async def test_a_tool_error_of_the_engine_stays_a_tool_error_for_the_model():
    app, tokens = dev_app()
    token = tokens.issue(RecordingTools("Ошибка: Сл 12 не из таблицы", True), (CHECK_TOOL,))

    async with bridge_to(app, token) as mcp:
        result = await mcp.call_tool("request_check", {"dc": 12})

    assert result.is_error is True
    assert [block.text for block in result.content] == ["Ошибка: Сл 12 не из таблицы"]


async def test_the_model_gets_the_full_result_even_when_the_trace_would_clip_it():
    # Обрезается только запись в трассе (`make_trace_call`), а не то, что видит модель.
    app, tokens = dev_app()
    long_result = "Страница летописи. " * 5000
    token = tokens.issue(RecordingTools(long_result), (CHECK_TOOL,))

    async with bridge_to(app, token) as mcp:
        result = await mcp.call_tool("request_check", {"dc": 15})

    assert [block.text for block in result.content] == [long_result]


async def test_a_revoked_token_stops_both_listing_and_calls():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = tokens.issue(tools, (CHECK_TOOL,))
    tokens.revoke(token)  # ход закончился

    async with bridge_to(app, token) as mcp:
        with pytest.raises(MCPError):
            await mcp.list_tools()
        with pytest.raises(MCPError):
            await mcp.call_tool("request_check", {"dc": 15})

    assert tools.calls == []


async def test_two_turns_never_reach_each_others_executor():
    app, tokens = dev_app()
    first, second = RecordingTools("первый"), RecordingTools("второй")
    first_token = tokens.issue(first, (CHECK_TOOL,))
    second_token = tokens.issue(second, (CHECK_TOOL,))

    async with bridge_to(app, first_token) as one, bridge_to(app, second_token) as two:
        reply_one = await one.call_tool("request_check", {"dc": 5})
        reply_two = await two.call_tool("request_check", {"dc": 10})
        tokens.revoke(first_token)
        with pytest.raises(MCPError):
            await one.call_tool("request_check", {"dc": 5})
        still_works = await two.call_tool("request_check", {"dc": 15})

    assert [block.text for block in reply_one.content] == ["первый"]
    assert [block.text for block in reply_two.content] == ["второй"]
    assert [block.text for block in still_works.content] == ["второй"]
    assert first.calls == [("request_check", {"dc": 5})]
    assert second.calls == [("request_check", {"dc": 10}), ("request_check", {"dc": 15})]


async def test_a_bridge_behind_a_foreign_address_gets_nothing():
    app, tokens = dev_app()
    tools = RecordingTools()
    token = tokens.issue(tools, (CHECK_TOOL,))

    async with bridge_to(app, token, client=FOREIGN_CLIENT) as mcp:
        with pytest.raises(MCPError):
            await mcp.list_tools()
        with pytest.raises(MCPError):
            await mcp.call_tool("request_check", {"dc": 15})

    assert tools.calls == []


async def test_a_crash_of_the_executor_is_an_error_for_the_bridge_not_a_success():
    async def broken(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        raise RuntimeError("баг движка")

    app, tokens = dev_app()
    token = tokens.issue(broken, (CHECK_TOOL,))

    async with bridge_to(app, token) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool("request_check", {"dc": 15})


@asynccontextmanager
async def serving(app) -> AsyncIterator[str]:
    """Приложение на настоящем локальном порту: к нему ходит процесс моста, а не ASGI-транспорт."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await task


async def test_the_bridge_process_works_against_the_real_server_over_http():
    # Настоящий процесс моста по stdio и настоящий внутренний API по HTTP: правила доступа сервера
    # (локальный клиент, ни одного заголовка пересылки, токен хода) применяются к клиенту моста.
    app, tokens = dev_app()
    tools = RecordingTools("вызван request_check")
    token = tokens.issue(tools, engine_tool_schemas())

    async with serving(app) as base_url:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "dnd_server.llm.mcp_bridge",
                "--url",
                f"{base_url}/internal/dm-tools/{token}",
            ],
            # Прокси из окружения не должен перехватывать вызов: токен ушёл бы постороннему узлу.
            env={"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"},
        )
        async with Client(params) as mcp:
            listed = await mcp.list_tools()
            result = await mcp.call_tool("request_check", {"character_id": "pc-1", "dc": 15})
        tokens.revoke(token)
        async with Client(params) as after_the_turn:
            with pytest.raises(MCPError):
                await after_the_turn.list_tools()

    assert {tool.name for tool in listed.tools} == {tool.name for tool in engine_tool_schemas()}
    assert [block.text for block in result.content] == ["вызван request_check"]
    assert tools.calls == [("request_check", {"character_id": "pc-1", "dc": 15})]
