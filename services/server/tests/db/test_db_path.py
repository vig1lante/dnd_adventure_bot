"""Где лежит файл SQLite (план B6.1, находка B6.explain F3).

Относительный путь из `DATABASE_URL` считается от корня репозитория, а не от текущего каталога:
иначе сервер, запущенный из `services/server` или `services/webapp`, завёл бы свою пустую базу.
Файл внутри `services/webapp` запрещён: эту папку раздаёт Vite. Проверки идут без обращения к
файловой системе, кроме тех, где она и есть предмет теста; базу владельца тесты не открывают.
"""

import sqlite3
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import make_url

from dnd_server.app import create_app
from dnd_server.config import Settings
from dnd_server.db import session as db_session
from dnd_server.db.session import (
    DatabasePathError,
    create_database_directory,
    resolve_database_url,
)

# tests/db/test_db_path.py -> db -> tests -> server -> services -> корень
REPO_ROOT = Path(__file__).resolve().parents[4]
WEBAPP_DIR = REPO_ROOT / "services" / "webapp"
DEFAULT_URL = str(Settings.model_fields["database_url"].default)
OWNER_DATABASE = REPO_ROOT / "data" / "dnd.sqlite3"
# Откуда запускают сервер: корень, каталог сервиса сервера, каталог webapp.
LAUNCH_DIRECTORIES = ["", "services/server", "services/webapp"]


def sqlite_url(path: Path | str) -> str:
    return f"sqlite+aiosqlite:///{path}"


# --- корень репозитория ---


def test_repo_root_is_the_uv_workspace_root():
    assert db_session.REPO_ROOT == REPO_ROOT
    assert (REPO_ROOT / "services" / "server" / "pyproject.toml").is_file()


# --- разрешение пути ---


@pytest.mark.parametrize("launch_directory", LAUNCH_DIRECTORIES)
def test_default_url_points_to_the_root_data_folder_from_any_launch_directory(
    monkeypatch, launch_directory
):
    monkeypatch.chdir(REPO_ROOT / launch_directory)

    url = resolve_database_url(DEFAULT_URL)

    assert url.database == str(OWNER_DATABASE)
    assert url.drivername == "sqlite+aiosqlite"


@pytest.mark.parametrize("launch_directory", LAUNCH_DIRECTORIES)
def test_app_built_in_any_launch_directory_uses_the_root_database(monkeypatch, launch_directory):
    monkeypatch.chdir(REPO_ROOT / launch_directory)

    app = create_app(Settings(database_url=DEFAULT_URL))

    assert app.state.engine.url.database == str(OWNER_DATABASE)


def test_relative_path_is_taken_from_the_repo_root_not_from_the_launch_directory(monkeypatch):
    monkeypatch.chdir(WEBAPP_DIR)

    url = resolve_database_url(sqlite_url("./dnd.sqlite3"))

    assert url.database == str(REPO_ROOT / "dnd.sqlite3")


def test_dots_in_the_path_are_resolved_before_the_checks(tmp_path):
    messy = tmp_path / "a" / ".." / "data" / "x.sqlite3"

    assert resolve_database_url(sqlite_url(messy)).database == str(tmp_path / "data" / "x.sqlite3")


def test_absolute_path_outside_the_repo_is_kept(tmp_path):
    path = tmp_path / "somewhere" / "dnd.sqlite3"

    assert resolve_database_url(sqlite_url(path)).database == str(path)


def test_other_options_of_the_url_survive_resolution(tmp_path):
    raw = sqlite_url(tmp_path / "x.sqlite3") + "?timeout=30"

    assert resolve_database_url(raw).query == {"timeout": "30"}


@pytest.mark.parametrize(
    "raw",
    [
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://user:pw@db.example/dnd",
    ],
    ids=["память", "память явно", "не SQLite"],
)
def test_databases_without_a_file_are_left_alone(raw):
    assert resolve_database_url(raw) == make_url(raw)


# --- корень репозитория не найден (пакет установлен без режима разработки) ---


@pytest.fixture
def installed_without_repo(tmp_path, monkeypatch) -> Path:
    """`REPO_ROOT` указывает туда, где нет файлов репозитория, как у пакета в `.venv`."""
    root = tmp_path / ".venv"
    root.mkdir()
    monkeypatch.setattr(db_session, "REPO_ROOT", root)
    return root


def test_relative_path_is_refused_when_the_repo_root_is_not_found(installed_without_repo):
    with pytest.raises(DatabasePathError, match="абсолютный путь"):
        resolve_database_url(DEFAULT_URL)


def test_app_refuses_to_start_when_the_relative_database_has_no_repo_root(installed_without_repo):
    with pytest.raises(DatabasePathError, match="абсолютный путь"):
        create_app(Settings(database_url=DEFAULT_URL))


