"""Инструменты проверок: `request_check`, `request_save`, `contest`.

Мастер выбирает, кто бросает, что и против какой Сл; число на кубах и итог считает движок. Поэтому:
- Сл берётся только из таблицы 5/10/15/20/25/30: в схеме аргументов она перечислена, а путь
  «Сл движка» (`engine_saving_throw`, `monster_saving_throw`) из аргументов модели недоступен;
- бонус навыка и характеристики считает движок по персонажу или статблоку, модель его не задаёт;
- броски идут только через `ctx.rng`, а вдохновение (`ctx.take_inspiration`) даёт преимущество
  первому к20 и тратится одноразово;
- хуки модулей правил (`ctx.rules.check_hooks`) передаются проверкам: например, «Специи BG3»
  делают натуральную 20 автоуспехом. На спасброски и состязания хуки не действуют.

Событие броска добавляется с общей аудиторией: тайные броски закрывает `ToolRegistry.call` для
всего вызова сразу по авторам из `ToolSpec.author_fields` (персонаж проверки, обе стороны
состязания), обработчики о тайных ходах не знают.
"""

from typing import Any, Literal, NamedTuple

from pydantic import Field

from dnd_engine.abilities import ABILITY_NAMES_RU, Ability, Skill
from dnd_engine.adventure.budget import find_monster
from dnd_engine.character import ActiveCondition, Character
from dnd_engine.checks import (
    CheckResult,
    ContestSide,
    ability_check,
    character_contest_side,
    check_label,
    combine_advantage,
    contest,
    monster_contest_side,
    saving_throw,
)
from dnd_engine.conditions import auto_fail_condition
from dnd_engine.content import MonsterDef
from dnd_engine.dice import Advantage, RollResult
from dnd_engine.events import RollEvent
from dnd_engine.tools.lore import MAX_ID_CHARS
from dnd_engine.tools.registry import (
    MAX_REASON_CHARS,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolSpec,
    find_or_error,
)

# Допустимые Сл как перечисление: схема для модели и проверка аргументов говорят одно и то же.
# Совпадение с `checks.VALID_DCS` проверяет тест.
TableDC = Literal[5, 10, 15, 20, 25, 30]
DC_HELP = (
    "Сл только из таблицы: 5 — очень лёгкая, 10 — лёгкая, 15 — средняя, 20 — трудная, "
    "25 — очень трудная, 30 — почти невозможная"
)
REASON_HELP = "Коротко, зачем бросок: попадает в карточку броска и журнал"
ADVANTAGE_HELP = "преимущество или помеха по обстоятельствам; вдохновение движок добавит сам"
# Угрозы, которым есть дело до расы персонажа (Дварфийская стойкость: преимущество против яда).
Threat = Literal["poison"]


# --- request_check и request_save ---


class RequestCheckArgs(ToolArgs):
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа, кто бросает")
    ability: Ability = Field(description="характеристика проверки")
    skill: Skill | None = Field(
        default=None, description="навык; без него проверяется чистая характеристика"
    )
    dc: TableDC = Field(description=DC_HELP)
    advantage: Advantage = Field(default=Advantage.NONE, description=ADVANTAGE_HELP)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, description=REASON_HELP)


class RequestSaveArgs(ToolArgs):
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа, кто бросает")
    ability: Ability = Field(description="характеристика спасброска")
    dc: TableDC = Field(description=DC_HELP)
    advantage: Advantage = Field(default=Advantage.NONE, description=ADVANTAGE_HELP)
    against: Threat | None = Field(
        default=None,
        description="poison, если угроза — яд (у дварфов против него преимущество); иначе пусто",
    )
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, description=REASON_HELP)


