"""Реестр инструментов мастера как целое (спека §5.4, §5.7, §8.2–§8.3, план B4.5–B4.7).

Инструменты уже проверены по группам (`test_tools_lore`, `test_tools_world`, `test_tools_combat`).
Здесь проверяются свойства, которые должны держаться для всех инструментов сразу, пока модель
зовёт их через настоящий `ToolRegistry` из `build_registry`:
- схемы аргументов для провайдера: сериализуемы, не допускают лишних полей и не дают модели
  присылать неограниченные значения;
- Сл существует в схемах только как таблица 5/10/15/20/25/30;
- неверный вызов любого инструмента не оставляет следов: ни в состоянии, ни в событиях, ни в кубах;
- броски тайного автора скрыты от остальных во всех инструментах, которые бросают кубы;
- каждый инструмент вызывается по своей схеме, а его события и состояние сохраняются в JSON;
- ход мастера воспроизводится по seed.
"""

import json
import re
from typing import Any, NamedTuple, get_args

import pytest
from pydantic import TypeAdapter
from tool_calls import (
    ALLY,
    BOTH_MODULES,
    CONTEST,
    HERO,
    VALID_CALLS,
    WIZARD,
    ValidCall,
)

from dnd_engine.adventure.schema import Adventure
from dnd_engine.checks import VALID_DCS
from dnd_engine.events import Event, RollEvent
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState
from dnd_engine.tools import build_registry
from dnd_engine.tools.narrative import ConditionId
from dnd_engine.tools.registry import MAX_ERROR_CHARS, MAX_NAME_CHARS, ToolRegistry

ALL_TOOLS = [spec.name for spec in build_registry(build_ruleset(BOTH_MODULES)).specs()]
EVENTS = TypeAdapter(list[Event])

VALID_CHECK = {"character_id": HERO, "ability": "dex", "dc": 15, "reason": "т"}

# Таблица инструментов из спеки §8.3: движок может добавлять свои, но эти обязаны остаться.
SPEC_TOOLS = {
    "get_location",
    "get_npc",
    "get_item",
    "search_lore",
    "request_check",
    "request_save",
    "contest",
    "move_party",
    "reveal",
    "give_item",
    "take_item",
    "set_flag",
    "change_attitude",
    "start_combat",
    "attack",
    "cast_spell",
    "monster_action",
    "apply_effect",
    "heal",
    "damage",
    "private_message",
    "grant_inspiration",
    "start_camp",
    "chronicle",
    "end_scene",
}

# --- набор инструментов и схемы ---


def test_the_registry_has_every_tool_of_the_spec_table(tool_registry):
    names = {spec.name for spec in tool_registry.specs()}

    assert SPEC_TOOLS <= names


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_a_tool_name_is_a_plain_identifier_that_model_providers_accept(name):
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)


def objects_of(schema: Any) -> list[dict[str, Any]]:
    """Все объекты схемы с полями, в том числе вложенные в `$defs`, `anyOf` и `items`."""
    found = []
    if isinstance(schema, dict):
        if "properties" in schema:
            found.append(schema)
        for value in schema.values():
            found += objects_of(value)
    elif isinstance(schema, list):
        for value in schema:
            found += objects_of(value)
    return found


def spec_of(registry: ToolRegistry, name: str):
    return next(spec for spec in registry.specs() if spec.name == name)


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_every_object_in_every_schema_forbids_extra_fields(tool_registry, name):
    schema = spec_of(tool_registry, name).json_schema()

    assert json.loads(json.dumps(schema)) == schema
    for obj in objects_of(schema):  # вложенная модель (`MonsterSpec`) — тоже без лишних полей
        assert obj["additionalProperties"] is False, (name, obj.get("title"))


