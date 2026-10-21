"""Бюджет боёв и валидатор приключения (спека §6.2, план B4.3).

Приключения собираются в памяти фабрикой `adventure_factory`: чистое приключение не даёт ни
ошибок, ни предупреждений, а каждый тест вносит в него одну правку и ждёт один код.
"""

import time
from collections.abc import Callable
from typing import Any, get_args

import pytest
from adventure_factory import adventure_data, custom_monster_data, make_adventure

from dnd_engine.adventure.budget import (
    XP_BUDGET_PER_CHARACTER,
    encounter_rating,
    encounter_xp,
)
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import (
    DAMAGE_TYPES,
    MAX_MESSAGE_LENGTH,
    MAX_REF_LENGTH,
    Issue,
    Report,
    validate,
)
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.tools.narrative import DamageType

FieldPath = tuple[str | int, ...]


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def with_value(path: FieldPath, value: Any) -> Adventure:
    """Чистое приключение с одним изменённым значением по пути из ключей и индексов."""
    data = adventure_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return Adventure.model_validate(data)


# --- бюджет XP ---


def test_xp_budget_table_is_the_2024_one_for_levels_1_to_5():
    assert dict(XP_BUDGET_PER_CHARACTER) == {
        1: (50, 75, 100),
        2: (100, 150, 200),
        3: (150, 225, 400),
        4: (250, 375, 500),
        5: (500, 750, 1100),
    }


def test_xp_budget_table_is_read_only():
    with pytest.raises(TypeError):
        XP_BUDGET_PER_CHARACTER[6] = (1, 2, 3)  # type: ignore[index]


def test_encounter_xp_multiplies_srd_monster_xp_by_count(srd):
    adventure = make_adventure()

    xp = encounter_xp(adventure.encounters["bones"], adventure, srd)

    assert xp == 8 * srd.monsters["ghoul"].xp


def test_encounter_xp_adds_up_several_groups(srd):
    groups = [
        {"monster_id": "skeleton", "count": 3, "zone": "here"},
        {"monster_id": "zombie", "count": 2, "zone": "here"},
    ]
    adventure = with_value(("encounters", "bones", "monsters"), groups)

    xp = encounter_xp(adventure.encounters["bones"], adventure, srd)

    assert xp == 3 * srd.monsters["skeleton"].xp + 2 * srd.monsters["zombie"].xp


def test_encounter_xp_takes_custom_statblocks_from_the_adventure(srd):
    data = adventure_data()
    data["monsters"] = {"crypt-rat": custom_monster_data(xp=70)}
    data["encounters"]["bones"]["monsters"] = [{"monster_id": "crypt-rat", "count": 4}]
    adventure = Adventure.model_validate(data)

    assert encounter_xp(adventure.encounters["bones"], adventure, srd) == 280


def test_a_custom_statblock_shadows_an_srd_monster_with_the_same_id(srd):
    data = adventure_data()
    data["monsters"] = {"ghoul": custom_monster_data(id="ghoul", xp=10)}
    adventure = Adventure.model_validate(data)

    assert encounter_xp(adventure.encounters["bones"], adventure, srd) == 80


def test_encounter_xp_refuses_a_monster_that_exists_nowhere(srd):
    adventure = with_value(("encounters", "bones", "monsters"), [{"monster_id": "dragon-x"}])

    with pytest.raises(ValueError, match="dragon-x"):
        encounter_xp(adventure.encounters["bones"], adventure, srd)


# --- рейтинг боя: 6 героев 3-го уровня, бюджет 900 / 1350 / 2400, «смертельный» — свыше 3600 ---


@pytest.mark.parametrize(
    ("xp", "rating"),
    [
        (0, "trivial"),
        (899, "trivial"),
        (900, "low"),
        (1349, "low"),
        (1350, "moderate"),
        (2399, "moderate"),
        (2400, "high"),
        (3600, "high"),
        (3601, "deadly"),
    ],
)
def test_encounter_rating_at_the_budget_boundaries(xp, rating):
    assert encounter_rating(xp, level=3, party_size=6) == rating


@pytest.mark.parametrize(
    ("level", "party_size", "xp", "rating"),
    [
        (1, 1, 49, "trivial"),
        (1, 1, 50, "low"),
        (1, 1, 150, "high"),
        (1, 1, 151, "deadly"),
        (5, 4, 1999, "trivial"),
        (5, 4, 2000, "low"),
        (5, 4, 6600, "high"),
        (5, 4, 6601, "deadly"),
    ],
)
def test_encounter_rating_scales_the_budget_by_level_and_party_size(level, party_size, xp, rating):
    assert encounter_rating(xp, level=level, party_size=party_size) == rating


@pytest.mark.parametrize("level", [0, 6, -1])
def test_encounter_rating_refuses_a_level_outside_the_budget_table(level):
    with pytest.raises(ValueError, match="уров"):
        encounter_rating(100, level=level, party_size=6)


# --- общие помощники валидатора ---


def codes(report: Report) -> list[str]:
    return sorted(issue.code for issue in report.issues)


def only_issue(report: Report) -> Issue:
    """Единственная проблема отчёта: правка теста не должна тянуть за собой другие."""
    assert len(report.issues) == 1, report.issues
    return report.issues[0]


def adventure_with_rat(**overrides: Any) -> Adventure:
    """Чистое приключение с кастомным статблоком `crypt-rat` (в бою он не участвует)."""
    data = adventure_data()
    data["monsters"] = {"crypt-rat": custom_monster_data(**overrides)}
    return Adventure.model_validate(data)


def rat_actions(index: int, **changes: Any) -> list[dict[str, Any]]:
    """Действия крысы, где у действия с номером `index` изменены поля `changes`."""
    actions = custom_monster_data()["actions"]
    actions[index] = actions[index] | changes
    return actions


def rat_scores(**changes: int) -> dict[str, int]:
    """Характеристики крысы, где значения из `changes` заменены."""
    return custom_monster_data()["scores"] | changes


# --- чистое приключение, отчёт и формулы ---


def test_a_clean_adventure_has_no_issues_and_a_full_score(srd):
    report = validate(make_adventure(), srd)

    assert report.issues == []
    assert report.score == 100
    assert report.blocking is False


def test_estimated_minutes_of_the_factory_adventure(srd):
    # 12 × 3 локации + 30 × 1 бой + 10 × 1 открытие + 5 × 2 NPC
    assert validate(make_adventure(), srd).estimated_minutes == 86


def test_estimated_minutes_follow_the_formula_for_any_counts(srd):
    data = adventure_data()
    data["locations"]["attic"] = {"id": "attic", "name": "Чердак", "read_aloud": "Пыльно."}
    data["npcs"]["ghost"] = dict(data["npcs"]["marta"], id="ghost", name="Призрак")
    data["encounters"]["rats"] = dict(data["encounters"]["bones"], id="rats")
    data["revelations"]["debt"] = dict(data["revelations"]["kidnapper"], id="debt")
    adventure = Adventure.model_validate(data)

    expected = 12 * 4 + 30 * 2 + 10 * 2 + 5 * 3
    assert validate(adventure, srd).estimated_minutes == expected == 143


def test_estimated_minutes_of_an_empty_world_are_zero(srd):
    adventure = make_adventure(
        locations={}, npcs={}, items={}, encounters={}, revelations={}, acts=(), endings=()
    )

    assert validate(adventure, srd).estimated_minutes == 0


def test_blocking_is_true_only_when_there_is_an_error():
    warning = Issue(code="no_features", severity="warning", ref="location:x", message="м")
    error = Issue(code="schema", severity="error", ref="adventure", message="м")

    assert Report(issues=[], score=100, estimated_minutes=0).blocking is False
    assert Report(issues=[warning], score=97, estimated_minutes=0).blocking is False
    assert Report(issues=[warning, error], score=87, estimated_minutes=0).blocking is True


@pytest.mark.parametrize("score", [-1, 101])
def test_report_score_stays_within_0_and_100(score):
    with pytest.raises(ValueError, match="score"):
        Report(issues=[], score=score, estimated_minutes=0)


def test_score_is_100_minus_10_per_error_minus_3_per_warning(srd):
    data = adventure_data()
    data["rules"]["modules"] = ["no-such-module"]  # 1 ошибка
    data["locations"]["village"]["read_aloud"] = ""  # предупреждение
    data["locations"]["crypt"]["read_aloud"] = ""  # предупреждение
    report = validate(Adventure.model_validate(data), srd)

    assert sorted(i.severity for i in report.issues) == ["error", "warning", "warning"]
    assert report.score == 100 - 10 - 2 * 3
    assert report.blocking is True


def test_warnings_alone_lower_the_score_but_do_not_block(srd):
    report = validate(with_value(("locations", "village", "read_aloud"), ""), srd)

    assert report.score == 97
    assert report.blocking is False


def test_score_never_drops_below_zero(srd):
    exits = [
        {"id": f"x{n}", "to": "nowhere", "direction": "?", "description": "?"} for n in range(11)
    ]
    report = validate(with_value(("locations", "village", "exits"), exits), srd)

    assert sum(issue.severity == "error" for issue in report.issues) > 10
    assert report.score == 0


def test_validate_is_repeatable_and_leaves_the_adventure_alone(srd):
    adventure = with_value(("locations", "village", "read_aloud"), "")
    before = adventure.model_dump()

    assert validate(adventure, srd) == validate(adventure, srd)
    assert adventure.model_dump() == before


def test_validate_survives_an_empty_world(srd):
    adventure = make_adventure(
        locations={}, npcs={}, items={}, encounters={}, revelations={}, acts=(), endings=()
    )

    report = validate(adventure, srd)

    assert report.blocking is True
    # Старт и финал никуда не ведут; оценка времени (0 минут) далека от цели.
    assert codes(report) == ["dangling_ref", "dangling_ref", "duration_mismatch"]


# --- schema ---


