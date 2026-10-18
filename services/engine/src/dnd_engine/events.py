"""События движка: то, что произошло в ходе действия (броски, урон, изменения мира).

Каждая функция движка возвращает новое состояние и список событий. Событие — запись для
журнала хода и для рассылки игрокам; состояние они не меняют. Виды событий различаются
полем `kind`, поэтому `TypeAdapter(Event)` восстанавливает нужный класс из JSON.
Поле `audience` отмечает тайное: «all» видят все, иначе — только персонаж с этим id (и мастер).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from dnd_engine.dice import RollResult
from dnd_engine.rest import RestOutcome
from dnd_engine.state import SceneType

Audience = Literal["all"] | str  # "all" или id персонажа


class RollEvent(BaseModel):
    kind: Literal["roll"] = "roll"
    audience: Audience = "all"
    actor_id: str
    label: str
    roll: RollResult
    dc: int | None = None
    target_ac: int | None = None
    success: bool | None = None


class DamageEvent(BaseModel):
    kind: Literal["damage"] = "damage"
    target_id: str
    amount: int
    damage_type: str
    hp_after: int


class HealEvent(BaseModel):
    kind: Literal["heal"] = "heal"
    target_id: str
    amount: int
    hp_after: int


class ConditionEvent(BaseModel):
    kind: Literal["condition"] = "condition"
    target_id: str
    condition: str
    added: bool


class ActionEvent(BaseModel):
    """Действие, которое произошло, но не оставило записи в состоянии: Уклонение, Рывок,
    заклинание без полей эффекта, особое действие монстра. `ConditionEvent` — только для
    состояний и эффектов, которые лежат в `GameState` и потом снимаются парным событием."""

    kind: Literal["action"] = "action"
    actor_id: str
    action: str  # id действия или заклинания
    target_ids: list[str] = []
    label: str | None = None  # название для рассказа: заклинания или действия монстра


class MoveEvent(BaseModel):
    kind: Literal["move"] = "move"
    location_id: str


class RevealEvent(BaseModel):
    kind: Literal["reveal"] = "reveal"
    ref: str  # "feature:<id>" | "clue:<id>" | "exit:<id>"


class ItemEvent(BaseModel):
    kind: Literal["item"] = "item"
    item_id: str
    holder_from: str
    holder_to: str


class FlagEvent(BaseModel):
    kind: Literal["flag"] = "flag"
    name: str
    value: str | int | bool


class AttitudeEvent(BaseModel):
    kind: Literal["attitude"] = "attitude"
    npc_id: str
    value: int


class SceneEvent(BaseModel):
    kind: Literal["scene"] = "scene"
    scene: SceneType


class CombatEvent(BaseModel):
    kind: Literal["combat"] = "combat"
    phase: Literal["started", "turn", "ended"]
    actor_id: str | None = None
    winner: str | None = None


class PrivateMessageEvent(BaseModel):
    kind: Literal["private"] = "private"
    audience: str
    text: str

    @field_validator("audience")
    @classmethod
    def _addressed_to_one_character(cls, audience: str) -> str:
        # «all» сделало бы личное сообщение общим: фильтр по получателю пропустил бы его всем.
        if audience == "all":
            raise ValueError("личное сообщение нельзя адресовать всем")
        return audience


class ChronicleEvent(BaseModel):
    kind: Literal["chronicle"] = "chronicle"
    text: str


class InspirationEvent(BaseModel):
    kind: Literal["inspiration"] = "inspiration"
    character_id: str
    granted: bool
    reason: str


class RestEvent(BaseModel):
    kind: Literal["rest"] = "rest"
    rest: Literal["short", "long"]
    outcomes: list[RestOutcome]


Event = Annotated[
    RollEvent
    | DamageEvent
    | HealEvent
    | ConditionEvent
    | ActionEvent
    | MoveEvent
    | RevealEvent
    | ItemEvent
    | FlagEvent
    | AttitudeEvent
    | SceneEvent
    | CombatEvent
    | PrivateMessageEvent
    | ChronicleEvent
    | InspirationEvent
    | RestEvent,
    Field(discriminator="kind"),
]
