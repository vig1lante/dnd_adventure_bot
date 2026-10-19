from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd_server.config import Settings, get_settings

# Корень репозитория: services/server/tests/test_config.py -> три уровня вверх.
ENV_EXAMPLE = Path(__file__).parents[3] / ".env.example"

# Секреты проекта (спека §12, §14): только эти поля должны быть SecretStr.
SECRET_FIELDS = [
    "session_secret",
    "telegram_bot_token",
    "anthropic_api_key",
    "openai_compat_api_key",
]

# Необязательные поля: без значения они равны None.
OPTIONAL_FIELDS = [name for name, field in Settings.model_fields.items() if field.default is None]

# Непустое значение для каждой переменной, отличное от значения по умолчанию: по нему
# видно, откуда поле прочитано (из окружения, `.env` или аргумента).
ALL_FIELDS_ENV = {
    "APP_ENV": "dev",
    "DATABASE_URL": "sqlite+aiosqlite:///./other/test.sqlite3",
    "WEBAPP_PUBLIC_URL": "https://webapp.example.test",
    "CONTENT_DIR": "other/adventures",
    "SESSION_SECRET": "env-session-secret",
    "TELEGRAM_BOT_TOKEN": "env-telegram-token",
    "TELEGRAM_BOT_USERNAME": "env_test_bot",
    "GAME_CREATOR_TG_IDS": "111,222",
    "LLM_PROVIDER": "anthropic_api",
    "LLM_MODEL": "env-model",
    "LLM_EFFORT": "high",
    "LLM_CHEAP_MODEL": "env-cheap-model",
    "ANTHROPIC_API_KEY": "env-anthropic-key",
    "OPENAI_COMPAT_BASE_URL": "https://llm.example.test/v1",
    "OPENAI_COMPAT_API_KEY": "env-openai-key",
    "OPENAI_COMPAT_MODEL": "env-openai-model",
    "CLAUDE_CLI_PATH": "/opt/claude",
    "FAKE_LLM_SCENARIO": "scenarios/demo.yaml",
    "COLLECT_SECONDS": "11",
    "HURRY_SECONDS": "22",
    "CAMP_SECONDS": "33",
}

# Намеренные отличия значений по умолчанию в коде от `.env.example` (отчёт B1.3):
# код по умолчанию закрыт, шаблон для локальной разработки открывает dev-режим.
INTENTIONAL_DEFAULT_DIFFERENCES = {"app_env", "session_secret"}


def env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keys.add(line.partition("=")[0])
    return keys


def unwrap_secrets(values: dict) -> dict:
    # model_dump оставляет SecretStr объектами; для сравнения достаём значения.
    return {
        name: value.get_secret_value() if name in SECRET_FIELDS and value is not None else value
        for name, value in values.items()
    }


def test_settings_fields_match_env_example_keys():
    assert {name.upper() for name in Settings.model_fields} == env_example_keys()


def test_defaults_differ_from_env_example_only_intentionally():
    defaults = Settings().model_dump()
    template = Settings(_env_file=ENV_EXAMPLE).model_dump()

    different = {name for name in defaults if defaults[name] != template[name]}

    assert different == INTENTIONAL_DEFAULT_DIFFERENCES


def test_defaults_are_closed_by_default():
    settings = Settings()

    # dev-вход и провайдер claude_code_cli включаются только явным APP_ENV=dev.
    assert settings.app_env == "prod"
    # Заглушки секрета в коде нет: наличие секрета проверяет потребитель.
    assert settings.session_secret is None
    assert settings.telegram_bot_token is None
    assert settings.anthropic_api_key is None
    assert settings.openai_compat_api_key is None
    # Живая модель по умолчанию не подключена.
    assert settings.llm_provider == "fake"


