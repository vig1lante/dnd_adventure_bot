"""Общий контракт ошибок: любой ответ 422 — `{"detail": "<русский текст>"}`.

FastAPI по умолчанию кладёт в `detail` список с входными значениями. Для Mini App нужна строка, а
недоверенный ввод не должен возвращаться клиенту эхом: ни значение поля, ни чужой ключ.
"""

import re

import httpx
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from dnd_server.app import create_app
from dnd_server.db.session import get_session
from dnd_server.games.service import GameCodeUnavailable

ECHO_PROBE = "Секрет-Значение-4242-<script>"


async def test_validation_error_is_one_russian_string_naming_the_field(client):
    response = await client.post("/api/auth/dev", json={"login": ECHO_PROBE})

    assert response.status_code == 422
    body = response.json()
    assert list(body) == ["detail"]
    assert isinstance(body["detail"], str)
    assert re.search("[А-Яа-я]", body["detail"])
    assert "login" in body["detail"]


BROKEN_JSON = {
    "content": b'{"login": "' + ECHO_PROBE.encode(),
    "headers": {"Content-Type": "application/json"},
}


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({"json": {"login": ECHO_PROBE}}, id="значение вне допустимых"),
        pytest.param({"json": {"login": [ECHO_PROBE]}}, id="не тот тип"),
        pytest.param({"json": {ECHO_PROBE: 1}}, id="чужой ключ"),
        pytest.param({"json": ECHO_PROBE}, id="тело не объект"),
        pytest.param(BROKEN_JSON, id="битый JSON"),
    ],
)
async def test_validation_error_never_echoes_untrusted_input(client, request_kwargs):
    response = await client.post("/api/auth/dev", **request_kwargs)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert ECHO_PROBE not in response.text
    assert "script" not in response.text


# --- необработанное исключение тоже отвечает по общему контракту ---

SECRET_IN_ERROR = "секрет-из-текста-исключения-7788"


@pytest.fixture
async def broken_app(settings):
    """Приложение, у которого запрос падает на первом обращении к базе: `broken_app(error)`."""
    apps = []

    def build(error: Exception):
        async def failing_session():
            raise error

        app = create_app(settings)
        app.dependency_overrides[get_session] = failing_session
        apps.append(app)
        return app

    yield build
    for app in apps:
        await app.state.engine.dispose()


UNEXPECTED_ERRORS = [
    pytest.param(GameCodeUnavailable(SECRET_IN_ERROR), id="код игры не нашёлся"),
    pytest.param(OperationalError("SELECT 1", {}, Exception(SECRET_IN_ERROR)), id="база занята"),
    pytest.param(IntegrityError("INSERT", {}, Exception(SECRET_IN_ERROR)), id="код игры занят"),
    pytest.param(RuntimeError(SECRET_IN_ERROR), id="любая другая ошибка"),
]


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
async def test_an_unexpected_error_is_a_json_500_without_details(broken_app, error):
    app = broken_app(error)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post("/api/auth/dev", json={"login": "dev-1"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert list(body) == ["detail"]
    assert re.search("[А-Яа-я]", body["detail"])
    assert SECRET_IN_ERROR not in response.text


async def test_an_unexpected_error_still_reaches_the_server_for_its_log(broken_app):
    # Ответ уходит клиенту, а исключение пробрасывается дальше: так uvicorn пишет трассировку.
    app = broken_app(RuntimeError(SECRET_IN_ERROR))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        with pytest.raises(RuntimeError, match=SECRET_IN_ERROR):
            await client.post("/api/auth/dev", json={"login": "dev-1"})


async def test_the_general_handler_leaves_the_known_errors_to_their_own_handlers(client):
    invalid = await client.post("/api/auth/dev", json={"login": [ECHO_PROBE]})
    missing_route = await client.get("/api/no-such-route")

    assert invalid.status_code == 422
    assert missing_route.status_code == 404
