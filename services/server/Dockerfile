# Образ сервера игры (dnd-server) для docker compose: Python 3.14, зависимости по uv.lock и
# Claude Code CLI для мастера по подписке (провайдер claude_code_cli, только APP_ENV=dev).
# Контекст сборки — корень репозитория (см. compose.yaml).
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /uvx /bin/

# curl нужен установщику Claude Code.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Сервер и CLI работают не от root.
RUN useradd --create-home --uid 1000 app && mkdir -p /app && chown app:app /app
USER app

# Claude Code ставится в ~/.local/bin. Установщик запускают не из корня файловой системы, иначе
# он долго обходит весь диск (совет документации Claude Code). Папка настроек CLI — том compose:
# вход в подписку делается один раз и переживает пересборку образа.
WORKDIR /tmp
RUN curl -fsSL https://claude.ai/install.sh | bash && mkdir -p /home/app/.claude
ENV PATH="/app/.venv/bin:/home/app/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR=/home/app/.claude \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app
# Сначала описания пакетов и lock-файл: слой зависимостей пересобирается только при их смене.
COPY --chown=app:app pyproject.toml uv.lock .python-version ./
COPY --chown=app:app services/engine/pyproject.toml services/engine/
COPY --chown=app:app services/server/pyproject.toml services/server/
RUN uv sync --locked --no-dev --all-packages --no-install-workspace

# Пакеты workspace ставятся в режиме разработки: по services/server/alembic.ini сервер находит
# корень репозитория (/app), от него считается относительный DATABASE_URL.
COPY --chown=app:app services/engine services/engine
COPY --chown=app:app services/server services/server
COPY --chown=app:app content content
RUN uv sync --locked --no-dev --all-packages

# Миграции до head перед каждым стартом: на актуальной базе они ничего не делают.
CMD ["sh", "-c", "alembic -c services/server/alembic.ini upgrade head && exec dnd-server"]
