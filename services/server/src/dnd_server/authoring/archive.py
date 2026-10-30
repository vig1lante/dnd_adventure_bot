"""Приключение zip-архивом: экспорт и разбор присланного архива.

`export_zip` пишет приключение папкой формата v2 (`dump_adventure` движка) во временную папку и
складывает её в zip с одной верхней папкой `<id приключения>/`. Файлы сущностей идут в порядке
разделов: импорт по нему восстанавливает порядок, который выбрал автор (загрузчик читает файлы по
порядку имён).

`read_adventure` — присланный архив, то есть чужой ввод. До распаковки проверяется каждая запись:
путь только внутри папки (без `..`, абсолютного пути, буквы диска и обратного слэша), не ссылка, не
зашифрована, сжата только без сжатия или deflate, имена не повторяются, вложенность — не глубже
папки сущностей; допускается одна верхняя папка (она снимается) или файлы прямо в корне архива.
Служебная папка Finder `__MACOSX/` проходит те же проверки, но не распаковывается. Распаковка идёт
во временную папку сервера с пределами по фактически распакованным байтам (заголовок zip может
врать), папка удаляется после. Дальше — загрузчик движка (`load_adventure`: без якорей и алиасов
YAML, с пределом файла). В `content/adventures` ничего не пишется. Отказ — `ArchiveInvalid` со
строками проблем без значений из файлов: архивные проблемы называют номер записи, а не её имя, у
проблем загрузчика значения в кавычках заменены.
"""

import io
import re
import stat
import struct
import tempfile
import zipfile
import zlib
from collections import Counter
from pathlib import Path

from dnd_engine.adventure.loader import (
    ENTITY_FOLDERS,
    AdventureFormatError,
    dump_adventure,
    load_adventure,
)
from dnd_engine.adventure.schema import Adventure
from dnd_engine.content import MAX_YAML_BYTES
from dnd_server.adventures import shorten_problems

KIB = 1024
MIB = 1024 * KIB
# Пределы присланного архива.
MAX_BODY_BYTES = 2 * MIB
MAX_ENTRIES = 200
MAX_FILE_BYTES = MAX_YAML_BYTES  # как у файла папки приключения
MAX_UNPACKED_BYTES = 2 * MIB
MAX_RATIO = 100  # распакованное / сжатое у записи
# Часть пути записи: id сущности (до 64 знаков) с «.yaml» помещается, имя файла ОС — тоже.
MAX_NAME_PART = 100
_CHUNK = 64 * KIB
# Глубина пути внутри папки приключения: файл в корне или в папке сущностей.
_MAX_DEPTH = 2
_DRIVE = re.compile(r"^[A-Za-z]:")
# Методы сжатия записей. Распаковщик zipfile ограничивает выход по кускам только у deflate; bzip2,
# lzma и zstd отдают весь результат прочитанного куска сразу, и предел байтов сработал бы после
# того, как он уже в памяти.
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
# Служебная папка архиватора Finder (файлы AppleDouble `._*`): приключению она не нужна.
_FINDER_FOLDER = "__MACOSX"
# Значение в кавычках в проблеме загрузчика (id, ключ, символ YAML) — оно из присланного файла.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# Что бросает чтение испорченного или подделанного архива и его записей: zipfile на чужом вводе
# падает не только `BadZipFile` (CRC, подмена заголовка, неизвестное сжатие, обрезанные данные).
_BROKEN = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
    struct.error,
    FileExistsError,
    NotADirectoryError,
    IsADirectoryError,
)


class ArchiveInvalid(Exception):
    """Архив отвергнут: `problems` — строки причин без значений из файлов. Не `ValueError`: его
    ловят как признак повреждённой записи (`_BROKEN`)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("архив не подходит")
        self.problems = shorten_problems(problems)


# --- экспорт ---


def export_zip(adventure: Adventure) -> bytes:
    """zip с одной папкой `<id>/` формата v2; файлы сущностей — в порядке разделов приключения."""
    order = {
        f"{folder}/{ident}.yaml": number
        for number, (folder, ident) in enumerate(
            (folder, ident) for folder in ENTITY_FOLDERS for ident in getattr(adventure, folder)
        )
    }
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary) / adventure.id
        dump_adventure(adventure, folder)
        names = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*.yaml"))
        # Файлы корня (`adventure.yaml`, мир, связи, открытия) — первыми, сущности — по разделам.
        names.sort(key=lambda name: order.get(name, -1))
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                archive.write(folder / name, f"{adventure.id}/{name}")
    return buffer.getvalue()


# --- импорт ---


def read_adventure(data: bytes) -> Adventure:
    """Приключение из присланного архива; порядок сущностей — порядок их файлов в архиве."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _BROKEN:
        raise ArchiveInvalid(["архив: это не zip-архив или он повреждён"]) from None
    with archive, tempfile.TemporaryDirectory() as temporary:
        files = _checked_files(archive)
        root = Path(temporary) / "adventure"
        root.mkdir()
        _unpack(archive, files, root)
        try:
            adventure = load_adventure(root)
        except AdventureFormatError as error:
            raise ArchiveInvalid([_QUOTED.sub("«…»", line) for line in error.problems]) from None
    return _in_order(adventure, ["/".join(path) for _number, _info, path in files])


