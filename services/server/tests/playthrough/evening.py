"""Общая часть сквозных тестов мастера (B7.coverage): настоящий сервер, вечер игры, клиент.

Тесты пунктов B7.1–B7.8 держат каждую фазу по отдельности на подготовленных строках базы. Здесь вся
игра идёт одним потоком, как у шестерых друзей за столом: лобби и герои, «Начать», вплетение,
открытие, обсуждение и сбор ходов с отсчётом, разбор, бой с передачей хода движком и «Поторопить»,
привал с репликами и продлением, длинный отдых, летопись. Сервер настоящий (приложение под uvicorn
на loopback с полным `lifespan`, временная SQLite после миграций), клиенты настоящие (`httpx` и
сокеты `websockets`, как Mini App). Подменены только те части, которые обязаны быть подменены:
модель (`FakeProvider` по сценарию из YAML), кубы (`app.state.rng`, как в тестах движка), часы
отсчётов (`ManualScheduler`: тест сам решает, сколько секунд прошло) и бот. Живых моделей, Telegram
и сети нет.

Модуль не содержит тестов: он собирает кирпичики (сервер, игроки, этапы вечера, отпечатки базы,
сборку клиента из событий), из которых тесты `test_*.py` этой папки складывают свои сценарии.
Фикстуры лежат в `conftest.py`.
"""

import asyncio
import contextlib
import copy
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from adventure_secrets import hidden_texts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.rng import SeededRng
from dnd_engine.state import GameState
from dnd_server.app import create_app
from dnd_server.config import Settings
from dnd_server.db.models import (
    DmTurnLog,
    Game,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    Turn,
    TurnStatus,
)
from dnd_server.turns.timers import ManualScheduler

# tests/flows/test_master_playthrough.py -> flows -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SESSION_SECRET = "playthrough-session-secret-0123456789-abcdefghij"
BOT_TOKEN = "987654321:AAF-playthrough-bot-token-0123456789_abcdef"
TIMEOUT_S = 10
PONG = {"type": "pong"}
COLLECT_SECONDS = 120
HURRY_SECONDS = 60
CAMP_SECONDS = 300

# Три героя: ключ, имя (то же имя в сценарии, чтобы рассказ называл героя), порядок входа в игру.
CAST = (("alice", "Алиса"), ("boris", "Борис"), ("vera", "Вера"))

# Тексты-«ловушки»: каждый должен дойти только до своего адресата. Метки нарочно необычные, чтобы
# совпадение по случайности было невозможно.
BORIS_SECRET_ACTION = "Незаметно срезать кошелёк у стражника ПОЛНОЧЬ-КОШЕЛЁК"
BORIS_SECRET_COMBAT = "Прижаться к надгробию и затаиться ПОЛНОЧЬ-ЗАСАДА"
PRIVATE_TO_BORIS = "ЛИЧНОЕ-БОРИСУ: в кошельке письмо с печатью гильдии."
GOAL = {
    "alice": "ЦЕЛЬ-АЛИСЫ: найти пропавшего смотрителя и вернуть его сестре",
    "boris": "ЦЕЛЬ-БОРИСА: вернуть долг до рассвета",
    "vera": "ЦЕЛЬ-ВЕРЫ: узнать, кто шепчет на кладбище",
}
HOOK_SECRET = {
    "alice": "ТАЙНА-АЛИСЫ: когда-то вы взяли деньги у гильдии воров",
    "boris": "ТАЙНА-БОРИСА: вы подделали расписку Фабиана",
    "vera": "ТАЙНА-ВЕРЫ: вы слышите шёпот мёртвых и молчите об этом",
}
BACKSTORY = {
    "alice": "ПРЕДЫСТОРИЯ-АЛИСЫ: двадцать лет на заставе у перевала.",
    "boris": "ПРЕДЫСТОРИЯ-БОРИСА: вырос в порту, где каждый второй чей-то должник.",
    "vera": "ПРЕДЫСТОРИЯ-ВЕРЫ: послушница, ушедшая из монастыря ночью.",
}
PUBLIC_ACTION = "Осмотреть доску объявлений у входа в трактир"
FIRE_LINES = {
    "alice": "Подкину дров и расскажу, как мы выросли у воды.",
    "boris": "Буду точить кинжал и молчать, пока кто-нибудь не заговорит.",
    "vera": "Тихо помолюсь за смотрителя Ульриха.",
}
SUMMARY = "Отряд собрался в Мшистом Броде, отбился от скелета на кладбище и переночевал у костра."

# Что принадлежит игроку: его зацепка, лист, тайные ходы, броски и личные сообщения. Метка есть
# только в кадрах хозяина и ни у кого больше.
OWN_SECRETS = {
    "alice": [GOAL["alice"], HOOK_SECRET["alice"], BACKSTORY["alice"]],
    "boris": [
        GOAL["boris"],
        HOOK_SECRET["boris"],
        BACKSTORY["boris"],
        BORIS_SECRET_ACTION,
        BORIS_SECRET_COMBAT,
        PRIVATE_TO_BORIS,
        # Метки — подписи карточек тайных бросков целиком: названия навыков («Ловкость рук»,
        # «Скрытность») есть в листе каждого игрока (`derived.skills`) и меткой быть не могут.
        "Ловкость (Ловкость рук)",  # тайный бросок проверки
        "Борис: Скрытность — ",  # тайный бросок в бою
    ],
    "vera": [GOAL["vera"], HOOK_SECRET["vera"], BACKSTORY["vera"]],
}

