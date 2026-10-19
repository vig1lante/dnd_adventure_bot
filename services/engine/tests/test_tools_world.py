"""Инструменты мира и проверок (план B4.6, спека §5.4, §5.5, §8.2, §8.3).

Инструменты зовутся через настоящий `ToolRegistry`, как их будет звать модель мастера. Приключение —
`demo-crypt`: деревня → кладбище (скрытый выход в склеп, запертый сундук) → склеп. Броски — на
`ScriptedRng`: значения кубов заданы тестом, а пустая очередь доказывает, что кубы не бросались.
"""

import json
from collections.abc import Callable
from typing import Any, get_args

import pytest

from dnd_engine.abilities import Ability, Skill
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveBuff, ActiveCondition, Character
from dnd_engine.checks import VALID_DCS
from dnd_engine.combat.model import Combat, Combatant, Zone
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage
from dnd_engine.events import (
    AttitudeEvent,
    FlagEvent,
    ItemEvent,
    MoveEvent,
    RestEvent,
    RevealEvent,
    RollEvent,
)
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, GameTime, RulesRef
from dnd_engine.tools.checks import CHECK_TOOLS, TableDC
from dnd_engine.tools.lore import LORE_TOOLS, exit_status, is_locked
from dnd_engine.tools.registry import ToolArgs, ToolContext, ToolOutcome, ToolRegistry
from dnd_engine.tools.world import WORLD_TOOLS, DayPart

STR, DEX, CON, INT, WIS, CHA = Ability
NO_DICE = ScriptedRng([])  # любой бросок на нём упадёт: так видно, что кубы не бросались

# --- помощники ---


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture
def make_state(make_character: Callable[..., Character]) -> Callable[..., GameState]:
    """Состояние `demo-crypt`: два героя (`pc-1`, `pc-2`) в деревне, оба модуля правил."""

    def factory(*party: Character, **overrides: Any) -> GameState:
        heroes = party or (make_character(id="pc-1"), make_character(id="pc-2", name="Второй"))
        fields = {
            "game_id": "game-1",
            "adventure_id": "demo-crypt",
            "rules": RulesRef(modules=["bg3_flavor", "campfire"]),
            "party": {hero.id: hero for hero in heroes},
            "location_id": "village",
        }
        return GameState(**(fields | overrides))

    return factory


@pytest.fixture
def make_ctx(
    demo_adventure: Adventure, srd: SrdContent, make_state: Callable[..., GameState]
) -> Callable[..., ToolContext]:
    """Контекст вызова: `make_ctx(state, rng=..., adventure=..., secret_actors=...)`."""

    def factory(state: GameState | None = None, **overrides: Any) -> ToolContext:
        state = state or make_state()
        fields = {
            "state": state,
            "adventure": demo_adventure,
            "content": srd,
            "rules": build_ruleset(state.rules),
            "rng": ScriptedRng([10] * 20),
        }
        return ToolContext(**(fields | overrides))

    return factory


@pytest.fixture
def tools() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in (*LORE_TOOLS, *WORLD_TOOLS, *CHECK_TOOLS):
        registry.register(spec)
    return registry


@pytest.fixture
def run(tools: ToolRegistry) -> Callable[..., ToolOutcome]:
    """`run(ctx, "move_party", location_id="cemetery")`: вызов, как его делает модель."""

    def call(ctx: ToolContext, tool: str, /, **args: Any) -> ToolOutcome:
        outcome = tools.call(ctx, tool, args)
        json.dumps(outcome.model_dump())  # результат для модели и журнала — чистый JSON
        return outcome

    return call


def events_of(ctx: ToolContext, kind: type) -> list:
    return [event for event in ctx.events if isinstance(event, kind)]


def variant(demo_adventure: Adventure, edit: Callable[[dict[str, Any]], None]) -> Adventure:
    """Копия `demo-crypt` с правкой данных: приключение неизменяемо, меняем его дамп."""
    data = demo_adventure.model_dump(mode="json")
    edit(data)
    return Adventure.model_validate(data)


def fight_state(make_state: Callable[..., GameState], *monsters: Combatant) -> GameState:
    """Состояние с идущим боем: бойцы — монстры с заданными id."""
    combat = Combat(
        encounter_id=None,
        zones=[Zone(id="here", name="здесь")],
        links=[],
        combatants={fighter.id: fighter for fighter in monsters},
    )
    return make_state(combat=combat)


def skeleton(fighter_id: str = "skeleton-1", **fields: Any) -> Combatant:
    base = {
        "id": fighter_id,
        "kind": "monster",
        "side": "enemies",
        "ref": "skeleton",
        "name": "Скелет",
        "hp": 13,
        "hp_max": 13,
        "ac": 14,
        "zone_id": "here",
    }
    return Combatant(**(base | fields))


# --- набор инструментов ---


def test_toolset_has_every_world_and_check_tool():
    names = {spec.name for spec in (*WORLD_TOOLS, *CHECK_TOOLS)}

    assert names == {
        "move_party",
        "reveal",
        "give_item",
        "take_item",
        "set_flag",
        "change_attitude",
        "advance_time",
        "short_rest",
        "request_check",
        "request_save",
        "contest",
    }


def test_every_tool_has_a_russian_description_and_a_serializable_forbidding_schema():
    for spec in (*WORLD_TOOLS, *CHECK_TOOLS):
        assert issubclass(spec.args_model, ToolArgs), spec.name
        assert any("а" <= ch.lower() <= "я" for ch in spec.description), spec.name
        schema = spec.json_schema()
        assert json.loads(json.dumps(schema)) == schema, spec.name
        assert schema["additionalProperties"] is False, spec.name


def test_day_parts_and_table_dcs_match_the_engine_definitions():
    assert set(get_args(DayPart)) == set(get_args(GameTime.model_fields["part"].annotation))
    assert set(get_args(TableDC)) == VALID_DCS


# --- move_party ---


def test_party_walks_through_an_open_exit(run, make_ctx):
    ctx = make_ctx()

    outcome = run(ctx, "move_party", location_id="cemetery")

    assert outcome.error is None
    assert outcome.result == {
        "location_id": "cemetery",
        "name": "Кладбище Мшистого Брода",
        "via_exit": "to-cemetery",
        "first_visit": True,
    }
    assert ctx.state.location_id == "cemetery"
    assert ctx.events == [MoveEvent(location_id="cemetery")]


def test_visited_locations_record_both_ends_and_a_return_is_not_a_first_visit(run, make_ctx):
    ctx = make_ctx()

    run(ctx, "move_party", location_id="cemetery")
    back = run(ctx, "move_party", location_id="village")

    assert ctx.state.world.visited_locations == ["village", "cemetery"]  # без повторов
    assert back.result["first_visit"] is False