type _File = tuple[int, zipfile.ZipInfo, tuple[str, ...]]


def _checked_files(archive: zipfile.ZipFile) -> list[_File]:
    """Файлы архива (номер записи, запись, путь внутри папки приключения) после всех проверок."""
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise ArchiveInvalid([f"архив: записей больше {MAX_ENTRIES}"])
    found = [
        f"архив, запись {number}: {reason}"
        for number, entry in enumerate(entries, start=1)
        if (reason := _entry_problem(entry)) is not None
    ]
    # Регистр букв не в счёт: на диске без различия регистра одно имя затёрло бы другое.
    names = Counter(entry.filename.rstrip("/").casefold() for entry in entries)
    if any(count > 1 for count in names.values()):
        found.append("архив: имена записей повторяются")
    if found:
        raise ArchiveInvalid(found)
    paths = [tuple(entry.filename.rstrip("/").split("/")) for entry in entries]
    # `__MACOSX/` уже проверена со всеми записями (имена, число), но не распаковывается и не мешает
    # снять верхнюю папку приключения.
    files = [
        (n, e, p)
        for n, (e, p) in enumerate(zip(entries, paths, strict=True), 1)
        if p[0] != _FINDER_FOLDER
    ]
    if _has_top_folder(files):
        files = [(number, entry, path[1:]) for number, entry, path in files]
    deep = [
        f"архив, запись {number}: лишняя вложенность — файлы лежат в папке приключения или в "
        "папках его сущностей"
        for number, entry, path in files
        if len(path) > (_MAX_DEPTH - 1 if entry.is_dir() else _MAX_DEPTH)
    ]
    if deep:
        raise ArchiveInvalid(deep)
    return [(number, entry, path) for number, entry, path in files if not entry.is_dir()]


def _entry_problem(entry: zipfile.ZipInfo) -> str | None:
    """Почему запись нельзя распаковать; `None` — можно. Имя записи в причину не идёт."""
    name = entry.filename
    parts = name.rstrip("/").split("/")
    if not name.isprintable():
        return "в имени есть непечатаемые символы"
    if "\\" in name:
        return "обратный слэш в пути"
    if name.startswith("/") or _DRIVE.match(name):
        return "абсолютный путь"
    if any(part in ("", ".", "..") for part in parts):
        return "путь выходит за папку приключения"
    if any(len(part) > MAX_NAME_PART for part in parts):
        return f"часть пути длиннее {MAX_NAME_PART} знаков"
    if stat.S_ISLNK(entry.external_attr >> 16):
        return "символическая ссылка"
    if entry.flag_bits & 0x1:
        return "запись зашифрована"
    if entry.compress_type not in _METHODS:
        return "метод сжатия не поддерживается"
    return None


def _has_top_folder(files: list[_File]) -> bool:
    """Все записи лежат в одной верхней папке (её архиватор кладёт сам, и она снимается)."""
    tops = {path[0] for _number, _entry, path in files}
    return len(tops) == 1 and all(len(path) > 1 or entry.is_dir() for _number, entry, path in files)


def _unpack(archive: zipfile.ZipFile, files: list[_File], root: Path) -> None:
    """Распаковывает файлы в `root`, считая фактические байты: предел файла, всей распаковки и
    степени сжатия срабатывает посреди чтения, а не после него."""
    total = 0
    for number, entry, path in files:
        where = f"архив, запись {number}"
        target = root.joinpath(*path)
        if not target.resolve().is_relative_to(root.resolve()):  # защита в глубину
            raise ArchiveInvalid([f"{where}: путь выходит за папку приключения"])
        ratio_limit = MAX_RATIO * max(entry.compress_size, 1)
        written = 0
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("xb") as sink:
                while chunk := source.read(_CHUNK):
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_FILE_BYTES:
                        raise ArchiveInvalid([f"{where}: файл больше {MAX_FILE_BYTES // KIB} КиБ"])
                    if written > ratio_limit:
                        raise ArchiveInvalid([f"{where}: сжат сильнее чем в {MAX_RATIO} раз"])
                    if total > MAX_UNPACKED_BYTES:
                        raise ArchiveInvalid(
                            [f"архив: распакованные файлы больше {MAX_UNPACKED_BYTES // MIB} МиБ"]
                        )
                    sink.write(chunk)
        except _BROKEN:
            raise ArchiveInvalid([f"{where}: запись повреждена и не распаковывается"]) from None


def _in_order(adventure: Adventure, names: list[str]) -> Adventure:
    """Приключение, где сущности коллекций идут в порядке своих файлов в архиве."""
    position = {name: number for number, name in enumerate(names)}
    return adventure.model_copy(
        update={
            folder: dict(
                sorted(
                    getattr(adventure, folder).items(),
                    key=lambda item, folder=folder: position.get(f"{folder}/{item[0]}.yaml", 0),
                )
            )
            for folder in ENTITY_FOLDERS
        }
    )
