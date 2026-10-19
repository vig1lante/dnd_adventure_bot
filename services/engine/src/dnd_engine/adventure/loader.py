"""Чтение и запись папки приключения (спека §6.1): `content/adventures/<slug>/`.

Структура папки:
- `adventure.yaml` — всё, кроме коллекций;
- `locations/<id>.yaml`, `npcs/<id>.yaml`, `items/<id>.yaml`, `encounters/<id>.yaml`,
  `monsters/<id>.yaml` — по файлу на сущность, имя файла равно `id` внутри;
- `revelations.yaml` — список открытий;
- `seeds.yaml` — необязательный список id сущностей, заданных автором вручную.

Загрузка читает только `yaml.safe_load` и собирает ошибки по всем файлам сразу, с путями
относительно папки приключения. Запись проверяет имена файлов до первой записи: id становится
именем файла, и сюда он попадает от модели-генератора.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from dnd_engine.adventure.schema import (
    COLLECTIONS,
    Adventure,
    Encounter,
    Item,
    Location,
    Npc,
    Revelation,
    is_entity_id,
)
from dnd_engine.content import MonsterDef, _describe_yaml_error

# Коллекции, у которых каждая сущность лежит в своём файле `<папка>/<id>.yaml`.
ENTITY_FOLDERS: dict[str, type[BaseModel]] = {
    "locations": Location,
    "npcs": Npc,
    "items": Item,
    "encounters": Encounter,
    "monsters": MonsterDef,
}
# Всё, что хранится не в adventure.yaml: коллекции и seeds.yaml.
SEPARATE_KEYS = (*COLLECTIONS, "seeds")


class AdventureFormatError(ValueError):
    """Папка приключения некорректна; `problems` — список всех найденных проблем."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("Приключение некорректно:\n" + "\n".join(f"- {p}" for p in self.problems))


def _describe_validation_error(name: str, exc: ValidationError) -> list[str]:
    # Значение из файла в сообщение не попадает (include_input=False): оно может быть огромным.
    problems = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        field = ".".join(str(part) for part in error["loc"])
        where = f", поле {field}" if field else ""
        problems.append(f"{name}{where}: {error['msg']}")
    return problems


def _read_yaml(file: Path, name: str, kind: type, problems: list[str]) -> Any | None:
    """Читает YAML-файл, который должен быть словарём (`dict`) или списком (`list`).

    Любая проблема (нет файла, битый YAML, не тот тип) пишется в `problems`, результат — `None`.
    """
    try:
        # Только safe_load: тег вроде `!!python/object/apply` не выполнит код, а даст YAMLError.
        data = yaml.safe_load(file.read_bytes())
    except FileNotFoundError:
        problems.append(f"{name}: файл не найден")
        return None
    except OSError as exc:
        problems.append(f"{name}: не удалось прочитать файл ({exc.strerror})")
        return None
    except yaml.YAMLError as exc:
        problems.append(f"{name}: {_describe_yaml_error(exc)}")
        return None
    if not isinstance(data, kind):
        expected = "объект (словарь)" if kind is dict else "список"
        problems.append(f"{name}: ожидался {expected}")
        return None
    return data


def _load_entities(root: Path, folder: str, problems: list[str]) -> dict[str, BaseModel]:
    """Читает `<folder>/*.yaml`; имя файла (без `.yaml`) обязано совпадать с `id` внутри."""
    model = ENTITY_FOLDERS[folder]
    entities: dict[str, BaseModel] = {}
    for file in sorted((root / folder).glob("*.yaml")):
        name = f"{folder}/{file.name}"
        raw = _read_yaml(file, name, dict, problems)
        if raw is None:
            continue
        raw_id = raw.get("id")
        mismatch = isinstance(raw_id, str) and raw_id != file.stem
        if mismatch:
            problems.append(f"{name}: id {raw_id[:64]!r} не совпадает с именем файла")
        try:
            entity = model.model_validate(raw)
        except ValidationError as exc:
            problems += _describe_validation_error(name, exc)
            continue
        if not mismatch:
            entities[file.stem] = entity
    return entities


