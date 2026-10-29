"""Правила создания персонажей (план B6.4, B8.s3): опции формы, случайная раскладка очков, три
режима, выбор игрока, описание от модели.

Слой не знает про HTTP: он принимает сессию базы, пользователя и зависимости (`Deps`), возвращает
готовые ответы и отказывает исключениями `GameError` из лобби (`NotFound`, `Conflict`, `Invalid`;
как они становятся кодами ответов, решает `games.api`). Числа персонажа считает только движок
(`build_character`, `quick_character`, `random_character`): сервер сохраняет результат, а модель
дописывает имя и текст. Клиент присылает выбор (`CharacterChoices`), но не персонажа.

Характеристики героя задаются только в рамках 27 очков (решение владельца R9): броска `4d6kh3` нет.
«Распределить случайно» — `random_abilities`: сервер раскладывает те же 27 очков по классу героя
(`random_point_buy`, свой генератор `deps.rng`), ничего не записывая и не ограничивая нажатия.

Выбор игрока хранится вместе с героем (решение владельца R10, `PlayerCharacter.choices`) и отдаётся
только ему самому (`my_character`): форма «Изменить героя» открывается заполненной. Новое создание
заменяет героя вместе с выбором одной транзакцией. Выбор нигде, кроме `my_character`, не читается.

Создание идёт в два приёма, чтобы ожидание модели не держало игру. Под блокировкой игры сервер
проверяет игру, собирает и сохраняет персонажа (с замысла игрока, без описания) и рассылает лобби.
Блокировка отпускается, и только потом идёт запрос к модели: пока она думает, остальные игроки
отмечают готовность и входят в игру. Описание дописывается вторым коротким приёмом под блокировкой,
если игра всё ещё в лобби и персонаж не заменён. Отказ модели и старт игры за время ожидания не
отменяют персонажа: клиент получает его без описания и с `flavor_error`. Ответ 409 остаётся только
тому запросу, чей персонаж уже заменён другим.

Случайный герой создаётся только в два шага (решение владельца R1, спека DND-2 §15):
`random_preview` бросает героя и запоминает его (`RandomPreviews`), ничего не записывая и не зовя
модель, а `mode: random` с `preview_id` («Беру») создаёт персонажа ровно из запомненного героя и
дописывает описание, как обычно. Числа второго шага не бросаются заново; прежнего пути «бросить и
сразу сохранить» нет (без `preview_id` маршрут отвечает 422).

Вызовы модели из действий игрока платные, поэтому они ограничены (`ModelCalls`): у игрока в игре
одновременно не больше одного вызова (второй запрос — 429 до любых изменений) и не больше
`MAX_MODEL_CALLS` за лобби (после этого быстрый и случайный режимы сохраняют персонажа без описания,
а «помоги написать» отвечает 429). Ожидая модель, запрос не держит соединение с базой (`_ask`).
"""

import logging
import secrets
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_ABILITY, SKILL_NAMES_RU, Ability, Skill
from dnd_engine.character import Character
from dnd_engine.content import (
    BackgroundDef,
    ClassDef,
    FeatureDef,
    SpeciesDef,
    SpellDef,
    SpellSchool,
    SrdContent,
)
from dnd_engine.creation import (
    BACKGROUND_BONUS_SHAPES,
    CHARACTER_LEVEL,
    EXPERTISE_FEATURE,
    EXPERTISE_PICKS,
    FIGHTING_STYLES,
    POINT_BUY_BUDGET,
    POINT_BUY_COST,
    SKILLFUL_TRAIT,
    STANDARD_ARRAY,
    TRAIT_SKILLS,
    UNNAMED_CHARACTER_NAME,
    CharacterChoices,
    CharacterValidationError,
    build_character,
    class_spell_options,
    quick_character,
    random_character,
    random_point_buy,
)
from dnd_engine.rng import Rng
from dnd_engine.units import meters_text
from dnd_server.characters.flavor import (
    MAX_REASON_CHARS,
    BackstoryOut,
    FlavorLimitReached,
    FlavorUnavailable,
    PersonaOut,
    ask_model,
    backstory_request,
    flavor_request,
    with_persona,
)
from dnd_server.characters.previews import RandomPreviews
from dnd_server.config import Settings
from dnd_server.db.models import Game, GamePhase, GamePlayer, PlayerCharacter, User
from dnd_server.dm.provider import game_provider
from dnd_server.games.hooks import Broadcaster, send_to_game
from dnd_server.games.schemas import GameOut
from dnd_server.games.service import (
    MAX_MODEL_CALLS,
    Conflict,
    GameLocks,
    Invalid,
    ModelCalls,
    NotFound,
    get_game,
    lobby_event,
)
from dnd_server.llm.base import JsonRequest, LLMProvider, printable
from dnd_server.llm.factory import ConfigError
from dnd_server.llm.internal_api import TurnTokens

