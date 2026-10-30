// Страницы `/?preview=style` и `/?preview=screens` нужны владельцу при разработке; в боевой
// сборке их быть не должно. Тест `main.tsx` проверяет ветку с подменой DEV, а здесь
// проверяется результат: настоящая сборка Vite не содержит модуля превью, а сборка в режиме
// разработки содержит (контроль: без него «ничего не нашли» ничего бы не доказывало).

type ChildProcess = {
  spawnSync(
    command: string,
    args: string[],
    options: {
      cwd: string;
      env: Record<string, string | undefined>;
      encoding: "utf8";
    },
  ): { status: number | null; stderr: string; stdout: string };
};
type Fs = {
  mkdtempSync(prefix: string): string;
  readdirSync(path: string, options: { recursive: true }): string[];
  readFileSync(path: string, encoding: "utf8"): string;
  rmSync(path: string, options: { recursive: true; force: true }): void;
};
type Path = { join(...parts: string[]): string };
type Os = { tmpdir(): string };
type NodeProcess = {
  execPath: string;
  env: Record<string, string | undefined>;
};

// Узнаётся только превью: строки, которых в самом приложении нет. Страница стиля — заголовок
// раздела; макеты экранов — заголовок оглавления и название приключения из их данных
// (screenData.ts): так видно, что в сборку не попали ни страница макетов, ни её данные.
const PREVIEW_MARKERS = [
  "Склеп забытых свечей",
  "Макеты экранов: как есть и как будет",
  "Маяк над Солёной бухтой",
] as const;
// Имена модулей превью: Vite называет ими отдельные куски сборки.
const PREVIEW_FILES = /StylePreview|ScreensPreview|screenData/;

async function node() {
  // Динамический импорт по имени из переменной: статический потребовал бы типов Node в
  // tsconfig.app.json, а клиентский код видеть process и Buffer не должен.
  const load = (name: string) =>
    import(/* @vite-ignore */ name) as Promise<unknown>;
  return {
    childProcess: (await load("node:child_process")) as ChildProcess,
    fs: (await load("node:fs")) as Fs,
    path: (await load("node:path")) as Path,
    os: (await load("node:os")) as Os,
    process: ((await load("node:process")) as { default: NodeProcess }).default,
  };
}

