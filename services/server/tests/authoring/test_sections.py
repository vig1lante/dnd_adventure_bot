"""Разделы черновика в конструкторе (планы B4.1, B5.1; спека §10.3; дополнения dispatcher B4 §2, §3,
§7, §8 и B5 §1, §3–§5).

Чистые функции без базы: раздел ↔ шаг, проверка данных раздела с пределами API и запись его в
черновик (`apply_section`), строки проблем без значений входа, статусы шагов и отчёт проверки по
заполненным разделам. Черновик — контракт B3 (`AuthoringDraft`); живых моделей нет.
"""

import re

import pytest
from generator_fakes import V2_SCENARIO, RecordingProvider, v2_answers, v2_draft
from pydantic import TypeAdapter, ValidationError
from test_drafts_api import (
    HARBOR,
    LIGHTHOUSE,
    cupboard,
    items_data,
    location_at,
    locations_data,
    map_with,
    npcs_data,
    relations_data,
)

from dnd_engine.adventure.schema import Deity, Faction, Item, Location, Npc, Relation, WorldInfo
from dnd_engine.content import load_srd
from dnd_server.authoring.draft import STEPS, AuthoringDraft, Plot, to_adventure
from dnd_server.authoring.pipeline import check, generate, owner_step
from dnd_server.authoring.sections import (
    EDITOR_STEPS,
    REQUIRED_STEPS,
    SECTION_STEPS,
    InvalidSection,
    apply_section,
    draft_report,
    problems,
    step_statuses,
    steps_written_by,
)
from dnd_server.authoring.steps import is_empty, run_step
from dnd_server.llm.fake import FakeProvider, load_scenario

SECRET_INPUT = "ЗНАЧЕНИЕ-ИЗ-ЗАПРОСА"


def settings_data(**changes) -> dict:
    """Данные раздела «Замысел» по дополнению §2: все поля настроек, название и главное для
    дневника."""
    return {
        "title": "Маяк",
        "journal_intro": "Маяк погас.",
        "pitch": "Маяк на мысу погас.",
        "tone": "морская готика",
        "target_hours": 2,
        "party_size": 4,
        "party_level": 3,
        "modules": ["campfire"],
        "profanity": False,
    } | changes


def deity(ident: str = "selmara", **changes) -> dict:
    return v2_answers()["generator.pantheon"]["deities"][0] | {"id": ident} | changes


def faction(ident: str = "smugglers", **changes) -> dict:
    return v2_answers()["generator.factions"]["factions"][0] | {"id": ident} | changes


def world(**changes) -> dict:
    return v2_answers()["generator.world"] | changes


def plot(**changes) -> dict:
    return v2_answers()["generator.plot"] | changes


def region(**changes) -> dict:
    return world()["regions"][0] | changes


def rumor(**changes) -> dict:
    return world()["rumors"][0] | changes


def act(**changes) -> dict:
    return plot()["acts"][0] | changes


def problems_of(section: str, data, draft: AuthoringDraft | None = None) -> list[str]:
    with pytest.raises(InvalidSection) as refusal:
        apply_section(draft or AuthoringDraft(), section, data)
    return refusal.value.problems


# --- раздел и шаг ---


def test_the_editor_has_every_section_of_the_contract_each_with_its_step():
    # Дополнение B6 §1: все 12 разделов; бои и свои статблоки пишет шаг encounters, откровения —
    # шаг clues.
    assert SECTION_STEPS == {
        "settings": "concept",
        "world": "world",
        "deities": "pantheon",
        "factions": "factions",
        "plot": "plot",
        "locations": "locations",
        "npcs": "npcs",
        "relations": "relations",
        "items": "items",
        "encounters": "encounters",
        "monsters": "encounters",
        "revelations": "clues",
    }
    # Шаги генерации — все 12 шагов генератора (дополнение B6 §1).
    assert EDITOR_STEPS == STEPS
    assert REQUIRED_STEPS == (
        "concept",
        "plot",
        "locations",
        "location_contents",
        "npcs",
        "encounters",
        "clues",
    )


# --- запись раздела ---


def test_settings_go_to_the_settings_and_the_title_and_journal_intro_to_the_top_of_the_draft():
    draft = apply_section(AuthoringDraft(), "settings", settings_data())

    assert (draft.title, draft.journal_intro) == ("Маяк", "Маяк погас.")
    assert draft.settings.model_dump(mode="json") == {
        "pitch": "Маяк на мысу погас.",
        "tone": "морская готика",
        "target_hours": 2.0,
        "party_size": 4,
        "party_level": 3,
        "modules": ["campfire"],
        "profanity": False,
    }


def test_settings_without_a_title_and_journal_intro_keep_the_ones_already_written():
    before = AuthoringDraft(title="Старое", journal_intro="Старое начало")
    data = settings_data()
    del data["title"], data["journal_intro"]

    draft = apply_section(before, "settings", data)

    assert (draft.title, draft.journal_intro) == ("Старое", "Старое начало")
    assert draft.settings.party_size == 4


@pytest.mark.parametrize(
    ("section", "data", "model"),
    [
        ("world", world(), TypeAdapter(WorldInfo)),
        ("deities", [deity(), deity("ashen-one")], TypeAdapter(tuple[Deity, ...])),
        ("factions", [faction()], TypeAdapter(tuple[Faction, ...])),
        ("plot", plot(), TypeAdapter(Plot)),
    ],
)
def test_a_section_is_written_in_the_shape_of_the_b3_models(section, data, model):
    draft = apply_section(AuthoringDraft(), section, data)

    assert getattr(draft, section) == model.validate_python(data)


def test_writing_a_section_leaves_the_other_sections_alone():
    before = apply_section(AuthoringDraft(), "deities", [deity()])

    draft = apply_section(before, "world", world())

    assert draft.deities == before.deities
    assert draft.world is not None


# --- пределы на границе API (дополнение §7) ---


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"party_size": 7}, "party_size"),
        ({"party_size": 0}, "party_size"),
        ({"party_level": 6}, "party_level"),  # в таблице бюджета опыта уровни 1–5
        ({"party_level": 0}, "party_level"),
        ({"target_hours": 0}, "target_hours"),
        ({"target_hours": float("inf")}, "target_hours"),
        ({"title": "Я" * 101}, "title"),
        ({"pitch": "Я" * 2001}, "pitch"),
        ({"tone": "Я" * 301}, "tone"),
        ({"modules": ["campfire", "dragons"]}, "modules"),
    ],
)
def test_settings_outside_the_limits_of_the_api_are_refused(changes, field):
    found = problems_of("settings", settings_data(**changes))

    assert any(line.startswith(f"settings.{field}") for line in found), found


def test_settings_at_the_limits_are_accepted():
    draft = apply_section(
        AuthoringDraft(),
        "settings",
        settings_data(
            title="Я" * 100, pitch="Я" * 2000, tone="Я" * 300, party_size=6, party_level=5
        ),
    )

    assert (draft.settings.party_size, draft.settings.party_level) == (6, 5)


def test_settings_need_every_field_of_the_contract_but_the_title_and_journal_intro():
    data = settings_data()
    del data["tone"]

    assert problems_of("settings", data) == ["settings.tone: обязательное поле"]


