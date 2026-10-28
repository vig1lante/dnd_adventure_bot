"""Таблицы сервера (спека §9.1, план B6.1).

Здесь только форма данных. Правила игры считает движок: JSON-колонки хранят его модели
(`Adventure`, `Character`, `GameState`) в виде `model_dump(mode="json")` и читаются обратно
через `model_validate`. Таблицы `adventures` нет: приключения читаются из `CONTENT_DIR` в память,
а игра хранит собственную копию (решение владельца «Копия в базе»).
"""

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, String, Text, text, true
from sqlalchemy.orm import Mapped, mapped_column

from dnd_engine.state import GameState
from dnd_server.db.base import Base

# Версия JSON-схемы снимка `GameState`. Растёт, когда формат снимка меняется несовместимо; сервер
# отказывается читать снимок с версией новее своей, чтобы откат кода не терял поля молча.
GAME_STATE_SCHEMA_VERSION = 2

# Адресат сообщения «всем»; личные сообщения адресованы id пользователя.
AUDIENCE_ALL = "all"


class GamePhase(StrEnum):
    LOBBY = "lobby"
    WEAVING = "weaving"
    PLAYING = "playing"
    PAUSED = "paused"
    FINISHED = "finished"


class TurnPhase(StrEnum):
    DISCUSSION = "discussion"
    COLLECTING = "collecting"
    RESOLVING = "resolving"
    DM_FAILED = "dm_failed"
    COMBAT_WAIT = "combat_wait"
    CAMP = "camp"


class TurnKind(StrEnum):
    OPENING = "opening"
    ROUND = "round"
    COMBAT = "combat"
    CAMP_END = "camp_end"


class TurnStatus(StrEnum):
    OPEN = "open"
    COLLECTING = "collecting"
    RESOLVING = "resolving"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ActionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PASS = "pass"
    AUTO_PASS = "auto_pass"
    AUTO_SUBMITTED = "auto_submitted"


class MessageKind(StrEnum):
    NARRATION = "narration"
    PRIVATE = "private"
    ROLL = "roll"
    SYSTEM = "system"
    CAMP_LINE = "camp_line"
    ACTION = "action"


