"""Шаги генератора приключений по ролям — контракт блока 3 (план DND-2 «Блок 3»; спека §5, §9).

`run_step(provider, draft, step, *, wish=None, target=None)` — один шаг черновика: запрос дешёвой
модели (`complete_json`) с промптом роли шага и общим блоком стиля с флажком мата черновика
(`role_with_style`), ответ по схеме шага (`schemas`) и новый черновик, где ответ лёг в раздел шага.
Его вызывают полный проход (`pipeline.generate`), CLI и конструктор на сайте (блоки 4–6).

Промпт шага: строка «Шаг: …», заказ (длина игры, отряд, подсказка о времени), весь черновик,
закреплённое и пожелание автора к шагу — каждое отдельным блоком данных; при круге исправления
(`repair_step`) в конце ещё отчёт проверки. Тексты ролей лежат в файлах `prompts/` (спека Р2), код
держит только подстановки. Всё, что пишет генератор, может дойти до игроков через мастера или
дневник, поэтому блок стиля получает каждая роль.

Закреплённое не меняется по правилу сервера, а не только по просьбе в промпте: ответ модели для
закреплённого id отбрасывается, закреплённый шаг не запускается, закреплённая цель `target` не
перегенерируется. `target` (`<вид>:<id>`) меняет ровно одну сущность раздела шага. Наполнение
локаций (`location_contents`) — по запросу на локацию, параллельно, не больше
`MAX_PARALLEL_CONTENTS` одновременно.
"""

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from dnd_engine.adventure.budget import XP_BUDGET_PER_CHARACTER
from dnd_engine.adventure.schema import (
    Clue,
    Deity,
    Encounter,
    Exit,
    Faction,
    Feature,
    Item,
    Location,
    LocationSecret,
    Npc,
    Relation,
    Revelation,
    WorldInfo,
)
from dnd_engine.adventure.validator import DURATION_TOLERANCE, Issue
from dnd_engine.content import load_srd
from dnd_server.authoring import schemas
from dnd_server.authoring.draft import SECTION_KINDS, STEPS, AuthoringDraft, DraftSettings, Plot
from dnd_server.llm.base import JsonRequest, LLMProvider, ProviderError
from dnd_server.prompts import role_with_style

__all__ = ["MAX_PARALLEL_CONTENTS", "STEPS", "is_empty", "repair_step", "run_step", "target_id"]

MAX_PARALLEL_CONTENTS = 4  # запросов наполнения локаций одновременно

# Роль шага — файл `prompts/<роль>.md` (спека §5).
STEP_ROLES = {
    "concept": "plot_architect",
    "world": "world_builder",
    "pantheon": "pantheon_keeper",
    "factions": "faction_builder",
    "plot": "plot_architect",
    "locations": "location_builder",
    "location_contents": "location_builder",
    "npcs": "npc_lore_writer",
    "relations": "relations_weaver",
    "encounters": "encounter_designer",
    "items": "item_smith",
    "clues": "clue_designer",
}
STEP_SCHEMAS: dict[str, type[BaseModel]] = {
    "concept": schemas.ConceptOut,
    "world": schemas.WorldOut,
    "pantheon": schemas.PantheonOut,
    "factions": schemas.FactionsOut,
    "plot": schemas.PlotOut,
    "locations": schemas.LocationGraphOut,
    "location_contents": schemas.LocationContentsOut,
    "npcs": schemas.NpcsOut,
    "relations": schemas.RelationsOut,
    "encounters": schemas.EncountersOut,
    "items": schemas.ItemsOut,
    "clues": schemas.CluesOut,
}
# Раздел-список, который пишет шаг: по нему шаг пуст, и в нём шаг перегенерирует цель `target`.
STEP_SECTIONS = {
    "pantheon": "deities",
    "factions": "factions",
    "locations": "locations",
    "location_contents": "locations",
    "npcs": "npcs",
    "relations": "relations",
    "encounters": "encounters",
    "items": "items",
    "clues": "revelations",
}

