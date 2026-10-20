"""Исполнитель инструментов хода мастера (план B7.1, спека §8).

Настоящий реестр движка и демо-приключение `demo-crypt`, кубы — `SeededRng`; живых моделей нет.
Защита от повторов проверяется на маленьком реестре со счётчиком вызовов обработчика: так видно,
что отклонённый повтор до инструмента не дошёл.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import SeededRng
from dnd_engine.state import GameState, RulesRef
from dnd_engine.tools import build_registry
from dnd_engine.tools.registry import ToolArgs, ToolContext, ToolRegistry, ToolSpec
from dnd_server.dm.executor import MAX_IDENTICAL_CALLS, DmToolExecutor
from dnd_server.llm.base import (
    MAX_TRACE_NAME_CHARS,
    MAX_TRACE_RESULT_CHARS,
    ToolCallTrace,
    ToolSchema,
    TurnRequest,
    make_trace_call,
)
from dnd_server.llm.fake import FakeProvider, load_scenario

DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
RULES = RulesRef(modules=["bg3_flavor", "campfire"])
HERO = "pc-1"


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return build_registry(build_ruleset(RULES))


def make_ctx(srd: SrdContent, adventure: Adventure) -> ToolContext:
    hero = quick_character(
        "fighter", "human", srd, name="Брон", concept="наёмник", character_id=HERO
    )
    state = GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RULES,
        party={HERO: hero},
        location_id="village",
    )
    return ToolContext(
        state=state, adventure=adventure, content=srd, rules=build_ruleset(RULES), rng=SeededRng(5)
    )


@pytest.fixture
def ctx(srd: SrdContent, adventure: Adventure) -> ToolContext:
    return make_ctx(srd, adventure)


@pytest.fixture
def executor(ctx: ToolContext, registry: ToolRegistry) -> DmToolExecutor:
    return DmToolExecutor(ctx, registry)


# --- маленький реестр со счётчиком вызовов обработчика ---


class PingArgs(ToolArgs):
    n: int = 0
    tag: str = ""


class CountingTool:
    """Инструмент `ping`: считает, сколько раз до него дошёл вызов."""

    def __init__(self) -> None:
        self.calls = 0

    def handler(self, ctx: ToolContext, args: PingArgs) -> dict[str, Any]:
        self.calls += 1
        return {"n": args.n}


def counting_registry(*names: str) -> tuple[ToolRegistry, dict[str, CountingTool]]:
    registry = ToolRegistry()
    tools = {name: CountingTool() for name in names}
    for name, tool in tools.items():
        registry.register(ToolSpec(name, f"Инструмент {name}.", PingArgs, tool.handler))
    return registry, tools


@pytest.fixture
def counting(ctx: ToolContext) -> tuple[DmToolExecutor, dict[str, CountingTool]]:
    registry, tools = counting_registry("ping", "pong")
    return DmToolExecutor(ctx, registry), tools


# --- успех ---


async def test_a_successful_call_returns_the_result_as_json_without_error(executor):
    text, is_error = await executor("private_message", {"character_id": HERO, "text": "Тише."})

    assert is_error is False
    assert json.loads(text) == {"to": "Брон"}


async def test_the_result_text_keeps_russian_letters_readable(executor):
    text, _ = await executor("private_message", {"character_id": HERO, "text": "Тише."})

    assert "Брон" in text
    assert "\\u" not in text


async def test_a_successful_call_changes_the_working_copy_through_the_engine(executor, ctx):
    await executor("chronicle", {"entry": "Отряд пришёл в деревню."})

    assert ctx.state.chronicle.entries == ["Отряд пришёл в деревню."]


async def test_the_result_is_the_engine_result_of_the_same_dice(executor, registry, srd, adventure):
    # Броски делает только сервер на `rng` контекста: на том же seed движок даёт тот же итог.
    args = {
        "character_id": HERO,
        "ability": "dex",
        "skill": "stealth",
        "dc": 15,
        "reason": "крадётся",
    }
    expected = registry.call(make_ctx(srd, adventure), "request_check", args)

    text, is_error = await executor("request_check", args)

    assert is_error is False
    assert expected.result
    assert json.loads(text) == expected.result


# --- ошибки модели: текст по которому она исправляется ---


async def test_invalid_arguments_come_back_as_an_error_and_change_nothing(executor, ctx):
    text, is_error = await executor("chronicle", {})

    assert is_error is True
    error = json.loads(text)["error"]
    assert "chronicle" in error
    assert "entry" in error
    assert ctx.state.chronicle.entries == []
    assert ctx.events == []


async def test_a_tool_error_of_the_engine_comes_back_as_an_error_with_its_reason(executor):
    text, is_error = await executor("private_message", {"character_id": "ghost", "text": "Эй."})

    assert is_error is True
    assert "ghost" in json.loads(text)["error"]


async def test_an_error_text_is_json_with_the_single_error_key(executor):
    text, _ = await executor("chronicle", {})

    assert list(json.loads(text)) == ["error"]


async def test_an_engine_bug_is_not_dressed_up_as_a_model_error(ctx):
    # Баг движка не должен выглядеть ошибкой модели: ход мастера откатывается целиком.
    def broken(ctx: ToolContext, args: PingArgs) -> dict[str, Any]:
        raise RuntimeError("баг движка")

    registry = ToolRegistry()
    registry.register(ToolSpec("broken", "Сломанный инструмент.", PingArgs, broken))

    with pytest.raises(RuntimeError, match="баг движка"):
        await DmToolExecutor(ctx, registry)("broken", {})


# --- имя вне списка хода ---


async def test_an_unknown_tool_is_refused_with_a_message_that_lists_the_available_ones(
    executor, registry
):
    text, is_error = await executor("teleport", {"to": "moon"})

    assert is_error is True
    error = json.loads(text)["error"]
    assert "teleport" in error
    assert all(spec.name in error for spec in registry.specs())


async def test_a_name_outside_the_tools_of_the_turn_never_reaches_the_registry(ctx):
    # Список хода зафиксирован при создании исполнителя: поздно добавленный в реестр инструмент
    # модель не получала в `schemas()`, значит, и вызвать его не может.
    registry, tools = counting_registry("ping")
    executor = DmToolExecutor(ctx, registry)
    late = CountingTool()
    registry.register(ToolSpec("late", "Добавлен после начала хода.", PingArgs, late.handler))

    text, is_error = await executor("late", {})

    assert is_error is True
    assert "late" in json.loads(text)["error"]
    assert late.calls == 0


async def test_a_huge_unknown_name_is_clipped_in_the_message_and_the_trace(executor):
    name = "x" * 10_000

    text, is_error = await executor(name, {})

    assert is_error is True
    assert len(text) < 2000
    assert len(executor.trace[0].name) <= MAX_TRACE_NAME_CHARS


# --- защита от повторов ---


async def test_three_identical_calls_in_a_row_are_executed(counting):
    executor, tools = counting

    answers = [await executor("ping", {"n": 1}) for _ in range(MAX_IDENTICAL_CALLS)]

    assert MAX_IDENTICAL_CALLS == 3
    assert [is_error for _, is_error in answers] == [False, False, False]
    assert tools["ping"].calls == 3


async def test_the_fourth_identical_call_is_refused_without_reaching_the_tool(counting):
    executor, tools = counting
    for _ in range(MAX_IDENTICAL_CALLS):
        await executor("ping", {"n": 1})

    text, is_error = await executor("ping", {"n": 1})

    assert is_error is True
    assert "повтор без изменений" in json.loads(text)["error"]
    assert "ping" in json.loads(text)["error"]
    assert tools["ping"].calls == 3


async def test_every_further_identical_call_is_refused_too(counting):
    executor, tools = counting
    answers = [await executor("ping", {"n": 1}) for _ in range(6)]

    assert [is_error for _, is_error in answers] == [False] * 3 + [True] * 3
    assert tools["ping"].calls == 3


async def test_another_call_in_between_resets_the_counter(counting):
    executor, tools = counting
    for _ in range(MAX_IDENTICAL_CALLS):
        await executor("ping", {"n": 1})
    await executor("pong", {"n": 1})  # другой инструмент сбрасывает счёт

    answers = [await executor("ping", {"n": 1}) for _ in range(MAX_IDENTICAL_CALLS)]

    assert [is_error for _, is_error in answers] == [False, False, False]
    assert tools["ping"].calls == 6


async def test_other_arguments_make_another_call_and_reset_the_counter(counting):
    executor, tools = counting
    for _ in range(MAX_IDENTICAL_CALLS):
        await executor("ping", {"n": 1})

    _, is_error = await executor("ping", {"n": 2})

    assert is_error is False
    assert tools["ping"].calls == 4


async def test_the_counter_resets_after_refused_repeats_too(counting):
    executor, _ = counting
    for _ in range(MAX_IDENTICAL_CALLS + 2):  # два последних отклонены
        await executor("ping", {"n": 1})
    await executor("ping", {"n": 2})

    answers = [await executor("ping", {"n": 1}) for _ in range(MAX_IDENTICAL_CALLS)]

    assert [is_error for _, is_error in answers] == [False, False, False]


async def test_the_order_of_argument_keys_does_not_make_a_call_different(counting):
    executor, tools = counting
    for _ in range(MAX_IDENTICAL_CALLS):
        await executor("ping", {"n": 1, "tag": "a"})

    _, is_error = await executor("ping", {"tag": "a", "n": 1})

    assert is_error is True
    assert tools["ping"].calls == 3


async def test_a_looping_invalid_call_is_cut_too(executor):
    answers = [await executor("chronicle", {}) for _ in range(MAX_IDENTICAL_CALLS + 1)]

    assert "повтор без изменений" in json.loads(answers[-1][0])["error"]
    assert "entry" in json.loads(answers[0][0])["error"]  # первые три — настоящая ошибка схемы


async def test_a_looping_unknown_tool_is_cut_too(executor):
    answers = [await executor("teleport", {}) for _ in range(MAX_IDENTICAL_CALLS + 1)]

    assert "повтор без изменений" in json.loads(answers[-1][0])["error"]
    assert "повтор без изменений" not in json.loads(answers[0][0])["error"]


async def test_a_streak_is_logged_once_without_the_arguments(counting, caplog):
    executor, _ = counting
    caplog.set_level(logging.WARNING, logger="dnd_server.dm.executor")

    for _ in range(MAX_IDENTICAL_CALLS + 3):
        await executor("ping", {"tag": "СЕКРЕТНЫЙ-АРГУМЕНТ"})

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "ping" in messages[0]
    assert "game-1" in messages[0]
    assert "СЕКРЕТНЫЙ-АРГУМЕНТ" not in messages[0]


# --- трасса ---


async def test_the_trace_lists_every_call_in_order_including_refused_ones(counting):
    executor, _ = counting

    await executor("ping", {"n": 1})
    await executor("nope", {})
    for _ in range(MAX_IDENTICAL_CALLS):
        await executor("pong", {"n": 2})

    assert [(call.name, call.is_error) for call in executor.trace] == [
        ("ping", False),
        ("nope", True),
        ("pong", False),
        ("pong", False),
        ("pong", False),
    ]


async def test_the_trace_entry_is_what_the_model_got_built_by_make_trace_call(counting):
    executor, _ = counting
    answers = [await executor("ping", {"n": 7})]
    answers.append(await executor("nope", {"x": 1}))

    expected = [
        make_trace_call("ping", {"n": 7}, answers[0][0], answers[0][1]),
        make_trace_call("nope", {"x": 1}, answers[1][0], answers[1][1]),
    ]
    assert executor.trace == expected
    assert all(isinstance(call, ToolCallTrace) for call in executor.trace)


async def test_the_trace_is_bounded_but_the_model_still_gets_the_full_result(ctx):
    big = "очень длинный результат " * 1000

    registry = ToolRegistry()
    registry.register(ToolSpec("big", "Большой.", PingArgs, lambda ctx, args: {"text": big}))
    executor = DmToolExecutor(ctx, registry)

    text, _ = await executor("big", {})

    assert big in text
    assert len(executor.trace[0].result) <= MAX_TRACE_RESULT_CHARS


async def test_the_trace_of_a_call_with_huge_arguments_is_bounded(executor):
    await executor("chronicle", {"entry": "я" * 10_000})

    assert executor.trace[0].is_error is True
    assert "_truncated" in executor.trace[0].args


async def test_a_fresh_executor_starts_with_an_empty_trace_and_a_clean_counter(ctx):
    registry, tools = counting_registry("ping")
    first = DmToolExecutor(ctx, registry)
    for _ in range(MAX_IDENTICAL_CALLS + 1):
        await first("ping", {"n": 1})

    second = DmToolExecutor(ctx, registry)
    _, is_error = await second("ping", {"n": 1})

    assert len(first.trace) == MAX_IDENTICAL_CALLS + 1
    assert len(second.trace) == 1
    assert is_error is False


# --- схемы для провайдера ---


def test_schemas_are_the_registry_specs_in_registration_order(executor, registry):
    schemas = executor.schemas()

    assert isinstance(schemas, tuple)
    assert all(isinstance(schema, ToolSchema) for schema in schemas)
    assert [(s.name, s.description, s.input_schema) for s in schemas] == [
        (spec.name, spec.description, spec.json_schema()) for spec in registry.specs()
    ]


def test_schemas_are_serializable_for_a_provider(executor):
    json.dumps([schema.input_schema for schema in executor.schemas()])


# --- стык с провайдером ---


async def test_the_executor_serves_a_provider_turn_end_to_end(executor, ctx):
    provider = FakeProvider(load_scenario(SCENARIOS_DIR / "basic_round.yaml"), pc_ids=(HERO,))
    request = TurnRequest(
        system="система",
        adventure_brief="сводка",
        context="контекст",
        tools=executor.schemas(),
    )

    turn = await provider.run_turn(request, executor)

    assert turn.text == "Тени скрывают вас…"
    assert [call.name for call in turn.trace] == ["request_check", "chronicle"]
    assert [call.is_error for call in turn.trace] == [False, False]
    # Трасса исполнителя и трасса адаптера строятся одной функцией и совпадают.
    assert list(turn.trace) == executor.trace
    assert ctx.state.chronicle.entries == ["Отряд прокрался к склепу."]
