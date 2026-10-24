"""Схема приключения (спека §6.1, формат v2 — спека DND-2 §8): неизменяемые модели папки
`content/adventures/<slug>/`.

Модели описывают только форму данных. Ссылки между сущностями (несуществующий id,
достижимость локаций, число улик) проверяет валидатор приключения, а не схема; схема проверяет
только форму ссылки v2 (`npc:<id>`, `faction:<id>`, `deity:<id>`, `location:<id>`).
Текст приключения — данные, а не инструкции: в схеме нет полей произвольного типа, поэтому
вложить в файл можно только то, что описано ниже.

Формат v2 (решение Р8): `Adventure` всегда хранит текущую версию. Данные без `format_version` —
формат v1: при проверке схемы связи из `npcs[].relations` переходят в общий граф `relations`
(`upgrade_v1`), поле NPC пустеет. Так одинаково читаются папка приключения и копия в начатой игре
(`games.adventure_data`), каким бы путём её ни разбирали.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self, get_args

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

# Ссылка формата v2 (конец связи, злодей, лидер фракции): вид и id по шаблону.
REF_KINDS = ("npc", "faction", "deity", "location")
Ref = Annotated[str, StringConstraints(pattern=rf"^({'|'.join(REF_KINDS)}):{ID_PATTERN[1:]}")]

# Коллекции-словари «id → сущность»; у каждой сущности свой файл, кроме открытий.
COLLECTIONS = (
    "locations",
    "npcs",
    "items",
    "encounters",
    "revelations",
    "monsters",
    "deities",
    "factions",
)

FORMAT_VERSION = 2


def is_format_version(value: Any, version: int) -> bool:
    """Версия ли `value` формата `version`: только целое число — `2.0`, `"2"` и `true` не версия."""
    return type(value) is int and value == version


def _require_current_version(value: Any) -> Any:
    # Поле видит версию уже после перевода v1, то есть 2; lax-проверка `Literal` приняла бы и `2.0`.
    if not is_format_version(value, FORMAT_VERSION):
        raise ValueError(f"версия формата — целое число 1 или {FORMAT_VERSION} (без поля — 1)")
    return value


# Версия, которую хранит модель: всегда текущая и только целым числом.
CurrentVersion = Annotated[Literal[2], BeforeValidator(_require_current_version)]


# Словарь видов связи (спека §8): родня, супруги, любовники, друзья, союзники, соперники, враги,
# должник, покровитель, слуга, поклоняется, предал, ищет; иначе `other` с пояснением в `note`.
# Значения — машинные имена, как у остальных перечислений схемы: из них же собирается id связи.
# Связь читается «a — <вид> для b»: `fabian debtor marta` — «Фабиан — должник Марты».
RelationKind = Literal[
    "kin",
    "spouse",
    "lover",
    "friend",
    "ally",
    "rival",
    "enemy",
    "debtor",
    "patron",
    "servant",
    "worships",
    "betrayed",
    "seeks",
    "other",
]


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


class _JournalEntity(_AdventureModel):
    """Поля дневника отряда (спека DND-2 §8, §12): `public` — что могут знать игроки, без тайн
    (пусто — запись строится из названия); `known_at_start` — известно отряду с начала игры."""

    public: str = ""
    known_at_start: bool = False


class LocationSecret(_AdventureModel):
    """Тайна места для мастера: раскрывается проверкой (Сл из таблицы) или событием сюжета."""

    id: EntityId
    text: str
    check: CheckSpec | None = None
    reveals: tuple[RevealRef, ...] = ()


class Location(_JournalEntity):
    id: EntityId
    name: str
    region: str = ""
    read_aloud: str
    dm_notes: str = ""
    exits: tuple[Exit, ...] = ()
    features: tuple[Feature, ...] = ()
    secrets: tuple[LocationSecret, ...] = ()
    encounter_id: str | None = None
    zones: tuple[StrictZone, ...] = (Zone(id="here", name="здесь"),)
    links: tuple[StrictZoneLink, ...] = ()


class NpcRelation(_AdventureModel):
    """Связь формата v1 внутри NPC: кем владельцу приходится `npc_id` (у Марты
    `{npc_id: fabian, kind: debtor}` — «Фабиан — её должник»). В v2 связи лежат в графе."""

    # id по шаблону: иначе связь ушла бы в граф и ошибка показала бы граф, а не поле NPC автора.
    npc_id: EntityId
    kind: str  # свободная строка: "sibling", "parent", "enemy", "debtor", "lover", ...
    note: str = ""


class Npc(_JournalEntity):
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
    relations: tuple[NpcRelation, ...] = ()  # только формат v1; при чтении переходят в граф
    location_id: str
    statblock: str | None = None  # id монстра SRD или кастомного


class Item(_JournalEntity):
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
    boss: bool = False  # финальный или ключевой бой: валидатор ждёт от него «высокой» сложности


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


class Region(_JournalEntity):
    id: EntityId
    name: str
    description: str


class Rumor(_JournalEntity):
    text: str
    true: bool  # правдив ли слух: знает только мастер

    @model_validator(mode="before")
    @classmethod
    def _bare_true_key(cls, data: Any) -> Any:
        """YAML читает ключ `true:` без кавычек как логическое `True`, а автор пишет поле по
        спеке: такой ключ — это поле `true`."""
        if isinstance(data, Mapping) and True in data and "true" not in data:
            return {("true" if key is True else key): value for key, value in data.items()}
        return data


class WorldInfo(_AdventureModel):
    """Мир приключения (`world.yaml`): краткая история, края и слухи."""

    history: str = ""
    regions: tuple[Region, ...] = ()
    rumors: tuple[Rumor, ...] = ()


class Deity(_JournalEntity):
    id: EntityId
    name: str
    title: str
    domains: tuple[str, ...]
    symbol: str
    dogma: str
    appearance: str
    attitude_to_mortals: str
    secret: str = ""
    avatar_statblock: str | None = None  # id монстра SRD или кастомного


class Faction(_JournalEntity):
    id: EntityId
    name: str
    goal: str
    methods: str
    leader: Ref | None = None  # ссылка на NPC
    base_location: EntityId | None = None  # id локации
    secret: str = ""
    attitude_to_party: int = Field(default=0, ge=-3, le=3)  # ступени, как у отношения NPC


class Relation(_AdventureModel):
    """Связь общего графа между NPC, фракциями, богами и местами: «a — <kind> для b».

    `secret` — игроки о связи не знают (признак для мастера); `mutual` — связь верна и в обратную
    сторону. Разные концы и существование ссылок проверяет валидатор."""

    id: EntityId
    a: Ref
    b: Ref
    kind: RelationKind
    note: str = ""
    secret: bool = False
    mutual: bool = True

    @model_validator(mode="after")
    def _other_needs_a_note(self) -> Self:
        if self.kind == "other" and not self.note.strip():
            raise ValueError("note: у связи kind other пояснение обязательно")
        return self


# --- перевод формата v1 в v2 ---

# Вид связи v1 (свободная строка) → (вид v2, уточнение для `note`). Вид, который уже есть в словаре
# v2, переходит как есть; незнакомый становится `other`, а сам он идёт в `note`.
V1_RELATION_KINDS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "sibling": ("kin", "брат или сестра"),
        "brother": ("kin", "брат"),
        "sister": ("kin", "сестра"),
        "parent": ("kin", "родитель"),
        "father": ("kin", "отец"),
        "mother": ("kin", "мать"),
        "child": ("kin", "ребёнок"),
        "son": ("kin", "сын"),
        "daughter": ("kin", "дочь"),
        "relative": ("kin", "родственник"),
        "husband": ("spouse", "муж"),
        "wife": ("spouse", "жена"),
        "creditor": ("other", "кредитор"),
    }
)
# Длина основы id связи из v1: остальное (до 64 символов id) — место для номера повтора «-<n>».
_MAX_GRAPH_ID_BASE = 56
_NPC_RELATIONS = TypeAdapter(tuple[NpcRelation, ...])


def _v2_kind(v1_kind: str) -> tuple[str, str]:
    """Вид v2 и уточнение для связи v1 по таблице `V1_RELATION_KINDS`."""
    key = v1_kind.strip().casefold()
    if key in V1_RELATION_KINDS:
        return V1_RELATION_KINDS[key]
    if key in get_args(RelationKind) and key != "other":
        return key, ""
    return "other", v1_kind.strip() or "связь без типа"


def _free_id(base: str, taken: set[str]) -> str:
    """Свободный id из `base` (обрезанного под шаблон), при повторе — с номером: `base-2`,
    `base-3`…; выданный id заносится в `taken`."""
    base = base[:_MAX_GRAPH_ID_BASE]
    ident, number = base, 1
    while ident in taken:
        number += 1
        ident = f"{base}-{number}"
    taken.add(ident)
    return ident


def _graph_relation(owner: str, relation: NpcRelation, taken: set[str]) -> dict[str, Any]:
    """Связь v1 у NPC `owner` — связь графа «npc_id — <вид> для owner». v1 говорит только о
    стороне владельца, поэтому связь не взаимная."""
    kind, detail = _v2_kind(relation.kind)
    return {
        "id": _free_id(f"{relation.npc_id}-{kind}-{owner}", taken),
        "a": f"npc:{relation.npc_id}",
        "b": f"npc:{owner}",
        "kind": kind,
        "note": "; ".join(part for part in (detail, relation.note.strip()) if part),
        "mutual": False,
    }


def _npc_data(npc: Any) -> Any:
    """NPC как «сырые» данные: модель `Npc` (так её передаёт генератор сервера) — словарём."""
    return npc.model_dump() if isinstance(npc, Npc) else npc


def _npc_relations(npc: Any) -> tuple[str, tuple[NpcRelation, ...]] | None:
    """(id владельца, связи v1) NPC; `None`, если данные NPC или его связи не по форме: тогда
    ошибку с точным путём покажет проверка поля NPC."""
    if not isinstance(npc, Mapping) or not isinstance(npc.get("id"), str):
        return None
    try:
        return npc["id"], _NPC_RELATIONS.validate_python(npc.get("relations", ()))
    except ValidationError:
        return None


def _given_relation_ids(given: Any) -> set[str]:
    """id уже заданного графа: словари (копия, генератор) или модели `Relation` (папка v1 с
    relations.yaml). Запись не по форме пропускается — её ошибку покажет проверка поля."""
    if not isinstance(given, list | tuple):
        return set()
    ids = (
        entry.id if isinstance(entry, Relation) else entry.get("id")
        for entry in given
        if isinstance(entry, Relation | Mapping)
    )
    return {ident for ident in ids if isinstance(ident, str)}


def upgrade_v1(data: Mapping[str, Any]) -> dict[str, Any]:
    """Данные формата v1 → v2: связи `npcs[].relations` переходят в граф `relations` (после уже
    заданных), поле NPC пустеет. Исходные данные не меняются: это может быть копия игры из базы."""
    upgraded = {**data, "format_version": FORMAT_VERSION}
    npcs = data.get("npcs")
    if not isinstance(npcs, Mapping):
        return upgraded  # не словарь — ошибку покажет проверка поля
    given = data.get("relations", ())
    moved: list[dict[str, Any]] = []
    # Перенесённая связь не берёт id заданного графа: автор не получит повтор id, которого не писал.
    taken = _given_relation_ids(given)
    new_npcs: dict[Any, Any] = {}
    for key, npc in npcs.items():
        raw = _npc_data(npc)
        found = _npc_relations(raw)
        if found is None or not found[1]:
            new_npcs[key] = npc
            continue
        owner, relations = found
        moved += [_graph_relation(owner, relation, taken) for relation in relations]
        new_npcs[key] = {**raw, "relations": []}
    if moved:
        upgraded["npcs"] = new_npcs
        # Не список — значит, ошибка формы: её покажет проверка поля, а связи не теряются молча.
        upgraded["relations"] = [*given, *moved] if isinstance(given, list | tuple) else given
    return upgraded


def _require_graph_relations(npcs: Any) -> None:
    """В формате v2 связи лежат только в графе: связи внутри NPC — ошибка, а не молчаливый перенос."""
    if not isinstance(npcs, Mapping):
        return
    owners = [
        str(key)
        for key, npc in npcs.items()
        if (found := _npc_relations(_npc_data(npc))) is not None and found[1]
    ]
    if owners:
        raise ValueError(
            f"npcs ({', '.join(owners)}): в формате v2 связи лежат в общем графе relations "
            "(relations.yaml), а не в npcs[].relations"
        )


class Adventure(_AdventureModel):
    format_version: CurrentVersion = FORMAT_VERSION
    id: EntityId
    title: str
    pitch: str
    journal_intro: str = ""  # главное в начале дневника: завязка и цель отряда; пусто — `pitch`
    tone: str
    # Конечное положительное число: на него делит оценка времени игры в валидаторе.
    target_hours: float = Field(gt=0, allow_inf_nan=False)
    party_level: int = Field(default=3, ge=1)
    party_size: int = Field(default=6, ge=1)
    rules: StrictRulesRef
    start_location: str
    finale_location: str
    villain: Ref | None = None  # злодей сюжета (NPC или бог), виден мастеру
    acts: tuple[Act, ...]
    endings: tuple[str, ...]
    locations: Mapping[str, Location]
    npcs: Mapping[str, Npc]
    items: Mapping[str, Item]
    encounters: Mapping[str, Encounter]
    revelations: Mapping[str, Revelation]
    world: WorldInfo = WorldInfo()
    deities: Mapping[str, Deity] = {}
    factions: Mapping[str, Faction] = {}
    relations: tuple[Relation, ...] = ()  # общий граф связей
    monsters: Mapping[str, MonsterDef] = {}  # кастомные статблоки
    seeds: tuple[str, ...] = ()  # id сущностей, заданных автором вручную

    @model_validator(mode="before")
    @classmethod
    def _read_format(cls, data: Any) -> Any:
        """Версия формата: без поля (или 1) — v1, он переводится в v2; в v2 связи только в графе.
        Неизвестную версию (и не целое: `2.0`, `"2"`, `true`) отвергает само поле. Пустое
        `journal_intro` — это `pitch`."""
        if not isinstance(data, Mapping):
            return data
        version = data.get("format_version", 1)
        if is_format_version(version, 1):
            data = upgrade_v1(data)
        elif is_format_version(version, FORMAT_VERSION):
            _require_graph_relations(data.get("npcs"))
        if not data.get("journal_intro") and isinstance(data.get("pitch"), str):
            data = {**data, "journal_intro": data["pitch"]}
        return data

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
