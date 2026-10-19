"""Бой: заклинания, простые действия, действия монстров (B3.4).

Данные — встроенные SRD. Случайность — только `ScriptedRng`: значения расходуются строго в
порядке бросков, поэтому лишний или недостающий бросок виден сразу.

Порядок бросков:
- заклинательная атака: к20 (две кости при преимуществе или помехе), кость благословения, кости
  урона, затем спасбросок концентрации цели; у заклинаний с дротиками и лучами — по порядку;
- спасбросок цели: у каждой цели к20 по порядку, затем кости урона один раз на всех;
- лечение: одна запись костей на всех целей.
"""

import pytest

from dnd_engine.abilities import Ability, Skill
from dnd_engine.character import (
    ActiveBuff,
    ActiveCondition,
    Character,
    DeathSaves,
    InventoryItem,
    armor_class,
)
from dnd_engine.combat.engine import (
    HIDDEN_SOURCE,
    CombatError,
    apply_condition,
    attack,
    cast_spell,
    current_actor,
    end_turn,
    monster_action,
    move,
    simple_action,
    start_combat,
)
from dnd_engine.combat import engine as combat_engine
from dnd_engine.combat.model import Zone, ZoneLink
from dnd_engine.conditions import ZERO_HP_SOURCE, is_dead
from dnd_engine.content import MonsterAction, SrdContent, load_srd
from dnd_engine.dice import Advantage
from dnd_engine.events import (
    ActionEvent,
    CombatEvent,
    ConditionEvent,
    DamageEvent,
    HealEvent,
    RestEvent,
    RollEvent,
    SceneEvent,
)
from dnd_engine.modules import build_ruleset
from dnd_engine.modules.base import (
    POTION_BONUS_ACTION,
    SHOVE_BONUS_ACTION,
    RuleRegistry,
)
from dnd_engine.rest import end_buffs
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef, SceneType

STR, DEX, CON, INT, WIS, CHA = Ability
NONE, ADVANTAGE, DISADVANTAGE = Advantage.NONE, Advantage.ADVANTAGE, Advantage.DISADVANTAGE
BG3 = build_ruleset(RulesRef(modules=["bg3_flavor"]))
CAMPFIRE = build_ruleset(RulesRef(modules=["campfire"]))  # модуль без флагов боя


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


# --- общая расстановка ---

ZONES = [
    Zone(id="hall", name="Зал"),
    Zone(id="stairs", name="Лестница"),
    Zone(id="vault", name="Хранилище"),
]
LINKS = [
    ZoneLink(a="hall", b="stairs", distance="near"),
    ZoneLink(a="hall", b="vault", distance="far"),
]
HERO, ALLY, WIZARD, CLERIC, RANGER = "pc-1", "pc-2", "wiz", "clr", "rgr"
GOBLIN, GOBLIN_2, OGRE = "goblin_warrior-1", "goblin_warrior-2", "ogre-1"
CAPTAIN, GHOUL, WOLF = "bandit_captain-1", "ghoul-1", "wolf-1"
BUGBEAR, SPIDER = "bugbear_warrior-1", "giant_spider-1"


def item(item_id: str, qty: int = 1, equipped: bool = True) -> InventoryItem:
    return InventoryItem(item_id=item_id, qty=qty, equipped=equipped)


@pytest.fixture
def make_hero(make_character):
    """Воин 3-го уровня, Сила 16 (+3), длинный меч: атака +5, урон 1к8+3, КД 10, HP 20.

    Предыстория «Служитель» (черта «Посвящённый в магию»): ни «Бдительного», ни «Свирепого
    атакующего»."""

    def factory(**overrides) -> Character:
        fields = {
            "id": HERO,
            "name": "Герой",
            "background": "acolyte",
            "scores": {STR: 16},
            "inventory": [item("longsword")],
            "resources": {"second_wind": 2, "action_surge": 1},
        }
        return make_character(**(fields | overrides))

    return factory


@pytest.fixture
def make_ally(make_hero):
    def factory(**overrides) -> Character:
        return make_hero(**({"id": ALLY, "name": "Союзник"} | overrides))

    return factory


@pytest.fixture
def make_wizard(make_character):
    """Волшебник 3-го уровня, Инт 16 (+3): атака заклинанием +5, Сл 13; ячейки 4 и 2."""

    def factory(**overrides) -> Character:
        fields = {
            "id": WIZARD,
            "name": "Маг",
            "class_id": "wizard",
            "background": "sage",
            "scores": {INT: 16},
            "save_proficiencies": [INT, WIS],
            "spell_slots_max": {1: 4, 2: 2},
            "spell_slots": {1: 4, 2: 2},
            "cantrips": ["fire_bolt", "ray_of_frost", "shocking_grasp", "sacred_flame"],
            "prepared_spells": [
                "magic_missile",
                "burning_hands",
                "thunderwave",
                "scorching_ray",
                "mage_armor",
                "sleep",
                "hold_person",
                "misty_step",
                "fog_cloud",
            ],
            "resources": {"arcane_recovery": 1},
        }
        return make_character(**(fields | overrides))

    return factory


@pytest.fixture
def make_cleric(make_character):
    """Жрец 3-го уровня, Мдр 16 (+3): Сл 13, Ученик жизни; ячейки 4 и 2, две искры."""

    def factory(**overrides) -> Character:
        fields = {
            "id": CLERIC,
            "name": "Жрец",
            "class_id": "cleric",
            "background": "acolyte",
            "scores": {WIS: 16},
            "save_proficiencies": [WIS, CHA],
            "spell_slots_max": {1: 4, 2: 2},
            "spell_slots": {1: 4, 2: 2},
            "cantrips": ["sacred_flame", "guidance", "spare_the_dying"],
            "prepared_spells": [
                "bless",
                "healing_word",
                "cure_wounds",
                "guiding_bolt",
                "aid",
                "hold_person",
                "prayer_of_healing",
                "inflict_wounds",
            ],
            "resources": {"channel_divinity": 2},
        }
        return make_character(**(fields | overrides))

    return factory


@pytest.fixture
def make_ranger(make_character):
    """Следопыт 3-го уровня, Лов 16, Мдр 14: стрела +5, заклинания +4, Сл 12; три ячейки."""

    def factory(**overrides) -> Character:
        fields = {
            "id": RANGER,
            "name": "Следопыт",
            "class_id": "ranger",
            "background": "acolyte",
            "scores": {DEX: 16, WIS: 14},
            "spell_slots_max": {1: 3},
            "spell_slots": {1: 3},
            "prepared_spells": ["hunters_mark", "cure_wounds", "goodberry", "fog_cloud"],
            "inventory": [item("longbow")],
            "resources": {"hunters_mark": 2},
        }
        return make_character(**(fields | overrides))

    return factory


def state_of(*characters: Character) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RulesRef(),
        party={c.id: c for c in characters},
        location_id="entrance",
    )


def standing(c: Character) -> Character:
    """Персонаж на ногах на время начала боя: лежащий первым бросал бы спасбросок от смерти."""
    if is_dead(c) or c.hp > 0:
        return c
    return c.model_copy(update={"hp": 1, "conditions": []})


def battle(
    srd: SrdContent,
    party: list[Character],
    monsters: tuple[str, ...] = ("goblin_warrior",),
    *,
    monster_zone: str = "hall",
) -> GameState:
    """Бой в зале: отряд ходит первым (инициатива 20, 19, ...), монстры — после.

    Упавшие персонажи ложатся после начала боя: он идёт, пока они уже лежат, но кубов на
    спасбросок от смерти первого хода у теста нет."""
    living = [c for c in party if not is_dead(c)]
    rolls = [20 - i for i in range(len(living))] + [5 - i for i in range(len(monsters))]
    state, _ = start_combat(
        state_of(*map(standing, party)),
        encounter_id="test",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters[monster_id], monster_zone) for monster_id in monsters],
        pc_zone="hall",
        rng=ScriptedRng(rolls),
        content=srd,
    )
    state.party = {c.id: c for c in party}
    return state


def melee(state: GameState, a: str, b: str) -> GameState:
    """Расстановка для теста: бойцы вовлечены друг с другом."""
    state.combat.combatants[a].engaged_with.append(b)
    state.combat.combatants[b].engaged_with.append(a)
    return state


def fighter(state: GameState, fighter_id: str):
    return state.combat.combatants[fighter_id]


def turn_of(state: GameState, fighter_id: str) -> GameState:
    """Расстановка для теста: сейчас ход этого бойца."""
    state.combat.turn_index = state.combat.order.index(fighter_id)
    return state


def flags(state: GameState, fighter_id: str) -> dict[str, bool]:
    return fighter(state, fighter_id).turn_flags


def all_dice_spent(rng: ScriptedRng) -> bool:
    try:
        rng.randint(1, 100)
    except ValueError:
        return True
    return False


def cast(srd, state, rolls, spell_id, targets=(), caster=WIZARD, **kwargs):
    """Заклинание на заданных кубах; заодно проверяет, что кубы израсходованы без остатка."""
    rng = ScriptedRng(rolls)
    after, events = cast_spell(state, caster, spell_id, targets, rng=rng, content=srd, **kwargs)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def act(srd, state, rolls, action, actor=HERO, **kwargs):
    """Простое действие на заданных кубах; кубы должны быть израсходованы без остатка."""
    rng = ScriptedRng(rolls)
    after, events = simple_action(state, actor, action, rng=rng, content=srd, **kwargs)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def mob(srd, state, rolls, monster_id, action_id, targets):
    """Действие монстра на заданных кубах; кубы должны быть израсходованы без остатка."""
    rng = ScriptedRng(rolls)
    after, events = monster_action(state, monster_id, action_id, targets, rng=rng, content=srd)
    assert all_dice_spent(rng), "остались неиспользованные значения кубов"
    return after, events


def refused(call, state, reason):
    """Действие отклонено: CombatError с понятной причиной, состояние и кубы не тронуты."""
    snapshot = state.model_copy(deep=True)
    rng = ScriptedRng([10])
    with pytest.raises(CombatError, match=reason):
        call(state, rng)
    assert state == snapshot
    assert rng.randint(1, 20) == 10


def refused_cast(srd, state, spell_id, targets, reason, caster=WIZARD, **kwargs):
    def call(s, rng):
        return cast_spell(s, caster, spell_id, targets, rng=rng, content=srd, **kwargs)

    refused(call, state, reason)


def refused_act(srd, state, action, reason, actor=HERO, **kwargs):
    def call(s, rng):
        return simple_action(s, actor, action, rng=rng, content=srd, **kwargs)

    refused(call, state, reason)


def refused_mob(srd, state, monster_id, action_id, targets, reason):
    def call(s, rng):
        return monster_action(s, monster_id, action_id, targets, rng=rng, content=srd)

    refused(call, state, reason)


def srd_with_monster(srd: SrdContent, monster_id: str, **changes) -> SrdContent:
    """Копия данных, где у монстра заменены поля (например, `actions`): кастомный статблок."""
    monster = srd.monsters[monster_id].model_copy(update=changes)
    return srd.model_copy(update={"monsters": {**srd.monsters, monster_id: monster}})


def damage_events(events) -> list[DamageEvent]:
    return [e for e in events if isinstance(e, DamageEvent)]


def roll_events(events) -> list[RollEvent]:
    return [e for e in events if isinstance(e, RollEvent)]


def condition_events(events) -> list[ConditionEvent]:
    return [e for e in events if isinstance(e, ConditionEvent)]


def labels(events) -> list[str]:
    return [e.label for e in roll_events(events)]


def hp(state: GameState, fighter_id: str) -> int:
    if fighter_id in state.party:
        return state.party[fighter_id].hp
    return fighter(state, fighter_id).hp


def condition_ids(state: GameState, fighter_id: str) -> list[str]:
    if fighter_id in state.party:
        return [c.id for c in state.party[fighter_id].conditions]
    return [c.id for c in fighter(state, fighter_id).conditions]


def bless(source: str = CLERIC) -> ActiveBuff:
    return ActiveBuff(id="bless", rounds_left=10, source_id=source)


# =============================================================================================
# Заклинания: общие проверки
# =============================================================================================


def test_unknown_spell_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "wish", [GOBLIN], "неизвестное заклинание")


def test_a_spell_that_the_character_has_not_prepared_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard(prepared_spells=["mage_armor"])])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "не знает")


def test_a_leveled_spell_known_only_as_a_cantrip_is_refused(srd, make_wizard):
    wizard = make_wizard(cantrips=["fire_bolt", "magic_missile"], prepared_spells=["mage_armor"])
    state = battle(srd, [wizard])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "не знает")


def test_a_cantrip_is_cast_without_a_slot_and_must_be_known(srd, make_wizard):
    state = battle(srd, [make_wizard(cantrips=["ray_of_frost"], spell_slots={1: 0, 2: 0})])

    refused_cast(srd, state, "fire_bolt", [GOBLIN], "не знает")
    after, _ = cast(srd, state, [15, 4], "ray_of_frost", [GOBLIN])

    assert after.party[WIZARD].spell_slots == {1: 0, 2: 0}


def test_a_character_without_spellcasting_cannot_cast(srd, make_hero):
    state = battle(srd, [make_hero(prepared_spells=["magic_missile"])])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "не умеет колдовать", caster=HERO)


def test_casting_with_no_slots_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard(spell_slots={1: 0, 2: 0})])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "нет ячейки")


def test_casting_with_no_slot_of_the_requested_level_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard(spell_slots={1: 0, 2: 1})])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "нет ячейки", slot_level=1)


def test_a_slot_below_the_spell_level_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "scorching_ray", [GOBLIN], "ниже уровня", slot_level=1)


def test_a_slot_level_the_character_does_not_have_is_refused(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "нет ячейки", slot_level=5)


def test_without_a_requested_level_the_lowest_available_slot_is_spent(srd, make_wizard):
    state = battle(srd, [make_wizard(spell_slots={1: 0, 2: 2})], ("ogre",))

    after, events = cast(srd, state, [3, 3, 3, 3], "magic_missile", [OGRE])

    # Ячейка 1-го уровня пуста, поэтому ушла ячейка 2-го уровня: дротиков четыре.
    assert after.party[WIZARD].spell_slots == {1: 0, 2: 1}
    assert len(damage_events(events)) == 4


def test_the_slot_is_spent_and_never_goes_negative(srd, make_wizard):
    state = battle(srd, [make_wizard(spell_slots={1: 1, 2: 0})], ("ogre",))

    once, _ = cast(srd, state, [2, 2, 2], "magic_missile", [OGRE])

    assert once.party[WIZARD].spell_slots == {1: 0, 2: 0}
    refused_cast(
        srd, fresh_turn(turn_of(once, WIZARD), WIZARD), "magic_missile", [OGRE], "нет ячейки"
    )


def test_the_casting_does_not_change_the_original_state(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))
    snapshot = state.model_copy(deep=True)

    after, _ = cast(srd, state, [2, 2, 2], "magic_missile", [OGRE])

    assert state == snapshot
    assert after is not state


def test_the_result_state_survives_a_json_round_trip(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))

    after, events = cast(srd, state, [2, 2, 2], "magic_missile", [OGRE])

    assert GameState.model_validate_json(after.model_dump_json()) == after
    assert all(type(e).model_validate_json(e.model_dump_json()) == e for e in events)


def test_casting_outside_combat_is_refused(srd, make_wizard):
    state = state_of(make_wizard())

    refused_cast(srd, state, "magic_missile", [GOBLIN], "бой не идёт")


def test_only_the_character_whose_turn_it_is_can_cast(srd, make_wizard, make_hero):
    state = battle(srd, [make_hero(), make_wizard()])

    refused_cast(srd, state, "magic_missile", [GOBLIN], "не ход")


def test_a_monster_cannot_cast_through_cast_spell(srd, make_wizard):
    state = turn_of(battle(srd, [make_wizard()]), GOBLIN)

    refused_cast(srd, state, "magic_missile", [WIZARD], "монстр", caster=GOBLIN)


def test_a_character_who_cannot_act_cannot_cast(srd, make_wizard):
    stunned = battle(srd, [make_wizard(conditions=[ActiveCondition(id="stunned")])])
    downed = battle(srd, [make_wizard(hp=0)])

    refused_cast(srd, stunned, "magic_missile", [GOBLIN], "недееспособен")
    refused_cast(srd, downed, "magic_missile", [GOBLIN], "0 HP")


def test_a_spent_action_blocks_an_action_spell_but_not_a_bonus_action_spell(
    srd, make_wizard, make_cleric
):
    wizard_state = battle(srd, [make_wizard()])
    flags(wizard_state, WIZARD)["action"] = True
    cleric_state = battle(srd, [make_cleric(hp=5)])
    flags(cleric_state, CLERIC)["action"] = True

    refused_cast(srd, wizard_state, "magic_missile", [GOBLIN], "действие уже потрачено")
    after, _ = cast(srd, cleric_state, [2, 2], "healing_word", [CLERIC], caster=CLERIC)

    assert flags(after, CLERIC)["bonus_action"] is True


def test_an_action_spell_spends_the_action_and_a_bonus_spell_spends_the_bonus_action(
    srd, make_wizard, make_cleric
):
    action_state, _ = cast(
        srd, battle(srd, [make_wizard()], ("ogre",)), [2, 2, 2], "magic_missile", [OGRE]
    )
    bonus_state, _ = cast(
        srd, battle(srd, [make_cleric(hp=5)]), [2, 2], "healing_word", [CLERIC], caster=CLERIC
    )

    assert flags(action_state, WIZARD) == {"action": True}
    assert flags(bonus_state, CLERIC) == {"bonus_action": True}


def test_a_spent_bonus_action_blocks_a_bonus_action_spell(srd, make_cleric):
    state = battle(srd, [make_cleric(hp=5)])
    flags(state, CLERIC)["bonus_action"] = True

    refused_cast(
        srd, state, "healing_word", [CLERIC], "бонусное действие уже потрачено", caster=CLERIC
    )


def test_a_spell_that_takes_minutes_is_unavailable_in_combat(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp=5)])

    refused_cast(srd, state, "prayer_of_healing", [HERO], "в бою недоступно", caster=CLERIC)


# =============================================================================================
# Заклинания: цели и дальность
# =============================================================================================


def test_a_spell_needs_targets_unless_it_is_aimed_at_the_caster(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "magic_missile", [], "укажите цели")
    refused_cast(srd, state, "burning_hands", [], "укажите цели")


def test_unknown_and_dead_targets_are_refused(srd, make_wizard):
    state = battle(srd, [make_wizard()])
    fighter(state, GOBLIN).hp = 0

    refused_cast(srd, state, "magic_missile", ["ghost-1"], "нет бойца")
    refused_cast(srd, state, "magic_missile", [GOBLIN], "мёртв")


def test_a_spell_attack_cannot_target_an_ally(srd, make_wizard, make_hero):
    state = battle(srd, [make_wizard(), make_hero()])

    refused_cast(srd, state, "fire_bolt", [HERO], "союзника")


def test_a_harmful_spell_cannot_target_the_caster(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "burning_hands", [WIZARD], "себя")
    refused_cast(srd, state, "magic_missile", [WIZARD], "себя")


def test_too_many_targets_are_refused(srd, make_cleric, make_hero, make_ally):
    extra = make_ally(id="pc-3", name="Третий")
    fourth = make_ally(id="pc-4", name="Четвёртый")
    state = battle(srd, [make_cleric(), make_hero(), make_ally(), extra, fourth])

    refused_cast(
        srd, state, "bless", [HERO, ALLY, "pc-3", "pc-4"], "слишком много целей", caster=CLERIC
    )


def test_the_same_target_twice_is_refused_for_an_ordinary_spell(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])

    refused_cast(srd, state, "bless", [HERO, HERO], "дважды", caster=CLERIC)


def test_darts_need_one_target_or_one_target_per_dart(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    refused_cast(srd, state, "magic_missile", [GOBLIN, GOBLIN_2], "по одной на")
    refused_cast(srd, state, "magic_missile", [GOBLIN] * 4, "по одной на")
    refused_cast(srd, state, "scorching_ray", [GOBLIN, GOBLIN_2], "по одной на")


def test_a_far_target_is_out_of_reach_of_a_near_spell(srd, make_wizard):
    # Хранилище связано с залом связью «далеко»; Луч холода бьёт на 60 футов.
    state = battle(srd, [make_wizard()], monster_zone="vault")

    refused_cast(srd, state, "ray_of_frost", [GOBLIN], "вне дальности")


def test_a_far_spell_reaches_a_far_target(srd, make_wizard):
    state = battle(srd, [make_wizard()], monster_zone="vault")

    after, events = cast(srd, state, [15, 4], "fire_bolt", [GOBLIN])

    assert hp(after, GOBLIN) == 6
    assert damage_events(events)[0].amount == 4


def test_a_near_spell_reaches_a_target_in_a_linked_near_zone(srd, make_wizard):
    state = battle(srd, [make_wizard()], monster_zone="stairs")

    after, _ = cast(srd, state, [15, 4], "ray_of_frost", [GOBLIN])

    assert hp(after, GOBLIN) == 10 - 4


def test_a_touch_spell_on_an_enemy_needs_melee_engagement(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    refused_cast(srd, state, "shocking_grasp", [GOBLIN], "вне дальности")
    hit, _ = cast(srd, melee(state, WIZARD, GOBLIN), [15, 4], "shocking_grasp", [GOBLIN])

    assert hp(hit, GOBLIN) == 6


def test_a_touch_spell_on_an_ally_needs_the_same_zone(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp=5)])
    fighter(state, HERO).zone_id = "stairs"

    refused_cast(srd, state, "cure_wounds", [HERO], "вне дальности", caster=CLERIC)