def test_party_cannot_walk_through_a_hidden_exit_until_it_is_revealed(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    before = run(ctx, "move_party", location_id="crypt")
    run(ctx, "reveal", ref="exit:to-crypt")
    after = run(ctx, "move_party", location_id="crypt")

    assert "скрыт" in before.error
    assert "reveal" in before.error  # модель узнаёт, что делать
    assert after.error is None
    assert ctx.state.location_id == "crypt"


def test_tracks_reveal_the_hidden_exit_for_the_walk(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    run(ctx, "reveal", ref="feature:tracks")

    assert run(ctx, "move_party", location_id="crypt").error is None


def test_party_cannot_leave_through_a_locked_exit_until_the_key_is_handed_over(
    run, make_ctx, make_state, demo_adventure
):
    adventure = variant(
        demo_adventure,
        lambda data: data["locations"]["village"]["exits"][0].update(locked_by="item:chest-key"),
    )
    ctx = make_ctx(make_state(), adventure=adventure)

    before = run(ctx, "move_party", location_id="cemetery")
    run(ctx, "give_item", item_id="chest-key", character_id="pc-2")  # ключ был у Марты
    after = run(ctx, "move_party", location_id="cemetery")

    assert "заперт" in before.error
    assert "item:chest-key" in before.error  # что именно открывает
    assert after.error is None


def test_a_flag_opens_a_flag_lock_for_the_walk(run, make_ctx, make_state, demo_adventure):
    adventure = variant(
        demo_adventure,
        lambda data: data["locations"]["village"]["exits"][0].update(locked_by="flag:gate-open"),
    )
    ctx = make_ctx(make_state(), adventure=adventure)

    before = run(ctx, "move_party", location_id="cemetery")
    run(ctx, "set_flag", name="gate-open", value=True)
    after = run(ctx, "move_party", location_id="cemetery")

    assert before.error is not None
    assert after.error is None


def test_via_exit_picks_the_exit_and_must_lead_to_the_named_location(run, make_ctx):
    ctx = make_ctx()

    wrong_place = run(ctx, "move_party", location_id="crypt", via_exit="to-cemetery")
    unknown_exit = run(ctx, "move_party", location_id="cemetery", via_exit="trapdoor")
    right = run(ctx, "move_party", location_id="cemetery", via_exit="to-cemetery")

    assert "ведёт" in wrong_place.error
    assert "нет выхода" in unknown_exit.error
    assert right.result["via_exit"] == "to-cemetery"


def test_party_cannot_walk_where_no_exit_leads_or_to_a_missing_location(run, make_ctx):
    ctx = make_ctx()

    far = run(ctx, "move_party", location_id="crypt")  # из деревни выхода в склеп нет
    missing = run(ctx, "move_party", location_id="moon")

    assert "нет выхода" in far.error
    assert "to-cemetery" in far.error  # модель видит, куда можно пойти
    assert "Нет локации" in missing.error
    assert ctx.state.location_id == "village"


def test_party_cannot_walk_during_combat(run, make_ctx, make_state):
    ctx = make_ctx(fight_state(make_state, skeleton()))

    outcome = run(ctx, "move_party", location_id="cemetery")

    assert "В бою" in outcome.error
    assert ctx.state.location_id == "village"


def test_an_open_exit_wins_over_a_locked_one_to_the_same_place(
    run, make_ctx, make_state, demo_adventure
):
    def edit(data: dict[str, Any]) -> None:
        exits = data["locations"]["village"]["exits"]
        exits.insert(0, {**exits[0], "id": "gate", "locked_by": "flag:never"})

    ctx = make_ctx(make_state(), adventure=variant(demo_adventure, edit))

    outcome = run(ctx, "move_party", location_id="cemetery")

    assert outcome.result["via_exit"] == "to-cemetery"


# --- reveal ---


def test_reveal_exit_makes_a_hidden_exit_open(run, make_ctx, make_state, demo_adventure):
    state = make_state(location_id="cemetery")
    crypt_exit = demo_adventure.locations["cemetery"].exits[1]
    ctx = make_ctx(state)
    assert exit_status(state, demo_adventure, crypt_exit) == "hidden"

    outcome = run(ctx, "reveal", ref="exit:to-crypt")

    assert outcome.result["new"] is True
    assert state.world.revealed == ["exit:to-crypt"]
    assert ctx.events == [RevealEvent(ref="exit:to-crypt")]
    assert exit_status(state, demo_adventure, crypt_exit) == "open"


def test_reveal_feature_applies_its_reveals_and_tells_the_clue(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    outcome = run(ctx, "reveal", ref="feature:tracks")

    assert ctx.state.world.revealed == ["feature:tracks", "clue:clue-tracks", "exit:to-crypt"]
    assert [event.ref for event in events_of(ctx, RevealEvent)] == ctx.state.world.revealed
    assert outcome.result["revealed"] == ctx.state.world.revealed
    [clue] = outcome.result["clues"]
    assert clue["id"] == "clue-tracks"
    assert "Следы волочения" in clue["description"]  # модель узнаёт, что нашли игроки


def test_a_repeated_reveal_is_answered_without_duplicates_or_events(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))
    run(ctx, "reveal", ref="feature:tracks")
    revealed, events = list(ctx.state.world.revealed), list(ctx.events)

    again = run(ctx, "reveal", ref="feature:tracks")
    derived = run(ctx, "reveal", ref="exit:to-crypt")  # уже найден через следы

    assert again.result == {"ref": "feature:tracks", "new": False}
    assert derived.result == {"ref": "exit:to-crypt", "new": False}
    assert ctx.state.world.revealed == revealed
    assert ctx.events == events


def test_a_feature_does_not_reveal_a_clue_that_was_found_before(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))
    run(ctx, "reveal", ref="clue:clue-tracks")  # улику нашли напрямую, до осмотра следов

    outcome = run(ctx, "reveal", ref="feature:tracks")

    assert ctx.state.world.revealed == ["clue:clue-tracks", "feature:tracks", "exit:to-crypt"]
    assert [event.ref for event in events_of(ctx, RevealEvent)] == ctx.state.world.revealed
    assert outcome.result["revealed"] == ["feature:tracks", "exit:to-crypt"]  # улика не повторена
    assert "clues" not in outcome.result


def test_a_repeated_reveal_works_even_after_the_party_has_left(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))
    run(ctx, "reveal", ref="feature:diary")
    ctx.state.location_id = "village"

    assert run(ctx, "reveal", ref="feature:diary").result["new"] is False


def test_reveal_works_only_for_the_current_location(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="village"))

    feature = run(ctx, "reveal", ref="feature:tracks")  # следы лежат на кладбище
    hidden_exit = run(ctx, "reveal", ref="exit:to-crypt")
    clue = run(ctx, "reveal", ref="clue:clue-diary")  # дневник тоже на кладбище

    assert "нет интересного места" in feature.error
    assert "нет выхода" in hidden_exit.error
    assert "лежит в другом месте" in clue.error
    assert ctx.state.world.revealed == []


def test_reveal_rejects_unknown_refs_and_visible_exits(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    no_feature = run(ctx, "reveal", ref="feature:ghost")
    no_clue = run(ctx, "reveal", ref="clue:ghost")
    visible_exit = run(ctx, "reveal", ref="exit:to-village")

    assert "нет интересного места" in no_feature.error
    assert "Нет улики" in no_clue.error
    assert "не скрыт" in visible_exit.error
    assert ctx.events == []


@pytest.mark.parametrize("ref", ["door:1", "feature", "feature:", "feature:Bad Id", "x" * 200])
def test_reveal_refs_must_have_the_expected_shape(run, make_ctx, ref):
    outcome = run(make_ctx(), "reveal", ref=ref)

    assert outcome.result is None
    assert "ref" in outcome.error
    assert "x" * 50 not in outcome.error  # значение не эхом в ответ модели


def test_a_locked_chest_cannot_be_revealed_until_the_key_is_handed_over(
    run, make_ctx, make_state, demo_adventure
):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    before = run(ctx, "reveal", ref="feature:chest")
    state.world.item_holders["chest-key"] = "character:pc-1"  # ключ передан отряду
    after = run(ctx, "reveal", ref="feature:chest")

    assert "заперто" in before.error
    assert "item:chest-key" in before.error
    assert after.error is None
    assert after.result["items"] == [{"id": "healing-potion", "name": "Зелье лечения"}]
    assert state.world.revealed == ["feature:chest"]


def test_the_chest_opens_after_the_key_is_given_through_the_tool(run, make_ctx, make_state):
    # Ключ лежит у Марты в деревне: сначала отряд получает его там, затем идёт на кладбище.
    ctx = make_ctx(make_state())
    run(ctx, "give_item", item_id="chest-key", character_id="pc-1")
    run(ctx, "move_party", location_id="cemetery")

    assert run(ctx, "reveal", ref="feature:chest").error is None


def test_a_flag_lock_on_a_feature_opens_with_the_flag(run, make_ctx, make_state, demo_adventure):
    def edit(data: dict[str, Any]) -> None:
        chest = data["locations"]["cemetery"]["features"][1]
        chest["locked_by"] = "flag:chest-open"

    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, adventure=variant(demo_adventure, edit))

    before = run(ctx, "reveal", ref="feature:chest")
    run(ctx, "set_flag", name="chest-open", value=True)
    after = run(ctx, "reveal", ref="feature:chest")

    assert "flag:chest-open" in before.error
    assert after.error is None
    assert is_locked(state, ctx.adventure, "flag:chest-open") is False


