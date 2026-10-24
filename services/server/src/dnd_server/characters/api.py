"""Маршруты персонажей (план B6.4): тонкая обёртка над `service`.

От клиента принимается только выбор игрока: `CharacterChoices` (подробный режим), класс, раса и
замысел (быстрый) или ничего (случайный; с id предпросмотра, который выдал сам сервер, — «Беру»).
Готового `Character` и id персонажа в телах запросов нет, а лишние поля отвергаются, так что их
нельзя прислать по ошибке или нарочно. Броска характеристик нет (решение владельца R9): вместо него
`random-abilities` раскладывает 27 очков по классу героя, а `mine` отдаёт игроку его героя и выбор,
по которому тот создан (R10).
Длины текстов и списков ограничены: они уходят в базу, ленту мастера и запросы к модели.

Ошибки правил (`CharacterValidationError`) и лобби (`GameError`) отдаёт общий обработчик
`games.api.handle_game_error`: `{"detail": "<русский текст>", "problems": [...]}`. Отказ модели в
«помоги написать» — 503, тайм-аут — 504, исчерпанный предел вызовов — 429, все с русским `detail`;
второй одновременный вызов того же игрока отклоняет `ModelCalls` как `GameError` (тоже 429).

Предпросмотр случайного героя (`random-preview`) модель не зовёт: он отвечает 409 вне лобби и 429
при слишком частых бросках, а «Беру» с чужим, устаревшим или уже взятым `preview_id` — 409.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import HTTPConnection

from dnd_engine.abilities import Skill
from dnd_engine.character import Persona
from dnd_engine.content import load_srd
from dnd_engine.creation import CharacterChoices
from dnd_server.auth.deps import app_settings
from dnd_server.characters import service
from dnd_server.characters.flavor import (
    APPEARANCE_MAX_CHARS,
    BACKSTORY_MAX_CHARS,
    CONCEPT_MAX_CHARS,
    DRAFT_MAX_CHARS,
    NAME_MAX_CHARS,
    SHORT_TEXT_MAX_CHARS,
    BackstoryOut,
    FlavorLimitReached,
    FlavorTimeout,
    FlavorUnavailable,
)
from dnd_server.characters.previews import PREVIEW_ID_PATTERN
from dnd_server.characters.service import (
    CreatedOut,
    CreationOptions,
    Deps,
    MineOut,
    RandomAbilitiesOut,
    RandomPreviewOut,
)
from dnd_server.games.api import (
    SessionDep,
    UserDep,
    get_broadcaster,
    get_locks,
    get_model_calls,
    get_turn_tokens,
)

router = APIRouter(prefix="/api")

# Идентификаторы правил (раса, класс, заклинание) короткие; длинная строка — не id, а мусор, который
# движок иначе повторил бы в тексте проблемы.
MAX_ID_CHARS = 64
MAX_SKILLS = len(Skill)
MAX_CANTRIPS = 10
MAX_PREPARED_SPELLS = 20

IdText = Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)]


# --- тела запросов ---


class PlayerPersona(Persona):
    """Описание героя, как его прислал игрок: те же поля, что у движка, но с пределами длины."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    concept: str = Field(default="", max_length=CONCEPT_MAX_CHARS)
    appearance: str = Field(default="", max_length=APPEARANCE_MAX_CHARS)
    traits: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    ideals: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    bonds: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    flaws: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    backstory: str = Field(default="", max_length=BACKSTORY_MAX_CHARS)


class PlayerChoices(CharacterChoices):
    """Выбор игрока в мастере создания: `CharacterChoices` движка с пределами длины и без лишних
    полей. Лишнее поле (`hp_max`, `id`, `rolled_set`) — ошибка, а не молча отброшенное значение."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    species: IdText
    class_id: IdText
    background: IdText
    skills: list[Skill] = Field(max_length=MAX_SKILLS)
    expertise: list[Skill] = Field(default=[], max_length=MAX_SKILLS)
    fighting_style: IdText | None = None
    cantrips: list[IdText] = Field(default=[], max_length=MAX_CANTRIPS)
    prepared_spells: list[IdText] = Field(default=[], max_length=MAX_PREPARED_SPELLS)
    persona: PlayerPersona = PlayerPersona()


class DetailedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["detailed"]
    choices: PlayerChoices


class QuickIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["quick"]
    class_id: IdText
    species: IdText
    concept: str = Field(min_length=1, max_length=CONCEPT_MAX_CHARS)
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_CHARS)


class RandomIn(BaseModel):
    """Случайный режим. Без `preview_id` герой бросается и сохраняется сразу (прежнее поведение);
    с ним («Беру») создаётся герой, которого сервер показал в `random-preview`. Героя, чисел и
    броска в теле нет, лишние поля отвергаются."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["random"]
    preview_id: str | None = Field(default=None, pattern=PREVIEW_ID_PATTERN, max_length=19)


