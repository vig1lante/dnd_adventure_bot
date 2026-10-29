"""Разделы черновика в конструкторе на сайте (планы B4.1, B5.1, B6.1; спека §10.1, §10.3;
дополнения dispatcher B4 §2, §3, §7, §8, B5 §1–§5 и B6 §1–§5).

Чистые функции без базы и сети над черновиком — контрактом B3 (`AuthoringDraft`), чья форма здесь
не меняется:
- раздел ↔ шаг генерации (`SECTION_STEPS`): автор правит замысел, мир, богов, фракции, сюжет (B4),
  карту с наполнением локаций, NPC, связи и предметы (B5), бои, свои статблоки и откровения (B6);
  генерировать можно все шаги генератора (`EDITOR_STEPS`);
- `apply_section` — данные раздела от автора, проверенные схемой раздела с пределами API и
  правилами черновика целиком, в новом черновике; замки сущностей, которых в разделе больше нет,
  снимаются; отказ — `InvalidSection` со строками `problems`;
- `limit_problems` — те же пределы у разделов, которые записал шаг генерации;
- `step_statuses` — «пусто / черновик / готово» по каждому шагу генератора;
- `draft_report` — проверка (`pipeline.check`) по заполненным разделам и доля готовности.

Текст автора — недоверенный ввод: в строки проблем значения входа не попадают, только путь поля и
русское сообщение по виду ошибки (как `llm.base.schema_problems`, но по-русски: дополнение §4).
Пределы текстов ручных разделов — те же, что у схем ответа модели (дополнение §7): что дала
генерация, автор сохранит обратно без правки, а длиннее ответа модели раздел не станет.
"""

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Self, get_args

from annotated_types import MaxLen
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from dnd_engine.adventure.budget import XP_BUDGET_PER_CHARACTER
from dnd_engine.adventure.schema import (
    ID_PATTERN,
    Act,
    CheckSpec,
    Clue,
    Deity,
    Encounter,
    EntityId,
    Exit,
    Faction,
    Feature,
    Item,
    Location,
    LocationSecret,
    MonsterGroup,
    Npc,
    NpcRelation,
    Region,
    Relation,
    RevealRef,
    Revelation,
    Rumor,
    WorldInfo,
)
from dnd_engine.adventure.validator import MAX_MONSTERS_PER_ENCOUNTER, Issue, Report
from dnd_engine.checks import VALID_DCS
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.content import MonsterAction, MonsterDef, load_srd
from dnd_engine.modules import MODULES
from dnd_server.adventures import shorten_problems
from dnd_server.authoring import schemas
from dnd_server.authoring.cli import MAX_PLAYERS
from dnd_server.authoring.draft import (
    SECTION_KINDS,
    STEPS,
    AuthoringDraft,
    DraftSettings,
    Plot,
    to_adventure,
)
from dnd_server.authoring.pipeline import check, owner_step
from dnd_server.authoring.steps import STEP_SECTIONS
from dnd_server.llm.base import MAX_LOC_PART_CHARS, printable

# Раздел конструктора -> шаг генератора, который его пишет (дополнения B4 §2, B5 §1 и B6 §1).
# Раздел карты пишет и наполнение локаций (`location_contents`), бои и свои статблоки — шаг боёв.
SECTION_STEPS = {
    "settings": "concept",
    "world": "world",
    "deities": "pantheon",
    "factions": "factions",
    "plot": "plot",
    "locations": "locations",
    "npcs": "npcs",
    "relations": "relations",
    "items": "items",
    "encounters": "encounters",
    "monsters": "encounters",
    "revelations": "clues",
}
# Шаги, которые конструктор генерирует: после B6 — все шаги генератора (дополнение B6 §1).
EDITOR_STEPS = STEPS
# Обязательные шаги для доли готовности (спека §10.3, дополнение §8).
REQUIRED_STEPS = (
    "concept",
    "plot",
    "locations",
    "location_contents",
    "npcs",
    "encounters",
    "clues",
)

# Пределы текста замысла на границе API (дополнение §7).
MAX_TITLE = 100
MAX_PITCH = 2000
MAX_TONE = 300
MAX_JOURNAL_INTRO = 2000
LEVELS = sorted(XP_BUDGET_PER_CHARACTER)
# Списки строк, у которых нет аналога в схемах ответа (сцены акта, сферы бога), — дополнение §7.
MAX_LIST_LINES = 12
# Своих статблоков на черновик (дополнение B6 §5): ответ шага боёв несёт до трёх, а повторные цели
# боя добавляют новые — без предела черновика они копились бы.
MAX_DRAFT_MONSTERS = 6


