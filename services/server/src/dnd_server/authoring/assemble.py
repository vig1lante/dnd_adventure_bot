"""Сборка `Adventure` формата v2 из черновика (план DND-2 B3.1; основа — сборка генератора DND-1).

Черновик хранит разделы так, как их пишут шаги и автор; приключение — то, что читают валидатор,
запись папки и игра. Сборка переносит разделы как есть и сводит связи, которые следуют из других
шагов: бой локации (`encounter_id` — бой, который в ней происходит) и находки мест (улика, которая
лежит в месте `feature:<id>`, раскрывается его осмотром). Уже записанная связь не меняется и не
дублируется, а закреплённую локацию сборка не трогает вовсе: её правит автор. Недостающее
(id, старт и финал) остаётся пустым, чтобы валидатор назвал, чего не хватает.
"""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from dnd_engine.adventure.schema import (
    FORMAT_VERSION,
    Adventure,
    Feature,
    Location,
    RevealRef,
    WorldInfo,
)

if TYPE_CHECKING:
    from dnd_server.authoring.draft import AuthoringDraft

PLACEHOLDER_ID = "untitled"  # id приключения, пока замысел не придумал свой


def assemble_adventure(draft: AuthoringDraft) -> Adventure:
    """Собирает приключение из черновика; проверку смысла оставляет валидатору."""
    settings = draft.settings
    fights: dict[str, str] = {}
    for encounter in draft.encounters:
        fights.setdefault(encounter.location_id, encounter.id)
    clues: dict[str, list[RevealRef]] = {}
    for revelation in draft.revelations:
        for clue in revelation.clues:
            kind, _, ident = clue.where.partition(":")
            if kind == "feature":
                clues.setdefault(ident, []).append(RevealRef(kind="clue", id=clue.id))
    locations = {
        location.id: location
        if draft.is_locked(f"location:{location.id}")
        else _linked(location, fights, clues)
        for location in draft.locations
    }
    data = {
        "format_version": FORMAT_VERSION,
        "id": draft.id or PLACEHOLDER_ID,
        "title": draft.title,
        "pitch": settings.pitch,
        "journal_intro": draft.journal_intro,
        "tone": settings.tone,
        "target_hours": settings.target_hours,
        "party_level": settings.party_level,
        "party_size": settings.party_size,
        "rules": {"modules": list(settings.modules)},
        "start_location": draft.start_location or "",
        "finale_location": draft.finale_location or "",
        "villain": draft.plot.villain,
        "acts": draft.plot.acts,
        "endings": draft.plot.endings,
        "locations": locations,
        "npcs": _by_id(draft.npcs),
        "items": _by_id(draft.items),
        "encounters": _by_id(draft.encounters),
        "revelations": _by_id(draft.revelations),
        "monsters": _by_id(draft.monsters),
        "deities": _by_id(draft.deities),
        "factions": _by_id(draft.factions),
        "world": draft.world or WorldInfo(),
        "relations": draft.relations,
        "seeds": draft.seed_ids(),
    }
    return Adventure.model_validate(data)


def _by_id(entities: Iterable[Any]) -> dict[str, Any]:
    """Коллекция приключения «id → сущность» из раздела-списка черновика."""
    return {entity.id: entity for entity in entities}


def _linked(
    location: Location, fights: Mapping[str, str], clues: Mapping[str, list[RevealRef]]
) -> Location:
    """Локация с боем, который в ней происходит, и уликами её мест среди находок."""
    features = tuple(
        _with_reveals(feature, clues.get(feature.id, ())) for feature in location.features
    )
    encounter_id = location.encounter_id or fights.get(location.id)
    return location.model_copy(update={"encounter_id": encounter_id, "features": features})


def _with_reveals(feature: Feature, reveals: Iterable[RevealRef]) -> Feature:
    """Место с находками `reveals`, которых в нём ещё нет."""
    known = {(reveal.kind, reveal.id) for reveal in feature.reveals}
    added = tuple(reveal for reveal in reveals if (reveal.kind, reveal.id) not in known)
    return feature.model_copy(update={"reveals": feature.reveals + added}) if added else feature
