"""Игры переживают перезапуск сервера (план B6.1, B6.3, спека §1 п.5, инвариант «в»).

Игра создаётся настоящими запросами REST: приключение, игроки, три способа создать персонажа,
готовность, старт. Потом сервер останавливается и поднимается заново на том же файле SQLite (новое
приложение, новый `lifespan`, пустая память) — как после перезапуска у владельца. Всё, что видели
игроки до остановки, они видят после неё, а игра идёт дальше.
"""

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from dnd_engine.abilities import Ability
from dnd_engine.adventure.loader import load_adventure
from dnd_server.db.models import Game, GamePhase, PendingRoll, PlayerCharacter

# tests/flows/test_restart.py -> flows -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
COUNTDOWNS = {"collect_seconds": 45, "hurry_seconds": 20, "camp_seconds": 100}


def without_clock(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Снимок без серверного времени: оно меняется при каждом чтении."""
    return {key: value for key, value in snapshot.items() if key != "server_time"}


async def test_a_started_game_is_read_back_after_a_restart_though_its_folder_is_gone(
    serve, content_dir
):
    created: dict[str, dict[str, Any]] = {}
    async with serve() as first:
        host, friend, other = [await first.dev_player(n) for n in (1, 2, 3)]
        game = await first.new_game(host, **COUNTDOWNS)
        for player in (friend, other):
            await first.join(player, game["code"])
        for player, make in ((host, first.detailed), (friend, first.quick), (other, first.random)):
            response = await make(player, game["id"])
            assert response.status_code == 201, response.text
            created[player.id] = response.json()["character"]
            assert (await first.ready(player, game["id"])).status_code == 200
        started = await first.call(host, "POST", f"/api/games/{game['id']}/start")
        assert started.status_code == 200, started.text
        await first.master_started(game["id"])
        game_before = await first.game(host, game["id"])
        snapshots_before = {
            p.id: await first.snapshot(game["id"], p) for p in (host, friend, other)
        }
        first.assert_clean()

    # Приключение убрали с сервера: игра идёт по своей копии в базе.
    shutil.rmtree(content_dir / "demo-crypt")

    async with serve() as second:
        second.tokens += first.tokens  # токены выданы прежним запуском, но принимаются новым
        assert await second.game(host, game["id"]) == game_before
        assert game_before["phase"] == "weaving"
        assert {k: game_before[k] for k in COUNTDOWNS} == COUNTDOWNS
        assert game_before["adventure_title"] == load_adventure(DEMO_ADVENTURE_DIR).title
        mine = await second.call(friend, "GET", "/api/games")
        assert [g["id"] for g in mine.json()] == [game["id"]]
        for player in (host, friend, other):
            snapshot = await second.snapshot(game["id"], player)
            assert without_clock(snapshot) == without_clock(snapshots_before[player.id])
            assert snapshot["character"] == created[player.id]
        # Каталог после перезапуска пуст, а игра на месте; новая игра на убранном приключении — нет.
        assert (await second.call(host, "GET", "/api/adventures")).json() == []
        assert (
            await second.call(host, "POST", "/api/games", json={"adventure_id": "demo-crypt"})
        ).status_code == 404
        # Игра не начинается заново сама и не стартует повторно по просьбе.
        assert second.hooks.started == []
        again = await second.call(host, "POST", f"/api/games/{game['id']}/start")
        assert again.status_code == 409
        # Игрок игры входит по коду, как раньше; посторонний на начатую игру — нет.
        assert (await second.join(friend, game["code"])) == game_before
        stranger = await second.dev_player(4)
        late = await second.call(stranger, "POST", "/api/games/join", json={"code": game["code"]})
        assert late.status_code == 409
        second.assert_clean()

    async with first.db() as session:
        stored = await session.get(Game, game["id"])
        rows = (await session.scalars(select(PlayerCharacter))).all()
    # Копия приключения в базе осталась ровно такой, какой её взяли из каталога.
    assert stored.adventure_data == load_adventure(DEMO_ADVENTURE_DIR).model_dump(mode="json")
    assert (stored.phase, stored.started_at is not None) == (GamePhase.WEAVING, True)
    assert {row.id: row.data for row in rows} == {c["id"]: c for c in created.values()}


async def test_a_lobby_goes_on_after_a_restart_with_ready_marks_and_the_rolled_set(serve):
    async with serve() as first:
        host, friend = [await first.dev_player(n) for n in (1, 2)]
        game = await first.new_game(host, **COUNTDOWNS)
        await first.join(friend, game["code"])
        values = await first.roll_abilities(host, game["id"])
        assert (await first.quick(friend, game["id"])).status_code == 201
        assert (await first.ready(friend, game["id"])).status_code == 200
        lobby_before = await first.game(host, game["id"])
        first.assert_clean()

    async with serve() as second:
        second.tokens += first.tokens
        # Отметка «Готов», наличие персонажа и длительности на месте.
        assert await second.game(host, game["id"]) == lobby_before
        assert [(p["name"], p["ready"], p["has_character"]) for p in lobby_before["players"]] == [
            ("dev-1", False, False),
            ("dev-2", True, True),
        ]
        # Набор, который сервер бросил до остановки, цел: по нему можно создать персонажа.
        scores = dict(zip((a.value for a in Ability), values, strict=True))
        created = await second.detailed(
            host,
            game["id"],
            method="roll",
            base_scores=scores,
            background_bonus={"str": 1, "dex": 1, "con": 1},
        )
        assert created.status_code == 201, created.text
        assert created.json()["character"]["scores"]["str"] == scores["str"] + 1
        assert (await second.ready(host, game["id"])).status_code == 200
        # Игра продолжается: старт после перезапуска переводит её в weaving и зовёт мастера.
        started = await second.call(host, "POST", f"/api/games/{game['id']}/start")
        assert started.status_code == 200, started.text
        await second.master_started(game["id"])
        assert second.hooks.started == [game["id"]]
        assert started.json()["phase"] == "weaving"
        second.assert_clean()

    async with second.db() as session:
        roll = await session.get(PendingRoll, (game["id"], host.id))
    assert roll.values == values  # набор не затёрт созданием персонажа
