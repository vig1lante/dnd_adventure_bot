import pytest

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveBuff, ActiveCondition, Character, DeathSaves
from dnd_engine.conditions import ZERO_HP_SOURCE
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.rest import (
    AID_BUFF,
    BREATH_WEAPON_TRAIT,
    RELENTLESS_ENDURANCE_TRAIT,
    SHORT_REST_ALL_USES,
    SHORT_REST_ONE_USE,
    RestError,
    RestOutcome,
    arcane_recovery,
    end_buffs,
    finish_short_rest,
    long_rest,
    short_rest,
    spend_hit_die,
)
from dnd_engine.rng import ScriptedRng, SeededRng

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


def test_short_rest_die_with_negative_total_still_heals_one_hit_point(make_character, srd):
    # Телосложение 6 даёт −2: единица на кости — итог −1, но кость хитов лечит не меньше 1 HP
    # (SRD 5.2.1 и план DND-2; прежде минимум был 0).
    c = make_character(scores={CON: 6}, hp_max=20, hp=10)

    rested, outcome = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([1]), content=srd)

    assert outcome.rolls[0].total == -1  # бросок записан честно
    assert (rested.hp, outcome.hp_gained) == (11, 1)
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


# --- смерть и «без сознания» от 0 HP ---


def knocked_out(make_character, **overrides) -> Character:
    """Персонаж на 0 HP: без сознания от нуля хитов, идёт череда спасбросков от смерти."""
    fields = {
        "hp": 0,
        "conditions": [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)],
        "death_saves": DeathSaves(successes=1, failures=2),
    }
    return make_character(**(fields | overrides))


def dead(make_character, **overrides) -> Character:
    return knocked_out(make_character, death_saves=DeathSaves(failures=3), **overrides)


def test_long_rest_wakes_up_a_character_who_was_knocked_out_by_zero_hit_points(make_character, srd):
    c = knocked_out(make_character, hp_max=20)

    rested, _ = long_rest(c, srd)

    assert rested.hp == 20
    assert rested.conditions == []
    assert rested.death_saves == DeathSaves()


def test_long_rest_keeps_unconsciousness_that_came_from_something_else(make_character, srd):
    spell_sleep = ActiveCondition(id="unconscious", source="sleep")
    c = knocked_out(make_character, conditions=[spell_sleep])

    rested, _ = long_rest(c, srd)

    assert rested.conditions == [spell_sleep]  # снимается только «без сознания» от нуля хитов


def test_short_rest_hit_dice_wake_up_a_character_at_zero_hit_points(make_character, srd):
    c = knocked_out(make_character, hp_max=20, hit_dice=2)

    rested, outcome = short_rest(c, spend_hit_dice=1, rng=ScriptedRng([5]), content=srd)

    assert (rested.hp, outcome.hp_gained) == (5, 5)
    assert rested.conditions == []
    assert rested.death_saves == DeathSaves()


def test_short_rest_without_healing_leaves_a_character_at_zero_hit_points_unconscious(
    make_character, srd
):
    c = knocked_out(make_character, hit_dice=2)

    rested, _ = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested == c


def test_long_rest_gives_a_dead_character_nothing(make_character, srd):
    c = dead(make_character, hp_max=20, spell_slots_max={1: 2}, spell_slots={1: 0})

    rested, outcome = long_rest(c, srd)

    assert rested == c and rested is not c
    assert outcome == RestOutcome(
        character_id="pc-1", hp_gained=0, hit_dice_spent=0, rolls=[], restored=[]
    )


def test_short_rest_gives_a_dead_character_nothing_and_rolls_no_dice(make_character, srd):
    c = dead(make_character, hp_max=20, hit_dice=3, resources={"second_wind": 0})

    rested, outcome = short_rest(c, spend_hit_dice=2, rng=ScriptedRng([]), content=srd)

    assert rested == c and rested is not c
    assert (outcome.hp_gained, outcome.hit_dice_spent, outcome.rolls) == (0, 0, [])
    assert outcome.restored == []


def test_short_rest_of_a_dead_character_still_rejects_an_impossible_dice_count(make_character, srd):
    c = dead(make_character, hit_dice=1)

    with pytest.raises(ValueError, match="костей хитов"):
        short_rest(c, spend_hit_dice=2, rng=ScriptedRng([]), content=srd)


