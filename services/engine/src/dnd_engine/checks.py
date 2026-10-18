"""Проверки характеристик и навыков, спасброски и состязания.

Сложность Сл берётся только из таблицы 5/10/15/20/25/30. Все броски идут через
`dice.roll` с переданным `Rng`; итог считает движок. Источники преимущества и помехи
(параметр вызова, черты вида, состояния) собираются в один режим по правилу 5e:
преимущество и помеха из любых источников гасят друг друга. Функции чистые: персонаж
не меняется, результат — новый объект.
"""

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel

from dnd_engine.abilities import SKILL_ABILITY, Ability, Skill
from dnd_engine.character import Character, ability_mod, save_bonus, skill_bonus
from dnd_engine.dice import Advantage, RollResult, roll
from dnd_engine.rng import Rng

# Таблица только для чтения: правила не должны меняться во время игры.
DC_TABLE: Mapping[str, int] = MappingProxyType(
    {
        "very_easy": 5,
        "easy": 10,
        "medium": 15,
        "hard": 20,
        "very_hard": 25,
        "nearly_impossible": 30,
    }
)
VALID_DCS = frozenset(DC_TABLE.values())

# Механические черты видов, влияющие на к20-броски. Сигнатуры функций не принимают
# SrdContent, поэтому черта определяется по id вида; соответствие данным SRD проверяет тест.
LUCK_TRAIT = "luck"  # Удача полурослика
GNOMISH_CUNNING_TRAIT = "gnomish_cunning"  # Гномья хитрость
DWARVEN_RESILIENCE_TRAIT = "dwarven_resilience"  # Дварфийская стойкость
CHECK_TRAIT_BY_SPECIES: Mapping[str, str] = MappingProxyType(
    {
        "halfling": LUCK_TRAIT,
        "gnome": GNOMISH_CUNNING_TRAIT,
        "dwarf": DWARVEN_RESILIENCE_TRAIT,
    }
)

GNOMISH_CUNNING_ABILITIES = frozenset({Ability.INT, Ability.WIS, Ability.CHA})
# SRD 5.2.1: преимущество на спасброски, «чтобы избежать или прекратить состояние Отравлен».
# Метка `against` — id состояния («poisoned») или тип угрозы («poison»); принимаем обе.
DWARVEN_RESILIENCE_AGAINST = frozenset({"poison", "poisoned"})

# Состояния с помехой на проверки характеристик и навыков (не на спасброски).
CHECK_DISADVANTAGE_CONDITIONS = frozenset({"poisoned", "frightened"})
# Состояния с помехой на спасброски отдельных характеристик.
SAVE_DISADVANTAGE_CONDITIONS: Mapping[str, frozenset[Ability]] = MappingProxyType(
    {"restrained": frozenset({Ability.DEX})}
)

BLESS_BUFF = "bless"  # +1d4 к спасброскам


class CheckKind(StrEnum):
    ABILITY = "ability"
    SKILL = "skill"
    SAVE = "save"


class CheckResult(BaseModel):
    actor_id: str
    kind: CheckKind
    ability: Ability
    skill: Skill | None
    dc: int
    bonus: int  # постоянный бонус проверки; кость благословения в него не входит
    roll: RollResult
    total: int
    success: bool
    auto: Literal["nat20", "nat1"] | None = None


# Хук получает итог проверки и возвращает (возможно изменённый) итог.
CheckHook = Callable[[CheckResult], CheckResult]


class ContestSide(BaseModel):
    actor_id: str
    label: str
    bonus: int


class ContestResult(BaseModel):
    a: ContestSide
    b: ContestSide
    a_roll: RollResult
    b_roll: RollResult
    winner: Literal["a", "b"]  # ничья — побеждает b (защищающийся)


# --- источники преимущества и помехи ---


def _combine_advantage(*modes: Advantage) -> Advantage:
    """Преимущество и помеха из любых источников гасят друг друга, их число не важно."""
    modes = tuple(Advantage(mode) for mode in modes)
    has_advantage = Advantage.ADVANTAGE in modes
    has_disadvantage = Advantage.DISADVANTAGE in modes
    if has_advantage == has_disadvantage:
        return Advantage.NONE
    return Advantage.ADVANTAGE if has_advantage else Advantage.DISADVANTAGE


def _species_advantage(
    c: Character, kind: CheckKind, ability: Ability, against: str | None
) -> Advantage:
    """Гномья хитрость и Дварфийская стойкость: преимущество только на спасброски."""
    if kind != CheckKind.SAVE:
        return Advantage.NONE
    trait = CHECK_TRAIT_BY_SPECIES.get(c.species)
    gnomish = trait == GNOMISH_CUNNING_TRAIT and ability in GNOMISH_CUNNING_ABILITIES
    dwarven = trait == DWARVEN_RESILIENCE_TRAIT and against in DWARVEN_RESILIENCE_AGAINST
    return Advantage.ADVANTAGE if gnomish or dwarven else Advantage.NONE


def _condition_advantage(c: Character, kind: CheckKind, ability: Ability) -> Advantage:
    """Помеха от состояний персонажа по таблицам выше."""
    for condition in c.conditions:
        if kind != CheckKind.SAVE and condition.id in CHECK_DISADVANTAGE_CONDITIONS:
            return Advantage.DISADVANTAGE
        if kind == CheckKind.SAVE and ability in SAVE_DISADVANTAGE_CONDITIONS.get(condition.id, ()):
            return Advantage.DISADVANTAGE
    return Advantage.NONE


