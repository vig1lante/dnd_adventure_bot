import os
import shutil
import socket
import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dnd_server.config import Settings, get_settings

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
