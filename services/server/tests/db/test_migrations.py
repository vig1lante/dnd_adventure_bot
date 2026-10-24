"""Миграции Alembic (план B6.1, B7.4, B8.s3, DND-2 B1.2): схемы создаются, откатываются и
совпадают с моделями.

Ревизия 0001 — начальная схема; 0002 (B7.4) добавляет к играм колонку `clock_at` — отметку часов
игры, из которой выводятся остаток отсчёта на паузе и время игры без пауз; 0003 (B8.s3, решения
владельца R9 и R10) убирает таблицу `pending_rolls` (броска характеристик больше нет) и добавляет
персонажам колонку `choices` — выбор игрока при создании героя; 0004 (DND-2 B1.2, решение Р1)
добавляет играм флажок мата `profanity`: обязательный, у старых игр — «разрешён».

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
from alembic import command, op
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.config import main as alembic_main
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, Boolean, inspect
from sqlalchemy.exc import OperationalError

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
        "started_at active_seconds clock_at profanity created_at"
    ),
    "game_players": "game_id user_id character_id is_host ready joined_order joined_at",
    "characters": "id game_id user_id data choices",
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
    "games": "turn_phase deadline_at current_turn_id provider model started_at clock_at",
    "game_players": "character_id",
    "characters": "choices",
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
    ("characters", "choices"),
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
    assert script.get_heads() == ["0004"]


# --- ревизия 0002: отметка часов игры ---

NOW = "2026-10-01 12:00:00.000000"


def insert_minimal_game(path: Path, *, phase: str, started_at: str | None, number: int) -> str:
    """Игра прямым SQL по таблицам ревизии 0001: ровно те столбцы, что требует схема."""
    game_id = f"game-{number}"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users (id, tg_id, dev_login, name, created_at)"
            " VALUES ('host', NULL, NULL, 'Хост', ?)",
            (NOW,),
        )
        connection.execute(
            "INSERT INTO games (id, code, adventure_id, host_user_id, adventure_data, phase,"
            " turn_phase, deadline_at, state_version, current_turn_id, provider, model,"
            " collect_seconds, hurry_seconds, camp_seconds, started_at, active_seconds, created_at)"
            " VALUES (?, ?, 'demo-crypt', 'host', '{}', ?, NULL, NULL, 0, NULL, NULL, NULL,"
            " 120, 60, 300, ?, 40, ?)",
            (game_id, f"CODE{number:02d}", phase, started_at, NOW),
        )
        connection.commit()
    return game_id


async def test_the_second_revision_only_adds_the_nullable_clock_column_to_games(settings, migrate):
    def columns(inspector: Any) -> dict[str, dict[str, Any]]:
        return {
            table: {c["name"]: c for c in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if table != ALEMBIC_TABLE
        }

    await migrate("upgrade", "0001")
    before = await reflect(settings, columns)
    await migrate("upgrade", "0002")
    after = await reflect(settings, columns)

    assert set(after) == set(before)
    for table in before:
        extra = set(after[table]) - set(before[table])
        assert extra == ({"clock_at"} if table == "games" else set()), table
        assert set(before[table]) <= set(after[table]), table
    assert after["games"]["clock_at"]["nullable"] is True


async def test_the_clock_of_games_in_play_starts_at_their_start_and_others_stay_empty(
    settings, migrate
):
    await migrate("upgrade", "0001")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    playing = insert_minimal_game(path, phase="playing", started_at=NOW, number=1)
    weaving = insert_minimal_game(path, phase="weaving", started_at=NOW, number=2)
    lobby = insert_minimal_game(path, phase="lobby", started_at=None, number=3)
    finished = insert_minimal_game(path, phase="finished", started_at=NOW, number=4)

    await migrate("upgrade", "head")

    with closing(sqlite3.connect(path)) as connection:
        clocks = dict(connection.execute("SELECT id, clock_at FROM games"))
        seconds = dict(connection.execute("SELECT id, active_seconds FROM games"))
    # Идущая игра продолжает считать время с момента старта, остальные часов не заводят.
    assert clocks[playing] == NOW and clocks[weaving] == NOW
    assert clocks[lobby] is None and clocks[finished] is None
    assert set(seconds.values()) == {40}  # накопленное время старой игры не тронуто


async def test_downgrade_to_the_first_revision_drops_the_column_and_keeps_the_games(
    settings, migrate
):
    await migrate("upgrade", "head")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO users (id, tg_id, dev_login, name, created_at)"
            " VALUES ('host', NULL, NULL, 'Хост', ?)",
            (NOW,),
        )
        connection.execute(
            "INSERT INTO games (id, code, adventure_id, host_user_id, adventure_data, phase,"
            " state_version, collect_seconds, hurry_seconds, camp_seconds, active_seconds,"
            " clock_at, created_at) VALUES ('game-1', 'ABC123', 'demo-crypt', 'host', '{}',"
            " 'playing', 3, 90, 45, 200, 600, ?, ?)",
            (NOW, NOW),
        )
        connection.commit()

    await migrate("downgrade", "0001")

    columns = await reflect(
        settings, lambda inspector: {c["name"] for c in inspector.get_columns("games")}
    )
    assert "clock_at" not in columns
    with closing(sqlite3.connect(path)) as connection:
        [row] = connection.execute("SELECT id, phase, state_version, active_seconds FROM games")
        assert row == ("game-1", "playing", 3, 600)
        # Откат не потерял ограничения таблицы: фаза вне списка по-прежнему отвергается.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE games SET phase = 'bogus'")


# --- ревизия 0003: выбор игрока при создании героя, таблицы pending_rolls больше нет ---


def schema_signature(inspector: Any) -> dict[str, Any]:
    """Всё, что видно о схеме инспектору: колонки с типом и обязательностью, ключи, ограничения."""
    return {
        table: {
            "columns": {
                c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)
            },
            "pk": (
                inspector.get_pk_constraint(table)["name"],
                tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            ),
            "fks": sorted(
                (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"])
                for fk in inspector.get_foreign_keys(table)
            ),
            "unique": sorted(
                (u["name"], tuple(u["column_names"]))
                for u in inspector.get_unique_constraints(table)
            ),
            "checks": sorted(
                (c["name"], c["sqltext"]) for c in inspector.get_check_constraints(table)
            ),
        }
        for table in inspector.get_table_names()
        if table != ALEMBIC_TABLE
    }


def insert_lobby_with_heroes(path: Path, *, with_roll: bool) -> None:
    """Лобби с двумя героями прямым SQL по таблицам ревизии 0002; героев выбирал игрок, а у
    таблицы `characters` на этой ревизии колонки `choices` ещё нет."""
    with closing(sqlite3.connect(path)) as connection:
        for user in ("host", "friend"):
            connection.execute(
                "INSERT INTO users (id, tg_id, dev_login, name, created_at)"
                " VALUES (?, NULL, NULL, ?, ?)",
                (user, user, NOW),
            )
        connection.execute(
            "INSERT INTO games (id, code, adventure_id, host_user_id, adventure_data, phase,"
            " state_version, collect_seconds, hurry_seconds, camp_seconds, active_seconds,"
            " created_at) VALUES ('game-1', 'ABC123', 'demo-crypt', 'host', '{}', 'lobby', 0,"
            " 90, 45, 200, 0, ?)",
            (NOW,),
        )
        for number, user in enumerate(("host", "friend"), start=1):
            connection.execute(
                "INSERT INTO characters (id, game_id, user_id, data) VALUES (?, 'game-1', ?, ?)",
                (f"pc-{number}", user, f'{{"id": "pc-{number}", "name": "Герой {number}"}}'),
            )
            connection.execute(
                "INSERT INTO game_players (game_id, user_id, character_id, is_host, ready,"
                " joined_order, joined_at) VALUES ('game-1', ?, ?, ?, 1, ?, ?)",
                (user, f"pc-{number}", number == 1, number, NOW),
            )
        if with_roll:
            connection.execute(
                'INSERT INTO pending_rolls (game_id, user_id, "values") VALUES (?, ?, ?)',
                ("game-1", "host", "[15, 14, 13, 12, 10, 8]"),
            )
        connection.commit()


async def test_the_third_revision_drops_pending_rolls_and_adds_the_nullable_choices_column(
    settings, migrate
):
    def columns(inspector: Any) -> dict[str, dict[str, Any]]:
        return {
            table: {c["name"]: c for c in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if table != ALEMBIC_TABLE
        }

    await migrate("upgrade", "0002")
    before = await reflect(settings, columns)
    await migrate("upgrade", "0003")
    after = await reflect(settings, columns)

    assert set(before) - set(after) == {"pending_rolls"}
    assert set(after) <= set(before)
    for table in after:
        extra = set(after[table]) - set(before[table])
        assert extra == ({"choices"} if table == "characters" else set()), table
        assert set(before[table]) <= set(after[table]), table
    assert after["characters"]["choices"]["nullable"] is True
    assert isinstance(after["characters"]["choices"]["type"], JSON)


async def test_games_and_heroes_saved_on_the_second_revision_survive_the_third(settings, migrate):
    await migrate("upgrade", "0002")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=True)

    await migrate("upgrade", "0003")

    with closing(sqlite3.connect(path)) as connection:
        games = list(connection.execute("SELECT id, code, phase, active_seconds FROM games"))
        heroes = list(connection.execute("SELECT id, user_id, data, choices FROM characters"))
        players = list(
            connection.execute("SELECT user_id, character_id, ready FROM game_players ORDER BY 1")
        )
        tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
    assert games == [("game-1", "ABC123", "lobby", 0)]
    # Герои целы, а выбор у них пуст: у героев прежних ревизий его не записывали.
    assert heroes == [
        ("pc-1", "host", '{"id": "pc-1", "name": "Герой 1"}', None),
        ("pc-2", "friend", '{"id": "pc-2", "name": "Герой 2"}', None),
    ]
    assert players == [("friend", "pc-2", 1), ("host", "pc-1", 1)]
    assert "pending_rolls" not in tables


async def test_downgrade_to_the_second_revision_brings_pending_rolls_back_and_keeps_the_games(
    settings, migrate
):
    await migrate("upgrade", "0002")
    at_second = await reflect(settings, schema_signature)
    await migrate("upgrade", "head")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE characters SET choices = ?", ('{"mode": "random"}',))
        connection.commit()

    await migrate("downgrade", "0002")

    # Схема вернулась к ревизии 0002 целиком: те же таблицы, колонки, ключи и ограничения.
    assert await reflect(settings, schema_signature) == at_second
    with closing(sqlite3.connect(path)) as connection:
        heroes = list(connection.execute("SELECT id, user_id FROM characters ORDER BY 1"))
        assert heroes == [("pc-1", "host"), ("pc-2", "friend")]
        assert connection.execute("SELECT count(*) FROM game_players").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM pending_rolls").fetchone() == (0,)
        # Возвращённая таблица работает как прежняя: ключ и внешние ключи на месте.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            'INSERT INTO pending_rolls (game_id, user_id, "values") VALUES (?, ?, ?)',
            ("game-1", "host", "[15, 14, 13, 12, 10, 8]"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO pending_rolls (game_id, user_id, "values") VALUES (?, ?, ?)',
                ("game-1", "host", "[8, 8, 8, 8, 8, 8]"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO pending_rolls (game_id, user_id, "values") VALUES (?, ?, ?)',
                ("no-such-game", "host", "[1]"),
            )


async def test_the_third_revision_can_be_applied_again_after_a_downgrade(settings, migrate):
    await migrate("upgrade", "head")
    after_first = await reflect(settings, schema_signature)
    await migrate("downgrade", "0002")
    await migrate("upgrade", "head")

    assert await reflect(settings, schema_signature) == after_first


# --- обрыв посреди миграции 0003 (B8.security.server.fix1) ---
#
# SQLite выполняет DROP TABLE и ADD COLUMN сразу и насовсем, а запись о ревизии появляется только
# после обоих шагов. Обрыв между ними (база занята, кончилось место, процесс убит) оставляет базу
# посередине: часть изменений сделана, ревизия прежняя. Повторный запуск обязан довести дело до
# конца, а не падать на шаге, который уже сделан, и сохранённые игры остаются целыми.


def break_operation(monkeypatch, name: str) -> None:
    """Операция Alembic падает так, как падает запись в занятую базу или на полный диск."""

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OperationalError(name, {}, sqlite3.OperationalError("database or disk is full"))

    monkeypatch.setattr(op, name, broken)


def set_recorded_revision(path: Path, revision: str) -> None:
    """Подменяет запись о ревизии: база осталась такой, какой её сделали шаги другой ревизии."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))
        connection.commit()


