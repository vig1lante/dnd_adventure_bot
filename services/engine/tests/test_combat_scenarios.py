"""Сквозные сценарии боя блока B3: от `start_combat` до `combat_winner` и привала после боя.

Отдельные функции боя проверяют `test_combat_*.py`; здесь они собираются вместе, как их будет
собирать оркестратор: бросок инициативы, перемещение, атаки, заклинания, смерть и спасброски
от смерти, лечение, конец хода, победа, а затем конец сцены привала модуля `campfire`.

Три раздела:
- рассказ на `ScriptedRng`: каждый куб назван, итог проверяется по правилам, а не по снимку;
- игра до конца на `SeededRng` с простой тактикой за обе стороны: проверяются свойства, которые
  должны держаться при любых бросках (события точно описывают состояние, ни одного скрытого
  куба, отказ ничего не тратит, бой заканчивается, привал возвращает всех живых в строй);
- контракт каждой функции боя: вход не меняется, результат ничего не делит с ним, все выпавшие
  кубы видны в событиях, а тайного бой сам не создаёт.
"""

import hashlib
import inspect
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveBuff, ActiveCondition, Character, InventoryItem
from dnd_engine.combat.engine import (
    CombatError,
    apply_condition,
    apply_damage,
    attack,
    cast_spell,
    combat_winner,
    current_actor,
    end_turn,
    heal_target,
    monster_action,
    move,
    simple_action,
    start_combat,
)
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.conditions import ZERO_HP_SOURCE, is_dead
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.events import (
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    HealEvent,
    PrivateMessageEvent,
    RestEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import Rng, ScriptedRng, SeededRng
from dnd_engine.state import GameState, GameTime, RulesRef, Scene, SceneType

STR, DEX, CON, INT, WIS, CHA = Ability
EVENTS = TypeAdapter(list[Event])
ZONES = [Zone(id="hall", name="Зал"), Zone(id="stairs", name="Лестница")]
LINKS = [ZoneLink(a="hall", b="stairs", distance="near")]
RULES = RulesRef(modules=["bg3_flavor", "campfire"])
HERO, CLERIC = "pc-1", "pc-2"
GOBLIN, GOBLIN_2 = "goblin_warrior-1", "goblin_warrior-2"


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


# --- общие помощники ---


def all_dice_spent(rng: ScriptedRng) -> bool:
    try:
        rng.randint(1, 100)
    except ValueError:
        return True
    return False


class CountingRng:
    """Обёртка над `Rng`, считающая выданные значения: так видны скрытые и лишние кубы."""

    def __init__(self, inner: Rng) -> None:
        self.inner = inner
        self.calls = 0

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return self.inner.randint(a, b)


def dice_shown(events: list[Event]) -> int:
    """Сколько кубов видно игрокам: все кости всех бросков в событиях."""
    return sum(len(e.roll.rolls) for e in events if isinstance(e, RollEvent))


def walk_mutable(value):
    """Каждое упоминание изменяемого объекта (модель, словарь, список) внутри значения: `(id, тип)`.

    Общий объект встречается несколько раз — по этому его и видно."""
    if isinstance(value, BaseModel):
        name, children = type(value).__name__, [getattr(value, f) for f in type(value).model_fields]
    elif isinstance(value, dict):
        name, children = "dict", list(value.values())
    elif isinstance(value, list):
        name, children = "list", value
    else:
        return
    yield id(value), name
    for child in children:
        yield from walk_mutable(child)


def shared_between(first, second) -> list[str]:
    """Изменяемые объекты, которые одновременно лежат в обоих значениях."""
    in_first, in_second = dict(walk_mutable(first)), dict(walk_mutable(second))
    return [in_first[i] for i in in_first.keys() & in_second.keys()]


def aliased_inside(value) -> list[str]:
    """Изменяемые объекты, которые лежат в значении больше чем в одном месте.

    Общая запись состояния у двух бойцов — скрытая ошибка: уменьшение срока у одного уменьшило
    бы его и у другого. После JSON-цикла такой общности нет, поэтому её ищут в памяти."""
    visits = list(walk_mutable(value))
    names = dict(visits)
    return [names[i] for i, count in Counter(i for i, _ in visits).items() if count > 1]


def hit_points(state: GameState, fighter_id: str) -> int:
    if fighter_id in state.party:
        return state.party[fighter_id].hp
    return state.combat.combatants[fighter_id].hp


def hit_points_told_by(events: list[Event]) -> dict[str, int]:
    """HP, какими их оставили события урона и лечения (последнее слово за последним событием)."""
    told: dict[str, int] = {}
    for event in events:
        if isinstance(event, DamageEvent | HealEvent):
            told[event.target_id] = event.hp_after
    return told


def assert_events_describe_the_state(state: GameState, events: list[Event]) -> None:
    for fighter_id, told in hit_points_told_by(events).items():
        assert hit_points(state, fighter_id) == told, fighter_id


def assert_events_are_public_and_valid(events: list[Event]) -> None:
    """Бой сам не делает ничего тайного: броски видят все, личных сообщений нет."""
    assert all(e.audience == "all" for e in events if isinstance(e, RollEvent))
    assert not [e for e in events if isinstance(e, PrivateMessageEvent)]
    assert EVENTS.validate_json(EVENTS.dump_json(events)) == events


def assert_state_is_sane(state: GameState) -> None:
    combat = state.combat
    assert 0 <= combat.turn_index < len(combat.order)
    assert set(combat.order) <= set(combat.combatants)
    for c in state.party.values():
        assert 0 <= c.hp <= c.hp_max, c.id
        assert all(
            0 <= left <= c.spell_slots_max.get(lvl, 0) for lvl, left in c.spell_slots.items()
        )
        assert all(left >= 0 for left in c.resources.values()), c.id
        assert all(entry.qty >= 1 for entry in c.inventory), c.id
    for f in combat.combatants.values():
        assert f.kind == "pc" or 0 <= f.hp <= f.hp_max, f.id
        assert all(f.id in combat.combatants[other].engaged_with for other in f.engaged_with)
    # Номер хода и версию повышает сервер при коммите: движок их не трогает.
    assert (state.turn, state.version) == (0, 0)
    assert aliased_inside(state) == []  # ни одна запись не лежит у двух бойцов сразу
    assert GameState.model_validate_json(state.model_dump_json()) == state


def make_state(*characters: Character, **fields) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RULES,
        party={c.id: c for c in characters},
        location_id="crypt",
        **fields,
    )


