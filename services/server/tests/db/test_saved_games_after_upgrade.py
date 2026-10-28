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
   Образец текущей версии нынешний код читает и пишет без изменений (точный круг «прочитать и
   записать»). Старый образец читается без потерь: ни одно его поле не потеряно и не изменено
   (`lost_fields`, как у копий приключений), а поля, которых в старой схеме не было, приходят со
   значениями по умолчанию — ровно те, что перечислены в `ADDED_FIELDS` для его версии. Так
   версия 2 (DND-2, блок B8) добавила дневник отряда `journal` и номер раунда игроков
   `party_round`: снимок версии 1 читается с пустым дневником и раундом 0, а стартовый дневник
   такой игре открывает сервер (снимок игрока — в памяти, ход мастера — со своим коммитом).
3. Копия приключения хранится по поколениям формата: `golden/adventure_v<N>.json` — дамп
   `model_dump(mode="json")` приключения `demo-crypt` тем кодом, который писал копии формата N.
   `adventure_v1.json` — копия DND-1 (без `format_version`), `adventure_v2.json` — формат v2
   (DND-2, решение Р8). Образец добавляет тот, кто меняет схему `Adventure` несовместимо; прежние
   образцы остаются и читаются всегда — через `adventure_from_data` движка, единственный вход для
   копий игр. Копия текущего формата читается и пишется без изменений, копия старого переводится
   в текущий без потерь. Падение теста чтения образца — сигнал, что сохранённые игры перестали
   читаться: схему делают совместимой (значение по умолчанию, перевод старого формата), а не
   переписывают образец.
