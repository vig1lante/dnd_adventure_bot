"""Модуль «Привал у костра»: тип сцены `camp`, привал по реальному таймеру, отдых в его конце.

Привал бывает долгим и коротким: вид лежит в `Scene.rest`. Любой привал начинает `begin_rest` —
её вызывают инструмент мастера `start_camp(reason)` (всегда долгий) и сервер по кнопке хоста.
Конец привала — не инструмент: его вызывает таймер или хост, а сервер зовёт
`combat.engine.end_scene`. Правило конца привала — триггер `on_scene_end` для сцены `camp`.
После долгого привала он проводит длинный отдых всем персонажам, переводит время на ближайшие
8 утра (`wake_up_time`) и обнуляет счётчик коротких отдыхов. После короткого — возвращает
особенности «до короткого отдыха», прибавляет к счётчику один и сдвигает часы на час (через
полночь — на следующий день): кости хитов игроки тратят во время привала (`rest.spend_hit_die`).
Промпт мастера описывает остальное: темы у костра, реплики персонажей и рассказ о конце привала.
"""

from typing import Any, Literal

from pydantic import Field

from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import SrdContent
from dnd_engine.events import Event, RestEvent
from dnd_engine.modules.base import MAX_SHORT_RESTS_PER_LONG, RuleRegistry, RuleSet
from dnd_engine.rest import SHORT_REST_HOURS, RestError, finish_short_rest, long_rest
from dnd_engine.state import DAY_PART_STARTS, GameState, GameTime, SceneType

LONG_REST_WAKE_HOUR = 8  # после долгого привала отряд просыпается в 8 утра

CAMP_FRAGMENT = """\
## Привал у костра

Привал бывает долгим и коротким. Оба идут по реальному таймеру, хост может продлить привал или
закончить раньше; конец привала — дело таймера или хоста, а не твоё. Привал возможен только
вне боя.

Долгий привал (по умолчанию 5 минут). Когда отряд решает отдохнуть и вокруг тихо, вызови
start_camp; хост тоже может начать долгий привал кнопкой. В конце долгого привала движок сам
проведёт длинный отдых и сдвинет время на 8 утра: после полуночи — того же дня, иначе
следующего. HP, ячейки, кости хитов и время не пересчитывай, а расскажи, как герои просыпаются
утром.

Короткий привал (по умолчанию 2 минуты) начинает только хост кнопкой. Во время него игроки сами
тратят кости хитов: бросает сервер, а не ты. В конце движок вернёт особенности «до короткого
отдыха» и сдвинет время на час; часть суток считается по часам. HP, ресурсы и время не
пересчитывай; расскажи коротко, что изменилось за час отдыха.

Когда привал начинаешь ты, предложи 2–3 темы для разговора у костра, опираясь на личные зацепки
персонажей: их цели, связи с людьми и местами. Чужие тайны при всех не раскрывай. Игроки
оставляют реплики своих персонажей: отвечай на них и дай героям поговорить друг с другом.
"""


def begin_rest(
    state: GameState,
    kind: Literal["short", "long"],
    rules: RuleSet,
    *,
    content: SrdContent,
    adventure: Adventure,
) -> tuple[GameState, list[Event]]:
    """Начало привала вида `kind`: сцена `camp` с этим видом; сам отдых — в конце привала.

    Отказ — `RestError` с объяснением для игроков: идёт бой, уже идёт привал или, для короткого,
    исчерпан предел коротких отдыхов `MAX_SHORT_RESTS_PER_LONG` (долгий отдых его снимает).
    Сцена меняется общей функцией `end_scene`, поэтому события — её события. Исходное состояние
    не меняется: возвращается копия."""
    # Импорт по месту, как в `start_camp_tool`: `combat.engine` сам импортирует пакет модулей.
    from dnd_engine.combat.engine import end_scene

    if state.combat is not None:
        raise RestError("В бою привал невозможен: сначала закончите бой.")
    if state.scene.type == SceneType.CAMP:
        raise RestError("Привал уже идёт: второй начать нельзя.")
    limit = rules.flags.get(MAX_SHORT_RESTS_PER_LONG)
    if kind == "short" and limit is not None and state.short_rests_since_long >= limit:
        raise RestError(
            f"Между долгими привалами допустимо не больше {limit} коротких, а прошло уже "
            f"{state.short_rests_since_long}: нужен долгий привал."
        )
    camped, events = end_scene(state, SceneType.CAMP, rules, content=content, adventure=adventure)
    camped.scene.rest = kind
    return camped, events


