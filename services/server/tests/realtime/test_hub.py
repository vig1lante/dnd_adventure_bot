"""Хаб сокетов (план B6.5): кому уходят события, порядок, мёртвые и медленные сокеты.

Сокеты здесь подставные: хабу нужен только `send_text`. Настоящие сокеты и сервер проверяет
`test_ws.py`.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from dnd_server.realtime import hub as hub_module
from dnd_server.realtime.hub import EVENT_TYPES, Connection, Hub

GAME = "game-1"
OTHER_GAME = "game-2"


class FakeSocket:
    """Подставной сокет: записывает отправленное; может быть мёртвым или не принимать данные."""

    def __init__(self, *, dead: bool = False, stuck: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._dead = dead
        self._stuck = stuck
        self.cancelled = False

    async def send_text(self, text: str) -> None:
        if self._dead:
            raise RuntimeError("сокет уже закрыт")
        if self._stuck:
            try:
                await asyncio.Event().wait()  # клиент не читает: отправка не завершается
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        self.sent.append(json.loads(text))


def event(kind: str = "phase", **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def join(hub: Hub, game_id: str, user_id: str, socket: FakeSocket, first: Any = None) -> Connection:
    """Подключает сокет и сразу запускает отправку, как это делает WS-эндпоинт после снимка."""
    connection = hub.connect(game_id, user_id, socket)
    connection.start(first)
    return connection


async def test_event_for_all_reaches_every_socket_of_the_game_and_no_other_game():
    hub = Hub()
    alice, boris, stranger = FakeSocket(), FakeSocket(), FakeSocket()
    join(hub, GAME, "alice", alice)
    join(hub, GAME, "boris", boris)
    join(hub, OTHER_GAME, "alice", stranger)

    await hub.send(GAME, event(round=1))

    await until(lambda: alice.sent and boris.sent)
    assert alice.sent == boris.sent == [event(round=1)]
    assert stranger.sent == []


async def test_event_for_a_user_reaches_only_that_user_including_all_their_tabs():
    hub = Hub()
    tab_1, tab_2, boris = FakeSocket(), FakeSocket(), FakeSocket()
    join(hub, GAME, "alice", tab_1)
    join(hub, GAME, "alice", tab_2)
    join(hub, GAME, "boris", boris)

    await hub.send(GAME, event("character", hp=5), audience="alice")
    await hub.send(GAME, event(round=2))  # общее событие идёт следом: за ним сверяем порядок

    await until(lambda: len(boris.sent) == 1 and len(tab_1.sent) == 2 and len(tab_2.sent) == 2)
    assert boris.sent == [event(round=2)]
    assert tab_1.sent == tab_2.sent == [event("character", hp=5), event(round=2)]


@pytest.mark.parametrize("audience", ["nobody", "", "ALL", "alice "])
async def test_unknown_audience_gets_the_event_to_no_one_rather_than_to_everyone(audience):
    hub = Hub()
    alice = FakeSocket()
    join(hub, GAME, "alice", alice)

    await hub.send(GAME, event("character", hp=5), audience=audience)
    await hub.send(GAME, event(round=3))

    await until(lambda: alice.sent)
    assert alice.sent == [event(round=3)]


async def test_events_sent_during_the_snapshot_wait_and_follow_it_in_order():
    hub = Hub()
    socket = FakeSocket()
    connection = hub.connect(GAME, "alice", socket)  # подписка есть, снимок ещё читается
    await hub.send(GAME, event(round=1))
    await hub.send(GAME, event(round=2))
    await asyncio.sleep(0.02)
    assert socket.sent == []  # до снимка клиент не получает ничего

    connection.start(json.dumps({"type": "snapshot"}))

    await until(lambda: len(socket.sent) == 3)
    assert socket.sent == [{"type": "snapshot"}, event(round=1), event(round=2)]


async def test_dead_socket_is_dropped_silently_and_the_others_keep_receiving():
    hub = Hub()
    dead, alive = FakeSocket(dead=True), FakeSocket()
    join(hub, GAME, "alice", dead)
    join(hub, GAME, "boris", alive)

    await hub.send(GAME, event(round=1))  # не бросает

    await until(lambda: hub.connections(GAME) == 1)
    await hub.send(GAME, event(round=2))
    await until(lambda: len(alive.sent) == 2)
    assert alive.sent == [event(round=1), event(round=2)]


async def test_a_socket_that_stops_reading_does_not_block_the_sender_and_is_dropped():
    hub = Hub(send_timeout=0.05)
    stuck, alive = FakeSocket(stuck=True), FakeSocket()
    join(hub, GAME, "alice", stuck)
    join(hub, GAME, "boris", alive)

    # Отправитель держит блокировку игры: ни одна отправка не должна ждать медленного сокета.
    async with asyncio.timeout(0.5):
        for number in range(5):
            await hub.send(GAME, event(round=number))

    await until(lambda: len(alive.sent) == 5)
    await until(lambda: hub.connections(GAME) == 1)  # по тайм-ауту отправки
    assert stuck.cancelled
    assert [e["round"] for e in alive.sent] == [0, 1, 2, 3, 4]


async def test_a_full_queue_drops_the_connection_so_the_client_reconnects_for_a_snapshot():
    hub = Hub(queue_limit=3, send_timeout=30)
    stuck, alive = FakeSocket(stuck=True), FakeSocket()
    join(hub, GAME, "alice", stuck)
    join(hub, GAME, "boris", alive)

    async with asyncio.timeout(0.5):
        for number in range(8):
            await hub.send(GAME, event(round=number))
            await asyncio.sleep(0)

    # Таймаут отправки ещё далеко, а соединение уже снято: очередь переполнилась.
    assert hub.connections(GAME) == 1
    await until(lambda: stuck.cancelled)
    await until(lambda: len(alive.sent) == 8)


async def test_a_connection_dropped_before_its_writer_started_never_starts_it():
    # Очередь переполнилась, пока читался снимок: сокет, которого хаб уже не знает, событий
    # не получит, и снимок ему отдавать нельзя — клиент должен переподключиться.
    hub = Hub(queue_limit=1)
    socket = FakeSocket()
    connection = hub.connect(GAME, "alice", socket)
    await hub.send(GAME, event(round=1))
    await hub.send(GAME, event(round=2))
    assert hub.connections(GAME) == 0

    connection.start(json.dumps({"type": "snapshot"}))
    await asyncio.wait_for(connection.task, 1)

    assert socket.sent == []


@pytest.mark.parametrize(
    "payload",
    [event("wat"), {"no": "type"}, event("snapshot"), event("pong"), event(bad={1, 2})],
    ids=["неизвестный тип", "без типа", "снимок не рассылается", "pong не рассылается", "не JSON"],
)
async def test_payloads_outside_the_event_contract_are_refused_without_raising(payload):
    hub = Hub()
    alice = FakeSocket()
    join(hub, GAME, "alice", alice)

    await hub.send(GAME, payload)
    await hub.send(GAME, event(round=1))

    await until(lambda: alice.sent)
    assert alice.sent == [event(round=1)]


async def test_a_socket_that_fails_is_logged_at_debug_without_the_input_of_the_error(caplog, leak):
    caplog.set_level(logging.DEBUG, logger=hub_module.__name__)
    hub = Hub()

    class BrokenSocket(FakeSocket):
        async def send_text(self, text: str) -> None:
            raise leak.error

    join(hub, GAME, "alice", BrokenSocket())

    await hub.send(GAME, event(round=1))
    await until(lambda: hub.connections(GAME) == 0)

    [record] = [r for r in caplog.records if r.name == hub_module.__name__]
    assert record.exc_info is None  # трассировку формирует хаб, а не logging
    assert "alice" in record.getMessage() and "ValidationError" in record.getMessage()
    assert leak.value not in caplog.text and "input_value" not in caplog.text


async def test_the_trace_of_a_closed_socket_is_not_built_when_debug_is_off(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=hub_module.__name__)
    built: list[BaseException] = []
    monkeypatch.setattr(hub_module, "safe_traceback", lambda exc, _settings: built.append(exc))
    hub = Hub()
    join(hub, GAME, "alice", FakeSocket(dead=True))

    await hub.send(GAME, event(round=1))
    await until(lambda: hub.connections(GAME) == 0)

    assert built == []  # дорогой текст ради отключённого сообщения не собирается


async def test_an_event_that_cannot_be_serialized_is_logged_without_the_input_of_the_error(
    monkeypatch, caplog, leak
):
    def broken_dumps(*_args: Any, **_kwargs: Any) -> str:
        raise leak.error  # ValidationError — наследник ValueError

    monkeypatch.setattr(hub_module, "json", SimpleNamespace(dumps=broken_dumps))
    hub = Hub()
    alice = FakeSocket()
    join(hub, GAME, "alice", alice)

    with caplog.at_level(logging.ERROR, logger=hub_module.__name__):
        await hub.send(GAME, event(round=1))

    [record] = caplog.records
    assert record.exc_info is None and GAME in record.getMessage()
    assert "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text
    assert alice.sent == []


def test_event_types_are_those_of_the_plan():
    assert {
        "phase",
        "player_status",
        "feed_item",
        "party",
        "combat",
        "character",
        "lobby",
        "journal_entry",
        "journal",
        "rest_started",
        "rest_idea",
        "rest_ended",
        "action_auto_submitted",  # блок B11: «Ход сдан» автору автосданного черновика
        "error",
    } == EVENT_TYPES
    # Кадр исключения уходит прямо в сокет исключённого, а не рассылкой хаба (блок B11).
    assert "kicked" not in EVENT_TYPES


async def test_disconnect_forgets_the_socket_and_is_safe_to_repeat():
    hub = Hub()
    socket = FakeSocket(stuck=True)
    connection = join(hub, GAME, "alice", socket, first=json.dumps({"type": "snapshot"}))
    await asyncio.sleep(0.01)  # писатель завис на отправке снимка

    hub.disconnect(connection)
    hub.disconnect(connection)

    assert hub.connections(GAME) == 0
    await until(lambda: socket.cancelled)


async def test_lobby_event_goes_through_the_hub_to_everyone():
    hub = Hub()
    socket = FakeSocket()
    join(hub, GAME, "alice", socket)

    lobby = {"type": "lobby", "game": {"id": GAME, "players": []}}
    await hub.send(GAME, lobby, "all")

    await until(lambda: socket.sent)
    assert socket.sent == [lobby]


# --- вышедший или исключённый игрок (S5) ---


async def test_drop_user_removes_every_connection_of_that_user_in_that_game_only():
    hub = Hub()
    tab_1, tab_2, boris, elsewhere = FakeSocket(), FakeSocket(), FakeSocket(), FakeSocket()
    join(hub, GAME, "alice", tab_1)
    join(hub, GAME, "alice", tab_2)
    join(hub, GAME, "boris", boris)
    join(hub, OTHER_GAME, "alice", elsewhere)

    dropped = hub.drop_user(GAME, "alice")
    await hub.send(GAME, event(round=1))
    await hub.send(OTHER_GAME, event(round=2))

    assert dropped == 2
    assert (hub.connections(GAME), hub.connections(OTHER_GAME)) == (1, 1)
    await until(lambda: boris.sent and elsewhere.sent)
    assert boris.sent == [event(round=1)] and elsewhere.sent == [event(round=2)]
    assert tab_1.sent == tab_2.sent == []


async def test_a_dropped_user_gets_neither_the_queued_nor_the_personal_events():
    hub = Hub()
    stuck = FakeSocket(stuck=True)  # клиент не читает: в очереди копятся события
    join(hub, GAME, "alice", stuck)
    await hub.send(GAME, event(round=1))
    await hub.send(GAME, event(round=2))
    await asyncio.sleep(0.01)

    hub.drop_user(GAME, "alice")
    await hub.send(GAME, event("character", hp=5), audience="alice")

    await until(lambda: stuck.cancelled)  # писатель остановлен, очередь не дослана
    assert stuck.sent == [] and hub.connections(GAME) == 0


async def test_dropping_a_user_without_connections_or_in_an_unknown_game_is_harmless():
    hub = Hub()
    alice = FakeSocket()
    join(hub, GAME, "alice", alice)

    assert hub.drop_user(GAME, "nobody") == 0
    assert hub.drop_user("no-such-game", "alice") == 0
    assert hub.drop_user(GAME, "alice") == 1
    assert hub.drop_user(GAME, "alice") == 0  # повторный вызов безопасен
    assert hub.connections(GAME) == 0


async def test_kicking_marks_and_drops_only_the_connections_of_the_kicked_player():
    # Блок B11: исключённого эндпоинт закроет кадром `kicked` и кодом 4403, вышедшего — нет.
    hub = Hub()
    tab_1 = join(hub, GAME, "alice", FakeSocket())
    tab_2 = join(hub, GAME, "alice", FakeSocket())
    boris = join(hub, GAME, "boris", FakeSocket())
    elsewhere = join(hub, OTHER_GAME, "alice", FakeSocket())
    leaving = join(hub, OTHER_GAME, "vera", FakeSocket())

    kicked = hub.kick_user(GAME, "alice")
    hub.drop_user(OTHER_GAME, "vera")

    assert kicked == 2
    assert (tab_1.kicked, tab_2.kicked) == (True, True)
    assert (boris.kicked, elsewhere.kicked, leaving.kicked) == (False, False, False)
    assert (hub.connections(GAME), hub.connections(OTHER_GAME)) == (1, 1)
