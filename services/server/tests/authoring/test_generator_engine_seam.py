"""Генератор и движок: стыки, которые тесты по отдельности не видят (план B5.6, спека §6.2, §6.3).

- `owner_step` получает проблемы настоящего валидатора, а не написанные вручную: если валидатор
  изменит `ref` или добавит код, маршрут к шагу-владельцу не должен сломаться молча;
- результат `dnd-generate` читается загрузчиком и проверяется валидатором так же, как исходный,
  в том числе когда блокирующие ошибки остались и приключение доделывает человек;
- установленная команда, как её запускает владелец (README, «Генерация приключения»).
Живой модели нет: ответы шагов проигрывает `FakeProvider` по сценарию `generator_demo.yaml`.
"""

import copy
import re
import subprocess
import sysconfig
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dnd_engine.adventure import validator
from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import validate
from dnd_engine.content import load_srd
from dnd_server.authoring.cli import main
from dnd_server.authoring.pipeline import GenerationRequest, generate, owner_step
from dnd_server.authoring.schemas import Seeds
from dnd_server.llm.fake import FakeProvider, load_scenario

DEMO_SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "generator_demo.yaml"
REQUEST = GenerationRequest(
    pitch="Колокол в часовне звонит сам по ночам.",
    hours=1,
    players=6,
    level=3,
    modules=("bg3_flavor", "campfire"),
)
CLI_ARGS = ["--pitch", "Колокол звонит.", "--players", "6", "--level", "3"]
CLI_MODULES = ["--modules", "bg3_flavor,campfire"]

Mutation = Callable[[dict[str, Any]], None]


async def generated_data() -> dict[str, Any]:
    """Данные приключения, которое собрал генератор по демо-сценарию: исходник для поломок."""
    provider = FakeProvider(load_scenario(DEMO_SCENARIO))
    result = await generate(provider, REQUEST, Seeds())
    assert not result.report.blocking
    return result.adventure.model_dump(mode="json")


# --- маршрут проблем валидатора к шагам генератора ---


def orphan_location(data: dict[str, Any]) -> None:
    data["locations"]["well"] = {
        "id": "well",
        "name": "Колодец",
        "read_aloud": "Из колодца тянет холодом.",
        "features": [{"id": "well-stones", "name": "Камни", "description": "Мокрые камни."}],
    }


