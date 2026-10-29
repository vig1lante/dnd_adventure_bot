"""Тесты CLI `dnd-generate` (план DND-1 B5.6, спека §6.3; DND-2 B3.1: заготовки v2 и пожелания).

Провайдер всегда `FakeProvider` по сценарию `generator_demo.yaml`: ключей, сети и живых моделей
нет. Фабрика провайдеров подменяется шпионом там, где нужно доказать, что модель не вызывалась
или что в фабрику дошли `--provider` и `--model`. Проверки модели запросов (`--model` против
`LLM_CHEAP_MODEL`) идут через настоящие клиенты SDK с `MockTransport` вместо сети.
"""

import asyncio
import copy
import json
import subprocess
import sysconfig
import traceback
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest
import yaml

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.adventure.validator import Issue, Report, validate
from dnd_engine.content import MAX_YAML_BYTES, load_srd
from dnd_server.authoring import cli, pipeline
from dnd_server.authoring.cli import format_report, main
from dnd_server.config import set_serving_port
from dnd_server.llm.base import MAX_LOC_PART_CHARS, ProviderError
from dnd_server.llm.fake import FakeProvider, load_scenario

# Конфтест сервера переходит во временную папку, поэтому путь считаем от файла теста.
DEMO_SCENARIO = Path(__file__).resolve().parent / "scenarios" / "generator_demo.yaml"

BASE_ARGS = [
    "--pitch",
    "Колокол в часовне звонит сам по ночам.",
    "--hours",
    "1",
    "--players",
    "6",
    "--level",
    "3",
    "--modules",
    "bg3_flavor,campfire",
]

# Имена лишних ключей из находки X1 security review: запись в буфер обмена (OSC 52) и стирание
# строки возвратом каретки. Ответ модели или файл сидов выбирают их сами.
HOSTILE_KEYS = {
    "osc52-буфер-обмена": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "\x1b[2K\rПриключение записано",
}


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch):
    """Генератор без живой модели запускают в dev, как в README: тестовый мастер `fake` вне dev
    не собирается (спека DND-2 §15). Тест боевого режима ставит APP_ENV=prod сам."""
    monkeypatch.setenv("APP_ENV", "dev")


@pytest.fixture
def demo_env(monkeypatch):
    """Настройки dev-прогона без модели: провайдер fake и сценарий генератора."""
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(DEMO_SCENARIO))


@pytest.fixture
def model_calls(monkeypatch):
    """Шпион на фабрику провайдеров: запоминает вызовы и отдаёт настоящий `FakeProvider`."""
    calls: list[dict[str, Any]] = []

    def spy(settings, **kwargs):
        calls.append(kwargs)
        return FakeProvider(load_scenario(DEMO_SCENARIO))

    monkeypatch.setattr(cli, "make_provider", spy)
    return calls


def generate_into(out: Path, *extra: str) -> int:
    return main([*BASE_ARGS, *extra, "--out", str(out)])


def write_scenario(path: Path, change) -> Path:
    """Копия демо-сценария с правкой: ответы шагов правит `change(json_answers)`."""
    scenario = yaml.safe_load(DEMO_SCENARIO.read_text(encoding="utf-8"))
    change(scenario["json"])
    path.write_text(yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8")
    return path


# --- команда и справка ---


def test_dnd_generate_console_script_points_to_main():
    [script] = entry_points(group="console_scripts", name="dnd-generate")

    assert script.value == "dnd_server.authoring.cli:main"
    assert script.load() is main


def test_installed_dnd_generate_prints_help_with_every_option():
    script = Path(sysconfig.get_path("scripts")) / "dnd-generate"

    result = subprocess.run(
        [str(script), "--help"], capture_output=True, text=True, encoding="utf-8", timeout=60
    )

    assert result.returncode == 0
    for option in (
        "--pitch",
        "--hours",
        "--players",
        "--level",
        "--seeds",
        "--wishes",
        "--modules",
        "--out",
        "--provider",
        "--model",
    ):
        assert option in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--hours", "1", "--out", "x"],
        ["--pitch", "p", "--out", "x"],
        ["--pitch", "p", "--hours", "1"],
        ["--pitch", "p", "--hours", "0", "--out", "x"],
        ["--pitch", "p", "--hours", "-1", "--out", "x"],
        ["--pitch", "p", "--hours", "nan", "--out", "x"],
        ["--pitch", "p", "--hours", "inf", "--out", "x"],
        ["--pitch", "p", "--hours", "1", "--players", "0", "--out", "x"],
        ["--pitch", "p", "--hours", "1", "--players", "7", "--out", "x"],
        ["--pitch", "p", "--hours", "1", "--level", "0", "--out", "x"],
        ["--pitch", "p", "--hours", "1", "--level", "6", "--out", "x"],
        ["--pitch", "p", "--hours", "1", "--modules", "no_such_module", "--out", "x"],
    ],
)
def test_bad_arguments_exit_with_code_2_before_any_model_call(arguments, model_calls):
    with pytest.raises(SystemExit) as stop:
        main(arguments)

    assert stop.value.code == 2
    assert model_calls == []


# --- успешный проход ---


