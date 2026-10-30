"""Модули правил: реестр, сборка `RuleSet`, «Специи BG3» и «Привал у костра».

Хук `bg3_flavor` проверяется на настоящих проверках и спасбросках с `ScriptedRng`, триггер
`campfire` — на настоящем `GameState` с отдыхом персонажей. Флаги, которые читает бой, проверяет
`test_combat_actions.py`.
"""

import pytest
from pydantic import ValidationError

from dnd_engine.abilities import Ability, Skill
from dnd_engine.character import ActiveBuff, ActiveCondition, Character, DeathSaves
from dnd_engine.checks import CheckKind, CheckResult, ability_check, saving_throw
from dnd_engine.combat.engine import end_scene
from dnd_engine.combat.model import Combat, Zone
from dnd_engine.conditions import ZERO_HP_SOURCE
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage
from dnd_engine.events import RestEvent, SceneEvent
from dnd_engine.modules import MODULES, build_ruleset
from dnd_engine.modules.base import (
    HELP_REVIVES,
    MAX_SHORT_RESTS_PER_LONG,
    NO_RULES,
    POTION_BONUS_ACTION,
    SHOVE_BONUS_ACTION,
    RuleRegistry,
    RuleSet,
)
from dnd_engine.modules.campfire import CAMP_FRAGMENT, LONG_REST_WAKE_HOUR, begin_rest
from dnd_engine.rest import AID_BUFF, SHORT_REST_HOURS, RestError, RestOutcome, spend_hit_die
from dnd_engine.rng import ScriptedRng, SeededRng
from dnd_engine.state import GameState, GameTime, RulesRef, Scene, SceneType

DEX = Ability.DEX
NONE, ADVANTAGE = Advantage.NONE, Advantage.ADVANTAGE

BG3_FLAGS = {
    POTION_BONUS_ACTION: True,
    SHOVE_BONUS_ACTION: True,
    HELP_REVIVES: True,
    MAX_SHORT_RESTS_PER_LONG: 2,
}


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


def ruleset_of(*module_ids: str) -> RuleSet:
    return build_ruleset(RulesRef(modules=list(module_ids)))


# Функции-заглушки для реестра: важна только их идентичность, а не поведение.
def keep_result(result: CheckResult) -> CheckResult:
    return result


def keep_state(state: GameState, content: SrdContent):
    return state, []


# =============================================================================================
# Реестр и сборка набора
# =============================================================================================


def test_registry_builds_a_ruleset_from_everything_the_modules_registered():
    spec = object()  # спецификации инструментов реестр только хранит
    registry = RuleRegistry()
    registry.add_check_hook(keep_result)
    registry.add_trigger("on_scene_end", keep_state)
    registry.add_trigger("on_scene_end", keep_state)
    registry.add_trigger("on_long_rest", keep_state)
    registry.add_tool(spec)
    registry.add_prompt_fragment("первый")
    registry.add_prompt_fragment("второй")
    registry.set_flag("flag", True)
    registry.set_flag("limit", 2)

    rules = registry.build(["demo"])

    assert rules.modules == ("demo",)
    assert rules.check_hooks == (keep_result,)
    assert rules.tools == (spec,)
    assert rules.prompt_fragments == ("первый", "второй")
    assert rules.flags == {"flag": True, "limit": 2}
    assert rules.triggers == {
        "on_scene_end": (keep_state, keep_state),
        "on_long_rest": (keep_state,),
    }


def test_flags_keep_their_types_and_a_flag_cannot_be_set_to_two_different_values():
    registry = RuleRegistry()
    registry.set_flag("flag", False)
    registry.set_flag("limit", 2)
    registry.set_flag("limit", 2)  # то же значение второй раз — не конфликт

    rules = registry.build([])

    assert (rules.flags["flag"], rules.flags["limit"]) == (False, 2)
    assert type(rules.flags["flag"]) is bool
    assert type(rules.flags["limit"]) is int
    with pytest.raises(ValueError, match="limit"):
        registry.set_flag("limit", 3)


def test_a_trigger_for_an_unknown_event_is_refused_when_the_ruleset_is_built():
    registry = RuleRegistry()
    registry.add_trigger("on_full_moon", keep_state)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        registry.build([])


