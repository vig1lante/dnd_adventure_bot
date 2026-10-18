"""Начало боя, дистанция между бойцами и перемещение по зонам.

Данные — встроенные SRD (воин-гоблин: КД 15, 10 HP, Лов 15). Случайность — только
`ScriptedRng`: броски инициативы идут в порядке «персонажи по порядку отряда, затем монстры
по порядку списка».
"""

import pytest

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveCondition, DeathSaves, InventoryItem
from dnd_engine.combat.engine import CombatError, current_actor, distance, move, start_combat
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.conditions import ZERO_HP_SOURCE
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.events import (
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    HealEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef, Scene, SceneType

DEX = Ability.DEX

ZONES = [
    Zone(id="hall", name="Зал"),
    Zone(id="stairs", name="Лестница"),
    Zone(id="vault", name="Хранилище"),
    Zone(id="cellar", name="Погреб"),  # ни с чем не связана
]
LINKS = [
    ZoneLink(a="hall", b="stairs", distance="near"),
    ZoneLink(a="stairs", b="vault", distance="near"),
    ZoneLink(a="hall", b="vault", distance="far"),
]
HERO = "pc-1"
GOBLIN = "goblin_warrior-1"
NO_DICE = ScriptedRng([])  # перемещение ничего не бросает: лишний бросок упадёт ошибкой


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def state_of(*characters) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RulesRef(),
        party={c.id: c for c in characters},
        location_id="entrance",
    )


def begin(srd, state, rolls, *monster_zones, pc_zone="hall", monster_id="goblin_warrior"):
    """Начинает бой с монстрами одного вида в указанных зонах; `rolls` — кости инициативы."""
    return start_combat(
        state,
        encounter_id="crypt-door",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters[monster_id], zone) for zone in monster_zones],
        pc_zone=pc_zone,
        rng=ScriptedRng(rolls),
        content=srd,
    )


@pytest.fixture
def hero_turn(srd, make_character):
    """Фабрика боя, где первым ходит герой pc-1; гоблины стоят в указанных зонах."""

    def factory(*goblin_zones, hero_zone="hall") -> GameState:
        state, _ = begin(
            srd,
            state_of(make_character(id=HERO, name="Герой")),
            [20, *[1] * len(goblin_zones)],
            *goblin_zones,
            pc_zone=hero_zone,
        )
        return state

    return factory


def go(srd, state, rolls=(), **kwargs):
    """Перемещение героя; `rolls` — кубы атак по возможности (без них любой бросок — ошибка)."""
    rng = ScriptedRng(list(rolls)) if rolls else NO_DICE
    return move(state, HERO, rng=rng, content=srd, **kwargs)


def engaged(state, fighter_id):
    return state.combat.combatants[fighter_id].engaged_with


def lock_in_melee(state, *fighter_ids):
    """Расстановка для теста: герой уже в ближнем бою с бойцами, бюджет перемещений цел."""
    for fighter_id in fighter_ids:
        state.combat.combatants[HERO].engaged_with.append(fighter_id)
        state.combat.combatants[fighter_id].engaged_with.append(HERO)
    return state


def engaged_with_both_goblins(hero_turn):
    return lock_in_melee(hero_turn("hall", "hall"), GOBLIN, "goblin_warrior-2")


# --- start_combat: бойцы ---


def test_party_members_and_monsters_become_combatants(srd, make_character):
    heroes = state_of(
        make_character(id="pc-1", name="Анна"), make_character(id="pc-2", name="Борис")
    )

    state, _ = begin(srd, heroes, [10, 10, 10, 10], "stairs", "stairs")

    combatants = state.combat.combatants
    assert set(combatants) == {"pc-1", "pc-2", "goblin_warrior-1", "goblin_warrior-2"}
    anna = combatants["pc-1"]
    assert (anna.kind, anna.side, anna.ref, anna.name) == ("pc", "party", "pc-1", "Анна")
    assert (anna.zone_id, anna.hp, anna.hp_max, anna.ac) == ("hall", None, None, None)
    goblin = combatants["goblin_warrior-2"]
    assert (goblin.kind, goblin.side, goblin.ref) == ("monster", "enemies", "goblin_warrior")
    assert goblin.zone_id == "stairs"


