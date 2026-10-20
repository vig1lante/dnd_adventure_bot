"""Перезапуск сервера на каждой фазе игры: ничего не теряется, ничего не запускается само.

Перезапуск — второй вход на той же базе: новое приложение, новый `lifespan`, пустая память, новые
сокеты. Часы отсчётов, кубы и игроки идут дальше, а сценарий мастера начинается с того хода, который
следует за уже разобранными (провайдер игры хранится в памяти и после остановки забыт).
"""

import json
from datetime import timedelta
from typing import Any

import pytest

from dnd_server.db.models import (
    ActionStatus,
    Game,
    GamePhase,
    MessageKind,
    PlayerAction,
    Turn,
    TurnPhase,
    TurnStatus,
)
from evening import (
    OWN_SECRETS,
    BORIS_SECRET_ACTION,
    BORIS_SECRET_COMBAT,
    CAMP_SECONDS,
    CAST,
    COLLECT_SECONDS,
    FIRE_LINES,
    FaultCase,
    HIDDEN_EXIT_ID,
    HIDDEN_FROM_PLAYERS,
    HURRY_SECONDS,
    PUBLIC_ACTION,
    SERVER_SECRETS,
    SUMMARY,
    assert_clients_agree_with_snapshots,
    battle,
    begin_evening,
    bring_to,
    camp,
    carry,
    fresh_snapshot,
    gather_in_lobby,
    host_failure_notes,
    iso,
    master_fingerprint,
    reconnect,
    round_one,
    round_two,
    view_of,
)


async def test_a_restart_during_the_discussion_keeps_the_drafts_and_nothing_starts_by_itself(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await first.draft("alice", PUBLIC_ACTION)
        await first.submit("boris", BORIS_SECRET_ACTION, secret=True)
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=1)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        game = await server.game()
        assert (game.turn_phase, game.deadline_at) == (TurnPhase.DISCUSSION, None)
        assert server.scheduler.pending() == {} and model_calls.turns == 1  # сам ход не идёт
        for key, expected in (
            ("alice", ("draft", PUBLIC_ACTION)),
            ("boris", ("submitted", BORIS_SECRET_ACTION)),
        ):
            action = (await fresh_snapshot(server, key))["action"]
            assert (action["status"], action["text"]) == expected
        assert (await fresh_snapshot(server, "vera"))["action"] is None
        await assert_clients_agree_with_snapshots(server, "после перезапуска в обсуждении")
        # Игра продолжается так, будто перезапуска не было.
        await server.host("collect")
        await server.settle()
        await server.scheduler.advance(COLLECT_SECONDS)
        await server.settle()
        assert (await server.game()).state_version == 3
        await assert_clients_agree_with_snapshots(server, "после раунда")


async def test_a_restart_during_the_countdown_gets_the_countdown_back_from_the_database(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await first.draft("alice", PUBLIC_ACTION)
        await first.host("collect")
        await first.settle()
        deadline = (await first.game()).deadline_at
        assert first.scheduler.pending() == {first.game_id: deadline}
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=1)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        # Отсчёт тот же, а не новый: срок взят из базы.
        assert server.scheduler.pending() == {server.game_id: deadline}
        assert view_of(server, "alice")["phase"]["deadline_at"] == iso(deadline)
        await server.scheduler.advance(COLLECT_SECONDS - 1)
        await server.settle()
        assert model_calls.turns == 1
        await server.scheduler.advance(1)
        await server.settle()
        actions = await server.actions(2)
        assert {k: r.status for k, r in actions.items()} == {
            "alice": ActionStatus.AUTO_SUBMITTED,
            "boris": ActionStatus.AUTO_PASS,
            "vera": ActionStatus.AUTO_PASS,
        }
        assert (await server.game()).state_version == 3 and model_calls.turns == 2
        await assert_clients_agree_with_snapshots(server, "после раунда по отсчёту")


