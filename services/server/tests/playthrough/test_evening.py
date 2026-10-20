"""Один вечер игры от «Начать» до утра после привала: ход, журнал, журнал сервера, приватность.

Сервер, игроки и этапы вечера собраны в `evening.py`. Здесь вечер играется целиком, и тесты сверяют
то, что осталось: каждую контрольную точку (база, часы, бот, экраны игроков), журнал мастера,
записи журнала сервера и содержимое сокетов каждого игрока.
"""

import itertools
import json
import logging
from datetime import timedelta
from typing import Any

from dnd_engine.state import SceneType
from dnd_server.bot.notify import (
    camp_text,
    collecting_text,
    combat_turn_text,
    game_continues_text,
    game_started_text,
    hurry_text,
)
from dnd_server.db.models import (
    ActionStatus,
    GamePhase,
    MessageKind,
    TurnKind,
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
    GOAL,
    HIDDEN_EXIT_ID,
    HIDDEN_FROM_PLAYERS,
    HOOK_SECRET,
    HURRY_SECONDS,
    ModelCalls,
    PRIVATE_TO_BORIS,
    PUBLIC_ACTION,
    SERVER_SECRETS,
    SUMMARY,
    Server,
    assert_clients_agree_with_snapshots,
    fresh_snapshot,
    iso,
    play_evening,
    view_of,
)


# --- ход вечера по контрольным точкам ---

STARTED = "игра началась"
DISCUSSING = "обсуждение: черновик и тайный ход"
COLLECTING = "сбор ходов идёт"
PAUSED = "сбор на паузе"
RESUMED = "сбор после паузы"
ROUND_ONE = "первый раунд разобран"
FIGHT_BEGINS = "бой начался"
ALICE_PLAYED = "ход Алисы разобран"
BORIS_PLAYED = "тайный ход Бориса разобран"
VERA_HURRIED = "Веру торопят"
VERA_TIMED_OUT = "время Веры вышло"
FIGHT_OVER = "бой окончен"
CAMP_BEGINS = "привал начался"
CAMP_LINES = "реплики у костра"
CAMP_RESUMED = "привал продлён и после паузы"
MORNING = "утро после привала"

# Какой проверкой `Story` встречает контрольную точку этапов вечера (`evening.py`).
POINTS = {
    STARTED: "at_started",
    DISCUSSING: "at_discussing",
    COLLECTING: "at_collecting",
    PAUSED: "at_paused",
    RESUMED: "at_resumed",
    ROUND_ONE: "at_round_one",
    FIGHT_BEGINS: "at_fight_begins",
    ALICE_PLAYED: "at_alice_played",
    BORIS_PLAYED: "at_boris_played",
    VERA_HURRIED: "at_vera_hurried",
    VERA_TIMED_OUT: "at_vera_timed_out",
    FIGHT_OVER: "at_fight_over",
    CAMP_BEGINS: "at_camp_begins",
    CAMP_LINES: "at_camp_lines",
    CAMP_RESUMED: "at_camp_resumed",
    MORNING: "at_morning",
}


