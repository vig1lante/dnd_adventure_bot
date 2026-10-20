"""Сохранённая игра переживает обновление сервера (план B6.1, инвариант «в»).

Игры хранятся в SQLite, и владелец обновляет сервер, не теряя их. Это ломается двумя способами, и
оба проверены на данных, записанных не нынешним кодом:
- миграция Alembic меняет таблицы, и строки прежней ревизии перестают читаться;
- меняется JSON-схема `GameState`, `Character` или `Adventure`, а таблицы остаются теми же, и
  старый снимок или копия приключения молча теряют поля (pydantic по умолчанию отбрасывает
  лишнее) или не разбираются вовсе. Копия приключения в `games.adventure_data` — часть сохранённой
  игры: ход мастера ведётся по ней, а у `Adventure` движка `extra="forbid"`, так что переименование
  или удаление поля, как и новое обязательное, ломает продолжение всех сохранённых игр.

Правило для следующих изменений, которое проверяет этот файл:
1. Каждая новая миграция добавляет сюда свой тест «игра, сохранённая до миграции, читается после
   `upgrade head`» и вносит его в `SAVED_GAME_TESTS`; без записи `test_every_migration_...` падает.
2. Каждая новая версия схемы снимка (`GAME_STATE_SCHEMA_VERSION`) добавляет файл
   `golden/game_state_v<N>.json`, а прежние файлы остаются: старые снимки должны читаться всегда.
   Образец записывается один раз кодом той версии (состояние игры с отрядом, боем и зацепками и
   лист персонажа из него) и потом не перезаписывается: его смысл в том, что он старше кода.
3. Копия приключения: у неё нет версии формата, поэтому образец `golden/adventure_v<N>.json` (дамп
   `model_dump(mode="json")` приключения `demo-crypt`) добавляет тот, кто меняет схему `Adventure`
   несовместимо; прежние образцы остаются и читаются всегда. Падение теста чтения образца — сигнал,
   что сохранённые игры перестали читаться: схему делают совместимой (значение по умолчанию,
   прежнее имя поля), а не переписывают образец.
"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character
from dnd_engine.content import load_srd
from dnd_engine.state import GameState
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.db.models import (
    GAME_STATE_SCHEMA_VERSION,
    Game,
    GamePlayer,
    GameStateSnapshot,
    PendingRoll,
    PlayerCharacter,
    User,
)
from dnd_server.db.session import make_engine, make_sessionmaker, resolve_database_url
from dnd_server.realtime.views import snapshot_for

TESTS = Path(__file__).resolve().parent
ALEMBIC_INI = TESTS.parents[1] / "alembic.ini"
GOLDEN = TESTS / "golden"
GOLDEN_NAME = re.compile(r"game_state_v(\d+)\.json")
GOLDEN_ADVENTURE_NAME = re.compile(r"adventure_v(\d+)\.json")
NOW = "2026-10-01 12:00:00.000000"
# Ревизия миграции -> тест этого файла, который читает игру, сохранённую до неё.
SAVED_GAME_TESTS = {
    "0001": "test_a_game_saved_on_the_first_revision_is_read_after_upgrade_head",
    "0002": "test_a_game_saved_before_the_clock_column_keeps_counting_after_upgrade_head",
}


def golden_files() -> dict[int, Path]:
    found = {
        int(GOLDEN_NAME.fullmatch(f.name).group(1)): f for f in GOLDEN.glob("game_state_v*.json")
    }
    return dict(sorted(found.items()))


def golden_adventures() -> dict[int, Path]:
    found = {
        int(GOLDEN_ADVENTURE_NAME.fullmatch(f.name).group(1)): f
        for f in GOLDEN.glob("adventure_v*.json")
    }
    return dict(sorted(found.items()))


def sqlite_file(database_url: str) -> str:
    return resolve_database_url(database_url).database


def insert_saved_game(
    path: str, golden: dict[str, Any], adventure: dict[str, Any]
) -> dict[str, str]:
    """Записывает игру в базу прямым SQL, как это сделал бы сервер прежней версии.

    Схема здесь — таблицы ревизии 0001 по их столбцам, а не нынешние модели: проверка в том, что
    нынешний код читает то, что записал не он. Копия приключения — образец `adventure`, а не
    заглушка: её читает ход мастера. Возвращает id игры и игроков."""
    state = golden["state"]
    game_id = golden["game_id"]
    heroes = list(state["party"].values())
    players = {hero["player_id"]: hero for hero in heroes}
    with closing(sqlite3.connect(path)) as db:
        for number, (user_id, hero) in enumerate(players.items(), start=1):
            db.execute(
                "INSERT INTO users (id, tg_id, dev_login, name, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, 5000 + number, None, hero["name"], NOW),
            )
        db.execute(
            "INSERT INTO games (id, code, adventure_id, host_user_id, adventure_data, phase,"
            " turn_phase, deadline_at, state_version, current_turn_id, provider, model,"
            " collect_seconds, hurry_seconds, camp_seconds, started_at, active_seconds, created_at)"
            " VALUES (?, 'OLD123', ?, ?, ?, 'playing', 'collecting', ?, ?, 'turn-3',"
            " 'fake', NULL, 90, 45, 200, ?, 600, ?)",
            (
                game_id,
                adventure["id"],
                next(iter(players)),
                json.dumps(adventure),
                NOW,
                state["version"],
                NOW,
                NOW,
            ),
        )
        for hero in heroes:
            db.execute(
                "INSERT INTO characters (id, game_id, user_id, data) VALUES (?, ?, ?, ?)",
                (hero["id"], game_id, hero["player_id"], json.dumps(hero)),
            )
        for order, hero in enumerate(heroes, start=1):
            db.execute(
                "INSERT INTO game_players (game_id, user_id, character_id, is_host, ready,"
                " joined_order, joined_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (game_id, hero["player_id"], hero["id"], order == 1, order, NOW),
            )
        db.execute(
            'INSERT INTO pending_rolls (game_id, user_id, "values") VALUES (?, ?, ?)',
            (game_id, heroes[0]["player_id"], json.dumps([15, 14, 13, 12, 10, 8])),
        )
        db.execute(
            "INSERT INTO game_states (game_id, version, schema_version, data, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (game_id, state["version"], golden["schema_version"], json.dumps(state), NOW),
        )
        db.execute(
            "INSERT INTO turns (id, game_id, number, kind, status, created_at)"
            " VALUES ('turn-3', ?, 3, 'round', 'open', ?)",
            (game_id, NOW),
        )
        for audience, kind, text in (
            ("all", "narration", "Туман стелется по колено."),
            (heroes[0]["player_id"], "private", "ЛИЧНОЕ-алисе"),
            (heroes[1]["player_id"], "private", "ЛИЧНОЕ-борису"),
        ):
            db.execute(
                "INSERT INTO messages (game_id, turn_id, audience, kind, text, data, superseded,"
                " created_at) VALUES (?, 'turn-3', ?, ?, ?, NULL, 0, ?)",
                (game_id, audience, kind, text, NOW),
            )
        db.commit()
    return {"game_id": game_id, **{hero["name"]: uid for uid, hero in players.items()}}


# --- версии схемы снимка ---


def test_every_snapshot_schema_version_has_its_golden_file_and_old_ones_stay():
    # Файлы v1..vN без пропусков: версию схемы не поднимают, не сохранив образец прежней.
    assert list(golden_files()) == list(range(1, GAME_STATE_SCHEMA_VERSION + 1))


@pytest.mark.parametrize("version", golden_files())
def test_a_snapshot_of_each_schema_version_is_read_whole_by_the_current_code(version):
    golden = json.loads(golden_files()[version].read_text(encoding="utf-8"))

    state = GameState.model_validate(golden["state"])
    hero = Character.model_validate(golden["character"])

    assert golden["schema_version"] == version
    # Круг «прочитать и записать» возвращает те же данные: ни одно поле старого снимка не потеряно
    # (pydantic молча отбрасывает поля, которых в модели больше нет) и не добавлено новое.
    assert state.model_dump(mode="json") == golden["state"]
    assert hero.model_dump(mode="json") == golden["character"]
    assert state.version == 3 and len(state.party) == 3 and state.combat is not None


def test_every_stored_adventure_copy_format_has_its_golden_file_and_old_ones_stay():
    # Образцы v1..vN без пропусков: прежний образец не удаляют, когда добавляют следующий.
    assert golden_adventures()
    assert list(golden_adventures()) == list(range(1, len(golden_adventures()) + 1))


@pytest.mark.parametrize("version", golden_adventures())
def test_an_adventure_copy_of_each_stored_format_is_read_whole_by_the_current_code(version):
    golden = json.loads(golden_adventures()[version].read_text(encoding="utf-8"))

    adventure = Adventure.model_validate(golden)

    # Круг «прочитать и записать» возвращает те же данные: ни одно поле старой копии не потеряно
    # (в модели `extra="forbid"`, но переименованное поле с умолчанием исчезло бы молча) и не
    # добавлено новое.
    assert adventure.model_dump(mode="json") == golden


# --- миграции ---


def test_every_migration_has_a_test_that_reads_a_saved_game_after_it():
    revisions = {
        script.revision
        for script in ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).walk_revisions()
    }

    assert revisions == set(SAVED_GAME_TESTS)  # новая миграция требует своего теста и записи
    for name in SAVED_GAME_TESTS.values():
        assert callable(globals().get(name)), name


async def test_a_game_saved_on_the_first_revision_is_read_after_upgrade_head(settings, migrate):
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = json.loads(golden_adventures()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0001")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, adventure)
    await migrate("upgrade", "head")  # все миграции после 0001 проходят по уже сохранённой игре

    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    app = create_app(settings)
    try:
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            snapshot = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            players = list(
                await session.scalars(select(GamePlayer).order_by(GamePlayer.joined_order))
            )
            rows = list(await session.scalars(select(PlayerCharacter)))
            roll = await session.get(PendingRoll, (ids["game_id"], ids["Алиса"]))
            alice = await session.get(User, ids["Алиса"])
            # Состояние игры, листы персонажей и копия приключения разбираются в модели движка
            # без потерь: по копии мастер продолжит игру.
            assert snapshot.to_state().model_dump(mode="json") == golden["state"]
            stored_copy = Adventure.model_validate(game.adventure_data)
            assert stored_copy.model_dump(mode="json") == adventure
            assert {r.id for r in rows} == set(golden["state"]["party"])
            assert [Character.model_validate(r.data).name for r in rows] == [
                "Алиса",
                "Борис",
                "Вера",
            ]
            assert (game.phase.value, game.turn_phase.value) == ("playing", "collecting")
            assert (game.collect_seconds, game.hurry_seconds, game.camp_seconds) == (90, 45, 200)
            assert [(p.is_host, p.ready) for p in players] == [
                (True, True),
                (False, True),
                (False, True),
            ]
            assert roll.values == [15, 14, 13, 12, 10, 8]
            # Снимок для игрока строится из старых строк: отряд, бой, лента только со своим личным.
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
        assert [m["name"] for m in view["party"]] == ["Алиса", "Борис", "Вера"]
        assert view["combat"]["round"] == 2 and view["phase"]["phase"] == "playing"
        assert [item["text"] for item in view["feed"]] == [
            "Туман стелется по колено.",
            "ЛИЧНОЕ-алисе",
        ]
        # И приложение отвечает по старой игре так же, как по новой.
        token = SessionSigner.from_settings(settings).issue(ids["Алиса"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            response = await http.get(
                f"/api/games/{ids['game_id']}", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["adventure_title"] == adventure["title"] and body["phase"] == "playing"
        assert [p["name"] for p in body["players"]] == ["Алиса", "Борис", "Вера"]
        # Тайные заметки мастера из копии игрокам не уходят.
        notes = [place["dm_notes"] for place in adventure["locations"].values()]
        assert any(notes), "в образце должна быть хоть одна заметка мастера"
        assert all(note not in response.text for note in notes if note)
    finally:
        await app.state.engine.dispose()
        await engine.dispose()


async def test_a_game_saved_before_the_clock_column_keeps_counting_after_upgrade_head(
    settings, migrate
):
    # Ревизия 0002 (B7.4) завела `clock_at`: идущая игра прежней ревизии не должна обнулить время
    # игры и должна читаться теми же моделями, что и раньше.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = json.loads(golden_adventures()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0001")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, adventure)
    await migrate("upgrade", "head")

    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    try:
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            snapshot = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            alice = await session.get(User, ids["Алиса"])
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
        started = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        assert (game.started_at, game.clock_at) == (started, started)
        assert (game.active_seconds, game.phase.value) == (600, "playing")
        # Часы идут с момента старта: накопленные 600 с и прошедшие после него. 600 — условный
        # остаток образца: он показывает, что накопленное миграцией не теряется. Но миграция ставит
        # `clock_at = started_at`, и у игры с ненулевым остатком эти секунды посчитались бы дважды.
        # Базы B6 такой игры не содержат: `active_seconds` писал только блок B7 (в B6 он всегда 0).
        assert game.active_time(started + timedelta(seconds=90)) == 690
        assert snapshot.to_state().model_dump(mode="json") == golden["state"]
        assert view["phase"]["phase"] == "playing" and view["phase"]["remaining_seconds"] is None
    finally:
        await engine.dispose()
