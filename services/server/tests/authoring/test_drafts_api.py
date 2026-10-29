"""API черновиков конструктора `/api/drafts` (планы B4.1, B5.1; спека §10.3; дополнения dispatcher
B4 и B5).

Контракт для параллельной работы с webapp: формы `Draft`, `DraftSummary`, `DraftReport`, ошибки
`{detail, code, problems?}`, версия черновика (409 `version_conflict`), разделы B4 и B5 и доступ
(только создатели игр, чужой черновик — 404). Задания генерации — `test_jobs.py`, справочник ссылок —
`test_refs.py`; они берут стенд и данные разделов отсюда.
"""

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from generator_fakes import V2_SCENARIO, v2_answers, v2_draft
from pydantic import BaseModel, SecretStr
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.budget import XP_BUDGET_PER_CHARACTER
from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Item, Location, Npc, Relation
from dnd_engine.modules import MODULES
from dnd_server.app import create_app
from dnd_server.auth.session import SessionSigner
from dnd_server.authoring import cli, schemas, service
from dnd_server.authoring.draft import STEPS, AuthoringDraft, from_adventure, seeded
from dnd_server.authoring.pipeline import generate
from dnd_server.config import Settings
from dnd_server.db.models import AdventureDraft, AuthoredAdventure, User
from dnd_server.llm.fake import FakeProvider, load_scenario

SESSION_SECRET = "drafts-api-session-secret-0123456789-abcdef"
CREATOR_TG_ID = 777
HEX_ID = re.compile(r"[0-9a-f]{32}")
SECRET_INPUT = "ЗНАЧЕНИЕ-ИЗ-ЗАПРОСА"

DRAFT_FIELDS = {
    "id",
    "version",
    "title",
    "adventure_id",
    "journal_intro",
    "settings",
    "world",
    "deities",
    "factions",
    "plot",
    "locations",
    "npcs",
    "relations",
    "encounters",
    "monsters",
    "items",
    "revelations",
    "start_location",
    "finale_location",
    "locks",
    "wishes",
    "steps",
    "job",
}
SETTINGS_FIELDS = {
    "pitch",
    "tone",
    "target_hours",
    "party_size",
    "party_level",
    "modules",
    "profanity",
}
JOB_FIELDS = {"id", "step", "target", "status", "error", "created_at", "finished_at"}


# --- стенд: приложение в памяти, база теста, авторы ---


@dataclass(frozen=True)
class Author:
    user_id: str
    headers: dict[str, str]


@dataclass
class Stand:
    app: FastAPI
    http: httpx.AsyncClient
    db: async_sessionmaker[AsyncSession]
    users: int = 0

    async def author(self, *, dev_login: str | None = None, tg_id: int | None = None) -> Author:
        """Пользователь с токеном сессии; без аргументов — новый dev-логин (создатель в dev)."""
        if dev_login is None and tg_id is None:
            self.users += 1
            dev_login = f"dev-{self.users}"
        async with self.db() as session:
            user = User(dev_login=dev_login, tg_id=tg_id, name=dev_login or str(tg_id))
            session.add(user)
            await session.commit()
        token = SessionSigner(SESSION_SECRET).issue(user.id)
        return Author(user.id, {"Authorization": f"Bearer {token}"})

    async def call(self, who: Author, method: str, path: str, **options: Any) -> httpx.Response:
        headers = who.headers | options.pop("headers", {})
        return await self.http.request(method, path, headers=headers, **options)

    async def new_draft(self, who: Author, **body: Any) -> dict[str, Any]:
        response = await self.call(who, "POST", "/api/drafts", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    async def put_section(
        self, who: Author, draft: dict[str, Any], section: str, data: Any
    ) -> httpx.Response:
        return await self.call(
            who,
            "PUT",
            f"/api/drafts/{draft['id']}/sections/{section}",
            json={"version": draft["version"], "data": data},
        )

    async def draft(self, who: Author, draft_id: str) -> dict[str, Any]:
        response = await self.call(who, "GET", f"/api/drafts/{draft_id}")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Dev-настройки conftest: секрет сессий, сценарий `fake` генератора v2, создатель из Telegram."""
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


def settings_data(**changes: Any) -> dict[str, Any]:
    return {
        "title": "Маяк на Вороньем мысу",
        "journal_intro": "Маяк погас.",
        "pitch": "Маяк на мысу погас.",
        "tone": "морская готика",
        "target_hours": 2,
        "party_size": 6,
        "party_level": 3,
        "modules": ["bg3_flavor", "campfire"],
        "profanity": True,
    } | changes


def deities_data() -> list[dict[str, Any]]:
    return v2_answers()["generator.pantheon"]["deities"]


def world_data() -> dict[str, Any]:
    return v2_answers()["generator.world"]


def factions_data() -> list[dict[str, Any]]:
    return v2_answers()["generator.factions"]["factions"]


def plot_data() -> dict[str, Any]:
    return v2_answers()["generator.plot"]


def as_served(model: type[BaseModel], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сущности в форме `Draft`: модель движка со всеми полями по умолчанию, как их отдаёт сервер."""
    return [model.model_validate(entity).model_dump(mode="json") for entity in entities]


def locations_data() -> dict[str, Any]:
    """Раздел «Карта» демо v2 (дополнение B5 §1): локации с графом и наполнением, старт и финал."""
    answers = v2_answers()
    graph = answers["generator.locations"]
    filled = [
        node | contents
        for node, contents in zip(
            graph["locations"], answers["generator.location_contents"], strict=True
        )
    ]
    return {
        "locations": as_served(Location, filled),
        "start_location": graph["start_location"],
        "finale_location": graph["finale_location"],
    }


# Локации демо по порядку: гавань, тропа, комнаты смотрителя (тайник и тайна), фонарная.
HARBOR, CLIFF_PATH, LIGHTHOUSE, LAMP_ROOM = range(4)


def location_at(index: int) -> dict[str, Any]:
    return locations_data()["locations"][index]


def map_with(index: int = HARBOR, **changes: Any) -> dict[str, Any]:
    """«Карта» демо, где у локации `index` заменены поля `changes`."""
    data = locations_data()
    data["locations"][index] |= changes
    return data


def cupboard(**changes: Any) -> dict[str, Any]:
    """Тайник демо: шкаф смотрителя, внутри которого лежит ключ от фонарной (`reveals: item`)."""
    [feature] = [f for f in location_at(LIGHTHOUSE)["features"] if f["id"] == "keeper-cupboard"]
    return feature | changes


# Проверка детали или тайны локации со Сл из таблицы.
CHECK = {"ability": "wis", "skill": "perception", "dc": 10}


def npcs_data() -> list[dict[str, Any]]:
    return as_served(Npc, v2_answers()["generator.npcs"]["npcs"])


def relations_data() -> list[dict[str, Any]]:
    return as_served(Relation, v2_answers()["generator.relations"]["relations"])


def items_data() -> list[dict[str, Any]]:
    return as_served(Item, v2_answers()["generator.items"]["items"])


def section_data(section: str) -> Any:
    """Верные данные любого раздела B4 и B5 (из демо-сценария v2)."""
    return {
        "settings": settings_data,
        "world": world_data,
        "deities": deities_data,
        "factions": factions_data,
        "plot": plot_data,
        "locations": locations_data,
        "npcs": npcs_data,
        "relations": relations_data,
        "items": items_data,
    }[section]()


def served_section(draft: dict[str, Any], section: str) -> Any:
    """Данные раздела из `Draft` — так их сохраняет webapp: «Карта» — объект со стартом и финалом,
    «Замысел» — настройки с названием и главным для дневника."""
    if section == "locations":
        return {name: draft[name] for name in ("locations", "start_location", "finale_location")}
    if section == "settings":
        return draft["settings"] | {
            "title": draft["title"],
            "journal_intro": draft["journal_intro"],
        }
    return draft[section]


def database_file(settings: Settings) -> Path:
    return Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))


def assert_draft_shape(draft: dict[str, Any]) -> None:
    """Форма `Draft` дополнения §1 — страж для webapp: все поля, все 12 шагов, `job` — `Job`."""
    assert set(draft) == DRAFT_FIELDS
    assert set(draft["settings"]) == SETTINGS_FIELDS
    assert list(draft["steps"]) == list(STEPS)
    for state in draft["steps"].values():
        assert set(state) == {"status", "updated_at"}
        assert state["status"] in ("empty", "draft", "ready")
    assert draft["job"] is None or set(draft["job"]) == JOB_FIELDS


async def count_drafts(db: async_sessionmaker[AsyncSession]) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(AdventureDraft))


def assert_error(response: httpx.Response, status: int, code: str | None) -> dict[str, Any]:
    """Ошибка конструктора: `detail` строкой, `code` у 409/422, `problems` — только списком строк."""
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    assert body.get("code") == code
    assert set(body) <= {"detail", "code", "problems"}
    if "problems" in body:
        assert body["problems"] and all(isinstance(line, str) for line in body["problems"])
    return body


# --- создание, чтение, список ---


async def test_a_new_draft_has_the_shape_of_the_contract(stand):
    author = await stand.author()

    draft = await stand.new_draft(author, title="Маяк")

    assert set(draft) == DRAFT_FIELDS
    assert HEX_ID.fullmatch(draft["id"])
    assert (draft["version"], draft["title"], draft["adventure_id"]) == (1, "Маяк", None)
    assert draft["journal_intro"] == ""
    assert draft["settings"] == {
        "pitch": "",
        "tone": "",
        "target_hours": 3.0,
        "party_size": 6,
        "party_level": 3,
        "modules": [],
        "profanity": True,
    }
    assert (draft["world"], draft["plot"], draft["job"]) == (None, None, None)
    for section in ("deities", "factions", "locations", "npcs", "relations", "encounters"):
        assert draft[section] == []
    for section in ("monsters", "items", "revelations"):
        assert draft[section] == []
    assert (draft["start_location"], draft["finale_location"]) == (None, None)
    assert (draft["locks"], draft["wishes"]) == ([], {})
    assert list(draft["steps"]) == list(STEPS)
    for step in STEPS:
        assert set(draft["steps"][step]) == {"status", "updated_at"}
        assert draft["steps"][step]["status"] in ("empty", "draft", "ready")
        assert draft["steps"][step]["updated_at"] is None
    # Название — уже содержимое Замысла, но без id приключения он черновик (дополнение §3);
    # остальное пусто, и шаги-наполнители тоже.
    assert draft["steps"]["concept"]["status"] == "draft"
    assert {draft["steps"][step]["status"] for step in STEPS if step != "concept"} == {"empty"}


