"""Инструменты рассказа: `private_message`, `chronicle`, `heal`, `damage`, `apply_effect`.

Рассказ ведёт модель, а числа считает код: урон и лечение берутся только из записи костей
(`dice`), причём запись проходит пределы `dice.parse_model_dice` до первого броска; состояние
накладывает и снимает `apply_effect` без Сл, спасброска и источника, которых модель не задаёт.
Порядок обработчиков тот же, что в других инструментах: сначала проверка всего, что пришло от
модели, потом бросок и изменение состояния, так что отказ ничего не оставляет за собой.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from dnd_engine.character import ActiveCondition, Character
from dnd_engine.combat import engine
from dnd_engine.combat.engine import EXTERNAL_CONDITION_MAX_ROUNDS, CombatError
from dnd_engine.combat.model import Combatant
from dnd_engine.conditions import is_dead
from dnd_engine.dice import MODEL_DICE_MAX_CHARS, DiceSyntaxError, parse_model_dice, roll
from dnd_engine.events import (
    ChronicleEvent,
    DamageEvent,
    HealEvent,
    PrivateMessageEvent,
    RollEvent,
)
from dnd_engine.tools.combat import combat_content, event_lines
from dnd_engine.tools.lore import MAX_ID_CHARS
from dnd_engine.tools.registry import (
    MAX_REASON_CHARS,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolSpec,
    commit_state,
    find_or_error,
)

MAX_MESSAGE_CHARS = 1000
MAX_CHRONICLE_CHARS = 400  # запись летописи — одна-две строки

# Типы урона SRD: совпадение с данными проверяет тест.
DamageType = Literal[
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
]
# Состояния SRD: метки заклинаний («Направленный снаряд», «Помощь») состояниями не являются и
# модели недоступны. Совпадение с данными SRD проверяет тест.
ConditionId = Literal[
    "blinded",
    "charmed",
    "deafened",
    "frightened",
    "grappled",
    "incapacitated",
    "invisible",
    "paralyzed",
    "petrified",
    "poisoned",
    "prone",
    "restrained",
    "stunned",
    "unconscious",
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- private_message и chronicle ---


class PrivateMessageArgs(ToolArgs):
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа-адресата")
    text: Text = Field(max_length=MAX_MESSAGE_CHARS, description="текст, который увидит только он")


def private_message(ctx: ToolContext, args: PrivateMessageArgs) -> dict[str, Any]:
    character = find_or_error(ctx.state.party, "персонажа отряда", args.character_id)
    ctx.events.append(PrivateMessageEvent(audience=character.id, text=args.text))
    return {"to": character.name}


class ChronicleArgs(ToolArgs):
    entry: Text = Field(
        max_length=MAX_CHRONICLE_CHARS, description="запись летописи: одна-две строки о ходе"
    )


def chronicle(ctx: ToolContext, args: ChronicleArgs) -> dict[str, Any]:
    ctx.state.chronicle.entries.append(args.entry)
    ctx.events.append(ChronicleEvent(text=args.entry))
    return {"entries": len(ctx.state.chronicle.entries)}


# --- heal и damage: только из кости ---


class HealArgs(ToolArgs):
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа отряда")
    dice: str = Field(
        max_length=MODEL_DICE_MAX_CHARS,
        description="запись костей лечения, например 2d4+2 (до 20 костей); числа без костей нельзя",
    )
    reason: str = Field(
        min_length=1, max_length=MAX_REASON_CHARS, description="коротко, откуда лечение"
    )


class DamageArgs(ToolArgs):
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа отряда")
    dice: str = Field(
        max_length=MODEL_DICE_MAX_CHARS,
        description="запись костей урона, например 2d6 (до 20 костей); числа без костей нельзя",
    )
    damage_type: DamageType = Field(description="тип урона")
    reason: str = Field(
        min_length=1, max_length=MAX_REASON_CHARS, description="коротко, откуда урон"
    )


def _character_to_hurt_or_heal(ctx: ToolContext, character_id: str, dice_text: str) -> Character:
    """Проверки до первого броска: персонаж жив, боя нет, запись костей в пределах."""
    character = find_or_error(ctx.state.party, "персонажа отряда", character_id)
    if ctx.state.combat is not None:
        raise ToolError(
            "В бою урон и лечение идут через attack, cast_spell и combat_action; этот "
            "инструмент для ловушек и событий вне боя."
        )
    if is_dead(character):
        raise ToolError(f"Персонаж {character.id!r} мёртв: урон и лечение ему не нужны.")
    try:
        parse_model_dice(dice_text)
    except DiceSyntaxError as exc:
        raise ToolError(f"Неверная запись костей: {exc}") from None
    return character


def heal(ctx: ToolContext, args: HealArgs) -> dict[str, Any]:
    character = _character_to_hurt_or_heal(ctx, args.character_id, args.dice)
    rolled = roll(args.dice, rng=ctx.rng, reason=f"Лечение: {args.reason}")
    try:
        healed, events = engine.heal_target(ctx.state, character.id, max(0, rolled.total))
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    roll_event = RollEvent(actor_id=character.id, label=f"Лечение: {args.reason}", roll=rolled)
    commit_state(ctx, healed, [roll_event, *events])
    gained = sum(e.amount for e in events if isinstance(e, HealEvent))
    return {
        "character": character.name,
        "rolled": rolled.total,
        "healed": gained,
        "hp": ctx.state.party[character.id].hp,
    }


def damage(ctx: ToolContext, args: DamageArgs) -> dict[str, Any]:
    character = _character_to_hurt_or_heal(ctx, args.character_id, args.dice)
    label = f"Урон: {args.reason}"
    rolled = roll(args.dice, rng=ctx.rng, reason=label)
    try:
        hurt, events = engine.apply_damage(
            ctx.state,
            character.id,
            max(0, rolled.total),
            args.damage_type,
            rng=ctx.rng,
            content=ctx.content,
        )
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    commit_state(ctx, hurt, [RollEvent(actor_id=character.id, label=label, roll=rolled), *events])
    dealt = sum(e.amount for e in events if isinstance(e, DamageEvent))
    return {
        "character": character.name,
        "rolled": rolled.total,
        "damage": dealt,
        "type": args.damage_type,
        "hp": ctx.state.party[character.id].hp,
        "events": event_lines(events),
    }


# --- apply_effect ---


class ApplyEffectArgs(ToolArgs):
    target_id: str = Field(
        max_length=MAX_ID_CHARS, description="id персонажа отряда или бойца идущего боя"
    )
    condition: ConditionId = Field(description="состояние SRD")
    add: bool = Field(default=True, description="true — наложить, false — снять")
    rounds: int | None = Field(
        default=None,
        ge=1,
        le=EXTERNAL_CONDITION_MAX_ROUNDS,
        description=(
            "сколько раундов держится (до 100, то есть 10 минут), только в бою: вне боя раунды "
            "не идут. Без значения — без срока"
        ),
    )


def _target(ctx: ToolContext, target_id: str) -> Character | Combatant:
    target = ctx.state.party.get(target_id)
    if target is None and ctx.state.combat is not None:
        target = ctx.state.combat.combatants.get(target_id)
    if target is None:
        raise ToolError(f"Нет цели {target_id!r}: нужен персонаж отряда или боец идущего боя.")
    return target


def apply_effect(ctx: ToolContext, args: ApplyEffectArgs) -> dict[str, Any]:
    if not args.add and args.rounds is not None:
        raise ToolError("rounds нужен только при add=true: снимается состояние целиком.")
    if args.rounds is not None and ctx.state.combat is None:
        # Срок тикает только в `end_turn`: вне боя он не истёк бы до конца следующего боя.
        raise ToolError(
            "Вне боя раунды не идут: наложите состояние без rounds и снимите его позже (add=false)."
        )
    target = _target(ctx, args.target_id)
    content = combat_content(ctx)
    try:
        if args.add:
            # Источник пустой, Сл и спасброска нет: их у внешнего состояния не бывает.
            condition = ActiveCondition(id=args.condition, rounds_left=args.rounds, source="")
            changed, events = engine.apply_condition(
                ctx.state, target.id, condition, content=content
            )
        else:
            changed, events = engine.remove_condition(
                ctx.state, target.id, args.condition, content=content
            )
    except CombatError as exc:
        raise ToolError(str(exc)) from None
    commit_state(ctx, changed, events)
    return {
        "target": target.name,
        "condition": args.condition,
        "added": args.add,
        "rounds": args.rounds,
    }


NARRATIVE_TOOLS = (
    ToolSpec(
        name="private_message",
        description=(
            "Личное сообщение одному игроку: только он увидит его на своём экране. Для тайных "
            "ощущений и находок персонажа; остальным об этом расскажите, только если персонаж "
            "сам захочет."
        ),
        args_model=PrivateMessageArgs,
        handler=private_message,
    ),
    ToolSpec(
        name="chronicle",
        description=(
            "Запись в летопись игры: одна-две строки о том, что произошло в этом ходу. "
            "Обязательна в конце каждого хода."
        ),
        args_model=ChronicleArgs,
        handler=chronicle,
    ),
    ToolSpec(
        name="heal",
        description=(
            "Лечение персонажа вне боя из кости (ручей, привал-находка, добрый жрец): запись "
            "костей вида 2d4+2, число без костей нельзя. Бросок и итог считает движок. В бою "
            "лечат заклинания и действия."
        ),
        args_model=HealArgs,
        handler=heal,
        author_fields=("character_id",),
    ),
    ToolSpec(
        name="damage",
        description=(
            "Урон персонажу вне боя из кости (ловушка, обвал, падение): запись костей вида 2d6 и "
            "тип урона SRD, число без костей нельзя. Бросок, сопротивления и падение до 0 HP "
            "считает движок. В бою бьют attack, cast_spell и monster_action."
        ),
        args_model=DamageArgs,
        handler=damage,
        author_fields=("character_id",),
    ),
    ToolSpec(
        name="apply_effect",
        description=(
            "Наложить или снять состояние SRD (сбит с ног, напуган, отравлен...) по ходу "
            "рассказа: на персонажа отряда или бойца боя. Срок rounds (до 100 раундов) задаётся "
            "только в бою; вне боя и без срока состояние держится, пока вы его не снимете "
            "(add=false). Сл и спасброска у "
            "такого состояния нет; состояния, которые ведёт движок (0 HP, скрытность, "
            "заклинания), снять нельзя."
        ),
        args_model=ApplyEffectArgs,
        handler=apply_effect,
    ),
)
