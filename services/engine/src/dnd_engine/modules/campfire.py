"""Модуль «Привал у костра»: тип сцены `camp`, привал по реальному таймеру, длинный отдых.

Сцену `camp` начинает инструмент мастера `start_camp(reason)`: сам инструмент появится в B4.7.
Здесь — правило конца привала: триггер `on_scene_end` для сцены `camp` проводит длинный отдых
всем персонажам, переводит время на утро следующего дня и обнуляет счётчик коротких отдыхов.
Промпт мастера описывает остальное: темы у костра, реплики персонажей и рассказ об утре.
"""

from dnd_engine.content import SrdContent
from dnd_engine.events import Event, RestEvent
from dnd_engine.modules.base import RuleRegistry
from dnd_engine.rest import long_rest
from dnd_engine.state import GameState, GameTime, SceneType

CAMP_FRAGMENT = """\
## Привал у костра

Когда отряд решает отдохнуть и вокруг тихо, вызови start_camp: привал возможен только вне боя.
Он идёт по реальному таймеру (по умолчанию 5 минут), хост может продлить его или закончить
раньше.

В начале привала предложи 2–3 темы для разговора у костра, опираясь на личные зацепки
персонажей: их цели, связи с людьми и местами. Чужие тайны при всех не раскрывай. Игроки
оставляют реплики своих персонажей: отвечай на них и дай героям поговорить друг с другом.

В конце привала движок сам проведёт длинный отдых и сдвинет время на утро следующего дня.
HP, ячейки и время не пересчитывай, а расскажи, как герои просыпаются утром.
"""


def long_rest_at_camp_end(state: GameState, content: SrdContent) -> tuple[GameState, list[Event]]:
    """Конец привала: длинный отдых всем, утро следующего дня, счётчик коротких отдыхов с нуля.

    Мёртвые отдыха не получают (в событии у них пустой итог). Сцена не `camp` — ничего не
    меняется. Исходное состояние не меняется: возвращается его копия."""
    rested = state.model_copy(deep=True)
    if state.scene.type != SceneType.CAMP:
        return rested, []
    outcomes = []
    for character_id, character in state.party.items():
        rested.party[character_id], outcome = long_rest(character, content)
        outcomes.append(outcome)
    rested.time = GameTime(day=state.time.day + 1, part="morning")
    rested.short_rests_since_long = 0
    return rested, [RestEvent(rest="long", outcomes=outcomes)]


class Campfire:
    id = "campfire"
    name = "Привал у костра"

    def register(self, registry: RuleRegistry) -> None:
        registry.add_trigger("on_scene_end", long_rest_at_camp_end)
        registry.add_prompt_fragment(CAMP_FRAGMENT)


CAMPFIRE = Campfire()
