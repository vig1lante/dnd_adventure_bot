"""Инструменты боя, рассказа и модулей, сборка реестра.

Инструменты зовутся через настоящий `ToolRegistry` из `build_registry`, как их будет звать модель
мастера. Приключение — `demo-crypt`: бой «Встающие кости» на кладбище (скелеты в могилах, зомби у
сторожки, отряд у ворот) и финальный бой в склепе. Броски — на `ScriptedRng`: значения кубов
заданы тестом, а остаток в очереди после вызова доказывает, что лишних бросков не было.

Кто ходит в этом вызове мастера, говорит `ctx.acting` (очередь бойцов): инструмент продвигает
ход только к бойцу из неё и только если он следующий способный действовать.
"""

import json
from collections.abc import Callable
from typing import Any, get_args

import pytest
from adventure_factory import adventure_data, custom_monster_data

from dnd_engine.abilities import Ability
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import MAX_MONSTERS_PER_ENCOUNTER, validate
from dnd_engine.character import ActiveCondition, Character, DeathSaves, InventoryItem
from dnd_engine.combat.engine import EXTERNAL_CONDITION_MAX_ROUNDS, current_actor, end_scene
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import MODEL_DICE_MAX_CHARS
from dnd_engine.events import (
    ChronicleEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    HealEvent,
    InspirationEvent,
    ItemEvent,
    PrivateMessageEvent,
    RestEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.modules import MODULES, build_ruleset
from dnd_engine.modules.base import RuleSet
from dnd_engine.modules.campfire import begin_rest
from dnd_engine.rest import RestError
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState, RulesRef, Scene, SceneType
from dnd_engine.tools import build_registry
from dnd_engine.tools.combat import COMBAT_TOOLS, MAX_MONSTERS
from dnd_engine.tools.narrative import NARRATIVE_TOOLS, ConditionId, DamageType
from dnd_engine.tools.registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec

STR, DEX, CON, INT, WIS, CHA = Ability
HERO, ALLY = "pc-1", "pc-2"
SKELETON_1, SKELETON_2, SKELETON_3 = "skeleton-1", "skeleton-2", "skeleton-3"
ZOMBIE_1, ZOMBIE_2 = "zombie-1", "zombie-2"
BOTH_MODULES = RulesRef(modules=["bg3_flavor", "campfire"])
# Инициатива «Встающих костей» для одного героя: герой 20, скелеты 5, 4, 4 (с Лов +3), зомби −1, −1.
ONE_HERO_INITIATIVE = [20, 2, 1, 1, 1, 1]
# То же для двух героев: 20 и 19, затем те же скелеты и зомби.
TWO_HEROES_INITIATIVE = [20, 19, 2, 1, 1, 1, 1]
ALL_FIGHTERS = (HERO, SKELETON_1, SKELETON_2, SKELETON_3, ZOMBIE_1, ZOMBIE_2)

# --- помощники ---


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def item(item_id: str, qty: int = 1, equipped: bool = True) -> InventoryItem:
    return InventoryItem(item_id=item_id, qty=qty, equipped=equipped)


@pytest.fixture
def make_hero(make_character: Callable[..., Character]) -> Callable[..., Character]:
    """Воин 3-го уровня, Сила 16: булава (атака +5, урон 1к6+3, скелеты уязвимы), HP 30."""

    def factory(**overrides: Any) -> Character:
        fields = {
            "id": HERO,
            "name": "Герой",
            "background": "acolyte",
            "scores": {STR: 16},
            "inventory": [item("mace")],
            "hp_max": 30,
            "hp": 30,
        }
        return make_character(**(fields | overrides))

    return factory


@pytest.fixture
def make_state(make_hero: Callable[..., Character]) -> Callable[..., GameState]:
    """Состояние `demo-crypt` на кладбище; по умолчанию в отряде один герой."""

    def factory(*party: Character, **overrides: Any) -> GameState:
        heroes = party or (make_hero(),)
        fields = {
            "game_id": "game-1",
            "adventure_id": "demo-crypt",
            "rules": BOTH_MODULES,
            "party": {hero.id: hero for hero in heroes},
            "location_id": "cemetery",
        }
        return GameState(**(fields | overrides))

    return factory


@pytest.fixture
def make_ctx(
    demo_adventure: Adventure, srd: SrdContent, make_state: Callable[..., GameState]
) -> Callable[..., ToolContext]:
    """Контекст вызова: `make_ctx(state, rng=..., adventure=..., secret_actors=...)`."""

    def factory(state: GameState | None = None, **overrides: Any) -> ToolContext:
        state = state or make_state()
        fields = {
            "state": state,
            "adventure": demo_adventure,
            "content": srd,
            "rules": build_ruleset(state.rules),
            "rng": ScriptedRng([10] * 30),
        }
        return ToolContext(**(fields | overrides))

    return factory


@pytest.fixture
def tools() -> ToolRegistry:
    return build_registry(build_ruleset(BOTH_MODULES))


@pytest.fixture
def run(tools: ToolRegistry) -> Callable[..., ToolOutcome]:
    """`run(ctx, "attack", actor_id=..., target_id=...)`: вызов, как его делает модель."""

    def call(ctx: ToolContext, tool: str, /, **args: Any) -> ToolOutcome:
        outcome = tools.call(ctx, tool, args)
        json.dumps(outcome.model_dump())  # результат для модели и журнала — чистый JSON
        return outcome

    return call


def dice(ctx: ToolContext, *values: int) -> ScriptedRng:
    """Следующий вызов бросает ровно эти значения; остаток проверяет `leftover`."""
    ctx.rng = ScriptedRng(values)
    return ctx.rng


def leftover(rng: ScriptedRng) -> bool:
    """Остались ли в очереди неиспользованные значения кубов."""
    try:
        rng.randint(1, 100)
    except ValueError:
        return False
    return True


def events_of(ctx: ToolContext, kind: type) -> list:
    return [event for event in ctx.events if isinstance(event, kind)]


def fighter(ctx: ToolContext, fighter_id: str):
    return ctx.state.combat.combatants[fighter_id]


def actor_id(ctx: ToolContext) -> str:
    return current_actor(ctx.state).id


def refused(run, ctx: ToolContext, tool: str, part: str, /, **args: Any) -> ToolOutcome:
    """Вызов отклонён: ошибка с `part`, состояние и события не тронуты, кубы целы."""
    before, count = ctx.state.model_copy(deep=True), len(ctx.events)
    rng = dice(ctx, 10)
    outcome = run(ctx, tool, **args)
    assert outcome.result is None, outcome
    assert part in outcome.error, outcome.error
    assert ctx.state == before and len(ctx.events) == count
    assert leftover(rng), "отказ потратил кубы"
    return outcome


@pytest.fixture
def started(make_ctx, make_state, run) -> ToolContext:
    """Бой «Встающие кости» с одним героем уже начат; в этом вызове ходят все бойцы по очереди."""
    ctx = make_ctx(make_state(), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    assert run(ctx, "start_combat", encounter_id="risen-bones").error is None
    ctx.acting = ALL_FIGHTERS
    return ctx


def variant(demo_adventure: Adventure, edit: Callable[[dict[str, Any]], None]) -> Adventure:
    """Копия `demo-crypt` с правкой данных: приключение неизменяемо, меняем его дамп."""
    data = demo_adventure.model_dump(mode="json")
    edit(data)
    return Adventure.model_validate(data)


# --- набор инструментов и сборка реестра ---

BASE_COMBAT = {
    "start_combat",
    "attack",
    "cast_spell",
    "move",
    "combat_action",
    "monster_action",
    "end_scene",
}
BASE_NARRATIVE = {"private_message", "chronicle", "heal", "damage", "apply_effect"}
BASE_OTHER = {
    "get_location",
    "get_npc",
    "get_item",
    "search_lore",
    "get_world",
    "get_deity",
    "get_faction",
    "get_relations",
    "move_party",
    "reveal",
    "give_item",
    "take_item",
    "set_flag",
    "change_attitude",
    "advance_time",
    "short_rest",
    "journal_add",
    "request_check",
    "request_save",
    "contest",
}


def names(registry: ToolRegistry) -> set[str]:
    return {spec.name for spec in registry.specs()}


def test_the_toolsets_have_every_combat_and_narrative_tool():
    assert {spec.name for spec in COMBAT_TOOLS} == BASE_COMBAT
    assert {spec.name for spec in NARRATIVE_TOOLS} == BASE_NARRATIVE


def test_build_registry_collects_all_base_tools_and_the_tools_of_the_enabled_modules():
    full = build_registry(build_ruleset(BOTH_MODULES))

    assert names(full) == BASE_OTHER | BASE_COMBAT | BASE_NARRATIVE | {
        "grant_inspiration",
        "start_camp",
    }


def test_without_the_campfire_module_there_is_no_start_camp():
    registry = build_registry(build_ruleset(RulesRef(modules=["bg3_flavor"])))

    assert "start_camp" not in names(registry)
    assert "grant_inspiration" in names(registry)


def test_without_the_bg3_flavor_module_there_is_no_grant_inspiration():
    registry = build_registry(build_ruleset(RulesRef(modules=["campfire"])))

    assert "grant_inspiration" not in names(registry)
    assert "start_camp" in names(registry)


def test_without_modules_only_the_base_tools_remain():
    registry = build_registry(build_ruleset(RulesRef()))

    assert names(registry) == BASE_OTHER | BASE_COMBAT | BASE_NARRATIVE


def test_module_tools_come_after_the_base_ones_and_an_unknown_tool_is_an_error_for_the_model(
    tools, make_ctx
):
    assert [spec.name for spec in tools.specs()][-2:] == ["grant_inspiration", "start_camp"]

    outcome = tools.call(make_ctx(), "summon_dragon", {})

    assert "Неизвестный инструмент" in outcome.error


def test_a_ruleset_tool_with_a_taken_name_is_a_programming_error():
    attack = next(spec for spec in COMBAT_TOOLS if spec.name == "attack")

    with pytest.raises(ValueError, match="уже зарегистрирован"):
        build_registry(RuleSet(tools=(attack,)))


def all_specs(tools: ToolRegistry) -> list[ToolSpec]:
    return tools.specs()


def refs_of(node: Any) -> set[str]:
    """Все ссылки `$ref` схемы."""
    if isinstance(node, dict):
        found = {node["$ref"]} if "$ref" in node else set()
        return found.union(*(refs_of(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(refs_of(value) for value in node))
    return set()


def test_every_tool_has_a_russian_description_and_a_serializable_schema_with_known_definitions(
    tools,
):
    for spec in all_specs(tools):
        assert any("а" <= ch.casefold() <= "я" for ch in spec.description), spec.name
        schema = spec.json_schema()
        assert json.loads(json.dumps(schema)) == schema, spec.name
        assert schema["additionalProperties"] is False, spec.name
        defs = schema.get("$defs", {})
        # Каждая ссылка схемы ведёт в её же `$defs`: неизвестных типов нет.
        assert refs_of(schema) <= {f"#/$defs/{name}" for name in defs}, spec.name


@pytest.mark.parametrize(
    ("tool", "forbidden"),
    [
        ("attack", {"crit", "advantage", "dc", "damage", "bonus", "natural"}),
        ("cast_spell", {"crit", "advantage", "dc", "damage", "save_dc"}),
        ("monster_action", {"crit", "advantage", "dc", "damage", "attack_bonus"}),
        ("apply_effect", {"dc", "save", "source", "advantage", "crit"}),
        ("heal", {"amount", "hp", "total"}),
        ("damage", {"amount", "hp", "total", "crit"}),
        ("start_combat", {"dc", "hp", "initiative"}),
    ],
)
def test_the_schemas_give_the_model_no_crit_dc_advantage_or_ready_numbers(tools, tool, forbidden):
    spec = next(spec for spec in tools.specs() if spec.name == tool)

    assert not forbidden & set(spec.json_schema()["properties"])


def test_damage_type_and_condition_enumerations_match_the_srd_data(srd):
    assert set(get_args(DamageType)) == {
        "acid",
        "bludgeoning",
        "cold",
        "fire",
        "force",
        "lightning",
        "necrotic",
        "piercing",
        "poison",
        "psychic",
        "radiant",
        "slashing",
        "thunder",
    }
    assert set(get_args(ConditionId)) == set(srd.conditions)


def test_the_dice_argument_of_heal_and_damage_is_bounded_by_the_dice_module_limit(tools):
    for tool in ("heal", "damage"):
        schema = next(spec for spec in tools.specs() if spec.name == tool).json_schema()
        assert schema["properties"]["dice"]["maxLength"] == MODEL_DICE_MAX_CHARS


def test_module_tool_arguments_forbid_extra_fields(tools):
    for tool in ("grant_inspiration", "start_camp"):
        schema = next(spec for spec in tools.specs() if spec.name == tool).json_schema()
        assert schema["additionalProperties"] is False


# --- start_combat ---


def test_start_combat_sets_up_risen_bones_from_the_encounter(run, make_ctx, make_state, make_hero):
    two = make_state(make_hero(), make_hero(id=ALLY, name="Союзник"))
    ctx = make_ctx(two, rng=ScriptedRng(TWO_HEROES_INITIATIVE))

    outcome = run(ctx, "start_combat", encounter_id="risen-bones")

    assert outcome.error is None
    combat = ctx.state.combat
    assert combat.encounter_id == "risen-bones"
    assert set(combat.combatants) == {HERO, ALLY, *ALL_FIGHTERS[1:]}
    zones = {fighter_id: slot.zone_id for fighter_id, slot in combat.combatants.items()}
    assert zones == {
        HERO: "gate",
        ALLY: "gate",
        SKELETON_1: "graves",
        SKELETON_2: "graves",
        SKELETON_3: "graves",
        ZOMBIE_1: "lodge",
        ZOMBIE_2: "lodge",
    }
    assert [zone.id for zone in combat.zones] == ["gate", "graves", "lodge"]
    assert {(link.a, link.b, link.distance) for link in combat.links} == {
        ("gate", "graves", "near"),
        ("graves", "lodge", "near"),
    }
    assert combat.order == [HERO, ALLY, SKELETON_1, SKELETON_2, SKELETON_3, ZOMBIE_1, ZOMBIE_2]
    assert ctx.state.world.flags["encounter:risen-bones"] == "active"
    assert ctx.state.scene.type == SceneType.COMBAT
    assert SceneEvent(scene=SceneType.COMBAT) in ctx.events
    assert CombatEvent(phase="started") in ctx.events
    assert not leftover(ctx.rng)  # ровно по броску инициативы на каждого бойца


def test_the_tools_change_the_working_copy_in_place_so_the_server_sees_the_result(
    run, make_ctx, make_state
):
    working = make_state()
    ctx = make_ctx(working, rng=ScriptedRng(ONE_HERO_INITIATIVE))

    run(ctx, "start_combat", encounter_id="risen-bones")

    # Сервер держит ссылку на рабочую копию и коммитит её, а не подменённый объект.
    assert ctx.state is working
    assert working.combat is not None and working.scene.type == SceneType.COMBAT


def test_start_combat_tells_the_model_the_order_zones_and_who_goes_first(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(), rng=ScriptedRng(ONE_HERO_INITIATIVE))

    result = run(ctx, "start_combat", encounter_id="risen-bones").result

    assert result["encounter"] == "risen-bones"
    assert (result["current_actor"], result["round"]) == (HERO, 1)
    assert [entry["id"] for entry in result["order"]] == [
        HERO,
        SKELETON_1,
        SKELETON_2,
        SKELETON_3,
        ZOMBIE_1,
        ZOMBIE_2,
    ]
    assert result["order"][1] == {
        "id": SKELETON_1,
        "name": "Скелет 1",
        "side": "enemies",
        "initiative": 5,
        "zone": "graves",
        "hp": 13,
        "ac": 14,
    }
    assert {zone["id"] for zone in result["zones"]} == {"gate", "graves", "lodge"}
    assert not any(line.get("roll") == "Инициатива" for line in result["events"])


def test_the_encounter_wins_over_the_monsters_list_when_both_are_given(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), rng=ScriptedRng(ONE_HERO_INITIATIVE))

    outcome = run(
        ctx,
        "start_combat",
        encounter_id="risen-bones",
        monsters=[{"monster_id": "ogre", "count": 1, "zone": "gate"}],
    )

    assert outcome.error is None
    assert "ogre-1" not in ctx.state.combat.combatants
    assert SKELETON_1 in ctx.state.combat.combatants
    assert "monsters" in outcome.result["ignored"]


def test_start_combat_with_a_custom_monster_list_uses_the_location_zones(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), rng=ScriptedRng([20, 3, 4]))

    outcome = run(
        ctx,
        "start_combat",
        monsters=[{"monster_id": "zombie", "count": 2, "zone": "graves"}],
        pc_zone="lodge",
    )

    assert outcome.error is None
    combat = ctx.state.combat
    assert combat.encounter_id is None
    assert [(fighter_id, slot.zone_id) for fighter_id, slot in combat.combatants.items()] == [
        (HERO, "lodge"),
        (ZOMBIE_1, "graves"),
        (ZOMBIE_2, "graves"),
    ]
    assert not any(flag.startswith("encounter:") for flag in ctx.state.world.flags)


def test_custom_monsters_default_to_the_zone_of_the_party(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), rng=ScriptedRng([20, 3]))

    run(ctx, "start_combat", monsters=[{"monster_id": "zombie"}])

    assert fighter(ctx, ZOMBIE_1).zone_id == fighter(ctx, HERO).zone_id == "gate"


def test_the_pc_zone_can_be_chosen_for_an_encounter_too(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), rng=ScriptedRng(ONE_HERO_INITIATIVE))

    run(ctx, "start_combat", encounter_id="risen-bones", pc_zone="lodge")

    assert fighter(ctx, HERO).zone_id == "lodge"