logger = logging.getLogger(__name__)

# --- id персонажа ---


def new_character_id() -> str:
    """Id нового персонажа: «pc_» и 16 шестнадцатеричных знаков, выдаёт только сервер.

    Формат нарочно не совпадает с id пользователей (uuid с дефисами) и со служебными источниками
    движка («all», пустая строка, `zero_hp`, `hide`): id персонажа попадает в события, адресаты
    личных сообщений и источники состояний, и совпадение выдало бы чужому игроку чужие тайны.
    """
    return f"pc_{secrets.token_hex(8)}"


# --- опции формы: только то, что нужно мастеру создания, на русском ---
# У каждого варианта есть `details` для подробной карточки (контракт блока 7): верхние поля нужны
# логике формы, `details` — карточке, поэтому часть данных нарочно повторяется. Имена моделей
# подробностей совпадают с интерфейсами клиента (`api/types.ts`).


class AbilityOption(BaseModel):
    id: Ability
    name: str


class SkillDetails(BaseModel):
    ability: Ability
    summary: str  # что проверяет навык и примеры применения


class SkillOption(BaseModel):
    id: Skill
    name: str
    ability: Ability
    details: SkillDetails


class SummaryDetails(BaseModel):
    summary: str


class Described(BaseModel):
    """Запись с названием и сутью: черта предыстории или боевой стиль."""

    id: str
    name: str
    summary: str
    details: SummaryDetails


class FeatureOption(BaseModel):
    name: str
    summary: str


class ItemLine(BaseModel):
    id: str
    name: str
    qty: int


class SpellDetails(BaseModel):
    school: str
    casting_time: str
    range_text: str  # дистанция в метрах
    duration: str
    summary: str


class SpellOption(BaseModel):
    id: str
    name: str
    level: int
    summary: str
    details: SpellDetails


class SpellcastingOption(BaseModel):
    ability: Ability
    cantrips: int  # сколько заговоров выбрать
    prepared: int  # сколько заклинаний выбрать
    cantrip_options: list[SpellOption]
    spell_options: list[SpellOption]


class SkillChoicesDetail(BaseModel):
    # `from` — слово Python: поле называется `from_`, а в ответ уходит под именем контракта. Модель
    # принимает и `from`, чтобы собраться заново из собственного ответа, и `from_` для кода сервера.
    model_config = ConfigDict(serialize_by_alias=True)

    count: int
    from_: list[Skill] = Field(
        validation_alias=AliasChoices("from", "from_"), serialization_alias="from"
    )


class SpellcastingDetail(BaseModel):
    ability: Ability
    cantrips: int
    prepared: int


class ClassFeatureDetail(BaseModel):
    level: int
    name: str
    summary: str
    mechanical: bool  # действует в бою: особенность учитывает движок


class ClassDetails(BaseModel):
    hit_die: int
    primary: Ability
    saves: list[Ability]
    armor: list[str]  # русские подписи; пустой список — класс без доспехов
    weapons: list[str]
    skill_choices: SkillChoicesDetail
    features: list[ClassFeatureDetail]
    spellcasting: SpellcastingDetail | None  # числа на уровне героя


class ClassOption(BaseModel):
    id: str
    name: str
    hit_die: int
    saving_throws: list[Ability]
    skill_choices: int
    skill_options: list[Skill]
    expertise_picks: int  # 0 — у класса нет компетентности
    fighting_styles: list[str]
    starting_equipment: list[ItemLine]
    features: list[FeatureOption]
    spellcasting: SpellcastingOption | None
    details: ClassDetails


class TraitDetail(BaseModel):
    name: str
    summary: str
    mechanical: bool  # действует в бою: черту учитывает движок


class SpeciesDetails(BaseModel):
    name_en: str  # английское название под русским на карточке
    description: str
    size: str  # русская подпись размера
    speed: int  # футы, число правил: игроку показывается `speed_text`
    speed_text: str  # скорость в метрах
    traits: list[TraitDetail]


class SpeciesOption(BaseModel):
    id: str
    name: str
    traits: list[FeatureOption]
    extra_skill: bool  # нужен навык на выбор (Умелость человека)
    granted_skills: list[Skill]  # навыки, которые раса даёт сама
    details: SpeciesDetails


class FeatDetail(BaseModel):
    id: str
    name: str
    summary: str


class BackgroundDetails(BaseModel):
    abilities: list[Ability]  # три характеристики для прибавки
    skills: list[Skill]
    tool: str  # владение инструментом, русский текст из данных
    feat: FeatDetail
    equipment: list[ItemLine]


class BackgroundOption(BaseModel):
    id: str
    name: str
    ability_options: list[Ability]
    skills: list[Skill]  # навыки, которые предыстория даёт сама
    feat: str  # id черты из `feats`
    equipment: list[ItemLine]
    details: BackgroundDetails