def monsters_in(srd: SrdContent, *placements: tuple[str, str]):
    return [(srd.monsters[monster_id], zone) for monster_id, zone in placements]


def go_to_camp(state: GameState, srd: SrdContent) -> tuple[GameState, list[Event]]:
    """Бой позади, отряд устраивается на привал; конец сцены разыгрывают триггеры набора правил.

    Так поступит инструмент `end_scene` (B4.7): сначала новая сцена, затем триггеры конца сцены
    в порядке набора. Пока что его роль играет этот помощник."""
    camp = state.model_copy(update={"combat": None, "scene": Scene(type=SceneType.CAMP)})
    events: list[Event] = []
    for trigger in build_ruleset(state.rules).triggers["on_scene_end"]:
        camp, new = trigger(camp, srd)
        events += new
    return camp, events


# =============================================================================================
# Рассказ: схватка в склепе от инициативы до утра после привала
# =============================================================================================


class Table:
    """Стол рассказа: состояние, журнал событий и шаги на заданных кубах.

    Каждый шаг расходует кубы без остатка, оставляет состояние сохраняемым в JSON и проверяет,
    что события не противоречат состоянию."""

    def __init__(self, srd: SrdContent, state: GameState) -> None:
        self.srd, self.state, self.events = srd, state, []

    def do(self, call, rolls, *args, **kwargs) -> list[Event]:
        rng = ScriptedRng(rolls)
        self.state, events = call(self.state, *args, rng=rng, content=self.srd, **kwargs)
        assert all_dice_spent(rng), f"{call.__name__}: остались неиспользованные кубы"
        assert dice_shown(events) == len(rolls), f"{call.__name__}: куб без записи в событиях"
        assert_events_are_public_and_valid(events)
        assert_events_describe_the_state(self.state, events)
        if self.state.combat is not None:
            assert_state_is_sane(self.state)
        self.events += events
        return events

    def now(self):
        return current_actor(self.state).id

    def hp(self, fighter_id: str) -> int:
        return hit_points(self.state, fighter_id)


