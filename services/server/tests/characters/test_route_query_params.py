"""У маршрутов сервера нет параметров query, которых не просили (находка security B6.5).

FastAPI превращает каждый аргумент функции-зависимости в параметр query. Так `Depends(load_srd)`
сделал из `load_srd(path=None)` параметр `?path=`: игрок выбирал каталог данных SRD. Зависимости
должны быть функциями без аргументов или с аргументами только `Request`/`WebSocket` и `Depends`.
Если маршруту query нужен на самом деле, его добавляют в `ALLOWED_QUERY` вместе с причиной.
"""

import pytest
from fastapi.dependencies.models import Dependant

from dnd_server.app import create_app
from dnd_server.config import Settings

# (метод, путь) -> допустимые параметры query. Сейчас таких нет ни у одного маршрута: токен сокета
# игры читается из query вручную, а не объявляется параметром.
ALLOWED_QUERY: dict[tuple[str, str], set[str]] = {}


def query_names(dependant: Dependant) -> set[str]:
    """Имена параметров query самой функции маршрута и всех её зависимостей."""
    names = {param.name for param in dependant.query_params}
    for dependency in dependant.dependencies:
        names |= query_names(dependency)
    return names


def declared_query(app_settings: Settings) -> dict[tuple[str, str], set[str]]:
    """Параметры query каждого описанного в OpenAPI маршрута приложения."""
    paths = create_app(app_settings).openapi()["paths"]
    return {
        (method.upper(), path): {
            p["name"] for p in operation.get("parameters", []) if p["in"] == "query"
        }
        for path, operations in paths.items()
        for method, operation in operations.items()
    }


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_no_documented_route_declares_unexpected_query_parameters(env):
    declared = declared_query(Settings(app_env=env, session_secret="s" * 40))

    assert declared  # приложение собралось и маршруты на месте
    for route, names in declared.items():
        assert names <= ALLOWED_QUERY.get(route, set()), f"{route}: лишний query {names}"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_no_mounted_route_has_query_parameters_in_its_dependencies(env, route_table):
    # WebSocket и внутренний API в OpenAPI не попадают, поэтому зависимости смотрим напрямую.
    routes = route_table(create_app(Settings(app_env=env, session_secret="s" * 40)))

    assert routes
    for route in routes:
        key = (route.method, route.path)
        assert query_names(route.dependant) <= ALLOWED_QUERY.get(key, set()), key


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_the_walk_sees_the_routers_of_every_part_of_the_block(env, route_table):
    # Обход бессмыслен, если он не видит роутеров: здесь перечислены по одному маршруту из каждого.
    app = create_app(Settings(app_env=env, session_secret="s" * 40))
    paths = {route.path for route in route_table(app)}

    always = {
        "/health",
        "/api/me",
        "/api/config",
        "/api/auth/telegram",
        "/api/adventures",
        "/api/games",
        "/api/games/join",
        "/api/games/{game_id}/ready",
        "/api/games/{game_id}/start",
        "/api/content/creation-options",
        "/api/games/{game_id}/characters",
        "/api/games/{game_id}/characters/roll-abilities",
        "/api/games/{game_id}/characters/backstory-help",
        "/ws/games/{game_id}",
    }
    only_in_dev = {
        "/api/auth/dev",
        "/internal/dm-tools/{token}/tools",
        "/internal/dm-tools/{token}/call",
    }
    assert always <= paths
    if env == "dev":
        assert only_in_dev <= paths
    else:
        assert not only_in_dev & paths  # в prod нет ни dev-входа, ни внутреннего API
