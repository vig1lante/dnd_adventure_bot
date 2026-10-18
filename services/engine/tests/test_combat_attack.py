"""Бой: таблица состояний, атака, урон, смерть, конец хода (B3.3).

Данные — встроенные SRD. Случайность — только `ScriptedRng`: значения расходуются строго в
порядке бросков, поэтому лишний или недостающий бросок виден сразу (очередь кончается или
остаётся). Порядок бросков атаки: к20 (две кости при преимуществе или помехе), затем кости
урона по частям — оружие, скрытая атака, убийца колоссов, бешенство, метка охотника, вторичный
урон монстра; затем спасброски концентрации.
"""

import pytest

from dnd_engine.abilities import Ability
from dnd_engine.character import (
    ActiveBuff,
    ActiveCondition,
    Character,
    DeathSaves,
    InventoryItem,
)
from dnd_engine.checks import combine_advantage
from dnd_engine.combat.engine import (
    ALERT_FEAT,
    COLOSSUS_SLAYER,
    FRENZY,
    GUIDING_BOLT,
    HUNTERS_MARK,
    IMPROVED_CRITICAL,
    SAVAGE_ATTACKER_FEAT,
    SNEAK_ATTACK,
    CombatError,
    apply_damage,
    attack,
    combat_winner,
    current_actor,
    end_turn,
    heal_target,
    simple_action,
    start_combat,
)
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.conditions import (
    CONDITION_EFFECTS,
    ZERO_HP_SOURCE,
    attack_modes,
    auto_crit,
    auto_fail_condition,
    cannot_move,
    defense_modes,
    is_dead,
    is_incapacitated,
    regain_consciousness,
    resists_all_damage,
)
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage
from dnd_engine.events import CombatEvent, ConditionEvent, DamageEvent, HealEvent, RollEvent
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef

STR, DEX, CON, INT, WIS, CHA = Ability
NONE, ADVANTAGE, DISADVANTAGE = Advantage.NONE, Advantage.ADVANTAGE, Advantage.DISADVANTAGE


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


# --- общая расстановка для боевых тестов ---

ZONES = [
    Zone(id="hall", name="Зал"),
    Zone(id="stairs", name="Лестница"),
    Zone(id="vault", name="Хранилище"),
]
LINKS = [
    ZoneLink(a="hall", b="stairs", distance="near"),
    ZoneLink(a="hall", b="vault", distance="far"),
]
HERO, ALLY = "pc-1", "pc-2"
GOBLIN, GOBLIN_2, OGRE = "goblin_warrior-1", "goblin_warrior-2", "ogre-1"


def item(item_id: str) -> InventoryItem:
    return InventoryItem(item_id=item_id, equipped=True)


@pytest.fixture
def make_hero(make_character):
    """Воин 3-го уровня, Сила 16 (+3), длинный меч: атака +5, урон 1к8+3, КД 10.

    Предыстория «Служитель» (черта «Посвящённый в магию»), чтобы случайно не получить
    «Свирепого атакующего» от предыстории по умолчанию."""

    def factory(**overrides) -> Character:
        fields = {
            "id": HERO,
            "name": "Герой",
            "background": "acolyte",
            "scores": {STR: 16},
            "inventory": [item("longsword")],
        }
        return make_character(**(fields | overrides))

    return factory


def state_of(*characters: Character) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RulesRef(),
        party={c.id: c for c in characters},
        location_id="entrance",
    )


def standing(c: Character) -> Character:
    """Персонаж на ногах на время начала боя: лежащий первым бросал бы спасбросок от смерти."""
    if is_dead(c) or c.hp > 0:
        return c
    return c.model_copy(update={"hp": 1, "conditions": []})


def battle(
    srd: SrdContent,
    party: list[Character],
    monsters: tuple[str, ...] = ("goblin_warrior",),
    *,
    monster_zone: str = "hall",
) -> GameState:
    """Бой в зале: отряд ходит первым (инициатива 20, 19, ...), монстры — после.

    Упавшие персонажи ложатся после начала боя: он идёт, пока они уже лежат, но кубов на
    спасбросок от смерти первого хода у теста нет."""
    living = [c for c in party if not is_dead(c)]
    rolls = [20 - i for i in range(len(living))] + [5 - i for i in range(len(monsters))]
    state, _ = start_combat(
        state_of(*map(standing, party)),
        encounter_id="test",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters[monster_id], monster_zone) for monster_id in monsters],
        pc_zone="hall",
        rng=ScriptedRng(rolls),
        content=srd,
    )
    state.party = {c.id: c for c in party}
    return state


def melee(state: GameState, a: str, b: str) -> GameState:
    """Расстановка для теста: бойцы вовлечены друг с другом."""
    state.combat.combatants[a].engaged_with.append(b)
    state.combat.combatants[b].engaged_with.append(a)
    return state


def fighter(state: GameState, fighter_id: str):
    return state.combat.combatants[fighter_id]


def readied(state: GameState, *fighter_ids: str) -> GameState:
    """Расстановка для теста: бойцы подготовили действие («Подготовка») и ждут условия."""
    for fighter_id in fighter_ids:
        fighter(state, fighter_id).turn_flags["readied"] = True
    return state


def with_armor_class(state: GameState, fighter_id: str, armor_class: int) -> GameState:
    fighter(state, fighter_id).ac = armor_class
    return state


def all_dice_spent(rng: ScriptedRng) -> bool:
    try:
        rng.randint(1, 100)
    except ValueError:
        return True
    return False


def strike(srd, state, rolls, attacker=HERO, target=GOBLIN, **kwargs):
    """Атака на заданных кубах; заодно проверяет, что кубы израсходованы без остатка."""
    kwargs = {"weapon": "longsword"} | kwargs if attacker.startswith("pc-") else kwargs
    rng = ScriptedRng(rolls)
    after, events = attack(state, attacker, target, rng=rng, content=srd, **kwargs)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def refused(srd, state, reason, attacker=HERO, target=GOBLIN, **kwargs):
    """Атака отклонена: CombatError, состояние и кубы не тронуты."""
    kwargs = {"weapon": "longsword"} | kwargs if attacker.startswith("pc-") else kwargs
    snapshot = state.model_copy(deep=True)
    rng = ScriptedRng([10])
    with pytest.raises(CombatError, match=reason):
        attack(state, attacker, target, rng=rng, content=srd, **kwargs)
    assert state == snapshot
    assert rng.randint(1, 20) == 10


def damage_events(events) -> list[DamageEvent]:
    return [e for e in events if isinstance(e, DamageEvent)]


def roll_events(events) -> list[RollEvent]:
    return [e for e in events if isinstance(e, RollEvent)]


def conds(*ids: str) -> list[ActiveCondition]:
    return [ActiveCondition(id=condition_id) for condition_id in ids]


# --- таблица состояний ---


def test_every_condition_in_the_table_exists_in_srd_data(srd):
    assert set(CONDITION_EFFECTS) <= set(srd.conditions)


def test_the_condition_table_is_read_only():
    with pytest.raises(TypeError):
        CONDITION_EFFECTS["blinded"] = CONDITION_EFFECTS["prone"]  # type: ignore[index]


@pytest.mark.parametrize(
    "condition_id", ["blinded", "frightened", "grappled", "poisoned", "prone", "restrained"]
)
def test_these_conditions_give_disadvantage_on_own_attacks(condition_id):
    assert attack_modes(conds(condition_id), "target") == [DISADVANTAGE]


def test_invisible_attacker_has_advantage():
    assert attack_modes(conds("invisible"), "target") == [ADVANTAGE]


def test_grappled_attacker_is_not_hindered_against_the_grappler():
    grappled = [ActiveCondition(id="grappled", source="bugbear_warrior-1")]

    assert attack_modes(grappled, "bugbear_warrior-1") == []
    assert attack_modes(grappled, "goblin_warrior-1") == [DISADVANTAGE]


@pytest.mark.parametrize("condition_id", ["charmed", "deafened", "incapacitated", "paralyzed"])
def test_other_conditions_do_not_change_own_attacks(condition_id):
    assert attack_modes(conds(condition_id), "target") == []


@pytest.mark.parametrize("close", [True, False])
@pytest.mark.parametrize(
    "condition_id", ["blinded", "paralyzed", "petrified", "restrained", "stunned"]
)
def test_attacks_against_these_conditions_have_advantage_at_any_range(condition_id, close):
    assert defense_modes(conds(condition_id), attacker_close=close) == [ADVANTAGE]


@pytest.mark.parametrize("close", [True, False])
def test_attacks_against_invisible_have_disadvantage(close):
    assert defense_modes(conds("invisible"), attacker_close=close) == [DISADVANTAGE]


def test_prone_gives_advantage_up_close_and_disadvantage_from_afar():
    assert defense_modes(conds("prone"), attacker_close=True) == [ADVANTAGE]
    assert defense_modes(conds("prone"), attacker_close=False) == [DISADVANTAGE]


def test_unconscious_includes_prone_so_ranged_attacks_cancel_out():
    close = combine_advantage(*defense_modes(conds("unconscious"), attacker_close=True))
    far = combine_advantage(*defense_modes(conds("unconscious"), attacker_close=False))

    assert (close, far) == (ADVANTAGE, NONE)


@pytest.mark.parametrize("condition_id", ["charmed", "poisoned", "frightened", "grappled"])
def test_other_conditions_do_not_change_attacks_against_the_creature(condition_id):
    assert defense_modes(conds(condition_id), attacker_close=True) == []


@pytest.mark.parametrize(("condition_id", "expected"), [("paralyzed", True), ("unconscious", True)])
def test_hits_up_close_are_critical_against_paralyzed_and_unconscious(condition_id, expected):
    assert auto_crit(conds(condition_id), attacker_close=True) is expected
    assert auto_crit(conds(condition_id), attacker_close=False) is False


@pytest.mark.parametrize("condition_id", ["petrified", "stunned", "blinded", "prone", "restrained"])
def test_other_conditions_never_make_hits_critical(condition_id):
    assert auto_crit(conds(condition_id), attacker_close=True) is False


@pytest.mark.parametrize("condition_id", ["incapacitated", "paralyzed", "petrified", "stunned"])
def test_these_conditions_incapacitate(condition_id):
    assert is_incapacitated(conds(condition_id)) is True


def test_unconscious_incapacitates_and_prone_does_not():
    assert is_incapacitated(conds("unconscious")) is True
    assert is_incapacitated(conds("prone", "poisoned", "restrained")) is False


@pytest.mark.parametrize(
    "condition_id", ["grappled", "paralyzed", "petrified", "restrained", "unconscious"]
)
def test_these_conditions_set_speed_to_zero(condition_id):
    assert cannot_move(conds(condition_id)) is True


# В SRD 5.2.1 Ошеломлён нулевой скорости не даёт, только недееспособность.
@pytest.mark.parametrize("condition_id", ["stunned", "prone", "incapacitated", "poisoned"])
def test_other_conditions_leave_the_creature_able_to_move(condition_id):
    assert cannot_move(conds(condition_id)) is False


@pytest.mark.parametrize("condition_id", ["paralyzed", "petrified", "stunned", "unconscious"])
@pytest.mark.parametrize("ability", [STR, DEX])
def test_these_conditions_fail_strength_and_dexterity_saves(condition_id, ability):
    assert auto_fail_condition(conds("poisoned", condition_id), ability) == condition_id


@pytest.mark.parametrize("ability", [CON, INT, WIS, CHA])
def test_no_condition_fails_the_other_saves_automatically(ability):
    every_condition = conds(*CONDITION_EFFECTS)

    assert auto_fail_condition(every_condition, ability) is None


def test_only_petrified_resists_all_damage():
    assert resists_all_damage(conds("petrified")) is True
    assert resists_all_damage(conds(*(c for c in CONDITION_EFFECTS if c != "petrified"))) is False


def test_marks_and_unknown_ids_in_the_condition_list_have_no_effect():
    marks = conds("guiding_bolt", "hunters_mark", "something_custom")

    assert attack_modes(marks, "t") == []
    assert defense_modes(marks, attacker_close=True) == []
    assert (is_incapacitated(marks), cannot_move(marks), auto_crit(marks, attacker_close=True)) == (
        False,
        False,
        False,
    )
    assert auto_fail_condition(marks, STR) is None


