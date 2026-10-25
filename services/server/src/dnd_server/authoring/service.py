"""Черновики конструктора (план B4.1; спека §10.2, §10.3, Р5, Р7; дополнение dispatcher B4).

Слой не знает про HTTP: принимает сессию базы и автора, отдаёт JSON контракта (`Draft`,
`DraftSummary`, `DraftReport`) и отказывает исключениями `DraftError` с кодом отказа (`code`).
Как они превращаются в ответы, решает `api.py`.

Черновик видит и меняет только автор: чужой черновик не отличается от несуществующего (404).
Каждая запись поднимает версию; запись по чужой версии отвергается (409 `version_conflict`), и
проверку держит сама база (`AdventureDraft.version` — счётчик версий ORM), а не только сравнение в
коде. Пока у черновика идёт задание генерации, правка и удаление закрыты (409 `job_running`):
результат задания не перетирает ручные правки.
"""

from collections.abc import Iterable
from typing import Any, ClassVar

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from dnd_server.authoring.draft import SECTION_KINDS, AuthoringDraft, LockRef, Plot, Wish
from dnd_server.authoring.sections import (
    EDITOR_STEPS,
    SECTION_STEPS,
    InvalidSection,
    apply_section,
    draft_report,
    step_statuses,
)
from dnd_server.db.models import (
    ACTIVE_JOB_STATUSES,
    AdventureDraft,
    AuthoringJob,
    User,
    utcnow,
)

_WISH = TypeAdapter(Wish)
_LOCK = TypeAdapter(LockRef)

JOB_RUNNING = "Идёт генерация шага: дождитесь её окончания, потом правьте черновик."
# Закрепления уходят в промпт каждого шага: их число ограничено (дополнение §7).
MAX_LOCKS = 256
VERSION_CONFLICT = "Черновик изменился: обновите его и повторите правку."


