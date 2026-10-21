"""Игра для тестов реального времени (план B6.5): три игрока, персонажи, состояние, лента.

Строки вставляются в базу напрямую: создание игры, персонажей и ход мастера — другие пункты блока.
В игру нарочно положены «ловушки» — тексты, которые игрок видеть не должен. Каждая начинается с
метки: `ТАЙНА-` (то, что игроку не показывают никому: зацепки, мир, заметки мастера, журнал хода),
`ЛИЧНОЕ-`, `ХОД-` и `БРОСОК-` с именем игрока (видны только ему), `ПРЕДЫСТОРИЯ-` с именем
игрока (лист персонажа виден только владельцу). Тесты ищут метки в ответах хаба целиком.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveBuff, ActiveCondition, Character, Persona
from dnd_engine.combat.model import Combat, Combatant, Zone, ZoneLink
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.creation import quick_character
from dnd_engine.state import (
    Chronicle,
    GameState,
    GameTime,
    PcHook,
    RulesRef,
    Scene,
    SceneType,
    WorldState,
)
from dnd_server.auth.session import SessionSigner
from dnd_server.characters.service import new_character_id
from dnd_server.config import Settings
from dnd_server.db.models import (
    ActionStatus,
    DmTurnLog,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    Turn,
    TurnKind,
    TurnPhase,
    User,
    new_id,
)

SESSION_SECRET = "test-session-secret-0123456789-abcdefghij"
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SECRET_MARK = "ТАЙНА-"
GAME_TITLE = "Склеп должника"
DEADLINE = datetime(2030, 5, 1, 12, 30, 0, tzinfo=UTC)

# (ключ игрока, имя, класс, вид)
PLAYERS = (
    ("alice", "Алиса", "fighter", "human"),
    ("boris", "Борис", "rogue", "elf"),
    ("vera", "Вера", "cleric", "dwarf"),
)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Dev-настройки из корневого conftest с секретом сессий для токенов тестовых игроков."""
    return settings.model_copy(update={"session_secret": SecretStr(SESSION_SECRET)})


@pytest.fixture(scope="session")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture
def signer() -> SessionSigner:
    """Тот же подписчик сессий, что у приложения тестов: им выпускают токены чужих игроков."""
    return SessionSigner(SESSION_SECRET)


@dataclass
class Seat:
    """Игрок за столом: пользователь, его токен сессии и персонаж."""

    key: str
    user_id: str
    name: str
    token: str
    character: Character


@cache
def demo_adventure() -> Adventure:
    return load_adventure(DEMO_ADVENTURE_DIR)


def adventure_copy() -> dict[str, Any]:
    """Копия приключения в строке игры (как её кладёт сервер) с ловушками в полях мастера."""
    data = demo_adventure().model_dump(mode="json")
    data["title"] = GAME_TITLE
    data["locations"]["village"]["dm_notes"] = f"{SECRET_MARK}заметки-мастера"
    data["npcs"]["marta"]["secret"] = f"{SECRET_MARK}секрет-npc"
    return data


@dataclass
class Table:
    game_id: str
    seats: dict[str, Seat]
    state: GameState | None
    db: async_sessionmaker[AsyncSession]
    adventure: Adventure
    messages: dict[str, int] = field(default_factory=dict)  # подпись сообщения -> его id

    @property
    def alice(self) -> Seat:
        return self.seats["alice"]

    @property
    def boris(self) -> Seat:
        return self.seats["boris"]

    @property
    def vera(self) -> Seat:
        return self.seats["vera"]

    async def add_message(
        self,
        text: str,
        *,
        audience: str = "all",
        kind: MessageKind = MessageKind.NARRATION,
        data: dict[str, Any] | None = None,
        superseded: bool = False,
    ) -> Message:
        async with self.db() as session:
            message = Message(
                game_id=self.game_id,
                audience=audience,
                kind=kind,
                text=text,
                data=data,
                superseded=superseded,
            )
            session.add(message)
            await session.commit()
        self.messages[text] = message.id
        return message


def hero(key: str, name: str, class_id: str, species: str, user_id: str, srd: SrdContent):
    """Персонаж игрока в том виде, как его сохраняет B6.4: id `pc_<16 hex>` и `player_id`."""
    built = quick_character(
        class_id,
        species,
        srd,
        name=name,
        concept="Бывший стражник",
        character_id=new_character_id(),
    )
    persona = Persona(concept="Бывший стражник", backstory=f"ПРЕДЫСТОРИЯ-{key}")
    return built.model_copy(update={"player_id": user_id, "persona": persona})