# --- смерть и «без сознания» от 0 HP ---


def test_character_is_dead_after_three_death_save_failures(make_character):
    assert is_dead(make_character(death_saves=DeathSaves(failures=3))) is True
    assert is_dead(make_character(death_saves=DeathSaves(failures=2, successes=3))) is False


def test_regaining_consciousness_removes_only_the_unconsciousness_from_zero_hp(make_character):
    sleeping = ActiveCondition(id="unconscious", source="sleep_spell")
    knocked_out = ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)
    c: Character = make_character(
        hp=0,
        conditions=[knocked_out, sleeping, ActiveCondition(id="poisoned")],
        death_saves=DeathSaves(successes=2, failures=1, stable=True),
    )

    regain_consciousness(c)

    assert c.conditions == [sleeping, ActiveCondition(id="poisoned")]
    assert c.death_saves == DeathSaves()


# --- атака: попадание, промах, критическое попадание ---


def test_attack_hits_when_the_total_reaches_the_armor_class(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, events = strike(srd, state, [10, 4])

    attack_roll, damage_roll = events[0], events[1]
    assert (attack_roll.actor_id, attack_roll.roll.natural, attack_roll.roll.total) == (
        HERO,
        10,
        15,
    )
    assert (attack_roll.target_ac, attack_roll.success, attack_roll.audience) == (15, True, "all")
    assert (damage_roll.roll.expr, damage_roll.roll.total) == ("1d8+3", 7)
    assert events[2] == DamageEvent(target_id=GOBLIN, amount=7, damage_type="slashing", hp_after=3)
    assert len(events) == 3
    assert after.combat.combatants[GOBLIN].hp == 3


def test_attack_misses_one_below_the_armor_class_and_rolls_no_damage(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, events = strike(srd, state, [9])

    assert len(events) == 1
    assert (events[0].roll.total, events[0].target_ac, events[0].success) == (14, 15, False)
    assert after.combat.combatants[GOBLIN].hp == 10


def test_natural_20_hits_whatever_the_armor_class_and_doubles_the_damage_dice(srd, make_hero):
    state = melee(with_armor_class(battle(srd, [make_hero()], ("ogre",)), OGRE, 30), HERO, OGRE)

    after, events = strike(srd, state, [20, 3, 5], target=OGRE)

    assert events[0].success is True
    assert (events[1].roll.expr, events[1].roll.total) == ("2d8+3", 11)  # кости дважды, бонус раз
    assert damage_events(events) == [
        DamageEvent(target_id=OGRE, amount=11, damage_type="slashing", hp_after=57)
    ]
    assert after.combat.combatants[OGRE].hp == 57


def test_natural_1_misses_whatever_the_bonus(srd, make_hero):
    state = melee(with_armor_class(battle(srd, [make_hero()]), GOBLIN, 5), HERO, GOBLIN)

    _, events = strike(srd, state, [1])

    assert (events[0].roll.total, events[0].target_ac, events[0].success) == (6, 5, False)


@pytest.mark.parametrize(
    ("overrides", "crits_on_19"),
    [
        ({}, True),  # Воин 3-го уровня — Чемпион: Улучшенный критический удар
        ({"level": 2}, False),  # особенность подкласса ещё не получена
        ({"class_id": "barbarian"}, False),
    ],
    ids=["чемпион", "воин 2-го уровня", "варвар"],
)
def test_champion_crits_on_a_natural_19(srd, make_hero, overrides, crits_on_19):
    hero = make_hero(**overrides)
    state = melee(with_armor_class(battle(srd, [hero], ("ogre",)), OGRE, 30), HERO, OGRE)

    # 19 + 5 = 24 против КД 30: попасть можно только критическим попаданием.
    _, events = strike(srd, state, [19, 3, 5] if crits_on_19 else [19], target=OGRE)

    assert events[0].success is crits_on_19


def test_improvised_attack_does_not_crit_on_19(srd, make_hero):
    state = melee(with_armor_class(battle(srd, [make_hero()], ("ogre",)), OGRE, 30), HERO, OGRE)

    _, events = strike(srd, state, [19], target=OGRE, weapon="improvised")

    assert events[0].success is False  # подручный предмет — не оружие


# --- атака: источники преимущества и помехи ---


def first_attack_roll(events):
    return roll_events(events)[0].roll


def test_attacker_condition_with_disadvantage_rolls_two_dice_and_keeps_the_lower(srd, make_hero):
    hero = make_hero(conditions=conds("poisoned"))
    state = melee(battle(srd, [hero]), HERO, GOBLIN)

    _, events = strike(srd, state, [18, 4])

    roll = first_attack_roll(events)
    assert (roll.advantage, roll.rolls, roll.natural) == (DISADVANTAGE, [18, 4], 4)
    assert events[0].success is False


def test_target_condition_with_advantage_rolls_two_dice_and_keeps_the_higher(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="blinded"))

    _, events = strike(srd, state, [4, 18, 5])

    roll = first_attack_roll(events)
    assert (roll.advantage, roll.natural) == (ADVANTAGE, 18)
    assert events[0].success is True


def test_advantage_requested_by_the_caller_is_used(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    _, events = strike(srd, state, [4, 18, 5], advantage=ADVANTAGE)

    assert first_attack_roll(events).advantage == ADVANTAGE


def test_advantage_and_disadvantage_from_different_sources_cancel_out(srd, make_hero):
    hero = make_hero(conditions=conds("poisoned"))
    state = melee(battle(srd, [hero]), HERO, GOBLIN)
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="blinded"))

    _, events = strike(srd, state, [10, 4])

    assert first_attack_roll(events).advantage == NONE


def test_reckless_attacker_has_advantage_on_strength_attacks_only(srd, make_hero):
    reckless = make_hero(scores={STR: 16, DEX: 14}, inventory=[item("longsword"), item("shortbow")])
    state = melee(battle(srd, [reckless]), HERO, GOBLIN)
    fighter(state, HERO).turn_flags["reckless"] = True

    _, with_sword = strike(srd, state, [4, 18, 5])
    ranged = battle(srd, [reckless], monster_zone="stairs")
    fighter(ranged, HERO).turn_flags["reckless"] = True
    _, with_bow = strike(srd, ranged, [11, 3], weapon="shortbow")

    assert first_attack_roll(with_sword).advantage == ADVANTAGE
    assert first_attack_roll(with_bow).advantage == NONE  # лук бьёт Ловкостью


def test_attacks_against_a_reckless_fighter_have_advantage(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).turn_flags["reckless"] = True

    _, events = strike(srd, state, [4, 18, 5])

    assert first_attack_roll(events).advantage == ADVANTAGE


def test_attacks_against_a_dodging_fighter_have_disadvantage(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).dodging = True

    _, events = strike(srd, state, [18, 4])

    assert (first_attack_roll(events).advantage, events[0].success) == (DISADVANTAGE, False)


def test_dodging_does_not_help_a_fighter_who_cannot_react(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).dodging = True
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="stunned"))

    _, events = strike(srd, state, [4, 18, 5])  # ошеломлённый: преимущество, а не помеха

    assert first_attack_roll(events).advantage == ADVANTAGE


def test_guiding_bolt_gives_the_next_attack_advantage_and_is_used_up(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).conditions.append(
        ActiveCondition(id="guiding_bolt", rounds_left=1, source="pc-9")
    )

    after, events = strike(srd, state, [4, 18, 5], target=OGRE)
    after.combat.combatants[HERO].turn_flags.clear()  # следующий ход: действие снова есть
    _, second = strike(srd, after, [10, 5], target=OGRE)

    assert first_attack_roll(events).advantage == ADVANTAGE
    assert ConditionEvent(target_id=OGRE, condition="guiding_bolt", added=False) in events
    assert fighter(after, OGRE).conditions == []
    assert first_attack_roll(second).advantage == NONE


def test_guiding_bolt_is_used_up_by_a_miss_too(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="guiding_bolt", source="pc-9"))

    after, _ = strike(srd, state, [2, 3])

    assert fighter(after, GOBLIN).conditions == []


def test_prone_target_is_easy_up_close_and_hard_from_afar(srd, make_hero):
    archer = make_hero(scores={DEX: 14}, inventory=[item("shortbow"), item("longsword")])
    close = melee(battle(srd, [archer]), HERO, GOBLIN)
    fighter(close, GOBLIN).conditions.append(ActiveCondition(id="prone"))
    far = battle(srd, [archer], monster_zone="stairs")
    fighter(far, GOBLIN).conditions.append(ActiveCondition(id="prone"))

    _, up_close = strike(srd, close, [4, 18, 5])
    _, from_afar = strike(srd, far, [18, 4], weapon="shortbow")

    assert first_attack_roll(up_close).advantage == ADVANTAGE
    assert first_attack_roll(from_afar).advantage == DISADVANTAGE


def test_hit_up_close_on_an_unconscious_target_is_always_critical(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="unconscious", source="sleep"))

    _, events = strike(srd, state, [10, 12, 2, 3])  # преимущество: 12 + 5 = 17 ≥ 15

    assert (events[1].roll.expr, events[1].roll.total) == ("2d8+3", 8)  # критический урон


def test_hit_on_a_paralyzed_target_is_not_critical_from_afar(srd, make_hero):
    archer = make_hero(scores={DEX: 14}, inventory=[item("shortbow")])
    state = battle(srd, [archer], monster_zone="stairs")
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="paralyzed"))

    _, events = strike(srd, state, [4, 18, 3], weapon="shortbow")

    assert events[1].roll.expr == "1d6+2"


# --- атака: дальность и вовлечённость ---


def test_melee_attack_needs_a_target_engaged_with_the_attacker(srd, make_hero):
    in_the_same_zone = battle(srd, [make_hero()])  # в одной зоне, но не вовлечены
    in_another_zone = battle(srd, [make_hero()], monster_zone="stairs")

    refused(srd, in_the_same_zone, "вовлечён")
    refused(srd, in_another_zone, "вовлечён")


def test_attack_reaches_beyond_engagement_with_a_reach_monster_in_the_same_zone(srd, make_hero):
    # Воин-багбир: досягаемость 10 футов — бьёт врага в своей зоне, не вовлечённого с ним.
    same_zone = readied(battle(srd, [make_hero()], ("bugbear_warrior",)), "bugbear_warrior-1")

    after, events = strike(
        srd,
        same_zone,
        [10, 3, 4],
        attacker="bugbear_warrior-1",
        target=HERO,
        action_id="grab",
        reaction=True,
    )

    assert damage_events(events)[0].amount == 3 + 4 + 2
    assert after.party[HERO].hp == 20 - 9
    other_zone = battle(srd, [make_hero()], ("bugbear_warrior",), monster_zone="stairs")
    refused(
        srd,
        readied(other_zone, "bugbear_warrior-1"),
        "вовлечён",
        attacker="bugbear_warrior-1",
        target=HERO,
        action_id="grab",
        reaction=True,
    )


def test_a_monster_without_reach_cannot_hit_an_unengaged_enemy_of_its_zone(srd, make_hero):
    state = readied(battle(srd, [make_hero()]), GOBLIN)

    refused(
        srd, state, "вовлечён", attacker=GOBLIN, target=HERO, action_id="scimitar", reaction=True
    )


def test_ranged_attack_on_an_engaged_target_has_disadvantage(srd, make_hero):
    archer = make_hero(scores={DEX: 14}, inventory=[item("shortbow")])
    state = melee(battle(srd, [archer]), HERO, GOBLIN)

    _, events = strike(srd, state, [18, 4], weapon="shortbow")

    assert (first_attack_roll(events).advantage, events[0].success) == (DISADVANTAGE, False)


@pytest.mark.parametrize("monster_zone", ["hall", "stairs", "vault"])
def test_ranged_attack_on_a_target_that_is_not_engaged_is_a_plain_roll(
    srd, make_hero, monster_zone
):
    archer = make_hero(scores={DEX: 14}, inventory=[item("shortbow")])
    state = battle(srd, [archer], monster_zone=monster_zone)

    after, events = strike(srd, state, [11, 3], weapon="shortbow")

    assert first_attack_roll(events).advantage == NONE
    assert (events[0].roll.total, events[1].roll.expr) == (15, "1d6+2")  # Лов +2, мастерство +2
    assert after.combat.combatants[GOBLIN].hp == 5


