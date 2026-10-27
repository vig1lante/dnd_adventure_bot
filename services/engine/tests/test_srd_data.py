"""Тесты данных SRD 5.2.1: классы, виды, предыстории, черты, навыки, предметы, заклинания,
монстры и состояния.

Каждый файл читается и валидируется моделью отдельно (форма и числа SRD), а затем
`load_srd()` без пути читает все девять файлов встроенных данных и проверяет ссылки между ними.
"""

import re
from importlib.resources import files

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from dnd_engine import content
from dnd_engine.abilities import Ability, Skill
from dnd_engine.content import (
    BackgroundDef,
    ClassDef,
    ConditionDef,
    FeatDef,
    ItemDef,
    MonsterDef,
    SkillDef,
    SpeciesDef,
    SpellDef,
    SrdContent,
    load_srd,
)
from dnd_engine.dice import DiceSyntaxError, parse
from dnd_engine.units import meters_text, meters_text_pair

SRD_DIR = files("dnd_engine") / "data" / "srd"

TABLE_MODELS: dict[str, type[BaseModel]] = {
    "classes": ClassDef,
    "species": SpeciesDef,
    "backgrounds": BackgroundDef,
    "feats": FeatDef,
    "skills": SkillDef,
    "spells": SpellDef,
    "items": ItemDef,
    "monsters": MonsterDef,
    "conditions": ConditionDef,
}

CLASS_IDS = ("barbarian", "fighter", "rogue", "ranger", "cleric", "wizard")
SPECIES_IDS = (
    "dragonborn",
    "dwarf",
    "elf",
    "gnome",
    "goliath",
    "halfling",
    "human",
    "orc",
    "tiefling",
)
BACKGROUND_IDS = ("acolyte", "criminal", "sage", "soldier")
CASTER_IDS = ("cleric", "wizard", "ranger")
NON_CASTER_IDS = tuple(i for i in CLASS_IDS if i not in CASTER_IDS)

STR, DEX, CON, INT, WIS, CHA = (
    Ability.STR,
    Ability.DEX,
    Ability.CON,
    Ability.INT,
    Ability.WIS,
    Ability.CHA,
)

# Числа ниже взяты из SRD 5.2.1 и таблицы плана B2.4.
# id класса -> (кость хитов, спасброски, id подкласса, число навыков на выбор)
CLASS_FACTS = {
    "barbarian": (12, (STR, CON), "path_of_the_berserker", 2),
    "fighter": (10, (STR, CON), "champion", 2),
    "rogue": (8, (DEX, INT), "thief", 4),
    "ranger": (10, (STR, DEX), "hunter", 3),
    "cleric": (8, (WIS, CHA), "life_domain", 2),
    "wizard": (6, (INT, WIS), "evoker", 2),
}
# id класса -> (владение доспехами, владение оружием)
CLASS_TRAINING = {
    "barbarian": ({"light", "medium", "shield"}, {"simple", "martial"}),
    "fighter": ({"light", "medium", "heavy", "shield"}, {"simple", "martial"}),
    # SRD: из воинского оружия только с «фехтовальным» или «лёгким»; модель этого не выражает.
    "rogue": ({"light"}, {"simple", "martial"}),
    "ranger": ({"light", "medium", "shield"}, {"simple", "martial"}),
    "cleric": ({"light", "medium", "shield"}, {"simple"}),
    "wizard": (set(), {"simple"}),
}
MECHANICAL_FEATURES = {
    "barbarian": {"rage", "unarmored_defense", "reckless_attack", "frenzy"},
    "fighter": {"fighting_style", "second_wind", "action_surge", "improved_critical"},
    "rogue": {"sneak_attack", "cunning_action", "expertise", "steady_aim"},
    "ranger": {"spellcasting", "favored_enemy", "fighting_style", "colossus_slayer"},
    "cleric": {
        "spellcasting",
        "channel_divinity",
        "divine_spark",
        "preserve_life",
        "disciple_of_life",
    },
    "wizard": {"spellcasting", "arcane_recovery", "potent_cantrip"},
}
# Ресурсы на 3-м уровне: имя -> число использований.
RESOURCES_AT_LEVEL_3 = {
    "barbarian": {"rage": 3},
    "fighter": {"second_wind": 2, "action_surge": 1},
    "rogue": {},
    "ranger": {"hunters_mark": 2},
    "cleric": {"channel_divinity": 2},
    "wizard": {"arcane_recovery": 1},
}
# id класса -> (характеристика, ячейки, заговоры, подготовленные); ключ — уровень персонажа.
SPELLCASTING = {
    "cleric": (
        WIS,
        {1: {1: 2}, 2: {1: 3}, 3: {1: 4, 2: 2}},
        {1: 3, 2: 3, 3: 3},
        {1: 4, 2: 5, 3: 6},
    ),
    "wizard": (
        INT,
        {1: {1: 2}, 2: {1: 3}, 3: {1: 4, 2: 2}},
        {1: 3, 2: 3, 3: 3},
        {1: 4, 2: 5, 3: 6},
    ),
    "ranger": (
        WIS,
        {1: {1: 2}, 2: {1: 2}, 3: {1: 3}},
        {1: 0, 2: 0, 3: 0},
        {1: 2, 2: 3, 3: 4},
    ),
}
# id вида -> (размер, скорость)
SPECIES_FACTS = {
    "dragonborn": ("medium", 30),
    "dwarf": ("medium", 30),
    "elf": ("medium", 30),
    "gnome": ("small", 30),
    "goliath": ("medium", 35),
    "halfling": ("small", 30),
    "human": ("medium", 30),
    "orc": ("medium", 30),
    "tiefling": ("medium", 30),
}
MECHANICAL_TRAITS = {
    "dragonborn": {"breath_weapon", "damage_resistance"},
    "dwarf": {"dwarven_resilience", "dwarven_toughness"},
    "elf": {"keen_senses"},
    "gnome": {"gnomish_cunning"},
    "goliath": set(),
    "halfling": {"luck"},
    "human": {"resourceful", "skillful"},
    "orc": {"relentless_endurance"},
    "tiefling": set(),
}
# id предыстории -> (варианты характеристик, навыки, id черты)
BACKGROUND_FACTS = {
    "acolyte": ({INT, WIS, CHA}, {Skill.INSIGHT, Skill.RELIGION}, "magic_initiate"),
    "criminal": ({DEX, CON, INT}, {Skill.SLEIGHT_OF_HAND, Skill.STEALTH}, "alert"),
    "sage": ({CON, INT, WIS}, {Skill.ARCANA, Skill.HISTORY}, "magic_initiate"),
    "soldier": ({STR, DEX, CON}, {Skill.ATHLETICS, Skill.INTIMIDATION}, "savage_attacker"),
}
# id предыстории -> владение инструментом (SRD 5.2.1) русским текстом для карточки предыстории.
BACKGROUND_TOOLS = {
    "acolyte": "Инструменты каллиграфа",
    "criminal": "Воровские инструменты",
    "sage": "Инструменты каллиграфа",
    "soldier": "Один игровой набор на выбор",
}
# magic_initiate не механическая: план B2.4 даёт ей только текст для мастера (заговоры из данных),
# а выбора заговоров черты и свободного сотворения в движке нет.
MECHANICAL_FEATS = {"alert", "savage_attacker"}

ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
ENGLISH_NAME_PATTERN = re.compile(r"[A-Za-z' -]+")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN = re.compile(r"[A-Za-z]")
MAX_SUMMARY_LENGTH = 300  # кураторский пересказ, а не копия текста SRD
# Описание расы для карточки: 2–3 предложения своими словами, поэтому предел больше, чем у summary.
MAX_DESCRIPTION_LENGTH = 500
# Конец предложения: точка, «!», «?» или многоточие перед пробелом или в конце текста.
SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")
# Короче этого summary особенности или черты — заглушка, а не описание для карточки.
MIN_FEATURE_SUMMARY_LENGTH = 40


# --- ожидаемое содержимое предметов, заклинаний, монстров и состояний (SRD 5.2.1, план B2.5) ---