class InvalidSection(ValueError):
    """Данные раздела не прошли проверку: `problems` — строки «<путь>: <сообщение>»."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("данные раздела не прошли проверку")
        self.problems = problems


def _budget_level(level: int) -> int:
    # Уровни, для которых у движка есть бюджет опыта боёв, — те же, что у флага `--level` CLI.
    if level not in XP_BUDGET_PER_CHARACTER:
        raise ValueError(f"уровень героев — от {LEVELS[0]} до {LEVELS[-1]}")
    return level


def _known_module(name: str) -> str:
    if name not in MODULES:
        raise ValueError(f"неизвестный модуль правил; доступны: {', '.join(MODULES)}")
    return name


class SettingsData(BaseModel):
    """Раздел «Замысел» (дополнение §2): настройки черновика, а ещё название и главное для дневника,
    которые лежат на верхнем уровне черновика. Без названия или главного для дневника они остаются
    прежними."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=MAX_TITLE)
    journal_intro: str | None = Field(default=None, max_length=MAX_JOURNAL_INTRO)
    pitch: str = Field(max_length=MAX_PITCH)
    tone: str = Field(max_length=MAX_TONE)
    target_hours: float = Field(gt=0, allow_inf_nan=False)  # как флаг `--hours` CLI
    party_size: int = Field(ge=1, le=MAX_PLAYERS)  # как флаг `--players` CLI
    party_level: Annotated[int, AfterValidator(_budget_level)]
    # Повторы модулей схлопываются: порядок первого вхождения сохраняется (дополнение §7).
    modules: Annotated[
        tuple[Annotated[str, AfterValidator(_known_module)], ...],
        AfterValidator(lambda names: tuple(dict.fromkeys(names))),
    ]
    profanity: bool
    # id приключения ручным автором (дополнение B6 §2): шаблон `EntityId`; пусто — не задан. Поля
    # нет в теле — id остаётся прежним (клиенты B4/B5 его не шлют).
    adventure_id: Annotated[
        EntityId | None, BeforeValidator(lambda value: None if value == "" else value)
    ] = None


def _answer_limit(model: type[BaseModel], field: str) -> int:
    """Предел длины списка в схеме ответа модели (`schemas`): раздел, написанный вручную, не длиннее
    того, что мог бы написать шаг генератора."""
    return next(
        rule.max_length for rule in model.model_fields[field].metadata if isinstance(rule, MaxLen)
    )


def _text_limit(text_type: Any) -> int:
    """Предел длины строки из типа схемы ответа модели (`schemas.Name`/`Line`/`Text`)."""
    return next(rule.max_length for rule in get_args(text_type)[1:] if rule.max_length)


# Строки ручных разделов: заголовок, одна строка и абзац — как у ответа модели (`schemas`).
NameText = Annotated[str, Field(max_length=_text_limit(schemas.Name))]
LineText = Annotated[str, Field(max_length=_text_limit(schemas.Line))]
LongText = Annotated[str, Field(max_length=_text_limit(schemas.Text))]
DirectionText = Annotated[str, Field(max_length=_text_limit(schemas.Direction))]


def _table_difficulty(dc: int) -> int:
    # Сообщение движка (`TableDC`) повторяет введённое число; автору хватает правила таблицы
    # (дополнение B5 §9).
    if dc not in VALID_DCS:
        raise ValueError(f"Сл — одна из {schemas.DC_VALUES}")
    return dc


# Сл проверки — только из таблицы движка, как `TableDC`, но с текстом без значения входа.
Difficulty = Annotated[int, AfterValidator(_table_difficulty)]


def _unique_ids(entities: tuple[Any, ...]) -> tuple[Any, ...]:
    # Та же проверка есть у черновика (`draft._Sections`), но там ошибка без пути раздела.
    repeated = [ident for ident, count in Counter(e.id for e in entities).items() if count > 1]
    if repeated:
        raise ValueError(f"id повторяются: {', '.join(repeated)}")
    return entities


# Список сущностей раздела: id не повторяются (отказ — с путём раздела, дополнение B5 §9).
UniqueIds = AfterValidator(_unique_ids)


