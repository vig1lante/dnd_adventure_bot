import asyncio
import copy
import io
import logging
import logging.config
import os
import socket
import sqlite3
from contextlib import closing, suppress
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from dnd_server import __main__ as server_main
from dnd_server.__main__ import LOG_CONFIG, main, mask_query_values
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.bot.bot import hide_token_in_logs
from dnd_server.config import get_settings
from dnd_server.db import session as db_session
from dnd_server.dm import provider as provider_module
from dnd_server.dm.provider import bridge_url, game_provider
from dnd_server.llm.internal_api import TurnTokens


def run_main_with_fake_uvicorn(monkeypatch, *args: str) -> tuple[str, dict]:
    # Реальный сервер в тестах не запускается: подменяем uvicorn.run и забираем
    # аргументы, с которыми main() его вызвал. Порт при этом не занимается (conftest).
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append((app, options)))
    main(list(args))
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

    # Журналы uvicorn глобальны для процесса: здесь их настройку не применяем.
    config = uvicorn.Config(app_path, **{**options, "log_config": None})
    config.load()
    transport = httpx.ASGITransport(app=config.loaded_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "dev"}


# --- тестовый мастер в боевом режиме (спека DND-2 §15, дополнение B11 §1 п. 7) ---

NO_MASTER_WARNING = "тестовый мастер в боевом режиме недоступен: задайте LLM_PROVIDER"
SCENARIO_FILE = Path(__file__).resolve().parent / "scenarios" / "basic_round.yaml"


@pytest.mark.parametrize(
    "environment",
    [
        pytest.param({}, id="LLM_PROVIDER по умолчанию"),
        pytest.param(
            {"LLM_PROVIDER": "fake", "FAKE_LLM_SCENARIO": str(SCENARIO_FILE)},
            id="fake со сценарием",
        ),
    ],
)
def test_prod_with_the_test_master_starts_and_warns_once(monkeypatch, caplog, environment):
    # Сервер владельца не падает: он стартует, пишет одно предупреждение, а игры с `fake`
    # не создаются (422 при создании игры).
    monkeypatch.setenv("APP_ENV", "prod")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="dnd_server"):
        app_path, _options = run_main_with_fake_uvicorn(monkeypatch)

    assert app_path == "dnd_server.app:create_app"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [NO_MASTER_WARNING]
    assert str(SCENARIO_FILE) not in caplog.text


@pytest.mark.parametrize(
    "environment",
    [
        pytest.param({"APP_ENV": "dev"}, id="dev с fake"),
        pytest.param({"APP_ENV": "prod", "LLM_PROVIDER": "anthropic_api"}, id="prod с моделью"),
    ],
)
def test_a_server_with_a_real_or_a_dev_master_starts_without_the_warning(
    monkeypatch, caplog, environment
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="dnd_server"):
        run_main_with_fake_uvicorn(monkeypatch)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# Секреты в окружении сервера: ни одно значение не должно попасть в журнал старта.
STARTUP_SECRETS = {
    "SESSION_SECRET": "startup-session-secret-0123456789-abcdefgh",
    "TELEGRAM_BOT_TOKEN": "123456:startup-bot-token-secret",
    "ANTHROPIC_API_KEY": "sk-ant-startup-secret-key",
    "OPENAI_COMPAT_API_KEY": "sk-compat-startup-secret",
}


def test_without_app_env_the_server_is_prod_and_warns_once_without_secrets_or_paths(
    monkeypatch, caplog
):
    # APP_ENV не задан — это боевой режим (значение по умолчанию). Тестовый мастер со сценарием
    # там недоступен: одно предупреждение, в нём нет ни секретов окружения, ни пути сценария.
    for name, value in {
        **STARTUP_SECRETS,
        "LLM_PROVIDER": "fake",
        "FAKE_LLM_SCENARIO": str(SCENARIO_FILE),
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with caplog.at_level(logging.DEBUG, logger="dnd_server"):
        run_main_with_fake_uvicorn(monkeypatch)

    assert get_settings().app_env == "prod"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [NO_MASTER_WARNING]
    for value in (*STARTUP_SECRETS.values(), str(SCENARIO_FILE), SCENARIO_FILE.name):
        assert value not in caplog.text


def test_dnd_server_console_script_points_to_main():
    [script] = entry_points(group="console_scripts", name="dnd-server")

    assert script.value == "dnd_server.__main__:main"
    assert script.load() is main


# --- один процесс на порту (план B8.s2, мета H4 и B1.security) ---
#
# Блокировки игр, отсчёты и учёт идущих ходов живут в памяти процесса. Вторая копия сервера на той
# же базе выполнила бы `lifespan` (восстановление игр, перевод идущих ходов в `dm_failed`) раньше,
# чем uvicorn привяжет порт, поэтому порт занимается до `uvicorn.run`. Порты в тестах — свободные
# порты самого теста на 127.0.0.1; порт сервера владельца (8000) тесты не трогают (conftest).

LOCALHOST = "127.0.0.1"


def free_port() -> int:
    """Свободный порт 127.0.0.1: система выдаёт его и сразу забирает обратно."""
    with closing(socket.socket()) as probe:
        probe.bind((LOCALHOST, 0))
        return probe.getsockname()[1]


def try_to_bind(port: int) -> bool:
    """Удалось ли «другой программе» занять порт 127.0.0.1 (с SO_REUSEADDR, как у uvicorn)."""
    with closing(socket.socket()) as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            other.bind((LOCALHOST, port))
        except OSError:
            return False
        return True


@pytest.fixture
def busy_port():
    """Порт, который занимает «первая копия сервера»: слушающий сокет самого теста."""
    with closing(socket.socket()) as first:
        first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        first.bind((LOCALHOST, 0))
        first.listen()
        yield first.getsockname()[1]


def test_main_runs_uvicorn_with_one_worker_explicitly(monkeypatch):
    _, options = run_main_with_fake_uvicorn(monkeypatch)

    assert options["workers"] == 1


def test_a_worker_count_from_the_environment_cannot_split_the_server(monkeypatch):
    # WEB_CONCURRENCY разделил бы хаб сокетов, блокировки игр, реестр токенов хода и бота.
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    app_path, options = run_main_with_fake_uvicorn(monkeypatch)

    config = uvicorn.Config(app_path, **{**options, "log_config": None})
    # Контроль: без явного `workers` uvicorn послушался бы переменной.
    options.pop("workers")
    unpinned = uvicorn.Config(app_path, **{**options, "log_config": None})

    assert config.workers == 1
    assert unpinned.workers == 4


def test_main_claims_localhost_port_8000_by_default(monkeypatch, no_real_port_claim):
    run_main_with_fake_uvicorn(monkeypatch)

    assert no_real_port_claim == [(LOCALHOST, 8000)]


def test_the_port_is_overridden_by_one_argument_and_the_host_stays_localhost(
    monkeypatch, no_real_port_claim
):
    _, options = run_main_with_fake_uvicorn(monkeypatch, "--port", "5301")

    assert (options["host"], options["port"]) == (LOCALHOST, 5301)
    assert no_real_port_claim == [(LOCALHOST, 5301)]


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--host", "0.0.0.0"], id="другой адрес"),
        pytest.param(["--host=127.0.0.2"], id="другой адрес через ="),
        pytest.param(["--port", "0"], id="порт 0"),
        pytest.param(["--port", "65536"], id="порт выше предела"),
        pytest.param(["--port", "-1"], id="отрицательный порт"),
        pytest.param(["--port", "http"], id="порт не число"),
        pytest.param(["--port"], id="порт без значения"),
        pytest.param(["--po", "5301"], id="сокращение имени"),
        pytest.param(["--workers", "4"], id="число процессов"),
        pytest.param(["--log-level", "debug"], id="уровень журнала"),
    ],
)
def test_wrong_arguments_stop_main_before_a_port_is_claimed(
    monkeypatch, no_real_port_claim, no_real_database_lock, capsys, args
):
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))

    with pytest.raises(SystemExit) as stop:
        main(args)

    assert stop.value.code == 2  # ошибка командной строки, как у любой программы
    assert capsys.readouterr().err  # причина названа
    assert started == [] and no_real_port_claim == [] and no_real_database_lock == []


