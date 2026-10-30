"""Чтение и запись папки приключения (формат v2):
`content/adventures/<slug>/`.

Структура папки:
- `adventure.yaml` — всё, кроме коллекций, мира и графа связей; первым полем `format_version`;
- `locations/<id>.yaml`, `npcs/<id>.yaml`, `items/<id>.yaml`, `encounters/<id>.yaml`,
  `monsters/<id>.yaml`, `deities/<id>.yaml`, `factions/<id>.yaml` — по файлу на сущность, имя файла
  равно `id` внутри;
- `revelations.yaml` — список открытий;
- `world.yaml` — мир (история, края, слухи), `relations.yaml` — список связей графа; в папке v2
  оба обязательны, в папке v1 (без `format_version`) их нет;
- `seeds.yaml` — необязательный список id сущностей, заданных автором вручную.

Формат v1 переводится в v2 при проверке схемы (`schema.upgrade_v1`): связи из `npcs[].relations`
переходят в граф. Поэтому папку v1 читает `load_adventure`, а копию приключения из игры
(`games.adventure_data`) — `adventure_from_data`, и обе дают одно и то же. Запись — всегда v2.

Загрузка читает безопасным разбором YAML без якорей и алиасов (`content.parse_yaml`), файлы не
больше `content.MAX_YAML_BYTES`, и собирает ошибки по всем файлам сразу, с путями относительно
папки приключения. Каждая проблема — одна короткая строка из печатных символов: тег, ключ или имя
файла из чужой папки не раздувают отчёт и не рисуют в терминале свои строки.

В папках сущностей читаются только `*.yaml`: любой другой файл или подпапка — проблема с путём,
а не молчаливый пропуск (сущность из `crypt.yml` пропала бы без следа). Исключение — скрытые файлы
и папки (имя с точки: `.DS_Store`, `._crypt.yaml` от архиваторов, `.#crypt.yaml` от редакторов):
они служебные, автор их не писал. Символические ссылки (на файл или на папку сущностей, рабочие и
битые) не читаются: файл `*.yaml` из присланного архива мог бы указывать на чужой файл вне папки,
поэтому ссылка — проблема с путём, а цель не открывается. Сама папка приключения может быть ссылкой:
её выбирает вызывающий, а не автор. Запись проверяет имена файлов до первой записи: id становится
именем файла, и сюда он попадает от модели-генератора. Строки с переводами строк Unicode пишутся
в двойных кавычках, чтобы чтение вернуло ровно записанный текст.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from dnd_engine.adventure.schema import (
    COLLECTIONS,
    FORMAT_VERSION,
    Adventure,
    Deity,
    Encounter,
    Faction,
    Item,
    Location,
    Npc,
    Relation,
    Revelation,
    WorldInfo,
    is_entity_id,
    is_format_version,
)
from dnd_engine.content import (
    MAX_FIELD_LENGTH,
    MAX_PROBLEM_LENGTH,
    MonsterDef,
    YamlTooLargeError,
    check_yaml_size,
    describe_parse_failure,
    parse_yaml,
    printable,
)

# Коллекции, у которых каждая сущность лежит в своём файле `<папка>/<id>.yaml`.
ENTITY_FOLDERS: dict[str, type[BaseModel]] = {
    "locations": Location,
    "npcs": Npc,
    "items": Item,
    "encounters": Encounter,
    "monsters": MonsterDef,
    "deities": Deity,
    "factions": Faction,
}
# Всё, что хранится не в adventure.yaml: коллекции, мир, граф связей и seeds.yaml.
SEPARATE_KEYS = (*COLLECTIONS, "world", "relations", "seeds")


class AdventureFormatError(ValueError):
    """Папка приключения некорректна; `problems` — список всех найденных проблем."""

    def __init__(self, problems: list[str]) -> None:
        # Каждая проблема — одна короткая строка из печатных символов, как бы ни выглядела папка.
        self.problems = [printable(problem, MAX_PROBLEM_LENGTH) for problem in problems]
        super().__init__("Приключение некорректно:\n" + "\n".join(f"- {p}" for p in self.problems))


# Причина отказа символической ссылке; цель ссылки в неё не попадает (путь чужой).
SYMLINK_REASON = "символическая ссылка не читается: файлы приключения лежат прямо в его папке"

# Переводы строк Unicode, кроме `\n`: NEL (U+0085), разделители строки и абзаца.
_UNICODE_LINE_BREAKS = "\x85\u2028\u2029"


class _AdventureDumper(yaml.SafeDumper):
    """`SafeDumper`, который пишет строки с переводами строк Unicode в двойных кавычках.

    В одинарных кавычках (выбор PyYAML) NEL записывается «сырым» символом, а чтение сворачивает его
    в пробел: после круга «запись → чтение» текст не тот. Двойные кавычки экранируют его (`\\N`,
    `\\L`, `\\P`). Остальной текст пишется так же, как у `yaml.safe_dump`."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    has_unicode_break = any(char in data for char in _UNICODE_LINE_BREAKS)
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", data, style='"' if has_unicode_break else None
    )