# Типы урона SRD; в данных допустимы только они.
DAMAGE_TYPES = {
    "acid",
    "bludgeoning",
    "cold",
    "fire",
    "force",
    "lightning",
    "necrotic",
    "piercing",
    "poison",
    "psychic",
    "radiant",
    "slashing",
    "thunder",
}
CONDITION_IDS = {
    "blinded",
    "charmed",
    "deafened",
    "frightened",
    "grappled",
    "incapacitated",
    "invisible",
    "paralyzed",
    "petrified",
    "poisoned",
    "prone",
    "restrained",
    "stunned",
    "unconscious",
}

# id оружия -> (категория, урон, тип урона, свойства, дальность). Метательное оружие ближнего
# боя имеет дальность melee и свойство thrown; луки и арбалеты — far.
WEAPON_FACTS = {
    "club": ("simple", "1d4", "bludgeoning", {"light"}, "melee"),
    "dagger": ("simple", "1d4", "piercing", {"finesse", "light", "thrown"}, "melee"),
    "quarterstaff": ("simple", "1d6", "bludgeoning", {"versatile"}, "melee"),
    "mace": ("simple", "1d6", "bludgeoning", set(), "melee"),
    "spear": ("simple", "1d6", "piercing", {"thrown", "versatile"}, "melee"),
    "handaxe": ("simple", "1d6", "slashing", {"light", "thrown"}, "melee"),
    "javelin": ("simple", "1d6", "piercing", {"thrown"}, "melee"),
    "light_crossbow": ("simple", "1d8", "piercing", {"ammunition", "two_handed"}, "far"),
    "shortbow": ("simple", "1d6", "piercing", {"ammunition", "two_handed"}, "far"),
    "shortsword": ("martial", "1d6", "piercing", {"finesse", "light"}, "melee"),
    "rapier": ("martial", "1d8", "piercing", {"finesse"}, "melee"),
    "scimitar": ("martial", "1d6", "slashing", {"finesse", "light"}, "melee"),
    "longsword": ("martial", "1d8", "slashing", {"versatile"}, "melee"),
    "battleaxe": ("martial", "1d8", "slashing", {"versatile"}, "melee"),
    "greataxe": ("martial", "1d12", "slashing", {"heavy", "two_handed"}, "melee"),
    "greatsword": ("martial", "2d6", "slashing", {"heavy", "two_handed"}, "melee"),
    "warhammer": ("martial", "1d8", "bludgeoning", {"versatile"}, "melee"),
    "longbow": ("martial", "1d8", "piercing", {"ammunition", "heavy", "two_handed"}, "far"),
}
# id доспеха -> (вид, базовый КД, предел Лов, помеха скрытности). Для щита base_ac — бонус к КД.
ARMOR_FACTS = {
    "leather_armor": ("light", 11, None, False),
    "studded_leather_armor": ("light", 12, None, False),
    "chain_shirt": ("medium", 13, 2, False),
    "scale_mail": ("medium", 14, 2, True),
    "chain_mail": ("heavy", 16, 0, True),
    "shield": ("shield", 2, None, False),
}
POTION_IDS = ("potion_of_healing", "potion_of_greater_healing")
GEAR_IDS = ("rope", "torch", "thieves_tools", "healers_kit", "spellbook", "rations")
FOCUS_IDS = ("holy_symbol", "arcane_focus")
ITEM_IDS = (*WEAPON_FACTS, *ARMOR_FACTS, *POTION_IDS, *GEAR_IDS, *FOCUS_IDS)

# id заклинания -> (уровень, время сотворения, дальность, концентрация, длительность в раундах,
# классы). Длительность в раундах по 6 секунд (минута — 10, час — 600); None — мгновенное.
SPELL_FACTS = {
    "sacred_flame": (0, "action", "near", False, None, {"cleric"}),
    "guidance": (0, "action", "touch", True, 10, {"cleric"}),
    "spare_the_dying": (0, "action", "near", False, None, {"cleric"}),
    "fire_bolt": (0, "action", "far", False, None, {"wizard"}),
    "ray_of_frost": (0, "action", "near", False, None, {"wizard"}),
    "shocking_grasp": (0, "action", "touch", False, None, {"wizard"}),
    "mage_hand": (0, "action", "near", False, 10, {"wizard"}),
    "light": (0, "action", "touch", False, 600, {"cleric", "wizard"}),
    "magic_missile": (1, "action", "far", False, None, {"wizard"}),
    "sleep": (1, "action", "near", True, 10, {"wizard"}),
    "burning_hands": (1, "action", "self", False, None, {"wizard"}),
    "thunderwave": (1, "action", "self", False, None, {"wizard"}),
    "mage_armor": (1, "action", "touch", False, 4800, {"wizard"}),
    "cure_wounds": (1, "action", "touch", False, None, {"cleric", "ranger"}),
    "healing_word": (1, "bonus_action", "near", False, None, {"cleric"}),
    "bless": (1, "action", "near", True, 10, {"cleric"}),
    "guiding_bolt": (1, "action", "far", False, 1, {"cleric"}),
    "inflict_wounds": (1, "action", "touch", False, None, {"cleric"}),
    "hunters_mark": (1, "bonus_action", "far", True, 600, {"ranger"}),
    "goodberry": (1, "action", "self", False, 14400, {"ranger"}),
    "fog_cloud": (1, "action", "far", True, 600, {"ranger", "wizard"}),
    "detect_magic": (1, "action", "self", True, 100, {"cleric", "ranger", "wizard"}),
    "scorching_ray": (2, "action", "far", False, None, {"wizard"}),
    "misty_step": (2, "bonus_action", "self", False, None, {"wizard"}),
    "hold_person": (2, "action", "near", True, 10, {"cleric", "wizard"}),
    "aid": (2, "action", "near", False, 4800, {"cleric", "ranger"}),
    "lesser_restoration": (2, "bonus_action", "touch", False, None, {"cleric", "ranger"}),
    # В SRD сотворяется 10 минут; модель времени сотворения этого не выражает.
    "prayer_of_healing": (2, "action", "near", False, None, {"cleric"}),
}
SPELL_IDS = tuple(SPELL_FACTS)
# id заклинания -> поля SpellEffect, отличные от значений по умолчанию.
SPELL_EFFECTS = {
    "sacred_flame": {"kind": "save", "save": "dex", "damage": "1d8", "damage_type": "radiant"},
    "guidance": {"kind": "buff", "buff": "guidance"},
    "spare_the_dying": {"kind": "utility"},
    "fire_bolt": {"kind": "attack", "attack": "ranged", "damage": "1d10", "damage_type": "fire"},
    "ray_of_frost": {"kind": "attack", "attack": "ranged", "damage": "1d8", "damage_type": "cold"},
    "shocking_grasp": {
        "kind": "attack",
        "attack": "melee",
        "damage": "1d8",
        "damage_type": "lightning",
    },
    "mage_hand": {"kind": "utility"},
    "light": {"kind": "utility"},
    "magic_missile": {
        "kind": "auto",
        "damage": "1d4+1",
        "damage_type": "force",
        "targets": 3,
        "repeats": 3,
    },
    "sleep": {
        "kind": "condition",
        "save": "wis",
        "condition": "incapacitated",
        "targets": 6,
    },
    "burning_hands": {
        "kind": "save",
        "save": "dex",
        "damage": "3d6",
        "damage_type": "fire",
        "half_on_save": True,
        "targets": 6,
    },
    "thunderwave": {
        "kind": "save",
        "save": "con",
        "damage": "2d8",
        "damage_type": "thunder",
        "half_on_save": True,
        "targets": 6,
    },
    "mage_armor": {"kind": "buff", "buff": "mage_armor"},
    "cure_wounds": {"kind": "heal", "heal": "2d8", "heal_modifier": True},
    "healing_word": {"kind": "heal", "heal": "2d4", "heal_modifier": True},
    "bless": {"kind": "buff", "buff": "bless", "targets": 3},
    "guiding_bolt": {
        "kind": "attack",
        "attack": "ranged",
        "damage": "4d6",
        "damage_type": "radiant",
        "buff": "guiding_bolt",
    },
    "inflict_wounds": {
        "kind": "save",
        "save": "con",
        "damage": "2d10",
        "damage_type": "necrotic",
        "half_on_save": True,
    },
    "hunters_mark": {"kind": "buff", "buff": "hunters_mark"},
    "goodberry": {"kind": "utility"},
    "fog_cloud": {"kind": "utility"},
    "detect_magic": {"kind": "utility"},
    "scorching_ray": {
        "kind": "attack",
        "attack": "ranged",
        "damage": "2d6",
        "damage_type": "fire",
        "targets": 3,
        "repeats": 3,
    },
    "misty_step": {"kind": "utility"},
    "hold_person": {"kind": "condition", "save": "wis", "condition": "paralyzed"},
    "aid": {"kind": "buff", "buff": "aid", "targets": 3},
    "lesser_restoration": {"kind": "utility"},
    "prayer_of_healing": {"kind": "heal", "heal": "2d8", "targets": 5},
}
# id заклинания -> добавка за уровень ячейки выше базового; у остальных upcast не задан.
SPELL_UPCASTS = {
    "magic_missile": "1d4+1",
    "burning_hands": "1d6",
    "thunderwave": "1d8",
    "cure_wounds": "2d8",
    "healing_word": "2d4",
    "guiding_bolt": "1d6",
    "inflict_wounds": "1d10",
    "scorching_ray": "2d6",
    "aid": "5",
    "prayer_of_healing": "1d8",
}

