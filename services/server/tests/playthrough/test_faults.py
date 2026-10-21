"""Сбой в любой фазе не оставляет половины хода; «Перегенерировать» откатывает всё; пока мастер
думает, ни один маршрут игры ничего не меняет.

Сбой — отказ диска при записи результата хода (`disk_fault`): модель уже ответила, копия состояния
готова, а запись не прошла. Откат обязан убрать всё, что к этому моменту попало в сессию: снимок,
сообщения, статусы. «Повторить» после этого проходит с теми же кубами и применяет результат один
раз.
"""

import asyncio
import logging
from typing import Any

import pytest
from evening import (
    CAST,
    FAULT_CASES,
    HAMMER_CASES,
    PUBLIC_ACTION,
    SUMMARY,
    TIMEOUT_S,
    assert_clients_agree_with_snapshots,
    begin_evening,
    bring_to,
    fresh_snapshot,
    hammer_every_route,
    host_failure_notes,
    master_fingerprint,
    play_evening,
    view_of,
    world_fingerprint,
)

from dnd_server.db.models import GamePhase, MessageKind, TurnKind, TurnPhase, TurnStatus


@pytest.mark.parametrize("case", FAULT_CASES, ids=lambda case: case.name)
async def test_a_failed_write_leaves_nothing_behind_on_any_turn_and_the_retry_goes_through(
    case, serve, evening_dice, disk_fault, write_scenario, model_calls
):
    scenario = write_scenario(repeat_turn=case.repeat_turn)
    async with serve(rng=evening_dice, changes={"fake_llm_scenario": scenario}) as server:
        trigger = await bring_to(server, case)
        before = await master_fingerprint(server)
        dice_before = evening_dice.checkpoint()
        disk_fault.arm(case.newer_than)

        pressed = await trigger()
        await server.settle()

        # Запись не прошла, ход не разобран: всё, что писал мастер, осталось как было.
        assert pressed.status_code == 200, pressed.text
        assert disk_fault.hits == 1
        after = await master_fingerprint(server)
        if case.name == "opening":
            # Зацепки записаны раньше и остаются; сорвалось только открытие: ни второй версии, ни
            # рассказа, ни следов хода, кроме него самого.
            assert after["state_version"] == 1 and [v for v, _ in after["snapshots"]] == [1]
            assert [m[3] for m in after["messages"]] == ["private"] * 3
            assert [t[2] for t in after["turns"]] == [TurnKind.OPENING]
            before = after | {"logs": before["logs"]}
        else:
            assert after | {"logs": 0} == before | {"logs": 0}
        game = await server.game()
        assert game.turn_phase == TurnPhase.DM_FAILED and game.deadline_at is None
        in_start = case.name in ("weaving", "opening")
        assert game.phase == (GamePhase.WEAVING if in_start else GamePhase.PLAYING)
        assert game.state_version == before["state_version"]
        # Попытка записана неудачной, хост видит причину (и только он), ходы игроков целы.
        [note] = await host_failure_notes(server)
        assert note.audience == server.people["alice"].user_id
        failed = [t for t in await server.turns() if t.status == TurnStatus.FAILED]
        if case.name == "weaving":  # хода ещё нет, журнал хода не ведётся: сообщение хосту и всё
            assert (game.current_turn_id, failed) == (None, [])
            assert len(await server.logs()) == before["logs"]
        else:
            assert [t.id for t in failed] == [game.current_turn_id]
            last = (await server.logs())[-1]
            assert (last.output, "OSError" in last.error) == (None, True)
            assert len(await server.logs()) == before["logs"] + 1
        await server.sync()
        assert view_of(server, "alice")["phase"]["turn_phase"] == "dm_failed"
        assert [f for f in view_of(server, "boris")["feed"] if f["text"] == note.text] == []
        assert [f for f in view_of(server, "alice")["feed"] if f["text"] == note.text]
        # Зацепки пишутся без `party` и `character`: они приходят с рассказом открытия, которое
        # сорвалось. Новый снимок их уже содержит, поэтому эти блоки у старого сокета отстают.
        lagging = ("party", "character", "combat") if case.name == "opening" else ()
        await assert_clients_agree_with_snapshots(server, "после сбоя", skip=lagging)

        # «Повторить»: тот же ход с теми же кубами, ровно один раз и без следов неудачи в состоянии.
        evening_dice.restore(dice_before)
        assert (await server.game_call("alice", "POST", "/host/retry")).status_code == 200
        await server.settle()

        game = await server.game()
        assert game.turn_phase != TurnPhase.DM_FAILED
        # Вплетение пишет версию 1 и открытие версию 2; любой другой ход — одну новую версию.
        final = before["state_version"] + (2 if case.name == "weaving" else 1)
        # После утра двадцать записей летописи сжимаются: это ещё одна версия поверх хода.
        squeezed = final + (1 if case.name == "camp_end" else 0)
        assert [row.version for row in await server.snapshots()] == list(range(1, squeezed + 1))
        assert game.state_version == squeezed
        if case.name != "weaving":
            turn = [t for t in await server.turns() if t.id == note.turn_id]
            assert [(t.status, t.state_version_after) for t in turn] == [(TurnStatus.DONE, final)]
        story = [m for m in await server.messages(MessageKind.NARRATION) if not m.superseded]
        assert len({m.text for m in story}) == len(story)  # ни один рассказ не записан дважды
        hooks = [m for m in await server.messages(MessageKind.PRIVATE) if "Твоя зацепка" in m.text]
        assert len(hooks) == 3
        await assert_clients_agree_with_snapshots(server, "после повтора")
        # Результат хода, который нельзя применить дважды, применён один раз.
        state = await server.state()
        hero = {name: h for h in state.party.values() for _key, name in CAST if h.name == name}
        if case.name == "round":
            assert hero["Алиса"].hp == hero["Алиса"].hp_max - 3
        if case.name == "camp_end":
            assert hero["Алиса"].hp == hero["Алиса"].hp_max and state.time.day == 2


