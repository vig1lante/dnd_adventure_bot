import { spawn, spawnSync } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { test as base } from "@playwright/test";

// Фикстура сервера игры для сквозного теста: настоящий `dnd-server` с `FakeProvider` по сценарию
// (по умолчанию `e2e_full.yaml`), временной базой SQLite и пустыми ключами. Запускается так же,
// как у разработчика: `uv run dnd-server --port <порт>` из корня репозитория. Порт выбирает
// `playwright.config.ts` (от него зависит прокси Vite) и передаёт через окружение.
//
// Свой сценарий спецификация выбирает параметром фикстуры: `test.use({ scenario: "…yaml" })`.
// Параметр живёт в рабочем процессе, поэтому Playwright запускает такую спецификацию в отдельном
// рабочем процессе: со своим сервером на том же порту и своей чистой базой (прежний сервер к
// этому времени остановлен и порт свободен — рабочий процесс один, см. `playwright.config.ts`).

/** Корень репозитория: `services/webapp/e2e` → три уровня вверх. */
export const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
/** Папка сценариев `FakeProvider` от корня репозитория. */
export const SCENARIOS_DIR = "services/server/tests/scenarios";
/** Сценарий по умолчанию: вечер «6 игроков» и остальные спецификации. */
export const DEFAULT_SCENARIO = "e2e_full.yaml";
const HOST = "127.0.0.1";
// Файл базы во временной папке стенда; SQLite кладёт рядом свои файлы с тем же началом имени.
const DB_FILE = "e2e.sqlite3";

// Отсчёты короткие, чтобы вечер уложился в минуты. Привалу хватает времени сказать реплику.
// Короткий привал чуть длиннее: на нём ещё тратят кость хитов (`rests.spec.ts`), а закончить
// его раньше хост может кнопкой.
const COLLECT_SECONDS = "5";
const HURRY_SECONDS = "15";
const CAMP_SECONDS = "15";
const SHORT_REST_SECONDS = "20";

// Тестовый секрет сессий: не боевой и не из `.env`. Токены при перезапуске остаются верными.
const SESSION_SECRET = "e2e-playwright-session-secret-0123456789abcdef";

const HEALTH_TIMEOUT_MS = 60_000;
const STOP_TIMEOUT_MS = 15_000;

/** Папка артефактов запуска вне репозитория (создаёт и выбирает `playwright.config.ts`). */
export function artifactsDir(): string {
  const dir = process.env.E2E_ARTIFACTS_DIR;
  if (dir === undefined) {
    throw new Error(
      "E2E_ARTIFACTS_DIR не задан: тест запускают через npm run e2e",
    );
  }
  return dir;
}

/** Итог запуска второй копии сервера на занятом порту. */
export interface SecondCopyResult {
  /** Код выхода второй копии; `null` — её остановили сигналом. */
  exitCode: number | null;
  /** Всё, что она написала в stdout и stderr. */
  output: string;
}

export interface DndServer {
  /** Порт, на котором слушает сервер (127.0.0.1). */
  readonly port: number;
  /** Останавливает сервер и запускает заново на том же порту и той же базе. */
  restart(): Promise<void>;
  /**
   * Останавливает сервер, начинает базу стенда заново (пустая база после миграций) и запускает
   * сервер на том же порту. Тест убирает так за собой то, что помешало бы следующим: игры
   * пользователей, черновики и опубликованные приключения (каталог перечитывается при старте).
   */
  resetDatabase(): Promise<void>;
  /**
   * Запускает вторую копию `dnd-server` на том же порту и той же базе, пока первая работает, и
   * ждёт её выхода. Вторая копия обязана упасть сразу, не выполнив запуск приложения.
   */
  startSecondCopy(): Promise<SecondCopyResult>;
}

function serverPort(): number {
  const port = Number(process.env.E2E_SERVER_PORT);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(
      "E2E_SERVER_PORT не задан: тест запускают через npm run e2e",
    );
  }
  return port;
}

/**
 * Окружение сервера. Каждая переменная настроек из `.env.example` задана явно, поэтому ни
 * окружение запустившего, ни `.env` владельца на стенд не влияют: пустое значение в окружении
 * важнее `.env` (README, «Приоритет источников»). К живой модели ведут три пути, и все закрыты:
 * - `anthropic_api` и `openai_compatible`: провайдер по умолчанию — `fake`, а ключи, адрес и
 *   модель чужого сервера пусты, поэтому игру с ними создать нельзя (в списке они «Не настроен»).
 *   Переменные самих SDK вне настроек (`ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`…) ничего не
 *   открывают: клиент собирается только после проверки ключа и адреса из настроек;
 * - `claude_code_cli`: в dev он остаётся в списке и выбирается (фабрика не проверяет путь), но
 *   `CLAUDE_CLI_PATH` указывает на несуществующий файл в папке стенда. Путь с каталогом в PATH
 *   не ищут, поэтому запуск хода кончается ошибкой «не удалось запустить claude», без процесса;
 * - модели (`LLM_MODEL`, `LLM_EFFORT`, `LLM_CHEAP_MODEL`) и модель подписки Claude Code
 *   (`CLAUDE_CLI_MODEL`, `CLAUDE_CLI_EFFORT`, `CLAUDE_CLI_CHEAP_MODEL`) сброшены к значениям по
 *   умолчанию: пустое значение — «не задано», поэтому модель и усилия выбрал бы сам CLI, а форма
 *   новой игры предлагает для него только псевдонимы `opus`, `sonnet`, `haiku`.
 * Ещё обнулены токен и имя бота (бот Telegram выключен) и задана своя временная база. Остальное
 * (PATH, HOME) наследуется, чтобы `uv` нашёл Python и кэш. Тест в `one-process.spec.ts` сверяет
 * эту функцию со списком переменных `.env.example`.
 * `dataDir` — временная папка стенда: в ней база и путь к несуществующему `claude`;
 * `scenario` — файл сценария `FakeProvider` в `SCENARIOS_DIR`.
 */
