"""Инструменты мира: переход, находки, предметы, флаги, отношение NPC, время, короткий отдых и
дневник отряда.

План B4.6, спека §5.7 и §8.3; дневник — спека DND-2 §12 и дополнение контракта B8 §4. Каждый
инструмент меняет рабочую копию состояния `ctx.state` и добавляет события в `ctx.events`;
результат — короткий словарь для модели. Переход, найденная улика и полученный героем предмет
пишутся в дневник сами (`journal`), остальное мастер записывает через `journal_add`.

Правило обработчиков (см. `registry`): сначала проверка всего, что пришло от модели, и только
потом первое изменение состояния или бросок. Поэтому `ToolError` ничего не оставляет за собой, а
ход мастера продолжается с прежним состоянием. Замки и скрытые выходы считает `lore`: одно правило
«открыто / заперто / скрыто» и для чтения лора, и для действий. Что находят и кому отдают,
решает мастер, а инструмент следит за законностью и записывает итог в состояние.
"""

import unicodedata
from collections.abc import Mapping
from typing import Annotated, Any, Literal, get_args

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints

from dnd_engine import journal, rest
from dnd_engine.adventure.schema import Adventure, Clue, Exit, Feature, Location, RevealRef
from dnd_engine.adventure.validator import ENGINE_FLAG_PREFIXES
from dnd_engine.character import Character
from dnd_engine.events import (
    AttitudeEvent,
    FlagEvent,
    ItemEvent,
    MoveEvent,
    RestEvent,
    RevealEvent,
)
from dnd_engine.journal import MAX_NOTE_CHARS, JournalError
from dnd_engine.modules.base import MAX_SHORT_RESTS_PER_LONG
from dnd_engine.state import GameState, GameTime
from dnd_engine.tools.lore import (
    MAX_ID_CHARS,
    MAX_REF_CHARS,
    closed_place_with_item,
    exit_status,
    is_locked,
    item_holder,
)
from dnd_engine.tools.registry import ToolArgs, ToolContext, ToolError, ToolSpec, find_or_error

DayPart = Literal["morning", "day", "evening", "night"]
DAY_PARTS: tuple[str, ...] = get_args(DayPart)  # порядок суток; после «night» идёт новый день

ATTITUDE_MIN, ATTITUDE_MAX = -3, 3  # диапазон `WorldState.npc_attitude`
MAX_ATTITUDE_STEP = 2
MAX_FLAG_NAME_CHARS, MAX_FLAG_TEXT_CHARS, FLAG_INT_LIMIT = 64, 200, 1_000_000
MAX_SPEND_ENTRIES = 12  # записей в `spend`: в отряде до шести героев, с запасом
MAX_HIT_DICE_PER_REST = 20

_REF_PATTERN = r"^(feature|exit|clue):[a-z0-9][a-z0-9_-]{0,63}$"


# --- поиск и общие проверки ---


def _here(ctx: ToolContext) -> Location:
    return ctx.adventure.locations[ctx.state.location_id]


def _npc_location(state: GameState, adventure: Adventure, npc_id: str) -> str | None:
    """Где сейчас NPC: переопределение мира, иначе локация из приключения; неизвестный — нигде."""
    npc = adventure.npcs.get(npc_id)
    if npc is None:
        return None
    return state.world.npc_location.get(npc.id, npc.location_id)


def _exits_text(location: Location) -> str:
    return ", ".join(f"{exit_.id} → {exit_.to}" for exit_ in location.exits) or "нет"


# --- move_party ---


class MovePartyArgs(ToolArgs):
    location_id: str = Field(max_length=MAX_ID_CHARS, description="id локации, куда идёт отряд")
    via_exit: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id выхода текущей локации; нужен, если в эту локацию ведёт несколько выходов",
    )


def _closed_exit_error(exit_: Exit, status: str) -> ToolError:
    if status == "hidden":
        return ToolError(
            f"Выход {exit_.id!r} в {exit_.to!r} скрыт: отряд его ещё не нашёл "
            f"(Сл поиска {exit_.hidden_dc}). Когда найдёт, вызовите reveal с ref "
            f'"exit:{exit_.id}".'
        )
    return ToolError(
        f"Выход {exit_.id!r} в {exit_.to!r} заперт: нужен {exit_.locked_by}. Пока предмет не у "
        "персонажа отряда (или флаг не выставлен), пройти нельзя."
    )