"""

import ast
import copy
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from dnd_engine.adventure.loader import adventure_from_data, load_adventure
from dnd_engine.adventure.schema import FORMAT_VERSION
from dnd_engine.character import Character
from dnd_engine.content import load_srd
from dnd_engine.journal import open_journal
from dnd_engine.rng import SeededRng
from dnd_engine.state import GameState
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.db.models import (
    GAME_STATE_SCHEMA_VERSION,
    AdventureDraft,
    DmTurnLog,
    Game,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerCharacter,
    TurnKind,
    TurnStatus,
    User,
)
from dnd_server.db.session import make_engine, make_sessionmaker, resolve_database_url
from dnd_server.dm.turn import Deps, run_dm_turn
from dnd_server.games.hooks import NullBroadcaster
from dnd_server.games.service import GameLocks
from dnd_server.realtime.views import snapshot_for

TESTS = Path(__file__).resolve().parent
SERVER_SRC = TESTS.parents[1] / "src" / "dnd_server"
DEMO_ADVENTURE_DIR = TESTS.parents[3] / "content" / "adventures" / "demo-crypt"
ALEMBIC_INI = TESTS.parents[1] / "alembic.ini"
GOLDEN = TESTS / "golden"
GOLDEN_NAME = re.compile(r"game_state_v(\d+)\.json")
GOLDEN_ADVENTURE_NAME = re.compile(r"adventure_v(\d+)\.json")
NOW = "2026-10-01 12:00:00.000000"
# Ревизия миграции -> тест этого файла, который читает игру, сохранённую до неё.
SAVED_GAME_TESTS = {
    "0001": "test_a_game_saved_on_the_first_revision_is_read_after_upgrade_head",
    "0002": "test_a_game_saved_before_the_clock_column_keeps_counting_after_upgrade_head",
    "0003": "test_a_game_saved_before_hero_choices_keeps_its_heroes_after_upgrade_head",
    "0004": "test_a_game_saved_before_the_profanity_flag_allows_profanity_after_upgrade_head",
    "0005": "test_a_game_saved_before_the_authoring_tables_is_read_after_upgrade_head",
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
    path: str, golden: dict[str, Any], adventure: dict[str, Any], *, pending_roll: bool = True
) -> dict[str, str]:
    """Записывает игру в базу прямым SQL, как это сделал бы сервер прежней версии.

    Схема здесь — таблицы ревизии 0001 по их столбцам, а не нынешние модели: проверка в том, что
    нынешний код читает то, что записал не он. Копия приключения — образец `adventure`, а не
    заглушка: её читает ход мастера. Набор характеристик в `pending_rolls` пишется, пока таблица
    есть (до ревизии 0003; `pending_roll=False` — на 0003 и позже). Возвращает id игры и игроков."""
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
        if pending_roll:
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


# Версия схемы старого снимка -> поля, которые нынешний код добавляет к нему, и их значения по
# умолчанию (как их пишет снимок). Новая версия схемы дописывает сюда свои поля.
ADDED_FIELDS: dict[int, dict[str, Any]] = {1: {"journal": [], "party_round": 0}}


def assert_read_whole(golden: dict[str, Any], state: GameState) -> None:
    """Снимок образца прочитан нынешним кодом целиком (правило 2 в начале файла).

    Образец текущей версии схемы — точный круг «прочитать и записать»: ни одно поле не потеряно
    (pydantic молча отбрасывает поля, которых в модели больше нет) и не добавлено новое. Старый
    образец — без потерь: ни одно его поле не потеряно и не изменено, а добавлены ровно новые поля
    схемы со значениями по умолчанию (`ADDED_FIELDS`)."""
    dumped = state.model_dump(mode="json")
    if golden["schema_version"] == GAME_STATE_SCHEMA_VERSION:
        assert dumped == golden["state"]
        return
    assert lost_fields(golden["state"], dumped) == []
    added = {key: value for key, value in dumped.items() if key not in golden["state"]}
    assert added == ADDED_FIELDS[golden["schema_version"]]


@pytest.mark.parametrize("version", golden_files())
def test_a_snapshot_of_each_schema_version_is_read_whole_by_the_current_code(version):
    golden = json.loads(golden_files()[version].read_text(encoding="utf-8"))

    state = GameState.model_validate(golden["state"])
    hero = Character.model_validate(golden["character"])

    assert golden["schema_version"] == version
    assert_read_whole(golden, state)
    assert hero.model_dump(mode="json") == golden["character"]
    assert state.version == 3 and len(state.party) == 3 and state.combat is not None


def test_the_sample_of_the_current_schema_has_every_field_the_server_writes_now():
    # Образец текущей версии записан нынешним кодом: в нём все поля снимка; дневник отряда
    # (версия 2) не пуст — в нём общие записи и личная запись одного героя, а игра идёт не в
    # первом раунде игроков, и записи этого раунда несут его номер.
    golden = json.loads(golden_files()[GAME_STATE_SCHEMA_VERSION].read_text(encoding="utf-8"))
    state = GameState.model_validate(golden["state"])

    assert set(golden["state"]) == set(state.model_dump(mode="json"))
    audiences = {entry.audience for entry in state.journal}
    assert "all" in audiences and audiences - {"all"} <= set(state.party)
    assert len(audiences) >= 2
    assert state.party_round >= 2
    assert {entry.turn for entry in state.journal} == {0, state.party_round}


def test_a_snapshot_of_the_first_schema_gets_exactly_the_new_fields_with_their_defaults():
    # R8: к снимку версии 1 нынешний код добавляет только дневник (пустой) и раунд игроков (0).
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))

    dumped = GameState.model_validate(golden["state"]).model_dump(mode="json")

    assert set(dumped) - set(golden["state"]) == {"journal", "party_round"}
    assert (dumped["journal"], dumped["party_round"]) == ([], 0)
    assert ADDED_FIELDS[1] == {"journal": [], "party_round": 0}


def test_every_stored_adventure_copy_format_has_its_golden_file_and_old_ones_stay():
    # Образцы v1..vN без пропусков: прежний образец не удаляют, когда добавляют следующий.
    assert golden_adventures()
    assert list(golden_adventures()) == list(range(1, len(golden_adventures()) + 1))


def golden_adventure(version: int) -> dict[str, Any]:
    return json.loads(golden_adventures()[version].read_text(encoding="utf-8"))


def test_the_newest_adventure_copy_sample_is_the_format_the_server_writes_now():
    # Копию пишет создание игры (`model_dump` приключения из каталога): образец последнего
    # поколения — ровно эта форма, с той же версией формата.
    newest = max(golden_adventures())
    written_now = load_adventure(DEMO_ADVENTURE_DIR).model_dump(mode="json")

    assert newest == FORMAT_VERSION
    assert golden_adventure(newest)["format_version"] == written_now["format_version"]
    assert set(golden_adventure(newest)) == set(written_now)


def test_an_adventure_copy_of_the_current_format_is_read_whole_by_the_current_code():
    golden = golden_adventure(FORMAT_VERSION)

    adventure = adventure_from_data(golden)

    # Круг «прочитать и записать» возвращает те же данные: ни одно поле копии не потеряно
    # (в модели `extra="forbid"`, но переименованное поле с умолчанием исчезло бы молча) и не
    # добавлено новое.
    assert adventure.model_dump(mode="json") == golden


def lost_fields(old: Any, new: Any, path: str = "") -> list[str]:
    """Пути полей старой копии, которых в новой нет или у которых другое значение. Новые поля
    новой копии не в счёт: это значения по умолчанию."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [
            lost
            for key, value in old.items()
            for lost in (
                lost_fields(value, new[key], f"{path}/{key}") if key in new else [f"{path}/{key}"]
            )
        ]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            lost
            for number, (was, now) in enumerate(zip(old, new, strict=True))
            for lost in lost_fields(was, now, f"{path}/{number}")
        ]
    return [] if old == new else [path]


