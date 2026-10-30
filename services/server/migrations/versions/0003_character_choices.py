"""Выбор игрока при создании героя; таблицы pending_rolls больше нет.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 18:00:00

Два решения владельца:
- бросок характеристик `4d6kh3` убран из создания героя, поэтому набор, который сервер бросал
  игроку и запоминал, не нужен — таблица `pending_rolls` удаляется;
- «Изменить героя» открывает форму, заполненную прежним выбором игрока, поэтому у героя хранится
  этот выбор — новая колонка `characters.choices` (JSON, `{"mode": …, "choices": …}`).

Сохранённые игры не теряются: игры, герои и состояния не затронуты, колонка пустая и
необязательная (у героев прежних ревизий выбора нет, форма «Изменить героя» у них пустая).
Единственное, что пропадает, — незавершённые наборы характеристик в `pending_rolls`: они жили до
создания героя и после обновления ни к чему не пригодны.

Миграция повторяема. SQLite не откатывает DDL: `DROP TABLE` и `ADD COLUMN` выполняются сразу и
насовсем, а запись о ревизии появляется после обоих шагов. Обрыв между ними (база занята, кончилось
место, процесс убит) оставляет базу посередине, и повторный запуск не должен падать на шаге, который
уже сделан: таблица удаляется и создаётся «если есть» и «если нет», а колонка добавляется и
убирается только по результату осмотра таблицы (у колонок в SQLite такого синтаксиса нет). То же
верно для отката.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str, *, if_offline: bool) -> bool:
    """Есть ли у таблицы колонка. В режиме `--sql` базы нет, посмотреть нельзя: ответ — обычное
    состояние базы перед этим шагом (`if_offline`), и печатаются все шаги подряд."""
    if op.get_context().as_sql:
        return if_offline
    return any(found["name"] == column for found in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    op.drop_table("pending_rolls", if_exists=True)
    if not _has_column("characters", "choices", if_offline=False):
        op.add_column("characters", sa.Column("choices", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # героев (так же сделан откат 0002).
    if _has_column("characters", "choices", if_offline=True):
        op.execute("ALTER TABLE characters DROP COLUMN choices")
    # Таблица возвращается пустой и точно такой, какой её создаёт 0001: набор характеристик, который
    # бросали до обновления, откат не восстанавливает.
    op.create_table(
        "pending_rolls",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_pending_rolls_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_pending_rolls_user_id_users")
        ),
        sa.PrimaryKeyConstraint("game_id", "user_id", name=op.f("pk_pending_rolls")),
        if_not_exists=True,
    )
