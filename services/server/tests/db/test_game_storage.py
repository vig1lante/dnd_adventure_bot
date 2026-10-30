"""Хранение игры: всё, что игра кладёт в базу, читается обратно и становится объектами движка.

Проверяется на базе после `upgrade head`: копия приключения (решение владельца «Копия в базе»),
персонажи, снимки `GameState` с версией схемы, остальные таблицы хода и их ограничения.
"""

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character
from dnd_engine.content import load_srd
from dnd_engine.creation import quick_character
from dnd_engine.state import GameState, RulesRef
from dnd_server.db.models import (
    AUDIENCE_ALL,
    GAME_STATE_SCHEMA_VERSION,
    ActionStatus,
    DmTurnLog,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    Turn,
    TurnKind,
    TurnPhase,
    TurnStatus,
    UnsupportedSnapshotVersion,
    User,
)

# tests/db/test_game_storage.py -> db -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


@pytest.fixture(scope="module")
def hero() -> Character:
    return quick_character(
        "fighter", "human", load_srd(), name="Борис", concept="Бывший стражник", character_id="pc-1"
    )


def make_state(adventure: Adventure, hero: Character, version: int = 1) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id=adventure.id,
        rules=RulesRef(modules=["campfire"]),
        party={hero.id: hero},
        location_id=adventure.start_location,
        turn=version,
        version=version,
    )


def make_game(adventure: Adventure, **overrides) -> Game:
    fields = {
        "id": "game-1",
        "code": "ABC123",
        "adventure_id": adventure.id,
        "host_user_id": "host",
        "adventure_data": adventure.model_dump(mode="json"),
        "collect_seconds": 120,
        "hurry_seconds": 60,
        "camp_seconds": 300,
    }
    return Game(**(fields | overrides))


async def save_host_and_game(db, adventure: Adventure, **game_overrides) -> None:
    async with db() as session:
        session.add(User(id="host", tg_id=1001, name="Хост"))
        await session.flush()
        session.add(make_game(adventure, **game_overrides))
        await session.commit()


# --- копия приключения ---


async def test_adventure_copy_is_saved_and_restores_the_engine_adventure(db, adventure):
    await save_host_and_game(db, adventure)

    async with db() as session:
        game = await session.get(Game, "game-1")
        restored = Adventure.model_validate(game.adventure_data)

    assert restored == adventure


# --- снимки состояния и версия их схемы ---


async def test_state_snapshot_is_saved_and_restores_the_engine_state(db, adventure, hero):
    state = make_state(adventure, hero)
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(GameStateSnapshot.from_state(state))
        await session.commit()

    async with db() as session:
        snapshot = await session.get(GameStateSnapshot, ("game-1", 1))

    assert snapshot.version == 1
    assert snapshot.schema_version == GAME_STATE_SCHEMA_VERSION
    assert snapshot.to_state() == state


async def test_every_version_of_the_state_is_kept(db, adventure, hero):
    # Нужно для отката «Перегенерировать»: прежняя версия не затирается новой.
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add_all(
            GameStateSnapshot.from_state(make_state(adventure, hero, version))
            for version in (1, 2, 3)
        )
        await session.commit()

    async with db() as session:
        versions = (await session.scalars(select(GameStateSnapshot.version))).all()

    assert sorted(versions) == [1, 2, 3]


async def test_second_commit_of_the_same_state_version_is_refused(db, adventure, hero):
    # Защита от двойного коммита хода: версия состояния уникальна в пределах игры.
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(GameStateSnapshot.from_state(make_state(adventure, hero)))
        await session.commit()

    async with db() as session:
        session.add(GameStateSnapshot.from_state(make_state(adventure, hero)))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_snapshot_from_a_newer_server_is_refused_with_a_clear_error(db, adventure, hero):
    # Откат кода не должен молча терять поля, которых он не знает.
    state = make_state(adventure, hero)
    await save_host_and_game(db, adventure)
    async with db() as session:
        snapshot = GameStateSnapshot.from_state(state)
        snapshot.schema_version = GAME_STATE_SCHEMA_VERSION + 1
        session.add(snapshot)
        await session.commit()

    async with db() as session:
        snapshot = await session.get(GameStateSnapshot, ("game-1", 1))
        with pytest.raises(UnsupportedSnapshotVersion) as refusal:
            snapshot.to_state()

    message = str(refusal.value)
    assert "game-1" in message
    assert str(GAME_STATE_SCHEMA_VERSION + 1) in message
    assert "обновите сервер" in message