CreateIn = Annotated[DetailedIn | QuickIn | RandomIn, Field(discriminator="mode")]


class BackstoryHelpIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    class_id: IdText
    species: IdText
    concept: str = Field(default="", max_length=CONCEPT_MAX_CHARS)
    draft: str = Field(default="", max_length=DRAFT_MAX_CHARS)


class RandomAbilitiesIn(BaseModel):
    """«Распределить случайно»: нужен только класс героя, числа выбирает сервер."""

    model_config = ConfigDict(extra="forbid")

    class_id: IdText


# --- зависимости ---


def get_deps(connection: HTTPConnection) -> Deps:
    """Части приложения для действий с персонажами; читаются из `app.state` при каждом запросе.

    Генератор бросков — `app.state.rng`: `create_app` кладёт туда `SystemRng`, а подменить его
    (на `SeededRng` в тестах) можно только кодом, настройки такой возможности не дают."""
    return Deps(
        locks=get_locks(connection),
        model_calls=get_model_calls(connection),
        broadcaster=get_broadcaster(connection),
        settings=app_settings(connection),
        turn_tokens=get_turn_tokens(connection),
        srd=load_srd(),
        rng=connection.app.state.rng,
        previews=connection.app.state.random_previews,
    )


DepsDep = Annotated[Deps, Depends(get_deps)]


# --- маршруты ---


@router.get("/content/creation-options")
async def creation_options(_user: UserDep, deps: DepsDep) -> CreationOptions:
    # Данные SRD берутся из `get_deps`, а не зависимостью `Depends(load_srd)`: у `load_srd` есть
    # параметр `path`, и FastAPI сделал бы его параметром query, то есть каталогом на выбор игрока.
    return service.creation_options(deps.srd)


@router.post("/games/{game_id}/characters/random-abilities")
async def random_abilities(
    game_id: str, body: RandomAbilitiesIn, user: UserDep, session: SessionDep, deps: DepsDep
) -> RandomAbilitiesOut:
    return await service.random_abilities(session, user, game_id, deps, class_id=body.class_id)


@router.get("/games/{game_id}/characters/mine")
async def my_character(game_id: str, user: UserDep, session: SessionDep) -> MineOut:
    return await service.my_character(session, user, game_id)


@router.post("/games/{game_id}/characters/random-preview")
async def random_preview(
    game_id: str, user: UserDep, session: SessionDep, deps: DepsDep
) -> RandomPreviewOut:
    return await service.random_preview(session, user, game_id, deps)


@router.post("/games/{game_id}/characters", status_code=201)
async def create_character(
    game_id: str, body: CreateIn, user: UserDep, session: SessionDep, deps: DepsDep
) -> CreatedOut:
    match body:
        case DetailedIn():
            # Движок получает свой тип, а не наш подкласс с пределами длины.
            choices = CharacterChoices.model_validate(body.choices.model_dump())
            return await service.create_detailed(session, user, game_id, deps, choices)
        case QuickIn():
            return await service.create_quick(
                session,
                user,
                game_id,
                deps,
                class_id=body.class_id,
                species=body.species,
                concept=body.concept,
                name=body.name,
            )
        case RandomIn():
            return await service.create_random(
                session, user, game_id, deps, preview_id=body.preview_id
            )


@router.post("/games/{game_id}/characters/backstory-help")
async def backstory_help(
    game_id: str, body: BackstoryHelpIn, user: UserDep, session: SessionDep, deps: DepsDep
) -> BackstoryOut:
    try:
        return await service.backstory_help(
            session,
            user,
            game_id,
            deps,
            class_id=body.class_id,
            species=body.species,
            concept=body.concept,
            draft=body.draft,
        )
    except FlavorTimeout as error:
        raise HTTPException(status_code=504, detail=_help_failed(error)) from None
    except FlavorLimitReached as error:
        # Предел не снимается ожиданием, поэтому совет «попробуйте позже» тут был бы неверным.
        raise HTTPException(
            status_code=429, detail=f"{error.message} Напишите предысторию сами."
        ) from None
    except FlavorUnavailable as error:
        raise HTTPException(status_code=503, detail=_help_failed(error)) from None


def _help_failed(error: FlavorUnavailable) -> str:
    return f"{error.message} Попробуйте позже или напишите предысторию сами."