def test_a_built_ruleset_does_not_change_when_the_registry_is_used_again():
    registry = RuleRegistry()
    registry.add_check_hook(keep_result)
    registry.add_trigger("on_scene_end", keep_state)
    registry.set_flag("flag", True)
    rules = registry.build(["demo"])

    registry.add_check_hook(keep_result)
    registry.add_trigger("on_scene_end", keep_state)
    registry.add_trigger("on_scene_start", keep_state)
    registry.add_prompt_fragment("позже")
    registry.set_flag("later", True)

    assert rules.check_hooks == (keep_result,)
    assert rules.triggers == {"on_scene_end": (keep_state,)}
    assert rules.prompt_fragments == ()
    assert rules.flags == {"flag": True}


def test_a_ruleset_is_immutable():
    rules = ruleset_of("bg3_flavor", "campfire")

    with pytest.raises(ValidationError):
        rules.modules = ()
    with pytest.raises(ValidationError):
        rules.flags = {}
    with pytest.raises(TypeError):
        rules.flags[POTION_BONUS_ACTION] = False
    with pytest.raises(TypeError):
        rules.triggers["on_scene_start"] = ()
    with pytest.raises(TypeError):
        NO_RULES.flags["anything"] = True
    assert rules.flags[POTION_BONUS_ACTION] is True


def test_no_rules_is_an_empty_ruleset_and_matches_building_without_modules():
    assert NO_RULES.modules == ()
    assert NO_RULES.check_hooks == ()
    assert NO_RULES.tools == ()
    assert NO_RULES.prompt_fragments == ()
    assert dict(NO_RULES.flags) == {}
    assert dict(NO_RULES.triggers) == {}
    assert build_ruleset(RulesRef()) == NO_RULES


def test_both_modules_are_known_and_have_ids_matching_their_keys():
    assert set(MODULES) == {"bg3_flavor", "campfire"}
    for module_id, module in MODULES.items():
        assert module.id == module_id
        assert module.name


def test_the_module_table_is_read_only():
    with pytest.raises(TypeError):
        MODULES["homebrew"] = MODULES["campfire"]  # type: ignore[index]


def test_a_ruleset_with_both_modules_has_the_hook_flags_and_prompt_fragments():
    rules = ruleset_of("bg3_flavor", "campfire")

    assert rules.modules == ("bg3_flavor", "campfire")
    assert len(rules.check_hooks) == 1
    assert dict(rules.flags) == BG3_FLAGS
    # Фрагменты идут в порядке модулей приключения: это порядок разделов промпта мастера.
    assert rules.prompt_fragments == (
        ruleset_of("bg3_flavor").prompt_fragments + ruleset_of("campfire").prompt_fragments
    )
    assert all(fragment.strip() for fragment in rules.prompt_fragments)
    assert set(rules.triggers) == {"on_scene_end"}
    assert len(rules.triggers["on_scene_end"]) == 1


def test_bg3_flavor_alone_brings_flags_hook_and_prompt_but_no_camp():
    rules = ruleset_of("bg3_flavor")

    assert dict(rules.flags) == BG3_FLAGS
    assert len(rules.check_hooks) == 1
    assert dict(rules.triggers) == {}
    prompt = "\n".join(rules.prompt_fragments)
    assert "grant_inspiration" in prompt
    assert "start_camp" not in prompt


def test_campfire_alone_brings_the_camp_trigger_and_prompt_but_no_flags_or_hooks():
    rules = ruleset_of("campfire")

    assert dict(rules.flags) == {}
    assert rules.check_hooks == ()
    prompt = "\n".join(rules.prompt_fragments)
    assert "start_camp" in prompt
    assert "grant_inspiration" not in prompt
    assert set(rules.triggers) == {"on_scene_end"}


def test_a_module_listed_twice_is_registered_once():
    rules = ruleset_of("campfire", "bg3_flavor", "campfire", "bg3_flavor")

    assert rules.modules == ("campfire", "bg3_flavor")
    assert len(rules.triggers["on_scene_end"]) == 1
    assert len(rules.check_hooks) == 1
    assert rules.prompt_fragments == ruleset_of("campfire", "bg3_flavor").prompt_fragments


def test_an_unknown_module_is_refused_with_its_id_in_the_message():
    with pytest.raises(ValueError, match="homebrew"):
        ruleset_of("bg3_flavor", "homebrew")


