"""Реестр инструментов мастера.

Модель мастера действует на мир только через инструменты. Инструмент — `ToolSpec`: имя, описание
для модели, модель аргументов и обработчик `(ToolContext, args) -> dict`. `ToolRegistry.call`
принимает вызов модели: проверяет имя и аргументы, зовёт обработчик и превращает ошибки модели в
`ToolOutcome(error=...)`, чтобы неверный вызов не ронял ход мастера.

Что считается ошибкой модели, а что ошибкой движка:
- ошибка модели — неизвестное имя, аргументы не по схеме и `ToolError` из обработчика. Это
  результат с текстом, по которому модель исправляется сама;
- всё остальное — ошибка движка (баг в нашем коде) и пробрасывается: ход не должен выглядеть
  удавшимся. Поэтому `call` не ловит общий `ValueError`.

Правило для обработчиков: ошибки движка, вызванные аргументами модели (`DiceSyntaxError`,
`CombatError`, `CharacterValidationError` и узкие `ValueError` проверок движка), обработчик
ловит сам, у места вызова функции движка, узким кортежем типов, и поднимает `ToolError`. Широкий
`except ValueError` вокруг всего обработчика спрятал бы настоящие баги. Обработчик проверяет всё
до первого изменения состояния и броска: `ToolError` после изменения оставил бы рабочую копию
изменённой.

Аргументы модели — недоверенный ввод. Модели аргументов наследуют `ToolArgs` (`extra="forbid"`),
у текстовых полей задан `max_length`, у чисел — границы; реестр отклоняет модель аргументов,
которая допускает лишние поля. Тексты ошибок для модели и журнала короткие (`MAX_ERROR_CHARS`):
ошибки схемы называют поле и причину без значения, а в тексте `ToolError` бывают id из аргументов
(не длиннее `MAX_ID_CHARS`, этого требует схема), поэтому `call` обрезает и его.
"""

import functools
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character
from dnd_engine.content import SrdContent
from dnd_engine.dice import Advantage
from dnd_engine.events import Audience, Event, RollEvent
from dnd_engine.modules.base import RuleSet
from dnd_engine.rng import Rng
from dnd_engine.state import GameState

# Аудитория «все» живёт в одном пространстве имён с id персонажей: тайным актёром «all» быть нельзя.
PUBLIC_AUDIENCE = "all"
# Тексты ошибок для модели и журнала короткие: в них попадают имена, которые выбрала модель.
MAX_ERROR_CHARS = 500
MAX_NAME_CHARS = 64
MAX_REASON_CHARS = 200  # «коротко, зачем»: причина броска, сцены, привала попадает в журнал


class ToolError(Exception):
    """Ошибка для модели: текст объясняет, что исправить."""


def find_or_error[T](collection: Mapping[str, T], what: str, key: str) -> T:
    """Элемент по id или `ToolError` со списком известных id: модель исправляется сама."""
    found = collection.get(key)
    if found is None:
        raise ToolError(f"Нет {what} {key!r}. Есть: {', '.join(collection)}")
    return found


