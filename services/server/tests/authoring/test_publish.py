"""Публикация черновика в каталог (план B6.1; спека §10.2, §10.3; дополнение dispatcher B6 §2, §6).

`POST /api/drafts/{id}/publish` проверяет черновик полным списком ошибок `pipeline.check` (без
фильтра пустых шагов), пишет строку `authored_adventures` и кладёт приключение в каталог на месте —
в тот же словарь, что держат маршруты игр и бот, без перезапуска. При старте сервера опубликованное
читается в тот же каталог, папка побеждает при совпадении id. Игры копируют приключение при
создании, поэтому новая публикация того же id их не трогает. Живых моделей нет.
"""

import asyncio
import logging
import shutil
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from test_archive import _draft_of as draft_of
from test_archive import crowded_cupboard, cupboard_finds
from test_drafts_api import (
    CREATOR_TG_ID,
    Author,
    Stand,
    assert_error,
    complete_draft,
    database_file,
    generated_draft,
    put_draft,
    saved,
    served_section,
    validate,
)
from test_jobs import generate

from dnd_engine.adventure.loader import adventure_from_data
from dnd_engine.content import load_srd
from dnd_server import app as app_module
from dnd_server.adventures import load_catalog
from dnd_server.app import create_app
from dnd_server.authoring import jobs, publish, service
from dnd_server.authoring.draft import to_adventure
from dnd_server.authoring.pipeline import check
from dnd_server.bot.handlers import newgame_keyboard
from dnd_server.bot.notify import Notifier
from dnd_server.config import Settings
from dnd_server.db.models import AuthoredAdventure, AuthoringJob, Game, User

DEMO_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
ADVENTURE_ID = "crow-lighthouse"  # id замысла демо v2
PUBLISHED_FIELDS = {"adventure_id", "published_at"}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """Настройки стенда черновиков (`conftest.py` папки) и папка контента с демо-приключением."""
    content = tmp_path / "content"
    shutil.copytree(DEMO_DIR, content / "demo-crypt")
    return settings.model_copy(update={"content_dir": content})


def with_folders(stand: Stand) -> None:
    """Каталог папок в том же словаре, что держит приложение (стенд без `lifespan`)."""
    stand.app.state.adventures.update(
        load_catalog(stand.app.state.settings.content_dir, load_srd())
    )


async def publish_draft(stand: Stand, who: Author, draft: dict[str, Any], **options: Any):
    options.setdefault("json", {})
    return await stand.call(who, "POST", f"/api/drafts/{draft['id']}/publish", **options)


async def rows(stand: Stand) -> list[AuthoredAdventure]:
    async with stand.db() as session:
        return list(await session.scalars(select(AuthoredAdventure)))


async def retitled(stand: Stand, who: Author, draft: dict[str, Any], title: str) -> dict:
    data = served_section(draft, "settings") | {"title": title}
    return await saved(stand, who, draft, "settings", data)


async def create_game(stand: Stand, who: Author, adventure_id: str) -> httpx.Response:
    return await stand.call(who, "POST", "/api/games", json={"adventure_id": adventure_id})