def test_monsters_get_the_average_hit_points_and_armor_class_of_their_statblock(
    srd, make_character
):
    state, _ = begin(
        srd, state_of(make_character()), [10, 10, 10], "hall", "hall", monster_id="bandit_captain"
    )

    for fighter_id in ("bandit_captain-1", "bandit_captain-2"):
        captain = state.combat.combatants[fighter_id]
        assert (captain.hp, captain.hp_max, captain.ac) == (52, 52, 15)


def test_same_kind_of_monster_is_numbered_in_names_only_when_there_are_several(srd, make_character):
    pair, _ = begin(srd, state_of(make_character()), [10, 10, 10], "hall", "hall")
    single, _ = begin(srd, state_of(make_character()), [10, 10], "hall")

    names = [pair.combat.combatants[f"goblin_warrior-{n}"].name for n in (1, 2)]
    assert names == ["Воин-гоблин 1", "Воин-гоблин 2"]
    assert single.combat.combatants[GOBLIN].name == "Воин-гоблин"


def test_new_combat_starts_at_the_first_round_with_the_given_battlefield(srd, make_character):
    state, _ = begin(srd, state_of(make_character()), [10, 10], "stairs")

    combat = state.combat
    assert (combat.encounter_id, combat.round, combat.turn_index) == ("crypt-door", 1, 0)
    assert combat.zones == ZONES
    assert combat.links == LINKS
    assert all(c.engaged_with == [] and c.turn_flags == {} for c in combat.combatants.values())


def test_scene_becomes_combat_and_forgets_inspiration_of_the_old_scene(srd, make_character):
    before = state_of(make_character())
    before.turn = 9
    before.scene = Scene(type=SceneType.SOCIAL, started_turn=3, inspiration_granted=["pc-1"])

    state, _ = begin(srd, before, [10, 10], "hall")

    assert state.scene == Scene(type=SceneType.COMBAT, started_turn=9, inspiration_granted=[])


# --- start_combat: инициатива ---


def test_initiative_is_d20_plus_dexterity_and_sorted_from_highest(srd, make_character):
    heroes = state_of(
        make_character(id="pc-1", scores={DEX: 14}),  # +2
        make_character(id="pc-2", scores={DEX: 10}),  # +0
    )

    state, _ = begin(srd, heroes, [15, 5, 10, 18], "hall", "hall")  # гоблины: Лов 15, +2

    combat = state.combat
    assert [(c.id, c.initiative) for c in map(combat.combatants.get, combat.order)] == [
        ("goblin_warrior-2", 20),
        ("pc-1", 17),
        ("goblin_warrior-1", 12),
        ("pc-2", 5),
    ]


@pytest.mark.parametrize(
    ("hero_dex", "expected_first"),
    [
        (12, GOBLIN),  # итог равен (11 + 1 против 10 + 2), у гоблина выше Лов
        (14, GOBLIN),  # модификатор тот же (+2), но значение Лов 15 выше 14
        (15, HERO),  # поровну и по Лов: персонаж идёт раньше монстра
    ],
)
def test_initiative_ties_go_to_higher_dexterity_then_to_characters(
    srd, make_character, hero_dex, expected_first
):
    hero = make_character(id=HERO, scores={DEX: hero_dex})
    hero_roll = 11 if hero_dex == 12 else 10

    state, _ = begin(srd, state_of(hero), [hero_roll, 10], "hall")

    assert state.combat.combatants[HERO].initiative == state.combat.combatants[GOBLIN].initiative
    assert state.combat.order[0] == expected_first


def test_alert_adds_the_proficiency_bonus_to_initiative(srd, make_character):
    alert_hero = make_character(id="pc-1", background="criminal", scores={DEX: 12})  # +1, бонус +2
    other_hero = make_character(id="pc-2", background="soldier", scores={DEX: 12})

    state, events = begin(srd, state_of(alert_hero, other_hero), [10, 10, 1], "hall")

    assert state.combat.combatants["pc-1"].initiative == 10 + 1 + 2
    assert state.combat.combatants["pc-2"].initiative == 10 + 1
    assert events[0].roll.expr == "1d20+3"


def test_the_alert_feat_in_data_matches_the_background_that_grants_it(srd):
    # Код ищет черту «Бдительный» по id: данные SRD должны выдавать её тем же id.
    assert srd.backgrounds["criminal"].feat == "alert"
    assert srd.feats["alert"].mechanical


