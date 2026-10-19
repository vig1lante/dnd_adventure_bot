"""Адаптер `claude_code_cli`: ход мастера через CLI Claude Code по подписке (спека §2, §7, B5.4).

Только для соло-тестов владельца при `APP_ENV=dev` (спека §2); проверку режима делает фабрика
(B5.5), а внутренний API, без которого адаптер не работает, подключается лишь в dev (`app.py`).

Каждый ход — новый процесс `claude -p` без `--resume`: контекст собираем мы. Процесс стартует в
пустой временной папке вне репозитория (там лежат только `system.md` и `mcp.json`), а после хода
папка удаляется. Инструментов у CLI нет (`--tools ""`), доступны только инструменты мастера,
которые отдаёт мост MCP (`mcp_bridge`) из внутреннего API сервера по одноразовому токену хода.
Исполняем их мы (`ToolRecorder`): отсюда трасса хода, лимит вызовов и сбои исполнителя, а не из
вывода CLI.

Окружение процесса — allowlist, а не копия окружения сервера: токен бота, ключ сессий и API-ключи
в него не попадают, в том числе `ANTHROPIC_API_KEY` (с ним CLI списывал бы деньги с API, а не с
подписки). `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` отключает чтение CLAUDE.md; флаг `--bare` не
используется. Процесс ограничен тайм-аутом: по истечении ему шлют SIGINT, а если он не
завершился — убивают, зомби не остаётся. Токен хода отзывается, а исполнитель закрывается
для новых вызовов в любом исходе.

Живой `claude` в тестах не запускается (запрет проекта): их заменяет `tests/llm/fake_claude.py`.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from dnd_server.llm.base import (
    MAX_JSON_RETRIES,
    JsonRequest,
    ProviderError,
    ToolCallTrace,
    ToolExecutor,
    ToolLoopExceeded,
    TurnRequest,
    TurnResult,
    Usage,
    make_trace_call,
    schema_problems,
    strip_code_fence,
    within_turn_timeout,
)
from dnd_server.llm.internal_api import TurnTokens

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "dm"  # имя сервера в mcp.json: инструменты для CLI называются mcp__dm__*
BRIDGE_MODULE = "dnd_server.llm.mcp_bridge"
JSON_TIMEOUT_S = 180.0  # у JsonRequest тайм-аута нет, а у CLI нет клиента с тайм-аутом, как у SDK
VERSION_TIMEOUT_S = 15.0
MAX_ERROR_CHARS = 600  # сколько stderr и вывода CLI попадает в текст исключения
STDERR_TAIL_LINES = 10

# Что нужно самому CLI: поиск программ, домашняя папка с входом по подписке, локаль, временные
# файлы, сертификаты и прокси для выхода в сеть. Всё остальное (включая любые секреты сервера)
# дочерний процесс не получает.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
        "LANG", "LANGUAGE",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
        "CLAUDE_CONFIG_DIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    }
)  # fmt: skip

_REFUSED_AFTER_LIMIT = (
    "Ошибка: лимит вызовов инструментов за ход исчерпан. Заверши рассказ без новых вызовов."
)
_REFUSED_AFTER_FAILURE = "Ошибка: ход прерван, инструменты больше недоступны."
_REFUSED_AFTER_CLOSE = "Ошибка: ход завершён, инструменты больше недоступны."


class ToolRecorder:
    """Исполнитель инструментов одного хода CLI: пишет трассу, держит лимит и порядок вызовов.

    Оборачивает исполнитель оркестратора. Вызовы идут по одному (CLI может звать инструменты
    параллельно, а броски и изменения состояния должны идти в порядке прихода). Сверх
    `max_calls` вызов не исполняется, модель получает ошибку, а `exhausted` фиксирует нарушение
    для адаптера. Исключение исполнителя (баг движка) запоминается в `failure` и пробрасывается
    дальше; после него новые вызовы отклоняются, чтобы ход не продолжал менять состояние.

    По окончании хода рекордер закрывают (`close`): запрос, который прошёл проверку токена до его
    отзыва и ещё ждёт тело или очередь, после конца хода не исполняется. Уже начатый вызов
    закрытие не прерывает."""

    def __init__(self, executor: ToolExecutor, max_calls: int) -> None:
        self._executor = executor
        self._max_calls = max_calls
        self._lock = asyncio.Lock()
        self._closed = False
        self.trace: list[ToolCallTrace] = []
        self.exhausted = False
        self.failure: Exception | None = None

    def close(self) -> None:
        """Ход окончен: вызовы, начатые позже, не исполняются (флаг, а не ожидание очереди)."""
        self._closed = True

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        async with self._lock:
            # Закрытие проверяется под замком: вызов мог простоять в очереди до конца хода.
            if self._closed:
                return _REFUSED_AFTER_CLOSE, True
            if self.failure is not None:
                return _REFUSED_AFTER_FAILURE, True
            if len(self.trace) >= self._max_calls:
                self.exhausted = True
                return _REFUSED_AFTER_LIMIT, True
            try:
                result, is_error = await self._executor(name, args)
            except Exception as exc:
                self.failure = exc
                raise
            self.trace.append(make_trace_call(name, args, result, is_error))
            return result, is_error


class _CliUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def to_usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens or 0,
            output_tokens=self.output_tokens or 0,
            cache_read_tokens=self.cache_read_input_tokens or 0,
            cache_write_tokens=self.cache_creation_input_tokens or 0,
        )


class _CliResult(BaseModel):
    """Ответ `claude -p --output-format json`; лишние поля CLI (стоимость, сессия) игнорируются."""

    result: str | None = None
    is_error: bool = False
    subtype: str = "success"
    usage: _CliUsage = _CliUsage()


def _child_env() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _ENV_ALLOWLIST or name.startswith("LC_")
    }
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env


def _mcp_config(bridge_url: str) -> dict[str, Any]:
    # Мост запускает текущий интерпретатор: пакет dnd_server в его окружении уже установлен.
    bridge = {"command": sys.executable, "args": ["-m", BRIDGE_MODULE, "--url", bridge_url]}
    return {"mcpServers": {MCP_SERVER_NAME: bridge}}


def _cli_argv(cli_path: str, prompt: str, workdir: Path, *, with_tools: bool) -> list[str]:
    """Командная строка CLI; набор флагов фиксирован планом B5.4 и спекой §7.

    Промпт идёт позиционным аргументом после `-p`, и начинающийся с «-» текст CLI разобрал бы как
    свою опцию. В промпт попадает текст игроков, поэтому такой текст предваряется переводом
    строки: для модели он не меняется, а набор флагов остаётся прежним."""
    argv = [
        cli_path,
        "-p",
        f"\n{prompt}" if prompt.startswith("-") else prompt,
        "--system-prompt-file",
        str(workdir / "system.md"),
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    if with_tools:
        argv += [
            "--mcp-config",
            str(workdir / "mcp.json"),
            "--allowedTools",
            f"mcp__{MCP_SERVER_NAME}__*",
        ]
    return [*argv, "--permission-mode", "dontAsk", "--output-format", "json"]


def _clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_result(stdout: str) -> _CliResult:
    try:
        parsed = _CliResult.model_validate_json(stdout)
    except ValidationError:
        raise ProviderError(f"не удалось разобрать ответ claude: {_clip(stdout)}") from None
    if parsed.is_error or parsed.subtype != "success":
        raise ProviderError(
            f"claude вернул ошибку ({parsed.subtype}): {_clip(parsed.result or '')}"
        )
    if parsed.result is None:
        raise ProviderError("не удалось разобрать ответ claude: в ответе нет поля result")
    return parsed


def _stderr_tail(stderr: bytes, secret: str | None) -> str:
    """Последние строки stderr для текста ошибки; токен хода, если CLI его вывел, вырезается."""
    text = stderr.decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "***")
    tail = "\n".join(text.strip().splitlines()[-STDERR_TAIL_LINES:])
    return tail[-MAX_ERROR_CHARS:] or "stderr пуст"


class ClaudeCodeCliProvider:
    """`LLMProvider` поверх CLI `claude`; реестр токенов и адрес сервера внедряются."""

    name = "claude_code_cli"

    def __init__(
        self,
        tokens: TurnTokens,
        internal_url: str,
        cli_path: str = "claude",
        stop_grace_s: float = 5.0,
    ) -> None:
        """`internal_url` — адрес сервера, по которому до него дотянется мост (например
        `http://127.0.0.1:8000`); `cli_path` — `CLAUDE_CLI_PATH`, он же способ зафиксировать
        версию CLI; `stop_grace_s` — сколько ждать после SIGINT, прежде чем убить процесс."""
        self._tokens = tokens
        self._internal_url = internal_url.rstrip("/")
        self._cli_path = cli_path
        self._stop_grace_s = stop_grace_s
        self._cli_version: str | None = None

    @property
    def cli_version(self) -> str | None:
        """Вывод `claude --version`, прочитанный перед первым запуском (без запуска модели).

        Поведение CLI подтверждено не полностью (спека §16), поэтому версию стоит писать
        в журнал хода: оркестратор берёт её отсюда. `None`, пока версия не определена."""
        return self._cli_version

    async def run_turn(self, req: TurnRequest, tools: ToolExecutor) -> TurnResult:
        recorder = ToolRecorder(tools, req.max_tool_iterations)
        token = self._tokens.issue(recorder, req.tools)
        try:
            output = await self._invoke(
                system=f"{req.system}\n\n{req.adventure_brief}",
                prompt=req.context,
                timeout_s=req.timeout_s,
                bridge_url=f"{self._internal_url}/internal/dm-tools/{token}",
                secret=token,
            )
        except ProviderError:
            # Сбой исполнителя инструментов — причина, а ошибка CLI — следствие (ответ 500 модели).
            if recorder.failure is not None:
                raise recorder.failure from None
            raise
        finally:
            # Рекордер закрывается вместе с отзывом токена: запрос, уже прошедший проверку токена,
            # не должен исполниться после конца хода.
            recorder.close()
            self._tokens.revoke(token)
        if recorder.failure is not None:
            raise recorder.failure
        if recorder.exhausted:
            raise ToolLoopExceeded(f"больше {req.max_tool_iterations} вызовов инструментов за ход")
        return TurnResult(
            text=(output.result or "").strip(),
            trace=tuple(recorder.trace),
            usage=output.usage.to_usage(),
            stop_reason="end_turn",
        )

    async def complete_json[T: BaseModel](self, req: JsonRequest, schema: type[T]) -> T:
        # CLI не умеет структурированный вывод по схеме: схема идёт в промпт, ответ проверяем сами.
        instruction = (
            "Ответь только JSON-объектом по этой JSON Schema, без пояснений и без markdown:\n"
            f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        )
        prompt = f"{req.prompt}\n\n{instruction}"
        problems = ""
        for _ in range(MAX_JSON_RETRIES + 1):
            output = await self._invoke(system=req.system, prompt=prompt, timeout_s=JSON_TIMEOUT_S)
            try:
                return schema.model_validate_json(strip_code_fence(output.result or ""))
            except ValidationError as exc:
                problems = schema_problems(exc)
                prompt = (
                    f"{req.prompt}\n\n{instruction}\n\n"
                    f"Предыдущий ответ не прошёл проверку схемы:\n{problems}\n"
                    "Исправь эти ошибки и верни JSON заново."
                )
        raise ProviderError(
            f"JSON-ответ {req.purpose!r} не подходит схеме {schema.__name__} "
            f"после {MAX_JSON_RETRIES + 1} попыток: {problems}"
        )

    async def _invoke(
        self,
        *,
        system: str,
        prompt: str,
        timeout_s: float,
        bridge_url: str | None = None,
        secret: str | None = None,
    ) -> _CliResult:
        """Один запуск CLI во временной папке; с `bridge_url` ему доступны инструменты хода."""
        with tempfile.TemporaryDirectory(prefix="dnd-claude-") as folder:
            workdir = Path(folder)
            (workdir / "system.md").write_text(system, encoding="utf-8")
            if bridge_url is not None:
                config = json.dumps(_mcp_config(bridge_url), ensure_ascii=False)
                (workdir / "mcp.json").write_text(config, encoding="utf-8")
            await self._remember_version(workdir)
            argv = _cli_argv(self._cli_path, prompt, workdir, with_tools=bridge_url is not None)
            stdout = await self._exec(argv, workdir, timeout_s, secret)
        return _parse_result(stdout)

    async def _remember_version(self, workdir: Path) -> None:
        if self._cli_version is not None:
            return
        try:
            version = await self._exec([self._cli_path, "--version"], workdir, VERSION_TIMEOUT_S)
        except ProviderError as exc:
            # Версия нужна для журнала, а не для хода: сбой запуска проявится в самом ходе.
            logger.warning("claude_code_cli: не удалось узнать версию CLI: %s", exc)
            return
        self._cli_version = _clip(version.strip(), 100)
        logger.info("claude_code_cli: версия CLI %s", self._cli_version)

    async def _exec(
        self, argv: list[str], cwd: Path, timeout_s: float, secret: str | None = None
    ) -> str:
        """Запускает процесс с урезанным окружением и возвращает stdout.

        Тайм-аут, ненулевой код и невозможность запуска — `ProviderError`. В любом исходе
        (включая отмену задачи) процесс к выходу отсюда уже завершён и забран."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=_child_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ProviderError(
                f"не удалось запустить claude ({argv[0]}): {exc.strerror}"
            ) from None
        try:
            stdout, stderr = await within_turn_timeout(timeout_s, process.communicate())
        finally:
            await self._stop(process)
        if process.returncode != 0:
            raise ProviderError(
                f"claude завершился с кодом {process.returncode}: {_stderr_tail(stderr, secret)}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        """Останавливает ещё живой процесс: сначала SIGINT, по истечении ожидания SIGKILL."""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGINT)
        try:
            async with asyncio.timeout(self._stop_grace_s):
                await process.wait()
        except TimeoutError:
            logger.warning(
                "claude_code_cli: процесс не завершился за %g с после SIGINT, он убит",
                self._stop_grace_s,
            )
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
