"""Начальная схема: пользователи, игры, персонажи, состояния, ходы, сообщения и журнал мастера.

Revision ID: 0001
Revises:
Create Date: 2026-10-19 13:48:41

Схема записана вручную, а не ссылается на модели: миграция не должна меняться вслед за кодом.
Тип времени (`UtcDateTime`) и перечисления (`enum_column`) в базе — обычные DATETIME и VARCHAR с
CHECK-ограничением, поэтому здесь они записаны как `sa.DateTime` и `sa.String`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _one_of(column: str, values: Sequence[str]) -> str:
    """Условие CHECK: значение колонки — одно из перечисленных."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


# Допустимые значения колонок-перечислений (план B6.1).
GAME_PHASES = ("lobby", "weaving", "playing", "paused", "finished")
TURN_PHASES = ("discussion", "collecting", "resolving", "dm_failed", "combat_wait", "camp")
TURN_KINDS = ("opening", "round", "combat", "camp_end")
TURN_STATUSES = ("open", "collecting", "resolving", "done", "failed", "superseded")
ACTION_STATUSES = ("draft", "submitted", "pass", "auto_pass", "auto_submitted")
MESSAGE_KINDS = ("narration", "private", "roll", "system", "camp_line", "action")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=True),
        sa.Column("dev_login", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_users_tg_id")),
        sa.UniqueConstraint("dev_login", name=op.f("uq_users_dev_login")),
    )
    op.create_table(
        "games",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("adventure_id", sa.String(), nullable=False),
        sa.Column("host_user_id", sa.String(), nullable=False),
        sa.Column("adventure_data", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=False),
        sa.Column("turn_phase", sa.String(length=11), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("current_turn_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("collect_seconds", sa.Integer(), nullable=False),
        sa.Column("hurry_seconds", sa.Integer(), nullable=False),
        sa.Column("camp_seconds", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("active_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_one_of("phase", GAME_PHASES), name=op.f("ck_games_gamephase")),
        sa.CheckConstraint(_one_of("turn_phase", TURN_PHASES), name=op.f("ck_games_turnphase")),
        sa.ForeignKeyConstraint(
            ["host_user_id"], ["users.id"], name=op.f("fk_games_host_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_games")),
        sa.UniqueConstraint("code", name=op.f("uq_games_code")),
    )
    op.create_table(
        "characters",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_characters_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_characters_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_characters")),
    )
    op.create_table(
        "game_players",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=True),
        sa.Column("is_host", sa.Boolean(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("joined_order", sa.Integer(), nullable=False),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_game_players_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_game_players_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name=op.f("fk_game_players_character_id_characters"),
        ),
        sa.PrimaryKeyConstraint("game_id", "user_id", name=op.f("pk_game_players")),
    )
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
    )
    op.create_table(
        "game_states",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_game_states_game_id_games")
        ),
        sa.PrimaryKeyConstraint("game_id", "version", name=op.f("pk_game_states")),
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("state_version_before", sa.Integer(), nullable=True),
        sa.Column("state_version_after", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_one_of("kind", TURN_KINDS), name=op.f("ck_turns_turnkind")),
        sa.CheckConstraint(_one_of("status", TURN_STATUSES), name=op.f("ck_turns_turnstatus")),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_turns_game_id_games")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
    )
    op.create_table(
        "player_actions",
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("secret", sa.Boolean(), nullable=False),
        sa.Column("inspiration", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=14), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _one_of("status", ACTION_STATUSES), name=op.f("ck_player_actions_actionstatus")
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_player_actions_turn_id_turns")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_player_actions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("turn_id", "user_id", name=op.f("pk_player_actions")),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=9), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_one_of("kind", MESSAGE_KINDS), name=op.f("ck_messages_messagekind")),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], name=op.f("fk_messages_game_id_games")),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], name=op.f("fk_messages_turn_id_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_table(
        "dm_turn_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name=op.f("fk_dm_turn_logs_game_id_games")
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_dm_turn_logs_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dm_turn_logs")),
    )


def downgrade() -> None:
    # Порядок обратный созданию: сначала таблицы, которые ссылаются на другие.
    for table in (
        "dm_turn_logs",
        "messages",
        "player_actions",
        "turns",
        "game_states",
        "pending_rolls",
        "game_players",
        "characters",
        "games",
        "users",
    ):
        op.drop_table(table)
