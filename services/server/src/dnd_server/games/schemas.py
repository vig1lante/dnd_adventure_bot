"""Тела запросов и ответы лобби (план B6.3).

Ответы описывают только то, что видит любой участник игры: состав лобби, готовность и настройки.
В них нет копии приключения, отчёта валидатора и чужих данных; кто в игре не состоит, игру не видит.
"""

from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from dnd_server.db.models import GamePhase, TurnPhase

# Пределы длительностей отсчётов в секундах, которые хост задаёт при создании игры. Нижняя граница —
# чтобы шестеро в одной комнате успели прочитать рассказ и набрать ход, а отсчёт не обнулил его по
# случайной опечатке; верхняя — чтобы забытый отсчёт не подвесил игру: хост в любой момент
# закрывает сбор вручную. Значения по умолчанию из env (120, 60, 300 и 120) лежат внутри. Целое
# строгое: «120» и 120.0 — не секунды, а ошибка клиента. `CampSeconds` — долгий привал (и привал,
# который начал мастер), `ShortRestSeconds` — короткий привал по кнопке хоста (блок B9).
CollectSeconds = Annotated[StrictInt, Field(ge=30, le=600)]
HurrySeconds = Annotated[StrictInt, Field(ge=15, le=300)]
CampSeconds = Annotated[StrictInt, Field(ge=30, le=900)]
ShortRestSeconds = Annotated[StrictInt, Field(ge=30, le=600)]


class CreateGameIn(BaseModel):
    adventure_id: str = Field(min_length=1, max_length=64)
    # Имена провайдера и модели ограничены по составу и длине: они уходят в базу, журналы и запросы
    # к провайдеру. Квадратные скобки схема пускает только ради псевдонимов CLI (`sonnet[1m]`):
    # подходит ли имя провайдеру, решают `games.service.check_model` и фабрика провайдеров. Первый
    # знак — буква или цифра, то же правило у адаптера CLI (`llm.claude_code_cli.is_model_name`).
    provider: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,99}$")
    collect_seconds: CollectSeconds | None = None
    hurry_seconds: HurrySeconds | None = None
    camp_seconds: CampSeconds | None = None
    short_rest_seconds: ShortRestSeconds | None = None
    # «Мат и грубая речь» (решение Р1): по умолчанию включён. Строгий: «false» и 0 — ошибка клиента.
    profanity: StrictBool = True


class JoinIn(BaseModel):
    code: Annotated[str, Field(pattern=r"^[A-Z0-9]{6}$")]

    @field_validator("code", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        # Код вводят руками или берут из ссылки: регистр и пробелы по краям роли не играют.
        return value.strip().upper() if isinstance(value, str) else value


class ReadyIn(BaseModel):
    ready: StrictBool


class PlayerOut(BaseModel):
    user_id: str
    name: str
    is_host: bool
    ready: bool
    has_character: bool


class GameOut(BaseModel):
    id: str
    code: str
    adventure_id: str
    adventure_title: str
    phase: GamePhase
    turn_phase: TurnPhase | None
    # Пусто — значения по умолчанию из env сервера.
    provider: str | None
    model: str | None
    collect_seconds: int
    hurry_seconds: int
    camp_seconds: int
    short_rest_seconds: int
    profanity: bool  # флажок мата игры: его видят мастер, служебные роли и лобби
    players: list[PlayerOut]
