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
from dnd_server.authoring.draft import AuthoringDraft, DraftSettings
from dnd_server.authoring.pipeline import generate
from dnd_server.authoring.steps import STEPS
from dnd_server.characters.flavor import BackstoryOut, PersonaOut
from dnd_server.dm.rest_ideas import IdeasOut
from dnd_server.dm.weaving import WeavingOut
from dnd_server.llm.base import JsonRequest, ProviderError, TurnRequest
from dnd_server.llm.fake import (
    FakeProvider,
    Scenario,
    ScenarioTurn,
    TextStep,
    ToolStep,
    load_scenario,
)

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
    # Сквозной тест Playwright (B8.7): шестеро игроков, зацепка у каждого.
    "e2e_full.yaml": (6,),
    # Сквозной тест Playwright «Привалы» (B9.4): трое игроков, зацепка у каждого.
    "e2e_rests.yaml": (3,),
}

SCHEMA_BY_STEP: dict[str, type[BaseModel]] = {
    "concept": schemas.ConceptOut,
    "world": schemas.WorldOut,
    "pantheon": schemas.PantheonOut,
    "factions": schemas.FactionsOut,
    "plot": schemas.PlotOut,
    "locations": schemas.LocationGraphOut,
    "location_contents": schemas.LocationContentsOut,
    "npcs": schemas.NpcsOut,
    "relations": schemas.RelationsOut,
    "encounters": schemas.EncountersOut,
    "items": schemas.ItemsOut,
    "clues": schemas.CluesOut,
}
# Демо-сценарии генератора и длина игры в их заказе (часы): оба проходят генератор целиком.
GENERATOR_DEMOS = {"generator_demo.yaml": 1, "generator_v2_demo.yaml": 2}


def generator_draft(hours: float) -> AuthoringDraft:
    """Заказ демо: 6 игроков, 3 уровень, модули bg3_flavor и campfire."""
    settings = DraftSettings(
        pitch="Колокол в часовне звонит сам по ночам.",
        target_hours=hours,
        party_size=6,
        party_level=3,
        modules=("bg3_flavor", "campfire"),
    )
    return AuthoringDraft(settings=settings)


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
    assert {"basic_round.yaml", *GENERATOR_DEMOS, "e2e_full.yaml"} <= {
        path.name for path in SCENARIO_FILES
    }


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


@pytest.mark.parametrize("name", ["combat.yaml", "e2e_full.yaml"])
def test_combat_scenario_engages_the_skeleton_before_each_of_its_strikes(name):
    # Рукопашная атака бьёт только вовлечённого противника, а после start_combat не вовлечён никто
    # (B7.manual): без своего вовлечения скелет в ручном прогоне всегда получал отказ движка.
    scenario = load_scenario(SCENARIOS_DIR / name)
    strikes = 0

    for turn in scenario.turns:
        steps = [step for step in turn.steps if isinstance(step, ToolStep)]
        for position, step in enumerate(steps):
            if step.tool != "monster_action":
                continue
            strikes += 1
            (target,) = step.args["target_ids"]
            engaged = [
                earlier
                for earlier in steps[:position]
                if earlier.tool == "move"
                and earlier.args.get("actor_id") == step.args["monster_id"]
                and earlier.args.get("engage") == target
            ]
            assert engaged, f"{step.args['monster_id']} бьёт {target} без вовлечения"
    assert strikes  # в сценарии есть атака скелета, иначе проверка ничего не проверяла


# --- сквозной сценарий Playwright «6 игроков» (B8.7) ---
#
# `e2e_full.yaml` играет настоящий сервер с `FakeProvider` под браузерами шестерых игроков, а кубы и
# инициатива настоящие (`SystemRng`). Тест Playwright (`services/webapp/e2e/six-players.spec.ts`)
# рассчитывает на порядок ходов мастера из этого файла: ниже он закреплён, чтобы правка сценария не
# разошлась с тестом молча.

E2E_SCENARIO = SCENARIOS_DIR / "e2e_full.yaml"
ALL_HEROES = [f"$pc{number}" for number in range(1, MAX_PC_NUMBER + 1)]
# Предел итераций инструментов за ход мастера (`dm.turn`); сценарий держится заметно ниже.
TOOL_STEPS_LIMIT = 20


def e2e_turns() -> list[ScenarioTurn]:
    return load_scenario(E2E_SCENARIO).turns


def tool_steps(turn: ScenarioTurn) -> list[ToolStep]:
    return [step for step in turn.steps if isinstance(step, ToolStep)]


def first_turn_with(turns: list[ScenarioTurn], tool: str) -> int:
    return next(
        index for index, turn in enumerate(turns) if any(s.tool == tool for s in tool_steps(turn))
    )


async def e2e_weaving() -> list:
    provider = FakeProvider(load_scenario(E2E_SCENARIO), pc_ids=PC_IDS)
    out = await provider.complete_json(
        JsonRequest(system="", prompt="", purpose="weaving"), WeavingOut
    )
    return out.hooks


