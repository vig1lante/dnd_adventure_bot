import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.engine import URL

from dnd_engine.content import load_srd
from dnd_engine.rng import SystemRng
from dnd_server.adventures import load_catalog
from dnd_server.adventures import router as adventures_router
from dnd_server.auth.api import router as auth_router
from dnd_server.auth.dev import dev_router
from dnd_server.auth.session import SessionSigner
from dnd_server.authoring.api import handle_draft_error
from dnd_server.authoring.api import router as authoring_router
from dnd_server.authoring.jobs import fail_interrupted_jobs
from dnd_server.authoring.publish import load_published
from dnd_server.authoring.service import DraftError
from dnd_server.bot.bot import running_bot
from dnd_server.bot.handlers import BotDeps
from dnd_server.bot.notify import Notifier
from dnd_server.characters.api import router as characters_router
from dnd_server.characters.previews import RandomPreviews
from dnd_server.config import Settings, get_settings
from dnd_server.db.session import create_database_directory, make_engine, make_sessionmaker
from dnd_server.dm.provider import GameProviders
from dnd_server.games.api import handle_game_error
from dnd_server.games.api import router as games_router
from dnd_server.games.providers import router as providers_router
from dnd_server.games.service import GameError, GameLocks, JoinAttempts, ModelCalls
from dnd_server.llm.internal_api import TurnTokens, build_internal_router
from dnd_server.realtime.hub import Hub
from dnd_server.realtime.ws import router as realtime_router
from dnd_server.turns.api import router as turns_router
from dnd_server.turns.flow import Flow, FlowHooks, restore_games, scene_follow_up
from dnd_server.turns.timers import Scheduler, TaskSet

# Имена, под которыми к серверу обращаются с этой же машины. Прокси Vite шлёт сюда либо
# `127.0.0.1:8000`, либо исходный Host страницы (`localhost:5173`); порт при проверке не важен.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


# Имя поля, которое можно назвать в ответе: обычный идентификатор модели. Всё остальное (чужой
# ключ тела, индекс в списке) в текст ошибки не попадает.
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")


# Ошибки поля-метки тела с вариантами: метки нет или она неизвестна. Путь у них — само тело,
# а имя метки pydantic даёт в `ctx["discriminator"]` в кавычках (`"'mode'"`).
TAG_ERRORS = ("union_tag_invalid", "union_tag_not_found")


def field_of(error: dict[str, Any], body: Any) -> object:
    """Поле запроса, о котором говорит ошибка pydantic: `("body", "login")` → `login`.

    У тела с вариантами (дискриминированное объединение) pydantic ставит после `body` тег ветки —
    значение поля-метки из самого тела (`("body", "quick", "name")`), и поле — следующий сегмент.
    Тег отличается от поля тем, что такого ключа в теле нет, а такое значение есть."""
    location = error["loc"]  # ("body", "login"), ("query", "limit"), ("body", 10) у битого JSON
    if error["type"] in TAG_ERRORS:
        return str(error.get("ctx", {}).get("discriminator", "")).strip("'")
    if (
        len(location) > 2
        and isinstance(body, dict)
        and location[1] not in body
        and location[1] in body.values()
    ):
        return location[2]
    return location[1] if len(location) > 1 else None


def validation_detail(exc: RequestValidationError) -> str:
    """Русский текст ошибки запроса: названы только поля, значения из запроса не повторяются.

    Ошибка — `{"detail": "<строка>"}`, а FastAPI отдаёт в `detail` список с входными значениями
    (`input`), то есть возвращает клиенту недоверенный ввод.
    """
    fields: list[str] = []
    for error in exc.errors():
        name = field_of(error, exc.body)
        if isinstance(name, str) and FIELD_NAME.fullmatch(name) and name not in fields:
            fields.append(name)
    if not fields:
        return "Некорректные данные запроса."
    return f"Некорректные данные запроса: проверьте {', '.join(fields)}."


async def handle_invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": validation_detail(exc)})


async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    """Необработанное исключение отвечает в общем формате ошибок (`detail`), а не текстом Starlette.

    Подробности клиенту не уходят: Starlette после ответа пробрасывает исключение серверу, и
    uvicorn пишет трассировку в журнал. WebSocket сюда не попадает: сокет сам закрывается с кодом.
    """
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера. Попробуйте ещё раз немного позже."},
    )


def trusted_hosts(settings: Settings) -> list[str]:
    """Допустимые значения заголовка Host: локальные имена и хост из `WEBAPP_PUBLIC_URL`.

    Публичный хост нужен для VPS и туннеля. Шаблоны со «*» из него не берутся: опечатка в адресе
    не должна открывать сервер для любых имён.
    """
    hosts = list(LOCAL_HOSTS)
    public_host = urlsplit(settings.webapp_public_url).hostname
    if public_host and "*" not in public_host:
        hosts.append(public_host)
    return hosts


