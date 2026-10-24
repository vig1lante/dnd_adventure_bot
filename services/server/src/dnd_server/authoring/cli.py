"""CLI `dnd-generate`: приключение формата v2 по короткому пожеланию (спека DND-1 §6.3, план
DND-1 B5.6; DND-2 §9, план B3.1).

    uv run dnd-generate --pitch "..." --hours 5 --players 6 --level 3 \\
        [--seeds seeds.yaml] [--wishes wishes.yaml] [--modules bg3_flavor,campfire] \\
        [--provider ...] [--model ...] --out content/adventures/<slug>

Команда собирает черновик (`AuthoringDraft`): заказ из флагов, заготовки `--seeds` (любые разделы
v2, все закреплены) и пожелания `--wishes` (`{шаг: текст}`), — и проходит генератор
(`pipeline.generate`) по ролям вокруг заданного.

Код выхода 0 — приключение записано и в отчёте проверки нет блокирующих ошибок. Код 2 — ошибка
аргументов или настройки, файла заготовок или пожеланий, отказ провайдера, непустая папка `--out`
либо блокирующие ошибки, которые не ушли за три круга исправления (папка в этом случае записана:
приключение можно доделать вручную). Всё, что можно проверить без модели, проверяется до первого
вызова.
"""

import argparse
import asyncio
import math
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from dnd_engine.adventure.budget import XP_BUDGET_PER_CHARACTER
from dnd_engine.adventure.loader import AdventureFormatError, dump_adventure
from dnd_engine.adventure.validator import Report
from dnd_engine.modules import MODULES
from dnd_server.authoring.draft import STEPS, AuthoringDraft, DraftSettings, Seeds, Wishes, seeded
from dnd_server.authoring.pipeline import generate
from dnd_server.config import Settings
from dnd_server.dm.provider import bridge_url
from dnd_server.llm.base import ProviderError, printable, schema_problems
from dnd_server.llm.factory import ConfigError, make_provider
from dnd_server.llm.fake import UnreadableYaml, describe_os_error, read_yaml
from dnd_server.llm.internal_api import TurnTokens

EXIT_OK = 0
EXIT_FAILED = 2  # то же значение, что у argparse для неверных аргументов

MAX_PLAYERS = 6
MAX_ERROR_CHARS = 1000

_WISHES = TypeAdapter(Wishes)


class CliError(Exception):
    """Ошибка запуска с понятным текстом: печатается как `Ошибка: ...`, код выхода 2."""


