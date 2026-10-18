"""Состояния: что они меняют в бою, и жизненный цикл «без сознания» от 0 HP.

Таблица `CONDITION_EFFECTS` — по SRD 5.2.1 («Rules Glossary»): преимущество и помеха на
атаки, автопровал спасбросков, нулевая скорость, недееспособность, критические попадания.
Помехи на проверки характеристик и спасброски остаются в `checks.py` (там они нужны броскам).
Функции читают список `ActiveCondition` и ничего не меняют; исключение — `regain_consciousness`,
которая правит переданного персонажа (вызывающий передаёт копию, как в `rest.py`).

Метки на цели (`guiding_bolt`, `hunters_mark`) лежат в тех же списках `conditions`, но в таблице
их нет: неизвестные id таблица молча пропускает, их читает бой по имени.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from dnd_engine.abilities import Ability
from dnd_engine.character import ActiveCondition, Character, DeathSaves
from dnd_engine.dice import Advantage

UNCONSCIOUS = "unconscious"
# Источник «без сознания», полученного при падении до 0 HP: только такое состояние снимается,
# когда существо получает хиты. Сон, оглушение и прочее «без сознания» имеют другой источник.
ZERO_HP_SOURCE = "zero_hp"
DEATH_SAVE_FAILURES_TO_DIE = 3

_STR_DEX = frozenset({Ability.STR, Ability.DEX})
_ADV, _DIS = Advantage.ADVANTAGE, Advantage.DISADVANTAGE


@dataclass(frozen=True)
class ConditionEffects:
    """Эффекты одного состояния; «вплотную» — атакующий на расстоянии 5 футов (вовлечён)."""

    implies: tuple[str, ...] = ()  # другие состояния, которые оно включает по SRD
    own_attacks: Advantage = Advantage.NONE  # на броски атаки самого существа
    spares_source: bool = False  # помеха не действует против того, кто наложил состояние
    against_close: Advantage = Advantage.NONE  # на броски атаки по существу вплотную
    against_distant: Advantage = Advantage.NONE  # то же издали
    auto_fail_saves: frozenset[Ability] = frozenset()
    speed_zero: bool = False
    incapacitated: bool = False
    auto_crit_close: bool = False  # попадание вплотную — критическое
    resists_all_damage: bool = False


# Таблица только для чтения: правила не должны меняться во время игры.
CONDITION_EFFECTS: Mapping[str, ConditionEffects] = MappingProxyType(
    {
        "blinded": ConditionEffects(own_attacks=_DIS, against_close=_ADV, against_distant=_ADV),
        "frightened": ConditionEffects(own_attacks=_DIS),
        "grappled": ConditionEffects(own_attacks=_DIS, spares_source=True, speed_zero=True),
        "incapacitated": ConditionEffects(incapacitated=True),
        "invisible": ConditionEffects(own_attacks=_ADV, against_close=_DIS, against_distant=_DIS),
        "paralyzed": ConditionEffects(
            implies=("incapacitated",),
            against_close=_ADV,
            against_distant=_ADV,
            auto_fail_saves=_STR_DEX,
            speed_zero=True,
            auto_crit_close=True,
        ),
        "petrified": ConditionEffects(
            implies=("incapacitated",),
            against_close=_ADV,
            against_distant=_ADV,
            auto_fail_saves=_STR_DEX,
            speed_zero=True,
            resists_all_damage=True,
        ),
        "poisoned": ConditionEffects(own_attacks=_DIS),
        "prone": ConditionEffects(own_attacks=_DIS, against_close=_ADV, against_distant=_DIS),
        "restrained": ConditionEffects(
            own_attacks=_DIS, against_close=_ADV, against_distant=_ADV, speed_zero=True
        ),
        "stunned": ConditionEffects(
            implies=("incapacitated",),
            against_close=_ADV,
            against_distant=_ADV,
            auto_fail_saves=_STR_DEX,
        ),
        UNCONSCIOUS: ConditionEffects(
            implies=("incapacitated", "prone"),
            against_close=_ADV,
            against_distant=_ADV,
            auto_fail_saves=_STR_DEX,
            speed_zero=True,
            auto_crit_close=True,
        ),
    }
)


def _effects_of(
    conditions: Iterable[ActiveCondition],
) -> list[tuple[ActiveCondition, ConditionEffects]]:
    """Эффекты каждого состояния и включённых в него (Без сознания включает Сбит с ног)."""
    found = []
    for condition in conditions:
        effects = CONDITION_EFFECTS.get(condition.id)
        if effects is None:
            continue
        found.append((condition, effects))
        found += [
            (condition, CONDITION_EFFECTS[implied])
            for implied in effects.implies
            if implied in CONDITION_EFFECTS
        ]
    return found


def attack_modes(conditions: Iterable[ActiveCondition], target_id: str) -> list[Advantage]:
    """Преимущество и помехи на атаку от состояний атакующего (их гасит `combine_advantage`)."""
    return [
        effects.own_attacks
        for condition, effects in _effects_of(conditions)
        if effects.own_attacks != Advantage.NONE
        and not (effects.spares_source and condition.source == target_id)
    ]


def defense_modes(
    conditions: Iterable[ActiveCondition], *, attacker_close: bool
) -> list[Advantage]:
    """Преимущество и помехи на атаку по существу от его состояний."""
    modes = [
        effects.against_close if attacker_close else effects.against_distant
        for _, effects in _effects_of(conditions)
    ]
    return [mode for mode in modes if mode != Advantage.NONE]


def auto_crit(conditions: Iterable[ActiveCondition], *, attacker_close: bool) -> bool:
    """Каждое попадание вплотную по существу критическое (парализован, без сознания)."""
    return attacker_close and any(e.auto_crit_close for _, e in _effects_of(conditions))


def is_incapacitated(conditions: Iterable[ActiveCondition]) -> bool:
    return any(effects.incapacitated for _, effects in _effects_of(conditions))


def cannot_move(conditions: Iterable[ActiveCondition]) -> bool:
    return any(effects.speed_zero for _, effects in _effects_of(conditions))


def resists_all_damage(conditions: Iterable[ActiveCondition]) -> bool:
    return any(effects.resists_all_damage for _, effects in _effects_of(conditions))


def auto_fail_condition(conditions: Iterable[ActiveCondition], ability: Ability) -> str | None:
    """Id состояния, из-за которого спасбросок этой характеристики проваливается без броска."""
    for condition, effects in _effects_of(conditions):
        if ability in effects.auto_fail_saves:
            return condition.id
    return None


# --- смерть и «без сознания» от 0 HP ---


def is_dead(c: Character) -> bool:
    """Мёртвый персонаж: три провала спасбросков от смерти (в том числе от массивного урона)."""
    return c.death_saves.failures >= DEATH_SAVE_FAILURES_TO_DIE


def regain_consciousness(c: Character) -> None:
    """Персонаж получил хиты: «без сознания» от 0 HP снимается, спасброски от смерти обнуляются.

    Правит переданного персонажа: вызывающий отвечает за копию."""
    c.conditions = [
        x for x in c.conditions if not (x.id == UNCONSCIOUS and x.source == ZERO_HP_SOURCE)
    ]
    c.death_saves = DeathSaves()
