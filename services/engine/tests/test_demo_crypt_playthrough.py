"""«Склеп должника» через инструменты мастера: сквозная партия и обход приключения (B4.4–B4.7).

Партию ведёт `build_registry`, как её будет вести мастер: каждый ход — копия состояния, вызовы
инструментов по порядку, коммит через JSON (так сервер хранит игру). Кубы заданы `ScriptedRng`, а
после вызова в очереди не должно остаться ни одного лишнего значения: инструмент бросил ровно то,
что нужно. Отряд проходит всё приключение: деревня, ключ от Марты, скрытый ход в склеп, сундук и
зелье, финальный бой с Фабианом и конец сцены.

Вторая половина файла сверяет данные приключения с инструментами для любого приключения, которое
принял валидатор (`demo-crypt` и приключение из фабрики): каждый выход проходим, каждая улика
находима, каждый бой начинается, а каждая Сл из файлов подходит инструменту проверки.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import pytest
from adventure_factory import adventure_data, make_adventure
from pydantic import TypeAdapter
from tool_calls import ALLY, HERO

from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import validate
from dnd_engine.character import Character, InventoryItem
from dnd_engine.checks import VALID_DCS
from dnd_engine.content import SrdContent
from dnd_engine.events import (
    ChronicleEvent,
    CombatEvent,
    DamageEvent,
    Event,
    ItemEvent,
    MoveEvent,
    PrivateMessageEvent,
    RevealEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef, SceneType
from dnd_engine.tools.registry import ToolContext, ToolOutcome, ToolRegistry

EVENTS = TypeAdapter(list[Event])

# --- стол: партия по ходам мастера ---


class Call(NamedTuple):
    tool: str
    args: dict[str, Any]
    rolls: tuple[int, ...] = ()


def call(tool: str, /, *rolls: int, **args: Any) -> Call:
    """`call("attack", 15, 6, 6, actor_id=...)`: инструмент, его аргументы и кубы, что выпадут."""
    return Call(tool, args, rolls)


def dice_left(rng: ScriptedRng) -> bool:
    try:
        rng.randint(1, 100)
    except ValueError:
        return False
    return True


class Table:
    """Игра за столом: зафиксированное состояние и события всех ходов мастера по порядку."""

    def __init__(
        self,
        state: GameState,
        registry: ToolRegistry,
        adventure: Adventure,
        srd: SrdContent,
    ) -> None:
        self.state, self.events = state, []
        self.registry, self.adventure, self.srd = registry, adventure, srd

    def context(self, working: GameState, **fields: Any) -> ToolContext:
        return ToolContext(
            state=working,
            adventure=self.adventure,
            content=self.srd,
            rules=build_ruleset(working.rules),
            rng=ScriptedRng([]),
            **fields,
        )

    def turn(
        self,
        *calls: Call,
        acting: tuple[str, ...] = (),
        secret: tuple[str, ...] = (),
        inspired: tuple[str, ...] = (),
    ) -> list[ToolOutcome]:
        """Один ход мастера: все вызовы удались, кубов потрачено ровно столько, сколько задано."""
        working = self.state.model_copy(deep=True)
        ctx = self.context(
            working, acting=acting, secret_actors=set(secret), inspired=set(inspired)
        )
        outcomes = []
        for step in calls:
            ctx.rng = ScriptedRng(step.rolls)
            outcome = self.registry.call(ctx, step.tool, step.args)
            assert outcome.error is None, f"{step.tool}: {outcome.error}"
            assert not dice_left(ctx.rng), f"{step.tool}: в очереди остались лишние кубы"
            outcomes.append(outcome)
        # Коммит, как у сервера: состояние уходит в базу JSON-ом и читается обратно.
        self.state = GameState.model_validate_json(working.model_dump_json())
        assert self.state == working
        self.events += ctx.events
        return outcomes

    def refuse(self, step: Call, because: str, *, acting: tuple[str, ...] = ()) -> ToolOutcome:
        """Вызов, который движок отклоняет: ошибка модели, состояние и события не тронуты."""
        working = self.state.model_copy(deep=True)
        ctx = self.context(working, acting=acting)
        ctx.rng = ScriptedRng(step.rolls)

        outcome = self.registry.call(ctx, step.tool, step.args)

        assert outcome.result is None and outcome.error, f"{step.tool} должен быть отклонён"
        assert because in outcome.error, outcome.error
        assert working == self.state and ctx.events == []
        assert dice_left(ctx.rng) == bool(step.rolls), "отказ потратил кубы"
        return outcome


# --- сама партия ---


@dataclass
class Played:
    """Сыгранное приключение: стол и итоги вызовов по этапам."""

    table: Table
    stages: dict[str, list[ToolOutcome]]


@pytest.fixture
def play(
    tool_registry: ToolRegistry,
    demo_adventure: Adventure,
    srd: SrdContent,
    make_game: Callable[..., GameState],
    make_fighter: Callable[..., Character],
) -> Callable[[], Played]:
    def run_the_whole_adventure() -> Played:
        greatsword = [InventoryItem(item_id="greatsword", qty=1, equipped=True)]
        state = make_game(
            make_fighter(inventory=greatsword),
            make_fighter(id=ALLY, name="Лира", inventory=greatsword, inspiration=True),
        )
        table = Table(state, tool_registry, demo_adventure, srd)
        stages: dict[str, list[ToolOutcome]] = {}

        # Деревня: оглядеться, поговорить с Мартой, получить ключ (сундук на кладбище пока заперт).
        stages["village"] = table.turn(
            call("get_location"),
            call("get_npc", npc_id="marta"),
            call("search_lore", query="долг"),
            call("get_location", location_id="cemetery"),
            call("give_item", item_id="chest-key", character_id=HERO),
            call("get_location", location_id="cemetery"),
            call("change_attitude", npc_id="marta", delta=1),
            call("chronicle", entry="Марта доверила отряду запасной ключ от сундука брата."),
        )

        # Кладбище: люк в склеп спрятан, пока его не найдут.
        stages["arrival"] = table.turn(
            call("move_party", location_id="cemetery"),
            call("get_location"),
            call("chronicle", entry="Отряд пришёл на кладбище."),
            call("get_item", item_id="healing-potion"),
        )
        table.refuse(call("move_party", location_id="crypt"), "скрыт")
        # Ключ у отряда, но сундук ещё закрыт: зелье из него в обход `reveal` не достать.
        table.refuse(
            call("give_item", item_id="healing-potion", character_id=HERO), "feature:chest"
        )

        # Лира тайно идёт по следам с вдохновением (два куба, берётся лучший), находит люк и ключом
        # открывает сундук; зелье из него отдаётся Брону.
        stages["search"] = table.turn(
            call(
                "request_check",
                8,
                14,
                character_id=ALLY,
                ability="wis",
                skill="survival",
                dc=10,
                reason="Идёт по следам волочения",
            ),
            call("reveal", ref="feature:tracks"),
            call("private_message", character_id=ALLY, text="Следы ведут к мшистой плите."),
            call("reveal", ref="feature:chest"),
            call("give_item", item_id="healing-potion", character_id=HERO),
            call("get_location"),
            call("chronicle", entry="Лира нашла люк в склеп и открыла сундук."),
            secret=(ALLY,),
            inspired=(ALLY,),
        )

        # Склеп: уговорить Фабиана не вышло, начинается бой (порядок: Брон, Лира, скелеты, Фабиан).
        stages["crypt"] = table.turn(
            call("move_party", location_id="crypt", via_exit="to-crypt"),
            call("get_location"),
            call(
                "request_check",
                4,
                character_id=HERO,
                ability="cha",
                skill="persuasion",
                dc=15,
                reason="Уговаривает Фабиана сдаться",
            ),
            call("chronicle", entry="Фабиан не стал слушать."),
        )
        stages["fight"] = table.turn(  # инициатива: Брон 20, Лира 15, скелеты и Фабиан по 1 на к20
            call("start_combat", 20, 15, 1, 1, 1, encounter_id="fabian-fight"),
            call("get_location"),
        )

        # Раунд 1. Ход Брона: сам по себе, дальше ходит Лира и идущие за ней монстры.
        table.refuse(call("move_party", location_id="cemetery"), "В бою")
        # Очередь acting называет сервер: без неё в бою не действует никто.
        table.refuse(
            call("attack", 15, 6, 6, actor_id=HERO, target_id="skeleton-1", weapon="greatsword"),
            "никто не действует",
        )
        stages["round 1 Брон"] = table.turn(
            call("move", actor_id=HERO, to_zone="hall", engage="skeleton-1"),
            call("attack", 15, 6, 6, actor_id=HERO, target_id="skeleton-1", weapon="greatsword"),
            acting=(HERO,),
        )
        everyone_after_lira = (ALLY, "skeleton-1", "skeleton-2", "cultist_fanatic-1")
        table.refuse(  # Фабиан ходит после Лиры, а Лира ещё не походила
            call(
                "monster_action",
                12,
                4,
                3,
                3,
                monster_id="cultist_fanatic-1",
                action_id="pact_blade",
                target_ids=[HERO],
            ),
            "сначала ходит Лира",
            acting=everyone_after_lira,
        )
        stages["round 1 Лира и враги"] = table.turn(
            call("move", actor_id=ALLY, to_zone="hall", engage="skeleton-2"),
            call("attack", 15, 6, 6, actor_id=ALLY, target_id="skeleton-2", weapon="greatsword"),
            call("move", actor_id="cultist_fanatic-1", to_zone="hall", engage=HERO),
            call(
                "monster_action",
                12,
                4,
                3,
                3,
                monster_id="cultist_fanatic-1",
                action_id="pact_blade",
                target_ids=[HERO],
            ),
            acting=everyone_after_lira,
        )

        # Раунд 2: Брон бьёт Фабиана, Лира вступает в ближний бой и бьёт тоже, Фабиан отвечает ей.
        stages["round 2 Брон"] = table.turn(
            call(
                "attack",
                15,
                6,
                6,
                actor_id=HERO,
                target_id="cultist_fanatic-1",
                weapon="greatsword",
            ),
            acting=(HERO,),
        )
        stages["round 2 Лира и Фабиан"] = table.turn(
            call("move", actor_id=ALLY, engage="cultist_fanatic-1"),
            call(
                "attack",
                15,
                6,
                6,
                actor_id=ALLY,
                target_id="cultist_fanatic-1",
                weapon="greatsword",
            ),
            call(
                "monster_action",
                10,
                5,
                2,
                2,
                monster_id="cultist_fanatic-1",
                action_id="pact_blade",
                target_ids=[ALLY],
            ),
            acting=(ALLY, "cultist_fanatic-1"),
        )

        # Раунд 3: последний удар, затем мастер закрывает сцену.
        stages["round 3 Брон"] = table.turn(
            call(
                "attack",
                15,
                6,
                6,
                actor_id=HERO,
                target_id="cultist_fanatic-1",
                weapon="greatsword",
            ),
            acting=(HERO,),
        )
        stages["ending"] = table.turn(
            call("end_scene", next="exploration", reason="Фабиан повержен, скелеты рассыпались"),
            call("get_location"),
            call("chronicle", entry="Фабиан повержен, склеп затих."),
        )
        return Played(table, stages)

    return run_the_whole_adventure


def test_the_party_walks_from_the_village_to_the_crypt_by_the_story(play):
    played = play()
    state = played.table.state

    assert state.location_id == "crypt"
    assert state.world.visited_locations == ["village", "cemetery", "crypt"]
    assert state.world.item_holders == {
        "chest-key": f"character:{HERO}",
        "healing-potion": f"character:{HERO}",
    }
    assert {"feature:tracks", "clue:clue-tracks", "exit:to-crypt", "feature:chest"} <= set(
        state.world.revealed
    )
    assert state.world.npc_attitude["marta"] == played.table.adventure.npcs["marta"].attitude + 1


def test_the_chest_is_locked_until_the_key_is_given_and_the_hidden_exit_is_found(play):
    stages = play().stages

    def chest(view: dict[str, Any]) -> dict[str, Any]:
        return next(f for f in view["features"] if f["id"] == "chest")

    def to_crypt(view: dict[str, Any]) -> dict[str, Any]:
        return next(e for e in view["exits"] if e["id"] == "to-crypt")

    before_key, after_key = stages["village"][3].result, stages["village"][5].result
    assert chest(before_key)["status"] == "locked" and chest(after_key)["status"] == "open"
    assert to_crypt(stages["arrival"][1].result)["status"] == "hidden"
    assert to_crypt(stages["search"][5].result)["status"] == "open"
    assert stages["search"][3].result["items"] == [
        {"id": "healing-potion", "name": "Зелье лечения"}
    ]


def test_the_potion_lies_inside_the_chest_until_the_chest_is_opened(play):
    stages = play().stages

    assert stages["arrival"][3].result["inside"] == "feature:chest"
    assert stages["search"][4].result["to"] == f"character:{HERO}"  # зелье отдано после reveal


def test_inspiration_gives_the_secret_search_two_dice_and_the_better_one_counts(play):
    survival = play().stages["search"][0].result

    assert survival["natural"] == 14 and survival["advantage"] == "advantage"
    assert survival["inspiration_spent"] is True and survival["success"] is True


def test_persuasion_of_fabian_fails_and_the_encounter_waits_in_the_description(play):
    stages = play().stages

    assert stages["crypt"][2].result["success"] is False
    encounter = stages["crypt"][1].result["encounter"]
    assert (encounter["id"], encounter["status"]) == ("fabian-fight", "pending")
    assert stages["fight"][1].result["encounter"]["status"] == "active"


def test_the_final_battle_goes_round_by_round_to_the_victory_of_the_party(play):
    played = play()
    state = played.table.state
    stages = played.stages

    start = stages["fight"][0].result
    assert [fighter["id"] for fighter in start["order"]] == [
        HERO,
        ALLY,
        "skeleton-1",
        "skeleton-2",
        "cultist_fanatic-1",
    ]
    assert stages["round 1 Брон"][1].result["events"][-1]["target"] == "skeleton-1"
    assert stages["round 3 Брон"][0].result["winner"] == "party"  # мастер видит: пора на end_scene
    # Фабиан получил 3 удара по 15 из 44 HP; герои потеряли столько, сколько нанесли клинки.
    assert state.party[HERO].hp == 30 - 12 and state.party[ALLY].hp == 30 - 11
    assert state.combat is None and state.scene.type == SceneType.EXPLORATION
    assert state.world.flags["encounter:fabian-fight"] == "done"
    assert state.world.npc_alive["fabian"] is False  # павший статблок отмечает NPC
    ending = stages["ending"]
    assert ending[0].result["winner"] == "party"
    assert ending[1].result["encounter"] is None
    assert [npc["alive"] for npc in ending[1].result["npcs"]] == [False]


def test_the_events_of_the_whole_game_tell_the_same_story(play):
    events = play().table.events

    moves = [e.location_id for e in events if isinstance(e, MoveEvent)]
    assert moves == ["cemetery", "crypt"]
    assert [e.ref for e in events if isinstance(e, RevealEvent)] == [
        "feature:tracks",
        "clue:clue-tracks",
        "exit:to-crypt",
        "feature:chest",
    ]
    assert [
        (e.item_id, e.holder_from, e.holder_to) for e in events if isinstance(e, ItemEvent)
    ] == [
        ("chest-key", "npc:marta", f"character:{HERO}"),
        ("healing-potion", "location:cemetery", f"character:{HERO}"),
    ]
    assert CombatEvent(phase="started") in events
    assert CombatEvent(phase="ended", winner="party") in events
    assert SceneEvent(scene=SceneType.EXPLORATION) in events
    assert events[-1] == ChronicleEvent(
        text="Фабиан повержен, склеп затих."
    )  # запись хода — последняя
    killed = [e.target_id for e in events if isinstance(e, DamageEvent) and e.hp_after == 0]
    assert killed == ["skeleton-1", "skeleton-2", "cultist_fanatic-1"]


def test_only_lira_secret_search_and_message_are_hidden_from_the_others(play):
    events = play().table.events

    hidden = [e for e in events if isinstance(e, RollEvent) and e.audience != "all"]
    assert [(e.actor_id, e.audience, e.label) for e in hidden] == [
        (ALLY, ALLY, "Мудрость (Выживание)")
    ]
    public = [e for e in events if isinstance(e, RollEvent) and e.audience == "all"]
    assert len(public) > 10  # уговоры, инициатива, удары и урон видят все
    assert [e.audience for e in events if isinstance(e, PrivateMessageEvent)] == [ALLY]


def test_no_event_of_the_game_gives_away_what_only_the_master_may_know(play):
    played = play()
    table = played.table
    story = EVENTS.dump_json(table.events).decode()

    secrets = [npc.secret for npc in table.adventure.npcs.values() if npc.secret]
    secrets += [location.dm_notes for location in table.adventure.locations.values()]
    assert secrets, "в приключении должны быть тексты только для мастера"
    for text in secrets:
        assert text[:40] not in story


def test_the_events_and_the_state_of_the_game_can_be_stored_and_read_back(play):
    table = play().table

    assert EVENTS.validate_json(EVENTS.dump_json(table.events)) == table.events
    assert GameState.model_validate_json(table.state.model_dump_json()) == table.state


def test_the_whole_game_leaves_the_adventure_and_the_srd_data_as_they_were(
    play, demo_adventure, srd
):
    adventure_before, srd_before = demo_adventure.model_dump_json(), srd.model_dump_json()

    play()

    assert demo_adventure.model_dump_json() == adventure_before
    assert srd.model_dump_json() == srd_before


def test_the_chronicle_has_one_entry_per_turn_that_wrote_it(play):
    state = play().table.state

    assert state.chronicle.entries == [
        "Марта доверила отряду запасной ключ от сундука брата.",
        "Отряд пришёл на кладбище.",
        "Лира нашла люк в склеп и открыла сундук.",
        "Фабиан не стал слушать.",
        "Фабиан повержен, склеп затих.",
    ]


def test_the_game_is_repeatable_from_the_same_script(play):
    first, second = play(), play()

    assert first.table.state == second.table.state
    assert first.table.events == second.table.events


# --- приключение, которое принял валидатор, играбельно целиком ---


@pytest.fixture(params=["demo-crypt", "фабричное"])
def adventure(request, demo_adventure: Adventure, srd: SrdContent) -> Adventure:
    found = demo_adventure if request.param == "demo-crypt" else make_adventure()
    assert not validate(found, srd).blocking  # дальше проверяется то, что валидатор пропустил
    return found


@pytest.fixture
def table_at(
    tool_registry: ToolRegistry,
    srd: SrdContent,
    adventure: Adventure,
    make_game: Callable[..., GameState],
) -> Callable[[str], Table]:
    """Стол с двумя героями в нужной локации приключения."""

    def build(location_id: str) -> Table:
        state = make_game(
            location_id=location_id,
            adventure_id=adventure.id,
            rules=RulesRef(modules=list(adventure.rules.modules)),
        )
        return Table(state, tool_registry, adventure, srd)

    return build


def open_the_lock(table: Table, locked_by: str) -> None:
    """Открывает замок так, как открыл бы отряд: ключ в руки героя или флаг сюжета."""
    kind, _, name = locked_by.partition(":")
    if kind == "item":
        table.state.world.item_holders[name] = f"character:{HERO}"  # ключ нашли и взяли
    else:
        table.turn(call("set_flag", name=name))


def view_of(table: Table, location_id: str | None = None) -> dict[str, Any]:
    args = {} if location_id is None else {"location_id": location_id}
    [outcome] = table.turn(call("get_location", **args))
    return outcome.result


def test_every_location_description_matches_the_data_of_the_adventure(adventure, table_at):
    table = table_at(adventure.start_location)

    for location in adventure.locations.values():
        view = view_of(table, location.id)

        assert view["name"] == location.name and view["read_aloud"] == location.read_aloud
        assert [e["id"] for e in view["exits"]] == [e.id for e in location.exits]
        assert [f["id"] for f in view["features"]] == [f.id for f in location.features]
        assert {n["id"] for n in view["npcs"]} == {
            n.id for n in adventure.npcs.values() if n.location_id == location.id
        }
        assert (view["encounter"] or {}).get("id") == location.encounter_id
        for shown, exit_ in zip(view["exits"], location.exits, strict=True):
            hidden = exit_.hidden_dc is not None
            assert shown["status"] == (
                "hidden" if hidden else "locked" if exit_.locked_by else "open"
            )


def test_every_npc_and_item_can_be_read_with_the_state_the_data_promises(adventure, table_at):
    table = table_at(adventure.start_location)

    for npc in adventure.npcs.values():
        [outcome] = table.turn(call("get_npc", npc_id=npc.id))
        assert (outcome.result["statblock"], outcome.result["alive"]) == (npc.statblock, True)
        assert outcome.result["location_id"] == npc.location_id
    for item in adventure.items.values():
        [outcome] = table.turn(call("get_item", item_id=item.id))
        assert outcome.result["holder"] == item.holder


def test_the_lore_search_finds_every_location_npc_and_item_by_its_name(adventure, table_at):
    table = table_at(adventure.start_location)
    named = {f"location:{x.id}": x.name for x in adventure.locations.values()}
    named |= {f"npc:{x.id}": x.name for x in adventure.npcs.values()}
    named |= {f"item:{x.id}": x.name for x in adventure.items.values()}

    for ref, name in named.items():
        [outcome] = table.turn(call("search_lore", query=name, limit=20))

        assert ref in [hit["ref"] for hit in outcome.result["results"]], (ref, name)


def test_every_exit_can_be_walked_once_the_party_has_found_it_and_opened_it(adventure, table_at):
    for location in adventure.locations.values():
        for exit_ in location.exits:
            table = table_at(location.id)
            walk = call("move_party", location_id=exit_.to, via_exit=exit_.id)
            if exit_.hidden_dc is not None:
                table.refuse(walk, "скрыт")
                table.turn(call("reveal", ref=f"exit:{exit_.id}"))
            if exit_.locked_by is not None:
                table.refuse(walk, "заперт")
                open_the_lock(table, exit_.locked_by)

            [moved] = table.turn(walk)

            assert moved.result["location_id"] == exit_.to
            assert table.state.location_id == exit_.to


def test_every_clue_of_every_revelation_can_be_found_through_the_tools(adventure, table_at):
    for revelation in adventure.revelations.values():
        for clue in revelation.clues:
            kind, _, target = clue.where.partition(":")
            if kind == "feature":
                place = next(
                    loc
                    for loc in adventure.locations.values()
                    if any(f.id == target for f in loc.features)
                )
                table = table_at(place.id)
                lock = next(f for f in place.features if f.id == target).locked_by
                if lock is not None:
                    open_the_lock(table, lock)
            elif kind == "npc":
                table = table_at(adventure.npcs[target].location_id)
            else:
                table = table_at(adventure.start_location)
                table.state.world.item_holders[target] = f"character:{HERO}"

            [found] = table.turn(call("reveal", ref=f"clue:{clue.id}"))

            assert f"clue:{clue.id}" in table.state.world.revealed, clue.id
            assert found.result["clues"] == [{"id": clue.id, "description": clue.description}]


def test_every_encounter_starts_where_it_is_with_its_monsters_in_its_zones(adventure, table_at):
    for encounter in adventure.encounters.values():
        table = table_at(encounter.location_id)
        monsters = sum(group.count for group in encounter.monsters)

        [started] = table.turn(
            call("start_combat", 20, 15, *[1] * monsters, encounter_id=encounter.id)
        )

        combat = table.state.combat
        assert started.result["encounter"] == encounter.id
        assert table.state.world.flags[f"encounter:{encounter.id}"] == "active"
        assert combat.combatants[HERO].zone_id == encounter.pc_zone
        for group in encounter.monsters:
            zones = [f.zone_id for f in combat.combatants.values() if f.ref == group.monster_id]
            assert zones == [group.zone] * group.count


def test_every_dc_written_in_the_adventure_is_a_dc_the_check_tool_accepts(adventure, table_at):
    table = table_at(adventure.start_location)
    checks = [
        (f.check.ability.value, f.check.skill and f.check.skill.value, f.check.dc)
        for location in adventure.locations.values()
        for f in location.features
        if f.check
    ]
    checks += [
        ("wis", "perception", e.hidden_dc)
        for location in adventure.locations.values()
        for e in location.exits
        if e.hidden_dc
    ]
    assert checks, "в приключении должны быть проверки"

    for ability, skill, dc in checks:
        assert dc in VALID_DCS
        args = {"character_id": HERO, "ability": ability, "dc": dc, "reason": "т"}
        if skill:
            args["skill"] = skill

        [outcome] = table.turn(call("request_check", 12, **args))

        assert outcome.result["dc"] == dc


# Правки чистого приключения, после которых валидатор молчит. Бой, который он принял, должен
# начинаться (так сказано в `tools/combat.py` у `MAX_MONSTERS`): модель мастера не может исправить
# данные приключения, и неначинающийся бой остановил бы игру.
def two_groups_of_one_monster(data: dict[str, Any]) -> None:
    groups = [
        {"monster_id": "ghoul", "count": 2, "zone": "here"},
        {"monster_id": "ghoul", "count": 3, "zone": "here"},
    ]
    data["encounters"]["bones"]["monsters"] = groups


def the_most_monsters_allowed(data: dict[str, Any]) -> None:
    data["encounters"]["bones"]["monsters"][0]["count"] = 20


def monsters_in_a_zone_without_a_link(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["zones"] = [
        {"id": "here", "name": "здесь"},
        {"id": "yard", "name": "двор"},
    ]
    data["encounters"]["bones"]["monsters"][0]["zone"] = "yard"


def a_zone_linked_to_itself(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["links"] = [{"a": "here", "b": "here", "distance": "near"}]


def two_zones_with_one_id(data: dict[str, Any]) -> None:
    data["locations"]["cemetery"]["zones"] = [
        {"id": "here", "name": "здесь"},
        {"id": "here", "name": "тоже здесь"},
    ]


def an_encounter_of_another_location(data: dict[str, Any]) -> None:
    data["locations"]["village"]["encounter_id"] = "bones"  # бой происходит на кладбище


@pytest.mark.parametrize(
    "edit",
    [
        two_groups_of_one_monster,
        the_most_monsters_allowed,
        monsters_in_a_zone_without_a_link,
        a_zone_linked_to_itself,
    ],
)
def test_an_encounter_the_validator_accepts_can_be_started_from_its_location(
    tool_registry: ToolRegistry,
    srd: SrdContent,
    make_game: Callable[..., GameState],
    edit: Callable[[dict[str, Any]], None],
):
    data = adventure_data()
    edit(data)
    adventure = Adventure.model_validate(data)
    assert not validate(adventure, srd).blocking
    rules = RulesRef(modules=list(adventure.rules.modules))

    for location in adventure.locations.values():
        if location.encounter_id is None:
            continue
        encounter = adventure.encounters[location.encounter_id]
        monsters = sum(group.count for group in encounter.monsters)
        table = Table(
            make_game(location_id=location.id, rules=rules), tool_registry, adventure, srd
        )

        table.turn(call("start_combat", 20, 15, *[1] * monsters, encounter_id=encounter.id))

        assert table.state.combat is not None, location.id


@pytest.mark.parametrize(
    ("edit", "location_id", "because"),
    [
        pytest.param(two_zones_with_one_id, "cemetery", "повторяются", id="two-zones-with-one-id"),
        pytest.param(
            an_encounter_of_another_location,
            "village",
            "происходит в локации",
            id="an-encounter-of-another-location",
        ),
    ],
)
def test_an_encounter_that_cannot_be_started_is_blocked_by_the_validator(
    tool_registry: ToolRegistry,
    srd: SrdContent,
    make_game: Callable[..., GameState],
    edit: Callable[[dict[str, Any]], None],
    location_id: str,
    because: str,
):
    # Две правки, после которых `start_combat` не начнёт бой, а модель данные не исправит:
    # валидатор обязан их остановить, иначе игра застрянет посреди приключения.
    data = adventure_data()
    edit(data)
    adventure = Adventure.model_validate(data)
    table = Table(make_game(location_id=location_id), tool_registry, adventure, srd)

    table.refuse(call("start_combat", 20, 15, *[1] * 8, encounter_id="bones"), because)

    assert validate(adventure, srd).blocking
