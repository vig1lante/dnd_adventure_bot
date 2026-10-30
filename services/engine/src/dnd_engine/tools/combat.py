"""Инструменты боя: `start_combat`, `attack`, `cast_spell`, `move`, `combat_action`,
`monster_action` и `end_scene`.

Обработчики — тонкие обёртки над `combat.engine`: правила, броски и числа считает движок, модель
выбирает действие. Ошибки правил (`CombatError`) становятся `ToolError`, то есть результатом с
текстом для модели; обработчик проверяет всё до первого изменения состояния, а изменённое
состояние принимает только целиком, после успешного вызова движка (`commit_state`).

Чей ход разыгрывается. Движок разрешает боевые действия только в ход бойца. За один вызов мастера
разбираются ход персонажа и идущие следом ходы монстров, поэтому обёртка сама
передаёт ход (`advance_to` = `end_turn` на рабочей копии), но только бойцу из очереди `ctx.acting`
и только если между ним и текущим нет способного действовать (его ход не пропустить). Очередь
называет сервер при создании контекста: персонаж, чей игрок сдал ход, и монстры за ним до
следующего персонажа. По умолчанию очередь пуста, и тогда в бою не действует никто: модель не
разыграет за игрока ход, который тот не сдавал. Реакция (подготовленная атака) и
действия монстра без своего хода (`kind: other`) очередь не требуют.

Статблоки приключения. Бой ищет монстров в данных SRD, а у приключения могут быть свои: обёртки
отдают движку `combat_content` — SRD вместе со статблоками приключения, свой важнее одноимённого
из SRD (как `adventure.budget.find_monster`).

Предметы приключения. `combat_action(use_item)` с id предмета приключения берёт механику у его
предмета SRD (`Item.base_item`), проверяет держателя через `lore.item_holder` и после применения
отдаёт предмет миру (`gone`, `ItemEvent`): предметы приключения учитывает один `world.item_holders`.
"""

