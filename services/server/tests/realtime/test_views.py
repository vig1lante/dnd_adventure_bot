"""Проекция игры для игрока (план B6.5): снимок, события и адресаты.

Главное, что здесь проверяется, — приватность (инвариант «г»): тайные ходы и личные сообщения видит
только адресат, а зацепки, мир, заметки мастера, журнал хода и точные HP монстров не видит никто.
Тексты-ловушки лежат в игре из `conftest.py`; тесты ищут их в ответах целиком.
"""

import copy
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_ABILITY, SKILL_NAMES_RU, Ability, Skill
from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import (
    ActiveBuff,
    ActiveCondition,
    InventoryItem,
    ability_mod,
    armor_class,
    passive_perception,
    prof_bonus,
    save_bonus,
    skill_bonus,
    spell_attack_bonus,
    spell_save_dc,
    weapon_attack,
)
from dnd_engine.combat.engine import MARK_NAMES_RU, monster_action, start_combat
from dnd_engine.combat.model import Combatant, Zone
from dnd_engine.content import SrdContent
from dnd_engine.creation import quick_character
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
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState, Scene, SceneType
from dnd_engine.tools import build_registry
from dnd_engine.tools.registry import ToolContext
from dnd_server.characters.service import new_character_id
from dnd_server.db.models import (
    ActionStatus,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    TurnPhase,
    UnsupportedSnapshotVersion,
    User,
    new_id,
)
from dnd_server.dm.combat_flow import waiting_hero
from dnd_server.dm.context import _carried_adventure_items
from dnd_server.games.service import NotFound
from dnd_server.realtime.hub import EVENT_TYPES
from dnd_server.realtime.views import (
    FEED_LIMIT,
    FeedEntry,
    audience_to_user,
    character_event,
    character_view,
    combat_event,
    combat_view,
    effect_name,
    feed_entries,
    feed_item_event,
    feed_withdrawn_event,
    npc_fighter_names,
    party_event,
    phase_event,
    player_status_event,
    snapshot_for,
    state_events,
)

