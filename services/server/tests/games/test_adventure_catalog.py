"""Каталог приключений (план B6.3): загрузка `CONTENT_DIR` при старте и `GET /api/adventures`.

Папки владельца — вход сервера: битая папка пропускается с записью в журнал и не роняет старт, а
отчёт валидатора (в нём id скрытых выходов и запертых мест) игрокам не выдаётся. Приложение
работает на настоящей SQLite после миграций; `lifespan` запускается явно, потому что клиент в памяти
(ASGI-транспорт) события жизненного цикла не посылает.
"""

import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.validator import MAX_MESSAGE_LENGTH, MAX_REF_LENGTH, validate
from dnd_engine.content import MAX_PROBLEM_LENGTH, load_srd
from dnd_server import adventures
from dnd_server.adventures import blocking_problems, load_catalog
from dnd_server.app import create_app
from dnd_server.authoring.pipeline import check
from dnd_server.auth.session import SessionSigner
from dnd_server.config import Settings
from dnd_server.db.models import User

# tests/games/test_adventure_catalog.py -> games -> tests -> server -> services -> корень
DEMO_ADVENTURE_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
SESSION_SECRET = "test-session-secret-0123456789-abcdefghij"
# Скрытый выход демо-приключения: его id не должен попасть игрокам.
HIDDEN_EXIT_ID = "to-crypt"
PLAN_FIELDS = {"id", "title", "pitch", "target_hours", "score", "blocking"}


def copy_demo(content_dir: Path, name: str = "demo-crypt") -> Path:
    return Path(shutil.copytree(DEMO_ADVENTURE_DIR, content_dir / name))


def add_blocked_adventure(content_dir: Path, adventure_id: str = "blocked-crypt") -> None:
    """Копия демо, где скрытый выход ведёт в пустоту: загрузка проходит, валидатор видит ошибку."""
    folder = copy_demo(content_dir, adventure_id)
    manifest = folder / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("id: demo-crypt", f"id: {adventure_id}", 1),
        encoding="utf-8",
    )
    cemetery = folder / "locations" / "cemetery.yaml"
    cemetery.write_text(
        cemetery.read_text(encoding="utf-8").replace("to: crypt", "to: nowhere", 1),
        encoding="utf-8",
    )


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "content"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(settings: Settings, content_dir: Path) -> Settings:
    return settings.model_copy(
        update={"content_dir": content_dir, "session_secret": SecretStr(SESSION_SECRET)}
    )