def test_a_clue_held_by_an_npc_needs_the_npc_in_the_current_location(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    elsewhere = run(ctx, "reveal", ref="clue:clue-marta")  # Марта в деревне
    state.world.npc_location["marta"] = "cemetery"
    here = run(ctx, "reveal", ref="clue:clue-marta")

    assert "лежит в другом месте" in elsewhere.error
    assert here.result["clues"][0]["id"] == "clue-marta"
    assert state.world.revealed == ["clue:clue-marta"]


def test_a_clue_in_a_feature_is_revealed_directly_in_its_location(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    outcome = run(ctx, "reveal", ref="clue:clue-diary")

    assert outcome.result["new"] is True
    assert ctx.state.world.revealed == ["clue:clue-diary"]  # особенность сама не раскрыта


def test_a_clue_in_a_locked_feature_stays_hidden_behind_the_lock(
    run, make_ctx, make_state, demo_adventure
):
    def edit(data: dict[str, Any]) -> None:
        data["revelations"]["kidnapper"]["clues"][0]["where"] = "feature:chest"

    ctx = make_ctx(make_state(location_id="cemetery"), adventure=variant(demo_adventure, edit))

    assert "заперто" in run(ctx, "reveal", ref="clue:clue-diary").error


def test_a_clue_in_an_item_needs_the_item_in_the_party(run, make_ctx, make_state, demo_adventure):
    def edit(data: dict[str, Any]) -> None:
        data["revelations"]["kidnapper"]["clues"][0]["where"] = "item:chest-key"

    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, adventure=variant(demo_adventure, edit))

    before = run(ctx, "reveal", ref="clue:clue-diary")  # ключ у Марты
    state.world.item_holders["chest-key"] = "character:pc-2"
    after = run(ctx, "reveal", ref="clue:clue-diary")

    assert before.error is not None
    assert after.error is None


def test_a_clue_in_an_item_inside_a_closed_place_points_to_that_place(
    run, make_ctx, make_state, demo_adventure
):
    def edit(data: dict[str, Any]) -> None:
        data["revelations"]["kidnapper"]["clues"][0]["where"] = "item:healing-potion"

    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, adventure=variant(demo_adventure, edit))

    inside = run(ctx, "reveal", ref="clue:clue-diary")  # зелье ещё в запертом сундуке
    state.world.item_holders["chest-key"] = "character:pc-1"
    run(ctx, "reveal", ref="feature:chest")
    run(ctx, "give_item", item_id="healing-potion", character_id="pc-2")
    after = run(ctx, "reveal", ref="clue:clue-diary")

    assert "feature:chest" in inside.error
    assert after.error is None


def test_a_feature_may_set_a_flag_and_the_flag_event_is_emitted(
    run, make_ctx, make_state, demo_adventure
):
    def edit(data: dict[str, Any]) -> None:
        reveals = data["locations"]["cemetery"]["features"][2]["reveals"]
        reveals.append({"kind": "flag", "id": "plate-found", "value": "yes"})
        reveals.append({"kind": "flag", "id": "plate-seen"})

    ctx = make_ctx(make_state(location_id="cemetery"), adventure=variant(demo_adventure, edit))

    outcome = run(ctx, "reveal", ref="feature:tracks")

    assert ctx.state.world.flags == {
        "plate-found": "yes",
        "plate-seen": True,
    }  # без значения — True
    assert outcome.result["flags"] == {"plate-found": "yes", "plate-seen": True}
    assert [(e.name, e.value) for e in events_of(ctx, FlagEvent)] == [
        ("plate-found", "yes"),
        ("plate-seen", True),
    ]


# --- give_item и take_item ---


def test_give_item_hands_over_an_item_from_an_npc_in_the_same_location(run, make_ctx):
    ctx = make_ctx()

    outcome = run(ctx, "give_item", item_id="chest-key", character_id="pc-1")

    assert outcome.error is None
    assert outcome.result["to"] == "character:pc-1"
    assert ctx.state.world.item_holders["chest-key"] == "character:pc-1"
    assert ctx.events == [
        ItemEvent(item_id="chest-key", holder_from="npc:marta", holder_to="character:pc-1")
    ]


def test_give_item_needs_the_npc_holder_to_be_here(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    far = run(ctx, "give_item", item_id="chest-key", character_id="pc-1")  # Марта в деревне
    state.world.npc_location["marta"] = "cemetery"
    near = run(ctx, "give_item", item_id="chest-key", character_id="pc-1")

    assert "chest-key" in far.error
    assert near.error is None


def test_give_item_takes_an_item_lying_in_the_current_location(
    run, make_ctx, make_state, demo_adventure
):
    def edit(data: dict[str, Any]) -> None:
        # Зелье не названо в `reveals` ни одного места: оно просто лежит на кладбище.
        data["locations"]["cemetery"]["features"][1]["reveals"] = []

    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, adventure=variant(demo_adventure, edit))

    outcome = run(ctx, "give_item", item_id="healing-potion", character_id="pc-2")

    assert outcome.error is None
    assert state.world.item_holders["healing-potion"] == "character:pc-2"
    assert ctx.events[0].holder_from == "location:cemetery"


def test_give_item_takes_an_item_out_of_a_chest_that_is_already_open(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state)
    run(ctx, "reveal", ref="feature:chest")

    outcome = run(ctx, "give_item", item_id="healing-potion", character_id="pc-2")

    assert outcome.error is None
    assert state.world.item_holders["healing-potion"] == "character:pc-2"
    assert events_of(ctx, ItemEvent)[0].holder_from == "location:cemetery"


# --- предмет внутри закрытого места не достать в обход замка ---
# Предмет лежит «внутри» места, пока оно не раскрыто (`reveal`): запертое, скрытое и просто ещё не
# найденное. Раскрывает место `reveal`, и он же проверяет замок; `give_item` обходом быть не должен.


def test_the_potion_of_a_locked_chest_is_not_given_without_the_key(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    outcome = run(ctx, "give_item", item_id="healing-potion", character_id="pc-1")

    assert outcome.result is None
    assert "feature:chest" in outcome.error and "item:chest-key" in outcome.error
    assert state.world.item_holders == {} and ctx.events == []


def test_the_potion_is_not_given_with_the_key_in_hand_until_the_chest_is_revealed(
    run, make_ctx, make_state
):
    state = make_state(location_id="cemetery")
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state)

    before = run(ctx, "give_item", item_id="healing-potion", character_id="pc-2")
    revealed = run(ctx, "reveal", ref="feature:chest")
    after = run(ctx, "give_item", item_id="healing-potion", character_id="pc-2")

    assert "feature:chest" in before.error and "reveal" in before.error
    assert "item:chest-key" not in before.error  # ключ уже у отряда: замок не причина отказа
    assert revealed.result["items"] == [{"id": "healing-potion", "name": "Зелье лечения"}]
    assert after.error is None
    assert state.world.item_holders["healing-potion"] == "character:pc-2"


