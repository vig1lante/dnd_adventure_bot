"""Формат приключения v2 (спека DND-2 §8, решение Р8): схема, перевод v1 → v2, чтение и запись.

v2 добавляет мир, пантеон, фракции, общий граф связей, секреты мест, боссов, злодея и поля
дневника. Старые приключения и копии в начатых играх (`games.adventure_data`) читаются без правок:
данные без `format_version` — это v1, и связи из `npcs[].relations` переходят в граф. Запись —
всегда v2. Образцы v1 лежат в `fixtures/`: копия демо до перевода и копия игры из golden DND-1.
"""

import copy
import json
import shutil
import traceback
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args

import pytest
import yaml
from adventure_factory import adventure_data, make_adventure
from pydantic import ValidationError

from dnd_engine.adventure.loader import (
    AdventureFormatError,
    adventure_from_data,
    dump_adventure,
    load_adventure,
)
from dnd_engine.adventure.schema import (
    V1_RELATION_KINDS,
    Adventure,
    Deity,
    Encounter,
    Faction,
    Item,
    Location,
    LocationSecret,
    Npc,
    Region,
    Relation,
    RelationKind,
    Rumor,
    WorldInfo,
    is_entity_id,
)
from dnd_engine.adventure.validator import validate
from dnd_engine.checks import VALID_DCS
from dnd_engine.content import SrdContent, load_srd

FIXTURES = Path(__file__).parent / "fixtures"
V1_DEMO_DIR = FIXTURES / "demo-crypt-v1"  # демо-приключение до перевода в v2
V1_GAME_COPY = FIXTURES / "adventure_v1.json"  # копия приключения в игре из golden DND-1

FieldPath = tuple[str | int, ...]


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def relation(**fields: Any) -> Relation:
    """Связь графа: по умолчанию «Фабиан — должник Марты»; поля переопределяются."""
    base = {"id": "rel", "a": "npc:fabian", "b": "npc:marta", "kind": "debtor"}
    return Relation.model_validate(base | fields)


def v1_data(**relations: list[dict[str, Any]]) -> dict[str, Any]:
    """Чистое приключение фабрики в форме v1: без `format_version` и графа, связи — у NPC.

    `relations` — связи v1 по id NPC-владельца; без них Марта и Фабиан связаны, как в демо DND-1."""
    data = adventure_data()
    for key in ("format_version", "relations", "world", "deities", "factions", "villain"):
        del data[key]
    for location in data["locations"].values():
        location.pop("secrets", None)
    relations = relations or {
        "marta": [{"npc_id": "fabian", "kind": "debtor", "note": "должник брата"}],
        "fabian": [{"npc_id": "marta", "kind": "creditor"}],
    }
    for npc_id, entries in relations.items():
        data["npcs"][npc_id]["relations"] = entries
    return data


def graph_of(adventure: Adventure) -> list[tuple[str, str, str, str, str]]:
    """Граф связей коротко: (id, a, b, kind, note)."""
    return [(r.id, r.a, r.b, r.kind, r.note) for r in adventure.relations]


def with_value(path: FieldPath, value: Any) -> dict[str, Any]:
    data = adventure_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


# --- схема v2 ---


def test_the_factory_adventure_is_a_full_v2_adventure():
    adventure = make_adventure()

    assert adventure.format_version == 2
    assert adventure.world.regions[0].id == "marsh"
    assert set(adventure.deities) == {"warden"} and set(adventure.factions) == {"candle-circle"}
    assert [r.id for r in adventure.relations] == [
        "fabian-debtor-marta",
        "warden-enemy-candle-circle",
        "marta-worships-warden",
        "fabian-seeks-crypt",
    ]
    assert adventure.locations["crypt"].secrets[0].id == "empty-vault"
    assert adventure.villain == "npc:fabian"


def test_the_relation_vocabulary_is_the_one_of_the_spec():
    # родня, супруги, любовники, друзья, союзники, соперники, враги, должник, покровитель, слуга,
    # поклоняется, предал, ищет — и `other` с пояснением.
    assert set(get_args(RelationKind)) == {
        "kin",
        "spouse",
        "lover",
        "friend",
        "ally",
        "rival",
        "enemy",
        "debtor",
        "patron",
        "servant",
        "worships",
        "betrayed",
        "seeks",
        "other",
    }


@pytest.mark.parametrize("kind", sorted(set(get_args(RelationKind)) - {"other"}))
def test_every_kind_of_the_vocabulary_is_a_relation(kind: str):
    assert relation(kind=kind).kind == kind


@pytest.mark.parametrize("kind", ["creditor", "враги", "Enemy", "", "sibling"])
def test_a_kind_outside_the_vocabulary_is_rejected(kind: str):
    with pytest.raises(ValidationError, match="kind"):
        relation(kind=kind)


@pytest.mark.parametrize("note", ["", "   "])
def test_an_other_relation_needs_a_note(note: str):
    with pytest.raises(ValidationError, match="note"):
        relation(kind="other", note=note)


def test_an_other_relation_with_a_note_is_fine():
    assert relation(kind="other", note="соседи через забор").note == "соседи через забор"


def test_a_relation_is_open_and_mutual_by_default():
    found = relation()

    assert (found.note, found.secret, found.mutual) == ("", False, True)