@pytest.mark.parametrize(
    ("args", "part"),
    [
        ({}, "encounter_id или monsters"),
        ({"encounter_id": "no-such-fight"}, "no-such-fight"),
        ({"encounter_id": "fabian-fight"}, "crypt"),  # бой в склепе, а отряд на кладбище
        ({"monsters": [{"monster_id": "dragon"}]}, "dragon"),
        ({"monsters": [{"monster_id": "zombie", "zone": "attic"}]}, "attic"),
        ({"monsters": [{"monster_id": "zombie"}], "pc_zone": "attic"}, "attic"),
        ({"encounter_id": "risen-bones", "pc_zone": "attic"}, "attic"),
        ({"monsters": []}, "encounter_id или monsters"),
    ],
)
def test_start_combat_refuses_bad_arguments_without_touching_the_state_or_the_dice(
    run, make_ctx, make_state, args, part
):
    ctx = make_ctx(make_state())

    refused(run, ctx, "start_combat", part, **args)


@pytest.mark.parametrize(
    "monsters",
    [
        [{"monster_id": "zombie", "count": 0}],
        [{"monster_id": "zombie", "count": 21}],
        [{"monster_id": "zombie"}] * 9,  # слишком много групп
        [{"monster_id": "zombie", "crit": True}],
        [{"monster_id": "zombie", "count": "много"}],
    ],
)
def test_start_combat_bounds_the_monster_groups_in_the_schema(run, make_ctx, make_state, monsters):
    ctx = make_ctx(make_state())

    refused(run, ctx, "start_combat", "monsters", monsters=monsters)


