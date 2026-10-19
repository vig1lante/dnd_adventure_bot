"""Загрузка и сохранение папки приключения (спека §6.1): структура, ошибки, безопасность."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dnd_engine.adventure.loader import AdventureFormatError, dump_adventure, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import load_srd

TRICKY_TEXT = (
    'Фабиан: «долг» — "да" # не комментарий; yes; null; 12:30; - не список\n\tвторая строка 🦴 '
)


def adventure_data() -> dict[str, Any]:
    """Приключение «сырыми» данными: все коллекции заполнены, тексты с коварными символами."""
    skeleton = load_srd().monsters["skeleton"]
    return {
        "id": "demo-crypt",
        "title": "Склеп у деревни",
        "pitch": TRICKY_TEXT,
        "tone": "мрачная сказка",
        "target_hours": 1.5,
        "rules": {"modules": ["campfire"]},
        "start_location": "village",
        "finale_location": "crypt",
        "acts": [
            {"id": "act-1", "title": "Деревня", "summary": "Завязка", "key_scenes": ["трактир"]}
        ],
        "endings": ["Смотритель спасён", "Смотритель погиб"],
        "locations": {
            "village": {
                "id": "village",
                "name": "Деревня",
                "read_aloud": TRICKY_TEXT,
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
                        "description": "Потрёпанный дневник.",
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
                "appearance": "Крепкая женщина.",
                "manner": "говорит коротко",
                "motive": "найти брата",
                "attitude": -2,
                "relations": [{"npc_id": "fabian", "kind": "enemy"}],
                "location_id": "village",
            }
        },
        # Id, которые YAML без кавычек прочёл бы как bool или число: проверяют кавычки при записи.
        "items": {
            "key": {"id": "key", "name": "Ключ", "description": "Ржавый.", "holder": "gone"},
            "yes": {"id": "yes", "name": "Да", "description": "Д.", "holder": "gone"},
            "007": {"id": "007", "name": "Агент", "description": "А.", "holder": "gone"},
        },
        "encounters": {
            "bones": {
                "id": "bones",
                "location_id": "village",
                "trigger": "игроки ступили на кладбище",
                "tactics": "скелеты идут к ближайшему",
                "monsters": [{"monster_id": "crypt-bones", "count": 3}],
            }
        },
        "revelations": {
            "fabian": {
                "id": "fabian",
                "statement": "Смотрителя похитил Фабиан.",
                "clues": [
                    {"id": "clue-1", "description": "Запись о долге.", "where": "feature:diary"}
                ],
            },
            "debt": {
                "id": "debt",
                "statement": "У Фабиана долг.",
                "clues": [{"id": "clue-2", "description": "Расписка.", "where": "npc:marta"}],
            },
        },
        "monsters": {
            "crypt-bones": skeleton.model_copy(update={"id": "crypt-bones"}).model_dump(mode="json")
        },
        "seeds": ["marta"],
    }


@pytest.fixture
def adventure() -> Adventure:
    return Adventure.model_validate(adventure_data())


@pytest.fixture
def folder(tmp_path: Path, adventure: Adventure) -> Path:
    """Папка с записанным приключением, которую тест портит по своему усмотрению."""
    target = tmp_path / "demo-crypt"
    dump_adventure(adventure, target)
    return target


def problems_of(path: Path) -> list[str]:
    with pytest.raises(AdventureFormatError) as caught:
        load_adventure(path)
    return caught.value.problems


def edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
    """Читает YAML-файл, правит данные на месте и записывает обратно."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def relative_files(root: Path) -> set[str]:
    return {file.relative_to(root).as_posix() for file in root.rglob("*") if file.is_file()}


# --- сохранение и чтение ---


def test_dump_then_load_returns_equal_adventure(adventure: Adventure, folder: Path):
    assert load_adventure(folder) == adventure


def test_dump_writes_the_documented_folder_layout(folder: Path):
    assert relative_files(folder) == {
        "adventure.yaml",
        "locations/village.yaml",
        "locations/crypt.yaml",
        "npcs/marta.yaml",
        "items/key.yaml",
        "items/yes.yaml",
        "items/007.yaml",
        "encounters/bones.yaml",
        "monsters/crypt-bones.yaml",
        "revelations.yaml",
        "seeds.yaml",
    }


