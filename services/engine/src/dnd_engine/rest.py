"""Короткий и длинный отдых персонажа.

Функции чистые: получают персонажа и возвращают его обновлённую копию и итог отдыха
(`RestOutcome`). Входной объект не меняется. Кости хитов на коротком отдыхе бросаются только
через переданный `Rng`. Максимум использований ресурса берётся из таблицы класса в данных SRD.
"""

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel

from dnd_engine.abilities import Ability
from dnd_engine.character import Character, DeathSaves, ability_mod
from dnd_engine.content import ClassDef, SrdContent
from dnd_engine.dice import RollResult, roll
from dnd_engine.rng import Rng

# Нижняя граница лечения одной костью хитов. План DND-1 задаёт 0; в SRD 5.2.1 стоит минимум 1 HP.
MIN_HEAL_PER_DIE: Final = 0

# Ресурсы класса, которые возвращает короткий отдых (SRD 5.2.1). Остальные ресурсы класса
# (Метка охотника, Магическое восстановление) возвращает только длинный отдых.
SHORT_REST_ONE_USE = frozenset({"second_wind", "channel_divinity", "rage"})  # одно использование
SHORT_REST_ALL_USES = frozenset({"action_surge"})  # все использования

RESOURCEFUL_TRAIT = "resourceful"  # Находчивость человека: вдохновение после длинного отдыха


class RestOutcome(BaseModel):
    character_id: str
    hp_gained: int
    hit_dice_spent: int
    rolls: list[RollResult]
    restored: list[str]  # ресурсы и "spell_slots": только то, что действительно восстановилось


# Правило возврата: (id ресурса, текущее число, максимум) -> новое число использований.
Recharge = Callable[[str, int, int], int]


def _after_short_rest(name: str, current: int, maximum: int) -> int:
    if name in SHORT_REST_ALL_USES:
        return maximum
    if name in SHORT_REST_ONE_USE:
        return min(maximum, current + 1)
    return current


def _after_long_rest(name: str, current: int, maximum: int) -> int:
    return maximum


def _recharge(c: Character, cls: ClassDef, recharge: Recharge) -> tuple[dict[str, int], list[str]]:
    """Новые значения ресурсов и список тех, что выросли. Ресурсы вне таблицы класса не трогаются."""
    resources = dict(c.resources)
    restored: list[str] = []
    for name, current in c.resources.items():
        by_level = cls.resources.get(name, {})
        if c.level not in by_level:
            continue
        resources[name] = recharge(name, current, by_level[c.level])
        if resources[name] > current:
            restored.append(name)
    return resources, restored


def short_rest(
    c: Character, *, spend_hit_dice: int, rng: Rng, content: SrdContent
) -> tuple[Character, RestOutcome]:
    """Короткий отдых: потраченные кости хитов лечат `1dX + Тел`, часть ресурсов возвращается."""
    if not 0 <= spend_hit_dice <= c.hit_dice:
        raise ValueError(
            f"нельзя потратить {spend_hit_dice} костей хитов: у персонажа их {c.hit_dice}"
        )
    cls = content.classes[c.class_id]
    constitution = ability_mod(c, Ability.CON)
    rolls = [
        roll(f"1d{cls.hit_die}{constitution:+d}", rng=rng, reason="Короткий отдых: кость хитов")
        for _ in range(spend_hit_dice)
    ]
    healed = sum(max(MIN_HEAL_PER_DIE, r.total) for r in rolls)
    hp_gained = max(0, min(healed, c.hp_max - c.hp))
    resources, restored = _recharge(c, cls, _after_short_rest)

    rested = c.model_copy(deep=True)
    rested.hp = c.hp + hp_gained
    rested.hit_dice = c.hit_dice - spend_hit_dice
    rested.resources = resources
    outcome = RestOutcome(
        character_id=c.id,
        hp_gained=hp_gained,
        hit_dice_spent=spend_hit_dice,
        rolls=rolls,
        restored=restored,
    )
    return rested, outcome


def long_rest(c: Character, content: SrdContent) -> tuple[Character, RestOutcome]:
    """Длинный отдых: HP, ячейки и ресурсы полностью, половина костей хитов, конец эффектов."""
    cls = content.classes[c.class_id]
    resources, restored = _recharge(c, cls, _after_long_rest)
    if c.spell_slots != c.spell_slots_max:
        restored.append("spell_slots")
    resourceful = any(t.id == RESOURCEFUL_TRAIT for t in content.species[c.species].traits)

    # Копия глубокая: списки состояний и эффектов ниже строятся из элементов копии, а не оригинала.
    rested = c.model_copy(deep=True)
    rested.hp = c.hp_max
    rested.temp_hp = 0
    rested.hit_dice = min(c.hit_dice_max, c.hit_dice + max(1, c.hit_dice_max // 2))
    rested.spell_slots = dict(c.spell_slots_max)
    rested.resources = resources
    rested.death_saves = DeathSaves()
    rested.concentration = None
    # За 8 часов отдыха заканчивается всё, что имеет срок; бессрочное остаётся.
    rested.conditions = [x for x in rested.conditions if x.rounds_left is None]
    rested.buffs = [x for x in rested.buffs if x.rounds_left is None]
    rested.inspiration = c.inspiration or resourceful
    outcome = RestOutcome(
        character_id=c.id,
        hp_gained=max(0, c.hp_max - c.hp),
        hit_dice_spent=0,
        rolls=[],
        restored=restored,
    )
    return rested, outcome
