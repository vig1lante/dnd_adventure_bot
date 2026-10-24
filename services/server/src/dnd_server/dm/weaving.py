"""Вплетение героев в приключение и открытие игры (спека §8.1 и §10, план B7.8).

После «Начать» игра в фазе `weaving`. `begin_weaving` делает три вещи:

1. Резерв (под блокировкой игры, короткая сессия): игра `weaving`, мастер не занят; в `turn_phase`
   ставится `resolving` («мастер думает», он же признак, что вплетение идёт), собираются приключение
   из копии игры и герои. Второй запрос в это время получает отказ (`Conflict`).
2. Вплетение (без блокировки и без сессии): один вызов `complete_json(purpose="weaving")` со своим
   тайм-аутом (у `complete_json` общего нет) связывает каждого героя с NPC и местами приключения,
   с другими героями и личной целью. Запрос один на весь отряд, поэтому личные сведения героев
   (предыстория, слабости) лежат в нём отдельным разделом «только для его зацепки», а у остальных
   героев в разделе отряда лишь id, имя, класс, раса и замысел (`weaving_request`). Ответ модели —
   недоверенный текст: ссылки на то, чего нет,
   выбрасываются, тексты сводятся к печатной строке и ограничиваются по длине при записи. Если у
   героя не осталось ни одной связи с NPC или местом, запрос повторяется один раз. Сбой повтора
   оставляет первый ответ; герой, которому связей так и не нашлось, играет без них. Результат (после
   проверки ссылок) пишется одной транзакцией: `GameState` версии 1 с `pc_hooks` и личные сообщения
   со своей зацепкой каждому игроку (адресат — игрок героя; чужие их не видят). Рассылка идёт после
   коммита.
3. Открытие игры: `run_dm_turn(opening)` — мастер описывает стартовую локацию и собравшийся отряд;
   удавшийся ход переводит игру в `playing` и `discussion`.

Сбой не оставляет игру в `weaving` навсегда. Ошибка или тайм-аут модели, отмена задачи при остановке
сервера и любой другой сбой вне хода мастера переводят игру в `dm_failed` (фаза остаётся
`weaving`), хост получает системное сообщение и нажимает «Повторить»; сбой самого открытия
записывает `run_dm_turn` (ход `failed`, тот же `dm_failed`). Если не записалось и сообщение хосту,
игра выходит из «мастер думает» короткой отметкой без него (`_abandon_safely`); не прошла и она —
ERROR с id игры, а «Повторить» принимает игру без живого разбора (`dm.turn.LiveRuns`). Повтор
`begin_weaving` продолжает с места остановки: если состояние версии 1 уже записано, зацепки не
просятся заново, а сразу идёт открытие. Игру, прерванную перезапуском сервера, отмечает
`turns.flow.restore_games` (решение D3): сама она не продолжается.

Расход токенов вплетения в `dm_turn_logs` не пишется: таблица привязана к ходу (`turn_id`
обязателен), а хода при вплетении ещё нет, и `complete_json` отдаёт только разобранный JSON. В
журнале сервера остаются число героев с зацепками, повтор запроса и длительность; текстов зацепок
там нет.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.adventure.schema import Adventure, Location, Npc
from dnd_engine.character import Character
from dnd_engine.content import SrdContent
from dnd_engine.state import GameState, PcHook, RulesRef
from dnd_server.characters.flavor import SHORT_TEXT_MAX_CHARS
from dnd_server.db.models import (
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    PlayerCharacter,
    TurnKind,
    TurnPhase,
)
from dnd_server.dm.text import (
    REASON_CANCELLED,
    REASON_INTERNAL,
    ModelTimeout,
    describe_error,
    failure_reason,
    json_line,
    one_line,
    printable_line,
    safe_traceback,
)
from dnd_server.dm.turn import Deps, TurnOutcome, announcing, failure_text, run_dm_turn
from dnd_server.games.hooks import send_to_game
from dnd_server.games.service import Conflict, NotFound
from dnd_server.llm.base import JsonRequest, LLMProvider
from dnd_server.prompts import role_with_style
from dnd_server.realtime.views import feed_item_event, phase_event

logger = logging.getLogger(__name__)

# Сколько ждём один ответ модели, секунд. Вплетение просит обычную модель (`cheap=False`: у
# `anthropic_api` оно не идёт на LLM_CHEAP_MODEL; `claude_code_cli` все ответы по схеме отдаёт
# дешёвой модели подписки, спека §11) и пишет зацепки на всех героев сразу; внутри `complete_json`
# до трёх попыток, а каждую SDK ещё повторяет при 429 и 5xx. Тайм-аут стоит на каждый запрос
# отдельно, повторный запрос — тоже под своим.
WEAVING_TIMEOUT_S = 120.0

# Пределы текстов зацепки: они применяются при записи, ответу модели длина не доверяется. Блок
# данных героев в контексте мастера не обрезается, поэтому каждый герой занимает в нём
# ограниченное место.
GOAL_MAX_CHARS = SHORT_TEXT_MAX_CHARS  # как пределы коротких текстов персонажа (characters.flavor)
SECRET_MAX_CHARS = SHORT_TEXT_MAX_CHARS
RELATION_MAX_CHARS = 60
NOTE_MAX_CHARS = 160
MAX_LINKS = 3  # связей каждого вида у одного героя

_FIELDS = (
    "Поля ответа:",
    "- hooks: список, по одному элементу на героя;",
    "- character_id: id героя из списка героев;",
    f"- npc_links: до {MAX_LINKS} связей с NPC; npc_id из списка NPC, relation (кем герой "
    f"приходится NPC, до {RELATION_MAX_CHARS} знаков), note (деталь, до {NOTE_MAX_CHARS} знаков);",
    f"- location_links: до {MAX_LINKS} мест; location_id из списка мест, note (чем место знакомо "
    f"герою, до {NOTE_MAX_CHARS} знаков);",
    f"- party_links: до {MAX_LINKS} связей с другими героями; character_id другого героя из "
    f"списка, relation (в общих словах, известных обоим героям, до {RELATION_MAX_CHARS} знаков);",
    f"- personal_goal: личная цель героя, до {GOAL_MAX_CHARS} знаков;",
    f"- secret: тайна, известная только этому герою, до {SECRET_MAX_CHARS} знаков; строится только "
    "из личных сведений этого героя.",
)


# --- ответ модели ---


class _Strict(BaseModel):
    """Лишнее поле в ответе — отказ: `complete_json` повторит запрос, а мы лишнего не используем."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NpcLinkOut(_Strict):
    npc_id: str
    relation: str = ""
    note: str = ""


