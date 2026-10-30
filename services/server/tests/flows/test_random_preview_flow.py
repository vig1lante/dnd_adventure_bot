"""Сквозные сценарии на настоящем сервере: случайный герой с предпросмотром и провайдеры.

Клиент — `httpx` и сокет игры, как у Mini App. Предпросмотр «Бросить судьбу» / «Ещё раз» не трогает
лобби (сокет молчит), «Беру» создаёт ровно показанного героя и сообщает лобби один раз, а список
провайдеров для создателей не выдаёт секретов и в prod не показывает dev-провайдеров.
`Stand.assert_clean` проверяет по накопленным ответам и журналу общие договорённости.
"""

from contextlib import AsyncExitStack

CREATOR_TG_ID = 700  # из GAME_CREATOR_TG_IDS стенда
OUTSIDER_TG_ID = 701
UNKNOWN_PREVIEW_ID = "rp_" + "0" * 16


async def test_rolling_again_and_taking_a_hero_over_the_wire(stand):
    host, friend = [await stand.dev_player(n) for n in (1, 2)]
    game = await stand.new_game(host)
    await stand.join(friend, game["code"])
    preview_url = f"/api/games/{game['id']}/characters/random-preview"

    async with AsyncExitStack() as stack:
        watcher = await stand.watch(stack, game["id"], friend)
        await watcher.drain()

        first = await stand.call(host, "POST", preview_url)
        second = await stand.call(host, "POST", preview_url)

        assert (first.status_code, second.status_code) == (200, 200)
        assert first.json()["preview_id"] != second.json()["preview_id"]
        assert await watcher.drain() == []  # предпросмотр лобби не меняет и никому не слышен
        assert (await stand.game(host, game["id"]))["players"][0]["has_character"] is False

        stale = await stand.create_character(
            host, game["id"], {"mode": "random", "preview_id": first.json()["preview_id"]}
        )
        unknown = await stand.create_character(
            host, game["id"], {"mode": "random", "preview_id": UNKNOWN_PREVIEW_ID}
        )
        assert (stale.status_code, unknown.status_code) == (409, 409)
        assert stale.json() == unknown.json()
        assert await watcher.drain() == []

        taken = await stand.create_character(
            host, game["id"], {"mode": "random", "preview_id": second.json()["preview_id"]}
        )
        events = await watcher.drain()

    assert taken.status_code == 201, taken.text
    shown, created = second.json()["character"], taken.json()["character"]
    assert taken.json()["flavor_error"] is None
    assert created["id"] == shown["id"]
    for field in ("class_id", "species", "background", "scores", "hp_max", "inventory", "cantrips"):
        assert created[field] == shown[field], field
    assert created["name"] != shown["name"]  # имя дописала нейронка уже после «Беру»
    # Кадр лобби при сохранении героя и ещё один после описания — с именем от нейронки.
    assert [event["type"] for event in events] == ["lobby", "lobby"]
    saved, described = (
        next(p for p in event["game"]["players"] if p["user_id"] == host.id) for event in events
    )
    assert saved["has_character"] is True
    assert described["hero_name"] == created["name"]
    stand.assert_clean()


async def test_a_friend_cannot_take_the_preview_of_the_host(stand):
    host, friend = [await stand.dev_player(n) for n in (1, 2)]
    game = await stand.new_game(host)
    await stand.join(friend, game["code"])
    shown = await stand.call(host, "POST", f"/api/games/{game['id']}/characters/random-preview")

    refused = await stand.create_character(
        friend, game["id"], {"mode": "random", "preview_id": shown.json()["preview_id"]}
    )

    assert refused.status_code == 409
    assert (await stand.game(friend, game["id"]))["players"][1]["has_character"] is False
    stand.assert_clean()


async def test_providers_in_prod_are_for_creators_and_show_no_dev_providers(serve):
    async with serve(app_env="prod") as stand:
        creator = await stand.telegram_player(CREATOR_TG_ID, "Создатель")
        outsider = await stand.telegram_player(OUTSIDER_TG_ID, "Посторонний")

        listed = await stand.call(creator, "GET", "/api/providers")
        refused = await stand.call(outsider, "GET", "/api/providers")
        anonymous = await stand.call(None, "GET", "/api/providers")

        assert [p["id"] for p in listed.json()["providers"]] == [
            "anthropic_api",
            "openai_compatible",
        ]
        assert (listed.status_code, refused.status_code, anonymous.status_code) == (200, 403, 401)
        stand.assert_clean()


async def test_providers_in_dev_include_the_dev_ones_and_the_fake_scenario_is_usable(stand):
    host = await stand.dev_player(1)

    listed = await stand.call(host, "GET", "/api/providers")

    by_id = {p["id"]: p for p in listed.json()["providers"]}
    assert list(by_id) == ["anthropic_api", "openai_compatible", "claude_code_cli", "fake"]
    assert by_id["fake"]["available"] is True and by_id["fake"]["is_default"] is True
    assert by_id["anthropic_api"]["available"] is False  # ключа в стенде нет
    # Игра с показанным провайдером создаётся.
    game = await stand.new_game(host, provider="fake")
    assert game["provider"] == "fake"
    stand.assert_clean()