def test_a_touch_spell_works_on_an_ally_in_the_same_zone_without_engagement(
    srd, make_cleric, make_hero
):
    state = battle(srd, [make_cleric(), make_hero(hp=5)])

    after, _ = cast(srd, state, [4, 4], "cure_wounds", [HERO], caster=CLERIC)

    assert hp(after, HERO) > 5


def test_a_self_range_area_spell_needs_targets_in_the_casters_zone(srd, make_wizard):
    state = battle(srd, [make_wizard()], monster_zone="stairs")

    refused_cast(srd, state, "burning_hands", [GOBLIN], "вне дальности")


def test_a_ranged_spell_attack_against_an_engaged_enemy_has_disadvantage(srd, make_wizard):
    # Чародей без «Мощного заговора» (2-й уровень): промах ничего не наносит.
    state = melee(battle(srd, [make_wizard(level=2)]), WIZARD, GOBLIN)

    after, events = cast(srd, state, [15, 3], "fire_bolt", [GOBLIN])

    attack_roll = roll_events(events)[0]
    assert (attack_roll.roll.advantage, attack_roll.roll.natural) == (DISADVANTAGE, 3)
    assert attack_roll.success is False
    assert hp(after, GOBLIN) == 10


def test_a_ranged_spell_attack_is_hindered_by_a_capable_enemy_next_to_the_caster(srd, make_wizard):
    two_goblins = battle(srd, [make_wizard(level=2)], ("goblin_warrior", "goblin_warrior"))
    state = melee(two_goblins, WIZARD, GOBLIN)  # второй гоблин с магом не вовлечён

    _, at_free = cast(srd, state, [15, 3], "fire_bolt", [GOBLIN_2])
    fighter(state, GOBLIN).conditions = [ActiveCondition(id="paralyzed")]
    _, at_free_after_paralysis = cast(srd, state, [3], "fire_bolt", [GOBLIN_2])

    assert roll_events(at_free)[0].roll.advantage == DISADVANTAGE
    # Недееспособный противник рядом не мешает.
    assert roll_events(at_free_after_paralysis)[0].roll.advantage == NONE


# =============================================================================================
# Заклинательная атака: Огненный снаряд, Палящий луч, Направленный снаряд
# =============================================================================================


def test_fire_bolt_hits_and_deals_fire_damage_without_a_slot(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [12, 6], "fire_bolt", [GOBLIN])

    attack_roll = roll_events(events)[0]
    assert (attack_roll.label, attack_roll.target_ac, attack_roll.success) == (
        "Атака: Огненный снаряд",
        15,
        True,
    )
    assert (attack_roll.roll.expr, attack_roll.roll.total) == ("1d20+5", 17)
    assert [(d.amount, d.damage_type, d.hp_after) for d in damage_events(events)] == [
        (6, "fire", 4)
    ]
    assert hp(after, GOBLIN) == 4
    assert after.party[WIZARD].spell_slots == {1: 4, 2: 2}


def test_fire_bolt_misses_against_armor_class(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2)])  # без «Мощного заговора»

    after, events = cast(srd, state, [9], "fire_bolt", [GOBLIN])  # 9 + 5 = 14 < 15

    assert roll_events(events)[0].success is False
    assert damage_events(events) == []
    assert hp(after, GOBLIN) == 10


def test_a_natural_one_misses_even_against_a_low_armor_class(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2)], ("ogre",))

    _, events = cast(srd, state, [1], "fire_bolt", [OGRE])

    assert roll_events(events)[0].success is False


def test_a_natural_twenty_hits_and_doubles_the_damage_dice(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [20, 3, 4], "fire_bolt", [GOBLIN])

    assert damage_events(events)[0].amount == 7  # две кости 1к10 вместо одной
    assert "крит" in labels(events)[1]
    assert hp(after, GOBLIN) == 3


def test_potent_cantrip_deals_half_damage_on_a_miss(srd, make_wizard):
    state = battle(srd, [make_wizard()])  # 3-й уровень: Эвокатор

    after, events = cast(srd, state, [5, 7], "fire_bolt", [GOBLIN])

    assert roll_events(events)[0].success is False
    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [(3, "fire")]
    assert "половина" in labels(events)[1]
    assert hp(after, GOBLIN) == 7


def test_potent_cantrip_half_damage_applies_after_a_natural_one_too(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, _ = cast(srd, state, [1, 9], "fire_bolt", [GOBLIN])

    assert hp(after, GOBLIN) == 10 - 4


def test_potent_cantrip_half_of_one_is_nothing(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [5, 1], "fire_bolt", [GOBLIN])

    assert damage_events(events) == []
    assert hp(after, GOBLIN) == 10


def test_potent_cantrip_does_not_reduce_the_damage_of_a_hit(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    _, events = cast(srd, state, [15, 8], "fire_bolt", [GOBLIN])

    assert [d.amount for d in damage_events(events)] == [8]


def test_potent_cantrip_does_not_apply_to_leveled_attack_spells(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))

    _, events = cast(srd, state, [2, 2, 2], "scorching_ray", [OGRE])

    assert damage_events(events) == []


def test_a_spell_attack_adds_the_blessing_die(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2, buffs=[bless()])])

    after, events = cast(srd, state, [10, 3, 6], "fire_bolt", [GOBLIN])

    attack_roll = roll_events(events)[0]
    assert (attack_roll.roll.rolls, attack_roll.roll.total) == ([10, 3], 18)
    assert attack_roll.roll.natural == 10  # крит и промах считаются по к20, а не по сумме
    assert attack_roll.success is True
    assert hp(after, GOBLIN) == 4


def test_halfling_luck_rerolls_a_one_on_a_spell_attack(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2, species="halfling")])

    after, events = cast(srd, state, [1, 14, 4], "fire_bolt", [GOBLIN])

    attack_roll = roll_events(events)[0]
    assert (attack_roll.roll.rolls, attack_roll.roll.natural, attack_roll.success) == (
        [1, 14],
        14,
        True,
    )
    assert hp(after, GOBLIN) == 6


def test_a_spell_attack_is_hindered_by_the_casters_own_conditions(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2, conditions=[ActiveCondition(id="poisoned")])])

    _, events = cast(srd, state, [15, 3], "fire_bolt", [GOBLIN])

    assert roll_events(events)[0].roll.advantage == DISADVANTAGE


def test_scorching_ray_makes_a_separate_attack_for_each_ray(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))

    after, events = cast(srd, state, [10, 3, 4, 12, 5, 6, 2], "scorching_ray", [OGRE])

    assert [r.success for r in roll_events(events) if r.label.startswith("Атака")] == [
        True,
        True,
        False,
    ]
    assert [d.amount for d in damage_events(events)] == [7, 11]
    assert hp(after, OGRE) == 68 - 18
    assert after.party[WIZARD].spell_slots == {1: 4, 2: 1}
    assert flags(after, WIZARD) == {"action": True}


def test_scorching_ray_can_split_the_rays_between_targets(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, events = cast(
        srd, state, [15, 1, 1, 15, 2, 2, 15, 3, 3], "scorching_ray", [GOBLIN, GOBLIN_2, GOBLIN]
    )

    assert [(d.target_id, d.amount) for d in damage_events(events)] == [
        (GOBLIN, 2),
        (GOBLIN_2, 4),
        (GOBLIN, 6),
    ]
    assert (hp(after, GOBLIN), hp(after, GOBLIN_2)) == (2, 6)


def test_a_higher_slot_adds_a_ray_to_scorching_ray(srd, make_wizard):
    wizard = make_wizard(spell_slots_max={1: 4, 2: 2, 3: 1}, spell_slots={1: 4, 2: 2, 3: 1})
    state = battle(srd, [wizard], ("ogre",))

    after, events = cast(srd, state, [2, 2, 2, 2], "scorching_ray", [OGRE], slot_level=3)

    assert len([r for r in roll_events(events) if r.label.startswith("Атака")]) == 4
    assert after.party[WIZARD].spell_slots == {1: 4, 2: 2, 3: 0}


def test_rays_at_a_target_that_already_died_are_skipped(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, events = cast(
        srd, state, [15, 6, 6, 15, 3, 3], "scorching_ray", [GOBLIN, GOBLIN, GOBLIN_2]
    )

    # Первый луч убил первого гоблина; второй луч в него пропущен и кубов не тратит.
    assert (hp(after, GOBLIN), hp(after, GOBLIN_2)) == (0, 4)
    assert len([r for r in roll_events(events) if r.label.startswith("Атака")]) == 2


def test_guiding_bolt_hits_marks_the_target_and_gives_the_next_attack_advantage(
    srd, make_cleric, make_hero
):
    state = battle(srd, [make_cleric(), make_hero()], ("ogre",))

    after, events = cast(srd, state, [10, 4, 4, 4, 4], "guiding_bolt", [OGRE], caster=CLERIC)

    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [(16, "radiant")]
    assert fighter(after, OGRE).conditions == [
        ActiveCondition(id="guiding_bolt", source=CLERIC, rounds_left=1)
    ]
    assert after.party[CLERIC].spell_slots == {1: 3, 2: 2}
    # Следующая атака по огру идёт с преимуществом и тратит метку.
    next_attack = melee(turn_of(after, HERO), HERO, OGRE)
    rng = ScriptedRng([3, 12, 5])
    followed, follow_events = attack(
        next_attack, HERO, OGRE, weapon="longsword", rng=rng, content=srd
    )
    assert roll_events(follow_events)[0].roll.advantage == ADVANTAGE
    assert fighter(followed, OGRE).conditions == []


def test_guiding_bolt_that_misses_leaves_no_mark(srd, make_cleric):
    state = battle(srd, [make_cleric()], ("ogre",))

    after, events = cast(srd, state, [2], "guiding_bolt", [OGRE], caster=CLERIC)

    assert damage_events(events) == []
    assert fighter(after, OGRE).conditions == []
    assert after.party[CLERIC].spell_slots == {1: 3, 2: 2}  # ячейка потрачена и при промахе


def test_a_higher_slot_adds_dice_to_guiding_bolt(srd, make_cleric):
    state = battle(srd, [make_cleric()], ("ogre",))

    _, events = cast(
        srd, state, [10, 1, 1, 1, 1, 1], "guiding_bolt", [OGRE], caster=CLERIC, slot_level=2
    )

    assert roll_events(events)[1].roll.expr == "4d6+1d6"
    assert [d.amount for d in damage_events(events)] == [5]


def test_a_spell_attack_uses_the_casters_spell_attack_bonus(srd, make_cleric, make_wizard):
    # Жрец: Мдр 16 (+3) и бонус мастерства +2 = +5; волшебник с Инт 20 (+5): +7.
    cleric_state = battle(srd, [make_cleric()], ("ogre",))
    wizard_state = battle(srd, [make_wizard(scores={INT: 20})], ("ogre",))

    _, cleric_events = cast(
        srd, cleric_state, [10, 1, 1, 1, 1], "guiding_bolt", [OGRE], caster=CLERIC
    )
    _, wizard_events = cast(srd, wizard_state, [10, 1], "fire_bolt", [OGRE])

    assert roll_events(cleric_events)[0].roll.expr == "1d20+5"
    assert roll_events(wizard_events)[0].roll.expr == "1d20+7"


# =============================================================================================
# Спасбросок цели: Огненные ладони, Волна грома, Священное пламя
# =============================================================================================


def test_burning_hands_deals_full_damage_on_a_failed_save(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [10, 2, 3, 4], "burning_hands", [GOBLIN])

    save_roll = roll_events(events)[0]
    assert (save_roll.label, save_roll.dc, save_roll.success) == (
        "Спасбросок: Огненные ладони",
        13,
        False,
    )
    assert save_roll.roll.total == 12  # к20 + Лов гоблина (+2)
    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [(9, "fire")]
    assert hp(after, GOBLIN) == 1
    assert after.party[WIZARD].spell_slots == {1: 3, 2: 2}


def test_burning_hands_deals_half_damage_on_a_successful_save(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [11, 2, 3, 4], "burning_hands", [GOBLIN])

    assert roll_events(events)[0].success is True
    assert [d.amount for d in damage_events(events)] == [4]  # половина от 9, вниз
    assert hp(after, GOBLIN) == 6


def test_one_damage_roll_is_shared_by_all_targets_of_an_area_spell(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, events = cast(srd, state, [10, 11, 6, 6, 5], "burning_hands", [GOBLIN, GOBLIN_2])

    assert [(d.target_id, d.amount) for d in damage_events(events)] == [(GOBLIN, 17), (GOBLIN_2, 8)]
    assert (hp(after, GOBLIN), hp(after, GOBLIN_2)) == (0, 2)
    assert labels(events).count("Урон: Огненные ладони") == 1


def test_a_higher_slot_adds_dice_to_burning_hands(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    _, events = cast(srd, state, [10, 1, 1, 1, 1], "burning_hands", [GOBLIN], slot_level=2)

    assert roll_events(events)[1].roll.expr == "3d6+1d6"
    assert [d.amount for d in damage_events(events)] == [4]


def test_thunderwave_uses_constitution_saves(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    failed, failed_events = cast(srd, state, [12, 4, 5], "thunderwave", [GOBLIN])
    saved, saved_events = cast(srd, state, [13, 4, 5], "thunderwave", [GOBLIN])

    assert roll_events(failed_events)[0].roll.expr == "1d20+0"  # Тел 10 — без бонуса
    assert [d.amount for d in damage_events(failed_events)] == [9]
    assert [d.amount for d in damage_events(saved_events)] == [4]
    assert (hp(failed, GOBLIN), hp(saved, GOBLIN)) == (1, 6)


def test_a_save_spell_can_hit_an_ally_standing_in_the_area(srd, make_wizard, make_hero):
    state = battle(srd, [make_wizard(), make_hero()])

    after, events = cast(srd, state, [10, 12, 4, 5], "thunderwave", [GOBLIN, HERO])

    assert [d.target_id for d in damage_events(events)] == [GOBLIN, HERO]
    assert hp(after, HERO) == 20 - 9


def test_a_cantrip_save_that_succeeds_deals_nothing_without_the_evoker_feature(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    after, events = cast(srd, state, [15], "sacred_flame", [GOBLIN], caster=CLERIC)

    assert roll_events(events)[0].success is True
    assert damage_events(events) == []  # кубы урона даже не брошены
    assert hp(after, GOBLIN) == 10


def test_a_failed_cantrip_save_deals_full_damage_and_costs_no_slot(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    after, events = cast(srd, state, [5, 6], "sacred_flame", [GOBLIN], caster=CLERIC)

    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [(6, "radiant")]
    assert after.party[CLERIC].spell_slots == {1: 4, 2: 2}


def test_potent_cantrip_deals_half_damage_on_a_successful_save(srd, make_wizard):
    state = battle(srd, [make_wizard()])  # Эвокатор 3-го уровня со Священным пламенем

    after, events = cast(srd, state, [15, 7], "sacred_flame", [GOBLIN])

    assert [d.amount for d in damage_events(events)] == [3]
    assert hp(after, GOBLIN) == 7


def test_potent_cantrip_does_nothing_for_a_wizard_below_the_feature_level(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2)])

    _, events = cast(srd, state, [15], "sacred_flame", [GOBLIN])

    assert damage_events(events) == []


def test_a_paralyzed_target_fails_a_dexterity_save_without_rolling(srd, make_wizard):
    state = battle(srd, [make_wizard()])
    fighter(state, GOBLIN).conditions = [ActiveCondition(id="paralyzed")]

    after, events = cast(srd, state, [2, 3, 4], "burning_hands", [GOBLIN])

    save_roll = roll_events(events)[0]
    assert (save_roll.roll.expr, save_roll.roll.rolls, save_roll.success) == (
        "auto_fail",
        [],
        False,
    )
    assert hp(after, GOBLIN) == 1


def test_a_restrained_target_saves_dexterity_with_disadvantage(srd, make_wizard):
    state = battle(srd, [make_wizard()])
    fighter(state, GOBLIN).conditions = [ActiveCondition(id="restrained")]

    _, events = cast(srd, state, [15, 3, 1, 1, 1], "burning_hands", [GOBLIN])

    save_roll = roll_events(events)[0]
    assert (save_roll.roll.advantage, save_roll.roll.natural, save_roll.success) == (
        DISADVANTAGE,
        3,
        False,
    )


def test_a_character_target_saves_with_its_own_bonuses(srd, make_wizard, make_hero):
    hero = make_hero(scores={STR: 16, DEX: 14}, save_proficiencies=[DEX])
    state = turn_of(battle(srd, [make_wizard(), hero]), WIZARD)  # Ловкость героя обгоняет мага

    _, events = cast(srd, state, [9, 1, 1, 1], "burning_hands", [HERO])

    save_roll = roll_events(events)[0]
    assert (save_roll.roll.expr, save_roll.roll.total, save_roll.success) == ("1d20+4", 13, True)


def test_a_save_damage_roll_drops_a_monster_to_zero_and_removes_it_from_play(srd, make_wizard):
    state = battle(srd, [make_wizard()])
    fighter(state, GOBLIN).hp = 2

    after, _ = cast(srd, state, [10, 6, 6, 6], "burning_hands", [GOBLIN])

    assert hp(after, GOBLIN) == 0


# =============================================================================================
# Урон без броска: Волшебная стрела
# =============================================================================================


def test_magic_missile_deals_force_damage_without_an_attack_roll(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))

    after, events = cast(srd, state, [3, 2, 4], "magic_missile", [OGRE])

    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [
        (4, "force"),
        (3, "force"),
        (5, "force"),
    ]
    assert not any(label.startswith("Атака") for label in labels(events))
    assert [r.roll.expr for r in roll_events(events)] == ["1d4+1"] * 3
    assert hp(after, OGRE) == 68 - 12
    assert after.party[WIZARD].spell_slots == {1: 3, 2: 2}


def test_darts_can_be_split_between_targets(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, events = cast(srd, state, [4, 4, 1], "magic_missile", [GOBLIN, GOBLIN, GOBLIN_2])

    assert [(d.target_id, d.amount) for d in damage_events(events)] == [
        (GOBLIN, 5),
        (GOBLIN, 5),
        (GOBLIN_2, 2),
    ]
    assert (hp(after, GOBLIN), hp(after, GOBLIN_2)) == (0, 8)


def test_a_higher_slot_adds_a_dart(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("ogre",))

    after, events = cast(srd, state, [1, 1, 1, 1], "magic_missile", [OGRE], slot_level=2)

    assert [d.amount for d in damage_events(events)] == [2, 2, 2, 2]
    assert after.party[WIZARD].spell_slots == {1: 4, 2: 1}


def test_darts_at_a_target_that_already_died_are_skipped(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [4, 4], "magic_missile", [GOBLIN])

    assert [d.amount for d in damage_events(events)] == [5, 5]  # третий дротик не брошен
    assert hp(after, GOBLIN) == 0


def test_spell_damage_is_checked_against_resistances(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("shadow",))  # тень сопротивляется огню

    after, events = cast(srd, state, [15, 8], "fire_bolt", ["shadow-1"])

    assert [d.amount for d in damage_events(events)] == [4]
    assert hp(after, "shadow-1") == 27 - 4


# =============================================================================================
# Лечение: Лечащее слово, Лечение ран, Ученик жизни
# =============================================================================================


def test_healing_word_adds_the_casting_modifier_and_disciple_of_life(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp_max=40, hp=5)])

    after, events = cast(srd, state, [3, 2], "healing_word", [HERO], caster=CLERIC)

    # 2к4 = 5, плюс Мдр +3, плюс Ученик жизни: 2 + уровень ячейки (1) = 3.
    heal_roll = roll_events(events)[0]
    assert (heal_roll.roll.expr, heal_roll.roll.total) == ("2d4+6", 11)
    assert [(h.target_id, h.amount, h.hp_after) for h in events if isinstance(h, HealEvent)] == [
        (HERO, 11, 16)
    ]
    assert hp(after, HERO) == 16
    assert after.party[CLERIC].spell_slots == {1: 3, 2: 2}
    assert flags(after, CLERIC) == {"bonus_action": True}


def test_disciple_of_life_grows_with_the_slot_level(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp_max=60, hp=5)])

    after, events = cast(
        srd, state, [4, 4, 4, 4], "cure_wounds", [HERO], caster=CLERIC, slot_level=2
    )

    # 2к8 и ещё 2к8 за ячейку 2-го уровня, Мдр +3, Ученик жизни 2 + 2.
    assert roll_events(events)[0].roll.expr == "2d8+2d8+7"
    assert hp(after, HERO) == 5 + 16 + 7


def test_a_healer_without_disciple_of_life_adds_only_the_modifier(srd, make_ranger, make_hero):
    state = battle(srd, [make_ranger(), make_hero(hp_max=40, hp=5)])

    after, events = cast(srd, state, [4, 4], "cure_wounds", [HERO], caster=RANGER)

    assert roll_events(events)[0].roll.expr == "2d8+2"  # Мдр 14 даёт +2
    assert hp(after, HERO) == 5 + 8 + 2


def test_healing_stops_at_the_hit_point_maximum(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp_max=20, hp=18)])

    after, events = cast(srd, state, [4, 4], "healing_word", [HERO], caster=CLERIC)

    assert hp(after, HERO) == 20
    assert [h.amount for h in events if isinstance(h, HealEvent)] == [2]


