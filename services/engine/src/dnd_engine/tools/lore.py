"""Чтение лора: `get_location`, `get_npc`, `get_item`, `search_lore` (план B4.5, спека §8.3).

Инструменты только читают приключение и состояние мира: состояние не меняют и событий не создают.
Результат — короткий словарь, пригодный для JSON. Это инструменты мастера, поэтому в ответе есть
заметки мастера и секреты NPC: игрокам их показывает только рассказ модели.

Расчёт замков, скрытых выходов и закрытых мест (`is_locked`, `exit_status`,
`closed_place_with_item`) вынесен отдельно: тот же расчёт нужен инструментам перехода, раскрытия
находок и передачи предметов, и правило «открыто / заперто / скрыто / внутри закрытого места»
должно быть одним.
"""

from collections.abc import Iterator
from typing import Any, Literal

from pydantic import Field

from dnd_engine.adventure.schema import Adventure, Exit, Feature, Location, Npc
from dnd_engine.state import GameState
from dnd_engine.tools.registry import ToolArgs, ToolContext, ToolError, ToolSpec, find_or_error

ExitStatus = Literal["open", "locked", "hidden"]

MAX_ID_CHARS = 64  # предел id сущности приключения (`schema.ID_PATTERN`)
MAX_QUERY_CHARS = 200
DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT = 5, 20
# Короткие слова («в», «на», «и») совпадают почти везде и портят порядок результатов.
MIN_WORD_CHARS = 3
SNIPPET_BEFORE_CHARS, SNIPPET_CHARS = 40, 120
_PUNCTUATION = ".,;:!?\"'«»()[]—–-"


# --- замки и скрытые выходы ---


def item_holder(state: GameState, adventure: Adventure, item_id: str) -> str:
    """Текущий держатель предмета: переопределение мира, иначе держатель из приключения.

    Предмет, которого нет в приключении, считается ушедшим (`gone`): ни у кого его нет."""
    if item_id in state.world.item_holders:
        return state.world.item_holders[item_id]
    item = adventure.items.get(item_id)
    return item.holder if item is not None else "gone"


def closed_place_with_item(state: GameState, adventure: Adventure, item_id: str) -> Feature | None:
    """Закрытое место, внутри которого лежит предмет, или `None`.

    Предмет лежит внутри, пока его держатель исходный (id нет в `world.item_holders`): в локации-
    держателе есть место, в чьих `reveals` назван этот предмет, и оно ещё не раскрыто (`reveal`
    пишет `feature:<id>` в `world.revealed`). Замок места отдельно не смотрим: запертое место не
    раскрыть, пока не снят замок. Брошенный или переданный предмет уже снаружи."""
    item = adventure.items.get(item_id)
    if item is None or item_id in state.world.item_holders:
        return None
    kind, _, location_id = item.holder.partition(":")
    location = adventure.locations.get(location_id) if kind == "location" else None
    if location is None:
        return None
    return next(
        (
            feature
            for feature in location.features
            if f"feature:{feature.id}" not in state.world.revealed
            and any(reveal.kind == "item" and reveal.id == item_id for reveal in feature.reveals)
        ),
        None,
    )


def is_locked(state: GameState, adventure: Adventure, locked_by: str | None) -> bool:
    """Заперт ли замок `locked_by` ("item:<id>" | "flag:<имя>"); без замка — нет.

    Замок-предмет открыт, пока предмет у персонажа отряда. Замок-флаг открыт, когда флаг выставлен
    (значение не пустое и не ложное). Неизвестный вид замка остаётся запертым."""
    if locked_by is None:
        return False
    kind, _, name = locked_by.partition(":")
    if kind == "item":
        holder_kind, _, holder_id = item_holder(state, adventure, name).partition(":")
        return not (holder_kind == "character" and holder_id in state.party)
    if kind == "flag":
        return not state.world.flags.get(name)
    return True


def exit_status(state: GameState, adventure: Adventure, exit_: Exit) -> ExitStatus:
    """Скрытый выход открывается `reveal` ("exit:<id>"), запертый — ключом или флагом.

    Скрытость сильнее замка: пока выход не найден, о замке говорить рано."""
    if exit_.hidden_dc is not None and f"exit:{exit_.id}" not in state.world.revealed:
        return "hidden"
    if is_locked(state, adventure, exit_.locked_by):
        return "locked"
    return "open"


# --- представления для модели ---


def _exit_view(state: GameState, adventure: Adventure, exit_: Exit) -> dict[str, Any]:
    status = exit_status(state, adventure, exit_)
    view: dict[str, Any] = {
        "id": exit_.id,
        "to": exit_.to,
        "direction": exit_.direction,
        "description": exit_.description,
        "status": status,
    }
    if status == "hidden":
        view["hidden_dc"] = exit_.hidden_dc  # Сл поиска: мастеру её просить
    if status == "locked":
        view["locked_by"] = exit_.locked_by  # что открывает: "item:<id>" | "flag:<имя>"
    return view


