"""Хаб WebSocket (план B6.5): кому и в каком порядке уходят события игры.

Хаб — `Broadcaster` приложения (`app.state.broadcaster`): сервисы вызывают `send` после коммита и
часто под блокировкой игры (лобби, персонажи, ход мастера). Поэтому `send` не ждёт сокетов: оно
кладёт готовое сообщение в очередь каждого получателя, а отправляет его писатель этого соединения.
Один писатель на соединение держит порядок событий.

Клиент, который не успевает читать, не тормозит игру: отправка одного сообщения ограничена
тайм-аутом, очередь — пределом, и соединение, превысившее любой из них, снимается. Клиент
переподключается и получает свежий снимок (спека §12), так что потерянные события не страшны.

Хаб ничего не знает об играх: что слать и кому, решает вызывающий (`views.py` строит содержимое
и адресата). Он только держит закрытый контракт: в сокеты уходят события известных типов и ничего
больше, а адресат, которого нет среди подключённых, не получает ничего (не «всех»).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from dnd_server.db.models import AUDIENCE_ALL
from dnd_server.dm.text import safe_traceback

logger = logging.getLogger(__name__)

# Типы событий плана B6.5, дневника отряда (блок B8 DND-2), привалов (блок B9 DND-2) и «Ход сдан»
# автору автосданного черновика (блок B11 DND-2). `snapshot`, `pong` и `kicked` сюда не входят: их
# отправляет только WS-эндпоинт своему соединению, а рассылать их через хаб незачем.
EVENT_TYPES = frozenset(
    {
        "phase",
        "player_status",
        "feed_item",
        "party",
        "combat",
        "character",
        "lobby",
        "journal_entry",
        "journal",
        "rest_started",
        "rest_idea",
        "rest_ended",
        "action_auto_submitted",
        "error",
    }
)
# Сколько сообщений ждёт отправки у одного соединения. Игра шлёт единицы событий за ход, поэтому
# переполнение означает, что клиент давно не читает.
QUEUE_LIMIT = 100
# Сколько секунд длится отправка одного сообщения; дольше — клиент не читает.
SEND_TIMEOUT_S = 10.0


class TextSocket(Protocol):
    async def send_text(self, data: str) -> None: ...


class Connection:
    """Один открытый сокет игрока: очередь исходящих сообщений и писатель, который её разбирает."""

    def __init__(
        self,
        game_id: str,
        user_id: str,
        websocket: TextSocket,
        *,
        queue_limit: int,
        send_timeout: float,
        on_stop: Callable[[Connection], None],
    ) -> None:
        self.game_id = game_id
        self.user_id = user_id
        self._websocket = websocket
        self._queue: asyncio.Queue[str] = asyncio.Queue(queue_limit)
        self._send_timeout = send_timeout
        self._on_stop = on_stop
        self._stopped = False
        self.task: asyncio.Task[None] | None = None
        # Игрока исключил хост (`Hub.kick_user`): закрывая сокет, эндпоинт скажет ему об этом.
        self.kicked = False

    def offer(self, text: str) -> bool:
        """Ставит сообщение в очередь без ожидания; `False` — очередь полна."""
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            return False
        return True

    def start(self, first: str | None = None) -> None:
        """Запускает писателя: сначала `first` (снимок), затем накопленные и новые сообщения."""
        self.task = asyncio.create_task(self._write(first))

    def stop(self) -> None:
        self._stopped = True
        if self.task is not None:
            self.task.cancel()

    async def _write(self, first: str | None) -> None:
        try:
            if self._stopped:  # снято до старта: снимок такому клиенту не нужен, он переподключится
                return
            text = first if first is not None else await self._queue.get()
            while True:
                async with asyncio.timeout(self._send_timeout):
                    await self._websocket.send_text(text)
                text = await self._queue.get()
        except TimeoutError:
            logger.warning(
                "Сокет игрока %s (игра %s) не принимает данные %s с: соединение снято.",
                self.user_id,
                self.game_id,
                self._send_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Закрытый клиентом сокет — обычное дело, а не сбой: подробности нужны только отладке,
            # поэтому трассировка собирается, только если уровень DEBUG включён.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Сокет игрока %s (игра %s) закрыт.\n%s",
                    self.user_id,
                    self.game_id,
                    safe_traceback(exc, None),
                )
        finally:
            self._on_stop(self)


class Hub:
    """Подключённые сокеты по играм. Один игрок может держать несколько соединений.

    Несколько вкладок или устройств одного игрока допускаются: Mini App переоткрывают, не закрыв
    прежнее окно, а при одном соединении на игрока два окна вытесняли бы друг друга в вечном
    переподключении. Событие для игрока получают все его соединения.
    """

    def __init__(self, *, queue_limit: int = QUEUE_LIMIT, send_timeout: float = SEND_TIMEOUT_S):
        self._queue_limit = queue_limit
        self._send_timeout = send_timeout
        self._games: dict[str, list[Connection]] = {}

    def connect(self, game_id: str, user_id: str, websocket: TextSocket) -> Connection:
        """Подписывает сокет на события игры. Писатель стартует позже (`Connection.start`):
        события, пришедшие, пока читается снимок, ждут в очереди и идут сразу за ним."""
        connection = Connection(
            game_id,
            user_id,
            websocket,
            queue_limit=self._queue_limit,
            send_timeout=self._send_timeout,
            on_stop=self._forget,
        )
        self._games.setdefault(game_id, []).append(connection)
        return connection

    def disconnect(self, connection: Connection) -> None:
        """Снимает соединение и останавливает писателя; повторный вызов безопасен."""
        self._forget(connection)
        connection.stop()

    def drop_user(self, game_id: str, user_id: str) -> int:
        """Снимает все соединения игрока в игре; возвращает, сколько снято.

        Нужен, когда игрок перестал быть участником (вышел из лобби, исключён): иначе его открытые
        сокеты продолжили бы получать события игры, в том числе общие. Очередь не досылается;
        клиент, увидев закрытие, переподключится и получит 4403 (`realtime.ws`)."""
        dropped = [c for c in self._games.get(game_id, ()) if c.user_id == user_id]
        for connection in dropped:
            self.disconnect(connection)
        return len(dropped)

    def kick_user(self, game_id: str, user_id: str) -> int:
        """Как `drop_user`, но для исключённого хостом: его соединения помечаются `kicked`, и
        эндпоинт (`realtime.ws`) шлёт каждому кадр `kicked` и закрывает его кодом 4403. Кадр идёт
        прямо в сокет исключённого, а не рассылкой: другие игроки его не получают."""
        for connection in self._games.get(game_id, ()):
            if connection.user_id == user_id:
                connection.kicked = True
        return self.drop_user(game_id, user_id)

    def connections(self, game_id: str) -> int:
        return len(self._games.get(game_id, ()))

    async def send(
        self, game_id: str, payload: dict[str, Any], audience: str = AUDIENCE_ALL
    ) -> None:
        """Событие всем соединениям игры (`all`) или соединениям одного игрока (его user_id).

        Адресат, которого среди подключённых нет, не получает ничего. Ждать сокетов не приходится:
        сообщение только встаёт в очереди. Событие вне контракта или не сериализуемое в JSON не
        рассылается и пишется в журнал: вызывающий уже закоммитил игру и от отказа ничего не
        выиграет.
        """
        text = self._encode(game_id, payload)
        if text is None:
            return
        for connection in list(self._games.get(game_id, ())):
            if audience != AUDIENCE_ALL and connection.user_id != audience:
                continue
            if not connection.offer(text):
                logger.warning(
                    "Очередь сокета игрока %s (игра %s) переполнена: соединение снято.",
                    connection.user_id,
                    game_id,
                )
                self.disconnect(connection)

    def _forget(self, connection: Connection) -> None:
        connections = self._games.get(connection.game_id)
        if connections is not None and connection in connections:
            connections.remove(connection)
            if not connections:
                del self._games[connection.game_id]

    @staticmethod
    def _encode(game_id: str, payload: dict[str, Any]) -> str | None:
        if payload.get("type") not in EVENT_TYPES:
            logger.error(
                "Событие вне контракта не разослано (игра %s): %r.", game_id, payload.get("type")
            )
            return None
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.error(
                "Событие %r игры %s не разослано: оно не сериализуется.\n%s",
                payload["type"],
                game_id,
                safe_traceback(exc, None),
            )
            return None
