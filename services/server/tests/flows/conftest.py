"""Стенд для сквозных сценариев блока B6: настоящий сервер на loopback, клиент как у Mini App.

Сервер — приложение под uvicorn на свободном порту `127.0.0.1` с полным `lifespan` (каталог
приключений из временной копии `demo-crypt`), база — настоящая SQLite после миграций. Клиент — REST
через `httpx` и сокеты через `websockets`. Хаб, лобби, персонажи, вход и каталог работают настоящие,
подменены только запуск мастера (`app.state.game_hooks` записывает вызовы), генератор бросков
(`SeededRng` через `app.state.rng`) и бот (в тестах его нет, см. `no_telegram_bot`). Модель — только
`FakeProvider` по сценарию `scenarios/characters.yaml`.

Стенд запоминает каждый ответ сервера и каждый выданный секрет, а `Stand.assert_clean` проверяет по
ним общие договорённости блока: контракт ошибок, отсутствие секретов в ответах и журнале,
отсутствие данных мастера в ответах игрокам.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.rng import SeededRng
from dnd_server.app import create_app
from dnd_server.config import Settings

# tests/flows/conftest.py -> flows -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "characters.yaml"
SESSION_SECRET = "flows-session-secret-0123456789-abcdefghij"
BOT_TOKEN = "123456789:AAF-flows-bot-token-0123456789_abcdef"
BOT_USERNAME = "dnd_flows_bot"
CREATOR_TG_ID = 700
OUTSIDER_TG_ID = 701
TIMEOUT_S = 5
PONG = {"type": "pong"}

# Поля приключения, которые игрок видеть не должен (тайны NPC, заметки мастера, что знают NPC), и
# слова, по которым сервер не должен выдавать устройство приключения и журнал мастера.
HIDDEN_ADVENTURE_FIELDS = ("secret", "dm_notes", "knows")
HIDDEN_KEYS = ("adventure_data", "dm_turn_logs", "issues")
HIDDEN_EXIT_ID = "to-crypt"  # скрытый выход демо-приключения: id называет отчёт валидатора


def hidden_texts(node: Any) -> list[str]:
    """Все строки полей `secret`, `dm_notes` и `knows` во вложенных данных приключения."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in HIDDEN_ADVENTURE_FIELDS:
                found += [value] if isinstance(value, str) else [str(v) for v in value]
            else:
                found += hidden_texts(value)
    elif isinstance(node, list):
        for item in node:
            found += hidden_texts(item)
    return [text for text in found if text]


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
    HIDDEN_EXIT_ID,
)


# Легальный выбор воина-дварфа (27 очков из 27); имя, вид и класс не совпадают с быстрым режимом.
DETAILED_CHOICES: dict[str, Any] = {
    "name": "Торин",
    "species": "dwarf",
    "class_id": "fighter",
    "background": "soldier",
    "method": "point_buy",
    "base_scores": {"str": 15, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
    "background_bonus": {"str": 2, "con": 1},
    "skills": ["acrobatics", "history"],
    "fighting_style": "defense",
    "persona": {"concept": "Ветеран пограничной стражи", "backstory": "Двадцать лет на заставе."},
}
QUICK_BODY: dict[str, Any] = {"concept": "Карманник, который мечтает о честной жизни"}


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class RecordingHooks:
    """Запуск мастера (B7.8) заменён записью: какие игры начались."""

    def __init__(self) -> None:
        self.started: list[str] = []

    async def on_game_started(self, game_id: str) -> None:
        self.started.append(game_id)


class Watcher:
    """Сокет игрока. Первый кадр — снимок; `drain` отдаёт всё, что сервер успел отправить.

    Ждать молчания нельзя, поэтому «ничего не пришло» проверяется замком: ping идёт через ту же
    очередь, что и события, и `pong` замыкает всё, что отправлено до него."""

    def __init__(self, ws: ClientConnection, snapshot: dict[str, Any]) -> None:
        self.ws = ws
        self.snapshot = snapshot
        self.seen: list[dict[str, Any]] = [snapshot]  # все кадры, полученные этим сокетом

    async def drain(self) -> list[dict[str, Any]]:
        await self.ws.send(json.dumps({"type": "ping"}))
        events = []
        while (message := await recv(self.ws)) != PONG:
            events.append(message)
        self.seen += events
        return events


async def recv(ws: ClientConnection) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))


def sign_init_data(
    user_id: int, first_name: str, start_param: str | None = None, bot_token: str = BOT_TOKEN
) -> str:
    """initData Mini App по алгоритму Telegram: поля, отсортированные `ключ=значение`, и их HMAC."""
    fields = {
        "auth_date": str(int(datetime.now(UTC).timestamp())),
        "user": json.dumps({"id": user_id, "first_name": first_name}),
    }
    if start_param is not None:
        fields["start_param"] = start_param
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def check_error_shape(response: httpx.Response) -> None:
    """Контракт ошибок блока: `{"detail": "<строка>"}`; `problems` лишь у 422, списком строк."""
    where = f"{response.request.method} {response.request.url.path} -> {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        raise AssertionError(f"{where}: ответ ошибки не JSON: {response.text[:80]!r}") from None
    assert isinstance(body, dict), where
    assert isinstance(body.get("detail"), str) and body["detail"], where
    extra = set(body) - {"detail"}
    if extra:
        assert extra == {"problems"} and response.status_code == 422, where
        assert all(isinstance(problem, str) for problem in body["problems"]), where


