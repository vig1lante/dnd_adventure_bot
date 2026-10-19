"""Рукописное мини-приключение `demo-crypt` (план B4.4, спека §6.1–§6.2).

Приключение — данные в `content/adventures/demo-crypt/`. На него опираются тесты инструментов
мастера (скрытый выход, запертый сундук, поиск по лору, бой «Встающие кости»), поэтому здесь
зафиксированы не только «валидатор доволен», но и те id и связи, которые эти тесты читают.
"""

from collections import Counter
from pathlib import Path

import pytest

from dnd_engine.adventure.loader import dump_adventure, load_adventure
from dnd_engine.adventure.schema import Adventure, Exit
from dnd_engine.adventure.validator import validate
from dnd_engine.content import SrdContent, load_srd

# Предупреждения демо-приключения заданы явно: правка контента, которая добавит или уберёт
# предупреждение, должна быть заметна в тесте. Оба боя из плана «слишком лёгкие» для шести героев
# 3-го уровня (решение владельца B4.4), а оценка валидатора (116 мин) выше цели 1,5 ч больше
# чем на 25%: формула считает полные 30 минут на каждый бой.
EXPECTED_WARNINGS = {
    ("encounter_too_easy", "encounter:risen-bones"),
    ("encounter_too_easy", "encounter:fabian-fight"),
    ("duration_mismatch", "adventure"),
}

# Текст приключения — данные, а не инструкции модели: фразы вроде этих в нём недопустимы.
INSTRUCTION_MARKERS = (
    "игнорируй",
    "забудь",
    "system prompt",
    "системный промпт",
    "ignore previous",
    "ты — мастер",
    "ты мастер",
    "вызови инструмент",
)


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


# --- общая форма и проверка валидатором ---


def test_adventure_header_follows_the_plan(demo_adventure: Adventure):
    assert demo_adventure.id == "demo-crypt"
    assert "юмор" in demo_adventure.tone
    assert (demo_adventure.party_level, demo_adventure.party_size) == (3, 6)
    assert demo_adventure.rules.base == "srd-5.2"
    assert demo_adventure.rules.modules == ["bg3_flavor", "campfire"]
    assert 1 <= demo_adventure.target_hours <= 1.5
    assert (demo_adventure.start_location, demo_adventure.finale_location) == ("village", "crypt")
    assert set(demo_adventure.locations) == {"village", "cemetery", "crypt"}
    assert set(demo_adventure.npcs) == {"marta", "fabian"}


def test_validator_finds_no_errors(demo_adventure: Adventure, srd: SrdContent):
    report = validate(demo_adventure, srd)

    assert not report.blocking
    assert [issue for issue in report.issues if issue.severity == "error"] == []


def test_estimated_time_fits_the_hour_to_two_window(demo_adventure: Adventure, srd: SrdContent):
    assert 60 <= validate(demo_adventure, srd).estimated_minutes <= 120


def test_warnings_are_exactly_the_expected_ones(demo_adventure: Adventure, srd: SrdContent):
    report = validate(demo_adventure, srd)

    assert {(issue.code, issue.ref) for issue in report.issues} == EXPECTED_WARNINGS
    assert report.score == 100 - 3 * len(EXPECTED_WARNINGS)


def test_dump_and_load_give_an_equal_adventure(demo_adventure: Adventure, tmp_path: Path):
    dump_adventure(demo_adventure, tmp_path)

    assert load_adventure(tmp_path) == demo_adventure


def test_every_act_has_key_scenes_and_there_are_several_endings(demo_adventure: Adventure):
    assert len(demo_adventure.acts) == 3
    assert all(act.key_scenes for act in demo_adventure.acts)
    assert len(demo_adventure.endings) >= 2


def test_text_is_data_not_instructions_to_the_model(demo_adventure: Adventure):
    text = demo_adventure.model_dump_json().lower()

    assert [marker for marker in INSTRUCTION_MARKERS if marker in text] == []


# --- выходы ---


def exit_of(adventure: Adventure, location_id: str, exit_id: str) -> Exit:
    return next(e for e in adventure.locations[location_id].exits if e.id == exit_id)


def test_the_way_into_the_crypt_is_hidden_from_the_cemetery(demo_adventure: Adventure):
    to_crypt = exit_of(demo_adventure, "cemetery", "to-crypt")

    assert to_crypt.to == "crypt"
    assert to_crypt.hidden_dc == 15
    assert to_crypt.locked_by is None  # запертым в приключении бывает только сундук


@pytest.mark.parametrize(
    ("location_id", "exit_id", "target"),
    [
        ("village", "to-cemetery", "cemetery"),
        ("cemetery", "to-village", "village"),
        ("crypt", "to-cemetery", "cemetery"),
    ],
)
def test_other_exits_are_plain_open_ones(
    demo_adventure: Adventure, location_id: str, exit_id: str, target: str
):
    exit_ = exit_of(demo_adventure, location_id, exit_id)

    assert exit_.to == target
    assert (exit_.hidden_dc, exit_.locked_by) == (None, None)


def test_the_crypt_is_reachable_only_through_the_cemetery(demo_adventure: Adventure):
    entrances = [
        (location.id, exit_.id)
        for location in demo_adventure.locations.values()
        for exit_ in location.exits
        if exit_.to == "crypt"
    ]

    assert entrances == [("cemetery", "to-crypt")]


