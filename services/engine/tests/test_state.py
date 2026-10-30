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
    DAY_PART_STARTS,
    Chronicle,
    GameState,
    GameTime,
    PcHook,
    RulesRef,
    Scene,
    SceneType,
    Spotlight,
    WorldState,
    day_part,
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
                revision=3,  # дополнена словами мастера и отмечена первым приходом
                visited_turn=5,
                updated_turn=4,
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
    assert state.scene.rest is None  # вид привала есть только у сцены `camp`
    assert state.world == WorldState()
    assert state.time == GameTime(day=1, hour=6)
    assert state.time.part == "morning"
    assert state.combat is None
    assert state.chronicle == Chronicle(summary="", entries=[])
    assert (state.spotlight, state.pc_hooks) == ({}, {})
    assert (state.short_rests_since_long, state.turn, state.version) == (0, 0, 0)
    assert state.rules.base == "srd-5.2"
    assert state.journal == ()
    assert state.party_round == 0  # раунд игроков: 0 — начало игры и открытие


def test_a_state_saved_before_the_journal_is_read_with_an_empty_journal(make_character):
    # Снимок, записанный кодом без дневника: полей `journal` и `party_round`
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
    # из базы, не теряет и не меняет ни одного поля, а дневник получает умолчание.
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
        "revision": 3,
        "visited_turn": 5,
        "updated_turn": 4,
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
        (Scene, {"type": "camp", "rest": "medium"}),
    ],
    ids=["time-of-day", "rules-base", "scene-type", "rest-kind"],
)
def test_values_outside_the_vocabulary_are_rejected(model, fields):
    with pytest.raises(ValidationError):
        model(**fields)


@pytest.mark.parametrize("kind", ["short", "long"])
def test_the_kind_of_a_camp_survives_a_json_round_trip(make_character, kind):
    state = make_state(
        make_character(), scene=Scene(type=SceneType.CAMP, started_turn=4, rest=kind)
    )

    restored = GameState.model_validate_json(state.model_dump_json())

    assert restored.scene == state.scene
    assert json.loads(state.model_dump_json())["scene"]["rest"] == kind


def test_a_camp_saved_before_the_kind_of_rest_is_read_without_a_kind(make_character):
    # Старый снимок, где коротких привалов ещё не было: у сцены `camp` поля `rest` нет; такой
    # привал движок считает долгим, а чтение не теряет и не меняет ни одного прежнего поля.
    camp = make_state(make_character(), scene=Scene(type=SceneType.CAMP, started_turn=4))
    saved = json.loads(camp.model_dump_json())
    del saved["scene"]["rest"]

    restored = GameState.model_validate_json(json.dumps(saved, ensure_ascii=False))

    assert restored.scene.rest is None
    dumped = json.loads(restored.model_dump_json())
    assert dumped == saved | {"scene": saved["scene"] | {"rest": None}}


@pytest.mark.parametrize(
    ("scene_type", "kind"),
    [
        (SceneType.EXPLORATION, None),
        (SceneType.CAMP, None),
        (SceneType.CAMP, "short"),
        (SceneType.CAMP, "long"),
    ],
    ids=["no-camp", "camp-without-a-kind", "short", "long"],
)
def test_the_kind_of_rest_is_always_in_the_stored_dict_and_reads_back(
    make_character, scene_type, kind
):
    # Сервер хранит состояние как `model_dump(mode="json")`: ключ `rest` есть всегда, `null`
    # тоже, и словарь читается обратно в то же состояние.
    state = make_state(make_character(), scene=Scene(type=scene_type, started_turn=2, rest=kind))

    stored = state.model_dump(mode="json")

    assert stored["scene"] == {
        "type": scene_type.value,
        "started_turn": 2,
        "inspiration_granted": [],
        "rest": kind,
    }
    assert GameState.model_validate(stored) == state


# --- Время игры: часы и части суток ---


@pytest.mark.parametrize(
    ("hour", "part"),
    [
        (0, "night"),
        (5, "night"),
        (6, "morning"),
        (11, "morning"),
        (12, "day"),
        (17, "day"),
        (18, "evening"),
        (21, "evening"),
        (22, "night"),
        (23, "night"),
    ],
)
def test_the_part_of_the_day_follows_the_hour(hour, part):
    # Ночь идёт через полночь: с 22 до 5 включительно.
    assert GameTime(day=2, hour=hour).part == part


def test_a_new_game_starts_on_the_first_morning_at_its_first_hour():
    assert (GameTime().day, GameTime().hour, GameTime().part) == (1, 6, "morning")