# id заклинания -> школа магии по SRD 5.2.1. Лечащие заклинания в SRD 5.2 — ограждение, а
# «Чудо-ягоды» — вызов (в правилах 2014 года было иначе).
SPELL_SCHOOLS = {
    "sacred_flame": "evocation",
    "guidance": "divination",
    "spare_the_dying": "necromancy",
    "fire_bolt": "evocation",
    "ray_of_frost": "evocation",
    "shocking_grasp": "evocation",
    "mage_hand": "conjuration",
    "light": "evocation",
    "magic_missile": "evocation",
    "sleep": "enchantment",
    "burning_hands": "evocation",
    "thunderwave": "evocation",
    "mage_armor": "abjuration",
    "cure_wounds": "abjuration",
    "healing_word": "abjuration",
    "bless": "enchantment",
    "guiding_bolt": "evocation",
    "inflict_wounds": "necromancy",
    "hunters_mark": "divination",
    "goodberry": "conjuration",
    "fog_cloud": "conjuration",
    "detect_magic": "divination",
    "scorching_ray": "evocation",
    "misty_step": "conjuration",
    "hold_person": "enchantment",
    "aid": "abjuration",
    "lesser_restoration": "abjuration",
    "prayer_of_healing": "abjuration",
}

# ПО -> опыт по таблице 2024.
XP_BY_CR = {0: 10, 0.125: 25, 0.25: 50, 0.5: 100, 1: 200, 2: 450, 3: 700}
# id монстра -> (ПО, КД, HP, скорость, кость HP). Числа из статблоков SRD 5.2.1.
MONSTER_FACTS = {
    "goblin_warrior": (0.25, 15, 10, 30, "3d6"),
    "goblin_boss": (1, 17, 21, 30, "6d6"),
    "skeleton": (0.25, 14, 13, 30, "2d8+4"),
    "zombie": (0.25, 8, 15, 20, "2d8+6"),
    "ghoul": (1, 12, 22, 30, "5d8"),
    "wolf": (0.25, 12, 11, 40, "2d8+2"),
    "dire_wolf": (1, 14, 22, 50, "3d10+6"),
    "giant_rat": (0.125, 13, 7, 30, "2d6"),
    "giant_spider": (1, 14, 26, 30, "4d10+4"),
    "bandit": (0.125, 12, 11, 30, "2d8+2"),
    "bandit_captain": (2, 15, 52, 30, "8d8+16"),
    "cultist": (0.125, 12, 9, 30, "2d8"),
    "cultist_fanatic": (2, 13, 44, 30, "8d8+8"),
    "guard": (0.125, 16, 11, 30, "2d8+2"),
    "bugbear_warrior": (1, 14, 33, 30, "6d8+6"),
    "ogre": (2, 11, 68, 40, "8d10+24"),
    "shadow": (0.5, 12, 27, 40, "5d8+5"),
    "animated_armor": (1, 18, 33, 25, "6d8+6"),
    "swarm_of_rats": (0.25, 10, 14, 30, "4d8-4"),
    "owlbear": (3, 13, 59, 40, "7d10+21"),
}
MONSTER_IDS = tuple(MONSTER_FACTS)
# id монстра -> (Сила, Ловкость, Телосложение, Интеллект, Мудрость, Харизма).
MONSTER_SCORES = {
    "goblin_warrior": (8, 15, 10, 10, 8, 8),
    "goblin_boss": (10, 15, 10, 10, 8, 10),
    "skeleton": (10, 16, 15, 6, 8, 5),
    "zombie": (13, 6, 16, 3, 6, 5),
    "ghoul": (13, 15, 10, 7, 10, 6),
    "wolf": (14, 15, 12, 3, 12, 6),
    "dire_wolf": (17, 15, 15, 3, 12, 7),
    "giant_rat": (7, 16, 11, 2, 10, 4),
    "giant_spider": (14, 16, 12, 2, 11, 4),
    "bandit": (11, 12, 12, 10, 10, 10),
    "bandit_captain": (15, 16, 14, 14, 11, 14),
    "cultist": (11, 12, 10, 10, 11, 10),
    "cultist_fanatic": (11, 14, 12, 10, 14, 13),
    "guard": (13, 12, 12, 10, 11, 10),
    "bugbear_warrior": (15, 14, 13, 8, 11, 9),
    "ogre": (19, 8, 16, 5, 7, 7),
    "shadow": (6, 14, 13, 6, 10, 8),
    "animated_armor": (14, 11, 13, 1, 3, 1),
    "swarm_of_rats": (9, 11, 9, 2, 10, 3),
    "owlbear": (20, 12, 17, 3, 12, 7),
}
# id монстра -> (спасброски, навыки) с итоговыми бонусами; спасбросок указан, только если он
# отличается от модификатора характеристики.
MONSTER_PROFICIENCIES = {
    "goblin_warrior": ({}, {"stealth": 6}),
    "goblin_boss": ({}, {"stealth": 6}),
    "skeleton": ({}, {}),
    "zombie": ({"wis": 0}, {}),
    "ghoul": ({}, {}),
    "wolf": ({}, {"perception": 5, "stealth": 4}),
    "dire_wolf": ({}, {"perception": 5, "stealth": 4}),
    "giant_rat": ({"dex": 5}, {"perception": 2}),
    "giant_spider": ({}, {"perception": 4, "stealth": 7}),
    "bandit": ({}, {}),
    "bandit_captain": (
        {"str": 4, "dex": 5, "wis": 2},
        {"athletics": 4, "deception": 4},
    ),
    "cultist": ({"wis": 2}, {"deception": 2, "religion": 2}),
    "cultist_fanatic": ({"wis": 4}, {"deception": 3, "persuasion": 3, "religion": 2}),
    "guard": ({}, {"perception": 2}),
    "bugbear_warrior": ({}, {"stealth": 6, "survival": 2}),
    "ogre": ({}, {}),
    "shadow": ({}, {"stealth": 6}),
    "animated_armor": ({}, {}),
    "swarm_of_rats": ({"dex": 2}, {}),
    "owlbear": ({}, {"perception": 5}),
}
# id монстра -> (сопротивления, иммунитеты). Иммунитеты к состояниям (кроме истощения, которого
# в срезе нет) записаны вместе с иммунитетами к урону.
MONSTER_DEFENSES = {
    "skeleton": ((), {"poison", "poisoned"}),
    "zombie": ((), {"poison", "poisoned"}),
    "ghoul": ((), {"poison", "charmed", "poisoned"}),
    "shadow": (
        {"acid", "cold", "fire", "lightning", "thunder"},
        {
            "necrotic",
            "poison",
            "frightened",
            "grappled",
            "paralyzed",
            "petrified",
            "poisoned",
            "prone",
            "restrained",
            "unconscious",
        },
    ),
    "animated_armor": (
        (),
        {
            "poison",
            "psychic",
            "charmed",
            "deafened",
            "frightened",
            "paralyzed",
            "petrified",
            "poisoned",
        },
    ),
    "swarm_of_rats": (
        {"bludgeoning", "piercing", "slashing"},
        {
            "charmed",
            "frightened",
            "grappled",
            "paralyzed",
            "petrified",
            "prone",
            "restrained",
            "stunned",
        },
    ),
}
# id монстра -> уязвимости (типы урона). У остальных монстров среза уязвимостей нет.
MONSTER_VULNERABILITIES = {
    "skeleton": {"bludgeoning"},
    "shadow": {"radiant"},
}
# id монстра -> {id действия: поля MonsterAction, отличные от значений по умолчанию}.
# У kind: attack поля save/dc/condition вместе означают: при попадании цель делает спасбросок и
# при провале получает состояние; condition без save — состояние при попадании без спасброска.


