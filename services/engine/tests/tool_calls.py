"""Образцовые вызовы инструментов мастера: по одному удачному на каждый инструмент реестра.

Общая таблица для тестов реестра (`test_tools_contract`) и случайных вызовов
(`test_tools_random_calls`). Вызовы рассчитаны на отряд из фикстуры `make_scene` (`conftest.py`):
воины `pc-1` и `pc-2`, маг `wiz`, ключ сундука у `pc-1`, отряд на кладбище. Поле `scene` говорит,
с какими параметрами фикстуры вызов удаётся; бой — «Встающие кости», первым ходит герой вызова.
"""

from typing import Any, NamedTuple

from dnd_engine.state import RulesRef

HERO, ALLY, WIZARD = "pc-1", "pc-2", "wiz"
BOTH_MODULES = RulesRef(modules=["bg3_flavor", "campfire"])  # правила демо-приключения


class ValidCall(NamedTuple):
    tool: str
    args: dict[str, Any]
    scene: dict[str, Any] = {}  # параметры фикстуры `make_scene`; пусто — исследование на кладбище


FIGHT = {"fight": True, "engaged": True}
CONTEST = {
    "a_id": HERO,
    "a_skill": "athletics",
    "b_id": ALLY,
    "b_skill": "acrobatics",
    "reason": "т",
}

VALID_CALLS = [
    ValidCall("get_location", {}),
    ValidCall("get_npc", {"npc_id": "marta"}),
    ValidCall("get_item", {"item_id": "chest-key"}),
    ValidCall("search_lore", {"query": "долг"}),
    ValidCall("move_party", {"location_id": "village"}),
    ValidCall("reveal", {"ref": "feature:diary"}),
    # Ключ уже у Брона: зелье из закрытого сундука так не отдать, а ключ передаётся напарнику.
    ValidCall("give_item", {"item_id": "chest-key", "character_id": ALLY}),
    ValidCall("take_item", {"item_id": "chest-key", "character_id": HERO, "to": "gone"}),
    ValidCall("set_flag", {"name": "met_marta"}),
    ValidCall("change_attitude", {"npc_id": "marta", "delta": 1}),
    ValidCall("advance_time", {"to": "evening"}),
    ValidCall("short_rest", {"spend": {HERO: 1}}),
    ValidCall("request_check", {"character_id": HERO, "ability": "dex", "dc": 15, "reason": "т"}),
    ValidCall("request_save", {"character_id": HERO, "ability": "wis", "dc": 15, "reason": "т"}),
    ValidCall("contest", CONTEST),
    ValidCall("private_message", {"character_id": HERO, "text": "Ты слышишь шёпот."}),
    ValidCall("chronicle", {"entry": "Отряд пришёл на кладбище."}),
    ValidCall("heal", {"character_id": HERO, "dice": "2d4+2", "reason": "Ручей"}),
    ValidCall(
        "damage", {"character_id": HERO, "dice": "2d6", "damage_type": "fire", "reason": "т"}
    ),
    # Срок в раундах накладывается только в бою: вне боя раунды не идут.
    ValidCall(
        "apply_effect", {"target_id": HERO, "condition": "prone", "rounds": 2}, {"fight": True}
    ),
    ValidCall("grant_inspiration", {"character_id": HERO, "reason": "Сыграл честь солдата"}),
    ValidCall("start_camp", {"reason": "Отряд устал"}),
    ValidCall("end_scene", {"next": "social", "reason": "Начался разговор"}),
    ValidCall("start_combat", {"monsters": [{"monster_id": "zombie"}]}),
    ValidCall("attack", {"actor_id": HERO, "target_id": "skeleton-1", "weapon": "mace"}, FIGHT),
    ValidCall(
        "cast_spell",
        {"actor_id": WIZARD, "spell_id": "fire_bolt", "target_ids": ["skeleton-1"]},
        {"first": WIZARD, "fight": True},
    ),
    ValidCall("move", {"actor_id": HERO, "to_zone": "gate"}, FIGHT),
    ValidCall("combat_action", {"actor_id": HERO, "action": "dodge"}, FIGHT),
    ValidCall(
        "monster_action",
        {"monster_id": "skeleton-1", "action_id": "shortbow", "target_ids": [HERO]},
        {"party": (HERO,), "fight": True},
    ),
    ValidCall("end_scene", {"next": "exploration", "reason": "Нежить повержена"}, FIGHT),
]