def test_a_character_with_two_failures_is_still_alive_for_rests(make_character, srd):
    c = knocked_out(make_character, hp_max=20)  # две неудачи из трёх

    rested, outcome = long_rest(c, srd)

    assert (rested.hp, outcome.hp_gained) == (20, 20)


# --- Неутомимость орка ---


def test_long_rest_restores_relentless_endurance_of_an_orc(make_character, srd):
    orc = make_character(species="orc", resources={"relentless_endurance": 0})

    rested, outcome = long_rest(orc, srd)

    assert rested.resources == {"relentless_endurance": 1}
    assert outcome.restored == ["relentless_endurance"]


def test_long_rest_does_not_invent_a_relentless_endurance_resource(make_character, srd):
    orc = make_character(species="orc", resources={})

    rested, outcome = long_rest(orc, srd)

    # Нет записи — значит, неиспользовано: отдых ничего не меняет.
    assert rested.resources == {} and outcome.restored == []


def test_short_rest_does_not_restore_relentless_endurance(make_character, srd):
    orc = make_character(species="orc", resources={"relentless_endurance": 0})

    rested, outcome = short_rest(orc, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested.resources == {"relentless_endurance": 0} and outcome.restored == []


def test_only_orcs_restore_relentless_endurance(make_character, srd):
    human = make_character(species="dwarf", resources={"relentless_endurance": 0})

    rested, _ = long_rest(human, srd)

    assert rested.resources == {"relentless_endurance": 0}


def test_orc_species_has_the_relentless_endurance_trait_the_rest_code_uses(srd):
    assert RELENTLESS_ENDURANCE_TRAIT in {t.id for t in srd.species["orc"].traits}


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


# --- «Подмога»: максимум HP растёт и откатывается, когда эффект кончается ---


def aided(make_character, bonus: int = 5, **overrides) -> Character:
    """Персонаж под «Подмогой»: максимум и текущие HP уже увеличены на `bonus`."""
    fields = {
        "hp_max": 20 + bonus,
        "hp": 20 + bonus,
        "buffs": [ActiveBuff(id=AID_BUFF, rounds_left=4800, source_id="pc-2", data={"hp": bonus})],
    }
    return make_character(**(fields | overrides))


def test_ending_aid_returns_the_hit_point_maximum_and_caps_current_hit_points(make_character):
    c = aided(make_character)

    ended = end_buffs(c, lambda buff: buff.id == AID_BUFF)

    assert [b.id for b in ended] == [AID_BUFF]
    assert (c.hp_max, c.hp, c.buffs) == (20, 20, [])


def test_ending_aid_keeps_current_hit_points_that_are_below_the_old_maximum(make_character):
    c = aided(make_character, hp=7)

    end_buffs(c, lambda buff: buff.id == AID_BUFF)

    assert (c.hp_max, c.hp) == (20, 7)


def test_ending_aid_works_for_a_higher_slot_bonus(make_character):
    c = aided(make_character, bonus=10)

    end_buffs(c, lambda buff: buff.id == AID_BUFF)

    assert (c.hp_max, c.hp) == (20, 20)


def test_end_buffs_leaves_the_buffs_that_do_not_match(make_character):
    c = aided(make_character, buffs=[bless_buff(), aid_buff(5)])

    ended = end_buffs(c, lambda buff: buff.id == "bless")

    assert [b.id for b in ended] == ["bless"]
    assert [b.id for b in c.buffs] == [AID_BUFF]
    assert c.hp_max == 25  # Подмога не кончилась: максимум прежний


def bless_buff() -> ActiveBuff:
    return ActiveBuff(id="bless", rounds_left=7, source_id="pc-2")


def aid_buff(bonus: int) -> ActiveBuff:
    return ActiveBuff(id=AID_BUFF, rounds_left=4800, source_id="pc-2", data={"hp": bonus})


def test_long_rest_ends_aid_and_heals_to_the_normal_maximum(make_character, srd):
    c = aided(make_character, hp=3)

    rested, outcome = long_rest(c, srd)

    assert (rested.hp_max, rested.hp, rested.buffs) == (20, 20, [])
    assert outcome.hp_gained == 17  # с 3 до обычного максимума 20, а не до 25


def test_long_rest_ends_aid_even_when_the_character_is_at_full_boosted_health(make_character, srd):
    c = aided(make_character)

    rested, outcome = long_rest(c, srd)

    assert (rested.hp_max, rested.hp) == (20, 20)
    assert outcome.hp_gained == 0


def test_short_rest_keeps_aid(make_character, srd):
    c = aided(make_character, hp=10)

    rested, _ = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert (rested.hp_max, [b.id for b in rested.buffs]) == (25, [AID_BUFF])


# --- дыхание драконорождённого возвращается длинным отдыхом ---


def test_long_rest_restores_breath_weapon_uses_of_a_dragonborn(make_character, srd):
    c = make_character(species="dragonborn", resources={BREATH_WEAPON_TRAIT: 0})

    rested, outcome = long_rest(c, srd)

    assert rested.resources[BREATH_WEAPON_TRAIT] == 2  # бонус мастерства 3-го уровня
    assert BREATH_WEAPON_TRAIT in outcome.restored


def test_short_rest_does_not_restore_breath_weapon(make_character, srd):
    c = make_character(species="dragonborn", resources={BREATH_WEAPON_TRAIT: 0})

    rested, outcome = short_rest(c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)

    assert rested.resources[BREATH_WEAPON_TRAIT] == 0
    assert outcome.restored == []


def test_long_rest_without_a_breath_weapon_record_changes_nothing(make_character, srd):
    c = make_character(species="dragonborn", resources={})

    rested, outcome = long_rest(c, srd)

    assert rested.resources == {}  # нет записи — все использования целы
    assert BREATH_WEAPON_TRAIT not in outcome.restored


# --- Магическое восстановление волшебника ---


def depleted_wizard(make_character, **overrides) -> Character:
    fields = {
        "class_id": "wizard",
        "spell_slots_max": {1: 4, 2: 2},
        "spell_slots": {1: 0, 2: 0},
        "resources": {"arcane_recovery": 1},
    }
    return make_character(**(fields | overrides))


def test_arcane_recovery_returns_slots_and_spends_the_use(make_character, srd):
    c = depleted_wizard(make_character)  # 3-й уровень: сумма уровней до 2

    recovered, outcome = arcane_recovery(c, [2], srd)

    assert recovered.spell_slots == {1: 0, 2: 1}
    assert recovered.resources["arcane_recovery"] == 0
    assert (outcome.character_id, outcome.hp_gained, outcome.hit_dice_spent) == ("pc-1", 0, 0)
    assert (outcome.rolls, outcome.restored) == ([], ["spell_slots"])


def test_arcane_recovery_can_return_several_small_slots(make_character, srd):
    recovered, _ = arcane_recovery(depleted_wizard(make_character), [1, 1], srd)

    assert recovered.spell_slots == {1: 2, 2: 0}


def wizard_srd(srd: SrdContent, level: int) -> SrdContent:
    """Данные SRD, где у волшебника этого уровня есть использование Магического восстановления."""
    wizard = srd.classes["wizard"]
    table = {**wizard.resources, "arcane_recovery": {level: 1}}
    changed = wizard.model_copy(update={"resources": table})
    return srd.model_copy(update={"classes": {**srd.classes, "wizard": changed}})


@pytest.mark.parametrize(
    ("level", "levels"),
    [(1, [1]), (2, [1]), (3, [2]), (3, [1, 1]), (4, [2]), (5, [3]), (5, [2, 1]), (11, [5, 1])],
)
def test_arcane_recovery_allows_up_to_half_the_wizard_level_rounded_up(
    make_character, srd, level, levels
):
    # Волшебник уровня L возвращает ячейки суммарным уровнем до ceil(L / 2).
    c = depleted_wizard(
        make_character, level=level, spell_slots_max=dict.fromkeys(range(1, 6), 3), spell_slots={}
    )

    recovered, _ = arcane_recovery(c, levels, wizard_srd(srd, level))

    assert sum(lvl * n for lvl, n in recovered.spell_slots.items()) == sum(levels)


@pytest.mark.parametrize(
    ("level", "levels"),
    [(1, [2]), (2, [2]), (3, [1, 1, 1]), (3, [2, 1]), (4, [3]), (5, [3, 1]), (11, [5, 2])],
)
def test_arcane_recovery_refuses_more_than_half_the_level_without_changes(
    make_character, srd, level, levels
):
    c = depleted_wizard(
        make_character, level=level, spell_slots_max=dict.fromkeys(range(1, 6), 3), spell_slots={}
    )
    snapshot = c.model_copy(deep=True)

    with pytest.raises(ValueError, match="суммарный уровень"):
        arcane_recovery(c, levels, wizard_srd(srd, level))

    assert c == snapshot


def test_arcane_recovery_never_returns_slots_of_the_sixth_level_or_higher(make_character, srd):
    c = depleted_wizard(make_character, level=12, spell_slots_max={6: 1}, spell_slots={6: 0})

    with pytest.raises(ValueError, match="6-го уровня"):
        arcane_recovery(c, [6], wizard_srd(srd, 12))  # сумма 6 допустима для 12-го уровня


def test_arcane_recovery_works_once_until_a_long_rest(make_character, srd):
    first, _ = arcane_recovery(depleted_wizard(make_character), [2], srd)

    with pytest.raises(ValueError, match="уже использовано"):
        arcane_recovery(first, [1], srd)

    rested, _ = long_rest(first, srd)
    assert rested.resources["arcane_recovery"] == 1


def test_arcane_recovery_is_only_for_classes_that_have_it(make_character, srd):
    cleric = make_character(class_id="cleric", spell_slots_max={1: 4}, spell_slots={1: 0})

    with pytest.raises(ValueError, match="Магического восстановления"):
        arcane_recovery(cleric, [1], srd)


def test_arcane_recovery_cannot_exceed_the_slot_maximum(make_character, srd):
    c = depleted_wizard(make_character, spell_slots={1: 3, 2: 2})

    with pytest.raises(ValueError, match="максимум"):
        arcane_recovery(c, [1, 1], srd)  # второй раз ячейка 1-го уровня уже не помещается
    with pytest.raises(ValueError, match="максимум"):
        arcane_recovery(c, [2], srd)


def test_arcane_recovery_refuses_slot_levels_the_wizard_does_not_have(make_character, srd):
    with pytest.raises(ValueError, match="нет ячеек"):
        arcane_recovery(depleted_wizard(make_character), [3], srd)


@pytest.mark.parametrize("levels", [[], [0], [-1]])
def test_arcane_recovery_needs_at_least_one_real_slot_level(make_character, srd, levels):
    with pytest.raises(ValueError, match="ячейк"):
        arcane_recovery(depleted_wizard(make_character), levels, srd)


def test_arcane_recovery_does_not_change_the_original(make_character, srd):
    c = depleted_wizard(make_character)
    snapshot = c.model_copy(deep=True)

    arcane_recovery(c, [2], srd)

    assert c == snapshot


def test_arcane_recovery_of_a_dead_wizard_changes_nothing(make_character, srd):
    c = dead(
        make_character,
        class_id="wizard",
        spell_slots_max={2: 2},
        spell_slots={2: 0},
        resources={"arcane_recovery": 1},
    )

    recovered, outcome = arcane_recovery(c, [2], srd)

    assert recovered == c
    assert outcome.restored == []


# --- короткий привал по кнопке: кость хитов по одной и конец отдыха (DND-2, B9) ---


@pytest.mark.parametrize("seed", range(5))
def test_spending_a_hit_die_heals_by_the_class_die_plus_constitution(make_character, srd, seed):
    c = make_character(class_id="fighter", scores={CON: 14}, hp_max=30, hp=5, hit_dice=3)
    face = SeededRng(seed).randint(1, 10)  # тот же seed — та же грань к10 воина

    rested, rolled = spend_hit_die(c, srd, SeededRng(seed))

    assert (rolled.expr, rolled.rolls, rolled.total) == ("1d10+2", [face], face + 2)
    assert rolled.reason == "Короткий отдых: кость хитов"
    assert rested.hp == 5 + face + 2
    assert rested.hit_dice == 2


def test_spending_a_hit_die_rolls_the_hit_die_of_the_class(make_character, srd):
    wizard = make_character(class_id="wizard", hp_max=20, hp=1)

    _, rolled = spend_hit_die(wizard, srd, SeededRng(3))

    assert rolled.expr == "1d6+0"


@pytest.mark.parametrize("seed", range(5))
def test_spending_a_hit_die_never_heals_above_the_maximum_but_still_spends_it(
    make_character, srd, seed
):
    c = make_character(hp_max=20, hp=19, hit_dice=2)

    rested, _ = spend_hit_die(c, srd, SeededRng(seed))

    assert (rested.hp, rested.hit_dice) == (20, 1)


def test_spending_a_hit_die_heals_up_to_the_maximum_raised_by_aid(make_character, srd):
    c = aided(make_character, hp=24)  # «Подмога» подняла максимум с 20 до 25

    rested, _ = spend_hit_die(c, srd, SeededRng(1))

    assert (rested.hp, rested.hp_max) == (25, 25)
    assert [b.id for b in rested.buffs] == [AID_BUFF]


@pytest.mark.parametrize("seed", range(10))
def test_a_hit_die_heals_at_least_one_hit_point_even_with_a_terrible_constitution(
    make_character, srd, seed
):
    # Телосложение 1 даёт −5, а кость волшебника — к6: итог броска не больше 1 при любом seed.
    c = make_character(class_id="wizard", scores={CON: 1}, hp_max=20, hp=10)

    rested, rolled = spend_hit_die(c, srd, SeededRng(seed))

    assert rolled.total <= 1  # бросок записан честно, даже отрицательный
    assert rested.hp == 11


def test_a_hit_die_wakes_up_a_character_at_zero_hit_points(make_character, srd):
    c = knocked_out(make_character, hp_max=20, hit_dice=2)

    rested, _ = spend_hit_die(c, srd, SeededRng(2))

    assert rested.hp >= 1
    assert rested.conditions == []
    assert rested.death_saves == DeathSaves()


def test_without_hit_dice_spending_one_is_a_rest_error_and_rolls_nothing(make_character, srd):
    c = make_character(hp=5, hit_dice=0)
    rng = ScriptedRng([5])

    with pytest.raises(RestError, match="Костей хитов не осталось"):
        spend_hit_die(c, srd, rng)

    assert rng.randint(1, 10) == 5  # кость не брошена


def test_a_dead_character_cannot_spend_a_hit_die(make_character, srd):
    c = dead(make_character, hp_max=20, hit_dice=3)
    rng = ScriptedRng([5])

    with pytest.raises(RestError, match="Погибший"):
        spend_hit_die(c, srd, rng)

    assert rng.randint(1, 10) == 5


def test_a_rest_error_is_a_value_error():
    # Прежние обработчики ловят `ValueError`: отказ правил привала не должен пройти мимо них.
    assert issubclass(RestError, ValueError)


def test_spending_a_hit_die_leaves_the_original_untouched(make_character, srd):
    c = make_character(hp_max=20, hp=5, resources={"second_wind": 0})
    before = c.model_copy(deep=True)

    rested, _ = spend_hit_die(c, srd, SeededRng(4))

    assert c == before
    assert rested is not c
    assert rested.resources == {"second_wind": 0}  # особенности возвращает конец отдыха


def test_the_end_of_a_short_rest_restores_short_rest_features_only(make_character, srd):
    c = make_character(
        class_id="fighter", hp_max=20, hp=5, resources={"second_wind": 0, "action_surge": 0}
    )

    rested, outcome = finish_short_rest(c, srd)

    assert rested.resources == {"second_wind": 1, "action_surge": 1}
    assert (rested.hp, rested.hit_dice) == (5, 3)
    assert outcome == RestOutcome(
        character_id="pc-1",
        hp_gained=0,
        hit_dice_spent=0,
        rolls=[],
        restored=["second_wind", "action_surge"],
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"class_id": "wizard", "spell_slots_max": {1: 2}, "spell_slots": {1: 0}},
        {"class_id": "wizard", "resources": {"arcane_recovery": 0}},
        {"class_id": "barbarian", "resources": {"rage": 1}},
        {"species": "orc", "resources": {RELENTLESS_ENDURANCE_TRAIT: 0}},
        {"species": "dragonborn", "resources": {BREATH_WEAPON_TRAIT: 0}},
        {"buffs": [ActiveBuff(id=AID_BUFF, rounds_left=4800, source_id="pc-2", data={"hp": 5})]},
    ],
    ids=["slots", "arcane-recovery", "rage", "relentless", "breath", "aid"],
)
def test_the_end_of_a_short_rest_is_the_short_rest_without_hit_dice(make_character, srd, fields):
    c = make_character(hp=7, **fields)

    assert finish_short_rest(c, srd) == short_rest(
        c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd
    )


