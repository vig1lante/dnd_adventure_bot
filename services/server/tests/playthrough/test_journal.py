"""Дневник отряда на настоящем сервере весь вечер (финальное покрытие блока B8 DND-2).

Клиент собирает дневник так же, как хранилище Mini App: начинает с блока `journal` снимка, кадр
`journal_entry` заменяет запись с тем же `id` на её месте или ставит новую в конец, кадр `journal`
заменяет дневник целиком. Здесь проверяется, что такой клиент на каждом шаге вечера видит ровно
дневник нового снимка (тот, что получит переподключившийся игрок): после раундов, боя и привала,
после перезапуска сервера, после «Перегенерировать» и после «Повторить» за сорвавшейся записью.
Личное знание Бориса видит только Борис, а исключённый из лобби игрок дневника не видит вовсе.

Экран игры Mini App подключается к игре заново, когда игра открылась (лобби и игра — разные
экраны, каждый со своим сокетом), поэтому клиент дневника здесь — сокет, открытый после открытия
игры (`open_game_screens`). Сокет, открытый ещё в лобби, получил снимок без дневника: после открытия
игры его догоняет блок `journal` (дополнение контракта B8 §11, п. 7) — это отдельный тест.

«Раунд» записи — номер раунда игроков (`GameState.party_round`, §11 п. 5–6): открытие — раунд 0,
первый раунд игроков — 1; ходы боя и конец привала номер не меняют.

Сценарий мастера — вечер `evening.py`, в первый раунд которого добавлены две записи `journal_add`:
общая о завсегдатаях и личная Бориса о Фабиане. Остальное в вечере прежнее; во втором раунде отряд
идёт на кладбище, и место, известное со старта, отмечается посещённым.
"""

import asyncio
import copy
import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from evening import (
    CAST,
    TIMEOUT_S,
    Server,
    Watcher,
    battle,
    begin_evening,
    camp,
    carry,
    evening_scenario,
    fresh_snapshot,
    gather_in_lobby,
    reconnect,
    round_one,
    round_two,
    tool,
)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from dnd_server.db.models import TurnKind, TurnPhase, TurnStatus

COMMON_NOTE = "ДНЕВНИК-ОБЩЕЕ: завсегдатаи держатся Марты и не любят чужаков."
BORIS_NOTE = "ДНЕВНИК-БОРИСУ: Фабиан задолжал половине деревни."
CEMETERY = "location:cemetery@all"
JOURNAL_FRAMES = ("journal_entry", "journal")


def journal_evening(*, repeat_turn: int | None = None, from_turn: int = 0) -> dict[str, Any]:
    """Вечер `evening.py` с двумя записями дневника в первом раунде (ход сценария 1).

    `repeat_turn` — ход сценария (с нуля), который стоит в очереди дважды: «Перегенерировать» и
    «Повторить» получают тот же ход. `from_turn` — хвост сценария для перезапущенного сервера."""
    scenario = evening_scenario()
    round_one_steps = scenario["turns"][1]["steps"]
    round_one_steps[-2:-2] = [
        tool("journal_add", ref="faction:regulars", note=COMMON_NOTE),
        tool("journal_add", ref="npc:fabian", note=BORIS_NOTE, for_hero="$pc2"),
    ]
    if repeat_turn is not None:
        scenario["turns"].insert(repeat_turn + 1, copy.deepcopy(scenario["turns"][repeat_turn]))
    scenario["turns"] = scenario["turns"][from_turn:]
    return scenario