def _feature_view(state: GameState, adventure: Adventure, feature: Feature) -> dict[str, Any]:
    locked = is_locked(state, adventure, feature.locked_by)
    view: dict[str, Any] = {
        "id": feature.id,
        "name": feature.name,
        "description": feature.description,
        "hidden": feature.hidden,
        "revealed": f"feature:{feature.id}" in state.world.revealed,
        "status": "locked" if locked else "open",
        "check": feature.check.model_dump(mode="json", exclude_none=True)
        if feature.check
        else None,
        "reveals": [
            reveal.model_dump(mode="json", exclude_none=True) for reveal in feature.reveals
        ],
    }
    if locked:
        view["locked_by"] = feature.locked_by
    return view


def _npc_here(state: GameState, npc: Npc) -> dict[str, Any]:
    return {
        "id": npc.id,
        "name": npc.name,
        "role": npc.role,
        "alive": state.world.npc_alive.get(npc.id, True),
        "attitude": state.world.npc_attitude.get(npc.id, npc.attitude),
    }


def _items_here(
    state: GameState, adventure: Adventure, location: Location, npcs_here: list[Npc]
) -> list[dict[str, Any]]:
    """Предметы, лежащие в локации открыто или находящиеся у присутствующих в ней NPC.

    Тот же расчёт видимости, что у `give_item`: предмет внутри ещё не раскрытого места (сундук,
    тайник) не показывается — он назван только в `reveals` этого места, пока его не найдут. Описание
    предмета в список не идёт: его читают через `get_item`."""
    holders = {f"location:{location.id}", *(f"npc:{npc.id}" for npc in npcs_here)}
    return [
        {"id": item.id, "name": item.name, "holder": holder}
        for item in adventure.items.values()
        if (holder := item_holder(state, adventure, item.id)) in holders
        and closed_place_with_item(state, adventure, item.id) is None
    ]


def _encounter_view(
    state: GameState, adventure: Adventure, location: Location
) -> dict[str, Any] | None:
    """Бой локации, пока он не закончен (`flags["encounter:<id>"] != "done"`)."""
    if location.encounter_id is None:
        return None
    flag = state.world.flags.get(f"encounter:{location.encounter_id}")
    if flag == "done":
        return None
    encounter = adventure.encounters[location.encounter_id]
    return {
        "id": encounter.id,
        "status": "active" if flag == "active" else "pending",
        "trigger": encounter.trigger,
        "tactics": encounter.tactics,
        "monsters": [group.model_dump(mode="json") for group in encounter.monsters],
        "pc_zone": encounter.pc_zone,
    }


# --- get_location, get_npc, get_item ---


class GetLocationArgs(ToolArgs):
    location_id: str | None = Field(
        default=None,
        max_length=MAX_ID_CHARS,
        description="id локации; без значения — текущая локация отряда",
    )


def get_location(ctx: ToolContext, args: GetLocationArgs) -> dict[str, Any]:
    state, adventure = ctx.state, ctx.adventure
    location = find_or_error(adventure.locations, "локации", args.location_id or state.location_id)
    npcs_here = [
        npc
        for npc in adventure.npcs.values()
        if state.world.npc_location.get(npc.id, npc.location_id) == location.id
    ]
    return {
        "id": location.id,
        "name": location.name,
        "region": location.region,
        "read_aloud": location.read_aloud,
        "dm_notes": location.dm_notes,
        "exits": [_exit_view(state, adventure, exit_) for exit_ in location.exits],
        "features": [_feature_view(state, adventure, feature) for feature in location.features],
        "npcs": [_npc_here(state, npc) for npc in npcs_here],
        "items": _items_here(state, adventure, location, npcs_here),
        "encounter": _encounter_view(state, adventure, location),
    }


class GetNpcArgs(ToolArgs):
    npc_id: str = Field(max_length=MAX_ID_CHARS, description="id NPC")


def get_npc(ctx: ToolContext, args: GetNpcArgs) -> dict[str, Any]:
    npc = find_or_error(ctx.adventure.npcs, "NPC", args.npc_id)
    return {
        "id": npc.id,
        "name": npc.name,
        "role": npc.role,
        "appearance": npc.appearance,
        "manner": npc.manner,
        "motive": npc.motive,
        "secret": npc.secret,
        "knows": list(npc.knows),
        "relations": [relation.model_dump(mode="json") for relation in npc.relations],
        "statblock": npc.statblock,
        "alive": ctx.state.world.npc_alive.get(npc.id, True),
        "attitude": ctx.state.world.npc_attitude.get(npc.id, npc.attitude),
        "location_id": ctx.state.world.npc_location.get(npc.id, npc.location_id),
    }


class GetItemArgs(ToolArgs):
    item_id: str = Field(max_length=MAX_ID_CHARS, description="id предмета")


