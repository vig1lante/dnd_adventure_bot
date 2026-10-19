"""WebSocket игры `/ws/games/{game_id}?token=<сессия>` (план B6.5).

Порядок такой: токен (нет или недействителен — код 4401), участие в игре (нет — 4403), подписка на
события в хабе, снимок для этого игрока, затем события. Подписка оформляется до чтения снимка:
события, пришедшие за время чтения, ждут в очереди и идут сразу за снимком, поэтому между
снимком и событиями ничего не теряется (повтор клиент отсекает по id, блоки событий заменяют
блоки снимка целиком).

Код закрытия 4401 или 4403 клиент видит, только если рукопожатие принято, поэтому эндпоинт сначала
принимает сокет и лишь потом проверяет игрока: до проверки он ничего не отправляет.

От клиента принимается один вид сообщений — `{"type": "ping"}`, на который идёт `pong`. Всё
остальное игнорируется: ходы и кнопки хоста идут через REST (сервер проверяет игрока и фазу игры),
а подделанное «событие» из сокета никому не рассылается.
"""

import asyncio
import contextlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from dnd_engine.content import SrdContent, load_srd
from dnd_server.auth.deps import websocket_user
from dnd_server.db.models import User
from dnd_server.games.service import NotFound, get_game
from dnd_server.realtime.hub import Connection, Hub
from dnd_server.realtime.views import snapshot_for

logger = logging.getLogger(__name__)

router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_A_MEMBER = 4403
CLOSE_SERVER_ERROR = 1011
CLOSE_TRY_AGAIN_LATER = 1013
# Сколько ждём, пока закроется сокет, который, возможно, уже не читает.
CLOSE_TIMEOUT_S = 1.0
# Единственное сообщение клиента — ping на пару десятков знаков: длинные не разбираются вовсе.
MAX_CLIENT_TEXT = 128

PONG = json.dumps({"type": "pong"})
SNAPSHOT_ERROR = json.dumps(
    {"type": "error", "detail": "Не удалось загрузить игру. Попробуйте переподключиться позже."},
    ensure_ascii=False,
)


def get_hub(connection: HTTPConnection) -> Hub:
    return connection.app.state.hub


def get_srd() -> SrdContent:
    """Встроенные данные SRD. Без параметров нарочно: FastAPI превратил бы аргумент `path` у
    `load_srd` в параметр query, и клиент выбирал бы каталог данных."""
    return load_srd()


def is_ping(text: str) -> bool:
    if len(text) > MAX_CLIENT_TEXT:
        return False
    try:
        message = json.loads(text)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("type") == "ping"


async def listen(websocket: WebSocket, connection: Connection) -> None:
    """Читает сообщения клиента до его ухода: на ping отвечает pong, остальное пропускает."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None and is_ping(text):
            connection.offer(PONG)  # через очередь соединения: один писатель, порядок кадров


async def close_quietly(websocket: WebSocket, code: int) -> None:
    """Закрывает сокет, если получится: клиент мог уйти, а мог просто не читать."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await websocket.close(code=code)


async def snapshot_text(websocket: WebSocket, user: User, game_id: str, srd: SrdContent) -> str:
    async with websocket.app.state.sessionmaker() as session:
        snapshot = await snapshot_for(session, user, game_id, srd=srd)
    return json.dumps(snapshot, ensure_ascii=False)


@router.websocket("/ws/games/{game_id}")
async def game_socket(
    websocket: WebSocket,
    game_id: str,
    hub: Annotated[Hub, Depends(get_hub)],
    srd: Annotated[SrdContent, Depends(get_srd)],
) -> None:
    await websocket.accept()
    user = await websocket_user(websocket)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    async with websocket.app.state.sessionmaker() as session:
        try:
            await get_game(session, user, game_id)
        except NotFound:  # чужая игра и несуществующая для игрока одно и то же
            await websocket.close(code=CLOSE_NOT_A_MEMBER)
            return

    connection = hub.connect(game_id, user.id, websocket)
    reader: asyncio.Task[None] | None = None
    try:
        try:
            snapshot = await snapshot_text(websocket, user, game_id, srd)
        except Exception:
            # Снимок не собрался (например, состояние записано более новой версией сервера):
            # подробности в журнал, клиенту — понятный отказ, а не молчание.
            logger.exception("Не удалось собрать снимок игры %s для игрока %s.", game_id, user.id)
            with contextlib.suppress(Exception):
                await websocket.send_text(SNAPSHOT_ERROR)
            await close_quietly(websocket, CLOSE_SERVER_ERROR)
            return
        connection.start(snapshot)
        reader = asyncio.create_task(listen(websocket, connection))
        done, _ = await asyncio.wait({connection.task, reader}, return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            reader.result()  # клиент ушёл (тишина) или чтение упало (ошибка уйдёт в журнал)
        else:
            # Писатель закончил раньше: клиент не читает или сокет уже закрыт.
            await close_quietly(websocket, CLOSE_TRY_AGAIN_LATER)
    finally:
        if reader is not None:
            reader.cancel()
        hub.disconnect(connection)
