"""Предпросмотр случайного героя (решение владельца).

«Бросить судьбу», «Ещё раз», «Беру».

Игрок нажимает «Бросить судьбу», сервер бросает героя (`random_character` на `SystemRng`) и
запоминает его здесь, ничего не записывая в базу и не зовя нейронку. «Ещё раз» заменяет героя
новым. «Беру» (`mode: random` с `preview_id`) создаёт персонажа ровно из запомненного героя: числа
клиент не присылает и не пересчитывает, а сервер не бросает заново. Описание героя нейронка пишет
уже после «Беру», как в прежнем случайном режиме.

Хранилище живёт в памяти процесса, как `GameLocks`, `ModelCalls` и `JoinAttempts` (сервер —
один процесс). В базу предпросмотр не пишется нарочно: это временный выбор на минуты, таблица
потребовала бы миграции, а потеря предпросмотра при перезапуске сервера стоит игроку одного
нажатия «Ещё раз». Правила хранилища:

- на пару «игра — игрок» один живой предпросмотр: новый отменяет прежний;
- «Беру» расходует предпросмотр (второй раз тот же id не сработает);
- предпросмотр живёт `PREVIEW_TTL_S` секунд и вытесняется при следующей записи, так что память не
  растёт: ключи берутся только из проверенного участия в лобби (проверку делает вызывающий);
- частота бросков ограничена скользящим окном (`MAX_PREVIEWS` за `PREVIEW_WINDOW_S` секунд на
  игрока и игру): нейронка тут не вызывается, но запрос тратит процессор и ленту событий.

Чужой, устаревший, заменённый и неизвестный id получают один и тот же отказ (`Conflict`): по
ответу не определить, существует ли id у другого игрока.
"""

import math
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from dnd_engine.character import Character
from dnd_server.games.service import Conflict, TooManyRequests

# Не больше 20 бросков в минуту на игрока в игре: живой человек смотрит героя дольше трёх секунд,
# а перебор ради «идеального» героя упирается в предел и ничего не стоит серверу.
MAX_PREVIEWS = 20
PREVIEW_WINDOW_S = 60
# Полчаса: дольше лобби обычно не ждут, а забытый предпросмотр не должен жить вечно.
PREVIEW_TTL_S = 1800

# Формат id задаёт сервер; схема тела запроса проверяет его до обращения к хранилищу.
PREVIEW_ID_PATTERN = r"^rp_[0-9a-f]{16}$"

STALE_PREVIEW = (
    "Этот герой больше не доступен: его заменил новый бросок, он уже взят или устарел. "
    "Бросьте судьбу ещё раз."
)

type Key = tuple[str, str]  # (id игры, id игрока)


def new_preview_id() -> str:
    """Id предпросмотра: «rp_» и 16 шестнадцатеричных знаков, выдаёт только сервер.

    Префикс отличает его от id персонажей («pc_») и пользователей: перепутать их нельзя."""
    return f"rp_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class Preview:
    id: str
    character: Character
    expires_at: float


class RandomPreviews:
    """Живые предпросмотры случайных героев и учёт частоты бросков; всё в памяти процесса.

    Методы синхронные и без ожидания внутри, поэтому проверка и запись в одном вызове не делятся
    между запросами. Часы подставляются в тестах."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Порядок вставки равен порядку создания (замена переставляет запись в конец), поэтому
        # просроченные записи лежат в начале.
        self._live: dict[Key, Preview] = {}
        self._rolls: dict[Key, deque[float]] = {}

    def __len__(self) -> int:
        return len(self._live)

    def counted(self) -> int:
        """Сколько пар «игра — игрок» сейчас на учёте частоты (нужно тестам памяти)."""
        return len(self._rolls)

    def charge(self, game_id: str, user_id: str) -> None:
        """Учитывает бросок или отказывает (`TooManyRequests`), если окно уже заполнено.

        Отказанный бросок в окно не записывается: упорство ожидание не продлевает."""
        now = self._clock()
        self._forget_old_rolls(now)
        rolls = self._rolls.setdefault((game_id, user_id), deque())
        if len(rolls) >= MAX_PREVIEWS:
            wait = math.ceil(rolls[0] + PREVIEW_WINDOW_S - now)
            raise TooManyRequests(
                f"Слишком много бросков подряд: подождите {wait} с и бросьте снова."
            )
        rolls.append(now)

    def put(self, game_id: str, user_id: str, character: Character) -> Preview:
        """Запоминает героя как единственный живой предпросмотр игрока; прежний отменяется."""
        now = self._clock()
        self._forget_expired(now)
        key = (game_id, user_id)
        self._live.pop(key, None)  # замена уходит в конец порядка вставки
        preview = Preview(new_preview_id(), character, now + PREVIEW_TTL_S)
        self._live[key] = preview
        return preview

    def current(self, game_id: str, user_id: str) -> Preview | None:
        """Живой предпросмотр игрока; просроченный забывается."""
        key = (game_id, user_id)
        preview = self._live.get(key)
        if preview is not None and preview.expires_at <= self._clock():
            del self._live[key]
            return None
        return preview

    def take(self, game_id: str, user_id: str, preview_id: str) -> Character:
        """Отдаёт героя предпросмотра и расходует его; любой другой id — `Conflict`."""
        preview = self.current(game_id, user_id)
        if preview is None or preview.id != preview_id:
            raise Conflict(STALE_PREVIEW)
        del self._live[(game_id, user_id)]
        return preview.character

    def _forget_expired(self, now: float) -> None:
        while self._live:
            key, oldest = next(iter(self._live.items()))
            if oldest.expires_at > now:
                break
            del self._live[key]

    def _forget_old_rolls(self, now: float) -> None:
        cutoff = now - PREVIEW_WINDOW_S
        for key in list(self._rolls):
            rolls = self._rolls[key]
            while rolls and rolls[0] <= cutoff:
                rolls.popleft()
            if not rolls:
                del self._rolls[key]
