"""Экспорт и импорт черновика zip-архивом (план B6.1; спека §10.3; дополнение dispatcher B6 §7).

Экспорт — zip с одной папкой `<adventure_id>/` формата v2 (`dump_adventure`). Импорт — сырое тело
`application/zip` с пределами и защитой: путь вне папки, симлинк, шифрование, повтор имени, лишняя
вложенность, бомба размера (по фактическим байтам), YAML с якорями — отказ без записи и без значений
из файлов. Распаковка идёт во временную папку сервера и удаляется после; в папку контента ничего не
пишется. Экспорт → импорт даёт тот же черновик. Живых моделей нет.
"""

import io
import os
import sqlite3
import stat
import struct
import tempfile
import unicodedata
import warnings
import zipfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from test_drafts_api import (
    CREATOR_TG_ID,
    Author,
    Stand,
    assert_draft_shape,
    assert_error,
    count_drafts,
    database_file,
    generated_draft,
    put_draft,
    saved,
    served_section,
)

from dnd_engine.adventure.loader import load_adventure
from dnd_engine.adventure.schema import Clue
from dnd_engine.content import load_srd
from dnd_server.adventures import load_catalog
from dnd_server.authoring import archive, service
from dnd_server.authoring.api import DRAIN_LIMIT_BYTES
from dnd_server.authoring.archive import MAX_BODY_BYTES
from dnd_server.authoring.draft import AuthoringDraft, from_adventure, to_adventure

DEMO_DIR = Path(__file__).resolve().parents[4] / "content" / "adventures" / "demo-crypt"
MARKER = "МЕТКА-ИЗ-АРХИВА"
ASCII_MARKER = "leakmark"  # там, где формат допускает только латиницу (имя якоря YAML)
KIB = 1024
MIB = 1024 * KIB
# Поля `Draft`, которые несут разделы и настройки (инвариант «экспорт → импорт», дополнение §7).
CONTENT_FIELDS = (
    "title",
    "adventure_id",
    "journal_intro",
    "settings",
    "world",
    "deities",
    "factions",
    "plot",
    "locations",
    "npcs",
    "relations",
    "encounters",
    "monsters",
    "items",
    "revelations",
    "start_location",
    "finale_location",
    "locks",
)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch) -> Path:
    """Временная папка сервера этого теста: после импорта в ней ничего не должно остаться."""
    folder = tmp_path / "server-tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def content_of(draft: dict[str, Any]) -> dict[str, Any]:
    return {field: draft[field] for field in CONTENT_FIELDS}


async def export(stand: Stand, who: Author, draft: dict[str, Any]) -> httpx.Response:
    return await stand.call(who, "GET", f"/api/drafts/{draft['id']}/export")


async def import_zip(
    stand: Stand, who: Author, body: Any, content_type: str | None = "application/zip"
) -> httpx.Response:
    headers = {"Content-Type": content_type} if content_type else {}
    return await stand.call(who, "POST", "/api/drafts/import", content=body, headers=headers)


def zip_of(
    files: dict[str, bytes] | list[tuple[str, bytes]], compression: int = zipfile.ZIP_DEFLATED
) -> bytes:
    """Архив из файлов «имя → байты»; список пар допускает повтор имени."""
    buffer = io.BytesIO()
    pairs = files.items() if isinstance(files, dict) else files
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # повтор имени zipfile пишет с предупреждением
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for name, data in pairs:
                archive.writestr(name, data)
    return buffer.getvalue()


def demo_files(top: str | None = "demo-crypt") -> dict[str, bytes]:
    """Файлы папки демо-приключения под верхней папкой `top` (или в корне архива)."""
    return {
        (f"{top}/" if top else "") + path.relative_to(DEMO_DIR).as_posix(): path.read_bytes()
        for path in sorted(DEMO_DIR.rglob("*.yaml"))
    }


def normalized(content: AuthoringDraft) -> AuthoringDraft:
    """Черновик в собранном виде: бои локаций и находки мест уже записаны (как после импорта)."""
    return from_adventure(to_adventure(content))


# --- экспорт ---


async def test_export_is_a_zip_with_one_folder_of_the_adventure_in_format_v2(stand, tmp_path):
    author = await stand.author()
    content = await generated_draft()
    draft = await put_draft(stand, author, content)

    response = await export(stand, author, draft)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == ('attachment; filename="crow-lighthouse.zip"')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names and all(name.startswith("crow-lighthouse/") for name in names)
        assert "crow-lighthouse/adventure.yaml" in names
        archive.extractall(tmp_path / "out")
    assert load_adventure(tmp_path / "out" / "crow-lighthouse") == to_adventure(content)


async def test_a_draft_without_an_adventure_id_is_not_exported(stand):
    author = await stand.author()
    draft = await stand.new_draft(author, title="Без id")

    assert_error(await export(stand, author, draft), 422, "no_adventure_id")


async def test_errors_of_the_check_do_not_block_the_export(stand):
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(
        stand,
        author,
        draft,
        "settings",
        served_section(draft, "settings") | {"adventure_id": "raw-idea", "pitch": "Идея"},
    )

    response = await export(stand, author, draft)

    assert response.status_code == 200, response.text


async def test_a_draft_that_cannot_be_assembled_gives_export_invalid_without_input(
    stand, monkeypatch
):
    author = await stand.author()
    draft = await put_draft(stand, author, await generated_draft())

    class Strict(BaseModel):
        title: int

    def refusing(_draft):
        Strict.model_validate({"title": f"sk-LEAK {MARKER}"})

    monkeypatch.setattr(service, "to_adventure", refusing)

    response = await export(stand, author, draft)

    body = assert_error(response, 422, "export_invalid")
    assert body["problems"]
    assert MARKER not in response.text and "sk-LEAK" not in response.text


async def test_someone_elses_draft_is_not_exported(stand):
    author, other = await stand.author(), await stand.author()
    draft = await put_draft(stand, author, await generated_draft())

    assert_error(await export(stand, other, draft), 404, None)


