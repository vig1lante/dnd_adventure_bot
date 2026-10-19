"""Схемы ответов модели на шагах генератора приключений (план B5.6) и формат файла сидов.

Ответ модели — недоверенный ввод: схемы строгие (`extra="forbid"`), у строк и списков есть
пределы, id подходят под шаблон схемы движка, Сл проверок берутся из таблицы 5/10/15/20/25/30
(типы `EntityId`, `TableDC` и `CheckSpec` из `dnd_engine.adventure.schema`). Схемы описывают только
то, что решает модель. Связи, которые следуют из других шагов (бой локации, улики места), и все
числа правил собирает код; правила целостности (три улики, достижимость, бюджет боя) проверяет
валидатор движка, поэтому схема допускает, например, открытие с одной уликой.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from dnd_engine.adventure.schema import (
    ID_PATTERN,
    CheckSpec,
    EntityId,
    Item,
    Location,
    Npc,
    TableDC,
)
from dnd_engine.checks import VALID_DCS

# Сл из таблицы движка текстом: её называют схема и промпты шагов.
DC_VALUES = ", ".join(str(dc) for dc in sorted(VALID_DCS))

# Пределы текстов: заголовок, одна строка и абзац. Ответ модели не должен раздувать файлы YAML.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Direction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]

# Замок выхода — только предмет-ключ: важное запирается предметом, а не флагом. Замка `flag:…`
# (в том числе `flag:encounter:…`, который открывается уже в начале боя) схема не пропускает.
LockRef = Annotated[str, StringConstraints(pattern=rf"^item:{ID_PATTERN[1:]}")]
# Держатель предмета в генераторе — локация или NPC; `gone` и персонажей в приключении нет.
HolderRef = Annotated[str, StringConstraints(pattern=rf"^(location|npc):{ID_PATTERN[1:]}")]
# Где лежит улика: место, NPC или предмет (локацию улика не называет).
CluePlace = Annotated[str, StringConstraints(pattern=rf"^(feature|npc|item):{ID_PATTERN[1:]}")]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_unique(kind: str, ids: Iterable[str]) -> None:
    """Повтор id внутри одного ответа — ошибка схемы: модель исправит её сама при повторе."""
    repeated = [ident for ident, count in Counter(ids).items() if count > 1]
    if repeated:
        raise ValueError(f"{kind}: id повторяются: {', '.join(repeated)}")


# --- generator.concept ---


class ActOut(_Out):
    id: EntityId
    title: Name
    summary: Text
    key_scenes: list[Line] = Field(default_factory=list, max_length=6)


class RevelationOut(_Out):
    id: EntityId
    statement: Text = Field(description="Вывод, к которому должны прийти игроки; без улик")


class ConceptOut(_Out):
    id: EntityId = Field(description="Короткий slug приключения: латиница, цифры, дефис")
    title: Name
    pitch: Text = Field(description="Завязка, которую расскажут игрокам; без тайн сюжета")
    tone: Line
    acts: list[ActOut] = Field(min_length=1, max_length=5)
    endings: list[Text] = Field(min_length=1, max_length=5)
    revelations: list[RevelationOut] = Field(
        min_length=1, max_length=5, description="Улики к открытиям придумает отдельный шаг"
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("acts", (act.id for act in self.acts))
        _require_unique("revelations", (revelation.id for revelation in self.revelations))
        return self


# --- generator.world ---


class FactionOut(_Out):
    name: Name
    goal: Line


class WorldOut(_Out):
    history: Text
    factions: list[FactionOut] = Field(default_factory=list, max_length=5)
    rumors: list[Line] = Field(default_factory=list, max_length=8)


# --- generator.locations ---


class ExitOut(_Out):
    id: EntityId
    to: EntityId
    direction: Direction
    description: Line
    locked_by: LockRef | None = Field(
        default=None, description='Замок только предметом: "item:<id предмета-ключа>"'
    )
    hidden_dc: TableDC | None = Field(
        default=None, description=f"Сл поиска скрытого выхода, одна из: {DC_VALUES}"
    )


class LocationNodeOut(_Out):
    id: EntityId
    name: Name
    region: Line = ""
    exits: list[ExitOut] = Field(min_length=1, max_length=6)


class LocationGraphOut(_Out):
    start_location: EntityId
    finale_location: EntityId
    locations: list[LocationNodeOut] = Field(max_length=12)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("locations", (node.id for node in self.locations))
        return self


# --- generator.location_detail ---


class FeatureOut(_Out):
    id: EntityId = Field(description="Уникален во всём приключении")
    name: Name
    description: Text
    check: CheckSpec | None = Field(
        default=None,
        description="Проверка, которой достают находку места; без неё место просто есть",
    )


class LocationDetailOut(_Out):
    read_aloud: Text = Field(description="Текст для чтения вслух; без тайн")
    dm_notes: Text | None = Field(default=None, description="Заметки мастера: тайны и тактика")
    features: list[FeatureOut] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("features", (feature.id for feature in self.features))
        return self


# --- generator.npcs ---


class RelationOut(_Out):
    npc_id: EntityId
    kind: Direction = Field(description="Тип связи: sibling, parent, enemy, debtor…")
    note: Line = ""


class NpcOut(_Out):
    id: EntityId
    name: Name
    role: Line
    appearance: Line
    manner: Line
    motive: Line
    secret: Line = ""
    knows: list[Line] = Field(default_factory=list, max_length=6)
    attitude: int = Field(default=0, ge=-3, le=3)
    relations: list[RelationOut] = Field(default_factory=list, max_length=5)
    location_id: EntityId
    statblock: EntityId | None = Field(default=None, description="id монстра SRD, если NPC дерётся")


class NpcsOut(_Out):
    npcs: list[NpcOut] = Field(max_length=12)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("npcs", (npc.id for npc in self.npcs))
        return self


# --- generator.encounters ---


class MonsterGroupOut(_Out):
    monster_id: EntityId = Field(description="id монстра SRD из списка в запросе")
    count: int = Field(ge=1, le=20)


class EncounterOut(_Out):
    id: EntityId
    location_id: EntityId
    trigger: Text
    tactics: Text
    monsters: list[MonsterGroupOut] = Field(min_length=1, max_length=4)


class EncountersOut(_Out):
    encounters: list[EncounterOut] = Field(max_length=8)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("encounters", (encounter.id for encounter in self.encounters))
        # У локации один бой (`Location.encounter_id`).
        _require_unique("encounters.location_id", (e.location_id for e in self.encounters))
        return self


# --- generator.items ---


class ItemOut(_Out):
    id: EntityId
    name: Name
    description: Text
    base_item: EntityId | None = Field(default=None, description="id предмета SRD для механики")
    holder: HolderRef = Field(description='"location:<id>" или "npc:<id>"')


class ItemsOut(_Out):
    items: list[ItemOut] = Field(max_length=20)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("items", (item.id for item in self.items))
        return self


# --- generator.clues ---


class ClueOut(_Out):
    id: EntityId = Field(description="Уникален во всём приключении")
    description: Text
    where: CluePlace = Field(description='"feature:<id>", "npc:<id>" или "item:<id>"')


class RevelationCluesOut(_Out):
    revelation_id: EntityId
    clues: list[ClueOut] = Field(min_length=1, max_length=8)


class CluesOut(_Out):
    revelations: list[RevelationCluesOut] = Field(max_length=5)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("revelations", (r.revelation_id for r in self.revelations))
        return self


# --- файл сидов (--seeds) ---


class Seeds(_Out):
    """Сущности, которые автор задал вручную: полные `Location`, `Npc` и `Item` движка.

    Генератор встраивает их в приключение как есть, а их id пишет в `seeds.yaml`."""

    locations: tuple[Location, ...] = ()
    npcs: tuple[Npc, ...] = ()
    items: tuple[Item, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(
            entity.id for group in (self.locations, self.npcs, self.items) for entity in group
        )

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        for kind in ("locations", "npcs", "items"):
            _require_unique(kind, (entity.id for entity in getattr(self, kind)))
        return self