def test_unknown_rules_module_is_a_schema_error(srd):
    adventure = with_value(("rules", "modules"), ["campfire", "no-such-module"])

    issue = only_issue(validate(adventure, srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "adventure")
    assert "no-such-module" in issue.message


def test_known_rules_modules_pass(srd):
    adventure = with_value(("rules", "modules"), ["bg3_flavor", "campfire"])

    assert validate(adventure, srd).issues == []


def test_party_level_outside_the_budget_table_is_a_schema_error_and_skips_the_rating(srd):
    # Бюджет боёв задан для уровней 1–5: на 6-м уровне сложность боя оценить нечем.
    issue = only_issue(validate(with_value(("party_level",), 6), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "adventure")
    assert "party_level" in issue.message


def test_a_complete_custom_statblock_passes(srd):
    assert validate(adventure_with_rat(), srd).issues == []


def test_custom_statblock_without_all_six_scores_is_a_schema_error(srd):
    scores = {"str": 8, "dex": 14, "con": 12, "int": 2, "cha": 4}  # нет wis

    issue = only_issue(validate(adventure_with_rat(scores=scores), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "monster:crypt-rat")
    assert "wis" in issue.message


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"hp": 0}, id="hp-0"),
        pytest.param({"hp": 1001}, id="hp-1001"),
        pytest.param({"actions": rat_actions(0, attack_bonus=21)}, id="attack-bonus-21"),
        pytest.param({"actions": rat_actions(0, attack_bonus=-6)}, id="attack-bonus-minus-6"),
        pytest.param({"actions": rat_actions(2, dc=4)}, id="dc-4"),
        pytest.param({"actions": rat_actions(2, dc=31)}, id="dc-31"),
        pytest.param({"actions": rat_actions(2, targets=9)}, id="targets-9"),
        pytest.param({"ac": 0}, id="ac-0"),
        pytest.param({"ac": 31}, id="ac-31"),
        pytest.param({"scores": rat_scores(dex=0)}, id="score-0"),
        pytest.param({"scores": rat_scores(str=31)}, id="score-31"),
        pytest.param({"saves": {"wis": -6}}, id="save-bonus-minus-6"),
        pytest.param({"saves": {"wis": 21}}, id="save-bonus-21"),
        pytest.param({"skills": {"athletics": -6}}, id="skill-bonus-minus-6"),
        pytest.param({"skills": {"athletics": 21}}, id="skill-bonus-21"),
    ],
)
def test_custom_statblock_numbers_outside_the_limits_are_schema_errors(srd, overrides):
    issue = only_issue(validate(adventure_with_rat(**overrides), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "monster:crypt-rat")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"hp": 1}, id="hp-1"),
        pytest.param({"hp": 1000}, id="hp-1000"),
        pytest.param({"actions": rat_actions(0, attack_bonus=20)}, id="attack-bonus-20"),
        pytest.param({"actions": rat_actions(0, attack_bonus=-5)}, id="attack-bonus-minus-5"),
        pytest.param({"actions": rat_actions(2, dc=5)}, id="dc-5"),
        pytest.param({"actions": rat_actions(2, dc=30)}, id="dc-30"),
        pytest.param({"actions": rat_actions(2, targets=8)}, id="targets-8"),
        pytest.param({"ac": 1}, id="ac-1"),
        pytest.param({"ac": 30}, id="ac-30"),
        pytest.param({"scores": rat_scores(dex=1)}, id="score-1"),
        pytest.param({"scores": rat_scores(str=30)}, id="score-30"),
        pytest.param({"saves": {"wis": -5}}, id="save-bonus-minus-5"),
        pytest.param({"saves": {"wis": 20}}, id="save-bonus-20"),
        pytest.param({"skills": {"athletics": -5}}, id="skill-bonus-minus-5"),
        pytest.param({"skills": {"athletics": 20}}, id="skill-bonus-20"),
    ],
)
def test_custom_statblock_numbers_on_the_limits_pass(srd, overrides):
    assert validate(adventure_with_rat(**overrides), srd).issues == []


