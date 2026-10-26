"""Маршруты конструктора `/api/drafts`, копии приключения каталога `/api/adventures/{id}/draft` и
справочника монстров `/api/srd/monsters` (планы B4.1, B5.1, B6.1; спека §10.3; дополнения
dispatcher B4, B5 и B6): тонкая обёртка над `service`, `jobs` и `publish`.

Все маршруты — только создателям игр (`require_game_creator`, иначе 403); черновик — только его
автору (чужой — 404). Ошибки конструктора (`DraftError`) — `{"detail", "code", "problems"?}` с
кодом по виду отказа (у 404 кода нет). Ошибка формы запроса здесь — тот же отказ, что ошибка
данных раздела: 422 `invalid_data` со строками `problems` (дополнение §4), поэтому у маршрутов свой
класс (`DraftRoute`) — так ловить ошибку формы запроса предлагает документация FastAPI.

Импорт принимает сырое тело `application/zip` (без multipart и новых пакетов): тип и размер тела
проверяются до разбора, размер — ещё и по мере чтения, так что большое тело не копится в памяти.
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

from dnd_server.adventures import Catalog, get_catalog
from dnd_server.auth.deps import app_settings, require_game_creator
from dnd_server.authoring import jobs, publish, service
from dnd_server.authoring.archive import MAX_BODY_BYTES
from dnd_server.authoring.sections import MAX_TITLE, problems
from dnd_server.authoring.service import DraftError, DraftInvalid, DraftNotFound
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


class BodyTooLarge(DraftError):
    """Тело импорта больше предела (`too_large`)."""

    status = 413


class UnsupportedMedia(DraftError):
    """Тело импорта не `application/zip` (`unsupported_media`)."""

    status = 415


ZIP_TYPE = "application/zip"
TOO_LARGE = f"Архив больше {MAX_BODY_BYTES // (1024 * 1024)} МиБ."

router = APIRouter(prefix="/api", route_class=DraftRoute)


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


class PublishIn(_Body):
    """Тело публикации — `{}`: id приключения задаёт раздел «Замысел» (дополнение B6 §2, §6)."""


def get_authoring_tasks(connection: HTTPConnection) -> TaskSet:
    return connection.app.state.authoring_tasks


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CreatorDep = Annotated[User, Depends(require_game_creator)]


@router.get("/drafts")
async def list_drafts(user: CreatorDep, session: SessionDep) -> list[dict[str, Any]]:
    return await service.list_drafts(session, user)


@router.post("/drafts", status_code=201)
async def create_draft(
    user: CreatorDep, session: SessionDep, body: NewDraftIn | None = None
) -> dict[str, Any]:
    return await service.create_draft(session, user, body.title if body else "")


@router.post("/drafts/import", status_code=201)
async def import_draft(user: CreatorDep, session: SessionDep, request: Request) -> dict[str, Any]:
    return await service.import_draft(session, user, await zip_body(request))


async def zip_body(request: Request) -> bytes:
    """Сырое тело импорта: тип `application/zip` (параметры типа не в счёт), не больше
    `MAX_BODY_BYTES` — по `Content-Length` до чтения и по фактическим байтам при чтении."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != ZIP_TYPE:
        raise UnsupportedMedia("Импорт принимает только zip-архив.", "unsupported_media")
    declared = request.headers.get("content-length", "")
    # `isdigit` пропускает и цифры Unicode («³»), которые `int` не читает.
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise BodyTooLarge(TOO_LARGE, "too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise BodyTooLarge(TOO_LARGE, "too_large")
    return bytes(body)


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> dict[str, Any]:
    return await service.get_draft(session, user, draft_id)


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> Response:
    await service.delete_draft(session, user, draft_id)
    return Response(status_code=204)


@router.put("/drafts/{draft_id}/sections/{section}")
async def put_section(
    draft_id: str, section: str, body: SectionIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_section(session, user, draft_id, section, body.version, body.data)


@router.put("/drafts/{draft_id}/locks")
async def put_locks(
    draft_id: str, body: LocksIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_locks(session, user, draft_id, body.version, body.locks)


@router.put("/drafts/{draft_id}/wishes/{step}")
async def put_wish(
    draft_id: str, step: str, body: WishIn, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await service.put_wish(session, user, draft_id, step, body.version, body.text)


@router.post("/drafts/{draft_id}/generate", status_code=202)
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


@router.get("/drafts/{draft_id}/jobs/{job_id}")
async def get_job(
    draft_id: str, job_id: str, user: CreatorDep, session: SessionDep
) -> dict[str, Any]:
    return await jobs.get_job(session, user, draft_id, job_id)


@router.post("/drafts/{draft_id}/validate")
async def validate_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> dict[str, Any]:
    return await service.validate_draft(session, user, draft_id)


@router.get("/drafts/{draft_id}/refs")
async def draft_refs(draft_id: str, user: CreatorDep, session: SessionDep) -> list[dict[str, str]]:
    return await service.draft_refs(session, user, draft_id)


@router.post("/drafts/{draft_id}/publish")
async def publish_draft(
    draft_id: str,
    user: CreatorDep,
    session: SessionDep,
    catalog: Annotated[Catalog, Depends(get_catalog)],
    _body: PublishIn | None = None,
) -> JSONResponse:
    # Первая публикация — 201, обновление своей строки — 200 (дополнение B6 §6).
    created, published = await publish.publish_draft(session, user, draft_id, catalog)
    return JSONResponse(status_code=201 if created else 200, content=published)


@router.get("/drafts/{draft_id}/export")
async def export_draft(draft_id: str, user: CreatorDep, session: SessionDep) -> Response:
    adventure_id, archive = await service.export_draft(session, user, draft_id)
    # id приключения — по шаблону `EntityId` (латиница, цифры, «-», «_»): кавычки и экранирование
    # имени файла в заголовке не нужны.
    return Response(
        content=archive,
        media_type=ZIP_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{adventure_id}.zip"'},
    )


@router.post("/adventures/{adventure_id}/draft", status_code=201)
async def copy_adventure(
    adventure_id: str,
    user: CreatorDep,
    session: SessionDep,
    catalog: Annotated[Catalog, Depends(get_catalog)],
) -> dict[str, Any]:
    # Создание записи — POST, а не GET спеки §10.3 (дополнение B6 §8).
    entry = catalog.get(adventure_id)
    if entry is None:
        raise DraftNotFound("Приключение не найдено.")
    return await service.copy_adventure(session, user, entry.adventure)


@router.get("/srd/monsters")
async def srd_monsters(_user: CreatorDep) -> list[dict[str, Any]]:
    return service.srd_monsters()
