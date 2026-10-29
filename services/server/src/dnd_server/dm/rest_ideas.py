"""Идеи для рассказа у костра: служебная роль `campfire_host` (спека DND-2 §13, блок B9).

В начале привала любого вида — по кнопке хоста (`turns.flow.start_rest`) или ходом мастера со
`start_camp` (`dm.turn`) — после коммита начала идёт одна фоновая задача `offer_rest_ideas`: один
дешёвый вызов `complete_json(purpose="campfire.ideas", cheap=True)` просит личные идеи «расскажи о
себе». Адресаты (`idea_addressees`): на долгом привале — каждый живой герой с игроком, на коротком —
двое, кто дольше всех вне фокуса рассказа (`Spotlight.turns_since_focus`, при равенстве — по порядку
отряда). Идея уходит только игроку своего героя (кадр `rest_idea`) и лежит в `games.rest_ideas`,
откуда её берёт снимок этого игрока; в ленту и в состояние игры идеи не попадают.

Тайн в запросе нет (дополнение контракта B9, §1 п. 16): ни тайны зацепки героя (`PcHook.secret`), ни
тайн и заметок приключения (`secret`, `dm_notes`, `knows`, мотивы NPC, мир), ни летописи мастера, ни
личных записей дневника и личных сообщений. Запрос один на всех адресатов, поэтому, как у вплетения,
у каждого героя в разделе отряда только то, что знают все (имя, класс, раса, предыстория, замысел),
а предыстория своими словами и открытая часть зацепки лежат отдельным разделом «только для идеи
своего героя». Недавние события — последние общие рассказы мастера.

Задача не мешает игре: пока ждёт модель, она не держит ни блокировку игры, ни базу; у вызова свой
тайм-аут (у `complete_json` общего нет). Записывает она, только если идёт тот же привал: тот же
открытый ход и фаза `camp` (иначе привал успел кончиться или его откатили — идеи отбрасываются).
Любой сбой — тайм-аут, отказ провайдера, ответ не по схеме, неожиданная ошибка — пишется в журнал
одной строкой без текстов, а привал идёт дальше без идей. Ответ модели — недоверенный текст:
идеи чужим и лишним героям отбрасываются, у героя остаётся первая идея, текст сводится к печатной
строке не длиннее `IDEA_MAX_CHARS`.

Фильтр ответа (`IdeaGuard`, fix-раунд 1 B9, находка S1). Личные разделы всех адресатов лежат в
одном запросе, и модель может перенести сведения одного героя в идею другого (роль это запрещает).
Поэтому идея героя B отбрасывается, если в ней есть фрагмент личных сведений другого адресата A:
тройка слов подряд из них (нижний регистр, без знаков препинания), пара значимых слов подряд (обе
от `SIGNIFICANT_LETTERS` букв: короткая цель «найти брата» тройки не даёт) или имя NPC и места из
связей A.
Не в счёт то, что знает и сам B (его сведения и связи), и то, что знают все: карточки отряда,
приключение и место, недавние общие рассказы. Ловится дословный перенос, пересказ своими словами —
нет: это остаточный риск того же рода, что у мастера и вплетения. Ложное срабатывание лишь снимает
необязательную идею; в журнал идёт только число отброшенных, без текстов.

Усиление B11 (находка S4 блока B9). Перед сверкой обе стороны нормализуются одинаково
(`_normalized`): NFKC, без знаков сочетания (ударение, «й» читается как «и») и невидимых знаков
форматирования (мягкий перенос, нулевая ширина — и их видимая запись `\xad` после
`printable_line`), латинские буквы-двойники — кириллицей, дефисы внутри слова убраны. Серия из
`GLUED_LETTERS` и больше одиночных букв («н а й т и») склеивается в одно слово и сверяется ещё и
как строка: в ней ищутся склеенные фрагменты и имена. Слова с заглавной буквы не в начале
предложения из личных текстов A — имена собственные (`_proper_names`): идея B с таким именем
(тоже с заглавной, в любом падеже) отбрасывается. Чтобы реже снимать безобидные идеи (находка N1),
у коротких имён окончание не длиннее `SHORT_NAME_ENDING` букв: «Брод» — «Броде», но не «Бродяга».
Перестановка слов, вставка слова и транслит не ловятся: защита от них — только отдельный запрос на
адресата (решение владельца).
"""