@pytest.fixture
def crypt(srd, make_character):
    """Герой-воин и жрец против двух воинов-гоблинов (КД 15, 10 HP, Лов 15) в одном зале.

    У героя КД 10, длинный меч (+5 к атаке, 1к8+3) и зелье лечения; у жреца КД 10, Мдр 16
    (Сл 13), Ученик жизни и четыре ячейки 1-го уровня."""
    hero = make_character(
        id=HERO,
        name="Герой",
        background="acolyte",
        scores={STR: 16},
        inventory=[
            InventoryItem(item_id="longsword", equipped=True),
            InventoryItem(item_id="potion_of_healing"),
        ],
        resources={"second_wind": 2, "action_surge": 1},
    )
    cleric = make_character(
        id=CLERIC,
        name="Жрец",
        class_id="cleric",
        background="acolyte",
        scores={WIS: 16},
        save_proficiencies=[WIS, CHA],
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 4, 2: 2},
        cantrips=["sacred_flame"],
        prepared_spells=["bless", "healing_word"],
        resources={"channel_divinity": 2},
    )
    # Перед боем отряд уже дважды отдыхал коротко: привал должен обнулить этот счётчик.
    state = make_state(hero, cleric, time=GameTime(day=1, part="night"), short_rests_since_long=2)
    table = Table(srd, state)
    # Инициатива: герой 18, жрец 12, первый гоблин 4 + 2, второй 2 + 2.
    table.do(
        start_combat,
        [18, 12, 4, 2],
        encounter_id="crypt-door",
        zones=ZONES,
        links=LINKS,
        monsters=monsters_in(srd, ("goblin_warrior", "hall"), ("goblin_warrior", "hall")),
        pc_zone="hall",
    )
    return table


