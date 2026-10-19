"""Инварианты движка, которые должны держаться во всех модулях сразу.

Проверяются границы пакета (только разрешённые импорты, нет динамического кода и произвольного
чтения файлов, нет чужих генераторов случайных чисел), честность расхода костей (бросок только
через переданный `Rng` и без лишних значений), чистота функций (вход и общие данные SRD не
меняются) и воспроизводимость сценария по seed.
"""

import ast
import json
from pathlib import Path

import pytest

import dnd_engine
from dnd_engine.abilities import Ability, Skill
from dnd_engine.character import ActiveCondition
from dnd_engine.checks import (
    VALID_DCS,
    ContestSide,
    ability_check,
    contest,
    engine_saving_throw,
    monster_saving_throw,
    saving_throw,
)
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character, random_character, roll_ability_set
from dnd_engine.dice import Advantage
from dnd_engine.rest import long_rest, short_rest
from dnd_engine.rng import Rng, ScriptedRng, SeededRng

STR, DEX, CON, INT, WIS, CHA = Ability
NONE = Advantage.NONE

PACKAGE_DIR = Path(dnd_engine.__file__).parent
MODULE_FILES = sorted(PACKAGE_DIR.rglob("*.py"))


def module_path(path: Path) -> str:
    """Путь модуля внутри пакета: `content.py`, `adventure/loader.py`.

    Ограничения «только в этом модуле» привязаны к пути, а не к имени файла: одноимённый файл
    в другом подпакете (`tools/content.py`) не получает чужих разрешений."""
    return path.relative_to(PACKAGE_DIR).as_posix()


# Допустимые корневые импорты движка (allowlist): только то, что ему реально нужно. Любой другой
# корень — сеть, БД, процессы, клиенты моделей, `os`, `sys`, `io`, `pickle` — тест отклонит.
# Новый корень — осознанное решение: добавьте его сюда вместе с обоснованием и, если он нужен
# одному модулю, ограничьте файлом в `IMPORT_ONLY_IN`.
ALLOWED_IMPORTS: dict[str, str] = {
    "dnd_engine": "собственный пакет",
    "typing": "аннотации типов",
    "enum": "StrEnum: Ability, Skill, Advantage, CheckKind, SceneType",
    "dataclasses": "неизменяемые описания разобранного удара, части урона и участника боя",
    "collections": "Counter, deque (очередь ScriptedRng) и collections.abc для аннотаций",
    "types": "MappingProxyType: таблицы правил и модулей только для чтения",
    "functools": "cache: общий разобранный набор SRD (`load_srd`)",
    "re": "разбор записи костей вида 2d6+3 (dice.py)",
    "pydantic": "модели данных: проверка и (де)сериализация состояния игры в JSON",
    "pydantic_core": "ErrorDetails: описание проблем при чтении данных SRD (content.py)",
    "yaml": (
        "безопасный разбор без якорей и алиасов — `content.parse_yaml` (подкласс SafeLoader) для "
        "данных SRD и папки приключения; запись папки — подклассом SafeDumper (loader.py)"
    ),
    "importlib": "importlib.resources.files: данные SRD лежат внутри пакета (content.py)",
    "pathlib": "Path: папка данных в `load_srd(path)` (content.py), папка приключения (loader.py)",
    "random": "SeededRng: воспроизводимый генератор для тестов (rng.py)",
    "secrets": "SystemRng: криптостойкий генератор для игры (rng.py)",
}
# Корни, которым место только в одном модуле: остальным они не нужны, а появление там — сигнал.
# Модуль назван путём внутри пакета (`adventure/loader.py`), а не именем файла.
IMPORT_ONLY_IN: dict[str, set[str]] = {
    "random": {"rng.py"},
    "secrets": {"rng.py"},
    "re": {"dice.py"},
    "yaml": {"content.py", "adventure/loader.py"},
    "importlib": {"content.py"},
    "pathlib": {"content.py", "adventure/loader.py"},
    "pydantic_core": {"content.py"},
}
# Динамический код и произвольный доступ к файлам. Имена встроенных функций ловятся по любому
# упоминанию (в том числе `f = open`), атрибуты — по любому обращению (`path.open()`).
FORBIDDEN_NAMES = {"open", "eval", "exec", "compile", "__import__", "__builtins__"}
FORBIDDEN_ATTRIBUTES = {"open", "read_text", "write_bytes", "import_module"}
# Обоснованный доступ к файлам — только в двух загрузчиках, и `open` они не вызывают:
# - чтение: данные SRD читает `Traversable.read_bytes()` (`content._read_entries`), путь идёт
#   из `importlib.resources`; папку приключения читает `Path.read_bytes()` (`adventure/loader.py`);
#   перед чтением `content.check_yaml_size` смотрит `stat`: файл больше предела и не обычный файл
#   (папка, канал FIFO, устройство) отвергаются, не открываясь;
# - запись: только `adventure/loader.py` (`dump_adventure`), и только после проверки имён файлов.
# Модули названы путями внутри пакета, как в `IMPORT_ONLY_IN`.
FILE_READS_ONLY_IN: dict[str, set[str]] = {
    "read_bytes": {"content.py", "adventure/loader.py"},
    "write_text": {"adventure/loader.py"},
}
ENVIRONMENT_ATTRIBUTES = {"environ", "getenv"}
# Генераторы случайных чисел разрешены только в `rng.py`; остальной код бросает через `Rng`.
RANDOMNESS_IMPORTS = {"random", "secrets"}