# Эти слова сервер не должен показать никому: ключи, адрес базы и тому подобное.
SERVER_SECRETS = (SESSION_SECRET, BOT_TOKEN, BOT_TOKEN.partition(":")[2])

# Ключи служебных данных, которые игроки видеть не должны (тайные тексты приключения собирает
# `hidden_texts`).
HIDDEN_KEYS = ("adventure_data", "dm_turn_logs", "issues", "pc_hooks")
HIDDEN_EXIT_ID = "to-crypt"  # скрытый выход демо-приключения: игрокам он открывается находкой


def with_sentences(text: str) -> list[str]:
    """Текст целиком и каждое его предложение длиннее двадцати знаков (утечка бывает частью)."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text)]
    return [text, *(sentence for sentence in sentences if len(sentence) >= 20)]


HIDDEN_FROM_PLAYERS = (
    *(
        fragment
        for text in hidden_texts(load_adventure(DEMO_ADVENTURE_DIR).model_dump(mode="json"))
        for fragment in with_sentences(text)
    ),
    *HIDDEN_KEYS,
)


# --- кубы ---


class ScriptedThenSeeded:
    """Первые броски заданы тестом (точная инициатива и исходы), остальные — `SeededRng`.

    Так бой начинается в известном порядке, а случайные броски, которых тест не предвидел
    (спасброски от смерти и подобное), всё равно воспроизводимы. `checkpoint`/`restore` возвращают
    кубы к моменту до неудачной попытки: «Повторить» бросает те же значения, что и она."""

    def __init__(self, values: list[int], seed: int = 2024) -> None:
        self._values = deque(values)
        self._rest = SeededRng(seed)

    def randint(self, a: int, b: int) -> int:
        if self._values:
            value = self._values.popleft()
            assert a <= value <= b, f"заданный бросок {value} вне диапазона [{a}, {b}]"
            return value
        return self._rest.randint(a, b)

    def checkpoint(self) -> tuple[deque[int], SeededRng]:
        return deque(self._values), copy.deepcopy(self._rest)

    def restore(self, checkpoint: tuple[deque[int], SeededRng]) -> None:
        values, rest = checkpoint
        self._values, self._rest = deque(values), copy.deepcopy(rest)


# --- сценарий мастера ---


def tool(name: str, **args: Any) -> dict[str, Any]:
    return {"tool": name, "args": args}


def say(text: str) -> dict[str, Any]:
    return {"text": text}


def chronicle(entry: str) -> dict[str, Any]:
    return tool("chronicle", entry=entry)


def evening_scenario(repeat_turn: int | None = None) -> dict[str, Any]:
    """Весь вечер мастера по порядку вызовов; id героев подставляет `FakeProvider` (`$pc1…$pc3`).

    Порядок кубов, который ждёт сценарий (остальные берёт `SeededRng`): проверка Бориса, проверка
    Алисы, урон Алисе, инициатива Алисы, Бориса, Веры, скелета, проверка «Спрятаться» Бориса.
    `repeat_turn` — номер хода (с нуля), который в очереди стоит дважды: так «Повторить» после
    сорвавшегося хода получает тот же ход сценария, а не следующий."""
    hooks = [
        {
            "character_id": f"$pc{number}",
            "npc_links": [{"npc_id": "marta", "relation": "старая знакомая"}],
            "location_links": [{"location_id": "village", "note": "здесь вы выросли"}],
            "personal_goal": GOAL[key],
            "secret": HOOK_SECRET[key],
        }
        for number, (key, _name) in enumerate(CAST, start=1)
    ]
    # Двадцать записей летописи к концу привала: хватит, чтобы сжатие сработало после утра.
    morning_notes = [chronicle(f"Утро после привала, запись {n}.") for n in range(1, 13)]
    scenario = {
        "turns": [
            # 1. Открытие игры.
            {
                "steps": [
                    chronicle("Отряд собрался в Мшистом Броде."),
                    say("Вечер в Мшистом Броде: в окнах трактира дрожит свет."),
                ]
            },
            # 2. Первый раунд: тайный бросок Бориса, общий бросок Алисы, урон и личное сообщение.
            {
                "steps": [
                    tool(
                        "request_check",
                        character_id="$pc2",
                        ability="dex",
                        skill="sleight_of_hand",
                        dc=15,
                        reason="ловкость рук у стражника",
                    ),
                    tool(
                        "request_check",
                        character_id="$pc1",
                        ability="int",
                        skill="investigation",
                        dc=10,
                        reason="объявления на доске",
                    ),
                    tool(
                        "damage",
                        character_id="$pc1",
                        dice="1d4",
                        damage_type="bludgeoning",
                        reason="оступилась на крыльце",
                    ),
                    tool("private_message", character_id="$pc2", text=PRIVATE_TO_BORIS),
                    chronicle("Алиса прочла объявления, Борис проверил карманы стражника."),
                    say("Алиса читает объявления, а у крыльца кто-то вздыхает."),
                ]
            },
            # 3. Второй раунд: отряд идёт на кладбище, начинается бой со скелетом.
            {
                "steps": [
                    tool("move_party", location_id="cemetery", via_exit="to-cemetery"),
                    tool(
                        "start_combat",
                        monsters=[{"monster_id": "skeleton", "count": 1, "zone": "gate"}],
                    ),
                    chronicle("У ворот кладбища поднялся скелет."),
                    say("Из тумана у ворот выходит скелет с ржавым мечом."),
                ]
            },
            # 4. Бой: ход Алисы.
            {
                "steps": [
                    tool("combat_action", actor_id="$pc1", action="dodge"),
                    chronicle("Алиса уклонилась от скелета."),
                    say("Алиса пригибается, и меч свистит над её головой."),
                ]
            },
            # 5. Бой: тайный ход Бориса («Спрятаться»), бросок виден только ему.
            {
                "steps": [
                    tool("combat_action", actor_id="$pc2", action="hide"),
                    chronicle("Борис затаился за надгробием."),
                    say("Борис отступает в тень надгробия."),
                ]
            },
            # 6. Бой: ход Веры «Уклоняется» (после «Поторопить»); скелет прячется и теряет ход.
            {
                "steps": [
                    tool("combat_action", actor_id="$pc3", action="dodge"),
                    tool("apply_effect", target_id="skeleton-1", condition="invisible", add=True),
                    chronicle("Вера прикрылась щитом, скелет исчез в тумане."),
                    say("Вера поднимает щит, а туман вокруг скелета густеет."),
                ]
            },
            # 7. Бой: ход Алисы и конец сцены.
            {
                "steps": [
                    tool("apply_effect", target_id="skeleton-1", condition="invisible", add=False),
                    tool("end_scene", next="exploration", reason="Скелет рассыпался."),
                    chronicle("Скелет рассыпался, бой окончен."),
                    say("Скелет разваливается на груду костей, и над кладбищем снова тихо."),
                ]
            },
            # 8. Третий раунд: отряд решает отдохнуть, мастер начинает привал.
            {
                "steps": [
                    tool("start_camp", reason="отряд разжигает костёр у сторожки"),
                    chronicle("Отряд разбил лагерь у сторожки."),
                    say("Над сторожкой поднимается дымок, отряд устраивается у огня."),
                ]
            },
            # 9. Конец привала: утро. Длинный отдых уже провёл сервер.
            {
                "steps": [*morning_notes, say("Утро. Туман отступил, и отряд снова полон сил.")],
            },
            # Дальше шаблон раунда: он нужен тестам, которые продолжают игру после вечера.
            {
                "repeat": True,
                "steps": [
                    chronicle("Отряд идёт дальше."),
                    say("Отряд идёт дальше, и впереди что-то шевелится."),
                ],
            },
        ],
        "json": {
            "weaving": {"hooks": hooks},
            "chronicle": {"summary": SUMMARY},
            # Идеи для рассказа у костра (блок B9): привал мастера после хода 8 просит их фоновым
            # вызовом; без ответа сценария вечер писал бы в журнал предупреждение о сбое идей.
            "campfire.ideas": {
                "ideas": [
                    {"character_id": f"$pc{number}", "text": f"Расскажи о себе у костра, {name}."}
                    for number, (_key, name) in enumerate(CAST, start=1)
                ]
            },
        },
    }
    if repeat_turn is not None:
        scenario["turns"].insert(repeat_turn + 1, copy.deepcopy(scenario["turns"][repeat_turn]))
    return scenario


# --- настоящий сервер на loopback ---


class RecordingNotifier:
    """Бот вместо Telegram: помнит, кому и что отправили."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def notify(self, user_id: str, text: str) -> None:
        self.sent.append((user_id, text))

    def texts_for(self, user_id: str) -> list[str]:
        return [text for who, text in self.sent if who == user_id]


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    user_id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class Watcher:
    """Сокет игрока: читает кадры в фоне и копит их. Первый кадр — снимок.

    Ждать молчания нельзя, поэтому «всё, что сервер успел отправить» ловится замком: ping идёт через
    ту же очередь соединения, что и события, и `pong` замыкает всё, что отправлено до него."""

    def __init__(self, person: Person, ws: ClientConnection) -> None:
        self.person = person
        self.ws = ws
        self.frames: list[dict[str, Any]] = []
        self._pong = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with contextlib.suppress(Exception):  # сокет закрыт сервером или тестом
            async for raw in self.ws:
                message = json.loads(raw)
                if message == PONG:
                    self._pong.set()
                else:
                    self.frames.append(message)

    async def sync(self) -> None:
        """Ждёт, пока придёт всё, что сервер отправил к этому моменту."""
        self._pong.clear()
        await self.ws.send(json.dumps({"type": "ping"}))
        await asyncio.wait_for(self._pong.wait(), TIMEOUT_S)

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self.ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    @property
    def snapshot(self) -> dict[str, Any]:
        assert self.frames and self.frames[0]["type"] == "snapshot", self.frames[:1]
        return self.frames[0]

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        rest = self.frames[1:]
        return rest if event_type is None else [f for f in rest if f["type"] == event_type]


