"""Привал на стороне мастера (план B7.6, спека §5.5 «Привал у костра» и §8.4).

Привал начинает мастер инструментом `start_camp`: сцена становится `camp`, а сервер ставит фазу игры
`camp` с отсчётом `games.camp_seconds` (`turns.flow.scene_follow_up`). Пока он идёт, игроки
оставляют реплики героев, хост может продлить привал или закончить его раньше. Мастер привал не
заканчивает: инструмент `end_scene` в сцене `camp` отказывает, конец привала — дело сервера
(отсчёт или кнопка хоста).

Конец привала — это ход мастера «конец привала» (`TurnKind.CAMP_END`), и длинный отдых входит
в него: сервер проводит его на рабочей копии состояния до обращения к модели (`end_camp`), в той
же транзакции, что и рассказ об утре. Поэтому мастер видит утро следующего дня и полное здоровье и
рассказывает, как герои просыпаются, а неудачный ход или «Перегенерировать» откатывают отдых вместе
с рассказом (копия просто выбрасывается). Функция чистая и детерминированная: кубов в отдыхе нет.
"""

import logging
from datetime import timedelta

from dnd_engine.adventure.schema import Adventure
from dnd_engine.combat import engine
from dnd_engine.content import SrdContent
from dnd_engine.events import Event
from dnd_engine.modules.base import RuleSet
from dnd_engine.state import GameState, SceneType

logger = logging.getLogger(__name__)

# «+1 минута» хоста на привале (спека §5.5: хост может продлить привал).
CAMP_EXTENSION = timedelta(seconds=60)
# Реплики героев попадают в запрос к модели: у каждой есть предел длины, а у героя — число реплик за
# привал, иначе один игрок раздул бы контекст хода (и счёт за него) без конца.
CAMP_LINE_MAX_CHARS = 400
CAMP_LINES_PER_HERO = 10


def end_camp(
    state: GameState,
    turn: int,
    rules: RuleSet,
    *,
    content: SrdContent,
    adventure: Adventure,
) -> tuple[GameState, list[Event]]:
    """Конец привала: длинный отдых всем (триггер модуля `campfire`) и сцена исследования.

    `turn` — номер хода мастера, в котором сцена начинается. Движок сам вернёт время на утро
    следующего дня и обнулит счётчик коротких отдыхов. Исходное состояние не меняется. Если сцена
    не `camp` (ошибка данных: фаза игры говорит о привале, а сцена другая), отдыха не будет и
    состояние вернётся как есть: ход расскажется обычным образом, а не будет падать при повторах."""
    if state.scene.type != SceneType.CAMP:
        logger.warning(
            "Игра %s: конец привала, а сцена «%s»: длинного отдыха не будет.",
            state.game_id,
            state.scene.type.value,
        )
        return state, []
    morning = state.model_copy(update={"turn": turn})
    return engine.end_scene(
        morning, SceneType.EXPLORATION, rules, content=content, adventure=adventure
    )
