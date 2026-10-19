from typing import get_args

import pytest

from dnd_engine.abilities import Ability, Skill
from dnd_engine.character import ActiveBuff, ActiveCondition, Character
from dnd_engine.checks import (
    BLESS_BUFF,
    CHECK_DISADVANTAGE_CONDITIONS,
    CHECK_TRAIT_BY_SPECIES,
    DC_TABLE,
    SAVE_DISADVANTAGE_CONDITIONS,
    VALID_DCS,
    CheckHook,
    CheckKind,
    CheckResult,
    ContestResult,
    ContestSide,
    GUIDANCE_BUFF,
    ability_check,
    apply_bless,
    apply_luck,
    character_contest_side,
    check_label,
    combine_advantage,
    contest,
    engine_saving_throw,
    monster_contest_side,
    monster_saving_throw,
    saving_throw,
)
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage, parse, roll
from dnd_engine.rng import ScriptedRng

STR, DEX, CON, INT, WIS, CHA = Ability
NONE, ADVANTAGE, DISADVANTAGE = Advantage.NONE, Advantage.ADVANTAGE, Advantage.DISADVANTAGE


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def check(
    c: Character,
    values: list[int],
    *,
    ability: Ability = DEX,
    skill: Skill | None = None,
    dc: int = 15,
    advantage: Advantage = NONE,
    hooks=(),
) -> CheckResult:
    """Проверка характеристики или навыка на заранее заданных значениях костей."""
    return ability_check(
        c,
        ability,
        skill=skill,
        dc=dc,
        advantage=advantage,
        rng=ScriptedRng(values),
        reason="тест",
        hooks=hooks,
    )


def save(
    c: Character,
    values: list[int],
    *,
    ability: Ability = WIS,
    dc: int = 15,
    advantage: Advantage = NONE,
    against: str | None = None,
    hooks=(),
) -> CheckResult:
    """Спасбросок на заранее заданных значениях костей."""
    return saving_throw(
        c,
        ability,
        dc=dc,
        advantage=advantage,
        rng=ScriptedRng(values),
        reason="тест",
        against=against,
        hooks=hooks,
    )


def bless() -> ActiveBuff:
    return ActiveBuff(id="bless", rounds_left=10, source_id="cleric-1")


def guidance() -> ActiveBuff:
    return ActiveBuff(id="guidance", rounds_left=10, source_id="cleric-1")


# --- таблица Сл ---


def test_dc_table_holds_only_the_six_standard_values():
    assert dict(DC_TABLE) == {
        "very_easy": 5,
        "easy": 10,
        "medium": 15,
        "hard": 20,
        "very_hard": 25,
        "nearly_impossible": 30,
    }
    assert frozenset({5, 10, 15, 20, 25, 30}) == VALID_DCS


@pytest.mark.parametrize("dc", [-5, 0, 1, 12, 14, 16, 31, 100])
def test_check_rejects_dc_outside_the_table_without_rolling(make_character, dc):
    rng = ScriptedRng([10])

    with pytest.raises(ValueError, match="Сл"):
        ability_check(
            make_character(),
            DEX,
            skill=None,
            dc=dc,
            advantage=NONE,
            rng=rng,
            reason="тест",
        )

    assert rng.randint(1, 20) == 10  # значение не потрачено: бросок не делался


@pytest.mark.parametrize("dc", [0, 12, 31])
def test_save_rejects_dc_outside_the_table_without_rolling(make_character, dc):
    rng = ScriptedRng([10])

    with pytest.raises(ValueError, match="Сл"):
        saving_throw(
            make_character(),
            WIS,
            dc=dc,
            advantage=NONE,
            rng=rng,
            reason="тест",
        )

    assert rng.randint(1, 20) == 10


# --- Сл, рассчитанная движком ---


def engine_save(
    c: Character,
    values: list[int],
    *,
    ability: Ability = CON,
    dc: int = 12,
    advantage: Advantage = NONE,
) -> CheckResult:
    return engine_saving_throw(
        c,
        ability,
        dc=dc,
        advantage=advantage,
        rng=ScriptedRng(values),
        reason="тест",
    )


@pytest.mark.parametrize("dc", [1, 8, 10, 12, 17, 31])
def test_engine_computed_dc_is_not_limited_to_the_table(make_character, dc):
    result = engine_save(make_character(), [10], dc=dc)

    assert result.dc == dc
    assert result.success == (10 >= dc)


def test_engine_save_rolls_like_a_normal_save(make_character):
    c = make_character(species="gnome", scores={WIS: 14}, save_proficiencies=[WIS], buffs=[bless()])

    result = engine_save(c, [4, 16, 3], ability=WIS, dc=12)

    # мастерство 2 + Мдр +2; Гномья хитрость даёт преимущество; благословение — отдельная кость
    assert (result.kind, result.bonus) == (CheckKind.SAVE, 4)
    assert (result.roll.advantage, result.roll.natural, result.roll.rolls) == (
        ADVANTAGE,
        16,
        [4, 16, 3],
    )
    assert result.total == 16 + 4 + 3


def test_engine_save_honours_the_poison_tag_of_dwarves(make_character):
    result = engine_saving_throw(
        make_character(species="dwarf"),
        CON,
        dc=12,
        advantage=NONE,
        rng=ScriptedRng([4, 16]),
        reason="тест",
        against="poison",
    )

    assert (result.roll.advantage, result.roll.natural) == (ADVANTAGE, 16)