class CountingRng:
    """Обёртка над `Rng`, считающая обращения: так видно, не потрачено ли лишнее."""

    def __init__(self, inner: Rng) -> None:
        self.inner = inner
        self.calls = 0

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return self.inner.randint(a, b)


def constant_dice(value: int = 10, count: int = 50) -> CountingRng:
    """Источник, всегда выдающий одно и то же значение (не единицу: Удача не сработает)."""
    return CountingRng(ScriptedRng([value] * count))


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


# --- границы пакета ---


def roots_of_imports(source: str) -> set[str]:
    """Корни всех импортов кода, в том числе внутри функций; относительные импорты — свои."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def imported_roots(path: Path) -> set[str]:
    return roots_of_imports(path.read_text(encoding="utf-8"))


def imports_outside_allowlist(source: str) -> set[str]:
    return roots_of_imports(source) - ALLOWED_IMPORTS.keys()


def imports_outside_their_module(source: str, module: str) -> set[str]:
    """Корни из `IMPORT_ONLY_IN`, которые импортирует не тот модуль (`module` — путь в пакете)."""
    return {
        root
        for root in roots_of_imports(source) & IMPORT_ONLY_IN.keys()
        if module not in IMPORT_ONLY_IN[root]
    }


def forbidden_constructs(source: str, module: str) -> list[str]:
    """Динамический код и обращения к файлам: `open`, `eval`, `exec`, `__import__` и им подобное.

    `module` — путь модуля в пакете: чтение и запись файлов разрешены только названным путям."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{node.id} (строка {node.lineno})")
        elif isinstance(node, ast.Attribute):
            only_in = FILE_READS_ONLY_IN.get(node.attr)
            if node.attr in FORBIDDEN_ATTRIBUTES or (only_in and module not in only_in):
                found.append(f".{node.attr} (строка {node.lineno})")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES
        ):
            found.append(f"{node.func.id}(..., {node.args[1].value!r}) (строка {node.lineno})")
    return found


def test_package_files_are_found():
    names = {path.name for path in MODULE_FILES}
    paths = {module_path(path) for path in MODULE_FILES}

    assert {"rng.py", "dice.py", "creation.py", "checks.py", "rest.py"} <= names
    # Новые модули блока B3 тоже под проверкой: иначе границы держались бы только на старых файлах.
    assert {"events.py", "state.py", "conditions.py", "engine.py", "model.py"} <= names
    assert {"base.py", "bg3_flavor.py", "campfire.py"} <= names
    # Блок B4: приключение и инструменты мастера тоже под проверкой границ, не только старые файлы.
    assert {
        "adventure/schema.py",
        "adventure/loader.py",
        "adventure/validator.py",
        "adventure/budget.py",
    } <= paths
    assert {
        "tools/registry.py",
        "tools/lore.py",
        "tools/world.py",
        "tools/checks.py",
        "tools/combat.py",
        "tools/narrative.py",
    } <= paths