def unbounded_arguments(node: Any, path: str) -> list[str]:
    """Места схемы, где модель может прислать значение без предела.

    Текст — с `maxLength` (или перечислением), число — с обеими границами, список — с `maxItems`,
    словарь — с числом ключей и длиной ключа."""
    if isinstance(node, list):
        return [
            problem
            for index, item in enumerate(node)
            for problem in unbounded_arguments(item, f"{path}[{index}]")
        ]
    if not isinstance(node, dict):
        return []
    problems = []
    kind = node.get("type")
    if kind == "string" and not {"maxLength", "enum", "const"} & node.keys():
        problems.append(f"{path}: строка без maxLength")
    if kind in {"integer", "number"} and "enum" not in node:
        if not {"minimum", "maximum"} <= node.keys():
            problems.append(f"{path}: число без границ")
    if kind == "array" and "maxItems" not in node:
        problems.append(f"{path}: список без maxItems")
    if kind == "object" and "properties" not in node:  # словарь с ключами от модели
        if "maxProperties" not in node or "maxLength" not in node.get("propertyNames", {}):
            problems.append(f"{path}: словарь без предела числа и длины ключей")
    for key, value in node.items():
        problems += unbounded_arguments(value, f"{path}/{key}")
    return problems


def test_the_bound_check_catches_every_kind_of_unbounded_argument():
    schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "number": {"type": "integer", "minimum": 0},
            "list": {"type": "array", "items": {"type": "string", "maxLength": 5}},
            "dict": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
    }

    problems = unbounded_arguments(schema, "tool")

    assert [p.split(":")[0] for p in problems] == [
        "tool/properties/text",
        "tool/properties/number",
        "tool/properties/list",
        "tool/properties/dict",
        "tool/properties/dict/additionalProperties",
    ]


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_no_tool_accepts_an_unbounded_argument_from_the_model(tool_registry, name):
    assert unbounded_arguments(spec_of(tool_registry, name).json_schema(), name) == []


@pytest.mark.parametrize(
    ("tool", "args", "scene"),
    [
        (
            "monster_action",
            {"monster_id": "skeleton-1", "action_id": "shortbow"},
            {"party": (HERO,)},
        ),
        ("cast_spell", {"actor_id": WIZARD, "spell_id": "fire_bolt"}, {"first": WIZARD}),
    ],
)
def test_a_huge_target_id_does_not_blow_up_the_error_text(make_scene, call_tool, tool, args, scene):
    ctx = make_scene(fight=True, **scene)

    outcome = call_tool(ctx, tool, **args, target_ids=["я" * 5000])

    assert outcome.result is None and outcome.error
    assert "я" * 100 not in outcome.error  # огромный id не должен попасть в ошибку
    assert len(outcome.error) <= MAX_ERROR_CHARS


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("get_location", {"я" * 5000: 1}, id="extra-field"),
        pytest.param("short_rest", {"spend": {"я" * 5000: 1}}, id="key-of-a-dict"),
    ],
)
def test_a_huge_name_chosen_by_the_model_is_clipped_in_the_validation_error(
    make_scene, call_tool, tool, args
):
    # Имя лишнего поля или ключа словаря входит в путь ошибки схемы: отражать его целиком нельзя.
    ctx = make_scene()

    outcome = call_tool(ctx, tool, **args)

    assert outcome.result is None and outcome.error
    assert "я" * MAX_NAME_CHARS in outcome.error
    assert "я" * (MAX_NAME_CHARS + 1) not in outcome.error
    assert len(outcome.error) <= MAX_ERROR_CHARS


# --- Сл только из таблицы: ни одной другой Сл в схемах ---


def property_nodes(schema: Any) -> list[tuple[str, dict[str, Any]]]:
    """Все поля всех объектов схемы: (имя, описание поля)."""
    return [
        (name, field) for obj in objects_of(schema) for name, field in obj["properties"].items()
    ]


def looks_like_a_dc(field_name: str) -> bool:
    return "dc" in field_name or field_name in {"save", "difficulty", "threshold"}


