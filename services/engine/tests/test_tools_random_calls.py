"""Случайные вызовы инструментов мастера: что бы ни прислала модель, ход не падает (спека §8.3).

Модель мастера — недоверенный источник аргументов (инвариант ж). Тест строит случайные вызовы по
JSON-схемам всех инструментов `build_registry`: чаще образцовые вызовы (`tool_calls.VALID_CALLS`)
с несколькими подмешанными случайными полями, иногда полностью случайные, а в полях бывает и
мусор. Серия вызовов идёт подряд на одном состоянии, как ход мастера. Каждый вызов обязан
закончиться результатом или ошибкой для модели; ошибка не меняет ни состояние, ни события, ни
вдохновение; после удачного вызова состояние остаётся целым и сохраняется в JSON. Случайность
только через `SeededRng`: любой сбой воспроизводится по номеру seed и шага.

Серия не должна вырождаться в одни отказы: каждый инструмент обязан хоть раз сработать. Новый
инструмент или образцовый вызов сдвигает всю случайную последовательность, и редкие удачи (ход
монстра, атака в ближнем бою) могут уйти из первых игр. Поэтому серия — не меньше `MIN_SEEDS`
игр, а дальше добирается новыми seed, пока не сработает каждый инструмент, но не дольше
`MAX_SEEDS`: сдвиг последовательности тест не роняет, а инструмент, который не срабатывает
никогда, роняет по-прежнему.
"""

import json
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import TypeAdapter
from tool_calls import ALLY, HERO, VALID_CALLS, WIZARD

from dnd_engine.adventure.schema import Adventure
from dnd_engine.combat.engine import can_act, current_actor
from dnd_engine.combat.model import Combatant
from dnd_engine.content import SrdContent
from dnd_engine.events import Event, RollEvent
from dnd_engine.rng import Rng, SeededRng
from dnd_engine.state import GameState, SceneType
from dnd_engine.tools.registry import ToolContext, ToolSpec

EVENTS = TypeAdapter(list[Event])
MIN_SEEDS = 80  # столько игр серия проходит всегда
MAX_SEEDS = 400  # предел добора: инструмент, не сработавший и здесь, — вырождение серии
CALLS_PER_SEED = 100
# Значения, которыми модель может ошибиться в любом поле: пустые, огромные, чужого типа.
JUNK: list[Any] = [None, "", " ", "я" * 300, -1, 0, 7, 10**9, 1.5, True, [], {}, ["pc-1"]]
DICE = ["1d6", "2d6", "2d4+2", "3d8+3", "d20", "5", "20d6", "1d6+1d4", "0d6", "100d100"]
WORDS = ["долг", "Фабиан", "кладбище", "ключ"]
OPTIONS = ["push", "prone", "heal", "necrotic", "fire"]
WEAPONS = ["mace", "improvised", "greatsword", "shortbow"]


def pick[T](rng: Rng, options: Sequence[T]) -> T:
    return options[rng.randint(0, len(options) - 1)]


