import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// Сквозной тест «6 игроков». Dev-сервер Vite поднимает `webServer`, а сервер игры
// с `FakeProvider` и временной базой поднимает фикстура `e2e/server.ts` (ей нужен перезапуск).
// Оба слушают свободные порты 127.0.0.1, а не 8000 и 5173: на них может работать сервер владельца.

/** Свободный порт на 127.0.0.1: сокет слушает на порту 0, берём выданный номер и закрываем. */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address() as net.AddressInfo;
      probe.close(() => resolve(port));
    });
  });
}

// Конфиг читают и главный процесс, и рабочий процесс теста; порты выбирает только главный и
// передаёт рабочему через окружение (рабочий процесс его наследует). Окружение пользователя
// порты не задаёт: чужое значение могло бы указать на 8000 или 5173.
const isWorker = process.env.TEST_WORKER_INDEX !== undefined;
if (!isWorker) {
  process.env.E2E_WEB_PORT = String(await freePort());
  process.env.E2E_SERVER_PORT = String(await freePort());
}
const webPort = Number(process.env.E2E_WEB_PORT);
const serverPort = Number(process.env.E2E_SERVER_PORT);

// Отчёты, трассы, снимки экранов и лог сервера лежат вне репозитория: ни одна из этих папок
// не должна оказаться в коммите или раздаваться dev-сервером Vite. Путь можно сменить.
const artifactsDir =
  process.env.E2E_ARTIFACTS_DIR ?? path.join(os.tmpdir(), "dnd-adventure-e2e");
process.env.E2E_ARTIFACTS_DIR = artifactsDir;
fs.mkdirSync(artifactsDir, { recursive: true });

const baseURL = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  // Один сервер и одна игра на весь запуск: тесты идут по очереди в одном процессе.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  // Вечер игры целиком: шестеро игроков, три раунда, бой, привал и перезапуск сервера.
  timeout: 300_000,
  expect: { timeout: 20_000 },
  outputDir: path.join(artifactsDir, "test-results"),
  reporter: [
    ["list"],
    [
      "html",
      { outputFolder: path.join(artifactsDir, "report"), open: "never" },
    ],
  ],
  use: {
    baseURL,
    locale: "ru-RU",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Вывод Vite уходит в лог рядом с логом сервера: при перезапуске сервера Vite пишет
    // «ws proxy error» об оборванных сокетах, и в выводе теста это выглядело бы сбоем.
    command: `npm run dev -- --host 127.0.0.1 --port ${webPort} > ${JSON.stringify(path.join(artifactsDir, "vite.log"))} 2>&1`,
    url: baseURL,
    // Чужой процесс на этом порту — не наш Vite: прокси смотрел бы не на тот сервер.
    reuseExistingServer: false,
    timeout: 120_000,
    env: { DND_SERVER_PORT: String(serverPort) },
  },
});
