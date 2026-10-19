"""Основа модулей правил: реестр, куда модуль записывает свои правила, и собранный из него набор.

Модуль (`RuleModule`) в `register` вызывает методы `RuleRegistry`: добавляет хуки проверок,
триггеры, спецификации инструментов мастера, фрагменты промпта и флаги правил. `RuleRegistry.build`
отдаёт неизменяемый `RuleSet`. Движок читает из него флаги (`combat.engine`), хуки передаёт
проверкам (`checks.ability_check(..., hooks=...)`), триггеры вызывает при событиях игры, а фрагменты
попадают в промпт мастера. Сборка набора по списку модулей приключения — `modules.build_ruleset`.

Триггер — чистая функция над состоянием: получает `GameState` и данные SRD, возвращает новую
копию состояния и события; исходное состояние не меняет. Пакет не зависит от боя: бой
импортирует отсюда `RuleSet` и имена флагов, а не наоборот.
"""

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from dnd_engine.checks import CheckHook
from dnd_engine.content import SrdContent
from dnd_engine.events import Event
from dnd_engine.state import GameState

# Флаги правил, которые читает движок. Имена общие: модуль выставляет флаг, бой его читает.
POTION_BONUS_ACTION = "potion_bonus_action"  # зелье тратит бонусное действие, а не действие
SHOVE_BONUS_ACTION = "shove_bonus_action"  # толчок тратит бонусное действие
HELP_REVIVES = "help_revives"  # «Помощь» упавшему союзнику поднимает его с 1 HP
# Сколько коротких отдыхов допустимо между длинными. Проверяет инструмент мастера `short_rest`
# (B4.6) по `GameState.short_rests_since_long`: функции короткого отдыха на уровне состояния
# в движке нет, а `rest.short_rest` работает с одним персонажем и счётчика не знает.
MAX_SHORT_RESTS_PER_LONG = "max_short_rests_per_long"

TriggerEvent = Literal["on_scene_start", "on_scene_end", "on_long_rest", "on_time_advance"]
Trigger = Callable[[GameState, SrdContent], tuple[GameState, list[Event]]]


class RuleSet(BaseModel):
    """Собранный набор правил: SRD плюс подключённые модули. Неизменяем.

    Словари отдаются только для чтения, поэтому набор нельзя испортить во время игры. Глубоко его
    не скопировать (`copy.deepcopy`) и не сериализовать: в нём функции; набор каждый раз
    собирается заново из `RulesRef` через `build_ruleset`."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    modules: tuple[str, ...] = ()
    check_hooks: tuple[CheckHook, ...] = ()
    flags: Mapping[str, bool | int] = {}
    # Спецификации инструментов мастера (`ToolSpec`, B4.5): здесь они только хранятся, поэтому
    # зависимости от будущего типа нет.
    tools: tuple[Any, ...] = ()
    prompt_fragments: tuple[str, ...] = ()
    triggers: Mapping[TriggerEvent, tuple[Trigger, ...]] = {}

    @field_validator("flags", "triggers", mode="after")
    @classmethod
    def _read_only(cls, mapping: Mapping) -> Mapping:
        return MappingProxyType(dict(mapping))


# Набор без модулей: базовые правила SRD 5.2. Значение по умолчанию для функций, читающих флаги.
NO_RULES = RuleSet()


class RuleRegistry:
    """Сюда модули записывают свои правила; `build` собирает из них неизменяемый `RuleSet`."""

    def __init__(self) -> None:
        self._check_hooks: list[CheckHook] = []
        self._triggers: dict[TriggerEvent, list[Trigger]] = {}
        self._tools: list[Any] = []
        self._prompt_fragments: list[str] = []
        self._flags: dict[str, bool | int] = {}

    def add_check_hook(self, hook: CheckHook) -> None:
        self._check_hooks.append(hook)

    def add_trigger(self, event: TriggerEvent, fn: Trigger) -> None:
        self._triggers.setdefault(event, []).append(fn)

    def add_tool(self, spec: Any) -> None:
        self._tools.append(spec)

    def add_prompt_fragment(self, text: str) -> None:
        self._prompt_fragments.append(text)

    def set_flag(self, name: str, value: bool | int) -> None:
        # Два модуля с разными значениями одного флага дали бы результат, зависящий от порядка.
        if name in self._flags and self._flags[name] != value:
            raise ValueError(f"флаг {name!r} уже задан значением {self._flags[name]!r}")
        self._flags[name] = value

    def build(self, modules: Sequence[str]) -> RuleSet:
        """Набор из всего, что зарегистрировано; дальнейшие вызовы реестра на него не влияют."""
        return RuleSet(
            modules=tuple(modules),
            check_hooks=tuple(self._check_hooks),
            flags=self._flags,
            tools=tuple(self._tools),
            prompt_fragments=tuple(self._prompt_fragments),
            triggers={event: tuple(fns) for event, fns in self._triggers.items()},
        )


class RuleModule(Protocol):
    """Модуль правил: `id` из `adventure.yaml`, название и регистрация своих правил."""

    id: str
    name: str

    def register(self, registry: RuleRegistry) -> None: ...
