"""Базовые типы слоя моделей и `FakeProvider` (план B5.1, спека §7, §13).

Живых моделей и сети здесь нет: `FakeProvider` проигрывает сценарий, а исполнитель
инструментов — заглушка, которая записывает вызовы.
"""

import json
import os
import signal
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from dnd_engine.content import MAX_YAML_BYTES
from dnd_engine.modules import build_ruleset
from dnd_engine.state import RulesRef
from dnd_engine.tools import build_registry
from dnd_server.llm.base import (
    MAX_PROBLEMS_CHARS,
    MAX_TRACE_ARGS_CHARS,
    MAX_TRACE_NAME_CHARS,
    MAX_TRACE_RESULT_CHARS,
    JsonRequest,
    LLMProvider,
    ProviderError,
    ProviderRefusal,
    ToolLoopExceeded,
    TurnRequest,
    Usage,
    make_trace_call,
)
from dnd_server.llm.fake import (
    FakeProvider,
    Scenario,
    UnreadableYaml,
    load_scenario,
    parse_yaml,
    read_yaml,
)

# Конфтест сервера переходит во временную папку, поэтому путь считаем от файла теста.
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


class Weaving(BaseModel):
    hooks: list[str]


class StrictWeaving(BaseModel):
    """Схема, которая отвергает лишние поля: имя лишнего ключа попадает в текст ошибки."""

    model_config = ConfigDict(extra="forbid")

    hooks: list[str]


# Имена лишних ключей из находки X1 security review: запись в буфер обмена (OSC 52) и стирание
# строки возвратом каретки. Ключ в сценарии выбирает его автор или записанный ответ модели.
HOSTILE_KEYS = {
    "osc52-буфер-обмена": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "стирание-строки": "\x1b[2K\rПриключение записано",
}


