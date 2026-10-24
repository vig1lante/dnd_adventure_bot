"""Описательная часть персонажа от модели: имя, внешность, характер, предыстория (план B6.4).

Модель только пишет текст. Числа персонажа считает движок, поэтому ответ модели — строгая схема
`PersonaOut` без чисел и без лишних полей: всё, что не подходит схеме, `complete_json` отвергает, а
мы не используем. Текст игрока (замысел, имя, черновик предыстории) — недоверенный ввод: в запрос он
попадает только строкой JSON, а промпт роли просит модель считать его данными, а не указаниями.

Промпты ролей — файлы пакета `dnd_server.prompts`: `hero_flavor` (описание героя) и
`backstory_helper` («помоги написать»). Их текст читают игроки, поэтому к роли добавляется общий
блок стиля со строкой режима мата игры (спека §4).

Вызов модели из действия игрока ограничен собственным тайм-аутом: у `complete_json` общего тайм-аута
нет (до трёх попыток, а каждую SDK ещё повторяет при 429 и 5xx). Любой отказ — `FlavorUnavailable`
с русским текстом для игрока и причиной для журнала; создание персонажа от него не зависит.
"""

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field

from dnd_engine.character import Character
from dnd_engine.content import SrdContent
from dnd_server.llm.base import JsonRequest, LLMProvider, ProviderError, printable
from dnd_server.prompts import role_with_style

# Пределы длины текстов персонажа. Они же защищают API: игрок присылает те же поля сам (подробный
# режим), а ответ модели обязан укладываться в те же рамки. Хватает на абзац, но не на простыню,
# которая раздует запросы к модели и ленту мастера.
NAME_MAX_CHARS = 40
CONCEPT_MAX_CHARS = 200
APPEARANCE_MAX_CHARS = 400
SHORT_TEXT_MAX_CHARS = 300  # черты, идеалы, привязанности, слабости
BACKSTORY_MAX_CHARS = 1500
DRAFT_MAX_CHARS = 1500  # черновик предыстории для «помоги написать»

# Сколько ждём ответ модели в действии игрока, секунд. Дешёвая модель отвечает за секунды; запас
# покрывает один повтор после неподходящего JSON и один сбой, который SDK повторяет сам. Дольше
# игрок смотрит на индикатор впустую: лучше отказать и дать написать описание самому.
FLAVOR_TIMEOUT_S = 45.0

# Пределы сообщений в журнале: в тексте ошибки провайдера бывают куски ответа модели.
MAX_REASON_CHARS = 300

# Назначения запросов: по ним FakeProvider находит ответ, а журнал сервера называет вызов.
FLAVOR_PURPOSE = "character.flavor"
BACKSTORY_PURPOSE = "character.backstory"


class PersonaOut(BaseModel):
    """Описание героя от модели; имя нужно, только если игрок его не задал."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    appearance: str = Field(min_length=1, max_length=APPEARANCE_MAX_CHARS)
    traits: str = Field(min_length=1, max_length=SHORT_TEXT_MAX_CHARS)
    ideals: str = Field(min_length=1, max_length=SHORT_TEXT_MAX_CHARS)
    bonds: str = Field(min_length=1, max_length=SHORT_TEXT_MAX_CHARS)
    flaws: str = Field(min_length=1, max_length=SHORT_TEXT_MAX_CHARS)
    backstory: str = Field(min_length=1, max_length=BACKSTORY_MAX_CHARS)


class BackstoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    backstory: str = Field(min_length=1, max_length=BACKSTORY_MAX_CHARS)


class FlavorUnavailable(Exception):
    """Модель не дала текст. `message` — игроку по-русски, `reason` — в журнал сервера."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


class FlavorTimeout(FlavorUnavailable):
    """Модель не ответила за `FLAVOR_TIMEOUT_S`."""


class FlavorLimitReached(FlavorUnavailable):
    """Игрок исчерпал предел вызовов модели в лобби: модель не вызывалась."""


def quote(text: str) -> str:
    """Текст игрока как строка JSON: кавычки и переводы строк экранированы, не выйти из строки."""
    return json.dumps(text, ensure_ascii=False)