@pytest.mark.parametrize(
    "chest",
    [
        pytest.param({}, id="место-просто-не-раскрыто"),
        pytest.param({"hidden": True}, id="скрытое-место"),
        pytest.param({"locked_by": "flag:chest-open"}, id="замок-флаг"),
    ],
)
def test_an_item_of_any_unrevealed_place_waits_for_the_reveal(
    run, make_ctx, make_state, demo_adventure, chest
):
    def edit(data: dict[str, Any]) -> None:
        data["locations"]["cemetery"]["features"][1] |= {"locked_by": None} | chest

    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, adventure=variant(demo_adventure, edit))

    before = run(ctx, "give_item", item_id="healing-potion", character_id="pc-1")
    # Флаг снимает замок-флаг; в остальных случаях он ничего не открывает.
    run(ctx, "set_flag", name="chest-open")
    still_closed = run(ctx, "give_item", item_id="healing-potion", character_id="pc-1")
    run(ctx, "reveal", ref="feature:chest")
    after = run(ctx, "give_item", item_id="healing-potion", character_id="pc-1")

    assert "feature:chest" in before.error and "feature:chest" in still_closed.error
    assert after.error is None
    assert state.world.item_holders["healing-potion"] == "character:pc-1"


def test_an_item_that_the_world_already_moved_is_no_longer_inside_the_closed_place(
    run, make_ctx, make_state
):
    state = make_state(location_id="cemetery")
    state.world.item_holders["healing-potion"] = "location:cemetery"  # брошен в этой локации
    ctx = make_ctx(state)

    outcome = run(ctx, "give_item", item_id="healing-potion", character_id="pc-1")

    assert outcome.error is None


def test_an_item_of_a_closed_place_in_another_location_is_refused_for_distance_first(run, make_ctx):
    outcome = run(make_ctx(), "give_item", item_id="healing-potion", character_id="pc-1")

    assert "location:cemetery" in outcome.error  # сначала нужно дойти, потом открывать


def test_take_item_cannot_pull_an_item_out_of_a_closed_place(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    outcome = run(ctx, "take_item", item_id="healing-potion", character_id="pc-1", to="location")

    assert outcome.result is None and "pc-1" in outcome.error
    assert state.world.item_holders == {} and ctx.events == []


def test_give_item_cannot_take_an_item_from_another_location(run, make_ctx):
    outcome = run(make_ctx(), "give_item", item_id="healing-potion", character_id="pc-1")

    assert "location:cemetery" in outcome.error  # где предмет на самом деле


def test_give_item_passes_an_item_between_party_members(run, make_ctx, make_state):
    state = make_state()
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state)

    to_self = run(ctx, "give_item", item_id="chest-key", character_id="pc-1")
    passed = run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    assert "уже у" in to_self.error
    assert passed.error is None
    assert ctx.events == [
        ItemEvent(item_id="chest-key", holder_from="character:pc-1", holder_to="character:pc-2")
    ]


def test_give_item_cannot_return_an_item_that_is_gone(run, make_ctx, make_state):
    state = make_state()
    state.world.item_holders["chest-key"] = "gone"

    outcome = run(make_ctx(state), "give_item", item_id="chest-key", character_id="pc-1")

    assert "gone" in outcome.error


def test_give_item_checks_the_item_and_the_character_exist(run, make_ctx):
    ctx = make_ctx()

    no_item = run(ctx, "give_item", item_id="sword", character_id="pc-1")
    no_hero = run(ctx, "give_item", item_id="chest-key", character_id="ghost")

    assert "chest-key" in no_item.error  # список известных предметов
    assert "pc-1" in no_hero.error  # список персонажей отряда
    assert ctx.events == []


def test_take_item_removes_an_item_from_the_party_for_good(
    run, make_ctx, make_state, demo_adventure
):
    state = make_state()
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state)

    outcome = run(ctx, "take_item", item_id="chest-key", character_id="pc-1", to="gone")

    assert outcome.error is None
    assert state.world.item_holders["chest-key"] == "gone"
    assert ctx.events == [
        ItemEvent(item_id="chest-key", holder_from="character:pc-1", holder_to="gone")
    ]
    assert is_locked(state, demo_adventure, "item:chest-key") is True  # замок закрылся снова


def test_take_item_can_leave_the_item_in_the_current_location(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    state.world.item_holders["chest-key"] = "character:pc-2"

    outcome = run(
        make_ctx(state), "take_item", item_id="chest-key", character_id="pc-2", to="location"
    )

    assert outcome.result["to"] == "location:cemetery"
    assert state.world.item_holders["chest-key"] == "location:cemetery"


def test_take_item_needs_the_item_to_be_with_that_character(run, make_ctx, make_state):
    state = make_state()
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state)

    wrong_hero = run(ctx, "take_item", item_id="chest-key", character_id="pc-2", to="gone")
    with_npc = run(ctx, "take_item", item_id="healing-potion", character_id="pc-1", to="gone")
    bad_target = run(ctx, "take_item", item_id="chest-key", character_id="pc-1", to="sky")

    assert "character:pc-1" in wrong_hero.error
    assert with_npc.error is not None
    assert "'gone' or 'location'" in bad_target.error
    assert ctx.events == []


# --- set_flag ---


@pytest.mark.parametrize("value", [True, False, 0, 7, -3, "готово"])
def test_set_flag_stores_the_value_and_emits_an_event(run, make_ctx, value):
    ctx = make_ctx()

    outcome = run(ctx, "set_flag", name="marta-trusts", value=value)

    assert outcome.error is None
    assert ctx.state.world.flags == {"marta-trusts": value}
    assert ctx.events == [FlagEvent(name="marta-trusts", value=value)]
    assert type(ctx.state.world.flags["marta-trusts"]) is type(value)


def test_set_flag_overwrites_and_defaults_to_true(run, make_ctx):
    ctx = make_ctx()

    run(ctx, "set_flag", name="alarm", value="тихая")
    run(ctx, "set_flag", name="alarm")

    assert ctx.state.world.flags == {"alarm": True}


@pytest.mark.parametrize(
    "args",
    [
        {"name": ""},
        {"name": "я" * 65},
        {"name": "ok", "value": "я" * 201},
        {"name": "ok", "value": 10**9},
        {"name": "ok", "value": 1.5},
        {"name": "ok", "value": None},
        {"name": "ok", "value": ["список"]},
    ],
)
def test_set_flag_keeps_names_and_values_within_bounds(run, make_ctx, args):
    ctx = make_ctx()

    outcome = run(ctx, "set_flag", **args)

    assert outcome.result is None
    assert "я" * 50 not in outcome.error
    assert ctx.state.world.flags == {}


def test_set_flag_cannot_forge_the_encounter_progress_that_the_engine_owns(run, make_ctx):
    ctx = make_ctx()

    outcome = run(ctx, "set_flag", name="encounter:risen-bones", value="done")

    assert "encounter:" in outcome.error
    assert ctx.state.world.flags == {}


# --- change_attitude ---


def test_change_attitude_starts_from_the_attitude_in_the_adventure(run, make_ctx):
    ctx = make_ctx()

    outcome = run(ctx, "change_attitude", npc_id="fabian", delta=2)  # у Фабиана -1

    assert outcome.result == {"npc": "Фабиан", "attitude": 1}
    assert ctx.state.world.npc_attitude == {"fabian": 1}
    assert ctx.events == [AttitudeEvent(npc_id="fabian", value=1)]


def test_change_attitude_stays_within_minus_three_and_three(run, make_ctx):
    ctx = make_ctx()

    run(ctx, "change_attitude", npc_id="marta", delta=2)
    run(ctx, "change_attitude", npc_id="marta", delta=2)
    top = run(ctx, "change_attitude", npc_id="marta", delta=2)
    run(ctx, "change_attitude", npc_id="fabian", delta=-2)
    bottom = run(ctx, "change_attitude", npc_id="fabian", delta=-2)

    assert top.result["attitude"] == 3
    assert bottom.result["attitude"] == -3
    assert ctx.state.world.npc_attitude == {"marta": 3, "fabian": -3}
    assert [e.value for e in events_of(ctx, AttitudeEvent)] == [2, 3, 3, -3, -3]


