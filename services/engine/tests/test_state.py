"""События движка и состояние игры: сериализация без потерь и форма данных.

`GameState` хранится в БД как JSON, а события уходят в журнал хода и по WebSocket, поэтому
главное свойство — то, что `model_validate_json(model_dump_json())` возвращает тот же объект.
"""

import json
from typing import get_args

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from dnd_engine import events
from dnd_engine.character import ActiveCondition
from dnd_engine.combat.model import Combat, Combatant, Zone, ZoneLink
from dnd_engine.dice import roll
from dnd_engine.events import (
    ActionEvent,
    AttitudeEvent,
    ChronicleEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    FlagEvent,
    HealEvent,
    InspirationEvent,
    ItemEvent,
    MoveEvent,
    PrivateMessageEvent,
    RestEvent,
    RevealEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.journal import JournalEntry
from dnd_engine.rest import RestOutcome
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import (
    Chronicle,
    GameState,
    GameTime,
    PcHook,
    RulesRef,
    Scene,
    SceneType,
    Spotlight,
    WorldState,
)

EVENT_ADAPTER = TypeAdapter(Event)


# --- помощники ---


def make_state(character, **overrides) -> GameState:
    """Минимальное состояние с одним персонажем; остальное переопределяется аргументами."""
    fields = {
        "game_id": "game-1",
        "adventure_id": "demo-crypt",
        "rules": RulesRef(),
        "party": {character.id: character},
        "location_id": "entrance",
    }
    return GameState(**(fields | overrides))


def make_combat() -> Combat:
    """Бой с двумя бойцами, где заполнены почти все поля."""
    return Combat(
        encounter_id="crypt-door",
        zones=[Zone(id="hall", name="Зал"), Zone(id="stairs", name="Лестница")],
        links=[ZoneLink(a="hall", b="stairs", distance="near")],
        combatants={
            "pc-1": Combatant(
                id="pc-1",
                kind="pc",
                side="party",
                ref="pc-1",
                name="Тестовый герой",
                initiative=15,
                zone_id="hall",
                engaged_with=["goblin_warrior-1"],
                turn_flags={"action": True, "moved": False},
            ),
            "goblin_warrior-1": Combatant(
                id="goblin_warrior-1",
                kind="monster",
                side="enemies",
                ref="goblin_warrior",
                name="Воин-гоблин",
                hp=7,
                hp_max=10,
                ac=15,
                initiative=12,
                zone_id="hall",
                engaged_with=["pc-1"],
                conditions=[ActiveCondition(id="prone", rounds_left=1, source="pc-1")],
                dodging=True,
            ),
        },
        order=["pc-1", "goblin_warrior-1"],
        round=2,
        turn_index=1,
    )


def rich_state(character) -> GameState:
    """Состояние, где у каждой вложенной модели значения отличаются от значений по умолчанию."""
    return make_state(
        character,
        rules=RulesRef(modules=["bg3_flavor", "campfire"]),
        scene=Scene(type=SceneType.COMBAT, started_turn=4, inspiration_granted=["pc-1"]),
        world=WorldState(
            visited_locations=["entrance", "hall"],
            revealed=["feature:altar", "clue:ring", "exit:east-door"],
            item_holders={"ring": "character:pc-1", "key": "location:hall", "torch": "gone"},
            npc_attitude={"guard": -2, "priest": 3},
            npc_alive={"guard": True, "priest": False},
            npc_location={"guard": "entrance"},
            flags={"alarm": True, "torches_lit": 3, "password": "тишина", "zero": 0},
        ),
        time=GameTime(day=3, part="night"),
        combat=make_combat(),
        chronicle=Chronicle(summary="Герои вошли в склеп.", entries=["Нашли кольцо."]),
        spotlight={"pc-1": Spotlight(submitted=5, focused=2, turns_since_focus=1)},
        pc_hooks={
            "pc-1": PcHook(
                character_id="pc-1",
                npc_links=[{"npc_id": "priest", "relation": "наставник", "note": "обязан жизнью"}],
                location_links=[{"location_id": "hall", "note": "здесь жил отец"}],
                party_links=[{"character_id": "pc-2", "relation": "старый друг"}],
                personal_goal="Найти отца",
                secret="Знает имя убийцы",
            )
        },
        short_rests_since_long=1,
        journal=(
            JournalEntry(
                id="intro@all",
                ref="intro",
                section="main",
                title="Главное",
                text="Найти смотрителя.",
                audience="all",
                turn=0,
            ),
            JournalEntry(
                id="location:hall@all",
                ref="location:hall",
                section="places",
                title="Зал",
                text="Зал склепа.\n\nЗдесь пахнет воском.",
                audience="all",
                turn=3,
                revision=2,
                visited_turn=5,
            ),
            JournalEntry(
                id="clue:ring@pc-1",
                ref="clue:ring",
                section="clues",
                title="Кольцо с гербом",
                text="Кольцо с гербом графа.",
                audience="pc-1",
                turn=9,
            ),
        ),
        party_round=4,
        turn=12,
        version=7,
    )


def sample_events() -> list[BaseModel]:
    """По одному событию каждого вида: тест ниже проверяет, что виды не потерялись."""
    d20 = roll("1d20+3", rng=ScriptedRng([14]), reason="Проверка Скрытности")
    return [
        RollEvent(actor_id="pc-1", label="Скрытность", roll=d20, dc=15, success=True),
        RollEvent(actor_id="pc-1", label="Атака", roll=d20, target_ac=13, success=True),
        RollEvent(audience="pc-2", actor_id="pc-2", label="Тайная проверка", roll=d20),
        DamageEvent(target_id="pc-1", amount=5, damage_type="slashing", hp_after=15),
        HealEvent(target_id="pc-1", amount=4, hp_after=19),
        ConditionEvent(target_id="pc-1", condition="prone", added=True),
        ActionEvent(actor_id="pc-1", action="dodge"),
        ActionEvent(actor_id="pc-2", action="aid", target_ids=["pc-1"], label="Подмога"),
        MoveEvent(location_id="hall"),
        RevealEvent(ref="clue:ring"),
        ItemEvent(item_id="ring", holder_from="location:hall", holder_to="character:pc-1"),
        FlagEvent(name="alarm", value=True),
        AttitudeEvent(npc_id="guard", value=-1),
        SceneEvent(scene=SceneType.CAMP),
        CombatEvent(phase="started"),
        CombatEvent(phase="turn", actor_id="pc-1"),
        CombatEvent(phase="ended", winner="party"),
        PrivateMessageEvent(audience="pc-1", text="Ты слышишь шорох за стеной."),
        ChronicleEvent(text="Герои победили стражника."),
        InspirationEvent(character_id="pc-1", granted=True, reason="Защитил слабого"),
        RestEvent(
            rest="short",
            outcomes=[
                RestOutcome(
                    character_id="pc-1", hp_gained=6, hit_dice_spent=1, rolls=[d20], restored=[]
                )
            ],
        ),
    ]


SAMPLES = sample_events()


# --- GameState ---


def test_state_with_one_character_survives_a_json_round_trip(make_character):
    state = make_state(make_character())

    restored = GameState.model_validate_json(state.model_dump_json())

    assert restored == state


def test_state_with_every_section_filled_survives_a_json_round_trip(make_character):
    state = rich_state(make_character(inspiration=True, spell_slots={1: 2}, spell_slots_max={1: 3}))

    restored = GameState.model_validate_json(state.model_dump_json())

    assert restored == state
    # Строгая проверка типов там, где JSON легко «исправляет» значение молча.
    assert restored.scene.type is SceneType.COMBAT
    assert restored.world.flags["alarm"] is True
    assert restored.world.flags["zero"] == 0
    assert type(restored.world.flags["zero"]) is int
    assert restored.party["pc-1"].spell_slots == {1: 2}
    assert restored.combat is not None
    assert restored.combat.combatants["goblin_warrior-1"].conditions[0].id == "prone"
    assert restored.journal == state.journal and isinstance(restored.journal, tuple)
    assert restored.journal[2].audience == "pc-1"  # личная запись остаётся личной


def test_new_state_has_neutral_defaults(make_character):
    state = make_state(make_character())

    assert state.scene == Scene(type=SceneType.EXPLORATION, started_turn=0, inspiration_granted=[])
    assert state.world == WorldState()
    assert state.time == GameTime(day=1, part="morning")
    assert state.combat is None
    assert state.chronicle == Chronicle(summary="", entries=[])
    assert (state.spotlight, state.pc_hooks) == ({}, {})
    assert (state.short_rests_since_long, state.turn, state.version) == (0, 0, 0)
    assert state.rules.base == "srd-5.2"
    assert state.journal == ()
    assert state.party_round == 0  # раунд игроков: 0 — начало игры и открытие (дополнение §11)


def test_a_state_saved_before_the_journal_is_read_with_an_empty_journal(make_character):
    # Снимок, записанный кодом без дневника (дополнение B8 §5): полей `journal` и `party_round`
    # в нём нет.
    saved = make_state(make_character()).model_dump(mode="json")
    del saved["journal"], saved["party_round"]

    restored = GameState.model_validate(saved)

    assert (restored.journal, restored.party_round) == ((), 0)
    assert restored.model_dump(mode="json") == saved | {"journal": [], "party_round": 0}


def test_the_round_of_the_players_survives_a_json_round_trip_apart_from_the_turn(make_character):
    state = rich_state(make_character())

    restored = GameState.model_validate_json(state.model_dump_json())

    assert (restored.party_round, restored.turn) == (4, 12)  # раунд игроков — не ход мастера


def test_a_game_in_progress_saved_before_the_journal_loses_no_field_after_reading(
    make_character,
):
    # Старая игра посреди боя (бой, зацепки, привалы, раунд и версия): чтение текстом JSON, как
    # из базы, не теряет и не меняет ни одного поля, а дневник получает умолчание (дополнение §5).
    saved = json.loads(rich_state(make_character()).model_dump_json())
    del saved["journal"], saved["party_round"]

    restored = GameState.model_validate_json(json.dumps(saved, ensure_ascii=False))

    assert (restored.journal, restored.party_round) == ((), 0)
    assert json.loads(restored.model_dump_json()) == saved | {"journal": [], "party_round": 0}
    assert (restored.turn, restored.version, restored.combat is not None) == (12, 7, True)


def test_the_journal_of_a_state_is_written_to_json_as_a_list_of_entries(make_character):
    state = rich_state(make_character())

    dumped = json.loads(state.model_dump_json())

    assert [entry["id"] for entry in dumped["journal"]] == [
        "intro@all",
        "location:hall@all",
        "clue:ring@pc-1",
    ]
    assert dumped["journal"][1] == {
        "id": "location:hall@all",
        "ref": "location:hall",
        "section": "places",
        "title": "Зал",
        "text": "Зал склепа.\n\nЗдесь пахнет воском.",
        "audience": "all",
        "turn": 3,
        "revision": 2,
        "visited_turn": 5,
    }


def test_defaults_are_not_shared_between_states(make_character):
    first = make_state(make_character())
    second = make_state(make_character())

    first.world.flags["alarm"] = True
    first.world.revealed.append("clue:ring")
    first.scene.inspiration_granted.append("pc-1")
    first.chronicle.entries.append("запись")
    first.spotlight["pc-1"] = Spotlight()

    assert second.world == WorldState()
    assert second.scene == Scene()
    assert second.chronicle == Chronicle()
    assert second.spotlight == {}


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (GameTime, {"part": "noon"}),
        (RulesRef, {"base": "srd-5.1"}),
        (Scene, {"type": "battle"}),
    ],
    ids=["time-of-day", "rules-base", "scene-type"],
)
def test_values_outside_the_vocabulary_are_rejected(model, fields):
    with pytest.raises(ValidationError):
        model(**fields)


