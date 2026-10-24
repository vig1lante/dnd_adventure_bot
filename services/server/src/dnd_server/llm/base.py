"""Общие типы слоя моделей (спека §7, план B5.1).

Провайдер (`LLMProvider`) делает две вещи: ведёт ход мастера (`run_turn`: модель вызывает
инструменты, итогом становится рассказ) и отвечает строго по схеме (`complete_json`). Числа,
броски и состояние игры провайдер не считает: инструменты исполняет переданный `ToolExecutor`,
то есть код сервера и движок.

Все ошибки провайдера — `ProviderError`; оркестратор ловит её и пишет попытку в журнал как
неудачную. Текст ошибки может нести куски ответа модели (имя лишнего ключа схемы, отказ, вывод
CLI), поэтому всё, что печатается в терминал владельца, проходит через `printable`.

Трассу вызовов инструментов адаптеры строят одной функцией `make_trace_call`: её пределы
защищают журнал хода от огромных аргументов и результатов. Общие для адаптеров части
`complete_json` (пределы повторов, описание ошибок схемы, снятие обёртки кода) и общий тайм-аут
хода (`within_turn_timeout`) тоже живут здесь, чтобы не расходиться между адаптерами.
"""

import asyncio
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

# Пределы записи вызова в трассе (журнал хода `dm_turn_logs`). Модель выбирает имя и аргументы,
# а результат инструмента зависит от размера мира, поэтому без пределов журнал раздувается.
MAX_TRACE_NAME_CHARS = 64
MAX_TRACE_ARGS_CHARS = 2000  # длина аргументов, записанных JSON-текстом
MAX_TRACE_RESULT_CHARS = 4000

MAX_JSON_RETRIES = 2  # повторов complete_json после первой попытки (спека §7)
MAX_PROBLEMS_CHARS = 1500  # сколько текста ошибок проверки схемы добавляется к повторному запросу
MAX_LOC_PART_CHARS = 64  # длина одной части пути поля в тексте ошибки схемы (имя ключа — от модели)
_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class TurnRequest:
    system: str  # стабильная часть промпта, кешируется
    adventure_brief: str  # сводка приключения, стабильная часть, кешируется
    context: str  # динамическая часть хода
    tools: tuple[ToolSchema, ...]
    max_tool_iterations: int = 20
    timeout_s: float = 180.0


@dataclass(frozen=True)
class JsonRequest:
    system: str
    prompt: str
    purpose: str  # "generator.concept", "weaving" и т. п.: по нему FakeProvider находит ответ
    # Служебный запрос, которому хватит дешёвой модели: у `anthropic_api` — LLM_CHEAP_MODEL, если
    # задана. У `claude_code_cli` все ответы по схеме — служебные роли спеки §11, и они идут на
    # CLAUDE_CLI_CHEAP_MODEL независимо от признака.
    cheap: bool = False


@dataclass(frozen=True)
class ToolCallTrace:
    name: str
    args: dict[str, Any]
    result: str
    is_error: bool


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        """Сумма расхода по итерациям хода: её считают все адаптеры."""
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class TurnResult:
    text: str  # публичный рассказ; пустой, если модель не дала итогового текста
    trace: tuple[ToolCallTrace, ...]
    usage: Usage
    stop_reason: str


class ToolExecutor(Protocol):
    """Исполнитель инструментов мастера: `(имя, аргументы) -> (текст результата, is_error)`."""

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]: ...


class ProviderError(Exception):
    """Провайдер не смог выполнить запрос: ход или шаг генератора не удался."""


class ProviderRefusal(ProviderError):
    """Модель отказалась отвечать."""


class ToolLoopExceeded(ProviderError):
    """Модель вызвала инструменты больше `TurnRequest.max_tool_iterations` раз."""


class LLMProvider(Protocol):
    name: str

    async def run_turn(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult: ...

    async def complete_json[T: BaseModel](self, req: JsonRequest, schema: type[T]) -> T: ...


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def printable(text: str, limit: int | None = None, *, keep_newlines: bool = False) -> str:
    """Текст, безопасный для терминала: непечатаемые символы заменены записью, как в `repr`.

    ESC, BEL, возврат каретки, C1 (в том числе одинарные CSI и OSC), разделители строк и
    управление направлением письма меняют вид вывода, заголовок окна и буфер обмена; одинокие
    суррогаты роняют `print`. Все они (`not str.isprintable()`) становятся `\\x1b`, `\\u202e`
    и т. п. Табуляция тоже: ни имени поля, ни сообщению она не нужна, а столбцы сдвигает.
    Перевод строки экранируется, если вызывающий не разрешил его (`keep_newlines`): имя поля
    или ключа — одна строка, иначе чужой текст нарисует собственную строку отчёта.

    `limit` — предел длины уже готовой записи (с «…» в конце), а не исходного текста."""
    escaped = "".join(
        char if char.isprintable() or (keep_newlines and char == "\n") else repr(char)[1:-1]
        for char in text
    )
    return escaped if limit is None else _clip(escaped, limit)


def _bounded_args(args: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(args, ensure_ascii=False, default=str)
    if len(text) <= MAX_TRACE_ARGS_CHARS:
        return args
    # Обрезанный JSON уже не словарь аргументов, поэтому кладём его текстом под один ключ.
    return {"_truncated": _clip(text, MAX_TRACE_ARGS_CHARS)}


def make_trace_call(name: str, args: dict[str, Any], result: str, is_error: bool) -> ToolCallTrace:
    """Запись вызова инструмента для трассы с пределами на имя, аргументы и результат.

    Обрезается только запись в трассе: инструмент получил полные аргументы, а модель — полный
    результат. Все адаптеры и исполнитель инструментов строят `ToolCallTrace` только так."""
    return ToolCallTrace(
        name=_clip(name, MAX_TRACE_NAME_CHARS),
        args=_bounded_args(args),
        result=_clip(result, MAX_TRACE_RESULT_CHARS),
        is_error=is_error,
    )


async def within_turn_timeout[T](timeout_s: float, turn: Awaitable[T]) -> T:
    """Выполняет `turn` с общим тайм-аутом хода (спека §8.4); по истечении — `ProviderError`.

    `expired()` отличает наш тайм-аут от `TimeoutError`, который мог поднять сам исполнитель
    инструментов: чужой тайм-аут проходит дальше как есть."""
    deadline = asyncio.timeout(timeout_s)
    try:
        async with deadline:
            return await turn
    except TimeoutError:
        if not deadline.expired():
            raise
        raise ProviderError(f"тайм-аут хода: больше {timeout_s:g} с") from None


def schema_problems(exc: ValidationError) -> str:
    """Ошибки схемы строкой `путь: сообщение` без значений: ответ модели или файл не цитируются.

    Имя лишнего ключа — часть пути, а сообщение о неизвестном теге цитирует тег, поэтому и путь,
    и сообщение проходят через `printable`: текст ложится в `ProviderError`, который печатает
    CLI, и в повторный запрос модели."""
    text = "; ".join(
        f"{'.'.join(printable(str(part), MAX_LOC_PART_CHARS) for part in e['loc'])}: "
        f"{printable(e['msg'])}"
        for e in exc.errors(include_input=False, include_url=False)
    )
    return text[:MAX_PROBLEMS_CHARS]


def strip_code_fence(text: str) -> str:
    """Ответ без обёртки ```json … ```: без структурированного вывода модели часто добавляют её."""
    fenced = _CODE_FENCE.fullmatch(text.strip())
    return fenced.group(1) if fenced else text