def test_dc_exists_in_schemas_only_as_the_table_of_two_check_tools(tool_registry):
    dc_fields = {
        spec.name: [(n, f) for n, f in property_nodes(spec.json_schema()) if looks_like_a_dc(n)]
        for spec in tool_registry.specs()
    }

    assert {name for name, fields in dc_fields.items() if fields} == {
        "request_check",
        "request_save",
    }
    for name in ("request_check", "request_save"):
        [(field_name, field)] = dc_fields[name]
        assert field_name == "dc"
        assert sorted(field["enum"]) == sorted(VALID_DCS)


def test_the_dc_detector_notices_other_names_for_a_difficulty():
    schema = {"properties": {"save_dc": {}, "difficulty": {}, "save": {}, "reason": {}}}

    assert [n for n, _ in property_nodes(schema) if looks_like_a_dc(n)] == [
        "save_dc",
        "difficulty",
        "save",
    ]


@pytest.mark.parametrize("tool", ["request_check", "request_save"])
@pytest.mark.parametrize("dc", range(-5, 40))
def test_a_dc_is_accepted_exactly_when_it_is_in_the_table(make_scene, call_tool, tool, dc):
    ctx = make_scene()
    ctx.rng = ScriptedRng([12])
    before = ctx.state.model_dump()

    outcome = call_tool(ctx, tool, character_id=HERO, ability="dex", dc=dc, reason="т")

    if dc in VALID_DCS:
        assert outcome.error is None and outcome.result["dc"] == dc
    else:
        assert outcome.result is None and "dc" in outcome.error
        assert ctx.state.model_dump() == before and ctx.events == []
        assert ctx.rng.randint(1, 20) == 12  # кубы не бросались


@pytest.mark.parametrize("condition", get_args(ConditionId))
def test_an_effect_of_the_model_has_neither_a_dc_nor_a_save_nor_a_source(
    make_scene, call_tool, condition
):
    ctx = make_scene(fight=True)  # срок в раундах бывает только в бою
    ctx.rng = ScriptedRng([])  # у состояния без Сл бросать нечего: любой куб уронит тест

    outcome = call_tool(ctx, "apply_effect", target_id=HERO, condition=condition, rounds=3)

    assert outcome.error is None
    [applied] = [c for c in ctx.state.party[HERO].conditions if c.id == condition]
    assert (applied.dc, applied.save, applied.source) == (None, None, "")


# --- неверный вызов любого инструмента не оставляет следов ---


