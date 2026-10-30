"""Страж: в коде и тестах движка нет отсылок к документам, которых нет в репозитории.

Комментарии, docstring, названия тестов и id параметров объясняют поведение своими словами.
Пути к локальным папкам владельца, номера задач и пунктов, параграфы документов и одиночные
идентификаторы решений и находок читатель репозитория открыть не может. Страж ищет их во всех
файлах `.py` и `.yaml` пакета движка и его тестов; сам этот файл он не проверяет — в нём образцы.
"""

import re
from pathlib import Path

import pytest

import dnd_engine

ENGINE_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(dnd_engine.__file__).parent
TESTS_DIR = ENGINE_DIR / "tests"

# Каждый образец собран так, чтобы не находить сам себя в этом файле: соседние знаки разбиты
# классами вида `[s]`, а знак параграфа записан кодом.
REFERENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "путь к локальным документам": re.compile(r"docs/superpower[s]|loop-engineerin[g]"),
    "номер задачи": re.compile(r"(?i)\bdnd-?[0-9]"),
    "спека": re.compile(r"\b[Сс]пек(?:а|и|е|у|ой)\b"),
    "дополнение контракта": re.compile(r"[Дд]ополнени\w* контракт"),
    "пункт плана или блок": re.compile(
        r"\b(?:план|блок)\w* B(?:[0-9]|\b)|\(B[0-9]+(?:\.[0-9]+)*\)"
        r"|\bB[0-9]+\.(?:[0-9]+|coverage|fix)"
    ),
    "этап цикла": re.compile(r"[Фф]инальн\w* покры[т]|[Сс]ценари\w* ревь[ю]|[Кк]онтракт\w* бло[к]"),
    "вариант решения буквой": re.compile(r"«[ВB]»"),
    "параграф документа": re.compile("§"),
    "одиночный id решения или находки": re.compile(
        r"(?<![\w.-])(?:[BRSNFOРДО][0-9]{1,2}|D(?!20\b)[0-9]{1,2})(?![\w.-])"
    ),
    "буква инварианта": re.compile(r"инвариант\w* «[а-яa-z]»"),
}


def checked_files() -> list[Path]:
    files = [
        path
        for root in (PACKAGE_DIR, TESTS_DIR)
        for pattern in ("*.py", "*.yaml")
        for path in root.rglob(pattern)
        if "__pycache__" not in path.parts
    ]
    return sorted(set(files) - {Path(__file__).resolve()})


def references_in(text: str) -> list[str]:
    return [
        f"{kind}: {match.group(0)!r}"
        for kind, pattern in REFERENCE_PATTERNS.items()
        for match in pattern.finditer(text)
    ]


def test_the_guard_sees_the_package_and_the_tests():
    files = checked_files()

    assert PACKAGE_DIR / "state.py" in files
    assert TESTS_DIR / "test_state.py" in files
    assert any(path.suffix == ".yaml" for path in files)
    assert Path(__file__).resolve() not in files


def test_no_file_of_the_engine_refers_to_a_local_document():
    found = [
        f"{path.relative_to(ENGINE_DIR)}:{number}: {reference}"
        for path in checked_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for reference in references_in(line)
    ]

    assert found == []


@pytest.mark.parametrize(
    "line",
    [
        "# план B4.6: переход и находки",
        "# правило блока B11 о тайнах",
        "# --- короткий привал (B9) ---",
        "# поправка (B11.3)",
        "# --- B8.coverage: дневник ---",
        "# см. спеку §5.7",
        "# спека DND-2",
        "# дополнение контракта: поле hour",
        "# дополнения контракта B8",
        "# подробности в docs/superpowers/plans",
        "# журнал в loop-engineering/runs",
        "# решение D9 владельца",
        "# находка F13",
        "# правка R2 и S1",
        "# вопрос N1",
        "# решение владельца О7",
        "# поправка Р1",
        '@pytest.mark.parametrize("x", [1], ids=["dnd1-camp"])',
        "# держится инвариант «а»",
        "# --- финальное покрытие блока: тайны ---",
        "# Финальные покрытия дневника",
        "# Тот самый сценарий ревью: два сундука",
        "# по сценариям ревью",
        "# Общий контракт блока: формат ошибок",
        "# контракта блоков сервера",
        "# сквозной тест (план B)",
        "# блок B: дневник",
        "# героев показывают в порядке «В»",
    ],
)
def test_the_guard_catches_every_kind_of_reference(line):
    assert references_in(line) != []


@pytest.mark.parametrize(
    "line",
    [
        "# статблок монстра из приключения",
        "# путешествие по планах бытия",
        "# дополнение текста словами мастера",
        "# бросок D20 и кость d8",
        "# формат v2, поле hp_max",
        "from dnd_engine.state import GameTime",
        "# спектр урона и спекуляции",
        "# контракт функции боя: вход не меняется",
        "# финальный бой с боссом и покрытие зон",
        "# сценарий боя: гоблины у ворот",
        "# блок кода и план боя мастера",
        "# формат записи — договор с сервером, а не контракт функции",
        "# «Вдохновение» и «В бой!» — тексты для игроков",
        "# имя Bob и план Bravo",
    ],
)
def test_the_guard_leaves_ordinary_words_alone(line):
    assert references_in(line) == []
