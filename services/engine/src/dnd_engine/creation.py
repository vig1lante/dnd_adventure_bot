"""Создание персонажа 3-го уровня: способы задать характеристики, проверка выбора и сборка.

`build_character` — единственная дверь к готовому `Character`: проверяет легальность каждого
выбора по правилам SRD и сам считает все числа (HP, ячейки, ресурсы). Быстрый и случайный
режимы (`quick_character`, `random_character`) только собирают `CharacterChoices` и отдают их
на ту же проверку. Все броски идут через переданный `Rng`.

Характеристики героя задаются только в рамках 27 очков (решение владельца R9): покупкой очков или
стандартным набором, который стоит те же 27. Броска `4d6kh3` нет; «Распределить случайно» —
`random_point_buy`: случайная раскладка тех же 27 очков, где больше достаётся ключевым
характеристикам класса.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from functools import cache
from types import MappingProxyType
from typing import NamedTuple

from pydantic import BaseModel

from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_NAMES_RU, Ability, Skill, modifier
from dnd_engine.character import Character, InventoryItem, Persona
from dnd_engine.content import (
    BackgroundDef,
    ClassDef,
    ItemDef,
    SpeciesDef,
    SpellcastingDef,
    SpellDef,
    SrdContent,
)
from dnd_engine.rng import Rng

CHARACTER_LEVEL = 3  # уровень всех персонажей среза
MAX_ABILITY_SCORE = 20

POINT_BUY_BUDGET = 27
# Таблица только для чтения: правила не должны меняться во время игры.
POINT_BUY_COST: Mapping[int, int] = MappingProxyType(
    {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}
)
STANDARD_ARRAY = (15, 14, 13, 12, 10, 8)

# Бонус предыстории: «+2 и +1» к двум характеристикам или «+1» к трём (по убыванию).
MAJOR_MINOR_BONUS = (2, 1)
SPREAD_BONUS = (1, 1, 1)
BACKGROUND_BONUS_SHAPES = (MAJOR_MINOR_BONUS, SPREAD_BONUS)

EXPERTISE_PICKS = 2
EXPERTISE_FEATURE = "expertise"
DWARVEN_TOUGHNESS_TRAIT = "dwarven_toughness"  # +1 HP за каждый уровень
SKILLFUL_TRAIT = "skillful"  # Умелость человека: один навык на выбор
# Острые чувства эльфа в срезе всегда дают Внимательность (выбор навыка не моделируется).
TRAIT_SKILLS: Mapping[str, Skill] = MappingProxyType({"keen_senses": Skill.PERCEPTION})

UNNAMED_CHARACTER_NAME = "Безымянный странник"


class FightingStyle(NamedTuple):
    name: str  # русское название для формы создания
    summary: str  # суть стиля одной фразой


# Движок знает боевые стили по id (`ClassDef.fighting_styles`), а русские название и суть нужны
# форме создания персонажа. Они записаны здесь, а не в таблице SRD: набор файлов `SrdContent` не
# меняется ради двух строк на стиль, а соседние названия (`ABILITY_NAMES_RU`, `SKILL_NAMES_RU`)
# тоже лежат в коде. Тест следит, что у каждого стиля каждого класса есть запись и лишних записей
# нет.
FIGHTING_STYLES: Mapping[str, FightingStyle] = MappingProxyType(
    {
        "archery": FightingStyle("Стрельба", "+2 к броскам атаки дальнобойным оружием."),
        "defense": FightingStyle("Оборона", "+1 к КД, пока надет доспех."),
        "dueling": FightingStyle(
            "Дуэлянт",
            "+2 к урону одноручным оружием ближнего боя, пока вторая рука свободна.",
        ),
    }
)


class AbilityMethod(StrEnum):
    POINT_BUY = "point_buy"
    STANDARD_ARRAY = "standard_array"
    # Бросок убран из создания (решение владельца R9). Значение осталось в перечне только затем,
    # чтобы старый клиент получил от `build_character` понятную русскую проблему, а не безликую
    # ошибку разбора; героя этим способом создать нельзя.
    ROLL = "roll"


class CharacterChoices(BaseModel):
    """Выбор игрока в мастере создания; числа персонажа из него считает движок."""

    name: str
    species: str
    class_id: str
    background: str
    method: AbilityMethod
    base_scores: dict[Ability, int]
    background_bonus: dict[Ability, int]  # {+2, +1} или {+1, +1, +1} из ability_options
    skills: list[Skill]  # выбор класса
    extra_skill: Skill | None = None  # Умелость человека
    expertise: list[Skill] = []  # плут: 2 навыка из владений
    fighting_style: str | None = None
    cantrips: list[str] = []
    prepared_spells: list[str] = []
    persona: Persona = Persona()


class CharacterValidationError(ValueError):
    """Выбор нарушает правила; `problems` — список всех найденных проблем."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "Выбор персонажа недопустим:\n" + "\n".join(f"- {p}" for p in self.problems)
        )


