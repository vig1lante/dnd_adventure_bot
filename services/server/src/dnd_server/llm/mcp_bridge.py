"""Мост MCP для `claude_code_cli`.

`python -m dnd_server.llm.mcp_bridge --url <база>/internal/dm-tools/<токен>` — stdio MCP-сервер,
который CLI `claude` запускает сам по `mcp.json`. Мост ничего не решает: список инструментов он
берёт у внутреннего API сервера (`GET …/tools`), вызов пересылает туда же (`POST …/call`), а
`is_error` результата превращает в ошибку инструмента MCP. Правила и числа считает сервер.

Токен хода живёт только в адресе и действует один ход. Сбой внутреннего API (неизвестный или
отозванный токен, 5xx, нет связи) мост не прячет за пустым списком или «успешным» результатом:
исключение SDK превращает в ошибку JSON-RPC, и CLI видит, что инструмент недоступен. В тексте
исключения нет адреса (`ask_internal_api`): токен хода не попадает в трассировку в stderr моста.
"""

import argparse
from typing import Any

import anyio
import httpx
from mcp import stdio_server
from mcp.server import Server, ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

SERVER_NAME = "dm"
HTTP_TIMEOUT_S = 30.0  # инструмент считает движок: вызов быстрый, зависание — сбой


async def ask_internal_api(http: httpx.AsyncClient, method: str, path: str, **options: Any) -> Any:
    """JSON-ответ внутреннего API; любой сбой — `RuntimeError` без адреса и без цепочки причин.

    Исключения httpx несут запрос, а с ним адрес с токеном хода, а SDK пишет трассировку сбоя
    обработчика в stderr моста — туда, где её может сохранить CLI. Поэтому наружу выходит только
    код ответа или имя типа сбоя."""
    failure = None
    try:
        response = await http.request(method, path, **options)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        failure = f"внутренний API ответил {error.response.status_code}"
    except httpx.HTTPError as error:
        failure = f"внутренний API недоступен: {type(error).__name__}"
    if failure is not None:
        # Поднимаем вне `except`: иначе исходное исключение осталось бы в `__context__`.
        raise RuntimeError(failure)
    return response.json()


def build_server(http: httpx.AsyncClient) -> Server:
    """MCP-сервер поверх клиента с `base_url` хода: `…/internal/dm-tools/<токен>`."""

    async def list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        schemas = await ask_internal_api(http, "GET", "/tools")
        return ListToolsResult(tools=[Tool(**schema) for schema in schemas])

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        reply = await ask_internal_api(
            http, "POST", "/call", json={"name": params.name, "args": params.arguments or {}}
        )
        return CallToolResult(
            content=[TextContent(type="text", text=reply["result"])], is_error=reply["is_error"]
        )

    return Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(url: str) -> None:
    # trust_env=False: прокси из окружения не должен перехватить вызов внутреннего API —
    # токен хода ушёл бы постороннему узлу.
    async with httpx.AsyncClient(base_url=url, timeout=HTTP_TIMEOUT_S, trust_env=False) as http:
        server = build_server(http)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="Мост MCP к внутреннему API инструментов мастера")
    parser.add_argument("--url", required=True, help="<база>/internal/dm-tools/<токен>")
    anyio.run(serve, parser.parse_args().url)


if __name__ == "__main__":
    main()
