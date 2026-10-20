"""Сценарии `tests/scenarios/*.yaml` и `FakeProvider`: контракт со схемами движка и генератора.

Каждый сценарий папки, а не только известные тесты, должен читаться загрузчиком, давать ходы с
верными аргументами инструментов движка и ответы шагов генератора, которые проходят их схемы.
Опечатка в `purpose` или в имени поля иначе всплыла бы не в сценарии, а в чужом тесте.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from dnd_engine.modules import build_ruleset
from dnd_engine.state import RulesRef
from dnd_engine.tools import build_registry
from dnd_server.authoring import schemas
from dnd_server.authoring.pipeline import STEPS, GenerationRequest, generate
from dnd_server.authoring.schemas import Seeds
from dnd_server.dm.weaving import WeavingOut
from dnd_server.llm.base import JsonRequest, ProviderError, TurnRequest
from dnd_server.llm.fake import FakeProvider, Scenario, ToolStep, load_scenario

# Конфтест сервера переходит во временную папку, поэтому путь считаем от файла теста.
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
SCENARIO_FILES = sorted(SCENARIOS_DIR.glob("*.yaml"))
PC_IDS = tuple(f"pc-{number}" for number in range(1, 7))
MAX_PC_NUMBER = len(PC_IDS)
# Сценарии, с которыми сервер запускают вручную (`FAKE_LLM_SCENARIO`): после «Начать» игра просит
# у провайдера вплетение героев, и без JSON-ответа `weaving` она встанет в `dm_failed`. Для каждого
# указано, при каком числе игроков он стартует: `combat.yaml` и `weaving.yaml` названы для трёх
# героев (`$pc1…$pc3`), у остальных зацепка есть у первого героя, поэтому годится любой состав.
# Без ответа вплетения остаются `loop.yaml` (предел итераций ставят только тесты) и
# `characters.yaml` (создание героев): игра на них после «Начать» останавливается.
MANUAL_RUN_PLAYERS: dict[str, tuple[int, ...]] = {
    "basic_round.yaml": tuple(range(1, MAX_PC_NUMBER + 1)),
    "failing.yaml": tuple(range(1, MAX_PC_NUMBER + 1)),
    "combat.yaml": (3,),
    "weaving.yaml": (3,),
}

SCHEMA_BY_STEP: dict[str, type[BaseModel]] = {
    "concept": schemas.ConceptOut,
    "world": schemas.WorldOut,
    "locations": schemas.LocationGraphOut,
    "location_detail": schemas.LocationDetailOut,
    "npcs": schemas.NpcsOut,
    "encounters": schemas.EncountersOut,
    "items": schemas.ItemsOut,
    "clues": schemas.CluesOut,
}
GENERATOR_REQUEST = GenerationRequest(
    pitch="Колокол в часовне звонит сам по ночам.",
    hours=1,
    players=6,
    level=3,
    modules=("bg3_flavor", "campfire"),
)
EMPTY_TURN = TurnRequest(system="", adventure_brief="", context="", tools=())


class EngineSchemaCheckingTools:
    """Исполнитель вместо оркестратора: принимает вызов, только если его принял бы реестр движка."""

    def __init__(self) -> None:
        registry = build_registry(build_ruleset(RulesRef(modules=["bg3_flavor", "campfire"])))
        self._specs = {spec.name: spec for spec in registry.specs()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        assert name in self._specs, f"в реестре движка нет инструмента {name!r}"
        self._specs[name].args_model.model_validate(args)  # неверные аргументы роняют тест
        self.calls.append((name, args))
        return "{}", False


class CountingProvider:
    """Обёртка над `FakeProvider`: считает запросы JSON по `purpose`."""

    name = "counting"

    def __init__(self, scenario: Scenario) -> None:
        self._fake = FakeProvider(scenario)
        self.requests: Counter[str] = Counter()

    async def complete_json(self, req: JsonRequest, schema: type[BaseModel]) -> BaseModel:
        self.requests[req.purpose] += 1
        return await self._fake.complete_json(req, schema)


def answers_of(scenario: Scenario, purpose: str) -> list[dict[str, Any]]:
    answers = scenario.json_answers[purpose]
    return answers if isinstance(answers, list) else [answers]


def generator_purposes(scenario: Scenario) -> list[str]:
    return [purpose for purpose in scenario.json_answers if purpose.startswith("generator.")]


def test_the_scenarios_of_the_block_are_in_the_folder():
    assert {"basic_round.yaml", "generator_demo.yaml"} <= {path.name for path in SCENARIO_FILES}


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_scenario_is_readable_by_the_loader(path):
    assert isinstance(load_scenario(path), Scenario)


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_scenario_names_only_characters_a_game_can_have(path):
    # В игре от одного до шести персонажей: `$pc7` или `$pc0` сделали бы ход ошибкой провайдера.
    numbers = [int(number) for number in re.findall(r"\$pc(\d+)", path.read_text("utf-8"))]

    assert all(1 <= number <= MAX_PC_NUMBER for number in numbers)


@pytest.mark.parametrize(
    ("name", "players"),
    [
        pytest.param(name, players, id=f"{name}-{players}")
        for name, counts in MANUAL_RUN_PLAYERS.items()
        for players in counts
    ],
)
async def test_a_scenario_for_manual_runs_answers_the_weaving_request(name, players):
    scenario = load_scenario(SCENARIOS_DIR / name)
    pc_ids = PC_IDS[:players]
    provider = FakeProvider(scenario, pc_ids=pc_ids)

    out = await provider.complete_json(
        JsonRequest(system="", prompt="", purpose="weaving"), WeavingOut
    )

    assert {hook.character_id for hook in out.hooks} <= set(pc_ids)
    # Связь с миром есть хотя бы у одного героя: иначе сервер повторял бы запрос впустую.
    assert any(hook.npc_links or hook.location_links for hook in out.hooks)


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
async def test_every_turn_calls_tools_the_engine_accepts_and_then_runs_out(path):
    scenario = load_scenario(path)
    provider = FakeProvider(scenario, pc_ids=PC_IDS)
    tools = EngineSchemaCheckingTools()

    for _ in scenario.turns:
        result = await provider.run_turn(EMPTY_TURN, tools)
        assert result.stop_reason == "end_turn"

    planned = sum(1 for turn in scenario.turns for step in turn.steps if isinstance(step, ToolStep))
    assert len(tools.calls) == planned
    if scenario.turns and scenario.turns[-1].repeat:
        await provider.run_turn(EMPTY_TURN, tools)  # ход-шаблон не кончается
    else:
        with pytest.raises(ProviderError, match="сценарий исчерпан"):
            await provider.run_turn(EMPTY_TURN, tools)


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_generator_answers_name_real_steps_and_fit_their_schemas(path):
    scenario = load_scenario(path)

    for purpose in generator_purposes(scenario):
        step = purpose.removeprefix("generator.")
        assert step in STEPS, f"{purpose}: такого шага генератора нет"
        for answer in answers_of(scenario, purpose):
            SCHEMA_BY_STEP[step].model_validate(answer)


def test_the_steps_known_to_the_tests_are_the_steps_of_the_generator():
    assert set(SCHEMA_BY_STEP) == set(STEPS)


def test_generator_demo_answers_every_step():
    scenario = load_scenario(SCENARIOS_DIR / "generator_demo.yaml")

    assert set(generator_purposes(scenario)) == {f"generator.{step}" for step in STEPS}


async def test_generator_demo_leaves_no_answer_unused():
    # Лишний ответ в сценарии прячет расхождение с генератором: он никогда не будет сыгран.
    scenario = load_scenario(SCENARIOS_DIR / "generator_demo.yaml")
    provider = CountingProvider(scenario)

    await generate(provider, GENERATOR_REQUEST, Seeds())

    for purpose in generator_purposes(scenario):
        expected = len(answers_of(scenario, purpose))
        if isinstance(scenario.json_answers[purpose], list):
            assert provider.requests[purpose] == expected, purpose
        else:
            assert provider.requests[purpose] >= 1, purpose


async def test_two_providers_from_one_scenario_have_independent_json_queues():
    # Очередь списка ответов хранится в провайдере: каждая игра и каждый запуск начинают сначала.
    scenario = Scenario.model_validate({"json": {"step": [{"n": 1}, {"n": 2}]}})
    request = JsonRequest(system="", prompt="", purpose="step")

    class Number(BaseModel):
        n: int

    first, second = FakeProvider(scenario), FakeProvider(scenario)

    given = [
        (await first.complete_json(request, Number)).n,
        (await first.complete_json(request, Number)).n,
        (await second.complete_json(request, Number)).n,
    ]

    assert given == [1, 2, 1]
    with pytest.raises(ProviderError, match="сценарий исчерпан"):
        await first.complete_json(request, Number)