# --- проверки выбора: каждая возвращает список проблем (пустой — всё в порядке) ---


def _unknown(kind: str, value: str, table: Mapping[str, object]) -> list[str]:
    return [] if value in table else [f"неизвестный {kind}: {value!r}"]


def _numbers(values: Sequence[int]) -> str:
    return ", ".join(map(str, values))


def _skill_names(skills: Sequence[Skill]) -> str:
    return ", ".join(SKILL_NAMES_RU[skill] for skill in skills)


def _check_permutation(
    method: str, scores: Mapping[Ability, int], expected: Sequence[int]
) -> list[str]:
    if sorted(scores.values()) == sorted(expected):
        return []
    return [f"{method}: значения должны быть перестановкой набора {_numbers(expected)}"]


def _check_point_buy(scores: Mapping[Ability, int]) -> list[str]:
    low, high = min(POINT_BUY_COST), max(POINT_BUY_COST)
    problems = [
        f"{ABILITY_NAMES_RU[ability]}: значение {value} вне диапазона от {low} до {high}"
        " для покупки очков"
        for ability, value in scores.items()
        if value not in POINT_BUY_COST
    ]
    spent = sum(POINT_BUY_COST.get(value, 0) for value in scores.values())
    if spent > POINT_BUY_BUDGET:
        problems.append(f"покупка очков: потрачено {spent} из {POINT_BUY_BUDGET}")
    return problems


ROLL_REMOVED_PROBLEM = (
    "бросок характеристик больше не используется: распределите 27 очков (покупка очков)"
    " или возьмите стандартный набор"
)


def _check_base_scores(choices: CharacterChoices) -> list[str]:
    if choices.method is AbilityMethod.ROLL:
        return [ROLL_REMOVED_PROBLEM]
    scores = choices.base_scores
    if missing := [ability for ability in Ability if ability not in scores]:
        return ["не заданы характеристики: " + ", ".join(ABILITY_NAMES_RU[a] for a in missing)]
    match choices.method:
        case AbilityMethod.POINT_BUY:
            return _check_point_buy(scores)
        case AbilityMethod.STANDARD_ARRAY:
            return _check_permutation("стандартный набор", scores, STANDARD_ARRAY)


def _check_background_bonus(bonus: Mapping[Ability, int], background: BackgroundDef) -> list[str]:
    problems = []
    if tuple(sorted(bonus.values(), reverse=True)) not in BACKGROUND_BONUS_SHAPES:
        problems.append("бонус предыстории: нужно +2 и +1 к двум характеристикам или +1 к трём")
    if any(ability not in background.ability_options for ability in bonus):
        allowed = ", ".join(ABILITY_NAMES_RU[a] for a in background.ability_options)
        problems.append(f"бонус предыстории «{background.name}» можно давать только: {allowed}")
    return problems


def _check_score_cap(choices: CharacterChoices) -> list[str]:
    if choices.method is AbilityMethod.ROLL:
        return []  # значения отвергнутого способа не разбираем: причина уже названа одна
    return [
        f"{ABILITY_NAMES_RU[ability]}: итоговое значение {total} больше {MAX_ABILITY_SCORE}"
        for ability, base in choices.base_scores.items()
        if (total := base + choices.background_bonus.get(ability, 0)) > MAX_ABILITY_SCORE
    ]


def _has_trait(species: SpeciesDef, trait_id: str) -> bool:
    return any(trait.id == trait_id for trait in species.traits)


