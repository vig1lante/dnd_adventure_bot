"""Вдохновение через весь путь: мастер даёт, игрок отмечает в ходе, сервер тратит и показывает.

Мастер выдаёт вдохновение инструментом (строка о выдаче видна всем), игрок отмечает «Вдохновение» в
черновике хода, бросок идёт с преимуществом и ровно один раз, лист героя у владельца обновляется.
Если ход тайный, броски видны только автору. Кубы подставлены: преимущество кидает два куба.
"""

import json
from typing import Any

import pytest
import yaml
from evening import (
    ScriptedThenSeeded,
    assert_clients_agree_with_snapshots,
    begin_evening,
    chronicle,
    fresh_snapshot,
    say,
    tool,
    view_of,
)

REASON = "Вера красиво сыграла характер"


def inspiration_scenario() -> dict[str, Any]:
    check = dict(character_id="$pc3", ability="dex", skill="acrobatics", dc=10)
    return {
        "turns": [
            {"steps": [chronicle("Шестеро собрались."), say("Вечер, трое в сборе.")]},
            {
                "steps": [
                    tool("grant_inspiration", character_id="$pc3", reason=REASON),
                    chronicle("Вера получила вдохновение."),
                    say("Вера вызывает одобрительный гул."),
                ]
            },
            {
                "steps": [
                    tool("request_check", reason="прыжок через ров", **check),
                    tool("request_check", reason="прыжок на тот берег", **check),
                    chronicle("Вера прыгала дважды."),
                    say("Вера прыгает через ров, а потом ещё раз."),
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


@pytest.mark.parametrize("secret", [False, True], ids=["открыто", "тайно"])
async def test_inspiration_is_given_by_the_master_and_spent_once_on_the_next_roll_of_the_player(
    secret, serve, tmp_path, model_calls
):
    path = tmp_path / "inspiration.yaml"
    path.write_text(yaml.safe_dump(inspiration_scenario(), allow_unicode=True), encoding="utf-8")
    dice = ScriptedThenSeeded([8, 17, 5])  # преимущество: два куба и выбор лучшего; затем один
    async with serve(rng=dice, changes={"fake_llm_scenario": path}) as server:
        await begin_evening(server)

        # Раунд 1: мастер выдаёт вдохновение Вере. Об этом знают все, лист Веры показывает его.
        await server.submit("alice", "Осмотреть деревню")
        await server.ok("boris", "POST", "/action/pass")
        await server.ok("vera", "POST", "/action/pass")
        await server.host("collect")
        await server.settle()
        vera = next(h for h in (await server.state()).party.values() if h.name == "Вера")
        assert vera.inspiration is True
        for key in ("alice", "boris", "vera"):
            lines = [i["text"] for i in view_of(server, key)["feed"] if i["kind"] == "system"]
            assert f"Вера получает вдохновение: {REASON}." in lines
        assert view_of(server, "vera")["character"]["inspiration"] is True
        await assert_clients_agree_with_snapshots(server, "после выдачи вдохновения")

        # Раунд 2: Вера отмечает «Вдохновение» (тайно или открыто) и её ход разбирается.
        saved = await server.draft("vera", "Перепрыгнуть ров", secret=secret, inspiration=True)
        assert saved.json()["inspiration"] is True  # у героя оно есть, отметка принята
        await server.ok("alice", "POST", "/action/pass")
        await server.ok("boris", "POST", "/action/pass")
        await server.ok("vera", "POST", "/action/submit")
        await server.host("collect")
        await server.settle()

        state = await server.state()
        vera = next(h for h in state.party.values() if h.name == "Вера")
        assert vera.inspiration is False  # потрачено
        for key in ("alice", "boris", "vera"):
            feed = view_of(server, key)["feed"]
            jumps = [i for i in feed if i["kind"] == "roll" and "прыжок" in i["data"]["reason"]]
            assert len(jumps) == (2 if key == "vera" or not secret else 0), key
        # Преимущество досталось только первому броску: два куба и лучший, затем один обычный.
        cards = [
            i["data"]
            for i in view_of(server, "vera")["feed"]
            if i["kind"] == "roll" and "прыжок" in i["data"]["reason"]
        ]
        assert [(c["advantage"], c["rolls"]) for c in cards] == [
            ("advantage", [8, 17]),
            ("none", [5]),
        ]
        assert cards[0]["kept"] == [17]
        assert (await fresh_snapshot(server, "vera"))["character"]["inspiration"] is False
        if secret:  # тайный ход: чужие кадры не содержат ни броска, ни причины, ни текста хода
            for key in ("alice", "boris"):
                frames = json.dumps(server.watchers[key].frames, ensure_ascii=False)
                assert "прыжок" not in frames and "Перепрыгнуть" not in frames
        await assert_clients_agree_with_snapshots(server, "после траты вдохновения")