def recorded_revision(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        [(revision,)] = connection.execute("SELECT version_num FROM alembic_version")
    return revision


def saved_lobby(path: Path) -> list[Any]:
    """Игра, герои и участники лобби, записанные `insert_lobby_with_heroes`."""
    with closing(sqlite3.connect(path)) as connection:
        return [
            list(connection.execute("SELECT id, code, phase FROM games")),
            list(connection.execute("SELECT id, user_id, data FROM characters ORDER BY 1")),
            list(connection.execute("SELECT user_id, character_id FROM game_players ORDER BY 1")),
        ]


async def test_the_third_revision_is_repeated_after_a_failure_on_its_second_step(
    settings, migrate, monkeypatch
):
    await migrate("upgrade", "0002")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=True)
    saved = saved_lobby(path)
    with monkeypatch.context() as broken:
        break_operation(broken, "add_column")
        with pytest.raises(OperationalError):
            await migrate("upgrade", "0003")
    assert recorded_revision(path) == "0002"  # обрыв: ревизия не записана

    await migrate("upgrade", "0003")

    assert recorded_revision(path) == "0003"
    assert saved_lobby(path) == saved
    await migrate("downgrade", "0002")
    await migrate("upgrade", "0003")  # и обычный путь после починки работает


async def test_the_third_revision_is_repeated_when_its_changes_were_made_but_not_recorded(
    settings, migrate
):
    await migrate("upgrade", "0002")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=True)
    saved = saved_lobby(path)
    await migrate("upgrade", "0003")
    done = await reflect(settings, schema_signature)
    set_recorded_revision(path, "0002")  # процесс убит после обоих шагов, до записи ревизии

    await migrate("upgrade", "0003")

    assert recorded_revision(path) == "0003"
    assert await reflect(settings, schema_signature) == done
    assert saved_lobby(path) == saved


