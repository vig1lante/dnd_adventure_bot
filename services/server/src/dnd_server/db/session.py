"""Подключение к базе: путь к файлу SQLite, движок, фабрика сессий и зависимость FastAPI.

Относительный путь SQLite из `DATABASE_URL` считается от корня репозитория, а не от текущего
каталога (план B6.1). Иначе сервер, запущенный из `services/server` или `services/webapp`, завёл
бы рядом свою пустую базу, а миграции из корня и приложение смотрели бы в разные файлы. Абсолютный
путь допустим где угодно (VPS), кроме `services/webapp` (при любом регистре букв там, где файловая
система его не различает): эту папку раздаёт Vite. Если корень репозитория не найден (пакет
установлен без режима разработки), относительный путь отвергается.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# session.py -> db -> dnd_server -> src -> server -> services -> корень репозитория.
# Пакет ставится в режиме разработки (uv workspace), поэтому __file__ лежит в самом репозитории.
REPO_ROOT = Path(__file__).resolve().parents[5]
WEBAPP_DIR = REPO_ROOT / "services" / "webapp"
# Файл, который есть только в репозитории: по нему видно, что `REPO_ROOT` указывает на корень.
# При установке без режима разработки пакет лежит в `.venv`, и корнем стал бы он.
REPO_MARKER = Path("services") / "server" / "alembic.ini"


class DatabasePathError(ValueError):
    """Путь к файлу SQLite недопустим. В сообщении только путь, остальной адрес не повторяется."""


def _sqlite_file(url: URL) -> Path | None:
    """Файл базы SQLite или `None`, если база не файловая (в памяти или другая СУБД)."""
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return None
    return Path(url.database)


def is_inside_folder(path: Path, folder: Path) -> bool:
    """Лежит ли `path` в `folder` (или это сама папка), с учётом вторых имён папки.

    Сначала сравниваются имена. Но на файловой системе без учёта регистра (macOS APFS, Windows)
    `services/WebApp` — та же папка, что `services/webapp`, а `resolve()` регистр не исправляет, и
    строковое сравнение алиас бы пропустило. Поэтому существующие предки пути сверяются с папкой по
    самому файлу (`samefile`). У несуществующего предка второго имени быть не может, а на
    файловой системе, различающей регистр, `services/WebApp` — просто другая папка."""
    if path.is_relative_to(folder):
        return True
    return folder.exists() and any(
        ancestor.exists() and ancestor.samefile(folder) for ancestor in (path, *path.parents)
    )


def resolve_database_url(raw_url: str) -> URL:
    """Адрес базы с абсолютным путём к файлу SQLite.

    Отказ, если файл лежит в `services/webapp` или относительный путь не от чего считать (корень
    репозитория не найден): иначе база молча легла бы в `.venv`, и пересоздание окружения стёрло
    бы сохранённые игры."""
    url = make_url(raw_url)
    file = _sqlite_file(url)
    if file is None:
        return url
    if not file.is_absolute() and not (REPO_ROOT / REPO_MARKER).is_file():
        raise DatabasePathError(
            f"Относительный путь к базе {file} не от чего считать: корень репозитория не найден "
            f"({REPO_ROOT}), так бывает при установке без режима разработки. "
            "Укажите абсолютный путь в DATABASE_URL."
        )
    resolved = (REPO_ROOT / file).resolve()  # абсолютный путь остаётся как есть
    if is_inside_folder(resolved, WEBAPP_DIR):
        raise DatabasePathError(
            f"Файл базы {resolved} лежит в services/webapp: эту папку раздаёт Vite, и базу "
            "можно было бы скачать по HTTP. Укажите другой путь в DATABASE_URL "
            "(по умолчанию data/dnd.sqlite3 в корне репозитория)."
        )
    return url.set(database=str(resolved))


def create_database_directory(url: URL) -> None:
    """Создаёт папку файла SQLite, если её нет. Ожидает адрес после `resolve_database_url`."""
    file = _sqlite_file(url)
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)


def _enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    # SQLite проверяет внешние ключи, только если включить это на каждом соединении.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str) -> AsyncEngine:
    """Асинхронный движок; для SQLite включены внешние ключи. Соединение открывается лениво.

    `hide_parameters`: в тексте ошибки базы нет значений запроса. Иначе сбой записи хода понёс бы
    в журнал сервера и в `dm_turn_logs.error` снимок состояния, тексты личных сообщений и тайных
    ходов; имя ошибки, SQL без значений и причина остаются."""
    engine = create_async_engine(resolve_database_url(url), hide_parameters=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _enable_foreign_keys)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после коммита атрибуты остаются доступны, а в async-сессии
    # неявно перечитать устаревший атрибут нельзя.
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на один запрос. Коммитит вызывающий; без коммита — откат."""
    async with request.app.state.sessionmaker() as session:
        yield session