GIGANTIC = 10**5000  # длиннее предела Python в 4300 цифр: str() такого числа бросает ValueError


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"hp": GIGANTIC}, id="hp"),
        pytest.param({"ac": GIGANTIC}, id="ac"),
        pytest.param({"scores": rat_scores(dex=GIGANTIC)}, id="score"),
        pytest.param({"saves": {"wis": GIGANTIC}}, id="save-bonus"),
        pytest.param({"skills": {"athletics": -GIGANTIC}}, id="skill-bonus"),
        pytest.param({"actions": rat_actions(0, attack_bonus=GIGANTIC)}, id="attack-bonus"),
        pytest.param({"actions": rat_actions(2, dc=GIGANTIC)}, id="dc"),
        pytest.param({"actions": rat_actions(2, targets=GIGANTIC)}, id="targets"),
    ],
)
def test_a_gigantic_number_in_a_statblock_is_a_schema_error_not_a_crash_of_the_validator(
    srd, overrides
):
    # И бой, и сообщение об ошибке печатают число: валидатор не должен падать на его записи.
    issue = only_issue(validate(adventure_with_rat(**overrides), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "monster:crypt-rat")
    assert len(issue.message) < 300


def test_save_dc_of_a_custom_monster_need_not_come_from_the_master_table(srd):
    # Сл спасброска монстра — правило статблока (дыхание, паутина), а не таблица Сл мастера.
    assert validate(adventure_with_rat(actions=rat_actions(2, dc=13)), srd).issues == []


def test_a_gigantic_party_level_does_not_crash_the_validator(srd):
    issue = only_issue(validate(with_value(("party_level",), GIGANTIC), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "adventure")


def test_a_gigantic_monster_count_does_not_crash_the_validator(srd):
    adventure = with_value(("encounters", "bones", "monsters", 0, "count"), GIGANTIC)

    assert codes(validate(adventure, srd)) == ["encounter_too_hard", "schema"]


def test_a_gigantic_xp_of_a_statblock_does_not_crash_the_validator(srd):
    data = adventure_data()
    data["monsters"] = {"crypt-rat": custom_monster_data(xp=GIGANTIC)}
    data["encounters"]["bones"]["monsters"] = [{"monster_id": "crypt-rat", "count": 2}]

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.severity) == ("encounter_too_hard", "warning")


# --- кастомные статблоки: записи костей, обязательные поля и справочники SRD ---
# Бой читает эти поля при каждом действии монстра. Запись, которую валидатор пропустил бы, роняла бы
# ход мастера исключением движка: модель не может исправить данные приключения, поэтому валидатор
# отклоняет их сам.

# Запись с русской буквой «к» вместо латинской d; закодирована кодом, чтобы не путать буквы.
CYRILLIC_DICE = "2\u043a6+1"
BITE_REF = "monster:crypt-rat/action:bite"
SCREECH_REF = "monster:crypt-rat/action:screech"
MULTIATTACK_REF = "monster:crypt-rat/action:multiattack"


def rat_actions_without(index: int, *names: str) -> list[dict[str, Any]]:
    """Действия крысы, где у действия с номером `index` убраны поля `names`."""
    actions = custom_monster_data()["actions"]
    for name in names:
        del actions[index][name]
    return actions


BROKEN_ACTIONS = [
    # запись костей, которую не разберёт движок
    pytest.param(rat_actions(0, damage=CYRILLIC_DICE), BITE_REF, "damage:", id="damage-not-dice"),
    pytest.param(rat_actions(0, damage=""), BITE_REF, "damage:", id="damage-empty"),
    pytest.param(rat_actions(0, damage="1000d6"), BITE_REF, "damage:", id="damage-too-many-dice"),
    pytest.param(rat_actions(0, damage="1d7"), BITE_REF, "damage:", id="damage-no-such-die"),
    pytest.param(
        rat_actions(0, secondary_damage="лол", secondary_damage_type="necrotic"),
        BITE_REF,
        "secondary_damage:",
        id="secondary-damage-not-dice",
    ),
    pytest.param(
        rat_actions(0, secondary_damage="1000d6", secondary_damage_type="necrotic"),
        BITE_REF,
        "secondary_damage:",
        id="secondary-damage-too-many-dice",
    ),
    pytest.param(
        rat_actions(2, damage="лол", damage_type="fire"),
        SCREECH_REF,
        "damage:",
        id="save-damage-not-dice",
    ),
    # поля, без которых действие своего вида не разыграть
    pytest.param(rat_actions_without(0, "damage"), BITE_REF, "damage:", id="attack-no-damage"),
    pytest.param(
        rat_actions_without(0, "attack_bonus"), BITE_REF, "attack_bonus:", id="attack-no-bonus"
    ),
    pytest.param(
        rat_actions_without(0, "damage_type"), BITE_REF, "damage_type:", id="attack-no-type"
    ),
    pytest.param(rat_actions_without(2, "save"), SCREECH_REF, "save:", id="save-no-ability"),
    pytest.param(rat_actions_without(2, "dc"), SCREECH_REF, "dc:", id="save-no-dc"),
    pytest.param(
        rat_actions(2, damage="1d6"), SCREECH_REF, "damage_type:", id="save-damage-without-type"
    ),
    pytest.param(
        rat_actions(1, multiattack=[]), MULTIATTACK_REF, "multiattack:", id="multiattack-empty"
    ),
    # типы урона и состояния бывают только из SRD
    pytest.param(
        rat_actions(0, damage_type="laser"), BITE_REF, "damage_type:", id="attack-unknown-type"
    ),
    pytest.param(
        rat_actions(0, secondary_damage="1d4", secondary_damage_type="laser"),
        BITE_REF,
        "secondary_damage_type:",
        id="secondary-unknown-type",
    ),
    pytest.param(
        rat_actions(2, damage="1d6", damage_type="laser"),
        SCREECH_REF,
        "damage_type:",
        id="save-unknown-type",
    ),
    pytest.param(
        rat_actions(0, condition="doomed"), BITE_REF, "condition:", id="attack-unknown-condition"
    ),
    pytest.param(
        rat_actions(2, condition="doomed"), SCREECH_REF, "condition:", id="save-unknown-condition"
    ),
]


@pytest.mark.parametrize(("actions", "ref", "prefix"), BROKEN_ACTIONS)
def test_a_broken_action_of_a_custom_statblock_is_a_schema_error_of_that_action(
    srd, actions, ref, prefix
):
    issue = only_issue(validate(adventure_with_rat(actions=actions), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", ref)
    assert issue.message.startswith(prefix)


@pytest.mark.parametrize(
    ("changes", "present"),
    [
        pytest.param({"save": "str"}, "save", id="save-without-dc"),
        pytest.param({"dc": 11}, "dc", id="dc-without-save"),
    ],
)
def test_the_saving_throw_of_an_on_hit_condition_is_a_pair_of_save_and_dc(srd, changes, present):
    # Без пары движок молча наложил бы состояние без спасброска.
    actions = rat_actions(0, condition="prone", **changes)

    issue = only_issue(validate(adventure_with_rat(actions=actions), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", BITE_REF)
    assert issue.message.startswith("condition:") and f"только {present}" in issue.message


@pytest.mark.parametrize("hp_dice", ["junk", "", CYRILLIC_DICE, "1000d6", "2d7+1"])
def test_the_hit_dice_of_a_custom_statblock_must_be_a_dice_record(srd, hp_dice):
    issue = only_issue(validate(adventure_with_rat(hp_dice=hp_dice), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "monster:crypt-rat")
    assert issue.message.startswith("hp_dice:")


def test_a_huge_dice_record_is_clipped_in_the_message(srd):
    issue = only_issue(validate(adventure_with_rat(hp_dice="я" * 5000), srd))

    assert len(issue.message) < 300


def test_the_dice_message_names_the_reason_the_engine_gives(srd):
    issue = only_issue(validate(adventure_with_rat(actions=rat_actions(0, damage="1000d6")), srd))

    assert "от 1 до 100" in issue.message


# `dice.parse` принимает целое до 4300 цифр: итог такой записи не сериализуется (журнал хода, ответ
# модели), а модификатор в десятки цифр — нелепый урон. Пределы статблока с запасом покрывают SRD
# (потолок +24 у 8d10+24, один терм с костями) и не зависят от `parse`.

TOO_BIG_DICE = [
    pytest.param("1d6+" + "9" * 4300, id="modifier-4300-digits"),
    pytest.param("1d6+" + "9" * 20, id="modifier-20-digits"),
    pytest.param("1d6+101", id="modifier-101"),
    pytest.param("1d6-101", id="modifier-minus-101"),
    pytest.param("1d6+60+60", id="modifiers-add-up"),
    pytest.param("1d4+1d6+1d8+1d10+1d12", id="five-dice-terms"),
    pytest.param("1d6" + "+1-1" * 20, id="long-record-of-flat-terms"),
]
FITTING_DICE = [
    pytest.param("8d10+24", id="srd-ogre"),
    pytest.param("1d6+100", id="modifier-100"),
    pytest.param("1d6-100", id="modifier-minus-100"),
    pytest.param("1d4+1d6+1d8+1d10", id="four-dice-terms"),
    pytest.param("1d6" + "+0" * 18, id="forty-characters-or-less"),
]


@pytest.mark.parametrize("expr", TOO_BIG_DICE)
def test_hit_dice_beyond_the_limits_are_a_schema_error(srd, expr):
    issue = only_issue(validate(adventure_with_rat(hp_dice=expr), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "monster:crypt-rat")
    assert issue.message.startswith("hp_dice:") and len(issue.message) < 300


@pytest.mark.parametrize(
    ("field", "with_type"),
    [("damage", {}), ("secondary_damage", {"secondary_damage_type": "necrotic"})],
)
@pytest.mark.parametrize("expr", TOO_BIG_DICE)
def test_action_damage_beyond_the_limits_is_a_schema_error(srd, field, with_type, expr):
    actions = rat_actions(0, **{field: expr}, **with_type)
    issue = only_issue(validate(adventure_with_rat(actions=actions), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", BITE_REF)
    assert issue.message.startswith(f"{field}:") and len(issue.message) < 300


@pytest.mark.parametrize("expr", FITTING_DICE)
def test_dice_within_the_limits_pass_in_hit_dice_and_damage(srd, expr):
    actions = rat_actions(0, damage=expr, secondary_damage=expr, secondary_damage_type="necrotic")

    assert validate(adventure_with_rat(hp_dice=expr, actions=actions), srd).issues == []


def test_a_dice_record_of_exactly_forty_characters_fits_and_forty_one_does_not(srd):
    # Длина проверяется до разбора, поэтому граница проходит по числу знаков записи, а не термов.
    fits = "1d10" + "+0" * 18
    too_long = "1d100" + "+0" * 18
    assert (len(fits), len(too_long)) == (40, 41)

    assert validate(adventure_with_rat(hp_dice=fits), srd).issues == []
    issue = only_issue(validate(adventure_with_rat(hp_dice=too_long), srd))
    assert (issue.code, issue.ref) == ("schema", "monster:crypt-rat")
    assert issue.message.startswith("hp_dice:") and "40" in issue.message


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"actions": rat_actions(0, damage="5")}, id="flat-damage"),
        pytest.param({"actions": rat_actions(0, damage="2d6+1d4-1")}, id="several-terms"),
        pytest.param(
            {"actions": rat_actions(0, secondary_damage="1d4", secondary_damage_type="necrotic")},
            id="secondary-damage",
        ),
        pytest.param(
            {"actions": rat_actions(0, condition="paralyzed", save="con", dc=10)},
            id="on-hit-condition-with-save",
        ),
        pytest.param({"actions": rat_actions(0, condition="prone")}, id="on-hit-condition"),
        pytest.param(
            {"actions": rat_actions(2, damage="2d6", damage_type="fire", half_on_save=True)},
            id="save-with-damage",
        ),
        pytest.param(
            {"actions": [{"id": "hiss", "name": "Шипение", "kind": "other"}]},
            id="other-action-needs-nothing",
        ),
    ],
)
def test_complete_actions_of_every_kind_pass(srd, overrides):
    assert validate(adventure_with_rat(**overrides), srd).issues == []


@pytest.mark.parametrize("damage_type", sorted(DAMAGE_TYPES))
def test_every_srd_damage_type_is_allowed_in_a_statblock(srd, damage_type):
    actions = rat_actions(0, damage_type=damage_type)

    assert validate(adventure_with_rat(actions=actions), srd).issues == []


@pytest.mark.parametrize("condition", sorted(load_srd().conditions))
def test_every_srd_condition_is_allowed_in_a_statblock(srd, condition):
    actions = rat_actions(2, condition=condition)

    assert validate(adventure_with_rat(actions=actions), srd).issues == []


def test_known_damage_types_are_the_srd_ones(srd):
    in_data = {
        *(a.damage_type for m in srd.monsters.values() for a in m.actions),
        *(a.secondary_damage_type for m in srd.monsters.values() for a in m.actions),
        *(spell.effect.damage_type for spell in srd.spells.values()),
        *(item.weapon.damage_type for item in srd.items.values() if item.weapon),
    } - {None}

    assert in_data <= DAMAGE_TYPES  # всё, что есть в данных SRD, валидатор допускает
    assert set(get_args(DamageType)) == DAMAGE_TYPES  # и ровно те же типы знают инструменты


def test_every_srd_statblock_passes_the_checks_of_a_custom_statblock(srd):
    # Правила для кастомных статблоков не строже данных SRD: монстр SRD, перенесённый в
    # приключение как свой, проходит их без замечаний.
    data = adventure_data()
    data["monsters"] = {m.id: m.model_dump(mode="json") for m in srd.monsters.values()}

    report = validate(Adventure.model_validate(data), srd)

    assert [issue for issue in report.issues if issue.ref.startswith("monster:")] == []


# --- локация без зон и бой без монстров ---
# `start_combat` берёт зону отряда из первой зоны локации и не начнёт бой без противников: данные,
# где их нет, — ошибка валидатора, как число улик и предел числа монстров.


def test_a_location_without_zones_is_a_schema_error(srd):
    issue = only_issue(validate(with_value(("locations", "village", "zones"), []), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "location:village")
    assert issue.message.startswith("zones:")


def test_a_fight_without_monsters_is_a_schema_error_and_has_no_rating(srd):
    # Пустой бой не должен получать ещё и предупреждение «слишком лёгок».
    issue = only_issue(validate(with_value(("encounters", "bones", "monsters"), []), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "encounter:bones")
    assert issue.message.startswith("monsters:")


def test_the_default_zone_of_a_location_is_enough(srd):
    # Локация без поля `zones` получает зону «здесь» из схемы: фабрика и demo-crypt так и пишут.
    assert "zones" not in adventure_data()["locations"]["village"]
    assert validate(make_adventure(), srd).issues == []


@pytest.mark.parametrize(
    "monsters",
    [
        pytest.param([{"monster_id": "skeleton", "count": 21}], id="one-group-of-21"),
        pytest.param(
            [{"monster_id": "skeleton", "count": 11}, {"monster_id": "zombie", "count": 10}],
            id="two-groups-of-21",
        ),
    ],
)
def test_more_than_20_monsters_in_a_fight_is_a_schema_error(srd, monsters):
    issue = only_issue(validate(with_value(("encounters", "bones", "monsters"), monsters), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", "encounter:bones")


def test_exactly_20_monsters_in_a_fight_pass(srd):
    monsters = [{"monster_id": "skeleton", "count": 20}]  # 1000 XP: «низкий» бой

    assert validate(with_value(("encounters", "bones", "monsters"), monsters), srd).issues == []


# --- повторяющиеся id вложенных сущностей ---
# Отметки находок в `world.revealed` (`feature:<id>`, `clue:<id>`, `exit:<id>`) общие на всё
# приключение, а инструменты ищут выход, место и зону по голому id: повтор id молча ломает игру.


def edited(edit: Callable[[dict[str, Any]], None]) -> Adventure:
    """Чистое приключение, правку которого делает `edit` над «сырыми» данными."""
    data = adventure_data()
    edit(data)
    return Adventure.model_validate(data)


def a_second_ledger_in_the_village(data: dict[str, Any]) -> None:
    data["locations"]["village"]["features"].append(
        {"id": "ledger", "name": "Ещё одна книга", "description": "Тоже долговая."}
    )


def a_second_chest_in_the_cemetery(data: dict[str, Any]) -> None:
    # Тот самый сценарий ревью: два «сундука», `reveal("feature:chest")` открыл бы только первый.
    chest = {"name": "Сундук", "description": "Окован железом."}
    data["locations"]["village"]["features"].append({"id": "chest", **chest})
    data["locations"]["cemetery"]["features"].append({"id": "chest", **chest})


def the_same_feature_twice_in_one_location(data: dict[str, Any]) -> None:
    board = data["locations"]["village"]["features"][0]
    data["locations"]["village"]["features"].append(dict(board))


def a_clue_id_of_another_revelation(data: dict[str, Any]) -> None:
    data["revelations"]["debt"] = {
        "id": "debt",
        "statement": "Марта должна больше, чем говорит.",
        "clues": [
            {"id": "clue-tracks", "description": "Те же следы.", "where": "feature:tracks"},
            {"id": "clue-debt-1", "description": "Расписка.", "where": "npc:marta"},
            {"id": "clue-debt-2", "description": "Запись.", "where": "feature:ledger"},
        ],
    }


def a_clue_id_twice_in_one_revelation(data: dict[str, Any]) -> None:
    data["revelations"]["kidnapper"]["clues"][1]["id"] = "clue-tracks"


def a_second_exit_with_the_same_id_in_a_location(data: dict[str, Any]) -> None:
    data["locations"]["village"]["exits"].append(
        {"id": "to-cemetery", "to": "crypt", "direction": "вниз", "description": "Ещё одна тропа."}
    )


def two_zones_with_one_id_in_a_location(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["zones"] = [
        {"id": "here", "name": "здесь"},
        {"id": "here", "name": "тоже здесь"},
    ]


REPEATED_IDS = [
    pytest.param(
        a_second_ledger_in_the_village,
        "location:crypt/feature:ledger",
        id="feature-in-two-locations",
    ),
    pytest.param(
        a_second_chest_in_the_cemetery,
        "location:cemetery/feature:chest",
        id="feature-chest-in-two-locations",
    ),
    pytest.param(
        the_same_feature_twice_in_one_location,
        "location:village/feature:board",
        id="feature-in-one-location",
    ),
    pytest.param(
        a_clue_id_of_another_revelation,
        "revelation:debt/clue:clue-tracks",
        id="clue-in-two-revelations",
    ),
    pytest.param(
        a_clue_id_twice_in_one_revelation,
        "revelation:kidnapper/clue:clue-tracks",
        id="clue-in-one-revelation",
    ),
    pytest.param(
        a_second_exit_with_the_same_id_in_a_location,
        "location:village/exit:to-cemetery",
        id="exit-in-one-location",
    ),
    pytest.param(
        two_zones_with_one_id_in_a_location,
        "location:cemetery/zone:here",
        id="zone-in-location",
    ),
]


@pytest.mark.parametrize(("edit", "ref"), REPEATED_IDS)
def test_a_repeated_id_of_a_nested_entity_is_a_schema_error(srd, edit, ref):
    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", ref)


def test_the_message_of_a_repeated_id_names_the_id_and_the_first_place_it_is_used(srd):
    issue = only_issue(validate(edited(a_second_ledger_in_the_village), srd))

    assert "ledger" in issue.message
    assert "location:village/feature:ledger" in issue.message


def test_every_extra_use_of_an_id_is_its_own_error(srd):
    def thrice(data: dict[str, Any]) -> None:
        a_second_ledger_in_the_village(data)
        data["locations"]["cemetery"]["features"].append(
            {"id": "ledger", "name": "Третья", "description": "Тоже книга."}
        )

    report = validate(edited(thrice), srd)

    assert codes(report) == ["schema", "schema"]


def test_the_same_id_of_plain_exits_and_zones_in_different_locations_is_fine(srd):
    # Чистое приключение уже так устроено: у деревни и склепа по выходу `to-cemetery`.
    def same_zone_names(data: dict[str, Any]) -> None:
        zones = [{"id": "hall", "name": "зал"}]
        data["locations"]["village"]["zones"] = zones
        data["locations"]["crypt"]["zones"] = zones

    assert validate(edited(same_zone_names), srd).issues == []


def a_hidden_exit_named_like_a_plain_exit_of_another_location(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["exits"][1]["id"] = "to-cemetery"


def two_hidden_exits_with_one_id(data: dict[str, Any]) -> None:
    # Сценарий ревью: `reveal("exit:trapdoor")` в деревне открыл бы и люк кладбища.
    trapdoor = {"id": "trapdoor", "direction": "вниз", "description": "Люк.", "hidden_dc": 20}
    data["locations"]["village"]["exits"].append({**trapdoor, "to": "crypt"})
    data["locations"]["cemetery"]["exits"].append({**trapdoor, "to": "crypt", "hidden_dc": 25})


def test_a_hidden_exit_must_not_share_its_id_with_an_exit_of_another_location(srd):
    issue = only_issue(
        validate(edited(a_hidden_exit_named_like_a_plain_exit_of_another_location), srd)
    )

    assert (issue.code, issue.severity) == ("schema", "error")
    assert issue.ref == "location:cemetery/exit:to-cemetery"
    assert "hidden_dc" in issue.message and "village" in issue.message


def test_two_hidden_exits_with_one_id_are_both_errors(srd):
    report = validate(edited(two_hidden_exits_with_one_id), srd)

    assert sorted((i.code, i.ref) for i in report.issues) == [
        ("schema", "location:cemetery/exit:trapdoor"),
        ("schema", "location:village/exit:trapdoor"),
    ]


def test_a_hidden_exit_with_a_repeated_id_in_its_own_location_is_reported_once(srd):
    def hidden_twice(data: dict[str, Any]) -> None:
        exits = data["locations"]["cemetery"]["exits"]
        exits.append(dict(exits[1]))

    issue = only_issue(validate(edited(hidden_twice), srd))

    assert issue.ref == "location:cemetery/exit:to-crypt"


def test_an_encounter_of_another_location_is_a_dangling_ref(srd):
    # Бой «bones» происходит на кладбище: из деревни `start_combat` его не начнёт.
    adventure = with_value(("locations", "village", "encounter_id"), "bones")

    issue = only_issue(validate(adventure, srd))

    assert (issue.code, issue.severity, issue.ref) == ("dangling_ref", "error", "location:village")
    assert "bones" in issue.message and "cemetery" in issue.message


def test_the_encounter_of_its_own_location_is_fine(srd):
    assert validate(make_adventure(), srd).issues == []  # кладбище и бой «bones»


# Обратное направление: `get_location` показывает бой только по `encounter_id` локации, а
# `start_combat` берёт id боя оттуда же. Бой, на который его локация не ссылается, в игре не
# появится, хотя оценка времени его уже посчитала.


def add_rats_encounter(data: dict[str, Any], location_id: str) -> None:
    """Второй бой в приключении: оценка времени растёт до 116 мин, цель подгоняем под неё."""
    data["encounters"]["rats"] = dict(
        data["encounters"]["bones"], id="rats", location_id=location_id
    )
    data["target_hours"] = 2


def unlinked_encounter_after_clearing_the_location(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["encounter_id"] = None


def second_encounter_in_a_linked_location(data: dict[str, Any]) -> None:
    add_rats_encounter(data, "cemetery")


def encounter_in_a_location_without_one(data: dict[str, Any]) -> None:
    add_rats_encounter(data, "village")


def encounter_that_the_location_swapped_for_another(data: dict[str, Any]) -> None:
    add_rats_encounter(data, "cemetery")
    data["locations"]["cemetery"]["encounter_id"] = "rats"


@pytest.mark.parametrize(
    ("edit", "ref", "location_id"),
    [
        pytest.param(
            unlinked_encounter_after_clearing_the_location,
            "encounter:bones",
            "cemetery",
            id="location-lost-its-encounter_id",
        ),
        pytest.param(
            second_encounter_in_a_linked_location,
            "encounter:rats",
            "cemetery",
            id="second-encounter-in-one-location",
        ),
        pytest.param(
            encounter_in_a_location_without_one,
            "encounter:rats",
            "village",
            id="location-never-had-an-encounter_id",
        ),
        pytest.param(
            encounter_that_the_location_swapped_for_another,
            "encounter:bones",
            "cemetery",
            id="location-points-to-another-encounter",
        ),
    ],
)
def test_an_encounter_its_location_does_not_point_to_is_a_dangling_ref(srd, edit, ref, location_id):
    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity, issue.ref) == ("dangling_ref", "error", ref)
    assert location_id in issue.message and "get_location" in issue.message


def test_an_encounter_of_a_missing_location_is_reported_once(srd):
    # Несуществующую локацию боя называет проверка ссылок; «не привязан» к ней — не вторая проблема.
    adventure = with_value(("encounters", "bones", "location_id"), "nowhere")

    issue = only_issue(validate(adventure, srd))

    assert (issue.code, issue.ref) == ("dangling_ref", "encounter:bones")
    assert "location_id" in issue.message and "nowhere" in issue.message


def test_a_location_pointing_into_the_void_does_not_orphan_its_encounter_twice(srd):
    # Опечатка в `encounter_id` кладбища: бой «bones» остался без ссылки, но причина одна, и её
    # называет проверка ссылок. Правка опечатки чинит обе стороны, второй ошибки не нужно.
    adventure = with_value(("locations", "cemetery", "encounter_id"), "bone")

    issue = only_issue(validate(adventure, srd))

    assert (issue.code, issue.ref) == ("dangling_ref", "location:cemetery")
    assert "encounter_id" in issue.message and "bone" in issue.message


def test_every_location_with_its_own_encounter_is_fine(srd):
    def a_fight_in_the_village_too(data: dict[str, Any]) -> None:
        add_rats_encounter(data, "village")
        data["locations"]["village"]["encounter_id"] = "rats"

    assert validate(edited(a_fight_in_the_village_too), srd).issues == []


def test_a_custom_statblock_must_not_repeat_the_id_of_its_action(srd):
    actions = [*rat_actions(0), custom_monster_data()["actions"][0]]

    issue = only_issue(validate(adventure_with_rat(actions=actions), srd))

    assert (issue.code, issue.severity) == ("schema", "error")
    assert issue.ref == "monster:crypt-rat/action:bite"


# --- dangling_ref ---

DANGLING_REFS = [
    pytest.param(("start_location",), "nowhere", "adventure", id="start_location"),
    pytest.param(("finale_location",), "nowhere", "adventure", id="finale_location"),
    pytest.param(
        ("locations", "crypt", "exits", 0, "to"),
        "nowhere",
        "location:crypt/exit:to-cemetery",
        id="exit.to",
    ),
    pytest.param(
        ("locations", "crypt", "exits", 0, "locked_by"),
        "item:nothing",
        "location:crypt/exit:to-cemetery",
        id="exit.locked_by-item",
    ),
    pytest.param(
        ("locations", "crypt", "exits", 0, "locked_by"),
        "key",
        "location:crypt/exit:to-cemetery",
        id="exit.locked_by-without-kind",
    ),
    pytest.param(
        ("locations", "crypt", "exits", 0, "locked_by"),
        "npc:marta",
        "location:crypt/exit:to-cemetery",
        id="exit.locked_by-wrong-kind",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "locked_by"),
        "item:nothing",
        "location:crypt/feature:ledger",
        id="feature.locked_by-item",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "locked_by"),
        "key",
        "location:crypt/feature:ledger",
        id="feature.locked_by-without-kind",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "locked_by"),
        "npc:marta",
        "location:crypt/feature:ledger",
        id="feature.locked_by-wrong-kind",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "reveals"),
        [{"kind": "clue", "id": "clue-nope"}],
        "location:crypt/feature:ledger",
        id="reveals-clue",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "reveals"),
        [{"kind": "item", "id": "nothing"}],
        "location:crypt/feature:ledger",
        id="reveals-item",
    ),
    pytest.param(
        ("locations", "crypt", "features", 0, "reveals"),
        [{"kind": "exit", "id": "nothing"}],
        "location:crypt/feature:ledger",
        id="reveals-exit",
    ),
    pytest.param(
        ("locations", "cemetery", "encounter_id"),
        "no-fight",
        "location:cemetery",
        id="encounter_id",
    ),
    pytest.param(
        ("locations", "cemetery", "links"),
        [{"a": "here", "b": "nowhere", "distance": "near"}],
        "location:cemetery",
        id="zone-link",
    ),
    pytest.param(("npcs", "marta", "location_id"), "nowhere", "npc:marta", id="npc.location_id"),
    pytest.param(("npcs", "marta", "relations", 0, "npc_id"), "nobody", "npc:marta", id="relation"),
    pytest.param(("npcs", "fabian", "statblock"), "no-such-monster", "npc:fabian", id="statblock"),
    pytest.param(("items", "key", "holder"), "location:nowhere", "item:key", id="holder-location"),
    pytest.param(("items", "potion", "holder"), "npc:nobody", "item:potion", id="holder-npc"),
    pytest.param(("items", "potion", "holder"), "somewhere", "item:potion", id="holder-no-kind"),
    pytest.param(("items", "potion", "holder"), "feature:board", "item:potion", id="holder-kind"),
    # Персонажей в приключении нет: предмет у несуществующего героя не достанется никому.
    pytest.param(("items", "key", "holder"), "character:pc-1", "item:key", id="holder-character"),
    pytest.param(
        ("items", "potion", "base_item"), "no-such-item", "item:potion", id="base_item-not-in-srd"
    ),
    pytest.param(
        ("encounters", "bones", "location_id"),
        "nowhere",
        "encounter:bones",
        id="encounter.location",
    ),
    pytest.param(
        ("encounters", "bones", "monsters", 0, "zone"),
        "nowhere",
        "encounter:bones",
        id="group-zone",
    ),
    pytest.param(("encounters", "bones", "pc_zone"), "nowhere", "encounter:bones", id="pc_zone"),
    pytest.param(
        ("revelations", "kidnapper", "clues", 0, "where"),
        "feature:nope",
        "revelation:kidnapper/clue:clue-tracks",
        id="where-feature",
    ),
    pytest.param(
        ("revelations", "kidnapper", "clues", 0, "where"),
        "npc:nobody",
        "revelation:kidnapper/clue:clue-tracks",
        id="where-npc",
    ),
    pytest.param(
        ("revelations", "kidnapper", "clues", 0, "where"),
        "item:nothing",
        "revelation:kidnapper/clue:clue-tracks",
        id="where-item",
    ),
    pytest.param(
        ("revelations", "kidnapper", "clues", 0, "where"),
        "location:village",
        "revelation:kidnapper/clue:clue-tracks",
        id="where-wrong-kind",
    ),
]


@pytest.mark.parametrize(("path", "value", "ref"), DANGLING_REFS)
def test_a_reference_into_the_void_is_a_dangling_ref(srd, path, value, ref):
    issue = only_issue(validate(with_value(path, value), srd))

    assert (issue.code, issue.severity, issue.ref) == ("dangling_ref", "error", ref)


def test_a_dangling_ref_message_names_the_field_and_the_target(srd):
    issue = only_issue(validate(with_value(("npcs", "marta", "location_id"), "nowhere"), srd))

    assert "location_id" in issue.message
    assert "nowhere" in issue.message


def test_references_that_cannot_be_checked_are_accepted(srd):
    # Флаги придумывает автор: проверять нечего.
    data = adventure_data()
    data["locations"]["crypt"]["exits"][0]["locked_by"] = "flag:ledger-read"
    data["locations"]["crypt"]["features"][0]["reveals"] = [
        {"kind": "clue", "id": "clue-ledger"},
        {"kind": "flag", "id": "ledger-read", "value": True},
    ]
    data["items"]["potion"]["holder"] = "gone"

    assert validate(Adventure.model_validate(data), srd).issues == []


def test_a_feature_locked_by_an_existing_item_or_a_flag_is_accepted(srd):
    # Замок особенности проверяется как замок выхода: предмет должен быть, флаг придумывает автор.
    for lock in ("item:key", "flag:ledger-read"):
        adventure = with_value(("locations", "crypt", "features", 0, "locked_by"), lock)

        assert validate(adventure, srd).issues == []


def test_a_custom_statblock_is_a_valid_npc_statblock(srd):
    data = adventure_data()
    data["monsters"] = {"crypt-rat": custom_monster_data()}
    data["npcs"]["fabian"]["statblock"] = "crypt-rat"

    assert validate(Adventure.model_validate(data), srd).issues == []


def test_multiattack_of_a_custom_statblock_must_name_existing_actions(srd):
    actions = rat_actions(1, multiattack=["bite", "claw"])

    issue = only_issue(validate(adventure_with_rat(actions=actions), srd))

    assert (issue.code, issue.severity) == ("dangling_ref", "error")
    assert issue.ref == "monster:crypt-rat/action:multiattack"
    assert "claw" in issue.message


def test_a_huge_dangling_id_is_clipped_in_the_message(srd):
    adventure = with_value(("locations", "crypt", "exits", 0, "to"), "x" * 5000)

    issue = only_issue(validate(adventure, srd))

    assert issue.code == "dangling_ref"
    assert len(issue.message) < 300


@pytest.mark.parametrize("junk", ["", ":", "a:b:c", "item:", "flag:", "ё:ё", "x" * 3000])
@pytest.mark.parametrize(
    "path",
    [
        ("items", "key", "holder"),
        ("locations", "crypt", "exits", 0, "locked_by"),
        ("locations", "crypt", "features", 0, "locked_by"),
        ("revelations", "kidnapper", "clues", 0, "where"),
    ],
)
def test_garbage_in_a_free_text_reference_is_a_dangling_ref_not_a_crash(srd, path, junk):
    report = validate(with_value(path, junk), srd)

    assert "dangling_ref" in codes(report)
    assert all(len(issue.message) < 300 for issue in report.issues)


# --- seeds: id сущностей, заданных автором ---


@pytest.mark.parametrize(
    "seed", ["village", "marta", "key", "bones", "kidnapper", "crypt-rat"], ids=lambda s: s
)
def test_a_seed_may_name_any_entity_of_the_adventure(srd, seed):
    adventure = adventure_with_rat()
    data = adventure.model_dump() | {"seeds": (seed,)}

    assert validate(Adventure.model_validate(data), srd).issues == []


def test_a_seed_that_names_nothing_is_a_dangling_ref(srd):
    adventure = make_adventure(seeds=("marta", "nobody"))

    issue = only_issue(validate(adventure, srd))

    assert (issue.code, issue.severity, issue.ref) == ("dangling_ref", "error", "adventure")
    assert "nobody" in issue.message and "seeds" in issue.message


def test_a_huge_seed_is_clipped_in_the_message(srd):
    issue = only_issue(validate(make_adventure(seeds=("x" * 5000,)), srd))

    assert issue.code == "dangling_ref" and len(issue.message) < 300


# --- флаги движка ---
# `set_flag` не даёт модели писать флаги `encounter:*` (их ведёт ход боя); находка приключения
# обошла бы этот запрет.


def reveals_flag(name: str) -> Adventure:
    reveals = [{"kind": "clue", "id": "clue-ledger"}, {"kind": "flag", "id": name, "value": "done"}]
    return with_value(("locations", "crypt", "features", 0, "reveals"), reveals)


@pytest.mark.parametrize("name", ["encounter:bones", "encounter:", "encounter:no-such-fight"])
def test_a_finding_must_not_set_a_flag_the_engine_owns(srd, name):
    issue = only_issue(validate(reveals_flag(name), srd))

    assert (issue.code, issue.severity) == ("schema", "error")
    assert issue.ref == "location:crypt/feature:ledger"
    assert "encounter:" in issue.message


@pytest.mark.parametrize("name", ["ledger-read", "encounters", "met:encounter:bones"])
def test_a_finding_may_set_a_story_flag(srd, name):
    assert validate(reveals_flag(name), srd).issues == []


# Замок-флаг открывается любым непустым значением, а флаг `encounter:<id>` движок выставляет уже при
# начале боя (`active`): дверь «после боя» открылась бы в его начале. Такие замки запрещены, как и
# выставление этих флагов находкой.

LOCKS_OF_THE_CRYPT = [
    pytest.param(("locations", "crypt", "exits", 0, "locked_by"), "exit:to-cemetery", id="exit"),
    pytest.param(
        ("locations", "crypt", "features", 0, "locked_by"), "feature:ledger", id="feature"
    ),
]


@pytest.mark.parametrize("name", ["encounter:bones", "encounter:", "encounter:no-such-fight"])
@pytest.mark.parametrize(("path", "place"), LOCKS_OF_THE_CRYPT)
def test_a_lock_must_not_wait_for_a_flag_the_engine_owns(srd, path, place, name):
    issue = only_issue(validate(with_value(path, f"flag:{name}"), srd))

    assert (issue.code, issue.severity) == ("schema", "error")
    assert issue.ref == f"location:crypt/{place}"
    assert "encounter:" in issue.message and "locked_by" in issue.message


@pytest.mark.parametrize("name", ["ledger-read", "encounters", "met:encounter:bones"])
@pytest.mark.parametrize(("path", "place"), LOCKS_OF_THE_CRYPT)
def test_a_lock_may_wait_for_a_story_flag(srd, path, place, name):
    assert validate(with_value(path, f"flag:{name}"), srd).issues == []


# --- id предметов приключения и SRD ---
# Бой считает предметом приключения любой `item_id` из `adventure.items`: предмет с id предмета SRD
# закрыл бы герою его собственное зелье (`use_item` отказал бы).


@pytest.mark.parametrize("srd_id", sorted(load_srd().items))
def test_an_adventure_item_must_not_take_the_id_of_an_srd_item(srd, srd_id):
    data = adventure_data()
    data["items"][srd_id] = dict(data["items"]["potion"], id=srd_id)

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.severity, issue.ref) == ("schema", "error", f"item:{srd_id}")
    assert srd_id in issue.message and "SRD" in issue.message


# --- предметы, которые находка кладёт в руки ---
# Предмет из `Feature.reveals` лежит «внутри» места, пока оно не раскрыто: инструменты ищут такое
# место в локации держателя предмета. Если держатель в другой локации или предмет назван у двух
# мест, понять, где он «внутри», нельзя, и закрытое место можно обойти.

ITEM_REVEAL = {"kind": "item", "id": "potion"}


def potion_revealed_in_the_cemetery(holder: str) -> Adventure:
    """Следы кладбища кладут в руки зелье, которое лежит у `holder`."""
    data = adventure_data()
    data["items"]["potion"]["holder"] = holder
    data["locations"]["cemetery"]["features"][0]["reveals"].append(ITEM_REVEAL)
    return Adventure.model_validate(data)


def test_an_item_revealed_by_a_place_of_the_location_it_lies_in_is_clean(srd):
    assert validate(potion_revealed_in_the_cemetery("location:cemetery"), srd).issues == []


@pytest.mark.parametrize(
    "holder",
    ["npc:marta", "location:village", "location:crypt", "gone"],
    ids=["у-NPC", "в-другой-локации", "в-третьей-локации", "ушёл-из-игры"],
)
def test_an_item_revealed_by_a_place_must_lie_in_the_location_of_that_place(srd, holder):
    issue = only_issue(validate(potion_revealed_in_the_cemetery(holder), srd))

    assert (issue.code, issue.severity) == ("dangling_ref", "error")
    assert issue.ref == "location:cemetery/feature:tracks"
    assert "potion" in issue.message and "location:cemetery" in issue.message


def test_an_item_that_does_not_exist_is_one_dangling_ref_not_two(srd):
    adventure = with_value(
        ("locations", "cemetery", "features", 0, "reveals"), [{"kind": "item", "id": "ghost"}]
    )

    issue = only_issue(validate(adventure, srd))

    assert issue.code == "dangling_ref" and "ghost" in issue.message


def test_an_item_cannot_be_revealed_by_two_places(srd):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["potion"]["holder"] = "location:cemetery"
        features = data["locations"]["cemetery"]["features"]
        features[0]["reveals"].append(ITEM_REVEAL)
        chest = {"id": "chest", "name": "Сундук", "description": "Окован железом."}
        features.append({**chest, "reveals": [ITEM_REVEAL]})

    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity) == ("schema", "error")
    assert issue.ref == "location:cemetery/feature:chest"
    assert "potion" in issue.message and "location:cemetery/feature:tracks" in issue.message


def test_an_item_named_twice_by_one_place_is_a_repeated_id(srd):
    adventure = potion_revealed_in_the_cemetery("location:cemetery")
    data = adventure.model_dump(mode="json")
    data["locations"]["cemetery"]["features"][0]["reveals"].append(ITEM_REVEAL)

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.ref) == ("schema", "location:cemetery/feature:tracks")


def test_an_item_revealed_by_places_of_two_locations_breaks_both_rules(srd):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["potion"]["holder"] = "location:cemetery"
        data["locations"]["cemetery"]["features"][0]["reveals"].append(ITEM_REVEAL)
        data["locations"]["crypt"]["features"][0]["reveals"].append(ITEM_REVEAL)

    report = validate(edited(edit), srd)

    assert sorted((issue.code, issue.ref) for issue in report.issues) == [
        ("dangling_ref", "location:crypt/feature:ledger"),
        ("schema", "location:crypt/feature:ledger"),
    ]


# --- достижимость ---


def test_a_location_nobody_can_walk_to_is_unreachable(srd):
    data = adventure_data()
    data["locations"]["attic"] = {
        "id": "attic",
        "name": "Чердак",
        "read_aloud": "Пыльно.",
        "features": [{"id": "chest", "name": "Сундук", "description": "Пустой."}],
    }

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "unreachable_location",
        "error",
        "location:attic",
    )


def test_exits_are_one_way_so_a_location_that_only_leads_out_is_unreachable(srd):
    data = adventure_data()
    data["locations"]["attic"] = {
        "id": "attic",
        "name": "Чердак",
        "read_aloud": "Пыльно.",
        "features": [{"id": "chest", "name": "Сундук", "description": "Пустой."}],
        "exits": [{"id": "down", "to": "village", "direction": "вниз", "description": "Люк."}],
    }

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.ref) == ("unreachable_location", "location:attic")


def test_hidden_and_locked_exits_still_count_as_paths(srd):
    # Склеп в чистом приключении достижим только через скрытый запертый выход кладбища.
    adventure = make_adventure()
    assert adventure.locations["cemetery"].exits[1].hidden_dc == 15

    assert validate(adventure, srd).issues == []


def test_no_path_to_the_finale_is_an_error_together_with_its_unreachable_location(srd):
    only_way_back = [adventure_data()["locations"]["cemetery"]["exits"][0]]
    adventure = with_value(("locations", "cemetery", "exits"), only_way_back)

    report = validate(adventure, srd)

    assert codes(report) == ["no_path_to_finale", "unreachable_location"]
    finale = next(i for i in report.issues if i.code == "no_path_to_finale")
    assert (finale.severity, finale.ref) == ("error", "location:crypt")
    assert report.blocking is True


# --- достижимость ключа замка-предмета ---
# Запертые выходы для `unreachable_location` и `no_path_to_finale` — пути, но замок-предмет открыт,
# только пока предмет у отряда. Ключ, которого не достать (ушёл из игры или лежит за собственным
# замком), оставляет замок закрытым навсегда. Ошибка, если замок отрезает локацию без другого
# пути или находки места; предупреждение, если закрыта лишняя дверь или пустое место.

LOCK_REF = "location:cemetery/exit:to-crypt"  # люк в склеп, заперт ключом `key`
DEAD_KEY = {"id": "chest-key", "name": "Ключ", "description": "Маленький ключ.", "holder": "gone"}


def with_key_at(holder: str) -> Adventure:
    return with_value(("items", "key", "holder"), holder)


def board_locked_by(lock: str) -> Callable[[dict[str, Any]], None]:
    """Доска объявлений в деревне заперта замком `lock`."""

    def edit(data: dict[str, Any]) -> None:
        data["locations"]["village"]["features"][0]["locked_by"] = lock

    return edit


def key_inside_the_board(data: dict[str, Any]) -> None:
    """Ключ лежит внутри доски объявлений (деревня), а доска заперта этим же ключом."""
    board = data["locations"]["village"]["features"][0]
    board["locked_by"] = "item:key"
    board["reveals"] = [{"kind": "item", "id": "key"}]


@pytest.mark.parametrize("holder", ["location:village", "location:cemetery", "npc:marta"])
def test_a_key_the_party_can_pick_up_before_the_lock_is_clean(srd, holder):
    assert validate(with_key_at(holder), srd).issues == []


@pytest.mark.parametrize(
    "holder",
    ["gone", "location:crypt", "npc:fabian"],
    ids=["ушёл-из-игры", "за-своим-люком", "у-NPC-за-своим-люком"],
)
def test_a_key_nobody_can_get_to_leaves_the_lock_closed_for_ever(srd, holder):
    report = validate(with_key_at(holder), srd)

    issue = only_issue(report)
    assert (issue.code, issue.severity, issue.ref) == ("unreachable_key", "error", LOCK_REF)
    assert "key" in issue.message
    assert report.blocking is True


def test_a_key_inside_the_place_it_locks_is_lost_with_every_lock_it_should_open(srd):
    report = validate(edited(key_inside_the_board), srd)

    assert sorted((i.code, i.severity, i.ref) for i in report.issues) == [
        ("unreachable_key", "error", LOCK_REF),
        ("unreachable_key", "error", "location:village/feature:board"),
    ]


def test_a_key_inside_its_own_place_is_an_error_for_a_place_with_a_finding(srd):
    def edit(data: dict[str, Any]) -> None:
        key_inside_the_board(data)
        data["locations"]["cemetery"]["exits"][1]["locked_by"] = None  # люк без замка

    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity) == ("unreachable_key", "error")
    assert issue.ref == "location:village/feature:board"