async def test_downgrade_of_the_third_revision_is_repeated_after_a_failure_on_its_second_step(
    settings, migrate, monkeypatch
):
    await migrate("upgrade", "0002")
    at_second = await reflect(settings, schema_signature)
    await migrate("upgrade", "0003")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    saved = saved_lobby(path)
    with monkeypatch.context() as broken:
        break_operation(broken, "create_table")
        with pytest.raises(OperationalError):
            await migrate("downgrade", "0002")
    assert recorded_revision(path) == "0003"  # обрыв: откат не записан

    await migrate("downgrade", "0002")

    assert recorded_revision(path) == "0002"
    assert await reflect(settings, schema_signature) == at_second
    assert saved_lobby(path) == saved


async def test_downgrade_of_the_third_revision_is_repeated_when_made_but_not_recorded(
    settings, migrate
):
    await migrate("upgrade", "0002")
    at_second = await reflect(settings, schema_signature)
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    saved = saved_lobby(path)
    set_recorded_revision(path, "0003")  # откат сделан целиком, запись о нём не появилась

    await migrate("downgrade", "0002")

    assert recorded_revision(path) == "0002"
    assert await reflect(settings, schema_signature) == at_second
    assert saved_lobby(path) == saved


def test_the_third_revision_prints_all_its_steps_in_sql_mode_without_a_database(
    monkeypatch, capsys, settings, tmp_path
):
    # В режиме `--sql` базы нет и осмотреть её нельзя: печатаются обычные шаги ревизии подряд.
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    printed = {}
    for name, revisions in (("upgrade", "0002:0003"), ("downgrade", "0003:0002")):
        getattr(command, name)(Config(str(ALEMBIC_INI)), revisions, sql=True)
        printed[name] = capsys.readouterr().out

    assert "DROP TABLE IF EXISTS pending_rolls" in printed["upgrade"]
    assert "ALTER TABLE characters ADD COLUMN choices JSON" in printed["upgrade"]
    assert "ALTER TABLE characters DROP COLUMN choices" in printed["downgrade"]
    assert "CREATE TABLE IF NOT EXISTS pending_rolls" in printed["downgrade"]
    assert not (tmp_path / "data").exists()  # печать ничего не создаёт на диске


