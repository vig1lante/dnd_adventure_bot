from typing import get_args

import pytest
from pydantic import ValidationError

from dnd_engine.abilities import SKILL_ABILITY, Ability, Skill, modifier
from dnd_engine.character import (
    IMPROVISED_WEAPON_ID,
    ActiveBuff,
    ActiveCondition,
    AttackProfile,
    Character,
    DeathSaves,
    InventoryItem,
    Persona,
    ability_mod,
    armor_class,
    improvised_attack,
    passive_perception,
    prof_bonus,
    save_bonus,
    skill_bonus,
    spell_attack_bonus,
    spell_save_dc,
    weapon_attack,
)
from dnd_engine.content import ItemDef, MonsterAction, SrdContent, WeaponProps, load_srd
from dnd_engine.dice import parse

STR, DEX, CON, INT, WIS, CHA = Ability


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def gear(*item_ids: str, equipped: bool = True) -> list[InventoryItem]:
    """Инвентарь из перечисленных предметов, надетых или лежащих в рюкзаке."""
    return [InventoryItem(item_id=item_id, equipped=equipped) for item_id in item_ids]


# --- модель Character ---


def test_make_character_rejects_unknown_override(make_character):
    with pytest.raises(TypeError, match="expertice"):
        make_character(expertice=[Skill.STEALTH])


def test_scores_must_cover_every_ability(make_character):
    data = make_character().model_dump()
    del data["scores"][CHA]

    with pytest.raises(ValidationError, match="характеристик"):
        Character.model_validate(data)


def test_expertise_requires_proficiency(make_character):
    with pytest.raises(ValidationError, match="компетентность"):
        make_character(skill_proficiencies=[Skill.STEALTH], expertise=[Skill.PERCEPTION])


def test_defaults_are_not_shared_between_characters(make_character):
    first, second = make_character(), make_character()

    first.death_saves.failures = 2
    first.inventory.append(InventoryItem(item_id="dagger"))
    first.persona.concept = "бывший стражник"
    first.resources["rage"] = 1

    assert second.death_saves.failures == 0
    assert second.inventory == []
    assert second.persona.concept == ""
    assert second.resources == {}


def test_new_models_start_from_documented_defaults(make_character):
    assert (DeathSaves().successes, DeathSaves().failures, DeathSaves().stable) == (0, 0, False)
    entry = InventoryItem(item_id="dagger")
    assert (entry.qty, entry.equipped) == (1, False)
    condition = ActiveCondition(id="poisoned")
    assert (condition.rounds_left, condition.source) == (None, "")
    assert (condition.save, condition.dc) == (None, None)
    assert ActiveBuff(id="bless", rounds_left=None, source_id="pc-2").data == {}

    c = make_character()
    assert c.level == 3
    assert (c.temp_hp, c.inspiration, c.concentration) == (0, False, None)
    assert (c.fighting_style, c.player_id) == (None, None)
    assert c.death_saves == DeathSaves()
    assert c.persona == Persona()
    assert (c.expertise, c.cantrips, c.prepared_spells) == ([], [], [])
    assert (c.spell_slots, c.spell_slots_max, c.resources) == ({}, {}, {})
    assert (c.inventory, c.conditions, c.buffs) == ([], [], [])


def test_character_survives_json_round_trip(make_character):
    character = make_character(
        class_id="cleric",
        scores={WIS: 16},
        skill_proficiencies=[Skill.INSIGHT, Skill.MEDICINE],
        expertise=[Skill.MEDICINE],
        save_proficiencies=[WIS, CHA],
        spell_slots_max={1: 4, 2: 2},
        spell_slots={1: 3, 2: 2},
        cantrips=["sacred_flame"],
        prepared_spells=["bless", "cure_wounds"],
        concentration="bless",
        inventory=[
            InventoryItem(item_id="mace", equipped=True),
            InventoryItem(item_id="rations", qty=3),
        ],
        conditions=[ActiveCondition(id="poisoned", rounds_left=3, source="паук", save=CON, dc=12)],
        buffs=[ActiveBuff(id="bless", rounds_left=10, source_id="pc-2", data={"die": "1d4"})],
        resources={"channel_divinity": 1},
        persona=Persona(concept="монах-отшельник", flaws="упрям"),
        player_id="user-7",
    )

    restored = Character.model_validate_json(character.model_dump_json())

    assert restored == character


# --- модификаторы и бонус мастерства ---


