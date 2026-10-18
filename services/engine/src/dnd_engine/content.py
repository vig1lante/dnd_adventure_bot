"""Модели SRD-контента (классы, виды, заклинания, предметы, монстры) и загрузчик из YAML.

Механика описана полями, а не текстом. Каждая таблица лежит в своём файле
`<таблица>.yaml` как список объектов; в `SrdContent` ключ словаря — `id` записи.
Загрузчик возвращает либо корректный набор, либо `ContentError` со списком всех проблем.
"""

from collections.abc import Container, Iterable
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails

from dnd_engine.abilities import Ability, Skill


class _SrdModel(BaseModel):
    # Строгие неизменяемые модели: опечатка в данных ловится при загрузке, а не посреди игры.
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeatureDef(_SrdModel):
    id: str
    level: int
    name: str
    summary: str
    mechanical: bool  # true — реализовано движком, false — текст для мастера


class SpellcastingDef(_SrdModel):
    ability: Ability
    slots: dict[int, dict[int, int]]  # уровень персонажа -> {уровень ячейки: число}
    cantrips_known: dict[int, int]  # уровень персонажа -> число заговоров
    prepared: dict[int, int]  # уровень персонажа -> число подготовленных
    spell_list: str  # "cleric" | "wizard" | "ranger"


class ClassDef(_SrdModel):
    id: str
    name: str
    name_en: str
    hit_die: int
    ability_priority: tuple[Ability, ...]  # 6 характеристик, первая — основная
    saving_throws: tuple[Ability, Ability]
    armor: tuple[str, ...]  # "light" | "medium" | "heavy" | "shield"
    weapons: tuple[str, ...]  # "simple" | "martial"
    skill_choices: int
    skill_options: tuple[Skill, ...]
    default_skills: tuple[Skill, ...]  # для быстрого режима
    starting_equipment: tuple[str, ...]  # id предметов
    default_background: str
    default_spells: tuple[str, ...] = ()  # для быстрого режима
    fighting_styles: tuple[str, ...] = ()  # доступные боевые стили
    features: tuple[FeatureDef, ...]
    subclass_id: str
    subclass_name: str
    spellcasting: SpellcastingDef | None = None
    resources: dict[str, dict[int, int]] = {}  # ресурс -> уровень -> число использований


class TraitDef(_SrdModel):
    id: str
    name: str
    summary: str
    mechanical: bool


class SpeciesDef(_SrdModel):
    id: str
    name: str
    name_en: str
    size: str
    speed: int
    traits: tuple[TraitDef, ...]


class BackgroundDef(_SrdModel):
    id: str
    name: str
    name_en: str
    ability_options: tuple[Ability, Ability, Ability]
    skills: tuple[Skill, Skill]
    feat: str  # id черты
    equipment: tuple[str, ...]
    inspiration_triggers: tuple[str, ...]  # 2–3 триггера отыгрыша (модуль bg3_flavor)


class SpellEffect(_SrdModel):
    kind: Literal["attack", "save", "auto", "heal", "buff", "condition", "utility"]
    attack: Literal["melee", "ranged"] | None = None
    save: Ability | None = None
    damage: str | None = None  # запись костей
    damage_type: str | None = None
    half_on_save: bool = False
    heal: str | None = None
    condition: str | None = None
    buff: str | None = None  # id эффекта: "bless", "mage_armor", "aid", ...
    targets: int = 1
    repeats: int = 1  # «Волшебная стрела», «Палящий луч»


class SpellDef(_SrdModel):
    id: str
    name: str
    name_en: str
    level: int  # 0 — заговор
    casting_time: Literal["action", "bonus_action"]
    range: Literal["self", "touch", "engaged", "near", "far"]
    concentration: bool
    duration_rounds: int | None
    classes: tuple[str, ...]
    effect: SpellEffect
    upcast: str | None = None  # добавка костей за уровень ячейки
    summary: str


class WeaponProps(_SrdModel):
    category: Literal["simple", "martial"]
    damage: str
    damage_type: str
    # finesse, light, thrown, two_handed, versatile, heavy, reach, ammunition
    properties: tuple[str, ...]
    range: Literal["melee", "near", "far"]