@pytest.mark.parametrize(
    ("section", "data", "where"),
    [
        ("deities", [deity(f"d{n}") for n in range(7)], "deities"),
        ("factions", [faction(f"f{n}") for n in range(7)], "factions"),
        (
            "world",
            world(regions=[world()["regions"][0] | {"id": f"r{n}"} for n in range(7)]),
            "world",
        ),
        ("world", world(rumors=[world()["rumors"][0]] * 9), "world"),
        ("plot", plot(acts=[plot()["acts"][0] | {"id": f"a{n}"} for n in range(6)]), "plot"),
        ("plot", plot(endings=["Конец."] * 6), "plot"),
    ],
)
def test_sections_longer_than_a_model_answer_may_be_are_refused(section, data, where):
    found = problems_of(section, data)

    assert found and all(line.startswith(where) for line in found), found


def test_sections_as_long_as_a_model_answer_may_be_are_accepted():
    draft = AuthoringDraft()
    draft = apply_section(draft, "deities", [deity(f"d{n}") for n in range(6)])
    draft = apply_section(draft, "factions", [faction(f"f{n}") for n in range(6)])
    draft = apply_section(draft, "plot", plot(endings=["Конец."] * 5))

    assert (len(draft.deities), len(draft.factions), len(draft.plot.endings)) == (6, 6, 5)


@pytest.mark.parametrize(
    ("section", "data", "path"),
    [
        ("settings", settings_data(journal_intro="Я" * 2001), "settings.journal_intro"),
        ("world", world(history="Я" * 2001), "world.history"),
        ("world", world(regions=[region(name="Я" * 101)]), "world.regions.0.name"),
        ("world", world(regions=[region(description="Я" * 2001)]), "world.regions.0.description"),
        ("world", world(regions=[region(public="Я" * 301)]), "world.regions.0.public"),
        ("world", world(rumors=[rumor(text="Я" * 301)]), "world.rumors.0.text"),
        ("world", world(rumors=[rumor(public="Я" * 301)]), "world.rumors.0.public"),
        ("deities", [deity(name="Я" * 101)], "deities.0.name"),
        ("deities", [deity(title="Я" * 101)], "deities.0.title"),
        ("deities", [deity(domains=["Я" * 101])], "deities.0.domains.0"),
        ("deities", [deity(domains=["море"] * 13)], "deities.0.domains"),
        ("deities", [deity(symbol="Я" * 301)], "deities.0.symbol"),
        ("deities", [deity(dogma="Я" * 301)], "deities.0.dogma"),
        ("deities", [deity(appearance="Я" * 301)], "deities.0.appearance"),
        ("deities", [deity(attitude_to_mortals="Я" * 301)], "deities.0.attitude_to_mortals"),
        ("deities", [deity(secret="Я" * 301)], "deities.0.secret"),
        ("deities", [deity(public="Я" * 301)], "deities.0.public"),
        ("deities", [deity(avatar_statblock="я" * 301)], "deities.0.avatar_statblock"),
        ("factions", [faction(name="Я" * 101)], "factions.0.name"),
        ("factions", [faction(goal="Я" * 301)], "factions.0.goal"),
        ("factions", [faction(methods="Я" * 301)], "factions.0.methods"),
        ("factions", [faction(secret="Я" * 301)], "factions.0.secret"),
        ("factions", [faction(public="Я" * 301)], "factions.0.public"),
        ("plot", plot(acts=[act(title="Я" * 101)]), "plot.acts.0.title"),
        ("plot", plot(acts=[act(summary="Я" * 2001)]), "plot.acts.0.summary"),
        ("plot", plot(acts=[act(key_scenes=["Я" * 301])]), "plot.acts.0.key_scenes.0"),
        ("plot", plot(acts=[act(key_scenes=["Сцена."] * 13)]), "plot.acts.0.key_scenes"),
        ("plot", plot(endings=["Я" * 2001]), "plot.endings.0"),
    ],
)
def test_texts_of_sections_longer_than_a_model_answer_may_be_are_refused(section, data, path):
    # Дополнение §7 (fix-раунд 1): пределы полей схем ответа модели, списки строк без аналога ≤ 12.
    found = problems_of(section, data)

    assert any(line.startswith(f"{path}: ") for line in found), found
    assert all("ЯЯЯ" not in line and "яяя" not in line for line in found)


def test_texts_of_sections_at_the_limits_are_accepted():
    draft = apply_section(
        AuthoringDraft(),
        "deities",
        [deity(name="Я" * 100, dogma="Я" * 300, domains=["Я" * 100] * 12, secret="Я" * 300)],
    )
    draft = apply_section(draft, "settings", settings_data(journal_intro="Я" * 2000))
    draft = apply_section(
        draft, "plot", plot(acts=[act(summary="Я" * 2000, key_scenes=["Я" * 300] * 12)])
    )

    assert len(draft.deities[0].domains) == 12
    assert len(draft.journal_intro) == 2000
    assert len(draft.plot.acts[0].key_scenes) == 12


def test_repeated_modules_collapse_keeping_the_order_of_the_first_mention():
    draft = apply_section(
        AuthoringDraft(), "settings", settings_data(modules=["campfire", "bg3_flavor", "campfire"])
    )

    assert draft.settings.modules == ("campfire", "bg3_flavor")


