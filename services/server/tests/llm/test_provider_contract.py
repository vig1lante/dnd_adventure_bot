"""Адаптеры и общие типы `base.py`: один контракт для `fake`, `anthropic_api` и `openai_compatible`.

Оркестратор не знает, какой провайдер ведёт ход, поэтому одинаковый разговор должен давать
одинаковый `TurnResult`, общие лимиты хода действуют на всех, а то, что уходит модели (схемы
инструментов движка, схемы шагов генератора), должно проходить через обе проводные формы. Сети нет:
у настоящих SDK-клиентов вместо неё `httpx2.MockTransport`.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from pydantic import BaseModel

from dnd_engine.modules import build_ruleset
from dnd_engine.state import RulesRef
from dnd_engine.tools import build_registry
from dnd_server.authoring import schemas
from dnd_server.llm import anthropic_api, claude_code_cli, factory, openai_compatible
from dnd_server.llm.anthropic_api import AnthropicProvider
from dnd_server.llm.base import (
    JsonRequest,
    LLMProvider,
    ToolCallTrace,
    ToolLoopExceeded,
    ToolSchema,
    TurnRequest,
    Usage,
)
from dnd_server.llm.fake import FakeProvider, Scenario, load_scenario
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
ADAPTERS = ["fake", "anthropic_api", "openai_compatible"]
FINAL_TEXT = "Тени скрывают вас…"
Call = tuple[str, dict[str, Any]]
# Один разговор для всех провайдеров: бросок, потом запись, которую инструмент отклонил.
CONVERSATION: list[Call] = [("request_check", {"dc": 15}), ("chronicle", {"entry": "запись"})]
RESULTS = {
    "request_check": ('{"total": 17}', False),
    "chronicle": ("Ошибка: записи нет", True),
}


class ScriptedTools:
    def __init__(self) -> None:
        self.calls: list[Call] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return RESULTS.get(name, ('{"ok": true}', False))


# --- проводные ответы: по одному вызову инструмента за ответ, затем итоговый рассказ ---


def anthropic_wire(call: Call | None = None, text: str = "") -> dict[str, Any]:
    if call is None:
        content = [{"type": "text", "text": text}]
    else:
        content = [{"type": "tool_use", "id": "toolu_01", "name": call[0], "input": call[1]}]
    return {
        "id": "msg_01Contract",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": "end_turn" if call is None else "tool_use",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def openai_wire(call: Call | None = None, text: str = "") -> dict[str, Any]:
    if call is None:
        message: dict[str, Any] = {"role": "assistant", "content": text, "refusal": None}
    else:
        arguments = json.dumps(call[1], ensure_ascii=False)
        function = {"name": call[0], "arguments": arguments}
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_01", "type": "function", "function": function}],
        }
    return {
        "id": "chatcmpl-contract",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "compat-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop" if call is None else "tool_calls",
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


class Backend:
    """Сервер вместо сети: ответ на n-й запрос выбирает `script(n)`, тела запросов запоминаются."""

    def __init__(self, script: Callable[[int], dict[str, Any]]) -> None:
        self._script = script
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return httpx2.Response(200, json=self._script(len(self.requests) - 1))


def dialogue(wire: Callable[..., dict[str, Any]], calls: list[Call]) -> Callable[[int], Any]:
    def script(number: int) -> dict[str, Any]:
        return wire(calls[number]) if number < len(calls) else wire(text=FINAL_TEXT)

    return script


def anthropic_provider(backend: Backend) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-contract",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(backend.handle)
        ),
    )
    return AnthropicProvider(client, "claude-test", "medium")


def openai_provider(backend: Backend) -> OpenAICompatibleProvider:
    client = openai.AsyncOpenAI(
        base_url="http://compat.invalid/v1",
        api_key="sk-compat-contract",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(backend.handle)),
    )
    return OpenAICompatibleProvider(client, "compat-model")


def provider_for(adapter: str, calls: list[Call]) -> LLMProvider:
    """Провайдер, чья «модель» просит `calls` по одному вызову за ответ и затем рассказывает."""
    if adapter == "anthropic_api":
        return anthropic_provider(Backend(dialogue(anthropic_wire, calls)))
    if adapter == "openai_compatible":
        return openai_provider(Backend(dialogue(openai_wire, calls)))
    steps = [{"tool": name, "args": args} for name, args in calls] + [{"text": FINAL_TEXT}]
    return FakeProvider(Scenario.model_validate({"turns": [{"steps": steps}]}))


def default_request() -> TurnRequest:
    """Запрос с лимитами по умолчанию: границы проверяются на них, а не на своих числах."""
    return TurnRequest(system="система", adventure_brief="сводка", context="контекст", tools=())


# --- один разговор -> один результат ---


async def test_the_same_conversation_gives_the_same_result_with_every_adapter():
    results = {}
    for adapter in ADAPTERS:
        tools = ScriptedTools()
        results[adapter] = (
            await provider_for(adapter, CONVERSATION).run_turn(default_request(), tools),
            tools,
        )

    expected_trace = (
        ToolCallTrace("request_check", {"dc": 15}, '{"total": 17}', False),
        ToolCallTrace("chronicle", {"entry": "запись"}, "Ошибка: записи нет", True),
    )
    for adapter, (result, tools) in results.items():
        assert result.text == FINAL_TEXT, adapter
        assert result.trace == expected_trace, adapter
        assert result.stop_reason == "end_turn", adapter
        assert tools.calls == CONVERSATION, adapter
    # Расход токенов: Fake ничего не тратит, а оба API суммируют его по трём ответам хода.
    assert results["fake"][0].usage == Usage()
    assert results["anthropic_api"][0].usage == Usage(input_tokens=300, output_tokens=60)
    assert results["openai_compatible"][0].usage == results["anthropic_api"][0].usage


# --- общие лимиты хода на настоящих значениях по умолчанию ---


def test_default_limits_are_the_agreed_ones():
    request = default_request()

    assert request.max_tool_iterations == 20
    # Тайм-аут запроса клиента SDK не больше тайм-аута всего хода.
    assert 0 < factory.REQUEST_TIMEOUT_S <= request.timeout_s


@pytest.mark.parametrize("module", [anthropic_api, openai_compatible, claude_code_cli])
def test_complete_json_retries_at_most_twice_in_every_adapter(module):
    assert module.MAX_JSON_RETRIES == 2


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_turn_with_twenty_tool_iterations_passes_at_default_limit(adapter):
    calls: list[Call] = [("chronicle", {"entry": str(number)}) for number in range(20)]
    tools = ScriptedTools()

    result = await provider_for(adapter, calls).run_turn(default_request(), tools)

    assert len(result.trace) == 20
    assert len(tools.calls) == 20
    assert result.text == FINAL_TEXT


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_the_twenty_first_tool_iteration_is_refused_before_it_runs(adapter):
    calls: list[Call] = [("chronicle", {"entry": str(number)}) for number in range(21)]
    tools = ScriptedTools()

    with pytest.raises(ToolLoopExceeded):
        await provider_for(adapter, calls).run_turn(default_request(), tools)

    # Состояние игры не должно меняться инструментом сверх лимита: двадцать первый не исполнен.
    assert len(tools.calls) == 20


# --- схемы инструментов движка на проводе ---


def engine_tool_schemas() -> tuple[ToolSchema, ...]:
    registry = build_registry(build_ruleset(RulesRef(modules=["bg3_flavor", "campfire"])))
    return tuple(ToolSchema(s.name, s.description, s.json_schema()) for s in registry.specs())


def test_engine_tools_have_names_and_descriptions_that_providers_accept():
    tools = engine_tool_schemas()

    assert len(tools) >= 27
    for tool in tools:
        # Допустимые имена функций у Anthropic и OpenAI: буквы, цифры, «_», «-», до 64 знаков.
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name), tool.name
        assert tool.description.strip(), tool.name
        assert tool.input_schema["type"] == "object", tool.name
        json.dumps(tool.input_schema)  # на провод уходит JSON


async def test_engine_tool_schemas_reach_the_anthropic_wire_unchanged():
    tools = engine_tool_schemas()
    backend = Backend(dialogue(anthropic_wire, []))
    request = TurnRequest(
        system="система", adventure_brief="сводка", context="контекст", tools=tools
    )

    await anthropic_provider(backend).run_turn(request, ScriptedTools())

    sent = backend.requests[0]["tools"]
    assert [tool["name"] for tool in sent] == [tool.name for tool in tools]
    assert [tool["description"] for tool in sent] == [tool.description for tool in tools]
    assert [tool["input_schema"] for tool in sent] == [tool.input_schema for tool in tools]


async def test_engine_tool_schemas_reach_the_openai_wire_unchanged():
    tools = engine_tool_schemas()
    backend = Backend(dialogue(openai_wire, []))
    request = TurnRequest(
        system="система", adventure_brief="сводка", context="контекст", tools=tools
    )

    await openai_provider(backend).run_turn(request, ScriptedTools())

    sent = [tool["function"] for tool in backend.requests[0]["tools"]]
    assert [tool["name"] for tool in sent] == [tool.name for tool in tools]
    assert [tool["description"] for tool in sent] == [tool.description for tool in tools]
    assert [tool["parameters"] for tool in sent] == [tool.input_schema for tool in tools]


# --- схемы шагов генератора через адаптеры ---

STEP_SCHEMAS: dict[str, type[BaseModel]] = {
    "generator.concept": schemas.ConceptOut,
    "generator.world": schemas.WorldOut,
    "generator.pantheon": schemas.PantheonOut,
    "generator.factions": schemas.FactionsOut,
    "generator.plot": schemas.PlotOut,
    "generator.locations": schemas.LocationGraphOut,
    "generator.location_contents": schemas.LocationContentsOut,
    "generator.npcs": schemas.NpcsOut,
    "generator.relations": schemas.RelationsOut,
    "generator.encounters": schemas.EncountersOut,
    "generator.items": schemas.ItemsOut,
    "generator.clues": schemas.CluesOut,
}


def demo_json_answers() -> list[tuple[str, dict[str, Any]]]:
    answers = load_scenario(SCENARIOS_DIR / "generator_demo.yaml").json_answers
    return [
        (purpose, answer)
        for purpose, given in answers.items()
        for answer in (given if isinstance(given, list) else [given])
    ]


@pytest.mark.parametrize(("purpose", "answer"), demo_json_answers())
async def test_generator_schemas_survive_the_anthropic_wire(purpose, answer):
    schema = STEP_SCHEMAS[purpose]
    backend = Backend(lambda number: anthropic_wire(text=json.dumps(answer, ensure_ascii=False)))
    request = JsonRequest(system="система", prompt="запрос", purpose=purpose, cheap=False)

    parsed = await anthropic_provider(backend).complete_json(request, schema)

    assert parsed == schema.model_validate(answer)
    sent_schema = backend.requests[0]["output_config"]["format"]["schema"]
    assert sent_schema["type"] == "object"
    assert sent_schema["additionalProperties"] is False


@pytest.mark.parametrize(("purpose", "answer"), demo_json_answers())
async def test_generator_schemas_survive_the_openai_wire(purpose, answer):
    schema = STEP_SCHEMAS[purpose]
    backend = Backend(lambda number: openai_wire(text=json.dumps(answer, ensure_ascii=False)))
    request = JsonRequest(system="система", prompt="запрос", purpose=purpose, cheap=False)

    parsed = await openai_provider(backend).complete_json(request, schema)

    assert parsed == schema.model_validate(answer)
    sent = backend.requests[0]["response_format"]["json_schema"]
    assert sent["schema"] == schema.model_json_schema()