def _exit_to_walk(ctx: ToolContext, here: Location, args: MovePartyArgs) -> Exit:
    """Выход, которым пойдёт отряд. Нет выхода, он скрыт или заперт — `ToolError`."""
    candidates = [exit_ for exit_ in here.exits if exit_.to == args.location_id]
    if args.via_exit is not None:
        chosen = next((exit_ for exit_ in here.exits if exit_.id == args.via_exit), None)
        if chosen is None:
            raise ToolError(
                f"В локации {here.id!r} нет выхода {args.via_exit!r}. Выходы: {_exits_text(here)}"
            )
        if chosen.to != args.location_id:
            raise ToolError(
                f"Выход {chosen.id!r} ведёт в {chosen.to!r}, а не в {args.location_id!r}."
            )
        candidates = [chosen]
    if not candidates:
        raise ToolError(
            f"Из локации {here.id!r} нет выхода в {args.location_id!r}. Выходы: {_exits_text(here)}"
        )
    statuses = [(exit_, exit_status(ctx.state, ctx.adventure, exit_)) for exit_ in candidates]
    for exit_, status in statuses:
        if status == "open":
            return exit_
    raise _closed_exit_error(*statuses[0])


def move_party(ctx: ToolContext, args: MovePartyArgs) -> dict[str, Any]:
    state = ctx.state
    if state.combat is not None:
        raise ToolError("В бою отряд не переходит в другую локацию: сначала закончите бой.")
    destination = find_or_error(ctx.adventure.locations, "локации", args.location_id)
    here = _here(ctx)
    exit_ = _exit_to_walk(ctx, here, args)

    visited = state.world.visited_locations
    first_visit = destination.id not in visited
    for place in (here.id, destination.id):  # отряд побывал и там, откуда ушёл
        if place not in visited:
            visited.append(place)
    state.location_id = destination.id
    ctx.events.append(MoveEvent(location_id=destination.id))
    journal.visit(state, ctx.adventure, destination.id)
    return {
        "location_id": destination.id,
        "name": destination.name,
        "via_exit": exit_.id,
        "first_visit": first_visit,
    }


# --- reveal ---


class RevealArgs(ToolArgs):
    ref: str = Field(
        pattern=_REF_PATTERN,
        max_length=80,
        description=(
            'Что найдено: "feature:<id>" — интересное место, "exit:<id>" — скрытый выход, '
            '"clue:<id>" — улика. Всё это должно быть в текущей локации (улика — у NPC здесь '
            "или в предмете отряда)."
        ),
    )
    for_hero: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description=(
            "id героя, чья это находка: если его ход тайный, найденные улики в дневнике увидит "
            "только он, если открытый — весь отряд. В раунде с тайным ходом обязательно для "
            "каждой находки с уликой"
        ),
    )


def _locked_feature_error(feature: Feature) -> ToolError:
    return ToolError(
        f"Интересное место {feature.id!r} заперто: нужен {feature.locked_by}. Пока предмет не у "
        "персонажа отряда (или флаг не выставлен), его нельзя открыть."
    )


def _item_inside_error(ctx: ToolContext, item_id: str, place: Feature) -> ToolError:
    """Предмет лежит внутри ещё не раскрытого места: путь к нему идёт через `reveal`."""
    message = (
        f"Предмет {item_id!r} лежит внутри места {place.id!r}, которое ещё не раскрыто: сначала "
        f'вызовите reveal с ref "feature:{place.id}".'
    )
    if is_locked(ctx.state, ctx.adventure, place.locked_by):
        message += f" Место заперто: нужен {place.locked_by}."
    return ToolError(message)


def _feature_to_reveal(ctx: ToolContext, here: Location, feature_id: str) -> Feature:
    feature = next((f for f in here.features if f.id == feature_id), None)
    if feature is None:
        known = ", ".join(f.id for f in here.features) or "нет"
        raise ToolError(
            f"В локации {here.id!r} нет интересного места {feature_id!r}. Здесь: {known}"
        )
    if is_locked(ctx.state, ctx.adventure, feature.locked_by):
        raise _locked_feature_error(feature)
    return feature


