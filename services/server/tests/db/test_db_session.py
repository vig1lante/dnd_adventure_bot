"""Подключение к базе (план B6.1): движок, фабрика сессий, зависимость FastAPI, жизненный цикл.

Проверяются настройки, без которых хранение молча работает неправильно (внешние ключи SQLite),
и то, как база подключена к приложению, чтобы следующие пункты брали сессию зависимостью.
"""

from typing import Annotated

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_server.app import create_app
from dnd_server.db.models import GamePlayer, User
from dnd_server.db.session import get_session


def make_user(name: str = "Игрок") -> User:
    return User(dev_login="dev-1", name=name)


# --- движок и сессии ---


async def test_sqlite_foreign_keys_are_enforced(db):
    # Без PRAGMA foreign_keys SQLite принимает строки с несуществующей игрой и пользователем.
    async with db() as session:
        session.add(GamePlayer(game_id="нет-такой-игры", user_id="нет-такого", joined_order=0))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_attributes_stay_readable_after_commit(db):
    # В async-сессии устаревший после коммита атрибут нельзя перечитать неявно (MissingGreenlet).
    async with db() as session:
        user = make_user()
        session.add(user)
        await session.commit()

        assert user.name == "Игрок"
        assert user.id


async def test_data_committed_by_one_session_is_seen_by_another(db):
    async with db() as session:
        session.add(make_user())
        await session.commit()

    async with db() as other:
        assert (await other.scalars(select(User.name))).all() == ["Игрок"]


# --- подключение к приложению ---


def test_app_keeps_the_engine_and_the_session_factory_in_its_state(settings):
    app = create_app(settings)

    assert app.state.engine.url.render_as_string(hide_password=False) == settings.database_url
    assert app.state.sessionmaker.kw["bind"] is app.state.engine


async def test_get_session_gives_a_session_and_leaves_the_commit_to_the_caller(settings, db):
    app = create_app(settings)

    @app.post("/probe/{name}")
    async def save(name: str, commit: bool, session: Annotated[AsyncSession, Depends(get_session)]):
        session.add(User(dev_login=name, name=name))
        if commit:
            await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        assert (await http.post("/probe/saved", params={"commit": True})).status_code == 200
        assert (await http.post("/probe/forgotten", params={"commit": False})).status_code == 200
    await app.state.engine.dispose()

    async with db() as session:
        # Не закоммиченное запросом откатывается: ход мастера коммитит себя сам, одной транзакцией.
        assert (await session.scalars(select(User.dev_login))).all() == ["saved"]


async def test_client_fixture_serves_the_app_on_a_migrated_database(client):
    health = await client.get("/health")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "env": "dev"}


# --- жизненный цикл ---


def test_building_the_app_creates_no_folders_and_no_files(settings, tmp_path):
    create_app(settings)

    assert not (tmp_path / "data").exists()


async def test_starting_the_app_creates_the_database_folder(settings, tmp_path):
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert (tmp_path / "data").is_dir()
        assert not (tmp_path / "data" / "test.sqlite3").exists()


async def test_stopping_the_app_closes_database_connections(settings):
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        assert app.state.engine.pool.checkedin() == 1

    assert app.state.engine.pool.checkedin() == 0