import asyncio
import logging
import re
import time
import unicodedata
from bisect import bisect_left
from itertools import islice
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dnd_engine.adventure.loader import adventure_from_data
from dnd_engine.adventure.schema import Adventure
from dnd_engine.character import Character
from dnd_engine.conditions import is_dead
from dnd_engine.content import SrdContent
from dnd_engine.state import GameState, PcHook, Spotlight
from dnd_server.db.models import (
    AUDIENCE_ALL,
    Game,
    GamePhase,
    GamePlayer,
    GameStateSnapshot,
    Message,
    MessageKind,
    TurnPhase,
)
from dnd_server.dm.camp_flow import RestKind, rest_kind
from dnd_server.dm.text import json_line, printable_line
from dnd_server.games.hooks import send_to_game
from dnd_server.llm.base import JsonRequest, LLMProvider, ProviderError
from dnd_server.prompts import role_with_style
from dnd_server.realtime.views import audience_to_user, rest_idea_event

if TYPE_CHECKING:
    from dnd_server.dm.turn import Deps

logger = logging.getLogger(__name__)

IDEAS_PURPOSE = "campfire.ideas"
# Текст журнала о сбое фоновой задачи идей (её запускают `dm.turn` и `turns.flow`).
IDEAS_TASK_FAILED = "Фоновая задача идей привала завершилась ошибкой."
IDEA_MAX_CHARS = 300  # идея — пара фраз на карточке игрока
IDEAS_TIMEOUT_S = 60.0  # на вызов модели; дешёвая модель отвечает за секунды, это запас
SHORT_REST_ADDRESSEES = 2  # на коротком привале идеи получают двое, дольше всех вне фокуса
RECENT_STORIES = 3  # недавние общие рассказы мастера в запросе
STORY_MAX_CHARS = 600  # рассказ в запросе: начало сцены, а не весь текст

_KIND_LINES: Mapping[RestKind, str] = {
    "short": "Привал короткий: около часа отдыха, герои перевязывают раны и переводят дух.",
    "long": "Привал долгий: отряд ночует у костра до утра.",
}
_FIELDS = (
    "Поля ответа:",
    "- ideas: список, по одной идее на каждого героя из раздела адресатов;",
    "- character_id: id героя из раздела адресатов;",
    f"- text: идея для игрока этого героя, до {IDEA_MAX_CHARS} знаков: о чём герою рассказать у "
    "костра, обращение к игроку на «ты».",
)