def test_start_combat_caps_the_total_number_of_monsters_like_the_validator_does(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state())
    groups = [{"monster_id": "zombie", "count": 10}, {"monster_id": "skeleton", "count": 11}]

    refused(run, ctx, "start_combat", "монстров", monsters=groups)
    assert MAX_MONSTERS == MAX_MONSTERS_PER_ENCOUNTER == 20


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"monsters": [{"monster_id": "zombie"}]}, id="monsters"),
        pytest.param({"monsters": [{"monster_id": "zombie"}], "pc_zone": "graves"}, id="pc-zone"),
        pytest.param({"encounter_id": "risen-bones"}, id="encounter"),
    ],
)
def test_start_combat_in_a_location_without_zones_is_an_error_for_the_model(
    run, make_ctx, make_state, demo_adventure, args
):
    # Валидатор такую локацию не пропускает; здесь защита в глубину: вместо `IndexError` движка
    # (ход мастера упал бы) модель получает ошибку, а состояние и кубы остаются нетронутыми.
    adventure = variant(demo_adventure, lambda data: data["locations"]["cemetery"].update(zones=[]))
    ctx = make_ctx(make_state(), adventure=adventure)

    refused(run, ctx, "start_combat", "нет зон", **args)


def test_start_combat_of_an_encounter_without_monsters_is_an_error_for_the_model(
    run, make_ctx, make_state, demo_adventure
):
    adventure = variant(
        demo_adventure, lambda data: data["encounters"]["risen-bones"].update(monsters=[])
    )
    ctx = make_ctx(make_state(), adventure=adventure)

    refused(run, ctx, "start_combat", "противников", encounter_id="risen-bones")


def test_a_battle_of_the_maximum_size_still_starts(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), rng=ScriptedRng([20] + [1] * 20))

    outcome = run(ctx, "start_combat", monsters=[{"monster_id": "zombie", "count": 20}])

    assert outcome.error is None and len(ctx.state.combat.combatants) == 21


def test_start_combat_cannot_start_a_second_battle_or_restart_a_finished_one(
    run, make_ctx, make_state, started
):
    refused(run, started, "start_combat", "уже идёт", encounter_id="risen-bones")

    done = make_ctx(make_state())
    done.state.world.flags["encounter:risen-bones"] = "done"
    refused(run, done, "start_combat", "завершён", encounter_id="risen-bones")


def test_a_battle_can_start_in_the_middle_of_a_camp_and_cancels_it(run, make_ctx, make_state):
    state = make_state()
    state.scene.type = SceneType.CAMP
    ctx = make_ctx(state, rng=ScriptedRng(ONE_HERO_INITIATIVE))

    outcome = run(ctx, "start_combat", encounter_id="risen-bones")

    assert outcome.error is None
    assert ctx.state.scene.type == SceneType.COMBAT


def test_a_statblock_of_the_adventure_is_used_in_the_battle_and_beats_the_srd_one(
    run, make_ctx, make_state, demo_adventure, srd
):
    bone_rat = (
        srd.monsters["skeleton"]
        .model_copy(update={"id": "bone-rat", "name": "Костяная крыса", "hp": 5})
        .model_dump(mode="json")
    )
    tough_zombie = srd.monsters["zombie"].model_copy(update={"hp": 99}).model_dump(mode="json")

    def edit(data: dict[str, Any]) -> None:
        data["monsters"] = {"bone-rat": bone_rat, "zombie": tough_zombie}

    adventure = variant(demo_adventure, edit)
    ctx = make_ctx(make_state(), adventure=adventure, rng=ScriptedRng([20, 1, 1]))
    outcome = run(
        ctx,
        "start_combat",
        monsters=[{"monster_id": "bone-rat", "zone": "graves"}, {"monster_id": "zombie"}],
    )

    assert outcome.error is None
    assert (fighter(ctx, "bone-rat-1").hp, fighter(ctx, "zombie-1").hp) == (5, 99)
    # Дальше бой идёт на данных приключения: кастомный монстр действует, иммунитеты видны.
    ctx.acting = (HERO, "bone-rat-1")
    result = run_monster(run, ctx, "bone-rat-1", "shortbow", [14, 4])
    assert result.error is None and result.result["current_actor"] == "bone-rat-1"


def run_monster(run, ctx, monster_id, action_id, rolls, targets=(HERO,)) -> ToolOutcome:
    dice(ctx, *rolls)
    return run(
        ctx, "monster_action", monster_id=monster_id, action_id=action_id, target_ids=list(targets)
    )


# --- статблок, который принял валидатор, не роняет ход мастера ---
# Бой читает из статблока приключения кости, бонусы, типы урона, состояния и числа. Данные, которые
# валидатор пропустил, не должны давать исключения движка: это ход мастера в `dm_failed`, а
# «Повторить» снова платит за вызов модели. Каждый вариант статблока либо отклонён валидатором, либо
# каждое его действие даёт результат или ошибку для модели.

RAT = "crypt-rat"
RAT_1 = "crypt-rat-1"
# Запись с русской буквой «к» вместо латинской d; закодирована кодом, чтобы не путать буквы.
CYRILLIC_DICE = "2\u043a6+1"


def rat_actions(index: int, **changes: Any) -> list[dict[str, Any]]:
    """Действия крысы, где у действия с номером `index` изменены поля `changes`."""
    actions = custom_monster_data()["actions"]
    actions[index] = actions[index] | changes
    return actions


def rat_actions_without(index: int, *names: str) -> list[dict[str, Any]]:
    actions = custom_monster_data()["actions"]
    for name in names:
        del actions[index][name]
    return actions


def rat_scores(**changes: int) -> dict[str, int]:
    return custom_monster_data()["scores"] | changes


GIGANTIC = 10**5000  # str() такого числа бросает ValueError: предел Python 4300 цифр

RAT_VARIANTS = [
    pytest.param({}, id="clean"),
    # удар (bite): кости, обязательные поля, типы, состояния
    pytest.param({"actions": rat_actions(0, damage=CYRILLIC_DICE)}, id="bite-damage-not-dice"),
    pytest.param({"actions": rat_actions(0, damage="1000d6")}, id="bite-damage-too-many-dice"),
    pytest.param({"actions": rat_actions(0, damage="")}, id="bite-damage-empty"),
    pytest.param({"actions": rat_actions(0, damage="5")}, id="bite-flat-damage"),
    pytest.param({"actions": rat_actions_without(0, "damage")}, id="bite-no-damage"),
    pytest.param({"actions": rat_actions_without(0, "attack_bonus")}, id="bite-no-bonus"),
    pytest.param({"actions": rat_actions_without(0, "damage_type")}, id="bite-no-damage-type"),
    pytest.param({"actions": rat_actions(0, damage_type="laser")}, id="bite-unknown-damage-type"),
    pytest.param(
        {"actions": rat_actions(0, secondary_damage="лол", secondary_damage_type="necrotic")},
        id="bite-secondary-not-dice",
    ),
    pytest.param(
        {"actions": rat_actions(0, secondary_damage="1d4", secondary_damage_type="necrotic")},
        id="bite-secondary-damage",
    ),
    pytest.param({"actions": rat_actions(0, condition="doomed")}, id="bite-unknown-condition"),
    pytest.param({"actions": rat_actions(0, condition="prone")}, id="bite-condition"),
    pytest.param(
        {"actions": rat_actions(0, condition="prone", save="str", dc=11)}, id="bite-condition-save"
    ),
    pytest.param(
        {"actions": rat_actions(0, condition="prone", save="str")}, id="bite-save-without-dc"
    ),
    pytest.param({"actions": rat_actions(0, range="far", reach=True)}, id="bite-far-reach"),
    # особые действия: спасбросок, мультиатака
    pytest.param({"actions": rat_actions_without(2, "save")}, id="screech-no-save"),
    pytest.param({"actions": rat_actions_without(2, "dc")}, id="screech-no-dc"),
    pytest.param(
        {"actions": rat_actions(2, damage="лол", damage_type="fire")}, id="screech-bad-dice"
    ),
    pytest.param({"actions": rat_actions(2, damage="1d6")}, id="screech-damage-no-type"),
    pytest.param(
        {"actions": rat_actions(2, damage="1d6", damage_type="fire", half_on_save=True)},
        id="screech-damage",
    ),
    pytest.param({"actions": rat_actions(2, condition="doomed")}, id="screech-unknown-condition"),
    pytest.param({"actions": rat_actions(2, targets=8)}, id="screech-8-targets"),
    pytest.param({"actions": rat_actions(2, targets=GIGANTIC)}, id="screech-gigantic-targets"),
    pytest.param({"actions": rat_actions(1, multiattack=[])}, id="multiattack-empty"),
    pytest.param({"actions": rat_actions(1, multiattack=["screech"])}, id="multiattack-of-a-save"),
    pytest.param({"actions": rat_actions(1, multiattack=["multiattack"])}, id="multiattack-itself"),
    # числа и характеристики
    pytest.param({"hp_dice": "junk"}, id="hit-dice-junk"),
    pytest.param({"ac": 0}, id="ac-0"),
    pytest.param({"ac": 30}, id="ac-30"),
    pytest.param({"ac": GIGANTIC}, id="ac-gigantic"),
    pytest.param({"hp": GIGANTIC}, id="hp-gigantic"),
    pytest.param({"scores": rat_scores(dex=GIGANTIC)}, id="dex-gigantic"),
    pytest.param({"scores": rat_scores(str=-GIGANTIC)}, id="str-gigantic-negative"),
    pytest.param({"scores": rat_scores(dex=30, str=1)}, id="extreme-scores"),
    pytest.param({"saves": {"str": GIGANTIC, "dex": GIGANTIC}}, id="saves-gigantic"),
    pytest.param({"saves": {"str": 20, "dex": -5}}, id="saves-on-the-limits"),
    pytest.param({"skills": {"athletics": GIGANTIC}}, id="skill-gigantic"),
    pytest.param({"skills": {"athletics": 20}}, id="skill-on-the-limit"),
    pytest.param({"actions": rat_actions(0, attack_bonus=GIGANTIC)}, id="attack-bonus-gigantic"),
    pytest.param({"actions": rat_actions(2, dc=GIGANTIC)}, id="dc-gigantic"),
    pytest.param({"actions": []}, id="no-actions"),
]