async def test_a_restart_of_a_paused_game_keeps_the_frozen_remainder(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await first.draft("alice", PUBLIC_ACTION)
        await first.host("collect")
        await first.scheduler.advance(50)
        await first.host("pause")
        await first.settle()
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=1)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        paused = view_of(server, "alice")["phase"]
        assert (paused["phase"], paused["deadline_at"]) == ("paused", None)
        assert paused["remaining_seconds"] == COLLECT_SECONDS - 50
        assert server.scheduler.pending() == {}
        await server.scheduler.advance(10_000)
        await server.settle()
        assert model_calls.turns == 1  # пока игра стоит, время не идёт
        await server.host("resume", "alice")
        await server.settle()
        remaining = timedelta(seconds=COLLECT_SECONDS - 50)
        assert server.scheduler.pending() == {server.game_id: server.scheduler.now() + remaining}
        await server.scheduler.advance(COLLECT_SECONDS - 50)
        await server.settle()
        assert model_calls.turns == 2
        await assert_clients_agree_with_snapshots(server, "после паузы и перезапуска")


async def test_a_restart_in_the_fight_keeps_the_draft_and_the_turn_of_the_hero(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await round_one(first)
        await round_two(first)
        await first.draft("alice", "Уклоняюсь от меча скелета")
        hero = dict(zip(first.people, (await first.state()).party, strict=True))
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=3)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        view = view_of(server, "boris")
        assert view["phase"]["turn_phase"] == "combat_wait" and view["phase"]["deadline_at"] is None
        assert view["combat"]["current_id"] == hero["alice"]
        assert server.scheduler.pending() == {} and model_calls.turns == 3
        draft = (await fresh_snapshot(server, "alice"))["action"]
        assert draft["text"] == "Уклоняюсь от меча скелета"
        body = {"text": "Бью первым", "secret": False, "inspiration": False}
        assert (await server.game_call("boris", "PUT", "/action", json=body)).status_code == 409
        await assert_clients_agree_with_snapshots(server, "после перезапуска в бою")
        # Бой идёт дальше до конца сцены, как в игре без перезапуска.
        await battle(server)
        assert (await server.game()).turn_phase == TurnPhase.DISCUSSION
        assert (await server.state()).combat is None
        await assert_clients_agree_with_snapshots(server, "после боя")


async def test_a_restart_while_a_hero_is_hurried_gets_the_countdown_back(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await round_one(first)
        await round_two(first)
        await first.submit("alice", "Уклоняюсь от меча скелета")
        await first.settle()
        await first.submit("boris", BORIS_SECRET_COMBAT, secret=True)
        await first.settle()
        await first.host("hurry")
        await first.settle()
        deadline = (await first.game()).deadline_at
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=5)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        assert server.scheduler.pending() == {server.game_id: deadline}
        assert view_of(server, "vera")["phase"]["deadline_at"] == iso(deadline)
        await server.scheduler.advance(HURRY_SECONDS - 1)
        await server.settle()
        assert model_calls.turns == 5
        await server.scheduler.advance(1)
        await server.settle()
        row = (await server.actions(6))["vera"]
        assert (row.status, row.text) == (ActionStatus.AUTO_SUBMITTED, "Уклоняется")
        await server.submit("alice", "Добиваю скелета")
        await server.settle()
        assert (await server.state()).combat is None and model_calls.turns == 7
        await assert_clients_agree_with_snapshots(server, "после боя с перезапуском")


async def test_a_restart_at_the_fire_keeps_the_lines_and_the_deadline_and_the_morning_comes(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await round_one(first)
        await round_two(first)
        await battle(first)
        await first.submit("alice", "Разжечь костёр у сторожки и отдохнуть до утра")
        await first.ok("boris", "POST", "/action/pass")
        await first.ok("vera", "POST", "/action/pass")
        await first.host("collect")
        await first.settle()
        for key, line in FIRE_LINES.items():
            await first.ok(key, "POST", "/camp/line", json={"text": line})
        await first.host("camp-extend")
        await first.settle()
        deadline = (await first.game()).deadline_at
        day = (await first.state()).time.day
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=8)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        assert server.scheduler.pending() == {server.game_id: deadline}
        assert view_of(server, "vera")["phase"]["turn_phase"] == "camp"
        lines = [i["text"] for i in view_of(server, "vera")["feed"] if i["kind"] == "camp_line"]
        assert lines == list(FIRE_LINES.values())
        await assert_clients_agree_with_snapshots(server, "после перезапуска у костра")
        await server.scheduler.advance(CAMP_SECONDS + 59)
        await server.settle()
        assert model_calls.turns == 8  # привал ещё идёт
        await server.scheduler.advance(1)
        await server.settle()
        state = await server.state()
        hero = next(iter(state.party.values()))
        assert (hero.hp, state.time.day) == (hero.hp_max, day + 1)
        assert (state.chronicle.summary, state.chronicle.entries) == (SUMMARY, [])
        assert model_calls.turns == 9
        await assert_clients_agree_with_snapshots(server, "утро после перезапуска")


