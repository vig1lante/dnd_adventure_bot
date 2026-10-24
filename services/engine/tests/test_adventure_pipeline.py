"""Приключение целиком: файлы, загрузчик, схема и валидатор (спека §6.1–§6.2, план B4.1–B4.4).

Каждая часть конвейера уже проверена отдельно (`test_adventure_schema`, `test_adventure_loader`,
`test_validator`, `test_demo_adventure`). Здесь проверяются стыки: настоящие файлы `demo-crypt`
проходят загрузчик и валидатор так же, как приключение в памяти; каждая ошибка и предупреждение
из спеки §6.2 находится по испорченным файлам; ни один файл папки не теряется молча.
"""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from adventure_factory import adventure_data, custom_monster_data, make_adventure

from dnd_engine.adventure.loader import AdventureFormatError, dump_adventure, load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import Report, validate
from dnd_engine.content import SrdContent

# Корень репозитория считаем от расположения файла (tests -> engine -> services -> корень).
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[3] / "content" / "adventures" / "demo-crypt"

# --- помощники ---


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Копия настоящей папки `demo-crypt`, которую тест портит, как автор правит файлы."""
    copy = tmp_path / "demo-crypt"
    shutil.copytree(DEMO_ADVENTURE_DIR, copy)
    return copy


def edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
    """Читает YAML-файл, правит данные на месте и записывает обратно."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def relative_files(root: Path) -> set[str]:
    """Файлы папки приключения без служебных (`.DS_Store` и подобных): они не часть приключения."""
    return {
        file.relative_to(root).as_posix()
        for file in root.rglob("*")
        if file.is_file() and not file.name.startswith(".")
    }


def triples(report: Report) -> set[tuple[str, str, str]]:
    return {(issue.code, issue.severity, issue.ref) for issue in report.issues}


def errors_of(report: Report) -> set[tuple[str, str, str]]:
    return {found for found in triples(report) if found[1] == "error"}


def score_follows_the_issues(report: Report) -> bool:
    """Балл: 100 минус 10 за ошибку и 3 за предупреждение."""
    errors = len(errors_of(report))
    return report.score == max(0, 100 - 10 * errors - 3 * (len(report.issues) - errors))


# --- настоящие файлы demo-crypt проходят весь конвейер ---


def test_an_untouched_copy_of_the_shipped_folder_is_the_clean_start_of_every_spoiled_case(
    folder: Path, demo_adventure: Adventure, srd: SrdContent
):
    adventure = load_adventure(folder)

    assert adventure == demo_adventure
    assert not validate(adventure, srd).blocking  # дальше ошибку вносит только правка теста


def test_no_file_of_the_shipped_folder_is_left_unread_by_the_loader(
    demo_adventure: Adventure, tmp_path: Path
):
    # Загрузчик читает только `<папка>/<id>.yaml`: файл с опечаткой в имени или папке (`.yml`,
    # `item/`) молча выпал бы из приключения. Запись загруженного даёт ровно те же файлы.
    dump_adventure(demo_adventure, tmp_path / "dumped")

    assert relative_files(DEMO_ADVENTURE_DIR) == relative_files(tmp_path / "dumped")


def broken_adventure() -> Adventure:
    """Приключение, которое проходит схему, но не валидатор: ошибки и предупреждения разом."""
    data = adventure_data()
    data["locations"]["village"]["read_aloud"] = ""
    data["locations"]["village"]["exits"][0]["to"] = "moon"
    data["encounters"]["bones"]["monsters"][0]["monster_id"] = "ghost-knight"
    data["revelations"]["kidnapper"]["clues"].pop()
    data["npcs"]["marta"]["motive"] = ""
    return Adventure.model_validate(data)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: make_adventure(), id="чистое"),
        pytest.param(
            lambda: make_adventure(monsters={"crypt-rat": custom_monster_data()}, seeds=("marta",)),
            id="свои статблоки и seeds",
        ),
        pytest.param(broken_adventure, id="с ошибками и предупреждениями"),
    ],
)
def test_writing_and_reading_a_folder_does_not_change_what_the_validator_says(
    build: Callable[[], Adventure], srd: SrdContent, tmp_path: Path
):
    adventure = build()
    dump_adventure(adventure, tmp_path)

    loaded = load_adventure(tmp_path)

    assert loaded == adventure
    before, after = validate(adventure, srd), validate(loaded, srd)
    # Загрузчик читает файлы по алфавиту, поэтому порядок проблем в отчёте может отличаться.
    assert set(after.issues) == set(before.issues) and len(after.issues) == len(before.issues)
    assert (after.score, after.estimated_minutes) == (before.score, before.estimated_minutes)


