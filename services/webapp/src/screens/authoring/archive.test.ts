import { archiveEntryNames, archiveProblems } from "./archive";

/**
 * Архив zip из одного центрального каталога с этими именами и конца каталога: читателю имён
 * больше ничего не нужно (данных записей он не читает). `utf8` — флаг имени в UTF-8.
 */
function zipOf(names: readonly string[], utf8 = true): Blob {
  const encoded = names.map((name) => new TextEncoder().encode(name));
  const directorySize = encoded.reduce(
    (sum, name) => sum + 46 + name.length,
    0,
  );
  const bytes = new Uint8Array(directorySize + 22);
  const view = new DataView(bytes.buffer);
  let at = 0;
  for (const name of encoded) {
    view.setUint32(at, 0x02014b50, true);
    view.setUint16(at + 8, utf8 ? 0x800 : 0, true);
    view.setUint16(at + 28, name.length, true);
    bytes.set(name, at + 46);
    at += 46 + name.length;
  }
  view.setUint32(at, 0x06054b50, true);
  view.setUint16(at + 8, names.length, true);
  view.setUint16(at + 10, names.length, true);
  view.setUint32(at + 12, directorySize, true);
  view.setUint32(at + 16, 0, true);
  return new Blob([bytes]);
}

describe("имена записей zip", () => {
  test("по порядку центрального каталога, в том числе папки", async () => {
    expect(
      await archiveEntryNames(
        zipOf(["crow/", "crow/adventure.yaml", "crow/items/ключ.yaml"]),
      ),
    ).toEqual(["crow/", "crow/adventure.yaml", "crow/items/ключ.yaml"]);
  });

  test("не zip или обрезанный каталог — имён нет", async () => {
    expect(await archiveEntryNames(new Blob(["это не архив"]))).toBeNull();
    expect(await archiveEntryNames(new Blob([]))).toBeNull();
  });
});

describe("отказ архива: одна строка на повторяющуюся причину", () => {
  test("18 записей с одной причиной — одна строка с диапазоном номеров", () => {
    const lines = Array.from(
      { length: 18 },
      (_, index) =>
        `архив, запись ${index + 1}: метод сжатия не поддерживается`,
    );

    expect(archiveProblems(lines, null)).toEqual([
      "архив, записи 1–18: метод сжатия не поддерживается",
    ]);
  });

  test("разные причины — строка на каждую в порядке первой встречи; одиночные номера через запятую", () => {
    expect(
      archiveProblems(
        [
          "архив, запись 2: абсолютный путь",
          "архив, запись 3: символическая ссылка",
          "архив, запись 5: абсолютный путь",
          "архив, запись 6: абсолютный путь",
          "архив, запись 7: абсолютный путь",
          "архив: имена записей повторяются",
          "…и ещё 2",
        ],
        null,
      ),
    ).toEqual([
      "архив, записи 2, 5–7: абсолютный путь",
      "архив, запись 3: символическая ссылка",
      "архив: имена записей повторяются",
      "…и ещё 2",
    ]);
  });
});

describe("отказ загрузчика: номер записи архива вместо пути файла", () => {
  const ENTRIES = [
    "crow/",
    "crow/adventure.yaml",
    "crow/items/",
    "crow/items/Тайна Мастера zzz.yaml",
    "crow/npcs/marta.yaml",
  ];

  test("файл сущности — номер записи и вид сущности; файлы корня — как есть", () => {
    expect(
      archiveProblems(
        [
          "items/Тайна Мастера zzz.yaml: id «…» не совпадает с именем файла",
          "items/Тайна Мастера zzz.yaml, поле name: Field required",
          "npcs/marta.yaml: ожидался объект (словарь)",
          "adventure.yaml, поле title: Field required",
        ],
        ENTRIES,
      ),
    ).toEqual([
      "архив, запись 4 (предмет): id «…» не совпадает с именем файла",
      "архив, запись 4 (предмет), поле name: Field required",
      "архив, запись 5 (персонаж): ожидался объект (словарь)",
      "adventure.yaml, поле title: Field required",
    ]);
  });

  test("файлы прямо в корне архива и служебная папка Finder: номера — как у сервера", () => {
    expect(
      archiveProblems(
        ["items/key.yaml: ожидался объект (словарь)"],
        [
          "__MACOSX/",
          "__MACOSX/._adventure.yaml",
          "adventure.yaml",
          "items/key.yaml",
        ],
      ),
    ).toEqual(["архив, запись 4 (предмет): ожидался объект (словарь)"]);
  });

  test("одинаковая причина у нескольких файлов — одна строка", () => {
    expect(
      archiveProblems(
        [
          "npcs/a.yaml: ожидался объект (словарь)",
          "npcs/b.yaml: ожидался объект (словарь)",
        ],
        ["adventure.yaml", "npcs/a.yaml", "npcs/b.yaml"],
      ),
    ).toEqual(["архив, записи 2, 3 (персонаж): ожидался объект (словарь)"]);
  });

  test("папка с именем служебного свойства объекта (constructor, __proto__) — не папка сущностей: путь как прислал сервер", () => {
    for (const folder of ["constructor", "__proto__", "toString"]) {
      const line = `${folder}/a.yaml: ожидался объект (словарь)`;

      expect(
        archiveProblems([line], ["adventure.yaml", `${folder}/a.yaml`]),
        folder,
      ).toEqual([line]);
    }
  });

  test("имён нет или файла нет среди записей — путь как прислал сервер", () => {
    const line = "items/key.yaml: ожидался объект (словарь)";

    expect(archiveProblems([line], null)).toEqual([line]);
    expect(archiveProblems([line], ["adventure.yaml"])).toEqual([line]);
  });
});