def archer_with_two_goblins(srd, make_hero, second_zone="hall", **overrides):
    """Лучник вовлечён с первым гоблином; второй стоит в `second_zone` и с ним не вовлечён."""
    archer = make_hero(scores={DEX: 14}, inventory=[item("shortbow")], **overrides)
    state = battle(srd, [archer], ("goblin_warrior", "goblin_warrior"))
    fighter(state, GOBLIN_2).zone_id = second_zone
    return melee(state, HERO, GOBLIN)


@pytest.mark.parametrize("second_zone", ["hall", "stairs", "vault"])
def test_ranged_attack_at_a_free_target_has_disadvantage_while_another_enemy_is_next_to_the_archer(
    srd, make_hero, second_zone
):
    state = archer_with_two_goblins(srd, make_hero, second_zone)

    _, events = strike(srd, state, [2, 18], target=GOBLIN_2, weapon="shortbow")

    assert (first_attack_roll(events).advantage, events[0].success) == (DISADVANTAGE, False)


def test_thrown_improvised_weapon_also_has_disadvantage_while_an_enemy_is_next_to_the_thrower(
    srd, make_hero
):
    state = archer_with_two_goblins(srd, make_hero, "stairs")

    _, events = strike(srd, state, [2, 18], target=GOBLIN_2, weapon="improvised")

    assert first_attack_roll(events).advantage == DISADVANTAGE


def test_a_monster_archer_is_hindered_by_an_enemy_next_to_it_too(srd, make_hero, make_character):
    ally = make_character(id=ALLY, name="Союзник")
    state = melee(battle(srd, [make_hero(), ally]), HERO, GOBLIN)
    state.combat.turn_index = state.combat.order.index(GOBLIN)

    _, events = strike(srd, state, [2, 18], attacker=GOBLIN, target=ALLY, action_id="shortbow")

    assert first_attack_roll(events).advantage == DISADVANTAGE


def test_melee_attack_is_not_hindered_by_other_enemies_next_to_the_attacker(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("goblin_warrior", "goblin_warrior")), HERO, GOBLIN)
    melee(state, HERO, GOBLIN_2)

    _, events = strike(srd, state, [2], target=GOBLIN_2)

    assert first_attack_roll(events).advantage == NONE


def test_an_engaged_enemy_who_cannot_act_does_not_hinder_ranged_attacks(srd, make_hero):
    state = archer_with_two_goblins(srd, make_hero)
    fighter(state, GOBLIN).conditions = conds("paralyzed")

    _, at_free = strike(srd, state, [2], target=GOBLIN_2, weapon="shortbow")
    _, at_paralyzed = strike(srd, state, [2, 3], target=GOBLIN, weapon="shortbow")

    assert first_attack_roll(at_free).advantage == NONE
    # Парализованная цель сама даёт преимущество, и помехи от неё же нет.
    assert first_attack_roll(at_paralyzed).advantage == ADVANTAGE


def test_a_second_capable_enemy_next_to_the_archer_still_hinders_a_shot_at_the_paralyzed_one(
    srd, make_hero
):
    state = archer_with_two_goblins(srd, make_hero)
    melee(state, HERO, GOBLIN_2)
    fighter(state, GOBLIN).conditions = conds("paralyzed")

    _, events = strike(srd, state, [2], target=GOBLIN, weapon="shortbow")

    # Преимущество от парализованной цели и помеха от второго врага гасят друг друга: одна кость.
    assert first_attack_roll(events).advantage == NONE


def test_improvised_attack_is_melee_up_close_and_thrown_at_near_range(srd, make_hero):
    up_close = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    near = battle(srd, [make_hero()], monster_zone="stairs")

    _, in_melee = strike(srd, up_close, [12, 2], weapon="improvised")
    after, thrown = strike(srd, near, [12, 2], weapon="improvised")

    # Сила +3 без бонуса мастерства; урон 1к4+3 дробящий.
    for events in (in_melee, thrown):
        assert (events[0].roll.total, events[1].roll.expr) == (15, "1d4+3")
        assert damage_events(events)[0].damage_type == "bludgeoning"
        assert first_attack_roll(events).advantage == NONE
    assert after.combat.combatants[GOBLIN].hp == 10 - 5


def test_improvised_attack_cannot_reach_a_far_target(srd, make_hero):
    refused(
        srd,
        battle(srd, [make_hero()], monster_zone="vault"),
        "далеко",
        weapon="improvised",
    )


# --- атака: кто, когда и чем ---


