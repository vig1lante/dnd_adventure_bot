"""Внимание: кто из героев давно не был в фокусе рассказа.

По каждому герою считаются сданные ходы, ходы «в фокусе» и ходы с последнего фокуса. Мастер получает
подсказку о тех, кто ждёт `SPOTLIGHT_HINT_AFTER` ходов и дольше (раздел «Давно не были в фокусе»
контекста).

Что считается:
- «в фокусе» — в событиях хода есть бросок, урон, лечение, новое состояние, действие, предмет,
  вдохновение или личное сообщение с этим героем. Вызывающий передаёт сюда только события, которые
  вызвала модель инструментами хода. Сделанное сервером (длинный отдых в конце привала, передача
  хода в бою со спасбросками от смерти лежащих героев) в фокус не идёт: игрок ничего не делал,
  мастер ничего не решал, а герой без сознания иначе обнулял бы счётчик каждый раунд, и мастер не
  получал бы подсказки вернуть его в рассказ;
- «сданный ход» — игрок не спасовал. На привале ходом игрока служат его реплики у костра: мастер
  получает их в ходе «конец привала» как ходы игроков, и правило «ответь каждому, кто не пасовал»
  действует на них так же. Кто сказал хотя бы одну реплику, сдал ход (сколько бы их ни было), кто
  промолчал — как пасующий.

Функции чистые: состояние не меняют, а возвращают новые значения. Ключи счётчиков — id персонажей
(`GameState.spotlight`, как и `pc_hooks`), а не id игроков. Порог живёт только здесь.
"""

from collections.abc import Collection, Iterable, Iterator

from dnd_engine.conditions import is_dead
from dnd_engine.events import (
    ActionEvent,
    ConditionEvent,
    DamageEvent,
    Event,
    HealEvent,
    InspirationEvent,
    ItemEvent,
    PrivateMessageEvent,
    RollEvent,
)
from dnd_engine.state import GameState, Spotlight

SPOTLIGHT_HINT_AFTER = 3  # ходов без фокуса, после которых мастеру напоминают о герое

_CHARACTER_HOLDER = "character:"


def _event_ids(event: Event) -> Iterator[str]:
    """Кого касается событие: id персонажей и бойцов; отряд отбирает вызывающий."""
    match event:
        case RollEvent(actor_id=actor_id):
            yield actor_id
        case DamageEvent(target_id=target_id) | HealEvent(target_id=target_id):
            yield target_id
        case ConditionEvent(target_id=target_id, added=True):
            yield target_id  # снятие срока в конце хода — не событие в жизни героя
        case ActionEvent(actor_id=actor_id, target_ids=target_ids):
            yield actor_id
            yield from target_ids
        case ItemEvent(holder_from=holder_from, holder_to=holder_to):
            for holder in (holder_from, holder_to):
                if holder.startswith(_CHARACTER_HOLDER):
                    yield holder.removeprefix(_CHARACTER_HOLDER)
        case InspirationEvent(character_id=character_id):
            yield character_id
        case PrivateMessageEvent(audience=audience):
            yield audience


def focused_characters(events: Iterable[Event]) -> set[str]:
    """Кого коснулись события хода (монстры и чужие id отбираются вызывающим)."""
    return {character_id for event in events for character_id in _event_ids(event)}


def update_spotlight(
    state: GameState, submitted: Collection[str], events: Iterable[Event]
) -> dict[str, Spotlight]:
    """Счётчики внимания после хода: по одной записи на каждого героя отряда.

    `submitted` — id героев, чьи игроки сдали ход (пас не в счёт; на привале — сказали реплику).
    `events` — события, которые вызвала модель (не сервер, см. описание модуля). Кто в фокусе, у
    того `turns_since_focus` обнуляется, у остальных растёт. `state` не меняется."""
    focused = focused_characters(events)
    result: dict[str, Spotlight] = {}
    for hero_id in state.party:
        before = state.spotlight.get(hero_id, Spotlight())
        in_focus = hero_id in focused
        result[hero_id] = Spotlight(
            submitted=before.submitted + (hero_id in submitted),
            focused=before.focused + in_focus,
            turns_since_focus=0 if in_focus else before.turns_since_focus + 1,
        )
    return result


def spotlight_hints(state: GameState) -> list[tuple[str, int]]:
    """Герои, которые ждут фокуса `SPOTLIGHT_HINT_AFTER` ходов и дольше: `(id, ходов)` по убыванию.

    Окончательно погибшим (`is_dead`) возвращаться в рассказ некуда, их подсказка не касается;
    лежащий без сознания герой ещё жив, и мастеру стоит напомнить о нём. Ничья решается порядком
    отряда: подсказка не должна зависеть от порядка ключей в базе."""
    waiting = {
        hero_id: counters.turns_since_focus
        for hero_id, counters in state.spotlight.items()
        if hero_id in state.party
        and not is_dead(state.party[hero_id])
        and counters.turns_since_focus >= SPOTLIGHT_HINT_AFTER
    }
    party_order = list(state.party)
    ordered = sorted(waiting, key=lambda hero_id: (-waiting[hero_id], party_order.index(hero_id)))
    return [(hero_id, waiting[hero_id]) for hero_id in ordered]