def test_snapshot_of_an_older_schema_is_read(adventure, hero):
    state = make_state(adventure, hero)
    snapshot = GameStateSnapshot.from_state(state)
    snapshot.schema_version = 0

    assert snapshot.to_state() == state


# --- персонажи, игроки и остальные таблицы ---


async def test_character_is_saved_as_json_and_restores_the_engine_character(db, adventure, hero):
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(
            PlayerCharacter(
                id=hero.id, game_id="game-1", user_id="host", data=hero.model_dump(mode="json")
            )
        )
        await session.commit()

    async with db() as session:
        saved = await session.get(PlayerCharacter, hero.id)

    assert Character.model_validate(saved.data) == hero


async def test_the_choice_of_a_character_is_json_and_its_absence_is_sql_null(db, adventure, hero):
    await save_host_and_game(db, adventure)
    choice = {"mode": "quick", "choices": {"class_id": "rogue", "name": None}}
    other = hero.model_copy(update={"id": "pc-other"})
    async with db() as session:
        session.add_all(
            [
                PlayerCharacter(
                    id=hero.id,
                    game_id="game-1",
                    user_id="host",
                    data=hero.model_dump(mode="json"),
                    choices=choice,
                ),
                PlayerCharacter(
                    id=other.id,
                    game_id="game-1",
                    user_id="host",
                    data=other.model_dump(mode="json"),
                ),
            ]
        )
        await session.commit()

    async with db() as session:
        with_choice = await session.get(PlayerCharacter, hero.id)
        without = await session.get(PlayerCharacter, other.id)
        # Пустой выбор — настоящий NULL, а не JSON `null`: так же выглядят герои до миграции 0003.
        empty = await session.scalar(select(func.count()).where(PlayerCharacter.choices.is_(None)))

    assert with_choice.choices == choice
    assert without.choices is None
    assert empty == 1


