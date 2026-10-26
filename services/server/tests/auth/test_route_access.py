"""Доступ ко всем маршрутам `/api` одним обходом (план B6.2, финальное покрытие блока).

Тесты пунктов проверяют вход по месту: «этот маршрут без токена — 401». Здесь то же самое сделано
для всех маршрутов, которые приложение подключило, в dev и в prod: маршрут, добавленный позже и
забывший зависимость входа, ответит не 401, и этот тест упадёт. Публичных маршрутов ровно четыре и
они перечислены явно; внутренний API (`/internal`, только dev) защищён по-своему — токеном хода и
запретом пересылки (`test_security_gates.py`), а сокет игры — кодами 4401 и 4403 (`test_ws.py`).
"""

import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import itsdangerous.timed
import pytest
from pydantic import SecretStr

from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.models import User

SESSION_SECRET = "route-access-session-secret-0123456789-abc"
ANOTHER_SECRET = "another-session-secret-0123456789-abcdef"
GAME_ID = "00000000-0000-0000-0000-000000000000"
# Единственные маршруты, которым токен сессии не нужен: проверка живости, настройки для входа и сами
# входы (они выдают токен). Всё остальное в `/api` без токена закрыто.
PUBLIC = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/api/config"),
        ("POST", "/api/auth/telegram"),
        ("POST", "/api/auth/dev"),
    }
)
# Какие префиксы вообще бывают у маршрутов приложения; новый префикс требует решения о доступе.
KNOWN_PREFIXES = ("/health", "/api/", "/ws/", "/internal/")
# Маршруты только для создателей игр: конструктор приключений (DND-2 B4.1), копия приключения
# каталога в черновик и справочник монстров SRD (B6.1).
CREATOR_ONLY_PREFIXES = ("/api/drafts", "/api/adventures/{adventure_id}/draft", "/api/srd/")


@asynccontextmanager
async def client_for(settings: Settings, app_env: str):
    prepared = settings.model_copy(
        update={"app_env": app_env, "session_secret": SecretStr(SESSION_SECRET)}
    )
    app = create_app(prepared)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield app, http
    await app.state.engine.dispose()


def protected_routes(route_table, app) -> list[tuple[str, str]]:
    """(метод, путь) каждого маршрута REST, кроме публичных; WebSocket и `/internal` не в счёте."""
    routes = route_table(app)
    assert routes
    assert all(route.path.startswith(KNOWN_PREFIXES) for route in routes)
    return [
        (route.method, route.path)
        for route in routes
        if route.path.startswith("/api/") and (route.method, route.path) not in PUBLIC
    ]


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", GAME_ID, path)


async def add_user(db) -> str:
    async with db() as session:
        user = User(dev_login="dev-1", name="Игрок")
        session.add(user)
        await session.commit()
    return user.id


@pytest.mark.parametrize("app_env", ["dev", "prod"])
async def test_only_the_routes_that_issue_or_describe_the_login_are_public(
    settings, db, route_table, app_env
):
    async with client_for(settings, app_env) as (app, http):
        public = {(route.method, route.path) for route in route_table(app)} & PUBLIC

        # Без токена отвечают: проверка живости и настройки входа.
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/api/config")).status_code == 200

    expected = PUBLIC if app_env == "dev" else PUBLIC - {("POST", "/api/auth/dev")}
    assert public == expected  # dev-входа в prod нет вовсе


@pytest.mark.parametrize("app_env", ["dev", "prod"])
async def test_every_protected_route_is_401_without_a_token(settings, db, route_table, app_env):
    async with client_for(settings, app_env) as (app, http):
        routes = protected_routes(route_table, app)
        statuses = {route: await http.request(route[0], concrete(route[1])) for route in routes}

    assert len(routes) >= 12  # обход видит маршруты всех пунктов блока
    for route, response in statuses.items():
        assert response.status_code == 401, route
        assert isinstance(response.json()["detail"], str), route
        assert response.headers["www-authenticate"] == "Bearer", route


async def test_a_route_added_without_a_login_check_is_found_by_the_walk(settings, db, route_table):
    # Контроль самого обхода: забытая зависимость входа видна как ответ не 401.
    async with client_for(settings, "dev") as (app, http):
        app.add_api_route("/api/leaky", lambda: {"ok": True})
        found = protected_routes(route_table, app)
        statuses = {r: (await http.request(r[0], concrete(r[1]))).status_code for r in found}

    assert ("GET", "/api/leaky") in found
    assert [route for route, status in statuses.items() if status != 401] == [("GET", "/api/leaky")]