@pytest.mark.parametrize("delta", [3, -3, 100, 1.5])
def test_change_attitude_delta_is_limited_to_two_steps(run, make_ctx, delta):
    ctx = make_ctx()

    outcome = run(ctx, "change_attitude", npc_id="marta", delta=delta)

    assert outcome.result is None
    assert "delta" in outcome.error
    assert ctx.state.world.npc_attitude == {}


def test_change_attitude_needs_a_known_npc(run, make_ctx):
    outcome = run(make_ctx(), "change_attitude", npc_id="ghost", delta=1)

    assert "marta" in outcome.error and "fabian" in outcome.error


# --- advance_time ---


@pytest.mark.parametrize(
    ("start", "to", "expected"),
    [
        ((1, "morning"), "day", (1, "day")),
        ((1, "morning"), "night", (1, "night")),
        ((1, "day"), "evening", (1, "evening")),
        ((1, "evening"), "night", (1, "night")),
        ((1, "evening"), "morning", (2, "morning")),  # ночь пройдена: наступил следующий день
        ((1, "night"), "morning", (2, "morning")),
        ((1, "night"), "evening", (2, "evening")),
        ((3, "day"), "morning", (4, "morning")),
        ((1, "evening"), "day", (2, "day")),
    ],
)
def test_time_moves_forward_and_a_new_day_starts_after_the_night(
    run, make_ctx, make_state, start, to, expected
):
    ctx = make_ctx(make_state(time=GameTime(day=start[0], part=start[1])))

    outcome = run(ctx, "advance_time", to=to)

    assert outcome.error is None
    assert (ctx.state.time.day, ctx.state.time.part) == expected
    assert (outcome.result["day"], outcome.result["part"]) == expected


def test_time_cannot_stand_still_or_use_an_unknown_part(run, make_ctx):
    ctx = make_ctx()  # день 1, утро

    same = run(ctx, "advance_time", to="morning")
    unknown = run(ctx, "advance_time", to="midnight")

    assert "morning" in same.error  # модель видит, что время уже такое
    assert unknown.result is None
    assert (ctx.state.time.day, ctx.state.time.part) == (1, "morning")


def test_advance_time_reports_a_new_day(run, make_ctx, make_state):
    ctx = make_ctx(make_state(time=GameTime(day=1, part="night")))

    outcome = run(ctx, "advance_time", to="morning")

    assert outcome.result == {"day": 2, "part": "morning", "new_day": True}


# --- short_rest ---


def wounded(make_character: Callable[..., Character], **fields: Any) -> Character:
    return make_character(id="pc-1", hp=5, **fields)


def test_short_rest_spends_hit_dice_heals_and_counts_the_rest(
    run, make_ctx, make_state, make_character
):
    state = make_state(wounded(make_character), make_character(id="pc-2", name="Второй"))
    ctx = make_ctx(state, rng=ScriptedRng([6, 7]))

    outcome = run(ctx, "short_rest", spend={"pc-1": 2})

    assert outcome.error is None
    hero = state.party["pc-1"]
    assert (hero.hp, hero.hit_dice) == (18, 1)  # 5 + 6 + 7; кость хитов воина — к10
    assert state.party["pc-2"].hit_dice == 3  # не тратил кости
    assert state.short_rests_since_long == 1
    [event] = events_of(ctx, RestEvent)
    assert event.rest == "short"
    assert [o.character_id for o in event.outcomes] == ["pc-1", "pc-2"]
    assert event.outcomes[0].hp_gained == 13
    assert outcome.result["short_rests_since_long"] == 1
    assert outcome.result["characters"][0]["hp_gained"] == 13


def test_short_rest_without_spending_still_restores_class_resources(
    run, make_ctx, make_state, make_character
):
    tired = make_character(id="pc-1", resources={"second_wind": 0})
    state = make_state(tired)

    outcome = run(make_ctx(state, rng=NO_DICE), "short_rest")

    assert state.party["pc-1"].resources["second_wind"] == 1
    assert outcome.result["characters"][0]["restored"] == ["second_wind"]


def test_bg3_flavor_allows_two_short_rests_between_long_ones(run, make_ctx, make_state):
    state = make_state()
    ctx = make_ctx(state, rng=NO_DICE)

    first = run(ctx, "short_rest")
    second = run(ctx, "short_rest")
    third = run(ctx, "short_rest")

    assert first.error is None and second.error is None
    assert "длинн" in third.error  # объяснение: нужен длинный отдых
    assert state.short_rests_since_long == 2
    assert len(events_of(ctx, RestEvent)) == 2


def test_the_short_rest_limit_is_lifted_by_a_long_rest(run, make_ctx, make_state):
    state = make_state(short_rests_since_long=2)
    ctx = make_ctx(state, rng=NO_DICE)
    assert run(ctx, "short_rest").error is not None

    state.short_rests_since_long = 0  # длинный отдых (привал) обнуляет счётчик

    assert run(ctx, "short_rest").error is None


def test_without_the_limit_flag_short_rests_are_unlimited(run, make_ctx, make_state):
    state = make_state(rules=RulesRef(modules=[]), short_rests_since_long=5)
    ctx = make_ctx(state, rng=NO_DICE)

    assert run(ctx, "short_rest").error is None
    assert state.short_rests_since_long == 6


def test_short_rest_checks_everything_before_the_first_roll(run, make_ctx, make_state):
    state = make_state()
    before = state.model_dump()
    ctx = make_ctx(state, rng=NO_DICE)  # любой бросок упал бы с ValueError, а не с ToolError

    unknown = run(ctx, "short_rest", spend={"pc-1": 1, "ghost": 1})
    too_many = run(ctx, "short_rest", spend={"pc-1": 1, "pc-2": 4})  # у героя только 3 кости
    negative = run(ctx, "short_rest", spend={"pc-1": -1})

    assert "ghost" in unknown.error
    assert "pc-2" in too_many.error
    assert negative.result is None
    assert state.model_dump() == before
    assert ctx.events == []


def test_short_rest_is_impossible_during_combat(run, make_ctx, make_state):
    ctx = make_ctx(fight_state(make_state, skeleton()), rng=NO_DICE)

    outcome = run(ctx, "short_rest")

    assert "В бою" in outcome.error
    assert ctx.state.short_rests_since_long == 0


# --- request_check ---


def lira(make_character: Callable[..., Character], **fields: Any) -> Character:
    """Плутовка Лира: Ловкость 14 (+2) и владение Скрытностью (+2 мастерство): бонус +4."""
    base = {
        "id": "pc-1",
        "name": "Лира",
        "scores": {DEX: 14},
        "skill_proficiencies": [Skill.STEALTH],
    }
    return make_character(**(base | fields))


def test_request_check_rolls_against_the_dc_and_reports_a_short_result(
    run, make_ctx, make_state, make_character
):
    ctx = make_ctx(make_state(lira(make_character)), rng=ScriptedRng([13]))

    outcome = run(
        ctx,
        "request_check",
        character_id="pc-1",
        ability="dex",
        skill="stealth",
        dc=15,
        reason="пробраться мимо спящего сторожа",
    )

    assert outcome.result == {
        "actor": "Лира",
        "check": "Ловкость (Скрытность)",
        "natural": 13,
        "total": 17,
        "dc": 15,
        "success": True,
    }
    [event] = events_of(ctx, RollEvent)
    assert (event.actor_id, event.label, event.dc, event.success, event.audience) == (
        "pc-1",
        "Ловкость (Скрытность)",
        15,
        True,
        "all",
    )
    assert event.roll.total == 17
    assert "сторожа" in event.roll.reason


def test_request_check_without_a_skill_uses_the_ability_modifier(
    run, make_ctx, make_state, make_character
):
    ctx = make_ctx(make_state(lira(make_character)), rng=ScriptedRng([10]))

    outcome = run(ctx, "request_check", character_id="pc-1", ability="dex", dc=15, reason="т")

    assert outcome.result["check"] == "Ловкость"
    assert (outcome.result["total"], outcome.result["success"]) == (12, False)


