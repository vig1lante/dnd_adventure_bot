"""Правила боя по зонам: начало боя, перемещение, атака, урон, лечение, смена хода, заклинания,
простые действия и действия монстров.

Функции, меняющие бой, получают `GameState` и возвращают новое состояние и список событий;
входной объект не меняется (работа идёт на глубокой копии). Нарушения правил — `CombatError`.
Броски идут только через переданный `Rng`.

Где что хранится: HP, состояния, эффекты и концентрация персонажа — в `Character`, HP, КД и
состояния монстра — в его `Combatant`; `Combatant` персонажа хранит только положение в бою и
флаги хода. Метки на цели («Направленный снаряд», «Метка охотника») лежат в её `conditions`
как `ActiveCondition(id=<эффект>, source=<id заклинателя>)`. Мёртвый персонаж — три провала
спасбросков от смерти, мёртвый монстр — 0 HP. Эффекты «до начала своего хода» («Безрассудная
атака», «Уклонение») живут в `turn_flags` и `dodging` и сбрасываются в начале хода бойца.

Модули правил (`RuleSet`) читает `simple_action` (флаги боя) и `end_scene` (триггеры конца
сцены), поэтому параметр `rules` есть у них (у `simple_action` по умолчанию `NO_RULES` —
правила SRD без модулей). Флаги: зелье и толчок тратят бонусное действие вместо действия
(`potion_bonus_action`, `shove_bonus_action`), «Помощь» поднимает упавшего союзника с 1 HP
(`help_revives`); проверка скрытности идёт через хуки проверок набора. Остальные функции
флагов не читают и `rules` не принимают.

Добавки «раз за ход» (Скрытая атака, Убийца колоссов, «Свирепый атакующий») по SRD действуют в
любой ход, в том числе в чужой (атака по возможности, подготовленная атака): их флаги
сбрасываются в начале каждого хода боя у всех бойцов (`ONCE_PER_TURN_FLAGS`). Остальные флаги
хода живут до начала своего хода бойца.

Союзники в `engaged_with` не записываются (вовлечение — только с противником), поэтому
«вовлечённый союзник» из плана означает союзника в той же зоне: до него достаёт касание, ему
можно передать зелье.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from dnd_engine.abilities import Ability, Skill, modifier
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import (
    IMPROVISED_WEAPON_ID,
    ActiveBuff,
    ActiveCondition,
    Character,
    DeathSaves,
    ability_mod,
    armor_class,
    improvised_attack,
    prof_bonus,
    save_bonus,
    spell_attack_bonus,
    spell_save_dc,
    weapon_attack,
)
from dnd_engine.checks import (
    DWARVEN_RESILIENCE_TRAIT,
    CheckResult,
    ability_check,
    apply_bless,
    apply_luck,
    combine_advantage,
    engine_saving_throw,
    monster_saving_throw,
)
from dnd_engine.combat.model import Combat, Combatant, Zone, ZoneLink
from dnd_engine.conditions import (
    DEATH_SAVE_FAILURES_TO_DIE,
    UNCONSCIOUS,
    ZERO_HP_SOURCE,
    attack_modes,
    auto_crit,
    cannot_move,
    defense_modes,
    is_dead,
    is_incapacitated,
    regain_consciousness,
    resists_all_damage,
)
from dnd_engine.content import MonsterAction, MonsterDef, SpellcastingDef, SpellDef, SrdContent
from dnd_engine.dice import Advantage, parse, roll
from dnd_engine.events import (
    ActionEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    HealEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.modules.base import (
    HELP_REVIVES,
    NO_RULES,
    POTION_BONUS_ACTION,
    SHOVE_BONUS_ACTION,
    RuleSet,
)
from dnd_engine.rest import (
    AID_BUFF,
    BREATH_WEAPON_TRAIT,
    RELENTLESS_ENDURANCE_TRAIT,
    end_buffs,
)
from dnd_engine.rng import Rng
from dnd_engine.state import GameState, Scene, SceneType
from dnd_engine.units import meters_text

# Особенности, черты и эффекты, на которые опирается бой (соответствие данным SRD проверяет тест).
ALERT_FEAT = "alert"  # черта «Бдительный»: бонус мастерства к инициативе
SAVAGE_ATTACKER_FEAT = "savage_attacker"  # черта «Свирепый атакующий»
SNEAK_ATTACK = "sneak_attack"  # Скрытая атака плута
COLOSSUS_SLAYER = "colossus_slayer"  # Убийца колоссов охотника
FRENZY = "frenzy"  # Бешенство берсерка
IMPROVED_CRITICAL = "improved_critical"  # Улучшенный критический удар чемпиона
RAGE_BUFF = "rage"  # эффект на персонаже, пока длится ярость
GUIDING_BOLT = "guiding_bolt"  # метка на цели: следующая атака по ней с преимуществом
HUNTERS_MARK = "hunters_mark"  # метка охотника на цели: +1к6 силового урона
RAGE_DAMAGE_BONUS = 2  # урон ярости на уровнях 1–8 по SRD
RAGE_RESISTANCES = frozenset({"bludgeoning", "piercing", "slashing"})
HUNTERS_MARK_DAMAGE = ("1d6", "force")
POTENT_CANTRIP = "potent_cantrip"  # «Мощный заговор» эвокатора
DISCIPLE_OF_LIFE = "disciple_of_life"  # «Ученик жизни» жреца: лечение ячейкой сильнее
FAVORED_ENEMY = "favored_enemy"  # «Избранный враг»: Метка охотника без ячейки
HELPED = "helped"  # метка на противнике: первая атака союзника помощника идёт с преимуществом
# Русские названия меток, которых нет в данных SRD (ни состояние, ни заклинание, ни умение класса):
# клиенту нужно название, а не id. Помощь — не состояние SRD, поэтому она не в `conditions.yaml`:
# иначе мастер мог бы наложить её внешним решением (`apply_condition`), а метку ставит только бой.
# Таблица только для чтения; сервер берёт название отсюда после данных SRD.
MARK_NAMES_RU: Mapping[str, str] = MappingProxyType({HELPED: "Помощь"})
INVISIBLE = "invisible"
HIDDEN_SOURCE = (
    "hide"  # источник «невидим» от действия «Спрятаться»: кончается атакой и заклинанием
)
# Заклинание -> (особенность, ресурс класса): сотворяется без ячейки, пока ресурс не кончился.
FREE_CASTS = {"hunters_mark": (FAVORED_ENEMY, "hunters_mark")}
# Эффекты заклинаний, которые лежат метками в `conditions` цели (у монстра иных списков нет).
MARK_EFFECTS = frozenset({GUIDING_BOLT, HUNTERS_MARK})
# Условный эффект попадания со спасброском (когти гуля) держится до конца следующего хода цели.
ON_HIT_SAVE_CONDITION_ROUNDS = 1
CRIT_ROLL, IMPROVED_CRIT_ROLL = 20, 19
CONCENTRATION_MIN_DC, CONCENTRATION_MAX_DC = 10, 30
# Внешнее состояние держится от раунда до 10 минут (100 раундов): дольше — только записью без срока.
EXTERNAL_CONDITION_MAX_ROUNDS = 100
DEATH_SAVE_DC = 10
# Добавки «раз за ход» по SRD 5.2.1 срабатывают в любой ход, а не только в свой: флаг снимается в
# начале каждого хода боя. «Бешенство» берсерка — «в свой ход», его флаг сбрасывает только свой ход.
ONCE_PER_TURN_FLAGS = ("sneak_attack_used", "colossus_slayer_used", "savage_attacker_used")
# Источники записей состояний, которые ведёт сам движок: «без сознания» от 0 HP и скрытность.
ENGINE_CONDITION_SOURCES = frozenset({ZERO_HP_SOURCE, HIDDEN_SOURCE})

Distance = Literal["engaged", "near", "far"]
# Что платит за атаку: действие своего хода, реакция по «Подготовке» (публичная `attack`) или
# реакция на уход из ближнего боя (только `move`).
AttackKind = Literal["action", "readied", "opportunity"]
_ATTACK_LABELS: dict[AttackKind, str] = {
    "action": "Атака",
    "readied": "Подготовленная атака",
    "opportunity": "Атака по возможности",
}


class CombatError(ValueError):
    """Действие в бою нарушает правила или невозможно в текущем состоянии боя."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CombatError(message)


def _active_combat(state: GameState) -> Combat:
    if state.combat is None:
        raise CombatError("бой не идёт")
    return state.combat


def _combatant(combat: Combat, fighter_id: str) -> Combatant:
    try:
        return combat.combatants[fighter_id]
    except KeyError:
        raise CombatError(f"в бою нет бойца {fighter_id!r}") from None


# Персонаж или монстр: общие поля (`name`, `hp`, `hp_max`, `conditions`) есть у обоих.
Entity = Character | Combatant


def _entity(state: GameState, fighter_id: str) -> Entity:
    """Персонаж отряда (в бою и вне боя) или монстр боя."""
    if fighter_id in state.party:
        return state.party[fighter_id]
    if state.combat is None:
        raise CombatError(f"в отряде нет персонажа {fighter_id!r}")
    return _combatant(state.combat, fighter_id)


def _is_dead(entity: Entity) -> bool:
    return is_dead(entity) if isinstance(entity, Character) else entity.hp == 0


def _is_standing(entity: Entity) -> bool:
    """На ногах: есть хиты (персонаж на 0 HP лежит без сознания, мёртвый тоже)."""
    return entity.hp > 0


def _has_feature(c: Character, feature_id: str, content: SrdContent) -> bool:
    """Особенность класса получена: подкласс в срезе один на класс, берём по уровню."""
    cls = content.classes.get(c.class_id)
    return cls is not None and any(f.id == feature_id and f.level <= c.level for f in cls.features)


def _has_feat(c: Character, feat_id: str, content: SrdContent) -> bool:
    # Черту персонаж получает от предыстории; предыстория вне данных SRD черт не даёт.
    background = content.backgrounds.get(c.background)
    return background is not None and background.feat == feat_id


def _has_trait(c: Character, trait_id: str, content: SrdContent) -> bool:
    species = content.species.get(c.species)
    return species is not None and any(trait.id == trait_id for trait in species.traits)


def _monster_def(combatant: Combatant, content: SrdContent) -> MonsterDef:
    try:
        return content.monsters[combatant.ref]
    except KeyError:
        raise CombatError(f"неизвестный монстр {combatant.ref!r}") from None


def _is_raging(entity: Entity) -> bool:
    return isinstance(entity, Character) and any(b.id == RAGE_BUFF for b in entity.buffs)


def _mark(entity: Entity, mark_id: str, source: str | None = None) -> ActiveCondition | None:
    """Метка на существе; `source` — id заклинателя (None — от любого)."""
    return next(
        (c for c in entity.conditions if c.id == mark_id and source in (None, c.source)), None
    )


def _armor_class(entity: Entity, content: SrdContent) -> int:
    return armor_class(entity, content) if isinstance(entity, Character) else entity.ac


# --- начало боя ---


@dataclass(frozen=True)
class _Entrant:
    """Участник до броска инициативы: боец, значение Лов для ничьей и бонус к броску."""

    fighter: Combatant
    dex_score: int
    initiative_bonus: int


def _party_entrants(state: GameState, zone_id: str, content: SrdContent) -> list[_Entrant]:
    entrants = []
    for c in state.party.values():
        if is_dead(c):
            continue  # мёртвый в бою не участвует и инициативу не бросает
        fighter = Combatant(
            id=c.id, kind="pc", side="party", ref=c.id, name=c.name, zone_id=zone_id
        )
        alert = prof_bonus(c) if _has_feat(c, ALERT_FEAT, content) else 0
        entrants.append(
            _Entrant(fighter, c.scores[Ability.DEX], ability_mod(c, Ability.DEX) + alert)
        )
    return entrants


def _monster_entrants(monsters: Sequence[tuple[MonsterDef, str]]) -> list[_Entrant]:
    """Монстры одного вида получают номера: `goblin_warrior-1`, `goblin_warrior-2`, ..."""
    totals = Counter(monster.id for monster, _ in monsters)
    seen: Counter[str] = Counter()
    entrants = []
    for monster, zone_id in monsters:
        seen[monster.id] += 1
        number = seen[monster.id]
        fighter = Combatant(
            id=f"{monster.id}-{number}",
            kind="monster",
            side="enemies",
            ref=monster.id,
            name=monster.name if totals[monster.id] == 1 else f"{monster.name} {number}",
            hp=monster.hp,  # в статблоке записано среднее значение
            hp_max=monster.hp,
            ac=monster.ac,
            zone_id=zone_id,
        )
        dex_score = monster.scores[Ability.DEX]
        entrants.append(_Entrant(fighter, dex_score, modifier(dex_score)))
    return entrants


def _check_battlefield(
    state: GameState,
    zones: Sequence[Zone],
    links: Sequence[ZoneLink],
    monsters: Sequence[tuple[MonsterDef, str]],
    pc_zone: str,
) -> None:
    _require(state.combat is None, "бой уже идёт")
    _require(
        any(not is_dead(c) for c in state.party.values()),
        "в отряде нет персонажей, способных сражаться",
    )
    _require(bool(monsters), "у боя нет противников")
    zone_ids = {zone.id for zone in zones}
    _require(len(zone_ids) == len(zones), "id зон в бою повторяются")
    _require(pc_zone in zone_ids, f"неизвестная зона отряда {pc_zone!r}")
    for monster, zone_id in monsters:
        _require(zone_id in zone_ids, f"монстр {monster.id!r} в неизвестной зоне {zone_id!r}")
    for link in links:
        _require(
            link.a in zone_ids and link.b in zone_ids,
            f"связь {link.a!r} — {link.b!r} ведёт в неизвестную зону",
        )


