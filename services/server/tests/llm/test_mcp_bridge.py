"""Тесты MCP-моста `dnd_server.llm.mcp_bridge`.

Мост — stdio MCP-сервер, который отдаёт CLI инструменты мастера и пересылает их вызовы во
внутренний API сервера. Внешней сети и настоящего `claude` здесь нет: HTTP-слой — это
`httpx.MockTransport`, а клиент MCP — настоящий SDK, подключённый к серверу в том же процессе.
Один smoke-тест запускает мост отдельным процессом (`python -m ...`) против локального
HTTP-сервера на стандартной библиотеке: так проверяется точка входа и обмен по stdio.
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from dnd_server.llm.mcp_bridge import build_server

BASE_URL = "http://dm.test/internal/dm-tools/turn-token"
CHECK_TOOL = {
    "name": "request_check",
    "description": "Запросить проверку навыка.",
    "input_schema": {
        "type": "object",
        "properties": {"dc": {"type": "integer"}},
        "required": ["dc"],
    },
}


@pytest.fixture(autouse=True)
def only_loopback(monkeypatch):
    """Подстраховка: в этих тестах разрешены соединения только с локальной машиной."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise AssertionError(f"тест пытался выйти в сеть: {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class FakeInternalApi:
    """Внутренний API сервера вместо сети: запоминает запросы моста и отвечает заданным."""

    def __init__(self, tools=(CHECK_TOOL,), call_reply: dict[str, Any] | None = None) -> None:
        self.tools = list(tools)
        self.call_reply = call_reply or {"result": "Бросок: 17 против Сл 15", "is_error": False}
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/tools"):
            return httpx.Response(200, json=self.tools)
        if request.method == "POST" and request.url.path.endswith("/call"):
            return httpx.Response(200, json=self.call_reply)
        return httpx.Response(404, json={"detail": "Not Found"})

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handle))


async def test_list_tools_returns_schemas_from_internal_api():
    api = FakeInternalApi()

    async with Client(build_server(api.http_client())) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == ["request_check"]
    assert listed.tools[0].description == "Запросить проверку навыка."
    assert listed.tools[0].input_schema == CHECK_TOOL["input_schema"]
    assert [(r.method, r.url.path) for r in api.requests] == [
        ("GET", "/internal/dm-tools/turn-token/tools")
    ]


async def test_call_tool_forwards_name_and_args_and_returns_result_text():
    api = FakeInternalApi()

    async with Client(build_server(api.http_client())) as client:
        result = await client.call_tool("request_check", {"dc": 15})

    assert result.is_error is False
    assert [block.text for block in result.content] == ["Бросок: 17 против Сл 15"]
    # Клиент SDK сам запрашивает список инструментов перед вызовом: считаем только POST.
    (request,) = [r for r in api.requests if r.method == "POST"]
    assert request.url.path == "/internal/dm-tools/turn-token/call"
    assert json.loads(request.content) == {"name": "request_check", "args": {"dc": 15}}


async def test_call_tool_without_arguments_sends_empty_args():
    api = FakeInternalApi()

    async with Client(build_server(api.http_client())) as client:
        await client.call_tool("rest", None)

    (request,) = [r for r in api.requests if r.method == "POST"]
    assert json.loads(request.content) == {"name": "rest", "args": {}}


async def test_tool_error_becomes_mcp_tool_error():
    api = FakeInternalApi(call_reply={"result": "Ошибка: Сл 12 не из таблицы", "is_error": True})

    async with Client(build_server(api.http_client())) as client:
        result = await client.call_tool("request_check", {"dc": 12})

    assert result.is_error is True
    assert [block.text for block in result.content] == ["Ошибка: Сл 12 не из таблицы"]


async def test_unknown_or_revoked_token_fails_list_tools_instead_of_listing():
    api = FakeInternalApi()
    # Даже если в теле ответа что-то похожее на список, статус 404 означает отказ.
    api.handle = lambda request: httpx.Response(404, json=[CHECK_TOOL])

    async with Client(build_server(api.http_client())) as client:
        with pytest.raises(MCPError):
            await client.list_tools()


async def test_internal_api_failure_on_call_is_an_error_not_a_success():
    api = FakeInternalApi()
    list_tools = api.handle
    # Список инструментов отдаётся нормально, а сам вызов падает со статусом 500.
    api.handle = lambda request: (
        httpx.Response(500, json={"result": "похоже на успех", "is_error": False})
        if request.method == "POST"
        else list_tools(request)
    )

    async with Client(build_server(api.http_client())) as client:
        with pytest.raises(MCPError):
            await client.call_tool("request_check", {"dc": 15})


# --- сбой внутреннего API не выдаёт токен хода ---

TURN_TOKEN = "s3cr3t-turn-token"
TOKEN_URL = f"http://dm.test/internal/dm-tools/{TURN_TOKEN}"


