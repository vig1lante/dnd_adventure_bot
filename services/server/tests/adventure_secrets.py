"""Тайные тексты данных приключения для тестов утечек: что мастер читает, а игрок видеть не должен.

Один помощник на все тесты, которые ищут тайны приключения в том, что уходит игрокам (сквозные
сценарии `flows/` и `playthrough/`, ход мастера в `dm/test_turn.py`): правка формата приключения
меняет его в одном месте, и проверки утечек не расходятся.

Модуль лежит в корне тестов сервера: эту папку pytest кладёт в `sys.path` вместе с её
`conftest.py`, поэтому тесты из подпапок берут его как `from adventure_secrets import …`.
"""

from typing import Any

# Поля приключения, строки которых игрок видеть не должен: тайны NPC, богов и фракций, заметки
# мастера и то, что знают NPC.
HIDDEN_ADVENTURE_FIELDS = ("secret", "dm_notes", "knows")


def hidden_texts(node: Any) -> list[str]:
    """Все тайные тексты на любой глубине данных приключения: строки полей `secret`, `dm_notes` и
    `knows`, тайны мест (`secrets[].text`) и пояснения тайных связей графа (`note` при
    `secret: true`; у связи `secret` — признак, а не текст)."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("secret") is True:
            found.append(node.get("note", ""))
        for key, value in node.items():
            if isinstance(value, bool):
                continue
            if key in HIDDEN_ADVENTURE_FIELDS:
                found += [value] if isinstance(value, str) else [str(item) for item in value]
            elif key == "secrets":
                found += [secret["text"] for secret in value]
            else:
                found += hidden_texts(value)
    elif isinstance(node, list):
        for item in node:
            found += hidden_texts(item)
    return [text for text in found if text]