class IdeaOut(BaseModel):
    """Идея одному герою. Лишнее поле — отказ: `complete_json` повторит запрос."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    character_id: str
    text: str = Field(min_length=1, max_length=IDEA_MAX_CHARS)


class IdeasOut(BaseModel):
    """Ответ `campfire_host`: идеи адресатам привала."""

    model_config = ConfigDict(extra="forbid")

    ideas: list[IdeaOut]


# --- кто получает идеи и о чём спросить модель ---


def idea_addressees(state: GameState) -> list[str]:
    """Id героев, которым нужна идея на этом привале.

    Только живые герои с игроком: погибшему рассказывать некому, герою без игрока — некуда
    отправить. Долгий привал — все такие герои по порядку отряда; короткий — `SHORT_REST_ADDRESSEES`
    дольше всех вне фокуса, при равенстве — по порядку отряда (не по порядку ключей в базе)."""
    order = list(state.party)
    living = [hero_id for hero_id in order if _can_listen(state.party[hero_id])]
    if rest_kind(state) == "long":
        return living
    waiting = sorted(
        living,
        key=lambda hero_id: (
            -state.spotlight.get(hero_id, Spotlight()).turns_since_focus,
            order.index(hero_id),
        ),
    )
    return waiting[:SHORT_REST_ADDRESSEES]


def _can_listen(hero: Character) -> bool:
    return bool(hero.player_id) and not is_dead(hero)


def ideas_request(
    state: GameState,
    adventure: Adventure,
    srd: SrdContent,
    addressees: Sequence[str],
    stories: Sequence[str],
    *,
    profanity: bool,
) -> JsonRequest:
    """Запрос идей: вид привала, где отряд, герои (общее), адресаты (своё) и недавние события.

    Тексты игроков и рассказы мастера идут строками JSON, как данные. `profanity` — флажок мата игры:
    идею читает игрок, поэтому к роли добавлен общий блок стиля."""
    lines = [
        "Приготовь идеи для рассказа у костра: каждому герою из раздела адресатов одну идею.",
        _KIND_LINES[rest_kind(state)],
        f"Приключение «{adventure.title}». Отряд сейчас: {_place_name(state, adventure)}.",
        "Герои отряда (то, что о каждом знают все; по строке JSON на каждого, тексты написали "
        "игроки):",
        *(json_line(_hero_card(hero, srd)) for hero in state.party.values()),
        "Адресаты (по строке JSON на каждого). Строка героя нужна только для его собственной "
        "идеи: в идеях других героев её не используй и не пересказывай:",
        *(json_line(_addressee(state, adventure, hero_id)) for hero_id in addressees),
        "Недавние события, которые видели все (рассказы мастера от старых к новым, строки JSON; "
        "пусто — событий ещё не было):",
        *(json_line(story) for story in stories),
        *_FIELDS,
    ]
    return JsonRequest(
        system=role_with_style("campfire_host", profanity),
        prompt="\n".join(lines),
        purpose=IDEAS_PURPOSE,
        cheap=True,
    )


def _place_name(state: GameState, adventure: Adventure) -> str:
    place = adventure.locations.get(state.location_id)
    return place.name if place else state.location_id


def _hero_card(hero: Character, srd: SrdContent) -> dict[str, str]:
    """Что о герое знает весь отряд: id, имя, класс, раса, предыстория по правилам и замысел."""
    data = {
        "id": hero.id,
        "name": hero.name,
        "class": srd.classes[hero.class_id].name,
        "species": srd.species[hero.species].name,
        "background": srd.backgrounds[hero.background].name,
        "concept": hero.persona.concept,
    }
    return {key: value for key, value in data.items() if value}


def _addressee(state: GameState, adventure: Adventure, hero_id: str) -> dict[str, Any]:
    """Сведения для идеи одного героя: предыстория своими словами и открытая часть зацепки.

    Тайны зацепки (`secret`) здесь нет: её не пересказывают даже своему герою, а запрос один на
    всех. NPC, места и спутники названы по именам, как в личном сообщении вплетения."""
    hero = state.party[hero_id]
    data: dict[str, Any] = {"id": hero_id, "backstory": hero.persona.backstory}
    hook = state.pc_hooks.get(hero_id)
    if hook is not None:
        data |= _open_hook(hook, adventure, state)
    return {key: value for key, value in data.items() if value}


def _open_hook(hook: PcHook, adventure: Adventure, state: GameState) -> dict[str, Any]:
    npcs = adventure.npcs
    places = adventure.locations
    return {
        "personal_goal": hook.personal_goal,
        "acquaintances": [
            _link(npcs[link["npc_id"]].name, link.get("relation", ""), link.get("note", ""))
            for link in hook.npc_links
            if link.get("npc_id") in npcs
        ],
        "places": [
            _link(places[link["location_id"]].name, "", link.get("note", ""))
            for link in hook.location_links
            if link.get("location_id") in places
        ],
        "companions": [
            _link(state.party[link["character_id"]].name, link.get("relation", ""), "")
            for link in hook.party_links
            if link.get("character_id") in state.party
        ],
    }


def _link(name: str, relation: str, note: str) -> str:
    text = f"{name} — {relation}" if relation else name
    return f"{text} ({note})" if note else text


def _link_names(state: GameState, adventure: Adventure, hero_id: str) -> list[str]:
    """Имена NPC и мест из открытой зацепки героя: какие из них он знает лично."""
    hook = state.pc_hooks.get(hero_id)
    if hook is None:
        return []
    npcs = [
        adventure.npcs[link["npc_id"]].name
        for link in hook.npc_links
        if link.get("npc_id") in adventure.npcs
    ]
    places = [
        adventure.locations[link["location_id"]].name
        for link in hook.location_links
        if link.get("location_id") in adventure.locations
    ]
    return npcs + places


# --- фильтр ответа: личные сведения другого адресата ---

_WORD = re.compile(r"\w+")
Fragment = tuple[str, ...]  # тройка слов подряд или пара значимых слов подряд
# Пара слов — фрагмент, когда оба слова не короче этого: предлоги и союзы («ей за») не в счёт.
SIGNIFICANT_LETTERS = 4
# Видимая запись невидимого знака, которую оставляет `printable_line` (`\xad`, `\u200b`).
_ESCAPED = re.compile(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})")
# Дефисы между буквами одного слова («на-йти»); тире между словами остаётся разделителем.
_INNER_HYPHENS = re.compile(r"(?<=\w)[-\u2010\u2011\u2012\u2043\ufe63\uff0d]+(?=\w)")
# Латинские буквы, похожие на кириллические, — кириллицей.
_LOOKALIKES = str.maketrans("ABCEHKMOPTXYaceopxyk", "АВСЕНКМОРТХУасеорхук")
# Столько одиночных букв подряд («н а й т и») — это слово, разбитое пробелами.
GLUED_LETTERS = 3
# Короткое имя (не длиннее `SHORT_NAME`) узнаётся с окончанием не длиннее этого (находка N1).
SHORT_NAME = 4
SHORT_NAME_ENDING = 2
# Знаки конца предложения: слово после них с заглавной — не обязательно имя.
_SENTENCE_ENDS = frozenset(".!?…")


def _normalized(text: str) -> str:
    """Текст для сверки с сохранённым регистром: без невидимых знаков и знаков сочетания, с
    кириллицей вместо латинских двойников и без дефисов внутри слов."""
    text = unicodedata.normalize("NFKC", _ESCAPED.sub("", text))
    kept = (
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) not in ("Mn", "Cf")
    )
    return _INNER_HYPHENS.sub("", "".join(kept).translate(_LOOKALIKES))


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Слова нормализованного текста с регистром и признаком «склеено из одиночных букв»."""
    tokens: list[tuple[str, bool]] = []
    letters: list[str] = []
    for word in [*_WORD.findall(_normalized(text)), ""]:  # "" закрывает последнюю серию
        if len(word) == 1:
            letters.append(word)
            continue
        if len(letters) >= GLUED_LETTERS:
            tokens.append(("".join(letters), True))
        else:
            tokens += [(letter, False) for letter in letters]
        letters = []
        if word:
            tokens.append((word, False))
    return tokens


