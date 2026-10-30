"""Флажок мата у игры: «Мат и грубая речь» из формы создания игры.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 14:00:00

Решение владельца: мат — свойство игры, по умолчанию включён. Мастер и служебные роли
получают его строкой «Мат: разрешён» / «Мат: запрещён», поэтому нужна одна новая колонка
`games.profanity` — обязательная, со значением по умолчанию `1`.

Сохранённые игры не теряются: SQLite добавляет колонку без пересоздания таблицы, а у всех уже
записанных игр она получает значение по умолчанию — мат разрешён, как решил владелец.

Миграция повторяема, как 0003: SQLite не откатывает DDL, и обрыв после `ADD COLUMN`, но до записи
ревизии, оставляет колонку на месте. Поэтому колонка добавляется и убирается только по результату
осмотра таблицы. То же верно для отката.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии для Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str, *, if_offline: bool) -> bool:
    """Есть ли у таблицы колонка. В режиме `--sql` базы нет, посмотреть нельзя: ответ — обычное
    состояние базы перед этим шагом (`if_offline`), и печатаются все шаги подряд."""
    if op.get_context().as_sql:
        return if_offline
    return any(found["name"] == column for found in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    """Обновление схемы."""
    if not _has_column("games", "profanity", if_offline=False):
        op.add_column(
            "games",
            sa.Column("profanity", sa.Boolean(), server_default=sa.true(), nullable=False),
        )


def downgrade() -> None:
    """Откат схемы."""
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # игр (так же сделаны откаты 0002 и 0003).
    if _has_column("games", "profanity", if_offline=True):
        op.execute("ALTER TABLE games DROP COLUMN profanity")
