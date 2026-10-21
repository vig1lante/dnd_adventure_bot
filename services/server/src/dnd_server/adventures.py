"""Каталог приключений сервера (план B6.3): `CONTENT_DIR` -> `Adventure` и отчёт валидатора.

Каталог строится один раз при старте (`lifespan`) и лежит в памяти: `app.state.adventures`. Игры не
читают его после создания, у каждой игры своя копия приключения в базе (решение владельца «Копия
в базе»), так что правка или удаление папки действует только на новые игры.

Папки владельца — вход сервера, поэтому загрузка и проверка идут через движок (`load_adventure` с
пределом размера файлов и без YAML-алиасов, затем `validate`), а не доверяют готовому отчёту.
Битая папка пропускается с записью в журнал и старт не роняет. Отчёт валидатора и проблемы
загрузки называют id скрытых выходов, запертых мест и секретных локаций, поэтому игрокам их не
выдают: `GET /api/adventures` отдаёт только поля плана, подробности идут в журнал сервера и в
ответ 422 создателю игры (`games`).
"""

import logging
from pathlib import Path
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from dnd_engine.adventure.loader import AdventureFormatError, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import Report, validate
from dnd_engine.content import SrdContent
from dnd_server.auth.deps import current_user
from dnd_server.dm.text import safe_traceback

logger = logging.getLogger(__name__)

# Сколько проблем одного приключения выводится сразу (в журнал и в ответ создателю игры). У
# испорченного приключения их бывают тысячи, а читать целиком их никто не будет.
MAX_REPORTED_PROBLEMS = 20


class CatalogEntry(NamedTuple):
    adventure: Adventure
    report: Report


# id приключения -> приключение и отчёт валидатора.
Catalog = dict[str, CatalogEntry]


class AdventureOut(BaseModel):
    """Приключение для игрока: только поля плана, без проблем из отчёта."""

    id: str
    title: str
    pitch: str
    target_hours: float
    score: int
    blocking: bool


def adventure_out(entry: CatalogEntry) -> AdventureOut:
    return AdventureOut(
        id=entry.adventure.id,
        title=entry.adventure.title,
        pitch=entry.adventure.pitch,
        target_hours=entry.adventure.target_hours,
        score=entry.report.score,
        blocking=entry.report.blocking,
    )


def shorten_problems(problems: list[str]) -> list[str]:
    """Первые `MAX_REPORTED_PROBLEMS` проблем и строка о том, сколько осталось."""
    if len(problems) <= MAX_REPORTED_PROBLEMS:
        return problems
    rest = len(problems) - MAX_REPORTED_PROBLEMS
    return [*problems[:MAX_REPORTED_PROBLEMS], f"…и ещё {rest}"]


def blocking_problems(report: Report) -> list[str]:
    """Блокирующие ошибки отчёта строками `объект: сообщение` (с id скрытых мест, не игрокам)."""
    return shorten_problems(
        [f"{issue.ref}: {issue.message}" for issue in report.issues if issue.severity == "error"]
    )


def _adventure_folders(content_dir: Path) -> list[Path]:
    """Папки приключений по порядку имён. Файлы и скрытые папки (`.DS_Store`, `.git`) не в счёт."""
    try:
        entries = sorted(content_dir.iterdir())
    except OSError as exc:  # нет каталога, это файл или нет прав
        logger.warning(
            "Каталог приключений %s не читается (%s): приключений нет.", content_dir, exc
        )
        return []
    return [entry for entry in entries if entry.is_dir() and not entry.name.startswith(".")]


def _load_entry(folder: Path, srd: SrdContent) -> CatalogEntry | None:
    """Приключение из папки с отчётом; `None` — папка пропущена (причина записана в журнал)."""
    try:
        adventure = load_adventure(folder)
        return CatalogEntry(adventure, validate(adventure, srd))
    except AdventureFormatError as exc:
        # Проблемы загрузчика — короткие печатные строки без значений из файлов.
        logger.warning(
            "Приключение из папки %r пропущено, папка некорректна: %s",
            folder.name,
            "; ".join(shorten_problems(exc.problems)),
        )
    except Exception as exc:
        # Папку читает чужой ввод; что бы с ней ни случилось, остальные приключения и старт
        # сервера не страдают. Трассировка нужна, чтобы такой сбой не остался загадкой; идёт она
        # через `safe_traceback`: в тексте ошибки бывают значения из файлов папки.
        logger.error(
            "Приключение из папки %r пропущено из-за непредвиденной ошибки.\n%s",
            folder.name,
            safe_traceback(exc, None),
        )
    return None


def load_catalog(content_dir: Path, srd: SrdContent) -> Catalog:
    """Загружает и проверяет каждое приключение из `content_dir`; ключ каталога — id приключения.

    Приключение с блокирующими ошибками остаётся в каталоге с отчётом: игроки видят флаг
    `blocking`, а игру на нём создать нельзя. Второе приключение с тем же id пропускается.
    """
    catalog: Catalog = {}
    for folder in _adventure_folders(content_dir):
        entry = _load_entry(folder, srd)
        if entry is None:
            continue
        adventure_id = entry.adventure.id
        if adventure_id in catalog:
            logger.warning(
                "Приключение из папки %r пропущено: id %r уже занят другой папкой.",
                folder.name,
                adventure_id,
            )
            continue
        if entry.report.blocking:
            logger.warning(
                "Приключение %r (папка %r) с блокирующими ошибками, игру на нём создать нельзя: %s",
                adventure_id,
                folder.name,
                "; ".join(blocking_problems(entry.report)),
            )
        catalog[adventure_id] = entry
    return catalog


def get_catalog(connection: HTTPConnection) -> Catalog:
    """Каталог приложения (`lifespan` кладёт его в `app.state`); годится для зависимости FastAPI."""
    return connection.app.state.adventures


router = APIRouter(prefix="/api")


@router.get("/adventures", dependencies=[Depends(current_user)])
async def list_adventures(catalog: Annotated[Catalog, Depends(get_catalog)]) -> list[AdventureOut]:
    return [adventure_out(entry) for entry in catalog.values()]