async def test_every_step_of_a_new_draft_without_a_title_is_empty(stand):
    author = await stand.author()

    draft = await stand.new_draft(author)

    assert {state["status"] for state in draft["steps"].values()} == {"empty"}


async def test_a_draft_may_be_created_without_a_body_and_then_has_no_title(stand):
    author = await stand.author()

    response = await stand.call(author, "POST", "/api/drafts")

    assert response.status_code == 201, response.text
    assert response.json()["title"] == ""


async def test_the_author_reads_the_draft_back_as_it_was_created(stand):
    author = await stand.author()
    created = await stand.new_draft(author, title="Маяк")

    assert await stand.draft(author, created["id"]) == created


async def test_the_list_holds_only_the_authors_drafts_newest_first(stand):
    author, other = await stand.author(), await stand.author()
    first = await stand.new_draft(author, title="Первый")
    second = await stand.new_draft(author, title="Второй")
    await stand.new_draft(other, title="Чужой")

    response = await stand.call(author, "GET", "/api/drafts")

    assert response.status_code == 200, response.text
    summaries = response.json()
    assert [s["id"] for s in summaries] == [second["id"], first["id"]]
    assert set(summaries[0]) == {
        "id",
        "title",
        "adventure_id",
        "version",
        "updated_at",
        "published_id",
        "published_at",
    }
    assert (summaries[0]["title"], summaries[0]["version"]) == ("Второй", 1)
    assert summaries[0]["adventure_id"] is None
    assert isinstance(summaries[0]["updated_at"], str)
    # Ни один черновик не публиковался.
    assert {(s["published_id"], s["published_at"]) for s in summaries} == {(None, None)}


async def publish_rows(stand: Stand, author: Author, draft_id: str, *rows: tuple[str, datetime]):
    """Строки опубликованного приключения из черновика, как их пишет публикация (блок B6)."""
    async with stand.db() as session:
        session.add_all(
            AuthoredAdventure(
                id=adventure_id,
                owner_user_id=author.user_id,
                data={},
                source_draft_id=draft_id,
                published_at=moment,
            )
            for adventure_id, moment in rows
        )
        await session.commit()


async def test_the_list_tells_which_drafts_are_published_with_their_first_publication(stand):
    # Дополнение B11 §2: бейдж «опубликовано» в списке черновиков. Если строк несколько (данные до
    # fix-раунда B6), берётся самая ранняя публикация, как у `published_id` одного черновика.
    author = await stand.author()
    plain = await stand.new_draft(author, title="Черновик")
    published = await stand.new_draft(author, title="Маяк")
    early = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    await publish_rows(
        stand,
        author,
        published["id"],
        ("lighthouse-late", early + timedelta(days=1)),
        ("lighthouse", early),
    )

    summaries = {s["id"]: s for s in (await stand.call(author, "GET", "/api/drafts")).json()}

    assert (summaries[plain["id"]]["published_id"], summaries[plain["id"]]["published_at"]) == (
        None,
        None,
    )
    assert summaries[published["id"]]["published_id"] == "lighthouse"
    assert summaries[published["id"]]["published_at"] == "2026-10-20T09:00:00+00:00"


async def test_the_badge_of_the_list_names_the_same_publication_as_the_locked_id_of_the_draft(
    stand,
):
    # Бейдж списка и закреплённый id черновика (`published_id`, блок B6) берут одну и ту же
    # публикацию — самую раннюю, при равном времени — с меньшим id, в любом порядке строк.
    author = await stand.author()
    tie = await stand.new_draft(author, title="Ничья")
    later = await stand.new_draft(author, title="Позже")
    moment = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    await publish_rows(stand, author, tie["id"], ("tie-b", moment), ("tie-a", moment))
    await publish_rows(
        stand, author, later["id"], ("later-a", moment + timedelta(hours=1)), ("later-z", moment)
    )

    summaries = {s["id"]: s for s in (await stand.call(author, "GET", "/api/drafts")).json()}
    async with stand.db() as session:
        locked = {
            draft["id"]: await service.published_id(session, draft["id"]) for draft in (tie, later)
        }

    assert locked == {tie["id"]: "tie-a", later["id"]: "later-z"}
    assert {key: summaries[key]["published_id"] for key in locked} == locked
    assert {summaries[key]["published_at"] for key in locked} == {"2026-10-20T09:00:00+00:00"}


async def test_the_list_reads_the_publications_of_every_draft_at_once(stand):
    # Без N+1: число запросов к базе не растёт с числом черновиков.
    author = await stand.author()

    async def statements_for_the_list() -> int:
        executed: list[str] = []

        def count(_conn, _cursor, statement, *_args) -> None:
            executed.append(statement)

        engine = stand.app.state.engine.sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            response = await stand.call(author, "GET", "/api/drafts")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert response.status_code == 200, response.text
        # JSON опубликованного приключения списку не нужен (R7 блока B11): его столбец не читается.
        assert not any("authored_adventures.data" in statement for statement in executed)
        return len(executed)

    first = await stand.new_draft(author, title="Один")
    await publish_rows(stand, author, first["id"], ("one", datetime(2026, 10, 20, tzinfo=UTC)))
    with_one = await statements_for_the_list()
    for number in range(4):
        made = await stand.new_draft(author, title=f"Ещё {number}")
        moment = datetime(2026, 10, 21, number, tzinfo=UTC)
        await publish_rows(stand, author, made["id"], (f"more-{number}", moment))
    with_five = await statements_for_the_list()

    assert with_five == with_one


async def test_an_edited_draft_moves_to_the_top_of_the_list(stand):
    author = await stand.author()
    first = await stand.new_draft(author, title="Первый")
    second = await stand.new_draft(author, title="Второй")

    await stand.put_section(author, first, "settings", settings_data(title="Первый, правка"))
    summaries = (await stand.call(author, "GET", "/api/drafts")).json()

    assert [(s["id"], s["title"], s["version"]) for s in summaries] == [
        (first["id"], "Первый, правка", 2),
        (second["id"], "Второй", 1),
    ]


async def test_a_title_longer_than_the_limit_is_refused_and_no_draft_is_made(stand):
    author = await stand.author()

    response = await stand.call(author, "POST", "/api/drafts", json={"title": "Я" * 101})

    assert_error(response, 422, "invalid_data")
    assert await count_drafts(stand.db) == 0


# --- разделы ---


async def test_settings_write_the_settings_title_and_journal_intro_and_raise_the_version(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, "settings", settings_data())

    assert response.status_code == 200, response.text
    saved = response.json()
    assert set(saved) == DRAFT_FIELDS
    assert saved["version"] == 2
    assert (saved["title"], saved["journal_intro"]) == ("Маяк на Вороньем мысу", "Маяк погас.")
    assert saved["settings"]["modules"] == ["bg3_flavor", "campfire"]
    assert isinstance(saved["steps"]["concept"]["updated_at"], str)
    assert all(saved["steps"][step]["updated_at"] is None for step in STEPS if step != "concept")
    assert await stand.draft(author, draft["id"]) == saved


@pytest.mark.parametrize(
    ("section", "data", "step"),
    [
        ("world", v2_answers()["generator.world"], "world"),
        ("deities", deities_data(), "pantheon"),
        ("factions", v2_answers()["generator.factions"]["factions"], "factions"),
        ("plot", v2_answers()["generator.plot"], "plot"),
    ],
)
async def test_each_section_of_block_4_is_saved_and_read_back(stand, section, data, step):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["version"] == 2
    assert saved[section] is not None and saved[section] != []
    assert saved["steps"][step]["status"] in ("draft", "ready")
    assert isinstance(saved["steps"][step]["updated_at"], str)
    # Чтение отдаёт ровно то, что записано, а повторная запись тех же данных — то же самое.
    again = await stand.put_section(author, saved, section, saved[section])
    assert again.status_code == 200, again.text
    assert again.json()[section] == saved[section]


async def test_a_stale_version_is_a_conflict_and_the_draft_stays_as_it_was(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    await stand.put_section(author, draft, "settings", settings_data(title="Свежая правка"))

    stale = await stand.put_section(author, draft, "settings", settings_data(title="Старая"))

    body = assert_error(stale, 409, "version_conflict")
    assert "Свежая правка" not in stale.text and "problems" not in body  # тело без данных
    assert (await stand.draft(author, draft["id"]))["title"] == "Свежая правка"


async def test_data_that_breaks_the_schema_is_422_with_problems_and_no_input_values(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    broken = [deities_data()[0] | {"name": None, "domains": SECRET_INPUT}]

    response = await stand.put_section(author, draft, "deities", broken)

    body = assert_error(response, 422, "invalid_data")
    assert all(line.startswith("deities.0.") for line in body["problems"]), body
    assert SECRET_INPUT not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


async def test_limits_of_the_api_are_422_with_problems(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, "settings", settings_data(party_size=7))

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith("settings.party_size") for line in body["problems"])


@pytest.mark.parametrize(
    "body",
    [
        {"version": 1, "data": settings_data(), "extra": 1},  # лишнее поле тела
        {"version": 1, "data": settings_data(extra=1)},  # лишнее поле раздела
        {"data": settings_data()},  # без версии
        {"version": "один", "data": settings_data()},
    ],
)
async def test_extra_fields_and_a_malformed_body_are_422_invalid_data(stand, body):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author, "PUT", f"/api/drafts/{draft['id']}/sections/settings", json=body
    )

    assert_error(response, 422, "invalid_data")
    assert (await stand.draft(author, draft["id"]))["version"] == 1


async def test_a_body_that_is_not_json_is_422_invalid_data(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/sections/settings",
        content=b'{"version": 1, "data": ',
        headers={"Content-Type": "application/json"},
    )

    assert_error(response, 422, "invalid_data")


@pytest.mark.parametrize("section", ["nope", "SETTINGS", "wishes"])
async def test_a_section_outside_the_editor_is_unsupported(stand, section):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, [])

    assert_error(response, 422, "unsupported_section")


# --- закрепления и пожелания ---


async def test_locks_are_replaced_as_a_whole_list_and_raise_the_version(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": 1, "locks": ["step:world", "deity:selmara"]},
    )
    again = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": 2, "locks": ["faction:smugglers"]},
    )

    assert response.status_code == 200, response.text
    assert (response.json()["version"], response.json()["locks"]) == (
        2,
        ["deity:selmara", "step:world"],
    )
    assert (again.json()["version"], again.json()["locks"]) == (3, ["faction:smugglers"])


