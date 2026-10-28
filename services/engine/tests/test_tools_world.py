"""Инструменты мира и проверок (план B4.6, спека §5.4, §5.5, §8.2, §8.3).

Инструменты зовутся через настоящий `ToolRegistry`, как их будет звать модель мастера. Приключение —
`demo-crypt`: деревня → кладбище (скрытый выход в склеп, запертый сундук) → склеп. Броски — на
`ScriptedRng`: значения кубов заданы тестом, а пустая очередь доказывает, что кубы не бросались.
"""

import json
import re
from collections.abc import Callable
from typing import Any, get_args

import pytest

from dnd_engine.abilities import Ability, Skill
from dnd_engine.adventure.schema import Adventure, RevealRef
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
from dnd_engine.journal import (
    MAX_NOTE_CHARS,
    MAX_TEXT_CHARS,
    JournalEntry,
    open_journal,
    visible_entries,
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
        "journal_add",
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


def test_a_secret_of_a_place_is_not_a_reveal_target_its_finding_is_set_by_the_master(
    run, make_ctx, make_state, demo_adventure
):
    # Текущий контракт (формат v2): `reveal` знает только место, скрытый выход и улику, а находки
    # тайны места (`secrets[].reveals`) движок сам не применяет. Тайну мастер раскрывает рассказом,
    # а её флаг ставит `set_flag`.
    ctx = make_ctx(make_state(location_id="crypt"))
    [secret] = demo_adventure.locations["crypt"].secrets
    [finding] = secret.reveals

    refused = run(ctx, "reveal", ref=f"secret:{secret.id}")

    assert refused.result is None and "ref" in refused.error
    assert (ctx.state.world.revealed, ctx.state.world.flags, ctx.events) == ([], {}, [])
    assert finding.kind == "flag"
    assert run(ctx, "set_flag", name=finding.id).error is None
    assert ctx.state.world.flags == {finding.id: True}


# Чем мастер сам применяет находку тайны места каждого вида после удачной проверки.
SECRET_FINDING_TOOLS = {"flag": "set_flag", "clue": "reveal", "exit": "reveal", "item": "give_item"}


def test_get_location_tells_the_master_how_to_apply_every_finding_of_a_secret():
    # Движок находки тайн не применяет, а модель видит только описание инструмента: для каждого
    # вида находки там назван существующий инструмент (в одной части фразы с видом).
    [spec] = [spec for spec in LORE_TOOLS if spec.name == "get_location"]
    finding_kinds = get_args(RevealRef.model_fields["kind"].annotation)
    world_tools = {spec.name for spec in WORLD_TOOLS}

    assert set(SECRET_FINDING_TOOLS) == set(finding_kinds)
    for kind, tool in SECRET_FINDING_TOOLS.items():
        assert tool in world_tools
        assert re.search(rf"\b{kind}\b[^,;]*\b{tool}\b", spec.description), (kind, tool)
    assert "value" in spec.description  # флаг ставится с заданным значением


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


def test_short_rest_heals_at_least_one_hit_point_per_die_even_with_a_poor_constitution(
    run, make_ctx, make_state, make_character
):
    # Телосложение 6 (−2): на костях 1 и 2 итоги −1 и 0, но каждая кость лечит не меньше 1 HP
    # (дополнение B9 §1 п. 5: SRD 5.2.1 и план DND-2; в DND-1 минимум был 0).
    state = make_state(wounded(make_character, scores={Ability.CON: 6}))
    ctx = make_ctx(state, rng=ScriptedRng([1, 2]))

    outcome = run(ctx, "short_rest", spend={"pc-1": 2})

    assert outcome.error is None
    assert (state.party["pc-1"].hp, state.party["pc-1"].hit_dice) == (7, 1)
    [event] = events_of(ctx, RestEvent)
    assert [roll.total for roll in event.outcomes[0].rolls] == [-1, 0]  # броски записаны честно
    assert event.outcomes[0].hp_gained == 2
    reported = outcome.result["characters"][0]
    assert (reported["hp_gained"], reported["hp"]) == (2, 7)


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


# Фабиан живёт в склепе, а состязание берёт только NPC из текущей локации (решение D9): отряд
# в этих тестах стоит там же.
def test_contest_against_an_npc_with_a_statblock_uses_the_statblock_bonus(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state(location_id="crypt"), rng=ScriptedRng([10, 10]))

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
    ctx = make_ctx(make_state(location_id="crypt"), adventure=adventure, rng=ScriptedRng([10, 10]))

    outcome = run(ctx, "contest", **contest_args(b_id="fabian", b_skill="deception"))

    assert outcome.result["b"]["total"] == 19


def test_a_broken_statblock_link_is_a_data_error_and_not_a_mistake_of_the_model(
    run, make_ctx, make_state, demo_adventure
):
    fabian = demo_adventure.npcs["fabian"].model_copy(update={"statblock": "no-such-monster"})
    adventure = demo_adventure.model_copy(
        update={"npcs": {**demo_adventure.npcs, "fabian": fabian}}
    )
    ctx = make_ctx(make_state(location_id="crypt"), adventure=adventure, rng=ScriptedRng([10, 10]))

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


def test_contest_with_a_secret_actor_is_visible_only_to_its_author(run, make_ctx, make_state):
    ctx = make_ctx(
        make_state(location_id="crypt"), secret_actors={"pc-1"}, rng=ScriptedRng([10, 10])
    )

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


# --- дневник: переход, находка, предмет и journal_add (дополнение контракта B8 §4) ---


def journal_of(state: GameState) -> dict[str, JournalEntry]:
    return {entry.id: entry for entry in state.journal}


def test_coming_to_a_place_writes_it_in_the_journal_with_the_turn_of_the_visit(
    run, make_ctx, make_state, demo_adventure
):
    state = open_journal(make_state(location_id="cemetery", party_round=4), demo_adventure)
    state.world.revealed.append("exit:to-crypt")
    ctx = make_ctx(state)

    assert run(ctx, "move_party", location_id="crypt").error is None

    crypt = journal_of(state)["location:crypt@all"]
    assert (crypt.section, crypt.audience, crypt.turn, crypt.visited_turn) == (
        "places",
        "all",
        4,
        4,
    )
    assert crypt.text == demo_adventure.locations["crypt"].public
    assert demo_adventure.locations["crypt"].dm_notes not in crypt.text


def test_the_first_visit_of_a_place_known_at_start_marks_it_again_and_a_return_does_not(
    run, make_ctx, make_state, demo_adventure
):
    state = open_journal(make_state(party_round=2), demo_adventure)
    size = len(state.journal)
    ctx = make_ctx(state)

    run(ctx, "move_party", location_id="cemetery")
    state.party_round = 3
    run(ctx, "move_party", location_id="village")
    run(ctx, "move_party", location_id="cemetery")

    cemetery = journal_of(state)["location:cemetery@all"]
    village = journal_of(state)["location:village@all"]
    assert (cemetery.turn, cemetery.visited_turn, cemetery.revision) == (0, 2, 2)
    assert (village.visited_turn, village.revision) == (0, 1)  # отряд начал игру в деревне
    assert len(state.journal) == size  # места известны со старта: новых записей нет


def test_a_found_clue_is_written_in_the_journal_once(run, make_ctx, make_state, demo_adventure):
    ctx = make_ctx(make_state(location_id="cemetery", party_round=5))
    tracks = next(c for c in demo_adventure.revelations["kidnapper"].clues if c.id == "clue-tracks")

    run(ctx, "reveal", ref="feature:tracks")  # следы раскрывают улику и скрытый выход
    run(ctx, "reveal", ref="feature:tracks")
    run(ctx, "reveal", ref="clue:clue-tracks")

    [clue] = ctx.state.journal
    assert (clue.id, clue.section, clue.text, clue.turn) == (
        "clue:clue-tracks@all",
        "clues",
        tracks.description,
        5,
    )
    assert demo_adventure.revelations["kidnapper"].statement not in clue.text


def test_a_clue_found_in_a_secret_turn_with_for_hero_is_written_only_for_that_hero(
    run, make_ctx, make_state
):
    # Тайный ход: мастер называет героя находки, и запись улики видит только он (дополнение §10).
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-2"})

    outcome = run(ctx, "reveal", ref="feature:tracks", for_hero="pc-2")

    assert outcome.error is None
    [clue] = ctx.state.journal
    assert (clue.id, clue.audience) == ("clue:clue-tracks@pc-2", "pc-2")
    assert visible_entries(ctx.state.journal, "pc-1") == ()
    assert ctx.state.world.revealed == ["feature:tracks", "clue:clue-tracks", "exit:to-crypt"]


def test_a_clue_found_in_a_secret_turn_by_an_open_hero_is_common(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-1"})

    run(ctx, "reveal", ref="clue:clue-diary", for_hero="pc-2")  # тайный ход у другого героя

    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-diary@all"]


def test_a_clue_found_in_an_open_turn_without_for_hero_is_common(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    run(ctx, "reveal", ref="clue:clue-diary")

    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-diary@all"]


@pytest.mark.parametrize(
    "secret", [{"pc-2"}, {"pc-1", "pc-2"}], ids=["один тайный", "несколько тайных"]
)
@pytest.mark.parametrize("ref", ["feature:tracks", "clue:clue-diary"])
def test_a_find_in_a_round_with_a_secret_turn_needs_the_hero_of_the_find_before_anything_changes(
    run, make_ctx, make_state, secret, ref
):
    # `secret_actors` — тайные авторы всего раунда, а не этого вызова: чья находка, движок не
    # знает, и общую находку открытого героя нельзя молча отдать тайному (дополнение §12).
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, secret_actors=secret)
    before = state.model_dump()

    outcome = run(ctx, "reveal", ref=ref)

    assert outcome.result is None
    assert "тайный ход" in outcome.error and "for_hero" in outcome.error
    assert state.model_dump() == before  # ни находки, ни выхода, ни записи в дневнике
    assert (state.world.revealed, state.journal, ctx.events) == ([], (), [])


def test_a_find_of_a_secret_hero_named_in_a_secret_round_is_personal(run, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-2"})

    outcome = run(ctx, "reveal", ref="feature:tracks", for_hero="pc-2")

    assert outcome.error is None
    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-tracks@pc-2"]
    assert visible_entries(ctx.state.journal, "pc-1") == ()


def test_a_find_of_an_open_hero_named_in_a_secret_round_stays_common(run, make_ctx, make_state):
    # Частый случай: Вера ходит тайно, а Борис открыто обыскивает место.
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-2"})

    outcome = run(ctx, "reveal", ref="feature:tracks", for_hero="pc-1")

    assert outcome.error is None
    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-tracks@all"]
    assert visible_entries(ctx.state.journal, "pc-1") == ctx.state.journal


def test_a_secret_actor_outside_the_party_does_not_make_the_round_secret(run, make_ctx, make_state):
    # Тайный раунд — это тайный ход героя отряда; посторонний id в `secret_actors` его не делает.
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"ghost"})

    outcome = run(ctx, "reveal", ref="clue:clue-diary")

    assert outcome.error is None
    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-diary@all"]


def test_a_find_without_clues_needs_no_hero_even_in_a_secret_turn_of_several(
    run, make_ctx, make_state
):
    # Скрытый выход в дневник не пишется: героя находки называть незачем.
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-1", "pc-2"})
    ctx.state.world.revealed.append("clue:clue-tracks")

    exit_ = run(ctx, "reveal", ref="exit:to-crypt")
    tracks = run(ctx, "reveal", ref="feature:tracks")  # улика следов уже найдена раньше

    assert exit_.error is None and tracks.error is None
    assert ctx.state.journal == ()


def test_the_hero_of_a_reveal_is_its_author(tools):
    spec = next(spec for spec in tools.specs() if spec.name == "reveal")

    assert spec.author_fields == ("for_hero",)


@pytest.mark.parametrize("hero", ["ghost", "marta", "all"])
def test_reveal_refuses_a_hero_outside_the_party_before_anything_changes(
    run, make_ctx, make_state, hero
):
    state = make_state(location_id="cemetery")
    state.world.revealed.append("clue:clue-marta")
    ctx = make_ctx(state, secret_actors={"pc-1"})
    before = state.model_dump()

    new = run(ctx, "reveal", ref="feature:tracks", for_hero=hero)
    repeated = run(ctx, "reveal", ref="clue:clue-marta", for_hero=hero)  # даже уже найденное

    assert new.result is None and "pc-1" in new.error
    assert repeated.result is None and "pc-1" in repeated.error
    assert state.model_dump() == before
    assert ctx.events == []


def test_a_clue_revealed_directly_is_written_and_a_bare_exit_writes_nothing(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state(location_id="cemetery"))

    run(ctx, "reveal", ref="exit:to-crypt")
    assert ctx.state.journal == ()

    run(ctx, "reveal", ref="clue:clue-diary")
    assert [entry.id for entry in ctx.state.journal] == ["clue:clue-diary@all"]


def test_a_hero_who_gets_an_item_writes_it_in_the_journal_and_taking_it_away_does_not(
    run, make_ctx, make_state, demo_adventure
):
    key = demo_adventure.items["chest-key"]
    ctx = make_ctx(make_state(party_round=1))

    run(ctx, "give_item", item_id="chest-key", character_id="pc-1")
    run(ctx, "take_item", item_id="chest-key", character_id="pc-1", to="location")

    [entry] = ctx.state.journal
    assert (entry.id, entry.section, entry.title, entry.text, entry.turn) == (
        "item:chest-key@all",
        "items",
        key.name,
        key.public,
        1,
    )
    assert key.description not in entry.text


def test_an_item_the_party_already_knows_about_is_not_written_twice(run, make_ctx, make_state):
    ctx = make_ctx(make_state())

    run(ctx, "give_item", item_id="chest-key", character_id="pc-1")
    run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    [entry] = ctx.state.journal
    assert entry.revision == 1


def test_an_item_got_in_a_secret_turn_is_written_only_for_that_hero(run, make_ctx, make_state):
    ctx = make_ctx(make_state(), secret_actors={"pc-2"})

    run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    [entry] = ctx.state.journal
    assert (entry.id, entry.audience) == ("item:chest-key@pc-2", "pc-2")
    assert visible_entries(ctx.state.journal, "pc-1") == ()
    assert visible_entries(ctx.state.journal, "pc-2") == (entry,)


def test_an_item_passed_by_a_secret_hero_goes_to_that_hero_like_the_feed_line(
    run, make_ctx, make_state
):
    # Строка ленты о передаче предмета уходит тайному герою из участников; запись — так же.
    state = make_state()
    state.world.item_holders["chest-key"] = "character:pc-1"
    ctx = make_ctx(state, secret_actors={"pc-1"})

    run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    [entry] = state.journal
    assert entry.audience == "pc-1"


def test_journal_add_writes_what_the_heroes_learned_and_answers_with_the_entry(
    run, make_ctx, make_state, demo_adventure
):
    ctx = make_ctx(make_state(party_round=3))

    outcome = run(ctx, "journal_add", ref="npc:fabian", note="Задолжал Марте сорок золотых.")

    assert outcome.error is None
    assert outcome.result == {"entry_id": "npc:fabian@all", "new": True, "revision": 1}
    [entry] = ctx.state.journal
    assert (entry.section, entry.title, entry.turn) == ("people", "Фабиан", 3)
    assert entry.text == f"{demo_adventure.npcs['fabian'].public}\n\nЗадолжал Марте сорок золотых."
    assert demo_adventure.npcs["fabian"].secret not in entry.text
    assert ctx.events == []  # отдельного события нет: сервер сравнивает дневник до и после хода


def test_journal_add_repeated_extends_with_a_note_and_without_it_changes_nothing(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state())

    first = run(ctx, "journal_add", ref="deity:veyla")
    same = run(ctx, "journal_add", ref="deity:veyla")
    more = run(ctx, "journal_add", ref="deity:veyla", note="Её символ — очаг.\nЕй молятся хозяйки.")

    assert first.result == {"entry_id": "deity:veyla@all", "new": True, "revision": 1}
    assert same.result == {"entry_id": "deity:veyla@all", "new": False, "revision": 1}
    assert more.result == {"entry_id": "deity:veyla@all", "new": False, "revision": 2}
    [entry] = ctx.state.journal
    assert entry.text.endswith("\n\nЕё символ — очаг.\nЕй молятся хозяйки.")


def test_journal_add_for_one_hero_is_a_personal_entry_next_to_the_common_one(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state())

    run(ctx, "journal_add", ref="faction:pale-candle")
    personal = run(
        ctx, "journal_add", ref="faction:pale-candle", note="Их письмо у Фабиана.", for_hero="pc-2"
    )

    assert personal.result == {"entry_id": "faction:pale-candle@pc-2", "new": True, "revision": 1}
    common, mine = ctx.state.journal
    assert (common.audience, mine.audience) == ("all", "pc-2")
    assert visible_entries(ctx.state.journal, "pc-1") == (common,)


def test_journal_add_has_no_author_and_a_secret_turn_does_not_change_its_addressee(
    run, tools, make_ctx, make_state
):
    # Адресат записи мастера — только for_hero: автор вызова закрыл бы её тайному герою молча.
    ctx = make_ctx(make_state(), secret_actors={"pc-1"})

    outcome = run(ctx, "journal_add", ref="rumor:2")

    assert outcome.result["entry_id"] == "rumor:2@all"
    assert next(s for s in tools.specs() if s.name == "journal_add").author_fields == ()


def test_journal_add_writes_the_history_of_the_world_and_any_kind_of_the_contract(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state())
    ctx.state.world.revealed.append("clue:clue-marta")  # улику пишут в дневник только найденной
    refs = [
        "world",
        "region:barrow-hill",
        "rumor:3",
        "location:crypt",
        "clue:clue-marta",
        "item:healing-potion",
    ]

    outcomes = [run(ctx, "journal_add", ref=ref, note="Узнали.") for ref in refs]

    assert [outcome.error for outcome in outcomes] == [None] * len(refs)
    assert [entry.section for entry in ctx.state.journal] == [
        "world",
        "world",
        "rumors",
        "places",
        "clues",
        "items",
    ]
    assert ctx.state.journal[0].text == "Узнали."
    assert ctx.state.journal[3].visited_turn is None  # о месте узнали, но отряд там не был


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param({"ref": "npc:ghost"}, "npc:marta", id="unknown-npc"),
        pytest.param({"ref": "rumor:9"}, "rumor:3", id="unknown-rumor"),
        pytest.param({"ref": "god:ossmar"}, "deity:<id>", id="unknown-kind"),
        pytest.param({"ref": "intro"}, "Главное", id="intro"),
        pytest.param({"ref": "world"}, "note", id="world-without-note"),
        pytest.param({"ref": "npc:fabian", "for_hero": "ghost"}, "pc-1", id="unknown-hero"),
        pytest.param({"ref": "npc:fabian", "for_hero": "marta"}, "pc-2", id="npc-is-no-hero"),
        pytest.param({"ref": "npc:fabian", "for_hero": "all"}, "pc-1", id="all-is-no-hero"),
        pytest.param({"ref": "npc:fabian", "note": "я" * (MAX_NOTE_CHARS + 1)}, "note", id="long"),
        pytest.param({"ref": "npc:fabian", "note": "   "}, "note", id="blank-note"),
        pytest.param({"ref": "npc:fabian", "note": "красное\x1b[31m"}, "note", id="escape"),
        pytest.param({"ref": "npc:fabian", "note": "тайно\u202eнавыворот"}, "note", id="bidi"),
        pytest.param({"ref": "npc:fabian", "note": "а\tб"}, "note", id="tab"),
        pytest.param({"ref": "x" * 81}, "ref", id="long-ref"),
        pytest.param({"ref": "rumor:0"}, "rumor:1", id="rumor-zero"),
        pytest.param({"ref": "intro", "note": "т", "for_hero": "pc-1"}, "Главное", id="intro-note"),
        pytest.param({"ref": "npc:fabian", "for_hero": ""}, "pc-1", id="empty-hero"),
        pytest.param({"ref": "npc:fabian", "for_hero": "PC-1"}, "pc-1", id="hero-case"),
        pytest.param({"ref": "npc:fabian", "for_hero": "x" * 65}, "for_hero", id="long-hero"),
        pytest.param({"ref": "npc:fabian", "note": "а\rб"}, "note", id="carriage-return"),
        pytest.param({"ref": "npc:fabian", "note": "скрытое\u200bслово"}, "note", id="zero-width"),
    ],
)
def test_journal_add_refuses_wrong_calls_without_changing_the_journal(
    run, make_ctx, make_state, args, expected
):
    state = make_state()
    ctx = make_ctx(state)
    run(ctx, "journal_add", ref="npc:fabian")
    before = state.model_dump()

    outcome = run(ctx, "journal_add", **args)

    assert outcome.result is None and expected in outcome.error
    assert state.model_dump() == before
    assert ctx.events == []


