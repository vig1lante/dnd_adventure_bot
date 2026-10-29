// Отказ импорта архива словами автора (manual B6, nit 1–2). Сервер называет запись архива
// номером («архив, запись 3: …»), а загрузчик — путём файла внутри папки приключения
// («items/<имя>.yaml: …»). Путь заменяется тем же номером записи (имя файла — данные архива),
// одинаковые причины собираются в одну строку.

const DIRECTORY_SIGNATURE = 0x02014b50;
const END_SIGNATURE = 0x06054b50;
const END_SIZE = 22;
const MAX_COMMENT = 0xffff;
// Флаг записи zip: имя в UTF-8; без него сервер читает имя как cp437 (у латиницы — то же).
const UTF8_NAME = 0x800;
// Служебная папка архиватора Finder: сервер её не распаковывает, но записи в ней нумерует.
const FINDER_FOLDER = "__MACOSX";

// Папки сущностей приключения (загрузчик движка, `ENTITY_FOLDERS`) — видом сущности.
const ENTITY_KINDS: Record<string, string> = {
  locations: "локация",
  npcs: "персонаж",
  items: "предмет",
  encounters: "бой",
  monsters: "монстр",
  deities: "бог",
  factions: "фракция",
};

/**
 * Имена записей zip по порядку центрального каталога — тот же порядок и та же нумерация с
 * единицы, что у сервера. Не zip или каталог не читается (zip64, обрезанный файл) — `null`.
 */
export async function archiveEntryNames(
  archive: Blob,
): Promise<string[] | null> {
  if (archive.size < END_SIZE) return null;
  const tailStart = Math.max(0, archive.size - END_SIZE - MAX_COMMENT);
  const tail = new DataView(await archive.slice(tailStart).arrayBuffer());
  let end = -1;
  for (let at = tail.byteLength - END_SIZE; at >= 0; at--) {
    if (tail.getUint32(at, true) === END_SIGNATURE) {
      end = at;
      break;
    }
  }
  if (end < 0) return null;
  const count = tail.getUint16(end + 10, true);
  const size = tail.getUint32(end + 12, true);
  const offset = tail.getUint32(end + 16, true);
  if (offset + size > tailStart + end) return null;
  const directory = new DataView(
    await archive.slice(offset, offset + size).arrayBuffer(),
  );
  const names: string[] = [];
  let at = 0;
  for (let index = 0; index < count; index++) {
    if (
      at + 46 > directory.byteLength ||
      directory.getUint32(at, true) !== DIRECTORY_SIGNATURE
    ) {
      return null;
    }
    const flags = directory.getUint16(at + 8, true);
    const nameLength = directory.getUint16(at + 28, true);
    const extraLength = directory.getUint16(at + 30, true);
    const commentLength = directory.getUint16(at + 32, true);
    const name = new Uint8Array(
      directory.buffer,
      directory.byteOffset + at + 46,
      Math.min(nameLength, directory.byteLength - at - 46),
    );
    // Без флага UTF-8 имя не латиницей у сервера другое (cp437): тогда путь просто не совпадёт.
    names.push(
      new TextDecoder(flags & UTF8_NAME ? "utf-8" : "latin1").decode(name),
    );
    at += 46 + nameLength + extraLength + commentLength;
  }
  return names;
}

/**
 * Номер записи (с единицы) по пути файла внутри папки приключения — как у сервера: записи
 * `__MACOSX/` нумеруются, но верхнюю папку не задают; одна общая верхняя папка снимается.
 */
function entryNumbers(entries: readonly string[]): Map<string, number> {
  const paths = entries.map((name) => name.replace(/\/+$/, "").split("/"));
  const own = entries
    .map((name, index) => ({ isDir: name.endsWith("/"), path: paths[index] }))
    .filter(({ path }) => path[0] !== FINDER_FOLDER);
  const tops = new Set(own.map(({ path }) => path[0]));
  const topFolder =
    tops.size === 1 && own.every(({ isDir, path }) => path.length > 1 || isDir);
  const numbers = new Map<string, number>();
  entries.forEach((name, index) => {
    const path = paths[index];
    if (name.endsWith("/") || path[0] === FINDER_FOLDER) return;
    numbers.set((topFolder ? path.slice(1) : path).join("/"), index + 1);
  });
  return numbers;
}

/** Строка загрузчика с путём файла сущности — номером записи: «архив, запись 4 (предмет): …». */
function withEntryNumber(line: string, numbers: Map<string, number>): string {
  for (const [path, number] of numbers) {
    const folder = path.split("/")[0];
    // Имя папки — из выбранного архива: служебные свойства объекта («constructor») не виды.
    const kind = Object.hasOwn(ENTITY_KINDS, folder)
      ? ENTITY_KINDS[folder]
      : undefined;
    const rest = line.slice(path.length);
    if (
      kind !== undefined &&
      line.startsWith(path) &&
      (rest.startsWith(":") || rest.startsWith(","))
    ) {
      return `архив, запись ${number} (${kind})${rest}`;
    }
  }
  return line;
}

/** Номера записей коротко: 1–18; 2, 5–7; пара соседних — через запятую. */
function numbersText(numbers: readonly number[]): string {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b);
  const runs: number[][] = [];
  for (const number of sorted) {
    const run = runs.at(-1);
    if (run !== undefined && run.at(-1) === number - 1) run.push(number);
    else runs.push([number]);
  }
  return runs
    .map((run) => (run.length > 2 ? `${run[0]}–${run.at(-1)}` : run.join(", ")))
    .join(", ");
}

const NUMBERED = /^архив, запись (\d+)(.*)$/s;

/**
 * Строки отказа архива для автора: путь файла загрузчика — номером записи (`entries` — имена
 * записей присланного zip; `null` — архив не прочитан, пути остаются), одна строка на
 * повторяющуюся причину в порядке первой встречи.
 */
export function archiveProblems(
  lines: readonly string[],
  entries: readonly string[] | null,
): string[] {
  const numbers = entries === null ? null : entryNumbers(entries);
  const grouped = new Map<string, number[]>();
  const result: (string | { reason: string })[] = [];
  for (const raw of lines) {
    const line = numbers === null ? raw : withEntryNumber(raw, numbers);
    const match = NUMBERED.exec(line);
    if (match === null) {
      result.push(line);
      continue;
    }
    const [, number, reason] = match;
    const group = grouped.get(reason);
    if (group === undefined) {
      grouped.set(reason, [Number(number)]);
      result.push({ reason });
    } else {
      group.push(Number(number));
    }
  }
  return result.map((item) => {
    if (typeof item === "string") return item;
    const group = grouped.get(item.reason) ?? [];
    const label = new Set(group).size > 1 ? "записи" : "запись";
    return `архив, ${label} ${numbersText(group)}${item.reason}`;
  });
}
