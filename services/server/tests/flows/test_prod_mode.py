"""Те же сценарии в режиме prod: так сервер работает у владельца с настоящим Telegram.

Все остальные сценарии идут в dev (там есть dev-вход и внутренний API). В prod игроки входят только
через Telegram, создают игры только из списка, а описания API, dev-входа и внутренних маршрутов нет:
лобби, персонажи, сокет и перезапуск при этом работают так же.

Тестового мастера `fake` в prod нет, поэтому стенд настроен на настоящую модель (`anthropic_api` с
выдуманным ключом), а её сборку подменяет двойник — `FakeProvider` со сценарием стенда
(`real_master_stand_in`). Живая модель не вызывается: сеть в тестах закрыта.
"""

import pytest
from pydantic import SecretStr

from dnd_server.dm import provider as provider_module
from dnd_server.llm.fake import FakeProvider, load_scenario

CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
FRIEND_TG_IDS = (801, 802)
# Настройки «настоящей модели» стенда: ключ выдуманный, до сети дело не доходит.
PROD_MASTER = {
    "app_env": "prod",
    "llm_provider": "anthropic_api",
    "anthropic_api_key": SecretStr("sk-ant-prod-stand-not-a-real-key"),
}


@pytest.fixture(autouse=True)
def real_master_stand_in(monkeypatch, settings):
    """Сборка провайдера игры отдаёт `FakeProvider` со сценарием стенда вместо настоящей модели."""

    def make(_settings, **options):
        return FakeProvider(load_scenario(settings.fake_llm_scenario), options.get("pc_ids", ()))

    monkeypatch.setattr(provider_module, "make_provider", make)


async def test_a_whole_lobby_works_in_prod_with_telegram_logins_only(serve):
    async with serve(**PROD_MASTER) as stand:
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
    async with serve(**PROD_MASTER) as first:
        host = await first.telegram_player(CREATOR_TG_ID, "Создатель")
        game = await first.new_game(host)
        assert (await first.quick(host, game["id"])).status_code == 201
        before = await first.game(host, game["id"])
    async with serve(**PROD_MASTER) as second:
        second.tokens += first.tokens
        assert await second.game(host, game["id"]) == before
        assert (await second.ready(host, game["id"])).status_code == 200
        assert (
            await second.call(host, "POST", f"/api/games/{game['id']}/start")
        ).status_code == 200
