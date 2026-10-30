"""Зависимости лобби, которые подставляются снаружи.

Лобби только сообщает о событиях, а кто их слушает, решают позже: рассылка по сокетам (хаб) и
запуск мастера. Пока вместо них стоят заглушки, которые ничего не делают; приложение держит
их в `app.state.broadcaster` и `app.state.game_hooks`, а тесты подставляют регистраторы.

Оба вызова идут после коммита, когда игра в базе уже изменена, и поэтому сбой слушателя не должен
ронять запрос: изменение не откатить, а ответ «ошибка» заставил бы клиента повторять его. Сбой
записывается в журнал сервера с id игры. Трассировка идёт через `dm.text.safe_traceback`: настроек
здесь нет, значения секретов из текста не вырезаются, но значения входа pydantic и управляющие
знаки в записи не попадают.
"""

import logging
from typing import Any, Protocol

from dnd_server.db.models import AUDIENCE_ALL
from dnd_server.dm.text import safe_traceback

logger = logging.getLogger(__name__)


class Broadcaster(Protocol):
    """Рассылка события игроков игры: `audience` — `AUDIENCE_ALL` или id пользователя."""

    async def send(
        self, game_id: str, payload: dict[str, Any], audience: str = AUDIENCE_ALL
    ) -> None: ...


class GameHooks(Protocol):
    """Что делать дальше с игрой, у которой началась игровая часть."""

    async def on_game_started(self, game_id: str) -> None: ...


class NullBroadcaster:
    """Пока хаб не подключён, события некому слать."""

    async def send(
        self, game_id: str, payload: dict[str, Any], audience: str = AUDIENCE_ALL
    ) -> None:
        return None


class NullGameHooks:
    """Пока мастер не подключён, старт игры только переводит её в `weaving`."""

    async def on_game_started(self, game_id: str) -> None:
        return None


async def send_to_game(
    broadcaster: Broadcaster, game_id: str, payload: dict[str, Any], audience: str = AUDIENCE_ALL
) -> None:
    """Рассылает событие; сбой рассылки пишется в журнал и вызывающего не касается."""
    try:
        await broadcaster.send(game_id, payload, audience)
    except Exception as exc:
        logger.error(
            "Не удалось разослать событие %r игры %s.\n%s",
            payload.get("type"),
            game_id,
            safe_traceback(exc, None),
        )


async def run_game_started(hooks: GameHooks, game_id: str) -> None:
    """Запускает `on_game_started` фоновой задачей запроса; сбой пишется в журнал, не в ответ."""
    try:
        await hooks.on_game_started(game_id)
    except Exception as exc:
        logger.error(
            "Не удалось начать игру %s после старта из лобби.\n%s",
            game_id,
            safe_traceback(exc, None),
        )
