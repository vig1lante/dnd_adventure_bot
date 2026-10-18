"""Тесты создания персонажа: способы задать характеристики, `build_character`,
`quick_character` и `random_character`.

Базовые выборы по классам (`CHOICES`) легальны сами по себе; тесты ломают в них ровно то
правило, которое проверяют. Случайность — только через `SeededRng` и `ScriptedRng`.
"""

import json
from collections import Counter
from typing import Any

import pytest
from pydantic import ValidationError

from dnd_engine import creation
from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_NAMES_RU, Ability, Skill, modifier
from dnd_engine.character import Character, Persona, armor_class
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import (
    POINT_BUY_BUDGET,
    POINT_BUY_COST,
    STANDARD_ARRAY,
    AbilityMethod,
    CharacterChoices,
    CharacterValidationError,
    build_character,
    quick_character,
    random_character,
    roll_ability_set,
)
from dnd_engine.rng import ScriptedRng, SeededRng

STR, DEX, CON, INT, WIS, CHA = Ability

CLASS_IDS = ("barbarian", "fighter", "rogue", "ranger", "cleric", "wizard")
SPECIES_IDS = (
    "dragonborn",
    "dwarf",
    "elf",
    "gnome",
    "goliath",
    "halfling",
    "human",
    "orc",
    "tiefling",
)

# Легальный выбор для каждого класса: покупка очков, бонус предыстории из её списка,
# навыки без пересечения с навыками предыстории и вида.
CHOICES: dict[str, dict[str, Any]] = {
    "barbarian": {
        "species": "dwarf",
        "background": "soldier",
        "base_scores": {STR: 15, DEX: 12, CON: 13, INT: 8, WIS: 10, CHA: 8},
        "background_bonus": {STR: 2, CON: 1},
        "skills": [Skill.PERCEPTION, Skill.SURVIVAL],
    },
    "fighter": {
        "species": "dwarf",
        "background": "soldier",
        "base_scores": {STR: 15, DEX: 12, CON: 14, INT: 8, WIS: 10, CHA: 8},
        "background_bonus": {STR: 2, CON: 1},
        "skills": [Skill.PERCEPTION, Skill.SURVIVAL],
        "fighting_style": "defense",
    },
    "rogue": {
        "species": "elf",
        "background": "criminal",
        "base_scores": {STR: 8, DEX: 15, CON: 14, INT: 10, WIS: 12, CHA: 8},
        "background_bonus": {DEX: 2, CON: 1},
        "skills": [Skill.ACROBATICS, Skill.ATHLETICS, Skill.DECEPTION, Skill.INSIGHT],
        "expertise": [Skill.ACROBATICS, Skill.STEALTH],
    },
    "ranger": {
        "species": "halfling",
        "background": "soldier",
        "base_scores": {STR: 10, DEX: 15, CON: 13, INT: 8, WIS: 14, CHA: 8},
        "background_bonus": {DEX: 2, CON: 1},
        "skills": [Skill.PERCEPTION, Skill.STEALTH, Skill.SURVIVAL],
        "fighting_style": "archery",
        "prepared_spells": ["hunters_mark", "cure_wounds", "goodberry", "fog_cloud"],
    },
    "cleric": {
        "species": "dwarf",
        "background": "acolyte",
        "base_scores": {STR: 10, DEX: 8, CON: 14, INT: 8, WIS: 15, CHA: 8},
        "background_bonus": {WIS: 2, CHA: 1},
        "skills": [Skill.MEDICINE, Skill.PERSUASION],
        "cantrips": ["sacred_flame", "guidance", "spare_the_dying"],
        "prepared_spells": [
            "bless",
            "cure_wounds",
            "healing_word",
            "guiding_bolt",
            "aid",
            "lesser_restoration",
        ],
    },
    "wizard": {
        "species": "human",
        "background": "sage",
        "base_scores": {STR: 8, DEX: 12, CON: 14, INT: 15, WIS: 10, CHA: 8},
        "background_bonus": {INT: 2, CON: 1},
        "skills": [Skill.INVESTIGATION, Skill.INSIGHT],
        "extra_skill": Skill.MEDICINE,
        "cantrips": ["fire_bolt", "ray_of_frost", "mage_hand"],
        "prepared_spells": [
            "magic_missile",
            "mage_armor",
            "burning_hands",
            "thunderwave",
            "scorching_ray",
            "misty_step",
        ],
    },
}


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def make_choices(base: str = "barbarian", **overrides: Any) -> CharacterChoices:
    """Легальный выбор на основе `CHOICES[base]`; `overrides` заменяют поля целиком."""
    data = {
        "name": "Тестовый герой",
        "class_id": base,
        "method": AbilityMethod.POINT_BUY,
    }
    return CharacterChoices(**(data | CHOICES[base] | overrides))


def build(
    srd: SrdContent,
    base: str = "barbarian",
    *,
    rolled_set: list[int] | None = None,
    **overrides: Any,
) -> Character:
    choices = make_choices(base, **overrides)
    return build_character(choices, srd, rolled_set=rolled_set, character_id="pc-1")


def problems_of(
    srd: SrdContent,
    base: str = "barbarian",
    *,
    rolled_set: list[int] | None = None,
    **overrides: Any,
) -> list[str]:
    """Список проблем, с которыми `build_character` отклоняет выбор."""
    with pytest.raises(CharacterValidationError) as info:
        build(srd, base, rolled_set=rolled_set, **overrides)
    return info.value.problems


def assert_problem(problems: list[str], *fragments: str) -> None:
    """Среди проблем есть хотя бы одна, в тексте которой встречаются все фрагменты."""
    assert any(all(f in p for f in fragments) for p in problems), (
        f"нет проблемы с фрагментами {fragments}: {problems}"
    )


def names(*skills: Skill) -> str:
    return ", ".join(SKILL_NAMES_RU[skill] for skill in skills)


# --- таблицы правил ---


def test_rule_tables_match_srd():
    assert POINT_BUY_BUDGET == 27
    assert STANDARD_ARRAY == (15, 14, 13, 12, 10, 8)
    assert POINT_BUY_COST == {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}


def test_choices_survive_json_round_trip():
    choices = make_choices("wizard", persona=Persona(concept="отшельник"))

    assert CharacterChoices.model_validate_json(choices.model_dump_json()) == choices


# --- roll_ability_set ---


def dice_for(scores: list[int]) -> list[int]:
    """Грани четырёх костей на каждую характеристику: три старшие дают нужную сумму,
    четвёртая (отбрасываемая) — единица."""
    dice: list[int] = []
    for score in scores:
        base, extra = divmod(score, 3)
        dice += [base + 1] * extra + [base] * (3 - extra) + [1]
    return dice


def modifier_sum(scores: list[int]) -> int:
    return sum(modifier(score) for score in scores)


def test_roll_ability_set_is_balanced_for_many_seeds():
    for seed in range(200):
        scores = roll_ability_set(SeededRng(seed))

        assert len(scores) == 6, f"seed {seed}"
        assert all(3 <= score <= 18 for score in scores), f"seed {seed}: {scores}"
        assert 3 <= modifier_sum(scores) <= 7, f"seed {seed}: {scores}"


