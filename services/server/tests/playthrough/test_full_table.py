"""Полный стол из шести игроков и шквал запросов: игра остаётся одной и целой.

Спека §8: «шесть друзей в одной комнате». Здесь шесть настоящих игроков с шестью сокетами проходят
вплетение и раунд, а затем игроки и хост одновременно сыплют запросами (черновики, «Готов», пас,
кнопки хоста, ход времени, переподключения). Что бы ни пришло и в каком порядке, игра остаётся
согласованной: один открытый ход, ни одного лишнего вызова мастера, рассказ у каждого разобранного
хода ровно один, отсчёт ровно один, клиенты совпадают со снимками, пятисотых ответов нет.
"""

import asyncio
import json
import random
from typing import Any

import pytest
import yaml

from dnd_server.bot.notify import collecting_text
from dnd_server.db import session as db_session
from dnd_server.db.models import GamePhase, MessageKind, TurnPhase, TurnStatus
from dnd_server.games.service import GameLocks
from evening import (
    COLLECT_SECONDS,
    HIDDEN_EXIT_ID,
    HIDDEN_FROM_PLAYERS,
    PUBLIC_ACTION,
    SERVER_SECRETS,
    TIMEOUT_S,
    Person,
    ScriptedThenSeeded,
    Server,
    assert_clients_agree_with_snapshots,
    begin_evening,
    chronicle,
    fresh_snapshot,
    game_route_calls,
    say,
    tool,
    view_of,
)

SIX = (
    ("alice", "Алиса"),
    ("boris", "Борис"),
    ("vera", "Вера"),
    ("gleb", "Глеб"),
    ("daria", "Дарья"),
    ("egor", "Егор"),
)
GOAL6 = {key: f"ЦЕЛЬ-{name.upper()}: дожить до рассвета" for key, name in SIX}
PRIVATE_TO_GLEB = "ЛИЧНОЕ-ГЛЕБУ: под половицей что-то блестит."
SECRET_OF_BORIS = "Подменить письмо стражника ПОЛНОЧЬ-ПИСЬМО"
BURST = 8  # сколько запросов летит в игру одновременно
# Что может прийти в игру во время шквала; ходы игроков встречаются чаще кнопок хоста.
STORM_KINDS = (
    ["draft"] * 2
    + ["submit"] * 2
    + ["pass", "collect", "close", "extend", "pause", "resume", "time", "snapshot"]
)


def table_scenario() -> dict[str, Any]:
    """Открытие, один особый раунд и шаблон раунда: его хватает на любое число ходов."""
    hooks = [
        {
            "character_id": f"$pc{number}",
            "npc_links": [{"npc_id": "marta", "relation": "знакомая"}],
            "personal_goal": GOAL6[key],
        }
        for number, (key, _name) in enumerate(SIX, start=1)
    ]
    return {
        "turns": [
            {"steps": [chronicle("Шестеро собрались у трактира."), say("Вечер, шестеро в сборе.")]},
            {
                "steps": [
                    tool(
                        "request_check",
                        character_id="$pc2",
                        ability="dex",
                        skill="sleight_of_hand",
                        dc=15,
                        reason="письмо стражника",
                    ),
                    tool("private_message", character_id="$pc4", text=PRIVATE_TO_GLEB),
                    chronicle("Борис занялся письмом, Глеб что-то заметил."),
                    say("У крыльца шепчутся, а над трактиром гаснет фонарь."),
                ]
            },
            {
                "repeat": True,
                "steps": [chronicle("Отряд идёт дальше."), say("Отряд идёт дальше.")],
            },
        ],
        "json": {"weaving": {"hooks": hooks}},
    }


@pytest.fixture
def six_scenario(tmp_path):
    path = tmp_path / "six.yaml"
    path.write_text(yaml.safe_dump(table_scenario(), allow_unicode=True), encoding="utf-8")
    return {"fake_llm_scenario": path}


async def start_table(server: Server) -> None:
    """Шесть игроков в лобби, «Начать», вплетение и открытие."""
    await begin_evening(server, cast=SIX)