def test_journal_add_refuses_a_note_beyond_the_limit_of_the_entry(run, make_ctx, make_state):
    ctx = make_ctx(make_state())

    outcomes = [run(ctx, "journal_add", ref="world", note="я" * MAX_NOTE_CHARS) for _ in range(4)]

    assert [outcome.error is None for outcome in outcomes] == [True, True, True, False]
    assert str(MAX_TEXT_CHARS) in outcomes[-1].error
    [entry] = ctx.state.journal
    assert (len(entry.text), entry.revision) == (3 * MAX_NOTE_CHARS + 4, 3)


# --- дневник: финальное покрытие блока (дополнение контракта B8 §3, §4, §10) ---


def test_a_move_of_the_party_is_written_for_everyone_even_in_a_secret_turn(
    run, make_ctx, make_state
):
    # Отряд ходит вместе: у перехода нет автора, и запись о месте общая (дополнение §4).
    ctx = make_ctx(make_state(), secret_actors={"pc-1"})

    run(ctx, "move_party", location_id="cemetery")

    [entry] = ctx.state.journal
    assert (entry.id, entry.audience, entry.visited_turn) == ("location:cemetery@all", "all", 0)


def test_with_several_secret_heroes_a_clue_goes_only_to_the_hero_of_the_find(
    run, make_ctx, make_state
):
    # Автор находки один — `for_hero`; правило «первый тайный по id» тут ни при чём.
    ctx = make_ctx(make_state(location_id="cemetery"), secret_actors={"pc-1", "pc-2"})

    outcome = run(ctx, "reveal", ref="feature:tracks", for_hero="pc-2")

    assert outcome.error is None
    [clue] = ctx.state.journal
    assert (clue.id, clue.audience) == ("clue:clue-tracks@pc-2", "pc-2")
    assert visible_entries(ctx.state.journal, "pc-1") == ()


