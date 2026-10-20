"""Правила создания персонажей (план B6.4): опции формы, бросок, три режима, описание от модели.

Слой не знает про HTTP: он принимает сессию базы, пользователя и зависимости (`Deps`), возвращает
готовые ответы и отказывает исключениями `GameError` из лобби (`NotFound`, `Conflict`, `Invalid`;
как они становятся кодами ответов, решает `games.api`). Числа персонажа считает только движок
(`build_character`, `quick_character`, `random_character`): сервер сохраняет результат, а модель
дописывает имя и текст. Клиент присылает выбор (`CharacterChoices`), но не персонажа и не набор
броска: набор для способа `ROLL` берётся из `pending_rolls`, куда его положил сам сервер.

Создание идёт в два приёма, чтобы ожидание модели не держало игру. Под блокировкой игры сервер
проверяет игру, собирает и сохраняет персонажа (с замысла игрока, без описания) и рассылает лобби.
Блокировка отпускается, и только потом идёт запрос к модели: пока она думает, остальные игроки
отмечают готовность и входят в игру. Описание дописывается вторым коротким приёмом под блокировкой,
если игра всё ещё в лобби и персонаж не заменён. Отказ модели и старт игры за время ожидания не
отменяют персонажа: клиент получает его без описания и с `flavor_error`. Ответ 409 остаётся только
тому запросу, чей персонаж уже заменён другим.

Вызовы модели из действий игрока платные, поэтому они ограничены (`ModelCalls`): у игрока в игре
одновременно не больше одного вызова (второй запрос — 429 до любых изменений) и не больше
`MAX_MODEL_CALLS` за лобби (после этого быстрый и случайный режимы сохраняют персонажа без описания,
а «помоги написать» отвечает 429). Ожидая модель, запрос не держит соединение с базой (`_ask`).
"""

import logging
import secrets
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_ABILITY, SKILL_NAMES_RU, Ability, Skill
from dnd_engine.character import Character
from dnd_engine.content import ClassDef, SpeciesDef, SpellDef, SrdContent
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
    roll_ability_set,
)
from dnd_engine.rng import Rng
from dnd_server.characters.flavor import (
    MAX_REASON_CHARS,
    BackstoryOut,
    FlavorLimitReached,
    FlavorUnavailable,
    PersonaOut,
    ask_model,
    backstory_prompt,
    flavor_prompt,
    with_persona,
)
from dnd_server.config import Settings
from dnd_server.db.models import Game, GamePhase, GamePlayer, PendingRoll, PlayerCharacter, User
from dnd_server.dm.provider import game_provider
from dnd_server.games.hooks import Broadcaster, send_to_game
from dnd_server.games.schemas import GameOut
from dnd_server.games.service import (
    MAX_MODEL_CALLS,
    Conflict,
    GameLocks,
    Invalid,
    ModelCalls,
    get_game,
    lobby_event,
)
from dnd_server.llm.base import LLMProvider, printable
from dnd_server.llm.factory import ConfigError
from dnd_server.llm.internal_api import TurnTokens

logger = logging.getLogger(__name__)

FLAVOR_PURPOSE = "character.flavor"
BACKSTORY_PURPOSE = "character.backstory"

# --- id персонажа ---


def new_character_id() -> str:
    """Id нового персонажа: «pc_» и 16 шестнадцатеричных знаков, выдаёт только сервер.

    Формат нарочно не совпадает с id пользователей (uuid с дефисами) и со служебными источниками
    движка («all», пустая строка, `zero_hp`, `hide`): id персонажа попадает в события, адресаты
    личных сообщений и источники состояний, и совпадение выдало бы чужому игроку чужие тайны.
    """
    return f"pc_{secrets.token_hex(8)}"


# --- опции формы: только то, что нужно мастеру создания, на русском ---


class AbilityOption(BaseModel):
    id: Ability
    name: str


class SkillOption(BaseModel):
    id: Skill
    name: str
    ability: Ability


class Described(BaseModel):
    """Запись с названием и сутью: черта предыстории или боевой стиль."""

    id: str
    name: str
    summary: str


class FeatureOption(BaseModel):
    name: str
    summary: str


class ItemLine(BaseModel):
    id: str
    name: str
    qty: int


class SpellOption(BaseModel):
    id: str
    name: str
    level: int
    summary: str


class SpellcastingOption(BaseModel):
    ability: Ability
    cantrips: int  # сколько заговоров выбрать
    prepared: int  # сколько заклинаний выбрать
    cantrip_options: list[SpellOption]
    spell_options: list[SpellOption]


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


class SpeciesOption(BaseModel):
    id: str
    name: str
    traits: list[FeatureOption]
    extra_skill: bool  # нужен навык на выбор (Умелость человека)
    granted_skills: list[Skill]  # навыки, которые вид даёт сам


class BackgroundOption(BaseModel):
    id: str
    name: str
    ability_options: list[Ability]
    skills: list[Skill]  # навыки, которые предыстория даёт сама
    feat: str  # id черты из `feats`
    equipment: list[ItemLine]


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


