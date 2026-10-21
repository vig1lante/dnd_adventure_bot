"""Общие помощники текстов мастера (`dm.text`): строки для модели и безопасные тексты ошибок.

Раньше одни и те же механизмы жили копиями в контексте, летописи, вплетении и ходе мастера, а модули
импортировали приватные имена друг друга. Здесь проверяется сам общий модуль; что из него получают
контекст, летопись, вплетение и ход, проверяют их собственные тесты.
"""

import json

import pytest
from pydantic import SecretStr

from dnd_server.config import Settings
from dnd_server.dm import text
from dnd_server.llm.base import ProviderError, ProviderRefusal, ToolLoopExceeded
from dnd_server.llm.factory import ConfigError

# --- строки для контекста и запросов ---


def test_a_json_line_is_one_line_even_for_the_separators_json_leaves_alone():
    value = {"текст": "раз\u0085два\u2028три\u2029четыре\nпять"}

    line = text.json_line(value)

    assert "\n" not in line and not any(sep in line for sep in "\u0085\u2028\u2029")
    assert len(line.splitlines()) == 1
    assert json.loads(line) == value  # экранирование обратимо: данные не изменились


def test_a_json_line_keeps_russian_letters_readable():
    assert text.json_line("Склеп") == '"Склеп"'


def test_one_line_folds_every_kind_of_whitespace_into_single_spaces():
    assert text.one_line("  раз\n\nдва\t три\u0085четыре\u2028пять  ") == "раз два три четыре пять"


def test_a_printable_line_is_one_clipped_line_with_the_unprintable_made_visible():
    line = text.printable_line("а\x1b[31mб\nв‮г", 100)

    assert line == "а\\x1b[31mб в\\u202eг"
    assert text.printable_line("я" * 50, 10) == "я" * 9 + "…"


# --- безопасный текст ошибки ---

KEY = "sk-ant-api-SECRET-0123456789"
TOKEN = "123456789:AAF-text-bot-token-0123456789_abcdef"


def settings_with_secrets() -> Settings:
    return Settings(anthropic_api_key=SecretStr(KEY), telegram_bot_token=SecretStr(TOKEN))


def test_an_error_text_never_holds_a_secret_of_the_settings():
    safe = text.safe_error_text(f"ключ {KEY} и токен {TOKEN}", settings_with_secrets())

    assert KEY not in safe and TOKEN not in safe
    assert safe == "ключ *** и токен ***"


def test_an_error_text_is_clipped_and_has_no_terminal_control_characters():
    safe = text.safe_error_text("x\x1b]0;вредное\x07" + "я" * 2000, settings_with_secrets())

    assert "\x1b" not in safe and "\x07" not in safe
    assert len(safe) <= text.MAX_ERROR_CHARS


def test_a_traceback_keeps_its_lines_and_its_length_when_asked_to():
    trace = "Traceback:\n  File x\nValueError: " + "я" * 2000

    safe = text.safe_error_text(trace, settings_with_secrets(), keep=True)

    assert safe.count("\n") == 2 and len(safe) == len(trace)


def test_the_description_of_a_validation_error_says_where_but_not_the_value_of_the_input(leak):
    description = text.describe_error(leak.error, settings_with_secrets())

    assert description.startswith("ValidationError: ")
    assert "chronicle" in description and "at most 3" in description  # где и что не так
    assert leak.value not in description and "input_value" not in description


def test_the_description_of_an_ordinary_error_is_its_type_and_its_text_without_secrets():
    description = text.describe_error(RuntimeError(f"сбой с ключом {KEY}"), settings_with_secrets())

    assert description == "RuntimeError: сбой с ключом ***"


def inner_failure(message: str) -> None:
    raise RuntimeError(message)


def failure_with_cause(message: str) -> Exception:
    try:
        try:
            inner_failure(message)
        except RuntimeError as inner:
            raise ValueError("итоговая ошибка") from inner
    except ValueError as outer:
        return outer
    raise AssertionError("ошибки не было")


def test_a_safe_traceback_shows_the_frames_and_the_described_errors_of_the_whole_chain():
    trace = text.safe_traceback(failure_with_cause("корень беды"), settings_with_secrets())

    assert "inner_failure" in trace and "failure_with_cause" in trace  # кадры стека на месте
    assert trace.index("RuntimeError: корень беды") < trace.index("ValueError: итоговая ошибка")
    assert trace.count("Traceback") == 2  # по одной на каждую ошибку цепочки


def test_a_safe_traceback_holds_no_secret_no_control_character_and_no_input_of_pydantic(leak):
    settings = settings_with_secrets()

    for error in (failure_with_cause(f"ключ {KEY}, токен {TOKEN}\x1b[31m"), leak.error):
        trace = text.safe_traceback(error, settings)
        assert KEY not in trace and TOKEN not in trace and "\x1b" not in trace
        assert leak.value not in trace and "input_value" not in trace
    assert "ValidationError" in text.safe_traceback(leak.error, settings)


def test_a_safe_traceback_works_where_there_are_no_settings_to_take_secrets_from(leak):
    trace = text.safe_traceback(leak.error, None)

    assert "ValidationError" in trace and leak.value not in trace


def test_the_text_of_a_long_error_in_a_traceback_is_clipped_but_the_frames_are_not():
    trace = text.safe_traceback(failure_with_cause("я" * 5000), None)

    assert "inner_failure" in trace
    assert len(trace) < 2 * text.MAX_ERROR_CHARS + 2000


# --- причина неудачи для хоста ---


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProviderRefusal("отказ"), text.REASON_REFUSAL),
        (ToolLoopExceeded("петля"), text.REASON_LOOP),
        (text.ModelTimeout("тайм-аут: 180 с"), text.REASON_TIMEOUT),
        (ConfigError("нет ключа"), text.REASON_CONFIG),
        (ProviderError("упал адаптер"), text.REASON_PROVIDER),
        (RuntimeError("баг"), text.REASON_INTERNAL),
        (TimeoutError("чужой тайм-аут"), text.REASON_INTERNAL),
    ],
    ids=["refusal", "loop", "timeout", "config", "provider", "internal", "foreign-timeout"],
)
def test_the_reason_for_the_host_follows_the_kind_of_the_error(error, reason):
    assert text.failure_reason(error) == reason


def test_a_refusal_is_told_apart_from_the_other_errors_of_the_provider():
    # Отказ и петля — частные случаи ошибки провайдера, и причина у них своя.
    assert issubclass(ProviderRefusal, ProviderError)
    assert issubclass(ToolLoopExceeded, ProviderError)
    assert text.failure_reason(ProviderRefusal("x")) != text.failure_reason(ProviderError("x"))
