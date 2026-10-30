"""Промпты ролей нейронки: по файлу `<роль>.md` на роль.

Код промптов не держит: роль читается одним загрузчиком `load`, а модули собирают запрос из её
текста и подстановок (данные игры, пределы длины). Файлы — ресурсы пакета, поэтому едут в колесе
вместе с кодом. Текст читается один раз за жизнь процесса: правка файла подхватывается после
перезапуска сервера.

Режим мата — свойство игры. Сам режим называет одна строка (`profanity_line`): её кладёт
в контекст хода мастер, а служебные роли, чей текст видят игроки, получают её в конце общего блока
стиля (`style_block`). Правила обоих режимов описаны в `master.md` и `style.md` словами этой же
строки, поэтому менять её можно только вместе с ними.
"""

import re
from functools import cache
from importlib import resources

PROFANITY_ALLOWED = "Мат: разрешён"
PROFANITY_FORBIDDEN = "Мат: запрещён"

# Имя роли — короткое слово кода, а не путь: другой файл пакета или файл вне его не прочитать.
_ROLE_NAME = re.compile(r"[a-z_]+")


@cache
def load(name: str) -> str:
    """Текст роли `name` из `<name>.md` без пробелов по краям; неизвестная роль — `KeyError`."""
    if not _ROLE_NAME.fullmatch(name):
        raise KeyError(name)
    resource = resources.files(__name__).joinpath(f"{name}.md")
    if not resource.is_file():
        raise KeyError(name)
    return resource.read_text(encoding="utf-8").strip()


def profanity_line(profanity: bool) -> str:
    """Строка режима мата игры: «Мат: разрешён» или «Мат: запрещён»."""
    return PROFANITY_ALLOWED if profanity else PROFANITY_FORBIDDEN


def style_block(profanity: bool) -> str:
    """Общий блок стиля для ролей, чей текст видят игроки, и строка режима мата игры в конце."""
    return f"{load('style')}\n\n{profanity_line(profanity)}"


def role_with_style(name: str, profanity: bool) -> str:
    """Промпт роли, чей текст видят игроки: текст роли и следом общий блок стиля игры."""
    return f"{load(name)}\n\n{style_block(profanity)}"
