"""Короткий и длинный отдых персонажа.

Функции чистые: получают персонажа и возвращают его обновлённую копию и итог отдыха
(`RestOutcome`). Входной объект не меняется. Мёртвый персонаж (три провала спасбросков от
смерти) пользы от отдыха не получает; лежавший без сознания на 0 HP приходит в себя, получив
хиты. Кости хитов на коротком отдыхе бросаются только через переданный `Rng`. Максимум
использований ресурса берётся из таблицы класса в данных SRD.

Здесь же — окончание эффектов с последствиями (`end_buffs`: «Подмога» возвращает максимум HP)
и Магическое восстановление волшебника (`arcane_recovery`): их вызывают и отдых, и бой.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import BaseModel

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveBuff, Character, ability_mod, prof_bonus
from dnd_engine.conditions import is_dead, regain_consciousness
from dnd_engine.content import ClassDef, SrdContent
from dnd_engine.dice import RollResult, roll
from dnd_engine.rng import Rng

# Нижняя граница лечения одной костью хитов. План DND-1 задаёт 0; в SRD 5.2.1 стоит минимум 1 HP.
MIN_HEAL_PER_DIE: Final = 0

# Ресурсы класса, которые возвращает короткий отдых (SRD 5.2.1). Остальные ресурсы класса
# (Метка охотника, Магическое восстановление) возвращает только длинный отдых.
SHORT_REST_ONE_USE = frozenset({"second_wind", "channel_divinity", "rage"})  # одно использование
SHORT_REST_ALL_USES = frozenset({"action_surge"})  # все использования

AID_BUFF = "aid"  # «Подмога»: максимум и текущие HP выше на data["hp"], пока эффект длится
# Дыхание драконорождённого: оставшиеся использования лежат в `resources[BREATH_WEAPON_TRAIT]`;
# нет записи — все использования (бонус мастерства) целы. Длинный отдых возвращает их.
BREATH_WEAPON_TRAIT = "breath_weapon"
ARCANE_RECOVERY = "arcane_recovery"  # ресурс класса «Магическое восстановление»
ARCANE_RECOVERY_MAX_SLOT = 5  # ячейки 6-го уровня и выше не возвращаются

RESOURCEFUL_TRAIT = "resourceful"  # Находчивость человека: вдохновение после длинного отдыха
# Неутомимость орка: раз за длинный отдых остаться с 1 HP. В `resources` значение 1 или отсутствие
# записи — не использована, 0 — использована (запись появляется, когда бой тратит эту способность).
RELENTLESS_ENDURANCE_TRAIT = "relentless_endurance"


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
    """Новые значения ресурсов и список выросших. Ресурсы вне таблицы класса не трогаются."""
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


def _no_rest(c: Character) -> tuple[Character, RestOutcome]:
    """Отдых мёртвого: копия без изменений и пустой итог (кости не тратятся и не бросаются)."""
    nothing = RestOutcome(character_id=c.id, hp_gained=0, hit_dice_spent=0, rolls=[], restored=[])
    return c.model_copy(deep=True), nothing


def end_buffs(c: Character, ended: Callable[[ActiveBuff], bool]) -> list[ActiveBuff]:
    """Заканчивает эффекты персонажа, для которых `ended` истинно; возвращает закончившиеся.

    Единственное место, где эффект уходит из `c.buffs` с последствиями: «Подмога» возвращает
    максимум HP, а текущие HP не остаются выше него. Поэтому срок эффекта, потеря концентрации
    и отдых убирают эффекты только через эту функцию. Правит переданного персонажа: вызывающий
    отвечает за копию."""
    kept, gone = [], []
    for buff in c.buffs:
        (gone if ended(buff) else kept).append(buff)
    c.buffs = kept
    for buff in gone:
        if buff.id == AID_BUFF:
            c.hp_max -= int(buff.data.get("hp", 0))
            c.hp = min(c.hp, c.hp_max)
    return gone


def short_rest(
    c: Character, *, spend_hit_dice: int, rng: Rng, content: SrdContent
) -> tuple[Character, RestOutcome]:
    """Короткий отдых: потраченные кости хитов лечат `1dX + Тел`, часть ресурсов возвращается."""
    if not 0 <= spend_hit_dice <= c.hit_dice:
        raise ValueError(
            f"нельзя потратить {spend_hit_dice} костей хитов: у персонажа их {c.hit_dice}"
        )
    if is_dead(c):
        return _no_rest(c)
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
    if c.hp == 0 and hp_gained > 0:
        regain_consciousness(rested)
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
    if is_dead(c):
        return _no_rest(c)
    cls = content.classes[c.class_id]
    resources, restored = _recharge(c, cls, _after_long_rest)
    if c.spell_slots != c.spell_slots_max:
        restored.append("spell_slots")
    trait_ids = {t.id for t in content.species[c.species].traits}
    resourceful = RESOURCEFUL_TRAIT in trait_ids
    if RELENTLESS_ENDURANCE_TRAIT in trait_ids and resources.get(RELENTLESS_ENDURANCE_TRAIT, 1) < 1:
        resources[RELENTLESS_ENDURANCE_TRAIT] = 1
        restored.append(RELENTLESS_ENDURANCE_TRAIT)

    uses = prof_bonus(c)
    if BREATH_WEAPON_TRAIT in trait_ids and resources.get(BREATH_WEAPON_TRAIT, uses) < uses:
        resources[BREATH_WEAPON_TRAIT] = uses
        restored.append(BREATH_WEAPON_TRAIT)

    # Копия глубокая: списки состояний и эффектов ниже строятся из элементов копии, а не оригинала.
    rested = c.model_copy(deep=True)
    # Эффекты со сроком кончаются раньше, чем считается полное здоровье: «Подмога» вернёт максимум.
    end_buffs(rested, lambda x: x.rounds_left is not None)
    rested.hp = rested.hp_max
    rested.temp_hp = 0
    rested.hit_dice = min(c.hit_dice_max, c.hit_dice + max(1, c.hit_dice_max // 2))
    rested.spell_slots = dict(c.spell_slots_max)
    rested.resources = resources
    rested.concentration = None
    # За 8 часов отдыха заканчивается всё, что имеет срок; бессрочное остаётся.
    rested.conditions = [x for x in rested.conditions if x.rounds_left is None]
    regain_consciousness(rested)  # сбрасывает и спасброски от смерти
    rested.inspiration = c.inspiration or resourceful
    outcome = RestOutcome(
        character_id=c.id,
        hp_gained=max(0, rested.hp - c.hp),
        hit_dice_spent=0,
        rolls=[],
        restored=restored,
    )
    return rested, outcome


def arcane_recovery(
    c: Character, levels: Sequence[int], content: SrdContent
) -> tuple[Character, RestOutcome]:
    """Магическое восстановление волшебника: возвращает потраченные ячейки после короткого отдыха.

    `levels` — уровни ячеек, по одной записи на ячейку (`[1, 1]` — две ячейки 1-го уровня).
    Суммарный уровень не больше половины уровня волшебника с округлением вверх, ячейки 6-го
    уровня и выше не возвращаются, ячеек не может стать больше максимума; использование одно
    до длинного отдыха. Вызывайте один раз после короткого отдыха (сам отдых здесь не
    проверяется). Нарушение — `ValueError`, персонаж не меняется. Мёртвый ничего не получает.
    """
    cls = content.classes[c.class_id]
    if c.level not in cls.resources.get(ARCANE_RECOVERY, {}):
        raise ValueError(f"у класса {cls.name} нет Магического восстановления")
    if is_dead(c):
        return _no_rest(c)
    if c.resources.get(ARCANE_RECOVERY, 0) < 1:
        raise ValueError("Магическое восстановление уже использовано до длинного отдыха")
    if not levels or min(levels) < 1:
        raise ValueError("нужно выбрать хотя бы одну ячейку, уровни ячеек — от 1")
    if max(levels) > ARCANE_RECOVERY_MAX_SLOT:
        raise ValueError(f"ячейки {ARCANE_RECOVERY_MAX_SLOT + 1}-го уровня и выше не возвращаются")
    for slot_level, count in Counter(levels).items():
        if slot_level not in c.spell_slots_max:
            raise ValueError(f"у персонажа нет ячеек {slot_level}-го уровня")
        if c.spell_slots.get(slot_level, 0) + count > c.spell_slots_max[slot_level]:
            raise ValueError(f"ячеек {slot_level}-го уровня станет больше, чем максимум")
    allowed = (c.level + 1) // 2
    if sum(levels) > allowed:
        raise ValueError(
            f"суммарный уровень ячеек {sum(levels)} больше допустимого {allowed} "
            "(половина уровня волшебника, вверх)"
        )

    recovered = c.model_copy(deep=True)
    for slot_level in levels:
        recovered.spell_slots[slot_level] = recovered.spell_slots.get(slot_level, 0) + 1
    recovered.resources[ARCANE_RECOVERY] = c.resources[ARCANE_RECOVERY] - 1
    outcome = RestOutcome(
        character_id=c.id, hp_gained=0, hit_dice_spent=0, rolls=[], restored=["spell_slots"]
    )
    return recovered, outcome