def test_master_save_still_refuses_a_dc_outside_the_table_when_engine_save_accepts_it(
    make_character,
):
    c = make_character()

    assert engine_save(c, [10], dc=12).dc == 12
    with pytest.raises(ValueError, match="Сл"):
        save(c, [10], dc=12)


# --- автопровал спасбросков ---

AUTO_FAIL_CONDITIONS = ["paralyzed", "petrified", "stunned", "unconscious"]


@pytest.mark.parametrize("condition_id", AUTO_FAIL_CONDITIONS)
@pytest.mark.parametrize("ability", [STR, DEX])
def test_some_conditions_fail_strength_and_dexterity_saves_without_a_roll(
    make_character, condition_id, ability
):
    c = make_character(conditions=[ActiveCondition(id=condition_id)], scores={DEX: 20, STR: 20})
    rng = ScriptedRng([])  # любой бросок упал бы ошибкой

    master = saving_throw(c, ability, dc=5, advantage=NONE, rng=rng, reason="тест")
    engine = engine_saving_throw(c, ability, dc=3, advantage=NONE, rng=rng, reason="тест")

    for result in (master, engine):
        assert (result.success, result.auto, result.condition) == (False, "condition", condition_id)
        assert (result.roll.rolls, result.roll.natural, result.total) == ([], None, 0)
        assert result.kind == CheckKind.SAVE


@pytest.mark.parametrize("ability", [CON, INT, WIS, CHA])
def test_auto_fail_does_not_touch_other_abilities(make_character, ability):
    c = make_character(conditions=[ActiveCondition(id="paralyzed")])

    result = save(c, [15], ability=ability)

    assert (result.success, result.auto, result.condition) == (True, None, None)


@pytest.mark.parametrize("condition_id", ["restrained", "prone", "blinded", "incapacitated"])
def test_other_conditions_do_not_fail_saves_automatically(make_character, condition_id):
    c = make_character(conditions=[ActiveCondition(id=condition_id)])

    result = save(c, [15, 15], ability=STR)

    assert result.auto is None and result.condition is None


def test_auto_fail_applies_to_saves_only_not_to_ability_checks(make_character):
    c = make_character(conditions=[ActiveCondition(id="paralyzed")])

    result = check(c, [15], ability=STR)

    assert (result.success, result.auto) == (True, None)


def test_auto_fail_names_the_first_blocking_condition(make_character):
    c = make_character(conditions=[ActiveCondition(id="poisoned"), ActiveCondition(id="stunned")])

    assert save(c, [], ability=DEX).condition == "stunned"


def test_dc_table_is_checked_before_auto_fail(make_character):
    c = make_character(conditions=[ActiveCondition(id="paralyzed")])

    with pytest.raises(ValueError, match="Сл"):
        save(c, [], ability=STR, dc=12)


def test_auto_fail_result_survives_json_round_trip(make_character):
    c = make_character(conditions=[ActiveCondition(id="unconscious")])
    result = save(c, [], ability=DEX)

    assert CheckResult.model_validate_json(result.model_dump_json()) == result


# --- спасброски монстров ---


def monster_save(
    srd: SrdContent,
    monster_id: str,
    values: list[int],
    *,
    ability: Ability = WIS,
    dc: int = 12,
    advantage: Advantage = NONE,
    conditions=(),
) -> CheckResult:
    return monster_saving_throw(
        srd.monsters[monster_id],
        f"{monster_id}-1",
        list(conditions),
        ability,
        dc=dc,
        advantage=advantage,
        rng=ScriptedRng(values),
        reason="тест",
    )


def test_monster_save_uses_the_statblock_bonus_when_it_differs_from_the_modifier(srd):
    # Зомби: Мудрость 6 даёт −2, но в статблоке спасбросок Мудрости +0.
    result = monster_save(srd, "zombie", [12], ability=WIS, dc=12)

    assert (result.bonus, result.total, result.success) == (0, 12, True)
    assert (result.actor_id, result.kind, result.ability) == ("zombie-1", CheckKind.SAVE, WIS)


def test_monster_save_falls_back_to_the_ability_modifier(srd):
    # Огр: Сила 19 — +4, спасброски в статблоке не записаны.
    result = monster_save(srd, "ogre", [7], ability=STR, dc=12)

    assert (result.bonus, result.total, result.success) == (4, 11, False)


def test_monster_save_follows_conditions_like_characters(srd):
    restrained = [ActiveCondition(id="restrained")]

    dex_save = monster_save(srd, "ogre", [4, 16], ability=DEX, conditions=restrained)
    wis_save = monster_save(srd, "ogre", [4], ability=WIS, conditions=restrained)

    assert (dex_save.roll.advantage, dex_save.roll.natural) == (DISADVANTAGE, 4)
    assert wis_save.roll.advantage == NONE


def test_monster_save_fails_without_a_roll_when_stunned(srd):
    result = monster_save(srd, "ogre", [], ability=STR, conditions=[ActiveCondition(id="stunned")])

    assert (result.success, result.auto, result.condition) == (False, "condition", "stunned")


