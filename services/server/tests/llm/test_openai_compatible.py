"""Контрактные тесты адаптера `openai_compatible`.

Живой модели и сети здесь нет. Адаптер получает настоящий `AsyncOpenAI`, у которого вместо сети
стоит `httpx2.MockTransport`: SDK сам собирает запрос и разбирает записанные JSON-ответы Chat
Completions, а тест видит тело ушедшего запроса. Так проверяется то, что действительно уходит
серверу (`tools`, `tool_calls`, `response_format`), а не вызовы заглушки.
"""

import asyncio
import json
import socket
from collections import deque
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel, ConfigDict, Field

from dnd_server.llm.base import (
    MAX_TRACE_ARGS_CHARS,
    JsonRequest,
    LLMProvider,
    ProviderError,
    ProviderRefusal,
    ToolLoopExceeded,
    ToolSchema,
    TurnRequest,
    Usage,
)
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider

API_KEY = "sk-compat-test-secret"
HANG = object()  # ответ, который не приходит никогда: нужен тестам тайм-аута


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Подстраховка: любая попытка открыть сокет в этих тестах — ошибка теста."""

    def refuse(self, address):
        raise AssertionError(f"тест пытался выйти в сеть: {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


# --- записанные ответы Chat Completions ---


def token_usage(prompt: int = 100, completion: int = 20, cached: int = 0) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def completion(
    message: dict[str, Any],
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "compat-model",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": usage if usage is not None else token_usage(),
    }


def says(content: str, finish_reason: str = "stop", **kwargs: Any) -> dict[str, Any]:
    message = {"role": "assistant", "content": content, "refusal": None}
    return completion(message, finish_reason, **kwargs)


def function_call(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    """Вызов функции; `arguments` — готовая строка (в том числе битая) или словарь."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def calls_tools(
    *calls: dict[str, Any], finish_reason: str = "tool_calls", usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    message = {"role": "assistant", "content": None, "tool_calls": list(calls)}
    return completion(message, finish_reason, usage=usage)


def error_response(status: int, message: str, param: str | None = None) -> httpx2.Response:
    error = {"message": message, "type": "invalid_request_error", "param": param, "code": None}
    return httpx2.Response(status, json={"error": error})


def response_format_rejected() -> httpx2.Response:
    return error_response(
        400,
        "Invalid parameter: 'response_format' of type 'json_schema' is not supported.",
        param="response_format",
    )


class FakeApi:
    """HTTP-слой вместо совместимого сервера: отдаёт ответы из очереди и запоминает запросы.

    Элемент очереди: словарь — тело ответа 200; `httpx2.Response` — как есть; исключение —
    поднимается (сбой транспорта); кортеж `(секунды, тело)` — ответ с задержкой; `HANG` — не
    отвечает никогда."""

    def __init__(self, *queue: Any) -> None:
        self._queue = deque(queue)
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        assert self._queue, "в очереди FakeApi закончились ответы: лишний запрос к серверу"
        item = self._queue.popleft()
        if item is HANG:
            await asyncio.Event().wait()
        if isinstance(item, tuple):
            delay, item = item
            await asyncio.sleep(delay)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(200, json=item)

    def make_provider(
        self, model: str = "compat-model", cheap_model: str | None = None
    ) -> OpenAICompatibleProvider:
        client = openai.AsyncOpenAI(
            api_key=API_KEY,
            base_url="http://compat.invalid/v1",
            max_retries=0,  # повторы 429/5xx — дело SDK и в этих тестах не нужны
            http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handle)),
        )
        return OpenAICompatibleProvider(client, model, cheap_model)


