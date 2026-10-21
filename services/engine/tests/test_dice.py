from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from dnd_engine import dice as dice_module
from dnd_engine.dice import (
    Advantage,
    DiceSyntaxError,
    DiceTerm,
    RollResult,
    parse,
    roll,
)
from dnd_engine.rng import ScriptedRng, SeededRng, SystemRng

# --- разбор записи ---


def test_parse_compound_expression():
    expr = parse("1d8 + 1d6 + 2")
    assert [(t.count, t.sides) for t in expr.terms] == [(1, 8), (1, 6)]
    assert expr.modifier == 2


def test_parse_short_form_means_one_die():
    term = parse("d20").terms[0]
    assert (term.count, term.sides, term.keep_highest, term.sign) == (1, 20, None, 1)


def test_parse_keep_highest_and_signs():
    expr = parse("4d6kh3-1d4")
    assert (expr.terms[0].count, expr.terms[0].keep_highest, expr.terms[0].sign) == (4, 3, 1)
    assert (expr.terms[1].count, expr.terms[1].sides, expr.terms[1].sign) == (1, 4, -1)


def test_parse_ignores_whitespace():
    assert parse(" 2d6 + 3 ") == parse("2d6+3")


def test_parse_folds_flat_numbers_into_modifier():
    expr = parse("1d4+2+3-1")
    assert len(expr.terms) == 1
    assert expr.modifier == 4


def test_parse_flat_number_without_dice():
    expr = parse("5")
    assert expr.terms == ()
    assert expr.modifier == 5


def test_parse_accepts_keep_all_dice():
    assert parse("2d6kh2").terms[0].keep_highest == 2


@pytest.mark.parametrize("sides", [2, 3, 4, 6, 8, 10, 12, 20, 100])
def test_parse_accepts_allowed_sides(sides):
    assert parse(f"1d{sides}").terms[0].sides == sides


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "d7",
        "1d1",
        "1d0",
        "0d6",
        "101d6",
        "2d6+",
        "2d6++3",
        "+2d6",
        "-1d6",
        "abc",
        "2d",
        "d",
        "1d6x",
        "1d6 1d4",
        "1.5d6",
        "1D6",
        "\uff11d6",  # цифра полной ширины не считается цифрой записи
        "1d20kh2",
        "2d6kh3",
        "2d6kh0",
        "2d6kh",
        "kh3",
    ],
)
def test_invalid_expressions(bad):
    with pytest.raises(DiceSyntaxError):
        parse(bad)


def test_too_long_number_is_syntax_error_not_crash():
    with pytest.raises(DiceSyntaxError):
        parse("1d6+" + "9" * 5000)


def test_syntax_error_is_value_error():
    assert issubclass(DiceSyntaxError, ValueError)


# --- броски ---


def test_roll_sums_dice_and_modifier():
    result = roll("2d6+3", rng=ScriptedRng([4, 5]), reason="урон")
    assert result.rolls == [4, 5]
    assert result.total == 12
    assert result.natural is None


def test_roll_keeps_expression_and_reason():
    result = roll("2d6+3", rng=ScriptedRng([4, 5]), reason="урон мечом")
    assert result.expr == "2d6+3"
    assert result.reason == "урон мечом"
    assert result.modifier == 3
    assert result.kept == [4, 5]
    assert result.advantage == Advantage.NONE


def test_roll_compound_expression_keeps_order_of_rolls():
    result = roll("1d8 + 1d6 + 2", rng=ScriptedRng([7, 3]), reason="урон")
    assert result.rolls == [7, 3]
    assert result.total == 12


def test_roll_flat_number_does_not_touch_rng():
    result = roll("5", rng=ScriptedRng([]), reason="фиксированный урон")
    assert result.rolls == []
    assert result.total == 5
    assert result.natural is None


def test_roll_consumes_exactly_one_value_per_die():
    rng = ScriptedRng([4, 5])
    roll("2d6", rng=rng, reason="x")
    with pytest.raises(ValueError):
        rng.randint(1, 6)


def test_keep_highest():
    result = roll("4d6kh3", rng=ScriptedRng([1, 6, 3, 5]), reason="характеристика")
    assert sorted(result.kept) == [3, 5, 6]
    assert result.total == 14