def attack(bonus: int, damage: str, damage_type: str, **extra) -> dict:
    return {
        "kind": "attack",
        "attack_bonus": bonus,
        "damage": damage,
        "damage_type": damage_type,
        **extra,
    }


def multiattack(*action_ids: str) -> dict:
    return {"kind": "multiattack", "multiattack": action_ids}


OTHER = {"kind": "other"}

MONSTER_ACTIONS = {
    "goblin_warrior": {
        "scimitar": attack(4, "1d6+2", "slashing"),
        "shortbow": attack(4, "1d6+2", "piercing", range="far"),
        "nimble_escape": OTHER,
    },
    "goblin_boss": {
        "multiattack": multiattack("scimitar", "scimitar"),
        "scimitar": attack(4, "1d6+2", "slashing"),
        "shortbow": attack(4, "1d6+2", "piercing", range="far"),
        "nimble_escape": OTHER,
        "redirect_attack": OTHER,
    },
    "skeleton": {
        "shortsword": attack(5, "1d6+3", "piercing"),
        "shortbow": attack(5, "1d6+3", "piercing", range="far"),
    },
    "zombie": {"slam": attack(3, "1d8+1", "bludgeoning")},
    "ghoul": {
        "multiattack": multiattack("bite", "bite"),
        "bite": attack(
            4, "1d6+2", "piercing", secondary_damage="1d6", secondary_damage_type="necrotic"
        ),
        "claw": attack(4, "1d4+2", "slashing", save="con", dc=10, condition="paralyzed"),
    },
    "wolf": {"bite": attack(4, "1d6+2", "piercing", condition="prone")},
    "dire_wolf": {"bite": attack(5, "1d10+3", "piercing", condition="prone")},
    "giant_rat": {"bite": attack(5, "1d4+3", "piercing")},
    "giant_spider": {
        "bite": attack(
            5, "1d8+3", "piercing", secondary_damage="2d6", secondary_damage_type="poison"
        ),
        "web": {
            "kind": "save",
            "save": "dex",
            "dc": 13,
            "range": "near",
            "condition": "restrained",
        },
    },
    "bandit": {
        "scimitar": attack(3, "1d6+1", "slashing"),
        "light_crossbow": attack(3, "1d8+1", "piercing", range="far"),
    },
    "bandit_captain": {
        "multiattack": multiattack("scimitar", "scimitar"),
        "scimitar": attack(5, "1d6+3", "slashing"),
        "pistol": attack(5, "1d10+3", "piercing", range="near"),
        "parry": OTHER,
    },
    "cultist": {
        "ritual_sickle": attack(
            3, "1d4+1", "slashing", secondary_damage="1", secondary_damage_type="necrotic"
        )
    },
    "cultist_fanatic": {
        "pact_blade": attack(
            4, "1d8+2", "slashing", secondary_damage="2d6", secondary_damage_type="necrotic"
        ),
        "spellcasting": OTHER,
    },
    "guard": {
        "spear": attack(3, "1d6+1", "piercing"),
        "spear_thrown": attack(3, "1d6+1", "piercing", range="near"),
    },
    "bugbear_warrior": {
        "grab": attack(4, "2d6+2", "bludgeoning", condition="grappled", reach=True),
        "light_hammer": attack(4, "3d4+2", "bludgeoning", reach=True),
        "light_hammer_thrown": attack(4, "3d4+2", "bludgeoning", range="near"),
    },
    "ogre": {
        "greatclub": attack(6, "2d8+4", "bludgeoning"),
        "javelin": attack(6, "2d6+4", "piercing"),
        "javelin_thrown": attack(6, "2d6+4", "piercing", range="near"),
    },
    "shadow": {
        "draining_swipe": attack(4, "1d6+2", "necrotic"),
        "shadow_stealth": OTHER,
    },
    "animated_armor": {
        "multiattack": multiattack("slam", "slam"),
        "slam": attack(4, "1d6+2", "bludgeoning"),
    },
    "swarm_of_rats": {"bites": attack(2, "2d4", "piercing")},
    "owlbear": {
        "multiattack": multiattack("rend", "rend"),
        "rend": attack(7, "2d8+5", "slashing"),
    },
}


def read_entries(table: str) -> list:
    """Сырые записи файла таблицы; YAML читается безопасно, как в загрузчике."""
    return yaml.safe_load((SRD_DIR / f"{table}.yaml").read_text(encoding="utf-8"))


def load_table(table: str) -> dict:
    """Записи таблицы, провалидированные моделью content.py, по id."""
    model = TABLE_MODELS[table]
    return {entry["id"]: model.model_validate(entry) for entry in read_entries(table)}


@pytest.fixture(scope="module")
def classes() -> dict[str, ClassDef]:
    return load_table("classes")


@pytest.fixture(scope="module")
def species() -> dict[str, SpeciesDef]:
    return load_table("species")


@pytest.fixture(scope="module")
def backgrounds() -> dict[str, BackgroundDef]:
    return load_table("backgrounds")


@pytest.fixture(scope="module")
def feats() -> dict[str, FeatDef]:
    return load_table("feats")


@pytest.fixture(scope="module")
def skills() -> dict[str, SkillDef]:
    return load_table("skills")


@pytest.fixture(scope="module")
def items() -> dict[str, ItemDef]:
    return load_table("items")


@pytest.fixture(scope="module")
def spells() -> dict[str, SpellDef]:
    return load_table("spells")


@pytest.fixture(scope="module")
def monsters() -> dict[str, MonsterDef]:
    return load_table("monsters")


@pytest.fixture(scope="module")
def conditions() -> dict[str, ConditionDef]:
    return load_table("conditions")


@pytest.fixture(scope="module")
def srd():
    # Кеш сбрасывается до и после: другие тесты подменяют встроенную папку и его очищают.
    content._load_builtin_srd.cache_clear()
    yield load_srd()
    content._load_builtin_srd.cache_clear()


# --- форма файлов ---


@pytest.mark.parametrize("table", TABLE_MODELS)
def test_file_is_list_of_valid_records_with_unique_ids(table):
    entries = read_entries(table)

    assert isinstance(entries, list)
    assert entries, f"{table}.yaml пуст"
    for entry in entries:
        try:
            TABLE_MODELS[table].model_validate(entry)  # модели запрещают лишние поля
        except ValidationError as exc:
            pytest.fail(f"{table}.yaml, запись {entry.get('id')!r}: {exc}")
    ids = [entry["id"] for entry in entries]
    assert all(isinstance(entry_id, str) for entry_id in ids)
    assert len(ids) == len(set(ids)), f"в {table}.yaml повторяются id"


def test_exactly_six_classes(classes):
    assert set(classes) == set(CLASS_IDS)
    assert len(classes) == 6


def test_exactly_nine_species(species):
    assert set(species) == set(SPECIES_IDS)
    assert len(species) == 9


def test_exactly_four_backgrounds(backgrounds):
    assert set(backgrounds) == set(BACKGROUND_IDS)
    assert len(backgrounds) == 4