def test_roll_ability_set_depends_on_rng():
    results = {tuple(roll_ability_set(SeededRng(seed))) for seed in range(20)}

    assert len(results) > 1


def test_roll_ability_set_keeps_three_highest_dice_of_four():
    # Первая характеристика: кости 6, 5, 4, 1 -> 6 + 5 + 4 = 15, единица отброшена.
    first_dice = [6, 5, 4, 1]
    rng = ScriptedRng(first_dice + dice_for([14, 13, 12, 10, 8]))

    assert roll_ability_set(rng) == [15, 14, 13, 12, 10, 8]


def test_roll_ability_set_rerolls_whole_set_until_balanced():
    too_low = dice_for([8] * 6)  # сумма модификаторов -6
    too_high = dice_for([18] * 6)  # сумма модификаторов +24
    good = dice_for([15, 14, 13, 12, 10, 8])  # сумма модификаторов +5
    rng = ScriptedRng(too_low + too_high + good)

    assert roll_ability_set(rng) == [15, 14, 13, 12, 10, 8]
    with pytest.raises(ValueError, match="закончилась"):
        rng.randint(1, 6)  # все 72 кости использованы, лишних бросков не было


@pytest.mark.parametrize(
    ("rejected", "accepted"),
    [
        ([12, 12, 10, 10, 10, 10], [12, 12, 12, 10, 10, 10]),  # сумма 2 отклонена, 3 принята
        ([14, 14, 14, 12, 12, 10], [14, 14, 12, 12, 12, 10]),  # сумма 8 отклонена, 7 принята
    ],
)
def test_roll_ability_set_range_bounds_are_inclusive(rejected, accepted):
    assert modifier_sum(rejected) in (2, 8)
    assert modifier_sum(accepted) in (3, 7)

    assert roll_ability_set(ScriptedRng(dice_for(rejected) + dice_for(accepted))) == accepted


# --- build_character: общий результат ---


def test_build_character_assembles_expected_character(srd):
    character = build(srd, "barbarian", persona=Persona(concept="бывший стражник"))

    assert character.id == "pc-1"
    assert character.name == "Тестовый герой"
    assert (character.level, character.species, character.class_id, character.background) == (
        3,
        "dwarf",
        "barbarian",
        "soldier",
    )
    assert character.scores == {STR: 17, DEX: 12, CON: 14, INT: 8, WIS: 10, CHA: 8}
    assert character.save_proficiencies == [STR, CON]
    assert (character.hit_dice_max, character.hit_dice) == (3, 3)
    assert character.hp == character.hp_max
    assert character.temp_hp == 0
    assert character.inspiration is False
    assert character.persona.concept == "бывший стражник"


def test_character_does_not_share_mutable_state_with_choices_and_content(srd):
    persona = Persona(concept="жрец")
    first = build(srd, "cleric", persona=persona)
    second = build(srd, "cleric")

    first.persona.concept = "изменено"
    first.spell_slots[1] = 0
    first.spell_slots_max[2] = 99
    first.resources["channel_divinity"] = 0

    assert persona.concept == "жрец"
    assert second.spell_slots == {1: 4, 2: 2}
    assert second.spell_slots_max == {1: 4, 2: 2}
    assert second.resources == {"channel_divinity": 2}
    assert srd.classes["cleric"].spellcasting.slots[3] == {1: 4, 2: 2}
    assert srd.classes["cleric"].resources["channel_divinity"] == {2: 2, 3: 2}


# --- build_character: характеристики ---


def test_point_buy_spending_exactly_27_points_is_legal(srd):
    spent = {STR: 15, DEX: 15, CON: 15, INT: 8, WIS: 8, CHA: 8}
    assert sum(POINT_BUY_COST[value] for value in spent.values()) == 27

    character = build(srd, base_scores=spent)

    assert character.scores[DEX] == 15


def test_point_buy_spending_28_points_is_rejected(srd):
    spent = {STR: 15, DEX: 15, CON: 15, INT: 9, WIS: 8, CHA: 8}
    assert sum(POINT_BUY_COST[value] for value in spent.values()) == 28

    assert_problem(problems_of(srd, base_scores=spent), "покупка очков", "28", "27")


@pytest.mark.parametrize("value", [7, 16])
def test_point_buy_value_outside_8_to_15_is_rejected(srd, value):
    scores = CHOICES["barbarian"]["base_scores"] | {INT: value}

    assert_problem(problems_of(srd, base_scores=scores), ABILITY_NAMES_RU[INT], str(value))


def test_missing_ability_is_rejected(srd):
    scores = {a: v for a, v in CHOICES["barbarian"]["base_scores"].items() if a != CHA}

    assert_problem(problems_of(srd, base_scores=scores), "не заданы", ABILITY_NAMES_RU[CHA])


@pytest.mark.parametrize(
    "values",
    [
        (15, 14, 13, 12, 10, 8),
        (8, 10, 12, 13, 14, 15),
        (12, 15, 8, 14, 10, 13),
    ],
)
def test_standard_array_accepts_any_permutation(srd, values):
    scores = dict(zip(Ability, values, strict=True))

    character = build(srd, method=AbilityMethod.STANDARD_ARRAY, base_scores=scores)

    assert character.scores[DEX] == scores[DEX]
    assert character.scores[STR] == scores[STR] + 2  # бонус предыстории


@pytest.mark.parametrize(
    "values",
    [
        (15, 15, 13, 12, 10, 8),  # повтор 15
        (15, 14, 13, 12, 10, 9),  # 9 вместо 8
        (16, 14, 13, 12, 10, 8),  # 16 вместо 15
    ],
)
def test_standard_array_rejects_anything_but_permutation(srd, values):
    scores = dict(zip(Ability, values, strict=True))

    problems = problems_of(srd, method=AbilityMethod.STANDARD_ARRAY, base_scores=scores)

    assert_problem(problems, "стандартный набор", "15, 14, 13, 12, 10, 8")


def test_rolled_set_accepts_any_permutation(srd):
    rolled = [16, 15, 13, 12, 11, 9]
    scores = dict(zip(Ability, reversed(rolled), strict=True))

    character = build(srd, method=AbilityMethod.ROLL, base_scores=scores, rolled_set=rolled)

    assert character.scores[CHA] == 16


def test_rolled_set_must_be_a_permutation_of_the_server_set(srd):
    rolled = [16, 15, 13, 12, 11, 9]
    claimed = dict(zip(Ability, [18, 15, 13, 12, 11, 9], strict=True))

    problems = problems_of(srd, method=AbilityMethod.ROLL, base_scores=claimed, rolled_set=rolled)

    assert_problem(problems, "бросок", "перестановкой")


def test_roll_method_without_rolled_set_is_rejected(srd):
    scores = dict(zip(Ability, [16, 15, 13, 12, 11, 9], strict=True))

    problems = problems_of(srd, method=AbilityMethod.ROLL, base_scores=scores)

    assert_problem(problems, "rolled_set")


# --- build_character: сам выпавший набор должен быть легальным броском ---

