"""Модуль «Специи BG3»: правила в духе Baldur's Gate 3 поверх SRD 5.2.

Флаги правил читает бой: зелье и толчок — бонусные действия, «Помощь» поднимает упавшего
союзника (`combat.engine.simple_action`). Хук проверок делает натуральные 20 и 1 автоматическим
успехом и провалом. Вдохновение за предысторию выдаёт мастер инструментом `grant_inspiration`:
сам инструмент появится в B4.7, а здесь правило описано для промпта мастера словами.
"""

from dnd_engine.checks import CheckKind, CheckResult
from dnd_engine.modules.base import (
    HELP_REVIVES,
    MAX_SHORT_RESTS_PER_LONG,
    POTION_BONUS_ACTION,
    SHOVE_BONUS_ACTION,
    RuleRegistry,
)

NATURAL_MAX, NATURAL_MIN = 20, 1
SHORT_RESTS_PER_LONG = 2

RULES_FRAGMENT = """\
## Правила «Специй BG3»

- Выпить зелье (себе или союзнику) — бонусное действие, а не действие.
- Толчок — тоже бонусное действие.
- Натуральная 20 на проверке характеристики или навыка — автоматический успех, натуральная 1 —
  автоматический провал, какой бы ни была Сл. На спасброски это не распространяется. Исход
  определяет инструмент проверки: расскажи то, что он вернул, и не объявляй исход сам.
- «Помощь» упавшему союзнику (0 HP) поднимает его с 1 HP: он приходит в себя.
- Между двумя длинными отдыхами — не больше двух коротких.
"""

INSPIRATION_FRAGMENT = """\
## Вдохновение за предысторию

У каждой предыстории есть 2–3 повода для отыгрыша. Когда игрок ярко отыгрывает такой повод,
выдай вдохновение инструментом grant_inspiration и коротко скажи об этом в рассказе. Не чаще
одного раза на персонажа за сцену: инструмент откажет, если в этой сцене вдохновение ему уже
выдавали.

Игрок тратит вдохновение вместе с ходом (галочка «Вдохновение»). Тогда первый бросок к20 при
разборе этого хода идёт с преимуществом. Преимущество применяет движок: сам ничего не бросай.
"""


def natural_20_and_1(result: CheckResult) -> CheckResult:
    """Хук проверок: 20 на кубе — успех, 1 — провал, что бы ни показал итог. Спасброски не трогает.

    Смотрит на оставленный куб (`roll.natural`), поэтому при преимуществе и помехе работает по
    выбранному кубу. Итог на кубах не подменяется: меняются только `success` и `auto`."""
    if result.kind == CheckKind.SAVE:
        return result
    if result.roll.natural == NATURAL_MAX:
        return result.model_copy(update={"success": True, "auto": "nat20"})
    if result.roll.natural == NATURAL_MIN:
        return result.model_copy(update={"success": False, "auto": "nat1"})
    return result


class Bg3Flavor:
    id = "bg3_flavor"
    name = "Специи BG3"

    def register(self, registry: RuleRegistry) -> None:
        registry.set_flag(POTION_BONUS_ACTION, True)
        registry.set_flag(SHOVE_BONUS_ACTION, True)
        registry.set_flag(HELP_REVIVES, True)
        registry.set_flag(MAX_SHORT_RESTS_PER_LONG, SHORT_RESTS_PER_LONG)
        registry.add_check_hook(natural_20_and_1)
        registry.add_prompt_fragment(RULES_FRAGMENT)
        registry.add_prompt_fragment(INSPIRATION_FRAGMENT)


BG3_FLAVOR = Bg3Flavor()
