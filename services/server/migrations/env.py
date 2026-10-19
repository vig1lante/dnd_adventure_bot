"""Окружение Alembic: адрес базы берётся из настроек сервера, миграции идут через aiosqlite."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from dnd_server.config import get_settings
from dnd_server.db import models  # noqa: F401  (регистрирует таблицы в метаданных)
from dnd_server.db.base import Base
from dnd_server.db.session import create_database_directory, resolve_database_url

target_metadata = Base.metadata

# render_as_batch: SQLite не умеет менять готовые таблицы через ALTER TABLE, и автогенерация
# следующих миграций оформляет такие изменения пакетными операциями.
CONTEXT_OPTIONS = {"target_metadata": target_metadata, "render_as_batch": True}


def run_migrations_offline() -> None:
    """Режим `--sql`: печатает SQL, ничего не выполняя и не создавая файлов."""
    url = resolve_database_url(get_settings().database_url)
    context.configure(
        url=url.render_as_string(hide_password=False),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **CONTEXT_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **CONTEXT_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = resolve_database_url(get_settings().database_url)
    # На свежем клоне папки data/ ещё нет, а первой командой из README идёт именно миграция.
    create_database_directory(url)
    # Свой движок, а не `make_engine`: при пересоздании таблиц SQLite внешние ключи должны быть
    # выключены (по умолчанию так и есть), а проверять их в миграциях нечем.
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
