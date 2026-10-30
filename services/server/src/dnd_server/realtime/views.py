"""Проекция игры для игрока: снимок и события, которые уходят в сокеты.

Это единственное место, где состояние игры превращается в то, что видит клиент. Всё построено из
белых списков: поле попадает в ответ, только если оно перечислено здесь. `GameState` и `Character`
целиком клиенту не уходят, потому что в них лежат данные мастера (`pc_hooks`, `world`, летопись,
точные HP монстров) и чужие листы персонажей. Снимок (`snapshot_for`) и события строятся одними и
теми же функциями, так что событие `party` — это ровно блок `party` снимка и так далее: клиент
заменяет блок целиком.

Приватность. Лента отбирает сообщения по адресату (`all` или сам игрок), личное сообщение не
показывается как «общее» ни при каких данных, тайные ходы и броски лежат в ленте с
адресатом-автором.
Журнал хода мастера (`dm_turn_logs`: результаты `get_location`, `get_npc`, `search_lore` с заметками
мастера и тайнами NPC) сюда не попадает вовсе, а копия приключения игры — только публичными
названиями: имена NPC бойцов и названия предметов, которые держит герой. Адресаты событий движка —
id персонажей; в id игрока их переводит `audience_to_user`, и это единственное такое место.

Сообщения ленты из событий хода мастера (`feed_entries`) строятся здесь же: что из событий
движка становится строкой ленты и кому она видна. Точных HP монстров и КД монстров в строках нет;
в карточке атаки монстра по герою есть КД героя-цели (решение владельца). События без автора
(находки, флаги) строк не дают вовсе: об этом игроки узнают из рассказа. Броски монстра,
спрятанного от отряда, идут от имени «Кто-то невидимый» без имени и id бойца, как и сам монстр
отсутствует в `combat_view`.

Боец-монстр, за которым стоит NPC приключения, называется в порядке хода и в карточках бросков
именем NPC (`npc_fighter_names`), а не именем статблока; причина его броска от движка в карточке
заменена названием броска, как у спрятанного: в ней бывает имя статблока. Скрытый монстр не
называется вовсе.

Числа листа (КД, бонусы навыков и спасбросков, Сл заклинаний, атаки оружием) считает движок:
`derived_view` только раскладывает его ответы в блок `derived`, формул здесь нет и клиент их не
повторяет. Так же движок решает, может ли боец-герой действовать (`can_act` в порядке хода): это
тот вопрос, по которому сервер ждёт ход героя, и клиент правил не повторяет.

Дневник отряда лежит в состоянии игры: записи с адресатом `all` или id персонажа, как у
событий движка, и текстом только из публичного (тайны приключения в него не попадают — это решает
движок). Игроку уходят общие записи и личные записи его героя (`journal_view`) — в снимке, событием
`journal_entry` о каждой новой или дополненной за ход записи и блоком `journal` целиком после
отката хода. Игра, записанная до дневника, получает стартовый дневник при чтении для снимка — в
памяти, в базу его запишет следующий ход мастера.

Фаза (`phase_view`) несёт и момент начала фазы хода (`phase_since`, по нему клиент считает минуту
до «Повторить» по серверному времени) и признак `can_regenerate`: можно ли сейчас нажать
«Перегенерировать». Правило отката (`regeneration`) живёт здесь, а не в `dm.turn`: его проверяет
откат хода мастера и видит клиент, а `dm.turn` импортирует этот модуль, а не наоборот. Так у кнопки
и у сервера одно правило.

Привал в снимке — блок `rest` (`rest_view`): вид, срок, личные идеи «расскажи о
себе» только героя смотрящего (из `games.rest_ideas`) и реплики у костра этого привала (записи ленты
`camp_line` открытого хода «конец привала»). Вне фазы `camp` блока нет. Кадры привала —
`rest_started` и `rest_ended` всем, `rest_idea` с текстом одной идеи — только игроку её героя.

Русские названия состояний и эффектов берутся из данных SRD движка (состояния, заклинания, умения
классов) и из таблицы названий боевых меток движка (`MARK_NAMES_RU`, например «Помощь» для
`helped`), а не из копии на сервере. Для id, которых нет и там, клиент получает сам id.
"""

