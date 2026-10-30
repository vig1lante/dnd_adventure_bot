"""Схема приключения: форма данных, таблица Сл, id сущностей, неизменяемость.

Ссылочную целостность (несуществующие id, достижимость) схема не проверяет — это валидатор.
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from dnd_engine.abilities import Ability, Skill
from dnd_engine.adventure.schema import (
    Adventure,
    CheckSpec,
    Encounter,
    Exit,
    Feature,
    Item,
    Location,
    Npc,
    Revelation,
    is_entity_id,
)
from dnd_engine.checks import VALID_DCS
from dnd_engine.content import load_srd

FieldPath = tuple[str | int, ...]


def minimal_data() -> dict[str, Any]:
    """Минимальное приключение «сырыми» данными: по одной сущности каждого вида."""
    return {
        "id": "demo-crypt",
        "title": "Склеп у деревни",
        "pitch": "Пропал смотритель кладбища.",
        "tone": "мрачная сказка",
        "target_hours": 1.5,
        "rules": {"modules": ["campfire"]},
        "start_location": "village",
        "finale_location": "crypt",
        "acts": [
            {"id": "act-1", "title": "Деревня", "summary": "Завязка", "key_scenes": ["трактир"]}
        ],
        "endings": ["Смотритель спасён"],
        "locations": {
            "village": {
                "id": "village",
                "name": "Деревня",
                "read_aloud": "Вы входите в притихшую деревню.",
                "exits": [
                    {
                        "id": "to-crypt",
                        "to": "crypt",
                        "direction": "север",
                        "description": "Тропа к склепу.",
                        "locked_by": "item:key",
                        "hidden_dc": 15,
                    }
                ],
                "features": [
                    {
                        "id": "diary",
                        "name": "Дневник",
                        "description": "Потрёпанный дневник смотрителя.",
                        "check": {"ability": "int", "skill": "investigation", "dc": 15},
                        "reveals": [{"kind": "clue", "id": "clue-1", "value": True}],
                    }
                ],
                "zones": [{"id": "here", "name": "здесь"}, {"id": "yard", "name": "двор"}],
                "links": [{"a": "here", "b": "yard", "distance": "near"}],
                "encounter_id": "bones",
            },
            "crypt": {"id": "crypt", "name": "Склеп", "read_aloud": "Холод и тишина."},
        },
        "npcs": {
            "marta": {
                "id": "marta",
                "name": "Марта",
                "role": "трактирщица",
                "appearance": "Крепкая женщина с усталым взглядом.",
                "manner": "говорит коротко",
                "motive": "найти брата",
                "knows": ["где дневник"],
                "attitude": 1,
                "relations": [{"npc_id": "fabian", "kind": "enemy", "note": "должник брата"}],
                "location_id": "village",
                "statblock": None,
            }
        },
        "items": {
            "key": {
                "id": "key",
                "name": "Ржавый ключ",
                "description": "Открывает склеп.",
                "holder": "location:village",
            }
        },
        "encounters": {
            "bones": {
                "id": "bones",
                "location_id": "village",
                "trigger": "игроки ступили на кладбище",
                "tactics": "скелеты идут к ближайшему",
                "monsters": [{"monster_id": "skeleton", "count": 3, "zone": "here"}],
            }
        },
        "revelations": {
            "fabian": {
                "id": "fabian",
                "statement": "Смотрителя похитил Фабиан.",
                "clues": [
                    {"id": "clue-1", "description": "Запись о долге.", "where": "feature:diary"}
                ],
            }
        },
    }


def with_value(path: FieldPath, value: Any) -> dict[str, Any]:
    """Минимальные данные с одним изменённым значением по пути из ключей и индексов."""
    data = minimal_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def error_types(exc_info: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {error["type"] for error in exc_info.value.errors()}


# --- сборка в памяти ---


def test_minimal_adventure_builds_in_memory():
    adventure = Adventure.model_validate(minimal_data())

    assert adventure.id == "demo-crypt"
    assert adventure.party_level == 3
    assert adventure.party_size == 6
    assert adventure.monsters == {}
    assert adventure.seeds == ()
    village = adventure.locations["village"]
    assert isinstance(village, Location)
    assert village.exits[0].hidden_dc == 15
    assert village.features[0].check == CheckSpec(
        ability=Ability.INT, skill=Skill.INVESTIGATION, dc=15
    )
    assert village.features[0].reveals[0].value is True
    assert [zone.id for zone in village.zones] == ["here", "yard"]
    assert isinstance(adventure.npcs["marta"], Npc)
    assert isinstance(adventure.items["key"], Item)
    assert isinstance(adventure.encounters["bones"], Encounter)
    assert isinstance(adventure.revelations["fabian"], Revelation)


def test_optional_fields_have_defaults():
    location = Location(id="gate", name="Ворота", read_aloud="Ворота.")

    assert location.exits == ()
    assert location.features == ()
    assert location.encounter_id is None
    assert [zone.id for zone in location.zones] == ["here"]
    assert location.links == ()


def test_custom_monsters_are_typed_stat_blocks():
    skeleton = load_srd().monsters["skeleton"]
    data = minimal_data()
    data["monsters"] = {
        "crypt-bones": skeleton.model_copy(update={"id": "crypt-bones"}).model_dump()
    }

    adventure = Adventure.model_validate(data)

    assert adventure.monsters["crypt-bones"].hp == skeleton.hp


def test_schema_does_not_check_references():
    # Ссылки на несуществующие id — работа валидатора, а не формы данных.
    data = with_value(("locations", "village", "exits", 0, "to"), "nowhere")

    adventure = Adventure.model_validate(data)

    assert adventure.locations["village"].exits[0].to == "nowhere"


def test_model_dump_roundtrips_through_json():
    adventure = Adventure.model_validate(minimal_data())

    assert Adventure.model_validate_json(adventure.model_dump_json()) == adventure


# --- extra="forbid" ---

EXTRA_FIELD_PATHS: list[FieldPath] = [
    (),
    ("rules",),
    ("acts", 0),
    ("locations", "village"),
    ("locations", "village", "exits", 0),
    ("locations", "village", "features", 0),
    ("locations", "village", "features", 0, "check"),
    ("locations", "village", "features", 0, "reveals", 0),
    ("locations", "village", "zones", 0),
    ("locations", "village", "links", 0),
    ("npcs", "marta"),
    ("npcs", "marta", "relations", 0),
    ("items", "key"),
    ("encounters", "bones"),
    ("encounters", "bones", "monsters", 0),
    ("revelations", "fabian"),
    ("revelations", "fabian", "clues", 0),
]


@pytest.mark.parametrize("path", EXTRA_FIELD_PATHS, ids=lambda p: "/".join(map(str, p)) or "root")
def test_extra_field_is_rejected_everywhere(path: FieldPath):
    data = minimal_data()
    target: Any = data
    for key in path:
        target = target[key]
    target["surprise"] = 1

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


def test_typo_in_rules_does_not_silently_drop_modules():
    # RulesRef сам лишние поля не запрещает: без защиты `module` вместо `modules` терял бы данные.
    data = with_value(("rules",), {"module": ["campfire"]})

    with pytest.raises(ValidationError, match="module"):
        Adventure.model_validate(data)


# --- Сл только из таблицы ---


@pytest.mark.parametrize("dc", sorted(VALID_DCS))
def test_check_accepts_table_dc(dc: int):
    assert CheckSpec(ability=Ability.STR, dc=dc).dc == dc


@pytest.mark.parametrize("dc", [0, 1, 12, 13, 14, 16, 31, 35, -5, True])
def test_check_rejects_dc_outside_table(dc: int):
    with pytest.raises(ValidationError, match="5, 10, 15, 20, 25, 30"):
        CheckSpec(ability=Ability.STR, dc=dc)


def test_check_dc_13_in_adventure_is_error():
    data = with_value(("locations", "village", "features", 0, "check", "dc"), 13)

    with pytest.raises(ValidationError, match="5, 10, 15, 20, 25, 30"):
        Adventure.model_validate(data)


def test_check_skill_is_optional():
    assert CheckSpec(ability=Ability.DEX, dc=10).skill is None


@pytest.mark.parametrize("hidden_dc", sorted(VALID_DCS))
def test_exit_accepts_table_hidden_dc(hidden_dc: int):
    exit_ = Exit(id="door", to="a", direction="восток", description="Дверь.", hidden_dc=hidden_dc)

    assert exit_.hidden_dc == hidden_dc


def test_exit_hidden_dc_defaults_to_none():
    exit_ = Exit(id="door", to="a", direction="восток", description="Дверь.")

    assert exit_.hidden_dc is None


@pytest.mark.parametrize("hidden_dc", [0, 12, 13, 31])
def test_exit_rejects_hidden_dc_outside_table(hidden_dc: int):
    with pytest.raises(ValidationError, match="5, 10, 15, 20, 25, 30"):
        Exit(id="door", to="a", direction="восток", description="Дверь.", hidden_dc=hidden_dc)


def test_feature_lock_defaults_to_none():
    feature = Feature(id="chest", name="Сундук", description="Окованный сундук.")

    assert feature.locked_by is None


@pytest.mark.parametrize("lock", ["item:key", "flag:chest-opened"])
def test_feature_keeps_its_lock_like_an_exit(lock: str):
    data = with_value(("locations", "village", "features", 0, "locked_by"), lock)

    adventure = Adventure.model_validate(data)

    assert adventure.locations["village"].features[0].locked_by == lock
    assert Adventure.model_validate_json(adventure.model_dump_json()) == adventure


def test_feature_lock_must_be_a_string():
    data = with_value(("locations", "village", "features", 0, "locked_by"), 5)

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


# --- id сущностей ---

ID_PATHS: list[FieldPath] = [
    ("id",),
    ("acts", 0, "id"),
    ("locations", "village", "id"),
    ("locations", "village", "exits", 0, "id"),
    ("locations", "village", "features", 0, "id"),
    ("npcs", "marta", "id"),
    ("items", "key", "id"),
    ("encounters", "bones", "id"),
    ("revelations", "fabian", "id"),
    ("revelations", "fabian", "clues", 0, "id"),
]
BAD_IDS = ["../x", "Bad Id", "", "-start", "a/b", "ё", "a\n", "x" * 65, "..", "a.b"]


@pytest.mark.parametrize("path", ID_PATHS, ids=lambda p: "/".join(map(str, p)))
@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_entity_id_must_match_pattern(path: FieldPath, bad_id: str):
    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(with_value(path, bad_id))

    assert "string_pattern_mismatch" in error_types(caught)


@pytest.mark.parametrize("good_id", ["a", "demo-crypt", "crypt_1", "0x", "9", "x" * 64])
def test_entity_id_accepts_safe_names(good_id: str):
    assert Item(id=good_id, name="n", description="d", holder="gone").id == good_id


# --- ключи коллекций и id внутри ---


def test_collection_key_must_equal_entity_id():
    data = with_value(("locations", "village", "id"), "other")

    with pytest.raises(ValidationError, match="не совпадает"):
        Adventure.model_validate(data)


def test_custom_monster_id_must_match_pattern():
    skeleton = load_srd().monsters["skeleton"]
    data = minimal_data()
    data["monsters"] = {"../x": skeleton.model_copy(update={"id": "../x"}).model_dump()}

    with pytest.raises(ValidationError, match="монстр"):
        Adventure.model_validate(data)


# --- числа и значения ---


@pytest.mark.parametrize("value", ["text", 7, True, None])
def test_reveal_value_accepts_scalars(value: Any):
    data = with_value(("locations", "village", "features", 0, "reveals", 0, "value"), value)

    reveal = Adventure.model_validate(data).locations["village"].features[0].reveals[0]

    assert reveal.value == value
    assert type(reveal.value) is type(value)


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, 1.5, object()])
def test_reveal_value_rejects_structures(value: Any):
    data = with_value(("locations", "village", "features", 0, "reveals", 0, "value"), value)

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


@pytest.mark.parametrize("kind", ["clue", "item", "flag", "exit"])
def test_reveal_kind_accepts_known_kinds(kind: str):
    data = with_value(("locations", "village", "features", 0, "reveals", 0, "kind"), kind)

    assert Adventure.model_validate(data)


def test_reveal_kind_rejects_unknown_kind():
    data = with_value(("locations", "village", "features", 0, "reveals", 0, "kind"), "spell")

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


@pytest.mark.parametrize("count", [0, -1])
def test_monster_group_count_must_be_positive(count: int):
    data = with_value(("encounters", "bones", "monsters", 0, "count"), count)

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


@pytest.mark.parametrize("attitude", [-4, 4, 100])
def test_npc_attitude_stays_within_game_range(attitude: int):
    data = with_value(("npcs", "marta", "attitude"), attitude)

    with pytest.raises(ValidationError):
        Adventure.model_validate(data)


@pytest.mark.parametrize("hours", [0, -1, float("inf"), float("nan")])
def test_target_hours_must_be_finite_and_positive(hours: float):
    with pytest.raises(ValidationError):
        Adventure.model_validate(with_value(("target_hours",), hours))


@pytest.mark.parametrize("field", ["party_level", "party_size"])
def test_party_numbers_must_be_positive(field: str):
    with pytest.raises(ValidationError):
        Adventure.model_validate(with_value((field,), 0))


def test_required_fields_are_required():
    data = minimal_data()
    del data["start_location"]

    with pytest.raises(ValidationError, match="start_location"):
        Adventure.model_validate(data)


# --- неизменяемость ---


def test_models_are_frozen():
    adventure = Adventure.model_validate(minimal_data())

    with pytest.raises(ValidationError):
        adventure.title = "Другое"
    with pytest.raises(ValidationError):
        adventure.locations["village"].name = "Другое"
    with pytest.raises(ValidationError):
        adventure.locations["village"].exits[0].hidden_dc = 5


def test_collections_are_read_only():
    adventure = Adventure.model_validate(minimal_data())

    with pytest.raises(TypeError):
        adventure.locations["new"] = adventure.locations["crypt"]
    with pytest.raises(TypeError):
        del adventure.npcs["marta"]
    with pytest.raises(TypeError):
        adventure.monsters["extra"] = load_srd().monsters["skeleton"]
    assert isinstance(adventure.locations["village"].exits, tuple)


def test_adventure_can_be_deep_copied_and_compared():
    adventure = Adventure.model_validate(minimal_data())

    assert copy.deepcopy(adventure) == adventure


def test_feature_is_not_hidden_by_default():
    feature = Feature(id="altar", name="Алтарь", description="Старый алтарь.")

    assert feature.hidden is False
    assert feature.check is None
    assert feature.reveals == ()


# --- в схеме нет Any ---


def _untyped_properties(schema: Any, path: str = "") -> list[str]:
    """Свойства JSON-схемы без типа: так выглядит поле `Any`."""
    found: list[str] = []
    if isinstance(schema, dict):
        for name, prop in schema.get("properties", {}).items():
            typed = {"type", "$ref", "anyOf", "allOf", "oneOf", "const", "enum"} & prop.keys()
            if not typed:
                found.append(f"{path}.{name}")
        for key, value in schema.items():
            found += _untyped_properties(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found += _untyped_properties(value, f"{path}[{index}]")
    return found


def test_schema_has_no_any_fields():
    # Поле без типа даёт вложить любую структуру, в том числе «бомбу псевдонимов» YAML.
    assert _untyped_properties(Adventure.model_json_schema()) == []


@pytest.mark.parametrize(("value", "expected"), [("crypt-1", True), ("../x", False), ("A", False)])
def test_is_entity_id_matches_the_id_pattern(value: str, expected: bool):
    assert is_entity_id(value) is expected
