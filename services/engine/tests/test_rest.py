import pytest

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveBuff, ActiveCondition, Character, DeathSaves
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.rest import RestOutcome, long_rest, short_rest
from dnd_engine.rng import ScriptedRng

CON = Ability.CON


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def with_resource_table(
    srd: SrdContent, class_id: str, resource: str, by_level: dict[int, int]
) -> SrdContent:
    """Копия данных SRD, где у класса задан ресурс с нужной таблицей использований."""
    cls = srd.classes[class_id]
    changed = cls.model_copy(update={"resources": {**cls.resources, resource: by_level}})
    return srd.model_copy(update={"classes": {**srd.classes, class_id: changed}})


# --- короткий отдых: кости хитов ---


def test_short_rest_heals_each_spent_die_with_constitution(make_character, srd):
    c = make_character(class_id="fighter", scores={CON: 14}, hp_max=30, hp=5, hit_dice=3)

    rested, outcome = short_rest(c, spend_hit_dice=2, rng=ScriptedRng([4, 7]), content=srd)

    assert rested.hp == 5 + (4 + 2) + (7 + 2)
    assert rested.hit_dice == 1
    assert outcome.character_id == "pc-1"
    assert (outcome.hp_gained, outcome.hit_dice_spent) == (15, 2)
    assert [r.total for r in outcome.rolls] == [6, 9]
    assert outcome.rolls[0].expr == "1d10+2"


def test_short_rest_uses_the_hit_die_of_the_class(make_character, srd):
    wizard = make_character(class_id="wizard", hp_max=20, hp=1)

    rested, outcome = short_rest(wizard, spend_hit_dice=1, rng=ScriptedRng([6]), content=srd)

    assert outcome.rolls[0].expr.startswith("1d6")
    assert rested.hp == 7


def test_short_rest_never_heals_above_the_maximum_but_still_spends_the_die(make_character, srd):
    c = make_character(hp_max=20, hp=18, hit_dice=3)

    rested, outcome = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([8]), content=srd)

    assert rested.hp == 20
    assert outcome.hp_gained == 2
    assert (rested.hit_dice, outcome.hit_dice_spent) == (2, 1)


def test_short_rest_die_with_negative_total_heals_nothing(make_character, srd):
    # Телосложение 6 даёт −2: единица на кости — итог −1, лечение не уходит ниже нуля.
    c = make_character(scores={CON: 6}, hp_max=20, hp=10)

    rested, outcome = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([1]), content=srd)

    assert outcome.rolls[0].total == -1  # бросок записан честно
    assert (rested.hp, outcome.hp_gained) == (10, 0)
    assert rested.hit_dice == 2


@pytest.mark.parametrize("spend", [4, -1])
def test_short_rest_rejects_impossible_dice_counts_without_rolling(make_character, srd, spend):
    c = make_character(hit_dice=3)
    rng = ScriptedRng([5])

    with pytest.raises(ValueError, match="костей хитов"):
        short_rest(c, spend_hit_dice=spend, rng=rng, content=srd)

    assert rng.randint(1, 10) == 5


def test_short_rest_cannot_spend_more_dice_than_the_character_has(make_character, srd):
    c = make_character(hit_dice=1)

    with pytest.raises(ValueError, match="костей хитов"):
        short_rest(c, spend_hit_dice=2, rng=ScriptedRng([5, 5]), content=srd)


def test_short_rest_without_spending_dice_still_recharges(make_character, srd):
    c = make_character(hp_max=20, hp=10, resources={"second_wind": 0})

    rested, outcome = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested.hp == 10
    assert rested.resources == {"second_wind": 1}
    assert (outcome.hp_gained, outcome.hit_dice_spent, outcome.rolls) == (0, 0, [])


# --- короткий отдых: ресурсы по SRD 5.2.1 ---


@pytest.mark.parametrize(
    ("class_id", "resource", "before", "after"),
    [
        # Второе дыхание, Ярость и Божественный канал: короткий отдых возвращает одно использование.
        ("fighter", "second_wind", 0, 1),
        ("fighter", "second_wind", 1, 2),
        ("fighter", "second_wind", 2, 2),
        ("barbarian", "rage", 0, 1),
        ("barbarian", "rage", 2, 3),
        ("cleric", "channel_divinity", 0, 1),
        # Всплеск действий возвращается полностью.
        ("fighter", "action_surge", 0, 1),
        # Метка охотника и Магическое восстановление короткий отдых не возвращает.
        ("ranger", "hunters_mark", 0, 0),
        ("wizard", "arcane_recovery", 0, 0),
    ],
)
def test_short_rest_recharges_resources_by_srd_rules(
    make_character, srd, class_id, resource, before, after
):
    c = make_character(class_id=class_id, resources={resource: before})

    rested, outcome = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested.resources == {resource: after}
    assert outcome.restored == ([resource] if after > before else [])