def _load_revelations(root: Path, problems: list[str]) -> dict[str, Revelation]:
    """Читает `revelations.yaml`: список открытий, id внутри записей уникальны."""
    name = "revelations.yaml"
    entries = _read_yaml(root / name, name, list, problems)
    revelations: dict[str, Revelation] = {}
    for number, entry in enumerate(entries or [], start=1):
        if not isinstance(entry, dict):
            problems.append(f"{name}, запись {number}: ожидался объект (словарь)")
            continue
        try:
            revelation = Revelation.model_validate(entry)
        except ValidationError as exc:
            problems += _describe_validation_error(f"{name}, запись {number}", exc)
            continue
        if revelation.id in revelations:
            problems.append(f"{name}, запись {number}: дубликат id {revelation.id!r}")
        else:
            revelations[revelation.id] = revelation
    return revelations


def _load_seeds(root: Path, problems: list[str]) -> tuple[str, ...]:
    """Читает необязательный `seeds.yaml`: список id сущностей, заданных автором."""
    name = "seeds.yaml"
    if not (root / name).exists():
        return ()
    seeds = _read_yaml(root / name, name, list, problems)
    if seeds is None:
        return ()
    if not all(isinstance(seed, str) for seed in seeds):
        problems.append(f"{name}: ожидался список id (строк)")
        return ()
    return tuple(seeds)


def load_adventure(path: Path) -> Adventure:
    """Превращает папку приключения в неизменяемый `Adventure`.

    Бросает `AdventureFormatError` со списком всех проблем по всем файлам.
    """
    if not path.is_dir():
        raise AdventureFormatError([f"{path}: папка приключения не найдена"])
    problems: list[str] = []
    top = _read_yaml(path / "adventure.yaml", "adventure.yaml", dict, problems)
    collections: dict[str, Any] = {
        folder: _load_entities(path, folder, problems) for folder in ENTITY_FOLDERS
    }
    collections["revelations"] = _load_revelations(path, problems)
    collections["seeds"] = _load_seeds(path, problems)
    adventure = None
    if top is not None:
        problems += [
            f"adventure.yaml: {key} хранится в отдельных файлах, не здесь"
            for key in SEPARATE_KEYS
            if key in top
        ]
        try:
            # Сущности уже проверены по файлам: здесь проверяются поля самого adventure.yaml.
            adventure = Adventure.model_validate({**top, **collections})
        except ValidationError as exc:
            problems += _describe_validation_error("adventure.yaml", exc)
    if problems or adventure is None:
        raise AdventureFormatError(problems)
    return adventure


def dump_adventure(adventure: Adventure, path: Path) -> None:
    """Записывает приключение в папку `path` (создаёт её); существующие файлы перезаписываются.

    Папка не очищается: файлы сущностей, которых в приключении больше нет, остаются на месте,
    поэтому вызывающий передаёт новую или пустую папку.
    """
    data = adventure.model_dump(mode="json")
    documents: dict[str, Any] = {}
    unsafe: list[str] = []
    for folder in ENTITY_FOLDERS:
        for entity_id, entity in data.pop(folder).items():
            # Защита в глубину: приключение могли собрать без проверки схемы (`model_copy`),
            # а имя файла нельзя доверять — `../x` вывело бы запись за пределы папки.
            if is_entity_id(entity_id):
                documents[f"{folder}/{entity_id}.yaml"] = entity
            else:
                unsafe.append(f"{folder}: id {entity_id[:64]!r} нельзя использовать как имя файла")
    if unsafe:
        raise AdventureFormatError(unsafe)
    documents["revelations.yaml"] = list(data.pop("revelations").values())
    if seeds := data.pop("seeds"):
        documents["seeds.yaml"] = seeds
    documents["adventure.yaml"] = data
    for name, document in documents.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
