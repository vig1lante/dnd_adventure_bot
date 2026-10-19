"""Инструменты мастера: реестр (`registry`), лор (`lore`), мир (`world`), проверки (`checks`),
бой (`combat`) и рассказ (`narrative`); `build_registry` собирает из них реестр игры.

Инструменты модулей правил (`grant_inspiration`, `start_camp`) лежат в самих модулях и приходят
через `RuleSet.tools`: без модуля нет и его инструмента.
"""

from dnd_engine.modules.base import RuleSet
from dnd_engine.tools.checks import CHECK_TOOLS
from dnd_engine.tools.combat import COMBAT_TOOLS
from dnd_engine.tools.lore import LORE_TOOLS
from dnd_engine.tools.narrative import NARRATIVE_TOOLS
from dnd_engine.tools.registry import ToolRegistry
from dnd_engine.tools.world import WORLD_TOOLS


def build_registry(ruleset: RuleSet) -> ToolRegistry:
    """Реестр инструментов игры: базовые, затем инструменты включённых модулей правил.

    Повтор имени инструмента — `ValueError` (ошибка сборки, а не модели)."""
    registry = ToolRegistry()
    for spec in (
        *LORE_TOOLS,
        *WORLD_TOOLS,
        *CHECK_TOOLS,
        *COMBAT_TOOLS,
        *NARRATIVE_TOOLS,
        *ruleset.tools,
    ):
        registry.register(spec)
    return registry