def test_short_rest_returns_one_use_of_second_wind_but_all_uses_of_action_surge(
    make_character, srd
):
    # На 3-м уровне у Всплеска одно использование, поэтому различие видно только на другой таблице.
    content = with_resource_table(srd, "fighter", "action_surge", {3: 2})
    content = with_resource_table(content, "fighter", "second_wind", {3: 4})
    c = make_character(class_id="fighter", resources={"second_wind": 0, "action_surge": 0})

    rested, _ = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=content)

    assert rested.resources == {"second_wind": 1, "action_surge": 2}


def test_short_rest_ignores_resources_unknown_to_the_class(make_character, srd):
    c = make_character(class_id="fighter", resources={"mystery": 0})

    rested, outcome = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested.resources == {"mystery": 0}
    assert outcome.restored == []


def test_short_rest_leaves_everything_else_alone(make_character, srd):
    c = make_character(
        class_id="wizard",
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 0, 2: 1},
        concentration="bless",
        temp_hp=5,
        conditions=[ActiveCondition(id="poisoned", rounds_left=3)],
        buffs=[ActiveBuff(id="bless", rounds_left=7, source_id="pc-1")],
        death_saves=DeathSaves(successes=1, failures=1),
        hit_dice=1,
    )

    rested, _ = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested == c


# --- длинный отдых ---


def test_long_rest_restores_hit_points_and_clears_temporary_ones(make_character, srd):
    c = make_character(hp_max=30, hp=4, temp_hp=6)

    rested, outcome = long_rest(c, srd)

    assert (rested.hp, rested.temp_hp) == (30, 0)
    assert (outcome.character_id, outcome.hp_gained) == ("pc-1", 26)
    assert (outcome.hit_dice_spent, outcome.rolls) == (0, [])


def test_long_rest_restores_spell_slots(make_character, srd):
    c = make_character(class_id="wizard", spell_slots_max={1: 4, 2: 2}, spell_slots={1: 0, 2: 1})

    rested, outcome = long_rest(c, srd)

    assert rested.spell_slots == {1: 4, 2: 2}
    assert outcome.restored == ["spell_slots"]


def test_long_rest_restores_every_resource_to_the_class_maximum(make_character, srd):
    c = make_character(
        class_id="fighter", resources={"second_wind": 0, "action_surge": 0, "mystery": 0}
    )

    rested, outcome = long_rest(c, srd)

    # Максимум берётся из таблицы класса; неизвестный классу ресурс не трогается.
    assert rested.resources == {"second_wind": 2, "action_surge": 1, "mystery": 0}
    assert outcome.restored == ["second_wind", "action_surge"]


@pytest.mark.parametrize("class_id", ["barbarian", "cleric", "ranger", "wizard"])
def test_long_rest_restores_class_resources_of_every_class(make_character, srd, class_id):
    maxima = {name: by_level[3] for name, by_level in srd.classes[class_id].resources.items()}
    c = make_character(class_id=class_id, resources=dict.fromkeys(maxima, 0))

    rested, _ = long_rest(c, srd)

    assert rested.resources == maxima
    assert maxima  # у класса есть хотя бы один ресурс


@pytest.mark.parametrize(
    ("hit_dice_max", "hit_dice", "expected"),
    [
        (3, 0, 1),  # половина от 3 — одна кость
        (3, 1, 2),
        (3, 3, 3),  # выше максимума не поднимается
        (1, 0, 1),  # не меньше одной кости
        (5, 0, 2),
        (6, 1, 4),
        (5, 4, 5),
    ],
)
def test_long_rest_returns_half_of_the_hit_dice_but_at_least_one(
    make_character, srd, hit_dice_max, hit_dice, expected
):
    c = make_character(hit_dice_max=hit_dice_max, hit_dice=hit_dice)

    rested, _ = long_rest(c, srd)

    assert rested.hit_dice == expected


def test_long_rest_resets_death_saves(make_character, srd):
    c = make_character(hp=0, death_saves=DeathSaves(successes=2, failures=1, stable=True))

    rested, _ = long_rest(c, srd)

    assert rested.death_saves == DeathSaves()