def _hours(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("нужно конечное число больше нуля")
    return value


def _count_between(low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"не целое число: {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"нужно число от {low} до {high}")
        return value

    return parse


def _modules(text: str) -> tuple[str, ...]:
    names = tuple(name.strip() for name in text.split(",") if name.strip())
    unknown = [name for name in names if name not in MODULES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"неизвестный модуль правил {', '.join(unknown)}; доступны: {', '.join(MODULES)}"
        )
    return names


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    levels = sorted(XP_BUDGET_PER_CHARACTER)
    parser = argparse.ArgumentParser(
        prog="dnd-generate",
        description="Генерирует приключение в папку YAML и печатает отчёт валидатора.",
    )
    parser.add_argument("--pitch", required=True, help="короткое пожелание: о чём приключение")
    parser.add_argument("--hours", required=True, type=_hours, help="целевая длина игры в часах")
    parser.add_argument(
        "--players",
        type=_count_between(1, MAX_PLAYERS),
        default=MAX_PLAYERS,
        help=f"число игроков, 1-{MAX_PLAYERS} (по умолчанию {MAX_PLAYERS})",
    )
    parser.add_argument(
        "--level",
        type=_count_between(levels[0], levels[-1]),
        default=3,
        help=f"уровень героев, {levels[0]}-{levels[-1]} (по умолчанию 3)",
    )
    parser.add_argument(
        "--seeds",
        type=Path,
        help="YAML с заготовками любых разделов v2 (world, deities, factions, plot, locations, "
        "npcs, relations, encounters, monsters, items, revelations): войдут как есть и закрепятся",
    )
    parser.add_argument(
        "--wishes",
        type=Path,
        help=f"YAML с пожеланиями к шагам генератора «шаг: текст»; шаги: {', '.join(STEPS)}",
    )
    parser.add_argument(
        "--modules",
        type=_modules,
        default=(),
        help=f"модули правил через запятую: {', '.join(MODULES)}",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="новая или пустая папка для приключения"
    )
    parser.add_argument("--provider", help="провайдер модели вместо LLM_PROVIDER")
    parser.add_argument("--model", help="модель вместо настроенной в окружении")
    return parser.parse_args(argv)


def _require_free_folder(out: Path) -> None:
    """`dump_adventure` папку не чистит: чужие файлы остались бы рядом с новыми."""
    if not out.exists():
        return
    if not out.is_dir():
        raise CliError(f"--out {out}: это не папка")
    # Скрытые записи (`.DS_Store` Finder, `.git`) загрузчик движка игнорирует, значит, не мешают.
    if any(not entry.name.startswith(".") for entry in out.iterdir()):
        raise CliError(f"папка --out {out} не пуста: укажите новую или пустую папку")


def _describe(exc: Exception) -> str:
    """Текст ошибки для строки «Ошибка: …», без значений из файлов.

    Для ошибки схемы — путь поля и сообщение. Остальные тексты безопасны сами по себе: `OSError`
    называет путь (у файла особого вида — причину «не обычный файл»), `UnicodeDecodeError` —
    позицию байта, `UnreadableYaml` — вид ошибки YAML или предел размера, `AdventureFormatError` —
    проблемы с путями файлов. Произвольный `ValueError` сюда не попадает: его текст может нести
    значение из файла (сиды могли указать на чужой файл). Имена полей и сообщения схемы
    нейтрализует `schema_problems`, остальное — барьер `_fail`."""
    if isinstance(exc, ValidationError):
        return schema_problems(exc)[:MAX_ERROR_CHARS]
    if isinstance(exc, OSError):
        return describe_os_error(exc)[:MAX_ERROR_CHARS]
    return str(exc)[:MAX_ERROR_CHARS]


def _load[T](path: Path, what: str, validate: Callable[[Any], T]) -> T:
    """Файл YAML автора, проверенный `validate`; любая ошибка — `CliError` без цитат из файла."""
    problem = None
    try:
        data = read_yaml(path)
        loaded = validate({} if data is None else data)
    except (OSError, UnicodeDecodeError, UnreadableYaml, ValidationError) as exc:
        problem = _describe(exc)
    if problem is not None:
        # Исключение поднимаем вне `except`: его причина и контекст хранят содержимое файла.
        raise CliError(f"{what} {path}: {problem}")
    return loaded


def _load_seeds(path: Path) -> Seeds:
    return _load(path, "файл сидов", Seeds.model_validate)


def _load_wishes(path: Path) -> Wishes:
    return _load(path, "файл пожеланий", _WISHES.validate_python)


def _draft(args: argparse.Namespace, seeds: Seeds, wishes: Wishes) -> AuthoringDraft:
    """Черновик заказа: настройки из флагов, пожелания по шагам и закреплённые заготовки."""
    settings = DraftSettings(
        pitch=args.pitch,
        target_hours=args.hours,
        party_size=args.players,
        party_level=args.level,
        modules=args.modules,
    )
    return seeded(AuthoringDraft(settings=settings, wishes=wishes), seeds)


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
        raise CliError(f"неверные переменные окружения: {fields}") from exc


def _settings_for(model: str | None) -> Settings:
    """Настройки запуска: явный `--model` побеждает `LLM_CHEAP_MODEL` во всех шагах генератора.

    Шаги идут с признаком «дешёвая модель», и адаптер предпочёл бы дешёвую модель из окружения
    модели из флага; без дешёвой модели все запросы уходят с заданной."""
    settings = _load_settings()
    return settings.model_copy(update={"llm_cheap_model": None}) if model else settings


def format_report(report: Report, target_hours: float) -> str:
    """Отчёт валидатора для терминала: проблемы по порядку, затем счётчики, балл и время."""
    names = {"error": "ошибка", "warning": "предупреждение"}
    lines = ["Отчёт валидатора"]
    lines += [
        f"[{names[issue.severity]}] {issue.code} {issue.ref}: {issue.message}"
        for issue in report.issues
    ]
    if not report.issues:
        lines.append("Замечаний нет")
    errors = sum(issue.severity == "error" for issue in report.issues)
    lines += [
        f"Ошибок: {errors}, предупреждений: {len(report.issues) - errors}",
        f"Балл: {report.score}",
        f"Оценка времени игры: {report.estimated_minutes} мин (цель: {target_hours:g} ч)",
    ]
    return "\n".join(lines)


def _say(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        _require_free_folder(args.out)
        seeds = _load_seeds(args.seeds) if args.seeds else Seeds()
        wishes = _load_wishes(args.wishes) if args.wishes else {}
        draft = _draft(args, seeds, wishes)
        provider = make_provider(
            _settings_for(args.model),
            provider=args.provider,
            model=args.model,
            # Генератор вызывает только `complete_json`, но фабрика claude_code_cli требует реестр
            # токенов и адрес моста MCP для `run_turn`. Ход мастера здесь не ведётся: токены не
            # выдаются, моста генератор не поднимает и адрес не открывает, поэтому свой адрес ему не
            # нужен: берётся тот же, что у сервера игры (`dm.provider.bridge_url`).
            turn_tokens=TurnTokens(),
            internal_url=bridge_url(),
        )
        result = asyncio.run(generate(provider, draft, _say))
        dump_adventure(result.adventure, args.out)
    except CliError as exc:
        return _fail(str(exc))
    except ConfigError as exc:
        return _fail(f"настройка провайдера: {exc}")
    except ProviderError as exc:
        return _fail(f"провайдер модели: {exc}")
    except ValidationError as exc:
        # Черновик проверяется при чтении файлов, так что сюда доходит только нарушенный инвариант
        # сборки: строка без трассировки и без значений из данных (`schema_problems`).
        return _fail(f"сборка приключения: {_describe(exc)}")
    except (OSError, AdventureFormatError) as exc:
        return _fail(f"не удалось записать приключение в {args.out}: {_describe(exc)}")
    print(format_report(result.report, args.hours))
    if result.report.blocking:
        print(
            f"Приключение записано в {args.out}, но в нём остались блокирующие ошибки: "
            "игру на нём создать нельзя, исправьте YAML вручную."
        )
        return EXIT_FAILED
    print(f"Приключение записано в {args.out}")
    return EXIT_OK


def _fail(message: str) -> int:
    # Последний барьер перед терминалом владельца: в сообщение попадают куски ответа модели
    # (отказ, вывод и stderr CLI), файла сидов и путей. Строки сохраняются: хвост stderr CLI и
    # `AdventureFormatError` многострочные.
    print(f"Ошибка: {printable(message, keep_newlines=True)}", file=sys.stderr)
    return EXIT_FAILED