def get_item(ctx: ToolContext, args: GetItemArgs) -> dict[str, Any]:
    item = find_or_error(ctx.adventure.items, "предмета", args.item_id)
    view: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "base_item": item.base_item,
        "holder": item_holder(ctx.state, ctx.adventure, item.id),
    }
    if place := closed_place_with_item(ctx.state, ctx.adventure, item.id):
        view["inside"] = f"feature:{place.id}"  # предмет не достать, пока место не раскрыто
    return view


# --- search_lore ---


class SearchLoreArgs(ToolArgs):
    query: str = Field(
        min_length=1, max_length=MAX_QUERY_CHARS, description="слова для поиска, без учёта регистра"
    )
    limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT,
        ge=1,
        le=MAX_SEARCH_LIMIT,
        description="сколько результатов вернуть",
    )


def _documents(adventure: Adventure) -> Iterator[tuple[str, str, str]]:
    """(ref, заголовок, весь текст) каждой сущности, по которой ищет `search_lore`."""
    for location in adventure.locations.values():
        parts = [location.name, location.region, location.read_aloud, location.dm_notes]
        parts += [f"{feature.name}. {feature.description}" for feature in location.features]
        parts += [exit_.description for exit_ in location.exits]
        yield f"location:{location.id}", location.name, "\n".join(parts)
    for npc in adventure.npcs.values():
        parts = [npc.name, npc.role, npc.appearance, npc.manner, npc.motive, npc.secret, *npc.knows]
        yield f"npc:{npc.id}", npc.name, "\n".join(parts)
    for item in adventure.items.values():
        yield f"item:{item.id}", item.name, f"{item.name}\n{item.description}"
    for revelation in adventure.revelations.values():
        parts = [revelation.statement, *(clue.description for clue in revelation.clues)]
        yield f"revelation:{revelation.id}", f"Открытие {revelation.id}", "\n".join(parts)


def _query_words(query: str) -> list[str]:
    words = [word for raw in query.casefold().split() if (word := raw.strip(_PUNCTUATION))]
    words = list(dict.fromkeys(words))
    return [word for word in words if len(word) >= MIN_WORD_CHARS] or words


def _snippet(text: str, position: int) -> str:
    start = max(0, position - SNIPPET_BEFORE_CHARS)
    end = start + SNIPPET_CHARS
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def search_lore(ctx: ToolContext, args: SearchLoreArgs) -> dict[str, Any]:
    words = _query_words(args.query)
    if not words:
        raise ToolError("Запрос состоит из одних знаков препинания: напишите слова для поиска.")
    found: list[tuple[int, str, str, str]] = []
    for ref, title, raw_text in _documents(ctx.adventure):
        text = " ".join(raw_text.split())  # переносы строк и двойные пробелы не нужны в выдержке
        folded = text.casefold()
        matches = sum(folded.count(word) for word in words)
        if matches:
            first = min(folded.find(word) for word in words if word in folded)
            found.append((matches, ref, title, _snippet(text, first)))
    found.sort(key=lambda hit: (-hit[0], hit[1]))  # больше совпадений — выше, ничья — по ref
    return {
        "results": [
            {"ref": ref, "title": title, "snippet": snippet}
            for _, ref, title, snippet in found[: args.limit]
        ]
    }


LORE_TOOLS = (
    ToolSpec(
        name="get_location",
        description=(
            "Прочитать локацию: текст для чтения вслух, заметки мастера, выходы, интересные места, "
            "NPC, предметы и бой. Без аргумента — текущая локация. Статус выхода: open — свободен, "
            "locked — заперт (locked_by говорит, что открывает), hidden — скрыт, пока его не "
            "нашли (hidden_dc — Сл поиска). Интересное место со статусом locked тоже заперто. "
            "Предметы — те, что лежат здесь открыто или у NPC здесь (holder); предмет внутри ещё "
            "не раскрытого места в списке не появится, пока место не найдут."
        ),
        args_model=GetLocationArgs,
        handler=get_location,
    ),
    ToolSpec(
        name="get_npc",
        description=(
            "Прочитать всё об NPC: облик, манеру, мотив, секрет, что знает, связи, статблок "
            "и текущее состояние (жив ли, отношение −3..3, где находится)."
        ),
        args_model=GetNpcArgs,
        handler=get_npc,
    ),
    ToolSpec(
        name="get_item",
        description=(
            "Прочитать предмет приключения: описание и текущий держатель. Если предмет лежит "
            "внутри ещё не раскрытого места, inside называет его: сначала раскройте место."
        ),
        args_model=GetItemArgs,
        handler=get_item,
    ),
    ToolSpec(
        name="search_lore",
        description=(
            "Найти в лоре приключения (локации, NPC, предметы, открытия) места, где встречаются "
            "слова запроса. Возвращает ref, заголовок и выдержку; больше совпадений — выше."
        ),
        args_model=SearchLoreArgs,
        handler=search_lore,
    ),
)