_AdventureDumper.add_representer(str, _represent_str)


def _describe_validation_error(name: str, exc: ValidationError) -> list[str]:
    # Значение из файла в сообщение не попадает (include_input=False): оно может быть огромным.
    problems = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        field = printable(".".join(str(part) for part in error["loc"]), MAX_FIELD_LENGTH)
        where = f", поле {field}" if field else ""
        problems.append(f"{name}{where}: {error['msg']}")
    return problems


def _read_yaml(file: Path, name: str, kind: type, problems: list[str]) -> Any | None:
    """Читает YAML-файл, который должен быть словарём (`dict`) или списком (`list`).

    Любая проблема (нет файла, символическая ссылка, битый YAML, не тот тип) пишется в `problems`,
    результат — `None`.
    """
    if file.is_symlink():  # до `stat` и чтения: они идут по ссылке к чужому файлу
        problems.append(f"{name}: {SYMLINK_REASON}")
        return None
    try:
        check_yaml_size(file)
        raw = file.read_bytes()
    except FileNotFoundError:
        problems.append(f"{name}: файл не найден")
        return None
    except YamlTooLargeError as exc:
        problems.append(f"{name}: {exc}")
        return None
    except OSError as exc:
        problems.append(f"{name}: не удалось прочитать файл ({exc.strerror})")
        return None
    try:
        # Только безопасный разбор: тег вроде `!!python/object/apply` не выполнит код, а даст
        # YAMLError; якорь или алиас тоже дают YAMLError (бомба алиасов не разворачивается).
        data = parse_yaml(raw)
    except Exception as exc:
        # Разбор на чужом вводе бросает не только YAMLError (см. `describe_parse_failure`).
        problems.append(f"{name}: {describe_parse_failure(exc)}")
        return None
    if not isinstance(data, kind):
        expected = "объект (словарь)" if kind is dict else "список"
        problems.append(f"{name}: ожидался {expected}")
        return None
    return data


def _read_document(
    root: Path, name: str, kind: type, problems: list[str], *, required: bool
) -> Any | None:
    """`_read_yaml` для файла `name` в корне папки. Отсутствующий необязательный файл — `None` без
    проблемы; битая символическая ссылка — проблема: `exists()` её не видит, а молчать нельзя."""
    file = root / name
    if not required and not file.exists() and not file.is_symlink():
        return None
    return _read_yaml(file, name, kind, problems)


def _validated_entries[T: BaseModel](
    name: str, entries: list[Any], model: type[T], problems: list[str]
) -> list[tuple[int, T]]:
    """Записи списка из файла `name`, проверенные моделью, с номерами (с единицы); проблема
    называет номер записи."""
    valid: list[tuple[int, T]] = []
    for number, entry in enumerate(entries, start=1):
        where = f"{name}, запись {number}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: ожидался объект (словарь)")
            continue
        try:
            valid.append((number, model.model_validate(entry)))
        except ValidationError as exc:
            problems += _describe_validation_error(where, exc)
    return valid


def _entity_files(root: Path, folder: str, problems: list[str]) -> list[Path]:
    """Файлы `<folder>/*.yaml` по порядку имён. Скрытые пропускаются, всё остальное, что не
    `*.yaml` (в том числе подпапка), — проблема с путём. Нет папки — нет и файлов."""
    if (root / folder).is_symlink():  # `glob` пошёл бы по ссылке в чужую папку
        problems.append(f"{folder}: {SYMLINK_REASON}")
        return []
    files: list[Path] = []
    for entry in sorted((root / folder).glob("*")):
        name = printable(f"{folder}/{entry.name}", MAX_FIELD_LENGTH)
        if entry.name.startswith("."):
            continue
        # Папка с именем `*.yaml` тоже идёт на чтение: проверка перед ним скажет «не обычный файл».
        if entry.suffix == ".yaml":
            files.append(entry)
        elif entry.is_dir():
            problems.append(f"{name}: вложенные папки не читаются, файлы лежат прямо в {folder}/")
        else:
            problems.append(f"{name}: не читается, сущности лежат только в файлах *.yaml")
    return files


