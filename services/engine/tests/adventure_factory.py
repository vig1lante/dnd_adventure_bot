"""Фабрика приключений для тестов валидатора: всё собирается в памяти, без файлов.

`make_adventure()` — «чистое» приключение формата v2: 3 локации, 2 NPC, 1 бой, 1 открытие с тремя
уликами в разных локациях; мир, бог, фракция, граф связей, секрет склепа и злодей. Валидатор не
находит в нём ни ошибок, ни предупреждений, поэтому любая проблема в тесте — результат правки,
которую тест сам внёс. Чтобы изменить вложенное поле, берут «сырые» данные из `adventure_data()`,
правят их и собирают `Adventure.model_validate(data)`.
"""

from typing import Any

from dnd_engine.adventure.schema import Adventure


def adventure_data() -> dict[str, Any]:
    """Свежие «сырые» данные чистого приключения (каждый вызов — новые словари и списки)."""
    return {
        "format_version": 2,
        "id": "factory-crypt",
        "title": "Склеп у деревни",
        "pitch": "Пропал смотритель кладбища, и в деревне боятся выходить после заката.",
        "tone": "готика с юмором",
        "target_hours": 1.5,
        "party_level": 3,
        "party_size": 6,
        "rules": {"modules": ["campfire"]},
        "start_location": "village",
        "finale_location": "crypt",
        "acts": [{"id": "act-1", "title": "Пропажа", "summary": "Найти смотрителя."}],
        "endings": ["Смотритель спасён", "Смотритель потерян"],
        "locations": {
            "village": {
                "id": "village",
                "name": "Деревня",
                "read_aloud": "Вы входите в притихшую деревню.",
                "exits": [
                    {
                        "id": "to-cemetery",
                        "to": "cemetery",
                        "direction": "север",
                        "description": "Тропа к кладбищу.",
                    }
                ],
                "features": [
                    {
                        "id": "board",
                        "name": "Доска объявлений",
                        "description": "Листок о пропавшем смотрителе.",
                    }
                ],
            },
            "cemetery": {
                "id": "cemetery",
                "name": "Кладбище",
                "read_aloud": "Покосившиеся кресты тонут в тумане.",
                "exits": [
                    {
                        "id": "to-village",
                        "to": "village",
                        "direction": "юг",
                        "description": "Тропа в деревню.",
                    },
                    {
                        # Скрытый и запертый выход тоже считается путём: валидатор не проверяет, как
                        # игроки его откроют.
                        "id": "to-crypt",
                        "to": "crypt",
                        "direction": "вниз",
                        "description": "Люк в склеп.",
                        "locked_by": "item:key",
                        "hidden_dc": 15,
                    },
                ],
                "features": [
                    {
                        "id": "tracks",
                        "name": "Следы",
                        "description": "Борозды от волочения ведут к люку.",
                        "check": {"ability": "wis", "skill": "survival", "dc": 10},
                        "reveals": [{"kind": "clue", "id": "clue-tracks"}],
                    }
                ],
                "encounter_id": "bones",
            },
            "crypt": {
                "id": "crypt",
                "name": "Склеп",
                "read_aloud": "Холод и тишина.",
                "exits": [
                    {
                        "id": "to-cemetery",
                        "to": "cemetery",
                        "direction": "вверх",
                        "description": "Лестница к люку.",
                    }
                ],
                "features": [
                    {
                        "id": "ledger",
                        "name": "Долговая книга",
                        "description": "Записи о долге трактирщицы.",
                        "reveals": [{"kind": "clue", "id": "clue-ledger"}],
                    }
                ],
                "secrets": [
                    {
                        "id": "empty-vault",
                        "text": "Тайник под алтарём давно пуст.",
                        "check": {"ability": "int", "skill": "investigation", "dc": 15},
                        "reveals": [{"kind": "flag", "id": "vault-empty"}],
                    }
                ],
            },
        },
        "npcs": {
            "marta": {
                "id": "marta",
                "name": "Марта",
                "role": "трактирщица",
                "appearance": "Крепкая женщина с усталым взглядом.",
                "manner": "говорит коротко",
                "motive": "найти брата",
                "location_id": "village",
            },
            "fabian": {
                "id": "fabian",
                "name": "Фабиан",
                "role": "некромант-неудачник",
                "appearance": "Тощий человек в чёрном плаще.",
                "manner": "тараторит и оправдывается",
                "motive": "расплатиться с долгами",
                "location_id": "crypt",
                "statblock": "cultist_fanatic",
            },
        },
        "items": {
            "key": {
                "id": "key",
                "name": "Ржавый ключ",
                "description": "Открывает люк в склеп.",
                "holder": "location:village",
            },
            "potion": {
                "id": "potion",
                "name": "Зелье лечения",
                "description": "Красная жидкость во фляжке.",
                "base_item": "potion_of_healing",
                "holder": "npc:marta",
            },
        },
        "encounters": {
            "bones": {
                "id": "bones",
                "location_id": "cemetery",
                "trigger": "игроки подходят к люку",
                "tactics": "гули наваливаются толпой",
                # 8 гулей по 200 XP на 6 персонажей 3-го уровня: «умеренный» бой.
                "monsters": [{"monster_id": "ghoul", "count": 8, "zone": "here"}],
            }
        },
        "revelations": {
            "kidnapper": {
                "id": "kidnapper",
                "statement": "Смотрителя похитил Фабиан ради долга.",
                "clues": [
                    {
                        "id": "clue-tracks",
                        "description": "Следы волочения.",
                        "where": "feature:tracks",
                    },
                    {"id": "clue-marta", "description": "Слова Марты.", "where": "npc:marta"},
                    {
                        "id": "clue-ledger",
                        "description": "Запись о долге.",
                        "where": "feature:ledger",
                    },
                ],
            }
        },
        "world": {
            "history": "Холм за деревней хранит склеп старого графа.",
            "regions": [{"id": "marsh", "name": "Болотный край", "description": "Туман и тропы."}],
            "rumors": [{"text": "В склепе спрятана казна графа.", "true": False}],
        },
        "deities": {
            "warden": {
                "id": "warden",
                "name": "Страж",
                "title": "Хранитель покоя мёртвых",
                "domains": ["смерть", "покой"],
                "symbol": "ключ на цепи",
                "dogma": "Мёртвым — покой.",
                "appearance": "Старик с фонарём.",
                "attitude_to_mortals": "строг, но справедлив",
                "avatar_statblock": "ghoul",
            }
        },
        "factions": {
            "candle-circle": {
                "id": "candle-circle",
                "name": "Круг бледной свечи",
                "goal": "Найти казну графа.",
                "methods": "Ритуалы по брошюрам.",
                "leader": "npc:fabian",
                "base_location": "crypt",
            }
        },
        "relations": [
            {
                "id": "fabian-debtor-marta",
                "a": "npc:fabian",
                "b": "npc:marta",
                "kind": "debtor",
                "note": "должник брата",
                "mutual": False,
            },
            {
                "id": "warden-enemy-candle-circle",
                "a": "deity:warden",
                "b": "faction:candle-circle",
                "kind": "enemy",
                "secret": True,
            },
            # У каждого NPC есть вторая связь: правка одной связи в тесте не оставляет его
            # без связей.
            {
                "id": "marta-worships-warden",
                "a": "npc:marta",
                "b": "deity:warden",
                "kind": "worships",
                "mutual": False,
            },
            {
                "id": "fabian-seeks-crypt",
                "a": "npc:fabian",
                "b": "location:crypt",
                "kind": "seeks",
                "note": "ищет казну графа",
                "mutual": False,
            },
        ],
        "villain": "npc:fabian",
    }