# --- сбой рассылки после коммита ---


async def test_a_broken_broadcast_after_the_commit_does_not_undo_the_turn(
    serve, evening_dice, monkeypatch, caplog, model_calls
):
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        await server.submit("alice", PUBLIC_ACTION)
        await server.ok("boris", "POST", "/action/pass")
        await server.ok("vera", "POST", "/action/pass")
        stale = {key: len(watcher.frames) for key, watcher in server.watchers.items()}

        async def broken_send(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("хаб недоступен")

        with monkeypatch.context() as patch:
            patch.setattr(server.app.state.hub, "send", broken_send)
            await server.host("collect")
            await server.settle()

        # Ход записан: рассылка идёт после коммита и откатить его не может, а сбой — в журнале.
        game = await server.game()
        assert (game.turn_phase, game.state_version) == (TurnPhase.DISCUSSION, 3)
        assert [t.status for t in await server.turns() if t.number == 2] == [TurnStatus.DONE]
        assert "Не удалось разослать событие" in caplog.text
        assert model_calls.turns == 2
        for key, watcher in server.watchers.items():  # старые сокеты событий не получили
            assert len(watcher.frames) <= stale[key] + 1
        # Переподключившийся клиент получает снимок со всем, что произошло.
        for key in ("alice", "boris", "vera"):
            snapshot = await fresh_snapshot(server, key)
            assert snapshot["phase"]["turn_phase"] == "discussion"
            assert any("Алиса читает объявления" in i["text"] for i in snapshot["feed"])


# --- «Перегенерировать» откатывает всё ---


async def test_regenerating_the_morning_takes_back_the_rest_the_story_and_the_squeezed_chronicle(
    serve, evening_dice, model_calls
):
    async with serve(rng=evening_dice) as server:
        await play_evening(server)
        before = await server.state()
        assert before.chronicle.summary == SUMMARY and before.version == 11
        old_morning = [m for m in await server.messages() if m.turn_id and m.superseded is False]
        day = before.time.day

        regenerated = await server.host("regenerate")
        await server.settle()

        assert regenerated.json()["phase"] == "playing"
        game = await server.game()
        assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.DISCUSSION)
        # Состояние вернулось к лагерю (версия 9), утро проведено заново: отдых один, день один.
        state = await server.state()
        assert (state.version, game.state_version) == (12, 12)
        # День прибавился при первом утре и не прибавился во второй раз.
        assert state.time.day == day
        hero = state.party[next(iter(state.party))]
        assert hero.hp == hero.hp_max
        # Сжатая летопись откатилась вместе с утром: записи лагеря и новая, сводки нет.
        assert state.chronicle.summary in (None, "")
        assert len(state.chronicle.entries) == 9 and state.chronicle.entries[-1] == (
            "Отряд идёт дальше."
        )
        assert [row.version for row in await server.snapshots()] == list(range(1, 13))
        # Старое утро снято, реплики у костра остались, журнал хранит обе попытки хода.
        messages = await server.messages()
        superseded = [m for m in messages if m.superseded]
        assert {m.kind for m in superseded} == {MessageKind.SYSTEM, MessageKind.NARRATION}
        assert len([m for m in messages if m.kind == MessageKind.CAMP_LINE]) == 3
        assert not [m for m in messages if m.kind == MessageKind.CAMP_LINE and m.superseded]
        camp_end = [t for t in await server.turns() if t.kind == TurnKind.CAMP_END]
        logs = [log for log in await server.logs() if log.turn_id == camp_end[0].id]
        assert [(log.attempt, log.error) for log in logs] == [(1, None), (2, None)]
        # Ход следующего раунда тот же по номеру: прежний открытый снят, новый открыт.
        turns = await server.turns()
        assert sorted(t.status.value for t in turns if t.number == 10) == ["open", "superseded"]
        assert model_calls.turns == 10
        # Клиенты получили надгробия снятых записей и совпадают со снимками.
        await assert_clients_agree_with_snapshots(server, "после перегенерации утра")
        for key, _name in CAST:
            texts = [item["text"] for item in view_of(server, key)["feed"]]
            assert texts.count("Утро. Туман отступил, и отряд снова полон сил.") == 0
            assert texts.count("Длинный отдых: отряд восстановил силы.") == 1
        assert old_morning  # контроль: до перегенерации у хода были записи


