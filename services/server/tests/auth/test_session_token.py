"""Токен сессии и секрет его подписи (`auth/session.py`).

Токен подписан `SESSION_SECRET` и живёт 7 дней. В prod слабый секрет делает подпись подделываемой,
поэтому такой запуск отвергается; в сообщении об отказе значения секрета нет.
"""

from types import SimpleNamespace

import itsdangerous.timed
import pytest

from dnd_server.auth.session import (
    MIN_SECRET_LENGTH,
    SESSION_MAX_AGE_S,
    InvalidSession,
    SessionSigner,
    WeakSessionSecret,
)
from dnd_server.config import Settings

SECRET = "s" * 40
ISSUED_AT = 1_800_000_000


def freeze_time(monkeypatch, moment: float) -> None:
    # itsdangerous берёт время из своего модуля `time`; подменяем только его, а не часы процесса.
    monkeypatch.setattr(itsdangerous.timed, "time", SimpleNamespace(time=lambda: moment))


# --- подпись и срок ---


def test_token_returns_the_user_id_it_was_issued_for():
    signer = SessionSigner(SECRET)

    assert signer.user_id(signer.issue("user-1")) == "user-1"


def test_tokens_of_different_users_differ():
    signer = SessionSigner(SECRET)

    assert signer.issue("user-1") != signer.issue("user-2")


def tamper_signature(token: str) -> str:
    """Токен с другим знаком в середине подписи: байты подписи меняются всегда.

    Последний знак base64 нельзя менять: у подписи в 20 байт в нём лишние биты, и замена на соседний
    знак иногда даёт ту же подпись (токен остаётся верным).
    """
    middle = len(token) - 10
    return token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1 :]


def test_tampered_token_is_rejected():
    signer = SessionSigner(SECRET)

    # Много пользователей — много разных подписей: тест не зависит от того, какая досталась.
    for number in range(50):
        token = signer.issue(f"user-{number}")

        with pytest.raises(InvalidSession):
            signer.user_id(tamper_signature(token))


def test_token_signed_with_another_secret_is_rejected():
    token = SessionSigner("o" * 40).issue("user-1")

    with pytest.raises(InvalidSession):
        SessionSigner(SECRET).user_id(token)


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "Bearer x", "токен"])
def test_garbage_is_rejected(token):
    with pytest.raises(InvalidSession):
        SessionSigner(SECRET).user_id(token)


def test_token_lives_seven_days(monkeypatch):
    signer = SessionSigner(SECRET)
    freeze_time(monkeypatch, ISSUED_AT)
    token = signer.issue("user-1")

    assert SESSION_MAX_AGE_S == 7 * 24 * 3600
    freeze_time(monkeypatch, ISSUED_AT + SESSION_MAX_AGE_S - 1)
    assert signer.user_id(token) == "user-1"
    freeze_time(monkeypatch, ISSUED_AT + SESSION_MAX_AGE_S + 1)
    with pytest.raises(InvalidSession):
        signer.user_id(token)


# --- секрет подписи из настроек ---


@pytest.mark.parametrize(
    "secret",
    [None, "change-me", "x" * (MIN_SECRET_LENGTH - 1), "weak-sentinel-value"],
    ids=["не задан", "заглушка", "31 символ", "короткий"],
)
def test_prod_refuses_a_missing_or_weak_secret(secret):
    settings = Settings(app_env="prod", session_secret=secret)

    with pytest.raises(WeakSessionSecret) as refusal:
        SessionSigner.from_settings(settings)

    assert "SESSION_SECRET" in str(refusal.value)  # владелец видит, что исправлять
    if secret not in (None, "change-me"):
        assert secret not in str(refusal.value)  # а сам секрет в сообщении не повторяется


def test_prod_accepts_a_strong_secret():
    strong = "k" * MIN_SECRET_LENGTH
    signer = SessionSigner.from_settings(Settings(app_env="prod", session_secret=strong))

    assert signer.user_id(SessionSigner(strong).issue("user-1")) == "user-1"


@pytest.mark.parametrize("secret", [None, "change-me", "short"])
def test_dev_works_with_a_missing_or_placeholder_secret(secret):
    # В dev вход открыт любому dev-логину, поэтому заглушка ничего не ослабляет.
    signer = SessionSigner.from_settings(Settings(app_env="dev", session_secret=secret))

    assert signer.user_id(signer.issue("user-1")) == "user-1"


def test_dev_without_a_secret_signs_the_same_way_on_every_start():
    # Токены dev-вкладок переживают перезапуск сервера, пока секрет не задан.
    first = SessionSigner.from_settings(Settings(app_env="dev"))
    second = SessionSigner.from_settings(Settings(app_env="dev"))

    assert second.user_id(first.issue("user-1")) == "user-1"
