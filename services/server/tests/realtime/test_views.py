"""Проекция игры для игрока (план B6.5): снимок, события и адресаты.

Главное, что здесь проверяется, — приватность (инвариант «г»): тайные ходы и личные сообщения видит
только адресат, а зацепки, мир, заметки мастера, журнал хода и точные HP монстров не видит никто.
Тексты-ловушки лежат в игре из `conftest.py`; тесты ищут их в ответах целиком.
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.content import SrdContent
from dnd_server.characters.service import new_character_id
from dnd_server.db.models import (
    ActionStatus,
    Game,
    GameStateSnapshot,
    MessageKind,
    PlayerAction,
    UnsupportedSnapshotVersion,
    User,
    new_id,
)
from dnd_server.games.service import NotFound
from dnd_server.realtime.hub import EVENT_TYPES
from dnd_server.realtime.views import (
    FEED_LIMIT,
    audience_to_user,
    character_event,
    combat_event,
    feed_item_event,
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
    # Названия берутся из данных SRD. У метки «Помощь» (`helped`) названия в данных нет: до его
    # появления клиент получает id, а не придуманную сервером копию.
    boris = party[table.boris.character.id]
    assert boris["conditions"] == [{"id": "helped", "name": "helped", "rounds_left": 1}]


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
    assert character_event(orphan)["character"]["id"] == orphan.id