def test_the_end_of_a_short_rest_gives_a_dead_character_nothing(make_character, srd):
    c = dead(make_character, hp_max=20, resources={"second_wind": 0})

    rested, outcome = finish_short_rest(c, srd)

    assert rested == c and rested is not c
    assert outcome == RestOutcome(
        character_id="pc-1", hp_gained=0, hit_dice_spent=0, rolls=[], restored=[]
    )


@pytest.mark.parametrize("seed", range(10))
def test_short_rest_is_hit_dice_one_by_one_and_then_the_end_of_the_rest(make_character, srd, seed):
    c = make_character(scores={CON: 12}, hp_max=30, hp=3, resources={"second_wind": 0})
    rng = SeededRng(seed)
    once, first = spend_hit_die(c, srd, rng)
    twice, second = spend_hit_die(once, srd, rng)
    expected, end = finish_short_rest(twice, srd)

    rested, outcome = short_rest(c, spend_hit_dice=2, rng=SeededRng(seed), content=srd)

    assert rested == expected
    assert outcome == RestOutcome(
        character_id="pc-1",
        hp_gained=expected.hp - c.hp,
        hit_dice_spent=2,
        rolls=[first, second],
        restored=end.restored,
    )


# --- финальное покрытие B9: границы кости хитов и конца короткого отдыха ---