@pytest.mark.parametrize(("score", "expected"), [(8, -1), (10, 0), (15, 2), (18, 4)])
def test_ability_mod_comes_from_score(make_character, score, expected):
    assert ability_mod(make_character(scores={DEX: score}), DEX) == expected


@pytest.mark.parametrize(("level", "expected"), [(1, 2), (3, 2), (5, 3), (9, 4)])
def test_prof_bonus_follows_level(make_character, level, expected):
    assert prof_bonus(make_character(level=level)) == expected


# --- навыки ---


def test_skill_without_proficiency_is_only_ability_modifier(make_character):
    assert skill_bonus(make_character(scores={WIS: 14}), Skill.PERCEPTION) == 2


def test_skill_with_proficiency_adds_proficiency_bonus(make_character):
    c = make_character(scores={WIS: 14}, skill_proficiencies=[Skill.PERCEPTION])

    assert skill_bonus(c, Skill.PERCEPTION) == 2 + 2


def test_proficiency_bonus_grows_with_level(make_character):
    c = make_character(level=5, scores={WIS: 14}, skill_proficiencies=[Skill.PERCEPTION])

    assert skill_bonus(c, Skill.PERCEPTION) == 2 + 3


def test_expertise_doubles_proficiency_bonus(make_character):
    c = make_character(
        scores={DEX: 16},
        skill_proficiencies=[Skill.STEALTH],
        expertise=[Skill.STEALTH],
    )

    assert skill_bonus(c, Skill.STEALTH) == 3 + 2 * 2


def test_proficiency_in_one_skill_does_not_leak_to_others(make_character):
    c = make_character(scores={WIS: 14}, skill_proficiencies=[Skill.PERCEPTION])

    assert skill_bonus(c, Skill.INSIGHT) == 2


@pytest.mark.parametrize("skill", list(Skill))
def test_every_skill_uses_its_own_ability(make_character, skill):
    scores = {STR: 8, DEX: 12, CON: 10, INT: 14, WIS: 16, CHA: 18}

    assert skill_bonus(make_character(scores=scores), skill) == modifier(
        scores[SKILL_ABILITY[skill]]
    )


# --- спасброски ---


def test_save_without_proficiency_is_only_ability_modifier(make_character):
    assert save_bonus(make_character(scores={CON: 14}), CON) == 2


def test_save_with_proficiency_adds_proficiency_bonus(make_character):
    c = make_character(scores={CON: 14}, save_proficiencies=[STR, CON])

    assert save_bonus(c, CON) == 2 + 2


def test_save_proficiency_applies_only_to_listed_abilities(make_character):
    c = make_character(scores={DEX: 12}, save_proficiencies=[STR, CON])

    assert save_bonus(c, DEX) == 1


# --- пассивная Внимательность ---


@pytest.mark.parametrize(
    ("proficiencies", "expertise", "expected"),
    [([], [], 12), ([Skill.PERCEPTION], [], 14), ([Skill.PERCEPTION], [Skill.PERCEPTION], 16)],
)
def test_passive_perception(make_character, proficiencies, expertise, expected):
    c = make_character(scores={WIS: 14}, skill_proficiencies=proficiencies, expertise=expertise)

    assert passive_perception(c) == expected


# --- класс доспеха ---


def test_unarmored_ac_is_ten_plus_dex(make_character, srd):
    assert armor_class(make_character(scores={DEX: 14}), srd) == 12


def test_chain_mail_with_shield(make_character, srd):
    c = make_character(scores={DEX: 16}, inventory=gear("chain_mail", "shield"))

    # Тяжёлый доспех: Ловкость не прибавляется; щит даёт +2.
    assert armor_class(c, srd) == 16 + 2


def test_heavy_armor_ignores_even_negative_dex(make_character, srd):
    assert armor_class(make_character(scores={DEX: 8}, inventory=gear("chain_mail")), srd) == 16


@pytest.mark.parametrize(
    ("dex", "expected"),
    [(18, 15), (14, 15), (12, 14), (8, 12)],
)
def test_medium_armor_caps_positive_dex_but_not_negative(make_character, srd, dex, expected):
    c = make_character(scores={DEX: dex}, inventory=gear("chain_shirt"))

    assert armor_class(c, srd) == expected


@pytest.mark.parametrize(
    ("armor_id", "dex", "expected"),
    [("leather_armor", 16, 14), ("studded_leather_armor", 20, 17), ("leather_armor", 8, 10)],
)
def test_light_armor_adds_full_dex(make_character, srd, armor_id, dex, expected):
    c = make_character(scores={DEX: dex}, inventory=gear(armor_id))

    assert armor_class(c, srd) == expected