class JobStatus(StrEnum):
    """Состояние задания генерации шага конструктора (спека §10.2)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# Незавершённые задания: у черновика такое одно (индекс ниже), его перезапуск сервера срывает.
ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class UnsupportedSnapshotVersion(ValueError):
    """Снимок состояния записан более новой схемой, чем понимает этот сервер."""


def new_id() -> str:
    return str(uuid.uuid4())


def new_hex_id() -> str:
    """id черновика и задания конструктора: uuid4 в 32 шестнадцатеричных знаках (дополнение B4 §1)."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_type: type[StrEnum]) -> Enum:
    """Тип колонки-перечисления: в базе лежат значения (`lobby`), а не имена членов.

    CHECK-ограничение отвергает неизвестное значение и мимо ORM.
    """
    return Enum(
        enum_type,
        name=enum_type.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)  # у dev-входа пусто
    dev_login: Mapped[str | None] = mapped_column(unique=True)  # от dev-1 до dev-6, только в dev
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(6), unique=True)  # приглашение: 6 символов A-Z0-9
    adventure_id: Mapped[str]
    host_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # Копия приключения на момент создания игры (JSON `Adventure`). Игра всегда идёт по ней, а
    # правки и удаление файлов в CONTENT_DIR касаются только новых игр. После создания игры
    # колонку никто не меняет.
    adventure_data: Mapped[dict[str, Any]]

    phase: Mapped[GamePhase] = mapped_column(enum_column(GamePhase), default=GamePhase.LOBBY)
    turn_phase: Mapped[TurnPhase | None] = mapped_column(enum_column(TurnPhase))
    deadline_at: Mapped[datetime | None]

    state_version: Mapped[int] = mapped_column(default=0)  # версия текущего `GameState`
    # Без внешнего ключа: games и turns ссылаются друг на друга, а SQLite не умеет добавлять
    # ограничения к готовой таблице.
    current_turn_id: Mapped[str | None]

    # Пусто — значения по умолчанию из env.
    provider: Mapped[str | None]
    model: Mapped[str | None]
    collect_seconds: Mapped[int]
    hurry_seconds: Mapped[int]
    camp_seconds: Mapped[int]
    # Флажок «Мат и грубая речь» (миграция 0004, решение Р1): мастер и служебные роли получают его
    # строкой «Мат: разрешён/запрещён». По умолчанию включён, и у старых игр тоже.
    profanity: Mapped[bool] = mapped_column(default=True, server_default=true())

    started_at: Mapped[datetime | None]
    active_seconds: Mapped[int] = mapped_column(default=0)  # время игры без пауз, для темпа
    # Отметка часов игры (миграция 0002). Идущая игра: `active_seconds` посчитано до `clock_at`, а
    # время после неё ещё не добавлено (`active_time`); часы заводят старт игры и снятие паузы.
    # Пауза добавляет прошедшее время в `active_seconds` и ставит сюда момент паузы: часы стоят, а
    # от этого момента считается остаток отсчёта до `deadline_at` (`paused_remaining`). Пусто —
    # часов нет: игра в лобби или законченная.
    clock_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def active_time(self, now: datetime) -> int:
        """Секунды игры без пауз на момент `now`; на паузе часы стоят."""
        if self.phase == GamePhase.PAUSED or self.clock_at is None:
            return self.active_seconds
        return self.active_seconds + max(0, int((now - self.clock_at).total_seconds()))

    def paused_remaining(self) -> timedelta | None:
        """Остаток отсчёта на паузе; `None` — игра не на паузе или отсчёта у неё нет."""
        if self.phase != GamePhase.PAUSED or self.deadline_at is None or self.clock_at is None:
            return None
        return max(timedelta(0), self.deadline_at - self.clock_at)


class PlayerCharacter(Base):
    """Персонаж игрока: JSON `Character` движка; `id` равен `Character.id`."""

    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    data: Mapped[dict[str, Any]]
    # Выбор игрока при создании героя (миграция 0003, решение владельца R10): `{"mode": …,
    # "choices": …}`. Для подробного режима `choices` — `CharacterChoices`, для быстрого — класс,
    # раса, замысел и имя игрока, для случайного — пусто. У героев, созданных до миграции, колонка
    # пуста. Отдаётся только владельцу героя (`GET .../characters/mine`), в снимки и лобби не идёт.
    choices: Mapped[dict[str, Any] | None]


class GamePlayer(Base):
    __tablename__ = "game_players"

    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    character_id: Mapped[str | None] = mapped_column(ForeignKey("characters.id"))
    is_host: Mapped[bool] = mapped_column(default=False)
    ready: Mapped[bool] = mapped_column(default=False)
    joined_order: Mapped[int]
    joined_at: Mapped[datetime] = mapped_column(default=utcnow)