# Самый длинный ответ, который допускают схемы ответа шагов (`authoring/schemas.py`): то, что даёт
# генерация, автор должен суметь сохранить обратно без правки (инвариант дополнения §7).
LONGEST_ANSWERS = {
    "concept": {
        "id": "a" * 64,
        "title": "Я" * 100,
        "pitch": "Я" * 2000,
        "tone": "Я" * 300,
        "journal_intro": "Я" * 2000,
        "revelations": [{"id": f"r{n}", "statement": "Я" * 2000} for n in range(5)],
    },
    "world": {
        "history": "Я" * 2000,
        "regions": [
            {"id": f"r{n}", "name": "Я" * 100, "description": "Я" * 2000, "public": "Я" * 300}
            for n in range(6)
        ],
        "rumors": [{"text": "Я" * 300, "true": True, "public": "Я" * 300}] * 8,
    },
    "pantheon": {
        "deities": [
            {
                "id": f"d{n}",
                "name": "Я" * 100,
                "title": "Я" * 100,
                "domains": ["Я" * 100] * 4,
                "symbol": "Я" * 300,
                "dogma": "Я" * 300,
                "appearance": "Я" * 300,
                "attitude_to_mortals": "Я" * 300,
                "secret": "Я" * 300,
                "public": "Я" * 300,
                "avatar_statblock": "a" * 64,
            }
            for n in range(6)
        ]
    },
    "factions": {
        "factions": [
            {
                "id": f"f{n}",
                "name": "Я" * 100,
                "goal": "Я" * 300,
                "methods": "Я" * 300,
                "leader": "npc:" + "a" * 64,
                "base_location": "a" * 64,
                "secret": "Я" * 300,
                "public": "Я" * 300,
                "attitude_to_party": -3,
            }
            for n in range(6)
        ]
    },
    "plot": {
        "acts": [
            {
                "id": f"a{n}",
                "title": "Я" * 100,
                "summary": "Я" * 2000,
                "key_scenes": ["Я" * 300] * 6,
            }
            for n in range(5)
        ],
        "endings": ["Я" * 2000] * 5,
        "villain": "deity:" + "a" * 64,
    },
    "locations": {
        "start_location": "l0",
        "finale_location": "l11",
        "locations": [
            {
                "id": f"l{n}",
                "name": "Я" * 100,
                "region": "Я" * 300,
                "public": "Я" * 300,
                "known_at_start": True,
                "exits": [
                    {
                        "id": f"x{k}",
                        "to": "a" * 64,
                        "direction": "Я" * 40,
                        "description": "Я" * 300,
                        "locked_by": "item:" + "a" * 64,
                        "hidden_dc": 30,
                    }
                    for k in range(6)
                ],
            }
            for n in range(12)
        ],
    },
    "location_contents": {
        "read_aloud": "Я" * 2000,
        "dm_notes": "Я" * 2000,
        "features": [
            {
                "id": f"f{n}",
                "name": "Я" * 100,
                "description": "Я" * 2000,
                "check": {"ability": "wis", "skill": "perception", "dc": 30},
                "locked_by": "item:" + "a" * 64,
                "reveals": [{"kind": "item", "id": "a" * 64}] * 2,
            }
            for n in range(5)
        ],
        "secrets": [
            {
                "id": f"s{n}",
                "text": "Я" * 2000,
                "check": {"ability": "int", "skill": "investigation", "dc": 30},
                "reveals": [{"kind": "flag", "id": "a" * 64}] * 3,
            }
            for n in range(3)
        ],
    },
    "npcs": {
        "npcs": [
            {
                "id": f"n{n}",
                "name": "Я" * 100,
                "role": "Я" * 300,
                "appearance": "Я" * 300,
                "manner": "Я" * 300,
                "motive": "Я" * 300,
                "secret": "Я" * 300,
                "knows": ["Я" * 300] * 6,
                "attitude": -3,
                "location_id": "a" * 64,
                "statblock": "a" * 64,
                "public": "Я" * 300,
                "known_at_start": True,
            }
            for n in range(12)
        ]
    },
    "relations": {
        "relations": [
            {
                "id": f"r{n}",
                "a": "npc:" + "a" * 64,
                "b": "deity:" + "b" * 64,
                "kind": "other",
                "note": "Я" * 300,
                "secret": True,
                "mutual": False,
            }
            for n in range(24)
        ]
    },
    "items": {
        "items": [
            {
                "id": f"i{n}",
                "name": "Я" * 100,
                "description": "Я" * 2000,
                "base_item": "a" * 64,
                "holder": "location:" + "a" * 64,
                "public": "Я" * 300,
                "known_at_start": True,
            }
            for n in range(20)
        ]
    },
    # Бой: самые длинные тексты и все четыре группы; монстров в бою — не больше 20 (предел раздела
    # и валидатора, дополнение B6 §5), хотя схема ответа пускает до 20 в каждой группе.
    "encounters": {
        "encounters": [
            {
                "id": f"e{n}",
                "location_id": f"{n}" + "a" * 63,  # у локации один бой
                "trigger": "Я" * 2000,
                "tactics": "Я" * 2000,
                "monsters": [{"monster_id": "a" * 64, "count": 5}] * 4,
                "boss": True,
            }
            for n in range(8)
        ],
        "monsters": [
            {
                "id": f"m{n}",
                "name": "Я" * 100,
                "name_en": "A" * 100,
                "cr": 3,
                "xp": 700,
                "ac": 15,
                "hp": 90,
                "hp_dice": "12d8+36",
                "speed": 30,
                "scores": {"str": 10, "dex": 14, "con": 16, "int": 13, "wis": 17, "cha": 15},
                "traits": ["Я" * 2000] * 12,
                "tags": ["Я" * 300] * 12,
                "actions": [
                    {"id": f"a{k}", "name": "Я" * 100, "kind": "other", "summary": "Я" * 2000}
                    for k in range(12)
                ],
            }
            for n in range(3)
        ],
    },
    "clues": {
        "revelations": [
            {
                "revelation_id": f"r{n}",
                "clues": [
                    {"id": f"c{n}-{k}", "description": "Я" * 2000, "where": "npc:" + "a" * 64}
                    for k in range(8)
                ],
            }
            for n in range(5)
        ]
    },
}


def section_data(draft: AuthoringDraft, section: str):
    """Раздел черновика в форме `Draft` — так его видит и сохраняет webapp."""
    dumped = draft.model_dump(mode="json")
    if section == "settings":
        return dumped["settings"] | {"title": draft.title, "journal_intro": draft.journal_intro}
    if section == "locations":
        return {name: dumped[name] for name in ("locations", "start_location", "finale_location")}
    return dumped[section]


@pytest.mark.parametrize("section", list(SECTION_STEPS))
async def test_the_longest_answer_a_step_may_give_is_saved_back_by_hand(section):
    step = SECTION_STEPS[section]
    provider = RecordingProvider(
        {f"generator.{name}": LONGEST_ANSWERS[name] for name in (step, "concept")}
    )
    # Улики наполняют открытия замысла: без них шагу нечего писать.
    base = (
        await run_step(provider, AuthoringDraft(), "concept")
        if step == "clues"
        else AuthoringDraft()
    )
    generated = await run_step(provider, base, step)  # ответ прошёл схему шага

    saved = apply_section(AuthoringDraft(), section, section_data(generated, section))

    assert section_data(saved, section) == section_data(generated, section)


async def test_the_longest_filling_a_step_may_give_is_saved_back_by_hand():
    provider = RecordingProvider(
        {f"generator.{step}": LONGEST_ANSWERS[step] for step in ("locations", "location_contents")}
    )
    graph = await run_step(provider, AuthoringDraft(), "locations")
    filled = await run_step(provider, graph, "location_contents")

    saved = apply_section(AuthoringDraft(), "locations", section_data(filled, "locations"))

    assert section_data(saved, "locations") == section_data(filled, "locations")
    assert all(location.read_aloud for location in saved.locations)


# Типичные отказы ручного ввода (находка minor reviewer B4): автор видит их в редакторе.
TYPICAL_REFUSALS = {
    "id не латиницей": ("deities", [deity("тиамат")], "deities.0.id"),
    "семь игроков": ("settings", settings_data(party_size=7), "settings.party_size"),
    "длинное название": ("settings", settings_data(title="Я" * 101), "settings.title"),
    "лишнее поле": ("settings", settings_data(extra=1), "settings.extra"),
    "нет поля": (
        "deities",
        [{k: v for k, v in deity().items() if k != "dogma"}],
        "deities.0.dogma",
    ),
    "неверный тип": ("settings", settings_data(party_size="шесть"), "settings.party_size"),
    "строка вместо списка": ("deities", [deity(domains="море")], "deities.0.domains"),
    "не объект": ("world", None, "world"),
    "отношение вне шкалы": ("factions", [faction(attitude_to_party=9)], "factions.0"),
    "ноль часов": ("settings", settings_data(target_hours=0), "settings.target_hours"),
    "бесконечные часы": ("settings", settings_data(target_hours=float("inf")), "settings.target"),
    "лидер не ссылкой": ("factions", [faction(leader="бармен")], "factions.0.leader"),
    "много актов": ("plot", plot(acts=[act(id=f"a{n}") for n in range(6)]), "plot.acts"),
    "флажок не флажок": ("settings", settings_data(profanity="иногда"), "settings.profanity"),
}