class ScriptedTools:
    """Исполнитель инструментов: записывает вызовы, ответы заданы по имени инструмента."""

    def __init__(self, **answers: tuple[str, bool]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = answers

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return self._answers.get(name, ('{"ok": true}', False))


def make_request(**overrides: Any) -> TurnRequest:
    fields: dict[str, Any] = {
        "system": "система",
        "adventure_brief": "сводка",
        "context": "контекст",
        "tools": (
            ToolSchema(
                name="request_check",
                description="Проверка навыка",
                input_schema={"type": "object", "properties": {"dc": {"type": "integer"}}},
            ),
            ToolSchema("chronicle", "Запись в летопись", {"type": "object", "properties": {}}),
        ),
    }
    return TurnRequest(**(fields | overrides))


class Weaving(BaseModel):
    hooks: list[str] = Field(min_length=2)


class StrictWeaving(BaseModel):
    """Схема, которая отвергает лишние поля: имя лишнего ключа попадает в текст ошибки."""

    model_config = ConfigDict(extra="forbid")

    hooks: list[str]


# Имена лишних ключей, найденные проверкой безопасности: запись в буфер обмена (OSC 52) и стирание
# строки возвратом каретки. Модель или чужой сервер выбирает их сам.
HOSTILE_KEYS = {
    "osc52-буфер-обмена": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "\x1b[2K\rПриключение записано",
}


def make_json_request(**overrides: Any) -> JsonRequest:
    fields: dict[str, Any] = {"system": "система", "prompt": "запрос", "purpose": "weaving"}
    return JsonRequest(**(fields | overrides))


def valid_json_reply() -> dict[str, Any]:
    return says(json.dumps({"hooks": ["крюк раз", "крюк два"]}, ensure_ascii=False))


# --- ход мастера: запрос ---


async def test_provider_satisfies_the_llm_provider_protocol():
    provider: LLMProvider = FakeApi().make_provider()

    assert provider.name == "openai_compatible"


async def test_the_provider_names_the_model_of_the_turn_and_not_the_cheap_one():
    # Журнал хода мастера берёт модель у собранного провайдера, а не повторяет правило фабрики.
    provider = FakeApi().make_provider(model="compat-test-main", cheap_model="compat-test-cheap")

    assert provider.model == "compat-test-main"


async def test_turn_request_puts_system_and_brief_first_then_context():
    api = FakeApi(says("Рассказ."))

    await api.make_provider(model="local-model").run_turn(make_request(), ScriptedTools())

    body = api.requests[0]
    assert body["model"] == "local-model"
    assert body["messages"] == [
        {"role": "system", "content": "система\n\nсводка"},
        {"role": "user", "content": "контекст"},
    ]
    assert body["max_completion_tokens"] == 16000
    assert "response_format" not in body


async def test_turn_request_passes_tools_as_functions_in_given_order():
    api = FakeApi(says("Рассказ."))

    await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert api.requests[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "request_check",
                "description": "Проверка навыка",
                "parameters": {"type": "object", "properties": {"dc": {"type": "integer"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "chronicle",
                "description": "Запись в летопись",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


async def test_turn_without_tool_schemas_sends_no_tools_field():
    # Часть серверов отвечает 400 на пустой список инструментов.
    api = FakeApi(says("Рассказ."))

    await api.make_provider().run_turn(make_request(tools=()), ScriptedTools())

    assert "tools" not in api.requests[0]


# --- ход мастера: цикл инструментов ---


async def test_turn_without_tools_returns_text_and_usage():
    api = FakeApi(says("Рассказ.", usage=token_usage(prompt=120, completion=30, cached=90)))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == "Рассказ."
    assert result.trace == ()
    assert result.stop_reason == "end_turn"
    # Как у Anthropic: input_tokens без кешированной части, она отдельно в cache_read_tokens.
    assert result.usage == Usage(input_tokens=30, output_tokens=30, cache_read_tokens=90)


async def test_two_tool_calls_in_one_reply_are_run_in_order_with_one_tool_message_each():
    api = FakeApi(
        calls_tools(
            function_call("call_1", "request_check", {"dc": 15}),
            function_call("call_2", "chronicle", {"entry": "запись"}),
        ),
        says("Рассказ."),
    )
    tools = ScriptedTools(request_check=('{"total": 17}', False), chronicle=("записано", False))

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == [("request_check", {"dc": 15}), ("chronicle", {"entry": "запись"})]
    history = api.requests[1]["messages"]
    assert [message["role"] for message in history] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert [call["id"] for call in history[2]["tool_calls"]] == ["call_1", "call_2"]
    assert history[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"total": 17}'}
    assert history[4] == {"role": "tool", "tool_call_id": "call_2", "content": "записано"}
    assert result.text == "Рассказ."
    assert [call.name for call in result.trace] == ["request_check", "chronicle"]
    assert result.trace[0].args == {"dc": 15}
    assert result.trace[0].result == '{"total": 17}'
    assert not result.trace[0].is_error


async def test_tool_error_result_goes_to_the_model_and_is_marked_in_trace():
    api = FakeApi(calls_tools(function_call("call_1", "request_check", {"dc": 7})), says("Ок."))
    tools = ScriptedTools(request_check=("Сл должна быть из таблицы", True))

    result = await api.make_provider().run_turn(make_request(), tools)

    assert api.requests[1]["messages"][3]["content"] == "Сл должна быть из таблицы"
    assert result.trace[0].is_error is True
    assert result.text == "Ок."


async def test_broken_json_arguments_become_a_tool_error_and_the_turn_goes_on():
    api = FakeApi(
        calls_tools(function_call("call_1", "request_check", '{"dc": 15')),  # обрыв JSON
        says("Рассказ."),
    )
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []  # инструмент не вызван с мусором
    tool_message = api.requests[1]["messages"][3]
    assert tool_message["tool_call_id"] == "call_1"
    assert "request_check" in tool_message["content"]
    assert "JSON" in tool_message["content"]
    assert result.trace[0].is_error is True
    assert result.trace[0].name == "request_check"
    assert result.text == "Рассказ."


@pytest.mark.parametrize("arguments", ["[1, 2]", '"строка"', "15", "null"])
async def test_json_arguments_that_are_not_an_object_become_a_tool_error(arguments):
    api = FakeApi(calls_tools(function_call("call_1", "request_check", arguments)), says("Ок."))
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []
    assert result.trace[0].is_error is True
    assert "объект" in api.requests[1]["messages"][3]["content"]


async def test_empty_arguments_string_means_no_arguments():
    # Так часть серверов отдаёт вызов инструмента без параметров.
    api = FakeApi(calls_tools(function_call("call_1", "chronicle", "")), says("Ок."))
    tools = ScriptedTools()

    await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == [("chronicle", {})]


async def test_tool_calls_are_run_even_when_the_server_reports_stop():
    # Некоторые совместимые серверы отдают вызовы инструментов с finish_reason "stop".
    api = FakeApi(
        calls_tools(function_call("call_1", "chronicle", {"entry": "a"}), finish_reason="stop"),
        says("Итог."),
    )
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == [("chronicle", {"entry": "a"})]
    assert result.text == "Итог."


async def test_trace_is_bounded_but_the_tool_gets_full_arguments():
    huge = "я" * (MAX_TRACE_ARGS_CHARS + 500)
    api = FakeApi(calls_tools(function_call("call_1", "chronicle", {"entry": huge})), says("Ок."))
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls[0][1] == {"entry": huge}
    assert set(result.trace[0].args) == {"_truncated"}


async def test_broken_arguments_in_trace_are_bounded_too():
    garbage = "{" + "я" * (MAX_TRACE_ARGS_CHARS + 500)
    api = FakeApi(calls_tools(function_call("call_1", "chronicle", garbage)), says("Ок."))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert set(result.trace[0].args) == {"_truncated"}


# --- слишком глубокая вложенность в ответах модели ---
#
# Патологический ответ (prompt injection или чужой сервер) не должен ронять ход исключением мимо
# контракта адаптера: `RecursionError` и `ValueError` SDK становятся `ProviderError`, а аргументы
# вызова, которые нельзя разобрать, — ошибкой для модели.

BEYOND_PARSER_DEPTH = 100_000  # глубже, чем json.loads способен разобрать
SDK_LIMIT_DEPTH = 1_000  # SDK такой ответ разберёт, но отправить его обратно уже не сможет
TRACE_WINDOW_CANDIDATES = (5_000, 10_000, 20_000, 30_000, 40_000, 50_000)


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def deep_response(body: dict[str, Any], depth: int) -> httpx2.Response:
    """Ответ 200, в котором строка `DEEP` заменена массивами, вложенными `depth` раз."""
    text = json.dumps(body).replace('"DEEP"', nested(depth))
    return httpx2.Response(200, content=text.encode(), headers={"content-type": "application/json"})


def arguments_that_json_parses_but_trace_cannot_write() -> str:
    """Аргументы, которые json.loads ещё разбирает, а json.dumps (им пишется трасса) уже нет."""
    for depth in TRACE_WINDOW_CANDIDATES:
        raw = '{"a":' * depth + "1" + "}" * depth
        try:
            parsed = json.loads(raw)
        except RecursionError:
            continue
        try:
            json.dumps(parsed)
        except RecursionError:
            return raw
    pytest.skip("в этой версии Python у json.loads и json.dumps одинаковый предел вложенности")


@pytest.mark.parametrize(
    "window", [False, True], ids=["глубже предела разбора", "глубже предела трассы"]
)
async def test_arguments_nested_too_deep_become_a_tool_error_and_the_turn_goes_on(window):
    arguments = (
        arguments_that_json_parses_but_trace_cannot_write()
        if window
        else nested(BEYOND_PARSER_DEPTH)
    )
    api = FakeApi(
        calls_tools(function_call("call_1", "request_check", arguments)), says("Рассказ.")
    )
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []  # инструмент не вызван с патологическими аргументами
    tool_message = api.requests[1]["messages"][3]
    assert tool_message["tool_call_id"] == "call_1"
    assert "request_check" in tool_message["content"]
    assert "вложен" in tool_message["content"]
    assert result.trace[0].is_error is True
    assert result.text == "Рассказ."


@pytest.mark.parametrize("depth", [BEYOND_PARSER_DEPTH, SDK_LIMIT_DEPTH])
async def test_server_message_too_deep_for_the_sdk_is_a_provider_error(depth):
    # Лишнее поле сообщения сервера SDK сохраняет и отправил бы обратно в следующем запросе;
    # при глубине сверх предела разбора тело ответа не разбирается вовсе.
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [function_call("call_1", "chronicle", {"entry": "a"})],
        "extra": "DEEP",
    }
    api = FakeApi(deep_response(completion(message, "tool_calls"), depth), says("Итог."))

    with pytest.raises(ProviderError, match=r"ValueError|RecursionError"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_complete_json_response_too_deep_to_parse_is_a_provider_error():
    api = FakeApi(deep_response(valid_json_reply() | {"extra": "DEEP"}, BEYOND_PARSER_DEPTH))

    with pytest.raises(ProviderError, match="RecursionError"):
        await api.make_provider().complete_json(make_json_request(), Weaving)


async def test_complete_json_content_nested_too_deep_is_retried_as_invalid_schema():
    deep = says(nested(BEYOND_PARSER_DEPTH))
    api = FakeApi(deep, deep, deep)

    with pytest.raises(ProviderError, match="weaving"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 3  # первая попытка и два повтора, как при любом неверном ответе


async def test_usage_is_summed_over_all_iterations():
    api = FakeApi(
        calls_tools(
            function_call("call_1", "chronicle", {"entry": "a"}),
            usage=token_usage(prompt=100, completion=10, cached=0),
        ),
        calls_tools(
            function_call("call_2", "chronicle", {"entry": "b"}),
            usage=token_usage(prompt=110, completion=20, cached=64),
        ),
        says("Итог.", usage=token_usage(prompt=130, completion=40, cached=128)),
    )

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.usage == Usage(input_tokens=148, output_tokens=70, cache_read_tokens=192)


async def test_missing_usage_counts_as_zero():
    # Не все совместимые серверы возвращают расход токенов.
    api = FakeApi(says("Рассказ.") | {"usage": None})

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.usage == Usage()


async def test_tool_loop_stops_at_the_iteration_limit():
    api = FakeApi(
        *[calls_tools(function_call(f"call_{i}", "chronicle", {"entry": "a"})) for i in range(5)]
    )
    tools = ScriptedTools()

    with pytest.raises(ToolLoopExceeded):
        await api.make_provider().run_turn(make_request(max_tool_iterations=2), tools)

    # Две итерации инструментов выполнены, третий запрос инструментов — отказ без выполнения.
    assert len(tools.calls) == 2
    assert len(api.requests) == 3


async def test_a_turn_that_ends_exactly_at_the_iteration_limit_succeeds():
    api = FakeApi(
        calls_tools(function_call("call_1", "chronicle", {"entry": "a"})),
        calls_tools(function_call("call_2", "chronicle", {"entry": "b"})),
        says("Итог."),
    )

    result = await api.make_provider().run_turn(
        make_request(max_tool_iterations=2), ScriptedTools()
    )

    assert result.text == "Итог."


async def test_tool_calls_finish_without_tool_calls_is_an_error():
    api = FakeApi(completion({"role": "assistant", "content": "Хм."}, "tool_calls"))

    with pytest.raises(ProviderError, match="tool_calls"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_non_function_tool_call_is_an_error():
    custom = {"id": "call_1", "type": "custom", "custom": {"name": "x", "input": "y"}}
    api = FakeApi(calls_tools(custom))

    with pytest.raises(ProviderError, match="custom"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


# --- ход мастера: исходы ---


async def test_length_without_text_is_an_error():
    api = FakeApi(says("", finish_reason="length"))

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_calls_cut_off_by_length_are_not_run():
    # Ответ оборван по длине: аргументы вызова могли обрезаться, исполнять их нельзя.
    api = FakeApi(
        calls_tools(function_call("call_1", "chronicle", {"entry": "a"}), finish_reason="length")
    )
    tools = ScriptedTools()

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []


async def test_length_with_text_returns_it_and_reports_max_tokens():
    api = FakeApi(says("Рассказ оборвался на полу", finish_reason="length"))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == "Рассказ оборвался на полу"
    assert result.stop_reason == "max_tokens"


async def test_stop_without_text_returns_empty_text_for_the_orchestrator_retry():
    api = FakeApi(completion({"role": "assistant", "content": None}, "stop"))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == ""
    assert result.stop_reason == "end_turn"


async def test_content_filter_is_a_refusal():
    api = FakeApi(says("", finish_reason="content_filter"))

    with pytest.raises(ProviderRefusal, match="content_filter"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_message_refusal_is_a_refusal():
    message = {"role": "assistant", "content": None, "refusal": "Не могу этого сделать."}
    api = FakeApi(completion(message, "stop"))

    with pytest.raises(ProviderRefusal):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_response_without_choices_is_an_error():
    api = FakeApi(says("Рассказ.") | {"choices": []})

    with pytest.raises(ProviderError, match=r"choices|вариант"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_unexpected_finish_reason_is_an_error():
    api = FakeApi(says("...", finish_reason="function_call"))

    with pytest.raises(ProviderError, match="function_call"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


# --- ход мастера: тайм-аут и ошибки API ---


async def test_turn_timeout_raises_provider_error():
    api = FakeApi(HANG)

    # Внешняя страховка теста: без тайм-аута адаптера тест упадёт, а не повиснет навсегда.
    async with asyncio.timeout(5):
        with pytest.raises(ProviderError, match="тайм-аут"):
            await api.make_provider().run_turn(make_request(timeout_s=0.05), ScriptedTools())


async def test_turn_timeout_covers_the_whole_turn_not_a_single_request():
    # Каждый запрос короче лимита, но вместе они его превышают.
    api = FakeApi(
        (0.05, calls_tools(function_call("call_1", "chronicle", {"entry": "a"}))),
        (0.05, calls_tools(function_call("call_2", "chronicle", {"entry": "b"}))),
        (0.05, says("Итог.")),
    )

    with pytest.raises(ProviderError, match="тайм-аут"):
        await api.make_provider().run_turn(make_request(timeout_s=0.08), ScriptedTools())


async def test_sdk_request_timeout_raises_provider_error():
    api = FakeApi(httpx2.ReadTimeout("медленный ответ"))

    with pytest.raises(ProviderError, match="тайм-аут"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_timeout_error_from_a_tool_is_not_mistaken_for_the_turn_timeout():
    class FailingTools:
        async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
            raise TimeoutError("таймаут базы данных")

    api = FakeApi(calls_tools(function_call("call_1", "chronicle", {"entry": "a"})))

    with pytest.raises(TimeoutError, match="базы данных"):
        await api.make_provider().run_turn(make_request(), FailingTools())


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (error_response(429, "rate limit"), "429"),
        (error_response(500, "внутренняя ошибка"), "500"),
        (httpx2.ConnectError("нет связи"), "соединени"),
    ],
)
async def test_api_failures_become_provider_errors(response, expected):
    api = FakeApi(response)

    with pytest.raises(ProviderError, match=expected):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_api_key_never_reaches_error_text_or_repr():
    # Шлюз может вернуть ключ в теле ошибки; в исключение он попасть не должен.
    api = FakeApi(error_response(401, f"Incorrect API key provided: {API_KEY}"))
    provider = api.make_provider()

    with pytest.raises(ProviderError) as caught:
        await provider.run_turn(make_request(), ScriptedTools())

    assert "401" in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert API_KEY not in repr(caught.value)
    assert API_KEY not in repr(provider)
    # Цепочка исключений не хранит исходное сообщение сервера, где ключ мог остаться.
    assert caught.value.__cause__ is None


async def test_long_server_message_is_clipped_in_error_text():
    api = FakeApi(error_response(500, "x" * 5000))

    with pytest.raises(ProviderError) as caught:
        await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert len(str(caught.value)) < 1000


# --- complete_json ---


async def test_complete_json_returns_a_validated_schema_instance():
    api = FakeApi(valid_json_reply())

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result == Weaving(hooks=["крюк раз", "крюк два"])


async def test_complete_json_asks_for_json_schema_response_format_first():
    api = FakeApi(valid_json_reply())

    await api.make_provider(model="local-model").complete_json(make_json_request(), Weaving)

    body = api.requests[0]
    assert body["model"] == "local-model"
    assert body["messages"] == [
        {"role": "system", "content": "система"},
        {"role": "user", "content": "запрос"},
    ]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"]["properties"]["hooks"]["minItems"] == 2
    assert response_format["json_schema"]["name"]
    assert "tools" not in body


async def test_complete_json_falls_back_to_a_prompt_instruction_when_response_format_is_rejected():
    api = FakeApi(response_format_rejected(), valid_json_reply())

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result.hooks == ["крюк раз", "крюк два"]
    assert len(api.requests) == 2
    fallback = api.requests[1]
    assert "response_format" not in fallback
    prompt = fallback["messages"][1]["content"]
    assert prompt.startswith("запрос")
    assert "JSON" in prompt  # инструкция «отвечай только JSON»
    assert '"hooks"' in prompt  # и сама схема


async def test_rejected_response_format_is_remembered_for_the_next_calls():
    api = FakeApi(response_format_rejected(), valid_json_reply(), valid_json_reply())
    provider = api.make_provider()

    await provider.complete_json(make_json_request(), Weaving)
    await provider.complete_json(make_json_request(), Weaving)

    # Третий запрос — второй вызов complete_json: сразу без response_format, без лишней попытки.
    assert len(api.requests) == 3
    assert "response_format" not in api.requests[2]
    assert '"hooks"' in api.requests[2]["messages"][1]["content"]


async def test_bad_request_about_something_else_is_not_a_fallback():
    api = FakeApi(error_response(400, "model not found", param="model"), valid_json_reply())

    with pytest.raises(ProviderError, match="400"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 1


async def test_second_rejection_after_the_fallback_is_an_error():
    api = FakeApi(response_format_rejected(), error_response(400, "bad request"))

    with pytest.raises(ProviderError, match="400"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 2


async def test_invalid_answer_is_retried_with_the_validation_error_in_the_request():
    api = FakeApi(
        says(json.dumps({"hooks": ["один"]})),  # меньше двух элементов
        says("это не JSON"),
        valid_json_reply(),
    )

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result.hooks == ["крюк раз", "крюк два"]
    assert len(api.requests) == 3
    second_prompt = api.requests[1]["messages"][1]["content"]
    assert second_prompt.startswith("запрос")  # исходный запрос сохранён
    assert "hooks" in second_prompt  # а в конец добавлена ошибка проверки с путём поля
    third_prompt = api.requests[2]["messages"][1]["content"]
    assert third_prompt.startswith("запрос")
    assert third_prompt != second_prompt  # в запрос попала ошибка последней попытки
    assert all("response_format" in request for request in api.requests)


async def test_complete_json_gives_up_after_two_retries():
    invalid = says(json.dumps({"hooks": []}))
    api = FakeApi(invalid, invalid, invalid, invalid)

    with pytest.raises(ProviderError, match="Weaving"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 3  # первая попытка и ровно два повтора


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
async def test_extra_key_with_terminal_escapes_is_escaped_in_the_error_and_the_retry_prompts(key):
    hostile = says(json.dumps({"hooks": ["крюк"], key: 1}))
    api = FakeApi(hostile, hostile, hostile)

    with pytest.raises(ProviderError) as error:
        await api.make_provider().complete_json(make_json_request(), StrictWeaving)

    assert str(error.value).isprintable()  # ни ESC, ни BEL, ни возврата каретки
    assert "\\x1b" in str(error.value)  # запись видна глазами, а не потеряна
    for request in api.requests[1:]:  # промпт повтора строится из того же текста
        prompt = request["messages"][1]["content"]
        assert not {"\x1b", "\x07", "\r"} & set(prompt)


async def test_fallback_does_not_use_up_the_retries():
    invalid = says(json.dumps({"hooks": []}))
    api = FakeApi(response_format_rejected(), invalid, invalid, valid_json_reply())

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result.hooks == ["крюк раз", "крюк два"]
    assert len(api.requests) == 4  # отклонённый запрос + первая попытка + два повтора


async def test_json_wrapped_in_a_markdown_fence_is_accepted():
    # Без response_format модели часто оборачивают ответ в ```json.
    fenced = '```json\n{"hooks": ["один", "два"]}\n```'
    api = FakeApi(says(fenced))

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result.hooks == ["один", "два"]


async def test_complete_json_refusal_is_not_retried():
    message = {"role": "assistant", "content": None, "refusal": "Не могу."}
    api = FakeApi(completion(message), valid_json_reply())

    with pytest.raises(ProviderRefusal):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 1


async def test_complete_json_truncated_answer_is_an_error_without_retry():
    api = FakeApi(says('{"hooks": ["обор', finish_reason="length"), valid_json_reply())

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 1


async def test_complete_json_api_failure_becomes_provider_error():
    api = FakeApi(error_response(500, "сбой"))

    with pytest.raises(ProviderError, match="500"):
        await api.make_provider().complete_json(make_json_request(), Weaving)


async def test_cheap_request_uses_the_cheap_model_when_it_is_set():
    api = FakeApi(valid_json_reply(), valid_json_reply())
    provider = api.make_provider(cheap_model="small-model")

    await provider.complete_json(make_json_request(cheap=True), Weaving)
    await provider.complete_json(make_json_request(cheap=False), Weaving)

    assert [request["model"] for request in api.requests] == ["small-model", "compat-model"]


async def test_cheap_request_without_cheap_model_uses_the_main_one():
    api = FakeApi(valid_json_reply())

    await api.make_provider().complete_json(make_json_request(cheap=True), Weaving)

    assert api.requests[0]["model"] == "compat-model"


async def test_a_request_on_the_model_of_the_game_goes_to_the_main_model():
    # Идеи у костра просят модель игры (`game_model=True`, не дешёвый запрос): у API это основная
    # модель, даже когда дешёвая модель задана.
    api = FakeApi(valid_json_reply())
    provider = api.make_provider(cheap_model="small-model")

    request = make_json_request(purpose="campfire.ideas", game_model=True)
    await provider.complete_json(request, Weaving)

    assert api.requests[0]["model"] == "compat-model"