@pytest.mark.parametrize("version", [v for v in golden_adventures() if v < FORMAT_VERSION])
def test_an_adventure_copy_of_an_older_format_is_read_without_losing_anything(version):
    golden = golden_adventure(version)
    old = copy.deepcopy(golden)
    moved = [  # связи v1 лежали в NPC, в v2 — в общем графе (решение Р8)
        (owner, relation)
        for owner, npc in old["npcs"].items()
        for relation in npc.pop("relations", [])
    ]

    adventure = adventure_from_data(golden)
    current = adventure.model_dump(mode="json")

    assert current["format_version"] == FORMAT_VERSION
    assert lost_fields(old, current) == []
    assert moved, "в образце должны быть связи NPC"
    for owner, relation in moved:
        # Связь v1 у владельца говорит, кем ему приходится `npc_id`: «npc_id — kind для owner».
        graph = [
            rel
            for rel in adventure.relations
            if (rel.a, rel.b) == (f"npc:{relation['npc_id']}", f"npc:{owner}")
        ]
        assert len(graph) == 1 and relation["note"] in graph[0].note
    assert golden == golden_adventure(version)  # чтение не меняет данные из базы
    # Переведённая копия пишется в текущем формате и читается так же.
    assert adventure_from_data(current) == adventure


# Одно имя у копии приключения в строке игры: `games.adventure_data`.
COPY_FIELD = "adventure_data"


ENTRY_POINT, ONE_FIELD = "adventure_from_data", "одно поле"


def copy_uses(source: str) -> list[str]:
    """Как код берёт копию приключения игры (`<…>.adventure_data` на чтение), по одному ответу на
    каждое место: `ENTRY_POINT` — копия идёт аргументом `adventure_from_data` (позиционно или по
    имени), `ONE_FIELD` — читается одно её поле (`game.adventure_data["title"]`), иначе — само
    выражение, в котором копия ушла дальше: в модель, в `**`, в переменную, в другую функцию.
    Запись копии (`game.adventure_data = …`, `Game(adventure_data=…)`) — не чтение."""
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    uses = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and node.attr == COPY_FIELD
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        holder = parent[node]
        call = parent[holder] if isinstance(holder, ast.keyword) and holder.arg else holder
        passed = holder is not call or any(arg is node for arg in getattr(call, "args", ()))
        if isinstance(holder, ast.Subscript) and holder.value is node:
            uses.append(ONE_FIELD)
        elif passed and isinstance(call, ast.Call) and ast.unparse(call.func) == ENTRY_POINT:
            uses.append(ENTRY_POINT)
        else:
            uses.append(ast.unparse(holder))
    return uses