def test_attack_spends_the_action_and_a_second_attack_in_the_turn_is_refused(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, _ = strike(srd, state, [9])

    assert fighter(after, HERO).turn_flags == {"action": True}
    refused(srd, after, "действие")


def test_reaction_attack_spends_the_reaction_and_the_readied_action_not_the_action(srd, make_hero):
    state = readied(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)

    after, events = strike(
        srd, state, [6, 3], attacker=GOBLIN, target=HERO, action_id="scimitar", reaction=True
    )

    # Ход героя, а бьёт гоблин: реакции не нужен свой ход. 6 + 4 = 10 против КД 10.
    assert fighter(after, GOBLIN).turn_flags == {"reaction": True}  # подготовка потрачена
    assert events[0].label == "Подготовленная атака: Скимитар"
    assert after.party[HERO].hp == 20 - 5
    refused(srd, after, "реакци", attacker=GOBLIN, target=HERO, action_id="scimitar", reaction=True)


def test_a_reaction_attack_without_a_readied_action_is_refused(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    refused(
        srd, state, "подготов", attacker=GOBLIN, target=HERO, action_id="scimitar", reaction=True
    )


def test_the_public_attack_cannot_pose_as_an_attack_of_opportunity(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    with pytest.raises(TypeError):
        attack(
            state,
            GOBLIN,
            HERO,
            action_id="scimitar",
            kind="opportunity",
            rng=ScriptedRng([10]),
            content=srd,
        )


def test_a_character_readies_an_action_and_attacks_when_the_trigger_comes(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    ready, _ = simple_action(state, HERO, "ready", rng=ScriptedRng([]), content=srd)
    on_goblin, _ = end_turn(ready, rng=ScriptedRng([]), content=srd)

    after, events = strike(srd, on_goblin, [15, 4], target=GOBLIN, reaction=True)

    assert events[0].label == "Подготовленная атака: Длинный меч"
    assert fighter(after, HERO).turn_flags == {"action": True, "reaction": True}
    assert fighter(after, GOBLIN).hp == 10 - 7


def test_a_readied_action_does_not_outlive_the_start_of_the_fighters_next_turn(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    ready, _ = simple_action(state, HERO, "ready", rng=ScriptedRng([]), content=srd)
    on_goblin, _ = end_turn(ready, rng=ScriptedRng([]), content=srd)
    back_on_hero, _ = end_turn(on_goblin, rng=ScriptedRng([]), content=srd)

    refused(srd, back_on_hero, "подготов", target=GOBLIN, reaction=True)


def test_attack_outside_the_attackers_turn_needs_a_reaction(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    refused(srd, state, "не ход", attacker=GOBLIN, target=HERO, action_id="scimitar")


def test_a_monster_attacks_with_a_named_action_on_its_own_turn(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    state.combat.turn_index = 1

    after, events = strike(srd, state, [6, 3], attacker=GOBLIN, target=HERO, action_id="scimitar")

    assert (events[0].label, events[0].roll.total, events[0].target_ac) == (
        "Атака: Скимитар",
        10,
        10,
    )
    assert (fighter(after, GOBLIN).turn_flags, after.party[HERO].hp) == ({"action": True}, 15)


def test_spent_action_blocks_attack_after_a_dash(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, HERO).turn_flags.update(action=True, dashed=True)

    refused(srd, state, "действие")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"weapon": "dagger"}, "нет предмета"),  # кинжала в инвентаре нет
        ({"weapon": "leather_armor"}, "нет предмета"),
        ({"weapon": "mystery_blade"}, "нет предмета"),
        ({"weapon": None}, "оружие"),
        ({"weapon": "longsword", "action_id": "scimitar"}, "либо"),
    ],
)
def test_attack_with_an_unusable_weapon_is_a_combat_error(srd, make_hero, kwargs, reason):
    state = melee(battle(srd, [make_hero(inventory=[item("longsword")])]), HERO, GOBLIN)

    refused(srd, state, reason, **kwargs)


def test_armor_in_the_inventory_is_not_a_weapon(srd, make_hero):
    hero = make_hero(inventory=[item("longsword"), item("leather_armor")])
    state = melee(battle(srd, [hero]), HERO, GOBLIN)

    refused(srd, state, "не оружие", weapon="leather_armor")


@pytest.mark.parametrize(
    ("monster_id", "kwargs", "reason"),
    [
        ("goblin_warrior", {"action_id": "nimble_escape"}, "не атака"),
        ("goblin_boss", {"action_id": "multiattack"}, "не атака"),
        ("goblin_warrior", {"action_id": "katana"}, "нет действия"),
        ("goblin_warrior", {}, "действие монстра"),
        ("goblin_warrior", {"action_id": "scimitar", "weapon": "longsword"}, "либо"),
    ],
)
def test_monster_attack_needs_one_of_its_attack_actions(srd, make_hero, monster_id, kwargs, reason):
    state = readied(battle(srd, [make_hero()], (monster_id,)), f"{monster_id}-1")
    melee(state, HERO, f"{monster_id}-1")

    refused(srd, state, reason, attacker=f"{monster_id}-1", target=HERO, reaction=True, **kwargs)


def test_attacking_an_ally_or_oneself_is_a_combat_error(srd, make_hero, make_character):
    ally = make_character(id=ALLY, name="Союзник")
    state = melee(battle(srd, [make_hero(), ally]), HERO, GOBLIN)

    refused(srd, state, "союзник", target=ALLY)
    refused(srd, state, "союзник", target=HERO)


def test_a_dead_monster_cannot_be_attacked(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).hp = 0

    refused(srd, state, "мёртв")


def test_a_monster_missing_from_the_content_takes_damage_without_defenses_but_cannot_act(
    srd, make_hero
):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    fighter(state, GOBLIN).ref = "homebrew_ghost"  # статблока нет в данных SRD

    # Защит у него нет, урон проходит как есть (КД и HP хранит сам боец) ...
    after, _ = hurt(srd, state, GOBLIN, 4, damage_type="bludgeoning")
    assert fighter(after, GOBLIN).hp == 6
    # ... а действовать без статблока он не может: отказ до первого куба.
    readied(state, GOBLIN)
    refused(srd, state, "неизвестный монстр", GOBLIN, HERO, action_id="scimitar", reaction=True)


def test_an_unknown_target_or_attacker_is_a_combat_error(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    refused(srd, state, "ghost", target="ghost")
    refused(srd, state, "ghost", attacker="ghost", weapon=None)


def test_attack_needs_an_ongoing_combat(srd, make_hero):
    with pytest.raises(CombatError, match="не идёт"):
        attack(
            state_of(make_hero()),
            HERO,
            GOBLIN,
            weapon="longsword",
            rng=ScriptedRng([]),
            content=srd,
        )


@pytest.mark.parametrize("condition_id", ["incapacitated", "stunned", "paralyzed", "petrified"])
def test_an_incapacitated_fighter_cannot_attack(srd, make_hero, condition_id):
    state = melee(battle(srd, [make_hero(conditions=conds(condition_id))]), HERO, GOBLIN)

    refused(srd, state, "не может действовать")


def test_a_fighter_at_zero_hit_points_cannot_attack(srd, make_hero):
    state = melee(battle(srd, [make_hero(hp=0)]), HERO, GOBLIN)

    refused(srd, state, "без сознания|мёртв|не может")


def test_attack_does_not_change_the_original_state_and_survives_json(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    snapshot = state.model_copy(deep=True)

    after, _ = strike(srd, state, [10, 4])

    assert state == snapshot
    assert after is not state
    assert GameState.model_validate_json(after.model_dump_json()) == after


# --- урон: добавки ---


RAGE = ActiveBuff(id="rage", rounds_left=10, source_id=HERO)


def labels(events) -> list[str]:
    return [e.label for e in roll_events(events)]


def test_rage_adds_2_to_strength_melee_damage_and_not_to_dexterity_damage(srd, make_hero):
    raging = make_hero(
        class_id="barbarian",
        scores={STR: 16, DEX: 14},
        inventory=[item("longsword"), item("shortbow")],
        buffs=[RAGE],
    )
    state = melee(battle(srd, [raging], ("ogre",)), HERO, OGRE)
    far = battle(srd, [raging], ("ogre",), monster_zone="stairs")

    _, with_sword = strike(srd, state, [10, 4], target=OGRE)
    _, with_bow = strike(srd, far, [10, 4], target=OGRE, weapon="shortbow")

    assert damage_events(with_sword)[0].amount == 4 + 3 + 2
    assert damage_events(with_bow)[0].amount == 4 + 2  # Ловкость: ярость не добавляет


def test_rage_adds_nothing_without_the_rage_buff(srd, make_hero):
    state = melee(battle(srd, [make_hero(class_id="barbarian")], ("ogre",)), HERO, OGRE)

    _, events = strike(srd, state, [10, 4], target=OGRE)

    assert damage_events(events)[0].amount == 4 + 3


def rogue(make_character, **overrides) -> Character:
    """Плут 3-го уровня: Лов 16 (+3), рапира (фехтовальная): атака +5, урон 1к8+3."""
    fields = {
        "id": HERO,
        "name": "Плут",
        "class_id": "rogue",
        "background": "acolyte",
        "scores": {DEX: 16},
        "inventory": [item("rapier"), item("shortbow"), item("longsword")],
    }
    return make_character(**(fields | overrides))


def rogue_battle(srd, make_character, *, ally_engaged=True, **overrides) -> GameState:
    """Плут и союзник против огра (КД 11); плут вплотную с огром, союзник — по желанию."""
    ally = make_character(id=ALLY, name="Союзник")
    state = battle(srd, [rogue(make_character, **overrides), ally], ("ogre",))
    melee(state, HERO, OGRE)
    return melee(state, ALLY, OGRE) if ally_engaged else state


def test_sneak_attack_with_an_ally_next_to_the_target_adds_2d6_of_the_weapon_type(
    srd, make_character
):
    state = rogue_battle(srd, make_character)

    after, events = strike(srd, state, [10, 4, 3, 5], target=OGRE, weapon="rapier")

    assert labels(events)[2].startswith("Скрытая атака")
    assert roll_events(events)[2].roll.expr == "2d6"
    # Один тип урона — одно событие: 1к8+3 = 7 и 3+5 = 8.
    assert damage_events(events) == [
        DamageEvent(target_id=OGRE, amount=15, damage_type="piercing", hp_after=53)
    ]
    assert fighter(after, HERO).turn_flags == {"action": True, "sneak_attack_used": True}


def test_sneak_attack_needs_an_ally_or_advantage(srd, make_character):
    alone = rogue_battle(srd, make_character, ally_engaged=False)

    _, events = strike(srd, alone, [10, 4], target=OGRE, weapon="rapier")

    assert damage_events(events)[0].amount == 7  # 1к8+3 без скрытой атаки


def test_sneak_attack_with_advantage_needs_no_ally(srd, make_character):
    alone = rogue_battle(srd, make_character, ally_engaged=False)
    fighter(alone, OGRE).conditions.append(ActiveCondition(id="blinded"))

    _, events = strike(srd, alone, [10, 12, 4, 3, 5], target=OGRE, weapon="rapier")

    assert damage_events(events)[0].amount == 15


def test_sneak_attack_is_lost_to_disadvantage_even_next_to_an_ally(srd, make_character):
    state = rogue_battle(srd, make_character, conditions=conds("poisoned"))

    _, events = strike(srd, state, [10, 12, 4], target=OGRE, weapon="rapier")

    assert damage_events(events)[0].amount == 7


def test_an_incapacitated_ally_does_not_enable_sneak_attack(srd, make_character):
    state = rogue_battle(srd, make_character)
    state.party[ALLY].conditions.append(ActiveCondition(id="stunned"))  # у персонажей — в Character

    _, events = strike(srd, state, [10, 4], target=OGRE, weapon="rapier")

    assert damage_events(events)[0].amount == 7


def test_an_ally_engaged_with_someone_else_does_not_enable_sneak_attack(srd, make_character):
    state = rogue_battle(srd, make_character, ally_engaged=False)  # союзник рядом, но не с огром

    _, events = strike(srd, state, [10, 4], target=OGRE, weapon="rapier")

    assert damage_events(events)[0].amount == 7


def test_sneak_attack_needs_a_finesse_or_ranged_weapon(srd, make_character):
    state = rogue_battle(srd, make_character)

    # Длинный меч не фехтовальный: Сила 10, мастерство +2, урон 1к8.
    _, with_sword = strike(srd, state, [10, 4], target=OGRE, weapon="longsword")
    _, thrown = strike(srd, state, [12, 2], target=OGRE, weapon="improvised")

    assert damage_events(with_sword)[0].amount == 4
    assert damage_events(thrown)[0].amount == 2


def test_sneak_attack_works_with_a_ranged_weapon(srd, make_character):
    state = rogue_battle(srd, make_character)
    fighter(state, HERO).engaged_with.clear()  # плут стреляет, не вовлечённый с огром
    fighter(state, OGRE).engaged_with.remove(HERO)

    _, events = strike(srd, state, [10, 4, 3, 5], target=OGRE, weapon="shortbow")

    assert damage_events(events)[0].amount == (4 + 3) + (3 + 5)


def test_sneak_attack_happens_once_per_turn_and_a_miss_does_not_use_it_up(srd, make_character):
    state = rogue_battle(srd, make_character)

    after_miss, _ = strike(srd, state, [2], target=OGRE, weapon="rapier")
    after_miss.combat.combatants[HERO].turn_flags["action"] = False  # второе действие в этот ход
    after_hit, first = strike(srd, after_miss, [10, 4, 3, 5], target=OGRE, weapon="rapier")
    after_hit.combat.combatants[HERO].turn_flags["action"] = False
    _, second = strike(srd, after_hit, [10, 4], target=OGRE, weapon="rapier")

    assert damage_events(first)[0].amount == 15
    assert damage_events(second)[0].amount == 7


def test_critical_hit_doubles_the_sneak_attack_dice_too(srd, make_character):
    state = rogue_battle(srd, make_character)

    _, events = strike(srd, state, [20, 4, 4, 3, 5, 2, 6], target=OGRE, weapon="rapier")

    assert [e.roll.expr for e in roll_events(events)[1:]] == ["2d8+3", "4d6"]
    assert damage_events(events)[0].amount == (4 + 4 + 3) + (3 + 5 + 2 + 6)


@pytest.mark.parametrize(("level", "dice"), [(1, "1d6"), (2, "1d6"), (3, "2d6"), (4, "2d6")])
def test_sneak_attack_dice_grow_every_second_rogue_level(srd, make_character, level, dice):
    state = rogue_battle(srd, make_character, level=level)

    _, events = strike(srd, state, [10, 4, *[1] * int(dice[0])], target=OGRE, weapon="rapier")

    assert roll_events(events)[2].roll.expr == dice


def ranger(make_character, **overrides) -> Character:
    fields = {
        "id": HERO,
        "name": "Следопыт",
        "class_id": "ranger",
        "background": "acolyte",
        "scores": {STR: 16},
        "inventory": [item("longsword")],
    }
    return make_character(**(fields | overrides))


def test_colossus_slayer_adds_1d8_to_the_first_hit_on_a_wounded_target_each_turn(
    srd, make_character
):
    state = melee(battle(srd, [ranger(make_character)], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).hp = 60

    after, first = strike(srd, state, [10, 4, 5], target=OGRE)
    after.combat.combatants[HERO].turn_flags["action"] = False
    _, second = strike(srd, after, [10, 4], target=OGRE)

    assert labels(first)[2].startswith("Убийца колоссов")
    assert damage_events(first)[0].amount == (4 + 3) + 5
    assert damage_events(second)[0].amount == 7


def test_colossus_slayer_needs_a_wounded_target_and_the_feature(srd, make_character):
    full_health = melee(battle(srd, [ranger(make_character)], ("ogre",)), HERO, OGRE)
    low_level = melee(battle(srd, [ranger(make_character, level=2)], ("ogre",)), HERO, OGRE)
    fighter(low_level, OGRE).hp = 60

    _, at_full = strike(srd, full_health, [10, 4], target=OGRE)
    _, without = strike(srd, low_level, [10, 4], target=OGRE)

    assert damage_events(at_full)[0].amount == 7
    assert damage_events(without)[0].amount == 7


def test_hunters_mark_adds_1d6_force_damage_as_a_separate_type(srd, make_character):
    hero = ranger(make_character, concentration="hunters_mark")
    state = melee(battle(srd, [hero], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).conditions.append(ActiveCondition(id="hunters_mark", source=HERO))

    after, events = strike(srd, state, [10, 4, 5], target=OGRE)

    assert damage_events(events) == [
        DamageEvent(target_id=OGRE, amount=7, damage_type="slashing", hp_after=61),
        DamageEvent(target_id=OGRE, amount=5, damage_type="force", hp_after=56),
    ]
    assert after.combat.combatants[OGRE].hp == 56


def test_a_hunters_mark_of_another_ranger_adds_nothing(srd, make_character):
    state = melee(battle(srd, [ranger(make_character)], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).conditions.append(ActiveCondition(id="hunters_mark", source="pc-9"))

    _, events = strike(srd, state, [10, 4], target=OGRE)

    assert [e.damage_type for e in damage_events(events)] == ["slashing"]


def barbarian(make_character, **overrides) -> Character:
    fields = {
        "id": HERO,
        "name": "Варвар",
        "class_id": "barbarian",
        "background": "acolyte",
        "scores": {STR: 16, DEX: 14},
        "inventory": [item("longsword"), item("shortbow")],
        "buffs": [RAGE],
    }
    return make_character(**(fields | overrides))


def frenzied(srd, make_character, **overrides) -> GameState:
    state = melee(battle(srd, [barbarian(make_character, **overrides)], ("ogre",)), HERO, OGRE)
    fighter(state, HERO).turn_flags["reckless"] = True
    return state


def test_frenzy_adds_rage_damage_dice_to_the_first_hit_of_a_reckless_raging_barbarian(
    srd, make_character
):
    state = frenzied(srd, make_character)

    after, first = strike(srd, state, [4, 18, 5, 3, 6], target=OGRE)  # преимущество: две к20
    after.combat.combatants[HERO].turn_flags["action"] = False
    _, second = strike(srd, after, [4, 18, 5], target=OGRE)

    assert labels(first)[2].startswith("Бешенство")
    assert roll_events(first)[2].roll.expr == "2d6"
    assert damage_events(first)[0].amount == (5 + 3 + 2) + (3 + 6)
    assert damage_events(second)[0].amount == 5 + 3 + 2  # только раз за ход


def test_frenzy_needs_rage_reckless_attack_and_a_strength_attack(srd, make_character):
    no_rage = frenzied(srd, make_character, buffs=[])
    not_reckless = frenzied(srd, make_character)
    fighter(not_reckless, HERO).turn_flags.clear()
    level_two = frenzied(srd, make_character, level=2)
    shooting = battle(srd, [barbarian(make_character)], ("ogre",), monster_zone="stairs")
    fighter(shooting, HERO).turn_flags["reckless"] = True

    _, a = strike(srd, no_rage, [4, 18, 5], target=OGRE)
    _, b = strike(srd, not_reckless, [10, 5], target=OGRE)
    _, c = strike(srd, level_two, [4, 18, 5], target=OGRE)
    _, d = strike(srd, shooting, [10, 5], target=OGRE, weapon="shortbow")

    assert damage_events(a)[0].amount == 5 + 3  # ни ярости, ни бешенства
    assert damage_events(b)[0].amount == 5 + 3 + 2  # ярость есть, бешенства нет
    assert damage_events(c)[0].amount == 5 + 3 + 2  # бешенство с 3-го уровня
    assert damage_events(d)[0].amount == 5 + 2  # лук: Ловкость


def test_savage_attacker_rolls_weapon_damage_twice_and_keeps_the_better_roll(srd, make_hero):
    hero = make_hero(background="soldier")
    state = melee(battle(srd, [hero], ("ogre",)), HERO, OGRE)

    after, first = strike(srd, state, [10, 2, 6], target=OGRE)
    after.combat.combatants[HERO].turn_flags["action"] = False
    _, second = strike(srd, after, [10, 2], target=OGRE)

    assert [r.roll.total for r in roll_events(first)[1:]] == [2 + 3, 6 + 3]
    assert damage_events(first)[0].amount == 9
    assert len(roll_events(second)) == 2  # раз за ход: теперь одна кость
    assert damage_events(second)[0].amount == 5


def test_savage_attacker_applies_to_weapons_only_and_doubles_dice_on_a_crit(srd, make_hero):
    hero = make_hero(background="soldier")
    state = melee(battle(srd, [hero], ("ogre",)), HERO, OGRE)

    _, thrown = strike(srd, state, [12, 2], target=OGRE, weapon="improvised")
    _, crit = strike(srd, state, [20, 1, 2, 4, 5], target=OGRE)

    assert damage_events(thrown)[0].amount == 2 + 3
    assert damage_events(crit)[0].amount == max(1 + 2 + 3, 4 + 5 + 3)


def monster_hits(srd, state, monster_ref, rolls, action_id, target=HERO):
    """Монстр бьёт реакцией: ход героя, но проверяем только удар."""
    return strike(
        srd,
        readied(state, f"{monster_ref}-1"),
        rolls,
        attacker=f"{monster_ref}-1",
        target=target,
        action_id=action_id,
        reaction=True,
    )


def test_a_monster_hit_with_two_damage_types_rolls_and_applies_each_type_separately(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("ghoul",)), HERO, "ghoul-1")

    after, events = monster_hits(srd, state, "ghoul", [10, 3, 4], "bite")

    assert [r.roll.expr for r in roll_events(events)[1:]] == ["1d6+2", "1d6"]
    assert damage_events(events) == [
        DamageEvent(target_id=HERO, amount=5, damage_type="piercing", hp_after=15),
        DamageEvent(target_id=HERO, amount=4, damage_type="necrotic", hp_after=11),
    ]
    assert after.party[HERO].hp == 11


def test_a_critical_hit_doubles_the_dice_of_both_damage_types(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("ghoul",)), HERO, "ghoul-1")

    _, events = monster_hits(srd, state, "ghoul", [20, 3, 4, 5, 6], "bite")

    assert [r.roll.expr for r in roll_events(events)[1:]] == ["2d6+2", "2d6"]
    assert [d.amount for d in damage_events(events)] == [9, 11]


def test_resistance_to_one_damage_type_does_not_touch_the_other_type_of_the_same_hit(
    srd, make_hero
):
    raging = make_hero(class_id="barbarian", buffs=[RAGE])
    state = melee(battle(srd, [raging], ("ghoul",)), HERO, "ghoul-1")

    after, events = monster_hits(srd, state, "ghoul", [10, 3, 4], "bite")

    # Колющий урон 5 вдвое меньше (ярость), некротический 4 остаётся целиком.
    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [
        (2, "piercing"),
        (4, "necrotic"),
    ]
    assert after.party[HERO].hp == 20 - 6


def test_a_flat_secondary_damage_needs_no_dice(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("cultist",)), HERO, "cultist-1")

    _, hit = monster_hits(srd, state, "cultist", [10, 2], "ritual_sickle")
    _, crit = monster_hits(srd, state, "cultist", [20, 2, 3], "ritual_sickle")

    assert [(d.amount, d.damage_type) for d in damage_events(hit)] == [
        (3, "slashing"),
        (1, "necrotic"),
    ]
    assert [(d.amount, d.damage_type) for d in damage_events(crit)] == [
        (6, "slashing"),
        (1, "necrotic"),  # плоский урон при критическом попадании не удваивается
    ]


def test_damage_of_one_type_from_several_sources_is_summed_before_resistance(srd, make_character):
    ally = make_character(id=ALLY, name="Союзник")
    state = battle(srd, [rogue(make_character), ally], ("swarm_of_rats",))
    melee(state, HERO, "swarm_of_rats-1")
    melee(state, ALLY, "swarm_of_rats-1")

    # Рапира 2+3 = 5 и скрытая атака 1+2 = 3: вместе 8, вдвое меньше — 4 (по частям было бы 2+1).
    _, events = strike(srd, state, [12, 2, 1, 2], target="swarm_of_rats-1", weapon="rapier")

    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [(4, "piercing")]


# --- apply_damage: куда пишется урон ---

NO_DICE = ScriptedRng([])


def hurt(srd, state, target, amount, damage_type="slashing", rolls=(), **kwargs):
    """Урон на заданных кубах (кубы нужны только спасброску концентрации)."""
    rng = ScriptedRng(list(rolls))
    after, events = apply_damage(state, target, amount, damage_type, rng=rng, content=srd, **kwargs)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def downed(make_hero, **overrides) -> Character:
    """Персонаж на 0 HP: без сознания от нуля хитов, спасброски от смерти ещё не шли."""
    fields = {"hp": 0, "conditions": [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)]}
    return make_hero(**(fields | overrides))


def test_damage_to_a_character_changes_character_hit_points_not_the_combatant(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=20, hp=20)])

    after, events = hurt(srd, state, HERO, 7)

    assert after.party[HERO].hp == 13
    assert fighter(after, HERO).hp is None  # HP персонажа хранит только Character
    assert events == [DamageEvent(target_id=HERO, amount=7, damage_type="slashing", hp_after=13)]


def test_damage_to_a_monster_changes_the_combatant_hit_points(srd, make_hero):
    state = battle(srd, [make_hero()])

    after, events = hurt(srd, state, GOBLIN, 4, "piercing")

    assert (fighter(after, GOBLIN).hp, fighter(after, GOBLIN).hp_max) == (6, 10)
    assert events == [DamageEvent(target_id=GOBLIN, amount=4, damage_type="piercing", hp_after=6)]


def test_temporary_hit_points_are_spent_first(srd, make_hero):
    state = battle(srd, [make_hero(hp=10, temp_hp=6)])

    after, events = hurt(srd, state, HERO, 9)
    absorbed, absorbed_events = hurt(srd, state, HERO, 4)

    assert (after.party[HERO].temp_hp, after.party[HERO].hp) == (0, 7)
    assert events[0].hp_after == 7 and events[0].amount == 9
    assert (absorbed.party[HERO].temp_hp, absorbed.party[HERO].hp) == (2, 10)
    assert absorbed_events[0].hp_after == 10


def test_damage_works_on_a_party_member_outside_combat(srd, make_hero):
    state = state_of(make_hero(hp=10))

    after, events = hurt(srd, state, HERO, 3, "fire")

    assert after.party[HERO].hp == 7 and state.party[HERO].hp == 10
    assert events[0].hp_after == 7


def test_damage_to_an_unknown_target_or_outside_combat_is_a_combat_error(srd, make_hero):
    with pytest.raises(CombatError, match="ghost"):
        apply_damage(state_of(make_hero()), "ghost", 1, "fire", rng=NO_DICE, content=srd)
    with pytest.raises(CombatError, match="ghost"):
        apply_damage(battle(srd, [make_hero()]), "ghost", 1, "fire", rng=NO_DICE, content=srd)


def test_negative_damage_is_a_combat_error(srd, make_hero):
    with pytest.raises(CombatError, match="отрицательн"):
        apply_damage(battle(srd, [make_hero()]), HERO, -1, "fire", rng=NO_DICE, content=srd)


def test_damage_to_a_dead_target_is_a_combat_error(srd, make_hero, make_character):
    state = battle(srd, [make_hero(), make_character(id=ALLY, name="Мёртвый")])
    state.party[ALLY].death_saves = DeathSaves(failures=3)
    fighter(state, GOBLIN).hp = 0

    for target in (ALLY, GOBLIN):
        with pytest.raises(CombatError, match="мёртв"):
            apply_damage(state, target, 1, "fire", rng=NO_DICE, content=srd)


# --- apply_damage: сопротивления, иммунитеты, уязвимости по типам ---


@pytest.mark.parametrize(
    ("monster", "damage_type", "amount", "expected"),
    [
        ("skeleton", "bludgeoning", 5, 10),  # уязвимость: вдвое больше
        ("skeleton", "piercing", 5, 5),
        ("shadow", "radiant", 4, 8),
        ("shadow", "fire", 5, 2),  # сопротивление: вдвое меньше, вниз
        ("shadow", "necrotic", 5, 0),  # иммунитет
        ("shadow", "psychic", 5, 5),
        ("swarm_of_rats", "piercing", 7, 3),
    ],
)
def test_monster_defenses_apply_per_damage_type(
    srd, make_hero, monster, damage_type, amount, expected
):
    state = battle(srd, [make_hero()], (monster,))
    target = f"{monster}-1"
    before = fighter(state, target).hp

    after, events = hurt(srd, state, target, amount, damage_type)

    assert events[0].amount == expected
    assert fighter(after, target).hp == max(0, before - expected)


@pytest.mark.parametrize(
    ("overrides", "damage_type", "expected"),
    [
        ({"class_id": "barbarian", "buffs": [RAGE]}, "slashing", 4),
        ({"class_id": "barbarian", "buffs": [RAGE]}, "piercing", 4),
        ({"class_id": "barbarian", "buffs": [RAGE]}, "bludgeoning", 4),
        ({"class_id": "barbarian", "buffs": [RAGE]}, "fire", 9),
        ({"class_id": "barbarian"}, "slashing", 9),
        ({"species": "dwarf"}, "poison", 4),
        ({"species": "dwarf"}, "fire", 9),
        ({"species": "elf"}, "poison", 9),
        ({"conditions": [ActiveCondition(id="petrified")]}, "fire", 4),
        ({"class_id": "barbarian", "buffs": [RAGE], "species": "dwarf"}, "poison", 4),
    ],
)
def test_character_resistances_apply_per_damage_type(
    srd, make_hero, overrides, damage_type, expected
):
    state = battle(srd, [make_hero(hp_max=30, hp=30, **overrides)])

    _, events = hurt(srd, state, HERO, 9, damage_type)

    assert events[0].amount == expected


def test_immunity_still_reports_the_damage_event_with_zero(srd, make_hero):
    state = battle(srd, [make_hero()], ("shadow",))

    after, events = hurt(srd, state, "shadow-1", 5, "necrotic")

    assert events == [
        DamageEvent(target_id="shadow-1", amount=0, damage_type="necrotic", hp_after=27)
    ]
    assert fighter(after, "shadow-1").hp == 27


# --- apply_damage: смерть монстра ---


def test_a_monster_at_zero_hit_points_dies_but_stays_in_the_turn_order_until_its_turn_ends(
    srd, make_hero
):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, _ = hurt(srd, state, GOBLIN, 12)

    goblin = fighter(after, GOBLIN)
    assert goblin.hp == 0
    assert goblin.engaged_with == [] and fighter(after, HERO).engaged_with == []
    assert goblin.conditions == []  # у монстров нет спасбросков от смерти и «без сознания»
    assert GOBLIN in after.combat.combatants and GOBLIN in after.combat.order


# --- apply_damage: падение до 0 HP, спасброски от смерти, массивный урон ---


def test_a_character_dropping_to_zero_hit_points_falls_unconscious(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=20, hp=6, concentration=None)])

    after, events = hurt(srd, state, HERO, 8)

    hero = after.party[HERO]
    assert hero.hp == 0
    assert hero.conditions == [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)]
    assert hero.death_saves == DeathSaves()
    assert events == [
        DamageEvent(target_id=HERO, amount=8, damage_type="slashing", hp_after=0),
        ConditionEvent(target_id=HERO, condition="unconscious", added=True),
    ]