# Модели разделов — модели движка с пределами полей; сами модели движка не меняются. Поле без
# аналога в схемах ответа — `LongText` или `LineText`, список строк без аналога — `MAX_LIST_LINES`.


class _Region(Region):
    name: NameText
    description: LongText
    public: LineText = ""


class _Rumor(Rumor):
    text: LineText
    public: LineText = ""


class _World(WorldInfo):
    history: LongText = ""
    regions: tuple[_Region, ...] = Field(
        default=(), max_length=_answer_limit(schemas.WorldOut, "regions")
    )
    rumors: tuple[_Rumor, ...] = Field(
        default=(), max_length=_answer_limit(schemas.WorldOut, "rumors")
    )


class _Deity(Deity):
    name: NameText
    title: NameText
    domains: tuple[NameText, ...] = Field(max_length=MAX_LIST_LINES)
    symbol: LineText
    dogma: LineText
    appearance: LineText
    attitude_to_mortals: LineText
    secret: LineText = ""
    public: LineText = ""
    avatar_statblock: LineText | None = None


class _Faction(Faction):
    name: NameText
    goal: LineText
    methods: LineText
    secret: LineText = ""
    public: LineText = ""


class _Act(Act):
    title: NameText
    summary: LongText
    key_scenes: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)


class _Plot(Plot):
    acts: tuple[_Act, ...] = Field(default=(), max_length=_answer_limit(schemas.PlotOut, "acts"))
    endings: tuple[LongText, ...] = Field(
        default=(), max_length=_answer_limit(schemas.PlotOut, "endings")
    )


# Разделы блока 5 (дополнение B5 §1, §4). Поля-ссылки движка, которые там свободные строки (куда
# ведёт выход, держатель предмета, бой локации), здесь только ограничены по длине: висящую или
# кривую ссылку автор увидит в проверке (§5), а не отказом записи.


class _Reveal(RevealRef):
    id: LineText
    value: LineText | int | bool | None = None


class _Check(CheckSpec):
    dc: Difficulty


class _Exit(Exit):
    to: LineText
    direction: DirectionText
    description: LineText
    locked_by: LineText | None = None
    hidden_dc: Difficulty | None = None


# Находки места (дополнение B6 §11, по отчёту B6.1 att.002). Предметы и флаги — не больше, чем даёт
# ответ модели (`FeatureOut.reveals`). Находки-улики дописывает сборка приключения из улик
# откровений (`clue.where = feature:<id>`), поэтому они в этом пределе не считаются, а ограничены
# пределами откровений: 5 откровений × 8 улик. Иначе черновик, прошедший пределы, после «экспорт →
# импорт» или копии опубликованного уже не проходил бы их.
MAX_PLACE_FINDS = _answer_limit(schemas.FeatureOut, "reveals")
MAX_PLACE_CLUES = _answer_limit(schemas.CluesOut, "revelations") * _answer_limit(
    schemas.RevelationCluesOut, "clues"
)


def _place_finds(reveals: tuple[_Reveal, ...]) -> tuple[_Reveal, ...]:
    clues = sum(reveal.kind == "clue" for reveal in reveals)
    if len(reveals) - clues > MAX_PLACE_FINDS:
        raise ValueError(f"не больше {MAX_PLACE_FINDS} находок, кроме улик")
    if clues > MAX_PLACE_CLUES:
        raise ValueError(f"не больше {MAX_PLACE_CLUES} находок-улик")
    return reveals


class _Feature(Feature):
    name: NameText
    description: LongText
    check: _Check | None = None
    reveals: Annotated[
        tuple[_Reveal, ...],
        Field(max_length=MAX_PLACE_FINDS + MAX_PLACE_CLUES),
        AfterValidator(_place_finds),
    ] = ()
    locked_by: LineText | None = None


class _Secret(LocationSecret):
    text: LongText
    check: _Check | None = None
    reveals: tuple[_Reveal, ...] = Field(
        default=(), max_length=_answer_limit(schemas.SecretOut, "reveals")
    )


class _Zone(Zone):
    # Зона боя — модель движка без запрета лишних полей: здесь лишнее поле — отказ, как везде.
    model_config = ConfigDict(extra="forbid")

    id: LineText
    name: NameText


class _ZoneLink(ZoneLink):
    model_config = ConfigDict(extra="forbid")

    a: LineText
    b: LineText


