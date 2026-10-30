"""Черновик приключения — общий контракт генератора и конструктора.

`AuthoringDraft` — частичный формат v2: все разделы необязательны, сущности — модели движка
(`Location`, `Npc`, `Deity`…), сверху настройки заказа (`settings`), закрепления (`locks`) и
пожелания автора по шагам (`wishes`). На нём стоят шаги генератора (`steps.run_step`), полный
проход (`pipeline.generate`), CLI и конструктор на сайте, поэтому форма черновика не меняется.

Закрепление — ссылка `<вид>:<id>` на сущность раздела или `step:<шаг>` на шаг целиком (мир, сюжет,
замысел — разделы без отдельных сущностей). Закреплённое сервер не меняет: шаг отбрасывает
изменения закреплённых id, а не только просит модель их не трогать.

`to_adventure` собирает из черновика `Adventure` (через `assemble`) для валидатора и записи папки,
`from_adventure` даёт черновик из готового приключения. Заготовки автора (`Seeds`, файл `--seeds`
у CLI) входят в черновик закреплёнными: `seeded`.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from dnd_engine.adventure.schema import (
    COLLECTIONS,
    ID_PATTERN,
    Act,
    Adventure,
    Deity,
    Encounter,
    EntityId,
    Faction,
    Item,
    Location,
    Npc,
    Ref,
    Relation,
    Revelation,
    WorldInfo,
    upgrade_v1,
)
from dnd_server.authoring.assemble import assemble_adventure
from dnd_server.authoring.schemas import CustomMonster

# Шаги генератора по порядку. Они же — ключи пожеланий и имена закреплений `step:<шаг>`.
Step = Literal[
    "concept",
    "world",
    "pantheon",
    "factions",
    "plot",
    "locations",
    "location_contents",
    "npcs",
    "relations",
    "encounters",
    "items",
    "clues",
]
STEPS: tuple[str, ...] = get_args(Step)

# Разделы-списки черновика и вид ссылки их сущностей. Порядок — порядок коллекций приключения:
# в нём закреплённые сущности попадают в `Adventure.seeds`.
SECTION_KINDS = {
    "locations": "location",
    "npcs": "npc",
    "items": "item",
    "encounters": "encounter",
    "revelations": "revelation",
    "monsters": "monster",
    "deities": "deity",
    "factions": "faction",
    "relations": "relation",
}
# Виды, чьи сущности лежат в коллекциях приключения и поэтому входят в `seeds` (связь — нет).
_SEED_KINDS = {SECTION_KINDS[name] for name in COLLECTIONS}

_LOCK_PATTERN = (
    rf"^(?:(?:{'|'.join(SECTION_KINDS.values())}):{ID_PATTERN[1:-1]}|step:(?:{'|'.join(STEPS)}))$"
)
LockRef = Annotated[str, StringConstraints(pattern=_LOCK_PATTERN)]
# Пожелание автора к шагу: свободный текст, в промпт уходит только блоком данных.
Wish = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
# Пожелания по шагам: `{шаг: текст}` (поле черновика и файл `--wishes` у CLI).
Wishes = dict[Step, Wish]

DEFAULT_TARGET_HOURS = 3.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DraftSettings(_Model):
    """Заказ автора: завязка, тон, длина игры, отряд, модули правил и флажок мата (включён)."""

    pitch: str = ""
    tone: str = ""
    target_hours: float = Field(default=DEFAULT_TARGET_HOURS, gt=0, allow_inf_nan=False)
    party_size: int = Field(default=6, ge=1)
    party_level: int = Field(default=3, ge=1)
    modules: tuple[str, ...] = ()
    profanity: bool = True


class Plot(_Model):
    """Сюжет: акты с ключевыми сценами, концовки и злодей (NPC или бог)."""

    acts: tuple[Act, ...] = ()
    endings: tuple[str, ...] = ()
    villain: Ref | None = None


def _repeated_ids(sections: dict[str, tuple[BaseModel, ...]]) -> list[str]:
    """Повторы id внутри каждого раздела: `npcs: otto`."""
    return [
        f"{name}: {ident}"
        for name, entities in sections.items()
        for ident, count in Counter(entity.id for entity in entities).items()
        if count > 1
    ]


class _Sections(_Model):
    """Разделы-списки, общие для черновика и заготовок: id внутри раздела не повторяются.

    Связи формата v1 внутри NPC (`npcs[].relations: [{npc_id, kind, note}]` — так их пишут
    заготовки первой версии игры, README «Совместимость с v1») переходят в граф `relations` по
    правилам движка (`upgrade_v1`: вид по таблице v1 → v2, id `<npc_id>-<вид>-<владелец>` со
    свободным номером рядом с уже заданным графом, связь не взаимная), а поле NPC пустеет. Так
    черновик из любого источника (заготовки CLI, данные конструктора) собирается в `Adventure` v2,
    где связей внутри NPC не бывает, и ошибка их формы видна сразу, до вызовов модели."""

    deities: tuple[Deity, ...] = ()
    factions: tuple[Faction, ...] = ()
    locations: tuple[Location, ...] = ()
    npcs: tuple[Npc, ...] = ()
    relations: tuple[Relation, ...] = ()
    encounters: tuple[Encounter, ...] = ()
    monsters: tuple[CustomMonster, ...] = ()
    items: tuple[Item, ...] = ()
    revelations: tuple[Revelation, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _npc_relations_into_the_graph(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or not isinstance(data.get("npcs"), list | tuple):
            return data  # не по форме — ошибку с путём покажет проверка поля
        # Ключи — номера NPC: id в сырых данных может повторяться или отсутствовать.
        upgraded = upgrade_v1(
            {"npcs": dict(enumerate(data["npcs"])), "relations": data.get("relations", ())}
        )
        return {
            **data,
            "npcs": list(upgraded["npcs"].values()),
            "relations": upgraded["relations"],
        }

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        # Иначе сборка приключения молча оставила бы одну из двух сущностей с общим id.
        repeated = _repeated_ids({name: getattr(self, name) for name in SECTION_KINDS})
        if repeated:
            raise ValueError(f"id повторяются: {', '.join(repeated)}")
        return self


class AuthoringDraft(_Sections):
    id: EntityId | None = None  # id приключения; его придумывает шаг concept
    title: str = ""
    journal_intro: str = ""
    settings: DraftSettings = DraftSettings()
    world: WorldInfo | None = None
    plot: Plot = Plot()
    start_location: str | None = None
    finale_location: str | None = None
    locks: frozenset[LockRef] = frozenset()
    wishes: Wishes = {}

    def is_locked(self, ref: str) -> bool:
        return ref in self.locks

    def seed_ids(self) -> tuple[str, ...]:
        """id закреплённых сущностей коллекций приключения в порядке разделов (для `seeds`)."""
        return tuple(
            dict.fromkeys(
                entity.id
                for name, kind in SECTION_KINDS.items()
                if kind in _SEED_KINDS
                for entity in getattr(self, name)
                if self.is_locked(f"{kind}:{entity.id}")
            )
        )


class Seeds(_Sections):
    """Заготовки автора (файл `--seeds`): любые разделы v2, все они входят в черновик закреплёнными.

    Сущность разделов-списков закрепляется своей ссылкой, мир и сюжет — шагом целиком."""

    world: WorldInfo | None = None
    plot: Plot | None = None


def seeded(draft: AuthoringDraft, seeds: Seeds) -> AuthoringDraft:
    """Черновик с заготовками: их разделы заменяют разделы черновика и закрепляются."""
    update: dict[str, object] = {}
    locks = set(draft.locks)
    for name, kind in SECTION_KINDS.items():
        entities = getattr(seeds, name)
        if entities:
            update[name] = entities
            locks |= {f"{kind}:{entity.id}" for entity in entities}
    for name, step in (("world", "world"), ("plot", "plot")):
        section = getattr(seeds, name)
        if section is not None:
            update[name] = section
            locks.add(f"step:{step}")
    if not update:
        return draft
    # Разделы заготовок уже проверены `Seeds`, ссылки закреплений собраны из их id.
    return draft.model_copy(update=update | {"locks": frozenset(locks)})


def to_adventure(draft: AuthoringDraft) -> Adventure:
    """`Adventure` из черновика: недостающее — пустым, чтобы валидатор назвал, чего не хватает."""
    return assemble_adventure(draft)


def from_adventure(adventure: Adventure) -> AuthoringDraft:
    """Черновик из готового приключения: id сидов становятся закреплениями (как у заготовок)."""
    sections = {name: tuple(getattr(adventure, name).values()) for name in COLLECTIONS}
    seeds = set(adventure.seeds)
    locks = {
        f"{SECTION_KINDS[name]}:{ident}"
        for name in COLLECTIONS
        for ident in getattr(adventure, name)
        if ident in seeds
    }
    return AuthoringDraft(
        id=adventure.id,
        title=adventure.title,
        journal_intro=adventure.journal_intro,
        settings=DraftSettings(
            pitch=adventure.pitch,
            tone=adventure.tone,
            target_hours=adventure.target_hours,
            party_size=adventure.party_size,
            party_level=adventure.party_level,
            modules=tuple(adventure.rules.modules),
        ),
        world=adventure.world,
        plot=Plot(acts=adventure.acts, endings=adventure.endings, villain=adventure.villain),
        start_location=adventure.start_location,
        finale_location=adventure.finale_location,
        relations=adventure.relations,
        locks=frozenset(locks),
        **sections,
    )
