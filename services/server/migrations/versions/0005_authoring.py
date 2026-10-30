"""Таблицы конструктора приключений: черновики, задания генерации и опубликованное.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 00:30:00

Три новые таблицы сразу —
- `adventure_drafts` — черновик автора: весь черновик одним JSON (`data`), название для списка,
  время записи разделов по шагам и версия, которая растёт с каждой записью;
- `authoring_jobs` — задание генерации шага; незавершённое (`queued`/`running`) у черновика одно,
  это держит частичный уникальный индекс;
- `authored_adventures` — опубликованные приключения (строку пишет публикация черновика).

Сохранённые игры не теряются: таблицы игр миграция не трогает, а откат удаляет только свои таблицы.

Миграция повторяема, как 0003 и 0004: SQLite не откатывает DDL, и обрыв после части шагов, но до
записи ревизии, оставляет созданные таблицы. Поэтому таблицы и индекс создаются «если нет», а при
откате удаляются «если есть». Схема записана вручную, а не ссылается на модели (как в 0001): время —
`sa.DateTime`, перечисление статуса — `sa.String` с CHECK-ограничением.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии для Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUSES = ("queued", "running", "done", "failed")
ACTIVE_JOB = "status IN ('queued', 'running')"


def upgrade() -> None:
    """Обновление схемы."""
    op.create_table(
        "adventure_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("step_times", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_adventure_drafts_owner_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adventure_drafts")),
        if_not_exists=True,
    )
    op.create_table(
        "authoring_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column("step", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("wish", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in JOB_STATUSES)})",
            name=op.f("ck_authoring_jobs_jobstatus"),
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["adventure_drafts.id"],
            name=op.f("fk_authoring_jobs_draft_id_adventure_drafts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authoring_jobs")),
        if_not_exists=True,
    )
    op.create_table(
        "authored_adventures",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("source_draft_id", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_authored_adventures_owner_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authored_adventures")),
        if_not_exists=True,
    )
    # Одно незавершённое задание на черновик: вторая вставка отвергается самой базой.
    op.create_index(
        "ix_authoring_jobs_active_draft",
        "authoring_jobs",
        ["draft_id"],
        unique=True,
        sqlite_where=sa.text(ACTIVE_JOB),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Откат схемы."""
    op.drop_index("ix_authoring_jobs_active_draft", table_name="authoring_jobs", if_exists=True)
    # Порядок обратный созданию: сначала таблица, которая ссылается на черновики.
    for table in ("authored_adventures", "authoring_jobs", "adventure_drafts"):
        op.drop_table(table, if_exists=True)
