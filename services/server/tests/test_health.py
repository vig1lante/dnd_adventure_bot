import httpx

from dnd_server.app import create_app
from dnd_server.config import Settings, get_settings


async def test_health_reports_env():
    app = create_app(Settings(app_env="dev"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "dev"}


async def get_health(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get("/health")


async def test_health_reports_prod_env():
    response = await get_health(create_app(Settings(app_env="prod")))
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "prod"}


async def test_apps_with_different_settings_do_not_share_env():
    # Настройки привязаны к приложению, а не к модулю: два приложения в одном
    # процессе отвечают каждое своим режимом.
    dev_app = create_app(Settings(app_env="dev"))
    prod_app = create_app(Settings(app_env="prod"))

    assert (await get_health(dev_app)).json()["env"] == "dev"
    assert (await get_health(prod_app)).json()["env"] == "prod"


async def test_health_does_not_expose_secrets():
    # Секреты не попадают в ответы API.
    secrets = {
        "session_secret": "sentinel-session-secret",
        "telegram_bot_token": "sentinel-telegram-token",
        "anthropic_api_key": "sentinel-anthropic-key",
        "openai_compat_api_key": "sentinel-openai-key",
    }
    response = await get_health(create_app(Settings(app_env="dev", **secrets)))

    assert response.json() == {"status": "ok", "env": "dev"}
    for secret in secrets.values():
        assert secret not in response.text


async def test_create_app_without_arguments_reads_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")

    response = await get_health(create_app())

    assert response.json() == {"status": "ok", "env": "dev"}


async def test_create_app_without_arguments_is_prod_by_default():
    # Без APP_ENV сервер закрыт: dev-режим включается только явно.
    response = await get_health(create_app())

    assert response.json() == {"status": "ok", "env": "prod"}


async def test_create_app_without_arguments_uses_cached_settings(monkeypatch):
    # Без аргумента create_app берёт общий кэшированный get_settings(): после
    # первого чтения настроек смена окружения новое приложение не затрагивает.
    monkeypatch.setenv("APP_ENV", "dev")
    assert get_settings().app_env == "dev"
    monkeypatch.setenv("APP_ENV", "prod")

    response = await get_health(create_app())

    assert response.json()["env"] == "dev"