GOOD_ROLLED_SET = [15, 14, 13, 12, 10, 8]  # сумма модификаторов +5

# Граничные наборы: (набор, мог ли он выпасть из `roll_ability_set`). Их проверяют и
# генератор, и `build_character`: у обоих одно правило легального броска.
BOUNDARY_SETS = [
    ([12, 12, 12, 10, 10, 10], True),  # сумма модификаторов 3 — нижняя граница
    ([16, 15, 14, 12, 10, 8], True),  # сумма модификаторов 7 — верхняя граница
    ([18, 15, 14, 10, 8, 3], True),  # крайние значения 18 и 3, сумма модификаторов 3
    ([12, 12, 10, 10, 10, 10], False),  # сумма модификаторов 2
    ([14, 14, 14, 12, 12, 10], False),  # сумма модификаторов 8
    ([3] * 6, False),
    ([18] * 6, False),
]

# Наборы, которые генератор выдать не может, но сервер (по ошибке) мог бы передать.
IMPOSSIBLE_ROLLED_SETS = [
    pytest.param([18, 18, 18, 19, 19, 19], id="nineteens"),
    pytest.param([-50] * 6, id="negative-values"),
    pytest.param([2, 15, 14, 13, 12, 12], id="below-3-sum-in-range"),
    pytest.param([19, 13, 12, 10, 10, 10], id="above-18-sum-in-range"),
    pytest.param([15, 14, 13, 12, 10], id="five-values"),
    pytest.param([15, 14, 13, 12, 10, 8, 8], id="seven-values"),
    pytest.param([], id="empty"),
    pytest.param([15, 14, 13, 12, 10, True], id="bool-instead-of-int"),
    pytest.param([15.0, 14, 13, 12, 10, 8], id="float-instead-of-int"),
    pytest.param(["15", 14, 13, 12, 10, 8], id="str-instead-of-int"),
]


def rolled_choices(rolled: list, **overrides: Any) -> dict[str, Any]:
    """Выбор способа `ROLL`, где игрок «честно» раскладывает присланный набор по порядку.

    Если в наборе не шесть значений, недостающие берутся из `GOOD_ROLLED_SET`, чтобы
    характеристики были заданы полностью и проверялся именно набор."""
    scores = dict(zip(Ability, [*rolled, *GOOD_ROLLED_SET], strict=False))
    return {"method": AbilityMethod.ROLL, "base_scores": scores, "rolled_set": rolled} | overrides


@pytest.mark.parametrize(("rolled", "possible"), BOUNDARY_SETS)
def test_build_character_accepts_exactly_the_sets_the_generator_can_produce(srd, rolled, possible):
    if possible:
        assert build(srd, **rolled_choices(rolled)).scores[STR] == rolled[0] + 2
    else:
        problems = problems_of(srd, **rolled_choices(rolled))
        assert_problem(problems, "бросок", "не мог выпасть")


@pytest.mark.parametrize(("rolled", "possible"), BOUNDARY_SETS)
def test_roll_ability_set_keeps_exactly_the_sets_build_character_accepts(rolled, possible):
    rng = ScriptedRng(dice_for(rolled) + dice_for(GOOD_ROLLED_SET))

    assert roll_ability_set(rng) == (rolled if possible else GOOD_ROLLED_SET)


@pytest.mark.parametrize("rolled", IMPOSSIBLE_ROLLED_SETS)
def test_rolled_set_that_could_not_have_been_rolled_is_rejected(srd, rolled):
    problems = problems_of(srd, **rolled_choices(rolled))

    assert_problem(problems, "бросок", "не мог выпасть", "от 3 до 18")


def test_exploit_set_with_nineteens_cannot_make_a_character_with_all_scores_19(srd):
    rolled = [18, 18, 18, 19, 19, 19]

    with pytest.raises(CharacterValidationError):
        build(srd, **rolled_choices(rolled, background_bonus={DEX: 1, CON: 1, STR: 1}))


def test_hostile_rolled_set_is_reported_together_with_other_problems(srd):
    rolled = [-50] * 6

    problems = problems_of(srd, **rolled_choices(rolled, skills=[], background_bonus={STR: 3}))

    assert_problem(problems, "бросок", "не мог выпасть")
    assert_problem(problems, "ровно 2")
    assert_problem(problems, "бонус предыстории")


def test_legal_rolled_set_still_reports_a_scores_mismatch(srd):
    # Набор легален, но игрок заявил другие значения: по-прежнему проблема перестановки.
    claimed = dict(zip(Ability, [18, 15, 13, 12, 11, 9], strict=True))

    problems = problems_of(srd, **rolled_choices([16, 15, 13, 12, 11, 9], base_scores=claimed))

    assert_problem(problems, "бросок", "перестановкой")


@pytest.mark.parametrize("method", [AbilityMethod.POINT_BUY, AbilityMethod.STANDARD_ARRAY])
@pytest.mark.parametrize("rolled", [[-50] * 6, [18, 18, 18, 19, 19, 19], None])
def test_rolled_set_is_ignored_unless_the_method_is_roll(srd, method, rolled):
    scores = (
        CHOICES["barbarian"]["base_scores"]
        if method == AbilityMethod.POINT_BUY
        else dict(zip(Ability, STANDARD_ARRAY, strict=True))
    )

    character = build(srd, method=method, base_scores=scores, rolled_set=rolled)

    assert character.class_id == "barbarian"


def test_a_set_from_the_generator_is_always_accepted_as_rolled_set(srd):
    for seed in range(200):
        rolled = roll_ability_set(SeededRng(seed))

        character = build(srd, **rolled_choices(sorted(rolled, reverse=True)))

        assert character.scores[STR] == max(rolled) + 2, f"seed {seed}: {rolled}"


# --- build_character: бонус предыстории ---


@pytest.mark.parametrize(
    "bonus",
    [
        {STR: 2, DEX: 1},
        {CON: 2, STR: 1},
        {STR: 1, DEX: 1, CON: 1},
    ],
)
def test_background_bonus_legal_variants_are_added_to_base_scores(srd, bonus):
    base = CHOICES["barbarian"]["base_scores"]

    character = build(srd, background_bonus=bonus)

    for ability in Ability:
        assert character.scores[ability] == base[ability] + bonus.get(ability, 0)


@pytest.mark.parametrize(
    "bonus",
    [
        {},
        {STR: 2},  # одного +2 мало
        {STR: 3},
        {STR: 2, DEX: 2},
        {STR: 1, DEX: 1},  # два +1 вместо +2 и +1
        {STR: 2, DEX: 1, CON: 1},  # слишком много
        {STR: 2, DEX: 0},
        {STR: 1, DEX: 1, CON: 1, INT: 1},
        {STR: 2, INT: 1},  # Интеллекта нет в списке предыстории «Солдат»
        {STR: 1, DEX: 1, INT: 1},  # +1/+1/+1 обязаны лечь ровно на список предыстории
    ],
)
def test_illegal_background_bonus_is_rejected(srd, bonus):
    assert_problem(problems_of(srd, background_bonus=bonus), "бонус предыстории")


