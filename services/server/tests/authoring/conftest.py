"""Стенд API черновиков конструктора: фикстуры `settings` и `stand` для тестов этой папки.

Их запрашивают тесты API черновиков, заданий генерации, архива, публикации, справочника ссылок и
проверки. Класс стенда и данные разделов живут в `test_drafts_api.py`, откуда их берут и тесты, и
эти фикстуры. Остальные тесты папки (схемы, разделы, шаги генератора) ни настроек, ни базы не
запрашивают, поэтому переопределение `settings` их не касается.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from generator_fakes import V2_SCENARIO
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_drafts_api import CREATOR_TG_ID, SESSION_SECRET, Stand

from dnd_server.app import create_app
from dnd_server.config import Settings


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Dev-настройки корневого conftest: секрет сессий, сценарий `fake` генератора v2 и создатель
    игр из Telegram."""
    return settings.model_copy(
        update={
            "session_secret": SecretStr(SESSION_SECRET),
            "fake_llm_scenario": V2_SCENARIO,
            "game_creator_tg_ids": (CREATOR_TG_ID,),
        }
    )


@pytest.fixture
async def stand(settings: Settings, db: async_sessionmaker[AsyncSession]) -> AsyncIterator[Stand]:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield Stand(app, http, db)
    await app.state.authoring_tasks.shutdown()
    await app.state.engine.dispose()