class LocationLinkOut(_Strict):
    location_id: str
    note: str = ""


class PartyLinkOut(_Strict):
    character_id: str
    relation: str = ""


class HookOut(_Strict):
    """Зацепка одного героя от модели. Длины не ограничены схемой: тексты режутся при записи."""

    character_id: str
    npc_links: list[NpcLinkOut] = []
    location_links: list[LocationLinkOut] = []
    party_links: list[PartyLinkOut] = []
    personal_goal: str = ""
    secret: str = ""


class WeavingOut(_Strict):
    hooks: list[HookOut]


# --- состояние версии 1 ---


def new_game_state(game_id: str, adventure: Adventure, party: Sequence[Character]) -> GameState:
    """Состояние игры перед открытием: отряд, старт, модули правил из копии приключения.

    Ключ отряда — `Character.id` (по нему мастер, инструменты и сервер находят героя). Версия 1:
    ход мастера требует состояния, а версии растут только вперёд."""
    state = GameState(
        game_id=game_id,
        adventure_id=adventure.id,
        rules=RulesRef(base=adventure.rules.base, modules=list(adventure.rules.modules)),
        party={hero.id: hero for hero in party},
        location_id=adventure.start_location,
        version=1,
    )
    state.world.visited_locations = [adventure.start_location]
    return state


# --- запрос к модели ---