class ArmorProps(_SrdModel):
    kind: Literal["light", "medium", "heavy", "shield"]
    base_ac: int
    dex_cap: int | None
    stealth_disadvantage: bool = False


class ItemDef(_SrdModel):
    id: str
    name: str
    name_en: str
    kind: Literal["weapon", "armor", "potion", "gear", "focus"]
    weapon: WeaponProps | None = None
    armor: ArmorProps | None = None
    heal: str | None = None  # для зелий
    summary: str = ""


class MonsterAction(_SrdModel):
    id: str
    name: str
    kind: Literal["attack", "save", "multiattack", "other"]
    attack_bonus: int | None = None
    damage: str | None = None
    damage_type: str | None = None
    range: Literal["melee", "near", "far"] = "melee"
    save: Ability | None = None
    dc: int | None = None
    half_on_save: bool = False
    condition: str | None = None
    multiattack: tuple[str, ...] = ()
    summary: str = ""


class MonsterDef(_SrdModel):
    id: str
    name: str
    name_en: str
    cr: float
    xp: int
    ac: int
    hp: int
    hp_dice: str
    speed: int
    scores: dict[Ability, int]
    saves: dict[Ability, int] = {}
    skills: dict[Skill, int] = {}
    resistances: tuple[str, ...] = ()
    immunities: tuple[str, ...] = ()
    actions: tuple[MonsterAction, ...]
    traits: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()  # "undead", "beast", ...


class ConditionDef(_SrdModel):
    id: str
    name: str
    summary: str


class FeatDef(_SrdModel):
    id: str
    name: str
    summary: str
    mechanical: bool


class SrdContent(_SrdModel):
    # Имена полей совпадают с именами файлов данных: `classes` читается из classes.yaml.
    classes: dict[str, ClassDef]
    species: dict[str, SpeciesDef]
    backgrounds: dict[str, BackgroundDef]
    feats: dict[str, FeatDef]
    spells: dict[str, SpellDef]
    items: dict[str, ItemDef]
    monsters: dict[str, MonsterDef]
    conditions: dict[str, ConditionDef]


class ContentError(ValueError):
    """Данные SRD некорректны; `problems` — список всех найденных проблем."""

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems)
        super().__init__("Данные SRD некорректны:\n" + "\n".join(f"- {p}" for p in self.problems))


def _describe_yaml_error(exc: yaml.YAMLError) -> str:
    # У ошибок разбора есть причина и место; у ошибок кодировки их нет, берём весь текст.
    mark = getattr(exc, "problem_mark", None)
    where = f" (строка {mark.line + 1})" if mark else ""
    reason = getattr(exc, "problem", None) or " ".join(str(exc).split())
    return f"некорректный YAML{where}: {reason}"


def _read_entries(source: Traversable, problems: list[str]) -> list[Any]:
    """Читает YAML-файл таблицы; ошибки чтения и формата пишет в `problems`."""
    try:
        # Только safe_load: он не умеет создавать python-объекты, так что тег вроде
        # `!!python/object/apply` не выполнит код, а даст YAMLError.
        data = yaml.safe_load(source.read_bytes())
    except FileNotFoundError:
        problems.append(f"{source.name}: файл не найден")
        return []
    except yaml.YAMLError as exc:
        problems.append(f"{source.name}: {_describe_yaml_error(exc)}")
        return []
    if not isinstance(data, list):
        problems.append(f"{source.name}: ожидался список объектов")
        return []
    return data


def _index_by_id(file_name: str, entries: list[Any], problems: list[str]) -> dict[str, Any]:
    """Превращает список записей в словарь по `id`; записи без id и дубликаты — в `problems`."""
    indexed: dict[str, Any] = {}
    for number, entry in enumerate(entries, start=1):
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        # YAML 1.1 читает `no`/`on`/`yes` как bool, поэтому id проверяем на тип строки.
        if not isinstance(entry_id, str):
            problems.append(f"{file_name}: запись {number}: нужен объект с текстовым полем id")
        elif entry_id in indexed:
            problems.append(f"{file_name}: дубликат id {entry_id!r} (запись {number})")
        else:
            indexed[entry_id] = entry
    return indexed