def custom_monster_data(**overrides: Any) -> dict[str, Any]:
    """«Сырые» данные корректного кастомного статблока: шесть характеристик, укус и мультиатака."""
    base: dict[str, Any] = {
        "id": "crypt-rat",
        "name": "Склепная крыса",
        "name_en": "Crypt Rat",
        "cr": 0.25,
        "xp": 50,
        "ac": 12,
        "hp": 9,
        "hp_dice": "2d6+2",
        "speed": 30,
        "scores": {"str": 8, "dex": 14, "con": 12, "int": 2, "wis": 10, "cha": 4},
        "actions": [
            {
                "id": "bite",
                "name": "Укус",
                "kind": "attack",
                "attack_bonus": 4,
                "damage": "1d4+2",
                "damage_type": "piercing",
            },
            {
                "id": "multiattack",
                "name": "Мультиатака",
                "kind": "multiattack",
                "multiattack": ["bite", "bite"],
            },
            {
                "id": "screech",
                "name": "Визг",
                "kind": "save",
                "save": "wis",
                "dc": 11,
                "condition": "frightened",
            },
        ],
    }
    return base | overrides


def make_adventure(**overrides: Any) -> Adventure:
    """Чистое приключение; `overrides` целиком заменяют поля верхнего уровня (`locations=...`)."""
    unknown = overrides.keys() - Adventure.model_fields.keys()
    if unknown:
        # Опечатка в имени поля молча превратила бы тест в проверку значений по умолчанию.
        raise TypeError(f"у Adventure нет полей {sorted(unknown)}")
    return Adventure.model_validate(adventure_data() | overrides)