def weaving_request(
    adventure: Adventure,
    party: Sequence[Character],
    srd: SrdContent,
    *,
    profanity: bool,
    unlinked: Sequence[str] = (),
) -> JsonRequest:
    """Запрос вплетения: приключение без тайн мастера, NPC с мотивами и связями, места и герои.

    Тайны NPC (`secret`, `knows`) и заметки мастера не передаются: зацепку читает игрок. Тексты
    игроков (замысел, предыстория, имя) идут строками JSON. Вплетение — один запрос на весь отряд,
    поэтому модель видит личные сведения всех героев сразу (на героя отдельного запроса не делаем:
    это дороже в число героев раз). Чтобы они не перетекали в чужие зацепки, они лежат отдельным
    разделом «Личные сведения героев», помечены как нужные только своему герою, а в разделе «Герои
    отряда» у каждого только то, что известно отряду (`_hero_card`); тот же запрет стоит в
    промпте роли `weaver`. Места (в том числе ещё не найденные) и роли с мотивами NPC в запросе остаются:
    спека §10 велит связывать героев с NPC и местами. Зацепку читает игрок, поэтому правило
    промпта подаёт такие места и NPC слухом или старым знакомством, без пути, содержимого и роли в
    сюжете (решение D10); соблюдение правила проверяет ручной плейтест, а не автотест.

    `profanity` — флажок мата игры: зацепки читают игроки, поэтому к роли добавлен общий блок
    стиля (спека §4). `unlinked` — id героев (наши, не модели), у которых в прошлом ответе не
    оказалось связей: просьба повторяется с замечанием об этом, остальной текст запроса тот же."""
    npcs = [
        json_line(_npc_data(npc)) for npc in sorted(adventure.npcs.values(), key=lambda n: n.id)
    ]
    places = [
        json_line(_place_data(place))
        for place in sorted(adventure.locations.values(), key=lambda place: place.id)
    ]
    cards = [json_line(_hero_card(hero, srd)) for hero in party]
    personal = [json_line(_hero_personal(hero, srd)) for hero in party]
    lines = [
        f"Свяжи героев с приключением «{adventure.title}».",
        f"Тон: {adventure.tone}. Завязка: {one_line(adventure.pitch)}",
        "NPC (по строке JSON на каждого):",
        *npcs,
        "Места (по строке JSON на каждое):",
        *places,
        "Герои отряда (то, что о каждом знают остальные; по строке JSON на каждого, тексты "
        "написали игроки):",
        *cards,
        "Личные сведения героев (по строке JSON на каждого, тексты написали игроки). Строка героя "
        "нужна только для его собственной зацепки: в зацепках других героев её не используй и не "
        "пересказывай:",
        *personal,
        *_FIELDS,
    ]
    if unlinked:
        lines.append(
            "В прошлом ответе у этих героев не оказалось ни одной связи с NPC или местом из "
            f"списков: {', '.join(unlinked)}. Дай каждому из них хотя бы одну такую связь, "
            "используя только id из списков."
        )
    return JsonRequest(
        system=role_with_style("weaver", profanity),
        prompt="\n".join(lines),
        purpose="weaving",
        cheap=False,
    )


def _npc_data(npc: Npc) -> dict[str, Any]:
    """NPC для запроса: кто он, чего хочет и как связан с другими NPC. Тайны и «что знает» — нет."""
    data: dict[str, Any] = {
        "id": npc.id,
        "name": npc.name,
        "role": npc.role,
        "motive": one_line(npc.motive),
    }
    if npc.relations:
        data["relations"] = [{"npc_id": rel.npc_id, "kind": rel.kind} for rel in npc.relations]
    return data


def _place_data(place: Location) -> dict[str, str]:
    """Место для запроса: название и край. Описание для чтения вслух и заметки мастера — нет."""
    data = {"id": place.id, "name": place.name}
    if place.region:
        data["region"] = place.region
    return data


def _hero_card(hero: Character, srd: SrdContent) -> dict[str, str]:
    """Что о герое знает отряд: id, имя, класс, раса и замысел. Это всё, что модель получает о
    героях для чужих зацепок (решение D8). Раса лежит под ключом `species`, как поле данных."""
    data = {
        "id": hero.id,
        "name": hero.name,
        "class": srd.classes[hero.class_id].name,
        "species": srd.species[hero.species].name,
        "concept": hero.persona.concept,
    }
    return {key: value for key, value in data.items() if value}


def _hero_personal(hero: Character, srd: SrdContent) -> dict[str, str]:
    """Личные сведения героя для его собственной зацепки: предыстория по правилам и тексты игрока.
    Имени и замысла здесь нет: героя называет `id`, остальное лежит в его карточке."""
    persona = hero.persona
    data = {
        "id": hero.id,
        "background": srd.backgrounds[hero.background].name,
        "traits": persona.traits,
        "bonds": persona.bonds,
        "flaws": persona.flaws,
        "backstory": persona.backstory,
    }
    return {key: value for key, value in data.items() if value}