class StubTools:
    """Исполнитель инструментов: записывает вызовы и каждый раз отдаёт один и тот же ответ."""

    def __init__(self, result: str = '{"ok": true}', is_error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answer = (result, is_error)

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        return self._answer


def tool(name: str, **args: Any) -> dict[str, Any]:
    return {"tool": name, "args": args}


def say(text: str) -> dict[str, Any]:
    return {"text": text}


def make_scenario(*turns: dict[str, Any], **json_answers: Any) -> Scenario:
    return Scenario.model_validate({"turns": list(turns), "json": json_answers})


def make_request(**overrides: Any) -> TurnRequest:
    return TurnRequest(
        system="система", adventure_brief="сводка", context="контекст", tools=(), **overrides
    )


def make_json_request(purpose: str = "weaving") -> JsonRequest:
    return JsonRequest(system="система", prompt="запрос", purpose=purpose)


# --- ход мастера ---


async def test_turn_runs_tools_in_order_and_returns_text_and_trace():
    scenario = make_scenario(
        {
            "steps": [
                tool("request_check", dc=15),
                tool("chronicle", entry="запись"),
                say("Рассказ."),
            ]
        }
    )
    tools = StubTools(result='{"total": 17}')

    result = await FakeProvider(scenario).run_turn(make_request(), tools)

    assert tools.calls == [("request_check", {"dc": 15}), ("chronicle", {"entry": "запись"})]
    assert result.text == "Рассказ."
    assert [call.name for call in result.trace] == ["request_check", "chronicle"]
    assert result.trace[0].args == {"dc": 15}
    assert result.trace[0].result == '{"total": 17}'
    assert result.stop_reason == "end_turn"
    assert result.usage == Usage()


async def test_tool_error_is_traced_and_the_turn_continues():
    scenario = make_scenario({"steps": [tool("request_check"), say("Рассказ.")]})

    result = await FakeProvider(scenario).run_turn(make_request(), StubTools("нет такого", True))

    assert result.trace[0].is_error is True
    assert result.text == "Рассказ."


async def test_turn_without_text_step_returns_empty_text():
    # Так тесты оркестратора получают «модель не дала итогового текста».
    scenario = make_scenario({"steps": [tool("chronicle", entry="запись")]})

    result = await FakeProvider(scenario).run_turn(make_request(), StubTools())

    assert result.text == ""


async def test_exhausted_scenario_raises_provider_error():
    provider = FakeProvider(make_scenario({"steps": [say("Раз.")]}))
    await provider.run_turn(make_request(), StubTools())

    with pytest.raises(ProviderError, match="сценарий исчерпан"):
        await provider.run_turn(make_request(), StubTools())


async def test_scenario_without_turns_is_exhausted_at_once():
    with pytest.raises(ProviderError, match="сценарий исчерпан"):
        await FakeProvider(make_scenario()).run_turn(make_request(), StubTools())


async def test_repeat_turn_is_replayed_forever_after_ordinary_turns():
    provider = FakeProvider(
        make_scenario(
            {"steps": [say("Первый.")]},
            {"steps": [tool("chronicle", entry="запись"), say("Снова.")], "repeat": True},
        )
    )
    tools = StubTools()

    texts = [(await provider.run_turn(make_request(), tools)).text for _ in range(5)]

    assert texts == ["Первый.", "Снова.", "Снова.", "Снова.", "Снова."]
    assert len(tools.calls) == 4


async def test_pc_placeholders_become_character_ids_in_nested_args():
    scenario = make_scenario(
        {
            "steps": [
                tool(
                    "request_check",
                    character_id="$pc2",
                    targets=["$pc1", {"who": "$pc2"}],
                    reason="заметил $pc1",
                ),
                say("Рассказ."),
            ]
        }
    )
    tools = StubTools()

    result = await FakeProvider(scenario, pc_ids=("char-a", "char-b")).run_turn(
        make_request(), tools
    )

    expected = {
        "character_id": "char-b",
        "targets": ["char-a", {"who": "char-b"}],
        "reason": "заметил char-a",
    }
    assert tools.calls == [("request_check", expected)]
    assert result.trace[0].args == expected


@pytest.mark.parametrize("pc_ids", [(), ("char-a", "char-b")])
async def test_placeholder_without_character_is_provider_error(pc_ids):
    scenario = make_scenario({"steps": [tool("request_check", character_id="$pc3"), say("Ок.")]})
    tools = StubTools()

    with pytest.raises(ProviderError, match=r"\$pc3"):
        await FakeProvider(scenario, pc_ids=pc_ids).run_turn(make_request(), tools)

    assert tools.calls == []


async def test_tool_iterations_over_the_limit_raise_before_the_extra_call():
    scenario = make_scenario(
        {"steps": [tool("a"), tool("b"), tool("c"), say("Рассказ.")]},
    )
    tools = StubTools()

    with pytest.raises(ToolLoopExceeded):
        await FakeProvider(scenario).run_turn(make_request(max_tool_iterations=2), tools)

    assert [name for name, _ in tools.calls] == ["a", "b"]


async def test_tool_iterations_exactly_at_the_limit_pass():
    scenario = make_scenario({"steps": [tool("a"), tool("b"), say("Рассказ.")]})

    result = await FakeProvider(scenario).run_turn(make_request(max_tool_iterations=2), StubTools())

    assert result.text == "Рассказ."


def test_loop_and_refusal_errors_are_provider_errors():
    # Оркестратор ловит `ProviderError` и пишет неудачную попытку в журнал для всех видов отказа.
    assert issubclass(ToolLoopExceeded, ProviderError)
    assert issubclass(ProviderRefusal, ProviderError)


def test_fake_provider_satisfies_the_provider_protocol():
    provider: LLMProvider = FakeProvider(make_scenario())

    assert provider.name == "fake"


# --- complete_json ---


async def test_complete_json_returns_schema_instance_for_purpose():
    provider = FakeProvider(make_scenario(weaving={"hooks": ["крючок"]}))

    result = await provider.complete_json(make_json_request("weaving"), Weaving)

    assert result == Weaving(hooks=["крючок"])


async def test_complete_json_with_invalid_data_raises_provider_error():
    provider = FakeProvider(make_scenario(weaving={"hooks": "не список"}))

    with pytest.raises(ProviderError, match="hooks"):
        await provider.complete_json(make_json_request("weaving"), Weaving)


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
async def test_complete_json_error_escapes_the_name_of_an_extra_key(key):
    provider = FakeProvider(make_scenario(weaving={"hooks": ["крючок"], key: 1}))

    with pytest.raises(ProviderError) as error:
        await provider.complete_json(make_json_request("weaving"), StrictWeaving)

    assert str(error.value).isprintable()  # ни ESC, ни BEL, ни возврата каретки
    assert "\\x1b" in str(error.value)  # запись видна глазами, а не потеряна


async def test_complete_json_error_text_is_bounded_like_the_real_adapters():
    provider = FakeProvider(
        make_scenario(weaving={"hooks": [], **{f"лишний-{n}": n for n in range(500)}})
    )

    with pytest.raises(ProviderError) as error:
        await provider.complete_json(make_json_request("weaving"), StrictWeaving)

    assert len(str(error.value)) < 2 * MAX_PROBLEMS_CHARS


async def test_complete_json_without_answer_for_purpose_raises_provider_error():
    provider = FakeProvider(make_scenario(weaving={"hooks": []}))

    with pytest.raises(ProviderError, match="concept"):
        await provider.complete_json(make_json_request("concept"), Weaving)


async def test_complete_json_list_gives_answers_one_per_call_then_runs_out():
    # Список нужен тестам генератора: первый круг шага даёт плохой ответ, повтор — хороший.
    provider = FakeProvider(make_scenario(weaving=[{"hooks": ["первый"]}, {"hooks": ["второй"]}]))
    request = make_json_request("weaving")

    first = await provider.complete_json(request, Weaving)
    second = await provider.complete_json(request, Weaving)

    assert (first.hooks, second.hooks) == (["первый"], ["второй"])
    with pytest.raises(ProviderError, match="сценарий исчерпан"):
        await provider.complete_json(request, Weaving)


async def test_complete_json_single_answer_is_given_every_time():
    provider = FakeProvider(make_scenario(weaving={"hooks": ["один"]}))

    answers = [await provider.complete_json(make_json_request(), Weaving) for _ in range(3)]

    assert all(answer.hooks == ["один"] for answer in answers)


# --- формат сценария ---


@pytest.mark.parametrize(
    "turn",
    [
        {"steps": [say("Конец."), tool("chronicle")]},
        {"steps": [{"tool": "chronicle", "text": "и то и другое"}]},
        {"steps": [{"unknown": 1}]},
        {"steps": [say("Рассказ.")], "repeats": True},
    ],
    ids=["шаги после text", "tool и text вместе", "неизвестный шаг", "неизвестное поле хода"],
)
def test_malformed_turn_is_rejected(turn):
    with pytest.raises(ValidationError):
        make_scenario(turn)


def test_repeat_turn_must_be_the_last_one():
    with pytest.raises(ValidationError, match="repeat"):
        make_scenario({"steps": [say("Всегда.")], "repeat": True}, {"steps": [say("Никогда.")]})


async def test_basic_round_scenario_runs_against_real_engine_tool_schemas():
    # Сценарий не должен расходиться с реестром движка: аргументы шагов проходят схемы инструментов.
    registry = build_registry(build_ruleset(RulesRef()))
    specs = {spec.name: spec for spec in registry.specs()}

    class SchemaCheckingTools:
        async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
            specs[name].args_model.model_validate(args)
            return "{}", False

    provider = FakeProvider(load_scenario(SCENARIOS_DIR / "basic_round.yaml"), pc_ids=("pc-1",))

    turn = await provider.run_turn(make_request(), SchemaCheckingTools())
    weaving = await provider.complete_json(make_json_request("weaving"), Weaving)

    assert [call.name for call in turn.trace] == ["request_check", "chronicle"]
    assert turn.trace[0].args["character_id"] == "pc-1"
    assert turn.text == "Тени скрывают вас…"
    assert weaving.hooks == []


# --- чтение недоверенного YAML (сценарий, сиды генератора) ---
# Конструкторы значений PyYAML на чужом вводе бросают не только `YAMLError`: `!!int ""` даёт
# `IndexError`, `!!bool x` — `KeyError`, `!!timestamp x` — `AttributeError`, `!!int x` —
# `ValueError`; у двух последних в тексте значение из файла. Вложенность — `RecursionError`.

SECRET = "sk-LEAK-yaml"
UNREADABLE_TEXTS = {
    "битый YAML": (f"a: [{SECRET}", r"^ParserError, строка \d+, столбец \d+$"),
    "IndexError": ('a: !!int ""', "^значение не построить"),
    "KeyError": (f"a: !!bool {SECRET}", "^значение не построить"),
    "AttributeError": (f"a: !!timestamp {SECRET}", "^значение не построить"),
    "ValueError": (f"a: !!int {SECRET}", "^значение не построить"),
    "RecursionError": ("a: " + "[" * 5000 + "]" * 5000, "^слишком глубокая вложенность$"),
}


def test_parse_yaml_returns_the_data_of_a_good_document():
    assert parse_yaml("a: [1, 2]\nb: текст\n") == {"a": [1, 2], "b": "текст"}


@pytest.mark.parametrize(
    ("text", "expected"), UNREADABLE_TEXTS.values(), ids=UNREADABLE_TEXTS.keys()
)
def test_unreadable_yaml_is_reported_by_kind_without_the_file_content(text, expected):
    with pytest.raises(UnreadableYaml, match=expected) as error:
        parse_yaml(text)

    story = "".join(traceback.format_exception(error.value))
    assert SECRET.lower() not in story.lower()
    # Исходное исключение несёт значение из файла: ни причины, ни контекста у ошибки быть не должно.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_load_scenario_reports_unreadable_yaml_not_a_parser_exception(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(f"turns: !!int {SECRET}\n", encoding="utf-8")

    with pytest.raises(UnreadableYaml, match="значение не построить"):
        load_scenario(path)


# --- чтение YAML-файла: предел размера, только обычные файлы, без якорей и алиасов ---
# Файл сценария и файл сидов читаются теми же частями движка, что и папка приключения: якорь и
# алиас раздувают малый файл в миллионы моделей (в сидах это ещё и каждый платный запрос
# генератора), а канал FIFO вместо файла подвесил бы чтение.

ANCHORS_REFUSED = "якоря и алиасы YAML не поддерживаются"
TOO_LARGE = f"файл больше {MAX_YAML_BYTES // 1024} КБ"
ANCHOR_SECRET = "sk-LEAK-anchor"
ANCHORED_TEXTS = {
    "якорь и алиас": "a: &base [1, 2]\nb: *base\n",
    "якорь без алиаса": "a: &base [1, 2]\n",
    "слияние по алиасу": "base: &base {x: 1}\nitem: {<<: *base}\n",
    "алиас без якоря": "a: *base\n",
    "алиас в корне": "*base\n",
    "секрет в именах": f"[&{ANCHOR_SECRET} 1, *{ANCHOR_SECRET}]",
    # PyYAML сам называет имя в сообщениях «неопределённый алиас» и «повторный якорь»: до них дело
    # не должно дойти, иначе причину пришлось бы прятать.
    "секрет в имени неопределённого алиаса": f"[*{ANCHOR_SECRET}]",
    "секрет в именах повторных якорей": f"[&{ANCHOR_SECRET} 1, &{ANCHOR_SECRET} 2]",
}

needs_fifo = pytest.mark.skipif(
    not (hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM")), reason="нужны FIFO и SIGALRM"
)


@pytest.fixture
def fail_if_reading_hangs() -> Iterator[None]:
    """Зависшее чтение канала роняет тест через пять секунд, а не подвешивает прогон."""

    def give_up(signum: int, frame: object) -> None:
        raise TimeoutError("чтение канала не закончилось")

    previous = signal.signal(signal.SIGALRM, give_up)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def write_text_file(tmp_path: Path, text: str, name: str = "data.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_read_yaml_returns_the_data_of_a_good_file(tmp_path):
    path = write_text_file(tmp_path, "a: [1, 2]\nb: текст\nc: {<<: {x: 1}}\n")

    # Слияние со встроенным словарём (без алиаса) разрешено: бомбы оно не строит.
    assert read_yaml(path) == {"a": [1, 2], "b": "текст", "c": {"x": 1}}


@pytest.mark.parametrize("text", ANCHORED_TEXTS.values(), ids=ANCHORED_TEXTS.keys())
def test_read_yaml_refuses_anchors_and_aliases_with_a_clear_reason(tmp_path, text):
    with pytest.raises(UnreadableYaml, match=ANCHORS_REFUSED) as error:
        read_yaml(write_text_file(tmp_path, text))

    story = "".join(traceback.format_exception(error.value))
    assert ANCHOR_SECRET not in story
    # Исходное исключение разбора несёт позицию и разметку файла: цепочки у ошибки быть не должно.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_read_yaml_names_the_place_of_the_refused_alias(tmp_path):
    with pytest.raises(UnreadableYaml, match=r"ComposerError, строка 2, столбец 4"):
        read_yaml(write_text_file(tmp_path, "a: 1\nb: *base\n"))


def test_read_yaml_names_a_second_document_without_quoting_the_file(tmp_path):
    # Другая ошибка композера: текст PyYAML фиксирован, но и он не должен цитировать файл.
    path = write_text_file(tmp_path, f"a: 1\n---\nb: {ANCHOR_SECRET}\n")

    with pytest.raises(UnreadableYaml, match=r"^ComposerError, строка \d+, столбец \d+") as error:
        read_yaml(path)

    assert ANCHOR_SECRET not in str(error.value)


def test_read_yaml_refuses_a_file_over_the_limit_and_takes_one_at_the_limit(tmp_path):
    at_limit = write_text_file(tmp_path, "#" * MAX_YAML_BYTES, "at_limit.yaml")
    over_limit = write_text_file(tmp_path, "#" * (MAX_YAML_BYTES + 1), "over_limit.yaml")

    assert read_yaml(at_limit) is None  # один комментарий: данных нет, но файл прочитан
    with pytest.raises(UnreadableYaml, match=TOO_LARGE) as error:
        read_yaml(over_limit)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_read_yaml_does_not_read_a_file_over_the_limit(tmp_path, monkeypatch):
    path = write_text_file(tmp_path, "#" * (MAX_YAML_BYTES + 1))

    def forbidden(self: Path) -> bytes:
        raise AssertionError("файл больше предела прочитан")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(UnreadableYaml, match=TOO_LARGE):
        read_yaml(path)


def test_read_yaml_keeps_the_error_of_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        read_yaml(tmp_path / "nope.yaml")


def test_read_yaml_keeps_the_error_of_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "data.yaml"
    path.write_bytes(b"a: \xff\xfe\n")

    with pytest.raises(UnicodeDecodeError):
        read_yaml(path)


def test_read_yaml_refuses_a_folder_under_a_file_name(tmp_path):
    folder = tmp_path / "data.yaml"
    folder.mkdir()

    with pytest.raises(OSError, match="не обычный файл"):
        read_yaml(folder)


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="нет /dev/null")
def test_read_yaml_refuses_a_device_behind_a_file_name(tmp_path):
    device = tmp_path / "data.yaml"
    device.symlink_to("/dev/null")

    with pytest.raises(OSError, match="не обычный файл"):
        read_yaml(device)


@needs_fifo
def test_read_yaml_refuses_a_pipe_without_reading_it(tmp_path, fail_if_reading_hangs):
    pipe = tmp_path / "data.yaml"
    os.mkfifo(pipe)

    with pytest.raises(OSError, match="не обычный файл"):
        read_yaml(pipe)


def test_load_scenario_refuses_a_scenario_with_an_alias_in_a_valid_shape(tmp_path):
    scenario = write_text_file(
        tmp_path,
        "turns:\n  - steps: &say [{text: Привет}]\n  - steps: *say\n",
    )

    with pytest.raises(UnreadableYaml, match=ANCHORS_REFUSED):
        load_scenario(scenario)


def test_load_scenario_refuses_a_scenario_over_the_limit(tmp_path):
    path = write_text_file(tmp_path, "turns: []\n" + "#" * MAX_YAML_BYTES)

    with pytest.raises(UnreadableYaml, match=TOO_LARGE):
        load_scenario(path)


@needs_fifo
def test_load_scenario_refuses_a_pipe_without_reading_it(tmp_path, fail_if_reading_hangs):
    pipe = tmp_path / "scenario.yaml"
    os.mkfifo(pipe)

    with pytest.raises(OSError, match="не обычный файл"):
        load_scenario(pipe)


# --- обрезка трассы ---


def test_trace_call_keeps_a_small_call_untouched():
    trace = make_trace_call("chronicle", {"entry": "запись"}, '{"entries": 1}', False)

    assert (trace.name, trace.args, trace.result, trace.is_error) == (
        "chronicle",
        {"entry": "запись"},
        '{"entries": 1}',
        False,
    )


def test_trace_call_clips_result_to_the_limit_and_marks_the_cut():
    trace = make_trace_call("get_location", {}, "я" * (MAX_TRACE_RESULT_CHARS * 3), True)

    assert len(trace.result) == MAX_TRACE_RESULT_CHARS
    assert trace.result.endswith("…")
    assert trace.is_error is True


def test_trace_call_keeps_a_result_exactly_at_the_limit():
    result = "я" * MAX_TRACE_RESULT_CHARS

    assert make_trace_call("get_location", {}, result, False).result == result


def test_trace_call_replaces_oversized_args_with_a_clipped_json_text():
    args = {"entry": "я" * (MAX_TRACE_ARGS_CHARS * 3)}

    trace = make_trace_call("chronicle", args, "{}", False)

    assert set(trace.args) == {"_truncated"}
    assert len(trace.args["_truncated"]) <= MAX_TRACE_ARGS_CHARS
    assert trace.args["_truncated"].startswith('{"entry": "яяя')
    assert trace.args["_truncated"].endswith("…")
    json.dumps(trace.args)  # трасса попадает в JSON-журнал хода


def test_trace_call_keeps_args_whose_json_is_exactly_at_the_limit():
    filler = "я" * (MAX_TRACE_ARGS_CHARS - len(json.dumps({"entry": ""}, ensure_ascii=False)))
    args = {"entry": filler}
    assert len(json.dumps(args, ensure_ascii=False)) == MAX_TRACE_ARGS_CHARS

    assert make_trace_call("chronicle", args, "{}", False).args == args


def test_trace_call_clips_tool_name_chosen_by_the_model():
    trace = make_trace_call("и" * 1000, {}, "{}", True)

    assert len(trace.name) == MAX_TRACE_NAME_CHARS


async def test_turn_trace_is_clipped_but_the_executor_gets_full_args():
    huge_args = {"entry": "я" * (MAX_TRACE_ARGS_CHARS * 2)}
    scenario = make_scenario({"steps": [tool("chronicle", **huge_args), say("Рассказ.")]})
    tools = StubTools(result="р" * (MAX_TRACE_RESULT_CHARS * 2))

    result = await FakeProvider(scenario).run_turn(make_request(), tools)

    assert tools.calls == [("chronicle", huge_args)]
    assert len(result.trace[0].result) == MAX_TRACE_RESULT_CHARS
    assert set(result.trace[0].args) == {"_truncated"}


# --- расход токенов ---


def test_usage_addition_sums_every_counter():
    first = Usage(input_tokens=10, output_tokens=2, cache_read_tokens=5, cache_write_tokens=1)
    second = Usage(input_tokens=1, output_tokens=3, cache_read_tokens=0, cache_write_tokens=4)

    assert first + second == Usage(
        input_tokens=11, output_tokens=5, cache_read_tokens=5, cache_write_tokens=5
    )