from collections.abc import Callable, Iterable
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from dnd_engine.adventure.budget import find_monster
from dnd_engine.adventure.schema import Encounter, Item, Location
from dnd_engine.adventure.validator import MAX_ACTION_TARGETS, MAX_MONSTERS_PER_ENCOUNTER
from dnd_engine.combat import engine
from dnd_engine.combat.engine import CombatError, SimpleAction
from dnd_engine.combat.model import Combatant
from dnd_engine.content import MonsterDef, SrdContent
from dnd_engine.dice import Advantage
from dnd_engine.events import (
    ActionEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    HealEvent,
    ItemEvent,
    RestEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.state import GameState, SceneType
from dnd_engine.tools.lore import MAX_ID_CHARS, item_holder
from dnd_engine.tools.registry import (
    MAX_REASON_CHARS,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolSpec,
    commit_state,
    find_or_error,
)

# Монстров в бою столько же, сколько допускает валидатор приключения: бой, который он принял,
# должен начинаться. То же число — предел одной группы.
MAX_MONSTERS = MAX_MONSTERS_PER_ENCOUNTER
MAX_MONSTER_GROUPS = 8
# Целей заклинания или действия монстра: дротики и лучи берут несколько. Действие монстра, которое
# принял валидатор (`targets` не больше этого числа), модель должна суметь назвать целиком.
MAX_TARGETS = MAX_ACTION_TARGETS
MAX_OPTION_CHARS = 32
# Элемент списка id: у `Field(max_length=...)` на самом списке предел относится к числу элементов.
TargetId = Annotated[str, StringConstraints(max_length=MAX_ID_CHARS)]
INITIATIVE_LABEL = "Инициатива"  # так движок подписывает бросок инициативы (`start_combat`)


# --- общие части: данные, состояние, пересказ событий ---


def combat_content(ctx: ToolContext) -> SrdContent:
    """Данные SRD вместе со статблоками приключения; свой статблок важнее одноимённого из SRD.

    Бой находит монстра по `Combatant.ref` только в SRD, поэтому без этого кастомный монстр
    приключения не смог бы ни действовать, ни получить защиты."""
    if not ctx.adventure.monsters:
        return ctx.content
    monsters = {**ctx.content.monsters, **ctx.adventure.monsters}
    return ctx.content.model_copy(update={"monsters": monsters})


def _roll_line(event: RollEvent) -> dict[str, Any]:
    line = {
        "roll": event.label,
        "actor": event.actor_id,
        "natural": event.roll.natural,
        "total": event.roll.total,
        "dc": event.dc,
        "target_ac": event.target_ac,
        "success": event.success,
    }
    return {key: value for key, value in line.items() if value is not None}


def _event_line(event: Event) -> dict[str, Any]:
    """Короткая строка события для модели; неизвестные виды событий — только их название."""
    match event:
        case RollEvent():
            return _roll_line(event)
        case DamageEvent():
            return {
                "damage": event.amount,
                "type": event.damage_type,
                "target": event.target_id,
                "hp_left": event.hp_after,
            }
        case HealEvent():
            return {"heal": event.amount, "target": event.target_id, "hp": event.hp_after}
        case ConditionEvent():
            return {"condition": event.condition, "target": event.target_id, "added": event.added}
        case ActionEvent():
            line = {
                "action": event.action,
                "actor": event.actor_id,
                "targets": event.target_ids,
                "label": event.label,
            }
            return {key: value for key, value in line.items() if value}
        case CombatEvent():
            line = {"combat": event.phase, "actor": event.actor_id, "winner": event.winner}
            return {key: value for key, value in line.items() if value is not None}
        case SceneEvent():
            return {"scene": event.scene.value}
        case RestEvent():
            return {"rest": event.rest}
        case _:
            return {"event": event.kind}


def event_lines(events: Iterable[Event]) -> list[dict[str, Any]]:
    """Пересказ событий вызова для модели: что выпало, кто получил урон и какие состояния."""
    return [_event_line(event) for event in events]


def _fight_result(ctx: ToolContext, events: list[Event]) -> dict[str, Any]:
    """Итог боевого вызова: события и где сейчас бой. `winner` подсказывает вызвать `end_scene`."""
    result: dict[str, Any] = {"events": event_lines(events)}
    if ctx.state.combat is not None:
        result["current_actor"] = engine.current_actor(ctx.state).id
        result["round"] = ctx.state.combat.round
        result["winner"] = engine.combat_winner(ctx.state)
    return result


# --- ход бойца ---


def _require_combat(ctx: ToolContext) -> None:
    if ctx.state.combat is None:
        raise ToolError("Доступно только в бою: сначала начните бой инструментом start_combat.")


def _fighter(ctx: ToolContext, fighter_id: str) -> Combatant:
    fighters = ctx.state.combat.combatants
    return find_or_error(fighters, "бойца", fighter_id)


def _check_in_acting(ctx: ToolContext, fighter: Combatant) -> None:
    """Боец ходит в этом вызове только из очереди, которую назвал сервер (`ctx.acting`)."""
    acting = ctx.acting
    if not acting:
        raise ToolError(
            "В этом вызове в бою никто не действует: ходы называет сервер (очередь acting), "
            "и разыгрывается только ход игрока, который его сдал, и монстров за ним."
        )
    if fighter.id not in acting:
        raise ToolError(
            f"{fighter.name} ({fighter.id}) не действует в этом вызове: ходят {', '.join(acting)}."
        )


def _play(
    ctx: ToolContext,
    fighter_id: str,
    call: Callable[[GameState, SrdContent], tuple[GameState, list[Event]]],
    *,
    own_turn: bool = True,
) -> list[Event]:
    """Одно боевое действие бойца на копии состояния; возвращает события, принятые в `ctx`.

    Для действия своего хода ход сначала передаётся бойцу (`own_turn`), если сейчас ходит другой.
    Передача и действие идут на копии, а `ctx.state` меняется только после успеха обоих: отказ
    движка не оставляет ни переданного хода, ни событий. Броски конца прежнего хода при отказе
    пропадают вместе с копией и модели не видны. Броски передачи хода (конец хода прежнего
    бойца, спасброски лежащих от смерти) к действию автора не относятся: тайный автор их не
    закрывает (`ToolContext.begin_action`)."""
    _require_combat(ctx)
    fighter = _fighter(ctx, fighter_id)
    content = combat_content(ctx)
    state, passed = ctx.state, []
    try:
        if own_turn:
            _check_in_acting(ctx, fighter)
            state, passed = engine.advance_to(state, fighter_id, rng=ctx.rng, content=content)
        state, acted = call(state, content)
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    first = len(ctx.events)
    commit_state(ctx, state, [*passed, *acted])
    ctx.begin_action(first + len(passed))
    return [*passed, *acted]


def _inspired_advantage(ctx: ToolContext, actor_id: str) -> Advantage:
    """Преимущество от вдохновения, если игрок отметил его и оно есть у персонажа. Не тратит.

    То же условие, что в `ToolContext.take_inspiration`: тратить вдохновение можно только после
    удачного вызова движка, иначе отказ потерял бы его зря."""
    hero = ctx.state.party.get(actor_id)
    inspired = hero is not None and hero.inspiration and actor_id in ctx.inspired
    return Advantage.ADVANTAGE if inspired else Advantage.NONE


# --- start_combat ---


class MonsterSpec(ToolArgs):
    monster_id: str = Field(max_length=MAX_ID_CHARS, description="id монстра SRD или приключения")
    count: int = Field(default=1, ge=1, le=MAX_MONSTERS, description="сколько таких монстров")
    zone: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id зоны локации; без значения — зона отряда",
    )