def test_a_key_inside_a_place_opened_by_another_reachable_key_is_clean(srd):
    def edit(data: dict[str, Any]) -> None:
        key_inside_the_board(data)
        data["items"]["chest-key"] = DEAD_KEY | {"holder": "npc:marta"}
        board_locked_by("item:chest-key")(data)

    assert validate(edited(edit), srd).issues == []


def test_a_key_inside_a_place_opened_by_a_lost_key_is_lost_too(srd):
    def edit(data: dict[str, Any]) -> None:
        key_inside_the_board(data)
        data["items"]["chest-key"] = DEAD_KEY
        board_locked_by("item:chest-key")(data)

    report = validate(edited(edit), srd)

    assert sorted(i.ref for i in report.issues) == [LOCK_REF, "location:village/feature:board"]
    assert {i.severity for i in report.issues} == {"error"}


def test_a_key_in_a_place_without_a_lock_is_clean(srd):
    def edit(data: dict[str, Any]) -> None:
        board = data["locations"]["village"]["features"][0]
        board["reveals"] = [{"kind": "item", "id": "key"}]

    assert validate(edited(edit), srd).issues == []


def test_a_lock_behind_a_lost_lock_is_not_reported_on_its_own(srd):
    # Склеп недостижим из-за люка; замок на выходе из склепа — следствие, а не вторая причина.
    def edit(data: dict[str, Any]) -> None:
        data["items"]["key"]["holder"] = "gone"
        data["items"]["chest-key"] = DEAD_KEY
        data["locations"]["crypt"]["exits"][0]["locked_by"] = "item:chest-key"

    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.ref) == ("unreachable_key", LOCK_REF)


