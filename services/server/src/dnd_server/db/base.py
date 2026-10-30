"""Основа моделей: метаданные с соглашением об именах и тип времени."""

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

# Имена ограничений задаются заранее: SQLite не умеет менять ограничения через ALTER TABLE,
# а пакетные миграции Alembic (render_as_batch) находят ограничение по имени.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """Время в базе — всегда UTC; наружу выходит с часовым поясом.

    SQLite хранит дату без зоны, и «голое» время при чтении нельзя сравнить с `datetime.now(UTC)`
    (отсчёты хода от этого зависят). Поэтому записывается только время с зоной, а читается UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Время должно быть с часовым поясом (например, datetime.now(UTC))")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # `None` в JSON-колонке — это SQL NULL, а не JSON-значение null: иначе обязательная колонка
    # молча приняла бы пустоту.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UtcDateTime(),
        dict[str, Any]: JSON(none_as_null=True),
        list[Any]: JSON(none_as_null=True),
    }