class StartCombatArgs(ToolArgs):
    encounter_id: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id боя из приключения в текущей локации; важнее списка monsters",
    )
    monsters: list[MonsterSpec] | None = Field(
        default=None,
        max_length=MAX_MONSTER_GROUPS,
        description="свои группы монстров, если боя в приключении нет",
    )
    pc_zone: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="зона, где стоит отряд; без значения — как в бою приключения или первая зона",
    )


def _encounter_here(ctx: ToolContext, encounter_id: str) -> Encounter:
    encounter = find_or_error(ctx.adventure.encounters, "боя", encounter_id)
    state = ctx.state
    if encounter.location_id != state.location_id:
        raise ToolError(
            f"Бой {encounter.id!r} происходит в локации {encounter.location_id!r}, а отряд в "
            f"{state.location_id!r}: зоны боя берутся из локации."
        )
    if state.world.flags.get(f"encounter:{encounter.id}") == "done":
        raise ToolError(f"Бой {encounter.id!r} уже завершён.")
    return encounter


def _statblock(ctx: ToolContext, monster_id: str) -> MonsterDef:
    monster = find_monster(monster_id, ctx.adventure, ctx.content)
    if monster is None:
        known = ", ".join(combat_content(ctx).monsters)
        raise ToolError(f"Неизвестный монстр {monster_id!r}. Есть: {known}")
    return monster


def _lineup(
    ctx: ToolContext, groups: Iterable[tuple[str, int, str]]
) -> list[tuple[MonsterDef, str]]:
    """Монстры боя парами (статблок, зона) в порядке групп; общее число монстров ограничено."""
    lineup: list[tuple[MonsterDef, str]] = []
    for monster_id, count, zone in groups:
        lineup += [(_statblock(ctx, monster_id), zone)] * count
    if len(lineup) > MAX_MONSTERS:
        raise ToolError(f"Слишком много монстров: {len(lineup)}, предел {MAX_MONSTERS}.")
    return lineup


def _fighter_line(fighter: Combatant) -> dict[str, Any]:
    line: dict[str, Any] = {
        "id": fighter.id,
        "name": fighter.name,
        "side": fighter.side,
        "initiative": fighter.initiative,
        "zone": fighter.zone_id,
    }
    if fighter.kind == "monster":
        line |= {"hp": fighter.hp, "ac": fighter.ac}
    return line


def _start_result(ctx: ToolContext, events: list[Event], ignored: str | None) -> dict[str, Any]:
    combat = ctx.state.combat
    result: dict[str, Any] = {
        "encounter": combat.encounter_id,
        "round": combat.round,
        "current_actor": engine.current_actor(ctx.state).id,
        "order": [_fighter_line(combat.combatants[fighter_id]) for fighter_id in combat.order],
        "zones": [{"id": zone.id, "name": zone.name} for zone in combat.zones],
        "links": [link.model_dump() for link in combat.links],
        # Броски инициативы уже видны в порядке хода.
        "events": event_lines(
            e for e in events if not (isinstance(e, RollEvent) and e.label == INITIATIVE_LABEL)
        ),
    }
    if ignored:
        result["ignored"] = ignored
    return result