def test_a_secret_find_stays_personal_and_the_master_shares_it_with_journal_add(
    run, make_ctx, make_state
):
    state = make_state(location_id="cemetery")
    run(make_ctx(state, secret_actors={"pc-2"}), "reveal", ref="clue:clue-diary", for_hero="pc-2")
    open_turn = make_ctx(state)  # следующий ход — открытый

    again = run(open_turn, "reveal", ref="clue:clue-diary", for_hero="pc-1")

    assert again.result == {"ref": "clue:clue-diary", "new": False}
    assert [entry.id for entry in state.journal] == ["clue:clue-diary@pc-2"]  # всем не открылась

    shared = run(open_turn, "journal_add", ref="clue:clue-diary")

    assert shared.result == {"entry_id": "clue:clue-diary@all", "new": True, "revision": 1}
    personal, common = state.journal
    assert (personal.audience, common.audience, common.text) == ("pc-2", "all", personal.text)


@pytest.mark.parametrize(("holder", "receiver"), [("pc-2", "pc-1"), ("pc-1", "pc-2")])
def test_an_item_passed_between_two_secret_heroes_goes_to_the_first_of_them_by_id(
    run, make_ctx, make_state, holder, receiver
):
    # Адресат записи один (`ToolContext.audience_of`, как у строки ленты): закрыто, а не всем.
    state = make_state()
    state.world.item_holders["chest-key"] = f"character:{holder}"
    ctx = make_ctx(state, secret_actors={"pc-1", "pc-2"})

    run(ctx, "give_item", item_id="chest-key", character_id=receiver)

    [entry] = state.journal
    assert (entry.id, entry.audience) == ("item:chest-key@pc-1", "pc-1")


