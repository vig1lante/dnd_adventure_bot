"""Адаптер `openai_compatible`: Chat Completions с function calling (спека §7, план B5.3).

Подходит любой сервер с совместимым API (`base_url`, `api_key`, `model` задаёт фабрика B5.5).
Цикл инструментов ведёт наш код: модель просит `tool_calls`, `ToolExecutor` исполняет вызовы, на
каждый вызов уходит своё сообщение `tool`. Числа и состояние игры адаптер не трогает.
Стоимость ограничена: итераций инструментов не больше `TurnRequest.max_tool_iterations`, у хода
общий тайм-аут, `complete_json` повторяет не более двух раз. Повторы 429/5xx делает сам SDK
(спека §12), поэтому клиент настраивает фабрика.

Ключ API живёт только в переданном клиенте: адаптер его не хранит, не логирует и вырезает из
текстов ошибок (совместимый сервер или шлюз может вернуть его в теле ответа), а цепочку
исключений SDK не сохраняет.
"""

import json
from typing import Any

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from pydantic import BaseModel, ValidationError

from dnd_server.llm.base import (
    MAX_JSON_RETRIES,
    JsonRequest,
    ProviderError,
    ProviderRefusal,
    ToolCallTrace,
    ToolExecutor,
    ToolLoopExceeded,
    ToolSchema,
    TurnRequest,
    TurnResult,
    Usage,
    make_trace_call,
    schema_problems,
    strip_code_fence,
    within_turn_timeout,
)

# Универсальный параметр предела ответа: новые модели OpenAI отвергают устаревший `max_tokens`,
# а совместимые серверы без него обычно просто игнорируют неизвестное поле.
MAX_COMPLETION_TOKENS = 16000
MAX_ERROR_CHARS = 300  # сколько текста ошибки сервера попадает в исключение
JSON_SCHEMA_NAME = "answer"
# Словарь причин остановки общий для всех адаптеров: оркестратор не знает про финиш-причины OpenAI.
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens"}


class _ResponseFormatRejected(ProviderError):
    """Сервер ответил 400 на `response_format`: `complete_json` повторит запрос без него."""


