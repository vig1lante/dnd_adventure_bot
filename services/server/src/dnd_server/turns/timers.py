"""Отсчёты игр и фоновые задачи хода (план B7.4).

`Scheduler` держит по одному сроку на ключ (ключ — id игры: срок игры один, он же
`games.deadline_at`). Срок — момент по часам планировщика, а не длительность: так тот же `at` лежит
в базе, и после перезапуска сервера отсчёт восстанавливается из неё без пересчёта. Часы
подставляются: в тестах `ManualScheduler` не ждёт, а срабатывает по `advance(seconds)`. Всё, что
считает срок (конец сбора, продление, остаток после паузы), берёт «сейчас» из `scheduler.now()`,
поэтому тест управляет временем целиком.

`TaskSet` держит фоновые задачи разбора мастера: сильные ссылки (иначе цикл событий вправе забыть
задачу), ожидание в тестах (`join`) и остановку вместе с сервером (`shutdown`).
"""

import asyncio
import bisect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from dnd_server.config import Settings
from dnd_server.dm.text import safe_traceback
from dnd_server.games.service import GameError

logger = logging.getLogger(__name__)

Action = Callable[[], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    """Отсчёты по ключам: `schedule` ставит (или заменяет) срок, `cancel` снимает.

    Срок, который уже наступил или прошёл (после перезапуска сервера), срабатывает сразу. Сработав,
    отсчёт сам убирается из учёта до запуска действия: действие смело снимает или ставит срок того
    же ключа (закрытие сбора, продление), не отменяя само себя. Ошибка действия пишется в журнал и
    другие отсчёты не касается. `settings` нужны журналу: значения секретов вырезаются из текста
    ошибки (приложение передаёт свои настройки)."""

    def __init__(
        self, *, clock: Callable[[], datetime] = utc_now, settings: Settings | None = None
    ) -> None:
        self._clock = clock
        self._settings = settings
        self._timers: dict[str, tuple[datetime, asyncio.Task[None]]] = {}

    def now(self) -> datetime:
        return self._clock()

    def schedule(self, key: str, at: datetime, action: Action) -> None:
        self.cancel(key)
        task = asyncio.create_task(self._run(key, at, action))
        self._timers[key] = (at, task)

    def cancel(self, key: str) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer[1].cancel()

    def pending(self) -> dict[str, datetime]:
        """Ожидающие отсчёты: ключ -> срок. Для тестов и диагностики."""
        return {key: at for key, (at, _task) in self._timers.items()}

    async def close(self) -> None:
        """Снимает все отсчёты и дожидается отмены задач (остановка сервера)."""
        tasks = [task for _at, task in self._timers.values()]
        self._timers.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, key: str, at: datetime, action: Action) -> None:
        # Спим остаток и сверяемся с часами снова: цикл событий и стенные часы расходятся, и
        # проснуться чуть раньше срока можно, а пропустить срок нельзя.
        while (left := (at - self.now()).total_seconds()) > 0:
            await asyncio.sleep(left)
        current = self._timers.get(key)
        if current is not None and current[1] is asyncio.current_task():
            del self._timers[key]
        try:
            await action()
        except Exception as exc:
            # Трассировка идёт без текста ошибки pydantic (в нём куски данных игры) и без значений
            # секретов (`safe_traceback`).
            logger.error(
                "Действие по сроку отсчёта %s завершилось ошибкой.\n%s",
                key,
                safe_traceback(exc, self._settings),
            )


class ManualScheduler(Scheduler):
    """Планировщик тестов: время стоит, пока тест не вызовет `advance`.

    Действия не запускаются фоновыми задачами, а выполняются внутри `advance` в порядке сроков (при
    равных сроках — в порядке постановки). Ошибка действия поднимается из `advance`: тест её
    видит, а живой планировщик её только пишет в журнал."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or utc_now()
        super().__init__(clock=lambda: self._now)
        self._queue: list[tuple[datetime, int, str, Action]] = []
        self._sequence = 0

    def schedule(self, key: str, at: datetime, action: Action) -> None:
        self.cancel(key)
        self._sequence += 1
        bisect.insort(self._queue, (at, self._sequence, key, action), key=lambda item: item[:2])

    def cancel(self, key: str) -> None:
        self._queue = [item for item in self._queue if item[2] != key]

    def pending(self) -> dict[str, datetime]:
        return {key: at for at, _number, key, _action in self._queue}

    async def close(self) -> None:
        self._queue.clear()

    async def advance(self, seconds: float) -> None:
        """Переводит время вперёд и выполняет все сроки, что наступили (в том числе поставленные
        самими действиями). Каждое действие видит часы на своём сроке, как в жизни, а не на конце
        отрезка."""
        target = self._now + timedelta(seconds=seconds)
        while self._queue and self._queue[0][0] <= target:
            at, _number, _key, action = self._queue.pop(0)
            self._now = max(self._now, at)
            await action()
        self._now = target


class TaskSet:
    """Фоновые задачи с учётом: пока задача идёт, на неё держится ссылка.

    Отказ игры (`GameError`) для задачи разбора — не сбой: ход не зарезервирован, и отказ уже
    получил тот, кто просил (`turns.flow.start_turn`). Любая другая ошибка пишется в журнал: без
    значений секретов из `settings` (их передаёт приложение) и без значений входа pydantic."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._settings = settings

    def __len__(self) -> int:
        return len(self._tasks)

    def spawn[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()  # читается всегда: иначе Python пожалуется на непрочитанную ошибку
        if error is not None and not isinstance(error, GameError):
            logger.error(
                "Фоновая задача хода завершилась ошибкой.\n%s",
                safe_traceback(error, self._settings),
            )

    async def join(self) -> None:
        """Ждёт, пока кончатся все задачи, в том числе поставленные уже во время ожидания."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Отменяет идущие задачи и ждёт их уборки (ход мастера при отмене пишет себя неудачным)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