@pytest.mark.parametrize(
    "ref",
    ["npc:marta", "faction:candle-circle", "deity:warden", "location:crypt", "npc:" + "a" * 64],
)
def test_a_reference_is_a_kind_and_an_id(ref: str):
    assert relation(a=ref).a == ref


@pytest.mark.parametrize(
    "ref",
    ["marta", "item:key", "monster:ghoul", "npc:", "npc:Марта", "npc:../x", "npc:" + "a" * 65, ""],
)
def test_a_reference_of_another_kind_or_with_a_bad_id_is_rejected(ref: str):
    with pytest.raises(ValidationError):
        relation(b=ref)


@pytest.mark.parametrize(
    "path",
    [("villain",), ("factions", "candle-circle", "leader")],
    ids=["villain", "leader"],
)
@pytest.mark.parametrize("ref", ["fabian", "item:key", "npc:Фабиан"])
def test_villain_and_leader_are_references_too(path: FieldPath, ref: str):
    with pytest.raises(ValidationError):
        Adventure.model_validate(with_value(path, ref))


V2_MODEL_PATHS: list[FieldPath] = [
    ("world",),
    ("world", "regions", 0),
    ("world", "rumors", 0),
    ("deities", "warden"),
    ("factions", "candle-circle"),
    ("relations", 0),
    ("locations", "crypt", "secrets", 0),
    ("locations", "crypt", "secrets", 0, "check"),
    ("locations", "crypt", "secrets", 0, "reveals", 0),
    ("encounters", "bones"),  # встреча с новым полем `boss`
]


@pytest.mark.parametrize("path", V2_MODEL_PATHS, ids=lambda p: "/".join(map(str, p)))
def test_an_extra_field_is_rejected_in_every_v2_model(path: FieldPath):
    data = adventure_data()
    target: Any = data
    for key in path:
        target = target[key]
    target["surprise"] = 1

    with pytest.raises(ValidationError, match="surprise"):
        Adventure.model_validate(data)


@pytest.mark.parametrize("dc", sorted(VALID_DCS))
def test_a_secret_check_takes_a_dc_from_the_table(dc: int):
    secret = LocationSecret.model_validate(
        {"id": "s", "text": "т", "check": {"ability": "int", "dc": dc}}
    )

    assert secret.check is not None and secret.check.dc == dc


@pytest.mark.parametrize("dc", [0, 12, 13, 31])
def test_a_secret_check_with_a_dc_outside_the_table_is_rejected(dc: int):
    data = with_value(("locations", "crypt", "secrets", 0, "check", "dc"), dc)

    with pytest.raises(ValidationError, match="5, 10, 15, 20, 25, 30"):
        Adventure.model_validate(data)


def test_a_secret_needs_neither_a_check_nor_reveals():
    secret = LocationSecret(id="s", text="Клада здесь нет.")

    assert (secret.check, secret.reveals) == (None, ())


def test_an_encounter_is_not_a_boss_fight_unless_marked():
    bones = make_adventure().encounters["bones"]
    boss = Adventure.model_validate(with_value(("encounters", "bones", "boss"), True))

    assert bones.boss is False and boss.encounters["bones"].boss is True
    assert Encounter.model_fields["boss"].default is False


@pytest.mark.parametrize("model", [Region, Rumor, Deity, Faction, Location, Npc, Item])
def test_every_journal_entity_has_a_public_text_and_is_unknown_at_start_by_default(model):
    fields = model.model_fields

    assert (fields["public"].default, fields["known_at_start"].default) == ("", False)


def test_journal_intro_defaults_to_the_pitch():
    data = adventure_data()

    assert Adventure.model_validate(data).journal_intro == data["pitch"]
    data["journal_intro"] = "Найти смотрителя."
    assert Adventure.model_validate(data).journal_intro == "Найти смотрителя."


@pytest.mark.parametrize("attitude", [-4, 4])
def test_faction_attitude_to_the_party_has_the_steps_of_an_npc(attitude: int):
    with pytest.raises(ValidationError):
        Adventure.model_validate(
            with_value(("factions", "candle-circle", "attitude_to_party"), attitude)
        )


def test_deity_and_faction_defaults():
    data = adventure_data()
    deity = Deity.model_validate({**data["deities"]["warden"], "avatar_statblock": None})
    faction = Faction(id="f", name="Ф", goal="ц", methods="м")

    assert (deity.secret, deity.avatar_statblock) == ("", None)
    assert (faction.leader, faction.base_location, faction.secret) == (None, None, "")
    assert faction.attitude_to_party == 0


@pytest.mark.parametrize("base", ["location:crypt", "Склеп", "../crypt", "crypt\nзаголовок", ""])
def test_a_faction_base_is_the_id_of_a_location(base: str):
    # Спека §8: `base_location: <id> | null` — id по шаблону, как у остальных ссылок на сущности.
    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(with_value(("factions", "candle-circle", "base_location"), base))

    locations = [error["loc"] for error in caught.value.errors()]
    assert locations == [("factions", "candle-circle", "base_location")]


def test_an_adventure_without_world_has_an_empty_one():
    data = adventure_data()
    del data["world"]

    world = Adventure.model_validate(data).world

    assert world == WorldInfo() and (world.history, world.regions, world.rumors) == ("", (), ())