def test_initiative_rolls_are_public_events_followed_by_scene_and_turn_events(srd, make_character):
    heroes = state_of(make_character(id="pc-1", name="Анна"))

    state, events = begin(srd, heroes, [12, 3], "hall")

    rolls = events[:2]
    assert all(isinstance(e, RollEvent) and e.audience == "all" for e in rolls)
    assert [(e.actor_id, e.label, e.roll.natural) for e in rolls] == [
        ("pc-1", "Инициатива", 12),
        (GOBLIN, "Инициатива", 3),
    ]
    assert events[2:] == [
        SceneEvent(scene=SceneType.COMBAT),
        CombatEvent(phase="started"),
        CombatEvent(phase="turn", actor_id=state.combat.order[0]),
    ]


def downed_hero(make_character, **overrides):
    """Герой на 0 HP без сознания: бой начинается, пока он лежит."""
    fields = {"id": HERO, "name": "Герой", "hp": 0}
    fields["conditions"] = [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)]
    return make_character(**(fields | overrides))


@pytest.mark.parametrize(
    ("die", "expected"),
    [
        (10, DeathSaves(successes=1)),
        (9, DeathSaves(failures=1)),
        (1, DeathSaves(failures=2)),
    ],
)
def test_a_downed_character_who_goes_first_makes_a_death_save_as_the_combat_starts(
    srd, make_character, die, expected
):
    # Кости: инициатива героя, инициатива гоблина, затем спасбросок от смерти его первого хода.
    state, events = begin(srd, state_of(downed_hero(make_character)), [20, 1, die], "hall")

    assert state.party[HERO].death_saves == expected
    assert [type(e) for e in events[2:]] == [SceneEvent, CombatEvent, CombatEvent, RollEvent]
    assert events[-1].label == "Спасбросок от смерти"


def test_a_natural_20_death_save_at_the_start_of_combat_brings_the_character_back(
    srd, make_character
):
    state, events = begin(srd, state_of(downed_hero(make_character)), [20, 1, 20], "hall")

    hero = state.party[HERO]
    assert (hero.hp, hero.conditions, hero.death_saves) == (1, [], DeathSaves())
    assert HealEvent(target_id=HERO, amount=1, hp_after=1) in events
    assert ConditionEvent(target_id=HERO, condition="unconscious", added=False) in events


def test_a_stable_character_or_one_who_does_not_go_first_makes_no_death_save_at_the_start(
    srd, make_character
):
    stable = downed_hero(make_character, death_saves=DeathSaves(stable=True))
    behind = downed_hero(make_character)

    # Кубов ровно на инициативу: лишний бросок упал бы ошибкой ScriptedRng.
    first_stable, _ = begin(srd, state_of(stable), [20, 1], "hall")
    second, _ = begin(srd, state_of(behind), [1, 20], "hall")

    assert first_stable.party[HERO].death_saves == DeathSaves(stable=True)
    assert second.party[HERO].death_saves == DeathSaves()


# --- start_combat: чистота и ошибки ---


def test_start_combat_leaves_the_original_state_and_input_zones_untouched(srd, make_character):
    original = state_of(make_character())
    snapshot = original.model_copy(deep=True)
    zones = [zone.model_copy() for zone in ZONES]
    links = [link.model_copy() for link in LINKS]

    state, _ = start_combat(
        original,
        encounter_id="e",
        zones=zones,
        links=links,
        monsters=[(srd.monsters["goblin_warrior"], "hall")],
        pc_zone="hall",
        rng=ScriptedRng([10, 10]),
        content=srd,
    )
    state.combat.zones[0].name = "Переименовано"
    state.combat.links[0].distance = "far"

    assert original == snapshot
    assert original.combat is None
    assert zones[0].name == "Зал"
    assert links[0].distance == "near"
    assert state is not original


def test_start_combat_survives_a_json_round_trip(srd, make_character):
    state, _ = begin(srd, state_of(make_character()), [10, 10], "stairs")

    assert GameState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize(
    ("pc_zone", "monster_zones", "zones", "links"),
    [
        ("attic", ["hall"], ZONES, LINKS),
        ("hall", ["attic"], ZONES, LINKS),
        ("hall", [], ZONES, LINKS),
        ("hall", ["hall"], ZONES, [ZoneLink(a="hall", b="attic", distance="near")]),
        ("hall", ["hall"], [*ZONES, Zone(id="hall", name="Ещё зал")], LINKS),
    ],
    ids=["pc-zone", "monster-zone", "no-monsters", "link-to-unknown-zone", "duplicate-zone"],
)
def test_impossible_battlefield_is_a_combat_error_and_rolls_nothing(
    srd, make_character, pc_zone, monster_zones, zones, links
):
    rng = ScriptedRng([7, 8])

    with pytest.raises(CombatError):
        start_combat(
            state_of(make_character()),
            encounter_id="e",
            zones=zones,
            links=links,
            monsters=[(srd.monsters["goblin_warrior"], zone) for zone in monster_zones],
            pc_zone=pc_zone,
            rng=rng,
            content=srd,
        )

    assert rng.randint(1, 20) == 7  # бросков не было