def assert_in_russian(found: list[str], path: str) -> None:
    """Отказ называет поле `path`, а сообщения всех строк — по-русски, без фраз pydantic."""
    assert any(line.startswith(path) for line in found), found
    for line in found:
        message = line.split(": ", 1)[1]
        assert message and not re.search(r"[A-Za-z]", message), line


@pytest.mark.parametrize("case", TYPICAL_REFUSALS)
def test_problems_of_typical_refusals_are_in_russian_without_pydantic_phrases(case):
    section, data, path = TYPICAL_REFUSALS[case]

    assert_in_russian(problems_of(section, data), path)


def test_problems_of_common_refusals_read_as_the_contract_says():
    assert problems_of("settings", settings_data(party_size=7)) == [
        "settings.party_size: не больше 6"
    ]
    assert problems_of("settings", settings_data(title="Я" * 101)) == [
        "settings.title: не длиннее 100 знаков"
    ]
    assert problems_of("settings", settings_data(extra=1)) == ["settings.extra: лишнее поле"]
    [line] = problems_of("deities", [deity("тиамат")])
    assert line.startswith("deities.0.id: ") and "латиница" in line


def test_own_russian_messages_of_the_checks_stay_as_they_are():
    [line] = problems_of("settings", settings_data(party_level=6))

    assert line == "settings.party_level: уровень героев — от 1 до 5"


def test_repeated_ids_in_a_section_are_refused_by_the_rules_of_the_draft():
    found = problems_of("deities", [deity(), deity()])

    assert len(found) == 1 and "selmara" in found[0], found


@pytest.mark.parametrize(
    ("section", "data"),
    [
        ("settings", settings_data(extra=1)),
        ("deities", [deity(extra=1)]),
        ("world", world(extra=1)),
        ("plot", plot(extra=1)),
        ("deities", {"deities": []}),  # раздел-список приходит списком
        ("world", None),
    ],
)
def test_extra_fields_and_a_wrong_shape_are_refused(section, data):
    assert problems_of(section, data)


def test_problems_name_the_path_and_never_repeat_the_input():
    found = problems_of(
        "deities", [deity(name=None, domains=SECRET_INPUT, title={"x": SECRET_INPUT})]
    )

    assert found and all(line.startswith("deities.0.") for line in found), found
    assert all(SECRET_INPUT not in line for line in found)


def test_problems_of_a_validation_error_are_lines_with_a_path_and_a_message():
    try:
        TypeAdapter(tuple[Deity, ...]).validate_python([{"id": SECRET_INPUT}])
    except ValidationError as error:
        lines = problems(error.errors(include_input=False), "deities")

    assert lines[0].startswith("deities.0.")
    assert all(": " in line and SECRET_INPUT not in line for line in lines)


# --- статусы шагов (дополнение §3) ---


def test_every_step_of_the_generator_has_a_status_and_the_time_of_its_last_write():
    draft = apply_section(AuthoringDraft(), "world", world())

    statuses = step_statuses(draft, {"world": "2026-10-25T00:00:00+00:00"})

    assert set(statuses) == set(STEPS)
    assert statuses["world"] == {"status": "ready", "updated_at": "2026-10-25T00:00:00+00:00"}
    assert statuses["pantheon"] == {"status": "empty", "updated_at": None}
    assert statuses["plot"]["status"] == "empty"


def test_every_step_of_an_empty_draft_is_empty_the_filling_steps_too():
    # Дополнение §3 (уточнение dispatcher): шаг-наполнитель пуст и тогда, когда пуст его
    # раздел-основа — наполнять ещё нечего.
    statuses = step_statuses(AuthoringDraft(), {})

    assert {step: status["status"] for step, status in statuses.items()} == dict.fromkeys(
        STEPS, "empty"
    )


def test_the_filling_steps_have_content_once_their_base_section_is_filled():
    revelation = {"id": "ash-truth", "statement": "Маяк погасил жрец.", "clues": []}
    without_clues = AuthoringDraft.model_validate({"revelations": [revelation]})
    complete = AuthoringDraft.model_validate(
        {
            "revelations": [
                revelation | {"clues": [{"id": "c1", "description": "Пепел.", "where": "npc:x"}]}
            ],
            "locations": [{"id": "harbor", "name": "Гавань", "read_aloud": "Пахнет рыбой."}],
        }
    )

    # Ни у одного открытия нет улик — шаг улик пуст (дополнение B6 §3).
    assert step_statuses(without_clues, {})["clues"]["status"] == "empty"
    filled = step_statuses(complete, {})
    assert filled["clues"]["status"] != "empty"
    assert filled["location_contents"]["status"] != "empty"


# --- пустота по содержимому раздела (дополнение §3, fix-раунд 1: находка major reviewer B4) ---


def test_a_concept_written_by_hand_is_a_draft_until_it_has_the_id_of_the_adventure():
    draft = apply_section(AuthoringDraft(), "settings", settings_data())

    assert step_statuses(draft, {})["concept"]["status"] == "draft"
    assert draft_report(draft)["readiness_percent"] == 100 // len(REQUIRED_STEPS) == 14


def test_a_concept_with_the_id_of_the_adventure_is_ready():
    draft = apply_section(AuthoringDraft(), "settings", settings_data())

    statuses = step_statuses(draft.model_copy(update={"id": "crow-lighthouse"}), {})

    assert statuses["concept"]["status"] == "ready"


BLANK_CONCEPT = {"title": "  ", "journal_intro": "\n", "pitch": "", "tone": " \t"}


def test_a_concept_of_blank_texts_and_default_numbers_is_empty():
    draft = apply_section(AuthoringDraft(), "settings", settings_data(**BLANK_CONCEPT))

    assert step_statuses(draft, {})["concept"]["status"] == "empty"
    assert draft_report(draft)["readiness_percent"] == 0


@pytest.mark.parametrize("field", ["title", "journal_intro", "pitch", "tone"])
def test_any_text_of_the_concept_is_content(field):
    data = settings_data(**BLANK_CONCEPT) | {field: "Маяк"}

    draft = apply_section(AuthoringDraft(), "settings", data)

    assert step_statuses(draft, {})["concept"]["status"] == "draft"


@pytest.mark.parametrize(
    "data", [{"history": ""}, {"history": "  \n", "regions": [], "rumors": []}]
)
def test_a_saved_world_without_history_regions_and_rumors_is_empty(data):
    draft = apply_section(AuthoringDraft(), "world", data)

    assert step_statuses(draft, {})["world"]["status"] == "empty"


@pytest.mark.parametrize(
    "data", [{"history": "Мыс."}, {"regions": [region()]}, {"rumors": [rumor()]}]
)
def test_a_world_with_history_regions_or_rumors_has_content(data):
    draft = apply_section(AuthoringDraft(), "world", data)

    assert step_statuses(draft, {})["world"]["status"] != "empty"


def test_a_saved_plot_without_acts_endings_and_villain_is_empty():
    draft = apply_section(AuthoringDraft(), "plot", {"acts": [], "endings": [], "villain": None})

    assert step_statuses(draft, {})["plot"]["status"] == "empty"
    assert draft_report(draft)["readiness_percent"] == 0


@pytest.mark.parametrize(
    ("data", "status"),
    [({"endings": ["Конец."]}, "ready"), ({"villain": "deity:nobody"}, "draft")],
)
def test_a_plot_with_only_endings_or_a_villain_has_content(data, status):
    draft = apply_section(AuthoringDraft(), "plot", data)

    assert step_statuses(draft, {})["plot"]["status"] == status