def _has_feature(cls: ClassDef, feature_id: str) -> bool:
    return any(f.id == feature_id and f.level <= CHARACTER_LEVEL for f in cls.features)


def _granted_skills(species: SpeciesDef, background: BackgroundDef) -> dict[Skill, str]:
    """Навыки, которые даются автоматически, вместе с источником — для подсказок об ошибках."""
    granted = dict.fromkeys(background.skills, f"предыстория «{background.name}»")
    for trait in species.traits:
        if (skill := TRAIT_SKILLS.get(trait.id)) is not None:
            granted.setdefault(skill, f"вид «{species.name}»")
    return granted


def _proficient_skills(
    granted: Mapping[Skill, str], picks: Sequence[Skill], extra_skill: Skill | None
) -> list[Skill]:
    """Все навыки с владением без повторов: автоматические, выбор класса, умелость."""
    extra = [] if extra_skill is None else [extra_skill]
    return list(dict.fromkeys([*granted, *picks, *extra]))


def _check_class_skills(
    picks: Sequence[Skill], cls: ClassDef, granted: Mapping[Skill, str]
) -> list[str]:
    problems = []
    if len(picks) != cls.skill_choices:
        problems.append(
            f"навыки класса «{cls.name}»: нужно выбрать ровно {cls.skill_choices},"
            f" выбрано {len(picks)}"
        )
    free = [s for s in cls.skill_options if s not in granted and s not in picks]
    for skill in dict.fromkeys(picks):
        name = SKILL_NAMES_RU[skill]
        if skill not in cls.skill_options:
            problems.append(f"навык «{name}» не входит в список класса «{cls.name}»")
        elif skill in granted:
            problems.append(
                f"навык «{name}» уже даёт {granted[skill]}; выберите другой из: "
                + (_skill_names(free) or "свободных навыков нет")
            )
        if picks.count(skill) > 1:
            problems.append(f"навык «{name}» выбран больше одного раза")
    return problems


def _check_extra_skill(
    extra: Skill | None, picks: Sequence[Skill], species: SpeciesDef, granted: Mapping[Skill, str]
) -> list[str]:
    if not _has_trait(species, SKILLFUL_TRAIT):
        if extra is None:
            return []
        return [f"extra_skill не нужен: вид «{species.name}» не даёт навык на выбор"]
    if extra is None:
        return [f"вид «{species.name}» даёт навык на выбор: укажите extra_skill"]
    name = SKILL_NAMES_RU[extra]
    if extra in granted:
        return [f"дополнительный навык «{name}» уже даёт {granted[extra]}; выберите другой"]
    if extra in picks:
        return [f"дополнительный навык «{name}» уже выбран среди навыков класса; выберите другой"]
    return []


def _check_expertise(
    picks: Sequence[Skill], cls: ClassDef, proficient: Sequence[Skill]
) -> list[str]:
    if not _has_feature(cls, EXPERTISE_FEATURE):
        return [f"у класса «{cls.name}» нет компетентности"] if picks else []
    problems = []
    if len(picks) != EXPERTISE_PICKS:
        problems.append(
            f"компетентность: нужно выбрать навыков ровно {EXPERTISE_PICKS}, выбрано {len(picks)}"
        )
    for skill in dict.fromkeys(picks):
        name = SKILL_NAMES_RU[skill]
        if skill not in proficient:
            problems.append(f"компетентность: навык «{name}» не из владений")
        if picks.count(skill) > 1:
            problems.append(f"компетентность: навык «{name}» выбран больше одного раза")
    return problems


def _check_fighting_style(style: str | None, cls: ClassDef) -> list[str]:
    if not cls.fighting_styles:
        return [] if style is None else [f"у класса «{cls.name}» нет боевого стиля"]
    options = ", ".join(cls.fighting_styles)
    if style is None:
        return [f"боевой стиль обязателен: выберите один из: {options}"]
    if style not in cls.fighting_styles:
        return [f"боевой стиль {style!r} недоступен классу «{cls.name}»; доступны: {options}"]
    return []


class SpellOptions(NamedTuple):
    cantrips: tuple[SpellDef, ...]  # заговоры класса (0-й уровень)
    spells: tuple[SpellDef, ...]  # заклинания от 1-го уровня до высшего уровня ячеек персонажа