async def test_six_players_get_each_only_his_own_hook_and_secrets_in_one_round(
    serve, six_scenario, model_calls
):
    async with serve(rng=ScriptedThenSeeded([14]), changes=six_scenario) as server:
        await start_table(server)
        assert len(server.watchers) == 6
        for key, _name in SIX:
            hook = [i for i in view_of(server, key)["feed"] if i["kind"] == "private"]
            assert [GOAL6[key] in i["text"] for i in hook] == [True]

        await server.submit("alice", "Осмотреть доску объявлений")
        await server.submit("boris", SECRET_OF_BORIS, secret=True)
        await server.ok("vera", "POST", "/action/pass")
        await server.draft("gleb", "Заглянуть под крыльцо")
        await server.submit("daria", "Послушать, о чём шепчутся")
        await server.host("collect")
        await server.settle()

        # Звали только тех, кто ещё пишет, кроме хоста: Глеба и Егора.
        asked = collecting_text(COLLECT_SECONDS)
        called = {
            key
            for key, _name in SIX
            if asked in server.notifier.texts_for(server.people[key].user_id)
        }
        assert called == {"gleb", "egor"}
        await server.scheduler.advance(COLLECT_SECONDS)
        await server.settle()

        game = await server.game()
        assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 3)
        assert model_calls.turns == 2
        await assert_clients_agree_with_snapshots(server, "после раунда за полным столом")
        everything = {
            key: json.dumps(watcher.frames, ensure_ascii=False)
            for key, watcher in server.watchers.items()
        }
        for key, text in everything.items():
            for owner, _ in SIX:
                assert (GOAL6[owner] in text) == (owner == key), (owner, key)
            assert ("ПОЛНОЧЬ" in text) == (key == "boris")
            assert (PRIVATE_TO_GLEB in text) == (key == "gleb")
            assert ("Ловкость рук" in text) == (key == "boris")  # тайный бросок только автору
            for hidden in (*HIDDEN_FROM_PLAYERS, HIDDEN_EXIT_ID, *SERVER_SECRETS):
                assert hidden not in text


# --- шквал запросов ---


async def one_request(server: Server, rng: random.Random, statuses: list[int]) -> None:
    """Одно случайное действие случайного участника: ход игрока, кнопка хоста, время, сокет."""
    keys = list(server.people)
    kind = rng.choice(STORM_KINDS)
    who = rng.choice(keys)
    if kind == "draft":
        body = {
            "text": rng.choice(["", "Идти вперёд", "Ждать", "Бить"]),
            "secret": rng.random() < 0.3,
            "inspiration": False,
        }
        response = await server.game_call(who, "PUT", "/action", json=body)
    elif kind in ("submit", "pass"):
        response = await server.game_call(who, "POST", f"/action/{kind}")
    elif kind == "time":
        await server.scheduler.advance(rng.choice([15, 60, 130]))
        return
    elif kind == "snapshot":
        await fresh_snapshot(server, who)
        return
    else:
        response = await server.game_call("alice", "POST", f"/host/{kind}")
    statuses.append(response.status_code)


async def done_turns(server: Server) -> int:
    return len([t for t in await server.turns() if t.status == TurnStatus.DONE])


async def settle_round(server: Server, done_before: int) -> None:
    """Доводит игру до обсуждения после шквала, в котором мастер разобрал хотя бы один ход: снимает
    паузу, собирает и закрывает ходы, повторяет сорвавшийся разбор, ждёт мастера."""
    for _ in range(8):
        await server.settle()
        game = await server.game()
        if game.phase == GamePhase.PAUSED:
            await server.game_call("alice", "POST", "/host/resume")
        elif game.turn_phase == TurnPhase.DISCUSSION:
            if await done_turns(server) > done_before:
                return
            await server.game_call("alice", "POST", "/host/collect")
        elif game.turn_phase == TurnPhase.COLLECTING:
            await server.game_call("alice", "POST", "/host/close")
        elif game.turn_phase == TurnPhase.DM_FAILED:
            await server.game_call("alice", "POST", "/host/retry")
    raise AssertionError("игра не пришла в обсуждение после разбора мастера")


