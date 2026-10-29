"""Момент начала текущей фазы хода у игры.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29 12:00:00

DND-2, блок B11 (спека §15, дополнение B11 §1 п. 1): кнопка хоста «Повторить» в фазе «мастер думает»
появляется через минуту по серверному времени, а не по таймеру браузера, который после перезагрузки
страницы начинается заново. Для этого игре нужна колонка `games.phase_since` — когда началась
текущая `turn_phase`. Её ставит каждая смена фазы хода в той же транзакции (`Game.set_turn_phase`).

Сохранённые игры не теряются: SQLite добавляет колонку без пересоздания таблицы, у старых игр она
пустая, и клиент такой игры ведёт себя как раньше, пока фаза не сменится.

Миграция повторяема, как 0003–0006: SQLite не откатывает DDL, и обрыв после `ADD COLUMN`, но до
записи ревизии, оставляет колонку. Поэтому колонка добавляется и убирается только по результату
осмотра таблицы. То же верно для отката.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии для Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
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
    if not _has_column("games", "phase_since", if_offline=False):
        op.add_column("games", sa.Column("phase_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Откат схемы."""
    # Родной DROP COLUMN SQLite (3.35+) не пересоздаёт таблицу и не копирует строки сохранённых
    # игр (так же сделаны откаты 0002–0006).
    if _has_column("games", "phase_since", if_offline=True):
        op.execute("ALTER TABLE games DROP COLUMN phase_since")