def class_spell_options(spellcasting: SpellcastingDef, content: SrdContent) -> SpellOptions:
    """Заклинания списка класса, из которых персонаж 3-го уровня выбирает свои, в порядке данных.

    Заговоры — заклинания 0-го уровня; подготовленные — от 1-го уровня до высшего уровня ячеек
    персонажа (заговоры сюда не входят). По этим вариантам `build_character` проверяет выбор,
    из них же случайный режим набирает заклинания и форма создания показывает список."""
    top_level = max(spellcasting.slots[CHARACTER_LEVEL])
    in_list = [
        spell for spell in content.spells.values() if spellcasting.spell_list in spell.classes
    ]
    return SpellOptions(
        cantrips=tuple(spell for spell in in_list if spell.level == 0),
        spells=tuple(spell for spell in in_list if 1 <= spell.level <= top_level),
    )


def _check_spell_group(
    label: str, picks: Sequence[str], wanted: int, allowed: Sequence[SpellDef], content: SrdContent
) -> list[str]:
    allowed_ids = {spell.id for spell in allowed}
    problems = []
    if len(picks) != wanted:
        problems.append(f"{label}: нужно выбрать ровно {wanted}, выбрано {len(picks)}")
    for spell_id in dict.fromkeys(picks):
        spell = content.spells.get(spell_id)
        if spell is None:
            problems.append(f"{label}: неизвестное заклинание {spell_id!r}")
            continue
        if spell_id not in allowed_ids:
            problems.append(
                f"{label}: заклинание «{spell.name}» недоступно — нужно из списка класса"
                " и подходящего уровня"
            )
        if picks.count(spell_id) > 1:
            problems.append(f"{label}: заклинание «{spell.name}» выбрано больше одного раза")
    return problems


def _check_spells(choices: CharacterChoices, cls: ClassDef, content: SrdContent) -> list[str]:
    spellcasting = cls.spellcasting
    if spellcasting is None:
        has_spells = choices.cantrips or choices.prepared_spells
        return [f"у класса «{cls.name}» нет заклинаний"] if has_spells else []
    options = class_spell_options(spellcasting, content)
    return [
        *_check_spell_group(
            "заговоры",
            choices.cantrips,
            spellcasting.cantrips_known[CHARACTER_LEVEL],
            options.cantrips,
            content,
        ),
        *_check_spell_group(
            "подготовленные заклинания",
            choices.prepared_spells,
            spellcasting.prepared[CHARACTER_LEVEL],
            options.spells,
            content,
        ),
    ]


# --- сборка персонажа из проверенного выбора ---


def _max_hp(cls: ClassDef, species: SpeciesDef, constitution: int) -> int:
    """Максимум кости хитов на 1-м уровне, среднее за остальные, Тел за каждый уровень."""
    average = cls.hit_die // 2 + 1
    hp = cls.hit_die + (CHARACTER_LEVEL - 1) * average + CHARACTER_LEVEL * modifier(constitution)
    return hp + CHARACTER_LEVEL if _has_trait(species, DWARVEN_TOUGHNESS_TRAIT) else hp


def _equipment_slot(item: ItemDef) -> str | None:
    """Что занимает предмет в экипировке: оружие, доспех или щит (у остального слота нет)."""
    if item.weapon is not None:
        return "weapon"
    if item.armor is not None:
        return "shield" if item.armor.kind == "shield" else "armor"
    return None


def _starting_inventory(
    cls: ClassDef, background: BackgroundDef, content: SrdContent
) -> list[InventoryItem]:
    """Снаряжение класса и предыстории; одинаковые предметы складываются в одну стопку.

    Надеты первое оружие, первый доспех и первый щит из набора."""
    counts = Counter([*cls.starting_equipment, *background.equipment])
    inventory = [InventoryItem(item_id=item_id, qty=qty) for item_id, qty in counts.items()]
    taken: set[str] = set()
    for entry in inventory:
        slot = _equipment_slot(content.items[entry.item_id])
        if slot is not None and slot not in taken:
            entry.equipped = True
            taken.add(slot)
    return inventory


