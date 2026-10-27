import copy
import operator
import os
import shutil
import types
from contextlib import suppress
from pathlib import Path
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

import dnd_engine
from dnd_engine import content
from dnd_engine.abilities import Ability, Skill
from dnd_engine.content import (
    MAX_FIELD_LENGTH,
    MAX_PROBLEM_LENGTH,
    MAX_YAML_BYTES,
    ContentError,
    MonsterAction,
    MonsterDef,
    SpellDef,
    SpellEffect,
    SpellSchool,
    SrdContent,
    WeaponProps,
    YamlTooLargeError,
    check_yaml_size,
    load_srd,
    printable,
    quoted,
)

FIXTURES = Path(__file__).parent / "fixtures" / "srd_min"
TABLES = (
    "classes",
    "species",
    "backgrounds",
    "feats",
    "skills",
    "spells",
    "items",
    "monsters",
    "conditions",
)


@pytest.fixture
def srd_dir(tmp_path: Path) -> Path:
    # Копия минимального набора: тест портит её, а не общие фикстуры.
    target = tmp_path / "srd"
    shutil.copytree(FIXTURES, target)
    return target


def edit(srd_dir: Path, table: str, old: str, new: str) -> None:
    """Заменяет ровно одно вхождение `old` в файле таблицы (иначе тест пустой или неоднозначный)."""
    path = srd_dir / f"{table}.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} должно встречаться в {table}.yaml ровно один раз"
    path.write_text(text.replace(old, new), encoding="utf-8")


def load_problems(srd_dir: Path) -> list[str]:
    with pytest.raises(ContentError) as caught:
        load_srd(srd_dir)
    return caught.value.problems


# --- корректный набор ---


def test_fixture_has_a_file_for_every_table():
    assert set(TABLES) == set(SrdContent.model_fields)
    assert {p.stem for p in FIXTURES.glob("*.yaml")} == set(TABLES)


def test_minimal_set_loads():
    srd = load_srd(FIXTURES)

    assert isinstance(srd, SrdContent)
    assert set(srd.classes) == {"fighter", "wizard"}
    assert set(srd.items) == {"dagger", "leather_armor", "healing_potion"}
    assert set(srd.monsters) == {"goblin_warrior", "bandit_captain"}
    assert set(srd.skills) == set(Skill)


def test_dictionary_keys_are_ids():
    srd = load_srd(FIXTURES)

    for table in TABLES:
        for key, record in getattr(srd, table).items():
            assert record.id == key


def test_values_become_typed_models():
    srd = load_srd(FIXTURES)

    wizard = srd.classes["wizard"]
    assert wizard.ability_priority[0] is Ability.INT
    assert wizard.default_skills == (Skill.ARCANA, Skill.HISTORY)
    assert wizard.spellcasting is not None
    assert wizard.spellcasting.slots[3] == {1: 4, 2: 2}
    assert wizard.resources == {"arcane_recovery": {1: 1}}
    assert srd.monsters["bandit_captain"].saves == {Ability.STR: 4, Ability.DEX: 5, Ability.WIS: 2}
    assert srd.items["dagger"].weapon is not None
    assert srd.items["dagger"].weapon.properties == ("finesse", "light", "thrown")
    assert srd.spells["fire_bolt"].school == "evocation"
    assert srd.backgrounds["sage"].tool == "Инструменты каллиграфа"
    assert srd.species["human"].description.startswith("Люди")
    assert srd.skills[Skill.STEALTH].id is Skill.STEALTH
    assert srd.skills[Skill.STEALTH].summary.startswith("Двигаться тихо")


def test_models_are_frozen():
    srd = load_srd(FIXTURES)

    with pytest.raises(ValidationError):
        srd.items["dagger"].name = "Другой кинжал"


# --- лишние поля и неверные значения ---


@pytest.mark.parametrize(
    ("table", "old", "new", "field"),
    [
        ("classes", "  hit_die: 6\n", "  hit_die: 6\n  luck: 7\n", "luck"),
        (
            "classes",
            "      mechanical: true\n  subclass_id: evoker",
            "      mechanical: true\n      cost: 1\n  subclass_id: evoker",
            "cost",
        ),
        ("items", "    range: melee\n", "    range: melee\n    weight: 1\n", "weight"),
        (
            "monsters",
            "      damage_type: piercing\n",
            "      damage_type: piercing\n      aura: 1\n",
            "aura",
        ),
    ],
    ids=["class", "nested-feature", "nested-weapon", "nested-action"],
)
def test_extra_field_is_error(srd_dir, table, old, new, field):
    edit(srd_dir, table, old, new)

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert field in problems[0]
    assert f"{table}.yaml" in problems[0]


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        ("items", "kind: potion", "kind: scroll"),
        ("classes", "saving_throws: [int, wis]", "saving_throws: [int, wisdom]"),
        ("monsters", "scores: {str: 15,", "scores: {strength: 15,"),
        ("classes", "hit_die: 6", "hit_die: много"),
    ],
    ids=["literal", "enum-tuple", "enum-key", "number"],
)
def test_wrong_value_is_error(srd_dir, table, old, new):
    edit(srd_dir, table, old, new)

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert f"{table}.yaml" in problems[0]


def test_problem_names_record_id(srd_dir):
    edit(srd_dir, "classes", "  hit_die: 6\n", "  hit_die: 6\n  luck: 7\n")

    assert "wizard" in load_problems(srd_dir)[0]


# --- дубликаты, формат файлов ---


def test_duplicate_id_is_error(srd_dir):
    with (srd_dir / "conditions.yaml").open("a", encoding="utf-8") as file:
        file.write("- id: prone\n  name: Ещё раз\n  summary: Дубликат.\n")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "conditions.yaml" in problems[0]
    assert "prone" in problems[0]


