import asyncio
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import sysconfig
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_server.app import create_app
from dnd_server.bot import bot as bot_module
from dnd_server.config import Settings, get_settings
from dnd_server.db.session import make_engine, make_sessionmaker

# --- запрет живых вызовов во всех тестах сервера (спека §13, инвариант «и») ---
#
# Тесты не ходят в сеть, к живым моделям и к настоящему `claude`. Разрешены: память процесса
# (ASGI-транспорт `httpx`, `MockTransport` SDK), loopback (локальный uvicorn и мост MCP), три
# процесса проекта (имитатор `fake_claude.py`, мост MCP и установленная команда `dnd-generate`)
# и справочные утилиты ОС, которые зовёт стандартный `platform`.
# Попытка выйти дальше роняет вызов `AssertionError`, а SDK и адаптеры часто глотают такие
# исключения (`APIConnectionError` -> `ProviderError`), поэтому попытки ещё и записываются:
# тест с записанной попыткой падает при завершении, что бы ни сделал код с исключением.

LOCAL_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})
# Адреса «любой интерфейс» нужны только для привязки сервера, но их разбор тоже не выходит наружу.
BIND_NAMES = frozenset({"", "0.0.0.0", "::"})
FAKE_CLAUDE = Path(__file__).resolve().parent / "llm" / "fake_claude.py"
BRIDGE_ARGS = ["-m", "dnd_server.llm.mcp_bridge"]
GENERATOR_SCRIPT = "dnd-generate"
# Справочные утилиты ОС: их зовёт стандартный `platform` (заголовки запросов SDK), модели и сети
# они не касаются.
SYSTEM_INFO_COMMANDS = frozenset({"uname", "file", "sw_vers"})
SYSTEM_DIRECTORIES = frozenset({"/usr/bin", "/bin", "/usr/sbin", "/sbin"})
WRAPPER_READ_BYTES = 1024 * 1024


def _local_address(address: Any) -> bool:
    """Путь unix-сокета или кортеж с локальным именем хоста; всё остальное — внешний адрес."""
    if isinstance(address, str | bytes):
        return True
    return isinstance(address, tuple) and bool(address) and address[0] in LOCAL_NAMES


def _local_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    return host in LOCAL_NAMES | BIND_NAMES


def _is_fake_claude_wrapper(program: Path) -> bool:
    """Скрипт-обёртка тестов, которая запускает имитатор: `#!`-файл со ссылкой на него в начале.

    Настоящий `claude` — двоичный файл или многомегабайтный скрипт без такой ссылки."""
    try:
        if not program.is_file():
            return False
        with program.open("rb") as handle:
            head = handle.read(WRAPPER_READ_BYTES)
    except OSError:
        return False
    return head.startswith(b"#!") and FAKE_CLAUDE.name.encode() in head


def _refusal_for_process(argv: list[str]) -> str | None:
    """Почему процесс запускать нельзя; `None` — можно. Несуществующую программу не трогаем:
    запускаться нечему, а ошибку «нет такого файла» тесты адаптера проверяют сами."""
    found = shutil.which(argv[0])
    if found is None:
        return None
    program = Path(found)
    if program.resolve() == Path(sys.executable).resolve():
        # Интерпретатор проекта: только мост MCP и имитатор, но не произвольный код.
        if argv[1:3] == BRIDGE_ARGS or argv[1:2] == [str(FAKE_CLAUDE)]:
            return None
        return "интерпретатор разрешён только для моста MCP и fake_claude.py"
    if program.name in SYSTEM_INFO_COMMANDS and str(program.parent) in SYSTEM_DIRECTORIES:
        return None
    scripts = Path(sysconfig.get_path("scripts")).resolve()
    if program.name == GENERATOR_SCRIPT and program.parent.resolve() == scripts:
        return None
    if _is_fake_claude_wrapper(program):
        return None
    return "разрешены только имитатор fake_claude.py, мост MCP и команда dnd-generate"