def test_env_example_template_enables_dev_with_placeholder_secret():
    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.app_env == "dev"
    assert settings.session_secret.get_secret_value() == "change-me"
    # Пустые значения шаблона (`TELEGRAM_BOT_TOKEN=`) означают «не задано».
    assert settings.telegram_bot_token is None
    assert settings.anthropic_api_key is None


def test_every_field_is_read_from_its_uppercase_env_variable(monkeypatch):
    for key, value in ALL_FIELDS_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings()

    assert unwrap_secrets(settings.model_dump()) == {
        "app_env": "dev",
        "database_url": "sqlite+aiosqlite:///./other/test.sqlite3",
        "webapp_public_url": "https://webapp.example.test",
        "content_dir": Path("other/adventures"),
        "session_secret": "env-session-secret",
        "telegram_bot_token": "env-telegram-token",
        "telegram_bot_username": "env_test_bot",
        "game_creator_tg_ids": (111, 222),
        "llm_provider": "anthropic_api",
        "llm_model": "env-model",
        "llm_effort": "high",
        "llm_cheap_model": "env-cheap-model",
        "anthropic_api_key": "env-anthropic-key",
        "openai_compat_base_url": "https://llm.example.test/v1",
        "openai_compat_api_key": "env-openai-key",
        "openai_compat_model": "env-openai-model",
        "claude_cli_path": "/opt/claude",
        "fake_llm_scenario": Path("scenarios/demo.yaml"),
        "collect_seconds": 11,
        "hurry_seconds": 22,
        "camp_seconds": 33,
    }
    # Тест должен менять каждое поле: иначе непрочитанная переменная осталась бы незамеченной.
    assert {key.lower() for key in ALL_FIELDS_ENV} == set(Settings.model_fields)


def test_secret_fields_are_exactly_the_secret_looking_ones():
    # Любое новое поле с именем секрета обязано стать SecretStr и попасть в SECRET_FIELDS.
    secret_looking = {
        name for name in Settings.model_fields if name.endswith(("_secret", "_token", "_api_key"))
    }

    assert secret_looking == set(SECRET_FIELDS)


@pytest.fixture
def settings_with_secrets(monkeypatch) -> Settings:
    for name in SECRET_FIELDS:
        monkeypatch.setenv(name.upper(), f"sentinel-{name}")
    return Settings()


@pytest.mark.parametrize("name", SECRET_FIELDS)
def test_secret_is_hidden_in_every_text_form(settings_with_secrets, name):
    sentinel = f"sentinel-{name}"

    # Значение на месте: иначе скрытие в тексте ничего бы не доказывало.
    assert getattr(settings_with_secrets, name).get_secret_value() == sentinel

    assert sentinel not in repr(settings_with_secrets)
    assert sentinel not in str(settings_with_secrets)
    assert sentinel not in settings_with_secrets.model_dump_json()
    assert sentinel not in str(settings_with_secrets.model_dump())
    assert sentinel not in str(settings_with_secrets.model_dump(mode="json"))


def test_secret_is_hidden_in_validation_error(monkeypatch):
    # Запуск с неверной настройкой пишет ошибку в лог: секреты в ней быть не должны.
    for name in SECRET_FIELDS:
        monkeypatch.setenv(name.upper(), f"sentinel-{name}")
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError) as error:
        Settings()

    for name in SECRET_FIELDS:
        assert f"sentinel-{name}" not in str(error.value)


def test_validation_error_hides_input_values_of_field_and_model_level_errors():
    # Ошибка настройки попадает в журнал запуска. Входное значение в ней показывать нельзя: у
    # секретного поля оно и есть секрет, а у ошибки целой модели входом служит весь набор значений.
    with pytest.raises(ValidationError) as field_error:
        Settings(session_secret=["sentinel-field-level"])
    with pytest.raises(ValidationError) as model_error:
        Settings.model_validate(["sentinel-model-level"])

    assert "session_secret" in str(field_error.value)  # само поле названо
    assert "sentinel-field-level" not in str(field_error.value)
    assert "sentinel-field-level" not in repr(field_error.value)
    assert "sentinel-model-level" not in str(model_error.value)
    assert "sentinel-model-level" not in repr(model_error.value)


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_empty_env_value_means_not_set(monkeypatch, name):
    monkeypatch.setenv(name.upper(), "")

    assert getattr(Settings(), name) is None


