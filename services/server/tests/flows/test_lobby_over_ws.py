"""Лобби, персонажи и хаб вместе (план B6.3, B6.4, B6.5): что слышат игроки по сокету.

Сервер настоящий (стенд `flows`), действия делаются запросами REST, а клиенты сокетов подключены
всё время, как открытые Mini App у компании за столом. Событие `lobby` приходит каждому участнику
после входа, создания персонажа, готовности и старта; сводка в событии та же, что отдаёт REST, и в
ней нет ничего, кроме состава лобби: текст персонажей чужим игрокам не уходит.
"""

import asyncio
import json
import re
from contextlib import AsyncExitStack
from itertools import pairwise
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def lobby_events(watchers: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Каждый участник получил ровно `count` событий `lobby`, у всех одни и те же; их сводки игры.

    Герой, которого описывает модель (быстрый и случайный), даёт два кадра: при сохранении и после
    описания — во втором имя героя от модели (дополнение B11 §5 п. 2)."""
    frames = {name: await watcher.drain() for name, watcher in watchers.items()}
    for name, events in frames.items():
        assert [e["type"] for e in events] == ["lobby"] * count, (name, events)
    assert len({json.dumps(events) for events in frames.values()}) == 1
    return [event["game"] for event in next(iter(frames.values()))]


async def one_lobby_event(watchers: dict[str, Any]) -> dict[str, Any]:
    """Каждый участник получил ровно одно событие `lobby`, и у всех оно одно и то же."""
    [game] = await lobby_events(watchers, 1)
    return game


async def nothing_happened(watchers: dict[str, Any]) -> None:
    for name, watcher in watchers.items():
        assert await watcher.drain() == [], name


def seats(game: dict[str, Any]) -> dict[str, tuple[bool, bool]]:
    """Имя игрока -> (готов, есть персонаж)."""
    return {p["name"]: (p["ready"], p["has_character"]) for p in game["players"]}


async def test_every_member_hears_each_change_of_the_lobby_and_the_start(stand):
    host, mia, ivan = [await stand.dev_player(n) for n in (1, 2, 3)]
    game = await stand.new_game(host)
    gid = game["id"]

    async with AsyncExitStack() as stack:
        watchers = {"host": await stand.watch(stack, gid, host)}
        assert seats(watchers["host"].snapshot["game"]) == {"dev-1": (False, False)}

        # Вход по коду: событие получают те, кто уже в игре, а новичок — снимок с самим собой.
        await stand.join(mia, game["code"])
        assert seats(await one_lobby_event(watchers)) == {
            "dev-1": (False, False),
            "dev-2": (False, False),
        }
        watchers["mia"] = await stand.watch(stack, gid, mia)
        assert len(watchers["mia"].snapshot["game"]["players"]) == 2
        await stand.join(ivan, game["code"])
        assert len((await one_lobby_event(watchers))["players"]) == 3
        watchers["ivan"] = await stand.watch(stack, gid, ivan)

        # Три способа создать персонажа: событие после каждого (и ещё одно после описания от
        # модели у быстрого и случайного), без текста героя в чужих кадрах.
        for player, make, frames in (
            (mia, stand.quick, 2),
            (host, stand.detailed, 1),
            (ivan, stand.random, 2),
        ):
            response = await make(player, gid)
            assert response.status_code == 201, response.text
            *_saved, lobby = await lobby_events(watchers, frames)
            assert seats(lobby)[player.name] == (False, True)
            hero = response.json()["character"]["name"]
            assert (
                next(p for p in lobby["players"] if p["user_id"] == player.id)["hero_name"] == hero
            )
        assert all(has for _ready, has in seats(lobby).values())

        # Готовность: событие после каждой отметки.
        for number, player in enumerate((host, mia, ivan), start=1):
            assert (await stand.ready(player, gid)).status_code == 200
            lobby = await one_lobby_event(watchers)
            assert sum(ready for ready, _has in seats(lobby).values()) == number

        # Новый персонаж заменяет прежнего и сбрасывает «Готов»; это видно всем и в новом снимке.
        old_id = (await stand.snapshot(gid, mia))["character"]["id"]
        assert (await stand.random(mia, gid)).status_code == 201
        saved, described = await lobby_events(watchers, 2)
        assert seats(saved)["dev-2"] == seats(described)["dev-2"] == (False, True)
        for player in (host, mia, ivan):
            snapshot = await stand.snapshot(gid, player)
            assert seats(snapshot["game"])["dev-2"] == (False, True)
        assert (await stand.snapshot(gid, mia))["character"]["id"] != old_id

        # Пока не все готовы, старт не проходит и ничего никому не отправляет.
        refused = await stand.call(host, "POST", f"/api/games/{gid}/start")
        forbidden = await stand.call(mia, "POST", f"/api/games/{gid}/start")
        assert (refused.status_code, forbidden.status_code) == (409, 403)
        await nothing_happened(watchers)
        assert stand.hooks.started == []

        # Старт: фаза weaving у всех, мастер вызван один раз, лобби уже не изменить.
        assert (await stand.ready(mia, gid)).status_code == 200
        await one_lobby_event(watchers)
        started = await stand.call(host, "POST", f"/api/games/{gid}/start")
        assert started.status_code == 200, started.text
        assert (await one_lobby_event(watchers))["phase"] == "weaving"
        await stand.master_started(gid)
        assert stand.hooks.started == [gid]
        assert (await stand.snapshot(gid, ivan))["phase"]["phase"] == "weaving"
        assert (await stand.quick(ivan, gid)).status_code == 409
        assert (await stand.ready(ivan, gid, False)).status_code == 409
        await nothing_happened(watchers)

    stand.assert_clean()


async def test_a_players_hero_never_reaches_the_frames_of_the_others(stand):
    host, mia = [await stand.dev_player(n) for n in (1, 2)]
    game = await stand.new_game(host)
    await stand.join(mia, game["code"])
    # Имя героя — не тайна: его видят все в лобби (`hero_name`, решение D3 блока B11), как и в
    # отряде после старта. Тайна — замысел и предыстория.
    secrets = ["ЗАМЫСЕЛ-миры-4242", "ПРЕДЫСТОРИЯ-миры-4242"]
    hero_name = "ИМЯ-героя-4242"
    # Имя героя нарочно совпадает с id другого игрока и словом «все»: это текст, а не адрес.
    tricky = [host.id, "all", hero_name]

    async with AsyncExitStack() as stack:
        watchers = {
            "host": await stand.watch(stack, game["id"], host),
            "mia": await stand.watch(stack, game["id"], mia),
        }
        for name in tricky:
            response = await stand.quick(mia, game["id"], name=name, concept=f"{secrets[0]} {name}")
            assert response.status_code == 201, response.text
            for lobby in await lobby_events(watchers, 2):  # сохранение и описание от модели
                assert seats(lobby)["dev-2"] == (False, True)
                assert [p["hero_name"] for p in lobby["players"]] == [None, name]
        response = await stand.detailed(
            mia,
            game["id"],
            name=hero_name,
            persona={"concept": secrets[0], "backstory": secrets[1]},
        )
        assert response.status_code == 201, response.text
        await one_lobby_event(watchers)

        host_view = await stand.snapshot(game["id"], host)
        mia_view = await stand.snapshot(game["id"], mia)
        host_mine = await stand.my_character(host, game["id"])
        mia_mine = await stand.my_character(mia, game["id"])

    # У хозяина нет ни героя Мии, ни её текста ни в снимке, ни в кадрах сокета за всё время.
    assert host_view["character"] is None and host_view["feed"] == []
    heard = json.dumps([host_view, *watchers["host"].seen], ensure_ascii=False)
    assert not any(secret in heard for secret in secrets)
    # Выбор игрока (R10) отдаёт только `characters/mine` и только самой Мии: ни снимки, ни кадры
    # сокета (её собственные тоже) его не несут, а хозяину чужого выбора не достать.
    everything = json.dumps([host_view, mia_view, *watchers["host"].seen, *watchers["mia"].seen])
    seen_keys = set(re.findall(r'"(\w+)":', everything))
    assert not {"choices", "base_scores", "background_bonus", "method"} & seen_keys
    assert host_mine.status_code == 404 and not any(s in host_mine.text for s in secrets)
    assert mia_mine.json()["mode"] == "detailed"
    assert mia_mine.json()["choices"]["name"] == hero_name
    # Сама Мия видит своего героя целиком.
    assert mia_view["character"]["name"] == hero_name
    assert mia_view["character"]["persona"]["backstory"] == secrets[1]
    assert mia_view["character"]["player_id"] == mia.id
    stand.assert_clean()


async def test_parallel_joins_reach_the_host_as_lobby_events_in_the_order_of_the_changes(stand):
    host = await stand.dev_player(1)
    friends = [await stand.dev_player(n) for n in range(2, 7)]
    late = await stand.telegram_player(710, "Опоздавший")
    game = await stand.new_game(host)
    code = {"code": game["code"]}

    async with AsyncExitStack() as stack:
        listening = await stand.watch(stack, game["id"], host)
        joins = await asyncio.gather(
            *(stand.call(f, "POST", "/api/games/join", json=code) for f in [*friends, late])
        )
        events = await listening.drain()

    # Шестой по счёту игрок входит, седьмой получает отказ и ничего не меняет и не рассылает.
    assert sorted(response.status_code for response in joins) == [200] * 5 + [409]
    assert [e["type"] for e in events] == ["lobby"] * 5
    rosters = [[p["user_id"] for p in e["game"]["players"]] for e in events]
    assert [len(roster) for roster in rosters] == [2, 3, 4, 5, 6]
    # Каждое событие — прежний состав плюс один вошедший: события шли в порядке изменений игры.
    assert all(a == b[: len(a)] for a, b in pairwise(rosters))
    assert (await stand.game(host, game["id"]))["players"] == events[-1]["game"]["players"]
    stand.assert_clean()


async def test_the_tunnel_host_is_served_over_rest_and_the_socket_and_a_foreign_host_is_not(stand):
    # Mini App приходит через туннель под публичным именем (`WEBAPP_PUBLIC_URL` стенда); страница
    # злоумышленника ходит на тот же сервер под своим (DNS-rebinding), и Host у неё чужой.
    host = await stand.dev_player(1)
    gid = (await stand.new_game(host))["id"]
    tunnel = "app.example.test"

    allowed = await stand.call(host, "GET", "/api/me", headers={"Host": tunnel})
    refused = await stand.call(host, "GET", "/api/me", headers={"Host": "evil.example"})
    async with connect(
        f"ws://{tunnel}/ws/games/{gid}?token={host.token}", host="127.0.0.1", port=stand.port
    ) as ws:
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), 5))
    with pytest.raises(InvalidStatus) as foreign:
        async with connect(
            f"ws://evil.example/ws/games/{gid}?token={host.token}",
            host="127.0.0.1",
            port=stand.port,
        ):
            pass

    assert allowed.status_code == 200 and refused.status_code == 400
    assert snapshot["type"] == "snapshot" and snapshot["game"]["id"] == gid
    assert foreign.value.response.status_code == 400
    assert stand.app.state.hub.connections(gid) == 0