@pytest.mark.parametrize(
    ("class_id", "hit_die"),
    [
        ("barbarian", 12),
        ("fighter", 10),
        ("ranger", 10),
        ("rogue", 8),
        ("cleric", 8),
        ("wizard", 6),
    ],
)
def test_a_hit_die_of_every_class_is_exactly_the_die_of_that_class(
    make_character, srd, class_id, hit_die
):
    c = make_character(class_id=class_id, hp_max=40, hp=1)

    rested, rolled = spend_hit_die(c, srd, ScriptedRng([hit_die]))

    assert (rolled.expr, rolled.total) == (f"1d{hit_die}+0", hit_die)
    assert (rested.hp, rested.hit_dice) == (1 + hit_die, 2)
    # Высшая грань кости принята, а грань на единицу выше `ScriptedRng` отверг: кость ровно та.
    with pytest.raises(ValueError, match="вне диапазона"):
        spend_hit_die(c, srd, ScriptedRng([hit_die + 1]))


@pytest.mark.parametrize(
    ("con", "face", "expr", "total", "healed"),
    [
        (8, 5, "1d10-1", 4, 4),  # итог положительный — лечит ровно итог
        (6, 4, "1d10-2", 2, 2),
        (6, 3, "1d10-2", 1, 1),  # итог 1 — сама граница
        (6, 2, "1d10-2", 0, 1),  # итог 0 — лечит 1, а не 0
        (1, 1, "1d10-5", -4, 1),  # итог отрицательный — всё равно 1
    ],
)
def test_a_hit_die_with_a_negative_constitution_heals_its_total_but_never_less_than_one(
    make_character, srd, con, face, expr, total, healed
):
    c = make_character(scores={CON: con}, hp_max=30, hp=10)

    rested, rolled = spend_hit_die(c, srd, ScriptedRng([face]))

    assert (rolled.expr, rolled.total) == (expr, total)  # бросок записан честно
    assert rested.hp == 10 + healed