def state_of(game_id: str, seats: dict[str, Seat]) -> GameState:
    """Состояние игры в бою: у отряда состояния и эффекты, в мире и зацепках — тайны."""
    alice, boris, vera = (seat.character for seat in seats.values())
    alice = alice.model_copy(
        update={
            "hp": alice.hp - 4,
            "conditions": [ActiveCondition(id="poisoned", rounds_left=3, source="goblin-1")],
            "buffs": [ActiveBuff(id="bless", rounds_left=5, source_id=vera.id)],
            "inspiration": True,
        }
    )
    boris = boris.model_copy(
        update={"conditions": [ActiveCondition(id="helped", source="hide", rounds_left=1)]}
    )
    party = {c.id: c for c in (alice, boris, vera)}
    goblin = dict(kind="monster", side="enemies", ref="goblin_warrior", ac=15)
    combat = Combat(
        encounter_id=f"{SECRET_MARK}встреча",
        zones=[Zone(id="gate", name="Ворота"), Zone(id="yard", name="Двор")],
        links=[ZoneLink(a="gate", b="yard", distance="near")],
        combatants={
            alice.id: Combatant(
                id=alice.id, kind="pc", side="party", ref=alice.id, name=alice.name, zone_id="gate"
            ),
            boris.id: Combatant(
                id=boris.id, kind="pc", side="party", ref=boris.id, name=boris.name, zone_id="gate"
            ),
            "goblin_warrior-1": Combatant(
                id="goblin_warrior-1", name="Гоблин 1", hp=7, hp_max=7, zone_id="yard", **goblin
            ),
            "goblin_warrior-2": Combatant(
                id="goblin_warrior-2", name="Гоблин 2", hp=5, hp_max=7, zone_id="yard", **goblin
            ),
            "goblin_warrior-3": Combatant(
                id="goblin_warrior-3", name="Гоблин 3", hp=3, hp_max=7, zone_id="yard", **goblin
            ),
            "goblin_warrior-4": Combatant(
                id="goblin_warrior-4", name="Гоблин 4", hp=0, hp_max=7, zone_id="yard", **goblin
            ),
        },
        order=[
            "goblin_warrior-1",
            boris.id,
            "goblin_warrior-2",
            alice.id,
            "goblin_warrior-3",
            "goblin_warrior-4",
        ],
        round=2,
        turn_index=1,  # ходит Борис
    )
    hooks = {
        c.id: PcHook(
            character_id=c.id,
            npc_links=[{"npc_id": "warden", "relation": f"{SECRET_MARK}связь", "note": "?"}],
            personal_goal=f"{SECRET_MARK}цель-{c.name}",
            secret=f"{SECRET_MARK}секрет-{c.name}",
        )
        for c in party.values()
    }
    return GameState(
        game_id=game_id,
        adventure_id="demo-crypt",
        rules=RulesRef(modules=["bg3_flavor"]),
        party=party,
        location_id=f"{SECRET_MARK}тайная-локация",
        scene=Scene(type=SceneType.COMBAT, started_turn=2),
        world=WorldState(
            revealed=[f"clue:{SECRET_MARK}улика"],
            item_holders={f"{SECRET_MARK}предмет": "gone"},
            flags={f"{SECRET_MARK}флаг": True},
            npc_alive={f"{SECRET_MARK}npc": False},
        ),
        time=GameTime(day=2, part="evening"),
        combat=combat,
        chronicle=Chronicle(summary=f"{SECRET_MARK}летопись", entries=[f"{SECRET_MARK}запись"]),
        pc_hooks=hooks,
        turn=3,
        version=3,
    )


