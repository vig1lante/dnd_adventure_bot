"""Закрытые по умолчанию входы dev-режима: внутренний API и `claude_code_cli`.

Оба входа должны открываться одним и тем же значением `APP_ENV`, из тех же источников настроек
(окружение и `.env`), и только строгим `dev`. По отдельности каждый вход проверен в своих тестах;
здесь проверяется, что они не расходятся, и то, что видно только на настоящем HTTP-сервере.
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from dnd_server.__main__ import main
from dnd_server.app import create_app
from dnd_server.config import get_settings
from dnd_server.llm.base import ToolSchema
from dnd_server.llm.factory import ConfigError, make_provider
from dnd_server.llm.internal_api import TurnTokens

INTERNAL_URL = "http://127.0.0.1:8000"
# Заголовки, которые добавляет туннель cloudflared и типичные прокси. Список записан здесь, а не
# берётся из кода: тест не должен следовать за кодом, если из него заголовок пропадёт.
FORWARDING_HEADERS = ["X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP"]
CHECK_TOOL = ToolSchema("request_check", "Проверка.", {"type": "object", "properties": {}})

# (APP_ENV в окружении, содержимое .env, открыты ли оба входа). Приоритет источников: окружение
# важнее `.env`, пустое значение окружения тоже важнее; без значения сервер работает как prod.
GATE_CASES = [
    pytest.param("dev", None, True, id="окружение dev"),
    pytest.param(None, "APP_ENV=dev\n", True, id=".env dev"),
    pytest.param("dev", "APP_ENV=prod\n", True, id="окружение dev важнее .env prod"),
    pytest.param("prod", "APP_ENV=dev\n", False, id="окружение prod важнее .env dev"),
    pytest.param("", "APP_ENV=dev\n", False, id="пустое окружение важнее .env dev"),
    pytest.param(None, "APP_ENV=prod\n", False, id=".env prod"),
    pytest.param(None, None, False, id="не задано"),
    pytest.param("", None, False, id="пусто"),
]


class RecordingTools:
    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        return "результат", False


LOCAL_CLIENT = ("127.0.0.1", 50000)


def local_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=LOCAL_CLIENT)
    return httpx.AsyncClient(transport=transport, base_url=INTERNAL_URL)


async def internal_api_answers(app) -> bool:
    """Отвечает ли приложение локальному клиенту с настоящим токеном хода (а в prod токенов нет)."""
    tokens = getattr(app.state, "turn_tokens", None)
    token = tokens.issue(RecordingTools(), (CHECK_TOOL,)) if tokens else "any-token"
    async with local_client(app) as client:
        response = await client.get(f"/internal/dm-tools/{token}/tools")
    return response.status_code == 200


def claude_cli_is_allowed() -> bool:
    try:
        make_provider(
            get_settings(),
            provider="claude_code_cli",
            turn_tokens=TurnTokens(),
            internal_url=INTERNAL_URL,
        )
    except ConfigError:
        return False
    return True


@asynccontextmanager
async def serving(config: uvicorn.Config):
    """Приложение на настоящем локальном порту: запрос идёт через сокет и middleware uvicorn."""
    server = uvicorn.Server(config)
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


@contextmanager
def captured_server_log() -> Iterator[list[str]]:
    """Строки журналов uvicorn за время блока; сервер и его `Config` создают внутри блока.

    Журналы uvicorn глобальны для процесса: серверы других тестов могли подключить им свои
    обработчики и запретить передачу записей наверх. На время блока они сбрасываются в исходное
    состояние, а запись ловит обработчик корня. Так `access_log=False` действует, как в жизни:
    uvicorn выключает журнал доступа, убирая обработчики, и не пишет, если их нигде нет."""
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: (
        lines.append(record.getMessage()) if record.name.startswith("uvicorn") else None
    )
    loggers = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.access", "uvicorn.error")]
    saved = [(lg, lg.handlers[:], lg.propagate, lg.level) for lg in loggers]
    for logger in loggers:
        logger.handlers, logger.propagate = [], True
        logger.setLevel(logging.NOTSET)
    root = logging.getLogger()
    root_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(root_level)
        for logger, handlers, propagate, level in saved:
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)


# --- оба входа открывает один APP_ENV ---


@pytest.mark.parametrize(("app_env", "dotenv", "is_open"), GATE_CASES)
async def test_internal_api_and_claude_cli_open_and_close_together(
    monkeypatch, tmp_path, app_env, dotenv, is_open
):
    if app_env is not None:
        monkeypatch.setenv("APP_ENV", app_env)
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")

    app = create_app()  # без аргументов: настройки читаются из окружения и .env

    assert await internal_api_answers(app) is is_open
    assert hasattr(app.state, "turn_tokens") is is_open
    assert claude_cli_is_allowed() is is_open


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("suffix", ["tools", "call"])
async def test_dev_internal_api_accepts_only_the_methods_of_the_plan(method, suffix):
    app = create_app(get_settings().model_copy(update={"app_env": "dev"}))
    token = app.state.turn_tokens.issue(RecordingTools(), (CHECK_TOOL,))

    async with local_client(app) as client:
        response = await client.request(method, f"/internal/dm-tools/{token}/{suffix}")

    assert response.status_code == 405


@pytest.mark.parametrize("suffix", ["", "/exec", "/tools/extra", "/call/extra"])
async def test_dev_internal_api_has_no_other_routes(suffix):
    app = create_app(get_settings().model_copy(update={"app_env": "dev"}))
    token = app.state.turn_tokens.issue(RecordingTools(), (CHECK_TOOL,))

    async with local_client(app) as client:
        got = await client.get(f"/internal/dm-tools/{token}{suffix}", follow_redirects=False)
        posted = await client.post(f"/internal/dm-tools/{token}{suffix}", json={})

    assert got.status_code in (404, 405)
    assert posted.status_code in (404, 405)


@pytest.mark.parametrize("value", ["DEV", "Dev", "development", "production", "1", " dev", "true"])
def test_anything_but_strict_dev_or_prod_stops_the_server_before_it_opens_anything(
    monkeypatch, value
):
    monkeypatch.setenv("APP_ENV", value)

    with pytest.raises(ValidationError):
        create_app()
    with pytest.raises(ValidationError):
        claude_cli_is_allowed()


# --- настоящий HTTP: туннель и журнал ---


def dev_config(app) -> uvicorn.Config:
    # Как в `__main__.main()`: прокси-заголовки включены, доверяются только пересылке с 127.0.0.1.
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,  # настройка журналов uvicorn глобальна для процесса: тесты её не трогают
        log_level="warning",
        lifespan="off",
        forwarded_allow_ips="127.0.0.1",
    )


@pytest.mark.parametrize("claimed_address", ["198.51.100.7", "127.0.0.1"])
@pytest.mark.parametrize("header", FORWARDING_HEADERS)
async def test_traffic_that_a_tunnel_would_bring_is_refused_on_a_real_server(
    header, claimed_address
):
    app = create_app(get_settings().model_copy(update={"app_env": "dev"}))
    token = app.state.turn_tokens.issue(RecordingTools(), (CHECK_TOOL,))
    url_path = f"/internal/dm-tools/{token}/tools"

    async with (
        serving(dev_config(app)) as base_url,
        httpx.AsyncClient(base_url=base_url, trust_env=False) as client,
    ):
        tunnelled = await client.get(url_path, headers={header: claimed_address})
        local = await client.get(url_path)

    # Туннель приходит с 127.0.0.1, но с заголовком пересылки: даже «я локальный» не помогает.
    assert tunnelled.status_code == 403
    assert local.status_code == 200
    assert [tool["name"] for tool in local.json()] == ["request_check"]


async def test_turn_token_never_reaches_the_server_access_log(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append((app, options)))
    main([])  # настоящие параметры запуска сервера; сам сервер не стартует, порт не занимается
    [(_, options)] = calls
    # Приложение собираем сами (нужен реестр токенов), остальные параметры берём у main() как есть.
    options = {name: value for name, value in options.items() if name != "factory"}
    app = create_app()
    token = app.state.turn_tokens.issue(RecordingTools(), (CHECK_TOOL,))

    with captured_server_log() as server_log:
        config = uvicorn.Config(
            app, **{**options, "fd": None, "port": 0, "log_config": None, "lifespan": "off"}
        )
        async with (
            serving(config) as base_url,
            httpx.AsyncClient(base_url=base_url, trust_env=False) as client,
        ):
            response = await client.get(f"/internal/dm-tools/{token}/tools")

    assert response.status_code == 200
    assert token not in "\n".join(server_log)
