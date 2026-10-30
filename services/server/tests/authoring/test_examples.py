"""Примеры автора из репозитория: `content/examples/seeds_v2.yaml` и `wishes.yaml` (README
«Генерация приключения»).

Примеры — рабочие шаблоны для владельца: они проходят чтение CLI, а полный прогон `dnd-generate`
по сценарию `generator_v2_demo.yaml` с ними даёт приключение без блокирующих проблем, где всё
закреплённое из примеров лежит таким, как в файле. Файлы читаются по пути из репозитория, а не
копией: тест сломается, если пример разойдётся с форматом заготовок. Живой модели нет: ответы
шагов проигрывает `FakeProvider`, обёрнутый записью запросов.
"""

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from generator_fakes import MODULES, RecordingProvider, v2_answers

from dnd_engine.adventure.loader import load_adventure
from dnd_server.authoring import cli
from dnd_server.authoring.draft import SECTION_KINDS, STEPS
from dnd_server.prompts import PROFANITY_ALLOWED

EXAMPLES = Path(__file__).resolve().parents[4] / "content" / "examples"
SEEDS = EXAMPLES / "seeds_v2.yaml"
WISHES = EXAMPLES / "wishes.yaml"

# Запуск из README («Проверка без живой модели»): заказ сценария демо v2.
V2_ARGS = [
    "--pitch",
    "Маяк на мысу погас.",
    "--hours",
    "2",
    "--players",
    "6",
    "--level",
    "3",
    "--modules",
    ",".join(MODULES),
]


@pytest.fixture
def provider(monkeypatch) -> RecordingProvider:
    """Фабрика CLI отдаёт демо v2 с записью запросов: видно, что и в каком промпте ушло модели."""
    recording = RecordingProvider(v2_answers())
    monkeypatch.setattr(cli, "make_provider", lambda settings, **kwargs: recording)
    return recording


def run_with_examples(out: Path) -> int:
    return cli.main([*V2_ARGS, "--seeds", str(SEEDS), "--wishes", str(WISHES), "--out", str(out)])


# --- чтение CLI ---


def test_the_example_seeds_pass_the_cli_reader_with_every_section_they_show():
    seeds = cli._load_seeds(SEEDS)

    assert seeds.world is not None and seeds.world.regions and seeds.world.rumors
    assert [d.id for d in seeds.deities] == ["selmara"]
    assert [f.id for f in seeds.factions] == ["fishers-guild"]
    assert [n.id for n in seeds.npcs] == ["ilsa"]
    assert [r.id for r in seeds.relations] == ["ilsa-worships-selmara"]


def test_the_example_wishes_pass_the_cli_reader_and_name_only_real_steps():
    wishes = cli._load_wishes(WISHES)

    assert wishes
    assert set(wishes) <= set(STEPS)
    assert all(0 < len(text) <= 2000 for text in wishes.values())


def test_the_example_wishes_skip_the_steps_the_example_seeds_lock_whole():
    # Пожелание к шагу, закреплённому заготовкой целиком, не используется (README): в примере
    # такого пожелания нет, чтобы шаблон не вводил владельца в заблуждение.
    seeds = cli._load_seeds(SEEDS)
    locked_whole = {name for name in ("world", "plot") if getattr(seeds, name) is not None}

    assert locked_whole == {"world"}
    assert not locked_whole & set(cli._load_wishes(WISHES))


# --- полный прогон ---


def test_a_full_run_with_the_examples_writes_an_adventure_without_blocking_problems(
    provider, tmp_path, capsys
):
    out = tmp_path / "crow-lighthouse"

    code = run_with_examples(out)

    printed = capsys.readouterr()
    assert code == 0, printed.err
    assert "Ошибок: 0" in printed.out
    assert load_adventure(out).id == "crow-lighthouse"


def test_every_seed_of_the_examples_is_in_the_result_exactly_as_written(provider, tmp_path):
    out = tmp_path / "crow-lighthouse"
    seeds = cli._load_seeds(SEEDS)

    assert run_with_examples(out) == 0

    adventure = load_adventure(out)
    assert adventure.world == seeds.world
    for section, kind in SECTION_KINDS.items():
        for entity in getattr(seeds, section):
            if section == "relations":
                assert entity in adventure.relations, f"{kind}:{entity.id}"
            else:
                assert getattr(adventure, section)[entity.id] == entity, f"{kind}:{entity.id}"
    written = yaml.safe_load((out / "seeds.yaml").read_text(encoding="utf-8"))
    assert written == ["ilsa", "selmara", "fishers-guild"]  # связи и мир в seeds не входят


def test_the_model_builds_around_the_examples_and_its_own_versions_of_them_are_dropped(
    provider, tmp_path
):
    # В ответах сценария есть свои Ильза, Сельмара и связь с тем же id: побеждает пример.
    out = tmp_path / "crow-lighthouse"

    assert run_with_examples(out) == 0

    adventure = load_adventure(out)
    assert set(adventure.deities) == {"selmara", "ashen-one"}
    assert set(adventure.factions) == {"fishers-guild", "smugglers", "ash-circle"}
    assert set(adventure.npcs) == {"ilsa", "bram", "veran"}
    assert adventure.npcs["ilsa"].name == "Ильза"
    assert adventure.npcs["ilsa"].role == "начальница гавани и старшина артели"


def test_each_example_wish_reaches_only_the_prompt_of_its_own_step(provider, tmp_path):
    wishes = cli._load_wishes(WISHES)

    assert run_with_examples(tmp_path / "out") == 0

    for request in provider.requests:
        step = request.purpose.removeprefix("generator.")
        for wish_step, wish in wishes.items():
            if wish_step == step:
                # Строкой JSON в блоке данных.
                assert json.dumps(wish, ensure_ascii=False) in request.prompt, step
            else:
                assert wish not in request.prompt, (wish_step, step)
    asked = {request.purpose.removeprefix("generator.") for request in provider.requests}
    assert set(wishes) <= asked


def test_a_run_with_the_examples_costs_what_the_readme_says(provider, tmp_path):
    # README «Сколько стоит запуск»: с заготовками примера — 14 запросов, мир задан и не
    # запрашивается; наполнение — по запросу на каждую из 4 локаций.
    assert run_with_examples(tmp_path / "out") == 0

    counts = Counter(provider.purposes())
    assert len(provider.requests) == 14
    assert "generator.world" not in counts
    assert counts["generator.location_contents"] == 4
    assert all(count == 1 for purpose, count in counts.items() if "contents" not in purpose)


def test_every_role_of_a_run_with_the_examples_may_use_profanity_by_default(provider, tmp_path):
    # Флага мата у CLI нет: черновик берёт значение по умолчанию.
    assert run_with_examples(tmp_path / "out") == 0

    assert provider.requests
    assert all(r.system.splitlines()[-1] == PROFANITY_ALLOWED for r in provider.requests)