@pytest.mark.parametrize(
    ("code", "use"),
    [
        ("adventure_from_data(game.adventure_data)", ENTRY_POINT),
        ("adventure_from_data(data=game.adventure_data)", ENTRY_POINT),
        ("title = game.adventure_data['title']", ONE_FIELD),
        ("Adventure.model_validate(game.adventure_data)", None),
        ("Adventure.model_validate(obj=game.adventure_data)", None),
        ("Adventure(**game.adventure_data)", None),
        ("adventure_from_data(**game.adventure_data)", None),
        ("Adventure.model_validate_json(json.dumps(game.adventure_data))", None),
        ("TypeAdapter(Adventure).validate_python(row.adventure_data)", None),
        ("data = game.adventure_data", None),
    ],
)
def test_the_search_for_readers_of_the_copy_tells_the_entry_point_from_any_other_reading(code, use):
    # Страж ниже ищет не только `Adventure.model_validate(<копия>)`: любая другая форма разбора
    # копии (распаковка, разбор JSON, адаптер типа, переменная) — тоже чтение мимо движка.
    (found,) = copy_uses(code)

    assert found == use if use is not None else found not in (ENTRY_POINT, ONE_FIELD)


@pytest.mark.parametrize(
    "code", ["game.adventure_data = adventure.model_dump()", "Game(adventure_data=dump)"]
)
def test_a_write_of_the_copy_is_not_a_reading(code):
    assert copy_uses(code) == []


def test_the_server_reads_every_adventure_copy_through_the_engine_entry_point():
    # Копия игры бывает любого поколения формата: читает её только `adventure_from_data`
    # движка, а не модель напрямую (B2.2, контракт блока 2); одно поле (название) можно взять.
    uses = [
        (path.name, use)
        for path in sorted(SERVER_SRC.rglob("*.py"))
        for use in copy_uses(path.read_text(encoding="utf-8"))
    ]

    assert ENTRY_POINT in {use for _, use in uses}, "сервер должен где-то читать копию приключения"
    assert [(name, use) for name, use in uses if use not in (ENTRY_POINT, ONE_FIELD)] == []


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
            alice = await session.get(User, ids["Алиса"])
            # Состояние игры, листы персонажей и копия приключения разбираются в модели движка
            # без потерь: по копии мастер продолжит игру. Копия v1 лежит в базе как была и
            # читается так же, как образец (перевод в v2 — при чтении).
            assert_read_whole(golden, snapshot.to_state())
            assert game.adventure_data == adventure
            assert adventure_from_data(game.adventure_data) == adventure_from_data(adventure)
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
            # Героев прежних ревизий выбор игрока не записан: колонка пуста (миграция 0003).
            assert [r.choices for r in rows] == [None, None, None]
            # Снимок для игрока строится из старых строк: отряд, бой, лента только со своим личным.
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
        assert [m["name"] for m in view["party"]] == ["Алиса", "Борис", "Вера"]
        assert view["combat"]["round"] == 2 and view["phase"]["phase"] == "playing"
        # Снимок версии 1 без дневника: игрок видит стартовый, а в базу снимок ничего не пишет.
        assert view["journal"] and "journal" not in snapshot.data
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