class DraftError(Exception):
    """Конструктор отказал. `message` — текст автору по-русски, `code` — вид отказа для клиента,
    `problems` — подробности (строки «<путь>: <сообщение>» без значений входа)."""

    status: ClassVar[int]

    def __init__(
        self, message: str, code: str | None = None, problems: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.problems = problems or []


class DraftNotFound(DraftError):
    """Нет такого черновика или задания, либо черновик чужой."""

    status = 404


class DraftConflict(DraftError):
    """Чужая версия (`version_conflict`) или идёт задание (`job_running`)."""

    status = 409


class DraftInvalid(DraftError):
    """Запрос верен по форме, но так править или генерировать нельзя (код — вид отказа)."""

    status = 422


# --- чтение ---


def stored_draft(row: AdventureDraft) -> AuthoringDraft:
    return AuthoringDraft.model_validate(row.data)


async def owned_draft(session: AsyncSession, user: User, draft_id: str) -> AdventureDraft:
    """Черновик автора; чужой или несуществующий — `DraftNotFound`."""
    row = await session.get(AdventureDraft, draft_id)
    if row is None or row.owner_user_id != user.id:
        raise DraftNotFound("Черновик не найден.")
    return row


async def active_job(session: AsyncSession, draft_id: str) -> AuthoringJob | None:
    return await session.scalar(
        select(AuthoringJob).where(
            AuthoringJob.draft_id == draft_id, AuthoringJob.status.in_(ACTIVE_JOB_STATUSES)
        )
    )


async def latest_job(session: AsyncSession, draft_id: str) -> AuthoringJob | None:
    return await session.scalar(
        select(AuthoringJob)
        .where(AuthoringJob.draft_id == draft_id)
        .order_by(AuthoringJob.created_at.desc())
        .limit(1)
    )


def job_out(job: AuthoringJob) -> dict[str, Any]:
    """`Job` (дополнение §5): без пожелания — оно уходит только в промпт шага."""
    return {
        "id": job.id,
        "step": job.step,
        "target": job.target,
        "status": job.status.value,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def draft_out(row: AdventureDraft, job: AuthoringJob | None) -> dict[str, Any]:
    """`Draft` (дополнение §1): разделы — модели B3 в JSON, `id` — id записи черновика, slug
    приключения — `adventure_id`; `job` — активное или последнее задание."""
    draft = stored_draft(row)
    data = draft.model_dump(mode="json")
    return {
        "id": row.id,
        "version": row.version,
        "title": draft.title,
        "adventure_id": draft.id,
        "journal_intro": draft.journal_intro,
        "settings": data["settings"],
        "world": data["world"],
        **{section: data[section] for section in SECTION_KINDS},
        "plot": None if draft.plot == Plot() else data["plot"],
        "start_location": draft.start_location,
        "finale_location": draft.finale_location,
        "locks": sorted(draft.locks),
        "wishes": data["wishes"],
        "steps": step_statuses(draft, row.step_times),
        "job": job_out(job) if job is not None else None,
    }


def summary_out(row: AdventureDraft) -> dict[str, Any]:
    """`DraftSummary`: строка списка черновиков без разбора всего черновика."""
    return {
        "id": row.id,
        "title": row.title,
        "adventure_id": row.data.get("id"),
        "version": row.version,
        "updated_at": row.updated_at.isoformat(),
    }


async def _out(session: AsyncSession, row: AdventureDraft) -> dict[str, Any]:
    return draft_out(row, await latest_job(session, row.id))


async def list_drafts(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(AdventureDraft)
        .where(AdventureDraft.owner_user_id == user.id)
        .order_by(AdventureDraft.updated_at.desc())
    )
    return [summary_out(row) for row in rows]


async def get_draft(session: AsyncSession, user: User, draft_id: str) -> dict[str, Any]:
    return await _out(session, await owned_draft(session, user, draft_id))


async def validate_draft(session: AsyncSession, user: User, draft_id: str) -> dict[str, Any]:
    return draft_report(stored_draft(await owned_draft(session, user, draft_id)))


# --- запись ---


def put_into(row: AdventureDraft, draft: AuthoringDraft, steps: Iterable[str] = ()) -> None:
    """Кладёт черновик в строку; `steps` — шаги, чей раздел записан сейчас (время их записи).

    Версию поднимает ORM при сбросе изменений; время правки меняется всегда, поэтому запись тех же
    данных тоже идёт в базу и тоже поднимает версию."""
    now = utcnow()
    row.data = draft.model_dump(mode="json")
    row.title = draft.title
    row.step_times = row.step_times | {step: now.isoformat() for step in steps}
    row.updated_at = now


async def create_draft(session: AsyncSession, user: User, title: str) -> dict[str, Any]:
    row = AdventureDraft(owner_user_id=user.id, step_times={})
    put_into(row, AuthoringDraft(title=title))
    session.add(row)
    await session.commit()
    return draft_out(row, None)


async def _require_editable(
    session: AsyncSession, row: AdventureDraft, version: int | None
) -> None:
    """Черновик можно менять: задания нет (`job_running`), версия, если дана, — текущая."""
    if await active_job(session, row.id) is not None:
        raise DraftConflict(JOB_RUNNING, "job_running")
    if version is not None and version != row.version:
        raise DraftConflict(VERSION_CONFLICT, "version_conflict")


async def _commit(session: AsyncSession) -> None:
    """Коммит записи; черновик успел измениться в базе после чтения — `version_conflict`."""
    try:
        await session.commit()
    except StaleDataError:
        await session.rollback()
        raise DraftConflict(VERSION_CONFLICT, "version_conflict") from None


async def put_section(
    session: AsyncSession, user: User, draft_id: str, section: str, version: int, data: Any
) -> dict[str, Any]:
    row = await owned_draft(session, user, draft_id)
    if section not in SECTION_STEPS:
        raise DraftInvalid("Этот раздел пока нельзя править в конструкторе.", "unsupported_section")
    await _require_editable(session, row, version)
    try:
        draft = apply_section(stored_draft(row), section, data)
    except InvalidSection as error:
        raise DraftInvalid(
            "Данные раздела не прошли проверку.", "invalid_data", error.problems
        ) from None
    put_into(row, draft, steps=[SECTION_STEPS[section]])
    await _commit(session)
    return await _out(session, row)


async def put_locks(
    session: AsyncSession, user: User, draft_id: str, version: int, locks: list[str]
) -> dict[str, Any]:
    row = await owned_draft(session, user, draft_id)
    await _require_editable(session, row, version)
    if len(locks) > MAX_LOCKS:
        raise DraftInvalid(
            "Слишком много закреплений.", "invalid_locks", [f"locks: не больше {MAX_LOCKS} ссылок"]
        )
    wrong = [index for index, ref in enumerate(locks) if not _is_lock_ref(ref)]
    if wrong:
        raise DraftInvalid(
            "Закрепления не по формату.",
            "invalid_locks",
            [f"locks.{index}: нужна ссылка «<вид>:<id>» или «step:<шаг>»" for index in wrong],
        )
    put_into(row, stored_draft(row).model_copy(update={"locks": frozenset(locks)}))
    await _commit(session)
    return await _out(session, row)


def _is_lock_ref(ref: str) -> bool:
    try:
        _LOCK.validate_python(ref)
    except ValidationError:
        return False
    return True


def checked_wish(text: str) -> str:
    """Пожелание автора (`draft.Wish`: 1–2000 знаков после обрезки); иначе `invalid_wish`."""
    try:
        return _WISH.validate_python(text)
    except ValidationError:
        raise DraftInvalid("Пожелание — от 1 до 2000 знаков.", "invalid_wish") from None


def require_editor_step(step: str) -> None:
    """Шаг — из тех, что конструктор генерирует в B4; иначе `unsupported_step`."""
    if step not in EDITOR_STEPS:
        raise DraftInvalid("Этот шаг пока нельзя генерировать в конструкторе.", "unsupported_step")


async def put_wish(
    session: AsyncSession, user: User, draft_id: str, step: str, version: int, text: str
) -> dict[str, Any]:
    """Пожелание к шагу: пустое после обрезки удаляет пожелание (дополнение §6)."""
    row = await owned_draft(session, user, draft_id)
    require_editor_step(step)
    await _require_editable(session, row, version)
    draft = stored_draft(row)
    wishes = {name: wish for name, wish in draft.wishes.items() if name != step}
    if text.strip():
        wishes[step] = checked_wish(text)
    put_into(row, draft.model_copy(update={"wishes": wishes}))
    await _commit(session)
    return await _out(session, row)


async def delete_draft(session: AsyncSession, user: User, draft_id: str) -> None:
    row = await owned_draft(session, user, draft_id)
    await _require_editable(session, row, None)
    await session.execute(delete(AuthoringJob).where(AuthoringJob.draft_id == row.id))
    await session.delete(row)
    await _commit(session)