@pytest.mark.real_port_claim
def test_claim_port_holds_the_port_against_everyone_else_and_listens():
    port = free_port()

    with server_main.claim_port(LOCALHOST, port) as claimed:
        assert claimed.getsockname() == (LOCALHOST, port)
        assert try_to_bind(port) is False
        # Слушает сразу: вторая копия не пролезла бы и там, где два непрослушиваемых сокета
        # с SO_REUSEADDR делят порт (Linux).
        with closing(socket.create_connection((LOCALHOST, port), timeout=5)):
            pass

    assert try_to_bind(port) is True  # закрытый сокет порт отпустил


@pytest.mark.real_port_claim
def test_a_port_that_has_just_served_connections_is_claimed_again_at_once():
    # Перезапуск сервера идёт сразу за остановкой: закрытые соединения прежнего запуска ещё держат
    # порт (TIME_WAIT), и без SO_REUSEADDR занять его снова не получится.
    port = free_port()
    with server_main.claim_port(LOCALHOST, port) as first:
        client = socket.create_connection((LOCALHOST, port), timeout=5)
        accepted, _ = first.accept()
        accepted.close()  # первым соединение закрывает сервер: TIME_WAIT остаётся за его портом
        client.close()

    with server_main.claim_port(LOCALHOST, port):
        pass


@pytest.mark.real_port_claim
def test_claim_port_of_a_busy_port_stops_the_process_with_a_clear_message(busy_port):
    with pytest.raises(SystemExit) as stop:
        server_main.claim_port(LOCALHOST, busy_port)

    # Текст в `SystemExit` печатается в stderr, а код возврата процесса — 1.
    message = stop.value.code
    assert isinstance(message, str)
    assert f"{LOCALHOST}:{busy_port}" in message and "занят" in message
    assert "--port" in message  # как выбрать другой порт


@pytest.mark.real_port_claim
def test_a_second_copy_stops_before_uvicorn_so_its_lifespan_never_runs(monkeypatch, busy_port):
    # Lifespan (восстановление игр, перевод идущего хода в `dm_failed`) запускает только uvicorn.
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))

    with pytest.raises(SystemExit):
        main(["--port", str(busy_port)])

    assert started == []


@pytest.mark.real_port_claim
def test_the_port_is_already_held_when_uvicorn_starts_and_is_released_after_it(monkeypatch):
    port = free_port()
    seen = {}

    def fake_run(app, **options):
        seen["options"] = options
        seen["other_program_can_bind"] = try_to_bind(port)
        with closing(socket.fromfd(options["fd"], socket.AF_INET, socket.SOCK_STREAM)) as handed:
            seen["handed_over"] = handed.getsockname()

    monkeypatch.setattr(uvicorn, "run", fake_run)

    main(["--port", str(port)])

    assert seen["other_program_can_bind"] is False
    assert seen["handed_over"] == (LOCALHOST, port)  # uvicorn получает занятый сокет, а не гонку
    assert (seen["options"]["host"], seen["options"]["port"]) == (LOCALHOST, port)
    assert try_to_bind(port) is True