def rat_adventure(**overrides: Any) -> Adventure:
    """Фабричное приключение с кастомным статблоком крысы; бой с ней начинает инструмент."""
    data = adventure_data()
    data["monsters"] = {RAT: custom_monster_data(**overrides)}
    return Adventure.model_validate(data)


def rat_fight(make_ctx, make_state, run, adventure: Adventure, seed: int) -> ToolContext:
    """Бой героя с крысой, они вплотную друг к другу; в этом вызове ходят оба. Кубы — по `seed`."""
    ctx = make_ctx(make_state(), adventure=adventure, rng=SeededRng(seed))
    assert run(ctx, "start_combat", monsters=[{"monster_id": RAT}]).error is None
    combat = ctx.state.combat
    combat.combatants[HERO].engaged_with.append(RAT_1)
    combat.combatants[RAT_1].engaged_with.append(HERO)
    ctx.acting = (HERO, RAT_1)
    return ctx


def every_call_on_the_rat(adventure: Adventure) -> list[tuple[str, str, dict[str, Any]]]:
    """Все вызовы, где боевой инструмент читает статблок крысы: (чей ход, инструмент, аргументы).

    Действия самой крысы идут двумя инструментами (`monster_action` и `attack`), приёмы героя
    читают её защиту, спасброски и навыки, особое действие и Уклонение — только её саму."""
    rat_calls = [
        (RAT_1, tool, args)
        for action in adventure.monsters[RAT].actions
        for tool, args in (
            ("monster_action", {"monster_id": RAT_1, "action_id": action.id, "target_ids": [HERO]}),
            ("attack", {"actor_id": RAT_1, "target_id": HERO, "action_id": action.id}),
        )
    ]
    contest = {"a_id": HERO, "a_skill": "athletics", "b_id": RAT_1, "b_skill": "athletics"}
    hero_calls = [
        (HERO, "attack", {"actor_id": HERO, "target_id": RAT_1, "weapon": "mace"}),
        (HERO, "combat_action", {"actor_id": HERO, "action": "shove", "target_id": RAT_1}),
        (HERO, "combat_action", {"actor_id": HERO, "action": "grapple", "target_id": RAT_1}),
        (HERO, "contest", {**contest, "reason": "т"}),
        (RAT_1, "combat_action", {"actor_id": RAT_1, "action": "dodge"}),
    ]
    return rat_calls + hero_calls


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("overrides", RAT_VARIANTS)
def test_a_statblock_the_validator_accepts_never_breaks_the_master_turn(
    run, make_ctx, make_state, srd, overrides, seed
):
    adventure = rat_adventure(**overrides)
    if validate(adventure, srd).blocking:
        return  # данные отклонены на входе: до боя они не доходят

    for who, tool, args in every_call_on_the_rat(adventure):
        ctx = rat_fight(make_ctx, make_state, run, adventure, seed)
        combat = ctx.state.combat
        combat.turn_index = combat.order.index(who)

        outcome = run(ctx, tool, **args)  # исключение движка здесь роняет тест

        assert (outcome.result is None) != (outcome.error is None), (tool, args)


def test_the_clean_statblock_is_accepted_and_every_call_but_two_really_resolves(
    run, make_ctx, make_state, srd
):
    # Страховка от пустого теста выше: на чистом статблоке вызовы доходят до боя, а не упираются
    # в отказ очереди хода или дальности. Отказов ровно два: мультиатака и визг — не атаки.
    adventure = rat_adventure()
    assert validate(adventure, srd).blocking is False
    refused_calls = set()

    for who, tool, args in every_call_on_the_rat(adventure):
        ctx = rat_fight(make_ctx, make_state, run, adventure, 0)
        combat = ctx.state.combat
        combat.turn_index = combat.order.index(who)
        if run(ctx, tool, **args).error is not None:
            refused_calls.add((tool, args.get("action_id")))

    assert refused_calls == {("attack", "multiattack"), ("attack", "screech")}


# --- общая только инициатива начала боя ---


def test_a_monster_action_named_like_the_initiative_does_not_open_the_save_of_a_secret_hero(
    run, make_ctx, make_state, srd
):
    # Имя действия монстра приходит из данных приключения и входит в подпись спасброска цели. В
    # одном бою инициатива тайного героя общая, а его спасбросок против «Инициативы» крысы — только
    # его: открывает объявление `start_combat`, а не текст подписи.
    adventure = rat_adventure(actions=rat_actions(2, name="Инициатива"))
    assert not validate(adventure, srd).blocking
    ctx = make_ctx(make_state(), adventure=adventure, rng=SeededRng(0), secret_actors={HERO})

    assert run(ctx, "start_combat", monsters=[{"monster_id": RAT}]).error is None

    initiative = events_of(ctx, RollEvent)
    assert {(roll.label, roll.audience) for roll in initiative} == {("Инициатива", "all")}
    assert {roll.actor_id for roll in initiative} == {HERO, RAT_1}

    combat = ctx.state.combat
    combat.combatants[HERO].engaged_with.append(RAT_1)
    combat.combatants[RAT_1].engaged_with.append(HERO)
    combat.turn_index = combat.order.index(RAT_1)
    ctx.acting = (RAT_1,)
    before = len(ctx.events)

    outcome = run(ctx, "monster_action", monster_id=RAT_1, action_id="screech", target_ids=[HERO])

    assert outcome.error is None, outcome.error
    saves = [e for e in ctx.events[before:] if isinstance(e, RollEvent)]
    assert [(roll.actor_id, roll.label, roll.audience) for roll in saves] == [
        (HERO, "Спасбросок: Инициатива", HERO)
    ]


# --- вне боя ---


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("attack", {"actor_id": HERO, "target_id": SKELETON_1, "weapon": "mace"}),
        ("cast_spell", {"actor_id": HERO, "spell_id": "fire_bolt", "target_ids": [SKELETON_1]}),
        ("move", {"actor_id": HERO, "to_zone": "graves"}),
        ("combat_action", {"actor_id": HERO, "action": "dodge"}),
        (
            "monster_action",
            {"monster_id": SKELETON_1, "action_id": "shortsword", "target_ids": [HERO]},
        ),
    ],
)
def test_combat_tools_outside_a_battle_are_an_error_for_the_model(run, make_ctx, tool, args):
    ctx = make_ctx()

    outcome = refused(run, ctx, tool, "только в бою", **args)

    assert outcome.name == tool


# --- ход бойца и очередь acting ---


