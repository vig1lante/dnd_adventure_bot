#!/usr/bin/env python3
"""Имитатор CLI `claude` для тестов адаптера `claude_code_cli` (план B5.4).

Настоящий `claude` в тестах не запускается никогда: имитатор записывает, с чем его вызвали,
и печатает заданный ответ. Только стандартная библиотека, чтобы запускаться любым Python.

Управление — переменными окружения (их выставляет скрипт-обёртка из теста):
- `FAKE_CLAUDE_LOG` — файл журнала (JSON-строка на каждый запуск): `argv`, окружение, `cwd`,
  файлы в `cwd`, содержимое `mcp.json` и `system.md`, `pid`. Без него журнал не пишется;
- `FAKE_CLAUDE_REPLIES` — JSON-массив строк stdout; N-й запуск с `-p` печатает N-ю строку
  (после последней повторяется последняя). По умолчанию — успешный ответ хода с `modelUsage`;
- `FAKE_CLAUDE_MODEL` — модель, которую ответ по умолчанию называет в `modelUsage` (так CLI
  разворачивает псевдоним в полное имя); без неё — значение `--model`, а без флага —
  `DEFAULT_MODEL`;
- `FAKE_CLAUDE_EXIT` — код возврата (по умолчанию 0); `FAKE_CLAUDE_STDERR` — текст в stderr
  (`{url}` в нём заменяется адресом моста из `mcp.json`);
- `FAKE_CLAUDE_VERSION_FAIL` — `--version` завершается ошибкой;
- `FAKE_CLAUDE_HANG` — `1`: не отвечать, пока не придёт SIGINT; `ignore-sigint`: не реагировать
  и на SIGINT (останавливается только SIGKILL);
- `FAKE_CLAUDE_TOOL_CALLS` — JSON-массив `{name, args}`: перед ответом имитатор вызывает
  внутренний API по адресу из `mcp.json`, как это делал бы мост MCP;
- `FAKE_CLAUDE_IGNORE_TOOL_ERRORS` — ошибки HTTP при этих вызовах не роняют имитатор (модель
  «справилась» и дала ответ).
"""

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HANG_LIMIT_S = 60
VERSION_TEXT = "9.9.9 (Claude Code)"
DEFAULT_MODEL = "claude-fake-subscription"  # модель по умолчанию «подписки» имитатора
DEFAULT_REPLY = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Тени скрывают вас…",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5,
    },
}


def option_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def read_text(path: str | None) -> str | None:
    return Path(path).read_text(encoding="utf-8") if path else None


def write_log(argv: list[str]) -> None:
    log_path = os.environ.get("FAKE_CLAUDE_LOG")
    if not log_path:
        return
    mcp_text = read_text(option_value(argv, "--mcp-config"))
    entry = {
        "argv": argv,
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "cwd_files": sorted(os.listdir(".")),
        "mcp_config": json.loads(mcp_text) if mcp_text else None,
        "system_prompt": read_text(option_value(argv, "--system-prompt-file")),
        "pid": os.getpid(),
    }
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")


def prompt_runs_so_far() -> int:
    """Сколько запусков с `-p` записано в журнал, включая текущий (для очереди ответов)."""
    log_path = os.environ.get("FAKE_CLAUDE_LOG")
    if not log_path or not os.path.exists(log_path):
        return 0
    with open(log_path, encoding="utf-8") as log:
        return sum("-p" in json.loads(line)["argv"] for line in log)


def bridge_url(argv: list[str]) -> str | None:
    mcp_text = read_text(option_value(argv, "--mcp-config"))
    if not mcp_text:
        return None
    return option_value(json.loads(mcp_text)["mcpServers"]["dm"]["args"], "--url")


def call_internal_api(argv: list[str]) -> None:
    """Вызывает инструменты хода через внутренний API так же, как мост из `mcp.json`."""
    calls = json.loads(os.environ.get("FAKE_CLAUDE_TOOL_CALLS", "[]"))
    base_url = bridge_url(argv)
    for call in calls:
        request = urllib.request.Request(
            f"{base_url}/call",
            data=json.dumps(call).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except urllib.error.HTTPError:
            if not os.environ.get("FAKE_CLAUDE_IGNORE_TOOL_ERRORS"):
                raise


def default_reply(argv: list[str]) -> dict:
    """Успешный ответ хода; `modelUsage` называет модель, которой «ответил» CLI."""
    model = os.environ.get("FAKE_CLAUDE_MODEL") or option_value(argv, "--model") or DEFAULT_MODEL
    usage = DEFAULT_REPLY["usage"]
    model_usage = {"inputTokens": usage["input_tokens"], "outputTokens": usage["output_tokens"]}
    return {**DEFAULT_REPLY, "modelUsage": {model: model_usage}}


def reply_for_this_run(argv: list[str]) -> str:
    raw = os.environ.get("FAKE_CLAUDE_REPLIES")
    if raw is None:
        return json.dumps(default_reply(argv), ensure_ascii=False)
    replies = json.loads(raw)
    return replies[min(prompt_runs_so_far() - 1, len(replies) - 1)]


def main() -> int:
    # Кодировка вывода не зависит от локали: окружение процесса урезано до allowlist.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    if "--version" in argv:
        write_log(argv)
        if os.environ.get("FAKE_CLAUDE_VERSION_FAIL"):
            print("версия недоступна", file=sys.stderr)
            return 3
        print(VERSION_TEXT)
        return 0
    write_log(argv)
    hang = os.environ.get("FAKE_CLAUDE_HANG")
    if hang == "ignore-sigint":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hang:
        # Предохранитель: если тест не остановил имитатор, он завершится сам, а не зависнет.
        time.sleep(HANG_LIMIT_S)
        return 1
    call_internal_api(argv)
    stderr_text = os.environ.get("FAKE_CLAUDE_STDERR")
    if stderr_text:
        print(stderr_text.replace("{url}", bridge_url(argv) or ""), file=sys.stderr)
    print(reply_for_this_run(argv))
    return int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
