"""Промпты ролей нейронки — файлы пакета `dnd_server.prompts` (спека Р2, §4, §5, план B1.2).

Каждая роль, чей текст получает модель, лежит отдельным `<роль>.md` и читается одним загрузчиком
`load`. Голос мастера (раздел «Как ты пишешь») и общий блок стиля описывают современный язык,
запрет архаики и два режима мата; какой режим действует в игре, говорит одна строка
(`profanity_line`), её же подставляет контекст хода и блок стиля служебных ролей.
"""

import re
from importlib import resources

import pytest

from dnd_server import prompts
from dnd_server.prompts import (
    PROFANITY_ALLOWED,
    PROFANITY_FORBIDDEN,
    load,
    profanity_line,
    role_with_style,
    style_block,
)

# Роли блока B1 (спека §5): мастер, общий стиль и служебные роли.
ROLES = ("master", "style", "hero_flavor", "backstory_helper", "weaver", "chronicler")
# Роли, чей текст читают игроки: им нужен запрет архаики и правила мата (спека §4).
VOICES = ("master", "style")
# Архаика, которой нет места в рассказе (спека §4); «сударь» — только как манера NPC.
ARCHAIC = ("дабы", "сей", "оный", "молвил", "отрок", "ибо", "весьма", "отнюдь", "узрел", "сударь")
DONT_LIST_MARK = "Не пиши так"


def words(text: str) -> set[str]:
    return set(re.findall(r"[а-яё]+", text.lower()))


def without_dont_list(text: str) -> str:
    """Текст промпта без строки со списком «не пиши так»: только в ней архаика уместна."""
    return "\n".join(line for line in text.splitlines() if DONT_LIST_MARK not in line)


# --- загрузчик ---


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_a_non_empty_prompt_file_in_the_package(role):
    # Файл — ресурс пакета: так он едет в колесе вместе с кодом (uv_build кладёт всё под src).
    resource = resources.files(prompts).joinpath(f"{role}.md")

    assert resource.is_file()
    assert load(role).strip()
    assert load(role) == resource.read_text(encoding="utf-8").strip()


def test_the_package_holds_exactly_the_role_files_these_tests_check():
    # Проверки ниже (метры, «раса», архаика) идут по списку ролей: файл роли вне списка ушёл бы
    # к модели непроверенным. Новая роль сначала попадает в ROLES.
    files = {
        entry.name.removesuffix(".md")
        for entry in resources.files(prompts).iterdir()
        if entry.name.endswith(".md")
    }

    assert files == set(ROLES)


def test_an_unknown_role_is_a_key_error_that_names_it():
    with pytest.raises(KeyError, match="nope"):
        load("nope")


@pytest.mark.parametrize("name", ["", "../master", "master.md", "dm/master", "Master", "мастер"])
def test_only_a_plain_role_name_is_read(name):
    # Имя роли — короткое слово кода, а не путь: ни соседний файл, ни файл вне пакета не читаются.
    with pytest.raises(KeyError):
        load(name)


def test_a_prompt_is_read_once_and_then_comes_from_the_cache():
    assert load("master") is load("master")


def test_the_old_master_prompt_file_is_gone():
    assert not resources.files("dnd_server.dm").joinpath("prompts", "system.md").is_file()


# --- голос мастера и блок стиля ---


@pytest.mark.parametrize("role", ROLES)
def test_no_prompt_speaks_of_feet(role):
    # Расстояния для модели — только в метрах (спека §6).
    assert not re.search(r"фут|feet|foot", load(role), re.IGNORECASE)


@pytest.mark.parametrize("role", ROLES)
def test_no_prompt_calls_the_race_a_kind(role):
    # Спека Р4: в промптах «раса», поле данных остаётся `species` («на виду» — другое слово).
    assert not {"вид", "вида", "видом"} & words(load(role))


@pytest.mark.parametrize("role", VOICES)
def test_the_voice_forbids_archaic_words_by_name(role):
    (dont_list,) = [line for line in load(role).splitlines() if DONT_LIST_MARK in line]

    assert set(ARCHAIC) <= words(dont_list)


@pytest.mark.parametrize("role", ROLES)
def test_no_prompt_uses_archaic_words_outside_the_list_of_what_not_to_write(role):
    assert not set(ARCHAIC) & words(without_dont_list(load(role)))


@pytest.mark.parametrize("role", VOICES)
def test_the_voice_has_its_writing_section_and_the_manner_of_npcs(role):
    text = load(role)

    assert "Как ты пишешь" in text
    assert "Современный живой русский" in text
    if role == "master":
        # Манеру NPC мастер берёт из данных приключения (`manner` в ответе get_npc).
        assert "`manner`" in text and "`tone`" in text


@pytest.mark.parametrize("role", VOICES)
def test_the_voice_describes_both_profanity_modes_by_the_line_of_the_game(role):
    # Режим называет строка контекста; промпт описывает оба значения этой строки.
    text = load(role)

    assert PROFANITY_ALLOWED in text and PROFANITY_FORBIDDEN in text


@pytest.mark.parametrize("role", VOICES)
def test_the_voice_never_allows_insulting_the_players_or_forbidden_content(role):
    text = load(role).lower()

    assert "в любом режиме не оскорбляй самих игроков" in text
    assert "недопустимого" in text


def test_the_master_writes_the_story_of_the_dnd1_length_and_shape():
    text = load("master")

    assert "От 150 до 350 слов" in text
    assert "Живым литературным" not in text and "живым литературным" not in text


# --- строка режима и блок стиля ---


def test_the_profanity_line_names_the_mode_of_the_game():
    assert profanity_line(True) == PROFANITY_ALLOWED == "Мат: разрешён"
    assert profanity_line(False) == PROFANITY_FORBIDDEN == "Мат: запрещён"


@pytest.mark.parametrize("profanity", [True, False])
def test_the_style_block_is_the_style_prompt_and_the_line_of_the_mode(profanity):
    block = style_block(profanity)

    assert block.startswith(load("style"))
    assert block.splitlines()[-1] == profanity_line(profanity)
    assert block.count(profanity_line(not profanity)) == load("style").count(
        profanity_line(not profanity)
    )  # строка другого режима есть только в описании правил, а не в конце блока


@pytest.mark.parametrize("role", ["hero_flavor", "backstory_helper", "weaver"])
@pytest.mark.parametrize("profanity", [True, False])
def test_a_role_whose_text_players_read_gets_the_style_block_after_its_own_text(role, profanity):
    assert role_with_style(role, profanity) == f"{load(role)}\n\n{style_block(profanity)}"