/** Собирает приложение в пустую временную папку и отдаёт тексты всех собранных файлов. */
async function build(mode: "production" | "development") {
  const { childProcess, fs, path, os, process } = await node();
  // Первый аргумент — не литерал: Vite заменяет `new URL("…", import.meta.url)` адресом
  // dev-сервера, а нужен путь на диске.
  const rootPath = "../..";
  const root = new URL(rootPath, import.meta.url).pathname;
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "dnd-bundle-"));
  try {
    const result = childProcess.spawnSync(
      process.execPath,
      [
        path.join(root, "node_modules/vite/bin/vite.js"),
        "build",
        "--outDir",
        outDir,
        "--emptyOutDir",
        "--mode",
        mode,
      ],
      { cwd: root, env: { ...process.env, NODE_ENV: mode }, encoding: "utf8" },
    );
    expect(result.status, result.stderr || result.stdout).toBe(0);
    const files: Record<string, string> = {};
    for (const name of fs.readdirSync(outDir, { recursive: true })) {
      if (/\.(js|html|css)$/.test(name)) {
        files[name] = fs.readFileSync(path.join(outDir, name), "utf8");
      }
    }
    return files;
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

describe("превью (стиль и макеты экранов) и боевая сборка", () => {
  test("в сборке для разработки превью есть (контроль поиска)", async () => {
    const files = await build("development");

    for (const marker of PREVIEW_MARKERS) {
      const holders = Object.entries(files)
        .filter(([, text]) => text.includes(marker))
        .map(([name]) => name);
      expect(holders.length, marker).toBeGreaterThan(0);
    }
  }, 60_000);

  test("в боевой сборке превью нет ни в одном файле", async () => {
    const files = await build("production");

    expect(Object.keys(files).some((name) => name.endsWith(".js"))).toBe(true);
    const holders = Object.entries(files)
      .filter(
        ([name, text]) =>
          PREVIEW_MARKERS.some((marker) => text.includes(marker)) ||
          PREVIEW_FILES.test(name),
      )
      .map(([name]) => name);
    expect(holders).toEqual([]);
  }, 60_000);
});

// --- кто подключает модули превью ---

// Маркеры выше узнают в сборке только страницу стиля, оглавление макетов и данные screenData.
// Черновики «как будет» (drafts, NextScreens), «сервер» превью (backend, scenes) своих маркеров не
// имеют: если боевой экран импортирует их напрямую, они вошли бы в сборку незаметно. Поэтому
// здесь проверяется исходник: модули превью подключает только main.tsx — гейт адреса и две
// ленивые ветки за `import.meta.env.DEV`, которые прод-сборка вырезает.
describe("модули превью подключает только dev-ветка main.tsx", () => {
  // Путь подключаемого модуля: статический импорт, реэкспорт или import().
  const IMPORT_SPEC = /(?:from\s+|import\s*\(\s*|import\s+)["']([^"']+)["']/g;

  async function sources(): Promise<Record<string, string>> {
    const { fs, path } = await node();
    const rootPath = "../..";
    const src = path.join(new URL(rootPath, import.meta.url).pathname, "src");
    const files: Record<string, string> = {};
    for (const name of fs.readdirSync(src, { recursive: true })) {
      if (!/\.tsx?$/.test(name) || /\.test\.tsx?$/.test(name)) continue;
      if (name === "test-setup.ts" || name.endsWith("fixtures.ts")) continue;
      files[name] = fs.readFileSync(path.join(src, name), "utf8");
    }
    return files;
  }

  /** Пути модулей превью, которые подключает файл `name` (относительно src/). */
  const previewImports = (name: string, code: string): string[] =>
    name.startsWith("preview/")
      ? []
      : [...code.matchAll(IMPORT_SPEC)]
          .map((match) => match[1])
          .filter((spec) => /(^|\/)preview\//.test(spec));

  test("ни один боевой модуль вне preview/ не импортирует превью, кроме main.tsx", async () => {
    const files = await sources();
    expect(Object.keys(files).length).toBeGreaterThan(60);

    const importers = Object.entries(files)
      .filter(([name, code]) => previewImports(name, code).length > 0)
      .map(([name]) => name);

    expect(importers).toEqual(["main.tsx"]);
  });

  test("main.tsx: статически — только гейт адреса, страницы превью — лениво и только при DEV", async () => {
    const main = (await sources())["main.tsx"];

    expect(previewImports("main.tsx", main).sort()).toEqual([
      "./preview/ScreensPreview",
      "./preview/StylePreview",
      "./preview/gate",
    ]);
    const lazyInDev = [
      ...main.matchAll(
        /import\.meta\.env\.DEV\s*\?\s*lazy\(\s*\(\)\s*=>\s*import\(\s*"\.\/preview\/(\w+)"\s*\)\s*\)/g,
      ),
    ].map((match) => match[1]);
    expect(lazyInDev.sort()).toEqual(["ScreensPreview", "StylePreview"]);
    expect(main).toMatch(/^import\s*\{[^}]*\}\s*from\s*"\.\/preview\/gate";$/m);
  });

  test("гейт адреса лёгкий: из приложения он берёт только типы", async () => {
    const gate = (await sources())["preview/gate.ts"];

    const imports = [...gate.matchAll(/^import\s+(type\s+)?[^;]*;$/gm)];
    expect(imports.length).toBeGreaterThan(0);
    for (const [line, typeOnly] of imports) {
      expect(typeOnly, line).toBe("type ");
    }
  });
});
