"""Маршруты конструктора `/api/drafts` (план B4.1; спека §10.3; дополнение dispatcher B4): тонкая
обёртка над `service` и `jobs`.

Все маршруты — только создателям игр (`require_game_creator`, иначе 403); черновик — только его
автору (чужой — 404). Ошибки конструктора (`DraftError`) — `{"detail", "code", "problems"?}` с
кодом по виду отказа (у 404 кода нет). Ошибка формы запроса здесь — тот же отказ, что ошибка
данных раздела: 422 `invalid_data` со строками `problems` (дополнение §4), поэтому у маршрутов свой
класс (`DraftRoute`) — так ловить ошибку формы запроса предлагает документация FastAPI.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from dnd_server.auth.deps import app_settings, require_game_creator
from dnd_server.authoring import jobs, service
from dnd_server.authoring.sections import MAX_TITLE, problems
from dnd_server.authoring.service import DraftError, DraftInvalid
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.db.session import get_session
from dnd_server.games.api import get_turn_tokens
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.turns.timers import TaskSet


async def handle_draft_error(_request: Request, exc: DraftError) -> JSONResponse:
    content: dict[str, Any] = {"detail": exc.message}
    if exc.code:
        content["code"] = exc.code
    if exc.problems:
        content["problems"] = exc.problems
    return JSONResponse(status_code=exc.status, content=content)


class DraftRoute(APIRoute):
    """Маршрут конструктора: ошибка формы запроса (лишнее поле, не JSON, нет версии) — 422
    `invalid_data`. Пути полей без значений входа; первая часть пути (`body`) не нужна клиенту."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                found = problems({**e, "loc": tuple(e["loc"])[1:]} for e in error.errors())
                raise DraftInvalid("Некорректные данные запроса.", "invalid_data", found) from None

        return handle


router = APIRouter(prefix="/api/drafts", route_class=DraftRoute)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewDraftIn(_Body):
    title: str = Field(default="", max_length=MAX_TITLE)


class SectionIn(_Body):
    version: int
    data: Any


class LocksIn(_Body):
    version: int
    locks: list[str]


class WishIn(_Body):
    version: int
    text: str


class GenerateIn(_Body):
    step: str
    wish: str | None = None
    target: str | None = None


def get_authoring_tasks(connection: HTTPConnection) -> TaskSet:
    return connection.app.state.authoring_tasks


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CreatorDep = Annotated[User, Depends(require_game_creator)]


@router.get("")
async def list_drafts(user: CreatorDep, session: SessionDep) -> list[dict[str, Any]]:
    return await service.list_drafts(session, user)


@router.post("", status_code=201)
async def create_draft(
    user: CreatorDep, session: SessionDep, body: NewDraftIn | None = None
) -> dict[str, Any]:
    return await service.create_draft(session, user, body.title if body else "")


@router.get("/{draft_id}")
async def get_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> dict[str, Any]:
    return await service.get_draft(session, user, draft_id)


@router.delete("/{draft_id}", status_code=204)
async def delete_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> Response:
    await service.delete_draft(session, user, draft_id)
    return Response(status_code=204)


@router.put("/{draft_id}/sections/{section}")
async def put_section(
    draft_id: str, section: str, body: SectionIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_section(session, user, draft_id, section, body.version, body.data)


@router.put("/{draft_id}/locks")
async def put_locks(
    draft_id: str, body: LocksIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_locks(session, user, draft_id, body.version, body.locks)


@router.put("/{draft_id}/wishes/{step}")
async def put_wish(
    draft_id: str, step: str, body: WishIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_wish(session, user, draft_id, step, body.version, body.text)


@router.post("/{draft_id}/generate", status_code=202)
async def generate(
    draft_id: str,
    body: GenerateIn,
    user: CreatorDep,
    session: SessionDep,
    request: Request,
    settings: Annotated[Settings, Depends(app_settings)],
    tasks: Annotated[TaskSet, Depends(get_authoring_tasks)],
    turn_tokens: Annotated[TurnTokens | None, Depends(get_turn_tokens)],
) -> dict[str, Any]:
    job = await jobs.create_job(
        session, user, draft_id, step=body.step, wish=body.wish, target=body.target
    )
    # Генерация идёт секунды и минуты: задание выполняется после ответа, клиент опрашивает его.
    tasks.spawn(
        jobs.run_job(
            request.app.state.sessionmaker, job["id"], settings=settings, turn_tokens=turn_tokens
        ),
        failure_text=jobs.TASK_FAILED,
    )
    return job


@router.get("/{draft_id}/jobs/{job_id}")
async def get_job(
    draft_id: str, job_id: str, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await jobs.get_job(session, user, draft_id, job_id)


@router.post("/{draft_id}/validate")
async def validate_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> dict[str, Any]:
    return await service.validate_draft(session, user, draft_id)