def test_shield_without_armor_adds_two(make_character, srd):
    c = make_character(scores={DEX: 14}, inventory=gear("shield"))

    assert armor_class(c, srd) == 10 + 2 + 2


def test_two_shields_count_once(make_character, srd):
    assert armor_class(make_character(inventory=gear("shield", "shield")), srd) == 12


def test_armor_in_backpack_does_not_count(make_character, srd):
    c = make_character(scores={DEX: 14}, inventory=gear("chain_mail", "shield", equipped=False))

    assert armor_class(c, srd) == 12


def test_equipped_item_outside_srd_is_not_armor(make_character, srd):
    c = make_character(inventory=gear("silver_amulet"))

    assert armor_class(c, srd) == 10


def test_barbarian_unarmored_defense_uses_con(make_character, srd):
    c = make_character(class_id="barbarian", scores={DEX: 14, CON: 18})

    assert armor_class(c, srd) == 10 + 2 + 4


def test_barbarian_unarmored_defense_allows_shield(make_character, srd):
    c = make_character(class_id="barbarian", scores={DEX: 14, CON: 18}, inventory=gear("shield"))

    assert armor_class(c, srd) == 10 + 2 + 4 + 2


def test_barbarian_in_armor_loses_unarmored_defense(make_character, srd):
    c = make_character(
        class_id="barbarian", scores={DEX: 14, CON: 18}, inventory=gear("chain_shirt")
    )

    assert armor_class(c, srd) == 13 + 2


def test_other_classes_do_not_get_con_without_armor(make_character, srd):
    assert armor_class(make_character(scores={DEX: 14, CON: 18}), srd) == 12


def test_mage_armor_gives_thirteen_plus_dex_without_armor(make_character, srd):
    c = make_character(
        class_id="wizard",
        scores={DEX: 14},
        buffs=[ActiveBuff(id="mage_armor", rounds_left=4800, source_id="pc-1")],
    )

    assert armor_class(c, srd) == 13 + 2


def test_mage_armor_is_ignored_when_wearing_armor(make_character, srd):
    c = make_character(
        scores={DEX: 14},
        inventory=gear("leather_armor"),
        buffs=[ActiveBuff(id="mage_armor", rounds_left=4800, source_id="pc-1")],
    )

    assert armor_class(c, srd) == 11 + 2


def test_mage_armor_works_with_shield(make_character, srd):
    c = make_character(
        scores={DEX: 14},
        inventory=gear("shield"),
        buffs=[ActiveBuff(id="mage_armor", rounds_left=4800, source_id="pc-1")],
    )

    assert armor_class(c, srd) == 13 + 2 + 2


def test_best_unarmored_formula_wins(make_character, srd):
    mage_armor = [ActiveBuff(id="mage_armor", rounds_left=4800, source_id="pc-1")]
    tough = make_character(class_id="barbarian", scores={DEX: 14, CON: 18}, buffs=mage_armor)
    frail = make_character(class_id="barbarian", scores={DEX: 14, CON: 8}, buffs=mage_armor)

    assert armor_class(tough, srd) == 10 + 2 + 4  # 16 против 15 у «доспехов мага»
    assert armor_class(frail, srd) == 13 + 2  # 15 против 11 у защиты без доспехов


def test_defense_style_adds_one_only_in_armor(make_character, srd):
    in_armor = make_character(fighting_style="defense", inventory=gear("chain_mail"))
    unarmored = make_character(fighting_style="defense")
    shield_only = make_character(fighting_style="defense", inventory=gear("shield"))

    assert armor_class(in_armor, srd) == 16 + 1
    assert armor_class(unarmored, srd) == 10
    assert armor_class(shield_only, srd) == 10 + 2


@pytest.mark.parametrize(
    ("class_id", "expected"),
    [
        ("fighter", 16),  # кольчуга
        ("barbarian", 10),  # без доспеха, Тел и Лов по 10
        ("rogue", 11),  # кожаный доспех
        ("ranger", 12),  # проклёпанный кожаный
        ("cleric", 15),  # кольчужная рубаха + щит
        ("wizard", 10),
    ],
)
def test_ac_in_starting_kit(make_character, srd, class_id, expected):
    kit = srd.classes[class_id].starting_equipment
    armor_ids = [i for i in kit if srd.items[i].armor is not None]
    c = make_character(class_id=class_id, inventory=gear(*armor_ids))

    assert armor_class(c, srd) == expected


# --- атака оружием ---