def _describe_schema_error(error: ErrorDetails) -> str:
    # loc из SrdContent.model_validate: (таблица, id записи, путь к полю внутри записи).
    table, entry_id, *path = error["loc"]
    field = ".".join(str(part) for part in path)
    return f"{table}.yaml, запись {entry_id!r}, поле {field}: {error['msg']}"


def _unknown_ids(owner: str, field: str, ids: Iterable[str], known: Container[str]) -> list[str]:
    """Жалобы на id из `ids`, которых нет в `known` (повторы одного id — одной жалобой)."""
    return [
        f"{owner}: {field} ссылается на несуществующий id {ref!r}"
        for ref in dict.fromkeys(ids)
        if ref not in known
    ]


def _check_references(srd: SrdContent) -> list[str]:
    """Проверяет ссылки между таблицами; возвращает все найденные проблемы."""
    problems: list[str] = []
    for cls in srd.classes.values():
        owner = f"classes.yaml, запись {cls.id!r}"
        problems += _unknown_ids(owner, "starting_equipment", cls.starting_equipment, srd.items)
        problems += _unknown_ids(
            owner, "default_background", [cls.default_background], srd.backgrounds
        )
        problems += _check_default_spells(owner, cls, srd)
    for background in srd.backgrounds.values():
        owner = f"backgrounds.yaml, запись {background.id!r}"
        problems += _unknown_ids(owner, "equipment", background.equipment, srd.items)
        problems += _unknown_ids(owner, "feat", [background.feat], srd.feats)
    for spell in srd.spells.values():
        owner = f"spells.yaml, запись {spell.id!r}"
        problems += _unknown_ids(owner, "classes", spell.classes, srd.classes)
    for monster in srd.monsters.values():
        action_ids = {action.id for action in monster.actions}
        for action in monster.actions:
            owner = f"monsters.yaml, запись {monster.id!r}, действие {action.id!r}"
            problems += _unknown_ids(owner, "multiattack", action.multiattack, action_ids)
    return problems


def _check_default_spells(owner: str, cls: ClassDef, srd: SrdContent) -> list[str]:
    """Заклинания быстрого режима должны существовать и входить в список заклинаний класса."""
    problems = _unknown_ids(owner, "default_spells", cls.default_spells, srd.spells)
    spell_list = cls.spellcasting.spell_list if cls.spellcasting else None
    for spell_id in dict.fromkeys(cls.default_spells):
        spell = srd.spells.get(spell_id)
        if spell is not None and (spell_list is None or spell_list not in spell.classes):
            problems.append(f"{owner}: default_spells содержит {spell_id!r} не из списка класса")
    return problems


def _load_dir(directory: Traversable) -> SrdContent:
    """Читает все таблицы из папки. Сначала собирает проблемы формы по всем файлам сразу;
    ссылки между таблицами проверяются, только когда форма корректна: иначе запись,
    не прошедшая проверку формы, дала бы ложную жалобу на «несуществующий id»."""
    problems: list[str] = []
    tables: dict[str, dict[str, Any]] = {}
    for name in SrdContent.model_fields:
        file_name = f"{name}.yaml"
        entries = _read_entries(directory / file_name, problems)
        tables[name] = _index_by_id(file_name, entries, problems)
    try:
        srd = SrdContent.model_validate(tables)
    except ValidationError as exc:
        raise ContentError([*problems, *map(_describe_schema_error, exc.errors())]) from None
    if problems:
        raise ContentError(problems)
    if problems := _check_references(srd):
        raise ContentError(problems)
    return srd


def _builtin_srd_dir() -> Traversable:
    return files("dnd_engine") / "data" / "srd"


@cache
def _load_builtin_srd() -> SrdContent:
    return _load_dir(_builtin_srd_dir())


def load_srd(path: Path | None = None) -> SrdContent:
    """Читает {classes,species,backgrounds,feats,spells,items,monsters,conditions}.yaml.

    Без пути — встроенные данные пакета (`data/srd/`), результат кешируется;
    с путём — указанная папка, каждый вызов читает её заново.
    """
    return _load_builtin_srd() if path is None else _load_dir(path)