class _Location(Location):
    name: NameText
    region: LineText = ""
    read_aloud: LongText
    dm_notes: LongText = ""
    public: LineText = ""
    exits: tuple[_Exit, ...] = Field(
        default=(), max_length=_answer_limit(schemas.LocationNodeOut, "exits")
    )
    features: tuple[_Feature, ...] = Field(
        default=(), max_length=_answer_limit(schemas.LocationContentsOut, "features")
    )
    secrets: tuple[_Secret, ...] = Field(
        default=(), max_length=_answer_limit(schemas.LocationContentsOut, "secrets")
    )
    encounter_id: LineText | None = None
    # Умолчание — то же, что у движка: одна зона «здесь».
    zones: tuple[_Zone, ...] = Field(
        default=({"id": "here", "name": "здесь"},), max_length=MAX_LIST_LINES
    )
    links: tuple[_ZoneLink, ...] = Field(default=(), max_length=MAX_LIST_LINES)


class LocationsData(BaseModel):
    """Раздел «Карта» (дополнение B5 §1): локации и поля верхнего уровня черновика — старт и финал.
    Старт и финал — id локаций этого же списка или null."""

    model_config = ConfigDict(extra="forbid")

    locations: Annotated[tuple[_Location, ...], UniqueIds] = Field(max_length=schemas.MAX_LOCATIONS)
    start_location: str | None
    finale_location: str | None

    @field_validator("start_location", "finale_location")
    @classmethod
    def _on_the_map(cls, ident: str | None, info: ValidationInfo) -> str | None:
        # Локаций нет в `info.data`, если их список не прошёл проверку: ошибка уже названа.
        listed = info.data.get("locations")
        if ident is not None and listed is not None and all(loc.id != ident for loc in listed):
            raise ValueError("такой локации на карте нет")
        return ident


def _no_v1_relations(relations: Any) -> Any:
    # Связи формата v1 черновик молча перенёс бы в граф (`draft._Sections`): в конструкторе их место
    # — шаг «Связи» (дополнение B5 §1).
    if relations:
        raise ValueError("связи NPC — на шаге «Связи»")
    return relations


class _Npc(Npc):
    name: NameText
    role: LineText
    appearance: LineText
    manner: LineText
    motive: LineText
    secret: LineText = ""
    public: LineText = ""
    knows: tuple[LineText, ...] = Field(
        default=(), max_length=_answer_limit(schemas.NpcOut, "knows")
    )
    relations: Annotated[tuple[NpcRelation, ...], BeforeValidator(_no_v1_relations)] = ()
    location_id: LineText
    statblock: LineText | None = None


class _Relation(Relation):
    note: LineText = ""

    @field_validator("note")
    @classmethod
    def _note_of_other(cls, note: str, info: ValidationInfo) -> str:
        # Вида нет в `info.data`, если он не прошёл проверку: ошибка уже названа.
        if info.data.get("kind") == "other" and not note.strip():
            raise ValueError("у вида «другое» нужно пояснение")
        return note

    @model_validator(mode="after")
    def _other_needs_a_note(self) -> Self:
        # Одноимённый метод заменяет проверку движка: её текст («kind other») по-английски и без
        # пути поля, а то же правило по-русски и у поля проверяет `_note_of_other`.
        return self


class _Item(Item):
    name: NameText
    description: LongText
    public: LineText = ""
    base_item: LineText | None = None
    holder: LineText


# Разделы блока 6 (дополнение B6 §1, §5): бои, свои статблоки (модель движка `MonsterDef`) и
# откровения с уликами. Числа статблока — форма и конечность; разумность чисел, кости и опыт по cr
# проверяет проверка черновика (`pipeline.check`), а не отказ записи.


class _MonsterGroup(MonsterGroup):
    monster_id: LineText
    count: int = Field(default=1, ge=1, le=MAX_MONSTERS_PER_ENCOUNTER)
    zone: LineText = "here"


def _monsters_in_a_fight(groups: tuple[_MonsterGroup, ...]) -> tuple[_MonsterGroup, ...]:
    if sum(group.count for group in groups) > MAX_MONSTERS_PER_ENCOUNTER:
        raise ValueError(f"всего монстров в бою — не больше {MAX_MONSTERS_PER_ENCOUNTER}")
    return groups


