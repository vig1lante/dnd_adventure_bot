"""Общие помощники тестов генератора: сценарии, провайдер-запись и черновики заказа.

Модуль лежит рядом с тестами `tests/authoring/`: pytest кладёт эту папку в `sys.path`, поэтому
тесты берут его как `from generator_fakes import …`. Живых моделей нет: ответы шагов проигрывает
`FakeProvider`, а обёртка запоминает запросы и считает одновременные вызовы.
"""

import asyncio
import copy
from pathlib import Path
from typing import Any

from dnd_server.authoring.draft import AuthoringDraft, DraftSettings
from dnd_server.llm.base import JsonRequest
from dnd_server.llm.fake import FakeProvider, Scenario, load_scenario

# Конфтест сервера переходит во временную папку, поэтому путь считаем от файла.
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
DEMO_SCENARIO = SCENARIOS_DIR / "generator_demo.yaml"
V2_SCENARIO = SCENARIOS_DIR / "generator_v2_demo.yaml"

MODULES = ("bg3_flavor", "campfire")


def demo_answers() -> dict[str, Any]:
    """Свежие ответы шагов короткого демо: каждый тест правит свою копию."""
    return copy.deepcopy(load_scenario(DEMO_SCENARIO).json_answers)


def v2_answers() -> dict[str, Any]:
    """Свежие ответы шагов демо формата v2."""
    return copy.deepcopy(load_scenario(V2_SCENARIO).json_answers)


def good_clues(answers: dict[str, Any]) -> dict[str, Any]:
    """Ответ `clues` короткого демо без плохого первого: проход без круга исправления."""
    return answers["generator.clues"][1]


def demo_draft(**settings: Any) -> AuthoringDraft:
    """Заказ короткого демо: 1 час, 6 игроков, 3 уровень, модули bg3_flavor и campfire."""
    values = {
        "pitch": "Колокол в часовне звонит сам по ночам.",
        "target_hours": 1,
        "party_size": 6,
        "party_level": 3,
        "modules": MODULES,
    }
    return AuthoringDraft(settings=DraftSettings(**(values | settings)))


def v2_draft(**settings: Any) -> AuthoringDraft:
    """Заказ демо v2: 2 часа, 6 игроков, 3 уровень, модули bg3_flavor и campfire."""
    return demo_draft(**({"pitch": "Маяк на мысу погас.", "target_hours": 2} | settings))


class RecordingProvider:
    """Обёртка над `FakeProvider`: запоминает запросы и число одновременных вызовов."""

    name = "recording"

    def __init__(self, answers: dict[str, Any]) -> None:
        self._fake = FakeProvider(Scenario.model_validate({"json": answers}))
        self.requests: list[JsonRequest] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def run_turn(self, req, tools):
        raise AssertionError("генератор не ведёт ход мастера")

    async def complete_json(self, req: JsonRequest, schema):
        self.requests.append(req)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Отдаём управление: запросы, которые разрешено начать вместе, успевают начаться.
            await asyncio.sleep(0)
            return await self._fake.complete_json(req, schema)
        finally:
            self.in_flight -= 1

    def purposes(self) -> list[str]:
        return [request.purpose for request in self.requests]

    def prompts(self, purpose: str) -> list[str]:
        return [request.prompt for request in self.requests if request.purpose == purpose]
