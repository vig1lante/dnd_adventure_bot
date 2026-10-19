import asyncio
import copy
import io
import logging
import logging.config
from contextlib import suppress
from importlib.metadata import entry_points

import httpx
import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from dnd_server.__main__ import LOG_CONFIG, main, mask_query_values
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.bot.bot import hide_token_in_logs


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

    # Журналы uvicorn глобальны для процесса: здесь их настройку не применяем.
    config = uvicorn.Config(app_path, **{**options, "log_config": None})
    config.load()
    transport = httpx.ASGITransport(app=config.loaded_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "dev"}


def test_dnd_server_console_script_points_to_main():
    [script] = entry_points(group="console_scripts", name="dnd-server")

    assert script.value == "dnd_server.__main__:main"
    assert script.load() is main


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