async def build_table(
    db: async_sessionmaker[AsyncSession], srd: SrdContent, *, playing: bool
) -> Table:
    """Игра с тремя игроками: в лобби (персонажи без состояния игры) или в разгаре боя."""
    game_id = new_id()
    seats: dict[str, Seat] = {}
    signer = SessionSigner(SESSION_SECRET)
    for key, name, class_id, species in PLAYERS:
        user_id = new_id()
        character = hero(key, name, class_id, species, user_id, srd)
        seats[key] = Seat(key, user_id, name, signer.issue(user_id), character)
    state = state_of(game_id, seats) if playing else None
    if state is not None:  # персонажи игроков — те, что живут в состоянии игры (с HP и эффектами)
        for seat in seats.values():
            seat.character = state.party[seat.character.id]

    turn_id = new_id()
    async with db() as session:
        # Порядок вставки задаётся явно: связей между моделями нет, сам порядок не сложится.
        session.add_all(
            User(id=seat.user_id, dev_login=f"dev-{number}", name=seat.name)
            for number, seat in enumerate(seats.values(), start=1)
        )
        await session.flush()
        session.add(
            Game(
                id=game_id,
                code="ABC123",
                adventure_id="demo-crypt",
                host_user_id=seats["alice"].user_id,
                adventure_data=adventure_copy(),
                phase=GamePhase.PLAYING if playing else GamePhase.LOBBY,
                turn_phase=TurnPhase.COLLECTING if playing else None,
                deadline_at=DEADLINE if playing else None,
                state_version=3 if playing else 0,
                current_turn_id=turn_id if playing else None,
                collect_seconds=120,
                hurry_seconds=60,
                camp_seconds=300,
            )
        )
        await session.flush()
        session.add_all(
            PlayerCharacter(
                id=seat.character.id,
                game_id=game_id,
                user_id=seat.user_id,
                data=seat.character.model_dump(mode="json"),
            )
            for seat in seats.values()
        )
        await session.flush()
        session.add_all(
            GamePlayer(
                game_id=game_id,
                user_id=seat.user_id,
                character_id=seat.character.id,
                is_host=seat.key == "alice",
                ready=not playing and seat.key != "vera",
                joined_order=number,
            )
            for number, seat in enumerate(seats.values(), start=1)
        )
        if playing:
            session.add(GameStateSnapshot.from_state(state))
            session.add(
                Turn(
                    id=turn_id,
                    game_id=game_id,
                    number=3,
                    kind=TurnKind.ROUND,
                    state_version_before=3,
                )
            )
            await session.flush()
            session.add_all(
                [
                    PlayerAction(
                        turn_id=turn_id, user_id=seats["alice"].user_id, text="обыскать сундук"
                    ),
                    PlayerAction(
                        turn_id=turn_id,
                        user_id=seats["boris"].user_id,
                        text="ХОД-boris: подкрасться",
                        secret=True,
                        inspiration=True,
                        status=ActionStatus.SUBMITTED,
                    ),
                    DmTurnLog(
                        game_id=game_id,
                        turn_id=turn_id,
                        attempt=1,
                        provider="fake",
                        request={"system": f"{SECRET_MARK}промпт"},
                        trace=[
                            {
                                "name": "get_location",
                                "result": {"dm_notes": f"{SECRET_MARK}заметки-локации"},
                            },
                            {"name": "get_npc", "result": {"secret": f"{SECRET_MARK}секрет-npc"}},
                        ],
                        output=f"{SECRET_MARK}итог",
                    ),
                ]
            )
        await session.commit()

    table = Table(game_id, seats, state, db, demo_adventure())
    if playing:
        await seed_feed(table)
    return table


async def seed_feed(table: Table) -> None:
    """Лента игры: общее, личное каждому, тайные ходы и броски, снятое и ошибочное."""
    alice, boris = table.alice, table.boris
    await table.add_message("Рассказ: сырой подвал.", kind=MessageKind.NARRATION)
    await table.add_message(
        "Проверка Внимательности",
        kind=MessageKind.ROLL,
        data={"label": "Внимательность", "total": 14},
    )
    await table.add_message(
        "ЛИЧНОЕ-alice: вы чуете холод.", audience=alice.user_id, kind=MessageKind.PRIVATE
    )
    await table.add_message(
        "ЛИЧНОЕ-boris: в кармане ключ.", audience=boris.user_id, kind=MessageKind.PRIVATE
    )
    await table.add_message("Алиса осматривает дверь.", kind=MessageKind.ACTION)
    await table.add_message(
        "ХОД-boris: подкрасться", audience=boris.user_id, kind=MessageKind.ACTION
    )
    await table.add_message(
        "Скрытность",
        audience=boris.user_id,
        kind=MessageKind.ROLL,
        data={"label": "БРОСОК-boris", "total": 19},
    )
    await table.add_message("Рассказ, снятый «Перегенерировать».", superseded=True)
    # Ошибка записи: личное сообщение «всем». Показывать его нельзя никому.
    await table.add_message("ЛИЧНОЕ-ошибочно-всем", kind=MessageKind.PRIVATE)


@pytest.fixture
async def table(db: async_sessionmaker[AsyncSession], srd: SrdContent) -> Table:
    """Игра в разгаре боя (фаза сбора ходов)."""
    return await build_table(db, srd, playing=True)


@pytest.fixture
async def lobby_table(db: async_sessionmaker[AsyncSession], srd: SrdContent) -> Table:
    """Игра в лобби: персонажи созданы, состояния игры ещё нет."""
    return await build_table(db, srd, playing=False)