@pytest.mark.parametrize("path", MODULE_FILES, ids=module_path)
def test_engine_module_imports_only_what_the_allowlist_permits(path):
    outside = imports_outside_allowlist(path.read_text(encoding="utf-8"))

    assert not outside, f"корни {sorted(outside)} не из ALLOWED_IMPORTS: сеть, БД или лишнее"


@pytest.mark.parametrize("path", MODULE_FILES, ids=module_path)
def test_restricted_imports_stay_in_their_own_module(path):
    misplaced = imports_outside_their_module(path.read_text(encoding="utf-8"), module_path(path))

    assert not misplaced, f"{sorted(misplaced)} разрешены только в {IMPORT_ONLY_IN}"


def test_the_allowlist_has_no_unused_entries():
    used = set().union(*(imported_roots(path) for path in MODULE_FILES))

    assert set(ALLOWED_IMPORTS) == used  # лишняя запись молча расширяла бы допустимое
    assert IMPORT_ONLY_IN.keys() <= ALLOWED_IMPORTS.keys()
    for root, files in IMPORT_ONLY_IN.items():
        assert files == {module_path(p) for p in MODULE_FILES if root in imported_roots(p)}, root


@pytest.mark.parametrize("path", MODULE_FILES, ids=module_path)
def test_engine_module_has_no_dynamic_code_or_arbitrary_file_access(path):
    found = forbidden_constructs(path.read_text(encoding="utf-8"), module_path(path))

    assert not found, found