def test_a_fight_from_initiative_to_the_morning_after_the_camp(crypt, srd):
    t = crypt
    assert t.state.combat.order == [HERO, CLERIC, GOBLIN, GOBLIN_2]
    assert t.state.scene.type == SceneType.COMBAT

    # --- раунд 1 ---
    # Герой вступает в ближний бой с первым гоблином и попадает: 14 + 5 = 19 против КД 15,
    # урон 4 + 3 = 7 из 10.
    t.do(move, [], HERO, engage=GOBLIN)
    t.do(attack, [14, 4], HERO, GOBLIN, weapon="longsword")
    assert t.hp(GOBLIN) == 3
    t.do(end_turn, [])

    # Жрец благословляет себя и героя и держит концентрацию.
    t.do(cast_spell, [], CLERIC, "bless", [HERO, CLERIC])
    assert t.state.party[CLERIC].concentration == "bless"
    assert t.state.party[CLERIC].spell_slots == {1: 3, 2: 2}
    assert [b.id for b in t.state.party[HERO].buffs] == ["bless"]
    t.do(end_turn, [])

    # Первый гоблин бьёт героя: 17 + 4 против КД 10, урон 5 + 2 = 7; у героя 13 из 20.
    t.do(monster_action, [17, 5], GOBLIN, "scimitar", [HERO])
    assert t.hp(HERO) == 13
    t.do(end_turn, [])

    # Второй гоблин подходит к жрецу и бьёт его на 6 + 2 = 8. Концентрация: Сл = max(10, 8 // 2),
    # к20 даёт 7, Благословение жреца ещё 2, Тел +0 — итог 9 против 10, и заклинание рушится.
    t.do(move, [], GOBLIN_2, engage=CLERIC)
    events = t.do(monster_action, [12, 6, 7, 2], GOBLIN_2, "scimitar", [CLERIC])
    save = [e for e in events if isinstance(e, RollEvent) and e.dc is not None][0]
    assert (save.dc, save.roll.total, save.success) == (10, 9, False)
    assert t.hp(CLERIC) == 12
    assert t.state.party[CLERIC].concentration is None
    assert t.state.party[HERO].buffs == [] and t.state.party[CLERIC].buffs == []
    assert {(e.target_id, e.condition) for e in events if isinstance(e, ConditionEvent)} == {
        (HERO, "bless"),
        (CLERIC, "bless"),
    }
    t.do(end_turn, [])
    assert (t.state.combat.round, t.now()) == (2, HERO)

    # --- раунд 2 ---
    # Герой добивает первого гоблина: 10 + 5 = 15 ровно против КД 15, урон 2 + 3 = 5 при 3 HP.
    t.do(attack, [10, 2], HERO, GOBLIN, weapon="longsword")
    assert t.hp(GOBLIN) == 0
    assert t.state.combat.combatants[HERO].engaged_with == []  # убитый ни с кем не вовлечён
    assert combat_winner(t.state) is None
    t.do(end_turn, [])
    assert GOBLIN not in t.state.combat.order
    assert t.now() == CLERIC

    # Жрец жжёт второго гоблина Священным пламенем: спасбросок Лов 5 + 2 = 7 против Сл 13, 8 урона.
    t.do(cast_spell, [5, 8], CLERIC, "sacred_flame", [GOBLIN_2])
    assert t.hp(GOBLIN_2) == 2
    assert t.state.party[CLERIC].spell_slots == {1: 3, 2: 2}  # заговор ячейки не тратит
    t.do(end_turn, [])

    # Второй гоблин попадает по жрецу натуральной 20: кости урона вдвое, 6 + 6 + 2 = 14 при 12 HP.
    # Остаток 2 меньше максимума 20, поэтому жрец не гибнет, а падает без сознания.
    t.do(monster_action, [20, 6, 6], GOBLIN_2, "scimitar", [CLERIC])
    assert t.hp(CLERIC) == 0
    assert not is_dead(t.state.party[CLERIC])
    assert any(
        c.id == "unconscious" and c.source == ZERO_HP_SOURCE
        for c in t.state.party[CLERIC].conditions
    )
    t.do(end_turn, [])
    assert (t.state.combat.round, t.now()) == (3, HERO)

    # --- раунд 3 ---
    # Герой подходит ко второму гоблину и добивает его (18 против КД 15, урон 1 + 3 = 4 при 2 HP):
    # бой выигран, хотя жрец лежит.
    t.do(move, [], HERO, engage=GOBLIN_2)
    t.do(attack, [13, 1], HERO, GOBLIN_2, weapon="longsword")
    assert t.hp(GOBLIN_2) == 0
    assert combat_winner(t.state) == "party"

    # Ход переходит к лежащему жрецу: в его начале спасбросок от смерти, 12 — успех.
    events = t.do(end_turn, [12])
    death_save = [e for e in events if isinstance(e, RollEvent)][0]
    assert (death_save.label, death_save.dc, death_save.success) == (
        "Спасбросок от смерти",
        10,
        True,
    )
    assert t.state.party[CLERIC].death_saves.successes == 1
    assert t.now() == CLERIC  # лежащий ходит вхолостую: оркестратор вызывает end_turn снова
    t.do(end_turn, [])

    # --- раунд 4 ---
    # Герой даёт жрецу зелье: с «Специями BG3» это бонусное действие. Лечит 2к4 + 2 = 9.
    events = t.do(
        simple_action,
        [3, 4],
        HERO,
        "use_item",
        item_id="potion_of_healing",
        target_id=CLERIC,
        rules=build_ruleset(RULES),
    )
    cleric = t.state.party[CLERIC]
    assert (cleric.hp, cleric.conditions, cleric.death_saves.successes) == (9, [], 0)
    assert t.state.combat.combatants[HERO].turn_flags == {"bonus_action": True}
    assert t.state.party[HERO].inventory == [InventoryItem(item_id="longsword", equipped=True)]
    assert any(isinstance(e, HealEvent) and e.hp_after == 9 for e in events)

    # Очнувшись, жрец снова благословляет отряд и держит концентрацию: к утру её не останется.
    t.do(end_turn, [])
    t.do(cast_spell, [], CLERIC, "bless", [HERO, CLERIC])
    assert t.state.party[CLERIC].concentration == "bless"
    assert t.state.party[CLERIC].spell_slots == {1: 2, 2: 2}

    # --- привал ---
    camp, events = go_to_camp(t.state, srd)
    for c in camp.party.values():
        assert c.hp == c.hp_max == 20
        assert c.buffs == [] and c.concentration is None  # длинный отдых заканчивает эффекты
    assert camp.party[CLERIC].spell_slots == {1: 4, 2: 2}
    assert camp.time == GameTime(day=2, part="morning")  # утро следующего дня
    assert camp.short_rests_since_long == 0
    (rest,) = events
    assert isinstance(rest, RestEvent) and rest.rest == "long"
    assert [(o.character_id, o.hp_gained) for o in rest.outcomes] == [(HERO, 7), (CLERIC, 11)]
    assert GameState.model_validate_json(camp.model_dump_json()) == camp

    # Журнал всего боя: события описывают состояние и записываются без потерь.
    assert_events_are_public_and_valid(t.events + events)
    assert_events_describe_the_state(t.state, t.events)
    assert [e.phase for e in t.events if isinstance(e, CombatEvent)].count("started") == 1
    assert sum(isinstance(e, SceneEvent) for e in t.events) == 1


