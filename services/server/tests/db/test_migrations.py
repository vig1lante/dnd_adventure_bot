"""Миграции Alembic (план B6.1): схема 0001 создаётся, откатывается и совпадает с моделями.

Правило на будущее: каждая следующая миграция добавляет тест «игра с состоянием, сохранённая
до миграции, читается после `upgrade`» (база на предыдущей ревизии, строки игры, `upgrade head`,
чтение через модели) в `test_saved_games_after_upgrade.py` и вносит его в `SAVED_GAME_TESTS` там
же: без записи падает `test_every_migration_has_a_test_that_reads_a_saved_game_after_it`. Для
0001 предыдущей ревизии нет, поэтому игра записывается прямым SQL по схеме 0001.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.config import main as alembic_main
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, inspect

from dnd_server.config import Settings, get_settings
from dnd_server.db import models  # noqa: F401  (регистрирует таблицы в метаданных)
from dnd_server.db.base import Base
from dnd_server.db.session import make_engine

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
REPO_ROOT = Path(__file__).resolve().parents[4]

# Что план B6.1 требует от таблиц. Сверка «миграция = модели» ниже ловит расхождение между ними,
# а эти таблицы — расхождение обоих с планом.
COLUMNS = {
    "users": "id tg_id dev_login name created_at",
    "games": (
        "id code adventure_id host_user_id adventure_data phase turn_phase deadline_at "
        "state_version current_turn_id provider model collect_seconds hurry_seconds camp_seconds "
        "started_at active_seconds created_at"
    ),
    "game_players": "game_id user_id character_id is_host ready joined_order joined_at",
    "characters": "id game_id user_id data",
    "pending_rolls": "game_id user_id values",
    "game_states": "game_id version schema_version data created_at",
    "turns": "id game_id number kind status state_version_before state_version_after created_at",
    "player_actions": "turn_id user_id text secret inspiration status updated_at",
    "messages": "id game_id turn_id audience kind text data superseded created_at",
    "dm_turn_logs": (
        "id game_id turn_id attempt provider model request trace output usage duration_ms error "
        "created_at"
    ),
}
PRIMARY_KEYS = {
    "users": "id",
    "games": "id",
    "game_players": "game_id user_id",
    "characters": "id",
    "pending_rolls": "game_id user_id",
    "game_states": "game_id version",
    "turns": "id",
    "player_actions": "turn_id user_id",
    "messages": "id",
    "dm_turn_logs": "id",
}
UNIQUE = {"users": ["tg_id", "dev_login"], "games": ["code"]}
# Остальные колонки обязательны.
NULLABLE = {
    "users": "tg_id dev_login",
    "games": "turn_phase deadline_at current_turn_id provider model started_at",
    "game_players": "character_id",
    "turns": "state_version_before state_version_after",
    "messages": "turn_id data",
    "dm_turn_logs": "model output usage duration_ms error",
}
FOREIGN_KEYS = {
    ("games", "host_user_id"): "users",
    ("game_players", "game_id"): "games",
    ("game_players", "user_id"): "users",
    ("game_players", "character_id"): "characters",
    ("characters", "game_id"): "games",
    ("characters", "user_id"): "users",
    ("pending_rolls", "game_id"): "games",
    ("pending_rolls", "user_id"): "users",
    ("game_states", "game_id"): "games",
    ("turns", "game_id"): "games",
    ("player_actions", "turn_id"): "turns",
    ("player_actions", "user_id"): "users",
    ("messages", "game_id"): "games",
    ("messages", "turn_id"): "turns",
    ("dm_turn_logs", "game_id"): "games",
    ("dm_turn_logs", "turn_id"): "turns",
}
JSON_COLUMNS = {
    ("games", "adventure_data"),
    ("characters", "data"),
    ("pending_rolls", "values"),
    ("game_states", "data"),
    ("messages", "data"),
    ("dm_turn_logs", "request"),
    ("dm_turn_logs", "trace"),
    ("dm_turn_logs", "usage"),
}
TABLES = set(COLUMNS)
ALEMBIC_TABLE = "alembic_version"


async def reflect(settings: Settings, read: Callable[[Any], Any]) -> Any:
    """Читает схему базы теста инспектором SQLAlchemy: `read(inspector)`."""
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(lambda sync_connection: read(inspect(sync_connection)))
    finally:
        await engine.dispose()


def split(names: str) -> set[str]:
    return set(names.split())


# --- upgrade и downgrade ---


async def test_upgrade_on_an_empty_database_creates_all_tables(settings, migrate):
    await migrate("upgrade", "head")

    tables = set(await reflect(settings, lambda inspector: inspector.get_table_names()))
    assert tables == TABLES | {ALEMBIC_TABLE}


async def test_downgrade_to_base_removes_all_tables(settings, migrate):
    await migrate("upgrade", "head")
    await migrate("downgrade", "base")

    tables = set(await reflect(settings, lambda inspector: inspector.get_table_names()))
    assert tables == {ALEMBIC_TABLE}


async def test_schema_can_be_rebuilt_after_a_downgrade(settings, migrate):
    await migrate("upgrade", "head")
    await migrate("downgrade", "base")
    await migrate("upgrade", "head")

    tables = set(await reflect(settings, lambda inspector: inspector.get_table_names()))
    assert tables == TABLES | {ALEMBIC_TABLE}


async def test_upgrade_creates_the_database_folder(settings, migrate, tmp_path):
    # На свежем клоне папки data/ нет: первая команда из README не должна падать на этом.
    assert not (tmp_path / "data").exists()

    await migrate("upgrade", "head")

    assert (tmp_path / "data" / "test.sqlite3").is_file()


def test_migration_history_is_a_single_line_from_0001():
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))

    assert script.get_bases() == ["0001"]
    assert len(script.get_heads()) == 1


# --- миграция совпадает с моделями и планом ---


async def test_migrations_and_models_describe_the_same_schema(settings, db):
    def differences(inspector: Any) -> list[Any]:
        context = MigrationContext.configure(inspector.bind, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    assert await reflect(settings, differences) == []


async def test_tables_have_the_columns_keys_and_nullability_of_the_plan(settings, db):
    def read(inspector: Any) -> dict[str, dict[str, Any]]:
        return {
            table: {
                "columns": {c["name"]: c for c in inspector.get_columns(table)},
                "pk": inspector.get_pk_constraint(table)["constrained_columns"],
                "unique": [u["column_names"] for u in inspector.get_unique_constraints(table)],
                "fks": {
                    (table, fk["constrained_columns"][0]): fk["referred_table"]
                    for fk in inspector.get_foreign_keys(table)
                },
            }
            for table in inspector.get_table_names()
            if table != ALEMBIC_TABLE
        }

    schema = await reflect(settings, read)

    assert {table: set(info["columns"]) for table, info in schema.items()} == {
        table: split(names) for table, names in COLUMNS.items()
    }
    assert {table: set(info["pk"]) for table, info in schema.items()} == {
        table: split(names) for table, names in PRIMARY_KEYS.items()
    }
    nullable = {
        table: {name for name, column in info["columns"].items() if column["nullable"]}
        for table, info in schema.items()
    }
    for table in COLUMNS:
        assert nullable[table] == split(NULLABLE.get(table, "")), table
    assert {
        (table, name)
        for table, info in schema.items()
        for name, column in info["columns"].items()
        if isinstance(column["type"], JSON)
    } == JSON_COLUMNS
    assert {(t, tuple(cols)) for t, i in schema.items() for cols in i["unique"]} == {
        (table, (column,)) for table, columns in UNIQUE.items() for column in columns
    }
    assert {fk: target for info in schema.values() for fk, target in info["fks"].items()} == (
        FOREIGN_KEYS
    )


# --- команда из терминала ---


@pytest.mark.parametrize(
    ("launch_directory", "config_path"),
    [("", "services/server/alembic.ini"), ("services/server", "alembic.ini")],
    ids=["из корня репозитория", "из services/server"],
)
def test_alembic_command_line_works_from_the_repo_root_and_the_service_folder(
    monkeypatch, settings, tmp_path, launch_directory, config_path
):
    # `.env` владельца в корне репозитория тест не читает; адрес базы приходит из окружения.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    monkeypatch.chdir(REPO_ROOT / launch_directory)

    alembic_main(argv=["-c", config_path, "upgrade", "head"])

    with closing(sqlite3.connect(tmp_path / "data" / "test.sqlite3")) as connection:
        tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
        [(revision,)] = connection.execute("SELECT version_num FROM alembic_version")
    assert TABLES <= tables
    assert revision == "0001"