def test_the_hero_acts_and_the_next_monster_acts_in_one_set_of_calls(run, started):
    moved = run(started, "move", actor_id=HERO, to_zone="graves", engage=SKELETON_1)
    assert moved.error is None and fighter(started, HERO).zone_id == "graves"

    dice(started, 15, 6)  # к20 попадает (20 против КД 14), 1к6 булавы
    struck = run(started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace")

    assert struck.error is None, struck
    # 6+3 дробящего вдвое для уязвимого скелета: 18 убивают его (13 HP).
    assert fighter(started, SKELETON_1).hp == 0
    assert struck.result["events"][-1] == {
        "damage": 18,
        "type": "bludgeoning",
        "target": SKELETON_1,
        "hp_left": 0,
    }

    rng = dice(started, 14, 4)  # к20 (19 против КД 10), 1к6+3
    bite = run(
        started, "monster_action", monster_id=SKELETON_2, action_id="shortbow", target_ids=[HERO]
    )

    assert bite.error is None, bite
    assert actor_id(started) == SKELETON_2  # ход передан: убитый скелет пропущен
    assert bite.result["current_actor"] == SKELETON_2
    assert started.state.party[HERO].hp == 30 - 7
    assert SKELETON_1 not in started.state.combat.order
    assert CombatEvent(phase="turn", actor_id=SKELETON_2) in started.events
    assert not leftover(rng)


def test_acting_given_to_the_context_constructor_decides_who_acts(run, started, make_ctx):
    # Сервер создаёт контекст каждого вызова заново и передаёт очередь в конструктор.
    fresh = make_ctx(started.state, acting=[HERO])

    assert run(fresh, "combat_action", actor_id=HERO, action="dodge").error is None
    refused(
        run,
        fresh,
        "monster_action",
        "не действует",
        monster_id=SKELETON_1,
        action_id="shortbow",
        target_ids=[HERO],
    )


def test_a_monster_outside_acting_is_refused_without_a_trace(run, started):
    started.acting = (HERO,)

    refused(
        run,
        started,
        "monster_action",
        "не действует",
        monster_id=SKELETON_1,
        action_id="shortbow",
        target_ids=[HERO],
    )


def test_acting_may_not_skip_a_monster_that_goes_before(run, started):
    refused(
        run,
        started,
        "monster_action",
        "Скелет 1",
        monster_id=SKELETON_2,
        action_id="shortbow",
        target_ids=[HERO],
    )
    assert actor_id(started) == HERO


def test_a_character_outside_acting_cannot_act_even_on_their_own_turn(run, started):
    started.acting = (SKELETON_1,)

    refused(
        run, started, "attack", "не действует", actor_id=HERO, target_id=SKELETON_1, weapon="mace"
    )
    refused(run, started, "move", "не действует", actor_id=HERO, to_zone="graves")
    refused(run, started, "combat_action", "не действует", actor_id=HERO, action="dodge")


def test_without_acting_nobody_acts_on_a_turn_in_combat(run, started):
    started.acting = ()

    refused(run, started, "combat_action", "никто", actor_id=HERO, action="dodge")


def test_the_turn_cannot_be_advanced_past_a_character_who_can_act(
    run, make_ctx, make_state, make_hero
):
    state = make_state(make_hero(), make_hero(id=ALLY, name="Союзник"))
    ctx = make_ctx(state, rng=ScriptedRng(TWO_HEROES_INITIATIVE))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO, SKELETON_1)  # союзник сдаёт ход отдельным вызовом

    refused(
        run,
        ctx,
        "monster_action",
        "Союзник",
        monster_id=SKELETON_1,
        action_id="shortbow",
        target_ids=[HERO],
    )


def test_the_next_character_in_acting_takes_the_turn(run, make_ctx, make_state, make_hero):
    state = make_state(make_hero(), make_hero(id=ALLY, name="Союзник"))
    ctx = make_ctx(state, rng=ScriptedRng(TWO_HEROES_INITIATIVE))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO, ALLY)

    outcome = run(ctx, "combat_action", actor_id=ALLY, action="dodge")

    assert outcome.error is None
    assert actor_id(ctx) == ALLY and fighter(ctx, ALLY).dodging


def test_the_end_of_the_previous_turn_is_played_while_the_turn_is_passed_on(run, started):
    started.state.party[HERO].conditions.append(
        ActiveCondition(id="poisoned", rounds_left=5, source="", save=CON, dc=12)
    )

    rng = dice(started, 12, 14, 4)  # повторный спасбросок героя (успех), затем атака скелета
    outcome = run(
        started, "monster_action", monster_id=SKELETON_1, action_id="shortbow", target_ids=[HERO]
    )

    assert outcome.error is None
    assert started.state.party[HERO].conditions == []  # спасбросок снял отравление
    assert ConditionEvent(target_id=HERO, condition="poisoned", added=False) in started.events
    assert not leftover(rng)


def test_a_failed_action_after_passing_the_turn_leaves_the_state_as_it_was(run, started):
    before, before_events = started.state.model_copy(deep=True), list(started.events)

    outcome = run(
        started, "monster_action", monster_id=SKELETON_1, action_id="no-such", target_ids=[HERO]
    )

    assert outcome.result is None and "no-such" in outcome.error
    assert started.state == before and started.events == before_events
    assert actor_id(started) == HERO


def test_a_monsters_bonus_action_is_played_outside_its_turn_and_without_acting(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state(location_id="crypt"), rng=ScriptedRng([20, 1, 1, 1]))
    run(ctx, "start_combat", encounter_id="fabian-fight")
    ctx.acting = ()
    fanatic = "cultist_fanatic-1"
    assert actor_id(ctx) == HERO

    outcome = run(
        ctx, "monster_action", monster_id=fanatic, action_id="spellcasting", target_ids=[]
    )

    assert outcome.error is None
    assert actor_id(ctx) == HERO  # очередь не сдвинулась


def test_a_readied_attack_is_a_reaction_on_someone_elses_turn_for_a_character_outside_acting(
    run, started
):
    fighter(started, SKELETON_1).engaged_with.append(HERO)
    fighter(started, HERO).engaged_with.append(SKELETON_1)
    fighter(started, SKELETON_1).zone_id = "gate"
    started.state.combat.turn_index = started.state.combat.order.index(SKELETON_1)
    fighter(started, HERO).turn_flags["readied"] = True
    started.acting = (SKELETON_1,)

    dice(started, 15, 6)
    outcome = run(
        started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace", reaction=True
    )

    assert outcome.error is None, outcome
    assert actor_id(started) == SKELETON_1  # чужой ход не передвигался
    assert fighter(started, HERO).turn_flags == {"reaction": True}


def test_a_reaction_attack_without_a_readied_action_is_refused(run, started):
    started.state.combat.turn_index = started.state.combat.order.index(SKELETON_1)
    started.acting = (SKELETON_1,)

    refused(
        run,
        started,
        "attack",
        "подготов",
        actor_id=HERO,
        target_id=SKELETON_1,
        weapon="mace",
        reaction=True,
    )


# --- атака: вдохновение, тайный ход ---


def engaged_in_graves(ctx: ToolContext) -> None:
    run_move = ctx.state.combat
    run_move.combatants[HERO].zone_id = "graves"
    run_move.combatants[HERO].engaged_with.append(SKELETON_1)
    run_move.combatants[SKELETON_1].engaged_with.append(HERO)