def test_monster_save_takes_the_advantage_parameter(srd):
    result = monster_save(srd, "ogre", [4, 16], ability=CON, advantage=ADVANTAGE)

    assert (result.roll.advantage, result.roll.natural) == (ADVANTAGE, 16)


def test_combine_advantage_cancels_any_number_of_opposite_sources():
    assert combine_advantage(ADVANTAGE, ADVANTAGE, DISADVANTAGE) == NONE
    assert combine_advantage(NONE, DISADVANTAGE, DISADVANTAGE) == DISADVANTAGE
    assert combine_advantage() == NONE


# --- проверка характеристики и навыка ---


def test_skill_check_reports_every_number_of_the_roll_card(make_character):
    c = make_character(scores={DEX: 14}, skill_proficiencies=[Skill.ACROBATICS])  # Лов +2, БМ +2

    result = check(c, [14], skill=Skill.ACROBATICS, dc=15)

    assert result.actor_id == "pc-1"
    assert result.kind == CheckKind.SKILL
    assert (result.ability, result.skill) == (DEX, Skill.ACROBATICS)
    assert (result.dc, result.bonus) == (15, 4)
    assert (result.roll.natural, result.roll.modifier, result.roll.total) == (14, 4, 18)
    assert result.total == 18
    assert result.success is True
    assert result.auto is None


def test_check_succeeds_exactly_at_the_dc_and_fails_one_below(make_character):
    c = make_character(scores={DEX: 14}, skill_proficiencies=[Skill.ACROBATICS])  # бонус +4

    at_dc = check(c, [11], skill=Skill.ACROBATICS, dc=15)
    below = check(c, [10], skill=Skill.ACROBATICS, dc=15)

    assert (at_dc.total, at_dc.success) == (15, True)
    assert (below.total, below.success) == (14, False)


def test_plain_ability_check_ignores_skill_proficiency(make_character):
    c = make_character(scores={DEX: 14}, skill_proficiencies=[Skill.STEALTH])

    result = check(c, [10], ability=DEX, skill=None)

    assert result.kind == CheckKind.ABILITY
    assert result.skill is None
    assert result.bonus == 2


def test_skill_proficiency_and_expertise_add_to_the_bonus(make_character):
    base = {"scores": {DEX: 14}}
    untrained = make_character(**base)
    trained = make_character(**base, skill_proficiencies=[Skill.STEALTH])
    expert = make_character(**base, skill_proficiencies=[Skill.STEALTH], expertise=[Skill.STEALTH])

    bonuses = [check(c, [10], skill=Skill.STEALTH).bonus for c in (untrained, trained, expert)]

    assert bonuses == [2, 4, 6]


def test_skill_can_be_used_with_a_non_default_ability(make_character):
    # «Сила (Запугивание)»: модификатор Силы вместо Харизмы, мастерство навыка сохраняется.
    c = make_character(scores={STR: 16}, skill_proficiencies=[Skill.INTIMIDATION])

    result = check(c, [10], ability=STR, skill=Skill.INTIMIDATION)

    assert result.ability == STR
    assert result.bonus == 3 + 2


def test_advantage_parameter_takes_the_higher_die_and_disadvantage_the_lower(make_character):
    c = make_character()

    high = check(c, [3, 17], advantage=ADVANTAGE)
    low = check(c, [3, 17], advantage=DISADVANTAGE)

    assert (high.roll.natural, high.roll.rolls, high.roll.advantage) == (17, [3, 17], ADVANTAGE)
    assert (low.roll.natural, low.roll.advantage) == (3, DISADVANTAGE)


def test_invalid_advantage_is_rejected(make_character):
    with pytest.raises(ValueError, match="foo"):
        check(make_character(), [10], advantage="foo")


def test_check_does_not_change_the_character(make_character):
    c = make_character(species="halfling", buffs=[bless()])
    before = c.model_copy(deep=True)

    check(c, [1, 15])
    save(c, [1, 15, 2])

    assert c == before


# --- спасбросок ---


def test_saving_throw_uses_save_bonus(make_character):
    c = make_character(scores={WIS: 14}, save_proficiencies=[WIS])  # Мдр +2, БМ +2

    result = save(c, [11], ability=WIS, dc=15)

    assert result.kind == CheckKind.SAVE
    assert (result.ability, result.skill) == (WIS, None)
    assert result.bonus == 4
    assert (result.total, result.success) == (15, True)


def test_saving_throw_fails_one_below_the_dc(make_character):
    c = make_character(scores={WIS: 14}, save_proficiencies=[WIS])

    result = save(c, [10], ability=WIS, dc=15)

    assert (result.total, result.success) == (14, False)


# --- Удача полурослика ---


def test_halfling_rerolls_a_natural_one_and_keeps_both_dice_visible(make_character):
    result = check(make_character(species="halfling"), [1, 15], dc=10)

    assert result.roll.rolls == [1, 15]
    assert result.roll.kept == [15]
    assert result.roll.natural == 15
    assert (result.total, result.success) == (15, True)


def test_halfling_keeps_a_second_natural_one(make_character):
    # Перебросить можно один раз, новое значение обязательно, даже если снова 1.
    result = check(make_character(species="halfling"), [1, 1], dc=10)

    assert result.roll.rolls == [1, 1]
    assert (result.roll.natural, result.success) == (1, False)


