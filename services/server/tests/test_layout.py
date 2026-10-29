"""Раскладка тестов workspace: у модулей тестов engine и server нет общих базовых имён.

В `services/*/tests` нет `__init__.py`, и pytest (режим импорта `prepend`) импортирует каждый файл
по базовому имени, добавив его папку в `sys.path`. Два `test_<имя>.py` с одним именем в общем
прогоне `pytest services/engine services/server` дают «import file mismatch», и второй модуль не
собирается вовсе; два вспомогательных модуля с одним именем (`evening.py`, `generator_fakes.py`)
импортировались бы молча не те. Только `conftest.py` pytest грузит сам, и он повторяется по папкам.
"""

from collections import defaultdict
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2]
TEST_ROOTS = (SERVICES / "engine" / "tests", SERVICES / "server" / "tests")


def test_modules_of_the_engine_and_server_tests_have_distinct_base_names():
    places: dict[str, list[str]] = defaultdict(list)
    for root in TEST_ROOTS:
        for path in root.rglob("*.py"):
            if path.name != "conftest.py":
                places[path.name].append(path.relative_to(SERVICES).as_posix())

    clashes = {name: sorted(paths) for name, paths in places.items() if len(paths) > 1}

    assert clashes == {}