def test_an_item_got_by_an_open_hero_during_the_secret_turn_of_another_is_common(
    run, make_ctx, make_state
):
    ctx = make_ctx(make_state(), secret_actors={"pc-2"})

    run(ctx, "give_item", item_id="chest-key", character_id="pc-1")

    assert [entry.id for entry in ctx.state.journal] == ["item:chest-key@all"]


def test_an_item_known_at_start_got_in_a_secret_turn_adds_a_personal_entry_beside_the_common(
    run, make_ctx, make_state, demo_adventure
):
    adventure = variant(
        demo_adventure, lambda data: data["items"]["chest-key"].update(known_at_start=True)
    )
    state = open_journal(make_state(), adventure)
    common = journal_of(state)["item:chest-key@all"]
    ctx = make_ctx(state, adventure=adventure, secret_actors={"pc-2"})

    run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    assert journal_of(state)["item:chest-key@all"] == common  # общая запись не тронута
    assert journal_of(state)["item:chest-key@pc-2"].audience == "pc-2"
    assert common in visible_entries(state.journal, "pc-1")
    assert "item:chest-key@pc-2" not in {e.id for e in visible_entries(state.journal, "pc-1")}


@pytest.mark.parametrize(
    ("ref", "section"),
    [
        ("location:crypt", "places"),
        ("npc:marta", "people"),
        ("deity:ossmar", "gods"),
        ("faction:regulars", "factions"),
        ("region:barrow-hill", "world"),
        ("rumor:2", "rumors"),
        ("clue:clue-tracks", "clues"),
        ("item:healing-potion", "items"),
    ],
)
def test_journal_add_without_a_note_writes_the_public_entry_of_any_kind(
    run, make_ctx, make_state, ref, section
):
    state = make_state(party_round=6)
    state.world.revealed.append("clue:clue-tracks")  # улику пишут в дневник только найденной
    ctx = make_ctx(state)

    outcome = run(ctx, "journal_add", ref=ref)

    assert outcome.result == {"entry_id": f"{ref}@all", "new": True, "revision": 1}
    [entry] = ctx.state.journal
    assert (entry.section, entry.audience, entry.turn, entry.visited_turn) == (
        section,
        "all",
        6,
        None,
    )
    assert entry.title and entry.text