def test_halfling_does_not_reroll_other_results(make_character):
    result = check(make_character(species="halfling"), [2])

    assert result.roll.rolls == [2]


def test_other_species_do_not_reroll_a_natural_one(make_character):
    result = check(make_character(species="human"), [1])

    assert result.roll.natural == 1


def test_halfling_luck_applies_to_saving_throws(make_character):
    result = save(make_character(species="halfling"), [1, 15], dc=10)

    assert (result.roll.natural, result.success) == (15, True)


def test_halfling_with_disadvantage_replaces_only_the_die_that_showed_one(make_character):
    halfling = make_character(species="halfling")

    better = check(halfling, [1, 12, 18], advantage=DISADVANTAGE)
    worse = check(halfling, [1, 12, 5], advantage=DISADVANTAGE)

    # Вторая кость (12) остаётся в игре: при помехе берётся меньшая из неё и новой.
    assert (better.roll.natural, better.roll.rolls) == (12, [1, 12, 18])
    assert worse.roll.natural == 5


def test_halfling_with_advantage_rerolls_only_when_both_dice_show_one(make_character):
    halfling = make_character(species="halfling")

    both_ones = check(halfling, [1, 1, 9], advantage=ADVANTAGE)
    one_good_die = check(halfling, [1, 14], advantage=ADVANTAGE)

    assert (both_ones.roll.natural, both_ones.roll.rolls) == (9, [1, 1, 9])
    assert (one_good_die.roll.natural, one_good_die.roll.rolls) == (14, [1, 14])


# --- Гномья хитрость ---


@pytest.mark.parametrize("ability", [INT, WIS, CHA])
def test_gnome_has_advantage_on_mental_saves(make_character, ability):
    result = save(make_character(species="gnome"), [4, 16], ability=ability)

    assert (result.roll.advantage, result.roll.natural) == (ADVANTAGE, 16)


@pytest.mark.parametrize("ability", [STR, DEX, CON])
def test_gnome_has_no_advantage_on_physical_saves(make_character, ability):
    result = save(make_character(species="gnome"), [4], ability=ability)

    assert result.roll.advantage == NONE


def test_gnomish_cunning_does_not_apply_to_ability_checks(make_character):
    result = check(make_character(species="gnome"), [4], ability=INT)

    assert result.roll.advantage == NONE


def test_other_species_have_no_advantage_on_mental_saves(make_character):
    result = save(make_character(species="human"), [4], ability=WIS)

    assert result.roll.advantage == NONE


# --- Дварфийская стойкость ---


@pytest.mark.parametrize("against", ["poison", "poisoned"])
def test_dwarf_has_advantage_on_saves_against_poison(make_character, against):
    result = save(make_character(species="dwarf"), [4, 16], ability=CON, against=against)

    assert (result.roll.advantage, result.roll.natural) == (ADVANTAGE, 16)


@pytest.mark.parametrize("against", [None, "fire", "frightened"])
def test_dwarf_has_no_advantage_on_other_saves(make_character, against):
    result = save(make_character(species="dwarf"), [4], ability=CON, against=against)

    assert result.roll.advantage == NONE


def test_other_species_get_nothing_from_the_poison_tag(make_character):
    result = save(make_character(species="human"), [4], ability=CON, against="poison")

    assert result.roll.advantage == NONE


# --- преимущество и помеха из разных источников ---


def test_advantage_and_disadvantage_from_any_sources_cancel_out(make_character):
    gnome = make_character(species="gnome")

    result = save(gnome, [4], ability=WIS, advantage=DISADVANTAGE)

    assert result.roll.advantage == NONE
    assert result.roll.rolls == [4]


def test_several_disadvantages_do_not_beat_one_advantage(make_character):
    c = make_character(
        conditions=[ActiveCondition(id="poisoned"), ActiveCondition(id="frightened")]
    )

    result = check(c, [4], advantage=ADVANTAGE)

    assert result.roll.advantage == NONE


# --- благословение ---


def test_bless_adds_a_separate_d4_to_saving_throws(make_character):
    c = make_character(scores={WIS: 14}, save_proficiencies=[WIS], buffs=[bless()])  # бонус +4

    result = save(c, [10, 3], ability=WIS, dc=15)

    assert result.roll.rolls == [10, 3]
    assert result.roll.natural == 10  # натуральное значение — по к20, не по кости благословения
    assert result.bonus == 4
    assert (result.total, result.roll.total) == (17, 17)
    assert result.success is True


def test_bless_does_not_apply_to_ability_checks(make_character):
    # «Благословение» в SRD 5.2.1 действует на броски атаки и спасброски, но не на проверки.
    c = make_character(buffs=[bless()])

    result = check(c, [10])

    assert result.roll.rolls == [10]


def test_saving_throw_without_bless_uses_a_single_die(make_character):
    result = save(make_character(), [10])

    assert result.roll.rolls == [10]


def test_bless_with_advantage_keeps_all_dice_visible(make_character):
    result = save(make_character(buffs=[bless()]), [4, 16, 2], advantage=ADVANTAGE)

    assert result.roll.rolls == [4, 16, 2]
    assert result.roll.kept == [16, 2]
    assert result.total == 18