def _words(text: str) -> list[str]:
    """Слова текста для сверки в нижнем регистре, без знаков препинания (`_tokens`)."""
    return [word.lower() for word, _glued in _tokens(text)]


def _proper_names(text: str) -> list[str]:
    """Слова с заглавной буквы не в начале предложения: имена собственные чужого текста.

    Один проход по словам: начало предложения — первое слово текста и слово, перед которым (после
    прошлого слова) стоит знак конца предложения. Каждый знак текста смотрится один раз, поэтому
    подделанная предыстория («!» тысячу раз и много слов с заглавной) не стоит квадратичного
    поиска (находка S1 блока B11)."""
    normalized = _normalized(text)
    names: list[str] = []
    previous_end = 0
    for found in _WORD.finditer(normalized):
        gap = normalized[previous_end : found.start()]
        starts_sentence = previous_end == 0 or not _SENTENCE_ENDS.isdisjoint(gap)
        if found.group()[0].isupper() and not starts_sentence:
            names.append(found.group().lower())
        previous_end = found.end()
    return names


def _fragments(texts: Iterable[str]) -> frozenset[Fragment]:
    """Фрагменты текстов для сверки: тройки слов подряд и пары значимых слов подряд."""
    return frozenset(fragment for text in texts for fragment in _fragments_of(_words(text)))


