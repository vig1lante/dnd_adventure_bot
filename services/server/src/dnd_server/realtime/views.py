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

Сообщения ленты из событий хода мастера (`feed_entries`) строятся здесь же: что из событий
движка становится строкой ленты и кому она видна. Точных HP монстров и КД целей в строках нет, а
события без автора (находки, флаги) строк не дают вовсе: об этом игроки узнают из рассказа. Броски
монстра, спрятанного от отряда, идут от имени «Кто-то невидимый» без имени и id бойца, как и сам
монстр отсутствует в `combat_view`.

Русские названия состояний и эффектов берутся из данных SRD движка (состояния, заклинания, умения
классов) и из таблицы названий боевых меток движка (`MARK_NAMES_RU`, например «Помощь» для
`helped`), а не из копии на сервере. Для id, которых нет и там, клиент получает сам id.
"""

import logging
import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import ActiveBuff, ActiveCondition, Character
from dnd_engine.combat.engine import INVISIBLE, MARK_NAMES_RU
from dnd_engine.combat.model import Combatant
from dnd_engine.content import SrdContent
from dnd_engine.events import (
    CombatEvent,
    Event,
    InspirationEvent,
    ItemEvent,
    MoveEvent,
    PrivateMessageEvent,
    RestEvent,
    RollEvent,
)
from dnd_engine.state import GameState
from dnd_server.db.models import (
    AUDIENCE_ALL,
    ActionStatus,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerAction,
    PlayerCharacter,
    User,
)
from dnd_server.games.service import get_game

logger = logging.getLogger(__name__)

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
    """Русское название состояния или эффекта; неизвестный id возвращается как есть.

    Состояния лежат в `conditions` данных SRD, эффекты заклинаний (`bless`, `aid`, `hunters_mark`)
    называются как заклинание, ярость и подобное — как умение класса. Метки боя, которых в данных
    нет (`helped`), называет таблица движка `MARK_NAMES_RU`.
    """
    if (condition := srd.conditions.get(effect_id)) is not None:
        return condition.name
    if (spell := srd.spells.get(effect_id)) is not None:
        return spell.name
    for class_def in srd.classes.values():
        for feature in class_def.features:
            if feature.id == effect_id:
                return feature.name
    return MARK_NAMES_RU.get(effect_id, effect_id)


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
    """Фаза игры, фаза хода, отсчёт и сцена. До старта игры состояния нет, и сцены тоже.

    Идущий отсчёт описывает срок `deadline_at`. На паузе отсчёт стоит: срока нет, а остаток в
    секундах (вверх) лежит в `remaining_seconds`, чтобы клиент показал его неподвижным; после
    «Продолжить» приходит новый срок."""
    scene = None
    if state is not None:
        scene = {"type": state.scene.type.value, "day": state.time.day, "part": state.time.part}
    remaining = game.paused_remaining()
    running = game.phase != GamePhase.PAUSED and game.deadline_at is not None
    return {
        "phase": game.phase.value,
        "turn_phase": game.turn_phase.value if game.turn_phase is not None else None,
        "deadline_at": iso_utc(game.deadline_at) if running else None,
        "remaining_seconds": None if remaining is None else math.ceil(remaining.total_seconds()),
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


def is_hidden_monster(fighter: Combatant) -> bool:
    """Монстр спрятан от отряда (состояние «невидим»): игроки не знают о нём ничего."""
    return fighter.kind == "monster" and any(c.id == INVISIBLE for c in fighter.conditions)


def combat_view(state: GameState, srd: SrdContent) -> dict[str, Any] | None:
    """Бой для всех: порядок, текущий ход, зоны с бойцами. Монстры описаны словами, без чисел.

    Спрятавшегося монстра в ответе нет вовсе — ни в порядке хода, ни в зоне: имя и положение
    выдали бы засаду. Если сейчас его ход, `current_id` пуст. Спрятавшийся герой виден отряду."""
    combat = state.combat
    if combat is None:
        return None
    fighters = [
        fighter
        for fighter in (combat.combatants[fighter_id] for fighter_id in combat.order)
        if not is_hidden_monster(fighter)
    ]
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
    current_id = combat.order[combat.turn_index] if in_turn else None
    if current_id is not None and is_hidden_monster(combat.combatants[current_id]):
        current_id = None
    return {
        "round": combat.round,
        "current_id": current_id,
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


def character_view(character: Character, srd: SrdContent) -> dict[str, Any]:
    """Лист персонажа. Уходит только его владельцу (`character_event`, снимок игрока).

    Состояния (`conditions`) и эффекты (`buffs`) проходят через белый список блока отряда
    (`_effects`: id, название, срок). Без этого лист выдавал бы `source` и `source_id`, а в них id
    спрятанного монстра, который опутал героя: монстра нет ни в порядке хода, ни в зонах боя, и лист
    не должен называть его вместо них (решение D7). Остальные поля листа идут как есть."""
    return {
        **character.model_dump(mode="json", exclude={"conditions", "buffs"}),
        "conditions": _effects(character.conditions, srd),
        "buffs": _effects(character.buffs, srd),
    }


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


def feed_withdrawn_event(message_id: int) -> dict[str, Any]:
    """Событие-надгробие: запись ленты снята («Перегенерировать»), клиент убирает её по `id`.

    Снимок такие записи не содержит вовсе, поэтому после переподключения надгробие не нужно.
    Адресат рассылки — тот же, кому уходила сама запись."""
    return {"type": "feed_item", "item": {"id": message_id, "superseded": True}}


def party_event(state: GameState, srd: SrdContent) -> dict[str, Any]:
    return {"type": "party", "party": party_view(state, srd)}


def combat_event(state: GameState, srd: SrdContent) -> dict[str, Any]:
    return {"type": "combat", "combat": combat_view(state, srd)}


def character_event(character: Character, srd: SrdContent) -> dict[str, Any]:
    return {"type": "character", "character": character_view(character, srd)}


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
            deliveries.append((character_event(character, srd), character.player_id))
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


# --- строки ленты из событий хода мастера ---


@dataclass(frozen=True)
class FeedEntry:
    """Строка ленты до записи в базу: кому, какого вида, текст и данные карточки.

    `audience` — `AUDIENCE_ALL` или id пользователя (не персонажа). В `data` только то, что видит
    адресат этой строки."""

    audience: str
    kind: MessageKind
    text: str
    data: dict[str, Any] | None = None


# Под этим именем в ленте бросает монстр, спрятанный от отряда (решение D7).
UNSEEN_ACTOR = "Кто-то невидимый"

_COMBAT_END = {
    "party": "Бой окончен: отряд победил.",
    "enemies": "Бой окончен: отряд повержен.",
    None: "Бой окончен.",
}
_REST_TEXT = {
    "long": "Длинный отдых: отряд восстановил силы.",
    "short": "Короткий отдых: отряд перевёл дух.",
}


def feed_entries(
    events: Iterable[Event],
    *,
    before: GameState,
    after: GameState,
    adventure: Adventure,
    srd: SrdContent,
    secret_actors: Collection[str],
) -> list[FeedEntry]:
    """Строки ленты из событий хода по порядку: броски, личные сообщения и системные строки.

    `before` и `after` — состояние до и после хода (имена бойцов бой мог уже убрать),
    `secret_actors` — id персонажей авторов тайных ходов. NPC приключения (сторона состязания)
    бросает под именем из приключения, а в `data` карточки его id нет (решение D9). Бросок монстра,
    спрятанного в любом из двух состояний, идёт без имени и id бойца (`UNSEEN_ACTOR`): `combat_view`
    прячет такого монстра целиком, и лента не должна выдать засаду. Скрывается с запасом: событий
    между состояниями здесь нет, а лишняя безымянная карточка безвреднее раскрытой засады. Адресат
    броска и личного сообщения задан движком (id персонажа) и переводится в id игрока через
    `audience_to_user`; событие, адресата которого нет среди игроков, не пишется и не рассылается.
    Строки о предметах и вдохновении тайного героя уходят только ему. События без строки (находки,
    флаги, отношение NPC, смена сцены, состояния, действия, урон и лечение, летопись) в ленту не
    идут: у находок и флагов нет автора, а урон и лечение несут точные HP (`hp_after`), которые
    игрокам показывают словами и блоками отряда и боя."""
    # NPC приключения бросают в состязании (`contest`): в карточке только имя, без id из данных
    # приключения. Герой или боец с тем же id важнее, как и при поиске участника в движке.
    fighters = _names(before, after)
    npcs = {npc.id: npc.name for npc in adventure.npcs.values() if npc.id not in fighters}
    names = npcs | fighters
    hidden = _hidden_monsters(before, after)
    entries: list[FeedEntry] = []
    for event in events:
        entry = _entry(event, after.party, names, hidden, npcs, adventure, srd, secret_actors)
        if entry is not None:
            entries.append(entry)
    return entries


def _names(*states: GameState) -> dict[str, str]:
    """Названия бойцов и героев по id; поздние состояния важнее ранних."""
    names: dict[str, str] = {}
    for state in states:
        if state.combat is not None:
            names |= {fighter.id: fighter.name for fighter in state.combat.combatants.values()}
        names |= {hero.id: hero.name for hero in state.party.values()}
    return names


def _hidden_monsters(*states: GameState) -> set[str]:
    """Id монстров, спрятанных от отряда хотя бы в одном из состояний."""
    return {
        fighter.id
        for state in states
        if state.combat is not None
        for fighter in state.combat.combatants.values()
        if is_hidden_monster(fighter)
    }


def _entry(
    event: Event,
    party: Mapping[str, Character],
    names: Mapping[str, str],
    hidden: Collection[str],
    npcs: Collection[str],
    adventure: Adventure,
    srd: SrdContent,
    secret_actors: Collection[str],
) -> FeedEntry | None:
    match event:
        case RollEvent():
            return _roll_entry(event, party, names, hidden, npcs)
        case PrivateMessageEvent(audience=audience, text=text):
            return _addressed(party, audience, MessageKind.PRIVATE, text, "личное сообщение")
        case MoveEvent(location_id=location_id):
            place = adventure.locations.get(location_id)
            line = f"Отряд приходит: {place.name if place else location_id}."
            return _addressed(party, AUDIENCE_ALL, MessageKind.SYSTEM, line, "переход")
        case CombatEvent(phase="started"):
            return _addressed(party, AUDIENCE_ALL, MessageKind.SYSTEM, "Начался бой.", "бой")
        case CombatEvent(phase="ended", winner=winner):
            line = _COMBAT_END.get(winner, _COMBAT_END[None])
            return _addressed(party, AUDIENCE_ALL, MessageKind.SYSTEM, line, "конец боя")
        case RestEvent(rest=rest):
            return _addressed(party, AUDIENCE_ALL, MessageKind.SYSTEM, _REST_TEXT[rest], "отдых")
        case InspirationEvent(character_id=character_id, granted=granted, reason=reason):
            name = names.get(character_id, character_id)
            line = (
                f"{name} получает вдохновение: {reason}."
                if granted
                else f"{name} тратит вдохновение."
            )
            audience = _author_audience([character_id], secret_actors)
            return _addressed(party, audience, MessageKind.SYSTEM, line, "вдохновение")
        case ItemEvent():
            return _item_entry(event, party, names, adventure, srd, secret_actors)
        case _:
            return None


def _addressed(
    party: Mapping[str, Character], audience: str, kind: MessageKind, text: str, what: str
) -> FeedEntry | None:
    """Строка для адресата движка (`all` или id персонажа); нет адресата — не пишется, в журнал
    идёт только вид события, без текста: в нём бывают тайные ходы и личные сообщения."""
    user_id = audience_to_user(party, audience)
    if user_id is None:
        logger.warning("Событие «%s» не записано в ленту: у адресата нет игрока.", what)
        return None
    return FeedEntry(user_id, kind, text)


def _author_audience(subjects: Iterable[str], secret_actors: Collection[str]) -> str:
    """Адресат строки о героях: тайный герой из `subjects` — он один, иначе все."""
    secret = sorted(set(subjects) & set(secret_actors))
    return secret[0] if secret else AUDIENCE_ALL


def _roll_entry(
    event: RollEvent,
    party: Mapping[str, Character],
    names: Mapping[str, str],
    hidden: Collection[str],
    npcs: Collection[str],
) -> FeedEntry | None:
    user_id = audience_to_user(party, event.audience)
    if user_id is None:
        logger.warning("Бросок не записан в ленту: у адресата нет игрока.")
        return None
    roll = event.roll
    unseen = event.actor_id in hidden
    actor = UNSEEN_ACTOR if unseen else names.get(event.actor_id, event.actor_id)
    # КД цели в карточку не идёт: у монстров он скрыт, игроки видят только попадание или промах.
    data: dict[str, Any] = {
        "actor_id": event.actor_id,
        "actor": actor,
        "label": event.label,
        "reason": roll.reason,
        "expr": roll.expr,
        "rolls": roll.rolls,
        "kept": roll.kept,
        "modifier": roll.modifier,
        "total": roll.total,
        "natural": roll.natural,
        "advantage": roll.advantage.value,
        "dc": event.dc,
        "success": event.success,
    }
    if unseen:
        # Причина броска от движка бывает с именем бойца («Скрытность: Гоблин 2»): остаётся
        # название броска.
        del data["actor_id"]
        data["reason"] = event.label
    elif event.actor_id in npcs:
        # Id NPC из данных приключения (слаг вроде `cult-leader`) бывает откровеннее имени.
        del data["actor_id"]
    text = f"{actor}: {event.label} — {roll.total}"
    if event.dc is not None:
        text += f" против Сл {event.dc}"
    if event.success is not None:
        hit, miss = ("попадание", "промах") if event.target_ac is not None else ("успех", "провал")
        text += f": {hit if event.success else miss}"
    return FeedEntry(user_id, MessageKind.ROLL, text, data)


def _holder(holder: str, names: Mapping[str, str], adventure: Adventure) -> tuple[str, str]:
    """Вид держателя предмета и его название; держатель — `character:<id>`, `npc:<id>` и т. п."""
    kind, _, ident = holder.partition(":")
    if kind == "character":
        return kind, names.get(ident, ident)
    if kind == "npc":
        npc = adventure.npcs.get(ident)
        return kind, npc.name if npc else ident
    return kind, ident


def _item_entry(
    event: ItemEvent,
    party: Mapping[str, Character],
    names: Mapping[str, str],
    adventure: Adventure,
    srd: SrdContent,
    secret_actors: Collection[str],
) -> FeedEntry | None:
    item = adventure.items.get(event.item_id) or srd.items.get(event.item_id)
    title = item.name if item else event.item_id
    (from_kind, giver), (to_kind, taker) = (
        _holder(holder, names, adventure) for holder in (event.holder_from, event.holder_to)
    )
    if to_kind == "character":
        line = (
            f"{giver} передаёт «{title}»: теперь у героя {taker}."
            if from_kind == "character"
            else f"{taker} получает «{title}»."
        )
    elif from_kind == "character":
        line = {
            "gone": f"{giver} расстаётся с «{title}»: предмет ушёл из игры.",
            "location": f"{giver} оставляет «{title}» здесь.",
        }.get(to_kind, f"{giver} отдаёт «{title}».")
    else:
        return None
    subjects = [
        holder.partition(":")[2]
        for holder in (event.holder_from, event.holder_to)
        if holder.startswith("character:")
    ]
    audience = _author_audience(subjects, secret_actors)
    return _addressed(party, audience, MessageKind.SYSTEM, line, "предмет")


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
        "character": character_view(character, srd) if character is not None else None,
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