def test_deities_and_factions_are_read_only():
    adventure = make_adventure()

    with pytest.raises(TypeError):
        adventure.deities["new"] = adventure.deities["warden"]
    with pytest.raises(TypeError):
        del adventure.factions["candle-circle"]


def test_the_collection_key_must_be_the_id_of_a_deity_and_a_faction():
    data = adventure_data()
    data["deities"]["other"] = data["deities"].pop("warden")

    with pytest.raises(ValidationError, match="deities"):
        Adventure.model_validate(data)


# --- версия формата ---


@pytest.mark.parametrize("given", [{}, {"format_version": 1}, {"format_version": 2}])
def test_any_supported_version_is_read_as_the_current_one(given: dict[str, Any]):
    data = v1_data() if given.get("format_version", 1) == 1 else adventure_data()
    data.pop("format_version", None)

    assert Adventure.model_validate(data | given).format_version == 2


@pytest.mark.parametrize("version", [0, 3, "2", True, None, 2.0, 1.0, "1"])
def test_an_unknown_format_version_is_rejected(version: Any):
    # Версия — только целое число: `2.0` отвергается так же, как `"2"` и `true`.
    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(adventure_data() | {"format_version": version})

    assert [error["loc"] for error in caught.value.errors()] == [("format_version",)]


def test_a_float_version_is_rejected_even_when_npcs_carry_v1_relations():
    # `2.0` — не v2: ошибка называет версию, а не связи внутри NPC, как было бы у настоящей v2.
    data = v1_data() | {"format_version": 2.0}

    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(data)

    assert [error["loc"] for error in caught.value.errors()] == [("format_version",)]


def test_v2_data_keeps_relations_only_in_the_graph():
    data = adventure_data()
    data["npcs"]["marta"]["relations"] = [{"npc_id": "fabian", "kind": "debtor"}]

    with pytest.raises(ValidationError, match="relations"):
        Adventure.model_validate(data)


def test_v2_data_with_npc_models_carrying_relations_is_rejected_too():
    # Так NPC приходят от генератора сервера: моделью `Npc`, а не словарём.
    data = adventure_data()
    data["npcs"]["marta"]["relations"] = [{"npc_id": "fabian", "kind": "debtor"}]
    data["npcs"] = {key: Npc.model_validate(npc) for key, npc in data["npcs"].items()}

    with pytest.raises(ValidationError, match="relations.yaml"):
        Adventure.model_validate(data)


# --- перевод v1 → v2 ---


def test_v1_relations_move_into_the_graph_and_the_npc_field_empties():
    adventure = Adventure.model_validate(v1_data())

    assert graph_of(adventure) == [
        ("fabian-debtor-marta", "npc:fabian", "npc:marta", "debtor", "должник брата"),
        ("marta-other-fabian", "npc:marta", "npc:fabian", "other", "кредитор"),
    ]
    assert all(npc.relations == () for npc in adventure.npcs.values())


def test_a_v1_relation_names_who_the_other_npc_is_to_its_owner():
    # В v1 связь лежит у NPC-владельца и говорит, кем ему приходится `npc_id`: у Марты
    # `{npc_id: fabian, kind: debtor}` — «Фабиан — её должник». В графе это `a` = Фабиан, `b` =
    # Марта: связь читается «a — <kind> для b».
    adventure = Adventure.model_validate(
        v1_data(marta=[{"npc_id": "fabian", "kind": "debtor"}], fabian=[])
    )

    [moved] = adventure.relations
    assert (moved.a, moved.kind, moved.b) == ("npc:fabian", "debtor", "npc:marta")


def test_a_v1_relation_is_not_mutual_in_the_graph():
    # v1 говорит только о стороне владельца; взаимность в нём записывалась второй связью.
    adventure = Adventure.model_validate(v1_data())

    assert [r.mutual for r in adventure.relations] == [False, False]
    assert [r.secret for r in adventure.relations] == [False, False]


V1_KINDS = [
    # (вид и пояснение v1) → (вид и пояснение v2)
    (("enemy", ""), ("enemy", "")),
    (("debtor", "должник брата"), ("debtor", "должник брата")),
    (("lover", ""), ("lover", "")),
    (("ally", ""), ("ally", "")),
    ((" Enemy ", ""), ("enemy", "")),
    (("sibling", ""), ("kin", "брат или сестра")),
    (("brother", ""), ("kin", "брат")),
    (("sister", ""), ("kin", "сестра")),
    (("parent", "старший в роду"), ("kin", "родитель; старший в роду")),
    (("father", ""), ("kin", "отец")),
    (("mother", ""), ("kin", "мать")),
    (("child", ""), ("kin", "ребёнок")),
    (("son", ""), ("kin", "сын")),
    (("daughter", ""), ("kin", "дочь")),
    (("relative", ""), ("kin", "родственник")),
    (("husband", ""), ("spouse", "муж")),
    (("wife", ""), ("spouse", "жена")),
    (("creditor", ""), ("other", "кредитор")),
    (("neighbour", "живут рядом"), ("other", "neighbour; живут рядом")),
    (("старший брат", ""), ("other", "старший брат")),
    (("", ""), ("other", "связь без типа")),
]