def _fragments_of(words: Sequence[str]) -> set[Fragment]:
    triples = {(a, b, c) for a, b, c in zip(words, words[1:], words[2:], strict=False)}
    pairs = {
        (a, b)
        for a, b in zip(words, words[1:], strict=False)
        if min(len(a), len(b)) >= SIGNIFICANT_LETTERS
    }
    return triples | pairs


def _stem(name_word: str) -> str:
    """Неизменная часть слова имени: короткое — целиком, длинное — без одной-двух букв конца."""
    size = len(name_word)
    return name_word if size <= SHORT_NAME else name_word[: -1 if size == 5 else -2]


def _same_word(name_word: str, word: str) -> bool:
    """Слово имени в тексте с русским окончанием: «Марта» — «Марте», «Мшистый Брод» — «Мшистого
    Брода». Короткое слово — с прибавленным окончанием не длиннее `SHORT_NAME_ENDING` букв
    («Брод» — «Бродом», но не «Бродяга»), длинное — ещё и со сменой одной-двух букв конца. Грубо,
    но ложное срабатывание лишь снимает необязательную идею."""
    longest = SHORT_NAME_ENDING if len(name_word) <= SHORT_NAME else 3
    return word.startswith(_stem(name_word)) and len(word) - len(name_word) <= longest


Name = tuple[str, ...]  # слова имени после `_words`: считаются один раз, при сборке фильтра


def _in_glued(parts: Name, glued: Sequence[str]) -> bool:
    """Имя в слове, склеенном из одиночных букв: каждая его часть встречается там подстрокой."""
    return bool(parts) and any(all(_stem(part) in run for part in parts) for run in glued)


def _mentions(parts: Name, words: Sequence[str]) -> bool:
    """Имя (одно или несколько слов подряд) встречается в словах текста."""
    if not parts:
        return False
    return any(
        all(_same_word(part, word) for part, word in zip(parts, words[start:], strict=False))
        for start in range(len(words) - len(parts) + 1)
    )


@dataclass(frozen=True)
class _Private:
    """Личные сведения одного адресата в запросе: фрагменты слов, имена NPC и мест из связей,
    имена собственные из его текстов (по одному слову, без повторов) и все его слова по порядку
    (`known` — что он знает сам)."""

    fragments: frozenset[Fragment]
    names: tuple[Name, ...]
    proper_names: tuple[str, ...]
    known: tuple[str, ...]


@dataclass(frozen=True)
class _Foreign:
    """Личные сведения другого адресата, которых герой идеи не знает сам и которых нет в общем
    тексте: только с ними и сверяется его идея."""

    fragments: frozenset[Fragment]
    names: tuple[Name, ...]
    proper_names: tuple[str, ...]


def _known(name_word: str, known: Sequence[str]) -> bool:
    """Слово имени есть среди отсортированных слов `known` (с окончанием, как `_same_word`).

    Слова с основой имени лежат в отсортированном списке подряд: двоичный поиск находит начало, и
    просматриваются только они, а не весь текст (находка S4 блока B11)."""
    stem = _stem(name_word)
    for word in islice(known, bisect_left(known, stem), None):
        if not word.startswith(stem):
            return False
        if _same_word(name_word, word):
            return True
    return False


