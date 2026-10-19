"""Общие договорённости блока B6 на всех маршрутах сразу (план B6.2–B6.5, спека §9, §12).

1. Контракт ошибок: каждая ошибка 401, 403, 404, 409, 422 и 503 — `{"detail": "<строка>"}`,
   а у 422 может быть ещё `problems` (отчёт валидатора создателю игры).
2. Секреты: `SESSION_SECRET`, токен бота, подпись и сырые данные initData не попадают ни в один
   ответ и ни в одну запись журнала, а токены сессий — только в ответ входа своему владельцу
   (в том числе токен в query сокета и ошибки входа).
3. Данные мастера (тайны NPC, заметки, копия приключения, журнал хода, id скрытых выходов) не
   попадают в ответы игрокам.

Все три проверки делает `Stand.assert_clean` по ответам и журналу, накопленным за сценарий; сценарий
нарочно проходит все виды отказа. Тесты пунктов проверяют отдельные случаи, здесь они сведены
в один обход, который не зависит от того, в каком файле и в каком порядке писались маршруты.
"""

import dataclasses
import json
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from dnd_server.db.models import DmTurnLog, Message, MessageKind, Turn, TurnKind

MISSING = "00000000-0000-0000-0000-000000000000"
CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
OUTSIDER_TG_ID = 701
# Значения секретов стенда (`flows/conftest.py`): тест ищет именно их.
SESSION_SECRET = "flows-session-secret-0123456789-abcdefghij"
BOT_TOKEN = "123456789:AAF-flows-bot-token-0123456789_abcdef"
# Тайна NPC из демо-приключения: игрок видеть её не должен.
HIDDEN_FROM_PLAYERS = ("Клада в склепе, скорее всего, нет.",)


def add_blocked_adventure(content_dir: Path) -> None:
    """Копия демо, где скрытый выход ведёт в пустоту: в каталоге с признаком `blocking`."""
    folder = shutil.copytree(content_dir / "demo-crypt", content_dir / "blocked-crypt")
    manifest = folder / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("id: demo-crypt", "id: blocked-crypt", 1),
        encoding="utf-8",
    )
    cemetery = folder / "locations" / "cemetery.yaml"
    cemetery.write_text(
        cemetery.read_text(encoding="utf-8").replace("to: crypt", "to: nowhere", 1),
        encoding="utf-8",
    )


def with_hash_changed(raw: str) -> str:
    fields = dict(parse_qsl(raw))
    fields["hash"] = ("0" if fields["hash"][0] != "0" else "1") + fields["hash"][1:]
    return urlencode(fields)