def test_the_broken_adventure_really_has_both_errors_and_warnings(srd: SrdContent):
    report = validate(broken_adventure(), srd)

    assert report.blocking
    assert {"dangling_ref", "unknown_monster", "too_few_clues"} <= {i.code for i in report.issues}
    assert {"empty_read_aloud", "npc_without_motive"} <= {i.code for i in report.issues}


# --- спека §6.2: блокирующие ошибки находятся по испорченным файлам ---


def point_exit_into_the_void(folder: Path) -> None:
    edit_yaml(folder / "locations/village.yaml", lambda d: d["exits"][0].update(to="moon"))


def cut_the_way_to_the_crypt(folder: Path) -> None:
    edit_yaml(
        folder / "locations/cemetery.yaml",
        lambda d: d.update(exits=[e for e in d["exits"] if e["id"] != "to-crypt"]),
    )


def drop_a_clue(folder: Path) -> None:
    edit_yaml(folder / "revelations.yaml", lambda d: d[0]["clues"].pop())


def send_skeletons_from_nowhere(folder: Path) -> None:
    edit_yaml(
        folder / "encounters/risen-bones.yaml",
        lambda d: d["monsters"][0].update(monster_id="ghost-knight"),
    )


def lock_the_chest_with_a_missing_key(folder: Path) -> None:
    edit_yaml(
        folder / "locations/cemetery.yaml",
        lambda d: next(f for f in d["features"] if f["id"] == "chest").update(
            locked_by="item:lost-key"
        ),
    )


def send_the_party_to_a_missing_zone(folder: Path) -> None:
    edit_yaml(folder / "encounters/fabian-fight.yaml", lambda d: d.update(pc_zone="attic"))


def put_all_clues_in_one_place(folder: Path) -> None:
    def change(revelations: list[dict[str, Any]]) -> None:
        for clue in revelations[0]["clues"]:
            clue["where"] = "feature:diary"

    edit_yaml(folder / "revelations.yaml", change)


BLOCKING_BREAKS = [
    pytest.param(
        point_exit_into_the_void,
        {("dangling_ref", "error", "location:village/exit:to-cemetery")},
        id="ссылка на несуществующий id",
    ),
    pytest.param(
        cut_the_way_to_the_crypt,
        {
            ("unreachable_location", "error", "location:crypt"),
            ("no_path_to_finale", "error", "location:crypt"),
        },
        id="до локации и финала не дойти",
    ),
    pytest.param(
        drop_a_clue,
        {("too_few_clues", "error", "revelation:kidnapper")},
        id="у открытия меньше трёх улик",
    ),
    pytest.param(
        send_skeletons_from_nowhere,
        {("unknown_monster", "error", "encounter:risen-bones")},
        id="монстра нет ни в SRD, ни в статблоках",
    ),
    pytest.param(
        lock_the_chest_with_a_missing_key,
        {("dangling_ref", "error", "location:cemetery/feature:chest")},
        id="замок сундука ссылается на несуществующий предмет",
    ),
    pytest.param(
        send_the_party_to_a_missing_zone,
        {("dangling_ref", "error", "encounter:fabian-fight")},
        id="зоны боя нет в локации",
    ),
    pytest.param(
        put_all_clues_in_one_place,
        {("clues_same_place", "error", "revelation:kidnapper")},
        id="все улики в одном месте",
    ),
]


@pytest.mark.parametrize(("spoil", "expected"), BLOCKING_BREAKS)
def test_a_spoiled_file_gives_a_blocking_error_through_loader_and_validator(
    folder: Path, srd: SrdContent, spoil: Callable[[Path], None], expected: set
):
    spoil(folder)

    report = validate(load_adventure(folder), srd)  # схема такое пропускает, ловит валидатор

    assert report.blocking
    assert expected <= errors_of(report)
    assert score_follows_the_issues(report)


# Файл, не подходящий под схему, останавливает загрузчик: до валидатора дело не доходит.
SCHEMA_BREAKS = [
    pytest.param(
        "locations/village.yaml",
        lambda d: d.update(weather="дождь"),
        id="лишнее поле",
    ),
    pytest.param(
        "locations/cemetery.yaml",
        lambda d: next(f for f in d["features"] if f["id"] == "tracks")["check"].update(dc=13),
        id="Сл проверки вне таблицы",
    ),
    pytest.param(
        "locations/cemetery.yaml",
        lambda d: next(e for e in d["exits"] if e["id"] == "to-crypt").update(hidden_dc=13),
        id="Сл поиска скрытого выхода вне таблицы",
    ),
    pytest.param(
        "npcs/marta.yaml",
        lambda d: d.update(id="martha"),
        id="id не совпадает с именем файла",
    ),
    pytest.param(
        "items/chest-key.yaml",
        lambda d: d.update(id="../chest-key"),
        id="id выводит за пределы папки",
    ),
    pytest.param(
        "encounters/fabian-fight.yaml",
        lambda d: d["monsters"][0].update(count=0),
        id="ни одного монстра в группе",
    ),
]