export function serverEnv(
  dataDir: string,
  scenario = DEFAULT_SCENARIO,
): NodeJS.ProcessEnv {
  return {
    ...process.env,
    APP_ENV: "dev",
    LLM_PROVIDER: "fake",
    FAKE_LLM_SCENARIO: path.posix.join(SCENARIOS_DIR, scenario),
    DATABASE_URL: `sqlite+aiosqlite:///${path.join(dataDir, DB_FILE)}`,
    CONTENT_DIR: "content/adventures",
    SESSION_SECRET,
    TELEGRAM_BOT_TOKEN: "",
    TELEGRAM_BOT_USERNAME: "",
    GAME_CREATOR_TG_IDS: "",
    ANTHROPIC_API_KEY: "",
    OPENAI_COMPAT_API_KEY: "",
    OPENAI_COMPAT_BASE_URL: "",
    OPENAI_COMPAT_MODEL: "",
    LLM_MODEL: "",
    LLM_EFFORT: "",
    LLM_CHEAP_MODEL: "",
    CLAUDE_CLI_PATH: path.join(dataDir, "claude-is-disabled-in-e2e"),
    CLAUDE_CLI_MODEL: "",
    CLAUDE_CLI_EFFORT: "",
    CLAUDE_CLI_CHEAP_MODEL: "",
    WEBAPP_PUBLIC_URL: `http://${HOST}:${process.env.E2E_WEB_PORT ?? ""}`,
    COLLECT_SECONDS,
    HURRY_SECONDS,
    CAMP_SECONDS,
    SHORT_REST_SECONDS,
    PYTHONUNBUFFERED: "1",
  };
}

function killGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  if (child.pid === undefined) return;
  try {
    // Отдельная группа процессов: `uv run` и Python под ним получают сигнал вместе.
    process.kill(-child.pid, signal);
  } catch {
    // группы уже нет
  }
}

function hasExited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

/** Запущенный сервер; `failure` — процесс не удалось даже создать (например, нет `uv`). */
interface Running {
  child: ChildProcess;
  failure: Error | undefined;
  /** Страховка на выход рабочего процесса Playwright: убить сервер, если тот не остановлен. */
  killOnExit: () => void;
}

/** Ждёт `GET /health`: порт слушает ещё до старта приложения, «соединился» не значит «готов». */
async function waitForHealth(port: number, running: Running): Promise<void> {
  const { child } = running;
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (running.failure !== undefined) {
      throw new Error(`не удалось запустить uv: ${running.failure.message}`);
    }
    if (hasExited(child)) {
      throw new Error(
        `dnd-server завершился при запуске (код ${child.exitCode ?? child.signalCode}); лог — в папке артефактов`,
      );
    }
    try {
      const response = await fetch(`http://${HOST}:${port}/health`, {
        signal: AbortSignal.timeout(2_000),
      });
      const body = (await response.json()) as { status?: string; env?: string };
      if (response.ok && body.status === "ok") {
        if (body.env !== "dev") {
          throw new Error(
            `сервер на порту ${port} работает не в dev: ${body.env}`,
          );
        }
        return;
      }
    } catch (error) {
      if (error instanceof Error && error.message.includes("не в dev"))
        throw error;
      // ещё не слушает или не успел ответить: пробуем снова
    }
    await sleep(200);
  }
  throw new Error(
    `dnd-server не ответил на /health за ${HEALTH_TIMEOUT_MS / 1000} с`,
  );
}

/** Порт принимает соединения (кто-то на нём слушает). */
function isListening(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect({ host: HOST, port });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
  });
}

async function waitUntilFree(port: number): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (!(await isListening(port))) return;
    await sleep(100);
  }
  throw new Error(`порт ${port} остался занят после остановки сервера`);
}

