"""Dev-вход без Telegram: `dev-1`…`dev-6`.

Маршрут подключается только при `APP_ENV=dev` (см. `app.py`): в prod его нет вовсе, и любой запрос
к нему получает обычный 404. Так ни по форме тела, ни по логину не видно, что он когда-то был.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_server.auth.api import LoginOut, login_out
from dnd_server.auth.deps import app_settings, find_or_create_user, get_signer
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.session import get_session

DevLogin = Literal["dev-1", "dev-2", "dev-3", "dev-4", "dev-5", "dev-6"]

dev_router = APIRouter(prefix="/api/auth")


class DevLoginIn(BaseModel):
    login: DevLogin


@dev_router.post("/dev")
async def login_as_dev_player(
    body: DevLoginIn,
    settings: Annotated[Settings, Depends(app_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    signer: Annotated[SessionSigner, Depends(get_signer)],
) -> LoginOut:
    # Имя игрока — сам логин: в шести вкладках видно, кто есть кто.
    user = await find_or_create_user(session, name=body.login, dev_login=body.login)
    return login_out(user, signer, settings)