def test_healing_wakes_up_an_unconscious_ally(srd, make_cleric, make_hero):
    downed = make_hero(
        hp=0,
        conditions=[ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)],
        death_saves=DeathSaves(failures=2),
    )
    state = battle(srd, [make_cleric(), downed])

    after, events = cast(srd, state, [1, 1], "healing_word", [HERO], caster=CLERIC)

    assert hp(after, HERO) == 1 + 1 + 3 + 3
    assert after.party[HERO].conditions == []
    assert after.party[HERO].death_saves == DeathSaves()
    assert ConditionEvent(target_id=HERO, condition="unconscious", added=False) in events


def test_a_dead_character_cannot_be_healed(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])
    state.party[HERO].hp = 0
    state.party[HERO].death_saves = DeathSaves(failures=3)

    refused_cast(srd, state, "healing_word", [HERO], "мёртв", caster=CLERIC)


# =============================================================================================
# Эффекты на персонажах: Благословение, Доспехи мага, Указание, Подмога
# =============================================================================================


def fresh_turn(state: GameState, fighter_id: str) -> GameState:
    """Расстановка для теста: у бойца снова целый ход (флаги сброшены)."""
    fighter(state, fighter_id).turn_flags = {}
    return state


def test_bless_puts_a_timed_effect_on_each_target_and_starts_concentration(
    srd, make_cleric, make_hero, make_ally
):
    state = battle(srd, [make_cleric(), make_hero(), make_ally()])

    after, events = cast(srd, state, [], "bless", [CLERIC, HERO, ALLY], caster=CLERIC)

    expected = [ActiveBuff(id="bless", rounds_left=10, source_id=CLERIC)]
    assert [after.party[x].buffs for x in (CLERIC, HERO, ALLY)] == [expected] * 3
    assert after.party[CLERIC].concentration == "bless"
    assert after.party[CLERIC].spell_slots == {1: 3, 2: 2}
    assert [(e.target_id, e.condition, e.added) for e in condition_events(events)] == [
        (x, "bless", True) for x in (CLERIC, HERO, ALLY)
    ]


def test_blessing_adds_the_die_to_the_weapon_attack_of_a_blessed_character(
    srd, make_cleric, make_hero
):
    state = battle(srd, [make_cleric(), make_hero()])
    blessed, _ = cast(srd, state, [], "bless", [HERO], caster=CLERIC)

    rng = ScriptedRng([10, 3, 5])  # к20, кость благословения, урон
    after, events = attack(
        melee(turn_of(blessed, HERO), HERO, GOBLIN),
        HERO,
        GOBLIN,
        weapon="longsword",
        rng=rng,
        content=srd,
    )

    attack_roll = roll_events(events)[0]
    assert (attack_roll.roll.rolls, attack_roll.roll.total, attack_roll.success) == (
        [10, 3],
        18,
        True,
    )
    assert hp(after, GOBLIN) == 10 - 8


def test_bless_cannot_be_put_on_an_enemy(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    refused_cast(srd, state, "bless", [GOBLIN], "только на персонажа", caster=CLERIC)


def test_a_new_concentration_spell_ends_the_previous_one(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])
    blessed, _ = cast(srd, state, [], "bless", [HERO], caster=CLERIC)

    after, events = cast(
        srd, fresh_turn(blessed, CLERIC), [20], "hold_person", [GOBLIN], caster=CLERIC
    )

    assert after.party[HERO].buffs == []
    assert after.party[CLERIC].concentration == "hold_person"
    assert ConditionEvent(target_id=HERO, condition="bless", added=False) in events


def test_a_spell_without_concentration_keeps_the_current_concentration(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp=5)])
    blessed, _ = cast(srd, state, [], "bless", [HERO], caster=CLERIC)

    after, _ = cast(srd, fresh_turn(blessed, CLERIC), [1, 1], "cure_wounds", [HERO], caster=CLERIC)

    assert after.party[CLERIC].concentration == "bless"
    assert [b.id for b in after.party[HERO].buffs] == ["bless"]


def test_recasting_a_spell_refreshes_it_instead_of_stacking(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])
    first, _ = cast(srd, state, [], "bless", [HERO], caster=CLERIC)
    first.party[HERO].buffs[0].rounds_left = 3

    again, _ = cast(srd, fresh_turn(first, CLERIC), [], "bless", [HERO], caster=CLERIC)

    assert again.party[HERO].buffs == [ActiveBuff(id="bless", rounds_left=10, source_id=CLERIC)]
    assert again.party[CLERIC].spell_slots == {1: 2, 2: 2}


def test_mage_armor_raises_the_armor_class_of_an_unarmored_target(srd, make_wizard, make_hero):
    hero = make_hero(scores={STR: 16, DEX: 14})
    state = turn_of(battle(srd, [make_wizard(), hero]), WIZARD)
    before = armor_class(state.party[HERO], srd)

    after, _ = cast(srd, state, [], "mage_armor", [HERO])

    assert (before, armor_class(after.party[HERO], srd)) == (12, 15)
    assert after.party[HERO].buffs == [
        ActiveBuff(id="mage_armor", rounds_left=4800, source_id=WIZARD)
    ]
    assert after.party[WIZARD].concentration is None


def test_guidance_is_a_cantrip_buff_with_concentration(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])

    after, _ = cast(srd, state, [], "guidance", [HERO], caster=CLERIC)

    assert after.party[HERO].buffs == [ActiveBuff(id="guidance", rounds_left=10, source_id=CLERIC)]
    assert after.party[CLERIC].concentration == "guidance"
    assert after.party[CLERIC].spell_slots == {1: 4, 2: 2}


def test_aid_raises_maximum_and_current_hit_points_and_remembers_the_bonus(
    srd, make_cleric, make_hero, make_ally
):
    state = battle(srd, [make_cleric(), make_hero(hp=12), make_ally()])

    after, events = cast(srd, state, [], "aid", [HERO, ALLY], caster=CLERIC)

    assert (after.party[HERO].hp_max, after.party[HERO].hp) == (25, 17)
    assert (after.party[ALLY].hp_max, after.party[ALLY].hp) == (25, 25)
    assert after.party[HERO].buffs == [
        ActiveBuff(id="aid", rounds_left=4800, source_id=CLERIC, data={"hp": 5})
    ]
    assert after.party[CLERIC].concentration is None
    assert after.party[CLERIC].spell_slots == {1: 4, 2: 1}
    assert [h.amount for h in events if isinstance(h, HealEvent)] == [5, 5]


def test_a_higher_slot_adds_more_hit_points_to_aid(srd, make_cleric, make_hero):
    cleric = make_cleric(spell_slots_max={1: 4, 2: 2, 3: 1}, spell_slots={1: 4, 2: 2, 3: 1})
    state = battle(srd, [cleric, make_hero()])

    after, _ = cast(srd, state, [], "aid", [HERO], caster=CLERIC, slot_level=3)

    assert (after.party[HERO].hp_max, after.party[HERO].buffs[0].data) == (30, {"hp": 10})


def two_clerics_and_a_hero(srd, make_cleric, make_hero):
    """Два жреца с ячейкой 3-го уровня и герой с полными 20 HP."""
    slots = {"spell_slots_max": {1: 4, 2: 2, 3: 1}, "spell_slots": {1: 4, 2: 2, 3: 1}}
    second = make_cleric(id="clr-2", name="Второй жрец", **slots)
    return battle(srd, [make_cleric(**slots), second, make_hero()])


def aids_on(character: Character) -> list[tuple[str, int]]:
    return [(b.source_id, b.data["hp"]) for b in character.buffs if b.id == "aid"]


@pytest.mark.parametrize(
    ("first_slot", "second_slot", "hp_max", "left"),
    [
        (2, 2, 25, [("clr-2", 5)]),  # равные по силе не складываются; действует свежая
        (3, 2, 30, [(CLERIC, 10)]),  # слабая после сильной ничего не меняет
        (2, 3, 30, [("clr-2", 10)]),  # сильная после слабой заменяет её, а не прибавляется
    ],
)
def test_aid_from_a_second_caster_does_not_add_to_the_aid_already_on_the_target(
    srd, make_cleric, make_hero, first_slot, second_slot, hp_max, left
):
    state = two_clerics_and_a_hero(srd, make_cleric, make_hero)
    first_aid, _ = cast(srd, state, [], "aid", [HERO], caster=CLERIC, slot_level=first_slot)

    twice, _ = cast(
        srd, turn_of(first_aid, "clr-2"), [], "aid", [HERO], caster="clr-2", slot_level=second_slot
    )

    hero = twice.party[HERO]
    assert (hero.hp_max, hero.hp, aids_on(hero)) == (hp_max, hp_max, left)
    end_buffs(hero, lambda buff: buff.id == "aid")
    assert (hero.hp_max, hero.hp) == (20, 20)  # никакой лишней прибавки не осталось


def test_a_weaker_aid_leaves_the_target_untouched_and_is_reported_as_an_action(
    srd, make_cleric, make_hero
):
    state = two_clerics_and_a_hero(srd, make_cleric, make_hero)
    state.party[HERO].hp = 12
    strong, _ = cast(srd, state, [], "aid", [HERO], caster=CLERIC, slot_level=3)
    before = strong.party[HERO].model_copy(deep=True)

    after, events = cast(srd, turn_of(strong, "clr-2"), [], "aid", [HERO], caster="clr-2")

    assert after.party[HERO] == before
    assert after.party["clr-2"].spell_slots == {1: 4, 2: 1, 3: 1}  # ячейка потрачена всё равно
    assert events == [
        ActionEvent(actor_id="clr-2", action="aid", target_ids=[HERO], label="Подмога")
    ]


def test_the_same_caster_does_not_weaken_a_stronger_aid_by_casting_a_weaker_one(
    srd, make_cleric, make_hero
):
    cleric = make_cleric(spell_slots_max={1: 4, 2: 2, 3: 1}, spell_slots={1: 4, 2: 2, 3: 1})
    strong, _ = cast(
        srd, battle(srd, [cleric, make_hero()]), [], "aid", [HERO], caster=CLERIC, slot_level=3
    )

    again, _ = cast(srd, fresh_turn(strong, CLERIC), [], "aid", [HERO], caster=CLERIC)

    assert aids_on(again.party[HERO]) == [(CLERIC, 10)]
    assert (again.party[HERO].hp_max, again.party[HERO].hp) == (30, 30)


def test_aid_is_taken_back_when_the_buff_ends(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero()])
    aided, _ = cast(srd, state, [], "aid", [HERO], caster=CLERIC)

    hero = aided.party[HERO]
    end_buffs(hero, lambda buff: buff.id == "aid")

    assert (hero.hp_max, hero.hp) == (20, 20)


# =============================================================================================
# Метка охотника и Подсказка: эффекты на врагах и бесплатные сотворения
# =============================================================================================


def test_hunters_mark_marks_an_enemy_with_the_casters_effect(srd, make_ranger):
    state = battle(srd, [make_ranger()])

    after, events = cast(srd, state, [], "hunters_mark", [GOBLIN], caster=RANGER)

    assert fighter(after, GOBLIN).conditions == [
        ActiveCondition(id="hunters_mark", source=RANGER, rounds_left=600)
    ]
    assert after.party[RANGER].concentration == "hunters_mark"
    assert flags(after, RANGER) == {"bonus_action": True}
    assert condition_events(events)[0].target_id == GOBLIN


def test_hunters_mark_adds_a_d6_to_every_hit_of_the_marker(srd, make_ranger):
    state = battle(srd, [make_ranger()])
    marked, _ = cast(srd, state, [], "hunters_mark", [GOBLIN], caster=RANGER)

    rng = ScriptedRng([15, 4, 3])
    after, events = attack(marked, RANGER, GOBLIN, weapon="longbow", rng=rng, content=srd)

    assert [(d.amount, d.damage_type) for d in damage_events(events)] == [
        (7, "piercing"),
        (3, "force"),
    ]
    assert hp(after, GOBLIN) == 0


def test_hunters_mark_is_cast_for_free_with_the_favored_enemy_resource(srd, make_ranger):
    state = battle(srd, [make_ranger()])

    after, _ = cast(srd, state, [], "hunters_mark", [GOBLIN], caster=RANGER)

    assert after.party[RANGER].spell_slots == {1: 3}
    assert after.party[RANGER].resources["hunters_mark"] == 1


def test_hunters_mark_uses_a_slot_when_one_is_requested_or_the_resource_is_spent(srd, make_ranger):
    by_choice, _ = cast(
        srd, battle(srd, [make_ranger()]), [], "hunters_mark", [GOBLIN], caster=RANGER, slot_level=1
    )
    exhausted, _ = cast(
        srd,
        battle(srd, [make_ranger(resources={"hunters_mark": 0})]),
        [],
        "hunters_mark",
        [GOBLIN],
        caster=RANGER,
    )

    assert by_choice.party[RANGER].spell_slots == {1: 2}
    assert by_choice.party[RANGER].resources["hunters_mark"] == 2
    assert exhausted.party[RANGER].spell_slots == {1: 2}
    assert exhausted.party[RANGER].resources["hunters_mark"] == 0


def test_hunters_mark_cannot_mark_an_ally(srd, make_ranger, make_hero):
    state = battle(srd, [make_ranger(), make_hero()])

    refused_cast(srd, state, "hunters_mark", [HERO], "противник", caster=RANGER)


# =============================================================================================
# Состояния от заклинаний: Удержание личности, Усыпление
# =============================================================================================