@dataclass(frozen=True)
class IdeaGuard:
    """Фильтр ответа модели: идея героя не несёт личных сведений других адресатов.

    `foreign` — для каждого адресата то, чего он не знает из личных разделов других (`_Foreign`),
    `stranger` — то же для героя вне адресатов: всё личное, кроме общего. Что знают все (карточки
    отряда, приключение и место, недавние рассказы) и что знает сам адресат, от идеи не зависит:
    это вычтено один раз при сборке (`idea_guard`), а проверка идеи — только сверка с её словами."""

    foreign: Mapping[str, tuple[_Foreign, ...]]
    stranger: tuple[_Foreign, ...]

    def leaks(self, hero_id: str, text: str) -> bool:
        """В идее героя `hero_id` есть фрагмент личных сведений другого адресата: фрагмент слов,
        имя NPC или места из его связей или имя собственное из его текстов."""
        tokens = _tokens(text)
        words = [word.lower() for word, _glued in tokens]
        # Имя собственное узнаётся только с заглавной: «Серой гавани» — не «в серой мгле».
        capitalized = [word.lower() for word, _glued in tokens if word[0].isupper()]
        glued = [word.lower() for word, joined in tokens if joined]
        fragments = _fragments_of(words)
        for other in self.foreign.get(hero_id, self.stranger):
            if fragments & other.fragments:
                return True
            if any("".join(fragment) in run for fragment in other.fragments for run in glued):
                return True
            for name in other.names:
                if _mentions(name, words) or _in_glued(name, glued):
                    return True
            for single in other.proper_names:
                if any(_same_word(single, word) for word in capitalized):
                    return True
                if _in_glued((single,), glued):
                    return True
        return False

    def screen(self, ideas: Mapping[str, list[str]]) -> tuple[dict[str, list[str]], int]:
        """Идеи без чужих личных сведений и число отброшенных; герой без идей выпадает."""
        kept: dict[str, list[str]] = {}
        dropped = 0
        for hero_id, texts in ideas.items():
            clean = [text for text in texts if not self.leaks(hero_id, text)]
            dropped += len(texts) - len(clean)
            if clean:
                kept[hero_id] = clean
        return kept, dropped


def idea_guard(
    state: GameState,
    adventure: Adventure,
    srd: SrdContent,
    addressees: Sequence[str],
    stories: Sequence[str],
) -> IdeaGuard:
    """Фильтр для запроса `ideas_request` с теми же данными: личные разделы адресатов против общего.

    Здесь же один раз вычитается известное: общий текст — из сведений каждого адресата, сведения
    самого героя — для каждой пары «герой идеи — другой адресат»."""
    private = {hero_id: _private(state, adventure, hero_id) for hero_id in addressees}
    shared = [
        adventure.title,
        _place_name(state, adventure),
        *(text for hero in state.party.values() for text in _hero_card(hero, srd).values()),
        *stories,
    ]
    shared_words = [word for text in shared for word in _words(text)]
    shared_known = sorted(set(shared_words))
    shared_fragments = _fragments(shared)
    unshared = {
        hero_id: _Foreign(
            fragments=found.fragments - shared_fragments,
            names=tuple(name for name in found.names if not _mentions(name, shared_words)),
            proper_names=tuple(
                name for name in found.proper_names if not _known(name, shared_known)
            ),
        )
        for hero_id, found in private.items()
    }
    foreign = {
        hero_id: tuple(
            _unknown_to(own, other) for other_id, other in unshared.items() if other_id != hero_id
        )
        for hero_id, own in private.items()
    }
    return IdeaGuard(foreign, tuple(unshared.values()))