# =============================================================================================
# bg3_flavor: натуральные 20 и 1 на проверках
# =============================================================================================


@pytest.fixture
def bg3_hooks():
    return ruleset_of("bg3_flavor").check_hooks


def skill_check(c: Character, rolls, *, dc: int, hooks=(), advantage=NONE) -> CheckResult:
    return ability_check(
        c,
        DEX,
        skill=Skill.STEALTH,
        dc=dc,
        advantage=advantage,
        rng=ScriptedRng(rolls),
        reason="Скрытность",
        hooks=hooks,
    )


def test_a_natural_20_on_a_skill_check_succeeds_against_dc_25_only_with_bg3_flavor(
    make_character, bg3_hooks
):
    hero = make_character()  # бонус навыка 0: 20 на кубе не дотягивает до Сл 25

    plain = skill_check(hero, [20], dc=25)
    spiced = skill_check(hero, [20], dc=25, hooks=bg3_hooks)

    assert (plain.success, plain.auto) == (False, None)
    assert (spiced.success, spiced.auto) == (True, "nat20")
    assert spiced.total == plain.total == 20  # итог на кубах не подменяется, меняется исход


def test_a_natural_20_on_a_plain_ability_check_also_succeeds_against_the_hardest_dc(
    make_character, bg3_hooks
):
    result = ability_check(
        make_character(),
        Ability.STR,
        skill=None,
        dc=30,
        advantage=NONE,
        rng=ScriptedRng([20]),
        reason="Выломать дверь",
        hooks=bg3_hooks,
    )

    assert (result.kind, result.success, result.auto) == (CheckKind.ABILITY, True, "nat20")


def test_a_natural_1_fails_a_skill_check_even_when_the_bonus_would_have_passed_it(
    make_character, bg3_hooks
):
    rogue = make_character(scores={DEX: 20}, skill_proficiencies=[Skill.STEALTH])  # бонус +7

    plain = skill_check(rogue, [1], dc=5)
    spiced = skill_check(rogue, [1], dc=5, hooks=bg3_hooks)

    assert (plain.total, plain.success, plain.auto) == (8, True, None)
    assert (spiced.total, spiced.success, spiced.auto) == (8, False, "nat1")


def test_the_hook_looks_at_the_kept_die_when_rolling_with_advantage(make_character, bg3_hooks):
    hero = make_character()

    best = skill_check(hero, [1, 20], dc=30, hooks=bg3_hooks, advantage=ADVANTAGE)
    worst = skill_check(
        make_character(scores={DEX: 20}),
        [20, 1],
        dc=5,
        hooks=bg3_hooks,
        advantage=Advantage.DISADVANTAGE,
    )

    assert (best.success, best.auto) == (True, "nat20")  # оставлена двадцатка
    assert (worst.success, worst.auto) == (False, "nat1")  # оставлена единица


@pytest.mark.parametrize("natural", [2, 10, 19])
def test_other_rolls_are_left_to_the_dc(make_character, bg3_hooks, natural):
    hero = make_character()

    plain = skill_check(hero, [natural], dc=15)
    spiced = skill_check(hero, [natural], dc=15, hooks=bg3_hooks)

    assert spiced == plain
    assert spiced.auto is None


def test_saving_throws_are_not_touched_by_the_hook(make_character, bg3_hooks):
    hero = make_character(scores={DEX: 20}, save_proficiencies=[DEX])  # бонус спасброска +7

    def save(natural: int, dc: int, hooks=()):
        return saving_throw(
            hero,
            DEX,
            dc=dc,
            advantage=NONE,
            rng=ScriptedRng([natural]),
            reason="Ловушка",
            hooks=hooks,
        )

    twenty = save(20, 30, bg3_hooks)  # 27 против 30: двадцатка не спасает
    one = save(1, 5, bg3_hooks)  # 8 против 5: единица не губит

    assert (twenty.success, twenty.auto) == (False, None)
    assert (one.success, one.auto) == (True, None)
    assert twenty == save(20, 30)
    assert one == save(1, 5)


# =============================================================================================
# campfire: конец сцены привала
# =============================================================================================