# --- проверка ответа ---


def _links[T](
    raw: Sequence[T],
    target: Callable[[T], str],
    known: Container[str],
    build: Callable[[T], dict[str, str]],
) -> list[dict[str, str]]:
    """Не больше `MAX_LINKS` связей с известными целями; повтор цели — одна связь (первая)."""
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in raw:
        key = target(link)
        if key in known and key not in seen:
            seen.add(key)
            links.append(build(link))
            if len(links) == MAX_LINKS:
                break
    return links


def clean_hooks(
    answer: WeavingOut, party: Mapping[str, Character], adventure: Adventure
) -> dict[str, PcHook]:
    """Зацепки из ответа модели: только настоящие герои, NPC, места и связи, тексты ограничены.

    Герой, которого нет в отряде, и его зацепка выбрасываются; второй элемент про того же героя
    не склеивается с первым. Связь героя с самим собой, с неизвестным NPC, местом или героем
    выбрасывается, повторная связь с той же целью — тоже. Зацепка без единой уцелевшей связи, цели
    и тайны не пишется вовсе."""
    hooks: dict[str, PcHook] = {}
    seen: set[str] = set()
    for item in answer.hooks:
        hero_id = item.character_id
        if hero_id not in party or hero_id in seen:
            continue
        seen.add(hero_id)
        hook = PcHook(
            character_id=hero_id,
            npc_links=_links(
                item.npc_links,
                lambda link: link.npc_id,
                adventure.npcs,
                lambda link: {
                    "npc_id": link.npc_id,
                    "relation": printable_line(link.relation, RELATION_MAX_CHARS),
                    "note": printable_line(link.note, NOTE_MAX_CHARS),
                },
            ),
            location_links=_links(
                item.location_links,
                lambda link: link.location_id,
                adventure.locations,
                lambda link: {
                    "location_id": link.location_id,
                    "note": printable_line(link.note, NOTE_MAX_CHARS),
                },
            ),
            party_links=_links(
                item.party_links,
                lambda link: link.character_id,
                party.keys() - {hero_id},
                lambda link: {
                    "character_id": link.character_id,
                    "relation": printable_line(link.relation, RELATION_MAX_CHARS),
                },
            ),
            personal_goal=printable_line(item.personal_goal, GOAL_MAX_CHARS),
            secret=printable_line(item.secret, SECRET_MAX_CHARS),
        )
        if hook.model_dump(exclude={"character_id"}, exclude_defaults=True):
            hooks[hero_id] = hook
    return hooks


def _is_linked(hook: PcHook | None) -> bool:
    """Герой привязан к миру приключения: у него есть связь с NPC или местом. Связи с другими
    героями и личная цель мир приключения не раскрывают."""
    return hook is not None and bool(hook.npc_links or hook.location_links)


def unlinked_heroes(hooks: Mapping[str, PcHook], party: Mapping[str, Character]) -> list[str]:
    """Герои отряда (по порядку отряда) без связи с NPC или местом приключения."""
    return [hero_id for hero_id in party if not _is_linked(hooks.get(hero_id))]


def _best(
    repeated: Mapping[str, PcHook], earlier: Mapping[str, PcHook], party: Mapping[str, Character]
) -> dict[str, PcHook]:
    """Зацепки после повторного запроса: у каждого героя лучшая из двух, повторная в приоритете.

    Лучшая — с привязкой к миру (`_is_linked`); если её нет ни в одном ответе, берётся любая."""
    best: dict[str, PcHook] = {}
    for hero_id in party:
        candidates = [hooks[hero_id] for hooks in (repeated, earlier) if hero_id in hooks]
        chosen = next((hook for hook in candidates if _is_linked(hook)), None)
        if chosen is None and candidates:
            chosen = candidates[0]
        if chosen is not None:
            best[hero_id] = chosen
    return best


# --- личное сообщение игроку ---


def _described(name: str, relation: str, note: str) -> str:
    text = f"{name} — {relation}" if relation else name
    return f"{text} ({note})" if note else text


