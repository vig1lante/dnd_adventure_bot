"""В коде сервера нет небезопасных источников случайности.

Броски делает только `SystemRng` (`secrets.SystemRandom`). Модуль `random`, `SeededRng` и
`ScriptedRng` в рабочем коде сервера недопустимы: первый предсказуем, остальные подменяют броски
заданными числами. Проверка читает исходники `src/dnd_server` как деревья разбора, поэтому она не
зависит от того, какие модули загружены. Тесты вправе брать `SeededRng`: приложение держит
генератор в `app.state.rng`, и подмена возможна только из кода тестов, а не из настроек.
"""

import ast
from pathlib import Path

import pytest

from dnd_engine.rng import SystemRng
from dnd_server.app import create_app
from dnd_server.config import Settings

# tests/characters/test_no_insecure_rng.py -> characters -> tests -> server
SOURCE_DIR = Path(__file__).resolve().parents[2] / "src" / "dnd_server"
FORBIDDEN_RNG_NAMES = frozenset({"SeededRng", "ScriptedRng"})


def insecure_randomness(tree: ast.AST) -> list[str]:
    """Места в дереве, где подключён `random` или упомянуты предсказуемые генераторы."""
    found = []
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                found += [
                    f"import {alias.name}"
                    for alias in names
                    if alias.name == "random" or alias.name.startswith("random.")
                ]
            case ast.ImportFrom(module=module, names=names):
                if module == "random" or (module or "").startswith("random."):
                    found.append(f"from {module} import …")
                found += [
                    f"import {alias.name}" for alias in names if alias.name in FORBIDDEN_RNG_NAMES
                ]
            case ast.Name(id=name) | ast.Attribute(attr=name) | ast.Constant(value=str() as name):
                if name in FORBIDDEN_RNG_NAMES:
                    found.append(name)
    return found


def rng_constructors(tree: ast.AST) -> list[str]:
    """Имена вызываемых классов генераторов (всё, что называется `…Rng`)."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name.endswith("Rng"):
                names.append(name)
    return names


def server_sources() -> list[tuple[Path, ast.AST]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(SOURCE_DIR.rglob("*.py"))
    ]


def test_server_code_has_no_random_module_and_no_seeded_or_scripted_rng():
    problems = {
        str(path.relative_to(SOURCE_DIR)): found
        for path, tree in server_sources()
        if (found := insecure_randomness(tree))
    }

    assert problems == {}


def test_the_only_generator_the_server_creates_is_system_rng():
    created = [name for _path, tree in server_sources() for name in rng_constructors(tree)]

    assert created
    assert set(created) == {"SystemRng"}


@pytest.mark.parametrize(
    "source",
    [
        "import random",
        "import random.foo",
        "from random import randint",
        "from dnd_engine.rng import SeededRng",
        "from dnd_engine.rng import ScriptedRng as Fixed",
        "import dnd_engine.rng as rng\nrng.SeededRng(1)",
        "make = ScriptedRng",
        "getattr(module, 'SeededRng')",
    ],
)
def test_the_scan_catches_what_it_is_looking_for(source):
    assert insecure_randomness(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "import secrets",
        "from dnd_engine.rng import SystemRng",
        "rng = SystemRng()",
        "randomize = 1",
    ],
)
def test_the_scan_passes_clean_code(source):
    assert insecure_randomness(ast.parse(source)) == []


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_the_app_rolls_with_system_rng_in_every_environment(settings: Settings, env):
    app = create_app(settings.model_copy(update={"app_env": env}))

    assert type(app.state.rng) is SystemRng


def test_no_setting_can_choose_the_random_generator():
    names = [name for name in Settings.model_fields if "seed" in name or "rng" in name]

    assert names == []