def test_dagger_uses_dex_when_dex_is_higher(make_character, srd):
    c = make_character(scores={STR: 10, DEX: 16}, inventory=gear("dagger"))

    assert weapon_attack(c, "dagger", srd) == AttackProfile(
        item_id="dagger",
        name="Кинжал",
        bonus=3 + 2,
        damage="1d4+3",
        damage_type="piercing",
        range="melee",
        ability=DEX,
    )


def test_finesse_weapon_uses_str_when_str_is_higher(make_character, srd):
    c = make_character(scores={STR: 16, DEX: 12}, inventory=gear("rapier"))

    profile = weapon_attack(c, "rapier", srd)

    assert (profile.ability, profile.bonus, profile.damage) == (STR, 3 + 2, "1d8+3")


def test_finesse_tie_prefers_str(make_character, srd):
    # При равенстве берём Силу: от неё зависят бонусы ярости.
    c = make_character(scores={STR: 14, DEX: 14}, inventory=gear("rapier"))

    assert weapon_attack(c, "rapier", srd).ability == STR


def test_non_finesse_melee_weapon_uses_str_even_if_dex_is_higher(make_character, srd):
    c = make_character(scores={STR: 10, DEX: 18}, inventory=gear("greatsword"))

    profile = weapon_attack(c, "greatsword", srd)

    assert (profile.ability, profile.bonus, profile.damage) == (STR, 2, "2d6")


def test_thrown_melee_weapon_uses_str(make_character, srd):
    c = make_character(scores={STR: 10, DEX: 18}, inventory=gear("javelin"))

    profile = weapon_attack(c, "javelin", srd)

    assert (profile.ability, profile.range, profile.bonus) == (STR, "melee", 2)


def test_ranged_weapon_uses_dex(make_character, srd):
    c = make_character(scores={STR: 18, DEX: 16}, inventory=gear("longbow"))

    profile = weapon_attack(c, "longbow", srd)

    assert (profile.ability, profile.range) == (DEX, "far")
    assert (profile.bonus, profile.damage) == (3 + 2, "1d8+3")


def test_archery_adds_two_to_ranged_attack_but_not_to_damage(make_character, srd):
    c = make_character(scores={DEX: 16}, fighting_style="archery", inventory=gear("longbow"))

    profile = weapon_attack(c, "longbow", srd)

    assert (profile.bonus, profile.damage) == (3 + 2 + 2, "1d8+3")


@pytest.mark.parametrize("style", [None, "defense", "dueling"])
def test_other_styles_do_not_help_ranged_attacks(make_character, srd, style):
    c = make_character(scores={DEX: 16}, fighting_style=style, inventory=gear("longbow"))

    assert weapon_attack(c, "longbow", srd).bonus == 3 + 2


def test_archery_does_not_help_melee_attacks(make_character, srd):
    c = make_character(fighting_style="archery", inventory=gear("longsword"))

    assert weapon_attack(c, "longsword", srd).bonus == 2


@pytest.mark.parametrize(
    ("class_id", "weapon_id", "proficient"),
    [
        ("fighter", "longsword", True),
        ("fighter", "dagger", True),
        ("barbarian", "greataxe", True),
        ("wizard", "quarterstaff", True),
        ("wizard", "longsword", False),
        ("cleric", "mace", True),
        ("cleric", "longsword", False),
        # В данных плут владеет и воинским оружием (модель не выражает ограничение SRD).
        ("rogue", "rapier", True),
    ],
)
def test_proficiency_comes_from_class_weapon_categories(
    make_character, srd, class_id, weapon_id, proficient
):
    c = make_character(class_id=class_id, inventory=gear(weapon_id))

    assert weapon_attack(c, weapon_id, srd).bonus == (2 if proficient else 0)


def test_proficiency_bonus_in_attack_grows_with_level(make_character, srd):
    c = make_character(level=9, inventory=gear("longsword"))

    assert weapon_attack(c, "longsword", srd).bonus == 4


@pytest.mark.parametrize(
    ("strength", "weapon_id", "damage"),
    [(10, "rapier", "1d8"), (8, "mace", "1d6-1"), (16, "mace", "1d6+3")],
)
def test_damage_is_written_with_signed_modifier(make_character, srd, strength, weapon_id, damage):
    c = make_character(scores={STR: strength}, inventory=gear(weapon_id))

    assert weapon_attack(c, weapon_id, srd).damage == damage