def test_the_only_file_read_is_the_srd_loader_through_importlib_resources():
    readers = {
        module_path(path)
        for path in MODULE_FILES
        if any(
            isinstance(node, ast.Attribute) and node.attr == "read_bytes"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }

    # `open` движок не вызывает нигде: данные SRD читает `Traversable.read_bytes()` загрузчика,
    # а путь к ним даёт `importlib.resources.files("dnd_engine")`; папку приключения читает
    # загрузчик приключения.
    assert readers == FILE_READS_ONLY_IN["read_bytes"] == {"content.py", "adventure/loader.py"}
    assert "importlib" in imported_roots(PACKAGE_DIR / "content.py")


def test_the_only_file_writer_is_the_adventure_loader():
    writers = {
        module_path(path)
        for path in MODULE_FILES
        if any(
            isinstance(node, ast.Attribute) and node.attr == "write_text"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }

    assert writers == FILE_READS_ONLY_IN["write_text"] == {"adventure/loader.py"}


@pytest.mark.parametrize("path", MODULE_FILES, ids=module_path)
def test_engine_module_does_not_read_the_environment(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert not attributes & ENVIRONMENT_ATTRIBUTES


@pytest.mark.parametrize("path", MODULE_FILES, ids=module_path)
def test_only_rng_module_imports_random_generators(path):
    if module_path(path) == "rng.py":
        assert imported_roots(path) & RANDOMNESS_IMPORTS
    else:
        assert not imported_roots(path) & RANDOMNESS_IMPORTS


# Сами проверки границ тоже проверяются: иначе они могли бы молча перестать что-либо ловить.


@pytest.mark.parametrize(
    ("source", "outside"),
    [
        ("import socket", {"socket"}),
        ("import os.path", {"os"}),
        ("from subprocess import run", {"subprocess"}),
        ("from sqlalchemy.orm import Session", {"sqlalchemy"}),
        ("import httpx, yaml", {"httpx"}),
        ("def f():\n    import anthropic\n", {"anthropic"}),
        ("try:\n    import numpy\nexcept ImportError:\n    pass\n", {"numpy"}),
        ("import io", {"io"}),
        ("import pickle", {"pickle"}),
        ("import builtins", {"builtins"}),
    ],
)
def test_the_allowlist_check_catches_imports_of_the_outside_world(source, outside):
    assert imports_outside_allowlist(source) == outside


def test_the_allowlist_check_accepts_allowed_and_relative_imports():
    source = "import re\nfrom pydantic import BaseModel\nfrom . import x\nfrom .y import z\n"

    assert imports_outside_allowlist(source) == set()


def test_the_restricted_import_check_catches_a_root_in_the_wrong_file():
    assert imports_outside_their_module("import yaml", "checks.py") == {"yaml"}
    assert imports_outside_their_module("import yaml", "content.py") == set()
    assert imports_outside_their_module("import yaml", "adventure/loader.py") == set()
    assert imports_outside_their_module("import secrets", "dice.py") == {"secrets"}


@pytest.mark.parametrize(
    "source",
    [
        "open('data.yaml')",
        "eval('1 + 1')",
        "exec('x = 1')",
        "compile('1', 'f', 'eval')",
        "__import__('os')",
        "reader = open",
        "path.open()",
        "path.read_text()",
        "path.write_text('x')",
        "path.write_bytes(b'x')",
        "importlib.import_module('os')",
        "getattr(obj, 'open')",
        "getattr(__builtins__, 'eval')",
        "__builtins__['exec']",
        "files('dnd_engine').read_bytes()",  # вне загрузчика данных SRD
    ],
)
def test_the_dynamic_code_check_catches_every_form(source):
    assert forbidden_constructs(source, "checks.py")


@pytest.mark.parametrize(
    "source",
    [
        "re.compile('[0-9]+')",  # метод модуля re, а не встроенная compile
        "getattr(target, 'temp_hp', 0)",
        "opened = True\nopen_slots = []",
        "def reopen(): ...",
        "x = obj.opened",
    ],
)
def test_the_dynamic_code_check_leaves_ordinary_code_alone(source):
    assert forbidden_constructs(source, "checks.py") == []


def test_the_srd_loader_may_read_bytes_but_other_files_may_not():
    source = "data = source.read_bytes()"

    assert forbidden_constructs(source, "content.py") == []
    assert forbidden_constructs(source, "rest.py")


def test_only_the_adventure_loader_may_write_text_and_other_writes_stay_forbidden():
    assert forbidden_constructs("path.write_text('x')", "adventure/loader.py") == []
    assert forbidden_constructs("path.write_text('x')", "rest.py")
    assert forbidden_constructs("path.write_bytes(b'x')", "adventure/loader.py")
    assert forbidden_constructs("path.open('w')", "adventure/loader.py")


@pytest.mark.parametrize(
    "module", ["loader.py", "tools/loader.py", "adventure/content.py", "tools/content.py"]
)
def test_a_file_with_a_trusted_name_in_another_place_gets_no_loader_rights(module):
    # Ограничения идут по пути в пакете: одноимённый файл в другом подпакете их не обходит.
    assert imports_outside_their_module("import yaml\nimport pathlib", module) == {
        "yaml",
        "pathlib",
    }
    assert forbidden_constructs("data = path.read_bytes()", module)
    assert forbidden_constructs("path.write_text('x')", module)


def test_a_random_generator_import_is_allowed_only_in_the_top_level_rng_module():
    assert imports_outside_their_module("import random", "rng.py") == set()
    assert imports_outside_their_module("import random", "tools/rng.py") == {"random"}
    assert imports_outside_their_module("import secrets", "adventure/rng.py") == {"secrets"}


def test_every_restricted_module_named_in_the_guard_exists():
    # Опечатка или переименованный файл молча оставили бы запись без модуля.
    paths = {module_path(path) for path in MODULE_FILES}

    for files in (*IMPORT_ONLY_IN.values(), *FILE_READS_ONLY_IN.values()):
        assert files <= paths, files - paths


# --- инструменты мастера: Сл только из таблицы ---

TOOL_FILES = sorted((PACKAGE_DIR / "tools").rglob("*.py"))
# «Сл движка»: концентрация, заклинания и статблоки задают её сами, до таблицы 5/10/15/20/25/30
# она не округляется. Из аргументов модели эти пути недоступны: модули `tools/` их не вызывают.
ENGINE_DC_FUNCTIONS = {"engine_saving_throw", "monster_saving_throw"}
# Поля состояния, которыми оно получает свою Сл и спасбросок (повторный бросок в конце хода).
CONDITION_DC_FIELDS = {"dc", "save"}
# Проверки, чью Сл инструмент берёт из аргумента (`args.dc`), а модель аргументов ограничивает
# таблицей. Событие броска (`RollEvent`) только записывает Сл, которую посчитал движок.
TABLE_DC_CHECKS = {"ability_check", "saving_throw"}
CONDITION_CONSTRUCTORS = {"model_validate", "model_validate_json", "model_construct"}


def callee_name(call: ast.Call) -> str | None:
    """Имя вызываемого: `f(...)` и `module.f(...)` дают `f`."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def engine_dc_mentions(source: str) -> list[str]:
    """Любое упоминание путей «Сл движка»: имя, атрибут, импорт (и под псевдонимом), строка."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name.split(".")[-1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value  # getattr(checks, "monster_saving_throw")
        else:
            continue
        if name in ENGINE_DC_FUNCTIONS:
            found.append(f"{name} (строка {node.lineno})")
    return found


def condition_names(tree: ast.AST) -> set[str]:
    """Имена `ActiveCondition` в модуле, в том числе псевдонимы импорта."""
    return {"ActiveCondition"} | {
        node.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.alias) and node.name.split(".")[-1] == "ActiveCondition"
        if node.asname
    }


def condition_dc_inputs(source: str) -> list[str]:
    """Откуда у состояния может появиться своя Сл или спасбросок.

    Это `dc=`/`save=` в `ActiveCondition(...)`, распаковка `**данные` туда же, построение из
    словаря (`model_validate`) и присваивание полю `.dc`/`.save`: в нём может быть что угодно."""
    tree = ast.parse(source)
    names = condition_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if callee_name(node) in names:
                found += [
                    f"ActiveCondition({kw.arg or '**'}=...) (строка {node.lineno})"
                    for kw in node.keywords
                    if kw.arg is None or kw.arg in CONDITION_DC_FIELDS
                ]
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in CONDITION_CONSTRUCTORS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names
            ):
                found.append(f"ActiveCondition.{node.func.attr} (строка {node.lineno})")
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [
                f".{target.attr} = ... (строка {node.lineno})"
                for target in targets
                if isinstance(target, ast.Attribute) and target.attr in CONDITION_DC_FIELDS
            ]
    return found


def dc_arguments_outside_the_table(source: str) -> list[str]:
    """Вызовы с `dc=`, где Сл не из аргументов инструмента: число, расчёт, другая функция."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "dc":
                continue
            from_arguments = isinstance(keyword.value, ast.Attribute) and keyword.value.attr == "dc"
            allowed = callee_name(node) == "RollEvent" or (
                callee_name(node) in TABLE_DC_CHECKS and from_arguments
            )
            if not allowed:
                found.append(f"{callee_name(node)}(dc=...) (строка {node.lineno})")
    return found


def test_tool_files_are_found():
    names = {path.name for path in TOOL_FILES}

    # Пустой список молча превратил бы все проверки ниже в «ничего не найдено».
    assert {"registry.py", "lore.py", "world.py", "checks.py", "combat.py", "narrative.py"} <= names


@pytest.mark.parametrize("path", TOOL_FILES, ids=module_path)
def test_tool_modules_do_not_use_the_engine_dc_paths(path):
    found = engine_dc_mentions(path.read_text(encoding="utf-8"))

    assert not found, f"Сл движка недоступна модели: {found}"


@pytest.mark.parametrize("path", TOOL_FILES, ids=module_path)
def test_tool_modules_give_conditions_no_dc_and_no_save(path):
    found = condition_dc_inputs(path.read_text(encoding="utf-8"))

    assert not found, f"у внешнего состояния нет своей Сл и спасброска: {found}"


@pytest.mark.parametrize("path", TOOL_FILES, ids=module_path)
def test_tool_modules_take_the_check_dc_only_from_their_arguments(path):
    found = dc_arguments_outside_the_table(path.read_text(encoding="utf-8"))

    assert not found, f"Сл проверки берётся только из аргументов инструмента: {found}"


@pytest.mark.parametrize(
    "source",
    [
        "engine_saving_throw(hero, CON, dc=12)",
        "monster_saving_throw(monster, 'ogre-1', [], CON, dc=12)",
        "from dnd_engine.checks import engine_saving_throw",
        "from dnd_engine.checks import monster_saving_throw as roll_save",
        "import dnd_engine.checks as checks\nchecks.engine_saving_throw(hero, CON, dc=12)",
        "getattr(checks, 'monster_saving_throw')",
        "save = engine_saving_throw",
    ],
)
def test_the_engine_dc_check_catches_every_form(source):
    assert engine_dc_mentions(source)


@pytest.mark.parametrize(
    "source",
    [
        "saving_throw(hero, CON, dc=args.dc)",
        '"""Путь engine_saving_throw из аргументов модели недоступен."""',
        "# monster_saving_throw: комментарий",
        "engine_dc = 12",
    ],
)
def test_the_engine_dc_check_leaves_ordinary_code_alone(source):
    assert engine_dc_mentions(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "ActiveCondition(id='prone', dc=15)",
        "ActiveCondition(id='prone', save='con')",
        "ActiveCondition(id='prone', **extra)",
        "character.ActiveCondition(id='prone', dc=15)",
        "from dnd_engine.character import ActiveCondition as Tag\nTag(id='prone', save='dex')",
        "ActiveCondition.model_validate(data)",
        "ActiveCondition.model_construct(id='prone', dc=15)",
        "condition.dc = 12",
        "condition.save = 'con'",
        "condition.dc += 1",
    ],
)
def test_the_condition_check_catches_every_form(source):
    assert condition_dc_inputs(source)


@pytest.mark.parametrize(
    "source",
    [
        "ActiveCondition(id='prone', rounds_left=3, source='')",
        "other(dc=15)",
        "RollEvent(dc=result.dc)",
        "x.dc_text = 1",
        "saved = hero.save_proficiencies",
    ],
)
def test_the_condition_check_leaves_ordinary_code_alone(source):
    assert condition_dc_inputs(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "ability_check(hero, DEX, dc=15)",
        "saving_throw(hero, DEX, dc=args.dc + 1)",
        "saving_throw(hero, DEX, dc=compute())",
        "other_check(hero, DEX, dc=args.dc)",
    ],
)
def test_the_dc_argument_check_catches_a_dc_that_is_not_from_the_arguments(source):
    assert dc_arguments_outside_the_table(source)


@pytest.mark.parametrize(
    "source",
    [
        "ability_check(hero, DEX, dc=args.dc)",
        "checks.saving_throw(hero, DEX, dc=args.dc)",
        "RollEvent(actor_id='x', dc=result.dc)",
        "ability_check(hero, DEX, advantage=NONE)",
    ],
)
def test_the_dc_argument_check_accepts_the_table_dc_from_the_arguments(source):
    assert dc_arguments_outside_the_table(source) == []


# --- расход костей: только через переданный Rng и без лишнего ---


@pytest.mark.parametrize("ability", list(Ability))
def test_saving_throw_dice_count_follows_species_traits(srd, ability):
    for species_id in srd.species:
        hero = quick_character(
            "fighter", species_id, srd, name="Т", concept="т", character_id="pc-1"
        )
        rng = constant_dice()

        saving_throw(hero, ability, dc=15, advantage=NONE, rng=rng, reason="тест")

        # Преимущество даёт только Гномья хитрость на Инт, Мдр и Хар; остальным одна к20.
        gnomish = species_id == "gnome" and ability in {INT, WIS, CHA}
        assert rng.calls == (2 if gnomish else 1), (species_id, ability)


@pytest.mark.parametrize(
    ("against", "dwarf_dice", "other_dice"),
    [(None, 1, 1), ("poison", 2, 1), ("poisoned", 2, 1), ("fire", 1, 1)],
)
def test_dwarven_resilience_costs_a_second_die_only_against_poison(
    srd, against, dwarf_dice, other_dice
):
    for species_id, expected in [("dwarf", dwarf_dice), ("human", other_dice)]:
        hero = quick_character(
            "fighter", species_id, srd, name="Т", concept="т", character_id="pc-1"
        )
        rng = constant_dice()

        saving_throw(hero, CON, dc=15, advantage=NONE, rng=rng, reason="яд", against=against)

        assert rng.calls == expected, (species_id, against)


def test_ability_check_dice_count_with_every_advantage_mode(srd):
    hero = quick_character("rogue", "human", srd, name="Т", concept="т", character_id="pc-1")

    for advantage, expected in [(NONE, 1), (Advantage.ADVANTAGE, 2), (Advantage.DISADVANTAGE, 2)]:
        rng = constant_dice()
        ability_check(
            hero, DEX, skill=Skill.STEALTH, dc=15, advantage=advantage, rng=rng, reason="тест"
        )

        assert rng.calls == expected, advantage


@pytest.mark.parametrize("dc", range(-5, 40))
def test_only_table_dcs_are_rolled_for_and_nothing_else_is_spent(srd, dc):
    hero = quick_character("fighter", "human", srd, name="Т", concept="т", character_id="pc-1")
    rng = constant_dice()

    if dc in VALID_DCS:
        assert (
            ability_check(hero, STR, skill=None, dc=dc, advantage=NONE, rng=rng, reason="т").dc
            == dc
        )
        assert saving_throw(hero, STR, dc=dc, advantage=NONE, rng=rng, reason="т").dc == dc
        assert rng.calls == 2
    else:
        with pytest.raises(ValueError, match="Сл"):
            ability_check(hero, STR, skill=None, dc=dc, advantage=NONE, rng=rng, reason="т")
        with pytest.raises(ValueError, match="Сл"):
            saving_throw(hero, STR, dc=dc, advantage=NONE, rng=rng, reason="т")
        assert rng.calls == 0


@pytest.mark.parametrize("dc", range(-5, 40))
def test_dcs_computed_by_the_engine_are_rolled_for_whatever_their_value(srd, dc):
    hero = quick_character("fighter", "human", srd, name="Т", concept="т", character_id="pc-1")
    rng = constant_dice()

    # Концентрация, Сл заклинаний и статблоков не из таблицы мастера: путь движка её не проверяет.
    result = engine_saving_throw(hero, CON, dc=dc, advantage=NONE, rng=rng, reason="т")

    assert result.dc == dc
    assert rng.calls == 1


@pytest.mark.parametrize("condition_id", ["paralyzed", "petrified", "stunned", "unconscious"])
@pytest.mark.parametrize("ability", [STR, DEX])
def test_automatic_save_failure_spends_no_dice_on_any_path(srd, condition_id, ability):
    hero = quick_character("fighter", "human", srd, name="Т", concept="т", character_id="pc-1")
    hero.conditions.append(ActiveCondition(id=condition_id))
    rng = constant_dice()

    saving_throw(hero, ability, dc=15, advantage=NONE, rng=rng, reason="т")
    engine_saving_throw(hero, ability, dc=12, advantage=NONE, rng=rng, reason="т")
    monster_saving_throw(
        srd.monsters["ogre"],
        "ogre-1",
        hero.conditions,
        ability,
        dc=12,
        advantage=NONE,
        rng=rng,
        reason="т",
    )

    assert rng.calls == 0


@pytest.mark.parametrize(
    ("advantage", "expected"), [(NONE, 1), (Advantage.ADVANTAGE, 2), (Advantage.DISADVANTAGE, 2)]
)
def test_monster_save_dice_count_follows_the_advantage_mode(srd, advantage, expected):
    rng = constant_dice()

    monster_saving_throw(
        srd.monsters["ogre"], "ogre-1", [], CON, dc=13, advantage=advantage, rng=rng, reason="т"
    )

    assert rng.calls == expected


@pytest.mark.parametrize("dc", [True, "15", None, 15.5, 7.5])
def test_dc_of_the_wrong_type_is_rejected_before_rolling(srd, dc):
    hero = quick_character("fighter", "human", srd, name="Т", concept="т", character_id="pc-1")
    rng = constant_dice()

    with pytest.raises(ValueError, match="Сл"):
        ability_check(hero, STR, skill=None, dc=dc, advantage=NONE, rng=rng, reason="т")

    assert rng.calls == 0


def test_invalid_advantage_value_is_rejected_before_rolling(srd):
    hero = quick_character("fighter", "human", srd, name="Т", concept="т", character_id="pc-1")
    rng = constant_dice()

    with pytest.raises(ValueError):
        saving_throw(hero, STR, dc=15, advantage="foo", rng=rng, reason="т")

    assert rng.calls == 0


def test_contest_rolls_one_die_per_side():
    rng = constant_dice()

    contest(
        ContestSide(actor_id="a", label="А", bonus=2),
        ContestSide(actor_id="b", label="Б", bonus=-1),
        rng=rng,
        reason="тест",
    )

    assert rng.calls == 2


@pytest.mark.parametrize("spent", range(4))
def test_short_rest_rolls_exactly_one_die_per_spent_hit_die(srd, spent):
    hero = quick_character("barbarian", "human", srd, name="Т", concept="т", character_id="pc-1")
    rng = constant_dice(value=5)

    short_rest(hero, spend_hit_dice=spent, rng=rng, content=srd)

    assert rng.calls == spent


def test_ability_set_always_costs_whole_sets_of_twenty_four_dice():
    # Шесть характеристик по четыре кости; несбалансированный набор перебрасывается целиком.
    for seed in range(200):
        rng = CountingRng(SeededRng(seed))

        roll_ability_set(rng)

        assert rng.calls > 0 and rng.calls % 24 == 0, f"seed {seed}: {rng.calls}"


# --- чистота: вход и общие данные не меняются ---


def play_scenario(rng: Rng, srd: SrdContent) -> list[str]:
    """Один и тот же набор действий: создание, проверки, состязание, отдых. Возвращает JSON."""
    hero = random_character(rng, srd, taken_classes=set(), character_id="pc-1")
    rival = quick_character("fighter", "dwarf", srd, name="Т", concept="т", character_id="pc-2")
    scores = roll_ability_set(rng)
    check = ability_check(
        hero, DEX, skill=Skill.STEALTH, dc=15, advantage=NONE, rng=rng, reason="тест"
    )
    save = saving_throw(hero, WIS, dc=15, advantage=NONE, rng=rng, reason="тест", against="poison")
    duel = contest(
        ContestSide(actor_id=hero.id, label="А", bonus=2),
        ContestSide(actor_id=rival.id, label="Б", bonus=3),
        rng=rng,
        reason="тест",
    )
    hurt = hero.model_copy(update={"hp": 1})
    short, short_outcome = short_rest(hurt, spend_hit_dice=2, rng=rng, content=srd)
    long, long_outcome = long_rest(short, srd)
    return [
        hero.model_dump_json(),
        json.dumps(scores),
        check.model_dump_json(),
        save.model_dump_json(),
        duel.model_dump_json(),
        short.model_dump_json(),
        short_outcome.model_dump_json(),
        long.model_dump_json(),
        long_outcome.model_dump_json(),
    ]


def test_scenario_is_reproducible_for_the_same_seed_and_differs_between_seeds(srd):
    first = play_scenario(SeededRng(42), srd)
    again = play_scenario(SeededRng(42), srd)
    other = play_scenario(SeededRng(43), srd)

    # Совпадение с тем же seed означает, что нигде нет скрытого генератора или общего состояния.
    assert first == again
    assert first != other


def test_scenario_does_not_change_the_shared_srd_data(srd):
    before = srd.model_dump(mode="json")

    for seed in range(1, 40):
        play_scenario(SeededRng(seed), srd)
        hero = random_character(SeededRng(seed), srd, taken_classes=set(), character_id="p")
        # Изменение того, что вернул движок, не должно дойти до кешированных данных.
        hero.spell_slots.clear()
        hero.spell_slots_max.clear()
        hero.resources.clear()
        hero.inventory.clear()
        hero.cantrips.clear()
        hero.prepared_spells.clear()
        hero.skill_proficiencies.clear()
        hero.save_proficiencies.clear()

    assert srd.model_dump(mode="json") == before


def test_checks_and_rests_leave_their_input_untouched_for_many_characters(srd):
    for seed in range(1, 80):
        hero = random_character(SeededRng(seed), srd, taken_classes=set(), character_id="p")
        hero = hero.model_copy(update={"hp": 1, "hit_dice": 2}, deep=True)
        before = hero.model_copy(deep=True)
        rng = SeededRng(seed)

        ability_check(hero, DEX, skill=Skill.STEALTH, dc=15, advantage=NONE, rng=rng, reason="т")
        saving_throw(hero, WIS, dc=15, advantage=NONE, rng=rng, reason="т", against="poison")
        engine_saving_throw(hero, CON, dc=12, advantage=NONE, rng=rng, reason="т")
        short_rest(hero, spend_hit_dice=2, rng=rng, content=srd)
        long_rest(hero, srd)

        assert hero == before, f"seed {seed}"
