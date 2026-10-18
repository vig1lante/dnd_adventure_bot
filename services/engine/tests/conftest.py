"""Общие фикстуры тестов движка."""

from collections.abc import Callable
from typing import Any

import pytest

from dnd_engine.abilities import Ability
from dnd_engine.character import Character

NEUTRAL_SCORE = 10  # модификатор 0: тест сам задаёт только те числа, которые проверяет


@pytest.fixture
def make_character() -> Callable[..., Character]:
    """Фабрика персонажа: `make_character(**overrides)`.

    По умолчанию — воин 3-го уровня без владений, снаряжения и заклинаний, все
    характеристики по 10. В `scores` можно передать только нужные характеристики,
    остальные останутся по 10. Остальные поля переопределяются целиком.
    """

    def factory(**overrides: Any) -> Character:
        unknown = overrides.keys() - Character.model_fields.keys()
        if unknown:
            # Опечатка в имени поля молча превратила бы тест в проверку значений по умолчанию.
            raise TypeError(f"у Character нет полей {sorted(unknown)}")
        scores = dict.fromkeys(Ability, NEUTRAL_SCORE) | overrides.pop("scores", {})
        base: dict[str, Any] = {
            "id": "pc-1",
            "name": "Тестовый герой",
            "species": "human",
            "class_id": "fighter",
            "background": "soldier",
            "skill_proficiencies": [],
            "save_proficiencies": [],
            "hp_max": 20,
            "hp": 20,
            "hit_dice_max": 3,
            "hit_dice": 3,
        }
        return Character(**(base | overrides | {"scores": scores}))

    return factory