@pytest.fixture(autouse=True)
def live_call_attempts(monkeypatch) -> Iterator[list[str]]:
    """Запрещает сеть и чужие процессы; список записанных попыток проверяется при завершении.

    Тест, который сам проверяет запрет, очищает список после своих проверок."""
    attempts: list[str] = []

    def refuse(what: str, detail: str) -> None:
        attempts.append(f"{what}: {detail}")
        raise AssertionError(f"тест вышел за пределы памяти и loopback ({what}): {detail}")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_popen_init = subprocess.Popen.__init__

    def connect(self, address):
        if not _local_address(address):
            refuse("connect", repr(address))
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _local_address(address):
            refuse("connect_ex", repr(address))
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _local_host(host):
            refuse("getaddrinfo", repr(host))
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host):
        if not _local_host(host):
            refuse("gethostbyname", repr(host))
        return real_gethostbyname(host)

    def popen_init(self, args, *other, **options):
        # Единая точка входа для `subprocess.run`, `asyncio.create_subprocess_exec` и anyio.
        if isinstance(args, str | bytes) or options.get("shell"):
            refuse("процесс", "команда оболочкой")
        argv = (
            [os.fsdecode(args)] if isinstance(args, os.PathLike) else [os.fsdecode(a) for a in args]
        )
        reason = _refusal_for_process(argv) if argv else None
        if reason is not None:
            refuse("процесс", f"{argv[0]}: {reason}")
        return real_popen_init(self, args, *other, **options)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(subprocess.Popen, "__init__", popen_init)
    yield attempts
    assert not attempts, "тест пытался выйти за пределы памяти: " + "; ".join(attempts)


# Переменные, из которых SDK моделей сами берут адрес, учётные данные и заголовки запросов. Сборка
# провайдеров (`llm/factory.py`) на них реагирует: часть отвергает ошибкой настройки, часть
# отменяет явными аргументами. Тесты, которым эти переменные нужны, ставят их сами.
SDK_ENVIRONMENT = (
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
)


@pytest.fixture(autouse=True)
def isolated_settings_sources(monkeypatch, tmp_path):
    # Settings читает `.env` из текущего каталога и переменные окружения. Пустой
    # каталог и снятые переменные делают тесты независимыми от окружения владельца.
    monkeypatch.chdir(tmp_path)
    # pydantic-settings сопоставляет имена без учёта регистра (`app_env` читается
    # так же, как `APP_ENV`), поэтому снимаем переменные в любом регистре.
    for variable in list(os.environ):
        if variable.lower() in Settings.model_fields:
            monkeypatch.delenv(variable)
    # Переменные SDK владельца не должны менять сборку провайдеров и запросы в тестах.
    for variable in SDK_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)
    # get_settings кэширует результат: чистим кэш до и после теста, чтобы настройки
    # одного теста не просачивались в другой.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- таблица маршрутов приложения (финальное покрытие B6) ---
#
# Общие проверки блока обходят маршруты, которые приложение действительно подключило (REST,
# WebSocket, внутренний API в dev), а не список роутеров из теста: маршрут, добавленный позже,
# попадёт в обход сам. FastAPI не разворачивает подключённые роутеры в `app.routes`, поэтому
# обход идёт через `iter_route_contexts`, как у самой схемы OpenAPI.
WEBSOCKET = "WEBSOCKET"
# Маршруты, которые FastAPI добавляет сам (документация, только в dev): без зависимостей проекта.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


@dataclass(frozen=True)
class MountedRoute:
    method: str  # GET, POST и так далее; у WebSocket — `WEBSOCKET`
    path: str  # с префиксом роутера, например `/api/games/{game_id}/ready`
    dependant: Dependant


def mounted_routes(app: FastAPI) -> list[MountedRoute]:
    """Все маршруты приложения по одному на метод. Неизвестный вид маршрута (подприложение
    `Mount` и подобное) останавливает обход, а не пропускается молча."""
    routes: list[MountedRoute] = []
    for context in iter_route_contexts(app.routes):
        original = context.original_route
        if isinstance(original, APIRoute):
            routes += [
                MountedRoute(m, context.path, context.dependant) for m in sorted(context.methods)
            ]
        elif isinstance(original, APIWebSocketRoute):
            routes.append(MountedRoute(WEBSOCKET, context.path, context.dependant))
        else:
            assert context.path in FRAMEWORK_PATHS, f"обход не знает маршрут {context.path!r}"
    return routes