def test_hold_person_paralyzes_on_a_failed_save_with_a_repeat_save_at_turn_end(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    after, events = cast(srd, state, [10], "hold_person", [GOBLIN], caster=CLERIC)

    save_roll = roll_events(events)[0]
    assert (save_roll.dc, save_roll.success) == (13, False)  # 10 − 1 (Мдр 8) = 9
    assert fighter(after, GOBLIN).conditions == [
        ActiveCondition(id="paralyzed", rounds_left=10, source=CLERIC, save=WIS, dc=13)
    ]
    assert after.party[CLERIC].concentration == "hold_person"
    assert after.party[CLERIC].spell_slots == {1: 4, 2: 1}
    assert ConditionEvent(target_id=GOBLIN, condition="paralyzed", added=True) in events


def test_hold_person_has_no_effect_on_a_successful_save_but_still_costs_the_slot(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    after, events = cast(srd, state, [15], "hold_person", [GOBLIN], caster=CLERIC)

    assert fighter(after, GOBLIN).conditions == []
    assert after.party[CLERIC].concentration == "hold_person"
    assert after.party[CLERIC].spell_slots == {1: 4, 2: 1}
    assert condition_events(events) == []


def test_a_held_monster_repeats_the_save_when_its_turn_ends(srd, make_cleric):
    held, _ = cast(srd, battle(srd, [make_cleric()]), [10], "hold_person", [GOBLIN], caster=CLERIC)
    on_its_turn = turn_of(held, GOBLIN)

    freed, free_events = end_turn(on_its_turn, rng=ScriptedRng([15]), content=srd)
    still, still_events = end_turn(on_its_turn, rng=ScriptedRng([5]), content=srd)

    assert fighter(freed, GOBLIN).conditions == []
    assert ConditionEvent(target_id=GOBLIN, condition="paralyzed", added=False) in free_events
    assert [c.id for c in fighter(still, GOBLIN).conditions] == ["paralyzed"]
    assert fighter(still, GOBLIN).conditions[0].rounds_left == 9
    assert roll_events(still_events)[0].success is False


def test_a_paralyzed_monster_is_hit_with_advantage_and_critically_up_close(
    srd, make_cleric, make_hero
):
    held, _ = cast(
        srd, battle(srd, [make_cleric(), make_hero()]), [10], "hold_person", [GOBLIN], caster=CLERIC
    )

    after, events = attack(
        melee(turn_of(held, HERO), HERO, GOBLIN),
        HERO,
        GOBLIN,
        weapon="longsword",
        rng=ScriptedRng([4, 12, 2, 3]),
        content=srd,
    )

    assert roll_events(events)[0].roll.advantage == ADVANTAGE
    assert [d.amount for d in damage_events(events)] == [8]  # крит: две кости 1к8 и +3
    assert hp(after, GOBLIN) == 2


def test_hold_person_is_refused_for_a_target_immune_to_paralysis_without_spending_the_slot(
    srd, make_cleric
):
    state = battle(srd, [make_cleric()], ("shadow",))

    refused_cast(srd, state, "hold_person", ["shadow-1"], "невосприимчив", caster=CLERIC)


def test_sleep_makes_each_failed_target_incapacitated(srd, make_wizard):
    state = battle(srd, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, _ = cast(srd, state, [10, 15], "sleep", [GOBLIN, GOBLIN_2])

    assert [c.id for c in fighter(after, GOBLIN).conditions] == ["incapacitated"]
    assert fighter(after, GOBLIN_2).conditions == []
    assert after.party[WIZARD].concentration == "sleep"


# =============================================================================================
# Заклинания без полей эффекта: только событие для рассказа
# =============================================================================================


def test_a_utility_spell_only_reports_an_event(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, events = cast(srd, state, [], "fog_cloud", [])

    # В состоянии записи нет, поэтому событие действия, а не состояния: парного снятия не будет.
    assert events == [
        ActionEvent(
            actor_id=WIZARD, action="fog_cloud", target_ids=[WIZARD], label="Туманное облако"
        )
    ]
    assert after.party[WIZARD].concentration == "fog_cloud"
    assert after.party[WIZARD].spell_slots == {1: 3, 2: 2}
    assert fighter(after, GOBLIN) == fighter(state, GOBLIN)


def test_a_utility_cantrip_costs_nothing(srd, make_wizard):
    state = battle(srd, [make_wizard(cantrips=["light"])])

    after, events = cast(srd, state, [], "light", [WIZARD])

    assert after.party[WIZARD].spell_slots == {1: 4, 2: 2}
    assert after.party[WIZARD].concentration is None
    assert flags(after, WIZARD) == {"action": True}
    assert [type(e) for e in events] == [ActionEvent]


def test_misty_step_spends_the_bonus_action(srd, make_wizard):
    state = battle(srd, [make_wizard()])

    after, _ = cast(srd, state, [], "misty_step", [])

    assert flags(after, WIZARD) == {"bonus_action": True}


# =============================================================================================
# Простые действия: общие проверки
# =============================================================================================


def test_an_unknown_action_is_refused(srd, make_hero):
    state = battle(srd, [make_hero()])

    refused_act(srd, state, "fly", "неизвестное действие")


def test_simple_actions_work_only_in_combat_and_in_the_actors_turn(srd, make_hero, make_ally):
    refused_act(srd, state_of(make_hero()), "dodge", "бой не идёт")
    refused_act(srd, battle(srd, [make_hero(), make_ally()]), "dodge", "не ход", actor=ALLY)


def test_a_downed_or_incapacitated_character_cannot_use_actions(srd, make_hero):
    downed = battle(srd, [make_hero(hp=0)])
    stunned = battle(srd, [make_hero(conditions=[ActiveCondition(id="stunned")])])

    refused_act(srd, downed, "dodge", "0 HP")
    refused_act(srd, stunned, "dodge", "недееспособен")


def test_an_action_that_was_already_spent_is_refused(srd, make_hero):
    state = battle(srd, [make_hero()])
    flags(state, HERO)["action"] = True

    refused_act(srd, state, "dodge", "действие уже потрачено")


def test_a_simple_action_does_not_change_the_original_state(srd, make_hero):
    state = battle(srd, [make_hero()])
    snapshot = state.model_copy(deep=True)

    after, _ = act(srd, state, [], "dodge")

    assert state == snapshot
    assert GameState.model_validate_json(after.model_dump_json()) == after


def test_a_monster_can_dodge_dash_and_disengage_but_not_use_character_actions(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()]), GOBLIN)

    dodged, _ = act(srd, state, [], "dodge", actor=GOBLIN)
    refused_act(srd, state, "rage", "монстр", actor=GOBLIN)

    assert fighter(dodged, GOBLIN).dodging is True


# =============================================================================================
# Простые действия: Уклонение, Отход, Рывок, Подготовка
# =============================================================================================


def test_dodge_gives_attackers_disadvantage_until_the_next_turn(srd, make_hero):
    state = battle(srd, [make_hero()])

    after, events = act(srd, state, [], "dodge")

    assert fighter(after, HERO).dodging is True
    assert flags(after, HERO) == {"action": True}
    assert events
    strike = melee(turn_of(after, GOBLIN), HERO, GOBLIN)
    _, attack_events = attack(
        strike, GOBLIN, HERO, action_id="scimitar", rng=ScriptedRng([15, 3, 1]), content=srd
    )
    assert roll_events(attack_events)[0].roll.advantage == DISADVANTAGE


def test_dodge_ends_when_the_characters_next_turn_starts(srd, make_hero):
    dodged, _ = act(srd, battle(srd, [make_hero()]), [], "dodge")

    on_goblin, _ = end_turn(dodged, rng=ScriptedRng([]), content=srd)
    back, _ = end_turn(on_goblin, rng=ScriptedRng([]), content=srd)

    assert current_actor(back).id == HERO
    assert fighter(back, HERO).dodging is False


def test_disengage_lets_the_character_leave_melee_without_provoking(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    disengaged, _ = act(srd, state, [], "disengage")
    after, events = move(disengaged, HERO, to_zone="stairs", rng=ScriptedRng([]), content=srd)

    assert fighter(disengaged, HERO).disengaged is True
    assert flags(disengaged, HERO) == {"action": True}
    assert events == []  # атаки по возможности нет
    assert fighter(after, HERO).zone_id == "stairs"


def test_dash_marks_the_extra_move_and_spends_the_action(srd, make_hero):
    state = battle(srd, [make_hero()])

    dashed, _ = act(srd, state, [], "dash")
    first, _ = move(dashed, HERO, to_zone="stairs", rng=ScriptedRng([]), content=srd)
    second, _ = move(first, HERO, to_zone="hall", rng=ScriptedRng([]), content=srd)

    assert flags(dashed, HERO) == {"action": True, "dashed": True}
    assert fighter(second, HERO).zone_id == "hall"  # два перемещения за ход


def test_dash_twice_is_refused_because_the_action_is_spent(srd, make_hero):
    dashed, _ = act(srd, battle(srd, [make_hero()]), [], "dash")

    refused_act(srd, dashed, "dash", "действие уже потрачено")


def test_ready_spends_the_action_and_remembers_the_readied_action(srd, make_hero):
    after, _ = act(srd, battle(srd, [make_hero()]), [], "ready")

    assert flags(after, HERO) == {"action": True, "readied": True}
    refused_act(srd, after, "dodge", "действие уже потрачено")


@pytest.mark.parametrize(
    "action",
    ["dodge", "disengage", "dash", "ready", "action_surge", "reckless", "steady_aim"],
)
def test_an_action_that_leaves_no_record_in_the_state_is_reported_as_an_action_event(
    srd, make_hero, action
):
    special = {"reckless": make_barbarian, "steady_aim": make_rogue}
    hero = special[action](make_hero) if action in special else make_hero()
    state = battle(srd, [hero])
    if action == "action_surge":
        flags(state, HERO)["action"] = True  # Всплеск возвращает уже потраченное действие

    _, events = act(srd, state, [], action)

    # Парного снятия у таких действий нет, поэтому ConditionEvent потребитель считал бы вечным.
    assert events == [ActionEvent(actor_id=HERO, action=action)]


# =============================================================================================
# Помощь и Спрятаться
# =============================================================================================


def test_help_marks_an_adjacent_enemy_so_that_an_ally_attacks_it_with_advantage(
    srd, make_hero, make_ally
):
    state = melee(battle(srd, [make_hero(), make_ally()]), HERO, GOBLIN)

    helped, events = act(srd, state, [], "help", target_id=GOBLIN)

    assert fighter(helped, GOBLIN).conditions == [ActiveCondition(id="helped", source=HERO)]
    assert flags(helped, HERO) == {"action": True}
    assert ConditionEvent(target_id=GOBLIN, condition="helped", added=True) in events
    follow = melee(turn_of(helped, ALLY), ALLY, GOBLIN)
    after, attack_events = attack(
        follow, ALLY, GOBLIN, weapon="longsword", rng=ScriptedRng([3, 12, 5]), content=srd
    )
    assert roll_events(attack_events)[0].roll.advantage == ADVANTAGE
    assert fighter(after, GOBLIN).conditions == []  # помощь потрачена первой атакой


def test_help_does_not_help_the_enemy_side(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    helped, _ = act(srd, state, [], "help", target_id=GOBLIN)

    # Метка висит на гоблине: удар гоблина по герою она не усиливает.
    strike = turn_of(helped, GOBLIN)
    _, events = attack(
        strike, GOBLIN, HERO, action_id="scimitar", rng=ScriptedRng([15, 1]), content=srd
    )

    assert roll_events(events)[0].roll.advantage == NONE


def test_help_ends_when_the_helpers_next_turn_starts(srd, make_hero, make_ally):
    state = melee(battle(srd, [make_hero(), make_ally()]), HERO, GOBLIN)
    state, _ = act(srd, state, [], "help", target_id=GOBLIN)

    for _ in range(2):  # ход союзника и ход гоблина
        state, _ = end_turn(state, rng=ScriptedRng([]), content=srd)
        assert fighter(state, GOBLIN).conditions == [ActiveCondition(id="helped", source=HERO)]
    state, events = end_turn(state, rng=ScriptedRng([]), content=srd)  # снова ход героя

    assert current_actor(state).id == HERO
    assert fighter(state, GOBLIN).conditions == []
    assert ConditionEvent(target_id=GOBLIN, condition="helped", added=False) in events


def test_help_needs_an_enemy_next_to_the_helper(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), make_ally()])

    refused_act(srd, state, "help", "вплотную", target_id=GOBLIN)
    refused_act(srd, state, "help", "противник", target_id=ALLY)
    refused_act(srd, state, "help", "противник", target_id=HERO)
    refused_act(srd, state, "help", "укажите цель")


def test_hide_succeeds_with_a_stealth_check_and_makes_the_character_unseen(srd, make_hero):
    hero = make_hero(scores={STR: 16, DEX: 14}, skill_proficiencies=[Skill.STEALTH])
    state = battle(srd, [hero])

    after, events = act(srd, state, [12], "hide")

    check_roll = roll_events(events)[0]
    assert (check_roll.dc, check_roll.roll.total, check_roll.success) == (15, 16, True)
    assert after.party[HERO].conditions == [ActiveCondition(id="invisible", source="hide")]
    assert flags(after, HERO) == {"action": True}


def test_hide_fails_with_a_low_stealth_roll_but_still_costs_the_action(srd, make_hero):
    state = battle(srd, [make_hero()])

    after, events = act(srd, state, [5], "hide")

    assert roll_events(events)[0].success is False
    assert after.party[HERO].conditions == []
    assert flags(after, HERO) == {"action": True}


def test_guidance_adds_its_die_to_the_stealth_check_of_hiding(srd, make_hero):
    guided = make_hero(
        scores={STR: 16, DEX: 14},
        skill_proficiencies=[Skill.STEALTH],
        buffs=[ActiveBuff(id="guidance", rounds_left=10, source_id=CLERIC)],
    )
    state = battle(srd, [guided])

    # Лов +2 и мастерство +2: 9 + 4 = 13 не хватает до Сл 15, а кость Указания (3) хватает.
    after, events = act(srd, state, [9, 3], "hide")

    check_roll = roll_events(events)[0]
    assert (check_roll.roll.total, check_roll.success) == (16, True)
    assert after.party[HERO].conditions == [ActiveCondition(id="invisible", source="hide")]


def test_an_attack_ends_the_hiding_after_giving_advantage(srd, make_hero):
    hidden = make_hero(conditions=[ActiveCondition(id="invisible", source="hide")])
    state = melee(battle(srd, [hidden]), HERO, GOBLIN)

    after, events = attack(
        state, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([3, 12, 5]), content=srd
    )

    assert roll_events(events)[0].roll.advantage == ADVANTAGE
    assert after.party[HERO].conditions == []
    assert ConditionEvent(target_id=HERO, condition="invisible", added=False) in events


def test_a_spell_also_ends_the_hiding(srd, make_wizard):
    hidden = make_wizard(conditions=[ActiveCondition(id="invisible", source="hide")])
    state = battle(srd, [hidden])

    after, _ = cast(srd, state, [], "mage_armor", [WIZARD])

    assert after.party[WIZARD].conditions == []


def test_invisibility_from_other_sources_is_not_ended_by_an_attack(srd, make_hero):
    invisible = make_hero(conditions=[ActiveCondition(id="invisible", source="pc-9")])
    state = melee(battle(srd, [invisible]), HERO, GOBLIN)

    after, _ = attack(
        state, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([3, 12, 5]), content=srd
    )

    assert [c.source for c in after.party[HERO].conditions] == ["pc-9"]


# =============================================================================================
# Использовать предмет: зелье лечения
# =============================================================================================


def potion_hero(make_hero, qty: int = 1, **overrides):
    inventory = [item("longsword"), item("potion_of_healing", qty=qty, equipped=False)]
    return make_hero(hp_max=30, hp=10, inventory=inventory, **overrides)


def test_a_healing_potion_heals_and_disappears_from_the_inventory(srd, make_hero):
    state = battle(srd, [potion_hero(make_hero)])

    after, events = act(srd, state, [3, 2], "use_item", item_id="potion_of_healing")

    heal_roll = roll_events(events)[0]
    assert (heal_roll.roll.expr, heal_roll.roll.total) == ("2d4+2", 7)
    assert [(h.target_id, h.amount, h.hp_after) for h in events if isinstance(h, HealEvent)] == [
        (HERO, 7, 17)
    ]
    assert hp(after, HERO) == 17
    assert [i.item_id for i in after.party[HERO].inventory] == ["longsword"]
    assert flags(after, HERO) == {"action": True}


def test_drinking_one_of_several_potions_keeps_the_rest(srd, make_hero):
    state = battle(srd, [potion_hero(make_hero, qty=3)])

    after, _ = act(srd, state, [1, 1], "use_item", item_id="potion_of_healing")

    assert [(i.item_id, i.qty) for i in after.party[HERO].inventory] == [
        ("longsword", 1),
        ("potion_of_healing", 2),
    ]


def test_a_potion_can_be_given_to_an_ally_in_the_same_zone(srd, make_hero, make_ally):
    state = battle(srd, [potion_hero(make_hero), make_ally(hp=3)])

    after, _ = act(srd, state, [4, 4], "use_item", item_id="potion_of_healing", target_id=ALLY)

    assert hp(after, ALLY) == 3 + 10
    assert hp(after, HERO) == 10  # пил союзник, а не герой
    assert [i.item_id for i in after.party[HERO].inventory] == ["longsword"]


def test_a_potion_wakes_up_a_downed_ally(srd, make_hero, make_ally):
    downed = make_ally(hp=0, conditions=[ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)])
    state = battle(srd, [potion_hero(make_hero), downed])

    after, _ = act(srd, state, [1, 1], "use_item", item_id="potion_of_healing", target_id=ALLY)

    assert hp(after, ALLY) == 4
    assert after.party[ALLY].conditions == []


def test_a_potion_cannot_reach_an_ally_in_another_zone_or_an_enemy(srd, make_hero, make_ally):
    state = battle(srd, [potion_hero(make_hero), make_ally()])
    fighter(state, ALLY).zone_id = "stairs"

    refused_act(
        srd, state, "use_item", "вне дальности", item_id="potion_of_healing", target_id=ALLY
    )
    refused_act(srd, state, "use_item", "союзник", item_id="potion_of_healing", target_id=GOBLIN)


def test_using_an_item_that_is_not_in_the_inventory_or_not_usable_is_refused(srd, make_hero):
    state = battle(srd, [potion_hero(make_hero, qty=1)])

    refused_act(srd, state, "use_item", "нет предмета", item_id="potion_of_greater_healing")
    refused_act(srd, state, "use_item", "укажите предмет")
    refused_act(
        srd,
        battle(srd, [make_hero(inventory=[item("rope")])]),
        "use_item",
        "нельзя применить",
        item_id="rope",
    )


def test_a_potion_cannot_heal_a_dead_ally(srd, make_hero, make_ally):
    state = battle(srd, [potion_hero(make_hero), make_ally()])
    state.party[ALLY].hp = 0
    state.party[ALLY].death_saves = DeathSaves(failures=3)

    refused_act(srd, state, "use_item", "мёртв", item_id="potion_of_healing", target_id=ALLY)


# =============================================================================================
# Толчок и захват
# =============================================================================================


def test_shove_knocks_the_target_prone_when_it_fails_the_save(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, events = act(srd, state, [10], "shove", target_id=GOBLIN)

    save_roll = roll_events(events)[0]
    # Сл = 8 + Сил героя (+3) + бонус мастерства (+2); гоблин берёт лучшее из Силы и Ловкости.
    assert (save_roll.dc, save_roll.roll.total, save_roll.success) == (13, 12, False)
    assert fighter(after, GOBLIN).conditions == [ActiveCondition(id="prone", source=HERO)]
    assert flags(after, HERO) == {"action": True}


def test_a_target_that_makes_the_save_is_not_shoved(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, events = act(srd, state, [11], "shove", target_id=GOBLIN)

    assert roll_events(events)[0].success is True
    assert fighter(after, GOBLIN).conditions == []
    assert flags(after, HERO) == {"action": True}  # действие потрачено и при неудаче


def test_the_target_saves_with_its_better_of_strength_and_dexterity(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("ogre",)), HERO, OGRE)

    _, events = act(srd, state, [8], "shove", target_id=OGRE)

    assert roll_events(events)[0].roll.expr == "1d20+4"  # у огра Сила 19 лучше Ловкости 8


def test_shove_can_push_the_target_out_of_melee_instead_of_knocking_it_down(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, _ = act(srd, state, [3], "shove", target_id=GOBLIN, option="push")

    assert fighter(after, GOBLIN).conditions == []
    assert (fighter(after, HERO).engaged_with, fighter(after, GOBLIN).engaged_with) == ([], [])


def test_shove_needs_an_engaged_enemy_that_can_be_knocked_down(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), make_ally()])

    refused_act(srd, state, "shove", "вплотную", target_id=GOBLIN)
    refused_act(srd, state, "shove", "противник", target_id=ALLY)
    refused_act(srd, state, "shove", "укажите цель")
    refused_act(
        srd,
        melee(battle(srd, [make_hero()], ("swarm_of_rats",)), HERO, "swarm_of_rats-1"),
        "shove",
        "невосприимчив",
        target_id="swarm_of_rats-1",
    )
    refused_act(
        srd, melee(state, HERO, GOBLIN), "shove", "push или prone", target_id=GOBLIN, option="fly"
    )


def test_grapple_holds_the_target_with_the_grapplers_id_as_the_source(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    after, events = act(srd, state, [10], "grapple", target_id=GOBLIN)

    assert roll_events(events)[0].dc == 13
    assert fighter(after, GOBLIN).conditions == [ActiveCondition(id="grappled", source=HERO)]


def test_a_grappled_creature_attacks_others_with_disadvantage_but_the_grappler_normally(
    srd, make_hero, make_ally
):
    state = melee(battle(srd, [make_hero(), make_ally()]), HERO, GOBLIN)
    held, _ = act(srd, state, [10], "grapple", target_id=GOBLIN)
    held = melee(turn_of(held, GOBLIN), ALLY, GOBLIN)

    _, at_grappler = attack(
        held, GOBLIN, HERO, action_id="scimitar", rng=ScriptedRng([15, 1]), content=srd
    )
    _, at_other = attack(
        held, GOBLIN, ALLY, action_id="scimitar", rng=ScriptedRng([15, 3, 1]), content=srd
    )

    assert roll_events(at_grappler)[0].roll.advantage == NONE
    assert roll_events(at_other)[0].roll.advantage == DISADVANTAGE


def test_grapple_is_refused_for_a_target_immune_to_it(srd, make_hero):
    state = melee(battle(srd, [make_hero()], ("swarm_of_rats",)), HERO, "swarm_of_rats-1")

    refused_act(srd, state, "grapple", "невосприимчив", target_id="swarm_of_rats-1")


# =============================================================================================
# Ресурсы классов и видов: Второе дыхание, Всплеск действий, Ярость, Безрассудная атака
# =============================================================================================


def test_second_wind_heals_one_d10_plus_level_and_spends_a_use(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=30, hp=5)])

    after, events = act(srd, state, [6], "second_wind")

    heal_roll = roll_events(events)[0]
    assert (heal_roll.roll.expr, heal_roll.roll.total) == ("1d10+3", 9)  # 3-й уровень
    assert [(h.amount, h.hp_after) for h in events if isinstance(h, HealEvent)] == [(9, 14)]
    assert hp(after, HERO) == 14
    assert after.party[HERO].resources["second_wind"] == 1
    assert flags(after, HERO) == {"bonus_action": True}  # бонусное действие, а не действие


def test_second_wind_cannot_be_used_without_uses_left(srd, make_hero):
    state = battle(srd, [make_hero(hp=5, resources={"second_wind": 0, "action_surge": 1})])

    refused_act(srd, state, "second_wind", "нет использований")


def test_the_use_counter_never_goes_below_zero(srd, make_hero):
    state = battle(srd, [make_hero(hp=1, hp_max=40, resources={"second_wind": 1})])

    once, _ = act(srd, state, [1], "second_wind")

    assert once.party[HERO].resources["second_wind"] == 0
    refused_act(srd, fresh_turn(turn_of(once, HERO), HERO), "second_wind", "нет использований")


def test_a_class_without_second_wind_cannot_use_it(srd, make_hero):
    state = battle(srd, [make_hero(class_id="wizard", resources={"second_wind": 2})])

    refused_act(srd, state, "second_wind", "нет особенности")


def test_second_wind_needs_the_bonus_action(srd, make_hero):
    state = battle(srd, [make_hero(hp=5)])
    flags(state, HERO)["bonus_action"] = True

    refused_act(srd, state, "second_wind", "бонусное действие уже потрачено")


def test_second_wind_heals_nothing_beyond_the_maximum_but_the_use_is_spent(srd, make_hero):
    state = battle(srd, [make_hero(hp_max=20, hp=19)])

    after, events = act(srd, state, [9], "second_wind")

    assert hp(after, HERO) == 20
    assert after.party[HERO].resources["second_wind"] == 1
    assert [h.amount for h in events if isinstance(h, HealEvent)] == [1]


def test_action_surge_gives_a_new_action_after_the_first_was_spent(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    first, _ = attack(
        state, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([1]), content=srd
    )  # промах: действие потрачено
    assert flags(first, HERO)["action"] is True

    surged, events = act(srd, first, [], "action_surge")

    assert flags(surged, HERO)["action"] is False
    assert surged.party[HERO].resources["action_surge"] == 0
    assert events
    second, _ = attack(
        surged, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([15, 4]), content=srd
    )
    assert hp(second, GOBLIN) == 10 - 7


def test_action_surge_is_pointless_before_the_action_is_spent_and_so_refused(srd, make_hero):
    state = battle(srd, [make_hero()])

    refused_act(srd, state, "action_surge", "сначала")


def test_action_surge_without_uses_or_the_feature_is_refused(srd, make_hero):
    spent = battle(srd, [make_hero(resources={"second_wind": 2, "action_surge": 0})])
    flags(spent, HERO)["action"] = True
    other = battle(srd, [make_hero(class_id="rogue", resources={"action_surge": 1})])
    flags(other, HERO)["action"] = True

    refused_act(srd, spent, "action_surge", "нет использований")
    refused_act(srd, other, "action_surge", "нет особенности")


def make_barbarian(make_hero, **overrides):
    fields = {
        "class_id": "barbarian",
        "inventory": [item("greataxe")],
        "resources": {"rage": 3},
        "scores": {STR: 16, CON: 14},
    }
    return make_hero(**(fields | overrides))


def test_rage_is_a_bonus_action_that_adds_an_effect_and_spends_a_use(srd, make_hero):
    state = battle(srd, [make_barbarian(make_hero)])

    after, events = act(srd, state, [], "rage")

    assert after.party[HERO].buffs == [ActiveBuff(id="rage", rounds_left=10, source_id=HERO)]
    assert after.party[HERO].resources["rage"] == 2
    assert flags(after, HERO) == {"bonus_action": True}
    assert ConditionEvent(target_id=HERO, condition="rage", added=True) in events


def test_rage_adds_damage_and_resistance_to_following_fights(srd, make_hero):
    state = melee(battle(srd, [make_barbarian(make_hero)]), HERO, GOBLIN)
    raging, _ = act(srd, state, [], "rage")

    after, events = attack(
        raging, HERO, GOBLIN, weapon="greataxe", rng=ScriptedRng([15, 6]), content=srd
    )

    assert [d.amount for d in damage_events(events)] == [6 + 3 + 2]
    assert hp(after, GOBLIN) == 0


def test_rage_cannot_be_started_twice_or_without_uses_or_by_other_classes(srd, make_hero):
    raging, _ = act(srd, battle(srd, [make_barbarian(make_hero)]), [], "rage")

    refused_act(srd, fresh_turn(turn_of(raging, HERO), HERO), "rage", "уже в ярости")
    refused_act(
        srd,
        battle(srd, [make_barbarian(make_hero, resources={"rage": 0})]),
        "rage",
        "нет использований",
    )
    refused_act(srd, battle(srd, [make_hero(resources={"rage": 3})]), "rage", "нет особенности")


def test_rage_ends_by_itself_after_ten_rounds(srd, make_hero):
    state = battle(srd, [make_barbarian(make_hero)])
    state, _ = act(srd, state, [], "rage")

    for _ in range(10 * 2):  # десять кругов по два бойца
        state, events = end_turn(state, rng=ScriptedRng([]), content=srd)
        if state.party[HERO].buffs == []:
            break

    assert state.party[HERO].buffs == []
    assert ConditionEvent(target_id=HERO, condition="rage", added=False) in events


def test_reckless_attack_is_a_free_declaration_before_the_first_attack(srd, make_hero):
    state = melee(battle(srd, [make_barbarian(make_hero)]), HERO, GOBLIN)

    declared, events = act(srd, state, [], "reckless")
    after, attack_events = attack(
        declared, HERO, GOBLIN, weapon="greataxe", rng=ScriptedRng([3, 15, 5]), content=srd
    )

    assert flags(declared, HERO) == {"reckless": True}  # действие не потрачено
    assert events
    assert roll_events(attack_events)[0].roll.advantage == ADVANTAGE
    assert hp(after, GOBLIN) < 10


def test_reckless_attack_is_refused_after_the_attack_or_for_other_classes(srd, make_hero):
    attacked = battle(srd, [make_barbarian(make_hero)])
    flags(attacked, HERO)["action"] = True

    refused_act(srd, attacked, "reckless", "до первой атаки")
    refused_act(srd, battle(srd, [make_hero()]), "reckless", "нет особенности")
    refused_act(
        srd, battle(srd, [make_barbarian(make_hero, level=1)]), "reckless", "нет особенности"
    )


def test_reckless_attack_cannot_be_declared_twice(srd, make_hero):
    declared, _ = act(srd, battle(srd, [make_barbarian(make_hero)]), [], "reckless")

    refused_act(srd, declared, "reckless", "уже объявлена")


def make_rogue(make_hero, **overrides):
    fields = {
        "class_id": "rogue",
        "inventory": [item("shortsword")],
        "scores": {DEX: 16},
        "resources": {},
    }
    return make_hero(**(fields | overrides))


def test_steady_aim_gives_advantage_on_the_next_attack_and_is_then_spent(srd, make_hero):
    state = melee(battle(srd, [make_rogue(make_hero)]), HERO, GOBLIN)

    aimed, _ = act(srd, state, [], "steady_aim")
    after, events = attack(
        aimed, HERO, GOBLIN, weapon="shortsword", rng=ScriptedRng([3, 12, 4, 1, 1]), content=srd
    )  # кости: два к20, урон меча, скрытая атака (преимущество даёт право на неё)

    assert flags(aimed, HERO) == {"bonus_action": True, "steady_aim": True}
    assert roll_events(events)[0].roll.advantage == ADVANTAGE
    assert "Скрытая атака" in " ".join(labels(events))
    assert "steady_aim" not in flags(after, HERO)


def test_steady_aim_also_applies_to_a_spell_attack(srd, make_wizard):
    state = battle(srd, [make_wizard(level=2)])
    flags(state, WIZARD)["steady_aim"] = True  # плут не колдует: ставим флаг волшебнику напрямую

    _, events = cast(srd, state, [3, 12, 4], "fire_bolt", [GOBLIN])

    assert roll_events(events)[0].roll.advantage == ADVANTAGE


def test_steady_aim_stops_the_character_from_moving_for_the_rest_of_the_turn(srd, make_hero):
    aimed, _ = act(srd, battle(srd, [make_rogue(make_hero)]), [], "steady_aim")

    with pytest.raises(CombatError, match="скорость 0"):
        move(aimed, HERO, to_zone="stairs", rng=ScriptedRng([]), content=srd)


def test_steady_aim_is_refused_after_moving_or_for_other_classes_or_level_two(srd, make_hero):
    moved = battle(srd, [make_rogue(make_hero)])
    flags(moved, HERO)["moved"] = True

    refused_act(srd, moved, "steady_aim", "двигался")
    refused_act(srd, battle(srd, [make_hero()]), "steady_aim", "нет особенности")
    refused_act(srd, battle(srd, [make_rogue(make_hero, level=2)]), "steady_aim", "нет особенности")


def test_steady_aim_needs_the_bonus_action(srd, make_hero):
    state = battle(srd, [make_rogue(make_hero)])
    flags(state, HERO)["bonus_action"] = True

    refused_act(srd, state, "steady_aim", "бонусное действие уже потрачено")


# =============================================================================================
# Божественный канал жреца и дыхание драконорождённого
# =============================================================================================


def test_divine_spark_can_heal_a_creature_with_one_d8_plus_wisdom(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp_max=40, hp=5)])

    after, events = act(
        srd, state, [4], "divine_spark", actor=CLERIC, target_id=HERO, option="heal"
    )

    spark = roll_events(events)[0]
    assert (spark.roll.expr, spark.roll.total) == ("1d8+3", 7)
    assert hp(after, HERO) == 12
    assert after.party[CLERIC].resources["channel_divinity"] == 1
    assert flags(after, CLERIC) == {"action": True}