def hook_message(
    hero: Character, hook: PcHook, adventure: Adventure, party: Mapping[str, Character]
) -> str:
    """Текст личного сообщения со своей зацепкой: цель, знакомые, места, спутники, тайна.

    NPC, места и спутники названы по именам (id игроку ни к чему); пустые части пропускаются.
    Тайны NPC и заметок мастера здесь нет: их не было и в запросе к модели."""
    sections = {
        "Личная цель": hook.personal_goal,
        "Знакомые": "; ".join(
            _described(adventure.npcs[link["npc_id"]].name, link["relation"], link["note"])
            for link in hook.npc_links
        ),
        "Места": "; ".join(
            _described(adventure.locations[link["location_id"]].name, "", link["note"])
            for link in hook.location_links
        ),
        "Спутники": "; ".join(
            _described(party[link["character_id"]].name, link["relation"], "")
            for link in hook.party_links
        ),
        "Тайна": hook.secret,
    }
    lines = [f"Твоя зацепка, {hero.name}."]
    lines += [f"{title}: {text}" for title, text in sections.items() if text]
    return "\n".join(lines)


# --- запуск ---


@dataclass
class _Plan:
    """Всё, что нужно вплетению после резерва; обычные данные, без сессии базы."""

    game_id: str
    adventure: Adventure
    heroes: list[Character]  # по порядку входа игроков; `player_id` — игрок героя
    provider_name: str | None
    model: str | None
    host_user_id: str
    profanity: bool  # флажок мата игры: зацепки читают игроки

    @property
    def party(self) -> dict[str, Character]:
        return {hero.id: hero for hero in self.heroes}


class _Stale(Exception):
    """Игра изменилась за время вплетения: результат записывать нельзя."""


async def begin_weaving(
    game_id: str, *, deps: Deps, on_reserved: Callable[[], None] | None = None
) -> TurnOutcome | None:
    """Вплетает героев в приключение и открывает игру; возвращает итог открытия.

    `None` — до хода дело не дошло: вплетение сорвалось, игра в `dm_failed`, хост видит сообщение
    и повторяет. Итог `FAILED` — сорвалось само открытие (его записал `run_dm_turn`). Если ход даже
    не начался, поднимается `NotFound` (нет игры) или `Conflict` (игра не в начале, вплетение уже
    идёт): в игре ничего не менялось. Повтор после сбоя продолжает с места остановки.
    `on_reserved` вызывается один раз, когда игра уже в «мастер думает» и до обращения к модели."""
    with deps.live.running(game_id):  # с самого начала: см. `dm.turn.LiveRuns`
        plan = await _reserve(game_id, deps)
        if plan is None:
            # Вплетение записано раньше, осталось открытие игры (повтор после его сбоя).
            return await run_dm_turn(
                game_id, mode=TurnKind.OPENING, deps=deps, on_reserved=on_reserved
            )
        if on_reserved is not None:
            try:
                on_reserved()
            except Exception as exc:
                logger.error(
                    "Сбой on_reserved вплетения (игра %s).\n%s",
                    game_id,
                    safe_traceback(exc, deps.settings),
                )
        try:
            hooks = await _weave(deps, plan)
            await _write_hooks(deps, plan, hooks)
            return await run_dm_turn(game_id, mode=TurnKind.OPENING, deps=deps)
        except asyncio.CancelledError:
            # Задачу отменили (остановка сервера): игра не должна зависнуть в «мастер думает».
            await asyncio.shield(_abandon_safely(deps, game_id, REASON_CANCELLED))
            raise
        except Exception as exc:
            reason = _log_failure(deps, game_id, exc)
            await _abandon_safely(deps, game_id, reason)
            return None


async def _reserve(game_id: str, deps: Deps) -> _Plan | None:
    """Резерв под блокировкой игры; `None` — состояние версии 1 уже записано, вплетать нечего."""
    async with deps.locks.lock(game_id), deps.sessionmaker() as session:
        game = await session.get(Game, game_id)
        if game is None:
            raise NotFound("Игра не найдена.")
        if game.phase != GamePhase.WEAVING:
            raise Conflict("Игра сейчас не в начале: героев вплетать некуда.")
        if game.turn_phase == TurnPhase.RESOLVING:
            raise Conflict("Мастер уже вплетает героев в историю: дождитесь начала игры.")
        if game.state_version >= 1:
            return None
        plan = await _load(session, game)
        game.turn_phase = TurnPhase.RESOLVING
        game.deadline_at = None
        await session.commit()
        with announcing(deps, game_id, None):
            await send_to_game(deps.broadcaster, game_id, phase_event(game, None))
    return plan


