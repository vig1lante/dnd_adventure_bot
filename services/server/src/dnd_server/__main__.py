import argparse
import copy
import errno
import fcntl
import logging
import logging.config
import os
import re
import socket
import sqlite3
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import uvicorn
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from dnd_server.config import HOST, PORT, Settings, get_settings, set_serving_port
from dnd_server.db.session import (
    REPO_MARKER,
    REPO_ROOT,
    _sqlite_file,
    create_database_directory,
    resolve_database_url,
)

# Имя журнала задано явно: при `python -m dnd_server` модуль называется `__main__`, и запись ушла
# бы мимо журналов приложения `dnd_server`.
logger = logging.getLogger("dnd_server.main")

# Тестовый мастер (`fake`) в боевом режиме не собирается (`llm.factory`, спека DND-2 §15). Сервер
# с ним всё равно стартует, чтобы не сломать запуск владельца, но говорит, что игру создать нельзя.
NO_MASTER_WARNING = "тестовый мастер в боевом режиме недоступен: задайте LLM_PROVIDER"

# Токен сессии WebSocket идёт в query (`/ws/games/<id>?token=…`): браузер не умеет слать заголовки
# при открытии сокета. uvicorn пишет путь вместе с query в строку рукопожатия (`uvicorn.error`),
# а на уровнях debug и trace ещё и в строки websockets и в repr scope (`uvicorn.asgi`). Поэтому
# маскируется значение каждого параметра query, а не только `token`: имя параметра можно записать
# в процентной кодировке (`t%6Fken`), и сервер всё равно его примет.
MASK = "***"
QUERY_VALUE = re.compile(r"(?<=[?&])([^=&\s\"'?]+)=[^&\s\"']*")
SCOPE_QUERY = re.compile(r"(?<=query_string': b[\'\"])[^\'\"]*")


def mask_query_values(text: str) -> str:
    """Строка журнала, где значения параметров query заменены на `***`."""
    return SCOPE_QUERY.sub(MASK, QUERY_VALUE.sub(rf"\1={MASK}", text))


# Маскируется готовая строка, а не запись: так закрыты и сообщение, и аргументы, и текст
# исключения, а запись остаётся нетронутой для других обработчиков.
class MaskingDefaultFormatter(DefaultFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask_query_values(super().format(record))


class MaskingAccessFormatter(AccessFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask_query_values(super().format(record))


# Журналы приложения и бота: формат uvicorn, но с именем журнала, чтобы записи `dnd_server.games`,
# `dnd_server.realtime` и `aiogram.dispatcher` можно было отличить и отфильтровать.
APP_LOG_FORMAT = "%(levelprefix)s %(name)s: %(message)s"


def make_log_config() -> dict:
    """Настройка журналов uvicorn с маскирующими форматтерами и журналы приложения рядом с ними.

    Без `dnd_server` и `aiogram` записи INFO приложения терялись, а WARNING и ERROR печатались
    голым текстом без уровня и имени. aiogram — от WARNING: на INFO он пишет строку на каждое
    обработанное обновление, а сбои опроса Telegram видны и так."""
    config = copy.deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"]["()"] = MaskingDefaultFormatter
    config["formatters"]["access"]["()"] = MaskingAccessFormatter
    config["formatters"]["app"] = {**config["formatters"]["default"], "fmt": APP_LOG_FORMAT}
    config["handlers"]["app"] = {**config["handlers"]["default"], "formatter": "app"}
    config["loggers"]["dnd_server"] = {"handlers": ["app"], "level": "INFO", "propagate": False}
    config["loggers"]["aiogram"] = {"handlers": ["app"], "level": "WARNING", "propagate": False}
    return config


LOG_CONFIG = make_log_config()


def configure_logging() -> None:
    """Журналы процесса по LOG_CONFIG до проверок старта.

    Без этого предупреждения старта (тестовый мастер в боевом режиме, жёсткая ссылка на месте
    lock-файла) печатал бы `logging.lastResort` голым текстом, без метки WARNING и имени журнала.
    Настройка та же, что потом применит uvicorn (`log_config=LOG_CONFIG`): маскировка query и формат
    журналов не меняются."""
    logging.config.dictConfig(LOG_CONFIG)


def port_number(text: str) -> int:
    """Порт из командной строки: целое от 1 до 65535 (порт 0 — «любой», сервер тогда не найти)."""
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"порт — целое число от 1 до 65535, а не {text!r}")
    return port


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Единственный параметр запуска — порт. Адрес всегда `127.0.0.1`, остальное uvicorn
    (число процессов, уровень журнала) задаёт `main()`: чужое значение здесь ошибка, а не совет."""
    parser = argparse.ArgumentParser(
        prog="dnd-server",
        description=f"Сервер игры. Слушает только {HOST}: наружу его выставляет туннель.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--port",
        type=port_number,
        default=PORT,
        help=f"порт сервера (по умолчанию {PORT}); нужен, чтобы запустить второй сервер, "
        "например для сквозных тестов, рядом с основным",
    )
    return parser.parse_args(argv)


def claim_port(host: str, port: int) -> socket.socket:
    """Занимает порт и держит его, пока работает процесс; занят — останавливает процесс с причиной.

    Порт нужно занять до `uvicorn.run`, потому что uvicorn сначала выполняет `lifespan` приложения
    (восстановление игр, перевод идущих ходов в `dm_failed`) и только потом привязывает порт. Вторая
    копия сервера на той же базе успела бы тронуть игры первой, и лишь затем упасть. Сокет сразу
    слушает: так порт не достаётся второй копии и там, где два непрослушиваемых сокета с
    SO_REUSEADDR делят порт (Linux). SO_REUSEADDR нужен, чтобы быстрый перезапуск не ждал
    закрытия соединений предыдущего запуска (как у самого uvicorn)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen()
    except OSError as error:
        sock.close()
        if error.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"Порт {host}:{port} уже занят: на этой машине работает другая копия dnd-server "
                "или другая программа. Вторую копию на той же базе запускать нельзя: она могла "
                "бы тронуть идущий ход первой. Остановите первую копию или выберите другой "
                "порт: dnd-server --port <порт>."
            ) from error
        raise SystemExit(f"Не удалось занять порт {host}:{port}: {error.strerror}.") from error
    return sock


