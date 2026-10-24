"""Схемы ответов модели на шагах генератора приключений формата v2 (план DND-2 B3.1, спека §8, §9).

Ответ модели — недоверенный ввод: схемы строгие (`extra="forbid"`), у строк и списков есть
пределы, id подходят под шаблон схемы движка, Сл проверок берутся из таблицы 5/10/15/20/25/30
(типы `EntityId`, `TableDC` и `CheckSpec` из `dnd_engine.adventure.schema`). Пределы числа сущностей
(богов, фракций, локаций, NPC, связей, боёв, предметов) — это и есть ограничение размера мира,
который строит генератор: контекст мастера не разрастается от сгенерированного приключения.

Схемы описывают только то, что решает модель. Связи, которые следуют из других шагов (бой локации,
улики места), и все числа правил собирает код; правила целостности (три улики, достижимость,
бюджет боя, ссылки) проверяет валидатор движка, поэтому схема допускает, например, открытие с
одной уликой. Исключение — свой статблок монстра (`EncountersOut.monsters`): это модель движка
`MonsterDef` целиком, а её числа проверяет валидатор приключения.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from dnd_engine.adventure.schema import (
    ID_PATTERN,
    REF_KINDS,
    CheckSpec,
    EntityId,
    RelationKind,
    TableDC,
    is_entity_id,
)
from dnd_engine.checks import VALID_DCS
from dnd_engine.content import MonsterDef

# Сл из таблицы движка текстом: её называют схема и промпты ролей.
DC_VALUES = ", ".join(str(dc) for dc in sorted(VALID_DCS))

# Пределы текстов: заголовок, одна строка и абзац. Ответ модели не должен раздувать файлы YAML.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Direction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
# Необязательная строка (тайна, что знают игроки): пусто — поля нет.
OptionalLine = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]

_ID_TAIL = ID_PATTERN[1:]  # шаблон id без «^»: хвост ссылки «вид:id»

# Замок выхода и места — только предмет-ключ: важное запирается предметом, а не флагом. Замка
# `flag:…` (в том числе `flag:encounter:…`, который открывается уже в начале боя) схема не пропускает.
LockRef = Annotated[str, StringConstraints(pattern=rf"^item:{_ID_TAIL}")]
# Держатель предмета в генераторе — локация или NPC; `gone` и персонажей в приключении нет.
HolderRef = Annotated[str, StringConstraints(pattern=rf"^(location|npc):{_ID_TAIL}")]
# Где лежит улика: место, NPC или предмет (локацию улика не называет).
CluePlace = Annotated[str, StringConstraints(pattern=rf"^(feature|npc|item):{_ID_TAIL}")]
# Конец связи графа — NPC, фракция, бог или место (ссылка формата v2).
EndRef = Annotated[str, StringConstraints(pattern=rf"^({'|'.join(REF_KINDS)}):{_ID_TAIL}")]
# Злодей сюжета — NPC или бог; лидер фракции — NPC.
VillainRef = Annotated[str, StringConstraints(pattern=rf"^(npc|deity):{_ID_TAIL}")]
NpcRef = Annotated[str, StringConstraints(pattern=rf"^npc:{_ID_TAIL}")]


def _file_name_id(monster: MonsterDef) -> MonsterDef:
    # id своего статблока становится именем файла `monsters/<id>.yaml`. `Adventure` проверяет это
    # только при сборке, то есть уже после всех вызовов модели; здесь — сразу, в схеме ответа и в
    # черновике. В сообщении нет самого id: его выбирала модель или чужой файл.
    if not is_entity_id(monster.id):
        raise ValueError(f"id своего статблока не подходит под шаблон {ID_PATTERN}")
    return monster


# Свой статблок (модель движка `MonsterDef`) с id по шаблону id сущностей.
CustomMonster = Annotated[MonsterDef, AfterValidator(_file_name_id)]

MAX_DEITIES = 6
MAX_FACTIONS = 6
MAX_LOCATIONS = 12
MAX_NPCS = 12
MAX_RELATIONS = 24
MAX_ENCOUNTERS = 8
MAX_CUSTOM_MONSTERS = 3
MAX_ITEMS = 20


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _JournalOut(_Out):
    """Поля дневника (спека §8, §12): что могут знать игроки, без тайн, и известно ли с начала."""

    public: OptionalLine = Field(default="", description="Что могут знать игроки; без тайн")
    known_at_start: bool = Field(default=False, description="Известно отряду с начала игры")


def _require_unique(kind: str, ids: Iterable[str]) -> None:
    """Повтор id внутри одного ответа — ошибка схемы: модель исправит её сама при повторе."""
    repeated = [ident for ident, count in Counter(ids).items() if count > 1]
    if repeated:
        raise ValueError(f"{kind}: id повторяются: {', '.join(repeated)}")


# --- generator.concept ---


class RevelationOut(_Out):
    id: EntityId
    statement: Text = Field(description="Вывод, к которому должны прийти игроки; без улик")


class ConceptOut(_Out):
    id: EntityId = Field(description="Короткий slug приключения: латиница, цифры, дефис")
    title: Name
    pitch: Text = Field(description="Завязка, которую расскажут игрокам; без тайн сюжета")
    tone: Line
    journal_intro: Text = Field(description="Главное для дневника: завязка и цель отряда")
    revelations: list[RevelationOut] = Field(
        min_length=1, max_length=5, description="Улики к открытиям придумает отдельный шаг"
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("revelations", (revelation.id for revelation in self.revelations))
        return self


# --- generator.world ---


class RegionOut(_JournalOut):
    id: EntityId
    name: Name
    description: Text


class RumorOut(_JournalOut):
    text: Line
    true: bool = Field(description="Правдив ли слух: знает только мастер")


class WorldOut(_Out):
    history: Text
    regions: list[RegionOut] = Field(default_factory=list, max_length=6)
    rumors: list[RumorOut] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("regions", (region.id for region in self.regions))
        return self


# --- generator.pantheon ---


class DeityOut(_JournalOut):
    id: EntityId
    name: Name
    title: Name
    domains: list[Name] = Field(min_length=1, max_length=4)
    symbol: Line
    dogma: Line
    appearance: Line
    attitude_to_mortals: Line
    secret: OptionalLine = ""
    avatar_statblock: EntityId | None = Field(
        default=None, description="id монстра SRD или своего статблока, если бог может явиться"
    )


class PantheonOut(_Out):
    deities: list[DeityOut] = Field(max_length=MAX_DEITIES)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("deities", (deity.id for deity in self.deities))
        return self


# --- generator.factions ---


class FactionOut(_JournalOut):
    id: EntityId
    name: Name
    goal: Line
    methods: Line
    leader: NpcRef | None = Field(default=None, description='Лидер: "npc:<id>"')
    base_location: EntityId | None = Field(default=None, description="id локации базы")
    secret: OptionalLine = ""
    attitude_to_party: int = Field(default=0, ge=-3, le=3)


class FactionsOut(_Out):
    factions: list[FactionOut] = Field(max_length=MAX_FACTIONS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("factions", (faction.id for faction in self.factions))
        return self


# --- generator.plot ---


class ActOut(_Out):
    id: EntityId
    title: Name
    summary: Text
    key_scenes: list[Line] = Field(default_factory=list, max_length=6)


class PlotOut(_Out):
    acts: list[ActOut] = Field(min_length=1, max_length=5)
    endings: list[Text] = Field(min_length=1, max_length=5)
    villain: VillainRef | None = Field(default=None, description='"npc:<id>" или "deity:<id>"')

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("acts", (act.id for act in self.acts))
        return self


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


class LocationNodeOut(_JournalOut):
    id: EntityId
    name: Name
    region: OptionalLine = ""
    exits: list[ExitOut] = Field(min_length=1, max_length=6)


class LocationGraphOut(_Out):
    start_location: EntityId
    finale_location: EntityId
    locations: list[LocationNodeOut] = Field(max_length=MAX_LOCATIONS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("locations", (node.id for node in self.locations))
        return self


# --- generator.location_contents ---


class HiddenItemOut(_Out):
    """Находка тайника: предмет, который лежит внутри места (его создаст шаг items)."""

    kind: Literal["item"]
    id: EntityId


class FeatureOut(_Out):
    id: EntityId = Field(description="Уникален во всём приключении")
    name: Name
    description: Text
    check: CheckSpec | None = Field(
        default=None,
        description="Проверка, которой достают находку места; без неё место просто есть",
    )
    locked_by: LockRef | None = Field(default=None, description='Замок: "item:<id ключа>"')
    reveals: list[HiddenItemOut] = Field(
        default_factory=list, max_length=2, description="Тайник: предметы внутри места"
    )


class SecretRevealOut(_Out):
    kind: Literal["clue", "item", "flag", "exit"]
    id: EntityId


class SecretOut(_Out):
    id: EntityId = Field(description="Уникален во всём приключении")
    text: Text = Field(description="Тайна места для мастера")
    check: CheckSpec | None = Field(default=None, description="Проверка, которой её раскрывают")
    reveals: list[SecretRevealOut] = Field(default_factory=list, max_length=3)


class LocationContentsOut(_Out):
    read_aloud: Text = Field(description="Текст для чтения вслух; без тайн")
    dm_notes: Text | None = Field(default=None, description="Заметки мастера: тайны и тактика")
    features: list[FeatureOut] = Field(default_factory=list, max_length=5)
    secrets: list[SecretOut] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("features", (feature.id for feature in self.features))
        _require_unique("secrets", (secret.id for secret in self.secrets))
        return self


# --- generator.npcs ---


class NpcOut(_JournalOut):
    id: EntityId
    name: Name
    role: Line
    appearance: Line
    manner: Line
    motive: Line
    secret: OptionalLine = ""
    knows: list[Line] = Field(default_factory=list, max_length=6)
    attitude: int = Field(default=0, ge=-3, le=3)
    location_id: EntityId
    statblock: EntityId | None = Field(
        default=None, description="id монстра SRD или своего статблока, если NPC дерётся"
    )


class NpcsOut(_Out):
    npcs: list[NpcOut] = Field(max_length=MAX_NPCS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("npcs", (npc.id for npc in self.npcs))
        return self


# --- generator.relations ---


class RelationOut(_Out):
    id: EntityId
    a: EndRef
    b: EndRef
    kind: RelationKind = Field(description="Связь читается «a — kind для b»")
    note: OptionalLine = ""
    secret: bool = Field(default=False, description="Игроки о связи не знают")
    mutual: bool = Field(default=True, description="Связь верна и в обратную сторону")

    @model_validator(mode="after")
    def _other_needs_a_note(self) -> Self:
        if self.kind == "other" and not self.note:
            raise ValueError("note: у связи kind other пояснение обязательно")
        return self


class RelationsOut(_Out):
    relations: list[RelationOut] = Field(max_length=MAX_RELATIONS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("relations", (relation.id for relation in self.relations))
        return self


# --- generator.encounters ---


class MonsterGroupOut(_Out):
    monster_id: EntityId = Field(description="id монстра SRD из списка или своего статблока")
    count: int = Field(ge=1, le=20)


class EncounterOut(_Out):
    id: EntityId
    location_id: EntityId
    trigger: Text
    tactics: Text
    monsters: list[MonsterGroupOut] = Field(min_length=1, max_length=4)
    boss: bool = Field(default=False, description="Финальный или ключевой бой")


class EncountersOut(_Out):
    encounters: list[EncounterOut] = Field(max_length=MAX_ENCOUNTERS)
    monsters: list[CustomMonster] = Field(
        default_factory=list,
        max_length=MAX_CUSTOM_MONSTERS,
        description="Свои статблоки: только когда в SRD нет подходящего монстра",
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _require_unique("encounters", (encounter.id for encounter in self.encounters))
        # У локации один бой (`Location.encounter_id`).
        _require_unique("encounters.location_id", (e.location_id for e in self.encounters))
        _require_unique("monsters", (monster.id for monster in self.monsters))
        return self


# --- generator.items ---


class ItemOut(_JournalOut):
    id: EntityId
    name: Name
    description: Text
    base_item: EntityId | None = Field(default=None, description="id предмета SRD для механики")
    holder: HolderRef = Field(description='"location:<id>" или "npc:<id>"')


class ItemsOut(_Out):
    items: list[ItemOut] = Field(max_length=MAX_ITEMS)

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