def test_damage_at_zero_hit_points_adds_a_death_save_failure_and_a_crit_adds_two(srd, make_hero):
    state = battle(srd, [downed(make_hero, hp_max=20)])

    once, _ = hurt(srd, state, HERO, 3)
    twice, _ = hurt(srd, state, HERO, 3, crit=True)
    thrice, _ = hurt(srd, once, HERO, 3, crit=True)

    assert once.party[HERO].death_saves.failures == 1
    assert twice.party[HERO].death_saves.failures == 2
    assert is_dead(thrice.party[HERO]) and thrice.party[HERO].death_saves.failures == 3


def test_a_stable_character_who_takes_damage_starts_failing_death_saves_again(srd, make_hero):
    stable = downed(make_hero, hp_max=20, death_saves=DeathSaves(stable=True))
    state = battle(srd, [stable])

    after, _ = hurt(srd, state, HERO, 3)

    assert after.party[HERO].death_saves == DeathSaves(failures=1, stable=False)


def test_damage_at_zero_hit_points_equal_to_the_maximum_kills(srd, make_hero):
    state = battle(srd, [downed(make_hero, hp_max=12)])

    after, _ = hurt(srd, state, HERO, 12)

    assert is_dead(after.party[HERO])


def test_damage_that_temporary_hit_points_fully_absorb_is_no_death_save_failure(srd, make_hero):
    state = battle(srd, [downed(make_hero, hp_max=20, temp_hp=5)])

    after, _ = hurt(srd, state, HERO, 4)

    assert after.party[HERO].death_saves.failures == 0 and after.party[HERO].temp_hp == 1


