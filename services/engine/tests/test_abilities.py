import pytest

from dnd_engine.abilities import (
    ABILITY_NAMES_RU,
    SKILL_ABILITY,
    SKILL_NAMES_RU,
    Ability,
    Skill,
    modifier,
    proficiency_bonus,
)

# Таблица навыков 5e целиком: сверяем с правилами, а не с самим кодом.
EXPECTED_SKILLS_BY_ABILITY = {
    Ability.STR: {Skill.ATHLETICS},
    Ability.DEX: {Skill.ACROBATICS, Skill.SLEIGHT_OF_HAND, Skill.STEALTH},
    Ability.CON: set(),
    Ability.INT: {
        Skill.ARCANA,
        Skill.HISTORY,
        Skill.INVESTIGATION,
        Skill.NATURE,
        Skill.RELIGION,
    },
    Ability.WIS: {
        Skill.ANIMAL_HANDLING,
        Skill.INSIGHT,
        Skill.MEDICINE,
        Skill.PERCEPTION,
        Skill.SURVIVAL,
    },
    Ability.CHA: {Skill.DECEPTION, Skill.INTIMIDATION, Skill.PERFORMANCE, Skill.PERSUASION},
}


@pytest.mark.parametrize(
    ("score", "expected"),
    [(1, -5), (8, -1), (9, -1), (10, 0), (11, 0), (15, 2), (20, 5), (30, 10)],
)
def test_modifier(score, expected):
    assert modifier(score) == expected


def test_modifier_grows_by_one_every_two_points():
    assert [modifier(score) for score in range(8, 16)] == [-1, -1, 0, 0, 1, 1, 2, 2]


# Таблица значений характеристик SRD «Ability Scores and Modifiers» целиком, по парам значений.
SRD_MODIFIER_TABLE = [
    (range(1, 2), -5),
    (range(2, 4), -4),
    (range(4, 6), -3),
    (range(6, 8), -2),
    (range(8, 10), -1),
    (range(10, 12), 0),
    (range(12, 14), 1),
    (range(14, 16), 2),
    (range(16, 18), 3),
    (range(18, 20), 4),
    (range(20, 22), 5),
    (range(22, 24), 6),
    (range(24, 26), 7),
    (range(26, 28), 8),
    (range(28, 30), 9),
    (range(30, 31), 10),
]


def test_modifier_matches_the_srd_table_for_every_score_from_1_to_30():
    for scores, expected in SRD_MODIFIER_TABLE:
        for score in scores:
            assert modifier(score) == expected, score
    assert sum(len(scores) for scores, _ in SRD_MODIFIER_TABLE) == 30


def test_proficiency_bonus_matches_the_srd_table_for_every_level_from_1_to_20():
    expected = [2] * 4 + [3] * 4 + [4] * 4 + [5] * 4 + [6] * 4

    assert [proficiency_bonus(level) for level in range(1, 21)] == expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [(1, 2), (4, 2), (5, 3), (8, 3), (9, 4), (12, 4), (13, 5), (16, 5), (17, 6), (20, 6)],
)
def test_proficiency_bonus(level, expected):
    assert proficiency_bonus(level) == expected


@pytest.mark.parametrize("level", [0, -1])
def test_proficiency_bonus_rejects_levels_below_one(level):
    with pytest.raises(ValueError):
        proficiency_bonus(level)


def test_ability_ids_and_order():
    assert [a.value for a in Ability] == ["str", "dex", "con", "int", "wis", "cha"]
    assert Ability("dex") is Ability.DEX


def test_there_are_eighteen_skills_with_stable_ids():
    assert len(Skill) == 18
    assert Skill("sleight_of_hand") is Skill.SLEIGHT_OF_HAND
    assert Skill("animal_handling") is Skill.ANIMAL_HANDLING


def test_skill_ids_are_the_full_stable_list_used_in_saved_characters_and_tool_arguments():
    assert [skill.value for skill in Skill] == [
        "athletics",
        "acrobatics",
        "sleight_of_hand",
        "stealth",
        "arcana",
        "history",
        "investigation",
        "nature",
        "religion",
        "animal_handling",
        "insight",
        "medicine",
        "perception",
        "survival",
        "deception",
        "intimidation",
        "performance",
        "persuasion",
    ]


def test_every_skill_has_ability_and_russian_name():
    for skill in Skill:
        assert isinstance(SKILL_ABILITY[skill], Ability)
        assert SKILL_NAMES_RU[skill].strip()
    assert set(SKILL_ABILITY) == set(Skill)
    assert set(SKILL_NAMES_RU) == set(Skill)


def test_every_ability_has_russian_name():
    assert set(ABILITY_NAMES_RU) == set(Ability)
    assert all(name.strip() for name in ABILITY_NAMES_RU.values())


def test_key_skills_use_expected_abilities():
    assert SKILL_ABILITY[Skill.STEALTH] == Ability.DEX
    assert SKILL_ABILITY[Skill.ATHLETICS] == Ability.STR


def test_skill_ability_table_matches_5e_rules():
    for ability, expected_skills in EXPECTED_SKILLS_BY_ABILITY.items():
        actual = {skill for skill, owner in SKILL_ABILITY.items() if owner == ability}
        assert actual == expected_skills, ability


def test_russian_names_are_unique():
    assert len(set(ABILITY_NAMES_RU.values())) == len(Ability)
    assert len(set(SKILL_NAMES_RU.values())) == len(Skill)


def test_sample_russian_names():
    assert ABILITY_NAMES_RU[Ability.STR] == "Сила"
    assert ABILITY_NAMES_RU[Ability.CON] == "Телосложение"
    assert SKILL_NAMES_RU[Skill.ATHLETICS] == "Атлетика"
    assert SKILL_NAMES_RU[Skill.ACROBATICS] == "Акробатика"
    assert SKILL_NAMES_RU[Skill.PERCEPTION] == "Внимательность"


def test_constant_tables_cannot_be_changed():
    with pytest.raises(TypeError):
        SKILL_ABILITY[Skill.STEALTH] = Ability.STR  # type: ignore[index]
    with pytest.raises(TypeError):
        ABILITY_NAMES_RU[Ability.STR] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        SKILL_NAMES_RU[Skill.STEALTH] = "x"  # type: ignore[index]