def test_gods_and_factions_locked_by_the_author_are_content():
    draft = apply_section(AuthoringDraft(), "deities", [deity()])
    draft = apply_section(draft, "factions", [faction()])
    locked = draft.model_copy(update={"locks": frozenset({"deity:selmara", "faction:smugglers"})})

    statuses = step_statuses(locked, {})

    # Генератору шаг пуст (закреплённое он не пишет заново), автору — нет: боги написаны.
    assert is_empty(locked, "pantheon") and is_empty(locked, "factions")
    assert statuses["pantheon"]["status"] != "empty"
    assert statuses["factions"]["status"] != "empty"


def test_a_filled_step_with_an_error_of_its_own_is_a_draft():
    # Злодей — бог, которого нет: ссылку чинит шаг сюжета (бога создаёт более ранний шаг).
    draft = apply_section(AuthoringDraft(), "plot", plot(villain="deity:nobody"))

    assert step_statuses(draft, {})["plot"]["status"] == "draft"


def test_a_filled_step_whose_problems_belong_to_later_steps_is_ready():
    # Лидер фракции — NPC, которого ещё нет: ссылку вперёд чинит шаг NPC, а не шаг фракций.
    draft = apply_section(AuthoringDraft(), "factions", [faction(leader="npc:nobody")])

    assert step_statuses(draft, {})["factions"]["status"] == "ready"


# --- отчёт проверки (дополнение §8) ---


def test_the_report_of_an_empty_draft_has_no_errors_of_steps_not_written_yet():
    report = draft_report(AuthoringDraft())

    assert set(report) == {"errors", "warnings", "score", "estimated_minutes", "readiness_percent"}
    assert report["errors"] == []
    for finding in report["warnings"]:
        assert set(finding) == {"code", "message", "ref", "step"}


def test_a_finding_of_a_filled_step_names_its_step():
    draft = apply_section(AuthoringDraft(), "plot", plot(villain="deity:nobody"))

    report = draft_report(draft)

    assert [(f["code"], f["ref"], f["step"]) for f in report["errors"]] == [
        ("dangling_ref", "adventure", "plot")
    ]
    assert "villain" in report["errors"][0]["message"]


def test_an_empty_draft_is_not_ready_at_all():
    assert draft_report(AuthoringDraft())["readiness_percent"] == 0


def test_readiness_is_the_share_of_required_steps_with_content_rounded_down():
    draft = v2_draft().model_copy(update={"id": "crow-lighthouse"})  # замысел написан
    draft = apply_section(draft, "plot", plot())

    # Написаны concept и plot; локаций и открытий нет, значит их наполнение тоже пусто: 2 из 7.
    assert draft_report(draft)["readiness_percent"] == 2 * 100 // len(REQUIRED_STEPS) == 28


async def test_a_complete_draft_is_ready_and_an_error_keeps_it_below_one_hundred():
    provider = FakeProvider(load_scenario(V2_SCENARIO))
    complete = (await generate(provider, v2_draft())).draft

    ready = draft_report(complete)
    broken = draft_report(
        complete.model_copy(
            update={"plot": complete.plot.model_copy(update={"villain": "deity:nobody"})}
        )
    )

    assert (ready["errors"], ready["readiness_percent"]) == ([], 100)
    assert broken["errors"] and broken["readiness_percent"] == 99
    assert broken["score"] < ready["score"]
    assert ready["estimated_minutes"] > 0


# --- финальное покрытие блока 4: находки пустых шагов и округление доли готовности ---


def test_the_report_drops_findings_owned_by_steps_that_are_still_empty():
    # Лидер фракции и её база — NPC и локация, которых ещё нет: проверка их находит, но чинить их
    # будут пустые шаги NPC и локаций, поэтому в отчёте по заполненным разделам их нет.
    draft = apply_section(
        AuthoringDraft(), "factions", [faction(leader="npc:nobody", base_location="nowhere")]
    )
    found = check(to_adventure(draft), load_srd()).issues
    owners = {owner_step(issue) for issue in found}

    report = draft_report(draft)

    assert {"npcs", "locations"} & owners  # находки пустых шагов в проверке есть
    statuses = step_statuses(draft, {})
    empty = {step for step, state in statuses.items() if state["status"] == "empty"}
    assert all(f["step"] not in empty for f in report["errors"] + report["warnings"]), report
    assert report["errors"] == []


def test_readiness_of_one_required_step_out_of_seven_is_rounded_down():
    draft = v2_draft().model_copy(update={"id": "crow-lighthouse"})  # написан только замысел

    assert draft_report(draft)["readiness_percent"] == 100 // len(REQUIRED_STEPS) == 14


def test_readiness_counts_the_steps_and_not_their_errors_below_99():
    # Доля непустых шагов с ошибкой не поднимается до 99: предел — сверху, а не снизу.
    draft = apply_section(AuthoringDraft(), "plot", plot(villain="deity:nobody"))

    report = draft_report(draft)

    assert report["errors"] and report["readiness_percent"] == 100 // len(REQUIRED_STEPS)


# --- разделы блока 5: форма, пределы, замки удалённых (дополнение B5 §1, §4, §5) ---


def graph_only() -> dict:
    """«Карта» демо без наполнения: имена, края и выходы, как их пишет шаг locations."""
    data = locations_data()
    for location in data["locations"]:
        location |= {"read_aloud": "", "dm_notes": "", "features": [], "secrets": []}
    return data


def npc(**changes) -> dict:
    return npcs_data()[0] | changes


def relation(**changes) -> dict:
    return relations_data()[0] | changes


def item(**changes) -> dict:
    return items_data()[0] | changes


def secret(**changes) -> dict:
    return location_at(LIGHTHOUSE)["secrets"][0] | changes


def exit_(**changes) -> dict:
    return location_at(HARBOR)["exits"][0] | changes


def feature(**changes) -> dict:
    return location_at(HARBOR)["features"][0] | changes


def test_the_map_is_written_in_the_shape_of_the_engine_models_with_its_start_and_finale():
    data = locations_data()

    draft = apply_section(AuthoringDraft(), "locations", data)

    assert draft.locations == TypeAdapter(tuple[Location, ...]).validate_python(data["locations"])
    assert (draft.start_location, draft.finale_location) == ("harbor", "lamp-room")


@pytest.mark.parametrize(
    ("section", "data", "model"),
    [
        ("npcs", npcs_data(), Npc),
        ("relations", relations_data(), Relation),
        ("items", items_data(), Item),
    ],
)
def test_a_list_section_of_block_5_is_written_in_the_shape_of_the_engine_models(
    section, data, model
):
    draft = apply_section(AuthoringDraft(), section, data)

    assert getattr(draft, section) == TypeAdapter(tuple[model, ...]).validate_python(data)


@pytest.mark.parametrize("field", ["start_location", "finale_location"])
def test_a_start_or_finale_outside_the_map_is_refused_without_the_value(field):
    found = problems_of("locations", locations_data() | {field: SECRET_INPUT})

    assert found == [f"locations.{field}: такой локации на карте нет"]


def test_a_map_without_a_start_and_a_finale_is_accepted():
    draft = apply_section(
        AuthoringDraft(),
        "locations",
        graph_only() | {"start_location": None, "finale_location": None},
    )

    assert (draft.start_location, draft.finale_location) == (None, None)