async def test_regenerating_the_opening_keeps_the_hooks_and_replaces_only_the_first_story(
    serve, evening_dice, write_scenario
):
    # Первый ход сценария стоит в очереди дважды: перегенерированное открытие читает его же.
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=0)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        await begin_evening(server)
        hooks_before = [m for m in await server.messages(MessageKind.PRIVATE)]
        assert len(hooks_before) == 3

        await server.host("regenerate")
        await server.settle()

        game = await server.game()
        assert (game.phase, game.turn_phase) == (GamePhase.PLAYING, TurnPhase.DISCUSSION)
        # Зацепки (они записаны до хода, у них нет хода) на месте и не продублированы.
        hooks_after = [m for m in await server.messages(MessageKind.PRIVATE) if not m.superseded]
        assert [m.id for m in hooks_after] == [m.id for m in hooks_before]
        state = await server.state()
        assert len(state.pc_hooks) == 3 and state.version == 3
        story = [m for m in await server.messages(MessageKind.NARRATION)]
        assert [m.superseded for m in story] == [True, False]
        assert (await server.logs())[-1].attempt == 2
        await assert_clients_agree_with_snapshots(server, "после перегенерации открытия")
        for key, _name in CAST:
            kinds = [item["kind"] for item in view_of(server, key)["feed"]]
            assert kinds == ["private", "narration"]


# --- мастер думает: по любому маршруту игры ничего не меняется ---


