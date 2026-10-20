"""Отметка часов игры: остаток отсчёта на паузе и время игры без пауз.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 03:10:00

Пауза хоста (B7.4) останавливает отсчёт и время игры. Остаток отсчёта выводится из `deadline_at` и
момента паузы, время игры копится от последнего запуска часов, поэтому нужна одна новая колонка
`games.clock_at` (подробнее в комментарии к `Game.clock_at`).

Старые игры не теряются: колонка пустая и необязательная, а у игр, которые уже идут (`weaving`,
`playing`), часы заводятся со времени старта, чтобы время игры продолжало считаться.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("games", sa.Column("clock_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE games SET clock_at = started_at"
        " WHERE started_at IS NOT NULL AND phase IN ('weaving', 'playing')"
    )


def downgrade() -> None:
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # игр; пакетная операция Alembic делала бы и то и другое ради одной колонки.
    op.execute("ALTER TABLE games DROP COLUMN clock_at")
