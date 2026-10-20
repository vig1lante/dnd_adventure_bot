"""Проекция игры для игрока (план B6.5): снимок, события и адресаты.

Главное, что здесь проверяется, — приватность (инвариант «г»): тайные ходы и личные сообщения видит
только адресат, а зацепки, мир, заметки мастера, журнал хода и точные HP монстров не видит никто.
Тексты-ловушки лежат в игре из `conftest.py`; тесты ищут их в ответах целиком.
"""

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.abilities import Ability
from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveCondition
from dnd_engine.combat.engine import MARK_NAMES_RU
from dnd_engine.content import SrdContent
from dnd_engine.dice import roll as roll_dice
from dnd_engine.events import (
    ActionEvent,
    AttitudeEvent,
    ChronicleEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    FlagEvent,
    HealEvent,
    InspirationEvent,
    ItemEvent,
    MoveEvent,
    PrivateMessageEvent,
    RestEvent,
    RevealEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.rng import SeededRng
from dnd_engine.state import Scene, SceneType
from dnd_server.characters.service import new_character_id
from dnd_server.db.models import (
    ActionStatus,
    Game,
    GamePhase,
    GameStateSnapshot,
    MessageKind,
    PlayerAction,
    TurnPhase,
    UnsupportedSnapshotVersion,
    User,
    new_id,
)
from dnd_server.games.service import NotFound
from dnd_server.realtime.hub import EVENT_TYPES
from dnd_server.realtime.views import (
    FEED_LIMIT,
    FeedEntry,
    audience_to_user,
    character_event,
    combat_event,
    combat_view,
    effect_name,
    feed_entries,
    feed_item_event,
    feed_withdrawn_event,
    party_event,
    phase_event,
    player_status_event,
    snapshot_for,
    state_events,
)

SECRET_MARK = "ТАЙНА-"  # метка из conftest.py: то, что не видит никто
PLAYER_KEYS = ["alice", "boris", "vera"]
# Ключи, из которых не должно состоять ничего, что уходит игроку: это данные мастера.
MASTER_KEYS = {"pc_hooks", "world", "dm_notes", "chronicle", "item_holders", "npc_alive", "flags"}
PARTY_KEYS = {"id", "name", "player_id", "hp", "hp_max", "temp_hp", "conditions", "effects"}
MONSTER_KEYS = {"id", "name", "kind", "side", "zone_id", "health", "conditions"}
PC_FIGHTER_KEYS = {
    "id", "name", "kind", "side", "zone_id", "character_id", "hp", "hp_max", "conditions",
}  # fmt: skip
FEED_ITEM_KEYS = {"id", "kind", "text", "data", "private", "created_at"}


def keys_of(value: Any) -> Iterator[str]:
    """Все ключи словарей на любой глубине."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys_of(inner)


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def snapshot_of(db: async_sessionmaker[AsyncSession], srd: SrdContent, table, key: str):
    async with db() as session:
        user = await session.get(User, table.seats[key].user_id)
        return await snapshot_for(session, user, table.game_id, srd=srd)


# --- игра, фаза и игроки ---


async def test_snapshot_describes_game_phase_scene_host_and_server_time(db, srd, table):
    snapshot = await snapshot_of(db, srd, table, "alice")

    assert snapshot["type"] == "snapshot"
    assert snapshot["game"]["id"] == table.game_id
    assert snapshot["game"]["adventure_title"] == "Склеп должника"
    assert [p["name"] for p in snapshot["game"]["players"]] == ["Алиса", "Борис", "Вера"]
    assert snapshot["host_user_id"] == table.alice.user_id
    assert snapshot["phase"] == {
        "phase": "playing",
        "turn_phase": "collecting",
        # Клиенту нужен явный пояс: без него браузер прочтёт время как местное.
        "deadline_at": "2030-05-01T12:30:00+00:00",
        "remaining_seconds": None,
        "scene": {"type": "combat", "day": 2, "part": "evening"},
    }
    assert datetime.fromisoformat(snapshot["server_time"]).utcoffset().total_seconds() == 0


async def test_statuses_show_every_player_as_writing_ready_or_pass_and_never_the_secret_flag(
    db, srd, table
):
    async with db() as session:
        turn_id = (await session.get(Game, table.game_id)).current_turn_id
        action = await session.get(PlayerAction, (turn_id, table.alice.user_id))
        action.status = ActionStatus.AUTO_PASS
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "vera")

    # Вера не начинала ход, Алиса «выжидает» (автопас), Борис сдал тайный ход.
    assert snapshot["statuses"] == {
        table.alice.user_id: "pass",
        table.boris.user_id: "ready",
        table.vera.user_id: "writing",
    }
    assert "secret" not in dump(snapshot["statuses"])


async def test_my_draft_is_mine_alone(db, srd, table):
    mine = (await snapshot_of(db, srd, table, "boris"))["action"]
    other = (await snapshot_of(db, srd, table, "vera"))["action"]

    assert mine == {
        "text": "ХОД-boris: подкрасться",
        "secret": True,
        "inspiration": True,
        "status": "submitted",
    }
    assert other is None  # у Веры черновика нет, чужой ей не показывают
    assert "подкрасться" not in dump(await snapshot_of(db, srd, table, "vera"))


# --- персонаж, отряд, бой ---


async def test_my_character_is_the_live_sheet_from_the_game_state(db, srd, table):
    snapshot = await snapshot_of(db, srd, table, "alice")

    character = snapshot["character"]
    assert character["id"] == table.alice.character.id
    assert character["hp"] == table.alice.character.hp  # уже с уроном из состояния игры
    assert character["inspiration"] is True
    assert character["persona"]["backstory"] == "ПРЕДЫСТОРИЯ-alice"


async def test_party_shows_names_hp_and_conditions_with_russian_names_from_the_srd(db, srd, table):
    party = {m["id"]: m for m in (await snapshot_of(db, srd, table, "vera"))["party"]}

    assert set(party) == {seat.character.id for seat in table.seats.values()}
    assert all(set(member) == PARTY_KEYS for member in party.values())
    alice = party[table.alice.character.id]
    assert alice["name"] == "Алиса"
    assert alice["hp"] == table.alice.character.hp
    assert alice["hp_max"] == table.alice.character.hp_max
    assert alice["player_id"] == table.alice.user_id
    assert alice["conditions"] == [{"id": "poisoned", "name": "Отравлен", "rounds_left": 3}]
    assert alice["effects"] == [{"id": "bless", "name": "Благословение", "rounds_left": 5}]
    # Названия берутся из данных SRD; названия боевых меток, которых в SRD нет (`helped`), даёт
    # движок (`MARK_NAMES_RU`), а не копия на сервере.
    boris = party[table.boris.character.id]
    assert boris["conditions"] == [{"id": "helped", "name": "Помощь", "rounds_left": 1}]


# --- русские названия состояний и эффектов ---


def test_a_combat_mark_that_is_not_in_the_srd_is_named_by_the_engine_table(srd):
    assert "helped" not in srd.conditions
    assert effect_name(srd, "helped") == "Помощь"


@pytest.mark.parametrize("mark_id", sorted(MARK_NAMES_RU))
def test_every_mark_of_the_engine_table_reaches_the_client_by_its_name(srd, mark_id):
    assert effect_name(srd, mark_id) == MARK_NAMES_RU[mark_id]


@pytest.mark.parametrize(
    ("effect_id", "name"),
    [("poisoned", "Отравлен"), ("bless", "Благословение"), ("rage", "Ярость")],
    ids=["состояние", "заклинание", "умение класса"],
)
def test_srd_names_stay_the_source_for_conditions_spells_and_class_features(srd, effect_id, name):
    assert effect_name(srd, effect_id) == name


def test_an_id_unknown_to_the_srd_and_the_engine_is_returned_as_it_is(srd):
    assert effect_name(srd, "no_such_effect") == "no_such_effect"


async def test_combat_lists_order_current_turn_zones_and_describes_monster_health(db, srd, table):
    combat = (await snapshot_of(db, srd, table, "vera"))["combat"]

    assert combat["round"] == 2
    assert combat["current_id"] == table.boris.character.id
    fighters = {f["id"]: f for f in combat["order"]}
    assert [f["id"] for f in combat["order"]] == [
        "goblin_warrior-1",
        table.boris.character.id,
        "goblin_warrior-2",
        table.alice.character.id,
        "goblin_warrior-3",
        "goblin_warrior-4",
    ]
    assert [fighters[f"goblin_warrior-{n}"]["health"] for n in (1, 2, 3, 4)] == [
        "цел",
        "ранен",
        "тяжело ранен",
        "повержен",
    ]
    assert combat["zones"] == [
        {
            "id": "gate",
            "name": "Ворота",
            "combatants": [table.boris.character.id, table.alice.character.id],
        },
        {
            "id": "yard",
            "name": "Двор",
            "combatants": [
                "goblin_warrior-1",
                "goblin_warrior-2",
                "goblin_warrior-3",
                "goblin_warrior-4",
            ],
        },
    ]


async def test_combat_never_gives_monsters_numbers_and_gives_heroes_their_hp(db, srd, table):
    combat = (await snapshot_of(db, srd, table, "vera"))["combat"]

    for fighter in combat["order"]:
        if fighter["kind"] == "monster":
            assert set(fighter) == MONSTER_KEYS  # ни hp, ни hp_max, ни ac, ни ref
        else:
            assert set(fighter) == PC_FIGHTER_KEYS
    alice = next(f for f in combat["order"] if f["id"] == table.alice.character.id)
    assert alice["hp"] == table.alice.character.hp
    assert alice["character_id"] == table.alice.character.id


# --- скрытые монстры (I7 из security-review B6) ---


def with_hidden(table, fighter_id: str, *, source: str = "hide"):
    """Состояние боя, где боец спрятан: на нём состояние «невидим»."""
    state = table.state.model_copy(deep=True)
    # Состояния героя лежат в персонаже, состояния монстра — в самом бойце.
    holder = state.party.get(fighter_id) or state.combat.combatants[fighter_id]
    holder.conditions.append(ActiveCondition(id="invisible", source=source))
    return state


@pytest.mark.parametrize("source", ["hide", "", "dm"], ids=["спрятался", "без источника", "мастер"])
def test_a_hidden_monster_is_not_in_the_order_or_the_zones_of_the_combat(srd, table, source):
    state = with_hidden(table, "goblin_warrior-2", source=source)

    combat = combat_view(state, srd)

    assert "goblin_warrior-2" not in [fighter["id"] for fighter in combat["order"]]
    assert all("goblin_warrior-2" not in zone["combatants"] for zone in combat["zones"])
    assert [f["id"] for f in combat["order"]] == [
        "goblin_warrior-1",
        table.boris.character.id,
        table.alice.character.id,
        "goblin_warrior-3",
        "goblin_warrior-4",
    ]


def test_nothing_about_a_hidden_monster_reaches_the_client(srd, table):
    state = with_hidden(table, "goblin_warrior-2")

    text = dump(combat_view(state, srd))

    for trace in ("goblin_warrior-2", "Гоблин 2", "invisible", "Невидим"):
        assert trace not in text, trace


def test_the_current_turn_of_a_hidden_monster_is_not_named_to_the_players(srd, table):
    state = with_hidden(table, "goblin_warrior-1")
    state.combat.turn_index = 0  # ходит спрятавшийся гоблин

    combat = combat_view(state, srd)

    assert combat["current_id"] is None
    assert combat["order"][0]["id"] == table.boris.character.id


def test_a_hidden_hero_stays_visible_to_his_own_party(srd, table):
    state = with_hidden(table, table.boris.character.id)

    combat = combat_view(state, srd)

    boris = next(f for f in combat["order"] if f["id"] == table.boris.character.id)
    assert {"id": "invisible", "name": "Невидим", "rounds_left": None} in boris["conditions"]


def test_a_monster_that_gave_itself_away_is_back_in_the_view(srd, table):
    state = with_hidden(table, "goblin_warrior-2")
    state.combat.combatants["goblin_warrior-2"].conditions = []  # напал и перестал прятаться

    combat = combat_view(state, srd)

    assert "goblin_warrior-2" in [fighter["id"] for fighter in combat["order"]]


def test_the_combat_event_hides_the_monster_the_same_way_as_the_snapshot_block(srd, table):
    state = with_hidden(table, "goblin_warrior-2")

    assert combat_event(state, srd) == {"type": "combat", "combat": combat_view(state, srd)}
    assert "goblin_warrior-2" not in dump(state_events(state, srd))


async def test_without_combat_the_snapshot_has_none(db, srd, table):
    async with db() as session:
        state = table.state.model_copy(update={"combat": None, "version": 4})
        session.add(GameStateSnapshot.from_state(state))
        (await session.get(Game, table.game_id)).state_version = 4
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "alice")

    assert snapshot["combat"] is None


async def test_snapshot_follows_the_current_state_version_of_the_game(db, srd, table):
    # «Перегенерировать» возвращает игру к прежней версии, а новая остаётся в базе.
    async with db() as session:
        older = table.state.model_copy(deep=True, update={"version": 2})
        older.party[table.alice.character.id].hp = 1
        session.add(GameStateSnapshot.from_state(older))
        (await session.get(Game, table.game_id)).state_version = 2
        await session.commit()

    party = {m["id"]: m for m in (await snapshot_of(db, srd, table, "vera"))["party"]}

    assert party[table.alice.character.id]["hp"] == 1


async def test_unsupported_newer_snapshot_is_refused_not_shown_partially(db, srd, table):
    async with db() as session:
        row = await session.get(GameStateSnapshot, (table.game_id, 3))
        row.schema_version = 999
        await session.commit()

    with pytest.raises(UnsupportedSnapshotVersion):
        await snapshot_of(db, srd, table, "alice")


async def test_lobby_snapshot_has_my_character_but_no_game_state(db, srd, lobby_table):
    snapshot = await snapshot_of(db, srd, lobby_table, "alice")

    assert snapshot["phase"] == {
        "phase": "lobby",
        "turn_phase": None,
        "deadline_at": None,
        "remaining_seconds": None,
        "scene": None,
    }
    assert snapshot["character"]["id"] == lobby_table.alice.character.id
    assert snapshot["game"]["players"][2]["ready"] is False  # Вера ещё не готова
    assert snapshot["party"] == []
    assert snapshot["combat"] is None
    assert snapshot["statuses"] == {}
    assert snapshot["action"] is None
    assert snapshot["feed"] == []


async def test_a_stranger_gets_no_snapshot_and_the_game_looks_like_it_does_not_exist(
    db, srd, table
):
    async with db() as session:
        stranger = User(dev_login="dev-9", name="Чужой")
        session.add(stranger)
        await session.commit()
        with pytest.raises(NotFound):
            await snapshot_for(session, stranger, table.game_id, srd=srd)


# --- лента ---


@pytest.mark.parametrize(
    ("key", "texts"),
    [
        pytest.param(
            "alice",
            [
                "Рассказ: сырой подвал.",
                "Проверка Внимательности",
                "ЛИЧНОЕ-alice: вы чуете холод.",
                "Алиса осматривает дверь.",
            ],
            id="Алиса",
        ),
        pytest.param(
            "boris",
            [
                "Рассказ: сырой подвал.",
                "Проверка Внимательности",
                "ЛИЧНОЕ-boris: в кармане ключ.",
                "Алиса осматривает дверь.",
                "ХОД-boris: подкрасться",
                "Скрытность",
            ],
            id="Борис",
        ),
        pytest.param(
            "vera",
            ["Рассказ: сырой подвал.", "Проверка Внимательности", "Алиса осматривает дверь."],
            id="Вера",
        ),
    ],
)
async def test_feed_has_only_what_the_player_may_see_in_order(db, srd, table, key, texts):
    feed = (await snapshot_of(db, srd, table, key))["feed"]

    # Нет чужих личных сообщений, чужих тайных ходов и бросков, снятых и «личных для всех».
    assert [item["text"] for item in feed] == texts
    assert [item["id"] for item in feed] == sorted(item["id"] for item in feed)


async def test_feed_item_marks_private_rows_without_exposing_the_audience(db, srd, table):
    feed = (await snapshot_of(db, srd, table, "boris"))["feed"]

    assert all(set(item) == FEED_ITEM_KEYS for item in feed)
    private = {item["text"]: item["private"] for item in feed}
    assert private["Рассказ: сырой подвал."] is False
    assert private["ЛИЧНОЕ-boris: в кармане ключ."] is True
    assert private["Скрытность"] is True
    roll = next(item for item in feed if item["text"] == "Проверка Внимательности")
    assert roll["kind"] == "roll"
    assert roll["data"] == {"label": "Внимательность", "total": 14}
    assert datetime.fromisoformat(roll["created_at"]).utcoffset().total_seconds() == 0
    assert table.boris.user_id not in dump(feed)


async def test_feed_keeps_the_last_200_messages_the_player_can_see(db, srd, table):
    for number in range(FEED_LIMIT + 10):
        await table.add_message(f"Событие {number}")
    for _ in range(50):  # чужие личные сообщения не отнимают места у ленты Алисы
        await table.add_message("ЛИЧНОЕ-boris: шёпот", audience=table.boris.user_id)

    feed = (await snapshot_of(db, srd, table, "alice"))["feed"]

    assert len(feed) == FEED_LIMIT
    assert feed[-1]["text"] == f"Событие {FEED_LIMIT + 9}"
    assert feed[0]["text"] == "Событие 10"
    assert "ЛИЧНОЕ-boris" not in dump(feed)


# --- тайны игры не уходят никому ---


@pytest.mark.parametrize("key", PLAYER_KEYS)
async def test_snapshot_has_no_secrets_of_the_game_for_any_player(db, srd, table, key):
    snapshot = await snapshot_of(db, srd, table, key)

    text = dump(snapshot)
    assert SECRET_MARK not in text
    assert not MASTER_KEYS & set(keys_of(snapshot))
    for other in PLAYER_KEYS:
        if other != key:
            assert f"ПРЕДЫСТОРИЯ-{other}" not in text
            assert f"-{other}" not in text  # ни личного, ни тайного хода, ни броска другого
    assert f"ПРЕДЫСТОРИЯ-{key}" in text


async def test_snapshot_has_only_the_keys_of_the_contract(db, srd, table):
    snapshot = await snapshot_of(db, srd, table, "alice")

    assert set(snapshot) == {
        "type",
        "server_time",
        "game",
        "host_user_id",
        "phase",
        "statuses",
        "character",
        "action",
        "party",
        "combat",
        "feed",
    }


# --- адресат события движка ---


def test_audience_all_stays_all_and_a_character_id_becomes_the_id_of_its_player(table):
    party = table.state.party

    assert audience_to_user(party, "all") == "all"
    for seat in table.seats.values():
        assert audience_to_user(party, seat.character.id) == seat.user_id


@pytest.mark.parametrize(
    "audience", ["", "nobody", "ALL", "zero_hp", "hide", "pc_0000000000000000"]
)
def test_unknown_audience_is_nobody_and_never_everybody(table, audience):
    assert audience_to_user(table.state.party, audience) is None


def test_user_id_is_not_an_address_of_the_engine(table):
    # События движка адресованы персонажам: id игрока в этом пространстве имён ничей.
    assert audience_to_user(table.state.party, table.alice.user_id) is None


def test_character_without_a_player_has_no_addressee(table):
    orphan = table.alice.character.model_copy(update={"player_id": None})
    party = {orphan.id: orphan}

    assert audience_to_user(party, orphan.id) is None


def test_character_id_equal_to_another_players_user_id_is_still_delivered_to_its_owner(table):
    # Серверу так не выдают id, но проверка не должна на это полагаться: строка «id персонажа»
    # совпала с user_id Бориса, тайное получает владелец персонажа (Алиса), а не Борис.
    clash = table.alice.character.model_copy(update={"id": table.boris.user_id})

    party = {clash.id: clash}

    assert audience_to_user(party, table.boris.user_id) == table.alice.user_id


def test_a_character_named_all_makes_the_everyone_audience_ambiguous_so_nobody_gets_it(table):
    clash = table.alice.character.model_copy(update={"id": "all"})
    party = {clash.id: clash, **table.state.party}

    assert audience_to_user(party, "all") is None


def test_server_issued_character_ids_have_a_format_no_user_id_or_reserved_word_has():
    ids = {new_character_id() for _ in range(200)}

    assert all(re.fullmatch(r"pc_[0-9a-f]{16}", character_id) for character_id in ids)
    assert not ids & {"all", "", "zero_hp", "hide"}
    assert not re.fullmatch(r"pc_[0-9a-f]{16}", new_id())  # id пользователя — другого вида


# --- события: те же построители, что и у снимка ---


async def test_a_paused_game_shows_the_frozen_remainder_instead_of_a_running_deadline(
    db, srd, table
):
    # Срок отсчёта на паузе не идёт: клиент получает остаток, а не срок, от которого он считал бы
    # время дальше. Остаток считается от момента паузы и округляется вверх до секунды.
    paused_at = datetime(2030, 5, 1, 12, 29, 10, 800000, tzinfo=UTC)
    async with db() as session:
        game = await session.get(Game, table.game_id)
        game.phase = GamePhase.PAUSED
        game.clock_at = paused_at
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "alice")

    assert snapshot["phase"]["phase"] == "paused"
    assert snapshot["phase"]["turn_phase"] == "collecting"
    assert snapshot["phase"]["deadline_at"] is None
    assert snapshot["phase"]["remaining_seconds"] == 50  # 49,2 с вверх


async def test_a_paused_game_without_a_countdown_has_no_remainder(db, srd, table):
    async with db() as session:
        game = await session.get(Game, table.game_id)
        game.phase, game.deadline_at = GamePhase.PAUSED, None
        game.clock_at = datetime(2030, 5, 1, 12, 0, tzinfo=UTC)
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "alice")

    assert snapshot["phase"]["deadline_at"] is None
    assert snapshot["phase"]["remaining_seconds"] is None


async def test_phase_event_carries_the_phase_block_of_the_snapshot(db, srd, table):
    async with db() as session:
        game = await session.get(Game, table.game_id)

    snapshot = await snapshot_of(db, srd, table, "alice")

    assert phase_event(game, table.state) == {"type": "phase", "phase": snapshot["phase"]}
    assert phase_event(game, None)["phase"]["scene"] is None


def test_player_status_event_names_the_player_and_the_public_status():
    assert player_status_event("u1", ActionStatus.AUTO_SUBMITTED) == {
        "type": "player_status",
        "user_id": "u1",
        "status": "ready",
    }
    assert player_status_event("u1", ActionStatus.DRAFT)["status"] == "writing"
    assert player_status_event("u1", ActionStatus.PASS)["status"] == "pass"


async def test_feed_item_event_is_the_feed_item_of_the_snapshot(db, srd, table):
    message = await table.add_message(
        "ЛИЧНОЕ-vera: тихо.", audience=table.vera.user_id, kind=MessageKind.PRIVATE
    )

    event = feed_item_event(message)
    feed = (await snapshot_of(db, srd, table, "vera"))["feed"]

    assert event == {"type": "feed_item", "item": feed[-1]}
    assert event["item"]["private"] is True


async def test_state_events_are_party_combat_and_one_character_sheet_per_owner(srd, table):
    deliveries = state_events(table.state, srd)

    assert [payload["type"] for payload, _ in deliveries] == [
        "party",
        "combat",
        "character",
        "character",
        "character",
    ]
    assert all(payload["type"] in EVENT_TYPES for payload, _ in deliveries)
    assert [audience for _, audience in deliveries] == [
        "all",
        "all",
        *(seat.user_id for seat in table.seats.values()),
    ]


async def test_state_events_never_carry_secrets_and_a_sheet_goes_only_to_its_owner(srd, table):
    for payload, audience in state_events(table.state, srd):
        text = dump(payload)
        assert SECRET_MARK not in text
        assert not MASTER_KEYS & set(keys_of(payload))
        for seat in table.seats.values():
            if f"ПРЕДЫСТОРИЯ-{seat.key}" in text:
                assert payload["type"] == "character"
                assert audience == seat.user_id


def test_party_and_combat_events_match_the_views_of_the_snapshot_blocks(srd, table):
    assert party_event(table.state, srd)["type"] == "party"
    assert {m["id"] for m in party_event(table.state, srd)["party"]} == set(table.state.party)
    assert combat_event(table.state, srd)["combat"]["current_id"] == table.boris.character.id
    ended = table.state.model_copy(update={"combat": None})
    assert combat_event(ended, srd) == {"type": "combat", "combat": None}


def test_character_without_an_owner_gets_no_sheet_event(srd, table):
    orphan = table.alice.character.model_copy(update={"player_id": None})
    state = table.state.model_copy(update={"party": {orphan.id: orphan}})

    assert [p["type"] for p, _ in state_events(state, srd)] == ["party", "combat"]
    assert character_event(orphan, srd)["character"]["id"] == orphan.id


# --- лист владельца не называет источник состояний (security-review B7, L2) ---

SNARING_MONSTER = "goblin_warrior-2"
SNARED = {"id": "restrained", "name": "Опутан", "rounds_left": 2}  # как состояние видит владелец


def snared_by_a_hidden_monster(table):
    """Алиса опутана монстром, который спрятан в бою: в состоянии героя лежит id этого монстра."""
    state = with_hidden(table, SNARING_MONSTER)
    snare = ActiveCondition(
        id="restrained", rounds_left=2, source=SNARING_MONSTER, save=Ability.STR, dc=12
    )
    state.party[table.alice.character.id].conditions.append(snare)
    return state


def test_the_sheet_of_the_owner_lists_states_and_effects_in_the_form_of_the_party_block(
    srd, table
):
    sheet = character_event(table.alice.character, srd)["character"]

    assert sheet["conditions"] == [{"id": "poisoned", "name": "Отравлен", "rounds_left": 3}]
    assert sheet["buffs"] == [{"id": "bless", "name": "Благословение", "rounds_left": 5}]
    assert sheet["conditions"] == party_event(table.state, srd)["party"][0]["conditions"]
    assert sheet["buffs"] == party_event(table.state, srd)["party"][0]["effects"]


def test_the_rest_of_the_sheet_is_still_the_whole_character(srd, table):
    hero = table.alice.character

    sheet = character_event(hero, srd)["character"]

    whole = hero.model_dump(mode="json")
    rest = whole.keys() - {"conditions", "buffs"}
    assert sheet.keys() == whole.keys()  # ни одно поле листа не пропало
    assert {key: sheet[key] for key in rest} == {key: whole[key] for key in rest}
    assert sheet["persona"]["backstory"] == "ПРЕДЫСТОРИЯ-alice"
    assert sheet["inspiration"] is True


def test_no_event_of_the_state_carries_the_source_of_a_state_or_an_effect(srd, table):
    for payload, _ in state_events(table.state, srd):
        assert not {"source", "source_id"} & set(keys_of(payload)), payload["type"]
    assert "goblin-1" not in dump(state_events(table.state, srd))  # источник «отравлен» у Алисы


def test_the_sheet_event_of_a_hero_snared_by_a_hidden_monster_never_names_the_monster(srd, table):
    state = snared_by_a_hidden_monster(table)

    deliveries = state_events(state, srd)

    (event,) = [
        payload
        for payload, audience in deliveries
        if payload["type"] == "character" and audience == table.alice.user_id
    ]
    assert SNARED in event["character"]["conditions"]
    for trace in (SNARING_MONSTER, "Гоблин 2"):
        assert trace not in dump(deliveries), trace
    assert not {"source", "source_id"} & set(keys_of(deliveries))


async def test_the_snapshot_of_a_hero_snared_by_a_hidden_monster_never_names_the_monster(
    db, srd, table
):
    state = snared_by_a_hidden_monster(table).model_copy(update={"version": 4})
    async with db() as session:
        session.add(GameStateSnapshot.from_state(state))
        (await session.get(Game, table.game_id)).state_version = 4
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "alice")

    assert SNARED in snapshot["character"]["conditions"]
    for block in ("character", "party", "combat"):
        for trace in (SNARING_MONSTER, "Гоблин 2"):
            assert trace not in dump(snapshot[block]), (block, trace)
        assert not {"source", "source_id"} & set(keys_of(snapshot[block])), block


# --- сообщения ленты из событий хода мастера (B7.3) ---

DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


def dice_roll(expr: str = "1d20+3", seed: int = 3):
    return roll_dice(expr, rng=SeededRng(seed), reason="проверка")


def entries_of(
    table, srd, adventure, *events: Event, secret=(), before=None, after=None
) -> list[FeedEntry]:
    return feed_entries(
        events,
        before=before or table.state,
        after=after or table.state,
        adventure=adventure,
        srd=srd,
        secret_actors=set(secret),
    )


def test_a_public_roll_is_a_card_for_everybody_without_the_armor_class_of_the_target(
    table, srd, adventure
):
    event = RollEvent(
        actor_id=table.alice.character.id,
        label="Атака: длинный меч",
        roll=dice_roll(),
        target_ac=15,
        success=True,
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert (entry.audience, entry.kind) == ("all", MessageKind.ROLL)
    assert "попадание" in entry.text and "Алиса" in entry.text
    assert entry.data["actor"] == "Алиса"
    assert entry.data["actor_id"] == table.alice.character.id
    assert entry.data["total"] == event.roll.total and entry.data["natural"] == event.roll.natural
    assert entry.data["success"] is True
    assert "target_ac" not in entry.data


def test_a_check_card_names_the_difficulty_and_the_result(table, srd, adventure):
    event = RollEvent(
        actor_id=table.boris.character.id,
        label="Скрытность",
        roll=dice_roll(),
        dc=20,
        success=False,
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["dc"] == 20 and entry.data["success"] is False
    assert "Сл 20" in entry.text and "провал" in entry.text
    assert entry.data["advantage"] == "none"


def test_a_secret_roll_goes_to_the_user_of_the_addressed_character(table, srd, adventure):
    event = RollEvent(
        audience=table.boris.character.id,
        actor_id=table.boris.character.id,
        label="Скрытность",
        roll=dice_roll(),
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.audience == table.boris.user_id


def test_a_monster_rolls_under_its_name_from_the_combat(table, srd, adventure):
    event = RollEvent(actor_id="goblin_warrior-1", label="Атака: ятаган", roll=dice_roll())

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["actor"] == "Гоблин 1"


# --- броски NPC приключения (решение D9, security-review B7 L1) ---
# Карточка NPC несёт только имя: id из данных приключения (слаг вроде `cult-leader`) бывает
# откровеннее имени, поэтому его нет ни в подписи, ни в `data`.


def test_an_npc_of_the_adventure_rolls_under_its_name_and_not_under_its_id(table, srd, adventure):
    event = RollEvent(actor_id="fabian", label="Харизма (Обман)", roll=dice_roll(), success=False)

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["actor"] == adventure.npcs["fabian"].name == "Фабиан"
    assert entry.text.startswith("Фабиан: Харизма (Обман) — ")
    assert "actor_id" not in entry.data
    assert "fabian" not in entry.text + dump(entry.data)  # сырого id нет нигде в карточке
    assert entry.data["total"] == event.roll.total and entry.data["success"] is False


def test_a_contest_of_a_hero_and_an_npc_is_two_cards_with_names(table, srd, adventure):
    alice = table.alice.character.id
    events = [
        RollEvent(actor_id=alice, label="Харизма (Обман)", roll=dice_roll(), success=True),
        RollEvent(actor_id="fabian", label="Харизма (Проницательность)", roll=dice_roll()),
    ]

    entries = entries_of(table, srd, adventure, *events)

    assert [entry.data["actor"] for entry in entries] == ["Алиса", "Фабиан"]
    assert {entry.audience for entry in entries} == {"all"}
    # У героя id остаётся, как и у бойцов боя: клиент находит по нему героя отряда.
    assert entries[0].data["actor_id"] == alice and "actor_id" not in entries[1].data


def test_the_card_of_an_npc_in_a_secret_contest_goes_only_to_the_author_under_its_name(
    table, srd, adventure
):
    alice = table.alice.character.id
    events = [
        RollEvent(audience=alice, actor_id=alice, label="Скрытность", roll=dice_roll()),
        RollEvent(audience=alice, actor_id="fabian", label="Внимательность", roll=dice_roll()),
    ]

    entries = entries_of(table, srd, adventure, *events, secret={alice})

    assert [entry.audience for entry in entries] == [table.alice.user_id] * 2
    assert [entry.data["actor"] for entry in entries] == ["Алиса", "Фабиан"]
    assert entries[0].data["actor_id"] == alice and "actor_id" not in entries[1].data
    assert "fabian" not in dump([entry.data for entry in entries])


def test_a_roller_that_is_neither_a_hero_a_fighter_nor_an_npc_keeps_its_id(table, srd, adventure):
    event = RollEvent(actor_id="no-such-npc", label="Проверка", roll=dice_roll())

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["actor"] == entry.data["actor_id"] == "no-such-npc"


def test_a_fighter_that_shares_its_id_with_an_npc_is_still_a_fighter_with_its_id(
    table, srd, adventure
):
    # Герой или боец важнее NPC с тем же id, как и при поиске участника в движке.
    state = table.state.model_copy(deep=True)
    goblin = state.combat.combatants["goblin_warrior-1"]
    twin = goblin.model_copy(update={"id": "fabian", "name": "Двойник"})
    state.combat.combatants["fabian"] = twin
    event = RollEvent(actor_id="fabian", label="Атака: ятаган", roll=dice_roll())

    (entry,) = entries_of(table, srd, adventure, event, before=state, after=state)

    assert (entry.data["actor"], entry.data["actor_id"]) == ("Двойник", "fabian")


# --- броски спрятанного монстра (решение D7, security-review B7 L3) ---

HIDDEN_MONSTER = "goblin_warrior-2"
SOMEBODY_INVISIBLE = "Кто-то невидимый"
MONSTER_TRACES = ("goblin_warrior-2", "Гоблин 2", "Гоблин", "goblin")


def monster_roll(label: str = "Атака: ятаган", *, reason: str = "проверка", **fields) -> RollEvent:
    roll = roll_dice("1d20+4", rng=SeededRng(3), reason=reason)
    return RollEvent(actor_id=HIDDEN_MONSTER, label=label, roll=roll, **fields)


def test_a_roll_of_a_hidden_monster_is_a_card_of_somebody_invisible(table, srd, adventure):
    hidden = with_hidden(table, HIDDEN_MONSTER)
    event = monster_roll(target_ac=15, success=True)

    (entry,) = entries_of(table, srd, adventure, event, before=hidden, after=hidden)

    assert (entry.audience, entry.kind) == ("all", MessageKind.ROLL)
    assert entry.text.startswith(f"{SOMEBODY_INVISIBLE}: Атака: ятаган — {event.roll.total}")
    assert "попадание" in entry.text
    assert entry.data["actor"] == SOMEBODY_INVISIBLE
    assert "actor_id" not in entry.data
    for trace in MONSTER_TRACES:
        assert trace not in entry.text + dump(entry.data), trace


def test_the_card_of_a_hidden_monster_keeps_the_numbers_of_the_roll(table, srd, adventure):
    hidden = with_hidden(table, HIDDEN_MONSTER)
    event = monster_roll(dc=12, success=False)

    (entry,) = entries_of(table, srd, adventure, event, before=hidden, after=hidden)

    assert entry.data["total"] == event.roll.total and entry.data["rolls"] == event.roll.rolls
    assert (entry.data["dc"], entry.data["success"]) == (12, False)
    assert entry.data["label"] == "Атака: ятаган"


def test_the_reason_of_the_roll_cannot_give_the_hidden_monster_away(table, srd, adventure):
    # Причина броска, которую пишет движок, бывает с именем бойца: «Скрытность: Гоблин 2».
    hidden = with_hidden(table, HIDDEN_MONSTER)
    event = monster_roll("Скрытность", reason="Скрытность: Гоблин 2")

    (entry,) = entries_of(table, srd, adventure, event, before=hidden, after=hidden)

    assert "Гоблин 2" not in dump(entry.data) and entry.data["reason"] == "Скрытность"


@pytest.mark.parametrize(
    ("hidden_before", "hidden_after"),
    [(True, True), (True, False), (False, True)],
    ids=["невидим весь ход", "напал и перестал прятаться", "спрятался в этом ходу"],
)
def test_a_monster_hidden_before_or_after_the_turn_is_anonymous_in_its_rolls(
    table, srd, adventure, hidden_before, hidden_after
):
    # В `feed_entries` только состояния до и после хода, поэтому скрывается с запасом: лишний раз
    # безымянная карточка безвреднее выданной засады.
    visible, hidden = table.state, with_hidden(table, HIDDEN_MONSTER)
    event = monster_roll()

    (entry,) = entries_of(
        table,
        srd,
        adventure,
        event,
        before=hidden if hidden_before else visible,
        after=hidden if hidden_after else visible,
    )

    assert entry.data["actor"] == SOMEBODY_INVISIBLE and "actor_id" not in entry.data
    assert "Гоблин" not in entry.text


def test_only_the_hidden_monster_is_anonymous_the_others_roll_under_their_names(
    table, srd, adventure
):
    hidden = with_hidden(table, HIDDEN_MONSTER)
    events = [
        RollEvent(actor_id="goblin_warrior-1", label="Атака: ятаган", roll=dice_roll()),
        monster_roll(),
        RollEvent(actor_id=table.alice.character.id, label="Атака: меч", roll=dice_roll()),
    ]

    entries = entries_of(table, srd, adventure, *events, before=hidden, after=hidden)

    assert [entry.data["actor"] for entry in entries] == ["Гоблин 1", SOMEBODY_INVISIBLE, "Алиса"]
    assert entries[0].data["actor_id"] == "goblin_warrior-1"
    assert entries[2].data["actor_id"] == table.alice.character.id


def test_a_hidden_hero_rolls_under_his_name_for_his_own_party(table, srd, adventure):
    hidden = with_hidden(table, table.boris.character.id)
    event = RollEvent(actor_id=table.boris.character.id, label="Атака: кинжал", roll=dice_roll())

    (entry,) = entries_of(table, srd, adventure, event, before=hidden, after=hidden)

    assert entry.data["actor"] == "Борис" and entry.data["actor_id"] == table.boris.character.id


def test_an_event_for_an_unknown_audience_is_dropped_and_logged_without_its_text(
    table, srd, adventure, caplog
):
    caplog.set_level(logging.WARNING)
    events = [
        RollEvent(audience="pc_0000000000000000", actor_id="x", label="Тайна", roll=dice_roll()),
        PrivateMessageEvent(audience="pc_0000000000000000", text="ТАЙНА-текст личного сообщения"),
    ]

    assert entries_of(table, srd, adventure, *events) == []

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    assert "ТАЙНА" not in caplog.text


def test_a_private_message_goes_to_the_user_of_its_addressee_as_is(table, srd, adventure):
    event = PrivateMessageEvent(audience=table.vera.character.id, text="ЛИЧНОЕ-vera: шёпот")

    (entry,) = entries_of(table, srd, adventure, event)

    assert (entry.audience, entry.kind, entry.text, entry.data) == (
        table.vera.user_id,
        MessageKind.PRIVATE,
        "ЛИЧНОЕ-vera: шёпот",
        None,
    )


def test_move_combat_and_rest_are_short_lines_for_everybody(table, srd, adventure):
    events = [
        MoveEvent(location_id="cemetery"),
        CombatEvent(phase="started"),
        CombatEvent(phase="turn", actor_id=table.alice.character.id),
        CombatEvent(phase="ended", winner="party"),
        CombatEvent(phase="ended", winner="enemies"),
        RestEvent(rest="long", outcomes=[]),
        RestEvent(rest="short", outcomes=[]),
    ]

    result = entries_of(table, srd, adventure, *events)

    assert [(e.audience, e.kind) for e in result] == [("all", MessageKind.SYSTEM)] * 6
    texts = [e.text for e in result]
    assert adventure.locations["cemetery"].name in texts[0]
    assert "бой" in texts[1].lower() and "победил" in texts[2] and "повержен" in texts[3]
    assert "Длинный отдых" in texts[4] and "Короткий отдых" in texts[5]


def test_inspiration_is_a_line_for_everybody_unless_its_hero_acts_in_secret(table, srd, adventure):
    event = InspirationEvent(
        character_id=table.vera.character.id, granted=True, reason="красивая реплика"
    )

    (public,) = entries_of(table, srd, adventure, event)
    (secret,) = entries_of(table, srd, adventure, event, secret={table.vera.character.id})

    assert (public.audience, "Вера" in public.text, "красивая реплика" in public.text) == (
        "all",
        True,
        True,
    )
    assert secret.audience == table.vera.user_id


def test_item_lines_name_the_item_of_the_adventure_and_who_got_it(table, srd, adventure):
    alice, boris = table.alice.character.id, table.boris.character.id
    events = [
        ItemEvent(item_id="chest-key", holder_from="npc:marta", holder_to=f"character:{boris}"),
        ItemEvent(
            item_id="chest-key", holder_from=f"character:{boris}", holder_to=f"character:{alice}"
        ),
        ItemEvent(item_id="chest-key", holder_from=f"character:{alice}", holder_to="gone"),
        ItemEvent(
            item_id="chest-key", holder_from=f"character:{alice}", holder_to="location:village"
        ),
        ItemEvent(
            item_id="no-such-item", holder_from="location:village", holder_to=f"character:{boris}"
        ),
    ]

    result = entries_of(table, srd, adventure, *events)

    assert [e.audience for e in result] == ["all"] * 5
    name = "Запасной ключ Ульриха"
    assert "Борис получает" in result[0].text and name in result[0].text
    assert "Борис" in result[1].text and "Алиса" in result[1].text
    assert "Алиса" in result[2].text and "Алиса" in result[3].text
    assert "no-such-item" in result[4].text  # предмета нет ни в приключении, ни в SRD: остаётся id


def test_an_item_that_changes_hands_with_a_secret_hero_is_a_private_line(table, srd, adventure):
    alice, boris = table.alice.character.id, table.boris.character.id
    event = ItemEvent(
        item_id="chest-key", holder_from=f"character:{alice}", holder_to=f"character:{boris}"
    )

    (line,) = entries_of(table, srd, adventure, event, secret={boris})

    assert line.audience == table.boris.user_id


def test_events_without_a_line_in_the_feed_leave_no_trace_of_hidden_numbers(table, srd, adventure):
    events = [
        RevealEvent(ref="feature:board"),
        FlagEvent(name="met_marta", value=True),
        AttitudeEvent(npc_id="marta", value=2),
        SceneEvent(scene=SceneType.COMBAT),
        ConditionEvent(target_id=table.alice.character.id, condition="prone", added=True),
        ActionEvent(actor_id=table.alice.character.id, action="dodge"),
        ChronicleEvent(text="ТАЙНА-запись летописи"),
        DamageEvent(target_id="goblin_warrior-3", amount=4, damage_type="slashing", hp_after=3),
        HealEvent(target_id=table.alice.character.id, amount=2, hp_after=9),
        CombatEvent(phase="turn", actor_id=table.alice.character.id),
    ]

    assert entries_of(table, srd, adventure, *events) == []


def test_entries_keep_the_order_of_the_events(table, srd, adventure):
    events = [
        MoveEvent(location_id="cemetery"),
        PrivateMessageEvent(audience=table.alice.character.id, text="шёпот"),
        RollEvent(actor_id=table.alice.character.id, label="Внимательность", roll=dice_roll()),
    ]

    result = entries_of(table, srd, adventure, *events)

    assert [e.kind for e in result] == [MessageKind.SYSTEM, MessageKind.PRIVATE, MessageKind.ROLL]


def test_a_withdrawn_feed_item_is_a_tombstone_in_the_feed_item_event():
    event = feed_withdrawn_event(12)

    assert event == {"type": "feed_item", "item": {"id": 12, "superseded": True}}
    assert event["type"] in EVENT_TYPES


# --- привал (план B7.6) ---


async def in_the_camp(db: async_sessionmaker[AsyncSession], table) -> None:
    """Игра у костра: фаза `camp` с идущим отсчётом, сцена `camp`, боя нет."""
    async with db() as session:
        game = await session.get(Game, table.game_id)
        game.turn_phase = TurnPhase.CAMP
        row = await session.get(GameStateSnapshot, (table.game_id, game.state_version))
        state = row.to_state()
        state.scene, state.combat = Scene(type=SceneType.CAMP), None
        row.data = GameStateSnapshot.from_state(state).data
        await session.commit()


async def test_the_camp_phase_shows_the_running_deadline_and_the_camp_scene(db, srd, table):
    await in_the_camp(db, table)

    snapshot = await snapshot_of(db, srd, table, "vera")

    assert snapshot["phase"] == {
        "phase": "playing",
        "turn_phase": "camp",
        "deadline_at": "2030-05-01T12:30:00+00:00",
        "remaining_seconds": None,
        "scene": {"type": "camp", "day": 2, "part": "evening"},
    }
    assert snapshot["combat"] is None


async def test_a_paused_camp_shows_the_frozen_remainder_not_a_running_deadline(db, srd, table):
    await in_the_camp(db, table)
    async with db() as session:
        game = await session.get(Game, table.game_id)
        game.phase = GamePhase.PAUSED
        game.clock_at = datetime(2030, 5, 1, 12, 25, tzinfo=UTC)  # поставлена за 5 минут до срока
        await session.commit()

    phase = (await snapshot_of(db, srd, table, "alice"))["phase"]

    assert (phase["turn_phase"], phase["deadline_at"], phase["remaining_seconds"]) == (
        "camp",
        None,
        300,
    )


async def test_a_camp_line_is_in_the_feed_of_every_player_with_its_speaker(db, srd, table):
    line = await table.add_message(
        "Подкину дров.",
        kind=MessageKind.CAMP_LINE,
        data={"actor_id": table.alice.character.id, "actor": "Алиса"},
    )

    for key in PLAYER_KEYS:
        feed = (await snapshot_of(db, srd, table, key))["feed"]
        item = next(i for i in feed if i["id"] == line.id)
        assert item["kind"] == "camp_line" and item["private"] is False
        assert item["data"] == {"actor_id": table.alice.character.id, "actor": "Алиса"}
    event = feed_item_event(line)
    assert event["type"] == "feed_item" and event["item"]["text"] == "Подкину дров."


async def test_a_withdrawn_camp_line_is_not_in_the_snapshot_any_more(db, srd, table):
    await table.add_message("Лишняя реплика.", kind=MessageKind.CAMP_LINE, superseded=True)

    feed = (await snapshot_of(db, srd, table, "boris"))["feed"]

    assert "Лишняя реплика." not in [item["text"] for item in feed]