async def test_every_kind_of_refusal_has_one_shape_and_nothing_secret_leaks(serve, content_dir):
    add_blocked_adventure(content_dir)
    statuses: set[int] = set()

    async with serve() as stand:

        async def expect(status, player, method, path, **options):
            response = await stand.call(player, method, path, **options)
            assert response.status_code == status, (
                method,
                path,
                response.status_code,
                response.text,
            )
            statuses.add(status)
            return response

        host, friend, stranger, latecomer = [await stand.dev_player(n) for n in (1, 2, 3, 4)]
        creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
        outsider = await stand.telegram_player(OUTSIDER_TG_ID, "Посторонний")
        assert (await stand.call(creator, "GET", "/api/me")).json()["can_create_games"] is True

        # 401: нет токена, чужой токен, нет данных входа, поддельные данные входа.
        await expect(401, None, "GET", "/api/me")
        await expect(401, None, "GET", "/api/me", headers={"Authorization": "Bearer x"})
        await expect(401, None, "POST", "/api/auth/telegram")
        forged = with_hash_changed(stand.init_data_for(CREATOR_TG_ID, "Создатель"))
        stand.init_data.append(forged)
        await expect(
            401,
            None,
            "POST",
            "/api/auth/telegram",
            headers={"Authorization": f"tma {forged}"},
        )

        # Каталог и создание игры: 200, 201, 403, 404, 422 (в том числе отчёт валидатора).
        catalog = (await expect(200, host, "GET", "/api/adventures")).json()
        assert {a["id"]: a["blocking"] for a in catalog} == {
            "demo-crypt": False,
            "blocked-crypt": True,
        }
        await expect(403, outsider, "POST", "/api/games", json={"adventure_id": "demo-crypt"})
        await expect(404, host, "POST", "/api/games", json={"adventure_id": "nope"})
        await expect(
            422,
            host,
            "POST",
            "/api/games",
            json={"adventure_id": "demo-crypt", "collect_seconds": 5},
        )
        report = await expect(
            422, host, "POST", "/api/games", json={"adventure_id": "blocked-crypt"}
        )
        assert report.json()["problems"], "создатель получает отчёт валидатора"
        await expect(422, None, "POST", "/api/auth/dev", json={"login": "dev-0"})
        game = (
            await expect(201, creator, "POST", "/api/games", json={"adventure_id": "demo-crypt"})
        ).json()
        gid = game["id"]
        # Игру создал Telegram-создатель; dev-игроки host и friend входят в неё по коду.
        for player in (host, friend):
            await expect(200, player, "POST", "/api/games/join", json={"code": game["code"]})

        # Вход по коду, готовность, просмотр игры: 404, 409, 422.
        await expect(404, friend, "POST", "/api/games/join", json={"code": "ZZZZZZ"})
        await expect(422, friend, "POST", "/api/games/join", json={"code": "abc"})
        await expect(404, stranger, "GET", f"/api/games/{gid}")
        await expect(404, stranger, "GET", f"/api/games/{MISSING}")
        await expect(409, friend, "POST", f"/api/games/{gid}/ready", json={"ready": True})
        await expect(422, friend, "POST", f"/api/games/{gid}/ready", json={"ready": "yes"})

        # Персонажи: 200, 201, 404, 422 (форма и правила), 503 (нейронка не настроена).
        await expect(200, friend, "GET", "/api/content/creation-options")
        await expect(200, friend, "POST", f"/api/games/{gid}/characters/roll-abilities")
        await expect(404, stranger, "POST", f"/api/games/{gid}/characters/roll-abilities")
        forged_hero = {
            "mode": "quick",
            "class_id": "rogue",
            "species": "halfling",
            "concept": "x",
            "hp_max": 999,
        }
        await expect(422, friend, "POST", f"/api/games/{gid}/characters", json=forged_hero)
        rules = await expect(
            422,
            friend,
            "POST",
            f"/api/games/{gid}/characters",
            json={**forged_hero, "class_id": "nope", "hp_max": None},
        )
        assert isinstance(rules.json()["detail"], str)
        await expect(422, friend, "POST", f"/api/games/{gid}/characters/backstory-help", json={})
        for player, make in (
            (creator, stand.quick),
            (friend, stand.detailed),
            (host, stand.random),
        ):
            response = await make(player, gid)
            assert response.status_code == 201, response.text
        working_settings = stand.app.state.settings
        stand.app.state.settings = working_settings.model_copy(update={"fake_llm_scenario": None})
        await expect(
            503,
            friend,
            "POST",
            f"/api/games/{gid}/characters/backstory-help",
            json={"class_id": "rogue", "species": "halfling"},
        )
        stand.app.state.settings = working_settings

        # Старт: 403 не хосту, 409 пока не все готовы и после старта.
        await expect(403, friend, "POST", f"/api/games/{gid}/start")
        await expect(409, creator, "POST", f"/api/games/{gid}/start")
        for player in (creator, friend, host):
            await expect(200, player, "POST", f"/api/games/{gid}/ready", json={"ready": True})
        await expect(200, creator, "POST", f"/api/games/{gid}/start")
        await expect(409, latecomer, "POST", "/api/games/join", json={"code": game["code"]})
        await expect(409, friend, "POST", f"/api/games/{gid}/characters/roll-abilities")
        await expect(409, creator, "POST", f"/api/games/{gid}/start")

        # Сокеты: токен в query участника, отказы 4401 и 4403 — и ни следа токена потом.
        async with AsyncExitStack() as stack:
            await stand.watch(stack, gid, friend)
        for token, code in ((stranger.token, 4403), ("garbage", 4401)):
            async with connect(stand.ws_url(gid, token)) as ws:
                with pytest.raises(ConnectionClosed) as closed:
                    await ws.recv()
            assert closed.value.rcvd.code == code

        assert statuses >= {200, 201, 401, 403, 404, 409, 422, 503}
        # Журнал не пуст: в нём есть запись об отказе во входе, которую тоже проверяет assert_clean.
        assert "Вход через Telegram отклонён" in stand.log_text()
        stand.assert_clean()


async def test_the_master_log_and_the_secrets_of_other_players_never_reach_a_player(stand):
    host, friend = [await stand.dev_player(n) for n in (1, 2)]
    game = await stand.new_game(host)
    gid = game["id"]
    await stand.join(friend, game["code"])
    # Строки, которые в блоке B7 запишет мастер: журнал хода и сообщения игрокам.
    async with stand.db() as session:
        turn = Turn(game_id=gid, number=1, kind=TurnKind.ROUND)
        session.add(turn)
        await session.flush()
        session.add_all(
            [
                DmTurnLog(
                    game_id=gid,
                    turn_id=turn.id,
                    attempt=1,
                    provider="fake",
                    request={"system": "ТАЙНА-промпт"},
                    trace=[{"name": "get_npc", "result": {"secret": "ТАЙНА-секрет-npc"}}],
                    output="ТАЙНА-итог",
                    error="ТАЙНА-ошибка",
                ),
                Message(
                    game_id=gid, audience="all", kind=MessageKind.NARRATION, text="Общий рассказ."
                ),
                Message(
                    game_id=gid, audience=host.id, kind=MessageKind.PRIVATE, text="ЛИЧНОЕ-хоста"
                ),
                Message(
                    game_id=gid, audience=friend.id, kind=MessageKind.PRIVATE, text="ЛИЧНОЕ-друга"
                ),
            ]
        )
        await session.commit()

    # Все чтения REST игрока и его снимок: журнала мастера нет нигде, чужого личного — тоже.
    for player, other in ((host, "ЛИЧНОЕ-друга"), (friend, "ЛИЧНОЕ-хоста")):
        for path in ("/api/me", "/api/games", f"/api/games/{gid}", "/api/adventures"):
            response = await stand.call(player, "GET", path)
            assert response.status_code == 200, path
            assert "ТАЙНА-" not in response.text and other not in response.text, path
        view = await stand.snapshot(gid, player)
        assert [item["text"] for item in view["feed"]] == [
            "Общий рассказ.",
            "ЛИЧНОЕ-хоста" if player is host else "ЛИЧНОЕ-друга",
        ]
        assert "ТАЙНА-" not in json.dumps(view, ensure_ascii=False)
    stand.assert_clean()


