"""Состояние игры: всё, что сохраняется между ходами мастера.

`GameState` — одна pydantic-модель, которая целиком сериализуется в JSON и читается обратно.
Движок меняет только копию состояния и возвращает её вместе со списком событий; версию
(`version`) повышает сервер при коммите хода. Новые поля получают значения по умолчанию: снимки,
записанные прежним кодом, должны читаться (так появились дневник `journal`, номер раунда игроков
`party_round` и часы `GameTime.hour`).
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    ModelWrapValidatorHandler,
    computed_field,
    model_validator,
)

from dnd_engine.character import Character
from dnd_engine.combat.model import Combat
from dnd_engine.journal import JournalEntry


class SceneType(StrEnum):
    EXPLORATION = "exploration"
    SOCIAL = "social"
    COMBAT = "combat"
    CAMP = "camp"


class Scene(BaseModel):
    type: SceneType = SceneType.EXPLORATION
    started_turn: int = 0
    inspiration_granted: list[str] = []  # кому уже дали вдохновение в этой сцене
    # Вид привала у сцены `camp`: короткий или долгий. `None` у `camp` — долгий (так записаны
    # привалы в старых снимках, где коротких ещё не было); у других сцен вида нет.
    rest: Literal["short", "long"] | None = None


class WorldState(BaseModel):
    visited_locations: list[str] = []
    revealed: list[str] = []  # "feature:<id>", "clue:<id>", "exit:<id>"
    item_holders: dict[str, str] = {}  # item_id -> "location:<id>" | "character:<id>" | "gone"
    npc_attitude: dict[str, int] = {}  # −3..3
    npc_alive: dict[str, bool] = {}
    npc_location: dict[str, str] = {}
    flags: dict[str, str | int | bool] = {}


DayPart = Literal["morning", "day", "evening", "night"]
# Час начала каждой части суток, в порядке суток. Ночь длится через полночь до начала утра.
DAY_PART_STARTS: dict[DayPart, int] = {"morning": 6, "day": 12, "evening": 18, "night": 22}
HOURS_PER_DAY = 24


def day_part(hour: int) -> DayPart:
    """Часть суток по часу: утро 6–11, день 12–17, вечер 18–21, ночь 22–5."""
    current: DayPart = "night"  # часы до начала утра — ночь, начавшаяся накануне
    for part, start in DAY_PART_STARTS.items():
        if hour >= start:
            current = part
    return current


class GameTime(BaseModel):
    """Время игры: день и час (0–23); часть суток считается по часу и хранится вместе с ним.

    День прибавляется в полночь. Снимки, записанные до появления часов, хранят только часть
    суток: при чтении час ставится на её начало, и игра, шедшая вечером, остаётся вечерней.
    Часть суток рядом с часом только сверяется: несовпадение — ошибка данных, а не повод
    молча выбрать одно из двух."""

    day: int = 1
    hour: int = Field(default=DAY_PART_STARTS["morning"], ge=0, lt=HOURS_PER_DAY)

    @model_validator(mode="wrap")
    @classmethod
    def _hour_from_the_part(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        if not isinstance(data, dict) or "part" not in data:
            return handler(data)
        data = dict(data)
        part = data.pop("part")
        if not isinstance(part, str) or part not in DAY_PART_STARTS:
            raise ValueError(f"неизвестная часть суток {part!r}: {', '.join(DAY_PART_STARTS)}")
        data.setdefault("hour", DAY_PART_STARTS[part])
        # Сверка — после разбора поля: час строкой «19» или дробью 19.0 поле приводит к 19, и
        # несовпадающая часть тоже должна стать ошибкой, а не молча пропасть.
        time = handler(data)
        if time.part != part:
            raise ValueError(f"час {time.hour} — это {time.part}, а не {part}")
        return time

    @computed_field
    @property
    def part(self) -> DayPart:
        return day_part(self.hour)

    def plus_hours(self, hours: int) -> GameTime:
        """Время через `hours` часов; полночь переводит день."""
        days, hour = divmod(self.hour + hours, HOURS_PER_DAY)
        return GameTime(day=self.day + days, hour=hour)


class Chronicle(BaseModel):
    summary: str = ""
    entries: list[str] = []


class Spotlight(BaseModel):
    submitted: int = 0
    focused: int = 0
    turns_since_focus: int = 0


class PcHook(BaseModel):
    character_id: str
    npc_links: list[dict[str, str]] = []  # {"npc_id", "relation", "note"}
    location_links: list[dict[str, str]] = []  # {"location_id", "note"}
    party_links: list[dict[str, str]] = []  # {"character_id", "relation"}
    personal_goal: str = ""
    secret: str = ""


class RulesRef(BaseModel):
    base: Literal["srd-5.2"] = "srd-5.2"
    modules: list[str] = []


class GameState(BaseModel):
    game_id: str
    adventure_id: str
    rules: RulesRef
    party: dict[str, Character]
    location_id: str
    scene: Scene = Scene()
    world: WorldState = WorldState()
    time: GameTime = GameTime()
    combat: Combat | None = None
    chronicle: Chronicle = Chronicle()
    spotlight: dict[str, Spotlight] = {}
    pc_hooks: dict[str, PcHook] = {}
    short_rests_since_long: int = 0
    journal: tuple[JournalEntry, ...] = ()  # дневник отряда в порядке появления записей
    # Номер текущего раунда игроков: 0 — начало игры и открытие; растёт с каждым разбором ходов
    # игроков (ставит сервер), а в бою и на привале не меняется. В отличие от `turn` (номер хода
    # мастера, в том числе каждого хода бойца) им помечены записи дневника.
    party_round: int = 0
    turn: int = 0
    version: int = 0