def wrong_arguments(schema: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """Вызовы, которые схема обязана отклонить: (поле, аргументы).

    Лишнее поле, пустой набор (если есть обязательные поля) и по одному полю с чужим по типу
    значением: словарь не подходит ни к строке, ни к числу, ни к списку, ни к перечислению."""
    calls: list[tuple[str | None, dict[str, Any]]] = [(None, {"no_such_field": 1})]
    if schema.get("required"):
        calls.append((None, {}))
    calls += [(field, {field: {"boom": [1]}}) for field in schema["properties"]]
    return calls


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_arguments_that_break_the_schema_leave_no_trace_for_any_tool(
    tool_registry, make_scene, name
):
    ctx = make_scene(secret=(ALLY,))
    ctx.rng = ScriptedRng([12])
    ctx.inspired = {HERO}
    before = ctx.state.model_dump()

    calls = wrong_arguments(spec_of(tool_registry, name).json_schema())

    for field, args in calls:
        outcome = tool_registry.call(ctx, name, args)

        assert outcome.result is None and outcome.error, (name, args)
        assert len(outcome.error) <= MAX_ERROR_CHARS
        if field is not None:
            assert field in outcome.error, (name, field, outcome.error)  # поле названо в ошибке
    assert ctx.state.model_dump() == before and ctx.events == []
    assert ctx.inspired == {HERO} and ctx.secret_actors == {ALLY}
    assert ctx.rng.randint(1, 20) == 12  # ни один вызов не бросал кубы


def test_every_tool_gets_at_least_one_wrong_call(tool_registry):
    for spec in tool_registry.specs():
        assert wrong_arguments(spec.json_schema()), spec.name


# --- тайные броски: все инструменты, которые бросают кубы ---


class RollingCall(NamedTuple):
    """Вызов инструмента, который бросает кубы от лица `author` (его ход может быть тайным)."""

    tool: str
    args: dict[str, Any]
    author: str
    fight: bool = False
    engaged: bool = False  # перед вызовом автор стоит в ближнем бою со скелетом


ROLLING_CALLS = {
    "проверка": RollingCall(
        "request_check", {"character_id": HERO, "ability": "dex", "dc": 15, "reason": "т"}, HERO
    ),
    "спасбросок": RollingCall(
        "request_save", {"character_id": HERO, "ability": "wis", "dc": 15, "reason": "т"}, HERO
    ),
    "состязание": RollingCall("contest", CONTEST, HERO),
    "урон ловушки": RollingCall(
        "damage",
        {"character_id": HERO, "dice": "2d6", "damage_type": "fire", "reason": "Ловушка"},
        HERO,
    ),
    "лечение": RollingCall(
        "heal", {"character_id": HERO, "dice": "2d4+2", "reason": "Ручей"}, HERO
    ),
    "удар оружием": RollingCall(
        "attack",
        {"actor_id": HERO, "target_id": "skeleton-1", "weapon": "mace"},
        HERO,
        fight=True,
        engaged=True,
    ),
    "заклинание-атака": RollingCall(
        "cast_spell",
        {"actor_id": WIZARD, "spell_id": "fire_bolt", "target_ids": ["skeleton-1"]},
        WIZARD,
        fight=True,
    ),
}

# Действие автора заставляет бросать других: толчок — спасбросок цели, отход из ближнего боя —
# атаку по возможности монстра. Сам автор не бросает, но это броски по его ходу.
ROLLS_BY_OTHERS = {
    "толчок": RollingCall(
        "combat_action",
        {"actor_id": HERO, "action": "shove", "target_id": "skeleton-1", "option": "push"},
        HERO,
        fight=True,
        engaged=True,
    ),
    "отход с атакой по возможности": RollingCall(
        "move", {"actor_id": HERO, "to_zone": "gate"}, HERO, fight=True, engaged=True
    ),
}


def rolls_of_the_call(make_scene, call_tool, call: RollingCall, secret: bool) -> list[RollEvent]:
    """Выполняет вызов от лица автора (его ход тайный или нет) и возвращает броски вызова."""
    ctx = make_scene(
        first=call.author,
        fight=call.fight,
        engaged=call.engaged,
        secret=(call.author,) if secret else (),
    )
    first_new = len(ctx.events)

    outcome = call_tool(ctx, call.tool, **call.args)

    assert outcome.error is None, outcome.error
    rolls = [e for e in ctx.events[first_new:] if isinstance(e, RollEvent)]
    assert rolls, "инструмент не бросил кубы: сценарий ничего не проверяет"
    return rolls


@pytest.mark.parametrize("secret", [False, True], ids=["обычный ход", "тайный ход"])
@pytest.mark.parametrize("call", ROLLING_CALLS.values(), ids=ROLLING_CALLS.keys())
def test_rolls_of_a_secret_author_are_hidden_from_the_others_in_every_rolling_tool(
    make_scene, call_tool, call, secret
):
    rolls = rolls_of_the_call(make_scene, call_tool, call, secret)

    assert {roll.audience for roll in rolls} == {call.author if secret else "all"}


# Тайность определяет автор вызова (спека §8.2 «броски по тайному ходу видны только его автору»),
# а не то, кто бросал: когда действие автора заставляет бросать других (спасбросок цели, атака по
# возможности), эти броски тоже закрыты, даже если сам автор не бросал.
@pytest.mark.parametrize(
    "secret",
    [False, True],
    ids=["обычный ход", "тайный ход"],
)
@pytest.mark.parametrize("call", ROLLS_BY_OTHERS.values(), ids=ROLLS_BY_OTHERS.keys())
def test_rolls_that_a_secret_action_makes_others_roll_are_hidden_too(
    make_scene, call_tool, call, secret
):
    rolls = rolls_of_the_call(make_scene, call_tool, call, secret)

    assert {roll.audience for roll in rolls} == {call.author if secret else "all"}


def audiences_of_rolls(events: list[Event]) -> dict[str, str]:
    """Аудитория каждого броска в событиях: {id бросавшего: аудитория}."""
    return {e.actor_id: e.audience for e in events if isinstance(e, RollEvent)}


def test_a_secret_move_in_the_turn_hides_only_the_initiative_of_the_secret_actor(
    make_scene, call_tool
):
    # Начало боя — вызов без автора: чужие броски инициативы тайный ход не закрывает.
    ctx = make_scene(secret=(HERO,))
    first_new = len(ctx.events)

    assert call_tool(ctx, "start_combat", encounter_id="risen-bones").error is None

    audiences = audiences_of_rolls(ctx.events[first_new:])
    assert audiences[HERO] == HERO
    assert set(audiences) >= {HERO, ALLY, WIZARD, "skeleton-1"}
    assert {a for who, a in audiences.items() if who != HERO} == {"all"}


def test_each_secret_actor_sees_only_their_own_initiative_and_the_rest_is_public(
    make_scene, call_tool
):
    ctx = make_scene(secret=(HERO, ALLY))
    first_new = len(ctx.events)

    assert call_tool(ctx, "start_combat", encounter_id="risen-bones").error is None

    audiences = audiences_of_rolls(ctx.events[first_new:])
    assert (audiences[HERO], audiences[ALLY]) == (HERO, ALLY)
    assert {a for who, a in audiences.items() if who not in {HERO, ALLY}} == {"all"}


def test_the_saving_throw_of_a_spell_target_is_hidden_with_the_secret_caster(make_scene, call_tool):
    # Удержание личности: бросает цель (спасбросок Мудрости), заклинатель сам кубов не бросает.
    def hold_person(secret: tuple[str, ...]) -> list[RollEvent]:
        ctx = make_scene(first=WIZARD, fight=True, secret=secret)
        wizard = ctx.state.party[WIZARD]
        wizard.prepared_spells.append("hold_person")
        wizard.spell_slots[2] = wizard.spell_slots_max[2] = 1
        first_new = len(ctx.events)

        outcome = call_tool(
            ctx,
            "cast_spell",
            actor_id=WIZARD,
            spell_id="hold_person",
            target_ids=["skeleton-1"],
            slot_level=2,
        )

        assert outcome.error is None, outcome.error
        rolls = [e for e in ctx.events[first_new:] if isinstance(e, RollEvent)]
        assert [roll.actor_id for roll in rolls] == ["skeleton-1"]  # заклинатель не бросал
        return rolls

    assert [roll.audience for roll in hold_person(secret=(WIZARD,))] == [WIZARD]
    assert [roll.audience for roll in hold_person(secret=())] == ["all"]
    assert [roll.audience for roll in hold_person(secret=(HERO,))] == ["all"]  # чужой тайный ход


def test_rolls_made_while_the_turn_is_passed_to_a_secret_author_stay_public(make_scene, call_tool):
    # Отряд: Лира ходит, Брон лежит на 0 HP, маг тайно творит заклинание. Чтобы маг мог
    # ходить, движок передаёт ход через лежащего Брона: тот бросает спасбросок от смерти. Это
    # бросок Брона по ходу боя, а не часть тайного действия мага.
    ctx = make_scene(first=ALLY, party=(ALLY, HERO, WIZARD), fight=True, secret=(WIZARD,))
    ctx.state.party[HERO].hp = 0
    ctx.rng = ScriptedRng([10, 15, 5])  # спасбросок от смерти, атака заклинанием, урон
    first_new = len(ctx.events)

    outcome = call_tool(
        ctx, "cast_spell", actor_id=WIZARD, spell_id="fire_bolt", target_ids=["skeleton-1"]
    )

    assert outcome.error is None, outcome.error
    rolls = [e for e in ctx.events[first_new:] if isinstance(e, RollEvent)]
    assert [(r.actor_id, r.label) for r in rolls][0] == (HERO, "Спасбросок от смерти")
    assert {r.actor_id: r.audience for r in rolls} == {HERO: "all", WIZARD: WIZARD}


def test_every_tool_that_rolls_dice_is_in_the_secret_rolls_table(tool_registry):
    # Инструменты без бросков и те, чьи броски движок делает за монстров, — осознанный список:
    # новый инструмент с бросками без строки в таблице выше остался бы без проверки тайны.
    without_author_rolls = {
        "get_location",
        "get_npc",
        "get_item",
        "search_lore",
        "move_party",
        "reveal",
        "give_item",
        "take_item",
        "set_flag",
        "change_attitude",
        "advance_time",
        "short_rest",
        "private_message",
        "chronicle",
        "apply_effect",
        "grant_inspiration",
        "start_camp",
        "end_scene",
        "monster_action",  # бросает монстр
    }
    names = {spec.name for spec in tool_registry.specs()}
    tested = {call.tool for call in (*ROLLING_CALLS.values(), *ROLLS_BY_OTHERS.values())}
    tested |= {"start_combat"}  # вызов без автора: отдельные тесты выше

    assert names - without_author_rolls == tested


def test_every_tool_of_the_secret_rolls_table_names_its_author_field(tool_registry):
    # Без объявленного поля автора реестр не узнает автора вызова, и тайность пропадёт.
    for call in (*ROLLING_CALLS.values(), *ROLLS_BY_OTHERS.values()):
        spec = spec_of(tool_registry, call.tool)
        author_fields = [name for name, value in call.args.items() if value == call.author]

        assert set(author_fields) & set(spec.author_fields), call.tool


# --- каждый инструмент вызывается по своей схеме, события и состояние переживают JSON ---


def test_the_table_of_valid_calls_has_a_call_for_every_registered_tool(tool_registry):
    assert {valid.tool for valid in VALID_CALLS} == {spec.name for spec in tool_registry.specs()}


@pytest.mark.parametrize(
    "valid",
    VALID_CALLS,
    ids=[f"{v.tool}-{'бой' if v.scene else 'исследование'}" for v in VALID_CALLS],
)
def test_each_tool_works_by_its_own_schema_and_leaves_json_that_can_be_stored(
    make_scene, call_tool, demo_adventure: Adventure, valid: ValidCall
):
    ctx = make_scene(**valid.scene)
    adventure_before = demo_adventure.model_dump_json()

    outcome = call_tool(ctx, valid.tool, **valid.args)

    assert outcome.error is None, outcome.error
    assert outcome.result is not None
    # Журнал хода хранит события, а база — состояние: оба должны читаться обратно без потерь.
    assert EVENTS.validate_json(EVENTS.dump_json(ctx.events)) == ctx.events
    assert GameState.model_validate_json(ctx.state.model_dump_json()) == ctx.state
    assert demo_adventure.model_dump_json() == adventure_before  # приключение не меняется в игре


# --- стыки инструментов: результат одного работает в следующем вызове ---


def test_inspiration_granted_in_one_call_gives_advantage_to_a_later_check_and_is_spent(
    make_scene, call_tool
):
    ctx = make_scene()
    ctx.state.party[HERO].inspiration = False
    granted = call_tool(ctx, "grant_inspiration", character_id=HERO, reason="Сыграл честь солдата")
    assert granted.error is None and ctx.state.party[HERO].inspiration is True

    ctx.inspired = {HERO}  # в следующем ходе игрок отметил «Вдохновение»
    ctx.rng = ScriptedRng([3, 17])
    check = call_tool(ctx, "request_check", **VALID_CHECK)

    assert (check.result["natural"], check.result["inspiration_spent"]) == (17, True)
    assert ctx.state.party[HERO].inspiration is False and ctx.inspired == set()
    ctx.rng = ScriptedRng([9])  # вдохновения больше нет: второй бросок идёт одним кубом
    assert call_tool(ctx, "request_check", **VALID_CHECK).result["natural"] == 9


def test_a_trap_knocks_a_hero_out_and_a_healing_brook_wakes_them_up(make_scene, call_tool):
    ctx = make_scene()  # у Брона 10 HP из 30
    ctx.rng = ScriptedRng([6, 6])

    trap = {"dice": "2d6", "damage_type": "bludgeoning", "reason": "Обвал"}
    call_tool(ctx, "damage", character_id=HERO, **trap)

    fallen = ctx.state.party[HERO]  # состояние после вызова — новые объекты, берём их заново
    assert fallen.hp == 0 and [c.id for c in fallen.conditions] == ["unconscious"]
    ctx.rng = ScriptedRng([3, 2])
    healed = call_tool(ctx, "heal", character_id=HERO, dice="2d4+2", reason="Целебный родник")
    assert healed.result["hp"] == 7 and ctx.state.party[HERO].conditions == []


def test_a_potion_in_the_closed_chest_is_out_of_reach_of_every_tool_until_the_chest_is_opened(
    make_scene, call_tool
):
    ctx = make_scene(fight=True)  # ключ у Брона, сундук закрыт, бой идёт
    before = ctx.state.model_dump()
    potion = {"item_id": "healing-potion", "character_id": HERO}
    drink = {"actor_id": HERO, "action": "use_item", "item_id": "healing-potion"}

    refused = [
        call_tool(ctx, "give_item", **potion),
        call_tool(ctx, "take_item", **potion, to="gone"),
        call_tool(ctx, "combat_action", **drink),
    ]

    assert all(outcome.result is None and outcome.error for outcome in refused)
    assert ctx.state.model_dump() == before  # отказ ничего не тронул
    assert call_tool(ctx, "reveal", ref="feature:chest").error is None
    assert call_tool(ctx, "give_item", **potion).error is None
    assert call_tool(ctx, "combat_action", **drink).error is None
    assert ctx.state.world.item_holders["healing-potion"] == "gone"  # выпито


# --- ход воспроизводится по seed ---

REPLAY_CALLS = [
    ("request_check", {"character_id": HERO, "ability": "wis", "dc": 10, "reason": "т"}),
    ("contest", CONTEST),
    ("request_save", {"character_id": ALLY, "ability": "dex", "dc": 15, "reason": "т"}),
    ("damage", {"character_id": ALLY, "dice": "2d6", "damage_type": "fire", "reason": "т"}),
    ("heal", {"character_id": HERO, "dice": "2d4+2", "reason": "т"}),
    ("short_rest", {"spend": {HERO: 1, ALLY: 1}}),
    ("start_combat", {"encounter_id": "risen-bones"}),
    ("move", {"actor_id": HERO, "to_zone": "graves", "engage": "skeleton-1"}),
    ("attack", {"actor_id": HERO, "target_id": "skeleton-1", "weapon": "mace"}),
    ("monster_action", {"monster_id": "skeleton-2", "action_id": "shortbow", "target_ids": [HERO]}),
    ("end_scene", {"next": "exploration", "reason": "т"}),
]


def replay(make_scene, tool_registry, seed: int) -> dict[str, Any]:
    """Тот же ход мастера на кубах с заданным seed: что вернули инструменты, события и состояние."""
    ctx = make_scene(party=(HERO, ALLY), secret=(ALLY,))
    ctx.rng = SeededRng(seed)
    ctx.state.party[HERO].inspiration = True
    ctx.inspired = {HERO}
    outcomes = []
    for tool, args in REPLAY_CALLS:
        outcomes.append(tool_registry.call(ctx, tool, args).model_dump())
        if tool == "start_combat":  # очередь называет сервер; здесь ходят все
            ctx.acting = tuple(ctx.state.combat.combatants)
    return {
        "outcomes": outcomes,
        "events": EVENTS.dump_python(ctx.events, mode="json"),
        "state": ctx.state.model_dump(mode="json"),
    }


def test_the_same_seed_replays_the_whole_master_turn_and_another_seed_changes_the_dice(
    make_scene, tool_registry
):
    first = replay(make_scene, tool_registry, seed=11)

    assert replay(make_scene, tool_registry, seed=11) == first
    assert replay(make_scene, tool_registry, seed=12) != first
    assert sum(outcome["error"] is None for outcome in first["outcomes"]) >= 8  # ход не пустой


# --- состязание с NPC: только из текущей локации отряда (решение D9, security-review B7, L1) ---

# Фабиан живёт в склепе, а `make_scene` ставит отряд на кладбище: «здесь» его нет.
HERO_VS_FABIAN = {
    "a_id": HERO,
    "a_skill": "deception",
    "b_id": "fabian",
    "b_skill": "insight",
    "reason": "т",
}


def test_a_contest_with_an_npc_from_another_location_is_refused_and_leaves_no_trace(
    make_scene, call_tool
):
    ctx = make_scene()
    ctx.rng = ScriptedRng([12])
    ctx.state.party[HERO].inspiration = True
    ctx.inspired = {HERO}
    before = ctx.state.model_dump()

    outcome = call_tool(ctx, "contest", **HERO_VS_FABIAN)

    assert outcome.error is not None
    assert "fabian" in outcome.error and "cemetery" in outcome.error  # кто и где сейчас отряд
    assert "текущей локации" in outcome.error
    assert ctx.state.model_dump() == before and ctx.events == []
    assert ctx.inspired == {HERO}  # вдохновение не потрачено
    assert ctx.rng.randint(1, 20) == 12  # кубы не бросались: очередь осталась нетронутой


def test_a_contest_with_an_npc_of_the_current_location_rolls_for_both_sides(make_scene, call_tool):
    ctx = make_scene()
    ctx.state.location_id = "crypt"  # родная локация Фабиана по данным приключения

    outcome = call_tool(ctx, "contest", **HERO_VS_FABIAN)

    assert outcome.error is None, outcome.error
    assert [e.actor_id for e in ctx.events if isinstance(e, RollEvent)] == [HERO, "fabian"]


def test_the_place_of_an_npc_is_where_the_world_put_him_and_not_only_his_home(
    make_scene, call_tool
):
    # Мир переопределяет положение NPC: его сюда привели.
    arrived = make_scene()
    arrived.state.world.npc_location["fabian"] = "cemetery"
    # Отряд в склепе, но Фабиан ушёл оттуда.
    left = make_scene()
    left.state.location_id = "crypt"
    left.state.world.npc_location["fabian"] = "village"

    assert call_tool(arrived, "contest", **HERO_VS_FABIAN).error is None
    outcome = call_tool(left, "contest", **HERO_VS_FABIAN)

    assert outcome.error is not None and "crypt" in outcome.error


def test_a_contest_with_a_hero_or_a_fighter_does_not_depend_on_the_location_of_npcs(
    make_scene, call_tool
):
    heroes = make_scene()
    fight = make_scene(fight=True)
    fighter = {**CONTEST, "b_id": "skeleton-1"}

    assert call_tool(heroes, "contest", **CONTEST).error is None  # герои отряда, склепа нет
    assert call_tool(fight, "contest", **fighter).error is None  # скелет из идущего боя