SECRET_MARK = "ТАЙНА-"  # метка из conftest.py: то, что не видит никто
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
DEMO_ADVENTURE = load_adventure(DEMO_ADVENTURE_DIR)  # то же приключение, что в копии игры стола
PLAYER_KEYS = ["alice", "boris", "vera"]
# Ключи, из которых не должно состоять ничего, что уходит игроку: это данные мастера.
MASTER_KEYS = {"pc_hooks", "world", "dm_notes", "chronicle", "item_holders", "npc_alive", "flags"}
PARTY_KEYS = {"id", "name", "player_id", "hp", "hp_max", "temp_hp", "conditions", "effects"}
MONSTER_KEYS = {"id", "name", "kind", "side", "zone_id", "health", "conditions"}
PC_FIGHTER_KEYS = {
    "id", "name", "kind", "side", "zone_id", "character_id", "hp", "hp_max", "conditions",
    "can_act",
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


def with_hidden(table, fighter_id: str, *, source: str = "hide", state=None):
    """Состояние боя, где боец спрятан: на нём состояние «невидим». По умолчанию это состояние
    игры стола, иначе переданное `state`."""
    state = (state or table.state).model_copy(deep=True)
    # Состояния героя лежат в персонаже, состояния монстра — в самом бойце.
    holder = state.party.get(fighter_id) or state.combat.combatants[fighter_id]
    holder.conditions.append(ActiveCondition(id="invisible", source=source))
    return state


@pytest.mark.parametrize("source", ["hide", "", "dm"], ids=["спрятался", "без источника", "мастер"])
def test_a_hidden_monster_is_not_in_the_order_or_the_zones_of_the_combat(srd, table, source):
    state = with_hidden(table, "goblin_warrior-2", source=source)

    combat = combat_view(state, srd, table.adventure)

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

    text = dump(combat_view(state, srd, table.adventure))

    for trace in ("goblin_warrior-2", "Гоблин 2", "invisible", "Невидим"):
        assert trace not in text, trace


def test_a_hero_in_the_order_shows_his_current_and_maximum_health(srd, table):
    state = table.state.model_copy(deep=True)
    boris = table.boris.character.id
    state.party[boris].hp = 4

    fighters = {f["id"]: f for f in combat_view(state, srd, table.adventure)["order"]}

    for hero_id in (boris, table.alice.character.id):
        hero = state.party[hero_id]
        assert (fighters[hero_id]["hp"], fighters[hero_id]["hp_max"]) == (hero.hp, hero.hp_max)
    assert fighters[boris]["hp"] == 4 and fighters[boris]["hp_max"] > 4


def test_conditions_of_a_hero_in_the_order_are_named_for_everybody(srd, table):
    state = table.state.model_copy(deep=True)
    boris, alice = table.boris.character.id, table.alice.character.id
    state.party[boris].conditions.append(ActiveCondition(id="blinded"))

    fighters = {f["id"]: f for f in combat_view(state, srd, table.adventure)["order"]}

    blinded = {"id": "blinded", "name": "Ослеплён", "rounds_left": None}
    assert blinded in fighters[boris]["conditions"]
    assert blinded not in fighters[alice]["conditions"]


def test_the_current_turn_is_named_for_every_place_of_the_queue_including_the_first(srd, table):
    # Бой начинается с первого в инициативе (индекс 0): его ход игроки обязаны видеть так же, как
    # любой другой; вне очереди (до начала и после конца круга) текущего нет.
    order = combat_view(table.state, srd, table.adventure)["order"]
    state = table.state.model_copy(deep=True)

    for index, fighter in enumerate(order):
        state.combat.turn_index = index
        assert combat_view(state, srd, table.adventure)["current_id"] == fighter["id"], index

    for outside in (-1, len(order)):
        state.combat.turn_index = outside
        assert combat_view(state, srd, table.adventure)["current_id"] is None, outside


def test_the_current_turn_of_a_hidden_monster_is_not_named_to_the_players(srd, table):
    state = with_hidden(table, "goblin_warrior-1")
    state.combat.turn_index = 0  # ходит спрятавшийся гоблин

    combat = combat_view(state, srd, table.adventure)

    assert combat["current_id"] is None
    assert combat["order"][0]["id"] == table.boris.character.id


def test_a_hidden_hero_stays_visible_to_his_own_party(srd, table):
    state = with_hidden(table, table.boris.character.id)

    combat = combat_view(state, srd, table.adventure)

    boris = next(f for f in combat["order"] if f["id"] == table.boris.character.id)
    assert {"id": "invisible", "name": "Невидим", "rounds_left": None} in boris["conditions"]


def test_a_monster_that_gave_itself_away_is_back_in_the_view(srd, table):
    state = with_hidden(table, "goblin_warrior-2")
    state.combat.combatants["goblin_warrior-2"].conditions = []  # напал и перестал прятаться

    combat = combat_view(state, srd, table.adventure)

    assert "goblin_warrior-2" in [fighter["id"] for fighter in combat["order"]]


def test_the_combat_event_hides_the_monster_the_same_way_as_the_snapshot_block(srd, table):
    state = with_hidden(table, "goblin_warrior-2")

    combat = combat_view(state, srd, table.adventure)
    assert combat_event(state, srd, table.adventure) == {"type": "combat", "combat": combat}
    assert "goblin_warrior-2" not in dump(state_events(state, srd, table.adventure))


# --- боец-монстр, за которым стоит NPC (B8.6, nit B4): в бою он под именем NPC ---
# В приключении у боя нет `npc_id`: NPC связан с бойцом через статблок и локацию (так же, как движок
# отмечает павших NPC). Любая неоднозначность оставляет имя статблока.

FABIAN_FIGHTER = "cultist_fanatic-1"


def with_cultist(table, *, count: int = 1, location: str = "crypt", **world):
    """Бой, в котором против отряда стоит Фанатик культа (статблок Фабиана из demo-crypt)."""
    state = table.state.model_copy(deep=True)
    state.location_id = location
    state.world = state.world.model_copy(update=world)
    for number in range(1, count + 1):
        fighter_id = f"cultist_fanatic-{number}"
        state.combat.combatants[fighter_id] = Combatant(
            id=fighter_id,
            kind="monster",
            side="enemies",
            ref="cultist_fanatic",
            name="Фанатик культа" if count == 1 else f"Фанатик культа {number}",
            hp=44,
            hp_max=44,
            ac=13,
            zone_id="yard",
        )
        state.combat.order.append(fighter_id)
    return state


def names_in(state, srd, adventure) -> dict[str, str]:
    order = combat_view(state, srd, adventure)["order"]
    return {fighter["id"]: fighter["name"] for fighter in order}


def test_a_monster_that_is_an_npc_of_the_adventure_is_listed_under_the_name_of_the_npc(table, srd):
    names = names_in(with_cultist(table), srd, table.adventure)

    assert names[FABIAN_FIGHTER] == "Фабиан"
    # Остальные бойцы как были: безымянные монстры — по статблоку, герои — по имени.
    goblins = [names[f"goblin_warrior-{number}"] for number in (1, 2, 3, 4)]
    assert goblins == [f"Гоблин {number}" for number in (1, 2, 3, 4)]
    assert (names[table.alice.character.id], names[table.boris.character.id]) == ("Алиса", "Борис")


def test_the_named_monster_keeps_its_id_and_the_keys_of_a_monster(table, srd):
    order = combat_view(with_cultist(table), srd, table.adventure)["order"]

    fabian = next(fighter for fighter in order if fighter["id"] == FABIAN_FIGHTER)

    assert set(fabian) == MONSTER_KEYS  # ни признака NPC, ни статблока, ни id NPC
    assert fabian["kind"] == "monster" and fabian["health"] == "цел"


def test_the_name_of_the_npc_reaches_the_clients_in_the_event_the_same_way(table, srd):
    state = with_cultist(table)

    event = combat_event(state, srd, table.adventure)
    deliveries = dump(state_events(state, srd, table.adventure))

    assert event == {"type": "combat", "combat": combat_view(state, srd, table.adventure)}
    assert "Фабиан" in deliveries and "Фанатик культа" not in deliveries


async def test_the_snapshot_shows_the_monster_under_the_name_of_the_npc(db, srd, table):
    state = with_cultist(table).model_copy(update={"version": 4})
    async with db() as session:
        session.add(GameStateSnapshot.from_state(state))
        (await session.get(Game, table.game_id)).state_version = 4
        await session.commit()

    snapshot = await snapshot_of(db, srd, table, "vera")

    names = {fighter["id"]: fighter["name"] for fighter in snapshot["combat"]["order"]}
    assert names[FABIAN_FIGHTER] == "Фабиан"
    assert "Фанатик культа" not in dump(snapshot["combat"])


@pytest.mark.parametrize(
    "options",
    [{"location": "cemetery"}, {"npc_alive": {"fabian": False}}, {"count": 2}],
    ids=["NPC в другой локации", "NPC уже мёртв", "в бою два бойца с этим статблоком"],
)
def test_without_a_sure_link_the_monster_keeps_the_name_of_its_statblock(table, srd, options):
    names = names_in(with_cultist(table, **options), srd, table.adventure)

    cultists = [name for fighter_id, name in names.items() if fighter_id.startswith("cultist")]
    assert cultists and all(name.startswith("Фанатик культа") for name in cultists)
    assert "Фабиан" not in dump(names)


def test_an_npc_that_has_moved_to_the_place_of_the_fight_is_still_the_monster(table, srd):
    # Место NPC определяет мир (`npc_location`), а не только приключение.
    state = with_cultist(table, location="cemetery", npc_location={"fabian": "cemetery"})

    assert names_in(state, srd, table.adventure)[FABIAN_FIGHTER] == "Фабиан"


def test_two_npcs_with_the_same_statblock_at_the_place_make_the_link_unsure(table, srd):
    twin = table.adventure.npcs["fabian"].model_copy(update={"id": "twin", "name": "Двойник"})
    crowded = table.adventure.model_copy(update={"npcs": {**table.adventure.npcs, "twin": twin}})

    names = names_in(with_cultist(table), srd, crowded)

    assert names[FABIAN_FIGHTER] == "Фанатик культа"


def test_a_hidden_twin_of_the_statblock_makes_the_link_unsure_and_is_not_shown(table, srd):
    # Спрятавшегося нет в ответе вовсе, а оставшийся из того же статблока уже не отличим от него.
    state = with_hidden(table, "cultist_fanatic-2", state=with_cultist(table, count=2))

    view = dump(combat_view(state, srd, table.adventure))

    assert "cultist_fanatic-2" not in view and "Фанатик культа 2" not in view
    assert "Фанатик культа 1" in view and "Фабиан" not in view


def test_a_lone_hidden_monster_of_an_npc_is_absent_and_the_npc_is_not_named(table, srd):
    state = with_hidden(table, FABIAN_FIGHTER, state=with_cultist(table))

    view = dump(combat_view(state, srd, table.adventure))

    assert FABIAN_FIGHTER not in view
    assert "Фабиан" not in view and "Фанатик" not in view


def test_the_helper_never_names_a_hidden_monster_whoever_calls_it(table):
    # Скрытие в порядке хода и в карточках само по себе, но имя спрятанного не должно выйти и из
    # самого помощника: на него смотрят оба места.
    state = with_hidden(table, FABIAN_FIGHTER, state=with_cultist(table))

    assert npc_fighter_names(state, table.adventure) == {}
    assert npc_fighter_names(with_cultist(table), table.adventure) == {FABIAN_FIGHTER: "Фабиан"}
    assert npc_fighter_names(table.state.model_copy(update={"combat": None}), table.adventure) == {}


def test_the_events_give_nothing_of_the_npc_but_the_name(table, srd):
    fabian = table.adventure.npcs["fabian"]
    deliveries = state_events(with_cultist(table), srd, table.adventure)

    text = dump(deliveries)

    for private in (fabian.secret, fabian.role, fabian.motive, fabian.appearance, '"fabian"'):
        assert private not in text, private
    assert not MASTER_KEYS & set(keys_of(deliveries))


# --- может ли герой действовать (план B8.6b, фаза B8.s2) ---
# Очередь может стоять на герое, который не ходит (парализован, без сознания): сервер ждёт героя
# только тогда, когда он способен действовать (`waiting_hero`), и отказывает ему иначе. Клиент не
# знает правил, поэтому в порядке хода у героя есть `can_act` от движка; у монстров его нет.

INCAPACITATING = ["incapacitated", "paralyzed", "petrified", "stunned", "unconscious"]
STILL_ABLE = ["poisoned", "prone", "restrained", "blinded", "frightened", "grappled", "invisible"]


def on_turn(table, hero_id: str, *, condition: str | None = None, hp: int | None = None):
    """Состояние стола, где очередь стоит на герое; у героя при желании состояние и своё HP."""
    state = table.state.model_copy(deep=True)
    state.combat.turn_index = state.combat.order.index(hero_id)
    hero = state.party[hero_id]
    if condition is not None:
        hero.conditions.append(ActiveCondition(id=condition))
    if hp is not None:
        hero.hp = hp
    return state


def fighter_in(state, srd, adventure, fighter_id: str) -> dict[str, Any]:
    order = combat_view(state, srd, adventure)["order"]
    return next(fighter for fighter in order if fighter["id"] == fighter_id)


def test_a_hero_on_his_feet_can_act_and_the_flag_is_a_plain_bool(srd, table):
    for seat in (table.alice, table.boris):
        # У Алисы «отравлен», у Бориса «помог»: помеха на атаки не отнимает ход.
        assert fighter_in(table.state, srd, table.adventure, seat.character.id)["can_act"] is True


@pytest.mark.parametrize("condition", INCAPACITATING)
def test_a_hero_with_an_incapacitating_condition_cannot_act(srd, table, condition):
    boris = table.boris.character.id
    state = on_turn(table, boris, condition=condition)

    assert fighter_in(state, srd, table.adventure, boris)["can_act"] is False


@pytest.mark.parametrize("condition", STILL_ABLE)
def test_a_hero_with_a_condition_that_leaves_the_turn_to_him_can_act(srd, table, condition):
    boris = table.boris.character.id
    state = on_turn(table, boris, condition=condition)

    assert fighter_in(state, srd, table.adventure, boris)["can_act"] is True


@pytest.mark.parametrize("condition", [None, "unconscious"], ids=["только 0 HP", "и без сознания"])
def test_a_hero_on_zero_hit_points_cannot_act(srd, table, condition):
    alice = table.alice.character.id
    state = on_turn(table, alice, condition=condition, hp=0)

    assert fighter_in(state, srd, table.adventure, alice)["can_act"] is False


def test_the_flag_belongs_to_each_hero_and_not_to_the_one_on_turn(srd, table):
    boris, alice = table.boris.character.id, table.alice.character.id
    state = on_turn(table, boris, condition="paralyzed")  # очередь стоит на парализованном Борисе

    assert fighter_in(state, srd, table.adventure, boris)["can_act"] is False
    assert fighter_in(state, srd, table.adventure, alice)["can_act"] is True


@pytest.mark.parametrize("hero", ["alice", "boris"])
@pytest.mark.parametrize("hp", [None, 0], ids=["здоров", "лежит"])
@pytest.mark.parametrize("condition", [None, *INCAPACITATING, *STILL_ABLE])
def test_the_flag_is_the_very_rule_by_which_the_server_waits_for_the_hero(
    srd, table, hero, hp, condition
):
    hero_id = table.seats[hero].character.id
    state = on_turn(table, hero_id, condition=condition, hp=hp)

    shown = fighter_in(state, srd, table.adventure, hero_id)["can_act"]

    # `waiting_hero` решает, ждать ли ход героя, принимать ли его «Готов» и когда звать мастера.
    waits = waiting_hero(state)
    assert shown == (waits is not None and waits.id == hero_id)


def test_monsters_get_no_flag_whatever_their_state(srd, table):
    state = table.state.model_copy(deep=True)
    state.combat.combatants["goblin_warrior-1"].conditions.append(ActiveCondition(id="paralyzed"))

    order = combat_view(state, srd, table.adventure)["order"]

    monsters = [fighter for fighter in order if fighter["kind"] == "monster"]
    assert {"goblin_warrior-1", "goblin_warrior-4"} <= {m["id"] for m in monsters}  # сражённый тоже
    assert all("can_act" not in monster and set(monster) == MONSTER_KEYS for monster in monsters)


def test_a_hidden_hero_who_is_able_to_act_is_still_listed_as_able(srd, table):
    boris = table.boris.character.id
    state = with_hidden(table, boris)

    assert fighter_in(state, srd, table.adventure, boris)["can_act"] is True


def test_a_fighter_whose_hero_is_not_in_the_party_cannot_act_and_does_not_break_the_view(
    srd, table
):
    state = table.state.model_copy(deep=True)
    boris = table.boris.character.id
    del state.party[boris]  # рассинхрон состояния: боец есть, героя в отряде нет

    fighter = fighter_in(state, srd, table.adventure, boris)

    assert fighter["can_act"] is False and fighter["hp"] is None


def test_the_flag_reaches_the_clients_in_the_combat_event_too(srd, table):
    boris = table.boris.character.id
    state = on_turn(table, boris, condition="paralyzed")

    event = combat_event(state, srd, table.adventure)
    deliveries = [
        p for p, audience in state_events(state, srd, table.adventure) if audience == "all"
    ]

    assert fighter_in(state, srd, table.adventure, boris)["can_act"] is False
    assert event in deliveries
    (boris_in_event,) = [f for f in event["combat"]["order"] if f["id"] == boris]
    assert boris_in_event["can_act"] is False


async def test_the_snapshot_tells_every_player_which_hero_cannot_act(db, srd, table):
    boris = table.boris.character.id
    state = on_turn(table, boris, condition="paralyzed").model_copy(update={"version": 4})
    async with db() as session:
        session.add(GameStateSnapshot.from_state(state))
        (await session.get(Game, table.game_id)).state_version = 4
        await session.commit()

    for key in PLAYER_KEYS:
        snapshot = await snapshot_of(db, srd, table, key)
        flags = {f["id"]: f["can_act"] for f in snapshot["combat"]["order"] if f["kind"] == "pc"}
        assert flags == {boris: False, table.alice.character.id: True}, key


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


async def test_a_player_who_has_no_hero_yet_gets_a_snapshot_with_no_character(db, srd, lobby_table):
    # Только что вошедший в лобби игрок ещё не создал героя: снимок строится, а листа в нём нет.
    seat = lobby_table.seats["alice"]
    async with db() as session:
        player = await session.get(GamePlayer, (lobby_table.game_id, seat.user_id))
        hero_id, player.character_id = player.character_id, None
        await session.flush()
        await session.execute(delete(PlayerCharacter).where(PlayerCharacter.id == hero_id))
        await session.commit()

    snapshot = await snapshot_of(db, srd, lobby_table, "alice")

    assert snapshot["character"] is None
    assert [p["has_character"] for p in snapshot["game"]["players"]].count(False) == 1


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
    deliveries = state_events(table.state, srd, table.adventure)

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
    for payload, audience in state_events(table.state, srd, table.adventure):
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
    combat = combat_event(table.state, srd, table.adventure)["combat"]
    assert combat["current_id"] == table.boris.character.id
    ended = table.state.model_copy(update={"combat": None})
    assert combat_event(ended, srd, table.adventure) == {"type": "combat", "combat": None}


def test_character_without_an_owner_gets_no_sheet_event(srd, table):
    orphan = table.alice.character.model_copy(update={"player_id": None})
    state = table.state.model_copy(update={"party": {orphan.id: orphan}})

    assert [p["type"] for p, _ in state_events(state, srd, table.adventure)] == ["party", "combat"]
    assert character_event(orphan, srd, table.adventure, state)["character"]["id"] == orphan.id


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
    sheet = character_event(table.alice.character, srd, table.adventure, table.state)["character"]

    assert sheet["conditions"] == [{"id": "poisoned", "name": "Отравлен", "rounds_left": 3}]
    assert sheet["buffs"] == [{"id": "bless", "name": "Благословение", "rounds_left": 5}]
    assert sheet["conditions"] == party_event(table.state, srd)["party"][0]["conditions"]
    assert sheet["buffs"] == party_event(table.state, srd)["party"][0]["effects"]


def test_the_rest_of_the_sheet_is_still_the_whole_character(srd, table):
    hero = table.alice.character

    sheet = character_event(hero, srd, table.adventure, table.state)["character"]

    whole = hero.model_dump(mode="json")
    rest = whole.keys() - {"conditions", "buffs"}
    # Ни одно поле персонажа не пропало; к ним добавлен только блок чисел движка (`derived`).
    assert sheet.keys() == whole.keys() | {"derived"}
    assert {key: sheet[key] for key in rest} == {key: whole[key] for key in rest}
    assert sheet["persona"]["backstory"] == "ПРЕДЫСТОРИЯ-alice"
    assert sheet["inspiration"] is True


def test_no_event_of_the_state_carries_the_source_of_a_state_or_an_effect(srd, table):
    for payload, _ in state_events(table.state, srd, table.adventure):
        assert not {"source", "source_id"} & set(keys_of(payload)), payload["type"]
    # Источник «отравлен» у Алисы.
    assert "goblin-1" not in dump(state_events(table.state, srd, table.adventure))


def test_the_sheet_event_of_a_hero_snared_by_a_hidden_monster_never_names_the_monster(srd, table):
    state = snared_by_a_hidden_monster(table)

    deliveries = state_events(state, srd, table.adventure)

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


# --- числа листа, посчитанные движком (план B8.5, фаза B8.s1) ---
# Клиент ничего не считает (инвариант «а»): КД, бонусы навыков и спасбросков, пассивная
# внимательность, Сл и бонус атаки заклинаний и атаки оружием приходят готовыми в `derived`.

DERIVED_KEYS = {
    "armor_class",
    "proficiency_bonus",
    "passive_perception",
    "abilities",
    "skills",
    "spell_save_dc",
    "spell_attack_bonus",
    "attacks",
}
ABILITY_ROW_KEYS = {"id", "name", "modifier", "save_bonus"}
SKILL_ROW_KEYS = {"id", "name", "ability", "bonus"}
ATTACK_KEYS = {"item_id", "name", "equipped", "bonus", "damage", "damage_type", "range", "ability"}


def derived_of(hero, srd) -> dict[str, Any]:
    return character_view(hero, srd, DEMO_ADVENTURE, None)["derived"]


@pytest.mark.parametrize("key", PLAYER_KEYS)
def test_the_sheet_has_the_numbers_the_engine_counted_for_the_hero(srd, table, key):
    hero = table.seats[key].character

    derived = derived_of(hero, srd)

    assert set(derived) == DERIVED_KEYS
    assert derived["armor_class"] == armor_class(hero, srd)
    assert derived["proficiency_bonus"] == prof_bonus(hero)
    assert derived["passive_perception"] == passive_perception(hero)
    assert derived["spell_save_dc"] == spell_save_dc(hero, srd)
    assert derived["spell_attack_bonus"] == spell_attack_bonus(hero, srd)


@pytest.mark.parametrize("key", PLAYER_KEYS)
def test_every_ability_has_its_modifier_and_saving_throw_bonus_in_the_engine_order(srd, table, key):
    hero = table.seats[key].character

    rows = derived_of(hero, srd)["abilities"]

    assert [row["id"] for row in rows] == [ability.value for ability in Ability]
    for row in rows:
        ability = Ability(row["id"])
        assert set(row) == ABILITY_ROW_KEYS
        assert row["name"] == ABILITY_NAMES_RU[ability]
        assert row["modifier"] == ability_mod(hero, ability)
        assert row["save_bonus"] == save_bonus(hero, ability)


@pytest.mark.parametrize("key", PLAYER_KEYS)
def test_every_skill_has_its_bonus_name_and_ability_in_the_engine_order(srd, table, key):
    hero = table.seats[key].character

    rows = derived_of(hero, srd)["skills"]

    assert [row["id"] for row in rows] == [skill.value for skill in Skill]
    for row in rows:
        skill = Skill(row["id"])
        assert set(row) == SKILL_ROW_KEYS
        assert row["name"] == SKILL_NAMES_RU[skill]
        assert row["ability"] == SKILL_ABILITY[skill].value
        assert row["bonus"] == skill_bonus(hero, skill)


def test_the_numbers_of_the_sheet_are_the_ones_the_player_sees_in_the_game(srd, table):
    # Опорные значения: боец в кольчуге с двуручным мечом и жрица с булавой и щитом.
    alice = derived_of(table.alice.character, srd)
    vera = derived_of(table.vera.character, srd)

    assert (alice["armor_class"], alice["passive_perception"]) == (17, 13)
    assert (vera["armor_class"], vera["spell_save_dc"], vera["spell_attack_bonus"]) == (16, 13, 5)
    greatsword = next(a for a in alice["attacks"] if a["item_id"] == "greatsword")
    assert (greatsword["bonus"], greatsword["damage"]) == (5, "2d6+3")
    assert greatsword["name"] == "Двуручный меч" and greatsword["equipped"] is True


def test_a_hero_without_spellcasting_has_no_spell_numbers(srd, table):
    derived = derived_of(table.alice.character, srd)  # боец

    assert derived["spell_save_dc"] is None and derived["spell_attack_bonus"] is None


def test_the_bonuses_follow_the_level_of_the_hero(srd, table):
    # Все герои игры 3-го уровня, но число берётся по уровню персонажа, а не зашито.
    veteran = table.alice.character.model_copy(update={"level": 5})

    derived = derived_of(veteran, srd)

    assert derived["proficiency_bonus"] == prof_bonus(veteran) == 3
    athletics = next(row for row in derived["skills"] if row["id"] == Skill.ATHLETICS.value)
    assert athletics["bonus"] == skill_bonus(veteran, Skill.ATHLETICS)
    strike = derived["attacks"][0]
    assert strike["bonus"] == weapon_attack(veteran, strike["item_id"], srd).bonus


def test_the_attacks_are_the_weapons_of_the_inventory_counted_by_the_engine(srd, table):
    hero = table.alice.character

    attacks = derived_of(hero, srd)["attacks"]

    weapons = [e for e in hero.inventory if srd.items[e.item_id].weapon is not None]
    assert [a["item_id"] for a in attacks] == [e.item_id for e in weapons]  # ни брони, ни трав
    for attack, entry in zip(attacks, weapons, strict=True):
        profile = weapon_attack(hero, entry.item_id, srd)
        assert set(attack) == ATTACK_KEYS
        assert attack["equipped"] is entry.equipped
        assert (attack["name"], attack["bonus"], attack["damage"]) == (
            profile.name,
            profile.bonus,
            profile.damage,
        )
        assert (attack["damage_type"], attack["range"], attack["ability"]) == (
            profile.damage_type,
            profile.range,
            profile.ability.value,
        )
    assert {a["equipped"] for a in attacks} == {True, False}  # надетое отличимо от запасного


def test_an_item_that_is_not_a_weapon_of_the_srd_is_not_an_attack(srd, table):
    hero = table.alice.character.model_copy(deep=True)
    hero.inventory.append(InventoryItem(item_id="silver-key-of-the-crypt"))  # id вне SRD

    attacks = derived_of(hero, srd)["attacks"]

    assert "silver-key-of-the-crypt" not in [a["item_id"] for a in attacks]
    assert [a["item_id"] for a in attacks] == [
        a["item_id"] for a in derived_of(table.alice.character, srd)["attacks"]
    ]


def test_the_numbers_follow_the_live_hero_and_not_a_copy_made_earlier(srd, table):
    # КД и бонусы считаются по тому герою, что передан: снял доспех или получил эффект — число
    # другое. Закешированного значения нет.
    hero = table.alice.character.model_copy(deep=True)
    armored = derived_of(hero, srd)["armor_class"]
    for entry in hero.inventory:
        entry.equipped = False

    unarmored = derived_of(hero, srd)["armor_class"]

    assert unarmored == armor_class(hero, srd)
    assert unarmored < armored


def test_a_buff_that_changes_the_armor_class_changes_the_number_of_the_sheet(srd):
    mage = quick_character(
        "wizard", "elf", srd, name="Маг", concept="Книжник", character_id=new_character_id()
    )
    plain = derived_of(mage, srd)["armor_class"]
    warded = mage.model_copy(
        update={"buffs": [ActiveBuff(id="mage_armor", rounds_left=None, source_id=mage.id)]}
    )

    shown = derived_of(warded, srd)["armor_class"]

    assert shown == armor_class(warded, srd)
    assert shown > plain


async def test_my_character_in_the_snapshot_has_the_numbers_of_the_engine(db, srd, table):
    snapshot = await snapshot_of(db, srd, table, "vera")

    derived = snapshot["character"]["derived"]
    assert (
        derived
        == character_view(table.vera.character, srd, table.adventure, table.state)["derived"]
    )
    assert derived["spell_save_dc"] == 13


async def test_a_hero_in_the_lobby_has_the_numbers_before_the_game_starts(db, srd, lobby_table):
    snapshot = await snapshot_of(db, srd, lobby_table, "alice")

    derived = snapshot["character"]["derived"]
    assert derived["armor_class"] == armor_class(lobby_table.alice.character, srd)


def test_the_numbers_of_a_hero_go_to_his_owner_alone(srd, table):
    owners = {seat.character.id: seat.user_id for seat in table.seats.values()}
    deliveries = state_events(table.state, srd, table.adventure)

    for payload, audience in deliveries:
        if "derived" in set(keys_of(payload)):
            assert payload["type"] == "character"
            assert audience == owners[payload["character"]["id"]]
    shared = (party_event(table.state, srd), combat_event(table.state, srd, table.adventure))
    for block in shared:
        assert not {"derived", "armor_class", "attacks"} & set(keys_of(block))


async def test_the_snapshot_of_a_player_has_no_numbers_of_the_sheets_of_others(db, srd, table):
    snapshot = await snapshot_of(db, srd, table, "boris")

    assert snapshot["character"]["id"] == table.boris.character.id
    # Отряд и бой — общие блоки: в них нет ни КД, ни атак, ни блока чисел чужого листа.
    for block in ("party", "combat"):
        assert not {"derived", "armor_class", "attacks"} & set(keys_of(snapshot[block])), block


# --- предметы приключения в листе: то, что держит герой (общая проверка B8, fix 5) ---
# Предметы приключения движок не кладёт в `Character.inventory`: кто их держит, говорит только
# `world.item_holders` (`give_item`, `take_item`, расход в бою). Лист героя строится по нему, как
# контекст мастера: у предмета, который держит герой, в `inventory` есть строка с публичным
# названием из копии приключения игры. Предметы, лежащие в мире, у NPC, у других героев и ушедшие
# (`gone`), в лист не попадают; описаний, держателей и заметок мастера в листе нет. Состояние
# в тестах меняет мастер настоящими инструментами движка, а не правка листа или `item_holders`.

KEY_NAME = "Запасной ключ Ульриха"
POTION_NAME = "Зелье лечения"
SRD_LINE_KEYS = {"item_id", "qty", "equipped"}
ADVENTURE_LINE_KEYS = SRD_LINE_KEYS | {"name"}


def give(item_id: str, hero) -> tuple[str, dict[str, Any]]:
    return "give_item", {"item_id": item_id, "character_id": hero.id}


def take(item_id: str, hero, to: str) -> tuple[str, dict[str, Any]]:
    return "take_item", {"item_id": item_id, "character_id": hero.id, "to": to}


def drink(item_id: str, hero) -> tuple[str, dict[str, Any]]:
    return "combat_action", {"actor_id": hero.id, "action": "use_item", "item_id": item_id}


def master_turn(
    srd,
    table,
    *calls: tuple[str, dict[str, Any]],
    state: GameState | None = None,
    at: str = "village",
    acting: tuple[str, ...] = (),
    adventure: Adventure | None = None,
) -> GameState:
    """Ход мастера: настоящие инструменты движка поверх копии состояния (по умолчанию стола).

    Отряд стоит в локации `at` (Марта с ключом в деревне, зелье в сундуке на кладбище). Отказ
    инструмента — ошибка теста: проверяется путь, который проходит настоящая игра."""
    state = (state or table.state).model_copy(deep=True)
    state.location_id = at
    rules = build_ruleset(state.rules)
    ctx = ToolContext(
        state=state,
        adventure=adventure or table.adventure,
        content=srd,
        rules=rules,
        rng=SeededRng(1),
        acting=acting,
    )
    registry = build_registry(rules)
    for name, args in calls:
        outcome = registry.call(ctx, name, args)
        assert outcome.error is None, outcome
    return state


def sheets_of(state: GameState, srd, table, adventure: Adventure | None = None):
    """Листы так, как их получают владельцы в событии `character` после хода: id героя -> лист."""
    deliveries = state_events(state, srd, adventure or table.adventure)
    return {p["character"]["id"]: p["character"] for p, _ in deliveries if p["type"] == "character"}


def lines_of(sheet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {line["item_id"]: line for line in sheet["inventory"]}


def with_both_items(srd, table, hero) -> GameState:
    """Ключ у героя, сундук на кладбище открыт ключом, и зелье из него тоже у героя."""
    keyed = master_turn(srd, table, give("chest-key", hero))
    opened = [("reveal", {"ref": "feature:chest"}), give("healing-potion", hero)]
    return master_turn(srd, table, *opened, state=keyed, at="cemetery")


def test_an_item_the_master_gives_to_a_hero_is_a_line_of_his_sheet_with_the_public_name(srd, table):
    alice = table.alice.character
    state = master_turn(srd, table, give("chest-key", alice))

    sheet = sheets_of(state, srd, table)[alice.id]

    assert sheet["inventory"][-1] == {
        "item_id": "chest-key",
        "qty": 1,
        "equipped": False,
        "name": KEY_NAME,
    }
    # Снаряжение SRD идёт первым и без изменений: названия у него нет.
    kit = [entry.model_dump(mode="json") for entry in alice.inventory]
    assert sheet["inventory"][:-1] == kit
    assert all(set(line) == SRD_LINE_KEYS for line in kit)


def test_the_item_is_in_the_sheet_of_its_holder_alone(srd, table):
    boris = table.boris.character
    state = master_turn(srd, table, give("chest-key", boris))

    sheets = sheets_of(state, srd, table)

    assert [cid for cid, sheet in sheets.items() if "chest-key" in lines_of(sheet)] == [boris.id]
    for other in (table.alice.character, table.vera.character):
        assert "chest-key" not in dump(sheets[other.id]) and KEY_NAME not in dump(sheets[other.id])
    shared = [
        payload
        for payload, audience in state_events(state, srd, table.adventure)
        if audience == "all"
    ]
    assert "chest-key" not in dump(shared) and KEY_NAME not in dump(shared)


def test_an_item_the_master_passes_to_another_hero_moves_to_his_sheet(srd, table):
    alice, boris = table.alice.character, table.boris.character
    given = master_turn(srd, table, give("chest-key", alice))

    passed = master_turn(srd, table, give("chest-key", boris), state=given)

    sheets = sheets_of(passed, srd, table)
    assert "chest-key" not in lines_of(sheets[alice.id])
    assert lines_of(sheets[boris.id])["chest-key"]["name"] == KEY_NAME


@pytest.mark.parametrize("fate", ["gone", "location"])
def test_an_item_the_master_takes_from_the_hero_leaves_his_sheet(srd, table, fate):
    alice = table.alice.character
    given = master_turn(srd, table, give("chest-key", alice))
    assert "chest-key" in lines_of(sheets_of(given, srd, table)[alice.id])

    taken = master_turn(srd, table, take("chest-key", alice, fate), state=given)

    sheet = sheets_of(taken, srd, table)[alice.id]
    assert "chest-key" not in dump(sheet) and KEY_NAME not in dump(sheet)
    assert sheet["inventory"] == [e.model_dump(mode="json") for e in alice.inventory]


def test_a_hero_whose_id_continues_the_id_of_another_does_not_get_his_items(srd, table):
    short = table.alice.character.model_copy(update={"id": "pc_1"})
    long = table.boris.character.model_copy(update={"id": "pc_10"})
    party = table.state.model_copy(
        update={"party": {short.id: short, long.id: long}, "combat": None}
    )

    state = master_turn(srd, table, give("chest-key", long), state=party)

    sheets = sheets_of(state, srd, table)
    assert "chest-key" in lines_of(sheets["pc_10"])
    assert "chest-key" not in dump(sheets["pc_1"])


def test_a_potion_drunk_in_the_battle_leaves_the_sheet_and_the_key_stays(srd, table):
    boris = table.boris.character  # в бою ходит Борис
    holding = with_both_items(srd, table, boris)
    held = lines_of(sheets_of(holding, srd, table)[boris.id])
    assert held["healing-potion"] == {
        "item_id": "healing-potion",
        "qty": 1,
        "equipped": False,
        "name": POTION_NAME,
    }

    drunk = master_turn(
        srd, table, drink("healing-potion", boris), state=holding, at="cemetery", acting=(boris.id,)
    )

    after = lines_of(sheets_of(drunk, srd, table)[boris.id])
    assert "healing-potion" not in after and POTION_NAME not in dump(after)
    assert after["chest-key"]["name"] == KEY_NAME


def test_the_items_follow_the_srd_gear_in_the_order_of_their_ids(srd, table):
    alice = table.alice.character
    state = with_both_items(srd, table, alice)

    sheet = sheets_of(state, srd, table)[alice.id]

    kit = [entry.item_id for entry in alice.inventory]
    assert [line["item_id"] for line in sheet["inventory"]] == [*kit, "chest-key", "healing-potion"]
    named = [line for line in sheet["inventory"] if "name" in line]
    assert [set(line) for line in named] == [ADVENTURE_LINE_KEYS] * 2


def test_items_that_lie_in_the_world_or_belong_to_an_npc_are_in_no_sheet(srd, table):
    boris = table.boris.character
    # Ключ у Марты, зелье в сундуке на кладбище; ключ, оставленный в деревне, тоже лежит в мире.
    dropped = master_turn(
        srd, table, give("chest-key", boris), take("chest-key", boris, "location")
    )

    for state in (table.state, dropped):
        for sheet in sheets_of(state, srd, table).values():
            assert all("name" not in line for line in sheet["inventory"])


def test_the_name_is_all_the_sheet_takes_from_the_items_of_the_adventure(srd, table):
    key = table.adventure.items["chest-key"]
    trapped = table.adventure.model_copy(
        update={
            "items": {
                **table.adventure.items,
                "chest-key": key.model_copy(update={"description": f"{SECRET_MARK}описание"}),
            }
        }
    )
    alice = table.alice.character
    state = with_both_items(srd, table, alice)

    sheet = sheets_of(state, srd, table, trapped)[alice.id]

    text = dump(sheet)
    assert SECRET_MARK not in text and "Маленький железный ключ" not in text
    assert "Красная жидкость" not in text and "приписал на этикетке" not in text
    assert "npc:marta" not in text and "location:cemetery" not in text  # держатели предметов
    assert "potion_of_healing" not in text  # предмет SRD, на котором держится механика зелья
    assert not {"description", "holder", "base_item"} & set(keys_of(sheet))
    assert {tuple(sorted(line)) for line in sheet["inventory"] if "name" in line} == {
        tuple(sorted(ADVENTURE_LINE_KEYS))
    }


def test_the_names_reach_only_the_owner_in_the_character_event(srd, table):
    state = master_turn(srd, table, give("chest-key", table.alice.character))

    deliveries = state_events(state, srd, table.adventure)

    named = [(p, audience) for p, audience in deliveries if KEY_NAME in dump(p)]
    assert [(p["type"], audience) for p, audience in named] == [("character", table.alice.user_id)]
    assert lines_of(named[0][0]["character"])["chest-key"]["name"] == KEY_NAME


def test_the_event_after_the_item_has_left_the_hero_carries_no_trace_of_it(srd, table):
    alice = table.alice.character
    given = master_turn(srd, table, give("chest-key", alice))
    taken = master_turn(srd, table, take("chest-key", alice, "gone"), state=given)

    assert KEY_NAME in dump(state_events(given, srd, table.adventure))
    assert KEY_NAME not in dump(state_events(taken, srd, table.adventure))


def test_the_sheet_lists_the_same_items_as_the_context_of_the_master(srd, table):
    # Мастеру в контексте и игроку в листе называет предметы один и тот же вопрос: кто держит.
    state = with_both_items(srd, table, table.alice.character)
    state = master_turn(
        srd, table, give("chest-key", table.boris.character), state=state, at="cemetery"
    )
    sheets = sheets_of(state, srd, table)

    for hero in state.party.values():
        named = [line for line in sheets[hero.id]["inventory"] if "name" in line]
        shown = ", ".join(f"{line['item_id']} («{line['name']}»)" for line in named)
        assert shown == _carried_adventure_items(hero, state, table.adventure), hero.name
    extra = [len(sheets[c.id]["inventory"]) - len(c.inventory) for c in state.party.values()]
    assert extra == [1, 1, 0]  # у Алисы зелье, у Бориса ключ, у Веры ничего


async def save_state(db, table, state: GameState) -> None:
    """Состояние после хода мастера как новая версия игры (то, что читает снимок)."""
    async with db() as session:
        state = state.model_copy(update={"version": state.version + 1})
        session.add(GameStateSnapshot.from_state(state))
        (await session.get(Game, table.game_id)).state_version = state.version
        await session.commit()


async def test_the_snapshot_has_the_same_sheet_as_the_event_after_the_turn(db, srd, table):
    alice = table.alice.character
    state = with_both_items(srd, table, alice)
    await save_state(db, table, state)

    snapshots = {key: await snapshot_of(db, srd, table, key) for key in PLAYER_KEYS}

    sheets = sheets_of(state, srd, table)
    for key in PLAYER_KEYS:
        seat = table.seats[key]
        assert snapshots[key]["character"] == sheets[seat.character.id], key
    assert KEY_NAME in dump(snapshots["alice"]) and POTION_NAME in dump(snapshots["alice"])
    for key in ("boris", "vera"):
        assert "chest-key" not in dump(snapshots[key]) and KEY_NAME not in dump(snapshots[key])


async def test_the_snapshot_names_the_items_from_the_copy_of_the_adventure_of_the_game(
    db, srd, table
):
    await save_state(db, table, master_turn(srd, table, give("chest-key", table.alice.character)))
    async with db() as session:
        game = await session.get(Game, table.game_id)
        # Копия игры, а не каталог: название в ней другое, и лист называет предмет по копии.
        data = copy.deepcopy(game.adventure_data)  # новый объект: иначе SQLAlchemy не увидит правки
        data["items"]["chest-key"]["name"] = "Ключ из копии игры"
        game.adventure_data = data
        await session.commit()

    alice = await snapshot_of(db, srd, table, "alice")
    boris = await snapshot_of(db, srd, table, "boris")

    assert lines_of(alice["character"])["chest-key"]["name"] == "Ключ из копии игры"
    assert "chest-key" not in dump(boris) and "Ключ из копии игры" not in dump(boris)
    assert SECRET_MARK not in dump(alice)


async def test_the_lobby_sheet_has_the_items_of_the_srd_without_names(db, srd, lobby_table):
    snapshot = await snapshot_of(db, srd, lobby_table, "alice")

    lines = snapshot["character"]["inventory"]
    assert lines and all(set(line) == SRD_LINE_KEYS for line in lines)


# --- сообщения ленты из событий хода мастера (B7.3) ---


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return DEMO_ADVENTURE


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


@pytest.mark.parametrize("label", ["Урон: меч", "Инициатива", "Лечение: Лечение ран"])
def test_a_roll_without_an_outcome_has_no_hit_or_miss_in_its_text(table, srd, adventure, label):
    # Урон, инициатива и лечение ничего не «проваливают»: исхода у них нет ни в данных, ни в тексте.
    event = RollEvent(
        actor_id=table.alice.character.id, label=label, roll=dice_roll("1d8+3"), success=None
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["success"] is None
    assert entry.text == f"Алиса: {label} — {event.roll.total}"
    for word in ("успех", "провал", "попадание", "промах"):
        assert word not in entry.text


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


# --- КД героя в карточке атаки (решение владельца R2, фаза B8.s1) ---
# Монстр бьёт только героя, а герой — только монстра: цель атаки определяет сторона бойца. КД
# героя игрокам известен, и карточка атаки монстра называет его; КД монстров скрыт всегда.

HERO_AC = 16
MONSTER_AC = 77  # такого числа нет ни в кубах карточки, ни в чьём-либо тексте


def numbers_of(value: Any) -> Iterator[int]:
    """Все целые числа данных карточки на любой глубине. Строки не в счёт: id персонажа — это
    случайные цифры, и число КД в нём бывает случайно."""
    if isinstance(value, dict):
        for inner in value.values():
            yield from numbers_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from numbers_of(inner)
    elif isinstance(value, int):
        yield value


def assert_no_armor_class_of_the_monster(entries: list[FeedEntry]) -> None:
    for entry in entries:
        assert "target_ac" not in entry.data
        assert "КД" not in entry.text and str(MONSTER_AC) not in entry.text
        assert MONSTER_AC not in set(numbers_of(entry.data))


@pytest.mark.parametrize("success", [True, False], ids=["попадание", "промах"])
def test_the_attack_card_of_a_monster_names_the_armor_class_of_the_hero_it_attacks(
    table, srd, adventure, success
):
    event = RollEvent(
        actor_id="goblin_warrior-1",
        label="Атака: ятаган",
        roll=dice_roll(),
        target_ac=HERO_AC,
        success=success,
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.data["target_ac"] == HERO_AC
    assert entry.data["success"] is success
    assert f"против КД {HERO_AC}" in entry.text
    assert ("попадание" if success else "промах") in entry.text
    assert entry.data["actor"] == "Гоблин 1"


@pytest.mark.parametrize("success", [True, False], ids=["попадание", "промах"])
def test_the_attack_card_of_a_hero_never_gives_the_armor_class_of_the_monster(
    table, srd, adventure, success
):
    event = RollEvent(
        actor_id=table.alice.character.id,
        label="Атака: длинный меч",
        roll=dice_roll(),
        target_ac=MONSTER_AC,
        success=success,
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert_no_armor_class_of_the_monster([entry])
    assert ("попадание" if success else "промах") in entry.text  # итог игрок всё равно видит


def test_a_secret_attack_of_a_hero_does_not_give_the_armor_class_of_the_monster_either(
    table, srd, adventure
):
    alice = table.alice.character.id
    event = RollEvent(
        audience=alice,
        actor_id=alice,
        label="Атака: длинный меч",
        roll=dice_roll(),
        target_ac=MONSTER_AC,
        success=True,
    )

    (entry,) = entries_of(table, srd, adventure, event, secret={alice})

    assert entry.audience == table.alice.user_id
    assert_no_armor_class_of_the_monster([entry])


def test_in_one_turn_only_the_attack_on_the_hero_has_an_armor_class(table, srd, adventure):
    alice = table.alice.character.id
    events = [
        RollEvent(actor_id=alice, label="Атака: меч", roll=dice_roll(), target_ac=MONSTER_AC),
        RollEvent(
            actor_id="goblin_warrior-1", label="Атака: ятаган", roll=dice_roll(), target_ac=HERO_AC
        ),
        RollEvent(actor_id=alice, label="Атака: меч", roll=dice_roll(), target_ac=MONSTER_AC),
    ]

    entries = entries_of(table, srd, adventure, *events)

    assert ["target_ac" in entry.data for entry in entries] == [False, True, False]
    assert_no_armor_class_of_the_monster([entries[0], entries[2]])
    assert entries[1].data["target_ac"] == HERO_AC


def test_a_hidden_monster_attacks_a_hero_under_a_mask_but_the_armor_class_of_the_hero_is_named(
    table, srd, adventure
):
    hidden = with_hidden(table, HIDDEN_MONSTER)
    event = monster_roll(target_ac=HERO_AC, success=True)

    (entry,) = entries_of(table, srd, adventure, event, before=hidden, after=hidden)

    assert entry.data["target_ac"] == HERO_AC
    assert entry.data["actor"] == SOMEBODY_INVISIBLE and "actor_id" not in entry.data
    for trace in MONSTER_TRACES:
        assert trace not in entry.text + dump(entry.data), trace


def test_a_monster_that_left_the_combat_in_this_turn_still_hits_a_hero_with_a_known_target(
    table, srd, adventure
):
    # Скелет бил героя, а потом бой кончился: `after` без боя, но боец ещё в `before`.
    ended = table.state.model_copy(update={"combat": None})
    event = RollEvent(
        actor_id="goblin_warrior-1", label="Атака: ятаган", roll=dice_roll(), target_ac=HERO_AC
    )

    (entry,) = entries_of(table, srd, adventure, event, before=table.state, after=ended)

    assert entry.data["target_ac"] == HERO_AC


@pytest.mark.parametrize("actor_id", ["fabian", "no-such-fighter"], ids=["NPC", "неизвестный"])
def test_an_attacker_that_is_not_a_known_monster_gets_no_armor_class_in_its_card(
    table, srd, adventure, actor_id
):
    # Кто цель, по такому бросающему не понять: КД не называется (лучше без числа, чем чужой КД).
    event = RollEvent(actor_id=actor_id, label="Атака", roll=dice_roll(), target_ac=MONSTER_AC)

    entries = entries_of(table, srd, adventure, event)

    assert_no_armor_class_of_the_monster(entries)


def test_a_check_with_a_difficulty_still_says_difficulty_and_no_armor_class(table, srd, adventure):
    event = RollEvent(
        actor_id="goblin_warrior-1", label="Спасбросок", roll=dice_roll(), dc=12, success=False
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert "Сл 12" in entry.text and "КД" not in entry.text
    assert "target_ac" not in entry.data


# --- карточка бойца-монстра, за которым стоит NPC: то же имя, что в порядке хода боя ---


def cultist_strike() -> RollEvent:
    return RollEvent(
        actor_id=FABIAN_FIGHTER,
        label="Атака: Клинок договора",
        roll=dice_roll(),
        target_ac=HERO_AC,
        success=False,
    )


def test_the_card_of_a_monster_that_is_an_npc_is_under_the_name_of_the_npc(table, srd, adventure):
    state = with_cultist(table)

    (entry,) = entries_of(table, srd, adventure, cultist_strike(), before=state, after=state)

    assert entry.data["actor"] == "Фабиан" and entry.text.startswith("Фабиан: Атака: ")
    # id бойца остаётся (по нему клиент находит бойца в порядке хода), id NPC в карточке нет.
    assert entry.data["actor_id"] == FABIAN_FIGHTER
    assert "fabian" not in dump(entry.data) and "Фанатик" not in entry.text + dump(entry.data)
    assert entry.data["target_ac"] == HERO_AC


def test_the_card_of_a_monster_without_a_sure_link_is_under_the_name_of_its_statblock(
    table, srd, adventure
):
    state = with_cultist(table, location="cemetery")

    (entry,) = entries_of(table, srd, adventure, cultist_strike(), before=state, after=state)

    assert entry.data["actor"] == "Фанатик культа" and "Фабиан" not in entry.text


def test_the_card_keeps_the_name_of_the_npc_when_the_fight_ends_in_the_same_turn(
    table, srd, adventure
):
    state = with_cultist(table)
    ended = state.model_copy(update={"combat": None})

    (entry,) = entries_of(table, srd, adventure, cultist_strike(), before=state, after=ended)

    assert entry.data["actor"] == "Фабиан"


def test_the_card_of_a_hidden_monster_that_is_an_npc_names_neither_the_npc_nor_the_statblock(
    table, srd, adventure
):
    hidden = with_hidden(table, FABIAN_FIGHTER, state=with_cultist(table))

    (entry,) = entries_of(table, srd, adventure, cultist_strike(), before=hidden, after=hidden)

    assert entry.data["actor"] == SOMEBODY_INVISIBLE and "actor_id" not in entry.data
    for trace in ("Фабиан", "Фанатик", "fabian", "cultist"):
        assert trace not in entry.text + dump(entry.data), trace


# --- причина броска бойца-NPC: настоящие броски движка (B8.review.server.fix1, minor 1) ---
# Причину броска (`data.reason`) пишет движок, и в ней бывает имя бойца по статблоку: «Инициатива:
# Фанатик культа». Боец идёт под именем NPC, поэтому именем статблока причина его выдать не должна:
# как у спрятанного монстра, в карточке остаётся название броска. Тесты берут события у настоящего
# движка, а не пишут причину руками.


def fabian_fight(table, srd, *, location: str = "crypt"):
    """Бой настоящего движка с Фанатиком культа (статблок Фабиана): состояние до и после удара
    монстра и события начала боя и удара. Кубы подобраны так, чтобы монстр ходил первым и попал."""
    fresh = table.state.model_copy(update={"combat": None, "location_id": location})
    started, start_events = start_combat(
        fresh,
        encounter_id=None,
        zones=[Zone(id="gate", name="Ворота")],
        links=[],
        monsters=[(srd.monsters["cultist_fanatic"], "gate")],
        pc_zone="gate",
        rng=ScriptedRng([1, 1, 1, 20]),
        content=srd,
    )
    alice = table.alice.character.id
    started.combat.combatants[FABIAN_FIGHTER].engaged_with = [alice]
    started.combat.combatants[alice].engaged_with = [FABIAN_FIGHTER]
    after, strike_events = monster_action(
        started, FABIAN_FIGHTER, "pact_blade", [alice], rng=ScriptedRng([19, 4, 3, 5]), content=srd
    )
    return started, after, [*start_events, *strike_events]


def roll_cards(entries: list[FeedEntry]) -> list[FeedEntry]:
    """Карточки бросков среди строк ленты (системные строки боя «Начался бой» не в счёт)."""
    return [entry for entry in entries if entry.kind == MessageKind.ROLL]


def fabian_cards(table, srd, adventure, **fight) -> list[FeedEntry]:
    """Карточки бросков самого Фанатика культа за начало боя и его удар."""
    before, after, events = fabian_fight(table, srd, **fight)
    entries = entries_of(table, srd, adventure, *events, before=before, after=after)
    return [card for card in roll_cards(entries) if card.data["actor_id"] == FABIAN_FIGHTER]


def test_the_engine_writes_the_name_of_the_statblock_into_the_reason_of_the_initiative(table, srd):
    # Опора для тестов ниже: причина, которую пишет движок, в самом деле называет статблок.
    _before, _after, events = fabian_fight(table, srd)

    reasons = [
        e.roll.reason for e in events if isinstance(e, RollEvent) and e.label == "Инициатива"
    ]

    assert "Инициатива: Фанатик культа" in reasons


def test_no_card_of_a_monster_that_is_an_npc_names_the_statblock_in_the_reason(
    table, srd, adventure
):
    cards = fabian_cards(table, srd, adventure)

    labels = [card.data["label"] for card in cards]
    assert labels[0] == "Инициатива" and any(label.startswith("Атака") for label in labels)
    assert len(labels) >= 3  # инициатива, атака и урон
    for card in cards:
        assert card.data["actor"] == "Фабиан"
        assert card.data["reason"] == card.data["label"]
        assert "Фанатик" not in card.text + dump(card.data)


def test_the_cards_of_the_other_fighters_keep_the_reason_the_engine_wrote(table, srd, adventure):
    before, after, events = fabian_fight(table, srd)
    cards = roll_cards(entries_of(table, srd, adventure, *events, before=before, after=after))

    initiative = {
        card.data["actor"]: card.data["reason"]
        for card in cards
        if card.data["label"] == "Инициатива"
    }

    assert initiative["Алиса"] == "Инициатива: Алиса"
    assert initiative["Фабиан"] == "Инициатива"


def test_the_reason_of_a_monster_without_a_sure_link_to_an_npc_names_its_statblock_as_the_card_does(
    table, srd, adventure
):
    # Связи с NPC нет (отряд не в его локации): боец идёт под именем статблока и в подписи, и в
    # причине.
    cards = fabian_cards(table, srd, adventure, location="cemetery")

    initiative = next(card for card in cards if card.data["label"] == "Инициатива")
    assert initiative.data["actor"] == "Фанатик культа"
    assert initiative.data["reason"] == "Инициатива: Фанатик культа"


def test_the_reason_stays_clean_when_the_fight_ends_in_the_same_turn(table, srd, adventure):
    before, after, events = fabian_fight(table, srd)
    ended = after.model_copy(update={"combat": None})

    cards = roll_cards(entries_of(table, srd, adventure, *events, before=before, after=ended))

    fabian = [card for card in cards if card.data["actor_id"] == FABIAN_FIGHTER]
    assert len(fabian) >= 3
    for card in fabian:
        assert card.data["actor"] == "Фабиан" and card.data["reason"] == card.data["label"]
        assert "Фанатик" not in card.text + dump(card.data)


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("Скрытность", "Скрытность: Фанатик культа"),
        ("Спасбросок: poisoned", "Спасбросок: poisoned (Фанатик культа)"),
        ("Харизма (Обман)", "Проверка Фанатика культа"),
    ],
    ids=["скрытность", "спасбросок", "проверка мастера"],
)
def test_the_reason_of_any_roll_of_such_a_monster_is_its_label_whatever_the_reason_says(
    table, srd, adventure, label, reason
):
    # Движок вправе назвать бойца в причине любого броска (или модель в причине своей проверки):
    # смотреть на вид броска нельзя, по имени бойца в карточке решает сам боец.
    state = with_cultist(table)
    event = RollEvent(
        actor_id=FABIAN_FIGHTER,
        label=label,
        roll=roll_dice("1d20+2", rng=SeededRng(3), reason=reason),
    )

    (entry,) = entries_of(table, srd, adventure, event, before=state, after=state)

    assert entry.data["actor"] == "Фабиан" and entry.data["reason"] == label
    assert "Фанатик" not in entry.text + dump(entry.data)


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


def test_spent_inspiration_is_a_line_too_and_does_not_promise_a_reason(table, srd, adventure):
    event = InspirationEvent(
        character_id=table.vera.character.id, granted=False, reason="красивая реплика"
    )

    (entry,) = entries_of(table, srd, adventure, event)

    assert entry.text == "Вера тратит вдохновение."
    assert entry.audience == "all"


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