class PointBuyOption(BaseModel):
    budget: int
    costs: dict[int, int]  # значение характеристики -> цена в очках


class CreationOptions(BaseModel):
    level: int
    abilities: list[AbilityOption]
    skills: list[SkillOption]
    standard_array: list[int]
    point_buy: PointBuyOption
    background_bonus_shapes: list[list[int]]
    classes: list[ClassOption]
    species: list[SpeciesOption]
    backgrounds: list[BackgroundOption]
    feats: list[Described]
    fighting_styles: list[Described]


# --- подписи кодов SRD для подробных карточек (контракт блока 7, §2.7) ---
# Движок хранит коды и числа правил, а игрок читает русские подписи: отображение — дело сервера.

SIZE_NAMES: Mapping[str, str] = MappingProxyType({"medium": "Средний", "small": "Маленький"})
ARMOR_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "light": "Лёгкие доспехи",
        "medium": "Средние доспехи",
        "heavy": "Тяжёлые доспехи",
        "shield": "Щиты",
    }
)
WEAPON_NAMES: Mapping[str, str] = MappingProxyType(
    {"simple": "Простое оружие", "martial": "Воинское оружие"}
)
SCHOOL_NAMES: Mapping[SpellSchool, str] = MappingProxyType(
    {
        "abjuration": "Ограждение",
        "conjuration": "Вызов",
        "divination": "Прорицание",
        "enchantment": "Очарование",
        "evocation": "Воплощение",
        "illusion": "Иллюзия",
        "necromancy": "Некромантия",
        "transmutation": "Преобразование",
    }
)
CASTING_TIMES: Mapping[str, str] = MappingProxyType(
    {"action": "Действие", "bonus_action": "Бонусное действие"}
)
OUT_OF_COMBAT_CASTING = "Несколько минут, только вне боя"
# Движок решает дистанцию заклинания зоной, а не точными футами SRD: игрок видит зону и её границу.
# Числа метров — из `meters_text` движка: 5 футов (вплотную) и 60 футов (граница «близко»).
SPELL_RANGES: Mapping[str, str] = MappingProxyType(
    {
        "self": "На себя",
        "touch": "Касание",
        "engaged": f"Вплотную, {meters_text(5)}",
        "near": f"Близко, до {meters_text(60)}",
        "far": f"Далеко, дальше {meters_text(60)}",
    }
)
ROUNDS_PER_MINUTE = 10
ROUNDS_PER_HOUR = 600


def _counted(number: int, one: str, few: str, many: str) -> str:
    """Число со словом в нужной форме: «1 час», «2 часа», «5 часов», «11 часов», «21 час»."""
    if number % 10 == 1 and number % 100 != 11:
        word = one
    elif number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{number} {word}"


