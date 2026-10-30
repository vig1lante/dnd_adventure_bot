"""Провайдер модели для игры: одна функция сборки и кеш провайдеров игр.

Провайдера игры собирают три места: проверка при создании игры (`games.service.check_provider`),
описание персонажей (`characters.service`) и ход мастера (`dm.turn`). Все три идут через
`game_provider`, чтобы переопределение хоста (`provider`, `model`), id персонажей для сценария
`FakeProvider`, реестр токенов хода и адрес моста MCP задавались в одном месте. Сама сборка
(`llm.factory.make_provider`) в сеть не ходит.

Адрес моста — это адрес, по которому слушает сам сервер: `HOST` и порт процесса (`serving_port`:
`PORT`, а при `dnd-server --port` — выбранный). Мост MCP (процесс `claude_code_cli`) обращается по
нему к внутреннему API сервера и несёт в адресе токен хода, поэтому порт не должен расходиться
с тем, на котором сервер слушает на самом деле.

Ход мастера берёт провайдера из `GameProviders`: у `FakeProvider` очередь ходов сценария хранится
в самом провайдере, и «Повторить» или следующий раунд должны получить следующий ход сценария, а не
первый. Настоящие провайдеры состояния игры не хранят, для них кеш просто избавляет от пересборки
клиента на каждый ход. Кеш живёт в памяти процесса, как блокировки игр.
"""

from collections.abc import Sequence

from dnd_server.config import HOST, Settings, serving_port
from dnd_server.llm.base import LLMProvider
from dnd_server.llm.factory import make_provider
from dnd_server.llm.internal_api import TurnTokens


def bridge_url() -> str:
    """Адрес сервера для моста MCP: тот же, по которому слушает `dnd-server`."""
    return f"http://{HOST}:{serving_port()}"


def game_provider(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    pc_ids: Sequence[str] = (),
    turn_tokens: TurnTokens | None = None,
) -> LLMProvider:
    """Провайдер игры: `provider` и `model` — переопределение хоста, иначе значения из env.

    `pc_ids` — id персонажей по порядку входа (для `$pc1…$pc6` сценария `FakeProvider`);
    `turn_tokens` есть только у dev-сервера (для `claude_code_cli`). Ошибка настройки —
    `ConfigError` фабрики."""
    return make_provider(
        settings,
        provider=provider,
        model=model,
        pc_ids=pc_ids,
        turn_tokens=turn_tokens,
        internal_url=bridge_url(),
    )


class GameProviders:
    """Собранные провайдеры игр по id игры: игра ведётся одним провайдером до конца работы сервера.

    Ключи — id игр, к которым у хода мастера есть доступ (проверка игры идёт до сборки), поэтому
    запись на игру одна и мала."""

    def __init__(self) -> None:
        self._by_game: dict[str, LLMProvider] = {}

    def of(
        self,
        game_id: str,
        settings: Settings,
        *,
        provider: str | None,
        model: str | None,
        pc_ids: Sequence[str],
        turn_tokens: TurnTokens | None,
    ) -> LLMProvider:
        """Провайдер игры; при первом обращении собирается, ошибка настройки в кеш не попадает."""
        found = self._by_game.get(game_id)
        if found is None:
            found = game_provider(
                settings, provider=provider, model=model, pc_ids=pc_ids, turn_tokens=turn_tokens
            )
            self._by_game[game_id] = found
        return found
