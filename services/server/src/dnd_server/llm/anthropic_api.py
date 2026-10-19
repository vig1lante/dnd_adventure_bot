"""Адаптер `anthropic_api`: Messages API через официальный SDK `anthropic` (спека §7, план B5.2).

Цикл инструментов ведёт наш код: модель просит `tool_use`, `ToolExecutor` исполняет вызовы, все
результаты шага уходят одним user-сообщением. Числа и состояние игры адаптер не трогает.
Стоимость ограничена: стабильные части промпта кешируются, итераций инструментов не больше
`TurnRequest.max_tool_iterations`, у хода есть общий тайм-аут, `complete_json` повторяет не более
двух раз. Повторы 429/5xx делает сам SDK (спека §12), поэтому клиент настраивает фабрика (B5.5).

Ключ API живёт только в переданном клиенте: адаптер его не хранит, не логирует и вырезает из
текстов ошибок, а цепочку исключений SDK не сохраняет.
"""

import json
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaMessage
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
    within_turn_timeout,
)

MAX_TOKENS = 16000
# Серверный фолбэк при отказе: форма `fallbacks: "default"` включается именно этим заголовком.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
MAX_ERROR_CHARS = 300  # сколько текста ошибки сервера попадает в исключение

_CACHE_MARK = {"type": "ephemeral"}
# Остановки, которыми ход может честно закончиться; остальные при наших параметрах не ожидаются.
_FINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens"})


class AnthropicProvider:
    """`LLMProvider` поверх `AsyncAnthropic`; клиент внедряется, чтобы в тестах не было сети."""

    name = "anthropic_api"

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        effort: str,
        cheap_model: str | None = None,
    ) -> None:
        if effort not in EFFORTS:
            raise ValueError(f"LLM_EFFORT={effort!r}: допустимо одно из {sorted(EFFORTS)}")
        self._client = client
        self._model = model
        self._effort = effort
        self._cheap_model = cheap_model

    async def run_turn(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult:
        # Тайм-аут общий на весь ход (спека §8.4), а не на один запрос: инструменты и повторные
        # запросы цикла тоже ограничены.
        return await within_turn_timeout(req.timeout_s, self._tool_loop(req, tools))

    async def complete_json[T: BaseModel](self, req: JsonRequest, schema: type[T]) -> T:
        use_cheap = req.cheap and self._cheap_model is not None
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
        }
        if use_cheap:
            # Возможности дешёвой модели неизвестны (LLM_CHEAP_MODEL свободная строка), поэтому
            # ей уходит только то, что понимает любая модель: без thinking, effort и fallbacks.
            params: dict[str, Any] = {"model": self._cheap_model, "output_config": output_config}
        else:
            params = {
                "model": self._model,
                "output_config": output_config | {"effort": self._effort},
                "thinking": {"type": "adaptive"},
                "betas": [FALLBACK_BETA],
                "fallbacks": "default",
            }
        system = [_cached_text(req.system)]

        prompt = req.prompt
        problems = ""
        # У JsonRequest тайм-аута нет: пределом каждой попытки служит тайм-аут клиента SDK.
        for _ in range(MAX_JSON_RETRIES + 1):
            response = await self._create(
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                **params,
            )
            _raise_if_refused(response)
            if response.stop_reason == "max_tokens":
                # Оборванный JSON повтор не вылечит: он оборвётся там же.
                raise ProviderError(f"JSON-ответ {req.purpose!r} оборван по max_tokens")
            try:
                return schema.model_validate_json(_text_of(response))
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
        system = [_cached_text(req.system)]
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                # Сводка стабильна и кешируется; динамический контекст идёт после неё.
                "content": [
                    _cached_text(req.adventure_brief),
                    {"type": "text", "text": req.context},
                ],
            }
        ]
        tool_params = [_tool_param(tool) for tool in req.tools]
        usage = Usage()
        trace: list[ToolCallTrace] = []
        iterations = 0  # шагов цикла, на которых модель просила инструменты
        while True:
            response = await self._create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tool_params,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            usage += _usage_of(response)
            _raise_if_refused(response)
            if response.stop_reason != "tool_use":
                return _final_result(response, trace, usage)
            if iterations >= req.max_tool_iterations:
                raise ToolLoopExceeded(
                    f"больше {req.max_tool_iterations} итераций инструментов за ход"
                )
            iterations += 1
            tool_results = await _run_tools(response, tools, trace)
            # Ответ модели возвращается целиком (с thinking-блоками), результаты — одним сообщением.
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    async def _create(self, **params: Any) -> BetaMessage:
        """Один запрос к API; сбои SDK становятся `ProviderError` без ключа в тексте."""
        try:
            return await self._client.beta.messages.create(**params)
        except anthropic.APITimeoutError:
            raise ProviderError("тайм-аут запроса к Anthropic API") from None
        except anthropic.APIConnectionError:
            raise ProviderError("нет соединения с Anthropic API") from None
        except anthropic.APIStatusError as exc:
            request_id = f" (request_id: {exc.request_id})" if exc.request_id else ""
            raise ProviderError(
                f"Anthropic API ответил {exc.status_code}: {self._safe_text(exc.message)}"
                f"{request_id}"
            ) from None
        except anthropic.APIError as exc:
            raise ProviderError(f"сбой запроса к Anthropic API: {type(exc).__name__}") from None
        except (RecursionError, ValueError) as exc:
            # Ответ вложен глубже, чем SDK разбирает (`RecursionError`), или блок `tool_use` глубже,
            # чем он может отправить обратно в следующем запросе (`ValueError`): история хода
            # испорчена, и это сбой хода, а не ошибка аргументов, о которой можно сообщить модели.
            # Тексты исключений не цитируются: в них бывают куски ответа.
            raise ProviderError(
                f"ответ Anthropic API не удалось обработать: {type(exc).__name__}"
            ) from None

    def _safe_text(self, text: str) -> str:
        """Текст ошибки сервера без ключа и не длиннее `MAX_ERROR_CHARS`."""
        # Ключ вырезается до обрезки: иначе обрезка могла бы оставить его начало.
        for secret in (self._client.api_key, self._client.auth_token):
            if secret:
                text = text.replace(secret, "***")
        return text[:MAX_ERROR_CHARS]


