"""Черновики конструктора.

Слой не знает про HTTP: принимает сессию базы и автора, отдаёт JSON контракта (`Draft`,
`DraftSummary`, `DraftReport`, справочник ссылок, справочник монстров SRD) и архив черновика, а
отказывает исключениями `DraftError` с кодом отказа (`code`). Как они превращаются в ответы, решает
`api.py`. Новый черновик бывает пустым, копией приключения каталога или импортом архива
(`from_adventure`); публикация — `publish.py`, разбор и сборка архива — `archive.py`.

Черновик видит и меняет только автор: чужой черновик не отличается от несуществующего (404).
Каждая запись поднимает версию; запись по чужой версии отвергается (409 `version_conflict`), и
проверку держит сама база (`AdventureDraft.version` — счётчик версий ORM), а не только сравнение в
коде. Пока у черновика идёт задание генерации, правка и удаление закрыты (409 `job_running`):
результат задания не перетирает ручные правки.
"""

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Any, ClassVar, NamedTuple

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import load_srd
from dnd_server.adventures import shorten_problems
from dnd_server.authoring.archive import ArchiveInvalid, export_zip, read_adventure
from dnd_server.authoring.draft import (
    SECTION_KINDS,
    AuthoringDraft,
    LockRef,
    Plot,
    Wish,
    from_adventure,
    to_adventure,
)
from dnd_server.authoring.sections import (
    EDITOR_STEPS,
    SECTION_STEPS,
    InvalidSection,
    apply_section,
    draft_report,
    problems,
    section_problems,
    section_steps,
    step_statuses,
)
from dnd_server.db.models import (
    ACTIVE_JOB_STATUSES,
    AdventureDraft,
    AuthoredAdventure,
    AuthoringJob,
    User,
    iso_utc,
    utcnow,
)

_WISH = TypeAdapter(Wish)
_LOCK = TypeAdapter(LockRef)