@pytest.mark.parametrize(
    ("species", "advantage", "values"),
    [
        ("halfling", NONE, [1, 15, 3]),
        ("halfling", DISADVANTAGE, [1, 12, 5, 3]),
        ("human", ADVANTAGE, [4, 16, 2]),
    ],
)
def test_total_always_equals_kept_dice_plus_modifier(make_character, species, advantage, values):
    c = make_character(species=species, scores={WIS: 14}, save_proficiencies=[WIS])
    c.buffs.append(bless())

    result = save(c, values, advantage=advantage)

    assert sum(result.roll.kept) + result.roll.modifier == result.roll.total == result.total


# --- состояния ---


@pytest.mark.parametrize("condition_id", ["poisoned", "frightened"])
def test_poisoned_and_frightened_give_disadvantage_on_ability_checks(make_character, condition_id):
    c = make_character(conditions=[ActiveCondition(id=condition_id)])

    plain = check(c, [4, 16])
    skill = check(c, [4, 16], skill=Skill.STEALTH)

    assert (plain.roll.advantage, plain.roll.natural) == (DISADVANTAGE, 4)
    assert (skill.roll.advantage, skill.roll.natural) == (DISADVANTAGE, 4)


@pytest.mark.parametrize("condition_id", ["poisoned", "frightened"])
def test_poisoned_and_frightened_do_not_affect_saving_throws(make_character, condition_id):
    c = make_character(conditions=[ActiveCondition(id=condition_id)])

    result = save(c, [4])

    assert result.roll.advantage == NONE


def test_restrained_gives_disadvantage_on_dexterity_saves_only(make_character):
    c = make_character(conditions=[ActiveCondition(id="restrained")])

    dex_save = save(c, [4, 16], ability=DEX)
    str_save = save(c, [4], ability=STR)
    dex_check = check(c, [4], ability=DEX)

    assert (dex_save.roll.advantage, dex_save.roll.natural) == (DISADVANTAGE, 4)
    assert str_save.roll.advantage == NONE
    assert dex_check.roll.advantage == NONE


def test_unrelated_conditions_change_nothing(make_character):
    c = make_character(conditions=[ActiveCondition(id="prone"), ActiveCondition(id="charmed")])

    assert check(c, [4]).roll.advantage == NONE
    assert save(c, [4]).roll.advantage == NONE


# --- базовые правила без модулей: натуральные 20 и 1 ничего не решают сами ---


@pytest.mark.parametrize("kind", ["check", "save"])
def test_natural_20_is_not_an_automatic_success_without_hooks(make_character, kind):
    c = make_character(scores={DEX: 8, WIS: 8})  # бонус −1
    run = check if kind == "check" else save

    result = run(c, [20], dc=25)

    assert (result.roll.natural, result.total) == (20, 19)
    assert (result.success, result.auto) == (False, None)


@pytest.mark.parametrize("kind", ["check", "save"])
def test_natural_1_is_not_an_automatic_failure_without_hooks(make_character, kind):
    c = make_character(scores={DEX: 20, WIS: 20})  # бонус +5
    run = check if kind == "check" else save

    result = run(c, [1], dc=5)

    assert (result.roll.natural, result.total) == (1, 6)
    assert (result.success, result.auto) == (True, None)


# --- результаты сериализуются для событий и карточек броска ---


def test_check_result_survives_json_round_trip_with_luck_and_bless(make_character):
    c = make_character(species="halfling", buffs=[bless()], scores={WIS: 14})
    result = save(c, [1, 15, 3], dc=15)

    restored = CheckResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.roll.rolls == [1, 15, 3]
    assert restored.kind == CheckKind.SAVE


def test_check_result_with_automatic_outcome_survives_json_round_trip(make_character):
    result = check(make_character(), [20], dc=30, hooks=[force_success_on_nat20])

    restored = CheckResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert (restored.success, restored.auto) == (True, "nat20")


def test_contest_result_survives_json_round_trip():
    a = ContestSide(actor_id="pc-1", label="Сила (Атлетика)", bonus=5)
    b = ContestSide(actor_id="npc-1", label="Сила (Атлетика)", bonus=-1)
    result = contest(a, b, rng=ScriptedRng([10, 12]), reason="армрестлинг")

    assert ContestResult.model_validate_json(result.model_dump_json()) == result


# --- таблицы правил нельзя изменить во время игры ---