def _load_entities(root: Path, folder: str, problems: list[str]) -> dict[str, BaseModel]:
    """Читает `<folder>/*.yaml`; имя файла (без `.yaml`) обязано совпадать с `id` внутри."""
    model = ENTITY_FOLDERS[folder]
    entities: dict[str, BaseModel] = {}
    for file in _entity_files(root, folder, problems):
        name = printable(f"{folder}/{file.name}", MAX_FIELD_LENGTH)
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
    for number, revelation in _validated_entries(name, entries or [], Revelation, problems):
        if revelation.id in revelations:
            problems.append(f"{name}, запись {number}: дубликат id {revelation.id!r}")
        else:
            revelations[revelation.id] = revelation
    return revelations


def _load_world(root: Path, required: bool, problems: list[str]) -> WorldInfo | None:
    """Читает `world.yaml`; нет необязательного файла или он с ошибкой — `None`."""
    name = "world.yaml"
    raw = _read_document(root, name, dict, problems, required=required)
    if raw is None:
        return None
    try:
        return WorldInfo.model_validate(raw)
    except ValidationError as exc:
        problems += _describe_validation_error(name, exc)
        return None


def _load_relations(root: Path, required: bool, problems: list[str]) -> list[Relation] | None:
    """Читает `relations.yaml`: список связей графа. Повтор id — забота валидатора (по видам)."""
    name = "relations.yaml"
    entries = _read_document(root, name, list, problems, required=required)
    if entries is None:
        return None
    return [relation for _, relation in _validated_entries(name, entries, Relation, problems)]


def _load_seeds(root: Path, problems: list[str]) -> tuple[str, ...]:
    """Читает необязательный `seeds.yaml`: список id сущностей, заданных автором."""
    name = "seeds.yaml"
    seeds = _read_document(root, name, list, problems, required=False)
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
    # Мир и граф обязательны в папке v2; в папке v1 их нет, а связи придут из NPC.
    v2 = top is not None and is_format_version(top.get("format_version"), FORMAT_VERSION)
    if (world := _load_world(path, v2, problems)) is not None:
        collections["world"] = world
    if (relations := _load_relations(path, v2, problems)) is not None:
        collections["relations"] = relations
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


def adventure_from_data(data: Mapping[str, Any]) -> Adventure:
    """Приключение из готовых данных — копии в игре (`games.adventure_data`) или JSON — v1 или v2.

    Данные v1 (без `format_version`) переводятся в v2. Бросает `AdventureFormatError` со всеми
    проблемами, в том числе на данных не словарём."""
    if not isinstance(data, Mapping):
        raise AdventureFormatError(["adventure: ожидался объект (словарь)"])
    try:
        return Adventure.model_validate(dict(data))
    except ValidationError as exc:
        # `from None`: печать трассировки (журнал uvicorn) не тащит `ValidationError` с
        # `input_value`; первопричина остаётся в `__context__` для `safe_traceback` сервера.
        raise AdventureFormatError(_describe_validation_error("adventure", exc)) from None


def dump_adventure(adventure: Adventure, path: Path) -> None:
    """Записывает приключение в папку `path` (создаёт её) в формате v2; существующие файлы
    перезаписываются.

    Папка не очищается: файлы сущностей, которых в приключении больше нет, остаются на месте,
    поэтому вызывающий передаёт новую или пустую папку.
    """
    # Поле связей v1 у NPC после чтения всегда пусто: в файлах v2 его нет.
    data = adventure.model_dump(mode="json", exclude={"npcs": {"__all__": {"relations"}}})
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
    documents["world.yaml"] = data.pop("world")
    documents["relations.yaml"] = data.pop("relations")
    if seeds := data.pop("seeds"):
        documents["seeds.yaml"] = seeds
    documents["adventure.yaml"] = data
    for name, document in documents.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.dump(document, Dumper=_AdventureDumper, allow_unicode=True, sort_keys=False)
        file.write_text(text, encoding="utf-8")