@pytest.fixture
def route_table() -> Callable[[FastAPI], list[MountedRoute]]:
    """Обход маршрутов приложения: `route_table(create_app(settings))`."""
    return mounted_routes


# --- база данных тестов (план B6.1) ---
#
# Каждый тест работает со своим файлом SQLite во временном каталоге. Базу владельца
# (`<корень>/data/`) тесты не трогают, и это не обещание, а запрет: любая попытка открыть файл
# внутри неё записывается и роняет тест.

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
# Корень репозитория считаем независимо от кода сервера: запрет не должен следовать за ним,
# если код неверно определит корень.
OWNER_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


@pytest.fixture(autouse=True)
def owner_database_guard(monkeypatch, live_call_attempts: list[str]) -> None:
    """Запрещает открывать файлы SQLite в `<корень>/data/`; попытка записывается как живой вызов."""
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        is_file_path = isinstance(database, str | os.PathLike)
        if is_file_path and Path(database).resolve().is_relative_to(OWNER_DATA_DIR):
            live_call_attempts.append(f"sqlite: {database}")
            raise AssertionError(f"тест открыл базу владельца: {database}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


# --- Telegram-бот в тестах (план B6.6) ---
#
# Приложение с заданным `telegram_bot_token` поднимает в `lifespan` задачу опроса Telegram. Без
# подмены тест, который просто запускает приложение с токеном (вход через Telegram, проверка
# секрета), оставил бы задачу, идущую на api.telegram.org: сеть запрещена, но задача успевает
# записать попытку, и тест падает случайно. Поэтому по умолчанию `make_bot` отвечает «бота нет»,
# а тесты, которым нужен бот, подставляют свой (`monkeypatch.setattr(bot_module, "make_bot", ...)`)
# или берут настоящую сборку маркером `real_make_bot`.


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_make_bot: тест проверяет настоящую `make_bot` (токен -> `Bot`), а не заглушку",
    )


@pytest.fixture(autouse=True)
def no_telegram_bot(monkeypatch, request) -> None:
    """Подменяет `make_bot` на «бота нет»; маркер `real_make_bot` оставляет настоящую."""
    if request.node.get_closest_marker("real_make_bot") is not None:
        return
    monkeypatch.setattr(bot_module, "make_bot", lambda _settings: None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Настройки dev-режима с базой во временном каталоге.

    Папки для файла базы нет: её создаёт код (приложение или Alembic), а не фикстура.
    """
    return Settings(
        app_env="dev",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/data/test.sqlite3",
    )


@dataclass(frozen=True)
class Leak:
    """Ошибка, которой в журнал сервера нельзя класть значения входа (`input_value`).

    `value` — кусок «состояния игры» (запись летописи), `error` — ошибка pydantic, в тексте которой
    это значение лежит. Тесты записи ошибок в журнал ищут `value` в записях."""

    value: str
    error: ValidationError


@pytest.fixture
def leak() -> Leak:
    class Row(BaseModel):
        chronicle: str = Field(max_length=3)

    value = "ТАЙНАЯ-ЗАПИСЬ-ЛЕТОПИСИ"
    try:
        Row(chronicle=value)
    except ValidationError as error:
        return Leak(value, error)
    raise AssertionError("ошибка pydantic не возникла")


@pytest.fixture
def migrate(settings: Settings, monkeypatch) -> Callable[[str, str], Awaitable[None]]:
    """Запускает Alembic на базе теста: `await migrate("upgrade", "head")`.

    Адрес базы env.py берёт из настроек, поэтому он передаётся окружением, как при запуске из
    терминала. Сам env.py запускает цикл событий, значит команда идёт в отдельном потоке.
    """
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    alembic_config = Config(str(ALEMBIC_INI))

    async def run(command_name: str, revision: str) -> None:
        await asyncio.to_thread(getattr(command, command_name), alembic_config, revision)

    return run


@pytest.fixture
async def db(
    settings: Settings, migrate: Callable[[str, str], Awaitable[None]]
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на базе теста, поднятой миграциями до `head`: `async with db() as session`."""
    await migrate("upgrade", "head")
    engine = make_engine(settings.database_url)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def client(
    settings: Settings, db: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент к приложению в памяти (ASGI) на той же мигрированной базе, что и `db`."""
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http
    await app.state.engine.dispose()