@pytest.mark.parametrize(("old", "new"), V1_KINDS, ids=lambda pair: pair[0] or "пусто")
def test_a_v1_kind_is_translated_by_the_table(old: tuple[str, str], new: tuple[str, str]):
    kind, note = old
    data = v1_data(marta=[{"npc_id": "fabian", "kind": kind, "note": note}], fabian=[])

    [moved] = Adventure.model_validate(data).relations

    assert (moved.kind, moved.note) == new
    assert moved.id == f"fabian-{new[0]}-marta"


@pytest.mark.parametrize(("v1_kind", "expected"), sorted(V1_RELATION_KINDS.items()))
def test_every_entry_of_the_v1_kind_table_is_translated_whatever_the_case_and_spaces(
    v1_kind: str, expected: tuple[str, str]
):
    # Ожидание берётся из самой таблицы: новая запись в ней сразу проходит через перевод.
    data = v1_data(marta=[{"npc_id": "fabian", "kind": f"  {v1_kind.upper()} "}], fabian=[])

    [moved] = Adventure.model_validate(data).relations

    assert (moved.kind, moved.note) == expected


@pytest.mark.parametrize("kind", sorted(set(get_args(RelationKind)) - {"other"}))
def test_every_kind_of_the_vocabulary_passes_from_v1_as_it_is(kind: str):
    data = v1_data(marta=[{"npc_id": "fabian", "kind": kind, "note": "давно"}], fabian=[])

    [moved] = Adventure.model_validate(data).relations

    assert (moved.id, moved.kind, moved.note) == (f"fabian-{kind}-marta", kind, "давно")


@pytest.mark.parametrize(("note", "expected"), [("", "other"), ("соседи", "other; соседи")])
def test_a_v1_kind_other_keeps_a_note_so_the_moved_relation_stays_valid(note: str, expected: str):
    # У связи `other` пояснение обязательно: вид v1 `other` уходит в `note`, иначе старая копия
    # игры со связью без пояснения перестала бы читаться.
    data = v1_data(marta=[{"npc_id": "fabian", "kind": "other", "note": note}], fabian=[])

    [moved] = Adventure.model_validate(data).relations

    assert (moved.id, moved.kind, moved.note) == ("fabian-other-marta", "other", expected)


def test_equal_converted_ids_of_different_owners_get_numbered_too():
    # «a — kin для b-kin-c» и «a-kin-b — kin для c» дают одну основу id `a-kin-b-kin-c`: номер
    # получает вторая связь, хотя её владелец — другой NPC.
    data = v1_data(marta=[], fabian=[])
    for npc_id in ("a", "a-kin-b", "b-kin-c", "c"):
        data["npcs"][npc_id] = dict(data["npcs"]["marta"], id=npc_id, relations=[])
    data["npcs"]["b-kin-c"]["relations"] = [{"npc_id": "a", "kind": "kin"}]
    data["npcs"]["c"]["relations"] = [{"npc_id": "a-kin-b", "kind": "kin"}]

    adventure = Adventure.model_validate(data)

    assert [(r.id, r.a, r.b) for r in adventure.relations] == [
        ("a-kin-b-kin-c", "npc:a", "npc:b-kin-c"),
        ("a-kin-b-kin-c-2", "npc:a-kin-b", "npc:c"),
    ]


def test_a_v1_relation_to_a_missing_npc_moves_and_the_validator_finds_it(srd: SrdContent):
    # Перевод ссылки не судит: связь в пустоту не пропадает молча, а становится ошибкой валидатора.
    adventure = Adventure.model_validate(
        v1_data(marta=[{"npc_id": "nobody", "kind": "enemy"}], fabian=[])
    )

    issues = {(issue.code, issue.ref) for issue in validate(adventure, srd).issues}

    assert graph_of(adventure) == [("nobody-enemy-marta", "npc:nobody", "npc:marta", "enemy", "")]
    assert ("dangling_ref", "relation:nobody-enemy-marta") in issues


def test_repeated_v1_relations_of_one_pair_get_numbered_ids():
    entries = [
        {"npc_id": "fabian", "kind": "creditor"},
        {"npc_id": "fabian", "kind": "neighbour"},
        {"npc_id": "fabian", "kind": "landlord"},
    ]
    adventure = Adventure.model_validate(v1_data(marta=entries, fabian=[]))

    assert [r.id for r in adventure.relations] == [
        "fabian-other-marta",
        "fabian-other-marta-2",
        "fabian-other-marta-3",
    ]


def test_a_converted_id_of_long_npc_ids_still_fits_the_id_pattern():
    data = v1_data(marta=[], fabian=[])
    long_a, long_b = "a" * 64, "b" * 64
    for npc_id in (long_a, long_b):
        data["npcs"][npc_id] = dict(data["npcs"]["marta"], id=npc_id)
    data["npcs"][long_b]["relations"] = [{"npc_id": long_a, "kind": "enemy"}] * 2

    adventure = Adventure.model_validate(data)

    ids = [r.id for r in adventure.relations]
    assert all(is_entity_id(ident) for ident in ids) and len(set(ids)) == 2
    assert [(r.a, r.b) for r in adventure.relations] == [(f"npc:{long_a}", f"npc:{long_b}")] * 2


def test_reading_v1_data_does_not_change_the_data():
    data = v1_data()
    before = copy.deepcopy(data)

    Adventure.model_validate(data)

    assert data == before