# --- броски ---


def _add_roll(base: RollResult, extra: RollResult) -> RollResult:
    """Приклеивает отдельный бросок к основному: кости видны в `rolls`, сумма — в `total`.

    `natural` остаётся значением к20: по нему работают хуки (натуральные 20 и 1)."""
    return base.model_copy(
        update={
            "expr": f"{base.expr}+{extra.expr}",
            "rolls": [*base.rolls, *extra.rolls],
            "kept": [*base.kept, *extra.kept],
            "total": base.total + extra.total,
        }
    )


def _reroll_natural_one(first: RollResult, *, rng: Rng) -> RollResult:
    """Удача полурослика: единицу на к20 можно перебросить один раз, новое значение обязательно.

    Заменяется ровно та кость, что дала единицу. Вторая кость (при преимуществе или помехе)
    остаётся в игре и участвует в выборе по обычному правилу. Оба броска видны в `rolls`."""
    again = roll("1d20", rng=rng, reason=f"{first.reason} (Удача)")
    other_dice = [max(first.rolls)] if len(first.rolls) == 2 else []
    candidates = [*again.rolls, *other_dice]
    natural = max(candidates) if first.advantage == Advantage.ADVANTAGE else min(candidates)
    return first.model_copy(
        update={
            "rolls": [*first.rolls, *again.rolls],
            "kept": [natural],
            "total": natural + first.modifier,
            "natural": natural,
        }
    )


def _check_dc(dc: int) -> None:
    if dc not in VALID_DCS:
        raise ValueError(f"Сл {dc} не из таблицы, допустимы {sorted(VALID_DCS)}")


def _d20_test(
    c: Character,
    kind: CheckKind,
    ability: Ability,
    skill: Skill | None,
    *,
    bonus: int,
    dc: int,
    advantage: Advantage,
    against: str | None,
    rng: Rng,
    reason: str,
    hooks: Sequence[CheckHook],
) -> CheckResult:
    """Общий ход проверки и спасброска: режим броска, к20, Удача, благословение, хуки."""
    _check_dc(dc)
    mode = _combine_advantage(
        advantage,
        _species_advantage(c, kind, ability, against),
        _condition_advantage(c, kind, ability),
    )
    d20 = roll(f"1d20{bonus:+d}", rng=rng, advantage=mode, reason=reason)
    if d20.natural == 1 and CHECK_TRAIT_BY_SPECIES.get(c.species) == LUCK_TRAIT:
        d20 = _reroll_natural_one(d20, rng=rng)
    if kind == CheckKind.SAVE and any(buff.id == BLESS_BUFF for buff in c.buffs):
        d20 = _add_roll(d20, roll("1d4", rng=rng, reason=f"{reason} (Благословение)"))

    result = CheckResult(
        actor_id=c.id,
        kind=kind,
        ability=ability,
        skill=skill,
        dc=dc,
        bonus=bonus,
        roll=d20,
        total=d20.total,
        success=d20.total >= dc,
    )
    for hook in hooks:
        result = hook(result)
    return result


def ability_check(
    c: Character,
    ability: Ability,
    *,
    skill: Skill | None,
    dc: int,
    advantage: Advantage,
    rng: Rng,
    reason: str,
    hooks: Sequence[CheckHook] = (),
) -> CheckResult:
    """Проверка характеристики или навыка против Сл.

    Навык можно применить к нестандартной характеристике («Сила (Запугивание)»):
    берётся модификатор указанной характеристики и мастерство навыка."""
    if skill is None:
        kind, bonus = CheckKind.ABILITY, ability_mod(c, ability)
    else:
        # skill_bonus считает по основной характеристике навыка; подменяем её модификатор.
        kind = CheckKind.SKILL
        bonus = (
            skill_bonus(c, skill) - ability_mod(c, SKILL_ABILITY[skill]) + ability_mod(c, ability)
        )
    return _d20_test(
        c,
        kind,
        ability,
        skill,
        bonus=bonus,
        dc=dc,
        advantage=advantage,
        against=None,
        rng=rng,
        reason=reason,
        hooks=hooks,
    )


def saving_throw(
    c: Character,
    ability: Ability,
    *,
    dc: int,
    advantage: Advantage,
    rng: Rng,
    reason: str,
    against: str | None = None,
    hooks: Sequence[CheckHook] = (),
) -> CheckResult:
    """Спасбросок против Сл.

    `against` — явная метка угрозы («poison», «poisoned»): по ней дварфийская стойкость
    даёт преимущество. Метка надёжнее разбора текста `reason`, который пишет мастер."""
    return _d20_test(
        c,
        CheckKind.SAVE,
        ability,
        None,
        bonus=save_bonus(c, ability),
        dc=dc,
        advantage=advantage,
        against=against,
        rng=rng,
        reason=reason,
        hooks=hooks,
    )


def contest(a: ContestSide, b: ContestSide, *, rng: Rng, reason: str) -> ContestResult:
    """Состязание: каждая сторона бросает к20 с бонусом; ничья — в пользу b."""
    a_roll = roll(f"1d20{a.bonus:+d}", rng=rng, reason=f"{reason}: {a.label}")
    b_roll = roll(f"1d20{b.bonus:+d}", rng=rng, reason=f"{reason}: {b.label}")
    winner = "a" if a_roll.total > b_roll.total else "b"
    return ContestResult(a=a, b=b, a_roll=a_roll, b_roll=b_roll, winner=winner)