def _assemble(choices: CharacterChoices, content: SrdContent, character_id: str) -> Character:
    """Собирает персонажа из выбора, прошедшего проверки: все id уже найдены в данных."""
    species = content.species[choices.species]
    cls = content.classes[choices.class_id]
    background = content.backgrounds[choices.background]
    scores = {a: choices.base_scores[a] + choices.background_bonus.get(a, 0) for a in Ability}
    hp_max = _max_hp(cls, species, scores[Ability.CON])
    slots = cls.spellcasting.slots[CHARACTER_LEVEL] if cls.spellcasting else {}
    granted = _granted_skills(species, background)
    return Character(
        id=character_id,
        name=choices.name,
        level=CHARACTER_LEVEL,
        species=choices.species,
        class_id=choices.class_id,
        background=choices.background,
        scores=scores,
        skill_proficiencies=_proficient_skills(granted, choices.skills, choices.extra_skill),
        expertise=choices.expertise,
        save_proficiencies=list(cls.saving_throws),
        fighting_style=choices.fighting_style,
        hp_max=hp_max,
        hp=hp_max,
        hit_dice_max=CHARACTER_LEVEL,
        hit_dice=CHARACTER_LEVEL,
        spell_slots_max=slots,
        spell_slots=slots,
        cantrips=choices.cantrips,
        prepared_spells=choices.prepared_spells,
        inventory=_starting_inventory(cls, background, content),
        resources={
            name: by_level[CHARACTER_LEVEL]
            for name, by_level in cls.resources.items()
            if CHARACTER_LEVEL in by_level
        },
        # Экземпляр модели pydantic не копирует сам: без копии персонаж делил бы persona с выбором.
        persona=choices.persona.model_copy(),
    )


def build_character(
    choices: CharacterChoices, content: SrdContent, *, character_id: str
) -> Character:
    """Проверяет выбор по правилам и собирает персонажа 3-го уровня.

    Нарушения не прерывают проверку: `CharacterValidationError.problems` содержит их все.
    """
    species = content.species.get(choices.species)
    cls = content.classes.get(choices.class_id)
    background = content.backgrounds.get(choices.background)

    problems = [
        *_unknown("вид", choices.species, content.species),
        *_unknown("класс", choices.class_id, content.classes),
        *_unknown("предыстория", choices.background, content.backgrounds),
        *_check_base_scores(choices),
        *_check_score_cap(choices),
    ]
    # Проверки, которым нужны данные записи, пропускаются, если запись не найдена.
    if background is not None:
        problems += _check_background_bonus(choices.background_bonus, background)
    if cls is not None:
        problems += _check_fighting_style(choices.fighting_style, cls)
        problems += _check_spells(choices, cls, content)
    if species is not None and cls is not None and background is not None:
        granted = _granted_skills(species, background)
        proficient = _proficient_skills(granted, choices.skills, choices.extra_skill)
        problems += _check_class_skills(choices.skills, cls, granted)
        problems += _check_extra_skill(choices.extra_skill, choices.skills, species, granted)
        problems += _check_expertise(choices.expertise, cls, proficient)

    if problems:
        raise CharacterValidationError(problems)
    return _assemble(choices, content, character_id)


# --- быстрый режим ---


def _scores_by_priority(cls: ClassDef, values: Sequence[int]) -> dict[Ability, int]:
    return dict(zip(cls.ability_priority, values, strict=True))


def _first_free_skill(cls: ClassDef, held: Sequence[Skill]) -> Skill:
    """Первый навык, которого ещё нет: сначала из списка класса, затем любой."""
    return next(skill for skill in [*cls.skill_options, *Skill] if skill not in held)


def _quick_skills(cls: ClassDef, granted: Mapping[Skill, str]) -> list[Skill]:
    """Навыки быстрого режима: `default_skills`, а те, что уже даны видом или предысторией
    (например, Внимательность эльфа), заменяются следующими навыками из списка класса."""
    kept = [skill for skill in cls.default_skills if skill not in granted]
    replacements = [s for s in cls.skill_options if s not in granted and s not in kept]
    return [*kept, *replacements][: cls.skill_choices]