def texts_of(value: Any) -> list[str]:
    """Все строки внутри данных раздела `Draft` (вложенные объекты и списки)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in texts_of(item)]
    if isinstance(value, list):
        return [text for item in value for text in texts_of(item)]
    return []


# --- первая публикация ---


async def test_a_valid_draft_is_published_into_the_table_and_the_catalog_in_place(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    catalog = stand.app.state.adventures

    response = await publish_draft(stand, author, draft)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == PUBLISHED_FIELDS
    assert body["adventure_id"] == ADVENTURE_ID
    published_at = datetime.fromisoformat(body["published_at"])
    [row] = await rows(stand)
    assert (row.id, row.owner_user_id, row.source_draft_id) == (
        ADVENTURE_ID,
        author.user_id,
        draft["id"],
    )
    assert row.published_at.replace(tzinfo=None) == published_at.replace(tzinfo=None)
    expected = to_adventure(await generated_draft())
    assert adventure_from_data(row.data) == expected
    # Каталог — тот же словарь, что был у приложения: его держат маршруты игр и бот.
    assert stand.app.state.adventures is catalog
    entry = catalog[ADVENTURE_ID]
    assert entry.adventure == expected
    assert entry.report == check(expected, load_srd())


async def test_the_published_adventure_is_listed_without_secrets(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)

    response = await stand.call(player, "GET", "/api/adventures")

    assert response.status_code == 200, response.text
    [listed] = [a for a in response.json() if a["id"] == ADVENTURE_ID]
    assert set(listed) == {"id", "title", "pitch", "target_hours", "score", "blocking"}
    assert (listed["title"], listed["blocking"]) == (draft["title"], False)
    secrets = [npc["secret"] for npc in draft["npcs"]] + [
        location["dm_notes"] for location in draft["locations"] if location["dm_notes"]
    ]
    assert secrets and all(secret not in response.text for secret in secrets)
    # Финальное покрытие B6: ни один текст разделов (тайны NPC и мест, откровения и улики, тактика
    # боя, особенности своего статблока, цели фракций) не уходит в каталог — только название и
    # завязка.
    texts = [
        text
        for section in (
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
        )
        for text in texts_of(draft[section])
        if len(text) >= 12 and text not in (draft["title"], draft["settings"]["pitch"])
    ]
    assert len(texts) > 50
    assert [text for text in texts if text in response.text] == []


async def test_a_game_is_created_on_the_published_adventure_without_a_restart(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)

    response = await create_game(stand, author, ADVENTURE_ID)

    assert response.status_code == 201, response.text
    async with stand.db() as session:
        game = await session.get(Game, response.json()["id"])
    assert adventure_from_data(game.adventure_data) == to_adventure(await generated_draft())


# --- повторная публикация и занятый id ---


async def test_republishing_the_same_draft_updates_only_its_own_row(stand):
    author, other = await stand.author(), await stand.author()
    draft = await complete_draft(stand, author)
    first = (await publish_draft(stand, author, draft)).json()
    foreign = await complete_draft(stand, other)
    foreign = await saved(
        stand,
        other,
        foreign,
        "settings",
        served_section(foreign, "settings") | {"adventure_id": "other-lighthouse"},
    )
    assert (await publish_draft(stand, other, foreign)).status_code == 201
    draft = await retitled(stand, author, draft, "Маяк, второе издание")

    response = await publish_draft(stand, author, draft)

    assert response.status_code == 200, response.text
    assert response.json()["adventure_id"] == ADVENTURE_ID
    assert response.json()["published_at"] >= first["published_at"]
    by_id = {row.id: row for row in await rows(stand)}
    assert by_id[ADVENTURE_ID].data["title"] == "Маяк, второе издание"
    assert by_id["other-lighthouse"].data["title"] == foreign["title"]
    assert stand.app.state.adventures[ADVENTURE_ID].adventure.title == "Маяк, второе издание"
    assert stand.app.state.adventures["other-lighthouse"].adventure.title == foreign["title"]


async def test_another_authors_draft_cannot_take_a_published_id(stand):
    author, other = await stand.author(), await stand.author()
    await publish_draft(stand, author, await complete_draft(stand, author))
    rival = await complete_draft(stand, other)
    rival = await retitled(stand, other, rival, "Чужой маяк")

    response = await publish_draft(stand, other, rival)

    assert_error(response, 409, "id_taken")
    [row] = await rows(stand)
    assert row.owner_user_id == author.user_id
    assert stand.app.state.adventures[ADVENTURE_ID].adventure.title != "Чужой маяк"


async def test_another_draft_of_the_same_author_cannot_take_a_published_id(stand):
    author = await stand.author()
    await publish_draft(stand, author, await complete_draft(stand, author))
    second = await complete_draft(stand, author)

    response = await publish_draft(stand, author, second)

    assert_error(response, 409, "id_taken")
    assert len(await rows(stand)) == 1


async def test_the_id_of_a_content_folder_cannot_be_published(stand):
    author = await stand.author()
    with_folders(stand)
    draft = await complete_draft(stand, author)
    draft = await saved(
        stand,
        author,
        draft,
        "settings",
        served_section(draft, "settings") | {"adventure_id": "demo-crypt"},
    )
    folder = stand.app.state.adventures["demo-crypt"]

    response = await publish_draft(stand, author, draft)

    assert_error(response, 409, "id_taken")
    assert await rows(stand) == []
    assert stand.app.state.adventures["demo-crypt"] is folder


async def test_two_publishes_of_one_id_at_once_give_one_success_and_one_conflict(stand):
    author, other = await stand.author(), await stand.author()
    mine = await retitled(stand, author, await complete_draft(stand, author), "Мой маяк")
    theirs = await retitled(stand, other, await complete_draft(stand, other), "Их маяк")

    responses = await asyncio.gather(
        publish_draft(stand, author, mine), publish_draft(stand, other, theirs)
    )

    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    loser = next(r for r in responses if r.status_code == 409)
    assert_error(loser, 409, "id_taken")
    [row] = await rows(stand)
    winner = mine if row.owner_user_id == author.user_id else theirs
    assert row.source_draft_id == winner["id"]
    # Финальное покрытие B6: проигравший не перетёр каталог — там приключение победителя.
    assert row.data["title"] == winner["title"]
    assert stand.app.state.adventures[ADVENTURE_ID].adventure.title == winner["title"]


# --- отказы ---


async def test_a_draft_with_errors_is_not_published_and_the_problems_name_the_step(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    data = served_section(draft, "locations") | {"start_location": None}
    draft = await saved(stand, author, draft, "locations", data)

    response = await publish_draft(stand, author, draft)

    body = assert_error(response, 422, "blocking_errors")
    assert "locations: Старт не выбран" in body["problems"]
    assert await rows(stand) == []
    assert ADVENTURE_ID not in stand.app.state.adventures


async def test_an_error_hidden_by_the_empty_step_filter_still_blocks_the_publication(stand):
    # Дополнение §6, Q-L: «Проверка» показывает находки заполненных шагов, а публикация смотрит на
    # все ошибки. Откровения без единой улики — шаг улик пуст, его находки не видны в проверке.
    author = await stand.author()
    content = await generated_draft()
    bare = tuple(r.model_copy(update={"clues": ()}) for r in content.revelations)
    draft = await put_draft(stand, author, content.model_copy(update={"revelations": bare}))
    assert draft["steps"]["clues"]["status"] == "empty"
    assert all(
        f["code"] != "too_few_clues" for f in (await validate(stand, author, draft))["errors"]
    )

    response = await publish_draft(stand, author, draft)

    body = assert_error(response, 422, "blocking_errors")
    assert any(line.startswith("clues: ") for line in body["problems"]), body
    assert await rows(stand) == []


async def test_a_draft_without_an_adventure_id_is_not_published(stand):
    author = await stand.author()
    draft = await stand.new_draft(author, title="Без id")

    response = await publish_draft(stand, author, draft)

    assert_error(response, 422, "no_adventure_id")
    assert await rows(stand) == []


async def test_a_draft_with_a_running_job_is_not_published(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    async with stand.db() as session:
        session.add(AuthoringJob(draft_id=draft["id"], step="world"))
        await session.commit()

    response = await publish_draft(stand, author, draft)

    assert_error(response, 409, "job_running")
    assert await rows(stand) == []


async def test_someone_elses_draft_is_not_found_and_a_player_is_refused(stand):
    author, other = await stand.author(), await stand.author()
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)
    draft = await complete_draft(stand, author)

    assert_error(await publish_draft(stand, other, draft), 404, None)
    assert (await publish_draft(stand, player, draft)).status_code == 403
    assert await rows(stand) == []


async def test_publish_takes_an_empty_body_and_refuses_extra_fields(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    refused = await publish_draft(stand, author, draft, json={"adventure_id": "x"})
    without_body = await stand.call(author, "POST", f"/api/drafts/{draft['id']}/publish")

    assert_error(refused, 422, "invalid_data")
    assert without_body.status_code == 201, without_body.text


# --- id после публикации закреплён (дополнение §2) ---


async def test_the_adventure_id_of_a_published_draft_cannot_be_changed(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)
    settings_now = served_section(draft, "settings")

    changed = await stand.put_section(
        author, draft, "settings", settings_now | {"adventure_id": "new-id"}
    )
    cleared = await stand.put_section(
        author, draft, "settings", settings_now | {"adventure_id": None}
    )
    emptied = await stand.put_section(
        author, draft, "settings", settings_now | {"adventure_id": ""}
    )
    same = await stand.put_section(
        author, draft, "settings", settings_now | {"adventure_id": ADVENTURE_ID, "tone": "мрак"}
    )

    body = assert_error(changed, 422, "adventure_id_locked")
    assert body["detail"] == "Приключение уже опубликовано под этим id."
    assert_error(cleared, 422, "adventure_id_locked")
    assert_error(emptied, 422, "adventure_id_locked")
    assert same.status_code == 200, same.text
    assert same.json()["adventure_id"] == ADVENTURE_ID
    # Настройки без поля id (клиенты B4/B5) после публикации тоже пишутся: id не меняется.
    without_field = await stand.put_section(
        author, same.json(), "settings", settings_now | {"tone": "туман"}
    )
    assert without_field.status_code == 200, without_field.text
    assert without_field.json()["adventure_id"] == ADVENTURE_ID


async def test_regenerating_the_concept_keeps_the_published_adventure_id(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    draft = await saved(
        stand,
        author,
        draft,
        "settings",
        served_section(draft, "settings") | {"adventure_id": "my-lighthouse"},
    )
    assert (await publish_draft(stand, author, draft)).status_code == 201

    await generate(stand, author, draft, step="concept")
    await stand.app.state.authoring_tasks.join()

    after = await stand.draft(author, draft["id"])
    assert after["job"]["status"] == "done", after["job"]
    assert after["adventure_id"] == "my-lighthouse"


# --- игры не меняются ---


async def test_an_earlier_game_keeps_its_copy_after_a_new_publication_of_the_same_id(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)
    game_id = (await create_game(stand, author, ADVENTURE_ID)).json()["id"]
    async with stand.db() as session:
        before = (await session.get(Game, game_id)).adventure_data

    draft = await retitled(stand, author, await stand.draft(author, draft["id"]), "Новое издание")
    assert (await publish_draft(stand, author, draft)).status_code == 200

    async with stand.db() as session:
        game = await session.get(Game, game_id)
    assert game.adventure_data == before
    assert game.adventure_data["title"] != "Новое издание"
    summary = (await stand.call(author, "GET", f"/api/games/{game_id}")).json()
    assert summary["adventure_title"] == before["title"]


# --- старт сервера: опубликованное в том же каталоге ---


@asynccontextmanager
async def started(settings: Settings, monkeypatch, deps: list[Any] | None = None):
    """Приложение после `lifespan` и клиент к нему; `deps` получает зависимости бота."""

    @asynccontextmanager
    async def recording_bot(bot_deps):
        if deps is not None:
            deps.append(bot_deps)
        yield Notifier(bot_deps.sessionmaker)

    monkeypatch.setattr(app_module, "running_bot", recording_bot)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield app, http


async def test_after_a_restart_the_published_adventure_is_back_in_the_catalog(
    stand, settings, monkeypatch
):
    author = await stand.author()
    await publish_draft(stand, author, await complete_draft(stand, author))

    async with started(settings, monkeypatch) as (app, http):
        listed = await http.get("/api/adventures", headers=author.headers)
        game = await http.post(
            "/api/games", headers=author.headers, json={"adventure_id": ADVENTURE_ID}
        )

    assert {a["id"] for a in listed.json()} == {"demo-crypt", ADVENTURE_ID}
    assert app.state.adventures[ADVENTURE_ID].adventure == to_adventure(await generated_draft())
    assert game.status_code == 201, game.text


async def test_the_bot_sees_a_new_publication_in_the_same_catalog(stand, settings, monkeypatch):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    deps: list[Any] = []

    async with started(settings, monkeypatch, deps) as (app, http):
        response = await http.post(
            f"/api/drafts/{draft['id']}/publish", headers=author.headers, json={}
        )
        [bot_deps] = deps
        keyboard = newgame_keyboard(bot_deps.catalog)

    assert response.status_code == 201, response.text
    assert bot_deps.catalog is app.state.adventures
    buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert any(data.endswith(ADVENTURE_ID) for data in buttons), buttons


async def test_a_folder_wins_over_a_published_row_with_the_same_id_at_start(
    stand, settings, monkeypatch, caplog
):
    author = await stand.author()
    adventure = to_adventure(await generated_draft()).model_copy(update={"id": "demo-crypt"})
    async with stand.db() as session:
        session.add(
            AuthoredAdventure(
                id="demo-crypt",
                owner_user_id=author.user_id,
                data=adventure.model_dump(mode="json"),
                source_draft_id=None,
            )
        )
        await session.commit()

    with caplog.at_level(logging.WARNING, logger=publish.__name__):
        async with started(settings, monkeypatch) as (app, _http):
            entry = app.state.adventures["demo-crypt"]

    assert entry.adventure.title != adventure.title
    assert "demo-crypt" in caplog.text


async def test_a_broken_published_row_is_skipped_at_start_and_the_rest_is_loaded(
    stand, settings, monkeypatch, caplog
):
    author = await stand.author()
    await publish_draft(stand, author, await complete_draft(stand, author))
    async with stand.db() as session:
        session.add(
            AuthoredAdventure(
                id="broken",
                owner_user_id=author.user_id,
                data={"id": "broken"},
                source_draft_id=None,
            )
        )
        await session.commit()

    with caplog.at_level(logging.WARNING, logger=publish.__name__):
        async with started(settings, monkeypatch) as (app, _http):
            loaded = set(app.state.adventures)

    assert loaded == {"demo-crypt", ADVENTURE_ID}
    assert "broken" in caplog.text


async def test_publications_of_the_table_count_one_row_per_adventure(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    for _ in range(3):
        await publish_draft(stand, author, draft)

    async with stand.db() as session:
        count = await session.scalar(select(func.count()).select_from(AuthoredAdventure))
    assert count == 1


# --- финальное покрытие B6: порядок проверок публикации (отчёт B6.1, заметка 4) ---


async def add_job(stand: Stand, draft: dict[str, Any]) -> None:
    async with stand.db() as session:
        session.add(AuthoringJob(draft_id=draft["id"], step="world"))
        await session.commit()


async def test_someone_elses_draft_is_not_found_before_its_running_job_is_seen(stand):
    author, other = await stand.author(), await stand.author()
    draft = await complete_draft(stand, author)
    await add_job(stand, draft)

    assert_error(await publish_draft(stand, other, draft), 404, None)


async def test_a_player_is_refused_before_the_draft_is_looked_up(stand):
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)

    response = await publish_draft(stand, player, {"id": "0" * 32})

    assert response.status_code == 403, response.text


async def test_a_running_job_is_named_before_a_missing_adventure_id(stand):
    author = await stand.author()
    draft = await stand.new_draft(author, title="Без id")
    await add_job(stand, draft)

    assert_error(await publish_draft(stand, author, draft), 409, "job_running")


async def test_a_missing_adventure_id_is_named_before_the_errors_of_the_check(stand):
    # У нового черновика нет ни карты, ни сюжета: ошибок проверки много, но сначала — id.
    author = await stand.author()
    draft = await stand.new_draft(author, title="Только название")

    assert_error(await publish_draft(stand, author, draft), 422, "no_adventure_id")


@pytest.mark.parametrize("taken_by", ["папка", "другой автор"])
async def test_errors_of_the_check_are_named_before_a_taken_id(stand, taken_by):
    author, other = await stand.author(), await stand.author()
    if taken_by == "папка":
        with_folders(stand)
        taken = "demo-crypt"
    else:
        await publish_draft(stand, other, await complete_draft(stand, other))
        taken = ADVENTURE_ID
    draft = await complete_draft(stand, author)
    settings_data = served_section(draft, "settings") | {"adventure_id": taken}
    draft = await saved(stand, author, draft, "settings", settings_data)
    data = served_section(draft, "locations") | {"start_location": None}
    draft = await saved(stand, author, draft, "locations", data)

    response = await publish_draft(stand, author, draft)

    body = assert_error(response, 422, "blocking_errors")
    assert "locations: Старт не выбран" in body["problems"]


# --- финальное покрытие B6: гонки и сбои публикации ---


async def test_a_double_click_on_publish_gives_one_row_and_no_server_error(stand):
    # Две публикации одного черновика сразу: одна первая (201), вторая — обновление своей строки
    # (200) или отказ по занятому id (409, если вставки столкнулись); но не 500 и не две строки.
    author = await stand.author()
    draft = await complete_draft(stand, author)

    responses = await asyncio.gather(*(publish_draft(stand, author, draft) for _ in range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses in ([200, 201], [201, 409]), [r.text for r in responses]
    [row] = await rows(stand)
    assert (row.id, row.source_draft_id) == (ADVENTURE_ID, draft["id"])
    assert stand.app.state.adventures[ADVENTURE_ID].published


def fail_inserts_of_publications(settings: Settings) -> None:
    """Запись публикации падает ошибкой базы: триггер ссылается на несуществующую таблицу."""
    with closing(sqlite3.connect(database_file(settings))) as connection:
        connection.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON authored_adventures "
            "BEGIN INSERT INTO no_such_table VALUES (1); END"
        )
        connection.commit()


async def test_a_publication_that_the_database_refuses_leaves_neither_a_row_nor_the_catalog(
    stand, settings
):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    catalog = dict(stand.app.state.adventures)
    fail_inserts_of_publications(settings)

    with pytest.raises(Exception, match="no_such_table"):
        await publish_draft(stand, author, draft)

    assert await rows(stand) == []
    assert stand.app.state.adventures == catalog
    assert await stand.draft(author, draft["id"]) == draft
    # База снова пишет — та же публикация проходит как первая.
    with closing(sqlite3.connect(database_file(settings))) as connection:
        connection.execute("DROP TRIGGER fail_publish")
        connection.commit()
    assert (await publish_draft(stand, author, draft)).status_code == 201


# Дефект B6.coverage D1 исправлен в fix-раунде 1 B6 (R1/S5): xfail снят.
async def test_an_id_change_landing_during_the_publication_does_not_split_the_draft(
    stand, settings, monkeypatch
):
    # Вторая вкладка автора меняет id приключения, пока идёт публикация: запись настроек
    # проходит (черновик ещё не опубликован) между проверкой черновика и записью строки.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    real_check = publish.check

    def check_and_race(adventure, srd):
        report = real_check(adventure, srd)
        with closing(sqlite3.connect(database_file(settings))) as connection:
            connection.execute(
                "UPDATE adventure_drafts SET data = json_set(data, '$.id', 'second-tab'), "
                "version = version + 1 WHERE id = ?",
                (draft["id"],),
            )
            connection.commit()
        return report

    monkeypatch.setattr(publish, "check", check_and_race)
    first = await publish_draft(stand, author, draft)
    monkeypatch.setattr(publish, "check", real_check)
    after = await stand.draft(author, draft["id"])
    again = await publish_draft(stand, author, after)

    # Черновик и его публикация согласованы: одна строка, и её id — id черновика.
    published = {row.id for row in await rows(stand) if row.source_draft_id == draft["id"]}
    assert (first.status_code, again.status_code) in ((201, 200), (409, 201), (422, 201))
    assert published == {after["adventure_id"]}


# --- финальное покрытие B6: удалённый черновик и старт сервера ---


async def test_deleting_a_published_draft_keeps_the_adventure_and_freezes_its_id(stand):
    # Ссылка строки на черновик без внешнего ключа: черновик можно удалить. Приключение остаётся в
    # каталоге и играх, а копия того же приключения — другой черновик: этот id ей не опубликовать.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)

    deleted = await stand.call(author, "DELETE", f"/api/drafts/{draft['id']}")
    copy = await stand.call(author, "POST", f"/api/adventures/{ADVENTURE_ID}/draft")

    assert deleted.status_code == 204, deleted.text
    [row] = await rows(stand)
    assert row.source_draft_id == draft["id"]
    assert ADVENTURE_ID in stand.app.state.adventures
    assert (await create_game(stand, author, ADVENTURE_ID)).status_code == 201
    assert copy.status_code == 201, copy.text
    assert copy.json()["adventure_id"] == ADVENTURE_ID
    assert_error(await publish_draft(stand, author, copy.json()), 409, "id_taken")


BROKEN_ROWS = {
    "данные не объект": lambda adventure: ["не", "объект"],
    "id в данных другой": lambda adventure: adventure | {"id": ADVENTURE_ID},
    "ошибка схемы v2": lambda adventure: adventure | {"locations": {"x": {"id": "x"}}},
}


@pytest.mark.parametrize("kind", BROKEN_ROWS)
async def test_a_published_row_that_does_not_read_is_skipped_at_start(
    stand, settings, monkeypatch, caplog, kind
):
    author = await stand.author()
    adventure = to_adventure(await generated_draft()).model_copy(update={"id": "broken-row"})
    async with stand.db() as session:
        session.add(
            AuthoredAdventure(
                id="broken-row",
                owner_user_id=author.user_id,
                data=BROKEN_ROWS[kind](adventure.model_dump(mode="json")),
                source_draft_id=None,
            )
        )
        await session.commit()

    with caplog.at_level(logging.WARNING, logger=publish.__name__):
        async with started(settings, monkeypatch) as (app, http):
            loaded = set(app.state.adventures)
            listed = await http.get("/api/adventures", headers=author.headers)

    assert loaded == {"demo-crypt"}
    assert listed.status_code == 200, listed.text
    assert "broken-row" in caplog.text


async def test_the_server_starts_with_folders_when_the_publications_table_is_missing(
    stand, settings, monkeypatch, caplog
):
    # Миграция таблицы опубликованного ещё не выполнена: старт не падает, папки на месте.
    with closing(sqlite3.connect(database_file(settings))) as connection:
        connection.execute("DROP TABLE authored_adventures")
        connection.commit()

    with caplog.at_level(logging.ERROR, logger=publish.__name__):
        async with started(settings, monkeypatch) as (app, _http):
            loaded = set(app.state.adventures)

    assert loaded == {"demo-crypt"}
    assert "Не удалось прочитать опубликованные приключения" in caplog.text


async def test_a_row_skipped_at_start_comes_back_with_the_next_publication(
    stand, settings, monkeypatch
):
    # Строка испортилась и при старте пропущена; автор публикует черновик снова — своя строка
    # обновляется (200), и приключение снова в каталоге без нового перезапуска.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    await publish_draft(stand, author, draft)
    async with stand.db() as session:
        row = await session.get(AuthoredAdventure, ADVENTURE_ID)
        row.data = {"id": ADVENTURE_ID}
        await session.commit()

    async with started(settings, monkeypatch) as (app, http):
        skipped = ADVENTURE_ID not in app.state.adventures
        response = await http.post(
            f"/api/drafts/{draft['id']}/publish", headers=author.headers, json={}
        )
        back = app.state.adventures.get(ADVENTURE_ID)

    assert skipped
    assert response.status_code == 200, response.text
    assert back is not None and back.adventure == to_adventure(await generated_draft())


async def test_another_creator_copies_a_published_adventure_into_a_draft_of_their_own(stand):
    # Дополнение §8: копия — и папки, и опубликованного. Флажок мата — умолчание Р1, пожеланий нет;
    # копия — черновик вызывающего, а опубликовать тот же id ему нельзя: строка чужая.
    author, other = await stand.author(), await stand.author()
    content = await generated_draft()
    original = await put_draft(
        stand,
        author,
        content.model_copy(
            update={
                "settings": content.settings.model_copy(update={"profanity": False}),
                "wishes": {"plot": "Побольше пепла"},
            }
        ),
    )
    assert (await publish_draft(stand, author, original)).status_code == 201

    response = await stand.call(other, "POST", f"/api/adventures/{ADVENTURE_ID}/draft")

    assert response.status_code == 201, response.text
    copy = response.json()
    assert (copy["adventure_id"], copy["version"]) == (ADVENTURE_ID, 1)
    assert copy["settings"]["profanity"] is True
    assert copy["wishes"] == {}
    assert copy["title"] == original["title"]
    published = stand.app.state.adventures[ADVENTURE_ID].adventure
    assert to_adventure(draft_of(copy)) == published
    assert_error(await stand.call(author, "GET", f"/api/drafts/{copy['id']}"), 404, None)
    assert_error(await publish_draft(stand, other, copy), 409, "id_taken")


# --- fix-раунд 1 B6: гонка публикации (R1/S5), пределы разделов (S2) ---


async def test_a_write_landing_during_the_publication_is_a_version_conflict(stand, monkeypatch):
    # Черновик изменился после чтения публикацией: отказ 409 `version_conflict`, строки нет; версия
    # черновика публикацией не поднимается (webapp не перечитывает черновик после 201).
    author = await stand.author()
    draft = await complete_draft(stand, author)
    real_check = publish.check

    def check_and_write(adventure, srd):
        report = real_check(adventure, srd)
        with closing(sqlite3.connect(database_file(stand.app.state.settings))) as connection:
            connection.execute(
                "UPDATE adventure_drafts SET version = version + 1 WHERE id = ?", (draft["id"],)
            )
            connection.commit()
        return report

    monkeypatch.setattr(publish, "check", check_and_write)
    refused = await publish_draft(stand, author, draft)
    monkeypatch.setattr(publish, "check", real_check)
    after = await stand.draft(author, draft["id"])
    published = await publish_draft(stand, author, after)

    assert_error(refused, 409, "version_conflict")
    assert published.status_code == 201, published.text
    assert after["version"] == draft["version"] + 1  # только запись второй вкладки
    assert (await stand.draft(author, draft["id"]))["version"] == after["version"]


async def test_a_concept_job_landing_during_the_publication_does_not_split_the_draft(
    stand, monkeypatch
):
    # Задание Замысла начато после проверки `job_running` публикацией и успело записать свой id до
    # записи публикации: публикация отказывает, черновик и публикация не расходятся.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    draft = await saved(
        stand,
        author,
        draft,
        "settings",
        served_section(draft, "settings") | {"adventure_id": "my-lighthouse"},
    )
    real_active_job = publish.active_job

    async def run_concept_job_in_between(session, draft_id):
        found = await real_active_job(session, draft_id)
        async with stand.db() as other:
            user = await other.get(User, author.user_id)
            job = await jobs.create_job(
                other, user, draft_id, step="concept", wish=None, target=None
            )
        await jobs.run_job(
            stand.app.state.sessionmaker,
            job["id"],
            settings=stand.app.state.settings,
            turn_tokens=None,
        )
        return found

    monkeypatch.setattr(publish, "active_job", run_concept_job_in_between)
    refused = await publish_draft(stand, author, draft)
    monkeypatch.setattr(publish, "active_job", real_active_job)
    after = await stand.draft(author, draft["id"])
    published = await publish_draft(stand, author, after)

    assert_error(refused, 409, "version_conflict")
    assert after["job"]["status"] == "done" and after["adventure_id"] == ADVENTURE_ID
    assert published.status_code == 201, published.text
    assert {row.id for row in await rows(stand)} == {ADVENTURE_ID}


async def test_a_draft_with_a_publication_under_another_id_is_refused(stand):
    # Черновик уже опубликован под одним id, а в его данных — другой (запись мимо API или гонка с
    # записью «Замысла»): вторая строка не появляется.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    assert (await publish_draft(stand, author, draft)).status_code == 201
    with closing(sqlite3.connect(database_file(stand.app.state.settings))) as connection:
        connection.execute(
            "UPDATE adventure_drafts SET data = json_set(data, '$.id', 'other-id') WHERE id = ?",
            (draft["id"],),
        )
        connection.commit()

    response = await publish_draft(stand, author, await stand.draft(author, draft["id"]))

    body = assert_error(response, 422, "adventure_id_locked")
    assert body["detail"] == "Приключение уже опубликовано под этим id."
    assert {row.id for row in await rows(stand)} == {ADVENTURE_ID}
    assert "other-id" not in stand.app.state.adventures


async def test_the_published_id_of_a_draft_is_its_earliest_publication(stand):
    # Две строки одного черновика (данные до исправления гонки): закреплён первый опубликованный id,
    # и ответ не зависит от порядка строк в базе.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    data = to_adventure(await generated_draft()).model_dump(mode="json")
    async with stand.db() as session:
        for ident, when in (
            ("later-id", "2026-10-02T00:00+00:00"),
            ("first-id", "2026-10-01T00:00+00:00"),
        ):
            session.add(
                AuthoredAdventure(
                    id=ident,
                    owner_user_id=author.user_id,
                    data=data | {"id": ident},
                    source_draft_id=draft["id"],
                    published_at=datetime.fromisoformat(when),
                )
            )
        await session.commit()

    async with stand.db() as session:
        assert await service.published_id(session, draft["id"]) == "first-id"


async def test_a_draft_over_the_limits_of_a_section_is_not_published(stand):
    # S2: разделы, записанные мимо PUT (копия папки, данные до B6), публикация проверяет теми же
    # пределами: строка «<шаг>: <раздел>.<путь>: <предел>», без значения поля.
    author = await stand.author()
    content = await generated_draft()
    long_notes = "ТАЙНА-" + "я" * 3000
    locations = (
        content.locations[0].model_copy(update={"dm_notes": long_notes}),
        *content.locations[1:],
    )
    draft = await put_draft(stand, author, content.model_copy(update={"locations": locations}))

    response = await publish_draft(stand, author, draft)

    body = assert_error(response, 422, "blocking_errors")
    assert "locations: locations.locations.0.dm_notes: не длиннее 2000 знаков" in body["problems"]
    assert "ТАЙНА-" not in response.text
    assert await rows(stand) == []


async def test_a_copy_of_a_published_place_with_a_hidden_item_and_two_clues_is_published(stand):
    # Дополнение §11 (по отчёту B6.1 att.002): опубликованное приключение собрано — у тайника
    # находка-предмет и две находки-улики; копия этого приключения проходит пределы и публикуется.
    author = await stand.author()
    draft = await put_draft(stand, author, crowded_cupboard(await generated_draft()))
    assert (await publish_draft(stand, author, draft)).status_code == 201
    copy = (await stand.call(author, "POST", f"/api/adventures/{ADVENTURE_ID}/draft")).json()
    assert len(cupboard_finds(copy)) == 3
    copy = await saved(
        stand,
        author,
        copy,
        "settings",
        served_section(copy, "settings") | {"adventure_id": "copied-lighthouse"},
    )

    response = await publish_draft(stand, author, copy)

    assert response.status_code == 201, response.text
    assert "copied-lighthouse" in stand.app.state.adventures