class GameStateSnapshot(Base):
    """Одна версия `GameState`. Хранятся все версии: прежняя нужна откату «Перегенерировать»."""

    __tablename__ = "game_states"

    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    schema_version: Mapped[int]  # версия формата `data`, см. GAME_STATE_SCHEMA_VERSION
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @classmethod
    def from_state(cls, state: GameState) -> GameStateSnapshot:
        return cls(
            game_id=state.game_id,
            version=state.version,
            schema_version=GAME_STATE_SCHEMA_VERSION,
            data=state.model_dump(mode="json"),
        )

    def to_state(self) -> GameState:
        if self.schema_version > GAME_STATE_SCHEMA_VERSION:
            raise UnsupportedSnapshotVersion(
                f"Снимок игры {self.game_id} (версия {self.version}) записан схемой "
                f"{self.schema_version}, а этот сервер читает схемы не новее "
                f"{GAME_STATE_SCHEMA_VERSION}: обновите сервер. Снимок не изменён."
            )
        return GameState.model_validate(self.data)


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    number: Mapped[int]
    kind: Mapped[TurnKind] = mapped_column(enum_column(TurnKind))
    status: Mapped[TurnStatus] = mapped_column(enum_column(TurnStatus), default=TurnStatus.OPEN)
    # Пусто, пока версии нет: у открытия игры нет «до», у неудавшегося хода нет «после».
    state_version_before: Mapped[int | None]
    state_version_after: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlayerAction(Base):
    __tablename__ = "player_actions"

    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(default=False)
    inspiration: Mapped[bool] = mapped_column(default=False)
    status: Mapped[ActionStatus] = mapped_column(
        enum_column(ActionStatus), default=ActionStatus.DRAFT
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id"))  # пусто вне хода
    audience: Mapped[str]  # AUDIENCE_ALL или id пользователя
    kind: Mapped[MessageKind] = mapped_column(enum_column(MessageKind))
    text: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any] | None]
    superseded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DmTurnLog(Base):
    """Журнал попытки хода мастера (спека §8.8)."""

    __tablename__ = "dm_turn_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"))
    attempt: Mapped[int]
    provider: Mapped[str]
    model: Mapped[str | None]
    # Без секретов (инвариант «д»): ни токенов бота, ни ключей, ни initData.
    request: Mapped[dict[str, Any]]
    trace: Mapped[list[Any]] = mapped_column(default=list)  # вызовы инструментов и их результаты
    output: Mapped[str | None] = mapped_column(Text)  # пусто, если итогового текста не было
    usage: Mapped[dict[str, Any] | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AdventureDraft(Base):
    """Черновик конструктора приключений (миграция 0005, спека §10.2, Р5).

    Весь черновик — JSON `AuthoringDraft` (контракт B3) в `data`: разделы, закрепления, пожелания и
    название; так у черновика одна версия и одно место правды. `title` повторяет название для списка
    черновиков. `version` ведёт ORM (`version_id_col`): каждая запись поднимает её на 1, а запись
    поверх чужой версии отвергается (`StaleDataError`), так что две вкладки не перетрут друг друга.
    """

    __tablename__ = "adventure_drafts"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_hex_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str]
    data: Mapped[dict[str, Any]]
    # Время последней записи раздела каждого шага: `{шаг: ISO-время}` (`Draft.steps[шаг].updated_at`).
    step_times: Mapped[dict[str, Any]] = mapped_column(default=dict)
    version: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    __mapper_args__ = {"version_id_col": version}


class AuthoringJob(Base):
    """Задание генерации шага черновика (миграция 0005, спека §10.2, Р6).

    Незавершённое задание (`queued`/`running`) у черновика одно: это держит частичный уникальный
    индекс базы, а не только проверка в коде, поэтому два одновременных запуска не пройдут оба.
    `wish` — пожелание этого запуска (текст автора); в журнал сервера и ответы оно не попадает.
    """

    __tablename__ = "authoring_jobs"
    __table_args__ = (
        Index(
            "ix_authoring_jobs_active_draft",
            "draft_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_hex_id)
    draft_id: Mapped[str] = mapped_column(ForeignKey("adventure_drafts.id"))
    step: Mapped[str]
    target: Mapped[str | None]
    wish: Mapped[str | None] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(enum_column(JobStatus), default=JobStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class AuthoredAdventure(Base):
    """Опубликованное приключение конструктора (миграция 0005, спека §10.2; заполняет блок B6).

    `id` — id приключения. Ссылка на черновик без внешнего ключа: черновик можно удалить и после
    публикации."""

    __tablename__ = "authored_adventures"

    id: Mapped[str] = mapped_column(primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    data: Mapped[dict[str, Any]]
    source_draft_id: Mapped[str | None]
    published_at: Mapped[datetime] = mapped_column(default=utcnow)
