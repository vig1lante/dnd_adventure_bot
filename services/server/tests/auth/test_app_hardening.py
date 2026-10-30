"""Защита приложения целиком: проверка Host и закрытая документация в prod.

Сервер слушает только localhost, и наружу его выставляет туннель или VPS. Страница злоумышленника
может привязать своё имя к 127.0.0.1 (DNS-rebinding) и ходить на сервер как на свой, поэтому
запросы с чужим именем хоста отвергаются. В prod нет и описания API.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from dnd_server.app import create_app
from dnd_server.config import Settings

PUBLIC_URL = "https://game.example.org/"


async def get(settings: Settings, path: str, *, host: str | None = None):
    app = create_app(settings)
    transport = ASGITransport(app=app)
    headers = {"Host": host} if host else {}
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get(path, headers=headers)
    await app.state.engine.dispose()
    return response


# --- проверка Host ---


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:5173",  # Vite отдаёт серверу исходный Host (в том числе для /ws через туннель)
        "127.0.0.1",
        "127.0.0.1:8000",  # прокси Vite с подменой Host
        "[::1]:8000",
        "game.example.org",
        "game.example.org:443",
    ],
)
async def test_local_names_and_the_public_host_are_served(host):
    response = await get(Settings(webapp_public_url=PUBLIC_URL), "/health", host=host)

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8000",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "game.example.org.evil.example",
        "evilgame.example.org",
        "192.168.1.5:8000",
    ],
)
async def test_any_other_host_is_refused(host):
    response = await get(Settings(webapp_public_url=PUBLIC_URL), "/health", host=host)

    assert response.status_code == 400


async def test_host_check_covers_the_api_too():
    response = await get(Settings(app_env="dev"), "/api/config", host="evil.example")

    assert response.status_code == 400


@pytest.mark.parametrize("app_env", ["dev", "prod"])
async def test_host_check_works_in_both_modes(app_env):
    settings = Settings(app_env=app_env)

    assert (await get(settings, "/health", host="localhost")).status_code == 200
    assert (await get(settings, "/health", host="evil.example")).status_code == 400


async def test_the_default_public_address_is_local():
    # Из шаблона WEBAPP_PUBLIC_URL=http://localhost:5173: чужие имена по-прежнему отвергаются.
    assert (await get(Settings(), "/health", host="localhost:5173")).status_code == 200
    assert (await get(Settings(), "/health", host="evil.example")).status_code == 400


@pytest.mark.parametrize(
    "public_url", ["https://*", "https://*.example.org", "garbage", "https://"]
)
async def test_a_broken_public_address_does_not_open_the_server_to_other_hosts(public_url):
    settings = Settings(webapp_public_url=public_url)

    assert (await get(settings, "/health", host="x.example.org")).status_code == 400
    assert (await get(settings, "/health", host="evil.example")).status_code == 400
    assert (await get(settings, "/health", host="localhost")).status_code == 200


@pytest.mark.parametrize("host", ["t", "testserver"])
async def test_names_of_in_process_test_clients_are_refused(host):
    # Одиночное имя может разрешить враждебный DNS или суффикс поиска, поэтому тестовым клиентам
    # в списке допустимых Host не место: они ходят на `http://localhost`.
    assert (await get(Settings(), "/health", host=host)).status_code == 400


async def test_a_websocket_handshake_with_a_foreign_host_is_refused():
    app = create_app(Settings(app_env="dev"))
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/ws/games/any",
        "raw_path": b"/ws/games/any",
        "query_string": b"",
        "headers": [(b"host", b"evil.example")],
        "scheme": "ws",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
        "subprotocols": ["dnd-game.v1", "x"],
    }

    await app(scope, receive, send)
    await app.state.engine.dispose()

    assert "websocket.accept" not in [message["type"] for message in sent]
    assert any(message.get("status") == 400 for message in sent)


async def test_a_request_without_a_host_header_is_refused():
    # Настоящий сервер с httptools пропускает HTTP/1.1 без Host, поэтому отказ даёт само
    # приложение: пропуск «клиентов в памяти процесса» ослабил бы проверку без причины, им хватает
    # Host в запросе.
    app = create_app(Settings(app_env="dev"))
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    await app(scope, receive, send)
    await app.state.engine.dispose()

    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 400


# --- документация API ---


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_api_description_is_closed_in_prod(path):
    response = await get(Settings(app_env="prod"), path)

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
async def test_api_description_is_open_in_dev(path):
    response = await get(Settings(app_env="dev"), path)

    assert response.status_code == 200
