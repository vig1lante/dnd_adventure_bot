"""Валидатор приключения (формат v2): блокирующие ошибки,
предупреждения, балл и оценка времени.

Схема (`schema`) проверяет форму данных, а этот модуль — смысл: ссылки между сущностями (в том
числе концы связей графа, лидеры и базы фракций, аватары богов, злодей и находки тайн мест),
уникальность id вложенных сущностей (места, улики, выходы, зоны, связи, края, тайны мест) и
предметов (свои не совпадают с предметами SRD), разные концы связи, держателей предметов, которые
кладут находки, достижимость локаций и ключей замков-предметов, улики открытий, кастомные статблоки
(всё, что бой читает из них: числа, записи костей в пределах, обязательные поля действий, типы урона
и состояния SRD) и сложность боёв (бой с боссом — не легче «высокой»). Это чистая функция:
она читает приключение и данные SRD, ничего не меняет, не касается файлов и сети и не бросает
исключений на корректно собранном `Adventure`. Тексты приключения для неё — данные: в сообщения
они попадают только как короткие id и ссылки, а `ref` и `message` каждой проблемы — одна печатная
строка ограниченной длины (`_issue`): отчёт идёт в журнал сервера, ответ создателю и терминал.
"""

from collections import deque
from collections.abc import Collection, Container, Iterable, Iterator
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from dnd_engine.abilities import Ability
from dnd_engine.adventure.budget import (
    XP_BUDGET_PER_CHARACTER,
    encounter_rating,
    encounter_xp,
    find_monster,
    party_budget,
)
from dnd_engine.adventure.schema import (
    COLLECTIONS,
    REF_KINDS,
    Adventure,
    Clue,
    Feature,
    Item,
    RevealRef,
)
from dnd_engine.content import (
    MonsterAction,
    MonsterDef,
    SrdContent,
    _unknown_ids,
    printable,
    quoted,
)
from dnd_engine.dice import DiceSyntaxError, parse
from dnd_engine.modules import MODULES

# Меньше трёх улик у открытия игроки легко упускают: одна неудачная проверка — и вывод недоступен.
MIN_CLUES = 3
# Бой, где монстров больше двадцати, не помещается в зоны и очередь хода (шесть героев и ещё
# двадцать бойцов); без предела модель-генератор может задать count в миллиарды.
MAX_MONSTERS_PER_ENCOUNTER = 20
# Пределы чисел кастомного статблока. Самые тяжёлые монстры SRD — несколько сотен HP, КД до 25,
# характеристики от 1 до 30, бонус атаки, спасброска и навыка — до +20, Сл спасброска монстра лежит
# в шкале Сл от 5 до 30. Границы с запасом: они отсекают заведомо нелепые числа генератора, а не
# балансируют бой. Главное другое: число в тысячи цифр роняет и бой (`f"{bonus:+d}"`, JSON), и
# сообщение об ошибке (`str` бросает ValueError).
MONSTER_HP_LIMITS = (1, 1000)
AC_LIMITS = (1, 30)
ABILITY_SCORE_LIMITS = (1, 30)
BONUS_LIMITS = (-5, 20)  # бонус атаки, спасброска и навыка
SAVE_DC_LIMITS = (5, 30)
# Пределы записи костей статблока (`hp_dice`, урон). `dice.parse` принимает любое целое до 4300
# цифр, а итог такой записи не сериализуется в журнал хода; модификатор в десятки цифр — нелепый
# урон. Потолок SRD — +24 (8d10+24) и один терм с костями. Длина проверяется до разбора: запись
# в мегабайт не тратит время, а плоские числа (`+1-1+1…`), которые `parse` не считает термами с
# костями, тоже ограничены.
MAX_DICE_CHARS = 40
MAX_DICE_TERMS = 4  # термов с костями
MAX_DICE_MODIFIER = 100  # |итоговый плоский модификатор|
# Целей одного действия не больше, чем модель может назвать в вызове `monster_action`.
MAX_ACTION_TARGETS = 8
TARGETS_LIMITS = (1, MAX_ACTION_TARGETS)
# Типы урона SRD 5.2. Отдельной таблицы типов в данных SRD нет (они встречаются в оружии,
# заклинаниях и ударах монстров), поэтому список задан здесь; что он покрывает данные SRD и
# совпадает со списком инструментов мастера, проверяет тест. Состояния берутся из данных SRD:
# `content.conditions`.
DAMAGE_TYPES = frozenset(
    {
        "acid",
        "bludgeoning",
        "cold",
        "fire",
        "force",
        "lightning",
        "necrotic",
        "piercing",
        "poison",
        "psychic",
        "radiant",
        "slashing",
        "thunder",
    }
)
# Оценка времени игры считается отличающейся от цели, когда расходится больше чем на четверть.
DURATION_TOLERANCE = 0.25
# Длина сообщения ограничена: в него попадают id из файлов приключения, а длина id не задана.
# Предел считается после экранирования и включает «…» на конце.
MAX_MESSAGE_LENGTH = 200
# Длина ссылки `ref` ограничена по той же причине: id зоны и id действия монстра шаблона не имеют.
# Самая длинная ссылка из id по шаблону (до 64 символов) — `location:<id>/feature:<id>`, 146
# символов, поэтому такие ссылки предел не режет.
MAX_REF_LENGTH = 160

# Флаги с этими префиксами ведёт движок (ход боя: `flags["encounter:<id>"]`): ни модель
# (`set_flag`), ни находка приключения не должны подделывать прогресс боя.
ENGINE_FLAG_PREFIXES = ("encounter:",)

# Вид ссылки, у которой проверять нечего: имена флагов придумывает автор приключения.
_FREE_KINDS = ("flag",)


