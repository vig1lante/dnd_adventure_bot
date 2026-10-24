"""Реестр инструментов мастера и чтение лора (план B4.5, спека §5.7, §8.2–§8.3).

Реестр принимает вызов модели: проверяет имя и аргументы, зовёт обработчик и превращает ошибки
модели в `ToolOutcome(error=...)`. Здесь же проверяется приватность бросков тайного хода и
одноразовое вдохновение. Инструменты лора читают приключение `demo-crypt` и состояние игры,
ничего не меняя.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from adventure_factory import make_adventure
from pydantic import BaseModel, Field

from dnd_engine.adventure.schema import Adventure, Exit, Relation, WorldInfo
from dnd_engine.character import Character
from dnd_engine.content import SrdContent, load_srd
from dnd_engine.dice import Advantage, roll
from dnd_engine.events import FlagEvent, RollEvent
from dnd_engine.modules import build_ruleset
from dnd_engine.rng import ScriptedRng
from dnd_engine.state import GameState, RulesRef, WorldState
from dnd_engine.tools.lore import LORE_TOOLS, exit_status, is_locked, relations_of
from dnd_engine.tools.registry import (
    MAX_ERROR_CHARS,
    MAX_NAME_CHARS,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)

# --- помощники ---


@pytest.fixture(scope="module")
def srd() -> SrdContent:
    return load_srd()


@pytest.fixture
def make_state(make_character: Callable[..., Character]) -> Callable[..., GameState]:
    """Состояние `demo-crypt` с двумя героями (`pc-1`, `pc-2`) в деревне; поля переопределяются."""

    def factory(**overrides: Any) -> GameState:
        party = [make_character(id="pc-1"), make_character(id="pc-2", name="Второй герой")]
        fields = {
            "game_id": "game-1",
            "adventure_id": "demo-crypt",
            "rules": RulesRef(modules=["bg3_flavor", "campfire"]),
            "party": {character.id: character for character in party},
            "location_id": "village",
        }
        return GameState(**(fields | overrides))

    return factory


@pytest.fixture
def make_ctx(
    demo_adventure: Adventure, srd: SrdContent, make_state: Callable[..., GameState]
) -> Callable[..., ToolContext]:
    """Контекст вызова: `make_ctx(state=..., secret_actors=..., inspired=...)`."""

    def factory(state: GameState | None = None, **overrides: Any) -> ToolContext:
        state = state or make_state()
        fields = {
            "state": state,
            "adventure": demo_adventure,
            "content": srd,
            "rules": build_ruleset(state.rules),
            "rng": ScriptedRng([10] * 20),
        }
        return ToolContext(**(fields | overrides))

    return factory


@pytest.fixture
def lore() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in LORE_TOOLS:
        registry.register(spec)
    return registry


class RollersArgs(ToolArgs):
    actors: list[str] = Field(max_length=4)
    # Авторы вызова: чьё это действие. Без них у вызова автора нет.
    author: str | None = Field(default=None, max_length=20)
    partner: str | None = Field(default=None, max_length=20)


def roll_for_everyone(ctx: ToolContext, args: RollersArgs) -> dict[str, Any]:
    """Тестовый инструмент: бросок к20 за каждого актёра и одно небросковое событие."""
    for actor in args.actors:
        result = roll("1d20", rng=ctx.rng, reason="тест")
        ctx.events.append(RollEvent(actor_id=actor, label="проверка", roll=result))
    ctx.events.append(FlagEvent(name="seen", value=True))
    return {"rolled": len(args.actors)}


ROLLERS = ToolSpec(
    name="roll_for_everyone",
    description="Тестовый инструмент: бросает к20 за каждого перечисленного актёра.",
    args_model=RollersArgs,
    handler=roll_for_everyone,
    author_fields=("author", "partner"),
)


class PassersArgs(ToolArgs):
    passed: str = Field(max_length=20)
    author: str = Field(max_length=20)


def pass_turns_then_roll(ctx: ToolContext, args: PassersArgs) -> dict[str, Any]:
    """Тестовый инструмент: бросок при передаче хода, затем бросок чужого актёра в действии."""
    first = len(ctx.events)
    passed = roll("1d20", rng=ctx.rng, reason="конец хода")
    ctx.events.append(RollEvent(actor_id=args.passed, label="конец хода", roll=passed))
    ctx.begin_action(first + 1)
    target = roll("1d20", rng=ctx.rng, reason="цель")
    ctx.events.append(RollEvent(actor_id="pc-2", label="цель", roll=target))
    return {}


PASSERS = ToolSpec(
    name="pass_turns_then_roll",
    description="Тестовый инструмент: передача хода и действие автора.",
    args_model=PassersArgs,
    handler=pass_turns_then_roll,
    author_fields=("author",),
)


def roll_audiences(ctx: ToolContext) -> list[tuple[str, str]]:
    """(актёр, аудитория) всех бросков журнала событий в порядке появления."""
    return [(e.actor_id, e.audience) for e in ctx.events if isinstance(e, RollEvent)]


def call_rollers(ctx: ToolContext, *actors: str, **authors: str) -> ToolOutcome:
    """Вызов тестового инструмента: `authors` — поля `author` и `partner` (авторы вызова)."""
    registry = ToolRegistry()
    registry.register(ROLLERS)
    return registry.call(ctx, "roll_for_everyone", {"actors": list(actors)} | authors)


# --- ToolRegistry: имя, аргументы, ошибки ---


class EchoArgs(ToolArgs):
    text: str = Field(max_length=20)


def echo(ctx: ToolContext, args: EchoArgs) -> dict[str, Any]:
    return {"text": args.text}


def fail_with_tool_error(ctx: ToolContext, args: EchoArgs) -> dict[str, Any]:
    raise ToolError("Нельзя так делать: выберите другой предмет.")


def fail_with_a_huge_tool_error(ctx: ToolContext, args: EchoArgs) -> dict[str, Any]:
    # Текст ошибки собирают из id, которые прислала модель: он мог оказаться любой длины.
    raise ToolError(f"Нет предмета {'я' * 5000!r}.")


def fail_with_engine_bug(ctx: ToolContext, args: EchoArgs) -> dict[str, Any]:
    raise RuntimeError("баг движка")


def fail_with_plain_value_error(ctx: ToolContext, args: EchoArgs) -> dict[str, Any]:
    raise ValueError("общий ValueError — не ошибка модели")


def make_spec(name: str, handler: Callable[..., dict[str, Any]]) -> ToolSpec:
    return ToolSpec(name, f"Тестовый инструмент {name}.", EchoArgs, handler)


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name, handler in [
        ("echo", echo),
        ("tool_error", fail_with_tool_error),
        ("huge_tool_error", fail_with_a_huge_tool_error),
        ("engine_bug", fail_with_engine_bug),
        ("value_error", fail_with_plain_value_error),
    ]:
        registry.register(make_spec(name, handler))
    return registry


def test_call_returns_the_handler_result_and_keeps_name_and_args(registry, make_ctx):
    outcome = registry.call(make_ctx(), "echo", {"text": "привет"})

    assert outcome == ToolOutcome(
        name="echo", args={"text": "привет"}, result={"text": "привет"}, error=None
    )


def test_unknown_tool_gives_an_error_for_the_model(registry, make_ctx):
    outcome = registry.call(make_ctx(), "teleport", {})

    assert outcome.result is None
    assert "teleport" in outcome.error
    assert "echo" in outcome.error  # модель видит, какие инструменты есть


def test_unknown_tool_name_is_not_echoed_in_full(registry, make_ctx):
    outcome = registry.call(make_ctx(), "x" * 5000, {})

    assert outcome.error is not None
    assert len(outcome.error) < 600


def test_invalid_arguments_give_an_error_with_validation_text(registry, make_ctx):
    outcome = registry.call(make_ctx(), "echo", {"text": 5})

    assert outcome.result is None
    assert "echo" in outcome.error
    assert "text" in outcome.error  # название поля, чтобы модель знала, что исправить


def test_missing_and_extra_arguments_are_errors_not_exceptions(registry, make_ctx):
    missing = registry.call(make_ctx(), "echo", {})
    extra = registry.call(make_ctx(), "echo", {"text": "a", "crit": True})

    assert "text" in missing.error
    assert "crit" in extra.error  # лишнее поле не отбрасывается молча


def test_overlong_text_argument_is_an_error_and_is_not_echoed(registry, make_ctx):
    outcome = registry.call(make_ctx(), "echo", {"text": "я" * 10_000})

    assert outcome.result is None
    assert "я" * 50 not in outcome.error  # значение не попадает в ответ модели и журнал
    assert len(outcome.error) < 600


def test_a_huge_unknown_argument_name_does_not_blow_up_the_error(registry, make_ctx):
    outcome = registry.call(make_ctx(), "echo", {"text": "a", "ы" * 10_000: 1})

    assert outcome.result is None
    assert len(outcome.error) < 600


def test_a_long_unknown_argument_name_is_clipped_in_the_error_but_still_named(registry, make_ctx):
    outcome = registry.call(make_ctx(), "echo", {"text": "a", "ы" * 5000: 1})

    assert outcome.result is None
    assert "ы" * MAX_NAME_CHARS in outcome.error  # поле названо
    assert "ы" * (MAX_NAME_CHARS + 1) not in outcome.error  # но имя, выбранное моделью, обрезано


@pytest.mark.parametrize("raw_args", [[], [1, 2], None, "{}", "текст", 5, 2.5, True])
@pytest.mark.parametrize("name", ["echo", "teleport"], ids=["known-tool", "unknown-tool"])
def test_arguments_that_are_not_an_object_are_an_error_for_the_model_not_an_exception(
    registry, make_ctx, name, raw_args
):
    # Аргументы — ввод модели: `json.loads` пропускает любой валидный JSON, не только объект.
    ctx = make_ctx()
    before = ctx.state.model_copy(deep=True)

    outcome = registry.call(ctx, name, raw_args)

    assert outcome.result is None and outcome.error
    assert outcome.args == {}
    assert len(outcome.error) <= MAX_ERROR_CHARS
    assert ctx.state == before and ctx.events == []


def test_the_error_of_non_object_arguments_names_the_tool_and_the_expected_shape(
    registry, make_ctx
):
    outcome = registry.call(make_ctx(), "echo", ["привет"])

    assert "echo" in outcome.error and "объект" in outcome.error


def test_tool_error_becomes_an_outcome_error(registry, make_ctx):
    outcome = registry.call(make_ctx(), "tool_error", {"text": "a"})

    assert outcome.result is None
    assert outcome.error == "Нельзя так делать: выберите другой предмет."


def test_a_huge_tool_error_is_shortened_like_a_validation_error(registry, make_ctx):
    outcome = registry.call(make_ctx(), "huge_tool_error", {"text": "a"})

    assert outcome.result is None and outcome.error.startswith("Нет предмета")
    assert len(outcome.error) <= MAX_ERROR_CHARS


@pytest.mark.parametrize("name", ["engine_bug", "value_error"])
def test_other_exceptions_propagate_as_engine_errors(registry, make_ctx, name):
    # Ошибка движка — не ошибка модели: общий ValueError тоже не ловится, иначе баг в нашем коде
    # выглядел бы для модели как её неверный аргумент.
    with pytest.raises((RuntimeError, ValueError)):
        registry.call(make_ctx(), name, {"text": "a"})


def test_duplicate_tool_name_is_rejected():
    registry = ToolRegistry()
    registry.register(make_spec("echo", echo))

    with pytest.raises(ValueError, match="echo"):
        registry.register(make_spec("echo", echo))


def test_specs_keep_registration_order(registry):
    assert [spec.name for spec in registry.specs()] == [
        "echo",
        "tool_error",
        "huge_tool_error",
        "engine_bug",
        "value_error",
    ]


def test_json_schema_is_serializable_and_forbids_extra_fields():
    schema = make_spec("echo", echo).json_schema()

    assert json.loads(json.dumps(schema)) == schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["text"]["maxLength"] == 20


def test_args_model_that_allows_extra_fields_is_rejected_at_registration():
    class Loose(BaseModel):
        text: str

    with pytest.raises(ValueError, match="extra"):
        ToolRegistry().register(ToolSpec("loose", "Тестовый инструмент.", Loose, echo))


def test_author_field_that_the_arguments_do_not_have_is_rejected_at_registration():
    # Опечатка в имени поля молча выключила бы тайность бросков инструмента.
    spec = ToolSpec("echo", "Тестовый инструмент.", EchoArgs, echo, author_fields=("actor_id",))

    with pytest.raises(ValueError, match="actor_id"):
        ToolRegistry().register(spec)


# --- аудитория бросков: тайный ход виден только автору ---


def test_audience_for_is_all_for_ordinary_actors_and_own_id_for_secret_ones(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    assert ctx.audience_for("pc-1") == "pc-1"
    assert ctx.audience_for("pc-2") == "all"


def test_secret_actors_cannot_contain_the_reserved_audience_all(make_ctx):
    # «all» в роли актёра сделал бы тайный бросок общим.
    with pytest.raises(ValueError, match="all"):
        make_ctx(secret_actors={"all"})


# --- очередь бойцов вызова: ctx.acting ---


def test_acting_is_empty_by_default(make_ctx):
    assert make_ctx().acting == ()


def test_acting_is_passed_to_the_constructor_and_kept_as_a_tuple(make_ctx):
    # Сервер может передать список (план B7.5: acting — list[str]); контекст хранит кортеж.
    ctx = make_ctx(acting=["pc-1", "skeleton-1"])

    assert ctx.acting == ("pc-1", "skeleton-1")
    assert make_ctx(acting=("pc-2",)).acting == ("pc-2",)


def test_rolls_without_secret_actors_stay_public(make_ctx):
    ctx = make_ctx()

    call_rollers(ctx, "pc-1", "pc-2")

    assert roll_audiences(ctx) == [("pc-1", "all"), ("pc-2", "all")]


def test_secret_actor_roll_is_visible_only_to_the_author(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-1")

    assert roll_audiences(ctx) == [("pc-1", "pc-1")]


def test_ordinary_participant_roll_in_a_secret_call_goes_to_the_secret_author(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-2", "pc-1", author="pc-1")

    # Бросок pc-2 — часть тайного действия pc-1: его не видит никто, кроме автора.
    assert roll_audiences(ctx) == [("pc-2", "pc-1"), ("pc-1", "pc-1")]


def test_a_secret_author_who_does_not_roll_still_owns_the_rolls_of_the_call(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-2", author="pc-1")  # толчок: бросает цель, а не автор

    assert roll_audiences(ctx) == [("pc-2", "pc-1")]


def test_without_an_author_a_secret_actor_in_the_call_does_not_hide_the_others(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-2", "pc-1")  # у вызова автора нет (как у начала боя)

    # Свой бросок тайного актёра закрыт, чужой остаётся общим.
    assert roll_audiences(ctx) == [("pc-2", "all"), ("pc-1", "pc-1")]


def test_an_ordinary_author_does_not_hide_anything_but_the_rolls_of_secret_actors(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-1", "pc-2", author="pc-2")

    assert roll_audiences(ctx) == [("pc-1", "pc-1"), ("pc-2", "all")]


def test_the_secret_roll_of_another_actor_stays_with_that_actor_in_a_secret_call(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1", "pc-2"})

    call_rollers(ctx, "pc-3", "pc-2", "pc-1", author="pc-1")

    # Бросок обычного участника — автору, собственный бросок другого тайного актёра — ему самому.
    assert roll_audiences(ctx) == [("pc-3", "pc-1"), ("pc-2", "pc-2"), ("pc-1", "pc-1")]


def test_two_secret_actors_each_see_only_their_own_roll(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1", "pc-2"})

    call_rollers(ctx, "pc-1", "pc-2")

    assert roll_audiences(ctx) == [("pc-1", "pc-1"), ("pc-2", "pc-2")]


def test_ordinary_roll_next_to_two_secret_authors_is_never_public(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1", "pc-2"})

    call_rollers(ctx, "pc-3", "pc-2", "pc-1", author="pc-2", partner="pc-1")

    # Аудитория одна, поэтому выбран первый по id тайный автор: закрыто, а не «всем».
    assert roll_audiences(ctx) == [("pc-3", "pc-1"), ("pc-2", "pc-2"), ("pc-1", "pc-1")]


def test_secret_actor_who_is_not_in_the_call_does_not_hide_other_calls(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-2", author="pc-2")

    # В этом вызове pc-1 не участвует: бросок pc-2 остаётся общим.
    assert roll_audiences(ctx) == [("pc-2", "all")]


def test_only_the_rolls_of_the_current_call_are_remarked(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})
    call_rollers(ctx, "pc-2", author="pc-2")  # прошлый вызов без тайного автора: бросок общий

    call_rollers(ctx, "pc-1", author="pc-1")

    assert roll_audiences(ctx) == [("pc-2", "all"), ("pc-1", "pc-1")]


def test_rolls_before_the_action_of_the_call_belong_to_their_own_actors(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})
    registry = ToolRegistry()
    registry.register(PASSERS)

    registry.call(ctx, "pass_turns_then_roll", {"passed": "pc-3", "author": "pc-1"})

    # Передача хода (бросок pc-3) размечена по актёру: общий. Действие автора pc-1 — тайное.
    assert roll_audiences(ctx) == [("pc-3", "all"), ("pc-2", "pc-1")]


def test_non_roll_events_are_left_untouched(make_ctx):
    ctx = make_ctx(secret_actors={"pc-1"})

    call_rollers(ctx, "pc-1")

    assert ctx.events[-1] == FlagEvent(name="seen", value=True)


# --- вдохновение: одноразово, источник правды — Character.inspiration ---


def test_take_inspiration_gives_advantage_once_and_clears_both_marks(make_ctx, make_state):
    state = make_state()
    state.party["pc-1"].inspiration = True
    ctx = make_ctx(state, inspired={"pc-1"})

    first = ctx.take_inspiration("pc-1")
    second = ctx.take_inspiration("pc-1")

    assert (first, second) == (Advantage.ADVANTAGE, Advantage.NONE)
    assert state.party["pc-1"].inspiration is False
    assert ctx.inspired == set()


def test_take_inspiration_needs_the_players_mark_for_this_turn(make_ctx, make_state):
    state = make_state()
    state.party["pc-1"].inspiration = True
    ctx = make_ctx(state)  # игрок не отметил «Вдохновение» в этом ходе

    assert ctx.take_inspiration("pc-1") == Advantage.NONE
    assert state.party["pc-1"].inspiration is True  # само вдохновение не потрачено


def test_take_inspiration_needs_the_character_to_really_have_it(make_ctx, make_state):
    state = make_state()  # у pc-1 вдохновения нет, а игрок его отметил
    ctx = make_ctx(state, inspired={"pc-1"})

    assert ctx.take_inspiration("pc-1") == Advantage.NONE
    assert state.party["pc-1"].inspiration is False


def test_take_inspiration_for_an_unknown_character_is_no_advantage(make_ctx):
    ctx = make_ctx(inspired={"ghost"})

    assert ctx.take_inspiration("ghost") == Advantage.NONE


def test_take_inspiration_spends_only_the_named_character(make_ctx, make_state):
    state = make_state()
    state.party["pc-1"].inspiration = state.party["pc-2"].inspiration = True
    ctx = make_ctx(state, inspired={"pc-1", "pc-2"})

    ctx.take_inspiration("pc-1")

    assert state.party["pc-2"].inspiration is True
    assert ctx.inspired == {"pc-2"}


# --- замки и скрытые выходы: общий расчёт для get_location, move_party и reveal ---


def cemetery_exit(adventure: Adventure, exit_id: str) -> Exit:
    return next(e for e in adventure.locations["cemetery"].exits if e.id == exit_id)


def locked_exit(locked_by: str) -> Exit:
    return Exit(id="gate", to="crypt", direction="вниз", description="Дверь.", locked_by=locked_by)


def test_hidden_exit_is_hidden_until_it_is_revealed(demo_adventure, make_state):
    state = make_state(location_id="cemetery")
    to_crypt = cemetery_exit(demo_adventure, "to-crypt")

    before = exit_status(state, demo_adventure, to_crypt)
    state.world.revealed.append("exit:to-crypt")
    after = exit_status(state, demo_adventure, to_crypt)

    assert (before, after) == ("hidden", "open")


def test_exit_without_hidden_dc_or_lock_is_open(demo_adventure, make_state):
    to_village = cemetery_exit(demo_adventure, "to-village")

    assert exit_status(make_state(), demo_adventure, to_village) == "open"


def test_item_lock_opens_only_when_a_party_member_holds_the_item(demo_adventure, make_state):
    state = make_state()
    gate = locked_exit("item:chest-key")

    # По приключению ключ у Марты.
    assert exit_status(state, demo_adventure, gate) == "locked"
    for holder in ["character:ghost", "location:cemetery", "npc:marta", "gone"]:
        state.world.item_holders["chest-key"] = holder
        assert exit_status(state, demo_adventure, gate) == "locked", holder

    state.world.item_holders["chest-key"] = "character:pc-2"
    assert exit_status(state, demo_adventure, gate) == "open"


@pytest.mark.parametrize(
    ("flag_value", "locked"),
    [(None, True), (False, True), (0, True), ("", True), (True, False), (1, False), ("yes", False)],
)
def test_flag_lock_opens_when_the_flag_is_set(demo_adventure, make_state, flag_value, locked):
    state = make_state()
    if flag_value is not None:
        state.world.flags["gate-open"] = flag_value

    assert is_locked(state, demo_adventure, "flag:gate-open") is locked


def test_no_lock_is_never_locked_and_an_unknown_lock_kind_is_closed(demo_adventure, make_state):
    state = make_state()

    assert is_locked(state, demo_adventure, None) is False
    assert is_locked(state, demo_adventure, "spell:knock") is True  # неизвестный замок не открыт


def test_hidden_wins_over_locked(demo_adventure, make_state):
    gate = Exit(
        id="secret",
        to="crypt",
        direction="вниз",
        description="Люк.",
        locked_by="flag:never",
        hidden_dc=15,
    )

    assert exit_status(make_state(), demo_adventure, gate) == "hidden"


# --- get_location ---


def by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def test_get_location_defaults_to_the_current_location(lore, make_ctx):
    outcome = lore.call(make_ctx(), "get_location", {})

    assert outcome.error is None
    result = outcome.result
    assert result["id"] == "village"
    assert result["name"] == "Мшистый Брод"
    assert "Вечерний Мшистый Брод" in result["read_aloud"]
    assert "Марта" in result["dm_notes"]
    assert result["encounter"] is None


def test_get_location_by_id_overrides_the_current_one(lore, make_ctx):
    outcome = lore.call(make_ctx(), "get_location", {"location_id": "crypt"})

    assert outcome.result["id"] == "crypt"
    assert outcome.result["dm_notes"]


def test_get_location_shows_a_hidden_exit_as_hidden_until_it_is_revealed(
    lore, make_ctx, make_state
):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    before = by_id(lore.call(ctx, "get_location", {}).result["exits"])
    state.world.revealed.append("exit:to-crypt")
    after = by_id(lore.call(ctx, "get_location", {}).result["exits"])

    assert before["to-crypt"]["status"] == "hidden"
    assert before["to-crypt"]["hidden_dc"] == 15  # мастеру нужна Сл поиска
    assert before["to-village"]["status"] == "open"
    assert after["to-crypt"]["status"] == "open"
    assert "hidden_dc" not in after["to-crypt"]


def test_get_location_describes_an_open_exit_without_lock_details(lore, make_ctx):
    # В demo-crypt запертых выходов нет: замок выхода проверяют тесты `exit_status` выше.
    exits = lore.call(make_ctx(), "get_location", {}).result["exits"]

    assert exits == [
        {
            "id": "to-cemetery",
            "to": "cemetery",
            "direction": "север",
            "description": "Тропа через мокрый холм к кладбищу.",
            "status": "open",
        }
    ]


def test_get_location_shows_a_locked_exit_before_and_after_the_key_is_handed_over(
    lore, make_ctx, make_state, demo_adventure
):
    # В demo-crypt запертых выходов нет: запираем выход из деревни ключом от сундука.
    data = demo_adventure.model_dump(mode="json")
    data["locations"]["village"]["exits"][0]["locked_by"] = "item:chest-key"
    state = make_state()
    ctx = make_ctx(state, adventure=Adventure.model_validate(data))

    before = lore.call(ctx, "get_location", {}).result["exits"][0]
    state.world.item_holders["chest-key"] = "character:pc-2"
    after = lore.call(ctx, "get_location", {}).result["exits"][0]

    assert (before["status"], before["locked_by"]) == ("locked", "item:chest-key")
    assert after["status"] == "open"
    assert "locked_by" not in after


def test_get_location_shows_a_locked_feature_before_and_after_the_key_is_handed_over(
    lore, make_ctx, make_state
):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    before = by_id(lore.call(ctx, "get_location", {}).result["features"])
    state.world.item_holders["chest-key"] = "character:pc-1"
    after = by_id(lore.call(ctx, "get_location", {}).result["features"])

    assert before["chest"]["status"] == "locked"
    assert before["chest"]["locked_by"] == "item:chest-key"
    assert before["diary"]["status"] == "open"
    assert "locked_by" not in before["diary"]
    assert after["chest"]["status"] == "open"
    assert "locked_by" not in after["chest"]


def test_get_location_marks_revealed_features(lore, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    state.world.revealed.append("feature:diary")

    features = by_id(lore.call(make_ctx(state), "get_location", {}).result["features"])

    assert features["diary"]["revealed"] is True
    assert features["tracks"]["revealed"] is False
    assert features["diary"]["check"] == {"ability": "int", "skill": "investigation", "dc": 10}
    assert features["chest"]["check"] is None


def test_get_location_lists_npcs_present_with_their_current_state(lore, make_ctx, make_state):
    state = make_state(world=WorldState(npc_attitude={"marta": 2}))

    npcs = lore.call(make_ctx(state), "get_location", {}).result["npcs"]

    assert npcs == [
        {
            "id": "marta",
            "name": "Марта",
            "role": "трактирщица «Сонного покойника», сестра пропавшего смотрителя",
            "alive": True,
            "attitude": 2,
        }
    ]


def test_get_location_follows_npc_moves_and_deaths(lore, make_ctx, make_state):
    state = make_state(
        world=WorldState(npc_location={"fabian": "village"}, npc_alive={"fabian": False})
    )
    ctx = make_ctx(state)

    village = by_id(lore.call(ctx, "get_location", {}).result["npcs"])
    crypt = lore.call(ctx, "get_location", {"location_id": "crypt"}).result["npcs"]

    assert set(village) == {"marta", "fabian"}
    assert village["fabian"]["alive"] is False
    assert village["fabian"]["attitude"] == -1  # отношение из приключения, пока мир его не менял
    assert crypt == []


# Предметы «здесь»: лежащие в локации открыто и находящиеся у NPC, присутствующих в ней. Предмет
# внутри ещё не раскрытого места (сундук в сторожке) в список не попадает: он по-прежнему назван
# только в `reveals` места, а выдать его до раскрытия мастеру не даёт `give_item`.

CHEST_KEY = {"id": "chest-key", "name": "Запасной ключ Ульриха"}
POTION = {"id": "healing-potion", "name": "Зелье лечения"}


def items_of(lore, ctx, **args) -> list[dict[str, Any]]:
    return lore.call(ctx, "get_location", args).result["items"]


def test_get_location_shows_an_item_held_by_an_npc_who_is_here(lore, make_ctx):
    assert items_of(lore, make_ctx()) == [{**CHEST_KEY, "holder": "npc:marta"}]


def test_get_location_does_not_show_an_item_inside_a_closed_place(lore, make_ctx, make_state):
    ctx = make_ctx(make_state(location_id="cemetery"))

    result = lore.call(ctx, "get_location", {}).result

    assert result["items"] == []
    # Предмет назван только как следствие находки места, по-прежнему для мастера.
    assert by_id(result["features"])["chest"]["reveals"] == [
        {"kind": "item", "id": "healing-potion"}
    ]


def test_get_location_shows_an_item_once_its_place_is_revealed(lore, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    state.world.revealed.append("feature:chest")

    assert items_of(lore, make_ctx(state)) == [{**POTION, "holder": "location:cemetery"}]


def test_get_location_shows_an_item_left_lying_in_the_location(lore, make_ctx, make_state):
    # Брошенный предмет уже снаружи места, где он лежал, даже если место не раскрывали.
    state = make_state(
        location_id="cemetery",
        world=WorldState(item_holders={"healing-potion": "location:cemetery"}),
    )

    assert items_of(lore, make_ctx(state)) == [{**POTION, "holder": "location:cemetery"}]


@pytest.mark.parametrize("holder", ["character:pc-1", "character:ghost", "gone", "npc:fabian"])
def test_get_location_hides_an_item_that_is_held_by_someone_else_or_gone(
    lore, make_ctx, make_state, holder
):
    # Ключ у героя, у того, кого нет в отряде, ушёл из игры или у NPC, которого здесь нет.
    state = make_state(world=WorldState(item_holders={"chest-key": holder}))

    assert items_of(lore, make_ctx(state)) == []


def test_get_location_follows_an_npc_with_the_item_to_another_location(lore, make_ctx, make_state):
    state = make_state(world=WorldState(npc_location={"marta": "cemetery"}))
    ctx = make_ctx(state)

    assert items_of(lore, ctx) == []
    assert items_of(lore, ctx, location_id="cemetery") == [{**CHEST_KEY, "holder": "npc:marta"}]


def test_get_location_still_shows_the_item_of_a_dead_npc(lore, make_ctx, make_state):
    # `give_item` отдаёт и то, что осталось у павшего NPC здесь, так что мастер должен его видеть.
    state = make_state(world=WorldState(npc_alive={"marta": False}))

    assert items_of(lore, make_ctx(state)) == [{**CHEST_KEY, "holder": "npc:marta"}]


def test_get_location_by_id_lists_the_items_of_that_location(lore, make_ctx, make_state):
    state = make_state(world=WorldState(item_holders={"healing-potion": "location:crypt"}))
    ctx = make_ctx(state)

    assert items_of(lore, ctx, location_id="crypt") == [{**POTION, "holder": "location:crypt"}]
    assert items_of(lore, ctx) == [{**CHEST_KEY, "holder": "npc:marta"}]


def test_get_location_items_are_short_and_json_compatible(lore, make_ctx, make_state):
    state = make_state(
        location_id="cemetery", world=WorldState(item_holders={"chest-key": "location:cemetery"})
    )
    state.world.revealed.append("feature:chest")

    result = lore.call(make_ctx(state), "get_location", {}).result
    items = result["items"]

    assert [item["id"] for item in items] == ["chest-key", "healing-potion"]
    # Описание предмета не копируется в ответ: его читают через `get_item`.
    assert all(set(item) == {"id", "name", "holder"} for item in items)
    assert json.loads(json.dumps(result)) == result


def test_get_location_shows_an_unfinished_encounter(lore, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state)

    pending = lore.call(ctx, "get_location", {}).result["encounter"]
    state.world.flags["encounter:risen-bones"] = "active"
    active = lore.call(ctx, "get_location", {}).result["encounter"]

    assert pending["id"] == "risen-bones"
    assert pending["status"] == "pending"
    assert pending["monsters"] == [
        {"monster_id": "skeleton", "count": 3, "zone": "graves"},
        {"monster_id": "zombie", "count": 2, "zone": "lodge"},
    ]
    assert "Скелеты" in pending["tactics"]
    assert active["status"] == "active"


def test_get_location_says_whether_the_fight_is_a_boss_fight(lore, make_ctx, make_state):
    cemetery = lore.call(make_ctx(make_state(location_id="cemetery")), "get_location", {})
    crypt = lore.call(make_ctx(), "get_location", {"location_id": "crypt"})

    assert cemetery.result["encounter"]["boss"] is False
    assert crypt.result["encounter"]["boss"] is True


def test_get_location_gives_the_master_the_secrets_of_the_place(lore, make_ctx):
    crypt = lore.call(make_ctx(), "get_location", {"location_id": "crypt"}).result
    village = lore.call(make_ctx(), "get_location", {}).result

    [secret] = crypt["secrets"]
    assert secret["id"] == "empty-treasury"
    assert "казну" in secret["text"].casefold()
    assert secret["check"] == {"ability": "int", "skill": "investigation", "dc": 15}
    assert secret["reveals"] == [{"kind": "flag", "id": "treasury-empty"}]
    assert village["secrets"] == []


def test_get_location_hides_a_finished_encounter(lore, make_ctx, make_state):
    state = make_state(location_id="cemetery")
    state.world.flags["encounter:risen-bones"] = "done"

    result = lore.call(make_ctx(state), "get_location", {}).result

    assert result["encounter"] is None


def test_get_location_with_an_unknown_id_lists_the_known_ones(lore, make_ctx):
    outcome = lore.call(make_ctx(), "get_location", {"location_id": "moon"})

    assert outcome.result is None
    assert "moon" in outcome.error
    assert "village" in outcome.error and "crypt" in outcome.error


# --- get_npc ---


def test_get_npc_returns_everything_with_the_current_attitude(lore, make_ctx, make_state):
    state = make_state(world=WorldState(npc_attitude={"fabian": -3}))

    outcome = lore.call(make_ctx(state), "get_npc", {"npc_id": "fabian"})

    result = outcome.result
    assert result["id"] == "fabian"
    assert result["name"] == "Фабиан"
    assert result["attitude"] == -3
    assert result["alive"] is True
    assert result["location_id"] == "crypt"
    assert result["statblock"] == "cultist_fanatic"
    assert "Ульрих" in result["secret"]
    assert result["knows"]
    # Связи — из общего графа: все, где Фабиан на любом конце, вместе с тайными.
    assert [r["id"] for r in result["relations"]] == [
        "fabian-debtor-marta",
        "fabian-other-pale-candle",
        "regulars-seeks-fabian",
    ]
    assert result["relations"][0] == {
        "id": "fabian-debtor-marta",
        "a": "npc:fabian",
        "b": "npc:marta",
        "kind": "debtor",
        "note": "Должен ей сорок золотых за комнату и ужины.",
        "secret": False,
        "mutual": False,
    }
    assert result["relations"][1]["secret"] is True
    assert {"appearance", "manner", "motive", "role"} <= result.keys()


def test_get_npc_uses_the_adventure_attitude_until_the_world_changes_it(lore, make_ctx, make_state):
    assert lore.call(make_ctx(), "get_npc", {"npc_id": "fabian"}).result["attitude"] == -1
    assert lore.call(make_ctx(), "get_npc", {"npc_id": "marta"}).result["attitude"] == 0


def test_get_npc_follows_moves_and_deaths(lore, make_ctx, make_state):
    state = make_state(
        world=WorldState(npc_location={"marta": "cemetery"}, npc_alive={"marta": False})
    )

    result = lore.call(make_ctx(state), "get_npc", {"npc_id": "marta"}).result

    assert result["location_id"] == "cemetery"
    assert result["alive"] is False


def test_get_npc_with_an_unknown_id_lists_the_known_ones(lore, make_ctx):
    outcome = lore.call(make_ctx(), "get_npc", {"npc_id": "bob"})

    assert outcome.result is None
    assert "bob" in outcome.error
    assert "marta" in outcome.error and "fabian" in outcome.error


# --- get_world, get_deity, get_faction, get_relations ---


def test_get_world_returns_history_regions_rumors_and_the_villain(lore, make_ctx):
    result = lore.call(make_ctx(), "get_world", {}).result

    assert "граф" in result["history"].casefold()
    assert [region["id"] for region in result["regions"]] == ["marshland", "barrow-hill"]
    assert [rumor["true"] for rumor in result["rumors"]] == [False, False, True]
    assert result["villain"] == "npc:fabian"


def test_get_world_of_an_adventure_without_a_world_is_empty(lore, make_ctx, demo_adventure):
    bare = demo_adventure.model_copy(update={"world": WorldInfo(), "villain": None})

    result = lore.call(make_ctx(adventure=bare), "get_world", {}).result

    assert result == {"history": "", "regions": [], "rumors": [], "villain": None}


def test_get_deity_returns_everything_with_the_secret_and_the_relations(lore, make_ctx):
    result = lore.call(make_ctx(), "get_deity", {"deity_id": "ossmar"}).result

    assert (result["id"], result["name"], result["title"]) == ("ossmar", "Оссмар", "Сторож могил")
    assert result["domains"] == ["смерть", "покой"]
    assert "Ульрих" in result["secret"]
    assert result["avatar_statblock"] is None
    assert [r["id"] for r in result["relations"]] == ["ossmar-enemy-pale-candle"]
    assert {"symbol", "dogma", "appearance", "attitude_to_mortals"} <= result.keys()


def test_get_faction_returns_everything_with_the_secret_and_the_relations(lore, make_ctx):
    regulars = lore.call(make_ctx(), "get_faction", {"faction_id": "regulars"}).result
    candle = lore.call(make_ctx(), "get_faction", {"faction_id": "pale-candle"}).result

    assert (regulars["leader"], regulars["base_location"]) == ("npc:marta", "village")
    assert regulars["attitude_to_party"] == 1
    assert [r["id"] for r in regulars["relations"]] == ["regulars-seeks-fabian"]
    assert (candle["leader"], candle["base_location"]) == (None, None)
    assert "мошенник" in candle["secret"]
    assert [r["id"] for r in candle["relations"]] == [
        "fabian-other-pale-candle",
        "ossmar-enemy-pale-candle",
    ]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("npc:marta", ["fabian-debtor-marta", "marta-worships-veyla"]),
        ("deity:veyla", ["marta-worships-veyla"]),
        ("faction:regulars", ["regulars-seeks-fabian"]),
        ("location:crypt", []),
    ],
)
def test_get_relations_lists_every_relation_with_the_entity_at_either_end(
    lore, make_ctx, ref, expected
):
    result = lore.call(make_ctx(), "get_relations", {"ref": ref}).result

    assert result["ref"] == ref
    assert [r["id"] for r in result["relations"]] == expected


def test_get_relations_marks_the_secret_ones_for_the_master(lore, make_ctx):
    result = lore.call(make_ctx(), "get_relations", {"ref": "faction:pale-candle"}).result

    assert [(r["id"], r["secret"]) for r in result["relations"]] == [
        ("fabian-other-pale-candle", True),
        ("ossmar-enemy-pale-candle", True),
    ]


@pytest.mark.parametrize(
    ("name", "args", "known"),
    [
        ("get_deity", {"deity_id": "thor"}, ("veyla", "ossmar")),
        ("get_faction", {"faction_id": "guild"}, ("regulars", "pale-candle")),
        ("get_relations", {"ref": "npc:bob"}, ("marta", "fabian")),
        ("get_relations", {"ref": "deity:thor"}, ("veyla", "ossmar")),
        ("get_relations", {"ref": "location:moon"}, ("village", "crypt")),
    ],
)
def test_an_unknown_id_lists_the_known_ones(lore, make_ctx, name, args, known):
    outcome = lore.call(make_ctx(), name, args)

    assert outcome.result is None
    assert all(ident in outcome.error for ident in known), outcome.error


@pytest.mark.parametrize(
    "ref",
    ["marta", "item:chest-key", "npc:", ":marta", "clue:clue-diary", "", "NPC:marta", " npc:marta"],
)
def test_get_relations_names_the_allowed_references_for_a_wrong_one(lore, make_ctx, ref):
    outcome = lore.call(make_ctx(), "get_relations", {"ref": ref})

    assert outcome.result is None
    assert "npc:" in outcome.error and "deity:" in outcome.error


@pytest.mark.parametrize(
    ("name", "args", "known"),
    [
        ("get_deity", {"deity_id": ""}, ("veyla", "ossmar")),
        ("get_deity", {"deity_id": "deity:veyla"}, ("veyla", "ossmar")),
        ("get_deity", {"deity_id": "VEYLA"}, ("veyla", "ossmar")),
        ("get_deity", {"deity_id": "v" * 64}, ("veyla", "ossmar")),
        ("get_faction", {"faction_id": "../regulars"}, ("regulars", "pale-candle")),
        ("get_faction", {"faction_id": "regulars "}, ("regulars", "pale-candle")),
        ("get_relations", {"ref": "npc:MARTA"}, ("marta", "fabian")),
        ("get_relations", {"ref": "npc: marta"}, ("marta", "fabian")),
        ("get_relations", {"ref": "npc:marta:veyla"}, ("marta", "fabian")),
        ("get_relations", {"ref": "location:" + "v" * 71}, ("village", "crypt")),
    ],
)
def test_a_foreign_shaped_id_is_unknown_not_guessed(lore, make_ctx, name, args, known):
    # Аргумент пишет модель, а в него мог попасть текст игрока: id сравнивается точно, без
    # приведения регистра, пробелов и путей, и ошибка подсказывает настоящие id.
    outcome = lore.call(make_ctx(), name, args)

    assert outcome.result is None
    assert all(ident in outcome.error for ident in known), outcome.error


def test_without_the_graph_nobody_has_relations(lore, make_ctx, demo_adventure):
    ctx = make_ctx(adventure=demo_adventure.model_copy(update={"relations": ()}))

    assert lore.call(ctx, "get_npc", {"npc_id": "fabian"}).result["relations"] == []
    assert lore.call(ctx, "get_deity", {"deity_id": "ossmar"}).result["relations"] == []
    assert lore.call(ctx, "get_faction", {"faction_id": "pale-candle"}).result["relations"] == []
    assert lore.call(ctx, "get_relations", {"ref": "npc:marta"}).result == {
        "ref": "npc:marta",
        "relations": [],
    }


def test_get_relations_of_a_place_lists_what_is_linked_to_it(lore, make_ctx):
    # В демо мест на концах связей нет; в приключении фабрики Фабиан ищет склеп.
    ctx = make_ctx(adventure=make_adventure())

    result = lore.call(ctx, "get_relations", {"ref": "location:crypt"}).result

    assert [(r["id"], r["a"], r["b"]) for r in result["relations"]] == [
        ("fabian-seeks-crypt", "npc:fabian", "location:crypt")
    ]


# --- relations_of: связи сущности из графа ---


def with_graph(*ends: tuple[str, str]) -> Adventure:
    """Приключение фабрики с графом из связей `rel-<n>` между заданными концами."""
    relations = tuple(
        Relation(id=f"rel-{number}", a=a, b=b, kind="ally")
        for number, (a, b) in enumerate(ends, start=1)
    )
    return make_adventure(relations=relations)


def test_relations_of_takes_both_ends_in_the_order_of_the_graph():
    adventure = with_graph(
        ("npc:marta", "npc:fabian"),
        ("deity:warden", "faction:candle-circle"),
        ("npc:fabian", "npc:marta"),
        ("faction:candle-circle", "npc:marta"),
    )

    found = relations_of(adventure, "npc:marta")

    assert [relation["id"] for relation in found] == ["rel-1", "rel-3", "rel-4"]
    assert found == relations_of(adventure, "npc:marta")  # тот же ответ при каждом вызове


def test_relations_of_matches_the_whole_reference_with_its_kind():
    adventure = with_graph(("npc:marta", "npc:fabian"), ("deity:warden", "faction:candle-circle"))

    assert relations_of(adventure, "npc:mart") == []
    assert relations_of(adventure, "marta") == []
    assert (
        relations_of(adventure, "faction:marta") == []
    )  # тот же id у другого вида — не та сущность
    assert relations_of(adventure, "faction:warden") == []


def test_relations_of_gives_a_relation_once_even_with_both_ends_on_the_entity():
    # Связь с самим собой отвергает валидатор, но не схема: в ответе она всё равно одна.
    adventure = with_graph(("npc:marta", "npc:marta"))

    assert [relation["id"] for relation in relations_of(adventure, "npc:marta")] == ["rel-1"]


def test_relations_of_gives_whole_relations_as_json():
    found = relations_of(make_adventure(), "deity:warden")

    assert found == [
        {
            "id": "warden-enemy-candle-circle",
            "a": "deity:warden",
            "b": "faction:candle-circle",
            "kind": "enemy",
            "note": "",
            "secret": True,
            "mutual": True,
        },
        {
            "id": "marta-worships-warden",
            "a": "npc:marta",
            "b": "deity:warden",
            "kind": "worships",
            "note": "",
            "secret": False,
            "mutual": False,
        },
    ]
    assert json.loads(json.dumps(found)) == found


# --- get_item ---


def test_get_item_returns_description_and_the_holder_from_the_adventure(lore, make_ctx):
    result = lore.call(make_ctx(), "get_item", {"item_id": "chest-key"}).result

    assert result["id"] == "chest-key"
    assert result["name"] == "Запасной ключ Ульриха"
    assert "ключ" in result["description"]
    assert result["holder"] == "npc:marta"


def test_get_item_prefers_the_holder_from_the_world(lore, make_ctx, make_state):
    state = make_state(world=WorldState(item_holders={"chest-key": "character:pc-1"}))

    result = lore.call(make_ctx(state), "get_item", {"item_id": "chest-key"}).result

    assert result["holder"] == "character:pc-1"


def test_get_item_reports_a_gone_item(lore, make_ctx, make_state):
    state = make_state(world=WorldState(item_holders={"healing-potion": "gone"}))

    result = lore.call(make_ctx(state), "get_item", {"item_id": "healing-potion"}).result

    assert result["holder"] == "gone"


def test_get_item_says_which_closed_place_the_item_lies_in(lore, make_ctx, make_state):
    state = make_state(location_id="cemetery")

    inside = lore.call(make_ctx(state), "get_item", {"item_id": "healing-potion"}).result
    state.world.revealed.append("feature:chest")  # сундук открыт
    opened = lore.call(make_ctx(state), "get_item", {"item_id": "healing-potion"}).result

    assert (inside["holder"], inside["inside"]) == ("location:cemetery", "feature:chest")
    assert opened["holder"] == "location:cemetery" and "inside" not in opened


def test_get_item_does_not_say_inside_for_an_item_outside_of_any_place(lore, make_ctx, make_state):
    key = lore.call(make_ctx(), "get_item", {"item_id": "chest-key"}).result
    state = make_state(world=WorldState(item_holders={"healing-potion": "location:cemetery"}))
    dropped = lore.call(make_ctx(state), "get_item", {"item_id": "healing-potion"}).result

    assert "inside" not in key  # ключ у Марты, а не в каком-то месте
    assert "inside" not in dropped  # зелье вынесли и бросили: оно уже снаружи


def test_get_item_with_an_unknown_id_lists_the_known_ones(lore, make_ctx):
    outcome = lore.call(make_ctx(), "get_item", {"item_id": "sword"})

    assert outcome.result is None
    assert "sword" in outcome.error
    assert "chest-key" in outcome.error and "healing-potion" in outcome.error


# --- search_lore ---


def search(lore: ToolRegistry, ctx: ToolContext, query: str, **extra: Any) -> list[dict[str, Any]]:
    outcome = lore.call(ctx, "search_lore", {"query": query} | extra)
    assert outcome.error is None, outcome.error
    return outcome.result["results"]


def test_search_lore_finds_fabian_by_the_word_debt(lore, make_ctx):
    results = search(lore, make_ctx(), "долг")

    assert results[0]["ref"] == "npc:fabian"
    assert results[0]["title"] == "Фабиан"
    assert "долг" in results[0]["snippet"].casefold()
    assert set(results[0]) == {"ref", "title", "snippet"}


def test_search_lore_covers_locations_npcs_items_and_revelations(lore, make_ctx):
    refs = {r["ref"] for r in search(lore, make_ctx(), "ключ", limit=20)}

    assert {"item:chest-key", "npc:marta", "location:cemetery"} <= refs
    revelation_refs = {r["ref"] for r in search(lore, make_ctx(), "Ульрих", limit=20)}
    assert "revelation:kidnapper" in revelation_refs


@pytest.mark.parametrize(
    ("query", "ref"),
    [
        ("торф", "world"),
        ("гати", "region:marshland"),
        ("очага", "deity:veyla"),
        ("взносы", "faction:pale-candle"),
        ("тайник", "location:crypt"),  # тайна места ищется вместе с локацией
    ],
)
def test_search_lore_also_covers_the_world_deities_factions_and_secrets(lore, make_ctx, query, ref):
    assert ref in {r["ref"] for r in search(lore, make_ctx(), query, limit=20)}


@pytest.mark.parametrize(
    ("query", "ref"),
    [("обету", "deity:ossmar"), ("мошенников", "faction:pale-candle")],
)
def test_search_lore_reaches_the_secrets_of_deities_and_factions(lore, make_ctx, query, ref):
    # Поиск — инструмент мастера: тайны богов и фракций ему видны, как секрет NPC.
    [found] = search(lore, make_ctx(), query, limit=20)

    assert found["ref"] == ref
    assert query in found["snippet"]


def test_search_lore_finds_a_rumor_in_the_world(lore, make_ctx):
    results = search(lore, make_ctx(), "взаперти", limit=20)

    assert results[0]["ref"] == "world"
    assert results[0]["title"] == "Мир"


def test_search_lore_ignores_case(lore, make_ctx):
    assert search(lore, make_ctx(), "ДоЛг") == search(lore, make_ctx(), "долг")


def test_search_lore_orders_by_the_number_of_matches(lore, make_ctx):
    refs = [r["ref"] for r in search(lore, make_ctx(), "долг", limit=20)]

    # Слово «долг» трижды у Фабиана и по одному разу у Марты и в открытии: больше совпадений — выше.
    assert refs.index("npc:fabian") < refs.index("npc:marta")
    assert refs.index("npc:fabian") < refs.index("revelation:kidnapper")
    assert len(refs) == len(set(refs))


def test_search_lore_respects_the_limit(lore, make_ctx):
    assert len(search(lore, make_ctx(), "долг", limit=1)) == 1
    assert len(search(lore, make_ctx(), "долг")) <= 5


def test_search_lore_returns_an_empty_list_when_nothing_matches(lore, make_ctx):
    assert search(lore, make_ctx(), "дирижабль") == []


def test_search_lore_snippet_is_short(lore, make_ctx):
    for result in search(lore, make_ctx(), "кладбище", limit=20):
        assert 0 < len(result["snippet"]) <= 200


def test_search_lore_rejects_a_query_made_only_of_punctuation(lore, make_ctx):
    outcome = lore.call(make_ctx(), "search_lore", {"query": " ?! "})

    assert outcome.result is None
    assert "запрос" in outcome.error.casefold()


@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "я" * 201},
        {"query": "долг", "limit": 0},
        {"query": "долг", "limit": 21},
        {"query": "долг", "limit": "много"},
        {"query": "долг", "crit": True},
    ],
)
def test_search_lore_rejects_bad_arguments(lore, make_ctx, args):
    outcome = lore.call(make_ctx(), "search_lore", args)

    assert outcome.result is None and outcome.error


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_location", {"location_id": "я" * 65}),
        ("get_npc", {"npc_id": "я" * 65}),
        ("get_item", {"item_id": "я" * 65}),
        ("get_npc", {}),
        ("get_item", {"item_id": 7}),
        ("get_deity", {"deity_id": "я" * 65}),
        ("get_faction", {"faction_id": "я" * 65}),
        ("get_relations", {"ref": "npc:" + "я" * 80}),
        ("get_relations", {}),
        ("get_world", {"crit": True}),
    ],
)
def test_lore_tools_validate_their_arguments(lore, make_ctx, name, args):
    outcome = lore.call(make_ctx(), name, args)

    assert outcome.result is None and outcome.error
    assert "я" * 50 not in outcome.error


# --- общее свойство инструментов лора ---


LORE_CALLS = [
    ("get_location", {}),
    ("get_location", {"location_id": "cemetery"}),
    ("get_npc", {"npc_id": "marta"}),
    ("get_item", {"item_id": "chest-key"}),
    ("search_lore", {"query": "долг"}),
    ("get_world", {}),
    ("get_deity", {"deity_id": "ossmar"}),
    ("get_faction", {"faction_id": "pale-candle"}),
    ("get_relations", {"ref": "npc:fabian"}),
]


@pytest.mark.parametrize(("name", "args"), LORE_CALLS)
def test_lore_tools_change_nothing_and_make_no_events(lore, make_ctx, make_state, name, args):
    state = make_state(location_id="cemetery")
    ctx = make_ctx(state, secret_actors={"pc-1"}, inspired={"pc-1"})
    before = state.model_dump()

    outcome = lore.call(ctx, name, args)

    assert outcome.error is None
    assert state.model_dump() == before
    assert ctx.events == []
    assert ctx.inspired == {"pc-1"}


@pytest.mark.parametrize(("name", "args"), LORE_CALLS)
def test_lore_results_are_short_json_compatible_dicts(lore, make_ctx, name, args):
    result = lore.call(make_ctx(), name, args).result

    assert json.loads(json.dumps(result)) == result
    assert len(json.dumps(result, ensure_ascii=False)) < 6000


def test_lore_tools_are_registered_with_russian_descriptions_and_schemas():
    names = {spec.name for spec in LORE_TOOLS}

    assert names == {
        "get_location",
        "get_npc",
        "get_item",
        "search_lore",
        "get_world",
        "get_deity",
        "get_faction",
        "get_relations",
    }
    for spec in LORE_TOOLS:
        assert any("а" <= ch.casefold() <= "я" for ch in spec.description), spec.name
        schema = spec.json_schema()
        assert json.loads(json.dumps(schema)) == schema
        assert schema["additionalProperties"] is False
