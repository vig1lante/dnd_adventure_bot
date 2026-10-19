from fastapi import FastAPI

from dnd_server.config import Settings, get_settings
from dnd_server.llm.internal_api import TurnTokens, build_internal_router


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="DnD Adventure Bot")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    # Внутренний API нужен только провайдеру claude_code_cli, а он доступен лишь в dev
    # (спека §2): в prod маршрутов /internal нет вовсе, запрос получает обычный 404.
    if settings.app_env == "dev":
        app.state.turn_tokens = TurnTokens()
        app.include_router(build_internal_router(app.state.turn_tokens))

    return app