@pytest.mark.real_port_claim
async def test_uvicorn_serves_on_the_claimed_socket_and_the_port_is_held_during_lifespan(
    monkeypatch,
):
    port = free_port()
    handed = {}

    def fake_run(app, **options):
        # Сокет main() закроет, когда вернётся; дубликат дескриптора держит тот же сокет.
        handed.update(options, fd=os.dup(options["fd"]))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main(["--port", str(port)])
    port_free_in_lifespan = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    port_free_in_lifespan.append(try_to_bind(port))
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    # Параметры `main()` как есть: приложение и журналы подставляет тест.
    config = uvicorn.Config(
        app, **{**handed, "factory": False, "log_config": None, "access_log": False}
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://{LOCALHOST}:{port}/")
    finally:
        server.should_exit = True
        await serving
        os.close(handed["fd"])

    assert response.status_code == 200 and response.text == "ok"
    assert port_free_in_lifespan == [False]  # порт занят уже в lifespan, а не после него


# --- мост MCP ходит на порт, на котором сервер слушает на самом деле (B8.security.server.fix1) ---
#
# Мост процесса `claude_code_cli` отправляет вызовы инструментов хода на адрес сервера, и в адресе
# лежит одноразовый токен хода. Если `dnd-server --port 8001` оставит мосту порт по умолчанию, токен
# уйдёт на 127.0.0.1:8000: туда, где работает основная копия сервера или любая другая программа.


def seen_by_uvicorn(monkeypatch, read, *args: str):
    """Что возвращает `read()`, когда `main()` запускает uvicorn (сервер «работает»)."""
    seen = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.append(read()))
    main(list(args))
    [value] = seen
    return value


def test_the_bridge_address_stays_on_the_default_port_without_the_port_argument(monkeypatch):
    assert seen_by_uvicorn(monkeypatch, bridge_url) == "http://127.0.0.1:8000"


@pytest.mark.parametrize("port", ["1", "5301", "8001", "65535"])
def test_the_bridge_address_follows_the_port_argument_and_the_host_stays_localhost(
    monkeypatch, port
):
    assert seen_by_uvicorn(monkeypatch, bridge_url, "--port", port) == f"http://127.0.0.1:{port}"


def test_a_provider_of_a_game_gets_the_bridge_address_of_the_running_port(monkeypatch, settings):
    # Все три места, где собирают провайдера игры (проверка при создании, описание героя, ход
    # мастера), идут через `game_provider`, поэтому проверяется он.
    calls = []
    monkeypatch.setattr(provider_module, "make_provider", lambda _settings, **kw: calls.append(kw))

    seen_by_uvicorn(monkeypatch, lambda: game_provider(settings), "--port", "8001")

    assert [call["internal_url"] for call in calls] == ["http://127.0.0.1:8001"]


def test_the_claude_code_cli_provider_sends_the_turn_token_to_the_running_port(
    monkeypatch, settings
):
    def build():
        return game_provider(settings, provider="claude_code_cli", turn_tokens=TurnTokens())

    provider = seen_by_uvicorn(monkeypatch, build, "--port", "8001")

    assert provider._internal_url == "http://127.0.0.1:8001"


def test_a_server_that_could_not_take_its_port_does_not_change_the_bridge_address(monkeypatch):
    # Порт занят чужой копией: этот процесс сервером не стал, и адрес моста остаётся прежним.
    def refuse(host, port):
        raise SystemExit("порт занят")

    monkeypatch.setattr(server_main, "claim_port", refuse)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: None)

    with pytest.raises(SystemExit):
        main(["--port", "8001"])

    assert bridge_url() == "http://127.0.0.1:8000"


# --- одна копия на файл базы (B8.security.server.fix1, решение по B8.security.repo L2) ---
#
# Порт защищает только от копии на том же порту. Вторая копия на другом порту и той же базе
# выполнила бы `lifespan` (восстановление игр, перевод идущего хода в `dm_failed`) и запустила бы
# свои отсчёты поверх первой. Поэтому до uvicorn файловая база блокируется: lock-файл рядом с
# файлом базы, `flock` без ожидания; снимается вместе с процессом. Базы в памяти и не-SQLite не
# блокируются.


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
def use_database(monkeypatch):
    """Ставит DATABASE_URL, который `main()` прочитает из настроек: `use_database(url)`."""

    def use(url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()

    return use


def run_copies_side_by_side(monkeypatch, use_database, first_url, second_url, second_port=5302):
    """Первая копия «работает» (uvicorn.run), и в это время запускается вторая.

    Возвращает порты, с которыми дошёл до uvicorn каждый запуск, и причину, по которой вторая копия
    остановилась (`None`, если она запустилась)."""
    started: list[int] = []
    refusal: list[object] = []

    def fake_run(app, **options):
        started.append(options["port"])
        if len(started) > 1:
            return
        use_database(second_url)
        try:
            main(["--port", str(second_port)])
        except SystemExit as stop:
            refusal.append(stop.code)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    use_database(first_url)
    main(["--port", "5301"])
    return started, (refusal[0] if refusal else None)


@pytest.mark.real_database_lock
def test_a_second_copy_on_the_same_database_stops_before_uvicorn_with_a_clear_message(
    monkeypatch, tmp_path, use_database
):
    base = tmp_path / "data" / "one.sqlite3"

    started, message = run_copies_side_by_side(
        monkeypatch, use_database, sqlite_url(base), sqlite_url(base)
    )

    # До uvicorn вторая копия не дошла, значит не выполнился и lifespan с восстановлением игр.
    assert started == [5301]
    # Текст в `SystemExit` печатается в stderr, а код возврата процесса — 1.
    assert isinstance(message, str)
    assert str(base.resolve()) in message and "dnd-server" in message
    assert "DATABASE_URL" in message  # как запустить рядом: свою базу


@pytest.mark.real_database_lock
def test_the_refusal_names_the_database_file_and_not_the_address_or_the_lock_file(
    monkeypatch, tmp_path, use_database
):
    base = tmp_path / "data" / "one.sqlite3"

    _, message = run_copies_side_by_side(
        monkeypatch, use_database, sqlite_url(base), sqlite_url(base)
    )

    assert sqlite_url(base) not in message and sqlite_url(base.resolve()) not in message
    assert ".lock" not in message


@pytest.mark.real_database_lock
def test_a_copy_on_another_database_starts_beside_the_first(monkeypatch, tmp_path, use_database):
    first, second = tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"

    started, message = run_copies_side_by_side(
        monkeypatch, use_database, sqlite_url(first), sqlite_url(second)
    )

    assert started == [5301, 5302] and message is None


@pytest.mark.real_database_lock
def test_the_database_is_free_again_when_the_first_copy_has_exited(
    monkeypatch, tmp_path, use_database
):
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(options["port"]))
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    main(["--port", "5301"])
    main(["--port", "5302"])  # перезапуск e2e и обычный перезапуск владельца

    assert started == [5301, 5302]