def test_npc_models_with_v1_relations_are_converted_too():
    # Так собирает приключение генератор сервера: NPC автора приходят уже моделями `Npc`.
    data = v1_data()
    data["npcs"] = {key: Npc.model_validate(npc) for key, npc in data["npcs"].items()}

    adventure = Adventure.model_validate(data)

    assert graph_of(adventure) == graph_of(Adventure.model_validate(v1_data()))
    assert all(npc.relations == () for npc in adventure.npcs.values())


def test_v1_data_without_relations_gets_an_empty_graph():
    adventure = Adventure.model_validate(v1_data(marta=[], fabian=[]))

    assert adventure.relations == ()


def test_data_without_a_version_keeps_its_graph_and_adds_the_moved_relations():
    data = v1_data(marta=[{"npc_id": "fabian", "kind": "enemy"}], fabian=[])
    data["relations"] = [{"id": "given", "a": "npc:marta", "b": "npc:fabian", "kind": "kin"}]

    adventure = Adventure.model_validate(data)

    assert [r.id for r in adventure.relations] == ["given", "fabian-enemy-marta"]


@pytest.mark.parametrize(
    "given_ids",
    [["fabian-debtor-marta"], ["fabian-debtor-marta", "fabian-debtor-marta-2"]],
    ids=["тот-же-id", "и-номер-занят"],
)
@pytest.mark.parametrize("as_models", [False, True], ids=["словари", "модели"])
def test_a_moved_v1_relation_gets_an_id_free_of_the_given_graph(
    srd: SrdContent, given_ids: list[str], as_models: bool
):
    # Заданный граф бывает словарями (копия, генератор) или моделями `Relation` (папка v1 с
    # relations.yaml): id переносимой связи не повторяет ни один его id, валидатор молчит о повторе.
    given = [relation(id=ident) for ident in given_ids]
    data = v1_data(marta=[{"npc_id": "fabian", "kind": "debtor"}], fabian=[])
    data["relations"] = given if as_models else [r.model_dump() for r in given]

    adventure = Adventure.model_validate(data)

    moved = f"fabian-debtor-marta-{len(given_ids) + 1}"
    assert [r.id for r in adventure.relations] == [*given_ids, moved]
    assert not [i for i in validate(adventure, srd).issues if "повторяется" in i.message]


def test_a_v1_folder_with_a_graph_file_moves_npc_relations_under_free_ids(
    srd: SrdContent, tmp_path: Path
):
    # Папка v1 может нести relations.yaml (сценарий ревью): связь Марты из NPC и связь из файла
    # говорят об одном, но автор не получает ошибку повтора id, которого сам не писал.
    shutil.copytree(V1_DEMO_DIR, tmp_path / "v1")
    graph = [{"id": "fabian-debtor-marta", "a": "npc:fabian", "b": "npc:marta", "kind": "debtor"}]
    (tmp_path / "v1" / "relations.yaml").write_text(yaml.safe_dump(graph), encoding="utf-8")

    adventure = load_adventure(tmp_path / "v1")

    assert [r.id for r in adventure.relations] == [
        "fabian-debtor-marta",
        "marta-other-fabian",
        "fabian-debtor-marta-2",
    ]
    assert not validate(adventure, srd).blocking


def test_a_broken_given_graph_of_v1_data_is_reported_at_its_entries():
    # Записи заданного графа без id или не словари — ошибка их поля, а не сбой перевода.
    data = v1_data(marta=[{"npc_id": "fabian", "kind": "enemy"}], fabian=[])
    data["relations"] = [42, {"id": 7, "a": "npc:marta", "b": "npc:fabian", "kind": "kin"}]

    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(data)

    locations = {error["loc"][:2] for error in caught.value.errors()}
    assert locations == {("relations", 0), ("relations", 1)}


def test_v1_data_keeps_a_given_world_pantheon_and_factions_as_they_are(srd: SrdContent):
    # Перевод трогает только связи NPC и граф: мир, боги и фракции папки v1 идут как есть, а их id
    # (другие виды) с id перенесённых связей не спорят.
    v2 = make_adventure()
    data = v1_data() | {
        "world": v2.world.model_dump(),
        "deities": {key: deity.model_dump() for key, deity in v2.deities.items()},
        "factions": {key: faction.model_dump() for key, faction in v2.factions.items()},
    }

    adventure = Adventure.model_validate(data)

    assert (adventure.world, adventure.deities, adventure.factions) == (
        v2.world,
        v2.deities,
        v2.factions,
    )
    assert not [i for i in validate(adventure, srd).issues if "повторяется" in i.message]


@pytest.mark.parametrize(
    ("kind", "note", "expected"),
    [
        ("sibling", "   ", "брат или сестра"),
        ("sibling", "  старший  ", "брат или сестра; старший"),
        ("enemy", "\tдавно\n", "давно"),
        ("enemy", "   ", ""),
        ("neighbour", " рядом ", "neighbour; рядом"),
    ],
)
def test_a_v1_note_is_trimmed_before_it_joins_the_graph(kind: str, note: str, expected: str):
    data = v1_data(marta=[{"npc_id": "fabian", "kind": kind, "note": note}], fabian=[])

    [moved] = Adventure.model_validate(data).relations

    assert moved.note == expected


