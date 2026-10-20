"""Тексты мастера: строки для модели и безопасные тексты ошибок (план B7, исправление B7.review).

Одно место для механизмов, которым нужна одна реализация: когда правило экранирования или список
секретов меняется, расходиться нечему. Модули `context`, `chronicle`, `weaving` и `turn` берут
отсюда, а не друг у друга:
- `json_line`, `one_line`, `printable_line` — тексты игроков и модели в запросе: одна строка, без
  знаков, которыми чужой текст нарисовал бы свой заголовок или управлял терминалом;
- `safe_error_text`, `describe_error`, `safe_traceback` — текст ошибки для журнала и лога: без
  значений секретов, управляющих знаков и значений входа pydantic (`input_value`: кусок состояния
  игры, тайный ход). Любая запись сервера об исключении идёт через `safe_traceback`
  (`logger.exception` и `exc_info` в журнале сервера не используются; фильтр токена бота читает
  `exc_info` только чужих записей aiogram);
- `failure_reason` и `REASON_*` — короткая причина неудачи для хоста по виду исключения.
"""

import json
import traceback
from typing import Any

from pydantic import SecretStr, ValidationError

from dnd_server.config import Settings
from dnd_server.llm.base import ProviderError, ProviderRefusal, ToolLoopExceeded, printable
from dnd_server.llm.factory import ConfigError

MAX_ERROR_CHARS = 500  # сколько текста ошибки идёт в журнал и в лог

# JSON не экранирует эти знаки конца строки (NEL и разделители строк и абзацев Unicode), а читатель,
# который режет текст по строкам, разорвал бы по ним блок данных.
_LINE_SEPARATORS = {0x85: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"}

# Причины неудачи для хоста: короткий русский текст без подробностей провайдера.
REASON_REFUSAL = "модель отказалась отвечать"
REASON_LOOP = "слишком много вызовов инструментов за ход"
REASON_TIMEOUT = "время ожидания ответа нейронки вышло"
REASON_CONFIG = "нейронка для этой игры не настроена"
REASON_PROVIDER = "ошибка провайдера нейронки"
REASON_EMPTY = "мастер не дал итогового рассказа"
REASON_TRUNCATED = "рассказ оборвался на лимите длины ответа"
REASON_CONFLICT = "состояние игры изменилось за время хода"
REASON_CANCELLED = "ход прерван (остановка сервера)"
REASON_INTERNAL = "внутренняя ошибка сервера"


class ModelTimeout(Exception):
    """Модель не уложилась в отведённое время (ход мастера или вплетение): своя граница сервера."""


# --- строки для контекста и запросов ---


def json_line(value: Any) -> str:
    """JSON в одну строку, без знаков конца строки: безопасен для вставки в контекст."""
    return json.dumps(value, ensure_ascii=False).translate(_LINE_SEPARATORS)


def one_line(text: str) -> str:
    """Текст в одну строку: перевод строки в чужой записи не должен рисовать свои заголовки."""
    return " ".join(text.split())


def printable_line(text: str, limit: int) -> str:
    """Текст как одна печатная строка не длиннее `limit`: пробельные знаки (в том числе переводы
    строк и разрывы U+0085, U+2028, U+2029) сворачиваются, непечатаемое заменяется видимой записью
    (`\\x1b`, `\\u202e`): спрятать в строке нечего."""
    return printable(one_line(text), limit)


# --- ошибки для журнала и хоста ---


def _secret_values(settings: Settings) -> list[str]:
    values = (getattr(settings, name) for name in type(settings).model_fields)
    secrets = [v.get_secret_value() for v in values if isinstance(v, SecretStr)]
    return [secret for secret in secrets if secret]


def safe_error_text(text: str, settings: Settings | None, *, keep: bool = False) -> str:
    """Текст ошибки для журнала и лога: без значений секретов, без управляющих знаков, коротко.

    Ошибка провайдера бывает цитатой ответа модели или вывода CLI: `printable` убирает из неё
    знаки, которыми можно управлять терминалом владельца. `keep` оставляет переводы строк и длину
    (трассировка). Без настроек (`None`: планировщик и набор задач их не знают) значения секретов
    не вырезаются."""
    secrets = [] if settings is None else _secret_values(settings)
    for secret in secrets:
        text = text.replace(secret, "***")
    return printable(text, None if keep else MAX_ERROR_CHARS, keep_newlines=keep)


def _message_of(exc: BaseException) -> str:
    """Текст ошибки; у ошибки pydantic — только где и что не так, без значений входа: они несут
    куски данных игры (летопись, имена, тексты ходов), которым в журнале сервера не место."""
    if isinstance(exc, ValidationError):
        problems = exc.errors(include_input=False, include_url=False, include_context=False)
        return "; ".join(f"{'.'.join(map(str, p['loc']))}: {p['msg']}" for p in problems)
    return str(exc)


def describe_error(exc: BaseException, settings: Settings | None) -> str:
    """Тип и безопасный текст ошибки для журнала хода и журнала сервера."""
    return f"{type(exc).__name__}: {safe_error_text(_message_of(exc), settings)}"


def safe_traceback(exc: BaseException, settings: Settings | None) -> str:
    """Трассировка для журнала сервера: кадры стека и безопасное описание каждой ошибки цепочки.

    Обычная трассировка (`logger.exception`) печатает `str(exc)` целиком, а у ошибок pydantic и базы
    он несёт значения входа. Здесь кадры идут как есть (в них код, а не данные), а ошибки — через
    `describe_error`. Цепочка (`__cause__`, `__context__`) идёт от первопричины к итоговой
    ошибке."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    parts = []
    for error in reversed(chain):
        frames = "".join(traceback.format_tb(error.__traceback__))
        header = "Traceback (most recent call last):\n"
        parts.append(
            safe_error_text(header + frames, settings, keep=True) + describe_error(error, settings)
        )
    return "\n\n".join(parts)


def failure_reason(exc: BaseException) -> str:
    """Причина неудачи для хоста по виду исключения; всё незнакомое — внутренняя ошибка сервера."""
    match exc:
        case ProviderRefusal():
            return REASON_REFUSAL
        case ToolLoopExceeded():
            return REASON_LOOP
        case ModelTimeout():
            return REASON_TIMEOUT
        case ConfigError():
            return REASON_CONFIG
        case ProviderError():
            return REASON_PROVIDER
        case _:
            return REASON_INTERNAL
