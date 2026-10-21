"""WebSocket игры (план B6.5): вход, снимок, события, приватность, ping/pong.

Сервер настоящий: приложение крутит uvicorn на свободном порту loopback, клиент — библиотека
`websockets`, как браузер. Так видны настоящие коды закрытия (4401 и 4403 доходят до клиента только
после принятия рукопожатия) и порядок кадров. Ждать молчания нельзя, поэтому «клиенту ничего не
пришло» проверяется замком: ping идёт через ту же очередь, что и события, и `pong` замыкает
всё, что отправлено до него.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.realtime import ws as ws_module
from dnd_server.db.models import DmTurnLog, Game, GameStateSnapshot, MessageKind, User
from dnd_server.realtime.hub import Hub
from dnd_server.realtime.views import (
    audience_to_user,
    feed_item_event,
    phase_event,
    state_events,
)

SECRET_MARK = "ТАЙНА-"  # метка из conftest.py: то, что не видит никто
PONG = {"type": "pong"}
TIMEOUT_S = 5


@dataclass
class Server:
    app: FastAPI
    port: int

    @property
    def hub(self) -> Hub:
        return self.app.state.hub

    def url(self, game_id: str, token: str | None = None) -> str:
        query = "" if token is None else f"?token={token}"
        return f"ws://127.0.0.1:{self.port}/ws/games/{game_id}{query}"

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", trust_env=False)


@pytest.fixture
async def server(settings: Settings, db: async_sessionmaker[AsyncSession]) -> AsyncIterator[Server]:
    app = create_app(settings)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, log_level="warning", lifespan="off"
    )
    running = uvicorn.Server(config)
    task = asyncio.create_task(running.serve())
    async with asyncio.timeout(10):
        while not running.started:
            await asyncio.sleep(0.01)
    port = running.servers[0].sockets[0].getsockname()[1]
    yield Server(app, port)
    running.should_exit = True
    await task
    await app.state.engine.dispose()


async def recv(ws: ClientConnection) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))


async def drain(ws: ClientConnection) -> list[dict[str, Any]]:
    """Всё, что сервер отправил этому клиенту к моменту вызова (до ответа на ping)."""
    await ws.send(json.dumps({"type": "ping"}))
    seen = []
    while (message := await recv(ws)) != PONG:
        seen.append(message)
    return seen


async def close_code(url: str) -> int:
    """Код, которым сервер закрыл сокет, не пустив игрока."""
    async with connect(url) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), TIMEOUT_S)
    return closed.value.rcvd.code


@asynccontextmanager
async def watching(server: Server, table, *keys: str) -> AsyncIterator[dict[str, ClientConnection]]:
    """Игроки подключены и получили снимок; сокеты по ключам игроков. К этому моменту хаб уже
    держит их подписки, так что события, отправленные после, дойдут."""
    async with AsyncExitStack() as stack:
        sockets = {}
        for key in keys:
            url = server.url(table.game_id, table.seats[key].token)
            ws = await stack.enter_async_context(connect(url))
            assert (await recv(ws))["type"] == "snapshot"
            sockets[key] = ws
        yield sockets


# --- вход ---


async def test_snapshot_comes_first_and_events_follow(server, table):
    async with connect(server.url(table.game_id, table.alice.token)) as ws:
        snapshot = await recv(ws)
        assert snapshot["type"] == "snapshot"
        assert snapshot["game"]["id"] == table.game_id

        await server.hub.send(table.game_id, {"type": "phase", "phase": {"phase": "paused"}})

        assert await recv(ws) == {"type": "phase", "phase": {"phase": "paused"}}


async def test_event_sent_while_the_snapshot_is_being_prepared_follows_it_and_is_not_lost(
    server, table, db, monkeypatch
):
    async with db() as session:
        game = await session.get(Game, table.game_id)
    released = asyncio.Event()
    read_snapshot = ws_module.snapshot_text

    async def slow_snapshot(*args: Any) -> str:
        text = await read_snapshot(*args)
        await released.wait()  # снимок прочитан, но клиент его ещё не получил
        return text

    monkeypatch.setattr(ws_module, "snapshot_text", slow_snapshot)
    try:
        async with connect(server.url(table.game_id, table.alice.token)) as ws:
            async with asyncio.timeout(TIMEOUT_S):
                while not server.hub.connections(table.game_id):
                    await asyncio.sleep(0.005)
            await server.hub.send(table.game_id, phase_event(game, table.state))
            released.set()

            assert (await recv(ws))["type"] == "snapshot"
            assert (await recv(ws))["type"] == "phase"
    finally:
        released.set()  # при падении теста сервер не должен зависнуть на закрытии


async def test_extra_query_parameters_do_not_reach_the_code_of_the_endpoint(server, table):
    # `load_srd(path)` читает каталог по пути; как зависимость FastAPI он превратил бы `path` из
    # query в путь, выбранный клиентом. Сокет принимает из query один параметр — `token`.
    url = server.url(table.game_id, table.alice.token) + "&path=/no/such/dir&srd=1"

    async with connect(url) as ws:
        snapshot = await recv(ws)

    assert snapshot["type"] == "snapshot"
    assert snapshot["party"]  # данные SRD встроенные: названия состояний берутся из них


async def test_the_hub_is_the_broadcaster_of_the_app(server):
    # Сервисы лобби и персонажей шлют события через `app.state.broadcaster`.
    assert server.app.state.broadcaster is server.hub


@pytest.mark.parametrize("key", ["alice", "boris", "vera"])
async def test_each_player_gets_a_snapshot_without_the_secrets_of_the_others(server, table, key):
    async with connect(server.url(table.game_id, table.seats[key].token)) as ws:
        text = json.dumps(await recv(ws), ensure_ascii=False)

    assert SECRET_MARK not in text
    for other in ("alice", "boris", "vera"):
        # Ни личных сообщений, ни тайных ходов и бросков, ни листов других игроков.
        assert (f"-{other}" in text) == (other == key)


async def test_player_b_does_not_see_the_secret_move_and_private_message_of_player_a(server, table):
    async with connect(server.url(table.game_id, table.boris.token)) as ws:
        boris = await recv(ws)
    async with connect(server.url(table.game_id, table.alice.token)) as ws:
        alice = await recv(ws)

    boris_feed = [item["text"] for item in boris["feed"]]
    alice_feed = [item["text"] for item in alice["feed"]]
    assert "ХОД-boris: подкрасться" in boris_feed
    assert "ЛИЧНОЕ-boris: в кармане ключ." in boris_feed
    assert not any("boris" in text for text in alice_feed)
    assert boris["action"]["secret"] is True and alice["action"]["secret"] is False


async def test_after_reconnecting_the_snapshot_has_the_feed_including_new_messages(server, table):
    async with connect(server.url(table.game_id, table.alice.token)) as ws:
        first = await recv(ws)
        message = await table.add_message("Рассказ после коммита.")
        await server.hub.send(table.game_id, feed_item_event(message))
        event = await recv(ws)

    async with connect(server.url(table.game_id, table.alice.token)) as ws:
        again = await recv(ws)

    assert event["item"]["text"] == "Рассказ после коммита."
    assert [item["text"] for item in first["feed"]][-1] != "Рассказ после коммита."
    assert [item["text"] for item in again["feed"]][-1] == "Рассказ после коммита."
    assert again["feed"][-1]["id"] == event["item"]["id"]


@pytest.mark.parametrize("who", ["no_token", "empty", "garbage", "foreign_secret", "unknown_user"])
async def test_missing_or_bad_token_is_closed_with_4401(server, table, who):
    foreign = SessionSigner("another-session-secret-0123456789-abcdef")
    signer = SessionSigner("test-session-secret-0123456789-abcdefghij")
    token = {
        "no_token": None,
        "empty": "",
        "garbage": "garbage",
        "foreign_secret": foreign.issue(table.alice.user_id),
        "unknown_user": signer.issue("no-such-user"),
    }[who]

    assert await close_code(server.url(table.game_id, token)) == 4401
    assert server.hub.connections(table.game_id) == 0


async def test_not_a_member_is_closed_with_4403_the_same_for_a_missing_game(
    server, table, db, signer
):
    async with db() as session:
        stranger = User(dev_login="dev-9", name="Чужой")
        session.add(stranger)
        await session.commit()
    token = signer.issue(stranger.id)

    assert await close_code(server.url(table.game_id, token)) == 4403
    assert await close_code(server.url("no-such-game", token)) == 4403
    assert server.hub.connections(table.game_id) == 0


async def test_foreign_host_header_is_refused_before_the_socket_opens(server, table):
    # Страница злоумышленника ходит на localhost под своим именем (DNS-rebinding): Host чужой.
    url = server.url(table.game_id, table.alice.token).replace("127.0.0.1", "evil.example")

    with pytest.raises(InvalidStatus) as refused:
        async with connect(url, host="127.0.0.1", port=server.port):
            pass

    assert refused.value.response.status_code == 400


async def test_unreadable_game_state_is_reported_with_an_error_event_and_1011(server, table, db):
    async with db() as session:
        row = await session.get(GameStateSnapshot, (table.game_id, 3))
        row.schema_version = 999  # записан более новой версией сервера
        await session.commit()

    async with connect(server.url(table.game_id, table.alice.token)) as ws:
        error = await recv(ws)
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), TIMEOUT_S)

    assert error["type"] == "error"
    assert "детали" not in error["detail"] and "999" not in error["detail"]
    assert closed.value.rcvd.code == 1011
    assert server.hub.connections(table.game_id) == 0


async def test_a_snapshot_that_fails_is_logged_without_the_input_of_the_error(
    server, table, monkeypatch, caplog, leak
):
    async def broken_snapshot(*_args: Any, **_kwargs: Any) -> str:
        raise leak.error

    monkeypatch.setattr(ws_module, "snapshot_text", broken_snapshot)

    with caplog.at_level(logging.ERROR, logger=ws_module.__name__):
        async with connect(server.url(table.game_id, table.alice.token)) as ws:
            assert (await recv(ws))["type"] == "error"
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), TIMEOUT_S)

    [record] = caplog.records
    assert record.exc_info is None  # трассировку формирует сервер, а не logging
    assert table.game_id in record.getMessage() and "ValidationError" in record.getMessage()
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_the_hub_forgets_a_player_who_left(server, table):
    async with watching(server, table, "alice", "boris"):
        assert server.hub.connections(table.game_id) == 2

    async with asyncio.timeout(TIMEOUT_S):
        while server.hub.connections(table.game_id):
            await asyncio.sleep(0.01)


# --- ping и входящие ---


async def frames_within(ws: ClientConnection, seconds: float) -> list[dict[str, Any]]:
    """Все кадры, которые придут за `seconds` секунд; для проверки, что лишнего не пришло."""
    frames = []
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            while True:
                frames.append(json.loads(await ws.recv()))
    return frames


async def test_ping_gets_exactly_one_pong_and_everything_else_from_the_client_is_ignored(
    server, table
):
    async with watching(server, table, "alice") as sockets:
        ws = sockets["alice"]
        await ws.send("не json")
        await ws.send(json.dumps({"type": "snapshot"}))
        await ws.send(json.dumps({"type": "feed_item", "item": {"text": "подделка"}}))
        await ws.send(json.dumps([1, 2, 3]))
        await ws.send(b"\x00\x01")
        await ws.send("x" * 100_000)
        await ws.send(json.dumps({"type": "ping", "padding": "x" * 1000}))  # слишком длинный ping
        await ws.send(json.dumps({"type": "ping"}))

        assert await frames_within(ws, 0.3) == [PONG]


# --- события и адресаты ---


async def test_a_secret_event_reaches_only_its_addressee(server, table):
    async with watching(server, table, "alice", "boris", "vera") as sockets:
        secret = await table.add_message(
            "ХОД-boris: тайный бросок", audience=table.boris.user_id, kind=MessageKind.ROLL
        )
        await server.hub.send(table.game_id, feed_item_event(secret), secret.audience)
        public = await table.add_message("Общий рассказ.")
        await server.hub.send(table.game_id, feed_item_event(public), public.audience)

        boris = await drain(sockets["boris"])
        alice = await drain(sockets["alice"])
        vera = await drain(sockets["vera"])

    assert [e["item"]["text"] for e in boris] == ["ХОД-boris: тайный бросок", "Общий рассказ."]
    assert (
        [e["item"]["text"] for e in alice]
        == [e["item"]["text"] for e in vera]
        == ["Общий рассказ."]
    )


async def test_two_tabs_of_one_player_both_get_the_events(server, table, db):
    async with db() as session:
        game = await session.get(Game, table.game_id)
    async with (
        connect(server.url(table.game_id, table.alice.token)) as tab_1,
        connect(server.url(table.game_id, table.alice.token)) as tab_2,
    ):
        await recv(tab_1)
        await recv(tab_2)

        await server.hub.send(table.game_id, phase_event(game, table.state))

        assert [e["type"] for e in await drain(tab_1)] == ["phase"]
        assert [e["type"] for e in await drain(tab_2)] == ["phase"]


async def test_character_id_equal_to_the_user_id_of_another_player_gives_the_secret_to_the_owner_only(
    server, table
):
    # Серверу так id не выдают, но проверка адресата не должна зависеть от этого: персонаж Алисы
    # получил строку, совпавшую с user_id Бориса; тайное «персонажу Бориса-строки» читает Алиса.
    clash = table.alice.character.model_copy(update={"id": table.boris.user_id})
    audience = audience_to_user({clash.id: clash}, table.boris.user_id)
    secret = await table.add_message(
        "ЛИЧНОЕ-для-владельца", audience=audience, kind=MessageKind.PRIVATE
    )
    async with watching(server, table, "alice", "boris") as sockets:
        await server.hub.send(table.game_id, feed_item_event(secret), secret.audience)

        assert [e["item"]["text"] for e in await drain(sockets["alice"])] == [
            "ЛИЧНОЕ-для-владельца"
        ]
        assert await drain(sockets["boris"]) == []


async def test_state_events_reach_players_without_secrets_and_with_only_their_own_sheet(
    server, table, srd
):
    async with watching(server, table, "alice", "boris", "vera") as sockets:
        for payload, audience in state_events(table.state, srd, table.adventure):
            await server.hub.send(table.game_id, payload, audience)

        received = {key: await drain(ws) for key, ws in sockets.items()}

    for key, events in received.items():
        assert [e["type"] for e in events] == ["party", "combat", "character"]
        text = json.dumps(events, ensure_ascii=False)
        assert SECRET_MARK not in text
        assert events[2]["character"]["id"] == table.seats[key].character.id
        for other in ("alice", "boris", "vera"):
            assert (f"ПРЕДЫСТОРИЯ-{other}" in text) == (other == key)


async def test_nothing_of_the_dm_turn_log_or_adventure_copy_is_ever_sent(server, table, db, srd):
    async with db() as session:
        logs = (await session.scalars(select(DmTurnLog))).all()
    assert len(logs) == 1 and SECRET_MARK in json.dumps(logs[0].trace, ensure_ascii=False)

    async with watching(server, table, "alice", "boris") as sockets:
        for payload, audience in state_events(table.state, srd, table.adventure):
            await server.hub.send(table.game_id, payload, audience)
        frames = [await drain(ws) for ws in sockets.values()]

    assert SECRET_MARK not in json.dumps(frames, ensure_ascii=False)


# --- лобби через хаб ---


async def test_lobby_event_arrives_through_the_hub_after_join_and_ready(
    server, lobby_table, db, signer
):
    async with db() as session:
        newcomer = User(dev_login="dev-4", name="Новичок")
        session.add(newcomer)
        await session.commit()
    headers = {"Authorization": f"Bearer {signer.issue(newcomer.id)}"}

    async with (
        watching(server, lobby_table, "alice") as sockets,
        server.http() as http,
    ):
        joined = await http.post("/api/games/join", json={"code": "ABC123"}, headers=headers)
        vera_headers = {"Authorization": f"Bearer {lobby_table.vera.token}"}
        ready = await http.post(
            f"/api/games/{lobby_table.game_id}/ready", json={"ready": True}, headers=vera_headers
        )
        events = await drain(sockets["alice"])

    assert joined.status_code == ready.status_code == 200
    assert [e["type"] for e in events] == ["lobby", "lobby"]
    assert [p["name"] for p in events[0]["game"]["players"]] == [
        "Алиса",
        "Борис",
        "Вера",
        "Новичок",
    ]
    assert events[1]["game"]["players"][2]["ready"] is True


# --- вышедший и исключённый игрок ---


async def test_a_player_kicked_from_the_lobby_is_closed_and_cannot_come_back_to_the_socket(
    server, lobby_table
):
    boris, alice = lobby_table.boris, lobby_table.alice
    async with (
        watching(server, lobby_table, "alice", "boris") as sockets,
        server.http() as http,
    ):
        kicked = await http.post(
            f"/api/games/{lobby_table.game_id}/players/{boris.user_id}/kick",
            headers={"Authorization": f"Bearer {alice.token}"},
        )
        host_events = await drain(sockets["alice"])
        with pytest.raises(ConnectionClosed) as closed:
            async with asyncio.timeout(TIMEOUT_S):
                while True:
                    await sockets["boris"].recv()  # кадров игры исключённому больше нет
        connections_left = server.hub.connections(lobby_table.game_id)

    assert kicked.status_code == 200
    assert [e["type"] for e in host_events] == ["lobby"]
    assert closed.value.rcvd.code == 1013  # «переподключитесь»: вход уже закрыт
    assert connections_left == 1  # остался только хост
    assert await close_code(server.url(lobby_table.game_id, boris.token)) == 4403
