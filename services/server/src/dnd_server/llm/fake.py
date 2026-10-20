"""`FakeProvider`: проигрывает сценарий из YAML вместо живой модели (спека §13, план B5.1).

На нём работают все автотесты и ручные прогоны агентов: сети и ключей нет. Сценарий задаёт
очередь ходов мастера и JSON-ответы по `purpose`:

    turns:
      - steps:
          - tool: request_check
            args: {character_id: "$pc1", ability: dex, dc: 15, reason: "крадётся"}
          - text: "Тени скрывают вас…"
    json:
      weaving: {hooks: []}

Инструменты шаги вызывают через переданный `ToolExecutor`, как настоящий провайдер: Fake сам
ничего не считает. `timeout_s` запроса он не применяет, потому что ничего не ждёт.

`$pc1…$pc6` в сценарии — id персонажей игры по порядку входа: Fake подставляет их и в аргументы
инструментов, и в JSON-ответы (так ответ вплетения называет героев, id которых до игры неизвестны).
Подстановка идёт на копии, сам сценарий остаётся шаблоном. Герой, которого в игре нет, — ошибка
сценария (`ProviderError`).
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dnd_engine import content as engine_content
from dnd_server.llm.base import (
    JsonRequest,
    ProviderError,
    ToolCallTrace,
    ToolExecutor,
    ToolLoopExceeded,
    TurnRequest,
    TurnResult,
    Usage,
    make_trace_call,
    schema_problems,
)

# `$pc1…$pc6` — id персонажей по порядку входа в игру.
_PC_PLACEHOLDER = re.compile(r"\$pc(\d+)")


class _Strict(BaseModel):
    """Лишнее поле в сценарии — ошибка: опечатка не должна молча менять сценарий."""

    model_config = ConfigDict(extra="forbid")


class ToolStep(_Strict):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class TextStep(_Strict):
    text: str


class ScenarioTurn(_Strict):
    steps: list[ToolStep | TextStep]
    # Ход-шаблон: не уходит из очереди и повторяется бесконечно (нужен e2e и ручной проверке).
    repeat: bool = False

    @model_validator(mode="after")
    def _text_ends_the_turn(self) -> Self:
        if any(isinstance(step, TextStep) for step in self.steps[:-1]):
            raise ValueError("text-шаг завершает ход и должен быть последним")
        return self


class Scenario(_Strict):
    # Имя `json` занято методом BaseModel, поэтому поле называется иначе, а в YAML ключ `json`.
    model_config = ConfigDict(validate_by_name=True)

    turns: list[ScenarioTurn] = Field(default_factory=list)
    # Один ответ отдаётся при каждом вызове; список — по одному ответу на вызов (тесты
    # генератора: плохой ответ на первом круге, хороший на повторе).
    json_answers: dict[str, dict[str, Any] | list[dict[str, Any]]] = Field(
        default_factory=dict, validation_alias="json"
    )

    @model_validator(mode="after")
    def _repeat_turn_is_last(self) -> Self:
        if any(turn.repeat for turn in self.turns[:-1]):
            raise ValueError("ход с repeat повторяется бесконечно: других ходов после него нет")
        return self


class UnreadableYaml(Exception):
    """YAML не прочитан; текст — причина без значений из файла (см. `parse_yaml`)."""


def yaml_problem(exc: yaml.YAMLError) -> str:
    """Вид и позиция ошибки YAML без цитат из файла.

    Файл мог указывать на чужое содержимое с секретами, а `problem` бывает цитатой (имя тега или
    якоря), не говоря о фрагменте строки в `str(exc)`. Исключение — `ComposerError`: движковый
    разбор отвергает якорь и алиас раньше, чем PyYAML успеет назвать их имена в своих сообщениях,
    так что его `problem` — фиксированный текст («якоря и алиасы YAML не поддерживаются»), и без
    него владелец не узнал бы, что править."""
    mark = getattr(exc, "problem_mark", None)
    place = f", строка {mark.line + 1}, столбец {mark.column + 1}" if mark else ""
    reason = f": {exc.problem}" if isinstance(exc, yaml.composer.ComposerError) else ""
    return f"{type(exc).__name__}{place}{reason}"


def parse_yaml(text: str) -> Any:
    """Данные из YAML недоверенного ввода (сценарий, сиды генератора) или `UnreadableYaml`.

    Единственное место разбора YAML в сервере. Разбор движковый (`content.parse_yaml`), как у
    загрузчика папки приключения: безопасные теги и без якорей и алиасов, которые раздувают малый
    файл в миллионы моделей.

    Конструкторы значений PyYAML на чужом вводе бросают что угодно, а не только `YAMLError`:
    `!!int ""` — `IndexError`, `!!bool x` — `KeyError`, `!!timestamp x` — `AttributeError`, длинное
    число или несуществующая дата — `ValueError`; глубокая вложенность — `RecursionError`. Перечень
    не закрыт, поэтому ловим всё, но строго вокруг разбора. Тип и текст исключения в сообщение не
    идут: по ним причину не назвать, а текст содержит значение из файла.

    `UnreadableYaml` поднимается вне `except`: у него нет ни `__cause__`, ни `__context__`, и
    значение из файла не доедет до трассировки."""
    try:
        return engine_content.parse_yaml(text.encode("utf-8"))
    except yaml.YAMLError as exc:
        problem = yaml_problem(exc)
    except Exception as exc:
        # Причины не-YAML ошибок общие с загрузчиком движка: свои копии текстов разошлись бы.
        problem = engine_content.describe_parse_failure(exc)
    raise UnreadableYaml(problem)


def read_yaml(path: Path) -> Any:
    """Данные из YAML-файла недоверенного ввода (сценарий, сиды генератора) или `UnreadableYaml`.

    Файл проверяется до чтения тем же `check_yaml_size`, что и файлы папки приключения: канал FIFO
    или устройство вместо файла подвесили бы чтение, а огромный файл целиком попал бы в память.
    Файла нет — `FileNotFoundError`; не обычный файл (папка, канал, устройство) — `OSError`
    «не обычный файл»; не в UTF-8 — `UnicodeDecodeError`; больше предела размера —
    `UnreadableYaml` с готовой причиной («файл больше 512 КБ»), как у нечитаемого YAML."""
    try:
        engine_content.check_yaml_size(path)
    except engine_content.YamlTooLargeError as exc:
        problem = str(exc)
    else:
        return parse_yaml(path.read_text(encoding="utf-8"))
    # Вне `except`, как в `parse_yaml`: у ошибки нет ни причины, ни контекста.
    raise UnreadableYaml(problem)


def describe_os_error(exc: OSError) -> str:
    """Текст ошибки файловой системы для сообщения владельцу.

    Проверка вида файла (`check_yaml_size`) бросает `OSError(0, «не обычный файл»)`, а `str()` такой
    ошибки — «[Errno 0] не обычный файл»: 0 не код ОС, и префикс только путает. У неё остаётся
    причина (`strerror`). Ошибка ОС с настоящим кодом (нет файла, нет прав) печатается как есть:
    в ней путь, который владельцу нужен."""
    if exc.errno == 0 and exc.strerror:
        return exc.strerror
    return str(exc)


def load_scenario(path: Path) -> Scenario:
    """Читает YAML-сценарий.

    Неверный формат — `pydantic.ValidationError` с путём поля, нечитаемый или слишком большой
    YAML — `UnreadableYaml`, файл не открылся или не в UTF-8 — `OSError` или `UnicodeDecodeError`
    (подробнее в `read_yaml`)."""
    return Scenario.model_validate(read_yaml(path))


class FakeProvider:
    """Провайдер без сети: ход берётся из очереди сценария, JSON — из его раздела `json`.

    `pc_ids` — id персонажей по порядку входа: сервер передаёт их при создании провайдера
    игры, и `$pc1…$pc6` в аргументах инструментов и в JSON-ответах превращаются в эти id. У каждой
    игры свой провайдер: очередь ходов хранится в нём."""

    name = "fake"

    def __init__(self, scenario: Scenario, pc_ids: Sequence[str] = ()) -> None:
        self._scenario = scenario
        self._pc_ids = tuple(pc_ids)
        self._next_turn = 0
        self._json_given: dict[str, int] = {}  # сколько ответов из списка уже выдано по purpose

    async def run_turn(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult:
        turn = self._take_turn()
        # Подстановки выполняются до первого вызова: ошибка сценария не оставляет следов.
        calls = [
            (step.tool, self._fill_placeholders(step.args))
            for step in turn.steps
            if isinstance(step, ToolStep)
        ]
        # Ход без text-шага даёт пустой текст: так тесты оркестратора получают «нет рассказа».
        text = next((step.text for step in turn.steps if isinstance(step, TextStep)), "")
        trace: list[ToolCallTrace] = []
        for name, args in calls:
            if len(trace) >= req.max_tool_iterations:
                raise ToolLoopExceeded(
                    f"больше {req.max_tool_iterations} вызовов инструментов за ход"
                )
            result, is_error = await tools(name, args)
            trace.append(make_trace_call(name, args, result, is_error))
        return TurnResult(text=text, trace=tuple(trace), usage=Usage(), stop_reason="end_turn")

    async def complete_json[T: BaseModel](self, req: JsonRequest, schema: type[T]) -> T:
        answer = self._fill_placeholders(self._next_json_answer(req.purpose))
        try:
            return schema.model_validate(answer)
        except ValidationError as exc:
            # Те же описание и нейтрализация текста, что у настоящих адаптеров: ключ ответа в
            # сценарии, как и ответ модели, не должен управлять терминалом.
            raise ProviderError(
                f"JSON-ответ {req.purpose!r} не подходит схеме {schema.__name__}: "
                f"{schema_problems(exc)}"
            ) from exc

    def _take_turn(self) -> ScenarioTurn:
        turns = self._scenario.turns
        if self._next_turn >= len(turns):
            raise ProviderError("сценарий исчерпан: ходы закончились")
        turn = turns[self._next_turn]
        if not turn.repeat:
            self._next_turn += 1
        return turn

    def _next_json_answer(self, purpose: str) -> dict[str, Any]:
        answers = self._scenario.json_answers.get(purpose)
        if answers is None:
            raise ProviderError(f"в сценарии нет JSON-ответа для purpose {purpose!r}")
        if isinstance(answers, dict):
            return answers
        given = self._json_given.get(purpose, 0)
        if given >= len(answers):
            raise ProviderError(f"сценарий исчерпан: ответы для purpose {purpose!r} закончились")
        self._json_given[purpose] = given + 1
        return answers[given]

    def _fill_placeholders(self, value: Any) -> Any:
        match value:
            case str():
                return _PC_PLACEHOLDER.sub(self._character_id, value)
            case list():
                return [self._fill_placeholders(item) for item in value]
            case dict():
                return {key: self._fill_placeholders(item) for key, item in value.items()}
            case _:
                return value

    def _character_id(self, placeholder: re.Match[str]) -> str:
        number = int(placeholder.group(1))
        if not 1 <= number <= len(self._pc_ids):
            raise ProviderError(
                f"в сценарии {placeholder.group(0)}, а персонажей в игре: {len(self._pc_ids)}"
            )
        return self._pc_ids[number - 1]