def test_journal_add_writes_the_history_of_the_world_for_one_hero(run, make_ctx, make_state):
    ctx = make_ctx(make_state())

    outcome = run(
        ctx, "journal_add", ref="world", note="Графы Мортены правили краем.", for_hero="pc-1"
    )

    assert outcome.result == {"entry_id": "world@pc-1", "new": True, "revision": 1}
    assert visible_entries(ctx.state.journal, "pc-2") == ()


def test_journal_add_trims_the_note_and_counts_its_length_after_the_trim(run, make_ctx, make_state):
    ctx = make_ctx(make_state())

    outcome = run(ctx, "journal_add", ref="world", note="  " + "я" * MAX_NOTE_CHARS + "\n ")

    assert outcome.error is None
    [entry] = ctx.state.journal
    assert entry.text == "я" * MAX_NOTE_CHARS


def test_journal_add_extends_a_place_the_party_has_been_to_and_keeps_the_visit(
    run, make_ctx, make_state, demo_adventure
):
    state = open_journal(make_state(party_round=3), demo_adventure)
    village = journal_of(state)["location:village@all"]
    ctx = make_ctx(state)

    outcome = run(ctx, "journal_add", ref="location:village", note="Трактир закрыт до утра.")

    assert outcome.result == {"entry_id": "location:village@all", "new": False, "revision": 2}
    entry = journal_of(state)["location:village@all"]
    assert (entry.turn, entry.visited_turn) == (0, 0)
    assert entry.text == f"{village.text}\n\nТрактир закрыт до утра."