async def test_a_game_with_a_v1_adventure_copy_plays_a_master_round_after_upgrade_head(
    settings, migrate, tmp_path
):
    # Игра DND-1: копия приключения формата v1, связи NPC лежат в самих NPC. После обновления
    # мастер разбирает по ней раунд (FakeProvider): копия читается, а связи NPC приходят из графа.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0001")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, golden_adventure(1))
    await migrate("upgrade", "head")
    narration = "Туман редеет, и у ворот кладбища видны свежие следы."
    steps = [
        {"tool": "get_npc", "args": {"npc_id": "marta"}},
        {"tool": "chronicle", "args": {"entry": "Отряд осмотрел кладбище."}},
        {"text": narration},
    ]
    scenario = tmp_path / "round.yaml"
    scenario.write_text(yaml.safe_dump({"turns": [{"steps": steps}]}, allow_unicode=True), "utf-8")
    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    deps = Deps(
        sessionmaker=db,
        locks=GameLocks(),
        broadcaster=NullBroadcaster(),
        settings=settings.model_copy(
            update={
                "fake_llm_scenario": scenario,
                "content_dir": tmp_path / "нет-такой-папки",  # приключение — только из копии
            }
        ),
        turn_tokens=None,
        srd=load_srd(),
        rng=SeededRng(7),
    )
    try:
        outcome = await run_dm_turn(ids["game_id"], mode=TurnKind.ROUND, deps=deps)
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            (log,) = await session.scalars(select(DmTurnLog))
            told = await session.scalars(
                select(Message).where(Message.kind == MessageKind.NARRATION)
            )
            texts = [message.text for message in told]
    finally:
        await engine.dispose()

    assert outcome.status == TurnStatus.DONE, log.error
    assert game.state_version > golden["state"]["version"]
    assert narration in texts
    (npc_call,) = [call for call in log.trace if call["name"] == "get_npc"]
    relations = json.loads(npc_call["result"])["relations"]  # в журнале результат — текст JSON
    assert {(rel["a"], rel["b"]) for rel in relations} == {
        ("npc:fabian", "npc:marta"),  # «Фабиан — должник Марты» из связи v1 у Марты
        ("npc:marta", "npc:fabian"),  # «Марта — кредитор Фабиана» из связи v1 у Фабиана
    }


async def test_an_old_game_without_a_journal_shows_the_starting_one_and_saves_it_with_a_turn(
    settings, migrate, tmp_path
):
    # Игра DND-2, начатая до блока B8: снимок схемы 1 без дневника и копия приключения v2 (с
    # «Главным» и известным со старта). Игрок видит стартовый дневник сразу, а в базе его сохраняет
    # ход мастера (FakeProvider) — тем же коммитом, что и весь ход.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = golden_adventure(FORMAT_VERSION)
    await migrate("upgrade", "0001")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, adventure)
    await migrate("upgrade", "head")
    steps = [{"tool": "chronicle", "args": {"entry": "Отряд отдышался."}}, {"text": "Тихо."}]
    scenario = tmp_path / "round.yaml"
    scenario.write_text(yaml.safe_dump({"turns": [{"steps": steps}]}, allow_unicode=True), "utf-8")
    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    deps = Deps(
        sessionmaker=db,
        locks=GameLocks(),
        broadcaster=NullBroadcaster(),
        settings=settings.model_copy(update={"fake_llm_scenario": scenario}),
        turn_tokens=None,
        srd=load_srd(),
        rng=SeededRng(7),
    )
    starting = open_journal(
        GameState.model_validate(golden["state"]), adventure_from_data(adventure)
    ).journal
    try:
        async with db() as session:
            alice = await session.get(User, ids["Алиса"])
            before = await snapshot_for(session, alice, ids["game_id"], srd=deps.srd)
        outcome = await run_dm_turn(ids["game_id"], mode=TurnKind.ROUND, deps=deps)
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            old = await session.get(GameStateSnapshot, (ids["game_id"], golden["state"]["version"]))
            new = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            alice = await session.get(User, ids["Алиса"])
            after = await snapshot_for(session, alice, ids["game_id"], srd=deps.srd)
    finally:
        await engine.dispose()

    assert outcome.status == TurnStatus.DONE
    assert {"intro", "location:village"} <= {entry.ref for entry in starting}
    assert before["journal"] == [entry.model_dump(mode="json") for entry in starting]
    assert "journal" not in old.data  # снимок игрока ничего не записал
    assert new.schema_version == GAME_STATE_SCHEMA_VERSION == 2
    assert new.to_state().journal == starting
    assert after["journal"] == before["journal"]