@pytest.mark.real_database_lock
def test_the_database_is_free_again_when_the_first_copy_has_crashed(
    monkeypatch, tmp_path, use_database
):
    started = []

    def crash(app, **options):
        raise RuntimeError("приложение не собралось")

    use_database(sqlite_url(tmp_path / "one.sqlite3"))
    monkeypatch.setattr(uvicorn, "run", crash)
    with pytest.raises(RuntimeError):
        main(["--port", "5301"])
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(options["port"]))

    main(["--port", "5302"])

    assert started == [5302]


@pytest.mark.real_database_lock
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("sqlite+aiosqlite:///:memory:", id="SQLite в памяти"),
        pytest.param("sqlite+aiosqlite://", id="SQLite в памяти без имени"),
        pytest.param("postgresql+asyncpg://dnd:pw@db.example/dnd", id="другая СУБД"),
    ],
)
def test_databases_without_a_file_are_not_locked(monkeypatch, tmp_path, use_database, url):
    started, message = run_copies_side_by_side(monkeypatch, use_database, url, url)

    assert started == [5301, 5302] and message is None
    assert list(tmp_path.iterdir()) == []  # нигде не появилось ни файла, ни папки


@pytest.mark.real_database_lock
def test_the_lock_file_lies_next_to_the_database_and_is_left_in_place_at_the_exit(
    monkeypatch, tmp_path, use_database
):
    base = tmp_path / "fresh" / "dnd.sqlite3"  # свежая установка: папки базы ещё нет
    lock = base.parent / "dnd.sqlite3.lock"
    seen = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **options: seen.append((lock.is_file(), base.exists()))
    )
    use_database(sqlite_url(base))

    main(["--port", "5301"])

    # Сама база не создана: её заводит приложение, и по её отсутствию оно знает, что игр нет.
    assert seen == [(True, False)]
    # Файл остаётся: удалённый и созданный заново, он стал бы другим файлом, и две копии удержали
    # бы по «своему» замку одной базы.
    assert lock.is_file()


# Lock-файл (спека DND-2 §15, дополнение B11 §1 п. 15): не идёт по символической ссылке и закрыт от
# чужих глаз — права 0600 у нового файла и у прежнего файла своего пользователя.


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.real_database_lock
def test_a_new_lock_file_is_readable_by_its_owner_only(monkeypatch, tmp_path, use_database):
    previous = os.umask(0o022)  # обычный umask дал бы 0644
    try:
        monkeypatch.setattr(uvicorn, "run", lambda app, **options: None)
        use_database(sqlite_url(tmp_path / "one.sqlite3"))

        main(["--port", "5301"])
    finally:
        os.umask(previous)

    assert mode_of(tmp_path / "one.sqlite3.lock") == 0o600


@pytest.mark.real_database_lock
def test_an_old_lock_file_of_the_owner_gets_owner_only_rights_and_stays_in_place(
    monkeypatch, tmp_path, use_database
):
    lock = tmp_path / "one.sqlite3.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    inode = lock.stat().st_ino
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(mode_of(lock)))
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    main(["--port", "5301"])

    assert started == [0o600]
    assert lock.stat().st_ino == inode  # тот же файл: его не пересоздавали


@pytest.mark.real_database_lock
def test_a_hard_link_in_place_of_the_lock_file_keeps_the_rights_of_the_linked_file(
    monkeypatch, tmp_path, use_database, caplog
):
    # Дополнение B11 §5 п. 5 (S2): `O_NOFOLLOW` жёсткую ссылку не видит, а `fchmod` сменил бы права
    # связанного файла (скрипт 0755 стал бы 0600). Права меняются только у обычного файла с одной
    # ссылкой; иначе — предупреждение, файл и его права как были.
    script = tmp_path / "elsewhere" / "script.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    os.link(script, tmp_path / "one.sqlite3.lock")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    monkeypatch.setenv("APP_ENV", "dev")  # без предупреждения о тестовом мастере
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    with caplog.at_level(logging.WARNING, logger="dnd_server"):
        main(["--port", "5301"])

    assert started  # замок взят, сервер запущен
    assert mode_of(script) == 0o755
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\n"
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "ссылк" in warning.getMessage()
    assert str(tmp_path / "one.sqlite3") in warning.getMessage()
    assert str(script) not in warning.getMessage()


@pytest.mark.real_database_lock
def test_a_lock_file_of_another_user_is_kept_as_it_is_with_a_warning(
    monkeypatch, tmp_path, use_database, caplog
):
    lock = tmp_path / "one.sqlite3.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    monkeypatch.setattr(server_main.os, "getuid", lambda: lock.stat().st_uid + 1)
    monkeypatch.setenv("APP_ENV", "dev")  # без предупреждения о тестовом мастере
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    with caplog.at_level(logging.WARNING, logger="dnd_server"):
        main(["--port", "5301"])

    assert started and mode_of(lock) == 0o644
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "другому пользователю" in warning.getMessage()
    assert str(tmp_path / "one.sqlite3") in warning.getMessage()
    # Названа база, а не lock-файл: его путь в сообщения не попадает (как и в отказе «занято»).
    assert ".lock" not in warning.getMessage()