@pytest.mark.parametrize(
    ("part", "hour"), [("morning", 6), ("day", 12), ("evening", 18), ("night", 22)]
)
def test_a_time_saved_without_the_hour_starts_at_the_beginning_of_its_part(
    make_character, part, hour
):
    # Снимок до появления часов хранит только часть суток: вечерняя игра остаётся вечерней.
    saved = json.loads(make_state(make_character()).model_dump_json())
    saved["time"] = {"day": 3, "part": part}

    restored = GameState.model_validate_json(json.dumps(saved, ensure_ascii=False))

    assert (restored.time.day, restored.time.hour, restored.time.part) == (3, hour, part)
    assert json.loads(restored.model_dump_json())["time"] == {"day": 3, "hour": hour, "part": part}


def test_the_time_is_stored_with_the_day_the_hour_and_the_part_and_reads_back():
    time = GameTime(day=4, hour=19)

    dumped = time.model_dump(mode="json")

    assert dumped == {"day": 4, "hour": 19, "part": "evening"}
    assert list(dumped) == ["day", "hour", "part"]
    assert GameTime.model_validate(dumped) == time
    assert GameTime.model_validate_json(time.model_dump_json()) == time


@pytest.mark.parametrize(
    "fields",
    [
        {"hour": 24},
        {"hour": -1},
        {"hour": 19, "part": "morning"},  # часть суток не совпадает с часом
        {"part": "noon"},
        {"part": ["night"]},  # испорченный снимок: не строка
    ],
    ids=["hour-24", "hour-below-zero", "part-against-the-hour", "unknown-part", "part-not-text"],
)
def test_an_hour_outside_the_day_or_a_part_against_the_hour_is_rejected(fields):
    with pytest.raises(ValidationError):
        GameTime.model_validate({"day": 1, **fields})


@pytest.mark.parametrize(
    ("start", "hours", "expected"),
    [
        ((1, 6), 1, (1, 7, "morning")),
        ((1, 17), 1, (1, 18, "evening")),  # час отдыха меняет часть суток
        ((1, 23), 1, (2, 0, "night")),  # полночь: наступил следующий день
        ((2, 22), 3, (3, 1, "night")),
        ((1, 5), 1, (1, 6, "morning")),
        ((1, 20), 0, (1, 20, "evening")),
    ],
)
def test_hours_pass_through_midnight_into_the_next_day(start, hours, expected):
    time = GameTime(day=start[0], hour=start[1])

    later = time.plus_hours(hours)

    assert (later.day, later.hour, later.part) == expected
    assert (time.day, time.hour) == start  # исходное время не меняется


# --- Время игры: все часы суток, долгие сдвиги и сверка части с часом ---

# Часть суток каждого часа выписана руками, а не через `day_part`: таблица проверяет правило,
# а не повторяет его. Ночь идёт через полночь: 0–5 и 22–23.
PART_OF_EACH_HOUR = ["night"] * 6 + ["morning"] * 6 + ["day"] * 6 + ["evening"] * 4 + ["night"] * 2


def test_every_hour_of_the_day_belongs_to_exactly_its_part():
    assert len(PART_OF_EACH_HOUR) == 24
    for hour, part in enumerate(PART_OF_EACH_HOUR):
        assert day_part(hour) == part, hour
        assert GameTime(day=1, hour=hour).part == part, hour


def test_hour_by_hour_the_part_changes_only_at_its_first_hour_and_the_day_only_at_midnight():
    # Двое суток по часу: часть суток меняется на границах 5→6, 11→12, 17→18 и 21→22, а день —
    # только на переходе 23→0.
    time = GameTime(day=1, hour=0)
    part_changes, day_changes = [], []
    for _ in range(48):
        later = time.plus_hours(1)
        if later.part != time.part:
            part_changes.append((time.hour, later.hour, later.part))
        if later.day != time.day:
            day_changes.append((time.hour, later.hour, later.day))
        time = later

    borders = [(5, 6, "morning"), (11, 12, "day"), (17, 18, "evening"), (21, 22, "night")]
    assert part_changes == borders * 2
    assert day_changes == [(23, 0, 2), (23, 0, 3)]
    assert time == GameTime(day=3, hour=0)
    # Первый час каждой части — ровно её начало из таблицы движка.
    assert {part: hour for _, hour, part in borders} == DAY_PART_STARTS