# =============================================================================================
# Игра до конца: простая тактика за обе стороны на SeededRng
# =============================================================================================

MAX_TURNS = 400
PARTY = [
    ("fighter", "orc"),
    ("barbarian", "human"),
    ("cleric", "halfling"),
    ("wizard", "gnome"),
]
# Встреча: сколько героев из PARTY участвует и кто против них.
ENCOUNTERS = {
    # Слабые противники: отряд из четверых выигрывает, лежащих спасают.
    "goblins": (4, (("goblin_warrior", "hall"), ("goblin_warrior", "stairs"), ("wolf", "hall"))),
    # Сильные: двое героев против огра и двух гулей — выигрывают то одни, то другие, встречаются
    # паралич от когтей гуля, мультиатаки и смерть героев.
    "ogre-and-ghouls": (2, (("ogre", "hall"), ("ghoul", "hall"), ("ghoul", "stairs"))),
}


def build_party(srd: SrdContent, size: int) -> list[Character]:
    return [
        quick_character(class_id, species, srd, name=class_id, concept="т", character_id=f"pc-{i}")
        for i, (class_id, species) in enumerate(PARTY[:size], start=1)
    ]


def standing(state: GameState, side: str) -> list[str]:
    return [
        i for i, f in state.combat.combatants.items() if f.side == side and hit_points(state, i) > 0
    ]


def weapon_of(c: Character, srd: SrdContent) -> str:
    for entry in c.inventory:
        item = srd.items[entry.item_id]
        if entry.equipped and item.weapon and item.weapon.range == "melee":
            return entry.item_id
    return "improvised"


def approach(state: GameState, actor_id: str, foes: list[str]) -> tuple:
    """Подойти: вступить в бой с врагом из своей зоны, иначе перейти в зону первого врага."""
    combat = state.combat
    here = [f for f in foes if combat.combatants[f].zone_id == combat.combatants[actor_id].zone_id]
    kwargs = {"engage": here[0]} if here else {"to_zone": combat.combatants[foes[0]].zone_id}
    return move, (actor_id,), kwargs


def downed_allies_nearby(state: GameState, zone_id: str) -> list[str]:
    """Живые герои на 0 HP в этой зоне: их можно поднять лечением."""
    return [
        i
        for i, c in state.party.items()
        if c.hp == 0 and not is_dead(c) and state.combat.combatants[i].zone_id == zone_id
    ]


def plans_for(state: GameState, srd: SrdContent) -> list[tuple]:
    """Что стоит попробовать бойцу в его ход: `(функция, аргументы, именованные аргументы)`."""
    actor = current_actor(state)
    foes = standing(state, "enemies" if actor.side == "party" else "party")
    engaged = [f for f in foes if f in actor.engaged_with]
    if actor.kind == "monster":
        actions = [a for a in srd.monsters[actor.ref].actions if a.kind != "other"]
        # Действия монстра идут по кругу, чтобы в игре встретились и когти гуля, и мультиатаки.
        action = actions[(state.combat.round + len(actor.id)) % len(actions)]
        close = action.kind == "multiattack" or action.range == "melee"
        if close and not engaged:
            return [approach(state, actor.id, foes)]
        return [(monster_action, (actor.id, action.id, [(engaged or foes)[0]]), {})]
    me = state.party[actor.id]
    if me.hp == 0 or is_dead(me):
        return []
    plans = []
    if me.class_id == "barbarian" and not me.buffs and me.resources.get("rage", 0) > 0:
        plans.append((simple_action, (actor.id, "rage"), {}))
    downed = downed_allies_nearby(state, actor.zone_id)
    if me.class_id == "cleric" and downed:
        plans.append((cast_spell, (actor.id, "healing_word", [downed[0]]), {}))
    if me.class_id == "wizard":
        plans.append((cast_spell, (actor.id, "fire_bolt", [foes[0]]), {}))
    elif engaged:
        plans.append((attack, (actor.id, engaged[0]), {"weapon": weapon_of(me, srd)}))
    else:
        plans.append(approach(state, actor.id, foes))
    return plans


def attempt(state: GameState, dice: CountingRng, call, *args, **kwargs):
    """Действие как у оркестратора: отказ (`CombatError`) не меняет состояние и не тратит кубы."""
    before, used = state.model_dump_json(), dice.calls
    try:
        return call(state, *args, rng=dice, **kwargs)
    except CombatError:
        assert state.model_dump_json() == before
        assert dice.calls == used, "отказ потратил кубы"
        return state, []