class _Encounter(Encounter):
    location_id: LineText
    trigger: LongText
    tactics: LongText
    monsters: Annotated[
        tuple[_MonsterGroup, ...],
        Field(max_length=_answer_limit(schemas.EncounterOut, "monsters")),
        AfterValidator(_monsters_in_a_fight),
    ]
    pc_zone: LineText = "here"


class _Action(MonsterAction):
    id: LineText
    name: NameText
    damage: LineText | None = None
    damage_type: LineText | None = None
    condition: LineText | None = None
    multiattack: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)
    summary: LongText = ""
    secondary_damage: LineText | None = None
    secondary_damage_type: LineText | None = None


class _Monster(MonsterDef):
    # id своего статблока — имя файла `monsters/<id>.yaml`, поэтому шаблон id сущностей.
    id: EntityId
    name: NameText
    name_en: NameText
    cr: float = Field(ge=0, allow_inf_nan=False)
    xp: int = Field(ge=0)
    hp_dice: LineText
    resistances: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)
    vulnerabilities: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)
    immunities: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)
    actions: tuple[_Action, ...] = Field(max_length=MAX_LIST_LINES)
    traits: tuple[LongText, ...] = Field(default=(), max_length=MAX_LIST_LINES)
    tags: tuple[LineText, ...] = Field(default=(), max_length=MAX_LIST_LINES)


class _Clue(Clue):
    description: LongText
    where: LineText


class _Revelation(Revelation):
    statement: LongText
    clues: tuple[_Clue, ...] = Field(max_length=_answer_limit(schemas.RevelationCluesOut, "clues"))


# Схема данных каждого раздела: формы — модели B3 и движка (дополнения B4 §1, B5 §1, B6 §1),
# пределы — B4 §7, B5 §4 и B6 §5.
_SECTION_DATA: dict[str, TypeAdapter[Any]] = {
    "settings": TypeAdapter(SettingsData),
    "world": TypeAdapter(_World),
    "deities": TypeAdapter(
        Annotated[tuple[_Deity, ...], Field(max_length=schemas.MAX_DEITIES), UniqueIds]
    ),
    "factions": TypeAdapter(
        Annotated[tuple[_Faction, ...], Field(max_length=schemas.MAX_FACTIONS), UniqueIds]
    ),
    "plot": TypeAdapter(_Plot),
    "locations": TypeAdapter(LocationsData),
    "npcs": TypeAdapter(Annotated[tuple[_Npc, ...], Field(max_length=schemas.MAX_NPCS), UniqueIds]),
    "relations": TypeAdapter(
        Annotated[tuple[_Relation, ...], Field(max_length=schemas.MAX_RELATIONS), UniqueIds]
    ),
    "items": TypeAdapter(
        Annotated[tuple[_Item, ...], Field(max_length=schemas.MAX_ITEMS), UniqueIds]
    ),
    "encounters": TypeAdapter(
        Annotated[tuple[_Encounter, ...], Field(max_length=schemas.MAX_ENCOUNTERS), UniqueIds]
    ),
    "monsters": TypeAdapter(
        Annotated[tuple[_Monster, ...], Field(max_length=MAX_DRAFT_MONSTERS), UniqueIds]
    ),
    "revelations": TypeAdapter(
        Annotated[
            tuple[_Revelation, ...],
            Field(max_length=_answer_limit(schemas.CluesOut, "revelations")),
            UniqueIds,
        ]
    ),
}


# Сообщения частых ошибок pydantic по-русски (дополнение §4): по виду ошибки и её контексту
# (`ctx` — пределы схемы, а не значения входа).
_MESSAGES = {
    "missing": "обязательное поле",
    "extra_forbidden": "лишнее поле",
    "string_too_long": "не длиннее {max_length} знаков",
    "string_too_short": "не короче {min_length} знаков",
    "too_long": "не больше {max_length} элементов",
    "too_short": "не меньше {min_length} элементов",
    "less_than_equal": "не больше {le}",
    "less_than": "меньше {lt}",
    "greater_than_equal": "не меньше {ge}",
    "greater_than": "больше {gt}",
    "finite_number": "нужно конечное число",
    "json_invalid": "тело запроса — не JSON",
}
# Ожидаемый вид значения по началу кода `*_type` / `*_parsing`.
_KINDS = {
    "string": "нужна строка",
    "int": "нужно целое число",
    "float": "нужно число",
    "bool": "нужно «да» или «нет»",
    "list": "нужен список",
    "tuple": "нужен список",
    "set": "нужен список",
    "frozen_set": "нужен список",
    "dict": "нужен объект",
    "model": "нужен объект",
    "model_attributes": "нужен объект",
}
_ID_RULE = "только строчная латиница, цифры, «-» и «_», до 64 знаков"
_OWN = {"value_error": "Value error, ", "assertion_error": "Assertion failed, "}