def test_keep_highest_leaves_kept_in_roll_order_and_all_in_rolls():
    result = roll("4d6kh3", rng=ScriptedRng([4, 1, 6, 3]), reason="x")
    assert result.rolls == [4, 1, 6, 3]
    assert result.kept == [4, 6, 3]


def test_keep_highest_with_ties_drops_exactly_one_die():
    result = roll("4d6kh3", rng=ScriptedRng([2, 2, 2, 5]), reason="x")
    assert result.kept == [2, 2, 5]
    assert result.total == 9


def test_negative_modifier_and_term():
    assert roll("1d4-1", rng=ScriptedRng([1]), reason="x").total == 0
    assert roll("1d8-1d4", rng=ScriptedRng([5, 2]), reason="x").total == 3


def test_subtracted_dice_are_negative_in_kept():
    result = roll("1d8-1d4", rng=ScriptedRng([5, 2]), reason="x")
    assert result.rolls == [5, 2]
    assert result.kept == [5, -2]


def test_subtracted_term_supports_keep_highest():
    result = roll("1d20-4d6kh3", rng=ScriptedRng([10, 1, 6, 3, 5]), reason="x")
    assert result.total == 10 - 14


def test_natural_is_set_for_single_d20_only():
    single = roll("d20+5", rng=ScriptedRng([14]), reason="проверка")
    assert single.natural == 14
    assert single.total == 19
    assert roll("2d20", rng=ScriptedRng([14, 3]), reason="x").natural is None
    assert roll("d20+1d4", rng=ScriptedRng([14, 3]), reason="x").natural is None
    assert roll("d12", rng=ScriptedRng([9]), reason="x").natural is None


@pytest.mark.parametrize(
    ("advantage", "expected"),
    [(Advantage.ADVANTAGE, 17), (Advantage.DISADVANTAGE, 3)],
)
def test_advantage_keeps_one_d20(advantage, expected):
    result = roll("d20+2", rng=ScriptedRng([3, 17]), advantage=advantage, reason="атака")
    assert result.rolls == [3, 17]
    assert result.kept == [expected]
    assert result.natural == expected
    assert result.total == expected + 2
    assert result.advantage == advantage


def test_advantage_takes_the_better_die_regardless_of_order():
    result = roll("d20", rng=ScriptedRng([17, 3]), advantage=Advantage.ADVANTAGE, reason="x")
    assert result.natural == 17
    result = roll("d20", rng=ScriptedRng([17, 3]), advantage=Advantage.DISADVANTAGE, reason="x")
    assert result.natural == 3


def test_advantage_consumes_exactly_two_dice():
    rng = ScriptedRng([3, 17])
    roll("d20", rng=rng, advantage=Advantage.ADVANTAGE, reason="x")
    with pytest.raises(ValueError):
        rng.randint(1, 20)


def test_advantage_none_rolls_a_single_d20():
    result = roll("d20", rng=ScriptedRng([12]), advantage=Advantage.NONE, reason="x")
    assert result.rolls == [12]


@pytest.mark.parametrize(
    "expr",
    [
        "2d6",
        "2d20",
        "d20+1d4",
        "d12",
        "5",
        "d20+d20",
        "1d20-1d20",
        "2d20kh1",
        # вычитаемая к20 — не «ровно один 1d20», знак терма важен
        "5-1d20",
        "10-d20",
        "3-1d20+2",
    ],
)
@pytest.mark.parametrize("advantage", [Advantage.ADVANTAGE, Advantage.DISADVANTAGE])
def test_advantage_requires_single_d20(expr, advantage):
    with pytest.raises(ValueError):
        roll(expr, rng=SeededRng(1), advantage=advantage, reason="x")


@pytest.mark.parametrize("expr", ["5-1d20", "10-d20", "d20+d20", "1d20-1d20"])
@pytest.mark.parametrize("advantage", [Advantage.ADVANTAGE, Advantage.DISADVANTAGE])
def test_advantage_refusal_happens_before_any_die_is_rolled(expr, advantage):
    rng = ScriptedRng([4, 5])

    with pytest.raises(ValueError):
        roll(expr, rng=rng, advantage=advantage, reason="x")

    assert (rng.randint(1, 20), rng.randint(1, 20)) == (4, 5)