def start_combat(ctx: ToolContext, args: StartCombatArgs) -> dict[str, Any]:
    state, adventure = ctx.state, ctx.adventure
    if state.combat is not None:
        raise ToolError("Бой уже идёт: начать второй нельзя, закончите его инструментом end_scene.")
    location: Location = adventure.locations[state.location_id]
    if not location.zones:
        # Валидатор такую локацию отклоняет; без этой проверки `zones[0]` ниже роняет ход мастера.
        raise ToolError(
            f"В локации {location.id!r} нет зон: бой в ней начать нельзя (ошибка данных "
            "приключения)."
        )
    encounter: Encounter | None = None
    ignored = None
    if args.encounter_id is not None:
        encounter = _encounter_here(ctx, args.encounter_id)
        groups = [(group.monster_id, group.count, group.zone) for group in encounter.monsters]
        pc_zone = args.pc_zone or encounter.pc_zone
        ignored = "monsters: при encounter_id берутся монстры боя" if args.monsters else None
    elif args.monsters:
        pc_zone = args.pc_zone or location.zones[0].id
        groups = [(spec.monster_id, spec.count, spec.zone or pc_zone) for spec in args.monsters]
    else:
        raise ToolError("Укажите encounter_id или monsters: бой из приключения или свои монстры.")
    lineup = _lineup(ctx, groups)

    try:
        started, events = engine.start_combat(
            state,
            encounter_id=encounter.id if encounter else None,
            zones=location.zones,
            links=location.links,
            monsters=lineup,
            pc_zone=pc_zone,
            rng=ctx.rng,
            content=combat_content(ctx),
        )
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    if encounter is not None:
        started.world.flags[f"encounter:{encounter.id}"] = "active"
    commit_state(ctx, started, events)
    return _start_result(ctx, events, ignored)


# --- attack, cast_spell, move, combat_action, monster_action ---


class AttackArgs(ToolArgs):
    actor_id: str = Field(max_length=MAX_ID_CHARS, description="id бойца, который атакует")
    target_id: str = Field(max_length=MAX_ID_CHARS, description="id бойца-противника")
    weapon: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="для персонажа: id оружия из его инвентаря или improvised (подручный предмет)",
    )
    action_id: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="для монстра: id действия-атаки из его статблока",
    )
    reaction: bool = Field(
        default=False,
        description=(
            "true только для подготовленной атаки: боец выбрал «Подготовку», условие наступило, "
            "и он бьёт реакцией вне своего хода"
        ),
    )


def attack(ctx: ToolContext, args: AttackArgs) -> dict[str, Any]:
    advantage = _inspired_advantage(ctx, args.actor_id)
    events = _play(
        ctx,
        args.actor_id,
        lambda state, content: engine.attack(
            state,
            args.actor_id,
            args.target_id,
            weapon=args.weapon,
            action_id=args.action_id,
            advantage=advantage,
            reaction=args.reaction,
            rng=ctx.rng,
            content=content,
        ),
        own_turn=not args.reaction,
    )
    result = _fight_result(ctx, events)
    if advantage == Advantage.ADVANTAGE:
        ctx.take_inspiration(args.actor_id)  # удар удался: вдохновение потрачено
        result["inspiration_spent"] = True
    return result


class CastSpellArgs(ToolArgs):
    actor_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа-заклинателя")
    spell_id: str = Field(max_length=MAX_ID_CHARS, description="id заклинания или заговора")
    target_ids: list[TargetId] = Field(
        default_factory=list,
        max_length=MAX_TARGETS,
        description="id целей; для заклинаний на себя можно не указывать",
    )
    slot_level: int | None = Field(
        default=None,
        ge=1,
        le=9,
        description="уровень ячейки; без значения — самая низкая подходящая, заговору не нужна",
    )


def cast_spell(ctx: ToolContext, args: CastSpellArgs) -> dict[str, Any]:
    events = _play(
        ctx,
        args.actor_id,
        lambda state, content: engine.cast_spell(
            state,
            args.actor_id,
            args.spell_id,
            args.target_ids,
            slot_level=args.slot_level,
            rng=ctx.rng,
            content=content,
        ),
    )
    return _fight_result(ctx, events)


class MoveArgs(ToolArgs):
    actor_id: str = Field(max_length=MAX_ID_CHARS, description="id бойца, который перемещается")
    to_zone: str | None = Field(
        default=None, max_length=MAX_ID_CHARS, description="id связанной зоны «рядом»"
    )
    engage: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id противника из зоны назначения, с которым боец вступает в ближний бой",
    )
    dash: bool = Field(default=False, description="Рывок: действие даёт второе перемещение")


def move(ctx: ToolContext, args: MoveArgs) -> dict[str, Any]:
    events = _play(
        ctx,
        args.actor_id,
        lambda state, content: engine.move(
            state,
            args.actor_id,
            to_zone=args.to_zone,
            engage=args.engage,
            dash=args.dash,
            rng=ctx.rng,
            content=content,
        ),
    )
    return _fight_result(ctx, events)