def database_file_exists(url: URL) -> bool:
    """Есть ли база, из которой можно восстанавливать игры.

    Ложь только для файловой SQLite, которой ещё нет (свежая установка): подключение создало бы
    пустой файл, а сборка и старт приложения ничего на диске создавать не должны."""
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return True
    return Path(url.database).exists()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # В prod слабый SESSION_SECRET останавливает старт: с ним сессии подделываются.
    SessionSigner.from_settings(app.state.settings)
    # Папку для файла SQLite создаёт приложение при старте, а не сборка приложения: сборка
    # (в том числе в тестах) не должна ничего создавать на диске.
    create_database_directory(app.state.engine.url)
    # Каталог приключений — один словарь на всё время работы: его держат маршруты игр, публикация
    # конструктора и бот. Папки читаются при старте (битая в каталог не попадает), опубликованное —
    # из базы ниже, а публикация дописывает его на месте, без перезапуска.
    catalog = app.state.adventures
    catalog.update(load_catalog(app.state.settings.content_dir, load_srd()))
    # Бот живёт, пока работает приложение, и только с токеном; к этому моменту каталог уже загружен.
    bot_deps = BotDeps(
        settings=app.state.settings,
        sessionmaker=app.state.sessionmaker,
        catalog=catalog,
        turn_tokens=getattr(app.state, "turn_tokens", None),
    )
    async with running_bot(bot_deps) as notifier:
        app.state.notifier = notifier
        # Игры переживают перезапуск: отсчёты ставятся из базы, прерванные разборы отмечаются.
        if database_file_exists(app.state.engine.url):
            await restore_games(Flow.from_app(app))
            # Задания конструктора, прерванные остановкой, иначе навсегда закрыли бы свои черновики.
            await fail_interrupted_jobs(app.state.sessionmaker, app.state.settings)
            # Опубликованное конструктором — в тот же каталог; папка побеждает при совпадении id.
            await load_published(app.state.sessionmaker, catalog, app.state.settings)
        try:
            yield
        finally:
            # Отсчёты снимаются, идущий разбор мастера отменяется и успевает записать себя
            # неудачным (игра не остаётся в `resolving`); бот ещё работает, пока это происходит.
            await app.state.scheduler.close()
            await app.state.turn_tasks.shutdown()
            await app.state.authoring_tasks.shutdown()
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    # В prod описание API (/openapi.json, /docs, /redoc) закрыто: оно выдаёт устройство сервера тем,
    # кто знает адрес туннеля.
    description_urls = (
        {}
        if settings.app_env == "dev"
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    app = FastAPI(title="DnD Adventure Bot", lifespan=lifespan, **description_urls)
    app.state.settings = settings
    app.add_exception_handler(RequestValidationError, handle_invalid_request)
    app.add_exception_handler(GameError, handle_game_error)
    app.add_exception_handler(DraftError, handle_draft_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    # Чужое имя хоста — признак DNS-rebinding (страница злоумышленника ходит на localhost как на
    # свой сайт); запрос получает 400 до любого маршрута, в том числе рукопожатие WebSocket.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(settings))
    # Путь к базе проверяется здесь, при сборке: недопустимый путь останавливает старт, а соединение
    # с файлом открывается только при первом запросе. Сессию берут зависимостью `get_session`.
    app.state.engine = make_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    # Пока `lifespan` не выполнен (клиент в памяти без старта), приключений нет, но маршруты живы.
    app.state.adventures = {}
    # События игры рассылает хаб сокетов; он же `Broadcaster` для лобби, персонажей и хода мастера.
    # Запуск мастера после «Начать» — `FlowHooks` (вплетение героев и открытие игры).
    app.state.game_locks = GameLocks()
    # Пределы тоже считаются в памяти процесса: сервер всегда один процесс (README), как блокировки.
    app.state.model_calls = ModelCalls()
    app.state.join_attempts = JoinAttempts()
    # Предпросмотры случайных героев тоже живут в памяти процесса, с пределом частоты.
    app.state.random_previews = RandomPreviews()
    app.state.hub = Hub()
    app.state.broadcaster = app.state.hub
    app.state.game_hooks = FlowHooks(app)
    # Ход мастера: провайдеры игр живут в памяти процесса, как блокировки; выбор следующей фазы
    # по сцене подставляют бой и привал; отсчёты ведёт планировщик, разборы мастера идут фоновыми
    # задачами, обе части останавливаются вместе с сервером.
    app.state.game_providers = GameProviders()
    app.state.turn_follow_up = scene_follow_up
    app.state.scheduler = Scheduler(settings=settings)
    app.state.turn_tasks = TaskSet(settings=settings)
    # Задания генерации конструктора — такие же фоновые задачи с учётом и остановкой.
    app.state.authoring_tasks = TaskSet(settings=settings)
    # Уведомления в Telegram шлёт мастер через `app.state.notifier`. До старта и без токена
    # бота это заглушка, которая ничего не отправляет; `lifespan` подставляет рабочую.
    app.state.notifier = Notifier(app.state.sessionmaker)
    # Броски персонажей делает только сервер и только `SystemRng`. Подменить генератор можно лишь
    # кодом (тесты кладут сюда `SeededRng`); настройки и запросы такой возможности не дают.
    app.state.rng = SystemRng()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    app.include_router(auth_router)
    app.include_router(adventures_router)
    app.include_router(games_router)
    app.include_router(providers_router)
    app.include_router(characters_router)
    app.include_router(turns_router)
    app.include_router(realtime_router)
    app.include_router(authoring_router)
    # dev-вход есть только в dev: в prod маршрута нет вовсе, запрос получает обычный 404.
    if settings.app_env == "dev":
        app.include_router(dev_router)

    # Внутренний API нужен только провайдеру claude_code_cli, а он доступен лишь в dev: в prod
    # маршрутов /internal нет вовсе, запрос получает обычный 404.
    if settings.app_env == "dev":
        app.state.turn_tokens = TurnTokens()
        app.include_router(build_internal_router(app.state.turn_tokens))

    return app
