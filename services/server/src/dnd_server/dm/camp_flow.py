"""Привал на стороне мастера.

Привал бывает долгим и коротким (вид лежит в сцене: `Scene.rest`). Долгий начинает мастер
инструментом `start_camp` или хост кнопкой, короткий — только хост кнопкой (`turns.flow.start_rest`,
без хода мастера). Сцена становится `camp`, а сервер ставит фазу игры `camp` с отсчётом своего вида
(`rest_seconds`: `games.short_rest_seconds` или `games.camp_seconds`). Пока он идёт, игроки
оставляют реплики героев, на коротком тратят кости хитов, хост может продлить привал или закончить
его раньше. Мастер привал не заканчивает: инструмент `end_scene` в сцене `camp` отказывает, конец
привала — дело сервера (отсчёт или кнопка хоста).

Конец привала — это ход мастера «конец привала» (`TurnKind.CAMP_END`), и отдых входит в него: сервер
проводит его на рабочей копии состояния до обращения к модели (`end_camp`), в той же транзакции, что
и рассказ. Вид отдыха выбирает движок по виду привала: после долгого мастер видит 8 часов ближайшего
утра (привал после полуночи — того же дня, иначе следующего) и полное здоровье, после короткого —
вернувшиеся особенности «до короткого отдыха» и время на час позже. Неудачный ход или
«Перегенерировать» откатывают отдых вместе с рассказом (копия просто выбрасывается). Функция чистая
и детерминированная: кубов в отдыхе нет, кости хитов игроки бросают во время привала (через сервер).
"""

import logging
from datetime import timedelta
from typing import Literal

from dnd_engine.adventure.schema import Adventure
from dnd_engine.combat import engine
from dnd_engine.content import SrdContent
from dnd_engine.events import Event
from dnd_engine.modules.base import RuleSet
from dnd_engine.state import GameState, SceneType
from dnd_server.db.models import Game

logger = logging.getLogger(__name__)

# «+1 минута» хоста на привале: хост может продлить привал.
CAMP_EXTENSION = timedelta(seconds=60)
# Реплики героев попадают в запрос к модели: у каждой есть предел длины, а у героя — число реплик за
# привал, иначе один игрок раздул бы контекст хода (и счёт за него) без конца.
CAMP_LINE_MAX_CHARS = 400
CAMP_LINES_PER_HERO = 10

RestKind = Literal["short", "long"]


def rest_kind(state: GameState) -> RestKind:
    """Вид привала сцены: короткий только явно, сцена `camp` без вида (прежний привал) — долгий."""
    return "short" if state.scene.rest == "short" else "long"


def is_short_rest(state: GameState) -> bool:
    """Идёт короткий привал: только на нём герои тратят кости хитов."""
    return state.scene.type == SceneType.CAMP and state.scene.rest == "short"


def rest_seconds(game: Game, kind: RestKind) -> int:
    """Срок привала вида `kind` в игре: короткий — `short_rest_seconds`, долгий — `camp_seconds`."""
    return game.short_rest_seconds if kind == "short" else game.camp_seconds


def end_camp(
    state: GameState,
    turn: int,
    rules: RuleSet,
    *,
    content: SrdContent,
    adventure: Adventure,
) -> tuple[GameState, list[Event]]:
    """Конец привала: отдых его вида всем (триггер модуля `campfire`) и сцена исследования.

    `turn` — номер хода мастера, в котором сцена начинается. После долгого привала движок сам
    поставит время на 8 часов ближайшего утра и обнулит счётчик коротких отдыхов, после короткого —
    вернёт особенности «до короткого отдыха», сдвинет время на час и прибавит счётчик. Исходное
    состояние не меняется. Если сцена не `camp` (ошибка данных: фаза игры говорит о привале, а сцена
    другая), отдыха не будет и состояние вернётся как есть: ход расскажется обычным образом, а не
    будет падать при повторах."""
    if state.scene.type != SceneType.CAMP:
        logger.warning(
            "Игра %s: конец привала, а сцена «%s»: отдыха не будет.",
            state.game_id,
            state.scene.type.value,
        )
        return state, []
    morning = state.model_copy(update={"turn": turn})
    return engine.end_scene(
        morning, SceneType.EXPLORATION, rules, content=content, adventure=adventure
    )
