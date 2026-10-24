"""Загрузка и сохранение папки приключения (спека §6.1, формат v2 — спека DND-2 §8): структура,
ошибки, безопасность. Перевод v1 → v2 и файлы v2 по смыслу проверяет `test_adventure_v2`; здесь
новые файлы и папки v2 проходят те же проверки безопасности, что и прежние."""

import copy
import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from dnd_engine.adventure.loader import AdventureFormatError, dump_adventure, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import MAX_PROBLEM_LENGTH, MAX_YAML_BYTES, load_srd

TRICKY_TEXT = (
    'Фабиан: «долг» — "да" # не комментарий; yes; null; 12:30; - не список\n\tвторая строка 🦴 '
)


def adventure_data() -> dict[str, Any]:
    """Приключение «сырыми» данными: все коллекции заполнены, тексты с коварными символами."""
    skeleton = load_srd().monsters["skeleton"]
    return {
        "format_version": 2,
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
                "location_id": "village",
            }
        },
        "world": {
            "history": TRICKY_TEXT,
            "regions": [{"id": "marsh", "name": "Болото", "description": TRICKY_TEXT}],
            "rumors": [{"text": TRICKY_TEXT, "true": True}],
        },
        "deities": {
            "hearth": {
                "id": "hearth",
                "name": "Хозяйка очага",
                "title": "Богиня",
                "domains": ["очаг"],
                "symbol": "кружка",
                "dogma": TRICKY_TEXT,
                "appearance": "Тёплая.",
                "attitude_to_mortals": "добра",
            }
        },
        "factions": {
            "guild": {
                "id": "guild",
                "name": "Гильдия",
                "goal": "Цель.",
                "methods": TRICKY_TEXT,
                "leader": "npc:marta",
            }
        },
        "relations": [
            {"id": "marta-enemy-fabian", "a": "npc:marta", "b": "npc:fabian", "kind": "enemy"}
        ],
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
        "world.yaml",
        "relations.yaml",
        "deities/hearth.yaml",
        "factions/guild.yaml",
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
    assert not top.keys() & {"world", "relations", "deities", "factions"}
    assert "seeds" not in top


def test_revelations_file_is_a_list_and_seeds_file_is_a_list_of_ids(folder: Path):
    revelations = yaml.safe_load((folder / "revelations.yaml").read_text(encoding="utf-8"))
    seeds = yaml.safe_load((folder / "seeds.yaml").read_text(encoding="utf-8"))

    assert [revelation["id"] for revelation in revelations] == ["fabian", "debt"]
    assert seeds == ["marta"]


def test_dump_keeps_unicode_readable_and_field_order(folder: Path):
    text = (folder / "adventure.yaml").read_text(encoding="utf-8")

    assert "Склеп у деревни" in text  # allow_unicode: без \u-последовательностей
    # sort_keys=False: поля в порядке схемы, версия формата — первой строкой.
    assert text.startswith("format_version: 2\nid: demo-crypt\n")


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
        "deities": {},
        "factions": {},
        "seeds": [],
    }
    target = tmp_path / "small"
    dump_adventure(Adventure.model_validate(adventure_data() | empty), target)

    loaded = load_adventure(target)

    # Пустые коллекции не создают папок (revelations.yaml лежит всегда), но читаются как пустые.
    assert not (target / "npcs").exists() and not (target / "deities").exists()
    assert loaded.npcs == {} and loaded.monsters == {} and loaded.seeds == ()
    assert loaded.deities == {} and loaded.factions == {}
    assert set(loaded.locations) == {"village", "crypt"}


# --- ошибки формата ---


def test_error_is_a_value_error_with_all_problems_in_text(folder: Path):
    (folder / "locations" / "village.yaml").write_text("id: [", encoding="utf-8")

    with pytest.raises(ValueError, match=r"village\.yaml") as caught:
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
        # Копия, а не та же запись: `safe_dump` записал бы повтор объекта якорем, а якоря запрещены.
        entries += [copy.deepcopy(entries[0]), "строка"]

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