@pytest.mark.parametrize("npc_id", ["Фабиан", "fabian brother", "../fabian", ""])
def test_a_v1_relation_to_an_id_off_the_pattern_is_reported_at_its_npc(npc_id: str):
    # Ошибку показывает поле NPC, где автор её написал, а не граф, куда связь ушла бы при переводе.
    data = v1_data(marta=[{"npc_id": npc_id, "kind": "enemy"}], fabian=[])

    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(data)

    locations = [error["loc"] for error in caught.value.errors()]
    assert locations == [("npcs", "marta", "relations", 0, "npc_id")]


def test_a_broken_v1_relation_is_reported_at_its_npc():
    data = v1_data(marta=[{"npc_id": "fabian", "kind": "enemy", "surprise": 1}], fabian=[])

    with pytest.raises(ValidationError) as caught:
        Adventure.model_validate(data)

    locations = [error["loc"] for error in caught.value.errors()]
    assert ("npcs", "marta", "relations", 0, "surprise") in locations


# --- старое демо и копия игры DND-1 ---

# Порядок графа — порядок NPC (файлы по именам: fabian, затем marta) и их связей.
V1_DEMO_GRAPH = [
    (
        "marta-other-fabian",
        "npc:marta",
        "npc:fabian",
        "other",
        "кредитор; Марта — его кредитор, ей он должен сорок золотых.",
    ),
    (
        "fabian-debtor-marta",
        "npc:fabian",
        "npc:marta",
        "debtor",
        "Фабиан должен ей сорок золотых за комнату и ужины.",
    ),
]


def test_the_old_demo_folder_is_read_as_v2(srd: SrdContent):
    adventure = load_adventure(V1_DEMO_DIR)

    assert adventure.format_version == 2
    assert graph_of(adventure) == V1_DEMO_GRAPH
    assert all(npc.relations == () for npc in adventure.npcs.values())
    assert adventure.world == WorldInfo() and adventure.deities == {} and adventure.villain is None
    assert adventure.journal_intro == adventure.pitch
    assert not validate(adventure, srd).blocking


def test_the_golden_game_copy_of_dnd1_is_read_by_adventure_from_data(srd: SrdContent):
    golden = json.loads(V1_GAME_COPY.read_text(encoding="utf-8"))
    assert "format_version" not in golden  # образец старше формата v2

    adventure = adventure_from_data(golden)

    assert graph_of(adventure) == V1_DEMO_GRAPH
    assert all(npc.relations == () for npc in adventure.npcs.values())
    assert not validate(adventure, srd).blocking


def test_a_v1_game_copy_written_back_is_v2_and_reads_the_same():
    adventure = adventure_from_data(json.loads(V1_GAME_COPY.read_text(encoding="utf-8")))

    written = adventure.model_dump(mode="json")

    assert written["format_version"] == 2 and len(written["relations"]) == 2
    assert all(npc["relations"] == [] for npc in written["npcs"].values())
    assert adventure_from_data(written) == adventure


def test_adventure_from_data_reads_v2_data_as_it_is():
    adventure = make_adventure()

    assert adventure_from_data(adventure.model_dump(mode="json")) == adventure


def test_adventure_from_data_takes_any_mapping():
    assert adventure_from_data(MappingProxyType(adventure_data())) == make_adventure()


def test_adventure_from_data_reports_every_problem_as_a_format_error():
    data = adventure_data()
    del data["title"]
    data["relations"][0]["kind"] = "creditor"

    with pytest.raises(AdventureFormatError) as caught:
        adventure_from_data(data)

    assert isinstance(caught.value, ValueError)
    assert any("title" in problem for problem in caught.value.problems)
    assert any("relations.0.kind" in problem for problem in caught.value.problems)


INPUT_MARK = "ЗНАЧЕНИЕ-ИЗ-КОПИИ-42"  # значение, которого не должно быть в тексте ошибки
# Поля, где строка — не то значение: число, логическое, ссылка, список или версия.
BROKEN_V2_FIELDS: list[FieldPath] = [
    ("format_version",),
    ("target_hours",),
    ("villain",),
    ("world", "regions", 0, "known_at_start"),
    ("world", "rumors", 0, "true"),
    ("encounters", "bones", "boss"),
    ("deities", "warden", "domains"),
    ("factions", "candle-circle", "attitude_to_party"),
    ("relations", 0, "kind"),
    ("relations", 1, "a"),
    ("relations", 2, "secret"),
    ("locations", "crypt", "secrets", 0, "check", "dc"),
]


def test_adventure_from_data_names_broken_fields_without_their_values():
    # Копия игры из базы — чужие данные: в ошибке поле и причина, но не само значение.
    data = adventure_data()
    for path in BROKEN_V2_FIELDS:
        target: Any = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = INPUT_MARK

    with pytest.raises(AdventureFormatError) as caught:
        adventure_from_data(data)

    problems = caught.value.problems
    for path in BROKEN_V2_FIELDS:
        field = ".".join(map(str, path))
        assert any(f"поле {field}:" in problem for problem in problems), (field, problems)
    assert all(INPUT_MARK not in problem for problem in problems)
    assert INPUT_MARK not in str(caught.value)


