"""Те же сценарии в режиме prod: так сервер работает у владельца с настоящим Telegram.

Все остальные сценарии идут в dev (там есть dev-вход и внутренний API). В prod игроки входят только
через Telegram, создают игры только из списка, а описания API, dev-входа и внутренних маршрутов нет:
лобби, персонажи, сокет и перезапуск при этом работают так же.
"""

CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
FRIEND_TG_IDS = (801, 802)


async def test_a_whole_lobby_works_in_prod_with_telegram_logins_only(serve):
    async with serve(app_env="prod") as stand:
        host = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
        friends = [await stand.telegram_player(tg_id, f"Друг {tg_id}") for tg_id in FRIEND_TG_IDS]
        assert (await stand.call(host, "GET", "/health")).json()["env"] == "prod"
        game = await stand.new_game(host)
        for friend in friends:
            await stand.join(friend, game["code"])
        for player, make in (
            (host, stand.detailed),
            (friends[0], stand.quick),
            (friends[1], stand.random),
        ):
            assert (await make(player, game["id"])).status_code == 201
            assert (await stand.ready(player, game["id"])).status_code == 200
        started = await stand.call(host, "POST", f"/api/games/{game['id']}/start")
        await stand.master_started(game["id"])
        snapshot = await stand.snapshot(game["id"], friends[1])

        assert started.status_code == 200 and snapshot["phase"]["phase"] == "weaving"
        assert [p["name"] for p in snapshot["game"]["players"]] == [
            "Создатель",
            "Друг 801",
            "Друг 802",
        ]
        # Друг не из списка в prod не создаёт игры, а dev-вход, описание API и мост инструментов
        # мастера не существуют вовсе: обычный 404.
        refused = await stand.call(
            friends[0], "POST", "/api/games", json={"adventure_id": "demo-crypt"}
        )
        assert refused.status_code == 403
        for method, path in (
            ("POST", "/api/auth/dev"),
            ("GET", "/openapi.json"),
            ("GET", "/docs"),
            ("GET", "/internal/dm-tools/anything/tools"),
        ):
            response = await stand.call(
                None, method, path, json={"login": "dev-1"} if method == "POST" else None
            )
            assert response.status_code == 404, path
        stand.assert_clean()


async def test_a_prod_game_survives_a_restart(serve):
    async with serve(app_env="prod") as first:
        host = await first.telegram_player(CREATOR_TG_ID, "Создатель")
        game = await first.new_game(host)
        assert (await first.quick(host, game["id"])).status_code == 201
        before = await first.game(host, game["id"])
    async with serve(app_env="prod") as second:
        second.tokens += first.tokens
        assert await second.game(host, game["id"]) == before
        assert (await second.ready(host, game["id"])).status_code == 200
        assert (
            await second.call(host, "POST", f"/api/games/{game['id']}/start")
        ).status_code == 200