# Lock-файл: чтение и запись, создать при отсутствии, не идти по символической ссылке и не
# передавать дескриптор дочерним процессам (`claude` хода мастера).
LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC


def _keep_lock_private(descriptor: int, database: Path) -> None:
    """Права 0600 у lock-файла своего пользователя; иначе — предупреждение без изменений.

    Права меняются только у обычного файла с одной ссылкой: `O_NOFOLLOW` не видит жёсткую ссылку,
    и `fchmod` сменил бы права связанного с ней файла (например, скрипта). Чужой файл сменить
    права не даёт. В предупреждении — путь базы, а не того, на что указывает ссылка."""
    found = os.fstat(descriptor)
    if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
        logger.warning(
            "Файл блокировки базы %s — не отдельный обычный файл (жёсткая ссылка или особый "
            "файл): его права не меняются.",
            database,
        )
    elif found.st_uid != os.getuid():
        logger.warning(
            "Файл блокировки базы %s принадлежит другому пользователю: его права не меняются.",
            database,
        )
    elif found.st_mode & 0o777 != 0o600:
        os.fchmod(descriptor, 0o600)


@contextmanager
def claim_database(database_url: str) -> Iterator[None]:
    """Блокирует файл базы SQLite, пока работает процесс; база уже занята — останавливает процесс.

    Порт защищает только от копии на том же порту. Вторая копия на другом порту и той же базе
    выполнила бы `lifespan` (восстановление игр, перевод идущих ходов в `dm_failed`) и запустила бы
    свои отсчёты поверх первой, поэтому база занимается до `uvicorn.run`, как и порт.

    Замок — `flock` без ожидания на lock-файле рядом с файлом базы (не на самой базе: её блокировки
    принадлежат SQLite). Он снимается вместе с процессом при любом выходе, в том числе аварийном, и
    не наследуется дочерними процессами: файл открыт не наследуемым, и `claude`, запущенный ходом
    мастера, не удержит замок после остановки сервера. Lock-файл не удаляется: удалённый и
    созданный заново, он стал бы другим файлом, и две копии удержали бы по «своему» замку одной
    базы. Базы без файла (в памяти, другие СУБД) не блокируются.

    Lock-файл открывается без перехода по символической ссылке (`O_NOFOLLOW`): ссылка на месте
    lock-файла останавливает запуск, и чужой файл не создаётся и не меняется. Новый файл получает
    права 0600, прежний файл своего пользователя — тоже; файл другого пользователя остаётся как
    есть с предупреждением в журнале (сменить ему права может только владелец).

    В сообщении названа только база: путь к ней и так входит в сообщения об ошибке пути (см.
    `DatabasePathError`) и печатается в терминал владельца. Адрес `DATABASE_URL` и lock-файл в
    сообщение не попадают."""
    url = resolve_database_url(database_url)
    database = _sqlite_file(url)
    if database is None:
        yield
        return
    try:
        # Свежая установка: папки базы ещё нет, а замок нужен до того, как её создаст приложение.
        create_database_directory(url)
        descriptor = os.open(database.with_name(database.name + ".lock"), LOCK_FLAGS, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SystemExit(
                f"Рядом с базой {database} вместо файла блокировки лежит символическая ссылка: "
                "по ней сервер не пойдёт. Удалите ссылку и запустите сервер снова."
            ) from error
        raise SystemExit(
            f"Не удалось подготовить блокировку базы {database}: {error.strerror}."
        ) from error
    with os.fdopen(descriptor, "rb") as lock:
        _keep_lock_private(lock.fileno(), database)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit(
                f"База {database} уже занята: с ней работает другая копия dnd-server. Две копии на "
                "одной базе вели бы одни и те же игры: каждая восстанавливала бы чужие идущие ходы "
                "и запускала бы свои отсчёты. Остановите первую копию или запустите эту на своей "
                "базе: DATABASE_URL=sqlite+aiosqlite:///./data/<другой файл>.sqlite3 "
                "dnd-server --port <порт>."
            ) from error
        except OSError as error:
            raise SystemExit(
                f"Не удалось заблокировать базу {database}: {error.strerror}."
            ) from error
        yield


# Команда, которая доводит базу до ревизии кода (README, «База данных и миграции»); из корня
# репозитория, как и запуск сервера.
UPGRADE_COMMAND = "uv run alembic -c services/server/alembic.ini upgrade head"


def require_current_schema(database_url: str) -> None:
    """Останавливает запуск, если файла базы нет или его ревизия — не `head` миграций кода.

    На старой базе новый код падал бы непонятными ошибками запросов, а пустая база без таблиц хуже
    понятного отказа (дополнение B11 §1 п. 8). Ревизия читается только на чтение (`mode=ro`): файл
    не создаётся и не меняется. Базы без файла (в памяти, другие СУБД) не проверяются, как и не
    блокируются. В сообщении — путь к файлу и команда; адреса `DATABASE_URL` в нём нет."""
    database = _sqlite_file(resolve_database_url(database_url))
    if database is None:
        return
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / REPO_MARKER)))
    head = script.get_current_head()
    if not database.is_file():
        raise SystemExit(
            f"Базы {database} нет: сервер не создаёт её сам. Создайте её миграциями: "
            f"{UPGRADE_COMMAND}"
        )
    revision = _recorded_revision(database)
    if revision == head:
        return
    if revision is not None and not _known_revision(script, revision):
        raise SystemExit(
            f"База {database} записана более новой версией сервера (ревизия {revision}, этот "
            f"код знает ревизии до {head}): обновите сервер. База не изменена."
        )
    raise SystemExit(
        f"База {database} старее кода: ревизия {revision or 'не записана'}, нужна {head}. "
        f"Обновите базу (сохранённые игры не теряются): {UPGRADE_COMMAND}"
    )