def test_divine_spark_can_burn_an_enemy_with_a_constitution_save_for_half(srd, make_cleric):
    state = battle(srd, [make_cleric()])

    failed, failed_events = act(
        srd, state, [10, 5], "divine_spark", actor=CLERIC, target_id=GOBLIN, option="necrotic"
    )
    saved, saved_events = act(
        srd, state, [14, 5], "divine_spark", actor=CLERIC, target_id=GOBLIN, option="radiant"
    )

    assert roll_events(failed_events)[0].dc == 13
    assert [(d.amount, d.damage_type) for d in damage_events(failed_events)] == [(8, "necrotic")]
    assert [(d.amount, d.damage_type) for d in damage_events(saved_events)] == [(4, "radiant")]
    assert (hp(failed, GOBLIN), hp(saved, GOBLIN)) == (2, 6)


def test_divine_spark_needs_a_valid_option_a_target_in_reach_and_a_use(srd, make_cleric):
    state = battle(srd, [make_cleric()])
    spent = battle(srd, [make_cleric(resources={"channel_divinity": 0})])
    far = battle(srd, [make_cleric()], monster_zone="vault")

    refused_act(
        srd, state, "divine_spark", "heal, necrotic или radiant", actor=CLERIC, target_id=GOBLIN
    )
    refused_act(
        srd,
        state,
        "divine_spark",
        "heal, necrotic или radiant",
        actor=CLERIC,
        target_id=GOBLIN,
        option="fire",
    )
    refused_act(srd, state, "divine_spark", "укажите цель", actor=CLERIC, option="heal")
    refused_act(
        srd,
        spent,
        "divine_spark",
        "нет использований",
        actor=CLERIC,
        target_id=GOBLIN,
        option="radiant",
    )
    refused_act(
        srd, far, "divine_spark", "вне дальности", actor=CLERIC, target_id=GOBLIN, option="radiant"
    )


def test_divine_spark_is_only_for_clerics_of_the_right_level(srd, make_cleric):
    low = battle(srd, [make_cleric(level=1)])
    other = battle(srd, [make_cleric(class_id="wizard")])

    refused_act(
        srd,
        low,
        "divine_spark",
        "нет особенности",
        actor=CLERIC,
        target_id=GOBLIN,
        option="radiant",
    )
    refused_act(
        srd,
        other,
        "divine_spark",
        "нет особенности",
        actor=CLERIC,
        target_id=GOBLIN,
        option="radiant",
    )


def test_preserve_life_heals_up_to_the_fifteen_point_pool_without_dice(srd, make_cleric, make_hero):
    state = battle(srd, [make_cleric(), make_hero(hp_max=40, hp=4)])

    after, events = act(srd, state, [], "preserve_life", actor=CLERIC, target_id=HERO)

    assert [h.amount for h in events if isinstance(h, HealEvent)] == [15]
    assert hp(after, HERO) == 19
    assert after.party[CLERIC].resources["channel_divinity"] == 1
    assert flags(after, CLERIC) == {"action": True}


def test_preserve_life_never_lifts_a_creature_above_half_of_its_maximum(
    srd, make_cleric, make_hero
):
    near_half = battle(srd, [make_cleric(), make_hero(hp_max=40, hp=15)])
    above_half = battle(srd, [make_cleric(), make_hero(hp_max=40, hp=25)])

    after, _ = act(srd, near_half, [], "preserve_life", actor=CLERIC, target_id=HERO)

    assert hp(after, HERO) == 20
    refused_act(srd, above_half, "preserve_life", "половин", actor=CLERIC, target_id=HERO)


def test_preserve_life_needs_a_use_and_the_feature(srd, make_cleric, make_hero):
    spent = battle(srd, [make_cleric(resources={"channel_divinity": 0}), make_hero(hp=2)])
    low = battle(srd, [make_cleric(level=2), make_hero(hp=2)])

    refused_act(srd, spent, "preserve_life", "нет использований", actor=CLERIC, target_id=HERO)
    refused_act(srd, low, "preserve_life", "нет особенности", actor=CLERIC, target_id=HERO)


def dragonborn(make_hero, **overrides):
    fields = {"species": "dragonborn", "scores": {STR: 16, CON: 14}}
    return make_hero(**(fields | overrides))


def test_breath_weapon_is_a_dexterity_save_for_half_damage(srd, make_hero):
    state = battle(srd, [dragonborn(make_hero)])

    failed, failed_events = act(srd, state, [9, 7], "breath_weapon", target_id=GOBLIN)
    saved, saved_events = act(srd, state, [10, 7], "breath_weapon", target_id=GOBLIN)

    # Сл = 8 + Тел (+2) + бонус мастерства (+2) = 12; Лов гоблина даёт +2.
    assert roll_events(failed_events)[0].dc == 12
    assert [(d.amount, d.damage_type) for d in damage_events(failed_events)] == [(7, "fire")]
    assert [d.amount for d in damage_events(saved_events)] == [3]
    assert (hp(failed, GOBLIN), hp(saved, GOBLIN)) == (3, 7)
    assert flags(failed, HERO) == {"action": True}


def test_breath_weapon_damage_type_follows_the_option(srd, make_hero):
    state = battle(srd, [dragonborn(make_hero)])

    _, events = act(srd, state, [1, 4], "breath_weapon", target_id=GOBLIN, option="cold")

    assert [d.damage_type for d in damage_events(events)] == ["cold"]
    refused_act(srd, state, "breath_weapon", "acid, cold", target_id=GOBLIN, option="radiant")


def test_breath_weapon_uses_are_limited_by_the_proficiency_bonus(srd, make_hero):
    state = battle(srd, [dragonborn(make_hero)])

    once, _ = act(srd, state, [1, 4], "breath_weapon", target_id=GOBLIN)
    twice, _ = act(
        srd, fresh_turn(turn_of(once, HERO), HERO), [1, 4], "breath_weapon", target_id=GOBLIN
    )

    assert once.party[HERO].resources["breath_weapon"] == 1  # бонус мастерства 3-го уровня — 2
    assert twice.party[HERO].resources["breath_weapon"] == 0
    refused_act(
        srd,
        fresh_turn(turn_of(twice, HERO), HERO),
        "breath_weapon",
        "нет использований",
        target_id=GOBLIN,
    )


def test_breath_weapon_needs_the_trait_and_a_target_in_the_same_zone(srd, make_hero):
    refused_act(srd, battle(srd, [make_hero()]), "breath_weapon", "нет черты", target_id=GOBLIN)
    refused_act(
        srd,
        battle(srd, [dragonborn(make_hero)], monster_zone="stairs"),
        "breath_weapon",
        "вне дальности",
        target_id=GOBLIN,
    )
    refused_act(srd, battle(srd, [dragonborn(make_hero)]), "breath_weapon", "укажите цель")


def test_breath_weapon_returns_after_a_long_rest(srd, make_hero):
    from dnd_engine.rest import long_rest

    state = battle(srd, [dragonborn(make_hero)])
    used, _ = act(srd, state, [1, 4], "breath_weapon", target_id=GOBLIN)

    rested, _ = long_rest(used.party[HERO], srd)

    assert rested.resources["breath_weapon"] == 2


# =============================================================================================
# Действия монстров
# =============================================================================================