async def test_a_dnd1_game_opens_the_same_journal_on_every_snapshot_without_writing_it(
    settings, migrate, tmp_path
):
    # Игра DND-1 (финальное покрытие блока B8): снимок схемы 1 и копия приключения v1. Известного
    # со старта в копии v1 нет, поэтому стартовый дневник — «Главное» из перевода копии в v2 и
    # стартовая локация. Снимки подряд открывают один и тот же дневник в памяти и ничего не пишут
    # в базу, а ход мастера сохраняет ровно его — без дублей от прежних снимков.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = golden_adventure(1)
    await migrate("upgrade", "0001")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, adventure)
    await migrate("upgrade", "head")
    steps = [{"tool": "chronicle", "args": {"entry": "Отряд отдышался."}}, {"text": "Тихо."}]
    scenario = tmp_path / "round.yaml"
    scenario.write_text(yaml.safe_dump({"turns": [{"steps": steps}]}, allow_unicode=True), "utf-8")
    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    deps = Deps(
        sessionmaker=db,
        locks=GameLocks(),
        broadcaster=NullBroadcaster(),
        settings=settings.model_copy(update={"fake_llm_scenario": scenario}),
        turn_tokens=None,
        srd=load_srd(),
        rng=SeededRng(7),
    )
    old_copy = adventure_from_data(adventure)
    starting = open_journal(GameState.model_validate(golden["state"]), old_copy).journal
    expected = [entry.model_dump(mode="json") for entry in starting]

    async def snapshots_of_everyone() -> list[dict[str, Any]]:
        async with db() as session:
            return [
                await snapshot_for(
                    session, await session.get(User, ids[name]), ids["game_id"], srd=deps.srd
                )
                for name in ("Алиса", "Борис", "Вера")
            ]

    async def stored() -> list[tuple[int, bool]]:
        async with db() as session:
            rows = await session.scalars(
                select(GameStateSnapshot)
                .where(GameStateSnapshot.game_id == ids["game_id"])
                .order_by(GameStateSnapshot.version)
            )
            return [(row.version, bool(row.to_state().journal)) for row in rows]

    try:
        views = [*await snapshots_of_everyone(), *await snapshots_of_everyone()]
        rows_after_snapshots = await stored()
        outcome = await run_dm_turn(ids["game_id"], mode=TurnKind.ROUND, deps=deps)
        rows_after_turn = await stored()
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            saved = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            saved_journal = saved.to_state().journal
        after = await snapshots_of_everyone()
    finally:
        await engine.dispose()

    assert [entry.ref for entry in starting] == ["intro", f"location:{old_copy.start_location}"]
    assert {entry.audience for entry in starting} == {"all"} and old_copy.journal_intro
    assert all(view["journal"] == expected for view in views)
    assert rows_after_snapshots == [(golden["state"]["version"], False)]  # снимки не писали
    assert outcome.status == TurnStatus.DONE
    assert rows_after_turn == [(golden["state"]["version"], False), (game.state_version, True)]
    assert saved_journal == starting
    assert all(view["journal"] == expected for view in after)


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
        assert_read_whole(golden, snapshot.to_state())
        assert view["phase"]["phase"] == "playing" and view["phase"]["remaining_seconds"] is None
    finally:
        await engine.dispose()