class CombatActionArgs(ToolArgs):
    actor_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа, который действует")
    action: SimpleAction = Field(description="какое действие")
    target_id: str | None = Field(
        default=None, max_length=MAX_ID_CHARS, description="id цели, если действию она нужна"
    )
    item_id: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="для use_item: id предмета из инвентаря или предмета приключения у персонажа",
    )
    option: str | None = Field(
        default=None,
        max_length=MAX_OPTION_CHARS,
        description=(
            "вариант действия: толчок (push или prone), божественная искра (heal, necrotic, "
            "radiant), тип урона дыхания"
        ),
    )


def _adventure_item_to_use(
    ctx: ToolContext, args: CombatActionArgs
) -> tuple[dict[str, str], Item | None]:
    """Предмет приключения для `use_item`: держатель — сам персонаж, механика — у предмета SRD.

    Возвращает пары (предмет приключения -> предмет SRD) для движка и сам предмет. Не предмет
    приключения или другое действие — пусто: тогда зелье берётся из инвентаря персонажа."""
    if args.action != "use_item" or args.item_id not in ctx.adventure.items:
        return {}, None
    item = ctx.adventure.items[args.item_id]
    holder = item_holder(ctx.state, ctx.adventure, item.id)
    if holder != f"character:{args.actor_id}":
        raise ToolError(
            f"Предмет {item.id!r} сейчас у {holder!r}, а не у {args.actor_id!r}: сначала отдайте "
            "его инструментом give_item."
        )
    if item.base_item is None:
        raise ToolError(
            f"Предмет {item.id!r} нельзя применить в бою: у него нет боевой механики (base_item)."
        )
    return {item.id: item.base_item}, item


def combat_action(ctx: ToolContext, args: CombatActionArgs) -> dict[str, Any]:
    carried, adventure_item = _adventure_item_to_use(ctx, args)
    events = _play(
        ctx,
        args.actor_id,
        lambda state, content: engine.simple_action(
            state,
            args.actor_id,
            args.action,
            target_id=args.target_id,
            item_id=args.item_id,
            option=args.option,
            rules=ctx.rules,
            carried_items=carried,
            rng=ctx.rng,
            content=content,
        ),
    )
    result = _fight_result(ctx, events)
    if adventure_item is not None:  # действие удалось: предмет приключения израсходован
        ctx.state.world.item_holders[adventure_item.id] = "gone"
        ctx.events.append(
            ItemEvent(
                item_id=adventure_item.id,
                holder_from=f"character:{args.actor_id}",
                holder_to="gone",
            )
        )
        result["item_used"] = adventure_item.name
    return result


class MonsterActionArgs(ToolArgs):
    monster_id: str = Field(max_length=MAX_ID_CHARS, description="id монстра в бою")
    action_id: str = Field(max_length=MAX_ID_CHARS, description="id действия из статблока")
    target_ids: list[TargetId] = Field(
        default_factory=list,
        max_length=MAX_TARGETS,
        description="id целей: одна на все атаки или по одной на каждую; для особых — не нужны",
    )


def _is_free_monster_action(ctx: ToolContext, args: MonsterActionArgs) -> bool:
    """Особое действие (`kind: other`: бонусное, реакция, колдовство) идёт вне своего хода."""
    combat = ctx.state.combat
    slot = combat.combatants.get(args.monster_id) if combat is not None else None
    if slot is None or slot.kind != "monster":
        return False
    statblock = combat_content(ctx).monsters.get(slot.ref)
    action = (
        next((a for a in statblock.actions if a.id == args.action_id), None) if statblock else None
    )
    return action is not None and action.kind == "other"


def monster_action(ctx: ToolContext, args: MonsterActionArgs) -> dict[str, Any]:
    events = _play(
        ctx,
        args.monster_id,
        lambda state, content: engine.monster_action(
            state, args.monster_id, args.action_id, args.target_ids, rng=ctx.rng, content=content
        ),
        own_turn=not _is_free_monster_action(ctx, args),
    )
    return _fight_result(ctx, events)


# --- end_scene ---


class EndSceneArgs(ToolArgs):
    next: Literal["exploration", "social"] = Field(
        description="какая сцена начинается: exploration — исследование, social — разговор"
    )
    reason: str = Field(
        min_length=1, max_length=MAX_REASON_CHARS, description="коротко, почему сцена кончилась"
    )