def _hidden_exit_to_reveal(here: Location, exit_id: str) -> None:
    exit_ = next((e for e in here.exits if e.id == exit_id), None)
    if exit_ is None:
        raise ToolError(
            f"В локации {here.id!r} нет выхода {exit_id!r}. Выходы: {_exits_text(here)}"
        )
    if exit_.hidden_dc is None:
        raise ToolError(f"Выход {exit_id!r} не скрыт: отряд видит его и так, раскрывать нечего.")


def _clues_of(adventure: Adventure) -> dict[str, Clue]:
    return {clue.id: clue for rev in adventure.revelations.values() for clue in rev.clues}


def _clue_to_reveal(ctx: ToolContext, here: Location, clue_id: str) -> None:
    """Улику можно найти, если её место рядом: особенность здесь, NPC здесь или предмет у отряда."""
    clue = find_or_error(_clues_of(ctx.adventure), "улики", clue_id)
    kind, _, target = clue.where.partition(":")
    state = ctx.state
    in_reach = False
    if kind == "feature":
        feature = next((f for f in here.features if f.id == target), None)
        if feature is not None and is_locked(state, ctx.adventure, feature.locked_by):
            raise _locked_feature_error(feature)
        in_reach = feature is not None
    elif kind == "npc":
        in_reach = _npc_location(state, ctx.adventure, target) == here.id
    elif kind == "item":
        if place := closed_place_with_item(state, ctx.adventure, target):
            raise _item_inside_error(ctx, target, place)
        holder_kind, _, holder_id = item_holder(state, ctx.adventure, target).partition(":")
        in_reach = holder_kind == "character" and holder_id in state.party
    if not in_reach:
        raise ToolError(
            f"Улика {clue_id!r} лежит в другом месте ({clue.where}): сейчас отряд в локации "
            f"{here.id!r}, и этого места, NPC или предмета отряда здесь нет."
        )


def _clues_to_find(ctx: ToolContext, ref: str, effects: tuple[RevealRef, ...]) -> list[str]:
    """id улик, которые отметит находка `ref`: она сама или улики её `reveals`, ещё не найденные
    и известные приключению, — в том порядке, в каком их отметит `reveal`."""
    clues = _clues_of(ctx.adventure)
    refs = dict.fromkeys([ref, *(f"clue:{e.id}" for e in effects if e.kind == "clue")])
    return [
        clue_ref.partition(":")[2]
        for clue_ref in refs
        if clue_ref.startswith("clue:")
        and clue_ref not in ctx.state.world.revealed
        and clue_ref.partition(":")[2] in clues
    ]


def _finder_audience(ctx: ToolContext, for_hero: str | None) -> str:
    """Кому видна запись найденной улики (дополнение §10, поправка §12).

    Герой находки назван — видит он один, если его ход тайный, иначе весь отряд. Не назван: вне
    тайного раунда — весь отряд; в раунде с тайным ходом — `ToolError`. `secret_actors` — тайные
    авторы всего раунда, а не этого вызова: чья находка, знает только мастер, а дневник — её
    единственный видимый след, поэтому ни открыть тайную, ни отдать тайному открытую нельзя."""
    if for_hero is not None:
        return ctx.audience_of([for_hero])
    if ctx.secret_actors & set(ctx.state.party):
        raise ToolError(
            "В раунде есть тайный ход: назовите в for_hero героя находки; находка открытого "
            "героя останется общей, тайного — только его."
        )
    return journal.ALL


def _mark_revealed(ctx: ToolContext, ref: str) -> bool:
    """Отмечает находку и пишет событие; уже найденная второй раз не отмечается."""
    if ref in ctx.state.world.revealed:
        return False
    ctx.state.world.revealed.append(ref)
    ctx.events.append(RevealEvent(ref=ref))
    return True


def _apply_effect(ctx: ToolContext, effect: RevealRef, result: dict[str, Any]) -> None:
    """Одно следствие находки (`Feature.reveals`) с записью в `result` для модели."""
    if effect.kind in ("clue", "exit"):
        ref = f"{effect.kind}:{effect.id}"
        if _mark_revealed(ctx, ref):
            result["revealed"].append(ref)
    elif effect.kind == "flag":
        value = True if effect.value is None else effect.value
        ctx.state.world.flags[effect.id] = value
        ctx.events.append(FlagEvent(name=effect.id, value=value))
        result.setdefault("flags", {})[effect.id] = value
    else:  # item: предмет найден; кому его отдать, решает мастер через give_item
        item = ctx.adventure.items[effect.id]
        result.setdefault("items", []).append({"id": item.id, "name": item.name})