def test_a_character_cannot_share_an_id_with_a_monster(srd, make_character):
    clash = state_of(make_character(id=GOBLIN))

    with pytest.raises(CombatError, match="повторяются"):
        begin(srd, clash, [10, 10], "hall")


def test_combat_cannot_start_twice_or_without_party(srd, make_character):
    running, _ = begin(srd, state_of(make_character()), [10, 10], "hall")

    with pytest.raises(CombatError, match="уже идёт"):
        begin(srd, running, [10, 10], "hall")
    with pytest.raises(CombatError, match="отряд"):
        begin(srd, state_of(), [10], "hall")


# --- current_actor ---


def test_current_actor_follows_the_turn_index(srd, make_character):
    state, _ = begin(srd, state_of(make_character()), [20, 1], "hall")

    assert current_actor(state).id == HERO

    state.combat.turn_index = 1
    assert current_actor(state).id == GOBLIN


def test_current_actor_outside_combat_is_a_combat_error(make_character):
    with pytest.raises(CombatError, match="не идёт"):
        current_actor(state_of(make_character()))


# --- distance ---


@pytest.fixture
def battlefield(srd, make_character):
    """Герой в зале; гоблины: в зале, на лестнице, в хранилище и в погребе."""
    state, _ = begin(
        srd,
        state_of(make_character(id=HERO)),
        [10] * 5,
        "hall",
        "stairs",
        "vault",
        "cellar",
    )
    return state.combat


def test_fighters_in_one_zone_are_near_unless_both_sides_are_engaged(battlefield):
    assert distance(battlefield, HERO, "goblin_warrior-1") == "near"

    battlefield.combatants[HERO].engaged_with.append("goblin_warrior-1")
    assert distance(battlefield, HERO, "goblin_warrior-1") == "near"  # запись только у одного

    battlefield.combatants["goblin_warrior-1"].engaged_with.append(HERO)
    assert distance(battlefield, HERO, "goblin_warrior-1") == "engaged"
    assert distance(battlefield, "goblin_warrior-1", HERO) == "engaged"


def test_fighters_in_linked_zones_are_as_far_as_the_link_says(battlefield):
    assert distance(battlefield, HERO, "goblin_warrior-2") == "near"  # зал — лестница
    assert distance(battlefield, "goblin_warrior-2", HERO) == "near"  # связь без направления
    assert distance(battlefield, HERO, "goblin_warrior-3") == "far"  # зал — хранилище, связь far
    assert distance(battlefield, "goblin_warrior-2", "goblin_warrior-3") == "near"


def test_fighters_in_unlinked_zones_are_far(battlefield):
    assert distance(battlefield, HERO, "goblin_warrior-4") == "far"
    assert distance(battlefield, "goblin_warrior-2", "goblin_warrior-4") == "far"


def test_distance_to_an_unknown_fighter_is_a_combat_error(battlefield):
    with pytest.raises(CombatError, match="ghost"):
        distance(battlefield, HERO, "ghost")


# --- move: зоны ---


def test_move_changes_the_zone_and_marks_the_turn_as_moved(srd, hero_turn):
    state = hero_turn("hall")

    moved, events = go(srd, state, to_zone="stairs")

    hero = moved.combat.combatants[HERO]
    assert (hero.zone_id, hero.turn_flags) == ("stairs", {"moved": True})
    assert events == []


def test_move_does_not_change_the_original_state(srd, hero_turn):
    state = hero_turn("hall")
    snapshot = state.model_copy(deep=True)

    moved, _ = go(srd, state, to_zone="stairs")
    moved.combat.combatants[HERO].turn_flags["action"] = True

    assert state == snapshot
    assert moved is not state