def duration_text(rounds: int | None, concentration: bool) -> str:
    """Длительность заклинания для карточки: часы, минуты или раунды и пометка концентрации."""
    if rounds is None:
        text = "Мгновенно"
    elif rounds % ROUNDS_PER_HOUR == 0:
        text = _counted(rounds // ROUNDS_PER_HOUR, "час", "часа", "часов")
    elif rounds % ROUNDS_PER_MINUTE == 0:
        text = _counted(rounds // ROUNDS_PER_MINUTE, "минута", "минуты", "минут")
    else:
        text = _counted(rounds, "раунд", "раунда", "раундов")
    return f"{text}, концентрация" if concentration else text


def casting_time_text(casting_time: str, out_of_combat_only: bool) -> str:
    """Время сотворения: заклинание только вне боя сотворяется минуты, какое бы ни было действие."""
    return OUT_OF_COMBAT_CASTING if out_of_combat_only else CASTING_TIMES[casting_time]


def _item_lines(item_ids: Sequence[str], srd: SrdContent) -> list[ItemLine]:
    """Предметы набора по названиям; повторы складываются в количество."""
    return [
        ItemLine(id=item_id, name=srd.items[item_id].name, qty=qty)
        for item_id, qty in Counter(item_ids).items()
    ]


def _spell_option(spell: SpellDef) -> SpellOption:
    return SpellOption(
        id=spell.id,
        name=spell.name,
        level=spell.level,
        summary=spell.summary,
        details=SpellDetails(
            school=SCHOOL_NAMES[spell.school],
            casting_time=casting_time_text(spell.casting_time, spell.out_of_combat_only),
            range_text=SPELL_RANGES[spell.range],
            duration=duration_text(spell.duration_rounds, spell.concentration),
            summary=spell.summary,
        ),
    )


def _spellcasting_option(cls: ClassDef, srd: SrdContent) -> SpellcastingOption | None:
    spellcasting = cls.spellcasting
    if spellcasting is None:
        return None
    # Варианты те же, по которым `build_character` проверяет выбор: их считает движок.
    options = class_spell_options(spellcasting, srd)
    return SpellcastingOption(
        ability=spellcasting.ability,
        cantrips=spellcasting.cantrips_known[CHARACTER_LEVEL],
        prepared=spellcasting.prepared[CHARACTER_LEVEL],
        cantrip_options=[_spell_option(s) for s in options.cantrips],
        spell_options=[_spell_option(s) for s in options.spells],
    )


def _class_details(
    cls: ClassDef, features: Sequence[FeatureDef], spellcasting: SpellcastingOption | None
) -> ClassDetails:
    return ClassDetails(
        hit_die=cls.hit_die,
        primary=cls.ability_priority[0],
        saves=list(cls.saving_throws),
        armor=[ARMOR_NAMES[armor] for armor in cls.armor],
        weapons=[WEAPON_NAMES[weapon] for weapon in cls.weapons],
        skill_choices=SkillChoicesDetail(count=cls.skill_choices, from_=list(cls.skill_options)),
        features=[
            ClassFeatureDetail(
                level=f.level, name=f.name, summary=f.summary, mechanical=f.mechanical
            )
            for f in features
        ],
        spellcasting=None
        if spellcasting is None
        else SpellcastingDetail(
            ability=spellcasting.ability,
            cantrips=spellcasting.cantrips,
            prepared=spellcasting.prepared,
        ),
    )


def _class_option(cls: ClassDef, srd: SrdContent) -> ClassOption:
    has_expertise = any(
        feature.id == EXPERTISE_FEATURE and feature.level <= CHARACTER_LEVEL
        for feature in cls.features
    )
    features = [f for f in cls.features if f.level <= CHARACTER_LEVEL]
    spellcasting = _spellcasting_option(cls, srd)
    return ClassOption(
        id=cls.id,
        name=cls.name,
        hit_die=cls.hit_die,
        saving_throws=list(cls.saving_throws),
        skill_choices=cls.skill_choices,
        skill_options=list(cls.skill_options),
        expertise_picks=EXPERTISE_PICKS if has_expertise else 0,
        fighting_styles=list(cls.fighting_styles),
        starting_equipment=_item_lines(cls.starting_equipment, srd),
        features=[FeatureOption(name=f.name, summary=f.summary) for f in features],
        spellcasting=spellcasting,
        details=_class_details(cls, features, spellcasting),
    )


def _species_option(species: SpeciesDef) -> SpeciesOption:
    return SpeciesOption(
        id=species.id,
        name=species.name,
        traits=[FeatureOption(name=t.name, summary=t.summary) for t in species.traits],
        extra_skill=any(trait.id == SKILLFUL_TRAIT for trait in species.traits),
        granted_skills=[TRAIT_SKILLS[t.id] for t in species.traits if t.id in TRAIT_SKILLS],
        details=SpeciesDetails(
            name_en=species.name_en,
            description=species.description,
            size=SIZE_NAMES[species.size],
            speed=species.speed,
            speed_text=meters_text(species.speed),
            traits=[
                TraitDetail(name=t.name, summary=t.summary, mechanical=t.mechanical)
                for t in species.traits
            ],
        ),
    )


def _background_option(background: BackgroundDef, srd: SrdContent) -> BackgroundOption:
    feat = srd.feats[background.feat]
    equipment = _item_lines(background.equipment, srd)
    return BackgroundOption(
        id=background.id,
        name=background.name,
        ability_options=list(background.ability_options),
        skills=list(background.skills),
        feat=background.feat,
        equipment=equipment,
        details=BackgroundDetails(
            abilities=list(background.ability_options),
            skills=list(background.skills),
            tool=background.tool,
            feat=FeatDetail(id=feat.id, name=feat.name, summary=feat.summary),
            equipment=equipment,
        ),
    )


def _skill_option(skill: Skill, srd: SrdContent) -> SkillOption:
    ability = SKILL_ABILITY[skill]
    return SkillOption(
        id=skill,
        name=SKILL_NAMES_RU[skill],
        ability=ability,
        details=SkillDetails(ability=ability, summary=srd.skills[skill].summary),
    )


def _described(id_: str, name: str, summary: str) -> Described:
    return Described(id=id_, name=name, summary=summary, details=SummaryDetails(summary=summary))


def _fighting_styles(srd: SrdContent) -> list[Described]:
    """Боевые стили, которые есть хотя бы у одного класса, в порядке появления."""
    used = dict.fromkeys(style for cls in srd.classes.values() for style in cls.fighting_styles)
    return [
        _described(style, FIGHTING_STYLES[style].name, FIGHTING_STYLES[style].summary)
        for style in used
    ]


def creation_options(srd: SrdContent) -> CreationOptions:
    return CreationOptions(
        level=CHARACTER_LEVEL,
        abilities=[AbilityOption(id=a, name=ABILITY_NAMES_RU[a]) for a in Ability],
        skills=[_skill_option(s, srd) for s in Skill],
        standard_array=list(STANDARD_ARRAY),
        point_buy=PointBuyOption(budget=POINT_BUY_BUDGET, costs=dict(POINT_BUY_COST)),
        background_bonus_shapes=[list(shape) for shape in BACKGROUND_BONUS_SHAPES],
        classes=[_class_option(cls, srd) for cls in srd.classes.values()],
        species=[_species_option(s) for s in srd.species.values()],
        backgrounds=[_background_option(b, srd) for b in srd.backgrounds.values()],
        feats=[_described(f.id, f.name, f.summary) for f in srd.feats.values()],
        fighting_styles=_fighting_styles(srd),
    )


# --- зависимости и общие проверки ---


@dataclass(frozen=True)
class Deps:
    """Что нужно действиям с персонажами от приложения.

    Собирается при каждом запросе из `app.state`, поэтому тесты, хаб и мастер подменяют части
    там же."""

    locks: GameLocks
    model_calls: ModelCalls
    broadcaster: Broadcaster
    settings: Settings
    turn_tokens: TurnTokens | None
    srd: SrdContent
    rng: Rng
    previews: RandomPreviews


def _require_lobby(game: GameOut) -> None:
    if game.phase != GamePhase.LOBBY:
        raise Conflict("Игра уже началась: персонажа больше нельзя менять.")


# --- случайная раскладка очков ---


class RandomAbilitiesOut(BaseModel):
    method: Literal["point_buy"] = "point_buy"  # способ, в который клиент кладёт раскладку
    scores: dict[Ability, int]  # базовые значения 8–15 ровно на 27 очков, до прибавки предыстории


async def random_abilities(
    session: AsyncSession, user: User, game_id: str, deps: Deps, *, class_id: str
) -> RandomAbilitiesOut:
    """«Распределить случайно»: раскладка 27 очков для класса героя, больше — ключевым
    характеристикам класса. Бросает сервер (`deps.rng`); ничего не записывается и ничего не
    рассылается, нажатия не ограничены: запас очков у всех игроков один и тот же. Права и фаза те
    же, что у создания персонажа: участник игры, лобби. Блокировка игры не нужна: записи нет."""
    _require_lobby(await get_game(session, user, game_id))
    try:
        layout = random_point_buy(deps.rng, deps.srd, class_id)
    except CharacterValidationError as error:
        raise Invalid("Неизвестный класс.", error.problems) from None
    # Ключи идут в порядке характеристик, а не приоритета класса: ответ выглядит одинаково у всех.
    return RandomAbilitiesOut(scores={ability: layout[ability] for ability in Ability})


# --- создание персонажа ---


@dataclass(frozen=True)
class Slot:
    """Что известно под блокировкой игры о месте нового персонажа."""

    character_id: str


class CreatedOut(BaseModel):
    character: Character
    # Русский текст, если описание от модели не получилось; персонаж при этом сохранён.
    flavor_error: str | None


# --- выбор игрока: что хранится рядом с героем и как возвращается владельцу ---

Mode = Literal["detailed", "quick", "random"]


class QuickChoices(BaseModel):
    """Выбор игрока в быстром режиме: ровно то, что он прислал. Имя пусто, если игрок его не дал
    (его придумала модель, и в выбор оно не входит)."""

    model_config = ConfigDict(extra="forbid")

    class_id: str
    species: str
    concept: str
    name: str | None


class MineOut(BaseModel):
    """Свой герой игрока и выбор, по которому он создан.

    `mode` и `choices` пусты у героя, созданного до миграции 0003 (выбор не записывался) и у героя,
    чей сохранённый выбор уже не читается (`_mine`); у случайного героя пуст только `choices`.
    `choices` — тело, которое клиент может отправить обратно в `POST .../characters`
    (`mode: detailed` → `choices`; быстрый режим — поля выбора вместе с `mode: quick`)."""

    mode: Mode | None
    character: Character
    choices: CharacterChoices | QuickChoices | None


def _stored_choice(mode: Mode, choices: BaseModel | None) -> dict[str, Any]:
    """Выбор игрока в том виде, в каком он лежит в `PlayerCharacter.choices`."""
    return {"mode": mode, "choices": None if choices is None else choices.model_dump(mode="json")}


def _mine(row: PlayerCharacter) -> MineOut:
    """Герой и его выбор. Выбор читается строго, но модели выбора могли измениться после записи
    героя: тогда ответ тот же, что у героя до миграции 0003 (способа и выбора нет, форма откроется
    пустой), а не 500, и в журнал идёт предупреждение. Выбор в журнал не пишется: текст ошибки
    pydantic пересказывает значения."""
    character = Character.model_validate(row.data)
    stored = row.choices or {}
    mode, raw = stored.get("mode"), stored.get("choices")
    try:
        choices: CharacterChoices | QuickChoices | None = None
        if raw is not None:
            choices = (CharacterChoices if mode == "detailed" else QuickChoices).model_validate(raw)
        return MineOut(mode=mode, character=character, choices=choices)
    except ValidationError:
        logger.warning(
            "Сохранённый выбор героя %s игры %s не читается (формат выбора изменился): "
            "форма «Изменить героя» откроется пустой.",
            row.id,
            row.game_id,
        )
        return MineOut(mode=None, character=character, choices=None)


async def my_character(session: AsyncSession, user: User, game_id: str) -> MineOut:
    """Герой игрока и его выбор для формы «Изменить героя». Только свой: строка героя берётся по
    ссылке из строки самого игрока, чужой выбор отсюда не достать. Участнику своей игры отвечает в
    любой фазе (чтение безвредно); не участнику игра неотличима от несуществующей (404)."""
    await get_game(session, user, game_id)
    player = await session.get(GamePlayer, (game_id, user.id))
    row = (
        None
        if player.character_id is None
        else await session.get(PlayerCharacter, player.character_id)
    )
    if row is None:
        raise NotFound("У вас ещё нет героя в этой игре.")
    return _mine(row)


async def _taken_classes(session: AsyncSession, game_id: str, user_id: str) -> set[str]:
    """Классы чужих персонажей; прежний персонаж самого игрока заменяется и занятым не считается."""
    rows = await session.scalars(
        select(PlayerCharacter.data).where(
            PlayerCharacter.game_id == game_id, PlayerCharacter.user_id != user_id
        )
    )
    return {data["class_id"] for data in rows}


async def _store(
    session: AsyncSession, player: GamePlayer, character: Character, choice: dict[str, Any]
) -> None:
    """Новый персонаж заменяет прежнего вместе с выбором игрока одной транзакцией; отметка «Готов»
    сбрасывается."""
    previous = player.character_id
    session.add(
        PlayerCharacter(
            id=character.id,
            game_id=player.game_id,
            user_id=player.user_id,
            data=character.model_dump(mode="json"),
            choices=choice,
        )
    )
    await session.flush()  # внешний ключ: строка персонажа должна появиться раньше ссылки на неё
    player.character_id = character.id
    player.ready = False
    await session.flush()
    if previous is not None:
        await session.execute(delete(PlayerCharacter).where(PlayerCharacter.id == previous))
    await session.commit()


async def _store_new(
    session: AsyncSession,
    user: User,
    game_id: str,
    deps: Deps,
    build: Callable[[Slot], Character],
    choice: dict[str, Any],
) -> tuple[GameOut, Character]:
    """Под блокировкой игры: проверить игру, собрать персонажа, сохранить, разослать лобби."""
    async with deps.locks.lock(game_id):
        game = await get_game(session, user, game_id)
        _require_lobby(game)
        player = await session.get(GamePlayer, (game_id, user.id))
        slot = Slot(character_id=new_character_id())
        try:
            built = build(slot)
        except CharacterValidationError as error:
            raise Invalid("Персонаж не прошёл проверку правил.", error.problems) from None
        # Владелец нужен хабу: личное сообщение адресовано персонажу, а получает его игрок.
        character = built.model_copy(update={"player_id": user.id})
        await _store(session, player, character, choice)
        await send_to_game(
            deps.broadcaster, game_id, lobby_event(await get_game(session, user, game_id))
        )
    return game, character


async def _create(
    session: AsyncSession,
    user: User,
    game_id: str,
    deps: Deps,
    build: Callable[[Slot], Character],
    choice: dict[str, Any],
    *,
    describe: bool,
    keep_name: bool = False,
) -> CreatedOut:
    """Общий ход создания: сохранить персонажа с выбором игрока и, если нужно, дописать описание
    от модели.

    Место под вызов модели занимается до любых изменений: если игрок ещё ждёт прошлый ответ,
    запрос отклоняется (429), и персонаж остаётся прежним."""
    model_call = deps.model_calls.running(game_id, user.id) if describe else nullcontext()
    with model_call:
        game, character = await _store_new(session, user, game_id, deps, build, choice)
        if not describe:
            return CreatedOut(character=character, flavor_error=None)
        return await _describe(session, user, game, deps, character, keep_name=keep_name)


async def create_detailed(
    session: AsyncSession, user: User, game_id: str, deps: Deps, choices: CharacterChoices
) -> CreatedOut:
    """Подробный режим: игрок выбрал всё сам, движок проверяет выбор и считает числа."""

    def build(slot: Slot) -> Character:
        return build_character(choices, deps.srd, character_id=slot.character_id)

    choice = _stored_choice("detailed", choices)
    return await _create(session, user, game_id, deps, build, choice, describe=False)


async def create_quick(
    session: AsyncSession,
    user: User,
    game_id: str,
    deps: Deps,
    *,
    class_id: str,
    species: str,
    concept: str,
    name: str | None,
) -> CreatedOut:
    """Быстрый режим: класс, раса и замысел игрока; имя придумает модель, если игрок его не дал."""

    def build(slot: Slot) -> Character:
        return quick_character(
            class_id,
            species,
            deps.srd,
            name=name or UNNAMED_CHARACTER_NAME,
            concept=concept,
            character_id=slot.character_id,
        )

    # В выбор идёт то, что прислал игрок: имя, которое придумает модель, в него не попадает.
    choice = _stored_choice(
        "quick", QuickChoices(class_id=class_id, species=species, concept=concept, name=name)
    )
    return await _create(
        session, user, game_id, deps, build, choice, describe=True, keep_name=name is not None
    )


async def create_random(
    session: AsyncSession,
    user: User,
    game_id: str,
    deps: Deps,
    *,
    preview_id: str,
) -> CreatedOut:
    """Случайный режим («Беру»): герой, которого сервер показал этому игроку в `random_preview`,
    модель его описывает. Броска нет, числа те же. Предпросмотр расходуется под блокировкой игры в
    момент сборки, после проверок игры и места под вызов модели; отказ раньше (игра не в лобби,
    429) его не тратит. Если сохранение в базу после этого оборвётся (500), игрок бросит судьбу
    ещё раз."""

    def build(_slot: Slot) -> Character:
        return deps.previews.take(game_id, user.id, preview_id)

    return await _create(
        session, user, game_id, deps, build, _stored_choice("random", None), describe=True
    )


class RandomPreviewOut(BaseModel):
    preview_id: str
    # Лист героя, как его соберёт движок: имени и описания ещё нет, их пишет нейронка после «Беру».
    character: Character


async def random_preview(
    session: AsyncSession, user: User, game_id: str, deps: Deps
) -> RandomPreviewOut:
    """Предпросмотр случайного героя: бросает сервер, в базу ничего не пишется, модель не зовётся.

    Новый предпросмотр отменяет прежний игрока в этой игре. Класс берётся из ещё не занятых
    отрядом, как у прежнего случайного режима; это предпочтение, а не правило, поэтому к «Беру»
    занятость класса не проверяется заново. Права и фаза те же, что у создания персонажа: участник
    игры, лобби. Частота бросков ограничена (`RandomPreviews.charge`, 429); проверка частоты идёт
    после проверки участия, чтобы счётчики заводились только у настоящих игроков."""
    _require_lobby(await get_game(session, user, game_id))
    deps.previews.charge(game_id, user.id)
    taken_classes = await _taken_classes(session, game_id, user.id)
    try:
        hero = random_character(
            deps.rng, deps.srd, taken_classes=taken_classes, character_id=new_character_id()
        )
    except CharacterValidationError as error:
        raise Invalid("Персонаж не прошёл проверку правил.", error.problems) from None
    # Владелец проставляется так же, как при сохранении: «Беру» не меняет в листе ни одного поля.
    hero = hero.model_copy(update={"player_id": user.id})
    shown = deps.previews.put(game_id, user.id, hero)
    return RandomPreviewOut(preview_id=shown.id, character=shown.character)


# --- описание от модели ---


def _provider_for(deps: Deps, game: GameOut) -> LLMProvider:
    """Провайдер игры (переопределение хоста или значение env) для действий игрока.

    Он собирается заново на каждый запрос: сборка не ходит в сеть, а провайдера для ведения игры
    держит мастер (`dm.provider.GameProviders`). Описание просит дешёвую модель сам запрос
    (`cheap`)."""
    try:
        return game_provider(
            deps.settings, provider=game.provider, model=game.model, turn_tokens=deps.turn_tokens
        )
    except ConfigError as error:
        raise FlavorUnavailable(
            "Нейронка для этой игры не настроена.", printable(str(error), MAX_REASON_CHARS)
        ) from None


async def _ask[T: BaseModel](
    session: AsyncSession,
    deps: Deps,
    game: GameOut,
    user: User,
    request: JsonRequest,
    schema: type[T],
) -> T:
    """Запрос к модели игры от имени игрока; отказ пишется в журнал и идёт дальше как
    `FlavorUnavailable`.

    Единственный путь к модели из действий игрока, поэтому здесь же: соединение с базой
    отпускается, вызов учитывается в пределе игрока (`FlavorLimitReached`, если он исчерпан) и
    пишется в журнал. Соединение отпускается до ожидания: ответ идёт до `FLAVOR_TIMEOUT_S`
    секунд, а запрос, державший соединение всё это время, исчерпал бы пул (15 соединений) и
    оставил бы без базы весь сервер. Коммит ничего не пишет, он лишь завершает транзакцию,
    открытую чтением; объекты сессии после него целы (`expire_on_commit=False`), а следующее
    обращение к базе откроет новую транзакцию. Вызов считается, когда провайдер уже собран:
    ненастроенная нейронка предел не тратит. Неудавшийся вызов предел тратит: он мог стоить
    денег."""
    await session.commit()
    try:
        provider = _provider_for(deps, game)
        number = deps.model_calls.charge(game.id, user.id)
        if number is None:
            raise FlavorLimitReached(
                f"Предел описаний исчерпан: не больше {MAX_MODEL_CALLS} вызовов нейронки "
                "на игрока, пока игра в лобби.",
                f"у игрока {user.id} исчерпан предел {MAX_MODEL_CALLS} вызовов",
            )
        # Тексты игрока и модели в журнал не идут: только кто, где, зачем и какой по счёту вызов.
        logger.info(
            "Вызов модели (%s): игра %s, игрок %s, вызов %d из %d.",
            request.purpose,
            game.id,
            user.id,
            number,
            MAX_MODEL_CALLS,
        )
        return await ask_model(provider, request, schema)
    except FlavorUnavailable as failure:
        logger.warning(
            "Нейронка не дала текст (%s): игра %s: %s", request.purpose, game.id, failure.reason
        )
        raise


async def _describe(
    session: AsyncSession,
    user: User,
    game: GameOut,
    deps: Deps,
    character: Character,
    *,
    keep_name: bool,
) -> CreatedOut:
    """Дописывает описание сохранённому персонажу. Блокировка игры здесь не держится, пока
    модель отвечает; отказ модели или старт игры за это время оставляют персонажа с замыслом,
    а клиент получает его с `flavor_error`: персонаж сохранён, поэтому 409 тут не нужен."""
    request = flavor_request(character, deps.srd, name_given=keep_name, profanity=game.profanity)
    try:
        persona = await _ask(session, deps, game, user, request, PersonaOut)
    except FlavorUnavailable as failure:
        return CreatedOut(
            character=character,
            flavor_error=f"{failure.message} Персонаж сохранён: описание можно написать самому.",
        )
    described = with_persona(character, persona, keep_name=keep_name)
    if not await _save_description(session, user, game.id, deps, described):
        return CreatedOut(
            character=character,
            flavor_error=(
                "Игра уже началась: описание героя не записано, персонаж сохранён без него."
            ),
        )
    return CreatedOut(character=described, flavor_error=None)


async def _save_description(
    session: AsyncSession, user: User, game_id: str, deps: Deps, character: Character
) -> bool:
    """Записывает описание, если за время ожидания модели ничего не изменилось.

    Сессия видела строки до ожидания, а другие запросы успели их изменить: поэтому читаем заново
    (`populate_existing`). Игра, ушедшая из лобби, описание не принимает (`False`): персонаж у
    игрока остаётся прежним. Заменённому персонажу возвращать нечего, это `Conflict`; замена
    проверяется первой, потому что она важнее: её персонаж уже не тот, что сохранён. Игроку,
    которого исключили или который вышел за это время, тоже `Conflict`: его персонаж удалён.

    Записанное описание меняет имя героя в лобби (`hero_name`: модель придумала имя быстрому
    герою), поэтому после коммита и под той же блокировкой всем уходит новый кадр `lobby`, как
    при сохранении героя. Отказы кадра не шлют: в лобби ничего не изменилось."""
    async with deps.locks.lock(game_id):
        game = await session.get(Game, game_id, populate_existing=True)
        player = await session.get(GamePlayer, (game_id, user.id), populate_existing=True)
        if player is None:  # вышел из лобби или исключён, пока модель писала описание
            raise Conflict("Вы больше не участник этой игры: описание отброшено.")
        if player.character_id != character.id:
            raise Conflict("Персонаж уже заменён другим запросом: это описание отброшено.")
        if game.phase != GamePhase.LOBBY:
            return False
        row = await session.get(PlayerCharacter, character.id, populate_existing=True)
        row.data = character.model_dump(mode="json")
        await session.commit()
        await send_to_game(
            deps.broadcaster, game_id, lobby_event(await get_game(session, user, game_id))
        )
        return True


async def backstory_help(
    session: AsyncSession,
    user: User,
    game_id: str,
    deps: Deps,
    *,
    class_id: str,
    species: str,
    concept: str,
    draft: str,
) -> BackstoryOut:
    """Предыстория от модели по замыслу и черновику игрока; ничего не сохраняет."""
    game = await get_game(session, user, game_id)
    _require_lobby(game)
    class_def = deps.srd.classes.get(class_id)
    species_def = deps.srd.species.get(species)
    if class_def is None:
        raise Invalid("Неизвестный класс.")
    if species_def is None:
        raise Invalid("Неизвестная раса.")
    request = backstory_request(
        class_def.name, species_def.name, concept, draft, profanity=game.profanity
    )
    # Проверки выше отказывают раньше места под вызов: неверный запрос ничего не тратит.
    with deps.model_calls.running(game_id, user.id):
        return await _ask(session, deps, game, user, request, BackstoryOut)