def test_missing_file_is_error(srd_dir):
    (srd_dir / "spells.yaml").unlink()

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "spells.yaml" in problems[0]


@pytest.mark.parametrize(
    "text",
    ["id: prone\nname: Сбит с ног\n", "", "[prone", "- 5\n", "- name: без id\n", "- id: no\n"],
    ids=["mapping", "empty", "syntax", "not-object", "no-id", "boolean-id"],
)
def test_broken_conditions_file_is_reported_once(srd_dir, text):
    (srd_dir / "conditions.yaml").write_text(text, encoding="utf-8")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "conditions.yaml" in problems[0]


def test_missing_directory_reports_every_table_as_missing(tmp_path):
    with pytest.raises(ContentError) as caught:
        load_srd(tmp_path / "нет-такой-папки")

    problems = caught.value.problems
    assert len(problems) == len(TABLES)
    assert all("файл не найден" in problem for problem in problems)
    assert {problem.split(".yaml")[0] for problem in problems} == set(TABLES)


def test_yaml_syntax_error_names_the_line_where_it_was_found(srd_dir):
    (srd_dir / "conditions.yaml").write_text(
        "- id: prone\n  name: A\n\tsummary: s\n", encoding="utf-8"
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith("conditions.yaml: некорректный YAML (строка 3): ")
    assert "\n" not in problems[0]  # причина и место, без многострочного дампа ошибки
    assert "column" not in problems[0]  # строка уже указана в сообщении, дубль места не нужен


def test_invalid_utf8_is_a_content_problem_with_a_readable_reason(srd_dir):
    (srd_dir / "conditions.yaml").write_bytes(b"- id: prone\n  name: \xff\xfe\n")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "conditions.yaml" in problems[0]
    assert "invalid start byte" in problems[0]


def test_record_numbers_in_problems_count_from_one(srd_dir):
    (srd_dir / "conditions.yaml").write_text(
        "- id: prone\n  name: A\n  summary: s\n- 5\n", encoding="utf-8"
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "запись 2" in problems[0]


def test_optional_fields_default_to_neutral_values():
    effect = SpellEffect(kind="utility")
    action = MonsterAction(id="slam", name="Удар", kind="other")

    assert (effect.targets, effect.repeats, effect.half_on_save) == (1, 1, False)
    assert (effect.attack, effect.save, effect.damage, effect.heal) == (None, None, None, None)
    assert (effect.damage_type, effect.condition, effect.buff) == (None, None, None)
    assert (action.range, action.half_on_save, action.multiattack) == ("melee", False, ())
    assert (action.attack_bonus, action.damage, action.save, action.dc) == (None, None, None, None)


# --- лечение с модификатором и заклинания только вне боя ---


def spell_with(**fields) -> SpellDef:
    base = {
        "id": "test_spell",
        "name": "Тест",
        "name_en": "Test",
        "level": 1,
        "casting_time": "action",
        "range": "near",
        "concentration": False,
        "duration_rounds": None,
        "classes": ("cleric",),
        "school": "abjuration",
        "effect": SpellEffect(kind="heal", heal="2d4"),
        "summary": "s",
    }
    return SpellDef(**(base | fields))


def test_healing_modifier_and_out_of_combat_only_are_off_by_default():
    assert SpellEffect(kind="heal", heal="2d4").heal_modifier is False
    assert spell_with().out_of_combat_only is False


def test_heal_modifier_and_out_of_combat_only_are_accepted():
    effect = SpellEffect(kind="heal", heal="2d8", heal_modifier=True)

    assert effect.heal_modifier is True
    assert spell_with(out_of_combat_only=True).out_of_combat_only is True


def test_spell_flags_are_read_from_yaml(srd_dir):
    edit(
        srd_dir,
        "spells",
        "    damage: 1d4+1\n    damage_type: force\n    repeats: 3\n  upcast: 1d4+1\n",
        "    damage: 1d4+1\n    damage_type: force\n    repeats: 3\n    heal_modifier: true\n"
        "  upcast: 1d4+1\n  out_of_combat_only: true\n",
    )

    missile = load_srd(srd_dir).spells["magic_missile"]

    assert missile.effect.heal_modifier is True
    assert missile.out_of_combat_only is True


# --- подробности для карточек создания героя: описание расы, школа, инструмент, навыки ---

HUMAN_DESCRIPTION = "  description: Люди быстро учатся. Они легко приспосабливаются.\n"
STEALTH_ENTRY = (
    '- id: stealth\n  summary: "Двигаться тихо и прятаться. Например: прокрасться мимо стражи."\n'
)


@pytest.mark.parametrize(
    ("table", "old", "new", "field"),
    [
        ("species", HUMAN_DESCRIPTION, "", "description"),
        ("spells", "  level: 0\n  school: evocation\n", "  level: 0\n", "school"),
        ("backgrounds", "  tool: Инструменты каллиграфа\n", "", "tool"),
        ("skills", STEALTH_ENTRY, "- id: stealth\n", "summary"),
    ],
    ids=["species-description", "spell-school", "background-tool", "skill-summary"],
)
def test_card_detail_fields_are_required(srd_dir, table, old, new, field):
    edit(srd_dir, table, old, new)

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith(f"{table}.yaml, запись ")
    assert f"поле {field}: Field required" in problems[0]


def test_spell_school_is_one_of_the_eight_srd_schools():
    schools = set(get_args(SpellDef.model_fields["school"].annotation))

    assert schools == {
        "abjuration",
        "conjuration",
        "divination",
        "enchantment",
        "evocation",
        "illusion",
        "necromancy",
        "transmutation",
    }
    with pytest.raises(ValidationError):
        spell_with(school="chronomancy")


def test_skill_table_without_a_skill_is_error(srd_dir):
    edit(srd_dir, "skills", STEALTH_ENTRY, "")

    problems = load_problems(srd_dir)

    assert problems == ["skills.yaml: нет записи навыка 'stealth'"]


def test_every_missing_skill_is_reported_at_once(srd_dir):
    (srd_dir / "skills.yaml").write_text(
        "- id: athletics\n  summary: Лазать и прыгать.\n", encoding="utf-8"
    )

    problems = load_problems(srd_dir)

    assert sorted(problems) == sorted(
        f"skills.yaml: нет записи навыка {skill.value!r}"
        for skill in Skill
        if skill is not Skill.ATHLETICS
    )


def test_skill_table_with_a_repeated_skill_is_error(srd_dir):
    with (srd_dir / "skills.yaml").open("a", encoding="utf-8") as file:
        file.write("- id: stealth\n  summary: Ещё раз про скрытность.\n")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith("skills.yaml: дубликат id 'stealth'")


def test_skill_table_with_an_unknown_skill_is_error(srd_dir):
    with (srd_dir / "skills.yaml").open("a", encoding="utf-8") as file:
        file.write("- id: cooking\n  summary: Готовить еду в походе.\n")

    problems = load_problems(srd_dir)

    # Чужой id отвергает схема: и как ключ таблицы, и как поле id записи.
    assert problems
    assert all(p.startswith("skills.yaml, запись 'cooking'") for p in problems), problems


def test_the_public_spell_school_type_is_the_type_of_the_field():
    # Сервер берёт коды школ для своих подписей из `SpellSchool`: перечень должен быть тем же,
    # что принимает поле заклинания.
    assert get_args(SpellSchool) == get_args(SpellDef.model_fields["school"].annotation)


@pytest.mark.parametrize(
    "school", ["chronomancy", "Evocation", "Воплощение"], ids=["unknown", "wrong-case", "label"]
)
def test_a_wrong_spell_school_names_the_spell_and_lists_every_school(srd_dir, school):
    # Русская подпись школы живёт у сервера; в данных допустим только код.
    fire_bolt_school = "  level: 0\n  school: evocation\n"
    edit(srd_dir, "spells", fire_bolt_school, fire_bolt_school.replace("evocation", school))

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith("spells.yaml, запись 'fire_bolt', поле school: ")
    assert all(f"'{code}'" in problems[0] for code in get_args(SpellSchool)), problems[0]


@pytest.mark.parametrize(
    ("table", "old", "new", "where"),
    [
        (
            "backgrounds",
            "  tool: Инструменты каллиграфа\n",
            "  tool:\n",
            "backgrounds.yaml, запись 'sage', поле tool: ",
        ),
        (
            "backgrounds",
            "  tool: Инструменты каллиграфа\n",
            "  tool: [Лютня, Флейта]\n",
            "backgrounds.yaml, запись 'sage', поле tool: ",
        ),
        (
            "species",
            HUMAN_DESCRIPTION,
            "  description: 42\n",
            "species.yaml, запись 'human', поле description: ",
        ),
        (
            "skills",
            STEALTH_ENTRY,
            "- id: stealth\n  summary: [прятаться, красться]\n",
            "skills.yaml, запись 'stealth', поле summary: ",
        ),
    ],
    ids=["tool-without-value", "tool-list", "description-number", "skill-summary-list"],
)
def test_a_card_text_that_is_not_a_string_names_the_file_record_and_field(
    srd_dir, table, old, new, where
):
    # Пустое значение в YAML — это null, а не пустая строка: поле есть, но текста нет.
    edit(srd_dir, table, old, new)

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith(where)
    assert problems[0].endswith("Input should be a valid string")


@pytest.mark.parametrize("line", ["name: Скрытность", "ability: dex"], ids=["name", "ability"])
def test_a_skill_record_has_no_name_or_ability_of_its_own(srd_dir, line):
    # Название и характеристика навыка — в abilities.py; вторая копия в skills.yaml могла бы
    # разойтись с ней, поэтому схема такие поля не принимает.
    edit(srd_dir, "skills", "- id: stealth\n", f"- id: stealth\n  {line}\n")

    problems = load_problems(srd_dir)

    field = line.split(":")[0]
    assert problems == [
        f"skills.yaml, запись 'stealth', поле {field}: Extra inputs are not permitted"
    ]


def test_a_skill_id_in_another_case_is_an_unknown_skill_and_not_a_missing_one(srd_dir):
    edit(srd_dir, "skills", "- id: stealth\n", "- id: Stealth\n")

    problems = load_problems(srd_dir)

    assert problems
    assert all(p.startswith("skills.yaml, запись 'Stealth', поле ") for p in problems), problems
    # Полноту таблицы проверяют только при верной форме: ложной жалобы на пропуск нет.
    assert not any("нет записи навыка" in p for p in problems), problems


def test_a_missing_skill_file_is_one_problem_and_not_one_per_skill(srd_dir):
    (srd_dir / "skills.yaml").unlink()

    assert load_problems(srd_dir) == ["skills.yaml: файл не найден"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "ожидался список объектов"),
        ("id: stealth\nsummary: Тихо.\n", "ожидался список объектов"),
        ("[stealth", "некорректный YAML"),
    ],
    ids=["empty", "mapping", "syntax"],
)
def test_a_broken_skill_file_is_one_problem_and_not_one_per_skill(srd_dir, text, reason):
    (srd_dir / "skills.yaml").write_text(text, encoding="utf-8")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith(f"skills.yaml: {reason}")


