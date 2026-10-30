"""Сквозные проверки на встроенных данных SRD: создание → производные значения →
проверки → отдых на одном и том же персонаже.

Модульные тесты проверяют каждую часть отдельно. Здесь проверяются стыки: что персонаж,
собранный `creation`, правильно читают `character`, `checks` и `rest`, и что константы кода
(id черт, эффектов, ресурсов, состояний) совпадают с реальными данными SRD. Случайность —
только через `SeededRng` и `ScriptedRng`.
"""

import pytest

from dnd_engine.abilities import SKILL_ABILITY, Ability, Skill, modifier
from dnd_engine.character import (
    ActiveBuff,
    ActiveCondition,
    Character,
    DeathSaves,
    armor_class,
    passive_perception,
    prof_bonus,
    save_bonus,
    skill_bonus,
    spell_attack_bonus,
    spell_save_dc,
    weapon_attack,
)
from dnd_engine.checks import (
    BLESS_BUFF,
    CHECK_DISADVANTAGE_CONDITIONS,
    SAVE_DISADVANTAGE_CONDITIONS,
    ContestSide,
    ability_check,
    contest,
    saving_throw,
)
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import (
    DWARVEN_TOUGHNESS_TRAIT,
    EXPERTISE_FEATURE,
    SKILLFUL_TRAIT,
    STANDARD_ARRAY,
    TRAIT_SKILLS,
    quick_character,
    random_character,
)
from dnd_engine.dice import Advantage, parse
from dnd_engine.rest import (
    RESOURCEFUL_TRAIT,
    SHORT_REST_ALL_USES,
    SHORT_REST_ONE_USE,
    long_rest,
    short_rest,
)
from dnd_engine.rng import ScriptedRng, SeededRng

STR, DEX, CON, INT, WIS, CHA = Ability
NONE = Advantage.NONE

# Бонус мастерства на 3-м уровне: все персонажи среза именно такого уровня.
PROFICIENCY = 2
# Ресурсы, которые короткий отдых не возвращает; остальные перечислены в самом `rest`.
LONG_REST_ONLY_RESOURCES = {"hunters_mark", "arcane_recovery"}


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture(scope="module")
def created(srd) -> list[Character]:
    """Персонажи для сквозных проверок: быстрые (каждый класс × каждый вид) и случайные.

    Тесты их только читают; если нужно менять — через `model_copy(deep=True)`."""
    quick = [
        quick_character(
            class_id, species_id, srd, name="Тест", concept="тест", character_id=f"q-{class_id}"
        )
        for class_id in srd.classes
        for species_id in srd.species
    ]
    randomly = [
        random_character(SeededRng(seed), srd, taken_classes=set(), character_id=f"r-{seed}")
        for seed in range(1, 301)
    ]
    return [*quick, *randomly]


def assert_no_dice_left(rng: ScriptedRng) -> None:
    """Сценарий потратил ровно заданные кости: лишних бросков не было."""
    with pytest.raises(ValueError, match="закончилась"):
        rng.randint(1, 100)


# --- созданный персонаж и производные значения ---


def test_created_characters_cover_every_class_and_species(srd, created):
    pairs = {(c.class_id, c.species) for c in created}

    assert pairs == {(cls, sp) for cls in srd.classes for sp in srd.species}
    assert len(created) == len(srd.classes) * len(srd.species) + 300


