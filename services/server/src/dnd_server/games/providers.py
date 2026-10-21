"""Список провайдеров и моделей для создателей игр: `GET /api/providers` (план B8.s0, решение R4).

Форма «Создать игру» в Mini App даёт создателю выбрать нейронку для игры. Сервер отдаёт варианты:
какие провайдеры есть, как они называются, какая модель у каждого по умолчанию и настроен ли он.
Маршрут тот же по доступу, что и создание игры (`require_game_creator`): остальным — 403.

Секретов в ответе нет и быть не может: поля ответа перечислены в `ProviderOut`, значения настроек
(ключи, адрес сервера, сценарий) в них не попадают, а причина недоступности не пересказывается:
тексты ошибок настройки называют переменные окружения и пути. Провайдеры `claude_code_cli` и
`fake` показываются только в dev (`APP_ENV=dev`), в prod их нет в списке вовсе.

«Доступен» (`available`) значит ровно «игру с таким провайдером создать получится»: ответ берётся
из той же проверки, что и при `POST /api/games` (`games.service.check_provider` -> фабрика
провайдеров), поэтому список не может разойтись с созданием. Проверка собирает адаптер, но в сеть не
ходит и процессов не запускает.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dnd_server.auth.deps import app_settings, require_game_creator
from dnd_server.config import Settings
from dnd_server.db.models import User
from dnd_server.games.api import get_turn_tokens
from dnd_server.games.service import Invalid, check_provider
from dnd_server.llm.internal_api import TurnTokens

router = APIRouter(prefix="/api")


@dataclass(frozen=True)
class ProviderInfo:
    """Что о провайдере известно кодом, а не настройками."""

    id: str  # то же имя, что в `provider` запроса `POST /api/games` и в `llm.factory.PROVIDERS`
    title: str  # название для игрока по-русски
    # Использует ли провайдер имя модели из игры: `claude_code_cli` и `fake` его игнорируют.
    accepts_model: bool
    dev_only: bool
    default_model: Callable[[Settings], str | None]


# Порядок списка — порядок показа: сначала настоящие провайдеры, затем те, что нужны только в dev.
CATALOG = (
    ProviderInfo(
        id="anthropic_api",
        title="Claude через API Anthropic",
        accepts_model=True,
        dev_only=False,
        default_model=lambda settings: settings.llm_model,
    ),
    ProviderInfo(
        id="openai_compatible",
        title="Сервер, совместимый с OpenAI",
        accepts_model=True,
        dev_only=False,
        # LLM_MODEL здесь не подходит: это модель Anthropic, у чужого сервера её нет.
        default_model=lambda settings: settings.openai_compat_model,
    ),
    ProviderInfo(
        id="claude_code_cli",
        title="Claude Code на этом компьютере (только dev)",
        accepts_model=False,
        dev_only=True,
        default_model=lambda _settings: None,  # версию модели задаёт подписка владельца
    ),
    ProviderInfo(
        id="fake",
        title="Заглушка по сценарию (только dev)",
        accepts_model=False,
        dev_only=True,
        default_model=lambda _settings: None,
    ),
)


class ProviderOut(BaseModel):
    id: str
    title: str
    # Игру с этим провайдером можно создать прямо сейчас (ключи и адреса настроены). Почему нет —
    # не сказано и в журнал не пишется: причину (какую переменную окружения задать) называет ответ
    # на `POST /api/games` с этим провайдером, настройки описаны в README.
    available: bool
    # Модель, которую провайдер возьмёт, если игра её не переопределит; пусто, если модели нет.
    default_model: str | None
    # Модели на выбор, первая — по умолчанию. Сервер принимает и любое другое допустимое имя
    # (`model` в `POST /api/games`), если `accepts_model`; чтобы взять модель по умолчанию, поле
    # `model` можно не присылать.
    models: list[str]
    accepts_model: bool
    # Провайдер, которым игра ведётся без переопределения (`LLM_PROVIDER`). В prod заводской
    # `fake` в списке не показывается, и тогда не помечен никто.
    is_default: bool


class ProvidersOut(BaseModel):
    providers: list[ProviderOut]


def _is_available(settings: Settings, provider_id: str, turn_tokens: TurnTokens | None) -> bool:
    try:
        check_provider(settings, provider_id, None, turn_tokens)
    except Invalid:
        return False
    return True


def provider_out(
    info: ProviderInfo, settings: Settings, turn_tokens: TurnTokens | None
) -> ProviderOut:
    default_model = info.default_model(settings)
    return ProviderOut(
        id=info.id,
        title=info.title,
        available=_is_available(settings, info.id, turn_tokens),
        default_model=default_model,
        models=[default_model] if default_model else [],
        accepts_model=info.accepts_model,
        is_default=info.id == settings.llm_provider,
    )


@router.get("/providers")
async def list_providers(
    _creator: Annotated[User, Depends(require_game_creator)],
    settings: Annotated[Settings, Depends(app_settings)],
    turn_tokens: Annotated[TurnTokens | None, Depends(get_turn_tokens)],
) -> ProvidersOut:
    shown = [info for info in CATALOG if not info.dev_only or settings.app_env == "dev"]
    return ProvidersOut(providers=[provider_out(info, settings, turn_tokens) for info in shown])