def _message(error: Mapping[str, Any]) -> str:
    """Русское сообщение ошибки схемы; собственные русские сообщения проверок — как есть."""
    kind = str(error["type"])
    ctx = error.get("ctx") or {}
    if kind in _OWN:
        return str(error["msg"]).removeprefix(_OWN[kind])
    if kind in _MESSAGES:
        return _MESSAGES[kind].format(**ctx)
    if kind == "string_pattern_mismatch":
        if ctx.get("pattern") == ID_PATTERN:
            return _ID_RULE
        return f"нужна ссылка вида «вид:имя»; имя — {_ID_RULE}"
    for suffix in ("_type", "_parsing", "_from_float"):
        if kind.endswith(suffix):
            return _KINDS.get(kind.removesuffix(suffix), "неверный тип значения")
    return "значение не подходит"


def problems(errors: Iterable[Mapping[str, Any]], *where: str) -> list[str]:
    """Ошибки схемы строками «<путь>: <сообщение>» по-русски, без значений входа; `where` —
    начало пути.

    Путь проходит через `printable`, как в `schema_problems`: имя лишнего ключа приходит от
    клиента. Строк не больше, чем показывает отчёт создателю игры (`shorten_problems`)."""
    lines = []
    for error in errors:
        parts = (*where, *error["loc"])
        path = ".".join(printable(str(part), MAX_LOC_PART_CHARS) for part in parts)
        message = printable(_message(error))
        lines.append(f"{path}: {message}" if path else message)
    return shorten_problems(lines)


def apply_section(draft: AuthoringDraft, section: str, data: Any) -> AuthoringDraft:
    """Черновик, где раздел `section` (из `SECTION_STEPS`) заменён данными автора.

    Сначала данные проверяет схема раздела с пределами API, затем правила черновика целиком
    (id в разделе не повторяются); любой отказ — `InvalidSection`. Сущности, которых в разделе
    больше нет, теряют замки (`_locks_of_kept`)."""
    try:
        value = _SECTION_DATA[section].validate_python(data)
    except ValidationError as error:
        raise InvalidSection(problems(error.errors(include_input=False), section)) from None
    if section == "settings":
        update = _settings_update(value)
    elif section == "locations":
        # Поля «Карты» — сами поля верхнего уровня черновика: локации, старт и финал.
        update = _SECTION_DATA[section].dump_python(value)
    else:
        # Раздел — модели разделов с пределами; черновик собирает из них свои модели движка.
        update = {section: _SECTION_DATA[section].dump_python(value)}
    try:
        changed = AuthoringDraft.model_validate(draft.model_dump() | update)
    except ValidationError as error:
        raise InvalidSection(problems(error.errors(include_input=False))) from None
    return changed.model_copy(update={"locks": _locks_of_kept(draft, changed)})


def _entity_refs(draft: AuthoringDraft) -> set[str]:
    """Ссылки `<вид>:<id>` всех сущностей разделов-списков черновика."""
    return {
        f"{kind}:{entity.id}"
        for section, kind in SECTION_KINDS.items()
        for entity in getattr(draft, section)
    }


def _locks_of_kept(before: AuthoringDraft, after: AuthoringDraft) -> frozenset[str]:
    """Замки без замков удалённых сущностей (дополнение B5 §5): удаление — запись раздела без
    сущности, и новая сущность с тем же id не должна молча оказаться закреплённой. Замки шагов и
    сущностей, которых и раньше не было, не трогаются."""
    return before.locks - (_entity_refs(before) - _entity_refs(after))


def section_steps(section: str) -> list[str]:
    """Шаги, которые пишут раздел `section`: ручная запись раздела — запись каждого из них (время
    шага в `Draft.steps`). Карту пишут шаги `locations` и `location_contents`."""
    return [
        step
        for step in STEPS
        if step == SECTION_STEPS[section] or STEP_SECTIONS.get(step) == section
    ]