def test_adventure_file_holds_everything_except_collections(folder: Path):
    top = yaml.safe_load((folder / "adventure.yaml").read_text(encoding="utf-8"))

    assert top["id"] == "demo-crypt"
    assert top["rules"] == {"base": "srd-5.2", "modules": ["campfire"]}
    assert not top.keys() & {"locations", "npcs", "items", "encounters", "revelations", "monsters"}
    assert "seeds" not in top


def test_revelations_file_is_a_list_and_seeds_file_is_a_list_of_ids(folder: Path):
    revelations = yaml.safe_load((folder / "revelations.yaml").read_text(encoding="utf-8"))
    seeds = yaml.safe_load((folder / "seeds.yaml").read_text(encoding="utf-8"))

    assert [revelation["id"] for revelation in revelations] == ["fabian", "debt"]
    assert seeds == ["marta"]


def test_dump_keeps_unicode_readable_and_field_order(folder: Path):
    text = (folder / "adventure.yaml").read_text(encoding="utf-8")

    assert "Склеп у деревни" in text  # allow_unicode: без \u-последовательностей
    assert text.startswith("id: demo-crypt\n")  # sort_keys=False: поля в порядке схемы


def test_seeds_file_is_written_only_when_there_are_seeds(tmp_path: Path):
    data = adventure_data() | {"seeds": []}
    target = tmp_path / "no-seeds"

    dump_adventure(Adventure.model_validate(data), target)

    assert not (target / "seeds.yaml").exists()
    assert load_adventure(target).seeds == ()


def test_dumping_again_overwrites_files(adventure: Adventure, folder: Path):
    changed = adventure.model_copy(update={"title": "Другое название"})

    dump_adventure(changed, folder)

    assert load_adventure(folder).title == "Другое название"


def test_optional_files_and_folders_may_be_missing(tmp_path: Path):
    empty = {
        "npcs": {},
        "items": {},
        "encounters": {},
        "revelations": {},
        "monsters": {},
        "seeds": [],
    }
    target = tmp_path / "small"
    dump_adventure(Adventure.model_validate(adventure_data() | empty), target)

    loaded = load_adventure(target)

    # Пустые коллекции не создают папок (revelations.yaml лежит всегда), но читаются как пустые.
    assert not (target / "npcs").exists()
    assert loaded.npcs == {} and loaded.monsters == {} and loaded.seeds == ()
    assert set(loaded.locations) == {"village", "crypt"}


# --- ошибки формата ---


def test_error_is_a_value_error_with_all_problems_in_text(folder: Path):
    (folder / "locations" / "village.yaml").write_text("id: [", encoding="utf-8")

    with pytest.raises(ValueError, match="village.yaml") as caught:
        load_adventure(folder)

    assert isinstance(caught.value, AdventureFormatError)
    assert all(problem in str(caught.value) for problem in caught.value.problems)


def test_missing_folder_is_reported(tmp_path: Path):
    problems = problems_of(tmp_path / "nowhere")

    assert len(problems) == 1 and "nowhere" in problems[0]


@pytest.mark.parametrize("name", ["adventure.yaml", "revelations.yaml"])
def test_required_file_missing_is_reported_with_its_name(folder: Path, name: str):
    (folder / name).unlink()

    problems = problems_of(folder)

    assert any(problem.startswith(f"{name}:") and "не найден" in problem for problem in problems)


def test_file_name_must_match_id_inside(folder: Path):
    (folder / "locations" / "crypt.yaml").rename(folder / "locations" / "tomb.yaml")

    problems = problems_of(folder)

    assert len(problems) == 1
    assert "locations/tomb.yaml" in problems[0]
    assert "crypt" in problems[0] and "не совпадает" in problems[0]


@pytest.mark.parametrize("subfolder", ["npcs", "items", "encounters", "monsters"])
def test_file_name_must_match_id_in_every_collection(folder: Path, subfolder: str):
    source = next((folder / subfolder).glob("*.yaml"))
    source.rename(source.with_name("renamed.yaml"))

    problems = problems_of(folder)

    assert any(f"{subfolder}/renamed.yaml" in problem for problem in problems)


