"""Персонаж и числа, которые движок выводит из его характеристик и данных SRD.

Модели хранят только исходные данные. Бонусы навыков, КД, атаки и Сл заклинаний
считают функции этого модуля: ручного ввода производных значений нет. Функции
чистые — без сети, БД и бросков; все нужные данные приходят аргументами.
"""

from typing import Literal, Self

from pydantic import BaseModel, field_validator, model_validator

from dnd_engine.abilities import SKILL_ABILITY, Ability, Skill, modifier, proficiency_bonus
from dnd_engine.content import ArmorProps, ClassDef, ItemDef, SrdContent, WeaponProps

# id «оружия» для атаки подручным предметом: его же принимает бой (`weapon="improvised"`).
IMPROVISED_WEAPON_ID = "improvised"

MAGE_ARMOR_BASE_AC = 13  # заклинание «Доспехи мага»: 13 + Лов вместо 10 + Лов
DEFENSE_STYLE_AC_BONUS = 1  # стиль «Оборона»: только пока надет доспех
ARCHERY_ATTACK_BONUS = 2  # стиль «Стрельба»: к броску атаки дальнобойным оружием
DUELING_DAMAGE_BONUS = 2  # стиль «Дуэлянт»: к урону одноручным рукопашным оружием


class InventoryItem(BaseModel):
    item_id: str
    qty: int = 1
    equipped: bool = False


class ActiveCondition(BaseModel):
    id: str
    rounds_left: int | None = None
    source: str = ""
    save: Ability | None = None  # повторный спасбросок в конце хода
    dc: int | None = None


class ActiveBuff(BaseModel):
    id: str
    rounds_left: int | None
    source_id: str
    data: dict[str, int | str] = {}


class DeathSaves(BaseModel):
    successes: int = 0
    failures: int = 0
    stable: bool = False


class Persona(BaseModel):
    concept: str = ""
    appearance: str = ""
    traits: str = ""
    ideals: str = ""
    bonds: str = ""
    flaws: str = ""
    backstory: str = ""


class Character(BaseModel):
    id: str
    name: str
    level: int = 3
    species: str
    class_id: str
    background: str
    scores: dict[Ability, int]
    skill_proficiencies: list[Skill]
    expertise: list[Skill] = []
    save_proficiencies: list[Ability]
    fighting_style: str | None = None
    hp_max: int
    hp: int
    temp_hp: int = 0
    hit_dice_max: int
    hit_dice: int
    spell_slots_max: dict[int, int] = {}
    spell_slots: dict[int, int] = {}
    cantrips: list[str] = []
    prepared_spells: list[str] = []
    concentration: str | None = None  # id заклинания
    inventory: list[InventoryItem] = []
    conditions: list[ActiveCondition] = []
    buffs: list[ActiveBuff] = []
    resources: dict[str, int] = {}  # текущие использования
    inspiration: bool = False
    death_saves: DeathSaves = DeathSaves()
    persona: Persona = Persona()
    player_id: str | None = None

    @field_validator("scores")
    @classmethod
    def _scores_cover_every_ability(cls, scores: dict[Ability, int]) -> dict[Ability, int]:
        # Без проверки пропущенная характеристика всплыла бы KeyError посреди хода мастера.
        if missing := set(Ability) - scores.keys():
            raise ValueError("не хватает характеристик: " + ", ".join(sorted(missing)))
        return scores

    @model_validator(mode="after")
    def _expertise_needs_proficiency(self) -> Self:
        if without := set(self.expertise) - set(self.skill_proficiencies):
            raise ValueError(
                "компетентность возможна только в навыках с владением: "
                + ", ".join(sorted(without))
            )
        return self


class AttackProfile(BaseModel):
    item_id: str
    name: str
    bonus: int  # к броску атаки
    damage: str  # запись костей, например "1d8+3"
    damage_type: str
    range: Literal["melee", "near", "far"]
    ability: Ability


def _class_def(c: Character, content: SrdContent) -> ClassDef:
    try:
        return content.classes[c.class_id]
    except KeyError:
        raise ValueError(f"неизвестный класс {c.class_id!r}") from None


# --- характеристики, навыки, спасброски ---


def ability_mod(c: Character, ability: Ability) -> int:
    return modifier(c.scores[ability])


def prof_bonus(c: Character) -> int:
    return proficiency_bonus(c.level)


def skill_bonus(c: Character, skill: Skill) -> int:
    bonus = ability_mod(c, SKILL_ABILITY[skill])
    if skill in c.expertise:
        return bonus + 2 * prof_bonus(c)
    if skill in c.skill_proficiencies:
        return bonus + prof_bonus(c)
    return bonus


def save_bonus(c: Character, ability: Ability) -> int:
    bonus = ability_mod(c, ability)
    return bonus + prof_bonus(c) if ability in c.save_proficiencies else bonus


def passive_perception(c: Character) -> int:
    return 10 + skill_bonus(c, Skill.PERCEPTION)


# --- класс доспеха ---


def _capped_dex(dex_mod: int, dex_cap: int | None) -> int:
    if dex_cap is None:
        return dex_mod
    if dex_cap == 0:
        # Тяжёлый доспех: Ловкость не прибавляется вовсе, даже отрицательная.
        return 0
    return min(dex_mod, dex_cap)


