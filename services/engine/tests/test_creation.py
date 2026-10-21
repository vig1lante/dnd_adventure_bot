"""Тесты создания персонажа: способы задать характеристики, `build_character`,
`quick_character` и `random_character`.

Базовые выборы по классам (`CHOICES`) легальны сами по себе; тесты ломают в них ровно то
правило, которое проверяют. Случайность — только через `SeededRng` и `ScriptedRng`.
"""

import json
import re
from collections import Counter
from collections.abc import Sequence
from itertools import combinations_with_replacement
from typing import Any

import pytest
from pydantic import ValidationError

from dnd_engine import creation
from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_NAMES_RU, Ability, Skill
from dnd_engine.character import Character, Persona, armor_class
from dnd_engine.content import SpellDef, SrdContent, load_srd
from dnd_engine.creation import (
    FIGHTING_STYLES,
    POINT_BUY_BUDGET,
    POINT_BUY_COST,
    STANDARD_ARRAY,
    AbilityMethod,
    CharacterChoices,
    CharacterValidationError,
    build_character,
    class_spell_options,
    quick_character,
    random_character,
    random_point_buy,
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


def build(srd: SrdContent, base: str = "barbarian", **overrides: Any) -> Character:
    choices = make_choices(base, **overrides)
    return build_character(choices, srd, character_id="pc-1")


def problems_of(srd: SrdContent, base: str = "barbarian", **overrides: Any) -> list[str]:
    """Список проблем, с которыми `build_character` отклоняет выбор."""
    with pytest.raises(CharacterValidationError) as info:
        build(srd, base, **overrides)
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


# --- random_point_buy: «Распределить случайно» (решение владельца R9) ---

KEY_ABILITIES = 2  # столько первых характеристик приоритета класса считаются ключевыми


def spent_points(scores: dict[Ability, int]) -> int:
    return sum(POINT_BUY_COST[value] for value in scores.values())


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_random_point_buy_is_always_a_legal_layout_of_exactly_27_points(srd, class_id):
    for seed in range(1, 201):
        scores = random_point_buy(SeededRng(seed), srd, class_id)

        assert set(scores) == set(Ability), f"seed {seed}"
        assert all(8 <= value <= 15 for value in scores.values()), f"seed {seed}: {scores}"
        assert spent_points(scores) == POINT_BUY_BUDGET, f"seed {seed}: {scores}"


def legal_layouts() -> list[tuple[int, ...]]:
    """Все способы потратить ровно 27 очков на шесть значений 8–15 (по убыванию).

    Считаем независимо от движка, перебором всех сочетаний, а не его функциями."""
    return [
        tuple(sorted(values, reverse=True))
        for values in combinations_with_replacement(range(8, 16), 6)
        if sum(POINT_BUY_COST[value] for value in values) == POINT_BUY_BUDGET
    ]


def test_there_are_62_legal_layouts_with_a_main_ability_of_14_or_more():
    # Набор раскладок — решение владельца R9: все способы потратить ровно 27 очков, где лучшее
    # значение не ниже 14. Исключены только три «ровные» раскладки, где оно не выше 13.
    every = legal_layouts()
    strong = [layout for layout in every if layout[0] >= 14]

    assert len(every) == 65 and len(strong) == 62
    assert set(every) - set(strong) == {
        (13, 13, 13, 12, 12, 12),
        (13, 13, 13, 13, 12, 11),
        (13, 13, 13, 13, 13, 10),
    }


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_random_point_buy_draws_from_exactly_the_legal_layouts_with_a_strong_main_ability(
    srd, class_id
):
    # Только поведение: на тысячах seed функция выдаёт все 62 раскладки и ничего кроме них. Так
    # тест заметит и сужение выбора, и появление лишней раскладки, не зная внутреннего устройства.
    strong = {layout for layout in legal_layouts() if layout[0] >= 14}

    drawn = {
        tuple(sorted(random_point_buy(SeededRng(seed), srd, class_id).values(), reverse=True))
        for seed in range(1, 3001)
    }

    assert drawn == strong


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_build_character_accepts_every_random_point_buy(srd, class_id):
    bonus = CHOICES[class_id]["background_bonus"]

    for seed in range(1, 101):
        scores = random_point_buy(SeededRng(seed), srd, class_id)

        character = build(srd, class_id, method=AbilityMethod.POINT_BUY, base_scores=scores)

        assert character.scores == {a: scores[a] + bonus.get(a, 0) for a in Ability}, f"seed {seed}"


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_random_point_buy_gives_the_primary_ability_14_or_15_and_the_most_of_all(srd, class_id):
    primary = srd.classes[class_id].ability_priority[0]

    results = [random_point_buy(SeededRng(seed), srd, class_id) for seed in range(1, 201)]

    assert all(scores[primary] == max(scores.values()) for scores in results)
    assert {scores[primary] for scores in results} == {14, 15}  # обе границы встречаются


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_random_point_buy_gives_the_key_abilities_more_than_the_rest(srd, class_id):
    priority = srd.classes[class_id].ability_priority
    key, rest = priority[:KEY_ABILITIES], priority[KEY_ABILITIES:]

    for seed in range(1, 201):
        scores = random_point_buy(SeededRng(seed), srd, class_id)

        assert scores[key[0]] >= scores[key[1]] >= max(scores[a] for a in rest), f"seed {seed}"


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_random_point_buy_deals_the_remaining_values_to_non_key_abilities_in_random_order(
    srd, class_id
):
    # Ловит мутант «перемешать, затем отсортировать по приоритету класса»: он тратит те же
    # обращения к генератору и даёт те же наборы значений, но первая неключевая характеристика
    # у него всегда лучшая из четырёх, а последняя всегда худшая. Место значения определено
    # только когда все четыре различны, поэтому равные значения пропускаем.
    rest = srd.classes[class_id].ability_priority[KEY_ABILITIES:]
    places: dict[Ability, set[int]] = {ability: set() for ability in rest}

    for seed in range(1, 401):
        scores = random_point_buy(SeededRng(seed), srd, class_id)
        values = [scores[ability] for ability in rest]
        if len(set(values)) < len(values):
            continue
        best_first = sorted(values, reverse=True)
        for ability in rest:
            places[ability].add(best_first.index(scores[ability]))

    # Каждая неключевая характеристика на разных seed получает любое место: от лучшего (0)
    # из оставшихся значений до худшего.
    assert places == {ability: set(range(len(rest))) for ability in rest}


def test_random_point_buy_follows_the_priority_of_the_class(srd):
    # У волшебника главная — Интеллект, у варвара — Сила: тот же бросок кладёт максимум по-разному.
    wizard = random_point_buy(SeededRng(5), srd, "wizard")
    barbarian = random_point_buy(SeededRng(5), srd, "barbarian")

    assert wizard[INT] == max(wizard.values())
    assert barbarian[STR] == max(barbarian.values())
    assert sorted(wizard.values()) == sorted(barbarian.values())  # набор тот же, места разные


def test_random_point_buy_differs_between_seeds(srd):
    layouts = {
        tuple(random_point_buy(SeededRng(seed), srd, "wizard").items()) for seed in range(1, 51)
    }
    value_sets = {
        tuple(sorted(random_point_buy(SeededRng(seed), srd, "wizard").values()))
        for seed in range(1, 51)
    }

    assert len(layouts) > 30
    assert len(value_sets) > 10  # меняются не только места, но и сами значения


def test_random_point_buy_is_reproducible_for_the_same_seed(srd):
    first = random_point_buy(SeededRng(9), srd, "rogue")
    second = random_point_buy(SeededRng(9), srd, "rogue")

    assert first == second


def test_random_point_buy_takes_its_choices_from_the_given_rng(srd):
    # Пустой ScriptedRng отказывает на первом же запросе: раскладка идёт именно через него.
    with pytest.raises(ValueError, match="закончилась"):
        random_point_buy(ScriptedRng([]), srd, "wizard")


def test_random_point_buy_with_an_unknown_class_is_a_validation_error(srd):
    with pytest.raises(CharacterValidationError) as info:
        random_point_buy(SeededRng(1), srd, "paladin")

    assert_problem(info.value.problems, "класс", "paladin")


def test_random_point_buy_does_not_change_the_shared_srd_data(srd):
    before = srd.model_dump(mode="json")

    random_point_buy(SeededRng(3), srd, "cleric")

    assert srd.model_dump(mode="json") == before


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


@pytest.mark.parametrize(
    "spent",
    [
        {STR: 8, DEX: 8, CON: 8, INT: 8, WIS: 8, CHA: 8},  # 0 очков
        {STR: 15, DEX: 8, CON: 8, INT: 8, WIS: 8, CHA: 8},  # 9 очков
        {STR: 15, DEX: 15, CON: 14, INT: 8, WIS: 8, CHA: 8},  # 25 очков: два остались
    ],
)
def test_point_buy_may_leave_points_unspent(srd, spent):
    # «Не больше 27»: игрок, который не потратил всё, остаётся в правилах. Лишнего получить
    # нельзя ни одним способом: потолок задаёт стоимость значений, а не обязанность тратить.
    assert sum(POINT_BUY_COST[value] for value in spent.values()) < 27

    character = build(srd, base_scores=spent)

    assert character.scores[STR] >= spent[STR]


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


# --- build_character: бросок характеристик убран (решение владельца R9) ---


@pytest.mark.parametrize(
    "scores",
    [
        dict(zip(Ability, (15, 14, 13, 12, 10, 8), strict=True)),  # набор, как у стандартного
        dict(zip(Ability, (18, 18, 18, 19, 19, 19), strict=True)),  # недостижимые значения
        # С прибавкой предыстории (+2 к Силе, +1 к Телосложению) Сила выходит за потолок 20.
        dict(zip(Ability, (19, 19, 19, 19, 19, 19), strict=True)),
        {STR: 15},  # не заданы остальные характеристики
    ],
    ids=["legal-looking", "impossible", "above-the-cap", "incomplete"],
)
def test_the_roll_method_is_refused_with_a_clear_russian_problem(srd, scores):
    problems = problems_of(srd, method=AbilityMethod.ROLL, base_scores=scores)

    # Причина одна и понятная: не «не заданы характеристики», не «нет выпавшего набора» и не
    # «итоговое значение больше 20» — значения отвергнутого способа не разбираются вовсе.
    assert problems == [creation.ROLL_REMOVED_PROBLEM]
    assert_problem(problems, "бросок", "27 очков")


def test_the_roll_method_is_reported_together_with_other_problems(srd):
    scores = dict(zip(Ability, (15, 14, 13, 12, 10, 8), strict=True))

    problems = problems_of(srd, method=AbilityMethod.ROLL, base_scores=scores, skills=[])

    assert_problem(problems, "бросок")
    assert_problem(problems, "ровно 2")


def test_build_character_no_longer_takes_a_rolled_set(srd):
    choices = make_choices("barbarian")

    with pytest.raises(TypeError):
        build_character(choices, srd, rolled_set=[15, 14, 13, 12, 10, 8], character_id="pc-1")


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
    # Покупка очков такого значения не даёт (проблема диапазона тоже названа), но предел 20 —
    # правило SRD, и оно проверяется само, а не как следствие других проверок.
    scores = dict(zip(Ability, [19, 14, 13, 12, 10, 8], strict=True))

    problems = problems_of(srd, base_scores=scores, background_bonus={STR: 2, DEX: 1})

    assert_problem(problems, ABILITY_NAMES_RU[STR], "21", "20")


def test_a_final_score_of_17_is_reachable_with_a_base_of_15_and_a_plus_2_bonus(srd):
    scores = CHOICES["barbarian"]["base_scores"]
    assert max(scores.values()) == 15

    character = build(srd, base_scores=scores, background_bonus={STR: 2, CON: 1})

    assert max(character.scores.values()) == 17


def test_a_base_of_16_cannot_be_bought_to_reach_18(srd):
    # Выше 17 не подняться: больше 15 покупкой очков не получить, а прибавка предыстории — не
    # больше +2 (ей посвящены тесты выше).
    scores = CHOICES["barbarian"]["base_scores"] | {STR: 16}

    problems = problems_of(srd, base_scores=scores, background_bonus={STR: 2, CON: 1})

    assert_problem(problems, ABILITY_NAMES_RU[STR], "16", "вне диапазона")


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


# --- варианты заклинаний класса: одно место и для проверки выбора, и для формы ---

CASTER_IDS = ("ranger", "cleric", "wizard")


def picks(pool: Sequence[SpellDef], wanted: int, first: str | None = None) -> list[str]:
    """`wanted` разных заклинаний из пула; `first`, если задан, стоит первым."""
    rest = [spell.id for spell in pool if spell.id != first]
    return ([first] if first else []) + rest[: wanted - (1 if first else 0)]


@pytest.mark.parametrize("class_id", CASTER_IDS)
def test_every_spell_option_of_a_class_is_accepted_by_the_character_check(srd, class_id):
    spellcasting = srd.classes[class_id].spellcasting
    options = class_spell_options(spellcasting, srd)
    cantrips_wanted = spellcasting.cantrips_known[3]
    prepared_wanted = spellcasting.prepared[3]

    # Вариантов хватает, чтобы выбрать нужное число.
    assert len(options.cantrips) >= cantrips_wanted
    assert len(options.spells) >= prepared_wanted
    if cantrips_wanted:
        for cantrip in options.cantrips:
            build(srd, class_id, cantrips=picks(options.cantrips, cantrips_wanted, cantrip.id))
    for spell in options.spells:
        build(srd, class_id, prepared_spells=picks(options.spells, prepared_wanted, spell.id))


@pytest.mark.parametrize("class_id", CASTER_IDS)
def test_a_spell_outside_the_class_options_is_rejected_by_the_character_check(srd, class_id):
    spellcasting = srd.classes[class_id].spellcasting
    options = class_spell_options(spellcasting, srd)
    offered = {spell.id for spell in (*options.cantrips, *options.spells)}
    outsiders = [spell for spell in srd.spells.values() if spell.id not in offered]
    assert outsiders  # у каждого класса среза есть чужие заклинания

    for spell in outsiders:
        if spellcasting.cantrips_known[3]:
            cantrips = picks(options.cantrips, spellcasting.cantrips_known[3] - 1) + [spell.id]
            assert_problem(problems_of(srd, class_id, cantrips=cantrips), "недоступно")
        prepared = picks(options.spells, spellcasting.prepared[3] - 1) + [spell.id]
        assert_problem(problems_of(srd, class_id, prepared_spells=prepared), "недоступно")


@pytest.mark.parametrize("class_id", CASTER_IDS)
def test_spell_options_are_the_class_list_in_data_order_split_by_level(srd, class_id):
    spellcasting = srd.classes[class_id].spellcasting
    top_level = max(spellcasting.slots[3])

    options = class_spell_options(spellcasting, srd)

    in_list = [s for s in srd.spells.values() if spellcasting.spell_list in s.classes]
    assert options.cantrips == tuple(s for s in in_list if s.level == 0)
    assert options.spells == tuple(s for s in in_list if 1 <= s.level <= top_level)


def test_spell_options_stop_at_the_highest_slot_level_of_the_character(srd):
    # В данных среза заклинаний 3-го уровня нет: добавляем одно в копию данных. У жреца 3-го
    # уровня ячейки только 1-го и 2-го уровней, поэтому заклинание 3-го уровня не предлагается.
    third_level = srd.spells["aid"].model_copy(update={"id": "revivify", "level": 3})
    content = srd.model_copy(update={"spells": {**srd.spells, "revivify": third_level}})

    options = class_spell_options(content.classes["cleric"].spellcasting, content)

    assert "revivify" not in {spell.id for spell in options.spells}
    assert {spell.level for spell in options.spells} == {1, 2}


# --- боевые стили: русские названия и суть живут в движке ---


def test_every_fighting_style_of_every_class_has_a_russian_name_and_summary(srd):
    used = {style for cls in srd.classes.values() for style in cls.fighting_styles}

    assert used  # в данных среза боевые стили есть
    assert set(FIGHTING_STYLES) == used  # у каждого стиля есть запись, лишних записей нет
    for style_id, style in FIGHTING_STYLES.items():
        assert re.search("[а-яё]", style.name, re.IGNORECASE), style_id
        assert style.summary.strip(), style_id


def test_fighting_style_names_stay_what_the_players_know():
    assert FIGHTING_STYLES["dueling"].name == "Дуэлянт"
    assert FIGHTING_STYLES["archery"].name == "Стрельба"
    assert FIGHTING_STYLES["defense"].name == "Оборона"


def test_fighting_style_table_is_read_only():
    with pytest.raises(TypeError):
        FIGHTING_STYLES["dueling"] = ("Другое", "Другое")  # type: ignore[index]


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


def test_score_cap_applies_to_the_final_value_even_without_background_bonus(srd):
    # Бонус предыстории ложится на Лов и Тел, а базовое значение Силы 21 остаётся как есть.
    scores = dict(zip(Ability, [21, 14, 13, 12, 10, 8], strict=True))

    problems = problems_of(srd, base_scores=scores, background_bonus={DEX: 2, CON: 1})

    assert_problem(problems, ABILITY_NAMES_RU[STR], "21", "20")


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


def test_build_character_leaves_choices_untouched(srd):
    choices = make_choices(
        "barbarian",
        method=AbilityMethod.STANDARD_ARRAY,
        base_scores=dict(zip(Ability, reversed(STANDARD_ARRAY), strict=True)),
        persona=Persona(concept="бывший стражник"),
    )
    choices_before = choices.model_copy(deep=True)

    build_character(choices, srd, character_id="pc-1")

    assert choices == choices_before


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
    # «roll» остался в перечне только затем, чтобы старый клиент получил русскую проблему от
    # `build_character`, а не безликую ошибку разбора; создавать героя этим способом нельзя.
    assert [method.value for method in AbilityMethod] == ["point_buy", "standard_array", "roll"]
