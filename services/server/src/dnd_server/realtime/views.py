"""Проекция игры для игрока (план B6.5): снимок и события, которые уходят в сокеты.

Это единственное место, где состояние игры превращается в то, что видит клиент. Всё построено из
белых списков: поле попадает в ответ, только если оно перечислено здесь. `GameState` и `Character`
целиком клиенту не уходят, потому что в них лежат данные мастера (`pc_hooks`, `world`, летопись,
точные HP монстров) и чужие листы персонажей. Снимок (`snapshot_for`) и события строятся одними и
теми же функциями, так что событие `party` — это ровно блок `party` снимка и так далее: клиент
заменяет блок целиком.

Приватность. Лента отбирает сообщения по адресату (`all` или сам игрок), личное сообщение не
показывается как «общее» ни при каких данных, тайные ходы и броски лежат в ленте с адресатом-автором.
Журнал хода мастера (`dm_turn_logs`: результаты `get_location`, `get_npc`, `search_lore` с заметками
мастера и тайнами NPC) и копия приключения игры сюда не попадают вовсе. Адресаты событий движка —
id персонажей; в id игрока их переводит `audience_to_user`, и это единственное такое место.

Русские названия состояний и эффектов берутся из данных SRD движка (состояния, заклинания, умения
классов), а не из копии на сервере. Для id, которых в данных нет (например, метка `helped`),
клиент получает сам id.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.character import ActiveBuff, ActiveCondition, Character
from dnd_engine.content import SrdContent
from dnd_engine.state import GameState
from dnd_server.db.models import (
    AUDIENCE_ALL,
    ActionStatus,
    Game,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    User,
)
from dnd_server.games.service import get_game

# Сколько последних сообщений, видимых игроку, входит в ленту снимка.
FEED_LIMIT = 200

# Статус хода игрока для всех: «пишет», «готов», «пас». Что ход тайный и что в нём написано,
# статус не говорит. Автосдача и автопас по таймеру для остальных выглядят так же, как ручные.
WRITING = "writing"
PUBLIC_STATUS: Mapping[ActionStatus, str] = MappingProxyType(
    {
        ActionStatus.DRAFT: WRITING,
        ActionStatus.SUBMITTED: "ready",
        ActionStatus.AUTO_SUBMITTED: "ready",
        ActionStatus.PASS: "pass",
        ActionStatus.AUTO_PASS: "pass",
    }
)


def iso_utc(moment: datetime) -> str:
    """Время в ISO 8601 с явным поясом UTC (`+00:00`): без пояса браузер прочёл бы строку как
    местное время и сдвинул отсчёт. База отдаёт время уже с поясом UTC (`UtcDateTime`)."""
    return moment.astimezone(UTC).isoformat()


# --- названия и описания ---


def effect_name(srd: SrdContent, effect_id: str) -> str:
    """Русское название состояния или эффекта из данных SRD; неизвестный id возвращается как есть.

    Состояния лежат в `conditions`, эффекты заклинаний (`bless`, `aid`, `hunters_mark`) называются
    как заклинание, ярость и подобное — как умение класса.
    """
    if (condition := srd.conditions.get(effect_id)) is not None:
        return condition.name
    if (spell := srd.spells.get(effect_id)) is not None:
        return spell.name
    for class_def in srd.classes.values():
        for feature in class_def.features:
            if feature.id == effect_id:
                return feature.name
    return effect_id


def monster_health(hp: int, hp_max: int) -> str:
    """Как выглядит монстр: точные HP игрокам не показываются (мастер описывает словами).

    «Тяжело ранен» — осталась половина HP или меньше, как в правиле «окровавленного» существа.
    """
    if hp <= 0:
        return "повержен"
    if hp >= hp_max:
        return "цел"
    return "ранен" if hp * 2 > hp_max else "тяжело ранен"


def _effects(
    items: Iterable[ActiveCondition | ActiveBuff], srd: SrdContent
) -> list[dict[str, Any]]:
    # Источник и Сл эффекта (`source`, `dc`) — внутреннее дело движка, игроку они не нужны.
    return [
        {"id": item.id, "name": effect_name(srd, item.id), "rounds_left": item.rounds_left}
        for item in items
    ]


# --- блоки снимка (они же содержимое событий) ---


def phase_view(game: Game, state: GameState | None) -> dict[str, Any]:
    """Фаза игры, фаза хода, срок отсчёта и сцена. До старта игры состояния нет, и сцены тоже."""
    scene = None
    if state is not None:
        scene = {"type": state.scene.type.value, "day": state.time.day, "part": state.time.part}
    return {
        "phase": game.phase.value,
        "turn_phase": game.turn_phase.value if game.turn_phase is not None else None,
        "deadline_at": iso_utc(game.deadline_at) if game.deadline_at is not None else None,
        "scene": scene,
    }


def party_view(state: GameState, srd: SrdContent) -> list[dict[str, Any]]:
    """Отряд для всех: имена, HP и состояния. Листов персонажей здесь нет."""
    return [
        {
            "id": character.id,
            "name": character.name,
            "player_id": character.player_id,
            "hp": character.hp,
            "hp_max": character.hp_max,
            "temp_hp": character.temp_hp,
            "conditions": _effects(character.conditions, srd),
            "effects": _effects(character.buffs, srd),
        }
        for character in state.party.values()
    ]


def combat_view(state: GameState, srd: SrdContent) -> dict[str, Any] | None:
    """Бой для всех: порядок, текущий ход, зоны с бойцами. Монстры описаны словами, без чисел."""
    combat = state.combat
    if combat is None:
        return None
    fighters = [combat.combatants[fighter_id] for fighter_id in combat.order]
    order = []
    for fighter in fighters:
        base = {
            "id": fighter.id,
            "name": fighter.name,
            "kind": fighter.kind,
            "side": fighter.side,
            "zone_id": fighter.zone_id,
        }
        if fighter.kind == "monster":
            health = monster_health(fighter.hp or 0, fighter.hp_max or 0)
            order.append(
                {**base, "health": health, "conditions": _effects(fighter.conditions, srd)}
            )
            continue
        hero = state.party.get(fighter.ref)
        order.append(
            {
                **base,
                "character_id": fighter.ref,
                "hp": hero.hp if hero is not None else None,
                "hp_max": hero.hp_max if hero is not None else None,
                "conditions": _effects(hero.conditions if hero is not None else [], srd),
            }
        )
    in_turn = 0 <= combat.turn_index < len(combat.order)
    return {
        "round": combat.round,
        "current_id": combat.order[combat.turn_index] if in_turn else None,
        "order": order,
        "zones": [
            {
                "id": zone.id,
                "name": zone.name,
                "combatants": [f.id for f in fighters if f.zone_id == zone.id],
            }
            for zone in combat.zones
        ],
    }


def character_view(character: Character) -> dict[str, Any]:
    """Полный лист персонажа. Уходит только его владельцу (`character_event`, снимок игрока)."""
    return character.model_dump(mode="json")


def feed_item(message: Message) -> dict[str, Any]:
    """Сообщение ленты. Адресата клиент не получает, только признак «личное»; `data` кладёт тот,
    кто пишет сообщение, и в нём только то, что видит адресат этой строки."""
    return {
        "id": message.id,
        "kind": message.kind.value,
        "text": message.text,
        "data": message.data,
        "private": message.audience != AUDIENCE_ALL,
        "created_at": iso_utc(message.created_at),
    }


# --- события: те же блоки, обёрнутые типом ---


def phase_event(game: Game, state: GameState | None) -> dict[str, Any]:
    return {"type": "phase", "phase": phase_view(game, state)}


def player_status_event(user_id: str, status: ActionStatus) -> dict[str, Any]:
    return {"type": "player_status", "user_id": user_id, "status": PUBLIC_STATUS[status]}


def feed_item_event(message: Message) -> dict[str, Any]:
    """Событие ленты. Адресат рассылки — `message.audience`: вызывающий передаёт его хабу как есть."""
    return {"type": "feed_item", "item": feed_item(message)}


def party_event(state: GameState, srd: SrdContent) -> dict[str, Any]:
    return {"type": "party", "party": party_view(state, srd)}


def combat_event(state: GameState, srd: SrdContent) -> dict[str, Any]:
    return {"type": "combat", "combat": combat_view(state, srd)}


def character_event(character: Character) -> dict[str, Any]:
    return {"type": "character", "character": character_view(character)}


def state_events(state: GameState, srd: SrdContent) -> list[tuple[dict[str, Any], str]]:
    """События после нового состояния игры: пары `(событие, адресат)` для `Hub.send`.

    Отряд и бой уходят всем, лист персонажа — только его игроку. У персонажа без игрока листа
    не получает никто.
    """
    deliveries: list[tuple[dict[str, Any], str]] = [
        (party_event(state, srd), AUDIENCE_ALL),
        (combat_event(state, srd), AUDIENCE_ALL),
    ]
    for character in state.party.values():
        if character.player_id is not None:
            deliveries.append((character_event(character), character.player_id))
    return deliveries


# --- адресат события движка ---


def audience_to_user(party: Mapping[str, Character], audience: str) -> str | None:
    """Кому уходит событие движка: `all`, id игрока или `None` — никому.

    Движок адресует личное и тайное id персонажа, а сервер, лента и хаб — id игрока
    (`Character.player_id`). Перевод здесь единственный, и он закрытый: адресат, которого нет в
    отряде, у которого нет игрока или чья строка просто совпала с чьим-то user_id, не получает
    события, а не «все». Искать нужно только среди персонажей: id игрока в пространстве имён движка
    ничей. Если среди персонажей есть «all», слово «все» неоднозначно, и общее событие тоже
    не уходит никому.
    """
    if audience == AUDIENCE_ALL:
        return None if AUDIENCE_ALL in party else AUDIENCE_ALL
    character = party.get(audience)
    if character is None or character.id != audience:
        return None
    return character.player_id


# --- снимок ---


async def _current_state(session: AsyncSession, game: Game) -> GameState | None:
    """Текущая версия состояния игры; до старта игры состояния нет. Версию читает `to_state`."""
    row = await session.get(GameStateSnapshot, (game.id, game.state_version))
    return None if row is None else row.to_state()


async def _my_character(
    session: AsyncSession, game_id: str, user: User, state: GameState | None
) -> Character | None:
    """Лист игрока: живой из состояния игры, а в лобби — созданный, ещё без состояния."""
    player = await session.get(GamePlayer, (game_id, user.id))
    if player is None or player.character_id is None:
        return None
    if state is not None:
        return state.party.get(player.character_id)
    row = await session.get(PlayerCharacter, player.character_id)
    return None if row is None else Character.model_validate(row.data)


async def _turn_actions(session: AsyncSession, game: Game) -> dict[str, PlayerAction]:
    """Ходы игроков в текущем ходе игры по user_id."""
    if game.current_turn_id is None:
        return {}
    rows = await session.scalars(
        select(PlayerAction).where(PlayerAction.turn_id == game.current_turn_id)
    )
    return {action.user_id: action for action in rows}


async def _feed(session: AsyncSession, game_id: str, user: User) -> list[dict[str, Any]]:
    """Последние `FEED_LIMIT` сообщений, видимых игроку, по порядку. Лимит считается после отбора,
    чужие личные сообщения места в ленте не отнимают."""
    # «Личное» с адресатом «все» — ошибка записи: показывать его как общее нельзя.
    visible = or_(
        and_(Message.audience == AUDIENCE_ALL, Message.kind != MessageKind.PRIVATE),
        Message.audience == user.id,
    )
    rows = await session.scalars(
        select(Message)
        .where(Message.game_id == game_id, Message.superseded.is_(False), visible)
        .order_by(Message.id.desc())
        .limit(FEED_LIMIT)
    )
    return [feed_item(message) for message in reversed(list(rows))]


async def snapshot_for(
    session: AsyncSession, user: User, game_id: str, *, srd: SrdContent
) -> dict[str, Any]:
    """Полный снимок игры для игрока `user`; не участнику отказ (`NotFound`, как у REST).

    Сессию и SRD передаёт вызывающий: снимок не открывает соединений сам, и сокет не держит базу
    открытой, пока длится игра.
    """
    game_out = await get_game(session, user, game_id)  # проверка участия и сводка лобби
    game = await session.get(Game, game_id)
    state = await _current_state(session, game)
    actions = await _turn_actions(session, game)
    mine = actions.get(user.id)
    character = await _my_character(session, game_id, user, state)
    return {
        "type": "snapshot",
        # Отсчёт идёт по серверному времени: клиент вычтет разницу часов (план B8.5).
        "server_time": iso_utc(datetime.now(UTC)),
        "game": game_out.model_dump(mode="json"),
        "host_user_id": game.host_user_id,
        "phase": phase_view(game, state),
        "statuses": (
            {
                player.user_id: PUBLIC_STATUS[actions[player.user_id].status]
                if player.user_id in actions
                else WRITING
                for player in game_out.players
            }
            if game.current_turn_id is not None
            else {}
        ),
        "character": character_view(character) if character is not None else None,
        "action": (
            None
            if mine is None
            else {
                "text": mine.text,
                "secret": mine.secret,
                "inspiration": mine.inspiration,
                "status": mine.status.value,
            }
        ),
        "party": party_view(state, srd) if state is not None else [],
        "combat": combat_view(state, srd) if state is not None else None,
        "feed": await _feed(session, game_id, user),
    }
