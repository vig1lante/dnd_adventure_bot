"""Метры на выводе (спека DND-2, Р3 и §6): движок хранит числа SRD как в правилах,
а текст для людей и модели пишет в метрах по правилу 5 футов = 1,5 м.

Переводит одна функция движка; данные SRD и исходники движка проверяются на отсутствие
футов чтением файлов, поэтому новая строка с футами не пройдёт незамеченной.
"""

import re
from pathlib import Path

import pytest

import dnd_engine
from dnd_engine.units import meters, meters_text, meters_text_pair

# Футы -> (метры, текст для людей).
CONVERSIONS = {
    5: (1.5, "1,5 м"),
    10: (3.0, "3 м"),
    15: (4.5, "4,5 м"),
    30: (9.0, "9 м"),
    60: (18.0, "18 м"),
    80: (24.0, "24 м"),
    120: (36.0, "36 м"),
    320: (96.0, "96 м"),
}

ENGINE_DIR = Path(dnd_engine.__file__).parent
SRD_FILES = sorted((ENGINE_DIR / "data" / "srd").glob("*.yaml"))
# units.py — единственное место, где футы названы: это и есть перевод.
ENGINE_MODULES = sorted(path for path in ENGINE_DIR.rglob("*.py") if path.name != "units.py")
# Любые формы слова «фут», сокращение «фт» и английские feet/foot/ft, в любом регистре.
FEET = re.compile(
    r"\bфут(?:а|ам|ами|ах|е|ов|ом|у|ы)?\b|\bфт\b|\bfeet\b|\bfoot\b|\bft\b", re.IGNORECASE
)


@pytest.mark.parametrize(("feet", "expected"), [(f, m) for f, (m, _) in CONVERSIONS.items()])
def test_meters_follow_the_five_feet_to_one_and_a_half_meters_rule(feet, expected):
    assert meters(feet) == expected


@pytest.mark.parametrize(("feet", "text"), [(f, t) for f, (_, t) in CONVERSIONS.items()])
def test_meters_text_uses_a_comma_and_drops_a_zero_fraction(feet, text):
    assert meters_text(feet) == text


def test_meters_text_pair_writes_normal_and_long_range_with_one_unit():
    assert meters_text_pair(80, 320) == "24/96 м"
    assert meters_text_pair(20, 60) == "6/18 м"
    assert meters_text_pair(30, 90) == "9/27 м"


def test_odd_and_fractional_feet_convert_without_float_noise():
    # 3 × 0.3 во float — 0.8999999999999999; игрок не должен увидеть такой хвост.
    assert meters(3) == 0.9
    assert meters_text(3) == "0,9 м"
    assert meters(2.5) == 0.75
    assert meters_text(2.5) == "0,75 м"
    assert meters_text(35) == "10,5 м"


def test_zero_is_zero_meters_without_a_fraction():
    assert meters(0) == 0
    assert meters_text(0) == "0 м"


def test_whole_feet_given_as_a_float_read_like_an_integer():
    # Тип допускает float: 30.0 из JSON или YAML пишется так же, как 30, без хвоста «,0».
    assert meters_text(30.0) == "9 м"
    assert meters_text_pair(80.0, 320.0) == "24/96 м"


@pytest.mark.parametrize(("normal", "long", "text"), [(5, 15, "1,5/4,5 м"), (150, 600, "45/180 м")])
def test_meters_text_pair_writes_each_side_like_a_single_distance(normal, long, text):
    # Дробные метры — с запятой с обеих сторон; 150/600 — самая большая дальность SRD (длинный лук).
    assert meters_text_pair(normal, long) == text


@pytest.mark.parametrize("word", ["фут", "ФУТОВ", "футах", "Футы", "фт", "feet", "Foot", "ft"])
def test_the_feet_pattern_catches_any_form_and_case(word):
    assert FEET.search(f"на 30 {word}.")


def test_the_feet_pattern_ignores_words_that_merely_contain_the_letters():
    assert not FEET.search("футляр, left, feetless")


@pytest.mark.parametrize("path", SRD_FILES, ids=lambda p: p.name)
def test_srd_data_has_no_feet(path):
    lines = path.read_text(encoding="utf-8").splitlines()

    assert [f"{n}: {line}" for n, line in enumerate(lines, 1) if FEET.search(line)] == []


@pytest.mark.parametrize("path", ENGINE_MODULES, ids=lambda p: str(p.relative_to(ENGINE_DIR)))
def test_engine_messages_and_comments_have_no_feet(path):
    lines = path.read_text(encoding="utf-8").splitlines()

    assert [f"{n}: {line}" for n, line in enumerate(lines, 1) if FEET.search(line)] == []


def test_the_scans_see_all_srd_tables_and_the_combat_engine():
    assert {p.stem for p in SRD_FILES} >= {"monsters", "items", "species", "classes", "spells"}
    assert ENGINE_DIR / "combat" / "engine.py" in ENGINE_MODULES
