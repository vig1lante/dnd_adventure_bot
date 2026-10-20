"""Бой на стороне мастера: чьи ходы он разыгрывает и как ход передаётся дальше (план B7.5, спека
§8.4 и §8.5).

Ход в бою идёт по инициативе. Игрок, чей герой ходит, пишет действие и нажимает «Готов»; тогда
мастер одним вызовом разбирает действие героя и идущие следом ходы монстров, пока очередь не дойдёт
до следующего героя, способного действовать (`combat_acting`). Это «Кто действует» контекста
мастера и `ToolContext.acting`: боевые инструменты передают ход (`advance_to`) только названным
бойцам, а остальных не пропускают.

После вызова модели сервер сам передаёт ход (`settle_combat`) — на рабочей копии до коммита, в той
же транзакции, что и ход мастера (спека §8.4). Поэтому «Перегенерировать» откатывает и эти броски,
а неудавшийся ход не оставляет ни передачи хода, ни спасбросков от смерти. Правила:
- ход передаётся от текущего бойца до следующего героя, способного действовать. Лежащий на 0 HP
  герой пропускается сразу (его спасбросок от смерти бросает движок в начале хода). Монстры из
  `acting`, за которых мастер не сходил, теряют ход: иначе ход игры зависел бы от прилежности
  модели. Монстр вне `acting` своего хода не теряет: так бывает, когда герой, которым кончалось
  окно, упал в этом же вызове; передача останавливается на таком монстре, и его разыгрывает
  следующий вызов мастера. Инструмент `end_turn` для каждого из `acting` не зовётся: обёртки уже
  передали ход бойцам, которые ходили;
- перед каждой передачей хода проверяется победитель (`engine.combat_winner`), а цикл ограничен
  длиной очереди: бой, в котором ходить некому (все герои парализованы), не зависает под блокировкой
  игры, ход остаётся у монстра, и его разыгрывает следующий вызов мастера (`master_moves_next`);
- если победитель определён, а мастер не вызвал `end_scene`, сервер выводит из боя сам:
  `end_scene(exploration)`. Так же закрывается поражение отряда.

Вызов, в котором бой только начался (раунд, где мастер вызвал `start_combat`), ход не передаёт:
действие героя никто не сдавал. Там сервер лишь пропускает бойцов, которые не могут ходить, до
первого способного (героя ждут, монстра разыгрывает мастер сразу, без игрока).
"""

import logging

from dnd_engine.combat import engine
from dnd_engine.combat.model import Combatant
from dnd_engine.events import Event
from dnd_engine.state import GameState, SceneType
from dnd_engine.tools.combat import combat_content
from dnd_engine.tools.registry import ToolContext, commit_state
from dnd_server.db.models import TurnKind

logger = logging.getLogger(__name__)

# Текст хода, который сервер сдаёт за героя, когда отсчёт «Поторопить» вышел, а черновика нет.
DODGE_TEXT = "Уклоняется"
# Сколько ходов мастера подряд сервер запускает сам, пока ходят одни монстры (все герои не могут
# действовать): без предела бой, в котором герои парализованы надолго, тратил бы платные вызовы.
MAX_AUTO_TURNS = 12


def _current(state: GameState) -> Combatant | None:
    combat = state.combat
    if combat is None or not 0 <= combat.turn_index < len(combat.order):
        return None
    return combat.combatants[combat.order[combat.turn_index]]


def combat_acting(state: GameState) -> list[str]:
    """Бойцы, за которых мастер ходит в боевом вызове: текущий боец и идущие следом, пока очередь
    не дойдёт до следующего героя, способного действовать (его ход будет отдельным вызовом).

    Не способные действовать (лежащий герой, поверженный или парализованный монстр) в список не
    входят: их ходы проходят вхолостую, и монстры за ними всё равно получают ход в этом же вызове.
    Боя нет — список пуст."""
    combat = state.combat
    if combat is None or not combat.order:
        return []
    size = len(combat.order)
    acting: list[str] = []
    for step in range(size):
        fighter = combat.combatants[combat.order[(combat.turn_index + step) % size]]
        if not engine.can_act(state, fighter.id):
            continue
        if step > 0 and fighter.kind == "pc":
            break
        acting.append(fighter.id)
    return acting