def _item_lines(item_ids: Sequence[str], srd: SrdContent) -> list[ItemLine]:
    """Предметы набора по названиям; повторы складываются в количество."""
    return [
        ItemLine(id=item_id, name=srd.items[item_id].name, qty=qty)
        for item_id, qty in Counter(item_ids).items()
    ]


def _spell_option(spell: SpellDef) -> SpellOption:
    return SpellOption(id=spell.id, name=spell.name, level=spell.level, summary=spell.summary)


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


def _class_option(cls: ClassDef, srd: SrdContent) -> ClassOption:
    has_expertise = any(
        feature.id == EXPERTISE_FEATURE and feature.level <= CHARACTER_LEVEL
        for feature in cls.features
    )
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
        features=[
            FeatureOption(name=f.name, summary=f.summary)
            for f in cls.features
            if f.level <= CHARACTER_LEVEL
        ],
        spellcasting=_spellcasting_option(cls, srd),
    )


def _species_option(species: SpeciesDef) -> SpeciesOption:
    return SpeciesOption(
        id=species.id,
        name=species.name,
        traits=[FeatureOption(name=t.name, summary=t.summary) for t in species.traits],
        extra_skill=any(trait.id == SKILLFUL_TRAIT for trait in species.traits),
        granted_skills=[TRAIT_SKILLS[t.id] for t in species.traits if t.id in TRAIT_SKILLS],
    )


def _fighting_styles(srd: SrdContent) -> list[Described]:
    """Боевые стили, которые есть хотя бы у одного класса, в порядке появления."""
    used = dict.fromkeys(style for cls in srd.classes.values() for style in cls.fighting_styles)
    return [
        Described(
            id=style, name=FIGHTING_STYLES[style].name, summary=FIGHTING_STYLES[style].summary
        )
        for style in used
    ]


def creation_options(srd: SrdContent) -> CreationOptions:
    return CreationOptions(
        level=CHARACTER_LEVEL,
        abilities=[AbilityOption(id=a, name=ABILITY_NAMES_RU[a]) for a in Ability],
        skills=[SkillOption(id=s, name=SKILL_NAMES_RU[s], ability=SKILL_ABILITY[s]) for s in Skill],
        standard_array=list(STANDARD_ARRAY),
        point_buy=PointBuyOption(budget=POINT_BUY_BUDGET, costs=dict(POINT_BUY_COST)),
        background_bonus_shapes=[list(shape) for shape in BACKGROUND_BONUS_SHAPES],
        classes=[_class_option(cls, srd) for cls in srd.classes.values()],
        species=[_species_option(s) for s in srd.species.values()],
        backgrounds=[
            BackgroundOption(
                id=b.id,
                name=b.name,
                ability_options=list(b.ability_options),
                skills=list(b.skills),
                feat=b.feat,
                equipment=_item_lines(b.equipment, srd),
            )
            for b in srd.backgrounds.values()
        ],
        feats=[Described(id=f.id, name=f.name, summary=f.summary) for f in srd.feats.values()],
        fighting_styles=_fighting_styles(srd),
    )


# --- зависимости и общие проверки ---


@dataclass(frozen=True)
class Deps:
    """Что нужно действиям с персонажами от приложения; собирается при каждом запросе из `app.state`,
    поэтому тесты, хаб и мастер подменяют части там же."""

    locks: GameLocks
    model_calls: ModelCalls
    broadcaster: Broadcaster
    settings: Settings
    turn_tokens: TurnTokens | None
    srd: SrdContent
    rng: Rng


def _require_lobby(game: GameOut) -> None:
    if game.phase != GamePhase.LOBBY:
        raise Conflict("Игра уже началась: персонажа больше нельзя менять.")


# --- бросок характеристик ---


async def roll_abilities(session: AsyncSession, user: User, game_id: str, deps: Deps) -> list[int]:
    """Бросает набор характеристик на сервере и запоминает его; новый бросок заменяет прежний."""
    async with deps.locks.lock(game_id):
        _require_lobby(await get_game(session, user, game_id))
        values = roll_ability_set(deps.rng)
        pending = await session.get(PendingRoll, (game_id, user.id))
        if pending is None:
            session.add(PendingRoll(game_id=game_id, user_id=user.id, values=values))
        else:
            pending.values = values
        await session.commit()
    return values


# --- создание персонажа ---


@dataclass(frozen=True)
class Slot:
    """Что известно под блокировкой игры о месте нового персонажа."""

    character_id: str
    taken_classes: set[str]  # классы персонажей других игроков
    rolled_set: list[int] | None  # набор, который сервер бросил этому игроку


class CreatedOut(BaseModel):
    character: Character
    # Русский текст, если описание от модели не получилось; персонаж при этом сохранён.
    flavor_error: str | None