@pytest.mark.parametrize(
    ("to_zone", "reason"),
    [
        ("cellar", "не добраться"),  # нет связи
        ("vault", "не добраться"),  # связь far — не за одно перемещение
        ("attic", "неизвестная зона"),
        ("hall", "уже в этой зоне"),
    ],
)
def test_move_to_an_unreachable_zone_is_a_combat_error(srd, hero_turn, to_zone, reason):
    state = hero_turn("hall")
    snapshot = state.model_copy(deep=True)

    with pytest.raises(CombatError, match=reason):
        go(srd, state, to_zone=to_zone)

    assert state == snapshot


def test_second_move_in_a_turn_needs_dash(srd, hero_turn):
    first, _ = go(srd, hero_turn("hall"), to_zone="stairs")

    with pytest.raises(CombatError, match="Рывк"):
        go(srd, first, to_zone="vault")


def test_dash_gives_a_second_move_and_costs_the_action(srd, hero_turn):
    first, _ = go(srd, hero_turn("hall"), to_zone="stairs")

    second, _ = go(srd, first, to_zone="vault", dash=True)

    hero = second.combat.combatants[HERO]
    assert hero.zone_id == "vault"
    assert hero.turn_flags == {"moved": True, "moved_again": True, "dashed": True, "action": True}


def test_dash_never_gives_a_third_move(srd, hero_turn):
    second, _ = go(srd, go(srd, hero_turn("hall"), to_zone="stairs")[0], to_zone="vault", dash=True)

    with pytest.raises(CombatError, match="все перемещения"):
        go(srd, second, to_zone="stairs", dash=True)


def test_dash_on_the_first_move_allows_the_second_without_repeating_it(srd, hero_turn):
    first, _ = go(srd, hero_turn("hall"), to_zone="stairs", dash=True)

    second, _ = go(srd, first, to_zone="vault")

    assert second.combat.combatants[HERO].zone_id == "vault"
    with pytest.raises(CombatError):
        go(srd, second, to_zone="stairs")


def test_dash_is_impossible_when_the_action_is_already_spent(srd, hero_turn):
    state = hero_turn("hall")
    state.combat.combatants[HERO].turn_flags["action"] = True

    with pytest.raises(CombatError, match="действие"):
        go(srd, state, to_zone="stairs", dash=True)
    moved, _ = go(srd, state, to_zone="stairs")  # обычное перемещение действия не требует

    assert moved.combat.combatants[HERO].zone_id == "stairs"


# --- move: ближний бой ---


def test_engaging_a_fighter_of_the_same_zone_updates_both_sides(srd, hero_turn):
    state = hero_turn("hall")

    moved, events = go(srd, state, engage=GOBLIN)

    assert engaged(moved, HERO) == [GOBLIN]
    assert engaged(moved, GOBLIN) == [HERO]
    assert moved.combat.combatants[HERO].zone_id == "hall"
    assert moved.combat.combatants[HERO].turn_flags == {"moved": True}
    assert distance(moved.combat, HERO, GOBLIN) == "engaged"
    assert events == []


def test_engaging_uses_up_the_move_of_the_turn(srd, hero_turn):
    engaged_state, _ = go(srd, hero_turn("hall", "hall"), engage=GOBLIN)

    with pytest.raises(CombatError, match="Рывк"):
        go(srd, engaged_state, engage="goblin_warrior-2")


def test_a_fighter_can_be_engaged_with_several_enemies(srd, hero_turn):
    first, _ = go(srd, hero_turn("hall", "hall"), engage=GOBLIN, dash=True)

    both, _ = go(srd, first, engage="goblin_warrior-2")

    assert engaged(both, HERO) == [GOBLIN, "goblin_warrior-2"]


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("goblin_warrior-2", "не в зоне"),  # стоит на лестнице
        ("ghost", "ghost"),
        (HERO, "противник"),  # с самим собой
    ],
)
def test_engaging_someone_who_cannot_be_engaged_is_a_combat_error(srd, hero_turn, target, reason):
    state = hero_turn("hall", "stairs")

    with pytest.raises(CombatError, match=reason):
        go(srd, state, engage=target)


def test_engaging_an_ally_is_a_combat_error(srd, make_character):
    allies = state_of(make_character(id=HERO), make_character(id="pc-2"))
    state, _ = begin(srd, allies, [20, 1, 1], "hall")

    with pytest.raises(CombatError, match="противник"):
        go(srd, state, engage="pc-2")


