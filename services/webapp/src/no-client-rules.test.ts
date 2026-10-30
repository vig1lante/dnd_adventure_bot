// Инвариант проекта: нейронка рассказывает, код считает; броски делает только сервер, клиент
// не может их подменить. Тесты отдельных экранов проверяют, что конкретный экран показывает
// числа сервера как есть. Здесь охранник шире: во всём браузерном коде нет ни генератора
// случайных чисел, ни формулы правил, которой клиент мог бы «досчитать» результат сам. Если
// когда-нибудь понадобится честная случайность на клиенте (например, для анимации кубика), это
// решение владельца, а не побочная правка.

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

/** Исходник без комментариев: слово «random» в пояснении не должно считаться вызовом. */
const withoutComments = (code: string) =>
  code.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

const sources = await productionSources();
const entries = Object.entries(sources).map(
  ([file, code]) => [file, withoutComments(code)] as const,
);

// Что считается нарушением. Правила вынесены наверх, чтобы тест ниже мог проверить их на
// заведомо плохом коде: опечатка в выражении иначе молча превратила бы охранника в пустышку.
const RANDOM =
  /\bMath\s*\.\s*random\b|\bgetRandomValues\b|\brandomUUID\b|\brandomInt\b/;
const ABILITY_MODIFIER = /-\s*10\s*\)\s*\/\s*2|\/\s*2\s*\)\s*-\s*5\b/;
// Единственное место, где клиент трогает запись костей, — `ruDice` («1d8» → «1к8»): он заменяет
// буквы и не вычисляет ничего. Любое вычисление по записи кубика — расчёт.
const DICE_MATH = /\bparseDice\b|\brollDice\b|\bevalDice\b|\bnew\s+Dice\b/;

describe("правила охранника узнают нарушения", () => {
  test.each([
    ["Math.random", "const roll = Math.random() * 20;", RANDOM],
    ["Math .random", "x = Math . random();", RANDOM],
    ["crypto", "crypto.getRandomValues(new Uint8Array(1));", RANDOM],
    ["randomUUID", "const id = crypto.randomUUID();", RANDOM],
    [
      "модификатор",
      "const mod = Math.floor((score - 10) / 2);",
      ABILITY_MODIFIER,
    ],
    [
      "модификатор иначе",
      "const mod = Math.floor(score / 2) - 5;",
      ABILITY_MODIFIER,
    ],
    ["разбор костей", "const dice = parseDice(expr);", DICE_MATH],
    ["бросок костей", "return rollDice(2, 6);", DICE_MATH],
  ])("%s", (_name, code, pattern) => {
    expect(pattern.test(withoutComments(code))).toBe(true);
  });

  test("комментарии не считаются нарушением", () => {
    expect(RANDOM.test(withoutComments("// никакого Math.random()"))).toBe(
      false,
    );
    expect(RANDOM.test(withoutComments("/* Math.random */ const a = 1;"))).toBe(
      false,
    );
  });

  test("обычный код с похожими словами не считается нарушением", () => {
    expect(RANDOM.test("const randomly = 1; // nothing")).toBe(false);
    expect(
      ABILITY_MODIFIER.test("const total = value - 10; // not halved"),
    ).toBe(false);
  });
});

describe("клиент ничего не бросает и не считает за сервер", () => {
  test("охранник видит исходники (иначе он ничего не проверяет)", () => {
    expect(entries.length).toBeGreaterThan(60);
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining([
        "App.tsx",
        "api/client.ts",
        "components/RollCard.tsx",
        "screens/character/Random.tsx",
      ]),
    );
    expect(Object.keys(sources).some((file) => file.endsWith(".test.ts"))).toBe(
      false,
    );
  });

  test("нет генератора случайных чисел: ни Math.random, ни crypto", () => {
    const offenders = entries.filter(([, code]) => RANDOM.test(code));

    expect(offenders.map(([file]) => file)).toEqual([]);
  });

  test("нет формулы модификатора характеристики: (значение − 10) / 2", () => {
    const offenders = entries.filter(([, code]) => ABILITY_MODIFIER.test(code));

    expect(offenders.map(([file]) => file)).toEqual([]);
  });

  test("нет разбора и вычисления костей: запись «к8» только переводится словами", () => {
    const offenders = entries.filter(([, code]) => DICE_MATH.test(code));

    expect(offenders.map(([file]) => file)).toEqual([]);
  });

  test("клиент не присылает серверу бросков: в клиенте API нет метода броска", () => {
    const client = sources["api/client.ts"];

    expect(client).not.toMatch(/roll-abilities|rollAbilities|rolled_set/);
    expect(client).not.toMatch(/\bseed\b/);
  });
});