# --- ревизия 0004: флажок мата у игры (DND-2 B1.2, решение Р1) ---


def game_columns(inspector: Any) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in inspector.get_columns("games")}


async def test_the_fourth_revision_only_adds_the_required_profanity_flag_to_games(
    settings, migrate
):
    await migrate("upgrade", "0003")
    before = await reflect(settings, schema_signature)
    await migrate("upgrade", "0004")
    after = await reflect(settings, schema_signature)
    games = await reflect(settings, game_columns)

    assert set(after) == set(before)
    for table in before:
        extra = set(after[table]["columns"]) - set(before[table]["columns"])
        assert extra == ({"profanity"} if table == "games" else set()), table
        assert {key: value for key, value in after[table].items() if key != "columns"} == {
            key: value for key, value in before[table].items() if key != "columns"
        }, table
    assert games["profanity"]["nullable"] is False
    assert isinstance(games["profanity"]["type"], Boolean)
    assert games["profanity"]["default"] in ("1", "'1'", "true")


async def test_games_saved_on_the_third_revision_allow_profanity_after_the_fourth(
    settings, migrate
):
    await migrate("upgrade", "0003")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    saved = saved_lobby(path)

    await migrate("upgrade", "0004")

    with closing(sqlite3.connect(path)) as connection:
        assert list(connection.execute("SELECT id, profanity FROM games")) == [("game-1", 1)]
    assert saved_lobby(path) == saved


