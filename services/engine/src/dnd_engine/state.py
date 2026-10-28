"""Состояние игры: всё, что сохраняется между ходами мастера.

`GameState` — одна pydantic-модель, которая целиком сериализуется в JSON и читается обратно.
Движок меняет только копию состояния и возвращает её вместе со списком событий; версию
(`version`) повышает сервер при коммите хода. Новые поля получают значения по умолчанию: снимки,
записанные прежним кодом, должны читаться (дневник `journal` и номер раунда игроков
`party_round` появились в DND-2, блок B8).
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

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
    # привалы DND-1); у других сцен вида нет.
    rest: Literal["short", "long"] | None = None


class WorldState(BaseModel):
    visited_locations: list[str] = []
    revealed: list[str] = []  # "feature:<id>", "clue:<id>", "exit:<id>"
    item_holders: dict[str, str] = {}  # item_id -> "location:<id>" | "character:<id>" | "gone"
    npc_attitude: dict[str, int] = {}  # −3..3
    npc_alive: dict[str, bool] = {}
    npc_location: dict[str, str] = {}
    flags: dict[str, str | int | bool] = {}


class GameTime(BaseModel):
    day: int = 1
    part: Literal["morning", "day", "evening", "night"] = "morning"


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