def test_a_map_needs_its_three_fields():
    found = problems_of("locations", {"locations": []})

    assert sorted(found) == [
        "locations.finale_location: обязательное поле",
        "locations.start_location: обязательное поле",
    ]


def test_relations_inside_an_npc_are_refused_with_a_hint_to_the_relations_step():
    found = problems_of("npcs", [npc(relations=[{"npc_id": "bram", "kind": "rival"}])])

    assert found == ["npcs.0.relations: связи NPC — на шаге «Связи»"]


@pytest.mark.parametrize(
    ("section", "data", "path"),
    [
        ("locations", map_with(name="Я" * 101), "locations.locations.0.name"),
        ("locations", map_with(region="Я" * 301), "locations.locations.0.region"),
        ("locations", map_with(read_aloud="Я" * 2001), "locations.locations.0.read_aloud"),
        ("locations", map_with(dm_notes="Я" * 2001), "locations.locations.0.dm_notes"),
        ("locations", map_with(public="Я" * 301), "locations.locations.0.public"),
        ("locations", map_with(encounter_id="я" * 301), "locations.locations.0.encounter_id"),
        ("locations", map_with(exits=[exit_()] * 7), "locations.locations.0.exits"),
        ("locations", map_with(exits=[exit_(to="я" * 301)]), "locations.locations.0.exits.0.to"),
        (
            "locations",
            map_with(exits=[exit_(direction="Я" * 41)]),
            "locations.locations.0.exits.0.direction",
        ),
        (
            "locations",
            map_with(exits=[exit_(description="Я" * 301)]),
            "locations.locations.0.exits.0.description",
        ),
        (
            "locations",
            map_with(exits=[exit_(locked_by="я" * 301)]),
            "locations.locations.0.exits.0.locked_by",
        ),
        ("locations", map_with(features=[feature()] * 6), "locations.locations.0.features"),
        (
            "locations",
            map_with(features=[feature(name="Я" * 101)]),
            "locations.locations.0.features.0.name",
        ),
        (
            "locations",
            map_with(features=[feature(description="Я" * 2001)]),
            "locations.locations.0.features.0.description",
        ),
        (
            "locations",
            map_with(features=[cupboard(reveals=[{"kind": "item", "id": "k"}] * 3)]),
            "locations.locations.0.features.0.reveals",
        ),
        (
            "locations",
            map_with(features=[cupboard(reveals=[{"kind": "item", "id": "я" * 301}])]),
            "locations.locations.0.features.0.reveals.0.id",
        ),
        (
            "locations",
            map_with(LIGHTHOUSE, secrets=[secret()] * 4),
            "locations.locations.2.secrets",
        ),
        (
            "locations",
            map_with(LIGHTHOUSE, secrets=[secret(text="Я" * 2001)]),
            "locations.locations.2.secrets.0.text",
        ),
        (
            "locations",
            map_with(LIGHTHOUSE, secrets=[secret(reveals=[{"kind": "flag", "id": "f"}] * 4)]),
            "locations.locations.2.secrets.0.reveals",
        ),
        (
            "locations",
            map_with(zones=[{"id": f"z{n}", "name": "зона"} for n in range(13)]),
            "locations.locations.0.zones",
        ),
        (
            "locations",
            map_with(zones=[{"id": "here", "name": "Я" * 101}]),
            "locations.locations.0.zones.0.name",
        ),
        (
            "locations",
            map_with(links=[{"a": "here", "b": "here", "distance": "near"}] * 13),
            "locations.locations.0.links",
        ),
        ("npcs", [npc(name="Я" * 101)], "npcs.0.name"),
        ("npcs", [npc(role="Я" * 301)], "npcs.0.role"),
        ("npcs", [npc(appearance="Я" * 301)], "npcs.0.appearance"),
        ("npcs", [npc(manner="Я" * 301)], "npcs.0.manner"),
        ("npcs", [npc(motive="Я" * 301)], "npcs.0.motive"),
        ("npcs", [npc(secret="Я" * 301)], "npcs.0.secret"),
        ("npcs", [npc(knows=["Я" * 301])], "npcs.0.knows.0"),
        ("npcs", [npc(knows=["Знает."] * 7)], "npcs.0.knows"),
        ("npcs", [npc(public="Я" * 301)], "npcs.0.public"),
        ("npcs", [npc(location_id="я" * 301)], "npcs.0.location_id"),
        ("npcs", [npc(statblock="я" * 301)], "npcs.0.statblock"),
        ("relations", [relation(note="Я" * 301)], "relations.0.note"),
        ("items", [item(name="Я" * 101)], "items.0.name"),
        ("items", [item(description="Я" * 2001)], "items.0.description"),
        ("items", [item(public="Я" * 301)], "items.0.public"),
        ("items", [item(holder="я" * 301)], "items.0.holder"),
        ("items", [item(base_item="я" * 301)], "items.0.base_item"),
    ],
)
def test_texts_and_lists_of_block_5_longer_than_a_model_answer_may_be_are_refused(
    section, data, path
):
    # Дополнение B5 §4: пределы полей схем ответа модели; без аналога — Line 300, списки ≤ 12.
    found = problems_of(section, data)

    assert any(line.startswith(f"{path}: ") for line in found), found
    assert all("ЯЯЯ" not in line and "яяя" not in line for line in found)


def test_texts_and_lists_of_block_5_at_the_limits_are_accepted():
    harbor = location_at(HARBOR) | {
        "name": "Я" * 100,
        "read_aloud": "Я" * 2000,
        "exits": [exit_(id=f"x{n}", direction="Я" * 40) for n in range(6)],
        "features": [feature(id=f"f{n}") for n in range(4)] + [cupboard()],
        "secrets": [secret(id=f"s{n}") for n in range(3)],
        "encounter_id": "я" * 300,
        "zones": [{"id": f"z{n}", "name": "Я" * 100} for n in range(12)],
        "links": [{"a": "z0", "b": f"z{n}", "distance": "far"} for n in range(12)],
    }
    data = {"locations": [harbor], "start_location": "harbor", "finale_location": None}

    draft = apply_section(AuthoringDraft(), "locations", data)
    draft = apply_section(draft, "npcs", [npc(knows=["Я" * 300] * 6, secret="Я" * 300)])
    draft = apply_section(draft, "relations", [relation(note="Я" * 300)])
    draft = apply_section(draft, "items", [item(description="Я" * 2000, holder="я" * 300)])

    [location] = draft.locations
    assert (len(location.exits), len(location.features), len(location.zones)) == (6, 5, 12)
    assert len(draft.npcs[0].knows) == 6


def test_a_location_without_exits_and_with_the_default_zone_is_accepted():
    bare = {
        key: value for key, value in location_at(HARBOR).items() if key not in ("zones", "links")
    }

    draft = apply_section(
        AuthoringDraft(),
        "locations",
        {"locations": [bare | {"exits": []}], "start_location": None, "finale_location": None},
    )

    [location] = draft.locations
    assert location.exits == () and [zone.id for zone in location.zones] == ["here"]


def test_repeated_ids_inside_locations_are_accepted_and_left_to_the_check():
    data = map_with(features=[feature(), feature()], exits=[exit_(), exit_()])

    draft = apply_section(AuthoringDraft(), "locations", data)

    codes = {(issue.code, issue.ref) for issue in check(to_adventure(draft), load_srd()).issues}
    assert ("schema", "location:harbor/feature:harbor-board") in codes
    assert ("schema", "location:harbor/exit:to-cliff-path") in codes


