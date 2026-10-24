"""Фабрика провайдеров: настройки (и переопределение игры) -> `LLMProvider` (спека §7, B5.5).

Фабрика только собирает адаптер из частей, которые уже есть: настройки из env, клиент SDK и сам
адаптер. Сетевых вызовов и запуска процессов при сборке нет: SDK-клиенты соединяются лишь при
первом запросе, а `claude` запускается только в `run_turn`.

Любая неверная конфигурация — `ConfigError` с именем переменной окружения, но без значений:
в тексте нет ни ключей, ни адресов (в адресе может быть пароль). `ConfigError` — ошибка
настройки, а не отказ провайдера: сервер с ней не стартует, а игра с таким переопределением не
создаётся. `ProviderError` из `base.py` остаётся ошибкой самого хода.

Провайдер `claude_code_cli` собирается только при `APP_ENV=dev` (спека §2) — и для настроек по
умолчанию, и для переопределения при создании игры. Модель подписки ему задают модель игры (она
сильнее окружения, если CLI её примет) или CLAUDE_CLI_MODEL, CLAUDE_CLI_EFFORT и
CLAUDE_CLI_CHEAP_MODEL (спека DND-2 §11); незаданное выбирает сам CLI. LLM_MODEL, LLM_EFFORT и
LLM_CHEAP_MODEL — модели Anthropic API — ему не передаются.

Клиенты SDK адрес, учётные данные и заголовки берут из настроек, а не из окружения процесса: SDK
сами читают переменные владельца (`OPENAI_ORG_ID`, `ANTHROPIC_BASE_URL`, `*_CUSTOM_HEADERS`…), и
их значения ушли бы серверу, для которого не предназначались. Что можно, задано явно; заголовки из
`*_CUSTOM_HEADERS` отменить нельзя, поэтому сборка с такой переменной — `ConfigError`.
"""

import logging
import os
from collections.abc import Sequence
from urllib.parse import urlsplit

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI, Omit
from pydantic import SecretStr, ValidationError

from dnd_server.config import Settings
from dnd_server.llm.anthropic_api import AnthropicProvider
from dnd_server.llm.base import LLMProvider, printable, schema_problems
from dnd_server.llm.claude_code_cli import ClaudeCodeCliProvider, is_model_name
from dnd_server.llm.fake import FakeProvider, UnreadableYaml, describe_os_error, load_scenario
from dnd_server.llm.internal_api import TurnTokens
from dnd_server.llm.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

PROVIDERS = ("fake", "anthropic_api", "openai_compatible", "claude_code_cli")
MAX_LOGGED_MODEL_CHARS = 100  # имя модели игры в журнале сервера: из базы, длина ограничена

# Пределы клиентов SDK заданы явно, а не берутся из умолчаний библиотек (у них 600 с). Тайм-аут
# запроса не больше тайм-аута хода (`TurnRequest.timeout_s`, 180 с); он же ограничивает каждую
# попытку `complete_json`, у которой своего тайм-аута нет. Повторы 429/5xx делает SDK (спека §12).
REQUEST_TIMEOUT_S = 180.0
MAX_RETRIES = 2

# Локальные серверы (Ollama, LM Studio) ключа не требуют, но SDK без ключа падает или берёт
# OPENAI_API_KEY из окружения и отправил бы личный ключ владельца чужому серверу.
KEYLESS_API_KEY = "not-needed"

# Адрес Anthropic задан явно: иначе SDK взял бы его из ANTHROPIC_BASE_URL (её ставят для шлюзов
# Claude Code) и отправил ключ из настроек туда. Другой сервер — это `openai_compatible`.
ANTHROPIC_API_URL = "https://api.anthropic.com"


class ConfigError(Exception):
    """Конфигурация провайдера неверна или неполна; в тексте нет значений секретов."""