async def test_e2e_scenario_weaves_a_hook_for_each_of_the_six_heroes():
    hooks = await e2e_weaving()

    assert [hook.character_id for hook in hooks] == list(PC_IDS)
    # Связь с миром приключения есть у каждого: сервер не просит вплетение повторно.
    assert all(hook.npc_links or hook.location_links for hook in hooks)


async def test_e2e_hooks_have_goals_that_do_not_contain_one_another():
    # Тест Playwright ищет цель героя у него на экране и не должен находить её у остальных пятерых.
    goals = [hook.personal_goal for hook in await e2e_weaving()]

    assert all(goals)
    for goal in goals:
        assert [other for other in goals if goal in other] == [goal]


@pytest.mark.parametrize("name", ["e2e_full.yaml", "e2e_rests.yaml"])
async def test_e2e_scenario_describes_quick_heroes_and_their_backstories(name):
    # Оба сквозных теста создают героев кнопкой «Быстрый герой».
    scenario = load_scenario(SCENARIOS_DIR / name)
    provider = FakeProvider(scenario)

    persona = await provider.complete_json(
        JsonRequest(system="", prompt="", purpose="character.flavor"), PersonaOut
    )
    backstory = await provider.complete_json(
        JsonRequest(system="", prompt="", purpose="character.backstory"), BackstoryOut
    )

    assert persona.name
    assert backstory.backstory


def test_e2e_scenario_ends_every_turn_with_a_story_and_stays_under_the_tool_limit():
    for number, turn in enumerate(e2e_turns()):
        last = turn.steps[-1]
        assert isinstance(last, TextStep), f"ход {number} без рассказа в конце"
        assert last.text.strip(), f"ход {number}: пустой рассказ"
        assert len(tool_steps(turn)) < TOOL_STEPS_LIMIT, f"ход {number}: слишком много вызовов"


def test_e2e_scenario_opens_the_game_with_a_story_only():
    opening = e2e_turns()[0]

    assert {step.tool for step in tool_steps(opening)} <= {"chronicle"}


def test_e2e_first_round_rolls_for_the_second_hero_in_public_and_the_third_in_secret():
    # Игрок 3 сдаёт тайный ход, игрок 2 — обычный: бросок героя 3 виден только ему, героя 2 — всем.
    turn = e2e_turns()[1]
    checks = [
        step.args["character_id"] for step in tool_steps(turn) if step.tool == "request_check"
    ]
    damaged = [step.args["character_id"] for step in tool_steps(turn) if step.tool == "damage"]

    assert checks == ["$pc2", "$pc3"]
    # Урон остаётся на герое до привала: так видно, что длинный отдых вернул здоровье.
    assert damaged == ["$pc2"]


def test_e2e_scenario_fights_in_two_master_calls_whoever_acts_first():
    # Инициатива настоящая, поэтому тест не знает, кто ходит первым: герой или скелет. Бой всегда
    # занимает ровно два вызова мастера после начала сцены, и сцену кончает второй. Первый
    # зовёт «Уклоняется» за каждого героя (сработает у того, чей ход сейчас), а скелет вступает
    # в бой и бьёт, если ходит в этом вызове.
    turns = e2e_turns()
    start = first_turn_with(turns, "start_combat")
    end = first_turn_with(turns, "end_scene")
    camp = first_turn_with(turns, "start_camp")

    assert end == start + 2
    assert start < end < camp
    first_call, ending_call = turns[start + 1], turns[end]
    assert all(step.tool != "end_scene" for step in tool_steps(first_call))
    dodging = [
        step.args["actor_id"]
        for step in tool_steps(first_call)
        if step.tool == "combat_action" and step.args["action"] == "dodge"
    ]
    assert sorted(dodging) == sorted(ALL_HEROES)
    # Сцену закрывает только второй вызов: до него бой виден на экране.
    assert [step.tool for step in tool_steps(ending_call)].count("end_scene") == 1


def test_e2e_camp_is_followed_by_the_morning_and_then_by_a_template_round():
    turns = e2e_turns()
    camp = first_turn_with(turns, "start_camp")

    morning = turns[camp + 1]
    # Конец привала: длинный отдых делает сервер, а не инструмент; мастер только рассказывает.
    assert {step.tool for step in tool_steps(morning)} <= {"chronicle"}
    assert turns[-1].repeat  # ход-шаблон на случай лишних вызовов после перезапуска сервера


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


@pytest.mark.parametrize("name", GENERATOR_DEMOS)
def test_generator_demo_answers_every_step(name):
    scenario = load_scenario(SCENARIOS_DIR / name)

    assert set(generator_purposes(scenario)) == {f"generator.{step}" for step in STEPS}


