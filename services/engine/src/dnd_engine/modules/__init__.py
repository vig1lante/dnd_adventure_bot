"""Модули правил: «Специи BG3» (`bg3_flavor`) и «Привал у костра» (`campfire`).

Приключение включает модули в `rules.modules`, а `build_ruleset` собирает из них `RuleSet`.
Таблица `MODULES` и сборка лежат здесь, а не в `base`: модули берут из `base` реестр и имена
флагов, и импорт модулей обратно в `base` замкнул бы цикл.
"""

from collections.abc import Mapping
from types import MappingProxyType

from dnd_engine.modules.base import RuleModule, RuleRegistry, RuleSet
from dnd_engine.modules.bg3_flavor import BG3_FLAVOR
from dnd_engine.modules.campfire import CAMPFIRE
from dnd_engine.state import RulesRef

# Таблица только для чтения: набор модулей не должен меняться во время игры.
MODULES: Mapping[str, RuleModule] = MappingProxyType(
    {module.id: module for module in (BG3_FLAVOR, CAMPFIRE)}
)


def build_ruleset(ref: RulesRef) -> RuleSet:
    """Набор правил из SRD и модулей приключения; неизвестный модуль — `ValueError`.

    Повтор модуля в списке игнорируется: иначе его триггеры сработали бы дважды."""
    module_ids = list(dict.fromkeys(ref.modules))
    registry = RuleRegistry()
    for module_id in module_ids:
        module = MODULES.get(module_id)
        if module is None:
            known = ", ".join(MODULES)
            raise ValueError(f"неизвестный модуль правил {module_id!r}, доступны: {known}")
        module.register(registry)
    return registry.build(module_ids)
