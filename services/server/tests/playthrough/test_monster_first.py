"""Монстр ходит первым: мастер разыгрывает его ход сам, без игрока, и затем зовёт героя.

Если текущий боец — монстр, сервер сразу запускает ход мастера. Здесь это идёт через настоящие
маршруты, сокеты и бот: игроки ничего не нажимают, а после ответа мастера очередь стоит на герое, и
«Твой ход в бою» получает только он.
"""

import json

import yaml
from evening import (
    ScriptedThenSeeded,
    assert_clients_agree_with_snapshots,
    begin_evening,
    chronicle,
    say,
    tool,
    view_of,
)

from dnd_server.bot.notify import combat_turn_text
from dnd_server.db.models import MessageKind, TurnKind, TurnPhase
from dnd_server.realtime.views import UNSEEN_ACTOR

# Кубы: инициатива героев по 5 и скелета 20 (скелет ходит первым), затем атака скелета — промах.
DICE = [5, 5, 5, 20, 3]


def monster_first_scenario() -> dict:
    return {
        "turns": [
            {"steps": [chronicle("Отряд собрался."), say("Вечер, трое в сборе.")]},
            {
                "steps": [
                    tool("move_party", location_id="cemetery", via_exit="to-cemetery"),
                    tool(
                        "start_combat",
                        monsters=[{"monster_id": "skeleton", "count": 1, "zone": "gate"}],
                    ),
                    chronicle("У ворот кладбища поднялся скелет."),
                    say("Из тумана выходит скелет и бросается на отряд."),
                ]
            },
            # Ход скелета, который сервер запустил сам.
            {
                "steps": [
                    tool(
                        "monster_action",
                        monster_id="skeleton-1",
                        action_id="shortsword",
                        target_ids=["$pc1"],
                    ),
                    chronicle("Скелет замахнулся на Алису и промахнулся."),
                    say("Ржавый меч скелета свистит мимо Алисы."),
                ]
            },
            {"repeat": True, "steps": [chronicle("Дальше."), say("Дальше.")]},
        ],
        "json": {
            "weaving": {
                "hooks": [
                    {
                        "character_id": f"$pc{n}",
                        "npc_links": [{"npc_id": "marta", "relation": "знакомая"}],
                        "personal_goal": "цель",
                    }
                    for n in (1, 2, 3)
                ]
            }
        },
    }


async def test_a_monster_that_goes_first_is_played_by_the_master_and_then_the_hero_is_called(
    serve, tmp_path, model_calls
):
    path = tmp_path / "monster-first.yaml"
    path.write_text(yaml.safe_dump(monster_first_scenario(), allow_unicode=True), encoding="utf-8")
    async with serve(rng=ScriptedThenSeeded(DICE), changes={"fake_llm_scenario": path}) as server:
        await begin_evening(server)
        for who in ("alice", "boris", "vera"):
            await server.ok(who, "POST", "/action/pass")
        await server.host("collect")
        await server.settle()

        # Разобрали раунд и ход скелета, игроки не нажали ни одной кнопки в бою.
        assert model_calls.turns == 3
        game = await server.game()
        assert game.turn_phase == TurnPhase.COMBAT_WAIT
        state = await server.state()
        hero_ids = [hero.id for hero in state.party.values()]
        assert state.combat.order[0] == "skeleton-1"
        assert state.combat.order[state.combat.turn_index] == hero_ids[0]  # теперь ходит Алиса
        turns = await server.turns()
        assert [t.kind for t in turns][-3:] == [TurnKind.ROUND, TurnKind.COMBAT, TurnKind.COMBAT]
        assert (await server.logs())[-1].request["acting"] == ["skeleton-1"]
        # Зовут только того, чья очередь; бой не засыпает остальных уведомлениями.
        assert server.notifier.texts_for(server.people["alice"].user_id)[-1] == combat_turn_text()
        for key in ("boris", "vera"):
            assert combat_turn_text() not in server.notifier.texts_for(server.people[key].user_id)
        # Бросок скелета общий: его видят все.
        for key in ("alice", "boris", "vera"):
            rolls = [i["text"] for i in view_of(server, key)["feed"] if i["kind"] == "roll"]
            assert any(text.startswith("Скелет:") for text in rolls), key
        await assert_clients_agree_with_snapshots(server, "после хода скелета")

        # Дальше бой идёт как обычно: ход героя разбирает мастер.
        await server.submit("alice", "Бью скелета")
        await server.settle()
        assert model_calls.turns == 4
        await assert_clients_agree_with_snapshots(server, "после хода героя")