def test_final_score_above_20_is_rejected(srd):
    rolled = [19, 14, 13, 12, 10, 8]
    scores = dict(zip(Ability, rolled, strict=True))

    problems = problems_of(
        srd,
        method=AbilityMethod.ROLL,
        base_scores=scores,
        background_bonus={STR: 2, DEX: 1},
        rolled_set=rolled,
    )

    assert_problem(problems, ABILITY_NAMES_RU[STR], "21", "20")


def test_final_score_of_exactly_20_is_legal(srd):
    rolled = [18, 14, 13, 12, 10, 8]
    scores = dict(zip(Ability, rolled, strict=True))

    character = build(srd, method=AbilityMethod.ROLL, base_scores=scores, rolled_set=rolled)

    assert character.scores[STR] == 20


# --- build_character: навыки ---


def test_background_skills_are_added_automatically(srd):
    character = build(srd, "barbarian")

    # «Солдат» даёт Атлетику и Запугивание; два навыка выбраны игроком.
    assert sorted(character.skill_proficiencies) == sorted(
        [Skill.ATHLETICS, Skill.INTIMIDATION, Skill.PERCEPTION, Skill.SURVIVAL]
    )


@pytest.mark.parametrize(
    "skills",
    [[Skill.PERCEPTION], [Skill.PERCEPTION, Skill.SURVIVAL, Skill.NATURE], []],
)
def test_class_skill_count_must_match_skill_choices(srd, skills):
    assert_problem(problems_of(srd, skills=skills), "ровно 2", f"выбрано {len(skills)}")


def test_class_skill_outside_class_list_is_rejected(srd):
    problems = problems_of(srd, skills=[Skill.PERCEPTION, Skill.ARCANA])

    assert_problem(problems, names(Skill.ARCANA), "не входит")


def test_class_skill_chosen_twice_is_rejected(srd):
    problems = problems_of(srd, skills=[Skill.PERCEPTION, Skill.PERCEPTION])

    assert_problem(problems, names(Skill.PERCEPTION), "больше одного раза")


def test_class_skill_duplicating_background_skill_is_rejected_with_hint(srd):
    problems = problems_of(srd, skills=[Skill.ATHLETICS, Skill.PERCEPTION])

    # Подсказка называет источник дубля и перечисляет свободные навыки класса.
    assert_problem(problems, names(Skill.ATHLETICS), "Солдат", "выберите другой из")
    hint = next(p for p in problems if "выберите другой из" in p).split("выберите другой из")[1]
    assert names(Skill.SURVIVAL) in hint
    assert names(Skill.PERCEPTION) not in hint  # этот навык уже выбран


def test_elf_gets_perception_automatically(srd):
    character = build(srd, "rogue")

    assert Skill.PERCEPTION in character.skill_proficiencies
    assert len(character.skill_proficiencies) == len(set(character.skill_proficiencies)) == 7


def test_elf_choosing_perception_as_class_skill_is_rejected_with_hint(srd):
    skills = [Skill.PERCEPTION, Skill.ACROBATICS, Skill.DECEPTION, Skill.INSIGHT]

    problems = problems_of(srd, "rogue", skills=skills)

    assert_problem(problems, names(Skill.PERCEPTION), "Эльф", "выберите другой")


def test_human_gets_extra_skill_as_proficiency(srd):
    character = build(srd, "wizard")

    # «Мудрец» даёт Магию и Историю, класс — Анализ и Проницательность, человек — Медицину.
    assert sorted(character.skill_proficiencies) == sorted(
        [Skill.ARCANA, Skill.HISTORY, Skill.INVESTIGATION, Skill.INSIGHT, Skill.MEDICINE]
    )


def test_human_without_extra_skill_is_rejected(srd):
    assert_problem(problems_of(srd, "wizard", extra_skill=None), "extra_skill")


@pytest.mark.parametrize("duplicate", [Skill.ARCANA, Skill.INVESTIGATION])
def test_extra_skill_duplicating_other_proficiency_is_rejected(srd, duplicate):
    problems = problems_of(srd, "wizard", extra_skill=duplicate)

    assert_problem(problems, "дополнительный навык", names(duplicate))


def test_extra_skill_for_species_without_it_is_rejected(srd):
    problems = problems_of(srd, "barbarian", extra_skill=Skill.NATURE)

    assert_problem(problems, "extra_skill", "Дварф")


# --- build_character: выборы класса ---


def test_rogue_expertise_doubles_chosen_proficiencies(srd):
    character = build(srd, "rogue")

    assert character.expertise == [Skill.ACROBATICS, Skill.STEALTH]


@pytest.mark.parametrize(
    "expertise",
    [[], [Skill.ACROBATICS], [Skill.ACROBATICS, Skill.STEALTH, Skill.INSIGHT]],
)
def test_rogue_expertise_needs_exactly_two_skills(srd, expertise):
    assert_problem(problems_of(srd, "rogue", expertise=expertise), "компетентность", "ровно 2")


def test_rogue_expertise_only_in_skills_with_proficiency(srd):
    problems = problems_of(srd, "rogue", expertise=[Skill.ACROBATICS, Skill.ARCANA])

    assert_problem(problems, "компетентность", names(Skill.ARCANA), "владений")


def test_rogue_expertise_cannot_repeat_a_skill(srd):
    problems = problems_of(srd, "rogue", expertise=[Skill.ACROBATICS, Skill.ACROBATICS])

    assert_problem(problems, "компетентность", "больше одного раза")


def test_expertise_for_class_without_it_is_rejected(srd):
    problems = problems_of(srd, "barbarian", expertise=[Skill.PERCEPTION, Skill.SURVIVAL])

    assert_problem(problems, "нет компетентности")


@pytest.mark.parametrize("class_id", ["fighter", "ranger"])
def test_fighting_style_is_required_for_fighter_and_ranger(srd, class_id):
    assert_problem(problems_of(srd, class_id, fighting_style=None), "боевой стиль", "обязателен")


@pytest.mark.parametrize("class_id", ["fighter", "ranger"])
def test_fighting_style_must_be_from_class_list(srd, class_id):
    assert_problem(problems_of(srd, class_id, fighting_style="two_weapon"), "two_weapon")


def test_fighting_style_for_class_without_styles_is_rejected(srd):
    assert_problem(problems_of(srd, "barbarian", fighting_style="defense"), "нет боевого стиля")


def test_fighting_style_is_saved_to_character(srd):
    assert build(srd, "fighter").fighting_style == "defense"
    assert build(srd, "ranger").fighting_style == "archery"
    assert build(srd, "barbarian").fighting_style is None


# --- build_character: заклинания ---


def test_spells_are_saved_to_character(srd):
    character = build(srd, "cleric")

    assert character.cantrips == ["sacred_flame", "guidance", "spare_the_dying"]
    assert character.prepared_spells == CHOICES["cleric"]["prepared_spells"]


def test_spell_counts_must_match_class_table(srd):
    problems = problems_of(
        srd,
        "cleric",
        cantrips=["sacred_flame", "guidance"],
        prepared_spells=CHOICES["cleric"]["prepared_spells"][:5],
    )

    assert_problem(problems, "заговоры", "ровно 3", "выбрано 2")
    assert_problem(problems, "подготовленные заклинания", "ровно 6", "выбрано 5")