@pytest.mark.parametrize(
    ("section", "data"),
    [
        ("locations", locations_data() | {"extra": 1}),
        ("locations", map_with(zones=[{"id": "here", "name": "здесь", "x": 1}])),
        ("npcs", [npc(extra=1)]),
        ("relations", [relation(extra=1)]),
        ("items", [item(extra=1)]),
        ("locations", locations_data()["locations"]),  # «Карта» приходит объектом
        ("npcs", {"npcs": npcs_data()}),  # раздел-список приходит списком
    ],
)
def test_extra_fields_and_a_wrong_shape_of_block_5_are_refused(section, data):
    assert problems_of(section, data)


def test_writing_a_section_without_an_entity_drops_its_lock_and_only_its_lock():
    # Дополнение B5 §5: замок удалённой сущности снимается в той же записи, иначе новая сущность
    # с тем же id молча оказалась бы закреплённой; шаг и другие замки не трогаются.
    draft = apply_section(AuthoringDraft(), "locations", locations_data())
    draft = draft.model_copy(
        update={"locks": frozenset({"location:harbor", "location:lighthouse", "step:locations"})}
    )
    without = locations_data()
    del without["locations"][HARBOR]
    without["start_location"] = "cliff-path"

    changed = apply_section(draft, "locations", without)

    assert changed.locks == {"location:lighthouse", "step:locations"}
    assert apply_section(changed, "locations", locations_data()).locks == changed.locks


def test_writing_one_section_keeps_the_locks_of_the_others():
    draft = apply_section(AuthoringDraft(), "npcs", npcs_data())
    draft = draft.model_copy(update={"locks": frozenset({"npc:ilsa", "deity:selmara"})})

    changed = apply_section(draft, "locations", locations_data())

    assert changed.locks == {"npc:ilsa", "deity:selmara"}


# --- статусы шагов блока 5 по содержимому (дополнение B5 §3) ---


def statuses_of(draft: AuthoringDraft) -> dict[str, str]:
    return {step: state["status"] for step, state in step_statuses(draft, {}).items()}


def test_the_map_has_content_once_it_has_a_location_locked_ones_too():
    draft = apply_section(AuthoringDraft(), "locations", graph_only())
    locked = draft.model_copy(
        update={"locks": frozenset(f"location:{loc.id}" for loc in draft.locations)}
    )

    assert statuses_of(AuthoringDraft())["locations"] == "empty"
    assert statuses_of(draft)["locations"] == "ready"
    # Генератору шаг пуст (закреплённое он не пишет заново), автору — нет: карта нарисована.
    assert is_empty(locked, "locations")
    assert statuses_of(locked)["locations"] == "ready"


@pytest.mark.parametrize(
    "contents",
    [
        {"read_aloud": "Пахнет рыбой."},
        {"dm_notes": "Ильза ждёт у доски."},
        {"features": [feature()]},
        {"secrets": [secret()]},
    ],
)
def test_the_filling_has_content_once_a_location_has_text_details_or_secrets(contents):
    data = graph_only()
    data["locations"][HARBOR] |= contents

    draft = apply_section(AuthoringDraft(), "locations", data)

    assert statuses_of(draft)["location_contents"] == "draft"  # у остальных нет текста вслух


def test_the_filling_has_content_once_an_item_lies_in_a_location():
    draft = apply_section(AuthoringDraft(), "locations", graph_only())

    with_item = apply_section(draft, "items", [item(holder="location:harbor")])
    elsewhere = apply_section(draft, "items", [item(holder="npc:ilsa")])

    assert statuses_of(with_item)["location_contents"] == "draft"
    assert statuses_of(elsewhere)["location_contents"] == "empty"


def test_a_map_of_blank_texts_has_no_filling():
    data = graph_only()
    for location in data["locations"]:
        location |= {"read_aloud": "  \n", "dm_notes": "\t"}

    draft = apply_section(AuthoringDraft(), "locations", data)

    assert statuses_of(draft)["location_contents"] == "empty"
    assert statuses_of(AuthoringDraft())["location_contents"] == "empty"


def test_the_filling_is_ready_when_every_location_has_text_to_read_aloud_and_no_errors():
    draft = apply_section(AuthoringDraft(), "locations", locations_data())
    blank = locations_data()
    blank["locations"][LIGHTHOUSE]["read_aloud"] = " "

    assert statuses_of(draft)["location_contents"] == "ready"
    assert statuses_of(apply_section(draft, "locations", blank))["location_contents"] == "draft"


def test_the_filling_with_an_error_of_its_own_is_a_draft():
    # Тайна раскрывает предмет, который лежит не в её локации: это чинит наполнение.
    draft = apply_section(AuthoringDraft(), "items", [item(id="keeper-ring", holder="npc:ilsa")])
    draft = apply_section(draft, "locations", locations_data())

    assert statuses_of(draft)["location_contents"] == "draft"


@pytest.mark.parametrize(
    ("section", "data", "kind"),
    [("npcs", npcs_data(), "npc"), ("relations", relations_data(), "relation")],
)
def test_npcs_and_relations_have_content_once_written_locked_ones_too(section, data, kind):
    draft = apply_section(AuthoringDraft(), section, data)
    locked = draft.model_copy(
        update={"locks": frozenset(f"{kind}:{entity.id}" for entity in getattr(draft, section))}
    )

    assert statuses_of(AuthoringDraft())[section] == "empty"
    assert statuses_of(draft)[section] != "empty"
    assert is_empty(locked, section) and statuses_of(locked)[section] != "empty"


def test_readiness_counts_the_map_and_its_filling_by_their_content():
    concept = v2_draft().model_copy(update={"id": "crow-lighthouse"})
    graph = apply_section(concept, "locations", graph_only())
    filled = apply_section(concept, "locations", locations_data())
    with_npcs = apply_section(filled, "npcs", npcs_data())

    # Обязательных шагов семь: замысел и карта — 2 из 7, с наполнением — 3, с NPC — 4.
    assert draft_report(graph)["readiness_percent"] == 2 * 100 // len(REQUIRED_STEPS) == 28
    assert draft_report(filled)["readiness_percent"] == 3 * 100 // len(REQUIRED_STEPS) == 42
    assert draft_report(with_npcs)["readiness_percent"] == 4 * 100 // len(REQUIRED_STEPS) == 57


def test_the_report_keeps_findings_of_the_map_and_drops_those_of_an_empty_filling():
    # Выход ведёт в локацию, которой нет: находку чинит карта (она не пуста), а пустое наполнение
    # своих находок в отчёт не даёт.
    data = graph_only()
    data["locations"][HARBOR]["exits"][0]["to"] = "nowhere"
    draft = apply_section(AuthoringDraft(), "locations", data)

    report = draft_report(draft)

    found = {(f["code"], f["ref"], f["step"]) for f in report["errors"] + report["warnings"]}
    assert ("dangling_ref", "location:harbor/exit:to-cliff-path", "locations") in found
    assert all(step != "location_contents" for _, _, step in found), found


# --- финальное покрытие блока 5: типичные отказы, словарь связей, шкала отношения ---


def without_field(entity: dict, field: str) -> dict:
    return {key: value for key, value in entity.items() if key != field}