def waiting_hero(state: GameState) -> Combatant | None:
    """Герой, чей ход ждёт игрока: текущий боец, если это герой и он способен действовать."""
    actor = _current(state)
    if actor is not None and actor.kind == "pc" and engine.can_act(state, actor.id):
        return actor
    return None


def master_moves_next(state: GameState) -> bool:
    """Бой идёт, а ждать некого: следующий ход разыгрывает мастер (ходят монстры)."""
    return state.combat is not None and waiting_hero(state) is None


def settle_combat(ctx: ToolContext, mode: TurnKind) -> list[Event]:
    """Конец удавшегося вызова модели на рабочей копии: передача хода и выход из боя.

    Вызывается до коммита хода мастера, поэтому всё сделанное здесь попадает в тот же снимок
    состояния и пропадает вместе с копией, если ход не удался. Броски передачи хода (спасброски от
    смерти, повторные спасброски состояний) размечаются по своим актёрам, как броски любого
    действия: бросок тайного автора виден только ему. Возвращает события, которые сделал сервер:
    ход мастера пишет их в журнал отдельной записью."""
    if ctx.state.combat is None:
        return []  # боя нет или мастер сам закончил сцену
    first = len(ctx.events)
    ctx.action_from = first
    _hand_over(ctx, after_fight=mode == TurnKind.COMBAT)
    ctx.begin_action(len(ctx.events))
    if ctx.state.combat is not None and engine.combat_winner(ctx.state) is not None:
        _leave_combat(ctx)
    return ctx.events[first:]


def _hand_over(ctx: ToolContext, *, after_fight: bool) -> None:
    """Передаёт ход вперёд до бойца, который теперь ходит сам.

    После боевого вызова (`after_fight`) ход текущего бойца сыгран: он кончается сразу, и ход
    останавливается на ближайшем герое, способном действовать, или на монстре вне `acting`, а
    монстры из `acting`, не сыгравшие свой ход, его теряют. В остальных вызовах (бой начался в
    раунде) `acting` пуст, и ход текущего бойца не кончается: ждать нужно первого способного
    бойца, героя или монстра."""
    content = combat_content(ctx)
    limit = len(ctx.state.combat.order)
    for step in range(limit + 1):
        state = ctx.state
        if engine.combat_winner(state) is not None:
            return  # воевать больше некому: бой закроет вызывающий
        if (step > 0 or not after_fight) and _plays_next(state, ctx.acting):
            return
        if step == limit:
            break  # очередь пройдена полностью: цикл конечен
        passed, events = engine.end_turn(state, rng=ctx.rng, content=content)
        commit_state(ctx, passed, events)
    logger.warning(
        "Игра %s: очередь боя прошла полный круг, и ходить пока некому.", ctx.state.game_id
    )


def _plays_next(state: GameState, offered: tuple[str, ...]) -> bool:
    """Текущий боец ходит сам: герой, способный действовать, или монстр, которого мастеру в этом
    вызове не называли (`offered` — бойцы вызова, их ход уже предложен мастеру)."""
    actor = _current(state)
    if actor is None or not engine.can_act(state, actor.id):
        return False
    return actor.kind == "pc" or actor.id not in offered


def _leave_combat(ctx: ToolContext) -> None:
    """Бой выигран или проигран, а мастер не завершил сцену: сервер завершает её сам."""
    ended, events = engine.end_scene(
        ctx.state,
        SceneType.EXPLORATION,
        ctx.rules,
        content=combat_content(ctx),
        adventure=ctx.adventure,
    )
    commit_state(ctx, ended, events)
