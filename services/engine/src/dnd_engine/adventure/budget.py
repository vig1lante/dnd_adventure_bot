"""Бюджет боёв: какой сложности партии соответствует опыт (XP) монстров боя.

Таблица `XP_BUDGET_PER_CHARACTER` — «XP Budget per Character» правил 2024 (SRD 5.2): на одного
персонажа даётся бюджет трёх сложностей (низкая, умеренная, высокая), бюджет партии — это он,
умноженный на число персонажей. Множителей за число монстров в правилах 2024 нет: опыт боя — просто
сумма опыта его монстров.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from dnd_engine.adventure.schema import Adventure, Encounter
from dnd_engine.content import MonsterDef, SrdContent

# Уровень -> (низкая, умеренная, высокая) на одного персонажа. Только для чтения: бюджет не должен
# меняться во время игры.
XP_BUDGET_PER_CHARACTER: Mapping[int, tuple[int, int, int]] = MappingProxyType(
    {
        1: (50, 75, 100),
        2: (100, 150, 200),
        3: (150, 225, 400),
        4: (250, 375, 500),
        5: (500, 750, 1100),
    }
)

EncounterRating = Literal["trivial", "low", "moderate", "high", "deadly"]


def find_monster(monster_id: str, adventure: Adventure, content: SrdContent) -> MonsterDef | None:
    """Статблок монстра: свой из приключения важнее одноимённого из SRD; нигде нет — `None`."""
    custom = adventure.monsters.get(monster_id)
    return custom if custom is not None else content.monsters.get(monster_id)


def encounter_xp(encounter: Encounter, adventure: Adventure, content: SrdContent) -> int:
    """Опыт боя: сумма опыта всех его монстров. Неизвестный монстр — `ValueError`."""
    total = 0
    for group in encounter.monsters:
        monster = find_monster(group.monster_id, adventure, content)
        if monster is None:
            raise ValueError(f"неизвестный монстр {group.monster_id!r} в бою {encounter.id!r}")
        total += monster.xp * group.count
    return total


def encounter_rating(xp: int, level: int, party_size: int) -> EncounterRating:
    """Сложность боя для партии: наибольшая из сложностей, чей бюджет бой уже набрал.

    Ниже бюджета «низкой» — `trivial`; больше бюджета «высокой» в полтора раза — `deadly`
    (ровно в полтора — ещё `high`). Уровень вне таблицы (1–5) — `ValueError`.
    """
    if level not in XP_BUDGET_PER_CHARACTER:
        first, last = min(XP_BUDGET_PER_CHARACTER), max(XP_BUDGET_PER_CHARACTER)
        raise ValueError(f"бюджет боёв задан для уровней {first}–{last}, получен {level}")
    low, moderate, high = (
        per_character * party_size for per_character in XP_BUDGET_PER_CHARACTER[level]
    )
    if xp * 2 > high * 3:  # «больше high × 1,5» без дробей
        return "deadly"
    if xp >= high:
        return "high"
    if xp >= moderate:
        return "moderate"
    if xp >= low:
        return "low"
    return "trivial"
