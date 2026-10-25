"""Разделы черновика в конструкторе (план B4.1; спека §10.3; дополнение dispatcher B4 §2, §3, §7, §8).

Чистые функции без базы: раздел ↔ шаг, проверка данных раздела с пределами API и запись его в
черновик (`apply_section`), строки проблем без значений входа, статусы шагов и отчёт проверки по
заполненным разделам. Черновик — контракт B3 (`AuthoringDraft`); живых моделей нет.
"""

import re

import pytest
from generator_fakes import V2_SCENARIO, RecordingProvider, v2_answers, v2_draft
from pydantic import TypeAdapter, ValidationError

from dnd_engine.adventure.schema import Deity, Faction, WorldInfo
from dnd_engine.content import load_srd
from dnd_server.authoring.draft import STEPS, AuthoringDraft, Plot, to_adventure
from dnd_server.authoring.pipeline import check, generate, owner_step
from dnd_server.authoring.sections import (
    REQUIRED_STEPS,
    SECTION_STEPS,
    InvalidSection,
    apply_section,
    draft_report,
    problems,
    step_statuses,
)
from dnd_server.authoring.steps import is_empty, run_step
from dnd_server.llm.fake import FakeProvider, load_scenario

SECRET_INPUT = "ЗНАЧЕНИЕ-ИЗ-ЗАПРОСА"


def settings_data(**changes) -> dict:
    """Данные раздела «Замысел» по дополнению §2: все поля настроек, название и главное для дневника."""
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


def test_the_editor_has_the_five_sections_of_block_4_each_with_its_step():
    assert SECTION_STEPS == {
        "settings": "concept",
        "world": "world",
        "deities": "pantheon",
        "factions": "factions",
        "plot": "plot",
    }
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
}


def section_data(draft: AuthoringDraft, section: str):
    """Раздел черновика в форме `Draft` — так его видит и сохраняет webapp."""
    dumped = draft.model_dump(mode="json")
    if section == "settings":
        return dumped["settings"] | {"title": draft.title, "journal_intro": draft.journal_intro}
    return dumped[section]


@pytest.mark.parametrize("section", list(SECTION_STEPS))
async def test_the_longest_answer_a_step_may_give_is_saved_back_by_hand(section):
    step = SECTION_STEPS[section]
    provider = RecordingProvider({f"generator.{step}": LONGEST_ANSWERS[step]})
    generated = await run_step(provider, AuthoringDraft(), step)  # ответ прошёл схему шага

    saved = apply_section(AuthoringDraft(), section, section_data(generated, section))

    assert section_data(saved, section) == section_data(generated, section)


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


@pytest.mark.parametrize("case", TYPICAL_REFUSALS)
def test_problems_of_typical_refusals_are_in_russian_without_pydantic_phrases(case):
    section, data, path = TYPICAL_REFUSALS[case]

    found = problems_of(section, data)

    assert any(line.startswith(path) for line in found), found
    for line in found:
        message = line.split(": ", 1)[1]
        assert message and not re.search(r"[A-Za-z]", message), line


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

    # Открытие без улик — шагу улик есть что наполнять (`steps.is_empty`): шаг пуст.
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