def test_weapon_from_backpack_can_attack(make_character, srd):
    c = make_character(scores={STR: 14}, inventory=gear("handaxe", equipped=False))

    profile = weapon_attack(c, "handaxe", srd)

    assert (profile.item_id, profile.damage_type, profile.damage) == (
        "handaxe",
        "slashing",
        "1d6+2",
    )


def test_dueling_adds_two_damage_to_one_handed_melee_weapon(make_character, srd):
    c = make_character(scores={STR: 16}, fighting_style="dueling", inventory=gear("rapier"))

    profile = weapon_attack(c, "rapier", srd)

    # Бонус к попаданию не меняется: стиль добавляет только урон.
    assert (profile.bonus, profile.damage) == (3 + 2, "1d8+5")


def test_dueling_works_with_versatile_weapon_held_in_one_hand(make_character, srd):
    c = make_character(fighting_style="dueling", inventory=gear("longsword"))

    assert weapon_attack(c, "longsword", srd).damage == "1d8+2"


def test_dueling_ignores_unequipped_second_weapon(make_character, srd):
    inventory = gear("longsword") + gear("dagger", equipped=False)
    c = make_character(fighting_style="dueling", inventory=inventory)

    assert weapon_attack(c, "longsword", srd).damage == "1d8+2"


@pytest.mark.parametrize(
    ("weapon_id", "inventory_ids", "damage"),
    [
        ("greatsword", ["greatsword"], "2d6"),  # двуручное оружие занимает обе руки
        ("longbow", ["longbow"], "1d8"),  # не рукопашное
        ("longsword", ["longsword", "shield"], "1d8"),  # вторая рука держит щит
        ("longsword", ["longsword", "dagger"], "1d8"),  # вторая рука держит оружие
    ],
)
def test_dueling_needs_one_handed_melee_weapon_and_free_hand(
    make_character, srd, weapon_id, inventory_ids, damage
):
    c = make_character(fighting_style="dueling", inventory=gear(*inventory_ids))

    assert weapon_attack(c, weapon_id, srd).damage == damage


@pytest.mark.parametrize("worn_id", ["chain_mail", "torch", "arcane_focus", "potion_of_healing"])
def test_dueling_is_not_blocked_by_worn_items_that_are_not_a_weapon_or_shield(
    make_character, srd, worn_id
):
    c = make_character(fighting_style="dueling", inventory=gear("longsword", worn_id))

    assert weapon_attack(c, "longsword", srd).damage == "1d8+2"


def test_dueling_does_not_apply_to_one_handed_ranged_weapon(make_character, srd):
    # В данных всё дальнобойное оружие двуручное, поэтому добавляем одноручный арбалет.
    hand_crossbow = ItemDef(
        id="hand_crossbow",
        name="Ручной арбалет",
        name_en="Hand Crossbow",
        kind="weapon",
        weapon=WeaponProps(
            category="martial",
            damage="1d6",
            damage_type="piercing",
            properties=("ammunition", "light"),
            range="far",
        ),
    )
    content = srd.model_copy(update={"items": srd.items | {hand_crossbow.id: hand_crossbow}})
    c = make_character(fighting_style="dueling", inventory=gear("hand_crossbow"))

    assert weapon_attack(c, "hand_crossbow", content).damage == "1d6"


@pytest.mark.parametrize("style", [None, "archery", "defense"])
def test_only_dueling_style_adds_melee_damage(make_character, srd, style):
    c = make_character(fighting_style=style, inventory=gear("rapier"))

    assert weapon_attack(c, "rapier", srd).damage == "1d8"


@pytest.mark.parametrize(
    ("inventory_ids", "message"),
    [
        ([], "нет предмета"),
        (["shield"], "не оружие"),
        (["silver_amulet"], "не оружие"),
    ],
)
def test_weapon_attack_rejects_missing_or_non_weapon_item(
    make_character, srd, inventory_ids, message
):
    c = make_character(inventory=gear(*inventory_ids))
    item_id = inventory_ids[0] if inventory_ids else "dagger"

    with pytest.raises(ValueError, match=message):
        weapon_attack(c, item_id, srd)


def test_weapon_attack_rejects_unknown_class(make_character, srd):
    c = make_character(class_id="paladin", inventory=gear("dagger"))

    with pytest.raises(ValueError, match="paladin"):
        weapon_attack(c, "dagger", srd)


