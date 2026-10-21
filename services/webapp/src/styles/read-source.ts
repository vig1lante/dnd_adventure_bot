// Только для тестов стилей. CSS в vitest отключён (test.css.include пуст), поэтому
// `import x from "./a.css?raw"` отдаёт пустую строку. Файлы читаем через node:fs
// динамическим импортом: статический import потребовал бы типов Node в tsconfig.app.json,
// а их там нет намеренно — клиентский код не должен видеть process и Buffer.

type ReadFile = (url: URL, encoding: "utf8") => string;

/** Текст файла по пути относительно этой папки (`src/styles`). */
export async function readSource(relativePath: string): Promise<string> {
  const nodeFs = "node:fs";
  const { readFileSync } = (await import(/* @vite-ignore */ nodeFs)) as {
    readFileSync: ReadFile;
  };
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}