def test_scene_types_serialize_as_their_text_values():
    assert {t.value for t in SceneType} == {"exploration", "social", "combat", "camp"}
    assert json.loads(Scene(type=SceneType.CAMP).model_dump_json())["type"] == "camp"


# --- События ---


def test_every_event_class_is_in_the_union_and_has_its_own_kind():
    in_module = {
        value
        for name, value in vars(events).items()
        if name.endswith("Event") and isinstance(value, type) and issubclass(value, BaseModel)
    }
    in_union = set(get_args(get_args(Event)[0]))
    kinds = [cls.model_fields["kind"].default for cls in in_union]

    assert in_module == in_union
    assert len(set(kinds)) == len(kinds)


def test_samples_cover_every_kind_of_event():
    covered = {type(event) for event in SAMPLES}

    assert covered == set(get_args(get_args(Event)[0]))


@pytest.mark.parametrize("event", SAMPLES, ids=[f"{i}-{e.kind}" for i, e in enumerate(SAMPLES)])
def test_event_serializes_with_kind_and_restores_to_the_same_class(event):
    dumped = json.loads(event.model_dump_json())

    restored = EVENT_ADAPTER.validate_json(event.model_dump_json())

    assert dumped["kind"] == event.kind
    assert type(restored) is type(event)
    assert restored == event
    assert EVENT_ADAPTER.validate_python(event.model_dump()) == event