@pytest.fixture
def journal_scenario(tmp_path: Path) -> Callable[..., Path]:
    """Пишет вечер с дневником (`journal_evening`) во временную папку, каждый раз в свой файл."""
    numbers = itertools.count(1)

    def write(**options: Any) -> Path:
        path = tmp_path / f"journal-evening-{next(numbers)}.yaml"
        text = yaml.safe_dump(journal_evening(**options), allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        return path

    return write


# --- клиент дневника ---


def client_journal(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Дневник клиента, принявшего снимок и все кадры сокета (как `withJournalEntry` в Mini App)."""
    snapshot, events = frames[0], frames[1:]
    assert snapshot["type"] == "snapshot", snapshot["type"]
    journal = list(snapshot["journal"])
    for event in events:
        if event["type"] == "journal":
            journal = list(event["entries"])
        elif event["type"] == "journal_entry":
            ids = [entry["id"] for entry in journal]
            if event["entry"]["id"] in ids:
                journal[ids.index(event["entry"]["id"])] = event["entry"]
            else:
                journal.append(event["entry"])
    return journal


async def assert_journals_agree(server: Server, where: str) -> None:
    """У каждого игрока дневник из кадров равен дневнику нового снимка, и дублей в нём нет."""
    await server.sync()
    for key, watcher in server.watchers.items():
        fresh = (await fresh_snapshot(server, key))["journal"]
        assert client_journal(watcher.frames) == fresh, f"{where}: у {key} дневник расходится"
        ids = [entry["id"] for entry in fresh]
        assert len(ids) == len(set(ids)), f"{where}: у {key} запись дневника дважды"


async def open_game_screens(server: Server) -> None:
    """Игра открылась: экран игры каждого игрока подключается заново и получает снимок."""
    await server.close_watchers()
    await server.watch_everyone()


def journal_frames_since(server: Server, marks: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
    """Кадры дневника, пришедшие каждому игроку после отметки `marks` (числа кадров)."""
    return {
        key: [f for f in watcher.frames[marks[key] :] if f["type"] in JOURNAL_FRAMES]
        for key, watcher in server.watchers.items()
    }


def frame_marks(server: Server) -> dict[str, int]:
    return {key: len(watcher.frames) for key, watcher in server.watchers.items()}


def entries_sent(watcher: Watcher) -> list[tuple[str, int]]:
    """Записи из кадров `journal_entry` игрока: (id, ревизия) по порядку рассылки."""
    return [(f["entry"]["id"], f["entry"]["revision"]) for f in watcher.events("journal_entry")]


async def boris_hero_id(server: Server) -> str:
    state = await server.state()
    (hero_id,) = [hero.id for hero in state.party.values() if hero.name == "Борис"]
    return hero_id


# --- весь вечер ---


async def test_the_journal_from_the_frames_is_the_journal_of_the_snapshot_all_evening_long(
    serve, evening_dice, journal_scenario
):
    async with serve(rng=evening_dice, changes={"fake_llm_scenario": journal_scenario()}) as server:
        await begin_evening(server)
        await open_game_screens(server)
        starting = [entry.model_dump(mode="json") for entry in (await server.state()).journal]
        for watcher in server.watchers.values():
            assert watcher.snapshot["journal"] == starting
        assert {entry["audience"] for entry in starting} == {"all"}
        assert {"intro", "location:village", "location:cemetery"} <= {e["ref"] for e in starting}

        async def check(where: str) -> None:
            await assert_journals_agree(server, where)

        await round_one(server, check)
        await round_two(server, check)
        await battle(server, check)
        await camp(server, check)

        boris = await boris_hero_id(server)
        sent = {key: entries_sent(watcher) for key, watcher in server.watchers.items()}
        common = [("faction:regulars@all", 1), (CEMETERY, 2)]
        assert sent["alice"] == sent["vera"] == common
        assert sent["boris"] == [common[0], (f"npc:fabian@{boris}", 1), common[1]]
        # Без отката кадр всего дневника не нужен.
        assert all(not watcher.events("journal") for watcher in server.watchers.values())
        # Место отмечено номером раунда игроков, в котором отряд пришёл (`party_round`): второй
        # раунд. Запись мастера — первый. Открытие, ходы боя и конец привала раундом не считаются.
        state = await server.state()
        (cemetery,) = [e for e in state.journal if e.id == CEMETERY]
        (regulars,) = [e for e in state.journal if e.id == "faction:regulars@all"]
        rounds = [
            turn
            for turn in await server.turns()
            if turn.kind == TurnKind.ROUND and turn.status == TurnStatus.DONE
        ]
        assert (cemetery.visited_turn, cemetery.turn, cemetery.revision) == (2, 0, 2)
        assert regulars.turn == 1
        assert state.party_round == len(rounds) < max(turn.number for turn in await server.turns())
        # Личное знание Бориса — только у Бориса: в кадрах, в снимке и ни в одном ответе REST.
        frames = {key: str(watcher.frames) for key, watcher in server.watchers.items()}
        assert BORIS_NOTE in frames["boris"] and COMMON_NOTE in frames["alice"]
        assert BORIS_NOTE not in frames["alice"] and BORIS_NOTE not in frames["vera"]
        for key in ("alice", "vera"):
            assert BORIS_NOTE not in str(await fresh_snapshot(server, key))
        for key in server.people:
            for method, path in (
                ("GET", "/api/games"),
                ("GET", f"/api/games/{server.game_id}"),
                ("GET", f"/api/games/{server.game_id}/characters/mine"),
            ):
                response = await server.call(key, method, path)
                assert response.status_code == 200, (key, path, response.text)
                # Дневник уходит только снимком и кадрами сокета: в REST его нет ни у кого.
                assert BORIS_NOTE not in response.text and "npc:fabian@" not in response.text
                assert COMMON_NOTE not in response.text


# --- сокет из лобби ---


async def test_a_socket_opened_in_the_lobby_gets_the_starting_journal_after_the_opening(
    serve, evening_dice, journal_scenario
):
    async with serve(rng=evening_dice, changes={"fake_llm_scenario": journal_scenario()}) as server:
        await begin_evening(server)  # сокеты открыты в лобби и остались после открытия игры

        for watcher in server.watchers.values():
            assert watcher.snapshot["journal"] == []  # снимок лобби: дневника ещё нет
            assert len(watcher.events("journal")) == 1
            assert watcher.events("journal_entry") == []
        await assert_journals_agree(server, "сокет из лобби после открытия")
        await round_one(server)
        await assert_journals_agree(server, "сокет из лобби после первого раунда")


# --- перезапуск сервера ---


async def test_a_restart_keeps_the_journal_and_its_frames_go_on_after_it(
    serve, evening_dice, journal_scenario
):
    async with serve(rng=evening_dice, changes={"fake_llm_scenario": journal_scenario()}) as first:
        await begin_evening(first)
        await round_one(first)
        before = {key: (await fresh_snapshot(first, key))["journal"] for key in first.people}
        knowledge = carry(first)
    tail = {"fake_llm_scenario": journal_scenario(from_turn=2)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)

        for key, watcher in server.watchers.items():
            assert watcher.snapshot["journal"] == before[key], key
        assert BORIS_NOTE in str(before["boris"]) and BORIS_NOTE not in str(before["alice"])
        await round_two(server)
        await assert_journals_agree(server, "раунд после перезапуска")
        for key, watcher in server.watchers.items():
            assert entries_sent(watcher) == [(CEMETERY, 2)], key


# --- «Перегенерировать» ---


async def test_regenerating_a_round_replaces_the_journal_block_and_writes_the_visit_once(
    serve, evening_dice, journal_scenario
):
    # Второй раунд (ход сценария 2) стоит в очереди дважды: новая попытка снова ведёт на кладбище.
    scenario = {"fake_llm_scenario": journal_scenario(repeat_turn=2)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        await begin_evening(server)
        await open_game_screens(server)
        await round_one(server)
        await round_two(server)
        boris = await boris_hero_id(server)
        marks = frame_marks(server)

        await server.host("regenerate")
        await server.settle()

        for key, frames in journal_frames_since(server, marks).items():
            # Сначала дневник целиком до снятого хода, потом запись новой попытки.
            assert [f["type"] for f in frames] == ["journal", "journal_entry"], key
            rolled_back = {entry["id"]: entry for entry in frames[0]["entries"]}
            cemetery = rolled_back[CEMETERY]
            assert (cemetery["revision"], cemetery["visited_turn"]) == (1, None)
            assert (f"npc:fabian@{boris}" in rolled_back) == (key == "boris")
            assert BORIS_NOTE not in str(frames) or key == "boris"
            entry = frames[1]["entry"]
            assert (entry["id"], entry["revision"], entry["visited_turn"]) == (CEMETERY, 2, 2)
        await assert_journals_agree(server, "после перегенерации")
        state = await server.state()
        assert [e.id for e in state.journal].count(CEMETERY) == 1
        (cemetery,) = [e for e in state.journal if e.id == CEMETERY]
        assert cemetery.revision == 2  # посещение одно: снятая попытка не добавила своего


# --- сбой записи и «Повторить» ---


async def test_a_failed_write_sends_no_journal_frame_and_the_retry_writes_the_visit_once(
    serve, evening_dice, disk_fault, journal_scenario
):
    scenario = {"fake_llm_scenario": journal_scenario(repeat_turn=2)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        await begin_evening(server)
        await open_game_screens(server)
        await round_one(server)
        before = await server.state()
        dice = evening_dice.checkpoint()
        marks = frame_marks(server)
        disk_fault.arm(before.version)

        await round_two(server)

        assert disk_fault.hits == 1
        assert (await server.game()).turn_phase == TurnPhase.DM_FAILED
        assert (await server.state()).journal == before.journal
        assert journal_frames_since(server, marks) == {key: [] for key in server.watchers}
        await assert_journals_agree(server, "после сбоя записи")

        evening_dice.restore(dice)
        assert (await server.game_call("alice", "POST", "/host/retry")).status_code == 200
        await server.settle()

        for key, frames in journal_frames_since(server, marks).items():
            assert [(f["type"], f["entry"]["id"], f["entry"]["revision"]) for f in frames] == [
                ("journal_entry", CEMETERY, 2)
            ], key
        await assert_journals_agree(server, "после «Повторить»")
        (cemetery,) = [e for e in (await server.state()).journal if e.id == CEMETERY]
        assert cemetery.revision == 2


# --- исключённый из лобби ---


async def test_a_player_kicked_from_the_lobby_gets_nothing_of_the_journal(
    serve, evening_dice, journal_scenario
):
    cast = (*CAST, ("gleb", "Глеб"))
    async with serve(rng=evening_dice, changes={"fake_llm_scenario": journal_scenario()}) as server:
        await gather_in_lobby(server, cast)
        await server.watch_everyone()
        gleb = server.people["gleb"]
        await server.ok("alice", "POST", f"/players/{gleb.user_id}/kick")
        kicked = server.watchers.pop("gleb")
        del server.people["gleb"]
        await asyncio.wait_for(kicked.ws.wait_closed(), TIMEOUT_S)
        started = await server.game_call("alice", "POST", "/start")
        assert started.status_code == 200, started.text
        await server.settle()
        await open_game_screens(server)
        await round_one(server)
        await round_two(server)
        await assert_journals_agree(server, "без исключённого")

        assert kicked.snapshot["journal"] == []  # снимок лобби: дневника ещё нет
        assert not [f for f in kicked.frames if f["type"] in JOURNAL_FRAMES]
        everything = str(kicked.frames)
        assert COMMON_NOTE not in everything and BORIS_NOTE not in everything
        # Вернуться к игре он не может: сокет закрыт как «не участник», REST отказывает.
        async with connect(server.ws_url(gleb.token)) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), TIMEOUT_S)
        assert closed.value.rcvd.code == 4403
        response = await server.call(gleb, "GET", f"/api/games/{server.game_id}")
        assert response.status_code == 404  # чужая игра отвечает как несуществующая
        assert COMMON_NOTE not in response.text and BORIS_NOTE not in response.text
        # Герои игры — трое оставшихся: у исключённого героя нет и записей.
        state = await server.state()
        assert sorted(hero.name for hero in state.party.values()) == ["Алиса", "Борис", "Вера"]
        assert {e.audience for e in state.journal} <= {"all", *state.party}