@pytest.mark.parametrize(
    ("expr", "values", "total"),
    [
        ("5-1d20", [3], 2),
        ("10-d20", [14], -4),
        ("3-1d20+2", [3], 2),
        ("0-1d20", [8], -8),
    ],
)
def test_subtracted_d20_has_no_natural_and_is_subtracted(expr, values, total):
    result = roll(expr, rng=ScriptedRng(values), reason="x")

    assert result.natural is None
    assert result.kept == [-values[0]]
    assert result.total == total


@pytest.mark.parametrize(
    ("expr", "values"),
    [
        ("2d20", [3, 17]),
        ("d20+d20", [3, 17]),
        ("1d20-1d20", [3, 17]),
        ("2d20kh1", [3, 17]),
        ("1d4-1d20", [3, 17]),
        ("5-1d20", [3]),
        ("d12", [9]),
        ("5-3", []),
    ],
)
def test_natural_is_none_unless_exactly_one_added_d20(expr, values):
    assert roll(expr, rng=ScriptedRng(values), reason="x").natural is None


@pytest.mark.parametrize("expr", ["1d20", "5+1d20", "1d20kh1", "1d20+5-2", "d20 + 1"])
def test_added_single_d20_has_natural_and_accepts_advantage(expr):
    plain = roll(expr, rng=ScriptedRng([14]), reason="x")
    best = roll(expr, rng=ScriptedRng([3, 17]), advantage=Advantage.ADVANTAGE, reason="x")
    worst = roll(expr, rng=ScriptedRng([3, 17]), advantage=Advantage.DISADVANTAGE, reason="x")

    assert plain.natural == 14
    assert (best.natural, worst.natural) == (17, 3)
    # Итог — выпавшая к20 плюс целые числа записи; знак целых сохраняется.
    assert best.total == 17 + best.modifier
    assert worst.total == 3 + worst.modifier


@pytest.mark.parametrize(
    "expr",
    ["d20", "5+1d20", "1d20kh1", "1d20+5-2", "5-1d20", "10-d20", "1d20-1d20", "2d20", "d12", "5"],
)
def test_natural_is_always_a_rolled_d20_face_or_none(expr):
    for seed in range(30):
        result = roll(expr, rng=SeededRng(seed), reason="x")

        assert result.natural is None or (1 <= result.natural <= 20), f"seed {seed}"
        assert result.natural is None or result.natural in result.rolls, f"seed {seed}"


def test_advantage_error_does_not_consume_rng():
    rng = ScriptedRng([4, 5])
    with pytest.raises(ValueError):
        roll("2d6", rng=rng, advantage=Advantage.ADVANTAGE, reason="x")
    assert roll("2d6", rng=rng, reason="x").rolls == [4, 5]


def test_unknown_advantage_is_rejected():
    with pytest.raises(ValueError):
        roll("d20", rng=SeededRng(1), advantage="strong", reason="x")  # type: ignore[arg-type]


def test_syntax_error_does_not_consume_rng():
    rng = ScriptedRng([4])
    with pytest.raises(DiceSyntaxError):
        roll("1d6+", rng=rng, reason="x")
    assert roll("1d6", rng=rng, reason="x").total == 4


def test_roll_result_round_trips_through_json():
    result = roll("d20+2", rng=ScriptedRng([3, 17]), advantage=Advantage.ADVANTAGE, reason="x")
    dumped = result.model_dump(mode="json")
    assert dumped["advantage"] == "advantage"
    assert RollResult.model_validate(dumped) == result


# --- генераторы случайных чисел ---


def test_scripted_rng_rejects_out_of_range_value():
    with pytest.raises(ValueError):
        roll("1d6", rng=ScriptedRng([7]), reason="x")
    with pytest.raises(ValueError):
        roll("1d6", rng=ScriptedRng([0]), reason="x")


def test_scripted_rng_accepts_range_borders():
    assert ScriptedRng([1, 6]).randint(1, 6) == 1
    rng = ScriptedRng([1, 6])
    rng.randint(1, 6)
    assert rng.randint(1, 6) == 6


def test_scripted_rng_raises_when_queue_is_exhausted():
    rng = ScriptedRng([3])
    assert rng.randint(1, 6) == 3
    with pytest.raises(ValueError):
        rng.randint(1, 6)