# --- классы ---


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_class_core_traits_match_srd(classes, class_id):
    hit_die, saving_throws, subclass_id, skill_choices = CLASS_FACTS[class_id]
    armor, weapons = CLASS_TRAINING[class_id]
    cls = classes[class_id]

    assert cls.hit_die == hit_die
    assert cls.saving_throws == saving_throws
    assert cls.subclass_id == subclass_id
    assert cls.skill_choices == skill_choices
    assert set(cls.armor) == armor
    assert set(cls.weapons) == weapons


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_ability_priority_is_permutation_of_all_abilities(classes, class_id):
    priority = classes[class_id].ability_priority

    assert len(priority) == 6
    assert set(priority) == set(Ability)


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_default_skills_fit_skill_choices(classes, class_id):
    cls = classes[class_id]

    assert len(cls.default_skills) == cls.skill_choices
    assert len(set(cls.default_skills)) == len(cls.default_skills)
    assert set(cls.default_skills) <= set(cls.skill_options)


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_default_background_exists_and_gives_other_skills(classes, backgrounds, class_id):
    cls = classes[class_id]

    assert cls.default_background in backgrounds
    # Иначе быстрый персонаж получил бы дубль навыка и не прошёл бы собственную проверку.
    assert not set(cls.default_skills) & set(backgrounds[cls.default_background].skills)


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_class_has_starting_equipment(classes, class_id):
    assert classes[class_id].starting_equipment


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_features_are_unique_and_within_levels_one_to_three(classes, class_id):
    features = classes[class_id].features
    ids = [feature.id for feature in features]

    assert len(ids) == len(set(ids))
    assert {feature.level for feature in features} <= {1, 2, 3}
    assert any(feature.level == 1 for feature in features)
    assert any(feature.level == 3 for feature in features)


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_mechanical_features_match_plan(classes, class_id):
    mechanical = {feature.id for feature in classes[class_id].features if feature.mechanical}

    assert mechanical == MECHANICAL_FEATURES[class_id]


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_resources_at_level_three_match_plan(classes, class_id):
    resources = classes[class_id].resources

    # levels[3] без .get: отсутствие строки для 3-го уровня должно быть ошибкой данных.
    assert {name: levels[3] for name, levels in resources.items()} == (
        RESOURCES_AT_LEVEL_3[class_id]
    )


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_resource_tables_have_no_gaps_up_to_level_three(classes, class_id):
    for name, levels in classes[class_id].resources.items():
        assert sorted(levels) == list(range(min(levels), 4)), f"{name}: пропущен уровень"


def test_fighting_styles_only_for_fighter_and_ranger(classes):
    styles = {"archery", "defense", "dueling"}

    assert set(classes["fighter"].fighting_styles) == styles
    assert set(classes["ranger"].fighting_styles) == styles
    for class_id in ("barbarian", "rogue", "cleric", "wizard"):
        assert classes[class_id].fighting_styles == ()


@pytest.mark.parametrize("class_id", CASTER_IDS)
def test_caster_has_spellcasting_tables_for_levels_one_to_three(classes, class_id):
    ability, slots, cantrips, prepared = SPELLCASTING[class_id]
    spellcasting = classes[class_id].spellcasting

    assert spellcasting is not None
    assert spellcasting.ability == ability
    assert spellcasting.slots == slots
    assert spellcasting.cantrips_known == cantrips
    assert spellcasting.prepared == prepared
    assert spellcasting.spell_list == class_id


@pytest.mark.parametrize("class_id", CASTER_IDS)
def test_caster_has_default_spells_for_quick_mode(classes, class_id):
    cls = classes[class_id]
    spellcasting = cls.spellcasting

    assert cls.default_spells
    assert len(set(cls.default_spells)) == len(cls.default_spells)
    # Быстрый режим делит список по уровню заклинаний: заговоры и подготовленные.
    assert len(cls.default_spells) == spellcasting.cantrips_known[3] + spellcasting.prepared[3]


@pytest.mark.parametrize("class_id", NON_CASTER_IDS)
def test_non_caster_has_no_spellcasting(classes, class_id):
    assert classes[class_id].spellcasting is None
    assert classes[class_id].default_spells == ()


# --- виды ---


@pytest.mark.parametrize("species_id", SPECIES_IDS)
def test_species_size_and_speed_match_srd(species, species_id):
    size, speed = SPECIES_FACTS[species_id]

    assert species[species_id].size == size
    assert species[species_id].speed == speed


@pytest.mark.parametrize("species_id", SPECIES_IDS)
def test_species_traits_are_unique(species, species_id):
    ids = [trait.id for trait in species[species_id].traits]

    assert ids
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("species_id", SPECIES_IDS)
def test_mechanical_species_traits_match_plan(species, species_id):
    mechanical = {trait.id for trait in species[species_id].traits if trait.mechanical}

    assert mechanical == MECHANICAL_TRAITS[species_id]


@pytest.mark.parametrize("species_id", SPECIES_IDS)
def test_species_description_is_two_or_three_russian_sentences(species, species_id):
    # Карточка расы (спека DND-2, §7): короткий пересказ своими словами, а не копия SRD.
    description = species[species_id].description

    assert CYRILLIC.search(description)
    assert len(description) <= MAX_DESCRIPTION_LENGTH
    assert description == description.strip() and SENTENCE_END.search(description[-1])
    assert 2 <= len(SENTENCE_END.findall(description)) <= 3


def test_species_descriptions_differ(species):
    descriptions = [kind.description for kind in species.values()]

    assert len(set(descriptions)) == len(descriptions)


# --- предыстории и черты ---


@pytest.mark.parametrize("background_id", BACKGROUND_IDS)
def test_background_matches_srd(backgrounds, background_id):
    abilities, skills, feat = BACKGROUND_FACTS[background_id]
    background = backgrounds[background_id]

    assert set(background.ability_options) == abilities
    assert set(background.skills) == skills
    assert background.feat == feat


@pytest.mark.parametrize("background_id", BACKGROUND_IDS)
def test_background_tool_proficiency_matches_srd(backgrounds, background_id):
    # Инструмент — русский текст для карточки, а не id предмета: инструментов в items.yaml нет.
    assert backgrounds[background_id].tool == BACKGROUND_TOOLS[background_id]


@pytest.mark.parametrize("background_id", BACKGROUND_IDS)
def test_background_has_two_or_three_inspiration_triggers(backgrounds, background_id):
    triggers = backgrounds[background_id].inspiration_triggers

    assert 2 <= len(triggers) <= 3
    assert all(trigger.strip() for trigger in triggers)
    assert len(set(triggers)) == len(triggers)


def test_background_feats_exist(backgrounds, feats):
    for background in backgrounds.values():
        assert background.feat in feats


def test_only_planned_feats_are_mechanical(feats):
    assert {feat.id for feat in feats.values() if feat.mechanical} == MECHANICAL_FEATS
    assert set(feats) >= MECHANICAL_FEATS


def test_every_feature_up_to_level_three_and_every_feat_has_a_card_summary(classes, feats):
    # Подробная карточка класса и черты (спека DND-2, §7) показывает summary как описание.
    summaries = {
        f"{cls.id}.{feature.id}": feature.summary
        for cls in classes.values()
        for feature in cls.features
        if feature.level <= 3
    }
    summaries |= {f"feat {feat.id}": feat.summary for feat in feats.values()}

    assert len(summaries) == sum(len(cls.features) for cls in classes.values()) + len(feats)
    weak = [
        owner for owner, text in summaries.items() if len(text.strip()) < MIN_FEATURE_SUMMARY_LENGTH
    ]
    assert weak == []
    assert [owner for owner, text in summaries.items() if len(text) > MAX_SUMMARY_LENGTH] == []


# --- навыки ---


def test_skill_table_describes_every_skill_exactly_once():
    ids = [entry["id"] for entry in read_entries("skills")]

    assert sorted(ids) == sorted(skill.value for skill in Skill)


@pytest.mark.parametrize("skill", Skill)
def test_skill_summary_says_what_it_checks_and_gives_examples(skills, skill):
    summary = skills[skill].summary

    assert CYRILLIC.search(summary)
    assert len(summary) <= MAX_SUMMARY_LENGTH
    assert "Например" in summary  # примеры применения для карточки навыка (спека DND-2, §7)