def test_ranger_has_no_cantrips(srd):
    assert_problem(problems_of(srd, "ranger", cantrips=["guidance"]), "заговоры", "ровно 0")


def test_spell_from_another_class_list_is_rejected(srd):
    cantrips = ["fire_bolt", "guidance", "spare_the_dying"]  # «Огненный снаряд» — для волшебника

    problems = problems_of(srd, "cleric", cantrips=cantrips)

    assert_problem(problems, srd.spells["fire_bolt"].name, "недоступно")


def test_cantrip_among_prepared_spells_is_rejected(srd):
    prepared = ["guidance", *CHOICES["cleric"]["prepared_spells"][1:]]

    problems = problems_of(srd, "cleric", prepared_spells=prepared)

    assert_problem(problems, "подготовленные заклинания", srd.spells["guidance"].name, "недоступно")


def test_spell_above_available_slot_levels_is_rejected(srd):
    # В данных среза заклинаний 3-го уровня нет, поэтому добавляем одно в копию данных:
    # у жреца 3-го уровня ячейки только 1-го и 2-го уровней.
    third_level = srd.spells["aid"].model_copy(update={"id": "revivify", "level": 3})
    content = srd.model_copy(update={"spells": {**srd.spells, "revivify": third_level}})
    prepared = [*CHOICES["cleric"]["prepared_spells"][:5], "revivify"]

    problems = problems_of(content, "cleric", prepared_spells=prepared)

    assert_problem(problems, "подготовленные заклинания", third_level.name, "недоступно")


def test_leveled_spell_among_cantrips_is_rejected(srd):
    cantrips = ["bless", "guidance", "spare_the_dying"]

    assert_problem(problems_of(srd, "cleric", cantrips=cantrips), "заговоры", "недоступно")


def test_unknown_and_repeated_spells_are_rejected(srd):
    prepared = ["bless", "bless", "no_such_spell", "cure_wounds", "aid", "healing_word"]

    problems = problems_of(srd, "cleric", prepared_spells=prepared)

    assert_problem(problems, "неизвестное заклинание", "no_such_spell")
    assert_problem(problems, srd.spells["bless"].name, "больше одного раза")


def test_spells_for_class_without_spellcasting_are_rejected(srd):
    problems = problems_of(srd, "fighter", cantrips=["guidance"], prepared_spells=["bless"])

    assert_problem(problems, "нет заклинаний")


# --- build_character: числа ---


def test_dwarf_barbarian_with_constitution_14_has_35_hp(srd):
    character = build(srd, "barbarian")

    assert character.scores[CON] == 14
    # 12 (кость хитов) + 2 * 7 (среднее за 2-й и 3-й уровни) + 3 * 2 (Тел) + 3 (дварф).
    assert character.hp_max == character.hp == 35


def test_hp_for_non_dwarf_does_not_get_dwarven_toughness(srd):
    # Тот же варвар, но человек: 12 + 14 + 6 = 32.
    character = build(srd, "barbarian", species="human", extra_skill=Skill.NATURE)

    assert character.hp_max == 32


def test_hp_uses_negative_constitution_modifier(srd):
    scores = CHOICES["wizard"]["base_scores"] | {CON: 8}

    character = build(srd, "wizard", base_scores=scores, background_bonus={INT: 2, WIS: 1})

    assert character.scores[CON] == 8
    # 6 + 2 * 4 + 3 * (-1)
    assert character.hp_max == 11


def test_cleric_gets_slots_and_resources_for_level_3(srd):
    character = build(srd, "cleric")

    assert character.spell_slots_max == {1: 4, 2: 2}
    assert character.spell_slots == {1: 4, 2: 2}
    assert character.resources == {"channel_divinity": 2}
    assert character.hp_max == 27  # 8 + 2 * 5 + 3 * 2 + 3 (дварф)


@pytest.mark.parametrize(
    ("class_id", "slots", "resources"),
    [
        ("barbarian", {}, {"rage": 3}),
        ("fighter", {}, {"second_wind": 2, "action_surge": 1}),
        ("rogue", {}, {}),
        ("ranger", {1: 3}, {"hunters_mark": 2}),
        ("wizard", {1: 4, 2: 2}, {"arcane_recovery": 1}),
    ],
)
def test_slots_and_resources_follow_class_tables(srd, class_id, slots, resources):
    character = build(srd, class_id)

    assert character.spell_slots_max == slots
    assert character.spell_slots == slots
    assert character.resources == resources


# --- build_character: инвентарь ---


def equipped_ids(character: Character) -> list[str]:
    return [entry.item_id for entry in character.inventory if entry.equipped]


def test_inventory_combines_class_and_background_equipment(srd):
    character = build(srd, "barbarian")

    assert [(e.item_id, e.qty) for e in character.inventory] == [
        ("greataxe", 1),
        ("handaxe", 1),
        ("spear", 1),
        ("shortbow", 1),
        ("healers_kit", 1),
    ]


def test_same_item_from_class_and_background_becomes_one_stack(srd):
    rogue = build(srd, "rogue")
    wizard = build(srd, "wizard")

    assert {e.item_id: e.qty for e in rogue.inventory}["dagger"] == 2
    assert {e.item_id: e.qty for e in rogue.inventory}["thieves_tools"] == 2
    assert {e.item_id: e.qty for e in wizard.inventory}["quarterstaff"] == 2


@pytest.mark.parametrize(
    ("class_id", "expected"),
    [
        ("barbarian", ["greataxe"]),
        ("fighter", ["chain_mail", "greatsword"]),
        ("rogue", ["leather_armor", "shortsword"]),
        ("ranger", ["studded_leather_armor", "longbow"]),
        ("cleric", ["chain_shirt", "shield", "mace"]),
        ("wizard", ["quarterstaff"]),
    ],
)
def test_first_weapon_armor_and_shield_are_equipped(srd, class_id, expected):
    assert equipped_ids(build(srd, class_id)) == expected


def test_equipped_gear_gives_expected_armor_class(srd):
    # Кольчуга 16 + стиль «Оборона» 1; кольчужная рубаха 13 + Лов 8 (-1) + щит 2.
    assert armor_class(build(srd, "fighter"), srd) == 17
    assert armor_class(build(srd, "cleric"), srd) == 14


# --- build_character: все проблемы сразу ---


def test_all_problems_are_returned_as_a_list(srd):
    problems = problems_of(
        srd,
        "barbarian",
        base_scores={STR: 15, DEX: 15, CON: 15, INT: 9, WIS: 8, CHA: 8},  # 28 очков
        background_bonus={STR: 3},
        skills=[Skill.ATHLETICS],  # одного навыка мало, и он дублирует предысторию
        fighting_style="defense",
        cantrips=["guidance"],
    )

    assert_problem(problems, "покупка очков")
    assert_problem(problems, "бонус предыстории")
    assert_problem(problems, "ровно 2")
    assert_problem(problems, names(Skill.ATHLETICS), "Солдат")
    assert_problem(problems, "нет боевого стиля")
    assert_problem(problems, "нет заклинаний")
    assert len(problems) == 6