class Pools:
    """Из чего собираются значения полей: id игры и приключения, находки, заклинания, действия.

    Поля с id берут значения по смыслу (кто действует — из отряда, цель — из бойцов), чтобы заметная
    часть вызовов удавалась, а не упиралась в «нет такого героя»."""

    def __init__(self, ctx: ToolContext, srd: SrdContent) -> None:
        adventure, state = ctx.adventure, ctx.state
        party = list(state.party)
        fighters = list(state.combat.combatants) if state.combat else []
        enemies = [f for f in fighters if f not in state.party]
        features = [f.id for loc in adventure.locations.values() for f in loc.features]
        exits = [e.id for loc in adventure.locations.values() for e in loc.exits]
        clues = [c.id for rev in adventure.revelations.values() for c in rev.clues]
        zones = [z.id for loc in adventure.locations.values() for z in loc.zones]
        monsters = sorted({m.monster_id for e in adventure.encounters.values() for m in e.monsters})
        npcs, items = list(adventure.npcs), list(adventure.items)
        places = list(adventure.locations)
        everything = {*party, *fighters, *npcs, *items, *places, *adventure.encounters}
        self.ids = sorted({*everything, *features, *exits, *zones, *monsters, "ghost"})
        refs = [f"feature:{x}" for x in features] + [f"exit:{x}" for x in exits]
        self.by_name: dict[str, Sequence[str]] = {
            "ref": [*refs, *(f"clue:{x}" for x in clues), "clue:nope", "door:1"],
            "dice": DICE,
            "query": WORDS,
            "option": OPTIONS,
            "weapon": WEAPONS,
            "spell_id": sorted(srd.spells),
            "action_id": sorted({a.id for m in monsters for a in srd.monsters[m].actions}),
            "character_id": [*party, "ghost"],
            "actor_id": [*party, *enemies[:1]],
            "a_id": party,
            "b_id": [*party, *npcs],
            "target_id": [*enemies, *party] or party,
            "target_ids": [*enemies, *party] or party,
            "monster_id": [*enemies, *monsters] or monsters,
            "engage": enemies or monsters,
            "encounter_id": [*adventure.encounters, "ghost"],
            "npc_id": [*npcs, "ghost"],
            "item_id": [*items, "ghost"],
            "location_id": [*places, "ghost"],
            "via_exit": exits,
            "to_zone": zones,
            "pc_zone": zones,
            "zone": zones,
        }