@pytest.mark.parametrize("ref", ["selmara", "dragon:x", "step:nope", "deity:Bad Id", ""])
async def test_a_lock_not_in_the_ref_format_is_invalid_locks(stand, ref):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": 1, "locks": ["step:world", ref]},
    )

    body = assert_error(response, 422, "invalid_locks")
    assert any(line.startswith("locks.1") for line in body["problems"]), body
    if ref:
        assert all(ref not in line for line in body["problems"])


async def test_locks_with_a_stale_version_are_a_conflict(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author, "PUT", f"/api/drafts/{draft['id']}/locks", json={"version": 5, "locks": []}
    )

    assert_error(response, 409, "version_conflict")


async def test_a_wish_is_saved_trimmed_and_an_empty_one_removes_it(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = f"/api/drafts/{draft['id']}/wishes/world"

    saved = await stand.call(author, "PUT", path, json={"version": 1, "text": "  Пираты  "})
    removed = await stand.call(author, "PUT", path, json={"version": 2, "text": "   "})

    assert saved.status_code == 200, saved.text
    assert (saved.json()["version"], saved.json()["wishes"]) == (2, {"world": "Пираты"})
    assert (removed.json()["version"], removed.json()["wishes"]) == (3, {})


async def test_a_wish_longer_than_2000_characters_is_invalid_wish(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/wishes/world",
        json={"version": 1, "text": "Я" * 2001},
    )

    assert_error(response, 422, "invalid_wish")
    assert (await stand.draft(author, draft["id"]))["wishes"] == {}


@pytest.mark.parametrize("step", ["nope", "Concept", "settings"])
async def test_a_wish_for_a_step_outside_the_generator_is_unsupported(stand, step):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/wishes/{step}",
        json={"version": 1, "text": "Пираты"},
    )

    assert_error(response, 422, "unsupported_step")


# --- удаление ---


async def test_the_author_deletes_the_draft(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(author, "DELETE", f"/api/drafts/{draft['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert_error(await stand.call(author, "GET", f"/api/drafts/{draft['id']}"), 404, None)
    assert await count_drafts(stand.db) == 0


# --- проверка ---


async def test_validate_gives_the_report_of_the_written_sections(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    plot = v2_answers()["generator.plot"] | {"villain": "deity:nobody"}
    draft = (await stand.put_section(author, draft, "plot", plot)).json()

    response = await stand.call(author, "POST", f"/api/drafts/{draft['id']}/validate")

    assert response.status_code == 200, response.text
    report = response.json()
    assert set(report) == {"errors", "warnings", "score", "estimated_minutes", "readiness_percent"}
    assert [(f["code"], f["ref"], f["step"]) for f in report["errors"]] == [
        ("dangling_ref", "adventure", "plot")
    ]
    for finding in report["errors"] + report["warnings"]:
        assert set(finding) == {"code", "message", "ref", "step"}
    assert isinstance(report["score"], int) and isinstance(report["estimated_minutes"], int)
    assert 0 <= report["readiness_percent"] <= 99
    assert (await stand.draft(author, draft["id"]))["version"] == draft["version"]  # не запись


# --- доступ ---


# Тело каждого маршрута черновика: обход ниже вызывает все маршруты, и новый маршрут без записи
# здесь останавливает тест — доступ к нему должен быть проверен.
DRAFT_ROUTES = {
    ("GET", "/api/drafts"): None,
    ("POST", "/api/drafts"): {"title": "Чужое"},
    ("GET", "/api/drafts/{draft_id}"): None,
    ("DELETE", "/api/drafts/{draft_id}"): None,
    ("PUT", "/api/drafts/{draft_id}/sections/{section}"): {"version": 1, "data": settings_data()},
    ("PUT", "/api/drafts/{draft_id}/locks"): {"version": 1, "locks": []},
    ("PUT", "/api/drafts/{draft_id}/wishes/{step}"): {"version": 1, "text": "Пираты"},
    ("POST", "/api/drafts/{draft_id}/generate"): {"step": "concept"},
    ("GET", "/api/drafts/{draft_id}/jobs/{job_id}"): None,
    ("POST", "/api/drafts/{draft_id}/validate"): None,
    ("GET", "/api/drafts/{draft_id}/refs"): None,
    ("POST", "/api/drafts/{draft_id}/publish"): {},
    ("GET", "/api/drafts/{draft_id}/export"): None,
    ("POST", "/api/drafts/import"): None,
}
# Маршруты, которым id черновика не нужен: у них нет чужого черновика.
WITHOUT_DRAFT = {("GET", "/api/drafts"), ("POST", "/api/drafts"), ("POST", "/api/drafts/import")}


def draft_routes(stand: Stand, route_table) -> set[tuple[str, str]]:
    return {
        (route.method, route.path)
        for route in route_table(stand.app)
        if route.path.startswith("/api/drafts")
    }


def concrete(template: str, draft_id: str, job_id: str) -> str:
    return template.format(draft_id=draft_id, section="settings", step="world", job_id=job_id)


async def test_the_walk_knows_every_route_of_the_drafts(stand, route_table):
    assert draft_routes(stand, route_table) == set(DRAFT_ROUTES)


async def test_a_player_outside_the_creator_list_gets_403_on_every_draft_route(stand):
    author = await stand.author()
    outsider = await stand.author(tg_id=CREATOR_TG_ID + 1)
    draft = await stand.new_draft(author)

    for (method, template), body in DRAFT_ROUTES.items():
        path = concrete(template, draft["id"], "job")
        response = await stand.call(outsider, method, path, json=body)
        assert_error(response, 403, None)
    assert await count_drafts(stand.db) == 1
    assert (await stand.draft(author, draft["id"]))["version"] == 1


async def test_a_creator_from_the_telegram_list_may_make_drafts(stand):
    creator = await stand.author(tg_id=CREATOR_TG_ID)

    draft = await stand.new_draft(creator, title="Маяк")

    assert draft["title"] == "Маяк"


async def test_another_creators_draft_is_404_on_every_route_and_stays_untouched(stand):
    author, other = await stand.author(), await stand.author()
    draft = await stand.new_draft(author)
    job = await stand.call(
        author, "POST", f"/api/drafts/{draft['id']}/generate", json={"step": "world"}
    )
    assert job.status_code == 202, job.text
    await stand.app.state.authoring_tasks.join()
    before = await stand.draft(author, draft["id"])

    for (method, template), body in DRAFT_ROUTES.items():
        if (method, template) in WITHOUT_DRAFT:
            continue
        path = concrete(template, draft["id"], job.json()["id"])
        response = await stand.call(other, method, path, json=body)
        assert_error(response, 404, None)
    await stand.app.state.authoring_tasks.join()
    assert await stand.draft(author, draft["id"]) == before


async def test_a_draft_that_does_not_exist_is_404(stand):
    author = await stand.author()

    response = await stand.call(author, "GET", f"/api/drafts/{'0' * 32}")

    assert_error(response, 404, None)


# --- финальное покрытие блока 4: каждая запись — целый `Draft` и версия ровно +1 ---

SECTIONS = (
    "settings",
    "world",
    "deities",
    "factions",
    "plot",
    "locations",
    "npcs",
    "relations",
    "items",
)


async def test_every_write_answers_with_the_whole_draft_one_version_up(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = f"/api/drafts/{draft['id']}"
    writes = [
        *((f"{path}/sections/{section}", {"data": section_data(section)}) for section in SECTIONS),
        (f"{path}/sections/settings", {"data": settings_data()}),  # те же данные ещё раз
        (f"{path}/locks", {"locks": ["step:world", "deity:selmara"]}),
        (f"{path}/wishes/plot", {"text": "Больше моря"}),
        (f"{path}/wishes/plot", {"text": ""}),  # удаление пожелания — тоже запись
    ]

    for number, (url, body) in enumerate(writes, start=1):
        response = await stand.call(author, "PUT", url, json={"version": number} | body)
        assert response.status_code == 200, (url, response.text)
        saved = response.json()
        assert_draft_shape(saved)
        assert saved["version"] == number + 1, url
        assert await stand.draft(author, draft["id"]) == saved
    # Чтение и проверка черновик не пишут.
    await stand.call(author, "POST", f"{path}/validate")
    assert (await stand.draft(author, draft["id"]))["version"] == len(writes) + 1


# --- чужая версия у каждой записи и гонка двух записей ---


STALE_WRITES = {
    "sections/settings": {"data": settings_data(title="Старая вкладка")},
    "sections/deities": {"data": deities_data()},
    "sections/locations": {"data": locations_data()},
    "sections/npcs": {"data": npcs_data()},
    "sections/relations": {"data": relations_data()},
    "sections/items": {"data": items_data()},
    "locks": {"locks": ["step:world"]},
    "wishes/world": {"text": "Пираты"},
}


@pytest.mark.parametrize("write", STALE_WRITES)
async def test_a_stale_version_on_every_write_is_409_without_the_draft(stand, write):
    author = await stand.author()
    draft = await stand.new_draft(author)
    fresh = await stand.put_section(author, draft, "settings", settings_data(title="Свежая"))
    fresh = fresh.json()

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/{write}",
        json={"version": draft["version"]} | STALE_WRITES[write],
    )

    body = assert_error(response, 409, "version_conflict")
    assert set(body) == {"detail", "code"}
    assert "Свежая" not in response.text and "Пираты" not in response.text
    assert await stand.draft(author, draft["id"]) == fresh


RACING_WRITES = {
    "settings": ("sections/settings", {"data": settings_data(title="Первая вкладка")}),
    "other-settings": ("sections/settings", {"data": settings_data(title="Вторая вкладка")}),
    "locks": ("locks", {"locks": ["step:world"]}),
    "wish": ("wishes/plot", {"text": "Пираты"}),
}


@pytest.mark.parametrize(
    ("one", "other"), [("settings", "other-settings"), ("settings", "locks"), ("locks", "wish")]
)
async def test_two_writes_on_one_version_at_once_let_exactly_one_through(stand, one, other):
    author = await stand.author()
    draft = await stand.new_draft(author)

    async def write(name: str) -> httpx.Response:
        tail, body = RACING_WRITES[name]
        return await stand.call(
            author, "PUT", f"/api/drafts/{draft['id']}/{tail}", json={"version": 1} | body
        )

    first, second = await asyncio.gather(write(one), write(other))

    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    winner, loser = (first, second) if first.status_code == 200 else (second, first)
    assert_error(loser, 409, "version_conflict")
    assert await stand.draft(author, draft["id"]) == winner.json()
    assert winner.json()["version"] == 2


async def test_a_write_landing_between_the_check_and_the_save_is_refused_by_the_database(
    stand, settings, monkeypatch
):
    # Проверка версии в коде прошла, а до сохранения черновик изменила другая запись. Её держит
    # счётчик версий в самой базе: правка этой вкладки не перетирает чужую.
    author = await stand.author()
    draft = await stand.new_draft(author)
    real_apply = service.apply_section

    def apply_and_race(*args: Any) -> Any:
        changed = real_apply(*args)
        with closing(sqlite3.connect(database_file(settings))) as connection:
            connection.execute(
                "UPDATE adventure_drafts SET title = 'Другая вкладка', version = version + 1"
                " WHERE id = ?",
                (draft["id"],),
            )
            connection.commit()
        return changed

    monkeypatch.setattr(service, "apply_section", apply_and_race)

    response = await stand.put_section(author, draft, "settings", settings_data(title="Эта"))

    assert_error(response, 409, "version_conflict")
    saved = await stand.draft(author, draft["id"])
    assert saved["version"] == 2 and saved["settings"]["pitch"] == ""  # раздел не записан
    [summary] = (await stand.call(author, "GET", "/api/drafts")).json()
    assert summary["title"] == "Другая вкладка"


# --- пределы на границе API (дополнение §7) ---


LEVELS = sorted(XP_BUDGET_PER_CHARACTER)


@pytest.mark.parametrize(
    ("changes", "accepted"),
    [
        ({"party_size": 0}, False),
        ({"party_size": 1}, True),
        ({"party_size": 6}, True),
        ({"party_size": 7}, False),
        ({"party_level": LEVELS[0] - 1}, False),
        ({"party_level": LEVELS[0]}, True),
        ({"party_level": LEVELS[-1]}, True),
        ({"party_level": LEVELS[-1] + 1}, False),
        ({"target_hours": 0}, False),
        ({"target_hours": -1}, False),
        ({"target_hours": 0.25}, True),
        ({"target_hours": 12}, True),
        ({"title": "Я" * 100}, True),
        ({"title": "Я" * 101}, False),
        ({"title": "🐉" * 100}, True),  # знак вне базовой плоскости — один знак, а не четыре байта
        ({"title": "🐉" * 101}, False),
        ({"pitch": "Я" * 2000}, True),
        ({"pitch": "Я" * 2001}, False),
        ({"pitch": "🐉" * 2000}, True),
        ({"pitch": "🐉" * 2001}, False),
        ({"tone": "Я" * 300}, True),
        ({"tone": "Я" * 301}, False),
        ({"tone": "🐉" * 300}, True),
        ({"tone": "🐉" * 301}, False),
        ({"modules": []}, True),
        ({"modules": list(MODULES)}, True),
        ({"modules": ["campfire", "dragons"]}, False),
    ],
)
async def test_settings_at_and_past_the_limits_of_the_api(stand, changes, accepted):
    author = await stand.author()
    draft = await stand.new_draft(author)
    [(field, value)] = changes.items()

    response = await stand.put_section(author, draft, "settings", settings_data(**changes))

    if accepted:
        assert response.status_code == 200, response.text
        saved = response.json()
        assert (saved["title"] if field == "title" else saved["settings"][field]) == value
        return
    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith(f"settings.{field}") for line in body["problems"]), body
    # Значения входа в ответ не попадают: ни длинный текст, ни имя неизвестного модуля.
    if isinstance(value, str):
        assert value[:50] not in response.text
    assert "dragons" not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


@pytest.mark.parametrize("number", ["Infinity", "-Infinity", "NaN"])
async def test_hours_that_are_not_a_finite_number_are_refused(stand, number):
    # JSON по стандарту таких чисел не знает, но разбор тела в Python их принимает.
    author = await stand.author()
    draft = await stand.new_draft(author)
    body = json.dumps({"version": 1, "data": settings_data(target_hours="ЧИСЛО")})

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/sections/settings",
        content=body.replace('"ЧИСЛО"', number).encode(),
        headers={"Content-Type": "application/json"},
    )

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith("settings.target_hours") for line in body["problems"]), body


