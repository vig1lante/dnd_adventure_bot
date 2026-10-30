"""Контрактные тесты адаптера `anthropic_api`.

Живой модели и сети здесь нет. Адаптер получает настоящий `AsyncAnthropic`, у которого вместо
сети стоит `httpx2.MockTransport`: SDK сам собирает запрос и разбирает записанные JSON-ответы
Messages API, а тест видит тело и заголовки ушедшего запроса. Поэтому проверяется то, что
действительно уходит по проводу (`cache_control`, `thinking`, `fallbacks`), а не вызовы заглушки.
"""

import asyncio
import json
import socket
from collections import deque
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field

from dnd_server.llm.anthropic_api import AnthropicProvider
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

API_KEY = "sk-ant-api03-test-secret"
HANG = object()  # ответ, который не приходит никогда: нужен тестам тайм-аута


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Подстраховка: любая попытка открыть сокет в этих тестах — ошибка теста."""

    def refuse(self, address):
        raise AssertionError(f"тест пытался выйти в сеть: {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


# --- записанные ответы Messages API ---


def usage(
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def reply(
    *content: dict[str, Any],
    stop_reason: str = "end_turn",
    token_usage: dict[str, int] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_01TestTestTestTest",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": token_usage or usage(),
    }


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def thinking() -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def tool_use(call_id: str, name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def calls_tool(
    *blocks: dict[str, Any], token_usage: dict[str, int] | None = None
) -> dict[str, Any]:
    """Ответ, в котором модель просит выполнить инструменты."""
    return reply(*blocks, stop_reason="tool_use", token_usage=token_usage)


def refusal(category: str | None = "cyber") -> dict[str, Any]:
    return reply(
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": category, "explanation": None},
    )


def status_error(status: int, message: str) -> httpx2.Response:
    error = {"type": "authentication_error" if status == 401 else "api_error", "message": message}
    return httpx2.Response(status, json={"type": "error", "error": error})


class FakeApi:
    """HTTP-слой вместо Anthropic: отдаёт ответы из очереди и запоминает запросы.

    Элемент очереди: словарь — тело ответа 200; `httpx2.Response` — как есть; исключение —
    поднимается (сбой транспорта); кортеж `(секунды, тело)` — ответ с задержкой; `HANG` — не
    отвечает никогда."""

    def __init__(self, *queue: Any) -> None:
        self._queue = deque(queue)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        assert self._queue, "в очереди FakeApi закончились ответы: лишний запрос к API"
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
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        cheap_model: str | None = None,
    ) -> AnthropicProvider:
        client = anthropic.AsyncAnthropic(
            api_key=API_KEY,
            max_retries=0,  # повторы 429/5xx — дело SDK и в этих тестах не нужны
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self.handle)
            ),
        )
        return AnthropicProvider(client, model, effort, cheap_model)


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
# строки возвратом каретки. Модель выбирает их сама.
HOSTILE_KEYS = {
    "osc52-буфер-обмена": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "\x1b[2K\rПриключение записано",
}


def make_json_request(**overrides: Any) -> JsonRequest:
    fields: dict[str, Any] = {"system": "система", "prompt": "запрос", "purpose": "weaving"}
    return JsonRequest(**(fields | overrides))


def valid_json_reply() -> dict[str, Any]:
    return reply(text(json.dumps({"hooks": ["крюк раз", "крюк два"]}, ensure_ascii=False)))


# --- ход мастера: запрос ---


async def test_provider_satisfies_the_llm_provider_protocol():
    provider: LLMProvider = FakeApi().make_provider()

    assert provider.name == "anthropic_api"


async def test_the_provider_names_the_model_of_the_turn_and_not_the_cheap_one():
    # Журнал хода мастера берёт модель у собранного провайдера, а не повторяет правило фабрики.
    provider = FakeApi().make_provider(model="claude-test-main", cheap_model="claude-test-cheap")

    assert provider.model == "claude-test-main"


async def test_turn_request_marks_stable_parts_for_caching_and_sets_reasoning():
    api = FakeApi(reply(text("Рассказ.")))

    await api.make_provider(effort="high").run_turn(make_request(), ScriptedTools())

    body = api.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 16000
    # system — один блок с cache_control; сводка — первый блок user-сообщения с cache_control;
    # динамический контекст идёт после сводки и без cache_control.
    assert body["system"] == [
        {"type": "text", "text": "система", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "сводка", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "контекст"},
            ],
        }
    ]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert "temperature" not in body


async def test_turn_request_passes_tools_in_given_order_with_plain_schema():
    api = FakeApi(reply(text("Рассказ.")))

    await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert api.requests[0]["tools"] == [
        {
            "name": "request_check",
            "description": "Проверка навыка",
            "input_schema": {"type": "object", "properties": {"dc": {"type": "integer"}}},
        },
        {
            "name": "chronicle",
            "description": "Запись в летопись",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


async def test_turn_request_opts_into_server_side_fallback():
    api = FakeApi(reply(text("Рассказ.")))

    await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert api.requests[0]["fallbacks"] == "default"
    assert api.headers[0]["anthropic-beta"] == "server-side-fallback-2026-07-01"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_effort_from_config_goes_to_output_config(effort):
    api = FakeApi(reply(text("Рассказ.")))

    await api.make_provider(effort=effort).run_turn(make_request(), ScriptedTools())

    assert api.requests[0]["output_config"] == {"effort": effort}


def test_unknown_effort_is_rejected_at_construction():
    # Опечатка в LLM_EFFORT не должна всплыть только как 400 посреди первой игры.
    with pytest.raises(ValueError, match=r"LLM_EFFORT|effort"):
        FakeApi().make_provider(effort="turbo")


# --- ход мастера: цикл инструментов ---


async def test_turn_without_tools_returns_joined_text_and_usage():
    api = FakeApi(
        reply(text("Первая часть. "), text("Вторая часть."), token_usage=usage(120, 30, 90, 15))
    )

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == "Первая часть. Вторая часть."
    assert result.trace == ()
    assert result.stop_reason == "end_turn"
    assert result.usage == Usage(
        input_tokens=120, output_tokens=30, cache_read_tokens=90, cache_write_tokens=15
    )


async def test_two_tool_uses_in_one_reply_get_all_results_in_one_user_message():
    api = FakeApi(
        calls_tool(
            thinking(),
            tool_use("toolu_1", "request_check", dc=15),
            tool_use("toolu_2", "chronicle", entry="запись"),
        ),
        reply(text("Рассказ.")),
    )
    tools = ScriptedTools(request_check=('{"total": 17}', False), chronicle=("записано", False))

    result = await api.make_provider().run_turn(make_request(), tools)

    # Инструменты выполнены по порядку, с аргументами модели.
    assert tools.calls == [("request_check", {"dc": 15}), ("chronicle", {"entry": "запись"})]
    # Второй запрос: история + ответ модели целиком + ОДНО user-сообщение с обоими результатами.
    history = api.requests[1]["messages"]
    assert [message["role"] for message in history] == ["user", "assistant", "user"]
    echoed = history[1]["content"]
    assert [block["type"] for block in echoed] == ["thinking", "tool_use", "tool_use"]
    assert echoed[0]["signature"] == "sig-abc"  # thinking-блок вернулся без изменений
    assert history[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"total": 17}',
            "is_error": False,
        },
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "записано", "is_error": False},
    ]
    assert result.text == "Рассказ."
    assert [call.name for call in result.trace] == ["request_check", "chronicle"]
    assert result.trace[0].args == {"dc": 15}
    assert result.trace[0].result == '{"total": 17}'


async def test_tool_error_is_marked_in_tool_result_and_in_trace():
    api = FakeApi(calls_tool(tool_use("toolu_1", "request_check", dc=7)), reply(text("Рассказ.")))
    tools = ScriptedTools(request_check=("Сл должна быть из таблицы", True))

    result = await api.make_provider().run_turn(make_request(), tools)

    tool_result = api.requests[1]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert tool_result["content"] == "Сл должна быть из таблицы"
    assert result.trace[0].is_error is True
    assert result.text == "Рассказ."


async def test_final_text_comes_only_from_the_last_reply():
    # Фраза перед вызовом инструмента — не рассказ: игрокам идёт текст последнего ответа.
    api = FakeApi(
        calls_tool(text("Сейчас брошу кость."), tool_use("toolu_1", "chronicle", entry="a")),
        reply(text("Итог хода.")),
    )

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == "Итог хода."


async def test_trace_is_bounded_but_the_tool_gets_full_arguments():
    huge = "я" * (MAX_TRACE_ARGS_CHARS + 500)
    api = FakeApi(calls_tool(tool_use("toolu_1", "chronicle", entry=huge)), reply(text("Ок.")))
    tools = ScriptedTools()

    result = await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls[0][1] == {"entry": huge}
    assert set(result.trace[0].args) == {"_truncated"}


# --- слишком глубокая вложенность в ответах модели ---
#
# Патологический ответ (prompt injection) не должен ронять ход исключением мимо контракта
# адаптера: `RecursionError` и `ValueError` SDK становятся `ProviderError`.

BEYOND_PARSER_DEPTH = 100_000  # глубже, чем SDK способен разобрать тело ответа
SDK_LIMIT_DEPTH = 1_000  # SDK такой ответ разберёт, но отправить его обратно уже не сможет
TRACE_WINDOW_CANDIDATES = (5_000, 10_000, 20_000, 30_000, 40_000, 50_000)


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def deep_response(body: dict[str, Any], deep_text: str) -> httpx2.Response:
    """Ответ 200, в котором строка `DEEP` заменена готовым вложенным JSON."""
    text = json.dumps(body).replace('"DEEP"', deep_text)
    return httpx2.Response(200, content=text.encode(), headers={"content-type": "application/json"})


def object_that_json_parses_but_trace_cannot_write() -> str:
    """Объект, который json.loads ещё разбирает, а json.dumps (им пишется трасса) уже нет."""
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


@pytest.mark.parametrize("depth", [BEYOND_PARSER_DEPTH, SDK_LIMIT_DEPTH])
async def test_tool_input_too_deep_for_the_sdk_is_a_provider_error(depth):
    # При глубине сверх предела разбора тело ответа не читается вовсе; при меньшей SDK разбирает
    # его, но не может отправить блок `tool_use` обратно в следующем запросе.
    body = calls_tool(tool_use("toolu_01", "request_check", dc="DEEP"))
    api = FakeApi(deep_response(body, nested(depth)), reply(text("Итог.")))

    with pytest.raises(ProviderError, match=r"ValueError|RecursionError"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_tool_input_that_the_trace_cannot_write_is_refused_before_the_tool_runs():
    body = calls_tool(tool_use("toolu_01", "request_check", dc="DEEP"))
    api = FakeApi(
        deep_response(body, object_that_json_parses_but_trace_cannot_write()),
        reply(text("Итог.")),
    )
    tools = ScriptedTools()

    with pytest.raises(ProviderError, match="вложен"):
        await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []  # инструмент не вызван с патологическими аргументами


async def test_complete_json_response_too_deep_to_parse_is_a_provider_error():
    body = valid_json_reply() | {"extra": "DEEP"}
    api = FakeApi(deep_response(body, nested(BEYOND_PARSER_DEPTH)))

    with pytest.raises(ProviderError, match="RecursionError"):
        await api.make_provider().complete_json(make_json_request(), Weaving)


async def test_complete_json_content_nested_too_deep_is_retried_as_invalid_schema():
    deep = reply(text(nested(BEYOND_PARSER_DEPTH)))
    api = FakeApi(deep, deep, deep)

    with pytest.raises(ProviderError, match="weaving"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 3  # первая попытка и два повтора, как при любом неверном ответе


async def test_usage_is_summed_over_all_iterations():
    api = FakeApi(
        calls_tool(tool_use("toolu_1", "chronicle", entry="a"), token_usage=usage(100, 10, 0, 500)),
        calls_tool(tool_use("toolu_2", "chronicle", entry="b"), token_usage=usage(110, 20, 500, 0)),
        reply(text("Итог."), token_usage=usage(130, 40, 500, 25)),
    )

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.usage == Usage(
        input_tokens=340, output_tokens=70, cache_read_tokens=1000, cache_write_tokens=525
    )


async def test_tool_loop_stops_at_the_iteration_limit():
    api = FakeApi(*[calls_tool(tool_use(f"toolu_{i}", "chronicle", entry="a")) for i in range(5)])
    tools = ScriptedTools()

    with pytest.raises(ToolLoopExceeded):
        await api.make_provider().run_turn(make_request(max_tool_iterations=2), tools)

    # Две итерации инструментов выполнены, третий запрос инструментов — отказ без выполнения.
    assert len(tools.calls) == 2
    assert len(api.requests) == 3


async def test_a_turn_that_ends_exactly_at_the_iteration_limit_succeeds():
    api = FakeApi(
        calls_tool(tool_use("toolu_1", "chronicle", entry="a")),
        calls_tool(tool_use("toolu_2", "chronicle", entry="b")),
        reply(text("Итог.")),
    )

    result = await api.make_provider().run_turn(
        make_request(max_tool_iterations=2), ScriptedTools()
    )

    assert result.text == "Итог."


async def test_tool_use_stop_without_tool_use_blocks_is_an_error():
    # Пустое user-сообщение API отклонит; лучше понятная ошибка провайдера.
    api = FakeApi(calls_tool(text("Хм.")))

    with pytest.raises(ProviderError, match="tool_use"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


# --- ход мастера: исходы ---


async def test_refusal_raises_provider_refusal_with_category():
    api = FakeApi(refusal("cyber"))

    with pytest.raises(ProviderRefusal, match="cyber"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_refusal_without_category_is_still_a_refusal():
    api = FakeApi(refusal(None))

    with pytest.raises(ProviderRefusal):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_refusal_in_the_middle_of_the_loop_fails_the_turn():
    api = FakeApi(calls_tool(tool_use("toolu_1", "chronicle", entry="a")), refusal("bio"))

    with pytest.raises(ProviderRefusal):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_max_tokens_without_text_is_an_error():
    api = FakeApi(reply(thinking(), stop_reason="max_tokens"))

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_tool_use_cut_off_by_max_tokens_is_not_run():
    # Ответ оборван по длине: вход вызова мог обрезаться, исполнять его нельзя.
    api = FakeApi(
        reply(tool_use("toolu_1", "chronicle", entry="a"), stop_reason="max_tokens"),
    )
    tools = ScriptedTools()

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().run_turn(make_request(), tools)

    assert tools.calls == []


async def test_max_tokens_with_text_returns_it_and_keeps_the_stop_reason():
    api = FakeApi(reply(text("Рассказ оборвался на полу"), stop_reason="max_tokens"))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == "Рассказ оборвался на полу"
    assert result.stop_reason == "max_tokens"


async def test_end_turn_without_text_returns_empty_text_for_the_orchestrator_retry():
    api = FakeApi(reply(thinking()))

    result = await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert result.text == ""
    assert result.stop_reason == "end_turn"


async def test_unexpected_stop_reason_is_an_error():
    api = FakeApi(reply(text("..."), stop_reason="pause_turn"))

    with pytest.raises(ProviderError, match="pause_turn"):
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
        (0.05, calls_tool(tool_use("toolu_1", "chronicle", entry="a"))),
        (0.05, calls_tool(tool_use("toolu_2", "chronicle", entry="b"))),
        (0.05, reply(text("Итог."))),
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

    api = FakeApi(calls_tool(tool_use("toolu_1", "chronicle", entry="a")))

    with pytest.raises(TimeoutError, match="базы данных"):
        await api.make_provider().run_turn(make_request(), FailingTools())


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (status_error(429, "rate limit"), "429"),
        (status_error(500, "внутренняя ошибка"), "500"),
        (httpx2.ConnectError("нет связи"), "соединени"),
    ],
)
async def test_api_failures_become_provider_errors(response, expected):
    api = FakeApi(response)

    with pytest.raises(ProviderError, match=expected):
        await api.make_provider().run_turn(make_request(), ScriptedTools())


async def test_api_key_never_reaches_error_text_or_repr():
    # Прокси или шлюз может вернуть ключ в теле ошибки; в исключение он попасть не должен.
    api = FakeApi(status_error(401, f"invalid x-api-key {API_KEY}"))
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
    api = FakeApi(status_error(500, "x" * 5000))

    with pytest.raises(ProviderError) as caught:
        await api.make_provider().run_turn(make_request(), ScriptedTools())

    assert len(str(caught.value)) < 1000


# --- complete_json ---


async def test_complete_json_returns_a_validated_schema_instance():
    api = FakeApi(valid_json_reply())

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result == Weaving(hooks=["крюк раз", "крюк два"])


async def test_complete_json_request_carries_the_schema_and_the_main_settings():
    api = FakeApi(valid_json_reply())

    await api.make_provider(effort="low").complete_json(make_json_request(), Weaving)

    body = api.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == [
        {"type": "text", "text": "система", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "запрос"}]
    json_format = body["output_config"]["format"]
    assert json_format["type"] == "json_schema"
    assert json_format["schema"]["additionalProperties"] is False
    assert "hooks" in json_format["schema"]["properties"]
    assert body["output_config"]["effort"] == "low"
    assert body["thinking"] == {"type": "adaptive"}
    assert "tools" not in body


async def test_cheap_request_uses_the_cheap_model_without_reasoning_settings():
    api = FakeApi(valid_json_reply())
    provider = api.make_provider(cheap_model="claude-haiku-5-5")

    await provider.complete_json(make_json_request(cheap=True), Weaving)

    body = api.requests[0]
    assert body["model"] == "claude-haiku-5-5"
    # Возможности дешёвой модели неизвестны: thinking, effort и fallbacks ей не отправляются.
    assert "thinking" not in body
    assert "effort" not in body["output_config"]
    assert "fallbacks" not in body
    assert "anthropic-beta" not in api.headers[0]


async def test_cheap_request_without_cheap_model_falls_back_to_the_main_one():
    api = FakeApi(valid_json_reply())

    await api.make_provider(cheap_model=None).complete_json(make_json_request(cheap=True), Weaving)

    assert api.requests[0]["model"] == "claude-opus-5-5"
    assert api.requests[0]["thinking"] == {"type": "adaptive"}


async def test_not_cheap_request_ignores_the_cheap_model():
    api = FakeApi(valid_json_reply())
    provider = api.make_provider(cheap_model="claude-haiku-5-5")

    await provider.complete_json(make_json_request(cheap=False), Weaving)

    assert api.requests[0]["model"] == "claude-opus-5-5"


async def test_a_request_on_the_model_of_the_game_goes_to_the_main_model_with_its_effort():
    # Идеи у костра просят модель игры (`game_model=True`, не дешёвый запрос): у API это основная
    # модель с её усилиями, даже когда дешёвая модель задана.
    api = FakeApi(valid_json_reply())
    provider = api.make_provider(effort="high", cheap_model="claude-haiku-5-5")

    request = make_json_request(purpose="campfire.ideas", game_model=True)
    await provider.complete_json(request, Weaving)

    body = api.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "high"
    assert body["thinking"] == {"type": "adaptive"}


async def test_invalid_answer_is_retried_with_the_validation_error_in_the_request():
    api = FakeApi(
        reply(text(json.dumps({"hooks": ["один"]}))),  # меньше двух элементов
        reply(text("это не JSON")),
        valid_json_reply(),
    )

    result = await api.make_provider().complete_json(make_json_request(), Weaving)

    assert result.hooks == ["крюк раз", "крюк два"]
    assert len(api.requests) == 3
    second_prompt = api.requests[1]["messages"][0]["content"]
    assert second_prompt.startswith("запрос")  # исходный запрос сохранён
    assert "hooks" in second_prompt  # а в конец добавлена ошибка проверки с путём поля
    third_prompt = api.requests[2]["messages"][0]["content"]
    assert third_prompt.startswith("запрос")
    assert third_prompt != second_prompt  # в запрос попала ошибка последней попытки


async def test_complete_json_gives_up_after_two_retries():
    invalid = reply(text(json.dumps({"hooks": []})))
    api = FakeApi(invalid, invalid, invalid, invalid)

    with pytest.raises(ProviderError, match="Weaving"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 3  # первая попытка и ровно два повтора


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
async def test_extra_key_with_terminal_escapes_is_escaped_in_the_error_and_the_retry_prompts(key):
    hostile = reply(text(json.dumps({"hooks": ["крюк"], key: 1})))
    api = FakeApi(hostile, hostile, hostile)

    with pytest.raises(ProviderError) as error:
        await api.make_provider().complete_json(make_json_request(), StrictWeaving)

    assert str(error.value).isprintable()  # ни ESC, ни BEL, ни возврата каретки
    assert "\\x1b" in str(error.value)  # запись видна глазами, а не потеряна
    for request in api.requests[1:]:  # промпт повтора строится из того же текста
        prompt = request["messages"][0]["content"]
        assert not {"\x1b", "\x07", "\r"} & set(prompt)


async def test_complete_json_refusal_is_not_retried():
    api = FakeApi(refusal("cyber"), valid_json_reply())

    with pytest.raises(ProviderRefusal):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 1


async def test_complete_json_truncated_answer_is_an_error_without_retry():
    api = FakeApi(reply(text('{"hooks": ["обор'), stop_reason="max_tokens"), valid_json_reply())

    with pytest.raises(ProviderError, match="max_tokens"):
        await api.make_provider().complete_json(make_json_request(), Weaving)

    assert len(api.requests) == 1


async def test_complete_json_api_failure_becomes_provider_error():
    api = FakeApi(status_error(500, "сбой"))

    with pytest.raises(ProviderError, match="500"):
        await api.make_provider().complete_json(make_json_request(), Weaving)