@pytest.mark.real_database_lock
@pytest.mark.parametrize("target_exists", [True, False], ids=["на чужой файл", "в никуда"])
def test_a_symlink_in_place_of_the_lock_file_stops_the_server_and_touches_nothing(
    monkeypatch, tmp_path, use_database, target_exists
):
    target = tmp_path / "elsewhere" / "victim.txt"
    target.parent.mkdir()
    if target_exists:
        target.write_text("чужие данные", encoding="utf-8")
        target.chmod(0o644)
    (tmp_path / "one.sqlite3.lock").symlink_to(target)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    with pytest.raises(SystemExit) as stop:
        main(["--port", "5301"])

    assert started == []
    message = stop.value.code
    assert isinstance(message, str)
    assert "символическая ссылка" in message and str(tmp_path / "one.sqlite3") in message
    assert str(target) not in message
    assert ".lock" not in message  # названа база, а не lock-файл
    if target_exists:
        assert target.read_text(encoding="utf-8") == "чужие данные" and mode_of(target) == 0o644
    else:
        assert not target.exists()


# --- ревизия базы при старте (спека DND-2 §15, дополнение B11 §1 п. 8) ---
#
# Новый код на старой базе падал бы непонятными ошибками запросов. Поэтому до uvicorn сервер сверяет
# ревизию файла базы с `head` миграций: базы нет или она не на `head` — запуск останавливается с
# командой, которая это исправит. Адреса `DATABASE_URL` в тексте нет.

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
UPGRADE = "uv run alembic -c services/server/alembic.ini upgrade head"
HEAD = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()


def migrated_to(monkeypatch, path: Path, revision: str) -> None:
    """База теста, поднятая настоящими миграциями до `revision`."""
    monkeypatch.setenv("DATABASE_URL", sqlite_url(path))
    get_settings.cache_clear()
    command.upgrade(Config(str(ALEMBIC_INI)), revision)
    get_settings.cache_clear()


def started_or_stopped(monkeypatch, url: str) -> tuple[list[str], object]:
    """Дошёл ли `main()` до uvicorn и с какой причиной остановился (`None` — запустился)."""
    started: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        main(["--port", "5301"])
    except SystemExit as stop:
        return started, stop.code
    return started, None


@pytest.mark.real_revision_check
def test_a_database_at_the_head_revision_starts_the_server(monkeypatch, tmp_path):
    base = tmp_path / "data" / "dnd.sqlite3"
    migrated_to(monkeypatch, base, "head")

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert started == ["dnd_server.app:create_app"] and refusal is None


@pytest.mark.real_revision_check
def test_an_older_database_stops_the_server_with_the_command_that_updates_it(monkeypatch, tmp_path):
    base = tmp_path / "data" / "dnd.sqlite3"
    migrated_to(monkeypatch, base, "0006")
    before = base.read_bytes()

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert started == []
    assert isinstance(refusal, str)
    assert str(base) in refusal and "0006" in refusal and HEAD in refusal
    assert UPGRADE in refusal
    assert "DATABASE_URL" not in refusal and "sqlite+aiosqlite" not in refusal
    assert base.read_bytes() == before  # проверка только читает базу


@pytest.mark.real_revision_check
def test_a_missing_database_stops_the_server_and_is_not_created(monkeypatch, tmp_path):
    base = tmp_path / "data" / "dnd.sqlite3"

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert started == []
    assert isinstance(refusal, str)
    assert str(base) in refusal and UPGRADE in refusal
    assert "DATABASE_URL" not in refusal
    assert not base.exists()


@pytest.mark.real_revision_check
def test_a_database_written_by_a_newer_server_stops_this_one(monkeypatch, tmp_path):
    base = tmp_path / "data" / "dnd.sqlite3"
    migrated_to(monkeypatch, base, "head")
    with closing(sqlite3.connect(base)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
        connection.commit()

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert started == []
    assert isinstance(refusal, str)
    assert "9999" in refusal and "обновите сервер" in refusal
    assert UPGRADE not in refusal  # миграции тут не помогут


@pytest.mark.real_revision_check
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("sqlite+aiosqlite:///:memory:", id="SQLite в памяти"),
        pytest.param("postgresql+asyncpg://dnd:pw@db.example/dnd", id="другая СУБД"),
    ],
)
def test_databases_without_a_file_are_not_checked(monkeypatch, tmp_path, url):
    started, refusal = started_or_stopped(monkeypatch, url)

    assert started and refusal is None
    assert list(tmp_path.iterdir()) == []


def test_main_checks_the_revision_of_the_database_it_will_open(monkeypatch, no_real_revision_check):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/another.sqlite3")
    get_settings.cache_clear()

    run_main_with_fake_uvicorn(monkeypatch)

    assert no_real_revision_check == ["sqlite+aiosqlite:///./data/another.sqlite3"]


def files_of(folder: Path) -> dict[str, tuple[int, int]]:
    """Файлы папки базы: имя -> (размер, время изменения в наносекундах)."""
    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.iterdir()}


@pytest.mark.real_revision_check
@pytest.mark.parametrize("kind", ["пустой файл", "пустая таблица ревизии"])
def test_a_database_without_a_recorded_revision_stops_the_server_with_the_command(
    monkeypatch, tmp_path, kind
):
    # Третий вид «база старее кода»: файл есть, а ревизии в нём нет (пустой файл, запись ревизии
    # стёрта). Отказ называет файл, нужную ревизию и команду; адреса DATABASE_URL в нём нет.
    base = tmp_path / "data" / "dnd.sqlite3"
    if kind == "пустой файл":
        base.parent.mkdir()
        base.write_bytes(b"")
    else:
        migrated_to(monkeypatch, base, "head")
        with closing(sqlite3.connect(base)) as connection:
            connection.execute("DELETE FROM alembic_version")
            connection.commit()
    before = files_of(base.parent)

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert started == []
    assert isinstance(refusal, str)
    assert "не записана" in refusal and HEAD in refusal and str(base) in refusal
    assert UPGRADE in refusal
    assert "DATABASE_URL" not in refusal and "sqlite+aiosqlite" not in refusal
    assert files_of(base.parent) == before