def test_a_missing_skill_and_a_dangling_reference_are_reported_together(srd_dir):
    edit(srd_dir, "skills", STEALTH_ENTRY, "")
    edit(srd_dir, "backgrounds", "feat: magic_initiate", "feat: lucky")

    problems = load_problems(srd_dir)

    assert sorted(problems) == sorted(
        [
            "skills.yaml: нет записи навыка 'stealth'",
            "backgrounds.yaml, запись 'sage': feat ссылается на несуществующий id 'lucky'",
        ]
    )


def test_the_skill_table_is_keyed_by_skill_and_found_by_its_code():
    skills = load_srd(FIXTURES).skills

    assert all(type(key) is Skill for key in skills)
    # Сервер и движок ищут навык и по `Skill`, и по коду строкой — это одна запись.
    assert skills["stealth"] is skills[Skill.STEALTH]


# --- защиты и вторичный урон монстров ---


def rat_statblock(**overrides) -> MonsterDef:
    fields = {
        "id": "rat",
        "name": "Крыса",
        "name_en": "Rat",
        "cr": 0,
        "xp": 10,
        "ac": 10,
        "hp": 1,
        "hp_dice": "1d4",
        "speed": 20,
        "scores": dict.fromkeys(Ability, 10),
        "actions": (),
    }
    return MonsterDef(**(fields | overrides))