# Регрессия B8.coverage D1 (исправлена в B8.1.fix1, дополнение §11 п. 1): улика с пустым
# описанием, предмет и место без названия и `public` проходят схему, валидатор и формы
# конструктора. Авто-запись дневника о них раньше бросала ошибку уже после изменения рабочей копии,
# и ход мастера падал целиком. Теперь находка, передача и переход работают, как до B8, а записи о
# сущности без текста нет; мастер может записать её сам — только с note.


def without_text(data: dict[str, Any]) -> None:
    data["revelations"]["kidnapper"]["clues"][2]["description"] = ""  # clue-tracks
    data["items"]["chest-key"].update(name="", public="")


def test_a_clue_without_a_description_is_still_found(run, make_ctx, make_state, demo_adventure):
    adventure = variant(demo_adventure, without_text)
    ctx = make_ctx(make_state(location_id="cemetery"), adventure=adventure)

    outcome = run(ctx, "reveal", ref="feature:tracks")

    assert outcome.error is None
    assert "clue:clue-tracks" in ctx.state.world.revealed
    assert ctx.state.journal == ()  # о находке без текста дневнику сказать нечего


def test_an_item_without_a_name_and_public_text_is_still_given(
    run, make_ctx, make_state, demo_adventure
):
    adventure = variant(demo_adventure, without_text)
    ctx = make_ctx(make_state(), adventure=adventure)

    outcome = run(ctx, "give_item", item_id="chest-key", character_id="pc-2")

    assert outcome.error is None
    assert ctx.state.world.item_holders["chest-key"] == "character:pc-2"
    assert ctx.state.journal == ()