@pytest.mark.parametrize(
    ("title", "status"), [("", 201), ("Я" * 100, 201), ("🐉" * 100, 201), ("🐉" * 101, 422)]
)
async def test_the_title_of_a_new_draft_stops_at_100_characters(stand, title, status):
    author = await stand.author()

    response = await stand.call(author, "POST", "/api/drafts", json={"title": title})

    assert response.status_code == status, response.text
    if status == 201:
        assert response.json()["title"] == title
    else:
        assert_error(response, 422, "invalid_data")
        assert await count_drafts(stand.db) == 0


def many_regions(count: int) -> dict[str, Any]:
    region = world_data()["regions"][0]
    return world_data() | {"regions": [region | {"id": f"r{n}"} for n in range(count)]}


def many_rumors(count: int) -> dict[str, Any]:
    return world_data() | {"rumors": [world_data()["rumors"][0]] * count}


def many_acts(count: int) -> dict[str, Any]:
    act = plot_data()["acts"][0]
    return plot_data() | {"acts": [act | {"id": f"a{n}"} for n in range(count)]}


def many_endings(count: int) -> dict[str, Any]:
    return plot_data() | {"endings": ["Конец."] * count}


def many_locations(count: int) -> dict[str, Any]:
    harbor = locations_data()["locations"][0]
    return {
        "locations": [harbor | {"id": f"l{n}"} for n in range(count)],
        "start_location": None,
        "finale_location": None,
    }


def many_of(build, count: int) -> list[dict[str, Any]]:
    """`count` копий первой сущности раздела с разными id."""
    return [build()[0] | {"id": f"e{n}"} for n in range(count)]


@pytest.mark.parametrize(
    ("section", "build", "limit"),
    [
        (
            "deities",
            lambda n: [deities_data()[0] | {"id": f"d{i}"} for i in range(n)],
            schemas.MAX_DEITIES,
        ),
        (
            "factions",
            lambda n: [factions_data()[0] | {"id": f"f{i}"} for i in range(n)],
            schemas.MAX_FACTIONS,
        ),
        ("world", many_regions, 6),
        ("world", many_rumors, 8),
        ("plot", many_acts, 5),
        ("plot", many_endings, 5),
        ("locations", many_locations, schemas.MAX_LOCATIONS),
        ("npcs", lambda n: many_of(npcs_data, n), schemas.MAX_NPCS),
        ("relations", lambda n: many_of(relations_data, n), schemas.MAX_RELATIONS),
        ("items", lambda n: many_of(items_data, n), schemas.MAX_ITEMS),
    ],
    ids=[
        "deities",
        "factions",
        "regions",
        "rumors",
        "acts",
        "endings",
        "locations",
        "npcs",
        "relations",
        "items",
    ],
)
async def test_the_number_of_entities_stops_at_the_limit_of_a_model_answer(
    stand, section, build, limit
):
    author = await stand.author()
    draft = await stand.new_draft(author)

    at_limit = await stand.put_section(author, draft, section, build(limit))
    assert at_limit.status_code == 200, at_limit.text
    over = await stand.put_section(author, at_limit.json(), section, build(limit + 1))

    body = assert_error(over, 422, "invalid_data")
    assert all(line.startswith(section) for line in body["problems"]), body
    assert await stand.draft(author, draft["id"]) == at_limit.json()


def test_the_limits_of_gods_and_factions_are_the_ones_of_the_contract():
    # Дополнение §7 называет числа: богов и фракций не больше шести.
    assert (schemas.MAX_DEITIES, schemas.MAX_FACTIONS) == (6, 6)


def test_the_limits_of_block_5_are_the_ones_of_the_model_answers():
    # Дополнение B5 §4: пределы схем ответа модели — локаций и NPC ≤ 12, связей ≤ 24, предметов ≤ 20.
    assert (schemas.MAX_LOCATIONS, schemas.MAX_NPCS) == (12, 12)
    assert (schemas.MAX_RELATIONS, schemas.MAX_ITEMS) == (24, 20)


# --- лишние поля и ошибки формы тела ---


@pytest.mark.parametrize(
    ("tail", "body", "extra"),
    [
        (None, {"title": "Маяк", "owner_user_id": "dev-2"}, "owner_user_id"),
        ("sections/settings", {"version": 1, "data": settings_data(), "force": True}, "force"),
        ("locks", {"version": 1, "locks": [], "force": True}, "force"),
        ("wishes/world", {"version": 1, "text": "Пираты", "force": True}, "force"),
    ],
    ids=["create", "section", "locks", "wish"],
)
async def test_an_extra_field_in_any_body_is_422_invalid_data_and_writes_nothing(
    stand, tail, body, extra
):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = "/api/drafts" if tail is None else f"/api/drafts/{draft['id']}/{tail}"

    response = await stand.call(author, "PUT" if tail else "POST", path, json=body)

    found = assert_error(response, 422, "invalid_data")
    assert found["problems"] == [f"{extra}: лишнее поле"]
    assert await count_drafts(stand.db) == 1
    assert await stand.draft(author, draft["id"]) == draft


@pytest.mark.parametrize(
    ("section", "data"),
    [
        ("world", world_data() | {"climate": "сыро"}),
        ("world", many_regions(1) | {"regions": [many_regions(1)["regions"][0] | {"x": 1}]}),
        ("deities", [deities_data()[0] | {"power": 9}]),
        ("factions", [factions_data()[0] | {"members": 40}]),
        ("plot", plot_data() | {"twist": "Нет."}),
        ("plot", many_acts(1) | {"acts": [many_acts(1)["acts"][0] | {"x": 1}]}),
        ("locations", locations_data() | {"rooms": 4}),
        ("locations", map_with(climate="сыро")),
        ("locations", map_with(exits=[location_at(HARBOR)["exits"][0] | {"x": 1}])),
        ("locations", map_with(features=[location_at(HARBOR)["features"][0] | {"x": 1}])),
        (
            "locations",
            map_with(LIGHTHOUSE, secrets=[location_at(LIGHTHOUSE)["secrets"][0] | {"x": 1}]),
        ),
        ("locations", map_with(zones=[{"id": "here", "name": "здесь", "x": 1}])),
        ("locations", map_with(links=[{"a": "here", "b": "here", "distance": "near", "x": 1}])),
        (
            "locations",
            map_with(
                LIGHTHOUSE, features=[cupboard(reveals=[{"kind": "item", "id": "k", "x": 1}])]
            ),
        ),
        # Проверка детали и тайны — тоже уровень вложенности со своим запретом лишнего.
        ("locations", map_with(LIGHTHOUSE, features=[cupboard(check=CHECK | {"x": 1})])),
        (
            "locations",
            map_with(
                LIGHTHOUSE,
                secrets=[location_at(LIGHTHOUSE)["secrets"][0] | {"check": CHECK | {"x": 1}}],
            ),
        ),
        ("npcs", [npcs_data()[0] | {"x": 1}]),
        ("relations", [relations_data()[0] | {"x": 1}]),
        ("items", [items_data()[0] | {"x": 1}]),
    ],
)
async def test_an_extra_field_inside_a_section_is_422_invalid_data(stand, section, data):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert all(line.startswith(f"{section}") for line in body["problems"]), body
    assert any(line.endswith(": лишнее поле") for line in body["problems"]), body