class ToolArgs(BaseModel):
    """Основа моделей аргументов: лишнее поле — ошибка, а не молчаливо потерянное значение."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolContext:
    """Всё, что нужно обработчику: рабочая копия состояния, данные и источник бросков.

    `secret_actors` и `inspired` — id персонажей (не пользователей): авторов тайных ходов и
    игроков, отметивших «Вдохновение», сервер переводит в id их персонажей. `acting` — id бойцов
    (персонажей и монстров), за которых мастер ходит в этом вызове; боевые инструменты передают
    ход только им.
    """

    state: GameState  # рабочая копия: инструменты её меняют, сервер коммитит весь ход разом
    adventure: Adventure
    content: SrdContent
    rules: RuleSet
    rng: Rng
    events: list[Event] = field(default_factory=list)
    secret_actors: set[str] = field(default_factory=set)  # броски видны только автору
    inspired: set[str] = field(default_factory=set)  # первый к20 с преимуществом
    # id бойцов, чьи ходы разыгрываются в этом вызове (персонаж, сдавший ход, и монстры за ним);
    # заполняет сервер. Пусто — в бою не действует никто.
    acting: tuple[str, ...] = ()
    # Первое событие действия текущего вызова: его ставит реестр перед обработчиком, а передачу
    # хода (`begin_action`) обработчик отделяет от действия сам. События до него автору не
    # принадлежат.
    action_from: int = 0

    def __post_init__(self) -> None:
        self.acting = tuple(self.acting)  # сервер может передать список
        if PUBLIC_AUDIENCE in self.secret_actors:
            # Аудитория «all» значит «видят все»: тайный бросок такого актёра стал бы общим.
            raise ValueError(
                f"id {PUBLIC_AUDIENCE!r} зарезервирован и не может быть тайным актёром"
            )

    def audience_for(self, actor_id: str) -> Audience:
        """Кому виден бросок актёра: тайному актёру — только ему, остальным — всем."""
        return actor_id if actor_id in self.secret_actors else PUBLIC_AUDIENCE

    def audience_of(self, subjects: Iterable[str]) -> Audience:
        """Кому видна запись о героях `subjects` (например, запись дневника о предмете, который
        перешёл из рук в руки): тайный герой из них видит её один, иначе — все.

        То же правило, что у строк ленты сервера о предметах и вдохновении: аудитория одна,
        поэтому из нескольких тайных героев запись достаётся первому по id — закрыто, а не всем."""
        secret = sorted(set(subjects) & self.secret_actors)
        return secret[0] if secret else PUBLIC_AUDIENCE

    def mark_secret_rolls(
        self, authors: Iterable[str] = (), public_labels: Collection[str] = ()
    ) -> None:
        """Закрывает броски действия вызова (`events[action_from:]`) по его авторам.

        Единственное место, где размечается аудитория бросков: обработчики о тайных ходах не
        знают. Автор вызова — тот, чьё это действие (`ToolSpec.author_fields`); кто бросал
        кубы, не важно: толчок заставляет бросать цель, а ход тайный. Правило закрыто по
        умолчанию:
        - собственный бросок тайного актёра видит только он;
        - остальные броски действия тайного автора — часть его действия, их видит автор;
          при нескольких тайных авторах бросок достаётся первому по id: аудитория события одна;
        - если автора нет или он не тайный, остальные броски общие;
        - единственное исключение — броски с подписью из `public_labels`
          (`ToolSpec.public_roll_labels`): их видят все, даже у тайного актёра.
        """
        self._close_rolls(self.action_from, len(self.events), authors, public_labels)

    def begin_action(self, first_event: int) -> None:
        """Отделяет передачу хода от действия: `events[action_from:first_event]` — броски конца
        хода и спасброски от смерти бойцов, которых пришлось пройти. Они не часть действия
        автора и размечаются только по своим актёрам; действие начинается с `first_event`."""
        self._close_rolls(self.action_from, first_event, (), ())
        self.action_from = first_event

    def _close_rolls(
        self, start: int, end: int, authors: Iterable[str], public_labels: Collection[str]
    ) -> None:
        secret_authors = sorted(set(authors) & self.secret_actors)
        for index in range(start, end):
            event = self.events[index]
            if not isinstance(event, RollEvent) or event.label in public_labels:
                continue
            audience = self.audience_for(event.actor_id)
            if audience == PUBLIC_AUDIENCE and secret_authors:
                audience = secret_authors[0]
            if audience != PUBLIC_AUDIENCE:
                self.events[index] = event.model_copy(update={"audience": audience})

    def take_inspiration(self, character_id: str) -> Advantage:
        """Тратит вдохновение персонажа и даёт преимущество; без вдохновения — без преимущества.

        Одноразово: нужны обе отметки — игрок отметил «Вдохновение» в этом ходе (`inspired`) и оно
        у персонажа действительно есть (`Character.inspiration`, источник правды). Тратятся обе.
        """
        character: Character | None = self.state.party.get(character_id)
        if character_id not in self.inspired or character is None or not character.inspiration:
            return Advantage.NONE
        self.inspired.discard(character_id)
        character.inspiration = False
        return Advantage.ADVANTAGE


def commit_state(ctx: ToolContext, state: GameState, events: list[Event]) -> None:
    """Принимает результат функции движка: `ctx.state` обновляется на месте, события копятся.

    Функции движка возвращают новое состояние, а сервер держит рабочую копию, которую
    инструменты «меняют» (`ToolContext.state`), поэтому подменять объект нельзя."""
    for name in GameState.model_fields:
        setattr(ctx.state, name, getattr(state, name))
    ctx.events += events


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str  # на русском, для модели
    args_model: type[ToolArgs]
    handler: Callable[[ToolContext, Any], dict[str, Any]]
    # Поля аргументов с id автора вызова (персонаж или боец, чьё это действие): по ним реестр
    # решает, чьи броски вызова тайные. Пусто — у инструмента автора нет (лор, мир, начало боя).
    author_fields: tuple[str, ...] = ()
    # Подписи бросков, которые этот инструмент всегда показывает всем, даже у тайного актёра:
    # инициатива начала боя и так видна в порядке хода. Подпись сверяется
    # только в вызовах этого инструмента: та же подпись в другом (имя действия монстра из
    # приключения, против которого тайный герой спасается) бросок не открывает.
    public_roll_labels: tuple[str, ...] = ()

    def authors(self, args: ToolArgs) -> set[str]:
        """id авторов вызова; необязательное поле без значения автора не даёт."""
        values = (getattr(args, name) for name in self.author_fields)
        return {value for value in values if isinstance(value, str)}

    def json_schema(self) -> dict[str, Any]:
        """Схема аргументов для провайдера модели; сериализуется `json.dumps`.

        Каждый вызов получает свою копию: схема класса строится один раз (`_schema_text`)."""
        return _SCHEMA.validate_json(_schema_text(self.args_model))


_SCHEMA = TypeAdapter(dict[str, Any])


@functools.cache
def _schema_text(args_model: type[BaseModel]) -> bytes:
    # Схема зависит только от класса аргументов, pydantic строит её долго, а каждый ход мастера
    # запрашивает схемы всех инструментов. Кэш хранит JSON, чтобы копии не делили словари.
    return _SCHEMA.dump_json(args_model.model_json_schema())


class ToolOutcome(BaseModel):
    """Итог вызова для модели и журнала хода: либо `result`, либо `error`."""

    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


def _shorten(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _describe_validation_error(name: str, exc: ValidationError) -> str:
    # Берём только путь поля и сообщение, без значения: модель может прислать огромную строку.
    # Имена в пути тоже выбирает модель (лишнее поле, ключ словаря), поэтому каждое обрезано.
    problems = []
    for error in exc.errors():
        field_path = ".".join(str(part)[:MAX_NAME_CHARS] for part in error["loc"])
        problems.append(f"{field_path}: {error['msg']}" if field_path else error["msg"])
    return _shorten(f"Неверные аргументы инструмента {name}: " + "; ".join(problems))


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Добавляет инструмент; повтор имени, лишние поля аргументов или поле автора, которого
        нет в аргументах, — `ValueError`."""
        if spec.name in self._specs:
            raise ValueError(f"инструмент {spec.name!r} уже зарегистрирован")
        if spec.args_model.model_config.get("extra") != "forbid":
            raise ValueError(
                f"аргументы инструмента {spec.name!r} должны запрещать лишние поля "
                "(extra='forbid'): наследуйте ToolArgs"
            )
        missing = [name for name in spec.author_fields if name not in spec.args_model.model_fields]
        if missing:
            # Опечатка в имени поля молча выключила бы тайность бросков инструмента.
            raise ValueError(
                f"у аргументов инструмента {spec.name!r} нет полей автора: {', '.join(missing)}"
            )
        self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        """Инструменты в порядке регистрации."""
        return list(self._specs.values())

    def call(self, ctx: ToolContext, name: str, raw_args: Any) -> ToolOutcome:
        """Вызов инструмента моделью. Ошибки модели — в `error`, ошибки движка пробрасываются.

        `raw_args` — ввод модели: разобранный JSON любой формы (список, строка, `null`), не
        обязательно объект. Не объект — ошибка модели, а в `ToolOutcome.args` кладётся `{}`."""
        spec = self._specs.get(name)
        if spec is None:
            known = ", ".join(self._specs)
            shown_name = name[:MAX_NAME_CHARS]
            error = f"Неизвестный инструмент {shown_name!r}. Доступны: {known}"
            args = raw_args if isinstance(raw_args, dict) else {}
            return ToolOutcome(name=shown_name, args=args, error=error)
        if not isinstance(raw_args, dict):
            error = (
                f"Аргументы инструмента {name} должны быть объектом с полями, "
                f"а пришло: {type(raw_args).__name__}"
            )
            return ToolOutcome(name=name, args={}, error=error)
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            return ToolOutcome(
                name=name, args=raw_args, error=_describe_validation_error(name, exc)
            )
        ctx.action_from = len(ctx.events)
        try:
            result = spec.handler(ctx, args)
        except ToolError as exc:
            return ToolOutcome(name=name, args=raw_args, error=_shorten(str(exc)))
        ctx.mark_secret_rolls(spec.authors(args), spec.public_roll_labels)
        return ToolOutcome(name=name, args=raw_args, result=result)