def test_empty_env_value_for_field_with_default_keeps_default(monkeypatch):
    monkeypatch.setenv("APP_ENV", "")
    monkeypatch.setenv("COLLECT_SECONDS", "")

    settings = Settings()

    assert settings.app_env == "prod"
    assert settings.collect_seconds == 120


def not_set_value(name: str):
    # «Не задано» для необязательного поля — None, для поля со значением — оно само.
    return Settings.model_fields[name].default


def write_full_dotenv(directory: Path) -> None:
    (directory / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in ALL_FIELDS_ENV.items()), encoding="utf-8"
    )


@pytest.mark.parametrize("name", Settings.model_fields)
def test_empty_env_value_overrides_non_empty_dotenv(monkeypatch, tmp_path, name):
    # Пустая переменная окружения — способ выключить значение из `.env` в чужом
    # прогоне (`TELEGRAM_BOT_TOKEN=` без бота, без ключей владельца).
    write_full_dotenv(tmp_path)
    from_dotenv = Settings().model_dump()
    # Контроль: `.env` действительно прочитан и поле отличается от «не задано».
    assert from_dotenv[name] != not_set_value(name)

    monkeypatch.setenv(name.upper(), "")

    # Меняется только это поле: остальные значения по-прежнему из `.env`.
    assert Settings().model_dump() == {**from_dotenv, name: not_set_value(name)}


@pytest.mark.parametrize("name", Settings.model_fields)
def test_empty_dotenv_value_means_not_set(tmp_path, name):
    (tmp_path / ".env").write_text(f"{name.upper()}=\n", encoding="utf-8")

    assert getattr(Settings(), name) == not_set_value(name)


@pytest.mark.parametrize("name", Settings.model_fields)
def test_empty_init_argument_means_not_set_and_overrides_other_sources(monkeypatch, tmp_path, name):
    # Пустая строка одинакова во всех источниках: «не задано» и перекрывает
    # значения из источников ниже (окружение и `.env`).
    write_full_dotenv(tmp_path)
    monkeypatch.setenv(name.upper(), ALL_FIELDS_ENV[name.upper()])

    assert getattr(Settings(**{name: ""}), name) == not_set_value(name)


def test_non_empty_values_keep_priority_init_env_dotenv_default(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "APP_ENV=dev\nCOLLECT_SECONDS=15\nHURRY_SECONDS=16\n", encoding="utf-8"
    )
    monkeypatch.setenv("COLLECT_SECONDS", "25")
    monkeypatch.setenv("HURRY_SECONDS", "26")

    settings = Settings(hurry_seconds=36)

    assert settings.hurry_seconds == 36  # аргумент важнее окружения и `.env`
    assert settings.collect_seconds == 25  # окружение важнее `.env`
    assert settings.app_env == "dev"  # `.env` важнее значения по умолчанию
    assert settings.camp_seconds == 300  # нигде не задано: значение по умолчанию


