"""Двойные нажатия и гонки кнопок хоста и ходов игроков: мастера зовут ровно один раз.

Все пары запросов уходят в игру одновременно настоящими маршрутами. Выигравший резервирует ход,
проигравший получает 409; ни два успеха, ни пятисотый ответ, ни второй вызов модели невозможны.
"""

import asyncio

from dnd_server.db.models import TurnPhase, TurnStatus
from evening import (
    BORIS_SECRET_ACTION,
    COLLECT_SECONDS,
    PUBLIC_ACTION,
    FaultCase,
    ScriptedThenSeeded,
    Server,
    assert_clients_agree_with_snapshots,
    begin_evening,
    bring_to,
    round_one,
    round_two,
)


async def press_together(server: Server, *presses: tuple[str, str, str]) -> list[int]:
    """Одновременные запросы `(кто, метод, путь игры)`; коды ответов по порядку."""
    responses = await asyncio.wait_for(
        asyncio.gather(*(server.game_call(who, method, path) for who, method, path in presses)),
        10,
    )
    return [response.status_code for response in responses]


def assert_one_wins(statuses: list[int]) -> None:
    assert sorted(statuses) == [200, 409], statuses


async def test_two_retry_buttons_at_once_repeat_the_failed_turn_once(
    serve, evening_dice, disk_fault, write_scenario, model_calls
):
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=1)}
    async with serve(rng=evening_dice, changes=scenario) as server:
        await begin_evening(server)
        await server.submit("alice", PUBLIC_ACTION)
        await server.submit("boris", BORIS_SECRET_ACTION, secret=True)
        await server.ok("vera", "POST", "/action/pass")
        dice = evening_dice.checkpoint()
        disk_fault.arm(2)
        await server.host("collect")
        await server.settle()
        assert (await server.game()).turn_phase == TurnPhase.DM_FAILED
        evening_dice.restore(dice)
        calls = model_calls.turns

        statuses = await press_together(
            server, ("alice", "POST", "/host/retry"), ("alice", "POST", "/host/retry")
        )
        await server.settle()

        assert_one_wins(statuses)
        assert model_calls.turns == calls + 1
        assert (await server.game()).state_version == 3
        await assert_clients_agree_with_snapshots(server, "после двойного «Повторить»")


async def test_two_regenerate_buttons_at_once_regenerate_the_story_once(
    serve, write_scenario, model_calls
):
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=1)}
    async with serve(rng=ScriptedThenSeeded([]), changes=scenario) as server:
        await begin_evening(server)
        await round_one(server)
        calls = model_calls.turns

        statuses = await press_together(
            server, ("alice", "POST", "/host/regenerate"), ("alice", "POST", "/host/regenerate")
        )
        await server.settle()

        assert_one_wins(statuses)
        assert model_calls.turns == calls + 1
        live = [t for t in await server.turns() if t.status == TurnStatus.OPEN]
        assert [t.id for t in live] == [(await server.game()).current_turn_id]
        await assert_clients_agree_with_snapshots(server, "после двойной перегенерации")


async def test_regenerate_and_collect_at_once_start_the_master_only_once(
    serve, write_scenario, model_calls
):
    scenario = {"fake_llm_scenario": write_scenario(repeat_turn=1)}
    async with serve(rng=ScriptedThenSeeded([]), changes=scenario) as server:
        await begin_evening(server)
        await round_one(server)
        # Все готовы к следующему раунду, и хост одновременно собирает ходы и перегенерирует.
        for who in ("alice", "boris", "vera"):
            await server.ok(who, "POST", "/action/pass")
        calls = model_calls.turns

        statuses = await press_together(
            server, ("alice", "POST", "/host/regenerate"), ("alice", "POST", "/host/collect")
        )
        await server.settle()

        assert_one_wins(statuses)
        assert model_calls.turns == calls + 1
        assert (await server.game()).turn_phase not in (TurnPhase.RESOLVING, TurnPhase.DM_FAILED)
        await assert_clients_agree_with_snapshots(server, "после перегенерации против сбора")