# Как валидатор оценивает время игры: веса и допуск — числа для модели, а что с ними делать,
# говорят роли. Допуск берётся у валидатора; веса записаны текстом и сверяются с его оценкой тестом
# (`test_time_hint_repeats_the_validator_estimate_and_tolerance`).
_TIME_ESTIMATE = (
    "Оценка времени игры: 12 мин на локацию, 30 на бой, 10 на открытие, 5 на NPC; "
    f"допуск {round(DURATION_TOLERANCE * 100)}%."
)
# Шаги, которые только наполняют уже существующие сущности своего раздела (локации, открытия):
# без незакреплённых сущностей модели нечего дописывать, и платный запрос не нужен.
_FILLING_STEPS = ("location_contents", "clues")

type Merge = Callable[[AuthoringDraft, Any, str | None], AuthoringDraft]


async def run_step(
    provider: LLMProvider,
    draft: AuthoringDraft,
    step: str,
    *,
    wish: str | None = None,
    target: str | None = None,
) -> AuthoringDraft:
    """Новый черновик после шага `step`; закреплённое не тронуто, `target` — одна сущность.

    `wish` — пожелание к этому запуску; без него берётся пожелание шага из черновика. Шаг или
    цель, которые закрепил автор, модель не спрашивают: черновик возвращается как есть. Цель не из
    раздела шага (или которой нет в черновике) — `ValueError`; ответ без цели и ошибки модели —
    `ProviderError`."""
    chosen = target_id(draft, step, target)
    if draft.is_locked(f"step:{step}") or (target is not None and draft.is_locked(target)):
        return draft
    if _nothing_to_fill(draft, step):
        return draft
    wish = wish or draft.wishes.get(step)
    if step == "location_contents":
        ids = [chosen] if chosen else [loc.id for loc in _unlocked(draft, "locations")]
        return await _fill_contents(provider, draft, ids, wish=wish, target=target, issues={})
    answer = await _ask(provider, draft, step, wish=wish, target=target, issues=())
    return _MERGES[step](draft, answer, chosen)


async def repair_step(
    provider: LLMProvider, draft: AuthoringDraft, step: str, issues: Sequence[Issue]
) -> AuthoringDraft:
    """Круг исправления шага: тот же запрос, что у `run_step`, и в конце отчёт проверки.

    Наполнение локаций исправляется только у локаций с проблемами и у тех, что ещё не наполнены
    (новые локации после исправления графа); остальные локации не перезапрашиваются."""
    if draft.is_locked(f"step:{step}") or _nothing_to_fill(draft, step):
        return draft
    wish = draft.wishes.get(step)
    if step == "location_contents":
        by_location: dict[str, list[Issue]] = {}
        for issue in issues:
            kind, _, rest = issue.ref.partition(":")
            if kind == "location":
                by_location.setdefault(rest.partition("/")[0], []).append(issue)
        ids = [
            loc.id
            for loc in _unlocked(draft, "locations")
            if loc.id in by_location or not loc.read_aloud
        ]
        return await _fill_contents(
            provider, draft, ids, wish=wish, target=None, issues=by_location
        )
    answer = await _ask(provider, draft, step, wish=wish, target=None, issues=issues)
    return _MERGES[step](draft, answer, None)


def _nothing_to_fill(draft: AuthoringDraft, step: str) -> bool:
    """Шаг только наполняет сущности раздела, а незакреплённых сущностей в нём нет."""
    return step in _FILLING_STEPS and not _unlocked(draft, STEP_SECTIONS[step])


def is_empty(draft: AuthoringDraft, step: str) -> bool:
    """Пуст ли шаг: его раздел ещё не написан (закреплённое автором не считается — вокруг него
    шаг и строит)."""
    match step:
        case "concept":
            return draft.id is None
        case "world":
            return draft.world is None
        case "plot":
            return not draft.plot.acts
        case "location_contents":
            return any(not loc.read_aloud for loc in _unlocked(draft, "locations"))
        case "clues":
            return any(not revelation.clues for revelation in _unlocked(draft, "revelations"))
    return not _unlocked(draft, STEP_SECTIONS[step])


# --- запрос к модели ---