async def test_a_game_created_after_the_fourth_revision_allows_profanity_by_default(
    settings, migrate
):
    await migrate("upgrade", "head")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)  # строка без колонки: значение по умолчанию
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT profanity FROM games").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE games SET profanity = NULL")


async def test_downgrade_to_the_third_revision_drops_the_flag_and_keeps_the_games(
    settings, migrate
):
    await migrate("upgrade", "0003")
    at_third = await reflect(settings, schema_signature)
    await migrate("upgrade", "head")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE games SET profanity = 0")
        connection.commit()
    saved = saved_lobby(path)

    await migrate("downgrade", "0003")

    assert await reflect(settings, schema_signature) == at_third
    assert saved_lobby(path) == saved


async def test_the_fourth_revision_can_be_applied_again_after_a_downgrade(settings, migrate):
    await migrate("upgrade", "head")
    after_first = await reflect(settings, schema_signature)
    await migrate("downgrade", "0003")
    await migrate("upgrade", "head")

    assert await reflect(settings, schema_signature) == after_first


async def test_the_fourth_revision_is_repeated_when_its_change_was_made_but_not_recorded(
    settings, migrate
):
    # SQLite выполняет ADD COLUMN сразу и насовсем: обрыв до записи ревизии оставляет колонку.
    await migrate("upgrade", "0003")
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    saved = saved_lobby(path)
    await migrate("upgrade", "0004")
    done = await reflect(settings, schema_signature)
    set_recorded_revision(path, "0003")

    await migrate("upgrade", "0004")

    assert recorded_revision(path) == "0004"
    assert await reflect(settings, schema_signature) == done
    assert saved_lobby(path) == saved


async def test_downgrade_of_the_fourth_revision_is_repeated_when_made_but_not_recorded(
    settings, migrate
):
    await migrate("upgrade", "0003")
    at_third = await reflect(settings, schema_signature)
    path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    insert_lobby_with_heroes(path, with_roll=False)
    saved = saved_lobby(path)
    set_recorded_revision(path, "0004")  # откат сделан целиком, запись о нём не появилась

    await migrate("downgrade", "0003")

    assert recorded_revision(path) == "0003"
    assert await reflect(settings, schema_signature) == at_third
    assert saved_lobby(path) == saved


def test_the_fourth_revision_prints_its_steps_in_sql_mode_without_a_database(
    monkeypatch, capsys, settings, tmp_path
):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    printed = {}
    for name, revisions in (("upgrade", "0003:0004"), ("downgrade", "0004:0003")):
        getattr(command, name)(Config(str(ALEMBIC_INI)), revisions, sql=True)
        printed[name] = capsys.readouterr().out

    assert "ALTER TABLE games ADD COLUMN profanity BOOLEAN DEFAULT 1 NOT NULL" in printed["upgrade"]
    assert "ALTER TABLE games DROP COLUMN profanity" in printed["downgrade"]
    assert not (tmp_path / "data").exists()


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
    assert tables >= TABLES
    assert revision == "0004"