def chapel_without_exits(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["exits"] = []


def two_clues(data: dict[str, Any]) -> None:
    data["revelations"]["bell-truth"]["clues"] = data["revelations"]["bell-truth"]["clues"][:2]


def clues_in_one_place(data: dict[str, Any]) -> None:
    for clue in data["revelations"]["bell-truth"]["clues"]:
        clue["where"] = "feature:bell-rope"


def unknown_monster(data: dict[str, Any]) -> None:
    data["encounters"]["ghoul-pack"]["monsters"][0]["monster_id"] = "dragon-of-doom"


def deadly_fight(data: dict[str, Any]) -> None:
    data["encounters"]["ghoul-pack"]["monsters"] = [{"monster_id": "ghoul", "count": 20}]


def trivial_fight(data: dict[str, Any]) -> None:
    data["encounters"]["ghoul-pack"]["monsters"] = [{"monster_id": "skeleton", "count": 1}]


def key_that_left_the_game(data: dict[str, Any]) -> None:
    data["items"]["undercroft-key"]["holder"] = "gone"


def exit_to_nowhere(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["exits"][0]["to"] = "lost-tower"


def npc_in_nowhere(data: dict[str, Any]) -> None:
    data["npcs"]["otto"]["location_id"] = "lost-tower"


def item_held_in_nowhere(data: dict[str, Any]) -> None:
    data["items"]["undercroft-key"]["holder"] = "location:lost-tower"


def fight_in_nowhere(data: dict[str, Any]) -> None:
    data["encounters"]["ghoul-pack"]["location_id"] = "lost-tower"


def fight_its_location_does_not_show(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["encounter_id"] = None


def clue_in_nowhere(data: dict[str, Any]) -> None:
    data["revelations"]["bell-truth"]["clues"][1]["where"] = "npc:lost-npc"


def start_in_nowhere(data: dict[str, Any]) -> None:
    data["start_location"] = "lost-tower"


def item_with_srd_id(data: dict[str, Any]) -> None:
    data["items"]["rope"] = {
        "id": "rope",
        "name": "Верёвка",
        "description": "Моток.",
        "holder": "location:chapel",
    }


def lock_on_engine_flag(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["exits"][0]["locked_by"] = "flag:encounter:ghoul-pack"


def unknown_rule_module(data: dict[str, Any]) -> None:
    data["rules"]["modules"] = ["no_such_module"]


def repeated_feature_id(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["features"][0]["id"] = "bell-rope"


def npc_without_motive(data: dict[str, Any]) -> None:
    data["npcs"]["otto"]["motive"] = ""


def important_npc_without_relations(data: dict[str, Any]) -> None:
    for npc in data["npcs"].values():
        npc["relations"] = []
    data["npcs"]["otto"]["statblock"] = "skeleton"


def silent_location(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["read_aloud"] = " "


def location_without_features(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["features"] = []


def long_game_for_a_short_wish(data: dict[str, Any]) -> None:
    data["target_hours"] = 10


# (имя, поломка, код, ref настоящего валидатора, шаг-владелец или None)
ROUTES: list[tuple[str, Mutation, str, str, str | None]] = [
    ("orphan location", orphan_location, "unreachable_location", "location:well", "locations"),
    (
        "no path to finale",
        chapel_without_exits,
        "no_path_to_finale",
        "location:undercroft",
        "locations",
    ),
    ("two clues", two_clues, "too_few_clues", "revelation:bell-truth", "clues"),
    ("one place", clues_in_one_place, "clues_same_place", "revelation:bell-truth", "clues"),
    ("unknown monster", unknown_monster, "unknown_monster", "encounter:ghoul-pack", "encounters"),
    ("deadly", deadly_fight, "encounter_too_hard", "encounter:ghoul-pack", "encounters"),
    ("trivial", trivial_fight, "encounter_too_easy", "encounter:ghoul-pack", "encounters"),
    (
        "lost key",
        key_that_left_the_game,
        "unreachable_key",
        "location:chapel/exit:to-undercroft",
        "items",
    ),
    (
        "exit to nowhere",
        exit_to_nowhere,
        "dangling_ref",
        "location:chapel/exit:to-undercroft",
        "locations",
    ),
    ("npc in nowhere", npc_in_nowhere, "dangling_ref", "npc:otto", "npcs"),
    ("item in nowhere", item_held_in_nowhere, "dangling_ref", "item:undercroft-key", "items"),
    ("fight in nowhere", fight_in_nowhere, "dangling_ref", "encounter:ghoul-pack", "encounters"),
    # Бой, которого его локация не показывает (например, бой модели в локации-сиде): по `ref`
    # проблему получает шаг encounters, а не граф локаций.
    (
        "fight its location does not show",
        fight_its_location_does_not_show,
        "dangling_ref",
        "encounter:ghoul-pack",
        "encounters",
    ),
    (
        "clue in nowhere",
        clue_in_nowhere,
        "dangling_ref",
        "revelation:bell-truth/clue:clue-otto",
        "clues",
    ),
    ("start in nowhere", start_in_nowhere, "dangling_ref", "adventure", "locations"),
    ("srd item id", item_with_srd_id, "schema", "item:rope", "items"),
    (
        "engine flag lock",
        lock_on_engine_flag,
        "schema",
        "location:chapel/exit:to-undercroft",
        "locations",
    ),
    (
        "repeated feature",
        repeated_feature_id,
        "schema",
        "location:undercroft/feature:bell-rope",
        "locations",
    ),
    ("unknown module", unknown_rule_module, "schema", "adventure", None),
    ("no motive", npc_without_motive, "npc_without_motive", "npc:otto", "npcs"),
    (
        "no relations",
        important_npc_without_relations,
        "npc_without_relations",
        "npc:otto",
        "npcs",
    ),
    ("silent", silent_location, "empty_read_aloud", "location:chapel", "locations"),
    ("no features", location_without_features, "no_features", "location:undercroft", "locations"),
    ("duration", long_game_for_a_short_wish, "duration_mismatch", "adventure", None),
]


def seed_exit_to_nowhere(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["exits"][0]["to"] = "lost-tower"


def seed_npc_relation_to_nowhere(data: dict[str, Any]) -> None:
    data["npcs"]["otto"]["relations"][0]["npc_id"] = "lost-brother"


def seed_npc_statblock_to_nowhere(data: dict[str, Any]) -> None:
    data["npcs"]["otto"]["statblock"] = "lost-beast"


def seed_item_held_by_a_lost_npc(data: dict[str, Any]) -> None:
    data["items"]["wet-locket"]["holder"] = "npc:lost-npc"


def seed_item_base_to_nowhere(data: dict[str, Any]) -> None:
    data["items"]["wet-locket"]["base_item"] = "lost-sword"


def seed_location_fight_to_nowhere(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["encounter_id"] = "author-fight"


def seed_lock_on_a_lost_key(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["exits"][0]["locked_by"] = "item:lost-key"


def key_behind_its_own_lock(data: dict[str, Any]) -> None:
    data["items"]["undercroft-key"]["holder"] = "location:undercroft"


def place_locked_with_a_key_behind_the_door(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["features"][1]["locked_by"] = "item:undercroft-key"
    key_behind_its_own_lock(data)


def hidden_exit_with_the_id_of_another_exit(data: dict[str, Any]) -> None:
    data["locations"]["undercroft"]["exits"][0]["hidden_dc"] = 15
    data["locations"]["chapel"]["exits"].append(
        {"id": "to-chapel", "to": "undercroft", "direction": "в обход", "description": "Тропа."}
    )


def seed_place_reveals_a_lost_item(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["features"][1]["reveals"] = [{"kind": "item", "id": "lost-relic"}]


def seed_place_reveals_a_lost_clue(data: dict[str, Any]) -> None:
    data["locations"]["chapel"]["features"][1]["reveals"] = [{"kind": "clue", "id": "lost-clue"}]


# (имя, поломка, код, ref настоящего валидатора, id сидов, шаг-владелец или None)
# Сид правит автор: чинится только то, что исправляют другие шаги. Ссылку в пустоту чинит шаг,
# который создаёт цель ссылки; цель `owner_step` берёт из текста «ссылка '<вид>:…'» проблемы,
# поэтому формат этого текста закреплён здесь на настоящем валидаторе.
SEED_ROUTES: list[tuple[str, Mutation, str, str, set[str], str | None]] = [
    (
        "seed exit to nowhere",
        seed_exit_to_nowhere,
        "dangling_ref",
        "location:undercroft/exit:to-chapel",
        {"undercroft"},
        "locations",
    ),
    (
        "seed npc relation to nowhere",
        seed_npc_relation_to_nowhere,
        "dangling_ref",
        "npc:otto",
        {"otto"},
        "npcs",
    ),
    ("seed npc in nowhere", npc_in_nowhere, "dangling_ref", "npc:otto", {"otto"}, "locations"),
    (
        "seed npc statblock to nowhere",
        seed_npc_statblock_to_nowhere,
        "dangling_ref",
        "npc:otto",
        {"otto"},
        None,
    ),
    (
        "seed item held by a lost npc",
        seed_item_held_by_a_lost_npc,
        "dangling_ref",
        "item:wet-locket",
        {"wet-locket"},
        "npcs",
    ),
    (
        "seed item held in nowhere",
        item_held_in_nowhere,
        "dangling_ref",
        "item:undercroft-key",
        {"undercroft-key"},
        "locations",
    ),
    (
        "seed item base to nowhere",
        seed_item_base_to_nowhere,
        "dangling_ref",
        "item:wet-locket",
        {"wet-locket"},
        None,
    ),
    (
        "seed location fight to nowhere",
        seed_location_fight_to_nowhere,
        "dangling_ref",
        "location:undercroft",
        {"undercroft"},
        None,
    ),
    # Замок сида на несуществующий ключ: предметы-ключи создаёт шаг items.
    (
        "seed lock on a lost key",
        seed_lock_on_a_lost_key,
        "dangling_ref",
        "location:chapel/exit:to-undercroft",
        {"chapel"},
        "items",
    ),
    (
        "seed place reveals a lost item",
        seed_place_reveals_a_lost_item,
        "dangling_ref",
        "location:chapel/feature:altar-niche",
        {"chapel"},
        "items",
    ),
    # Улику с заданным id шаг clues не придумает: ссылка остаётся в отчёте.
    (
        "seed place reveals a lost clue",
        seed_place_reveals_a_lost_clue,
        "dangling_ref",
        "location:chapel/feature:altar-niche",
        {"chapel"},
        None,
    ),
    # Ключ замка сида создал шаг items, но положил его недостижимо: сид не меняется, ключ
    # переносит items. Ключ-сид не переносит никто.
    (
        "seed lock and a key behind that lock",
        key_behind_its_own_lock,
        "unreachable_key",
        "location:chapel/exit:to-undercroft",
        {"chapel"},
        "items",
    ),
    (
        "seed lock and a key that left the game",
        key_that_left_the_game,
        "unreachable_key",
        "location:chapel/exit:to-undercroft",
        {"chapel"},
        "items",
    ),
    (
        "seed place and a key behind the door",
        place_locked_with_a_key_behind_the_door,
        "unreachable_key",
        "location:chapel/feature:altar-niche",
        {"chapel"},
        "items",
    ),
    (
        "seed lock and a seed key",
        key_behind_its_own_lock,
        "unreachable_key",
        "location:chapel/exit:to-undercroft",
        {"chapel", "undercroft-key"},
        None,
    ),
    # Скрытый выход сида делит id с выходом другой локации: чужой выход переименует граф. Если все
    # остальные локации тоже сиды, переименовывать нечего.
    (
        "seed hidden exit clashing with a generated exit",
        hidden_exit_with_the_id_of_another_exit,
        "schema",
        "location:undercroft/exit:to-chapel",
        {"undercroft"},
        "locations",
    ),
    (
        "seed hidden exit clashing with a seed exit",
        hidden_exit_with_the_id_of_another_exit,
        "schema",
        "location:undercroft/exit:to-chapel",
        {"undercroft", "chapel"},
        None,
    ),
    (
        "seed repeated feature",
        repeated_feature_id,
        "schema",
        "location:undercroft/feature:bell-rope",
        {"undercroft"},
        None,
    ),
    (
        "seed location without features",
        location_without_features,
        "no_features",
        "location:undercroft",
        {"undercroft"},
        None,
    ),
    (
        "seed location without a way to the finale",
        chapel_without_exits,
        "no_path_to_finale",
        "location:undercroft",
        {"undercroft"},
        None,
    ),
    # Выход к сиду проводит граф: промпт locations просит «проведи к ним выходы».
    (
        "unreachable seed location",
        orphan_location,
        "unreachable_location",
        "location:well",
        {"well"},
        "locations",
    ),
]


def validator_codes() -> set[str]:
    """Коды, которые валидатор способен выдать: их названия стоят в его исходнике."""
    source = Path(validator.__file__).read_text(encoding="utf-8")
    direct = re.findall(r'_(?:error|warning)\(\s*"(\w+)"', source)
    severity_chosen_later = re.findall(r'_issue\(\s*\w+,\s*"(\w+)"', source)
    return set(direct) | set(severity_chosen_later)


@pytest.mark.parametrize(
    ("mutate", "code", "ref", "step"),
    [pytest.param(*route[1:], id=route[0]) for route in ROUTES],
)
async def test_a_real_validator_issue_goes_to_its_owner_step(mutate, code, ref, step):
    broken = copy.deepcopy(await generated_data())
    mutate(broken)

    report = validate(Adventure.model_validate(broken), load_srd())

    found = [issue for issue in report.issues if (issue.code, issue.ref) == (code, ref)]
    assert found, f"валидатор не выдал {code} {ref}: {[(i.code, i.ref) for i in report.issues]}"
    assert owner_step(found[0]) == step


@pytest.mark.parametrize(
    ("mutate", "code", "ref", "seed_ids", "step"),
    [pytest.param(*route[1:], id=route[0]) for route in SEED_ROUTES],
)
async def test_a_real_validator_issue_of_a_seed_goes_to_the_step_that_can_fix_it(
    mutate, code, ref, seed_ids, step
):
    broken = copy.deepcopy(await generated_data())
    mutate(broken)

    report = validate(Adventure.model_validate(broken), load_srd())

    found = [issue for issue in report.issues if (issue.code, issue.ref) == (code, ref)]
    assert found, f"валидатор не выдал {code} {ref}: {[(i.code, i.ref) for i in report.issues]}"
    assert owner_step(found[0], seed_ids) == step


def test_every_code_the_validator_can_give_has_a_routing_case_above():
    # Новый код валидатора без решения, кому его чинить, молча не запускал бы круг исправления
    # (так было бы с `unreachable_key`). Добавляя код, добавьте сюда поломку и шаг-владельца.
    assert validator_codes() == {code for _, _, code, _, _ in ROUTES}


# --- коды выхода и записанная папка ---


def test_warnings_alone_do_not_fail_the_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(DEMO_SCENARIO))
    out = tmp_path / "chapel"

    # Цель в пять часов: оценка игры (74 мин) расходится с ней больше чем на 25%.
    code = main([*CLI_ARGS, *CLI_MODULES, "--hours", "5", "--out", str(out)])

    printed = capsys.readouterr().out
    assert code == 0
    assert "[предупреждение] duration_mismatch adventure" in printed
    assert "Ошибок: 0, предупреждений: 1" in printed
    assert "Приключение записано" in printed
    assert not validate(load_adventure(out), load_srd()).blocking


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # README: --players по умолчанию 6, --level по умолчанию 3, модулей без --modules нет.
        (["--hours", "1.5"], (1.5, 6, 3, [])),
        (
            [
                "--hours",
                "2",
                "--players",
                "2",
                "--level",
                "4",
                "--modules",
                " campfire , bg3_flavor ",
            ],
            (2.0, 2, 4, ["campfire", "bg3_flavor"]),
        ),
    ],
    ids=["по умолчанию", "всё задано"],
)
def test_command_line_arguments_reach_the_written_adventure(
    monkeypatch, tmp_path, arguments, expected
):
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(DEMO_SCENARIO))
    out = tmp_path / "chapel"

    code = main(["--pitch", "Колокол звонит.", *arguments, "--out", str(out)])

    adventure = load_adventure(out)
    assert code == 0
    got = (
        adventure.target_hours,
        adventure.party_size,
        adventure.party_level,
        adventure.rules.modules,
    )
    assert got == expected


def stubborn_scenario(tmp_path: Path) -> Path:
    """Копия демо-сценария, в которой улик всегда две: три круга исправления не помогут."""
    scenario = yaml.safe_load(DEMO_SCENARIO.read_text(encoding="utf-8"))
    # Копии, а не один объект четыре раза: `safe_dump` записал бы его якорем и алиасами,
    # а сценарий читается без них.
    first = scenario["json"]["generator.clues"][0]
    scenario["json"]["generator.clues"] = [copy.deepcopy(first) for _ in range(4)]
    path = tmp_path / "stubborn.yaml"
    path.write_text(yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8")
    return path


def test_the_folder_left_with_blocking_errors_can_be_finished_by_hand(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(stubborn_scenario(tmp_path)))
    out = tmp_path / "chapel"

    code = main([*CLI_ARGS, *CLI_MODULES, "--hours", "1", "--out", str(out)])

    assert code == 2
    assert "исправьте YAML вручную" in capsys.readouterr().out
    before = validate(load_adventure(out), load_srd())
    assert [issue.code for issue in before.issues if issue.severity == "error"] == ["too_few_clues"]
    # Человек добавляет третью улику в файл, как велит сообщение: папка снова проходит проверку.
    revelations = yaml.safe_load((out / "revelations.yaml").read_text(encoding="utf-8"))
    revelations[0]["clues"].append(
        {"id": "clue-otto", "description": "Отто проговаривается.", "where": "npc:otto"}
    )
    (out / "revelations.yaml").write_text(
        yaml.safe_dump(revelations, allow_unicode=True), encoding="utf-8"
    )
    assert not validate(load_adventure(out), load_srd()).blocking


def test_generation_writes_only_into_the_out_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(DEMO_SCENARIO))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("не трогать", encoding="utf-8")
    before = {path for path in tmp_path.rglob("*")}
    out = workspace / "adventures" / "chapel"

    assert main([*CLI_ARGS, *CLI_MODULES, "--hours", "1", "--out", str(out)]) == 0

    created = {path for path in tmp_path.rglob("*")} - before
    assert created
    assert all(
        path == workspace / "adventures" or out in path.parents or path == out for path in created
    )
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "не трогать"


# --- установленная команда, как её запускает владелец ---


def run_script(monkeypatch, out: Path, **env: str) -> subprocess.CompletedProcess[str]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    script = Path(sysconfig.get_path("scripts")) / "dnd-generate"
    return subprocess.run(
        [str(script), *CLI_ARGS, *CLI_MODULES, "--hours", "1", "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_installed_command_generates_with_the_fake_provider_from_the_readme(monkeypatch, tmp_path):
    out = tmp_path / "chapel-whispers"

    result = run_script(monkeypatch, out, LLM_PROVIDER="fake", FAKE_LLM_SCENARIO=str(DEMO_SCENARIO))

    assert result.returncode == 0, result.stderr
    assert "Круг исправления 1 из 3: clues (too_few_clues)" in result.stdout
    assert f"Приключение записано в {out}" in result.stdout
    assert result.stderr == ""
    assert not validate(load_adventure(out), load_srd()).blocking


def test_installed_command_refuses_a_busy_folder_and_a_missing_scenario(monkeypatch, tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "old.txt").write_text("не трогать", encoding="utf-8")

    no_scenario = run_script(monkeypatch, tmp_path / "new", LLM_PROVIDER="fake")
    not_empty = run_script(
        monkeypatch, busy, LLM_PROVIDER="fake", FAKE_LLM_SCENARIO=str(DEMO_SCENARIO)
    )

    assert no_scenario.returncode == 2
    assert "FAKE_LLM_SCENARIO" in no_scenario.stderr
    assert not (tmp_path / "new").exists()
    assert not_empty.returncode == 2
    assert "не пуста" in not_empty.stderr
    assert [path.name for path in busy.iterdir()] == ["old.txt"]