def _split_default_spells(cls: ClassDef, content: SrdContent) -> tuple[list[str], list[str]]:
    """Делит `default_spells` на заговоры и подготовленные заклинания по уровню заклинания."""
    cantrips = [s for s in cls.default_spells if content.spells[s].level == 0]
    return cantrips, [s for s in cls.default_spells if s not in cantrips]


def quick_character(
    class_id: str,
    species: str,
    content: SrdContent,
    *,
    name: str,
    concept: str,
    character_id: str,
) -> Character:
    """Легальный персонаж по классу и виду: стандартный набор по приоритетам класса,
    предыстория, навыки и заклинания по умолчанию."""
    if problems := [
        *_unknown("вид", species, content.species),
        *_unknown("класс", class_id, content.classes),
    ]:
        raise CharacterValidationError(problems)
    species_def = content.species[species]
    cls = content.classes[class_id]
    background = content.backgrounds[cls.default_background]

    granted = _granted_skills(species_def, background)
    skills = _quick_skills(cls, granted)
    extra_skill = (
        _first_free_skill(cls, [*granted, *skills])
        if _has_trait(species_def, SKILLFUL_TRAIT)
        else None
    )
    # Бонус получают первые две характеристики предыстории в порядке приоритета класса.
    bonus_abilities = [a for a in cls.ability_priority if a in background.ability_options][:2]
    cantrips, prepared = _split_default_spells(cls, content)
    choices = CharacterChoices(
        name=name,
        species=species,
        class_id=class_id,
        background=background.id,
        method=AbilityMethod.STANDARD_ARRAY,
        base_scores=_scores_by_priority(cls, STANDARD_ARRAY),
        background_bonus=dict(zip(bonus_abilities, MAJOR_MINOR_BONUS, strict=True)),
        skills=skills,
        extra_skill=extra_skill,
        expertise=skills[:EXPERTISE_PICKS] if _has_feature(cls, EXPERTISE_FEATURE) else [],
        fighting_style=cls.fighting_styles[0] if cls.fighting_styles else None,
        cantrips=cantrips,
        prepared_spells=prepared,
        persona=Persona(concept=concept),
    )
    return build_character(choices, content, character_id=character_id)


# --- случайный режим ---


def _choice[T](rng: Rng, items: Sequence[T]) -> T:
    return items[rng.randint(0, len(items) - 1)]


def _shuffled[T](rng: Rng, items: Sequence[T]) -> list[T]:
    """Тасование Фишера — Йетса: порядок зависит только от значений `rng`."""
    result = list(items)
    for i in range(len(result) - 1, 0, -1):
        j = rng.randint(0, i)
        result[i], result[j] = result[j], result[i]
    return result


# Первые позиции приоритета класса — ключевые характеристики: как и в `random_character`, они
# получают лучшие значения раскладки, а остальные четыре делят оставшиеся в случайном порядке.
KEY_ABILITY_COUNT = 2
PRIMARY_ABILITY_MIN = 14  # основная характеристика получает 15 или 14


@cache
def _budget_layouts() -> tuple[tuple[int, ...], ...]:
    """Все раскладки шести значений (по убыванию), стоящие ровно `POINT_BUY_BUDGET` очков,
    с основной характеристикой не ниже `PRIMARY_ABILITY_MIN`. Строится из `POINT_BUY_COST`, поэтому
    легальность по цене и диапазону следует из самой таблицы правил, а не из отдельной проверки."""
    values = sorted(POINT_BUY_COST, reverse=True)
    found: list[tuple[int, ...]] = []

    def extend(prefix: tuple[int, ...], ceiling: int, left: int) -> None:
        if len(prefix) == len(Ability):
            if left == 0 and prefix[0] >= PRIMARY_ABILITY_MIN:
                found.append(prefix)
            return
        for value in values:
            if value <= ceiling and POINT_BUY_COST[value] <= left:
                extend((*prefix, value), value, left - POINT_BUY_COST[value])

    extend((), values[0], POINT_BUY_BUDGET)
    return tuple(found)