def camp_state(*characters: Character, scene: SceneType = SceneType.CAMP) -> GameState:
    return GameState(
        game_id="game-1",
        adventure_id="demo-crypt",
        rules=RulesRef(modules=["campfire"]),
        party={c.id: c for c in characters},
        location_id="clearing",
        scene=Scene(type=scene),
        time=GameTime(day=2, part="night"),
        short_rests_since_long=2,
    )


@pytest.fixture
def camp_party(make_character):
    """Раненый воин, волшебник с пустыми ячейками и погибший: всем троим привал по-разному."""
    wounded = make_character(id="pc-1", name="Воин", hp=4, hit_dice=1)
    wizard = make_character(
        id="pc-2",
        name="Маг",
        class_id="wizard",
        spell_slots_max={1: 2},
        spell_slots={1: 0},
    )
    dead = make_character(id="pc-3", name="Павший", hp=0, death_saves=DeathSaves(failures=3))
    return wounded, wizard, dead


def end_of_scene(state: GameState, srd: SrdContent, *module_ids: str):
    (trigger,) = ruleset_of(*module_ids).triggers["on_scene_end"]
    return trigger(state, srd)


def test_the_end_of_a_camp_scene_gives_everyone_a_long_rest_and_the_next_morning(srd, camp_party):
    state = camp_state(*camp_party[:2])

    after, events = end_of_scene(state, srd, "campfire")

    assert after.party["pc-1"].hp == after.party["pc-1"].hp_max == 20
    assert after.party["pc-2"].spell_slots == {1: 2}
    assert after.time == GameTime(day=3, hour=8)  # утро следующего дня, 8:00
    assert after.time.part == "morning"
    assert after.short_rests_since_long == 0
    (rest,) = events
    assert isinstance(rest, RestEvent)
    assert rest.rest == "long"
    assert [(o.character_id, o.hp_gained) for o in rest.outcomes] == [("pc-1", 16), ("pc-2", 0)]
    assert rest.outcomes[1].restored == ["spell_slots"]


@pytest.mark.parametrize("hour", range(24))  # в том числе после полуночи и ровно в 8:00
def test_a_long_rest_wakes_the_party_at_eight_on_the_nearest_morning(srd, make_character, hour):
    # День меняется в полночь: привал с 0:00 до 5:59 идёт в ту же ночь, и утро — того же дня, как
    # у `advance_time`. С 6:00 ближайшее утро после отдыха — уже следующего дня.
    state = camp_state(make_character())
    state.time = GameTime(day=5, hour=hour)

    after, _ = end_of_scene(state, srd, "campfire")

    assert after.time == GameTime(day=5 if hour < 6 else 6, hour=8)


def test_a_short_rest_past_midnight_and_then_a_long_one_skip_no_day(srd, make_character):
    # 23:00 — короткий привал до полуночи, затем долгий: герои просыпаются тем же новым днём.
    state = camp_state(make_character())
    state.time = GameTime(day=5, hour=23)
    state.scene.rest = "short"

    after_short, _ = end_of_scene(state, srd, "campfire")
    after_short.scene.rest = "long"
    after_long, _ = end_of_scene(after_short, srd, "campfire")

    assert after_short.time == GameTime(day=6, hour=0)
    assert after_long.time == GameTime(day=6, hour=8)


def test_the_end_of_a_long_camp_returns_every_spent_hit_die_to_the_living(srd, make_character):
    # Долгий привал возвращает все потраченные кости хитов (SRD 5.2.1): из одной, из нуля и при
    # полном запасе. Павший отдыха не получает: его кости остаются потраченными.
    party = (
        make_character(id="pc-1", hp=4, hit_dice=1),
        make_character(id="pc-2", hit_dice_max=5, hit_dice=0),
        make_character(id="pc-3"),
        make_character(id="pc-4", hp=0, hit_dice=0, death_saves=DeathSaves(failures=3)),
    )

    after, _ = end_of_scene(camp_state(*party), srd, "campfire")

    hit_dice = {character_id: c.hit_dice for character_id, c in after.party.items()}
    assert hit_dice == {"pc-1": 3, "pc-2": 5, "pc-3": 3, "pc-4": 0}