def test_a_lost_lock_with_another_way_round_is_only_a_warning(srd):
    # Люк заперт навсегда, но в склеп можно спуститься и по лестнице из деревни.
    def edit(data: dict[str, Any]) -> None:
        data["items"]["key"]["holder"] = "gone"
        data["locations"]["village"]["exits"].append(
            {"id": "stairs", "to": "crypt", "direction": "вниз", "description": "Лестница."}
        )

    report = validate(edited(edit), srd)

    issue = only_issue(report)
    assert (issue.code, issue.severity, issue.ref) == ("unreachable_key", "warning", LOCK_REF)
    assert report.blocking is False


def test_a_lost_lock_on_a_place_with_nothing_in_it_is_only_a_warning(srd):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["chest-key"] = DEAD_KEY
        board_locked_by("item:chest-key")(data)

    report = validate(edited(edit), srd)

    issue = only_issue(report)
    assert issue.code == "unreachable_key" and issue.severity == "warning"
    assert issue.ref == "location:village/feature:board"
    assert report.blocking is False


def test_a_lost_lock_on_a_place_that_reveals_something_is_an_error(srd):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["chest-key"] = DEAD_KEY
        data["locations"]["crypt"]["features"][0]["locked_by"] = "item:chest-key"  # улика книги

    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity) == ("unreachable_key", "error")
    assert issue.ref == "location:crypt/feature:ledger"


