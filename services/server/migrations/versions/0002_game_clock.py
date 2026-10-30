"""Отметка часов игры: остаток отсчёта на паузе и время игры без пауз.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 03:10:00

Пауза хоста останавливает отсчёт и время игры. Остаток отсчёта выводится из `deadline_at` и
момента паузы, время игры копится от последнего запуска часов, поэтому нужна одна новая колонка
`games.clock_at` (подробнее в комментарии к `Game.clock_at`).

Старые игры не теряются: колонка пустая и необязательная, а у игр, которые уже идут (`weaving`,
`playing`), часы заводятся со времени старта, чтобы время игры продолжало считаться.

Миграция повторяема, как 0003–0006: SQLite не откатывает DDL, и обрыв после
`ADD COLUMN` до записи ревизии оставляет колонку. Поэтому колонка добавляется только по результату
осмотра таблицы, а часы заводятся только там, где их ещё нет (`clock_at IS NULL`): повтор не
возвращает к старту часы игры, которая уже вставала на паузу, и не сбрасывает её время.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    """Есть ли у таблицы колонка. В режиме `--sql` базы нет: перед этим шагом колонки нет, и
    печатаются все шаги подряд."""
    if op.get_context().as_sql:
        return False
    return any(found["name"] == column for found in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_column("games", "clock_at"):
        op.add_column("games", sa.Column("clock_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE games SET clock_at = started_at WHERE clock_at IS NULL"
        " AND started_at IS NOT NULL AND phase IN ('weaving', 'playing')"
    )


def downgrade() -> None:
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # игр; пакетная операция Alembic делала бы и то и другое ради одной колонки.
    op.execute("ALTER TABLE games DROP COLUMN clock_at")