class FixedRng:
    """Кубы всегда показывают заданное значение (не больше граней): для счёта бросков."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return min(self.value, b)


def test_a_monster_attack_rolls_against_the_armor_class_and_spends_the_action(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)

    after, events = mob(srd, state, [10, 4], GOBLIN, "scimitar", [HERO])

    attack_roll = roll_events(events)[0]
    assert (attack_roll.label, attack_roll.target_ac, attack_roll.success) == (
        "Атака: Скимитар",
        10,
        True,
    )
    assert [(d.target_id, d.amount) for d in damage_events(events)] == [(HERO, 6)]
    assert hp(after, HERO) == 14
    assert flags(after, GOBLIN) == {"action": True}


def test_a_monster_attack_that_misses_deals_nothing(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)

    after, events = mob(srd, state, [2], GOBLIN, "scimitar", [HERO])

    assert damage_events(events) == []
    assert hp(after, HERO) == 20


def test_a_melee_monster_action_needs_an_engaged_target_but_a_ranged_one_does_not(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()]), GOBLIN)

    refused_mob(srd, state, GOBLIN, "scimitar", [HERO], "вовлечённого")
    after, _ = mob(srd, state, [10, 4], GOBLIN, "shortbow", [HERO])

    assert hp(after, HERO) == 14


def test_a_reach_monster_hits_an_enemy_in_its_zone_without_engagement(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()], ("bugbear_warrior",)), BUGBEAR)

    after, _ = mob(srd, state, [10, 3, 4, 2], BUGBEAR, "light_hammer", [HERO])  # 3к4 + 2

    assert hp(after, HERO) < 20


def test_a_wolf_bite_knocks_the_target_prone_on_a_hit(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("wolf",)), HERO, WOLF), WOLF)

    after, events = mob(srd, state, [10, 3], WOLF, "bite", [HERO])

    assert after.party[HERO].conditions == [ActiveCondition(id="prone", source=WOLF)]
    assert ConditionEvent(target_id=HERO, condition="prone", added=True) in events


def test_a_wolf_bite_that_misses_leaves_the_target_standing(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("wolf",)), HERO, WOLF), WOLF)

    after, _ = mob(srd, state, [2], WOLF, "bite", [HERO])

    assert after.party[HERO].conditions == []


def test_the_same_effect_comes_from_the_plain_attack_function_of_a_monster(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("wolf",)), HERO, WOLF), WOLF)

    after, _ = attack(state, WOLF, HERO, action_id="bite", rng=ScriptedRng([10, 3]), content=srd)

    assert after.party[HERO].conditions == [ActiveCondition(id="prone", source=WOLF)]


def test_a_second_hit_does_not_stack_the_same_condition_from_the_same_monster(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("wolf",)), HERO, WOLF), WOLF)
    first, _ = mob(srd, state, [10, 3], WOLF, "bite", [HERO])

    # Сбитый с ног цель получает удары вплотную с преимуществом: две кости к20.
    again, _ = mob(srd, fresh_turn(first, WOLF), [10, 12, 3], WOLF, "bite", [HERO])

    assert again.party[HERO].conditions == [ActiveCondition(id="prone", source=WOLF)]


def test_a_bugbear_grab_holds_the_target_with_the_bugbear_as_source(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()], ("bugbear_warrior",)), BUGBEAR)

    after, _ = mob(srd, state, [10, 3, 4], BUGBEAR, "grab", [HERO])

    assert after.party[HERO].conditions == [ActiveCondition(id="grappled", source=BUGBEAR)]


def test_ghoul_claws_paralyze_after_a_failed_constitution_save_until_the_targets_turn_ends(
    srd, make_hero
):
    state = turn_of(melee(battle(srd, [make_hero()], ("ghoul",)), HERO, GHOUL), GHOUL)

    after, events = mob(srd, state, [10, 2, 9], GHOUL, "claw", [HERO])

    save_roll = roll_events(events)[-1]
    assert (save_roll.dc, save_roll.success) == (10, False)
    assert after.party[HERO].conditions == [
        ActiveCondition(id="paralyzed", rounds_left=1, source=GHOUL)
    ]
    # Паралич кончается вместе со следующим ходом цели.
    freed, free_events = end_turn(turn_of(after, HERO), rng=ScriptedRng([]), content=srd)
    assert freed.party[HERO].conditions == []
    assert ConditionEvent(target_id=HERO, condition="paralyzed", added=False) in free_events


def test_a_target_that_makes_the_save_against_ghoul_claws_stays_free(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("ghoul",)), HERO, GHOUL), GHOUL)

    after, events = mob(srd, state, [10, 2, 10], GHOUL, "claw", [HERO])

    assert roll_events(events)[-1].success is True
    assert after.party[HERO].conditions == []
    assert hp(after, HERO) == 20 - 4


def test_a_paralyzing_hit_makes_the_caster_lose_concentration(srd, make_cleric, make_hero):
    cleric = make_cleric(concentration="bless")
    ally = make_hero(buffs=[bless()])
    state = battle(srd, [cleric, ally], ("ghoul",))
    state = turn_of(melee(state, CLERIC, GHOUL), GHOUL)

    # Удар: к20, урон, спасбросок концентрации (успех), затем спасбросок от паралича (провал).
    after, events = mob(srd, state, [10, 2, 15, 9], GHOUL, "claw", [CLERIC])

    assert after.party[CLERIC].concentration is None
    assert after.party[HERO].buffs == []
    assert after.party[CLERIC].buffs == []
    assert ConditionEvent(target_id=HERO, condition="bless", added=False) in events


def test_a_giant_spider_web_restrains_a_target_that_fails_the_dexterity_save(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()], ("giant_spider",)), SPIDER)

    failed, failed_events = mob(srd, state, [12], SPIDER, "web", [HERO])
    saved, _ = mob(srd, state, [13], SPIDER, "web", [HERO])

    assert roll_events(failed_events)[0].dc == 13
    assert failed.party[HERO].conditions == [ActiveCondition(id="restrained", source=SPIDER)]
    assert saved.party[HERO].conditions == []
    assert flags(failed, SPIDER) == {"action": True}


def test_a_web_hits_one_creature_so_a_second_target_is_refused(srd, make_hero, make_ally):
    state = turn_of(battle(srd, [make_hero(), make_ally()], ("giant_spider",)), SPIDER)

    refused_mob(srd, state, SPIDER, "web", [HERO, ALLY], "целей")
    # Одна цель, названная дважды, — всё ещё одна цель.
    after, _ = mob(srd, state, [12], SPIDER, "web", [HERO, HERO])

    assert [c.id for c in after.party[HERO].conditions] == ["restrained"]
    assert after.party[ALLY].conditions == []


def breath_of(targets: int | None = None) -> MonsterAction:
    extra = {} if targets is None else {"targets": targets}
    return MonsterAction(
        id="fire_breath",
        name="Огненное дыхание",
        kind="save",
        save=DEX,
        dc=13,
        damage="2d6",
        damage_type="fire",
        half_on_save=True,
        range="near",
        **extra,
    )


@pytest.mark.parametrize(
    ("targets", "chosen", "allowed"),
    [
        (None, [HERO, ALLY], False),  # по умолчанию действие бьёт одну цель
        (2, [HERO, ALLY], True),
        (2, [HERO, ALLY, "pc-3"], False),
        (6, [HERO, ALLY, "pc-3"], True),  # площадь — явное большее число в данных
    ],
)
def test_a_save_action_hits_at_most_the_number_of_targets_the_data_names(
    srd, make_hero, make_ally, targets, chosen, allowed
):
    custom = srd_with_monster(srd, "goblin_warrior", actions=(breath_of(targets),))
    party = [make_hero(), make_ally(), make_hero(id="pc-3", name="Третий")]
    state = turn_of(battle(custom, party), GOBLIN)

    if allowed:
        # По одному спасброску на цель, затем кости урона один раз на всех.
        after, _ = mob(custom, state, [*[15] * len(chosen), 3, 4], GOBLIN, "fire_breath", chosen)
        assert flags(after, GOBLIN) == {"action": True}
    else:
        refused_mob(custom, state, GOBLIN, "fire_breath", chosen, "целей")


def test_a_web_cannot_reach_a_far_target(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()], ("giant_spider",), monster_zone="vault"), SPIDER)

    refused_mob(srd, state, SPIDER, "web", [HERO], "вне дальности")


@pytest.mark.parametrize(
    ("monster_id", "fighter_id"),
    [
        ("bandit_captain", CAPTAIN),
        ("goblin_boss", "goblin_boss-1"),
        ("owlbear", "owlbear-1"),
        ("ghoul", GHOUL),
        ("animated_armor", "animated_armor-1"),
    ],
)
def test_a_multiattack_makes_every_listed_attack_and_spends_the_action_once(
    srd, make_hero, monster_id, fighter_id
):
    state = turn_of(
        melee(battle(srd, [make_hero(hp_max=90, hp=90)], (monster_id,)), HERO, fighter_id),
        fighter_id,
    )
    rng = FixedRng(10)

    after, events = monster_action(state, fighter_id, "multiattack", [HERO], rng=rng, content=srd)

    attack_rolls = [e for e in roll_events(events) if e.label.startswith("Атака")]
    assert len(attack_rolls) == len(srd.monsters[monster_id].actions[0].multiattack) == 2
    assert flags(after, fighter_id) == {"action": True}


def test_the_bandit_captain_multiattack_deals_each_scimitar_hit_separately(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("bandit_captain",)), HERO, CAPTAIN), CAPTAIN)

    after, events = mob(srd, state, [10, 3, 10, 4], CAPTAIN, "multiattack", [HERO])

    assert [d.amount for d in damage_events(events)] == [6, 7]  # 1к6 + 3 дважды
    assert hp(after, HERO) == 20 - 13


def test_a_multiattack_can_spread_its_attacks_between_targets(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), make_ally()], ("bandit_captain",))
    state = turn_of(melee(melee(state, HERO, CAPTAIN), ALLY, CAPTAIN), CAPTAIN)

    after, events = mob(srd, state, [10, 3, 10, 4], CAPTAIN, "multiattack", [HERO, ALLY])

    assert [(d.target_id, d.amount) for d in damage_events(events)] == [(HERO, 6), (ALLY, 7)]
    assert (hp(after, HERO), hp(after, ALLY)) == (14, 13)


def test_a_multiattack_needs_one_target_or_one_per_attack(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), make_ally()], ("bandit_captain",))
    state = turn_of(melee(melee(state, HERO, CAPTAIN), ALLY, CAPTAIN), CAPTAIN)

    refused_mob(srd, state, CAPTAIN, "multiattack", [HERO, ALLY, HERO], "по одной на")
    refused_mob(srd, state, CAPTAIN, "multiattack", [], "укажите цель")


def test_a_multiattack_skips_attacks_at_a_target_that_already_died(srd, make_hero):
    fragile = make_hero(hp_max=3, hp=3)  # массивный урон убивает сразу
    state = turn_of(melee(battle(srd, [fragile], ("bandit_captain",)), HERO, CAPTAIN), CAPTAIN)

    after, events = mob(srd, state, [10, 3], CAPTAIN, "multiattack", [HERO])

    assert is_dead(after.party[HERO])
    assert len([e for e in roll_events(events) if e.label.startswith("Атака")]) == 1


def test_an_action_without_a_mechanic_only_reports_an_event(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()]), GOBLIN)

    after, events = mob(srd, state, [], GOBLIN, "nimble_escape", [])

    assert events == [ActionEvent(actor_id=GOBLIN, action="nimble_escape", label="Ловкий уход")]
    assert after == state  # ни флагов, ни ресурсов: расходует его мастер


def test_a_monster_can_use_a_free_form_action_off_its_turn(srd, make_hero):
    state = battle(srd, [make_hero()], ("bandit_captain",))  # ход героя

    after, events = mob(srd, state, [], CAPTAIN, "parry", [])

    assert events
    assert after == state


@pytest.mark.parametrize(
    ("monster", "action_id"), [("goblin_warrior", "nimble_escape"), ("bandit_captain", "parry")]
)
@pytest.mark.parametrize(
    ("hurt_monster", "reason"),
    [
        (lambda f: setattr(f, "hp", 0), "на 0 HP"),  # убитый ничего не делает
        (lambda f: f.conditions.append(ActiveCondition(id="stunned")), "недееспособен"),
    ],
    ids=["dead", "incapacitated"],
)
def test_a_free_form_monster_action_needs_a_living_capable_monster(
    srd, make_hero, monster, action_id, hurt_monster, reason
):
    state = battle(srd, [make_hero()], (monster,))  # ход героя: свой ход монстру не нужен
    hurt_monster(fighter(state, f"{monster}-1"))

    refused_mob(srd, state, f"{monster}-1", action_id, [], reason)


def test_monster_action_refusals_leave_the_state_and_the_dice_alone(srd, make_hero, make_ally):
    state = turn_of(melee(battle(srd, [make_hero(), make_ally()]), HERO, GOBLIN), GOBLIN)
    on_hero_turn = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    stunned = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)
    fighter(stunned, GOBLIN).conditions = [ActiveCondition(id="stunned")]
    spent = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)
    flags(spent, GOBLIN)["action"] = True
    dead_target = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)
    dead_target.party[HERO].death_saves = DeathSaves(failures=3)

    refused_mob(srd, state, GOBLIN, "fireball", [HERO], "нет действия")
    refused_mob(srd, state, "ghost-1", "scimitar", [HERO], "нет бойца")
    refused_mob(srd, state, HERO, "scimitar", [GOBLIN], "не монстр")
    refused_mob(srd, on_hero_turn, GOBLIN, "scimitar", [HERO], "не ход")
    refused_mob(srd, stunned, GOBLIN, "scimitar", [HERO], "недееспособен")
    refused_mob(srd, spent, GOBLIN, "scimitar", [HERO], "действие уже потрачено")
    refused_mob(srd, dead_target, GOBLIN, "scimitar", [HERO], "мёртв")
    refused_mob(srd, state, GOBLIN, "scimitar", [GOBLIN], "союзника")
    refused_mob(srd, state, GOBLIN, "scimitar", [], "укажите цель")
    refused_mob(srd, state, GOBLIN, "scimitar", ["ghost-1"], "нет бойца")


def test_a_monster_save_action_with_damage_shares_one_roll_and_halves_on_success(
    srd, make_hero, make_ally
):
    custom = srd_with_monster(srd, "goblin_warrior", actions=(breath_of(targets=2),))
    state = turn_of(battle(custom, [make_hero(), make_ally()]), GOBLIN)

    after, events = mob(custom, state, [12, 13, 3, 4], GOBLIN, "fire_breath", [HERO, ALLY])

    assert [(d.target_id, d.amount) for d in damage_events(events)] == [(HERO, 7), (ALLY, 3)]
    assert (hp(after, HERO), hp(after, ALLY)) == (13, 17)
    assert labels(events).count("Урон: Огненное дыхание") == 1


def test_a_save_effect_leaving_an_orc_on_its_feet_still_asks_for_the_concentration_save(
    srd, make_hero
):
    custom = srd_with_monster(srd, "goblin_warrior", actions=(breath_of(),))
    orc = make_hero(species="orc", hp=5, concentration="hold_person")
    state = turn_of(battle(custom, [orc]), GOBLIN)

    # Спасбросок Лов 2 — провал, 6 + 6 огня роняют орка до 0, Неутомимость оставляет 1 HP,
    # и на 12 урона нужен спасбросок Тел против Сл 10: 9 — провал.
    after, events = mob(custom, state, [2, 6, 6, 9], GOBLIN, "fire_breath", [HERO])

    assert (hp(after, HERO), after.party[HERO].concentration) == (1, None)
    assert [e.dc for e in roll_events(events) if "Концентрация" in e.label] == [10]


# =============================================================================================
# Наложение состояний: общее место для заклинаний, ударов монстров и решений мастера
# =============================================================================================

INCAPACITATING = ["incapacitated", "paralyzed", "stunned", "unconscious", "petrified"]
HARMLESS_FOR_CONCENTRATION = [
    "poisoned",
    "prone",
    "restrained",
    "grappled",
    "frightened",
    "blinded",
]


def concentrating_cleric_state(srd, make_cleric, make_hero) -> GameState:
    cleric = make_cleric(concentration="bless", buffs=[bless()])
    return battle(srd, [cleric, make_hero(buffs=[bless()])])


@pytest.mark.parametrize("condition_id", INCAPACITATING)
def test_an_incapacitated_caster_loses_concentration_and_its_effects(
    srd, make_cleric, make_hero, condition_id
):
    state = concentrating_cleric_state(srd, make_cleric, make_hero)

    after, events = apply_condition(state, CLERIC, ActiveCondition(id=condition_id), content=srd)

    assert after.party[CLERIC].concentration is None
    assert after.party[CLERIC].buffs == [] and after.party[HERO].buffs == []
    assert [c.id for c in after.party[CLERIC].conditions] == [condition_id]
    assert ConditionEvent(target_id=HERO, condition="bless", added=False) in events


@pytest.mark.parametrize("condition_id", HARMLESS_FOR_CONCENTRATION)
def test_a_condition_that_does_not_incapacitate_keeps_concentration(
    srd, make_cleric, make_hero, condition_id
):
    state = concentrating_cleric_state(srd, make_cleric, make_hero)

    after, _ = apply_condition(state, CLERIC, ActiveCondition(id=condition_id), content=srd)

    assert after.party[CLERIC].concentration == "bless"
    assert [b.id for b in after.party[HERO].buffs] == ["bless"]


def test_a_condition_on_a_non_concentrating_character_changes_only_the_list(srd, make_hero):
    state = battle(srd, [make_hero()])

    after, events = apply_condition(
        state, HERO, ActiveCondition(id="paralyzed", rounds_left=3, source=GOBLIN), content=srd
    )

    assert after.party[HERO].conditions == [
        ActiveCondition(id="paralyzed", rounds_left=3, source=GOBLIN)
    ]
    assert events == [ConditionEvent(target_id=HERO, condition="paralyzed", added=True)]


def test_a_condition_can_be_put_on_a_monster_and_on_a_character_outside_combat(srd, make_hero):
    in_combat = battle(srd, [make_hero()])

    on_goblin, _ = apply_condition(in_combat, GOBLIN, ActiveCondition(id="prone"), content=srd)
    outside, _ = apply_condition(
        state_of(make_hero()), HERO, ActiveCondition(id="poisoned"), content=srd
    )

    assert [c.id for c in fighter(on_goblin, GOBLIN).conditions] == ["prone"]
    assert [c.id for c in outside.party[HERO].conditions] == ["poisoned"]


def test_the_same_condition_from_the_same_source_is_refreshed_and_other_sources_stack(
    srd, make_hero
):
    state = battle(srd, [make_hero()], ("goblin_warrior", "goblin_warrior"))
    first, _ = apply_condition(
        state, HERO, ActiveCondition(id="poisoned", rounds_left=2, source=GOBLIN), content=srd
    )
    refreshed, _ = apply_condition(
        first, HERO, ActiveCondition(id="poisoned", rounds_left=5, source=GOBLIN), content=srd
    )
    stacked, _ = apply_condition(
        refreshed,
        HERO,
        ActiveCondition(id="poisoned", rounds_left=1, source=GOBLIN_2),
        content=srd,
    )

    assert [(c.source, c.rounds_left) for c in refreshed.party[HERO].conditions] == [(GOBLIN, 5)]
    assert [(c.source, c.rounds_left) for c in stacked.party[HERO].conditions] == [
        (GOBLIN, 5),
        (GOBLIN_2, 1),
    ]


SRD_CONDITIONS = sorted(load_srd().conditions)


@pytest.mark.parametrize("condition_id", SRD_CONDITIONS)
def test_apply_condition_accepts_every_srd_condition(srd, make_hero, condition_id):
    after, _ = apply_condition(
        battle(srd, [make_hero()]), HERO, ActiveCondition(id=condition_id), content=srd
    )

    assert [c.id for c in after.party[HERO].conditions] == [condition_id]


@pytest.mark.parametrize("rounds_left", [None, 1, 100])
@pytest.mark.parametrize("source", ["", HERO, GOBLIN])
def test_apply_condition_accepts_a_bounded_term_and_a_source_that_is_a_fighter_or_empty(
    srd, make_hero, rounds_left, source
):
    condition = ActiveCondition(id="poisoned", rounds_left=rounds_left, source=source)

    after, _ = apply_condition(battle(srd, [make_hero()]), HERO, condition, content=srd)

    assert after.party[HERO].conditions == [condition]


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        # Метки и эффекты заклинаний — служебные записи движка, а не состояния SRD.
        (ActiveCondition(id="hunters_mark", source=HERO), "не состояние SRD"),
        (ActiveCondition(id="guiding_bolt"), "не состояние SRD"),
        (ActiveCondition(id="helped"), "не состояние SRD"),
        (ActiveCondition(id="bless"), "не состояние SRD"),
        (ActiveCondition(id="fireball"), "не состояние SRD"),
        # Повторного спасброска у внешнего состояния нет: Сл движка не должна идти от вызывающего.
        (ActiveCondition(id="paralyzed", save=CON, dc=99), "повторного спасброска"),
        (ActiveCondition(id="paralyzed", save=CON), "повторного спасброска"),
        (ActiveCondition(id="paralyzed", dc=15), "повторного спасброска"),
        # Срок: от одного раунда до десяти минут или без срока.
        (ActiveCondition(id="paralyzed", rounds_left=0), "срок"),
        (ActiveCondition(id="paralyzed", rounds_left=-3), "срок"),
        (ActiveCondition(id="paralyzed", rounds_left=101), "срок"),
        (ActiveCondition(id="paralyzed", rounds_left=10**9), "срок"),
        # Служебные источники и неизвестный источник.
        (ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE), "источник"),
        (ActiveCondition(id="invisible", source=HIDDEN_SOURCE), "источник"),
        (ActiveCondition(id="prone", source="ghost-1"), "источник"),
    ],
)
def test_apply_condition_refuses_what_only_the_engine_itself_puts_on(
    srd, make_hero, condition, reason
):
    state = battle(srd, [make_hero()])
    snapshot = state.model_copy(deep=True)

    with pytest.raises(CombatError, match=reason):
        apply_condition(state, HERO, condition, content=srd)

    assert state == snapshot


def test_a_monster_immune_to_a_condition_refuses_it(srd, make_hero):
    state = battle(srd, [make_hero()], ("shadow",))

    with pytest.raises(CombatError, match="невосприимчив"):
        apply_condition(state, "shadow-1", ActiveCondition(id="paralyzed"), content=srd)
    # Невосприимчивость к состоянию не мешает ему получить урон соответствующего названия.
    ok, _ = apply_condition(state, "shadow-1", ActiveCondition(id="blinded"), content=srd)
    assert [c.id for c in fighter(ok, "shadow-1").conditions] == ["blinded"]


def test_applying_a_condition_to_an_unknown_target_is_refused(srd, make_hero):
    with pytest.raises(CombatError, match="нет бойца"):
        apply_condition(
            battle(srd, [make_hero()]), "ghost-1", ActiveCondition(id="prone"), content=srd
        )


def test_apply_condition_does_not_change_the_original_state(srd, make_cleric, make_hero):
    state = concentrating_cleric_state(srd, make_cleric, make_hero)
    snapshot = state.model_copy(deep=True)

    after, _ = apply_condition(state, CLERIC, ActiveCondition(id="paralyzed"), content=srd)

    assert state == snapshot
    assert GameState.model_validate_json(after.model_dump_json()) == after


def test_a_monster_hit_that_incapacitates_a_caster_ends_the_concentration_through_the_same_path(
    srd, make_cleric, make_hero
):
    # Удар гуля: то же правило, что и у apply_condition, а не отдельная ветка.
    state = battle(
        srd, [make_cleric(concentration="bless"), make_hero(buffs=[bless()])], ("ghoul",)
    )
    state = turn_of(melee(state, CLERIC, GHOUL), GHOUL)

    after, _ = mob(srd, state, [10, 2, 15, 5], GHOUL, "claw", [CLERIC])

    assert after.party[CLERIC].concentration is None
    assert after.party[HERO].buffs == []


# =============================================================================================
# Удача и Благословение на других бросках к20: инициатива, спасбросок от смерти
# =============================================================================================


def downed_hero(make_hero, **overrides) -> Character:
    fields = {
        "hp": 0,
        "conditions": [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)],
    }
    return make_hero(**(fields | overrides))


def to_the_heroes_next_turn(srd, state: GameState, rolls: list[int]):
    """Ход гоблина кончается, начинается ход лежащего героя: в его начале спасбросок от смерти."""
    return end_turn(turn_of(state, GOBLIN), rng=ScriptedRng(rolls), content=srd)


def test_halfling_luck_rerolls_a_one_on_the_initiative_roll(srd, make_hero):
    hero = make_hero(species="halfling", scores={STR: 16, DEX: 14})  # Лов +2
    rng = ScriptedRng([1, 14, 5])  # единица, перебросок, затем гоблин

    state, events = start_combat(
        state_of(hero),
        encounter_id="t",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters["goblin_warrior"], "hall")],
        pc_zone="hall",
        rng=rng,
        content=srd,
    )

    assert all_dice_spent(rng)
    assert fighter(state, HERO).initiative == 14 + 2
    assert roll_events(events)[0].roll.rolls == [1, 14]


def test_halfling_luck_rerolls_a_natural_one_on_a_death_save(srd, make_hero):
    state = battle(srd, [downed_hero(make_hero, species="halfling")])

    after, events = to_the_heroes_next_turn(srd, state, [1, 15])

    assert after.party[HERO].death_saves.successes == 1
    assert after.party[HERO].death_saves.failures == 0
    assert roll_events(events)[0].success is True


def test_a_natural_one_on_a_death_save_still_counts_two_failures_without_luck(srd, make_hero):
    state = battle(srd, [downed_hero(make_hero, species="human")])

    after, _ = to_the_heroes_next_turn(srd, state, [1])

    assert after.party[HERO].death_saves.failures == 2


def test_bless_adds_its_die_to_a_death_save(srd, make_hero):
    # 8 на к20 — провал, но с благословением 8 + 3 = 11 — успех.
    blessed = battle(srd, [downed_hero(make_hero, buffs=[bless()])])
    plain = battle(srd, [downed_hero(make_hero)])

    with_bless, bless_events = to_the_heroes_next_turn(srd, blessed, [8, 3])
    without, _ = to_the_heroes_next_turn(srd, plain, [8])

    assert (
        with_bless.party[HERO].death_saves.successes,
        without.party[HERO].death_saves.failures,
    ) == (1, 1)
    assert roll_events(bless_events)[0].roll.rolls == [8, 3]


def test_a_natural_twenty_on_a_blessed_death_save_still_wakes_the_character(srd, make_hero):
    state = battle(srd, [downed_hero(make_hero, buffs=[bless()])])

    after, _ = to_the_heroes_next_turn(srd, state, [20, 2])

    assert (hp(after, HERO), after.party[HERO].conditions) == (1, [])


def test_halfling_luck_rerolls_a_one_on_a_weapon_attack(srd, make_hero):
    state = melee(battle(srd, [make_hero(species="halfling")]), HERO, GOBLIN)

    after, events = attack(
        state, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([1, 14, 4]), content=srd
    )

    assert roll_events(events)[0].roll.rolls == [1, 14]
    assert hp(after, GOBLIN) == 10 - 7


def test_a_human_natural_one_on_a_weapon_attack_is_a_miss(srd, make_hero):
    state = melee(battle(srd, [make_hero(species="human")]), HERO, GOBLIN)

    after, events = attack(
        state, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([1]), content=srd
    )

    assert damage_events(events) == [] and hp(after, GOBLIN) == 10


def test_a_monster_attack_gets_neither_luck_nor_a_blessing(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()]), HERO, GOBLIN), GOBLIN)

    _, events = mob(srd, state, [1], GOBLIN, "scimitar", [HERO])

    assert roll_events(events)[0].roll.rolls == [1]


# =============================================================================================
# Согласованность с данными SRD и усиление заклинаний
# =============================================================================================


def test_ids_that_the_engine_relies_on_exist_in_the_srd_data(srd):
    from dnd_engine.combat import engine
    from dnd_engine.rest import AID_BUFF, BREATH_WEAPON_TRAIT

    features = {f.id for cls in srd.classes.values() for f in cls.features}
    traits = {t.id for species in srd.species.values() for t in species.traits}

    assert {
        "second_wind",
        "action_surge",
        "rage",
        "reckless_attack",
        "steady_aim",
        "divine_spark",
        "preserve_life",
        engine.DISCIPLE_OF_LIFE,
        engine.POTENT_CANTRIP,
        engine.FAVORED_ENEMY,
    } <= features
    assert BREATH_WEAPON_TRAIT in traits
    assert srd.spells["aid"].effect.buff == AID_BUFF
    assert engine.MARK_EFFECTS == {
        srd.spells[sid].effect.buff for sid in ("guiding_bolt", "hunters_mark")
    }
    for spell_id, (feature, resource) in engine.FREE_CASTS.items():
        assert feature in features and spell_id in srd.spells
        assert any(resource in cls.resources for cls in srd.classes.values())
    assert engine.INVISIBLE in srd.conditions and engine.RAGE_BUFF == "rage"


def test_class_resources_used_by_actions_exist_in_the_class_tables(srd):
    needed = {
        "fighter": {"second_wind", "action_surge"},
        "barbarian": {"rage"},
        "cleric": {"channel_divinity"},
        "ranger": {"hunters_mark"},
        "wizard": {"arcane_recovery"},
    }

    for class_id, names in needed.items():
        assert names <= set(srd.classes[class_id].resources), class_id


@pytest.mark.parametrize(
    ("spell_id", "base", "higher"),
    [
        ("burning_hands", ["3d6"], ["3d6", "1d6"]),
        ("thunderwave", ["2d8"], ["2d8", "1d8"]),
        ("inflict_wounds", ["2d10"], ["2d10", "1d10"]),
        ("guiding_bolt", ["4d6"], ["4d6", "1d6"]),
        ("cure_wounds", ["2d8"], ["2d8", "2d8"]),
        ("healing_word", ["2d4"], ["2d4", "2d4"]),
    ],
)
def test_a_slot_one_level_higher_adds_the_dice_of_the_upcast_field(
    srd, make_cleric, spell_id, base, higher
):
    cleric = make_cleric(prepared_spells=[spell_id])
    state = melee(battle(srd, [cleric], ("ogre",)), CLERIC, OGRE)
    target = CLERIC if srd.spells[spell_id].effect.kind == "heal" else OGRE

    def dice_terms(slot_level: int) -> list[str]:
        _, events = cast_spell(
            state, CLERIC, spell_id, [target], slot_level=slot_level, rng=FixedRng(15), content=srd
        )
        rolled = next(r for r in roll_events(events) if r.label.startswith(("Урон", "Лечение")))
        return [term for term in rolled.roll.expr.split("+") if "d" in term]

    assert dice_terms(srd.spells[spell_id].level) == base
    assert dice_terms(srd.spells[spell_id].level + 1) == higher


# =============================================================================================
# Модули правил: флаги боя «Специй BG3»
# =============================================================================================


def downed(make_ally, **overrides):
    """Союзник на 0 HP: без сознания, идут спасброски от смерти."""
    fields = {
        "hp": 0,
        "conditions": [ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)],
        "death_saves": DeathSaves(successes=1, failures=2),
    }
    return make_ally(**(fields | overrides))


def test_bg3_flavor_makes_drinking_a_potion_a_bonus_action(srd, make_hero):
    state = melee(battle(srd, [potion_hero(make_hero)]), HERO, GOBLIN)

    drunk, events = act(srd, state, [3, 2], "use_item", item_id="potion_of_healing", rules=BG3)

    assert hp(drunk, HERO) == 17
    assert [i.item_id for i in drunk.party[HERO].inventory] == ["longsword"]
    assert flags(drunk, HERO) == {"bonus_action": True}
    # Действие осталось: после зелья можно ударить.
    struck, _ = attack(
        drunk, HERO, GOBLIN, weapon="longsword", rng=ScriptedRng([12, 5]), content=srd
    )
    assert flags(struck, HERO) == {"bonus_action": True, "action": True}


def test_a_potion_as_a_bonus_action_needs_a_free_bonus_action_but_not_a_free_action(srd, make_hero):
    state = battle(srd, [potion_hero(make_hero, qty=2)])
    flags(state, HERO)["action"] = True

    drunk, _ = act(srd, state, [3, 2], "use_item", item_id="potion_of_healing", rules=BG3)

    assert flags(drunk, HERO) == {"action": True, "bonus_action": True}
    refused_act(
        srd,
        drunk,
        "use_item",
        "бонусное действие уже потрачено",
        item_id="potion_of_healing",
        rules=BG3,
    )


def only_flag(flag: str):
    """Набор правил, где включён единственный флаг: так видно, что каждое действие читает свой."""
    registry = RuleRegistry()
    registry.set_flag(flag, True)
    return registry.build(["single-flag"])


def test_a_potion_and_a_shove_each_read_their_own_flag(srd, make_hero):
    state = melee(battle(srd, [potion_hero(make_hero)]), HERO, GOBLIN)

    potion_rules, shove_rules = only_flag(POTION_BONUS_ACTION), only_flag(SHOVE_BONUS_ACTION)
    drunk_by_potion_flag, _ = act(
        srd, state, [3, 2], "use_item", item_id="potion_of_healing", rules=potion_rules
    )
    drunk_by_shove_flag, _ = act(
        srd, state, [3, 2], "use_item", item_id="potion_of_healing", rules=shove_rules
    )
    shoved_by_shove_flag, _ = act(srd, state, [10], "shove", target_id=GOBLIN, rules=shove_rules)
    shoved_by_potion_flag, _ = act(srd, state, [10], "shove", target_id=GOBLIN, rules=potion_rules)

    assert flags(drunk_by_potion_flag, HERO) == {"bonus_action": True}
    assert flags(drunk_by_shove_flag, HERO) == {"action": True}
    assert flags(shoved_by_shove_flag, HERO) == {"bonus_action": True}
    assert flags(shoved_by_potion_flag, HERO) == {"action": True}


def test_without_the_flag_a_potion_still_costs_the_action(srd, make_hero):
    state = battle(srd, [potion_hero(make_hero)])

    for rules in (CAMPFIRE, build_ruleset(RulesRef())):
        drunk, _ = act(srd, state, [3, 2], "use_item", item_id="potion_of_healing", rules=rules)
        assert flags(drunk, HERO) == {"action": True}
    flags(state, HERO)["action"] = True
    refused_act(
        srd,
        state,
        "use_item",
        "действие уже потрачено",
        item_id="potion_of_healing",
        rules=CAMPFIRE,
    )


def test_bg3_flavor_makes_shove_a_bonus_action_but_not_grapple(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    shoved, _ = act(srd, state, [10], "shove", target_id=GOBLIN, rules=BG3)
    grappled, _ = act(srd, state, [10], "grapple", target_id=GOBLIN, rules=BG3)

    assert flags(shoved, HERO) == {"bonus_action": True}
    assert [c.id for c in fighter(shoved, GOBLIN).conditions] == ["prone"]
    assert flags(grappled, HERO) == {"action": True}


def test_a_shove_as_a_bonus_action_follows_an_attack_and_needs_a_free_bonus_action(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)
    flags(state, HERO)["action"] = True

    shoved, _ = act(srd, state, [10], "shove", target_id=GOBLIN, rules=BG3)

    assert flags(shoved, HERO) == {"action": True, "bonus_action": True}
    refused_act(srd, state, "shove", "действие уже потрачено", target_id=GOBLIN)  # без флага
    flags(state, HERO)["bonus_action"] = True
    refused_act(srd, state, "shove", "бонусное действие уже потрачено", target_id=GOBLIN, rules=BG3)


def test_without_the_flag_a_shove_still_costs_the_action(srd, make_hero):
    state = melee(battle(srd, [make_hero()]), HERO, GOBLIN)

    shoved, _ = act(srd, state, [10], "shove", target_id=GOBLIN, rules=CAMPFIRE)

    assert flags(shoved, HERO) == {"action": True}


def test_help_raises_a_downed_ally_to_1_hp_and_wakes_them_with_help_revives(
    srd, make_hero, make_ally
):
    state = battle(srd, [make_hero(), downed(make_ally)])

    after, events = act(srd, state, [], "help", target_id=ALLY, rules=BG3)

    ally = after.party[ALLY]
    assert (ally.hp, ally.conditions, ally.death_saves) == (1, [], DeathSaves())
    assert flags(after, HERO) == {"action": True}
    assert [(h.target_id, h.amount, h.hp_after) for h in events if isinstance(h, HealEvent)] == [
        (ALLY, 1, 1)
    ]
    assert ConditionEvent(target_id=ALLY, condition="unconscious", added=False) in events
    assert roll_events(events) == []  # «Помощь» ничего не бросает


def test_help_revives_also_a_stable_ally_and_leaves_no_trace_of_the_death_saves(
    srd, make_hero, make_ally
):
    stable = downed(make_ally, death_saves=DeathSaves(stable=True))
    state = battle(srd, [make_hero(), stable])

    after, _ = act(srd, state, [], "help", target_id=ALLY, rules=BG3)

    assert (after.party[ALLY].hp, after.party[ALLY].death_saves) == (1, DeathSaves())


def test_help_revives_only_a_living_ally_on_0_hp_in_the_same_zone(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), downed(make_ally)])
    fighter(state, ALLY).zone_id = "stairs"
    refused_act(srd, state, "help", "вне дальности", target_id=ALLY, rules=BG3)

    dead = battle(srd, [make_hero(), downed(make_ally)])
    dead.party[ALLY].death_saves = DeathSaves(failures=3)  # умер уже в бою
    refused_act(srd, dead, "help", "«Помощь» его не вернёт", target_id=ALLY, rules=BG3)

    standing = battle(srd, [make_hero(), make_ally()])
    refused_act(srd, standing, "help", "на 0 HP", target_id=ALLY, rules=BG3)
    refused_act(srd, standing, "help", "на 0 HP", target_id=HERO, rules=BG3)
    refused_act(srd, standing, "help", "укажите цель", rules=BG3)

    spent = battle(srd, [make_hero(), downed(make_ally)])
    flags(spent, HERO)["action"] = True
    refused_act(srd, spent, "help", "действие уже потрачено", target_id=ALLY, rules=BG3)


def test_help_does_not_revive_without_the_flag(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), downed(make_ally)])

    refused_act(srd, state, "help", "противник", target_id=ALLY)
    refused_act(srd, state, "help", "противник", target_id=ALLY, rules=CAMPFIRE)
    assert state.party[ALLY].hp == 0


def test_help_with_the_flag_still_marks_an_adjacent_enemy(srd, make_hero, make_ally):
    state = melee(battle(srd, [make_hero(), make_ally()]), HERO, GOBLIN)

    helped, _ = act(srd, state, [], "help", target_id=GOBLIN, rules=BG3)

    assert fighter(helped, GOBLIN).conditions == [ActiveCondition(id="helped", source=HERO)]
    refused_act(srd, battle(srd, [make_hero()]), "help", "вплотную", target_id=GOBLIN, rules=BG3)


def test_hide_runs_the_check_hooks_of_the_ruleset(srd, make_hero):
    def always_fails(result):
        return result.model_copy(update={"success": False, "auto": "nat1"})

    registry = RuleRegistry()
    registry.add_check_hook(always_fails)
    strict = registry.build(["strict"])
    state = battle(srd, [make_hero()])

    plain, _ = act(srd, state, [20], "hide")
    hooked, events = act(srd, state, [20], "hide", rules=strict)

    assert plain.party[HERO].conditions == [ActiveCondition(id="invisible", source="hide")]
    assert hooked.party[HERO].conditions == []
    assert roll_events(events)[0].success is False
    assert flags(hooked, HERO) == {"action": True}


def test_a_bg3_flavor_hide_check_with_a_middling_roll_works_as_before(srd, make_hero):
    state = battle(srd, [make_hero(scores={STR: 16, DEX: 14}, skill_proficiencies=[Skill.STEALTH])])

    plain, plain_events = act(srd, state, [12], "hide")
    spiced, spiced_events = act(srd, state, [12], "hide", rules=BG3)

    assert spiced == plain
    assert spiced_events == plain_events


# =============================================================================================
# Случайная игра: инварианты ресурсов и отказов
# =============================================================================================


class CountingRng:
    """Обёртка над `Rng`, считающая обращения: отказ не должен тратить кубы."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return self.inner.randint(a, b)


