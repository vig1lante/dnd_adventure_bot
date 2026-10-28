"""Чужая игра для игрока — несуществующая, на каждом маршруте и в сокете (инвариант «г»).

Тесты пунктов проверяют это по месту (лобби, персонажи, снимок). Здесь обход всех маршрутов, где в
пути есть id игры: посторонний получает 404 с тем же телом, что и для игры, которой нет, и ничего в
игре не меняется. Список маршрутов берётся из приложения, а тела запросов — из таблицы ниже:
новый маршрут игры без записи в таблице останавливает тест, а не пропускается.
"""

import pytest
from sqlalchemy import func, select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from dnd_server.db.models import GamePlayer, PlayerCharacter

MISSING_GAME = "00000000-0000-0000-0000-000000000000"
# Тело каждого маршрута игры: верное по форме, чтобы отказ был по игре, а не по запросу.
BODIES = {
    ("GET", "/api/games/{game_id}"): None,
    ("POST", "/api/games/{game_id}/ready"): {"ready": True},
    ("POST", "/api/games/{game_id}/start"): None,
    ("POST", "/api/games/{game_id}/characters/random-abilities"): {"class_id": "rogue"},
    ("GET", "/api/games/{game_id}/characters/mine"): None,
    ("POST", "/api/games/{game_id}/characters"): {
        "mode": "quick",
        "class_id": "rogue",
        "species": "halfling",
        "concept": "Карманник, который мечтает о честной жизни",
    },
    ("POST", "/api/games/{game_id}/characters/random-preview"): None,
    ("POST", "/api/games/{game_id}/characters/backstory-help"): {
        "class_id": "rogue",
        "species": "halfling",
        "concept": "Карманник",
        "draft": "",
    },
    ("PUT", "/api/games/{game_id}/action"): {"text": "Ход", "secret": False, "inspiration": False},
    ("POST", "/api/games/{game_id}/action/submit"): None,
    ("POST", "/api/games/{game_id}/action/pass"): None,
    ("POST", "/api/games/{game_id}/host/collect"): None,
    ("POST", "/api/games/{game_id}/host/extend"): None,
    ("POST", "/api/games/{game_id}/host/close"): None,
    ("POST", "/api/games/{game_id}/host/pause"): None,
    ("POST", "/api/games/{game_id}/host/resume"): None,
    ("POST", "/api/games/{game_id}/host/retry"): None,
    ("POST", "/api/games/{game_id}/host/regenerate"): None,
    ("POST", "/api/games/{game_id}/host/hurry"): None,
    ("POST", "/api/games/{game_id}/host/camp-extend"): None,
    ("POST", "/api/games/{game_id}/host/camp-end"): None,
    ("POST", "/api/games/{game_id}/host/rest"): {"kind": "short"},
    ("POST", "/api/games/{game_id}/host/rest-extend"): None,
    ("POST", "/api/games/{game_id}/host/rest-end"): None,
    ("POST", "/api/games/{game_id}/rest/hit-dice"): None,
    ("POST", "/api/games/{game_id}/camp/line"): {"text": "Подкину дров в костёр."},
    ("POST", "/api/games/{game_id}/leave"): None,
    ("POST", "/api/games/{game_id}/players/{user_id}/kick"): None,
}


async def row_counts(stand) -> dict[str, int]:
    async with stand.db() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (PlayerCharacter, GamePlayer)
        }


def game_routes(stand, route_table) -> list[tuple[str, str]]:
    found = [
        (route.method, route.path)
        for route in route_table(stand.app)
        if "{game_id}" in route.path and route.path.startswith("/api/")
    ]
    unknown = sorted(set(found) - set(BODIES))
    assert not unknown, f"добавьте тело запроса в BODIES для новых маршрутов игры: {unknown}"
    assert set(found) == set(BODIES)  # и таблица не хранит маршрутов, которых уже нет
    return found


async def test_a_stranger_gets_the_same_404_as_for_a_missing_game_on_every_route(
    stand, route_table
):
    host, friend, stranger = [await stand.dev_player(n) for n in (1, 2, 3)]
    game = await stand.new_game(host)
    await stand.join(friend, game["code"])
    before = await row_counts(stand)
    seen_identifiers = (game["id"], game["code"])

    for method, template in game_routes(stand, route_table):
        body = BODIES[(method, template)]
        options = {} if body is None else {"json": body}
        # `user_id` нужен только маршруту исключения: целью берём участника той же игры.
        foreign_url = template.format(game_id=game["id"], user_id=friend.id)
        missing_url = template.format(game_id=MISSING_GAME, user_id=friend.id)
        foreign = await stand.call(stranger, method, foreign_url, **options)
        missing = await stand.call(stranger, method, missing_url, **options)
        assert (foreign.status_code, missing.status_code) == (404, 404), (method, template)
        assert foreign.json() == missing.json(), (method, template)
        # Отказ не называет ни id, ни код чужой игры.
        assert not any(value in foreign.text for value in seen_identifiers), (method, template)

    assert await row_counts(stand) == before
    # Участник той же игры, наоборот, доходит до маршрута: 404 был именно из-за чужой игры.
    assert (await stand.game(friend, game["id"]))["id"] == game["id"]
    stand.assert_clean()


async def test_a_stranger_is_turned_away_from_the_socket_the_same_way_for_any_game(stand):
    host, stranger = [await stand.dev_player(n) for n in (1, 2)]
    game = await stand.new_game(host)

    async def close_code(game_id: str, token: str | None) -> int:
        async with connect(stand.ws_url(game_id, token)) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                await ws.recv()
        return closed.value.rcvd.code

    # Не участник — 4403 и для чужой игры, и для несуществующей; без токена и с чужим — 4401.
    assert await close_code(game["id"], stranger.token) == 4403
    assert await close_code(MISSING_GAME, stranger.token) == 4403
    assert await close_code(game["id"], None) == 4401
    assert await close_code(game["id"], "garbage") == 4401
    assert stand.app.state.hub.connections(game["id"]) == 0
    # Участнику та же игра открывается: 4403 был именно из-за членства.
    assert (await stand.snapshot(game["id"], host))["game"]["id"] == game["id"]
    stand.assert_clean()