@pytest.mark.parametrize("dc", [0, 1, 13, 31, 100, -5, "hard", None, 15.5])
def test_request_check_refuses_a_dc_outside_the_table_without_rolling(
    run, make_ctx, make_state, make_character, dc
):
    state = make_state(lira(make_character))
    ctx = make_ctx(state, rng=NO_DICE)

    outcome = run(ctx, "request_check", character_id="pc-1", ability="dex", dc=dc, reason="т")

    assert outcome.result is None
    assert "dc" in outcome.error
    assert "5, 10, 15, 20, 25 or 30" in outcome.error  # допустимые значения
    assert ctx.events == []


def test_the_dc_in_the_tool_schemas_is_the_table_enumeration(tools):
    for name in ("request_check", "request_save"):
        schema = next(spec for spec in tools.specs() if spec.name == name).json_schema()
        assert schema["properties"]["dc"]["enum"] == sorted(VALID_DCS), name


@pytest.mark.parametrize("tool", ["request_check", "request_save"])
@pytest.mark.parametrize("path", ["engine_dc", "dc_engine", "save_dc", "crit", "rolls", "natural"])
def test_a_model_has_no_way_around_the_dc_table_or_the_dice(run, make_ctx, tool, path):
    ctx = make_ctx(rng=NO_DICE)

    outcome = run(ctx, tool, character_id="pc-1", ability="dex", dc=15, reason="т", **{path: 13})

    assert outcome.result is None
    assert path in outcome.error  # лишнее поле — ошибка, а не молчаливо потерянное значение


@pytest.mark.parametrize(
    "args",
    [
        {"ability": "luck"},
        {"skill": "swimming"},
        {"advantage": "always"},
        {"reason": ""},
        {"reason": "я" * 300},
        {"character_id": "я" * 300},
    ],
)
def test_request_check_bounds_every_argument(run, make_ctx, args):
    base = {"character_id": "pc-1", "ability": "dex", "dc": 15, "reason": "т"}

    outcome = run(make_ctx(rng=NO_DICE), "request_check", **(base | args))

    assert outcome.result is None
    assert "я" * 50 not in outcome.error


def test_request_check_needs_a_character_of_the_party(run, make_ctx):
    outcome = run(
        make_ctx(rng=NO_DICE),
        "request_check",
        character_id="ghost",
        ability="dex",
        dc=15,
        reason="т",
    )

    assert "ghost" in outcome.error
    assert "pc-1" in outcome.error  # кто есть в отряде


def test_inspiration_gives_advantage_to_the_first_check_and_is_spent(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, inspiration=True)
    state = make_state(hero)
    ctx = make_ctx(state, rng=ScriptedRng([4, 16, 9]), inspired={"pc-1"})
    args = {"character_id": "pc-1", "ability": "dex", "skill": "stealth", "dc": 15, "reason": "т"}

    first = run(ctx, "request_check", **args)
    second = run(ctx, "request_check", **args)

    [first_event, second_event] = events_of(ctx, RollEvent)
    assert first_event.roll.advantage == Advantage.ADVANTAGE
    assert first_event.roll.rolls == [4, 16]  # два куба, берётся лучший
    assert (first.result["natural"], first.result["total"], first.result["success"]) == (
        16,
        20,
        True,
    )
    assert first.result["advantage"] == "advantage"
    assert first.result["inspiration_spent"] is True
    assert hero.inspiration is False  # источник правды снят
    assert ctx.inspired == set()
    assert second_event.roll.advantage == Advantage.NONE  # вдохновение одноразовое
    assert second_event.roll.rolls == [9]
    assert "inspiration_spent" not in second.result


def test_inspiration_without_the_players_mark_or_the_flag_is_not_used(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, inspiration=True)
    marked_only = lira(make_character, id="pc-2")
    state = make_state(hero, marked_only)
    ctx = make_ctx(state, rng=ScriptedRng([10, 10]), inspired={"pc-2"})

    run(ctx, "request_check", character_id="pc-1", ability="dex", dc=15, reason="т")  # без отметки
    run(ctx, "request_check", character_id="pc-2", ability="dex", dc=15, reason="т")  # без флага

    assert [e.roll.advantage for e in events_of(ctx, RollEvent)] == [Advantage.NONE] * 2
    assert hero.inspiration is True
    assert ctx.inspired == {"pc-2"}


def test_inspiration_cancels_a_disadvantage_set_by_the_master(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, inspiration=True)
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([10]), inspired={"pc-1"})

    run(
        ctx,
        "request_check",
        character_id="pc-1",
        ability="dex",
        dc=15,
        reason="т",
        advantage="disadvantage",
    )

    [event] = events_of(ctx, RollEvent)
    assert event.roll.advantage == Advantage.NONE
    assert event.roll.rolls == [10]
    assert hero.inspiration is False


def test_the_master_may_grant_advantage_or_disadvantage_directly(
    run, make_ctx, make_state, make_character
):
    ctx = make_ctx(make_state(lira(make_character)), rng=ScriptedRng([4, 16, 18, 7]))
    args = {"character_id": "pc-1", "ability": "dex", "dc": 15, "reason": "т"}

    high = run(ctx, "request_check", advantage="advantage", **args)
    low = run(ctx, "request_check", advantage="disadvantage", **args)

    assert (high.result["natural"], low.result["natural"]) == (16, 7)


def test_bg3_flavor_makes_a_natural_twenty_succeed_against_dc_twenty_five(
    run, make_ctx, make_state, make_character
):
    # Лира без владения: бонус +2, итог 22 меньше Сл 25 — успех даёт только хук модуля.
    hero = lira(make_character, skill_proficiencies=[])
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([20]))

    outcome = run(ctx, "request_check", character_id="pc-1", ability="dex", dc=25, reason="т")

    assert outcome.result["total"] == 22
    assert outcome.result["success"] is True
    assert outcome.result["auto"] == "nat20"


def test_bg3_flavor_makes_a_natural_one_fail_even_with_a_huge_bonus(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, scores={DEX: 20}, skill_proficiencies=[])
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([1]))

    outcome = run(ctx, "request_check", character_id="pc-1", ability="dex", dc=5, reason="т")

    assert outcome.result["total"] == 6  # выше Сл 5
    assert outcome.result["success"] is False
    assert outcome.result["auto"] == "nat1"


def test_without_the_module_a_natural_twenty_is_just_a_number(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, skill_proficiencies=[])
    state = make_state(hero, rules=RulesRef(modules=[]))
    ctx = make_ctx(state, rng=ScriptedRng([20]))

    outcome = run(ctx, "request_check", character_id="pc-1", ability="dex", dc=25, reason="т")

    assert outcome.result["success"] is False
    assert "auto" not in outcome.result


def test_halfling_luck_and_guidance_apply_to_the_tool_check(
    run, make_ctx, make_state, make_character
):
    guidance = ActiveBuff(id="guidance", rounds_left=10, source_id="cleric-1")
    hero = lira(make_character, species="halfling", buffs=[guidance])
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([1, 14, 3]))

    outcome = run(
        ctx, "request_check", character_id="pc-1", ability="dex", skill="stealth", dc=20, reason="т"
    )

    [event] = events_of(ctx, RollEvent)
    assert event.roll.rolls == [1, 14, 3]  # единица, переброс Удачи, кость Указания
    assert (outcome.result["natural"], outcome.result["total"]) == (14, 21)
    assert outcome.result["success"] is True