class Issue(BaseModel):
    """Одна проблема приключения. `ref` — объект, где она найдена: `location:village`,
    `location:village/exit:to-crypt`, `encounter:bones`, `adventure`."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Literal["error", "warning"]
    ref: str
    message: str


class Report(BaseModel):
    """Итог проверки. `score` = 100 − 10·ошибки − 3·предупреждения, не меньше нуля."""

    model_config = ConfigDict(frozen=True)

    issues: list[Issue]
    score: int = Field(ge=0, le=100)
    estimated_minutes: int = Field(ge=0)

    @property
    def blocking(self) -> bool:
        """Есть ли ошибка: игру на таком приключении создавать нельзя."""
        return any(issue.severity == "error" for issue in self.issues)


def _issue(severity: Literal["error", "warning"], code: str, ref: str, message: str) -> Issue:
    # Единственное место, где проблема становится `Issue`. Id без шаблона (зона, действие монстра)
    # и чужие значения приходят сюда как есть, а отчёт читают журнал сервера, ответ создателю и
    # отчёт генератора: ссылка и сообщение — одна печатная строка ограниченной длины.
    return Issue(
        code=code,
        severity=severity,
        ref=printable(ref, MAX_REF_LENGTH),
        message=printable(message, MAX_MESSAGE_LENGTH),
    )


def _error(code: str, ref: str, message: str) -> Issue:
    return _issue("error", code, ref, message)


def _warning(code: str, ref: str, message: str) -> Issue:
    return _issue("warning", code, ref, message)


def _shown(value: int) -> str:
    """Число для сообщения. Число в тысячи цифр из файла вывести нельзя (`str` бросает
    ValueError), а валидатор исключений не бросает."""
    return str(value) if abs(value) < 10**15 else "слишком большое число"


# --- настройки приключения и кастомные статблоки ---


def _check_settings(adventure: Adventure) -> list[Issue]:
    """Модули правил должны существовать, а уровень партии — иметь бюджет боёв."""
    issues = [
        _error(
            "schema",
            "adventure",
            f"rules.modules: неизвестный модуль правил {quoted(module)}, "
            f"доступны: {', '.join(MODULES)}",
        )
        for module in dict.fromkeys(adventure.rules.modules)
        if module not in MODULES
    ]
    if adventure.party_level not in XP_BUDGET_PER_CHARACTER:
        first, last = min(XP_BUDGET_PER_CHARACTER), max(XP_BUDGET_PER_CHARACTER)
        issues.append(
            _error(
                "schema",
                "adventure",
                f"party_level: уровень {_shown(adventure.party_level)} вне {first}–{last}, "
                "для него не задан бюджет боёв",
            )
        )
    return issues


def _dice_problems(field: str, expr: str) -> list[str]:
    """Запись костей читает тот же `dice.parse`, что и бой: что он отверг, в бою упало бы. Сверх
    разбора — пределы `MAX_DICE_*`: число в тысячи цифр разбор принимает, а бой не сериализует."""
    if len(expr) > MAX_DICE_CHARS:
        return [f"{field}: запись костей длиннее {MAX_DICE_CHARS} символов"]
    try:
        parsed = parse(expr)
    except DiceSyntaxError as exc:
        return [f"{field}: {exc}"]
    problems: list[str] = []
    if len(parsed.terms) > MAX_DICE_TERMS:
        problems.append(f"{field}: термов с костями больше {MAX_DICE_TERMS}")
    if abs(parsed.modifier) > MAX_DICE_MODIFIER:
        problems.append(f"{field}: модификатор по модулю больше {MAX_DICE_MODIFIER}")
    return problems


def _number_problems(monster: MonsterDef) -> list[str]:
    """Числа статблока, которые читает бой, лежат в своих пределах."""
    numbers = [("hp", monster.hp, MONSTER_HP_LIMITS), ("ac", monster.ac, AC_LIMITS)]
    numbers += [(f"scores.{a.value}", v, ABILITY_SCORE_LIMITS) for a, v in monster.scores.items()]
    numbers += [(f"saves.{a.value}", v, BONUS_LIMITS) for a, v in monster.saves.items()]
    numbers += [(f"skills.{s.value}", v, BONUS_LIMITS) for s, v in monster.skills.items()]
    for action in monster.actions:
        label = f"действие {quoted(action.id)}, "
        numbers.append((label + "targets", action.targets, TARGETS_LIMITS))
        if action.attack_bonus is not None:
            numbers.append((label + "attack_bonus", action.attack_bonus, BONUS_LIMITS))
        if action.dc is not None:
            numbers.append((label + "dc", action.dc, SAVE_DC_LIMITS))
    return [
        f"{field}: {_shown(value)} вне пределов {low}–{high}"
        for field, value, (low, high) in numbers
        if not low <= value <= high
    ]


def _missing_field_problems(action: MonsterAction) -> list[str]:
    """Поля, без которых действие своего вида не разыграть: бой читает их без проверки на `None`."""
    match action.kind:
        case "attack":
            needed = (
                ("attack_bonus", action.attack_bonus),
                ("damage", action.damage),
                ("damage_type", action.damage_type),
            )
        case "save":
            needed = (("save", action.save), ("dc", action.dc))
        case _:
            needed = ()
    problems = [
        f"{name}: у действия вида {action.kind} поле обязательно"
        for name, value in needed
        if value is None
    ]
    if action.kind == "save" and action.damage is not None and action.damage_type is None:
        problems.append("damage_type: у урона действия со спасброском нет типа")
    if action.kind == "multiattack" and not action.multiattack:
        problems.append("multiattack: в мультиатаке не названо ни одной атаки")
    if (
        action.kind == "attack"
        and action.condition is not None
        and (action.save is None) != (action.dc is None)
    ):
        # Без пары движок наложил бы состояние без спасброска: данные потерялись бы молча.
        given = "save" if action.save is not None else "dc"
        problems.append(
            f"condition: спасбросок от состояния задаётся парой save и dc, а задано только {given}"
        )
    return problems


def _value_problems(action: MonsterAction, conditions: Collection[str]) -> list[str]:
    """Заданные поля действия: записи костей, типы урона и состояния должны быть из SRD."""
    problems: list[str] = []
    for field, expr in (("damage", action.damage), ("secondary_damage", action.secondary_damage)):
        if expr is not None:
            problems += _dice_problems(field, expr)
    for field, damage_type in (
        ("damage_type", action.damage_type),
        ("secondary_damage_type", action.secondary_damage_type),
    ):
        if damage_type is not None and damage_type not in DAMAGE_TYPES:
            known = ", ".join(sorted(DAMAGE_TYPES))
            problems.append(
                f"{field}: неизвестный тип урона {quoted(damage_type)}; допустимы: {known}"
            )
    if action.condition is not None and action.condition not in conditions:
        known = ", ".join(sorted(conditions))
        problems.append(
            f"condition: неизвестное состояние {quoted(action.condition)}; допустимы: {known}"
        )
    return problems


def _check_custom_monster(monster: MonsterDef, conditions: Collection[str]) -> list[Issue]:
    ref = f"monster:{monster.id}"
    issues: list[Issue] = []
    missing = [ability.value for ability in Ability if ability not in monster.scores]
    if missing:
        issues.append(_error("schema", ref, f"scores: не хватает {', '.join(missing)}"))
    issues += [
        _error("schema", ref, problem)
        for problem in [*_number_problems(monster), *_dice_problems("hp_dice", monster.hp_dice)]
    ]

    action_ids = {action.id for action in monster.actions}
    issues += _repeated_id_errors(
        "действие",
        ((f"{ref}/action:{action.id}", action.id) for action in monster.actions),
        "монстр берёт первое действие с этим id, остальные недостижимы",
    )
    for action in monster.actions:
        owner = f"действие {quoted(action.id)}"
        issues += [
            _error("dangling_ref", f"{ref}/action:{action.id}", problem)
            for problem in _unknown_ids(owner, "multiattack", action.multiattack, action_ids)
        ]
        issues += [
            _error("schema", f"{ref}/action:{action.id}", problem)
            for problem in [
                *_missing_field_problems(action),
                *_value_problems(action, conditions),
            ]
        ]
    return issues


def _check_custom_monsters(adventure: Adventure, content: SrdContent) -> list[Issue]:
    """Кастомный статблок не должен ронять движок: полный набор характеристик, разумные числа,
    верные записи костей, обязательные поля действий, типы урона и состояния из SRD, мультиатака
    только из своих действий. Статблоки SRD проверяет загрузчик данных SRD."""
    return [
        issue
        for monster in adventure.monsters.values()
        for issue in _check_custom_monster(monster, content.conditions)
    ]


# --- ссылки между сущностями ---


class _Ref(NamedTuple):
    owner: str  # кто ссылается: `location:crypt/exit:to-cemetery`
    field: str  # в каком поле
    target: str  # куда: `вид:id`
    kinds: tuple[str, ...]  # какие виды целей допустимы в этом поле


def _reveal_sources(adventure: Adventure) -> Iterator[tuple[str, tuple[RevealRef, ...]]]:
    """(ref, находки) всего, что раскрывает находки: интересные места и тайны мест."""
    for location in adventure.locations.values():
        for feature in location.features:
            yield f"location:{location.id}/feature:{feature.id}", feature.reveals
        for secret in location.secrets:
            yield f"location:{location.id}/secret:{secret.id}", secret.reveals


def _references(adventure: Adventure) -> Iterator[_Ref]:
    """Все ссылки приключения, кроме `MonsterGroup.monster_id` (её проверяет `unknown_monster`)
    и зон боя (их проверяет `_check_zones`)."""
    yield _Ref("adventure", "start_location", f"location:{adventure.start_location}", ("location",))
    yield _Ref(
        "adventure", "finale_location", f"location:{adventure.finale_location}", ("location",)
    )
    if adventure.villain is not None:
        yield _Ref("adventure", "villain", adventure.villain, ("npc", "deity"))
    for location in adventure.locations.values():
        owner = f"location:{location.id}"
        if location.encounter_id is not None:
            yield _Ref(owner, "encounter_id", f"encounter:{location.encounter_id}", ("encounter",))
        for exit_ in location.exits:
            exit_owner = f"{owner}/exit:{exit_.id}"
            yield _Ref(exit_owner, "to", f"location:{exit_.to}", ("location",))
            if exit_.locked_by is not None:
                yield _Ref(exit_owner, "locked_by", exit_.locked_by, ("item", "flag"))
        for feature in location.features:
            if feature.locked_by is not None:
                feature_owner = f"{owner}/feature:{feature.id}"
                yield _Ref(feature_owner, "locked_by", feature.locked_by, ("item", "flag"))
    for source, reveals in _reveal_sources(adventure):
        for reveal in reveals:
            yield _Ref(source, "reveals", f"{reveal.kind}:{reveal.id}", (reveal.kind,))
    for npc in adventure.npcs.values():
        owner = f"npc:{npc.id}"
        yield _Ref(owner, "location_id", f"location:{npc.location_id}", ("location",))
        if npc.statblock is not None:
            yield _Ref(owner, "statblock", f"monster:{npc.statblock}", ("monster",))
    for deity in adventure.deities.values():
        if deity.avatar_statblock is not None:
            target = f"monster:{deity.avatar_statblock}"
            yield _Ref(f"deity:{deity.id}", "avatar_statblock", target, ("monster",))
    for faction in adventure.factions.values():
        owner = f"faction:{faction.id}"
        if faction.leader is not None:
            yield _Ref(owner, "leader", faction.leader, ("npc",))
        if faction.base_location is not None:
            target = f"location:{faction.base_location}"
            yield _Ref(owner, "base_location", target, ("location",))
    for relation in adventure.relations:
        for field in ("a", "b"):
            yield _Ref(f"relation:{relation.id}", field, getattr(relation, field), REF_KINDS)
    for item in adventure.items.values():
        owner = f"item:{item.id}"
        if item.base_item is not None:
            yield _Ref(owner, "base_item", f"srd_item:{item.base_item}", ("srd_item",))
        if item.holder != "gone":
            # Персонажей в приключении нет: предмет у героя не достанется никому.
            yield _Ref(owner, "holder", item.holder, ("location", "npc"))
    for encounter in adventure.encounters.values():
        owner = f"encounter:{encounter.id}"
        yield _Ref(owner, "location_id", f"location:{encounter.location_id}", ("location",))
    for revelation in adventure.revelations.values():
        for clue in revelation.clues:
            owner = f"revelation:{revelation.id}/clue:{clue.id}"
            yield _Ref(owner, "where", clue.where, ("feature", "npc", "item"))


def _known_ids(adventure: Adventure, content: SrdContent) -> dict[str, Container[str]]:
    """Какие id есть для каждого вида ссылки: монстр — свой или из SRD, `srd_item` — из SRD."""
    locations = adventure.locations.values()
    return {
        "location": adventure.locations,
        "npc": adventure.npcs,
        "item": adventure.items,
        "encounter": adventure.encounters,
        "deity": adventure.deities,
        "faction": adventure.factions,
        "monster": adventure.monsters.keys() | content.monsters.keys(),
        "srd_item": content.items,
        "feature": {feature.id for location in locations for feature in location.features},
        "exit": {exit_.id for location in locations for exit_ in location.exits},
        "clue": {clue.id for rev in adventure.revelations.values() for clue in rev.clues},
    }


def _resolves(ref: _Ref, known: dict[str, Container[str]]) -> bool:
    kind, _, ident = ref.target.partition(":")
    if kind not in ref.kinds:
        return False
    if kind in _FREE_KINDS:
        return bool(ident)
    return ident in known[kind]


def _check_references(adventure: Adventure, content: SrdContent) -> list[Issue]:
    known = _known_ids(adventure, content)
    return [
        _error(
            "dangling_ref",
            ref.owner,
            f"{ref.field}: ссылка {quoted(ref.target)} никуда не ведёт "
            f"(допустимы: {', '.join(ref.kinds)})",
        )
        for ref in _references(adventure)
        if not _resolves(ref, known)
    ]


def _check_zones(adventure: Adventure) -> list[Issue]:
    """Зоны боя — тоже ссылки: `start_combat` откажет, если связь, группа монстров или зона
    отряда указывают на зону, которой нет в локации боя. Сама локация должна иметь хотя бы одну
    зону: без неё некуда поставить отряд."""
    issues: list[Issue] = []
    for location in adventure.locations.values():
        if not location.zones:
            issues.append(
                _error(
                    "schema",
                    f"location:{location.id}",
                    "zones: у локации нет ни одной зоны, start_combat не начнёт в ней бой",
                )
            )
        zones = {zone.id for zone in location.zones}
        for link in location.links:
            issues += [
                _error("dangling_ref", f"location:{location.id}", f"links: зоны {quoted(end)} нет")
                for end in (link.a, link.b)
                if end not in zones
            ]
    for encounter in adventure.encounters.values():
        location = adventure.locations.get(encounter.location_id)
        if location is None:
            continue  # локация боя — ссылка в пустоту, её уже нашёл `_check_references`
        zones = {zone.id for zone in location.zones}
        claims = [("pc_zone", encounter.pc_zone)]
        claims += [("monsters.zone", group.zone) for group in encounter.monsters]
        issues += [
            _error("dangling_ref", f"encounter:{encounter.id}", f"{field}: зоны {quoted(zone)} нет")
            for field, zone in claims
            if zone not in zones
        ]
    return issues


def _check_location_encounters(adventure: Adventure) -> list[Issue]:
    """Бой локации должен происходить в ней: `get_location` показывает бой по `encounter_id`, а
    `start_combat` берёт зоны из локации боя и откажет, если отряд стоит в другой."""
    return [
        _error(
            "dangling_ref",
            f"location:{location.id}",
            f"encounter_id: бой {encounter.id!r} происходит в локации "
            f"{quoted(encounter.location_id)}, а не здесь: start_combat его не начнёт",
        )
        for location in adventure.locations.values()
        if location.encounter_id is not None
        and (encounter := adventure.encounters.get(location.encounter_id)) is not None
        and encounter.location_id in adventure.locations  # иначе её уже нашёл `_check_references`
        and encounter.location_id != location.id
    ]


def _check_unlinked_encounters(adventure: Adventure) -> list[Issue]:
    """Обратное направление: бой, на который его локация не ссылается через `encounter_id`.

    Мастер узнаёт бой только из `get_location`, а `start_combat` берёт его id оттуда же; бой без
    ссылки из локации в игре не появится, хотя оценка времени его уже посчитала. Заодно это
    ловит второй бой в одной локации: у неё один `encounter_id`."""
    return [
        _error(
            "dangling_ref",
            f"encounter:{encounter.id}",
            f"бой не привязан к локации {quoted(encounter.location_id)}: "
            "get_location его не покажет",
        )
        for encounter in adventure.encounters.values()
        # нет локации боя — её уже нашёл `_check_references`
        if (location := adventure.locations.get(encounter.location_id)) is not None
        and location.encounter_id != encounter.id
        # `encounter_id` локации ведёт в пустоту — это та же причина, её уже нашёл он же
        and (location.encounter_id is None or location.encounter_id in adventure.encounters)
    ]


def _check_seeds(adventure: Adventure) -> list[Issue]:
    """`seeds` — id сущностей, заданных автором: каждый должен лежать в одной из коллекций."""
    known = {key for name in COLLECTIONS for key in getattr(adventure, name)}
    return [
        _error("dangling_ref", "adventure", f"seeds: id {quoted(seed)} нет ни в одной коллекции")
        for seed in dict.fromkeys(adventure.seeds)
        if seed not in known
    ]


def _waits_for_engine_flag(locked_by: str | None) -> bool:
    """Замок `flag:<имя>`, где имя — флаг движка."""
    kind, _, name = (locked_by or "").partition(":")
    return kind == "flag" and name.startswith(ENGINE_FLAG_PREFIXES)


def _check_engine_flags(adventure: Adventure) -> list[Issue]:
    """Флаги, которые ведёт движок, автор не трогает: находка не может их выставить (`set_flag`
    тоже запрещает), а замок на таком флаге открылся бы в начале боя (флаг `active` уже непустой),
    а не после него."""
    issues = [
        _error(
            "schema",
            source,
            f"reveals: флаг {quoted(reveal.id)} ведёт движок "
            f"(префиксы {', '.join(ENGINE_FLAG_PREFIXES)}), находка не может его выставить",
        )
        for source, reveals in _reveal_sources(adventure)
        for reveal in reveals
        if reveal.kind == "flag" and reveal.id.startswith(ENGINE_FLAG_PREFIXES)
    ]
    locks = (  # выходы и места: у обоих есть `id` и `locked_by`
        (f"location:{location.id}/{kind}:{place.id}", place.locked_by)
        for location in adventure.locations.values()
        for kind, places in (("exit", location.exits), ("feature", location.features))
        for place in places
    )
    issues += [
        _error(
            "schema",
            ref,
            f"locked_by: замок {quoted(locked_by)} ждёт флаг, который ведёт движок "
            f"(префиксы {', '.join(ENGINE_FLAG_PREFIXES)}): он открылся бы в начале боя",
        )
        for ref, locked_by in locks
        if _waits_for_engine_flag(locked_by)
    ]
    return issues


def _check_srd_item_ids(adventure: Adventure, content: SrdContent) -> list[Issue]:
    """Предмет приключения не берёт id предмета SRD: бой считает предметом приключения любой
    `item_id` из `adventure.items`, и герою отказали бы в его собственном зелье SRD."""
    return [
        _error(
            "schema",
            f"item:{item.id}",
            f"id: предмет приключения {item.id!r} совпадает с предметом SRD; бой принял бы "
            "предмет SRD героя за предмет приключения, выберите другой id",
        )
        for item in adventure.items.values()
        if item.id in content.items
    ]


def _check_revealed_items(adventure: Adventure) -> list[Issue]:
    """Предмет, который находка кладёт в руки (`reveals: item`), лежит в локации этого места.

    Инструменты ищут закрытое место предмета в локации его держателя: пока место не раскрыто,
    предмет внутри и `give_item` его не отдаёт. Если держатель в другой локации или у NPC, понять,
    где предмет «внутри», нельзя, и закрытое место обходится. Несуществующий предмет уже нашёл
    `_check_references`."""
    return [
        _error(
            "dangling_ref",
            f"location:{location.id}/feature:{feature.id}",
            f"reveals: предмет {quoted(reveal.id)} должен лежать в локации места "
            f"(holder 'location:{location.id}'), а лежит у {quoted(item.holder)}",
        )
        for location in adventure.locations.values()
        for feature in location.features
        for reveal in feature.reveals
        if reveal.kind == "item"
        and (item := adventure.items.get(reveal.id)) is not None
        and item.holder != f"location:{location.id}"
    ]


# --- повторяющиеся id ---


def _repeated_id_errors(kind: str, entries: Iterable[tuple[str, str]], reason: str) -> list[Issue]:
    """Ошибка на каждое повторное использование id. `entries` — пары (ref, id) одной области
    видимости в порядке файла; первое использование считается законным."""
    first_use: dict[str, str] = {}
    issues: list[Issue] = []
    for ref, ident in entries:
        if ident not in first_use:
            first_use[ident] = ref
            continue
        earlier = f" (раньше: {first_use[ident]})" if first_use[ident] != ref else ""
        issues.append(
            _error("schema", ref, f"{kind}: id {quoted(ident)} повторяется{earlier}; {reason}")
        )
    return issues


def _check_unique_ids(adventure: Adventure) -> list[Issue]:
    """Инструменты находят место, улику, выход и зону по голому id и берут первое совпадение, а
    отметки находок (`feature:<id>`, `clue:<id>`) общие на всё приключение. Повтор id молча
    ломает игру, исправить данные модель мастера не может, поэтому повтор — блокирующая ошибка.
    Места, улики и предметы из `reveals` уникальны во всём приключении, выходы и зоны — внутри
    локации."""
    locations = adventure.locations.values()
    issues = _repeated_id_errors(
        "место",
        ((f"location:{loc.id}/feature:{f.id}", f.id) for loc in locations for f in loc.features),
        "находка feature:<id> одна на всё приключение",
    )
    issues += _repeated_id_errors(
        "улика",
        (
            (f"revelation:{rev.id}/clue:{clue.id}", clue.id)
            for rev in adventure.revelations.values()
            for clue in rev.clues
        ),
        "находка clue:<id> одна на всё приключение",
    )
    issues += _repeated_id_errors(
        "предмет",
        (
            (f"location:{loc.id}/feature:{f.id}", reveal.id)
            for loc in locations
            for f in loc.features
            for reveal in f.reveals
            if reveal.kind == "item"
        ),
        "предмет лежит внутри одного места, иначе неясно, какое из них его закрывает",
    )
    issues += _repeated_id_errors(
        "тайна места",
        ((f"location:{loc.id}/secret:{s.id}", s.id) for loc in locations for s in loc.secrets),
        "id тайны места один на всё приключение",
    )
    issues += _repeated_id_errors(
        "связь",
        ((f"relation:{r.id}", r.id) for r in adventure.relations),
        "по id связь находят в графе",
    )
    issues += _repeated_id_errors(
        "край",
        ((f"region:{r.id}", r.id) for r in adventure.world.regions),
        "по id край находят в мире",
    )
    for location in locations:
        issues += _repeated_id_errors(
            "выход",
            ((f"location:{location.id}/exit:{e.id}", e.id) for e in location.exits),
            "move_party берёт первый выход с этим id",
        )
        issues += _repeated_id_errors(
            "зона",
            ((f"location:{location.id}/zone:{z.id}", z.id) for z in location.zones),
            "start_combat не начнёт бой в такой локации",
        )
    return issues


def _check_relation_ends(adventure: Adventure) -> list[Issue]:
    """У связи графа разные концы: связь сущности с самой собой ничего не говорит мастеру."""
    return [
        _error(
            "schema",
            f"relation:{relation.id}",
            f"a и b: связь ведёт от {quoted(relation.a)} к нему же, у связи разные концы",
        )
        for relation in adventure.relations
        if relation.a == relation.b
    ]


def _check_hidden_exit_ids(adventure: Adventure) -> list[Issue]:
    """Скрытый выход открывает находка `exit:<id>`, общая на всё приключение: id скрытого выхода
    не должен встречаться у выходов других локаций, иначе поиск одного откроет и другой.
    Повтор внутри самой локации ловит `_check_unique_ids`."""
    places: dict[str, set[str]] = {}
    for location in adventure.locations.values():
        for exit_ in location.exits:
            places.setdefault(exit_.id, set()).add(location.id)
    return [
        _error(
            "schema",
            f"location:{location.id}/exit:{exit_.id}",
            f"hidden_dc: id скрытого выхода {exit_.id!r} есть и у выходов в локациях "
            f"{', '.join(others)}: находка exit:<id> общая, поиск одного открыл бы другой",
        )
        for location in adventure.locations.values()
        for exit_ in location.exits
        if exit_.hidden_dc is not None and (others := sorted(places[exit_.id] - {location.id}))
    ]


# --- достижимость ---


def _lock_key(locked_by: str | None, adventure: Adventure) -> str | None:
    """id ключа замка `item:<id>`. Замок-флаг (имена флагов придумывает автор) и ссылка на
    несуществующий предмет (её уже нашёл `_check_references`) ключа не имеют."""
    kind, _, name = (locked_by or "").partition(":")
    return name if kind == "item" and name in adventure.items else None


def _reachable_locations(adventure: Adventure, keys: Container[str]) -> set[str]:
    """Локации, до которых можно дойти от старта по выходам, имея предметы `keys`.

    Скрытые выходы и замки-флаги считаются путями: как игроки их откроют, валидатор не судит.
    Замок-предмет проходим, только если его ключ в `keys`: с ключами «все предметы приключения»
    получается граф как нарисован, с пустым множеством — путь без единого ключа."""
    seen = {adventure.start_location}
    queue = deque(seen)
    while queue:
        location = adventure.locations.get(queue.popleft())
        if location is None:
            continue  # ссылка в пустоту: `dangling_ref` уже есть
        for exit_ in location.exits:
            key = _lock_key(exit_.locked_by, adventure)
            if exit_.to not in seen and (key is None or key in keys):
                seen.add(exit_.to)
                queue.append(exit_.to)
    return seen


def _check_reachability(adventure: Adventure) -> list[Issue]:
    if adventure.start_location not in adventure.locations:
        return []  # без старта нечего обходить; об этом уже сказал `dangling_ref`
    reached = _reachable_locations(adventure, adventure.items.keys())
    issues = [
        _error(
            "unreachable_location", f"location:{location_id}", "до локации нельзя дойти от старта"
        )
        for location_id in adventure.locations
        if location_id not in reached
    ]
    finale = adventure.finale_location
    if finale in adventure.locations and finale not in reached:
        issues.append(
            _error("no_path_to_finale", f"location:{finale}", "от старта нет пути к финалу")
        )
    return issues


# --- открытия и улики ---


def _holder_location(holder: str, adventure: Adventure) -> str | None:
    """Локация держателя предмета: сама локация или локация NPC; иначе (`gone`) — `None`."""
    kind, _, ident = holder.partition(":")
    if kind == "location":
        return ident
    npc = adventure.npcs.get(ident) if kind == "npc" else None
    return None if npc is None else npc.location_id


def _clue_place(clue: Clue, adventure: Adventure) -> str | None:
    """Где игроки найдут улику: id локации, а у предмета вне локаций — сама ссылка `where`.
    Ссылка в пустоту даёт `None` (её уже нашёл `_check_references`)."""
    kind, _, ident = clue.where.partition(":")
    if kind == "feature":
        return next(
            (
                location.id
                for location in adventure.locations.values()
                if any(feature.id == ident for feature in location.features)
            ),
            None,
        )
    if kind == "npc":
        npc = adventure.npcs.get(ident)
        return None if npc is None else npc.location_id
    if kind == "item":
        item = adventure.items.get(ident)
        if item is None:
            return None
        return _holder_location(item.holder, adventure) or clue.where
    return None


def _check_revelations(adventure: Adventure) -> list[Issue]:
    issues: list[Issue] = []
    for revelation in adventure.revelations.values():
        ref = f"revelation:{revelation.id}"
        if len(revelation.clues) < MIN_CLUES:
            issues.append(
                _error(
                    "too_few_clues",
                    ref,
                    f"улик {len(revelation.clues)}, нужно не меньше {MIN_CLUES}",
                )
            )
        places = [_clue_place(clue, adventure) for clue in revelation.clues]
        found = {place for place in places if place is not None}
        if len(found) == 1 and len(places) > 1:
            issues.append(
                _error(
                    "clues_same_place",
                    ref,
                    "все улики лежат в одном месте: не дойдя туда, игроки не найдут ни одной",
                )
            )
    return issues


# --- ключи замков ---
# `_check_reachability` считает запертые выходы путями, но замок-предмет открыт, только пока
# предмет у отряда (`tools.lore.is_locked`). Ключ, которого не достать, оставляет замок закрытым
# навсегда, и финал недостижим без единого замечания. Поэтому здесь отряд проходит приключение
# «по-настоящему»: ключ сначала нужно получить.


def _closed_places(adventure: Adventure) -> dict[str, Feature]:
    """Предмет → место, внутри которого он лежит: в локации-держателе есть место, чьи `reveals` его
    называют (первое по порядку). Так же ищет закрытое место `tools.lore.closed_place_with_item`; у
    NPC предмет не «внутри», и таких предметов в словаре нет.

    Индекс строится один раз: поиск места перебором на каждый предмет в каждом круге
    `_playthrough` делал проверку кубической (800 сундуков цепочкой считались около 47 секунд)."""
    named_in: dict[tuple[str, str], Feature] = {}
    for location_id, location in adventure.locations.items():
        for feature in location.features:
            for reveal in feature.reveals:
                if reveal.kind == "item":
                    named_in.setdefault((location_id, reveal.id), feature)
    places: dict[str, Feature] = {}
    for item in adventure.items.values():
        kind, _, location_id = item.holder.partition(":")
        if kind == "location" and (feature := named_in.get((location_id, item.id))) is not None:
            places[item.id] = feature
    return places


def _unopened_place(
    item: Item, adventure: Adventure, places: dict[str, Feature], owned: Container[str]
) -> Feature | None:
    """Место, внутри которого лежит предмет (`places`) и которое заперто ключом, ещё не полученным
    (`owned`). Нет такого места (предмет лежит открыто, замка нет или он флаговый, ключ уже есть) —
    `None`."""
    closed = places.get(item.id)
    inner_key = None if closed is None else _lock_key(closed.locked_by, adventure)
    return closed if inner_key is not None and inner_key not in owned else None


def _obtainable_items(
    adventure: Adventure,
    places: dict[str, Feature],
    reached: Container[str],
    owned: Container[str],
) -> set[str]:
    """Предметы, которые отряд получит, побывав в `reached` и имея `owned`: предмет лежит в такой
    локации (сам или у NPC) и не заперт внутри места, ключ которого ещё не получен. Ушедший из игры
    (`gone`) предмет не достать никогда. Держатель в пустоту (его уже нашёл `_check_references`)
    не судим: предмет считается доступным, чтобы одна ошибка не плодила вторую."""
    found: set[str] = set()
    for item in adventure.items.values():
        if item.holder == "gone":
            continue
        place = _holder_location(item.holder, adventure)
        if place is None or place not in adventure.locations:
            found.add(item.id)
            continue
        if place in reached and _unopened_place(item, adventure, places, owned) is None:
            found.add(item.id)
    return found


def _playthrough(adventure: Adventure, places: dict[str, Feature]) -> tuple[set[str], set[str]]:
    """Что отряд пройдёт и получит от старта: (локации, предметы). Круг за кругом: полученные
    предметы открывают замки, новые локации дают новые предметы, пока что-то прибавляется."""
    owned: set[str] = set()
    while True:
        reached = _reachable_locations(adventure, owned)
        found = _obtainable_items(adventure, places, reached, owned)
        if found <= owned:
            return reached, owned
        owned |= found


def _lost_key(locked_by: str | None, adventure: Adventure, owned: Container[str]) -> Item | None:
    """Ключ замка, которого отряд не получит; без замка-предмета или с доступным ключом — `None`."""
    key = _lock_key(locked_by, adventure)
    return None if key is None or key in owned else adventure.items[key]


def _lost_key_issue(
    severity: Literal["error", "warning"],
    ref: str,
    key: Item,
    adventure: Adventure,
    places: dict[str, Feature],
    owned: Container[str],
) -> Issue:
    """Проблема `unreachable_key` с настоящей причиной: ключ ушёл из игры, заперт внутри места или
    лежит там, куда отряд не попадёт. Если верны две последние, называется место: его замок
    исправляется первым, а недоступность локации покажут другие проблемы."""
    locked_place = _unopened_place(key, adventure, places, owned)
    if key.holder == "gone":
        reason = "ушёл из игры (holder 'gone')"
    elif locked_place is not None:
        lock = _lock_key(locked_place.locked_by, adventure)
        reason = (
            f"лежит внутри места {locked_place.id!r} ({key.holder}), "
            f"которое заперто недоступным ключом {lock!r}"
        )
    else:
        reason = (
            f"лежит у {quoted(key.holder)}, "
            "куда отряд не попадёт без него самого или другого такого ключа"
        )
    return _issue(severity, "unreachable_key", ref, f"locked_by: ключ {key.id!r} {reason}")


def _check_lock_keys(adventure: Adventure) -> list[Issue]:
    """Замок-предмет с недостижимым ключом (`unreachable_key`) не откроется никогда.

    Ошибка, если замок отрезает то, что нужно игре: выход, за которым локация без другого пути, или
    место с находками (`reveals`, улика лежит в нём). Иначе предупреждение: закрыта лишняя дверь
    или пустое место. Замки в локациях, куда отряд не попадёт, не судим: это следствие другого
    замка (его покажет он сам) или `unreachable_location`."""
    if adventure.start_location not in adventure.locations:
        return []  # без старта нечего проходить; об этом уже сказал `dangling_ref`
    places = _closed_places(adventure)
    reached, owned = _playthrough(adventure, places)
    clue_places = {clue.where for rev in adventure.revelations.values() for clue in rev.clues}
    issues: list[Issue] = []
    for location in adventure.locations.values():
        if location.id not in reached:
            continue
        for exit_ in location.exits:
            if key := _lost_key(exit_.locked_by, adventure, owned):
                severity = "warning" if exit_.to in reached else "error"
                ref = f"location:{location.id}/exit:{exit_.id}"
                issues.append(_lost_key_issue(severity, ref, key, adventure, places, owned))
        for feature in location.features:
            if key := _lost_key(feature.locked_by, adventure, owned):
                holds_findings = bool(feature.reveals) or f"feature:{feature.id}" in clue_places
                severity = "error" if holds_findings else "warning"
                ref = f"location:{location.id}/feature:{feature.id}"
                issues.append(_lost_key_issue(severity, ref, key, adventure, places, owned))
    return issues


# --- бои ---


def _check_encounters(adventure: Adventure, content: SrdContent) -> list[Issue]:
    issues: list[Issue] = []
    rating_known = adventure.party_level in XP_BUDGET_PER_CHARACTER
    for encounter in adventure.encounters.values():
        ref = f"encounter:{encounter.id}"
        if not encounter.monsters:
            issues.append(_error("schema", ref, "monsters: в бою нет ни одной группы монстров"))
            continue  # сложность пустого боя не оценить, а «слишком лёгок» было бы лишним шумом
        unknown = [
            group.monster_id
            for group in encounter.monsters
            if find_monster(group.monster_id, adventure, content) is None
        ]
        issues += [
            _error(
                "unknown_monster",
                ref,
                f"монстра {quoted(monster_id)} нет ни в SRD, ни в статблоках",
            )
            for monster_id in dict.fromkeys(unknown)
        ]
        total = sum(group.count for group in encounter.monsters)
        if total > MAX_MONSTERS_PER_ENCOUNTER:
            issues.append(
                _error(
                    "schema",
                    ref,
                    f"monsters: {_shown(total)} монстров, в бою их не больше "
                    f"{MAX_MONSTERS_PER_ENCOUNTER}",
                )
            )
        if unknown or not rating_known:
            continue  # опыт неизвестных монстров или бюджет неизвестного уровня не посчитать
        xp = encounter_xp(encounter, adventure, content)
        rating = encounter_rating(xp, adventure.party_level, adventure.party_size)
        if rating == "deadly":
            issues.append(
                _warning("encounter_too_hard", ref, f"бой смертельно тяжёл: {_shown(xp)} XP")
            )
        elif encounter.boss and rating != "high":
            # Одно предупреждение вместо двух: «слишком лёгок» для босса уже сказано этим.
            high = party_budget(adventure.party_level, adventure.party_size)[2]
            message = f"бой с боссом легче высокой сложности: {_shown(xp)} XP, нужно от {high} XP"
            issues.append(_warning("boss_too_easy", ref, message))
        elif rating == "trivial":
            issues.append(
                _warning("encounter_too_easy", ref, f"бой слишком лёгок: {_shown(xp)} XP")
            )
    return issues


# --- предупреждения о тексте и NPC ---


def _check_locations(adventure: Adventure) -> list[Issue]:
    issues: list[Issue] = []
    for location in adventure.locations.values():
        ref = f"location:{location.id}"
        if not location.read_aloud.strip():
            issues.append(_warning("empty_read_aloud", ref, "нет текста для чтения вслух"))
        if not location.features:
            issues.append(_warning("no_features", ref, "в локации нет ни одного интересного места"))
    return issues


def _check_journal_texts(adventure: Adventure) -> list[Issue]:
    """Улика без описания и предмет без названия и `public`: дневнику отряда нечего о них
    написать — запись о сущности без текста не делается."""
    issues: list[Issue] = []
    for revelation in adventure.revelations.values():
        for clue in revelation.clues:
            if not clue.description.strip():
                ref = f"revelation:{revelation.id}/clue:{clue.id}"
                message = "у улики нет описания: дневник отряда не расскажет о находке"
                issues.append(_warning("clue_without_description", ref, message))
    for item in adventure.items.values():
        if not (item.name.strip() or item.public.strip()):
            message = "у предмета нет ни названия, ни public: дневник отряда не расскажет о нём"
            issues.append(_warning("item_without_text", f"item:{item.id}", message))
    return issues


def _check_npcs(adventure: Adventure) -> list[Issue]:
    """Без мотива предупреждаем любого NPC, без связей — тех, кто важен: со статблоком или
    названный в уликах. Связи берутся из общего графа, NPC может стоять на любом её конце."""
    named_in_clues = {clue.where for rev in adventure.revelations.values() for clue in rev.clues}
    related = {end for relation in adventure.relations for end in (relation.a, relation.b)}
    issues: list[Issue] = []
    for npc in adventure.npcs.values():
        ref = f"npc:{npc.id}"
        if not npc.motive.strip():
            issues.append(_warning("npc_without_motive", ref, "у NPC нет мотива"))
        important = npc.statblock is not None or ref in named_in_clues
        if important and ref not in related:
            issues.append(_warning("npc_without_relations", ref, "у важного NPC нет связей"))
    return issues


# --- оценка времени ---


def _estimate_minutes(adventure: Adventure) -> int:
    """12 мин на локацию, 30 на бой, 10 на открытие и 5 на NPC."""
    return (
        12 * len(adventure.locations)
        + 30 * len(adventure.encounters)
        + 10 * len(adventure.revelations)
        + 5 * len(adventure.npcs)
    )


def _check_duration(adventure: Adventure, estimated: int) -> list[Issue]:
    # Сравнение в часах: цель в минутах у огромного `target_hours` переполнила бы float.
    estimated_hours = estimated / 60
    target = adventure.target_hours
    if abs(estimated_hours - target) <= DURATION_TOLERANCE * target:
        return []
    message = (
        f"оценка времени игры {estimated} мин ({estimated_hours:.1f} ч) отличается от цели "
        f"{target:g} ч больше чем на {round(DURATION_TOLERANCE * 100)}%"
    )
    return [_warning("duration_mismatch", "adventure", message)]


def validate(adventure: Adventure, content: SrdContent) -> Report:
    """Проверяет приключение и собирает отчёт: проблемы, балл и оценку времени игры в минутах."""
    estimated = _estimate_minutes(adventure)
    issues = [
        *_check_settings(adventure),
        *_check_custom_monsters(adventure, content),
        *_check_unique_ids(adventure),
        *_check_hidden_exit_ids(adventure),
        *_check_relation_ends(adventure),
        *_check_references(adventure, content),
        *_check_zones(adventure),
        *_check_location_encounters(adventure),
        *_check_unlinked_encounters(adventure),
        *_check_seeds(adventure),
        *_check_engine_flags(adventure),
        *_check_srd_item_ids(adventure, content),
        *_check_revealed_items(adventure),
        *_check_reachability(adventure),
        *_check_lock_keys(adventure),
        *_check_revelations(adventure),
        *_check_encounters(adventure, content),
        *_check_locations(adventure),
        *_check_journal_texts(adventure),
        *_check_npcs(adventure),
        *_check_duration(adventure, estimated),
    ]
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = len(issues) - errors
    return Report(
        issues=issues,
        score=max(0, 100 - 10 * errors - 3 * warnings),
        estimated_minutes=estimated,
    )