class Story:
    """Что должно быть на каждой контрольной точке вечера: база, часы, бот и экраны игроков."""

    def __init__(self, server: Server, calls: ModelCalls) -> None:
        self.server = server
        self.calls = calls
        self.hero: dict[str, str] = {}  # ключ игрока -> id героя
        self.day_before_camp = 0

    async def __call__(self, where: str) -> None:
        await assert_clients_agree_with_snapshots(self.server, where)
        await getattr(self, POINTS[where])()

    # --- вспомогательное ---

    def phase(self, key: str = "alice") -> dict[str, Any]:
        return view_of(self.server, key)["phase"]

    def user(self, key: str) -> str:
        return self.server.people[key].user_id

    def feed(self, key: str) -> list[dict[str, Any]]:
        return view_of(self.server, key)["feed"]

    def texts(self, key: str, kind: str | None = None) -> list[str]:
        return [item["text"] for item in self.feed(key) if kind in (None, item["kind"])]

    def told(self, key: str) -> list[str]:
        return self.server.notifier.texts_for(self.user(key))

    def combat(self) -> dict[str, Any]:
        combat = view_of(self.server, "alice")["combat"]
        assert combat is not None
        return combat

    async def hp(self, key: str) -> tuple[int, int]:
        hero = (await self.server.state()).party[self.hero[key]]
        return hero.hp, hero.hp_max

    # --- точки ---

    async def at_started(self) -> None:
        server = self.server
        game = await server.game()
        assert (game.phase, game.turn_phase, game.state_version) == (
            GamePhase.PLAYING,
            TurnPhase.DISCUSSION,
            2,
        )
        assert [row.version for row in await server.snapshots()] == [1, 2]
        assert [(t.number, t.kind, t.status) for t in await server.turns()] == [
            (1, TurnKind.OPENING, TurnStatus.DONE),
            (2, TurnKind.ROUND, TurnStatus.OPEN),
        ]
        state = await server.state()
        self.hero = dict(zip(server.people, state.party, strict=True))
        assert [hero.name for hero in state.party.values()] == [name for _key, name in CAST]
        assert set(state.pc_hooks) == set(self.hero.values())
        for key, _name in CAST:
            hook = state.pc_hooks[self.hero[key]]
            assert (hook.personal_goal, hook.secret) == (GOAL[key], HOOK_SECRET[key])
            # Зацепка лежит у игрока личным сообщением, затем общий рассказ открытия.
            first, story = self.feed(key)
            assert (first["kind"], first["private"]) == ("private", True)
            assert GOAL[key] in first["text"] and HOOK_SECRET[key] in first["text"]
            assert (story["kind"], story["private"]) == ("narration", False)
            assert self.phase(key) == {
                "phase": "playing",
                "turn_phase": "discussion",
                "deadline_at": None,
                "remaining_seconds": None,
                "scene": {"type": "exploration", "day": 1, "part": "morning"},
            }
            assert self.told(key) == [game_started_text()]
        assert self.calls.turns == 1 and self.calls.json == ["weaving"]

    async def at_discussing(self) -> None:
        server = self.server
        assert server.scheduler.pending() == {}
        statuses = view_of(server, "alice")["statuses"]
        assert {key: statuses[self.user(key)] for key, _ in CAST} == {
            "alice": "writing",
            "boris": "ready",
            "vera": "writing",
        }
        # Тайный сданный ход лежит в ленте и в снимке только у автора.
        assert BORIS_SECRET_ACTION in self.texts("boris", "action")
        assert not [t for key in ("alice", "vera") for t in self.texts(key) if "ПОЛНОЧЬ" in t]
        mine = (await fresh_snapshot(server, "boris"))["action"]
        assert mine == {
            "text": BORIS_SECRET_ACTION,
            "secret": True,
            "inspiration": False,
            "status": "submitted",
        }
        draft = (await fresh_snapshot(server, "alice"))["action"]
        assert draft == {
            "text": PUBLIC_ACTION,
            "secret": False,
            "inspiration": False,
            "status": "draft",
        }
        assert (await fresh_snapshot(server, "vera"))["action"] is None

    async def at_collecting(self) -> None:
        server = self.server
        deadline = server.scheduler.now() + timedelta(seconds=COLLECT_SECONDS)
        assert self.phase() == {
            "phase": "playing",
            "turn_phase": "collecting",
            "deadline_at": iso(deadline),
            "remaining_seconds": None,
            "scene": self.phase()["scene"],
        }
        assert server.scheduler.pending() == {server.game_id: deadline}
        # Звали только того, кто ещё пишет и не ведёт игру.
        assert self.told("vera")[-1] == collecting_text(COLLECT_SECONDS)
        assert collecting_text(COLLECT_SECONDS) not in self.told("boris") + self.told("alice")
        assert self.calls.turns == 1  # сбор ходов мастера не зовёт

    async def at_paused(self) -> None:
        server = self.server
        assert server.scheduler.pending() == {}
        paused = self.phase()
        assert (paused["phase"], paused["deadline_at"]) == ("paused", None)
        assert paused["remaining_seconds"] == COLLECT_SECONDS

    async def at_resumed(self) -> None:
        server = self.server
        assert self.calls.turns == 1  # пока игра стояла, отсчёт не сработал
        deadline = server.scheduler.now() + timedelta(seconds=COLLECT_SECONDS)
        assert self.phase()["deadline_at"] == iso(deadline)
        assert self.phase()["phase"] == "playing"
        assert server.scheduler.pending() == {server.game_id: deadline}

    async def at_round_one(self) -> None:
        server = self.server
        game = await server.game()
        assert (game.turn_phase, game.state_version, game.deadline_at) == (
            TurnPhase.DISCUSSION,
            3,
            None,
        )
        assert server.scheduler.pending() == {}
        actions = await server.actions(2)
        assert {key: (row.status, row.text, row.secret) for key, row in actions.items()} == {
            "alice": (ActionStatus.AUTO_SUBMITTED, PUBLIC_ACTION, False),
            "boris": (ActionStatus.SUBMITTED, BORIS_SECRET_ACTION, True),
            "vera": (ActionStatus.AUTO_PASS, "", False),
        }
        for key in ("alice", "boris", "vera"):
            statuses = view_of(server, key)["statuses"]
            assert set(statuses.values()) == {"writing"}  # у нового хода все снова пишут
            assert self.told(key)[-1] == game_continues_text()
        assert PUBLIC_ACTION in self.texts("vera", "action")  # автосдача видна всем
        # Тайный бросок и личное сообщение — только Борису, общий бросок и урон — всем.
        for key in ("alice", "vera"):
            assert not [t for t in self.texts(key) if "Ловкость рук" in t or "ЛИЧНОЕ" in t]
            assert any("Интеллект (Анализ)" in t for t in self.texts(key, "roll"))
        secret_roll = [i for i in self.feed("boris") if "Ловкость рук" in i["text"]]
        assert [item["private"] for item in secret_roll] == [True]
        assert PRIVATE_TO_BORIS in self.texts("boris", "private")
        hp, hp_max = await self.hp("alice")
        assert hp == hp_max - 3
        assert self.calls.turns == 2

    async def at_fight_begins(self) -> None:
        server = self.server
        game = await server.game()
        assert (game.turn_phase, game.deadline_at) == (TurnPhase.COMBAT_WAIT, None)
        assert self.phase()["scene"]["type"] == "combat"
        combat = self.combat()
        assert [fighter["id"] for fighter in combat["order"]] == [
            *(self.hero[key] for key, _ in CAST),
            "skeleton-1",
        ]
        assert combat["current_id"] == self.hero["alice"]
        assert server.scheduler.pending() == {}
        # «Твой ход в бою» получил один игрок.
        assert self.told("alice")[-1] == combat_turn_text()
        assert combat_turn_text() not in self.told("boris") + self.told("vera")
        # Писать ход может только тот, чья очередь; кнопка «Собрать ходы» в бою закрыта.
        body = {"text": "Бью первым", "secret": False, "inspiration": False}
        assert (await server.game_call("vera", "PUT", "/action", json=body)).status_code == 409
        assert (await server.game_call("alice", "POST", "/host/collect")).status_code == 409
        assert (await server.game_call("boris", "POST", "/host/hurry")).status_code == 403

    async def at_alice_played(self) -> None:
        assert self.combat()["current_id"] == self.hero["boris"]
        assert self.told("boris")[-1] == combat_turn_text()
        assert self.told("vera")[-1] != combat_turn_text()
        assert self.server.scheduler.pending() == {}

    async def at_boris_played(self) -> None:
        assert self.combat()["current_id"] == self.hero["vera"]
        hide = [i for i in self.feed("boris") if "Скрытность" in i["text"]]
        assert [item["private"] for item in hide] == [True]
        assert BORIS_SECRET_COMBAT in self.texts("boris", "action")
        for key in ("alice", "vera"):
            assert not [t for t in self.texts(key) if "Скрытность" in t or "ЗАСАДА" in t]
        assert self.told("vera")[-1] == combat_turn_text()

    async def at_vera_hurried(self) -> None:
        server = self.server
        deadline = server.scheduler.now() + timedelta(seconds=HURRY_SECONDS)
        assert self.phase()["turn_phase"] == "combat_wait"
        assert self.phase()["deadline_at"] == iso(deadline)
        assert server.scheduler.pending() == {server.game_id: deadline}
        assert self.told("vera")[-1] == hurry_text(HURRY_SECONDS)
        assert hurry_text(HURRY_SECONDS) not in self.told("alice") + self.told("boris")
        assert (await server.game_call("alice", "POST", "/host/hurry")).status_code == 409

    async def at_vera_timed_out(self) -> None:
        server = self.server
        assert self.phase()["deadline_at"] is None and server.scheduler.pending() == {}
        row = (await server.actions(6))["vera"]
        assert (row.status, row.text) == (ActionStatus.AUTO_SUBMITTED, "Уклоняется")
        assert "Уклоняется" in self.texts("alice", "action")
        # Скелет спрятался: ни порядка хода, ни зон, ни хода у него игрокам не видно.
        combat = self.combat()
        assert combat["current_id"] == self.hero["alice"] and combat["round"] == 2
        assert "skeleton-1" not in json.dumps(combat) and "Скелет" not in json.dumps(combat)
        trace = [call["name"] for call in (await server.logs())[-1].trace]
        assert "server_end_turn" in trace  # передачу хода сделал сервер

    async def at_fight_over(self) -> None:
        server = self.server
        assert view_of(server, "alice")["combat"] is None
        assert self.phase()["scene"]["type"] == "exploration"
        assert self.phase()["turn_phase"] == "discussion"
        assert "Бой окончен." in self.texts("vera", "system")
        assert (await server.state()).combat is None

    async def at_camp_begins(self) -> None:
        server = self.server
        deadline = server.scheduler.now() + timedelta(seconds=CAMP_SECONDS)
        assert self.phase()["turn_phase"] == "camp"
        assert self.phase()["deadline_at"] == iso(deadline)
        assert self.phase()["scene"]["type"] == "camp"
        assert server.scheduler.pending() == {server.game_id: deadline}
        for key, _name in CAST:
            assert self.told(key)[-1] == camp_text(CAMP_SECONDS)
        hp, hp_max = await self.hp("alice")
        assert hp < hp_max  # здоровье Алисы до длинного отдыха
        self.day_before_camp = (await server.state()).time.day
        body = {"text": "Ход", "secret": False, "inspiration": False}
        assert (await server.game_call("alice", "PUT", "/action", json=body)).status_code == 409
        assert (await server.game_call("alice", "POST", "/host/collect")).status_code == 409

    async def at_camp_lines(self) -> None:
        for key, _name in CAST:  # реплики видят все, по порядку, с автором
            lines = [i for i in self.feed(key) if i["kind"] == "camp_line"]
            assert [(i["data"]["actor"], i["text"]) for i in lines] == [
                (name, FIRE_LINES[k]) for k, name in CAST
            ]
            assert not any(item["private"] for item in lines)
        assert self.calls.turns == 8  # реплики мастера не зовут

    async def at_camp_resumed(self) -> None:
        server = self.server
        assert self.calls.turns == 8  # ни пауза, ни продление мастера не зовут
        deadline = server.scheduler.now() + timedelta(seconds=CAMP_SECONDS + 60)
        assert self.phase()["deadline_at"] == iso(deadline)
        assert server.scheduler.pending() == {server.game_id: deadline}

    async def at_morning(self) -> None:
        server = self.server
        hp, hp_max = await self.hp("alice")
        assert hp == hp_max  # длинный отдых вылечил Алису
        state = await server.state()
        assert state.time.day == self.day_before_camp + 1
        assert (state.scene.type, state.combat) == (SceneType.EXPLORATION, None)
        assert self.phase()["turn_phase"] == "discussion" and server.scheduler.pending() == {}
        for key, _name in CAST:
            lines = self.texts(key)
            rest = lines.index("Длинный отдых: отряд восстановил силы.")
            assert lines[rest + 1] == "Утро. Туман отступил, и отряд снова полон сил."
            assert self.told(key)[-1] == game_continues_text()
        # Двадцать записей летописи сжаты в сводку фоновым вызовом после утра.
        assert (state.chronicle.summary, state.chronicle.entries) == (SUMMARY, [])
        assert state.version == 11 and (await server.game()).state_version == 11
        assert self.calls.turns == 9 and self.calls.json == ["weaving", "chronicle"]


