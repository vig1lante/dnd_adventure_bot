"""Генератор приключений: полный проход шагов, проверка и круги исправления (план DND-2 B3.1,
спека §9; основа — генератор DND-1, план B5.6).

`generate(provider, draft, progress)` проходит по порядку все пустые шаги черновика
(`steps.run_step`), собирает приключение (`draft.to_adventure`) и проверяет его (`check`:
валидатор движка и применимость находок тайн мест). Блокирующие ошибки возвращаются шагу-владельцу
(`owner_step`): шаг повторяется с отчётом об ошибках в промпте (`steps.repair_step`), приключение
пересобирается и проверяется снова. Кругов исправления не больше `MAX_REPAIR_ROUNDS`;
предупреждения круг не запускают.

Нейронка рассказывает, код считает: модель выбирает id, тексты и монстров, а связи между шагами,
сборку и проверку делает код. Результат шагов — недоверенный ввод: схема режет его по форме,
валидатор — по смыслу. Закреплённое автором не исправляется: его проблемы чинит только тот шаг,
который может обойтись без правки закреплённого (создать недостающую цель ссылки, провести выход,
перенести ключ), а остальные остаются в отчёте.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from dnd_engine.adventure.schema import Adventure, Location, RevealRef
from dnd_engine.adventure.validator import MAX_MESSAGE_LENGTH, Issue, Report, validate
from dnd_engine.content import SrdContent, load_srd, printable, quoted
from dnd_server.authoring.draft import STEPS, AuthoringDraft, to_adventure
from dnd_server.authoring.steps import is_empty, repair_step, run_step
from dnd_server.llm.base import LLMProvider

MAX_REPAIR_ROUNDS = 3  # круги исправления после первой сборки (спека §9, инвариант «з»)

# Проблема генератора сверх валидатора (решение dispatcher B3): находка тайны места, которую мастер
# не сможет выдать — улика или выход другой локации, предмет, который лежит не здесь.
INAPPLICABLE_SECRET_REVEAL = "inapplicable_secret_reveal"

_STEP_BY_CODE = {
    "unreachable_location": "locations",
    "no_path_to_finale": "locations",
    "empty_read_aloud": "location_contents",
    "no_features": "location_contents",
    INAPPLICABLE_SECRET_REVEAL: "location_contents",
    "npc_without_motive": "npcs",
    "npc_without_relations": "relations",
    "unknown_monster": "encounters",
    "encounter_too_hard": "encounters",
    "encounter_too_easy": "encounters",
    "boss_too_easy": "encounters",
    # Ключ переносит шаг предметов: он держит ключи и не трогает граф с остальными шагами.
    "unreachable_key": "items",
    "too_few_clues": "clues",
    "clues_same_place": "clues",
}
# Владелец объекта из `ref` проблемы (`dangling_ref`, `schema`): вложенная часть важнее сущности.
_STEP_BY_PART = {
    "exit": "locations",
    "feature": "location_contents",
    "secret": "location_contents",
    "zone": "location_contents",
    "action": "encounters",
    "clue": "clues",
}
_STEP_BY_KIND = {
    "region": "world",
    "deity": "pantheon",
    "faction": "factions",
    "location": "location_contents",
    "npc": "npcs",
    "relation": "relations",
    "encounter": "encounters",
    "monster": "encounters",
    "item": "items",
    "revelation": "clues",
}
_STEP_BY_ADVENTURE_FIELD = {
    "start_location": "locations",
    "finale_location": "locations",
    "villain": "plot",
}
# Шаг, который создаёт цель ссылки по её id. Предмет SRD, выход и место ссылкой не создаются.
_CREATOR_BY_TARGET = {
    "deity": "pantheon",
    "faction": "factions",
    "location": "locations",
    "npc": "npcs",
    "encounter": "encounters",
    "monster": "encounters",
    "item": "items",
    "clue": "clues",
}
# Поле и цель ссылки в тексте `dangling_ref` валидатора движка: «поле: ссылка 'вид:id' никуда не
# ведёт». Формат закреплён seam-тестом на настоящем валидаторе. Id с апострофом `repr` берёт в
# двойные кавычки, поэтому кавычка любая.
_DANGLING = re.compile(r"([\w.]+): ссылка ['\"](\w+):")
# Ключ замка в тексте `unreachable_key` валидатора: «locked_by: ключ '<id>' …» (тот же seam-тест).
_LOST_KEY = re.compile(r"locked_by: ключ ['\"]([^'\"]+)['\"]")
# Предмет тайника не в той локации в тексте `dangling_ref` валидатора: «reveals: предмет '<id>'
# должен лежать в локации места …» (тот же seam-тест).
_MISPLACED_HIDDEN_ITEM = re.compile(r"reveals: предмет ['\"]([^'\"]+)['\"] должен лежать")
# Неприменимая находка тайны в тексте `INAPPLICABLE_SECRET_REVEAL` (этот модуль, `_misplaced`).
_MISPLACED_SECRET_ITEM = re.compile(r"reveals: предмет ['\"]([^'\"]+)['\"]")
_MISPLACED_SECRET_CLUE = re.compile(
    r"reveals: улика ['\"][^'\"]+['\"] открытия ['\"]([^'\"]+)['\"]"
)
# Другие локации с тем же id скрытого выхода в тексте `schema` валидатора: «в локациях <id>, <id>:».
_HIDDEN_EXIT_CLASH = re.compile(
    r"hidden_dc: id скрытого выхода .* есть и у выходов в локациях ([^:]+):"
)


@dataclass(frozen=True)
class GenerationResult:
    draft: AuthoringDraft
    adventure: Adventure
    report: Report
    repair_rounds: int


async def generate(
    provider: LLMProvider,
    draft: AuthoringDraft,
    progress: Callable[[str], None] | None = None,
    *,
    content: SrdContent | None = None,
) -> GenerationResult:
    """Проходит пустые шаги, собирает приключение и ведёт круги исправления; на диск не пишет.

    Ошибки провайдера (`ProviderError`) не глотаются. Блокирующие ошибки после последнего круга
    остаются в `result.report`: решать, что с ними делать, должен вызывающий."""
    say = progress or (lambda message: None)
    srd = content or load_srd()
    for step in STEPS:
        if is_empty(draft, step) and not draft.is_locked(f"step:{step}"):
            say(f"Шаг {step}")
            draft = await run_step(provider, draft, step)
    adventure = to_adventure(draft)
    report = check(adventure, srd)
    rounds = 0
    while report.blocking and rounds < MAX_REPAIR_ROUNDS:
        targets = _repair_targets(report, draft.locks)
        if not targets:
            break  # блокирующие ошибки есть, но шага-владельца для них нет
        rounds += 1
        codes = sorted({issue.code for issues in targets.values() for issue in issues})
        say(
            f"Круг исправления {rounds} из {MAX_REPAIR_ROUNDS}: {', '.join(targets)} "
            f"({', '.join(codes)})"
        )
        for step, issues in targets.items():
            draft = await repair_step(provider, draft, step, issues)
        adventure = to_adventure(draft)
        report = check(adventure, srd)
    return GenerationResult(draft, adventure, report, rounds)


# --- проверка ---


def check(adventure: Adventure, content: SrdContent) -> Report:
    """Отчёт валидатора движка и проверки находок тайн мест (`INAPPLICABLE_SECRET_REVEAL`).

    Каждая такая находка — ошибка, и балл снижается так же, как за ошибку валидатора (на 10)."""
    report = validate(adventure, content)
    extra = _secret_reveal_issues(adventure)
    if not extra:
        return report
    return Report(
        issues=[*report.issues, *extra],
        score=max(0, report.score - 10 * len(extra)),
        estimated_minutes=report.estimated_minutes,
    )


def _secret_reveal_issues(adventure: Adventure) -> list[Issue]:
    """Находки тайн мест, которые мастер не сможет выдать в этой локации: улика лежит в другой,
    выход принадлежит другой, предмет лежит не здесь. Цель, которой нет вовсе, — `dangling_ref`
    валидатора, а не эта проблема."""
    return [
        Issue(
            code=INAPPLICABLE_SECRET_REVEAL,
            severity="error",
            ref=f"location:{location.id}/secret:{secret.id}",
            message=printable(misplaced, MAX_MESSAGE_LENGTH),
        )
        for location in adventure.locations.values()
        for secret in location.secrets
        for reveal in secret.reveals
        if (misplaced := _misplaced(reveal, location, adventure)) is not None
    ]


def _misplaced(reveal: RevealRef, location: Location, adventure: Adventure) -> str | None:
    """Почему находка тайны неприменима в `location`; применима или цели нет — `None`."""
    here = location.id
    match reveal.kind:
        case "clue":
            revelation, place = _clue_place(reveal.id, adventure)
            if place is not None and place != here:
                return (
                    f"reveals: улика {quoted(reveal.id)} открытия {quoted(revelation)} лежит в "
                    f"локации {quoted(place)}, а тайна — в {quoted(here)}"
                )
        case "exit":
            owners = [
                other.id
                for other in adventure.locations.values()
                if any(exit_.id == reveal.id for exit_ in other.exits)
            ]
            if owners and here not in owners:
                return (
                    f"reveals: выход {quoted(reveal.id)} есть только у локаций "
                    f"{', '.join(owners)}, а не у {quoted(here)}"
                )
        case "item":
            item = adventure.items.get(reveal.id)
            if item is not None and item.holder != f"location:{here}":
                return (
                    f"reveals: предмет {quoted(reveal.id)} должен лежать в локации тайны "
                    f"(holder 'location:{here}'), а лежит у {quoted(item.holder)}"
                )
    return None


def _clue_place(clue_id: str, adventure: Adventure) -> tuple[str, str | None]:
    """(открытие, локация), где игроки найдут улику: место, NPC или держатель предмета.
    Улики нет или её место неизвестно — локация `None`."""
    revelation, where = next(
        (
            (revelation.id, clue.where)
            for revelation in adventure.revelations.values()
            for clue in revelation.clues
            if clue.id == clue_id
        ),
        ("", ""),
    )
    return revelation, _where_location(where, adventure)


def _where_location(where: str, adventure: Adventure) -> str | None:
    """Локация места улики `where` (`feature:`, `npc:`, `item:`); неизвестно — `None`."""
    kind, _, ident = where.partition(":")
    if kind == "feature":
        return next(
            (
                location.id
                for location in adventure.locations.values()
                if any(feature.id == ident for feature in location.features)
            ),
            None,
        )
    if kind == "item" and (item := adventure.items.get(ident)) is not None:
        # Улика в предмете лежит там же, где предмет: в локации или у NPC (тогда — ниже).
        kind, _, ident = item.holder.partition(":")
        if kind == "location":
            return ident
    if kind == "npc" and (npc := adventure.npcs.get(ident)) is not None:
        return npc.location_id
    return None


# --- маршрут проблем к шагам ---


def owner_step(issue: Issue, locks: Iterable[str] = ()) -> str | None:
    """Шаг, который отвечает за проблему проверки; `None` — чинить нечем.

    Код проблемы называет шаг сам (`_STEP_BY_CODE`: граф, наполнение, бои, ключи, улики…).
    Остальные коды (`dangling_ref`, `schema`) идут владельцу объекта из `ref`: выход — locations,
    место и тайна места — location_contents, бог — pantheon, связь графа — relations, край — world
    и т. д.; у `adventure` — по полю ссылки (старт и финал — locations, злодей — plot). Ссылку
    вперёд, цель которой создаёт более поздний шаг (злодей-NPC, лидер фракции, предмет тайника,
    свой статблок), чинит этот шаг: он её и создаст.

    Закреплённое правит автор, а не модель: из его проблем чинится только то, что можно исправить,
    не меняя его (`_locked_owner`). Проблема, чей шаг-владелец закреплён целиком, тоже остаётся
    в отчёте."""
    locks = set(locks)
    kind, _, rest = issue.ref.partition(":")
    if f"{kind}:{rest.partition('/')[0]}" in locks:
        step = _locked_owner(issue, locks)
    else:
        step = _owner(issue)
    return None if step is None or f"step:{step}" in locks else step


def _owner(issue: Issue) -> str | None:
    if issue.code in _STEP_BY_CODE:
        return _STEP_BY_CODE[issue.code]
    found = _DANGLING.match(issue.message) if issue.code == "dangling_ref" else None
    kind, _, rest = issue.ref.partition(":")
    if kind == "adventure":
        owner = _STEP_BY_ADVENTURE_FIELD.get(found.group(1)) if found else None
    else:
        part = rest.partition("/")[2].partition(":")[0]
        owner = _STEP_BY_PART.get(part) or _STEP_BY_KIND.get(kind)
    creator = _CREATOR_BY_TARGET.get(found.group(2)) if found else None
    if owner is not None and creator is not None and STEPS.index(creator) > STEPS.index(owner):
        return creator
    return owner


def _locked_owner(issue: Issue, locks: set[str]) -> str | None:
    """Шаг, который исправит проблему закреплённого, не меняя его; `None` — проблема в отчёте."""
    if issue.code == INAPPLICABLE_SECRET_REVEAL:
        return _locked_reveal_owner(issue.message, locks)
    match issue.code:
        case "unreachable_location":
            return "locations"  # граф проводит выходы к закреплённым локациям
        case "npc_without_relations":
            return "relations"  # связь — отдельная сущность графа, NPC она не меняет
        case "unreachable_key":
            # Замок цел, а ключ, который создала модель, переносит шаг items; закреплённый — нет.
            key = _LOST_KEY.match(issue.message)
            return "items" if key and f"item:{key.group(1)}" not in locks else None
        case "schema":
            # Скрытый выход делит id с выходом другой локации: чужой выход переименует граф.
            clash = _HIDDEN_EXIT_CLASH.match(issue.message)
            others = clash.group(1).split(", ") if clash else []
            return "locations" if any(f"location:{o}" not in locks for o in others) else None
        case "dangling_ref":
            # Предмет тайника закреплённого места лежит не там: переносит шаг items.
            hidden = _MISPLACED_HIDDEN_ITEM.match(issue.message)
            if hidden is not None:
                return "items" if f"item:{hidden.group(1)}" not in locks else None
            # Ссылку закреплённого в пустоту чинит шаг, который создаёт цель.
            found = _DANGLING.match(issue.message)
            return _CREATOR_BY_TARGET.get(found.group(2)) if found else None
    return None


def _locked_reveal_owner(message: str, locks: set[str]) -> str | None:
    """Неприменимая находка тайны закреплённой локации: незакреплённый предмет переносит шаг
    items, улику незакреплённого открытия — шаг clues; выход чужой локации исправить нечем."""
    if item := _MISPLACED_SECRET_ITEM.match(message):
        return "items" if f"item:{item.group(1)}" not in locks else None
    if clue := _MISPLACED_SECRET_CLUE.match(message):
        return "clues" if f"revelation:{clue.group(1)}" not in locks else None
    return None


def _repair_targets(report: Report, locks: Iterable[str]) -> dict[str, list[Issue]]:
    """Шаги с блокирующей ошибкой и все их проблемы (ошибки и предупреждения) в порядке шагов:
    поздний шаг видит уже исправленный ранний. После исправления графа в тот же круг идёт
    наполнение локаций: новые локации графа без него пусты."""
    by_step: dict[str, list[Issue]] = {}
    for issue in report.issues:
        step = owner_step(issue, locks)
        if step is not None:
            by_step.setdefault(step, []).append(issue)
    failing = {
        step for step, issues in by_step.items() if any(i.severity == "error" for i in issues)
    }
    if "locations" in failing and "location_contents" not in failing:
        by_step["location_contents"] = []
        failing.add("location_contents")
    return {step: by_step[step] for step in STEPS if step in failing}