def make_provider(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    pc_ids: Sequence[str] = (),
    turn_tokens: TurnTokens | None = None,
    internal_url: str | None = None,
) -> LLMProvider:
    """Собирает провайдера: `provider` и `model` — переопределение игры, иначе значения из env.

    `pc_ids` — id персонажей игры по порядку входа (только для `fake`: `$pc1…$pc6` в сценарии).
    `turn_tokens` и `internal_url` — реестр токенов хода и адрес сервера для моста MCP (только
    для `claude_code_cli`): они есть лишь у запущенного dev-сервера, поэтому приходят явно."""
    name = provider or settings.llm_provider
    match name:
        case "fake":
            return _fake(settings, pc_ids)
        case "anthropic_api":
            return _anthropic_api(settings, model)
        case "openai_compatible":
            return _openai_compatible(settings, model)
        case "claude_code_cli":
            return _claude_code_cli(settings, model, turn_tokens, internal_url)
        case _:
            raise ConfigError(f"неизвестный провайдер {name!r}: допустимо одно из {PROVIDERS}")


def _fake(settings: Settings, pc_ids: Sequence[str]) -> FakeProvider:
    path = settings.fake_llm_scenario
    if path is None:
        raise ConfigError("провайдер fake: не задан FAKE_LLM_SCENARIO (путь к YAML-сценарию)")
    problem = None
    try:
        scenario = load_scenario(path)
    except (OSError, UnicodeDecodeError, UnreadableYaml, ValidationError) as exc:
        problem = _scenario_problem(exc)
    if problem is not None:
        # Исключение поднимаем вне `except`: его причина и контекст хранят содержимое файла.
        raise ConfigError(f"FAKE_LLM_SCENARIO={path}: не удалось прочитать сценарий: {problem}")
    # Очередь ходов хранится в провайдере, поэтому у каждой игры он свой.
    return FakeProvider(scenario, pc_ids)


def _scenario_problem(exc: Exception) -> str:
    """Что не так со сценарием, без цитат из файла.

    Для ошибки схемы — путь поля и сообщение, без значений. Остальные тексты безопасны сами по
    себе: `OSError` называет путь, `UnicodeDecodeError` — позицию байта, `UnreadableYaml` — вид
    ошибки YAML. Произвольный `ValueError` сюда не попадает: его текст может нести значение из
    файла."""
    if isinstance(exc, ValidationError):
        return schema_problems(exc)
    if isinstance(exc, OSError):
        return describe_os_error(exc)
    return str(exc)