# --- значения, которые YAML не может построить ---
# `yaml.safe_load` бросает не только `YAMLError`: конструкторы значений на чужом вводе падают чем
# попало. Целое длиннее 4300 цифр, несуществующая дата, `!!int abc` и смещение пояса вне суток дают
# `ValueError`; `!!int ""` — `IndexError`; `!!bool maybe` — `KeyError`; `!!timestamp abc` —
# `AttributeError`; слишком глубокая вложенность — `RecursionError`. Файл портит владелец или
# модель, а вызывающий должен получить отчёт с путём файла, а не необработанное исключение.

UNBUILDABLE_VALUES = {
    "число-длиннее-4300-цифр": "9" * 4301,
    "несуществующая-дата": "2023-02-30",
    "несуществующее-время": "2023-02-28 25:61:00",
    "int-пустая-строка": '!!int ""',
    "float-пустая-строка": '!!float ""',
    "bool-не-литерал": "!!bool maybe",
    "timestamp-не-дата": "!!timestamp abc",
    "int-не-число": "!!int abc",
    "float-не-число": "!!float abc",
    "смещение-пояса-вне-суток": "2001-12-14t21:59:43.10+99:00",
}
ENTITY_FOLDERS = ["locations", "npcs", "items", "encounters", "monsters", "deities", "factions"]


def write_file_with(folder: Path, name: str, value: str) -> None:
    """Файл `name` с одним значением `value`, формой подходящий своему месту в папке."""
    is_list = name in ("revelations.yaml", "seeds.yaml", "relations.yaml")
    content = f"- {value}\n" if is_list else f"id: x\nattitude: {value}\n"
    (folder / name).write_text(content, encoding="utf-8")


ROOT_FILES = ["adventure.yaml", "revelations.yaml", "seeds.yaml", "world.yaml", "relations.yaml"]


@pytest.mark.parametrize("value", UNBUILDABLE_VALUES.values(), ids=UNBUILDABLE_VALUES.keys())
@pytest.mark.parametrize("name", [*ROOT_FILES, "npcs/marta.yaml", "deities/hearth.yaml"])
def test_a_value_yaml_cannot_build_is_reported_with_the_file_path(
    folder: Path, name: str, value: str
):
    write_file_with(folder, name, value)

    problems = problems_of(folder)

    # Сообщение общее для любой причины (она зависит от парсера): без значения из файла.
    assert len(problems) == 1 and problems[0].startswith(f"{name}: значение не построить")
    assert len(problems[0]) < 300 and value not in problems[0]


@pytest.mark.parametrize("name", [*ROOT_FILES, "npcs/marta.yaml", "factions/guild.yaml"])
def test_too_deep_nesting_is_reported_with_the_file_path(folder: Path, name: str):
    (folder / name).write_text("[" * 5000 + "]" * 5000, encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}:")


# --- файлы в папках сущностей, которые загрузчик не читает ---
# Читаются только `*.yaml`. Файл с другим именем (`crypt.yml`, `marta.json`) молча терял бы
# сущность, у которой нет ссылок на неё, поэтому он — проблема с путём. Скрытые файлы служебные
# (`.DS_Store` macOS, `._crypt.yaml` от архиваторов, `.#crypt.yaml` от редакторов) и игнорируются.


@pytest.mark.parametrize(
    "name",
    [
        "locations/crypt.yml",
        "npcs/marta.json",
        "items/key.yaml.bak",
        "encounters/notes.txt",
        "monsters/crypt-bones.yaml~",
        "locations/README",
    ],
)
def test_a_file_that_is_not_yaml_in_an_entity_folder_is_reported_with_its_path(
    folder: Path, name: str
):
    (folder / name).write_text("id: x\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}:")
    assert ".yaml" in problems[0]


@pytest.mark.parametrize("subfolder", ENTITY_FOLDERS)
def test_an_upper_case_extension_is_an_unread_file_not_a_hidden_entity(
    folder: Path, subfolder: str
):
    # `crypt.YAML` читают не все файловые системы одинаково: загрузчик не угадывает, а называет
    # файл. Имя без пары среди настоящих файлов, чтобы тест не зависел от регистра файловой системы.
    name = f"{subfolder}/extra-entity.YAML"
    (folder / name).write_text("id: extra-entity\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}:")