def test_engaging_an_enemy_you_are_already_engaged_with_is_a_combat_error(srd, hero_turn):
    first, _ = go(srd, hero_turn("hall"), engage=GOBLIN, dash=True)

    with pytest.raises(CombatError, match="уже в ближнем бою"):
        go(srd, first, engage=GOBLIN)


def test_move_and_engage_in_one_call_engages_a_fighter_of_the_new_zone(srd, hero_turn):
    state = hero_turn("stairs")

    moved, _ = go(srd, state, to_zone="stairs", engage=GOBLIN)

    assert moved.combat.combatants[HERO].zone_id == "stairs"
    assert engaged(moved, HERO) == [GOBLIN]
    assert engaged(moved, GOBLIN) == [HERO]
    assert moved.combat.combatants[HERO].turn_flags == {"moved": True}


def test_engaging_must_be_in_the_zone_where_the_move_ends(srd, hero_turn):
    state = hero_turn("hall")  # гоблин остаётся в зале, а герой уходит на лестницу

    with pytest.raises(CombatError, match="не в зоне"):
        go(srd, state, to_zone="stairs", engage=GOBLIN)


def test_leaving_melee_ends_the_engagement_on_both_sides(srd, hero_turn):
    state = engaged_with_both_goblins(hero_turn)

    moved, _ = go(srd, state, rolls=[1, 1], to_zone="stairs")  # оба гоблина промахиваются

    assert engaged(moved, HERO) == []
    assert engaged(moved, GOBLIN) == []
    assert engaged(moved, "goblin_warrior-2") == []
    assert distance(moved.combat, HERO, GOBLIN) == "near"


# --- move: атаки по возможности ---


def attacks_of_opportunity(events):
    return [e for e in events if isinstance(e, RollEvent) and e.label.startswith("Атака по")]


def test_leaving_melee_lets_every_engaged_enemy_attack_with_its_reaction(srd, hero_turn):
    state = engaged_with_both_goblins(hero_turn)

    # Первый гоблин: 15 + 4 против КД 10 — попадание, урон 1к6+2 = 3+2. Второй: 2 + 4 — промах.
    moved, events = go(srd, state, rolls=[15, 3, 2], to_zone="stairs")

    assert [e.actor_id for e in attacks_of_opportunity(events)] == [GOBLIN, "goblin_warrior-2"]
    assert [e.success for e in attacks_of_opportunity(events)] == [True, False]
    assert DamageEvent(target_id=HERO, amount=5, damage_type="slashing", hp_after=15) in events
    assert moved.party[HERO].hp == 15
    for goblin_id in (GOBLIN, "goblin_warrior-2"):
        assert moved.combat.combatants[goblin_id].turn_flags == {"reaction": True}
    hero = moved.combat.combatants[HERO]
    assert (hero.zone_id, hero.turn_flags) == ("stairs", {"moved": True})


def test_an_attack_of_opportunity_needs_no_readied_action_and_uses_a_readied_one_up(srd, hero_turn):
    state = lock_in_melee(hero_turn("hall"), GOBLIN)
    state.combat.combatants[GOBLIN].turn_flags["readied"] = True

    moved, events = go(srd, state, rolls=[1], to_zone="stairs")

    assert attacks_of_opportunity(events)[0].label == "Атака по возможности: Скимитар"
    # Реакция ушла на атаку по возможности, и подготовленному действию её больше не взять.
    assert moved.combat.combatants[GOBLIN].turn_flags == {"reaction": True}


def test_disengaging_makes_leaving_melee_safe(srd, hero_turn):
    state = engaged_with_both_goblins(hero_turn)
    state.combat.combatants[HERO].disengaged = True

    moved, events = go(srd, state, to_zone="stairs")  # кубов нет: атак не было

    assert events == []
    assert engaged(moved, HERO) == []


def test_moving_between_zones_without_melee_provokes_nobody(srd, hero_turn):
    moved, events = go(srd, hero_turn("stairs"), to_zone="stairs")

    assert events == [] and moved.party[HERO].hp == 20


def test_engaging_without_changing_zone_is_not_leaving_melee(srd, hero_turn):
    state = lock_in_melee(hero_turn("hall", "hall"), GOBLIN)

    moved, events = go(srd, state, engage="goblin_warrior-2")

    assert events == [] and engaged(moved, HERO) == [GOBLIN, "goblin_warrior-2"]