@dataclass
class Server:
    """Запущенное приложение: клиент REST, часы отсчётов, бот-регистратор и сокеты игроков."""

    app: Any
    port: int
    http: httpx.AsyncClient
    scheduler: ManualScheduler
    notifier: RecordingNotifier
    db: async_sessionmaker[AsyncSession]
    people: dict[str, Person]
    game_id: str = ""
    watchers: dict[str, Watcher] = field(default_factory=dict)

    # --- запросы ---

    async def call(
        self, who: str | Person | None, method: str, path: str, **options: Any
    ) -> httpx.Response:
        person = self.people[who] if isinstance(who, str) else who
        headers = {} if person is None else person.headers
        return await self.http.request(method, path, headers=headers, **options)

    async def game_call(self, who: str, method: str, path: str, **options: Any) -> httpx.Response:
        return await self.call(who, method, f"/api/games/{self.game_id}{path}", **options)

    async def ok(self, who: str, method: str, path: str, **options: Any) -> httpx.Response:
        """Запрос игры, который обязан удаться."""
        response = await self.game_call(who, method, path, **options)
        assert response.status_code == 200, (
            f"{method} {path}: {response.status_code} {response.text}"
        )
        return response

    async def host(self, name: str, who: str = "alice") -> httpx.Response:
        return await self.ok(who, "POST", f"/host/{name}")

    async def draft(
        self, who: str, text: str, *, secret: bool = False, inspiration: bool = False
    ) -> httpx.Response:
        body = {"text": text, "secret": secret, "inspiration": inspiration}
        return await self.ok(who, "PUT", "/action", json=body)

    async def submit(self, who: str, text: str, **flags: bool) -> httpx.Response:
        await self.draft(who, text, **flags)
        return await self.ok(who, "POST", "/action/submit")

    async def settle(self) -> None:
        """Ждёт, пока кончатся разборы мастера и фоновые задачи, и пока сокеты получат всё."""
        await asyncio.wait_for(self.app.state.turn_tasks.join(), TIMEOUT_S)
        await self.sync()

    async def sync(self) -> None:
        for watcher in self.watchers.values():
            await watcher.sync()

    # --- сокеты ---

    def ws_url(self, token: str) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/games/{self.game_id}?token={token}"

    async def watch(self, who: str) -> Watcher:
        person = self.people[who]
        ws = await connect(self.ws_url(person.token))
        watcher = Watcher(person, ws)
        self.watchers[who] = watcher
        await asyncio.wait_for(self._first_frame(watcher), TIMEOUT_S)
        return watcher

    @staticmethod
    async def _first_frame(watcher: Watcher) -> None:
        while not watcher.frames:
            await asyncio.sleep(0.005)

    async def watch_everyone(self) -> None:
        for key in self.people:
            await self.watch(key)

    async def close_watchers(self) -> None:
        for watcher in self.watchers.values():
            await watcher.close()
        self.watchers.clear()

    # --- что лежит в базе ---

    async def game(self) -> Game:
        async with self.db() as session:
            return await session.get(Game, self.game_id)

    async def turns(self) -> list[Turn]:
        async with self.db() as session:
            rows = await session.scalars(
                select(Turn).where(Turn.game_id == self.game_id).order_by(Turn.number, Turn.id)
            )
            return list(rows)

    async def messages(self, *kinds: MessageKind) -> list[Message]:
        async with self.db() as session:
            query = select(Message).where(Message.game_id == self.game_id).order_by(Message.id)
            if kinds:
                query = query.where(Message.kind.in_(kinds))
            return list(await session.scalars(query))

    async def logs(self) -> list[DmTurnLog]:
        async with self.db() as session:
            rows = await session.scalars(
                select(DmTurnLog).where(DmTurnLog.game_id == self.game_id).order_by(DmTurnLog.id)
            )
            return list(rows)

    async def snapshots(self) -> list[GameStateSnapshot]:
        async with self.db() as session:
            rows = await session.scalars(
                select(GameStateSnapshot)
                .where(GameStateSnapshot.game_id == self.game_id)
                .order_by(GameStateSnapshot.version)
            )
            return list(rows)

    async def actions(self, turn_number: int) -> dict[str, PlayerAction]:
        """Ходы игроков хода с этим номером по ключу игрока (`alice`, `boris`, `vera`)."""
        key_of = {person.user_id: key for key, person in self.people.items()}
        async with self.db() as session:
            turn = await session.scalar(
                select(Turn).where(
                    Turn.game_id == self.game_id,
                    Turn.number == turn_number,
                    Turn.status != TurnStatus.SUPERSEDED,
                )
            )
            rows = await session.scalars(
                select(PlayerAction).where(PlayerAction.turn_id == turn.id)
            )
            return {key_of[row.user_id]: row for row in rows}

    async def state(self) -> GameState:
        game = await self.game()
        async with self.db() as session:
            row = await session.get(GameStateSnapshot, (self.game_id, game.state_version))
            return row.to_state()

    def hero_id(self, key: str) -> str:
        """Id героя игрока: порядок героев в игре совпадает с порядком `CAST`."""
        return {k: f"$pc{n}" for n, (k, _name) in enumerate(CAST, start=1)}[key]