def test_scripted_rng_with_empty_queue_raises_immediately():
    with pytest.raises(ValueError):
        ScriptedRng([]).randint(1, 6)


def test_seeded_rng_is_reproducible():
    first = [roll("d20", rng=SeededRng(42), reason="x").total for _ in range(1)]
    second = [roll("d20", rng=SeededRng(42), reason="x").total for _ in range(1)]
    assert first == second


def test_seeded_rng_gives_same_sequence_for_same_seed():
    def sequence(seed):
        rng = SeededRng(seed)
        return [roll("2d6+1", rng=rng, reason="x").rolls for _ in range(20)]

    assert sequence(7) == sequence(7)
    assert sequence(7) != sequence(8)


@pytest.mark.parametrize("sides", [2, 3, 4, 6, 8, 10, 12, 20, 100])
def test_seeded_rng_stays_within_die_range(sides):
    rng = SeededRng(sides)
    rolls = [roll(f"1d{sides}", rng=rng, reason="x").total for _ in range(200)]
    assert all(1 <= value <= sides for value in rolls)


def test_system_rng_stays_within_range():
    # Конкретные значения случайны, поэтому проверяем только границы диапазона.
    rng = SystemRng()
    assert all(1 <= rng.randint(1, 6) <= 6 for _ in range(200))
    assert rng.randint(4, 4) == 4


def test_system_rng_can_be_used_for_roll():
    result = roll("2d6+3", rng=SystemRng(), reason="x")
    assert 5 <= result.total <= 15
    assert len(result.rolls) == 2


# --- свойства броска на разных записях и seed ---


class SpyRng:
    """Запоминает, какие кости запрашивались у генератора (нижняя и верхняя грань)."""

    def __init__(self, seed: int) -> None:
        self._inner = SeededRng(seed)
        self.requests: list[tuple[int, int]] = []

    def randint(self, a: int, b: int) -> int:
        self.requests.append((a, b))
        return self._inner.randint(a, b)


# (запись, грани запрошенных костей по порядку, сколько костей идёт в сумму)
ROLL_CASES = [
    ("d20", [20], 1),
    ("2d6+3", [6, 6], 2),
    ("1d8+1d6+2", [8, 6], 2),
    ("4d6kh3", [6, 6, 6, 6], 3),
    ("1d8-1d4", [8, 4], 2),
    ("1d20-4d6kh3+5", [20, 6, 6, 6, 6], 4),
    ("3d4kh1-10", [4, 4, 4], 1),
    ("100d100kh7", [100] * 100, 7),
    ("100d6", [6] * 100, 100),
    ("5", [], 0),
]


@pytest.mark.parametrize(("expr", "sides", "kept_count"), ROLL_CASES)
def test_roll_asks_the_rng_only_for_its_dice_and_adds_them_up_honestly(expr, sides, kept_count):
    for seed in range(30):
        rng = SpyRng(seed)

        result = roll(expr, rng=rng, reason="свойства")

        assert rng.requests == [(1, s) for s in sides], f"seed {seed}"
        assert len(result.rolls) == len(sides), f"seed {seed}"
        assert all(1 <= value <= s for value, s in zip(result.rolls, sides, strict=True))
        assert len(result.kept) == kept_count, f"seed {seed}"
        assert result.total == sum(result.kept) + result.modifier, f"seed {seed}"
        # В счёт идут только выпавшие кости (со знаком терма), других значений нет.
        assert not Counter(abs(v) for v in result.kept) - Counter(result.rolls), f"seed {seed}"


def test_total_can_be_negative_when_subtracted_dice_outweigh_the_rest():
    result = roll("1d4-1d20-3", rng=ScriptedRng([1, 20]), reason="x")

    assert result.total == 1 - 20 - 3
    assert result.kept == [1, -20]


def test_roll_keeps_exactly_the_requested_number_of_the_highest_dice_out_of_a_hundred():
    values = list(range(1, 101))  # 1d100 принимает любое значение от 1 до 100
    result = roll("100d100kh3", rng=ScriptedRng(values), reason="x")

    assert sorted(result.kept) == [98, 99, 100]
    assert result.total == 98 + 99 + 100
    assert result.rolls == values