@pytest.mark.parametrize(("name", "spoil"), SCHEMA_BREAKS)
def test_a_file_that_breaks_the_schema_is_a_loader_error_with_its_path(
    folder: Path, name: str, spoil: Callable[[Any], None]
):
    edit_yaml(folder / name, spoil)

    with pytest.raises(AdventureFormatError) as caught:
        load_adventure(folder)

    assert any(problem.startswith(name) for problem in caught.value.problems)


# --- спека §6.2: предупреждения ---


def empty_the_village_text(folder: Path) -> None:
    edit_yaml(folder / "locations/village.yaml", lambda d: d.update(read_aloud=""))


def strip_the_village_features(folder: Path) -> None:
    edit_yaml(folder / "locations/village.yaml", lambda d: d.update(features=[]))


def take_marta_motive(folder: Path) -> None:
    edit_yaml(folder / "npcs/marta.yaml", lambda d: d.update(motive=""))


def isolate_fabian(folder: Path) -> None:
    # Связь считается с любого конца: убираем из графа все связи Фабиана, он со статблоком важен.
    def drop_fabian(relations: list[dict[str, Any]]) -> None:
        relations[:] = [r for r in relations if "npc:fabian" not in (r["a"], r["b"])]

    edit_yaml(folder / "relations.yaml", drop_fabian)


def gather_a_deadly_crowd(folder: Path) -> None:
    edit_yaml(
        folder / "encounters/fabian-fight.yaml",
        lambda d: d.update(
            monsters=[{"monster_id": "cultist_fanatic", "count": 9, "zone": "hall"}]
        ),
    )


WARNING_BREAKS = [
    pytest.param(
        empty_the_village_text,
        {("empty_read_aloud", "warning", "location:village")},
        id="пустой текст для чтения вслух",
    ),
    pytest.param(
        strip_the_village_features,
        {("no_features", "warning", "location:village")},
        id="нет интересных мест",
    ),
    pytest.param(
        take_marta_motive,
        {("npc_without_motive", "warning", "npc:marta")},
        id="у NPC нет мотива",
    ),
    pytest.param(
        isolate_fabian,
        {("npc_without_relations", "warning", "npc:fabian")},
        id="у важного NPC нет связей",
    ),
    pytest.param(
        gather_a_deadly_crowd,
        {("encounter_too_hard", "warning", "encounter:fabian-fight")},
        id="бой выходит за бюджет партии",
    ),
]


@pytest.mark.parametrize(("spoil", "expected"), WARNING_BREAKS)
def test_a_spoiled_file_gives_a_warning_that_does_not_block_the_game(
    folder: Path, srd: SrdContent, spoil: Callable[[Path], None], expected: set
):
    spoil(folder)

    report = validate(load_adventure(folder), srd)

    assert not report.blocking
    assert expected <= triples(report)
    assert score_follows_the_issues(report)


def test_the_time_estimate_warning_goes_away_when_the_target_matches(folder: Path, srd: SrdContent):
    estimate = validate(load_adventure(folder), srd).estimated_minutes
    edit_yaml(folder / "adventure.yaml", lambda d: d.update(target_hours=estimate / 60))

    report = validate(load_adventure(folder), srd)

    assert ("duration_mismatch", "warning", "adventure") not in triples(report)
    assert report.estimated_minutes == estimate  # оценка зависит от содержимого, а не от цели


def test_a_target_far_from_the_estimate_is_a_warning_in_both_directions(
    folder: Path, srd: SrdContent
):
    estimate_hours = validate(load_adventure(folder), srd).estimated_minutes / 60
    for target in (estimate_hours * 2, estimate_hours / 2):
        edit_yaml(folder / "adventure.yaml", lambda d, target=target: d.update(target_hours=target))

        report = validate(load_adventure(folder), srd)

        assert ("duration_mismatch", "warning", "adventure") in triples(report)
        assert not report.blocking


# --- потеря файла не остаётся незамеченной ---

DEMO_FILES = sorted(relative_files(DEMO_ADVENTURE_DIR))


def test_every_shipped_file_is_a_yaml_file_and_is_in_the_parametrization_below():
    assert "adventure.yaml" in DEMO_FILES and "revelations.yaml" in DEMO_FILES
    assert all(name.endswith(".yaml") for name in DEMO_FILES), DEMO_FILES


@pytest.mark.parametrize("name", DEMO_FILES)
def test_losing_any_file_of_the_adventure_is_either_a_loader_error_or_a_blocking_report(
    folder: Path, srd: SrdContent, name: str
):
    (folder / name).unlink()

    try:
        adventure = load_adventure(folder)
    except AdventureFormatError as error:
        assert any(problem.startswith(name) for problem in error.problems)
    else:
        assert validate(adventure, srd).blocking, f"{name} пропал, а приключение «в порядке»"