def random_party(make_hero, make_wizard, make_cleric, make_ranger) -> list[Character]:
    return [
        make_wizard(),
        make_cleric(),
        make_ranger(),
        make_hero(),
        make_barbarian(make_hero, id="pc-5", name="Варвар"),
        make_rogue(make_hero, id="pc-6", name="Плут"),
        dragonborn(make_hero, id="pc-7", name="Драконорождённый"),
    ]


def check_invariants(state: GameState) -> None:
    for c in state.party.values():
        assert 0 <= c.hp <= c.hp_max, c.id
        assert c.temp_hp >= 0, c.id
        assert all(
            0 <= left <= c.spell_slots_max.get(lvl, 0) for lvl, left in c.spell_slots.items()
        ), c.id
        assert all(left >= 0 for left in c.resources.values()), c.id
        assert all(entry.qty >= 1 for entry in c.inventory), c.id
        assert c.hit_dice >= 0
        if c.hp == 0 and not is_dead(c):
            assert any(x.id == "unconscious" for x in c.conditions), c.id
    for f in state.combat.combatants.values():
        if f.kind == "monster":
            assert 0 <= f.hp <= f.hp_max, f.id
        assert all(other in state.combat.combatants for other in f.engaged_with)
        assert all(f.id in state.combat.combatants[o].engaged_with for o in f.engaged_with)
    assert GameState.model_validate_json(state.model_dump_json()) == state


def sensible_targets(state: GameState, actor, spell, pick, choice) -> list[str]:
    """Цели, похожие на разумные: врагов бьют, союзников лечат и усиливают."""
    combat = state.combat
    allies = [i for i, f in combat.combatants.items() if f.side == actor.side]
    enemies = [i for i, f in combat.combatants.items() if f.side != actor.side]
    kind = spell.effect.kind
    if kind in ("heal", "buff") and spell.effect.buff not in ("hunters_mark",):
        return [pick(allies) for _ in range(choice.randint(1, 3))]
    if kind == "utility":
        return []
    return [pick(enemies) for _ in range(choice.randint(1, 3))]


@pytest.mark.parametrize("seed", range(16))
def test_random_play_keeps_resources_valid_and_refusals_harmless(
    srd, make_hero, make_wizard, make_cleric, make_ranger, seed
):
    from dnd_engine.rng import SeededRng

    dice = CountingRng(SeededRng(seed))
    choice = SeededRng(seed + 1000)

    def pick(options):
        return options[choice.randint(0, len(options) - 1)]

    party = random_party(make_hero, make_wizard, make_cleric, make_ranger)
    monsters = ("goblin_warrior", "wolf", "ghoul", "giant_spider", "bandit_captain", "shadow")
    state, _ = start_combat(
        state_of(*party),
        encounter_id="fuzz",
        zones=ZONES,
        links=LINKS,
        monsters=[(srd.monsters[m], pick(["hall", "stairs"])) for m in monsters],
        pc_zone="hall",
        rng=dice,
        content=srd,
    )
    actions = list(simple_action.__annotations__["action"].__args__)
    successes = 0
    for _ in range(250):
        actor = current_actor(state)
        combat = state.combat
        everyone = list(combat.combatants)
        enemies = [i for i in everyone if combat.combatants[i].side != actor.side]
        if actor.kind == "pc":
            c = state.party[actor.id]
            spell = srd.spells[pick([*c.cantrips, *c.prepared_spells] or ["fire_bolt"])]
            action = pick(actions)
            calls = [
                lambda s, r: cast_spell(
                    s,
                    actor.id,
                    spell.id,
                    sensible_targets(s, actor, spell, pick, choice),
                    slot_level=pick([None, None, 1, 2, 3]),
                    rng=r,
                    content=srd,
                ),
                lambda s, r: simple_action(
                    s,
                    actor.id,
                    action,
                    target_id=pick(enemies if action != "use_item" else [actor.id]),
                    item_id="potion_of_healing" if action == "use_item" else None,
                    option=pick([None, "heal", "radiant", "necrotic", "push", "prone", "cold"]),
                    rng=r,
                    content=srd,
                ),
                lambda s, r: attack(
                    s,
                    actor.id,
                    pick(enemies),
                    weapon=pick([i.item_id for i in c.inventory] or ["improvised"]),
                    rng=r,
                    content=srd,
                ),
                lambda s, r: move(
                    s,
                    actor.id,
                    to_zone=pick(["hall", "stairs"]),
                    engage=pick([None, *enemies]),
                    rng=r,
                    content=srd,
                ),
            ]
            if choice.randint(0, 3) == 0:  # чаще вступаем в ближний бой: иначе мало что работает
                calls = [lambda s, r: move(s, actor.id, engage=pick(enemies), rng=r, content=srd)]
        else:
            monster = srd.monsters[actor.ref]
            calls = [
                lambda s, r: monster_action(
                    s,
                    actor.id,
                    pick([a.id for a in monster.actions]),
                    [pick(enemies) for _ in range(choice.randint(1, 2))],
                    rng=r,
                    content=srd,
                ),
                lambda s, r: move(s, actor.id, engage=pick(enemies), rng=r, content=srd),
            ]
        call = pick([*calls, lambda s, r: end_turn(s, rng=r, content=srd)])
        snapshot = state.model_copy(deep=True)
        used = dice.calls
        try:
            state, _ = call(state, dice)
        except CombatError:
            assert state == snapshot
            assert dice.calls == used, "отказ потратил кубы"
        else:
            successes += 1
            check_invariants(state)

    assert successes > 100  # игра действительно идёт, а не только отказы


def test_a_condition_spell_without_a_save_puts_the_condition_on_every_target_without_dice(
    srd, make_wizard
):
    hold = srd.spells["hold_person"]
    effect = hold.effect.model_copy(update={"save": None, "targets": 2})
    custom = srd.model_copy(
        update={"spells": {**srd.spells, "hold_person": hold.model_copy(update={"effect": effect})}}
    )
    state = battle(custom, [make_wizard()], ("goblin_warrior", "goblin_warrior"))

    after, events = cast(custom, state, [], "hold_person", [GOBLIN, GOBLIN_2], slot_level=2)

    held = ActiveCondition(id="paralyzed", rounds_left=10, source=WIZARD)  # без повторного броска
    assert [fighter(after, g).conditions for g in (GOBLIN, GOBLIN_2)] == [[held], [held]]
    assert [(e.target_id, e.condition, e.added) for e in condition_events(events)] == [
        (GOBLIN, "paralyzed", True),
        (GOBLIN_2, "paralyzed", True),
    ]
    assert after.party[WIZARD].spell_slots == {1: 4, 2: 1}
    assert after.party[WIZARD].concentration == "hold_person"


def test_a_condition_added_by_a_hit_skips_a_monster_that_is_immune_to_it(srd, make_wizard):
    bolt = srd.spells["fire_bolt"]
    effect = bolt.effect.model_copy(update={"condition": "restrained"})
    custom = srd.model_copy(
        update={"spells": {**srd.spells, "fire_bolt": bolt.model_copy(update={"effect": effect})}}
    )
    state = battle(custom, [make_wizard(level=2)], ("swarm_of_rats", "goblin_warrior"))

    on_rats, _ = cast(custom, state, [15, 4], "fire_bolt", ["swarm_of_rats-1"])
    on_goblin, _ = cast(custom, state, [15, 4], "fire_bolt", [GOBLIN])

    assert hp(on_rats, "swarm_of_rats-1") == 14 - 4  # урон прошёл
    assert fighter(on_rats, "swarm_of_rats-1").conditions == []  # рой неуязвим к опутыванию
    assert [c.id for c in fighter(on_goblin, GOBLIN).conditions] == ["restrained"]


def test_monster_action_does_not_change_the_original_state(srd, make_hero):
    state = turn_of(melee(battle(srd, [make_hero()], ("wolf",)), HERO, WOLF), WOLF)
    snapshot = state.model_copy(deep=True)

    after, events = mob(srd, state, [10, 3], WOLF, "bite", [HERO])

    assert state == snapshot
    assert after is not state
    assert GameState.model_validate_json(after.model_dump_json()) == after
    assert all(type(e).model_validate_json(e.model_dump_json()) == e for e in events)


def test_a_multiattack_that_names_a_missing_action_is_refused_before_any_roll(srd, make_hero):
    # Кастомный статблок мог обойти проверку ссылок данных SRD.
    flurry = MonsterAction(
        id="flurry", name="Шквал", kind="multiattack", multiattack=("scimitar", "katana")
    )
    goblin = srd.monsters["goblin_warrior"]
    custom = srd_with_monster(srd, "goblin_warrior", actions=(*goblin.actions, flurry))
    state = turn_of(melee(battle(custom, [make_hero()]), HERO, GOBLIN), GOBLIN)

    refused_mob(custom, state, GOBLIN, "flurry", [HERO], "katana")


def test_a_save_action_without_a_save_dc_in_the_statblock_is_refused(srd, make_hero):
    broken = MonsterAction(id="mist", name="Туман", kind="save", range="near")
    goblin = srd.monsters["goblin_warrior"]
    custom = srd.model_copy(
        update={
            "monsters": {
                **srd.monsters,
                "goblin_warrior": goblin.model_copy(update={"actions": (*goblin.actions, broken)}),
            }
        }
    )
    state = turn_of(battle(custom, [make_hero()]), GOBLIN)

    refused_mob(custom, state, GOBLIN, "mist", [HERO], "не заданы")


# =============================================================================================
# B4.7: флаги «раз за ход» живут один ход любого бойца
# =============================================================================================
# По SRD 5.2.1 Скрытая атака, Убийца колоссов и «Свирепый атакующий» срабатывают раз за ход, и
# «ходом» считается чужой тоже: атака по возможности или подготовленная атака в ход противника
# — новый ход, поэтому флаги сбрасываются в начале каждого хода боя, а не только своего.

ONCE_PER_TURN = ("sneak_attack_used", "colossus_slayer_used", "savage_attacker_used")