def test_a_secret_actors_check_is_visible_only_to_them(run, make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"}, rng=ScriptedRng([10, 10]))

    run(ctx, "request_check", character_id="pc-1", ability="dex", dc=15, reason="т")
    run(ctx, "request_check", character_id="pc-2", ability="dex", dc=15, reason="т")

    assert [(e.actor_id, e.audience) for e in events_of(ctx, RollEvent)] == [
        ("pc-1", "pc-1"),  # тайный ход: бросок видит только автор
        ("pc-2", "all"),
    ]


# --- request_save ---


def test_request_save_rolls_a_save_with_the_proficiency_bonus(
    run, make_ctx, make_state, make_character
):
    hero = lira(make_character, scores={WIS: 14}, save_proficiencies=[WIS])  # +2 и мастерство +2
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([9]))

    outcome = run(ctx, "request_save", character_id="pc-1", ability="wis", dc=15, reason="чары")

    assert outcome.result == {
        "actor": "Лира",
        "check": "Спасбросок: Мудрость",
        "natural": 9,
        "total": 13,
        "dc": 15,
        "success": False,
    }
    [event] = events_of(ctx, RollEvent)
    assert (event.label, event.dc, event.success) == ("Спасбросок: Мудрость", 15, False)


def test_bg3_check_hook_does_not_touch_saving_throws(run, make_ctx):
    ctx = make_ctx(rng=ScriptedRng([20]))

    outcome = run(ctx, "request_save", character_id="pc-1", ability="wis", dc=30, reason="т")

    assert outcome.result["success"] is False  # нат. 20 автоуспех — только для проверок
    assert "auto" not in outcome.result


def test_request_save_refuses_a_dc_outside_the_table(run, make_ctx):
    outcome = run(
        make_ctx(rng=NO_DICE), "request_save", character_id="pc-1", ability="wis", dc=13, reason="т"
    )

    assert outcome.result is None
    assert "5, 10, 15, 20, 25 or 30" in outcome.error


def test_request_save_spends_inspiration_like_a_check(run, make_ctx, make_state, make_character):
    hero = make_character(id="pc-1", inspiration=True)
    ctx = make_ctx(make_state(hero), rng=ScriptedRng([3, 18]), inspired={"pc-1"})

    outcome = run(ctx, "request_save", character_id="pc-1", ability="wis", dc=15, reason="т")

    assert (outcome.result["natural"], outcome.result["success"]) == (18, True)
    assert hero.inspiration is False


def test_a_dwarf_gets_advantage_against_poison_only_when_the_threat_is_named(
    run, make_ctx, make_state, make_character
):
    dwarf = make_character(id="pc-1", species="dwarf")
    ctx = make_ctx(make_state(dwarf), rng=ScriptedRng([4, 15, 4]))
    args = {"character_id": "pc-1", "ability": "con", "dc": 10, "reason": "яд"}

    poison = run(ctx, "request_save", against="poison", **args)
    plain = run(ctx, "request_save", **args)
    bad = run(ctx, "request_save", against="любая угроза", **args)

    assert poison.result["natural"] == 15 and poison.result["advantage"] == "advantage"
    assert plain.result["natural"] == 4 and "advantage" not in plain.result
    assert bad.result is None  # угроза — из списка, а не из текста мастера


def test_a_paralyzed_hero_fails_a_dexterity_save_without_dice_and_keeps_inspiration(
    run, make_ctx, make_state, make_character
):
    hero = make_character(id="pc-1", inspiration=True, conditions=[ActiveCondition(id="paralyzed")])
    ctx = make_ctx(make_state(hero), rng=NO_DICE, inspired={"pc-1"})

    outcome = run(ctx, "request_save", character_id="pc-1", ability="dex", dc=5, reason="т")

    assert outcome.result["success"] is False
    assert outcome.result["natural"] is None
    assert (outcome.result["auto"], outcome.result["condition"]) == ("condition", "paralyzed")
    assert hero.inspiration is True and ctx.inspired == {"pc-1"}  # куб не бросался: не тратим


def test_a_secret_actors_save_is_visible_only_to_them(run, make_ctx):
    ctx = make_ctx(secret_actors={"pc-2"}, rng=ScriptedRng([10]))

    run(ctx, "request_save", character_id="pc-2", ability="wis", dc=10, reason="т")

    assert [e.audience for e in events_of(ctx, RollEvent)] == ["pc-2"]


# --- contest ---


def contest_args(**fields: Any) -> dict[str, Any]:
    base = {
        "a_id": "pc-1",
        "a_skill": "athletics",
        "b_id": "pc-2",
        "b_skill": "acrobatics",
        "reason": "борьба",
    }
    return base | fields


def test_contest_between_two_heroes_uses_the_bonus_the_engine_counts(
    run, make_ctx, make_state, make_character
):
    strong = make_character(
        id="pc-1", name="Бранка", scores={STR: 16}, skill_proficiencies=[Skill.ATHLETICS]
    )
    agile = make_character(id="pc-2", name="Лира", scores={DEX: 14})
    ctx = make_ctx(make_state(strong, agile), rng=ScriptedRng([10, 12]))

    outcome = run(ctx, "contest", **contest_args())

    # Сила 16 и мастерство: +5 против Ловкости 14 без владения: +2.
    assert outcome.result == {
        "a": {"actor": "Бранка", "check": "Сила (Атлетика)", "natural": 10, "total": 15},
        "b": {"actor": "Лира", "check": "Ловкость (Акробатика)", "natural": 12, "total": 14},
        "winner": "Бранка",
        "winner_side": "a",
    }
    assert [(e.actor_id, e.label, e.success, e.dc) for e in events_of(ctx, RollEvent)] == [
        ("pc-1", "Сила (Атлетика)", True, None),
        ("pc-2", "Ловкость (Акробатика)", False, None),
    ]


def test_contest_tie_goes_to_the_second_side_and_says_so(run, make_ctx):
    ctx = make_ctx(rng=ScriptedRng([10, 10]))

    outcome = run(ctx, "contest", **contest_args())

    assert outcome.result["winner_side"] == "b"
    assert outcome.result["tie"] is True


def test_contest_accepts_a_bare_ability_instead_of_a_skill(
    run, make_ctx, make_state, make_character
):
    strong = make_character(id="pc-1", scores={STR: 16})
    ctx = make_ctx(make_state(strong, make_character(id="pc-2")), rng=ScriptedRng([10, 10]))

    outcome = run(ctx, "contest", **contest_args(a_skill="str", b_skill="str"))

    assert outcome.result["a"]["check"] == "Сила"
    assert (outcome.result["a"]["total"], outcome.result["b"]["total"]) == (13, 10)


def test_contest_against_an_npc_with_a_statblock_uses_the_statblock_bonus(run, make_ctx):
    ctx = make_ctx(rng=ScriptedRng([10, 10]))

    outcome = run(
        ctx, "contest", **contest_args(a_skill="deception", b_id="fabian", b_skill="deception")
    )

    # Фабиан — фанатик культа: в статблоке Обман +3.
    assert outcome.result["b"] == {
        "actor": "Фабиан",
        "check": "Харизма (Обман)",
        "natural": 10,
        "total": 13,
    }
    assert events_of(ctx, RollEvent)[1].actor_id == "fabian"


def test_contest_against_a_custom_statblock_of_the_adventure(
    run, make_ctx, make_state, demo_adventure, srd
):
    custom = srd.monsters["cultist_fanatic"].model_copy(
        update={"id": "custom-fanatic", "skills": {Skill.DECEPTION: 9}}
    )
    fabian = demo_adventure.npcs["fabian"].model_copy(update={"statblock": "custom-fanatic"})
    adventure = demo_adventure.model_copy(
        update={
            "monsters": {"custom-fanatic": custom},
            "npcs": {**demo_adventure.npcs, "fabian": fabian},
        }
    )
    ctx = make_ctx(make_state(), adventure=adventure, rng=ScriptedRng([10, 10]))

    outcome = run(ctx, "contest", **contest_args(b_id="fabian", b_skill="deception"))

    assert outcome.result["b"]["total"] == 19