def _private(state: GameState, adventure: Adventure, hero_id: str) -> _Private:
    """Личный раздел адресата для сверки: ровно то, что ушло модели (`_addressee`)."""
    texts = _texts(_addressee(state, adventure, hero_id))
    return _Private(
        fragments=_fragments(texts),
        names=tuple(tuple(_words(name)) for name in _link_names(state, adventure, hero_id)),
        proper_names=tuple(dict.fromkeys(name for text in texts for name in _proper_names(text))),
        known=tuple(sorted({word for text in texts for word in _words(text)})),
    )


def _unknown_to(own: _Private, other: _Foreign) -> _Foreign:
    """Сведения другого адресата без того, что герой знает сам (его фрагменты, связи и слова)."""
    return _Foreign(
        fragments=other.fragments - own.fragments,
        names=tuple(name for name in other.names if name not in own.names),
        proper_names=tuple(name for name in other.proper_names if not _known(name, own.known)),
    )


def _texts(data: Mapping[str, Any]) -> list[str]:
    """Все тексты личного раздела героя: строки и строки списков, кроме id."""
    found: list[str] = []
    for key, value in data.items():
        if key == "id":
            continue
        found += (
            [value] if isinstance(value, str) else [item for item in value if isinstance(item, str)]
        )
    return found


# --- фоновая задача ---


@dataclass(frozen=True)
class _Seen:
    """Что задача прочла под блокировкой: запрос, адресаты с их игроками и провайдер игры."""

    request: JsonRequest
    users: dict[str, str]  # id героя-адресата -> id его игрока
    guard: IdeaGuard  # фильтр ответа по личным разделам этого запроса
    provider_name: str | None
    model: str | None
    pc_ids: tuple[str, ...]


class _Unavailable(Exception):
    """Модель не дала идей: тайм-аут или отказ провайдера (текст — без содержимого ответа)."""


async def offer_rest_ideas(game_id: str, turn_id: str, *, deps: Deps) -> None:
    """Идеи привала, начатого на открытом ходе `turn_id`: запрос, запись и кадры адресатам.

    Фоновая задача: исключения наружу не выходят (кроме отмены при остановке сервера)."""
    started = time.monotonic()
    try:
        seen = await _read(game_id, turn_id, deps)
        if seen is None:
            return
        provider = deps.providers.of(
            game_id,
            deps.settings,
            provider=seen.provider_name,
            model=seen.model,
            pc_ids=seen.pc_ids,
            turn_tokens=deps.turn_tokens,
        )
        ideas, dropped = seen.guard.screen(
            addressed_ideas(await _ask(provider, seen.request), seen.users)
        )
        if dropped:
            # Только число: тексты идей и личные сведения героев в журнал не идут.
            logger.warning(
                "Идеи для привала игры %s: отброшено %d с личными сведениями другого героя.",
                game_id,
                dropped,
            )
        written = bool(ideas) and await _write(game_id, turn_id, ideas, seen.users, deps)
    except asyncio.CancelledError:
        raise
    except _Unavailable as problem:
        logger.warning(
            "Идеи для привала игры %s не получены: %s. Привал идёт без них.", game_id, problem
        )
        return
    except Exception as exc:
        # Текст ошибки не пишем: в нём бывают куски ответа модели и данных игры.
        logger.warning(
            "Идеи для привала игры %s прерваны неожиданной ошибкой %s. Привал идёт без них.",
            game_id,
            type(exc).__name__,
        )
        return
    logger.info(
        "Идеи для привала игры %s: %s за %d мс.",
        game_id,
        f"адресатов {len(ideas)}" if written else "не записаны (пусто или привал кончился)",
        round((time.monotonic() - started) * 1000),
    )


def addressed_ideas(answer: IdeasOut, users: Mapping[str, str]) -> dict[str, list[str]]:
    """Идеи из ответа модели по героям-адресатам: чужие id отброшены, у героя — первая идея."""
    ideas: dict[str, list[str]] = {}
    for idea in answer.ideas:
        text = printable_line(idea.text, IDEA_MAX_CHARS)
        if idea.character_id in users and idea.character_id not in ideas and text:
            ideas[idea.character_id] = [text]
    return ideas