def test_action_event_names_the_actor_and_the_targets_and_does_not_describe_a_state():
    event = ActionEvent(actor_id="pc-1", action="dodge")

    assert (event.target_ids, event.label) == ([], None)
    assert json.loads(event.model_dump_json()) == {
        "kind": "action",
        "actor_id": "pc-1",
        "action": "dodge",
        "target_ids": [],
        "label": None,
    }
    assert EVENT_ADAPTER.validate_python({"kind": "action", "actor_id": "pc-1", "action": "dodge"})


def test_action_event_does_not_share_the_target_list_between_instances():
    first, second = ActionEvent(actor_id="a", action="x"), ActionEvent(actor_id="a", action="x")

    first.target_ids.append("pc-2")

    assert second.target_ids == []


def test_event_with_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python({"kind": "teleport", "location_id": "hall"})


def test_event_without_kind_is_rejected():
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python({"target_id": "pc-1", "amount": 5})


def test_roll_event_is_public_by_default_and_keeps_the_dice():
    d20 = roll("1d20+3", rng=ScriptedRng([14]), reason="Проверка")

    event = RollEvent(actor_id="pc-1", label="Проверка", roll=d20)

    assert event.audience == "all"
    assert (event.dc, event.target_ac, event.success) == (None, None, None)
    restored = EVENT_ADAPTER.validate_json(event.model_dump_json())
    assert restored.roll.rolls == [14]
    assert restored.roll.total == 17