async def test_a_whole_evening_goes_from_the_start_button_to_the_morning_after_the_camp(
    serve, evening_dice, model_calls
):
    async with serve(rng=evening_dice) as server:
        await play_evening(server, Story(server, model_calls))


async def test_the_journal_of_the_master_and_the_ledger_of_the_game_are_complete_and_consistent(
    serve, evening_dice
):
    async with serve(rng=evening_dice) as server:
        await play_evening(server)

        turns, logs = await server.turns(), await server.logs()
        assert [(t.number, t.kind.value, t.status.value) for t in turns] == [
            (1, "opening", "done"),
            (2, "round", "done"),
            (3, "round", "done"),
            (4, "combat", "done"),
            (5, "combat", "done"),
            (6, "combat", "done"),
            (7, "combat", "done"),
            (8, "round", "done"),
            (9, "camp_end", "done"),
            (10, "round", "open"),
        ]
        # Версии состояния идут подряд: вплетение, открытие, девять ходов... и сжатая летопись.
        assert [row.version for row in await server.snapshots()] == list(range(1, 12))
        done = [t for t in turns if t.status == TurnStatus.DONE]
        assert [(t.state_version_before, t.state_version_after) for t in done] == [
            (1, 2),
            (2, 3),
            (3, 4),
            (4, 5),
            (5, 6),
            (6, 7),
            (7, 8),
            (8, 9),
            (9, 10),
        ]
        assert turns[-1].state_version_before == 11  # открытый ход следует за сжатой летописью
        # По записи журнала на ход, без ошибок; вызовы инструментов — ровно те, что назвал сценарий.
        assert [(log.turn_id, log.attempt, log.error) for log in logs] == [
            (turn.id, 1, None) for turn in done
        ]
        assert [[call["name"] for call in log.trace] for log in logs] == [
            ["chronicle"],
            ["request_check", "request_check", "damage", "private_message", "chronicle"],
            ["move_party", "start_combat", "chronicle"],
            ["combat_action", "chronicle", "server_end_turn"],
            ["combat_action", "chronicle", "server_end_turn"],
            ["combat_action", "apply_effect", "chronicle", "server_end_turn"],
            ["apply_effect", "end_scene", "chronicle"],
            ["start_camp", "chronicle"],
            ["server_end_camp", *["chronicle"] * 12],
        ]
        assert not [call for log in logs for call in log.trace if call["is_error"]]
        hero_ids = [hero.id for hero in (await server.state()).party.values()]
        assert [log.request["acting"] for log in logs[3:7]] == [
            [hero_ids[0]],
            [hero_ids[1]],
            [hero_ids[2], "skeleton-1"],
            [hero_ids[0]],
        ]
        assert all(log.provider == "fake" and log.usage is not None for log in logs)
        # Ключи сервера в журнал не попадают: модели они не нужны, и в контекст хода не идут.
        dumped = json.dumps(
            [[log.request, log.trace, log.output, log.usage, log.error] for log in logs],
            ensure_ascii=False,
        )
        assert not [secret for secret in SERVER_SECRETS if secret in dumped]
        # Рассказ мастера идёт последней записью своего хода.
        narration_turns = {
            message.turn_id: message
            for message in await server.messages(MessageKind.NARRATION)
            if message.turn_id is not None
        }
        for turn in done:
            tail = [m for m in await server.messages() if m.turn_id == turn.id]
            outputs = [m for m in tail if m.kind != MessageKind.CAMP_LINE]
            assert outputs[-1].id == narration_turns[turn.id].id


