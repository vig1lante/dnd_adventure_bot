"""Разделы черновика в конструкторе на сайте (план B4.1; спека §10.1, §10.3; дополнение dispatcher
B4 §2, §3, §7, §8).

Чистые функции без базы и сети над черновиком — контрактом B3 (`AuthoringDraft`), чья форма здесь
не меняется:
- раздел ↔ шаг генерации (`SECTION_STEPS`): в B4 автор правит замысел, мир, богов, фракции, сюжет;
- `apply_section` — данные раздела от автора, проверенные схемой раздела с пределами API и
  правилами черновика целиком, в новом черновике; отказ — `InvalidSection` со строками `problems`;
- `step_statuses` — «пусто / черновик / готово» по каждому шагу генератора;
- `draft_report` — проверка (`pipeline.check`) по заполненным разделам и доля готовности.

Текст автора — недоверенный ввод: в строки проблем значения входа не попадают, только путь поля и
русское сообщение по виду ошибки (как `llm.base.schema_problems`, но по-русски: дополнение §4).
Пределы текстов ручных разделов — те же, что у схем ответа модели (дополнение §7): что дала
генерация, автор сохранит обратно без правки, а длиннее ответа модели раздел не станет.
"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, get_args

from annotated_types import MaxLen
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from dnd_engine.adventure.budget import XP_BUDGET_PER_CHARACTER
from dnd_engine.adventure.schema import (
    ID_PATTERN,
    Act,
    Deity,
    Faction,
    Region,
    Rumor,
    WorldInfo,
)
from dnd_engine.adventure.validator import Issue, Report
from dnd_engine.content import load_srd
from dnd_engine.modules import MODULES
from dnd_server.adventures import shorten_problems
from dnd_server.authoring import schemas
from dnd_server.authoring.cli import MAX_PLAYERS
from dnd_server.authoring.draft import STEPS, AuthoringDraft, DraftSettings, Plot, to_adventure
from dnd_server.authoring.pipeline import check, owner_step
from dnd_server.authoring.steps import STEP_SECTIONS, is_empty
from dnd_server.llm.base import MAX_LOC_PART_CHARS, printable

# Раздел конструктора -> шаг генератора, который его пишет (дополнение §2: разделы и шаги B4).
SECTION_STEPS = {
    "settings": "concept",
    "world": "world",
    "deities": "pantheon",
    "factions": "factions",
    "plot": "plot",
}
EDITOR_STEPS = tuple(SECTION_STEPS.values())
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


# Схема данных каждого раздела B4: формы — модели B3 и движка (дополнение §1), пределы — §7.
_SECTION_DATA: dict[str, TypeAdapter[Any]] = {
    "settings": TypeAdapter(SettingsData),
    "world": TypeAdapter(_World),
    "deities": TypeAdapter(Annotated[tuple[_Deity, ...], Field(max_length=schemas.MAX_DEITIES)]),
    "factions": TypeAdapter(
        Annotated[tuple[_Faction, ...], Field(max_length=schemas.MAX_FACTIONS)]
    ),
    "plot": TypeAdapter(_Plot),
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
    (id в разделе не повторяются); любой отказ — `InvalidSection`."""
    try:
        value = _SECTION_DATA[section].validate_python(data)
    except ValidationError as error:
        raise InvalidSection(problems(error.errors(include_input=False), section)) from None
    if section == "settings":
        update = _settings_update(value)
    else:
        # Раздел — модели разделов с пределами; черновик собирает из них свои модели движка.
        update = {section: _SECTION_DATA[section].dump_python(value)}
    try:
        return AuthoringDraft.model_validate(draft.model_dump() | update)
    except ValidationError as error:
        raise InvalidSection(problems(error.errors(include_input=False))) from None


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
    return update


# --- статусы шагов и проверка ---

# Шаги-наполнители: дописывают сущности своего раздела-основы (локации, открытия).
FILLING_STEPS = ("location_contents", "clues")


def is_step_empty(draft: AuthoringDraft, step: str) -> bool:
    """Пуст ли шаг для автора (дополнение §3): по содержимому раздела, а не по правилу генератора
    `steps.is_empty` («модель этот раздел ещё не писала»).

    Числа и флажки настроек Замысла имеют умолчания и содержимым не считаются. Шаги B5/B6 — по
    `steps.is_empty`, а шаг-наполнитель пуст ещё и без сущностей раздела-основы."""
    match step:
        case "concept":
            texts = (draft.title, draft.settings.pitch, draft.settings.tone, draft.journal_intro)
            return draft.id is None and not any(text.strip() for text in texts)
        case "world":
            world = draft.world
            return world is None or not (world.history.strip() or world.regions or world.rumors)
        case "pantheon":
            return not draft.deities
        case "factions":
            return not draft.factions
        case "plot":
            return not (draft.plot.acts or draft.plot.endings or draft.plot.villain)
    if step in FILLING_STEPS and not getattr(draft, STEP_SECTIONS[step]):
        return True
    return is_empty(draft, step)


def _check(draft: AuthoringDraft) -> Report:
    return check(to_adventure(draft), load_srd())


def step_statuses(draft: AuthoringDraft, times: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """`{шаг: {status, updated_at}}` по всем шагам генератора (дополнение §3).

    `empty` — шаг пуст (`is_step_empty`); `draft` — не пуст, но в проверке есть ошибка, которую
    `pipeline.owner_step` относит к нему, или это Замысел без id приключения (его в B4 даёт только
    генерация); иначе `ready`. `times` — время последней записи раздела шага (ручной или
    генерацией)."""
    failing = {owner_step(issue) for issue in _check(draft).issues if issue.severity == "error"}
    return {
        step: {"status": _status(draft, step, failing), "updated_at": times.get(step)}
        for step in STEPS
    }


def _status(draft: AuthoringDraft, step: str, failing: set[str | None]) -> str:
    if is_step_empty(draft, step):
        return "empty"
    if step in failing or (step == "concept" and draft.id is None):
        return "draft"
    return "ready"


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
        "message": issue.message,
        "ref": issue.ref,
        "step": owner_step(issue),
    }