def test_the_traceback_of_a_broken_copy_holds_no_value_of_the_copy():
    # Трассировку итоговой ошибки печатает журнал процесса (uvicorn): цепочка не тащит
    # `ValidationError` с `input_value`. Первопричина остаётся в `__context__` для безопасного
    # журнала сервера (`safe_traceback` описывает её без значений входа).
    data = adventure_data()
    data["relations"][0]["kind"] = INPUT_MARK

    with pytest.raises(AdventureFormatError) as caught:
        adventure_from_data(data)

    printed = "".join(traceback.format_exception(caught.value))
    assert INPUT_MARK not in printed and "input_value" not in printed
    assert "relations.0.kind" in printed
    assert isinstance(caught.value.__context__, ValidationError)


@pytest.mark.parametrize(
    "data",
    [None, [], 42, INPUT_MARK, [["id", INPUT_MARK]]],
    ids=["None", "список", "число", "строка", "список-пар"],
)
def test_adventure_from_data_rejects_what_is_not_a_mapping(data: Any):
    # Единый тип ошибки входа и для копии не той формы; список пар — тоже не объект приключения.
    with pytest.raises(AdventureFormatError) as caught:
        adventure_from_data(data)

    assert caught.value.problems == ["adventure: ожидался объект (словарь)"]
    assert INPUT_MARK not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "read_old",
    [
        pytest.param(lambda: load_adventure(V1_DEMO_DIR), id="старая папка"),
        pytest.param(
            lambda: adventure_from_data(json.loads(V1_GAME_COPY.read_text(encoding="utf-8"))),
            id="копия игры DND-1",
        ),
    ],
)
def test_old_entities_get_the_default_journal_fields(read_old):
    adventure = read_old()
    entities = [*adventure.locations.values(), *adventure.npcs.values(), *adventure.items.values()]

    assert entities
    assert all((entity.public, entity.known_at_start) == ("", False) for entity in entities)
    assert adventure.journal_intro == adventure.pitch


def test_the_journal_fields_of_the_demo_survive_the_game_copy(demo_adventure: Adventure):
    # Копия в игре пишется `model_dump(mode="json")` и хранится как JSON; читает её
    # `adventure_from_data`. В демо поля дневника заданы, поэтому равенство их проверяет.
    stored = json.loads(json.dumps(demo_adventure.model_dump(mode="json"), ensure_ascii=False))

    restored = adventure_from_data(stored)

    assert restored == demo_adventure
    assert restored.journal_intro != restored.pitch
    marta = restored.npcs["marta"]
    assert marta.known_at_start and marta.public
    assert [rumor.known_at_start for rumor in restored.world.rumors] == [True, False, False]


# --- папка v2: чтение и запись ---


def read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def relative_files(root: Path) -> set[str]:
    return {file.relative_to(root).as_posix() for file in root.rglob("*") if file.is_file()}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    target = tmp_path / "factory-crypt"
    dump_adventure(make_adventure(), target)
    return target


def problems_of(path: Path) -> list[str]:
    with pytest.raises(AdventureFormatError) as caught:
        load_adventure(path)
    return caught.value.problems


def test_dump_writes_the_v2_folder_layout(folder: Path):
    assert relative_files(folder) == {
        "adventure.yaml",
        "world.yaml",
        "relations.yaml",
        "deities/warden.yaml",
        "factions/candle-circle.yaml",
        "locations/village.yaml",
        "locations/cemetery.yaml",
        "locations/crypt.yaml",
        "npcs/marta.yaml",
        "npcs/fabian.yaml",
        "items/key.yaml",
        "items/potion.yaml",
        "encounters/bones.yaml",
        "revelations.yaml",
    }


def test_adventure_file_starts_with_the_format_version_and_keeps_v2_parts_apart(folder: Path):
    text = (folder / "adventure.yaml").read_text(encoding="utf-8")
    top = yaml.safe_load(text)

    assert text.startswith("format_version: 2\n")
    assert top["villain"] == "npc:fabian" and top["journal_intro"] == top["pitch"]
    assert not top.keys() & {"world", "relations", "deities", "factions"}


def test_world_and_relations_files_hold_the_world_and_the_graph(folder: Path):
    world = read(folder / "world.yaml")
    relations = read(folder / "relations.yaml")

    assert world["regions"][0]["id"] == "marsh" and world["rumors"][0]["true"] is False
    assert [entry["id"] for entry in relations] == [
        "fabian-debtor-marta",
        "warden-enemy-candle-circle",
        "marta-worships-warden",
        "fabian-seeks-crypt",
    ]


def test_npc_files_of_v2_have_no_relations_field(folder: Path):
    assert "relations" not in read(folder / "npcs/marta.yaml")


def test_a_v2_folder_reads_back_as_the_same_adventure(folder: Path):
    assert load_adventure(folder) == make_adventure()


def test_an_empty_world_and_graph_are_still_written_as_files(tmp_path: Path):
    data = adventure_data()
    for key in ("world", "relations", "deities", "factions", "villain"):
        del data[key]
    adventure = Adventure.model_validate(data)

    dump_adventure(adventure, tmp_path)

    assert read(tmp_path / "relations.yaml") == []
    assert read(tmp_path / "world.yaml") == {"history": "", "regions": [], "rumors": []}
    assert load_adventure(tmp_path) == adventure


