"""Задания генерации шага конструктора.

`create_job` проверяет вход до задания — шаг конструктора (`sections.EDITOR_STEPS`), пожелание
(`draft.Wish`), цель (та же проверка, что у `run_step`: `steps.target_id`, одна сущность раздела
шага) и закрепления — и записывает задание `queued`. Незавершённое задание у черновика одно:
проверка в коде и частичный уникальный индекс базы, так что два одновременных запуска не пройдут
оба.

`run_job` выполняет задание фоновой задачей сервера: `run_step` с провайдером игры и генератора
(`dm.provider.game_provider`; дешёвую модель просит сам запрос шага, `cheap`). Пока задание идёт,
правка черновика закрыта (`service`), поэтому результат пишется поверх той же версии и поднимает её;
если черновик всё же изменился, результат не пишется. Результат проходит те же пределы разделов,
что ручная запись: не уложился — `failed`, черновик не меняется (так повторные цели боя не копят
свои статблоки). id приключения опубликованного черновика результат не меняет. Любой сбой —
`failed` с безопасным текстом: без пожелания, данных черновика и текста исключения (в нём бывают
ответ модели и куски настроек).
В журнал сервера — id черновика, шаг, итог, длительность и класс ошибки.

`fail_interrupted_jobs` при старте сервера помечает задания, прерванные остановкой, сорванными:
иначе они навсегда закрыли бы правку своих черновиков.
"""

import logging
import time
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from dnd_server.authoring.sections import limit_problems, steps_written_by
from dnd_server.authoring.service import (
    JOB_RUNNING,
    DraftConflict,
    DraftInvalid,
    DraftNotFound,
    active_job,
    checked_wish,
    job_out,
    owned_draft,
    published_id,
    put_into,
    require_editor_step,
    stored_draft,
)
from dnd_server.authoring.steps import run_step, target_id
from dnd_server.config import Settings
from dnd_server.db.models import (
    ACTIVE_JOB_STATUSES,
    AdventureDraft,
    AuthoringJob,
    JobStatus,
    User,
    utcnow,
)
from dnd_server.dm.provider import game_provider
from dnd_server.dm.text import safe_traceback
from dnd_server.llm.factory import ConfigError
from dnd_server.llm.internal_api import TurnTokens

logger = logging.getLogger(__name__)

FAILED = "Генерация не удалась. Попробуйте ещё раз."
NOT_CONFIGURED = "Нейронка для конструктора не настроена: обратитесь к владельцу сервера."
DRAFT_CHANGED = "Черновик изменился во время генерации: результат не записан."
RESTARTED = "Генерация прервана: сервер перезапускался."
OVER_LIMITS = (
    "Результат генерации не уложился в пределы раздела: черновик не изменён. Сократите раздел "
    "или закрепите нужное и повторите."
)
# Причина отказа записи в журнале: отличает «черновик изменился» от сбоя провайдера.
DRAFT_CHANGED_REASON = "черновик изменился"
# Текст журнала, если задача конструктора всё же упадёт мимо `run_job` (`TaskSet.spawn`).
TASK_FAILED = "Фоновая задача конструктора завершилась ошибкой."


async def create_job(
    session: AsyncSession,
    user: User,
    draft_id: str,
    *,
    step: str,
    wish: str | None,
    target: str | None,
) -> dict[str, Any]:
    """Задание `queued` после всех проверок входа; отказ до записи — `DraftError`."""
    row = await owned_draft(session, user, draft_id)
    require_editor_step(step)
    if wish is not None:
        wish = checked_wish(wish)
    draft = stored_draft(row)
    try:
        target_id(draft, step, target)
    except ValueError:
        raise DraftInvalid("Цель не подходит этому шагу.", "invalid_target") from None
    if draft.is_locked(f"step:{step}") or (target is not None and draft.is_locked(target)):
        raise DraftInvalid(
            "Шаг или цель закреплены: снимите закрепление, чтобы генерировать.", "step_locked"
        )
    if await active_job(session, row.id) is not None:
        raise DraftConflict(JOB_RUNNING, "job_running")
    job = AuthoringJob(draft_id=row.id, step=step, target=target, wish=wish)
    session.add(job)
    try:
        await session.commit()
    except IntegrityError:
        # Второй запуск успел записать своё задание между проверкой и вставкой.
        await session.rollback()
        raise DraftConflict(JOB_RUNNING, "job_running") from None
    return job_out(job)


async def get_job(session: AsyncSession, user: User, draft_id: str, job_id: str) -> dict[str, Any]:
    """Задание черновика автора; задание другого черновика — `DraftNotFound`."""
    row = await owned_draft(session, user, draft_id)
    job = await session.get(AuthoringJob, job_id)
    if job is None or job.draft_id != row.id:
        raise DraftNotFound("Задание не найдено.")
    return job_out(job)


class _Gone(Exception):
    """Задание или его черновик удалены (гонка генерации с удалением черновика): итог писать
    некуда, и он никому не нужен."""