def fight(srd: SrdContent, seed: int, encounter: str):
    """Бой до победы одной из сторон; возвращает начальное и конечное состояние и все события."""
    dice = CountingRng(SeededRng(seed))
    party_size, monsters = ENCOUNTERS[encounter]
    start = make_state(*build_party(srd, party_size), short_rests_since_long=2)
    state, events = start_combat(
        start,
        encounter_id=encounter,
        zones=ZONES,
        links=LINKS,
        monsters=monsters_in(srd, *monsters),
        pc_zone="hall",
        rng=dice,
        content=srd,
    )
    log = list(events)
    assert dice_shown(events) == dice.calls
    for _ in range(MAX_TURNS):
        if combat_winner(state) is not None:
            break
        used, turn_events = dice.calls, []
        for call, args, kwargs in plans_for(state, srd):
            state, new = attempt(state, dice, call, *args, content=srd, **kwargs)
            turn_events += new
        state, new = end_turn(state, rng=dice, content=srd)
        turn_events += new
        assert dice.calls - used == dice_shown(turn_events), "куб без записи в событиях"
        assert_state_is_sane(state)
        log += turn_events
    return start, state, log


SEEDS = range(6)


def expected_monster_ids(encounter: str) -> list[tuple[str, int]]:
    """Id монстров встречи: вид и номер внутри вида, как их выдаёт `start_combat`."""
    kinds = [monster for monster, _ in ENCOUNTERS[encounter][1]]
    return [(monster, kinds[: i + 1].count(monster)) for i, monster in enumerate(kinds)]


@pytest.mark.parametrize("encounter", ENCOUNTERS)
@pytest.mark.parametrize("seed", SEEDS)
def test_a_fight_played_to_the_end_has_a_winner_and_honest_events(srd, seed, encounter):
    start, end, events = fight(srd, seed, encounter)

    assert combat_winner(end) in ("party", "enemies")
    assert_events_are_public_and_valid(events)
    assert_events_describe_the_state(end, events)
    # Игра шла по кругу ходов: бой начался один раз, ходов было много, раунды росли.
    phases = [e.phase for e in events if isinstance(e, CombatEvent)]
    assert phases.count("started") == 1 and phases.count("turn") > len(end.party)
    assert end.combat.round > 1
    # Состав отряда и бойцы те же, что и в начале боя: никто не появился и не пропал.
    assert set(end.party) == set(start.party)
    assert set(end.combat.combatants) - set(end.party) == {
        f"{monster}-{n}" for monster, n in expected_monster_ids(encounter)
    }


def test_the_weak_encounter_is_won_by_the_party_and_the_strong_one_by_both_sides(srd):
    def played(encounter):
        return [fight(srd, seed, encounter)[1] for seed in range(20)]

    weak, strong = played("goblins"), played("ogre-and-ghouls")

    # Простая тактика не должна прятать исходы: сильные противники иногда побеждают и убивают
    # героев, иначе сценарии ниже никогда не дошли бы до поражения и до мёртвых персонажей.
    assert {combat_winner(end) for end in weak} == {"party"}
    assert {combat_winner(end) for end in strong} == {"party", "enemies"}
    assert any(is_dead(c) for end in strong for c in end.party.values())


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_fight_is_reproducible_by_seed_and_leaves_the_srd_alone(srd, seed):
    before = srd.model_dump(mode="json")

    first = fight(srd, seed, "ogre-and-ghouls")
    again = fight(srd, seed, "ogre-and-ghouls")
    other = fight(srd, seed + 100, "ogre-and-ghouls")

    def as_json(result):
        _, end, events = result
        return end.model_dump_json(), EVENTS.dump_json(events)

    assert as_json(first) == as_json(again)  # ни скрытого генератора, ни общего состояния
    assert as_json(first) != as_json(other)
    assert srd.model_dump(mode="json") == before


def fights_digest(srd: SrdContent, seeds) -> str:
    """Отпечаток боёв по seed: итоговые состояния и все события."""
    digest = hashlib.sha256()
    for encounter in ENCOUNTERS:
        for seed in seeds:
            _, end, events = fight(srd, seed, encounter)
            digest.update(end.model_dump_json().encode())
            digest.update(EVENTS.dump_json(events))
    return digest.hexdigest()