# Кубы: инициатива героев по 5 и скелета 20, затем два запаса на атаку скелета с преимуществом.
HIDDEN_DICE = [5, 5, 5, 20, 3, 3]


def hidden_monster_scenario() -> dict:
    """Засада: скелет становится невидимым, едва начался бой, и стреляет из лука. Рассказ мастера
    о нём молчит."""
    scenario = monster_first_scenario()
    scenario["turns"][1] = {
        "steps": [
            tool("move_party", location_id="cemetery", via_exit="to-cemetery"),
            tool("start_combat", monsters=[{"monster_id": "skeleton", "count": 1, "zone": "gate"}]),
            tool("apply_effect", target_id="skeleton-1", condition="invisible"),
            chronicle("На кладбище началась засада."),
            say("Из тумана доносится сухой шорох."),
        ]
    }
    scenario["turns"][2] = {
        "steps": [
            tool(
                "monster_action",
                monster_id="skeleton-1",
                action_id="shortbow",
                target_ids=["$pc1"],
            ),
            chronicle("Из тумана что-то выстрелило в Алису и промахнулось."),
            say("Стрела свистит мимо Алисы."),
        ]
    }
    return scenario


async def test_the_rolls_of_a_hidden_monster_reach_every_client_without_its_name_or_id(
    serve, tmp_path
):
    path = tmp_path / "hidden-monster.yaml"
    path.write_text(yaml.safe_dump(hidden_monster_scenario(), allow_unicode=True), encoding="utf-8")
    dice = ScriptedThenSeeded(HIDDEN_DICE)
    async with serve(rng=dice, changes={"fake_llm_scenario": path}) as server:
        await begin_evening(server)
        for who in ("alice", "boris", "vera"):
            await server.ok(who, "POST", "/action/pass")
        await server.host("collect")
        await server.settle()

        # Скелет и правда невидим и стрелял: иначе проверка ниже ничего не доказывает.
        state = await server.state()
        assert any(c.id == "invisible" for c in state.combat.combatants["skeleton-1"].conditions)
        [log] = [log for log in await server.logs() if log.request["acting"] == ["skeleton-1"]]
        assert [call["name"] for call in log.trace][:2] == ["monster_action", "chronicle"]
        assert not any(call["is_error"] for call in log.trace)

        # В базе карточки скелета общие и безымянные: его инициатива и выстрел.
        cards = [m for m in await server.messages(MessageKind.ROLL) if m.audience == "all"]
        unseen = [m for m in cards if m.data["actor"] == UNSEEN_ACTOR]
        assert len(unseen) == 2 and all("actor_id" not in m.data for m in unseen)
        assert any("Короткий лук" in m.text for m in unseen)
        # У каждого игрока: ни имени, ни id скелета в карточках бросков и в блоке боя.
        for who in ("alice", "boris", "vera"):
            view = view_of(server, who)
            rolls = [item for item in view["feed"] if item["kind"] == "roll"]
            assert sum(item["data"]["actor"] == UNSEEN_ACTOR for item in rolls) == 2, who
            seen = json.dumps([rolls, view["combat"]], ensure_ascii=False)
            for trace in ("skeleton", "Скелет", "invisible"):
                assert trace not in seen, (who, trace)
        await assert_clients_agree_with_snapshots(server, "после хода невидимого скелета")