def test_broken_yaml_is_reported_with_path_and_line(folder: Path):
    (folder / "npcs" / "marta.yaml").write_text("id: marta\nname: [\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1
    assert problems[0].startswith("npcs/marta.yaml:")
    assert "строка" in problems[0]


def test_invalid_utf8_is_reported_with_path(folder: Path):
    (folder / "items" / "key.yaml").write_bytes(b"id: key\nname: \xff\xfe\n")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("items/key.yaml:")


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "просто текст\n"])
def test_entity_file_must_be_an_object(folder: Path, content: str):
    (folder / "items" / "key.yaml").write_text(content, encoding="utf-8")

    problems = problems_of(folder)

    assert problems == ["items/key.yaml: ожидался объект (словарь)"]


def test_schema_error_names_file_and_field(folder: Path):
    edit_yaml(folder / "locations" / "village.yaml", lambda data: data["exits"][0].pop("to"))

    problems = problems_of(folder)

    assert len(problems) == 1
    assert problems[0].startswith("locations/village.yaml, поле exits.0.to:")


def test_extra_field_in_a_file_is_an_error_with_path(folder: Path):
    path = folder / "npcs" / "marta.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "surprise: 1\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and "npcs/marta.yaml" in problems[0] and "surprise" in problems[0]


def test_dc_outside_table_in_a_file_is_an_error(folder: Path):
    edit_yaml(
        folder / "locations" / "village.yaml",
        lambda data: data["features"][0]["check"].update(dc=13),
    )

    problems = problems_of(folder)

    assert len(problems) == 1 and "locations/village.yaml" in problems[0]
    assert "5, 10, 15, 20, 25, 30" in problems[0]


def test_adventure_file_schema_error_names_adventure_file(folder: Path):
    edit_yaml(folder / "adventure.yaml", lambda data: data.pop("title"))

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("adventure.yaml, поле title:")


@pytest.mark.parametrize(
    "key", ["locations", "npcs", "items", "encounters", "revelations", "monsters", "seeds"]
)
def test_collections_cannot_live_in_adventure_file(folder: Path, key: str):
    path = folder / "adventure.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"{key}: []\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1
    assert problems[0].startswith("adventure.yaml:") and key in problems[0]


def test_errors_from_many_files_are_collected_together(folder: Path):
    (folder / "npcs" / "marta.yaml").write_text("id: marta\nname: [\n", encoding="utf-8")
    (folder / "locations" / "crypt.yaml").rename(folder / "locations" / "tomb.yaml")
    (folder / "items" / "key.yaml").write_text("id: key\n", encoding="utf-8")
    (folder / "revelations.yaml").write_text("not: a list\n", encoding="utf-8")
    (folder / "seeds.yaml").write_text("- 1\n", encoding="utf-8")
    edit_yaml(folder / "adventure.yaml", lambda data: data.pop("title"))

    problems = problems_of(folder)

    for name in (
        "npcs/marta.yaml",
        "locations/tomb.yaml",
        "items/key.yaml",
        "revelations.yaml",
        "seeds.yaml",
        "adventure.yaml",
    ):
        assert any(name in problem for problem in problems), (name, problems)


def test_several_errors_in_one_file_are_all_reported(folder: Path):
    (folder / "items" / "key.yaml").write_text("id: key\n", encoding="utf-8")

    problems = problems_of(folder)

    # name, description и holder обязательны: все три — в одном ответе, а не по одной за запуск.
    assert len(problems) == 3
    assert all(problem.startswith("items/key.yaml, поле ") for problem in problems)


def test_revelations_list_errors_name_the_entry(folder: Path):
    def spoil(entries: list[Any]) -> None:
        del entries[1]["statement"]
        entries += [entries[0], "строка"]

    edit_yaml(folder / "revelations.yaml", spoil)

    problems = problems_of(folder)

    assert any("запись 2" in p and "statement" in p for p in problems)
    assert any("запись 3" in p and "дубликат" in p for p in problems)
    assert any("запись 4" in p and "ожидался объект" in p for p in problems)
    assert all(p.startswith("revelations.yaml") for p in problems)


