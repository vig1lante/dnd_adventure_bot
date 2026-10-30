"""Конструктор на стороне сервера: разделы боёв, своих статблоков и откровений, шаги
`encounters`/`items`/`clues`, id приключения ручным автором, пустота и статусы новых шагов, понятные
тексты проверки, пределы, копия приключения каталога и справочник монстров SRD.

Публикация — `test_publish.py`, экспорт и импорт архивом — `test_archive.py`; стенд и данные
разделов — из `test_drafts_api.py`. Живых моделей нет: ответы шагов проигрывает `FakeProvider`.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from generator_fakes import v2_answers
from test_drafts_api import (
    CREATOR_TG_ID,
    LIGHTHOUSE,
    SECRET_INPUT,
    as_served,
    assert_draft_shape,
    assert_error,
    complete_draft,
    cupboard,
    findings,
    generated_draft,
    map_with,
    put_draft,
    saved,
    served_section,
    settings_data,
    validate,
)
from test_jobs import generate

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Encounter
from dnd_engine.content import MonsterDef, load_srd
from dnd_server.adventures import load_catalog
from dnd_server.authoring.draft import AuthoringDraft, from_adventure

# tests/authoring/test_validate_api.py -> authoring -> tests -> server -> services -> корень
DEMO_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
FIGHT_AND_CLUE_SECTIONS = ("encounters", "monsters", "revelations")


def encounters_data() -> list[dict[str, Any]]:
    return as_served(Encounter, v2_answers()["generator.encounters"]["encounters"])


def monsters_data() -> list[dict[str, Any]]:
    """Свои статблоки демо v2 (пепельный жрец), опыт которых соответствует cr."""
    return as_served(MonsterDef, v2_answers()["generator.encounters"]["monsters"])


def revelations_data() -> list[dict[str, Any]]:
    """Откровения демо v2: утверждение из замысла и улики из шага clues."""
    answers = v2_answers()
    clues = {r["revelation_id"]: r["clues"] for r in answers["generator.clues"]["revelations"]}
    return [
        revelation | {"clues": clues.get(revelation["id"], [])}
        for revelation in answers["generator.concept"]["revelations"]
    ]


def b6_data(section: str) -> Any:
    return {
        "encounters": encounters_data,
        "monsters": monsters_data,
        "revelations": revelations_data,
    }[section]()


def monster(**changes: Any) -> dict[str, Any]:
    return monsters_data()[0] | changes


def encounter(**changes: Any) -> dict[str, Any]:
    return encounters_data()[0] | changes


def group(monster_id: str = "ghoul", count: int = 1) -> dict[str, Any]:
    return {"monster_id": monster_id, "count": count, "zone": "here"}


def revelation(**changes: Any) -> dict[str, Any]:
    return revelations_data()[0] | changes


def clue(n: int) -> dict[str, Any]:
    return {"id": f"clue-{n}", "description": "Пепел на подоконнике.", "where": "npc:bram"}


# --- разделы боёв, своих статблоков и откровений: запись и чтение ---


@pytest.mark.parametrize("section", FIGHT_AND_CLUE_SECTIONS)
async def test_each_section_of_fights_and_clues_is_saved_and_read_back_as_given(stand, section):
    author = await stand.author()
    draft = await stand.new_draft(author)
    data = b6_data(section)

    written = await saved(stand, author, draft, section, data)

    assert_draft_shape(written)
    assert written[section] == data
    assert await stand.draft(author, draft["id"]) == written
    again = await saved(stand, author, written, section, served_section(written, section))
    assert again[section] == data


@pytest.mark.parametrize("section", FIGHT_AND_CLUE_SECTIONS)
async def test_what_the_generator_wrote_into_a_section_of_fights_and_clues_is_saved_back_unchanged(
    stand, section
):
    # Инвариант: сервер принимает назад всё, что отдаёт.
    author = await stand.author()
    draft = await complete_draft(stand, author)

    written = await saved(stand, author, draft, section, served_section(draft, section))

    assert written[section] == draft[section]


def many(build, count: int) -> list[dict[str, Any]]:
    return [build() | {"id": f"e{n}"} for n in range(count)]


@pytest.mark.parametrize(
    ("section", "data", "where"),
    [
        ("monsters", many(monster, 7), "monsters: не больше 6 элементов"),
        ("encounters", many(encounter, 9), "encounters: не больше 8 элементов"),
        (
            "encounters",
            [encounter(monsters=[group(f"m{n}") for n in range(5)])],
            "encounters.0.monsters: не больше 4 элементов",
        ),
        ("encounters", [encounter(monsters=[group(count=0)])], "encounters.0.monsters.0.count"),
        ("encounters", [encounter(monsters=[group(count=21)])], "encounters.0.monsters.0.count"),
        (
            "encounters",
            [encounter(monsters=[group("ghoul", 11), group("zombie", 10)])],
            "encounters.0.monsters: всего монстров в бою — не больше 20",
        ),
        ("encounters", [encounter(trigger="Я" * 2001)], "encounters.0.trigger"),
        ("encounters", [encounter(tactics="Я" * 2001)], "encounters.0.tactics"),
        ("revelations", many(revelation, 6), "revelations: не больше 5 элементов"),
        (
            "revelations",
            [revelation(clues=[clue(n) for n in range(9)])],
            "revelations.0.clues: не больше 8 элементов",
        ),
        ("revelations", [revelation(statement="Я" * 2001)], "revelations.0.statement"),
        (
            "revelations",
            [revelation(clues=[clue(1) | {"description": "Я" * 2001}])],
            "revelations.0.clues.0.description",
        ),
        ("monsters", [monster(name="Я" * 101)], "monsters.0.name"),
        ("monsters", [monster(traits=["Я" * 2001])], "monsters.0.traits.0"),
        ("monsters", [monster(id="Пепельный жрец")], "monsters.0.id"),
    ],
)
async def test_a_section_of_fights_and_clues_over_its_limits_is_refused_with_the_path(
    stand, section, data, where
):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith(where) for line in body["problems"]), body
    assert "ЯЯЯ" not in response.text
    assert await stand.draft(author, draft["id"]) == draft


@pytest.mark.parametrize(
    ("section", "data"),
    [
        ("encounters", [encounter(), encounter()]),
        ("monsters", [monster(), monster()]),
        ("revelations", [revelation(), revelation()]),
    ],
)
async def test_repeated_ids_in_a_section_of_fights_and_clues_are_refused(stand, section, data):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith(f"{section}: id повторяются") for line in body["problems"]), body


@pytest.mark.parametrize("section", FIGHT_AND_CLUE_SECTIONS)
async def test_an_extra_field_in_a_section_of_fights_and_clues_is_refused(stand, section):
    author = await stand.author()
    draft = await stand.new_draft(author)
    data = b6_data(section)
    data[0] |= {SECRET_INPUT: 1}

    response = await stand.put_section(author, draft, section, data)

    body = assert_error(response, 422, "invalid_data")
    assert any("лишнее поле" in line for line in body["problems"]), body


async def test_a_custom_statblock_with_a_cr_that_is_not_a_finite_number_is_refused(stand):
    # JSON сервера понимает `NaN`; такое число не должно попасть в черновик и в сравнение опыта.
    author = await stand.author()
    draft = await stand.new_draft(author)
    data = [monster(cr="NOT-A-NUMBER")]
    body = json.dumps({"version": 1, "data": data}).replace('"NOT-A-NUMBER"', "NaN")

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/sections/monsters",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith("monsters.0.cr") for line in body["problems"]), body


async def test_six_custom_statblocks_are_the_limit_of_a_draft(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "monsters", many(monster, 6))

    assert len(written["monsters"]) == 6


# Ровно на пределах раздела запись проходит (отказ на шаг дальше — выше).
AT_THE_LIMITS = {
    "8 боёв": ("encounters", lambda: many(encounter, 8)),
    "4 группы в бою": (
        "encounters",
        lambda: [
            encounter(monsters=[group(m) for m in ("ghoul", "zombie", "skeleton", "cultist")])
        ],
    ),
    "группа из одного": ("encounters", lambda: [encounter(monsters=[group(count=1)])]),
    "группа из 20": ("encounters", lambda: [encounter(monsters=[group(count=20)])]),
    "20 монстров в бою": (
        "encounters",
        lambda: [
            encounter(monsters=[group(m, 5) for m in ("ghoul", "zombie", "skeleton", "wolf")])
        ],
    ),
    "тексты боя по 2000": (
        "encounters",
        lambda: [encounter(trigger="Я" * 2000, tactics="Я" * 2000)],
    ),
    "5 откровений": ("revelations", lambda: many(revelation, 5)),
    "8 улик": ("revelations", lambda: [revelation(clues=[clue(n) for n in range(8)])]),
    "тексты откровения по 2000": (
        "revelations",
        lambda: [revelation(statement="Я" * 2000, clues=[clue(1) | {"description": "Я" * 2000}])],
    ),
    "имя статблока в 100 знаков": ("monsters", lambda: [monster(name="🐉" * 100)]),
}


@pytest.mark.parametrize("case", AT_THE_LIMITS)
async def test_a_section_of_fights_and_clues_exactly_at_its_limits_is_saved(stand, case):
    section, build = AT_THE_LIMITS[case]
    author = await stand.author()
    draft = await stand.new_draft(author)
    data = build()

    written = await saved(stand, author, draft, section, data)

    assert written[section] == data


@pytest.mark.parametrize("number", ["Infinity", "-Infinity", "-1", "-0.125"])
async def test_a_custom_statblock_with_an_infinite_or_negative_cr_is_refused(stand, number):
    author = await stand.author()
    draft = await stand.new_draft(author)
    body = json.dumps({"version": 1, "data": [monster(cr="CR")]}).replace('"CR"', number)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/sections/monsters",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    body = assert_error(response, 422, "invalid_data")
    assert any(line.startswith("monsters.0.cr") for line in body["problems"]), body
    assert await stand.draft(author, draft["id"]) == draft


async def test_a_removed_encounter_loses_its_lock_in_the_same_write(stand):
    # Замок удалённой сущности снимается в той же записи во всех разделах-списках, и в боях тоже.
    author = await stand.author()
    draft = await saved(
        stand, author, await stand.new_draft(author), "encounters", encounters_data()
    )
    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/locks",
        json={"version": draft["version"], "locks": ["encounter:drowned-ambush", "step:plot"]},
    )
    draft = response.json()

    written = await saved(stand, author, draft, "encounters", encounters_data()[1:])

    assert written["locks"] == ["step:plot"]


# --- шаги боёв, предметов и улик генерируются ---


@pytest.mark.parametrize("step", ["encounters", "items", "clues"])
async def test_a_wish_for_a_step_of_fights_and_clues_is_saved(stand, step):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.call(
        author,
        "PUT",
        f"/api/drafts/{draft['id']}/wishes/{step}",
        json={"version": 1, "text": "Побольше пепла"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["wishes"] == {step: "Побольше пепла"}


def without_step(content: AuthoringDraft, step: str) -> AuthoringDraft:
    """Черновик демо v2, где шаг `step` ещё не писал свой раздел."""
    if step == "clues":
        bare = tuple(r.model_copy(update={"clues": ()}) for r in content.revelations)
        return content.model_copy(update={"revelations": bare})
    sections = {"encounters": ("encounters", "monsters"), "items": ("items",)}[step]
    return content.model_copy(update=dict.fromkeys(sections, ()))


@pytest.mark.parametrize(
    ("step", "written"),
    [("encounters", "encounters"), ("items", "items"), ("clues", "revelations")],
)
async def test_a_step_of_fights_and_clues_is_generated_into_its_section(stand, step, written):
    author = await stand.author()
    draft = await put_draft(stand, author, without_step(await generated_draft(), step))

    response = await generate(stand, author, draft, step=step)
    await stand.app.state.authoring_tasks.join()

    assert response.status_code == 202, response.text
    after = await stand.draft(author, draft["id"])
    assert after["job"]["status"] == "done", after["job"]
    assert after[written] != draft[written]
    assert after["steps"][step]["status"] != "empty"
    assert after["steps"][step]["updated_at"] is not None


async def test_a_single_encounter_is_regenerated_by_its_target(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    response = await generate(stand, author, draft, step="encounters", target="encounter:ash-rite")
    await stand.app.state.authoring_tasks.join()

    assert response.status_code == 202, response.text
    after = await stand.draft(author, draft["id"])
    assert after["job"]["status"] == "done", after["job"]
    assert [e["id"] for e in after["encounters"]] == ["drowned-ambush", "ash-rite"]


# --- id приключения ручным автором ---


async def test_the_author_sets_the_adventure_id_in_the_settings(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "settings", settings_data(adventure_id="my-lamp"))

    assert written["adventure_id"] == "my-lamp"
    # Замысел с названием, завязкой и id готов и без генерации.
    assert written["steps"]["concept"]["status"] == "ready"
    summaries = (await stand.call(author, "GET", "/api/drafts")).json()
    assert summaries[0]["adventure_id"] == "my-lamp"


@pytest.mark.parametrize("value", ["", None])
async def test_an_empty_adventure_id_means_it_is_not_set(stand, value):
    author = await stand.author()
    draft = await saved(
        stand, author, await stand.new_draft(author), "settings", settings_data(adventure_id="x")
    )

    written = await saved(stand, author, draft, "settings", settings_data(adventure_id=value))

    assert written["adventure_id"] is None
    assert written["steps"]["concept"]["status"] == "draft"


async def test_settings_without_the_adventure_id_keep_the_one_the_draft_has(stand):
    # Старые клиенты шлют настройки без этого поля: id, который дала генерация, не теряется.
    author = await stand.author()
    draft = await complete_draft(stand, author)

    written = await saved(stand, author, draft, "settings", settings_data())

    assert written["adventure_id"] == draft["adventure_id"] is not None


@pytest.mark.parametrize("value", ["Мой маяк", "../lamp", "a" * 65, "-lamp", SECRET_INPUT])
async def test_an_adventure_id_out_of_the_pattern_is_refused_without_the_value(stand, value):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(author, draft, "settings", settings_data(adventure_id=value))

    body = assert_error(response, 422, "invalid_data")
    assert body["problems"] == [
        "settings.adventure_id: только строчная латиница, цифры, «-» и «_», до 64 знаков"
    ]
    assert value not in response.text


# --- пустота и статусы шагов боёв, предметов и улик ---


async def test_a_dangling_encounter_of_a_location_makes_the_encounters_step_visible(stand):
    # Висящий `encounter_id` в черновике без боёв больше не прячется — шаг боёв не пуст, и находка
    # видна со ссылкой на него.
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "locations", map_with(encounter_id="ghost-fight"))

    assert written["steps"]["encounters"]["status"] == "draft"
    report = await validate(stand, author, written)
    assert ("dangling_ref", "location:harbor", "encounters") in findings(report)


async def test_the_encounters_step_without_encounters_or_links_is_empty(stand):
    author = await stand.author()
    draft = await saved(stand, author, await stand.new_draft(author), "locations", map_with())

    assert draft["steps"]["encounters"]["status"] == "empty"


async def test_the_items_step_is_empty_without_items_and_not_empty_with_a_locked_one(stand):
    author = await stand.author()
    content = await generated_draft()
    empty = await put_draft(stand, author, content.model_copy(update={"items": ()}))
    locked = await put_draft(
        stand, author, content.model_copy(update={"locks": frozenset({"item:lamp-key"})})
    )

    assert empty["steps"]["items"]["status"] == "empty"
    # Закреплённое — тоже содержимое.
    assert locked["steps"]["items"]["status"] == "ready"


async def test_a_revelation_without_clues_beside_one_with_clues_is_shown_by_the_check(stand):
    # Находка `too_few_clues` больше не прячется фильтром пустых шагов: шаг улик пуст, только если
    # ни у одного откровения нет улик.
    author = await stand.author()
    draft = await complete_draft(stand, author)
    bare = {"id": "second-truth", "statement": "Брам в долгу.", "clues": []}

    written = await saved(
        stand, author, draft, "revelations", [*served_section(draft, "revelations"), bare]
    )

    assert written["steps"]["clues"]["status"] == "draft"
    report = await validate(stand, author, written)
    assert ("too_few_clues", "revelation:second-truth", "clues") in findings(report)


async def test_the_clues_step_with_only_bare_revelations_is_empty(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(stand, author, draft, "revelations", [revelation(clues=[])])

    assert written["steps"]["clues"]["status"] == "empty"


async def test_the_steps_of_fights_and_clues_of_a_complete_draft_are_ready(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    assert {step: draft["steps"][step]["status"] for step in ("encounters", "items", "clues")} == {
        "encounters": "ready",
        "items": "ready",
        "clues": "ready",
    }


async def test_an_error_of_a_custom_statblock_keeps_the_encounters_step_a_draft(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    written = await saved(stand, author, draft, "monsters", [monster(xp=1800)])

    assert written["steps"]["encounters"]["status"] == "draft"


# --- проверка: понятные тексты и опыт своего статблока ---


@pytest.mark.parametrize(
    ("field", "text"),
    [("start_location", "Старт не выбран"), ("finale_location", "Финал не выбран")],
)
async def test_a_map_without_a_start_or_a_finale_says_so_plainly(stand, field, text):
    author = await stand.author()
    draft = await stand.new_draft(author)
    written = await saved(stand, author, draft, "locations", map_with() | {field: None})

    report = await validate(stand, author, written)

    found = [f for f in report["errors"] if f["message"] == text]
    assert found and found[0]["step"] == "locations", report["errors"]
    assert all("ссылка 'location:'" not in f["message"] for f in report["errors"])


async def test_a_custom_statblock_whose_xp_does_not_match_its_cr_is_an_error_of_encounters(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)
    written = await saved(stand, author, draft, "monsters", [monster(cr=5, xp=1800)])

    report = await validate(stand, author, written)

    [found] = [f for f in report["errors"] if f["code"] == "custom_monster_xp"]
    assert (found["ref"], found["step"]) == ("monster:ash-priest", "encounters")
    assert "cr" in found["message"] and "1800" not in found["message"]
    assert report["readiness_percent"] <= 99


async def test_a_complete_draft_has_no_error_and_is_fully_ready(stand):
    author = await stand.author()
    draft = await complete_draft(stand, author)

    report = await validate(stand, author, draft)

    assert (report["errors"], report["readiness_percent"]) == ([], 100)


# --- копия приключения каталога ---


def demo_catalog(stand) -> None:
    """Каталог стенда с демо-приключением; словарь тот же, что держит приложение."""
    stand.app.state.adventures.update(load_catalog(DEMO_DIR.parent, load_srd()))


async def test_a_catalog_adventure_is_copied_into_a_new_draft_of_the_caller(stand):
    author, other = await stand.author(), await stand.author()
    demo_catalog(stand)

    response = await stand.call(author, "POST", "/api/adventures/demo-crypt/draft")

    assert response.status_code == 201, response.text
    copy = response.json()
    assert_draft_shape(copy)
    assert (copy["version"], copy["adventure_id"]) == (1, "demo-crypt")
    expected = from_adventure(load_adventure(DEMO_DIR)).model_dump(mode="json")
    for section in ("locations", "npcs", "items", "encounters", "revelations", "relations"):
        assert copy[section] == expected[section], section
    assert copy["settings"]["profanity"] is True  # у приключения флажка нет: значение по умолчанию
    assert copy["wishes"] == {}
    assert await stand.draft(author, copy["id"]) == copy
    foreign = await stand.call(other, "GET", f"/api/drafts/{copy['id']}")
    assert_error(foreign, 404, None)


async def test_an_unknown_adventure_cannot_be_copied(stand):
    author = await stand.author()
    demo_catalog(stand)

    response = await stand.call(author, "POST", "/api/adventures/no-such/draft")

    assert_error(response, 404, None)


async def test_a_player_who_may_not_create_games_cannot_copy_an_adventure(stand):
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)
    demo_catalog(stand)

    response = await stand.call(player, "POST", "/api/adventures/demo-crypt/draft")

    assert response.status_code == 403, response.text


# --- справочник монстров SRD ---


async def test_the_srd_monster_list_names_id_name_cr_xp_and_tags_in_the_order_of_the_data(stand):
    author = await stand.author()

    response = await stand.call(author, "GET", "/api/srd/monsters")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": m.id, "name": m.name, "cr": m.cr, "xp": m.xp, "tags": list(m.tags)}
        for m in load_srd().monsters.values()
    ]


async def test_the_srd_monster_list_is_only_for_creators(stand):
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)

    response = await stand.call(player, "GET", "/api/srd/monsters")

    assert response.status_code == 403, response.text


# --- находки места и улики ---


def reveals(items: int = 0, flags: int = 0, clues: int = 0) -> list[dict[str, Any]]:
    """Находки места в форме `Draft` (`RevealRef` со значением по умолчанию)."""
    return [
        *({"kind": "item", "id": f"item-{n}", "value": None} for n in range(items)),
        *({"kind": "flag", "id": f"flag-{n}", "value": None} for n in range(flags)),
        *({"kind": "clue", "id": f"clue-{n}", "value": None} for n in range(clues)),
    ]


@pytest.mark.parametrize(
    "found",
    [reveals(items=2, clues=3), reveals(items=1, flags=1, clues=40), reveals(clues=40)],
)
async def test_clue_finds_of_a_place_do_not_count_against_its_two_finds(stand, found):
    # Находки-улики дописывает сборка из улик откровений; предел 2 — у предметов и флагов, а у улик
    # места свой — 5 откровений × 8 улик.
    author = await stand.author()
    draft = await stand.new_draft(author)

    written = await saved(
        stand, author, draft, "locations", map_with(LIGHTHOUSE, features=[cupboard(reveals=found)])
    )

    assert written["locations"][LIGHTHOUSE]["features"][0]["reveals"] == found


@pytest.mark.parametrize(
    ("found", "message"),
    [
        (reveals(items=3), "не больше 2 находок, кроме улик"),
        (reveals(items=1, flags=2, clues=1), "не больше 2 находок, кроме улик"),
        (reveals(clues=41), "не больше 40 находок-улик"),
    ],
)
async def test_a_place_over_its_finds_or_clue_finds_is_refused(stand, found, message):
    author = await stand.author()
    draft = await stand.new_draft(author)

    response = await stand.put_section(
        author, draft, "locations", map_with(LIGHTHOUSE, features=[cupboard(reveals=found)])
    )

    body = assert_error(response, 422, "invalid_data")
    assert body["problems"] == [f"locations.locations.{LIGHTHOUSE}.features.0.reveals: {message}"]