def test_a_lost_lock_on_a_place_where_a_clue_lies_is_an_error(srd):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["chest-key"] = DEAD_KEY
        board_locked_by("item:chest-key")(data)
        data["revelations"]["kidnapper"]["clues"][1]["where"] = "feature:board"

    issue = only_issue(validate(edited(edit), srd))

    assert (issue.code, issue.severity) == ("unreachable_key", "error")
    assert issue.ref == "location:village/feature:board"


def keys_that_lock_each_other(data: dict[str, Any]) -> None:
    """Ключ люка лежит на полке, запертой ключом сундука, а тот — в доске, запертой ключом люка."""
    village = data["locations"]["village"]
    village["features"][0] |= {
        "locked_by": "item:key",
        "reveals": [{"kind": "item", "id": "chest-key"}],
    }
    village["features"].append(
        {
            "id": "shelf",
            "name": "Полка",
            "description": "Пыльная полка.",
            "locked_by": "item:chest-key",
            "reveals": [{"kind": "item", "id": "key"}],
        }
    )
    data["items"]["chest-key"] = DEAD_KEY | {"holder": "location:village"}
    data["items"]["key"]["holder"] = "location:village"


def test_two_keys_each_locked_in_a_place_the_other_opens_are_both_lost(srd):
    # Замкнутый круг не открывается: ни один из ключей не получить первым. Круги «пройти честно»
    # начинаются с пустых рук, а не с «все предметы у отряда».
    report = validate(edited(keys_that_lock_each_other), srd)

    assert sorted((i.code, i.severity, i.ref) for i in report.issues) == [
        ("unreachable_key", "error", LOCK_REF),
        ("unreachable_key", "error", "location:village/feature:board"),
        ("unreachable_key", "error", "location:village/feature:shelf"),
    ]


def test_a_chain_of_three_keys_that_open_each_other_in_turn_is_clean(srd):
    # Ключ деревни открывает полку, на полке лежит ключ сундука, он открывает доску, а в доске —
    # ключ люка: чтобы дойти до склепа, нужно три круга «получил предмет — открыл замок».
    def edit(data: dict[str, Any]) -> None:
        village = data["locations"]["village"]
        village["features"][0] |= {
            "locked_by": "item:chest-key",
            "reveals": [{"kind": "item", "id": "key"}],
        }
        village["features"].append(
            {
                "id": "shelf",
                "name": "Полка",
                "description": "Пыльная полка.",
                "locked_by": "item:first-key",
                "reveals": [{"kind": "item", "id": "chest-key"}],
            }
        )
        data["items"]["first-key"] = DEAD_KEY | {"id": "first-key", "holder": "npc:marta"}
        data["items"]["chest-key"] = DEAD_KEY | {"holder": "location:village"}
        data["items"]["key"]["holder"] = "location:village"

    assert validate(edited(edit), srd).issues == []


# Текст ошибки уходит автору и в круг исправления генератора, поэтому называет настоящую причину:
# ключ ушёл, лежит там, куда не попасть, или заперт внутри места, а не просто «где-то далеко».


def message_at(report: Report, ref: str) -> str:
    return next(issue.message for issue in report.issues if issue.ref == ref)


def key_in_a_box_locked_by_a_gone_key(data: dict[str, Any]) -> None:
    """Ключ люка лежит в ящике стартовой деревни, а ящик заперт ушедшим из игры ключом."""
    data["locations"]["village"]["features"].append(
        {
            "id": "box",
            "name": "Ящик",
            "description": "Тяжёлый ящик.",
            "locked_by": "item:old-key",
            "reveals": [{"kind": "item", "id": "key"}],
        }
    )
    data["items"]["key"]["holder"] = "location:village"
    data["items"]["old-key"] = DEAD_KEY | {"id": "old-key"}


def test_a_key_inside_a_locked_place_of_a_reachable_location_names_the_place_and_its_lock(srd):
    report = validate(edited(key_in_a_box_locked_by_a_gone_key), srd)

    assert sorted((i.code, i.severity, i.ref) for i in report.issues) == [
        ("unreachable_key", "error", LOCK_REF),
        ("unreachable_key", "error", "location:village/feature:box"),
    ]
    message = message_at(report, LOCK_REF)
    # Деревня достижима: «куда не попасть» было бы неправдой, и генератор унёс бы ключ в другую
    # локацию.
    assert "'key'" in message and "внутри места 'box' (location:village)" in message
    assert "заперто недоступным ключом 'old-key'" in message
    assert "куда отряд не попадёт" not in message
    assert "ушёл из игры" in message_at(report, "location:village/feature:box")


@pytest.mark.parametrize(
    ("holder", "reason"),
    [
        ("gone", "ушёл из игры"),
        ("location:crypt", "лежит у 'location:crypt', куда отряд не попадёт"),
        ("npc:fabian", "лежит у 'npc:fabian', куда отряд не попадёт"),
    ],
    ids=["ушёл-из-игры", "за-своим-люком", "у-NPC-за-своим-люком"],
)
def test_a_key_that_is_not_inside_a_locked_place_says_where_it_lies_or_that_it_is_gone(
    srd, holder, reason
):
    message = message_at(validate(with_key_at(holder), srd), LOCK_REF)

    assert reason in message
    assert "внутри места" not in message


def test_a_key_in_an_unlocked_place_of_an_unreachable_location_is_not_called_locked(srd):
    # Место в склепе называет ключ в `reveals`, но не заперто: ключ потерян, потому что в склеп не
    # попасть, а не из-за замка места.
    def edit(data: dict[str, Any]) -> None:
        data["items"]["key"]["holder"] = "location:crypt"
        data["locations"]["crypt"]["features"][0]["reveals"].append({"kind": "item", "id": "key"})

    message = message_at(validate(edited(edit), srd), LOCK_REF)

    assert "лежит у 'location:crypt', куда отряд не попадёт" in message
    assert "внутри места" not in message and "заперто" not in message


# --- скорость прохождения: длинные цепочки замков ---
# Приключение пишет автор или кто-то передал папку, а валидатор идёт при старте сервера: цепочка
# из сотен сундуков не должна задерживать старт. Раньше каждый круг прохождения заново искал для
# каждого предмета место, где он лежит, перебором всех мест локации, и 800 сундуков считались около
# 47 с. Порог в тестах с большим запасом над целью (меньше секунды): он ловит возврат к кубическому
# росту, но не зависит от загруженности машины.

