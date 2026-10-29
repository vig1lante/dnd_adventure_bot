"""Поток боя (план B7.5, спека §8.4 и §8.5).

Настоящий движок, демо-приключение `demo-crypt`, временная SQLite после миграций, настоящее
приложение (`create_app` и его `lifespan`) и `FakeProvider` по сценариям; время отсчётов ведёт
`ManualScheduler`. Живых моделей нет. Кубы подставляются через `app.state.rng`: `ScriptedRng`
там, где важны точные значения (инициатива, спасброски от смерти), и `FlatRng` там, где важен лишь
исход (все единицы — промахи, все двадцатки — попадания с максимальным уроном).

Что держат тесты:
- ход пишет только игрок, чей герой ходит по инициативе, и сразу после «Готов» ход идёт мастеру: он
  разбирает действие героя и ходы монстров за ним до следующего героя, способного действовать;
- передача хода делается на рабочей копии до коммита, одной транзакцией с ходом мастера: ход
  никому не пропускается, лежащий герой пропускается со спасброском от смерти по правилам
  движка, «Перегенерировать» откатывает и эти броски;
- перед каждой передачей хода проверяется победитель, цикл ограничен длиной очереди;
- победа или поражение выводят из боя, даже если мастер не вызвал `end_scene`;
- «Поторопить» ставит отсчёт, по его концу сдаётся черновик или «Уклоняется»;
- монстры, которые идут первыми, и «Повторить» после падения процесса не теряют бойцов.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveCondition, Character, DeathSaves, armor_class
from dnd_engine.combat import engine
from dnd_engine.combat.model import Combat, Combatant, Zone
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.events import CombatEvent, RollEvent, SceneEvent
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef, Scene, SceneType
from dnd_engine.tools import build_registry
from dnd_engine.tools.registry import ToolContext
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.models import (
    ActionStatus,
    DmTurnLog,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    Turn,
    TurnKind,
    TurnPhase,
    TurnStatus,
    User,
    new_id,
)
from dnd_server.dm import combat_flow
from dnd_server.dm import provider as provider_module
from dnd_server.dm.combat_flow import (
    DODGE_TEXT,
    combat_acting,
    master_moves_next,
    settle_combat,
    waiting_hero,
)
from dnd_server.dm.provider import GameProviders
from dnd_server.dm.turn import Deps, run_dm_turn
from dnd_server.llm.base import ProviderError, TurnRequest, TurnResult, Usage
from dnd_server.realtime.views import snapshot_for
from dnd_server.turns import flow as flow_module
from dnd_server.turns.timers import ManualScheduler

# tests/dm/test_combat_flow.py -> dm -> tests -> server -> services -> корень репозитория
REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_ADVENTURE_DIR = REPO_ROOT / "content" / "adventures" / "demo-crypt"
COMBAT_SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "combat.yaml"
SESSION_SECRET = "combat-session-secret-0123456789-abcdefghij"
TIMEOUT_S = 5
T0 = datetime(2030, 5, 1, 12, 0, 0, tzinfo=UTC)
HURRY_SECONDS = 60
HEROES = {  # ключ -> (имя, класс, вид); ключ же — id героя
    "alice": ("Алиса", "fighter", "human"),
    "boris": ("Борис", "rogue", "elf"),
    "vera": ("Вера", "cleric", "dwarf"),
}
MONSTER_IDS = {"sk1": "skeleton-1", "sk2": "skeleton-2", "sk3": "skeleton-3"}
MONSTER_HP = 13


def fid(name: str) -> str:
    """Id бойца по короткому имени из тестов (`sk1` -> `skeleton-1`, герои называются как есть)."""
    return MONSTER_IDS.get(name, name)


# --- кубы ---


class FlatRng:
    """Везде выпадает одно значение, обрезанное до граней куба: 20 — максимум на любом кубе
    (попадание, критический удар, наибольший урон), 1 — минимум (промах, провал)."""

    def __init__(self, value: int) -> None:
        self._value = value

    def randint(self, a: int, b: int) -> int:
        return max(a, min(b, self._value))


# --- состояния боя ---


def make_hero(srd: SrdContent, key: str, user_id: str | None = None) -> Character:
    name, class_id, species = HEROES[key]
    hero = quick_character(
        class_id, species, srd, name=name, concept="Бывший стражник", character_id=key
    )
    return hero.model_copy(update={"player_id": user_id})


def lay_down(hero: Character) -> None:
    """Герой на 0 HP без сознания, как после урона в бою."""
    hero.hp = 0
    hero.conditions = [ActiveCondition(id="unconscious", source="zero_hp")]


def make_monster(name: str, *, hp: int, zone: str) -> Combatant:
    number = int(name.removeprefix("sk"))
    return Combatant(
        id=fid(name),
        kind="monster",
        side="enemies",
        ref="skeleton",
        name=f"Скелет {number}",
        zone_id=zone,
        hp=hp,
        hp_max=MONSTER_HP,
        ac=14,
    )


def fight_state(
    srd: SrdContent,
    adventure: Adventure,
    order: Sequence[str] | None,
    *,
    current: int = 0,
    down: Collection[str] = (),
    dead: Collection[str] = (),
    felled: Collection[str] = (),
    paralyzed: Collection[str] = (),
    wounded: Mapping[str, int] | None = None,
    monster_hp: int = MONSTER_HP,
    rounds: int = 1,
    users: Mapping[str, str] | None = None,
    location_id: str = "cemetery",
) -> GameState:
    """Состояние игры на кладбище. `order` — очередь боя по именам (`alice`, `sk1`), в ней должны
    быть все трое героев; `None` — боя нет. Все стоят в одной зоне и вовлечены с противниками, так
    что рукопашные атаки возможны без перемещения."""
    users = users or {}
    party = {key: make_hero(srd, key, users.get(key)) for key in HEROES}
    for key, hp in (wounded or {}).items():
        party[key].hp = hp
    for key in (*down, *dead):
        lay_down(party[key])
    for key in dead:
        party[key].death_saves = DeathSaves(failures=3)
    for key in paralyzed:
        if key in party:
            party[key].conditions.append(ActiveCondition(id="paralyzed"))
    state = GameState(
        game_id=new_id(),
        adventure_id=adventure.id,
        rules=RulesRef(modules=list(adventure.rules.modules)),
        party=party,
        location_id=location_id,
        turn=1,
        version=1,
    )
    state.world.visited_locations = [location_id]
    if order is None:
        return state
    assert set(HEROES) <= set(order), "в очереди боя должны быть все герои"
    fighters: dict[str, Combatant] = {}
    for name in order:
        if name in HEROES:
            fighters[name] = Combatant(
                id=name, kind="pc", side="party", ref=name, name=HEROES[name][0], zone_id="gate"
            )
            continue
        hp = 0 if name in felled else monster_hp
        fighters[fid(name)] = make_monster(name, hp=hp, zone="gate")
    for name in paralyzed:
        if name not in HEROES:
            fighters[fid(name)].conditions.append(ActiveCondition(id="paralyzed"))
    for fighter in fighters.values():
        foes = "enemies" if fighter.side == "party" else "party"
        fighter.engaged_with = [f.id for f in fighters.values() if f.side == foes]
    state.scene = Scene(type=SceneType.COMBAT)
    state.combat = Combat(
        encounter_id=None,
        zones=[Zone(id="gate", name="Ворота")],
        links=[],
        combatants=fighters,
        order=[fid(name) for name in order],
        round=rounds,
        turn_index=current,
    )
    return state


def context_of(
    state: GameState,
    srd: SrdContent,
    adventure: Adventure,
    rng: Any,
    *,
    acting: Sequence[str] = (),
    secret: Collection[str] = (),
) -> ToolContext:
    return ToolContext(
        state=state,
        adventure=adventure,
        content=srd,
        rules=build_ruleset(state.rules),
        rng=rng,
        acting=tuple(acting),
        secret_actors=set(secret),
    )


def current_id(state: GameState) -> str:
    return engine.current_actor(state).id


def turn_actors(ctx: ToolContext) -> list[str]:
    """Чьи ходы начались за время вызова: по событиям движка, в порядке появления."""
    return [e.actor_id for e in ctx.events if isinstance(e, CombatEvent) and e.phase == "turn"]


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="module")
def adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


# --- чьи ходы разыгрывает мастер ---


def test_acting_is_the_hero_and_the_monsters_after_him_up_to_the_next_hero(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "sk2", "boris", "vera"])

    assert combat_acting(state) == ["alice", "skeleton-1", "skeleton-2"]


def test_monsters_at_the_head_of_the_queue_act_up_to_the_first_hero(srd, adventure):
    state = fight_state(srd, adventure, ["sk1", "sk2", "alice", "boris", "vera"])

    assert combat_acting(state) == ["skeleton-1", "skeleton-2"]


def test_acting_starts_from_whoever_is_current_and_goes_round_the_queue(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "boris", "vera", "sk1", "sk2"], current=2)

    assert combat_acting(state) == ["vera", "skeleton-1", "skeleton-2"]


def test_a_fallen_hero_does_not_stop_the_monsters_behind_him(srd, adventure):
    # Лежащий Борис не ходит, и монстр за ним должен получить ход в том же вызове мастера.
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "sk2", "vera"], down=["boris"])

    assert combat_acting(state) == ["alice", "skeleton-1", "skeleton-2"]


def test_the_only_hero_able_to_act_is_not_counted_twice_round_the_queue(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris", "vera"])

    assert combat_acting(state) == ["alice", "skeleton-1"]


def test_a_felled_or_paralyzed_monster_does_not_act(srd, adventure):
    state = fight_state(
        srd,
        adventure,
        ["alice", "sk1", "sk2", "sk3", "boris", "vera"],
        felled=["sk1"],
        paralyzed=["sk2"],
    )

    assert combat_acting(state) == ["alice", "skeleton-3"]


def test_there_is_nobody_to_act_when_no_combat_is_going(srd, adventure):
    assert combat_acting(fight_state(srd, adventure, None)) == []


def test_the_waiting_hero_is_the_current_fighter_if_he_is_a_hero_able_to_act(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"])

    assert waiting_hero(state).id == "alice"
    assert not master_moves_next(state)


@pytest.mark.parametrize(
    ("order", "options"),
    [
        (["sk1", "alice", "boris", "vera"], {}),
        (["alice", "sk1", "boris", "vera"], {"down": ["alice"]}),
        (["alice", "sk1", "boris", "vera"], {"paralyzed": ["alice"]}),
        (None, {}),
    ],
    ids=["ходит монстр", "герой лежит", "герой парализован", "боя нет"],
)
def test_no_hero_waits_when_the_current_fighter_is_a_monster_or_cannot_act(
    srd, adventure, order, options
):
    state = fight_state(srd, adventure, order, **options)

    assert waiting_hero(state) is None


def test_the_master_moves_next_when_a_fight_goes_on_without_a_hero_to_wait_for(srd, adventure):
    assert master_moves_next(fight_state(srd, adventure, ["sk1", "alice", "boris", "vera"]))
    assert not master_moves_next(fight_state(srd, adventure, None))


# --- передача хода на рабочей копии ---


def test_after_the_master_the_turn_goes_to_the_next_hero_and_no_turn_is_skipped(srd, adventure):
    # «Ход не пропущен»: герой и монстр за ним сыграли, обёртки инструментов уже передали ход
    # монстру. Сервер добавляет ровно одну передачу, до Бориса. Если бы он закончил ход «для каждого
    # из acting», Борис остался бы без хода.
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"])
    ctx = context_of(state, srd, adventure, FlatRng(1), acting=["alice", "skeleton-1"])
    registry = build_registry(ctx.rules)
    registry.call(ctx, "combat_action", {"actor_id": "alice", "action": "dodge"})
    registry.call(
        ctx,
        "monster_action",
        {"monster_id": "skeleton-1", "action_id": "shortsword", "target_ids": ["alice"]},
    )
    assert turn_actors(ctx) == ["skeleton-1"]

    settle_combat(ctx, TurnKind.COMBAT)

    assert turn_actors(ctx) == ["skeleton-1", "boris"]
    assert current_id(ctx.state) == "boris"
    assert ctx.state.combat.round == 1


def test_a_monster_the_master_did_not_play_loses_its_turn_instead_of_hanging_the_fight(
    srd, adventure
):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"])
    ctx = context_of(state, srd, adventure, FlatRng(1), acting=["alice", "skeleton-1"])
    build_registry(ctx.rules).call(ctx, "combat_action", {"actor_id": "alice", "action": "dodge"})

    settle_combat(ctx, TurnKind.COMBAT)

    assert turn_actors(ctx) == ["skeleton-1", "boris"]
    assert current_id(ctx.state) == "boris"


def test_a_fallen_hero_is_skipped_with_a_death_save_by_the_engine(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris"])
    ctx = context_of(state, srd, adventure, ScriptedRng([15]), acting=["alice", "skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    saves = [e for e in ctx.events if isinstance(e, RollEvent)]
    assert [(e.actor_id, e.label) for e in saves] == [("boris", "Спасбросок от смерти")]
    assert turn_actors(ctx) == ["skeleton-1", "boris", "vera"]
    assert current_id(ctx.state) == "vera"
    assert ctx.state.party["boris"].death_saves.successes == 1


def test_a_hero_who_dies_on_the_death_save_is_skipped_and_the_fight_goes_on(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris"])
    state.party["boris"].death_saves = DeathSaves(failures=2)
    ctx = context_of(state, srd, adventure, ScriptedRng([5]), acting=combat_acting(state))

    settle_combat(ctx, TurnKind.COMBAT)

    assert ctx.state.party["boris"].death_saves.failures == 3
    assert current_id(ctx.state) == "vera"
    assert ctx.state.combat is not None


def test_the_monsters_behind_a_fallen_hero_are_passed_and_the_next_hero_is_found(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "boris", "sk1", "vera"], down=["boris"])
    ctx = context_of(state, srd, adventure, ScriptedRng([12]), acting=["alice", "skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert turn_actors(ctx) == ["boris", "skeleton-1", "vera"]
    assert current_id(ctx.state) == "vera"


def test_a_monster_behind_a_hero_who_fell_in_the_same_call_does_not_lose_its_turn(srd, adventure):
    # Окно мастера кончалось на Борисе (он стоял на ногах), но в этом же вызове скелет его уложил.
    # Скелет за Борисом в окне не был: передача хода останавливается на нём, а не пропускает его.
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "sk2", "vera"])
    acting = combat_acting(state)
    assert acting == ["alice", "skeleton-1"]
    lay_down(state.party["boris"])
    ctx = context_of(state, srd, adventure, ScriptedRng([12]), acting=acting)

    settle_combat(ctx, TurnKind.COMBAT)

    assert turn_actors(ctx) == ["skeleton-1", "boris", "skeleton-2"]
    assert current_id(ctx.state) == "skeleton-2"
    assert master_moves_next(ctx.state)
    assert combat_acting(ctx.state) == ["skeleton-2"]


def test_settling_returns_exactly_the_events_the_server_made(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris"])
    ctx = context_of(state, srd, adventure, FlatRng(1), acting=["alice", "skeleton-1"])
    build_registry(ctx.rules).call(ctx, "combat_action", {"actor_id": "alice", "action": "dash"})
    before = len(ctx.events)

    settled = settle_combat(ctx, TurnKind.COMBAT)

    assert settled == ctx.events[before:]
    assert [e.label for e in settled if isinstance(e, RollEvent)] == ["Спасбросок от смерти"]


def test_settling_a_scene_without_a_fight_returns_nothing(srd, adventure):
    ctx = context_of(fight_state(srd, adventure, None), srd, adventure, ScriptedRng([]))

    assert settle_combat(ctx, TurnKind.COMBAT) == []


def test_the_round_grows_when_the_hand_over_wraps_the_queue(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "boris", "vera", "sk1"], current=2)
    ctx = context_of(state, srd, adventure, FlatRng(1), acting=["vera", "skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert current_id(ctx.state) == "alice"
    assert ctx.state.combat.round == 2


def test_the_only_hero_gets_the_turn_back_after_a_full_circle(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris", "vera"])
    ctx = context_of(state, srd, adventure, ScriptedRng([12, 12]), acting=["alice", "skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert current_id(ctx.state) == "alice"
    assert ctx.state.combat.round == 2


def test_a_won_fight_is_closed_without_handing_the_turn_over(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], felled=["sk1"])
    ctx = context_of(state, srd, adventure, ScriptedRng([]), acting=["alice"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert turn_actors(ctx) == []  # передачи хода не было: ScriptedRng([]) не дал бы ни одного куба
    assert ctx.state.combat is None
    assert ctx.state.scene.type == SceneType.EXPLORATION
    ended = [e for e in ctx.events if isinstance(e, CombatEvent) and e.phase == "ended"]
    assert [e.winner for e in ended] == ["party"]
    assert any(isinstance(e, SceneEvent) for e in ctx.events)


def test_a_lost_fight_is_closed_the_same_way(srd, adventure):
    state = fight_state(
        srd, adventure, ["sk1", "alice", "boris", "vera"], down=["alice", "boris", "vera"]
    )
    ctx = context_of(state, srd, adventure, ScriptedRng([]), acting=["skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert ctx.state.combat is None
    ended = [e for e in ctx.events if isinstance(e, CombatEvent) and e.phase == "ended"]
    assert [e.winner for e in ended] == ["enemies"]


def test_the_winner_is_checked_before_every_hand_over(srd, adventure, monkeypatch):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"], down=["boris", "vera"])
    ctx = context_of(state, srd, adventure, FlatRng(12), acting=["alice", "skeleton-1"])
    calls: list[str] = []
    real_winner, real_end_turn = engine.combat_winner, engine.end_turn

    def watching_winner(s: GameState):
        calls.append("победитель")
        return real_winner(s)

    def watching_end_turn(*args: Any, **kwargs: Any):
        calls.append("конец хода")
        return real_end_turn(*args, **kwargs)

    monkeypatch.setattr(engine, "combat_winner", watching_winner)
    monkeypatch.setattr(engine, "end_turn", watching_end_turn)

    settle_combat(ctx, TurnKind.COMBAT)

    assert "конец хода" in calls
    for position, call in enumerate(calls):
        if call == "конец хода":
            assert calls[position - 1] == "победитель"


def test_a_scene_the_master_already_ended_is_left_alone(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"])
    ctx = context_of(state, srd, adventure, ScriptedRng([]), acting=["alice"])
    build_registry(ctx.rules).call(
        ctx, "end_scene", {"next": "exploration", "reason": "Скелет рассыпался."}
    )
    events_before = list(ctx.events)

    settle_combat(ctx, TurnKind.COMBAT)

    assert ctx.events == events_before
    assert ctx.state.scene.type == SceneType.EXPLORATION


def test_the_loop_is_bounded_by_the_queue_when_nobody_can_act(srd, adventure):
    # Все герои парализованы, ходит скелет: круг пройден, героя для ожидания нет. Цикл конечен, а
    # ход остаётся у монстра: дальше его разыграет мастер.
    state = fight_state(
        srd,
        adventure,
        ["alice", "sk1", "boris", "vera"],
        current=1,
        paralyzed=["alice", "boris", "vera"],
    )
    ctx = context_of(state, srd, adventure, FlatRng(1), acting=["skeleton-1"])

    settle_combat(ctx, TurnKind.COMBAT)

    assert len(turn_actors(ctx)) == 4  # ровно по числу бойцов очереди
    assert current_id(ctx.state) == "skeleton-1"
    assert ctx.state.combat.round == 2
    assert waiting_hero(ctx.state) is None
    assert master_moves_next(ctx.state)


def test_a_fight_started_in_a_round_turn_keeps_the_hero_who_is_first(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "sk1", "boris", "vera"])
    ctx = context_of(state, srd, adventure, ScriptedRng([]))

    settle_combat(ctx, TurnKind.ROUND)

    assert ctx.events == []
    assert current_id(ctx.state) == "alice"


def test_a_fight_started_in_a_round_turn_keeps_the_monster_who_is_first_for_the_master(
    srd, adventure
):
    state = fight_state(srd, adventure, ["sk1", "alice", "boris", "vera"])
    ctx = context_of(state, srd, adventure, ScriptedRng([]))

    settle_combat(ctx, TurnKind.ROUND)

    assert ctx.events == []
    assert master_moves_next(ctx.state)


def test_a_fight_started_in_a_round_turn_skips_a_first_hero_who_cannot_act(srd, adventure):
    state = fight_state(srd, adventure, ["boris", "alice", "sk1", "vera"], down=["boris"])
    ctx = context_of(state, srd, adventure, ScriptedRng([]))

    settle_combat(ctx, TurnKind.ROUND)

    assert turn_actors(ctx) == ["alice"]
    assert current_id(ctx.state) == "alice"
    # Спасбросок первого бойца бросил сам `start_combat` при начале хода; здесь кубов нет.
    assert [e for e in ctx.events if isinstance(e, RollEvent)] == []


def test_rolls_of_the_end_of_a_secret_hero_turn_are_private_to_him(srd, adventure):
    # Повторный спасбросок в конце хода тайного автора — его бросок, как и бросок действия.
    state = fight_state(srd, adventure, ["alice", "boris", "vera", "sk1"])
    state.party["alice"].conditions.append(
        ActiveCondition(id="poisoned", rounds_left=3, save="con", dc=12)
    )
    ctx = context_of(state, srd, adventure, ScriptedRng([15]), acting=["alice"], secret=["alice"])

    settle_combat(ctx, TurnKind.COMBAT)

    [save] = [e for e in ctx.events if isinstance(e, RollEvent)]
    assert (save.actor_id, save.audience) == ("alice", "alice")


def test_rolls_of_the_hand_over_are_public_when_the_author_is_not_secret(srd, adventure):
    state = fight_state(srd, adventure, ["alice", "boris", "vera", "sk1"])
    state.party["alice"].conditions.append(
        ActiveCondition(id="poisoned", rounds_left=3, save="con", dc=12)
    )
    ctx = context_of(state, srd, adventure, ScriptedRng([15]), acting=["alice"])

    settle_combat(ctx, TurnKind.COMBAT)

    [save] = [e for e in ctx.events if isinstance(e, RollEvent)]
    assert save.audience == "all"


# --- стенд: приложение, игра, игроки ---


@dataclass(frozen=True)
class Seat:
    key: str
    user_id: str
    character_id: str
    name: str
    token: str


@dataclass(frozen=True)
class Setup:
    seats: dict[str, Seat]
    game_id: str
    turn_id: str
    created: datetime


class RecordingBroadcaster:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(self, game_id: str, payload: dict[str, Any], audience: str = "all") -> None:
        self.sent.append((game_id, payload, audience))

    def of_type(self, event_type: str) -> list[tuple[dict[str, Any], str]]:
        return [(p, who) for _game, p, who in self.sent if p["type"] == event_type]


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def notify(self, user_id: str, text: str) -> None:
        self.sent.append((user_id, text))

    def texts_for(self, user_id: str) -> list[str]:
        return [text for who, text in self.sent if who == user_id]


class StubProvider:
    """Модель, которой управляет тест: готовые ответы или ошибки, запросы запоминаются."""

    name = "stub"

    def __init__(self, *outcomes: TurnResult | Exception) -> None:
        self._outcomes = list(outcomes) or [narration()]
        self.requests: list[TurnRequest] = []

    async def run_turn(self, req: TurnRequest, tools: Any) -> TurnResult:
        self.requests.append(req)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def complete_json(self, req: Any, schema: Any) -> Any:
        raise AssertionError("ход мастера не просит JSON")


def narration(text: str = "Рассказ мастера.") -> TurnResult:
    return TurnResult(text=text, trace=(), usage=Usage(11, 7, 5, 3), stop_reason="end_turn")


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(provider_module, "make_provider", lambda _settings, **_kw: provider)


def tool(name: str, /, **args: Any) -> dict[str, Any]:
    return {"tool": name, "args": args}


def say(text: str) -> dict[str, Any]:
    return {"text": text}


def scenario_turn(*steps: dict[str, Any], repeat: bool = False) -> dict[str, Any]:
    return {"steps": list(steps), "repeat": repeat} if repeat else {"steps": list(steps)}


def dodge(hero: str) -> dict[str, Any]:
    return tool("combat_action", actor_id=hero, action="dodge")


def dash(hero: str) -> dict[str, Any]:
    """Рывок: действие без кубов (Уклонение, в отличие от него, даёт помеху атакам по герою)."""
    return tool("combat_action", actor_id=hero, action="dash")


def skeleton_strikes(number: int, target: str) -> dict[str, Any]:
    return tool(
        "monster_action",
        monster_id=f"skeleton-{number}",
        action_id="shortsword",
        target_ids=[target],
    )


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "session_secret": SecretStr(SESSION_SECRET),
            "fake_llm_scenario": COMBAT_SCENARIO,
            "content_dir": tmp_path / "content",
            "hurry_seconds": HURRY_SECONDS,
        }
    )


def make_seats() -> dict[str, Seat]:
    signer = SessionSigner(SESSION_SECRET)
    seats = {}
    for key, (name, _class_id, _species) in HEROES.items():
        user_id = new_id()
        seats[key] = Seat(key, user_id, key, name, signer.issue(user_id))
    return seats


async def seed_game(
    db: async_sessionmaker[AsyncSession],
    adventure: Adventure,
    seats: dict[str, Seat],
    state: GameState,
    *,
    turn_phase: TurnPhase,
    turn_kind: TurnKind,
    game_fields: Mapping[str, Any] | None = None,
) -> Setup:
    created = T0
    game_id, turn_id = state.game_id, new_id()
    fields: dict[str, Any] = {
        "phase": GamePhase.PLAYING,
        "turn_phase": turn_phase,
        "state_version": 1,
        "current_turn_id": turn_id,
        "collect_seconds": 120,
        "hurry_seconds": HURRY_SECONDS,
        "camp_seconds": 300,
        "started_at": created,
        "clock_at": created,
    } | dict(game_fields or {})
    async with db() as session:
        session.add_all(
            User(id=seat.user_id, dev_login=f"dev-{number}", name=seat.name)
            for number, seat in enumerate(seats.values(), start=1)
        )
        await session.flush()
        session.add(
            Game(
                id=game_id,
                code="ABC123",
                adventure_id=adventure.id,
                host_user_id=seats["alice"].user_id,
                adventure_data=adventure.model_dump(mode="json"),
                **fields,
            )
        )
        await session.flush()
        session.add_all(
            PlayerCharacter(
                id=seat.character_id,
                game_id=game_id,
                user_id=seat.user_id,
                data=state.party[seat.character_id].model_dump(mode="json"),
            )
            for seat in seats.values()
        )
        await session.flush()
        session.add_all(
            GamePlayer(
                game_id=game_id,
                user_id=seat.user_id,
                character_id=seat.character_id,
                is_host=seat.key == "alice",
                ready=True,
                joined_order=number,
            )
            for number, seat in enumerate(seats.values(), start=1)
        )
        session.add(GameStateSnapshot.from_state(state))
        session.add(
            Turn(
                id=turn_id,
                game_id=game_id,
                number=1,
                kind=turn_kind,
                status=TurnStatus.OPEN,
                state_version_before=1,
            )
        )
        await session.commit()
    return Setup(seats, game_id, turn_id, created)


@dataclass
class Table:
    app: Any
    http: httpx.AsyncClient
    scheduler: ManualScheduler
    broadcaster: RecordingBroadcaster
    notifier: RecordingNotifier
    db: async_sessionmaker[AsyncSession]
    settings: Settings
    srd: SrdContent
    setup: Setup
    tmp_path: Path

    @property
    def seats(self) -> dict[str, Seat]:
        return self.setup.seats

    @property
    def game_id(self) -> str:
        return self.setup.game_id

    def user_id(self, key: str) -> str:
        return self.seats[key].user_id

    # --- запросы ---

    async def call(self, key: str | None, method: str, path: str, **options: Any) -> httpx.Response:
        headers = {} if key is None else {"Authorization": f"Bearer {self.seats[key].token}"}
        return await self.http.request(
            method, f"/api/games/{self.game_id}{path}", headers=headers, **options
        )

    async def draft(
        self, key: str, text: str = "", *, secret: bool = False, inspiration: bool = False
    ) -> httpx.Response:
        body = {"text": text, "secret": secret, "inspiration": inspiration}
        return await self.call(key, "PUT", "/action", json=body)

    async def submit(self, key: str, text: str | None = None, **flags: bool) -> httpx.Response:
        if text is not None:
            assert (await self.draft(key, text, **flags)).status_code == 200
        return await self.call(key, "POST", "/action/submit")

    async def pass_(self, key: str) -> httpx.Response:
        return await self.call(key, "POST", "/action/pass")

    async def host(self, name: str, key: str = "alice") -> httpx.Response:
        return await self.call(key, "POST", f"/host/{name}")

    async def settle(self) -> None:
        """Ждёт, пока кончатся все разборы мастера, запущенные приложением."""
        await asyncio.wait_for(self.app.state.turn_tasks.join(), TIMEOUT_S)

    # --- подмены ---

    def set_rng(self, rng: Any) -> None:
        self.app.state.rng = rng

    def use_scenario(self, *turns: dict[str, Any]) -> None:
        """Мастер играет по этому сценарию (очередь с начала)."""
        path = self.tmp_path / f"scenario-{len(list(self.tmp_path.glob('scenario-*.yaml')))}.yaml"
        path.write_text(yaml.safe_dump({"turns": list(turns)}, allow_unicode=True), "utf-8")
        self.use_scenario_file(path)

    def use_scenario_file(self, path: Path) -> None:
        self.app.state.settings = self.app.state.settings.model_copy(
            update={"fake_llm_scenario": path}
        )
        self.app.state.game_providers = GameProviders()

    # --- что лежит в базе ---

    async def game(self) -> Game:
        async with self.db() as session:
            return await session.get(Game, self.game_id)

    async def state(self, version: int | None = None) -> GameState:
        async with self.db() as session:
            if version is None:
                version = (await session.get(Game, self.game_id)).state_version
            return (await session.get(GameStateSnapshot, (self.game_id, version))).to_state()

    async def turns(self) -> list[Turn]:
        async with self.db() as session:
            rows = await session.scalars(
                select(Turn).where(Turn.game_id == self.game_id).order_by(Turn.number)
            )
            return list(rows)

    async def actions(self, turn_id: str | None = None) -> dict[str, PlayerAction]:
        key_of = {seat.user_id: seat.key for seat in self.seats.values()}
        async with self.db() as session:
            rows = await session.scalars(
                select(PlayerAction).where(PlayerAction.turn_id == (turn_id or self.setup.turn_id))
            )
            return {key_of[row.user_id]: row for row in rows}

    async def messages(self, kind: MessageKind | None = None) -> list[Message]:
        async with self.db() as session:
            query = select(Message).where(Message.game_id == self.game_id).order_by(Message.id)
            if kind is not None:
                query = query.where(Message.kind == kind)
            return list(await session.scalars(query))

    async def logs(self) -> list[DmTurnLog]:
        async with self.db() as session:
            rows = await session.scalars(
                select(DmTurnLog).where(DmTurnLog.game_id == self.game_id).order_by(DmTurnLog.id)
            )
            return list(rows)

    async def snapshot_versions(self) -> list[int]:
        async with self.db() as session:
            rows = await session.scalars(
                select(GameStateSnapshot.version)
                .where(GameStateSnapshot.game_id == self.game_id)
                .order_by(GameStateSnapshot.version)
            )
            return list(rows)

    async def update_game(self, **values: Any) -> None:
        async with self.db() as session:
            await session.execute(update(Game).where(Game.id == self.game_id).values(**values))
            await session.commit()

    async def feed_of(self, key: str) -> list[dict[str, Any]]:
        async with self.db() as session:
            user = await session.get(User, self.user_id(key))
            return (await snapshot_for(session, user, self.game_id, srd=self.srd))["feed"]

    # --- рассылка ---

    def phases_sent(self) -> list[tuple[str | None, str | None]]:
        return [
            (p["phase"]["phase"], p["phase"]["turn_phase"])
            for p, _who in self.broadcaster.of_type("phase")
        ]

    async def whose_turn(self) -> str | None:
        state = await self.state()
        return None if state.combat is None else current_id(state)


@asynccontextmanager
async def running_table(
    settings: Settings,
    db: async_sessionmaker[AsyncSession],
    srd: SrdContent,
    setup: Setup,
    tmp_path: Path,
    *,
    scheduler: ManualScheduler | None = None,
    rng: Any = None,
) -> AsyncIterator[Table]:
    app = create_app(settings)
    scheduler = scheduler or ManualScheduler(setup.created)
    broadcaster = RecordingBroadcaster()
    app.state.scheduler = scheduler
    app.state.broadcaster = broadcaster
    app.state.rng = rng or FlatRng(1)
    async with app.router.lifespan_context(app):
        notifier = RecordingNotifier()
        app.state.notifier = notifier
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield Table(
                app, http, scheduler, broadcaster, notifier, db, settings, srd, setup, tmp_path
            )


FightTable = Callable[..., AbstractAsyncContextManager[Table]]


@pytest.fixture
def fight(settings, db, srd, adventure, tmp_path) -> FightTable:
    """Приложение с игрой в бою: `async with fight(["alice", "sk1", "boris", "vera"]) as table`.

    `order` — очередь боя (`None` — боя нет, игра в обсуждении). Остальные параметры:
    `scenario` — ходы сценария мастера (по умолчанию `combat.yaml`), `rng` — кубы приложения,
    `turn_phase`, `turn_kind`, `game_fields` — поля игры, остальное идёт в `fight_state`."""

    @asynccontextmanager
    async def open_table(
        order: Sequence[str] | None,
        *,
        scenario: Sequence[dict[str, Any]] | None = None,
        rng: Any = None,
        turn_phase: TurnPhase | None = None,
        turn_kind: TurnKind | None = None,
        game_fields: Mapping[str, Any] | None = None,
        **state_options: Any,
    ) -> AsyncIterator[Table]:
        seats = make_seats()
        state = fight_state(
            srd, adventure, order, users={k: s.user_id for k, s in seats.items()}, **state_options
        )
        in_combat = order is not None
        setup = await seed_game(
            db,
            adventure,
            seats,
            state,
            turn_phase=turn_phase or (TurnPhase.COMBAT_WAIT if in_combat else TurnPhase.DISCUSSION),
            turn_kind=turn_kind or (TurnKind.COMBAT if in_combat else TurnKind.ROUND),
            game_fields=game_fields,
        )
        run_settings = settings
        if scenario is not None:
            path = tmp_path / "scenario.yaml"
            path.write_text(yaml.safe_dump({"turns": list(scenario)}, allow_unicode=True), "utf-8")
            run_settings = settings.model_copy(update={"fake_llm_scenario": path})
        async with running_table(run_settings, db, srd, setup, tmp_path, rng=rng) as table:
            yield table

    return open_table


# --- сквозной бой по сценарию combat.yaml ---


async def test_the_master_starts_a_fight_and_the_first_hero_waits_for_his_turn(fight):
    # Кубы инициативы: Алиса 20+1, скелет 5+3, Борис 1+3, Вера 1+1 -> Алиса, скелет, Борис, Вера.
    async with fight(None, rng=ScriptedRng([20, 1, 1, 5])) as table:
        for key in HEROES:
            await table.pass_(key)

        response = await table.host("collect")
        await table.settle()

        assert response.status_code == 200, response.text
        game = await table.game()
        assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.COMBAT_WAIT)
        assert game.deadline_at is None  # таймера в бою нет, пока хост не нажмёт «Поторопить»
        state = await table.state()
        assert state.scene.type == SceneType.COMBAT
        assert state.combat.order == ["alice", "skeleton-1", "boris", "vera"]
        assert current_id(state) == "alice"
        opened = (await table.turns())[-1]
        assert (opened.kind, opened.status) == (TurnKind.COMBAT, TurnStatus.OPEN)
        assert table.notifier.texts_for(table.user_id("alice")) == ["Твой ход в бою"]
        assert table.notifier.texts_for(table.user_id("boris")) == []
        assert table.notifier.texts_for(table.user_id("vera")) == []


async def test_a_monster_who_goes_first_is_played_by_the_master_without_waiting(fight):
    # Скелет 20+3 ходит первым, Борис 1+3, Алиса 1+1, Вера 1+1: ждать нужно Бориса.
    scenario = [
        scenario_turn(
            tool("start_combat", monsters=[{"monster_id": "skeleton", "count": 1, "zone": "gate"}]),
            tool("chronicle", entry="Из могилы поднялся скелет."),
            say("Из могилы поднимается скелет."),
        ),
        scenario_turn(skeleton_strikes(1, "$pc1"), say("Скелет бросается на Алису.")),
    ]
    async with fight(None, scenario=scenario, rng=ScriptedRng([1, 1, 1, 20])) as table:
        for key in HEROES:
            await table.pass_(key)

        await table.host("collect")
        await table.settle()
        table.set_rng(FlatRng(1))

        state = await table.state()
        assert state.combat.order == ["skeleton-1", "boris", "alice", "vera"]
        # Ход первого скелета разобран без ожидания: игра дошла до первого героя.
        game = await table.game()
        assert game.turn_phase == TurnPhase.COMBAT_WAIT
        assert current_id(state) == "boris"
        logs = await table.logs()
        assert [(log.request["mode"], log.request["acting"]) for log in logs] == [
            ("round", None),
            ("combat", ["skeleton-1"]),
        ]
        statuses = [turn.status for turn in await table.turns()]
        assert statuses == [TurnStatus.DONE, TurnStatus.DONE, TurnStatus.OPEN]
        assert table.notifier.texts_for(table.user_id("boris")) == ["Твой ход в бою"]


async def test_the_scenario_plays_a_whole_fight_from_the_first_round_to_the_end_of_the_scene(
    fight,
):
    async with fight(None, rng=ScriptedRng([20, 1, 1, 5])) as table:
        for key in HEROES:
            await table.pass_(key)
        await table.host("collect")
        await table.settle()
        table.set_rng(FlatRng(1))
        assert await table.whose_turn() == "alice"

        for key, expected_next in (("alice", "boris"), ("boris", "vera"), ("vera", "alice")):
            response = await table.submit(key, f"{HEROES[key][0]} прикрывается щитом")
            assert response.status_code == 200, response.text
            await table.settle()
            assert await table.whose_turn() == expected_next

        # Второй круг: Алиса, а мастер объявляет конец сцены.
        await table.submit("alice", "Добить скелета")
        await table.settle()

        game = await table.game()
        assert (game.turn_phase, (await table.state()).combat) == (TurnPhase.DISCUSSION, None)
        assert [turn.status for turn in await table.turns()][:-1] == [TurnStatus.DONE] * 5
        feed = [item["text"] for item in await table.feed_of("alice")]
        assert any("Бой окончен" in text for text in feed)


async def test_a_fight_with_an_npc_names_him_in_every_live_event_and_snapshot_without_his_secrets(
    fight, adventure
):
    # Статблок Фабиана стоит в локации Фабиана: в порядке хода и в карточках бросков он «Фабиан»,
    # как в рассказе, а не «Фанатик культа». Имя доходит и в живых событиях хода мастера, и в
    # снимке; тайны NPC (секрет, знания) и его id не выходят ни там, ни там.
    # Причину броска (`data.reason`) пишет движок, и в ней бывает имя статблока («Инициатива:
    # Фанатик культа»): в карточке бойца-NPC вместо неё название броска, имени статблока нет нигде.
    fabian = adventure.npcs["fabian"]
    assert fabian.statblock == "cultist_fanatic" and fabian.location_id == "crypt"
    scenario = [
        scenario_turn(
            tool("start_combat", monsters=[{"monster_id": "cultist_fanatic", "count": 1}]),
            tool("chronicle", entry="Фабиан выхватил жезл."),
            say("Из тени выходит Фабиан."),
        )
    ]
    async with fight(None, location_id="crypt", scenario=scenario) as table:
        for key in HEROES:
            await table.pass_(key)

        await table.host("collect")
        await table.settle()

        state = await table.state()
        assert state.combat is not None
        assert state.combat.combatants["cultist_fanatic-1"].name == "Фанатик культа"
        live = [payload for payload, _audience in table.broadcaster.of_type("combat")]
        assert live, "бой начался, а события combat не ушло"
        async with table.db() as session:
            user = await session.get(User, table.user_id("boris"))
            snapshot = await snapshot_for(session, user, table.game_id, srd=table.srd)
        feed_rolls = [item for item in snapshot["feed"] if item["kind"] == "roll"]
        cards = [item for item in feed_rolls if item["data"].get("actor_id") == "cultist_fanatic-1"]
        assert cards, "карточки броска Фабиана в ленте нет"
        for card in cards:
            assert card["data"]["actor"] == "Фабиан" and card["text"].startswith("Фабиан:")
            assert card["data"]["reason"] == card["data"]["label"]
            dumped = json.dumps(card["data"], ensure_ascii=False)
            assert "Фанатик культа" not in card["text"] + dumped
        assert "Инициатива" in [card["data"]["label"] for card in cards]
        for label, payload in [("событие", live[-1]), ("снимок", snapshot)]:
            monsters = [f["name"] for f in payload["combat"]["order"] if f["kind"] == "monster"]
            assert monsters == ["Фабиан"], f"{label}: {monsters}"
            assert "Фанатик культа" not in json.dumps(payload["combat"], ensure_ascii=False)
        # Тайны NPC и его id не выходят ни в одном кадре и ни в снимке.
        everything = json.dumps([*live, snapshot], ensure_ascii=False)
        assert '"fabian"' not in everything
        for hidden in (fabian.secret, *fabian.knows):
            assert hidden not in everything


async def test_the_scenario_skeleton_engages_and_strikes_with_a_card_of_the_hero_armor_class(
    fight, srd
):
    # Очередь «Алиса, скелет, Борис, Вера»: скелет вовлекается в бой и бьёт героя, а не получает
    # от движка отказ; карточка атаки называет КД героя и не называет КД скелета.
    async with fight(None, rng=ScriptedRng([20, 1, 1, 5])) as table:
        for key in HEROES:
            await table.pass_(key)
        await table.host("collect")
        await table.settle()
        table.set_rng(FlatRng(1))

        await table.submit("alice", "Прикрыться щитом")
        await table.settle()

        state = await table.state()
        alice_ac = armor_class(state.party["alice"], srd)
        skeleton_ac = state.combat.combatants["skeleton-1"].ac
        assert alice_ac != skeleton_ac
        logs = await table.logs()
        assert logs[-1].request["acting"] == ["alice", "skeleton-1"]
        assert all(not call["is_error"] for call in logs[-1].trace), logs[-1].trace
        for key in HEROES:  # карточка общая: её видят все трое
            (strike,) = [
                item
                for item in await table.feed_of(key)
                if item["kind"] == "roll" and item["data"]["label"].startswith("Атака:")
            ]
            assert strike["data"]["actor"].startswith("Скелет")
            assert strike["data"]["target_ac"] == alice_ac
            assert f"против КД {alice_ac}" in strike["text"]
            assert str(skeleton_ac) not in strike["text"]


async def test_the_scenario_skeleton_that_goes_first_engages_the_hero_by_itself(fight, srd):
    # Ручной прогон со скелетом в начале очереди: сервер зовёт мастера за скелета без игрока.
    # Ход Алисы из сценария отказывает («не действует в этом вызове»), но вовлечение и удар скелета
    # проходят, и карточка атаки с КД героя есть в ленте.
    # Кубы: инициатива Алисы, Бориса, Веры и скелета, затем бросок атаки скелета (единица: промах).
    async with fight(None, rng=ScriptedRng([1, 1, 1, 20, 1])) as table:
        for key in HEROES:
            await table.pass_(key)
        await table.host("collect")
        await table.settle()
        table.set_rng(FlatRng(1))

        state = await table.state()
        assert state.combat.order[0] == "skeleton-1"
        (call_log,) = [log for log in await table.logs() if log.request["mode"] == "combat"]
        assert call_log.request["acting"] == ["skeleton-1"]
        assert [(call["name"], call["is_error"]) for call in call_log.trace] == [
            ("combat_action", True),
            ("move", False),
            ("monster_action", False),
            ("chronicle", False),
            ("server_end_turn", False),  # ход скелета закрывает сервер
        ]
        (strike,) = [
            item
            for item in await table.feed_of("boris")
            if item["kind"] == "roll" and item["data"]["label"].startswith("Атака:")
        ]
        assert strike["data"]["target_ac"] == armor_class(state.party["alice"], srd)


# --- ход героя и ход мастера ---

QUEUE = ["alice", "sk1", "boris", "vera"]


def round_actions(context: str) -> dict[str, dict[str, Any]]:
    """Ходы раунда из контекста мастера по id героя: `{"id", "name", "text", "marks"}`."""
    section = context.split("## Ходы этого раунда", 1)[1].split("\n## ", 1)[0]
    lines = (line.rstrip(",") for line in section.splitlines() if line.startswith('{"id"'))
    return {row["id"]: row for row in map(json.loads, lines)}


async def test_each_hero_and_the_monsters_after_him_move_once_and_no_turn_is_skipped(fight):
    scenario = [
        scenario_turn(dodge("$pc1"), skeleton_strikes(1, "$pc1"), say("Алиса уклоняется.")),
        scenario_turn(dodge("$pc2"), skeleton_strikes(2, "$pc2"), say("Борис ныряет под удар.")),
        scenario_turn(dodge("$pc3"), say("Вера прикрывается щитом.")),
    ]
    async with fight(["alice", "sk1", "boris", "sk2", "vera"], scenario=scenario) as table:
        for key, expected_next in (("alice", "boris"), ("boris", "vera"), ("vera", "alice")):
            response = await table.submit(key, "Прикрыться от ударов")
            await table.settle()
            assert response.status_code == 200, response.text
            assert await table.whose_turn() == expected_next

        logs = await table.logs()
        assert [log.request["acting"] for log in logs] == [
            ["alice", "skeleton-1"],
            ["boris", "skeleton-2"],
            ["vera"],
        ]
        # Каждый вызов инструмента принят: боец ходил в свой ход и никому ход не пропущен.
        assert all(not call["is_error"] for log in logs for call in log.trace)
        assert (await table.state()).combat.round == 2
        for key in HEROES:  # каждому герою пришло «Твой ход в бою», и только ему лично
            assert table.notifier.texts_for(table.user_id(key)) == ["Твой ход в бою"]


async def test_the_hero_turn_is_resolved_at_once_on_ready_without_the_hosts_button(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса уклоняется."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.draft("alice", "Прикрыться")
        response = await table.call("alice", "POST", "/action/submit")
        await table.settle()

        assert response.status_code == 200
        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT
        assert table.phases_sent()[0] == ("playing", "resolving")
        [first, second] = await table.turns()
        assert (first.kind, first.status) == (TurnKind.COMBAT, TurnStatus.DONE)
        assert (second.kind, second.status) == (TurnKind.COMBAT, TurnStatus.OPEN)
        assert second.number == first.number + 1


async def test_a_fallen_hero_is_skipped_with_a_death_save_in_the_same_commit_as_the_story(fight):
    scenario = [
        scenario_turn(dash("$pc1"), skeleton_strikes(1, "$pc1"), say("Скелет промахнулся."))
    ]
    # Первый куб — атака скелета (единица, промах), второй — спасбросок от смерти Бориса.
    async with fight(QUEUE, scenario=scenario, rng=ScriptedRng([1, 15]), down=["boris"]) as table:
        await table.submit("alice", "Прикрыться")
        await table.settle()

        assert await table.whose_turn() == "vera"
        assert (await table.state()).party["boris"].death_saves.successes == 1
        assert await table.snapshot_versions() == [1, 2]  # одна транзакция: один новый снимок
        feed = await table.feed_of("vera")
        saves = [
            i for i in feed if i["kind"] == "roll" and i["data"]["label"] == "Спасбросок от смерти"
        ]
        assert [(i["data"]["actor_id"], i["private"]) for i in saves] == [("boris", False)]
        story = [i for i in feed if i["kind"] == "narration"][-1]
        assert saves[0]["id"] < story["id"]  # броски идут в ленте до рассказа


async def test_the_rolls_the_server_makes_for_the_hand_over_are_in_the_journal(fight):
    scenario = [
        scenario_turn(dash("$pc1"), skeleton_strikes(1, "$pc1"), say("Скелет промахнулся."))
    ]
    async with fight(QUEUE, scenario=scenario, rng=ScriptedRng([1, 15]), down=["boris"]) as table:
        await table.submit("alice", "Прикрыться")
        await table.settle()

        [log] = await table.logs()
        assert [call["name"] for call in log.trace] == [
            "combat_action",
            "monster_action",
            "server_end_turn",
        ]
        server_call = log.trace[-1]
        assert not server_call["is_error"]
        assert "Спасбросок от смерти" in server_call["result"]


async def test_only_the_hero_whose_turn_it_is_may_write_and_the_others_get_a_refusal(fight):
    async with fight(QUEUE) as table:
        other = await table.draft("boris", "Я тоже хочу")
        mine = await table.draft("alice", "Бью скелета")

        assert other.status_code == 409
        assert mine.status_code == 200


async def test_a_hero_may_pass_his_turn_in_combat_and_the_master_sees_the_pass(fight, monkeypatch):
    provider = StubProvider(narration("Алиса выжидает."))
    use_provider(monkeypatch, provider)
    async with fight(QUEUE) as table:
        response = await table.pass_("alice")
        await table.settle()

        assert response.status_code == 200
        actions = round_actions(provider.requests[0].context)
        assert actions["alice"]["marks"] == ["пас"]
        assert await table.whose_turn() == "boris"


async def test_a_secret_hero_action_in_combat_is_seen_only_by_its_author(fight):
    scenario = [
        scenario_turn(
            tool("attack", actor_id="$pc1", target_id="skeleton-1", weapon="greatsword"),
            say("Алиса бьёт скелета."),
        )
    ]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.submit("alice", "Тихо ударить скелета", secret=True)
        await table.settle()

        def attack_rolls(feed: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [i for i in feed if i["kind"] == "roll" and "Атака" in i["data"]["label"]]

        assert len(attack_rolls(await table.feed_of("alice"))) == 1
        assert attack_rolls(await table.feed_of("boris")) == []
        assert attack_rolls(await table.feed_of("vera")) == []


async def test_the_initiative_of_a_secret_hero_reaches_everybody_and_his_own_check_only_him(fight):
    # Общая инициатива (B11.1) на проводе сервера: тайный ход Алисы начинает бой. Карточку её
    # инициативы видят все — в кадрах и в снимке, а её собственная проверка остаётся только ей.
    scenario = [
        scenario_turn(
            tool(
                "request_check",
                character_id="$pc1",
                ability="wis",
                skill="perception",
                dc=15,
                reason="прислушивается",
            ),
            tool("start_combat", monsters=[{"monster_id": "skeleton", "count": 1, "zone": "gate"}]),
            say("Из тумана выходит скелет."),
        )
    ]
    # Кубы: проверка Алисы 10, затем инициатива Алисы 20, Бориса 1, Веры 1 и скелета 5.
    async with fight(None, scenario=scenario, rng=ScriptedRng([10, 20, 1, 1, 5])) as table:
        await table.submit("alice", "Тихо подкрасться к воротам", secret=True)
        await table.pass_("boris")
        await table.pass_("vera")

        response = await table.host("collect")
        await table.settle()

        assert response.status_code == 200, response.text
        assert await table.whose_turn() == "alice"
        alice = table.user_id("alice")
        rolls = [
            (p["item"]["data"]["actor_id"], p["item"]["data"]["label"], who)
            for p, who in table.broadcaster.of_type("feed_item")
            if p["item"]["kind"] == "roll"
        ]
        initiative = sorted((actor, who) for actor, label, who in rolls if label == "Инициатива")
        assert initiative == sorted(
            [("alice", "all"), ("boris", "all"), ("vera", "all"), ("skeleton-1", "all")]
        )
        [(check_label, check_audience)] = [
            (label, who)
            for actor, label, who in rolls
            if actor == "alice" and label != "Инициатива"
        ]
        assert check_audience == alice

        def cards_of_alice(feed: list[dict[str, Any]]) -> set[str]:
            return {
                item["data"]["label"]
                for item in feed
                if item["kind"] == "roll" and item["data"].get("actor_id") == "alice"
            }

        assert cards_of_alice(await table.feed_of("alice")) == {"Инициатива", check_label}
        for other in ("boris", "vera"):
            assert cards_of_alice(await table.feed_of(other)) == {"Инициатива"}, other


async def test_collecting_and_closing_are_refused_in_combat(fight):
    async with fight(QUEUE) as table:
        for button in ("collect", "close", "extend"):
            assert (await table.host(button)).status_code == 409, button
        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT


# --- конец боя ---


async def test_a_won_fight_is_left_by_the_server_when_the_master_forgets_end_scene(fight):
    scenario = [
        scenario_turn(
            tool("attack", actor_id="$pc1", target_id="skeleton-1", weapon="greatsword"),
            say("Скелет рассыпается в прах."),
        )
    ]
    async with fight(QUEUE, scenario=scenario, rng=FlatRng(20), monster_hp=1) as table:
        await table.submit("alice", "Рубить скелета")
        await table.settle()

        game = await table.game()
        assert game.turn_phase == TurnPhase.DISCUSSION
        state = await table.state()
        assert state.combat is None
        assert state.scene.type == SceneType.EXPLORATION
        opened = (await table.turns())[-1]
        assert (opened.kind, opened.status) == (TurnKind.ROUND, TurnStatus.OPEN)
        systems = [m.text for m in await table.messages(MessageKind.SYSTEM)]
        assert systems.count("Бой окончен: отряд победил.") == 1
        for key in HEROES:
            assert table.notifier.texts_for(table.user_id(key)) == ["Игра продолжается"]


async def test_the_masters_own_end_of_the_scene_gives_one_message_and_no_second_closing(fight):
    scenario = [
        scenario_turn(
            tool("attack", actor_id="$pc1", target_id="skeleton-1", weapon="greatsword"),
            tool("end_scene", next="exploration", reason="Скелет уничтожен."),
            say("Скелет рассыпается в прах."),
        )
    ]
    async with fight(QUEUE, scenario=scenario, rng=FlatRng(20), monster_hp=1) as table:
        await table.submit("alice", "Рубить скелета")
        await table.settle()

        assert (await table.game()).turn_phase == TurnPhase.DISCUSSION
        systems = [m.text for m in await table.messages(MessageKind.SYSTEM)]
        assert systems.count("Бой окончен: отряд победил.") == 1


async def test_a_lost_fight_is_left_by_the_server_and_the_game_goes_on(fight):
    scenario = [scenario_turn(skeleton_strikes(1, "$pc1"), say("Скелет добивает Алису."))]
    async with fight(
        QUEUE,
        scenario=scenario,
        rng=FlatRng(20),
        down=["boris", "vera"],
        wounded={"alice": 1},
    ) as table:
        await table.submit("alice", "Стоять до конца")
        await table.settle()

        game = await table.game()
        assert game.turn_phase == TurnPhase.DISCUSSION
        state = await table.state()
        assert state.combat is None
        assert state.party["alice"].hp == 0
        systems = [m.text for m in await table.messages(MessageKind.SYSTEM)]
        assert systems.count("Бой окончен: отряд повержен.") == 1


# --- «Перегенерировать» и ошибки ---


async def test_regenerate_rolls_back_the_death_saves_of_the_hand_over_too(fight):
    scenario = [
        scenario_turn(dash("$pc1"), skeleton_strikes(1, "$pc1"), say("Первый рассказ.")),
        scenario_turn(dash("$pc1"), skeleton_strikes(1, "$pc1"), say("Второй рассказ.")),
    ]
    async with fight(QUEUE, scenario=scenario, rng=ScriptedRng([1, 15]), down=["boris"]) as table:
        await table.submit("alice", "Прикрыться")
        await table.settle()
        assert (await table.state()).party["boris"].death_saves.successes == 1
        # Бой ждёт следующего героя после разобранного хода: откатить можно, клиент видит кнопку.
        [*_, (waiting, _who)] = table.broadcaster.of_type("phase")
        assert (waiting["phase"]["turn_phase"], waiting["phase"]["can_regenerate"]) == (
            "combat_wait",
            True,
        )
        table.set_rng(ScriptedRng([1, 5]))  # промах скелета; на этот раз спасбросок провален

        response = await table.host("regenerate")
        await table.settle()

        assert response.status_code == 200, response.text
        saves = (await table.state()).party["boris"].death_saves
        assert (saves.successes, saves.failures) == (0, 1)  # прежний бросок откатан, а не сложился
        live = [
            m
            for m in await table.messages(MessageKind.ROLL)
            if not m.superseded and m.data["label"] == "Спасбросок от смерти"
        ]
        assert [m.data["total"] for m in live] == [5]
        assert await table.whose_turn() == "vera"


async def test_a_failed_fight_turn_leaves_the_queue_where_it_was_and_retry_repeats_it(
    fight, monkeypatch
):
    provider = StubProvider(ProviderError("модель недоступна"), narration("Скелет ударил."))
    use_provider(monkeypatch, provider)
    async with fight(QUEUE) as table:
        await table.submit("alice", "Прикрыться")
        await table.settle()

        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED
        assert await table.snapshot_versions() == [1]
        assert await table.whose_turn() == "alice"
        assert (await table.actions())["alice"].status == ActionStatus.SUBMITTED

        response = await table.host("retry")
        await table.settle()

        assert response.status_code == 200, response.text
        logs = await table.logs()
        assert [(log.request["mode"], log.request["acting"]) for log in logs] == [
            ("combat", ["alice", "skeleton-1"]),
            ("combat", ["alice", "skeleton-1"]),
        ]
        assert await table.whose_turn() == "boris"


async def test_retry_after_a_crash_recomputes_the_fighters_from_the_combat_of_the_state(
    fight, monkeypatch
):
    provider = StubProvider(narration("Скелет ударил."))
    use_provider(monkeypatch, provider)
    # Игра осталась в `resolving`: процесс умер во время боевого хода (запись восстановления несёт
    # `acting: null`).
    async with fight(QUEUE, turn_phase=TurnPhase.RESOLVING) as table:
        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED
        [crash] = await table.logs()
        assert crash.request["acting"] is None

        response = await table.host("retry")
        await table.settle()

        assert response.status_code == 200, response.text
        assert "Кто действует: alice, skeleton-1" in provider.requests[0].context
        assert (await table.logs())[-1].request["acting"] == ["alice", "skeleton-1"]
        assert await table.whose_turn() == "boris"


@pytest.mark.parametrize(
    ("order", "options", "expected"),
    [
        (["sk1", "alice", "boris", "vera"], {}, 200),
        (QUEUE, {}, 409),
    ],
    ids=["ход у монстра, который никто не начал", "ждёт герой"],
)
async def test_retry_in_combat_wait_starts_only_a_monster_turn_nobody_started(
    fight, order, options, expected
):
    scenario = [scenario_turn(skeleton_strikes(1, "$pc1"), say("Скелет бьёт Алису."))]
    async with fight(order, scenario=scenario, **options) as table:
        response = await table.host("retry")
        await table.settle()

        assert response.status_code == expected, response.text
        if expected == 200:
            assert [log.request["acting"] for log in await table.logs()] == [["skeleton-1"]]
            assert await table.whose_turn() == "alice"
        else:
            assert await table.logs() == []


async def test_the_masters_own_turns_in_a_row_are_limited_and_the_host_can_go_on(
    fight, monkeypatch
):
    monkeypatch.setattr(flow_module, "MAX_AUTO_TURNS", 2)
    scenario = [scenario_turn(skeleton_strikes(1, "alice"), say("Скелет бьёт Алису."), repeat=True)]
    # Все герои парализованы, но стоят на ногах: победителя нет, а ждать некого.
    async with fight(
        ["sk1", "alice", "boris", "vera"], scenario=scenario, paralyzed=["alice", "boris", "vera"]
    ) as table:
        await table.host("retry")
        await table.settle()

        assert len(await table.logs()) == 1 + 2  # ход и два подряд за ним
        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT
        assert master_moves_next(await table.state())

        await table.host("retry")
        await table.settle()

        assert len(await table.logs()) == 6


async def test_a_crash_of_the_hand_over_fails_the_attempt_and_changes_nothing(fight, monkeypatch):
    provider = StubProvider(narration("Алиса уклоняется."))
    use_provider(monkeypatch, provider)

    def broken(*_args: Any, **_kwargs: Any):
        raise RuntimeError("ошибка движка при передаче хода")

    monkeypatch.setattr(combat_flow.engine, "end_turn", broken)
    async with fight(QUEUE) as table:
        await table.submit("alice", "Прикрыться")
        await table.settle()

        assert (await table.game()).turn_phase == TurnPhase.DM_FAILED
        assert await table.snapshot_versions() == [1]
        [log] = await table.logs()
        assert log.error.startswith("RuntimeError")
        assert log.output is None
        assert await table.whose_turn() == "alice"


# --- «Поторопить» ---


async def test_hurry_starts_the_countdown_and_only_the_acting_hero_is_told(fight):
    async with fight(QUEUE) as table:
        response = await table.host("hurry")

        assert response.status_code == 200, response.text
        deadline = T0 + timedelta(seconds=HURRY_SECONDS)
        body = response.json()
        assert (body["turn_phase"], body["deadline_at"]) == ("combat_wait", deadline.isoformat())
        assert (await table.game()).deadline_at == deadline
        assert table.scheduler.pending() == {table.game_id: deadline}
        [text] = table.notifier.texts_for(table.user_id("alice"))
        assert str(HURRY_SECONDS) in text
        assert table.notifier.texts_for(table.user_id("boris")) == []
        assert ("playing", "combat_wait") in table.phases_sent()


@pytest.mark.parametrize(
    ("order", "options", "key", "expected"),
    [
        (QUEUE, {}, "boris", 403),
        (None, {}, "alice", 409),
        (["sk1", "alice", "boris", "vera"], {}, "alice", 409),
        (QUEUE, {"turn_phase": TurnPhase.DM_FAILED}, "alice", 409),
    ],
    ids=["не хост", "обсуждение, а не бой", "ходит мастер", "мастер не справился"],
)
async def test_hurry_is_refused_unless_the_host_hurries_a_hero_who_thinks(
    fight, order, options, key, expected
):
    async with fight(order, **options) as table:
        response = await table.host("hurry", key=key)

        assert response.status_code == expected, response.text
        assert table.scheduler.pending() == {}
        assert (await table.game()).deadline_at is None


async def test_hurry_cannot_be_pressed_again_while_the_countdown_goes(fight):
    async with fight(QUEUE) as table:
        assert (await table.host("hurry")).status_code == 200

        again = await table.host("hurry")

        assert again.status_code == 409
        assert table.scheduler.pending() == {table.game_id: T0 + timedelta(seconds=HURRY_SECONDS)}


async def test_when_the_countdown_ends_an_empty_turn_becomes_a_dodge(fight):
    scenario = [scenario_turn(dodge("$pc1"), skeleton_strikes(1, "$pc1"), say("Алиса уклоняется."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.host("hurry")

        await table.scheduler.advance(HURRY_SECONDS - 1)
        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT
        assert len(table.app.state.turn_tasks) == 0
        await table.scheduler.advance(1)
        await table.settle()

        row = (await table.actions())["alice"]
        assert (row.status, row.text) == (ActionStatus.AUTO_SUBMITTED, DODGE_TEXT)
        assert await table.whose_turn() == "boris"
        feed = await table.feed_of("boris")
        assert [i["text"] for i in feed if i["kind"] == "action"] == [DODGE_TEXT]
        [log] = await table.logs()
        assert f'"text": "{DODGE_TEXT}"' in log.request["context"]
        assert table.scheduler.pending() == {}
        # «Уклоняется» игрок не писал: «Ход сдан» ему не приходит (дополнение B11 §1 п. 5).
        assert table.broadcaster.of_type("action_auto_submitted") == []


async def test_when_the_countdown_ends_the_draft_of_the_hero_is_submitted_as_it_is(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса делает что может."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.draft("alice", "Бью скелета мечом")
        await table.host("hurry")

        await table.scheduler.advance(HURRY_SECONDS)
        await table.settle()

        row = (await table.actions())["alice"]
        assert (row.status, row.text) == (ActionStatus.AUTO_SUBMITTED, "Бью скелета мечом")
        assert await table.whose_turn() == "boris"
        # Черновик с текстом сдан отсчётом: «Ход сдан» — только Алисе и без текста хода.
        assert table.broadcaster.of_type("action_auto_submitted") == [
            ({"type": "action_auto_submitted"}, table.user_id("alice"))
        ]


async def test_a_hero_who_is_ready_before_the_countdown_ends_stops_it(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса уклоняется."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.host("hurry")

        await table.submit("alice", "Прикрыться")
        await table.settle()
        assert table.scheduler.pending() == {}
        await table.scheduler.advance(10 * HURRY_SECONDS)
        await table.settle()

        assert len(await table.logs()) == 1
        assert await table.whose_turn() == "boris"


async def test_the_pause_freezes_the_hurry_countdown_and_resume_gives_back_the_rest(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса уклоняется."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.host("hurry")
        await table.scheduler.advance(20)

        await table.host("pause")
        await table.scheduler.advance(1000)
        await table.settle()
        assert await table.logs() == []

        await table.host("resume")
        rest = table.scheduler.now() + timedelta(seconds=40)
        assert table.scheduler.pending() == {table.game_id: rest}
        await table.scheduler.advance(39)
        await table.settle()
        assert await table.logs() == []
        await table.scheduler.advance(1)
        await table.settle()

        assert len(await table.logs()) == 1


async def test_after_a_restart_an_expired_hurry_countdown_closes_at_once(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса уклоняется."))]
    expired = {"deadline_at": T0 - timedelta(seconds=5)}
    async with fight(QUEUE, scenario=scenario, game_fields=expired) as table:
        await table.scheduler.advance(0)
        await table.settle()

        row = (await table.actions())["alice"]
        assert (row.status, row.text) == (ActionStatus.AUTO_SUBMITTED, DODGE_TEXT)
        assert await table.whose_turn() == "boris"


async def test_a_countdown_of_an_earlier_turn_closes_nothing(fight):
    scenario = [scenario_turn(dodge("$pc1"), say("Алиса уклоняется."))]
    async with fight(QUEUE, scenario=scenario) as table:
        await table.host("hurry")
        await table.submit("alice", "Прикрыться")
        await table.settle()
        assert await table.whose_turn() == "boris"
        # У Бориса срок уже вышел, но опоздавший отсчёт принадлежит прошлому ходу Алисы.
        await table.update_game(deadline_at=T0 - timedelta(seconds=1))

        await flow_module.on_deadline(
            flow_module.Flow.from_app(table.app), table.game_id, table.setup.turn_id
        )

        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT
        assert await table.actions((await table.turns())[-1].id) == {}
        assert len(await table.logs()) == 1


@pytest.mark.parametrize("hurried", [False, True], ids=["отсчёта нет", "срок ещё не вышел"])
async def test_a_countdown_that_fires_before_its_time_closes_nothing(fight, hurried):
    async with fight(QUEUE) as table:
        if hurried:
            await table.host("hurry")

        await flow_module.on_deadline(
            flow_module.Flow.from_app(table.app), table.game_id, table.setup.turn_id
        )
        await table.settle()

        assert await table.actions() == {}
        assert await table.logs() == []
        assert (await table.game()).turn_phase == TurnPhase.COMBAT_WAIT


# --- шов хода мастера ---


async def test_a_fight_turn_without_named_fighters_takes_them_from_the_state(fight, monkeypatch):
    provider = StubProvider(narration("Скелет ударил."))
    use_provider(monkeypatch, provider)
    async with fight(QUEUE) as table:
        outcome = await run_dm_turn(
            table.game_id, mode=TurnKind.COMBAT, deps=Deps.from_app(table.app)
        )

        assert outcome.status == TurnStatus.DONE
        [log] = await table.logs()
        assert log.request["acting"] == ["alice", "skeleton-1"]
        assert "Кто действует: alice, skeleton-1" in provider.requests[0].context


async def test_the_hand_over_and_the_story_are_one_commit(fight, monkeypatch):
    use_provider(monkeypatch, StubProvider(narration("Алиса уклоняется.")))
    async with fight(QUEUE) as table:
        outcome = await run_dm_turn(
            table.game_id, mode=TurnKind.COMBAT, deps=Deps.from_app(table.app)
        )

        assert outcome.state_version == 2
        assert await table.snapshot_versions() == [1, 2]
        # Модель не вызывала инструментов, поэтому сервер сам довёл ход до Бориса.
        assert await table.whose_turn() == "boris"
        assert (await table.game()).state_version == 2


async def test_a_failed_attempt_leaves_no_hand_over_behind(fight, monkeypatch):
    use_provider(monkeypatch, StubProvider(ProviderError("нет связи")))
    async with fight(QUEUE, down=["boris"], rng=ScriptedRng([])) as table:
        outcome = await run_dm_turn(
            table.game_id, mode=TurnKind.COMBAT, deps=Deps.from_app(table.app)
        )

        assert outcome.status == TurnStatus.FAILED
        assert await table.snapshot_versions() == [1]
        assert await table.whose_turn() == "alice"
        assert await table.messages(MessageKind.ROLL) == []
        assert (await table.state()).party["boris"].death_saves.successes == 0