@pytest.mark.real_revision_check
@pytest.mark.parametrize(
    "recorded", ["head", "0006", "9999"], ids=["на head", "старее", "новее кода"]
)
def test_the_revision_check_only_reads_the_database_and_leaves_nothing_next_to_it(
    monkeypatch, tmp_path, recorded
):
    # Ревизия читается адресом `mode=ro`: файл базы не меняется, журналов SQLite (`-journal`,
    # `-wal`, `-shm`) рядом не появляется — ни при запуске, ни при любом отказе.
    base = tmp_path / "data" / "dnd.sqlite3"
    migrated_to(monkeypatch, base, "0006" if recorded == "0006" else "head")
    if recorded == "9999":
        with closing(sqlite3.connect(base)) as connection:
            connection.execute("UPDATE alembic_version SET version_num = '9999'")
            connection.commit()
    before = files_of(base.parent)

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(base))

    assert files_of(base.parent) == before
    assert bool(started) is (recorded == "head")
    if refusal is not None:
        assert str(base) in refusal
        assert "DATABASE_URL" not in refusal and "sqlite+aiosqlite" not in refusal


def open_descriptors_of(path: Path) -> list[int]:
    """Дескрипторы этого процесса, открытые на файл `path` (сверка по устройству и номеру файла)."""
    target = os.stat(path)
    found = []
    for descriptor in range(3, 256):
        try:
            opened = os.fstat(descriptor)
        except OSError:
            continue
        if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
            found.append(descriptor)
    return found


@pytest.mark.real_database_lock
def test_the_lock_is_held_while_the_server_runs_and_is_not_inherited_by_child_processes(
    monkeypatch, tmp_path, use_database
):
    lock = tmp_path / "one.sqlite3.lock"
    held = {}

    def fake_run(app, **options):
        descriptors = open_descriptors_of(lock)
        held["descriptors"] = descriptors
        held["inheritable"] = [os.get_inheritable(descriptor) for descriptor in descriptors]

    monkeypatch.setattr(uvicorn, "run", fake_run)
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    main(["--port", "5301"])

    # Пока сервер работает, замок открыт; процесс `claude`, запущенный ходом мастера, его не
    # получает и не удержит замок после остановки сервера.
    assert held["descriptors"] and not any(held["inheritable"])
    assert open_descriptors_of(lock) == []  # после выхода дескриптора нет


@pytest.mark.real_database_lock
def test_a_database_path_inside_the_webapp_folder_is_refused_before_a_lock_file_appears(
    monkeypatch, tmp_path, use_database
):
    # Эту папку раздаёт Vite: файл базы, а с ним и lock-файл, лежали бы на виду.
    monkeypatch.setattr(db_session, "WEBAPP_DIR", tmp_path / "webapp")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    use_database(sqlite_url(tmp_path / "webapp" / "data" / "dnd.sqlite3"))

    with pytest.raises(db_session.DatabasePathError):
        main([])

    assert started == [] and not (tmp_path / "webapp").exists()


@pytest.mark.real_database_lock
@pytest.mark.real_port_claim
def test_the_same_port_and_the_same_database_report_the_busy_port_first(
    tmp_path, use_database, busy_port
):
    # Сквозной тест e2e «вторая копия на занятом порту» ждёт именно это сообщение.
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    with (
        server_main.claim_database(sqlite_url(tmp_path / "one.sqlite3")),
        pytest.raises(SystemExit) as stop,
    ):
        main(["--port", str(busy_port)])

    message = stop.value.code
    assert isinstance(message, str)
    assert f"Порт {LOCALHOST}:{busy_port} уже занят" in message


@pytest.mark.real_database_lock
@pytest.mark.real_port_claim
def test_a_copy_refused_for_the_database_gives_its_port_back_and_keeps_the_bridge_address(
    monkeypatch, tmp_path, use_database
):
    port = free_port()
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.append(app))
    use_database(sqlite_url(tmp_path / "one.sqlite3"))

    with (
        server_main.claim_database(sqlite_url(tmp_path / "one.sqlite3")),
        pytest.raises(SystemExit),
    ):
        main(["--port", str(port)])

    assert started == []
    assert try_to_bind(port) is True  # порт занят не остался
    assert bridge_url() == "http://127.0.0.1:8000"  # этот процесс сервером не стал


def test_main_asks_to_lock_the_database_of_the_settings(
    monkeypatch, no_real_database_lock, use_database
):
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: None)

    main([])
    use_database("sqlite+aiosqlite:///./data/other.sqlite3")
    main([])

    assert no_real_database_lock == [
        "sqlite+aiosqlite:///./data/dnd.sqlite3",
        "sqlite+aiosqlite:///./data/other.sqlite3",
    ]


# --- токен сессии WebSocket не попадает в журналы (план B6.5) ---
#
# Браузер не умеет слать заголовки при открытии сокета, поэтому токен сессии идёт в query:
# `/ws/games/<id>?token=…`. uvicorn пишет путь вместе с query в строку рукопожатия
# (`uvicorn.error`), а при `--log-level debug/trace` — ещё и в строки websockets и в repr scope
# (`uvicorn.asgi`). Проверяется настоящая настройка журналов, которую main() передаёт uvicorn.

# Журналы, которые настраивает LOG_CONFIG: uvicorn, приложение и aiogram.
CONFIGURED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
    "dnd_server",
    "aiogram",
)
TOKEN_FIELD = "token="