def _cached_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "cache_control": _CACHE_MARK}


def _tool_param(tool: ToolSchema) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


def _usage_of(response: BetaMessage) -> Usage:
    return Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=response.usage.cache_read_input_tokens or 0,
        cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
    )


def _text_of(response: BetaMessage) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _raise_if_refused(response: BetaMessage) -> None:
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise ProviderRefusal(f"модель отказалась отвечать (категория: {category or 'не указана'})")


def _final_result(response: BetaMessage, trace: list[ToolCallTrace], usage: Usage) -> TurnResult:
    stop_reason = response.stop_reason
    if stop_reason not in _FINAL_STOP_REASONS:
        raise ProviderError(f"неожиданная причина остановки модели: {stop_reason!r}")
    text = _text_of(response)
    if stop_reason == "max_tokens" and not text:
        raise ProviderError("ответ модели оборван по max_tokens до итогового текста")
    return TurnResult(text=text, trace=tuple(trace), usage=usage, stop_reason=stop_reason)


async def _run_tools(
    response: BetaMessage, tools: ToolExecutor, trace: list[ToolCallTrace]
) -> list[dict[str, Any]]:
    """Выполняет по порядку все `tool_use` ответа; блоки `tool_result` идут одним сообщением."""
    results: list[dict[str, Any]] = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        args = dict(block.input)
        try:
            # Трасса хода и журнал хранят аргументы JSON-ом: вложенность, которую json.dumps не
            # потянет (его предел ниже, чем у разбора ответа), отвергается до выполнения.
            json.dumps(args)
        except RecursionError:
            raise ProviderError("аргументы вызова инструмента вложены слишком глубоко") from None
        result, is_error = await tools(block.name, args)
        trace.append(make_trace_call(block.name, args, result, is_error))
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
                "is_error": is_error,
            }
        )
    if not results:
        raise ProviderError("модель остановилась на tool_use, но блоков tool_use в ответе нет")
    return results
