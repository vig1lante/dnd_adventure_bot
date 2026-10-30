"""Вход и сведения о пользователе: `/api/auth/telegram`, `/api/me`, `/api/config`.

Ошибки отдаются как `{"detail": "<русский текст>"}`. Ни в ответах, ни в журнале нет initData, hash,
токена бота и `SESSION_SECRET`.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_server.auth.deps import (
    app_settings,
    can_create_games,
    current_user,
    find_or_create_user,
    get_signer,
)
from dnd_server.auth.session import SessionSigner
from dnd_server.auth.telegram import InvalidInitData, validate_init_data
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class UserOut(BaseModel):
    id: str
    name: str
    # Для Mini App: показывать ли создание игры. Право проверяет сервер (403), признак — подсказка.
    can_create_games: bool


class LoginOut(BaseModel):
    token: str
    user: UserOut
    # Параметр запуска Mini App (`startapp=<код игры>`); при dev-входе его нет.
    start_param: str | None


class ConfigOut(BaseModel):
    env: Literal["dev", "prod"]
    # Открыт ли dev-вход: Mini App вне Telegram показывает выбор dev-1…dev-6 только при `true`.
    dev_login: bool
    # Имя бота для ссылок-приглашений; пусто, пока `TELEGRAM_BOT_USERNAME` не задан.
    bot_username: str | None


def user_out(user: User, settings: Settings) -> UserOut:
    return UserOut(id=user.id, name=user.name, can_create_games=can_create_games(settings, user))


def login_out(
    user: User, signer: SessionSigner, settings: Settings, start_param: str | None = None
) -> LoginOut:
    """Ответ входа: токен сессии, пользователь и параметр запуска. Общий для всех способов входа."""
    return LoginOut(
        token=signer.issue(user.id), user=user_out(user, settings), start_param=start_param
    )


def init_data_from(authorization: str | None) -> str | None:
    """`Authorization: tma <initDataRaw>` -> строка initData; иное — `None`."""
    scheme, _, raw = (authorization or "").partition(" ")
    if scheme.lower() != "tma":
        return None
    return raw.strip() or None


@router.post("/auth/telegram")
async def login_with_telegram(
    settings: Annotated[Settings, Depends(app_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    signer: Annotated[SessionSigner, Depends(get_signer)],
    authorization: Annotated[str | None, Header()] = None,
) -> LoginOut:
    if settings.telegram_bot_token is None:
        raise HTTPException(
            status_code=503, detail="Вход через Telegram не настроен на сервере: нет токена бота."
        )
    raw = init_data_from(authorization)
    if raw is None:
        raise HTTPException(
            status_code=401, detail="Откройте приложение из Telegram: данные входа не переданы."
        )
    try:
        telegram_user = validate_init_data(
            raw, settings.telegram_bot_token.get_secret_value(), now=datetime.now(UTC)
        )
    except InvalidInitData as refusal:
        # Причина названа общими словами (подпись, срок): данных входа в журнале нет.
        logger.warning("Вход через Telegram отклонён: %s", refusal)
        raise HTTPException(
            status_code=401,
            detail="Не удалось проверить вход через Telegram. Откройте приложение заново.",
        ) from None
    user = await find_or_create_user(session, name=telegram_user.first_name, tg_id=telegram_user.id)
    return login_out(user, signer, settings, telegram_user.start_param)


@router.get("/me")
async def me(
    user: Annotated[User, Depends(current_user)],
    settings: Annotated[Settings, Depends(app_settings)],
) -> UserOut:
    return user_out(user, settings)


@router.get("/config")
async def config(settings: Annotated[Settings, Depends(app_settings)]) -> ConfigOut:
    return ConfigOut(
        env=settings.app_env,
        dev_login=settings.app_env == "dev",
        bot_username=settings.telegram_bot_username,
    )