def test_unknown_ids_are_reported_together_with_independent_problems(srd):
    problems = problems_of(
        srd,
        "barbarian",
        species="gnoll",
        class_id="paladin",
        background="pirate",
        base_scores={STR: 18, DEX: 12, CON: 13, INT: 8, WIS: 10, CHA: 8},
    )

    assert_problem(problems, "вид", "gnoll")
    assert_problem(problems, "класс", "paladin")
    assert_problem(problems, "предыстория", "pirate")
    assert_problem(problems, ABILITY_NAMES_RU[STR], "18")


def test_validation_error_exposes_problems_and_readable_message(srd):
    with pytest.raises(CharacterValidationError) as info:
        build(srd, skills=[], background_bonus={})

    error = info.value
    assert isinstance(error, ValueError)
    assert isinstance(error.problems, list)
    assert len(error.problems) == 2
    for problem in error.problems:
        assert problem in str(error)
    # Сообщение — заголовок и по одной строке на каждую проблему.
    assert str(error).splitlines() == [
        "Выбор персонажа недопустим:",
        *(f"- {problem}" for problem in error.problems),
    ]


# --- quick_character ---


def quick(srd: SrdContent, class_id: str, species: str = "human", **kwargs: Any) -> Character:
    kwargs = {"name": "Мерлин", "concept": "отшельник-учёный", "character_id": "pc-9"} | kwargs
    return quick_character(class_id, species, srd, **kwargs)


@pytest.mark.parametrize("class_id", CLASS_IDS)
@pytest.mark.parametrize("species", SPECIES_IDS)
def test_quick_character_is_legal_for_every_class_and_species(srd, class_id, species):
    character = quick(srd, class_id, species)

    assert (character.class_id, character.species) == (class_id, species)
    assert len(set(character.skill_proficiencies)) == len(character.skill_proficiencies)


def test_quick_character_has_requested_name_concept_and_id(srd):
    character = quick(srd, "wizard", name="Эльминстер", concept="старый маг", character_id="x-1")

    assert (character.id, character.name) == ("x-1", "Эльминстер")
    assert character.persona.concept == "старый маг"


def test_quick_character_puts_standard_array_by_class_priority_and_bonus_on_top_two(srd):
    # У волшебника приоритет Инт, Тел: 15 и 14 + бонус «Мудреца» +2 и +1.
    wizard = quick(srd, "wizard")
    # У следопыта Мудрости нет в списке «Солдата»: бонус достаётся Лов (+2) и Тел (+1).
    ranger = quick(srd, "ranger")

    assert wizard.scores == {INT: 17, CON: 15, DEX: 13, WIS: 12, CHA: 10, STR: 8}
    assert ranger.scores == {DEX: 17, WIS: 14, CON: 14, STR: 12, INT: 10, CHA: 8}


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_quick_character_takes_default_background_of_class(srd, class_id):
    assert quick(srd, class_id).background == srd.classes[class_id].default_background


def test_quick_character_takes_default_skills_of_class(srd):
    character = quick(srd, "fighter", "dwarf")

    assert {Skill.ACROBATICS, Skill.PERCEPTION} <= set(character.skill_proficiencies)
    # «Солдат» + два навыка класса.
    assert len(character.skill_proficiencies) == 4


def test_quick_elf_replaces_default_perception_instead_of_duplicating_it(srd):
    character = quick(srd, "barbarian", "elf")  # у варвара по умолчанию Внимательность и Выживание

    assert character.skill_proficiencies.count(Skill.PERCEPTION) == 1
    assert Skill.SURVIVAL in character.skill_proficiencies
    assert len(character.skill_proficiencies) == 5  # «Солдат» 2 + эльф 1 + варвар 2


def test_quick_human_gets_extra_skill(srd):
    character = quick(srd, "wizard", "human")

    assert len(character.skill_proficiencies) == 5  # «Мудрец» 2 + класс 2 + человек 1


def test_quick_character_takes_first_fighting_style_and_first_two_expertise(srd):
    assert quick(srd, "fighter").fighting_style == "defense"
    assert quick(srd, "ranger").fighting_style == "archery"
    assert quick(srd, "barbarian").fighting_style is None

    rogue = quick(srd, "rogue", "human")
    assert rogue.expertise == [Skill.PERCEPTION, Skill.ACROBATICS]
    assert quick(srd, "wizard").expertise == []


def test_quick_character_splits_default_spells_into_cantrips_and_prepared(srd):
    cleric = quick(srd, "cleric")
    ranger = quick(srd, "ranger")

    assert cleric.cantrips == ["sacred_flame", "guidance", "spare_the_dying"]
    assert cleric.prepared_spells == [
        "bless",
        "cure_wounds",
        "healing_word",
        "guiding_bolt",
        "aid",
        "lesser_restoration",
    ]
    assert ranger.cantrips == []
    assert ranger.prepared_spells == list(srd.classes["ranger"].default_spells)
    assert quick(srd, "barbarian").prepared_spells == []


def test_quick_character_unknown_class_or_species_is_validation_error(srd):
    with pytest.raises(CharacterValidationError) as info:
        quick(srd, "paladin", "gnoll")

    assert_problem(info.value.problems, "класс", "paladin")
    assert_problem(info.value.problems, "вид", "gnoll")


def test_quick_character_does_not_bypass_validation(srd):
    # Поломанные данные класса: навыки быстрого режима не из списка класса.
    fighter = srd.classes["fighter"].model_copy(
        update={"default_skills": (Skill.ARCANA, Skill.HISTORY)}
    )
    broken = srd.model_copy(update={"classes": {**srd.classes, "fighter": fighter}})

    with pytest.raises(CharacterValidationError) as info:
        quick(broken, "fighter")

    assert_problem(info.value.problems, names(Skill.ARCANA), "не входит")


# --- random_character ---


@pytest.fixture
def submitted_choices(monkeypatch: pytest.MonkeyPatch) -> list[CharacterChoices]:
    """Выборы, которые random_character отправляет на проверку в build_character."""
    seen: list[CharacterChoices] = []
    real_build = creation.build_character

    def spy(choices: CharacterChoices, content: SrdContent, **kwargs: Any) -> Character:
        seen.append(choices)
        return real_build(choices, content, **kwargs)

    monkeypatch.setattr(creation, "build_character", spy)
    return seen


def test_random_character_is_always_legal_with_primary_ability_at_least_14(srd, submitted_choices):
    for seed in range(1, 51):
        character = random_character(
            SeededRng(seed), srd, taken_classes=set(), character_id=f"pc-{seed}"
        )

        choices = submitted_choices[-1]
        cls = srd.classes[choices.class_id]
        primary, secondary = cls.ability_priority[:2]
        assert choices.method == AbilityMethod.STANDARD_ARRAY, f"seed {seed}"
        assert sorted(choices.base_scores.values()) == sorted(STANDARD_ARRAY), f"seed {seed}"
        assert {choices.base_scores[primary], choices.base_scores[secondary]} == {15, 14}, (
            f"seed {seed}"
        )
        assert character.class_id == choices.class_id
        assert character.id == f"pc-{seed}"
    assert len(submitted_choices) == 50  # каждый персонаж прошёл build_character


