import copy
import logging
import re

import uvicorn
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from dnd_server.config import HOST, PORT

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


def main() -> None:
    # Фабрика без аргументов: create_app сам берёт кэшированные настройки.
    # Журнал доступа выключен: путь /internal/dm-tools/<токен>/… содержит одноразовый токен хода,
    # а токены в журналы не пишутся. Остальные журналы uvicorn идут через LOG_CONFIG: он прячет
    # значения query, где лежит токен сессии сокета.
    uvicorn.run(
        "dnd_server.app:create_app",
        factory=True,
        host=HOST,
        port=PORT,
        access_log=False,
        log_config=LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