def test_monster_has_no_vulnerabilities_and_action_no_secondary_damage_by_default():
    action = MonsterAction(id="bite", name="Укус", kind="attack")

    assert rat_statblock().vulnerabilities == ()
    assert (action.secondary_damage, action.secondary_damage_type) == (None, None)
    assert action.reach is False


def test_vulnerabilities_are_a_tuple_of_damage_types():
    monster = rat_statblock(vulnerabilities=["bludgeoning"])

    assert monster.vulnerabilities == ("bludgeoning",)


@pytest.mark.parametrize(
    "extra",
    [{"secondary_damage": "1d6"}, {"secondary_damage_type": "necrotic"}],
    ids=["кости без типа", "тип без костей"],
)
def test_secondary_damage_needs_both_dice_and_type(extra):
    with pytest.raises(ValidationError, match="вторичн"):
        MonsterAction(id="bite", name="Укус", kind="attack", **extra)


def test_secondary_damage_with_dice_and_type_is_accepted():
    action = MonsterAction(
        id="bite", name="Укус", kind="attack", secondary_damage="1d6", secondary_damage_type="fire"
    )

    assert (action.secondary_damage, action.secondary_damage_type) == ("1d6", "fire")


def test_new_monster_fields_are_read_from_yaml(srd_dir):
    edit(
        srd_dir,
        "monsters",
        "  scores: {str: 8, dex: 15, con: 10, int: 10, wis: 8, cha: 8}\n",
        "  scores: {str: 8, dex: 15, con: 10, int: 10, wis: 8, cha: 8}\n"
        "  vulnerabilities: [fire]\n",
    )
    edit(
        srd_dir,
        "monsters",
        "      damage_type: slashing\n    - id: shortbow",
        "      damage_type: slashing\n      secondary_damage: 1d4\n"
        "      secondary_damage_type: cold\n      reach: true\n    - id: shortbow",
    )

    goblin = load_srd(srd_dir).monsters["goblin_warrior"]

    scimitar = goblin.actions[0]
    assert goblin.vulnerabilities == ("fire",)
    assert (scimitar.secondary_damage, scimitar.secondary_damage_type) == ("1d4", "cold")
    assert scimitar.reach is True


def test_a_monster_action_hits_one_target_unless_the_data_says_otherwise():
    assert MonsterAction(id="web", name="Паутина", kind="save").targets == 1
    assert MonsterAction(id="breath", name="Дыхание", kind="save", targets=6).targets == 6


@pytest.mark.parametrize("targets", [0, -1])
def test_a_monster_action_cannot_have_fewer_than_one_target(targets):
    with pytest.raises(ValidationError, match="targets"):
        MonsterAction(id="breath", name="Дыхание", kind="save", targets=targets)


def test_the_number_of_targets_is_read_from_yaml(srd_dir):
    edit(
        srd_dir,
        "monsters",
        "      damage_type: slashing\n    - id: shortbow",
        "      damage_type: slashing\n    - id: breath\n      name: Дыхание\n      kind: save\n"
        "      save: dex\n      dc: 11\n      targets: 4\n    - id: shortbow",
    )

    breath = next(
        a for a in load_srd(srd_dir).monsters["goblin_warrior"].actions if a.id == "breath"
    )

    assert breath.targets == 4


