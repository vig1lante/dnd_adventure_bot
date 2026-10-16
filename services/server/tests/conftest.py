import os

import pytest

from dnd_server.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings_sources(monkeypatch, tmp_path):
    # Settings читает `.env` из текущего каталога и переменные окружения. Пустой
    # каталог и снятые переменные делают тесты независимыми от окружения владельца.
    monkeypatch.chdir(tmp_path)
    # pydantic-settings сопоставляет имена без учёта регистра (`app_env` читается
    # так же, как `APP_ENV`), поэтому снимаем переменные в любом регистре.
    for variable in list(os.environ):
        if variable.lower() in Settings.model_fields:
            monkeypatch.delenv(variable)
    # get_settings кэширует результат: чистим кэш до и после теста, чтобы настройки
    # одного теста не просачивались в другой.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