# --- экспорт → импорт ---


async def test_export_then_import_gives_the_same_draft(stand, scratch):
    # Порядок сущностей разделов — порядок автора, а не порядок имён файлов.
    author = await stand.author()
    draft = await put_draft(stand, author, normalized(await generated_draft()))
    assert [d["id"] for d in draft["deities"]] == ["selmara", "ashen-one"]

    response = await import_zip(stand, author, (await export(stand, author, draft)).content)

    assert response.status_code == 201, response.text
    imported = response.json()
    assert_draft_shape(imported)
    assert imported["id"] != draft["id"]
    assert content_of(imported) == content_of(draft)
    assert {s: v["status"] for s, v in imported["steps"].items()} == {
        s: v["status"] for s, v in draft["steps"].items()
    }
    assert await stand.draft(author, imported["id"]) == imported
    assert list(scratch.iterdir()) == []


async def test_a_copy_of_a_folder_survives_export_and_import_unchanged(stand, scratch):
    author = await stand.author()
    stand.app.state.adventures.update(load_catalog(DEMO_DIR.parent, load_srd()))
    copy = (await stand.call(author, "POST", "/api/adventures/demo-crypt/draft")).json()

    response = await import_zip(stand, author, (await export(stand, author, copy)).content)

    assert response.status_code == 201, response.text
    assert content_of(response.json()) == content_of(copy)


async def test_an_imported_draft_assembles_into_the_same_adventure_as_the_exported_one(stand):
    # Экспорт несёт собранное приключение: бой локации и находки мест-улик в импортированном
    # черновике записаны явно, а приключение то же.
    author = await stand.author()
    content = await generated_draft()
    draft = await put_draft(stand, author, content)

    imported = (
        await import_zip(stand, author, (await export(stand, author, draft)).content)
    ).json()

    stored = await stand.draft(author, imported["id"])
    assert to_adventure(_draft_of(stored)) == to_adventure(content)


def _draft_of(served: dict[str, Any]) -> AuthoringDraft:
    """`AuthoringDraft` из ответа `Draft` (поля верхнего уровня и разделы)."""
    data = {field: served[field] for field in CONTENT_FIELDS if field != "adventure_id"}
    data["id"] = served["adventure_id"]
    data["plot"] = served["plot"] or {}
    return AuthoringDraft.model_validate(data)


async def test_an_imported_draft_takes_the_default_profanity_flag(stand):
    # У приключения нет флажка мата: копия получает умолчание Р1 (дополнение §7). Пожелания шагов
    # в архив не идут: у импортированного черновика их нет (финальное покрытие B6).
    author = await stand.author()
    content = normalized(await generated_draft())
    no_swearing = content.model_copy(
        update={
            "settings": content.settings.model_copy(update={"profanity": False}),
            "wishes": {"plot": "Побольше пепла", "encounters": "Босс у алтаря"},
        }
    )
    draft = await put_draft(stand, author, no_swearing)
    assert draft["wishes"]

    exported = await export(stand, author, draft)
    imported = (await import_zip(stand, author, exported.content)).json()

    assert imported["settings"]["profanity"] is True
    assert imported["wishes"] == {}
    with zipfile.ZipFile(io.BytesIO(exported.content)) as packed:
        texts = [packed.read(name).decode("utf-8") for name in packed.namelist()]
    assert texts and all("Побольше пепла" not in text for text in texts)


async def test_an_archive_with_the_files_in_its_root_is_imported(stand, scratch):
    author = await stand.author()

    response = await import_zip(stand, author, zip_of(demo_files(top=None)))

    assert response.status_code == 201, response.text
    assert response.json()["adventure_id"] == "demo-crypt"


async def test_an_import_with_the_id_of_a_catalog_adventure_is_a_new_draft_of_the_importer(
    stand, settings
):
    author, other = await stand.author(), await stand.author()
    stand.app.state.adventures.update(load_catalog(DEMO_DIR.parent, load_srd()))

    response = await import_zip(stand, author, zip_of(demo_files()))

    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["adventure_id"] == "demo-crypt"
    assert imported["version"] == 1
    assert_error(await stand.call(other, "GET", f"/api/drafts/{imported['id']}"), 404, None)


def snapshot(folder: Path) -> dict[Path, bytes | None]:
    """Пути папки и содержимое её файлов: запись поверх файла тоже видна."""
    return {path: path.read_bytes() if path.is_file() else None for path in folder.rglob("*")}


async def test_import_writes_nothing_into_the_content_folder(stand, scratch):
    contents = DEMO_DIR.parent
    before = snapshot(contents)
    author = await stand.author()

    accepted = await import_zip(stand, author, zip_of(demo_files()))
    refused = await import_zip(stand, author, zip_of({"../" + MARKER + ".yaml": b"x: 1"}))
    # Финальное покрытие B6: архив с теми же путями, что у папки демо, но другими файлами.
    changed = demo_files() | {"demo-crypt/world.yaml": f"history: {MARKER}\n".encode()}
    overwrite = await import_zip(stand, author, zip_of(changed))

    assert (accepted.status_code, refused.status_code) == (201, 422)
    assert overwrite.status_code in (201, 422), overwrite.text
    assert snapshot(contents) == before
    assert list(scratch.iterdir()) == []


# --- враждебные архивы: отказ без записи и без значений из файлов ---


def with_extra(name: str, data: bytes = b"id: x\n") -> bytes:
    return zip_of(demo_files() | {name: data})


def symlink_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in demo_files().items():
            archive.writestr(name, data)
        link = zipfile.ZipInfo(f"demo-crypt/locations/{MARKER}.yaml")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    return buffer.getvalue()


def _patch_headers(data: bytes, patch) -> bytes:
    """Правит заголовки всех записей архива: `patch(buffer, offset, central)`."""
    buffer = bytearray(data)
    for signature, central in ((b"PK\x03\x04", False), (b"PK\x01\x02", True)):
        start = 0
        while (offset := buffer.find(signature, start)) != -1:
            patch(buffer, offset, central)
            start = offset + 4
    return bytes(buffer)