def test_random_character_covers_all_classes_species_and_both_background_bonus_shapes(
    srd, submitted_choices
):
    characters = [
        random_character(SeededRng(seed), srd, taken_classes=set(), character_id="p")
        for seed in range(1, 201)
    ]

    assert {c.class_id for c in characters} == set(CLASS_IDS)
    assert {c.species for c in characters} == set(SPECIES_IDS)
    assert {c.background for c in characters} == {"acolyte", "criminal", "sage", "soldier"}
    primary_scores = {
        c.base_scores[srd.classes[c.class_id].ability_priority[0]] for c in submitted_choices
    }
    assert primary_scores == {15, 14}  # основная характеристика получает то 15, то 14
    bonus_shapes = {tuple(sorted(c.background_bonus.values())) for c in submitted_choices}
    assert bonus_shapes == {(1, 2), (1, 1, 1)}
    # Особые выборы появляются там, где они нужны: умелость человека, компетентность плута,
    # боевой стиль и заклинания.
    assert all(c.extra_skill for c in submitted_choices if c.species == "human")
    assert all(len(c.expertise) == 2 for c in submitted_choices if c.class_id == "rogue")
    assert all(c.fighting_style for c in submitted_choices if c.class_id in ("fighter", "ranger"))
    assert all(
        len(c.cantrips) == 3 for c in submitted_choices if c.class_id in ("cleric", "wizard")
    )
    assert all(len(c.prepared_spells) == 4 for c in submitted_choices if c.class_id == "ranger")


def test_random_character_has_placeholder_name_for_flavor_to_replace(srd):
    character = random_character(SeededRng(3), srd, taken_classes=set(), character_id="pc-1")

    assert character.name == "Безымянный странник"


def test_random_character_prefers_classes_not_taken_yet(srd):
    taken = set(CLASS_IDS) - {"rogue"}

    for seed in range(1, 51):
        character = random_character(SeededRng(seed), srd, taken_classes=taken, character_id="p")

        assert character.class_id == "rogue", f"seed {seed}"


def test_random_character_may_repeat_class_when_all_are_taken(srd):
    classes = {
        random_character(
            SeededRng(seed), srd, taken_classes=set(CLASS_IDS), character_id="p"
        ).class_id
        for seed in range(1, 51)
    }

    assert len(classes) > 1
    assert classes <= set(CLASS_IDS)


def test_random_character_varies_class_and_species_with_rng(srd):
    characters = [
        random_character(SeededRng(seed), srd, taken_classes=set(), character_id="p")
        for seed in range(1, 51)
    ]

    assert len({c.class_id for c in characters}) > 1
    assert len({c.species for c in characters}) > 1


def test_random_character_is_reproducible_for_the_same_seed(srd):
    first = random_character(SeededRng(7), srd, taken_classes=set(), character_id="p")
    second = random_character(SeededRng(7), srd, taken_classes=set(), character_id="p")

    assert first == second


def test_random_character_takes_its_choices_from_the_given_rng(srd):
    # Пустой ScriptedRng отказывает на первом же запросе: выбор идёт именно через него.
    with pytest.raises(ValueError, match="закончилась"):
        random_character(ScriptedRng([]), srd, taken_classes=set(), character_id="p")


# --- границы правил, которых не хватало ---


def test_point_buy_range_error_names_both_borders(srd):
    scores = CHOICES["barbarian"]["base_scores"] | {INT: 16}

    assert_problem(problems_of(srd, base_scores=scores), "от 8 до 15")


@pytest.mark.parametrize(("top", "legal"), [(18, True), (21, False)])
def test_score_cap_applies_to_the_final_value_even_without_background_bonus(srd, top, legal):
    # Бонус предыстории ложится на Лов и Тел, а выпавшее значение Силы остаётся как есть.
    # Больше 18 при броске 4d6kh3 выпасть не может, поэтому легальна только граница 18.
    rolled = [top, 14, 13, 12, 10, 8]
    scores = dict(zip(Ability, rolled, strict=True))
    overrides = {
        "method": AbilityMethod.ROLL,
        "base_scores": scores,
        "background_bonus": {DEX: 2, CON: 1},
        "rolled_set": rolled,
    }

    if legal:
        assert build(srd, **overrides).scores[STR] == top
    else:
        assert_problem(problems_of(srd, **overrides), ABILITY_NAMES_RU[STR], "21", "20")


@pytest.mark.parametrize(
    ("field", "value", "word"),
    [
        ("species", "gnoll", "вид"),
        ("class_id", "paladin", "класс"),
        ("background", "pirate", "предыстория"),
    ],
)
def test_a_single_unknown_id_is_a_problem_and_does_not_break_other_checks(srd, field, value, word):
    problems = problems_of(srd, **{field: value})

    assert_problem(problems, word, value)


def srd_with_expertise_at_level(srd: SrdContent, level: int) -> SrdContent:
    """Копия данных, где компетентность плута открывается на заданном уровне."""
    rogue = srd.classes["rogue"]
    features = tuple(
        f.model_copy(update={"level": level}) if f.id == "expertise" else f for f in rogue.features
    )
    changed = rogue.model_copy(update={"features": features})
    return srd.model_copy(update={"classes": {**srd.classes, "rogue": changed}})


def test_class_feature_gained_exactly_at_level_3_is_available(srd):
    character = build(srd_with_expertise_at_level(srd, 3), "rogue")

    assert len(character.expertise) == 2


def test_class_feature_gained_after_level_3_is_not_available(srd):
    problems = problems_of(srd_with_expertise_at_level(srd, 4), "rogue")

    assert_problem(problems, "нет компетентности")


# --- вход не меняется, персонаж ничего не делит с выбором ---


def test_build_character_leaves_choices_and_rolled_set_untouched(srd):
    rolled = [16, 15, 13, 12, 11, 9]
    choices = make_choices(
        "barbarian",
        method=AbilityMethod.ROLL,
        base_scores=dict(zip(Ability, reversed(rolled), strict=True)),
        persona=Persona(concept="бывший стражник"),
    )
    choices_before = choices.model_copy(deep=True)

    build_character(choices, srd, rolled_set=rolled, character_id="pc-1")

    assert choices == choices_before
    assert rolled == [16, 15, 13, 12, 11, 9]


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_character_lists_and_dicts_are_not_shared_with_choices(srd, class_id):
    choices = make_choices(class_id)
    choices_before = choices.model_copy(deep=True)

    character = build_character(choices, srd, character_id="pc-1")
    for field in ("skill_proficiencies", "expertise", "cantrips", "prepared_spells"):
        getattr(character, field).clear()
    character.scores.clear()
    character.inventory.clear()

    assert choices == choices_before


def test_quick_characters_of_the_same_class_do_not_share_inventory_or_spells(srd):
    first = quick(srd, "cleric")
    second = quick(srd, "cleric")

    first.inventory[0].qty = 99
    first.inventory[0].equipped = False
    first.prepared_spells.append("wish")
    first.cantrips.clear()

    assert second.inventory[0].qty == 1
    assert second.inventory[0].equipped is True
    assert "wish" not in second.prepared_spells
    assert len(second.cantrips) == 3


