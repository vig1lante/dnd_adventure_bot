"""Дневник отряда (спека DND-2 §3 Р11, §12; дополнение контракта B8 §1–§4, §10).

Дневник — часть состояния игры: стартовые записи (`open_journal`), запись о сущности по ссылке
(`write`), первый приход отряда в место (`visit`) и выборки для сервера (`visible_entries`,
`changed_entries`). Приключение — `demo-crypt`: в начале известны деревня, кладбище, Марта, оба
бога, оба края и первый слух; отряд начинает в деревне — она отмечена как место, где отряд был с
раунда 0. Тексты записей — только публичные: тайны приключения в дневник не попадают никогда.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any, get_args

import pytest

from dnd_engine.adventure.schema import Adventure
from dnd_engine.journal import (
    MAX_NOTE_CHARS,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    SECTIONS,
    JournalEntry,
    JournalError,
    Section,
    changed_entries,
    open_journal,
    record,
    visible_entries,
    visit,
    write,
)
from dnd_engine.state import GameState

START_REFS = {
    "intro",
    "location:village",
    "location:cemetery",
    "npc:marta",
    "deity:ossmar",
    "deity:veyla",
    "region:marshland",
    "region:barrow-hill",
    "rumor:1",
}
SECTION_ORDER = list(get_args(Section))


# --- помощники ---


def variant(demo_adventure: Adventure, edit: Callable[[dict[str, Any]], None]) -> Adventure:
    """Копия `demo-crypt` с правкой данных: приключение неизменяемо, меняем его дамп."""
    data = demo_adventure.model_dump(mode="json")
    edit(data)
    return Adventure.model_validate(data)


def entry_of(state: GameState, entry_id: str) -> JournalEntry:
    [entry] = [entry for entry in state.journal if entry.id == entry_id]
    return entry


def secrets_of(adventure: Adventure) -> list[str]:
    """Всё, что игроки не должны прочитать в дневнике: тайны, мотивы, заметки мастера, полные
    описания предметов, утверждения открытий."""
    texts = [npc.secret for npc in adventure.npcs.values()]
    texts += [npc.motive for npc in adventure.npcs.values()]
    texts += [deity.secret for deity in adventure.deities.values()]
    texts += [faction.secret for faction in adventure.factions.values()]
    texts += [location.dm_notes for location in adventure.locations.values()]
    texts += [s.text for location in adventure.locations.values() for s in location.secrets]
    texts += [item.description for item in adventure.items.values()]
    texts += [revelation.statement for revelation in adventure.revelations.values()]
    texts += [region.description for region in adventure.world.regions]
    return [text for text in texts if text]


def every_ref(adventure: Adventure) -> list[str]:
    """Все ссылки, которые может записать мастер (кроме служебной `intro`)."""
    refs = [f"location:{x}" for x in adventure.locations]
    refs += [f"npc:{x}" for x in adventure.npcs]
    refs += [f"deity:{x}" for x in adventure.deities]
    refs += [f"faction:{x}" for x in adventure.factions]
    refs += [f"region:{region.id}" for region in adventure.world.regions]
    refs += [f"rumor:{n}" for n in range(1, len(adventure.world.rumors) + 1)]
    refs += [f"clue:{c.id}" for rev in adventure.revelations.values() for c in rev.clues]
    refs += [f"item:{x}" for x in adventure.items]
    return refs


# --- модель записи ---


def test_every_kind_of_ref_has_its_section_and_the_sections_keep_the_interface_order():
    assert dict(SECTIONS) == {
        "intro": "main",
        "location": "places",
        "npc": "people",
        "deity": "gods",
        "faction": "factions",
        "region": "world",
        "world": "world",
        "rumor": "rumors",
        "clue": "clues",
        "item": "items",
    }
    assert SECTION_ORDER == [
        "main",
        "places",
        "people",
        "gods",
        "factions",
        "world",
        "rumors",
        "clues",
        "items",
    ]


def test_an_entry_serializes_with_every_field_of_the_contract():
    entry = JournalEntry(
        id="location:cemetery@all",
        ref="location:cemetery",
        section="places",
        title="Кладбище",
        text="Кладбище на холме.",
        audience="all",
        turn=0,
        revision=2,
        visited_turn=3,
        updated_turn=4,
    )

    assert entry.model_dump(mode="json") == {
        "id": "location:cemetery@all",
        "ref": "location:cemetery",
        "section": "places",
        "title": "Кладбище",
        "text": "Кладбище на холме.",
        "audience": "all",
        "turn": 0,
        "revision": 2,
        "visited_turn": 3,
        "updated_turn": 4,
    }
    assert JournalEntry.model_validate_json(entry.model_dump_json()) == entry


def test_an_entry_cannot_be_changed_in_place():
    # Сервер сравнивает дневник до и после хода: запись, изменённая на месте, испортила бы «до».
    entry = JournalEntry(
        id="intro@all",
        ref="intro",
        section="main",
        title="Главное",
        text="Т",
        audience="all",
        turn=0,
    )

    with pytest.raises(ValueError):
        entry.text = "другое"  # type: ignore[misc]
    assert (entry.revision, entry.visited_turn) == (1, None)


def test_an_unknown_section_is_rejected():
    with pytest.raises(ValueError):
        JournalEntry(
            id="x@all", ref="x", section="secrets", title="Т", text="Т", audience="all", turn=0
        )


# --- старт игры: open_journal ---


def test_the_start_has_only_the_intro_and_what_is_known_at_start(make_game, demo_adventure):
    state = open_journal(make_game(), demo_adventure)

    assert {entry.ref for entry in state.journal} == START_REFS
    assert state.journal[0].ref == "intro"
    assert all(entry.audience == "all" for entry in state.journal)
    assert all((entry.turn, entry.revision) == (0, 1) for entry in state.journal)
    visited = {
        entry.ref: entry.visited_turn for entry in state.journal if entry.visited_turn is not None
    }
    assert visited == {"location:village": 0}  # отряд начинает в деревне (дополнение §10)
    assert all(entry.id == f"{entry.ref}@all" for entry in state.journal)
    sections = [SECTION_ORDER.index(entry.section) for entry in state.journal]
    assert sections == sorted(sections)  # записи старта идут в порядке разделов


def test_start_entries_take_the_public_text_and_the_name(make_game, demo_adventure):
    state = open_journal(make_game(), demo_adventure)

    intro = entry_of(state, "intro@all")
    marta = entry_of(state, "npc:marta@all")
    rumor = entry_of(state, "rumor:1@all")
    region = entry_of(state, "region:marshland@all")
    assert (intro.section, intro.title, intro.text) == (
        "main",
        "Главное",
        demo_adventure.journal_intro,
    )
    assert (marta.section, marta.title) == ("people", "Марта")
    assert marta.text == demo_adventure.npcs["marta"].public
    assert (region.section, region.title) == ("world", "Болотистый край")
    assert region.text == "Болотистый край, где стоит деревня Мшистый Брод."
    assert rumor.section == "rumors"
    assert rumor.text == demo_adventure.world.rumors[0].text  # у слуха нет public — его текст


def test_the_start_location_is_written_as_visited_even_when_it_is_not_known_at_start(
    make_game, demo_adventure
):
    # Отряд уже стоит в стартовой локации: «пришёл в локацию — место» (дополнение §10, поправка §4).
    adventure = variant(
        demo_adventure, lambda data: data["locations"]["village"].update(known_at_start=False)
    )

    state = open_journal(make_game(), adventure)

    village = entry_of(state, "location:village@all")
    assert (village.section, village.title, village.text) == (
        "places",
        "Мшистый Брод",
        demo_adventure.locations["village"].public,
    )
    assert (village.audience, village.turn, village.visited_turn, village.revision) == (
        "all",
        0,
        0,
        1,
    )
    assert {entry.ref for entry in state.journal} == START_REFS  # и ничего лишнего
    sections = [SECTION_ORDER.index(entry.section) for entry in state.journal]
    assert sections == sorted(sections)


def test_coming_back_to_the_start_location_changes_nothing(make_game, demo_adventure):
    state = open_journal(make_game(party_round=4), demo_adventure)
    before = state.model_dump()

    assert visit(state, demo_adventure, "village") is None
    assert state.model_dump() == before


def test_the_turn_of_the_start_is_zero_even_when_an_old_game_opens_the_journal_later(
    make_game, demo_adventure
):
    # Старая игра без дневника получает стартовый дневник посреди игры (дополнение §5).
    state = open_journal(make_game(party_round=7), demo_adventure)

    assert {entry.turn for entry in state.journal} == {0}


def test_without_public_text_a_start_entry_is_built_from_the_name(make_game, demo_adventure):
    adventure = variant(demo_adventure, lambda data: data["npcs"]["marta"].update(public=""))

    state = open_journal(make_game(), adventure)

    assert entry_of(state, "npc:marta@all").text == "Марта"


def test_an_entity_becomes_known_at_start_only_by_its_flag(make_game, demo_adventure):
    def edit(data: dict[str, Any]) -> None:
        data["items"]["chest-key"]["known_at_start"] = True
        data["factions"]["regulars"]["known_at_start"] = True
        data["world"]["rumors"][0]["known_at_start"] = False

    state = open_journal(make_game(), variant(demo_adventure, edit))

    refs = {entry.ref for entry in state.journal}
    assert {"item:chest-key", "faction:regulars"} <= refs
    assert "rumor:1" not in refs


def test_an_empty_intro_gives_no_main_entry(make_game, demo_adventure):
    adventure = variant(demo_adventure, lambda data: data.update(pitch="", journal_intro=""))

    state = open_journal(make_game(), adventure)

    assert "intro" not in {entry.ref for entry in state.journal}


def test_open_journal_returns_a_new_state_and_leaves_the_given_one_untouched(
    make_game, demo_adventure
):
    state = make_game()
    before = state.model_dump()

    opened = open_journal(state, demo_adventure)

    assert state.model_dump() == before and state.journal == ()
    assert opened.model_dump(exclude={"journal"}) == state.model_dump(exclude={"journal"})
    assert opened.journal
    assert opened.party == state.party and opened.party is not state.party


def test_open_journal_on_a_journal_that_is_not_empty_changes_nothing(make_game, demo_adventure):
    state = open_journal(make_game(), demo_adventure)
    write(state, demo_adventure, "npc:fabian", note="Задолжал Марте.")
    before = state.model_dump()

    again = open_journal(state, demo_adventure)

    assert again.model_dump() == before


# --- запись о сущности: write ---


def test_write_creates_an_entry_with_the_public_text_and_the_turn_of_the_state(
    make_game, demo_adventure
):
    state = make_game(party_round=4)

    entry, new = write(state, demo_adventure, "npc:fabian")

    assert new is True
    assert state.journal == (entry,)
    assert entry == JournalEntry(
        id="npc:fabian@all",
        ref="npc:fabian",
        section="people",
        title="Фабиан",
        text=demo_adventure.npcs["fabian"].public,
        audience="all",
        turn=4,
    )


def test_a_note_is_a_new_paragraph_and_the_repeat_with_a_note_extends_the_entry(
    make_game, demo_adventure
):
    state = make_game(party_round=2)
    base = demo_adventure.deities["ossmar"].public

    first, _ = write(state, demo_adventure, "deity:ossmar", note="Его ключ висит на воротах.")
    state.party_round = 5
    second, new = write(state, demo_adventure, "deity:ossmar", note="Жрецов у него нет.")

    assert first.text == f"{base}\n\nЕго ключ висит на воротах."
    assert new is False
    assert second.text == f"{first.text}\n\nЖрецов у него нет."
    assert (second.revision, second.turn) == (2, 2)  # раунд появления записи не меняется
    assert state.journal == (second,)  # повтор дополняет, а не плодит дубль


def test_a_repeat_without_a_note_changes_nothing(make_game, demo_adventure):
    state = make_game()
    created, _ = write(state, demo_adventure, "faction:regulars")

    entry, new = write(state, demo_adventure, "faction:regulars")

    assert (entry, new) == (created, False)
    assert state.journal == (created,)


def test_the_common_and_the_personal_entry_about_one_entity_are_two_entries(
    make_game, demo_adventure
):
    state = make_game()

    common, _ = write(state, demo_adventure, "npc:fabian")
    personal, new = write(state, demo_adventure, "npc:fabian", audience="pc-2", note="Он врёт.")

    assert new is True
    assert (common.id, personal.id) == ("npc:fabian@all", "npc:fabian@pc-2")
    assert personal.audience == "pc-2"
    assert state.journal == (common, personal)


def test_an_entry_can_be_addressed_only_to_everyone_or_to_a_hero_of_the_party(
    make_game, demo_adventure
):
    state = make_game()

    with pytest.raises(JournalError, match="ghost"):
        write(state, demo_adventure, "npc:fabian", audience="ghost")
    assert state.journal == ()


@pytest.mark.parametrize(
    ("ref", "known"),
    [
        ("npc:ghost", "npc:marta"),
        ("location:moon", "location:crypt"),
        ("deity:nobody", "deity:veyla"),
        ("faction:none", "faction:pale-candle"),
        ("region:sea", "region:marshland"),
        ("rumor:4", "rumor:3"),
        ("rumor:0", "rumor:1"),
        ("rumor:x", "rumor:2"),
        ("clue:ghost", "clue:clue-diary"),
        ("item:sword", "item:healing-potion"),
        ("npc", "npc:fabian"),  # вид без id — та же подсказка
    ],
)
def test_an_unknown_ref_is_an_error_with_the_known_refs_of_its_kind(
    make_game, demo_adventure, ref, known
):
    state = make_game()

    with pytest.raises(JournalError) as error:
        write(state, demo_adventure, ref, note="Т")

    assert known in str(error.value)
    assert state.journal == ()


@pytest.mark.parametrize("ref", ["god:ossmar", "feature:diary", "", "secret:marta"])
def test_a_ref_of_an_unknown_kind_is_an_error_that_names_the_kinds(make_game, demo_adventure, ref):
    state = make_game()

    with pytest.raises(JournalError, match="deity:<id>"):
        write(state, demo_adventure, ref, note="Т")
    assert state.journal == ()


def test_the_history_of_the_world_is_written_only_with_a_note(make_game, demo_adventure):
    state = make_game()

    with pytest.raises(JournalError, match="note"):
        write(state, demo_adventure, "world")
    entry, _ = write(state, demo_adventure, "world", note="Край принадлежал графам Мортенам.")

    assert (entry.section, entry.title, entry.text) == (
        "world",
        "История мира",
        "Край принадлежал графам Мортенам.",
    )
    assert demo_adventure.world.history not in entry.text  # история мира — данные мастера


def test_rumors_are_addressed_by_their_number_from_one(make_game, demo_adventure):
    state = make_game()

    entries = [write(state, demo_adventure, f"rumor:{n}")[0] for n in (1, 2, 3)]

    assert [entry.text for entry in entries] == [r.text for r in demo_adventure.world.rumors]
    assert all(entry.section == "rumors" for entry in entries)
    assert entries[0].title == entries[0].text  # короткий слух — сам себе название


def test_a_rumor_shows_its_public_text_and_never_its_truth(make_game, demo_adventure):
    adventure = variant(
        demo_adventure,
        lambda data: data["world"]["rumors"][2].update(public="Говорят про Ульриха."),
    )
    state = make_game()

    entry, _ = write(state, adventure, "rumor:3")

    assert entry.text == "Говорят про Ульриха."
    assert "правд" not in entry.text.casefold() and "true" not in entry.text.casefold()
    assert adventure.world.rumors[2].text not in entry.text


def test_a_clue_shows_what_the_heroes_found_and_its_title_is_the_start_of_it(
    make_game, demo_adventure
):
    clue = demo_adventure.revelations["kidnapper"].clues[0]
    state = make_game()

    entry, _ = write(state, demo_adventure, "clue:clue-diary")

    assert entry.section == "clues"
    assert entry.text == clue.description
    assert len(entry.title) <= MAX_TITLE_CHARS
    assert entry.title.endswith("…")
    head = entry.title[:-1]
    words = " ".join(clue.description.split())
    assert words.startswith(head)
    assert words[len(head)] in " ,.;:"  # обрезано по границе слова, а не посреди слова


def test_an_item_shows_its_public_text_and_not_its_description(make_game, demo_adventure):
    key = demo_adventure.items["chest-key"]
    state = make_game()

    entry, _ = write(state, demo_adventure, "item:chest-key")

    assert (entry.section, entry.title, entry.text) == ("items", key.name, key.public)
    assert key.description not in entry.text


def test_no_secret_of_the_adventure_reaches_any_entry(make_game, demo_adventure):
    state = open_journal(make_game(), demo_adventure)
    for ref in every_ref(demo_adventure):
        write(state, demo_adventure, ref)
        write(state, demo_adventure, ref, audience="pc-1")
    write(state, demo_adventure, "world", note="Край принадлежал графам.")

    texts = [f"{entry.title}\n{entry.text}" for entry in state.journal]
    for secret in secrets_of(demo_adventure):
        assert not any(secret in text for text in texts), secret
    marta = entry_of(state, "npc:marta@all")
    assert demo_adventure.npcs["marta"].secret not in marta.text


def test_a_note_may_bring_the_text_exactly_to_the_limit_but_not_beyond(make_game, demo_adventure):
    state = make_game()
    write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)
    for _ in range(2):
        write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)
    entry = entry_of(state, "world@all")
    room = MAX_TEXT_CHARS - len(entry.text) - 2  # два знака — перенос абзаца

    full, _ = write(state, demo_adventure, "world", note="ю" * room)
    before = state.model_dump()
    with pytest.raises(JournalError, match=str(MAX_TEXT_CHARS)):
        write(state, demo_adventure, "world", note="ю")

    assert len(full.text) == MAX_TEXT_CHARS
    assert state.model_dump() == before


def test_a_new_entry_with_a_note_also_keeps_to_the_limit(make_game, demo_adventure):
    adventure = variant(
        demo_adventure,
        lambda data: data["npcs"]["fabian"].update(public="ф" * (MAX_TEXT_CHARS - 2)),
    )
    state = make_game()

    with pytest.raises(JournalError):
        write(state, adventure, "npc:fabian", note="ю")
    assert state.journal == ()


def test_a_long_public_text_of_the_adventure_is_written_whole_without_a_note(
    make_game, demo_adventure
):
    # Предел — для дополнений мастера; данные приключения проверены автором и не режутся.
    long_public = "ф" * (MAX_TEXT_CHARS + 10)
    adventure = variant(
        demo_adventure, lambda data: data["npcs"]["fabian"].update(public=long_public)
    )
    state = make_game()

    entry, _ = write(state, adventure, "npc:fabian")

    assert entry.text == long_public


# --- первый приход в место: visit ---


def test_coming_to_an_unknown_place_writes_it_with_the_turn_of_the_visit(make_game, demo_adventure):
    state = open_journal(make_game(party_round=3), demo_adventure)

    entry = visit(state, demo_adventure, "crypt")

    assert entry == entry_of(state, "location:crypt@all")
    assert (entry.section, entry.title, entry.text) == (
        "places",
        "Склеп графа Мортена",
        demo_adventure.locations["crypt"].public,
    )
    assert (entry.audience, entry.turn, entry.visited_turn, entry.revision) == ("all", 3, 3, 1)


def test_the_first_visit_of_a_known_place_marks_it_and_raises_the_revision(
    make_game, demo_adventure
):
    state = open_journal(make_game(party_round=2), demo_adventure)
    known = entry_of(state, "location:cemetery@all")

    entry = visit(state, demo_adventure, "cemetery")

    assert entry == known.model_copy(update={"visited_turn": 2, "revision": 2})
    assert entry_of(state, "location:cemetery@all") == entry
    assert len(state.journal) == len(START_REFS)  # дубля нет


def test_a_repeated_visit_changes_nothing(make_game, demo_adventure):
    state = open_journal(make_game(party_round=2), demo_adventure)
    visit(state, demo_adventure, "cemetery")
    state.party_round = 6
    before = state.model_dump()

    assert visit(state, demo_adventure, "cemetery") is None
    assert state.model_dump() == before


def test_a_personal_entry_about_a_place_does_not_count_as_a_visit_of_the_party(
    make_game, demo_adventure
):
    state = make_game(party_round=1)
    write(state, demo_adventure, "location:crypt", audience="pc-1", note="Слышал о склепе.")

    entry = visit(state, demo_adventure, "crypt")

    assert entry is not None and entry.id == "location:crypt@all"
    assert entry_of(state, "location:crypt@pc-1").visited_turn is None


# --- выборки для сервера ---


def test_a_player_sees_the_common_entries_and_the_personal_ones_of_their_hero(
    make_game, demo_adventure
):
    state = make_game()
    common, _ = write(state, demo_adventure, "npc:fabian")
    mine, _ = write(state, demo_adventure, "clue:clue-diary", audience="pc-1")
    theirs, _ = write(state, demo_adventure, "item:chest-key", audience="pc-2")

    assert visible_entries(state.journal, "pc-1") == (common, mine)
    assert visible_entries(state.journal, "pc-2") == (common, theirs)
    assert visible_entries(state.journal, None) == (common,)  # игрок без героя — только общие


def test_changed_entries_are_the_new_ones_and_the_ones_with_a_higher_revision(
    make_game, demo_adventure
):
    state = open_journal(make_game(party_round=1), demo_adventure)
    before = state.journal

    visit(state, demo_adventure, "cemetery")
    write(state, demo_adventure, "npc:fabian", audience="pc-2")
    write(state, demo_adventure, "npc:marta")  # повтор без note — не изменение

    changed = changed_entries(before, state.journal)

    assert [entry.id for entry in changed] == ["location:cemetery@all", "npc:fabian@pc-2"]
    assert changed_entries(state.journal, state.journal) == ()


# --- финальное покрытие блока: модель записи ---

CONTRACT_FIELDS = (
    "id",
    "ref",
    "section",
    "title",
    "text",
    "audience",
    "turn",
    "revision",
    "visited_turn",
    "updated_turn",
)


def test_an_entry_has_exactly_the_ten_fields_of_the_contract_and_all_of_them_reach_json():
    # Webapp и сервер читают запись буква в букву (дополнение §2): лишнее или пропавшее поле
    # разошлось бы с `JournalEntry` клиента. Поля по умолчанию тоже уходят в JSON (`null`).
    entry = JournalEntry(
        id="npc:marta@all",
        ref="npc:marta",
        section="people",
        title="Марта",
        text="Хозяйка трактира.",
        audience="all",
        turn=2,
    )

    dumped = json.loads(entry.model_dump_json())

    assert tuple(JournalEntry.model_fields) == CONTRACT_FIELDS
    assert tuple(dumped) == CONTRACT_FIELDS
    assert (dumped["revision"], dumped["visited_turn"], dumped["updated_turn"]) == (1, None, None)


@pytest.mark.parametrize("field", CONTRACT_FIELDS)
def test_no_field_of_an_entry_can_be_changed_in_place(field):
    entry = JournalEntry(
        id="intro@all", ref="intro", section="main", title="Г", text="Т", audience="all", turn=0
    )
    before = entry.model_dump()

    with pytest.raises(ValueError):
        setattr(entry, field, entry.model_dump()[field])

    assert entry.model_dump() == before


# --- финальное покрытие блока: старт игры ---


def nothing_known(data: dict[str, Any]) -> None:
    """Приключение без завязки и без того, что известно со старта."""
    data.update(pitch="", journal_intro="")
    for table in ("locations", "npcs", "items", "deities", "factions"):
        for entity in data[table].values():
            entity["known_at_start"] = False
    for entity in (*data["world"]["regions"], *data["world"]["rumors"]):
        entity["known_at_start"] = False


def test_an_adventure_without_intro_and_known_things_starts_with_the_start_location_only(
    make_game, demo_adventure
):
    adventure = variant(demo_adventure, nothing_known)

    state = open_journal(make_game(), adventure)

    [village] = state.journal
    assert (village.id, village.section, village.audience) == (
        "location:village@all",
        "places",
        "all",
    )
    assert (village.turn, village.visited_turn, village.revision) == (0, 0, 1)
    assert open_journal(state, adventure) is state  # дневник уже не пуст: второй вызов — тот же


def test_open_journal_on_an_opened_journal_returns_the_very_same_state(make_game, demo_adventure):
    # Дополнение §2: «на непустом дневнике ничего не меняет» — даже копии не делает.
    state = open_journal(make_game(), demo_adventure)

    assert open_journal(state, demo_adventure) is state


def test_an_old_game_gets_the_start_location_of_the_adventure_and_not_the_current_place(
    make_game, demo_adventure
):
    # Буква §10 (3): стартовая локация отмечена раундом 0; место, где отряд стоит сейчас, и
    # пройденные места старой игры в стартовый дневник не переносятся.
    state = make_game(location_id="crypt", party_round=9)
    state.world.visited_locations.extend(["village", "cemetery", "crypt"])

    opened = open_journal(state, demo_adventure)

    visited = {e.ref: e.visited_turn for e in opened.journal if e.visited_turn is not None}
    assert visited == {"location:village": 0}
    assert "location:crypt" not in {entry.ref for entry in opened.journal}


# --- финальное покрытие блока: текст и название записи ---


def entities_of(adventure: Adventure, kind: str) -> Mapping[str, Any]:
    if kind == "region":
        return {region.id: region for region in adventure.world.regions}
    tables = {
        "location": adventure.locations,
        "npc": adventure.npcs,
        "deity": adventure.deities,
        "faction": adventure.factions,
        "item": adventure.items,
    }
    return tables[kind]


def contract_text(adventure: Adventure, ref: str) -> str:
    """Текст записи по контракту (дополнение §3): `public` сущности, а если он пуст — название;
    у слуха — `public`, иначе сам слух; у улики — описание найденного."""
    kind, _, key = ref.partition(":")
    if kind == "rumor":
        rumor = adventure.world.rumors[int(key) - 1]
        return rumor.public or rumor.text
    if kind == "clue":
        clues = [clue for rev in adventure.revelations.values() for clue in rev.clues]
        return next(clue.description for clue in clues if clue.id == key)
    entity = entities_of(adventure, kind)[key]
    return entity.public or entity.name


def without_public(data: dict[str, Any]) -> None:
    for table in ("locations", "npcs", "items", "deities", "factions"):
        for entity in data[table].values():
            entity["public"] = ""
    for entity in (*data["world"]["regions"], *data["world"]["rumors"]):
        entity["public"] = ""


@pytest.mark.parametrize("edit", [None, without_public], ids=["public", "без public"])
def test_every_entry_takes_exactly_the_text_of_the_contract_and_the_name_as_its_title(
    make_game, demo_adventure, edit
):
    adventure = variant(demo_adventure, edit) if edit else demo_adventure
    state = make_game()

    for ref in every_ref(adventure):
        entry, new = write(state, adventure, ref)

        assert new is True, ref
        assert entry.text == contract_text(adventure, ref), ref
        kind, _, key = ref.partition(":")
        if kind in ("rumor", "clue"):  # без имени: название — начало текста
            assert entry.title and len(entry.title) <= MAX_TITLE_CHARS, ref
        else:
            assert entry.title == entities_of(adventure, kind)[key].name, ref


@pytest.mark.parametrize(
    ("description", "title"),
    [
        pytest.param("а" * 29 + " " + "б" * 30, "а" * 29 + " " + "б" * 30, id="ровно-60"),
        pytest.param("а" * 29 + " " + "б" * 31, "а" * 29 + "…", id="61-посреди-слова"),
        pytest.param("а" * 59 + " " + "б" * 5, "а" * 59 + "…", id="срез-на-пробеле"),
        pytest.param("я" * 100, "я" * 59 + "…", id="одно-длинное-слово"),
        pytest.param("а" * 30 + ", " + "б" * 40, "а" * 30 + "…", id="без-запятой-в-конце"),
        pytest.param("  Следы\n у   плиты  ", "Следы у плиты", id="пробелы-и-переносы"),
    ],
)
def test_the_title_of_a_clue_is_its_start_up_to_sixty_characters_by_a_word_boundary(
    make_game, demo_adventure, description, title
):
    def edit(data: dict[str, Any]) -> None:
        data["revelations"]["kidnapper"]["clues"][0]["description"] = description

    adventure = variant(demo_adventure, edit)
    clue_id = adventure.revelations["kidnapper"].clues[0].id

    entry, _ = write(make_game(), adventure, f"clue:{clue_id}")

    assert entry.title == title
    assert len(entry.title) <= MAX_TITLE_CHARS
    assert entry.text == description  # текст — описание как есть, сокращается только название


def test_the_title_of_a_rumor_comes_from_its_public_text_and_not_from_the_rumor(
    make_game, demo_adventure
):
    public = "Говорят, что у старого кладбища по ночам ходит кто-то с фонарём и лопатой."
    adventure = variant(
        demo_adventure, lambda data: data["world"]["rumors"][2].update(public=public)
    )

    entry, _ = write(make_game(), adventure, "rumor:3")

    assert entry.title.endswith("…") and public.startswith(entry.title[:-1])
    assert adventure.world.rumors[2].text not in f"{entry.title}\n{entry.text}"


def test_whether_a_rumor_is_true_does_not_change_its_entry_at_all(make_game, demo_adventure):
    # Правда о слухе — только для мастера: записи о правдивом и ложном слухе неотличимы.
    def flipped(data: dict[str, Any]) -> None:
        for rumor in data["world"]["rumors"]:
            rumor["true"] = not rumor["true"]

    refs = [f"rumor:{n}" for n in range(1, len(demo_adventure.world.rumors) + 1)]
    entries = []
    for adventure in (demo_adventure, variant(demo_adventure, flipped)):
        state = open_journal(make_game(), adventure)
        for ref in refs:
            write(state, adventure, ref)
            write(state, adventure, ref, audience="pc-1")
        entries.append(state.journal)

    assert entries[0] == entries[1]


# --- финальное покрытие блока: тайны ---


def hidden_everywhere(data: dict[str, Any]) -> None:
    """Каждое поле приключения, которого нет в контракте текста записи (§3), получает
    уникальную метку «ТАЙНА-n»: тайны, мотивы, заметки мастера, утверждения открытий, описания
    предметов и краёв, история мира, примечания связей и прочие поля сущностей."""
    numbers = iter(range(1, 10_000))

    def mark() -> str:
        return f"ТАЙНА-{next(numbers)}"

    for npc in data["npcs"].values():
        npc.update(secret=mark(), motive=mark(), role=mark(), appearance=mark(), manner=mark())
        npc["knows"] = [mark()]
    for deity in data["deities"].values():
        deity.update(secret=mark(), title=mark(), symbol=mark(), dogma=mark(), appearance=mark())
        deity["attitude_to_mortals"] = mark()
    for faction in data["factions"].values():
        faction.update(secret=mark(), goal=mark(), methods=mark())
    for location in data["locations"].values():
        location.update(dm_notes=mark(), read_aloud=mark())
        for secret in location["secrets"]:
            secret["text"] = mark()
        for part in (*location["features"], *location["exits"]):
            part["description"] = mark()
    for item in data["items"].values():
        item["description"] = mark()
    for revelation in data["revelations"].values():
        revelation["statement"] = mark()
    for region in data["world"]["regions"]:
        region["description"] = mark()
    for relation in data["relations"]:
        relation["note"] = mark()
    data["world"]["history"] = mark()
    for act in data["acts"]:
        act["summary"] = mark()


@pytest.mark.parametrize("edit", [None, without_public], ids=["public", "без public"])
def test_no_hidden_field_of_the_adventure_reaches_any_entry_title_or_text(
    make_game, demo_adventure, edit
):
    def both(data: dict[str, Any]) -> None:
        hidden_everywhere(data)
        if edit:
            edit(data)

    adventure = variant(demo_adventure, both)
    state = open_journal(make_game(party_round=1), adventure)
    for ref in every_ref(adventure):
        write(state, adventure, ref)
        write(state, adventure, ref, audience="pc-2")
    for location_id in adventure.locations:
        visit(state, adventure, location_id)
    write(state, adventure, "world", note="Край принадлежал графам.")

    leaked = [e.id for e in state.journal if "ТАЙНА" in f"{e.title}\n{e.text}"]
    assert leaked == []
    assert len(state.journal) == 2 * len(every_ref(adventure)) + 2  # + «Главное» и история мира


# --- финальное покрытие блока: запись и посещение ---


def test_extending_a_personal_entry_leaves_the_common_entry_about_the_entity_as_it_was(
    make_game, demo_adventure
):
    state = make_game(party_round=3)
    common, _ = write(state, demo_adventure, "faction:pale-candle")
    write(state, demo_adventure, "faction:pale-candle", audience="pc-1", note="Письмо у Фабиана.")

    personal, new = write(
        state, demo_adventure, "faction:pale-candle", audience="pc-1", note="Печать — свеча."
    )

    assert new is False and personal.revision == 2
    assert entry_of(state, "faction:pale-candle@all") == common
    assert [entry.id for entry in state.journal] == [
        "faction:pale-candle@all",
        "faction:pale-candle@pc-1",
    ]


def test_a_new_entry_with_a_note_may_be_exactly_at_the_limit(make_game, demo_adventure):
    public = "ф" * (MAX_TEXT_CHARS - 3)  # и два знака абзаца, и один знак note — ровно предел
    adventure = variant(demo_adventure, lambda data: data["npcs"]["fabian"].update(public=public))
    state = make_game()

    entry, new = write(state, adventure, "npc:fabian", note="ю")

    assert new is True
    assert len(entry.text) == MAX_TEXT_CHARS


@pytest.mark.parametrize(
    "ref", ["rumor:", "rumor:01", "rumor:-1", "rumor:1.0", "rumor: 1", "rumor:1 ", "world:history"]
)
def test_a_ref_that_only_looks_like_a_known_one_is_an_error(make_game, demo_adventure, ref):
    # Слух адресуется ровно номером «rumor:<n>» с 1, история мира — ровно «world».
    state = make_game()

    with pytest.raises(JournalError, match="Нет ссылки"):
        write(state, demo_adventure, ref, note="Т")
    assert state.journal == ()


def test_without_rumors_in_the_adventure_no_rumor_can_be_written(make_game, demo_adventure):
    adventure = variant(demo_adventure, lambda data: data["world"].update(rumors=[]))
    state = make_game()

    with pytest.raises(JournalError, match="Есть: нет"):
        write(state, adventure, "rumor:1", note="Т")
    assert state.journal == ()


def test_the_first_visit_keeps_what_the_master_already_added_about_the_place(
    make_game, demo_adventure
):
    # Мастер рассказал о склепе до прихода отряда: посещение отмечает место, текст не теряется.
    state = make_game(party_round=2)
    written, _ = write(state, demo_adventure, "location:crypt", note="Плита сдвинута.")
    state.party_round = 5

    entry = visit(state, demo_adventure, "crypt")

    assert entry == written.model_copy(update={"revision": 2, "visited_turn": 5})


# --- финальное покрытие блока: выборки для сервера ---


def test_visible_entries_keep_the_order_of_the_journal_and_never_show_others_personal_ones(
    make_game, demo_adventure
):
    state = make_game()
    first, _ = write(state, demo_adventure, "npc:fabian")
    mine, _ = write(state, demo_adventure, "clue:clue-diary", audience="pc-1")
    theirs, _ = write(state, demo_adventure, "clue:clue-diary", audience="pc-2")
    second, _ = write(state, demo_adventure, "deity:veyla")
    mine_too, _ = write(state, demo_adventure, "item:chest-key", audience="pc-1")

    assert visible_entries(state.journal, "pc-1") == (first, mine, second, mine_too)
    assert visible_entries(state.journal, "pc-2") == (first, theirs, second)
    assert visible_entries(state.journal, "pc-9") == (first, second)  # герой без личных записей
    assert visible_entries((), "pc-1") == ()


def test_changed_entries_ignore_removed_entries_and_revisions_that_did_not_grow(
    make_game, demo_adventure
):
    state = make_game()
    write(state, demo_adventure, "npc:fabian", note="Задолжал.")
    old, _ = write(state, demo_adventure, "npc:fabian", note="Сбежал.")
    gone, _ = write(state, demo_adventure, "npc:marta")
    before = state.journal
    lower = old.model_copy(update={"revision": 1})  # ниже прежней (2): не новее
    added, _ = write(make_game(), demo_adventure, "deity:ossmar")

    changed = changed_entries(before, (lower, added))

    assert changed == (added,)
    assert gone not in changed


def test_the_extension_of_a_personal_entry_is_a_change_for_its_hero_only(make_game, demo_adventure):
    state = make_game()
    write(state, demo_adventure, "npc:fabian")
    write(state, demo_adventure, "npc:fabian", audience="pc-2", note="Он врёт.")
    before = state.journal

    write(state, demo_adventure, "npc:fabian", audience="pc-2", note="И боится Марты.")

    [changed] = changed_entries(before, state.journal)
    assert (changed.id, changed.revision) == ("npc:fabian@pc-2", 2)
    assert changed not in visible_entries(state.journal, "pc-1")


# --- fix-раунд 1 (дополнение §11): номер раунда игроков и сущности без текста ---


def test_the_round_of_an_entry_is_the_round_of_the_players_and_not_the_turn_of_the_master(
    make_game, demo_adventure
):
    # Ход мастера (`turn`) растёт и в бою, и на привале; карточка говорит «узнали в раунде N» —
    # это раунд игроков (`party_round`, дополнение §11 п. 5).
    state = open_journal(make_game(turn=9, party_round=2), demo_adventure)

    written, _ = write(state, demo_adventure, "npc:fabian")
    recorded = record(state, demo_adventure, "item:chest-key")
    visited = visit(state, demo_adventure, "crypt")

    assert (written.turn, recorded.turn) == (2, 2)
    assert (visited.turn, visited.visited_turn) == (2, 2)
    assert {entry.turn for entry in state.journal if entry.ref.startswith(("intro", "rumor"))} == {
        0
    }


def test_an_automatic_entry_is_written_once_and_a_repeat_changes_nothing(make_game, demo_adventure):
    state = make_game(party_round=3)

    entry = record(state, demo_adventure, "clue:clue-diary", audience="pc-1")
    again = record(state, demo_adventure, "clue:clue-diary", audience="pc-1")

    assert entry is not None and entry.id == "clue:clue-diary@pc-1" and entry.turn == 3
    assert again is None
    assert state.journal == (entry,)


def without_texts(data: dict[str, Any]) -> None:
    """Сущности без текста: схема и валидатор их пропускают (дополнение §11 п. 1)."""
    data["revelations"]["kidnapper"]["clues"][0]["description"] = ""  # clue-diary
    data["items"]["chest-key"].update(name="", public="")
    data["world"]["rumors"][0].update(text="", public="")  # известен со старта
    data["world"]["regions"][0].update(name="", public="")  # известен со старта
    data["npcs"]["marta"].update(name="", public="")  # известна со старта
    data["locations"]["village"].update(name="", public="")  # стартовая локация
    data["locations"]["crypt"].update(name="", public="")


def test_an_automatic_entry_about_an_entity_without_text_is_not_written_and_does_not_fail(
    make_game, demo_adventure
):
    adventure = variant(demo_adventure, without_texts)
    state = make_game()

    assert record(state, adventure, "clue:clue-diary") is None
    assert record(state, adventure, "item:chest-key", audience="pc-2") is None
    assert visit(state, adventure, "crypt") is None
    assert state.journal == ()


def test_the_start_skips_entities_without_text_including_the_start_location(
    make_game, demo_adventure
):
    adventure = variant(demo_adventure, without_texts)

    state = open_journal(make_game(), adventure)

    refs = {entry.ref for entry in state.journal}
    assert refs == START_REFS - {"rumor:1", "region:marshland", "npc:marta", "location:village"}
    assert all(entry.title and entry.text for entry in state.journal)


def test_the_master_writes_an_entity_without_text_only_with_a_note(make_game, demo_adventure):
    # Строгая запись `journal_add`: без текста и без note — ошибка до изменений.
    adventure = variant(demo_adventure, without_texts)
    state = make_game()

    with pytest.raises(JournalError, match="note"):
        write(state, adventure, "clue:clue-diary")
    assert state.journal == ()
    entry, new = write(state, adventure, "clue:clue-diary", note="Дневник Ульриха.")

    assert (new, entry.text) == (True, "Дневник Ульриха.")
    assert entry.title == "Дневник Ульриха."  # без своего названия — начало текста записи


def test_an_entity_without_a_name_but_with_public_text_is_titled_by_its_text(
    make_game, demo_adventure
):
    adventure = variant(demo_adventure, lambda data: data["npcs"]["fabian"].update(name=""))
    state = make_game()

    entry, _ = write(state, adventure, "npc:fabian")

    assert entry.text == demo_adventure.npcs["fabian"].public
    assert entry.title and entry.text.startswith(entry.title.rstrip("…"))


# --- B11.1 (дополнение контракта B11 §1 п. 4, §2): раунд последнего дополнения ---


def test_extending_an_entry_with_a_note_remembers_the_round_of_the_players(
    make_game, demo_adventure
):
    # Карточка говорит «дополнено в раунде N»: раунд игроков последнего дополнения текста.
    state = make_game(turn=9, party_round=2)

    first, _ = write(state, demo_adventure, "deity:ossmar", note="Его ключ висит на воротах.")
    state.party_round = 5
    second, _ = write(state, demo_adventure, "deity:ossmar", note="Жрецов у него нет.")
    state.party_round = 7
    third, _ = write(state, demo_adventure, "deity:ossmar", note="И храма тоже.")

    assert first.updated_turn is None  # новая запись: раунд её появления — уже `turn`
    assert (second.updated_turn, third.updated_turn) == (5, 7)
    assert (third.turn, third.revision) == (2, 3)
    assert state.journal == (third,)


def test_a_personal_entry_is_extended_by_the_same_rule(make_game, demo_adventure):
    state = make_game(party_round=1)
    write(state, demo_adventure, "npc:fabian", audience="pc-1")
    state.party_round = 3

    entry, _ = write(state, demo_adventure, "npc:fabian", audience="pc-1", note="Он лжёт.")

    assert (entry.audience, entry.updated_turn) == ("pc-1", 3)


def test_a_repeat_without_a_note_keeps_the_round_of_the_last_extension(make_game, demo_adventure):
    state = make_game(party_round=1)
    write(state, demo_adventure, "faction:regulars")
    state.party_round = 3
    extended, _ = write(state, demo_adventure, "faction:regulars", note="Собираются по средам.")
    state.party_round = 6

    entry, new = write(state, demo_adventure, "faction:regulars")

    assert (entry, new) == (extended, False)
    assert entry.updated_turn == 3


def test_a_refused_extension_leaves_the_round_of_the_extension_as_it_was(make_game, demo_adventure):
    # Три заметки по 500 знаков с абзацами — 1504 знака; четвёртая вышла бы за предел 2000.
    state = make_game(party_round=1)
    write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)
    state.party_round = 3
    write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)
    write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)
    state.party_round = 4
    before = state.model_dump()

    with pytest.raises(JournalError):
        write(state, demo_adventure, "world", note="я" * MAX_NOTE_CHARS)

    assert state.model_dump() == before
    assert entry_of(state, "world@all").updated_turn == 3


def test_the_first_visit_and_automatic_entries_are_not_extensions_of_the_text(
    make_game, demo_adventure
):
    # Решение D4: первый приход в место поднимает `revision`, но текст не дополняет.
    state = open_journal(make_game(party_round=2), demo_adventure)

    known = visit(state, demo_adventure, "cemetery")
    unknown = visit(state, demo_adventure, "crypt")
    recorded = record(state, demo_adventure, "item:chest-key")

    assert known.revision == 2
    assert [known.updated_turn, unknown.updated_turn, recorded.updated_turn] == [None] * 3
    assert {entry.updated_turn for entry in state.journal} == {None}


def test_a_visit_keeps_the_round_of_the_last_extension_of_the_place(make_game, demo_adventure):
    state = open_journal(make_game(party_round=1), demo_adventure)
    write(state, demo_adventure, "location:cemetery", note="Ворота скрипят.")
    state.party_round = 4

    visited = visit(state, demo_adventure, "cemetery")

    assert (visited.updated_turn, visited.visited_turn, visited.revision) == (1, 4, 3)


def test_an_entry_saved_before_the_round_of_the_extension_existed_reads_without_it():
    # Сохранённые игры прежних версий: в дампе записи поля нет — оно читается как `None`.
    saved = {
        "id": "npc:marta@all",
        "ref": "npc:marta",
        "section": "people",
        "title": "Марта",
        "text": "Хозяйка трактира.",
        "audience": "all",
        "turn": 0,
        "revision": 2,
        "visited_turn": None,
    }

    entry = JournalEntry.model_validate(saved)

    assert entry.updated_turn is None
    assert entry.model_dump() == saved | {"updated_turn": None}


# --- B11.coverage: раунд дополнения на границах (дополнение контракта B11 §1 п. 4, §2) ---


def test_an_extension_in_round_zero_is_remembered_as_zero_and_not_as_none(
    make_game, demo_adventure
):
    # Раунд 0 — значение, а не «не дополняли»: запись дописали в начале игры. Ни дамп записи, ни
    # сохранённая игра не превращают его в `null`.
    state = make_game(party_round=0)
    write(state, demo_adventure, "deity:ossmar")

    entry, _ = write(state, demo_adventure, "deity:ossmar", note="Его ключ висит на воротах.")

    assert (entry.updated_turn, entry.revision) == (0, 2)
    assert entry.model_dump(mode="json")["updated_turn"] == 0
    restored = GameState.model_validate_json(state.model_dump_json())
    assert entry_of(restored, "deity:ossmar@all").updated_turn == 0


def test_extending_one_entry_does_not_touch_the_other_entry_about_the_same_thing(
    make_game, demo_adventure
):
    # Общая и личная записи о Фабиане — разные записи (`ref@адресат`): раунд дополнения у каждой
    # свой.
    state = make_game(party_round=1)
    common, _ = write(state, demo_adventure, "npc:fabian")
    write(state, demo_adventure, "npc:fabian", audience="pc-1")
    state.party_round = 4

    personal, _ = write(state, demo_adventure, "npc:fabian", audience="pc-1", note="Он лжёт.")

    assert personal.updated_turn == 4
    assert entry_of(state, "npc:fabian@all") == common

    state.party_round = 6
    common, _ = write(state, demo_adventure, "npc:fabian", note="Должен Марте.")

    assert common.updated_turn == 6
    assert entry_of(state, "npc:fabian@pc-1").updated_turn == 4


def test_an_automatic_record_of_an_extended_entry_keeps_the_round_of_the_extension(
    make_game, demo_adventure
):
    # Авто-запись уже записанное не меняет: полученный снова предмет не сбрасывает «дополнено».
    state = make_game(party_round=1)
    write(state, demo_adventure, "item:chest-key")
    state.party_round = 2
    extended, _ = write(state, demo_adventure, "item:chest-key", note="Висит на шнурке.")
    state.party_round = 5

    assert record(state, demo_adventure, "item:chest-key") is None
    assert entry_of(state, "item:chest-key@all") == extended
    assert extended.updated_turn == 2


def test_an_extended_entry_is_among_the_changed_ones_with_its_round(make_game, demo_adventure):
    # Сервер рассылает изменённые записи (`changed_entries`) целиком: клиент получает раунд
    # дополнения в том же событии, что и новый текст.
    state = make_game(party_round=1)
    write(state, demo_adventure, "faction:regulars")
    before = state.journal
    state.party_round = 3

    write(state, demo_adventure, "faction:regulars", note="Собираются по средам.")

    [changed] = changed_entries(before, state.journal)
    assert (changed.id, changed.updated_turn) == ("faction:regulars@all", 3)
    assert changed.model_dump(mode="json")["updated_turn"] == 3


# --- B11.1.fix1 (дополнение B11 §5 п. 1): текст из одних пробелов — пустой текст ---

BLANK = " \n\t "  # пробелы, перенос и табуляция: на экране — пустое место


def blank_texts(data: dict[str, Any]) -> None:
    """Те же сущности без текста, что в `without_texts`, но вместо пустых строк — пробелы."""
    data["revelations"]["kidnapper"]["clues"][0]["description"] = BLANK  # clue-diary
    data["items"]["chest-key"].update(name=BLANK, public=BLANK)
    data["world"]["rumors"][0].update(text=BLANK, public=BLANK)  # известен со старта
    data["world"]["regions"][0].update(name=BLANK, public=BLANK)  # известен со старта
    data["npcs"]["marta"].update(name=BLANK, public=BLANK)  # известна со старта
    data["locations"]["village"].update(name=BLANK, public=BLANK)  # стартовая локация
    data["locations"]["crypt"].update(name=BLANK, public=BLANK)


def test_an_automatic_entry_about_an_entity_of_only_spaces_is_not_written(
    make_game, demo_adventure
):
    # Валидатор считает такие тексты пустыми (`clue_without_description`, `item_without_text`):
    # дневник следует тому же правилу и не пишет запись, которая выглядит пустой.
    adventure = variant(demo_adventure, blank_texts)
    state = make_game()

    assert record(state, adventure, "clue:clue-diary") is None
    assert record(state, adventure, "item:chest-key", audience="pc-2") is None
    assert record(state, adventure, "rumor:1") is None
    assert visit(state, adventure, "crypt") is None
    assert state.journal == ()


def test_the_start_skips_entities_of_only_spaces_including_the_start_location(
    make_game, demo_adventure
):
    adventure = variant(demo_adventure, blank_texts)

    state = open_journal(make_game(), adventure)

    refs = {entry.ref for entry in state.journal}
    assert refs == START_REFS - {"rumor:1", "region:marshland", "npc:marta", "location:village"}
    assert all(entry.title.strip() and entry.text.strip() for entry in state.journal)


def test_the_master_writes_an_entity_of_only_spaces_only_with_a_note(make_game, demo_adventure):
    adventure = variant(demo_adventure, blank_texts)
    state = make_game()

    for ref in ("clue:clue-diary", "item:chest-key", "rumor:1", "npc:marta", "location:crypt"):
        with pytest.raises(JournalError, match="note"):
            write(state, adventure, ref)
    assert state.journal == ()
    entry, _ = write(state, adventure, "item:chest-key", note="Маленький ключ.")

    assert (entry.title, entry.text) == ("Маленький ключ.", "Маленький ключ.")


NAMED_ENTITIES = [
    pytest.param(("locations", "crypt"), "location:crypt", id="location"),
    pytest.param(("npcs", "fabian"), "npc:fabian", id="npc"),
    pytest.param(("deities", "ossmar"), "deity:ossmar", id="deity"),
    pytest.param(("factions", "regulars"), "faction:regulars", id="faction"),
    pytest.param(("world", "regions", 1), "region:barrow-hill", id="region"),
    pytest.param(("items", "chest-key"), "item:chest-key", id="item"),
]


def with_fields(adventure: Adventure, path: tuple[str | int, ...], **fields: str) -> Adventure:
    """Копия приключения, где у сущности по пути `path` в данных изменены поля `fields`."""

    def edit(data: dict[str, Any]) -> None:
        target: Any = data
        for key in path:
            target = target[key]
        target.update(fields)

    return variant(adventure, edit)


@pytest.mark.parametrize(("path", "ref"), NAMED_ENTITIES)
def test_a_public_text_of_only_spaces_gives_way_to_the_name(make_game, demo_adventure, path, ref):
    adventure = with_fields(demo_adventure, path, name="Название сущности", public=BLANK)

    entry, _ = write(make_game(), adventure, ref)

    assert (entry.title, entry.text) == ("Название сущности", "Название сущности")


@pytest.mark.parametrize(("path", "ref"), NAMED_ENTITIES)
def test_a_name_of_only_spaces_is_no_title_and_the_title_comes_from_the_text(
    make_game, demo_adventure, path, ref
):
    adventure = with_fields(demo_adventure, path, name=BLANK, public="Что знают о сущности.")

    entry, _ = write(make_game(), adventure, ref)

    assert (entry.title, entry.text) == ("Что знают о сущности.", "Что знают о сущности.")


def test_a_rumor_with_a_public_text_of_only_spaces_is_written_by_the_rumor_itself(
    make_game, demo_adventure
):
    adventure = variant(
        demo_adventure, lambda data: data["world"]["rumors"][1].update(public=BLANK)
    )
    rumor = demo_adventure.world.rumors[1].text

    entry, _ = write(make_game(), adventure, "rumor:2")

    assert entry.text == rumor
    assert entry.title.strip() and rumor.startswith(entry.title.rstrip("…"))


def test_an_intro_of_only_spaces_gives_way_to_the_pitch(make_game, demo_adventure):
    # Схема заменяет пустое `journal_intro` на `pitch`; из одних пробелов — тоже пустое.
    adventure = variant(demo_adventure, lambda data: data.update(journal_intro=BLANK))

    state = open_journal(make_game(), adventure)

    assert entry_of(state, "intro@all").text == demo_adventure.pitch


def test_an_intro_and_a_pitch_of_only_spaces_give_no_main_entry(make_game, demo_adventure):
    adventure = variant(demo_adventure, lambda data: data.update(pitch=BLANK, journal_intro=BLANK))

    state = open_journal(make_game(), adventure)

    assert "intro" not in {entry.ref for entry in state.journal}
    assert len(state.journal) == len(START_REFS) - 1