// --- граничные случаи архива ---

/** Архив `zipOf` с комментарием архива после конца каталога (его пишут некоторые архиваторы). */
function withComment(archive: Uint8Array, comment: string): Blob {
  const text = new TextEncoder().encode(comment);
  const bytes = new Uint8Array(archive.length + text.length);
  bytes.set(archive);
  new DataView(bytes.buffer).setUint16(archive.length - 2, text.length, true);
  bytes.set(text, archive.length);
  return new Blob([bytes]);
}

const bytesOf = async (blob: Blob) => new Uint8Array(await blob.arrayBuffer());

describe("имена записей zip: граничные случаи", () => {
  test("пустой архив (каталог без записей) — пустой список, а не «не zip»", async () => {
    expect(await archiveEntryNames(zipOf([]))).toEqual([]);
  });

  test("комментарий архива после конца каталога не мешает найти записи", async () => {
    const archive = withComment(
      await bytesOf(zipOf(["crow/", "crow/adventure.yaml"])),
      "сделано архиватором",
    );

    expect(await archiveEntryNames(archive)).toEqual([
      "crow/",
      "crow/adventure.yaml",
    ]);
  });

  test("имя без флага UTF-8 читается побайтно (латиница — как у сервера)", async () => {
    expect(
      await archiveEntryNames(zipOf(["crow/items/key.yaml"], false)),
    ).toEqual(["crow/items/key.yaml"]);
  });

  test("каталог указывает за конец архива — архив повреждён, имён нет", async () => {
    const bytes = await bytesOf(zipOf(["adventure.yaml"]));
    const view = new DataView(bytes.buffer);
    // Смещение каталога в конце каталога (EOCD + 16) — далеко за пределами файла.
    view.setUint32(bytes.length - 22 + 16, 10_000, true);

    expect(await archiveEntryNames(new Blob([bytes]))).toBeNull();
  });

  test("запись каталога без своей подписи — архив повреждён, имён нет", async () => {
    const bytes = await bytesOf(zipOf(["adventure.yaml", "items/key.yaml"]));
    // Портим подпись второй записи каталога (первая занимает 46 байт и имя).
    new DataView(bytes.buffer).setUint32(46 + "adventure.yaml".length, 0, true);

    expect(await archiveEntryNames(new Blob([bytes]))).toBeNull();
  });

  test("конец каталога обещает больше записей, чем в каталоге, — имён нет", async () => {
    const bytes = await bytesOf(zipOf(["adventure.yaml"]));
    new DataView(bytes.buffer).setUint16(bytes.length - 22 + 10, 5, true);

    expect(await archiveEntryNames(new Blob([bytes]))).toBeNull();
  });
});

describe("отказ архива: неизвестные пути и пустой отказ", () => {
  test("пустой список строк — пусто", () => {
    expect(archiveProblems([], ["adventure.yaml"])).toEqual([]);
    expect(archiveProblems([], null)).toEqual([]);
  });

  test("файл есть в архиве, но лежит не в папке сущностей — путь как прислал сервер", () => {
    const line = "maps/old.yaml: ожидался объект (словарь)";

    expect(
      archiveProblems([line], ["adventure.yaml", "maps/old.yaml"]),
    ).toEqual([line]);
  });

  test("путь — только начало имени другого файла: не подменяется", () => {
    const line = "items/key.yaml.bak: лишний файл";

    expect(
      archiveProblems([line], ["adventure.yaml", "items/key.yaml"]),
    ).toEqual([line]);
  });

  test("две верхние папки — общей папки нет, пути считаются от корня архива", () => {
    expect(
      archiveProblems(
        [
          "items/key.yaml: ожидался объект (словарь)",
          "crow/items/key.yaml: ожидался объект (словарь)",
        ],
        ["crow/adventure.yaml", "crow/items/key.yaml", "other/readme.txt"],
      ),
    ).toEqual([
      "items/key.yaml: ожидался объект (словарь)",
      "crow/items/key.yaml: ожидался объект (словарь)",
    ]);
  });

  test("номер записи от сервера и путь загрузчика: вид сущности — часть причины, строки не сливаются", () => {
    expect(
      archiveProblems(
        [
          "архив, запись 1: ожидался объект (словарь)",
          "items/key.yaml: ожидался объект (словарь)",
        ],
        ["adventure.yaml", "items/key.yaml"],
      ),
    ).toEqual([
      "архив, запись 1: ожидался объект (словарь)",
      "архив, запись 2 (предмет): ожидался объект (словарь)",
    ]);
  });

  test("единственная верхняя папка снимается, даже если зовётся как папка сущностей (как _has_top_folder сервера)", () => {
    // Сервер снимет «items/» как папку приключения, и загрузчик назовёт файл «key.yaml».
    expect(
      archiveProblems(
        ["key.yaml: ожидался объект (словарь)"],
        ["items/", "items/key.yaml"],
      ),
    ).toEqual(["key.yaml: ожидался объект (словарь)"]);
  });
});