def _record_roll(
    ctx: ToolContext, character: Character, label: str, result: CheckResult, inspiration: Advantage
) -> dict[str, Any]:
    """Событие броска и короткий итог для модели."""
    ctx.events.append(
        RollEvent(
            actor_id=character.id,
            label=label,
            roll=result.roll,
            dc=result.dc,
            success=result.success,
        )
    )
    summary: dict[str, Any] = {
        "actor": character.name,
        "check": label,
        "natural": result.roll.natural,
        "total": result.total,
        "dc": result.dc,
        "success": result.success,
    }
    if result.roll.advantage != Advantage.NONE:
        summary["advantage"] = result.roll.advantage.value
    if inspiration == Advantage.ADVANTAGE:
        summary["inspiration_spent"] = True
    if result.auto is not None:  # натуральная 20/1 от модуля правил или автопровал из-за состояния
        summary["auto"] = result.auto
    if result.condition is not None:
        summary["condition"] = result.condition
    return summary


def request_check(ctx: ToolContext, args: RequestCheckArgs) -> dict[str, Any]:
    character = find_or_error(ctx.state.party, "персонажа отряда", args.character_id)
    inspiration = ctx.take_inspiration(character.id)
    result = ability_check(
        character,
        args.ability,
        skill=args.skill,
        dc=args.dc,
        advantage=combine_advantage(args.advantage, inspiration),
        rng=ctx.rng,
        reason=args.reason,
        hooks=ctx.rules.check_hooks,
    )
    return _record_roll(ctx, character, check_label(args.ability, args.skill), result, inspiration)


def request_save(ctx: ToolContext, args: RequestSaveArgs) -> dict[str, Any]:
    character = find_or_error(ctx.state.party, "персонажа отряда", args.character_id)
    # Спасбросок, проваленный состоянием, идёт без куба: вдохновение игрока не тратим зря.
    auto_failed = auto_fail_condition(character.conditions, args.ability) is not None
    inspiration = Advantage.NONE if auto_failed else ctx.take_inspiration(character.id)
    result = saving_throw(
        character,
        args.ability,
        dc=args.dc,
        advantage=combine_advantage(args.advantage, inspiration),
        rng=ctx.rng,
        reason=args.reason,
        against=args.against,
        hooks=ctx.rules.check_hooks,
    )
    label = f"Спасбросок: {ABILITY_NAMES_RU[args.ability]}"
    return _record_roll(ctx, character, label, result, inspiration)


# --- contest ---


class ContestArgs(ToolArgs):
    a_id: str = Field(
        max_length=MAX_ID_CHARS,
        description=(
            "id первого участника: персонаж отряда, боец боя или NPC со статблоком из текущей "
            "локации"
        ),
    )
    a_skill: Skill | Ability = Field(description="навык или характеристика первого участника")
    b_id: str = Field(
        max_length=MAX_ID_CHARS,
        description="id второго участника (защищающегося: при ничьей побеждает он)",
    )
    b_skill: Skill | Ability = Field(description="навык или характеристика второго участника")
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, description=REASON_HELP)


class _Participant(NamedTuple):
    id: str
    name: str
    subject: Character | MonsterDef  # персонаж или статблок
    conditions: tuple[ActiveCondition, ...]  # состояния бойца боя; у остальных пусто


def _statblock(ctx: ToolContext, monster_id: str) -> MonsterDef:
    """Статблок: свой у приключения, иначе из SRD. Битая ссылка — ошибка данных, а не модели."""
    monster = find_monster(monster_id, ctx.adventure, ctx.content)
    if monster is None:
        raise LookupError(f"нет статблока {monster_id!r}: битая ссылка в данных")
    return monster


