"""Дневник отряда: что герои узнали за игру (спека DND-2 §3 Р11, §12; дополнение контракта B8,
поправки §10 и §11).

Дневник хранится в состоянии игры (`GameState.journal`) — кортеж записей в порядке появления.
Запись говорит об одной сущности приключения по ссылке `ref` и адресована всему отряду (`all`)
или одному герою (id персонажа, как аудитория событий движка). Ключ записи — пара
`(ref, audience)`: повтор по тому же ключу дополняет текст новым абзацем и поднимает `revision`,
дубля не бывает; общая и личная запись об одной сущности — две разные записи.

Текст записи — только то, что могут знать игроки: `public` сущности, а если он пуст — её
название; у улики — описание найденного, у слуха — `public` или сам слух. Текст из одних пробелов
считается пустым (дополнение B11 §5 п. 1). Тайны (`secret`,
мотивы, заметки мастера, тайны мест, правда о слухах, утверждения открытий, полное описание
предмета) сюда не попадают никогда. Дополнение мастера (`note`) — его слова о том, что узнали
герои; длину и печатаемость проверяет инструмент `journal_add`. Номер раунда записи — раунд
игроков (`GameState.party_round`), а не ход мастера: ходом мастера считается и каждый ход бойца.

Запись бывает строгой и мягкой. Строгая (`write`, инструмент `journal_add`) проверяет всё и
отказывает `JournalError` до изменения. Мягкая (`record`, `visit`, `open_journal`) — побочный
эффект перехода, находки и передачи предмета: она идёт после изменения рабочей копии и не бросает
ошибок из-за данных приключения — о сущности без текста записи просто нет.

`open_journal` возвращает новое состояние, а `write`, `record` и `visit` меняют дневник
переданного состояния на месте: это рабочая копия хода мастера, как у инструментов мира. Записи
неизменяемы, поэтому сервер может сравнить дневник до и после хода (`changed_entries`).

Модуль не импортирует состояние и приключение во время выполнения: `state` сам импортирует
отсюда модель записи, а схема приключения — `state`.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from dnd_engine.adventure.schema import Adventure, Deity, Faction, Item, Location, Npc, Region
    from dnd_engine.state import GameState

Section = Literal[
    "main", "places", "people", "gods", "factions", "world", "rumors", "clues", "items"
]

ALL = "all"  # весь отряд: то же значение, что у аудитории событий движка
INTRO_REF = "intro"  # служебная запись «Главное»: завязка и цель отряда
WORLD_REF = "world"  # история мира: без id и только со словами мастера
MAX_NOTE_CHARS = 500
MAX_TEXT_CHARS = 2000  # предел текста для дополнений мастера
MAX_TITLE_CHARS = 60  # название записи о сущности без имени: улика, слух
PARAGRAPH = "\n\n"
_TITLE_TRIM = " ,;:—–-"

# Вид ссылки → раздел дневника (дополнение §1).
SECTIONS: Mapping[str, Section] = MappingProxyType(
    {
        "intro": "main",
        "location": "places",
        "npc": "people",
        "deity": "gods",
        "faction": "factions",
        "region": "world",
        "world": "world",
        "rumor": "rumors",
        "clue": "clues",
        "item": "items",
    }
)
# Что известно со старта (`known_at_start`), в порядке разделов: улик и истории мира среди них нет.
_START_KINDS = ("location", "npc", "deity", "faction", "region", "rumor", "item")
_REF_FORMS = (
    "location:<id>, npc:<id>, deity:<id>, faction:<id>, region:<id>, rumor:<номер с 1>, "
    "clue:<id>, item:<id> или world"
)


class JournalError(ValueError):
    """Запись нельзя сделать: неизвестная ссылка или адресат, нет текста, текст сверх предела.

    Текст — для модели мастера: инструмент превращает ошибку в `ToolError`."""


class JournalEntry(BaseModel):
    """Запись дневника (дополнение §2). `visited_turn` — только у мест: раунд первого прихода
    отряда. `updated_turn` — раунд последнего дополнения текста словами мастера (дополнение B11
    §1 п. 4): первый приход в место текст не дополняет и его не ставит."""

    model_config = ConfigDict(frozen=True)

    id: str  # f"{ref}@{audience}"
    ref: str
    section: Section
    title: str
    text: str
    audience: str
    turn: int  # раунд, когда запись появилась; 0 — старт игры
    revision: int = 1  # +1 при каждом дополнении и при первом приходе в место
    visited_turn: int | None = None
    updated_turn: int | None = None  # None — текст не дополняли; в старых сохранениях поля нет


class _Page(NamedTuple):
    """Что дневник может сказать о сущности: название, публичный текст, известна ли со старта."""

    title: str
    text: str
    known_at_start: bool = False


def entry_id(ref: str, audience: str) -> str:
    return f"{ref}@{audience}"


def _short(text: str) -> str:
    """Начало текста не длиннее `MAX_TITLE_CHARS` по границе слова: название записи о сущности,
    у которой нет имени."""
    words = " ".join(text.split())
    if len(words) <= MAX_TITLE_CHARS:
        return words
    head = words[: MAX_TITLE_CHARS - 1]  # последний знак — под «…»
    if words[len(head)] != " " and " " in head:  # срез пришёлся посреди слова
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(_TITLE_TRIM) + "…"


def _first_text(*candidates: str) -> str:
    """Первый кандидат, в котором есть не только пробелы, иначе пусто.

    Текст из одних пробелов и переносов игроку ничего не скажет: для дневника он пустой, как и для
    валидатора (`clue_without_description`, `item_without_text`). Так сущность без текста не
    получает записи, а пробелы в `public` не закрывают название."""
    return next((text for text in candidates if text.strip()), "")


def _unnamed(text: str, known_at_start: bool = False) -> _Page:
    """Страница сущности без имени (слух, улика): название — начало текста."""
    return _Page(_short(text), text, known_at_start)


def _named(
    kind: str, entities: Iterable[Location | Npc | Deity | Faction | Item | Region]
) -> dict[str, _Page]:
    return {
        f"{kind}:{entity.id}": _Page(
            _first_text(entity.name),
            _first_text(entity.public, entity.name),
            entity.known_at_start,
        )
        for entity in entities
    }


def _pages(adventure: Adventure, kind: str) -> dict[str, _Page]:
    """Ссылки вида `kind`, которые есть в приключении: ссылка → страница сущности."""
    match kind:
        case "intro":
            # Пустое `journal_intro` схема заменяет на `pitch`; из одних пробелов — тоже пустое.
            intro = _first_text(adventure.journal_intro, adventure.pitch)
            return {INTRO_REF: _Page("Главное", intro, True)} if intro else {}
        case "world":
            return {WORLD_REF: _Page("История мира", "")}  # история мира — данные мастера
        case "rumor":
            # Слухи без id адресуются номером в `world.rumors` (дополнение §1); `true` не читается.
            return {
                f"rumor:{number}": _unnamed(
                    _first_text(rumor.public, rumor.text), rumor.known_at_start
                )
                for number, rumor in enumerate(adventure.world.rumors, start=1)
            }
        case "clue":
            # Улика — то, что нашли герои; утверждение открытия не показывается.
            return {
                f"clue:{clue.id}": _unnamed(_first_text(clue.description))
                for revelation in adventure.revelations.values()
                for clue in revelation.clues
            }
        case "region":
            return _named(kind, adventure.world.regions)
        case "location":
            return _named(kind, adventure.locations.values())
        case "npc":
            return _named(kind, adventure.npcs.values())
        case "deity":
            return _named(kind, adventure.deities.values())
        case "faction":
            return _named(kind, adventure.factions.values())
        case "item":
            return _named(kind, adventure.items.values())
    return {}


def _page(adventure: Adventure, ref: str) -> _Page:
    kind = ref.partition(":")[0]
    if kind not in SECTIONS:
        raise JournalError(f"Ссылка {ref!r} не подходит для дневника: нужна {_REF_FORMS}.")
    pages = _pages(adventure, kind)
    page = pages.get(ref)
    if page is None:
        raise JournalError(f"Нет ссылки {ref!r}. Есть: {', '.join(pages) or 'нет'}")
    return page


def _new_entry(ref: str, audience: str, page: _Page, text: str, turn: int) -> JournalEntry:
    return JournalEntry(
        id=entry_id(ref, audience),
        ref=ref,
        section=SECTIONS[ref.partition(":")[0]],
        title=page.title or _short(text),  # без своего названия — начало текста записи
        text=text,
        audience=audience,
        turn=turn,
    )


def _find(state: GameState, key: str) -> JournalEntry | None:
    return next((entry for entry in state.journal if entry.id == key), None)


def _put(state: GameState, entry: JournalEntry) -> None:
    """Новая запись встаёт в конец дневника, изменённая — на своё место."""
    if _find(state, entry.id) is None:
        state.journal = (*state.journal, entry)
    else:
        state.journal = tuple(entry if old.id == entry.id else old for old in state.journal)


def _check_length(text: str) -> None:
    if len(text) > MAX_TEXT_CHARS:
        raise JournalError(
            f"Запись дневника не длиннее {MAX_TEXT_CHARS} знаков, а с этим дополнением вышло бы "
            f"{len(text)}: скажите короче."
        )


def open_journal(state: GameState, adventure: Adventure) -> GameState:
    """Стартовый дневник для всего отряда, раунд 0: «Главное» (если `journal_intro` не пуст), всё,
    что отряд знает с начала игры (`known_at_start`), и стартовая локация — отряд уже в ней, поэтому
    она записана, даже если не известна заранее, и отмечена `visited_turn = 0`.

    Идемпотентна: на непустом дневнике возвращает то же состояние без изменений. Старая игра без
    дневника получает его при первом вызове, посреди игры — раунд записей всё равно 0."""
    if state.journal:
        return state
    start = f"location:{adventure.start_location}"
    entries = [
        _new_entry(ref, ALL, page, page.text, turn=0)
        for kind in (INTRO_REF, *_START_KINDS)
        for ref, page in _pages(adventure, kind).items()
        if page.text and (page.known_at_start or ref == start)  # без текста — записи нет
    ]
    opened = state.model_copy(deep=True)
    opened.journal = tuple(
        entry.model_copy(update={"visited_turn": 0}) if entry.ref == start else entry
        for entry in entries
    )
    return opened


def write(
    state: GameState,
    adventure: Adventure,
    ref: str,
    *,
    audience: str = ALL,
    note: str | None = None,
) -> tuple[JournalEntry, bool]:
    """Строгая запись мастера (`journal_add`) о сущности `ref` для `audience`: (запись, создана
    ли она).

    Новая запись — публичный текст сущности и `note` новым абзацем; существующая с `note` —
    дописывается, поднимает `revision` и запоминает раунд дополнения (`updated_turn`); без `note`
    повтор ничего не меняет. Всё проверяется до изменения: неизвестная ссылка или адресат, запись
    без текста (история мира или сущность без текста — без `note`) и текст с дополнением сверх
    `MAX_TEXT_CHARS` — `JournalError`, дневник прежний. Раунд новой записи и дополнения —
    `state.party_round`."""
    page = _page(adventure, ref)
    if audience != ALL and audience not in state.party:
        raise JournalError(f"Нет героя отряда {audience!r}. Есть: {', '.join(state.party)}")
    current = _find(state, entry_id(ref, audience))
    if current is None:
        text = PARAGRAPH.join(part for part in (page.text, note) if part)
        if not text:
            raise JournalError(f"О {ref!r} в дневник пишут только слова мастера: нужна note.")
        if note:
            _check_length(text)
        entry = _new_entry(ref, audience, page, text, state.party_round)
        _put(state, entry)
        return entry, True
    if not note:
        return current, False
    text = current.text + PARAGRAPH + note
    _check_length(text)
    entry = current.model_copy(
        update={"text": text, "revision": current.revision + 1, "updated_turn": state.party_round}
    )
    _put(state, entry)
    return entry, False


def record(
    state: GameState, adventure: Adventure, ref: str, *, audience: str = ALL
) -> JournalEntry | None:
    """Мягкая авто-запись о сущности `ref` для `audience` (найденная улика, полученный предмет):
    новая запись с публичным текстом или `None`, если записывать нечего.

    Зовётся после изменения рабочей копии, поэтому из-за данных приключения не отказывает: у
    сущности без текста записи нет, уже записанная не меняется. Неизвестная ссылка или адресат вне
    отряда — ошибка движка (их проверяют инструмент и валидатор), она по-прежнему бросается."""
    page = _page(adventure, ref)
    if audience != ALL and audience not in state.party:
        raise JournalError(f"Нет героя отряда {audience!r}. Есть: {', '.join(state.party)}")
    if not page.text or _find(state, entry_id(ref, audience)) is not None:
        return None
    entry = _new_entry(ref, audience, page, page.text, state.party_round)
    _put(state, entry)
    return entry


def visit(state: GameState, adventure: Adventure, location_id: str) -> JournalEntry | None:
    """Отряд пришёл в место: запись о нём для всего отряда с раундом первого прихода (мягкая).

    Места ещё нет в дневнике — оно записывается; оно известно, но отряд там не был — ставится
    `visited_turn` и растёт `revision` (место снова «новое»); повторный приход и место без текста
    ничего не меняют и возвращают `None`. Личные записи о месте посещением отряда не считаются."""
    ref = f"location:{location_id}"
    current = _find(state, entry_id(ref, ALL))
    if current is None:
        page = _page(adventure, ref)
        if not page.text:
            return None
        entry = _new_entry(ref, ALL, page, page.text, state.party_round)
    elif current.visited_turn is None:
        entry = current.model_copy(update={"revision": current.revision + 1})
    else:
        return None
    entry = entry.model_copy(update={"visited_turn": state.party_round})
    _put(state, entry)
    return entry


def visible_entries(
    journal: Iterable[JournalEntry], character_id: str | None
) -> tuple[JournalEntry, ...]:
    """Записи, которые видит игрок героя `character_id`: общие и личные его героя. Игрок без
    героя видит только общие; чужие личные не видит никто."""
    return tuple(entry for entry in journal if entry.audience in (ALL, character_id))


def changed_entries(
    before: Iterable[JournalEntry], after: Iterable[JournalEntry]
) -> tuple[JournalEntry, ...]:
    """Записи `after`, которых не было в `before` или чья `revision` выросла, в порядке
    дневника: их сервер рассылает адресатам после хода."""
    revisions = {entry.id: entry.revision for entry in before}
    return tuple(entry for entry in after if revisions.get(entry.id, 0) < entry.revision)