@pytest.mark.parametrize("case", HAMMER_CASES, ids=lambda case: case.name)
async def test_while_the_master_thinks_every_route_of_the_game_refuses_and_changes_nothing(
    case, serve, evening_dice, gate, model_calls, route_table
):
    async with serve(rng=evening_dice) as server:
        trigger = await bring_to(server, case)
        asked = (model_calls.turns, len(model_calls.json))
        gate.arm()

        pressed = await trigger()
        await asyncio.wait_for(gate.entered.wait(), TIMEOUT_S)
        assert pressed.status_code == 200, pressed.text
        assert (await server.game()).turn_phase == TurnPhase.RESOLVING
        held = await world_fingerprint(server)
        at_gate = (model_calls.turns, len(model_calls.json))

        statuses = await hammer_every_route(server, route_table)

        # Каждый маршрут отказал по состоянию игры, игра не изменилась, второй мастер не позван.
        assert set(statuses.values()) == {409}
        assert await world_fingerprint(server) == held
        assert (model_calls.turns, len(model_calls.json)) == at_gate
        # Новый сокет, подключившийся посреди разбора, видит «мастер думает» и мастера не запускает.
        await assert_clients_agree_with_snapshots(server, "пока мастер думает")
        assert (model_calls.turns, len(model_calls.json)) == at_gate
        gate.open()
        await server.settle()
        game = await server.game()
        assert game.turn_phase not in (TurnPhase.RESOLVING, TurnPhase.DM_FAILED)
        # Мастер ответил один раз: вплетение (JSON и открытие) или один ход; после утра ещё сжатие
        # летописи (JSON).
        extra = (1, 1) if case.name in ("weaving", "camp_end") else (1, 0)
        assert (model_calls.turns - asked[0], len(model_calls.json) - asked[1]) == extra
        await assert_clients_agree_with_snapshots(server, "после того, как мастер ответил")


# --- сбой записи самой неудачи ---


# Если не записался результат хода, а потом и запись неудачи (диск всё ещё полон, база занята), игра
# не остаётся в `resolving` без мастера: хост видит `dm_failed` и повторяет ход.
@pytest.mark.parametrize("case", [FAULT_CASES[0], FAULT_CASES[2]], ids=lambda case: case.name)
async def test_when_even_the_record_of_the_failure_cannot_be_written_the_host_can_still_repeat(
    case, serve, evening_dice, disk_fault, write_scenario
):
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=case.repeat_turn)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        trigger = await bring_to(server, case)
        dice_before = evening_dice.checkpoint()
        disk_fault.arm(case.newer_than, also_the_record=True)
        await trigger()
        await server.settle()
        assert disk_fault.hits == 2  # не прошли и запись результата, и запись неудачи

        evening_dice.restore(dice_before)
        retried = await server.game_call("alice", "POST", "/host/retry")

        assert retried.status_code == 200, retried.text
        await server.settle()
        assert (await server.game()).turn_phase == TurnPhase.DISCUSSION


# Диск не отвечает совсем: не проходит ни результат, ни запись неудачи, ни даже короткая отметка
# `dm_failed`. Игра остаётся в `resolving` без живого разбора, и «Повторить» принимает её без
# перезапуска сервера (разбор, которого нет в памяти, считается прерванным).
@pytest.mark.parametrize("case", [FAULT_CASES[0], FAULT_CASES[2]], ids=lambda case: case.name)
async def test_when_nothing_can_be_written_at_all_the_host_repeats_without_a_restart(
    case, serve, evening_dice, disk_fault, write_scenario, caplog
):
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=case.repeat_turn)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        trigger = await bring_to(server, case)
        dice_before = evening_dice.checkpoint()
        disk_fault.arm(case.newer_than, also_the_next=2)
        await trigger()
        await server.settle()

        assert disk_fault.hits == 3  # результат, запись неудачи и короткая отметка
        game = await server.game()
        assert game.turn_phase == TurnPhase.RESOLVING  # в базе записать нечего
        # Сбой в журнале сервера назван с id игры (и хода, если он есть).
        ids = [server.game_id, *([game.current_turn_id] if game.current_turn_id else [])]
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(all(i in message for i in ids) for message in errors), errors

        evening_dice.restore(dice_before)
        retried = await server.game_call("alice", "POST", "/host/retry")

        assert retried.status_code == 200, retried.text
        await server.settle()
        assert (await server.game()).turn_phase == TurnPhase.DISCUSSION
        await assert_clients_agree_with_snapshots(server, "после «Повторить» без перезапуска")