def _find_participant(ctx: ToolContext, who: str) -> _Participant:
    """Персонаж отряда, монстр идущего боя или живой NPC со статблоком из текущей локации; иначе
    `ToolError`.

    NPC не из этой локации в состязание не берётся: его карточка броска уходит всем игрокам и
    выдала бы NPC, которого отряд ещё не встретил. Положение NPC — как у остальных инструментов:
    переопределение мира (`world.npc_location`), иначе локация из приключения."""
    state = ctx.state
    if (character := state.party.get(who)) is not None:
        return _Participant(who, character.name, character, ())
    fighter = state.combat.combatants.get(who) if state.combat is not None else None
    if fighter is not None and fighter.kind == "monster":
        statblock = _statblock(ctx, fighter.ref)
        return _Participant(who, fighter.name, statblock, tuple(fighter.conditions))
    npc = ctx.adventure.npcs.get(who)
    if npc is None:
        raise ToolError(
            f"Нет участника {who!r}: нужен персонаж отряда, боец идущего боя или NPC со "
            f"статблоком. Персонажи: {', '.join(state.party)}"
        )
    if npc.statblock is None:
        raise ToolError(f"У NPC {npc.id!r} нет статблока: состязание с ним не посчитать.")
    if not state.world.npc_alive.get(npc.id, True):
        raise ToolError(f"NPC {npc.id!r} мёртв: состязание с ним невозможно.")
    if state.world.npc_location.get(npc.id, npc.location_id) != state.location_id:
        raise ToolError(
            f"NPC {npc.id!r} сейчас не в текущей локации отряда ({state.location_id!r}): "
            "состязание возможно только с NPC, который здесь."
        )
    return _Participant(who, npc.name, _statblock(ctx, npc.statblock), ())


def _contest_side(ctx: ToolContext, who: _Participant, stat: Skill | Ability) -> ContestSide:
    """Сторона состязания; бонус и эффекты считает движок. Вдохновение тратится только здесь."""
    if isinstance(who.subject, Character):
        return character_contest_side(who.subject, stat, advantage=ctx.take_inspiration(who.id))
    return monster_contest_side(who.subject, who.id, stat, conditions=who.conditions)


def _side_summary(who: _Participant, side: ContestSide, d20: RollResult) -> dict[str, Any]:
    return {"actor": who.name, "check": side.label, "natural": d20.natural, "total": d20.total}


def request_contest(ctx: ToolContext, args: ContestArgs) -> dict[str, Any]:
    if args.a_id == args.b_id:
        raise ToolError("Участники состязания должны различаться: a_id и b_id совпадают.")
    a, b = _find_participant(ctx, args.a_id), _find_participant(ctx, args.b_id)
    a_side, b_side = _contest_side(ctx, a, args.a_skill), _contest_side(ctx, b, args.b_skill)

    result = contest(a_side, b_side, rng=ctx.rng, reason=args.reason)

    ctx.events += [
        RollEvent(
            actor_id=a.id, label=a_side.label, roll=result.a_roll, success=result.winner == "a"
        ),
        RollEvent(
            actor_id=b.id, label=b_side.label, roll=result.b_roll, success=result.winner == "b"
        ),
    ]
    summary: dict[str, Any] = {
        "a": _side_summary(a, a_side, result.a_roll),
        "b": _side_summary(b, b_side, result.b_roll),
        "winner": (a if result.winner == "a" else b).name,
        "winner_side": result.winner,
    }
    if result.a_roll.total == result.b_roll.total:
        summary["tie"] = True  # ничья отдаёт победу второй стороне
    return summary


CHECK_TOOLS = (
    ToolSpec(
        name="request_check",
        description=(
            "Бросок проверки характеристики или навыка за персонажа против Сл. Сл выбирайте "
            "только из таблицы 5/10/15/20/25/30. Бонус, вдохновение, Удачу, Указание и правила "
            "модулей (например, натуральная 20) применяет движок: исход берите из результата "
            "и не объявляйте его сами."
        ),
        args_model=RequestCheckArgs,
        handler=request_check,
        author_fields=("character_id",),
    ),
    ToolSpec(
        name="request_save",
        description=(
            "Спасбросок персонажа против Сл из таблицы 5/10/15/20/25/30. Бонус, мастерство "
            "в спасброске и черты расы применяет движок. Для яда укажите against: poison."
        ),
        args_model=RequestSaveArgs,
        handler=request_save,
        author_fields=("character_id",),
    ),
    ToolSpec(
        name="contest",
        description=(
            "Состязание двух участников: каждый бросает к20 с бонусом своего навыка или "
            "характеристики, побеждает больший итог, при ничьей — второй (защищающийся). "
            "Участник — персонаж отряда, боец боя или NPC со статблоком из текущей локации; "
            "бонус считает движок."
        ),
        args_model=ContestArgs,
        handler=request_contest,
        author_fields=("a_id", "b_id"),
    ),
)
