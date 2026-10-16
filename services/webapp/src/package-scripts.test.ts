import packageJson from "../package.json";

const { scripts } = packageJson;

// Команды из плана B1.4: на них опираются README, CLAUDE.md и сам цикл.
test("скрипты package.json совпадают с планом", () => {
  expect(scripts).toMatchObject({
    dev: "vite",
    build: "tsc -b && vite build",
    preview: "vite preview",
    test: "vitest run",
    lint: "eslint .",
    "format:check": "prettier --check .",
    e2e: "playwright test",
  });
});

// Финальный lint только проверяет и ничего не правит.
test.each(["lint", "format:check"] as const)(
  "скрипт %s не исправляет файлы",
  (name) => {
    expect(scripts[name]).not.toMatch(/--fix|--write/);
  },
);