def rogue_vs_blinded_ogre(srd, make_character) -> GameState:
    """Плут вплотную с ослеплённым огром: у атаки по нему преимущество, значит, Скрытая атака есть."""
    rogue = make_character(
        id=HERO,
        name="Плут",
        class_id="rogue",
        background="acolyte",
        scores={DEX: 16},
        inventory=[item("rapier")],
    )
    state = melee(battle(srd, [rogue], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).conditions.append(ActiveCondition(id="blinded"))
    return state


SNEAK_ROLLS = [10, 12, 4, 3, 5]  # к20 с преимуществом, 1к8 рапиры, 2к6 Скрытой атаки


def sneak_attacked(events) -> bool:
    return any(label.startswith("Скрытая атака") for label in labels(events))


def test_the_once_per_turn_flags_of_every_fighter_are_cleared_when_any_turn_begins(
    srd, make_character
):
    state = rogue_vs_blinded_ogre(srd, make_character)
    flags(state, HERO).update(dict.fromkeys(ONCE_PER_TURN, True))

    after, _ = end_turn(state, rng=ScriptedRng([]), content=srd)

    assert current_actor(after).id == OGRE  # начался чужой ход, а флаги героя уже сброшены
    assert flags(after, HERO) == {}


def test_only_once_per_turn_flags_are_cleared_on_someone_elses_turn(srd, make_character):
    state = rogue_vs_blinded_ogre(srd, make_character)
    kept = {"action": True, "moved": True, "reaction": True, "readied": True, "frenzy_used": True}
    flags(state, HERO).update(kept | dict.fromkeys(ONCE_PER_TURN, True))

    after, _ = end_turn(state, rng=ScriptedRng([]), content=srd)

    # Реакция, «Подготовка» и расход хода живут до собственного хода; «Бешенство» — «в свой ход».
    assert flags(after, HERO) == kept


def test_the_once_per_turn_flags_of_the_new_actor_are_cleared_like_all_flags(srd, make_character):
    state = rogue_vs_blinded_ogre(srd, make_character)
    flags(state, OGRE).update(dict.fromkeys(ONCE_PER_TURN, True) | {"action": True})

    after, _ = end_turn(state, rng=ScriptedRng([]), content=srd)

    assert flags(after, OGRE) == {}


def test_a_rogue_sneak_attacks_again_with_an_opportunity_attack_on_the_enemys_turn(
    srd, make_character
):
    state = rogue_vs_blinded_ogre(srd, make_character)
    struck, first = attack(
        state, HERO, OGRE, weapon="rapier", rng=ScriptedRng(SNEAK_ROLLS), content=srd
    )
    assert sneak_attacked(first)
    assert flags(struck, HERO)["sneak_attack_used"]
    ogre_turn, _ = end_turn(struck, rng=ScriptedRng([]), content=srd)

    # Огр уходит: герой бьёт по возможности — это другой ход, Скрытая атака снова доступна.
    moved, events = move(
        ogre_turn, OGRE, to_zone="stairs", rng=ScriptedRng(SNEAK_ROLLS), content=srd
    )

    assert sneak_attacked(events)
    assert flags(moved, HERO) == {"action": True, "reaction": True, "sneak_attack_used": True}


def test_a_readied_attack_of_a_rogue_on_the_enemys_turn_sneak_attacks_again(srd, make_character):
    state = rogue_vs_blinded_ogre(srd, make_character)
    struck, _ = attack(
        state, HERO, OGRE, weapon="rapier", rng=ScriptedRng(SNEAK_ROLLS), content=srd
    )
    ogre_turn, _ = end_turn(struck, rng=ScriptedRng([]), content=srd)
    flags(ogre_turn, HERO)["readied"] = True  # «Подготовка» из прошлого круга, условие наступило

    after, events = attack(
        ogre_turn,
        HERO,
        OGRE,
        weapon="rapier",
        reaction=True,
        rng=ScriptedRng(SNEAK_ROLLS),
        content=srd,
    )

    assert sneak_attacked(events)
    assert flags(after, HERO) == {"action": True, "reaction": True, "sneak_attack_used": True}


def own_attack_then_opportunity_attack(srd, state, own_rolls, opportunity_rolls, weapon):
    """Атака героя в свой ход, затем огр уходит и герой бьёт по возможности в чужой ход."""
    struck, first = attack(
        state, HERO, OGRE, weapon=weapon, rng=ScriptedRng(own_rolls), content=srd
    )
    ogre_turn, _ = end_turn(struck, rng=ScriptedRng([]), content=srd)
    _, second = move(
        ogre_turn, OGRE, to_zone="stairs", rng=ScriptedRng(opportunity_rolls), content=srd
    )
    return first, second


def test_colossus_slayer_adds_its_die_again_to_an_opportunity_attack_on_the_enemys_turn(
    srd, make_character
):
    ranger = make_character(
        id=HERO,
        name="Следопыт",
        class_id="ranger",
        background="acolyte",
        scores={STR: 16},
        inventory=[item("longsword")],
    )
    state = melee(battle(srd, [ranger], ("ogre",)), HERO, OGRE)
    fighter(state, OGRE).hp = 60  # ранен: Убийца колоссов бьёт только по раненым

    first, second = own_attack_then_opportunity_attack(
        srd, state, [10, 4, 5], [10, 4, 5], "longsword"
    )

    assert labels(first)[2].startswith("Убийца колоссов")
    assert labels(second)[2].startswith("Убийца колоссов")


def test_savage_attacker_rolls_the_damage_twice_again_on_an_opportunity_attack(srd, make_hero):
    state = melee(battle(srd, [make_hero(background="soldier")], ("ogre",)), HERO, OGRE)

    first, second = own_attack_then_opportunity_attack(
        srd, state, [10, 2, 6], [10, 2, 6], "longsword"
    )

    assert len(roll_events(first)) == len(roll_events(second)) == 3  # к20 и две кости урона


def test_sneak_attack_still_happens_once_within_one_turn(srd, make_character):
    state = rogue_vs_blinded_ogre(srd, make_character)
    struck, _ = attack(
        state, HERO, OGRE, weapon="rapier", rng=ScriptedRng(SNEAK_ROLLS), content=srd
    )
    flags(struck, HERO)["action"] = False  # второе действие того же хода (например, Всплеск)

    _, again = attack(
        struck, HERO, OGRE, weapon="rapier", rng=ScriptedRng([10, 12, 4]), content=srd
    )

    assert not sneak_attacked(again)


# =============================================================================================
# B4.7: снятие внешнего состояния (apply_effect с add=false)
# =============================================================================================


def test_a_state_applied_by_the_master_can_be_removed_from_a_character_and_a_monster(
    srd, make_hero
):
    state = battle(srd, [make_hero(conditions=[ActiveCondition(id="frightened")])])
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="prone"))

    off_hero, hero_events = combat_engine.remove_condition(state, HERO, "frightened", content=srd)
    off_goblin, goblin_events = combat_engine.remove_condition(state, GOBLIN, "prone", content=srd)

    assert condition_ids(off_hero, HERO) == [] and condition_ids(state, HERO) == ["frightened"]
    assert hero_events == [ConditionEvent(target_id=HERO, condition="frightened", added=False)]
    assert condition_ids(off_goblin, GOBLIN) == []
    assert goblin_events == [ConditionEvent(target_id=GOBLIN, condition="prone", added=False)]


def test_prone_and_grappled_left_by_a_shove_or_a_grapple_can_be_removed(srd, make_hero):
    # Сбитого с ног и схваченного (без срока, источник — боец) больше ничто не отпустит.
    state = battle(srd, [make_hero()])
    fighter(state, GOBLIN).conditions += [
        ActiveCondition(id="prone", source=HERO),
        ActiveCondition(id="grappled", source=HERO),
    ]

    after, _ = combat_engine.remove_condition(state, GOBLIN, "grappled", content=srd)
    after, _ = combat_engine.remove_condition(after, GOBLIN, "prone", content=srd)

    assert condition_ids(after, GOBLIN) == []


def test_removal_drops_every_removable_record_of_the_state_and_keeps_the_rest(srd, make_hero):
    state = battle(srd, [make_hero()])
    fighter(state, GOBLIN).conditions += [
        ActiveCondition(id="prone"),
        ActiveCondition(id="prone", source=HERO),
        ActiveCondition(id="poisoned"),
    ]

    after, events = combat_engine.remove_condition(state, GOBLIN, "prone", content=srd)

    assert condition_ids(after, GOBLIN) == ["poisoned"]
    assert len(events) == 2


def test_the_zero_hp_and_hiding_records_belong_to_the_engine_and_stay(srd, make_hero):
    down = make_hero(hp=0, conditions=[ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)])
    state = battle(srd, [down])
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="invisible", source=HIDDEN_SOURCE))

    refused(
        lambda s, rng: combat_engine.remove_condition(s, HERO, "unconscious", content=srd),
        state,
        "служебн",
    )
    refused(
        lambda s, rng: combat_engine.remove_condition(s, GOBLIN, "invisible", content=srd),
        state,
        "служебн",
    )


def test_spell_records_with_a_repeated_save_or_held_by_concentration_stay(srd, make_cleric):
    cleric = make_cleric(concentration="hold_person")
    state = battle(srd, [cleric])
    held = fighter(state, GOBLIN)
    held.conditions.append(
        ActiveCondition(id="paralyzed", rounds_left=10, source=CLERIC, save=WIS, dc=13)
    )
    refused(
        lambda s, rng: combat_engine.remove_condition(s, GOBLIN, "paralyzed", content=srd),
        state,
        "заклинани",
    )

    # Без повторного спасброска запись всё равно принадлежит концентрации заклинателя.
    held.conditions = [ActiveCondition(id="paralyzed", rounds_left=10, source=CLERIC)]
    refused(
        lambda s, rng: combat_engine.remove_condition(s, GOBLIN, "paralyzed", content=srd),
        state,
        "заклинани",
    )


def test_removing_a_state_that_is_not_there_or_a_target_that_is_missing_is_refused(srd, make_hero):
    state = battle(srd, [make_hero()])

    refused(
        lambda s, rng: combat_engine.remove_condition(s, HERO, "prone", content=srd),
        state,
        "нет состояния",
    )
    refused(
        lambda s, rng: combat_engine.remove_condition(s, "ghost-1", "prone", content=srd),
        state,
        "ghost-1",
    )


# =============================================================================================
# B4.7: ход бойца в нужный момент (advance_to) и предметы приключения (carried_items)
# =============================================================================================


def two_heroes_and_goblins(srd, make_hero, make_ally) -> GameState:
    """Очередь: герой (20), союзник (19), гоблин 1 (5), гоблин 2 (4)."""
    return battle(srd, [make_hero(), make_ally()], ("goblin_warrior", "goblin_warrior"))


def test_advance_to_passes_the_turn_to_a_fighter_after_a_run_of_fighters_who_cannot_act(
    srd, make_hero, make_ally
):
    # Союзник лежит на 0 HP: его ход проходит вхолостую (спасбросок от смерти), затем ход гоблина.
    state = battle(srd, [make_hero(), downed(make_ally)])

    after, events = combat_engine.advance_to(state, GOBLIN, rng=ScriptedRng([12]), content=srd)

    assert current_actor(after).id == GOBLIN
    assert [e.actor_id for e in events if isinstance(e, CombatEvent)] == [ALLY, GOBLIN]
    assert labels(events) == ["Спасбросок от смерти"]
    assert current_actor(state).id == HERO  # исходное состояние не изменилось


def test_advance_to_refuses_when_a_fallen_fighter_stands_up_on_the_way(srd, make_hero, make_ally):
    # Союзник лежит: проверка до передачи хода его пропускает. Но натуральная 20 спасброска от
    # смерти в начале его хода ставит его на ноги (1 HP), и теперь его ход не пропустить.
    state = battle(srd, [make_hero(), downed(make_ally)])
    snapshot = state.model_copy(deep=True)

    with pytest.raises(CombatError, match="сначала ходит"):
        combat_engine.advance_to(state, GOBLIN, rng=ScriptedRng([20]), content=srd)

    assert state == snapshot


def test_advance_to_still_passes_a_fallen_fighter_who_stays_down(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), downed(make_ally)])

    after, _ = combat_engine.advance_to(state, GOBLIN, rng=ScriptedRng([19]), content=srd)

    assert current_actor(after).id == GOBLIN and after.party[ALLY].hp == 0


def test_advance_to_the_current_fighter_changes_nothing(srd, make_hero):
    state = battle(srd, [make_hero()])

    after, events = combat_engine.advance_to(state, HERO, rng=ScriptedRng([]), content=srd)

    assert (after, events) == (state, [])


def test_advance_to_refuses_to_jump_over_a_fighter_who_can_act(srd, make_hero, make_ally):
    state = two_heroes_and_goblins(srd, make_hero, make_ally)

    refused(
        lambda s, rng: combat_engine.advance_to(s, GOBLIN, rng=rng, content=srd),
        state,
        "сначала",
    )
    refused(
        lambda s, rng: combat_engine.advance_to(s, "goblin_warrior-2", rng=rng, content=srd),
        state,
        "сначала",
    )


def test_advance_to_passes_a_paralyzed_monster_in_the_middle_of_the_order(srd, make_hero):
    state = battle(srd, [make_hero()], ("goblin_warrior", "goblin_warrior"))
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="paralyzed", rounds_left=3))

    after, _ = combat_engine.advance_to(state, "goblin_warrior-2", rng=ScriptedRng([]), content=srd)

    assert current_actor(after).id == "goblin_warrior-2"


def test_advance_to_the_next_round_wraps_around_the_order(srd, make_hero):
    state = turn_of(battle(srd, [make_hero()]), GOBLIN)

    after, _ = combat_engine.advance_to(state, HERO, rng=ScriptedRng([]), content=srd)

    assert (current_actor(after).id, after.combat.round) == (HERO, 2)


def test_advance_to_refuses_a_fighter_who_cannot_act_or_is_not_in_the_battle(srd, make_hero):
    state = battle(srd, [make_hero()], ("goblin_warrior", "goblin_warrior"))
    fighter(state, "goblin_warrior-2").hp = 0

    refused(
        lambda s, rng: combat_engine.advance_to(s, "goblin_warrior-2", rng=rng, content=srd),
        state,
        "не может действовать",
    )
    refused(
        lambda s, rng: combat_engine.advance_to(s, "orc-1", rng=rng, content=srd),
        state,
        "в бою нет",
    )


def test_can_act_means_standing_and_not_incapacitated(srd, make_hero, make_ally):
    state = battle(srd, [make_hero(), downed(make_ally)])
    fighter(state, GOBLIN).conditions.append(ActiveCondition(id="stunned", rounds_left=1))

    assert combat_engine.can_act(state, HERO)
    assert not combat_engine.can_act(state, ALLY)  # лежит
    assert not combat_engine.can_act(state, GOBLIN)  # оглушён


def adventure_potion_hero(make_hero, **overrides):
    return make_hero(hp_max=30, hp=10, inventory=[item("longsword")], **overrides)


CHEST_POTION = {"healing-potion": "potion_of_healing"}  # предмет приключения -> предмет SRD


def test_an_adventure_item_carried_outside_the_sheet_is_drunk_with_the_srd_potion_mechanics(
    srd, make_hero
):
    state = battle(srd, [adventure_potion_hero(make_hero)])

    after, events = act(
        srd, state, [3, 2], "use_item", item_id="healing-potion", carried_items=CHEST_POTION
    )

    assert (roll_events(events)[0].roll.expr, hp(after, HERO)) == ("2d4+2", 17)
    assert flags(after, HERO) == {"action": True}
    # Лист персонажа не менялся: расход предмета приключения ведёт мир (`item_holders`).
    assert after.party[HERO].inventory == state.party[HERO].inventory


def test_an_adventure_item_follows_the_bonus_action_flag_of_the_modules(srd, make_hero):
    state = battle(srd, [adventure_potion_hero(make_hero)])

    drunk, _ = act(
        srd,
        state,
        [3, 2],
        "use_item",
        item_id="healing-potion",
        carried_items=CHEST_POTION,
        rules=BG3,
    )

    assert flags(drunk, HERO) == {"bonus_action": True}


def test_an_adventure_item_still_obeys_the_potion_rules_and_unknown_items_are_refused(
    srd, make_hero, make_ally
):
    state = battle(srd, [adventure_potion_hero(make_hero), make_ally()])
    fighter(state, ALLY).zone_id = "stairs"

    refused_act(
        srd,
        state,
        "use_item",
        "вне дальности",
        item_id="healing-potion",
        target_id=ALLY,
        carried_items=CHEST_POTION,
    )
    refused_act(
        srd,
        state,
        "use_item",
        "нельзя применить",
        item_id="rope-of-doom",
        carried_items={"rope-of-doom": "rope"},
    )
    refused_act(srd, state, "use_item", "нет предмета", item_id="healing-potion")


# =============================================================================================
# B4.7: конец сцены (end_scene)
# =============================================================================================


def crypt_battle(srd, make_hero, monsters=("cultist_fanatic", "skeleton")) -> GameState:
    state = battle(srd, [make_hero()], monsters)
    state.location_id = "crypt"
    state.combat.encounter_id = "fabian-fight"
    return state


def leave(srd, demo_adventure, state, next_type=SceneType.EXPLORATION, rules=None):
    rules = rules or build_ruleset(RulesRef())
    return combat_engine.end_scene(state, next_type, rules, content=srd, adventure=demo_adventure)


def test_end_scene_closes_the_battle_marks_the_encounter_and_starts_the_next_scene(
    srd, demo_adventure, make_hero
):
    state = crypt_battle(srd, make_hero)
    state.scene.inspiration_granted = [HERO]
    state.turn = 7
    for monster in (fighter(state, "cultist_fanatic-1"), fighter(state, "skeleton-1")):
        monster.hp = 0

    after, events = leave(srd, demo_adventure, state)

    assert after.combat is None
    assert after.world.flags["encounter:fabian-fight"] == "done"
    assert (after.scene.type, after.scene.started_turn) == (SceneType.EXPLORATION, 7)
    assert after.scene.inspiration_granted == []  # вдохновение считается за сцену
    assert events == [
        CombatEvent(phase="ended", winner="party"),
        SceneEvent(scene=SceneType.EXPLORATION),
    ]
    assert state.combat is not None and state.scene.type == SceneType.COMBAT  # вход цел


def test_end_scene_reports_no_winner_when_both_sides_still_stand(srd, demo_adventure, make_hero):
    state = crypt_battle(srd, make_hero)

    after, events = leave(srd, demo_adventure, state, SceneType.SOCIAL)

    assert after.scene.type == SceneType.SOCIAL
    assert CombatEvent(phase="ended", winner=None) in events
    assert after.world.npc_alive == {}  # противники живы: ни один NPC не отмечен погибшим


def test_a_fallen_npc_with_a_statblock_is_marked_dead_and_the_living_ones_are_not(
    srd, demo_adventure, make_hero
):
    state = crypt_battle(srd, make_hero)
    fighter(state, "cultist_fanatic-1").hp = 0

    after, _ = leave(srd, demo_adventure, state)

    assert after.world.npc_alive == {"fabian": False}


def test_an_npc_is_marked_dead_only_in_the_location_where_the_battle_happened(
    srd, demo_adventure, make_hero
):
    state = crypt_battle(srd, make_hero)
    state.location_id = "cemetery"  # Фабиан в склепе, а гибнет другой культист на кладбище
    fighter(state, "cultist_fanatic-1").hp = 0

    after, _ = leave(srd, demo_adventure, state)

    assert after.world.npc_alive == {}


def test_end_scene_removes_timed_effects_and_keeps_the_open_ended_ones(
    srd, demo_adventure, make_hero, make_cleric
):
    hero = make_hero(
        conditions=[
            ActiveCondition(id="frightened", rounds_left=3),
            ActiveCondition(id="prone"),
        ],
        buffs=[ActiveBuff(id="rage", rounds_left=8, source_id=HERO)],
    )
    state = battle(srd, [hero, make_cleric(concentration="bless")])
    state.party[HERO].buffs.append(bless())
    state.party[CLERIC].buffs.append(ActiveBuff(id="aid", rounds_left=None, source_id=CLERIC))

    after, events = leave(srd, demo_adventure, state)

    assert condition_ids(after, HERO) == ["prone"]  # без срока: снимает только мастер
    assert after.party[HERO].buffs == []
    assert after.party[CLERIC].concentration is None  # эффекты концентрации кончились вместе с ней
    assert after.party[CLERIC].buffs == [ActiveBuff(id="aid", rounds_left=None, source_id=CLERIC)]
    removed = {(e.target_id, e.condition) for e in events if isinstance(e, ConditionEvent)}
    assert removed == {(HERO, "frightened"), (HERO, "rage"), (HERO, "bless")}


def test_end_scene_returns_the_maximum_hp_of_an_expiring_aid(srd, demo_adventure, make_hero):
    aid = ActiveBuff(id="aid", rounds_left=40, source_id=ALLY, data={"hp": 5})
    state = battle(srd, [make_hero(hp_max=25, hp=25, buffs=[aid])])

    after, _ = leave(srd, demo_adventure, state)

    assert (after.party[HERO].hp_max, after.party[HERO].hp) == (20, 20)


def camp_state(make_hero) -> GameState:
    state = state_of(make_hero(hp=4, spell_slots={}))
    state.scene.type = SceneType.CAMP
    state.short_rests_since_long = 2
    return state


def test_end_scene_runs_the_scene_end_triggers_before_the_scene_changes(
    srd, demo_adventure, make_hero
):
    rules = build_ruleset(RulesRef(modules=["campfire"]))

    after, events = leave(srd, demo_adventure, camp_state(make_hero), rules=rules)

    # Триггер «Привала» видит сцену camp: длинный отдых и утро следующего дня уже посчитаны.
    assert after.party[HERO].hp == after.party[HERO].hp_max
    assert (after.time.day, after.time.part, after.short_rests_since_long) == (2, "morning", 0)
    assert after.scene.type == SceneType.EXPLORATION
    assert [type(e) for e in events] == [RestEvent, SceneEvent]


def test_without_the_campfire_module_the_end_of_a_camp_gives_no_rest(
    srd, demo_adventure, make_hero
):
    after, events = leave(
        srd, demo_adventure, camp_state(make_hero), rules=build_ruleset(RulesRef())
    )

    assert after.party[HERO].hp == 4 and after.time.day == 1
    assert events == [SceneEvent(scene=SceneType.EXPLORATION)]


def test_end_scene_applies_the_triggers_in_order_and_each_sees_the_previous_result(
    srd, demo_adventure, make_hero
):
    registry = RuleRegistry()
    seen = []

    def first(state, content):
        seen.append(("first", state.scene.type, state.time.day))
        moved = state.model_copy(deep=True)
        moved.time.day += 10
        return moved, [ActionEvent(actor_id="first", action="trigger")]

    def second(state, content):
        seen.append(("second", state.scene.type, state.time.day))
        return state.model_copy(deep=True), [ActionEvent(actor_id="second", action="trigger")]

    registry.add_trigger("on_scene_end", first)
    registry.add_trigger("on_scene_end", second)

    after, events = leave(
        srd,
        demo_adventure,
        state_of(make_hero()),
        SceneType.SOCIAL,
        rules=registry.build(["test"]),
    )

    assert seen == [("first", SceneType.EXPLORATION, 1), ("second", SceneType.EXPLORATION, 11)]
    assert after.time.day == 11
    assert [e.actor_id for e in events if isinstance(e, ActionEvent)] == ["first", "second"]


def test_end_scene_refuses_the_same_scene_and_a_battle_without_changing_the_state(
    srd, demo_adventure, make_hero
):
    state = state_of(make_hero())

    refused(lambda s, rng: leave(srd, demo_adventure, s, SceneType.EXPLORATION), state, "уже")
    refused(lambda s, rng: leave(srd, demo_adventure, s, SceneType.COMBAT), state, "start_combat")