def _anthropic_api(settings: Settings, model: str | None) -> AnthropicProvider:
    api_key = _secret_value(settings.anthropic_api_key)
    if api_key is None:
        raise ConfigError("провайдер anthropic_api: не задан ANTHROPIC_API_KEY")
    _refuse_custom_headers_from_environment("ANTHROPIC_CUSTOM_HEADERS", "Anthropic API")
    client = AsyncAnthropic(
        api_key=api_key,
        base_url=ANTHROPIC_API_URL,
        timeout=REQUEST_TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    try:
        return AnthropicProvider(
            client,
            model=model or settings.llm_model,
            effort=settings.llm_effort,
            cheap_model=settings.llm_cheap_model,
        )
    except ValueError as exc:
        # Адаптер отвергает неизвестный LLM_EFFORT; в его сообщении ключа нет.
        raise ConfigError(f"провайдер anthropic_api: {exc}") from exc


def _openai_compatible(settings: Settings, model: str | None) -> OpenAICompatibleProvider:
    base_url = settings.openai_compat_base_url
    if not base_url:
        raise ConfigError("провайдер openai_compatible: не задан OPENAI_COMPAT_BASE_URL")
    # LLM_MODEL здесь не подходит: это модель Anthropic, у чужого сервера её нет.
    model = model or settings.openai_compat_model
    if not model:
        raise ConfigError("провайдер openai_compatible: не задан OPENAI_COMPAT_MODEL")
    if not _is_http_address(base_url):
        raise ConfigError(
            "провайдер openai_compatible: OPENAI_COMPAT_BASE_URL должен быть адресом "
            "вида http://хост:порт/путь или https://хост/путь"
        )
    _refuse_custom_headers_from_environment(
        "OPENAI_CUSTOM_HEADERS", "сервер из OPENAI_COMPAT_BASE_URL"
    )
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=_secret_value(settings.openai_compat_api_key) or KEYLESS_API_KEY,
        # Организацию и проект SDK берёт из OPENAI_ORG_ID / OPENAI_PROJECT_ID: Omit снимает оба
        # заголовка, чужому серверу они не предназначены.
        default_headers={"OpenAI-Organization": Omit(), "OpenAI-Project": Omit()},
        timeout=REQUEST_TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    # LLM_CHEAP_MODEL тоже модель Anthropic: совместимому серверу дешёвая модель не передаётся.
    return OpenAICompatibleProvider(client, model)


def _claude_code_cli(
    settings: Settings,
    model: str | None,
    turn_tokens: TurnTokens | None,
    internal_url: str | None,
) -> ClaudeCodeCliProvider:
    if settings.app_env != "dev":
        raise ConfigError("claude_code_cli доступен только в dev-режиме")
    if turn_tokens is None or not internal_url:
        raise ConfigError(
            "провайдер claude_code_cli: нужны реестр токенов хода и адрес сервера для моста MCP"
        )
    try:
        return ClaudeCodeCliProvider(
            turn_tokens,
            internal_url,
            cli_path=settings.claude_cli_path,
            model=_cli_model(model, settings),
            effort=settings.claude_cli_effort,
            cheap_model=settings.claude_cli_cheap_model,
        )
    except ValueError as exc:
        # Адаптер отвергает значение, которое нельзя пустить в командную строку CLI; самого значения
        # в его сообщении нет.
        raise ConfigError(f"провайдер claude_code_cli: {exc}") from None


def _cli_model(model: str | None, settings: Settings) -> str | None:
    """Модель хода `claude_code_cli` (спека §11): модель игры → CLAUDE_CLI_MODEL → выбор самого CLI.

    Модель игры берётся, только если её примет адаптер (`is_model_name`). Список формы проверяется
    лишь при создании игры и по провайдеру на тот момент: игру без провайдера могли создать под
    другим LLM_PROVIDER с моделью вроде `qwen2.5:14b`, и после переключения на CLI её ходы не должны
    падать. Такая модель пропускается с записью в журнал сервера; CLAUDE_CLI_MODEL — настройка
    владельца, его проверяет адаптер, и ошибка остаётся ошибкой настройки."""
    if model is not None and not is_model_name(model):
        logger.warning(
            "Модель «%s» не подходит для claude_code_cli: вместо неё берётся CLAUDE_CLI_MODEL "
            "или модель CLI по умолчанию",
            printable(model, MAX_LOGGED_MODEL_CHARS),
        )
        model = None
    return model or settings.claude_cli_model


def _refuse_custom_headers_from_environment(variable: str, recipient: str) -> None:
    """`ConfigError`, если в окружении задана `variable` (`*_CUSTOM_HEADERS`); `recipient` — кому.

    SDK добавляет её заголовки к каждому запросу, а `Authorization` или `x-api-key` из них заменяет
    ключ клиента. Отменить это параметром нельзя, поэтому переменную надо снять. Значение в текст
    ошибки не попадает: в нём могут быть токены."""
    if os.environ.get(variable):
        raise ConfigError(
            f"{variable} задана в окружении сервера: SDK отправил бы её заголовки "
            f"(в том числе с учётными данными) на {recipient}; снимите переменную для сервера"
        )


def _secret_value(secret: SecretStr | None) -> str | None:
    """Значение секрета; пустой секрет считается незаданным."""
    if secret is None:
        return None
    return secret.get_secret_value() or None


def _is_http_address(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)