async def _read(game_id: str, turn_id: str, deps: Deps) -> _Seen | None:
    """Запрос по текущему привалу; `None` — привал уже не тот или адресатов нет."""
    async with deps.locks.lock(game_id), deps.sessionmaker() as session:
        game = await session.get(Game, game_id)
        if game is None or not _same_rest(game, turn_id):
            return None
        row = await session.get(GameStateSnapshot, (game_id, game.state_version))
        if row is None:
            return None
        state = row.to_state()
        adventure = adventure_from_data(game.adventure_data)
        stories = await _recent_stories(session, game_id)
        players = await session.scalars(
            select(GamePlayer.character_id)
            .where(GamePlayer.game_id == game_id)
            .order_by(GamePlayer.joined_order)
        )
        pc_ids = tuple(character_id for character_id in players if character_id)
        provider_name, model, profanity = game.provider, game.model, game.profanity
    users = {
        hero_id: user_id
        for hero_id in idea_addressees(state)
        if (user_id := audience_to_user(state.party, hero_id)) is not None
    }
    if not users:
        return None
    addressees = list(users)
    request = ideas_request(state, adventure, deps.srd, addressees, stories, profanity=profanity)
    guard = idea_guard(state, adventure, deps.srd, addressees, stories)
    return _Seen(request, users, guard, provider_name, model, pc_ids)


def _same_rest(game: Game, turn_id: str) -> bool:
    """Идёт тот же привал: открыт тот же ход «конец привала», фаза `camp` (пауза не в счёт)."""
    return (
        game.phase in (GamePhase.PLAYING, GamePhase.PAUSED)
        and game.turn_phase == TurnPhase.CAMP
        and game.current_turn_id == turn_id
    )


async def _recent_stories(session: AsyncSession, game_id: str) -> list[str]:
    """Последние общие рассказы мастера от старых к новым: только для всех, без снятых."""
    rows = await session.scalars(
        select(Message.text)
        .where(
            Message.game_id == game_id,
            Message.kind == MessageKind.NARRATION,
            Message.audience == AUDIENCE_ALL,
            Message.superseded.is_(False),
        )
        .order_by(Message.id.desc())
        .limit(RECENT_STORIES)
    )
    return [printable_line(text, STORY_MAX_CHARS) for text in reversed(list(rows))]


async def _ask(provider: LLMProvider, request: JsonRequest) -> IdeasOut:
    """Один вызов дешёвой модели со своим тайм-аутом."""
    deadline = asyncio.timeout(IDEAS_TIMEOUT_S)
    try:
        async with deadline:
            return await provider.complete_json(request, IdeasOut)
    except TimeoutError:
        # Свой тайм-аут отличаем от тайм-аута, который поднял сам провайдер.
        if deadline.expired():
            raise _Unavailable(f"тайм-аут: больше {IDEAS_TIMEOUT_S:g} с") from None
        raise _Unavailable("тайм-аут провайдера") from None
    except ProviderError as error:
        raise _Unavailable(f"отказ провайдера {type(error).__name__}") from None


async def _write(
    game_id: str,
    turn_id: str,
    ideas: dict[str, list[str]],
    users: Mapping[str, str],
    deps: Deps,
) -> bool:
    """Идеи в игру и кадры адресатам, если идёт тот же привал; `False` — привал кончился.

    Кадры уходят под той же блокировкой, что и запись: конец привала (коммит хода «конец
    привала», `rest_ended`) не встанет между ними."""
    async with deps.locks.lock(game_id), deps.sessionmaker() as session:
        game = await session.get(Game, game_id, populate_existing=True)
        if game is None or not _same_rest(game, turn_id):
            return False
        game.rest_ideas = ideas
        await session.commit()
        for hero_id, texts in ideas.items():
            for text in texts:
                await send_to_game(deps.broadcaster, game_id, rest_idea_event(text), users[hero_id])
    return True