@pytest.mark.parametrize(
    ("damage", "dead"),
    [
        (18, True),  # 6 хитов уходят в 0, остаток 12 = максимуму: смерть
        (17, False),  # остаток 11
        (6, False),
    ],
)
def test_massive_damage_kills_when_the_remainder_reaches_the_hit_point_maximum(
    srd, make_hero, damage, dead
):
    state = battle(srd, [make_hero(hp_max=12, hp=6)])

    after, _ = hurt(srd, state, HERO, damage)

    hero = after.party[HERO]
    assert is_dead(hero) is dead
    assert hero.hp == 0 and hero.conditions == [
        ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)
    ]


def test_massive_damage_counts_the_remainder_after_temporary_hit_points(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=12, hp=6, temp_hp=10)])

    survives, _ = hurt(srd, state, HERO, 27)  # 10 временных, 6 хитов, остаток 11
    dies, _ = hurt(srd, state, HERO, 28)

    assert not is_dead(survives.party[HERO])
    assert is_dead(dies.party[HERO])


def test_a_character_falling_to_zero_hit_points_loses_concentration_without_a_save(srd, make_hero):
    caster = make_hero(hp_max=20, hp=4, concentration="bless")
    state = battle(srd, [caster])

    after, _ = hurt(srd, state, HERO, 9)  # кубов нет: спасбросок не бросался

    assert after.party[HERO].concentration is None


# --- Неутомимость орка ---


def test_relentless_endurance_leaves_an_orc_with_one_hit_point_once(srd, make_hero):
    orc = make_hero(species="orc", hp_max=20, hp=5)
    state = battle(srd, [orc])

    first, events = hurt(srd, state, HERO, 6)
    survivor = first.party[HERO]
    facts = (survivor.hp, survivor.resources, survivor.conditions)
    survivor.hp = 4  # ранен снова: способность уже потрачена
    second, _ = hurt(srd, first, HERO, 6)

    assert facts == (1, {"relentless_endurance": 0}, [])
    assert events == [DamageEvent(target_id=HERO, amount=6, damage_type="slashing", hp_after=1)]
    assert second.party[HERO].hp == 0 and second.party[HERO].conditions[0].id == "unconscious"


def test_relentless_endurance_does_not_save_from_massive_damage_or_other_species(srd, make_hero):
    massive = battle(srd, [make_hero(species="orc", hp_max=12, hp=6)])
    human = battle(srd, [make_hero(species="human", hp_max=20, hp=5)])

    killed, _ = hurt(srd, massive, HERO, 18)
    knocked_out, _ = hurt(srd, human, HERO, 6)

    assert is_dead(killed.party[HERO])
    assert knocked_out.party[HERO].hp == 0 and knocked_out.party[HERO].resources == {}


def test_a_used_up_relentless_endurance_stays_used_up(srd, make_hero):
    orc = make_hero(species="orc", hp_max=20, hp=5, resources={"relentless_endurance": 0})
    state = battle(srd, [orc])

    after, _ = hurt(srd, state, HERO, 6)

    assert after.party[HERO].hp == 0


def relentless_orc(make_hero, **overrides) -> Character:
    """Орк на 5 HP из 20 с концентрацией: любой удар от 5 урона роняет его до 0 (Неутомимость)."""
    fields = {"species": "orc", "hp_max": 20, "hp": 5, "concentration": "hold_person"}
    return make_hero(**(fields | overrides))


def concentration_saves(events) -> list[RollEvent]:
    return [e for e in roll_events(events) if "Концентрация" in e.label]


def test_an_orc_kept_on_its_feet_by_relentless_endurance_still_saves_for_concentration(
    srd, make_hero
):
    state = battle(srd, [relentless_orc(make_hero)])

    # Спасбросок Тел: 1 + 0 против Сл 10 — провал.
    after, events = hurt(srd, state, HERO, 6, rolls=[1])

    # Орк остался на ногах (1 HP, в сознании), а по SRD любой полученный урон требует спасброска.
    assert after.party[HERO].hp == 1
    assert [e.dc for e in concentration_saves(events)] == [10]
    assert after.party[HERO].concentration is None


@pytest.mark.parametrize(("natural", "keeps"), [(12, True), (11, False)])
def test_the_save_of_an_orc_kept_by_relentless_endurance_uses_the_whole_damage(
    srd, make_hero, natural, keeps
):
    state = battle(srd, [relentless_orc(make_hero)])

    after, events = hurt(srd, state, HERO, 24, rolls=[natural])  # остаток 19 < 20: не массивный

    (save,) = concentration_saves(events)
    assert (save.dc, save.success) == (12, keeps)  # Сл = 24 // 2, а не по остатку после падения
    orc = after.party[HERO]
    assert (orc.hp, orc.resources, orc.conditions) == (1, {"relentless_endurance": 0}, [])
    assert (orc.concentration == "hold_person") is keeps


def test_temporary_hit_points_do_not_lower_the_save_dc_of_an_orc_kept_by_relentless_endurance(
    srd, make_hero
):
    orc = relentless_orc(make_hero, hp_max=40, temp_hp=10)
    state = battle(srd, [orc])

    after, events = hurt(srd, state, HERO, 30, rolls=[14])  # 10 временных, 20 до хитов

    (save,) = concentration_saves(events)
    assert (save.dc, save.success) == (15, False)  # Сл по всему урону 30, а не по 20 до хитов
    assert (after.party[HERO].hp, after.party[HERO].temp_hp) == (1, 0)
    assert after.party[HERO].concentration is None


def test_relentless_endurance_without_concentration_rolls_no_dice(srd, make_hero):
    state = battle(srd, [relentless_orc(make_hero, concentration=None)])

    after, events = hurt(srd, state, HERO, 6)  # кубов нет: спасбросок не бросался

    assert after.party[HERO].hp == 1 and concentration_saves(events) == []
    assert after.party[HERO].resources == {"relentless_endurance": 0}


def test_relentless_endurance_with_two_damage_types_makes_one_save_and_no_event_shows_zero(
    srd, make_hero
):
    state = battle(srd, [relentless_orc(make_hero)], ("ghoul",))
    melee(state, HERO, "ghoul-1")

    # Укус упыря: 6 + 2 колющего и 3 некротического — 11 за раз; оба типа роняют орка до 0.
    after, events = monster_hits(srd, state, "ghoul", [10, 6, 3, 10], "bite")

    assert [d.hp_after for d in damage_events(events)] == [1, 1]
    assert [e.dc for e in concentration_saves(events)] == [10]
    assert (after.party[HERO].hp, after.party[HERO].concentration) == (1, "hold_person")


@pytest.mark.parametrize(
    "orc_fields", [{"resources": {"relentless_endurance": 0}}, {"hp_max": 12, "hp": 6}]
)
def test_an_orc_who_relentless_endurance_cannot_save_loses_concentration_without_a_save(
    srd, make_hero, orc_fields
):
    damage = 18 if orc_fields.get("hp_max") else 6  # второй случай — массивный урон
    state = battle(srd, [relentless_orc(make_hero, **orc_fields)])

    after, events = hurt(srd, state, HERO, damage)  # кубов нет

    assert after.party[HERO].hp == 0 and after.party[HERO].concentration is None
    assert concentration_saves(events) == []


# --- концентрация ---


def blessed_party(make_hero, make_character, hero_hp=30):
    """Герой держит «Благословение» на себе и союзнике; у союзника есть и чужое благословение."""

    def bless(source: str) -> ActiveBuff:
        return ActiveBuff(id="bless", rounds_left=10, source_id=source)

    caster = make_hero(hp_max=30, hp=hero_hp, concentration="bless", buffs=[bless(HERO)])
    ally = make_character(id=ALLY, name="Союзник", buffs=[bless(HERO), bless("pc-9")])
    return caster, ally


def test_concentration_check_dc_is_half_the_damage_but_at_least_10(srd, make_hero, make_character):
    caster, ally = blessed_party(make_hero, make_character)
    state = battle(srd, [caster, ally])

    # Телосложение 10: +0; благословение заклинателя добавляет к спасброску 1к4 (здесь всегда 1).
    kept_24, events = hurt(srd, state, HERO, 24, rolls=[11, 1])  # Сл 12
    lost_24, _ = hurt(srd, state, HERO, 24, rolls=[10, 1])
    kept_8, _ = hurt(srd, state, HERO, 8, rolls=[9, 1])  # половина 4, но не меньше 10
    lost_8, _ = hurt(srd, state, HERO, 8, rolls=[8, 1])

    save = roll_events(events)[0]
    assert (save.dc, save.success, save.actor_id) == (12, True, HERO)
    assert "Концентрация" in save.label
    assert kept_24.party[HERO].concentration == "bless"
    assert lost_24.party[HERO].concentration is None
    assert kept_8.party[HERO].concentration == "bless"
    assert lost_8.party[HERO].concentration is None


def test_losing_concentration_ends_the_spell_effects_made_by_this_caster_only(
    srd, make_hero, make_character
):
    caster, ally = blessed_party(make_hero, make_character)
    state = battle(srd, [caster, ally])

    after, events = hurt(srd, state, HERO, 24, rolls=[1, 1])

    assert after.party[HERO].buffs == []
    assert [(b.id, b.source_id) for b in after.party[ALLY].buffs] == [("bless", "pc-9")]
    removed = [e for e in events if isinstance(e, ConditionEvent)]
    assert {(e.target_id, e.condition, e.added) for e in removed} == {
        (HERO, "bless", False),
        (ALLY, "bless", False),
    }


def test_losing_concentration_frees_enemies_held_by_the_spell(srd, make_hero):
    caster = make_hero(hp_max=30, hp=30, concentration="hold_person")
    state = battle(srd, [caster])
    held = ActiveCondition(id="paralyzed", rounds_left=10, source=HERO)
    other = ActiveCondition(id="paralyzed", rounds_left=10, source="goblin_warrior-2")
    fighter(state, GOBLIN).conditions += [held, other]

    after, events = hurt(srd, state, HERO, 24, rolls=[1])

    assert fighter(after, GOBLIN).conditions == [other]
    assert ConditionEvent(target_id=GOBLIN, condition="paralyzed", added=False) in events


def test_no_concentration_save_without_concentration_or_without_damage(srd, make_hero):
    plain = battle(srd, [make_hero(hp_max=30, hp=30)])
    concentrating = battle(srd, [make_hero(hp_max=30, hp=30, concentration="bless")])
    resistant = battle(
        srd, [make_hero(hp_max=30, hp=30, concentration="bless", conditions=conds("petrified"))]
    )

    hurt(srd, plain, HERO, 24)  # кубов нет: спасбросок не бросался
    hurt(srd, concentrating, HERO, 0)
    after, _ = hurt(srd, resistant, HERO, 1)  # половина от 1 — 0 урона
    assert after.party[HERO].concentration == "bless"