def test_the_old_demo_is_written_back_as_v2(tmp_path: Path):
    adventure = load_adventure(V1_DEMO_DIR)

    dump_adventure(adventure, tmp_path)

    assert read(tmp_path / "adventure.yaml")["format_version"] == 2
    assert [entry["id"] for entry in read(tmp_path / "relations.yaml")] == [
        "marta-other-fabian",
        "fabian-debtor-marta",
    ]
    assert "relations" not in read(tmp_path / "npcs/marta.yaml")
    assert load_adventure(tmp_path) == adventure


@pytest.mark.parametrize("name", ["world.yaml", "relations.yaml"])
def test_world_and_relations_files_are_required_in_a_v2_folder(folder: Path, name: str):
    (folder / name).unlink()

    assert problems_of(folder) == [f"{name}: файл не найден"]


@pytest.mark.parametrize("version", ["3", "'2'", "1.5", "2.0", "1.0"])
def test_an_unknown_format_version_of_a_folder_is_one_problem_about_the_version(
    folder: Path, version: str
):
    # Неизвестная версия — не v2: файлы мира и графа не требуются, и отчёт не засоряется лишним
    # «файл не найден» — автор видит одну настоящую причину. `2.0` — тоже не версия 2.
    path = folder / "adventure.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "format_version: 2", f"format_version: {version}"
    )
    path.write_text(text, encoding="utf-8")
    (folder / "world.yaml").unlink()
    (folder / "relations.yaml").unlink()

    assert problems_of(folder) == [
        "adventure.yaml, поле format_version: Value error, версия формата — целое число 1 или 2 "
        "(без поля — 1)"
    ]


def test_empty_deities_and_factions_folders_mean_no_deities_and_factions(tmp_path: Path):
    adventure = make_adventure(deities={}, factions={})
    dump_adventure(adventure, tmp_path)
    (tmp_path / "deities").mkdir()
    (tmp_path / "factions").mkdir()

    loaded = load_adventure(tmp_path)

    assert (loaded.deities, loaded.factions) == ({}, {})
    assert loaded == adventure


def test_a_secret_dc_outside_the_table_is_a_problem_of_its_location_file(folder: Path):
    path = folder / "locations/crypt.yaml"
    location = read(path)
    location["secrets"][0]["check"]["dc"] = 12
    path.write_text(yaml.safe_dump(location, allow_unicode=True), encoding="utf-8")

    [problem] = problems_of(folder)

    assert problem.startswith("locations/crypt.yaml, поле secrets.0.check.dc:")
    assert "5, 10, 15, 20, 25, 30" in problem


def test_relations_entries_are_checked_with_their_number(folder: Path):
    entries = read(folder / "relations.yaml")
    entries[1]["kind"] = "creditor"
    (folder / "relations.yaml").write_text(yaml.safe_dump(entries, allow_unicode=True))

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("relations.yaml, запись 2, поле kind:")


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("relations.yaml", "a: 1\n", "relations.yaml: ожидался список"),
        ("relations.yaml", "- 1\n", "relations.yaml, запись 1: ожидался объект (словарь)"),
        ("world.yaml", "- 1\n", "world.yaml: ожидался объект (словарь)"),
    ],
)
def test_world_and_relations_files_must_have_their_shape(
    folder: Path, name: str, content: str, expected: str
):
    (folder / name).write_text(content, encoding="utf-8")

    assert problems_of(folder) == [expected]


def test_a_rumor_written_by_hand_with_a_bare_true_key_is_read(folder: Path):
    # YAML читает ключ `true:` без кавычек как логическое значение, а не строку: автор пишет
    # слух по спеке (`text`, `true`) и не должен знать про кавычки.
    (folder / "world.yaml").write_text(
        "history: Давно.\nrumors:\n  - text: Клад есть.\n    true: false\n", encoding="utf-8"
    )

    [rumor] = load_adventure(folder).world.rumors

    assert (rumor.text, rumor.true) == ("Клад есть.", False)


def test_a_world_file_schema_error_names_the_file_and_the_field(folder: Path):
    world = read(folder / "world.yaml")
    world["regions"][0]["surprise"] = 1
    (folder / "world.yaml").write_text(yaml.safe_dump(world, allow_unicode=True))

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("world.yaml, поле regions.0.surprise:")


@pytest.mark.parametrize("key", ["world", "relations", "deities", "factions"])
def test_v2_parts_cannot_live_in_the_adventure_file(folder: Path, key: str):
    path = folder / "adventure.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"{key}: []\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1
    assert problems[0].startswith("adventure.yaml:") and key in problems[0]


@pytest.mark.parametrize(
    ("subfolder", "entity_id"), [("deities", "warden"), ("factions", "candle-circle")]
)
def test_a_deity_or_faction_file_name_must_match_its_id(
    folder: Path, subfolder: str, entity_id: str
):
    (folder / subfolder / f"{entity_id}.yaml").rename(folder / subfolder / "renamed.yaml")

    problems = problems_of(folder)

    assert any(p.startswith(f"{subfolder}/renamed.yaml: id") for p in problems), problems


def test_relations_inside_an_npc_file_of_a_v2_folder_are_an_error(folder: Path):
    path = folder / "npcs/marta.yaml"
    npc = read(path) | {"relations": [{"npc_id": "fabian", "kind": "enemy"}]}
    path.write_text(yaml.safe_dump(npc, allow_unicode=True), encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and "relations.yaml" in problems[0]
