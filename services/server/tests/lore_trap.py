"""Ловушки в данных мастера формата v2 для тестов видимости.

Мир, боги, фракции, связи, тайны мест и злодей — данные мастера: игрокам из них не уходит ничего,
кроме рассказа мастера. Тесты кладут метку в каждый такой текст и ищут её во всём, что получает
игрок (снимок, события, ответы REST, запрос вплетения), и в том, что видит мастер (контекст и
журнал хода). Поля дневника `public` («что могут знать игроки») — не тайна и не ловушка.

Модуль лежит в корне тестов сервера: эту папку pytest кладёт в `sys.path` вместе с её
`conftest.py`, поэтому тесты из подпапок берут его как `from lore_trap import …`.
"""

import copy
from typing import Any

# Ключи данных мастера формата v2: ни один не должен встретиться в том, что уходит игроку. Злодей —
# ссылка по шаблону (`npc:<id>`), метку в неё не положить: его ищут по ключу.
LORE_KEYS = frozenset(
    {"history", "regions", "rumors", "deities", "factions", "relations", "villain", "secrets"}
)
_DEITY_TEXTS = ("name", "title", "symbol", "dogma", "appearance", "attitude_to_mortals", "secret")
_FACTION_TEXTS = ("name", "goal", "methods", "secret")


def trap_lore(data: dict[str, Any], mark: str) -> dict[str, Any]:
    """Копия данных приключения (`model_dump(mode="json")`, как `games.adventure_data`), где каждый
    текст мира, богов, фракций, связей и тайн мест начинается с `mark`, а дальше сказано, откуда
    он (`<mark>связь-<id>`). Ссылки и id не меняются: копия проходит `adventure_from_data`.
    Исходные данные не меняются."""
    data = copy.deepcopy(data)
    world = data["world"]
    world["history"] = f"{mark}история-мира"
    for region in world["regions"]:
        region["name"] = f"{mark}край-{region['id']}"
        region["description"] = f"{mark}описание-края-{region['id']}"
    for number, rumor in enumerate(world["rumors"]):
        rumor["text"] = f"{mark}слух-{number}"
    for deity in data["deities"].values():
        deity |= {field: f"{mark}{field}-{deity['id']}" for field in _DEITY_TEXTS}
        deity["domains"] = [f"{mark}сфера-{deity['id']}"]
    for faction in data["factions"].values():
        faction |= {field: f"{mark}{field}-{faction['id']}" for field in _FACTION_TEXTS}
    for relation in data["relations"]:
        relation["note"] = f"{mark}связь-{relation['id']}"
    for location in data["locations"].values():
        for secret in location["secrets"]:
            secret["text"] = f"{mark}тайна-места-{secret['id']}"
    # Ловушка без мира проверяла бы пустоту: в данных должно быть всё, что она накрывает.
    assert data["villain"] and data["deities"] and data["factions"] and data["relations"]
    assert world["regions"] and world["rumors"]
    assert any(location["secrets"] for location in data["locations"].values())
    return data