def test_random_character_does_not_change_taken_classes_and_ignores_unknown_ones(srd):
    taken = {"paladin", "rogue"}

    with_unknown = random_character(SeededRng(5), srd, taken_classes=taken, character_id="p")
    plain = random_character(SeededRng(5), srd, taken_classes={"rogue"}, character_id="p")

    assert taken == {"paladin", "rogue"}
    assert with_unknown == plain


# --- стык броска характеристик и создания ---


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_every_rolled_set_builds_a_character_when_laid_out_by_class_priority(srd, class_id):
    priority = srd.classes[class_id].ability_priority
    bonus = CHOICES[class_id]["background_bonus"]

    for seed in range(1, 41):
        rolled = roll_ability_set(SeededRng(seed))
        by_priority = dict(zip(priority, sorted(rolled, reverse=True), strict=True))

        character = build(
            srd, class_id, method=AbilityMethod.ROLL, base_scores=by_priority, rolled_set=rolled
        )

        assert sorted(character.scores[a] - bonus.get(a, 0) for a in Ability) == sorted(rolled)
        assert all(3 <= score <= 20 for score in character.scores.values()), f"seed {seed}"


# --- устойчивость к любым выборам ---


HOSTILE_CHOICES = [
    ("species", ""),
    ("species", "gnoll"),
    ("class_id", ""),
    ("class_id", "paladin"),
    ("background", ""),
    ("background", "pirate"),
    ("base_scores", {}),
    ("base_scores", {STR: 15, DEX: 14}),
    ("base_scores", dict.fromkeys(Ability, 7)),
    ("base_scores", dict.fromkeys(Ability, 15)),
    ("base_scores", dict.fromkeys(Ability, 16)),
    ("base_scores", dict.fromkeys(Ability, 0)),
    ("base_scores", dict.fromkeys(Ability, -5)),
    ("base_scores", dict.fromkeys(Ability, 10**9)),
    ("background_bonus", {}),
    ("background_bonus", {STR: 3}),
    ("background_bonus", {STR: -1, DEX: 1}),
    ("background_bonus", {STR: 2, DEX: 2}),
    ("background_bonus", dict.fromkeys(Ability, 1)),
    ("skills", []),
    ("skills", list(Skill)),
    ("skills", [Skill.ARCANA, Skill.ARCANA]),
    ("expertise", list(Skill)),
    ("expertise", [Skill.ATHLETICS, Skill.ATHLETICS]),
    ("fighting_style", ""),
    ("fighting_style", "no_such_style"),
    ("cantrips", ["no_such_spell"]),
    ("cantrips", ["fire_bolt"] * 3),
    ("prepared_spells", ["no_such_spell"]),
    ("prepared_spells", ["magic_missile"] * 6),
]


@pytest.mark.parametrize("class_id", CLASS_IDS)
@pytest.mark.parametrize(
    ("field", "value"),
    HOSTILE_CHOICES,
    ids=[f"{f}-{i}" for i, (f, _) in enumerate(HOSTILE_CHOICES)],
)
def test_hostile_choice_is_rejected_with_a_list_of_readable_problems(srd, class_id, field, value):
    problems = problems_of(srd, class_id, **{field: value})

    assert problems
    assert all(isinstance(p, str) and p.strip() for p in problems)


def test_any_species_background_class_combination_is_built_or_rejected_with_problems(srd):
    built = rejected = 0

    for class_id in CLASS_IDS:
        for species in SPECIES_IDS:
            for background in srd.backgrounds:
                try:
                    character = build(srd, class_id, species=species, background=background)
                except CharacterValidationError as error:
                    assert error.problems
                    rejected += 1
                else:
                    assert (character.species, character.background) == (species, background)
                    built += 1

    # Выбор подогнан под одну связку, поэтому часть сочетаний нелегальна, а часть — легальна.
    assert built > 0
    assert rejected > 0
    assert built + rejected == len(CLASS_IDS) * len(SPECIES_IDS) * len(srd.backgrounds)


# --- случайный режим раздаёт набор поровну ---


def test_random_human_rogue_may_take_expertise_in_the_skill_given_by_skillful(
    srd, submitted_choices
):
    only_rogue = set(CLASS_IDS) - {"rogue"}
    for seed in range(1, 401):
        random_character(SeededRng(seed), srd, taken_classes=only_rogue, character_id="p")

    human_rogues = [c for c in submitted_choices if c.species == "human"]

    assert len(human_rogues) > 10
    assert any(c.extra_skill in c.expertise for c in human_rogues)
    # Остальные выборы человека-плута всё равно остаются в его владениях.
    assert all(len(c.expertise) == 2 for c in human_rogues)


def test_random_character_deals_the_standard_array_evenly(srd, submitted_choices):
    total = 2000
    for seed in range(1, total + 1):
        random_character(SeededRng(seed), srd, taken_classes=set(), character_id="p")

    counts: Counter[tuple[int, int]] = Counter()
    for choices in submitted_choices:
        priority = srd.classes[choices.class_id].ability_priority
        for rank, ability in enumerate(priority):
            counts[(rank, choices.base_scores[ability])] += 1

    # Две главные характеристики делят 15 и 14, остальные четыре — 13, 12, 10 и 8 в любом порядке.
    expected = {rank: (15, 14) for rank in (0, 1)} | {rank: (13, 12, 10, 8) for rank in range(2, 6)}
    for rank, values in expected.items():
        for value in values:
            share = counts[(rank, value)] / total
            assert abs(share - 1 / len(values)) < 0.06, (rank, value, share)


# --- выбор приходит от клиента обычным JSON ---


RAW_CHOICES = {
    "name": "Брунгильда",
    "species": "dwarf",
    "class_id": "barbarian",
    "background": "soldier",
    "method": "point_buy",
    "base_scores": {"str": 15, "dex": 12, "con": 13, "int": 8, "wis": 10, "cha": 8},
    "background_bonus": {"str": 2, "con": 1},
    "skills": ["perception", "survival"],
}


def test_plain_json_choices_from_a_client_build_the_same_character(srd):
    from_json = CharacterChoices.model_validate_json(json.dumps(RAW_CHOICES))

    character = build_character(from_json, srd, character_id="pc-1")

    assert character == build(srd, "barbarian", name="Брунгильда")
    assert character.scores[STR] == 17
    assert character.skill_proficiencies == [
        Skill.ATHLETICS,
        Skill.INTIMIDATION,
        Skill.PERCEPTION,
        Skill.SURVIVAL,
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "dice"),
        ("base_scores", {"strength": 15}),
        ("background_bonus", {"luck": 1}),
        ("skills", ["flying"]),
        ("extra_skill", "flying"),
    ],
)
def test_unknown_enum_values_never_reach_build_character(field, value):
    with pytest.raises(ValidationError):
        CharacterChoices.model_validate(RAW_CHOICES | {field: value})


def test_ability_method_values_are_the_strings_a_client_sends():
    assert [method.value for method in AbilityMethod] == ["point_buy", "standard_array", "roll"]