JOB_RUNNING = "Идёт генерация шага: дождитесь её окончания, потом правьте черновик."
ID_LOCKED = "Приключение уже опубликовано под этим id."
NO_ADVENTURE_ID = "Сначала задайте id приключения."
# Закрепления уходят в промпт каждого шага: их число ограничено.
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
    """`Job`: без пожелания — оно уходит только в промпт шага."""
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
    """`Draft`: разделы — модели черновика в JSON, `id` — id записи черновика, slug
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


# Виды справочника ссылок по порядку: то, что выбирают поля-ссылки разделов.
REF_KINDS = ("deity", "faction", "npc", "location", "item", "encounter")
_SECTION_OF_KIND = {kind: section for section, kind in SECTION_KINDS.items()}


def refs_out(draft: AuthoringDraft) -> list[dict[str, str]]:
    """Справочник ссылок `[{ref, kind, name}]`: виды по `REF_KINDS`, внутри вида — порядок раздела.
    Имя — `name` сущности, у боя имени нет — его id, пустое имя — тоже id. Ничего, кроме имени, в
    справочник не идёт: тайны (`secret`, `dm_notes`, тексты тайников) остаются в черновике."""
    return [
        {"ref": f"{kind}:{entity.id}", "kind": kind, "name": _ref_name(entity)}
        for kind in REF_KINDS
        for entity in getattr(draft, _SECTION_OF_KIND[kind])
    ]


def _ref_name(entity: Any) -> str:
    name = getattr(entity, "name", "")  # у боя (`Encounter`) поля имени нет
    return name if name.strip() else entity.id


class Publication(NamedTuple):
    """Публикация черновика для списка: id приключения и время, без JSON самого приключения."""

    adventure_id: str
    published_at: datetime


def summary_out(row: AdventureDraft, published: Publication | None) -> dict[str, Any]:
    """`DraftSummary`: строка списка черновиков без разбора всего черновика. `published` —
    первая публикация черновика (`first_publications`) или `None`: по ней клиент ставит
    бейдж «опубликовано»."""
    return {
        "id": row.id,
        "title": row.title,
        "adventure_id": row.data.get("id"),
        "version": row.version,
        "updated_at": row.updated_at.isoformat(),
        "published_id": None if published is None else published.adventure_id,
        "published_at": None if published is None else iso_utc(published.published_at),
    }


async def _out(session: AsyncSession, row: AdventureDraft) -> dict[str, Any]:
    return draft_out(row, await latest_job(session, row.id))


async def list_drafts(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = list(
        await session.scalars(
            select(AdventureDraft)
            .where(AdventureDraft.owner_user_id == user.id)
            .order_by(AdventureDraft.updated_at.desc())
        )
    )
    published = await first_publications(session, [row.id for row in rows])
    return [summary_out(row, published.get(row.id)) for row in rows]


async def first_publications(session: AsyncSession, draft_ids: list[str]) -> dict[str, Publication]:
    """Первая публикация каждого черновика из `draft_ids` одним запросом на все (без N+1).

    «Первая» — в том же порядке, что у `published_id`: по времени публикации, затем по id. Читаются
    только три нужных столбца: JSON опубликованного приключения списку ни к чему."""
    if not draft_ids:
        return {}
    rows = await session.execute(
        select(
            AuthoredAdventure.source_draft_id,
            AuthoredAdventure.id,
            AuthoredAdventure.published_at,
        )
        .where(AuthoredAdventure.source_draft_id.in_(draft_ids))
        .order_by(AuthoredAdventure.published_at, AuthoredAdventure.id)
    )
    first: dict[str, Publication] = {}
    for draft_id, adventure_id, published_at in rows:
        first.setdefault(draft_id, Publication(adventure_id, published_at))
    return first


async def get_draft(session: AsyncSession, user: User, draft_id: str) -> dict[str, Any]:
    return await _out(session, await owned_draft(session, user, draft_id))


async def validate_draft(session: AsyncSession, user: User, draft_id: str) -> dict[str, Any]:
    return draft_report(stored_draft(await owned_draft(session, user, draft_id)))


async def published_id(session: AsyncSession, draft_id: str) -> str | None:
    """id приключения, под которым черновик опубликован; не публиковался — `None`. После первой
    публикации id черновика закреплён. Если строк несколько (старые данные), закреплён самый
    ранний id: ответ не зависит от порядка строк в базе."""
    return await session.scalar(
        select(AuthoredAdventure.id)
        .where(AuthoredAdventure.source_draft_id == draft_id)
        .order_by(AuthoredAdventure.published_at, AuthoredAdventure.id)
        .limit(1)
    )


async def same_version(session: AsyncSession, draft_id: str, version: int) -> bool:
    """Черновик всё ещё версии `version` — сверка записью, которая версию не поднимает (запись
    публикации черновик не меняет). Она открывает транзакцию записи, и до её конца другая запись
    черновика не закоммитится между сверкой и записью вызывающего."""
    result = await session.execute(
        update(AdventureDraft)
        .where(AdventureDraft.id == draft_id, AdventureDraft.version == version)
        .values(version=AdventureDraft.version)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def srd_monsters() -> list[dict[str, Any]]:
    """Справочник монстров SRD для боя: `[{id, name, cr, xp, tags}]` в порядке данных; размера и
    типа в данных SRD нет."""
    return [
        {"id": m.id, "name": m.name, "cr": m.cr, "xp": m.xp, "tags": list(m.tags)}
        for m in load_srd().monsters.values()
    ]


async def export_draft(session: AsyncSession, user: User, draft_id: str) -> tuple[str, bytes]:
    """(id приключения, zip папки v2) черновика автора. Ошибки проверки экспорт не блокируют; нет
    id — `no_adventure_id`, черновик не собирается в приключение — `export_invalid`."""
    draft = stored_draft(await owned_draft(session, user, draft_id))
    if draft.id is None:
        raise DraftInvalid(NO_ADVENTURE_ID, "no_adventure_id")
    try:
        adventure = to_adventure(draft)
    except ValidationError as error:
        found = problems(error.errors(include_input=False))
        raise DraftInvalid(
            "Черновик не собирается в приключение.", "export_invalid", found
        ) from None
    # Запись файлов во временную папку и сжатие — не в цикле событий сервера.
    return draft.id, await asyncio.to_thread(export_zip, adventure)


async def draft_refs(session: AsyncSession, user: User, draft_id: str) -> list[dict[str, str]]:
    return refs_out(stored_draft(await owned_draft(session, user, draft_id)))


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
    return await _insert(session, user, AuthoringDraft(title=title))


IMPORT_REFUSED = "Архив не подходит для импорта."


async def copy_adventure(session: AsyncSession, user: User, adventure: Adventure) -> dict[str, Any]:
    """Новый черновик автора — копия приключения каталога (папки или опубликованного) без
    пожеланий; флажок мата — по умолчанию (у приключения его нет). Пределы разделов копия не
    проверяет: папки — данные владельца, опубликованное их уже прошло; публикация копии проверит."""
    return await _insert(session, user, draft_of(adventure))


async def import_draft(session: AsyncSession, user: User, data: bytes) -> dict[str, Any]:
    """Новый черновик автора из присланного архива; архив не подходит (в том числе по пределам
    разделов, как у ручной записи) — `archive_invalid`."""
    try:
        # Распаковка во временную папку и загрузчик — не в цикле событий сервера.
        adventure = await asyncio.to_thread(read_adventure, data)
    except ArchiveInvalid as error:
        raise DraftInvalid(IMPORT_REFUSED, "archive_invalid", error.problems) from None
    draft = draft_of(adventure)
    if found := section_problems(draft):
        lines = [line for section_lines in found.values() for line in section_lines]
        raise DraftInvalid(IMPORT_REFUSED, "archive_invalid", shorten_problems(lines))
    return await _insert(session, user, draft)


def draft_of(adventure: Adventure) -> AuthoringDraft:
    """Черновик из приключения (`from_adventure`). Старт и финал, которых нет, в формате приключения
    — пустые строки, а в черновике — `null` («не выбран», как пишет «Карта»)."""
    draft = from_adventure(adventure)
    return draft.model_copy(
        update={
            "start_location": draft.start_location or None,
            "finale_location": draft.finale_location or None,
        }
    )


async def _insert(session: AsyncSession, user: User, draft: AuthoringDraft) -> dict[str, Any]:
    row = AdventureDraft(owner_user_id=user.id, step_times={})
    put_into(row, draft)
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
    if section == "settings":
        locked = await published_id(session, row.id)
        if locked is not None and draft.id != locked:
            raise DraftInvalid(ID_LOCKED, "adventure_id_locked")
    put_into(row, draft, steps=section_steps(section))
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
    """Шаг — из тех, что генерирует конструктор (`EDITOR_STEPS`); иначе `unsupported_step`."""
    if step not in EDITOR_STEPS:
        raise DraftInvalid("Этот шаг пока нельзя генерировать в конструкторе.", "unsupported_step")


async def put_wish(
    session: AsyncSession, user: User, draft_id: str, step: str, version: int, text: str
) -> dict[str, Any]:
    """Пожелание к шагу: пустое после обрезки удаляет пожелание."""
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