@pytest.mark.parametrize("skill", Skill)
def test_skill_summary_first_says_what_it_checks_then_lists_examples(skills, skill):
    # Карточка навыка (контракт B7, §2.4): сначала что проверяет навык, затем примеры применения.
    checks, marker, examples = skills[skill].summary.partition("Например:")

    assert marker, "примеры идут после «Например:»"
    assert CYRILLIC.search(checks) and checks.strip().endswith(".")
    assert examples.strip().endswith(".")
    assert examples.count(",") >= 1  # не меньше двух примеров через запятую


def test_skill_summaries_differ(skills):
    summaries = [skill.summary for skill in skills.values()]

    assert len(set(summaries)) == len(summaries)


# --- ссылки на другие файлы и оформление ---


def test_referenced_item_and_spell_ids_are_snake_case(classes, backgrounds):
    referenced = [
        *(item for cls in classes.values() for item in cls.starting_equipment),
        *(spell for cls in classes.values() for spell in cls.default_spells),
        *(item for background in backgrounds.values() for item in background.equipment),
    ]

    assert referenced
    assert [ref for ref in referenced if not ID_PATTERN.fullmatch(ref)] == []


def test_english_names_are_plain_english(classes, species, backgrounds):
    names = [
        *(cls.name_en for cls in classes.values()),
        *(item.name_en for item in species.values()),
        *(background.name_en for background in backgrounds.values()),
    ]

    assert [name for name in names if not ENGLISH_NAME_PATTERN.fullmatch(name)] == []


def test_names_and_summaries_are_russian_and_short(classes, species, backgrounds, feats):
    texts: list[tuple[str, str]] = []
    for cls in classes.values():
        texts += [(f"class {cls.id}", cls.name), (f"subclass {cls.id}", cls.subclass_name)]
        for feature in cls.features:
            texts += [
                (f"{cls.id}.{feature.id}", feature.name),
                (f"{cls.id}.{feature.id} summary", feature.summary),
            ]
    for kind in species.values():
        texts.append((f"species {kind.id}", kind.name))
        for trait in kind.traits:
            texts += [
                (f"{kind.id}.{trait.id}", trait.name),
                (f"{kind.id}.{trait.id} summary", trait.summary),
            ]
    for background in backgrounds.values():
        texts += [
            (f"background {background.id}", background.name),
            (f"background {background.id} tool", background.tool),
        ]
        texts += [(f"{background.id} trigger", text) for text in background.inspiration_triggers]
    for feat in feats.values():
        texts += [(f"feat {feat.id}", feat.name), (f"feat {feat.id} summary", feat.summary)]

    assert [owner for owner, text in texts if not CYRILLIC.search(text)] == []
    too_long = [owner for owner, text in texts if len(text) > MAX_SUMMARY_LENGTH]
    assert too_long == []


def test_card_texts_of_hero_creation_have_no_latin_letters(species, backgrounds, skills):
    # Эти тексты сервер отдаёт в карточки как есть (контракт B7, §2: всё для игрока — на русском):
    # код вроде `darkvision` или английское слово вместо русского игрок увидел бы сразу.
    texts = {f"species {kind.id} description": kind.description for kind in species.values()}
    texts |= {f"background {item.id} tool": item.tool for item in backgrounds.values()}
    texts |= {f"skill {skill_id} summary": skill.summary for skill_id, skill in skills.items()}

    assert len(texts) == len(species) + len(backgrounds) + len(Skill)
    assert [owner for owner, text in texts.items() if LATIN.search(text)] == []


# --- полная загрузка встроенных данных ---


def test_builtin_data_loads_with_all_references(srd):
    # load_srd() без пути читает все 9 файлов и проверяет перекрёстные ссылки (ContentError иначе).
    assert isinstance(srd, SrdContent)
    assert {name: len(getattr(srd, name)) for name in SrdContent.model_fields} == {
        "classes": 6,
        "species": 9,
        "backgrounds": 4,
        "feats": 3,
        "skills": len(Skill),
        "spells": len(SPELL_IDS),
        "items": len(ITEM_IDS),
        "monsters": len(MONSTER_IDS),
        "conditions": len(CONDITION_IDS),
    }


def test_builtin_data_is_cached(srd):
    assert load_srd() is srd


# --- предметы ---


def test_items_match_plan(items):
    assert set(items) == set(ITEM_IDS)
    assert len(items) == 34


def test_item_details_match_kind(items):
    weapons = {i for i, item in items.items() if item.kind == "weapon"}
    armors = {i for i, item in items.items() if item.kind == "armor"}
    potions = {i for i, item in items.items() if item.kind == "potion"}

    assert weapons == set(WEAPON_FACTS)
    assert armors == set(ARMOR_FACTS)
    assert potions == set(POTION_IDS)
    for item in items.values():
        assert (item.weapon is not None) == (item.kind == "weapon"), item.id
        assert (item.armor is not None) == (item.kind == "armor"), item.id
        assert (item.heal is not None) == (item.kind == "potion"), item.id


@pytest.mark.parametrize("item_id", WEAPON_FACTS)
def test_weapon_matches_srd(items, item_id):
    category, damage, damage_type, properties, reach = WEAPON_FACTS[item_id]
    weapon = items[item_id].weapon

    assert weapon.category == category
    assert parse(weapon.damage) == parse(damage)
    assert weapon.damage_type == damage_type
    assert set(weapon.properties) == properties
    assert len(set(weapon.properties)) == len(weapon.properties)
    assert weapon.range == reach


@pytest.mark.parametrize("item_id", ARMOR_FACTS)
def test_armor_matches_srd(items, item_id):
    kind, base_ac, dex_cap, stealth_disadvantage = ARMOR_FACTS[item_id]
    armor = items[item_id].armor

    assert armor.kind == kind
    assert armor.base_ac == base_ac
    assert armor.dex_cap == dex_cap
    assert armor.stealth_disadvantage is stealth_disadvantage


def test_healing_potions_heal_by_srd_dice(items):
    assert parse(items["potion_of_healing"].heal) == parse("2d4+2")
    assert parse(items["potion_of_greater_healing"].heal) == parse("4d4+4")


@pytest.mark.parametrize("potion_id", POTION_IDS)
def test_potion_summary_says_what_the_engine_spends(items, potion_id):
    # Текст идёт мастеру: без модуля «Специи BG3» зелье тратит действие, бонусное — только с ним.
    summary = items[potion_id].summary

    assert (
        "выпить или дать союзнику рядом — действием (с модулем «Специи BG3» — бонусным)" in summary
    )
    assert "Выпить можно бонусным действием" not in summary


@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_starting_equipment_exists_and_suits_class(srd, class_id):
    cls = srd.classes[class_id]
    equipment = [srd.items[item_id] for item_id in cls.starting_equipment]

    assert equipment, f"у {class_id} нет стартового снаряжения"
    weapons = [item.weapon for item in equipment if item.weapon]
    armors = [item.armor for item in equipment if item.armor]
    assert weapons, f"у {class_id} нет оружия в стартовом наборе"
    assert {weapon.category for weapon in weapons} <= set(cls.weapons)
    assert {armor.kind for armor in armors} <= set(cls.armor)
    # B2.7 надевает первый доспех и щит: больше одного доспеха и одного щита быть не должно.
    assert sum(armor.kind != "shield" for armor in armors) <= 1
    assert sum(armor.kind == "shield" for armor in armors) <= 1


def test_background_equipment_exists(srd):
    for background in srd.backgrounds.values():
        assert set(background.equipment) <= set(srd.items), background.id


# --- заклинания ---


def test_spells_match_plan(spells):
    assert set(spells) == set(SPELL_IDS)
    assert len(spells) == 28


def test_every_spell_has_its_srd_school(spells):
    assert set(SPELL_SCHOOLS) == set(SPELL_IDS)
    assert {spell_id: spell.school for spell_id, spell in spells.items()} == SPELL_SCHOOLS