# Разделы, которые шаг генерации пишет сверх своего: Замысел пишет и открытия
# (`steps._merge_concept`, контракт B3), а это раздел шага «Улики» (дополнение B6 §11, R5).
_ALSO_WRITTEN = {"concept": ("revelations",)}


def steps_written_by(step: str) -> list[str]:
    """Шаги, чьё время отмечает результат шага генерации `step`: все шаги разделов, которые он
    пишет, как у ручной записи раздела (дополнения B4 §3, B5 §9). Шаг без раздела конструктора —
    только он сам."""
    own = next(
        (name for name, main in SECTION_STEPS.items() if main == step), STEP_SECTIONS.get(step)
    )
    sections = [own, *_ALSO_WRITTEN.get(step, ())]
    written = [
        name for section in sections if section in SECTION_STEPS for name in section_steps(section)
    ]
    return list(dict.fromkeys(written)) or [step]


def _settings_update(data: SettingsData) -> dict[str, Any]:
    update: dict[str, Any] = {
        "settings": DraftSettings(
            **{name: getattr(data, name) for name in DraftSettings.model_fields}
        )
    }
    if data.title is not None:
        update["title"] = data.title
    if data.journal_intro is not None:
        update["journal_intro"] = data.journal_intro
    if "adventure_id" in data.model_fields_set:
        update["id"] = data.adventure_id
    return update


def limit_problems(before: AuthoringDraft, after: AuthoringDraft) -> list[str]:
    """Нарушения пределов API у разделов, которые изменились от `before` к `after` (результат шага
    генерации, дополнение B6 §5): что не сохранил бы автор, не запишет и генерация. Разделы, которых
    шаг не трогал, не проверяются: черновик мог прийти из копии папки и быть больше."""
    old, new = _served_sections(before), _served_sections(after)
    changed = [section for section in _SECTION_DATA if new[section] != old[section]]
    found = _section_problems(new, changed)
    return shorten_problems([line for lines in found.values() for line in lines])


def section_problems(draft: AuthoringDraft) -> dict[str, list[str]]:
    """Нарушения пределов API во всех разделах черновика: `{раздел: строки «<раздел>.<путь>:
    <предел>»}` без значений. Те же схемы, что у ручной записи раздела: по ним импорт и публикация
    не пропускают то, что автор не смог бы сохранить (дополнение B6 §11, S2)."""
    return _section_problems(_served_sections(draft), list(_SECTION_DATA))