def end_scene(ctx: ToolContext, args: EndSceneArgs) -> dict[str, Any]:
    state = ctx.state
    if state.scene.type == SceneType.CAMP:
        raise ToolError(
            "Привал заканчивает таймер или хост, а не мастер: сцену camp инструментом не завершить."
        )
    try:
        ended, events = engine.end_scene(
            state,
            SceneType(args.next),
            ctx.rules,
            content=combat_content(ctx),
            adventure=ctx.adventure,
        )
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    commit_state(ctx, ended, events)
    winner = next(
        (e.winner for e in events if isinstance(e, CombatEvent) and e.phase == "ended"), None
    )
    return {"scene": args.next, "winner": winner, "events": event_lines(events)}


COMBAT_TOOLS = (
    ToolSpec(
        name="start_combat",
        description=(
            "Начать бой: encounter_id — бой из приключения в текущей локации (монстры, зоны и "
            "расстановка из данных) или monsters — свои группы монстров. Если указаны оба, "
            "берётся encounter_id. Инициативу бросает движок; в результате порядок хода, зоны "
            "и кто ходит первым. Бой нельзя начать, пока идёт другой, и нельзя повторить "
            "завершённый."
        ),
        args_model=StartCombatArgs,
        handler=start_combat,
        # Инициатива всегда общая, даже у тайного актёра: она и так видна
        # всем в порядке хода. Спасбросок от смерти первого бойца остаётся его тайным броском.
        public_roll_labels=(INITIATIVE_LABEL,),
    ),
    ToolSpec(
        name="attack",
        description=(
            "Атака бойца по противнику: бросок против КД, урон, добавки, сопротивления считает "
            "движок. Персонаж бьёт оружием (weapon), монстр — действием (action_id). "
            "Рукопашная достаёт только вовлечённого противника (сначала move с engage). "
            "reaction=true — только подготовленная атака. Ходить можно за бойцов, названных в "
            "этом вызове; результат содержит winner, когда одна сторона побеждена."
        ),
        args_model=AttackArgs,
        handler=attack,
        author_fields=("actor_id",),
    ),
    ToolSpec(
        name="cast_spell",
        description=(
            "Персонаж творит заклинание в свой ход: ячейка, дальность, спасброски целей, урон и "
            "лечение считает движок. Укажите цели (target_ids) и при необходимости уровень "
            "ячейки. Нет ячейки или заклинание не подготовлено — ошибка."
        ),
        args_model=CastSpellArgs,
        handler=cast_spell,
        author_fields=("actor_id",),
    ),
    ToolSpec(
        name="move",
        description=(
            "Перемещение бойца: в связанную зону «рядом» (to_zone) и/или в ближний бой с "
            "противником (engage). Выход из ближнего боя без Отхода вызывает атаки по "
            "возможности, их разыгрывает движок. dash=true — Рывок (тратит действие)."
        ),
        args_model=MoveArgs,
        handler=move,
        author_fields=("actor_id",),
    ),
    ToolSpec(
        name="combat_action",
        description=(
            "Простое действие персонажа в бою: Уклонение, Отход, Рывок, Подготовка, Помощь, "
            "Спрятаться, использование предмета (зелье; предмет приключения тоже, он должен "
            "быть у персонажа), толчок, захват, умения классов и рас. Действие, бонусное "
            "действие и ресурсы тратит движок; правила модулей (например, зелье бонусным "
            "действием) применяются сами."
        ),
        args_model=CombatActionArgs,
        handler=combat_action,
        author_fields=("actor_id",),
    ),
    ToolSpec(
        name="monster_action",
        description=(
            "Действие монстра по его статблоку: атака, мультиатака, действие со спасброском "
            "(Сл из статблока) или особое действие (колдовство, бонусное действие: только "
            "событие для рассказа). Ход монстру передаётся сам, если он следующий в очереди "
            "этого вызова; пропустить ход монстра нельзя, пока он способен действовать."
        ),
        args_model=MonsterActionArgs,
        handler=monster_action,
    ),
    ToolSpec(
        name="end_scene",
        description=(
            "Закончить сцену и начать exploration (исследование) или social (разговор). Если "
            "шёл бой, он завершается: победа, бегство или переговоры; эффекты со сроком "
            "снимаются, павшие NPC отмечаются. Привал инструментом не завершить: его "
            "заканчивает таймер или хост."
        ),
        args_model=EndSceneArgs,
        handler=end_scene,
    ),
)