@pytest.mark.parametrize(("name", "hours"), GENERATOR_DEMOS.items())
async def test_generator_demo_leaves_no_answer_unused(name, hours):
    # Лишний ответ в сценарии прячет расхождение с генератором: он никогда не будет сыгран.
    scenario = load_scenario(SCENARIOS_DIR / name)
    provider = CountingProvider(scenario)

    result = await generate(provider, generator_draft(hours))

    assert not result.report.blocking

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


# --- привалы (блок B9 DND-2) ---


@pytest.mark.parametrize(
    "path",
    [path for path in SCENARIO_FILES if "campfire.ideas" in load_scenario(path).json_answers],
    ids=lambda path: path.name,
)
async def test_every_answer_of_the_campfire_host_passes_the_schema_of_the_ideas(path):
    scenario = load_scenario(path)
    provider = FakeProvider(scenario, pc_ids=PC_IDS)

    for _answer in answers_of(scenario, "campfire.ideas"):
        out = await provider.complete_json(
            JsonRequest(system="", prompt="", purpose="campfire.ideas", cheap=True), IdeasOut
        )
        assert out.ideas
        assert {idea.character_id for idea in out.ideas} <= set(PC_IDS)


async def test_the_rest_scenario_has_an_idea_for_each_of_three_heroes_and_ends_any_rest():
    scenario = load_scenario(SCENARIOS_DIR / "rest.yaml")
    provider = FakeProvider(scenario, pc_ids=PC_IDS[:3])

    out = await provider.complete_json(
        JsonRequest(system="", prompt="", purpose="campfire.ideas", cheap=True), IdeasOut
    )

    assert [idea.character_id for idea in out.ideas] == list(PC_IDS[:3])
    # Привал начинает хост без мастера: ход-шаблон сценария — «конец привала», без смены сцены.
    [turn] = scenario.turns
    assert turn.repeat and {step.tool for step in tool_steps(turn)} == {"chronicle"}


# --- сквозной сценарий Playwright «Привалы» (B9.4) ---
#
# `e2e_rests.yaml` играет сервер под браузерами троих игроков (`services/webapp/e2e/rests.spec.ts`).
# Привалы начинает хост кнопкой, без хода мастера, поэтому после открытия идут только рассказы
# концов привалов, а идеи `campfire.ideas` — по ответу на каждый привал. Тест Playwright рассчитывает
# на этот порядок и ищет тексты идей целиком: ниже он закреплён, чтобы правка сценария не разошлась
# с тестом молча.

REST_E2E_SCENARIO = SCENARIOS_DIR / "e2e_rests.yaml"
REST_E2E_PC_IDS = PC_IDS[:3]
IDEAS_REQUEST = JsonRequest(system="", prompt="", purpose="campfire.ideas", cheap=True)


def test_the_scenarios_of_the_rests_are_in_the_folder():
    assert {"rest.yaml", "e2e_rests.yaml"} <= {path.name for path in SCENARIO_FILES}


def test_e2e_rest_scenario_opens_with_a_blow_to_the_first_hero_and_then_only_ends_rests():
    opening, short_end, long_end, template = load_scenario(REST_E2E_SCENARIO).turns

    # Открытие: урон только первому герою — ему есть что лечить костью хитов, и он в фокусе
    # рассказа, поэтому идеи короткого привала достаются героям 2 и 3.
    assert {step.tool for step in tool_steps(opening)} == {"damage", "chronicle"}
    assert [s.args["character_id"] for s in tool_steps(opening) if s.tool == "damage"] == ["$pc1"]
    # Концы привалов и шаблон: отдых делает сервер, привал начинает хост — мастер только
    # рассказывает (ни `start_camp`, ни `end_scene`).
    for turn in (short_end, long_end, template):
        assert {step.tool for step in tool_steps(turn)} <= {"chronicle"}
    assert (short_end.repeat, long_end.repeat, template.repeat) == (False, False, True)
    for turn in (opening, short_end, long_end, template):
        assert isinstance(turn.steps[-1], TextStep) and turn.steps[-1].text.strip()


async def test_e2e_rest_scenario_answers_two_rests_with_an_idea_for_each_of_three_heroes():
    scenario = load_scenario(REST_E2E_SCENARIO)
    provider = FakeProvider(scenario, pc_ids=REST_E2E_PC_IDS)

    answers = [await provider.complete_json(IDEAS_REQUEST, IdeasOut) for _ in range(2)]

    # Короткий привал, затем долгий: в каждом ответе идея каждому из троих по порядку.
    assert len(answers_of(scenario, "campfire.ideas")) == 2
    for out in answers:
        assert [idea.character_id for idea in out.ideas] == list(REST_E2E_PC_IDS)
    texts = [idea.text for out in answers for idea in out.ideas]
    # Тест Playwright ищет каждую идею целиком: ни одна не входит в другую, кавычек внутри нет.
    for text in texts:
        assert [other for other in texts if text in other] == [text]
        assert not set('"«»') & set(text), text
    with pytest.raises(ProviderError, match="сценарий исчерпан"):
        await provider.complete_json(IDEAS_REQUEST, IdeasOut)  # третьего привала тест не начинает