import logging
import math
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.abilities import ABILITY_NAMES_RU, SKILL_ABILITY, SKILL_NAMES_RU, Ability, Skill
from dnd_engine.adventure.loader import adventure_from_data
from dnd_engine.adventure.schema import Adventure, Item
from dnd_engine.character import (
    ActiveBuff,
    ActiveCondition,
    Character,
    InventoryItem,
    ability_mod,
    armor_class,
    passive_perception,
    prof_bonus,
    save_bonus,
    skill_bonus,
    spell_attack_bonus,
    spell_save_dc,
    weapon_attack,
)
from dnd_engine.combat.engine import INVISIBLE, MARK_NAMES_RU, can_act
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
from dnd_engine.journal import JournalEntry, changed_entries, open_journal
from dnd_engine.state import GameState
from dnd_engine.tools.lore import item_holder
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
    Turn,
    TurnPhase,
    TurnStatus,
    User,
    iso_utc,
)
from dnd_server.dm.camp_flow import RestKind, rest_kind
from dnd_server.games.service import Conflict, get_game

logger = logging.getLogger(__name__)

# Сколько последних сообщений, видимых игроку, входит в ленту снимка.
FEED_LIMIT = 200

# Статус хода игрока для всех: `idle` — ещё ничего не написал (строки хода нет или в черновике нет
# видимого текста), `writing` — черновик с текстом, `ready` — сдан, `pass` — пас. Что ход тайный и
# что в нём написано, статус не говорит. Автосдача и автопас по таймеру для остальных выглядят так
# же, как ручные. «Ещё не сдал» для сбора ходов — и `idle`, и `writing`.
IDLE = "idle"
WRITING = "writing"
SETTLED_STATUS: Mapping[ActionStatus, str] = MappingProxyType(
    {
        ActionStatus.SUBMITTED: "ready",
        ActionStatus.AUTO_SUBMITTED: "ready",
        ActionStatus.PASS: "pass",
        ActionStatus.AUTO_PASS: "pass",
    }
)


def public_status(action: PlayerAction | None) -> str:
    """Публичный статус игрока по его строке хода в текущем ходе (`None` — строки нет)."""
    if action is None:
        return IDLE
    if action.status == ActionStatus.DRAFT:
        return WRITING if action.text.strip() else IDLE
    return SETTLED_STATUS[action.status]


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


def _running_deadline(game: Game) -> str | None:
    """Срок идущего отсчёта; на паузе срока нет (остаток — в `phase.remaining_seconds`)."""
    running = game.phase != GamePhase.PAUSED and game.deadline_at is not None
    return iso_utc(game.deadline_at) if running and game.deadline_at is not None else None


def phase_view(game: Game, state: GameState | None, *, can_regenerate: bool) -> dict[str, Any]:
    """Фаза игры, фаза хода, отсчёт и сцена. До старта игры состояния нет, и сцены тоже.

    Идущий отсчёт описывает срок `deadline_at`. На паузе отсчёт стоит: срока нет, а остаток в
    секундах (вверх) лежит в `remaining_seconds`, чтобы клиент показал его неподвижным; после
    «Продолжить» приходит новый срок. `phase_since` — когда началась фаза хода (пусто у игр,
    записанных до миграции 0007, пока фаза не сменится); `can_regenerate` считает вызывающий
    (`can_regenerate()`): для него нужны ходы игры из базы."""
    scene = None
    if state is not None:
        scene = {"type": state.scene.type.value, "day": state.time.day, "part": state.time.part}
    remaining = game.paused_remaining()
    return {
        "phase": game.phase.value,
        "turn_phase": game.turn_phase.value if game.turn_phase is not None else None,
        "deadline_at": _running_deadline(game),
        "remaining_seconds": None if remaining is None else math.ceil(remaining.total_seconds()),
        "scene": scene,
        "phase_since": None if game.phase_since is None else iso_utc(game.phase_since),
        "can_regenerate": can_regenerate,
    }