/** Останавливает сервер сигналом (как Ctrl+C): uvicorn закрывается штатно; зависший — убивает. */
async function stop(running: Running, port: number): Promise<void> {
  const { child } = running;
  // Остановленному серверу страховка не нужна: номер его группы мог достаться чужому процессу.
  process.off("exit", running.killOnExit);
  if (child.pid === undefined) return; // процесс не создан
  if (!hasExited(child)) {
    const exited = new Promise<void>((resolve) =>
      child.once("exit", () => resolve()),
    );
    killGroup(child, "SIGTERM");
    const force = setTimeout(
      () => killGroup(child, "SIGKILL"),
      STOP_TIMEOUT_MS,
    );
    await exited;
    clearTimeout(force);
  }
  // Вся группа: после выхода `uv` под ним мог остаться Python.
  killGroup(child, "SIGKILL");
  await waitUntilFree(port);
}

/** Миграции на временной базе: сервер сам схему не создаёт (README, «База данных и миграции»). */
function migrate(env: NodeJS.ProcessEnv): void {
  const result = spawnSync(
    "uv",
    [
      "run",
      "--locked",
      "alembic",
      "-c",
      "services/server/alembic.ini",
      "upgrade",
      "head",
    ],
    { cwd: REPO_ROOT, env, encoding: "utf8", timeout: 120_000 },
  );
  if (result.status !== 0) {
    throw new Error(
      `миграции временной базы не прошли (код ${result.status}):\n${result.stderr}`,
    );
  }
}

function launch(
  port: number,
  env: NodeJS.ProcessEnv,
  log: fs.WriteStream,
): Running {
  const child = spawn(
    "uv",
    ["run", "--locked", "dnd-server", "--port", String(port)],
    {
      cwd: REPO_ROOT,
      env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const running: Running = {
    child,
    failure: undefined,
    killOnExit: () => killGroup(child, "SIGKILL"),
  };
  child.once("error", (error) => {
    running.failure = error;
  });
  child.stdout?.pipe(log, { end: false });
  child.stderr?.pipe(log, { end: false });
  // Если рабочий процесс Playwright завершится жёстко, сервер не должен остаться сиротой.
  process.once("exit", running.killOnExit);
  return running;
}

/** Запускает вторую копию и ждёт её выхода; зависшую — останавливает и сообщает об ошибке. */
async function runSecondCopy(
  port: number,
  env: NodeJS.ProcessEnv,
): Promise<SecondCopyResult> {
  const child = spawn(
    "uv",
    ["run", "--locked", "dnd-server", "--port", String(port)],
    { cwd: REPO_ROOT, env, detached: true, stdio: ["ignore", "pipe", "pipe"] },
  );
  let output = "";
  child.stdout?.on("data", (chunk: Buffer) => (output += String(chunk)));
  child.stderr?.on("data", (chunk: Buffer) => (output += String(chunk)));
  const finished = new Promise<number | null>((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code));
  });
  const stuck = setTimeout(
    () => killGroup(child, "SIGKILL"),
    HEALTH_TIMEOUT_MS,
  );
  try {
    const exitCode = await finished;
    if (exitCode === null) {
      throw new Error(`вторая копия не вышла за ${HEALTH_TIMEOUT_MS / 1000} с`);
    }
    return { exitCode, output };
  } finally {
    clearTimeout(stuck);
    killGroup(child, "SIGKILL");
  }
}

/** Лог сервера в папке артефактов: у другого сценария свой файл, чтобы его не затёр следующий. */
function serverLogName(scenario: string): string {
  return scenario === DEFAULT_SCENARIO
    ? "server.log"
    : `server-${path.parse(scenario).name}.log`;
}

interface WorkerFixtures {
  /** Файл сценария `FakeProvider` в `SCENARIOS_DIR`; спецификация меняет его `test.use`. */
  scenario: string;
  server: DndServer;
}

export const test = base.extend<object, WorkerFixtures>({
  scenario: [DEFAULT_SCENARIO, { scope: "worker", option: true }],
  server: [
    async ({ scenario }, provide) => {
      const port = serverPort();
      const dir = artifactsDir();
      fs.mkdirSync(dir, { recursive: true });
      // База во временной папке системы, не в репозитории и не в `data/`.
      const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "dnd-e2e-db-"));
      const env = serverEnv(dataDir, scenario);
      const logFile = path.join(dir, serverLogName(scenario));
      const log = fs.createWriteStream(logFile, { flags: "w" });
      let running: Running | undefined;

      const start = async () => {
        running = launch(port, env, log);
        await waitForHealth(port, running);
      };
      const halt = async () => {
        if (running !== undefined) await stop(running, port);
        running = undefined;
      };

      try {
        migrate(env);
        await start();
        await provide({
          port,
          restart: async () => {
            log.write("\n=== перезапуск сервера ===\n");
            await halt();
            await start();
          },
          resetDatabase: async () => {
            log.write("\n=== база стенда начата заново ===\n");
            await halt();
            for (const name of fs.readdirSync(dataDir)) {
              if (name.startsWith(DB_FILE)) fs.rmSync(path.join(dataDir, name));
            }
            migrate(env);
            await start();
          },
          startSecondCopy: () => runSecondCopy(port, env),
        });
      } finally {
        await halt();
        log.end();
        fs.rmSync(dataDir, { recursive: true, force: true });
      }
    },
    { scope: "worker", auto: true },
  ],
});

export { expect } from "@playwright/test";