def test_manual_command_writes_a_valid_adventure_and_exits_with_0(demo_env, tmp_path, capsys):
    out = tmp_path / "chapel-whispers"

    code = generate_into(out)

    assert code == 0
    printed = capsys.readouterr().out
    assert "Балл: 100" in printed
    assert "Ошибок: 0" in printed
    assert "круг исправления 1" in printed.lower()  # демо-сценарий проходит один круг
    adventure = load_adventure(out)  # файлы на диске читаются загрузчиком без ошибок
    assert not validate(adventure, load_srd()).blocking
    assert adventure.rules.modules == ["bg3_flavor", "campfire"]
    assert (out / "adventure.yaml").is_file()
    assert sorted(path.name for path in (out / "locations").iterdir()) == [
        "chapel.yaml",
        "undercroft.yaml",
    ]


def test_the_written_folder_is_format_v2_with_the_world_factions_and_the_graph(demo_env, tmp_path):
    # Связи пишет шаг relations, мир и фракции — свои шаги: папка v2 читается обратно с ними (Р8).
    out = tmp_path / "chapel-whispers"

    assert generate_into(out) == 0

    manifest = yaml.safe_load((out / "adventure.yaml").read_text(encoding="utf-8"))
    graph = yaml.safe_load((out / "relations.yaml").read_text(encoding="utf-8"))
    npcs = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in (out / "npcs").iterdir()]
    assert manifest["format_version"] == 2
    assert all("relations" not in npc for npc in npcs)
    expected = {("npc:otto", "npc:greta", "kin")}
    assert {(rel["a"], rel["b"], rel["kind"]) for rel in graph} == expected
    adventure = load_adventure(out)
    assert {(rel.a, rel.b, rel.kind) for rel in adventure.relations} == expected
    assert adventure.world.history and set(adventure.factions) == {"fishers"}
    assert (out / "factions" / "fishers.yaml").is_file()


def test_empty_existing_out_folder_is_accepted(demo_env, tmp_path):
    out = tmp_path / "empty"
    out.mkdir()

    assert generate_into(out) == 0
    assert (out / "adventure.yaml").is_file()


@pytest.mark.parametrize("hidden", [".DS_Store", "._crypt.yaml", ".git"])
def test_out_folder_with_only_hidden_entries_counts_as_empty(demo_env, tmp_path, hidden):
    # Скрытое (Finder, архиваторы, git) загрузчик движка игнорирует, значит, папка свободна.
    out = tmp_path / "finder"
    out.mkdir()
    (out / hidden).write_text("служебное", encoding="utf-8")

    assert generate_into(out) == 0
    assert (out / "adventure.yaml").is_file()
    assert (out / hidden).read_text(encoding="utf-8") == "служебное"


def test_provider_and_model_options_reach_the_factory(model_calls, tmp_path):
    code = generate_into(tmp_path / "out", "--provider", "anthropic_api", "--model", "cheap-x")

    assert code == 0
    assert model_calls[0]["provider"] == "anthropic_api"
    assert model_calls[0]["model"] == "cheap-x"


def test_provider_options_default_to_settings(model_calls, tmp_path):
    generate_into(tmp_path / "out")

    assert model_calls[0]["provider"] is None
    assert model_calls[0]["model"] is None


# Шаги генератора идут с признаком «дешёвая модель»; явный --model побеждает LLM_CHEAP_MODEL.
EXPLICIT_MODEL = "explicit-strong"
CHEAP_MODEL = "env-cheap"
COMPAT_MODEL = "env-compat"