def _section_problems(served: Mapping[str, Any], sections: Iterable[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for section in sections:
        if served[section] is None:
            continue  # раздела нет (мир не задан) — проверять нечего
        try:
            _SECTION_DATA[section].validate_python(served[section])
        except ValidationError as error:
            found[section] = problems(error.errors(include_input=False), section)
    return found


def _served_sections(draft: AuthoringDraft) -> dict[str, Any]:
    """Данные каждого раздела в той форме, в какой их шлёт автор (`PUT .../sections/{section}`)."""
    data = draft.model_dump(mode="json")
    served = {section: data[section] for section in _SECTION_DATA}
    served["settings"] = data["settings"] | {
        "title": draft.title,
        "journal_intro": draft.journal_intro,
        "adventure_id": draft.id,
    }
    served["locations"] = {
        name: data[name] for name in ("locations", "start_location", "finale_location")
    }
    return served


# --- статусы шагов и проверка ---


def is_step_empty(draft: AuthoringDraft, step: str) -> bool:
    """Пуст ли шаг для автора (дополнения B4 §3, B5 §3): по содержимому раздела, а не по правилу
    генератора `steps.is_empty` («модель этот раздел ещё не писала»); закреплённое — тоже
    содержимое.

    Числа и флажки настроек Замысла имеют умолчания и содержимым не считаются. Наполнение локаций
    пусто, пока ни у одной локации нет наполнения (`_has_contents`). Бои пусты, пока нет ни боя, ни
    ссылки локации на бой: висящая ссылка — содержимое, её находку автор должен видеть. Улики пусты,
    пока ни у одного открытия нет улик (дополнение B6 §3). Остальные шаги — по своему разделу."""
    match step:
        case "concept":
            texts = (draft.title, draft.settings.pitch, draft.settings.tone, draft.journal_intro)
            return draft.id is None and not any(text.strip() for text in texts)
        case "world":
            world = draft.world
            return world is None or not (world.history.strip() or world.regions or world.rumors)
        case "plot":
            return not (draft.plot.acts or draft.plot.endings or draft.plot.villain)
        case "location_contents":
            return not any(_has_contents(draft, location) for location in draft.locations)
        case "encounters":
            return not (draft.encounters or any(loc.encounter_id for loc in draft.locations))
        case "clues":
            return not any(revelation.clues for revelation in draft.revelations)
    return not getattr(draft, STEP_SECTIONS[step])


def _has_contents(draft: AuthoringDraft, location: Location) -> bool:
    """Есть ли у локации наполнение (дополнение B5 §3): текст вслух или заметки мастера (не пустые
    после обрезки), детали, секреты или предмет, который лежит здесь."""
    here = f"location:{location.id}"
    return bool(
        location.read_aloud.strip()
        or location.dm_notes.strip()
        or location.features
        or location.secrets
        or any(item.holder == here for item in draft.items)
    )


def _check(draft: AuthoringDraft) -> Report:
    return check(to_adventure(draft), load_srd())


def step_statuses(draft: AuthoringDraft, times: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """`{шаг: {status, updated_at}}` по всем шагам генератора (дополнения B4 §3, B5 §3).

    `empty` — шаг пуст (`is_step_empty`); `draft` — не пуст, но в проверке есть ошибка, которую
    `pipeline.owner_step` относит к нему, или шагу не хватает своего (`_complete`); иначе `ready`.
    `times` — время последней записи раздела шага (ручной или генерацией)."""
    failing = {owner_step(issue) for issue in _check(draft).issues if issue.severity == "error"}
    return {
        step: {"status": _status(draft, step, failing), "updated_at": times.get(step)}
        for step in STEPS
    }


def _status(draft: AuthoringDraft, step: str, failing: set[str | None]) -> str:
    if is_step_empty(draft, step):
        return "empty"
    if step in failing or not _complete(draft, step):
        return "draft"
    return "ready"


def _complete(draft: AuthoringDraft, step: str) -> bool:
    """Есть ли у непустого шага всё своё, кроме ошибок проверки: у Замысла — id приключения (его
    даёт только генерация), у наполнения — текст вслух каждой локации."""
    match step:
        case "concept":
            return draft.id is not None
        case "location_contents":
            return all(location.read_aloud.strip() for location in draft.locations)
    return True


def draft_report(draft: AuthoringDraft) -> dict[str, Any]:
    """`DraftReport` (дополнение §8): находки проверки с шагом-владельцем, кроме находок пустых
    шагов (проверка идёт по заполненным разделам); балл и оценка времени — из отчёта проверки;
    доля готовности — непустые обязательные шаги, с ошибками не выше 99."""
    report = _check(draft)
    kept = [
        issue
        for issue in report.issues
        if (step := owner_step(issue)) is None or not is_step_empty(draft, step)
    ]
    errors = [_finding(issue) for issue in kept if issue.severity == "error"]
    warnings = [_finding(issue) for issue in kept if issue.severity != "error"]
    filled = sum(not is_step_empty(draft, step) for step in REQUIRED_STEPS)
    readiness = filled * 100 // len(REQUIRED_STEPS)
    return {
        "errors": errors,
        "warnings": warnings,
        "score": report.score,
        "estimated_minutes": report.estimated_minutes,
        "readiness_percent": min(readiness, 99) if errors else readiness,
    }


def _finding(issue: Issue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "message": finding_message(issue),
        "ref": issue.ref,
        "step": owner_step(issue),
    }


# Пустой старт или финал карты: валидатор говорит о ссылке «location:» в никуда, а автору понятнее
# «не выбран» (manual B5 nit, дополнение B6 §3). Формат текста валидатора — как в `pipeline`.
_UNSET_PLACE = re.compile(r"^(start|finale)_location: ссылка ['\"]location:['\"]")
_UNSET_TEXT = {"start": "Старт не выбран", "finale": "Финал не выбран"}


def finding_message(issue: Issue) -> str:
    """Текст находки для автора: у пустого старта и финала — понятная фраза, иначе текст
    проверки."""
    unset = _UNSET_PLACE.match(issue.message) if issue.code == "dangling_ref" else None
    return _UNSET_TEXT[unset.group(1)] if unset else issue.message