def test_secret_roll_keeps_the_addressee():
    d20 = roll("1d20", rng=ScriptedRng([9]), reason="Тайная проверка")

    event = RollEvent(audience="pc-2", actor_id="pc-2", label="Тайно", roll=d20)

    assert EVENT_ADAPTER.validate_json(event.model_dump_json()).audience == "pc-2"


def test_private_message_cannot_be_addressed_to_everyone():
    with pytest.raises(ValidationError):
        PrivateMessageEvent(audience="all", text="Секрет")


@pytest.mark.parametrize("value", [True, False, 0, 7, "да", "1"])
def test_flag_event_keeps_the_type_of_its_value(value):
    restored = EVENT_ADAPTER.validate_json(FlagEvent(name="x", value=value).model_dump_json())

    assert restored.value == value
    assert type(restored.value) is type(value)


def test_scene_event_serializes_the_scene_as_text():
    dumped = json.loads(SceneEvent(scene=SceneType.COMBAT).model_dump_json())

    assert dumped == {"kind": "scene", "scene": "combat"}


def test_combat_event_phases_are_limited():
    with pytest.raises(ValidationError):
        CombatEvent(phase="paused")


def test_rest_event_carries_the_outcomes_of_every_character():
    outcome = RestOutcome(
        character_id="pc-1", hp_gained=0, hit_dice_spent=0, rolls=[], restored=["spell_slots"]
    )

    restored = EVENT_ADAPTER.validate_json(
        RestEvent(rest="long", outcomes=[outcome]).model_dump_json()
    )

    assert restored.outcomes == [outcome]
    with pytest.raises(ValidationError):
        RestEvent(rest="nap", outcomes=[])
