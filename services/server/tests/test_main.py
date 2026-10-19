from importlib.metadata import entry_points

import httpx
import uvicorn

from dnd_server.__main__ import main


def run_main_with_fake_uvicorn(monkeypatch) -> tuple[str, dict]:
    # Реальный сервер в тестах не запускается: подменяем uvicorn.run и забираем
    # аргументы, с которыми main() его вызвал.
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append((app, options)))
    main()
    [(app_path, options)] = calls
    return app_path, options


def test_main_runs_app_factory_on_localhost_port_8000(monkeypatch):
    _, options = run_main_with_fake_uvicorn(monkeypatch)

    assert options["factory"] is True
    # Наружу сервер выставляет туннель, поэтому привязка только к localhost.
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8000


def test_main_turns_the_access_log_off_because_internal_paths_carry_the_turn_token(monkeypatch):
    _, options = run_main_with_fake_uvicorn(monkeypatch)

    # Путь /internal/dm-tools/<токен>/… нельзя писать в журнал доступа: токен хода — секрет.
    assert options["access_log"] is False


async def test_main_app_path_builds_app_from_environment(monkeypatch):
    # Стык main() -> uvicorn -> create_app() -> get_settings(): uvicorn собирает
    # приложение по строке-импорту и фабрике без аргументов, как при настоящем запуске.
    monkeypatch.setenv("APP_ENV", "dev")
    app_path, options = run_main_with_fake_uvicorn(monkeypatch)

    config = uvicorn.Config(app_path, log_config=None, **options)
    config.load()
    transport = httpx.ASGITransport(app=config.loaded_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "dev"}


def test_dnd_server_console_script_points_to_main():
    [script] = entry_points(group="console_scripts", name="dnd-server")

    assert script.value == "dnd_server.__main__:main"
    assert script.load() is main
