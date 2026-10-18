"""Инварианты движка, которые должны держаться во всех модулях сразу.

Проверяются границы пакета (нет сети, БД, чужих генераторов случайных чисел), честность
расхода костей (бросок только через переданный `Rng` и без лишних значений), чистота функций
(вход и общие данные SRD не меняются) и воспроизводимость сценария по seed.
"""

import ast
import json
from pathlib import Path

import pytest

import dnd_engine
from dnd_engine.abilities import Ability, Skill
from dnd_engine.checks import (
    VALID_DCS,
    ContestSide,
    ability_check,
    contest,
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

# Движок — чистый Python: сеть, БД, процессы и клиенты моделей ему недоступны.
FORBIDDEN_IMPORTS = {
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "sqlite3",
    "sqlalchemy",
    "subprocess",
    "anthropic",
    "openai",
}
# Генераторы случайных чисел разрешены только в `rng.py`; остальной код бросает через `Rng`.
RANDOMNESS_IMPORTS = {"random", "secrets"}
ENVIRONMENT_ATTRIBUTES = {"environ", "getenv"}


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


def imported_roots(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def test_package_files_are_found():
    names = {path.name for path in MODULE_FILES}

    assert {"rng.py", "dice.py", "creation.py", "checks.py", "rest.py"} <= names


@pytest.mark.parametrize("path", MODULE_FILES, ids=lambda p: p.relative_to(PACKAGE_DIR).as_posix())
def test_engine_module_has_no_network_database_or_environment_access(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert not imported_roots(path) & FORBIDDEN_IMPORTS
    assert not attributes & ENVIRONMENT_ATTRIBUTES


@pytest.mark.parametrize("path", MODULE_FILES, ids=lambda p: p.relative_to(PACKAGE_DIR).as_posix())
def test_only_rng_module_imports_random_generators(path):
    if path.name == "rng.py":
        assert imported_roots(path) & RANDOMNESS_IMPORTS
    else:
        assert not imported_roots(path) & RANDOMNESS_IMPORTS


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
        short_rest(hero, spend_hit_dice=2, rng=rng, content=srd)
        long_rest(hero, srd)

        assert hero == before, f"seed {seed}"