def test_long_rest_ends_timed_conditions_buffs_and_concentration(make_character, srd):
    c = make_character(
        concentration="bless",
        conditions=[
            ActiveCondition(id="frightened", rounds_left=5),
            ActiveCondition(id="poisoned"),
        ],
        buffs=[
            ActiveBuff(id="bless", rounds_left=7, source_id="pc-2"),
            ActiveBuff(id="blessing_of_the_grove", rounds_left=None, source_id="pc-2"),
        ],
    )

    rested, _ = long_rest(c, srd)

    assert rested.concentration is None
    # Состояние без срока (его снимает только лечение) отдых не лечит.
    assert [x.id for x in rested.conditions] == ["poisoned"]
    assert [x.id for x in rested.buffs] == ["blessing_of_the_grove"]


def test_human_gains_inspiration_from_resourceful(make_character, srd):
    human = make_character(species="human", inspiration=False)

    rested, _ = long_rest(human, srd)

    assert rested.inspiration is True


@pytest.mark.parametrize("species", ["dwarf", "elf", "halfling", "gnome"])
def test_other_species_do_not_gain_inspiration_from_a_long_rest(make_character, srd, species):
    c = make_character(species=species, inspiration=False)

    rested, _ = long_rest(c, srd)

    assert rested.inspiration is False


def test_long_rest_does_not_take_inspiration_away(make_character, srd):
    c = make_character(species="dwarf", inspiration=True)

    rested, _ = long_rest(c, srd)

    assert rested.inspiration is True


def test_long_rest_of_a_rested_character_restores_nothing(make_character, srd):
    # Человек получил бы вдохновение, поэтому берём дварфа.
    c = make_character(
        species="dwarf", class_id="fighter", resources={"second_wind": 2, "action_surge": 1}
    )

    rested, outcome = long_rest(c, srd)

    assert rested == c
    assert outcome == RestOutcome(
        character_id="pc-1", hp_gained=0, hit_dice_spent=0, rolls=[], restored=[]
    )


# --- чистота функций ---


def test_rests_return_new_characters_and_leave_the_input_untouched(make_character, srd):
    c = make_character(
        class_id="wizard",
        hp_max=20,
        hp=5,
        hit_dice=1,
        spell_slots_max={1: 4},
        spell_slots={1: 0},
        resources={"arcane_recovery": 0},
        conditions=[ActiveCondition(id="frightened", rounds_left=2)],
        buffs=[ActiveBuff(id="bless", rounds_left=3, source_id="pc-2")],
        concentration="bless",
    )
    before = c.model_copy(deep=True)

    short, _ = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([3]), content=srd)
    long, _ = long_rest(c, srd)

    assert c == before
    assert short is not c
    assert long is not c


def test_rested_character_does_not_share_nested_data_with_the_original(make_character, srd):
    c = make_character(
        spell_slots_max={1: 2},
        spell_slots={1: 2},
        buffs=[ActiveBuff(id="mage_armor", rounds_left=None, source_id="pc-1")],
        resources={"second_wind": 2},
    )

    short, _ = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)
    long, _ = long_rest(c, srd)
    short.resources["second_wind"] = 0
    short.buffs[0].rounds_left = 1
    long.buffs[0].rounds_left = 1
    long.spell_slots[1] = 0

    assert c.resources == {"second_wind": 2}
    assert c.buffs[0].rounds_left is None
    assert c.spell_slots == c.spell_slots_max == {1: 2}


# --- исход отдыха сериализуется для событий ---


def test_rest_outcome_survives_json_round_trip_with_its_rolls(make_character, srd):
    c = make_character(
        class_id="fighter", scores={CON: 14}, hp_max=30, hp=5, resources={"second_wind": 0}
    )
    _, outcome = short_rest(c, spend_hit_dice=2, rng=ScriptedRng([4, 7]), content=srd)

    restored = RestOutcome.model_validate_json(outcome.model_dump_json())

    assert restored == outcome
    assert [r.total for r in restored.rolls] == [6, 9]
    assert restored.restored == ["second_wind"]


def test_rested_characters_survive_json_round_trip(make_character, srd):
    c = make_character(
        class_id="cleric",
        hp_max=24,
        hp=3,
        hit_dice=1,
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 0, 2: 1},
        resources={"channel_divinity": 0},
        conditions=[ActiveCondition(id="frightened", rounds_left=2), ActiveCondition(id="prone")],
        buffs=[ActiveBuff(id="bless", rounds_left=3, source_id="pc-2")],
    )
    short, _ = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([5]), content=srd)
    long, _ = long_rest(c, srd)

    for rested in (short, long):
        assert Character.model_validate_json(rested.model_dump_json()) == rested
    assert [x.id for x in long.conditions] == ["prone"]  # бессрочное состояние остаётся