def test_dotenv_file_in_current_directory_is_read(tmp_path):
    (tmp_path / ".env").write_text(
        "APP_ENV=dev\nCOLLECT_SECONDS=15\nSESSION_SECRET=dotenv-secret\nTELEGRAM_BOT_TOKEN=\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.app_env == "dev"
    assert settings.collect_seconds == 15
    assert settings.session_secret.get_secret_value() == "dotenv-secret"
    assert settings.telegram_bot_token is None


def test_dotenv_file_may_contain_foreign_variables(tmp_path):
    (tmp_path / ".env").write_text("UNRELATED_VARIABLE=1\nAPP_ENV=dev\n", encoding="utf-8")

    assert Settings().app_env == "dev"


def test_environment_overrides_dotenv_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("APP_ENV=dev\nCOLLECT_SECONDS=15\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("COLLECT_SECONDS", "25")

    settings = Settings()

    assert settings.app_env == "prod"
    assert settings.collect_seconds == 25


def test_init_arguments_override_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("COLLECT_SECONDS", "25")

    settings = Settings(app_env="dev", collect_seconds=35)

    assert settings.app_env == "dev"
    assert settings.collect_seconds == 35


def test_app_env_accepts_only_dev_and_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError) as error:
        Settings()

    assert [item["loc"] for item in error.value.errors()] == [("app_env",)]


@pytest.mark.parametrize("name", ["collect_seconds", "hurry_seconds", "camp_seconds"])
@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_seconds_must_be_positive_integers(monkeypatch, name, value):
    monkeypatch.setenv(name.upper(), value)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert [item["loc"] for item in error.value.errors()] == [(name,)]


@pytest.mark.parametrize("name", ["collect_seconds", "hurry_seconds", "camp_seconds"])
def test_seconds_reject_non_positive_init_value(name):
    with pytest.raises(ValidationError):
        Settings(**{name: 0})


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_get_settings_reads_environment_until_cache_is_cleared(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    assert get_settings().app_env == "dev"

    monkeypatch.setenv("APP_ENV", "prod")
    assert get_settings().app_env == "dev"

    get_settings.cache_clear()
    assert get_settings().app_env == "prod"


# --- список создателей игр (решение владельца B6: «Только из списка») ---


def test_nobody_creates_games_by_default():
    # Пусто — в prod игры не создаёт никто (в dev создают dev-логины, это решает auth).
    assert Settings().game_creator_tg_ids == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("111", (111,)),
        ("111,222", (111, 222)),
        (" 111 , 222 ", (111, 222)),
        ("111,,222,", (111, 222)),  # лишние запятые не ошибка
        ("9007199254740991", (9007199254740991,)),  # id Telegram — до 52 бит, не 32
    ],
)
def test_game_creator_ids_are_read_as_comma_separated_numbers(monkeypatch, raw, expected):
    monkeypatch.setenv("GAME_CREATOR_TG_IDS", raw)

    assert Settings().game_creator_tg_ids == expected


def test_game_creator_ids_are_read_from_the_dotenv_file(tmp_path):
    (tmp_path / ".env").write_text("GAME_CREATOR_TG_IDS=111,222\n", encoding="utf-8")

    assert Settings().game_creator_tg_ids == (111, 222)


@pytest.mark.parametrize("raw", ["abc", "111,secret-x", "0", "-5", "1.5", "[111]", "111;222"])
def test_game_creator_ids_reject_anything_but_positive_numbers(monkeypatch, raw):
    monkeypatch.setenv("GAME_CREATOR_TG_IDS", raw)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert {item["loc"][0] for item in error.value.errors()} == {"game_creator_tg_ids"}
    assert "secret-x" not in str(error.value)  # значение в сообщении не повторяется


# --- имя бота для ссылок-приглашений ---


def test_bot_username_is_not_set_by_default():
    assert Settings().telegram_bot_username is None


@pytest.mark.parametrize("value", ["my_test_bot", "MyBot", "a1234", "x" * 32])
def test_bot_username_accepts_telegram_usernames(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", value)

    assert Settings().telegram_bot_username == value


@pytest.mark.parametrize(
    "value", ["@my_test_bot", "abcd", "x" * 33, "bad name", "1abcde", "бот_бот"]
)
def test_bot_username_rejects_values_that_would_break_the_invite_link(monkeypatch, value):
    # Имя подставляется в `https://t.me/<имя>?startapp=…`: сюда не должна попасть «@» и прочее.
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", value)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert [item["loc"] for item in error.value.errors()] == [("telegram_bot_username",)]