def random_value(rng: Rng, pools: Pools, name: str, node: dict[str, Any], defs: dict) -> Any:
    """Значение поля по его схеме; в каждом двенадцатом случае — мусор."""
    if rng.randint(1, 12) == 1:
        return pick(rng, JUNK)
    if "$ref" in node:
        return random_value(rng, pools, name, defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in node:
        return random_value(rng, pools, name, pick(rng, node["anyOf"]), defs)
    if "enum" in node:
        return pick(rng, node["enum"])
    match node.get("type"):
        case "string":
            return pick(rng, pools.by_name.get(name) or [*pools.ids, "т"])
        case "integer":
            return rng.randint(node.get("minimum", -3), node.get("maximum", 25))
        case "boolean":
            return rng.randint(0, 1) == 1
        case "array":
            return [
                random_value(rng, pools, name, node.get("items", {}), defs)
                for _ in range(rng.randint(0, 3))
            ]
        case "object" if "properties" in node:
            return random_object(rng, pools, node, defs)
        case "object":  # словарь «id: число», как `short_rest.spend`
            return {pick(rng, pools.ids): rng.randint(0, 3) for _ in range(rng.randint(0, 2))}
    return pick(rng, JUNK)


def random_object(rng: Rng, pools: Pools, schema: dict[str, Any], defs: dict) -> dict[str, Any]:
    """Аргументы: все обязательные поля и каждое третье необязательное."""
    return {
        name: random_value(rng, pools, name, node, defs)
        for name, node in schema["properties"].items()
        if name in schema.get("required", []) or rng.randint(0, 2) == 0
    }


def start_game(seed: int, make_scene: Callable[..., ToolContext], rng: Rng) -> ToolContext:
    """Три героя на кладбище, первый в очереди случайный; бой идёт в двух играх из трёх."""
    fight = rng.randint(0, 2) > 0
    ctx = make_scene(
        first=pick(rng, [HERO, ALLY, WIZARD]),
        fight=fight,
        engaged=fight and rng.randint(0, 1) == 1,
        secret=(pick(rng, [HERO, ALLY]),),
    )
    ctx.rng = SeededRng(seed + 10_000)
    ctx.state.party[HERO].inspiration = True
    ctx.inspired = {HERO}
    return ctx


def next_actor(state: GameState) -> Combatant:
    """Следующий в очереди, кто способен действовать; если такого нет — тот, чей ход сейчас.

    Ход мастера разбирает ход героя и идущие за ним ходы монстров: обёртка боя передаёт ход
    следующему бойцу (`advance_to`), если между ними нет способного действовать."""
    combat = state.combat
    for step in range(1, len(combat.order)):
        fighter_id = combat.order[(combat.turn_index + step) % len(combat.order)]
        if can_act(state, fighter_id):
            return combat.combatants[fighter_id]
    return current_actor(state)


def turn_call(rng: Rng, ctx: ToolContext, srd: SrdContent) -> tuple[str, dict[str, Any]]:
    """Вызов за того, чей ход в бою, или за следующего в очереди (ход переходит к нему): монстр
    действует по статблоку, герой бьёт, идёт или защищается.

    Случайные вызовы редко попадают в очередь хода, и без этого бой почти не проверялся бы. Без
    передачи хода очередь стояла бы на первом герое, и монстры почти никогда не ходили бы."""
    combat = ctx.state.combat
    actor = pick(rng, [current_actor(ctx.state), next_actor(ctx.state)])
    enemies = [f for f in combat.combatants if f not in ctx.state.party]
    # Герой бьёт булавой того, с кем сцепился, если такой есть: иначе удар почти всегда — отказ.
    foes = [f for f in actor.engaged_with if f in enemies] or enemies
    if actor.kind == "monster":
        action = pick(rng, srd.monsters[actor.ref].actions)
        target = {"target_ids": [pick(rng, [*ctx.state.party])]}
        return "monster_action", {"monster_id": actor.id, "action_id": action.id} | target
    return pick(
        rng,
        [
            ("attack", {"actor_id": actor.id, "target_id": pick(rng, foes), "weapon": "mace"}),
            (
                "cast_spell",
                {"actor_id": actor.id, "spell_id": "fire_bolt", "target_ids": enemies[:1]},
            ),
            ("combat_action", {"actor_id": actor.id, "action": "dodge"}),
            (
                "move",
                {
                    "actor_id": actor.id,
                    "to_zone": pick(rng, [zone.id for zone in combat.zones]),
                    "engage": pick(rng, enemies),
                },
            ),
        ],
    )


def random_call(
    rng: Rng, ctx: ToolContext, srd: SrdContent, specs: list[ToolSpec]
) -> tuple[ToolSpec, dict[str, Any]]:
    """Вызов за героя в бою, образцовый вызов с подмешанными случайными полями или случайный."""
    pools = Pools(ctx, srd)
    if ctx.state.combat is not None and rng.randint(0, 1) == 1:
        name, args = turn_call(rng, ctx, srd)
        return next(spec for spec in specs if spec.name == name), args
    if rng.randint(1, 4) > 1:
        valid = pick(rng, VALID_CALLS)
        spec = next(spec for spec in specs if spec.name == valid.tool)
        schema = spec.json_schema()
        defs = schema.get("$defs", {})
        args = {
            name: random_value(rng, pools, name, schema["properties"][name], defs)
            if rng.randint(1, 4) == 1
            else value
            for name, value in valid.args.items()
        }
        return spec, args
    spec = pick(rng, specs)
    schema = spec.json_schema()
    return spec, random_object(rng, pools, schema, schema.get("$defs", {}))


def broken_invariants(state: GameState, adventure: Adventure) -> list[str]:
    """Что в состоянии после удачного вызова не должно нарушаться никогда."""
    world, found = state.world, []
    if len(set(world.revealed)) != len(world.revealed):
        found.append(f"находка отмечена дважды: {world.revealed}")
    if len(set(world.visited_locations)) != len(world.visited_locations):
        found.append(f"локация посещена дважды: {world.visited_locations}")
    if state.location_id not in adventure.locations:
        found.append(f"отряд в несуществующей локации {state.location_id!r}")
    known = {
        "feature": {f.id for loc in adventure.locations.values() for f in loc.features},
        "exit": {e.id for loc in adventure.locations.values() for e in loc.exits},
        "clue": {c.id for rev in adventure.revelations.values() for c in rev.clues},
    }
    for ref in world.revealed:
        kind, _, ident = ref.partition(":")
        if ident not in known.get(kind, set()):
            found.append(f"отмечена находка, которой нет в приключении: {ref!r}")
    for name, value in world.flags.items():
        if name.startswith("encounter:") and (
            name.partition(":")[2] not in adventure.encounters or value not in {"active", "done"}
        ):
            found.append(f"флаг боя {name!r} = {value!r}, который ведёт только движок")
    for npc_id in (*world.npc_alive, *world.npc_attitude, *world.npc_location):
        if npc_id not in adventure.npcs:
            found.append(f"состояние чужого NPC {npc_id!r}")
    for item_id, holder in world.item_holders.items():
        kind, _, holder_id = holder.partition(":")
        if item_id not in adventure.items:
            found.append(f"держатель у чужого предмета {item_id!r}")
        if holder != "gone" and kind not in {"location", "character", "npc"}:
            found.append(f"держатель {holder!r} не по формату")
        if kind == "character" and holder_id not in state.party:
            found.append(f"предмет {item_id!r} у героя вне отряда: {holder!r}")
    if any(not -3 <= value <= 3 for value in world.npc_attitude.values()):
        found.append(f"отношение NPC вне -3..3: {world.npc_attitude}")
    for hero in state.party.values():
        if not 0 <= hero.hp <= hero.hp_max:
            found.append(f"HP {hero.id}: {hero.hp} из {hero.hp_max}")
        if any(count < 0 for count in hero.spell_slots.values()):
            found.append(f"ячейки {hero.id} ушли в минус: {hero.spell_slots}")
        if len({(c.id, c.source) for c in hero.conditions}) != len(hero.conditions):
            # Одно состояние от разных источников (мастер и 0 HP) — не дубль: снимают по источнику.
            found.append(f"состояние {hero.id} с тем же источником наложено дважды")
        if any(entry.qty < 0 for entry in hero.inventory):
            found.append(f"предметов у {hero.id} меньше нуля")
    combat = state.combat
    for fighter in combat.combatants.values() if combat else []:
        if fighter.kind == "monster" and not 0 <= fighter.hp <= fighter.hp_max:
            found.append(f"HP {fighter.id}: {fighter.hp} из {fighter.hp_max}")
    if (combat is not None) != (state.scene.type == SceneType.COMBAT):
        found.append(f"бой {combat is not None}, а сцена {state.scene.type}")
    return found


def test_random_calls_never_break_the_turn_or_the_state(tool_registry, srd, make_scene):
    tools = {spec.name for spec in tool_registry.specs()}
    succeeded: Counter[str] = Counter()
    total = 0

    for seed in range(MAX_SEEDS):
        if seed >= MIN_SEEDS and set(succeeded) == tools:
            break  # добор не нужен: каждый инструмент уже сработал
        rng = SeededRng(seed)
        ctx = start_game(seed, make_scene, rng)
        for step in range(CALLS_PER_SEED):
            spec, args = random_call(rng, ctx, srd, tool_registry.specs())
            shown = json.dumps(args, ensure_ascii=False)[:200]
            where = f"seed {seed}, шаг {step}: {spec.name}({shown})"
            state_before, events_before = ctx.state.model_dump_json(), len(ctx.events)
            inspired_before = set(ctx.inspired)

            outcome = tool_registry.call(ctx, spec.name, args)  # сбой движка упадёт здесь

            total += 1
            assert (outcome.result is None) != (outcome.error is None), where
            if outcome.error is not None:
                assert ctx.state.model_dump_json() == state_before, where
                assert len(ctx.events) == events_before and ctx.inspired == inspired_before, where
                continue
            succeeded[spec.name] += 1
            assert broken_invariants(ctx.state, ctx.adventure) == [], where
            assert GameState.model_validate_json(ctx.state.model_dump_json()) == ctx.state, where
            new_events = ctx.events[events_before:]
            assert EVENTS.validate_json(EVENTS.dump_json(new_events)) == new_events, where
            for roll in (e for e in new_events if isinstance(e, RollEvent)):
                # Бросок тайного автора не бывает общим, а адресат тайного — герой отряда.
                # Исключение — броски, которые инструмент объявил общими (инициатива, §15).
                assert roll.audience == "all" or roll.audience in ctx.state.party, where
                if roll.label not in spec.public_roll_labels:
                    assert roll.audience != "all" or roll.actor_id not in ctx.secret_actors, where

    # Серия не должна тихо вырождаться в одни отказы: каждый инструмент хоть раз сработал.
    assert set(succeeded) == tools, (tools - set(succeeded), succeeded)
    assert sum(succeeded.values()) > total // 10