async def test_two_end_camp_buttons_at_once_end_the_camp_once(serve, evening_dice, model_calls):
    async with serve(rng=evening_dice) as server:
        await bring_to(server, FaultCase("camp_end", None, 0))
        calls = model_calls.turns

        statuses = await press_together(
            server, ("alice", "POST", "/host/camp-end"), ("alice", "POST", "/host/camp-end")
        )
        await server.settle()

        assert_one_wins(statuses)
        assert model_calls.turns == calls + 1
        state = await server.state()
        hero = next(iter(state.party.values()))
        assert (hero.hp, state.time.day) == (hero.hp_max, 2)  # длинный отдых проведён один раз
        await assert_clients_agree_with_snapshots(server, "после двойного конца привала")


async def test_the_hero_pressing_ready_and_pass_at_once_gives_the_master_one_turn(
    serve, evening_dice, model_calls
):
    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        await round_one(server)
        await round_two(server)
        await server.draft("alice", "Уклоняюсь от меча скелета")
        calls = model_calls.turns

        statuses = await press_together(
            server, ("alice", "POST", "/action/submit"), ("alice", "POST", "/action/pass")
        )
        await server.settle()

        # Первый запрос, дошедший до игры, сдаёт ход героя; второй видит, что мастер уже разбирает
        # (409) или успевает переиграть сдачу до разбора (200): в обоих случаях мастер один раз.
        assert set(statuses) <= {200, 409} and 200 in statuses
        assert model_calls.turns == calls + 1
        game = await server.game()
        assert game.turn_phase == TurnPhase.COMBAT_WAIT
        await assert_clients_agree_with_snapshots(server, "после «Готов» и «Пас» вместе")


async def test_a_pause_that_lands_between_the_close_and_the_master_does_not_lose_the_turn(
    serve, evening_dice, monkeypatch, model_calls
):
    """Хост ставит паузу в тот миг, когда сбор уже закрыт, а мастер ещё не зарезервировал ход.

    Мастер отказывает (игра на паузе), ходы игроков закрыты и не теряются. После «Продолжить»
    отсчёт снова идёт с прежним остатком, по его концу сбор закрывается заново, и мастер зван
    ровно один раз."""
    from dnd_server.turns import flow as flow_module

    real_start_turn = flow_module.start_turn
    pressed: list[int] = []

    async def pause_first(flow, game_id, **options):
        if not pressed:
            pressed.append(1)
            await flow_module.pause(flow, game_id, host_id)
        return await real_start_turn(flow, game_id, **options)

    async with serve(rng=evening_dice) as server:
        await begin_evening(server)
        host_id = server.people["alice"].user_id
        await server.draft("alice", PUBLIC_ACTION)
        await server.host("collect")
        await server.settle()

        with monkeypatch.context() as patch:
            patch.setattr(flow_module, "start_turn", pause_first)
            closed = await server.game_call("alice", "POST", "/host/close")
            await server.settle()

        assert closed.status_code == 409  # мастер отказал: игра на паузе
        game = await server.game()
        assert (game.phase.value, game.turn_phase) == ("paused", TurnPhase.COLLECTING)
        assert model_calls.turns == 1  # только открытие
        assert (await server.actions(2))["alice"].status.value == "auto_submitted"
        resumed = await server.host("resume")
        assert resumed.json()["turn_phase"] == "collecting"
        assert list(server.scheduler.pending()) == [server.game_id]  # отсчёт идёт с остатком
        await server.scheduler.advance(COLLECT_SECONDS)
        await server.settle()
        game = await server.game()
        assert (game.turn_phase, game.state_version, model_calls.turns) == (
            TurnPhase.DISCUSSION,
            3,
            2,
        )
        assert (await server.actions(2))["alice"].text == PUBLIC_ACTION
        await assert_clients_agree_with_snapshots(server, "после паузы между закрытием и мастером")
