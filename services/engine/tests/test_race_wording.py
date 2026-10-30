"""Слово «раса» в текстах движка: поле данных остаётся `species`,
а там, где слово видят игрок или модель, расу не называют «видом».

Модель видит описания и схемы всех инструментов реестра в каждом ходе; игрок видит проблемы
создания героя (сервер отдаёт их в 422) и тексты SRD. Проверки идут по самим текстам, которые
движок отдаёт наружу, а не по исходникам: так ловится и новая строка, и собранная фраза.
Заодно собранные фразы «неизвестн… <что>» согласуются по роду с тем, что не нашлось.
"""

import json
import re
from collections.abc import Callable

import pytest
from tool_calls import BOTH_MODULES

from dnd_engine.abilities import Ability, Skill
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import (
    AbilityMethod,
    CharacterChoices,
    CharacterValidationError,
    build_character,
    quick_character,
    random_point_buy,
)
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import SeededRng
from dnd_engine.tools import build_registry

# Любая форма слова «вид» в любом регистре. Исключены два значения, которые есть в текстах
# движка и к расе отношения не имеют: «запись костей вида 2d6» (форма записи) и «видов оружия»
# (разновидности оружия в мастерстве классов).
RACE_AS_KIND = re.compile(r"\bвид(?:а|ам|ами|ах|е|ов|ом|у|ы)?\b(?! \d*d\d| оружия)", re.IGNORECASE)

REGISTRY = build_registry(build_ruleset(BOTH_MODULES))
TOOL_NAMES = [spec.name for spec in REGISTRY.specs()]


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def kind_mentions(text: str) -> list[str]:
    """Места, где текст называет расу «видом», с окрестностью для понятного сообщения."""
    return [text[max(0, m.start() - 40) : m.end() + 20] for m in RACE_AS_KIND.finditer(text)]


# --- сам шаблон ---


@pytest.mark.parametrize(
    "text",
    [
        "черты вида применяет движок",
        "умения классов и видов",
        "навык уже даёт вид «Эльф»",
        "неизвестный вид: 'gnoll'",
        "Видом героя выбран дварф",
        "раса или ВИД",
    ],
)
def test_the_pattern_catches_the_race_called_a_kind_in_any_form_and_case(text):
    assert RACE_AS_KIND.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "текст, который увидит только он",
        "отряд видит его и так",
        "в видимое свободное место",
        "Невидим",
        "запись костей вида 2d6 и тип урона",
        "запись костей вида 2d4+2",
        "свойства мастерства двух видов оружия",
    ],
)
def test_the_pattern_ignores_other_words_and_other_meanings_of_the_word(text):
    assert not RACE_AS_KIND.search(text)


# --- то, что видит модель: описания и схемы инструментов ---


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_tool_description_and_schema_never_call_the_race_a_kind(name):
    spec = next(spec for spec in REGISTRY.specs() if spec.name == name)
    # Модель получает и описание, и схему аргументов (с описаниями полей и моделей).
    text = f"{spec.description}\n{json.dumps(spec.json_schema(), ensure_ascii=False)}"

    assert kind_mentions(text) == []


def test_the_scan_sees_the_tools_that_used_to_call_the_race_a_kind():
    assert {"request_save", "combat_action", "heal", "damage"} <= set(TOOL_NAMES)


def test_srd_texts_never_call_the_race_a_kind(srd):
    assert kind_mentions(srd.model_dump_json()) == []


def test_the_srd_scan_sees_the_card_texts_of_hero_creation(srd):
    # Описание расы, инструмент предыстории и описание навыка игрок читает в карточках создания
    # героя; проверка выше смотрит на дамп SRD, поэтому каждый такой текст должен в него попасть.
    dump = srd.model_dump_json()
    texts = [
        *(kind.description for kind in srd.species.values()),
        *(background.tool for background in srd.backgrounds.values()),
        *(skill.summary for skill in srd.skills.values()),
    ]

    assert len(texts) == len(srd.species) + len(srd.backgrounds) + len(Skill)
    assert [text for text in texts if text not in dump] == []


# --- то, что видит игрок: проблемы создания героя ---


def choices(**fields: object) -> CharacterChoices:
    """Выбор героя с нейтральными значениями: тест задаёт только то, что ломает."""
    base: dict[str, object] = {
        "name": "Тестовый герой",
        "species": "dwarf",
        "class_id": "barbarian",
        "background": "soldier",
        "method": AbilityMethod.POINT_BUY,
        "base_scores": dict.fromkeys(Ability, 10),
        "background_bonus": {},
        "skills": [],
    }
    return CharacterChoices.model_validate(base | fields)


def problems_of(make: Callable[[SrdContent], object], srd: SrdContent) -> list[str]:
    with pytest.raises(CharacterValidationError) as info:
        make(srd)
    return info.value.problems


def build(**fields: object) -> Callable[[SrdContent], object]:
    return lambda srd: build_character(choices(**fields), srd, character_id="pc-1")


# Каждый путь создания героя, где в проблеме названа раса или то, что не нашлось, и фраза,
# которую игрок должен увидеть. Фразы «неизвестн…» согласованы по роду с названием.
CREATION_PROBLEMS = {
    "build: неизвестная раса": (build(species="gnoll"), "неизвестная раса: 'gnoll'"),
    "build: неизвестный класс": (build(class_id="paladin"), "неизвестный класс: 'paladin'"),
    "build: неизвестная предыстория": (
        build(background="pirate"),
        "неизвестная предыстория: 'pirate'",
    ),
    "build: неизвестное заклинание": (
        build(class_id="wizard", background="sage", cantrips=["no_spell"]),
        "неизвестное заклинание 'no_spell'",
    ),
    "quick: неизвестная раса": (
        lambda srd: quick_character(
            "fighter", "gnoll", srd, name="Г", concept="к", character_id="pc-1"
        ),
        "неизвестная раса: 'gnoll'",
    ),
    "quick: неизвестный класс": (
        lambda srd: quick_character(
            "paladin", "human", srd, name="Г", concept="к", character_id="pc-1"
        ),
        "неизвестный класс: 'paladin'",
    ),
    "random_point_buy: неизвестный класс": (
        lambda srd: random_point_buy(SeededRng(1), srd, "paladin"),
        "неизвестный класс: 'paladin'",
    ),
    "навык уже даёт раса": (
        build(species="elf", class_id="rogue", background="criminal", skills=[Skill.PERCEPTION]),
        "навык «Внимательность» уже даёт раса «Эльф»",
    ),
    "раса даёт навык на выбор": (
        build(species="human"),
        "раса «Человек» даёт навык на выбор: укажите extra_skill",
    ),
    "раса не даёт навык на выбор": (
        build(extra_skill=Skill.NATURE),
        "extra_skill не нужен: раса «Дварф» не даёт навык на выбор",
    ),
}


@pytest.mark.parametrize(("make", "phrase"), CREATION_PROBLEMS.values(), ids=CREATION_PROBLEMS)
def test_creation_problem_names_the_race_and_agrees_in_gender(srd, make, phrase):
    problems = problems_of(make, srd)

    assert any(phrase in problem for problem in problems), problems
    assert kind_mentions("\n".join(problems)) == []