def test_even_the_worst_hit_die_wakes_up_a_character_at_zero_hit_points(make_character, srd):
    # Минимум 1 HP за кость значит, что любая кость поднимает лежащего на 0 HP.
    c = knocked_out(make_character, scores={CON: 6}, hp_max=20, hit_dice=2)

    rested, rolled = spend_hit_die(c, srd, ScriptedRng([1]))

    assert rolled.total == -1
    assert (rested.hp, rested.hit_dice) == (1, 1)
    assert rested.conditions == []
    assert rested.death_saves == DeathSaves()


@pytest.mark.parametrize("with_aid", [False, True], ids=["normal-maximum", "aid-maximum"])
def test_a_hit_die_at_full_health_heals_nothing_but_is_still_rolled_and_spent(
    make_character, srd, with_aid
):
    c = aided(make_character) if with_aid else make_character(hp_max=20, hp=20)
    rng = ScriptedRng([7, 3])

    rested, rolled = spend_hit_die(c, srd, rng)

    assert rolled.total == 7
    assert (rested.hp, rested.hp_max, rested.hit_dice) == (c.hp, c.hp_max, 2)
    assert rng.randint(1, 10) == 3  # брошена ровно одна кость


def test_a_dead_character_without_hit_dice_hears_about_death_first(make_character, srd):
    c = dead(make_character, hit_dice=0)

    with pytest.raises(RestError, match="Погибший"):
        spend_hit_die(c, srd, ScriptedRng([]))