def test_rule_tables_are_read_only():
    with pytest.raises(TypeError):
        DC_TABLE["trivial"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        CHECK_TRAIT_BY_SPECIES["human"] = "luck"  # type: ignore[index]
    with pytest.raises(TypeError):
        SAVE_DISADVANTAGE_CONDITIONS["poisoned"] = frozenset({DEX})  # type: ignore[index]
    with pytest.raises(AttributeError):
        VALID_DCS.add(12)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        CHECK_DISADVANTAGE_CONDITIONS.add("blinded")  # type: ignore[attr-defined]


# --- хуки ---


def force_success_on_nat20(result: CheckResult) -> CheckResult:
    if result.roll.natural == 20:
        return result.model_copy(update={"success": True, "auto": "nat20"})
    return result


def test_hook_can_turn_a_natural_20_into_success(make_character):
    result = check(make_character(), [20], dc=30, hooks=[force_success_on_nat20])

    assert result.total == 20  # итог не подменяется, меняется только исход
    assert (result.success, result.auto) == (True, "nat20")


def test_hook_leaves_other_results_untouched(make_character):
    result = check(make_character(), [19], dc=30, hooks=[force_success_on_nat20])

    assert (result.success, result.auto) == (False, None)


def test_hooks_run_in_order_and_each_sees_the_previous_result(make_character):
    seen: list[tuple[str, bool, str | None]] = []

    def first(result: CheckResult) -> CheckResult:
        seen.append(("first", result.success, result.auto))
        return result.model_copy(update={"success": True, "auto": "nat20"})

    def second(result: CheckResult) -> CheckResult:
        seen.append(("second", result.success, result.auto))
        return result.model_copy(update={"auto": "nat1"})

    result = save(make_character(), [3], dc=30, hooks=[first, second])

    assert seen == [("first", False, None), ("second", True, "nat20")]
    assert (result.success, result.auto) == (True, "nat1")


def test_hook_receives_the_final_roll_with_luck_and_bless(make_character):
    received: list[CheckResult] = []

    def spy(result: CheckResult) -> CheckResult:
        received.append(result)
        return result

    c = make_character(species="halfling", buffs=[bless()])
    save(c, [1, 15, 3], hooks=[spy])

    assert [r.roll.rolls for r in received] == [[1, 15, 3]]


# --- состязание ---


def test_contest_goes_to_the_higher_total():
    a = ContestSide(actor_id="pc-1", label="Сила (Атлетика)", bonus=5)
    b = ContestSide(actor_id="npc-1", label="Сила (Атлетика)", bonus=2)

    result = contest(a, b, rng=ScriptedRng([10, 12]), reason="армрестлинг")

    assert (result.a_roll.total, result.b_roll.total) == (15, 14)
    assert result.winner == "a"
    assert (result.a, result.b) == (a, b)


def test_contest_roll_order_is_a_then_b():
    a = ContestSide(actor_id="pc-1", label="А", bonus=0)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    result = contest(a, b, rng=ScriptedRng([14, 3]), reason="тест")

    assert (result.a_roll.natural, result.b_roll.natural) == (14, 3)


def test_contest_tie_goes_to_b_the_defender():
    a = ContestSide(actor_id="pc-1", label="А", bonus=3)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    result = contest(a, b, rng=ScriptedRng([10, 13]), reason="тест")

    assert result.a_roll.total == result.b_roll.total == 13
    assert result.winner == "b"


def test_contest_goes_to_b_when_b_is_higher():
    a = ContestSide(actor_id="pc-1", label="А", bonus=0)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    assert contest(a, b, rng=ScriptedRng([4, 5]), reason="тест").winner == "b"


def test_contest_rolls_name_their_side():
    a = ContestSide(actor_id="pc-1", label="Ловкость (Скрытность)", bonus=0)
    b = ContestSide(actor_id="npc-1", label="Мудрость (Внимательность)", bonus=0)

    result = contest(a, b, rng=ScriptedRng([4, 5]), reason="засада")

    assert "Ловкость (Скрытность)" in result.a_roll.reason
    assert "Мудрость (Внимательность)" in result.b_roll.reason
    assert "засада" in result.a_roll.reason


# --- связь с данными SRD ---


@pytest.mark.parametrize(("species_id", "trait_id"), CHECK_TRAIT_BY_SPECIES.items())
def test_check_traits_exist_as_mechanical_traits_in_srd_data(srd, species_id, trait_id):
    traits = {trait.id: trait for trait in srd.species[species_id].traits}

    assert traits[trait_id].mechanical is True


# --- формат результата, который видит клиент ---


def test_check_kind_values_are_stable_strings():
    assert [kind.value for kind in CheckKind] == ["ability", "skill", "save"]


def test_check_hook_is_a_function_from_result_to_result():
    assert get_args(CheckHook) == ([CheckResult], CheckResult)


def test_roll_expression_shows_the_blessing_die_next_to_the_d20(make_character):
    c = make_character(buffs=[bless()], scores={WIS: 14})

    blessed = save(c, [12, 3])
    plain = save(make_character(scores={WIS: 14}), [12])

    # Кость благословения видна в записи броска; сумма записи совпадает с числом костей.
    assert plain.roll.expr == "1d20+2"
    assert blessed.roll.expr == "1d20+2+1d4"
    terms = parse(blessed.roll.expr).terms
    assert [(term.count, term.sides) for term in terms] == [(1, 20), (1, 4)]
    assert parse(blessed.roll.expr).modifier == blessed.bonus == 2
    assert len(blessed.roll.rolls) == sum(term.count for term in terms)


# --- указание: +1к4 к проверкам (не к спасброскам) ---


def test_guidance_buff_id_matches_the_spell_data(srd):
    assert srd.spells["guidance"].effect.buff == GUIDANCE_BUFF == "guidance"
    assert srd.spells["bless"].effect.buff == BLESS_BUFF == "bless"


def test_guidance_adds_a_separate_d4_to_ability_checks(make_character):
    c = make_character(scores={DEX: 14}, buffs=[guidance()])  # бонус +2

    result = check(c, [10, 3], dc=15)

    assert result.roll.rolls == [10, 3]
    assert result.roll.natural == 10  # натуральное значение — по к20, не по кости указания
    assert result.bonus == 2
    assert (result.total, result.roll.total) == (15, 15)
    assert result.success is True
    assert result.roll.expr == "1d20+2+1d4"


def test_guidance_adds_a_d4_to_skill_checks(make_character):
    c = make_character(scores={DEX: 14}, buffs=[guidance()])

    result = check(c, [10, 3], skill=Skill.STEALTH)

    assert result.kind == CheckKind.SKILL
    assert result.roll.rolls == [10, 3]


def test_guidance_does_not_apply_to_saving_throws(make_character):
    c = make_character(buffs=[guidance()])

    assert save(c, [10]).roll.rolls == [10]
    rng = ScriptedRng([10])
    engine = engine_saving_throw(
        c, WIS, dc=13, advantage=NONE, rng=rng, reason="тест"
    )  # Сл движка — тот же путь
    assert engine.roll.rolls == [10]


def test_guidance_with_advantage_keeps_all_dice_visible(make_character):
    result = check(make_character(buffs=[guidance()]), [4, 16, 2], advantage=ADVANTAGE)

    assert result.roll.rolls == [4, 16, 2]
    assert (result.roll.kept, result.total) == ([16, 2], 18)


def test_guidance_comes_after_halfling_luck(make_character):
    c = make_character(species="halfling", buffs=[guidance()])

    result = check(c, [1, 15, 3])

    assert result.roll.rolls == [1, 15, 3]
    assert (result.roll.natural, result.total) == (15, 18)


def test_guidance_and_bless_do_not_mix_up(make_character):
    c = make_character(buffs=[bless(), guidance()])

    assert check(c, [10, 2]).roll.rolls == [10, 2]  # проверка: только указание
    assert save(c, [10, 2]).roll.rolls == [10, 2]  # спасбросок: только благословение


# --- удача и благословение для других бросков к20 (атака, спасбросок от смерти, инициатива) ---


def d20(values: list[int], modifier: int = 3, advantage: Advantage = NONE):
    rng = ScriptedRng(values)
    return roll(f"1d20{modifier:+d}", rng=rng, advantage=advantage, reason="атака"), rng


def test_apply_luck_rerolls_a_natural_one_for_a_halfling(make_character):
    first, _ = d20([1])

    result = apply_luck(make_character(species="halfling"), first, rng=ScriptedRng([14]))

    assert (result.natural, result.total, result.rolls) == (14, 17, [1, 14])
    assert first.natural == 1  # исходный бросок не изменился


def test_apply_luck_ignores_other_species_and_other_values(make_character):
    first, _ = d20([1])
    other, _ = d20([2])
    halfling = make_character(species="halfling")

    unchanged = apply_luck(make_character(species="human"), first, rng=ScriptedRng([]))
    two = apply_luck(halfling, other, rng=ScriptedRng([]))

    assert unchanged == first
    assert two == other  # кубы не бросались: ScriptedRng пуст


def test_apply_bless_adds_a_d4_only_when_the_buff_is_present(make_character):
    first, _ = d20([12])

    blessed = apply_bless(make_character(buffs=[bless()]), first, rng=ScriptedRng([3]))
    plain = apply_bless(make_character(), first, rng=ScriptedRng([]))

    assert (blessed.rolls, blessed.total, blessed.natural) == ([12, 3], 18, 12)
    assert blessed.expr == "1d20+3+1d4"
    assert plain == first


def test_the_same_effect_from_two_casters_adds_only_one_die(make_character):
    def twice(buff_id: str) -> list[ActiveBuff]:
        return [ActiveBuff(id=buff_id, rounds_left=10, source_id=f"cleric-{n}") for n in (1, 2)]

    blessed = make_character(buffs=twice("bless"))
    guided = make_character(buffs=twice("guidance"))
    first, _ = d20([12])

    # Третье значение очереди остаётся нетронутым: кость добавлена одна, а не по одной на жреца.
    assert apply_bless(blessed, first, rng=ScriptedRng([3, 4])).rolls == [12, 3]
    assert save(blessed, [10, 2, 4]).roll.rolls == [10, 2]
    assert check(guided, [10, 2, 4]).roll.rolls == [10, 2]


# --- состязание: сторона знает про персонажа (Удача, Указание, помехи) ---


def test_check_label_names_the_ability_and_the_skill():
    assert check_label(DEX, Skill.STEALTH) == "Ловкость (Скрытность)"
    assert check_label(STR) == "Сила"
    assert check_label(STR, Skill.INTIMIDATION) == "Сила (Запугивание)"  # нестандартная пара


def test_character_side_takes_the_skill_bonus_from_the_character(make_character):
    c = make_character(id="pc-7", scores={STR: 16}, skill_proficiencies=[Skill.ATHLETICS])

    side = character_contest_side(c, Skill.ATHLETICS)

    # Сила 16 (+3) и мастерство 3-го уровня (+2).
    assert (side.actor_id, side.label, side.bonus) == ("pc-7", "Сила (Атлетика)", 5)
    assert (side.advantage, side.luck, side.guidance) == (NONE, False, False)


def test_character_side_for_a_bare_ability_uses_only_the_modifier(make_character):
    c = make_character(scores={STR: 16}, skill_proficiencies=[Skill.ATHLETICS])

    side = character_contest_side(c, STR)

    assert (side.label, side.bonus) == ("Сила", 3)


def test_character_side_flags_halfling_luck_and_guidance(make_character):
    lucky = make_character(species="halfling", buffs=[guidance()])
    plain = make_character()

    flagged = character_contest_side(lucky, Skill.STEALTH)

    assert (flagged.luck, flagged.guidance) == (True, True)
    assert (character_contest_side(plain, Skill.STEALTH).luck) is False


@pytest.mark.parametrize(
    ("condition", "given", "expected"),
    [
        ("poisoned", NONE, DISADVANTAGE),
        ("frightened", NONE, DISADVANTAGE),
        ("poisoned", ADVANTAGE, NONE),  # вдохновение гасит помеху состояния
        ("blinded", NONE, NONE),  # состояние не влияет на проверки
    ],
)
def test_character_side_combines_given_advantage_with_condition_disadvantage(
    make_character, condition, given, expected
):
    c = make_character(conditions=[ActiveCondition(id=condition)])

    assert character_contest_side(c, Skill.ATHLETICS, advantage=given).advantage == expected


def test_monster_side_takes_the_statblock_skill_bonus(srd):
    fanatic = srd.monsters["cultist_fanatic"]

    side = monster_contest_side(fanatic, "fabian", Skill.DECEPTION)

    # В статблоке навык записан итоговым бонусом: Обман +3.
    assert (side.actor_id, side.label, side.bonus) == ("fabian", "Харизма (Обман)", 3)
    assert (side.advantage, side.luck, side.guidance) == (NONE, False, False)


def test_monster_side_without_the_skill_falls_back_to_the_ability_modifier(srd):
    fanatic = srd.monsters["cultist_fanatic"]

    assert monster_contest_side(fanatic, "m", Skill.ATHLETICS).bonus == 0  # Сила 11
    assert monster_contest_side(fanatic, "m", DEX).bonus == 2  # Ловкость 14
    assert monster_contest_side(fanatic, "m", DEX).label == "Ловкость"


def test_monster_side_gets_disadvantage_from_its_conditions(srd):
    fanatic = srd.monsters["cultist_fanatic"]

    poisoned = monster_contest_side(
        fanatic, "m", Skill.DECEPTION, conditions=[ActiveCondition(id="poisoned")]
    )

    assert poisoned.advantage == DISADVANTAGE


def test_contest_side_rerolls_a_natural_one_with_luck():
    a = ContestSide(actor_id="pc-1", label="А", bonus=2, luck=True)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    result = contest(a, b, rng=ScriptedRng([1, 14, 9]), reason="тест")

    assert result.a_roll.rolls == [1, 14]  # оба броска видны
    assert (result.a_roll.natural, result.a_roll.total) == (14, 16)
    assert result.b_roll.natural == 9
    assert result.winner == "a"


def test_contest_luck_does_not_trigger_on_other_values():
    a = ContestSide(actor_id="pc-1", label="А", bonus=0, luck=True)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0, luck=True)

    result = contest(a, b, rng=ScriptedRng([2, 1, 17]), reason="тест")

    # У a перебросов нет; единица у b перебрасывается после броска a.
    assert (result.a_roll.rolls, result.b_roll.rolls) == ([2], [1, 17])


