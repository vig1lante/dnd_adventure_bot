// Страница `/?preview=style` нужна владельцу при разработке; в боевой сборке её быть не должно
// (B8.1a, мета B8.explain F31). Тест `main.tsx` проверяет ветку с подменой DEV, а здесь
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

// Узнаётся только превью: заголовок раздела на странице стиля, в самом приложении его нет.
const PREVIEW_MARKER = "Склеп забытых свечей";

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

describe("превью стиля и боевая сборка", () => {
  test("в сборке для разработки превью есть (контроль поиска)", async () => {
    const files = await build("development");

    const holders = Object.entries(files)
      .filter(([, text]) => text.includes(PREVIEW_MARKER))
      .map(([name]) => name);
    expect(holders.length).toBeGreaterThan(0);
  }, 60_000);

  test("в боевой сборке превью нет ни в одном файле", async () => {
    const files = await build("production");

    expect(Object.keys(files).some((name) => name.endsWith(".js"))).toBe(true);
    const holders = Object.entries(files)
      .filter(
        ([name, text]) =>
          text.includes(PREVIEW_MARKER) || /StylePreview/.test(name),
      )
      .map(([name]) => name);
    expect(holders).toEqual([]);
  }, 60_000);
});