def flavor_request(
    character: Character, srd: SrdContent, *, name_given: bool, profanity: bool
) -> JsonRequest:
    """Запрос описания готового персонажа; класс, раса и предыстория названы словами правил.

    `profanity` — флажок мата игры: описание читают игроки."""
    concept = character.persona.concept
    name_line = (
        f"Имя героя задал игрок: {quote(character.name)}. В поле name верни его без изменений."
        if name_given
        else f"Имя не задано: придумай подходящее, не длиннее {NAME_MAX_CHARS} знаков."
    )
    concept_line = (
        f"Замысел игрока: {quote(concept)}."
        if concept
        else "Замысла нет: придумай героя, подходящего классу и расе."
    )
    prompt = "\n".join(
        [
            "Придумай описание героя.",
            f"Класс: {srd.classes[character.class_id].name}.",
            f"Раса: {srd.species[character.species].name}.",
            f"Предыстория по правилам: {srd.backgrounds[character.background].name}.",
            name_line,
            concept_line,
            "Поля ответа:",
            f"- name: имя героя, не длиннее {NAME_MAX_CHARS} знаков;",
            f"- appearance: внешность, не длиннее {APPEARANCE_MAX_CHARS} знаков;",
            f"- traits: черты характера, не длиннее {SHORT_TEXT_MAX_CHARS} знаков;",
            f"- ideals: идеалы, не длиннее {SHORT_TEXT_MAX_CHARS} знаков;",
            f"- bonds: привязанности, не длиннее {SHORT_TEXT_MAX_CHARS} знаков;",
            f"- flaws: слабости, не длиннее {SHORT_TEXT_MAX_CHARS} знаков;",
            f"- backstory: предыстория, 3–6 предложений, не длиннее {BACKSTORY_MAX_CHARS} знаков.",
        ]
    )
    return JsonRequest(
        system=role_with_style("hero_flavor", profanity),
        prompt=prompt,
        purpose=FLAVOR_PURPOSE,
        cheap=True,
    )


def backstory_request(
    class_name: str, species_name: str, concept: str, draft: str, *, profanity: bool
) -> JsonRequest:
    """Запрос «помоги написать»: предыстория по замыслу и черновику игрока."""
    draft_line = (
        f"Черновик игрока: {quote(draft)}. Сохрани его мысль и факты."
        if draft
        else "Черновика нет: придумай предысторию по замыслу."
    )
    prompt = "\n".join(
        [
            "Помоги написать предысторию героя.",
            f"Класс: {class_name}.",
            f"Раса: {species_name}.",
            f"Замысел игрока: {quote(concept)}." if concept else "Замысла нет.",
            draft_line,
            "Поле backstory: предыстория, 3–6 предложений, "
            f"не длиннее {BACKSTORY_MAX_CHARS} знаков.",
        ]
    )
    return JsonRequest(
        system=role_with_style("backstory_helper", profanity),
        prompt=prompt,
        purpose=BACKSTORY_PURPOSE,
        cheap=True,
    )


async def ask_model[T: BaseModel](
    provider: LLMProvider, request: JsonRequest, schema: type[T]
) -> T:
    """Один запрос к дешёвой модели со своим тайм-аутом; отказ — `FlavorUnavailable`."""
    deadline = asyncio.timeout(FLAVOR_TIMEOUT_S)
    try:
        async with deadline:
            return await provider.complete_json(request, schema)
    except TimeoutError:
        # Свой тайм-аут отличаем от тайм-аута, который поднял сам провайдер.
        if deadline.expired():
            raise FlavorTimeout(
                "Нейронка не ответила вовремя.", f"тайм-аут: больше {FLAVOR_TIMEOUT_S:g} с"
            ) from None
        raise FlavorUnavailable("Нейронка сейчас недоступна.", "тайм-аут провайдера") from None
    except ProviderError as error:
        # Текст ошибки провайдера может нести куски ответа модели: в журнал только печатный вид.
        raise FlavorUnavailable(
            "Нейронка сейчас недоступна.", printable(str(error), MAX_REASON_CHARS)
        ) from None


def with_persona(character: Character, persona: PersonaOut, *, keep_name: bool) -> Character:
    """Персонаж с описанием от модели: меняются имя (если не задано) и текст, числа — никогда."""
    texts = persona.model_dump(exclude={"name"})
    return character.model_copy(
        update={
            "name": character.name if keep_name else persona.name,
            # Замысел игрока остаётся: `concept` модель не пишет.
            "persona": character.persona.model_copy(update=texts),
        }
    )