def test_a_dead_character_gets_an_empty_outcome_and_stays_dead(srd, camp_party):
    state = camp_state(*camp_party)

    after, events = end_of_scene(state, srd, "campfire")

    dead = after.party["pc-3"]
    assert (dead.hp, dead.death_saves.failures) == (0, 3)
    assert after.party["pc-1"].hp == 20
    assert [o.character_id for o in events[0].outcomes] == ["pc-1", "pc-2", "pc-3"]
    empty = events[0].outcomes[2]
    assert (empty.hp_gained, empty.hit_dice_spent, empty.rolls, empty.restored) == (0, 0, [], [])


def test_the_trigger_does_not_change_the_original_state(srd, camp_party):
    state = camp_state(*camp_party)
    snapshot = state.model_copy(deep=True)

    after, events = end_of_scene(state, srd, "campfire")

    assert state == snapshot
    assert after is not state
    assert after.party["pc-1"] is not state.party["pc-1"]
    assert GameState.model_validate_json(after.model_dump_json()) == after
    assert all(type(e).model_validate_json(e.model_dump_json()) == e for e in events)


@pytest.mark.parametrize(
    "scene", [SceneType.EXPLORATION, SceneType.SOCIAL, SceneType.COMBAT], ids=lambda s: s.value
)
def test_the_end_of_any_other_scene_changes_nothing(srd, camp_party, scene):
    state = camp_state(*camp_party[:2], scene=scene)

    after, events = end_of_scene(state, srd, "campfire")

    assert after == state
    assert after is not state
    assert events == []


# =============================================================================================
# campfire: начало привала любого вида и конец короткого привала
# =============================================================================================


def begin(state: GameState, kind: str, srd: SrdContent, adventure, *module_ids: str):
    rules = ruleset_of(*(module_ids or ("campfire",)))
    return begin_rest(state, kind, rules, content=srd, adventure=adventure)


def battle() -> Combat:
    return Combat(encounter_id=None, zones=[Zone(id="here", name="здесь")], links=[], combatants={})


@pytest.mark.parametrize("kind", ["short", "long"])
def test_begin_rest_starts_a_camp_scene_of_the_chosen_kind(srd, demo_adventure, camp_party, kind):
    state = camp_state(*camp_party[:2], scene=SceneType.SOCIAL)
    state.turn = 7
    state.scene.inspiration_granted = ["pc-1"]
    before = state.model_copy(deep=True)

    camped, events = begin(state, kind, srd, demo_adventure)

    assert camped.scene == Scene(type=SceneType.CAMP, started_turn=7, rest=kind)
    assert events == [SceneEvent(scene=SceneType.CAMP)]
    # Отдых и время — в конце привала, а не в начале.
    assert (camped.party, camped.time, camped.short_rests_since_long) == (
        state.party,
        state.time,
        2,
    )
    assert state == before


@pytest.mark.parametrize("kind", ["short", "long"])
def test_a_rest_cannot_begin_in_battle(srd, demo_adventure, camp_party, kind):
    state = camp_state(*camp_party[:2], scene=SceneType.COMBAT)
    state.combat = battle()

    with pytest.raises(RestError, match="В бою привал невозможен"):
        begin(state, kind, srd, demo_adventure)


@pytest.mark.parametrize("current", [None, "short", "long"], ids=["no-kind", "short", "long"])
@pytest.mark.parametrize("kind", ["short", "long"])
def test_a_second_rest_cannot_begin_while_a_camp_goes_on(
    srd, demo_adventure, camp_party, kind, current
):
    state = camp_state(*camp_party[:2])
    state.scene.rest = current

    with pytest.raises(RestError, match="Привал уже идёт"):
        begin(state, kind, srd, demo_adventure)


def test_a_short_rest_does_not_begin_when_the_limit_of_short_rests_is_reached(
    srd, demo_adventure, camp_party
):
    state = camp_state(*camp_party[:2], scene=SceneType.EXPLORATION)  # уже два коротких отдыха

    with pytest.raises(RestError, match="не больше 2 коротких"):
        begin(state, "short", srd, demo_adventure, "bg3_flavor", "campfire")


def test_the_limit_of_short_rests_does_not_stop_a_long_rest(srd, demo_adventure, camp_party):
    state = camp_state(*camp_party[:2], scene=SceneType.EXPLORATION)

    camped, _ = begin(state, "long", srd, demo_adventure, "bg3_flavor", "campfire")

    assert camped.scene.rest == "long"  # долгий отдых и снимает предел