@pytest.mark.parametrize(
    "class_id", ["barbarian", "fighter", "rogue", "ranger", "cleric", "wizard"]
)
def test_every_weapon_in_starting_kit_has_valid_attack(make_character, srd, class_id):
    kit = srd.classes[class_id].starting_equipment
    weapon_ids = [i for i in kit if srd.items[i].weapon is not None]
    c = make_character(class_id=class_id, scores={STR: 14, DEX: 14}, inventory=gear(*kit))

    for weapon_id in weapon_ids:
        profile = weapon_attack(c, weapon_id, srd)
        assert profile.item_id == weapon_id
        assert profile.name == srd.items[weapon_id].name
        assert parse(profile.damage).terms  # запись урона разбирается движком костей


# --- импровизированная атака ---


def test_improvised_attack_is_d4_bludgeoning_without_proficiency(make_character):
    # Воин владеет всем оружием, но бонус мастерства к случайному предмету не добавляется.
    c = make_character(scores={STR: 16, DEX: 18})

    assert improvised_attack(c) == AttackProfile(
        item_id=IMPROVISED_WEAPON_ID,
        name="Импровизированное оружие",
        bonus=3,
        damage="1d4+3",
        damage_type="bludgeoning",
        range="melee",
        ability=STR,
    )


def test_improvised_attack_with_weak_strength(make_character):
    profile = improvised_attack(make_character(scores={STR: 8}))

    assert (profile.bonus, profile.damage) == (-1, "1d4-1")


def test_improvised_attack_ignores_level(make_character):
    assert improvised_attack(make_character(level=9)).bonus == 0


# --- заклинания ---


def test_cleric_spell_save_dc_is_eight_plus_proficiency_plus_wisdom(make_character, srd):
    c = make_character(class_id="cleric", scores={WIS: 16, INT: 8})

    assert spell_save_dc(c, srd) == 8 + 2 + 3


def test_cleric_spell_attack_bonus(make_character, srd):
    c = make_character(class_id="cleric", scores={WIS: 16})

    assert spell_attack_bonus(c, srd) == 2 + 3


def test_wizard_casts_with_intelligence(make_character, srd):
    c = make_character(class_id="wizard", scores={INT: 16, WIS: 8})

    assert spell_save_dc(c, srd) == 8 + 2 + 3
    assert spell_attack_bonus(c, srd) == 2 + 3


def test_ranger_casts_with_wisdom(make_character, srd):
    c = make_character(class_id="ranger", scores={WIS: 14, INT: 18})

    assert spell_save_dc(c, srd) == 8 + 2 + 2


def test_spell_numbers_grow_with_level(make_character, srd):
    c = make_character(class_id="cleric", level=5, scores={WIS: 16})

    assert spell_save_dc(c, srd) == 8 + 3 + 3
    assert spell_attack_bonus(c, srd) == 3 + 3


@pytest.mark.parametrize("class_id", ["barbarian", "fighter", "rogue"])
def test_non_casters_have_no_spell_numbers(make_character, srd, class_id):
    c = make_character(class_id=class_id, scores={WIS: 18, INT: 18, CHA: 18})

    assert spell_save_dc(c, srd) is None
    assert spell_attack_bonus(c, srd) is None


def test_improvised_weapon_id_is_the_one_combat_accepts(make_character):
    assert IMPROVISED_WEAPON_ID == "improvised"
    assert improvised_attack(make_character()).item_id == "improvised"


@pytest.mark.parametrize("weapon_range", get_args(WeaponProps.model_fields["range"].annotation))
def test_weapon_attack_supports_every_range_a_weapon_can_have(make_character, srd, weapon_range):
    # «Рядом» — брошенное оружие в боевых зонах; в данных его пока нет, но модель его допускает.
    weapon = ItemDef(
        id="test_weapon",
        name="Тестовое оружие",
        name_en="Test Weapon",
        kind="weapon",
        weapon=WeaponProps(
            category="simple",
            damage="1d6",
            damage_type="piercing",
            properties=("thrown",),
            range=weapon_range,
        ),
    )
    content = srd.model_copy(update={"items": srd.items | {weapon.id: weapon}})
    c = make_character(inventory=gear("test_weapon"))

    profile = weapon_attack(c, "test_weapon", content)

    assert profile.range == weapon_range
    assert profile.ability == (STR if weapon_range == "melee" else DEX)


def test_attack_profile_and_monster_actions_share_one_range_vocabulary():
    ranges = {
        model.__name__: get_args(model.model_fields["range"].annotation)
        for model in (AttackProfile, WeaponProps, MonsterAction)
    }

    assert ranges["AttackProfile"] == ranges["WeaponProps"] == ranges["MonsterAction"]
    assert set(ranges["WeaponProps"]) == {"melee", "near", "far"}
