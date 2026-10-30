"""Исполнитель инструментов хода мастера.

`DmToolExecutor` — то, что провайдер зовёт на каждый вызов инструмента моделью (`ToolExecutor`):
вызов идёт в реестр движка на рабочей копии игры, а модель получает JSON-текст результата и
признак ошибки. Числа и броски считает движок; исполнитель только переводит `ToolOutcome` в
текст и стережёт цикл инструментов.

Что считается ошибкой для модели (`is_error=True`, текст `{"error": ...}`, по нему модель
исправляется сама):
- имя вне списка хода (`schemas()`) — адаптеры и внутренний API передают любое имя;
- аргументы не по схеме и `ToolError` обработчика — это уже решает реестр;
- повтор: больше `MAX_IDENTICAL_CALLS` одинаковых вызовов подряд. Отклонённый повтор до
  инструмента не доходит: ни бросков, ни изменений состояния.

Ошибка движка (баг, не ошибка модели) не перехватывается: исключение уходит из `run_turn`, и
оркестратор откатывает ход целиком — ход не должен выглядеть удавшимся.

Каждый вызов, в том числе отклонённый, пишется в `trace` через `make_trace_call`: пределы записи
общие с адаптерами, а модель всегда получает полный текст результата.
"""

import json
import logging
from typing import Any

from dnd_engine.tools.registry import MAX_NAME_CHARS, ToolContext, ToolOutcome, ToolRegistry
from dnd_server.llm.base import ToolCallTrace, ToolSchema, make_trace_call

logger = logging.getLogger(__name__)

# Одинаковых вызовов подряд (то же имя и те же аргументы), которые ещё выполняются; следующий
# отклоняется. Защита от зацикливания модели и лишних трат.
MAX_IDENTICAL_CALLS = 3


def _error_text(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _outcome_text(outcome: ToolOutcome) -> tuple[str, bool]:
    if outcome.error is not None:
        return _error_text(outcome.error), True
    return json.dumps(outcome.result, ensure_ascii=False), False


class DmToolExecutor:
    """Исполнитель одного хода мастера: рабочая копия игры (`ctx`) и реестр инструментов.

    Один исполнитель на один ход: `trace` и счёт повторов принадлежат ходу. Список инструментов
    хода (`schemas()`) фиксируется при создании: модель знает только его."""

    def __init__(self, ctx: ToolContext, registry: ToolRegistry) -> None:
        self._ctx = ctx
        self._registry = registry
        self._schemas = tuple(
            ToolSchema(spec.name, spec.description, spec.json_schema()) for spec in registry.specs()
        )
        self._tool_names = frozenset(schema.name for schema in self._schemas)
        self.trace: list[ToolCallTrace] = []
        self._last_call: tuple[str, str] | None = None
        self._identical_calls = 0

    def schemas(self) -> tuple[ToolSchema, ...]:
        """Инструменты хода для провайдера: имя, описание и схема аргументов из реестра."""
        return self._schemas

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        # Имя выбирает модель: в тексты ошибок и журнал оно попадает только обрезанным.
        shown_name = name[:MAX_NAME_CHARS]
        if self._count_identical(name, args) > MAX_IDENTICAL_CALLS:
            text, is_error = self._refuse_repeat(shown_name), True
        elif name not in self._tool_names:
            text, is_error = self._refuse_unknown(shown_name), True
        else:
            text, is_error = _outcome_text(self._registry.call(self._ctx, name, args))
        self.trace.append(make_trace_call(name, args, text, is_error))
        return text, is_error

    def _count_identical(self, name: str, args: dict[str, Any]) -> int:
        """Сколько одинаковых вызовов подряд, считая текущий; другой вызов начинает счёт заново.

        Порядок ключей аргументов не важен. Отклонённые повторы тоже идут в счёт: модель,
        упорно повторяющая вызов, не получает новых попыток после отказа."""
        call = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
        self._identical_calls = self._identical_calls + 1 if call == self._last_call else 1
        self._last_call = call
        return self._identical_calls

    def _refuse_repeat(self, shown_name: str) -> str:
        if self._identical_calls == MAX_IDENTICAL_CALLS + 1:
            # Один раз за серию и без аргументов: в них бывают тайные ходы и личные сообщения.
            logger.warning(
                "игра %s: модель повторяет вызов %r без изменений, лишние вызовы отклонены",
                self._ctx.state.game_id,
                shown_name,
            )
        return _error_text(
            f"Вызов {shown_name!r} с теми же аргументами уже выполнен {MAX_IDENTICAL_CALLS} "
            "раза подряд: повтор без изменений. Измени аргументы, вызови другой инструмент "
            "или заверши рассказ."
        )

    def _refuse_unknown(self, shown_name: str) -> str:
        known = ", ".join(schema.name for schema in self._schemas)
        return _error_text(f"Неизвестный инструмент {shown_name!r}. Доступны: {known}")
