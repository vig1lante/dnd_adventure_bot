"""Схема приключения (спека §6.1): неизменяемые модели папки `content/adventures/<slug>/`.

Модели описывают только форму данных. Ссылки между сущностями (несуществующий id,
достижимость локаций, число улик) проверяет валидатор приключения, а не схема.
Текст приключения — данные, а не инструкции: в схеме нет полей произвольного типа, поэтому
вложить в файл можно только то, что описано ниже.
"""

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from dnd_engine.abilities import Ability, Skill
from dnd_engine.checks import VALID_DCS
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.content import MonsterDef, _SrdModel
from dnd_engine.state import RulesRef

# id сущности становится именем файла (`locations/<id>.yaml`), а новые id придумывает модель:
# только безопасные символы и ограниченная длина, чтобы id не мог выйти из папки приключения.
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
EntityId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
_ENTITY_ID = TypeAdapter(EntityId)


def is_entity_id(value: str) -> bool:
    """Подходит ли строка под шаблон id: так загрузчик перед записью проверяет имена файлов."""
    try:
        _ENTITY_ID.validate_python(value)
    except ValidationError:
        return False
    return True


# "location:<id>" | "npc:<id>" | "character:<id>" | "gone"
Holder = str

COLLECTIONS = ("locations", "npcs", "items", "encounters", "revelations", "monsters")


def _require_table_dc(dc: int) -> int:
    if dc not in VALID_DCS:
        raise ValueError(f"Сл должна быть одной из {sorted(VALID_DCS)}, получено {dc}")
    return dc


# Сл — только из таблицы 5/10/15/20/25/30, как и в инструментах мастера.
TableDC = Annotated[int, AfterValidator(_require_table_dc)]


def _forbid_extra(model: type[BaseModel]) -> BeforeValidator:
    """Запрещает лишние поля у модели движка, которая сама их молча отбрасывает."""

    def check(value: Any) -> Any:
        if isinstance(value, dict):
            extra = sorted(str(key) for key in value.keys() - model.model_fields.keys())
            if extra:
                # Без этого опечатка (`module` вместо `modules`) молча потеряла бы данные.
                raise ValueError(f"лишние поля: {', '.join(extra)}")
        return value

    return BeforeValidator(check)


StrictZone = Annotated[Zone, _forbid_extra(Zone)]
StrictZoneLink = Annotated[ZoneLink, _forbid_extra(ZoneLink)]
StrictRulesRef = Annotated[RulesRef, _forbid_extra(RulesRef)]


class _AdventureModel(_SrdModel):
    """Основа моделей приключения: та же, что у данных SRD (`extra="forbid"`, `frozen`).

    Заодно она делает словари-коллекции только для чтения: `frozen` защищает поля, но не
    содержимое словарей, а приключение не должно меняться во время игры.
    """


class CheckSpec(_AdventureModel):
    ability: Ability
    skill: Skill | None = None
    dc: TableDC


class RevealRef(_AdventureModel):
    kind: Literal["clue", "item", "flag", "exit"]
    id: str
    value: str | int | bool | None = None


class Feature(_AdventureModel):
    id: EntityId
    name: str
    description: str
    check: CheckSpec | None = None
    reveals: tuple[RevealRef, ...] = ()
    hidden: bool = False
    locked_by: str | None = None  # "item:<id>" | "flag:<name>", как у выхода (запертый сундук)


class Exit(_AdventureModel):
    id: EntityId
    to: str
    direction: str
    description: str
    locked_by: str | None = None  # "item:<id>" | "flag:<name>"
    hidden_dc: TableDC | None = None  # скрытый выход ищется Вниманием или Анализом


class Location(_AdventureModel):
    id: EntityId
    name: str
    region: str = ""
    read_aloud: str
    dm_notes: str = ""
    exits: tuple[Exit, ...] = ()
    features: tuple[Feature, ...] = ()
    encounter_id: str | None = None
    zones: tuple[StrictZone, ...] = (Zone(id="here", name="здесь"),)
    links: tuple[StrictZoneLink, ...] = ()


class Relation(_AdventureModel):
    npc_id: str
    kind: str  # "sibling", "parent", "enemy", "debtor", "lover", ...
    note: str = ""


class Npc(_AdventureModel):
    id: EntityId
    name: str
    role: str
    appearance: str
    manner: str
    motive: str
    secret: str = ""
    knows: tuple[str, ...] = ()
    attitude: int = Field(
        default=0, ge=-3, le=3
    )  # тот же диапазон, что у `WorldState.npc_attitude`
    relations: tuple[Relation, ...] = ()
    location_id: str
    statblock: str | None = None  # id монстра SRD или кастомного


class Item(_AdventureModel):
    id: EntityId
    name: str
    description: str
    base_item: str | None = None  # id предмета SRD
    holder: Holder


class MonsterGroup(_AdventureModel):
    monster_id: str
    count: int = Field(default=1, ge=1)
    zone: str = "here"


class Encounter(_AdventureModel):
    id: EntityId
    location_id: str
    trigger: str
    tactics: str
    monsters: tuple[MonsterGroup, ...]
    pc_zone: str = "here"


class Clue(_AdventureModel):
    id: EntityId
    description: str
    where: str  # "feature:<id>" | "npc:<id>" | "item:<id>"


class Revelation(_AdventureModel):
    id: EntityId
    statement: str
    clues: tuple[Clue, ...]


class Act(_AdventureModel):
    id: EntityId
    title: str
    summary: str
    key_scenes: tuple[str, ...] = ()


class Adventure(_AdventureModel):
    id: EntityId
    title: str
    pitch: str
    tone: str
    # Конечное положительное число: на него делит оценка времени игры в валидаторе.
    target_hours: float = Field(gt=0, allow_inf_nan=False)
    party_level: int = Field(default=3, ge=1)
    party_size: int = Field(default=6, ge=1)
    rules: StrictRulesRef
    start_location: str
    finale_location: str
    acts: tuple[Act, ...]
    endings: tuple[str, ...]
    locations: Mapping[str, Location]
    npcs: Mapping[str, Npc]
    items: Mapping[str, Item]
    encounters: Mapping[str, Encounter]
    revelations: Mapping[str, Revelation]
    monsters: Mapping[str, MonsterDef] = {}  # кастомные статблоки
    seeds: tuple[str, ...] = ()  # id сущностей, заданных автором вручную

    @model_validator(mode="after")
    def _check_collection_keys(self) -> Self:
        """Ключ коллекции равен id сущности; id кастомного монстра безопасен как имя файла.

        У `MonsterDef` (данные SRD) шаблона id нет, поэтому для кастомных монстров его
        проверяем здесь: файл `monsters/<id>.yaml` не должен выйти из папки приключения.
        """
        problems = [
            f"{name}: ключ {key!r} не совпадает с id {entity.id!r}"
            for name in COLLECTIONS
            for key, entity in getattr(self, name).items()
            if key != entity.id
        ]
        problems += [
            f"monsters: id {monster.id!r} монстра не подходит под {ID_PATTERN}"
            for monster in self.monsters.values()
            if not is_entity_id(monster.id)
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self