def reveal(ctx: ToolContext, args: RevealArgs) -> dict[str, Any]:
    for_hero = None
    if args.for_hero is not None:
        for_hero = find_or_error(ctx.state.party, "героя отряда", args.for_hero).id
    if args.ref in ctx.state.world.revealed:
        return {"ref": args.ref, "new": False}  # уже найдено, повтор ничего не меняет
    kind, _, target = args.ref.partition(":")
    here = _here(ctx)
    effects: tuple[RevealRef, ...] = ()
    if kind == "feature":
        effects = _feature_to_reveal(ctx, here, target).reveals
    elif kind == "exit":
        _hidden_exit_to_reveal(here, target)
    else:
        _clue_to_reveal(ctx, here, target)
    found = _clues_to_find(ctx, args.ref, effects)
    # Адресат записи решается до первого изменения: отказ ничего не оставляет за собой.
    audience = _finder_audience(ctx, for_hero) if found else journal.ALL

    _mark_revealed(ctx, args.ref)
    result: dict[str, Any] = {"ref": args.ref, "new": True, "revealed": [args.ref]}
    for effect in effects:
        _apply_effect(ctx, effect, result)
    for clue_id in found:
        journal.record(ctx.state, ctx.adventure, f"clue:{clue_id}", audience=audience)
    if found:
        clues = _clues_of(ctx.adventure)
        result["clues"] = [
            {"id": clue_id, "description": clues[clue_id].description} for clue_id in found
        ]
    return result


# --- give_item и take_item ---


class GiveItemArgs(ToolArgs):
    item_id: str = Field(max_length=MAX_ID_CHARS, description="id предмета приключения")
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа, кто получает")


class TakeItemArgs(ToolArgs):
    item_id: str = Field(max_length=MAX_ID_CHARS, description="id предмета приключения")
    character_id: str = Field(max_length=MAX_ID_CHARS, description="id персонажа, у кого он сейчас")
    to: Literal["gone", "location"] = Field(
        description='"gone" — предмет ушёл из игры, "location" — оставлен в текущей локации'
    )


def _holder_is_within_reach(state: GameState, adventure: Adventure, holder: str) -> bool:
    """Предмет можно передать, если он в текущей локации, у NPC здесь или у персонажа отряда."""
    kind, _, holder_id = holder.partition(":")
    if kind == "location":
        return holder_id == state.location_id
    if kind == "npc":
        return _npc_location(state, adventure, holder_id) == state.location_id
    return kind == "character" and holder_id in state.party


def give_item(ctx: ToolContext, args: GiveItemArgs) -> dict[str, Any]:
    state, adventure = ctx.state, ctx.adventure
    item = find_or_error(adventure.items, "предмета", args.item_id)
    character = find_or_error(state.party, "персонажа отряда", args.character_id)
    holder, new_holder = item_holder(state, adventure, item.id), f"character:{character.id}"
    if holder == new_holder:
        raise ToolError(f"Предмет {item.id!r} уже у персонажа {character.id!r}.")
    if not _holder_is_within_reach(state, adventure, holder):
        raise ToolError(
            f"Предмет {item.id!r} сейчас у {holder!r}: передать его из локации "
            f"{state.location_id!r} нельзя (он должен быть здесь, у NPC здесь или у отряда)."
        )
    if place := closed_place_with_item(state, adventure, item.id):
        raise _item_inside_error(ctx, item.id, place)
    state.world.item_holders[item.id] = new_holder
    ctx.events.append(ItemEvent(item_id=item.id, holder_from=holder, holder_to=new_holder))
    # Адресат записи — как у строки ленты о передаче: тайный герой из участников видит её один.
    heroes = [h.partition(":")[2] for h in (holder, new_holder) if h.startswith("character:")]
    journal.record(state, adventure, f"item:{item.id}", audience=ctx.audience_of(heroes))
    return {"item": item.name, "from": holder, "to": new_holder}