def test_a_place_without_a_name_and_public_text_is_still_reached(
    run, make_ctx, make_state, demo_adventure
):
    adventure = variant(
        demo_adventure, lambda data: data["locations"]["cemetery"].update(name="", public="")
    )
    ctx = make_ctx(make_state(), adventure=adventure)

    outcome = run(ctx, "move_party", location_id="cemetery")

    assert outcome.error is None and ctx.state.location_id == "cemetery"
    assert ctx.state.journal == ()


def test_the_master_cannot_write_an_entity_without_text_without_a_note(
    run, make_ctx, make_state, demo_adventure
):
    # Строгая запись `journal_add`: отказ до изменений, с note — можно (дополнение §11 п. 1).
    adventure = variant(demo_adventure, without_text)
    state = make_state(location_id="cemetery")
    state.world.revealed.append("clue:clue-tracks")
    ctx = make_ctx(state, adventure=adventure)

    bare = run(ctx, "journal_add", ref="item:chest-key")
    noted = run(ctx, "journal_add", ref="item:chest-key", note="Маленький ключ.")

    assert bare.result is None and "note" in bare.error
    assert noted.result == {"entry_id": "item:chest-key@all", "new": True, "revision": 1}
    assert [entry.text for entry in state.journal] == ["Маленький ключ."]


