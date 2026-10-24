"""Схемы ответов шагов генератора: пределы размера мира и Сл из таблицы (план DND-2 B3.1, спека §8,
§9; README «Генерация приключения», «Шаги и роли»).

Пределы числа сущностей в ответе модели — это и есть ограничение размера мира, который строит
генератор (решение dispatcher B3 world_section_limit): ответ сверх предела не проходит схему, и
`complete_json` повторяет запрос. Числа ниже — те, что названы в README. Сл проверок в тайниках,
тайнах мест и скрытых выходах — только из таблицы 5/10/15/20/25/30 (инвариант «ж»).
"""

import copy
from typing import Any

import pytest
from generator_fakes import v2_answers
from pydantic import BaseModel, ValidationError

from dnd_server.authoring import schemas


def copies(template: dict[str, Any], count: int, unique: tuple[str, ...]) -> list[dict[str, Any]]:
    """`count` копий сущности ответа, у каждой свои значения полей `unique` (id, локация боя)."""
    return [template | {key: f"{template[key]}-{n}" for key in unique} for n in range(count)]


def answer_with(step: str, field: str, unique: tuple[str, ...], count: int) -> dict[str, Any]:
    """Ответ шага из демо v2, где в списке `field` ровно `count` сущностей."""
    answer = copy.deepcopy(v2_answers()[f"generator.{step}"])
    answer[field] = copies(answer[field][0], count, unique)
    return answer


# (схема, шаг демо, поле-список, поля с уникальными значениями, предел из README)
LIMITS = [
    pytest.param(schemas.PantheonOut, "pantheon", "deities", ("id",), 6, id="боги"),
    pytest.param(schemas.FactionsOut, "factions", "factions", ("id",), 6, id="фракции"),
    pytest.param(schemas.LocationGraphOut, "locations", "locations", ("id",), 12, id="локации"),
    pytest.param(schemas.NpcsOut, "npcs", "npcs", ("id",), 12, id="NPC"),
    pytest.param(schemas.RelationsOut, "relations", "relations", ("id",), 24, id="связи"),
    pytest.param(
        schemas.EncountersOut, "encounters", "encounters", ("id", "location_id"), 8, id="бои"
    ),
    pytest.param(schemas.EncountersOut, "encounters", "monsters", ("id",), 3, id="свои статблоки"),
    pytest.param(schemas.ItemsOut, "items", "items", ("id",), 20, id="предметы"),
    pytest.param(schemas.ConceptOut, "concept", "revelations", ("id",), 5, id="открытия"),
    pytest.param(schemas.WorldOut, "world", "regions", ("id",), 6, id="края"),
    pytest.param(schemas.WorldOut, "world", "rumors", (), 8, id="слухи"),
    pytest.param(
        schemas.CluesOut, "clues", "revelations", ("revelation_id",), 5, id="улики открытий"
    ),
]


@pytest.mark.parametrize(("schema", "step", "field", "unique", "limit"), LIMITS)
def test_an_answer_exactly_at_the_limit_of_the_world_size_passes(
    schema: type[BaseModel], step, field, unique, limit
):
    answer = schema.model_validate(answer_with(step, field, unique, limit))

    assert len(getattr(answer, field)) == limit


@pytest.mark.parametrize(("schema", "step", "field", "unique", "limit"), LIMITS)
def test_one_entity_over_the_limit_of_the_world_size_is_a_schema_error(
    schema: type[BaseModel], step, field, unique, limit
):
    with pytest.raises(ValidationError) as error:
        schema.model_validate(answer_with(step, field, unique, limit + 1))

    assert {problem["loc"][0] for problem in error.value.errors()} == {field}


def test_the_limits_in_the_code_are_the_ones_named_in_the_readme():
    assert (schemas.MAX_DEITIES, schemas.MAX_FACTIONS) == (6, 6)
    assert (schemas.MAX_LOCATIONS, schemas.MAX_NPCS, schemas.MAX_RELATIONS) == (12, 12, 24)
    assert (schemas.MAX_ENCOUNTERS, schemas.MAX_CUSTOM_MONSTERS, schemas.MAX_ITEMS) == (8, 3, 20)


# --- Сл только из таблицы ---

TABLE_DCS = [5, 10, 15, 20, 25, 30]
NOT_IN_TABLE = [0, 4, 12, 31, 35, -5]


def check_of(dc: int) -> dict[str, Any]:
    return {"ability": "wis", "skill": "perception", "dc": dc}


def cache(dc: int) -> dict[str, Any]:
    """Тайник: место с проверкой, внутри которого лежит предмет."""
    return {
        "id": "keeper-cupboard",
        "name": "Шкаф смотрителя",
        "description": "За банками с маслом висит ключ.",
        "check": check_of(dc),
        "reveals": [{"kind": "item", "id": "lamp-key"}],
    }


def secret(dc: int) -> dict[str, Any]:
    """Тайна места с проверкой и находками."""
    return {
        "id": "drowned-keeper",
        "text": "Под половицей спрятано кольцо смотрителя.",
        "check": check_of(dc),
        "reveals": [{"kind": "item", "id": "keeper-ring"}],
    }


def hidden_exit(dc: int) -> dict[str, Any]:
    return {
        "id": "to-cellar",
        "to": "cellar",
        "direction": "вниз",
        "description": "Люк под ковром.",
        "hidden_dc": dc,
    }


DC_PLACES = [
    pytest.param(schemas.FeatureOut, cache, id="тайник"),
    pytest.param(schemas.SecretOut, secret, id="тайна места"),
    pytest.param(schemas.ExitOut, hidden_exit, id="скрытый выход"),
]


@pytest.mark.parametrize(("schema", "build"), DC_PLACES)
@pytest.mark.parametrize("dc", TABLE_DCS)
def test_a_dc_from_the_table_is_accepted(schema: type[BaseModel], build, dc):
    schema.model_validate(build(dc))


@pytest.mark.parametrize(("schema", "build"), DC_PLACES)
@pytest.mark.parametrize("dc", NOT_IN_TABLE)
def test_a_dc_outside_the_table_is_a_schema_error(schema: type[BaseModel], build, dc):
    with pytest.raises(ValidationError):
        schema.model_validate(build(dc))


@pytest.mark.parametrize("dc", NOT_IN_TABLE)
def test_a_location_contents_answer_with_a_dc_outside_the_table_is_refused_whole(dc):
    # Тот же запрет на уровне ответа шага: модель не протащит Сл через вложенное место или тайну.
    contents = copy.deepcopy(v2_answers()["generator.location_contents"][2])
    contents["secrets"][0]["check"]["dc"] = dc

    with pytest.raises(ValidationError):
        schemas.LocationContentsOut.model_validate(contents)