def test_hit_dice_are_spent_one_by_one_until_none_are_left(make_character, srd):
    c = make_character(hp_max=40, hp=1, hit_dice=2)
    rng = ScriptedRng([3, 4, 5])

    once, _ = spend_hit_die(c, srd, rng)
    twice, _ = spend_hit_die(once, srd, rng)

    assert (once.hp, once.hit_dice) == (4, 1)
    assert (twice.hp, twice.hit_dice) == (8, 0)
    with pytest.raises(RestError, match="Костей хитов не осталось"):
        spend_hit_die(twice, srd, rng)
    assert rng.randint(1, 10) == 5  # третья кость не брошена


def test_a_hit_die_changes_only_hit_points_and_hit_dice(make_character, srd):
    c = make_character(
        class_id="wizard",
        hp_max=20,
        hp=5,
        temp_hp=4,
        spell_slots_max={1: 4},
        spell_slots={1: 1},
        resources={"arcane_recovery": 0},
        concentration="bless",
        conditions=[ActiveCondition(id="poisoned", rounds_left=3)],
        buffs=[bless_buff()],
        inspiration=True,
    )

    rested, _ = spend_hit_die(c, srd, ScriptedRng([4]))

    # Кость лечит настоящие хиты: временные, ячейки, ресурсы, эффекты и состояния прежние.
    assert rested == c.model_copy(update={"hp": 9, "hit_dice": 2})


