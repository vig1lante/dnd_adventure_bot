"""Разбор записи костей и бросок по ней.

Грамматика: `терм (('+' | '-') терм)*`, где терм — `NdM`, `dM` (N = 1),
`NdMkhK` («оставить K старших») или целое число. Пробелы игнорируются.
Все броски идут через переданный `Rng`; итог считает `roll`, а не вызывающий.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from dnd_engine.rng import Rng

MAX_DICE_COUNT = 100
ALLOWED_SIDES = (2, 3, 4, 6, 8, 10, 12, 20, 100)

# Только ASCII-цифры: \d в Python принимает и цифры других письменностей.
_TERM = re.compile(
    r"(?:(?P<count>[0-9]*)d(?P<sides>[0-9]+)(?:kh(?P<keep>[0-9]+))?|(?P<flat>[0-9]+))"
)


class Advantage(StrEnum):
    NONE = "none"
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"


class DiceSyntaxError(ValueError):
    """Запись костей не соответствует грамматике или допустимым пределам."""


@dataclass(frozen=True)
class DiceTerm:
    count: int  # 1..100
    sides: int  # одно из ALLOWED_SIDES
    keep_highest: int | None = None
    sign: int = 1  # +1 или -1


@dataclass(frozen=True)
class DiceExpr:
    terms: tuple[DiceTerm, ...]
    modifier: int


class RollResult(BaseModel):
    expr: str
    rolls: list[int]  # все выпавшие кости по порядку
    kept: list[int]  # кости, пошедшие в сумму (со знаком терма)
    modifier: int
    total: int
    natural: int | None  # значение к20, если выражение — ровно один добавляемый 1d20
    advantage: Advantage
    reason: str


def _to_int(digits: str) -> int:
    try:
        return int(digits)
    except ValueError as exc:  # Python не разбирает числа длиннее 4300 цифр
        raise DiceSyntaxError("слишком длинное число в записи костей") from exc


def _parse_dice_term(match: re.Match[str], sign: int, expr: str) -> DiceTerm:
    count = _to_int(match["count"]) if match["count"] else 1
    sides = _to_int(match["sides"])
    keep = _to_int(match["keep"]) if match["keep"] else None
    if not 1 <= count <= MAX_DICE_COUNT:
        raise DiceSyntaxError(f"число костей должно быть от 1 до {MAX_DICE_COUNT}: {expr!r}")
    if sides not in ALLOWED_SIDES:
        raise DiceSyntaxError(f"недопустимое число граней {sides} в {expr!r}")
    if keep is not None and not 1 <= keep <= count:
        raise DiceSyntaxError(f"kh должно быть от 1 до числа костей ({count}): {expr!r}")
    return DiceTerm(count=count, sides=sides, keep_highest=keep, sign=sign)


def parse(expr: str) -> DiceExpr:
    # Операторы остаются в списке: [терм, оператор, терм, ...].
    pieces = re.split(r"([+-])", re.sub(r"\s+", "", expr))
    terms: list[DiceTerm] = []
    modifier = 0
    sign = 1
    for index, piece in enumerate(pieces):
        if index % 2 == 1:
            sign = 1 if piece == "+" else -1
            continue
        match = _TERM.fullmatch(piece)
        if match is None:
            raise DiceSyntaxError(f"не удалось разобрать запись костей {expr!r}")
        if match["flat"] is not None:
            modifier += sign * _to_int(match["flat"])
        else:
            terms.append(_parse_dice_term(match, sign, expr))
    return DiceExpr(terms=tuple(terms), modifier=modifier)


def _is_single_d20(expr: DiceExpr) -> bool:
    """Ровно один добавляемый 1d20; вычитаемая к20 (`5-1d20`) не считается: у неё нет `natural`."""
    if len(expr.terms) != 1:
        return False
    term = expr.terms[0]
    return (term.count, term.sides, term.sign) == (1, 20, 1)


def _highest(values: list[int], limit: int | None) -> list[int]:
    """Оставляет `limit` старших костей, сохраняя порядок броска; при равных — более ранние."""
    if limit is None:
        return values
    best = set(sorted(range(len(values)), key=lambda i: values[i], reverse=True)[:limit])
    return [value for i, value in enumerate(values) if i in best]


def roll(
    expr: str,
    *,
    rng: Rng,
    advantage: Advantage = Advantage.NONE,
    reason: str,
) -> RollResult:
    advantage = Advantage(advantage)
    parsed = parse(expr)
    single_d20 = _is_single_d20(parsed)
    if advantage != Advantage.NONE and not single_d20:
        raise ValueError(
            "преимущество и помеха допустимы только для выражения с одним добавляемым термом 1d20"
        )

    rolls: list[int] = []
    kept: list[int] = []
    if advantage == Advantage.NONE:
        for term in parsed.terms:
            term_rolls = [rng.randint(1, term.sides) for _ in range(term.count)]
            rolls.extend(term_rolls)
            kept.extend(term.sign * value for value in _highest(term_rolls, term.keep_highest))
    else:
        rolls = [rng.randint(1, 20), rng.randint(1, 20)]
        kept = [max(rolls) if advantage == Advantage.ADVANTAGE else min(rolls)]

    return RollResult(
        expr=expr,
        rolls=rolls,
        kept=kept,
        modifier=parsed.modifier,
        total=sum(kept) + parsed.modifier,
        natural=kept[0] if single_d20 else None,
        advantage=advantage,
        reason=reason,
    )
