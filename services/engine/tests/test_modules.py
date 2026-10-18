"""Модули правил: реестр, сборка `RuleSet`, «Специи BG3» и «Привал у костра».

Хук `bg3_flavor` проверяется на настоящих проверках и спасбросках с `ScriptedRng`, триггер
`campfire` — на настоящем `GameState` с отдыхом персонажей. Флаги, которые читает бой, проверяет
`test_combat_actions.py`.
"""

import pytest
from pydantic import ValidationError

from dnd_engine.abilities import Ability, Skill
from dnd_engine.character import Character, DeathSaves
from dnd_engine.checks import CheckKind, CheckResult, ability_check, saving_throw
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage
from dnd_engine.events import RestEvent
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
from dnd_engine.rng import ScriptedRng
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
    spec = object()  # спецификации инструментов (B4.5) реестр только хранит
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
    assert after.time == GameTime(day=3, part="morning")
    assert after.short_rests_since_long == 0
    (rest,) = events
    assert isinstance(rest, RestEvent)
    assert rest.rest == "long"
    assert [(o.character_id, o.hp_gained) for o in rest.outcomes] == [("pc-1", 16), ("pc-2", 0)]
    assert rest.outcomes[1].restored == ["spell_slots"]


def test_the_next_morning_is_the_day_after_even_when_the_camp_began_in_the_morning(
    srd, make_character
):
    state = camp_state(make_character())
    state.time = GameTime(day=5, part="morning")

    after, _ = end_of_scene(state, srd, "campfire")

    assert after.time == GameTime(day=6, part="morning")


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
