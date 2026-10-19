"""Внутренний API инструментов мастера для `claude_code_cli` (спека §7, §9.2, план B5.4).

CLI `claude` ведёт цикл инструментов сам, поэтому исполнять их приходит не наш адаптер, а мост
MCP (`mcp_bridge`) отдельным процессом. Он ходит сюда по токену хода:

- `GET  /internal/dm-tools/{token}/tools` — схемы инструментов хода;
- `POST /internal/dm-tools/{token}/call` с телом `{name, args}` — исполнить вызов,
  ответ `{result, is_error}`.

Доступ закрыт в два слоя. Клиент обязан быть локальным (`127.0.0.1` / `::1`) и не нести заголовков
пересылки: туннель cloudflared приходит с 127.0.0.1, и без второй проверки внешний запрос выглядел
бы локальным. Токен выдаётся на один ход (`TurnTokens.issue`) и отзывается по его окончании.
Чужой клиент получает 403 ещё до проверки токена и чтения тела, неизвестный или отозванный
токен — 404 даже с localhost. Роутер подключается только при `APP_ENV=dev` (см. `app.py`) и скрыт
из схемы OpenAPI.
"""

import logging
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from dnd_server.llm.base import ToolExecutor, ToolSchema

logger = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})
# Заголовки, которыми прокси и туннели сообщают адрес настоящего клиента. Любой из них — признак
# запроса не с этой машины, даже если сокет локальный.
FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip")
TOKEN_BYTES = 32


@dataclass(frozen=True)
class TurnSession:
    """Всё, что нужно мосту в пределах одного хода: исполнитель и схемы его инструментов."""

    executor: ToolExecutor
    schemas: tuple[ToolSchema, ...]


class TurnTokens:
    """Реестр токенов ходов: токен выдаётся на один ход и отзывается по его окончании."""

    def __init__(self) -> None:
        self._sessions: dict[str, TurnSession] = {}

    def issue(self, executor: ToolExecutor, schemas: tuple[ToolSchema, ...]) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._sessions[token] = TurnSession(executor, schemas)
        return token

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def get(self, token: str) -> TurnSession | None:
        return self._sessions.get(token)


class _CallBody(BaseModel):
    name: str
    args: dict[str, Any] = {}


def _require_local(request: Request) -> None:
    host = request.client.host if request.client else None
    forwarded = [name for name in FORWARDING_HEADERS if name in request.headers]
    if host in LOCAL_HOSTS and not forwarded:
        return
    # Токена в сообщении нет: путь запроса с ним в лог не попадает.
    logger.warning("internal API: отказ клиенту %s, заголовки пересылки: %s", host, forwarded)
    raise HTTPException(status_code=403, detail="Forbidden")


def _turn_for(tokens: TurnTokens, token: str, request: Request) -> TurnSession:
    _require_local(request)
    turn = tokens.get(token)
    if turn is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return turn


def build_internal_router(tokens: TurnTokens) -> APIRouter:
    router = APIRouter(prefix="/internal/dm-tools", include_in_schema=False)

    @router.get("/{token}/tools")
    async def list_tools(token: str, request: Request) -> list[dict[str, Any]]:
        return [asdict(schema) for schema in _turn_for(tokens, token, request).schemas]

    # Тело читаем сами, а не параметром FastAPI: тот разбирает тело до проверки доступа, и чужой
    # клиент получил бы 400/422 вместо 403.
    @router.post("/{token}/call")
    async def call_tool(token: str, request: Request) -> dict[str, Any]:
        turn = _turn_for(tokens, token, request)
        try:
            body = _CallBody.model_validate_json(await request.body())
        except ValidationError:
            raise HTTPException(status_code=422, detail="Нужно тело {name, args}") from None
        result, is_error = await turn.executor(body.name, body.args)
        return {"result": result, "is_error": is_error}

    return router