async def _job_and_draft(session: AsyncSession, job_id: str) -> tuple[AuthoringJob, AdventureDraft]:
    job = await session.get(AuthoringJob, job_id)
    row = await session.get(AdventureDraft, job.draft_id) if job is not None else None
    if job is None or row is None:
        raise _Gone
    return job, row


async def run_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    settings: Settings,
    turn_tokens: TurnTokens | None,
) -> None:
    """Выполняет задание и всегда заканчивает его `done` или `failed`.

    Модель вызывается без открытого соединения с базой. Любой сбой пути — чтения, перевода в
    `running`, вызова модели, записи итога — ведёт к записи `failed`; если не удалась и она,
    задание снимет сверка при старте сервера, а журнал говорит об этом. Задание или черновик
    исчезли — тихий выход."""
    started = time.monotonic()
    job = None
    reason = None  # причина отказа для журнала: класс ошибки или «черновик изменился»
    try:
        async with sessionmaker() as session:
            job, row = await _job_and_draft(session, job_id)
        await _set_status(sessionmaker, job_id, JobStatus.RUNNING)
        provider = game_provider(settings, turn_tokens=turn_tokens)
        before = stored_draft(row)
        result = await run_step(provider, before, job.step, wish=job.wish, target=job.target)
        if over := limit_problems(before, result):
            # Пути полей и пределы схем, без значений: их можно в журнал.
            error, reason = OVER_LIMITS, f"пределы раздела: {'; '.join(over[:3])}"
        elif (error := await _store(sessionmaker, job_id, result, row.version)) is not None:
            reason = DRAFT_CHANGED_REASON
    except _Gone:
        logger.info("Задание конструктора %s снято: черновик удалён.", job_id)
        return
    except Exception as exc:
        reason = type(exc).__name__
        error = NOT_CONFIGURED if isinstance(exc, ConfigError) else FAILED
    unwritten = await _fail(sessionmaker, job_id, error) if error is not None else None
    where = (job_id, job.draft_id if job else "?", job.step if job else "?")
    seconds = time.monotonic() - started
    if error is None:
        logger.info(
            "Задание конструктора %s: черновик %s, шаг %s — done за %.1f с.", *where, seconds
        )
    elif unwritten is None:
        logger.warning(
            "Задание конструктора %s: черновик %s, шаг %s — failed за %.1f с (%s).",
            *where,
            seconds,
            reason,
        )
    else:
        logger.error(
            "Задание конструктора %s: черновик %s, шаг %s — не удалось записать сбой (%s; запись: "
            "%s); задание снимет сверка при следующем старте сервера.",
            *where,
            reason,
            unwritten,
        )


async def _store(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: str, result: Any, version: int
) -> str | None:
    """Пишет результат шага и `done` одной транзакцией, если черновик всё ещё той же версии;
    иначе — текст отказа, черновик не тронут. Время записи получают все шаги раздела шага."""
    async with sessionmaker() as session:
        job, row = await _job_and_draft(session, job_id)
        if row.version != version:
            return DRAFT_CHANGED
        if (locked := await published_id(session, row.id)) is not None:
            # Замысел предлагает свой id, но опубликованный черновик его не меняет.
            result = result.model_copy(update={"id": locked})
        put_into(row, result, steps=steps_written_by(job.step))
        job.status = JobStatus.DONE
        job.finished_at = utcnow()
        try:
            await session.commit()
        except StaleDataError:
            return DRAFT_CHANGED
    return None


async def _set_status(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    status: JobStatus,
    error: str | None = None,
) -> None:
    async with sessionmaker() as session:
        job = await session.get(AuthoringJob, job_id)
        if job is None:
            raise _Gone
        job.status = status
        if status is JobStatus.FAILED:
            job.error = error
            job.finished_at = utcnow()
        await session.commit()


async def _fail(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: str, error: str
) -> str | None:
    """Пишет `failed`; не вышло — класс ошибки записи (её текст может нести данные базы)."""
    try:
        await _set_status(sessionmaker, job_id, JobStatus.FAILED, error)
    except _Gone:
        return None  # задания нет — писать некуда
    except Exception as exc:
        return type(exc).__name__
    return None


async def fail_interrupted_jobs(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Задания `queued`/`running` после перезапуска сервера — `failed` («сервер перезапускался»).

    Сбой базы (например, миграция 0005 ещё не выполнена) пишется в журнал и не останавливает
    сервер, как восстановление игр (`turns.flow.restore_games`)."""
    try:
        async with sessionmaker() as session:
            result = await session.execute(
                update(AuthoringJob)
                .where(AuthoringJob.status.in_(ACTIVE_JOB_STATUSES))
                .values(status=JobStatus.FAILED, error=RESTARTED, finished_at=utcnow())
            )
            await session.commit()
    except SQLAlchemyError as exc:
        logger.error(
            "Не удалось отметить задания конструктора, прерванные перезапуском: база недоступна "
            "или в ней нет таблиц (выполнены ли миграции?).\n%s",
            safe_traceback(exc, settings),
        )
        return
    if result.rowcount:
        logger.warning("Заданий конструктора прервано перезапуском сервера: %d.", result.rowcount)