@asynccontextmanager
async def running_app(settings: Settings):
    """Приложение с выполненным стартом (`lifespan`) и клиент к нему."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            yield app, http


async def auth_headers(db: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async with db() as session:
        user = User(dev_login="dev-1", name="Хост")
        session.add(user)
        await session.commit()
    token = SessionSigner(SESSION_SECRET).issue(user.id)
    return {"Authorization": f"Bearer {token}"}


# --- load_catalog ---


def test_every_valid_folder_is_loaded_with_its_report(content_dir):
    copy_demo(content_dir)

    catalog = load_catalog(content_dir, load_srd())

    entry = catalog["demo-crypt"]
    assert entry.adventure == load_adventure(DEMO_ADVENTURE_DIR)
    assert entry.report == validate(entry.adventure, load_srd())
    assert not entry.report.blocking


def test_catalog_is_keyed_by_adventure_id_not_by_folder_name(content_dir):
    copy_demo(content_dir, "any-folder-name")

    assert list(load_catalog(content_dir, load_srd())) == ["demo-crypt"]


def test_broken_folder_is_skipped_and_logged_the_rest_is_loaded(content_dir, caplog):
    copy_demo(content_dir)
    broken = copy_demo(content_dir, "broken-one")
    (broken / "adventure.yaml").write_text("title: [не закрыто", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert list(catalog) == ["demo-crypt"]
    assert "broken-one" in caplog.text


def test_unexpected_failure_in_one_folder_does_not_stop_the_others(
    content_dir, caplog, monkeypatch
):
    copy_demo(content_dir)
    copy_demo(content_dir, "aaa-first")
    real_load = adventures.load_adventure

    def flaky_load(path: Path):
        if path.name == "aaa-first":
            raise RuntimeError("непредвиденный сбой чтения")
        return real_load(path)

    monkeypatch.setattr(adventures, "load_adventure", flaky_load)

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert list(catalog) == ["demo-crypt"]
    assert "aaa-first" in caplog.text


def test_unexpected_failure_in_a_folder_is_logged_without_the_input_of_the_error(
    content_dir, caplog, monkeypatch, leak
):
    copy_demo(content_dir)
    copy_demo(content_dir, "aaa-first")
    real_load = adventures.load_adventure

    def flaky_load(path: Path):
        if path.name == "aaa-first":
            raise leak.error  # ошибка проверки данных: в её тексте лежит значение входа
        return real_load(path)

    monkeypatch.setattr(adventures, "load_adventure", flaky_load)

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert list(catalog) == ["demo-crypt"]
    [record] = caplog.records
    assert record.exc_info is None  # трассировку формирует сервер, а не logging
    assert "aaa-first" in caplog.text and "ValidationError" in caplog.text
    assert leak.value not in caplog.text and "input_value" not in caplog.text


def test_stray_files_and_hidden_folders_are_not_adventures(content_dir, caplog):
    copy_demo(content_dir)
    (content_dir / ".DS_Store").write_bytes(b"\x00")
    (content_dir / "notes.txt").write_text("заметки", encoding="utf-8")
    copy_demo(content_dir, ".hidden-copy")

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert list(catalog) == ["demo-crypt"]
    assert caplog.text == ""


def test_missing_content_dir_gives_an_empty_catalog_and_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(tmp_path / "no-such-dir", load_srd())

    assert catalog == {}
    assert "no-such-dir" in caplog.text


def test_second_folder_with_the_same_adventure_id_is_skipped(content_dir, caplog):
    copy_demo(content_dir, "a-original")
    copy_demo(content_dir, "b-duplicate")

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert list(catalog) == ["demo-crypt"]
    assert "b-duplicate" in caplog.text


def test_blocking_adventure_is_kept_with_its_report_and_the_details_go_to_the_log(
    content_dir, caplog
):
    add_blocked_adventure(content_dir)

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert catalog["blocked-crypt"].report.blocking
    assert HIDDEN_EXIT_ID in caplog.text  # подробности — в журнал сервера, не игрокам


# --- одна проверка для всех путей (дополнение dispatcher B6 §3, §4; security B3 I5) ---


CUSTOM_MONSTER = """\
id: crypt-warden
name: Страж склепа
name_en: Crypt Warden
cr: 2
xp: {xp}
ac: 14
hp: 45
hp_dice: 6d10+12
speed: 30
scores: {{str: 16, dex: 10, con: 14, int: 6, wis: 10, cha: 6}}
actions:
  - id: slam
    name: Удар
    kind: attack
    attack_bonus: 5
    damage: 2d6+3
    damage_type: bludgeoning