@pytest.mark.parametrize(
    ("line", "masked"),
    [
        pytest.param(
            '127.0.0.1:5 - "WebSocket /ws/games/g1?token=abc.DEF_ghi-1" [accepted]',
            '127.0.0.1:5 - "WebSocket /ws/games/g1?token=***" [accepted]',
            id="строка рукопожатия",
        ),
        pytest.param(
            "< GET /ws/games/g1?token=abc.DEF HTTP/1.1",
            "< GET /ws/games/g1?token=*** HTTP/1.1",
            id="отладочная строка websockets",
        ),
        pytest.param(
            "Started scope={'path': '/ws/games/g1', 'query_string': b'token=abc.DEF&x=1'}",
            "Started scope={'path': '/ws/games/g1', 'query_string': b'***'}",
            id="repr scope в trace",
        ),
        pytest.param(
            "/ws/games/g1?x=1&token=abc",
            "/ws/games/g1?x=***&token=***",
            id="несколько параметров",
        ),
        pytest.param(
            "/ws/games/g1?t%6Fken=abc",
            "/ws/games/g1?t%6Fken=***",
            id="имя параметра в процентной кодировке",
        ),
        pytest.param(
            "Сервер запущен: http://127.0.0.1:8000",
            "Сервер запущен: http://127.0.0.1:8000",
            id="обычная строка не меняется",
        ),
    ],
)
def test_mask_query_values_hides_every_query_value(line, masked):
    assert mask_query_values(line) == masked


def test_log_formatters_mask_the_final_line_of_both_uvicorn_handlers(monkeypatch):
    _, options = run_main_with_fake_uvicorn(monkeypatch)
    formatters = copy.deepcopy(options["log_config"]["formatters"])
    default = formatters["default"]
    access = formatters["access"]
    default_formatter = default.pop("()")(**default)
    access_formatter = access.pop("()")(**access)
    handshake = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:5", "/ws/games/g1?token=secret-token"),
        None,
    )
    request = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/ws/games/g1?token=secret-token", "1.1", 101),
        None,
    )

    assert "secret-token" not in default_formatter.format(handshake)
    assert "secret-token" not in access_formatter.format(request)
    assert "/ws/games/g1?token=***" in default_formatter.format(handshake)


@pytest.fixture
def uvicorn_loggers_restored():
    """Настройка журналов глобальна для процесса: после теста они возвращаются как были."""
    loggers = [logging.getLogger(name) for name in CONFIGURED_LOGGERS]
    saved = [(lg, lg.handlers[:], lg.propagate, lg.level, lg.disabled) for lg in loggers]
    yield
    for logger, handlers, propagate, level, disabled in saved:
        logger.handlers, logger.propagate, logger.disabled = handlers, propagate, disabled
        logger.setLevel(level)


def written_to(stream: io.StringIO, log_config: dict) -> dict:
    """Копия настройки журналов, где все обработчики пишут в память, а не в консоль теста."""
    config = copy.deepcopy(log_config)
    for handler in config["handlers"].values():
        handler["stream"] = stream
    return config


async def log_of_handshake(settings, options, log_config, token: str) -> str:
    """Всё, что настоящий сервер uvicorn записал в журнал при рукопожатии сокета с токеном."""
    stream = io.StringIO()
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        **{
            **options,
            "factory": False,
            "fd": None,  # порт main() здесь не нужен: сервер теста берёт свободный сам
            "port": 0,
            "lifespan": "off",
            "log_level": "trace",
            "log_config": written_to(stream, log_config),
        },
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        # Клиент может получить отказ или закрытие: журнал рукопожатия пишется в любом случае.
        with suppress(WebSocketException):
            async with connect(f"ws://127.0.0.1:{port}/ws/games/g-1?{TOKEN_FIELD}{token}") as ws:
                await asyncio.wait_for(ws.recv(), 5)
    finally:
        server.should_exit = True
        await task
        await app.state.engine.dispose()
    return stream.getvalue()


async def test_session_token_from_ws_query_never_reaches_the_real_uvicorn_log(
    monkeypatch, settings, db, uvicorn_loggers_restored
):
    _, options = run_main_with_fake_uvicorn(monkeypatch)
    token = SessionSigner.from_settings(settings).issue("user-with-a-long-id")

    log = await log_of_handshake(settings, options, options["log_config"], token)

    # Журнал не пуст: строка рукопожатия и записи trace на месте, а токена в них нет.
    assert '"WebSocket /ws/games/g-1?token=***"' in log
    assert "Started scope" in log
    assert token not in log
    assert all(part not in log for part in token.split("."))


async def test_unmasked_uvicorn_log_would_leak_the_token(
    monkeypatch, settings, db, uvicorn_loggers_restored
):
    # Контроль: тот же стенд со стандартной настройкой uvicorn токен выдаёт. Без этого
    # предыдущий тест мог бы проходить просто потому, что стенд ничего не записывает.
    _, options = run_main_with_fake_uvicorn(monkeypatch)
    token = SessionSigner.from_settings(settings).issue("user-with-a-long-id")

    log = await log_of_handshake(settings, options, LOGGING_CONFIG, token)

    assert token in log


# --- журналы приложения и aiogram выходят в тот же поток, что и журналы uvicorn ---
#
# LOG_CONFIG копирует настройку uvicorn, где есть только журналы `uvicorn.*`. Без остального записи
# `dnd_server.*` уровня INFO теряются, а WARNING и ERROR печатает `logging.lastResort` голым
# текстом без уровня и имени журнала.

BOT_TOKEN = "123456789:AAEhBP0av28-secret-part-of-the-token"


@pytest.fixture
def server_log(uvicorn_loggers_restored) -> io.StringIO:
    """Настоящий LOG_CONFIG применён к процессу, а вывод идёт в память: читать `getvalue()`."""
    stream = io.StringIO()
    logging.config.dictConfig(written_to(stream, LOG_CONFIG))
    return stream