def test_a_fight_does_not_depend_on_the_hash_seed_of_the_process(srd):
    # Порядок обхода множеств строк меняется от запуска к запуску; бой не должен его замечать.
    script = "import test_combat_scenarios as t; print(t.fights_digest(t.load_srd(), range(4)))"

    def digest_in_new_process(hash_seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(Path(__file__).parent)}
        done = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    expected = fights_digest(srd, range(4))

    assert {digest_in_new_process(hash_seed) for hash_seed in ("1", "2", "3")} == {expected}


@pytest.mark.parametrize("encounter", ENCOUNTERS)
@pytest.mark.parametrize("seed", SEEDS)
def test_the_camp_after_any_fight_gets_every_living_character_back_on_their_feet(
    srd, seed, encounter
):
    _, end, _ = fight(srd, seed, encounter)

    camp, events = go_to_camp(end, srd)

    (rest,) = events
    assert isinstance(rest, RestEvent)
    assert [o.character_id for o in rest.outcomes] == list(end.party)
    assert camp.combat is None and camp.scene.type == SceneType.CAMP
    assert camp.time == GameTime(day=2, part="morning") and camp.short_rests_since_long == 0
    for character_id, before in end.party.items():
        after = camp.party[character_id]
        if is_dead(before):
            # Мёртвый отдыха не получает: ни хитов, ни ячеек, пустой итог.
            assert after == before
            continue
        assert (after.hp, after.temp_hp) == (after.hp_max, 0)
        assert after.spell_slots == after.spell_slots_max
        assert after.concentration is None and after.death_saves.failures == 0
        assert all(b.rounds_left is None for b in after.buffs)  # эффекты со сроком кончились
        # Состояния со сроком и «без сознания» от нуля хитов прошли; бессрочные остаются, пока их
        # не снимет мастер (так устроен `long_rest`).
        assert after.conditions == [
            c for c in before.conditions
            if c.rounds_left is None and not (c.id == "unconscious" and c.source == ZERO_HP_SOURCE)
        ]  # fmt: skip
    assert GameState.model_validate_json(camp.model_dump_json()) == camp


# =============================================================================================
# Контракт функций боя: вход не меняется, результат ничего не делит с ним, кубы видны всем
# =============================================================================================


def turn_of(state: GameState, fighter_id: str) -> GameState:
    state.combat.turn_index = state.combat.order.index(fighter_id)
    return state


@pytest.fixture
def arena_party(make_character):
    """Герой с эффектами, состоянием со спасброском и запасами; маг с концентрацией."""
    hero = make_character(
        id=HERO,
        name="Герой",
        background="acolyte",
        scores={STR: 16},
        hp=12,
        temp_hp=3,
        conditions=[ActiveCondition(id="poisoned", rounds_left=3, source=GOBLIN, save=CON, dc=12)],
        buffs=[ActiveBuff(id="rage", rounds_left=5, source_id=HERO, data={"hp": 1})],
        inventory=[
            InventoryItem(item_id="longsword", equipped=True),
            InventoryItem(item_id="potion_of_healing", qty=2),
        ],
        resources={"second_wind": 2, "action_surge": 1},
    )
    wizard = make_character(
        id="wiz",
        name="Маг",
        class_id="wizard",
        background="sage",
        scores={INT: 16},
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 4, 2: 2},
        cantrips=["fire_bolt"],
        prepared_spells=["burning_hands", "hold_person", "sleep"],
        concentration="hold_person",
    )
    return hero, wizard


def arena(srd: SrdContent, party: tuple[Character, ...]) -> GameState:
    """Бой с двумя воинами-гоблинами в зале и гулем на лестнице; герой уже в ближнем бою."""
    state, _ = start_combat(
        make_state(*party),
        encounter_id="crypt",
        zones=ZONES,
        links=LINKS,
        monsters=monsters_in(
            srd, ("goblin_warrior", "hall"), ("goblin_warrior", "hall"), ("ghoul", "stairs")
        ),
        pc_zone="hall",
        rng=ScriptedRng([20, 19, 5, 4, 3]),
        content=srd,
    )
    state.combat.combatants[HERO].engaged_with.append(GOBLIN)
    state.combat.combatants[GOBLIN].engaged_with.append(HERO)
    return state