# Из каких фаз допустима «Перегенерировать»: игроки обсуждают следующий ход, сбор ещё не начался.
REGENERATE_FROM = (TurnPhase.DISCUSSION, TurnPhase.COMBAT_WAIT, TurnPhase.CAMP)
# Отказ «Перегенерировать», когда после хода игра менялась вне хода мастера.
CHANGED_SINCE_TURN = "После хода мастера игра уже изменилась: перегенерировать нельзя."


@dataclass(frozen=True)
class Regeneration:
    """Что откатывает «Перегенерировать»: последний разобранный ход, нетронутый следующий за ним и
    снимок состояния до разобранного хода."""

    last: Turn
    following: Turn
    previous: GameStateSnapshot


async def regeneration(session: AsyncSession, game: Game) -> Regeneration:
    """Ход, который можно перегенерировать сейчас; нельзя — `Conflict` с причиной.

    Можно, пока игроки обсуждают следующий ход (`REGENERATE_FROM`), следующий за разобранным ход
    ещё открыт и не тронут, а состояние игры с его начала не менялось вне хода мастера (привал по
    кнопке хоста, кость хитов): откат стёр бы эти изменения. Привал мастера (`start_camp`)
    перегенерировать можно: его начал сам ход. Только читает базу."""
    if game.turn_phase not in REGENERATE_FROM:
        raise Conflict("Перегенерировать ответ можно, пока игроки обсуждают следующий ход.")
    last = await session.scalar(
        select(Turn)
        .where(Turn.game_id == game.id, Turn.status == TurnStatus.DONE)
        .order_by(Turn.number.desc(), Turn.created_at.desc())
        .limit(1)
    )
    if last is None:
        raise Conflict("Мастеру пока нечего перегенерировать: ни один ход не разобран.")
    following = await session.get(Turn, game.current_turn_id) if game.current_turn_id else None
    if (
        following is None
        or following.status != TurnStatus.OPEN
        or following.number != last.number + 1
    ):
        raise Conflict("Следующий ход уже начат: перегенерировать нельзя.")
    started_at = following.state_version_before
    if started_at is not None and game.state_version != started_at:
        raise Conflict(CHANGED_SINCE_TURN)
    version_before = last.state_version_before
    previous = (
        None
        if version_before is None
        else await session.get(GameStateSnapshot, (game.id, version_before))
    )
    if previous is None:
        raise Conflict("Состояния до этого хода нет: перегенерировать нельзя.")
    return Regeneration(last, following, previous)


async def can_regenerate(session: AsyncSession, game: Game) -> bool:
    """Можно ли сейчас нажать «Перегенерировать»: игра идёт (на паузе кнопка отвечает 409, как
    все кнопки хода) и `regeneration` не откажет. Вне идущей игры и фаз обсуждения отвечает сразу,
    без запросов к базе."""
    if game.phase != GamePhase.PLAYING or game.turn_phase not in REGENERATE_FROM:
        return False
    try:
        await regeneration(session, game)
    except Conflict:
        return False
    return True


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


def npc_fighter_names(state: GameState, adventure: Adventure) -> dict[str, str]:
    """Публичные имена бойцов-монстров, за которыми стоит NPC приключения: id бойца → имя NPC.

    У боя в приключении нет `npc_id`, связь идёт через статблок и место, как у движка, когда он
    отмечает павших NPC. Боец называется именем NPC, только если связь однозначна: в бою ровно
    один монстр (спрятанные считаются) с этим статблоком и ровно один живой NPC с этим статблоком
    сейчас в локации отряда (с учётом переходов NPC в мире). Иначе остаётся имя статблока.
    Спрятанный монстр не называется: имя NPC выдало бы засаду. Кроме имени от NPC ничего не
    берётся."""
    combat = state.combat
    if combat is None:
        return {}
    monsters = [f for f in combat.combatants.values() if f.kind == "monster"]
    per_statblock = Counter(fighter.ref for fighter in monsters)
    npcs_here = [
        npc
        for npc in adventure.npcs.values()
        if npc.statblock is not None
        and state.world.npc_alive.get(npc.id, True)
        and state.world.npc_location.get(npc.id, npc.location_id) == state.location_id
    ]
    names: dict[str, str] = {}
    for fighter in monsters:
        if is_hidden_monster(fighter) or per_statblock[fighter.ref] != 1:
            continue
        candidates = [npc for npc in npcs_here if npc.statblock == fighter.ref]
        if len(candidates) == 1:
            names[fighter.id] = candidates[0].name
    return names