def test_yaml_with_secondary_dice_but_no_type_is_a_content_error(srd_dir):
    edit(
        srd_dir,
        "monsters",
        "      damage_type: slashing\n    - id: shortbow",
        "      damage_type: slashing\n      secondary_damage: 1d4\n    - id: shortbow",
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "monsters.yaml" in problems[0] and "вторичн" in problems[0]


# --- безопасность YAML ---


def test_python_tag_is_rejected_and_not_executed(srd_dir, tmp_path):
    marker = tmp_path / "executed"
    payload = f'- !!python/object/apply:os.mkdir ["{marker.as_posix()}"]\n'
    (srd_dir / "conditions.yaml").write_text(payload, encoding="utf-8")

    problems = load_problems(srd_dir)

    assert not marker.exists()
    assert len(problems) == 1
    assert "conditions.yaml" in problems[0]


def test_yaml_anchors_and_aliases_in_a_table_are_a_content_problem(srd_dir):
    # Якорь и алиас раздули бы данные при проверке схемы (бомба алиасов), поэтому запрещены целиком.
    (srd_dir / "conditions.yaml").write_text(
        "- &a {id: prone, name: A, summary: s}\n- *a\n", encoding="utf-8"
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith("conditions.yaml: некорректный YAML (строка 1): ")
    assert "якор" in problems[0] and "алиас" in problems[0]


def test_a_table_over_the_size_limit_is_a_content_problem_though_its_yaml_is_valid(srd_dir):
    path = srd_dir / "conditions.yaml"
    path.write_bytes(path.read_bytes() + b"#" * MAX_YAML_BYTES)

    problems = load_problems(srd_dir)

    assert problems == [f"conditions.yaml: файл больше {MAX_YAML_BYTES // 1024} КБ"]


def test_a_table_of_exactly_the_size_limit_is_read(srd_dir):
    path = srd_dir / "conditions.yaml"
    body = path.read_bytes()
    path.write_bytes(body + b"#" * (MAX_YAML_BYTES - len(body)))

    assert set(load_srd(srd_dir).conditions) == {"prone", "poisoned"}


def test_the_builtin_srd_tables_fit_the_size_limit_with_a_wide_margin():
    sizes = [
        file.stat().st_size
        for file in (Path(dnd_engine.__file__).parent / "data" / "srd").glob("*.yaml")
    ]

    assert len(sizes) == len(TABLES) and max(sizes) * 10 < MAX_YAML_BYTES


def test_a_long_yaml_tag_in_a_table_is_cut_in_the_problem(srd_dir):
    (srd_dir / "conditions.yaml").write_text("- !" + "t" * 100_000 + " x\n", encoding="utf-8")

    problems = load_problems(srd_dir)

    assert len(problems) == 1 and len(problems[0]) == MAX_PROBLEM_LENGTH
    assert problems[0].startswith("conditions.yaml: некорректный YAML (строка 1): ")
    assert problems[0].endswith("…")


def test_control_characters_and_length_of_a_schema_problem_are_neutralized(srd_dir):
    # Лишнее поле записи попадает в проблему по имени: имя из файла — чужой текст.
    (srd_dir / "conditions.yaml").write_text(
        '- id: prone\n  name: A\n  summary: s\n  "\\e[2K\\a\\r\\n": 1\n  ? '
        + "k" * 100_000
        + "\n  : 1\n",
        encoding="utf-8",
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 2
    assert all(len(problem) <= MAX_PROBLEM_LENGTH and problem.isprintable() for problem in problems)
    assert "\\x1b[2K\\x07\\r\\n" in problems[0]


# --- чтение YAML с пределом размера и безопасный текст проблем ---


class SizedSource:
    """Файл, который заявляет размер, но целиком читать его нельзя."""

    name = "huge.yaml"

    def __init__(self, size: int) -> None:
        self.size = size

    def stat(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(st_size=self.size)

    def is_file(self) -> bool:
        return True

    def read_bytes(self) -> bytes:
        raise AssertionError("файл больше предела прочитан целиком")


def test_a_huge_file_is_refused_by_its_size_before_it_is_read():
    with pytest.raises(YamlTooLargeError, match=f"файл больше {MAX_YAML_BYTES // 1024} КБ"):
        check_yaml_size(SizedSource(10 * 1024**3))


def test_a_file_of_exactly_the_limit_passes_the_size_check():
    check_yaml_size(SizedSource(MAX_YAML_BYTES))


def test_the_size_check_of_a_missing_file_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_yaml_size(tmp_path / "missing.yaml")


def test_the_size_check_refuses_a_folder_with_a_clear_reason(tmp_path):
    with pytest.raises(OSError, match="не обычный файл") as caught:
        check_yaml_size(tmp_path)

    assert not isinstance(caught.value, FileNotFoundError)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="нет os.mkfifo")
def test_the_size_check_refuses_a_pipe_though_it_has_no_size(tmp_path):
    pipe = tmp_path / "table.yaml"
    os.mkfifo(pipe)

    with pytest.raises(OSError, match="не обычный файл"):
        check_yaml_size(pipe)


def test_a_folder_named_like_a_table_is_a_content_problem_not_a_crash(srd_dir):
    (srd_dir / "conditions.yaml").unlink()
    (srd_dir / "conditions.yaml").mkdir()

    problems = load_problems(srd_dir)

    assert problems == ["conditions.yaml: не удалось прочитать файл (не обычный файл)"]


# `yaml.safe_load` на чужом вводе бросает не только `YAMLError`: конструкторы значений падают чем
# попало (`!!int ""` — `IndexError`, `!!bool x` — `KeyError`, длинное число — `ValueError`),
# глубокая вложенность — `RecursionError`. Любая беда данных обязана стать `ContentError` с путём
# файла и фиксированной причиной, без значения из файла (как у загрузчика приключения).

SECRET_VALUE = "секрет-из-файла"
UNBUILDABLE_VALUES = {
    "вложенность": ("[" * 5000 + "]" * 5000, "слишком глубокая вложенность"),
    "int-пустая-строка": ('- !!int ""\n', "значение не построить"),
    "int-не-число": (f"- !!int {SECRET_VALUE}\n", "значение не построить"),
    "bool-не-литерал": (f"- !!bool {SECRET_VALUE}\n", "значение не построить"),
    "число-5000-цифр": ("- " + "9" * 5000 + "\n", "значение не построить"),
    "несуществующая-дата": ("- 2023-02-30\n", "значение не построить"),
    "timestamp-не-дата": (f"- !!timestamp {SECRET_VALUE}\n", "значение не построить"),
}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize(
    ("text", "reason"), UNBUILDABLE_VALUES.values(), ids=UNBUILDABLE_VALUES.keys()
)
def test_a_value_yaml_cannot_build_is_a_content_problem_without_the_value(
    srd_dir, table, text, reason
):
    (srd_dir / f"{table}.yaml").write_text(text, encoding="utf-8")

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert problems[0].startswith(f"{table}.yaml: {reason}")
    assert SECRET_VALUE not in problems[0] and "9" * 20 not in problems[0]


@pytest.mark.parametrize(
    "error",
    [
        IndexError(SECRET_VALUE),
        KeyError(SECRET_VALUE),
        AttributeError(SECRET_VALUE),
        ValueError(SECRET_VALUE),
        OverflowError(SECRET_VALUE),
        TypeError(SECRET_VALUE),
    ],
    ids=lambda error: type(error).__name__,
)
def test_the_reason_for_an_unexpected_parse_error_is_one_fixed_text_that_never_repeats_the_error(
    error,
):
    reason = content.describe_parse_failure(error)

    assert SECRET_VALUE not in reason and type(error).__name__ not in reason
    assert reason == content.describe_parse_failure(RuntimeError("другая беда"))
    assert reason.startswith("значение не построить")


def test_the_reason_for_too_deep_nesting_is_its_own_and_keeps_no_text_of_the_error():
    reason = content.describe_parse_failure(RecursionError(SECRET_VALUE))

    assert reason == "слишком глубокая вложенность"


@pytest.mark.parametrize(
    ("raw", "line", "reason"),
    [
        ("ключ: [1, 2\nсекрет: значение: ещё\n", 2, "expected ',' or ']'"),
        ("a: b: c\n", 1, "mapping values are not allowed here"),
        ("a: &x 1\nb: *x\n", 1, "якоря и алиасы YAML не поддерживаются"),
    ],
    ids=["парсер", "сканер", "алиас"],
)
def test_the_reason_for_a_broken_yaml_names_the_line_and_the_cause_but_not_the_document(
    raw, line, reason
):
    with pytest.raises(yaml.YAMLError) as caught:
        content.parse_yaml(raw.encode())

    described = content.describe_parse_failure(caught.value)

    assert described.startswith(f"некорректный YAML (строка {line}): ")
    assert reason in described
    assert "секрет" not in described  # слов документа в причине нет


LONG_ID = "i" * 300


def test_a_long_duplicate_id_does_not_eat_the_reason(srd_dir):
    with (srd_dir / "conditions.yaml").open("a", encoding="utf-8") as file:
        file.write(f"- id: {LONG_ID}\n  name: A\n  summary: s\n" * 2)

    problems = load_problems(srd_dir)

    assert len(problems) == 1 and len(problems[0]) <= MAX_PROBLEM_LENGTH
    assert problems[0].startswith("conditions.yaml: дубликат id 'iii") and "запись 4" in problems[0]


def test_a_long_id_of_a_broken_record_does_not_eat_the_reason(srd_dir):
    with (srd_dir / "conditions.yaml").open("a", encoding="utf-8") as file:
        file.write(f"- id: {LONG_ID}\n  name: A\n")

    problems = load_problems(srd_dir)

    assert len(problems) == 1 and len(problems[0]) <= MAX_PROBLEM_LENGTH
    assert problems[0].startswith("conditions.yaml, запись 'iii")
    assert "поле summary" in problems[0] and "Field required" in problems[0]


def test_printable_shows_non_printable_characters_as_escapes():
    text = "a\x1b[2K\x07\r\n\t\x85\u2028\u202e\ud800b"

    assert printable(text) == "a\\x1b[2K\\x07\\r\\n\\t\\x85\\u2028\\u202e\\ud800b"


def test_printable_leaves_readable_text_alone():
    text = "Склеп у деревни — «долг» 🦴 ё, tab-free."

    assert printable(text, 200) == text


def test_printable_leaves_text_within_the_limit_as_it_is():
    assert printable("x" * 10, 10) == "x" * 10


def test_printable_cuts_longer_text_to_the_limit_with_an_ellipsis():
    assert printable("x" * 11, 10) == "x" * 9 + "…"


def test_printable_counts_the_limit_after_escaping():
    result = printable("\x1b" * 300, 200)

    assert len(result) == 200 and result.endswith("…") and result.startswith("\\x1b\\x1b")


def test_quoted_shows_a_value_as_repr_does():
    assert quoted("prone") == "'prone'"
    assert quoted("Сбит с ног") == "'Сбит с ног'"
    assert quoted("it's") == '"it\'s"'
    assert quoted(7) == "7"


def test_quoted_shows_non_printable_characters_as_escapes():
    assert quoted("a\x1b[2K\r\n\u2028\u202e") == "'a\\x1b[2K\\r\\n\\u2028\\u202e'"


def test_quoted_cuts_a_long_value_to_the_field_limit_with_an_ellipsis():
    shown = quoted("x" * 5000)

    assert len(shown) == MAX_FIELD_LENGTH
    assert shown.startswith("'xxx") and shown.endswith("…")


def test_quoted_leaves_a_value_of_the_longest_legal_id_whole():
    assert quoted("a" * 64) == "'" + "a" * 64 + "'"


# --- перекрёстные ссылки ---


@pytest.mark.parametrize(
    ("table", "old", "new", "expected"),
    [
        (
            "classes",
            "starting_equipment: [dagger]",
            "starting_equipment: [dagger, ghost_item]",
            ("wizard", "starting_equipment ссылается на несуществующий id 'ghost_item'"),
        ),
        (
            "classes",
            "starting_equipment: [leather_armor, healing_potion]\n  default_background: sage",
            "starting_equipment: [leather_armor, healing_potion]\n  default_background: hermit",
            ("fighter", "default_background ссылается на несуществующий id 'hermit'"),
        ),
        (
            "backgrounds",
            "equipment: [dagger]",
            "equipment: [dagger, lost_book]",
            ("sage", "equipment ссылается на несуществующий id 'lost_book'"),
        ),
        (
            "backgrounds",
            "feat: magic_initiate",
            "feat: lucky",
            ("sage", "feat ссылается на несуществующий id 'lucky'"),
        ),
        (
            "spells",
            "classes: [wizard]\n  effect:\n    kind: attack",
            "classes: [wizard, sorcerer]\n  effect:\n    kind: attack",
            ("fire_bolt", "classes ссылается на несуществующий id 'sorcerer'"),
        ),
        (
            "classes",
            "default_spells: [fire_bolt, magic_missile]",
            "default_spells: [fire_bolt, magic_missile, wish]",
            ("wizard", "default_spells ссылается на несуществующий id 'wish'"),
        ),
        (
            "classes",
            "  fighting_styles: [archery, defense]\n",
            "  fighting_styles: [archery, defense]\n  default_spells: [fire_bolt]\n",
            ("fighter", "default_spells содержит 'fire_bolt' не из списка класса"),
        ),
        (
            "monsters",
            "multiattack: [scimitar, scimitar]",
            "multiattack: [scimitar, shortbow]",
            ("bandit_captain", "multiattack ссылается на несуществующий id 'shortbow'"),
        ),
    ],
    ids=[
        "starting-item",
        "default-background",
        "background-item",
        "background-feat",
        "spell-class",
        "default-spell-unknown",
        "default-spell-without-spellcasting",
        "multiattack-of-other-monster",
    ],
)
def test_dangling_reference_is_error(srd_dir, table, old, new, expected):
    edit(srd_dir, table, old, new)

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert all(fragment in problems[0] for fragment in expected), problems[0]


def test_default_spell_from_other_class_list_is_error(srd_dir):
    # Заклинание существует, но в списке волшебника его нет.
    edit(
        srd_dir,
        "spells",
        "classes: [wizard]\n  effect:\n    kind: auto",
        "classes: []\n  effect:\n    kind: auto",
    )

    problems = load_problems(srd_dir)

    assert len(problems) == 1
    assert "wizard" in problems[0]
    assert "magic_missile" in problems[0]


# --- все проблемы сразу ---


def test_all_reference_problems_are_collected_at_once(srd_dir):
    edit(srd_dir, "classes", "starting_equipment: [dagger]", "starting_equipment: [ghost_item]")
    edit(srd_dir, "backgrounds", "feat: magic_initiate", "feat: lucky")
    edit(srd_dir, "monsters", "multiattack: [scimitar, scimitar]", "multiattack: [shortbow]")
    edit(
        srd_dir,
        "spells",
        "classes: [wizard]\n  effect:\n    kind: attack",
        "classes: [bard]\n  effect:\n    kind: attack",
    )

    caught = load_problems(srd_dir)

    assert len(caught) == 5  # плюс default_spells волшебника: fire_bolt больше не в его списке
    for fragment in ("ghost_item", "lucky", "shortbow", "bard", "fire_bolt"):
        assert any(fragment in problem for problem in caught), fragment


def test_structure_problems_from_different_files_are_collected_at_once(srd_dir):
    edit(srd_dir, "classes", "  hit_die: 6\n", "  hit_die: 6\n  luck: 7\n")
    (srd_dir / "feats.yaml").write_text(
        "- id: alert\n  name: A\n  summary: s\n  mechanical: true\n"
        "- id: alert\n  name: B\n  summary: s\n  mechanical: true\n",
        encoding="utf-8",
    )
    (srd_dir / "monsters.yaml").unlink()
    (srd_dir / "species.yaml").write_text("[broken", encoding="utf-8")

    caught = load_problems(srd_dir)

    assert len(caught) == 4
    for fragment in ("luck", "alert", "monsters.yaml", "species.yaml"):
        assert any(fragment in problem for problem in caught), fragment


def test_references_are_not_checked_while_structure_is_broken(srd_dir):
    # Предмет не прошёл проверку формы, поэтому его нет в таблице; но «ссылка в никуда»
    # на него была бы ложной жалобой — сообщается только настоящая проблема.
    edit(srd_dir, "items", "    range: melee\n", "    range: melee\n    weight: 1\n")

    caught = load_problems(srd_dir)

    assert len(caught) == 1
    assert "weight" in caught[0]


def test_error_text_lists_every_problem(srd_dir):
    edit(srd_dir, "classes", "starting_equipment: [dagger]", "starting_equipment: [ghost_item]")
    edit(srd_dir, "backgrounds", "feat: magic_initiate", "feat: lucky")

    with pytest.raises(ContentError) as caught:
        load_srd(srd_dir)

    text = str(caught.value)
    assert all(problem in text for problem in caught.value.problems)
    assert len(caught.value.problems) == 2


# --- путь по умолчанию и кеш ---


def test_explicit_path_is_not_cached(srd_dir):
    assert isinstance(load_srd(srd_dir), SrdContent)
    edit(srd_dir, "classes", "starting_equipment: [dagger]", "starting_equipment: [ghost_item]")

    with pytest.raises(ContentError):
        load_srd(srd_dir)


def test_builtin_data_lives_next_to_the_package_code():
    expected = Path(dnd_engine.__file__).parent / "data" / "srd"

    assert Path(str(content._builtin_srd_dir())) == expected


@pytest.fixture
def builtin_is_fixture(monkeypatch):
    # Подменяем папку встроенных данных, чтобы тест не зависел от того, есть ли они в пакете.
    monkeypatch.setattr(content, "_builtin_srd_dir", lambda: FIXTURES)
    content._load_builtin_srd.cache_clear()
    yield
    content._load_builtin_srd.cache_clear()


def test_load_without_path_reads_builtin_data(builtin_is_fixture):
    assert load_srd() == load_srd(FIXTURES)


def test_load_without_path_is_cached(builtin_is_fixture):
    assert load_srd() is load_srd()


def test_error_message_is_a_header_with_one_line_per_problem():
    error = ContentError(["первая проблема", "вторая проблема"])

    assert str(error).splitlines() == [
        "Данные SRD некорректны:",
        "- первая проблема",
        "- вторая проблема",
    ]
    assert error.problems == ["первая проблема", "вторая проблема"]


def test_spell_range_vocabulary_covers_self_touch_and_the_three_weapon_ranges():
    spell_ranges = set(get_args(SpellDef.model_fields["range"].annotation))
    weapon_ranges = set(get_args(WeaponProps.model_fields["range"].annotation))

    assert spell_ranges == {"self", "touch", "engaged", "near", "far"}
    assert weapon_ranges == {"melee", "near", "far"}


# --- данные только для чтения ---

# Попытки изменить данные SRD: кеш `load_srd()` общий, поэтому любая удавшаяся правка
# испортила бы правила для всех игр процесса.
MUTATIONS = {
    "добавить запись в таблицу": lambda srd: srd.classes.__setitem__(
        "ghost", srd.classes["fighter"]
    ),
    "дополнить таблицу через |=": lambda srd: operator.ior(srd.items, {}),
    "вложенная таблица ячеек": lambda srd: (
        srd.classes["wizard"].spellcasting.slots[3].update({1: 99})
    ),
    "характеристики монстра": lambda srd: srd.monsters["goblin_warrior"].scores.__setitem__(
        Ability.DEX, 30
    ),
    "ресурсы класса": lambda srd: srd.classes["fighter"].resources.clear(),
    "подменить запись навыка": lambda srd: srd.skills.__setitem__(
        Skill.STEALTH, srd.skills[Skill.ATHLETICS]
    ),
    # Разрушающие правки идут последними: иначе следующие падали бы KeyError, а не проверкой.
    "удалить запись из таблицы": lambda srd: srd.items.__delitem__("dagger"),
    "очистить таблицу": lambda srd: srd.monsters.clear(),
    "убрать навык из таблицы": lambda srd: srd.skills.pop(Skill.STEALTH),
}


@pytest.mark.parametrize("mutate", MUTATIONS.values(), ids=MUTATIONS.keys())
def test_srd_data_rejects_every_mutation(mutate):
    srd = load_srd(FIXTURES)

    with pytest.raises(TypeError):
        mutate(srd)


@pytest.mark.parametrize("table", TABLES)
def test_every_table_refuses_a_new_record(table):
    # Защита общая для всех таблиц, в том числе для новой таблицы навыков.
    records = getattr(load_srd(FIXTURES), table)
    key, record = next(iter(records.items()))

    with pytest.raises(TypeError):
        records[f"{key}_copy"] = record


# Новые поля карточек создания героя: модели неизменяемые, поэтому присвоение отвергается.
CARD_FIELD_EDITS = {
    "описание расы": lambda srd: setattr(srd.species["human"], "description", "Другое."),
    "школа заклинания": lambda srd: setattr(srd.spells["fire_bolt"], "school", "illusion"),
    "инструмент предыстории": lambda srd: setattr(srd.backgrounds["sage"], "tool", "Лютня"),
    "описание навыка": lambda srd: setattr(srd.skills[Skill.STEALTH], "summary", "Другое."),
    "таблица навыков целиком": lambda srd: setattr(srd, "skills", {}),
}


@pytest.mark.parametrize("change", CARD_FIELD_EDITS.values(), ids=CARD_FIELD_EDITS.keys())
def test_card_detail_fields_cannot_be_reassigned(change):
    srd = load_srd(FIXTURES)

    with pytest.raises(ValidationError):
        change(srd)
    assert srd == load_srd(FIXTURES)


def test_dict_fields_with_defaults_are_read_only_too():
    scores = dict.fromkeys(Ability, 10)
    monster = MonsterDef(
        id="rat",
        name="Крыса",
        name_en="Rat",
        cr=0,
        xp=10,
        ac=10,
        hp=1,
        hp_dice="1d4",
        speed=20,
        scores=scores,
        actions=(),
    )

    with pytest.raises(TypeError):
        monster.saves[Ability.STR] = 5  # значение по умолчанию тоже защищено
    scores[Ability.STR] = 20  # словарь вызывающего кода не должен просочиться в модель
    assert monster.scores[Ability.STR] == 10


def test_cached_srd_equals_a_fresh_load_after_mutation_attempts(builtin_is_fixture):
    shared = load_srd()

    for mutate in MUTATIONS.values():
        with suppress(TypeError):
            mutate(shared)

    fresh = load_srd(FIXTURES)
    assert load_srd() == fresh
    assert load_srd().model_dump_json() == fresh.model_dump_json()


def test_copies_of_srd_data_equal_the_original_and_stay_read_only():
    srd = load_srd(FIXTURES)

    for clone in (copy.deepcopy(srd), srd.model_copy(deep=True)):
        assert clone == srd
        with pytest.raises(TypeError):
            clone.classes.clear()


def test_dumping_srd_data_gives_plain_dicts_that_callers_may_change():
    dumped = load_srd(FIXTURES).model_dump()

    assert type(dumped["classes"]) is dict
    assert type(dumped["classes"]["wizard"]["spellcasting"]["slots"][3]) is dict
    dumped["classes"].clear()  # копия для вызывающего кода, данные SRD это не затрагивает