def take_item(ctx: ToolContext, args: TakeItemArgs) -> dict[str, Any]:
    state, adventure = ctx.state, ctx.adventure
    item = find_or_error(adventure.items, "предмета", args.item_id)
    character = find_or_error(state.party, "персонажа отряда", args.character_id)
    holder = item_holder(state, adventure, item.id)
    if holder != f"character:{character.id}":
        raise ToolError(f"Предмета {item.id!r} нет у персонажа {character.id!r}: он у {holder!r}.")
    new_holder = "gone" if args.to == "gone" else f"location:{state.location_id}"
    state.world.item_holders[item.id] = new_holder
    ctx.events.append(ItemEvent(item_id=item.id, holder_from=holder, holder_to=new_holder))
    return {"item": item.name, "from": holder, "to": new_holder}


# --- set_flag ---

FlagName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FLAG_NAME_CHARS)
]
FlagValue = (
    bool
    | Annotated[int, Field(ge=-FLAG_INT_LIMIT, le=FLAG_INT_LIMIT)]
    | Annotated[str, Field(max_length=MAX_FLAG_TEXT_CHARS)]
)


class SetFlagArgs(ToolArgs):
    name: FlagName = Field(description="имя флага сюжета, например met_marta")
    value: FlagValue = Field(
        default=True, description="значение: true/false, число или короткий текст"
    )


def set_flag(ctx: ToolContext, args: SetFlagArgs) -> dict[str, Any]:
    if args.name.startswith(ENGINE_FLAG_PREFIXES):
        raise ToolError(
            f"Флаги с префиксом {', '.join(ENGINE_FLAG_PREFIXES)} ведёт движок (ход боя): "
            "выберите другое имя."
        )
    ctx.state.world.flags[args.name] = args.value
    ctx.events.append(FlagEvent(name=args.name, value=args.value))
    return {"name": args.name, "value": args.value}


# --- change_attitude ---


class ChangeAttitudeArgs(ToolArgs):
    npc_id: str = Field(max_length=MAX_ID_CHARS, description="id NPC")
    delta: int = Field(
        ge=-MAX_ATTITUDE_STEP,
        le=MAX_ATTITUDE_STEP,
        description="на сколько ступеней меняется отношение к отряду; итог всегда от -3 до 3",
    )


def change_attitude(ctx: ToolContext, args: ChangeAttitudeArgs) -> dict[str, Any]:
    npc = find_or_error(ctx.adventure.npcs, "NPC", args.npc_id)
    current = ctx.state.world.npc_attitude.get(npc.id, npc.attitude)
    attitude = max(ATTITUDE_MIN, min(ATTITUDE_MAX, current + args.delta))
    ctx.state.world.npc_attitude[npc.id] = attitude
    ctx.events.append(AttitudeEvent(npc_id=npc.id, value=attitude))
    return {"npc": npc.name, "attitude": attitude}


# --- advance_time ---


class AdvanceTimeArgs(ToolArgs):
    to: DayPart = Field(
        description="часть суток: morning, day, evening или night; время идёт только вперёд"
    )


def advance_time(ctx: ToolContext, args: AdvanceTimeArgs) -> dict[str, Any]:
    now = ctx.state.time
    before, after = DAY_PARTS.index(now.part), DAY_PARTS.index(args.to)
    if after == before:
        raise ToolError(
            f"Сейчас уже {now.part}: время идёт только вперёд, выберите другую часть суток."
        )
    new_day = after < before  # дошли до нужной части, пройдя через ночь
    ctx.state.time = GameTime(day=now.day + int(new_day), part=args.to)
    return {"day": ctx.state.time.day, "part": args.to, "new_day": new_day}


# --- short_rest ---


class ShortRestArgs(ToolArgs):
    spend: dict[
        Annotated[str, Field(max_length=MAX_ID_CHARS)],
        Annotated[int, Field(ge=0, le=MAX_HIT_DICE_PER_REST)],
    ] = Field(
        default_factory=dict,
        max_length=MAX_SPEND_ENTRIES,
        description=(
            "сколько костей хитов тратит каждый герой: {id персонажа: число}. Кто не назван, "
            "кости не тратит; отдыхает весь отряд"
        ),
    )