@pytest.mark.parametrize(
    ("module_ids", "done"),
    [(("bg3_flavor", "campfire"), 1), (("campfire",), 5)],
    ids=["below-the-limit", "no-limit"],
)
def test_a_short_rest_begins_below_the_limit_or_without_one(
    srd, demo_adventure, camp_party, module_ids, done
):
    state = camp_state(*camp_party[:2], scene=SceneType.EXPLORATION)
    state.short_rests_since_long = done

    camped, _ = begin(state, "short", srd, demo_adventure, *module_ids)

    assert camped.scene.rest == "short"


def test_the_end_of_a_short_rest_restores_short_rest_features_and_counts_the_rest(
    srd, make_character
):
    fighter = make_character(id="pc-1", hp=4, hit_dice=1, resources={"second_wind": 0})
    wizard = make_character(
        id="pc-2", class_id="wizard", spell_slots_max={1: 2}, spell_slots={1: 0}
    )
    state = camp_state(fighter, wizard)
    state.scene.rest = "short"

    after, events = end_of_scene(state, srd, "campfire")

    # Кости хитов тратят игроки во время привала: конец отдыха их не бросает.
    assert (after.party["pc-1"].hp, after.party["pc-1"].hit_dice) == (4, 1)
    assert after.party["pc-1"].resources == {"second_wind": 1}
    assert after.party["pc-2"].spell_slots == {1: 0}  # ячейки возвращает только долгий отдых
    assert after.time == GameTime(day=2, hour=23)  # час отдыха: с 22:00 до 23:00
    assert after.short_rests_since_long == 3
    nothing = {"hp_gained": 0, "hit_dice_spent": 0, "rolls": []}
    assert events == [
        RestEvent(
            rest="short",
            outcomes=[
                RestOutcome(character_id="pc-1", restored=["second_wind"], **nothing),
                RestOutcome(character_id="pc-2", restored=[], **nothing),
            ],
        )
    ]


def test_the_end_of_a_short_rest_gives_a_dead_character_nothing(srd, camp_party):
    state = camp_state(*camp_party)
    state.scene.rest = "short"
    snapshot = state.model_copy(deep=True)

    after, events = end_of_scene(state, srd, "campfire")

    assert after.party["pc-3"] == state.party["pc-3"]
    empty = events[0].outcomes[2]
    assert (empty.hp_gained, empty.hit_dice_spent, empty.rolls, empty.restored) == (0, 0, [], [])
    assert state == snapshot


@pytest.mark.parametrize(
    "saved_scene",
    [
        {"type": "camp", "started_turn": 3, "inspiration_granted": []},
        {"type": "camp", "started_turn": 3, "inspiration_granted": [], "rest": "long"},
    ],
    ids=["old-camp-without-a-kind", "long"],
)
def test_a_camp_without_a_kind_or_a_long_one_ends_with_a_long_rest(srd, camp_party, saved_scene):
    # Сцена `camp` из старого снимка поля `rest` не знает: такой привал был только долгим.
    state = camp_state(*camp_party[:2])
    state.scene = Scene.model_validate(saved_scene)

    after, events = end_of_scene(state, srd, "campfire")

    assert after.party["pc-1"].hp == 20
    assert after.time == GameTime(day=3, hour=8)
    assert after.short_rests_since_long == 0
    assert [event.rest for event in events] == ["long"]


def test_a_short_rest_from_its_beginning_to_its_end_through_end_scene(
    srd, demo_adventure, make_character
):
    hero = make_character(id="pc-1", hp=4, resources={"second_wind": 0})
    state = camp_state(hero, scene=SceneType.EXPLORATION)
    state.short_rests_since_long = 0
    rules = ruleset_of("bg3_flavor", "campfire")

    camped, _ = begin_rest(state, "short", rules, content=srd, adventure=demo_adventure)
    camped.party["pc-1"], _ = spend_hit_die(camped.party["pc-1"], srd, SeededRng(1))
    healed = camped.party["pc-1"].hp
    after, events = end_scene(
        camped, SceneType.EXPLORATION, rules, content=srd, adventure=demo_adventure
    )

    assert [type(event) for event in events] == [RestEvent, SceneEvent]
    assert events[0].rest == "short"
    assert (after.party["pc-1"].hp, after.party["pc-1"].hit_dice) == (healed, 2)
    assert after.party["pc-1"].resources == {"second_wind": 1}
    assert (after.time, after.short_rests_since_long) == (GameTime(day=2, hour=23), 1)
    assert after.scene == Scene(type=SceneType.EXPLORATION, started_turn=state.turn)