async def test_a_game_saved_before_hero_choices_keeps_its_heroes_after_upgrade_head(
    settings, migrate
):
    # Ревизия 0003 (B8.s3) убрала `pending_rolls` и добавила `characters.choices`: игра на ревизии
    # 0002 с набором характеристик в брошенной таблице продолжает читаться теми же моделями, а
    # герои отдают лист без выбора — форма «Изменить героя» у них пустая.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = json.loads(golden_adventures()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0002")
    ids = insert_saved_game(sqlite_file(settings.database_url), golden, adventure)
    await migrate("upgrade", "head")

    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    app = create_app(settings)
    try:
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            snapshot = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            rows = list(await session.scalars(select(PlayerCharacter)))
            alice = await session.get(User, ids["Алиса"])
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
        assert_read_whole(golden, snapshot.to_state())
        assert {r.id: r.data for r in rows} == golden["state"]["party"]
        assert [r.choices for r in rows] == [None, None, None]
        assert [m["name"] for m in view["party"]] == ["Алиса", "Борис", "Вера"]
        token = SessionSigner.from_settings(settings).issue(ids["Алиса"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            mine = await http.get(
                f"/api/games/{ids['game_id']}/characters/mine",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert mine.status_code == 200, mine.text
        body = mine.json()
        assert (body["mode"], body["choices"]) == (None, None)
        assert body["character"]["name"] == "Алиса"
    finally:
        await app.state.engine.dispose()
        await engine.dispose()


async def test_a_game_saved_before_the_profanity_flag_allows_profanity_after_upgrade_head(
    settings, migrate
):
    # Ревизия 0004 (DND-2 B1.2, решение Р1) завела обязательный флажок мата: игра на ревизии 0003
    # продолжает читаться теми же моделями, и мат в ней разрешён — так решил владелец.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = json.loads(golden_adventures()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0003")
    ids = insert_saved_game(
        sqlite_file(settings.database_url), golden, adventure, pending_roll=False
    )
    await migrate("upgrade", "head")

    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    app = create_app(settings)
    try:
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            snapshot = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            alice = await session.get(User, ids["Алиса"])
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
        assert game.profanity is True
        assert_read_whole(golden, snapshot.to_state())
        assert view["game"]["profanity"] is True  # снимок игрока несёт флажок
        token = SessionSigner.from_settings(settings).issue(ids["Алиса"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            response = await http.get(
                f"/api/games/{ids['game_id']}", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200, response.text
        assert response.json()["profanity"] is True
    finally:
        await app.state.engine.dispose()
        await engine.dispose()


async def test_a_game_saved_before_the_authoring_tables_is_read_after_upgrade_head(
    settings, migrate
):
    # Ревизия 0005 (DND-2 B4.1, спека §10.2) завела таблицы конструктора и не тронула таблицы игр:
    # игра на ревизии 0004 читается теми же моделями, а черновиков у владельца ещё нет.
    golden = json.loads(golden_files()[1].read_text(encoding="utf-8"))
    adventure = json.loads(golden_adventures()[1].read_text(encoding="utf-8"))
    await migrate("upgrade", "0004")
    ids = insert_saved_game(
        sqlite_file(settings.database_url), golden, adventure, pending_roll=False
    )
    await migrate("upgrade", "head")

    engine = make_engine(settings.database_url)
    db = make_sessionmaker(engine)
    app = create_app(settings)
    try:
        async with db() as session:
            game = await session.get(Game, ids["game_id"])
            snapshot = await session.get(GameStateSnapshot, (ids["game_id"], game.state_version))
            alice = await session.get(User, ids["Алиса"])
            view = await snapshot_for(session, alice, ids["game_id"], srd=load_srd())
            drafts = list(await session.scalars(select(AdventureDraft)))
        assert_read_whole(golden, snapshot.to_state())
        assert adventure_from_data(game.adventure_data) == adventure_from_data(adventure)
        assert (game.phase.value, game.profanity) == ("playing", True)
        assert [m["name"] for m in view["party"]] == ["Алиса", "Борис", "Вера"]
        assert drafts == []
        token = SessionSigner.from_settings(settings).issue(ids["Алиса"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            response = await http.get(
                f"/api/games/{ids['game_id']}", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200, response.text
        assert [p["name"] for p in response.json()["players"]] == ["Алиса", "Борис", "Вера"]
    finally:
        await app.state.engine.dispose()
        await engine.dispose()