async def _load(session: AsyncSession, game: Game) -> _Plan:
    players = list(
        await session.scalars(
            select(GamePlayer)
            .where(GamePlayer.game_id == game.id, GamePlayer.character_id.is_not(None))
            .order_by(GamePlayer.joined_order)
        )
    )
    rows = {
        row.id: row
        for row in await session.scalars(
            select(PlayerCharacter).where(PlayerCharacter.game_id == game.id)
        )
    }
    heroes: list[Character] = []
    for player in players:
        row = rows.get(player.character_id)
        if row is None:
            raise ValueError(f"у игрока {player.user_id} нет персонажа {player.character_id}")
        hero = Character.model_validate(row.data)
        if hero.id != row.id:
            raise ValueError(f"id персонажа {row.id} не совпадает с id в его данных")
        # Игрок героя — тот, кто занимает место в игре: по нему сообщения находят адресата.
        heroes.append(hero.model_copy(update={"player_id": player.user_id}))
    if not heroes:
        raise ValueError("в игре нет ни одного персонажа")
    return _Plan(
        game_id=game.id,
        adventure=Adventure.model_validate(game.adventure_data),
        heroes=heroes,
        provider_name=game.provider,
        model=game.model,
        host_user_id=game.host_user_id,
        profanity=game.profanity,
    )


async def _weave(deps: Deps, plan: _Plan) -> dict[str, PcHook]:
    """Зацепки от модели (вне блокировки и сессии): запрос, проверка ссылок, один повтор."""
    started = time.monotonic()
    provider = deps.providers.of(
        plan.game_id,
        deps.settings,
        provider=plan.provider_name,
        model=plan.model,
        pc_ids=tuple(hero.id for hero in plan.heroes),
        turn_tokens=deps.turn_tokens,
    )
    party = plan.party
    asked = weaving_request(plan.adventure, plan.heroes, deps.srd, profanity=plan.profanity)
    hooks = clean_hooks(await _ask(provider, asked), party, plan.adventure)
    repeated = False
    if missing := unlinked_heroes(hooks, party):
        repeated = True
        again = weaving_request(
            plan.adventure, plan.heroes, deps.srd, profanity=plan.profanity, unlinked=missing
        )
        try:
            second = clean_hooks(await _ask(provider, again), party, plan.adventure)
        except Exception as exc:  # повтор — улучшение, а не условие: первый ответ уже есть
            logger.warning(
                "Повторный запрос вплетения игры %s не удался (%s): остаётся первый ответ.",
                plan.game_id,
                describe_error(exc, deps.settings),
            )
        else:
            hooks = _best(second, hooks, party)
    if missing := unlinked_heroes(hooks, party):
        logger.warning(
            "Вплетение игры %s: у героев %s нет связи с NPC или местом, они играют без неё.",
            plan.game_id,
            ", ".join(missing),
        )
    logger.info(
        "Вплетение героев игры %s: зацепки у %d из %d героев, повтор запроса: %s, %d мс.",
        plan.game_id,
        len(hooks),
        len(party),
        "да" if repeated else "нет",
        round((time.monotonic() - started) * 1000),
    )
    return hooks


async def _ask(provider: LLMProvider, request: JsonRequest) -> WeavingOut:
    """Один запрос к модели под своим тайм-аутом."""
    deadline = asyncio.timeout(WEAVING_TIMEOUT_S)
    try:
        async with deadline:
            return await provider.complete_json(request, WeavingOut)
    except TimeoutError:
        # Свой тайм-аут отличаем от тайм-аута, который поднял сам провайдер; хосту оба — одно.
        if deadline.expired():
            raise ModelTimeout(f"тайм-аут: больше {WEAVING_TIMEOUT_S:g} с") from None
        raise ModelTimeout("тайм-аут провайдера") from None