def test_a_renamed_entity_file_is_reported_instead_of_vanishing_silently(folder: Path):
    # Сценарий ручной проверки: `crypt.yaml` → `crypt.yml`. Загрузчик не видит склеп, а валидатор
    # заметит его только через ссылки; сущность без ссылок пропала бы без следа.
    (folder / "locations" / "crypt.yaml").rename(folder / "locations" / "crypt.yml")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("locations/crypt.yml:")


def test_every_unread_file_is_reported_at_once_with_the_other_problems(folder: Path):
    (folder / "locations" / "crypt.yml").write_text("id: crypt\n", encoding="utf-8")
    (folder / "npcs" / "marta.json").write_text("{}", encoding="utf-8")
    (folder / "items" / "key.yaml").write_text("id: [", encoding="utf-8")

    problems = problems_of(folder)

    assert sorted(problem.partition(":")[0] for problem in problems) == [
        "items/key.yaml",
        "locations/crypt.yml",
        "npcs/marta.json",
    ]


@pytest.mark.parametrize("name", [".DS_Store", "._crypt.yaml", ".#crypt.yaml", ".gitkeep"])
@pytest.mark.parametrize("subfolder", ENTITY_FOLDERS)
def test_hidden_files_in_entity_folders_are_ignored(
    folder: Path, adventure: Adventure, subfolder: str, name: str
):
    (folder / subfolder / name).write_bytes(b"\x00\x05\x16\x07 \xff\xfe not yaml")

    assert load_adventure(folder) == adventure


def test_a_hidden_folder_in_an_entity_folder_is_ignored(folder: Path, adventure: Adventure):
    (folder / "locations" / ".cache").mkdir()
    (folder / "locations" / ".cache" / "crypt.yaml").write_text("id: [", encoding="utf-8")

    assert load_adventure(folder) == adventure


