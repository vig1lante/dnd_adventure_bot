"""Маршруты игр и лобби `/api/games` (план B6.3): тонкая обёртка над `service`.

Ошибки сервиса (`GameError`) превращаются в `{"detail": "<русский текст>"}` с кодом по виду отказа:
нет игры или приключения — 404, чужое действие — 403, не то состояние игры — 409, нельзя создать —
422 (у 422 может быть ещё `problems` — подробности для создателя игры), слишком часто — 429.
Создавать игры могут только игроки из списка владельца (`require_game_creator`).
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection, Request

from dnd_server.adventures import Catalog, get_catalog
from dnd_server.auth.deps import app_settings, current_user, require_game_creator
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.db.session import get_session
from dnd_server.games import service
from dnd_server.games.hooks import Broadcaster, GameHooks, run_game_started
from dnd_server.games.schemas import CreateGameIn, GameOut, JoinIn, ReadyIn
from dnd_server.games.service import GameError, GameLocks, JoinAttempts, ModelCalls
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.realtime.hub import Hub
from dnd_server.realtime.ws import get_hub

router = APIRouter(prefix="/api/games")

STATUS_BY_ERROR = {
    service.NotFound: 404,
    service.Forbidden: 403,
    service.Conflict: 409,
    service.Invalid: 422,
    service.TooManyRequests: 429,
}


async def handle_game_error(_request: Request, exc: GameError) -> JSONResponse:
    content: dict[str, object] = {"detail": exc.message}
    if exc.problems:
        content["problems"] = exc.problems
    return JSONResponse(status_code=STATUS_BY_ERROR[type(exc)], content=content)


# Всё, что приложение кладёт в `app.state`: тесты и следующие пункты блока подменяют эти части.


def get_locks(connection: HTTPConnection) -> GameLocks:
    return connection.app.state.game_locks


def get_model_calls(connection: HTTPConnection) -> ModelCalls:
    return connection.app.state.model_calls


def get_join_attempts(connection: HTTPConnection) -> JoinAttempts:
    return connection.app.state.join_attempts


def get_broadcaster(connection: HTTPConnection) -> Broadcaster:
    return connection.app.state.broadcaster


def get_game_hooks(connection: HTTPConnection) -> GameHooks:
    return connection.app.state.game_hooks


def get_turn_tokens(connection: HTTPConnection) -> TurnTokens | None:
    # Реестр токенов хода есть только в dev (для `claude_code_cli`), в prod его нет вовсе.
    return getattr(connection.app.state, "turn_tokens", None)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(current_user)]
LocksDep = Annotated[GameLocks, Depends(get_locks)]
BroadcasterDep = Annotated[Broadcaster, Depends(get_broadcaster)]
HubDep = Annotated[Hub, Depends(get_hub)]


@router.post("", status_code=201)
async def create_game(
    body: CreateGameIn,
    host: Annotated[User, Depends(require_game_creator)],
    session: SessionDep,
    settings: Annotated[Settings, Depends(app_settings)],
    catalog: Annotated[Catalog, Depends(get_catalog)],
    turn_tokens: Annotated[TurnTokens | None, Depends(get_turn_tokens)],
) -> GameOut:
    return await service.create_game(
        session, host, body, settings=settings, catalog=catalog, turn_tokens=turn_tokens
    )


@router.get("")
async def my_games(user: UserDep, session: SessionDep) -> list[GameOut]:
    return await service.list_games(session, user)


@router.post("/join")
async def join_game(
    body: JoinIn,
    user: UserDep,
    session: SessionDep,
    locks: LocksDep,
    broadcaster: BroadcasterDep,
    attempts: Annotated[JoinAttempts, Depends(get_join_attempts)],
) -> GameOut:
    return await service.join_game(
        session, user, body.code, locks=locks, broadcaster=broadcaster, attempts=attempts
    )


@router.get("/{game_id}")
async def game_summary(game_id: str, user: UserDep, session: SessionDep) -> GameOut:
    return await service.get_game(session, user, game_id)


@router.post("/{game_id}/ready")
async def mark_ready(
    game_id: str,
    body: ReadyIn,
    user: UserDep,
    session: SessionDep,
    locks: LocksDep,
    broadcaster: BroadcasterDep,
) -> GameOut:
    return await service.set_ready(
        session, user, game_id, body.ready, locks=locks, broadcaster=broadcaster
    )


@router.post("/{game_id}/start")
async def start_game(
    game_id: str,
    user: UserDep,
    session: SessionDep,
    locks: LocksDep,
    broadcaster: BroadcasterDep,
    hooks: Annotated[GameHooks, Depends(get_game_hooks)],
    background: BackgroundTasks,
) -> GameOut:
    game = await service.start_game(session, user, game_id, locks=locks, broadcaster=broadcaster)
    # Мастер стартует после ответа: вплетение идёт долго, и хост не должен ждать его в запросе.
    background.add_task(run_game_started, hooks, game.id)
    return game


@router.post("/{game_id}/leave", status_code=204)
async def leave_game(
    game_id: str,
    user: UserDep,
    session: SessionDep,
    locks: LocksDep,
    broadcaster: BroadcasterDep,
    hub: HubDep,
) -> Response:
    await service.leave_game(session, user, game_id, locks=locks, broadcaster=broadcaster)
    # Вышедший больше не участник: его открытые сокеты перестают получать события игры.
    hub.drop_user(game_id, user.id)
    return Response(status_code=204)


@router.post("/{game_id}/players/{user_id}/kick")
async def kick_player(
    game_id: str,
    user_id: str,
    user: UserDep,
    session: SessionDep,
    locks: LocksDep,
    broadcaster: BroadcasterDep,
    hub: HubDep,
) -> GameOut:
    # Исключённый больше не участник: его сокеты снимаются до рассылки нового лобби, слышат только
    # `kicked` и закрываются кодом 4403.
    return await service.kick_player(
        session,
        user,
        game_id,
        user_id,
        locks=locks,
        broadcaster=broadcaster,
        cut_off=lambda target: hub.kick_user(game_id, target),
    )
