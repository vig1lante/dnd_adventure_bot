"""Хранилище предпросмотров случайного героя.

Хранилище живёт в памяти процесса, как `GameLocks` и `ModelCalls`: один живой предпросмотр на
пару «игра — игрок», расход при «Беру», срок жизни и предел частоты. Часы подставляются, поэтому
тесты ничего не ждут. Подробные сценарии по HTTP — в `test_characters.py`.
"""

import re

import pytest

from dnd_engine.content import load_srd
from dnd_engine.creation import random_character
from dnd_engine.rng import SeededRng
from dnd_server.characters.previews import (
    MAX_PREVIEWS,
    PREVIEW_TTL_S,
    PREVIEW_WINDOW_S,
    RandomPreviews,
)
from dnd_server.games.service import Conflict, TooManyRequests

PREVIEW_ID_FORMAT = re.compile(r"rp_[0-9a-f]{16}")
GAME, OTHER_GAME = "game-1", "game-2"
USER, FRIEND = "user-1", "user-2"


class Clock:
    """Часы теста: время стоит, пока тест его не сдвинет."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def hero(seed: int = 1, character_id: str = "pc_0123456789abcdef"):
    return random_character(
        SeededRng(seed), load_srd(), taken_classes=set(), character_id=character_id
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def previews(clock: Clock) -> RandomPreviews:
    return RandomPreviews(clock=clock)


def test_the_limits_are_the_ones_chosen_for_the_slice():
    # Не более 20 бросков в минуту на игрока и игру; предпросмотр живёт полчаса.
    assert (MAX_PREVIEWS, PREVIEW_WINDOW_S, PREVIEW_TTL_S) == (20, 60, 1800)


def test_a_preview_gets_a_fixed_format_id_and_keeps_the_hero(previews):
    first = previews.put(GAME, USER, hero(1))
    second = previews.put(GAME, FRIEND, hero(2))

    assert PREVIEW_ID_FORMAT.fullmatch(first.id) and PREVIEW_ID_FORMAT.fullmatch(second.id)
    assert first.id != second.id
    assert first.character == hero(1)
    assert previews.current(GAME, USER) == first


def test_there_is_one_live_preview_per_player_and_a_new_one_cancels_the_old(previews):
    old = previews.put(GAME, USER, hero(1))
    new = previews.put(GAME, USER, hero(2))

    assert len(previews) == 1
    assert previews.current(GAME, USER) == new
    with pytest.raises(Conflict):
        previews.take(GAME, USER, old.id)
    assert previews.take(GAME, USER, new.id) == hero(2)


def test_taking_uses_the_preview_up(previews):
    shown = previews.put(GAME, USER, hero(1))

    assert previews.take(GAME, USER, shown.id) == hero(1)
    assert previews.current(GAME, USER) is None
    with pytest.raises(Conflict):
        previews.take(GAME, USER, shown.id)


def test_a_preview_belongs_to_its_player_and_its_game(previews):
    shown = previews.put(GAME, USER, hero(1))

    # Чужой игрок той же игры и тот же игрок в другой игре получают тот же отказ, что и за
    # неизвестный id: id не выдаёт, чей он.
    refusals = []
    for game_id, user_id, preview_id in (
        (GAME, FRIEND, shown.id),
        (OTHER_GAME, USER, shown.id),
        (GAME, USER, "rp_" + "0" * 16),
    ):
        with pytest.raises(Conflict) as refused:
            previews.take(game_id, user_id, preview_id)
        refusals.append(refused.value.message)
    assert len(set(refusals)) == 1 and re.search("[а-яё]", refusals[0], re.IGNORECASE)
    # Чужие попытки предпросмотр не расходуют.
    assert previews.take(GAME, USER, shown.id) == hero(1)


def test_every_player_and_game_has_their_own_preview(previews):
    mine = previews.put(GAME, USER, hero(1))
    friends = previews.put(GAME, FRIEND, hero(2))
    elsewhere = previews.put(OTHER_GAME, USER, hero(3))

    assert len(previews) == 3
    assert previews.take(GAME, FRIEND, friends.id) == hero(2)
    assert previews.take(OTHER_GAME, USER, elsewhere.id) == hero(3)
    assert previews.take(GAME, USER, mine.id) == hero(1)


def test_a_preview_expires_and_the_store_forgets_it(previews, clock):
    stale = previews.put(GAME, USER, hero(1))
    clock.now += PREVIEW_TTL_S - 1
    assert previews.current(GAME, USER) == stale
    clock.now += 1

    assert previews.current(GAME, USER) is None
    with pytest.raises(Conflict):
        previews.take(GAME, USER, stale.id)


def test_expired_previews_do_not_pile_up_in_memory(previews, clock):
    for number in range(50):
        previews.put(GAME, f"user-{number}", hero(number))
    clock.now += PREVIEW_TTL_S
    previews.put(GAME, "user-new", hero(99))

    assert len(previews) == 1  # новый предпросмотр вытеснил просроченные


def test_a_replaced_preview_gets_a_fresh_lifetime(previews, clock):
    previews.put(GAME, USER, hero(1))
    clock.now += PREVIEW_TTL_S - 1
    fresh = previews.put(GAME, USER, hero(2))
    clock.now += PREVIEW_TTL_S - 1

    assert previews.current(GAME, USER) == fresh


def test_rolls_are_limited_per_player_and_game_in_a_sliding_window(previews, clock):
    for _ in range(MAX_PREVIEWS):
        previews.charge(GAME, USER)

    with pytest.raises(TooManyRequests) as refused:
        previews.charge(GAME, USER)
    assert re.search("[а-яё]", refused.value.message, re.IGNORECASE)
    # Предел личный и поигровой: друг и тот же игрок в другой игре бросают свободно.
    previews.charge(GAME, FRIEND)
    previews.charge(OTHER_GAME, USER)
    # Окно скользящее: пока самая старая попытка не вышла из него, отказ остаётся.
    clock.now += PREVIEW_WINDOW_S - 1
    with pytest.raises(TooManyRequests):
        previews.charge(GAME, USER)
    clock.now += 1
    previews.charge(GAME, USER)


def test_the_refusal_names_how_long_to_wait(previews, clock):
    for _ in range(MAX_PREVIEWS):
        previews.charge(GAME, USER)
    clock.now += 45

    with pytest.raises(TooManyRequests) as refused:
        previews.charge(GAME, USER)

    assert "15 с" in refused.value.message


def test_the_wait_counts_from_the_oldest_roll_of_the_window(previews, clock):
    # Первый бросок был раньше остальных: ждать нужно, пока выйдет из окна именно он.
    previews.charge(GAME, USER)
    clock.now += 10
    for _ in range(MAX_PREVIEWS - 1):
        previews.charge(GAME, USER)
    clock.now += 20  # с первого броска прошло 30 с

    with pytest.raises(TooManyRequests) as refused:
        previews.charge(GAME, USER)

    assert f"{PREVIEW_WINDOW_S - 30} с" in refused.value.message


def test_a_stored_preview_cannot_be_changed_from_outside(previews):
    # Хранилище отдаёт героя и срок как есть; подменить id или срок задним числом нельзя.
    shown = previews.put(GAME, USER, hero(1))

    with pytest.raises(AttributeError):
        shown.id = "rp_0000000000000000"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        shown.expires_at = 0.0  # type: ignore[misc]


def test_refused_rolls_do_not_extend_the_wait(previews, clock):
    for _ in range(MAX_PREVIEWS):
        previews.charge(GAME, USER)
    for _ in range(100):
        with pytest.raises(TooManyRequests):
            previews.charge(GAME, USER)
    clock.now += PREVIEW_WINDOW_S

    previews.charge(GAME, USER)  # отказы в окно не записываются


def test_idle_players_leave_no_counters_behind(previews, clock):
    for number in range(30):
        previews.charge(GAME, f"user-{number}")
    clock.now += PREVIEW_WINDOW_S

    previews.charge(GAME, "user-new")

    assert previews.counted() == 1