async def _taken_classes(session: AsyncSession, game_id: str, user_id: str) -> set[str]:
    """Классы чужих персонажей; прежний персонаж самого игрока заменяется и занятым не считается."""
    rows = await session.scalars(
        select(PlayerCharacter.data).where(
            PlayerCharacter.game_id == game_id, PlayerCharacter.user_id != user_id
        )
    )
    return {data["class_id"] for data in rows}


async def _rolled_set(session: AsyncSession, game_id: str, user_id: str) -> list[int] | None:
    pending = await session.get(PendingRoll, (game_id, user_id))
    return None if pending is None else list(pending.values)


async def _store(session: AsyncSession, player: GamePlayer, character: Character) -> None:
    """Новый персонаж заменяет прежнего одной транзакцией; отметка «Готов» сбрасывается."""
    previous = player.character_id
    session.add(
        PlayerCharacter(
            id=character.id,
            game_id=player.game_id,
            user_id=player.user_id,
            data=character.model_dump(mode="json"),
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
) -> tuple[GameOut, Character]:
    """Под блокировкой игры: проверить игру, собрать персонажа, сохранить, разослать лобби."""
    async with deps.locks.lock(game_id):
        game = await get_game(session, user, game_id)
        _require_lobby(game)
        player = await session.get(GamePlayer, (game_id, user.id))
        slot = Slot(
            character_id=new_character_id(),
            taken_classes=await _taken_classes(session, game_id, user.id),
            rolled_set=await _rolled_set(session, game_id, user.id),
        )
        try:
            built = build(slot)
        except CharacterValidationError as error:
            raise Invalid("Персонаж не прошёл проверку правил.", error.problems) from None
        # Владелец нужен хабу: личное сообщение адресовано персонажу, а получает его игрок.
        character = built.model_copy(update={"player_id": user.id})
        await _store(session, player, character)
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
    *,
    describe: bool,
    keep_name: bool = False,
) -> CreatedOut:
    """Общий ход создания: сохранить персонажа и, если нужно, дописать описание от модели.

    Место под вызов модели занимается до любых изменений: если игрок ещё ждёт прошлый ответ,
    запрос отклоняется (429), и персонаж остаётся прежним."""
    model_call = deps.model_calls.running(game_id, user.id) if describe else nullcontext()
    with model_call:
        game, character = await _store_new(session, user, game_id, deps, build)
        if not describe:
            return CreatedOut(character=character, flavor_error=None)
        return await _describe(session, user, game, deps, character, keep_name=keep_name)


async def create_detailed(
    session: AsyncSession, user: User, game_id: str, deps: Deps, choices: CharacterChoices
) -> CreatedOut:
    """Подробный режим: игрок выбрал всё сам, движок проверяет выбор и считает числа."""

    def build(slot: Slot) -> Character:
        return build_character(
            choices, deps.srd, rolled_set=slot.rolled_set, character_id=slot.character_id
        )

    return await _create(session, user, game_id, deps, build, describe=False)


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
    """Быстрый режим: класс, вид и замысел игрока; имя придумает модель, если игрок его не дал."""

    def build(slot: Slot) -> Character:
        return quick_character(
            class_id,
            species,
            deps.srd,
            name=name or UNNAMED_CHARACTER_NAME,
            concept=concept,
            character_id=slot.character_id,
        )

    return await _create(
        session, user, game_id, deps, build, describe=True, keep_name=name is not None
    )


async def create_random(session: AsyncSession, user: User, game_id: str, deps: Deps) -> CreatedOut:
    """Случайный режим: движок собирает персонажа из свободного класса, модель его описывает."""

    def build(slot: Slot) -> Character:
        return random_character(
            deps.rng, deps.srd, taken_classes=slot.taken_classes, character_id=slot.character_id
        )

    return await _create(session, user, game_id, deps, build, describe=True)


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
    purpose: str,
    prompt: str,
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
    ненастроенная нейронка предел не тратит. Неудавшийся вызов предел тратит: он мог стоить денег."""
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
            purpose,
            game.id,
            user.id,
            number,
            MAX_MODEL_CALLS,
        )
        return await ask_model(provider, purpose, prompt, schema)
    except FlavorUnavailable as failure:
        logger.warning("Нейронка не дала текст (%s): игра %s: %s", purpose, game.id, failure.reason)
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
    prompt = flavor_prompt(character, deps.srd, name_given=keep_name)
    try:
        persona = await _ask(session, deps, game, user, FLAVOR_PURPOSE, prompt, PersonaOut)
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
    которого исключили или который вышел за это время, тоже `Conflict`: его персонаж удалён."""
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
        raise Invalid("Неизвестный вид.")
    prompt = backstory_prompt(class_def.name, species_def.name, concept, draft)
    # Проверки выше отказывают раньше места под вызов: неверный запрос ничего не тратит.
    with deps.model_calls.running(game_id, user.id):
        return await _ask(session, deps, game, user, BACKSTORY_PURPOSE, prompt, BackstoryOut)