def _check_short_rest(ctx: ToolContext, spend: Mapping[str, int]) -> None:
    state = ctx.state
    if state.combat is not None:
        raise ToolError("В бою отдохнуть нельзя: сначала закончите бой.")
    limit = ctx.rules.flags.get(MAX_SHORT_RESTS_PER_LONG)
    if limit is not None and state.short_rests_since_long >= limit:
        raise ToolError(
            f"Между длинными отдыхами допустимо не больше {limit} коротких, а прошло уже "
            f"{state.short_rests_since_long}: нужен длинный отдых (привал)."
        )
    for character_id, dice in spend.items():
        character = find_or_error(state.party, "персонажа отряда", character_id)
        if dice > character.hit_dice:
            raise ToolError(
                f"У персонажа {character_id!r} костей хитов {character.hit_dice}, "
                f"а вы просите потратить {dice}."
            )


def short_rest_party(ctx: ToolContext, args: ShortRestArgs) -> dict[str, Any]:
    _check_short_rest(ctx, args.spend)
    state = ctx.state
    rested: dict[str, Character] = {}
    outcomes = []
    for character_id, character in state.party.items():
        rested[character_id], outcome = rest.short_rest(
            character,
            spend_hit_dice=args.spend.get(character_id, 0),
            rng=ctx.rng,
            content=ctx.content,
        )
        outcomes.append(outcome)
    state.party.update(rested)
    state.short_rests_since_long += 1
    ctx.events.append(RestEvent(rest="short", outcomes=outcomes))
    return {
        "rest": "short",
        "short_rests_since_long": state.short_rests_since_long,
        "characters": [
            {
                "id": outcome.character_id,
                "name": rested[outcome.character_id].name,
                "hp_gained": outcome.hp_gained,
                "hit_dice_spent": outcome.hit_dice_spent,
                "hp": rested[outcome.character_id].hp,
                "restored": outcome.restored,
            }
            for outcome in outcomes
        ],
    }


# --- journal_add ---


_SOFT_HYPHEN = "\u00ad"


def _plain_spaces(note: Any) -> Any:
    # Обычная типографика — не ошибка модели: неразрывные и широкие пробелы (категория Zs)
    # становятся обычным пробелом, мягкий перенос убирается. Длина считается уже после этого.
    if not isinstance(note, str):
        return note
    return "".join(
        " " if unicodedata.category(char) == "Zs" else char for char in note if char != _SOFT_HYPHEN
    )


def _printable_note(note: str) -> str:
    # Слова мастера уходят игрокам: управляющие знаки, знаки нулевой ширины и разворота строки
    # (escape, U+200B, U+202E, U+2066–2069) исказили бы экран или спрятали текст.
    if not all(char.isprintable() or char == "\n" for char in note):
        raise ValueError("только печатаемый текст и переносы строк")
    return note


# Порядок важен: сначала типографика (`_plain_spaces`), потом обрезка и длина, потом печатаемость.
JournalNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NOTE_CHARS),
    BeforeValidator(_plain_spaces),
    AfterValidator(_printable_note),
]


class JournalAddArgs(ToolArgs):
    ref: str = Field(
        max_length=MAX_REF_CHARS,
        description=(
            "о чём запись: deity:<id>, faction:<id>, npc:<id>, region:<id>, rumor:<номер слуха "
            "в мире с 1>, world (история мира, только с note); можно и location:<id>, "
            "item:<id> и clue:<id> — только найденная улика"
        ),
    )
    note: JournalNote | None = Field(
        default=None,
        description="что именно узнали герои, своими словами и без тайн; дописывается абзацем",
    )
    for_hero: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id героя, если узнал только он (тайно); без поля запись видит весь отряд",
    )


def journal_add(ctx: ToolContext, args: JournalAddArgs) -> dict[str, Any]:
    if args.ref == journal.INTRO_REF:
        raise ToolError("Запись «Главное» дневник делает сам в начале игры: её не дополняют.")
    revealed = ctx.state.world.revealed
    if args.ref.startswith("clue:") and args.ref not in revealed:
        # Улика попадает в дневник только найденной: иначе модель раскрыла бы её без поиска.
        found = [ref for ref in revealed if ref.startswith("clue:")]
        raise ToolError(
            f"Улика {args.ref!r} ещё не найдена: в дневник пишут только найденные улики "
            f"(сначала reveal). Найдены: {', '.join(found) or 'нет'}"
        )
    audience = journal.ALL
    if args.for_hero is not None:
        audience = find_or_error(ctx.state.party, "героя отряда", args.for_hero).id
    try:
        entry, new = journal.write(
            ctx.state, ctx.adventure, args.ref, audience=audience, note=args.note
        )
    except JournalError as exc:
        raise ToolError(str(exc)) from exc
    return {"entry_id": entry.id, "new": new, "revision": entry.revision}