# Каждая функция боя в деле: кто ходит, какая функция и с какими аргументами (кубы и данные SRD
# добавляет `run_call`). Все вызовы допустимы, а исход зависит от кубов, поэтому проверяются
# свойства, а не числа.
STUNNED = ActiveCondition(id="stunned", rounds_left=2, source=GOBLIN)
COMBAT_CALLS = {
    "move": (HERO, move, (HERO,), {"to_zone": "stairs"}),  # уход из боя: атака по возможности
    "attack": (HERO, attack, (HERO, GOBLIN), {"weapon": "longsword"}),
    "apply_damage": (HERO, apply_damage, (HERO, 5, "fire"), {}),
    "heal_target": (HERO, heal_target, (HERO, 4), {}),
    "end_turn": (HERO, end_turn, (), {}),
    "cast_spell": ("wiz", cast_spell, ("wiz", "burning_hands", [GOBLIN, GOBLIN_2]), {}),
    "simple_action": (HERO, simple_action, (HERO, "use_item"), {"item_id": "potion_of_healing"}),
    "monster_action": (GOBLIN, monster_action, (GOBLIN, "scimitar", [HERO]), {}),
    "apply_condition": ("wiz", apply_condition, ("wiz", STUNNED), {}),  # маг теряет концентрацию
}


def run_call(function, state, args, kwargs, dice, srd):
    """Вызывает функцию боя, добавляя `rng` и `content` только тем, кто их принимает."""
    accepted = inspect.signature(function).parameters
    extra = {
        name: value for name, value in {"rng": dice, "content": srd}.items() if name in accepted
    }
    return function(state, *args, **kwargs, **extra)


def assert_a_pure_honest_call(state: GameState, call, dice: CountingRng):
    before = state.model_dump_json()

    after, events = call(state, dice)

    assert events, "сценарий ничего не сделал"
    assert state.model_dump_json() == before  # вход не изменился
    assert after is not state and after.model_dump_json() != before
    assert shared_between(state, after) == []  # правка результата не доберётся до входа
    assert aliased_inside(after) == []  # и ни одна запись не лежит у двух бойцов сразу
    assert shared_between(state, events) == [] and shared_between(after, events) == []
    assert (after.turn, after.version) == (state.turn, state.version)  # их повышает сервер
    # Каждый выпавший куб виден в событиях; тайных бросков и личных сообщений бой не создаёт.
    assert dice.calls == dice_shown(events)
    assert_events_are_public_and_valid(events)
    assert GameState.model_validate_json(after.model_dump_json()) == after


@pytest.mark.parametrize("name", COMBAT_CALLS)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_combat_functions_leave_the_input_alone_share_nothing_and_hide_no_dice(
    srd, arena_party, name, seed
):
    actor, function, args, kwargs = COMBAT_CALLS[name]
    state = turn_of(arena(srd, arena_party), actor)

    def call(s, dice):
        return run_call(function, s, args, kwargs, dice, srd)

    assert_a_pure_honest_call(state, call, CountingRng(SeededRng(seed)))


def test_start_combat_leaves_the_input_alone_shares_nothing_and_hides_no_dice(srd, arena_party):
    state = make_state(*arena_party)
    kwargs = {
        "encounter_id": "crypt",
        "zones": ZONES,
        "links": LINKS,
        "monsters": monsters_in(srd, ("goblin_warrior", "hall")),
        "pc_zone": "hall",
    }

    def call(s, dice):
        return run_call(start_combat, s, (), kwargs, dice, srd)

    assert_a_pure_honest_call(state, call, CountingRng(SeededRng(5)))
    assert shared_between(ZONES, state) == []  # зоны приключения не попадают в состояние


def test_a_condition_given_to_several_targets_is_a_separate_record_for_each(srd, arena_party):
    state = turn_of(arena(srd, arena_party), "wiz")

    # Сл Усыпления 13, спасбросок Мдр гоблина −1: две единицы — оба засыпают.
    after, _ = cast_spell(
        state, "wiz", "sleep", [GOBLIN, GOBLIN_2], rng=ScriptedRng([1, 1]), content=srd
    )

    first, second = (after.combat.combatants[g].conditions[0] for g in (GOBLIN, GOBLIN_2))
    assert first == second and first is not second
    assert aliased_inside(after) == []


def test_the_shared_object_checks_really_see_a_shared_list_and_a_shared_record(arena_party):
    state = make_state(*arena_party)
    leaky = state.model_copy()  # поверхностная копия делит с оригиналом все вложенные объекты

    assert shared_between(state, leaky)
    assert shared_between(state, state.model_copy(deep=True)) == []
    assert aliased_inside(state) == []
    hero, wizard = state.party[HERO], state.party["wiz"]
    wizard.conditions = hero.conditions  # один и тот же список состояний у двух персонажей
    assert aliased_inside(state)
