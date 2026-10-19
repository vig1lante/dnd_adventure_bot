"""Бюджет боёв и валидатор приключения (спека §6.2, план B4.3).

Приключения собираются в памяти фабрикой `adventure_factory`: чистое приключение не даёт ни
ошибок, ни предупреждений, а каждый тест вносит в него одну правку и ждёт один код.
"""

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
from dnd_engine.adventure.validator import DAMAGE_TYPES, Issue, Report, validate
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
    assert DAMAGE_TYPES == set(get_args(DamageType))  # и ровно те же типы знают инструменты


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


def test_a_custom_statblock_must_not_repeat_the_id_of_its_action(srd):
    actions = rat_actions(0) + [custom_monster_data()["actions"][0]]

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