@pytest.mark.parametrize("spell_id", SPELL_IDS)
def test_spell_casting_facts_match_srd(spells, spell_id):
    level, casting_time, reach, concentration, duration, classes = SPELL_FACTS[spell_id]
    spell = spells[spell_id]

    assert spell.level == level
    assert spell.casting_time == casting_time
    assert spell.range == reach
    assert spell.concentration is concentration
    assert spell.duration_rounds == duration
    assert set(spell.classes) == classes
    assert len(set(spell.classes)) == len(spell.classes)


@pytest.mark.parametrize("spell_id", SPELL_IDS)
def test_spell_effect_matches_srd(spells, spell_id):
    spell = spells[spell_id]

    assert spell.effect.model_dump(exclude_defaults=True) == SPELL_EFFECTS[spell_id]
    if spell_id in SPELL_UPCASTS:
        assert parse(spell.upcast) == parse(SPELL_UPCASTS[spell_id])
    else:
        assert spell.upcast is None


# Лечащие заклинания, к которым SRD добавляет модификатор заклинательной характеристики.
HEALING_WITH_MODIFIER = {"cure_wounds", "healing_word"}
# Сотворяются 10 минут, поэтому в бою недоступны.
OUT_OF_COMBAT_ONLY = {"prayer_of_healing"}


def test_healing_modifier_is_a_field_of_exactly_the_srd_healing_spells(spells):
    flagged = {sid for sid, spell in spells.items() if spell.effect.heal_modifier}

    assert flagged == HEALING_WITH_MODIFIER
    assert all(spells[sid].effect.kind == "heal" for sid in flagged)


def test_only_long_casting_spells_are_out_of_combat_only(spells):
    flagged = {sid for sid, spell in spells.items() if spell.out_of_combat_only}

    assert flagged == OUT_OF_COMBAT_ONLY


def test_spell_attack_and_save_effects_are_complete(spells):
    for spell in spells.values():
        effect = spell.effect
        if effect.kind == "attack":
            assert effect.attack is not None, spell.id
        if effect.kind == "save":
            assert effect.save is not None, spell.id
        if effect.kind == "heal":
            assert effect.heal is not None, spell.id
        if effect.kind == "buff":
            assert effect.buff, spell.id
        if effect.kind == "condition":
            assert effect.condition, spell.id
        if effect.damage is not None:
            assert effect.damage_type in DAMAGE_TYPES, spell.id
            assert effect.kind in {"attack", "save", "auto"}, spell.id


def test_concentration_spells_have_duration(spells):
    for spell in spells.values():
        if spell.concentration:
            assert spell.duration_rounds is not None, spell.id


@pytest.mark.parametrize("class_id", ["cleric", "wizard", "ranger"])
def test_default_spells_have_levels_that_fit_the_class(srd, class_id):
    cls = srd.classes[class_id]
    spellcasting = cls.spellcasting
    levels = [srd.spells[spell_id].level for spell_id in cls.default_spells]
    top_slot = max(spellcasting.slots[3])

    # Быстрый режим берёт заговоры и подготовленные заклинания по уровню заклинания.
    assert levels.count(0) == spellcasting.cantrips_known[3]
    assert len(levels) - levels.count(0) == spellcasting.prepared[3]
    assert all(level <= top_slot for level in levels)


# --- монстры ---


def test_monsters_match_plan(monsters):
    assert set(monsters) == set(MONSTER_IDS)
    assert len(monsters) == 20
    assert all(monster.cr <= 3 for monster in monsters.values())


@pytest.mark.parametrize("monster_id", MONSTER_IDS)
def test_monster_xp_matches_challenge_rating(monsters, monster_id):
    monster = monsters[monster_id]

    assert monster.xp == XP_BY_CR[monster.cr]


def average_roll(expr: str) -> float:
    parsed = parse(expr)
    return parsed.modifier + sum(
        term.sign * term.count * (term.sides + 1) / 2 for term in parsed.terms
    )


@pytest.mark.parametrize("monster_id", MONSTER_IDS)
def test_monster_hit_dice_average_matches_hp(monsters, monster_id):
    monster = monsters[monster_id]

    # parse() бросает DiceSyntaxError на некорректной записи костей.
    assert abs(average_roll(monster.hp_dice) - monster.hp) <= 1


@pytest.mark.parametrize("monster_id", MONSTER_IDS)
def test_monster_stat_block_matches_srd(monsters, monster_id):
    cr, ac, hp, speed, hp_dice = MONSTER_FACTS[monster_id]
    monster = monsters[monster_id]

    assert (monster.cr, monster.ac, monster.hp, monster.speed) == (cr, ac, hp, speed)
    assert parse(monster.hp_dice) == parse(hp_dice)
    assert tuple(monster.scores[ability] for ability in Ability) == MONSTER_SCORES[monster_id]
    saves, skills = MONSTER_PROFICIENCIES[monster_id]
    assert monster.saves == saves
    assert monster.skills == skills
    resistances, immunities = MONSTER_DEFENSES.get(monster_id, ((), ()))
    assert set(monster.resistances) == set(resistances)
    assert set(monster.immunities) == set(immunities)
    assert set(monster.vulnerabilities) == MONSTER_VULNERABILITIES.get(monster_id, set())


@pytest.mark.parametrize("monster_id", MONSTER_IDS)
def test_monster_actions_match_srd(monsters, monster_id):
    expected = MONSTER_ACTIONS[monster_id]
    actions = {action.id: action for action in monsters[monster_id].actions}

    assert len(actions) == len(monsters[monster_id].actions), "повторяются id действий"
    assert set(actions) == set(expected)
    for action_id, fields in expected.items():
        dumped = actions[action_id].model_dump(exclude_defaults=True)
        for text_field in ("id", "name", "summary"):
            dumped.pop(text_field, None)
        assert dumped == fields, f"{monster_id}.{action_id}"


def test_the_giant_spider_web_restrains_one_creature(monsters):
    # SRD 5.2.1: паутина действует на одно существо; значение по умолчанию (1) и есть эта правда.
    web = next(a for a in monsters["giant_spider"].actions if a.id == "web")

    assert web.kind == "save" and web.targets == 1


def test_monster_actions_are_complete(monsters):
    for monster in monsters.values():
        for action in monster.actions:
            owner = f"{monster.id}.{action.id}"
            if action.kind == "attack":
                assert action.attack_bonus is not None, owner
                assert action.damage and action.damage_type in DAMAGE_TYPES, owner
            if action.kind == "save":
                assert action.save is not None and action.dc is not None, owner
            if action.kind == "multiattack":
                assert action.multiattack, owner
            else:
                assert not action.multiattack, owner
            if action.dc is not None:
                assert action.save is not None, owner


def test_secondary_damage_is_a_second_damage_type_of_the_same_hit(monsters):
    with_secondary = {
        (monster.id, action.id)
        for monster in monsters.values()
        for action in monster.actions
        if action.secondary_damage
    }

    assert with_secondary == {
        ("ghoul", "bite"),
        ("giant_spider", "bite"),
        ("cultist", "ritual_sickle"),
        ("cultist_fanatic", "pact_blade"),
    }
    for monster in monsters.values():
        for action in monster.actions:
            if action.secondary_damage:
                assert action.secondary_damage_type in DAMAGE_TYPES, (monster.id, action.id)
                assert action.secondary_damage_type != action.damage_type, (monster.id, action.id)


def test_vulnerabilities_are_fields_not_text_in_traits(monsters):
    for monster_id in MONSTER_VULNERABILITIES:
        assert not [t for t in monsters[monster_id].traits if "Уязвимость" in t], monster_id


def test_monster_tags_describe_creature_type(monsters):
    assert all(monster.tags for monster in monsters.values())
    undead = {monster.id for monster in monsters.values() if "undead" in monster.tags}
    assert undead == {"skeleton", "zombie", "ghoul", "shadow"}


# --- состояния ---


def test_conditions_match_plan(conditions):
    assert set(conditions) == CONDITION_IDS
    assert len(conditions) == 14


# --- ссылки на состояния и типы урона ---


def test_condition_references_point_to_known_conditions(spells, monsters):
    referenced = [
        *(spell.effect.condition for spell in spells.values() if spell.effect.condition),
        *(
            action.condition
            for monster in monsters.values()
            for action in monster.actions
            if action.condition
        ),
    ]

    assert referenced
    assert set(referenced) <= CONDITION_IDS