def test_inspiration_gives_advantage_to_the_attack_and_is_spent_once(run, started):
    engaged_in_graves(started)
    started.state.party[HERO].inspiration = True
    started.inspired = {HERO}

    rng = dice(started, 3, 15, 6)  # два к20 (берётся лучший), урон
    outcome = run(started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace")

    assert outcome.error is None
    assert outcome.result["inspiration_spent"] is True
    assert not started.state.party[HERO].inspiration and HERO not in started.inspired
    assert not leftover(rng)


def test_a_refused_attack_does_not_spend_the_inspiration(run, started):
    started.state.party[HERO].inspiration = True
    started.inspired = {HERO}

    refused(
        run, started, "attack", "вовлечённого", actor_id=HERO, target_id=SKELETON_1, weapon="mace"
    )

    assert started.state.party[HERO].inspiration and HERO in started.inspired


def test_without_the_players_tick_the_inspiration_is_not_used(run, started):
    engaged_in_graves(started)
    started.state.party[HERO].inspiration = True

    rng = dice(started, 15, 6)
    outcome = run(started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace")

    assert outcome.error is None and "inspiration_spent" not in outcome.result
    assert started.state.party[HERO].inspiration
    assert not leftover(rng)


def test_the_rolls_of_a_secret_move_in_combat_are_visible_to_its_author_only(run, started):
    engaged_in_graves(started)
    started.secret_actors = {HERO}
    before = len(started.events)

    dice(started, 15, 6)
    run(started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace")

    rolls = [e for e in started.events[before:] if isinstance(e, RollEvent)]
    assert len(rolls) == 2 and all(roll.audience == HERO for roll in rolls)


def test_the_rolls_of_an_ordinary_move_in_combat_are_public(run, started):
    engaged_in_graves(started)
    before = len(started.events)

    dice(started, 15, 6)
    run(started, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace")

    rolls = [e for e in started.events[before:] if isinstance(e, RollEvent)]
    assert len(rolls) == 2 and all(roll.audience == "all" for roll in rolls)


def test_the_result_names_the_winner_so_the_model_knows_to_end_the_scene(run, started):
    for monster in (SKELETON_1, SKELETON_2, SKELETON_3, ZOMBIE_1):
        fighter(started, monster).hp = 0
    fighter(started, ZOMBIE_2).hp = 1
    fighter(started, ZOMBIE_2).zone_id = "gate"
    fighter(started, ZOMBIE_2).engaged_with.append(HERO)
    fighter(started, HERO).engaged_with.append(ZOMBIE_2)

    dice(started, 15, 6)
    outcome = run(started, "attack", actor_id=HERO, target_id=ZOMBIE_2, weapon="mace")

    assert outcome.result["winner"] == "party"
    assert started.state.combat is not None  # бой закрывает end_scene, а не удар


# --- заклинания и действия ---


def test_a_spell_is_cast_through_the_tool_with_the_wizards_own_slots(
    run, make_ctx, make_state, make_character
):
    wizard = make_character(
        id="wiz",
        name="Маг",
        class_id="wizard",
        background="sage",
        scores={INT: 16},
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 4, 2: 2},
        prepared_spells=["magic_missile"],
    )
    ctx = make_ctx(make_state(wizard), rng=ScriptedRng([20, 2, 1, 1, 1, 1]))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = ("wiz",)
    fighter(ctx, SKELETON_1).zone_id = "gate"

    dice(ctx, 4, 3, 2)  # три дротика по 1к4+1
    outcome = run(
        ctx, "cast_spell", actor_id="wiz", spell_id="magic_missile", target_ids=[SKELETON_1]
    )

    assert outcome.error is None, outcome
    assert ctx.state.party["wiz"].spell_slots == {1: 3, 2: 2}
    assert fighter(ctx, SKELETON_1).hp < 13


def test_an_unknown_spell_is_an_error_for_the_model_and_changes_nothing(run, started):
    refused(
        run,
        started,
        "cast_spell",
        "неизвестное",
        actor_id=HERO,
        spell_id="wish",
        target_ids=[SKELETON_1],
    )


def test_combat_action_dodge_marks_the_character_and_uses_the_action(run, started):
    outcome = run(started, "combat_action", actor_id=HERO, action="dodge")

    assert outcome.error is None
    assert fighter(started, HERO).dodging and fighter(started, HERO).turn_flags == {"action": True}


def test_a_potion_from_the_inventory_is_a_bonus_action_with_the_bg3_flavor_module(
    run, make_ctx, make_state, make_hero
):
    hero = make_hero(hp=10, inventory=[item("mace"), item("potion_of_healing", equipped=False)])
    ctx = make_ctx(make_state(hero), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    dice(ctx, 3, 2)
    outcome = run(
        ctx, "combat_action", actor_id=HERO, action="use_item", item_id="potion_of_healing"
    )

    assert outcome.error is None
    assert ctx.state.party[HERO].hp == 17
    assert fighter(ctx, HERO).turn_flags == {"bonus_action": True}  # ctx.rules дошли до движка
    assert [i.item_id for i in ctx.state.party[HERO].inventory] == ["mace"]


def test_the_same_potion_costs_the_action_without_the_bg3_flavor_module(
    run, make_ctx, make_state, make_hero
):
    hero = make_hero(hp=10, inventory=[item("mace"), item("potion_of_healing", equipped=False)])
    state = make_state(hero, rules=RulesRef(modules=["campfire"]))
    ctx = make_ctx(state, rng=ScriptedRng(ONE_HERO_INITIATIVE))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    dice(ctx, 3, 2)
    outcome = run(
        ctx, "combat_action", actor_id=HERO, action="use_item", item_id="potion_of_healing"
    )

    assert outcome.error is None
    assert fighter(ctx, HERO).turn_flags == {"action": True}


def test_a_potion_from_the_chest_is_drunk_in_battle_after_give_item(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(make_hero(hp=10)), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    ctx.state.world.item_holders["healing-potion"] = "location:cemetery"
    assert run(ctx, "give_item", item_id="healing-potion", character_id=HERO).error is None
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    dice(ctx, 3, 2)
    outcome = run(ctx, "combat_action", actor_id=HERO, action="use_item", item_id="healing-potion")

    assert outcome.error is None, outcome
    assert ctx.state.party[HERO].hp == 17
    assert fighter(ctx, HERO).turn_flags == {"bonus_action": True}
    assert ctx.state.world.item_holders["healing-potion"] == "gone"
    assert (
        ItemEvent(item_id="healing-potion", holder_from=f"character:{HERO}", holder_to="gone")
        in ctx.events
    )
    assert [i.item_id for i in ctx.state.party[HERO].inventory] == ["mace"]  # лист не менялся
    # Предмет ушёл: второй раз его выпить нельзя.
    again = run(ctx, "combat_action", actor_id=HERO, action="use_item", item_id="healing-potion")
    assert again.result is None and "gone" in again.error


def test_an_adventure_item_must_be_held_by_the_one_who_uses_it(
    run, make_ctx, make_state, make_hero
):
    ally = make_hero(id=ALLY, name="Союзник")
    ctx = make_ctx(make_state(make_hero(hp=10), ally), rng=ScriptedRng(TWO_HEROES_INITIATIVE))
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    # Зелье лежит в сундуке на кладбище: ни у кого в руках его нет.
    refused(
        run,
        ctx,
        "combat_action",
        "location:cemetery",
        actor_id=HERO,
        action="use_item",
        item_id="healing-potion",
    )
    # Зелье у союзника: герой выпить его не может.
    ctx.state.world.item_holders["healing-potion"] = f"character:{ALLY}"
    refused(
        run,
        ctx,
        "combat_action",
        f"character:{ALLY}",
        actor_id=HERO,
        action="use_item",
        item_id="healing-potion",
    )
    assert ctx.state.world.item_holders["healing-potion"] == f"character:{ALLY}"


def test_an_adventure_item_without_a_srd_base_has_no_mechanics_in_battle(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(make_hero()), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    ctx.state.world.item_holders["chest-key"] = f"character:{HERO}"
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    refused(
        run,
        ctx,
        "combat_action",
        "нельзя применить",
        actor_id=HERO,
        action="use_item",
        item_id="chest-key",
    )
    assert ctx.state.world.item_holders["chest-key"] == f"character:{HERO}"


def test_a_failed_potion_does_not_use_up_the_adventure_item(run, make_ctx, make_state, make_hero):
    ctx = make_ctx(make_state(make_hero(hp=10)), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    ctx.state.world.item_holders["healing-potion"] = f"character:{HERO}"
    run(ctx, "start_combat", encounter_id="risen-bones")
    ctx.acting = (HERO,)

    refused(
        run,
        ctx,
        "combat_action",
        "союзнику",
        actor_id=HERO,
        action="use_item",
        item_id="healing-potion",
        target_id=SKELETON_1,
    )

    assert ctx.state.world.item_holders["healing-potion"] == f"character:{HERO}"


# --- end_scene ---


def test_the_whole_fight_risen_bones_from_the_start_to_the_end_of_the_scene(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state(), rng=ScriptedRng(ONE_HERO_INITIATIVE))
    assert run(ctx, "start_combat", encounter_id="risen-bones").error is None
    ctx.acting = ALL_FIGHTERS

    assert run(ctx, "move", actor_id=HERO, to_zone="graves", engage=SKELETON_1).error is None
    dice(ctx, 15, 6)
    assert run(ctx, "attack", actor_id=HERO, target_id=SKELETON_1, weapon="mace").error is None
    assert run_monster(run, ctx, SKELETON_2, "shortbow", [14, 4]).error is None
    for monster in (SKELETON_2, SKELETON_3, ZOMBIE_1, ZOMBIE_2):  # остальных добил отряд
        fighter(ctx, monster).hp = 0

    dice(ctx)
    outcome = run(ctx, "end_scene", next="exploration", reason="Нежить повержена")

    assert outcome.error is None
    assert outcome.result["scene"] == "exploration"
    assert outcome.result["winner"] == "party"
    state = ctx.state
    assert state.combat is None and state.scene.type == SceneType.EXPLORATION
    assert state.world.flags["encounter:risen-bones"] == "done"
    assert state.location_id == "cemetery"
    assert CombatEvent(phase="ended", winner="party") in ctx.events
    assert SceneEvent(scene=SceneType.EXPLORATION) in ctx.events


def test_the_ended_battle_disappears_from_the_location_description(run, started):
    for monster in ALL_FIGHTERS[1:]:
        fighter(started, monster).hp = 0
    run(started, "end_scene", next="exploration", reason="конец")

    outcome = run(started, "get_location")

    assert outcome.result["encounter"] is None


def test_end_scene_can_close_a_battle_that_nobody_won(run, started):
    outcome = run(started, "end_scene", next="social", reason="Нежить слушает: начались переговоры")

    assert outcome.error is None
    assert (outcome.result["winner"], started.state.scene.type) == (None, SceneType.SOCIAL)
    assert started.state.combat is None


@pytest.mark.parametrize(
    "args",
    [
        {"next": "combat", "reason": "т"},
        {"next": "camp", "reason": "т"},
        {"next": "exploration", "reason": ""},
        {"next": "exploration"},
    ],
)
def test_end_scene_accepts_only_exploration_or_social_with_a_reason(run, make_ctx, args):
    ctx = make_ctx()
    before = ctx.state.model_copy(deep=True)

    outcome = run(ctx, "end_scene", **args)

    assert outcome.result is None and outcome.error
    assert ctx.state == before


def test_end_scene_into_the_same_scene_is_refused(run, make_ctx):
    refused(run, make_ctx(), "end_scene", "уже", next="exploration", reason="т")


def test_the_model_cannot_end_the_camp_it_is_the_job_of_the_timer_or_the_host(run, make_ctx):
    ctx = make_ctx()
    assert run(ctx, "start_camp", reason="Отряд устал").error is None

    refused(run, ctx, "end_scene", "Привал", next="exploration", reason="Пора вставать")

    assert ctx.state.scene.type == SceneType.CAMP


def test_the_server_ends_the_camp_with_the_engine_function_and_the_campfire_trigger_rests_the_party(
    run, make_ctx, make_state, make_hero, demo_adventure, srd
):
    ctx = make_ctx(make_state(make_hero(hp=4)))
    run(ctx, "start_camp", reason="Отряд устал")
    ctx.state.short_rests_since_long = 2

    after, events = end_scene(
        ctx.state, SceneType.EXPLORATION, ctx.rules, content=srd, adventure=demo_adventure
    )

    assert after.party[HERO].hp == 30
    assert (after.time.day, after.time.part, after.short_rests_since_long) == (2, "morning", 0)
    assert [type(e) for e in events] == [RestEvent, SceneEvent]


# --- рассказ: private_message, chronicle ---


def test_a_private_message_goes_to_one_character_only(run, make_ctx):
    ctx = make_ctx()

    outcome = run(ctx, "private_message", character_id=HERO, text="Ты слышишь шёпот в тумане.")

    assert outcome.error is None and outcome.result == {"to": "Герой"}
    assert ctx.events == [PrivateMessageEvent(audience=HERO, text="Ты слышишь шёпот в тумане.")]


@pytest.mark.parametrize(
    ("args", "part"),
    [
        ({"character_id": "ghost", "text": "т"}, "ghost"),
        ({"character_id": HERO, "text": ""}, "text"),
        ({"character_id": HERO, "text": "я" * 3000}, "text"),
        ({"character_id": "all", "text": "всем"}, "all"),
    ],
)
def test_a_private_message_needs_a_real_character_and_a_reasonable_text(run, make_ctx, args, part):
    ctx = make_ctx()

    outcome = run(ctx, "private_message", **args)

    assert outcome.result is None and part in outcome.error
    assert ctx.events == []


def test_the_chronicle_collects_entries_and_emits_an_event(run, make_ctx):
    ctx = make_ctx()

    first = run(ctx, "chronicle", entry="Отряд пришёл на кладбище.")
    second = run(ctx, "chronicle", entry="Скелеты встали из могил.")

    assert first.error is None and second.result == {"entries": 2}
    assert ctx.state.chronicle.entries == ["Отряд пришёл на кладбище.", "Скелеты встали из могил."]
    assert events_of(ctx, ChronicleEvent) == [
        ChronicleEvent(text="Отряд пришёл на кладбище."),
        ChronicleEvent(text="Скелеты встали из могил."),
    ]


@pytest.mark.parametrize("entry", ["", "   ", "я" * 2000])
def test_a_blank_or_huge_chronicle_entry_is_refused(run, make_ctx, entry):
    ctx = make_ctx()

    outcome = run(ctx, "chronicle", entry=entry)

    assert outcome.result is None and "entry" in outcome.error
    assert ctx.state.chronicle.entries == []


# --- рассказ: heal и damage — только из кости ---


def test_heal_rolls_the_dice_and_heals_the_character(run, make_ctx, make_state, make_hero):
    ctx = make_ctx(make_state(make_hero(hp=10)), rng=ScriptedRng([3, 2]))

    outcome = run(ctx, "heal", character_id=HERO, dice="2d4+2", reason="Целебный родник")

    assert outcome.error is None
    assert ctx.state.party[HERO].hp == 17
    assert outcome.result["healed"] == 7 and outcome.result["hp"] == 17
    assert events_of(ctx, HealEvent) == [HealEvent(target_id=HERO, amount=7, hp_after=17)]
    roll = events_of(ctx, RollEvent)[0]
    assert (roll.actor_id, roll.roll.expr, roll.roll.total) == (HERO, "2d4+2", 7)
    assert not leftover(ctx.rng)


def test_damage_rolls_the_dice_and_hurts_the_character(run, make_ctx, make_state, make_hero):
    ctx = make_ctx(make_state(make_hero()), rng=ScriptedRng([4, 5]))

    outcome = run(
        ctx, "damage", character_id=HERO, dice="2d6", damage_type="fire", reason="Ловушка"
    )

    assert outcome.error is None
    assert ctx.state.party[HERO].hp == 21
    assert outcome.result["damage"] == 9 and outcome.result["hp"] == 21
    assert events_of(ctx, DamageEvent) == [
        DamageEvent(target_id=HERO, amount=9, damage_type="fire", hp_after=21)
    ]
    assert not leftover(ctx.rng)


def test_damage_that_drops_the_character_to_zero_follows_the_engine_rules(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(make_hero(hp=5)), rng=ScriptedRng([6, 6]))

    run(ctx, "damage", character_id=HERO, dice="2d6", damage_type="bludgeoning", reason="Обвал")

    hero = ctx.state.party[HERO]
    assert hero.hp == 0 and [c.id for c in hero.conditions] == ["unconscious"]


@pytest.mark.parametrize("tool", ["heal", "damage"])
@pytest.mark.parametrize(
    "dice_text",
    [
        "5",
        "",
        "abc",
        "100d100",
        "21d4",
        "1d4+31",
        "1d4+1d4+1d4+1d4+1d4",
        "1d7",
        "-1d6",
        "d20" + "+1" * 30,
    ],
)
def test_wrong_or_huge_dice_are_an_error_and_the_hit_points_stay(
    run, make_ctx, make_state, make_hero, tool, dice_text
):
    ctx = make_ctx(make_state(make_hero(hp=10)))
    extra = {"damage_type": "fire"} if tool == "damage" else {}

    refused(run, ctx, tool, "", character_id=HERO, dice=dice_text, reason="т", **extra)

    assert ctx.state.party[HERO].hp == 10


@pytest.mark.parametrize("tool", ["heal", "damage"])
def test_the_dice_limit_error_names_the_problem(run, make_ctx, make_state, make_hero, tool):
    ctx = make_ctx(make_state(make_hero()))
    extra = {"damage_type": "fire"} if tool == "damage" else {}

    outcome = run(ctx, tool, character_id=HERO, dice="30d6", reason="т", **extra)

    assert "костей" in outcome.error


@pytest.mark.parametrize(
    ("tool", "args", "part"),
    [
        ("damage", {"dice": "1d6", "damage_type": "laser", "reason": "т"}, "damage_type"),
        ("damage", {"dice": "1d6", "damage_type": "fire", "reason": "т", "amount": 50}, "amount"),
        ("heal", {"dice": "1d6", "reason": "т", "amount": 50}, "amount"),
        ("heal", {"dice": "1d6", "reason": ""}, "reason"),
    ],
)
def test_heal_and_damage_have_no_way_around_the_dice(run, make_ctx, tool, args, part):
    ctx = make_ctx()

    refused(run, ctx, tool, part, character_id=HERO, **args)


@pytest.mark.parametrize("tool", ["heal", "damage"])
def test_heal_and_damage_are_not_for_battle(run, started, tool):
    extra = {"damage_type": "fire"} if tool == "damage" else {}

    refused(run, started, tool, "бою", character_id=HERO, dice="1d6", reason="т", **extra)


@pytest.mark.parametrize("tool", ["heal", "damage"])
def test_heal_and_damage_refuse_a_dead_or_missing_character_before_rolling(
    run, make_ctx, make_state, make_hero, tool
):
    dead = make_hero(hp=0, death_saves=DeathSaves(failures=3))
    ctx = make_ctx(make_state(dead))
    extra = {"damage_type": "fire"} if tool == "damage" else {}

    refused(run, ctx, tool, "мёртв", character_id=HERO, dice="1d6", reason="т", **extra)
    refused(run, ctx, tool, "ghost", character_id="ghost", dice="1d6", reason="т", **extra)


def test_the_rolls_of_a_trap_on_a_secret_actor_are_visible_to_him_only(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(make_hero()), rng=ScriptedRng([4, 5]), secret_actors={HERO})

    run(ctx, "damage", character_id=HERO, dice="2d6", damage_type="fire", reason="Ловушка")

    assert [roll.audience for roll in events_of(ctx, RollEvent)] == [HERO]


# --- рассказ: apply_effect ---


def test_apply_effect_puts_an_external_state_without_a_source_for_a_limited_time(run, started):
    rng = dice(started, 10)

    outcome = run(started, "apply_effect", target_id=HERO, condition="frightened", rounds=3)

    assert outcome.error is None
    assert started.state.party[HERO].conditions == [
        ActiveCondition(id="frightened", rounds_left=3, source="")
    ]
    assert events_of(started, ConditionEvent) == [
        ConditionEvent(target_id=HERO, condition="frightened", added=True)
    ]
    assert leftover(rng)  # кубы не бросались


@pytest.mark.parametrize("rounds", [1, 3, EXTERNAL_CONDITION_MAX_ROUNDS])
@pytest.mark.parametrize("condition", ["poisoned", "prone", "frightened"])
def test_a_term_in_rounds_outside_battle_is_refused_because_rounds_do_not_pass(
    run, make_ctx, condition, rounds
):
    # Срок тикает только в `end_turn`: вне боя «отравлен на 10 раундов» висел бы до конца
    # следующего боя. Модель накладывает состояние без срока и сама снимает его.
    refused(
        run,
        make_ctx(),
        "apply_effect",
        "раунды не идут",
        target_id=HERO,
        condition=condition,
        rounds=rounds,
    )


def test_an_open_ended_state_outside_battle_is_fine_and_a_term_is_fine_in_battle(
    run, make_ctx, started
):
    assert run(make_ctx(), "apply_effect", target_id=HERO, condition="poisoned").error is None
    assert (
        run(started, "apply_effect", target_id=HERO, condition="poisoned", rounds=5).error is None
    )


def test_apply_effect_without_rounds_is_open_ended_and_can_be_removed_later(run, make_ctx):
    ctx = make_ctx()
    run(ctx, "apply_effect", target_id=HERO, condition="prone")
    assert ctx.state.party[HERO].conditions[0].rounds_left is None

    outcome = run(ctx, "apply_effect", target_id=HERO, condition="prone", add=False)

    assert outcome.error is None and ctx.state.party[HERO].conditions == []
    assert events_of(ctx, ConditionEvent)[-1] == ConditionEvent(
        target_id=HERO, condition="prone", added=False
    )


def test_apply_effect_works_on_monsters_in_battle_and_respects_their_immunities(run, started):
    assert run(started, "apply_effect", target_id=SKELETON_1, condition="prone").error is None
    assert [c.id for c in fighter(started, SKELETON_1).conditions] == ["prone"]

    refused(
        run, started, "apply_effect", "невосприимчив", target_id=SKELETON_1, condition="poisoned"
    )


def test_apply_effect_can_release_a_character_held_by_a_monster_in_battle(run, started):
    started.state.party[HERO].conditions.append(ActiveCondition(id="grappled", source=ZOMBIE_1))

    outcome = run(started, "apply_effect", target_id=HERO, condition="grappled", add=False)

    assert outcome.error is None and started.state.party[HERO].conditions == []


def test_apply_effect_cannot_remove_the_records_the_engine_keeps(
    run, make_ctx, make_state, make_hero
):
    down = make_hero(hp=0, conditions=[ActiveCondition(id="unconscious", source="zero_hp")])
    ctx = make_ctx(make_state(down))

    refused(run, ctx, "apply_effect", "служебн", target_id=HERO, condition="unconscious", add=False)


@pytest.mark.parametrize(
    ("args", "part"),
    [
        ({"condition": "hunters_mark"}, "condition"),  # метка заклинания — не состояние SRD
        ({"condition": "helped"}, "condition"),
        ({"condition": "asleep"}, "condition"),
        ({"condition": "prone", "rounds": 0}, "rounds"),
        ({"condition": "prone", "rounds": EXTERNAL_CONDITION_MAX_ROUNDS + 1}, "rounds"),
        ({"condition": "prone", "rounds": 10**9}, "rounds"),
        ({"condition": "prone", "rounds": 2.5}, "rounds"),
        ({"condition": "prone", "dc": 99}, "dc"),
        ({"condition": "prone", "save": "con"}, "save"),
        ({"condition": "prone", "source": "zero_hp"}, "source"),
        ({"condition": "prone", "rounds_left": 5}, "rounds_left"),
        ({"condition": "prone", "add": False, "rounds": 3}, "rounds"),
    ],
)
def test_apply_effect_gives_the_model_no_way_to_set_a_dc_a_source_or_an_endless_term(
    run, make_ctx, args, part
):
    ctx = make_ctx()

    refused(run, ctx, "apply_effect", part, target_id=HERO, **args)


def test_apply_effect_needs_a_real_target(run, make_ctx):
    refused(run, make_ctx(), "apply_effect", "ghost", target_id="ghost", condition="prone")


def test_apply_effect_on_a_monster_outside_battle_is_refused(run, make_ctx):
    refused(run, make_ctx(), "apply_effect", SKELETON_1, target_id=SKELETON_1, condition="prone")


# --- модули: grant_inspiration и start_camp ---


def test_grant_inspiration_gives_inspiration_once_per_character_per_scene(run, make_ctx):
    ctx = make_ctx()

    first = run(ctx, "grant_inspiration", character_id=HERO, reason="Сыграл честь солдата")

    assert first.error is None
    assert ctx.state.party[HERO].inspiration is True
    assert ctx.state.scene.inspiration_granted == [HERO]
    assert events_of(ctx, InspirationEvent) == [
        InspirationEvent(character_id=HERO, granted=True, reason="Сыграл честь солдата")
    ]

    ctx.state.party[HERO].inspiration = False  # игрок уже потратил вдохновение
    refused(run, ctx, "grant_inspiration", "уже", character_id=HERO, reason="Ещё раз")
    assert ctx.state.party[HERO].inspiration is False


def test_inspiration_granted_in_one_scene_does_not_block_the_next_scene(run, make_ctx):
    ctx = make_ctx()
    run(ctx, "grant_inspiration", character_id=HERO, reason="т")
    ctx.state.party[HERO].inspiration = False
    assert run(ctx, "end_scene", next="social", reason="Разговор").error is None

    outcome = run(ctx, "grant_inspiration", character_id=HERO, reason="Новая сцена")

    assert outcome.error is None and ctx.state.scene.inspiration_granted == [HERO]


def test_grant_inspiration_does_not_pile_up_on_a_character_who_already_has_it(
    run, make_ctx, make_state, make_hero
):
    ctx = make_ctx(make_state(make_hero(inspiration=True)))

    refused(run, ctx, "grant_inspiration", "уже есть", character_id=HERO, reason="т")

    assert ctx.state.scene.inspiration_granted == []


def test_grant_inspiration_needs_a_character_and_a_reason(run, make_ctx):
    refused(run, make_ctx(), "grant_inspiration", "ghost", character_id="ghost", reason="т")
    refused(run, make_ctx(), "grant_inspiration", "reason", character_id=HERO, reason="")


def test_start_camp_sets_the_camp_scene_outside_battle(run, make_ctx):
    ctx = make_ctx()
    ctx.state.scene.inspiration_granted = [HERO]

    outcome = run(ctx, "start_camp", reason="Отряд устал после боя")

    assert outcome.error is None and outcome.result["scene"] == "camp"
    assert ctx.state.scene.type == SceneType.CAMP
    assert ctx.state.scene.rest == "long"  # привал мастера — всегда долгий
    assert ctx.state.scene.inspiration_granted == []
    assert ctx.events == [SceneEvent(scene=SceneType.CAMP)]
    assert (ctx.state.time.day, ctx.state.party[HERO].hp) == (1, 30)  # отдых — в конце привала


def test_start_camp_in_battle_is_an_error_and_the_battle_goes_on(run, started):
    refused(run, started, "start_camp", "бою", reason="Передохнём")

    assert started.state.combat is not None and started.state.scene.type == SceneType.COMBAT


def test_a_camp_cannot_be_started_twice(run, make_ctx):
    ctx = make_ctx()
    run(ctx, "start_camp", reason="т")

    refused(run, ctx, "start_camp", "уже идёт", reason="т")


def test_start_camp_is_not_stopped_by_the_limit_of_short_rests(run, make_ctx, make_state):
    # Предел коротких отдыхов «Специй BG3» снимает как раз долгий отдых привала.
    ctx = make_ctx(make_state(short_rests_since_long=2))

    assert run(ctx, "start_camp", reason="Отряд устал").error is None
    assert ctx.state.scene.rest == "long"


def test_start_camp_during_a_short_rest_is_refused(run, make_ctx):
    ctx = make_ctx()
    ctx.state.scene = Scene(type=SceneType.CAMP, rest="short")

    refused(run, ctx, "start_camp", "уже идёт", reason="т")


def refusal_of_begin_rest(ctx: ToolContext) -> str:
    """Текст отказа `begin_rest` для долгого привала в том же состоянии, что видит инструмент."""
    with pytest.raises(RestError) as refusal:
        begin_rest(ctx.state, "long", ctx.rules, content=ctx.content, adventure=ctx.adventure)
    return str(refusal.value)


@pytest.mark.parametrize("kind", [None, "short", "long"], ids=["no-kind", "short", "long"])
def test_start_camp_during_any_camp_gives_the_model_the_refusal_of_begin_rest(run, make_ctx, kind):
    # `start_camp` идёт через `begin_rest`: модель видит тот же текст, что игроки в ответе 409.
    ctx = make_ctx()
    ctx.state.scene = Scene(type=SceneType.CAMP, rest=kind)
    expected = refusal_of_begin_rest(ctx)

    outcome = refused(run, ctx, "start_camp", "уже идёт", reason="т")

    assert outcome.error == expected


def test_start_camp_in_battle_gives_the_model_the_refusal_of_begin_rest(run, started):
    expected = refusal_of_begin_rest(started)

    outcome = refused(run, started, "start_camp", "бою", reason="Передохнём")

    assert outcome.error == expected


def test_start_camp_needs_a_reason_and_forbids_extra_fields(run, make_ctx):
    refused(run, make_ctx(), "start_camp", "reason", reason="")
    refused(run, make_ctx(), "start_camp", "rest", reason="т", rest="long")


# --- модули правил подключают свои инструменты ---


def test_the_module_tools_are_part_of_the_ruleset_and_registered_by_the_module_table():
    assert {spec.name for spec in build_ruleset(RulesRef(modules=["campfire"])).tools} == {
        "start_camp"
    }
    assert {spec.name for spec in build_ruleset(RulesRef(modules=["bg3_flavor"])).tools} == {
        "grant_inspiration"
    }
    assert set(MODULES) == {"bg3_flavor", "campfire"}