async def test_whole_game_is_read_back_after_upgrade(db, adventure, hero):
    state = make_state(adventure, hero)
    deadline = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)
    await save_host_and_game(db, adventure, deadline_at=deadline, provider="fake")
    async with db() as session:
        session.add(User(id="guest", dev_login="dev-2", name="Гость"))
        await session.flush()
        session.add(
            PlayerCharacter(
                id=hero.id,
                game_id="game-1",
                user_id="guest",
                data=hero.model_dump(mode="json"),
                choices={"mode": "random", "choices": None},
            )
        )
        await session.flush()
        session.add_all(
            [
                GamePlayer(
                    game_id="game-1", user_id="host", is_host=True, ready=True, joined_order=0
                ),
                GamePlayer(game_id="game-1", user_id="guest", character_id=hero.id, joined_order=1),
                GameStateSnapshot.from_state(state),
                Turn(
                    id="turn-1",
                    game_id="game-1",
                    number=1,
                    kind=TurnKind.ROUND,
                    status=TurnStatus.DONE,
                    state_version_before=0,
                    state_version_after=1,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                PlayerAction(
                    turn_id="turn-1",
                    user_id="guest",
                    text="Осматриваю дверь",
                    secret=True,
                    status=ActionStatus.SUBMITTED,
                ),
                Message(
                    game_id="game-1",
                    turn_id="turn-1",
                    audience="guest",
                    kind=MessageKind.PRIVATE,
                    text="Ты слышишь шорох.",
                    data={"roll": 14},
                ),
                DmTurnLog(
                    game_id="game-1",
                    turn_id="turn-1",
                    attempt=1,
                    provider="fake",
                    request={"context": "Отряд у двери склепа."},
                    trace=[{"tool": "request_check", "args": {"dc": 15}}],
                    output="Дверь скрипнула.",
                    usage={"input_tokens": 10, "output_tokens": 5},
                    duration_ms=120,
                ),
            ]
        )
        await session.commit()

    async with db() as session:
        game = await session.get(Game, "game-1")
        players = (
            await session.scalars(select(GamePlayer).order_by(GamePlayer.joined_order))
        ).all()
        saved_hero = await session.get(PlayerCharacter, hero.id)
        turn = await session.get(Turn, "turn-1")
        action = await session.get(PlayerAction, ("turn-1", "guest"))
        message = await session.scalar(select(Message))
        log = await session.scalar(select(DmTurnLog))

    assert (game.code, game.host_user_id, game.provider, game.model) == (
        "ABC123",
        "host",
        "fake",
        None,
    )
    assert game.deadline_at == deadline
    assert [(p.user_id, p.is_host, p.ready, p.character_id) for p in players] == [
        ("host", True, True, None),
        ("guest", False, False, hero.id),
    ]
    assert saved_hero.choices == {"mode": "random", "choices": None}
    assert (turn.kind, turn.status, turn.state_version_after) == (
        TurnKind.ROUND,
        TurnStatus.DONE,
        1,
    )
    assert (action.text, action.secret, action.inspiration, action.status) == (
        "Осматриваю дверь",
        True,
        False,
        ActionStatus.SUBMITTED,
    )
    assert (message.audience, message.kind, message.data, message.superseded) == (
        "guest",
        MessageKind.PRIVATE,
        {"roll": 14},
        False,
    )
    assert log.trace == [{"tool": "request_check", "args": {"dc": 15}}]
    assert (log.attempt, log.output, log.usage["input_tokens"], log.error) == (
        1,
        "Дверь скрипнула.",
        10,
        None,
    )


# --- значения по умолчанию ---


async def test_new_rows_get_the_defaults_of_the_plan(db, adventure):
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(GamePlayer(game_id="game-1", user_id="host", joined_order=0))
        session.add(Message(game_id="game-1", audience=AUDIENCE_ALL, kind=MessageKind.SYSTEM))
        await session.commit()

    async with db() as session:
        game = await session.get(Game, "game-1")
        player = await session.get(GamePlayer, ("game-1", "host"))
        message = await session.scalar(select(Message))
        host = await session.get(User, "host")

    assert (game.phase, game.turn_phase, game.state_version, game.active_seconds) == (
        GamePhase.LOBBY,
        None,
        0,
        0,
    )
    assert (game.deadline_at, game.started_at, game.current_turn_id) == (None, None, None)
    assert (player.is_host, player.ready) == (False, False)
    assert (message.text, message.superseded, message.data) == ("", False, None)
    assert all(row.created_at.utcoffset() == timedelta(0) for row in (game, host, message))


async def test_string_ids_are_unique_uuids_by_default(db):
    async with db() as session:
        first, second = User(name="А"), User(name="Б")
        session.add_all([first, second])
        await session.commit()

    assert first.id != second.id
    assert len(first.id) == 36


# --- ограничения ---


async def test_game_code_is_unique(db, adventure):
    await save_host_and_game(db, adventure)

    async with db() as session:
        session.add(make_game(adventure, id="game-2"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_telegram_id_and_dev_login_are_unique_but_may_be_missing(db):
    # У dev-пользователя нет tg_id, у telegram-пользователя нет dev_login: пустые значения не равны.
    async with db() as session:
        session.add_all([User(tg_id=1001, name="Telegram"), User(dev_login="dev-1", name="Dev")])
        await session.commit()

    for duplicate in (User(tg_id=1001, name="Двойник"), User(dev_login="dev-1", name="Двойник")):
        async with db() as session:
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()


@pytest.mark.parametrize(
    ("table", "column"), [("games", "phase"), ("games", "turn_phase"), ("turns", "status")]
)
async def test_state_machine_columns_reject_unknown_values_in_the_database_itself(
    db, adventure, table, column
):
    # Ограничение живёт в миграции, а не только в моделях: мимо ORM значение тоже не пройдёт.
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(
            Turn(
                id="turn-1", game_id="game-1", number=1, kind=TurnKind.ROUND, status=TurnStatus.OPEN
            )
        )
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(text(f"UPDATE {table} SET {column} = 'bogus'"))


def test_phase_values_are_the_ones_of_the_plan():
    assert [p.value for p in GamePhase] == ["lobby", "weaving", "playing", "paused", "finished"]
    assert [p.value for p in TurnPhase] == [
        "discussion",
        "collecting",
        "resolving",
        "dm_failed",
        "combat_wait",
        "camp",
    ]
    assert [k.value for k in TurnKind] == ["opening", "round", "combat", "camp_end"]
    assert [s.value for s in TurnStatus] == [
        "open",
        "collecting",
        "resolving",
        "done",
        "failed",
        "superseded",
    ]
    assert [s.value for s in ActionStatus] == [
        "draft",
        "submitted",
        "pass",
        "auto_pass",
        "auto_submitted",
    ]
    assert [k.value for k in MessageKind] == [
        "narration",
        "private",
        "roll",
        "system",
        "camp_line",
        "action",
    ]


# --- JSON и время ---


async def test_missing_json_is_sql_null_and_required_json_refuses_none(db, adventure):
    await save_host_and_game(db, adventure)
    async with db() as session:
        session.add(Message(game_id="game-1", audience=AUDIENCE_ALL, kind=MessageKind.SYSTEM))
        await session.commit()
        stored = (await session.execute(text("SELECT data IS NULL FROM messages"))).scalar_one()
        assert stored == 1  # None не превращается в JSON-значение null

    async with db() as session:
        session.add(PlayerCharacter(id="pc-x", game_id="game-1", user_id="host", data=None))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_times_come_back_as_utc_whatever_zone_they_were_saved_in(db, adventure):
    moscow = timezone(timedelta(hours=3))
    deadline = datetime(2026, 10, 19, 15, 0, tzinfo=moscow)
    await save_host_and_game(db, adventure, deadline_at=deadline)

    async with db() as session:
        game = await session.get(Game, "game-1")

    assert game.deadline_at == deadline
    assert game.deadline_at.utcoffset() == timedelta(0)
    assert game.deadline_at.hour == 12


async def test_time_without_a_zone_is_refused(db, adventure):
    # Наивное время при чтении нельзя сравнить с «сейчас»: отказываем при записи.
    async with db() as session:
        session.add(User(id="host", tg_id=1001, name="Хост"))
        await session.flush()
        session.add(make_game(adventure, deadline_at=datetime(2026, 10, 19, 12, 0)))
        with pytest.raises(StatementError, match="часовым поясом"):
            await session.commit()


# --- фаза хода и момент её начала ---

SERVER_SRC = Path(__file__).resolve().parents[2] / "src" / "dnd_server"


def turn_phase_assignments(source: str) -> list[int]:
    """Строки, где коду присваивают атрибут `turn_phase` напрямую (`x.turn_phase = …`)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AugAssign | ast.AnnAssign)
            else []
        )
        lines += [
            node.lineno
            for target in targets
            if isinstance(target, ast.Attribute) and target.attr == "turn_phase"
        ]
    return lines


def test_the_search_for_direct_assignments_sees_every_form_of_them():
    sample = "game.turn_phase = None\nx.turn_phase: int = 1\ngame.set_turn_phase(None, now)\n"

    assert turn_phase_assignments(sample) == [1, 2]


def test_the_turn_phase_is_changed_only_together_with_the_moment_it_started():
    # Каждая смена фазы хода пишет `phase_since` той же транзакцией (`Game.set_turn_phase`):
    # прямое присваивание `turn_phase` в коде сервера пропустило бы его.
    found = {
        str(path.relative_to(SERVER_SRC)): lines
        for path in SERVER_SRC.rglob("*.py")
        if (lines := turn_phase_assignments(path.read_text(encoding="utf-8")))
    }

    assert set(found) == {"db/models.py"}  # только внутри самого `set_turn_phase`
    assert len(found["db/models.py"]) == 1


async def test_a_new_turn_phase_records_its_start_and_the_same_one_keeps_it(db, adventure):
    await save_host_and_game(db, adventure)
    first = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    async with db() as session:
        game = await session.get(Game, "game-1")
        assert (game.turn_phase, game.phase_since) == (None, None)  # лобби: фазы хода нет
        game.set_turn_phase(TurnPhase.DISCUSSION, first)
        game.set_turn_phase(TurnPhase.DISCUSSION, first + timedelta(minutes=1))
        await session.commit()
    async with db() as session:
        game = await session.get(Game, "game-1")
        stored = (game.turn_phase, game.phase_since)
        game.set_turn_phase(TurnPhase.COLLECTING, first + timedelta(minutes=2))
        await session.commit()
    async with db() as session:
        game = await session.get(Game, "game-1")

    assert stored == (TurnPhase.DISCUSSION, first)
    assert (game.turn_phase, game.phase_since) == (
        TurnPhase.COLLECTING,
        first + timedelta(minutes=2),
    )


def bypassing_phase_writes(source: str) -> list[int]:
    """Строки, где фазу хода пишут в обход `set_turn_phase` иначе, чем `x.turn_phase = …`:
    распаковкой (`game.turn_phase, x = …`), через `setattr(…, "turn_phase", …)`, SQL-обновлением
    (`.values(turn_phase=…)`, в том числе `**{"turn_phase": …}`) или конструктором `Game(…)`."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple | ast.List):
                    lines += [
                        node.lineno
                        for part in ast.walk(target)
                        if isinstance(part, ast.Attribute) and part.attr == "turn_phase"
                    ]
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if name == "setattr" and any(
            isinstance(arg, ast.Constant) and arg.value == "turn_phase" for arg in node.args
        ):
            lines.append(node.lineno)
        if name in ("values", "Game"):
            named = {keyword.arg for keyword in node.keywords}
            unpacked = {
                key.value
                for keyword in node.keywords
                if keyword.arg is None and isinstance(keyword.value, ast.Dict)
                for key in keyword.value.keys
                if isinstance(key, ast.Constant)
            }
            if "turn_phase" in named | unpacked:
                lines.append(node.lineno)
    return lines


def test_the_search_for_bypassing_writes_sees_unpacking_setattr_sql_and_the_constructor():
    sample = (
        "game.turn_phase, game.deadline_at = phase, None\n"
        'setattr(game, "turn_phase", phase)\n'
        "update(Game).values(turn_phase=phase)\n"
        'update(Game).values(**{"turn_phase": phase})\n'
        "Game(id=game_id, turn_phase=phase)\n"
        "GameOut(turn_phase=game.turn_phase)\n"  # ответ API читает фазу, а не пишет её
        "game.set_turn_phase(phase, now)\n"
    )

    assert bypassing_phase_writes(sample) == [1, 2, 3, 4, 5]


def test_no_server_code_writes_the_turn_phase_around_the_moment_it_started():
    # `phase_since` пишется только вместе с фазой. Прямое присваивание ловит тест выше, а этот —
    # остальные пути в обход `Game.set_turn_phase`.
    found = {
        str(path.relative_to(SERVER_SRC)): lines
        for path in SERVER_SRC.rglob("*.py")
        if (lines := bypassing_phase_writes(path.read_text(encoding="utf-8")))
    }

    assert found == {}