# Тела, которые клиент не должен слать, но может: не объект, битый JSON, чужие типы, огромные и
# глубокие. Ответ на каждое — ошибка в формате блока, но не 500.
MALFORMED_BODIES = {
    "null": b"null",
    "массив": b"[]",
    "строка": b'"text"',
    "число": b"123",
    "пусто": b"",
    "не UTF-8": b"\xff\xfe\x00{",
    "обрыв": b'{"mode": "quick"',
    "глубокий": b"[" * 5000 + b"]" * 5000,
    "огромное число": b'{"ready": 1' + b"0" * 5000 + b"}",
    "NaN": b'{"ready": NaN}',
    "широкий": json.dumps({f"k{i}": i for i in range(5000)}).encode(),
    "режим не строка": b'{"mode": {"x": 1}}',
    "код списком": b'{"code": ["ABC123"]}',
    "логин числом": b'{"login": 1}',
}


async def test_malformed_bodies_never_cause_a_server_error_on_any_post_route(stand, route_table):
    host = await stand.dev_player(1)
    gid = (await stand.new_game(host))["id"]
    routes = [
        route.path.replace("{game_id}", gid)
        for route in route_table(stand.app)
        if route.method == "POST" and route.path.startswith("/api/")
    ]
    assert len(routes) >= 8  # входы, игры, готовность, старт, персонажи
    for path in routes:
        who = None if path.startswith("/api/auth/") else host
        for name, body in MALFORMED_BODIES.items():
            for content_type in ("application/json", "text/plain", None):
                headers = {} if content_type is None else {"Content-Type": content_type}
                response = await stand.call(who, "POST", path, content=body, headers=headers)
                where = (path, name, content_type, response.status_code)
                assert response.status_code < 500, where
                if response.status_code >= 400:
                    assert isinstance(response.json()["detail"], str), where
    stand.assert_clean()


# --- сама проверка не пустая ---


def fake_response(status: int, text: str, path: str = "/api/games"):
    request = httpx.Request("GET", f"http://t{path}")
    return httpx.Response(status, text=text, request=request)


LEAKS = {
    "секрет сессий в ответе": lambda s: fake_response(200, f"x {SESSION_SECRET} x"),
    "токен бота в ответе": lambda s: fake_response(200, BOT_TOKEN),
    "секретная часть токена бота": lambda s: fake_response(400, BOT_TOKEN.partition(":")[2]),
    "токен сессии не в ответе входа": lambda s: fake_response(200, s.tokens[0]),
    "подпись initData": lambda s: fake_response(200, s.init_data[1]),
    "ошибка списком, как у FastAPI": lambda s: fake_response(422, '{"detail": [{"loc": []}]}'),
    "ошибка без detail": lambda s: fake_response(404, '{"error": "нет"}'),
    "ошибка не JSON": lambda s: fake_response(500, "Internal Server Error"),
    "problems у 404": lambda s: fake_response(404, '{"detail": "нет", "problems": ["x"]}'),
    "тайна NPC в ответе": lambda s: fake_response(200, HIDDEN_FROM_PLAYERS[0]),
    "копия приключения в ответе": lambda s: fake_response(200, '{"adventure_data": {}}'),
}


@pytest.mark.parametrize("leak", LEAKS)
async def test_the_checker_catches_each_kind_of_leak_it_is_looking_for(stand, leak):
    await stand.telegram_player(CREATOR_TG_ID, "Создатель")
    stand.assert_clean()  # до подмены всё чисто

    stand.responses.append(LEAKS[leak](stand))

    with pytest.raises(AssertionError):
        stand.assert_clean()


@pytest.mark.parametrize("secret", [SESSION_SECRET, BOT_TOKEN, "token"])
async def test_the_checker_catches_a_secret_in_the_log(stand, secret):
    await stand.dev_player(1)
    token = stand.tokens[0]
    leaky = dataclasses.replace(
        stand, log_text=lambda: f"запрос ?token={token if secret == 'token' else secret}"
    )

    with pytest.raises(AssertionError):
        leaky.assert_clean()
