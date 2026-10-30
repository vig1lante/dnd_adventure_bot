"""Токен сессии: подпись `SESSION_SECRET`, нагрузка `{"uid": id пользователя}`, 7 дней.

Токен не шифруется, а только подписывается: по нему видно id пользователя, но подделать его без
секрета нельзя. Секрет живёт в env и в памяти процесса; ни в сообщениях об ошибках, ни в логах его
значения нет. В prod незаданный, шаблонный или короткий секрет отвергается (см. `from_settings`):
с таким секретом сессии подбираются или подделываются.
"""

from itsdangerous import BadData, URLSafeTimedSerializer

from dnd_server.config import Settings

SESSION_MAX_AGE_S = 7 * 24 * 3600
MIN_SECRET_LENGTH = 32
PLACEHOLDER_SECRET = "change-me"  # значение из `.env.example`
# Запасной секрет dev-режима, когда `SESSION_SECRET` не задан: токены dev-вкладок переживают
# перезапуск сервера. В dev вход и так открыт любому dev-логину, поэтому он ничего не ослабляет;
# в prod `from_settings` до него не доходит.
DEV_FALLBACK_SECRET = "dev-only-session-secret-never-used-in-prod"
# Соль отделяет токены сессий от любых других подписей, сделанных тем же секретом.
SESSION_SALT = "dnd-session"


class WeakSessionSecret(RuntimeError):
    """В prod `SESSION_SECRET` не задан или слаб. В тексте только причина, значения секрета нет."""


class InvalidSession(Exception):
    """Токен подделан, повреждён или просрочен."""


class SessionSigner:
    """Выдаёт токены сессий и читает их: единственное место, где применяется секрет подписи."""

    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt=SESSION_SALT)

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionSigner:
        """Подписчик на секрете из настроек; в prod слабый секрет — `WeakSessionSecret`."""
        secret = settings.session_secret.get_secret_value() if settings.session_secret else None
        if settings.app_env == "dev":
            return cls(secret or DEV_FALLBACK_SECRET)
        if secret is None:
            raise WeakSessionSecret(
                "SESSION_SECRET не задан: в prod сервер не работает без секрета сессий. "
                f"Задайте случайный секрет не короче {MIN_SECRET_LENGTH} символов (команда — "
                "в README, раздел «Переменные окружения»)."
            )
        if secret == PLACEHOLDER_SECRET or len(secret) < MIN_SECRET_LENGTH:
            raise WeakSessionSecret(
                "SESSION_SECRET слабый: в prod нужен случайный секрет не короче "
                f"{MIN_SECRET_LENGTH} символов, а не шаблонная заглушка из .env.example "
                "(команда — в README, раздел «Переменные окружения»)."
            )
        return cls(secret)

    def issue(self, user_id: str) -> str:
        return self._serializer.dumps({"uid": user_id})

    def user_id(self, token: str) -> str:
        """Id пользователя из токена; подделанный или просроченный токен — `InvalidSession`."""
        try:
            return self._serializer.loads(token, max_age=SESSION_MAX_AGE_S)["uid"]
        except BadData:  # подпись не сошлась, срок вышел или нагрузка повреждена
            raise InvalidSession("Токен сессии недействителен.") from None