# --- journal_add: только найденная улика (S4) и печатаемость note (R4), дополнение §11 ---


def test_journal_add_writes_a_clue_only_after_it_is_found(run, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)
    before = state.model_dump()

    early = run(ctx, "journal_add", ref="clue:clue-diary")
    unknown = run(ctx, "journal_add", ref="clue:ghost")
    assert state.model_dump() == before
    run(ctx, "reveal", ref="clue:clue-tracks")
    later = run(ctx, "journal_add", ref="clue:clue-diary", note="Нашли и дневник.")
    run(ctx, "reveal", ref="clue:clue-diary")
    shared = run(ctx, "journal_add", ref="clue:clue-diary", for_hero="pc-1")

    assert early.result is None and "не найдена" in early.error
    assert unknown.result is None and "clue:clue-tracks" not in unknown.error  # найденных нет
    assert later.result is None and "clue:clue-tracks" in later.error  # подсказка: найденные
    assert shared.result == {"entry_id": "clue:clue-diary@pc-1", "new": True, "revision": 1}
    assert ctx.events and all(event.kind == "reveal" for event in ctx.events)


@pytest.mark.parametrize(
    ("note", "written"),
    [
        pytest.param("Сорок\u00a0золотых.", "Сорок золотых.", id="nbsp"),
        pytest.param("10\u202fшагов", "10 шагов", id="narrow-nbsp"),
        pytest.param("пере\u00adнос", "перенос", id="soft-hyphen"),
        pytest.param("\u2003Отступ и\u3000пробел", "Отступ и пробел", id="wide-spaces"),
    ],
)
def test_journal_add_turns_typographic_spaces_into_plain_ones(
    run, make_ctx, make_state, note, written
):
    ctx = make_ctx(make_state())

    outcome = run(ctx, "journal_add", ref="world", note=note)

    assert outcome.error is None, outcome.error
    [entry] = ctx.state.journal
    assert entry.text == written


@pytest.mark.parametrize(
    "char",
    ["\u200b", "\u200d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\u2028", "\x07"],
)
def test_journal_add_still_refuses_invisible_and_reversing_characters(
    run, make_ctx, make_state, char
):
    ctx = make_ctx(make_state())

    outcome = run(ctx, "journal_add", ref="world", note=f"тайно{char}навыворот")

    assert outcome.result is None and "note" in outcome.error
    assert ctx.state.journal == ()


def test_the_limit_of_a_note_counts_after_the_soft_hyphens_are_removed(run, make_ctx, make_state):
    ctx = make_ctx(make_state())

    outcome = run(ctx, "journal_add", ref="world", note="я\u00ad" * MAX_NOTE_CHARS)

    assert outcome.error is None, outcome.error
    assert ctx.state.journal[0].text == "я" * MAX_NOTE_CHARS


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
    ("journal_add", {"ref": "npc:ghost"}),
    ("journal_add", {"ref": "world"}),
    ("journal_add", {"ref": "npc:marta", "for_hero": "ghost"}),
    ("journal_add", {"ref": "clue:clue-marta"}),  # улика ещё не найдена
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