def _recorded_revision(database: Path) -> str | None:
    """Ревизия из `alembic_version`; `None` — таблицы нет или файл не читается как база."""
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else row[0]


def _known_revision(script: ScriptDirectory, revision: str) -> bool:
    try:
        return script.get_revision(revision) is not None
    except CommandError:
        return False


def warn_without_master(settings: Settings) -> None:
    """Одно предупреждение, если в боевом режиме мастер по умолчанию — тестовый `fake`.

    Запуск не останавливается (решение dispatcher B11 §1 п. 7): игры с таким провайдером не
    создаются (422), а владелец видит, какую переменную задать. Путь к сценарию в текст не идёт."""
    if settings.app_env != "dev" and settings.llm_provider == "fake":
        logger.warning(NO_MASTER_WARNING)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging()
    settings = get_settings()
    warn_without_master(settings)
    # База блокируется по тем же настройкам, по которым приложение её откроет (кэш `get_settings`).
    database_url = settings.database_url
    # Порт раньше базы: у него нет следа на диске, а вторая копия на том же порту и той же базе
    # узнаёт о занятом порте, как и прежде. Занятый сокет передаётся uvicorn (`fd`): порт
    # принадлежит этому процессу с первой секунды, а не с привязки после lifespan. `host` и `port`
    # остаются в параметрах для журнала и тестов.
    with claim_port(HOST, args.port) as claimed, claim_database(database_url):
        # Ревизия сверяется под замком базы: вторая копия не мигрирует её между проверкой и стартом.
        require_current_schema(database_url)
        # Мост MCP `claude_code_cli` строит адрес сервера по порту процесса, и в адресе токен хода:
        # порт называется здесь, когда он уже наш, и до того, как uvicorn запустит приложение.
        set_serving_port(args.port)
        # Фабрика без аргументов: create_app сам берёт кэшированные настройки.
        # Журнал доступа выключен: путь /internal/dm-tools/<токен>/… содержит одноразовый токен
        # хода, а токены в журналы не пишутся. Остальные журналы uvicorn идут через LOG_CONFIG: он
        # прячет значения query, где лежит токен сессии сокета.
        # `workers=1` указан явно: хаб сокетов, блокировки игр, реестр токенов хода и бот живут в
        # памяти одного процесса, а без явного значения uvicorn берёт число процессов из
        # WEB_CONCURRENCY окружения (README, «Один процесс»).
        uvicorn.run(
            "dnd_server.app:create_app",
            factory=True,
            host=HOST,
            port=args.port,
            fd=claimed.fileno(),
            workers=1,
            access_log=False,
            log_config=LOG_CONFIG,
        )


if __name__ == "__main__":
    main()
