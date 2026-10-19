"""Проверка initData Mini App (спека §9.3, план B6.2).

Подпись и разбор делает `aiogram.utils.web_app.safe_parse_webapp_init_data`: он строит ключ как
`HMAC_SHA256(key="WebAppData", msg=токен бота)`, считает HMAC строки проверки (поля без `hash`,
отсортированные, `ключ=значение` через перевод строки) и сравнивает через `hmac.compare_digest`,
то есть алгоритм Telegram из документации. Срока действия aiogram не проверяет, это делаем здесь.

В ошибках нет ни данных запроса, ни hash, ни токена бота: причина названа общими словами, а
исходное исключение aiogram и pydantic не цепляется (в нём были бы входные данные).
"""

from dataclasses import dataclass
from datetime import datetime

from aiogram.utils.web_app import safe_parse_webapp_init_data

DEFAULT_MAX_AGE_S = 24 * 3600
# Допуск на расхождение часов сервера и Telegram. auth_date из будущего дальше допуска невозможен
# при честных часах: такую подпись принимать не за что.
MAX_CLOCK_SKEW_S = 300


class InvalidInitData(ValueError):
    """initData не прошла проверку: подпись, срок или состав данных."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    username: str | None
    start_param: str | None


def validate_init_data(
    raw: str, bot_token: str, *, max_age_s: int = DEFAULT_MAX_AGE_S, now: datetime
) -> TelegramUser:
    """Пользователь из initData, если подпись верна, а `auth_date` не старше `max_age_s`.

    `now` — момент проверки, обязательно с часовым поясом (сервер передаёт `datetime.now(UTC)`).
    """
    try:
        data = safe_parse_webapp_init_data(bot_token, raw)
    except ValueError, TypeError:
        # ValueError: неверная подпись; сломанный JSON и поля при верной подписи. TypeError:
        # `hmac.compare_digest` не сравнивает строки с не-ASCII знаками, а такой hash присылает
        # любой желающий. Это та же неверная подпись, а не сбой сервера с трассировкой в журнале.
        raise InvalidInitData("подпись неверна или данные повреждены") from None
    if data.user is None:
        raise InvalidInitData("в данных нет пользователя")
    age_s = (now - data.auth_date).total_seconds()
    if age_s > max_age_s:
        raise InvalidInitData("срок действия данных истёк")
    if age_s < -MAX_CLOCK_SKEW_S:
        raise InvalidInitData("дата данных лежит в будущем")
    return TelegramUser(
        id=data.user.id,
        first_name=data.user.first_name,
        username=data.user.username,
        start_param=data.start_param,
    )