def test_absolute_path_is_accepted_when_the_repo_root_is_not_found(
    installed_without_repo, tmp_path
):
    path = tmp_path / "data" / "dnd.sqlite3"

    assert resolve_database_url(sqlite_url(path)).database == str(path)


def test_database_without_a_file_does_not_need_the_repo_root(installed_without_repo):
    assert resolve_database_url("sqlite+aiosqlite://") == make_url("sqlite+aiosqlite://")


def test_relative_path_is_counted_from_a_root_that_has_the_repo_marker(installed_without_repo):
    marker = installed_without_repo / "services" / "server" / "alembic.ini"
    marker.parent.mkdir(parents=True)
    marker.touch()

    url = resolve_database_url(DEFAULT_URL)

    assert url.database == str(installed_without_repo / "data" / "dnd.sqlite3")


# --- запрет файла внутри webapp ---


@pytest.mark.parametrize("launch_directory", LAUNCH_DIRECTORIES)
@pytest.mark.parametrize(
    "path",
    [
        "./services/webapp/dnd.sqlite3",
        "./services/server/../webapp/public/dnd.sqlite3",
        str(WEBAPP_DIR / "dnd.sqlite3"),
        str(WEBAPP_DIR / "node_modules" / ".." / "dnd.sqlite3"),
    ],
    ids=["относительный", "через ..", "абсолютный", "абсолютный через .."],
)
def test_database_inside_webapp_is_refused(monkeypatch, launch_directory, path):
    monkeypatch.chdir(REPO_ROOT / launch_directory)

    with pytest.raises(DatabasePathError, match="раздаёт Vite"):
        resolve_database_url(sqlite_url(path))


def test_app_refuses_to_start_with_the_database_inside_webapp():
    settings = Settings(database_url=sqlite_url(WEBAPP_DIR / "dnd.sqlite3"))

    with pytest.raises(DatabasePathError, match="раздаёт Vite"):
        create_app(settings)


def test_refusal_names_the_path_but_not_the_whole_url():
    url = sqlite_url(WEBAPP_DIR / "dnd.sqlite3") + "?timeout=30"

    with pytest.raises(DatabasePathError) as refusal:
        resolve_database_url(url)

    assert str(WEBAPP_DIR / "dnd.sqlite3") in str(refusal.value)
    assert "timeout" not in str(refusal.value)


def test_folder_next_to_webapp_with_a_similar_name_is_allowed():
    path = REPO_ROOT / "services" / "webapp-backup" / "dnd.sqlite3"

    assert resolve_database_url(sqlite_url(path)).database == str(path)


# --- папка для файла ---


def test_missing_folders_of_the_database_file_are_created(tmp_path):
    path = tmp_path / "data" / "nested" / "dnd.sqlite3"

    create_database_directory(resolve_database_url(sqlite_url(path)))

    assert path.parent.is_dir()
    assert not path.exists()  # файл создаст сама СУБД при первом подключении


def test_existing_folder_is_not_an_error(tmp_path):
    create_database_directory(resolve_database_url(sqlite_url(tmp_path / "dnd.sqlite3")))


def test_in_memory_database_needs_no_folder(tmp_path):
    create_database_directory(resolve_database_url("sqlite+aiosqlite://"))

    assert list(tmp_path.iterdir()) == []


# --- тесты не открывают базу владельца ---


def test_opening_the_owner_database_from_a_test_is_refused_and_recorded(live_call_attempts):
    with pytest.raises(AssertionError, match="базу владельца"):
        sqlite3.connect(OWNER_DATABASE)

    assert len(live_call_attempts) == 1
    live_call_attempts.clear()


async def test_the_app_cannot_reach_the_owner_database_through_its_own_requests(
    live_call_attempts,
):
    # Приложение на настройках по умолчанию смотрит в `<корень>/data/`; запрос, которому нужна
    # база, доходит до `sqlite3.connect` (через aiosqlite) и упирается в запрет. Старт приложения
    # (`lifespan`) здесь не нужен и не запускается: он создал бы папку `data/` владельца.
    existed = OWNER_DATABASE.parent.exists()
    app = create_app(Settings(app_env="dev", database_url=DEFAULT_URL))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        with pytest.raises(AssertionError, match="базу владельца"):
            await http.post("/api/auth/dev", json={"login": "dev-1"})
    await app.state.engine.dispose()

    assert any(attempt.startswith("sqlite:") for attempt in live_call_attempts)
    assert OWNER_DATABASE.parent.exists() == existed
    assert not OWNER_DATABASE.exists()
    live_call_attempts.clear()


def test_opening_a_database_in_a_temporary_folder_is_allowed(tmp_path):
    sqlite3.connect(tmp_path / "x.sqlite3").close()