CHAIN_LENGTH = 800
CHAIN_TIME_LIMIT_SECONDS = 5


def add_chest_chain(data: dict[str, Any], length: int, *, closed_ring: bool = False) -> None:
    """В деревню добавлено `length` сундуков: сундук k заперт ключом k−1 и хранит ключ k. Первый
    сундук открыт; при `closed_ring` он заперт последним ключом, и ни один сундук не открыть."""
    village = data["locations"]["village"]
    for number in range(length):
        lock = number - 1 if number else (length - 1 if closed_ring else None)
        village["features"].append(
            {
                "id": f"chest-{number}",
                "name": f"Сундук {number}",
                "description": "Окованный сундук.",
                "locked_by": None if lock is None else f"item:chest-key-{lock}",
                "reveals": [{"kind": "item", "id": f"chest-key-{number}"}],
            }
        )
        data["items"][f"chest-key-{number}"] = DEAD_KEY | {
            "id": f"chest-key-{number}",
            "holder": "location:village",
        }


def validated_within_limit(adventure: Adventure, srd: SrdContent) -> Report:
    started = time.perf_counter()
    report = validate(adventure, srd)
    assert time.perf_counter() - started < CHAIN_TIME_LIMIT_SECONDS
    return report


def test_a_chain_of_800_chests_each_holding_the_key_to_the_next_is_validated_quickly(srd):
    adventure = edited(lambda data: add_chest_chain(data, CHAIN_LENGTH))

    assert validated_within_limit(adventure, srd).issues == []


def test_a_ring_of_800_chests_that_lock_each_other_is_validated_quickly_and_all_are_lost(srd):
    adventure = edited(lambda data: add_chest_chain(data, CHAIN_LENGTH, closed_ring=True))

    report = validated_within_limit(adventure, srd)

    assert codes(report) == ["unreachable_key"] * CHAIN_LENGTH
    assert {issue.ref for issue in report.issues} == {
        f"location:village/feature:chest-{number}" for number in range(CHAIN_LENGTH)
    }
    assert all(issue.severity == "error" for issue in report.issues)


def add_room_chain(data: dict[str, Any], length: int) -> None:
    """Из деревни ведёт цепочка из `length` комнат: дверь из комнаты k заперта ключом k, а ключ k
    лежит в ящике комнаты k. Чтобы дойти до последней, нужно получить все ключи по порядку."""
    data["locations"]["village"]["exits"].append(
        {"id": "to-room-0", "to": "room-0", "direction": "восток", "description": "Дверь."}
    )
    for number in range(length):
        exits = [
            {"id": f"back-{number}", "to": "village", "direction": "назад", "description": "."}
        ]
        if number + 1 < length:
            exits.append(
                {
                    "id": f"to-room-{number + 1}",
                    "to": f"room-{number + 1}",
                    "direction": "дальше",
                    "description": "Дверь.",
                    "locked_by": f"item:room-key-{number}",
                }
            )
        data["locations"][f"room-{number}"] = {
            "id": f"room-{number}",
            "name": f"Комната {number}",
            "read_aloud": "Тесно.",
            "exits": exits,
            "features": [
                {
                    "id": f"box-{number}",
                    "name": "Ящик",
                    "description": "Ящик с ключом.",
                    "reveals": [{"kind": "item", "id": f"room-key-{number}"}],
                }
            ],
        }
        data["items"][f"room-key-{number}"] = DEAD_KEY | {
            "id": f"room-key-{number}",
            "holder": f"location:room-{number}",
        }


def test_a_chain_of_800_rooms_each_behind_the_key_of_the_previous_one_is_validated_quickly(srd):
    # Другая форма той же цепочки: растёт число локаций, а не особенностей одной локации. Оценка
    # времени игры от такого числа комнат уходит от цели, это единственное замечание.
    adventure = edited(lambda data: add_room_chain(data, CHAIN_LENGTH))

    report = validated_within_limit(adventure, srd)

    assert codes(report) == ["duration_mismatch"]
    assert report.blocking is False


def test_a_chain_broken_in_the_middle_loses_only_the_chests_from_the_break_on(srd):
    # Сундук 3 заперт ключом, которого нет в игре: ключи 3 и дальше недостижимы, ключи 0–2 нет.
    def edit(data: dict[str, Any]) -> None:
        add_chest_chain(data, 8)
        data["items"]["ghost-key"] = DEAD_KEY | {"id": "ghost-key"}
        chests = {f["id"]: f for f in data["locations"]["village"]["features"]}
        chests["chest-3"]["locked_by"] = "item:ghost-key"

    report = validate(edited(edit), srd)

    assert {issue.ref for issue in report.issues} == {
        f"location:village/feature:chest-{number}" for number in range(3, 8)
    }
    assert codes(report) == ["unreachable_key"] * 5


# --- открытия и улики ---


def revelation_with_first_clues(count: int) -> Adventure:
    """Открытие `kidnapper` с первыми `count` уликами; особенности больше не раскрывают лишние."""
    data = adventure_data()
    data["revelations"]["kidnapper"]["clues"] = data["revelations"]["kidnapper"]["clues"][:count]
    data["locations"]["crypt"]["features"][0]["reveals"] = []  # clue-ledger — третья улика
    if count == 0:
        data["locations"]["cemetery"]["features"][0]["reveals"] = []  # clue-tracks — первая
    return Adventure.model_validate(data)