def random_point_buy(rng: Rng, content: SrdContent, class_id: str) -> dict[Ability, int]:
    """Случайная раскладка `POINT_BUY` ровно на `POINT_BUY_BUDGET` очков для класса.

    Раскладка выбирается равновероятно среди всех легальных, в которых основная характеристика
    не ниже `PRIMARY_ABILITY_MIN`. Лучшее значение получает основная характеристика класса,
    второе — вторая ключевая, остальные четыре значения раздаются остальным в случайном порядке.
    Прибавку предыстории игрок выбирает отдельно: максимум после неё — 17, до 20 не дойти.
    Неизвестный класс — `CharacterValidationError`."""
    if problems := _unknown("класс", class_id, content.classes):
        raise CharacterValidationError(problems)
    layout = _choice(rng, _budget_layouts())
    values = [*layout[:KEY_ABILITY_COUNT], *_shuffled(rng, layout[KEY_ABILITY_COUNT:])]
    return _scores_by_priority(content.classes[class_id], values)


def _random_skill_choices(
    rng: Rng, cls: ClassDef, species: SpeciesDef, granted: Mapping[Skill, str]
) -> tuple[list[Skill], Skill | None, list[Skill]]:
    """Навыки класса, умелость человека и компетентность плута — только те, что положены."""
    free = [skill for skill in cls.skill_options if skill not in granted]
    skills = _shuffled(rng, free)[: cls.skill_choices]
    held = [*granted, *skills]
    extra_skill = None
    if _has_trait(species, SKILLFUL_TRAIT):
        extra_skill = _choice(rng, [skill for skill in Skill if skill not in held])
        held.append(extra_skill)
    expertise = []
    if _has_feature(cls, EXPERTISE_FEATURE):
        expertise = _shuffled(rng, held)[:EXPERTISE_PICKS]
    return skills, extra_skill, expertise


def _random_spells(rng: Rng, cls: ClassDef, content: SrdContent) -> tuple[list[str], list[str]]:
    spellcasting = cls.spellcasting
    if spellcasting is None:
        return [], []
    options = class_spell_options(spellcasting, content)
    cantrips = _shuffled(rng, options.cantrips)[: spellcasting.cantrips_known[CHARACTER_LEVEL]]
    prepared = _shuffled(rng, options.spells)[: spellcasting.prepared[CHARACTER_LEVEL]]
    return [spell.id for spell in cantrips], [spell.id for spell in prepared]


def random_character(
    rng: Rng, content: SrdContent, *, taken_classes: set[str], character_id: str
) -> Character:
    """Случайный легальный персонаж; класс берётся из ещё не занятых, если они остались.

    Основная характеристика класса получает 15 или 14 из стандартного набора, как и вторая.
    Если случайный выбор нелегален (например, данные SRD не дают нужного числа вариантов),
    `build_character` откажет с `CharacterValidationError`.
    """
    free_classes = [class_id for class_id in content.classes if class_id not in taken_classes]
    class_id = _choice(rng, free_classes or list(content.classes))
    cls = content.classes[class_id]
    species = content.species[_choice(rng, list(content.species))]
    background = content.backgrounds[_choice(rng, list(content.backgrounds))]

    skills, extra_skill, expertise = _random_skill_choices(
        rng, cls, species, _granted_skills(species, background)
    )
    # Первые две позиции приоритета делят 15 и 14, остальные значения перемешиваются.
    values = [
        *_shuffled(rng, STANDARD_ARRAY[:2]),
        *_shuffled(rng, STANDARD_ARRAY[2:]),
    ]
    # Форма бонуса короче списка характеристик: zip берёт столько, сколько в форме.
    bonus_shape = _choice(rng, BACKGROUND_BONUS_SHAPES)
    bonus = dict(zip(_shuffled(rng, background.ability_options), bonus_shape, strict=False))
    cantrips, prepared = _random_spells(rng, cls, content)

    choices = CharacterChoices(
        name=UNNAMED_CHARACTER_NAME,
        species=species.id,
        class_id=class_id,
        background=background.id,
        method=AbilityMethod.STANDARD_ARRAY,
        base_scores=_scores_by_priority(cls, values),
        background_bonus=bonus,
        skills=skills,
        extra_skill=extra_skill,
        expertise=expertise,
        fighting_style=_choice(rng, cls.fighting_styles) if cls.fighting_styles else None,
        cantrips=cantrips,
        prepared_spells=prepared,
    )
    return build_character(choices, content, character_id=character_id)
