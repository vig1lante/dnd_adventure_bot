"""Стенд Docker Compose и приключения с диска (план DND-2 B3.2; README «Запуск в Docker Compose»,
«Генерация приключения»).

Сервер в контейнере читает каталог `CONTENT_DIR` при старте: новое приключение видно после
`docker compose restart server` без пересборки образа, только если сервер читает ту самую папку с
диска. Писать в неё может только генератор — отдельный сервис `generator` из профиля `tools`
(`docker compose run --rm generator dnd-generate …`); сервер игры подключает её только на чтение
(защита в глубину, находка security L1 блока 3). Docker здесь не запускается: проверяется
конфигурация стенда, которую читает `docker compose`.
"""

from pathlib import Path

import yaml

from dnd_server.config import Settings

COMPOSE = Path(__file__).resolve().parents[3] / "compose.yaml"
# Папка приключений на диске — та же, что у сервера без Docker.
CONTENT_SOURCE = f"./{Settings.model_fields['content_dir'].default.as_posix()}"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def content_mount(service: dict) -> tuple[str, str]:
    """(путь в контейнере, режим) тома папки приключений у сервиса compose."""
    [mount] = [
        v for v in service["volumes"] if isinstance(v, str) and v.startswith(f"{CONTENT_SOURCE}:")
    ]
    target, _, mode = mount.removeprefix(f"{CONTENT_SOURCE}:").partition(":")
    return target, mode


def test_the_server_container_reads_adventures_from_the_folder_on_disk_read_only():
    server = compose()["services"]["server"]

    target, mode = content_mount(server)

    assert CONTENT_SOURCE == "./content/adventures"
    assert mode == "ro"  # сервер игры в каталог не пишет
    assert server["environment"]["CONTENT_DIR"] == target


def test_the_generator_writes_into_the_same_folder_and_is_started_only_on_request():
    services = compose()["services"]
    generator = services["generator"]

    target, mode = content_mount(generator)

    assert mode in ("", "rw")  # в неё пишет dnd-generate
    assert target == content_mount(services["server"])[0]  # сервер прочтёт ту же папку
    # Профиль: `docker compose up` генератор не поднимает, `docker compose run generator` — да.
    assert generator["profiles"] == ["tools"]
    assert "ports" not in generator
    assert generator["build"] == services["server"]["build"]  # тот же образ, что у сервера


def test_the_webapp_restarts_with_the_server_it_shares_the_network_with():
    # README: после генерации — `docker compose restart server`; Vite живёт в сети сервера.
    webapp = compose()["services"]["webapp"]

    assert webapp["network_mode"] == "service:server"
    assert webapp["depends_on"]["server"]["restart"] is True
