"""WebSocket игры `/ws/games/{game_id}`; токен сессии — вторым подпротоколом после `dnd-game.v1`.

Клиент открывает сокет с подпротоколами `[dnd-game.v1, <токен>]` (`auth.deps.SOCKET_PROTOCOL`),
сервер принимает рукопожатие с подпротоколом `dnd-game.v1` и токен назад не называет. Токен в
адресе (`?token=…`, прежний способ) не принимается.

Порядок такой: токен (нет или недействителен — код 4401), участие в игре (нет — 4403), подписка на
события в хабе, снимок для этого игрока, затем события. Подписка оформляется до чтения снимка:
события, пришедшие за время чтения, ждут в очереди и идут сразу за снимком, поэтому между
снимком и событиями ничего не теряется (повтор клиент отсекает по id, блоки событий заменяют
блоки снимка целиком).

Код закрытия 4401 или 4403 клиент видит, только если рукопожатие принято, поэтому эндпоинт сначала
принимает сокет и лишь потом проверяет игрока: до проверки он ничего не отправляет. Предложенный
`dnd-game.v1` называется в ответе и при плохом токене: браузер, не получив в ответ одного из своих
подпротоколов, рвёт соединение, и клиент не увидел бы 4401.

Когда хаб снял соединение, эндпоинт закрывает сокет. Исключённому хостом (`Hub.kick_user`) он сам
шлёт кадр `{"type": "kicked"}` и закрывает сокет кодом 4403 с причиной `kicked`:
клиент говорит «Хост исключил вас», а не «игра недоступна». Остальным — 1013 «переподключитесь»:
вышедший из лобби при переподключении получит обычный 4403.

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
from dnd_server.auth.deps import SOCKET_PROTOCOL, websocket_user
from dnd_server.db.models import User
from dnd_server.dm.text import safe_traceback
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
KICKED = json.dumps({"type": "kicked"})
KICKED_REASON = "kicked"
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


async def close_quietly(websocket: WebSocket, code: int, reason: str | None = None) -> None:
    """Закрывает сокет, если получится: клиент мог уйти, а мог просто не читать."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await websocket.close(code=code, reason=reason)


async def say_kicked(websocket: WebSocket) -> None:
    """Последний кадр исключённому и закрытие 4403 `kicked`; писатель соединения уже остановлен,
    поэтому кадр идёт прямо в сокет и ничего не обгоняет."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await websocket.send_text(KICKED)
    await close_quietly(websocket, CLOSE_NOT_A_MEMBER, KICKED_REASON)


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
    offered = websocket.scope.get("subprotocols", [])
    await websocket.accept(subprotocol=SOCKET_PROTOCOL if SOCKET_PROTOCOL in offered else None)
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
        except Exception as exc:
            # Снимок не собрался (например, состояние записано более новой версией сервера):
            # подробности в журнал, клиенту — понятный отказ, а не молчание.
            logger.error(
                "Не удалось собрать снимок игры %s для игрока %s.\n%s",
                game_id,
                user.id,
                safe_traceback(exc, websocket.app.state.settings),
            )
            with contextlib.suppress(Exception):
                await websocket.send_text(SNAPSHOT_ERROR)
            await close_quietly(websocket, CLOSE_SERVER_ERROR)
            return
        connection.start(snapshot)
        reader = asyncio.create_task(listen(websocket, connection))
        done, _ = await asyncio.wait({connection.task, reader}, return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            reader.result()  # клиент ушёл (тишина) или чтение упало (ошибка уйдёт в журнал)
        elif connection.kicked:
            await say_kicked(websocket)
        else:
            # Писатель закончил раньше: клиент не читает, сокет уже закрыт или игрок вышел.
            await close_quietly(websocket, CLOSE_TRY_AGAIN_LATER)
    finally:
        if reader is not None:
            reader.cancel()
        hub.disconnect(connection)