def test_a_subfolder_in_an_entity_folder_is_reported_with_its_path(folder: Path):
    (folder / "locations" / "old").mkdir()
    (folder / "locations" / "old" / "crypt.yaml").write_text("id: crypt\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("locations/old:")
    assert "папк" in problems[0]


def test_a_folder_named_like_a_yaml_file_is_a_read_error_with_its_own_reason(folder: Path):
    (folder / "locations" / "village.yaml").unlink()
    (folder / "locations" / "village.yaml").mkdir()

    problems = problems_of(folder)

    assert problems == ["locations/village.yaml: не удалось прочитать файл (не обычный файл)"]


# Файл особого вида под именем `*.yaml` (канал FIFO, устройство) размера не имеет, предел размера
# его не ограничивает, а чтение канала без писателя не заканчивается. Загрузчик отказывает такому
# файлу по виду, не открывая его.

needs_fifo = pytest.mark.skipif(
    not (hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM")), reason="нужны FIFO и SIGALRM"
)


@pytest.fixture
def fail_if_reading_hangs() -> Iterator[None]:
    """Зависшее чтение канала роняет тест через пять секунд, а не подвешивает прогон."""

    def give_up(signum: int, frame: object) -> None:
        raise TimeoutError("чтение канала не закончилось")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@needs_fifo
@pytest.mark.parametrize(
    "name", ["adventure.yaml", "revelations.yaml", "seeds.yaml", "npcs/marta.yaml"]
)
def test_a_pipe_under_a_yaml_name_is_refused_without_being_read(
    folder: Path, fail_if_reading_hangs: None, name: str
):
    (folder / name).unlink(missing_ok=True)
    os.mkfifo(folder / name)

    problems = problems_of(folder)

    assert problems == [f"{name}: не удалось прочитать файл (не обычный файл)"]


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="нет /dev/null")
def test_a_symlink_to_a_device_is_refused_as_a_symlink_before_it_is_looked_at(folder: Path):
    (folder / "items" / "key.yaml").unlink()
    (folder / "items" / "key.yaml").symlink_to("/dev/null")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("items/key.yaml: символическая ссылка")


# --- симлинки в папке приключения ---
# Симлинк `*.yaml` может указывать на файл вне папки (например, из присланного архива), и загрузчик
# прочёл бы его как свой. Файлы приключения лежат прямо в его папке, поэтому любой симлинк — на
# файл или на папку сущностей, рабочий или битый, наружу или внутрь — это проблема с путём, а цель
# не читается.

SYMLINK_FILES = [
    "adventure.yaml",
    "revelations.yaml",
    "seeds.yaml",
    "world.yaml",
    "relations.yaml",
    "deities/hearth.yaml",
    "factions/guild.yaml",
    "locations/village.yaml",
    "npcs/marta.yaml",
    "items/key.yaml",
    "encounters/bones.yaml",
    "monsters/crypt-bones.yaml",
]


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Пути, которые читали через `Path.read_bytes` после начала теста."""
    seen: list[Path] = []
    original = Path.read_bytes

    def spy(self: Path) -> bytes:
        seen.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    return seen


@pytest.mark.parametrize("name", SYMLINK_FILES)
def test_a_symlink_to_a_file_outside_the_folder_is_refused_and_not_read(
    folder: Path, tmp_path: Path, reads: list[Path], name: str
):
    outside = tmp_path / "outside" / "stolen.yaml"
    outside.parent.mkdir()
    # Во внешнем файле настоящее содержимое: прочти его загрузчик, папка загрузилась бы без проблем.
    (folder / name).replace(outside)
    (folder / name).symlink_to(outside)

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}: символическая ссылка")
    assert folder / name not in reads and outside not in reads


def test_a_symlink_to_a_file_inside_the_folder_is_refused_too(folder: Path, reads: list[Path]):
    (folder / "npcs" / "alias.yaml").symlink_to("marta.yaml")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("npcs/alias.yaml: символическая ссылка")
    assert folder / "npcs" / "alias.yaml" not in reads


@pytest.mark.parametrize("name", [*ROOT_FILES, "npcs/x.yaml", "deities/x.yaml"])
def test_a_dangling_symlink_is_a_symlink_problem_not_a_missing_or_skipped_file(
    folder: Path, name: str
):
    (folder / name).unlink(missing_ok=True)
    (folder / name).symlink_to(folder / "нет-такого-файла.yaml")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}: символическая ссылка")


@pytest.mark.parametrize("subfolder", ENTITY_FOLDERS)
def test_a_symlink_instead_of_an_entity_folder_is_refused_and_not_listed(
    folder: Path, tmp_path: Path, reads: list[Path], subfolder: str
):
    outside = tmp_path / "outside"
    (folder / subfolder).replace(outside)
    (folder / subfolder).symlink_to(outside, target_is_directory=True)

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{subfolder}: символическая ссылка")
    assert all(folder / subfolder not in path.parents for path in reads)


def test_a_symlinked_entity_file_is_not_the_only_thing_reported(folder: Path, tmp_path: Path):
    """Остальные файлы читаются как обычно: симлинк не прячет прочие проблемы папки."""
    outside = tmp_path / "outside.yaml"
    (folder / "npcs" / "marta.yaml").replace(outside)
    (folder / "npcs" / "marta.yaml").symlink_to(outside)
    (folder / "items" / "key.yaml").write_text("id: key\n", encoding="utf-8")

    problems = problems_of(folder)

    assert any(p.startswith("npcs/marta.yaml: символическая ссылка") for p in problems)
    assert any(p.startswith("items/key.yaml") and "поле" in p for p in problems)


def test_the_folder_of_the_adventure_itself_may_be_a_symlink(
    folder: Path, tmp_path: Path, adventure: Adventure
):
    """Запрет симлинков касается файлов внутри папки: саму папку выбирает вызывающий."""
    link = tmp_path / "link-to-the-adventure"
    link.symlink_to(folder, target_is_directory=True)

    assert load_adventure(link) == adventure


def test_a_missing_or_empty_entity_folder_is_still_fine(folder: Path, adventure: Adventure):
    for file in (folder / "monsters").iterdir():
        file.unlink()
    (folder / "monsters").rmdir()
    (folder / "encounters" / "bones.yaml").unlink()

    loaded = load_adventure(folder)

    assert loaded.monsters == {} and loaded.encounters == {}
    assert loaded.locations == adventure.locations


# --- безопасность загрузки ---


def test_unsafe_yaml_tag_is_rejected_and_not_executed(folder: Path, tmp_path: Path):
    marker = tmp_path / "executed"
    # Небезопасный загрузчик выполнил бы вызов и создал бы файл-маркер.
    payload = f"!!python/object/apply:builtins.open [{marker}, w]\n"
    (folder / "items" / "key.yaml").write_text(payload, encoding="utf-8")

    problems = problems_of(folder)

    assert not marker.exists()
    assert len(problems) == 1 and problems[0].startswith("items/key.yaml:")


def test_alias_bomb_is_rejected_before_anything_is_expanded(folder: Path):
    layers = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    layers += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 12)]
    bomb = "\n".join(layers) + "\nid: key\nname: *a11\ndescription: d\nholder: gone\n"
    (folder / "items" / "key.yaml").write_text(bomb, encoding="utf-8")

    problems = problems_of(folder)

    # Якоря запрещены целиком: файл не разворачивается и до проверки схемы не доходит.
    assert len(problems) == 1 and problems[0].startswith("items/key.yaml: некорректный YAML")
    assert "якор" in problems[0] and "алиас" in problems[0]


# --- якоря и алиасы YAML ---
# Алиас `*a` отдаёт тот же объект, который схема потом проверяет заново на каждом упоминании,
# поэтому файл в несколько КБ превращается в миллионы моделей (K алиасов на K алиасов даёт K²
# объектов). Запись якорей не пишет (`dump_adventure` их не создаёт), рукописным приключениям они
# не нужны, поэтому запрещены якорь и алиас, а значит и слияние `<<: *якорь`. Слияние со встроенным
# словарём (`<<: {a: 1}`) разрешено: без алиаса оно линейно и бомбы не строит.

ANCHOR_DOCUMENTS = {
    "алиас": "x: &a 1\ny: *a\n",
    "якорь-без-алиаса": "x: &a 1\n",
    "якорь-на-списке": "x: &a [1, 2]\n",
    "ключ-слияния": "base: &b {a: 1}\nmerged:\n  <<: *b\n",
    "алиас-без-якоря": "x: *missing\n",
}


@pytest.mark.parametrize("document", ANCHOR_DOCUMENTS.values(), ids=ANCHOR_DOCUMENTS.keys())
def test_yaml_anchors_and_aliases_are_rejected_with_the_file_path_and_line(
    folder: Path, document: str
):
    (folder / "npcs" / "marta.yaml").write_text(document, encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1
    assert problems[0].startswith("npcs/marta.yaml: некорректный YAML (строка ")
    assert "якор" in problems[0] and "алиас" in problems[0]


@pytest.mark.parametrize(
    ("name", "document"),
    [
        ("adventure.yaml", "x: &a 1\ny: *a\n"),
        ("locations/village.yaml", "x: &a 1\ny: *a\n"),
        ("revelations.yaml", "- &a x\n- *a\n"),
        ("seeds.yaml", "- &a x\n- *a\n"),
        ("world.yaml", "x: &a 1\ny: *a\n"),
        ("relations.yaml", "- &a x\n- *a\n"),
        ("deities/hearth.yaml", "x: &a 1\ny: *a\n"),
    ],
)
def test_anchors_are_rejected_in_every_file_the_loader_reads(
    folder: Path, name: str, document: str
):
    (folder / name).write_text(document, encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith(f"{name}: некорректный YAML")


TEXTS_WITH_INDICATORS = [
    "&a",
    "*a",
    "&",
    "*",
    "<<",
    "!тег",
    "- не список",
    "Тор & Ко * 2 &amp; *x*",
]


@pytest.mark.parametrize("text", TEXTS_WITH_INDICATORS)
def test_text_that_looks_like_an_anchor_or_alias_survives_dump_and_load(tmp_path: Path, text: str):
    data = adventure_data()
    data["pitch"] = text
    data["locations"]["village"]["read_aloud"] = text
    adventure = Adventure.model_validate(data)

    dump_adventure(adventure, tmp_path / "adv")

    assert load_adventure(tmp_path / "adv") == adventure


# --- предел размера файла ---


def write_entity_file_of_size(folder: Path, size: int) -> Path:
    """Файл предмета `key` ровно `size` байт: корректное содержимое и комментарий-заполнитель."""
    path = folder / "items" / "key.yaml"
    body = path.read_bytes()
    path.write_bytes(body + b"#" * (size - len(body)))
    return path


def test_a_file_one_byte_over_the_limit_is_rejected_though_its_yaml_is_valid(folder: Path):
    write_entity_file_of_size(folder, MAX_YAML_BYTES + 1)

    problems = problems_of(folder)

    assert problems == [f"items/key.yaml: файл больше {MAX_YAML_BYTES // 1024} КБ"]


def test_a_file_over_the_limit_is_refused_without_being_read(
    folder: Path, monkeypatch: pytest.MonkeyPatch
):
    big = write_entity_file_of_size(folder, MAX_YAML_BYTES + 1)
    read_bytes = Path.read_bytes

    def read_all_but_the_big_one(self: Path) -> bytes:
        assert self != big, "файл больше предела прочитан целиком"
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_all_but_the_big_one)

    assert problems_of(folder) == [f"items/key.yaml: файл больше {MAX_YAML_BYTES // 1024} КБ"]


def test_a_file_of_exactly_the_limit_is_read(folder: Path, adventure: Adventure):
    write_entity_file_of_size(folder, MAX_YAML_BYTES)

    assert load_adventure(folder) == adventure


@pytest.mark.parametrize("name", ROOT_FILES)
def test_the_size_limit_applies_to_every_file_the_loader_reads(folder: Path, name: str):
    (folder / name).write_bytes(b"[]\n" + b"#" * MAX_YAML_BYTES)

    problems = problems_of(folder)

    assert problems == [f"{name}: файл больше {MAX_YAML_BYTES // 1024} КБ"]


# --- проблемы загрузчика: безопасный текст ограниченной длины ---
# Папку приключения пишет владелец или ему её кто-то передал: тег, ключ и имя файла в проблемах —
# чужой текст. Он не должен ни раздувать отчёт до сотен КБ, ни рисовать в терминале и логе свои
# строки (ESC, BEL, возврат каретки, перевод строки), поэтому каждая проблема ограничена по длине
# и содержит только печатные символы.

HOSTILE_ENTITY_TEXT = {
    "длинный-тег": "surprise: !" + "t" * 100_000 + " x\n",
    "длинный-явный-ключ": "? " + "k" * 100_000 + "\n: 1\n",
    "ключ-с-ESC-BEL-CR-LF": '"\\e[2Kx\\a\\r\\n\\u202e": 1\n',
    "длинный-ключ-с-ESC": '? "' + "\\e" * 5_000 + '"\n: 1\n',
}


@pytest.mark.parametrize("text", HOSTILE_ENTITY_TEXT.values(), ids=HOSTILE_ENTITY_TEXT.keys())
def test_every_problem_is_a_short_line_of_visible_characters(folder: Path, text: str):
    path = folder / "npcs" / "marta.yaml"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")

    problems = problems_of(folder)

    assert problems and all(problem.startswith("npcs/marta.yaml") for problem in problems)
    assert all(len(problem) <= MAX_PROBLEM_LENGTH and problem.isprintable() for problem in problems)


def test_a_long_extra_key_is_cut_but_the_reason_stays_readable(folder: Path):
    path = folder / "npcs" / "marta.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "? " + "k" * 100_000 + "\n: 1\n", encoding="utf-8"
    )

    problems = problems_of(folder)

    assert len(problems) == 1
    assert "…" in problems[0] and "Extra inputs are not permitted" in problems[0]


def test_control_characters_of_an_extra_key_are_shown_as_escapes(folder: Path):
    path = folder / "npcs" / "marta.yaml"
    path.write_text(path.read_text(encoding="utf-8") + '"\\e[2K\\a\\r": 1\n', encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and "\\x1b[2K\\x07\\r" in problems[0]


def test_a_long_yaml_tag_is_cut_with_an_ellipsis_after_the_file_and_line(folder: Path):
    (folder / "items" / "key.yaml").write_text("!" + "t" * 100_000 + " x\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and len(problems[0]) == MAX_PROBLEM_LENGTH
    assert problems[0].startswith("items/key.yaml: некорректный YAML (строка 1): ")
    assert problems[0].endswith("…")


def test_a_file_name_with_control_characters_is_shown_as_escapes(folder: Path):
    (folder / "locations" / "a\x07b\x1b.json").write_text("id: x\n", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].startswith("locations/a\\x07b\\x1b.json:")


LONG_NAME = "n" * 240  # имя файла вправе быть до 255 байт, а id по шаблону — до 64 символов


def test_a_long_file_name_is_cut_but_the_reason_stays_readable(folder: Path):
    (folder / "npcs" / f"{LONG_NAME}.yaml").write_text("id: x\n", encoding="utf-8")

    problems = problems_of(folder)

    assert all(len(problem) <= MAX_PROBLEM_LENGTH and problem.isprintable() for problem in problems)
    assert any("не совпадает с именем файла" in problem for problem in problems)
    assert any("Field required" in problem for problem in problems)
    assert all(problem.startswith("npcs/nnn") for problem in problems)


@pytest.mark.parametrize(
    ("file_name", "reason"),
    [
        (f"{LONG_NAME}.json", "не читается, сущности лежат только в файлах *.yaml"),
        (f"{LONG_NAME}.yaml", "ожидался объект (словарь)"),
    ],
    ids=["not-yaml", "empty-yaml"],
)
def test_a_long_file_name_does_not_eat_the_reason_of_a_read_problem(
    folder: Path, file_name: str, reason: str
):
    (folder / "npcs" / file_name).write_text("", encoding="utf-8")

    problems = problems_of(folder)

    assert len(problems) == 1 and problems[0].endswith(reason)
    assert len(problems[0]) <= MAX_PROBLEM_LENGTH


def test_a_long_folder_name_does_not_eat_the_reason_either(folder: Path):
    (folder / "npcs" / LONG_NAME).mkdir()

    problems = problems_of(folder)

    assert len(problems) == 1 and "вложенные папки не читаются" in problems[0]


def test_error_keeps_each_problem_one_visible_bounded_line():
    error = AdventureFormatError(["a\x1b]52;c;x\x07b\r\nc", "x" * 1000, "короткая"])

    first, second, third = error.problems
    assert first == "a\\x1b]52;c;x\\x07b\\r\\nc"
    assert len(second) == MAX_PROBLEM_LENGTH and second.endswith("…")
    assert third == "короткая"
    assert str(error).count("\n") == 3  # заголовок и по строке на проблему, чужих строк нет


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


# --- круг «запись → чтение» для переводов строк Unicode ---
# PyYAML пишет строку с U+0085 (NEL) в одинарных кавычках «сырым» символом, а читает его как обычный
# перевод строки, который сворачивается в пробел: после круга текст отличается от записанного.
# U+2028 и U+2029 круг переживают, но тоже «переводы строк»: все три пишутся в двойных кавычках
# с экранированием, а текст без них остаётся читаемым, как был.

UNICODE_BREAKS = ["\x85", "\u2028", "\u2029"]
TEXT_AROUND_A_BREAK = {
    "один-символ": "{}",
    "внутри-слов": "до{}после",
    "в-начале": "{}после",
    "в-конце": "до{}",
    "подряд-два": "до{0}{0}после",
    "после-обычного-перевода": "до\n{}после",
    "перед-обычным-переводом": "до{}\nпосле",
    "после-CRLF": "до\r\n{}после",
    "длинная-строка-с-переносом": "строка {0} " + "и ещё слова " * 20 + "{0}конец",
}


@pytest.mark.parametrize("pattern", TEXT_AROUND_A_BREAK.values(), ids=TEXT_AROUND_A_BREAK.keys())
@pytest.mark.parametrize("line_break", UNICODE_BREAKS, ids=["NEL", "LS", "PS"])
def test_unicode_line_breaks_survive_dump_and_load(tmp_path: Path, line_break: str, pattern: str):
    text = pattern.format(line_break)
    data = adventure_data()
    data["pitch"] = text
    data["locations"]["village"]["read_aloud"] = text
    data["locations"]["village"]["exits"][0]["locked_by"] = f"flag:{text}"
    data["seeds"] = ["marta", "village"]
    adventure = Adventure.model_validate(data)

    dump_adventure(adventure, tmp_path / "adv")

    assert load_adventure(tmp_path / "adv") == adventure


def test_all_unicode_line_breaks_in_one_text_survive_the_round_trip(tmp_path: Path):
    data = adventure_data()
    data["pitch"] = "a\x85b\u2028c\u2029d\ne\r\nf\rg"
    adventure = Adventure.model_validate(data)

    dump_adventure(adventure, tmp_path / "adv")

    assert load_adventure(tmp_path / "adv").pitch == data["pitch"]


def test_only_texts_with_unicode_line_breaks_are_written_escaped(tmp_path: Path):
    data = adventure_data()
    data["pitch"] = "Ветер\x85свистит"
    dump_adventure(Adventure.model_validate(data), tmp_path / "adv")

    text = (tmp_path / "adv" / "adventure.yaml").read_text(encoding="utf-8")

    assert "\\N" in text and "\x85" not in text
    assert "title: Склеп у деревни\n" in text  # остальной текст по-прежнему читаемый


def test_dump_of_ordinary_text_is_what_plain_safe_dump_writes(folder: Path):
    # Поведение для обычного текста (с табуляцией, кавычками и переводами строк `\n`) не меняется.
    for file in folder.rglob("*.yaml"):
        text = file.read_text(encoding="utf-8")
        again = yaml.safe_dump(yaml.safe_load(text), allow_unicode=True, sort_keys=False)
        assert text == again, file.name


# --- переводы строк Unicode в настоящем приключении ---
# Круг «запись -> чтение» проверен выше на коротком приключении теста. Здесь он проходит на
# рукописном `demo-crypt` с символом перевода строки внутри каждого свободного текста: названия,
# описания, реплики, заметки мастера, улики, замки на флагах. Какие поля свободные, тест не
# перечисляет: поле с символом принимает схема, значит, это текст, а не id, ссылка или enum.


def string_paths(node: Any, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    """Пути ко всем строковым значениям вложенных словарей и списков (ключи словарей не в счёт)."""
    if isinstance(node, dict):
        return [p for key, value in node.items() for p in string_paths(value, (*path, key))]
    if isinstance(node, list):
        return [p for index, value in enumerate(node) for p in string_paths(value, (*path, index))]
    return [path] if isinstance(node, str) else []


def value_at(data: Any, path: tuple[str | int, ...]) -> Any:
    for step in path:
        data = data[step]
    return data


def put_at(data: Any, path: tuple[str | int, ...], value: str) -> None:
    value_at(data, path[:-1])[path[-1]] = value


def mark_free_texts(data: dict[str, Any]) -> int:
    """Вставляет по переводу строки Unicode (по кругу NEL, LS, PS) в середину каждой строки, которую
    схема принимает с ним. Возвращает, в скольких строках это удалось."""
    marked = 0
    for path in string_paths(data):
        text = value_at(data, path)
        middle = len(text) // 2
        put_at(data, path, text[:middle] + UNICODE_BREAKS[marked % 3] + text[middle:])
        try:
            Adventure.model_validate(data)
        except ValidationError:  # id, ссылка или перечисление: не свободный текст
            put_at(data, path, text)
        else:
            marked += 1
    return marked


def test_the_demo_adventure_with_a_unicode_line_break_in_every_free_text_survives_the_round_trip(
    tmp_path: Path, demo_adventure: Adventure
):
    data = demo_adventure.model_dump(mode="json")
    marked = mark_free_texts(data)
    marked_adventure = Adventure.model_validate(data)

    dump_adventure(marked_adventure, tmp_path / "adv")

    assert marked >= 100  # поля нашлись: у demo-crypt сотни текстов, а не пара
    assert marked_adventure != demo_adventure
    assert load_adventure(tmp_path / "adv") == marked_adventure
    for file in (tmp_path / "adv").rglob("*.yaml"):
        text = file.read_text(encoding="utf-8")
        assert not any(line_break in text for line_break in UNICODE_BREAKS), file.name