def test_move_with_engage_provokes_only_the_enemies_left_behind(srd, hero_turn):
    state = lock_in_melee(hero_turn("hall", "stairs"), GOBLIN)

    moved, events = go(srd, state, rolls=[1], to_zone="stairs", engage="goblin_warrior-2")

    assert [e.actor_id for e in attacks_of_opportunity(events)] == [GOBLIN]
    assert engaged(moved, HERO) == ["goblin_warrior-2"]
    assert engaged(moved, GOBLIN) == []


@pytest.mark.parametrize(
    "disable",
    [
        lambda goblin, state: goblin.turn_flags.update(reaction=True),  # реакция уже потрачена
        lambda goblin, state: goblin.conditions.append(ActiveCondition(id="stunned")),
    ],
    ids=["реакция потрачена", "ошеломлён"],
)
def test_an_enemy_who_cannot_react_makes_no_attack_of_opportunity(srd, hero_turn, disable):
    state = lock_in_melee(hero_turn("hall"), GOBLIN)
    disable(state.combat.combatants[GOBLIN], state)

    moved, events = go(srd, state, to_zone="stairs")  # кубов нет

    assert events == [] and moved.combat.combatants[HERO].zone_id == "stairs"


def armed_hero_battle(srd, make_character, inventory):
    """Ход гоблина; он вовлечён с героем, у героя заданное снаряжение (Сила 10, мастерство +2)."""
    # Предыстория «Служитель»: у «Солдата» была бы черта «Свирепый атакующий» (лишний бросок урона).
    hero = make_character(id=HERO, name="Герой", background="acolyte", inventory=inventory)
    state, _ = begin(srd, state_of(hero), [20, 1], "hall")
    state = lock_in_melee(state, GOBLIN)
    state.combat.turn_index = 1
    return state


def test_a_character_leaving_nothing_behind_strikes_with_the_first_equipped_melee_weapon(
    srd, make_character
):
    inventory = [
        InventoryItem(item_id="dagger", equipped=False),
        InventoryItem(item_id="shortbow", equipped=True),
        InventoryItem(item_id="longsword", equipped=True),
    ]
    state = armed_hero_battle(srd, make_character, inventory)

    # Гоблин уходит: герой бьёт длинным мечом (15 + 2 против КД 15), урон 1к8 = 4.
    moved, events = move(state, GOBLIN, to_zone="stairs", rng=ScriptedRng([15, 4]), content=srd)

    assert attacks_of_opportunity(events)[0].label == "Атака по возможности: Длинный меч"
    assert moved.combat.combatants[GOBLIN].hp == 10 - 4
    assert moved.combat.combatants[HERO].turn_flags == {"reaction": True}
    assert moved.combat.combatants[GOBLIN].zone_id == "stairs"


def test_an_unarmed_character_makes_no_attack_of_opportunity(srd, make_character):
    state = armed_hero_battle(srd, make_character, [InventoryItem(item_id="dagger")])  # не надет

    moved, events = move(state, GOBLIN, to_zone="stairs", rng=NO_DICE, content=srd)

    assert events == [] and moved.combat.combatants[GOBLIN].zone_id == "stairs"


def test_an_enemy_with_only_ranged_attacks_makes_no_attack_of_opportunity(srd, make_character):
    goblin = srd.monsters["goblin_warrior"]
    archer = goblin.model_copy(
        update={"actions": tuple(a for a in goblin.actions if a.range != "melee")}
    )
    content = srd.model_copy(update={"monsters": {**srd.monsters, "goblin_warrior": archer}})
    state, _ = begin(content, state_of(make_character(id=HERO, name="Герой")), [20, 1], "hall")
    state = lock_in_melee(state, GOBLIN)

    moved, events = go(content, state, to_zone="stairs")  # кубов нет: ответной атаки не было

    assert events == []
    assert moved.combat.combatants[GOBLIN].turn_flags == {}  # реакция цела
    assert (moved.combat.combatants[HERO].zone_id, engaged(moved, HERO)) == ("stairs", [])