async def _write_hooks(deps: Deps, plan: _Plan, hooks: Mapping[str, PcHook]) -> None:
    """Одна транзакция: состояние версии 1 с зацепками и личные сообщения. Рассылка — после коммита.

    Игра должна остаться такой, какой её зарезервировали (иначе `_Stale`). Фаза хода снова пуста:
    следом идёт открытие игры, которое занимает её само (`dm.turn`)."""
    async with deps.locks.lock(plan.game_id), deps.sessionmaker() as session:
        game = await session.get(Game, plan.game_id, populate_existing=True)
        if (
            game is None
            or game.phase != GamePhase.WEAVING
            or game.turn_phase != TurnPhase.RESOLVING
            or game.state_version != 0
            or game.current_turn_id is not None
        ):
            raise _Stale("игра изменилась за время вплетения")
        state = new_game_state(plan.game_id, plan.adventure, plan.heroes)
        state.pc_hooks = dict(hooks)
        party = plan.party
        messages = [
            Message(
                game_id=plan.game_id,
                turn_id=None,
                audience=hero.player_id,
                kind=MessageKind.PRIVATE,
                text=hook_message(hero, hooks[hero.id], plan.adventure, party),
            )
            for hero in plan.heroes
            if hero.id in hooks and hero.player_id is not None
        ]
        session.add_all([GameStateSnapshot.from_state(state), *messages])
        game.state_version = state.version
        game.turn_phase = None
        await session.commit()
        with announcing(deps, plan.game_id, None):
            for message in messages:
                await send_to_game(
                    deps.broadcaster, plan.game_id, feed_item_event(message), message.audience
                )


# --- сбой ---


def _log_failure(deps: Deps, game_id: str, exc: Exception) -> str:
    """Пишет сбой в журнал сервера и возвращает причину для хоста."""
    reason = failure_reason(exc)
    if reason == REASON_INTERNAL:
        # Трассировка нужна для поиска бага; значения секретов и входа pydantic в ней нет.
        logger.error(
            "Вплетение героев игры %s сорвалось из-за ошибки сервера:\n%s",
            game_id,
            safe_traceback(exc, deps.settings),
        )
    else:
        logger.warning(
            "Вплетение героев игры %s не удалось: %s. Игра ждёт «Повторить».",
            game_id,
            describe_error(exc, deps.settings),
        )
    return reason


async def _abandon_safely(deps: Deps, game_id: str, reason: str) -> None:
    """`dm_failed` с сообщением хосту; если запись не удалась, игра всё равно выходит из «мастер
    думает» короткой отметкой без сообщения (диск полон, база занята). Не прошла и она — ERROR с id
    игры, а «Повторить» примет игру без живого разбора (`dm.turn.LiveRuns`)."""
    note = failure_text(TurnKind.OPENING, reason)
    try:
        await _abandon(deps, game_id, note)
    except Exception as exc:
        logger.error(
            "Не удалось записать сбой вплетения игры %s: игра выходит из «мастер думает» "
            "короткой отметкой.\n%s",
            game_id,
            safe_traceback(exc, deps.settings),
        )
    else:
        return
    try:
        await _abandon(deps, game_id, None)
    except Exception as exc:
        logger.error(
            "Не удалось отметить сбой вплетения игры %s даже коротко: игра осталась в «мастер "
            "думает», хост может нажать «Повторить».\n%s",
            game_id,
            safe_traceback(exc, deps.settings),
        )


async def _abandon(deps: Deps, game_id: str, note_text: str | None) -> None:
    """Игра в начале, мастера нет: `dm_failed` и сообщение хосту (`note_text`), чтобы он мог
    повторить.

    Ничего не делает, если игра уже ушла дальше: у неё есть ход (сбой открытия записал
    `run_dm_turn`) или её фаза другая."""
    async with deps.locks.lock(game_id), deps.sessionmaker() as session:
        game = await session.get(Game, game_id, populate_existing=True)
        if (
            game is None
            or game.phase != GamePhase.WEAVING
            or game.current_turn_id is not None
            or game.turn_phase not in (None, TurnPhase.RESOLVING)
        ):
            return
        game.turn_phase = TurnPhase.DM_FAILED
        game.deadline_at = None
        note = None
        if note_text is not None:
            note = Message(
                game_id=game_id,
                turn_id=None,
                audience=game.host_user_id,
                kind=MessageKind.SYSTEM,
                text=note_text,
            )
            session.add(note)
        await session.commit()
        with announcing(deps, game_id, None):
            row = (
                await session.get(GameStateSnapshot, (game_id, game.state_version))
                if game.state_version >= 1
                else None
            )
            state = None if row is None else row.to_state()
            if note is not None:
                await send_to_game(deps.broadcaster, game_id, feed_item_event(note), note.audience)
            await send_to_game(deps.broadcaster, game_id, phase_event(game, state))