@pytest.mark.parametrize("content", ["a: 1\n", "- 1\n", "- ok\n- [nested]\n"])
def test_seeds_file_must_be_a_list_of_strings(folder: Path, content: str):
    (folder / "seeds.yaml").write_text(content, encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("seeds.yaml: ожидался список")


def test_empty_seeds_list_is_fine(folder: Path):
    (folder / "seeds.yaml").write_text("[]\n", encoding="utf-8")

    assert load_adventure(folder).seeds == ()


# --- безопасность загрузки ---


def test_unsafe_yaml_tag_is_rejected_and_not_executed(folder: Path, tmp_path: Path):
    marker = tmp_path / "executed"
    # Небезопасный загрузчик выполнил бы вызов и создал бы файл-маркер.
    payload = f"!!python/object/apply:builtins.open [{marker}, w]\n"
    (folder / "items" / "key.yaml").write_text(payload, encoding="utf-8")

    problems = problems_of(folder)

    assert not marker.exists()
    assert len(problems) == 1 and problems[0].startswith("items/key.yaml:")


def test_alias_bomb_is_rejected_by_the_schema_quickly(folder: Path):
    layers = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    layers += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 12)]
    bomb = "\n".join(layers) + "\nid: key\nname: *a11\ndescription: d\nholder: gone\n"
    (folder / "items" / "key.yaml").write_text(bomb, encoding="utf-8")

    problems = problems_of(folder)

    # Поле `name` — строка: вложенный список отвергается сразу, без обхода 10^12 элементов.
    assert any("items/key.yaml, поле name" in problem for problem in problems)
    assert all(len(problem) < 500 for problem in problems)  # значение не попадает в сообщение


# --- безопасность записи ---

UNSAFE_NAMES = ["../../escape", "/tmp/escape", "a/b", "Bad Id", "", ".", "..", "x" * 65, "ключ"]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
@pytest.mark.parametrize("collection", ["locations", "npcs", "items", "encounters", "monsters"])
def test_dump_refuses_unsafe_file_names_and_writes_nothing(
    tmp_path: Path, adventure: Adventure, collection: str, name: str
):
    entity = next(iter(getattr(adventure, collection).values()))
    # `model_copy(update=...)` не проверяет значения: так в приключение попадает ключ, который
    # схема отвергла бы, — запись должна защищаться сама, а не полагаться на схему.
    hostile = adventure.model_copy(update={collection: {name: entity}})
    target = tmp_path / "adv"

    with pytest.raises(AdventureFormatError) as caught:
        dump_adventure(hostile, target)

    assert collection in caught.value.problems[0]
    assert list(tmp_path.iterdir()) == []  # ни папки приключения, ни файла рядом с ней


def test_dump_reports_every_unsafe_name_at_once(tmp_path: Path, adventure: Adventure):
    entity = next(iter(adventure.items.values()))
    hostile = adventure.model_copy(
        update={
            "items": {"../one": entity},
            "npcs": {"../two": next(iter(adventure.npcs.values()))},
        }
    )

    with pytest.raises(AdventureFormatError) as caught:
        dump_adventure(hostile, tmp_path / "adv")

    assert len(caught.value.problems) == 2


def test_dump_does_not_write_when_a_later_collection_is_unsafe(
    tmp_path: Path, adventure: Adventure
):
    # Проверка имён идёт до первой записи: безопасные коллекции тоже не остаются на диске.
    hostile = adventure.model_copy(
        update={"monsters": {"../../x": next(iter(adventure.monsters.values()))}}
    )

    with pytest.raises(AdventureFormatError):
        dump_adventure(hostile, tmp_path / "adv")

    assert not (tmp_path / "adv").exists()


def test_loaded_ids_cannot_point_outside_the_folder(folder: Path):
    # Чтение: id `../x` в файле не совпадает с именем файла и отвергается.
    (folder / "npcs" / "marta.yaml").write_text(
        (folder / "npcs" / "marta.yaml")
        .read_text(encoding="utf-8")
        .replace("id: marta", "id: ../x"),
        encoding="utf-8",
    )

    problems = problems_of(folder)

    assert any("npcs/marta.yaml" in problem for problem in problems)
