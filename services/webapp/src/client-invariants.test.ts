// Инварианты браузерного кода, которые проверяются по исходникам целиком: новые модули
// (ui/TabBar, ui/Toast, ui/Settings, theme/*, конструктор, превью) не попадают в списки
// отдельных тестов экранов, поэтому охранник смотрит на весь клиент сразу.
//
// 1. Хранилище браузера: токен сессии и initData не попадают в
//    хранилище. В localStorage лежат только настройки интерфейса этого телефона (размер текста,
//    контраст, вибрация) и отметки прочтения дневника. Новый ключ или новый модуль с хранилищем —
//    осознанное решение, которое должно поменять этот список.
// 2. Тексты мастера, игроков и авторов выводятся только как текст React: ни один модуль не
//    вставляет HTML строкой.

type Dirent = { name: string; parentPath: string; isFile(): boolean };
type FsModule = {
  readdirSync(
    path: URL,
    options: { withFileTypes: true; recursive: true },
  ): Dirent[];
  readFileSync(path: string, encoding: "utf8"): string;
};

/** Исходники клиента без тестов и тестовых данных: то, что попадает в сборку. */
async function productionSources(): Promise<Record<string, string>> {
  // Динамический импорт по имени из переменной: статический потребовал бы типов Node в
  // tsconfig.app.json, а клиентский код видеть process и Buffer не должен.
  const nodeFs = "node:fs";
  const fs = (await import(/* @vite-ignore */ nodeFs)) as FsModule;
  // Первый аргумент — не литерал: Vite заменяет `new URL("…", import.meta.url)` адресом
  // dev-сервера, а нужен настоящий путь на диске.
  const here = ".";
  const root = new URL(here, import.meta.url);
  const sources: Record<string, string> = {};
  for (const entry of fs.readdirSync(root, {
    withFileTypes: true,
    recursive: true,
  })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name)) continue;
    if (/\.test\.tsx?$/.test(entry.name)) continue;
    if (entry.name === "test-setup.ts" || entry.name === "fixtures.ts")
      continue;
    const path = `${entry.parentPath}/${entry.name}`;
    sources[path.slice(root.pathname.length).replace(/^\//, "")] =
      fs.readFileSync(path, "utf8");
  }
  return sources;
}

/** Исходник без комментариев: слово «localStorage» в пояснении не считается обращением. */
const withoutComments = (code: string) =>
  code.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

const sources = Object.fromEntries(
  Object.entries(await productionSources()).map(([file, code]) => [
    file,
    withoutComments(code),
  ]),
);

// Любое хранилище браузера; OTHER_STORAGE — всё, кроме localStorage (клиенту не нужно).
const ANY_STORAGE =
  /\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bdocument\s*\.\s*cookie\b|\bcaches\s*\.\s*open\b/;
const OTHER_STORAGE =
  /\bsessionStorage\b|\bindexedDB\b|\bdocument\s*\.\s*cookie\b|\bcaches\s*\.\s*open\b/;
// Ключ хранилища в исходнике: строка или шаблон, начинающиеся с «dnd-».
const STORAGE_KEY = /["'`](dnd-[a-z-]+:?)/g;

/** Модули, которым можно трогать localStorage, и их ключи. */
const ALLOWED: Record<string, readonly string[]> = {
  "screens/Journal.tsx": ["dnd-journal-seen:"],
  "theme/haptics.ts": ["dnd-ui-vibration"],
  "theme/settings.ts": ["dnd-ui-contrast", "dnd-ui-text-size"],
};

describe("правила охранника узнают обращения к хранилищу", () => {
  test.each([
    "window.localStorage.setItem('k', token)",
    "sessionStorage.setItem('k', v)",
    "indexedDB.open('db')",
    "document.cookie = 'a=b'",
    "await caches.open('c')",
  ])("%s", (code) => {
    expect(ANY_STORAGE.test(code)).toBe(true);
  });

  test("комментарий и похожие слова — не обращение", () => {
    expect(ANY_STORAGE.test(withoutComments("// пишем в localStorage"))).toBe(
      false,
    );
    expect(ANY_STORAGE.test("const storageKey = 'localStorageLike';")).toBe(
      false,
    );
  });

  test("ключ узнаётся и в строке, и в шаблоне", () => {
    const code = 'const a = "dnd-ui-x"; const b = `dnd-journal-seen:${id}`;';
    expect([...code.matchAll(STORAGE_KEY)].map((match) => match[1])).toEqual([
      "dnd-ui-x",
      "dnd-journal-seen:",
    ]);
  });
});

describe("хранилище браузера: только настройки интерфейса и отметки дневника", () => {
  test("охранник видит исходники (иначе он ничего не проверяет)", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(60);
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining([
        "App.tsx",
        "auth/session.ts",
        ...Object.keys(ALLOWED),
      ]),
    );
  });

  test("localStorage трогают только настройки, вибрация и дневник", () => {
    const users = Object.entries(sources)
      .filter(([, code]) => ANY_STORAGE.test(code))
      .map(([file]) => file)
      .sort();

    expect(users).toEqual(Object.keys(ALLOWED).sort());
  });

  test("sessionStorage, IndexedDB, cookie и Cache API клиент не использует", () => {
    const users = Object.entries(sources)
      .filter(([, code]) => OTHER_STORAGE.test(code))
      .map(([file]) => file);

    expect(users).toEqual([]);
  });

  test.each(Object.entries(ALLOWED))(
    "%s пишет только свои ключи из белого списка",
    (file, keys) => {
      const found = [
        ...new Set(
          [...sources[file].matchAll(STORAGE_KEY)].map((match) => match[1]),
        ),
      ];

      expect(found.sort()).toEqual([...keys].sort());
    },
  );
});

describe("тексты — только как текст React: HTML строкой не вставляется нигде", () => {
  const RAW_HTML =
    /dangerouslySetInnerHTML|\.innerHTML\b|\.outerHTML\s*=|insertAdjacentHTML|document\s*\.\s*write\b|createContextualFragment/;

  test.each([
    "<div dangerouslySetInnerHTML={{ __html: text }} />",
    "node.innerHTML = text;",
    "node.outerHTML = text;",
    "node.insertAdjacentHTML('beforeend', text);",
    "document.write(text);",
    "range.createContextualFragment(text);",
  ])("охранник узнаёт: %s", (code) => {
    expect(RAW_HTML.test(code)).toBe(true);
  });

  test("ни в одном модуле клиента нет вставки HTML строкой", () => {
    const offenders = Object.entries(sources)
      .filter(([, code]) => RAW_HTML.test(code))
      .map(([file]) => file);

    expect(offenders).toEqual([]);
  });
});