def encrypted_archive() -> bytes:
    def mark_encrypted(buffer: bytearray, offset: int, central: bool) -> None:
        flags = offset + (8 if central else 6)
        buffer[flags] |= 0x01

    return _patch_headers(zip_of({f"demo-crypt/{MARKER}.yaml": b"x: 1"}), mark_encrypted)


def lying_size_bomb() -> bytes:
    """Запись, у которой заголовок называет 100 байт, а данных внутри 3 МиБ."""

    def shrink(buffer: bytearray, offset: int, central: bool) -> None:
        field = offset + (24 if central else 22)
        buffer[field : field + 4] = struct.pack("<I", 100)

    return _patch_headers(zip_of({"demo-crypt/adventure.yaml": b"a" * (3 * MIB)}), shrink)


def hex_text(size: int) -> bytes:
    """Текст, который сжимается примерно вдвое: случайные шестнадцатеричные цифры."""
    return os.urandom(size // 2).hex().encode()


def anchored_manifest() -> bytes:
    files = demo_files()
    manifest = files["demo-crypt/adventure.yaml"].decode()
    files["demo-crypt/adventure.yaml"] = (
        manifest.replace("title:", f"title: &{ASCII_MARKER}", 1) + f"tone: *{ASCII_MARKER}\n"
    ).encode()
    return zip_of(files)


# Враждебные архивы финального покрытия B6: имена, которые на диске совпадают, и YAML, который
# ломает разбор, а не пределы архива.


def with_world(text: bytes, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """Архив демо, где `world.yaml` заменён текстом `text`."""
    return zip_of(demo_files() | {"demo-crypt/world.yaml": text}, compression)


def same_name_in_nfc_and_nfd() -> bytes:
    # «café» двумя способами записи Unicode: для macOS это одно имя файла.
    nfc, nfd = (unicodedata.normalize(form, "café") for form in ("NFC", "NFD"))
    return zip_of(
        demo_files()
        | {
            f"demo-crypt/locations/{nfc}.yaml": f"id: {MARKER}\n".encode(),
            f"demo-crypt/locations/{nfd}.yaml": f"id: {MARKER}-2\n".encode(),
        }
    )


def inflated_packed_size() -> bytes:
    """Бомба сжатия, у которой центральный каталог завышает сжатый размер записи: по заголовку
    степень сжатия в пределе, а данных на деле в 1000 раз меньше."""
    name = "demo-crypt/locations/z.yaml"
    buffer = bytearray(zip_of(demo_files() | {name: b" " * 400 * KIB}))
    start = 0
    while (offset := buffer.find(b"PK\x01\x02", start)) != -1:
        size = struct.unpack_from("<H", buffer, offset + 28)[0]
        if buffer[offset + 46 : offset + 46 + size] == name.encode():
            struct.pack_into("<I", buffer, offset + 20, 10_000)
        start = offset + 4
    return bytes(buffer)


def without_manifest() -> bytes:
    files = demo_files()
    del files["demo-crypt/adventure.yaml"]
    return zip_of(files)


def with_method(method: int) -> bytes:
    """Архив демо, где одна маленькая обычная запись (`adventure.yaml`) сжата методом `method`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as packed:
        for name, data in demo_files().items():
            kind = method if name == "demo-crypt/adventure.yaml" else zipfile.ZIP_DEFLATED
            packed.writestr(name, data, compress_type=kind)
    return buffer.getvalue()


# Методы сжатия, кроме stored и deflate (fix-раунд 1 B6, S1): их распаковщик zipfile отдаёт весь
# результат сразу, без предела памяти. zstd — если он есть в установленном Python.
OTHER_METHODS = {"bzip2": zipfile.ZIP_BZIP2, "lzma": zipfile.ZIP_LZMA}
if hasattr(zipfile, "ZIP_ZSTANDARD"):
    try:
        import compression.zstd  # noqa: F401
    except ImportError:
        pass
    else:
        OTHER_METHODS["zstd"] = zipfile.ZIP_ZSTANDARD


HOSTILE = {
    "путь наверх": lambda: with_extra(f"../{MARKER}.yaml"),
    "путь наверх внутри": lambda: with_extra(f"demo-crypt/../../{MARKER}.yaml"),
    "абсолютный путь": lambda: with_extra(f"/tmp/{MARKER}.yaml"),
    "буква диска": lambda: with_extra(f"C:/{MARKER}.yaml"),
    "обратный слэш": lambda: with_extra(f"demo-crypt\\{MARKER}.yaml"),
    "управляющий символ": lambda: with_extra(f"demo-crypt/locations/{MARKER}\x1b[31m.yaml"),
    "симлинк": symlink_archive,
    "шифрование": encrypted_archive,
    "повтор имени": lambda: zip_of(
        [*demo_files().items(), ("demo-crypt/world.yaml", f"history: {MARKER}".encode())]
    ),
    "лишняя вложенность": lambda: with_extra(f"demo-crypt/locations/deep/{MARKER}.yaml"),
    "две верхние папки": lambda: with_extra(f"{MARKER}/adventure.yaml"),
    "слишком много записей": lambda: zip_of(
        demo_files() | {f"demo-crypt/locations/n{n}.yaml": b"x: 1" for n in range(200)}
    ),
    "файл больше предела": lambda: zip_of(
        demo_files() | {"demo-crypt/locations/big.yaml": hex_text(513 * KIB)},
        zipfile.ZIP_STORED,
    ),
    "бомба размера": lambda: with_extra("demo-crypt/locations/bomb.yaml", b"a" * (3 * MIB)),
    "заголовок врёт о размере": lying_size_bomb,
    "распакованного больше предела": lambda: zip_of(
        demo_files() | {f"demo-crypt/locations/f{n}.yaml": hex_text(450 * KIB) for n in range(5)}
    ),
    "сжатие сильнее предела": lambda: with_extra("demo-crypt/locations/z.yaml", b" " * 400 * KIB),
    "якорь YAML": anchored_manifest,
    "не zip": lambda: f"PK это не архив {MARKER}".encode(),
    # --- финальное покрытие B6 ---
    "имя отличается только регистром": lambda: with_extra(
        "demo-crypt/Locations/CRYPT.yaml", f"id: {MARKER}\n".encode()
    ),
    "одно имя в NFC и NFD": same_name_in_nfc_and_nfd,
    "папка с именем файла": lambda: with_extra("demo-crypt/world.yaml/", b""),
    "файл с именем папки": lambda: zip_of({"demo-crypt/locations": MARKER.encode()} | demo_files()),
    "алиас без якоря": lambda: with_world(f"history: *{ASCII_MARKER}\n".encode()),
    "тег python": lambda: with_world(
        f"history: !!python/object/apply:{ASCII_MARKER} []\n".encode()
    ),
    # Без сжатия: иначе отказ дал бы степень сжатия, а не разбор YAML.
    "глубокая вложенность YAML": lambda: with_world(
        b"history: " + b"[" * 20_000 + b"]" * 20_000 + b"\n", zipfile.ZIP_STORED
    ),
    "нет adventure.yaml": without_manifest,
    "сжатый размер в заголовке завышен": inflated_packed_size,
    "посторонний файл в папке сущностей": lambda: with_extra(
        "demo-crypt/locations/notes.txt", MARKER.encode()
    ),
    "пустой архив": lambda: zip_of({}),
    # --- fix-раунд 1 B6 ---
    **{
        f"сжатие {name}": (lambda method=method: with_method(method))
        for name, method in OTHER_METHODS.items()
    },
    "__MACOSX с путём наверх": lambda: with_extra(f"__MACOSX/../{MARKER}.yaml"),
    "__MACOSX сверх числа записей": lambda: zip_of(
        demo_files() | {f"__MACOSX/demo-crypt/._n{n}.yaml": b"x" for n in range(200)}
    ),
}


@pytest.mark.parametrize("kind", HOSTILE)
async def test_a_hostile_archive_is_refused_without_writing_anything(stand, scratch, kind):
    author = await stand.author()
    before = await count_drafts(stand.db)

    response = await import_zip(stand, author, HOSTILE[kind]())

    body = assert_error(response, 422, "archive_invalid")
    assert body["problems"], body
    assert MARKER not in response.text and ASCII_MARKER not in response.text
    assert await count_drafts(stand.db) == before
    assert list(scratch.iterdir()) == []


async def test_a_body_over_the_limit_is_refused_by_its_length_before_reading(stand, scratch):
    author = await stand.author()

    response = await import_zip(stand, author, b"\0" * (2 * MIB + 1))

    assert_error(response, 413, "too_large")
    assert await count_drafts(stand.db) == 0


async def test_a_streamed_body_over_the_limit_is_refused_while_reading(stand, scratch):
    author = await stand.author()
    chunk = os.urandom(256 * KIB)

    async def chunks():
        for _ in range(9):  # 2,25 МиБ без заголовка Content-Length
            yield chunk

    response = await import_zip(stand, author, chunks())

    assert_error(response, 413, "too_large")
    assert await count_drafts(stand.db) == 0


@pytest.mark.parametrize("content_type", ["application/json", "multipart/form-data", None])
async def test_a_body_that_is_not_a_zip_by_its_type_is_refused(stand, content_type):
    author = await stand.author()

    response = await import_zip(stand, author, zip_of(demo_files()), content_type)

    assert_error(response, 415, "unsupported_media")
    assert await count_drafts(stand.db) == 0


async def test_a_player_who_may_not_create_games_cannot_import(stand):
    player = await stand.author(tg_id=CREATOR_TG_ID + 1)

    response = await import_zip(stand, player, zip_of(demo_files()))

    assert response.status_code == 403, response.text
    assert await count_drafts(stand.db) == 0


async def test_an_import_problem_of_the_engine_loader_keeps_the_values_out(stand, scratch):
    # Проблема загрузчика (id файла не совпадает с именем) называет файл, но не значение id.
    files = demo_files()
    name = "demo-crypt/locations/village.yaml"
    files[name] = files[name].replace(b"id: village", f"id: v-{MARKER}".encode(), 1)
    author = await stand.author()

    response = await import_zip(stand, author, zip_of(files))

    body = assert_error(response, 422, "archive_invalid")
    assert any("locations/village.yaml" in line for line in body["problems"]), body
    assert MARKER not in response.text


async def test_the_type_check_ignores_parameters_of_the_media_type(stand, scratch):
    author = await stand.author()

    response = await import_zip(stand, author, zip_of(demo_files()), "Application/ZIP; q=1")

    assert response.status_code == 201, response.text


# --- финальное покрытие B6: тело запроса, границы пределов, формы zip, уборка и атомарность ---


async def import_raw(stand: Stand, who: Author, body: Any, **headers: str) -> httpx.Response:
    """Импорт с заголовками запроса как есть (в том числе с подделанным `Content-Length`)."""
    return await stand.call(
        who,
        "POST",
        "/api/drafts/import",
        content=body,
        headers={"Content-Type": "application/zip"} | headers,
    )


def counted(chunk: bytes, times: int, pulled: list[int]):
    """Тело потоком из `times` кусков `chunk`; `pulled` — сколько кусков сервер забрал."""

    async def chunks():
        for _ in range(times):
            pulled.append(len(chunk))
            yield chunk

    return chunks()


async def test_a_declared_length_over_the_limit_is_refused_after_the_body_is_read_out(
    stand, scratch
):
    # Заголовок обещает больше 2 МиБ: отказ 413, но сначала тело дочитывается и выбрасывается.
    # Иначе uvicorn закрыл бы соединение, пока прокси Vite ещё передаёт тело, и клиент получил бы
    # 502 вместо 413 (R14, дополнение B11 §1 п. 9). Разбора и записи нет.
    author = await stand.author()
    pulled: list[int] = []
    body = counted(zip_of(demo_files()), 1, pulled)

    response = await import_raw(stand, author, body, **{"Content-Length": str(2 * MIB + 1)})

    assert_error(response, 413, "too_large")
    assert len(pulled) == 1  # дочитано до конца
    assert await count_drafts(stand.db) == 0


async def test_a_declared_length_far_over_the_limit_is_refused_without_reading_the_body(
    stand, scratch
):
    # Дочитывать без конца сервер не станет: больше `DRAIN_LIMIT_BYTES` — отказ сразу.
    author = await stand.author()
    pulled: list[int] = []
    body = counted(zip_of(demo_files()), 1, pulled)
    declared = str(DRAIN_LIMIT_BYTES + 1)

    response = await import_raw(stand, author, body, **{"Content-Length": declared})

    assert_error(response, 413, "too_large")
    assert pulled == []
    assert DRAIN_LIMIT_BYTES == 4 * MAX_BODY_BYTES


async def test_a_declared_length_right_at_the_drain_limit_is_still_read_out(stand, scratch):
    # Граница предела дочитывания: заявлено ровно `DRAIN_LIMIT_BYTES` — тело ещё дочитывается
    # (413 без 502), на байт больше — отказ без чтения (тест выше).
    author = await stand.author()
    pulled: list[int] = []
    body = counted(zip_of(demo_files()), 1, pulled)

    response = await import_raw(stand, author, body, **{"Content-Length": str(DRAIN_LIMIT_BYTES)})

    assert_error(response, 413, "too_large")
    assert len(pulled) == 1
    assert await count_drafts(stand.db) == 0


async def test_a_streamed_body_over_the_limit_is_read_out_before_the_refusal(stand, scratch):
    author = await stand.author()
    pulled: list[int] = []

    response = await import_zip(stand, author, counted(os.urandom(256 * KIB), 12, pulled))

    assert_error(response, 413, "too_large")
    assert len(pulled) == 12  # 3 МиБ дочитаны целиком, хотя предел — 2 МиБ
    assert await count_drafts(stand.db) == 0


async def test_a_huge_streamed_body_is_read_out_only_up_to_the_drain_limit(stand, scratch):
    author = await stand.author()
    pulled: list[int] = []
    chunk = os.urandom(512 * KIB)
    times = 3 * DRAIN_LIMIT_BYTES // len(chunk)

    response = await import_zip(stand, author, counted(chunk, times, pulled))

    assert_error(response, 413, "too_large")
    assert DRAIN_LIMIT_BYTES < sum(pulled) <= DRAIN_LIMIT_BYTES + len(chunk)
    assert len(pulled) < times


async def test_a_declared_length_smaller_than_the_body_does_not_let_a_big_body_through(
    stand, scratch
):
    # Заголовок врёт в меньшую сторону: предел держит подсчёт байт при чтении.
    author = await stand.author()
    chunk = os.urandom(512 * KIB)

    async def chunks():
        for _ in range(5):  # 2,5 МиБ при заявленных 100 байтах
            yield chunk

    response = await import_raw(stand, author, chunks(), **{"Content-Length": "100"})

    assert_error(response, 413, "too_large")
    assert await count_drafts(stand.db) == 0


def archive_of_size(size: int) -> bytes:
    """Годный архив демо ровно в `size` байт: записи без сжатия, добор — четыре файла в корне
    папки приключения (загрузчик их не читает), каждый не больше 512 КиБ."""
    pads = [f"demo-crypt/pad-{n}.bin" for n in range(4)]
    empty = zip_of(demo_files() | dict.fromkeys(pads, b""), zipfile.ZIP_STORED)
    room = size - len(empty)
    sizes = [room // len(pads)] * len(pads)
    sizes[-1] += room - sum(sizes)
    assert max(sizes) <= 512 * KIB
    data = zip_of(
        demo_files() | {name: os.urandom(n) for name, n in zip(pads, sizes, strict=True)},
        zipfile.ZIP_STORED,
    )
    assert len(data) == size
    return data


def in_chunks(data: bytes):
    """Тело запроса потоком без `Content-Length`, кусками по 256 КиБ."""

    async def chunks():
        for start in range(0, len(data), 256 * KIB):
            yield data[start : start + 256 * KIB]

    return chunks()


@pytest.mark.parametrize("streamed", [False, True], ids=["с длиной", "потоком без длины"])
async def test_a_body_of_exactly_two_mib_is_imported_and_one_byte_more_is_refused(
    stand, scratch, streamed
):
    author = await stand.author()
    exact, over = archive_of_size(2 * MIB), archive_of_size(2 * MIB + 1)

    accepted = await import_zip(stand, author, in_chunks(exact) if streamed else exact)
    refused = await import_zip(stand, author, in_chunks(over) if streamed else over)

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["adventure_id"] == "demo-crypt"
    assert_error(refused, 413, "too_large")
    assert await count_drafts(stand.db) == 1
    assert list(scratch.iterdir()) == []


def archive_with_entries(count: int) -> bytes:
    """Годный архив демо ровно из `count` записей: добор — маленькие файлы в корне папки."""
    files = demo_files()
    pads = {f"demo-crypt/pad-{n}.txt": b"-" for n in range(count - len(files))}
    data = zip_of(files | pads)
    with zipfile.ZipFile(io.BytesIO(data)) as packed:
        assert len(packed.infolist()) == count
    return data


async def test_two_hundred_entries_are_imported_and_two_hundred_and_one_are_refused(stand, scratch):
    author = await stand.author()

    accepted = await import_zip(stand, author, archive_with_entries(200))
    refused = await import_zip(stand, author, archive_with_entries(201))

    assert accepted.status_code == 201, accepted.text
    body = assert_error(refused, 422, "archive_invalid")
    assert body["problems"] == ["архив: записей больше 200"]
    assert await count_drafts(stand.db) == 1


def with_world_of_size(size: int) -> bytes:
    """Архив демо, где `world.yaml` дополнен комментарием YAML до `size` байт: этот файл читает
    загрузчик, так что предел проверяют и распаковка, и чтение файла."""
    files = demo_files()
    world = files["demo-crypt/world.yaml"] + b"\n# "
    filler = size - len(world) - 1
    files["demo-crypt/world.yaml"] = world + os.urandom(filler).hex().encode()[:filler] + b"\n"
    assert len(files["demo-crypt/world.yaml"]) == size
    return zip_of(files)


async def test_a_file_of_exactly_512_kib_is_imported_and_one_byte_more_is_refused(stand, scratch):
    author = await stand.author()

    accepted = await import_zip(stand, author, with_world_of_size(512 * KIB))
    refused = await import_zip(stand, author, with_world_of_size(512 * KIB + 1))

    assert accepted.status_code == 201, accepted.text
    body = assert_error(refused, 422, "archive_invalid")
    assert any("512 КиБ" in line for line in body["problems"]), body
    assert await count_drafts(stand.db) == 1
    assert list(scratch.iterdir()) == []


def with_unpacked_total(total: int) -> bytes:
    """Архив демо, где все файлы вместе после распаковки — ровно `total` байт: добор — пять файлов
    шестнадцатеричного текста в корне папки (сжатие около двух раз, каждый не больше 512 КиБ)."""
    files = demo_files()
    room = total - sum(len(data) for data in files.values())
    sizes = [room // 5] * 5
    sizes[-1] += room - sum(sizes)
    assert max(sizes) <= 512 * KIB
    pads = {
        f"demo-crypt/pad-{n}.txt": os.urandom(size).hex().encode()[:size]
        for n, size in enumerate(sizes)
    }
    data = zip_of(files | pads)
    assert len(data) < 2 * MIB  # тело в пределе: отказ может дать только сумма распакованного
    return data


async def test_two_mib_unpacked_in_all_are_imported_and_one_byte_more_is_refused(stand, scratch):
    author = await stand.author()

    accepted = await import_zip(stand, author, with_unpacked_total(2 * MIB))
    refused = await import_zip(stand, author, with_unpacked_total(2 * MIB + 1))

    assert accepted.status_code == 201, accepted.text
    body = assert_error(refused, 422, "archive_invalid")
    assert body["problems"] == ["архив: распакованные файлы больше 2 МиБ"]
    assert await count_drafts(stand.db) == 1
    assert list(scratch.iterdir()) == []


def with_packed_entry(size: int) -> tuple[bytes, int, int]:
    """Архив демо и файл `pad.txt` из `size` букв в корне папки: (архив, размер, сжатый размер)."""
    data = zip_of(demo_files() | {"demo-crypt/pad.txt": b"a" * size})
    with zipfile.ZipFile(io.BytesIO(data)) as packed:
        entry = packed.getinfo("demo-crypt/pad.txt")
    return data, entry.file_size, entry.compress_size


def exactly_hundredfold() -> int:
    """Длина файла из одних букв, который deflate сжимает ровно в 100 раз."""
    size = 1700
    for _ in range(50):
        _data, unpacked, packed = with_packed_entry(size)
        if unpacked == 100 * packed:
            return size
        size += 100 * packed - unpacked
    pytest.fail("не нашлась длина со сжатием ровно в 100 раз")


async def test_an_entry_packed_exactly_a_hundredfold_is_imported_and_more_is_refused(
    stand, scratch
):
    author = await stand.author()
    size = exactly_hundredfold()
    exact, _unpacked, _packed = with_packed_entry(size)
    over, unpacked, packed = with_packed_entry(size + 1)
    assert unpacked > 100 * packed  # на байт больше — уже сильнее ста раз

    accepted = await import_zip(stand, author, exact)
    refused = await import_zip(stand, author, over)

    assert accepted.status_code == 201, accepted.text
    body = assert_error(refused, 422, "archive_invalid")
    assert any("сжат сильнее чем в 100 раз" in line for line in body["problems"]), body
    assert await count_drafts(stand.db) == 1


class Unseekable(io.RawIOBase):
    """Поток без перемотки: zipfile пишет в него записи с дескриптором данных (флаг 0x08), как
    потоковые архиваторы."""

    def __init__(self) -> None:
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, chunk) -> int:
        self.data += chunk
        return len(chunk)


def with_folder_entries() -> bytes:
    """Как `zip -r`: у верхней папки и у папок сущностей свои записи, есть и пустая папка."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as packed:
        written: set[str] = set()
        for folder in ("demo-crypt/", "demo-crypt/empty/"):
            packed.writestr(zipfile.ZipInfo(folder), b"")
        for name, data in demo_files().items():
            parent = name.rsplit("/", 1)[0] + "/"
            if parent != "demo-crypt/" and parent not in written:
                written.add(parent)
                packed.writestr(zipfile.ZipInfo(parent), b"")
            packed.writestr(name, data)
    return buffer.getvalue()


def with_data_descriptors() -> bytes:
    stream = Unseekable()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as packed:
        for name, data in demo_files().items():
            with packed.open(name, "w") as sink:
                sink.write(data)
    with zipfile.ZipFile(io.BytesIO(bytes(stream.data))) as check:
        assert all(entry.flag_bits & 0x08 for entry in check.infolist())
    return bytes(stream.data)


def with_zip64() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as packed:
        for name, data in demo_files().items():
            with packed.open(name, "w", force_zip64=True) as sink:
                sink.write(data)
    return buffer.getvalue()


def with_unknown_files() -> bytes:
    # Файлы, которых формат не знает, рядом с приключением; скрытый файл в папке сущностей.
    return zip_of(
        demo_files()
        | {
            "demo-crypt/README.md": MARKER.encode(),
            "demo-crypt/.DS_Store": b"\0\0\0\1Bud1",
            "demo-crypt/notes/plan.yaml": f"secret: {MARKER}\n".encode(),
            "demo-crypt/locations/.hidden.yaml": f"id: {MARKER}\n".encode(),
        }
    )


ORDINARY = {
    "записи папок (zip -r)": with_folder_entries,
    "дескриптор данных": with_data_descriptors,
    "zip64": with_zip64,
    "неизвестные файлы рядом": with_unknown_files,
}


@pytest.mark.parametrize("kind", ORDINARY)
async def test_an_ordinary_zip_of_another_archiver_gives_the_same_draft(stand, scratch, kind):
    author = await stand.author()
    plain = (await import_zip(stand, author, zip_of(demo_files()))).json()

    response = await import_zip(stand, author, ORDINARY[kind]())

    assert response.status_code == 201, response.text
    assert content_of(response.json()) == content_of(plain)
    assert MARKER not in response.text
    assert list(scratch.iterdir()) == []


async def test_export_leaves_nothing_in_the_temporary_folder(stand, scratch):
    author = await stand.author()
    draft = await put_draft(stand, author, await generated_draft())

    response = await export(stand, author, draft)

    assert response.status_code == 200, response.text
    assert list(scratch.iterdir()) == []


async def test_the_temporary_folder_is_removed_when_the_loader_fails_unexpectedly(
    stand, scratch, monkeypatch
):
    # Сбой мимо ожидаемых отказов (ошибка самого загрузчика): архив уже распакован, но после
    # запроса во временной папке ничего нет и черновика нет.
    author = await stand.author()
    unpacked: list[bool] = []

    def failing_loader(root: Path):
        unpacked.append((root / "adventure.yaml").is_file())
        raise RuntimeError("сбой загрузчика")

    monkeypatch.setattr(archive, "load_adventure", failing_loader)

    with pytest.raises(RuntimeError, match="сбой загрузчика"):
        await import_zip(stand, author, zip_of(demo_files()))

    assert unpacked == [True]
    assert list(scratch.iterdir()) == []
    assert await count_drafts(stand.db) == 0


def failing_insert(settings, table: str) -> None:
    """Любая вставка в `table` падает ошибкой базы (триггер ссылается на несуществующую таблицу)."""
    with closing(sqlite3.connect(database_file(settings))) as connection:
        connection.execute(
            f"CREATE TRIGGER fail_insert BEFORE INSERT ON {table} "
            "BEGIN INSERT INTO no_such_table VALUES (1); END"
        )
        connection.commit()


async def test_an_import_whose_draft_cannot_be_saved_leaves_no_draft_and_no_files(
    stand, settings, scratch
):
    author = await stand.author()
    failing_insert(settings, "adventure_drafts")

    with pytest.raises(Exception, match="no_such_table"):
        await import_zip(stand, author, zip_of(demo_files()))

    assert await count_drafts(stand.db) == 0
    assert list(scratch.iterdir()) == []


@asynccontextmanager
async def tolerant(stand: Stand):
    """Клиент к тому же приложению, которому сбой сервера приходит ответом 500, а не исключением."""
    transport = httpx.ASGITransport(app=stand.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


WARDEN = """\
id: crypt-warden
name: Страж склепа
name_en: Crypt Warden
cr: {cr}
xp: 450
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


@pytest.mark.parametrize("cr", [".nan", ".inf", "-.inf", "-1"])
async def test_a_custom_statblock_of_an_archive_with_a_cr_not_finite_or_negative_is_refused(
    stand, scratch, cr
):
    # Намеренно изменено в fix-раунде 1 B6 (S2, R3): раньше такой импорт давал 201, а черновик с
    # `cr: null` нельзя было сохранить назад. Теперь импорт проходит те же пределы разделов, что
    # PUT: отказ `archive_invalid` с путём поля и без значения, черновика нет.
    author = await stand.author()
    files = demo_files() | {"demo-crypt/monsters/crypt-warden.yaml": WARDEN.format(cr=cr).encode()}

    async with tolerant(stand) as http:
        imported = await http.post(
            "/api/drafts/import",
            content=zip_of(files),
            headers=author.headers | {"Content-Type": "application/zip"},
        )

    body = assert_error(imported, 422, "archive_invalid")
    assert any(line.startswith("monsters.0.cr: ") for line in body["problems"]), body
    assert all(value not in " ".join(body["problems"]) for value in ("nan", "inf", "-1"))
    assert await count_drafts(stand.db) == 0
    assert list(scratch.iterdir()) == []


# --- fix-раунд 1 B6 ---


@pytest.mark.parametrize("name", list(OTHER_METHODS))
async def test_an_entry_packed_by_another_method_is_refused_before_unpacking(
    stand, scratch, monkeypatch, name
):
    # S1: отказ до распаковки — распаковщик не вызывается вовсе.
    author = await stand.author()
    body = with_method(OTHER_METHODS[name])
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def recording_open(self, entry, *args, **kwargs):
        opened.append(getattr(entry, "filename", entry))
        return real_open(self, entry, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", recording_open)
    files = list(demo_files())

    response = await import_zip(stand, author, body)

    refusal = assert_error(response, 422, "archive_invalid")
    number = files.index("demo-crypt/adventure.yaml") + 1
    assert refusal["problems"] == [f"архив, запись {number}: метод сжатия не поддерживается"]
    assert opened == []
    assert await count_drafts(stand.db) == 0


async def test_stored_and_deflated_entries_are_imported(stand, scratch):
    author = await stand.author()

    stored = await import_zip(stand, author, zip_of(demo_files(), zipfile.ZIP_STORED))
    deflated = await import_zip(stand, author, zip_of(demo_files(), zipfile.ZIP_DEFLATED))

    assert (stored.status_code, deflated.status_code) == (201, 201)
    assert content_of(stored.json()) == content_of(deflated.json())


def with_long_notes(length: int) -> bytes:
    """Архив демо, где заметки мастера деревни длиной `length` знаков (файл меньше 512 КиБ)."""
    files = demo_files()
    name = "demo-crypt/locations/village.yaml"
    text = files[name].decode()
    files[name] = (text + f"dm_notes: {MARKER}{'я' * (length - len(MARKER))}\n").encode()
    return zip_of(files)


async def test_an_archive_over_the_limits_of_a_section_is_refused_with_the_path(stand, scratch):
    # S2: импорт проходит те же пределы разделов, что ручная запись; в отказе путь и предел, без
    # значения поля.
    author = await stand.author()

    response = await import_zip(stand, author, with_long_notes(20_000))

    body = assert_error(response, 422, "archive_invalid")
    village = next(
        n
        for n, loc in enumerate(from_adventure(load_adventure(DEMO_DIR)).locations)
        if loc.id == "village"
    )
    assert f"locations.locations.{village}.dm_notes: не длиннее 2000 знаков" in body["problems"]
    assert MARKER not in response.text
    assert await count_drafts(stand.db) == 0
    assert list(scratch.iterdir()) == []


async def test_an_archive_at_the_limits_of_a_section_is_imported(stand, scratch):
    author = await stand.author()

    response = await import_zip(stand, author, with_long_notes(2000))

    assert response.status_code == 201, response.text


async def test_a_draft_without_a_start_and_a_finale_survives_export_and_import(stand, scratch):
    # Экспорт не блокируют ошибки проверки: в файлах нет старта и финала — пустые строки формата
    # приключения; импорт пишет их как «не выбран» (null), и раздел «Карта» проходит пределы.
    author = await stand.author()
    draft = await stand.new_draft(author)
    draft = await saved(
        stand,
        author,
        draft,
        "settings",
        served_section(draft, "settings") | {"adventure_id": "raw-idea", "pitch": "Идея"},
    )

    response = await import_zip(stand, author, (await export(stand, author, draft)).content)

    assert response.status_code == 201, response.text
    imported = response.json()
    assert (imported["start_location"], imported["finale_location"]) == (None, None)
    again = await stand.put_section(
        author, imported, "locations", served_section(imported, "locations")
    )
    assert again.status_code == 200, again.text


def finder_files() -> dict[str, bytes]:
    """Архив Finder на macOS: папка приключения и служебная `__MACOSX/` с файлами AppleDouble."""
    files = demo_files()
    return files | {
        "__MACOSX/": b"",
        "__MACOSX/demo-crypt/": b"",
        **{
            "__MACOSX/" + name.rpartition("/")[0] + "/._" + name.rpartition("/")[2]: MARKER.encode()
            for name in files
        },
    }


async def test_an_archive_made_by_finder_is_imported_like_the_plain_one(stand, scratch):
    # R4: `__MACOSX/` не распаковывается и не мешает снять верхнюю папку приключения.
    author = await stand.author()

    finder = await import_zip(stand, author, zip_of(finder_files()))
    plain = await import_zip(stand, author, zip_of(demo_files()))

    assert finder.status_code == 201, finder.text
    assert content_of(finder.json()) == content_of(plain.json())
    assert MARKER not in finder.text
    assert list(scratch.iterdir()) == []


async def test_a_declared_length_of_unicode_digits_is_ignored_and_the_body_is_counted(
    stand, scratch
):
    # R13/I2: «³» — цифра для `str.isdigit`, но не число для `int`: заголовок не роняет импорт.
    author = await stand.author()
    body = zip_of(demo_files())

    async def chunks():
        yield body

    # Байт 0xB3 Starlette читает как latin-1 — «³»; так заголовок приходит и от сервера ASGI.
    response = await import_raw(
        stand, author, chunks(), **{"Content-Length": "³".encode("latin-1")}
    )

    assert response.status_code == 201, response.text


# --- fix-раунд 1 B6, дополнение: находки-улики места не упираются в предел находок ---


def crowded_cupboard(content: AuthoringDraft) -> AuthoringDraft:
    """Демо v2, где в тайнике смотрителя (`keeper-cupboard`, внутри ключ) лежат ещё две улики:
    сборка допишет месту две находки-улики сверх находки-предмета."""
    extra = (
        Clue(id="clue-cupboard-a", description="Сажа на дверце.", where="feature:keeper-cupboard"),
        Clue(id="clue-cupboard-b", description="Пепел в замке.", where="feature:keeper-cupboard"),
    )
    first, *rest = content.revelations
    return content.model_copy(
        update={"revelations": (first.model_copy(update={"clues": first.clues + extra}), *rest)}
    )


def cupboard_finds(draft: dict[str, Any]) -> list[str]:
    [lighthouse] = [loc for loc in draft["locations"] if loc["id"] == "lighthouse"]
    [found] = [f["reveals"] for f in lighthouse["features"] if f["id"] == "keeper-cupboard"]
    return [f"{reveal['kind']}:{reveal['id']}" for reveal in found]


async def test_a_place_with_a_hidden_item_and_two_clues_survives_export_and_import(stand, scratch):
    author = await stand.author()
    assembled = normalized(crowded_cupboard(await generated_draft()))
    draft = await put_draft(stand, author, assembled)
    assert cupboard_finds(draft) == [
        "item:lamp-key",
        "clue:clue-cupboard-a",
        "clue:clue-cupboard-b",
    ]

    response = await import_zip(stand, author, (await export(stand, author, draft)).content)

    assert response.status_code == 201, response.text
    assert content_of(response.json()) == content_of(draft)


async def test_an_unassembled_place_with_two_clues_survives_export_and_import(stand, scratch):
    author = await stand.author()
    content = crowded_cupboard(await generated_draft())
    draft = await put_draft(stand, author, content)

    imported = await import_zip(stand, author, (await export(stand, author, draft)).content)

    assert imported.status_code == 201, imported.text
    assert to_adventure(
        _draft_of(await stand.draft(author, imported.json()["id"]))
    ) == to_adventure(content)