def start_combat(
    state: GameState,
    *,
    encounter_id: str | None,
    zones: Sequence[Zone],
    links: Sequence[ZoneLink],
    monsters: Sequence[tuple[MonsterDef, str]],
    pc_zone: str,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Начинает бой: расставляет бойцов по зонам, бросает инициативу всем, выстраивает очередь.

    Кости бросаются в порядке отряда, затем в порядке списка монстров. Все персонажи отряда
    встают в `pc_zone`; `monsters` — пары (статблок, id зоны). Первым ходит тот, у кого
    больше итог; при равенстве — выше Лов, затем персонаж раньше монстра. Первый ход начинается
    как любой следующий: лежащий на 0 HP персонаж, оказавшийся первым, бросает спасбросок от смерти.
    """
    _check_battlefield(state, zones, links, monsters, pc_zone)
    entrants = [*_party_entrants(state, pc_zone, content), *_monster_entrants(monsters)]
    _require(len({e.fighter.id for e in entrants}) == len(entrants), "id бойцов в бою повторяются")

    events: list[Event] = []
    for entrant in entrants:
        fighter = entrant.fighter
        initiative = roll(
            f"1d20{entrant.initiative_bonus:+d}", rng=rng, reason=f"Инициатива: {fighter.name}"
        )
        if fighter.kind == "pc":  # инициатива — проверка Ловкости, Удача полурослика действует
            initiative = apply_luck(state.party[fighter.id], initiative, rng=rng)
        fighter.initiative = initiative.total
        events.append(RollEvent(actor_id=fighter.id, label="Инициатива", roll=initiative))

    # sorted устойчива: при полной ничьей остаётся порядок отряда и списка монстров.
    order = sorted(
        entrants, key=lambda e: (-e.fighter.initiative, -e.dex_score, e.fighter.kind != "pc")
    )
    started = state.model_copy(deep=True)
    started.combat = Combat(
        encounter_id=encounter_id,
        # Копии: состояние не должно делить зоны и связи с данными приключения.
        zones=[zone.model_copy() for zone in zones],
        links=[link.model_copy() for link in links],
        combatants={e.fighter.id: e.fighter for e in entrants},
        order=[e.fighter.id for e in order],
    )
    # Новая сцена: вдохновение за прежнюю сцену не переносится.
    started.scene = Scene(type=SceneType.COMBAT, started_turn=state.turn)
    events += [SceneEvent(scene=SceneType.COMBAT), CombatEvent(phase="started")]
    events += _begin_turn(started, started.combat.order[0], rng=rng)  # первый ход — как любой
    return started, events


def current_actor(state: GameState) -> Combatant:
    combat = _active_combat(state)
    return combat.combatants[combat.order[combat.turn_index]]


# --- дистанция ---


def _link_distance(combat: Combat, zone_a: str, zone_b: str) -> Literal["near", "far"] | None:
    """Расстояние по связи между зонами (связь без направления) или None, если связи нет."""
    for link in combat.links:
        if {link.a, link.b} == {zone_a, zone_b}:
            return link.distance
    return None


def distance(combat: Combat, a_id: str, b_id: str) -> Distance:
    """«Вовлечены» — в одной зоне и записаны в `engaged_with` друг у друга; иначе в одной
    зоне «рядом». Для разных зон расстояние задаёт связь, без связи — «далеко»."""
    a, b = _combatant(combat, a_id), _combatant(combat, b_id)
    if a.zone_id == b.zone_id:
        return "engaged" if b.id in a.engaged_with and a.id in b.engaged_with else "near"
    return _link_distance(combat, a.zone_id, b.zone_id) or "far"


# --- перемещение ---


def _check_route(combat: Combat, actor: Combatant, to_zone: str) -> None:
    _require(any(zone.id == to_zone for zone in combat.zones), f"неизвестная зона {to_zone!r}")
    _require(to_zone != actor.zone_id, f"{actor.name} уже в этой зоне")
    _require(
        _link_distance(combat, actor.zone_id, to_zone) == "near",
        f"из зоны {actor.zone_id!r} в {to_zone!r} за одно перемещение не добраться",
    )


def _check_engage(state: GameState, actor: Combatant, target_id: str, zone_id: str) -> None:
    target = _combatant(_active_combat(state), target_id)
    _require(target.side != actor.side, "вступить в ближний бой можно только с противником")
    _require(not _is_dead(_entity(state, target_id)), f"{target.name} мёртв")
    _require(target.zone_id == zone_id, f"{target.name} не в зоне {zone_id!r}")
    _require(target_id not in actor.engaged_with, f"{actor.name} уже в ближнем бою с {target.name}")


def _check_move_budget(actor: Combatant, dash: bool) -> None:
    """Одно перемещение за ход, Рывок (действие) даёт второе; вступление в бой — перемещение."""
    flags = actor.turn_flags
    dashing = flags.get("dashed", False)
    if dash and not dashing and flags.get("action", False):
        raise CombatError(f"{actor.name}: действие уже потрачено, Рывок невозможен")
    allowed = 2 if dash or dashing else 1
    made = flags.get("moved", False) + flags.get("moved_again", False)
    if made < allowed:
        return
    if allowed == 1:
        raise CombatError(
            f"{actor.name} уже перемещался в этот ход; второе перемещение возможно только с Рывком"
        )
    raise CombatError(f"{actor.name} уже использовал все перемещения этого хода")


def _opportunity_strike(
    state: GameState, attacker_id: str, content: SrdContent
) -> dict[str, str] | None:
    """Чем боец бьёт по возможности: аргумент `weapon` или `action_id` для `attack`.

    Персонаж — первым надетым рукопашным оружием, монстр — первым рукопашным действием-атакой.
    Нет подходящего — None: безоружный персонаж атаки по возможности не делает."""
    attacker = _entity(state, attacker_id)
    if isinstance(attacker, Character):
        for entry in attacker.inventory:
            item = content.items.get(entry.item_id)
            if entry.equipped and item and item.weapon and item.weapon.range == "melee":
                return {"weapon": entry.item_id}
        return None
    for action in _monster_def(attacker, content).actions:
        if action.kind == "attack" and action.range == "melee":
            return {"action_id": action.id}
    return None


def _can_react(state: GameState, fighter_id: str) -> bool:
    entity = _entity(state, fighter_id)
    reacted = _combatant(_active_combat(state), fighter_id).turn_flags.get("reaction", False)
    return _is_standing(entity) and not is_incapacitated(entity.conditions) and not reacted


def _provoke_opportunity_attacks(
    state: GameState, mover_id: str, attacker_ids: list[str], *, rng: Rng, content: SrdContent
) -> tuple[GameState, list[Event]]:
    """Атаки по возможности по бойцу, который уходит из ближнего боя без Отхода.

    Каждый из `attacker_ids` бьёт реакцией (`_attack(kind="opportunity")`), если может
    реагировать и достаёт рукопашной; остальные пропускаются без ошибки. Вызывается, пока
    связь `engaged_with` ещё цела: по ней проверяется, что враг достаёт рукопашной.
    Если боец потерял сознание, остальные не бьют: он уже не уйдёт.
    """
    events: list[Event] = []
    for attacker_id in attacker_ids:
        if not _is_standing(_entity(state, mover_id)):
            break
        strike = _opportunity_strike(state, attacker_id, content)
        if strike is None or not _can_react(state, attacker_id):
            continue
        state, new_events = _attack(
            state, attacker_id, mover_id, kind="opportunity", rng=rng, content=content, **strike
        )
        events += new_events
    return state, events


def _apply_move(
    combat: Combat, actor_id: str, to_zone: str | None, engage: str | None, dash: bool
) -> None:
    """Применяет проверенное перемещение к бою (передаётся копия, а не исходный бой)."""
    actor = combat.combatants[actor_id]
    if dash and not actor.turn_flags.get("dashed", False):
        actor.turn_flags.update(action=True, dashed=True)
    actor.turn_flags["moved_again" if actor.turn_flags.get("moved") else "moved"] = True
    if to_zone is not None:
        for enemy_id in actor.engaged_with:
            combat.combatants[enemy_id].engaged_with.remove(actor_id)
        actor.engaged_with = []
        actor.zone_id = to_zone
    if engage is not None:
        actor.engaged_with.append(engage)
        combat.combatants[engage].engaged_with.append(actor_id)


def move(
    state: GameState,
    actor_id: str,
    *,
    to_zone: str | None = None,
    engage: str | None = None,
    dash: bool = False,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Перемещение бойца, чей сейчас ход: в связанную зону `near` и/или в ближний бой.

    За ход — одно перемещение, с `dash` (тратит действие) — два. `engage` — id противника
    из зоны, где боец окажется. Выход из ближнего боя без `disengaged` провоцирует атаки
    по возможности от всех, с кем боец был вовлечён; после перемещения связь снимается.
    Боец на 0 HP или с состоянием «скорость 0» (схвачен, опутан, парализован...) не двигается.
    """
    combat = _active_combat(state)
    actor = _combatant(combat, actor_id)
    _require(current_actor(state).id == actor_id, f"сейчас не ход бойца {actor.name}")
    _require(to_zone is not None or engage is not None, "нужно указать зону или цель ближнего боя")
    mover = _entity(state, actor_id)
    _require(_is_standing(mover), f"{actor.name} на 0 HP и не может двигаться")
    _require(not cannot_move(mover.conditions), f"{actor.name}: состояние не даёт двигаться")
    _require(
        not actor.turn_flags.get("steady_aim", False),
        f"{actor.name}: после «Точного прицеливания» скорость 0 до конца хода",
    )
    if to_zone is not None:
        _check_route(combat, actor, to_zone)
    if engage is not None:
        _check_engage(state, actor, engage, to_zone or actor.zone_id)
    _check_move_budget(actor, dash)

    leaving = to_zone is not None and not actor.disengaged
    provokers = list(actor.engaged_with) if leaving else []
    state, events = _provoke_opportunity_attacks(
        state, actor_id, provokers, rng=rng, content=content
    )
    if not _is_standing(_entity(state, actor_id)):
        return state, events  # атака по возможности свалила бойца: он остаётся на месте

    moved = state.model_copy(deep=True)
    _apply_move(_active_combat(moved), actor_id, to_zone, engage, dash)
    return moved, events


# --- атака ---


@dataclass(frozen=True)
class _Strike:
    """Атака, готовая к броску: оружие персонажа или действие монстра уже разобраны."""

    name: str
    bonus: int
    damage: str  # запись костей основного урона
    damage_type: str
    range: Literal["melee", "near", "far"]
    ability: Ability | None = None  # характеристика атаки персонажа; у монстров None
    is_weapon: bool = False  # оружие персонажа (не подручный предмет и не удар монстра)
    finesse: bool = False
    reach: bool = False
    extra: tuple[tuple[str, str], ...] = ()  # (кости, тип) урона второго типа при том же ударе
    half_on_miss: bool = False  # «Мощный заговор»: промах заговора наносит половину урона
    # Эффект попадания: метка заклинания или состояние от удара монстра. Если задан
    # `condition_save`, цель сначала делает спасбросок (характеристика, Сл) и при успехе свободна.
    condition: ActiveCondition | None = None
    condition_save: tuple[Ability, int] | None = None


@dataclass(frozen=True)
class _Part:
    """Одна часть урона удара: свои кости и свой тип, бросается отдельным броском."""

    label: str
    dice: str
    damage_type: str
    roll_twice: bool = False  # «Свирепый атакующий»: два броска, берётся лучший


def _character_strike(c: Character, weapon_id: str, dist: Distance, content: SrdContent) -> _Strike:
    if weapon_id == IMPROVISED_WEAPON_ID:
        profile = improvised_attack(c)
        # Брошенный предмет — атака на расстоянии «рядом»; вплотную им бьют как в рукопашной.
        reach = "melee" if dist == "engaged" else "near"
        return _Strike(
            profile.name,
            profile.bonus,
            profile.damage,
            profile.damage_type,
            reach,
            ability=profile.ability,
        )
    try:
        profile = weapon_attack(c, weapon_id, content)
    except ValueError as exc:
        raise CombatError(str(exc)) from None
    properties = content.items[weapon_id].weapon.properties
    return _Strike(
        profile.name,
        profile.bonus,
        profile.damage,
        profile.damage_type,
        profile.range,
        ability=profile.ability,
        is_weapon=True,
        finesse="finesse" in properties,
        reach="reach" in properties,
    )


def _monster_strike(combatant: Combatant, action_id: str, content: SrdContent) -> _Strike:
    monster = _monster_def(combatant, content)
    action = next((a for a in monster.actions if a.id == action_id), None)
    _require(action is not None, f"у {monster.name} нет действия {action_id!r}")
    _require(
        action.kind == "attack",
        f"действие {action.name!r} не атака, а {action.kind}: оно разыгрывается иначе",
    )
    extra = ()
    if action.secondary_damage is not None:
        extra = ((action.secondary_damage, action.secondary_damage_type),)
    condition, condition_save = None, None
    if action.condition is not None:
        with_save = action.save is not None and action.dc is not None
        condition = ActiveCondition(
            id=action.condition,
            source=combatant.id,
            rounds_left=ON_HIT_SAVE_CONDITION_ROUNDS if with_save else None,
        )
        condition_save = (action.save, action.dc) if with_save else None
    return _Strike(
        action.name,
        action.attack_bonus,
        action.damage,
        action.damage_type,
        action.range,
        reach=action.reach,
        extra=extra,
        condition=condition,
        condition_save=condition_save,
    )


def _choose_strike(
    entity: Entity,
    weapon: str | None,
    action_id: str | None,
    dist: Distance,
    content: SrdContent,
) -> _Strike:
    _require(
        (weapon is None) != (action_id is None),
        "укажите либо weapon (оружие персонажа), либо action_id (действие монстра)",
    )
    if isinstance(entity, Character):
        _require(weapon is not None, "персонаж атакует оружием (weapon), а не действием монстра")
        return _character_strike(entity, weapon, dist, content)
    _require(action_id is not None, "монстр атакует действием (action_id), а не оружием")
    return _monster_strike(entity, action_id, content)


def _can_act(entity: Entity) -> bool:
    return _is_standing(entity) and not is_incapacitated(entity.conditions)


def _check_can_act(state: GameState, slot: Combatant, *, own_turn: bool = True) -> Entity:
    """Боец на ногах, дееспособен и (если нужно) сейчас его ход; возвращает его сущность."""
    entity = _entity(state, slot.id)
    _require(_is_standing(entity), f"{slot.name} на 0 HP и не может действовать")
    _require(_can_act(entity), f"{slot.name} не может действовать: он недееспособен")
    if own_turn:
        _require(current_actor(state).id == slot.id, f"сейчас не ход бойца {slot.name}")
    return entity


def _require_free(slot: Combatant, flag: Literal["action", "bonus_action"]) -> None:
    what = "действие" if flag == "action" else "бонусное действие"
    _require(not slot.turn_flags.get(flag, False), f"{slot.name}: {what} уже потрачено")


def _check_can_attack(state: GameState, slot: Combatant, kind: AttackKind) -> None:
    _check_can_act(state, slot, own_turn=kind == "action")
    if kind == "action":
        _require_free(slot, "action")
        return
    _require(not slot.turn_flags.get("reaction", False), f"{slot.name}: реакция уже потрачена")
    if kind == "readied":
        _require(
            slot.turn_flags.get("readied", False),
            f"{slot.name}: реакцией атакует только тот, кто подготовил действие («Подготовка»)",
        )


def _foe_next_to(s: GameState, slot: Combatant) -> bool:
    """Вплотную к бойцу стоит противник, способный действовать (не лежит, не недееспособен)."""
    return any(_can_act(_entity(s, foe_id)) for foe_id in slot.engaged_with)


def _range_mode(
    s: GameState, slot: Combatant, target: Combatant, strike: _Strike, dist: Distance
) -> Advantage:
    """Достаёт ли удар до цели; дальнобойная атака идёт с помехой, если рядом с атакующим
    есть дееспособный противник (SRD 5.2.1) — всё равно, в него стреляют или в другого."""
    if strike.range == "melee":
        in_reach = dist == "engaged" or (strike.reach and slot.zone_id == target.zone_id)
        _require(
            in_reach,
            f"{slot.name}: рукопашной атакой можно бить только вовлечённого противника,"
            f" а {target.name} не вовлечён",
        )
        return Advantage.NONE
    _require(strike.range == "far" or dist != "far", f"{target.name} слишком далеко для этой атаки")
    return Advantage.DISADVANTAGE if _foe_next_to(s, slot) else Advantage.NONE


def _attack_advantage(
    s: GameState,
    actor_id: str,
    target_id: str,
    strike: _Strike,
    requested: Advantage,
    range_mode: Advantage,
    *,
    close: bool,
) -> Advantage:
    """Все источники преимущества и помехи на бросок атаки, сведённые в один режим."""
    actor, target = _entity(s, actor_id), _entity(s, target_id)
    actor_slot = _combatant(_active_combat(s), actor_id)
    target_slot = _combatant(_active_combat(s), target_id)
    modes = [
        requested,
        range_mode,
        *attack_modes(actor.conditions, target_id),
        *defense_modes(target.conditions, attacker_close=close),
    ]
    if strike.ability == Ability.STR and actor_slot.turn_flags.get("reckless", False):
        modes.append(Advantage.ADVANTAGE)
    if target_slot.turn_flags.get("reckless", False):
        modes.append(Advantage.ADVANTAGE)
    if target_slot.dodging and not (
        is_incapacitated(target.conditions) or cannot_move(target.conditions)
    ):
        modes.append(Advantage.DISADVANTAGE)
    if actor_slot.turn_flags.get("steady_aim", False):
        modes.append(Advantage.ADVANTAGE)  # «Точное прицеливание»: первая атака этого хода
    if _advantage_mark(s, actor_id, target_id) is not None:
        modes.append(Advantage.ADVANTAGE)
    return combine_advantage(*modes)


def _advantage_mark(s: GameState, actor_id: str, target_id: str) -> ActiveCondition | None:
    """Метка на цели, дающая этому атакующему преимущество на первый бросок атаки по ней.

    «Направленный снаряд» работает для любого атакующего, «Помощь» — для союзников помощника."""
    combat = _active_combat(s)
    side = combat.combatants[actor_id].side
    for mark in _entity(s, target_id).conditions:
        helper = combat.combatants.get(mark.source)
        if mark.id == GUIDING_BOLT or (
            mark.id == HELPED and helper is not None and helper.side == side
        ):
            return mark
    return None


def _ally_next_to(s: GameState, actor_id: str, target_id: str) -> bool:
    """Есть ли у цели вовлечённый с ней союзник атакующего, способный действовать."""
    combat = _active_combat(s)
    side = combat.combatants[actor_id].side
    return any(
        other_id != actor_id
        and combat.combatants[other_id].side == side
        and _can_act(_entity(s, other_id))
        for other_id in combat.combatants[target_id].engaged_with
    )


def _character_extras(
    s: GameState, actor: Character, target_id: str, strike: _Strike, mode: Advantage, content
) -> list[_Part]:
    """Добавки урона персонажа: скрытая атака, убийца колоссов, бешенство, метка охотника.

    Добавки «раз за ход» отмечаются в `turn_flags` сразу: вызывается только после попадания."""
    flags = _combatant(_active_combat(s), actor.id).turn_flags
    target = _entity(s, target_id)
    extras: list[_Part] = []

    def feature(feature_id: str, flag: str) -> bool:
        return _has_feature(actor, feature_id, content) and not flags.get(flag, False)

    sneaky_weapon = strike.is_weapon and (strike.finesse or strike.range != "melee")
    if (
        sneaky_weapon
        and feature(SNEAK_ATTACK, "sneak_attack_used")
        and mode != Advantage.DISADVANTAGE
        and (mode == Advantage.ADVANTAGE or _ally_next_to(s, actor.id, target_id))
    ):
        flags["sneak_attack_used"] = True
        extras.append(_Part("Скрытая атака", f"{(actor.level + 1) // 2}d6", strike.damage_type))
    if (
        strike.is_weapon
        and feature(COLOSSUS_SLAYER, "colossus_slayer_used")
        and target.hp < target.hp_max
    ):
        flags["colossus_slayer_used"] = True
        extras.append(_Part("Убийца колоссов", "1d8", strike.damage_type))
    if (
        strike.ability == Ability.STR
        and _is_raging(actor)
        and flags.get("reckless", False)
        and feature(FRENZY, "frenzy_used")
    ):
        flags["frenzy_used"] = True
        extras.append(_Part("Бешенство", f"{RAGE_DAMAGE_BONUS}d6", strike.damage_type))
    if _mark(target, HUNTERS_MARK, actor.id) is not None:
        extras.append(_Part("Метка охотника", *HUNTERS_MARK_DAMAGE))
    return extras


def _damage_parts(
    s: GameState, actor_id: str, target_id: str, strike: _Strike, mode: Advantage, content
) -> list[_Part]:
    """Части урона удара в порядке бросков: основная, добавки персонажа, второй тип монстра."""
    actor = _entity(s, actor_id)
    dice = strike.damage
    if strike.ability == Ability.STR and _is_raging(actor):
        dice += f"+{RAGE_DAMAGE_BONUS}"
    parts = [_Part(f"Урон: {strike.name}", dice, strike.damage_type)]
    if isinstance(actor, Character):
        flags = _combatant(_active_combat(s), actor_id).turn_flags
        if (
            strike.is_weapon
            and _has_feat(actor, SAVAGE_ATTACKER_FEAT, content)
            and not flags.get("savage_attacker_used", False)
        ):
            flags["savage_attacker_used"] = True
            parts[0] = _Part(parts[0].label, dice, strike.damage_type, roll_twice=True)
        parts += _character_extras(s, actor, target_id, strike, mode, content)
    parts += [
        _Part(f"Урон: {strike.name} ({damage_type})", dice_text, damage_type)
        for dice_text, damage_type in strike.extra
    ]
    return parts


def _doubled(expr: str) -> str:
    """Запись урона для критического попадания: кости удваиваются, плоский бонус остаётся."""
    parsed = parse(expr)
    if not parsed.terms:
        return expr
    dice = "".join(
        f"{'-' if term.sign < 0 else '+'}{2 * term.count}d{term.sides}"
        + (f"kh{term.keep_highest}" if term.keep_highest else "")
        for term in parsed.terms
    )
    flat = f"{parsed.modifier:+d}" if parsed.modifier else ""
    return (dice + flat).removeprefix("+")


def _roll_part(part: _Part, crit: bool, actor_id: str, rng: Rng) -> tuple[int, list[Event]]:
    """Бросок части урона (при критическом попадании кости вдвое): сумма и события бросков."""
    label = f"{part.label} (крит)" if crit else part.label
    expr = _doubled(part.dice) if crit else part.dice
    attempts = [roll(expr, rng=rng, reason=label) for _ in range(2 if part.roll_twice else 1)]
    events: list[Event] = [RollEvent(actor_id=actor_id, label=label, roll=r) for r in attempts]
    return max(0, max(r.total for r in attempts)), events


def _spend_attack_boosts(s: GameState, actor_id: str, target_id: str) -> list[Event]:
    """Что расходует бросок атаки: скрытность атакующего, «Точное прицеливание» и метка на цели
    («Направленный снаряд», «Помощь») — каждый бросок атаки тратит их независимо от исхода."""
    events = _reveal(s, actor_id)
    _combatant(_active_combat(s), actor_id).turn_flags.pop("steady_aim", None)
    mark = _advantage_mark(s, actor_id, target_id)
    if mark is not None:
        _entity(s, target_id).conditions.remove(mark)
        events.append(ConditionEvent(target_id=target_id, condition=mark.id, added=False))
    return events


def _half_damage_on_miss(
    s: GameState, actor_id: str, target_id: str, strike: _Strike, *, rng: Rng, content: SrdContent
) -> list[Event]:
    """«Мощный заговор»: промах заговора всё равно наносит половину урона (вниз), без эффектов."""
    part = _Part(f"Урон (половина при промахе): {strike.name}", strike.damage, strike.damage_type)
    amount, events = _roll_part(part, False, actor_id, rng)
    if amount // 2 > 0:
        packet = [(amount // 2, strike.damage_type)]
        events += _deal_damage(s, target_id, packet, crit=False, rng=rng, content=content)
    return events


def _apply_on_hit(
    s: GameState, target_id: str, strike: _Strike, *, rng: Rng, content: SrdContent
) -> list[Event]:
    """Эффект попадания (метка заклинания, состояние от удара монстра): сразу или после
    спасброска цели. На убитую цель не накладывается."""
    effect, target = strike.condition, _entity(s, target_id)
    if effect is None or _is_dead(target):
        return []
    events: list[Event] = []
    if strike.condition_save is not None:
        ability, dc = strike.condition_save
        label = f"Спасбросок: {effect.id}"
        result = _entity_save(target, ability, dc=dc, rng=rng, content=content, reason=label)
        events.append(_save_event(result, label))
        if result.success:
            return events
    return events + _add_condition(s, target, effect.model_copy(), content)


def _resolve_attack(
    s: GameState,
    actor_id: str,
    target_id: str,
    strike: _Strike,
    requested: Advantage,
    range_mode: Advantage,
    *,
    kind: AttackKind,
    rng: Rng,
    content: SrdContent,
) -> list[Event]:
    """Бросок атаки против КД и, при попадании, урон (меняет переданную копию `s`)."""
    actor, target = _entity(s, actor_id), _entity(s, target_id)
    close = distance(_active_combat(s), actor_id, target_id) == "engaged"
    mode = _attack_advantage(s, actor_id, target_id, strike, requested, range_mode, close=close)
    d20 = roll(f"1d20{strike.bonus:+d}", rng=rng, advantage=mode, reason=f"Атака: {strike.name}")
    if isinstance(actor, Character):
        # Удача и Благословение действуют на любой бросок атаки персонажа, как и на спасбросок.
        d20 = apply_bless(actor, apply_luck(actor, d20, rng=rng), rng=rng)
    champion = (
        strike.is_weapon
        and isinstance(actor, Character)
        and _has_feature(actor, IMPROVED_CRITICAL, content)
    )
    crit_from = IMPROVED_CRIT_ROLL if champion else CRIT_ROLL
    target_ac = _armor_class(target, content)
    natural_crit = d20.natural >= crit_from
    hit = natural_crit or (d20.natural != 1 and d20.total >= target_ac)
    crit = hit and (natural_crit or auto_crit(target.conditions, attacker_close=close))

    events: list[Event] = [
        RollEvent(
            actor_id=actor_id,
            label=f"{_ATTACK_LABELS[kind]}: {strike.name}",
            roll=d20,
            target_ac=target_ac,
            success=hit,
        )
    ]
    events += _spend_attack_boosts(s, actor_id, target_id)
    if not hit:
        if strike.half_on_miss:
            events += _half_damage_on_miss(s, actor_id, target_id, strike, rng=rng, content=content)
        return events

    damage: list[tuple[int, str]] = []
    for part in _damage_parts(s, actor_id, target_id, strike, mode, content):
        amount, part_events = _roll_part(part, crit, actor_id, rng)
        damage.append((amount, part.damage_type))
        events += part_events
    events += _deal_damage(s, target_id, damage, crit=crit, rng=rng, content=content)
    return events + _apply_on_hit(s, target_id, strike, rng=rng, content=content)


def _check_target(state: GameState, slot: Combatant, target_slot: Combatant) -> None:
    """Цель атаки — живой противник."""
    _require(
        slot.side != target_slot.side, f"{slot.name} не может атаковать союзника {target_slot.name}"
    )
    _require(not _is_dead(_entity(state, target_slot.id)), f"{target_slot.name} уже мёртв")


def _prepare_strike(
    state: GameState,
    actor_id: str,
    target_id: str,
    weapon: str | None,
    action_id: str | None,
    content: SrdContent,
) -> tuple[_Strike, Advantage]:
    """Удар, готовый к броску, и помеха дальнобойной атаке в ближнем бою. Бросков не делает."""
    combat = _active_combat(state)
    dist = distance(combat, actor_id, target_id)
    strike = _choose_strike(_entity(state, actor_id), weapon, action_id, dist, content)
    range_mode = _range_mode(
        state, combat.combatants[actor_id], combat.combatants[target_id], strike, dist
    )
    return strike, range_mode


def _attack(
    state: GameState,
    actor_id: str,
    target_id: str,
    *,
    weapon: str | None = None,
    action_id: str | None = None,
    advantage: Advantage = Advantage.NONE,
    kind: AttackKind,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Атака бойца с проверками и тратой того, чем она оплачена (`kind`): действием своего
    хода или реакцией. Реакция расходует и подготовку: после неё подготовленное действие уже
    не сработает. Все проверки идут до первого броска."""
    combat = _active_combat(state)
    slot = _combatant(combat, actor_id)
    target_slot = _combatant(combat, target_id)
    _check_target(state, slot, target_slot)
    _check_can_attack(state, slot, kind)
    strike, range_mode = _prepare_strike(state, actor_id, target_id, weapon, action_id, content)

    result = state.model_copy(deep=True)
    spent = _combatant(_active_combat(result), actor_id).turn_flags
    if kind == "action":
        spent["action"] = True
    else:
        spent["reaction"] = True
        spent.pop("readied", None)
    events = _resolve_attack(
        result,
        actor_id,
        target_id,
        strike,
        Advantage(advantage),
        range_mode,
        kind=kind,
        rng=rng,
        content=content,
    )
    return result, events


def attack(
    state: GameState,
    actor_id: str,
    target_id: str,
    *,
    weapon: str | None = None,
    action_id: str | None = None,
    advantage: Advantage = Advantage.NONE,
    reaction: bool = False,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Атака бойца по противнику: бросок против КД, урон, добавки, сопротивления.

    Персонаж бьёт оружием: `weapon` — id предмета из его инвентаря или `"improvised"`
    (подручный предмет: 1к4 дробящего, вплотную или броском «рядом»). Монстр бьёт действием
    `action_id` из статблока. Атака тратит действие своего хода. `reaction=True` — подготовленная
    атака: боец выбрал «Подготовку», условие наступило, и он бьёт реакцией вне своего хода;
    без подготовки или с потраченной реакцией — `CombatError`. Атаки по возможности при уходе
    из ближнего боя разыгрывает `move`. Рукопашная достаёт только вовлечённого противника
    (при досягаемости — и любого в своей зоне). Дальнобойная идёт с помехой, пока рядом с
    атакующим стоит способный действовать противник (не обязательно цель). `advantage` —
    преимущество или помеха от ситуации, которую движок не знает. Все проверки идут до
    первого броска: отказ (`CombatError`) не тратит кубы.
    """
    return _attack(
        state,
        actor_id,
        target_id,
        weapon=weapon,
        action_id=action_id,
        advantage=advantage,
        kind="readied" if reaction else "action",
        rng=rng,
        content=content,
    )


# --- урон ---


def _defenses(target: Entity, content: SrdContent) -> tuple[set[str], set[str], set[str]]:
    """Типы урона, к которым у цели сопротивление, иммунитет и уязвимость."""
    if isinstance(target, Character):
        resistances = set(RAGE_RESISTANCES) if _is_raging(target) else set()
        if _has_trait(target, DWARVEN_RESILIENCE_TRAIT, content):
            resistances.add("poison")
        return resistances, set(), set()
    monster = content.monsters.get(target.ref)  # кастомный монстр вне данных SRD защит не имеет
    if monster is None:
        return set(), set(), set()
    return set(monster.resistances), set(monster.immunities), set(monster.vulnerabilities)


def _adjusted_damage(
    target: Entity, packets: Sequence[tuple[int, str]], content: SrdContent
) -> dict[str, int]:
    """Урон по типам после защит цели: сначала складывается один тип, затем иммунитет,
    сопротивление (вдвое меньше, вниз) и уязвимость (вдвое больше) — в этом порядке по SRD."""
    by_type: dict[str, int] = {}
    for amount, damage_type in packets:
        by_type[damage_type] = by_type.get(damage_type, 0) + amount
    resistances, immunities, vulnerabilities = _defenses(target, content)
    resists_everything = resists_all_damage(target.conditions)
    adjusted = {}
    for damage_type, amount in by_type.items():
        if damage_type in immunities:
            amount = 0
        else:
            if resists_everything or damage_type in resistances:
                amount //= 2
            if damage_type in vulnerabilities:
                amount *= 2
        adjusted[damage_type] = amount
    return adjusted


def _clear_engagements(combat: Combat, fighter_id: str) -> None:
    """Убитый монстр ни с кем не вовлечён: по связи нельзя ни ударить его, ни им."""
    for other_id in combat.combatants[fighter_id].engaged_with:
        combat.combatants[other_id].engaged_with.remove(fighter_id)
    combat.combatants[fighter_id].engaged_with = []


def _save_event(result: CheckResult, label: str) -> RollEvent:
    return RollEvent(
        actor_id=result.actor_id,
        label=label,
        roll=result.roll,
        dc=result.dc,
        success=result.success,
    )


def _entity_save(
    entity: Entity,
    ability: Ability,
    *,
    dc: int,
    advantage: Advantage = Advantage.NONE,
    rng: Rng,
    content: SrdContent,
    reason: str,
) -> CheckResult:
    """Спасбросок персонажа или монстра против Сл, которую посчитал движок."""
    if isinstance(entity, Character):
        return engine_saving_throw(
            entity, ability, dc=dc, advantage=advantage, rng=rng, reason=reason
        )
    return monster_saving_throw(
        _monster_def(entity, content),
        entity.id,
        entity.conditions,
        ability,
        dc=dc,
        advantage=advantage,
        rng=rng,
        reason=reason,
    )


def _immune_to_condition(entity: Entity, condition_id: str, content: SrdContent) -> bool:
    """Монстр с иммунитетом к состоянию (в `immunities` вместе с типами урона) его не получает."""
    if isinstance(entity, Character):
        return False
    monster = content.monsters.get(entity.ref)
    return monster is not None and condition_id in monster.immunities


def _add_condition(
    s: GameState, holder: Entity, condition: ActiveCondition, content: SrdContent
) -> list[Event]:
    """Накладывает состояние в переданной копии — единственное место для всех источников
    (заклинания, удары монстров, толчок и захват, эффекты мастера).

    То же состояние от того же источника обновляется, а не дублируется. Недееспособный
    заклинатель теряет концентрацию (SRD). Иммунитет монстра к состоянию — состояние не
    накладывается."""
    if _immune_to_condition(holder, condition.id, content):
        return []
    holder.conditions = [
        c for c in holder.conditions if not (c.id == condition.id and c.source == condition.source)
    ]
    holder.conditions.append(condition)
    events: list[Event] = [ConditionEvent(target_id=holder.id, condition=condition.id, added=True)]
    if (
        isinstance(holder, Character)
        and holder.concentration is not None
        and is_incapacitated(holder.conditions)
    ):
        events += _end_concentration(s, holder, content)
    return events


def _check_external_condition(
    state: GameState, condition: ActiveCondition, content: SrdContent
) -> None:
    """Состояние, которое накладывает вызывающий (решение мастера), — только из SRD, на ограниченный
    срок, без повторного спасброска и без служебного источника. Метки заклинаний, «без сознания»
    от 0 HP и скрытность ставит и снимает только сам движок."""
    _require(
        condition.id in content.conditions,
        f"{condition.id!r} — не состояние SRD, внешним решением его не наложить",
    )
    _require(
        condition.save is None and condition.dc is None,
        "у внешнего состояния нет повторного спасброска: Сл берётся только из таблицы мастера",
    )
    rounds = condition.rounds_left
    _require(
        rounds is None or 1 <= rounds <= EXTERNAL_CONDITION_MAX_ROUNDS,
        f"срок состояния — от 1 до {EXTERNAL_CONDITION_MAX_ROUNDS} раундов или без срока",
    )
    # Служебные источники движка («zero_hp», «hide») — не бойцы, поэтому сюда не проходят.
    source = condition.source
    is_fighter = source in state.party or (
        state.combat is not None and source in state.combat.combatants
    )
    _require(
        source == "" or is_fighter,
        f"источник {source!r} не боец: внешнее состояние идёт без источника или от бойца",
    )


def apply_condition(
    state: GameState, target_id: str, condition: ActiveCondition, *, content: SrdContent
) -> tuple[GameState, list[Event]]:
    """Накладывает состояние на персонажа или монстра по решению мастера.

    Те же правила наложения, что у состояний от заклинаний и ударов: недееспособный
    заклинатель теряет концентрацию, цель с иммунитетом — `CombatError`. Но вход внешний,
    поэтому он ограничен: состояние SRD, срок `rounds_left` от 1 до 100 раундов (10 минут) или
    без срока, без `save` и `dc`, источник — пустой или боец. Иначе `CombatError` без изменений."""
    _check_external_condition(state, condition, content)
    result = state.model_copy(deep=True)
    target = _entity(result, target_id)
    _require(
        not _immune_to_condition(target, condition.id, content),
        _immune_message(target, condition.id),
    )
    return result, _add_condition(result, target, condition.model_copy(), content)


def _is_engine_owned(s: GameState, record: ActiveCondition, content: SrdContent) -> bool:
    """Запись состояния ведут правила, а не мастер: служебный источник (0 HP, «Спрятаться»),
    повторный спасбросок (заклинания и состояния со Сл) или эффект концентрации заклинателя."""
    if (
        record.source in ENGINE_CONDITION_SOURCES
        or record.save is not None
        or record.dc is not None
    ):
        return True
    caster = s.party.get(record.source)
    return (
        caster is not None
        and caster.concentration is not None
        and record.id in _concentration_effect_ids(caster, content)
    )


def remove_condition(
    state: GameState, target_id: str, condition_id: str, *, content: SrdContent
) -> tuple[GameState, list[Event]]:
    """Снимает состояние с персонажа или монстра по решению мастера («сбит с ног» встал, «схвачен»
    отпущен, ошибочное бессрочное состояние убрано).

    Снимаются все записи этого состояния, кроме тех, что ведёт движок (`_is_engine_owned`):
    «без сознания» от 0 HP, скрытность, эффекты заклинаний со спасброском и концентрацией. Если
    состояния нет или снять можно только служебное — `CombatError` без изменений."""
    result = state.model_copy(deep=True)
    target = _entity(result, target_id)
    records = [c for c in target.conditions if c.id == condition_id]
    _require(bool(records), f"{target.name}: нет состояния «{condition_id}»")
    removable = [c for c in records if not _is_engine_owned(result, c, content)]
    _require(
        bool(removable),
        f"состояние «{condition_id}» у {target.name} — служебная запись движка или эффект "
        "заклинания: его снимают правила, а не мастер",
    )
    target.conditions = [
        c for c in target.conditions if c.id != condition_id or _is_engine_owned(result, c, content)
    ]
    events: list[Event] = [
        ConditionEvent(target_id=target.id, condition=condition_id, added=False) for _ in removable
    ]
    return result, events


def _immune_message(target: Entity, condition_id: str) -> str:
    return f"{target.name} невосприимчив к состоянию «{condition_id}»"


def _reveal(s: GameState, fighter_id: str) -> list[Event]:
    """Атака или заклинание прекращают скрытность, полученную действием «Спрятаться»."""
    entity = _entity(s, fighter_id)
    kept = [c for c in entity.conditions if not (c.id == INVISIBLE and c.source == HIDDEN_SOURCE)]
    if len(kept) == len(entity.conditions):
        return []
    entity.conditions = kept
    return [ConditionEvent(target_id=fighter_id, condition=INVISIBLE, added=False)]


def _holders(s: GameState) -> list[Entity]:
    """Все, на ком могут висеть эффекты: персонажи отряда и монстры боя."""
    monsters = (
        [] if s.combat is None else [c for c in s.combat.combatants.values() if c.kind == "monster"]
    )
    return [*s.party.values(), *monsters]


def _concentration_effect_ids(caster: Character, content: SrdContent) -> set[str | None]:
    """Id эффектов на концентрации заклинателя (неизвестное заклинание — его id)."""
    spell = content.spells.get(caster.concentration)
    if spell is None:
        return {caster.concentration}
    return {spell.effect.buff, spell.effect.condition}


def _end_concentration(s: GameState, caster: Character, content: SrdContent) -> list[Event]:
    """Заклинатель теряет концентрацию: эффекты его заклинания (с `source` = он) исчезают у всех."""
    effect_ids = _concentration_effect_ids(caster, content)
    caster.concentration = None
    events: list[Event] = []
    for holder in _holders(s):
        if isinstance(holder, Character):
            ended = end_buffs(holder, lambda b: b.id in effect_ids and b.source_id == caster.id)
            events += [
                ConditionEvent(target_id=holder.id, condition=b.id, added=False) for b in ended
            ]
        events += _drop_conditions(holder, effect_ids, caster.id)
    return events


def _drop_conditions(holder: Entity, effect_ids: set[str | None], source: str) -> list[Event]:
    """Убирает состояния с id из `effect_ids`, наложенные `source`."""
    kept, events = [], []
    for condition in holder.conditions:
        if condition.id in effect_ids and condition.source == source:
            events.append(ConditionEvent(target_id=holder.id, condition=condition.id, added=False))
        else:
            kept.append(condition)
    holder.conditions = kept
    return events


def _concentration_check(
    s: GameState, c: Character, damage: int, *, rng: Rng, content: SrdContent
) -> list[Event]:
    """Спасбросок Телосложения за концентрацию: Сл = max(10, урон // 2), но не выше 30; провал
    снимает концентрацию."""
    spell = content.spells.get(c.concentration)
    label = f"Концентрация: {spell.name if spell else c.concentration}"
    dc = min(CONCENTRATION_MAX_DC, max(CONCENTRATION_MIN_DC, damage // 2))
    result = engine_saving_throw(
        c, Ability.CON, dc=dc, advantage=Advantage.NONE, rng=rng, reason=label
    )
    events: list[Event] = [_save_event(result, label)]
    if not result.success:
        events += _end_concentration(s, c, content)
    return events


def _fall_unconscious(c: Character) -> list[Event]:
    """Персонаж на 0 HP без сознания (если ещё нет); спасброски от смерти начинаются с нуля."""
    c.death_saves = DeathSaves()
    if any(x.id == UNCONSCIOUS and x.source == ZERO_HP_SOURCE for x in c.conditions):
        return []
    c.conditions.append(ActiveCondition(id=UNCONSCIOUS, source=ZERO_HP_SOURCE))
    return [ConditionEvent(target_id=c.id, condition=UNCONSCIOUS, added=True)]


def _die(s: GameState, c: Character, content: SrdContent) -> list[Event]:
    events = _fall_unconscious(c)
    c.death_saves = DeathSaves(failures=DEATH_SAVE_FAILURES_TO_DIE)
    if c.concentration is not None:
        events += _end_concentration(s, c, content)
    return events


def _relentless_endurance_ready(c: Character, content: SrdContent) -> bool:
    # Нет записи о ресурсе — способность ещё не использована.
    return (
        _has_trait(c, RELENTLESS_ENDURANCE_TRAIT, content)
        and c.resources.get(RELENTLESS_ENDURANCE_TRAIT, 1) > 0
    )


def _fail_death_save_from_damage(c: Character, *, crit: bool) -> None:
    """Урон персонажу на 0 HP — провал спасброска от смерти (два при критическом попадании)."""
    saves = c.death_saves
    saves.stable = False
    saves.failures = min(DEATH_SAVE_FAILURES_TO_DIE, saves.failures + (2 if crit else 1))


def _character_after_damage(
    s: GameState,
    c: Character,
    hp_before: int,
    hp_lost: int,
    total: int,
    hits: list[DamageEvent],
    *,
    crit: bool,
    rng: Rng,
    content: SrdContent,
) -> list[Event]:
    """Что происходит с персонажем после урона: смерть, падение, провалы спасбросков, концентрация.

    `hp_lost` — урон, дошедший до хитов (после временных); `total` — весь урон после защит.
    Порядок: массивный урон, урон на 0 HP, падение до 0 (его отменяет Неутомимость орка) —
    и общий хвост: кто остался на ногах (хитов хватило, урон поглотили временные HP или
    Неутомимость), тот при концентрации делает спасбросок на весь полученный урон."""
    if hp_lost > 0:
        overflow = hp_lost - hp_before  # остаток урона после падения до 0 (или весь, если уже на 0)
        if overflow >= c.hp_max:
            return _die(s, c, content)  # массивный урон
        if hp_before == 0:
            _fail_death_save_from_damage(c, crit=crit)
            return []
        if c.hp == 0:
            if not _relentless_endurance_ready(c, content):
                events = _fall_unconscious(c)
                if c.concentration is not None:
                    events += _end_concentration(s, c, content)  # лежащий не держит концентрацию
                return events
            c.hp = 1  # Неутомимость: орк остаётся на ногах и идёт в общий хвост ниже
            c.resources[RELENTLESS_ENDURANCE_TRAIT] = 0
            for hit in hits:  # ни одно событие урона не должно показывать 0 HP
                hit.hp_after = max(hit.hp_after, 1)
    if total > 0 and c.hp > 0 and c.concentration is not None:
        return _concentration_check(s, c, total, rng=rng, content=content)
    return []


def _deal_damage(
    s: GameState,
    target_id: str,
    packets: Sequence[tuple[int, str]],
    *,
    crit: bool,
    rng: Rng,
    content: SrdContent,
) -> list[Event]:
    """Применяет урон к цели в переданной копии `s`; `packets` — пары (урон, тип) одного удара.

    Защиты применяются к каждому типу отдельно, затем весь урон снимает временные HP и хиты;
    падение до 0, смерть и концентрация считаются один раз на весь удар."""
    target = _entity(s, target_id)
    _require(not _is_dead(target), f"{target.name} уже мёртв")
    adjusted = _adjusted_damage(target, packets, content)
    hp_before = hp = target.hp
    temp_hp = getattr(target, "temp_hp", 0)
    hits: list[DamageEvent] = []
    for damage_type, amount in adjusted.items():
        absorbed = min(temp_hp, amount)
        temp_hp -= absorbed
        hp = max(0, hp - (amount - absorbed))
        hits.append(
            DamageEvent(target_id=target_id, amount=amount, damage_type=damage_type, hp_after=hp)
        )
    hp_lost = sum(adjusted.values()) - (getattr(target, "temp_hp", 0) - temp_hp)
    target.hp = hp
    events: list[Event] = []
    if isinstance(target, Character):
        target.temp_hp = temp_hp
        events = _character_after_damage(
            s,
            target,
            hp_before,
            hp_lost,
            sum(adjusted.values()),
            hits,
            crit=crit,
            rng=rng,
            content=content,
        )
    elif hp == 0:
        _clear_engagements(_active_combat(s), target_id)
    return [*hits, *events]


def apply_damage(
    state: GameState,
    target_id: str,
    amount: int,
    damage_type: str,
    *,
    rng: Rng,
    content: SrdContent,
    crit: bool = False,
) -> tuple[GameState, list[Event]]:
    """Урон одного типа цели: боец боя или персонаж отряда (вне боя тоже, например ловушка).

    Применяет сопротивления, иммунитеты и уязвимости цели к этому типу, снимает временные HP,
    затем хиты. Монстр на 0 HP умирает; персонаж теряет сознание и начинает спасброски от
    смерти (урон на 0 HP — провал, `crit` — два), массивный урон убивает сразу, орк может
    остаться с 1 HP, концентрирующийся делает спасбросок Телосложения. Мёртвая цель — ошибка.
    """
    _require(amount >= 0, "урон не может быть отрицательным")
    result = state.model_copy(deep=True)
    events = _deal_damage(
        result, target_id, [(amount, damage_type)], crit=crit, rng=rng, content=content
    )
    return result, events


def _apply_heal(s: GameState, target_id: str, amount: int) -> list[Event]:
    """Лечение цели в переданной копии (до максимума); персонаж на 0 HP приходит в сознание."""
    target = _entity(s, target_id)
    _require(not _is_dead(target), f"{target.name} мёртв: лечение мёртвых не возвращает")
    hp_before = target.hp
    target.hp = min(target.hp_max, hp_before + amount)
    gained = target.hp - hp_before
    events: list[Event] = [HealEvent(target_id=target_id, amount=gained, hp_after=target.hp)]
    if isinstance(target, Character) and hp_before == 0 and gained > 0:
        events += _wake(target)
    return events


def heal_target(state: GameState, target_id: str, amount: int) -> tuple[GameState, list[Event]]:
    """Лечение до максимума HP (лишнее пропадает); персонаж на 0 HP приходит в сознание.

    Мёртвого лечение не возвращает: ошибка. Временные HP лечением не восстанавливаются."""
    _require(amount >= 0, "лечение не может быть отрицательным")
    result = state.model_copy(deep=True)
    return result, _apply_heal(result, target_id, amount)


def _wake(c: Character) -> list[Event]:
    """Персонаж получил хиты на 0 HP: снимает «без сознания» от нуля хитов."""
    was_down = any(x.id == UNCONSCIOUS and x.source == ZERO_HP_SOURCE for x in c.conditions)
    regain_consciousness(c)
    if was_down:
        return [ConditionEvent(target_id=c.id, condition=UNCONSCIOUS, added=False)]
    return []


# --- смена хода ---


def _tick_conditions(holder: Entity) -> list[Event]:
    """Уменьшает срок состояний на раунд; закончившиеся убирает."""
    kept, events = [], []
    for condition in holder.conditions:
        if condition.rounds_left is not None:
            condition.rounds_left -= 1
            if condition.rounds_left <= 0:
                events.append(
                    ConditionEvent(target_id=holder.id, condition=condition.id, added=False)
                )
                continue
        kept.append(condition)
    holder.conditions = kept
    return events


def _tick_buffs(c: Character) -> list[Event]:
    """Уменьшает срок эффектов на раунд; закончившиеся кончаются с последствиями («Подмога»)."""
    for buff in c.buffs:
        if buff.rounds_left is not None:
            buff.rounds_left -= 1
    ended = end_buffs(c, lambda b: b.rounds_left is not None and b.rounds_left <= 0)
    return [ConditionEvent(target_id=c.id, condition=b.id, added=False) for b in ended]


def _end_of_turn_effects(s: GameState, fighter_id: str, *, rng: Rng, content) -> list[Event]:
    """Конец хода бойца: повторные спасброски состояний, затем срок состояний и эффектов."""
    entity = _entity(s, fighter_id)
    if _is_dead(entity):
        return []
    events: list[Event] = []
    still_active = []
    for condition in entity.conditions:
        if condition.save is not None and condition.dc is not None:
            label = f"Повторный спасбросок: {condition.id}"
            result = _entity_save(
                entity, condition.save, dc=condition.dc, rng=rng, content=content, reason=label
            )
            events.append(_save_event(result, label))
            if result.success:
                events.append(
                    ConditionEvent(target_id=fighter_id, condition=condition.id, added=False)
                )
                continue
        still_active.append(condition)
    entity.conditions = still_active
    events += _tick_conditions(entity)
    if isinstance(entity, Character):
        events += _tick_buffs(entity)
    return events


def _death_save(c: Character, *, rng: Rng) -> list[Event]:
    """Спасбросок от смерти в начале хода персонажа на 0 HP (без модификаторов, 10+ — успех)."""
    result = roll("1d20", rng=rng, reason=f"Спасбросок от смерти: {c.name}")
    # Спасбросок от смерти — обычный спасбросок: Удача и Благословение действуют и на него.
    result = apply_bless(c, apply_luck(c, result, rng=rng), rng=rng)
    die = result.natural
    events: list[Event] = [
        RollEvent(
            actor_id=c.id,
            label="Спасбросок от смерти",
            roll=result,
            dc=DEATH_SAVE_DC,
            success=die == 20 or (die != 1 and result.total >= DEATH_SAVE_DC),
        )
    ]
    if die == 20:
        c.hp = 1
        events.append(HealEvent(target_id=c.id, amount=1, hp_after=1))
        return events + _wake(c)
    saves = c.death_saves
    if die == 1:
        saves.failures += 2
    elif result.total >= DEATH_SAVE_DC:
        saves.successes += 1
    else:
        saves.failures += 1
    if saves.failures >= DEATH_SAVE_FAILURES_TO_DIE:
        saves.failures = DEATH_SAVE_FAILURES_TO_DIE
    elif saves.successes >= 3:
        c.death_saves = DeathSaves(stable=True)  # стабилен: счётчики обнуляются
    return events


def _is_dead_monster(combat: Combat, fighter_id: str) -> bool:
    fighter = combat.combatants[fighter_id]
    return fighter.kind == "monster" and fighter.hp == 0


def _begin_turn(s: GameState, fighter_id: str, *, rng: Rng) -> list[Event]:
    """Начало хода бойца — одно для первого хода боя и для каждой смены хода: сбрасываются
    флаги хода, «Помощь» от этого бойца кончается, персонаж на 0 HP (не стабильный, не
    мёртвый) делает спасбросок от смерти. Флаги добавок «раз за ход» сбрасываются у всех
    бойцов: новый ход — новый «раз», и в чужой ход они снова доступны для реакции."""
    combat = _active_combat(s)
    fighter = _combatant(combat, fighter_id)
    fighter.turn_flags = {}
    fighter.dodging = False
    fighter.disengaged = False
    for other in combat.combatants.values():
        for flag in ONCE_PER_TURN_FLAGS:
            other.turn_flags.pop(flag, None)
    events: list[Event] = [CombatEvent(phase="turn", actor_id=fighter_id)]
    for holder in _holders(s):  # «Помощь» длится до начала следующего хода помощника
        events += _drop_conditions(holder, {HELPED}, fighter_id)
    entity = _entity(s, fighter_id)
    # Мёртвый (три провала) уже не бросает и не оживает от натуральной 20.
    if (
        isinstance(entity, Character)
        and entity.hp == 0
        and not entity.death_saves.stable
        and not is_dead(entity)
    ):
        events += _death_save(entity, rng=rng)
    return events


def _start_next_turn(s: GameState, *, rng: Rng) -> list[Event]:
    """Передаёт ход следующему живому бойцу; убитые монстры уходят из очереди."""
    combat = _active_combat(s)
    # Позиция преемника в очереди без убитых: сколько выживших стояло до конца текущего хода.
    index = sum(1 for f in combat.order[: combat.turn_index + 1] if not _is_dead_monster(combat, f))
    combat.order = [f for f in combat.order if not _is_dead_monster(combat, f)]
    count = len(combat.order)
    for _ in range(count):  # не больше одного круга: мёртвых персонажей пропускаем
        if index >= count:
            index = 0
            combat.round += 1
        if not _is_dead(_entity(s, combat.order[index])):
            break
        index += 1
    index = min(index, count - 1)
    combat.turn_index = index

    return _begin_turn(s, combat.order[index], rng=rng)


def end_turn(state: GameState, *, rng: Rng, content: SrdContent) -> tuple[GameState, list[Event]]:
    """Конец хода текущего бойца и начало хода следующего.

    У заканчивающего ход: повторные спасброски его состояний (успех снимает состояние), затем
    срок состояний и эффектов уменьшается на раунд. Ход переходит к следующему живому бойцу
    (раунд растёт после последнего), убитые монстры убираются из очереди; у нового бойца
    сбрасываются флаги хода. Персонаж на 0 HP (не стабильный, не мёртвый) в начале хода делает
    спасбросок от смерти. Лежащий персонаж ходит «вхолостую»: вызывающий снова вызывает
    `end_turn`. Если живых не осталось, ход достаётся мёртвому без бросков: победителя
    (`combat_winner`) вызывающий проверяет до `end_turn`.
    """
    combat = _active_combat(state)
    result = state.model_copy(deep=True)
    events = _end_of_turn_effects(result, combat.order[combat.turn_index], rng=rng, content=content)
    events += _start_next_turn(result, rng=rng)
    return result, events


def can_act(state: GameState, fighter_id: str) -> bool:
    """Боец способен действовать: на ногах и не недееспособен. Иначе его ход проходит впустую."""
    return _can_act(_entity(state, fighter_id))


def advance_to(
    state: GameState, fighter_id: str, *, rng: Rng, content: SrdContent
) -> tuple[GameState, list[Event]]:
    """Передаёт ход вперёд (`end_turn` столько раз, сколько нужно), пока он не окажется у бойца.

    Нужна, когда мастер разбирает за один вызов ход персонажа и идущие следом ходы монстров:
    движок разрешает действия только в ход бойца. Бойцы между текущим и нужным должны быть
    неспособны действовать (лежат, мертвы, парализованы): их ходы проходят вхолостую, со
    спасбросками от смерти и повторными спасбросками. Если среди них есть способный действовать,
    его ход нельзя перепрыгнуть — `CombatError`. Проверки идут до первого `end_turn` и повторяются
    после каждого: лежащий боец мог встать на ногах от натуральной 20 спасброска от смерти в
    начале хода, и тогда его ход уже не пропустить. Боец, чей ход уже идёт, — состояние и
    события не меняются."""
    combat = _active_combat(state)
    target = _combatant(combat, fighter_id)
    if current_actor(state).id == fighter_id:
        return state, []
    _require(can_act(state, fighter_id), f"{target.name} не может действовать: ход ему не нужен")
    order = combat.order
    for step in range(1, len(order)):
        passed = order[(combat.turn_index + step) % len(order)]
        if passed == fighter_id:
            break
        _require(
            not can_act(state, passed),
            f"сначала ходит {combat.combatants[passed].name}: его ход нельзя пропустить",
        )
    else:  # боец жив, но не в очереди: этого не бывает, пока очередь ведёт сам движок
        raise CombatError(f"{target.name} нет в очереди хода")

    events: list[Event] = []
    for _ in order:  # до цели не больше одного круга
        state, new_events = end_turn(state, rng=rng, content=content)
        events += new_events
        actor = current_actor(state)
        if actor.id == fighter_id:
            return state, events
        _require(
            not can_act(state, actor.id),
            f"сначала ходит {actor.name}: его ход нельзя пропустить",
        )
    raise CombatError(f"ход не дошёл до бойца {target.name}")


def combat_winner(state: GameState) -> Literal["party", "enemies"] | None:
    """Сторона, у которой остались бойцы на ногах (с хитами); у обеих — None.

    Парализованный или опутанный ещё в бою; персонаж на 0 HP и мёртвый — уже нет. Если
    не осталось никого ни с одной стороны, побеждает отряд."""
    combat = _active_combat(state)
    standing = {
        fighter.side
        for fighter in combat.combatants.values()
        if _is_standing(_entity(state, fighter.id))
    }
    if "enemies" not in standing:
        return "party"
    if "party" not in standing:
        return "enemies"
    return None


# --- конец сцены ---


def _end_timed_effects(s: GameState, content: SrdContent) -> list[Event]:
    """Эффекты персонажей со сроком кончаются вместе с боем: срок тикает только в `end_turn`, и
    вне боя они висели бы вечно. Сначала концентрация (она снимает свои эффекты у всех), затем
    состояния и эффекты со сроком; бессрочные остаются."""
    events: list[Event] = []
    for c in s.party.values():
        if c.concentration is not None:
            events += _end_concentration(s, c, content)
        timed = [x for x in c.conditions if x.rounds_left is not None]
        c.conditions = [x for x in c.conditions if x.rounds_left is None]
        events += [ConditionEvent(target_id=c.id, condition=x.id, added=False) for x in timed]
        ended = end_buffs(c, lambda b: b.rounds_left is not None)
        events += [ConditionEvent(target_id=c.id, condition=b.id, added=False) for b in ended]
    return events


def _mark_fallen_npcs(s: GameState, adventure: Adventure) -> None:
    """NPC со статблоком, павшие в этом бою, помечаются мёртвыми.

    Связь бойца с NPC — id статблока, и NPC должен быть там, где шёл бой. NPC считается павшим,
    когда пали все монстры его статблока: если рядом дерутся такие же безымянные монстры,
    NPC остаётся жив (ошибка в эту сторону безопаснее)."""
    fighters = s.combat.combatants.values()
    for npc in adventure.npcs.values():
        here = s.world.npc_location.get(npc.id, npc.location_id) == s.location_id
        if npc.statblock is None or not here or not s.world.npc_alive.get(npc.id, True):
            continue
        group = [f for f in fighters if f.kind == "monster" and f.ref == npc.statblock]
        if group and all(f.hp == 0 for f in group):
            s.world.npc_alive[npc.id] = False


def _close_combat(s: GameState, content: SrdContent, adventure: Adventure) -> list[Event]:
    """Завершает бой в переданной копии: эффекты со сроком, павшие NPC, метка боя, `combat=None`."""
    combat = s.combat
    winner = combat_winner(s)
    events = _end_timed_effects(s, content)
    _mark_fallen_npcs(s, adventure)
    if combat.encounter_id is not None:
        s.world.flags[f"encounter:{combat.encounter_id}"] = "done"
    s.combat = None
    return [*events, CombatEvent(phase="ended", winner=winner)]


def end_scene(
    state: GameState,
    next_type: SceneType,
    rules: RuleSet,
    *,
    content: SrdContent,
    adventure: Adventure,
) -> tuple[GameState, list[Event]]:
    """Заканчивает текущую сцену и начинает следующую (исследование, разговор, привал).

    Сначала по порядку вызываются триггеры модулей `on_scene_end`: каждый видит состояние как
    оно было в конце сцены (в том числе её тип и вид привала) и возвращает новое. Так «Привал у
    костра» на конце сцены `camp` проводит отдых по виду привала: после короткого — возвращает
    особенности «до короткого отдыха», после долгого (и привала без вида, как в DND-1) — длинный
    отдых и утро следующего дня. Если шёл бой, он завершается: эффекты со сроком снимаются,
    павшие NPC помечаются мёртвыми, `flags["encounter:<id>"] = "done"`, `combat = None`. Затем идёт
    новая сцена — со счётчиками сцены с нуля (вдохновение выдаётся раз за сцену) и без вида
    привала.

    Бой начинает `start_combat`, поэтому `next_type == COMBAT` и переход в ту же сцену —
    `CombatError`. Кому разрешено вызывать конец привала (таймер, хост), решает вызывающий;
    триггеры `on_scene_start` пока не вызываются: ни один модуль их не регистрирует."""
    _require(next_type != SceneType.COMBAT, "бой начинает start_combat, а не конец сцены")
    _require(next_type != state.scene.type, f"сцена уже «{next_type.value}»")
    result = state.model_copy(deep=True)
    events: list[Event] = []
    for trigger in rules.triggers.get("on_scene_end", ()):
        result, new_events = trigger(result, content)
        events += new_events
    if result.combat is not None:
        events += _close_combat(result, content, adventure)
    result.scene = Scene(type=next_type, started_turn=result.turn)
    events.append(SceneEvent(scene=next_type))
    return result, events


# --- заклинания, простые действия и действия монстров (B3.4) ---

SimpleAction = Literal[
    "dodge",
    "disengage",
    "dash",
    "help",
    "hide",
    "ready",
    "use_item",
    "shove",
    "grapple",
    "second_wind",
    "action_surge",
    "rage",
    "reckless",
    "steady_aim",
    "divine_spark",
    "preserve_life",
    "breath_weapon",
]


# --- заклинания ---

# Дальность заклинания -> дальность удара для проверок заклинательной атаки.
_STRIKE_RANGE: dict[str, Literal["melee", "near", "far"]] = {
    "self": "melee",
    "touch": "melee",
    "engaged": "melee",
    "near": "near",
    "far": "far",
}


@dataclass(frozen=True)
class _Cast:
    """Заклинание, прошедшее все проверки и готовое к розыгрышу."""

    spell: SpellDef
    level: int  # уровень, на котором сотворено: от него растёт урон, лечение и число дротиков
    slot: int | None  # потраченная ячейка (None — заговор или бесплатное сотворение)
    targets: tuple[str, ...]  # у заклинаний с дротиками и лучами — цель каждого из них
    strike: _Strike | None  # заклинательная атака


def _spellcasting(c: Character, content: SrdContent) -> SpellcastingDef:
    cls = content.classes.get(c.class_id)
    _require(cls is not None and cls.spellcasting is not None, f"{c.name} не умеет колдовать")
    return cls.spellcasting


def _scaled(dice: str, spell: SpellDef, level: int) -> str:
    """Кости заклинания с добавкой за каждый уровень ячейки выше уровня заклинания.

    У заклинаний с дротиками и лучами (`repeats` больше 1) за ячейку растёт их число, а не урон."""
    extra = level - spell.level
    if spell.upcast is None or spell.effect.repeats > 1 or extra <= 0:
        return dice
    return dice + f"+{spell.upcast}" * extra


def _pay_for(
    c: Character, spell: SpellDef, slot_level: int | None, content: SrdContent
) -> tuple[int | None, str | None, int]:
    """Чем платит заклинатель: (ячейка, ресурс класса, уровень сотворения). Только проверяет."""
    if spell.level == 0:
        return None, None, 0  # заговор ячейки не тратит, `slot_level` для него не нужен
    free = FREE_CASTS.get(spell.id)
    if (
        free is not None
        and slot_level is None
        and _has_feature(c, free[0], content)
        and c.resources.get(free[1], 0) > 0
    ):
        return None, free[1], spell.level
    if slot_level is not None:
        _require(
            slot_level >= spell.level,
            f"ячейка {slot_level}-го уровня ниже уровня заклинания «{spell.name}» ({spell.level})",
        )
        _require(
            c.spell_slots.get(slot_level, 0) > 0, f"{c.name}: нет ячейки {slot_level}-го уровня"
        )
        return slot_level, None, slot_level
    available = [
        level for level, left in sorted(c.spell_slots.items()) if level >= spell.level and left > 0
    ]
    _require(
        bool(available),
        f"{c.name}: нет ячейки для «{spell.name}» (нужна не ниже {spell.level}-го уровня)",
    )
    return available[0], None, available[0]


def _check_spell_range(
    combat: Combat, caster: Combatant, target: Combatant, spell: SpellDef
) -> None:
    dist = distance(combat, caster.id, target.id)
    same_zone = caster.zone_id == target.zone_id
    reach = {
        "far": True,
        "near": dist != "far",
        "engaged": dist == "engaged" or caster.id == target.id,
        # Касание: себя, противника вплотную или союзника рядом (союзники не «вовлечены»).
        "touch": caster.id == target.id
        or dist == "engaged"
        or (same_zone and caster.side == target.side),
        "self": same_zone,  # область от заклинателя: конус или куб в его зоне
    }[spell.range]
    _require(reach, f"{target.name} вне дальности заклинания «{spell.name}»")


def _spell_strike(c: Character, spell: SpellDef, level: int, content: SrdContent) -> _Strike:
    effect = spell.effect
    mark = effect.buff or effect.condition
    return _Strike(
        spell.name,
        spell_attack_bonus(c, content),
        _scaled(effect.damage, spell, level),
        effect.damage_type,
        _STRIKE_RANGE[spell.range],
        ability=_spellcasting(c, content).ability,
        half_on_miss=spell.level == 0 and _has_feature(c, POTENT_CANTRIP, content),
        condition=None
        if mark is None
        else ActiveCondition(id=mark, source=c.id, rounds_left=spell.duration_rounds),
    )


def _check_spell_target(
    s: GameState, caster: Combatant, target: Combatant, cast: _Cast, content: SrdContent
) -> None:
    spell, effect = cast.spell, cast.spell.effect
    entity = _entity(s, target.id)
    if effect.kind != "utility":
        _require(not _is_dead(entity), f"{target.name} уже мёртв")
    _check_spell_range(_active_combat(s), caster, target, spell)
    if effect.kind == "attack":
        _require(
            caster.side != target.side, f"{caster.name} не может атаковать союзника {target.name}"
        )
        dist = distance(_active_combat(s), caster.id, target.id)
        _range_mode(s, caster, target, cast.strike, dist)  # рукопашное — только вплотную
    elif effect.kind in ("auto", "save", "condition"):
        _require(caster.id != target.id, f"«{spell.name}» нельзя направить на себя")
    elif effect.kind == "buff" and effect.buff in MARK_EFFECTS:
        _require(caster.side != target.side, "метку можно поставить только на противника")
    elif effect.kind == "buff":
        _require(
            isinstance(entity, Character),
            f"эффект «{effect.buff}» можно наложить только на персонажа",
        )
    if effect.kind == "condition":
        _require(
            not _immune_to_condition(entity, effect.condition, content),
            _immune_message(entity, effect.condition),
        )


def _spell_targets(
    caster: Combatant, spell: SpellDef, level: int, target_ids: Sequence[str]
) -> tuple[str, ...]:
    """Цели заклинания по порядку. Дротики и лучи: одна цель на все или по одной на каждый."""
    effect, ids = spell.effect, list(target_ids)
    if not ids:
        self_aimed = effect.kind == "utility" or (
            effect.kind in ("buff", "heal") and spell.range == "self"
        )
        _require(self_aimed, "укажите цели заклинания")
        return (caster.id,)
    if effect.repeats > 1:
        darts = effect.repeats + level - spell.level
        _require(
            len(ids) in (1, darts),
            f"«{spell.name}»: одна цель на все или по одной на каждый из {darts}",
        )
        return tuple(ids * darts if len(ids) == 1 else ids)
    _require(len(set(ids)) == len(ids), "одна и та же цель указана дважды")
    _require(len(ids) <= effect.targets, f"слишком много целей: не больше {effect.targets}")
    return tuple(ids)


def _save_effect(
    s: GameState,
    source_id: str,
    target_ids: Sequence[str],
    *,
    ability: Ability,
    dc: int,
    label: str,
    damage: tuple[str, str] | None,
    half_on_save: bool,
    on_fail: ActiveCondition | None,
    rng: Rng,
    content: SrdContent,
) -> tuple[list[Event], list[str]]:
    """Цели делают спасбросок против Сл: провал — урон полностью и состояние, успех — половина
    урона (если положена). Кости урона бросаются один раз на всех. Возвращает события и
    список целей, проваливших спасбросок."""
    events: list[Event] = []
    outcomes: list[tuple[str, bool]] = []
    for target_id in dict.fromkeys(target_ids):
        target = _entity(s, target_id)
        if _is_dead(target):
            continue  # убита раньше в этом же действии
        result = _entity_save(target, ability, dc=dc, rng=rng, content=content, reason=label)
        events.append(_save_event(result, f"Спасбросок: {label}"))
        outcomes.append((target_id, result.success))
    if damage is not None and any(not saved or half_on_save for _, saved in outcomes):
        dice, damage_type = damage
        amount, rolled = _roll_part(
            _Part(f"Урон: {label}", dice, damage_type), False, source_id, rng
        )
        events += rolled
        for target_id, saved in outcomes:
            dealt = amount // 2 if saved else amount
            if (saved and not half_on_save) or dealt == 0:
                continue
            events += _deal_damage(
                s, target_id, [(dealt, damage_type)], crit=False, rng=rng, content=content
            )
    failed = [target_id for target_id, saved in outcomes if not saved]
    if on_fail is not None:
        for target_id in failed:
            target = _entity(s, target_id)
            if not _is_dead(target):
                events += _add_condition(s, target, on_fail.model_copy(), content)
    return events, failed


def _cast_attack(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    events: list[Event] = []
    combat = _active_combat(s)
    for target_id in cast.targets:
        if _is_dead(_entity(s, target_id)):
            continue  # луч в уже убитую цель пропускается
        dist = distance(combat, caster.id, target_id)
        mode = _range_mode(
            s, combat.combatants[caster.id], combat.combatants[target_id], cast.strike, dist
        )
        events += _resolve_attack(
            s,
            caster.id,
            target_id,
            cast.strike,
            Advantage.NONE,
            mode,
            kind="action",
            rng=rng,
            content=content,
        )
    return events


def _cast_auto(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    effect, events = cast.spell.effect, []
    part = _Part(f"Урон: {cast.spell.name}", effect.damage, effect.damage_type)
    for target_id in cast.targets:
        if _is_dead(_entity(s, target_id)):
            continue  # дротик в уже убитую цель пропускается
        amount, rolled = _roll_part(part, False, caster.id, rng)
        packet = [(amount, effect.damage_type)]
        events += rolled + _deal_damage(s, target_id, packet, crit=False, rng=rng, content=content)
    return events


def _cast_by_save(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    """Заклинания `save` и `condition`: цели делают спасбросок, провал — урон и/или состояние.

    Состояние от заклинания со спасброском повторяет его в конце хода цели (`save` и `dc`)."""
    spell, effect = cast.spell, cast.spell.effect
    dc = spell_save_dc(caster, content)
    condition = None
    if effect.condition is not None:
        condition = ActiveCondition(
            id=effect.condition,
            rounds_left=spell.duration_rounds,
            source=caster.id,
            save=effect.save,
            dc=dc if effect.save is not None else None,
        )
    if effect.save is None:  # состояние без спасброска
        events: list[Event] = []
        for target_id in cast.targets:
            events += _add_condition(s, _entity(s, target_id), condition.model_copy(), content)
        return events
    potent = spell.level == 0 and _has_feature(caster, POTENT_CANTRIP, content)
    damage = None
    if effect.damage is not None:
        damage = (_scaled(effect.damage, spell, cast.level), effect.damage_type)
    events, _ = _save_effect(
        s,
        caster.id,
        cast.targets,
        ability=effect.save,
        dc=dc,
        label=spell.name,
        damage=damage,
        half_on_save=effect.half_on_save or potent,
        on_fail=condition,
        rng=rng,
        content=content,
    )
    return events


def _heal_expression(cast: _Cast, caster: Character, content: SrdContent) -> str:
    """Кости лечения заклинания, модификатор заклинательной характеристики и Ученик жизни."""
    spell, effect = cast.spell, cast.spell.effect
    flat = 0
    if effect.heal_modifier:
        flat += ability_mod(caster, _spellcasting(caster, content).ability)
    if cast.slot is not None and _has_feature(caster, DISCIPLE_OF_LIFE, content):
        flat += 2 + cast.level
    dice = _scaled(effect.heal, spell, cast.level)
    return f"{dice}{flat:+d}" if flat else dice


def _cast_heal(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    label = f"Лечение: {cast.spell.name}"
    healed = roll(_heal_expression(cast, caster, content), rng=rng, reason=label)
    events: list[Event] = [RollEvent(actor_id=caster.id, label=label, roll=healed)]
    for target_id in dict.fromkeys(cast.targets):  # один бросок на всех целей
        events += _apply_heal(s, target_id, max(0, healed.total))
    return events


def _cast_aid(s: GameState, cast: _Cast, caster: Character, target: Character) -> list[Event]:
    """«Подмога» на цель: одноимённые эффекты не складываются, действует самый сильный.

    Более слабое наложение ничего не меняет; равное или более сильное снимает прежнее
    (максимум HP возвращает `end_buffs`) и ложится вместо него."""
    spell = cast.spell
    # В `upcast` «Подмоги» записан шаг +5 за уровень; столько же она даёт на базовом.
    bonus = int(spell.upcast) * (1 + cast.level - spell.level)
    current = max((int(b.data.get("hp", 0)) for b in target.buffs if b.id == AID_BUFF), default=0)
    if bonus < current:
        return [
            ActionEvent(
                actor_id=caster.id, action=spell.id, target_ids=[target.id], label=spell.name
            )
        ]
    end_buffs(target, lambda b: b.id == AID_BUFF)
    target.hp_max += bonus
    target.buffs.append(
        ActiveBuff(
            id=AID_BUFF, rounds_left=spell.duration_rounds, source_id=caster.id, data={"hp": bonus}
        )
    )
    return [
        ConditionEvent(target_id=target.id, condition=AID_BUFF, added=True),
        *_apply_heal(s, target.id, bonus),
    ]


def _cast_buff(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    spell, events = cast.spell, []
    buff_id = spell.effect.buff
    for target_id in cast.targets:
        target = _entity(s, target_id)
        if buff_id in MARK_EFFECTS:  # метка на противнике лежит в его состояниях
            mark = ActiveCondition(id=buff_id, source=caster.id, rounds_left=spell.duration_rounds)
            events += _add_condition(s, target, mark, content)
        elif buff_id == AID_BUFF:  # численный эффект: считается отдельно, не складывается
            events += _cast_aid(s, cast, caster, target)
        else:
            end_buffs(target, lambda b: b.id == buff_id and b.source_id == caster.id)  # обновление
            buff = ActiveBuff(id=buff_id, rounds_left=spell.duration_rounds, source_id=caster.id)
            target.buffs.append(buff)
            events.append(ConditionEvent(target_id=target_id, condition=buff_id, added=True))
    return events


def _cast_utility(
    s: GameState, cast: _Cast, caster: Character, rng: Rng, content: SrdContent
) -> list[Event]:
    # Полями эффект не описан: рассказывает мастер, а движок лишь отмечает сотворение.
    return [
        ActionEvent(
            actor_id=caster.id,
            action=cast.spell.id,
            target_ids=list(dict.fromkeys(cast.targets)),
            label=cast.spell.name,
        )
    ]


_SPELL_EFFECTS = {
    "attack": _cast_attack,
    "auto": _cast_auto,
    "save": _cast_by_save,
    "condition": _cast_by_save,
    "heal": _cast_heal,
    "buff": _cast_buff,
    "utility": _cast_utility,
}


def cast_spell(
    state: GameState,
    actor_id: str,
    spell_id: str,
    target_ids: Sequence[str],
    *,
    slot_level: int | None = None,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Персонаж творит заклинание в свой ход.

    Заклинание подготовлено (заговор — в списке заговоров), есть ячейка не ниже его уровня
    (`slot_level` не задан — берётся самая низкая из доступных; заговор ячейки не тратит) и
    цели в дальности. Эффекты по `SpellEffect.kind`: заклинательная атака, урон без броска,
    спасбросок цели против Сл заклинателя, лечение, эффект или состояние, событие для рассказа.
    Ячейка выше уровня усиливает заклинание (`upcast`; у дротиков и лучей растёт их число).
    Новая концентрация снимает прежнюю. Все проверки идут до первого броска: отказ
    (`CombatError`) не тратит кубы и не меняет состояние.
    """
    s = state.model_copy(deep=True)
    combat = _active_combat(s)
    caster_slot = _combatant(combat, actor_id)
    _require(
        caster_slot.kind == "pc",
        f"{caster_slot.name}: монстр не творит заклинаний, у него monster_action",
    )
    caster = s.party[actor_id]
    spell = content.spells.get(spell_id)
    _require(spell is not None, f"неизвестное заклинание {spell_id!r}")
    _check_can_act(s, caster_slot)
    _spellcasting(caster, content)
    known = caster.cantrips if spell.level == 0 else caster.prepared_spells
    _require(spell.id in known, f"{caster.name} не знает заклинания «{spell.name}»")
    _require(
        not spell.out_of_combat_only,
        f"«{spell.name}» сотворяется минутами и в бою недоступно",
    )
    time_flag = "bonus_action" if spell.casting_time == "bonus_action" else "action"
    _require_free(caster_slot, time_flag)
    slot, resource, level = _pay_for(caster, spell, slot_level, content)
    strike = _spell_strike(caster, spell, level, content) if spell.effect.kind == "attack" else None
    cast = _Cast(spell, level, slot, _spell_targets(caster_slot, spell, level, target_ids), strike)
    for target_id in dict.fromkeys(cast.targets):
        _check_spell_target(s, caster_slot, _combatant(combat, target_id), cast, content)

    # Все проверки позади: дальше расходуются ресурсы и бросаются кубы.
    if slot is not None:
        caster.spell_slots[slot] -= 1
    if resource is not None:
        caster.resources[resource] -= 1
    caster_slot.turn_flags[time_flag] = True
    events = _reveal(s, actor_id)
    if spell.concentration:
        if caster.concentration is not None:
            events += _end_concentration(s, caster, content)
        caster.concentration = spell.id
    return s, events + _SPELL_EFFECTS[spell.effect.kind](s, cast, caster, rng, content)


# --- простые действия ---

HIDE_DC = 15  # «Спрятаться»: проверка Ловкости (Скрытность) против Сл 15
RAGE_ROUNDS = 10  # ярость длится минуту
SPARK_OPTIONS = ("heal", "necrotic", "radiant")
# Дальность Божественного канала — число SRD (30, «9 м»); в бою по зонам это «не далеко».
CHANNEL_DIVINITY_RANGE = 30
# Типы урона дыхания по предкам драконорождённого; предок в персонаже не хранится (B2.4).
BREATH_DAMAGE_TYPES = ("acid", "cold", "fire", "lightning", "poison")
DEFAULT_BREATH_TYPE = "fire"


@dataclass
class _Act:
    """Простое действие, прошедшее общие проверки хода: рабочая копия состояния и аргументы."""

    s: GameState
    slot: Combatant
    entity: Entity
    target_id: str | None
    item_id: str | None
    option: str | None
    rng: Rng
    content: SrdContent
    rules: RuleSet
    carried_items: Mapping[str, str]  # предмет приключения -> предмет SRD, чья механика применяется

    def target(self) -> Combatant:
        _require(self.target_id is not None, "укажите цель (target_id)")
        return _combatant(_active_combat(self.s), self.target_id)

    def take(self, flag: Literal["action", "bonus_action"]) -> None:
        """Тратит действие или бонусное действие хода."""
        _require_free(self.slot, flag)
        self.slot.turn_flags[flag] = True


def _action_or_bonus(a: _Act, flag: str) -> Literal["action", "bonus_action"]:
    """Что тратит действие: бонусное действие, если модуль правил включил флаг, иначе действие."""
    return "bonus_action" if a.rules.flags.get(flag, False) else "action"


def _announce(actor_id: str, action: str) -> list[Event]:
    # Действию без бросков и без записи в состоянии нужно событие для рассказа; это не
    # `ConditionEvent`: у него не будет парного снятия.
    return [ActionEvent(actor_id=actor_id, action=action)]


def _require_feature(c: Character, feature_id: str, content: SrdContent) -> None:
    _require(_has_feature(c, feature_id, content), f"{c.name}: нет особенности «{feature_id}»")


def _require_uses(c: Character, resource: str, left: int | None = None) -> None:
    left = c.resources.get(resource, 0) if left is None else left
    _require(left > 0, f"{c.name}: нет использований «{resource}»")


def _enemy_in_melee(a: _Act) -> Combatant:
    """Противник вплотную: цель помощи, толчка и захвата."""
    target = a.target()
    _require(target.side != a.slot.side, f"{target.name}: нужен противник")
    _require(not _is_dead(_entity(a.s, target.id)), f"{target.name} уже мёртв")
    _require(
        distance(_active_combat(a.s), a.slot.id, target.id) == "engaged",
        f"{target.name}: противник должен быть вплотную",
    )
    return target


def _dodge(a: _Act) -> list[Event]:
    a.take("action")
    a.slot.dodging = True
    return _announce(a.slot.id, "dodge")


def _disengage(a: _Act) -> list[Event]:
    a.take("action")
    a.slot.disengaged = True
    return _announce(a.slot.id, "disengage")


def _dash(a: _Act) -> list[Event]:
    a.take("action")
    a.slot.turn_flags["dashed"] = True  # второе перемещение: см. `_check_move_budget`
    return _announce(a.slot.id, "dash")


def _ready(a: _Act) -> list[Event]:
    a.take("action")
    # Подготовленное действие разыгрывается реакцией (`attack(..., reaction=True)`), когда
    # наступит условие; флаг живёт до начала следующего хода бойца.
    a.slot.turn_flags["readied"] = True
    return _announce(a.slot.id, "ready")


def _revive_ally(a: _Act) -> list[Event]:
    """«Помощь» упавшему союзнику (`help_revives`): он приходит в себя с 1 HP.

    Это лечение на 1 HP: оно же снимает «без сознания» и обнуляет спасброски от смерти, так что
    упавший становится стабильным. Мёртвого «Помощь» не возвращает."""
    target = a.target()
    patient = _entity(a.s, target.id)
    _require(not _is_dead(patient), f"{target.name} мёртв: «Помощь» его не вернёт")
    _require(patient.hp == 0, f"{target.name}: «Помощь» союзнику нужна, только если он на 0 HP")
    _require(
        target.zone_id == a.slot.zone_id,
        f"{target.name} вне дальности: поднять упавшего можно в той же зоне",
    )
    a.take("action")
    return _apply_heal(a.s, target.id, 1)


def _help(a: _Act) -> list[Event]:
    if a.rules.flags.get(HELP_REVIVES, False) and a.target().side == a.slot.side:
        return _revive_ally(a)
    target = _enemy_in_melee(a)
    a.take("action")
    mark = ActiveCondition(id=HELPED, source=a.slot.id)
    return _add_condition(a.s, _entity(a.s, target.id), mark, a.content)


def _hide(a: _Act) -> list[Event]:
    a.take("action")
    check = ability_check(
        a.entity,
        Ability.DEX,
        skill=Skill.STEALTH,
        dc=HIDE_DC,
        advantage=Advantage.NONE,
        rng=a.rng,
        reason=f"Скрытность: {a.entity.name}",
        hooks=a.rules.check_hooks,
    )
    events: list[Event] = [_save_event(check, "Скрытность")]
    if check.success:
        hidden = ActiveCondition(id=INVISIBLE, source=HIDDEN_SOURCE)
        events += _add_condition(a.s, a.entity, hidden, a.content)
    return events


def _use_item(a: _Act) -> list[Event]:
    c = a.entity
    _require(a.item_id is not None, "укажите предмет (item_id)")
    # Предмет приключения лежит вне листа персонажа (его держателя ведёт мир): механика — у
    # предмета SRD, а расход записи в инвентаре не нужен.
    srd_id, entry = a.carried_items.get(a.item_id), None
    if srd_id is None:
        entry = next((e for e in c.inventory if e.item_id == a.item_id and e.qty > 0), None)
        _require(entry is not None, f"у {c.name} нет предмета {a.item_id!r}")
        srd_id = a.item_id
    item_def = a.content.items.get(srd_id)
    _require(
        item_def is not None and item_def.kind == "potion" and item_def.heal is not None,
        f"предмет {a.item_id!r} нельзя применить в бою",
    )
    target = _combatant(_active_combat(a.s), a.target_id or c.id)
    _require(target.side == a.slot.side, "зелье можно дать только себе или союзнику")
    _require(
        target.id == c.id or target.zone_id == a.slot.zone_id,
        f"{target.name} вне дальности: зелье отдают в той же зоне",
    )
    _require(not _is_dead(_entity(a.s, target.id)), f"{target.name} мёртв")
    a.take(_action_or_bonus(a, POTION_BONUS_ACTION))
    if entry is not None:
        entry.qty -= 1
        if entry.qty == 0:
            c.inventory.remove(entry)
    label = f"Зелье: {item_def.name}"
    healed = roll(item_def.heal, rng=a.rng, reason=label)
    events: list[Event] = [RollEvent(actor_id=c.id, label=label, roll=healed)]
    return events + _apply_heal(a.s, target.id, max(0, healed.total))


def _save_bonus(entity: Entity, ability: Ability, content: SrdContent) -> int:
    if isinstance(entity, Character):
        return save_bonus(entity, ability)
    monster = _monster_def(entity, content)
    return monster.saves.get(ability, modifier(monster.scores[ability]))


def _unarmed_contest(
    a: _Act, label: str, condition_id: str | None, cost: Literal["action", "bonus_action"]
) -> list[Event]:
    """Толчок и захват: цель вплотную выбирает лучший из спасбросков Силы и Ловкости против
    Сл 8 + Сила + бонус мастерства; при провале — состояние `condition_id` (если оно задано).
    `cost` — что тратит приём: действие или (по флагу модуля) бонусное действие."""
    target = _enemy_in_melee(a)
    victim = _entity(a.s, target.id)
    if condition_id is not None:
        _require(
            not _immune_to_condition(victim, condition_id, a.content),
            _immune_message(victim, condition_id),
        )
    a.take(cost)
    c = a.entity
    ability = max((Ability.STR, Ability.DEX), key=lambda x: _save_bonus(victim, x, a.content))
    condition = None
    if condition_id is not None:
        condition = ActiveCondition(id=condition_id, source=c.id)
    events, failed = _save_effect(
        a.s,
        c.id,
        [target.id],
        ability=ability,
        dc=8 + ability_mod(c, Ability.STR) + prof_bonus(c),
        label=label,
        damage=None,
        half_on_save=False,
        on_fail=condition,
        rng=a.rng,
        content=a.content,
    )
    if failed and condition_id is None:  # отталкивание: цель больше не вплотную
        a.slot.engaged_with.remove(target.id)
        target.engaged_with.remove(a.slot.id)
    return events


def _shove(a: _Act) -> list[Event]:
    _require(a.option in (None, "prone", "push"), "толчок: option должен быть push или prone")
    cost = _action_or_bonus(a, SHOVE_BONUS_ACTION)
    return _unarmed_contest(a, "Толчок", None if a.option == "push" else "prone", cost)


def _grapple(a: _Act) -> list[Event]:
    return _unarmed_contest(a, "Захват", "grappled", "action")


def _second_wind(a: _Act) -> list[Event]:
    c = a.entity
    _require_feature(c, "second_wind", a.content)
    _require_uses(c, "second_wind")
    a.take("bonus_action")
    c.resources["second_wind"] -= 1
    healed = roll(f"1d10+{c.level}", rng=a.rng, reason="Второе дыхание")
    events: list[Event] = [RollEvent(actor_id=c.id, label="Второе дыхание", roll=healed)]
    return events + _apply_heal(a.s, c.id, max(0, healed.total))


def _action_surge(a: _Act) -> list[Event]:
    c = a.entity
    _require_feature(c, "action_surge", a.content)
    _require_uses(c, "action_surge")
    _require(
        a.slot.turn_flags.get("action", False),
        "Всплеск действий даёт ещё одно действие: сначала потратьте обычное",
    )
    c.resources["action_surge"] -= 1
    a.slot.turn_flags["action"] = False
    return _announce(c.id, "action_surge")


def _rage(a: _Act) -> list[Event]:
    c = a.entity
    _require_feature(c, "rage", a.content)
    _require_uses(c, "rage")
    _require(not _is_raging(c), f"{c.name} уже в ярости")
    a.take("bonus_action")
    c.resources["rage"] -= 1
    c.buffs.append(ActiveBuff(id=RAGE_BUFF, rounds_left=RAGE_ROUNDS, source_id=c.id))
    # Ярость лежит в `buffs`, и срок снимает её парным событием: это состояние, а не действие.
    return [ConditionEvent(target_id=c.id, condition=RAGE_BUFF, added=True)]


def _reckless(a: _Act) -> list[Event]:
    c, flags = a.entity, a.slot.turn_flags
    _require_feature(c, "reckless_attack", a.content)
    _require(not flags.get("action", False), "Безрассудную атаку объявляют до первой атаки хода")
    _require(not flags.get("reckless", False), "Безрассудная атака уже объявлена")
    flags["reckless"] = True  # свободное объявление: действие не тратится
    return _announce(c.id, "reckless")


def _steady_aim(a: _Act) -> list[Event]:
    c, flags = a.entity, a.slot.turn_flags
    _require_feature(c, "steady_aim", a.content)
    _require(
        not (flags.get("moved", False) or flags.get("moved_again", False)),
        f"{c.name}: Точное прицеливание возможно, только если не двигался в этот ход",
    )
    a.take("bonus_action")
    flags["steady_aim"] = True  # `move` после этого запрещён, атака получает преимущество
    return _announce(c.id, "steady_aim")


def _channel_divinity_target(a: _Act) -> Combatant:
    """Цель Божественного канала в 9 м (расстояние не «далеко»)."""
    target = a.target()
    dist = distance(_active_combat(a.s), a.slot.id, target.id)
    _require(dist != "far", f"{target.name} вне дальности ({meters_text(CHANNEL_DIVINITY_RANGE)})")
    return target


def _divine_spark(a: _Act) -> list[Event]:
    c = a.entity
    _require_feature(c, "divine_spark", a.content)
    _require_uses(c, "channel_divinity")
    _require(a.option in SPARK_OPTIONS, "option должен быть heal, necrotic или radiant")
    target = _channel_divinity_target(a)
    a.take("action")
    c.resources["channel_divinity"] -= 1
    ability = _spellcasting(c, a.content).ability
    dice = f"1d8{ability_mod(c, ability):+d}"
    if a.option == "heal":
        healed = roll(dice, rng=a.rng, reason="Божественная искра")
        events: list[Event] = [RollEvent(actor_id=c.id, label="Божественная искра", roll=healed)]
        return events + _apply_heal(a.s, target.id, max(0, healed.total))
    events, _ = _save_effect(
        a.s,
        c.id,
        [target.id],
        ability=Ability.CON,
        dc=spell_save_dc(c, a.content),
        label="Божественная искра",
        damage=(dice, a.option),
        half_on_save=True,
        on_fail=None,
        rng=a.rng,
        content=a.content,
    )
    return events


def _preserve_life(a: _Act) -> list[Event]:
    c = a.entity
    _require_feature(c, "preserve_life", a.content)
    _require_uses(c, "channel_divinity")
    target = _channel_divinity_target(a)
    patient = _entity(a.s, target.id)
    _require(not _is_dead(patient), f"{target.name} мёртв")
    room = patient.hp_max // 2 - patient.hp
    _require(
        room > 0,
        f"{target.name}: HP выше половины максимума, «Сохранение жизни» их не поднимет",
    )
    a.take("action")
    c.resources["channel_divinity"] -= 1
    # Общий запас 5 × уровень; цель одна (target_id), поэтому он целиком уходит ей.
    return _apply_heal(a.s, target.id, min(5 * c.level, room))


def _breath_weapon(a: _Act) -> list[Event]:
    c = a.entity
    _require(
        _has_trait(c, BREATH_WEAPON_TRAIT, a.content),
        f"{c.name}: нет черты «{BREATH_WEAPON_TRAIT}»",
    )
    uses = c.resources.get(BREATH_WEAPON_TRAIT, prof_bonus(c))  # нет записи — все целы
    _require_uses(c, BREATH_WEAPON_TRAIT, uses)
    damage_type = a.option or DEFAULT_BREATH_TYPE
    _require(
        damage_type in BREATH_DAMAGE_TYPES,
        "тип урона дыхания — один из: " + ", ".join(BREATH_DAMAGE_TYPES),
    )
    target = a.target()
    _require(
        target.zone_id == a.slot.zone_id,
        f"{target.name} вне дальности: конус дыхания достаёт только до своей зоны",
    )
    a.take("action")
    c.resources[BREATH_WEAPON_TRAIT] = uses - 1
    events, _ = _save_effect(
        a.s,
        c.id,
        [target.id],
        ability=Ability.DEX,
        dc=8 + ability_mod(c, Ability.CON) + prof_bonus(c),
        label="Дыхание",
        damage=("1d10", damage_type),  # рост кости с 5-го уровня в срез не входит
        half_on_save=True,
        on_fail=None,
        rng=a.rng,
        content=a.content,
    )
    return events


_ACTIONS = {
    "dodge": _dodge,
    "disengage": _disengage,
    "dash": _dash,
    "help": _help,
    "hide": _hide,
    "ready": _ready,
    "use_item": _use_item,
    "shove": _shove,
    "grapple": _grapple,
    "second_wind": _second_wind,
    "action_surge": _action_surge,
    "rage": _rage,
    "reckless": _reckless,
    "steady_aim": _steady_aim,
    "divine_spark": _divine_spark,
    "preserve_life": _preserve_life,
    "breath_weapon": _breath_weapon,
}
# Действия, которые доступны и монстрам: им не нужны ни предметы, ни ресурсы, ни навыки.
_ANY_FIGHTER_ACTIONS = frozenset({"dodge", "disengage", "dash", "ready", "help"})


def simple_action(
    state: GameState,
    actor_id: str,
    action: SimpleAction,
    *,
    target_id: str | None = None,
    item_id: str | None = None,
    option: str | None = None,
    rules: RuleSet = NO_RULES,
    carried_items: Mapping[str, str] | None = None,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Простое действие бойца в его ход: стандартные действия и ресурсы классов и видов.

    Уклонение, Отход, Рывок, Подготовка, Помощь, Спрятаться, использование предмета (зелье
    лечения), толчок и захват; второе дыхание, всплеск действий, ярость, безрассудная атака,
    точное прицеливание, божественная искра, сохранение жизни, дыхание драконорождённого.
    Действие или бонусное действие тратится по правилам; ресурс не уходит в минус. Все
    проверки идут до первого броска: отказ (`CombatError`) не тратит кубы и не меняет состояние.
    `rules` — набор правил модулей: флаги `potion_bonus_action` и `shove_bonus_action` переводят
    зелье и толчок на бонусное действие, `help_revives` разрешает «Помощь» упавшему союзнику
    (цель в той же зоне на 0 HP, не мёртвая; она встаёт с 1 HP), хуки проверок идут в «Спрятаться».
    `carried_items` — предметы приключения, которые боец несёт вне листа персонажа: `item_id`
    из этого словаря берёт механику у предмета SRD (зелье лечения), но запись инвентаря не
    расходует — расход предмета ведёт вызывающий.
    Монстры по этой функции могут только уклоняться, отходить, делать рывок, готовить действие
    и помогать; остальное они делают через `monster_action`.
    """
    s = state.model_copy(deep=True)
    slot = _combatant(_active_combat(s), actor_id)
    handler = _ACTIONS.get(action)
    _require(handler is not None, f"неизвестное действие {action!r}")
    entity = _check_can_act(s, slot)
    if action not in _ANY_FIGHTER_ACTIONS:
        _require(
            isinstance(entity, Character), f"{slot.name}: монстр действует через monster_action"
        )
    act = _Act(
        s, slot, entity, target_id, item_id, option, rng, content, rules, carried_items or {}
    )
    return s, handler(act)


# --- действия монстров ---


def _monster_steps(monster: MonsterDef, action: MonsterAction) -> list[MonsterAction]:
    """Атаки, из которых состоит действие: мультиатака перечисляет их, обычная — одна."""
    if action.kind != "multiattack":
        return [action]
    by_id = {a.id: a for a in monster.actions}
    for step_id in action.multiattack:
        # Кастомный статблок мог обойти проверку ссылок данных SRD.
        _require(step_id in by_id, f"«{action.name}»: в мультиатаке нет действия {step_id!r}")
    steps = [by_id[step_id] for step_id in action.multiattack]
    _require(
        all(step.kind == "attack" for step in steps),
        f"«{action.name}»: в мультиатаку входят только атаки",
    )
    return steps


def _monster_attacks(
    s: GameState,
    slot: Combatant,
    steps: list[MonsterAction],
    target_ids: list[str],
    *,
    rng: Rng,
    content: SrdContent,
) -> list[Event]:
    """Атаки монстра по порядку; действие тратится один раз. Цель — одна на все или по одной
    на каждую атаку. Атака в уже убитую цель пропускается."""
    _require(
        len(target_ids) in (1, len(steps)),
        f"одна цель на все атаки или по одной на каждую из {len(steps)}",
    )
    targets = target_ids * len(steps) if len(target_ids) == 1 else target_ids
    combat = _active_combat(s)
    plan = []
    # Длины равны всегда: проверка выше пускает одну цель (её размножили) или по одной на атаку.
    for step, target_id in zip(steps, targets, strict=True):
        _check_target(s, slot, _combatant(combat, target_id))
        strike, range_mode = _prepare_strike(s, slot.id, target_id, None, step.id, content)
        plan.append((target_id, strike, range_mode))
    slot.turn_flags["action"] = True
    events: list[Event] = []
    for target_id, strike, range_mode in plan:
        if _is_dead(_entity(s, target_id)):
            continue
        events += _resolve_attack(
            s,
            slot.id,
            target_id,
            strike,
            Advantage.NONE,
            range_mode,
            kind="action",
            rng=rng,
            content=content,
        )
    return events


def _monster_save_action(
    s: GameState,
    slot: Combatant,
    action: MonsterAction,
    target_ids: list[str],
    *,
    rng: Rng,
    content: SrdContent,
) -> list[Event]:
    """Действие со спасброском (паутина, дыхание): Сл из статблока, цели делают спасбросок."""
    _require(
        action.save is not None and action.dc is not None,
        f"у действия «{action.name}» не заданы характеристика и Сл спасброска",
    )
    combat = _active_combat(s)
    distinct = list(dict.fromkeys(target_ids))
    _require(
        len(distinct) <= action.targets,
        f"«{action.name}»: целей не больше {action.targets}, названо {len(distinct)}",
    )
    for target_id in distinct:
        target = _combatant(combat, target_id)
        _check_target(s, slot, target)
        dist = distance(combat, slot.id, target_id)
        in_reach = {"melee": dist == "engaged", "near": dist != "far", "far": True}[action.range]
        _require(in_reach, f"{target.name} вне дальности действия «{action.name}»")
    slot.turn_flags["action"] = True
    damage = None if action.damage is None else (action.damage, action.damage_type)
    condition = None
    if action.condition is not None:
        condition = ActiveCondition(id=action.condition, source=slot.id)
    events, _ = _save_effect(
        s,
        slot.id,
        target_ids,
        ability=action.save,
        dc=action.dc,
        label=action.name,
        damage=damage,
        half_on_save=action.half_on_save,
        on_fail=condition,
        rng=rng,
        content=content,
    )
    return events


def monster_action(
    state: GameState,
    monster_id: str,
    action_id: str,
    target_ids: Sequence[str],
    *,
    rng: Rng,
    content: SrdContent,
) -> tuple[GameState, list[Event]]:
    """Действие монстра по его статблоку.

    `attack` — атака с бонусом монстра (как `attack` с `action_id`: условные эффекты попадания
    — сбит с ног, схвачен, паралич после спасброска — накладываются там же); `save` — Сл
    статблока и спасбросок целей, но разных целей не больше `targets` действия (по умолчанию
    одна); `multiattack` — перечисленные атаки подряд, действие тратится один раз, ссылка на
    несуществующее действие — `CombatError`; `other` — только событие для рассказа: бонусные
    действия, реакции и колдовство разыгрывает мастер, поэтому ни свой ход, ни флаги не
    проверяются и не меняются, но монстр должен быть жив и дееспособен. Все проверки идут до
    первого броска: отказ (`CombatError`) не тратит кубы и не меняет состояние.
    """
    s = state.model_copy(deep=True)
    slot = _combatant(_active_combat(s), monster_id)
    _require(
        slot.kind == "monster",
        f"{slot.name} — не монстр: персонажи действуют через attack, cast_spell и simple_action",
    )
    monster = _monster_def(slot, content)
    action = next((a for a in monster.actions if a.id == action_id), None)
    _require(action is not None, f"у {monster.name} нет действия {action_id!r}")
    # Реакции и бонусные действия идут вне своего хода, но мёртвый или недееспособный не действует.
    _check_can_act(s, slot, own_turn=action.kind != "other")
    if action.kind == "other":
        return s, [ActionEvent(actor_id=monster_id, action=action.id, label=action.name)]
    _require_free(slot, "action")
    ids = list(target_ids)
    _require(bool(ids), "укажите цель (target_ids)")
    if action.kind == "save":
        return s, _monster_save_action(s, slot, action, ids, rng=rng, content=content)
    steps = _monster_steps(monster, action)
    return s, _monster_attacks(s, slot, steps, ids, rng=rng, content=content)