WORLD_TOOLS = (
    ToolSpec(
        name="move_party",
        description=(
            "Перевести отряд в соседнюю локацию через выход. Выход должен вести в эту локацию, "
            "быть найденным (не hidden) и не запертым (locked: нужен ключ у кого-то из отряда "
            "или флаг). Если в локацию ведёт несколько выходов, назовите нужный в via_exit. "
            "В бою переход невозможен."
        ),
        args_model=MovePartyArgs,
        handler=move_party,
    ),
    ToolSpec(
        name="reveal",
        description=(
            "Отметить находку, когда отряд её нашёл: интересное место (feature:<id>), скрытый "
            "выход (exit:<id>) или улику (clue:<id>) в текущей локации. Находка раскрывает то, "
            "что с ней связано в приключении (улики, выходы, предметы, флаги). Запертое место "
            "нельзя раскрыть без ключа. Повторный вызов ничего не меняет. Найденные улики "
            "пишутся в дневник отряда; в раунде с тайным ходом назовите героя находки в for_hero: "
            "находку тайного героя увидит только он, открытого — весь отряд."
        ),
        args_model=RevealArgs,
        handler=reveal,
        author_fields=("for_hero",),
    ),
    ToolSpec(
        name="give_item",
        description=(
            "Отдать предмет персонажу. Предмет должен лежать в текущей локации, быть у NPC "
            "здесь или у другого персонажа отряда. Предмет внутри ещё не раскрытого места "
            "(например, в запертом сундуке) сначала достают через reveal."
        ),
        args_model=GiveItemArgs,
        handler=give_item,
    ),
    ToolSpec(
        name="take_item",
        description=(
            "Забрать предмет у персонажа: он уходит из игры (gone) или остаётся в текущей "
            "локации (location)."
        ),
        args_model=TakeItemArgs,
        handler=take_item,
    ),
    ToolSpec(
        name="set_flag",
        description=(
            "Записать флаг сюжета (true/false, число или короткий текст). Флагами открываются "
            "замки, помеченные flag:<имя>. Флаги encounter:* ведёт движок, их задать нельзя."
        ),
        args_model=SetFlagArgs,
        handler=set_flag,
    ),
    ToolSpec(
        name="change_attitude",
        description=(
            "Изменить отношение NPC к отряду на 1-2 ступени вверх или вниз; итог всегда от -3 "
            "(враждебен) до 3 (предан)."
        ),
        args_model=ChangeAttitudeArgs,
        handler=change_attitude,
    ),
    ToolSpec(
        name="advance_time",
        description=(
            "Сдвинуть время вперёд до части суток (morning, day, evening, night). Если для этого "
            "нужно пройти через ночь, наступает следующий день."
        ),
        args_model=AdvanceTimeArgs,
        handler=advance_time,
    ),
    ToolSpec(
        name="short_rest",
        description=(
            "Короткий отдых всего отряда: герои тратят кости хитов (spend), возвращаются часть "
            "ресурсов класса. Если правила приключения ограничивают число коротких отдыхов "
            "между длинными, лишний отдых невозможен. В бою отдохнуть нельзя."
        ),
        args_model=ShortRestArgs,
        handler=short_rest_party,
    ),
    ToolSpec(
        name="journal_add",
        description=(
            "Записать в дневник отряда то, что узнали герои: о боге, фракции, человеке, крае, "
            "слухе или истории мира. note — что именно узнали, своими словами и без тайн; "
            "повторный вызов с note дописывает её новым абзацем, без note ничего не меняет. "
            "for_hero — если узнал один герой (тайно): запись увидит только он; без for_hero "
            "запись видит весь отряд. Места, улики и предметы записываются сами при переходе, "
            "находке и передаче героям; тайной находкой можно поделиться со всем отрядом, "
            "записав найденную улику (clue:<id>) без for_hero."
        ),
        args_model=JournalAddArgs,
        handler=journal_add,
    ),
)
