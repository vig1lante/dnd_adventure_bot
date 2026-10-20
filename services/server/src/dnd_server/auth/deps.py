"""Кто делает запрос и что ему можно (план B6.2).

REST берёт токен сессии из `Authorization: Bearer` (`current_user`), WebSocket — из query `token`
(`websocket_user`: браузер не умеет слать заголовки при открытии сокета). Остальные части блока
(игры, персонажи, сокеты, бот) строят доступ из этих кирпичиков и сами сессий не проверяют.
"""

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from dnd_server.auth.session import InvalidSession, SessionSigner, WeakSessionSecret
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.db.session import get_session

# Без автоматического отказа: библиотека ответила бы 403, а договорённость блока — 401 с русским
# текстом ошибки.
bearer_scheme = HTTPBearer(auto_error=False)


def app_settings(connection: HTTPConnection) -> Settings:
    """Настройки приложения (`create_app` кладёт их в `app.state`); годится и для WebSocket."""
    return connection.app.state.settings


def get_signer(connection: HTTPConnection) -> SessionSigner:
    """Подписчик сессий. В prod со слабым секретом входа нет (сервер с ним и не стартует)."""
    try:
        return SessionSigner.from_settings(app_settings(connection))
    except WeakSessionSecret:
        raise HTTPException(
            status_code=503, detail="Вход недоступен: сервер настроен небезопасно."
        ) from None


# --- пользователь ---


async def find_or_create_user(
    session: AsyncSession, *, name: str, tg_id: int | None = None, dev_login: str | None = None
) -> User:
    """Пользователь по `tg_id` (вход через Telegram) или `dev_login` (dev-вход); нового создаёт.

    Один вызов — одна транзакция с коммитом. Первый вход двух запросов одного пользователя
    параллелен: оба не находят строки и вставляют её. Уникальность `tg_id` и `dev_login` отвергает
    вторую вставку, и тот запрос берёт строку победителя, а не отвечает 500. Имя Telegram
    меняется, поэтому при входе оно обновляется.
    """
    by_key = User.tg_id == tg_id if tg_id is not None else User.dev_login == dev_login
    user = await session.scalar(select(User).where(by_key))
    if user is None:
        user = User(tg_id=tg_id, dev_login=dev_login, name=name)
        session.add(user)
        try:
            await session.commit()
            return user
        except IntegrityError:
            await session.rollback()
            user = await session.scalar(select(User).where(by_key).limit(1))
            if user is None:  # отказ вставки не из-за уникальности: это ошибка, а не гонка
                raise
    if user.name != name:
        user.name = name
        await session.commit()
    return user


async def user_for_token(
    session: AsyncSession, signer: SessionSigner, token: str | None
) -> User | None:
    """Пользователь по токену сессии; `None` — токена нет, он недействителен или пользователь исчез.

    Не знает про HTTP: REST отвечает на `None` кодом 401, WebSocket закрывается с кодом 4401.
    """
    if not token:
        return None
    try:
        user_id = signer.user_id(token)
    except InvalidSession:
        return None
    return await session.get(User, user_id)


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    signer: Annotated[SessionSigner, Depends(get_signer)],
) -> User:
    user = await user_for_token(session, signer, credentials.credentials if credentials else None)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Нужно войти: токен сессии не передан или недействителен.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Чтение пользователя открыло транзакцию и заняло соединение пула, а дальше маршрут может долго
    # ждать блокировку игры. Держатель блокировки открывает свою сессию и ждёт соединение из того же
    # пула: если ждущие держат все соединения, встаёт вся база (дефект D1 блока B7). Коммит без
    # изменений отпускает соединение; `expire_on_commit=False` сохраняет поля пользователя, а
    # следующее обращение сессии возьмёт новое соединение уже под блокировкой.
    await session.commit()
    return user


async def websocket_user(websocket: WebSocket) -> User | None:
    """Пользователь по токену из query `token`; `None` — сокет надо закрыть с кодом 4401.

    Сессию базы открывает сама: зависимость `get_session` принимает `Request` и в маршрутах
    WebSocket не подставляется.
    """
    signer = SessionSigner.from_settings(app_settings(websocket))
    async with websocket.app.state.sessionmaker() as session:
        return await user_for_token(session, signer, websocket.query_params.get("token"))


# --- кто может создавать игры (решение владельца B6: «Только из списка») ---


def is_game_creator_tg_id(settings: Settings, tg_id: int) -> bool:
    """Есть ли Telegram-id в списке `GAME_CREATOR_TG_IDS`. Бот проверяет так до заведения хоста."""
    return tg_id in settings.game_creator_tg_ids


def can_create_games(settings: Settings, user: User) -> bool:
    """Может ли пользователь создавать игры и выбирать провайдера и модель.

    dev-логины могут в dev; строка dev-входа в базе при запуске в prod прав не даёт. Остальные —
    только Telegram-id из списка: пустой список означает, что в prod игры не создаёт никто.
    """
    if user.dev_login is not None:
        return settings.app_env == "dev"
    return user.tg_id is not None and is_game_creator_tg_id(settings, user.tg_id)


async def require_game_creator(
    user: Annotated[User, Depends(current_user)],
    settings: Annotated[Settings, Depends(app_settings)],
) -> User:
    """Зависимость маршрутов создания игры: не из списка — 403."""
    if not can_create_games(settings, user):
        raise HTTPException(
            status_code=403,
            detail="Создавать игры могут только игроки из списка владельца сервера.",
        )
    return user
