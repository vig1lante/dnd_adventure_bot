"""Публикация черновика в каталог приключений (план B6.1; спека §10.2, §10.3; дополнение dispatcher
B6 §2, §6).

`publish_draft` проверяет черновик полным списком ошибок `pipeline.check` (без фильтра пустых шагов
«Проверки») и пределами разделов, как у ручной записи, пишет строку `authored_adventures` и кладёт
приключение в каталог на месте: в тот же словарь, что держат маршруты игр и Telegram-бот, поэтому
новое приключение доступно без перезапуска. Повторная публикация своей строки (тот же автор и тот же
черновик) её обновляет; id, занятый папкой контента, чужим автором или другим черновиком, — отказ
`id_taken`, в том числе когда две публикации одного id идут одновременно (вторая вставка упирается в
первичный ключ). Черновик, уже опубликованный под другим id, — `adventure_id_locked`; черновик,
изменённый после чтения публикацией (вторая вкладка, задание генерации), — `version_conflict`.

Игры не меняются: игра копирует приключение в свою строку при создании (`games.service`).

`load_published` при старте сервера читает опубликованное в тот же каталог; при совпадении id с
папкой побеждает папка, строка пропускается с записью в журнал.
"""

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import adventure_from_data
from dnd_engine.adventure.validator import Issue
from dnd_engine.content import load_srd
from dnd_server.adventures import Catalog, CatalogEntry, shorten_problems
from dnd_server.authoring.draft import to_adventure
from dnd_server.authoring.pipeline import check, owner_step
from dnd_server.authoring.sections import (
    SECTION_STEPS,
    finding_message,
    problems,
    section_problems,
)
from dnd_server.authoring.service import (
    ID_LOCKED,
    JOB_RUNNING,
    NO_ADVENTURE_ID,
    VERSION_CONFLICT,
    DraftConflict,
    DraftInvalid,
    active_job,
    owned_draft,
    published_id,
    same_version,
    stored_draft,
)
from dnd_server.config import Settings
from dnd_server.db.models import AuthoredAdventure, User, utcnow
from dnd_server.dm.text import safe_traceback

logger = logging.getLogger(__name__)

BLOCKING = "В приключении есть ошибки: исправьте их, потом публикуйте."
ID_TAKEN = "Этот id уже занят: выберите другой."
# Шаг-владелец находки без шага в строках отказа «<шаг>: <текст>».
WITHOUT_STEP = "общее"


async def publish_draft(
    session: AsyncSession, user: User, draft_id: str, catalog: Catalog
) -> tuple[bool, dict[str, Any]]:
    """Публикует черновик автора: (первая ли это публикация, `{adventure_id, published_at}`).

    Отказы — `DraftError`: идёт задание (`job_running`), нет id (`no_adventure_id`), черновик уже
    опубликован под другим id (`adventure_id_locked`), есть ошибки проверки или пределов разделов
    (`blocking_errors`), id занят (`id_taken`), черновик изменился после чтения
    (`version_conflict`)."""
    row = await owned_draft(session, user, draft_id)
    if await active_job(session, row.id) is not None:
        raise DraftConflict(JOB_RUNNING, "job_running")
    draft = stored_draft(row)
    if draft.id is None:
        raise DraftInvalid(NO_ADVENTURE_ID, "no_adventure_id")
    if await published_id(session, row.id) not in (None, draft.id):
        # У черновика уже есть приключение под другим id: вторую строку ему не заводим.
        raise DraftInvalid(ID_LOCKED, "adventure_id_locked")
    try:
        adventure = to_adventure(draft)
    except ValidationError as error:
        found = problems(error.errors(include_input=False))
        raise DraftInvalid(BLOCKING, "blocking_errors", found) from None
    report = check(adventure, load_srd())
    errors = [
        # Разделы, записанные мимо ручной записи (копия папки, данные до B6), — теми же пределами.
        *(
            f"{SECTION_STEPS[section]}: {line}"
            for section, lines in section_problems(draft).items()
            for line in lines
        ),
        *(_problem(issue) for issue in report.issues if issue.severity == "error"),
    ]
    if errors:
        raise DraftInvalid(BLOCKING, "blocking_errors", shorten_problems(errors))
    folder = catalog.get(draft.id)
    if folder is not None and not folder.published:
        raise DraftConflict(ID_TAKEN, "id_taken")
    published = await session.get(AuthoredAdventure, draft.id)
    if published is not None and (published.owner_user_id, published.source_draft_id) != (
        user.id,
        row.id,
    ):
        raise DraftConflict(ID_TAKEN, "id_taken")
    now = utcnow()
    data = adventure.model_dump(mode="json")
    if published is None:
        session.add(
            AuthoredAdventure(
                id=draft.id,
                owner_user_id=user.id,
                data=data,
                source_draft_id=row.id,
                published_at=now,
            )
        )
    else:
        published.data = data
        published.published_at = now
    try:
        # Сверка версии черновика без её подъёма — в транзакции записи публикации: черновик,
        # изменённый после чтения (другой id «Замысла», результат задания), не публикуется.
        if not await same_version(session, row.id, row.version):
            await session.rollback()
            raise DraftConflict(VERSION_CONFLICT, "version_conflict")
        await session.commit()
    except IntegrityError:
        # Другая публикация этого id успела записать свою строку между проверкой и вставкой.
        await session.rollback()
        raise DraftConflict(ID_TAKEN, "id_taken") from None
    catalog[draft.id] = CatalogEntry(adventure, report, published=True)
    logger.info("Черновик %s опубликован как приключение %r.", row.id, draft.id)
    return published is None, {"adventure_id": draft.id, "published_at": now.isoformat()}


def _problem(issue: Issue) -> str:
    """Строка отказа публикации «<шаг>: <текст>» — тот же текст, что у находки «Проверки»."""
    return f"{owner_step(issue) or WITHOUT_STEP}: {finding_message(issue)}"


async def load_published(
    sessionmaker: async_sessionmaker[AsyncSession], catalog: Catalog, settings: Settings
) -> None:
    """Кладёт опубликованные приключения в каталог при старте сервера.

    id папки контента побеждает: строка с тем же id пропускается. Строка, которая не читается как
    приключение, тоже пропускается; обе причины — в журнал. Сбой базы (миграция 0005 ещё не
    выполнена) пишется в журнал и не останавливает сервер, как сверка заданий конструктора."""
    try:
        async with sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(AuthoredAdventure).order_by(AuthoredAdventure.published_at)
                )
            )
    except SQLAlchemyError as exc:
        logger.error(
            "Не удалось прочитать опубликованные приключения: база недоступна или в ней нет таблиц "
            "(выполнены ли миграции?).\n%s",
            safe_traceback(exc, settings),
        )
        return
    srd = load_srd()
    for row in rows:
        if row.id in catalog:
            logger.warning(
                "Опубликованное приключение %r пропущено: этот id занят папкой контента.", row.id
            )
            continue
        try:
            adventure = adventure_from_data(row.data)
            if adventure.id != row.id:
                raise ValueError("id в данных не совпадает с id строки")
            catalog[row.id] = CatalogEntry(adventure, check(adventure, srd), published=True)
        except Exception as exc:
            # Строку писал сервер, но данные могли испортиться или устареть; остальные приключения
            # и старт от неё не страдают. Значения из данных в журнал не идут (`safe_traceback`).
            logger.warning(
                "Опубликованное приключение %r пропущено: данные не читаются.\n%s",
                row.id,
                safe_traceback(exc, settings),
            )
