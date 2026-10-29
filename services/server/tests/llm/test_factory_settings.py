"""Фабрика провайдеров на настоящих источниках настроек: окружение, `.env` и шаблон `.env.example`.

`test_factory.py` собирает `Settings(...)` аргументами. Здесь настройки приходят так, как их
получит сервер: из переменных окружения и файла `.env` в текущей папке. Проверяется стык
«источники -> `Settings` -> `make_provider` -> адаптер» и обещания README про шаблон.
"""

from pathlib import Path

import pytest

from dnd_server.config import Settings
from dnd_server.llm.factory import PROVIDERS, ConfigError, make_provider
from dnd_server.llm.fake import FakeProvider
from dnd_server.llm.internal_api import TurnTokens

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
BASIC_ROUND = Path(__file__).resolve().parents[1] / "scenarios" / "basic_round.yaml"
INTERNAL_URL = "http://127.0.0.1:8000"

# Полный набор переменных каждого провайдера, как их пишут в `.env`.
PROVIDER_ENV = {
    # Тестовый мастер, как и `claude_code_cli`, собирается только в dev (спека DND-2 §15).
    "fake": {"LLM_PROVIDER": "fake", "APP_ENV": "dev", "FAKE_LLM_SCENARIO": str(BASIC_ROUND)},
    "anthropic_api": {
        "LLM_PROVIDER": "anthropic_api",
        "ANTHROPIC_API_KEY": "sk-ant-api03-env-secret",
        "LLM_MODEL": "claude-env-model",
        "LLM_EFFORT": "low",
    },
    "openai_compatible": {
        "LLM_PROVIDER": "openai_compatible",
        "OPENAI_COMPAT_BASE_URL": "http://localhost:11434/v1",
        "OPENAI_COMPAT_MODEL": "llama-env",
    },
    "claude_code_cli": {
        "LLM_PROVIDER": "claude_code_cli",
        "APP_ENV": "dev",
        "CLAUDE_CLI_PATH": "/opt/claude",
    },
}


def build(**options):
    """Провайдер по настройкам из окружения и `.env`; всё для `claude_code_cli` передано."""
    return make_provider(Settings(), turn_tokens=TurnTokens(), internal_url=INTERNAL_URL, **options)


def set_env(monkeypatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_provider_list_of_the_factory_matches_the_scenario_of_this_file():
    assert set(PROVIDER_ENV) == set(PROVIDERS)


@pytest.mark.parametrize("name", PROVIDERS)
def test_every_provider_is_built_from_environment_variables_under_its_own_name(monkeypatch, name):
    set_env(monkeypatch, PROVIDER_ENV[name])

    provider = build()

    assert provider.name == name


def test_dotenv_file_in_the_working_folder_configures_the_provider(monkeypatch, tmp_path):
    lines = [f"{name}={value}" for name, value in PROVIDER_ENV["openai_compatible"].items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert build().name == "openai_compatible"


def test_environment_beats_dotenv_when_choosing_the_provider(monkeypatch, tmp_path):
    lines = [f"{name}={value}" for name, value in PROVIDER_ENV["openai_compatible"].items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_env(monkeypatch, PROVIDER_ENV["fake"])

    assert isinstance(build(), FakeProvider)


@pytest.mark.parametrize(
    ("name", "emptied", "variable"),
    [
        ("anthropic_api", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        ("openai_compatible", "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_BASE_URL"),
        ("openai_compatible", "OPENAI_COMPAT_MODEL", "OPENAI_COMPAT_MODEL"),
        ("fake", "FAKE_LLM_SCENARIO", "FAKE_LLM_SCENARIO"),
    ],
)
def test_an_empty_variable_means_not_set_even_when_dotenv_has_a_value(
    monkeypatch, tmp_path, name, emptied, variable
):
    # `ANTHROPIC_API_KEY=` в окружении снимает ключ из `.env`: фабрика не должна его «найти».
    lines = [f"{key}={value}" for key, value in PROVIDER_ENV[name].items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv(emptied, "")

    with pytest.raises(ConfigError, match=variable):
        build()


def test_the_secret_of_a_missing_setting_does_not_leak_through_the_error(monkeypatch):
    # Ключ другого провайдера задан, нужного нет: в тексте ошибки секретов быть не должно.
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-must-not-appear")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-compat-must-not-appear")

    with pytest.raises(ConfigError) as caught:
        build()

    text = f"{caught.value} {caught.value!r}"
    assert "must-not-appear" not in text


# --- шаблон `.env.example` (README: «Переменные окружения» и «Запуск с Telegram») ---


def template_settings(**overrides) -> Settings:
    return Settings(_env_file=ENV_EXAMPLE, **overrides)


def test_the_template_alone_does_not_give_a_provider_because_fake_has_no_scenario():
    with pytest.raises(ConfigError, match="FAKE_LLM_SCENARIO"):
        make_provider(template_settings())


def test_the_template_plus_a_scenario_gives_the_fake_provider():
    provider = make_provider(template_settings(fake_llm_scenario=BASIC_ROUND))

    assert isinstance(provider, FakeProvider)


def test_the_template_is_dev_so_claude_cli_is_allowed_with_it():
    provider = make_provider(
        template_settings(llm_provider="claude_code_cli"),
        turn_tokens=TurnTokens(),
        internal_url=INTERNAL_URL,
    )

    assert provider.name == "claude_code_cli"


def test_switching_the_template_to_prod_closes_claude_cli_as_the_readme_says():
    settings = template_settings(llm_provider="claude_code_cli", app_env="prod")

    with pytest.raises(ConfigError, match="dev"):
        make_provider(settings, turn_tokens=TurnTokens(), internal_url=INTERNAL_URL)