def test_a_broken_statblock_link_is_a_data_error_and_not_a_mistake_of_the_model(
    run, make_ctx, make_state, demo_adventure
):
    fabian = demo_adventure.npcs["fabian"].model_copy(update={"statblock": "no-such-monster"})
    adventure = demo_adventure.model_copy(
        update={"npcs": {**demo_adventure.npcs, "fabian": fabian}}
    )
    ctx = make_ctx(make_state(), adventure=adventure, rng=ScriptedRng([10, 10]))

    # Валидатор такие ссылки не пропускает: если она всё же встретилась, это баг данных, и ход
    # должен упасть, а не выглядеть для модели её ошибкой.
    with pytest.raises(LookupError, match="no-such-monster"):
        run(ctx, "contest", **contest_args(b_id="fabian", b_skill="deception"))


def test_contest_against_a_monster_of_the_fight_counts_its_conditions(run, make_ctx, make_state):
    poisoned = skeleton(conditions=[ActiveCondition(id="poisoned")])
    ctx = make_ctx(fight_state(make_state, poisoned), rng=ScriptedRng([10, 4, 16]))

    outcome = run(ctx, "contest", **contest_args(b_id="skeleton-1", b_skill="athletics"))

    # У скелета Сила 10: бонус 0; Отравлен — помеха, из двух кубов (4 и 16) берётся меньший.
    assert outcome.result["b"] == {
        "actor": "Скелет",
        "check": "Сила (Атлетика)",
        "natural": 4,
        "total": 4,
    }
    assert events_of(ctx, RollEvent)[1].roll.advantage == Advantage.DISADVANTAGE


def test_halfling_luck_works_in_a_contest(run, make_ctx, make_state, make_character):
    halfling = make_character(id="pc-1", species="halfling")
    ctx = make_ctx(make_state(halfling, make_character(id="pc-2")), rng=ScriptedRng([1, 14, 9]))

    outcome = run(ctx, "contest", **contest_args(a_skill="str", b_skill="str"))

    assert events_of(ctx, RollEvent)[0].roll.rolls == [1, 14]
    assert (outcome.result["a"]["natural"], outcome.result["winner_side"]) == (14, "a")


def test_guidance_works_in_a_contest(run, make_ctx, make_state, make_character):
    guided = make_character(
        id="pc-1", buffs=[ActiveBuff(id="guidance", rounds_left=10, source_id="cleric-1")]
    )
    ctx = make_ctx(make_state(guided, make_character(id="pc-2")), rng=ScriptedRng([10, 3, 12]))

    outcome = run(ctx, "contest", **contest_args(a_skill="str", b_skill="str"))

    assert events_of(ctx, RollEvent)[0].roll.rolls == [10, 3]
    assert (outcome.result["a"]["total"], outcome.result["b"]["total"]) == (13, 12)


def test_inspiration_gives_a_contest_participant_advantage(
    run, make_ctx, make_state, make_character
):
    hero = make_character(id="pc-1", inspiration=True)
    ctx = make_ctx(
        make_state(hero, make_character(id="pc-2")), rng=ScriptedRng([4, 16, 10]), inspired={"pc-1"}
    )

    outcome = run(ctx, "contest", **contest_args(a_skill="str", b_skill="str"))

    assert outcome.result["a"]["natural"] == 16
    assert hero.inspiration is False and ctx.inspired == set()


def test_contest_with_a_secret_actor_is_visible_only_to_its_author(run, make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"}, rng=ScriptedRng([10, 10]))

    run(ctx, "contest", **contest_args(b_id="fabian", b_skill="persuasion"))

    # Бросок NPC — часть тайного действия: его видит автор тайного хода, а не все.
    assert [(e.actor_id, e.audience) for e in events_of(ctx, RollEvent)] == [
        ("pc-1", "pc-1"),
        ("fabian", "pc-1"),
    ]


def test_contest_with_a_secret_defender_is_visible_only_to_the_defender(run, make_ctx):
    # Автор тайного хода — вторая сторона (`b_id`): бросок обычного первого участника тоже тайный.
    ctx = make_ctx(secret_actors={"pc-2"}, rng=ScriptedRng([10, 10]))

    run(ctx, "contest", **contest_args(a_id="pc-1", b_id="pc-2"))

    assert [(e.actor_id, e.audience) for e in events_of(ctx, RollEvent)] == [
        ("pc-1", "pc-2"),
        ("pc-2", "pc-2"),
    ]


def test_contest_of_two_secret_actors_gives_each_their_own_roll(run, make_ctx):
    ctx = make_ctx(secret_actors={"pc-1", "pc-2"}, rng=ScriptedRng([10, 10]))

    run(ctx, "contest", **contest_args(a_id="pc-1", b_id="pc-2"))

    assert [(e.actor_id, e.audience) for e in events_of(ctx, RollEvent)] == [
        ("pc-1", "pc-1"),
        ("pc-2", "pc-2"),
    ]


def test_contest_rejects_bad_participants_before_anything_is_rolled_or_spent(
    run, make_ctx, make_state, make_character
):
    hero = make_character(id="pc-1", inspiration=True)
    state = make_state(hero, make_character(id="pc-2"))
    state.world.npc_alive["fabian"] = False
    ctx = make_ctx(state, rng=NO_DICE, inspired={"pc-1"})

    unknown = run(ctx, "contest", **contest_args(b_id="ghost"))
    no_statblock = run(ctx, "contest", **contest_args(b_id="marta"))
    dead = run(ctx, "contest", **contest_args(b_id="fabian"))
    same = run(ctx, "contest", **contest_args(b_id="pc-1"))

    assert "ghost" in unknown.error
    assert "статблока" in no_statblock.error
    assert "мёртв" in dead.error
    assert "a_id и b_id" in same.error
    assert hero.inspiration is True and ctx.inspired == {"pc-1"}  # вдохновение не потрачено
    assert ctx.events == []


# --- любая ошибка модели ничего не меняет ---

FAILING_CALLS = [
    ("move_party", {"location_id": "crypt"}),
    ("move_party", {"location_id": "moon"}),
    ("reveal", {"ref": "feature:chest"}),
    ("reveal", {"ref": "exit:to-crypt"}),
    ("reveal", {"ref": "clue:clue-diary"}),
    ("reveal", {"ref": "clue:ghost"}),
    ("give_item", {"item_id": "healing-potion", "character_id": "pc-1"}),
    ("give_item", {"item_id": "chest-key", "character_id": "ghost"}),
    ("take_item", {"item_id": "chest-key", "character_id": "pc-1", "to": "gone"}),
    ("set_flag", {"name": "encounter:risen-bones", "value": "done"}),
    ("change_attitude", {"npc_id": "ghost", "delta": 1}),
    ("change_attitude", {"npc_id": "marta", "delta": 3}),
    ("advance_time", {"to": "morning"}),
    ("short_rest", {"spend": {"ghost": 1}}),
    ("short_rest", {"spend": {"pc-1": 9}}),
    ("request_check", {"character_id": "ghost", "ability": "dex", "dc": 15, "reason": "т"}),
    ("request_check", {"character_id": "pc-1", "ability": "dex", "dc": 13, "reason": "т"}),
    ("request_save", {"character_id": "ghost", "ability": "wis", "dc": 15, "reason": "т"}),
    ("request_save", {"character_id": "pc-1", "ability": "wis", "dc": 14, "reason": "т"}),
    ("contest", contest_args(b_id="ghost")),
    ("contest", contest_args(b_id="marta")),
]


@pytest.mark.parametrize(
    ("name", "args"),
    FAILING_CALLS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(FAILING_CALLS)],
)
def test_a_failed_call_leaves_state_events_dice_and_inspiration_untouched(
    run, make_ctx, make_state, make_character, name, args
):
    hero = make_character(id="pc-1", inspiration=True)
    state = make_state(hero, make_character(id="pc-2"))
    before = state.model_dump()
    ctx = make_ctx(state, rng=NO_DICE, inspired={"pc-1"})

    outcome = run(ctx, name, **args)

    assert outcome.result is None and outcome.error
    assert state.model_dump() == before
    assert ctx.events == []
    assert ctx.inspired == {"pc-1"}