@dataclass
class Stand:
    app: FastAPI
    port: int
    http: httpx.AsyncClient
    settings: Settings
    db: async_sessionmaker[AsyncSession]
    hooks: RecordingHooks
    log_text: Callable[[], str]
    responses: list[httpx.Response] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    init_data: list[str] = field(default_factory=list)
    watchers: list[Watcher] = field(default_factory=list)

    # --- вход ---

    def player_from(self, login_body: dict[str, Any]) -> Player:
        self.tokens.append(login_body["token"])
        return Player(login_body["user"]["id"], login_body["user"]["name"], login_body["token"])

    async def dev_player(self, number: int) -> Player:
        response = await self.http.post("/api/auth/dev", json={"login": f"dev-{number}"})
        assert response.status_code == 200, response.text
        return self.player_from(response.json())

    def init_data_for(self, tg_id: int, name: str, start_param: str | None = None) -> str:
        """Подписанные данные входа; стенд запоминает их и подпись, чтобы искать их в утечках."""
        raw = sign_init_data(tg_id, name, start_param)
        self.init_data += [raw, dict(parse_qsl(raw))["hash"]]
        return raw

    async def telegram_login(
        self, tg_id: int, name: str, start_param: str | None = None
    ) -> dict[str, Any]:
        """Вход через Telegram, как его делает Mini App; ответ входа целиком."""
        raw = self.init_data_for(tg_id, name, start_param)
        response = await self.http.post(
            "/api/auth/telegram", headers={"Authorization": f"tma {raw}"}
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def telegram_player(self, tg_id: int, name: str) -> Player:
        return self.player_from(await self.telegram_login(tg_id, name))

    # --- запросы ---

    async def call(
        self, player: Player | None, method: str, path: str, **options: Any
    ) -> httpx.Response:
        headers = {**({} if player is None else player.headers), **options.pop("headers", {})}
        return await self.http.request(method, path, headers=headers, **options)

    async def new_game(self, host: Player, **body: Any) -> dict[str, Any]:
        response = await self.call(
            host, "POST", "/api/games", json={"adventure_id": "demo-crypt", **body}
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def join(self, player: Player, code: str) -> dict[str, Any]:
        response = await self.call(player, "POST", "/api/games/join", json={"code": code})
        assert response.status_code == 200, response.text
        return response.json()

    async def ready(self, player: Player, game_id: str, ready: bool = True) -> httpx.Response:
        return await self.call(player, "POST", f"/api/games/{game_id}/ready", json={"ready": ready})

    async def create_character(
        self, player: Player, game_id: str, body: dict[str, Any]
    ) -> httpx.Response:
        return await self.call(player, "POST", f"/api/games/{game_id}/characters", json=body)

    async def detailed(self, player: Player, game_id: str, **changes: Any) -> httpx.Response:
        """Подробный режим: легальный воин-дварф с покупкой очков, `changes` меняют выбор."""
        choices = {**DETAILED_CHOICES, **changes}
        return await self.create_character(
            player, game_id, {"mode": "detailed", "choices": choices}
        )

    async def quick(self, player: Player, game_id: str, **changes: Any) -> httpx.Response:
        """Быстрый режим: класс, вид и замысел; имя придумает модель, если `name` не дано."""
        body = {
            "mode": "quick",
            "class_id": "rogue",
            "species": "halfling",
            **QUICK_BODY,
            **changes,
        }
        return await self.create_character(player, game_id, body)

    async def random(self, player: Player, game_id: str) -> httpx.Response:
        return await self.create_character(player, game_id, {"mode": "random"})

    async def random_abilities(
        self, player: Player, game_id: str, class_id: str = "fighter"
    ) -> dict[str, int]:
        """«Распределить случайно»: раскладка 27 очков от сервера (покупка очков)."""
        response = await self.call(
            player,
            "POST",
            f"/api/games/{game_id}/characters/random-abilities",
            json={"class_id": class_id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["method"] == "point_buy"
        return response.json()["scores"]

    async def my_character(self, player: Player, game_id: str) -> httpx.Response:
        return await self.call(player, "GET", f"/api/games/{game_id}/characters/mine")

    async def game(self, player: Player, game_id: str) -> dict[str, Any]:
        response = await self.call(player, "GET", f"/api/games/{game_id}")
        assert response.status_code == 200, response.text
        return response.json()

    async def master_started(self, game_id: str) -> None:
        """Ждёт вызова мастера: фоновая задача старта идёт уже после ответа на запрос."""
        async with asyncio.timeout(TIMEOUT_S):
            while game_id not in self.hooks.started:
                await asyncio.sleep(0.01)

    # --- сокеты ---

    def ws_url(self, game_id: str, token: str | None = None) -> str:
        query = "" if token is None else f"?token={token}"
        return f"ws://127.0.0.1:{self.port}/ws/games/{game_id}{query}"

    async def watch(self, stack: AsyncExitStack, game_id: str, player: Player) -> Watcher:
        """Подключает игрока к игре и принимает снимок; сокет закроется вместе со `stack`."""
        ws = await stack.enter_async_context(connect(self.ws_url(game_id, player.token)))
        snapshot = await recv(ws)
        assert snapshot["type"] == "snapshot", snapshot
        self.watchers.append(Watcher(ws, snapshot))
        return self.watchers[-1]

    async def snapshot(self, game_id: str, player: Player) -> dict[str, Any]:
        async with AsyncExitStack() as stack:
            return (await self.watch(stack, game_id, player)).snapshot

    # --- общие договорённости блока ---

    def secrets_in(self, text: str, *, own_tokens_ok: bool) -> list[str]:
        """Секреты, которые встретились в тексте: токены не отдаются никому, кроме ответа входа."""
        secrets = [SESSION_SECRET, BOT_TOKEN, BOT_TOKEN.partition(":")[2], *self.init_data]
        if not own_tokens_ok:
            secrets += self.tokens
        return [secret for secret in secrets if secret in text]

    def assert_clean(self) -> None:
        """Все ответы и журнал сервера, накопленные стендом, чисты.

        Секреты (`SESSION_SECRET`, токен бота, initData и его подпись) нигде; токены сессий только в
        ответах входа; каждая ошибка 4xx и 5xx в формате `{"detail": "<строка>"}`; в ответах и
        кадрах сокетов игрокам нет данных мастера (тайны NPC, заметки, id скрытых выходов, копия
        приключения). Отчёт валидатора (`problems` у 422) уходит только создателю игры, поэтому в
        нём id выходов можно."""
        assert self.responses, "стенд не сделал ни одного запроса: проверять нечего"
        for response in self.responses:
            where = f"{response.request.method} {response.request.url.path}"
            login = response.request.url.path.startswith("/api/auth/")
            seen = response.text + str(response.headers)
            assert not self.secrets_in(seen, own_tokens_ok=login), where
            if response.status_code >= 400:
                check_error_shape(response)
            report_for_creator = response.status_code == 422 and "problems" in response.text
            if not report_for_creator:
                for hidden in HIDDEN_FROM_PLAYERS:
                    assert hidden not in response.text, f"{where}: {hidden!r}"
        for watcher in self.watchers:  # кадры сокетов — тот же ответ игроку
            frames = json.dumps(watcher.seen, ensure_ascii=False)
            assert not self.secrets_in(frames, own_tokens_ok=False)
            for hidden in HIDDEN_FROM_PLAYERS:
                assert hidden not in frames, f"в кадрах сокета: {hidden!r}"
        assert not self.secrets_in(self.log_text(), own_tokens_ok=False)


# --- запуск ---


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    shutil.copytree(DEMO_ADVENTURE_DIR, directory / "demo-crypt")
    return directory


@pytest.fixture
def settings(settings: Settings, content_dir: Path) -> Settings:
    """Dev-настройки из корневого conftest плюс каталог, секреты, сценарий и создатель игр."""
    return settings.model_copy(
        update={
            "content_dir": content_dir,
            "session_secret": SecretStr(SESSION_SECRET),
            "telegram_bot_token": SecretStr(BOT_TOKEN),
            "telegram_bot_username": BOT_USERNAME,
            "game_creator_tg_ids": (CREATOR_TG_ID,),
            "webapp_public_url": "https://app.example.test",
            "fake_llm_scenario": SCENARIO,
        }
    )


@asynccontextmanager
async def start_stand(
    settings: Settings, db: async_sessionmaker[AsyncSession], log_text: Callable[[], str]
) -> AsyncIterator[Stand]:
    app = create_app(settings)
    hooks = RecordingHooks()
    app.state.game_hooks = hooks
    app.state.rng = SeededRng(2024)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
    assert server.started, "сервер не стартовал"
    port = server.servers[0].sockets[0].getsockname()[1]

    responses: list[httpx.Response] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        responses.append(response)

    http = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, event_hooks={"response": [record]}
    )
    stand = Stand(app, port, http, settings, db, hooks, log_text, responses)
    try:
        yield stand
    finally:
        await http.aclose()
        server.should_exit = True
        await serving


@pytest.fixture
def serve(
    settings: Settings, db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> Callable[..., AbstractAsyncContextManager[Stand]]:
    """Запускает сервер на базе теста: `async with serve() as stand`; `serve(app_env="prod")`
    меняет настройки. Сервер можно запустить снова на той же базе после остановки прежнего, это и
    есть «перезапуск»."""
    caplog.set_level(logging.INFO)
    # Журнал приложения читается подробно (в том числе отладочные записи), чужие библиотеки — нет.
    caplog.set_level(logging.DEBUG, logger="dnd_server")

    def log_text() -> str:
        return caplog.text

    return lambda **changes: start_stand(settings.model_copy(update=changes), db, log_text)


@pytest.fixture
async def stand(
    serve: Callable[..., AbstractAsyncContextManager[Stand]],
) -> AsyncIterator[Stand]:
    async with serve() as running:
        yield running
