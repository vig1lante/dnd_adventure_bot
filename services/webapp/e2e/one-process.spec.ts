import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test as noServerTest } from "@playwright/test";
import { expect, REPO_ROOT, serverEnv, test } from "./server.ts";

// Один процесс на базу (план B8.s2, мета H4): вторая копия `dnd-server` на занятом порту обязана
// упасть сразу, не выполнив запуск приложения. Запуск приложения восстанавливает игры и переводит
// идущие ходы в «Мастер не справился»; случись это во второй копии, она испортила бы ход первой.
// Юнит-тесты `tests/test_main.py` проверяют порядок «порт, затем uvicorn» на двойниках; здесь
// настоящие процессы на настоящей базе.
//
// Там же проверяется окружение процесса стенда (план B8.security.fix1): оно не должно вести
// к живой модели, что бы ни лежало в оболочке запускающего.

const LOGIN = "dev-1";

test("вторая копия сервера на занятом порту падает сразу и первую не трогает", async ({
  request,
  server,
}) => {
  const login = await request.post("/api/auth/dev", { data: { login: LOGIN } });
  expect(login.ok()).toBe(true);
  const headers = {
    Authorization: `Bearer ${((await login.json()) as { token: string }).token}`,
  };
  const created = await request.post("/api/games", {
    headers,
    data: { adventure_id: "demo-crypt" },
  });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  // Игры других тестов в базе тоже есть: сравниваются списки до и после, а не их длина.
  const before = (await (
    await request.get("/api/games", { headers })
  ).json()) as { id: string }[];
  expect(before.map((game) => game.id)).toContain(id);

  const second = await server.startSecondCopy();

  // Код 1 и понятная причина; приложение второй копии не стартовало.
  expect(second.exitCode).toBe(1);
  expect(second.output).toContain(`Порт 127.0.0.1:${server.port} уже занят`);
  expect(second.output).not.toContain("Application startup complete");
  expect(second.output).not.toContain("Started server process");

  // Первая копия продолжает работать с теми же играми.
  const health = await fetch(`http://127.0.0.1:${server.port}/health`);
  expect(health.ok).toBe(true);
  const after = await (await request.get("/api/games", { headers })).json();
  expect(after).toEqual(before);
});

/** Имена переменных настроек сервера: строки `ИМЯ=значение` в `.env.example` (их читает `Settings`). */
function settingsVariableNames(): string[] {
  const text = fs.readFileSync(path.join(REPO_ROOT, ".env.example"), "utf8");
  return [...text.matchAll(/^([A-Z][A-Z0-9_]*)=/gm)].map((match) => match[1]);
}

/** Окружение стенда при условии, что оболочка запускающего задала каждую переменную настроек. */
function serverEnvFromOwnerShell(
  dataDir: string,
  owner: Record<string, string>,
): NodeJS.ProcessEnv {
  const saved = new Map(Object.keys(owner).map((n) => [n, process.env[n]]));
  Object.assign(process.env, owner);
  try {
    return serverEnv(dataDir);
  } finally {
    // `process.env[имя] = undefined` записало бы строку «undefined», поэтому снимаем явно.
    for (const [name, value] of saved) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

// Сервер стенду для этой проверки не нужен: тест смотрит на функцию окружения, а не на процесс.
noServerTest("окружение сервера стенда не ведёт к живой модели", () => {
  const names = settingsVariableNames();
  // Защита от пустого разбора: без него проверки ниже прошли бы вхолостую.
  expect(names).toContain("CLAUDE_CLI_PATH");
  const owner = Object.fromEntries(
    names.map((name) => [name, `owner-shell:${name}`]),
  );
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "dnd-e2e-env-"));
  try {
    const env = serverEnvFromOwnerShell(dataDir, owner);

    // Каждая переменная настроек задана самой фикстурой: ни оболочка запускающего, ни `.env`
    // владельца на стенд не влияют (пустое значение в окружении перекрывает `.env`).
    expect(names.filter((name) => env[name] === owner[name])).toEqual([]);

    // Путь к `claude` ведёт на несуществующий файл. Путь с каталогом в PATH не ищут, а файла
    // нет, поэтому запуск `claude` на стенде кончается ошибкой запуска, без процесса.
    const cli = env.CLAUDE_CLI_PATH ?? "";
    expect(path.isAbsolute(cli)).toBe(true);
    expect(path.dirname(cli)).toBe(dataDir);
    expect(fs.existsSync(cli)).toBe(false);

    // Провайдер по умолчанию — заглушка; ключи, адреса и модели настоящих провайдеров пусты.
    // Модель подписки Claude Code тоже пуста: в списке моделей формы только псевдонимы CLI.
    expect(env).toMatchObject({
      APP_ENV: "dev",
      LLM_PROVIDER: "fake",
      LLM_MODEL: "",
      LLM_EFFORT: "",
      LLM_CHEAP_MODEL: "",
      CLAUDE_CLI_MODEL: "",
      CLAUDE_CLI_EFFORT: "",
      CLAUDE_CLI_CHEAP_MODEL: "",
      ANTHROPIC_API_KEY: "",
      OPENAI_COMPAT_API_KEY: "",
      OPENAI_COMPAT_BASE_URL: "",
      OPENAI_COMPAT_MODEL: "",
      TELEGRAM_BOT_TOKEN: "",
    });
  } finally {
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});