def test_two_short_rests_reach_the_limit_and_a_long_rest_lifts_it(
    srd, demo_adventure, make_character
):
    state = camp_state(make_character(), scene=SceneType.EXPLORATION)
    state.short_rests_since_long = 0
    rules = ruleset_of("bg3_flavor", "campfire")

    def rest_through(state: GameState, kind: str) -> GameState:
        camped, _ = begin_rest(state, kind, rules, content=srd, adventure=demo_adventure)
        after, _ = end_scene(
            camped, SceneType.EXPLORATION, rules, content=srd, adventure=demo_adventure
        )
        return after

    state = rest_through(rest_through(state, "short"), "short")
    assert state.short_rests_since_long == 2
    with pytest.raises(RestError, match="коротких"):
        begin_rest(state, "short", rules, content=srd, adventure=demo_adventure)

    state = rest_through(state, "long")

    assert state.short_rests_since_long == 0
    assert rest_through(state, "short").short_rests_since_long == 1


def test_the_camp_prompt_describes_both_the_short_and_the_long_rest():
    assert "Долгий привал" in CAMP_FRAGMENT and "Короткий привал" in CAMP_FRAGMENT
    assert "кости хитов" in CAMP_FRAGMENT  # на коротком их тратят игроки
    assert "start_camp" in CAMP_FRAGMENT


def test_the_camp_prompt_names_the_hours_the_engine_sets_after_a_rest():
    # Мастер рассказывает о конце привала по подсказке: часы в ней те же, что ставит движок.
    assert f"на {LONG_REST_WAKE_HOUR} утра" in CAMP_FRAGMENT
    assert "после полуночи — того же дня" in CAMP_FRAGMENT  # привал в ту же ночь
    assert SHORT_REST_HOURS == 1
    assert "сдвинет время на час" in CAMP_FRAGMENT
    assert "время не изменит" not in CAMP_FRAGMENT  # так говорилось, пока часов не было


# --- предел из правил, порядок отказов, конец привала любого вида ---


def with_short_rest_limit(limit: int) -> RuleSet:
    """«Привал у костра» с пределом коротких отдыхов `limit`, какого нет ни у одного модуля."""
    return ruleset_of("campfire").model_copy(update={"flags": {MAX_SHORT_RESTS_PER_LONG: limit}})


@pytest.mark.parametrize(("limit", "done"), [(1, 1), (0, 0), (3, 4)])
def test_begin_rest_refuses_a_short_rest_by_the_limit_of_the_rules_it_is_given(
    srd, demo_adventure, camp_party, limit, done
):
    # Модули в состоянии — только «Привал у костра» без предела: предел берётся из `rules`.
    state = camp_state(*camp_party[:2], scene=SceneType.EXPLORATION)
    state.short_rests_since_long = done

    with pytest.raises(RestError) as refusal:
        begin_rest(
            state, "short", with_short_rest_limit(limit), content=srd, adventure=demo_adventure
        )

    assert str(refusal.value) == (
        f"Между долгими привалами допустимо не больше {limit} коротких, а прошло уже {done}: "
        "нужен долгий привал."
    )


@pytest.mark.parametrize(("limit", "done"), [(1, 0), (3, 2)])
def test_begin_rest_allows_a_short_rest_below_the_limit_of_the_rules_it_is_given(
    srd, demo_adventure, camp_party, limit, done
):
    state = camp_state(*camp_party[:2], scene=SceneType.EXPLORATION)
    state.short_rests_since_long = done

    camped, _ = begin_rest(
        state, "short", with_short_rest_limit(limit), content=srd, adventure=demo_adventure
    )

    assert camped.scene.rest == "short"
    assert camped.short_rests_since_long == done  # счётчик растёт в конце привала, не в начале