def test_a_fighter_knocked_out_by_an_attack_of_opportunity_does_not_leave(srd, make_character):
    hero = make_character(id=HERO, name="Герой", hp=3)
    state, _ = begin(srd, state_of(hero), [20, 1, 1], "hall", "hall")
    state = lock_in_melee(state, GOBLIN, "goblin_warrior-2")

    # Первый гоблин попадает на 6 урона (было 3 HP), второй уже не бьёт: герой лежит.
    moved, events = go(srd, state, rolls=[15, 4], to_zone="stairs")

    assert moved.party[HERO].hp == 0
    assert len(attacks_of_opportunity(events)) == 1
    assert moved.combat.combatants[HERO].zone_id == "hall"
    assert moved.combat.combatants[HERO].turn_flags == {}
    assert engaged(moved, HERO) == [GOBLIN, "goblin_warrior-2"]


# --- move: кто и когда может двигаться ---


def test_move_needs_an_ongoing_combat(srd, make_character):
    with pytest.raises(CombatError, match="не идёт"):
        go(srd, state_of(make_character(id=HERO)), to_zone="stairs")


def test_only_the_fighter_whose_turn_it_is_can_move(srd, hero_turn):
    state = hero_turn("hall")

    with pytest.raises(CombatError, match="не ход"):
        move(state, GOBLIN, to_zone="stairs", rng=NO_DICE, content=srd)

    state.combat.turn_index = 1
    moved, _ = move(state, GOBLIN, to_zone="stairs", rng=NO_DICE, content=srd)
    assert moved.combat.combatants[GOBLIN].zone_id == "stairs"


def test_unknown_fighter_cannot_move(srd, hero_turn):
    with pytest.raises(CombatError, match="ghost"):
        move(hero_turn("hall"), "ghost", to_zone="stairs", rng=NO_DICE, content=srd)


def test_move_without_destination_and_target_is_a_combat_error(srd, hero_turn):
    with pytest.raises(CombatError, match="зону"):
        go(srd, hero_turn("hall"))


def test_a_fighter_at_zero_hit_points_cannot_move(srd, hero_turn):
    state = hero_turn("hall")
    state.party[HERO].hp = 0

    with pytest.raises(CombatError, match="не может двигаться"):
        go(srd, state, to_zone="stairs")


@pytest.mark.parametrize("condition_id", ["grappled", "restrained", "paralyzed", "unconscious"])
def test_conditions_that_set_speed_to_zero_forbid_moving_and_engaging(srd, hero_turn, condition_id):
    state = hero_turn("hall")
    state.party[HERO].conditions.append(ActiveCondition(id=condition_id))

    with pytest.raises(CombatError, match="не даёт двигаться"):
        go(srd, state, to_zone="stairs")
    with pytest.raises(CombatError, match="не даёт двигаться"):
        go(srd, state, engage=GOBLIN)


@pytest.mark.parametrize("condition_id", ["prone", "poisoned", "stunned", "incapacitated"])
def test_other_conditions_do_not_stop_a_fighter_from_moving(srd, hero_turn, condition_id):
    state = hero_turn("hall")
    state.party[HERO].conditions.append(ActiveCondition(id=condition_id))

    moved, _ = go(srd, state, to_zone="stairs")

    assert moved.combat.combatants[HERO].zone_id == "stairs"


def test_a_dead_enemy_cannot_be_engaged(srd, hero_turn):
    state = hero_turn("hall")
    state.combat.combatants[GOBLIN].hp = 0

    with pytest.raises(CombatError, match="мёртв"):
        go(srd, state, engage=GOBLIN)


def test_a_dead_character_does_not_join_the_combat_or_roll_initiative(srd, make_character):
    alive = make_character(id="pc-1", name="Анна")
    dead = make_character(id="pc-2", name="Борис", death_saves=DeathSaves(failures=3))
    rng = ScriptedRng([10, 10])  # героя и гоблина; мёртвому кубов не достаётся

    state, events = start_combat(
        state_of(alive, dead),
        encounter_id="e",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters["goblin_warrior"], "hall")],
        pc_zone="hall",
        rng=rng,
        content=srd,
    )

    assert set(state.combat.combatants) == {"pc-1", GOBLIN}
    assert [e.actor_id for e in events if isinstance(e, RollEvent)] == ["pc-1", GOBLIN]


def test_combat_cannot_start_when_every_character_is_dead(srd, make_character):
    dead = make_character(death_saves=DeathSaves(failures=3))

    with pytest.raises(CombatError, match="способных сражаться"):
        begin(srd, state_of(dead), [10], "hall")


def test_move_result_survives_a_json_round_trip(srd, hero_turn):
    moved, _ = go(srd, hero_turn("hall"), engage=GOBLIN)

    assert GameState.model_validate_json(moved.model_dump_json()) == moved
