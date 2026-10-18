"""Источники случайных чисел движка.

Любой бросок в движке идёт только через переданный `Rng`: в игре это `SystemRng`,
в тестах — `SeededRng` или `ScriptedRng`.
"""

import random
import secrets
from collections import deque
from collections.abc import Iterable
from typing import Protocol


class Rng(Protocol):
    def randint(self, a: int, b: int) -> int:
        """Целое число от a до b включительно."""
        ...


class SystemRng:
    """Криптостойкий генератор операционной системы — только для игры."""

    def __init__(self) -> None:
        self._random = secrets.SystemRandom()

    def randint(self, a: int, b: int) -> int:
        return self._random.randint(a, b)


class SeededRng:
    """Воспроизводимый генератор: один seed — одна и та же последовательность."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def randint(self, a: int, b: int) -> int:
        return self._random.randint(a, b)


class ScriptedRng:
    """Отдаёт заранее заданные значения по очереди — для точных сценариев в тестах."""

    def __init__(self, values: Iterable[int]) -> None:
        self._values = deque(values)

    def randint(self, a: int, b: int) -> int:
        if not self._values:
            raise ValueError("очередь значений ScriptedRng закончилась")
        value = self._values.popleft()
        if not a <= value <= b:
            raise ValueError(f"значение {value} вне диапазона [{a}, {b}]")
        return value