@asynccontextmanager
async def running_server(
    settings: Settings,
    db: async_sessionmaker[AsyncSession],
    *,
    rng: Any = None,
    people: dict[str, Person] | None = None,
    game_id: str = "",
    start: datetime | None = None,
) -> AsyncIterator[Server]:
    """Приложение под uvicorn на свободном порту с полным `lifespan` (в том числе восстановление
    игр после перезапуска). Повторный вход на той же базе — это и есть «перезапуск сервера»."""
    app = create_app(settings)
    scheduler = ManualScheduler(start or datetime.now(UTC))
    app.state.scheduler = scheduler
    app.state.rng = rng if rng is not None else SeededRng(2024)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(TIMEOUT_S):
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
    assert server.started, "сервер не стартовал"
    port = server.servers[0].sockets[0].getsockname()[1]
    notifier = RecordingNotifier()
    app.state.notifier = notifier  # бот без токена в тестах ничего не шлёт: ставим регистратор
    http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False)
    running = Server(app, port, http, scheduler, notifier, db, dict(people or {}), game_id)
    try:
        yield running
    finally:
        await running.close_watchers()
        await http.aclose()
        server.should_exit = True
        await serving


# --- лобби: игроки, герои, «Начать» ---

# Легальный воин-дварф (покупка очков 27 из 27); меняется только имя: у троих разные имена.
DETAILED_CHOICES: dict[str, Any] = {
    "species": "dwarf",
    "class_id": "fighter",
    "background": "soldier",
    "method": "point_buy",
    "base_scores": {"str": 15, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
    "background_bonus": {"str": 2, "con": 1},
    "skills": ["acrobatics", "history"],
    "fighting_style": "defense",
    "persona": {"concept": "Ветеран пограничной стражи"},
}
# Кубы вечера по порядку: проверка Бориса, проверка Алисы, урон Алисе, инициатива (Алиса, Борис,
# Вера, скелет), проверка «Спрятаться» Бориса. Инициатива строит очередь: Алиса, Борис, Вера,
# скелет.
EVENING_DICE = [14, 16, 3, 20, 15, 12, 1, 17]


def hero_choices(key: str, name: str) -> dict[str, Any]:
    """Имя героя и его предыстория: у каждого своя, чтобы утечка листа персонажа была заметна."""
    persona = {
        **DETAILED_CHOICES["persona"],
        "backstory": BACKSTORY.get(key, f"ПРЕДЫСТОРИЯ-{name}"),
    }
    return {"name": name, "persona": persona}


async def gather_in_lobby(
    server: Server, cast: Sequence[tuple[str, str]] = CAST, **durations: int
) -> None:
    """Игроки (по умолчанию трое) входят, хост создаёт игру `demo-crypt`, все делают героев и
    отмечаются готовыми — настоящими маршрутами. Первый в `cast` — хост."""
    host, *others = (key for key, _name in cast)
    for number, (key, name) in enumerate(cast, start=1):
        response = await server.call(None, "POST", "/api/auth/dev", json={"login": f"dev-{number}"})
        assert response.status_code == 200, response.text
        body = response.json()
        server.people[key] = Person(key, name, body["user"]["id"], body["token"])
    created = await server.call(
        host,
        "POST",
        "/api/games",
        json={
            "adventure_id": "demo-crypt",
            "collect_seconds": COLLECT_SECONDS,
            "hurry_seconds": HURRY_SECONDS,
            "camp_seconds": CAMP_SECONDS,
            **durations,
        },
    )
    assert created.status_code == 201, created.text
    server.game_id = created.json()["id"]
    code = created.json()["code"]
    for key in others:
        joined = await server.call(key, "POST", "/api/games/join", json={"code": code})
        assert joined.status_code == 200, joined.text
    for key, name in cast:
        hero = await server.game_call(
            key,
            "POST",
            "/characters",
            json={"mode": "detailed", "choices": {**DETAILED_CHOICES, **hero_choices(key, name)}},
        )
        assert hero.status_code == 201, hero.text
        ready = await server.game_call(key, "POST", "/ready", json={"ready": True})
        assert ready.status_code == 200, ready.text


async def begin_evening(
    server: Server, cast: Sequence[tuple[str, str]] = CAST, **durations: int
) -> None:
    """Лобби, сокеты всех игроков, «Начать»: вплетение и открытие игры доиграны до обсуждения."""
    await gather_in_lobby(server, cast, **durations)
    await server.watch_everyone()
    started = await server.game_call(cast[0][0], "POST", "/start")
    assert started.status_code == 200, started.text
    assert started.json()["phase"] == "weaving"
    await server.settle()


# --- клиент: состояние из снимка и событий ---


def client_view(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Что знает клиент, который принял снимок и все события сокета: блоки заменяются целиком,
    записи ленты добавляются (повтор по id отсекается), надгробия снимают запись."""
    snapshot, events = frames[0], frames[1:]
    assert snapshot["type"] == "snapshot", snapshot["type"]
    view = {
        key: copy.deepcopy(snapshot[key])
        for key in ("phase", "statuses", "character", "party", "combat")
    }
    feed = {item["id"]: item for item in snapshot["feed"]}
    for event in events:
        match event["type"]:
            case "phase":
                view["phase"] = event["phase"]
            case "player_status":
                view["statuses"][event["user_id"]] = event["status"]
            case "feed_item":
                item = event["item"]
                if item.get("superseded"):
                    feed.pop(item["id"], None)
                else:
                    feed.setdefault(item["id"], item)
            case "party" | "combat" | "character":
                view[event["type"]] = event[event["type"]]
    view["feed"] = [feed[item_id] for item_id in sorted(feed)]
    return view


async def fresh_snapshot(server: Server, who: str) -> dict[str, Any]:
    """Новый снимок игрока на новом сокете: то, что увидит переподключившийся клиент."""
    async with connect(server.ws_url(server.people[who].token)) as ws:
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
    assert snapshot["type"] == "snapshot"
    return snapshot


async def assert_clients_agree_with_snapshots(
    server: Server, where: str, *, skip: tuple[str, ...] = ()
) -> None:
    """У каждого игрока клиент, собранный из событий, совпадает с новым снимком.

    Игрок без записи в статусах — `idle` (ещё ничего не написал): так читает его и сам снимок, у
    которого статусов нет, пока нет открытого хода (между вплетением и открытием игры)."""
    await server.sync()
    everyone = [person.user_id for person in server.people.values()]
    for key in server.watchers:
        known = client_view(server.watchers[key].frames)
        fresh = await fresh_snapshot(server, key)
        for block in known:
            if block in skip:
                continue
            if block == "statuses":
                known[block] = {u: known[block].get(u, "idle") for u in everyone}
                fresh[block] = {u: fresh[block].get(u, "idle") for u in everyone}
            assert known[block] == fresh[block], f"{where}: у {key} расходится блок {block}"


Check = Callable[[str], Awaitable[None]]


async def no_check(_where: str) -> None:
    """Контрольная точка, которая ничего не проверяет (этапы без разбора по кадрам)."""


def view_of(server: Server, who: str) -> dict[str, Any]:
    return client_view(server.watchers[who].frames)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# --- этапы вечера ---
#
# Каждый этап — то, что делают люди за столом: пишут ходы, жмут кнопки хоста, двигают время.
# Между действиями этапы зовут `check`: в сквозном тесте там сверяются клиенты со снимками.


async def round_one(server: Server, check: Check = no_check) -> None:
    """Первый раунд: общий черновик, тайный сданный ход, молчащая Вера; сбор по отсчёту с паузой."""
    assert server.scheduler.pending() == {}  # до «Собрать ходы» таймера нет
    await server.draft("alice", PUBLIC_ACTION)  # черновик сдастся сам, когда выйдет время
    await server.submit("boris", BORIS_SECRET_ACTION, secret=True)
    await check("обсуждение: черновик и тайный ход")
    await server.host("collect")
    await server.settle()
    await check("сбор ходов идёт")
    await server.host("pause")
    await server.settle()
    await check("сбор на паузе")
    await server.scheduler.advance(1000)  # пока игра стоит, ничего не срабатывает
    await server.host("resume")
    await server.settle()
    await check("сбор после паузы")
    await server.scheduler.advance(COLLECT_SECONDS)
    await server.settle()
    await check("первый раунд разобран")


async def round_two(server: Server, check: Check = no_check) -> None:
    """Второй раунд: все готовы, «Собрать ходы» отдаёт ход мастеру сразу; начинается бой."""
    await server.submit("alice", "Идти на кладбище по тропе за деревней")
    await server.ok("boris", "POST", "/action/pass")
    await server.ok("vera", "POST", "/action/pass")
    await server.host("collect")
    await server.settle()
    await check("бой начался")


async def battle(server: Server, check: Check = no_check) -> None:
    """Бой: Алиса, тайный ход Бориса, Вера после «Поторопить», снова Алиса и конец сцены."""
    await server.submit("alice", "Уклоняюсь от меча скелета")
    await server.settle()
    await check("ход Алисы разобран")
    await server.submit("boris", BORIS_SECRET_COMBAT, secret=True)
    await server.settle()
    await check("тайный ход Бориса разобран")
    await server.host("hurry")
    await server.settle()
    await check("Веру торопят")
    await server.scheduler.advance(HURRY_SECONDS)
    await server.settle()
    await check("время Веры вышло")
    await server.submit("alice", "Добиваю скелета")
    await server.settle()
    await check("бой окончен")


async def camp(server: Server, check: Check = no_check) -> None:
    """Привал: сцену начинает мастер, герои говорят у костра, хост продлевает, отсчёт кончается."""
    await server.submit("alice", "Разжечь костёр у сторожки и отдохнуть до утра")
    await server.ok("boris", "POST", "/action/pass")
    await server.ok("vera", "POST", "/action/pass")
    await server.host("collect")
    await server.settle()
    await check("привал начался")
    for key, line in FIRE_LINES.items():
        await server.ok(key, "POST", "/camp/line", json={"text": line})
    await check("реплики у костра")
    await server.host("camp-extend")
    await server.host("pause")
    await server.scheduler.advance(1000)
    await server.host("resume")
    await server.settle()
    await check("привал продлён и после паузы")
    await server.scheduler.advance(CAMP_SECONDS)
    await server.settle()
    await server.scheduler.advance(60)  # продление кончилось: привал закрывается
    await server.settle()
    await check("утро после привала")


async def play_evening(server: Server, check: Check = no_check) -> None:
    await begin_evening(server)
    await check("игра началась")
    await round_one(server, check)
    await round_two(server, check)
    await battle(server, check)
    await camp(server, check)


@dataclass
class ModelCalls:
    """Счётчик обращений к модели: ходы мастера и JSON-ответы (вплетение, сжатие летописи)."""

    turns: int = 0
    json: list[str] = field(default_factory=list)  # purpose каждого JSON-запроса


class StateWriteFault:
    """Сбой диска при записи нового состояния: первый коммит, в котором есть снимок игры новее
    `newer_than`, не проходит. Резерв хода, статусы и прочие коммиты идут как обычно, поэтому падает
    именно запись результата хода или вплетения, уже после ответа модели, а откат обязан убрать всё,
    что к этому моменту попало в сессию (снимок, сообщения, статусы). С `also_the_record` не
    проходит и следующий коммит, где пишется запись о неудаче (журнал хода или сообщение хосту):
    диск всё ещё полон. С `also_the_next` не проходят и следующие `also_the_next` коммитов, какие
    бы они ни были: диск не отвечает совсем."""

    def __init__(self) -> None:
        self.newer_than: int | None = None
        self.also_the_record = False
        self.also_the_next = 0
        self.record_fails_next = False
        self.dead_commits = 0
        self.hits = 0

    def arm(
        self, newer_than: int, *, also_the_record: bool = False, also_the_next: int = 0
    ) -> None:
        self.newer_than = newer_than
        self.also_the_record = also_the_record
        self.also_the_next = also_the_next


async def master_fingerprint(server: Server) -> dict[str, Any]:
    """Всё, что пишет мастер: снимки, его сообщения (рассказ, броски, личные и системные строки),
    версия, ходы (без статусов) и число записей журнала. Строки ходов игроков и сообщение «Мастер
    не справился» (оно адресовано хосту) в отпечаток не входят."""
    game = await server.game()
    snapshots = await server.snapshots()
    messages = [
        (m.id, m.turn_id, m.audience, m.kind.value, m.text, m.superseded)
        for m in await server.messages(
            MessageKind.NARRATION, MessageKind.ROLL, MessageKind.PRIVATE, MessageKind.SYSTEM
        )
        if not (m.kind == MessageKind.SYSTEM and m.text.startswith("Мастер не справился"))
    ]
    return {
        "state_version": game.state_version,
        "current_turn": game.current_turn_id,
        "snapshots": [(row.version, json.dumps(row.data, sort_keys=True)) for row in snapshots],
        "messages": messages,
        "turns": [(t.id, t.number, t.kind) for t in await server.turns()],
        "logs": len(await server.logs()),
    }


async def host_failure_notes(server: Server) -> list[Message]:
    return [
        m
        for m in await server.messages(MessageKind.SYSTEM)
        if m.text.startswith("Мастер не справился")
    ]


# Сбой записи на каждом ходе мастера: до какой версии снимков запись «не мешает», что подготовить и
# что нажать. После сбоя хост жмёт «Повторить», и ход проходит с теми же кубами.
@dataclass(frozen=True)
class FaultCase:
    name: str
    repeat_turn: int | None  # какой ход сценария стоит в очереди дважды
    newer_than: int  # сбой записи снимка новее этой версии


FAULT_CASES = [
    FaultCase("weaving", None, 0),
    FaultCase("opening", 0, 1),
    FaultCase("round", 1, 2),
    FaultCase("combat", 3, 4),
    FaultCase("camp_end", 8, 9),
]


async def bring_to(server: Server, case: FaultCase) -> Callable[[], Awaitable[Any]]:
    """Доводит игру до момента перед ходом мастера из `case`; возвращает действие, которое его
    запускает (оно ещё не сделано)."""
    if case.name in ("weaving", "opening"):
        await gather_in_lobby(server)
        await server.watch_everyone()
        return lambda: server.game_call("alice", "POST", "/start")
    await begin_evening(server)
    if case.name == "round":
        await server.submit("alice", PUBLIC_ACTION)
        await server.submit("boris", BORIS_SECRET_ACTION, secret=True)
        await server.ok("vera", "POST", "/action/pass")
        return lambda: server.game_call("alice", "POST", "/host/collect")
    await round_one(server)
    await round_two(server)
    if case.name == "combat":
        await server.draft("alice", "Уклоняюсь от меча скелета")
        return lambda: server.game_call("alice", "POST", "/action/submit")
    await battle(server)
    await server.submit("alice", "Разжечь костёр у сторожки и отдохнуть до утра")
    await server.ok("boris", "POST", "/action/pass")
    await server.ok("vera", "POST", "/action/pass")
    await server.host("collect")
    await server.settle()
    return lambda: server.game_call("alice", "POST", "/host/camp-end")


class Gate:
    """«Мастер думает»: вызов модели, пока ворота взведены, стоит, пока тест их не откроет."""

    def __init__(self) -> None:
        self.armed = False
        self.entered = asyncio.Event()
        self.opened = asyncio.Event()

    def arm(self) -> None:
        self.armed = True
        self.entered.clear()
        self.opened.clear()

    async def hold(self) -> None:
        if self.armed:
            self.armed = False
            self.entered.set()
            await self.opened.wait()

    def open(self) -> None:
        self.opened.set()


# Тело запроса каждого маршрута игры: верное по форме, чтобы отказ был по состоянию игры, а не по
# запросу. Игрок (не хост) зовёт маршруты игрока, хост — кнопки хоста.
ROUTE_BODIES: dict[tuple[str, str], Any] = {
    ("GET", "/api/games/{game_id}"): None,
    ("POST", "/api/games/{game_id}/ready"): {"ready": True},
    ("POST", "/api/games/{game_id}/start"): None,
    ("POST", "/api/games/{game_id}/characters/random-abilities"): {"class_id": "rogue"},
    ("GET", "/api/games/{game_id}/characters/mine"): None,
    ("POST", "/api/games/{game_id}/characters/random-preview"): None,
    ("POST", "/api/games/{game_id}/characters"): {
        "mode": "quick",
        "class_id": "rogue",
        "species": "halfling",
        "concept": "Карманник, который мечтает о честной жизни",
    },
    ("POST", "/api/games/{game_id}/characters/backstory-help"): {
        "class_id": "rogue",
        "species": "halfling",
        "concept": "Карманник",
        "draft": "",
    },
    ("PUT", "/api/games/{game_id}/action"): {"text": "Ход", "secret": False, "inspiration": False},
    ("POST", "/api/games/{game_id}/action/submit"): None,
    ("POST", "/api/games/{game_id}/action/pass"): None,
    ("POST", "/api/games/{game_id}/camp/line"): {"text": "Подкину дров."},
    # Привалы по кнопке хоста (блок B9): начало привала хостом и кость хитов героя.
    ("POST", "/api/games/{game_id}/host/rest"): {"kind": "short"},
    ("POST", "/api/games/{game_id}/rest/hit-dice"): None,
    ("POST", "/api/games/{game_id}/leave"): None,
    ("POST", "/api/games/{game_id}/players/{user_id}/kick"): None,
    **{
        ("POST", f"/api/games/{{game_id}}/host/{name}"): None
        for name in (
            "collect",
            "extend",
            "close",
            "pause",
            "resume",
            "retry",
            "regenerate",
            "hurry",
            "camp-extend",
            "camp-end",
            "rest-extend",
            "rest-end",
        )
    },
}


def game_route_calls(server: Server, route_table) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Каждый маршрут игры, кроме чтения, как вызов: (кто, метод, путь, параметры запроса).

    Кнопки хоста зовёт хост, остальное — Борис. Список берётся из приложения, а не из теста:
    новый маршрут игры останавливает тест, пока для него не заведено тело."""
    found = {
        (route.method, route.path)
        for route in route_table(server.app)
        if "{game_id}" in route.path and route.path.startswith("/api/")
    }
    assert found == set(ROUTE_BODIES), f"маршруты игры изменились: {found ^ set(ROUTE_BODIES)}"
    calls = []
    for (method, template), body in ROUTE_BODIES.items():
        if method == "GET":
            continue
        host_button = "/host/" in template or template.endswith(("/kick", "/start"))
        path = template.format(game_id=server.game_id, user_id=server.people["vera"].user_id)
        who = "alice" if host_button else "boris"
        calls.append((who, method, path, {} if body is None else {"json": body}))
    return calls


async def hammer_every_route(server: Server, route_table) -> dict[tuple[str, str], int]:
    """Зовёт каждый маршрут игры, кроме чтения, по одному за раз; ответ — по ключу (метод, путь)."""
    statuses: dict[tuple[str, str], int] = {}
    for who, method, path, options in game_route_calls(server, route_table):
        response = await server.call(who, method, path, **options)
        statuses[(method, path)] = response.status_code
    return statuses


async def world_fingerprint(server: Server) -> dict[str, Any]:
    """Вся игра в базе: строка игры, ходы, ходы игроков, сообщения, снимки и журнал."""
    game = await server.game()
    async with server.db() as session:
        actions = await session.scalars(select(PlayerAction).order_by(PlayerAction.turn_id))
        players = await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == server.game_id)
        )
        rows = [
            (a.turn_id, a.user_id, a.status.value, a.text, a.secret, a.inspiration) for a in actions
        ]
        seats = sorted((p.user_id, p.ready, p.character_id) for p in players)
    return {
        "game": {
            key: getattr(game, key)
            for key in (
                "phase",
                "turn_phase",
                "state_version",
                "current_turn_id",
                "deadline_at",
                "active_seconds",
                "clock_at",
            )
        },
        "master": await master_fingerprint(server),
        "turns": [(t.id, t.status.value) for t in await server.turns()],
        "actions": rows,
        "seats": seats,
        "messages": [(m.id, m.kind.value, m.superseded) for m in await server.messages()],
    }


HAMMER_CASES = [case for case in FAULT_CASES if case.name != "opening"]


# --- перезапуск сервера на каждой фазе ---
#
# Перезапуск — второй вход на той же базе: новое приложение, новый `lifespan`, пустая память, новые
# сокеты. Часы отсчётов, кубы и игроки идут дальше, а сценарий мастера начинается с того хода,
# который следует за уже разобранными (провайдер игры хранится в памяти и после остановки забыт).


def carry(server: Server) -> dict[str, Any]:
    """Что переходит в новый запуск: игроки, игра и «сейчас» часов отсчётов."""
    return {"people": server.people, "game_id": server.game_id, "start": server.scheduler.now()}


async def reconnect(server: Server) -> Server:
    """Игроки заново подключаются к перезапущенному серверу."""
    await server.watch_everyone()
    return server