def test_created_hit_points_follow_the_formula_from_the_spec(srd, created):
    for c in created:
        hit_die = srd.classes[c.class_id].hit_die
        expected = hit_die + 2 * (hit_die // 2 + 1) + 3 * modifier(c.scores[CON])
        expected += 3 if c.species == "dwarf" else 0  # дварфийская выносливость: +1 за уровень

        assert (c.hp_max, c.hp) == (expected, expected), c.id
        assert (c.level, c.hit_dice_max, c.hit_dice) == (3, 3, 3), c.id


def test_created_scores_are_standard_array_plus_three_points_of_background_bonus(created):
    for c in created:
        assert set(c.scores) == set(Ability), c.id
        assert sum(c.scores.values()) == sum(STANDARD_ARRAY) + 3, c.id
        assert all(8 <= score <= 17 for score in c.scores.values()), c.id


def test_created_inventory_has_known_items_and_at_most_one_piece_per_slot(srd, created):
    for c in created:
        item_ids = [entry.item_id for entry in c.inventory]
        worn = [srd.items[entry.item_id] for entry in c.inventory if entry.equipped]

        assert len(item_ids) == len(set(item_ids)), c.id  # одинаковые предметы — одна стопка
        assert set(item_ids) <= set(srd.items), c.id
        assert all(entry.qty >= 1 for entry in c.inventory), c.id
        assert sum(item.weapon is not None for item in worn) == 1, c.id
        assert sum(i.armor is not None and i.armor.kind != "shield" for i in worn) <= 1, c.id
        assert sum(i.armor is not None and i.armor.kind == "shield" for i in worn) <= 1, c.id


def test_created_proficiencies_combine_background_species_and_class(srd, created):
    for c in created:
        cls, background = srd.classes[c.class_id], srd.backgrounds[c.background]
        automatic = set(background.skills)
        if c.species == "elf":
            automatic.add(Skill.PERCEPTION)  # острые чувства
        human_extra = 1 if c.species == "human" else 0  # умелость

        held = c.skill_proficiencies
        assert len(held) == len(set(held)), c.id
        assert automatic <= set(held), c.id
        assert len(held) == len(automatic) + cls.skill_choices + human_extra, c.id
        assert set(c.expertise) <= set(held), c.id
        assert len(c.expertise) == (2 if c.class_id == "rogue" else 0), c.id
        assert c.save_proficiencies == list(cls.saving_throws), c.id


def test_created_bonuses_follow_proficiency_and_expertise(created):
    for c in created:
        assert prof_bonus(c) == PROFICIENCY, c.id
        for skill in Skill:
            multiplier = 2 if skill in c.expertise else int(skill in c.skill_proficiencies)
            expected = modifier(c.scores[SKILL_ABILITY[skill]]) + multiplier * PROFICIENCY
            assert skill_bonus(c, skill) == expected, (c.id, skill)
        for ability in Ability:
            expected = modifier(c.scores[ability]) + (
                PROFICIENCY if ability in c.save_proficiencies else 0
            )
            assert save_bonus(c, ability) == expected, (c.id, ability)
        assert passive_perception(c) == 10 + skill_bonus(c, Skill.PERCEPTION), c.id


def test_created_spellcasting_matches_class_tables(srd, created):
    for c in created:
        spellcasting = srd.classes[c.class_id].spellcasting
        if spellcasting is None:
            assert (c.cantrips, c.prepared_spells) == ([], []), c.id
            assert (c.spell_slots, c.spell_slots_max) == ({}, {}), c.id
            assert spell_save_dc(c, srd) is None, c.id
            assert spell_attack_bonus(c, srd) is None, c.id
            continue
        ability_mod = modifier(c.scores[spellcasting.ability])
        assert spell_save_dc(c, srd) == 8 + PROFICIENCY + ability_mod, c.id
        assert spell_attack_bonus(c, srd) == PROFICIENCY + ability_mod, c.id
        assert len(c.cantrips) == spellcasting.cantrips_known[3], c.id
        assert len(c.prepared_spells) == spellcasting.prepared[3], c.id
        assert c.spell_slots == c.spell_slots_max == spellcasting.slots[3], c.id
        top_level = max(spellcasting.slots[3])
        for spell_id in c.cantrips:
            spell = srd.spells[spell_id]
            assert spell.level == 0 and spellcasting.spell_list in spell.classes, c.id
        for spell_id in c.prepared_spells:
            spell = srd.spells[spell_id]
            assert 1 <= spell.level <= top_level, (c.id, spell_id)
            assert spellcasting.spell_list in spell.classes, (c.id, spell_id)


def test_every_weapon_of_a_created_character_has_a_valid_attack(srd, created):
    for c in created:
        weapon_ids = [e.item_id for e in c.inventory if srd.items[e.item_id].weapon is not None]
        assert weapon_ids, c.id
        for item_id in weapon_ids:
            attack = weapon_attack(c, item_id, srd)
            # Стартовое оружие классов — из их владений: бонус мастерства всегда есть.
            expected = modifier(c.scores[attack.ability]) + PROFICIENCY
            if c.fighting_style == "archery" and attack.range != "melee":
                expected += 2
            assert attack.bonus == expected, (c.id, item_id)
            assert parse(attack.damage).terms, (c.id, item_id)  # урон разбирается движком костей
        assert 9 <= armor_class(c, srd) <= 20, c.id


def test_created_character_survives_json_round_trip(created):
    for c in created:
        assert Character.model_validate_json(c.model_dump_json()) == c, c.id


# --- отдых созданного персонажа ---


def test_rests_change_nothing_in_a_fresh_character_but_human_inspiration(srd, created):
    for c in created:
        long_rested, long_outcome = long_rest(c, srd)
        short_rested, short_outcome = short_rest(
            c, spend_hit_dice=0, rng=ScriptedRng([]), content=srd
        )

        # Человек получает вдохновение от «Находчивости»; всё остальное уже на максимуме.
        assert long_rested == c.model_copy(update={"inspiration": c.species == "human"}), c.id
        assert (long_outcome.restored, long_outcome.hp_gained) == ([], 0), c.id
        assert short_rested == c, c.id
        assert (short_outcome.restored, short_outcome.hp_gained) == ([], 0), c.id


def test_long_rest_returns_a_worn_out_character_to_the_state_of_creation(srd, created):
    for c in created:
        worn_out = c.model_copy(
            deep=True,
            update={
                "hp": 1,
                "temp_hp": 5,
                "hit_dice": 0,
                "spell_slots": dict.fromkeys(c.spell_slots, 0),
                "resources": dict.fromkeys(c.resources, 0),
                "death_saves": DeathSaves(successes=1, failures=2),
                "concentration": "bless",
                "conditions": [ActiveCondition(id="poisoned", rounds_left=3)],
                "buffs": [ActiveBuff(id="bless", rounds_left=10, source_id="pc-x")],
            },
        )

        rested, outcome = long_rest(worn_out, srd)

        # Возвращаются все кости хитов, как при создании; вдохновение даёт только человек.
        assert rested == c.model_copy(update={"inspiration": c.species == "human"}), c.id
        assert rested.hit_dice == c.hit_dice_max, c.id
        assert outcome.hp_gained == c.hp_max - 1, c.id
        expected_restored = {*c.resources, *(["spell_slots"] if c.spell_slots else [])}
        assert set(outcome.restored) == expected_restored, c.id


def test_short_rest_of_a_created_character_spends_exactly_the_requested_dice(srd, created):
    for c in created:
        hurt = c.model_copy(update={"hp": 1})
        rng = ScriptedRng([1] * 3)

        rested, outcome = short_rest(hurt, spend_hit_dice=3, rng=rng, content=srd)

        assert_no_dice_left(rng)
        assert [r.expr for r in outcome.rolls] == [
            f"1d{srd.classes[c.class_id].hit_die}{modifier(c.scores[CON]):+d}"
        ] * 3, c.id
        assert (rested.hit_dice, outcome.hit_dice_spent) == (0, 3), c.id
        assert 1 <= rested.hp <= c.hp_max, c.id


# --- сквозные сценарии с точными числами ---


def test_gnome_wizard_from_mage_armor_to_long_rest(srd):
    wizard = quick_character(
        "wizard", "gnome", srd, name="Фенвик", concept="учёный", character_id="pc-1"
    )
    # Инт 17 (+3), Лов 13 (+1), Тел 15 (+2), БМ +2; Хиты: 6 + 2·4 + 3·2 = 20.
    assert (wizard.hp_max, armor_class(wizard, srd)) == (20, 11)

    # «Доспехи мага» накладываются по id эффекта из данных заклинания: 13 + Лов.
    mage_armor = srd.spells["mage_armor"]
    armored = wizard.model_copy(
        update={
            "buffs": [
                ActiveBuff(
                    id=mage_armor.effect.buff,
                    rounds_left=mage_armor.duration_rounds,
                    source_id=wizard.id,
                )
            ]
        }
    )
    assert armor_class(armored, srd) == 14

    # Спасбросок Инт: владение (+3 +2 = +5) и преимущество гнома (две к20, берётся старшая).
    rng = ScriptedRng([4, 17, 15, 3])
    illusion = saving_throw(armored, INT, dc=15, advantage=NONE, rng=rng, reason="иллюзия")
    assert (illusion.roll.rolls, illusion.roll.natural) == ([4, 17], 17)
    assert (illusion.bonus, illusion.total, illusion.success) == (5, 22, True)

    # Магия с владением: 15 + 5 = 20, ровно Сл.
    arcana = ability_check(
        armored, INT, skill=Skill.ARCANA, dc=20, advantage=NONE, rng=rng, reason="руна"
    )
    assert (arcana.bonus, arcana.total, arcana.success) == (5, 20, True)

    # Все ячейки потрачены, хитов осталось 4; короткий отдых лечит 1к6 + Тел, ячейки не трогает.
    hurt = armored.model_copy(update={"hp": 4, "spell_slots": {1: 0, 2: 0}})
    rested, short = short_rest(hurt, spend_hit_dice=1, rng=rng, content=srd)
    assert_no_dice_left(rng)
    assert (rested.hp, rested.hit_dice, short.hp_gained) == (4 + 3 + 2, 2, 5)
    assert rested.spell_slots == {1: 0, 2: 0}
    assert short.restored == []  # Магическое восстановление короткий отдых не возвращает

    # Длинный отдых: хиты и ячейки полностью, кость хитов возвращается, доспехи мага кончились.
    morning, long = long_rest(rested, srd)
    assert (morning.hp, morning.hit_dice, morning.spell_slots) == (20, 3, {1: 4, 2: 2})
    assert morning.buffs == []
    assert armor_class(morning, srd) == 11
    assert long.restored == ["spell_slots"]
    assert morning.inspiration is False  # вдохновение даёт только человек


def test_halfling_rogue_luck_expertise_and_finesse_weapon(srd):
    rogue = quick_character(
        "rogue", "halfling", srd, name="Пип", concept="карманник", character_id="pc-2"
    )
    # Лов 17 (+3), БМ +2: Скрытность с владением +5, Акробатика с компетентностью +3 + 4 = +7.
    assert skill_bonus(rogue, Skill.STEALTH) == 5
    assert skill_bonus(rogue, Skill.ACROBATICS) == 7
    assert armor_class(rogue, srd) == 14  # кожаный доспех 11 + Лов

    # Удача полурослика: единица перебрасывается, оба броска видны; без неё 1 + 5 = 6 — провал.
    rng = ScriptedRng([1, 11, 8])
    stealth = ability_check(
        rogue, DEX, skill=Skill.STEALTH, dc=15, advantage=NONE, rng=rng, reason="прокрасться"
    )
    assert (stealth.roll.rolls, stealth.roll.natural) == ([1, 11], 11)
    assert (stealth.total, stealth.success) == (16, True)

    acrobatics = ability_check(
        rogue, DEX, skill=Skill.ACROBATICS, dc=15, advantage=NONE, rng=rng, reason="кувырок"
    )
    assert (acrobatics.bonus, acrobatics.total, acrobatics.success) == (7, 15, True)
    assert_no_dice_left(rng)

    # Короткий меч — фехтовальное оружие: Лов (+3) и мастерство (+2); у плута нет ресурсов.
    attack = weapon_attack(rogue, "shortsword", srd)
    assert (attack.ability, attack.bonus, attack.damage) == (DEX, 5, "1d6+3")
    assert short_rest(rogue, spend_hit_dice=0, rng=ScriptedRng([]), content=srd)[1].restored == []


def test_dwarf_cleric_with_bless_against_poison_and_channel_divinity(srd):
    cleric = quick_character(
        "cleric", "dwarf", srd, name="Брунгильда", concept="целитель", character_id="pc-3"
    )
    # Мдр 17 (+3), Тел 14 (+2): хиты 8 + 2·5 + 3·2 + 3 (дварф) = 27; КД: кольчужная рубаха + щит.
    assert cleric.hp_max == 27
    assert armor_class(cleric, srd) == 16
    assert spell_save_dc(cleric, srd) == 13  # 8 + 2 + 3

    # «Благословение» по id эффекта из данных. Спасбросок Тел не в списке владений (+2):
    # дварф бросает две к20 против яда, затем отдельная кость 1к4 благословения.
    bless = srd.spells["bless"]
    blessed = cleric.model_copy(
        update={
            "buffs": [
                ActiveBuff(
                    id=bless.effect.buff, rounds_left=bless.duration_rounds, source_id="pc-9"
                )
            ]
        }
    )
    rng = ScriptedRng([5, 14, 3])
    poison = saving_throw(
        blessed, CON, dc=15, advantage=NONE, rng=rng, reason="яд", against="poison"
    )
    assert poison.roll.rolls == [5, 14, 3]
    assert (poison.roll.natural, poison.bonus, poison.total, poison.success) == (14, 2, 19, True)
    assert_no_dice_left(rng)

    # Божественный канал потрачен: короткий отдых возвращает одно использование, ячейки — нет.
    spent = cleric.model_copy(
        update={"hp": 10, "resources": {"channel_divinity": 0}, "spell_slots": {1: 1, 2: 0}}
    )
    rng = ScriptedRng([4, 6])
    rested, outcome = short_rest(spent, spend_hit_dice=2, rng=rng, content=srd)
    assert_no_dice_left(rng)
    assert (rested.hp, rested.hit_dice) == (10 + (4 + 2) + (6 + 2), 1)
    assert rested.resources == {"channel_divinity": 1}
    assert rested.spell_slots == {1: 1, 2: 0}
    assert outcome.restored == ["channel_divinity"]


def test_elf_ranger_gets_perception_and_archery_from_creation(srd):
    ranger = quick_character(
        "ranger", "elf", srd, name="Лиэль", concept="лесной страж", character_id="pc-4"
    )
    # Мдр 14 (+2), Внимательность от эльфа: 10 + 2 + 2.
    assert Skill.PERCEPTION in ranger.skill_proficiencies
    assert passive_perception(ranger) == 14

    # Длинный лук: Лов 17 (+3) + мастерство (+2) + стиль «Стрельба» (+2); урон без стиля.
    attack = weapon_attack(ranger, "longbow", srd)
    assert (attack.ability, attack.bonus, attack.damage) == (DEX, 7, "1d8+3")
    assert armor_class(ranger, srd) == 15  # проклёпанный кожаный доспех 12 + Лов


def test_contest_between_created_characters_uses_their_skill_bonuses(srd):
    fighter = quick_character("fighter", "dwarf", srd, name="А", concept="а", character_id="pc-a")
    rogue = quick_character("rogue", "halfling", srd, name="Б", concept="б", character_id="pc-b")
    # Сила 17 (+3) и владение Атлетикой (+2) против Акробатики с компетентностью (+7).
    grappler = ContestSide(
        actor_id=fighter.id, label="Атлетика", bonus=skill_bonus(fighter, Skill.ATHLETICS)
    )
    slippery = ContestSide(
        actor_id=rogue.id, label="Акробатика", bonus=skill_bonus(rogue, Skill.ACROBATICS)
    )
    assert (grappler.bonus, slippery.bonus) == (5, 7)

    rng = ScriptedRng([12, 10])
    result = contest(grappler, slippery, rng=rng, reason="захват")

    # 12 + 5 = 17 и 10 + 7 = 17: ничья в пользу защищающегося.
    assert (result.a_roll.total, result.b_roll.total) == (17, 17)
    assert result.winner == "b"
    assert_no_dice_left(rng)


# --- константы кода и данные SRD ---


def test_rest_rules_know_every_class_resource_in_srd(srd):
    in_data = {name for cls in srd.classes.values() for name in cls.resources}

    # Новый ресурс в данных без решения, возвращает ли его короткий отдых, ломает этот тест.
    assert in_data == SHORT_REST_ONE_USE | SHORT_REST_ALL_USES | LONG_REST_ONLY_RESOURCES
    assert not SHORT_REST_ONE_USE & SHORT_REST_ALL_USES


@pytest.mark.parametrize(
    ("species_id", "trait_id"),
    [
        ("dwarf", DWARVEN_TOUGHNESS_TRAIT),
        ("human", SKILLFUL_TRAIT),
        ("human", RESOURCEFUL_TRAIT),
        *[("elf", trait_id) for trait_id in TRAIT_SKILLS],
    ],
)
def test_species_traits_used_by_the_code_exist_in_srd(srd, species_id, trait_id):
    assert trait_id in {trait.id for trait in srd.species[species_id].traits}


def test_rogue_has_the_expertise_feature_used_by_creation(srd):
    features = {f.id for f in srd.classes["rogue"].features if f.level <= 3}

    assert EXPERTISE_FEATURE in features


def test_condition_and_effect_ids_used_by_checks_exist_in_srd(srd):
    condition_ids = CHECK_DISADVANTAGE_CONDITIONS | set(SAVE_DISADVANTAGE_CONDITIONS)

    assert condition_ids <= set(srd.conditions)
    assert srd.spells["bless"].effect.buff == BLESS_BUFF
    assert srd.spells["mage_armor"].effect.buff == "mage_armor"