async def _ask(
    provider: LLMProvider,
    draft: AuthoringDraft,
    step: str,
    *,
    wish: str | None,
    target: str | None,
    issues: Sequence[Issue],
    location: str | None = None,
) -> Any:
    request = JsonRequest(
        system=role_with_style(STEP_ROLES[step], draft.settings.profanity),
        prompt=_prompt(draft, step, wish=wish, target=target, issues=issues, location=location),
        purpose=f"generator.{step}",
        cheap=True,
    )
    return await provider.complete_json(request, STEP_SCHEMAS[step])


def _prompt(
    draft: AuthoringDraft,
    step: str,
    *,
    wish: str | None,
    target: str | None,
    issues: Sequence[Issue],
    location: str | None,
) -> str:
    """Шаг, заказ, черновик, закреплённое, данные шага, пожелание и в конце отчёт проверки."""
    task = f"Шаг: {step}"
    if location is not None:
        task += f", локация {location}"
    if target is not None:
        task += f", перегенерировать только {target}"
    parts = [f"{task}.", _brief(draft.settings), _data("Черновик приключения", _view(draft))]
    if draft.locks:
        parts.append(_data("Закреплено автором", sorted(draft.locks)))
    parts += _step_data(step, draft.settings)
    if wish:
        parts.append(_data("Пожелание автора к шагу", wish))
    if issues:
        parts.append(
            _data("Отчёт проверки", [f"{i.severity} {i.code} {i.ref}: {i.message}" for i in issues])
        )
    return "\n\n".join(parts)


def _data(label: str, value: Any) -> str:
    """Блок данных для промпта. Угловая скобка экранирована (`\\u003c` — тот же символ в JSON),
    поэтому текст из черновика или пожелания не закроет блок и не подделает тег."""
    text = json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    return f"{label}:\n<данные>\n{text}\n</данные>"


def _view(draft: AuthoringDraft) -> dict[str, Any]:
    """Черновик для модели: разделы без значений по умолчанию; закрепления идут своим блоком, а
    пожелания других шагов модели этого шага не нужны."""
    return draft.model_dump(mode="json", exclude={"locks", "wishes"}, exclude_defaults=True)


def _brief(settings: DraftSettings) -> str:
    hours = settings.target_hours
    return (
        f"Заказ: длительность {hours:g} ч ({hours * 60:g} мин), игроков {settings.party_size}, "
        f"уровень героев {settings.party_level}. {_TIME_ESTIMATE}"
    )


def _step_data(step: str, settings: DraftSettings) -> list[str]:
    """Справочники шага: монстры SRD и бюджет опыта для боёв, предметы SRD для предметов.

    У монстров — числа их статблоков как в данных SRD (без пересчёта единиц): по ним модель пишет
    свой статблок «как у похожего монстра»."""
    srd = load_srd()
    if step == "encounters":
        monsters = [
            {
                "id": m.id,
                "name": m.name,
                "cr": m.cr,
                "xp": m.xp,
                "ac": m.ac,
                "hp": m.hp,
                "speed": m.speed,
            }
            for m in srd.monsters.values()
        ]
        return [part for part in (_data("Монстры SRD", monsters), _budget_hint(settings)) if part]
    if step == "items":
        return [_data("Предметы SRD", list(srd.items))]
    return []


def _budget_hint(settings: DraftSettings) -> str:
    per_character = XP_BUDGET_PER_CHARACTER.get(settings.party_level)
    if per_character is None:
        return ""
    low, moderate, high = (budget * settings.party_size for budget in per_character)
    return f"Бюджет опыта боя для отряда: низкая {low}, умеренная {moderate}, высокая {high} XP."


# --- цель и закреплённое ---


def target_id(draft: AuthoringDraft, step: str, target: str | None) -> str | None:
    """id цели `target` в разделе шага; неизвестный шаг, цель не того вида или которой нет —
    `ValueError`. Этой же проверкой конструктор (блок 4) отвергает запрос до задания генерации."""
    if step not in STEPS:
        raise ValueError(f"неизвестный шаг генератора: {step!r}")
    if target is None:
        return None
    section = STEP_SECTIONS.get(step)
    kind, _, ident = target.partition(":")
    if section is None or kind != SECTION_KINDS[section] or not ident:
        raise ValueError(f"шаг {step} не перегенерирует {target!r}")
    if all(entity.id != ident for entity in getattr(draft, section)):
        raise ValueError(f"{target!r}: такой сущности в черновике нет")
    return ident


