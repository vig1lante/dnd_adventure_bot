"""Характеристики и навыки D&D 5e (SRD 5.2) и две базовые формулы."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class Ability(StrEnum):
    STR = "str"
    DEX = "dex"
    CON = "con"
    INT = "int"
    WIS = "wis"
    CHA = "cha"


class Skill(StrEnum):
    ATHLETICS = "athletics"
    ACROBATICS = "acrobatics"
    SLEIGHT_OF_HAND = "sleight_of_hand"
    STEALTH = "stealth"
    ARCANA = "arcana"
    HISTORY = "history"
    INVESTIGATION = "investigation"
    NATURE = "nature"
    RELIGION = "religion"
    ANIMAL_HANDLING = "animal_handling"
    INSIGHT = "insight"
    MEDICINE = "medicine"
    PERCEPTION = "perception"
    SURVIVAL = "survival"
    DECEPTION = "deception"
    INTIMIDATION = "intimidation"
    PERFORMANCE = "performance"
    PERSUASION = "persuasion"


# Таблицы только для чтения: правила не должны меняться во время игры.
SKILL_ABILITY: Mapping[Skill, Ability] = MappingProxyType(
    {
        Skill.ATHLETICS: Ability.STR,
        Skill.ACROBATICS: Ability.DEX,
        Skill.SLEIGHT_OF_HAND: Ability.DEX,
        Skill.STEALTH: Ability.DEX,
        Skill.ARCANA: Ability.INT,
        Skill.HISTORY: Ability.INT,
        Skill.INVESTIGATION: Ability.INT,
        Skill.NATURE: Ability.INT,
        Skill.RELIGION: Ability.INT,
        Skill.ANIMAL_HANDLING: Ability.WIS,
        Skill.INSIGHT: Ability.WIS,
        Skill.MEDICINE: Ability.WIS,
        Skill.PERCEPTION: Ability.WIS,
        Skill.SURVIVAL: Ability.WIS,
        Skill.DECEPTION: Ability.CHA,
        Skill.INTIMIDATION: Ability.CHA,
        Skill.PERFORMANCE: Ability.CHA,
        Skill.PERSUASION: Ability.CHA,
    }
)

ABILITY_NAMES_RU: Mapping[Ability, str] = MappingProxyType(
    {
        Ability.STR: "Сила",
        Ability.DEX: "Ловкость",
        Ability.CON: "Телосложение",
        Ability.INT: "Интеллект",
        Ability.WIS: "Мудрость",
        Ability.CHA: "Харизма",
    }
)

SKILL_NAMES_RU: Mapping[Skill, str] = MappingProxyType(
    {
        Skill.ATHLETICS: "Атлетика",
        Skill.ACROBATICS: "Акробатика",
        Skill.SLEIGHT_OF_HAND: "Ловкость рук",
        Skill.STEALTH: "Скрытность",
        Skill.ARCANA: "Магия",
        Skill.HISTORY: "История",
        Skill.INVESTIGATION: "Анализ",
        Skill.NATURE: "Природа",
        Skill.RELIGION: "Религия",
        Skill.ANIMAL_HANDLING: "Уход за животными",
        Skill.INSIGHT: "Проницательность",
        Skill.MEDICINE: "Медицина",
        Skill.PERCEPTION: "Внимательность",
        Skill.SURVIVAL: "Выживание",
        Skill.DECEPTION: "Обман",
        Skill.INTIMIDATION: "Запугивание",
        Skill.PERFORMANCE: "Выступление",
        Skill.PERSUASION: "Убеждение",
    }
)


def modifier(score: int) -> int:
    # Целочисленное деление округляет вниз, как требуют правила: 9 -> -1, 8 -> -1.
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    if level < 1:
        raise ValueError(f"уровень должен быть не меньше 1: {level}")
    return 2 + (level - 1) // 4