def test_concentration_save_follows_constitution_and_proficiency(srd, make_hero):
    sturdy = make_hero(
        hp_max=30, hp=30, concentration="bless", scores={STR: 16, CON: 14}, save_proficiencies=[CON]
    )
    state = battle(srd, [sturdy])

    # Телосложение +2 и мастерство +2: 8 + 4 = 12 против Сл 12.
    after, events = hurt(srd, state, HERO, 24, rolls=[8])

    assert roll_events(events)[0].roll.total == 12
    assert after.party[HERO].concentration == "bless"


def test_concentration_save_ignores_the_master_dc_table(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=40, hp=40, concentration="bless")])

    _, events = hurt(srd, state, HERO, 25, rolls=[12])  # Сл 12 — не из таблицы 5/10/15/...

    assert roll_events(events)[0].dc == 12


@pytest.mark.parametrize(("damage", "dc"), [(58, 29), (60, 30), (62, 30), (100, 30)])
def test_concentration_save_dc_never_exceeds_30(srd, make_hero, damage, dc):
    state = battle(srd, [make_hero(hp_max=200, hp=200, concentration="bless")])

    _, events = hurt(srd, state, HERO, damage, rolls=[15])

    assert concentration_saves(events)[0].dc == dc


@pytest.mark.parametrize(
    ("temp_hp", "damage", "natural", "dc", "keeps"),
    [
        (10, 6, 10, 10, True),  # Сл 10 ровно, спасбросок Тел +0: 10 держит
        (10, 6, 9, 10, False),
        (50, 30, 15, 15, True),  # Сл = половина урона 30
        (50, 30, 14, 15, False),
    ],
)
def test_damage_fully_absorbed_by_temporary_hit_points_still_asks_for_the_concentration_save(
    srd, make_hero, temp_hp, damage, natural, dc, keeps
):
    caster = make_hero(hp_max=20, hp=20, temp_hp=temp_hp, concentration="hold_person")
    state = battle(srd, [caster])

    after, events = hurt(srd, state, HERO, damage, rolls=[natural])

    hero = after.party[HERO]
    assert (hero.hp, hero.temp_hp) == (20, temp_hp - damage)  # до хитов урон не дошёл
    (save,) = [e for e in roll_events(events) if "Концентрация" in e.label]
    assert (save.dc, save.success) == (dc, keeps)
    assert (hero.concentration == "hold_person") is keeps


def test_death_by_massive_damage_ends_the_concentration_and_the_effects_it_held(
    srd, make_hero, make_character
):
    caster, ally = blessed_party(make_hero, make_character, hero_hp=6)
    state = battle(srd, [caster, ally])

    after, events = hurt(srd, state, HERO, 36)  # остаток 30 = максимуму: смерть, кубов нет

    assert is_dead(after.party[HERO]) and after.party[HERO].concentration is None
    assert after.party[HERO].buffs == []
    assert [(b.id, b.source_id) for b in after.party[ALLY].buffs] == [("bless", "pc-9")]
    assert ConditionEvent(target_id=ALLY, condition="bless", added=False) in events


# --- heal_target ---


def test_heal_restores_character_hit_points_up_to_the_maximum(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=20, hp=15)])

    after, events = heal_target(state, HERO, 8)

    assert after.party[HERO].hp == 20
    assert events == [HealEvent(target_id=HERO, amount=5, hp_after=20)]  # лишнее пропадает


def test_heal_restores_monster_hit_points_in_the_combatant(srd, make_hero):
    state = battle(srd, [make_hero()])
    fighter(state, GOBLIN).hp = 3

    after, events = heal_target(state, GOBLIN, 4)

    assert fighter(after, GOBLIN).hp == 7
    assert events == [HealEvent(target_id=GOBLIN, amount=4, hp_after=7)]


def test_heal_wakes_a_character_from_zero_hit_points(srd, make_hero):
    hero = downed(make_hero, hp_max=20, death_saves=DeathSaves(successes=2, failures=2))
    state = battle(srd, [hero])

    after, events = heal_target(state, HERO, 3)

    revived = after.party[HERO]
    assert (revived.hp, revived.conditions, revived.death_saves) == (3, [], DeathSaves())
    assert events == [
        HealEvent(target_id=HERO, amount=3, hp_after=3),
        ConditionEvent(target_id=HERO, condition="unconscious", added=False),
    ]


def test_heal_wakes_a_stable_character_too(srd, make_hero):
    stable = downed(make_hero, hp_max=20, death_saves=DeathSaves(stable=True))

    after, _ = heal_target(battle(srd, [stable]), HERO, 1)

    assert after.party[HERO].conditions == [] and after.party[HERO].death_saves == DeathSaves()


def test_heal_of_zero_does_not_wake_anyone(srd, make_hero):
    hero = downed(make_hero, hp_max=20)

    after, _ = heal_target(battle(srd, [hero]), HERO, 0)

    assert after.party[HERO].hp == 0 and after.party[HERO].conditions == hero.conditions


def test_heal_does_not_end_unconsciousness_of_another_origin(srd, make_hero):
    asleep = make_hero(
        hp_max=20, hp=5, conditions=[ActiveCondition(id="unconscious", source="sleep")]
    )

    after, _ = heal_target(battle(srd, [asleep]), HERO, 3)

    assert [c.source for c in after.party[HERO].conditions] == ["sleep"]


def test_healing_at_zero_hit_points_keeps_another_origin_of_unconsciousness_and_reports_no_wake(
    srd, make_hero
):
    # На 0 HP и вдобавок усыплён: хиты вернулись, а сон не снимается и «пробуждения» нет.
    asleep = make_hero(
        hp_max=20,
        hp=0,
        conditions=[ActiveCondition(id="unconscious", source="sleep")],
        death_saves=DeathSaves(failures=2),
    )

    after, events = heal_target(battle(srd, [asleep]), HERO, 3)

    hero = after.party[HERO]
    assert hero.hp == 3 and [c.source for c in hero.conditions] == ["sleep"]
    assert hero.death_saves == DeathSaves()  # хиты вернулись: череда провалов обнулена
    assert events == [HealEvent(target_id=HERO, amount=3, hp_after=3)]


def test_the_dead_are_not_revived_by_healing(srd, make_hero, make_character):
    state = battle(srd, [make_hero(), downed(make_character, id=ALLY, name="Мёртвый")])
    state.party[ALLY].death_saves = DeathSaves(failures=3)
    fighter(state, GOBLIN).hp = 0

    for target in (ALLY, GOBLIN):
        with pytest.raises(CombatError, match="мёртв"):
            heal_target(state, target, 10)


def test_negative_healing_is_a_combat_error_and_healing_works_outside_combat(srd, make_hero):
    with pytest.raises(CombatError, match="отрицательн"):
        heal_target(battle(srd, [make_hero()]), HERO, -1)

    after, _ = heal_target(state_of(make_hero(hp_max=20, hp=10)), HERO, 4)
    assert after.party[HERO].hp == 14


# --- чистота функций урона и лечения ---


def test_damage_and_healing_leave_the_original_state_untouched(srd, make_hero):
    state = melee(battle(srd, [make_hero(hp=6, concentration="bless")]), HERO, GOBLIN)
    snapshot = state.model_copy(deep=True)

    damaged, _ = hurt(srd, state, HERO, 9)
    healed, _ = heal_target(damaged, HERO, 2)
    hurt(srd, state, GOBLIN, 12)

    assert state == snapshot
    assert damaged is not state and healed is not damaged
    assert healed.party[HERO].hp == 2 and damaged.party[HERO].hp == 0


# --- end_turn: переход хода ---


def two_goblins(srd, hero) -> GameState:
    """Очередь: герой (20), гоблин 1 (7), гоблин 2 (6)."""
    return battle(srd, [hero], ("goblin_warrior", "goblin_warrior"))


def turn(srd, state, rolls=()):
    rng = ScriptedRng(list(rolls))
    after, events = end_turn(state, rng=rng, content=srd)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def test_end_turn_passes_the_turn_on_and_resets_only_the_next_fighters_flags(srd, make_hero):
    state = two_goblins(srd, make_hero())
    fighter(state, HERO).turn_flags.update(action=True, moved=True)
    goblin = fighter(state, GOBLIN)
    goblin.turn_flags.update(action=True, reaction=True, sneak_attack_used=True)
    goblin.dodging = goblin.disengaged = True

    after, events = turn(srd, state)

    assert current_actor(after).id == GOBLIN and after.combat.round == 1
    assert events == [CombatEvent(phase="turn", actor_id=GOBLIN)]
    new_goblin = fighter(after, GOBLIN)
    assert (new_goblin.turn_flags, new_goblin.dodging, new_goblin.disengaged) == ({}, False, False)
    # Флаги героя живут до начала его собственного хода: на чужом ходу он может ответить.
    assert fighter(after, HERO).turn_flags == {"action": True, "moved": True}


def test_after_the_last_fighter_the_round_counter_grows_and_the_first_acts_again(srd, make_hero):
    state = two_goblins(srd, make_hero())
    state.combat.turn_index = 2
    fighter(state, HERO).turn_flags["action"] = True

    after, events = turn(srd, state)

    assert (current_actor(after).id, after.combat.round, after.combat.turn_index) == (HERO, 2, 0)
    assert fighter(after, HERO).turn_flags == {}
    assert events == [CombatEvent(phase="turn", actor_id=HERO)]


def test_end_turn_needs_an_ongoing_combat(srd, make_hero):
    with pytest.raises(CombatError, match="не идёт"):
        end_turn(state_of(make_hero()), rng=NO_DICE, content=srd)


# --- end_turn: длительность и повторные спасброски состояний ---


def test_durations_of_the_ending_fighters_conditions_and_buffs_run_down(srd, make_hero):
    hero = make_hero(
        conditions=[
            ActiveCondition(id="frightened", rounds_left=2),
            ActiveCondition(id="poisoned", rounds_left=1),
            ActiveCondition(id="prone"),
        ],
        buffs=[
            ActiveBuff(id="bless", rounds_left=1, source_id="pc-9"),
            ActiveBuff(id="rage", rounds_left=10, source_id=HERO),
            ActiveBuff(id="mage_armor", rounds_left=None, source_id="pc-9"),
        ],
    )
    state = two_goblins(srd, hero)
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="blinded", rounds_left=1))

    after, events = turn(srd, state)

    assert [(c.id, c.rounds_left) for c in after.party[HERO].conditions] == [
        ("frightened", 1),
        ("prone", None),
    ]
    assert [(b.id, b.rounds_left) for b in after.party[HERO].buffs] == [
        ("rage", 9),
        ("mage_armor", None),
    ]
    assert fighter(after, GOBLIN).conditions[0].rounds_left == 1  # чужое состояние не тикает
    ended = [e for e in events if isinstance(e, ConditionEvent)]
    assert ended == [
        ConditionEvent(target_id=HERO, condition="poisoned", added=False),
        ConditionEvent(target_id=HERO, condition="bless", added=False),
    ]


def test_a_monsters_conditions_run_down_at_the_end_of_its_own_turn(srd, make_hero):
    state = two_goblins(srd, make_hero())
    state.combat.turn_index = 1
    fighter(state, GOBLIN).conditions += [
        ActiveCondition(id="blinded", rounds_left=1),
        ActiveCondition(id="prone", rounds_left=3),
    ]

    after, events = turn(srd, state)

    assert [(c.id, c.rounds_left) for c in fighter(after, GOBLIN).conditions] == [("prone", 2)]
    assert ConditionEvent(target_id=GOBLIN, condition="blinded", added=False) in events


def frightened_hero(make_hero) -> Character:
    """Страх с повторным спасброском Мудрости Сл 12 (Сл не из таблицы мастера)."""
    condition = ActiveCondition(id="frightened", rounds_left=10, source=GOBLIN, save=WIS, dc=12)
    return make_hero(conditions=[condition])


def test_a_character_repeats_the_save_at_the_end_of_the_turn_and_a_success_ends_the_condition(
    srd, make_hero
):
    state = two_goblins(srd, frightened_hero(make_hero))

    after, events = turn(srd, state, [12])

    assert after.party[HERO].conditions == []
    save, ended = roll_events(events)[0], [e for e in events if isinstance(e, ConditionEvent)]
    assert (save.dc, save.success, save.actor_id) == (12, True, HERO)
    assert save.label.startswith("Повторный спасбросок")
    assert ended == [ConditionEvent(target_id=HERO, condition="frightened", added=False)]