# Все ресурсы «до короткого отдыха» и два ресурса только длинного отдыха. Таблица с тремя
# использованиями на 3-м уровне показывает, возвращается одно использование или все.
SHORT_REST_RESOURCE_CASES = [
    ("fighter", "second_wind", 1),  # одно использование
    ("barbarian", "rage", 1),
    ("cleric", "channel_divinity", 1),
    ("fighter", "action_surge", 3),  # все использования
    ("ranger", "hunters_mark", 0),  # только длинный отдых
    ("wizard", "arcane_recovery", 0),
]


def test_the_resource_cases_name_every_short_rest_resource():
    # Новый ресурс «до короткого отдыха» должен попасть в таблицу проверок конца отдыха.
    named = {resource for _, resource, restored_to in SHORT_REST_RESOURCE_CASES if restored_to}
    assert named == SHORT_REST_ONE_USE | SHORT_REST_ALL_USES


@pytest.mark.parametrize(("class_id", "resource", "restored_to"), SHORT_REST_RESOURCE_CASES)
def test_the_end_of_a_short_rest_restores_each_resource_by_its_own_rule(
    make_character, srd, class_id, resource, restored_to
):
    content = with_resource_table(srd, class_id, resource, {3: 3})
    c = make_character(class_id=class_id, resources={resource: 0})

    rested, outcome = finish_short_rest(c, content)

    assert rested.resources == {resource: restored_to}
    assert outcome.restored == ([resource] if restored_to else [])


def test_the_end_of_a_short_rest_neither_heals_nor_wakes_nor_returns_hit_dice(make_character, srd):
    # Лежащего на 0 HP конец короткого отдыха не поднимает: поднимает только кость хитов.
    c = knocked_out(
        make_character,
        hp_max=25,
        hit_dice=0,
        temp_hp=3,
        resources={"second_wind": 0},
        buffs=[aid_buff(5)],
    )

    rested, _ = finish_short_rest(c, srd)

    assert rested == c.model_copy(update={"resources": {"second_wind": 1}})


def test_short_rest_applies_the_minimum_to_each_die_and_not_to_their_sum(make_character, srd):
    c = make_character(scores={CON: 6}, hp_max=30, hp=10)

    rested, outcome = short_rest(c, spend_hit_dice=2, rng=ScriptedRng([1, 5]), content=srd)

    assert [r.total for r in outcome.rolls] == [-1, 3]
    assert (rested.hp, outcome.hp_gained) == (14, 4)  # 1 + 3, а не −1 + 3 = 2


def test_short_rest_counts_only_the_hit_points_really_gained_when_the_dice_overflow(
    make_character, srd
):
    c = make_character(scores={CON: 6}, hp_max=20, hp=15)

    rested, outcome = short_rest(c, spend_hit_dice=3, rng=ScriptedRng([8, 1, 1]), content=srd)

    # Первая кость упирается в максимум (5 из 6), остальные «лечат 1», но лечить уже некого.
    assert [r.total for r in outcome.rolls] == [6, -1, -1]
    assert (rested.hp, outcome.hp_gained) == (20, 5)
    assert (rested.hit_dice, outcome.hit_dice_spent) == (0, 3)