async def test_a_game_that_runs_the_whole_evening_writes_no_warnings_and_no_secrets_to_the_log(
    serve, evening_dice, caplog
):
    async with serve(rng=evening_dice) as server:
        await play_evening(server)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    for secret in (*SERVER_SECRETS, *itertools.chain.from_iterable(OWN_SECRETS.values())):
        assert secret not in caplog.text, secret


# --- приватность на всех фазах ---

PARTY_KEYS = {"id", "name", "player_id", "hp", "hp_max", "temp_hp", "conditions", "effects"}
MONSTER_KEYS = {"id", "name", "kind", "side", "zone_id", "health", "conditions"}


async def test_every_private_thing_of_the_evening_reaches_only_its_owner_on_every_phase(
    serve, evening_dice
):
    async with serve(rng=evening_dice) as server:
        await play_evening(server)
        frames = {key: server.watchers[key].frames for key in server.watchers}
        everything = {key: json.dumps(f, ensure_ascii=False) for key, f in frames.items()}
        # Новые снимки после вечера: то же, что видит переподключившийся клиент.
        for key in frames:
            everything[key + "+снимок"] = json.dumps(
                await fresh_snapshot(server, key), ensure_ascii=False
            )

    for who, text in everything.items():
        owner = who.removesuffix("+снимок")
        # Свои тайны игрок видит, чужих — нет.
        for key, markers in OWN_SECRETS.items():
            for marker in markers:
                assert (marker in text) == (key == owner), f"{marker!r} в кадрах {who}"
        # Тайн мастера, ключей сервера и служебных данных нет ни у кого.
        for hidden in (*HIDDEN_FROM_PLAYERS, HIDDEN_EXIT_ID, *SERVER_SECRETS):
            assert hidden not in text, f"в кадрах {who}: {hidden!r}"
    # Лист персонажа приходит только владельцу, отряд и бой — только белыми списками полей.
    for key, watcher in server.watchers.items():
        sheets = [f["character"]["name"] for f in watcher.frames if f["type"] == "character"]
        assert set(sheets) == {dict(CAST)[key]}
        parties = [f["party"] for f in watcher.frames if f["type"] == "party"]
        assert parties and all({set(m) == PARTY_KEYS for p in parties for m in p})
        for block in (f["combat"] for f in watcher.frames if f["type"] == "combat"):
            for fighter in (block or {}).get("order", []):
                if fighter["kind"] == "monster":
                    assert set(fighter) == MONSTER_KEYS  # у монстров слова, а не числа здоровья
        for status in (f for f in watcher.frames if f["type"] == "player_status"):
            assert set(status) == {"type", "user_id", "status"}
            assert status["status"] in {"writing", "ready", "pass"}  # тайный ход статусом не виден