class OpenAICompatibleProvider:
    """`LLMProvider` поверх `AsyncOpenAI`; клиент внедряется, чтобы в тестах не было сети."""

    name = "openai_compatible"

    def __init__(self, client: AsyncOpenAI, model: str, cheap_model: str | None = None) -> None:
        self._client = client
        self._model = model
        self._cheap_model = cheap_model
        # Сервер, отвергший json_schema один раз, отвергнет и дальше: не платим лишним запросом.
        self._json_schema_supported = True

    @property
    def model(self) -> str:
        """Основная модель, которой идёт ход мастера (дешёвая нужна только `complete_json`)."""
        return self._model

    async def run_turn(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult:
        # Тайм-аут общий на весь ход (спека §8.4), а не на один запрос: инструменты и повторные
        # запросы цикла тоже ограничены.
        return await within_turn_timeout(req.timeout_s, self._tool_loop(req, tools))

    async def complete_json[T: BaseModel](self, req: JsonRequest, schema: type[T]) -> T:
        model = self._cheap_model if req.cheap and self._cheap_model else self._model
        prompt = req.prompt
        problems = ""
        # У JsonRequest тайм-аута нет: пределом каждой попытки служит тайм-аут клиента SDK.
        for _ in range(MAX_JSON_RETRIES + 1):
            response = await self._json_completion(model, req.system, prompt, schema)
            choice = _first_choice(response)
            _raise_if_refused(choice)
            if choice.finish_reason == "length":
                # Оборванный JSON повтор не вылечит: он оборвётся там же.
                raise ProviderError(f"JSON-ответ {req.purpose!r} оборван по max_tokens")
            try:
                return schema.model_validate_json(strip_code_fence(choice.message.content or ""))
            except ValidationError as exc:
                problems = schema_problems(exc)
                prompt = (
                    f"{req.prompt}\n\nПредыдущий ответ не прошёл проверку схемы:\n{problems}\n"
                    "Исправь эти ошибки и верни JSON заново."
                )
        raise ProviderError(
            f"JSON-ответ {req.purpose!r} не подходит схеме {schema.__name__} "
            f"после {MAX_JSON_RETRIES + 1} попыток: {problems}"
        )

    async def _tool_loop(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult:
        messages: list[Any] = [
            # Стабильная часть первой: серверы с кешем префикса переиспользуют её между ходами.
            {"role": "system", "content": f"{req.system}\n\n{req.adventure_brief}"},
            {"role": "user", "content": req.context},
        ]
        params: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        if req.tools:  # часть серверов отвечает 400 на пустой список инструментов
            params["tools"] = [_tool_param(tool) for tool in req.tools]
        usage = Usage()
        trace: list[ToolCallTrace] = []
        iterations = 0  # шагов цикла, на которых модель просила инструменты
        while True:
            response = await self._create(messages=messages, **params)
            choice = _first_choice(response)
            usage += _usage_of(response)
            _raise_if_refused(choice)
            # Оборванный по длине ответ вызовов не исполняет: аргументы могли обрезаться.
            tool_calls = choice.message.tool_calls if choice.finish_reason != "length" else None
            if choice.finish_reason == "tool_calls" and not tool_calls:
                raise ProviderError("модель остановилась на tool_calls, но вызовов в ответе нет")
            if not tool_calls:
                return _final_result(choice, trace, usage)
            if iterations >= req.max_tool_iterations:
                raise ToolLoopExceeded(
                    f"больше {req.max_tool_iterations} итераций инструментов за ход"
                )
            iterations += 1
            # Некоторые серверы отдают вызовы с finish_reason "stop": ориентируемся на tool_calls.
            replies = await _run_tool_calls(tool_calls, tools, trace)
            messages.append(choice.message)
            messages.extend(replies)

    async def _json_completion(
        self, model: str, system: str, prompt: str, schema: type[BaseModel]
    ) -> ChatCompletion:
        """Запрос JSON-ответа: с `response_format`, а если сервер его отверг — с инструкцией."""
        json_schema = schema.model_json_schema()

        def request(user_text: str, **extra: Any) -> dict[str, Any]:
            return {
                "model": model,
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                **extra,
            }

        if self._json_schema_supported:
            response_format = {
                "type": "json_schema",
                # strict не включаем: строгий режим требует особой формы схемы, а совместимые
                # серверы её не понимают; схему всё равно проверяет pydantic.
                "json_schema": {"name": JSON_SCHEMA_NAME, "schema": json_schema, "strict": False},
            }
            try:
                return await self._create(**request(prompt, response_format=response_format))
            except _ResponseFormatRejected:
                self._json_schema_supported = False
        instruction = (
            "Ответь только JSON-объектом по этой JSON Schema, без пояснений и без markdown:\n"
            f"{json.dumps(json_schema, ensure_ascii=False)}"
        )
        return await self._create(**request(f"{prompt}\n\n{instruction}"))

    async def _create(self, **params: Any) -> ChatCompletion:
        """Один запрос к серверу; сбои SDK становятся `ProviderError` без ключа в тексте."""
        try:
            return await self._client.chat.completions.create(**params)
        except openai.APITimeoutError:
            raise ProviderError("тайм-аут запроса к серверу модели") from None
        except openai.APIConnectionError:
            raise ProviderError("нет соединения с сервером модели") from None
        except openai.APIStatusError as exc:
            request_id = f" (request_id: {exc.request_id})" if exc.request_id else ""
            text = (
                f"сервер модели ответил {exc.status_code}: {self._safe_text(exc.message)}"
                f"{request_id}"
            )
            if exc.status_code == 400 and "response_format" in exc.message.lower():
                raise _ResponseFormatRejected(text) from None
            raise ProviderError(text) from None
        except openai.APIError as exc:
            raise ProviderError(f"сбой запроса к серверу модели: {type(exc).__name__}") from None
        except (RecursionError, ValueError) as exc:
            # Ответ вложен глубже, чем SDK разбирает (`RecursionError`), или сообщение сервера
            # глубже, чем он может отправить обратно в следующем запросе (`ValueError`). Тексты
            # этих исключений не цитируются: в них бывают куски ответа.
            raise ProviderError(
                f"ответ сервера модели не удалось обработать: {type(exc).__name__}"
            ) from None

    def _safe_text(self, text: str) -> str:
        """Текст ошибки сервера без ключа и не длиннее `MAX_ERROR_CHARS`."""
        # Ключ вырезается до обрезки: иначе обрезка могла бы оставить его начало.
        secret = self._client.api_key
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "***")
        return text[:MAX_ERROR_CHARS]


def _tool_param(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _first_choice(response: ChatCompletion) -> Choice:
    if not response.choices:
        raise ProviderError("сервер модели вернул ответ без вариантов (choices)")
    return response.choices[0]


def _usage_of(response: ChatCompletion) -> Usage:
    """Расход токенов; как у Anthropic, `input_tokens` не включает кешированную часть."""
    if response.usage is None:  # не все совместимые серверы сообщают расход
        return Usage()
    details = response.usage.prompt_tokens_details
    cached = (details.cached_tokens or 0) if details else 0
    return Usage(
        input_tokens=max(0, response.usage.prompt_tokens - cached),
        output_tokens=response.usage.completion_tokens,
        cache_read_tokens=cached,
    )


def _raise_if_refused(choice: Choice) -> None:
    if choice.finish_reason == "content_filter":
        raise ProviderRefusal("ответ отклонён фильтром содержимого сервера (content_filter)")
    if choice.message.refusal:
        raise ProviderRefusal(
            f"модель отказалась отвечать: {choice.message.refusal[:MAX_ERROR_CHARS]}"
        )


def _final_result(choice: Choice, trace: list[ToolCallTrace], usage: Usage) -> TurnResult:
    stop_reason = _STOP_REASONS.get(choice.finish_reason)
    if stop_reason is None:
        raise ProviderError(f"неожиданная причина остановки модели: {choice.finish_reason!r}")
    text = (choice.message.content or "").strip()
    if stop_reason == "max_tokens" and not text:
        raise ProviderError("ответ модели оборван по max_tokens до итогового текста")
    return TurnResult(text=text, trace=tuple(trace), usage=usage, stop_reason=stop_reason)


async def _run_tool_calls(
    tool_calls: list[Any], tools: ToolExecutor, trace: list[ToolCallTrace]
) -> list[dict[str, Any]]:
    """Выполняет по порядку все вызовы ответа; на каждый вызов — своё сообщение `tool`."""
    replies: list[dict[str, Any]] = []
    for call in tool_calls:
        if call.type != "function":
            raise ProviderError(f"неподдерживаемый вызов инструмента типа {call.type!r}")
        name, raw_arguments = call.function.name, call.function.arguments or ""
        try:
            args = _parse_arguments(raw_arguments)
        except ValueError as exc:
            # Битые аргументы — ошибка модели, а не хода: она узнаёт о ней и может повторить вызов.
            result = (
                f"Ошибка: аргументы вызова {name}: {exc}. Повтори вызов с корректным JSON-объектом."
            )
            is_error, args = True, {"_raw_arguments": raw_arguments}
        else:
            result, is_error = await tools(name, args)
        trace.append(make_trace_call(name, args, result, is_error))
        replies.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return replies


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Аргументы вызова из JSON-строки; пустая строка — вызов без аргументов."""
    if not raw.strip():
        return {}
    try:
        args = json.loads(raw)
        # Трасса хода и журнал хранят аргументы JSON-ом: вложенность, которую json.dumps не
        # потянет, отвергается здесь, до выполнения инструмента (у dumps предел ниже, чем у loads).
        json.dumps(args)
    except json.JSONDecodeError as exc:
        raise ValueError(f"это не JSON ({exc.msg})") from None
    except RecursionError:
        raise ValueError("аргументы вложены слишком глубоко") from None
    if not isinstance(args, dict):
        raise ValueError("нужен JSON-объект с аргументами")
    return args