def expired_token(monkeypatch, user_id: str) -> str:
    """Токен, которому исполнилось больше семи дней: часы библиотеки подписей уходят вперёд."""
    token = SessionSigner(SESSION_SECRET).issue(user_id)
    later = time.time() + 7 * 24 * 3600 + 5
    monkeypatch.setattr(itsdangerous.timed, "time", SimpleNamespace(time=lambda: later))
    return token


CREDENTIALS = {
    "чужой секрет": lambda user_id, monkeypatch: SessionSigner(ANOTHER_SECRET).issue(user_id),
    "просроченный": lambda user_id, monkeypatch: expired_token(monkeypatch, user_id),
    "пользователя нет": lambda user_id, monkeypatch: SessionSigner(SESSION_SECRET).issue("ghost"),
    "обрезанный": lambda user_id, monkeypatch: SessionSigner(SESSION_SECRET).issue(user_id)[:-4],
    "не токен": lambda user_id, monkeypatch: "garbage",
}


@pytest.mark.parametrize("app_env", ["dev", "prod"])
@pytest.mark.parametrize("kind", CREDENTIALS)
async def test_every_protected_route_is_401_with_a_foreign_expired_or_broken_token(
    settings, db, route_table, monkeypatch, app_env, kind
):
    user_id = await add_user(db)
    token = CREDENTIALS[kind](user_id, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}

    async with client_for(settings, app_env) as (app, http):
        routes = protected_routes(route_table, app)
        responses = {
            route: await http.request(route[0], concrete(route[1]), headers=headers)
            for route in routes
        }

    for route, response in responses.items():
        assert response.status_code == 401, (kind, route)
        assert isinstance(response.json()["detail"], str), (kind, route)
        assert token not in response.text, (kind, route)


@pytest.mark.parametrize("app_env", ["dev", "prod"])
async def test_every_route_of_the_constructor_is_403_for_a_player_who_may_not_create_games(
    settings, db, route_table, app_env
):
    # Конструктор — только для создателей игр (спека Р7, §10.3). Игрок с Telegram-id вне списка
    # не создатель ни в dev, ни в prod; отказ в праве идёт раньше проверки тела и черновика.
    async with db() as session:
        user = User(tg_id=4242, name="Игрок")
        session.add(user)
        await session.commit()
    headers = {"Authorization": f"Bearer {SessionSigner(SESSION_SECRET).issue(user.id)}"}

    async with client_for(settings, app_env) as (app, http):
        routes = [
            route
            for route in protected_routes(route_table, app)
            if route[1].startswith(CREATOR_ONLY_PREFIXES)
        ]
        responses = {
            route: await http.request(route[0], concrete(route[1]), headers=headers)
            for route in routes
        }

    # Список, создание, чтение, удаление, разделы, задания, проверка, публикация, экспорт, импорт,
    # копия приключения и справочник SRD.
    assert len(routes) >= 15
    assert {
        ("POST", "/api/drafts/{draft_id}/publish"),
        ("GET", "/api/drafts/{draft_id}/export"),
        ("POST", "/api/drafts/import"),
        ("POST", "/api/adventures/{adventure_id}/draft"),
        ("GET", "/api/srd/monsters"),
    } <= set(routes)
    for route, response in responses.items():
        assert response.status_code == 403, route
        assert isinstance(response.json()["detail"], str), route


@pytest.mark.parametrize("app_env", ["dev", "prod"])
async def test_a_good_token_passes_the_login_check_on_every_protected_route(
    settings, db, route_table, app_env
):
    # Контроль к тестам выше: они проверяют отказ, а этот — что отказ именно из-за токена.
    user_id = await add_user(db)
    headers = {"Authorization": f"Bearer {SessionSigner(SESSION_SECRET).issue(user_id)}"}

    async with client_for(settings, app_env) as (app, http):
        routes = protected_routes(route_table, app)
        responses = {
            route: await http.request(route[0], concrete(route[1]), headers=headers)
            for route in routes
        }

    for route, response in responses.items():
        assert response.status_code not in (401, 500), route
