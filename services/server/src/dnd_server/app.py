from fastapi import FastAPI

from dnd_server.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    app = FastAPI(title="DnD Adventure Bot")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    return app