def refuse_with(status: int):
    return lambda request: httpx.Response(status, json={"detail": "отказ"})


def break_with(error: type[httpx.TransportError]):
    # Текст сбоя нарочно содержит адрес запроса: так он выглядит у части транспортов httpx.
    def handler(request: httpx.Request) -> httpx.Response:
        raise error(f"сбой запроса {request.url}", request=request)

    return handler


API_FAILURES = [
    pytest.param(refuse_with(403), id="403"),
    pytest.param(refuse_with(404), id="404"),
    pytest.param(refuse_with(500), id="500"),
    pytest.param(break_with(httpx.ConnectError), id="нет соединения"),
    pytest.param(break_with(httpx.ReadTimeout), id="тайм-аут"),
]


def exception_chain(error: BaseException):
    """Исключение и вся его цепочка причин и контекстов (как её обойдёт любой сборщик ошибок)."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending += [current.__cause__, current.__context__]


@pytest.mark.parametrize("handler", API_FAILURES)
@pytest.mark.parametrize("operation", ["list_tools", "call_tool"])
async def test_internal_api_failure_never_puts_the_turn_token_into_errors_or_logs(
    handler, operation, caplog
):
    http = httpx.AsyncClient(base_url=TOKEN_URL, transport=httpx.MockTransport(handler))

    async with Client(build_server(http)) as client:
        with pytest.raises(MCPError) as failure:
            if operation == "list_tools":
                await client.list_tools()
            else:
                await client.call_tool("request_check", {"dc": 15})

    # Клиент MCP получает ошибку, а не успех или пустой список, и не видит токен.
    assert TURN_TOKEN not in str(failure.value)
    # SDK пишет трассировку сбоя обработчика в журнал; если перестанет, тест надо пересмотреть.
    logged = [record for record in caplog.records if record.exc_info]
    assert logged
    assert TURN_TOKEN not in caplog.text
    for record in logged:
        for error in exception_chain(record.exc_info[1]):
            # Исключения httpx несут запрос с адресом, а значит и токен: в цепочке их быть не может.
            assert not isinstance(error, httpx.HTTPError)
            assert TURN_TOKEN not in f"{error} {error!r}"


@pytest.mark.parametrize("operation", ["list_tools", "call_tool"])
async def test_bridge_process_does_not_print_the_turn_token_to_stderr(
    operation, refusing_internal_api, tmp_path
):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dnd_server.llm.mcp_bridge", "--url", refusing_internal_api],
    )

    # stderr моста пишем в файл: по умолчанию SDK привязывает его к stderr на момент импорта.
    with (tmp_path / "bridge-stderr.txt").open("w+", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog)) as client:
            with pytest.raises(MCPError):
                if operation == "list_tools":
                    await client.list_tools()
                else:
                    await client.call_tool("request_check", {"dc": 15})
        errlog.seek(0)
        stderr = errlog.read()

    assert "Traceback" in stderr  # мост действительно записал сбой в stderr, а не промолчал
    assert TURN_TOKEN not in stderr


# --- отдельный процесс моста по stdio ---


@pytest.fixture
def local_internal_api():
    """Локальный HTTP-сервер вместо сервера игры: отдаёт один инструмент и эхо-результат."""
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append(("GET", self.path, None))
            self._reply([CHECK_TOOL])

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, payload))
            self._reply({"result": f"вызван {payload['name']}", "is_error": False})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/internal/dm-tools/turn-token", seen
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def refusing_internal_api():
    """Локальный HTTP-сервер, который на любой запрос отвечает 404: токен хода уже отозван."""

    class Handler(BaseHTTPRequestHandler):
        def _refuse(self) -> None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _refuse

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/internal/dm-tools/{TURN_TOKEN}"
    server.shutdown()
    server.server_close()
    thread.join()


async def test_bridge_process_serves_mcp_over_stdio(local_internal_api):
    url, seen = local_internal_api
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dnd_server.llm.mcp_bridge", "--url", url],
        # Прокси из окружения не должен перехватывать вызов внутреннего API: токен хода
        # ушёл бы постороннему узлу, а вызов не дошёл бы до сервера.
        env={"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"},
    )

    async with Client(params) as client:
        listed = await client.list_tools()
        result = await client.call_tool("request_check", {"dc": 15})

    assert [tool.name for tool in listed.tools] == ["request_check"]
    assert [block.text for block in result.content] == ["вызван request_check"]
    posts = [entry for entry in seen if entry[0] == "POST"]
    assert posts == [
        (
            "POST",
            "/internal/dm-tools/turn-token/call",
            {"name": "request_check", "args": {"dc": 15}},
        )
    ]
    assert ("GET", "/internal/dm-tools/turn-token/tools", None) in seen