"""


def add_custom_monster(folder: Path, xp: int) -> None:
    (folder / "monsters").mkdir()
    (folder / "monsters" / "crypt-warden.yaml").write_text(
        CUSTOM_MONSTER.format(xp=xp), encoding="utf-8"
    )


def test_the_demo_folder_has_no_custom_statblocks_and_passes_the_shared_check(content_dir):
    copy_demo(content_dir)

    entry = load_catalog(content_dir, load_srd())["demo-crypt"]

    assert entry.adventure.monsters == {}
    assert entry.report == check(entry.adventure, load_srd())
    assert not entry.report.blocking


def test_a_folder_with_a_custom_statblock_of_wrong_xp_is_blocking_like_in_the_constructor(
    content_dir,
):
    add_custom_monster(copy_demo(content_dir), xp=1800)

    entry = load_catalog(content_dir, load_srd())["demo-crypt"]

    assert entry.report.blocking
    assert [i.code for i in entry.report.issues if i.severity == "error"] == ["custom_monster_xp"]


def test_a_folder_with_a_custom_statblock_of_the_xp_of_its_cr_is_playable(content_dir):
    add_custom_monster(copy_demo(content_dir), xp=450)

    entry = load_catalog(content_dir, load_srd())["demo-crypt"]

    assert "crypt-warden" in entry.adventure.monsters
    assert not entry.report.blocking


def test_a_folder_whose_secret_reveals_an_item_lying_elsewhere_is_blocking(content_dir):
    # Раньше папку проверял голый валидатор, а генератор считал такую находку блокирующей.
    folder = copy_demo(content_dir)
    crypt = folder / "locations" / "crypt.yaml"
    item = "chest-key"  # ключ у Марты, а не в склепе
    crypt.write_text(
        crypt.read_text(encoding="utf-8").replace(
            "      - kind: flag\n        id: treasury-empty",
            f"      - kind: flag\n        id: treasury-empty\n      - kind: item\n        id: {item}",
        ),
        encoding="utf-8",
    )

    entry = load_catalog(content_dir, load_srd())["demo-crypt"]

    assert "inapplicable_secret_reveal" in {i.code for i in entry.report.issues}
    assert entry.report.blocking


# --- старт сервера ---


async def test_server_starts_with_a_broken_folder_and_serves_the_valid_ones(db, settings):
    copy_demo(settings.content_dir)
    broken = copy_demo(settings.content_dir, "broken-one")
    (broken / "adventure.yaml").write_text("title: [не закрыто", encoding="utf-8")

    async with running_app(settings) as (app, _http):
        assert list(app.state.adventures) == ["demo-crypt"]


async def test_server_starts_when_content_dir_does_not_exist(db, settings, tmp_path):
    settings = settings.model_copy(update={"content_dir": tmp_path / "no-such-dir"})

    async with running_app(settings) as (app, http):
        headers = await auth_headers(db)
        response = await http.get("/api/adventures", headers=headers)

    assert app.state.adventures == {}
    assert response.json() == []


# --- GET /api/adventures ---


async def test_adventure_list_needs_a_login(db, settings):
    copy_demo(settings.content_dir)

    async with running_app(settings) as (_app, http):
        anonymous = await http.get("/api/adventures")
        wrong_token = await http.get("/api/adventures", headers={"Authorization": "Bearer x"})

    assert [anonymous.status_code, wrong_token.status_code] == [401, 401]


async def test_adventure_list_gives_only_the_fields_of_the_plan(db, settings):
    copy_demo(settings.content_dir)
    adventure = load_adventure(DEMO_ADVENTURE_DIR)
    report = validate(adventure, load_srd())

    async with running_app(settings) as (_app, http):
        response = await http.get("/api/adventures", headers=await auth_headers(db))

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "demo-crypt",
            "title": adventure.title,
            "pitch": adventure.pitch,
            "target_hours": adventure.target_hours,
            "score": report.score,
            "blocking": False,
        }
    ]


async def test_players_see_the_blocking_flag_but_never_the_issues(db, settings):
    add_blocked_adventure(settings.content_dir)

    async with running_app(settings) as (app, http):
        response = await http.get("/api/adventures", headers=await auth_headers(db))

    [item] = response.json()
    assert set(item) == PLAN_FIELDS
    assert item["blocking"] is True
    # Ни ссылка, ни текст проблем: в них id скрытых выходов и запертых мест.
    assert HIDDEN_EXIT_ID not in response.text
    for issue in app.state.adventures["blocked-crypt"].report.issues:
        assert issue.ref not in response.text
        assert issue.message not in response.text


# --- журнал сервера и чужие папки ---
# Папка в `CONTENT_DIR` — чужой ввод: ключи, имена файлов, теги YAML и флаги в ней могут нести
# управляющие символы и быть огромными. Запись журнала о такой папке — одна короткая печатная
# строка: ни перевода строки (подделанной записи), ни ESC (команд терминала), ни портянки.
# Движок обезвреживает проблемы загрузчика (`printable`, предел длины), сервер склеивает не более
# `MAX_REPORTED_PROBLEMS` проблем в одну запись.

HOSTILE_KEY = "\x1b[2J\x1b[31mINJECT\nWARNING поддельная запись\r\u2028\u202e"


def append_to_manifest(folder: Path, line: str) -> None:
    manifest = folder / "adventure.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + line, encoding="utf-8")


def hostile_extra_key(folder: Path) -> None:
    append_to_manifest(folder, f"\n{json.dumps(HOSTILE_KEY)}: 1\n")


def hostile_long_key(folder: Path) -> None:
    append_to_manifest(folder, f"\n? {'k' * 20_000}\n: 1\n")  # явный ключ: неявный короче 1024


def hostile_yaml_tag(folder: Path) -> None:
    append_to_manifest(folder, f"\nnotes: !!python/object/apply:{'x' * 5_000} [1]\n")


def hostile_file_names(folder: Path) -> None:
    (folder / "locations" / "bad\nname.yaml").write_text("id: bad", encoding="utf-8")
    (folder / "locations" / "evil\x1b[2J.txt").write_text("текст", encoding="utf-8")


def thousands_of_problems(folder: Path) -> None:
    for number in range(600):
        (folder / "locations" / f"broken-{number}.yaml").write_text("id: [", encoding="utf-8")


def hostile_flag_name(folder: Path) -> None:
    """Замок ждёт флаг с префиксом движка: блокирующая ошибка называет флаг из чужого файла.

    Папка при этом загружается, поэтому id у неё свой, иначе её пропустили бы как дубликат."""
    manifest = folder / "adventure.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("id: demo-crypt", "id: hostile", 1),
        encoding="utf-8",
    )
    cemetery = folder / "locations" / "cemetery.yaml"
    cemetery.write_text(
        cemetery.read_text(encoding="utf-8").replace(
            "locked_by: item:chest-key", f"locked_by: {json.dumps('flag:encounter:' + HOSTILE_KEY)}"
        ),
        encoding="utf-8",
    )


HOSTILE_FOLDERS = {
    "ключ с управляющими символами": hostile_extra_key,
    "ключ в двадцать тысяч знаков": hostile_long_key,
    "длинный тег YAML": hostile_yaml_tag,
    "имена файлов с управляющими символами": hostile_file_names,
    "шестьсот битых файлов": thousands_of_problems,
    "флаг движка с управляющими символами": hostile_flag_name,
}
# Предел записи: до двадцати проблем по двести знаков с разделителями и несколько слов вокруг.
MAX_LOG_LINE = adventures.MAX_REPORTED_PROBLEMS * (MAX_PROBLEM_LENGTH + 2) + 400


@pytest.mark.parametrize("damage", HOSTILE_FOLDERS.values(), ids=HOSTILE_FOLDERS.keys())
def test_a_hostile_folder_leaves_one_short_printable_line_per_log_record(
    content_dir, caplog, damage
):
    copy_demo(content_dir)
    damage(copy_demo(content_dir, "hostile"))

    with caplog.at_level(logging.DEBUG):
        catalog = load_catalog(content_dir, load_srd())

    assert "demo-crypt" in catalog  # соседнее приключение загружено
    records = [r for r in caplog.records if r.name == adventures.__name__]
    assert records, "о чужой папке в журнале ничего нет: проверять нечего"
    for record in records:
        line = record.getMessage()
        assert line.isprintable(), repr(line[:200])  # ни \n, ни \r, ни ESC, ни разделителей строк
        assert len(line) <= MAX_LOG_LINE
        assert record.exc_info is None  # ожидаемые проблемы папки — без трассировки


def test_a_folder_with_control_characters_in_its_name_is_written_to_the_log_escaped(
    content_dir, caplog
):
    copy_demo(content_dir)
    broken = copy_demo(content_dir, "evil\nWARNING подделка\x1b[2J")
    (broken / "adventure.yaml").write_text("title: [не закрыто", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=adventures.__name__):
        load_catalog(content_dir, load_srd())

    [record] = caplog.records
    assert record.getMessage().isprintable()
    assert "\\n" in record.getMessage() and "\\x1b" in record.getMessage()


# --- повтор зоны с чужим id в отчёте валидатора ---
# Второй поток «проблем» той же папки — отчёт валидатора. Id зоны шаблона не имеет (зона входит в
# `GameState`, шаблон сломал бы сохранённые снимки), поэтому он попадает в `Issue.ref` сырым:
# перевод строки, ESC и три тысячи знаков. Нейтрализует и обрезает его движок в одной точке
# (`validator._issue`); эти тесты держат результат на всех трёх стоках сервера: записи журнала
# каталога, списке блокирующих проблем и ответе 422 создателю игры. Чинится это в движке, а не
# здесь: серверу достаточно склеить уже безопасные строки.

HOSTILE_ZONE_ID = "x\nFAKE LOG LINE\x1b[31m\u2028‮" + "Z" * 3000
# Одна блокирующая проблема — `ссылка: сообщение`, обе части с пределами движка.
MAX_BLOCKING_PROBLEM = MAX_REF_LENGTH + len(": ") + MAX_MESSAGE_LENGTH


def repeat_zone_with_hostile_id(folder: Path) -> None:
    """Две зоны крипты с одним враждебным id: валидатор находит повтор и называет id в проблеме."""
    crypt = folder / "locations" / "crypt.yaml"
    zone = f"  - id: {json.dumps(HOSTILE_ZONE_ID)}\n    name: зона-двойник\n"
    crypt.write_text(
        crypt.read_text(encoding="utf-8").replace("zones:\n", f"zones:\n{zone}{zone}", 1),
        encoding="utf-8",
    )


def test_a_repeated_zone_with_a_hostile_id_leaves_one_short_printable_log_record(
    content_dir, caplog
):
    repeat_zone_with_hostile_id(copy_demo(content_dir))

    with caplog.at_level(logging.DEBUG, logger=adventures.__name__):
        catalog = load_catalog(content_dir, load_srd())

    assert catalog["demo-crypt"].report.blocking  # папка загрузилась, а валидатор нашёл повтор
    [record] = caplog.records
    line = record.getMessage()
    assert line.isprintable(), repr(line[:200])  # ни перевода строки, ни ESC, ни U+2028
    assert len(line) <= adventures.MAX_REPORTED_PROBLEMS * (MAX_BLOCKING_PROBLEM + 2) + 400
    assert "Z" * MAX_REF_LENGTH not in line  # три тысячи знаков id в запись не доехали
    assert "\\x1b" in line  # управляющие символы записаны, а не выполнены
    assert "повторяется" in line  # причина видна, её не съел длинный id
    assert record.exc_info is None


def test_blocking_problems_of_a_repeated_zone_with_a_hostile_id_are_short_and_printable(
    content_dir,
):
    repeat_zone_with_hostile_id(copy_demo(content_dir))

    problems = blocking_problems(load_catalog(content_dir, load_srd())["demo-crypt"].report)

    assert any("повторяется" in problem for problem in problems)
    for problem in problems:
        assert problem.isprintable(), repr(problem[:200])
        assert len(problem) <= MAX_BLOCKING_PROBLEM


async def test_creator_gets_422_with_safe_problems_for_a_repeated_zone_with_a_hostile_id(
    db, settings
):
    repeat_zone_with_hostile_id(copy_demo(settings.content_dir))

    async with running_app(settings) as (_app, http):
        response = await http.post(
            "/api/games", headers=await auth_headers(db), json={"adventure_id": "demo-crypt"}
        )

    assert response.status_code == 422
    problems = response.json()["problems"]
    assert any("повторяется" in problem for problem in problems)
    for problem in problems:
        assert problem.isprintable(), repr(problem[:200])
        assert len(problem) <= MAX_BLOCKING_PROBLEM