@pytest.mark.parametrize(
    ("tail", "body"),
    [
        ("sections/settings", {"version": SECRET_INPUT, "data": settings_data()}),
        ("sections/settings", {"version": 1, "data": settings_data(party_size=SECRET_INPUT)}),
        ("sections/settings", {"version": 1, "data": settings_data(profanity=SECRET_INPUT)}),
        ("locks", {"version": 1, "locks": SECRET_INPUT}),
        ("wishes/world", {"version": 1, "text": [SECRET_INPUT]}),
    ],
)
async def test_problems_of_a_malformed_body_name_the_field_and_never_its_value(stand, tail, body):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(author, "PUT", f"/api/drafts/{draft['id']}/{tail}", json=body)

    found = assert_error(response, 422, "invalid_data")
    assert SECRET_INPUT not in response.text
    assert not any(line.startswith("body") for line in found["problems"]), found


@pytest.mark.parametrize("body", [[], SECRET_INPUT, 42])
@pytest.mark.parametrize("tail", [None, "sections/settings", "locks", "wishes/world"])
async def test_a_body_that_is_not_an_object_is_422_with_a_problem_without_an_empty_path(
    stand, tail, body
):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = "/api/drafts" if tail is None else f"/api/drafts/{draft['id']}/{tail}"

    response = await stand.call(author, "PUT" if tail else "POST", path, json=body)

    found = assert_error(response, 422, "invalid_data")
    assert SECRET_INPUT not in response.text
    assert all(line and not line.startswith(":") for line in found["problems"]), found
    assert await count_drafts(stand.db) == 1


# --- пожелания: границы длины, пустое удаляет ---


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Я" * 2000, "Я" * 2000),
        ("  " + "Я" * 2000 + "\n", "Я" * 2000),  # длина считается после обрезки
        ("🐉" * 2000, "🐉" * 2000),
    ],
)
async def test_a_wish_of_2000_characters_is_kept(stand, text, kept):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author, "PUT", f"/api/drafts/{draft['id']}/wishes/plot", json={"version": 1, "text": text}
    )

    assert response.status_code == 200, response.text
    assert response.json()["wishes"] == {"plot": kept}


@pytest.mark.parametrize("text", ["🐉" * 2001, " " + "Я" * 2001 + " "])
async def test_a_wish_past_2000_characters_is_refused(stand, text):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author, "PUT", f"/api/drafts/{draft['id']}/wishes/plot", json={"version": 1, "text": text}
    )

    assert_error(response, 422, "invalid_wish")
    assert text.strip()[:50] not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


@pytest.mark.parametrize("empty", ["", "\n\t  "])
async def test_an_empty_wish_removes_only_the_wish_of_its_step(stand, empty):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = f"/api/drafts/{draft['id']}/wishes"
    await stand.call(author, "PUT", f"{path}/world", json={"version": 1, "text": "Пираты"})
    await stand.call(author, "PUT", f"{path}/plot", json={"version": 2, "text": "Предательство"})

    response = await stand.call(author, "PUT", f"{path}/world", json={"version": 3, "text": empty})

    assert response.status_code == 200, response.text
    assert (response.json()["version"], response.json()["wishes"]) == (
        4,
        {"plot": "Предательство"},
    )


# --- закрепления: каждый вид из §6 и каждый шаг ---


# Виды ссылок закрепления по дополнению §6 записаны здесь, а не взяты из кода: это страж контракта.
LOCK_KINDS = (
    "deity",
    "faction",
    "npc",
    "location",
    "item",
    "encounter",
    "monster",
    "relation",
    "revelation",
)


async def test_every_kind_of_entity_and_every_step_may_be_locked(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    locks = [f"{kind}:some-id_1" for kind in LOCK_KINDS] + [f"step:{step}" for step in STEPS]

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": 1, "locks": [*locks, locks[0]]},  # повтор схлопывается
    )

    assert response.status_code == 200, response.text
    assert response.json()["locks"] == sorted(locks)


@pytest.mark.parametrize(
    "ref",
    [
        "deity:selmara\n",
        "step:world\n",
        "deity:" + "a" * 65,  # id длиннее 64 знаков
        "step:",
        "deity:",
        "Deity:selmara",
        "deity:selmara ",
        "step:world:extra",
        "adventure:crow",
    ],
)
async def test_a_lock_slightly_off_the_format_is_invalid_locks(stand, ref):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author, "PUT", f"/api/drafts/{draft['id']}/locks", json={"version": 1, "locks": [ref]}
    )

    body = assert_error(response, 422, "invalid_locks")
    assert body["problems"] == ["locks.0: нужна ссылка «<вид>:<id>» или «step:<шаг>»"]
    assert (await stand.draft(author, draft["id"]))["locks"] == []


# --- статусы шагов и время записи раздела ---


