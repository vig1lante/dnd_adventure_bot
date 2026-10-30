"""Привалы по кнопке хоста: срок короткого привала и личные идеи привала у игры.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28 21:30:00

Хост начинает короткий или долгий привал кнопкой. Долгий идёт прежним сроком `games.camp_seconds`, а
для короткого нужна своя колонка `games.short_rest_seconds` — обязательная, со значением по
умолчанию 120 (как `SHORT_REST_SECONDS`). Личные идеи «расскажи о себе», которые служебная роль
готовит в начале привала, живут до его конца в `games.rest_ideas` (JSON `{id героя: [текст, …]}`,
пусто вне привала): им нужно пережить перезапуск сервера, но не попасть ни в ленту, ни в состояние
игры.

Сохранённые игры не теряются: SQLite добавляет колонки без пересоздания таблицы, старые игры
получают короткий привал в 120 с и пустые идеи. Вид привала лежит в снимке состояния
(`scene.rest`, версия схемы 3), таблиц он не меняет.

Миграция повторяема, как 0003 и 0004: SQLite не откатывает DDL, и обрыв между двумя `ADD COLUMN`
или до записи ревизии оставляет часть колонок. Поэтому каждая колонка добавляется и убирается
только по результату осмотра таблицы. То же верно для отката.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии для Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("short_rest_seconds", "rest_ideas")


def _has_column(table: str, column: str, *, if_offline: bool) -> bool:
    """Есть ли у таблицы колонка. В режиме `--sql` базы нет, посмотреть нельзя: ответ — обычное
    состояние базы перед этим шагом (`if_offline`), и печатаются все шаги подряд."""
    if op.get_context().as_sql:
        return if_offline
    return any(found["name"] == column for found in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    """Обновление схемы."""
    if not _has_column("games", "short_rest_seconds", if_offline=False):
        op.add_column(
            "games",
            sa.Column("short_rest_seconds", sa.Integer(), server_default="120", nullable=False),
        )
    if not _has_column("games", "rest_ideas", if_offline=False):
        op.add_column("games", sa.Column("rest_ideas", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Откат схемы."""
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # игр (так же сделаны откаты 0002–0004).
    for column in reversed(COLUMNS):
        if _has_column("games", column, if_offline=True):
            op.execute(f"ALTER TABLE games DROP COLUMN {column}")
