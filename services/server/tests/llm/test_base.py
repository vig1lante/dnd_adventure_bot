"""Общие помощники `base.py`: нейтрализация недоверенного текста и описание ошибок схемы.

Проверка безопасности показала: имена лишних ключей ответа модели попадали в текст `ProviderError`
как есть, а `dnd-generate` печатал его в терминал владельца. Escape-последовательности в таком
тексте подменяют вывод, меняют заголовок окна и пишут в буфер обмена (OSC 52).
"""

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dnd_server.llm.base import MAX_LOC_PART_CHARS, printable, schema_problems

# Атаки через такой текст: запись в буфер обмена (OSC 52) и стирание строки с возвратом каретки.
OSC_52_KEY = "\x1b]52;c;ZWNobyBwd25lZA==\x07"
ERASE_LINE_KEY = "\x1b[2K\rПриключение записано"
HOSTILE_KEYS = {"osc52-буфер-обмена": OSC_52_KEY, "стирание-строки": ERASE_LINE_KEY}


class StrictHooks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hooks: list[str]


class Cat(BaseModel):
    kind: Literal["cat"]


class Dog(BaseModel):
    kind: Literal["dog"]


class Owner(BaseModel):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]


def validation_error_of(model: type[BaseModel], data: dict) -> ValidationError:
    with pytest.raises(ValidationError) as error:
        model.model_validate(data)
    return error.value


# --- printable ---


def test_printable_keeps_ordinary_text_untouched():
    text = "Тени скрывают вас… «ё» 🕯 путь\\к\\файлу 'кавычки' \"двойные\""

    assert printable(text) == text


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        ("\x1b", r"\x1b"),  # начало escape-последовательности
        ("\x07", r"\x07"),  # BEL: завершает OSC
        ("\r", r"\r"),  # возврат каретки: стирает уже напечатанное в строке
        ("\x00", r"\x00"),
        ("\x7f", r"\x7f"),
        ("\x85", r"\x85"),  # NEL из C1
        ("\x9b", r"\x9b"),  # CSI одним байтом
        ("\x9d", r"\x9d"),  # OSC одним байтом
        ("\t", r"\t"),  # табуляция не нужна в имени поля и сообщении: сдвигает столбцы
        ("\n", r"\n"),  # перевод строки по умолчанию экранируется: имя поля — одна строка
        ("\xa0", r"\xa0"),  # неразрывный пробел выглядит как обычный
        ("\u200b", r"\u200b"),  # пробел нулевой ширины
        ("\u2028", r"\u2028"),  # разделитель строк Юникода
        ("\u202e", r"\u202e"),  # переключение направления письма
        ("\ud800", r"\ud800"),  # одинокий суррогат: print в stdout падает с UnicodeEncodeError
        ("\U000e0001", r"\U000e0001"),  # тег вне BMP
    ],
)
def test_printable_replaces_unprintable_character_with_its_escape(char, escaped):
    assert printable(f"до{char}после") == f"до{escaped}после"


def test_printable_leaves_nothing_unprintable_for_any_code_point():
    everything = "".join(chr(code) for code in range(0x110000))

    result = printable(everything)

    assert result.isprintable()


def test_printable_keeps_newlines_only_on_request():
    text = "строка один\nстрока два\r\nтретья"

    assert printable(text, keep_newlines=True) == "строка один\nстрока два\\r\nтретья"
    assert printable(text) == "строка один\\nстрока два\\r\\nтретья"


def test_printable_clips_to_the_limit_with_an_ellipsis():
    assert printable("а" * 100, 10) == "а" * 9 + "…"
    assert printable("а" * 10, 10) == "а" * 10


def test_printable_limit_counts_the_escaped_text():
    # Экранирование удлиняет запись: предел действует на то, что будет напечатано.
    result = printable("\x1b" * 20, 10)

    assert len(result) == 10
    assert result.endswith("…") and result.isprintable()


# --- schema_problems ---


@pytest.mark.parametrize("key", HOSTILE_KEYS.values(), ids=HOSTILE_KEYS.keys())
def test_schema_problems_escapes_the_name_of_an_extra_key(key):
    error = validation_error_of(StrictHooks, {"hooks": [], key: 1})

    problems = schema_problems(error)

    assert problems.isprintable()
    assert "\\x1b" in problems
    assert "Extra inputs are not permitted" in problems


def test_schema_problems_clips_a_long_extra_key():
    error = validation_error_of(StrictHooks, {"hooks": [], "к" * 5000: 1})

    problems = schema_problems(error)

    assert "к" * MAX_LOC_PART_CHARS not in problems
    assert "к" * (MAX_LOC_PART_CHARS - 1) + "…" in problems


def test_schema_problems_escapes_model_text_inside_the_message():
    # Сообщение pydantic о неизвестном теге цитирует тег из ответа модели.
    error = validation_error_of(Owner, {"pet": {"kind": OSC_52_KEY}})

    problems = schema_problems(error)

    assert problems.isprintable()
    assert "\\x1b" in problems


def test_schema_problems_keeps_the_path_and_the_message_of_ordinary_errors():
    error = validation_error_of(StrictHooks, {"hooks": ["a", 3]})

    assert schema_problems(error) == "hooks.1: Input should be a valid string"