def _unlocked(draft: AuthoringDraft, section: str) -> list[Any]:
    kind = SECTION_KINDS[section]
    return [e for e in getattr(draft, section) if not draft.is_locked(f"{kind}:{e.id}")]


def _merged(
    old: Sequence[Any], new: Iterable[Any], kind: str, draft: AuthoringDraft, target_id: str | None
) -> tuple[Any, ...]:
    """Раздел после ответа шага: закреплённое как было (и первым), остальное — из ответа; с целью
    меняется только она, на своём месте."""
    locked = {e.id for e in old if draft.is_locked(f"{kind}:{e.id}")}
    fresh = [e for e in new if e.id not in locked]
    if target_id is None:
        return (*(e for e in old if e.id in locked), *fresh)
    chosen = next((e for e in fresh if e.id == target_id), None)
    if chosen is None:
        raise ProviderError(f"в ответе нет {kind}:{target_id}, которую просили перегенерировать")
    return tuple(chosen if e.id == target_id else e for e in old)


def _engine[T: BaseModel](model: type[T], answers: Iterable[BaseModel]) -> list[T]:
    """Ответы модели — сущности движка с теми же полями."""
    return [model.model_validate(answer.model_dump()) for answer in answers]


# --- ответ шага в черновике ---


def _merge_concept(
    draft: AuthoringDraft, answer: schemas.ConceptOut, _target: str | None
) -> AuthoringDraft:
    """Замысел: id, название, завязка, тон, главное для дневника и открытия (улики открытия с
    тем же id остаются: их пишет шаг clues)."""
    clues = {revelation.id: revelation.clues for revelation in draft.revelations}
    revelations = [
        Revelation(id=r.id, statement=r.statement, clues=clues.get(r.id, ()))
        for r in answer.revelations
    ]
    settings = draft.settings.model_copy(update={"pitch": answer.pitch, "tone": answer.tone})
    return draft.model_copy(
        update={
            "id": answer.id,
            "title": answer.title,
            "journal_intro": answer.journal_intro,
            "settings": settings,
            "revelations": _merged(draft.revelations, revelations, "revelation", draft, None),
        }
    )


def _merge_world(
    draft: AuthoringDraft, answer: schemas.WorldOut, _target: str | None
) -> AuthoringDraft:
    return draft.model_copy(update={"world": WorldInfo.model_validate(answer.model_dump())})


def _merge_plot(
    draft: AuthoringDraft, answer: schemas.PlotOut, _target: str | None
) -> AuthoringDraft:
    return draft.model_copy(update={"plot": Plot.model_validate(answer.model_dump())})


def _merge_section(section: str, model: type[BaseModel]) -> Merge:
    """Шаг, чей ответ — список сущностей одного раздела (боги, фракции, NPC, связи, предметы)."""
    kind = SECTION_KINDS[section]

    def merge(draft: AuthoringDraft, answer: Any, target_id: str | None) -> AuthoringDraft:
        new = _engine(model, getattr(answer, section))
        return draft.model_copy(
            update={section: _merged(getattr(draft, section), new, kind, draft, target_id)}
        )

    return merge


def _merge_locations(
    draft: AuthoringDraft, answer: schemas.LocationGraphOut, target_id: str | None
) -> AuthoringDraft:
    """Граф: имена, края и выходы локаций; наполнение локации с тем же id сохраняется."""
    previous = {location.id: location for location in draft.locations}
    nodes = [_graph_location(node, previous.get(node.id)) for node in answer.locations]
    update: dict[str, Any] = {
        "locations": _merged(draft.locations, nodes, "location", draft, target_id)
    }
    if target_id is None:
        update |= {
            "start_location": answer.start_location,
            "finale_location": answer.finale_location,
        }
    return draft.model_copy(update=update)