def test_contest_side_adds_a_guidance_die_after_its_own_d20():
    a = ContestSide(actor_id="pc-1", label="А", bonus=1, guidance=True)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    result = contest(a, b, rng=ScriptedRng([10, 3, 12]), reason="тест")

    assert result.a_roll.rolls == [10, 3]
    assert (result.a_roll.natural, result.a_roll.total) == (10, 14)
    assert result.b_roll.rolls == [12]
    assert result.winner == "a"


def test_contest_side_rolls_two_dice_with_advantage_or_disadvantage():
    a = ContestSide(actor_id="pc-1", label="А", bonus=0, advantage=ADVANTAGE)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0, advantage=DISADVANTAGE)

    result = contest(a, b, rng=ScriptedRng([4, 16, 18, 7]), reason="тест")

    assert (result.a_roll.natural, result.b_roll.natural) == (16, 7)
    assert (result.a_roll.advantage, result.b_roll.advantage) == (ADVANTAGE, DISADVANTAGE)


def test_contest_side_with_luck_and_guidance_rolls_in_a_fixed_order():
    a = ContestSide(actor_id="pc-1", label="А", bonus=0, luck=True, guidance=True)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0)

    result = contest(a, b, rng=ScriptedRng([1, 15, 2, 5]), reason="тест")

    # Сначала к20, затем переброс единицы (Удача), затем кость Указания, и только потом сторона b.
    assert result.a_roll.rolls == [1, 15, 2]
    assert (result.a_roll.natural, result.a_roll.total) == (15, 17)
    assert result.b_roll.rolls == [5]


def test_contest_result_with_effects_survives_json_round_trip():
    a = ContestSide(actor_id="pc-1", label="А", bonus=1, luck=True, guidance=True)
    b = ContestSide(actor_id="npc-1", label="Б", bonus=0, advantage=DISADVANTAGE)
    result = contest(a, b, rng=ScriptedRng([1, 15, 2, 5, 8]), reason="тест")

    assert ContestResult.model_validate_json(result.model_dump_json()) == result