# --- сундук, ключ и зелье ---


def test_the_chest_in_the_cemetery_is_locked_by_the_key(demo_adventure: Adventure):
    features = {f.id: f for f in demo_adventure.locations["cemetery"].features}
    chest = features["chest"]

    assert chest.locked_by == "item:chest-key"
    assert chest.check is None  # замок открывает только ключ, а не проверка навыка
    assert [(r.kind, r.id) for r in chest.reveals] == [("item", "healing-potion")]


def test_key_and_potion_are_the_two_items_of_the_adventure(demo_adventure: Adventure):
    key = demo_adventure.items["chest-key"]
    potion = demo_adventure.items["healing-potion"]

    assert set(demo_adventure.items) == {"chest-key", "healing-potion"}
    assert key.base_item is None
    assert key.holder == "npc:marta"  # ключ отдаёт Марта: отряд получает его в деревне
    assert potion.base_item == "potion_of_healing"
    assert potion.holder == "location:cemetery"  # лежит в сундуке, пока тот заперт


# --- NPC ---


def test_marta_the_innkeeper_waits_in_the_village(demo_adventure: Adventure):
    marta = demo_adventure.npcs["marta"]

    assert (marta.location_id, marta.statblock) == ("village", None)
    assert [(r.npc_id, r.kind) for r in marta.relations] == [("fabian", "debtor")]
    assert "брат" in marta.secret + marta.motive + " ".join(marta.knows)


def test_fabian_the_failed_necromancer_is_the_creditors_debtor(demo_adventure: Adventure):
    fabian = demo_adventure.npcs["fabian"]

    assert (fabian.location_id, fabian.statblock) == ("crypt", "cultist_fanatic")
    assert [(r.npc_id, r.kind) for r in fabian.relations] == [("marta", "creditor")]


def test_the_word_debt_is_in_fabians_own_texts_for_lore_search(demo_adventure: Adventure):
    # search_lore("долг") (B4.5) ищет по текстам: Фабиан должен находиться и без чужих описаний.
    fabian = demo_adventure.npcs["fabian"]
    own_texts = [fabian.motive, fabian.secret, fabian.manner, fabian.appearance, *fabian.knows]

    assert "долг" in " ".join(own_texts).lower()


# --- интересные места и открытие ---


def test_each_location_has_features_and_read_aloud_and_dm_notes(demo_adventure: Adventure):
    for location in demo_adventure.locations.values():
        assert location.features, location.id
        assert location.read_aloud.strip(), location.id
        # Заметки мастера нужны тесту краткого описания приключения (B7.2): в нём их быть не должно.
        assert location.dm_notes.strip(), location.id


def test_village_has_a_notice_board(demo_adventure: Adventure):
    assert "board" in {f.id for f in demo_adventure.locations["village"].features}


def test_three_clues_of_the_revelation_lie_in_different_places(demo_adventure: Adventure):
    revelation = demo_adventure.revelations["kidnapper"]

    assert "Фабиан" in revelation.statement
    assert {clue.where for clue in revelation.clues} == {
        "feature:diary",
        "npc:marta",
        "feature:tracks",
    }


def test_diary_and_tracks_clues_are_given_by_cemetery_features(demo_adventure: Adventure):
    # Улики-места раскрываются инструментом reveal: интересное место обязано ссылаться на улику.
    features = {f.id: f for f in demo_adventure.locations["cemetery"].features}

    assert ("clue", "clue-diary") in {(r.kind, r.id) for r in features["diary"].reveals}
    assert ("clue", "clue-tracks") in {(r.kind, r.id) for r in features["tracks"].reveals}


# --- бои ---


def monster_counts(adventure: Adventure, encounter_id: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for group in adventure.encounters[encounter_id].monsters:
        counts[group.monster_id] += group.count
    return counts


def test_risen_bones_fight_is_three_skeletons_and_two_zombies(demo_adventure: Adventure):
    encounter = demo_adventure.encounters["risen-bones"]

    assert encounter.location_id == "cemetery"
    assert demo_adventure.locations["cemetery"].encounter_id == "risen-bones"
    assert monster_counts(demo_adventure, "risen-bones") == {"skeleton": 3, "zombie": 2}


def test_fabian_fight_is_fabian_and_two_skeletons(demo_adventure: Adventure):
    encounter = demo_adventure.encounters["fabian-fight"]

    assert encounter.location_id == "crypt"
    assert demo_adventure.locations["crypt"].encounter_id == "fabian-fight"
    # Статблок Фабиана — культист-фанатик (поле statblock его NPC), он выходит на бой сам.
    assert monster_counts(demo_adventure, "fabian-fight") == {"cultist_fanatic": 1, "skeleton": 2}


@pytest.mark.parametrize(
    ("location_id", "encounter_id"), [("cemetery", "risen-bones"), ("crypt", "fabian-fight")]
)
def test_fight_zones_are_laid_out_so_that_enemies_start_apart_from_the_party(
    demo_adventure: Adventure, location_id: str, encounter_id: str
):
    location = demo_adventure.locations[location_id]
    encounter = demo_adventure.encounters[encounter_id]
    enemy_zones = {group.zone for group in encounter.monsters}

    assert len(location.zones) >= 3
    assert location.links  # бойцы перемещаются только по связям между зонами
    assert encounter.pc_zone not in enemy_zones