@pytest.mark.parametrize("spaced", ["2d6+3", "\t2d6 +\n3 ", " 2 d 6 + 3", "2d6 + 3"])
def test_parse_ignores_any_whitespace(spaced):
    assert parse(spaced) == parse("2d6+3")


def test_parsed_expression_cannot_be_changed():
    expr = parse("2d6+3")

    with pytest.raises(FrozenInstanceError):
        expr.modifier = 100  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        expr.terms[0].sides = 20  # type: ignore[misc]


@pytest.mark.parametrize("advantage", [Advantage.ADVANTAGE, Advantage.DISADVANTAGE])
def test_advantage_asks_the_rng_for_exactly_two_d20(advantage):
    rng = SpyRng(5)

    result = roll("d20+3", rng=rng, advantage=advantage, reason="x")

    assert rng.requests == [(1, 20), (1, 20)]
    assert all(1 <= value <= 20 for value in result.rolls)


def test_dice_term_defaults_to_adding_all_its_dice():
    term = DiceTerm(count=2, sides=6)

    assert (term.keep_highest, term.sign) == (None, 1)


def test_advantage_values_are_stable_strings():
    assert [mode.value for mode in Advantage] == ["none", "advantage", "disadvantage"]


def test_unknown_advantage_is_rejected_before_rolling():
    rng = ScriptedRng([14])

    with pytest.raises(ValueError):
        roll("d20", rng=rng, advantage="strong", reason="x")  # type: ignore[arg-type]

    assert rng.randint(1, 20) == 14


@pytest.mark.parametrize(
    ("text", "expected", "mode"),
    [("advantage", 17, Advantage.ADVANTAGE), ("disadvantage", 3, Advantage.DISADVANTAGE)],
)
def test_advantage_given_as_plain_string_becomes_the_enum_value(text, expected, mode):
    rng = ScriptedRng([3, 17])

    result = roll("d20", rng=rng, advantage=text, reason="x")  # type: ignore[arg-type]

    assert (result.natural, result.advantage) == (expected, mode)


# --- пределы записи костей, которую прислала модель (heal/damage, B4.7) ---


@pytest.mark.parametrize(
    "good",
    ["2d6", "1d8+3", " 4d6 + 2 ", "20d6", "1d10-1d4", "1d4+30", "1d4-30", "4d6kh3", "d20"],
)
def test_model_dice_accepts_ordinary_dice_within_limits(good):
    assert dice_module.parse_model_dice(good) == parse(good)


def test_model_dice_check_does_not_change_the_ordinary_parser():
    # Записи SRD (фиксированный урон, сотня костей в тестах) по-прежнему разбираются `parse`.
    assert parse("5").terms == ()
    assert len(parse("100d6").terms) == 1


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("5", "кост"),  # ни одной кости: фиксированное число модель получила бы даром
        ("-3", "разобрать"),
        ("", "разобрать"),
        ("1d4" + "+1" * 40, "длин"),  # длиннее предела записи
        ("1d4+1d4+1d4+1d4+1d4", "терм"),  # слишком много термов
        ("21d4", "костей"),  # слишком много костей
        ("10d6+11d6", "костей"),  # общее число костей, а не по одному терму
        ("100d100", "костей"),
        ("1d4+31", "модификатор"),
        ("1d4-31", "модификатор"),
        ("1d4+20+20", "модификатор"),  # плоские числа складываются в модификатор
        ("1d7", "граней"),
    ],
)
def test_model_dice_rejects_what_exceeds_the_limits_with_a_syntax_error(bad, reason):
    with pytest.raises(DiceSyntaxError, match=reason):
        dice_module.parse_model_dice(bad)


def test_model_dice_rejects_a_huge_number_by_length_before_parsing_it():
    with pytest.raises(DiceSyntaxError, match="длин"):
        dice_module.parse_model_dice("1d4+" + "9" * 5000)


def test_model_dice_limits_are_the_documented_constants():
    assert (
        dice_module.MODEL_DICE_MAX_CHARS,
        dice_module.MODEL_DICE_MAX_TERMS,
        dice_module.MODEL_DICE_MAX_DICE,
        dice_module.MODEL_DICE_MAX_MODIFIER,
    ) == (40, 4, 20, 30)
