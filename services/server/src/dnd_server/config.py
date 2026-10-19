from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, PositiveInt, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация сервера из переменных окружения и `.env` (спека §14).

    Имена полей — имена переменных `.env.example` в нижнем регистре. Все значения
    по умолчанию безопасны: нет секретов, нет живых моделей, нет dev-режима.
    """

    model_config = SettingsConfigDict(
        # `.env` ищется в текущем каталоге, поэтому сервер запускают из корня репозитория.
        # От корня репозитория считается только путь SQLite в DATABASE_URL (db/session.py):
        # база не зависит от каталога запуска. Остальные относительные пути (CONTENT_DIR,
        # FAKE_LLM_SCENARIO) читаются от текущего каталога.
        env_file=".env",
        env_file_encoding="utf-8",
        # В `.env` могут лежать переменные других частей проекта.
        extra="ignore",
        # Ошибка настройки уходит в журнал запуска. Без этого pydantic дописывает к ней входное
        # значение: у секретного поля это сам секрет, у ошибки целой модели — все значения сразу.
        hide_input_in_errors=True,
    )

    # По умолчанию prod: dev-вход и claude_code_cli включаются только явным APP_ENV=dev.
    app_env: Literal["dev", "prod"] = "prod"
    database_url: str = "sqlite+aiosqlite:///./data/dnd.sqlite3"
    webapp_public_url: str = "http://localhost:5173"
    content_dir: Path = Path("content/adventures")

    # Секреты: SecretStr скрывает значение в repr, логах и сообщениях об ошибках.
    # Значения по умолчанию нет: заглушка вроде `change-me` в коде позволила бы
    # запустить сервер с подделываемыми сессиями, поэтому проверка наличия
    # секрета — на стороне потребителя.
    session_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    # Имя бота без «@»: нужно для ссылок-приглашений `https://t.me/<имя>?startapp=<код>`. Набор
    # символов — правило Telegram; шаблон не пускает в ссылку ничего лишнего.
    telegram_bot_username: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
    # Telegram-id тех, кто может создавать игры и выбирать провайдера и модель (решение владельца
    # B6: «Только из списка»). Остальные только входят в чужую игру по коду. Пусто — в prod игры не
    # создаёт никто; в dev создают dev-логины (см. auth/deps.py). Переменная — числа через запятую:
    # NoDecode не даёт pydantic-settings разбирать её как JSON, запятые режет валидатор ниже.
    game_creator_tg_ids: Annotated[tuple[PositiveInt, ...], NoDecode] = ()

    llm_provider: str = "fake"
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_cheap_model: str | None = None
    anthropic_api_key: SecretStr | None = None
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None
    openai_compat_model: str | None = None
    claude_cli_path: str = "claude"
    fake_llm_scenario: Path | None = None

    collect_seconds: int = Field(default=120, gt=0)
    hurry_seconds: int = Field(default=60, gt=0)
    camp_seconds: int = Field(default=300, gt=0)

    # Пустая строка — «не задано» в любом источнике (аргумент, окружение, `.env`): в
    # `.env.example` необязательные значения стоят пустыми (`TELEGRAM_BOT_TOKEN=`), а
    # `TELEGRAM_BOT_TOKEN=` в окружении выключает бота и снимает секрет в чужом прогоне.
    # `env_ignore_empty=True` для этого не годится: он выбрасывает пустую переменную
    # окружения ещё до слияния источников, и значение берётся из `.env`. Валидатор
    # работает после слияния, когда приоритет источников уже учтён: пустая строка
    # перекрыла нижние источники, и её ключ убирается, поэтому поле получает значение по
    # умолчанию (None для необязательных). Это работает и для полей, где `None` не
    # допускается (`APP_ENV`, секунды), в отличие от `env_parse_none_str`.
    @model_validator(mode="before")
    @classmethod
    def _drop_empty_values(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        return {name: value for name, value in values.items() if value != ""}

    @field_validator("game_creator_tg_ids", mode="before")
    @classmethod
    def _split_creator_ids(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