def _graph_location(node: schemas.LocationNodeOut, previous: Location | None) -> Location:
    graph = {
        "name": node.name,
        "region": node.region,
        "exits": tuple(_engine(Exit, node.exits)),
        "public": node.public,
        "known_at_start": node.known_at_start,
    }
    if previous is None:
        return Location(id=node.id, read_aloud="", **graph)
    return previous.model_copy(update=graph)


def _merge_encounters(
    draft: AuthoringDraft, answer: schemas.EncountersOut, target_id: str | None
) -> AuthoringDraft:
    """Бои и свои статблоки. С целью-боем прежние статблоки не меняются (на них ссылаются другие
    бои, NPC и боги), а из ответа добавляются только статблоки с новыми id."""
    encounters = _merged(
        draft.encounters, _engine(Encounter, answer.encounters), "encounter", draft, target_id
    )
    if target_id is None:
        monsters = _merged(draft.monsters, answer.monsters, "monster", draft, None)
    else:
        known = {m.id for m in draft.monsters}
        added = [
            m
            for m in answer.monsters
            if m.id not in known and not draft.is_locked(f"monster:{m.id}")
        ]
        monsters = (*draft.monsters, *added)
    return draft.model_copy(update={"encounters": encounters, "monsters": monsters})


def _merge_clues(
    draft: AuthoringDraft, answer: schemas.CluesOut, target_id: str | None
) -> AuthoringDraft:
    """Улики открытий: у каждого незакреплённого открытия из ответа — его новые улики."""
    given = {r.revelation_id: tuple(_engine(Clue, r.clues)) for r in answer.revelations}
    if target_id is not None:
        if target_id not in given:
            raise ProviderError(
                f"в ответе нет revelation:{target_id}, которую просили перегенерировать"
            )
        given = {target_id: given[target_id]}
    revelations = tuple(
        revelation.model_copy(update={"clues": given[revelation.id]})
        if revelation.id in given and not draft.is_locked(f"revelation:{revelation.id}")
        else revelation
        for revelation in draft.revelations
    )
    return draft.model_copy(update={"revelations": revelations})


_MERGES: dict[str, Merge] = {
    "concept": _merge_concept,
    "world": _merge_world,
    "pantheon": _merge_section("deities", Deity),
    "factions": _merge_section("factions", Faction),
    "plot": _merge_plot,
    "locations": _merge_locations,
    "npcs": _merge_section("npcs", Npc),
    "relations": _merge_section("relations", Relation),
    "encounters": _merge_encounters,
    "items": _merge_section("items", Item),
    "clues": _merge_clues,
}


# --- наполнение локаций ---


async def _fill_contents(
    provider: LLMProvider,
    draft: AuthoringDraft,
    ids: Sequence[str],
    *,
    wish: str | None,
    target: str | None,
    issues: Mapping[str, Sequence[Issue]],
) -> AuthoringDraft:
    """Наполнение локаций `ids`: по запросу на локацию, параллельно, не больше
    `MAX_PARALLEL_CONTENTS` одновременно. Каждый запрос видит один и тот же черновик."""
    if not ids:
        return draft
    slots = asyncio.Semaphore(MAX_PARALLEL_CONTENTS)

    async def contents(location_id: str) -> schemas.LocationContentsOut:
        async with slots:
            return await _ask(
                provider,
                draft,
                "location_contents",
                wish=wish,
                target=target,
                issues=issues.get(location_id, ()),
                location=location_id,
            )

    tasks = [asyncio.create_task(contents(location_id)) for location_id in ids]
    try:
        answers = await asyncio.gather(*tasks)
    except BaseException:
        # Первая ошибка останавливает остальные запросы: лишние вызовы модели платные.
        for task in tasks:
            task.cancel()
        raise
    filled = dict(zip(ids, answers, strict=True))
    locations = tuple(
        _with_contents(location, filled[location.id]) if location.id in filled else location
        for location in draft.locations
    )
    return draft.model_copy(update={"locations": locations})


def _with_contents(location: Location, answer: schemas.LocationContentsOut) -> Location:
    return location.model_copy(
        update={
            "read_aloud": answer.read_aloud,
            "dm_notes": answer.dm_notes or "",
            "features": tuple(_engine(Feature, answer.features)),
            "secrets": tuple(_engine(LocationSecret, answer.secrets)),
        }
    )