async def test_a_restart_of_a_paused_camp_keeps_the_frozen_remainder(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await round_one(first)
        await round_two(first)
        await battle(first)
        await first.submit("alice", "Разжечь костёр у сторожки и отдохнуть до утра")
        await first.ok("boris", "POST", "/action/pass")
        await first.ok("vera", "POST", "/action/pass")
        await first.host("collect")
        await first.settle()
        await first.scheduler.advance(100)
        await first.host("pause")
        await first.settle()
        knowledge = carry(first)
    tail = {"fake_llm_scenario": write_scenario(from_turn=8)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        paused = view_of(server, "alice")["phase"]
        assert (paused["phase"], paused["remaining_seconds"]) == ("paused", CAMP_SECONDS - 100)
        assert server.scheduler.pending() == {}
        await server.scheduler.advance(100_000)
        await server.settle()
        assert model_calls.turns == 8
        await server.host("resume")
        await server.scheduler.advance(CAMP_SECONDS - 100)
        await server.settle()
        assert model_calls.turns == 9 and (await server.game()).turn_phase == TurnPhase.DISCUSSION
        await assert_clients_agree_with_snapshots(server, "утро после паузы и перезапуска")


async def test_a_failed_turn_stays_failed_over_a_restart_and_the_host_repeats_it(
    serve, evening_dice, disk_fault, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        await begin_evening(first)
        await first.submit("alice", PUBLIC_ACTION)
        await first.submit("boris", BORIS_SECRET_ACTION, secret=True)
        await first.ok("vera", "POST", "/action/pass")
        dice_before = evening_dice.checkpoint()
        disk_fault.arm(2)
        await first.host("collect")
        await first.settle()
        assert (await first.game()).turn_phase == TurnPhase.DM_FAILED
        knowledge = carry(first)
    evening_dice.restore(dice_before)
    tail = {"fake_llm_scenario": write_scenario(from_turn=1)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        game = await server.game()
        assert (game.turn_phase, game.state_version) == (TurnPhase.DM_FAILED, 2)
        assert view_of(server, "alice")["phase"]["turn_phase"] == "dm_failed"
        assert model_calls.turns == 2  # сам ход после перезапуска не идёт
        await server.host("retry")
        await server.settle()
        game = await server.game()
        assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 3)
        actions = await server.actions(2)  # ходы игроков пережили и сбой, и перезапуск
        assert actions["boris"].text == BORIS_SECRET_ACTION and actions["boris"].secret
        await assert_clients_agree_with_snapshots(server, "после повтора")


@pytest.mark.parametrize("turn", ["combat", "camp_end"])
async def test_a_turn_cut_short_by_a_crash_becomes_dm_failed_and_the_repeat_does_it_once(
    turn, serve, evening_dice, write_scenario, model_calls, db
):
    case = FaultCase(turn, None, 0)
    async with serve(rng=evening_dice) as first:
        await bring_to(first, case)  # игра стоит перед ходом мастера, его кнопка не нажата
        await first.settle()
        before = await master_fingerprint(first)
        day = (await first.state()).time.day
        # Процесс умер посреди хода: в базе осталось «мастер разбирает».
        async with first.db() as session:
            game = await session.get(Game, first.game_id)
            current = await session.get(Turn, game.current_turn_id)
            game.turn_phase, game.deadline_at = TurnPhase.RESOLVING, None
            current.status = TurnStatus.RESOLVING
            if turn == "combat":
                action = await first.actions(current.number)
                if "alice" not in action:  # Алиса сдала ход перед сбоем
                    session.add(
                        PlayerAction(
                            turn_id=current.id,
                            user_id=first.people["alice"].user_id,
                            text="Уклоняюсь от меча скелета",
                            status=ActionStatus.SUBMITTED,
                        )
                    )
            await session.commit()
        knowledge = carry(first)
    consumed = {"combat": 3, "camp_end": 8}[turn]
    tail = {"fake_llm_scenario": write_scenario(from_turn=consumed)}
    async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
        await reconnect(server)
        calls_before = model_calls.turns
        game = await server.game()
        assert game.turn_phase == TurnPhase.DM_FAILED  # ход сам не перезапущен
        assert model_calls.turns == calls_before and server.scheduler.pending() == {}
        notes = await host_failure_notes(server)
        assert [n.audience for n in notes] == [server.people["alice"].user_id]
        last = (await server.logs())[-1]
        assert last.error == "Сервер перезапущен во время хода: попытка прервана"
        assert await master_fingerprint(server) | {"logs": 0} == before | {"logs": 0}
        await assert_clients_agree_with_snapshots(server, "после падения процесса")
        await server.host("retry")
        await server.settle()
        state = await server.state()
        hero = next(iter(state.party.values()))
        assert (await server.game()).turn_phase != TurnPhase.DM_FAILED
        assert state.version > before["state_version"]
        if turn == "camp_end":
            assert (hero.hp, state.time.day) == (hero.hp_max, day + 1)  # отдых проведён один раз
        await assert_clients_agree_with_snapshots(server, "после повтора")


async def test_a_game_started_but_never_woven_before_a_crash_is_begun_by_the_retry_button(
    serve, evening_dice, write_scenario, model_calls
):
    async with serve(rng=evening_dice) as first:
        # Сервер умер сразу после «Начать»: мастера ещё не звали (хук запуска ничего не делает).
        await gather_in_lobby(first)
        first.app.state.game_hooks = type("Silent", (), {"on_game_started": _nothing})()
        assert (await first.game_call("alice", "POST", "/start")).status_code == 200
        knowledge = carry(first)
    async with serve(rng=evening_dice, **knowledge) as server:
        await reconnect(server)
        game = await server.game()
        assert (game.phase, game.turn_phase, game.state_version) == (GamePhase.WEAVING, None, 0)
        assert model_calls.turns == 0 and await server.messages() == []
        await server.host("retry")
        await server.settle()
        game = await server.game()
        assert (game.phase, game.turn_phase, game.state_version) == (
            GamePhase.PLAYING,
            TurnPhase.DISCUSSION,
            2,
        )
        assert len(await server.messages(MessageKind.PRIVATE)) == 3
        await assert_clients_agree_with_snapshots(server, "после начала с повтором")


async def _nothing(_self: Any, _game_id: str) -> None:
    return None


async def test_an_evening_that_is_restarted_after_every_stage_is_the_same_evening(
    serve, evening_dice, write_scenario, model_calls
):
    seen: dict[str, list[dict[str, Any]]] = {key: [] for key, _name in CAST}
    knowledge: dict[str, Any] = {}
    # Этап и то, сколько ходов мастера разобрано после него: сценарий нового запуска продолжается с
    # первого неразобранного хода. После каждого этапа сервер перезапускается.
    stages = [(round_one, 2), (round_two, 3), (battle, 7), (camp, 9)]
    consumed = 0
    for number in range(len(stages) + 1):
        tail = {"fake_llm_scenario": write_scenario(from_turn=consumed)}
        async with serve(rng=evening_dice, changes=tail, **knowledge) as server:
            if number == 0:
                await begin_evening(server)
                consumed = 1
            else:
                await reconnect(server)
                await assert_clients_agree_with_snapshots(server, f"после перезапуска {number}")
                stage, consumed = stages[number - 1]
                await stage(server)
            await server.settle()
            await assert_clients_agree_with_snapshots(server, f"после этапа {number}")
            for key, watcher in server.watchers.items():
                seen[key] += watcher.frames
            knowledge = carry(server)
            last = server

    # Каждый ход мастера разобран ровно один раз, перезапуски лишних ходов не запускали.
    assert model_calls.turns == 9
    assert [(log.attempt, log.error) for log in await last.logs()] == [(1, None)] * 9
    assert [row.version for row in await last.snapshots()] == list(range(1, 12))
    state = await last.state()
    hero = next(iter(state.party.values()))
    assert (hero.hp, state.chronicle.summary) == (hero.hp_max, SUMMARY)
    # Приватность сквозь все перезапуски: чужих тайн в кадрах нет ни до, ни после.
    for key, frames in seen.items():
        text = json.dumps(frames, ensure_ascii=False)
        for owner, markers in OWN_SECRETS.items():
            for marker in markers:
                assert (marker in text) == (owner == key), f"{marker!r} у игрока {key}"
        for hidden in (*HIDDEN_FROM_PLAYERS, HIDDEN_EXIT_ID, *SERVER_SECRETS):
            assert hidden not in text
