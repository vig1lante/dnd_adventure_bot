"""Общие фикстуры тестов движка."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tool_calls import ALLY, BOTH_MODULES, HERO, WIZARD

from dnd_engine.abilities import Ability
from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character, InventoryItem
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState
from dnd_engine.tools import build_registry
from dnd_engine.tools.registry import ToolContext, ToolOutcome, ToolRegistry

NEUTRAL_SCORE = 10  # модификатор 0: тест сам задаёт только те числа, которые проверяет
# Корень репозитория считаем от расположения файла (tests -> engine -> services -> корень):
# абсолютных путей машины в тестах быть не должно.
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[3] / "content" / "adventures" / "demo-crypt"


@pytest.fixture
def make_character() -> Callable[..., Character]:
    """Фабрика персонажа: `make_character(**overrides)`.

    По умолчанию — воин 3-го уровня без владений, снаряжения и заклинаний, все
    характеристики по 10. В `scores` можно передать только нужные характеристики,
    остальные останутся по 10. Остальные поля переопределяются целиком.
    """

    def factory(**overrides: Any) -> Character:
        unknown = overrides.keys() - Character.model_fields.keys()
        if unknown:
            # Опечатка в имени поля молча превратила бы тест в проверку значений по умолчанию.
            raise TypeError(f"у Character нет полей {sorted(unknown)}")
        scores = dict.fromkeys(Ability, NEUTRAL_SCORE) | overrides.pop("scores", {})
        base: dict[str, Any] = {
            "id": "pc-1",
            "name": "Тестовый герой",
            "species": "human",
            "class_id": "fighter",
            "background": "soldier",
            "skill_proficiencies": [],
            "save_proficiencies": [],
            "hp_max": 20,
            "hp": 20,
            "hit_dice_max": 3,
            "hit_dice": 3,
        }
        return Character(**(base | overrides | {"scores": scores}))

    return factory


@pytest.fixture(scope="session")
def demo_adventure() -> Adventure:
    """Рукописное приключение `demo-crypt` из `content/adventures/`.

    `Adventure` неизменяем (даже словари внутри заморожены), поэтому один экземпляр
    на всю сессию безопасно делят все тесты.
    """
    return load_adventure(DEMO_ADVENTURE_DIR)


# --- инструменты мастера: реестр, состояние игры и контекст вызова ---


@pytest.fixture(scope="session")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="session")
def tool_registry() -> ToolRegistry:
    """Реестр, как его собирает игра для приключения с обоими модулями правил."""
    return build_registry(build_ruleset(BOTH_MODULES))


@pytest.fixture
def make_fighter(make_character: Callable[..., Character]) -> Callable[..., Character]:
    """Воин 3-го уровня с булавой в руках: Сила 16, 30 HP; всё переопределяется аргументами."""

    def factory(**overrides: Any) -> Character:
        fields = {
            "id": "pc-1",
            "name": "Брон",
            "background": "acolyte",
            "scores": {Ability.STR: 16},
            "inventory": [InventoryItem(item_id="mace", qty=1, equipped=True)],
            "hp_max": 30,
            "hp": 30,
        }
        return make_character(**(fields | overrides))

    return factory


@pytest.fixture
def make_game(make_fighter: Callable[..., Character]) -> Callable[..., GameState]:
    """Состояние `demo-crypt`: по умолчанию два воина (`pc-1`, `pc-2`) в деревне, оба модуля."""

    def factory(*party: Character, **overrides: Any) -> GameState:
        heroes = party or (make_fighter(), make_fighter(id="pc-2", name="Лира"))
        fields = {
            "game_id": "game-1",
            "adventure_id": "demo-crypt",
            "rules": BOTH_MODULES,
            "party": {hero.id: hero for hero in heroes},
            "location_id": "village",
        }
        return GameState(**(fields | overrides))

    return factory


@pytest.fixture
def make_tool_ctx(
    demo_adventure: Adventure, srd: SrdContent, make_game: Callable[..., GameState]
) -> Callable[..., ToolContext]:
    """Контекст вызова: `make_tool_ctx(state, rng=..., acting=..., secret_actors=...)`.

    По умолчанию кубов нет (пустой `ScriptedRng`): неожиданный бросок сразу роняет тест."""

    def factory(state: GameState | None = None, **overrides: Any) -> ToolContext:
        state = state or make_game()
        fields = {
            "state": state,
            "adventure": demo_adventure,
            "content": srd,
            "rules": build_ruleset(state.rules),
            "rng": ScriptedRng([]),
        }
        return ToolContext(**(fields | overrides))

    return factory


@pytest.fixture
def call_tool(tool_registry: ToolRegistry) -> Callable[..., ToolOutcome]:
    """`call_tool(ctx, "reveal", ref="feature:tracks")`: вызов, как его делает модель."""

    def call(ctx: ToolContext, tool: str, /, **args: Any) -> ToolOutcome:
        outcome = tool_registry.call(ctx, tool, args)
        json.dumps(outcome.model_dump())  # результат для модели и журнала — чистый JSON
        return outcome

    return call


@pytest.fixture
def make_scene(
    make_game: Callable[..., GameState],
    make_tool_ctx: Callable[..., ToolContext],
    call_tool: Callable[..., ToolOutcome],
    make_fighter: Callable[..., Character],
    make_character: Callable[..., Character],
) -> Callable[..., ToolContext]:
    """Контекст на кладбище: `make_scene(first=..., party=..., fight=..., engaged=..., secret=...)`.

    Отряд — воины `pc-1` (10 HP из 30) и `pc-2` и маг `wiz`; `first` ходит первым. Ключ сундука
    уже у `pc-1`. С `fight=True` бой «Встающие кости» начат, ходят все бойцы по очереди, а
    `engaged=True` ставит первого героя в ближний бой со скелетом. Кубы — `SeededRng(5)`."""

    def build(
        *,
        first: str = HERO,
        party: tuple[str, ...] = (HERO, ALLY, WIZARD),
        fight: bool = False,
        engaged: bool = False,
        secret: tuple[str, ...] = (),
    ) -> ToolContext:
        wizard = make_character(
            id=WIZARD,
            name="Маг",
            class_id="wizard",
            background="sage",
            scores={Ability.INT: 16},
            cantrips=["fire_bolt"],
            prepared_spells=["magic_missile"],
            spell_slots={1: 2},
            spell_slots_max={1: 2},
        )
        heroes = {
            HERO: make_fighter(hp=10),
            ALLY: make_fighter(id=ALLY, name="Лира"),
            WIZARD: wizard,
        }
        order = [heroes[first], *(heroes[who] for who in party if who != first)]
        state = make_game(*order, location_id="cemetery")
        state.world.item_holders["chest-key"] = f"character:{HERO}"
        initiative = [20 - n for n in range(len(order))] + [2, 1, 1, 1, 1]
        ctx = make_tool_ctx(state, rng=ScriptedRng(initiative), secret_actors=set(secret))
        if fight:
            assert call_tool(ctx, "start_combat", encounter_id="risen-bones").error is None
            combat = ctx.state.combat
            ctx.acting = tuple(combat.combatants)
            if engaged:
                combat.combatants[first].zone_id = "graves"
                combat.combatants[first].engaged_with.append("skeleton-1")
                combat.combatants["skeleton-1"].engaged_with.append(first)
        ctx.rng = SeededRng(5)
        return ctx

    return build