def combat_view(state: GameState, srd: SrdContent, adventure: Adventure) -> dict[str, Any] | None:
    """Бой для всех: порядок, текущий ход, зоны с бойцами. Монстры описаны словами, без чисел.

    Спрятавшегося монстра в ответе нет вовсе — ни в порядке хода, ни в зоне: имя и положение
    выдали бы засаду. Если сейчас его ход, `current_id` пуст. Спрятавшийся герой виден отряду.
    Монстр, за которым стоит NPC, назван его именем (`npc_fighter_names`).

    У героя есть `can_act`: способен ли он действовать (на ногах и не парализован, оглушён и так
    далее). Это тот же вопрос движка (`engine.can_act`), по которому сервер решает, ждать ли ход
    героя (`dm.combat_flow.waiting_hero`) и принимать ли его «Готов»: очередь может стоять на
    герое, который не ходит, и клиент не должен открывать ему поле хода. У монстров признака нет."""
    combat = state.combat
    if combat is None:
        return None
    npc_names = npc_fighter_names(state, adventure)
    fighters = [
        fighter
        for fighter in (combat.combatants[fighter_id] for fighter_id in combat.order)
        if not is_hidden_monster(fighter)
    ]
    order = []
    for fighter in fighters:
        base = {
            "id": fighter.id,
            "name": npc_names.get(fighter.id, fighter.name),
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
                # Героя нет в отряде (рассинхрон состояния): ходить некому, а движок на нём упал бы.
                "can_act": hero is not None and can_act(state, fighter.id),
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


def character_view(
    character: Character, srd: SrdContent, adventure: Adventure, state: GameState | None
) -> dict[str, Any]:
    """Лист персонажа. Уходит только его владельцу (`character_event`, снимок игрока).

    Состояния (`conditions`) и эффекты (`buffs`) проходят через белый список блока отряда
    (`_effects`: id, название, срок). Без этого лист выдавал бы `source` и `source_id`, а в них id
    спрятанного монстра, который опутал героя: монстра нет ни в порядке хода, ни в зонах боя, и лист
    не должен называть его вместо них. В снаряжении после вещей персонажа стоят
    предметы приключения, которые он держит (`_inventory_view`); `state` — состояние игры, а в лобби
    его нет. Остальные поля персонажа идут как есть, а числа, которые из них выводит движок, — в
    блоке `derived` (`derived_view`)."""
    return {
        **character.model_dump(mode="json", exclude={"conditions", "buffs", "inventory"}),
        "inventory": _inventory_view(character, adventure, state),
        "conditions": _effects(character.conditions, srd),
        "buffs": _effects(character.buffs, srd),
        "derived": derived_view(character, srd),
    }


def _inventory_view(
    character: Character, adventure: Adventure, state: GameState | None
) -> list[dict[str, Any]]:
    """Снаряжение героя: его вещи в его порядке, затем предметы приключения, которые он держит.

    Движок кладёт в `Character.inventory` только вещи SRD; предмет приключения числится за героем
    в `world.item_holders`, поэтому строки предметов приключения строятся по нему
    (`_held_adventure_items`). У такой строки рядом с `item_id` стоит `name` из копии приключения
    игры: справочник создания героя знает только предметы SRD, и без названия клиент показал бы
    id (`chest-key`). Только название: описание, держатель и прочее из приключения в лист не
    попадают. Предмет приключения один, поэтому `qty` равно 1 и он не надет. У вещей SRD ключа
    `name` нет."""
    lines = [entry.model_dump(mode="json") for entry in character.inventory]
    if state is not None:  # в лобби состояния игры нет, и предметов приключения у героев тоже
        lines += [
            {**InventoryItem(item_id=item.id).model_dump(mode="json"), "name": item.name}
            for item in _held_adventure_items(state, adventure, character.id)
        ]
    return lines


def _held_adventure_items(state: GameState, adventure: Adventure, character_id: str) -> list[Item]:
    """Предметы приключения в руках героя по порядку id; ушедшие, лежащие в мире и чужие не в счёт.

    Кто держит предмет, решает `item_holder` движка: переопределение мира (`give_item`,
    `take_item`, расход в бою) и держатель из приключения. Мастер читает ту же правду в контексте
    хода (`dm.context._carried_adventure_items`): фильтр повторяет её, а совпадение листа и
    контекста держит тест `test_the_sheet_lists_the_same_items_as_the_context_of_the_master`."""
    holder = f"character:{character_id}"
    return [
        item
        for item in sorted(adventure.items.values(), key=lambda item: item.id)
        if item_holder(state, adventure, item.id) == holder
    ]


def derived_view(character: Character, srd: SrdContent) -> dict[str, Any]:
    """Числа листа, посчитанные движком по живому персонажу: клиент ничего не считает.
    Сервер только называет значения: русские названия берутся из таблиц движка, порядок —
    порядок перечислений `Ability` и `Skill`. Заклинательных чисел у героя без заклинаний нет
    (`None`). В блоке нет источников эффектов, чужих листов и чисел монстров."""
    return {
        "armor_class": armor_class(character, srd),
        "proficiency_bonus": prof_bonus(character),
        "passive_perception": passive_perception(character),
        "abilities": [
            {
                "id": ability.value,
                "name": ABILITY_NAMES_RU[ability],
                "modifier": ability_mod(character, ability),
                "save_bonus": save_bonus(character, ability),
            }
            for ability in Ability
        ],
        "skills": [
            {
                "id": skill.value,
                "name": SKILL_NAMES_RU[skill],
                "ability": SKILL_ABILITY[skill].value,
                "bonus": skill_bonus(character, skill),
            }
            for skill in Skill
        ],
        "spell_save_dc": spell_save_dc(character, srd),
        "spell_attack_bonus": spell_attack_bonus(character, srd),
        "attacks": _attacks(character, srd),
    }


def _attacks(character: Character, srd: SrdContent) -> list[dict[str, Any]]:
    """Атаки оружием из инвентаря в его порядке; `equipped` отличает надетое от запасного.
    Предметы, которых нет в SRD или которые не оружие (броня, наборы, предметы приключения),
    атакой не считаются."""
    attacks = []
    for entry in character.inventory:
        item = srd.items.get(entry.item_id)
        if item is None or item.weapon is None:
            continue
        attack = weapon_attack(character, entry.item_id, srd)
        attacks.append(
            {
                "item_id": attack.item_id,
                "name": attack.name,
                "equipped": entry.equipped,
                "bonus": attack.bonus,
                "damage": attack.damage,
                "damage_type": attack.damage_type,
                "range": attack.range,
                "ability": attack.ability.value,
            }
        )
    return attacks


def journal_view(state: GameState, user_id: str) -> list[dict[str, Any]]:
    """Дневник для игрока `user_id` в порядке записей: общие и личные записи его героя.

    Кому видна запись, решает `audience_to_user`, как у ленты: чужую личную запись игрок не видит,
    запись героя без игрока не видит никто, а при неоднозначном «все» никто не видит и общую.
    Поля записи идут как есть: её текст движок собрал только из того, что могут знать игроки."""
    return [
        entry.model_dump(mode="json")
        for entry in state.journal
        if audience_to_user(state.party, entry.audience) in (AUDIENCE_ALL, user_id)
    ]


def rest_view(
    game: Game, state: GameState, my_ideas: list[str], lines: list[dict[str, Any]]
) -> dict[str, Any]:
    """Идущий привал глазами игрока: вид (`null` у сцены — долгий привал первой версии игры), срок
    (на паузе `null`), свои идеи и реплики у костра этого привала (записи ленты `camp_line`)."""
    return {
        "kind": rest_kind(state),
        "deadline_at": _running_deadline(game),
        "my_ideas": my_ideas,
        "lines": lines,
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


def phase_event(game: Game, state: GameState | None, *, can_regenerate: bool) -> dict[str, Any]:
    return {"type": "phase", "phase": phase_view(game, state, can_regenerate=can_regenerate)}


def action_auto_submitted_event() -> dict[str, Any]:
    """«Ход сдан» автору черновика с текстом, который сдал отсчёт. Текста хода в кадре нет: его
    адресат знает свой черновик, остальные этот кадр не получают."""
    return {"type": "action_auto_submitted"}


def player_status_event(user_id: str, action: PlayerAction | None) -> dict[str, Any]:
    """Публичный статус игрока для всех; `action` — его строка хода (`None` — строки ещё нет)."""
    return {"type": "player_status", "user_id": user_id, "status": public_status(action)}


def feed_item_event(message: Message) -> dict[str, Any]:
    """Событие ленты. Адресат рассылки — `message.audience`: вызывающий отдаёт его хабу как есть."""
    return {"type": "feed_item", "item": feed_item(message)}


def rest_started_event(game: Game, state: GameState) -> dict[str, Any]:
    """Начался привал (всем): вид и срок. Идеи и реплики приходят своими кадрами."""
    return {
        "type": "rest_started",
        "kind": rest_kind(state),
        "deadline_at": _running_deadline(game),
    }


def rest_idea_event(text: str) -> dict[str, Any]:
    """Идея для рассказа: адресат рассылки — игрок героя этой идеи, и больше никто."""
    return {"type": "rest_idea", "text": text}


def rest_ended_event(kind: RestKind) -> dict[str, Any]:
    """Привал кончился (всем), мастер рассказал его итог."""
    return {"type": "rest_ended", "kind": kind}


def feed_withdrawn_event(message_id: int) -> dict[str, Any]:
    """Событие-надгробие: запись ленты снята («Перегенерировать»), клиент убирает её по `id`.

    Снимок такие записи не содержит вовсе, поэтому после переподключения надгробие не нужно.
    Адресат рассылки — тот же, кому уходила сама запись."""
    return {"type": "feed_item", "item": {"id": message_id, "superseded": True}}


def party_event(state: GameState, srd: SrdContent) -> dict[str, Any]:
    return {"type": "party", "party": party_view(state, srd)}


def combat_event(state: GameState, srd: SrdContent, adventure: Adventure) -> dict[str, Any]:
    return {"type": "combat", "combat": combat_view(state, srd, adventure)}


def character_event(
    character: Character, srd: SrdContent, adventure: Adventure, state: GameState | None
) -> dict[str, Any]:
    return {"type": "character", "character": character_view(character, srd, adventure, state)}


def state_events(
    state: GameState, srd: SrdContent, adventure: Adventure
) -> list[tuple[dict[str, Any], str]]:
    """События после нового состояния игры: пары `(событие, адресат)` для `Hub.send`.

    Отряд и бой уходят всем, лист персонажа — только его игроку. У персонажа без игрока листа
    не получает никто.
    """
    deliveries: list[tuple[dict[str, Any], str]] = [
        (party_event(state, srd), AUDIENCE_ALL),
        (combat_event(state, srd, adventure), AUDIENCE_ALL),
    ]
    for character in state.party.values():
        if character.player_id is not None:
            sheet = character_event(character, srd, adventure, state)
            deliveries.append((sheet, character.player_id))
    return deliveries


def journal_entry_event(entry: JournalEntry) -> dict[str, Any]:
    return {"type": "journal_entry", "entry": entry.model_dump(mode="json")}


def journal_event(state: GameState, user_id: str) -> dict[str, Any]:
    return {"type": "journal", "entries": journal_view(state, user_id)}


def journal_entry_events(before: GameState, after: GameState) -> list[tuple[dict[str, Any], str]]:
    """Записи дневника, новые или дополненные между состояниями (`changed_entries` движка): пары
    `(событие, адресат)` для `Hub.send`, общая запись — всем, личная — только игроку её героя.

    Запись, адресата которой нет среди игроков, не уходит никому; в журнал идёт только раздел, без
    ссылки и текста: личная запись говорит о том, что герой узнал тайно."""
    deliveries: list[tuple[dict[str, Any], str]] = []
    for entry in changed_entries(before.journal, after.journal):
        user_id = audience_to_user(after.party, entry.audience)
        if user_id is None:
            logger.warning(
                "Запись дневника (раздел %s) не разослана: у адресата нет игрока.", entry.section
            )
            continue
        deliveries.append((journal_entry_event(entry), user_id))
    return deliveries


def journal_events(state: GameState, user_ids: Iterable[str]) -> list[tuple[dict[str, Any], str]]:
    """Дневник целиком каждому игроку из `user_ids` — его видимые записи, клиент заменяет ими свой
    блок. Нужен после отката хода: запись, которой в состоянии больше нет, уходит и с экрана."""
    return [(journal_event(state, user_id), user_id) for user_id in user_ids]


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


# Под этим именем в ленте бросает монстр, спрятанный от отряда.
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
    бросает под именем из приключения, а в `data` карточки его id нет. Бросок монстра,
    спрятанного в любом из двух состояний, идёт без имени и id бойца (`UNSEEN_ACTOR`): `combat_view`
    прячет такого монстра целиком, и лента не должна выдать засаду. Скрывается с запасом: событий
    между состояниями здесь нет, а лишняя безымянная карточка безвреднее раскрытой засады. У такого
    монстра и у бойца под именем NPC причина броска от движка (`data.reason`) заменена названием
    броска: в ней стоит имя по статблоку («Инициатива: Фанатик культа»). Адресат
    броска и личного сообщения задан движком (id персонажа) и переводится в id игрока через
    `audience_to_user`; событие, адресата которого нет среди игроков, не пишется и не рассылается.
    Строки о предметах и вдохновении тайного героя уходят только ему. События без строки (находки,
    флаги, отношение NPC, смена сцены, состояния, действия, урон и лечение, летопись) в ленту не
    идут: у находок и флагов нет автора, а урон и лечение несут точные HP (`hp_after`), которые
    игрокам показывают словами и блоками отряда и боя."""
    # NPC приключения бросают в состязании (`contest`): в карточке только имя, без id из данных
    # приключения. Герой или боец с тем же id важнее, как и при поиске участника в движке.
    # Боец, за которым стоит NPC, бросает под именем NPC, как назван в порядке хода (`combat_view`).
    npc_named = npc_fighter_names(before, adventure) | npc_fighter_names(after, adventure)
    fighters = _names(before, after) | npc_named
    npcs = {npc.id: npc.name for npc in adventure.npcs.values() if npc.id not in fighters}
    names = npcs | fighters
    hidden = _hidden_monsters(before, after)
    monsters = _monsters(before, after)
    entries: list[FeedEntry] = []
    for event in events:
        entry = _entry(
            event,
            after.party,
            names,
            hidden,
            npc_named.keys(),
            monsters,
            npcs,
            adventure,
            srd,
            secret_actors,
        )
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


def _monsters(*states: GameState) -> set[str]:
    """Id бойцов-монстров боя хотя бы в одном из состояний (в том числе спрятанных)."""
    return {
        fighter.id
        for state in states
        if state.combat is not None
        for fighter in state.combat.combatants.values()
        if fighter.kind == "monster"
    }


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
    npc_named: Collection[str],
    monsters: Collection[str],
    npcs: Collection[str],
    adventure: Adventure,
    srd: SrdContent,
    secret_actors: Collection[str],
) -> FeedEntry | None:
    match event:
        case RollEvent():
            return _roll_entry(event, party, names, hidden, npc_named, monsters, npcs)
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
    npc_named: Collection[str],
    monsters: Collection[str],
    npcs: Collection[str],
) -> FeedEntry | None:
    user_id = audience_to_user(party, event.audience)
    if user_id is None:
        logger.warning("Бросок не записан в ленту: у адресата нет игрока.")
        return None
    roll = event.roll
    unseen = event.actor_id in hidden
    actor = UNSEEN_ACTOR if unseen else names.get(event.actor_id, event.actor_id)
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
    # КД цели называется, только когда цель — герой (решение владельца): так атакует монстр, а
    # движок бьёт лишь противоположную сторону. КД монстра скрыт, игроки видят только попадание
    # или промах. Цель в событии не названа, поэтому её сторону определяет сторона бойца: бросок
    # не монстра боя (герой, NPC, неизвестный) КД не получает ни при каких данных.
    if event.target_ac is not None and event.actor_id in monsters:
        data["target_ac"] = event.target_ac
    if unseen:
        del data["actor_id"]
    elif event.actor_id in npcs:
        # Id NPC из данных приключения (слаг вроде `cult-leader`) бывает откровеннее имени.
        del data["actor_id"]
    if unseen or event.actor_id in npc_named:
        # Причина броска от движка бывает с именем бойца по статблоку («Скрытность: Гоблин 2»,
        # «Инициатива: Фанатик культа»). Боец идёт под другим именем (его не видно или за ним NPC),
        # и причина выдала бы настоящее: остаётся название броска. Решает боец, а не вид броска.
        data["reason"] = event.label
    text = f"{actor}: {event.label} — {roll.total}"
    if event.dc is not None:
        text += f" против Сл {event.dc}"
    elif "target_ac" in data:
        text += f" против КД {data['target_ac']}"
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


async def _current_state(
    session: AsyncSession, game: Game, adventure: Adventure
) -> GameState | None:
    """Текущая версия состояния игры; до старта игры состояния нет. Версию читает `to_state`.

    Игра, записанная до дневника, получает стартовый дневник (`open_journal`) только в памяти:
    снимок ничего не пишет в базу, а сохранит дневник коммит следующего хода мастера."""
    row = await session.get(GameStateSnapshot, (game.id, game.state_version))
    return None if row is None else open_journal(row.to_state(), adventure)


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


async def _rest_of(
    session: AsyncSession, game: Game, state: GameState | None, user: User
) -> dict[str, Any] | None:
    """Блок привала снимка: только в фазе `camp`. Идеи — героя смотрящего, реплики — открытого
    хода «конец привала» по порядку (отдельным запросом: лента снимка ограничена `FEED_LIMIT`)."""
    if state is None or game.turn_phase != TurnPhase.CAMP:
        return None
    player = await session.get(GamePlayer, (game.id, user.id))
    hero_id = None if player is None else player.character_id
    ideas = (game.rest_ideas or {}).get(hero_id, []) if hero_id else []
    rows = await session.scalars(
        select(Message)
        .where(
            Message.game_id == game.id,
            Message.turn_id == game.current_turn_id,
            Message.kind == MessageKind.CAMP_LINE,
            Message.superseded.is_(False),
        )
        .order_by(Message.id)
    )
    return rest_view(game, state, list(ideas), [feed_item(message) for message in rows])


def _combat_of(
    adventure: Adventure, state: GameState | None, srd: SrdContent
) -> dict[str, Any] | None:
    """Блок боя снимка; приключение нужно для имён NPC в порядке хода."""
    if state is None or state.combat is None:
        return None
    return combat_view(state, srd, adventure)


async def snapshot_for(
    session: AsyncSession, user: User, game_id: str, *, srd: SrdContent
) -> dict[str, Any]:
    """Полный снимок игры для игрока `user`; не участнику отказ (`NotFound`, как у REST).

    Сессию и SRD передаёт вызывающий: снимок не открывает соединений сам, и сокет не держит базу
    открытой, пока длится игра.
    """
    game_out = await get_game(session, user, game_id)  # проверка участия и сводка лобби
    game = await session.get(Game, game_id)
    # Копия приключения нужна листу (названия предметов, которые держит герой), бою (имена NPC) и
    # стартовому дневнику игры, записанной до него.
    adventure = adventure_from_data(game.adventure_data)
    state = await _current_state(session, game, adventure)
    actions = await _turn_actions(session, game)
    mine = actions.get(user.id)
    character = await _my_character(session, game_id, user, state)
    return {
        "type": "snapshot",
        # Отсчёт идёт по серверному времени: клиент вычтет разницу часов.
        "server_time": iso_utc(datetime.now(UTC)),
        "game": game_out.model_dump(mode="json"),
        "host_user_id": game.host_user_id,
        "phase": phase_view(game, state, can_regenerate=await can_regenerate(session, game)),
        "statuses": (
            {
                player.user_id: public_status(actions.get(player.user_id))
                for player in game_out.players
            }
            if game.current_turn_id is not None
            else {}
        ),
        "character": (
            character_view(character, srd, adventure, state) if character is not None else None
        ),
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
        "combat": _combat_of(adventure, state, srd),
        "feed": await _feed(session, game_id, user),
        "journal": journal_view(state, user.id) if state is not None else [],
        "rest": await _rest_of(session, game, state, user),
    }