def test_monster_defenses_use_known_names(monsters):
    for monster in monsters.values():
        assert set(monster.resistances) <= DAMAGE_TYPES, monster.id
        assert set(monster.vulnerabilities) <= DAMAGE_TYPES, monster.id
        assert set(monster.immunities) <= DAMAGE_TYPES | CONDITION_IDS, monster.id


# --- записи костей урона и лечения ---


def dice_expressions(items, spells, monsters) -> list[tuple[str, str]]:
    """Все записи урона и лечения в данных: (где найдена, запись)."""
    found: list[tuple[str, str]] = []
    for item in items.values():
        if item.weapon:
            found.append((f"item {item.id}.damage", item.weapon.damage))
        if item.heal:
            found.append((f"item {item.id}.heal", item.heal))
    for spell in spells.values():
        for name in ("damage", "heal"):
            if getattr(spell.effect, name):
                found.append((f"spell {spell.id}.{name}", getattr(spell.effect, name)))
        if spell.upcast:
            found.append((f"spell {spell.id}.upcast", spell.upcast))
    for monster in monsters.values():
        found.append((f"monster {monster.id}.hp_dice", monster.hp_dice))
        found += [
            (f"monster {monster.id}.{action.id}.damage", action.damage)
            for action in monster.actions
            if action.damage
        ]
        found += [
            (f"monster {monster.id}.{action.id}.secondary_damage", action.secondary_damage)
            for action in monster.actions
            if action.secondary_damage
        ]
    return found


def test_every_damage_and_healing_expression_parses(items, spells, monsters):
    expressions = dice_expressions(items, spells, monsters)
    broken: list[str] = []
    for owner, expr in expressions:
        try:
            parse(expr)
        except DiceSyntaxError as exc:
            broken.append(f"{owner}: {exc}")

    # Проверка не пустая: записи найдены и в предметах, и в заклинаниях, и у монстров.
    assert {owner.split()[0] for owner, _ in expressions} == {"item", "spell", "monster"}
    assert broken == []


# --- оформление новых таблиц ---


def test_new_tables_have_plain_english_names(items, spells, monsters):
    names = [entry.name_en for table in (items, spells, monsters) for entry in table.values()]

    assert [name for name in names if not ENGLISH_NAME_PATTERN.fullmatch(name)] == []


def test_new_tables_have_russian_names_and_short_summaries(items, spells, monsters, conditions):
    texts: list[tuple[str, str]] = []
    for item in items.values():
        texts += [(f"item {item.id}", item.name), (f"item {item.id} summary", item.summary)]
    for spell in spells.values():
        texts += [(f"spell {spell.id}", spell.name), (f"spell {spell.id} summary", spell.summary)]
    for condition in conditions.values():
        texts += [
            (f"condition {condition.id}", condition.name),
            (f"condition {condition.id} summary", condition.summary),
        ]
    for monster in monsters.values():
        texts.append((f"monster {monster.id}", monster.name))
        texts += [(f"monster {monster.id} trait", trait) for trait in monster.traits]
        for action in monster.actions:
            texts += [
                (f"monster {monster.id}.{action.id}", action.name),
                (f"monster {monster.id}.{action.id} summary", action.summary),
            ]

    assert [owner for owner, text in texts if not CYRILLIC.search(text)] == []
    assert [owner for owner, text in texts if len(text) > MAX_SUMMARY_LENGTH] == []


# --- расстояния в текстах: метры с шагом 1,5 м (спека DND-2, Р3 и §18) ---

# Метры с запятой; у оружия — пара «обычная/большая дальность»: «4,5 м», «24/96 м».
METERS_IN_TEXT = re.compile(r"(\d+(?:,\d+)?)(?:/(\d+(?:,\d+)?))? м\b")


def test_rewritten_distances_match_the_meters_rule(species, monsters):
    goblin_bow = next(a for a in monsters["goblin_warrior"].actions if a.id == "shortbow")
    elf_darkvision = next(t for t in species["elf"].traits if t.id == "darkvision")
    breath = next(t for t in species["dragonborn"].traits if t.id == "breath_weapon")

    assert "атака на 24/96 м." in goblin_bow.summary
    assert elf_darkvision.summary == "Видит в темноте на 18 м."
    assert "конус 4,5 м или линия 9 м." in breath.summary


def test_every_distance_in_srd_texts_is_a_multiple_of_one_and_a_half_meters():
    # Тексты SRD пишутся руками — риск опечаток (спека DND-2, §18). Любое расстояние SRD —
    # целое число шагов по 1,5 м.
    files_text = {t: (SRD_DIR / f"{t}.yaml").read_text(encoding="utf-8") for t in TABLE_MODELS}
    distances = [
        (table, value)
        for table, text in files_text.items()
        for match in METERS_IN_TEXT.finditer(text)
        for value in match.groups()
        if value
    ]

    assert distances, "в текстах SRD не найдено ни одного расстояния в метрах"
    assert [d for d in distances if round(float(d[1].replace(",", ".")) * 10) % 15] == []


# Расстояния из SRD 5.2.1 числами, как в правилах и в движке (скорость 30, лук 80/320). Текст
# записи — их перевод функцией движка: опечатку в любой переписанной строке видно по её id.
SRD_WEAPON_RANGES = {
    "dagger": (20, 60),
    "spear": (20, 60),
    "handaxe": (20, 60),
    "javelin": (30, 120),
    "light_crossbow": (80, 320),
    "shortbow": (80, 320),
    "longbow": (150, 600),
}
SRD_MONSTER_RANGES = {
    ("goblin_warrior", "shortbow"): (80, 320),
    ("goblin_boss", "shortbow"): (80, 320),
    ("skeleton", "shortbow"): (80, 320),
    ("bandit", "light_crossbow"): (80, 320),
    ("bandit_captain", "pistol"): (30, 90),
    ("guard", "spear_thrown"): (20, 60),
    ("bugbear_warrior", "light_hammer_thrown"): (20, 60),
    ("ogre", "javelin_thrown"): (30, 120),
}
SRD_REACH = 10  # досягаемость атаки с флагом reach
SRD_DARKVISION = {
    "dragonborn": 60,
    "dwarf": 120,
    "elf": 60,
    "gnome": 60,
    "orc": 120,
    "tiefling": 60,
}


def test_every_thrown_and_ranged_weapon_text_names_its_srd_range_in_meters(items):
    ranged = {
        item.id
        for item in items.values()
        if item.weapon and ("thrown" in item.weapon.properties or item.weapon.range == "far")
    }

    assert ranged == set(SRD_WEAPON_RANGES)
    assert [
        (item_id, items[item_id].summary)
        for item_id, srd_range in SRD_WEAPON_RANGES.items()
        if meters_text_pair(*srd_range) not in items[item_id].summary
    ] == []


def test_every_ranged_monster_attack_text_names_its_srd_range_in_meters(monsters):
    actions = {(m.id, a.id): a for m in monsters.values() for a in m.actions}
    ranged = {key for key, a in actions.items() if a.kind == "attack" and a.range != "melee"}

    assert ranged == set(SRD_MONSTER_RANGES)
    assert [
        (key, actions[key].summary)
        for key, srd_range in SRD_MONSTER_RANGES.items()
        if meters_text_pair(*srd_range) not in actions[key].summary
    ] == []


def test_reach_attack_texts_name_the_reach_in_meters(monsters):
    with_reach = [(m.id, a) for m in monsters.values() for a in m.actions if a.reach]

    assert with_reach, "в данных нет атак с досягаемостью"
    assert [
        (monster_id, a.id)
        for monster_id, a in with_reach
        if not a.summary.startswith(f"Досягаемость {meters_text(SRD_REACH)}.")
    ] == []


def test_darkvision_texts_name_the_srd_distance_in_meters(species):
    darkvision = {
        species_id: trait.summary
        for species_id, entry in species.items()
        for trait in entry.traits
        if trait.id == "darkvision"
    }

    assert darkvision == {
        species_id: f"Видит в темноте на {meters_text(distance)}."
        for species_id, distance in SRD_DARKVISION.items()
    }