async def assert_game_is_whole(server: Server, model_calls, where: str) -> None:
    game = await server.game()
    turns = await server.turns()
    unfinished = (TurnStatus.OPEN, TurnStatus.COLLECTING, TurnStatus.RESOLVING)
    live = [t for t in turns if t.status in unfinished]
    assert [t.id for t in live] == [game.current_turn_id], where  # один открытый ход
    assert game.turn_phase in (TurnPhase.DISCUSSION, TurnPhase.COLLECTING), where
    logs = await server.logs()
    assert model_calls.turns == len(logs), where  # каждый вызов мастера записан, лишних нет
    assert all(log.error is None for log in logs), where
    narration = [m for m in await server.messages(MessageKind.NARRATION) if not m.superseded]
    for turn in turns:
        if turn.status == TurnStatus.DONE:
            assert len([m for m in narration if m.turn_id == turn.id]) == 1, (where, turn.number)
    versions = [row.version for row in await server.snapshots()]
    assert versions == sorted(set(versions)), where
    assert game.state_version in versions, where
    pending = server.scheduler.pending()
    if game.turn_phase == TurnPhase.COLLECTING:
        assert pending == {server.game_id: game.deadline_at}, where
    else:
        assert pending == {}, where


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_a_storm_of_requests_from_six_players_leaves_one_whole_game(
    seed, serve, six_scenario, model_calls
):
    rng = random.Random(seed)
    async with serve(rng=ScriptedThenSeeded([14], seed=seed), changes=six_scenario) as server:
        await start_table(server)
        statuses: list[int] = []
        for number in range(3):
            done_before = await done_turns(server)
            for _burst in range(8):  # восемь запросов одновременно: меньше пула соединений
                requests = [one_request(server, rng, statuses) for _ in range(BURST)]
                await asyncio.wait_for(asyncio.gather(*requests), TIMEOUT_S * 3)
            await settle_round(server, done_before)
            await assert_game_is_whole(server, model_calls, f"шквал {number + 1}")
            await assert_clients_agree_with_snapshots(server, f"после шквала {number + 1}")
        assert statuses and max(statuses) < 500  # отказы бывают (409, 403), сбоев сервера нет


# --- нагрузка выше пула соединений ---


@pytest.fixture
def quick_pool_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же движок с пулом по умолчанию (5 + 10 соединений), но ожидание соединения две секунды
    вместо тридцати: так нехватка соединений видна быстро, а не через полминуты."""
    create_engine = db_session.create_async_engine
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        lambda url, **options: create_engine(url, pool_timeout=2, **options),
    )


async def test_sixteen_simultaneous_requests_to_one_game_are_all_served(
    serve, evening_dice, quick_pool_timeout
):
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        body = {"text": "Идти вперёд", "secret": False, "inspiration": False}
        keys = list(server.people)

        responses = await asyncio.gather(
            *(server.game_call(keys[i % 3], "PUT", "/action", json=body) for i in range(16)),
            return_exceptions=True,
        )

        assert [r.status_code for r in responses if not isinstance(r, BaseException)] == [200] * 16


async def test_a_flood_of_requests_from_a_stranger_does_not_stop_the_game(
    serve, evening_dice, quick_pool_timeout
):
    # Чужак (вошёл, но не участник) сыплет запросами на id этой игры и на id, которого нет: все они
    # встают в очередь за блокировкой игры. Очередь не должна держать соединения пула, иначе мастеру
    # нечем записать ход, а игрокам нечем читать (тот же дефект, что и у 16 запросов игрока).
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        signed_in = await server.call(None, "POST", "/api/auth/dev", json={"login": "dev-6"})
        body = signed_in.json()
        stranger = Person("stranger", "Чужак", body["user"]["id"], body["token"])
        draft = {"text": "Забрать всё", "secret": False, "inspiration": False}
        flood = [
            server.call(stranger, "PUT", f"/api/games/{game}/action", json=draft)
            for game in [server.game_id, "no-such-game"] * 20
        ]

        async def a_round_of_the_table() -> None:
            await server.submit("alice", PUBLIC_ACTION)
            await server.ok("boris", "POST", "/action/pass")
            await server.ok("vera", "POST", "/action/pass")
            await server.host("collect")
            await server.settle()

        answers = await asyncio.gather(*flood, a_round_of_the_table())

        assert {response.status_code for response in answers[:-1]} == {404}
        game = await server.game()
        assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 3)
        assert [t.status for t in await server.turns() if t.number == 2] == [TurnStatus.DONE]


class WatchedLocks(GameLocks):
    """Блокировки игр, которые считают, сколько запросов пришло их ждать."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = 0

    def lock(self, game_id: str) -> Any:
        return _WatchedLock(super().lock(game_id), self)