def _worn_armor(c: Character, content: SrdContent) -> list[ArmorProps]:
    """Надетые доспехи и щиты; предметы вне SRD (из приключения) доспехами не считаются."""
    items = (content.items.get(entry.item_id) for entry in c.inventory if entry.equipped)
    return [item.armor for item in items if item is not None and item.armor is not None]


def _unarmored_base_ac(c: Character) -> int:
    """Лучшая из формул КД без доспеха: обычная, «Доспехи мага», защита варвара."""
    dex = ability_mod(c, Ability.DEX)
    formulas = [10 + dex]
    if any(buff.id == "mage_armor" for buff in c.buffs):
        formulas.append(MAGE_ARMOR_BASE_AC + dex)
    if c.class_id == "barbarian":
        formulas.append(10 + dex + ability_mod(c, Ability.CON))
    return max(formulas)


def armor_class(c: Character, content: SrdContent) -> int:
    worn = _worn_armor(c, content)
    # Два надетых доспеха — недопустимое состояние инвентаря; берём первый.
    body = next((armor for armor in worn if armor.kind != "shield"), None)
    # У щита base_ac — бонус к КД; несколько щитов не складываются.
    shield = max((armor.base_ac for armor in worn if armor.kind == "shield"), default=0)
    if body is None:
        return _unarmored_base_ac(c) + shield
    defense = DEFENSE_STYLE_AC_BONUS if c.fighting_style == "defense" else 0
    dex = _capped_dex(ability_mod(c, Ability.DEX), body.dex_cap)
    return body.base_ac + dex + defense + shield


# --- заклинания ---


def _spellcasting_ability(c: Character, content: SrdContent) -> Ability | None:
    spellcasting = _class_def(c, content).spellcasting
    return None if spellcasting is None else spellcasting.ability


def spell_save_dc(c: Character, content: SrdContent) -> int | None:
    ability = _spellcasting_ability(c, content)
    return None if ability is None else 8 + prof_bonus(c) + ability_mod(c, ability)


def spell_attack_bonus(c: Character, content: SrdContent) -> int | None:
    ability = _spellcasting_ability(c, content)
    return None if ability is None else prof_bonus(c) + ability_mod(c, ability)


# --- атака ---


def _damage_text(dice: str, flat: int) -> str:
    return dice if flat == 0 else f"{dice}{flat:+d}"


def _find_weapon(c: Character, item_id: str, content: SrdContent) -> tuple[ItemDef, WeaponProps]:
    if not any(entry.item_id == item_id for entry in c.inventory):
        raise ValueError(f"у персонажа {c.id} нет предмета {item_id!r}")
    item = content.items.get(item_id)
    if item is None or item.weapon is None:
        raise ValueError(f"предмет {item_id!r} не оружие")
    return item, item.weapon


def _attack_ability(c: Character, weapon: WeaponProps) -> Ability:
    if weapon.range != "melee":
        return Ability.DEX
    # При равенстве берётся Сила: от неё зависят бонусы ярости.
    finesse_prefers_dex = ability_mod(c, Ability.DEX) > ability_mod(c, Ability.STR)
    return Ability.DEX if "finesse" in weapon.properties and finesse_prefers_dex else Ability.STR


def _off_hand_is_free(c: Character, weapon_id: str, content: SrdContent) -> bool:
    """Вторая рука свободна, если не надеты ни другое оружие, ни щит."""
    for entry in c.inventory:
        item = content.items.get(entry.item_id)
        if not entry.equipped or entry.item_id == weapon_id or item is None:
            continue
        if item.weapon is not None or (item.armor is not None and item.armor.kind == "shield"):
            return False
    return True


def _gets_dueling_bonus(
    c: Character, item_id: str, weapon: WeaponProps, content: SrdContent
) -> bool:
    return (
        c.fighting_style == "dueling"
        and weapon.range == "melee"
        and "two_handed" not in weapon.properties
        and _off_hand_is_free(c, item_id, content)
    )


def weapon_attack(c: Character, item_id: str, content: SrdContent) -> AttackProfile:
    item, weapon = _find_weapon(c, item_id, content)
    ability = _attack_ability(c, weapon)
    ability_bonus = ability_mod(c, ability)

    attack_bonus = ability_bonus
    if weapon.category in _class_def(c, content).weapons:
        attack_bonus += prof_bonus(c)
    if weapon.range != "melee" and c.fighting_style == "archery":
        attack_bonus += ARCHERY_ATTACK_BONUS

    damage_bonus = ability_bonus
    if _gets_dueling_bonus(c, item_id, weapon, content):
        damage_bonus += DUELING_DAMAGE_BONUS

    return AttackProfile(
        item_id=item_id,
        name=item.name,
        bonus=attack_bonus,
        damage=_damage_text(weapon.damage, damage_bonus),
        damage_type=weapon.damage_type,
        range=weapon.range,
        ability=ability,
    )


def improvised_attack(c: Character) -> AttackProfile:
    """Подручный предмет: 1к4 дробящего, Сила, без бонуса мастерства."""
    strength = ability_mod(c, Ability.STR)
    return AttackProfile(
        item_id=IMPROVISED_WEAPON_ID,
        name="Импровизированное оружие",
        bonus=strength,
        damage=_damage_text("1d4", strength),
        damage_type="bludgeoning",
        range="melee",
        ability=Ability.STR,
    )
