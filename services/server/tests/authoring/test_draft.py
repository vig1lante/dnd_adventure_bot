"""Черновик приключения `AuthoringDraft` — контракт блока 3 (план DND-2, «Блок 3», спека §9, §10.3).

Черновик — частичный формат v2: все разделы необязательны, сущности — модели движка, а сверху
настройки (`settings`), закрепления (`locks`) и пожелания по шагам (`wishes`). `to_adventure`
собирает из него `Adventure` для валидатора и записи, `from_adventure` даёт черновик из готового
приключения. Заготовки автора (`Seeds`) входят в черновик закреплёнными.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from generator_fakes import RecordingProvider, v2_answers, v2_draft
from pydantic import ValidationError

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import (
    Adventure,
    Encounter,
    Location,
    Npc,
    Relation,
    Revelation,
    WorldInfo,
)
from dnd_engine.adventure.validator import validate
from dnd_engine.content import load_srd
from dnd_server.authoring.draft import (
    STEPS,
    AuthoringDraft,
    DraftSettings,
    Plot,
    Seeds,
    from_adventure,
    seeded,
    to_adventure,
)
from dnd_server.authoring.pipeline import generate

DEMO_CRYPT = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"


def location(**changes: Any) -> Location:
    data = {
        "id": "chapel",
        "name": "Часовня",
        "read_aloud": "Ветер свистит в окнах.",
        "features": [{"id": "bell-rope", "name": "Верёвка", "description": "Срезана."}],
    }
    return Location.model_validate(data | changes)


def npc(**changes: Any) -> Npc:
    data = {
        "id": "otto",
        "name": "Отто",
        "role": "звонарь",
        "appearance": "Сутулый.",
        "manner": "Тихий.",
        "motive": "Вернуть дочь.",
        "location_id": "chapel",
    }
    return Npc.model_validate(data | changes)


def fight(**changes: Any) -> Encounter:
    data = {
        "id": "ghouls",
        "location_id": "chapel",
        "trigger": "Шум.",
        "tactics": "Толпой.",
        "monsters": [{"monster_id": "ghoul", "count": 3}],
    }
    return Encounter.model_validate(data | changes)


def truth_with_a_clue_at_the_rope() -> Revelation:
    return Revelation.model_validate(
        {
            "id": "bell-truth",
            "statement": "Звонит Отто.",
            "clues": [{"id": "clue-rope", "description": "Срез.", "where": "feature:bell-rope"}],
        }
    )


# --- форма черновика ---


def test_the_steps_of_the_contract_go_in_the_order_of_the_plan():
    assert STEPS == (
        "concept",
        "world",
        "pantheon",
        "factions",
        "plot",
        "locations",
        "location_contents",
        "npcs",
        "relations",
        "encounters",
        "items",
        "clues",
    )


def test_every_section_of_a_draft_is_optional():
    draft = AuthoringDraft()

    assert draft.id is None and draft.world is None
    assert draft.locations == () and draft.npcs == () and draft.relations == ()
    assert draft.plot == Plot()
    assert draft.locks == frozenset() and draft.wishes == {}


def test_a_new_draft_allows_profanity_by_default():
    # Спека Р1: флажок мата у черновика, как у игры, по умолчанию включён.
    assert AuthoringDraft().settings.profanity is True
    assert DraftSettings().profanity is True


def test_the_settings_hold_exactly_the_fields_of_the_contract():
    assert set(DraftSettings.model_fields) == {
        "pitch",
        "tone",
        "target_hours",
        "party_size",
        "party_level",
        "modules",
        "profanity",
    }


def test_a_draft_is_immutable_so_a_step_returns_a_new_one():
    draft = AuthoringDraft()

    with pytest.raises(ValidationError):
        draft.title = "Другое"  # type: ignore[misc]


@pytest.mark.parametrize("step", STEPS)
def test_a_wish_is_kept_for_any_step(step):
    assert AuthoringDraft(wishes={step: "мрачнее"}).wishes == {step: "мрачнее"}


def test_a_wish_for_an_unknown_step_is_rejected():
    with pytest.raises(ValidationError):
        AuthoringDraft(wishes={"dragons": "больше драконов"})


@pytest.mark.parametrize(
    "ref",
    [
        "location:chapel",
        "npc:otto",
        "item:key",
        "encounter:ghouls",
        "monster:ash-priest",
        "deity:selmara",
        "faction:smugglers",
        "relation:otto-kin-greta",
        "revelation:bell-truth",
        "step:world",
    ],
)
def test_a_lock_names_an_entity_or_a_whole_step(ref):
    assert AuthoringDraft(locks={ref}).locks == {ref}


@pytest.mark.parametrize("step", STEPS)
def test_any_step_of_the_generator_can_be_locked_whole(step):
    assert AuthoringDraft(locks={f"step:{step}"}).locks == {f"step:{step}"}


@pytest.mark.parametrize(
    "ref",
    [
        "otto",
        "spell:fireball",
        "npc:Otto",
        "npc:",
        "step:dragons",
        "step:",
        "region:crow-cape",  # край — часть мира, закрепляется шагом world
        "npc:otto\n",  # закрепление уходит в промпт: перевод строки не пропускается
        " npc:otto",
        "npc:otto ",
        "npc:" + "a" * 65,  # длиннее шаблона id
    ],
)
def test_a_lock_of_another_form_is_rejected(ref):
    with pytest.raises(ValidationError):
        AuthoringDraft(locks={ref})


def test_a_wish_is_trimmed_and_holds_up_to_2000_characters():
    wish = "я" * 2000

    assert AuthoringDraft(wishes={"world": f"  {wish}\n"}).wishes == {"world": wish}


@pytest.mark.parametrize("wish", ["я" * 2001, "", "   \n\t"], ids=["2001 знак", "пусто", "пробелы"])
def test_a_wish_too_long_or_empty_is_rejected(wish):
    with pytest.raises(ValidationError):
        AuthoringDraft(wishes={"world": wish})


def test_a_repeated_id_inside_a_section_is_rejected():
    # Иначе сборка молча оставила бы одну из двух сущностей.
    with pytest.raises(ValidationError, match="otto"):
        AuthoringDraft(npcs=(npc(), npc(name="Другой Отто")))


# --- сборка приключения ---


def test_an_adventure_read_back_into_a_draft_is_assembled_unchanged():
    adventure = load_adventure(DEMO_CRYPT)

    draft = from_adventure(adventure)

    assert draft.id == "demo-crypt"
    assert draft.settings.pitch == adventure.pitch
    assert draft.plot.villain == "npc:fabian"
    assert {deity.id for deity in draft.deities} == set(adventure.deities)
    assert to_adventure(draft) == adventure


def test_the_adventure_takes_the_settings_and_the_plot_of_the_draft():
    draft = AuthoringDraft(
        id="chapel-whispers",
        title="Шёпот",
        journal_intro="Колокол звонит сам.",
        settings=DraftSettings(
            pitch="Колокол звонит.",
            tone="мрачно",
            target_hours=1.5,
            party_size=4,
            party_level=2,
            modules=("campfire",),
        ),
        plot=Plot(endings=("Колокол замолк.",), villain="npc:otto"),
        start_location="chapel",
        finale_location="chapel",
        locations=(location(),),
        npcs=(npc(),),
    )

    adventure = to_adventure(draft)

    assert (adventure.id, adventure.title, adventure.pitch, adventure.tone) == (
        "chapel-whispers",
        "Шёпот",
        "Колокол звонит.",
        "мрачно",
    )
    assert adventure.journal_intro == "Колокол звонит сам."
    assert (adventure.target_hours, adventure.party_size, adventure.party_level) == (1.5, 4, 2)
    assert adventure.rules.modules == ["campfire"]
    assert adventure.villain == "npc:otto"
    assert adventure.endings == ("Колокол замолк.",)
    assert set(adventure.locations) == {"chapel"} and set(adventure.npcs) == {"otto"}


def test_the_fight_of_a_location_and_the_clues_of_its_places_are_linked_by_the_assembly():
    # Шаги не пишут связей, которые следуют из других шагов: бой в локации и улику в месте сводит
    # сборка (так было и в генераторе DND-1).
    draft = AuthoringDraft(
        locations=(location(),),
        encounters=(fight(),),
        revelations=(truth_with_a_clue_at_the_rope(),),
    )

    adventure = to_adventure(draft)

    chapel = adventure.locations["chapel"]
    assert chapel.encounter_id == "ghouls"
    assert [(reveal.kind, reveal.id) for reveal in chapel.features[0].reveals] == [
        ("clue", "clue-rope")
    ]


def test_the_assembly_does_not_touch_a_locked_location():
    draft = AuthoringDraft(
        locations=(location(),),
        encounters=(fight(),),
        revelations=(truth_with_a_clue_at_the_rope(),),
        locks={"location:chapel"},
    )

    chapel = to_adventure(draft).locations["chapel"]

    assert chapel == location()  # бой модели в локации автора не привязывается: решает автор


def test_the_assembly_keeps_a_link_already_written_in_the_location():
    draft = AuthoringDraft(
        locations=(location(encounter_id="ghouls"),),
        encounters=(fight(),),
        revelations=(truth_with_a_clue_at_the_rope(),),
    )
    once = to_adventure(draft)

    assert to_adventure(from_adventure(once)) == once  # сборка не дублирует находки


def test_the_locked_entities_of_the_collections_become_the_seeds_of_the_adventure():
    draft = AuthoringDraft(
        locations=(location(),),
        npcs=(npc(),),
        world=WorldInfo(history="Старый мыс."),
        locks={"npc:otto", "location:chapel", "step:world", "relation:gone"},
    )

    # Связи и целые шаги в коллекции приключения не входят, поэтому в seeds их нет.
    assert to_adventure(draft).seeds == ("chapel", "otto")


def test_the_seeds_of_an_adventure_become_locks_of_its_draft():
    adventure = to_adventure(AuthoringDraft(npcs=(npc(),), locks={"npc:otto"}))

    assert from_adventure(adventure).locks == {"npc:otto"}


def test_an_empty_draft_is_still_assembled_so_the_validator_can_report_what_is_missing():
    report = validate(to_adventure(AuthoringDraft()), load_srd())

    assert report.blocking
    assert any(issue.code == "dangling_ref" and issue.ref == "adventure" for issue in report.issues)


# --- заготовки автора ---


def test_seeds_enter_the_draft_locked_entity_by_entity_and_whole_sections_by_step():
    world = WorldInfo(history="Старый мыс.")
    seeds = Seeds(
        npcs=(npc(),), locations=(location(),), world=world, plot=Plot(endings=("Конец.",))
    )

    draft = seeded(AuthoringDraft(settings=DraftSettings(pitch="Колокол.")), seeds)

    assert draft.npcs == (npc(),) and draft.locations == (location(),)
    assert draft.world == world and draft.plot.endings == ("Конец.",)
    assert draft.settings.pitch == "Колокол."
    assert draft.locks == {"npc:otto", "location:chapel", "step:world", "step:plot"}


def test_seeds_reject_an_unknown_section_and_a_repeated_id():
    with pytest.raises(ValidationError):
        Seeds.model_validate({"dragons": []})
    with pytest.raises(ValidationError):
        Seeds(npcs=(npc(), npc()))


def test_empty_seeds_change_nothing():
    draft = AuthoringDraft(settings=DraftSettings(pitch="Колокол."))

    assert seeded(draft, Seeds()) == draft


# --- связи формата v1 внутри NPC (Р8: обратная совместимость заготовок) ---


def rival_of_bram(**changes: Any) -> dict[str, Any]:
    """NPC в форме заготовки DND-1: связь лежит внутри NPC (`npcs[].relations`)."""
    relation = {"npc_id": "bram", "kind": "rival", "note": "Делят причал."} | changes
    return npc(id="ilsa").model_dump() | {"relations": [relation]}


def test_v1_relations_inside_a_seed_npc_move_into_the_graph_by_the_engine_rules():
    # Правила движка (`upgrade_v1`): «npc_id — kind для владельца», id `<npc_id>-<kind>-<владелец>`,
    # связь не взаимная (v1 говорит только о стороне владельца).
    seeds = Seeds.model_validate({"npcs": [rival_of_bram()]})

    assert seeds.npcs[0].relations == ()
    assert seeds.relations == (
        Relation(
            id="bram-rival-ilsa",
            a="npc:bram",
            b="npc:ilsa",
            kind="rival",
            note="Делят причал.",
            mutual=False,
        ),
    )


def test_a_v1_kind_outside_the_v2_list_is_translated_by_the_engine_table():
    seeds = Seeds.model_validate({"npcs": [rival_of_bram(kind="sister", note="")]})

    [relation] = seeds.relations
    assert (relation.id, relation.kind, relation.note) == ("bram-kin-ilsa", "kin", "сестра")


def test_a_moved_relation_takes_a_free_id_next_to_the_given_graph():
    given = {"id": "bram-rival-ilsa", "a": "npc:bram", "b": "npc:ilsa", "kind": "enemy"}

    seeds = Seeds.model_validate({"npcs": [rival_of_bram()], "relations": [given]})

    assert [r.id for r in seeds.relations] == ["bram-rival-ilsa", "bram-rival-ilsa-2"]


def test_the_moved_relations_of_seeds_are_locked_like_any_seed_relation():
    draft = seeded(AuthoringDraft(), Seeds.model_validate({"npcs": [rival_of_bram()]}))

    assert draft.locks == {"npc:ilsa", "relation:bram-rival-ilsa"}
    assert [r.id for r in to_adventure(draft).relations] == ["bram-rival-ilsa"]


def test_a_draft_read_from_data_moves_v1_relations_too():
    # Черновик из базы или запроса конструктора (блок 4): тот же перевод, сборка v2 не падает.
    draft = AuthoringDraft.model_validate_json(json.dumps({"npcs": [rival_of_bram()]}))

    assert draft.npcs[0].relations == ()
    assert [r.id for r in draft.relations] == ["bram-rival-ilsa"]
    assert draft.locks == frozenset()  # закрепляет только заготовка
    assert [r.id for r in to_adventure(draft).relations] == ["bram-rival-ilsa"]


def test_a_draft_from_a_v1_adventure_has_its_relations_in_the_graph_already():
    data = to_adventure(AuthoringDraft(npcs=(npc(),))).model_dump(mode="json")
    del data["format_version"]  # копия игры формата v1: связи ещё внутри NPC
    data["npcs"]["otto"]["relations"] = [{"npc_id": "greta", "kind": "parent"}]

    draft = from_adventure(Adventure.model_validate(data))

    assert draft.npcs[0].relations == ()
    assert [r.id for r in draft.relations] == ["greta-kin-otto"]
    assert to_adventure(draft) == Adventure.model_validate(data)


@pytest.mark.parametrize("npc_id", ["Брам", "../bram", "", "bram rival"])
def test_a_v1_relation_with_an_id_that_cannot_be_an_npc_is_rejected_at_load(npc_id):
    with pytest.raises(ValidationError):
        Seeds.model_validate({"npcs": [rival_of_bram(npc_id=npc_id)]})


# --- свои статблоки: id — имя файла ---


def custom_monster(monster_id: str) -> dict[str, Any]:
    return {
        "id": monster_id,
        "name": "Пепельный жрец",
        "name_en": "Ash Priest",
        "cr": 5,
        "xp": 1800,
        "ac": 15,
        "hp": 90,
        "hp_dice": "12d8+36",
        "speed": 30,
        "scores": {"str": 10, "dex": 14, "con": 16, "int": 13, "wis": 17, "cha": 15},
        "actions": [{"id": "staff", "name": "Посох", "kind": "other"}],
    }


@pytest.mark.parametrize("monster_id", ["Ash Priest", "../ash", ""])
def test_a_custom_statblock_whose_id_cannot_be_a_file_name_is_rejected_by_the_draft(monster_id):
    # `Adventure` отверг бы его только при сборке — после всех вызовов модели.
    with pytest.raises(ValidationError):
        Seeds.model_validate({"monsters": [custom_monster(monster_id)]})
    with pytest.raises(ValidationError):
        AuthoringDraft.model_validate({"monsters": [custom_monster(monster_id)]})


def test_a_custom_statblock_with_a_good_id_is_assembled():
    draft = AuthoringDraft.model_validate({"monsters": [custom_monster("ash-priest")]})

    assert set(to_adventure(draft).monsters) == {"ash-priest"}


def test_a_draft_built_from_engine_models_moves_v1_relations_too():
    # Конструктор может собрать черновик и из моделей движка, а не только из JSON.
    ilsa = Npc.model_validate(rival_of_bram())

    draft = AuthoringDraft(npcs=(ilsa,))

    assert draft.npcs[0].relations == ()
    assert [r.id for r in draft.relations] == ["bram-rival-ilsa"]


# --- контракт для конструктора (блок 4): хранение и обратный путь ---


def test_an_empty_draft_survives_a_json_round_trip():
    draft = AuthoringDraft()

    assert AuthoringDraft.model_validate_json(draft.model_dump_json()) == draft


async def test_a_draft_stored_as_data_locks_and_wishes_columns_is_read_back_unchanged():
    # Спека §10.2: `adventure_drafts` хранит данные, закрепления и пожелания отдельными JSON.
    built = (await generate(RecordingProvider(v2_answers()), v2_draft(profanity=False))).draft
    draft = built.model_copy(
        update={
            "locks": frozenset({"npc:ilsa", "relation:bram-rival-ilsa", "step:world"}),
            "wishes": {"npcs": "Говорящий кот", "clues": "Улики — в предметах"},
        }
    )

    data = draft.model_dump_json(exclude={"locks", "wishes"})
    locks = json.dumps(sorted(draft.locks))
    wishes = json.dumps(draft.wishes, ensure_ascii=False)
    stored = {**json.loads(data), "locks": json.loads(locks), "wishes": json.loads(wishes)}

    assert AuthoringDraft.model_validate(stored) == draft


async def test_a_full_draft_goes_through_an_adventure_and_back_with_its_content():
    # `from_adventure(to_adventure(draft))`: разделы, заказ, мир, сюжет, старт и финал остаются;
    # у локаций появляются связи, которые проставляет сборка (бой локации, находки мест).
    draft = (await generate(RecordingProvider(v2_answers()), v2_draft())).draft
    draft = draft.model_copy(
        update={
            "locks": frozenset(
                {"npc:ilsa", "deity:selmara", "relation:bram-rival-ilsa", "step:world"}
            ),
            "wishes": {"npcs": "Говорящий кот"},
        }
    )
    adventure = to_adventure(draft)

    back = from_adventure(adventure)

    changed = {"locations", "locks", "wishes"}
    assert back.model_dump(exclude=changed) == draft.model_dump(exclude=changed)
    assert back.locations == tuple(adventure.locations.values())
    assert [loc.id for loc in back.locations] == [loc.id for loc in draft.locations]
    # Приключение несёт только сиды коллекций: закрепления связей и шагов и пожелания — в черновике.
    assert back.locks == {"npc:ilsa", "deity:selmara"}
    assert back.wishes == {}
    assert to_adventure(back) == adventure