class _WatchedLock:
    def __init__(self, lock: asyncio.Lock, owner: WatchedLocks) -> None:
        self._lock, self._owner = lock, owner

    async def __aenter__(self) -> None:
        self._owner.arrived += 1  # запрос дошёл до ожидания блокировки
        await self._lock.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        self._lock.release()


async def test_no_request_holds_a_pooled_connection_while_it_waits_for_the_game_lock(
    serve, evening_dice, gate, route_table, quick_pool_timeout
):
    # Инвариант: пока запрос ждёт блокировку игры, он не держит соединение пула. Держатель
    # блокировки открывает свою сессию и ждёт соединение из того же пула; если соединения держат
    # ждущие, встают все. Проверяются все маршруты игры, кроме чтения, по три запроса на маршрут.
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        await server.submit("alice", PUBLIC_ACTION)
        await server.ok("boris", "POST", "/action/pass")
        await server.ok("vera", "POST", "/action/pass")
        gate.arm()
        await server.host("collect")
        await asyncio.wait_for(gate.entered.wait(), TIMEOUT_S)  # мастер думает: всё откажет 409
        locks = server.app.state.game_locks = WatchedLocks()
        pool = server.app.state.engine.pool
        calls = game_route_calls(server, route_table) * 3

        async with locks.lock(server.game_id):  # игру держит тест, пока запросы подходят
            requests = [
                asyncio.create_task(server.call(who, method, path, **options))
                for who, method, path, options in calls
            ]
            async with asyncio.timeout(10):
                # Все запросы либо ждут блокировку (кроме нашего захода), либо уже ответили.
                while locks.arrived - 1 + sum(task.done() for task in requests) < len(requests):
                    await asyncio.sleep(0.01)
            held = pool.checkedout()
        answers = await asyncio.gather(*requests)

        assert held == 0, f"ждущие блокировку запросы держат соединений пула: {held}"
        # Отказ по состоянию игры (409); создание героя ещё и занимает место под вызов нейронки, и
        # второй запрос того же игрока отклоняется сразу (429), не дойдя до блокировки.
        assert {response.status_code for response in answers} == {409, 429}
        gate.open()
        await server.settle()


async def test_a_join_request_does_not_hold_a_pooled_connection_while_it_waits_for_the_game_lock(
    serve, evening_dice, quick_pool_timeout
):
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        code = (await server.game_call("alice", "GET", "")).json()["code"]
        signed_in = await server.call(None, "POST", "/api/auth/dev", json={"login": "dev-6"})
        body = signed_in.json()
        stranger = Person("stranger", "Чужак", body["user"]["id"], body["token"])
        locks = server.app.state.game_locks = WatchedLocks()
        pool = server.app.state.engine.pool

        async with locks.lock(server.game_id):
            joins = [
                asyncio.create_task(
                    server.call(stranger, "POST", "/api/games/join", json={"code": code})
                )
                for _ in range(5)
            ]
            async with asyncio.timeout(10):
                while locks.arrived < 1 + len(joins):
                    await asyncio.sleep(0.01)
            held = pool.checkedout()
        await asyncio.gather(*joins)

        assert held == 0, f"ждущие блокировку входы по коду держат соединений пула: {held}"