def wake_up_time(asleep: GameTime) -> GameTime:
    """Когда отряд просыпается после долгого привала: в 8 часов ближайшего утра.

    День меняется в полночь, поэтому привал с 0:00 до начала утра идёт в ту же ночь и кончается
    утром того же дня (так же ночь после полуночи считает `advance_time`); с начала утра и позже
    ближайшее утро после отдыха — следующего дня."""
    same_night = asleep.hour < DAY_PART_STARTS["morning"]
    return GameTime(day=asleep.day + int(not same_night), hour=LONG_REST_WAKE_HOUR)


def rest_at_camp_end(state: GameState, content: SrdContent) -> tuple[GameState, list[Event]]:
    """Конец привала: отдых того вида, каким был привал.

    Долгий (и привал из старых снимков, у которого вида нет) — длинный отдых всем, ближайшие
    8 утра (`wake_up_time`), счётчик коротких отдыхов с нуля. Короткий — особенности «до короткого
    отдыха» всем, счётчик плюс один, часы на час вперёд (через полночь — следующий день).
    Мёртвые отдыха не получают (в событии у них пустой итог). Сцена не `camp` — ничего не
    меняется. Исходное состояние не меняется: возвращается его копия."""
    rested = state.model_copy(deep=True)
    if state.scene.type != SceneType.CAMP:
        return rested, []
    kind: Literal["short", "long"] = "short" if state.scene.rest == "short" else "long"
    rest = finish_short_rest if kind == "short" else long_rest
    outcomes = []
    for character_id, character in state.party.items():
        rested.party[character_id], outcome = rest(character, content)
        outcomes.append(outcome)
    if kind == "short":
        rested.short_rests_since_long += 1
        rested.time = state.time.plus_hours(SHORT_REST_HOURS)
    else:
        rested.time = wake_up_time(state.time)
        rested.short_rests_since_long = 0
    return rested, [RestEvent(rest=kind, outcomes=outcomes)]


def start_camp_tool() -> Any:
    """Спецификация инструмента `start_camp` (`ToolSpec`).

    Типы реестра инструментов подключаются здесь, а не наверху файла: `tools.registry` сам
    импортирует `modules.base`, и верхний импорт замкнул бы цикл между пакетами. Привал мастера —
    всегда долгий: его начинает `begin_rest`, а её отказ модель получает как `ToolError`."""
    from dnd_engine.tools.registry import (
        MAX_REASON_CHARS,
        ToolArgs,
        ToolContext,
        ToolError,
        ToolSpec,
        commit_state,
    )

    class StartCampArgs(ToolArgs):
        reason: str = Field(
            min_length=1,
            max_length=MAX_REASON_CHARS,
            description="коротко, почему отряд встаёт на привал",
        )

    def start_camp(ctx: ToolContext, args: StartCampArgs) -> dict[str, Any]:
        try:
            camped, events = begin_rest(
                ctx.state, "long", ctx.rules, content=ctx.content, adventure=ctx.adventure
            )
        except RestError as exc:
            raise ToolError(str(exc)) from None
        commit_state(ctx, camped, events)
        return {"scene": SceneType.CAMP.value}

    return ToolSpec(
        name="start_camp",
        description=(
            "Начать привал у костра: сцена camp по реальному таймеру, в конце движок проведёт "
            "длинный отдых. Только вне боя; привал заканчивает таймер или хост, а не мастер."
        ),
        args_model=StartCampArgs,
        handler=start_camp,
    )


class Campfire:
    id = "campfire"
    name = "Привал у костра"

    def register(self, registry: RuleRegistry) -> None:
        registry.add_trigger("on_scene_end", rest_at_camp_end)
        registry.add_prompt_fragment(CAMP_FRAGMENT)
        registry.add_tool(start_camp_tool())


CAMPFIRE = Campfire()
