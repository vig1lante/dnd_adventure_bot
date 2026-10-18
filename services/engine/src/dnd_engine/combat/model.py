"""Модели боя: зоны, связи между ними, бойцы и сам бой.

Только данные, без правил: правила (инициатива, движение, атаки) живут в `combat.engine`.
Бой лежит в `GameState.combat` и сериализуется вместе с состоянием игры.
"""

from typing import Literal

from pydantic import BaseModel

from dnd_engine.character import ActiveCondition


class Zone(BaseModel):
    id: str
    name: str


class ZoneLink(BaseModel):
    a: str
    b: str
    distance: Literal["near", "far"]


class Combatant(BaseModel):
    id: str
    kind: Literal["pc", "monster"]
    side: Literal["party", "enemies"]
    ref: str  # id персонажа или монстра SRD/кастомного
    name: str
    hp: int | None = None  # только у монстров; HP персонажей хранит Character
    hp_max: int | None = None
    ac: int | None = None  # только у монстров
    initiative: int = 0
    zone_id: str
    engaged_with: list[str] = []
    conditions: list[ActiveCondition] = []
    turn_flags: dict[
        str, bool
    ] = {}  # action, bonus_action, reaction, moved, sneak_attack_used, ...
    dodging: bool = False
    disengaged: bool = False


class Combat(BaseModel):
    encounter_id: str | None
    zones: list[Zone]
    links: list[ZoneLink]
    combatants: dict[str, Combatant]
    order: list[str] = []
    round: int = 1
    turn_index: int = 0