@pytest.mark.parametrize(
    ("start", "hours", "expected"),
    [
        ((1, 6), 24, (2, 6, "morning")),  # ровно сутки: тот же час следующего дня
        ((1, 23), 25, (3, 0, "night")),
        ((1, 6), 48, (3, 6, "morning")),
        ((3, 10), 71, (6, 9, "morning")),
        ((1, 18), 100, (5, 22, "night")),
        ((2, 0), 23, (2, 23, "night")),  # без полуночи день прежний
    ],
)
def test_many_hours_and_whole_days_pass_at_once(start, hours, expected):
    later = GameTime(day=start[0], hour=start[1]).plus_hours(hours)

    assert (later.day, later.hour, later.part) == expected


@pytest.mark.parametrize("hours", [0, 1, 2, 3, 23, 24, 25, 47, 73])
def test_one_long_step_lands_where_as_many_single_hours_do(hours):
    start = GameTime(day=2, hour=21)
    step_by_step = start
    for _ in range(hours):
        step_by_step = step_by_step.plus_hours(1)

    assert start.plus_hours(hours) == step_by_step


@pytest.mark.parametrize(("part", "hour"), list(DAY_PART_STARTS.items()))
def test_an_old_time_built_from_its_part_goes_on_from_the_first_hour_of_the_part(part, hour):
    # Время без часа строится по части суток так же, как читается старый снимок: с её начала.
    old = GameTime(day=2, part=part)

    assert old == GameTime(day=2, hour=hour)
    assert old.part == part
    # Короткий отдых спустя игра идёт в той же части того же дня (ночь — с 22:00 до 23:00).
    after_a_rest = old.plus_hours(1)
    assert (after_a_rest.day, after_a_rest.hour, after_a_rest.part) == (2, hour + 1, part)


def test_a_saved_part_is_only_checked_against_the_hour_and_never_moves_it():
    # Снимок с часом и частью: совпадает — час остаётся каким был (не встаёт на начало части);
    # не совпадает — ошибка данных при любом часе суток, и в ней названы обе стороны.
    for hour, own_part in enumerate(PART_OF_EACH_HOUR):
        for part in DAY_PART_STARTS:
            saved = {"day": 4, "hour": hour, "part": part}
            if part == own_part:
                assert GameTime.model_validate(saved) == GameTime(day=4, hour=hour), saved
            else:
                with pytest.raises(
                    ValidationError, match=f"час {hour} — это {own_part}, а не {part}"
                ):
                    GameTime.model_validate(saved)


@pytest.mark.parametrize("hour", [24, -1, None, "вечер"])
def test_an_hour_outside_the_day_next_to_a_valid_part_is_a_validation_error(hour):
    # Час вне суток с частью не сверяется: его отклоняет проверка поля, а не падение валидатора.
    with pytest.raises(ValidationError):
        GameTime.model_validate({"day": 1, "hour": hour, "part": "night"})


@pytest.mark.parametrize(
    "saved",
    ['{"day": 1, "hour": "19", "part": "morning"}', '{"day": 1, "hour": 19.0, "part": "morning"}'],
    ids=["hour-as-text", "hour-as-a-fraction"],
)
def test_a_part_against_an_hour_written_not_as_a_whole_number_is_rejected(saved):
    # Час строкой или дробью поле приводит к 19, и сверка с частью идёт уже после приведения.
    with pytest.raises(ValidationError):
        GameTime.model_validate_json(saved)


@pytest.mark.parametrize(
    "saved",
    [
        {"day": 1, "hour": "19", "part": "evening"},
        {"day": 1, "hour": 19.0, "part": "evening"},
        '{"day": 1, "hour": "19", "part": "evening"}',
    ],
    ids=["text-in-python", "fraction-in-python", "text-in-json"],
)
def test_a_part_matching_an_hour_written_not_as_a_whole_number_is_accepted(saved):
    time = (
        GameTime.model_validate_json(saved)
        if isinstance(saved, str)
        else GameTime.model_validate(saved)
    )

    assert (time.day, time.hour, time.part) == (1, 19, "evening")


def test_a_saved_game_writes_the_time_as_day_hour_and_part_in_this_order(make_character):
    state = make_state(make_character(), time=GameTime(day=3, hour=19))

    text = state.model_dump_json()

    assert '"time":{"day":3,"hour":19,"part":"evening"}' in text
    # Хранится час, а часть суток вычисляется из него: задать её мимо часа нельзя.
    assert set(GameTime.model_fields) == {"day", "hour"}
    assert set(GameTime.model_computed_fields) == {"part"}
    with pytest.raises(AttributeError):
        state.time.part = "night"  # type: ignore[misc]
    assert GameState.model_validate_json(text).time == GameTime(day=3, hour=19)


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