@pytest.fixture
def requested_models(monkeypatch):
    """Модели из тел запросов, ушедших к SDK: HTTP-слой Anthropic и OpenAI заменён заглушкой.

    Заглушка отвечает текстом не по схеме, поэтому генератор повторяет запрос и останавливается
    с отказом провайдера: этого хватает, чтобы увидеть, с какой моделью ушли все его запросы."""
    models: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        models.append(json.loads(request.content)["model"])
        if request.url.path.endswith("/messages"):
            return httpx2.Response(
                200,
                json={
                    "id": "msg_01Test",
                    "type": "message",
                    "role": "assistant",
                    "model": "any",
                    "content": [{"type": "text", "text": "не JSON"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        message = {"role": "assistant", "content": "не JSON", "refusal": None}
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": "any",
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    transport = httpx2.MockTransport(handle)
    real_anthropic, real_openai = anthropic.AsyncAnthropic, openai.AsyncOpenAI
    monkeypatch.setattr(
        "dnd_server.llm.factory.AsyncAnthropic",
        lambda **options: real_anthropic(
            **options, http_client=anthropic.DefaultAsyncHttpxClient(transport=transport)
        ),
    )
    monkeypatch.setattr(
        "dnd_server.llm.factory.AsyncOpenAI",
        lambda **options: real_openai(
            **options, http_client=openai.DefaultAsyncHttpxClient(transport=transport)
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_CHEAP_MODEL", CHEAP_MODEL)
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", COMPAT_MODEL)
    return models


@pytest.mark.parametrize("provider", ["anthropic_api", "openai_compatible"])
def test_explicit_model_wins_over_the_cheap_model_in_every_step(
    provider, requested_models, tmp_path
):
    code = generate_into(tmp_path / "out", "--provider", provider, "--model", EXPLICIT_MODEL)

    assert code == 2  # заглушка отвечает не JSON, генерация останавливается на первом шаге
    assert requested_models
    assert set(requested_models) == {EXPLICIT_MODEL}


@pytest.mark.parametrize(
    ("provider", "configured_model"),
    [("anthropic_api", CHEAP_MODEL), ("openai_compatible", COMPAT_MODEL)],
)
def test_without_model_option_the_steps_use_the_configured_model(
    provider, configured_model, requested_models, tmp_path
):
    code = generate_into(tmp_path / "out", "--provider", provider)

    assert code == 2
    assert requested_models
    assert set(requested_models) == {configured_model}


# --- сиды ---


def test_seeds_option_keeps_seed_ids_and_writes_seeds_yaml(demo_env, tmp_path):
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(
        yaml.safe_dump(
            {
                "npcs": [
                    {
                        "id": "warden",
                        "name": "Смотритель",
                        "role": "сторож",
                        "appearance": "Седой.",
                        "manner": "Молчалив.",
                        "motive": "Сберечь покой мёртвых.",
                        "location_id": "chapel",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    code = generate_into(out, "--seeds", str(seeds))

    assert code == 0
    assert yaml.safe_load((out / "seeds.yaml").read_text(encoding="utf-8")) == ["warden"]
    assert load_adventure(out).npcs["warden"].name == "Смотритель"


def test_seeds_of_any_v2_section_are_locked_and_written_unchanged(demo_env, tmp_path):
    # Заготовки v2: мир (закрепляет шаг world целиком), бог и связь (каждая сущность закреплена).
    deity = {
        "id": "sea-mother",
        "name": "Морская мать",
        "title": "Хозяйка глубин",
        "domains": ["море"],
        "symbol": "Ракушка.",
        "dogma": "Море берёт своё.",
        "appearance": "Женщина из пены.",
        "attitude_to_mortals": "Равнодушна.",
    }
    relation = {
        "id": "otto-worships-sea-mother",
        "a": "npc:otto",
        "b": "deity:sea-mother",
        "kind": "worships",
        "mutual": False,
    }
    world = {"history": "Коса стоит на костях утопленников."}
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(
        yaml.safe_dump(
            {"world": world, "deities": [deity], "relations": [relation]}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    code = generate_into(out, "--seeds", str(seeds))

    assert code == 0
    adventure = load_adventure(out)
    assert adventure.world.history == world["history"]
    assert adventure.deities["sea-mother"].model_dump(mode="json", exclude_defaults=True) == deity
    assert relation["id"] in {rel.id for rel in adventure.relations}
    assert yaml.safe_load((out / "seeds.yaml").read_text(encoding="utf-8")) == ["sea-mother"]


V2_SCENARIO = DEMO_SCENARIO.with_name("generator_v2_demo.yaml")


def seed_npc_with_relations(npc_id: str, location_id: str, *relations: dict[str, str]) -> dict:
    """NPC заготовки в форме DND-1: связи лежат внутри NPC (`npcs[].relations`)."""
    return {
        "id": npc_id,
        "name": "Смотритель",
        "role": "сторож",
        "appearance": "Седой.",
        "manner": "Молчалив.",
        "motive": "Сберечь покой мёртвых.",
        "location_id": location_id,
        "relations": list(relations),
    }


def write_seeds(tmp_path: Path, seeds: dict[str, Any]) -> Path:
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump(seeds, allow_unicode=True), encoding="utf-8")
    return path


def test_v1_relations_of_a_seed_npc_reach_the_graph_and_stay_locked(demo_env, tmp_path):
    # Р8 и README B2: связи сида-NPC в старой форме переходят в граф по правилам движка.
    warden = seed_npc_with_relations(
        "warden", "chapel", {"npc_id": "otto", "kind": "sibling", "note": "Старший брат."}
    )
    out = tmp_path / "out"

    code = generate_into(out, "--seeds", str(write_seeds(tmp_path, {"npcs": [warden]})))

    assert code == 0
    adventure = load_adventure(out)
    moved = next(rel for rel in adventure.relations if rel.id == "otto-kin-warden")
    assert (moved.a, moved.b, moved.kind, moved.mutual) == ("npc:otto", "npc:warden", "kin", False)
    assert moved.note == "брат или сестра; Старший брат."
    npc_file = yaml.safe_load((out / "npcs" / "warden.yaml").read_text(encoding="utf-8"))
    assert "relations" not in npc_file
    assert yaml.safe_load((out / "seeds.yaml").read_text(encoding="utf-8")) == ["warden"]


def test_a_v1_relation_of_a_seed_npc_wins_over_the_same_relation_of_the_model(
    monkeypatch, tmp_path
):
    # Воспроизведение находки B3.2: Ильза из заготовки соперничает с Брамом, которого создаст
    # модель; модель присылает свою связь с тем же id — побеждает закреплённая связь заготовки.
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(V2_SCENARIO))
    ilsa = seed_npc_with_relations("ilsa", "harbor", {"npc_id": "bram", "kind": "rival"})
    out = tmp_path / "out"

    code = generate_into(
        out, "--hours", "2", "--seeds", str(write_seeds(tmp_path, {"npcs": [ilsa]}))
    )

    assert code == 0
    [rival] = [rel for rel in load_adventure(out).relations if rel.id == "bram-rival-ilsa"]
    assert (rival.note, rival.mutual) == ("", False)


@pytest.mark.parametrize("npc_id", ["Брам!", "../bram"])
def test_a_v1_relation_to_an_id_that_cannot_be_an_npc_exits_with_2_before_any_model_call(
    npc_id, model_calls, tmp_path, capsys
):
    warden = seed_npc_with_relations("warden", "chapel", {"npc_id": npc_id, "kind": "rival"})

    code = generate_into(
        tmp_path / "out", "--seeds", str(write_seeds(tmp_path, {"npcs": [warden]}))
    )

    printed = capsys.readouterr()
    assert code == 2
    assert model_calls == []
    lines = printed.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("Ошибка: файл сидов ")
    assert npc_id not in printed.err
    assert not (tmp_path / "out").exists()


def test_a_custom_statblock_whose_id_is_not_a_file_name_exits_with_2_before_any_model_call(
    model_calls, tmp_path, capsys
):
    monster = {
        "id": "Ash Priest",
        "name": "Жрец",
        "name_en": "Priest",
        "cr": 1,
        "xp": 200,
        "ac": 12,
        "hp": 20,
        "hp_dice": "4d8",
        "speed": 30,
        "scores": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
        "actions": [{"id": "staff", "name": "Посох", "kind": "other"}],
    }

    code = generate_into(
        tmp_path / "out", "--seeds", str(write_seeds(tmp_path, {"monsters": [monster]}))
    )

    assert code == 2
    assert model_calls == []
    assert "Ash Priest" not in capsys.readouterr().err


def test_a_rejected_assembly_exits_with_2_without_a_traceback_or_input_values(
    demo_env, monkeypatch, tmp_path, capsys
):
    # Последний барьер: если сборка приключения всё же отвергнет данные, команда отвечает строкой
    # «Ошибка: …» и кодом 2, без трассировки и без значений из данных.
    def rejecting(draft):
        return Adventure.model_validate({"id": "sk-LEAK-assembly value"})

    monkeypatch.setattr(pipeline, "to_adventure", rejecting)
    out = tmp_path / "out"

    code = generate_into(out)

    printed = capsys.readouterr()
    assert code == 2
    assert printed.err.startswith("Ошибка: ")
    assert "Traceback" not in printed.err
    assert "sk-LEAK" not in printed.out + printed.err
    assert not out.exists()


@pytest.mark.parametrize(
    "content",
    [
        "dragons: []",  # неизвестный раздел
        "npcs: [{id: warden}]",  # неполная сущность
        "npcs: что-то не то",
        "[1, 2",  # битый YAML
        "- 1",  # корень не словарь
    ],
)
def test_broken_seeds_exit_with_2_before_any_model_call(content, model_calls, tmp_path, capsys):
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(content, encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    assert code == 2
    assert model_calls == []
    assert "seeds.yaml" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "content",
    [
        "[sk-LEAK-seeds",  # битый YAML, секрет у ошибки
        "!sk-LEAK-seeds 1",  # секрет в имени тега
        "&sk-LEAK-seeds [*sk-LEAK-other]",  # секрет в имени якоря и ссылки
        "npcs: sk-LEAK-seeds",  # секрет вместо списка
    ],
)
def test_seeds_errors_never_quote_the_seeds_file(content, model_calls, tmp_path, capsys):
    # --seeds мог по ошибке указать на файл с секретами: его содержимое не попадает в сообщение.
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(content, encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    error = capsys.readouterr().err
    assert code == 2
    assert "seeds.yaml" in error
    assert "sk-LEAK" not in error


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
def test_seeds_with_an_extra_key_of_terminal_escapes_print_it_escaped(
    key, model_calls, tmp_path, capsys
):
    # Файл сидов может прислать чужой: имя лишнего раздела не должно управлять терминалом.
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(yaml.safe_dump({key: []}, allow_unicode=True), encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    printed = capsys.readouterr()
    assert code == 2
    assert model_calls == []
    assert printed.out == ""
    assert printed.err.strip().isprintable()  # одна строка без ESC, BEL и возврата каретки
    assert "\\x1b" in printed.err


def test_seeds_error_clips_a_long_field_name(model_calls, tmp_path, capsys):
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(yaml.safe_dump({"к" * 5000: []}, allow_unicode=True), encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    error = capsys.readouterr().err
    assert code == 2
    assert "к" * MAX_LOC_PART_CHARS not in error
    assert "к" * (MAX_LOC_PART_CHARS - 1) + "…" in error


# Конструкторы значений PyYAML на чужом вводе бросают не только `YAMLError`: `!!int ""` даёт
# `IndexError`, `!!bool x` — `KeyError`, `!!timestamp x` — `AttributeError`, длинное число и
# несуществующая дата — `ValueError`; в тексте последних значение из файла (секрет в маркере).
# Файл портит владелец или чужая выгрузка, а команда обязана ответить строкой «Ошибка: …» и кодом 2.
UNBUILDABLE_VALUES = {
    "число-длиннее-4300-цифр": "9" * 4301,
    "несуществующая-дата": "2023-02-30",
    "несуществующее-время": "2023-02-28 25:61:00",
    "int-пустая-строка": '!!int ""',
    "float-пустая-строка": '!!float ""',
    "bool-не-литерал": "!!bool sk-LEAK-bool",
    "timestamp-не-дата": "!!timestamp sk-LEAK-time",
    "int-не-число": "!!int sk-LEAK-int",
    "float-не-число": "!!float sk-LEAK-float",
    "смещение-пояса-вне-суток": "2001-12-14t21:59:43.10+99:00",
    "глубокая-вложенность": "[" * 5000 + "]" * 5000,
}


def write_seeds_with(tmp_path: Path, value: str) -> Path:
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(f"npcs: {value}\n", encoding="utf-8")
    return seeds


@pytest.mark.parametrize("value", UNBUILDABLE_VALUES.values(), ids=UNBUILDABLE_VALUES.keys())
def test_seeds_with_a_value_yaml_cannot_build_exit_with_2_and_one_error_line(
    value, model_calls, tmp_path, capsys
):
    seeds = write_seeds_with(tmp_path, value)

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    printed = capsys.readouterr()
    lines = printed.err.splitlines()
    assert code == 2
    assert model_calls == []
    assert printed.out == ""
    assert len(lines) == 1 and lines[0].startswith("Ошибка: файл сидов ")
    assert "seeds.yaml" in lines[0] and "Traceback" not in printed.err
    assert "sk-leak" not in printed.err.lower()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("value", UNBUILDABLE_VALUES.values(), ids=UNBUILDABLE_VALUES.keys())
def test_seeds_error_keeps_neither_the_cause_nor_the_context(value, tmp_path):
    # Исходное исключение парсера несёт значение из файла: в цепочке ошибки его быть не должно.
    seeds = write_seeds_with(tmp_path, value)

    with pytest.raises(cli.CliError) as error:
        cli._load_seeds(seeds)

    story = "".join(traceback.format_exception(error.value))
    assert "sk-leak" not in story.lower()
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_missing_seeds_file_exits_with_2_before_any_model_call(model_calls, tmp_path, capsys):
    code = generate_into(tmp_path / "out", "--seeds", str(tmp_path / "nope.yaml"))

    assert code == 2
    assert model_calls == []
    assert "nope.yaml" in capsys.readouterr().err


# Файл сидов читается теми же частями движка, что и папка приключения: якорь и алиас раздули бы
# малый файл в миллионы моделей, а сиды целиком уходят в промпт каждого шага генератора (каждый
# платный запрос). Огромный файл и файл особого вида отвергаются до чтения. Во всех случаях команда
# отвечает одной строкой «Ошибка: …» с причиной и кодом 2 до первого вызова модели.

# Один NPC с общим списком по алиасу, повторённый тысячи раз: так выглядит «бомба алиасов» в сидах.
SEEDS_WITH_ALIAS_BOMB = (
    "npcs:\n"
    + "  - {id: n0, name: Н, role: r, knows: &lore [a, b, c], location_id: x}\n"
    + "".join(
        f"  - {{id: n{i}, name: Н, role: r, knows: *lore, location_id: x}}\n"
        for i in range(1, 3000)
    )
)
SEEDS_REFUSED = {
    "алиас": ("items: &x []\nnpcs: *x\n", "якоря и алиасы YAML не поддерживаются"),
    "якорь без алиаса": ("items: &x []\n", "якоря и алиасы YAML не поддерживаются"),
    "слияние по алиасу": (
        "npcs: [{id: a, <<: &base {name: Н}}]\n",
        "якоря и алиасы YAML не поддерживаются",
    ),
    "бомба алиасов": (SEEDS_WITH_ALIAS_BOMB, "якоря и алиасы YAML не поддерживаются"),
    "больше предела": ("#" * (MAX_YAML_BYTES + 1), f"файл больше {MAX_YAML_BYTES // 1024} КБ"),
}


@pytest.mark.parametrize(("content", "reason"), SEEDS_REFUSED.values(), ids=SEEDS_REFUSED.keys())
def test_seeds_the_engine_reader_refuses_exit_with_2_and_name_the_reason(
    content, reason, model_calls, tmp_path, capsys
):
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(content, encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    printed = capsys.readouterr()
    lines = printed.err.splitlines()
    assert code == 2
    assert model_calls == []
    assert printed.out == ""
    assert len(lines) == 1 and lines[0].startswith("Ошибка: файл сидов ")
    assert "seeds.yaml" in lines[0] and reason in lines[0]
    assert not (tmp_path / "out").exists()


def test_seeds_error_for_an_alias_keeps_neither_the_cause_nor_the_context(tmp_path):
    # Исходное исключение разбора несёт позицию и разметку файла: цепочки у ошибки быть не должно.
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text("items: &sk-LEAK-anchor []\nnpcs: *sk-LEAK-anchor\n", encoding="utf-8")

    with pytest.raises(cli.CliError) as error:
        cli._load_seeds(seeds)

    story = "".join(traceback.format_exception(error.value))
    assert "sk-leak" not in story.lower()
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="нет /dev/null")
def test_seeds_that_are_a_device_exit_with_2_before_any_model_call(model_calls, tmp_path, capsys):
    # Пустое чтение устройства дало бы «пустые сиды» и молча запустило генератор без них.
    seeds = tmp_path / "seeds.yaml"
    seeds.symlink_to("/dev/null")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    error = capsys.readouterr().err
    assert code == 2
    assert model_calls == []
    assert "seeds.yaml" in error and "не обычный файл" in error
    assert "[Errno 0]" not in error  # 0 — не код ОС, а наша проверка вида файла


def test_seeds_that_are_a_folder_exit_with_2_naming_the_reason_without_errno(
    model_calls, tmp_path, capsys
):
    seeds = tmp_path / "seeds.yaml"
    seeds.mkdir()

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    error = capsys.readouterr().err
    assert code == 2
    assert model_calls == []
    assert "seeds.yaml" in error and "не обычный файл" in error
    assert "[Errno 0]" not in error


# --- пожелания по шагам (--wishes) ---


@pytest.fixture
def asked(monkeypatch):
    """Запросы к модели: фабрика отдаёт `FakeProvider` демо, обёрнутый записью запросов."""
    requests: list[Any] = []

    class Recording:
        name = "recording"

        def __init__(self) -> None:
            self._fake = FakeProvider(load_scenario(DEMO_SCENARIO))

        async def complete_json(self, req, schema):
            requests.append(req)
            return await self._fake.complete_json(req, schema)

    monkeypatch.setattr(cli, "make_provider", lambda settings, **kwargs: Recording())
    return requests


def write_wishes(tmp_path: Path, content: str) -> Path:
    wishes = tmp_path / "wishes.yaml"
    wishes.write_text(content, encoding="utf-8")
    return wishes


def test_wishes_reach_the_prompt_of_their_own_step_in_a_data_block(asked, tmp_path):
    wishes = write_wishes(tmp_path, "world: Пусть будут пираты\nnpcs: Говорящий кот\n")

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    assert code == 0
    prompts = {request.purpose: request.prompt for request in asked}
    assert '<данные>\n"Пусть будут пираты"\n</данные>' in prompts["generator.world"]
    assert '<данные>\n"Говорящий кот"\n</данные>' in prompts["generator.npcs"]
    assert "Говорящий кот" not in prompts["generator.world"]


@pytest.mark.parametrize(
    "content",
    [
        "dragons: Больше драконов",  # неизвестный шаг
        "world: [1, 2]",  # пожелание не строка
        "world: ''",  # пустое пожелание
        "[1, 2",  # битый YAML
        "- world",  # корень не словарь
    ],
)
def test_broken_wishes_exit_with_2_before_any_model_call(content, model_calls, tmp_path, capsys):
    wishes = write_wishes(tmp_path, content)

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    printed = capsys.readouterr()
    assert code == 2
    assert model_calls == []
    assert printed.out == ""
    lines = printed.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("Ошибка: файл пожеланий ")
    assert "wishes.yaml" in lines[0]
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "content",
    [
        "[sk-LEAK-wishes",  # битый YAML, секрет у ошибки
        "world: !sk-LEAK-wishes 1",  # секрет в имени тега
        "world: [sk-LEAK-wishes]",  # секрет вместо строки
        "world: &sk-LEAK-wishes x",  # секрет в имени якоря
    ],
)
def test_wishes_errors_never_quote_the_wishes_file(content, model_calls, tmp_path, capsys):
    wishes = write_wishes(tmp_path, content)

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    error = capsys.readouterr().err
    assert code == 2
    assert "wishes.yaml" in error
    assert "sk-LEAK" not in error


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
def test_wishes_with_an_unknown_step_of_terminal_escapes_print_it_escaped(
    key, model_calls, tmp_path, capsys
):
    wishes = tmp_path / "wishes.yaml"
    wishes.write_text(yaml.safe_dump({key: "пожелание"}, allow_unicode=True), encoding="utf-8")

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    printed = capsys.readouterr()
    assert code == 2
    assert model_calls == []
    assert printed.err.strip().isprintable()
    assert "\\x1b" in printed.err


def test_a_wish_of_2000_characters_reaches_its_prompt_whole(asked, tmp_path):
    wish = "я" * 2000
    wishes = write_wishes(tmp_path, f"npcs: {wish}\n")

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    assert code == 0
    [prompt] = [request.prompt for request in asked if request.purpose == "generator.npcs"]
    assert f'<данные>\n"{wish}"\n</данные>' in prompt


def test_a_wish_over_2000_characters_exits_with_2_without_quoting_it(model_calls, tmp_path, capsys):
    wish = ("sk-LEAK-wish " * 200)[:2001]
    wishes = write_wishes(tmp_path, yaml.safe_dump({"npcs": wish}, allow_unicode=True))

    code = generate_into(tmp_path / "out", "--wishes", str(wishes))

    printed = capsys.readouterr()
    assert code == 2
    assert model_calls == []
    lines = printed.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("Ошибка: файл пожеланий ")
    assert "npcs" in lines[0]
    assert "sk-LEAK" not in printed.out + printed.err
    assert not (tmp_path / "out").exists()


def test_missing_wishes_file_exits_with_2_before_any_model_call(model_calls, tmp_path, capsys):
    code = generate_into(tmp_path / "out", "--wishes", str(tmp_path / "nope.yaml"))

    assert code == 2
    assert model_calls == []
    assert "nope.yaml" in capsys.readouterr().err


def test_wishes_error_keeps_neither_the_cause_nor_the_context(tmp_path):
    wishes = write_wishes(tmp_path, "world: [sk-LEAK-wishes]")

    with pytest.raises(cli.CliError) as error:
        cli._load_wishes(wishes)

    story = "".join(traceback.format_exception(error.value))
    assert "sk-leak" not in story.lower()
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


# --- папка вывода ---


def test_non_empty_out_exits_with_2_before_any_model_call(model_calls, tmp_path, capsys):
    out = tmp_path / "busy"
    out.mkdir()
    (out / "old.txt").write_text("не трогать", encoding="utf-8")

    code = generate_into(out)

    assert code == 2
    assert model_calls == []
    assert "не пуста" in capsys.readouterr().err
    assert [path.name for path in out.iterdir()] == ["old.txt"]


def test_hidden_entry_does_not_hide_other_files_in_out_folder(model_calls, tmp_path, capsys):
    out = tmp_path / "busy"
    out.mkdir()
    (out / ".DS_Store").write_text("служебное", encoding="utf-8")
    (out / "old.txt").write_text("не трогать", encoding="utf-8")

    code = generate_into(out)

    assert code == 2
    assert model_calls == []
    assert "не пуста" in capsys.readouterr().err


def test_out_that_is_a_file_exits_with_2_before_any_model_call(model_calls, tmp_path, capsys):
    out = tmp_path / "file"
    out.write_text("файл", encoding="utf-8")

    code = generate_into(out)

    assert code == 2
    assert model_calls == []
    assert "file" in capsys.readouterr().err


# --- ошибки настройки и провайдера ---


def test_missing_scenario_is_a_config_error_with_exit_code_2(tmp_path, capsys):
    out = tmp_path / "out"

    code = generate_into(out)

    assert code == 2
    assert "FAKE_LLM_SCENARIO" in capsys.readouterr().err
    assert not out.exists()


def test_the_test_master_outside_dev_exits_with_2_without_naming_its_scenario(
    demo_env, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("APP_ENV", "prod")

    code = generate_into(tmp_path / "out")

    err = capsys.readouterr().err
    assert code == 2
    assert "тестовый мастер fake доступен только в dev-режиме" in err
    assert str(DEMO_SCENARIO) not in err and not (tmp_path / "out").exists()


def test_claude_code_cli_outside_dev_exits_with_2_without_starting_a_process(
    monkeypatch, tmp_path, capsys
):
    def refuse(*args, **kwargs):
        raise AssertionError("процесс claude запущен")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setenv("APP_ENV", "prod")

    code = generate_into(tmp_path / "out", "--provider", "claude_code_cli")

    assert code == 2
    assert "только в dev-режиме" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_claude_code_cli_gets_the_bridge_arguments_the_factory_requires(model_calls, tmp_path):
    # Адрес один на весь сервер (`dm.provider.bridge_url`): у генератора своего литерала нет, он
    # идёт за портом процесса. Моста генератор не поднимает: он зовёт только complete_json,
    # а адрес нужен фабрике.
    set_serving_port(9123)  # после теста порт вернёт автофикстура `serving_port_reset`

    code = generate_into(tmp_path / "out", "--provider", "claude_code_cli")

    assert code == 0
    assert model_calls[0]["provider"] == "claude_code_cli"
    assert model_calls[0]["turn_tokens"] is not None
    assert model_calls[0]["internal_url"] == "http://127.0.0.1:9123"


def test_provider_error_exits_with_2_and_prints_the_reason(tmp_path, monkeypatch, capsys):
    scenario = write_scenario(tmp_path / "broken.yaml", lambda a: a.pop("generator.world"))
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(scenario))
    out = tmp_path / "out"

    code = generate_into(out)

    assert code == 2
    assert "generator.world" in capsys.readouterr().err
    assert not out.exists()


def test_provider_error_text_does_not_leak_secrets(monkeypatch, tmp_path, capsys):
    secret = "sk-ant-api03-cli-secret"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    class Failing:
        name = "failing"

        async def complete_json(self, req, schema):
            raise ProviderError("сервер отказал")

    monkeypatch.setattr(cli, "make_provider", lambda settings, **kwargs: Failing())

    code = generate_into(tmp_path / "out", "--provider", "anthropic_api")

    printed = capsys.readouterr()
    assert code == 2
    assert "сервер отказал" in printed.err
    assert secret not in printed.out + printed.err


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
def test_extra_key_of_a_model_answer_with_terminal_escapes_is_printed_escaped(
    key, tmp_path, monkeypatch, capsys
):
    scenario = write_scenario(
        tmp_path / "hostile.yaml", lambda a: a["generator.concept"].update({key: 1})
    )
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(scenario))
    out = tmp_path / "out"

    code = generate_into(out)

    printed = capsys.readouterr()
    assert code == 2
    assert "generator.concept" in printed.err
    assert printed.err.strip().isprintable()  # одна строка без ESC, BEL и возврата каретки
    assert "\\x1b" in printed.err
    assert not out.exists()


# Любой текст `ProviderError` до стока проходит через общий барьер: в него попадают куски ответа
# модели (отказ, вывод и stderr CLI), которые адаптеры вставляют в сообщение как есть.
HOSTILE_PROVIDER_ERRORS = {
    "osc52": "claude вернул ошибку (error): \x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "модель отказалась отвечать: \x1b[2K\rПриключение записано",
    "csi-одним-байтом": "не удалось разобрать ответ claude: \x9b2J\x9d0;заголовок\x07",
    "смена-заголовка": "claude завершился с кодом 1: \x1b]0;подмена\x07",
}


@pytest.mark.parametrize(
    "message", HOSTILE_PROVIDER_ERRORS.values(), ids=HOSTILE_PROVIDER_ERRORS.keys()
)
def test_provider_error_text_never_carries_terminal_controls_to_stderr(
    message, monkeypatch, tmp_path, capsys
):
    class Failing:
        name = "failing"

        async def complete_json(self, req, schema):
            raise ProviderError(message)

    monkeypatch.setattr(cli, "make_provider", lambda settings, **kwargs: Failing())

    code = generate_into(tmp_path / "out")

    printed = capsys.readouterr()
    assert code == 2
    assert printed.err.startswith("Ошибка: провайдер модели: ")
    assert printed.err.strip().isprintable()
    assert "\\x" in printed.err  # от текста осталась видимая запись


def test_error_line_keeps_the_line_breaks_of_a_multi_line_message(monkeypatch, tmp_path, capsys):
    # Хвост stderr CLI многострочный: барьер экранирует управляющие символы, но не строки.
    class Failing:
        name = "failing"

        async def complete_json(self, req, schema):
            raise ProviderError("claude завершился с кодом 1: первая строка\nвторая строка")

    monkeypatch.setattr(cli, "make_provider", lambda settings, **kwargs: Failing())

    generate_into(tmp_path / "out")

    assert "первая строка\nвторая строка" in capsys.readouterr().err


def test_texts_from_the_model_in_a_successful_run_never_carry_terminal_controls(
    tmp_path, monkeypatch, capsys
):
    # Прогресс и отчёт валидатора печатают только id, коды и числа: свободный текст ответа
    # модели (заголовок, рассказ) в терминал не идёт, а попадает лишь в файлы приключения.
    def hostile_texts(answers):
        answers["generator.concept"]["title"] = "Шёпот\x1b]52;c;ZWNobyBwd25lZA==\x07"
        answers["generator.concept"]["pitch"] = "Завязка\x1b[2K\rПриключение записано"
        for contents in answers["generator.location_contents"]:
            contents["read_aloud"] = "Ветер\x1b[31mсвистит"

    scenario = write_scenario(tmp_path / "hostile-texts.yaml", hostile_texts)
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(scenario))

    code = generate_into(tmp_path / "out")

    printed = capsys.readouterr()
    assert code == 0
    for stream in (printed.out, printed.err):
        assert not {"\x1b", "\x07", "\r"} & set(stream)


def test_texts_from_the_seeds_file_in_a_successful_run_never_carry_terminal_controls(
    demo_env, tmp_path, capsys
):
    # Свободный текст сидов, как и текст ответа модели, попадает только в файлы приключения.
    seeds = tmp_path / "seeds.yaml"
    npc = {
        "id": "warden",
        "name": "Смотритель\x1b]0;подмена\x07",
        "role": "сторож",
        "appearance": "Седой.\x1b[2K\rПриключение записано",
        "manner": "Молчалив.",
        "motive": "Сберечь покой мёртвых.",
        "location_id": "chapel",
    }
    seeds.write_text(yaml.safe_dump({"npcs": [npc]}, allow_unicode=True), encoding="utf-8")

    code = generate_into(tmp_path / "out", "--seeds", str(seeds))

    printed = capsys.readouterr()
    assert code == 0
    for stream in (printed.out, printed.err):
        assert not {"\x1b", "\x07", "\r"} & set(stream)


# --- блокирующие ошибки ---


def test_three_failed_rounds_exit_with_2_print_the_report_and_keep_the_folder(
    tmp_path, monkeypatch, capsys
):
    def always_two_clues(answers):
        bad = answers["generator.clues"][0]
        # Копии, а не один объект четыре раза: `safe_dump` записал бы его якорем и алиасами,
        # а сценарий читается без них.
        answers["generator.clues"] = [copy.deepcopy(bad) for _ in range(4)]

    scenario = write_scenario(tmp_path / "stubborn.yaml", always_two_clues)
    monkeypatch.setenv("FAKE_LLM_SCENARIO", str(scenario))
    out = tmp_path / "out"

    code = generate_into(out)

    printed = capsys.readouterr().out
    assert code == 2
    assert "круг исправления 3" in printed.lower()
    assert "круг исправления 4" not in printed.lower()
    assert "[ошибка] too_few_clues revelation:bell-truth" in printed
    assert "Ошибок: 1" in printed
    # Папка записана, чтобы доделать приключение вручную; создавать игру на нём нельзя.
    assert validate(load_adventure(out), load_srd()).blocking


# --- отчёт валидатора ---


def test_format_report_lists_issues_by_severity_with_score_and_time():
    report = Report(
        issues=[
            Issue(code="too_few_clues", severity="error", ref="revelation:r", message="улик 2"),
            Issue(code="no_features", severity="warning", ref="location:a", message="пусто"),
        ],
        score=87,
        estimated_minutes=74,
    )

    text = format_report(report, target_hours=1)

    assert "[ошибка] too_few_clues revelation:r: улик 2" in text
    assert "[предупреждение] no_features location:a: пусто" in text
    assert "Балл: 87" in text
    assert "Оценка времени игры: 74 мин (цель: 1 ч)" in text
    assert "Ошибок: 1, предупреждений: 1" in text


def test_format_report_without_issues_says_so():
    text = format_report(Report(issues=[], score=100, estimated_minutes=60), target_hours=1)

    assert "Замечаний нет" in text
    assert "Ошибок: 0, предупреждений: 0" in text