async def test_the_time_of_a_step_changes_only_when_its_own_section_is_written(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = f"/api/drafts/{draft['id']}"
    draft = (await stand.put_section(author, draft, "world", world_data())).json()
    world_time = draft["steps"]["world"]["updated_at"]

    draft = (await stand.put_section(author, draft, "deities", deities_data())).json()
    locks = {"version": 3, "locks": ["step:world"]}
    draft = (await stand.call(author, "PUT", f"{path}/locks", json=locks)).json()
    wish = {"version": 4, "text": "Шторм"}
    draft = (await stand.call(author, "PUT", f"{path}/wishes/world", json=wish)).json()

    assert draft["steps"]["world"]["updated_at"] == world_time
    assert isinstance(draft["steps"]["pantheon"]["updated_at"], str)
    untouched = [step for step in STEPS if step not in ("world", "pantheon")]
    assert all(draft["steps"][step]["updated_at"] is None for step in untouched)
    rewritten = (await stand.put_section(author, draft, "world", world_data())).json()
    later = datetime.fromisoformat(rewritten["steps"]["world"]["updated_at"])
    assert later > datetime.fromisoformat(world_time)
    assert later.tzinfo is not None


async def test_validate_drops_findings_of_steps_not_written_yet(stand):
    # Фракции ссылаются на NPC и локацию, которых ещё нет: это работа пустых шагов, а не ошибка
    # написанного раздела. Проверка идёт по заполненным разделам (дополнение §8).
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = (await stand.put_section(author, draft, "factions", factions_data())).json()

    report = (await stand.call(author, "POST", f"/api/drafts/{draft['id']}/validate")).json()

    empty = {step for step, state in draft["steps"].items() if state["status"] == "empty"}
    assert draft["steps"]["factions"]["status"] == "ready"
    assert {"npcs", "locations"} <= empty
    assert all(f["step"] not in empty for f in report["errors"] + report["warnings"]), report
    assert report["errors"] == [] and report["readiness_percent"] == 0


# --- fix-раунд 1 блока 4: статус ручного Замысла, пределы, сохранение сгенерированного ---


async def test_a_concept_written_by_hand_is_a_draft_and_counts_for_readiness(stand):
    # Находка major reviewer B4: Замысел, написанный руками, — содержимое (дополнение §3).
    author = await stand.author()
    draft = await stand.new_draft(author)

    saved = (await stand.put_section(author, draft, "settings", settings_data())).json()
    report = (await stand.call(author, "POST", f"/api/drafts/{draft['id']}/validate")).json()

    assert saved["steps"]["concept"]["status"] == "draft"  # id приключения даёт только генерация
    assert report["readiness_percent"] == 14


async def test_an_empty_world_saved_by_hand_is_empty(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    saved = await stand.put_section(author, draft, "world", {"history": "", "regions": []})

    assert saved.json()["steps"]["world"]["status"] == "empty"


@pytest.mark.parametrize("section", ["settings", "world", "deities", "factions", "plot"])
async def test_a_generated_section_is_saved_back_unchanged(stand, section):
    # Инвариант дополнения §7: пределы PUT не строже схем ответа модели.
    step = {"settings": "concept", "deities": "pantheon"}.get(section, section)
    author = await stand.author()
    draft = await stand.new_draft(author)
    started = await stand.call(
        author, "POST", f"/api/drafts/{draft['id']}/generate", json={"step": step}
    )
    assert started.status_code == 202, started.text
    await stand.app.state.authoring_tasks.join()
    generated = await stand.draft(author, draft["id"])
    data = generated[section]
    if section == "settings":
        data = data | {"title": generated["title"], "journal_intro": generated["journal_intro"]}

    response = await stand.put_section(author, generated, section, data)

    assert response.status_code == 200, response.text
    assert response.json()[section] == generated[section]
    if section == "settings":
        assert response.json()["steps"]["concept"]["status"] == "ready"


async def test_at_most_256_locks_are_kept(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    path = f"/api/drafts/{draft['id']}/locks"
    refs = [f"deity:d{n}" for n in range(257)]

    refused = await stand.call(author, "PUT", path, json={"version": 1, "locks": refs})
    kept = await stand.call(author, "PUT", path, json={"version": 1, "locks": refs[:256]})

    body = assert_error(refused, 422, "invalid_locks")
    assert body["problems"] == ["locks: не больше 256 ссылок"]
    assert kept.status_code == 200, kept.text
    assert len(kept.json()["locks"]) == 256


async def test_problems_of_a_malformed_body_are_in_russian(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/sections/settings",
        json={"version": "один", "data": settings_data(), "force": True},
    )

    body = assert_error(response, 422, "invalid_data")
    assert sorted(body["problems"]) == ["force: лишнее поле", "version: нужно целое число"]


# --- разделы блока 5: карта, NPC, связи, предметы (дополнение B5 §1, §3–§5, §7) ---

B5_SECTIONS = ("locations", "npcs", "relations", "items")
B5_STEPS = ("locations", "location_contents", "npcs", "relations")


async def validate(stand: Stand, who: Author, draft: dict[str, Any]) -> dict[str, Any]:
    response = await stand.call(who, "POST", f"/api/drafts/{draft['id']}/validate")
    assert response.status_code == 200, response.text
    return response.json()


def findings(report: dict[str, Any]) -> set[tuple[str, str, str | None]]:
    """Находки проверки как (код, ссылка, шаг)."""
    return {(f["code"], f["ref"], f["step"]) for f in report["errors"] + report["warnings"]}


async def lock(stand: Stand, who: Author, draft: dict[str, Any], *refs: str) -> dict[str, Any]:
    response = await stand.call(
        who,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": draft["version"], "locks": list(refs)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def saved(stand: Stand, who: Author, draft: dict[str, Any], section: str, data: Any) -> dict:
    response = await stand.put_section(who, draft, section, data)
    assert response.status_code == 200, response.text
    return response.json()


async def put_draft(stand: Stand, who: Author, content: AuthoringDraft) -> dict[str, Any]:
    """Черновик автора с готовым содержимым `content` прямо в базе (как его пишет задание
    генерации) — то, что автор затем читает и правит через API."""
    draft = await stand.new_draft(who)
    async with stand.db() as session:
        row = await session.get(AdventureDraft, draft["id"])
        service.put_into(row, content)
        await session.commit()
    return await stand.draft(who, draft["id"])


async def complete_draft(stand: Stand, who: Author) -> dict[str, Any]:
    """Черновик автора со всеми разделами демо v2 (боги, фракции, NPC, карта, предметы, бои): так
    в черновике есть и то, что конструктор B5 сам не пишет (бои, свои статблоки, открытия)."""
    complete = (await generate(FakeProvider(load_scenario(V2_SCENARIO)), v2_draft())).draft
    return await put_draft(stand, who, complete)


@pytest.mark.parametrize("section", B5_SECTIONS)
async def test_each_section_of_block_5_is_saved_and_read_back_as_given(stand, section):
    author = await stand.author()
    draft = await stand.new_draft(author)
    data = section_data(section)

    response = await stand.put_section(author, draft, section, data)

    assert response.status_code == 200, response.text
    written = response.json()
    assert_draft_shape(written)
    assert written["version"] == 2
    # Сервер принимает назад всё, что отдаёт: поля по умолчанию (зоны, связи зон, бой) не теряются.
    assert served_section(written, section) == data
    assert await stand.draft(author, draft["id"]) == written
    again = await stand.put_section(author, written, section, served_section(written, section))
    assert again.status_code == 200, again.text
    assert served_section(again.json(), section) == data


async def test_the_map_writes_the_start_and_the_finale_of_the_draft(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "locations", locations_data())
    cleared = await saved(
        stand,
        author,
        written,
        "locations",
        locations_data() | {"start_location": None, "finale_location": None},
    )

    assert (written["start_location"], written["finale_location"]) == ("harbor", "lamp-room")
    assert (cleared["start_location"], cleared["finale_location"]) == (None, None)
    assert [loc["id"] for loc in cleared["locations"]] == [
        "harbor",
        "cliff-path",
        "lighthouse",
        "lamp-room",
    ]


@pytest.mark.parametrize("field", ["start_location", "finale_location"])
async def test_a_start_or_finale_outside_the_map_is_422_without_the_value(stand, field):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(
        author, draft, "locations", locations_data() | {field: SECRET_INPUT}
    )

    body = assert_error(response, 422, "invalid_data")
    assert body["problems"] == [f"locations.{field}: такой локации на карте нет"]
    assert SECRET_INPUT not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


@pytest.mark.parametrize(
    ("section", "data", "where"),
    [
        ("locations", map_with(name=None, exits=SECRET_INPUT), "locations.locations.0."),
        ("locations", {"locations": SECRET_INPUT}, "locations."),
        ("npcs", [npcs_data()[0] | {"knows": SECRET_INPUT, "attitude": 9}], "npcs.0."),
        ("relations", [relations_data()[0] | {"kind": SECRET_INPUT}], "relations.0."),
        ("items", [items_data()[0] | {"holder": None, "name": [SECRET_INPUT]}], "items.0."),
    ],
)
async def test_a_section_of_block_5_breaking_the_schema_is_422_without_input_values(
    stand, section, data, where
):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert all(line.startswith(where) for line in body["problems"]), body
    assert SECRET_INPUT not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


async def test_relations_inside_an_npc_are_refused_with_a_hint_to_the_relations_step(stand):
    # Дополнение B5 §1: связи NPC формата v1 в конструкторе — только пустой список.
    author = await stand.author()
    draft = await stand.new_draft(author)
    npc = npcs_data()[1] | {"relations": [{"npc_id": "veran", "kind": "debtor"}]}

    response = await stand.put_section(author, draft, "npcs", [npc])

    body = assert_error(response, 422, "invalid_data")
    assert body["problems"] == ["npcs.0.relations: связи NPC — на шаге «Связи»"]
    assert (await stand.draft(author, draft["id"]))["relations"] == []


async def test_a_location_without_exits_is_accepted(stand):
    # Дополнение B5 §4: минимум выходов на границе API не требуется.
    author = await stand.author()
    draft = await stand.new_draft(author)
    lonely = location_at(HARBOR) | {"exits": []}

    written = await saved(
        stand,
        author,
        draft,
        "locations",
        {"locations": [lonely], "start_location": "harbor", "finale_location": "harbor"},
    )

    assert written["locations"] == [lonely]


def map_with_a_repeated_location() -> dict[str, Any]:
    data = locations_data()
    data["locations"][CLIFF_PATH]["id"] = "harbor"
    return data


def with_the_first_repeated(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*entities, entities[0]]


@pytest.mark.parametrize(
    ("section", "data", "ident"),
    [
        ("locations", map_with_a_repeated_location(), "harbor"),
        ("npcs", with_the_first_repeated(npcs_data()), "ilsa"),
        ("relations", with_the_first_repeated(relations_data()), "ilsa-worships-selmara"),
        ("items", with_the_first_repeated(items_data()), "lamp-key"),
    ],
)
async def test_repeated_ids_in_a_section_of_block_5_are_refused(stand, section, data, ident):
    # Дополнение B5 §4: повтор id в верхнем списке раздела — отказ записи, как в B4.
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert any(section in line and ident in line for line in body["problems"]), body
    assert await stand.draft(author, draft["id"]) == draft


@pytest.mark.parametrize(
    ("data", "ref", "step"),
    [
        (
            map_with(features=[location_at(CLIFF_PATH)["features"][0]]),
            "location:cliff-path/feature:ash-marks",
            "location_contents",
        ),
        (
            map_with(exits=[location_at(HARBOR)["exits"][0]] * 2),
            "location:harbor/exit:to-cliff-path",
            "locations",
        ),
        (
            map_with(LIGHTHOUSE, secrets=[location_at(LIGHTHOUSE)["secrets"][0]] * 2),
            "location:lighthouse/secret:drowned-keeper",
            "location_contents",
        ),
    ],
    ids=["feature", "exit", "secret"],
)
async def test_a_repeated_id_inside_a_location_is_accepted_and_shown_by_the_check(
    stand, data, ref, step
):
    # Дополнение B5 §4: повтор id вложенных сущностей виден в проверке, а не на записи.
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "locations", data)

    report = await validate(stand, author, written)
    assert ("schema", ref, step) in findings(report)


async def test_an_item_in_a_cache_saved_before_the_map_is_linked_to_its_feature(stand):
    # Предмет «в тайнике»: держатель — локация места, деталь раскрывает предмет (дополнение B5 §1);
    # webapp пишет сначала `items`, потом `locations`.
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "items", items_data())

    draft = await saved(stand, author, draft, "locations", locations_data())

    assert draft["items"][0]["holder"] == "location:lighthouse"
    assert cupboard() in draft["locations"][LIGHTHOUSE]["features"]
    assert cupboard()["reveals"] == [{"kind": "item", "id": "lamp-key", "value": None}]
    report = await validate(stand, author, draft)
    cache = "location:lighthouse/feature:keeper-cupboard"
    assert all(ref != cache for _, ref, _ in findings(report)), report
    assert all("lamp-key" not in f["message"] for f in report["errors"]), report


async def test_an_item_in_a_cache_of_another_location_is_a_dangling_ref_of_the_filling(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    items = items_data()
    items[0]["holder"] = "location:harbor"  # ключ от фонарной лежит в гавани, а не в шкафу
    draft = await saved(stand, author, draft, "items", items)

    draft = await saved(stand, author, draft, "locations", locations_data())

    report = await validate(stand, author, draft)
    cache = "location:lighthouse/feature:keeper-cupboard"
    assert ("dangling_ref", cache, "location_contents") in findings(report)


async def test_an_encounter_of_a_location_that_does_not_exist_is_kept_and_shown_by_the_check(
    stand,
):
    # Дополнение B5 §1: `encounter_id` без такого боя принимается и виден в проверке как висящая
    # ссылка — в черновике, где бои уже есть (их шаг не пуст).
    author = await stand.author()
    draft = await complete_draft(stand, author)
    data = served_section(draft, "locations")
    data["locations"][HARBOR]["encounter_id"] = "ghost-fight"

    written = await saved(stand, author, draft, "locations", data)

    assert written["locations"][HARBOR]["encounter_id"] == "ghost-fight"
    assert written["encounters"] == draft["encounters"]
    report = await validate(stand, author, written)
    assert ("dangling_ref", "location:harbor", "encounters") in findings(report)


async def test_deleting_a_locked_location_unlocks_it_and_leaves_the_exit_to_the_check(stand):
    # Дополнение B5 §5: удаление — запись раздела без сущности; её замок снимается в той же записи,
    # закрепления шага и чужих сущностей остаются, ссылки на удалённое — находки проверки.
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "locations", locations_data())
    draft = await lock(
        stand, author, draft, "location:lamp-room", "location:harbor", "step:locations", "npc:ilsa"
    )
    without = locations_data()
    del without["locations"][LAMP_ROOM]
    without["finale_location"] = "lighthouse"

    deleted = await saved(stand, author, draft, "locations", without)

    assert deleted["version"] == draft["version"] + 1
    assert deleted["locks"] == ["location:harbor", "npc:ilsa", "step:locations"]
    report = await validate(stand, author, deleted)
    assert ("dangling_ref", "location:lighthouse/exit:to-lamp-room", "locations") in findings(
        report
    )
    # Новая локация с id удалённой не закреплена.
    restored = await saved(stand, author, deleted, "locations", locations_data())
    assert restored["locks"] == ["location:harbor", "npc:ilsa", "step:locations"]


@pytest.mark.parametrize(
    ("section", "kind", "step", "foreign"),
    [
        ("npcs", "npc", "npcs", "location:harbor"),
        ("relations", "relation", "relations", "npc:ilsa"),
        ("items", "item", "items", "location:harbor"),
        ("deities", "deity", "pantheon", "faction:smugglers"),
        ("factions", "faction", "factions", "deity:selmara"),
    ],
)
async def test_deleting_an_entity_unlocks_only_that_entity(stand, section, kind, step, foreign):
    # Замок шага, замок сущности другого раздела и замок id, которого в разделе не было, — не замки
    # удалённой сущности: запись раздела их не трогает (дополнение B5 §5).
    author = await stand.author()
    draft = await stand.new_draft(author)
    first, second, *rest = section_data(section)
    draft = await saved(stand, author, draft, section, [first, second, *rest])
    kept = sorted([f"{kind}:{second['id']}", f"step:{step}", foreign, f"{kind}:never-written"])
    draft = await lock(stand, author, draft, f"{kind}:{first['id']}", *kept)

    deleted = await saved(stand, author, draft, section, [second, *rest])

    assert deleted["locks"] == kept
    restored = await saved(stand, author, deleted, section, [first, second, *rest])
    assert restored["locks"] == kept


async def test_the_steps_of_block_5_take_their_status_from_the_content(stand):
    # Дополнение B5 §3: карта без наполнения — наполнение пусто; NPC и связи — по наличию.
    author = await stand.author()
    draft = await stand.new_draft(author)
    graph = locations_data()
    for location in graph["locations"]:
        location |= {"read_aloud": "", "dm_notes": "", "features": [], "secrets": []}

    bare = await saved(stand, author, draft, "locations", graph)
    filled = await saved(stand, author, bare, "locations", locations_data())
    with_npcs = await saved(stand, author, filled, "npcs", npcs_data())
    linked = await saved(stand, author, with_npcs, "relations", relations_data())

    assert {step: draft["steps"][step]["status"] for step in B5_STEPS} == dict.fromkeys(
        B5_STEPS, "empty"
    )
    assert bare["steps"]["locations"]["status"] == "ready"
    assert bare["steps"]["location_contents"]["status"] == "empty"
    assert filled["steps"]["location_contents"]["status"] == "ready"
    assert with_npcs["steps"]["npcs"]["status"] != "empty"
    assert linked["steps"]["relations"]["status"] != "empty"
    # Ручная запись карты — запись раздела обоих шагов, которые его пишут.
    for step in ("locations", "location_contents"):
        assert isinstance(bare["steps"][step]["updated_at"], str)
    assert bare["steps"]["npcs"]["updated_at"] is None


@pytest.mark.parametrize("step", B5_STEPS)
async def test_a_wish_for_a_step_of_block_5_is_saved(stand, step):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/wishes/{step}",
        json={"version": 1, "text": "Больше тумана"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["wishes"] == {step: "Больше тумана"}


@pytest.mark.parametrize(
    ("steps", "section"),
    [
        (["locations"], "locations"),
        (["locations", "location_contents"], "locations"),
        (["npcs"], "npcs"),
        (["relations"], "relations"),
    ],
    ids=["locations", "location_contents", "npcs", "relations"],
)
async def test_a_generated_section_of_block_5_is_saved_back_unchanged(stand, steps, section):
    # Инвариант дополнения B5 §4: всё, что даёт генерация, сохраняется обратно без правки.
    author = await stand.author()
    draft = await stand.new_draft(author)
    for step in steps:
        started = await stand.call(
            author, "POST", f"/api/drafts/{draft['id']}/generate", json={"step": step}
        )
        assert started.status_code == 202, started.text
        await stand.app.state.authoring_tasks.join()
    generated = await stand.draft(author, draft["id"])
    assert generated["steps"][steps[-1]]["status"] != "empty"

    response = await stand.put_section(
        author, generated, section, served_section(generated, section)
    )

    assert response.status_code == 200, response.text
    assert served_section(response.json(), section) == served_section(generated, section)


async def test_a_draft_written_before_block_5_is_read_and_edited(stand):
    # carry_over B4: в JSON черновика нет номера схемы — черновик в формате B4 (без разделов B5)
    # читается и правится после изменений B5.
    author = await stand.author()
    draft = await stand.new_draft(author)
    b4_data = {
        "id": None,
        "title": "Старый черновик",
        "journal_intro": "",
        "settings": settings_data(),
        "world": None,
        "plot": {"acts": [], "endings": [], "villain": None},
        "deities": deities_data(),
        "factions": [],
        "locks": ["deity:selmara"],
        "wishes": {"world": "Пираты"},
    }
    del b4_data["settings"]["title"], b4_data["settings"]["journal_intro"]
    async with stand.db() as session:
        row = await session.get(AdventureDraft, draft["id"])
        row.data = b4_data
        await session.commit()

    read = await stand.draft(author, draft["id"])
    written = await saved(stand, author, read, "locations", locations_data())

    assert_draft_shape(read)
    assert read["title"] == "Старый черновик" and read["locks"] == ["deity:selmara"]
    for section in ("locations", "npcs", "relations", "items", "encounters"):
        assert read[section] == []
    assert (read["start_location"], read["finale_location"]) == (None, None)
    assert written["deities"] == read["deities"] and written["wishes"] == {"world": "Пираты"}


# --- финальное покрытие блока 5: формы сущностей, заполненные черновики, границы, удаление ---


# Поля сущностей B5 в `Draft` (дополнение B5 §1) записаны здесь, а не взяты из моделей движка: это
# страж контракта webapp (`services/webapp/src/api/types.ts`).
JOURNAL_FIELDS = {"public", "known_at_start"}
LOCATION_FIELDS = JOURNAL_FIELDS | {
    "id",
    "name",
    "region",
    "read_aloud",
    "dm_notes",
    "exits",
    "features",
    "secrets",
    "encounter_id",
    "zones",
    "links",
}
EXIT_FIELDS = {"id", "to", "direction", "description", "locked_by", "hidden_dc"}
FEATURE_FIELDS = {"id", "name", "description", "check", "reveals", "hidden", "locked_by"}
SECRET_FIELDS = {"id", "text", "check", "reveals"}
CHECK_FIELDS = {"ability", "skill", "dc"}
REVEAL_FIELDS = {"kind", "id", "value"}
ZONE_FIELDS = {"id", "name"}
LINK_FIELDS = {"a", "b", "distance"}
NPC_FIELDS = JOURNAL_FIELDS | {
    "id",
    "name",
    "role",
    "appearance",
    "manner",
    "motive",
    "secret",
    "knows",
    "attitude",
    "relations",
    "location_id",
    "statblock",
}
RELATION_FIELDS = {"id", "a", "b", "kind", "note", "secret", "mutual"}
ITEM_FIELDS = JOURNAL_FIELDS | {"id", "name", "description", "base_item", "holder"}


async def test_entities_of_block_5_in_the_draft_have_exactly_the_fields_of_the_contract(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    zoned = map_with(
        zones=[{"id": "here", "name": "здесь"}, {"id": "pier", "name": "причал"}],
        links=[{"a": "here", "b": "pier", "distance": "far"}],
    )
    for section, data in [
        ("items", items_data()),
        ("locations", zoned),
        ("npcs", npcs_data()),
        ("relations", relations_data()),
    ]:
        draft = await saved(stand, author, draft, section, data)

    locations = draft["locations"]
    parts = [part for loc in locations for part in (*loc["features"], *loc["secrets"])]
    shapes = [
        (LOCATION_FIELDS, locations),
        (EXIT_FIELDS, [e for loc in locations for e in loc["exits"]]),
        (FEATURE_FIELDS, [f for loc in locations for f in loc["features"]]),
        (SECRET_FIELDS, [s for loc in locations for s in loc["secrets"]]),
        (CHECK_FIELDS, [part["check"] for part in parts if part["check"] is not None]),
        (REVEAL_FIELDS, [r for part in parts for r in part["reveals"]]),
        (ZONE_FIELDS, [z for loc in locations for z in loc["zones"]]),
        (LINK_FIELDS, [link for loc in locations for link in loc["links"]]),
        (NPC_FIELDS, draft["npcs"]),
        (RELATION_FIELDS, draft["relations"]),
        (ITEM_FIELDS, draft["items"]),
    ]
    # Каждая форма есть в ответе хотя бы раз: страж не проходит на пустых списках.
    for fields, entities in shapes:
        assert entities and all(set(entity) == fields for entity in entities), sorted(fields)
    # Связи NPC формата v1 в конструкторе — всегда пустой список (дополнение B5 §1).
    assert all(npc["relations"] == [] for npc in draft["npcs"])


@pytest.mark.parametrize(
    ("section", "data"),
    [
        ("locations", locations_data()["locations"]),  # «Карта» — объект, а не список
        ("npcs", {"npcs": npcs_data()}),  # разделы-списки — списки
        ("relations", relations_data()[0]),
        ("items", None),
    ],
)
async def test_a_section_of_block_5_in_another_shape_is_422_invalid_data(stand, section, data):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert all(line.startswith(section) for line in body["problems"]), body
    assert (await stand.draft(author, draft["id"]))["version"] == 1


# Готовые примеры автора из репозитория: читаются по пути, а не копией.
CONTENT_DIR = Path(__file__).resolve().parents[4] / "content"
EXAMPLE_SEEDS = CONTENT_DIR / "examples" / "seeds_v2.yaml"
DEMO_CRYPT = CONTENT_DIR / "adventures" / "demo-crypt"


async def generated_draft() -> AuthoringDraft:
    """Черновик, целиком наполненный генерацией демо v2: все шаги на FakeProvider."""
    return (await generate(FakeProvider(load_scenario(V2_SCENARIO)), v2_draft())).draft


async def seeded_draft() -> AuthoringDraft:
    """Черновик CLI с заготовками примера `seeds_v2.yaml` (закреплены), дописанный генерацией."""
    order = seeded(v2_draft(), cli._load_seeds(EXAMPLE_SEEDS))
    return (await generate(FakeProvider(load_scenario(V2_SCENARIO)), order)).draft


async def demo_crypt_draft() -> AuthoringDraft:
    """Черновик из папки готового приключения `demo-crypt`: связи NPC формата v1 уже в графе."""
    return from_adventure(load_adventure(DEMO_CRYPT))


FILLED_DRAFTS = {
    "generated": generated_draft,
    "seeded": seeded_draft,
    "demo-crypt": demo_crypt_draft,
}


def without_times(draft: dict[str, Any]) -> dict[str, Any]:
    """`Draft` без версии и времени записи шагов: запись тех же данных не меняет остального."""
    statuses = {step: state["status"] for step, state in draft["steps"].items()}
    return draft | {"version": None, "steps": statuses}


@pytest.mark.parametrize("source", FILLED_DRAFTS)
async def test_every_section_of_a_filled_draft_is_saved_back_unchanged(stand, source):
    # Инвариант дополнений B4 §7 и B5 §1, §4: сервер принимает назад всё, что сам отдаёт, — и в
    # черновике из генерации всех шагов, из сидов CLI и из папки готового приключения. Запись тех
    # же данных не снимает замков (сущности не удалены) и не меняет статусов.
    author = await stand.author()
    draft = await put_draft(stand, author, await FILLED_DRAFTS[source]())
    assert all(draft[section] for section in SECTIONS if section != "settings"), source

    for section in SECTIONS:
        response = await stand.put_section(author, draft, section, served_section(draft, section))
        assert response.status_code == 200, (section, response.text)
        written = response.json()
        assert written["version"] == draft["version"] + 1, section
        assert without_times(written) == without_times(draft), section
        draft = written


async def test_a_fully_generated_draft_is_ready_on_every_step_and_in_the_check(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    report = await validate(stand, author, draft)

    statuses = {step: state["status"] for step, state in draft["steps"].items()}
    assert statuses == dict.fromkeys(STEPS, "ready")
    assert (report["errors"], report["readiness_percent"]) == ([], 100)


DRAGONS = "🐉"


@pytest.mark.parametrize(
    ("section", "write", "limit"),
    [
        ("locations", lambda text: map_with(name=text), 100),
        ("locations", lambda text: map_with(read_aloud=text), 2000),
        (
            "locations",
            lambda text: map_with(exits=[location_at(HARBOR)["exits"][0] | {"description": text}]),
            300,
        ),
        ("npcs", lambda text: [npcs_data()[0] | {"knows": [text]}], 300),
        ("relations", lambda text: [relations_data()[0] | {"note": text}], 300),
        ("items", lambda text: [items_data()[0] | {"description": text}], 2000),
    ],
    ids=["location-name", "read-aloud", "exit", "knows", "relation-note", "item-description"],
)
async def test_texts_of_block_5_are_counted_in_characters_up_to_the_limit(
    stand, section, write, limit
):
    # Знак вне базовой плоскости — один знак, а не четыре байта (как у Замысла в B4).
    author = await stand.author()
    draft = await stand.new_draft(author)

    at_limit = await stand.put_section(author, draft, section, write(DRAGONS * limit))
    assert at_limit.status_code == 200, at_limit.text
    over = await stand.put_section(author, at_limit.json(), section, write(DRAGONS * (limit + 1)))

    assert served_section(at_limit.json(), section) == write(DRAGONS * limit)
    body = assert_error(over, 422, "invalid_data")
    assert any(line.endswith(f": не длиннее {limit} знаков") for line in body["problems"]), body
    assert DRAGONS * 50 not in over.text
    assert await stand.draft(author, draft["id"]) == at_limit.json()


def with_feature_dc(dc: int) -> dict[str, Any]:
    """«Карта» демо, где Сл проверки журнала смотрителя — `dc`."""
    data = locations_data()
    data["locations"][LIGHTHOUSE]["features"][0]["check"]["dc"] = dc
    return data


def with_secret_dc(dc: int) -> dict[str, Any]:
    """«Карта» демо, где Сл проверки тайны комнат смотрителя — `dc`."""
    data = locations_data()
    data["locations"][LIGHTHOUSE]["secrets"][0]["check"]["dc"] = dc
    return data


def with_hidden_exit(dc: int) -> dict[str, Any]:
    """«Карта» демо, где выход из комнат смотрителя скрыт со Сл поиска `dc`."""
    data = locations_data()
    data["locations"][LIGHTHOUSE]["exits"][0]["hidden_dc"] = dc
    return data


@pytest.mark.parametrize("dc", [5, 10, 15, 20, 25, 30])
async def test_every_difficulty_of_the_table_is_accepted_in_the_checks_of_a_location(stand, dc):
    author = await stand.author()
    draft = await stand.new_draft(author)

    for build in (with_feature_dc, with_secret_dc, with_hidden_exit):
        draft = await saved(stand, author, draft, "locations", build(dc))
        assert served_section(draft, "locations") == build(dc), build.__name__


@pytest.mark.parametrize("dc", [0, 12, 31, 1234567])
@pytest.mark.parametrize(
    ("build", "path"),
    [
        (with_feature_dc, "features.0.check.dc"),
        (with_secret_dc, "secrets.0.check.dc"),
        (with_hidden_exit, "exits.0.hidden_dc"),
    ],
    ids=["feature", "secret", "hidden-exit"],
)
async def test_a_difficulty_off_the_table_is_refused_in_every_check_of_a_location(
    stand, build, path, dc
):
    # Инвариант проекта: Сл — только из таблицы 5/10/15/20/25/30, и ручной ввод его не обходит.
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, "locations", build(dc))

    body = assert_error(response, 422, "invalid_data")
    # Дополнение B5 §9 (security I1): правило таблицы, а не введённое число.
    assert body["problems"] == [
        f"locations.locations.{LIGHTHOUSE}.{path}: Сл — одна из 5, 10, 15, 20, 25, 30"
    ]
    assert "получено" not in response.text and "1234567" not in response.text
    assert (await stand.draft(author, draft["id"]))["version"] == 1


def without(draft: dict[str, Any], section: str, ident: str) -> Any:
    """Данные раздела `section` из `Draft` без сущности `ident`: так автор удаляет сущность."""
    data = served_section(draft, section)
    if section == "locations":
        return data | {"locations": [loc for loc in data["locations"] if loc["id"] != ident]}
    return [entity for entity in data if entity["id"] != ident]


@pytest.mark.parametrize(
    ("section", "ident", "referrer", "finding"),
    [
        (
            "locations",
            "cliff-path",
            ("locations", "harbor"),
            ("dangling_ref", "location:harbor/exit:to-cliff-path", "locations"),
        ),
        ("locations", "cliff-path", ("npcs", "bram"), ("dangling_ref", "npc:bram", "npcs")),
        (
            "npcs",
            "ilsa",
            ("relations", "bram-rival-ilsa"),
            ("dangling_ref", "relation:bram-rival-ilsa", "relations"),
        ),
        (
            "deities",
            "selmara",
            ("relations", "ilsa-worships-selmara"),
            ("dangling_ref", "relation:ilsa-worships-selmara", "relations"),
        ),
        ("npcs", "veran", ("items", "ash-tome"), ("dangling_ref", "item:ash-tome", "items")),
        (
            "locations",
            "lighthouse",
            ("items", "lamp-key"),
            ("dangling_ref", "item:lamp-key", "items"),
        ),
    ],
    ids=[
        "exit-to-deleted-location",
        "npc-in-deleted-location",
        "relation-with-deleted-npc",
        "relation-with-deleted-god",
        "item-of-deleted-npc",
        "item-in-deleted-location",
    ],
)
async def test_deleting_an_entity_keeps_what_refers_to_it_and_the_check_names_the_dangling_ref(
    stand, section, ident, referrer, finding
):
    # Дополнение B5 §5: ссылающиеся сущности не удаляются, висящая ссылка — находка проверки со
    # своим шагом.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    assert finding not in findings(await validate(stand, author, draft))
    referrer_section, referrer_id = referrer

    deleted = await saved(stand, author, draft, section, without(draft, section, ident))

    assert ident not in {entity["id"] for entity in deleted[section]}
    before = {entity["id"]: entity for entity in draft[referrer_section]}
    after = {entity["id"]: entity for entity in deleted[referrer_section]}
    assert after[referrer_id] == before[referrer_id]  # ссылающееся осталось как было
    assert finding in findings(await validate(stand, author, deleted))


@pytest.mark.parametrize(
    ("field", "index", "ident"),
    [("start_location", HARBOR, "harbor"), ("finale_location", LAMP_ROOM, "lamp-room")],
)
async def test_the_start_or_finale_location_is_deleted_only_with_its_field_moved(
    stand, field, index, ident
):
    # Фактическое поведение: старт и финал сами не сбрасываются. Карта без локации, на которую они
    # указывают, — 422 без записи; с полем, переведённым в null, она записывается, замок удалённой
    # локации снимается, а проверка говорит, что старта (финала) нет.
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "locations", locations_data())
    draft = await lock(stand, author, draft, f"location:{ident}")
    data = locations_data()
    del data["locations"][index]

    refused = await stand.put_section(author, draft, "locations", data)
    body = assert_error(refused, 422, "invalid_data")
    assert body["problems"] == [f"locations.{field}: такой локации на карте нет"]
    assert await stand.draft(author, draft["id"]) == draft

    moved = await saved(stand, author, draft, "locations", data | {field: None})

    assert moved[field] is None and moved["locks"] == []
    assert moved["steps"]["locations"]["status"] == "draft"
    report = await validate(stand, author, moved)
    # Текст находки — понятный автору (дополнение B6 §3), а не фраза движка о ссылке в никуда.
    text = {"start_location": "Старт не выбран", "finale_location": "Финал не выбран"}[field]
    assert any(
        (f["code"], f["ref"], f["step"], f["message"])
        == ("dangling_ref", "adventure", "locations", text)
        for f in report["errors"]
    ), report


async def test_a_lock_and_a_deletion_of_its_entity_at_once_let_exactly_one_through(stand):
    # Замки и раздел пишутся по одной версии черновика: закрепление из одной вкладки и удаление той
    # же сущности из другой не оставят замка у сущности, которой нет, — проходит ровно одна запись.
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(stand, author, draft, "npcs", npcs_data())
    path = f"/api/drafts/{draft['id']}/locks"

    locked, deleted = await asyncio.gather(
        stand.call(author, "PUT", path, json={"version": draft["version"], "locks": ["npc:ilsa"]}),
        stand.put_section(author, draft, "npcs", without(draft, "npcs", "ilsa")),
    )

    statuses = sorted([locked.status_code, deleted.status_code])
    assert statuses == [200, 409], (locked.text, deleted.text)
    winner, loser = (locked, deleted) if locked.status_code == 200 else (deleted, locked)
    assert_error(loser, 409, "version_conflict")
    final = await stand.draft(author, draft["id"])
    assert final == winner.json() and final["version"] == draft["version"] + 1
    ilsa_kept = "ilsa" in {npc["id"] for npc in final["npcs"]}
    assert ilsa_kept == ("npc:ilsa" in final["locks"])
