"""Фикстуры сквозных тестов мастера: настройки вечера, сценарии, запуск сервера, сбои и ворота.

Сами кирпичики (сервер, игроки, этапы вечера, отпечатки базы) лежат в `evening.py`.
"""

import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from dnd_server.config import Settings
from dnd_server.db.models import DmTurnLog, GameStateSnapshot, Message, MessageKind
from dnd_server.llm.fake import FakeProvider
from evening import (
    BOT_TOKEN,
    DEMO_ADVENTURE_DIR,
    EVENING_DICE,
    SESSION_SECRET,
    Gate,
    ModelCalls,
    Person,
    ScriptedThenSeeded,
    Server,
    StateWriteFault,
    evening_scenario,
    running_server,
)


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    shutil.copytree(DEMO_ADVENTURE_DIR, directory / "demo-crypt")
    return directory


@pytest.fixture
def write_scenario(tmp_path: Path) -> Callable[..., Path]:
    """Пишет сценарий вечера во временную папку: целиком, с повтором хода или только хвост."""

    def write(*, repeat_turn: int | None = None, from_turn: int = 0) -> Path:
        scenario = evening_scenario(repeat_turn)
        scenario["turns"] = scenario["turns"][from_turn:]
        path = tmp_path / f"evening-{repeat_turn}-{from_turn}.yaml"
        path.write_text(yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8")
        return path

    return write


@pytest.fixture
def scenario_path(write_scenario: Callable[..., Path]) -> Path:
    return write_scenario()


@pytest.fixture
def settings(settings: Settings, content_dir: Path, scenario_path: Path) -> Settings:
    """Dev-настройки из корневого conftest плюс каталог приключений, секреты и сценарий вечера."""
    return settings.model_copy(
        update={
            "content_dir": content_dir,
            "session_secret": SecretStr(SESSION_SECRET),
            "telegram_bot_token": SecretStr(BOT_TOKEN),
            "telegram_bot_username": "dnd_playthrough_bot",
            "webapp_public_url": "https://app.example.test",
            "fake_llm_scenario": scenario_path,
        }
    )


@pytest.fixture
def serve(
    settings: Settings, db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> Callable[..., AbstractAsyncContextManager[Server]]:
    """`async with serve(rng=…) as server`; параметры настроек меняются именованными аргументами
    `changes={…}`. Второй вход на той же базе — перезапуск сервера."""
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="dnd_server")

    def factory(
        *,
        changes: dict[str, Any] | None = None,
        rng: Any = None,
        people: dict[str, Person] | None = None,
        game_id: str = "",
        start: datetime | None = None,
    ) -> AbstractAsyncContextManager[Server]:
        return running_server(
            settings.model_copy(update=changes or {}),
            db,
            rng=rng,
            people=people,
            game_id=game_id,
            start=start,
        )

    return factory


@pytest.fixture
def evening_dice() -> ScriptedThenSeeded:
    return ScriptedThenSeeded(EVENING_DICE)


@pytest.fixture
def model_calls(monkeypatch: pytest.MonkeyPatch) -> ModelCalls:
    """Считает вызовы `FakeProvider`: ход игры никто не должен запускать, кроме хоста и отсчёта."""
    calls = ModelCalls()
    run_turn, complete_json = FakeProvider.run_turn, FakeProvider.complete_json

    async def counting_turn(self: FakeProvider, req: Any, tools: Any) -> Any:
        calls.turns += 1
        return await run_turn(self, req, tools)

    async def counting_json(self: FakeProvider, req: Any, schema: Any) -> Any:
        calls.json.append(req.purpose)
        return await complete_json(self, req, schema)

    monkeypatch.setattr(FakeProvider, "run_turn", counting_turn)
    monkeypatch.setattr(FakeProvider, "complete_json", counting_json)
    return calls


@pytest.fixture
def disk_fault(monkeypatch: pytest.MonkeyPatch) -> Iterator[StateWriteFault]:
    fault = StateWriteFault()
    real_commit = AsyncSession.commit

    def is_failure_record(row: Any) -> bool:
        """Запись о неудаче: строка журнала хода или сообщение хосту «Мастер не справился»."""
        if isinstance(row, DmTurnLog):
            return True
        return (
            isinstance(row, Message)
            and row.kind == MessageKind.SYSTEM
            and row.text.startswith("Мастер не справился")
        )

    def versions_written(session: Any) -> list[int]:
        return [row.version for row in session.new if isinstance(row, GameStateSnapshot)]

    def remember_state_write(session: Any, _context: Any, _instances: Any) -> None:
        # После `flush` снимок держится в сессии слабой ссылкой, поэтому версию запоминаем заранее.
        if written := versions_written(session):
            session.info["state_write"] = max(written)

    async def commit(session: AsyncSession, *args: Any, **kwargs: Any) -> None:
        written = [*versions_written(session), session.sync_session.info.get("state_write", 0)]
        if fault.newer_than is not None and max(written) > fault.newer_than:
            fault.newer_than = None
            fault.record_fails_next = fault.also_the_record
            fault.dead_commits = fault.also_the_next
            fault.hits += 1
            raise OSError("диск переполнен")
        if fault.dead_commits > 0:
            fault.dead_commits -= 1
            fault.hits += 1
            raise OSError("диск не отвечает")
        if fault.record_fails_next and any(is_failure_record(row) for row in session.new):
            fault.record_fails_next = False
            fault.hits += 1
            raise OSError("диск всё ещё переполнен")
        return await real_commit(session, *args, **kwargs)

    event.listen(Session, "before_flush", remember_state_write)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    yield fault
    event.remove(Session, "before_flush", remember_state_write)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    gate = Gate()
    run_turn, complete_json = FakeProvider.run_turn, FakeProvider.complete_json

    async def held_turn(self: FakeProvider, req: Any, tools: Any) -> Any:
        await gate.hold()
        return await run_turn(self, req, tools)

    async def held_json(self: FakeProvider, req: Any, schema: Any) -> Any:
        if req.purpose == "weaving":
            await gate.hold()
        return await complete_json(self, req, schema)

    monkeypatch.setattr(FakeProvider, "run_turn", held_turn)
    monkeypatch.setattr(FakeProvider, "complete_json", held_json)
    return gate