@pytest.mark.parametrize(
    ("scene", "in_battle", "refusal"),
    [
        (SceneType.COMBAT, True, "В бою привал невозможен"),
        (SceneType.CAMP, False, "Привал уже идёт"),
        (SceneType.EXPLORATION, False, "не больше 2 коротких"),
    ],
    ids=["battle", "camp", "limit"],
)
def test_the_first_broken_rule_names_the_refusal_and_the_state_stays_as_it_was(
    srd, demo_adventure, camp_party, scene, in_battle, refusal
):
    # Предел коротких «Специй BG3» исчерпан во всех трёх случаях (уже два коротких отдыха), но
    # объяснение — по первому нарушенному правилу: бой, затем идущий привал, затем предел.
    state = camp_state(*camp_party[:2], scene=scene)
    if in_battle:
        state.combat = battle()
    before = state.model_copy(deep=True)

    with pytest.raises(RestError, match=refusal):
        begin(state, "short", srd, demo_adventure, "bg3_flavor", "campfire")

    assert state == before


@pytest.mark.parametrize(
    ("hour", "done", "expected"),
    [
        (6, 0, (4, 7, "morning")),
        (11, 0, (4, 12, "day")),  # утро сменилось днём
        (17, 1, (4, 18, "evening")),  # час отдыха сменил часть суток
        (21, 4, (4, 22, "night")),
        (23, 2, (5, 0, "night")),  # полночь: наступил следующий день
        (5, 0, (4, 6, "morning")),
    ],
)
def test_the_end_of_a_short_rest_moves_the_clock_one_hour_and_adds_one_short_rest(
    srd, make_character, hour, done, expected
):
    # Предел коротких проверяет только начало привала: конец считает и сверх него (4 -> 5).
    state = camp_state(make_character())
    state.scene.rest = "short"
    state.time = GameTime(day=4, hour=hour)
    state.short_rests_since_long = done

    after, _ = end_of_scene(state, srd, "campfire")

    assert (after.time.day, after.time.hour, after.time.part) == expected
    assert after.short_rests_since_long == done + 1
    assert state.time == GameTime(day=4, hour=hour)  # исходное состояние не меняется


def test_the_end_of_a_short_rest_neither_heals_nor_wakes_nor_ends_effects(srd, make_character):
    # Длинный отдых поднял бы лежащего, вернул бы кости хитов и снял бы «Подмогу»; короткий — нет.
    fallen = make_character(
        id="pc-1",
        hp=0,
        hit_dice=0,
        conditions=[ActiveCondition(id="unconscious", source=ZERO_HP_SOURCE)],
        death_saves=DeathSaves(successes=1, failures=2),
    )
    aided = make_character(
        id="pc-2",
        hp_max=25,
        hp=12,
        temp_hp=3,
        buffs=[ActiveBuff(id=AID_BUFF, rounds_left=4800, source_id="pc-1", data={"hp": 5})],
    )
    state = camp_state(fallen, aided)
    state.scene.rest = "short"

    after, events = end_of_scene(state, srd, "campfire")

    assert after.party == state.party
    assert [(o.hp_gained, o.restored) for o in events[0].outcomes] == [(0, []), (0, [])]


@pytest.mark.parametrize(
    ("kind", "rest"),
    [(None, "long"), ("long", "long"), ("short", "short")],
    ids=["camp-without-a-kind", "long", "short"],
)
@pytest.mark.parametrize(
    "next_type", [SceneType.EXPLORATION, SceneType.SOCIAL], ids=lambda s: s.value
)
def test_end_scene_after_a_camp_rests_by_its_kind_and_the_next_scene_has_no_kind(
    srd, demo_adventure, camp_party, kind, rest, next_type
):
    state = camp_state(*camp_party[:2])
    state.scene.rest = kind
    state.turn = 9

    after, events = end_scene(
        state,
        next_type,
        ruleset_of("bg3_flavor", "campfire"),
        content=srd,
        adventure=demo_adventure,
    )

    assert [type(event) for event in events] == [RestEvent, SceneEvent]
    assert (events[0].rest, events[1].scene) == (rest, next_type)
    assert after.scene == Scene(type=next_type, started_turn=9)
    assert after.scene.rest is None
    assert all(type(e).model_validate_json(e.model_dump_json()) == e for e in events)