def test_a_revelation_with_fewer_than_three_clues_is_an_error(srd):
    issue = only_issue(validate(revelation_with_first_clues(2), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "too_few_clues",
        "error",
        "revelation:kidnapper",
    )


def test_a_revelation_without_clues_is_only_too_few_clues(srd):
    issue = only_issue(validate(revelation_with_first_clues(0), srd))

    assert issue.code == "too_few_clues"


# id улик чистого приключения: их ждут `reveals` особенностей, поэтому при замене улик id те же.
CLUE_IDS = ("clue-tracks", "clue-marta", "clue-ledger", "clue-extra")


def clues_at(*places: str) -> list[dict[str, str]]:
    """Улики открытия `kidnapper` с заданными местами (`where`) вместо прежних."""
    return [
        {"id": clue_id, "description": "Улика.", "where": place}
        for clue_id, place in zip(CLUE_IDS, places, strict=False)
    ]


def test_three_clues_in_one_feature_are_in_one_place(srd):
    clues = clues_at("feature:tracks", "feature:tracks", "feature:tracks")

    issue = only_issue(validate(with_value(("revelations", "kidnapper", "clues"), clues), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "clues_same_place",
        "error",
        "revelation:kidnapper",
    )


def test_clues_of_different_kinds_in_one_location_are_in_one_place(srd):
    # Доска, Марта и ключ — всё в деревне: не дошли до деревни, не нашли ничего.
    clues = clues_at("feature:board", "npc:marta", "item:key")

    issue = only_issue(validate(with_value(("revelations", "kidnapper", "clues"), clues), srd))

    assert issue.code == "clues_same_place"


def test_a_clue_item_held_by_an_npc_lies_where_the_npc_is(srd):
    clues = clues_at("feature:board", "npc:marta", "item:potion")  # зелье у Марты, в деревне

    issue = only_issue(validate(with_value(("revelations", "kidnapper", "clues"), clues), srd))

    assert issue.code == "clues_same_place"


def test_two_clues_in_one_place_are_both_too_few_and_in_one_place(srd):
    data = adventure_data()
    data["revelations"]["kidnapper"]["clues"] = clues_at("feature:tracks", "feature:tracks")
    data["locations"]["crypt"]["features"][0]["reveals"] = []  # clue-ledger убрана

    report = validate(Adventure.model_validate(data), srd)

    assert codes(report) == ["clues_same_place", "too_few_clues"]


def test_a_lone_clue_is_only_too_few_not_in_one_place(srd):
    assert codes(validate(revelation_with_first_clues(1), srd)) == ["too_few_clues"]


def test_clues_in_two_locations_are_not_in_one_place(srd):
    clues = clues_at("feature:board", "npc:marta", "feature:tracks")

    assert validate(with_value(("revelations", "kidnapper", "clues"), clues), srd).issues == []


def test_an_item_nobody_holds_in_a_location_counts_as_its_own_place(srd):
    data = adventure_data()
    data["items"]["potion"]["holder"] = "gone"
    data["revelations"]["kidnapper"]["clues"] = clues_at(
        "feature:board", "npc:marta", "item:potion"
    )

    assert validate(Adventure.model_validate(data), srd).issues == []


# --- бои ---


def test_a_monster_from_nowhere_is_an_unknown_monster(srd):
    monsters = [{"monster_id": "no-such-monster", "count": 2}]

    issue = only_issue(validate(with_value(("encounters", "bones", "monsters"), monsters), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "unknown_monster",
        "error",
        "encounter:bones",
    )
    assert "no-such-monster" in issue.message


def test_a_custom_statblock_is_a_known_monster(srd):
    data = adventure_data()
    data["monsters"] = {"crypt-rat": custom_monster_data(xp=100)}
    data["encounters"]["bones"]["monsters"] = [{"monster_id": "crypt-rat", "count": 14}]

    assert validate(Adventure.model_validate(data), srd).issues == []  # 1400 XP: «умеренный» бой


@pytest.mark.parametrize(
    ("count", "code"),
    [(19, "encounter_too_hard"), (4, "encounter_too_easy")],
)
def test_a_fight_outside_the_budget_is_a_warning(srd, count, code):
    # Гуль — 200 XP. Для 6 героев 3-го уровня бой слабее 900 XP тривиален, сильнее 3600 смертелен.
    monsters = [{"monster_id": "ghoul", "count": count}]

    issue = only_issue(validate(with_value(("encounters", "bones", "monsters"), monsters), srd))

    assert (issue.code, issue.severity, issue.ref) == (code, "warning", "encounter:bones")


@pytest.mark.parametrize("count", [5, 18])  # 1000 XP — «низкий», 3600 XP — ещё «высокий»
def test_a_fight_inside_the_budget_is_clean(srd, count):
    monsters = [{"monster_id": "ghoul", "count": count}]

    assert validate(with_value(("encounters", "bones", "monsters"), monsters), srd).issues == []


# --- предупреждения о тексте и NPC ---


@pytest.mark.parametrize("text", ["", "   \n"])
def test_an_empty_read_aloud_text_is_a_warning(srd, text):
    issue = only_issue(validate(with_value(("locations", "village", "read_aloud"), text), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "empty_read_aloud",
        "warning",
        "location:village",
    )


def test_a_location_without_features_is_a_warning(srd):
    issue = only_issue(validate(with_value(("locations", "village", "features"), []), srd))

    assert (issue.code, issue.severity, issue.ref) == ("no_features", "warning", "location:village")


@pytest.mark.parametrize("motive", ["", "  "])
def test_an_npc_without_a_motive_is_a_warning(srd, motive):
    issue = only_issue(validate(with_value(("npcs", "marta", "motive"), motive), srd))

    assert (issue.code, issue.severity, issue.ref) == ("npc_without_motive", "warning", "npc:marta")


def extra_npc_data(**fields: Any) -> dict[str, Any]:
    """«Сырые» данные третьего NPC `extra`: по умолчанию без связей и статблока."""
    return {
        "id": "extra",
        "name": "Прохожий",
        "role": "крестьянин",
        "appearance": "Усталый.",
        "manner": "бормочет",
        "motive": "дойти до дома",
        "location_id": "village",
    } | fields


def with_extra_npc(**fields: Any) -> Adventure:
    data = adventure_data()
    data["npcs"]["extra"] = extra_npc_data(**fields)
    return Adventure.model_validate(data)


def test_an_npc_with_a_statblock_and_no_relations_is_a_warning(srd):
    issue = only_issue(validate(with_extra_npc(statblock="skeleton"), srd))

    assert (issue.code, issue.severity, issue.ref) == (
        "npc_without_relations",
        "warning",
        "npc:extra",
    )


def test_an_npc_named_in_a_clue_and_without_relations_is_a_warning(srd):
    data = adventure_data()
    data["npcs"]["extra"] = extra_npc_data()
    places = ("feature:tracks", "npc:marta", "feature:ledger", "npc:extra")
    data["revelations"]["kidnapper"]["clues"] = clues_at(*places)

    issue = only_issue(validate(Adventure.model_validate(data), srd))

    assert (issue.code, issue.ref) == ("npc_without_relations", "npc:extra")


def test_a_plain_npc_without_relations_is_not_warned(srd):
    assert validate(with_extra_npc(), srd).issues == []


def test_an_npc_other_npcs_have_a_relation_to_is_not_warned(srd):
    data = adventure_data()
    data["npcs"]["extra"] = extra_npc_data(statblock="skeleton")
    data["npcs"]["marta"]["relations"].append({"npc_id": "extra", "kind": "neighbour"})

    assert validate(Adventure.model_validate(data), srd).issues == []


# --- оценка времени ---


@pytest.mark.parametrize("hours", [1.9, 1.15])  # 114 и 69 минут при оценке 86: отличие ≤ 25%
def test_a_duration_within_25_percent_of_the_target_is_clean(srd, hours):
    assert validate(with_value(("target_hours",), hours), srd).issues == []


def test_a_huge_target_does_not_overflow_the_comparison(srd):
    issue = only_issue(validate(with_value(("target_hours",), 1e307), srd))

    assert issue.code == "duration_mismatch"


@pytest.mark.parametrize("hours", [1.95, 1.1])  # 117 и 66 минут при оценке 86: отличие > 25%
def test_a_duration_more_than_25_percent_off_the_target_is_a_warning(srd, hours):
    issue = only_issue(validate(with_value(("target_hours",), hours), srd))

    assert (issue.code, issue.severity, issue.ref) == ("duration_mismatch", "warning", "adventure")
    assert "86" in issue.message


# --- безопасный текст проблем ---
# Отчёт идёт в журнал сервера, в ответ создателю игры и в отчёт генератора. Папку приключения пишет
# владелец или ему её кто-то передал, поэтому текст проблем — чужой: id зоны и id действия монстра
# шаблона не имеют (модели боя общие с состоянием игры), а ссылки (`Exit.to`, `locked_by`, `holder`)
# — свободные строки. Ссылка и сообщение каждой проблемы состоят из печатных символов и ограничены
# по длине, а после чужого значения в сообщении остаётся причина.

HOSTILE = "x\nFAKE LOG LINE\x1b[31m\u2028\u202e" + "Z" * 3000
HOSTILE_ESCAPED = "x\\nFAKE LOG LINE\\x1b[31m\\u2028\\u202e"


def assert_issues_are_safe(report: Report) -> None:
    assert report.issues
    for issue in report.issues:
        # Значения не выводятся в сообщение проверки: чужой текст на три тысячи символов.
        ref_is_safe = issue.ref.isprintable() and len(issue.ref) <= MAX_REF_LENGTH
        message_is_safe = issue.message.isprintable() and len(issue.message) <= MAX_MESSAGE_LENGTH
        assert ref_is_safe, f"ссылка проблемы {issue.code} небезопасна"
        assert message_is_safe, f"сообщение проблемы {issue.code} небезопасно"


def test_a_repeated_zone_with_a_hostile_id_gives_safe_issues_and_names_the_reason(srd):
    def hostile_zones(data: dict[str, Any]) -> None:
        zones = [{"id": HOSTILE, "name": "зал"}, {"id": HOSTILE, "name": "ещё зал"}]
        data["locations"]["cemetery"]["zones"] = zones

    report = validate(edited(hostile_zones), srd)

    assert_issues_are_safe(report)
    repeated = [issue for issue in report.issues if issue.message.startswith("зона: id")]
    assert len(repeated) == 1
    assert repeated[0].ref.startswith(f"location:cemetery/zone:{HOSTILE_ESCAPED}")
    assert "повторяется" in repeated[0].message


def hostile_action_ids(data: dict[str, Any]) -> None:
    bite = custom_monster_data()["actions"][0]
    actions = [bite | {"id": HOSTILE}, bite | {"id": HOSTILE}]
    data["monsters"] = {"crypt-rat": custom_monster_data(actions=actions)}


def test_a_repeated_action_with_a_hostile_id_gives_safe_issues_and_names_the_reason(srd):
    report = validate(edited(hostile_action_ids), srd)

    assert_issues_are_safe(report)
    repeated = [issue for issue in report.issues if issue.message.startswith("действие: id")]
    assert len(repeated) == 1
    assert repeated[0].ref.startswith(f"monster:crypt-rat/action:{HOSTILE_ESCAPED}")
    assert "повторяется" in repeated[0].message


def hostile_link_end(data: dict[str, Any]) -> None:
    link = {"a": HOSTILE, "b": "here", "distance": "near"}
    data["locations"]["cemetery"]["links"] = [link]


def hostile_engine_flag_reveal(data: dict[str, Any]) -> None:
    flag = {"kind": "flag", "id": "encounter:" + HOSTILE}
    data["locations"]["village"]["features"][0]["reveals"] = [flag]


def hostile_item_holder_of_a_revealed_item(data: dict[str, Any]) -> None:
    data["items"]["potion"]["holder"] = HOSTILE
    data["locations"]["cemetery"]["features"][0]["reveals"].append(ITEM_REVEAL)


def hostile_monster(**overrides: Any) -> Callable[[dict[str, Any]], None]:
    def edit(data: dict[str, Any]) -> None:
        data["monsters"] = {"crypt-rat": custom_monster_data(**overrides)}

    return edit


def at(path: FieldPath, value: Any) -> Callable[[dict[str, Any]], None]:
    """Правка «сырых» данных: значение `value` по пути из ключей и индексов."""

    def edit(data: dict[str, Any]) -> None:
        target: Any = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return edit


# (правка, фрагмент причины, который виден в сообщении после чужого значения)
HOSTILE_VALUES = [
    pytest.param(
        at(("locations", "crypt", "exits", 0, "to"), HOSTILE), "никуда не ведёт", id="exit-to"
    ),
    pytest.param(at(("items", "key", "holder"), HOSTILE), "никуда не ведёт", id="item-holder"),
    pytest.param(
        at(("locations", "crypt", "exits", 0, "locked_by"), HOSTILE),
        "никуда не ведёт",
        id="exit-lock",
    ),
    pytest.param(
        at(("locations", "village", "features", 0, "locked_by"), HOSTILE),
        "никуда не ведёт",
        id="feature-lock",
    ),
    pytest.param(
        at(("revelations", "kidnapper", "clues", 0, "where"), HOSTILE),
        "никуда не ведёт",
        id="clue-where",
    ),
    pytest.param(at(("npcs", "marta", "location_id"), HOSTILE), "никуда не ведёт", id="npc-place"),
    pytest.param(
        at(("npcs", "marta", "statblock"), HOSTILE), "никуда не ведёт", id="npc-statblock"
    ),
    pytest.param(
        at(("npcs", "marta", "relations", 0, "npc_id"), HOSTILE),
        "никуда не ведёт",
        id="npc-relation",
    ),
    pytest.param(at(("items", "key", "base_item"), HOSTILE), "никуда не ведёт", id="srd-item"),
    pytest.param(
        at(("encounters", "bones", "location_id"), HOSTILE), "никуда не ведёт", id="fight-place"
    ),
    pytest.param(at(("seeds",), [HOSTILE]), "нет ни в одной коллекции", id="seed"),
    pytest.param(
        at(("encounters", "bones", "monsters", 0, "monster_id"), HOSTILE),
        "нет ни в SRD",
        id="monster-id",
    ),
    pytest.param(at(("encounters", "bones", "pc_zone"), HOSTILE), " нет", id="party-zone"),
    pytest.param(
        at(("encounters", "bones", "monsters", 0, "zone"), HOSTILE), " нет", id="monster-zone"
    ),
    pytest.param(hostile_link_end, " нет", id="link-end"),
    pytest.param(at(("rules", "modules"), [HOSTILE]), "доступны", id="rules-module"),
    pytest.param(
        hostile_engine_flag_reveal, "находка не может его выставить", id="engine-flag-reveal"
    ),
    pytest.param(
        at(("locations", "village", "features", 0, "locked_by"), "flag:encounter:" + HOSTILE),
        "открылся бы в начале боя",
        id="engine-flag-lock",
    ),
    pytest.param(
        hostile_item_holder_of_a_revealed_item, "должен лежать в локации", id="revealed-holder"
    ),
    pytest.param(hostile_action_ids, "повторяется", id="action-id"),
    pytest.param(
        hostile_monster(actions=rat_actions(1, multiattack=[HOSTILE])),
        "ссылается на несуществующий id",
        id="multiattack-target",
    ),
    pytest.param(
        hostile_monster(actions=rat_actions(0, damage_type=HOSTILE)),
        "допустимы",
        id="damage-type",
    ),
    pytest.param(
        hostile_monster(actions=rat_actions(2, condition=HOSTILE)), "допустимы", id="condition"
    ),
    pytest.param(
        hostile_monster(actions=rat_actions(0, damage="2d6\n\x1b[31m")),
        "не удалось разобрать запись костей",
        id="dice",
    ),
    pytest.param(
        hostile_monster(actions=rat_actions(0, id=HOSTILE, attack_bonus=99)),
        "вне пределов",
        id="action-id-in-number",
    ),
]


@pytest.mark.parametrize(("edit", "reason"), HOSTILE_VALUES)
def test_a_hostile_value_from_a_file_gives_safe_issues_and_leaves_the_reason_visible(
    srd, edit, reason
):
    report = validate(edited(edit), srd)

    assert_issues_are_safe(report)
    assert any(reason in issue.message for issue in report.issues), report.issues


def test_the_longest_reference_made_of_pattern_ids_is_not_clipped():
    longest_id = "a" * 64  # длиннее id шаблон не пропускает
    refs = [
        f"location:{longest_id}/feature:{longest_id}",
        f"location:{longest_id}/exit:{longest_id}",
        f"revelation:{longest_id}/clue:{longest_id}",
    ]

    assert max(map(len, refs)) <= MAX_REF_LENGTH


def test_a_longest_legal_id_stays_whole_in_a_message(srd):
    longest_id = "a" * 64
    adventure = with_value(("locations", "crypt", "exits", 0, "to"), longest_id)

    issue = only_issue(validate(adventure, srd))

    assert f"location:{longest_id}" in issue.message and "…" not in issue.message


def test_the_shipped_adventure_keeps_its_report_word_for_word(demo_adventure, srd):
    # Обычные id короче пределов: нейтрализация и обрезка текста проблем отчёт не меняют.
    report = validate(demo_adventure, srd)

    assert (report.blocking, report.score) == (False, 91)
    assert [(issue.code, issue.severity, issue.ref, issue.message) for issue in report.issues] == [
        ("encounter_too_easy", "warning", "encounter:fabian-fight", "бой слишком лёгок: 550 XP"),
        ("encounter_too_easy", "warning", "encounter:risen-bones", "бой слишком лёгок: 250 XP"),
        (
            "duration_mismatch",
            "warning",
            "adventure",
            "оценка времени игры 116 мин (1.9 ч) отличается от цели 1.5 ч больше чем на 25%",
        ),
    ]