# Типичные отказы ручного ввода разделов блока 5: автор видит их в редакторе карты, наполнения,
# персонажей и связей.
B5_TYPICAL_REFUSALS = {
    "id локации не латиницей": ("locations", map_with(id="гавань"), "locations.locations.0.id"),
    "нет текста вслух": (
        "locations",
        {"locations": [without_field(location_at(HARBOR), "read_aloud")]}
        | {"start_location": None, "finale_location": None},
        "locations.locations.0.read_aloud",
    ),
    "раскрывает неизвестно что": (
        "locations",
        map_with(features=[cupboard(reveals=[{"kind": "monster", "id": "ghoul"}])]),
        "locations.locations.0.features.0.reveals.0.kind",
    ),
    "проверка не той характеристикой": (
        "locations",
        map_with(features=[feature(check={"ability": "luck", "skill": None, "dc": 10})]),
        "locations.locations.0.features.0.check.ability",
    ),
    "зоны ни близко, ни далеко": (
        "locations",
        map_with(links=[{"a": "here", "b": "here", "distance": "middle"}]),
        "locations.locations.0.links.0.distance",
    ),
    "отношение NPC вне шкалы": ("npcs", [npc(attitude=4)], "npcs.0.attitude"),
    "NPC не латиницей": ("npcs", [npc(id="ильза")], "npcs.0.id"),
    "вид связи не из словаря": ("relations", [relation(kind="nemesis")], "relations.0.kind"),
    "конец связи не ссылкой": ("relations", [relation(a="бармен")], "relations.0.a"),
    "предмет без держателя": ("items", [without_field(item(), "holder")], "items.0.holder"),
}


@pytest.mark.parametrize("case", B5_TYPICAL_REFUSALS)
def test_problems_of_typical_refusals_of_block_5_are_in_russian(case):
    section, data, path = B5_TYPICAL_REFUSALS[case]

    assert_in_russian(problems_of(section, data), path)


# Словарь видов связи спеки §8 записан здесь, а не взят из движка: это страж контракта webapp
# (`RelationKind` в `services/webapp/src/api/types.ts`).
RELATION_KINDS = (
    "kin",
    "spouse",
    "lover",
    "friend",
    "ally",
    "rival",
    "enemy",
    "debtor",
    "patron",
    "servant",
    "worships",
    "betrayed",
    "seeks",
    "other",
)


def test_every_kind_of_relation_of_the_contract_is_accepted():
    relations = [relation(id=f"r-{kind}", kind=kind, note="Пояснение.") for kind in RELATION_KINDS]

    draft = apply_section(AuthoringDraft(), "relations", relations)

    assert tuple(entity.kind for entity in draft.relations) == RELATION_KINDS


def test_a_relation_of_kind_other_needs_a_note():
    # Дополнение B5 §9: путь поля и русский текст вместо сообщения движка «kind other».
    for note in ("  ", None):
        data = relation(kind="other", note=note)
        if note is None:
            del data["note"]
        assert problems_of("relations", [data]) == [
            "relations.0.note: у вида «другое» нужно пояснение"
        ]


@pytest.mark.parametrize("attitude", [-3, 3])
def test_the_ends_of_the_attitude_scale_of_an_npc_are_accepted(attitude):
    [saved] = apply_section(AuthoringDraft(), "npcs", [npc(attitude=attitude)]).npcs

    assert saved.attitude == attitude


@pytest.mark.parametrize(("attitude", "message"), [(-4, "не меньше -3"), (4, "не больше 3")])
def test_an_attitude_of_an_npc_past_the_scale_is_refused(attitude, message):
    assert problems_of("npcs", [npc(attitude=attitude)]) == [f"npcs.0.attitude: {message}"]


# --- fix-раунд 1 блока 5: тексты `problems` без значений входа и с путём (дополнение B5 §9) ---


DC_RULE = "Сл — одна из 5, 10, 15, 20, 25, 30"


@pytest.mark.parametrize("dc", [1234567, 12, 0, -5])
@pytest.mark.parametrize(
    ("place", "path"),
    [
        ("feature", "locations.locations.0.features.0.check.dc"),
        ("secret", "locations.locations.2.secrets.0.check.dc"),
        ("hidden_dc", "locations.locations.0.exits.0.hidden_dc"),
    ],
)
def test_a_difficulty_off_the_table_is_named_by_the_rule_and_not_by_the_number(place, path, dc):
    # Security I1 B5: сообщение движка повторяло введённое число («…получено 1234567»).
    check = {"ability": "wis", "skill": "perception", "dc": dc}
    data = {
        "feature": lambda: map_with(features=[feature(check=check)]),
        "secret": lambda: map_with(LIGHTHOUSE, secrets=[secret(check=check)]),
        "hidden_dc": lambda: map_with(exits=[exit_(hidden_dc=dc)]),
    }[place]()

    found = problems_of("locations", data)

    assert found == [f"{path}: {DC_RULE}"]
    assert "1234567" not in found[0] and "получено" not in found[0]


@pytest.mark.parametrize("dc", [5, 10, 15, 20, 25, 30])
def test_every_difficulty_of_the_table_is_still_accepted(dc):
    check = {"ability": "int", "skill": "investigation", "dc": dc}
    data = map_with(features=[feature(check=check)], exits=[exit_(hidden_dc=dc)])
    data["locations"][LIGHTHOUSE]["secrets"] = [secret(check=check)]

    draft = apply_section(AuthoringDraft(), "locations", data)

    harbor, lighthouse = draft.locations[HARBOR], draft.locations[LIGHTHOUSE]
    assert (harbor.features[0].check.dc, harbor.exits[0].hidden_dc) == (dc, dc)
    assert lighthouse.secrets[0].check.dc == dc


def repeated(build) -> list[dict]:
    first, *rest = build()
    return [first, *rest, first]


@pytest.mark.parametrize(
    ("section", "data", "line"),
    [
        ("deities", [deity(), deity()], "deities: id повторяются: selmara"),
        ("factions", [faction(), faction()], "factions: id повторяются: smugglers"),
        ("npcs", repeated(npcs_data), "npcs: id повторяются: ilsa"),
        ("relations", repeated(relations_data), "relations: id повторяются: ilsa-worships-selmara"),
        ("items", repeated(items_data), "items: id повторяются: lamp-key"),
        (
            "locations",
            locations_data() | {"locations": repeated(lambda: locations_data()["locations"])},
            "locations.locations: id повторяются: harbor",
        ),
    ],
)
def test_repeated_ids_of_a_section_are_named_with_the_path_of_the_section(section, data, line):
    # Reviewer nit B5: строка повтора id шла от черновика целиком, без пути раздела.
    assert problems_of(section, data) == [line]


# --- fix-раунд 1 B6 (R5): шаги, чьё время отмечает результат генерации ---


@pytest.mark.parametrize(
    ("step", "written"),
    [
        # Замысел пишет и открытия — раздел шага «Улики».
        ("concept", ["concept", "clues"]),
        ("locations", ["locations", "location_contents"]),
        ("location_contents", ["locations", "location_contents"]),
        ("encounters", ["encounters"]),
        ("clues", ["clues"]),
        ("world", ["world"]),
    ],
)
def test_a_generated_step_marks_the_time_of_every_step_whose_section_it_writes(step, written):
    assert steps_written_by(step) == written