@pytest.mark.parametrize(
    ("level", "shown"),
    [(logging.INFO, "INFO"), (logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")],
)
def test_application_log_records_come_out_with_the_level_and_the_logger_name(
    server_log, level, shown
):
    logging.getLogger("dnd_server.adventures").log(
        level, "Папка приключения пропущена: %s", "crypt"
    )

    line = server_log.getvalue()
    assert shown in line
    assert "dnd_server.adventures" in line
    assert "Папка приключения пропущена: crypt" in line


def test_application_debug_records_are_not_written(server_log):
    logging.getLogger("dnd_server.realtime.ws").debug("Сокет закрыт")

    assert server_log.getvalue() == ""


def test_aiogram_warnings_are_written_but_routine_info_is_not(server_log):
    # На INFO aiogram пишет строку на каждое обработанное обновление: для игры на шестерых это шум.
    logging.getLogger("aiogram.dispatcher").info("Update id=1 is handled")
    logging.getLogger("aiogram.dispatcher").warning("Sleep for 1.0 seconds and try again")

    line = server_log.getvalue()
    assert "Update id=1" not in line
    assert "WARNING" in line and "aiogram.dispatcher" in line


def test_application_log_lines_mask_the_query_values_too(server_log):
    logging.getLogger("dnd_server.realtime.ws").warning(
        "Сокет не принял событие: /ws/games/g1?token=%s", "abc.DEF-session"
    )

    line = server_log.getvalue()
    assert "/ws/games/g1?token=***" in line
    assert "abc.DEF-session" not in line


def test_application_records_are_written_once(server_log):
    logging.getLogger("dnd_server.games.service").warning("Один раз")

    assert server_log.getvalue().count("Один раз") == 1


@pytest.mark.real_logging_setup
@pytest.mark.real_database_lock
def test_warnings_of_the_start_come_out_with_their_level_and_the_masking_stays(
    monkeypatch, tmp_path, use_database, uvicorn_loggers_restored
):
    # Дополнение B11 §7 п. 3 (N1): предупреждения старта (тестовый мастер в prod, жёсткая ссылка
    # на месте lock-файла) идут через настроенный журнал — с меткой WARNING и именем журнала, как
    # обещает README, а не голым текстом `logging.lastResort`. Настройка та же, что у uvicorn.
    stream = io.StringIO()
    monkeypatch.setattr(server_main, "LOG_CONFIG", written_to(stream, LOG_CONFIG))
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.link(script, tmp_path / "one.sqlite3.lock")
    handed: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: handed.append(options["log_config"]))
    use_database(sqlite_url(tmp_path / "one.sqlite3"))  # APP_ENV не задан: боевой режим

    main(["--port", "5301"])
    logging.getLogger("dnd_server.realtime.ws").warning("Сокет: /ws/games/g1?token=%s", "abc.DEF")

    lines = stream.getvalue().splitlines()
    master = [line for line in lines if NO_MASTER_WARNING in line]
    lock = [line for line in lines if "Файл блокировки базы" in line]
    assert len(master) == len(lock) == 1
    for line in (*master, *lock):
        assert line.startswith("WARNING:") and "dnd_server.main:" in line
    assert "abc.DEF" not in stream.getvalue() and "token=***" in stream.getvalue()
    assert handed == [server_main.LOG_CONFIG]  # uvicorn получает ту же настройку


def test_main_sets_up_the_logs_before_the_checks_of_the_start(monkeypatch, no_real_logging_setup):
    order: list[str] = []
    monkeypatch.setattr(server_main, "configure_logging", lambda: order.append("журналы"))
    monkeypatch.setattr(
        server_main, "warn_without_master", lambda _settings: order.append("мастер")
    )

    run_main_with_fake_uvicorn(monkeypatch)

    assert order == ["журналы", "мастер"]


def test_the_bot_token_filter_still_hides_the_token_in_the_configured_logs(server_log):
    token_part = BOT_TOKEN.partition(":")[2]
    with hide_token_in_logs(BOT_TOKEN):
        logging.getLogger("aiogram.dispatcher").error(
            "Failed to fetch updates: https://api.telegram.org/bot%s/getUpdates", BOT_TOKEN
        )
        try:
            raise RuntimeError(f"прокси недоступен: /bot{BOT_TOKEN}/sendMessage")
        except RuntimeError:
            logging.getLogger("dnd_server.bot.bot").exception("Telegram-бот остановился")

    log = server_log.getvalue()
    assert "aiogram.dispatcher" in log and "dnd_server.bot.bot" in log
    assert "***" in log
    assert BOT_TOKEN not in log and token_part not in log
    assert "Traceback" in log


@pytest.mark.real_revision_check
@pytest.mark.parametrize("folder", ["папка с пробелом", "data#1", "50%"], ids=["пробел", "#", "%"])
def test_the_revision_is_read_from_a_database_whose_path_has_special_characters(
    monkeypatch, tmp_path, folder
):
    # Адрес `file:…?mode=ro` строится из пути: пробел, `#` и `%` в папке базы не ломают чтение
    # ревизии и не подменяют параметры адреса (база на head запускает сервер, старая — нет).
    # `?` в пути здесь не проверяется: в DATABASE_URL он начинает параметры адреса SQLAlchemy.
    current, older = tmp_path / folder / "dnd.sqlite3", tmp_path / folder / "old.sqlite3"
    migrated_to(monkeypatch, current, "head")
    migrated_to(monkeypatch, older, "0006")

    started, refusal = started_or_stopped(monkeypatch, sqlite_url(current))
    stopped, old_refusal = started_or_stopped(monkeypatch, sqlite_url(older))

    assert started and refusal is None
    assert stopped == [] and isinstance(old_refusal, str) and "0006" in old_refusal