def test_a_failed_repeat_save_keeps_the_condition_and_it_runs_down(srd, make_hero):
    state = two_goblins(srd, frightened_hero(make_hero))

    after, events = turn(srd, state, [11])

    assert [(c.id, c.rounds_left) for c in after.party[HERO].conditions] == [("frightened", 9)]
    assert roll_events(events)[0].success is False


def test_a_monster_repeats_saves_with_its_statblock_bonus(srd, make_hero):
    state = two_goblins(srd, make_hero())
    state.combat.turn_index = 1
    held = ActiveCondition(id="paralyzed", rounds_left=10, source=HERO, save=WIS, dc=12)
    fighter(state, GOBLIN).conditions.append(held)

    # Мудрость гоблина 8 (−1): 13 − 1 = 12 — успех, 12 − 1 = 11 — провал.
    freed, freed_events = turn(srd, state, [13])
    still_held, _ = turn(srd, state, [12])

    assert fighter(freed, GOBLIN).conditions == []
    assert (roll_events(freed_events)[0].dc, roll_events(freed_events)[0].actor_id) == (12, GOBLIN)
    assert fighter(still_held, GOBLIN).conditions[0].rounds_left == 9


def test_a_repeat_save_that_the_condition_itself_fails_costs_no_dice(srd, make_hero):
    state = two_goblins(srd, make_hero())
    state.combat.turn_index = 1
    fighter(state, GOBLIN).conditions.append(
        ActiveCondition(id="paralyzed", rounds_left=5, save=STR, dc=5)
    )

    after, events = turn(srd, state)  # паралич проваливает спасбросок Силы без броска

    assert roll_events(events)[0].success is False
    assert roll_events(events)[0].roll.rolls == []
    assert fighter(after, GOBLIN).conditions[0].rounds_left == 4


def test_conditions_without_a_save_roll_nothing(srd, make_hero):
    hero = make_hero(conditions=[ActiveCondition(id="poisoned", rounds_left=3)])

    after, events = turn(srd, two_goblins(srd, hero))

    assert roll_events(events) == []
    assert after.party[HERO].conditions[0].rounds_left == 2


# --- end_turn: убитые монстры и мёртвые персонажи ---


@pytest.mark.parametrize(
    ("dead", "turn_index", "order", "next_actor", "round_number"),
    [
        (GOBLIN, 0, [HERO, GOBLIN_2], GOBLIN_2, 1),  # убит следующий по очереди
        (GOBLIN_2, 0, [HERO, GOBLIN], GOBLIN, 1),
        (GOBLIN, 1, [HERO, GOBLIN_2], GOBLIN_2, 1),  # убит на собственном ходу
        (GOBLIN, 2, [HERO, GOBLIN_2], HERO, 2),  # убит раньше по очереди, ход заканчивает последний
        (GOBLIN_2, 2, [HERO, GOBLIN], HERO, 2),  # убит тот, чей ход заканчивается
    ],
)
def test_dead_monsters_leave_the_order_when_the_turn_ends(
    srd, make_hero, dead, turn_index, order, next_actor, round_number
):
    state = two_goblins(srd, make_hero())
    state.combat.turn_index = turn_index
    fighter(state, dead).hp = 0

    after, events = turn(srd, state)

    assert after.combat.order == order
    assert (current_actor(after).id, after.combat.round) == (next_actor, round_number)
    assert dead in after.combat.combatants  # запись остаётся для журнала
    assert events == [CombatEvent(phase="turn", actor_id=next_actor)]


def test_when_every_monster_is_dead_the_heroes_keep_taking_turns(srd, make_hero):
    state = two_goblins(srd, make_hero())
    fighter(state, GOBLIN).hp = fighter(state, GOBLIN_2).hp = 0

    after, _ = turn(srd, state)

    assert after.combat.order == [HERO]
    assert (current_actor(after).id, after.combat.round) == (HERO, 2)


def test_dead_characters_are_skipped(srd, make_hero, make_character):
    state = battle(srd, [make_hero(), make_character(id=ALLY, name="Союзник")])
    state.party[ALLY].death_saves = DeathSaves(failures=3)  # погиб уже в бою

    after, events = turn(srd, state)

    assert after.combat.order[after.combat.turn_index] == GOBLIN
    assert events == [CombatEvent(phase="turn", actor_id=GOBLIN)]


# --- end_turn: спасброски от смерти в начале хода ---


def downed_turn(srd, make_hero, **overrides):
    """Конец хода гоблина; следующим по кругу ходит герой на 0 HP."""
    state = battle(srd, [downed(make_hero, hp_max=20, **overrides)])
    state.combat.turn_index = 1
    return state


@pytest.mark.parametrize(
    ("die", "expected"),
    [
        (10, DeathSaves(successes=1)),
        (19, DeathSaves(successes=1)),
        (9, DeathSaves(failures=1)),
        (2, DeathSaves(failures=1)),
        (1, DeathSaves(failures=2)),
    ],
)
def test_a_character_at_zero_hit_points_makes_a_death_save_when_its_turn_starts(
    srd, make_hero, die, expected
):
    after, events = turn(srd, downed_turn(srd, make_hero), [die])

    assert after.party[HERO].death_saves == expected
    assert after.party[HERO].hp == 0
    turn_event, save = events
    assert turn_event == CombatEvent(phase="turn", actor_id=HERO)
    assert (save.actor_id, save.label, save.dc, save.success, save.audience) == (
        HERO,
        "Спасбросок от смерти",
        10,
        die >= 10,
        "all",
    )
    assert (save.roll.natural, save.roll.total) == (die, die)  # без модификаторов


def test_a_natural_20_on_a_death_save_brings_the_character_back_with_one_hit_point(srd, make_hero):
    state = downed_turn(srd, make_hero, death_saves=DeathSaves(successes=1, failures=2))

    after, events = turn(srd, state, [20])

    hero = after.party[HERO]
    assert (hero.hp, hero.conditions, hero.death_saves) == (1, [], DeathSaves())
    assert HealEvent(target_id=HERO, amount=1, hp_after=1) in events
    assert ConditionEvent(target_id=HERO, condition="unconscious", added=False) in events


def test_three_successes_make_the_character_stable_and_stable_characters_stop_rolling(
    srd, make_hero
):
    state = downed_turn(srd, make_hero, death_saves=DeathSaves(successes=2, failures=1))

    stable, _ = turn(srd, state, [12])
    stable.combat.turn_index = 1
    again, events = turn(srd, stable)  # стабильный: кубов нет

    assert stable.party[HERO].death_saves == DeathSaves(stable=True)
    assert stable.party[HERO].hp == 0
    assert again.party[HERO].death_saves == DeathSaves(stable=True)
    assert events == [CombatEvent(phase="turn", actor_id=HERO)]


@pytest.mark.parametrize(("die", "start_failures"), [(5, 2), (1, 2), (1, 1)])
def test_three_failures_kill_the_character(srd, make_hero, die, start_failures):
    state = downed_turn(srd, make_hero, death_saves=DeathSaves(failures=start_failures))

    after, _ = turn(srd, state, [die])

    assert is_dead(after.party[HERO])
    assert after.party[HERO].death_saves.failures == 3


def test_a_dead_character_who_is_handed_the_turn_rolls_no_death_save_and_stays_dead(srd, make_hero):
    # Все убиты в бою, а вызывающий ещё раз зовёт end_turn: ход достаётся мёртвому в очереди.
    state = battle(srd, [make_hero(hp_max=20, hp=20)])
    state, _ = hurt(srd, state, HERO, 100)  # массивный урон
    state, _ = hurt(srd, state, GOBLIN, 10, "piercing")
    rng = ScriptedRng([20])  # будь это спасбросок от смерти, натуральная 20 подняла бы героя

    after, events = end_turn(state, rng=rng, content=srd)

    hero = after.party[HERO]
    assert is_dead(hero) and hero.hp == 0 and hero.death_saves.failures == 3
    assert events == [CombatEvent(phase="turn", actor_id=HERO)]
    assert rng.randint(1, 20) == 20  # куб на мёртвого не брошен


def test_a_conscious_character_makes_no_death_save(srd, make_hero):
    state = battle(srd, [make_hero()])
    state.combat.turn_index = 1

    _, events = turn(srd, state)

    assert events == [CombatEvent(phase="turn", actor_id=HERO)]


def test_a_blow_to_zero_hit_points_then_death_saves_down_to_stability(srd, make_hero):
    state = melee(battle(srd, [make_hero(hp_max=20, hp=5)]), HERO, GOBLIN)
    state.combat.turn_index = 1  # ход гоблина
    state, _ = strike(srd, state, [15, 4], attacker=GOBLIN, target=HERO, action_id="scimitar")
    assert state.party[HERO].hp == 0 and not is_dead(state.party[HERO])

    for die in (12, 14, 10):
        state, _ = turn(srd, state, [die])  # конец хода гоблина: герой бросает спасбросок
        assert current_actor(state).id == HERO
        state, _ = turn(srd, state)  # герой без сознания ничего не делает
    hero = state.party[HERO]

    assert (hero.hp, hero.death_saves) == (0, DeathSaves(stable=True))
    assert hero.conditions == [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)]


# --- combat_winner ---


def test_nobody_has_won_while_both_sides_have_fighters_on_their_feet(srd, make_hero):
    assert combat_winner(two_goblins(srd, make_hero())) is None


def test_the_party_wins_when_the_last_enemy_dies(srd, make_hero):
    state = two_goblins(srd, make_hero())
    fighter(state, GOBLIN).hp = 0
    assert combat_winner(state) is None

    after, _ = hurt(srd, state, GOBLIN_2, 99)

    assert combat_winner(after) == "party"


def test_the_enemies_win_when_every_character_is_down(srd, make_hero, make_character):
    ally = make_character(id=ALLY, name="Союзник")
    state = battle(srd, [downed(make_hero), ally])
    assert combat_winner(state) is None  # союзник на ногах

    after, _ = hurt(srd, state, ALLY, 99)

    assert combat_winner(after) == "enemies"


def test_a_dying_character_cannot_fight_but_a_paralyzed_one_still_counts(srd, make_hero):
    paralyzed = make_hero(conditions=conds("paralyzed"))

    assert combat_winner(two_goblins(srd, paralyzed)) is None


def test_combat_winner_needs_an_ongoing_combat(make_hero):
    with pytest.raises(CombatError, match="не идёт"):
        combat_winner(state_of(make_hero()))


# --- end_turn: чистота ---


def test_end_turn_leaves_the_original_state_untouched_and_survives_json(srd, make_hero):
    hero = make_hero(conditions=[ActiveCondition(id="poisoned", rounds_left=2)])
    state = two_goblins(srd, hero)
    snapshot = state.model_copy(deep=True)

    after, _ = turn(srd, state)

    assert state == snapshot and after is not state
    assert GameState.model_validate_json(after.model_dump_json()) == after


# --- данные SRD, на которые опирается бой ---


@pytest.mark.parametrize(
    ("class_id", "feature_id", "level"),
    [
        ("rogue", SNEAK_ATTACK, 1),
        ("ranger", COLOSSUS_SLAYER, 3),
        ("barbarian", FRENZY, 3),
        ("fighter", IMPROVED_CRITICAL, 3),
    ],
)
def test_class_features_used_by_combat_exist_as_mechanical_features(
    srd, class_id, feature_id, level
):
    features = {f.id: f for f in srd.classes[class_id].features}

    assert (features[feature_id].level, features[feature_id].mechanical) == (level, True)


def test_feats_used_by_combat_are_granted_by_backgrounds(srd):
    assert srd.backgrounds["soldier"].feat == SAVAGE_ATTACKER_FEAT
    assert srd.backgrounds["criminal"].feat == ALERT_FEAT
    assert {SAVAGE_ATTACKER_FEAT, ALERT_FEAT} <= set(srd.feats)


def test_spell_effects_used_by_combat_match_the_data(srd):
    assert srd.spells["guiding_bolt"].effect.buff == GUIDING_BOLT
    assert srd.spells["hunters_mark"].effect.buff == HUNTERS_MARK
    assert srd.spells["hold_person"].effect.condition in CONDITION_EFFECTS
    assert srd.spells["bless"].effect.buff == "bless"
