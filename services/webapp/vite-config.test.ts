import type { IncomingMessage } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { resolveConfig, resolveEnvPrefix } from "vite";
import type { Plugin, ProxyOptions, UserConfig } from "vite";

const WEBAPP_DIR = path.dirname(fileURLToPath(import.meta.url));
const SERVER_URL = "http://127.0.0.1:8000";

// Vite принимает прокси строкой-сокращением или объектом; приводим к объекту.
function proxyOptions(from: UserConfig, route: string): ProxyOptions {
  const entry = from.server?.proxy?.[route];
  if (entry === undefined) {
    throw new Error(`В server.proxy нет правила для ${route}`);
  }
  return typeof entry === "string" ? { target: entry } : entry;
}

/** Конфиг так, как его прочитал бы Vite при заданном `DND_SERVER_PORT` ("" — не задан). */
async function configWithServerPort(value: string): Promise<UserConfig> {
  vi.resetModules();
  vi.stubEnv("DND_SERVER_PORT", value);
  return (await import("./vite.config.ts")).default;
}

// Конфиг без переопределений, как он читается у разработчика без DND_SERVER_PORT.
let config: UserConfig;

beforeAll(async () => {
  config = await configWithServerPort("");
});

/** Решение прокси для адреса запроса: `false` — Vite ответит 404, пусто — запрос уйдёт на сервер. */
async function bypassOf(route: "/api" | "/ws", url: string | undefined) {
  const options = proxyOptions(config, route);
  return options.bypass?.({ url } as IncomingMessage, undefined, options);
}

/** Плагины конфига одним списком: `react()` отдаёт массив, а пустые места Vite пропускает. */
function namedPlugins(options: unknown): Plugin[] {
  if (Array.isArray(options)) return options.flatMap(namedPlugins);
  return typeof options === "object" && options !== null && "name" in options
    ? [options as Plugin]
    : [];
}

afterEach(() => {
  vi.unstubAllEnvs();
});

// Клиент ходит только на относительные /api/... и /ws; в dev их должен
// проксировать Vite, иначе запросы не дойдут до сервера.
describe("прокси dev-сервера", () => {
  test("/api идёт на сервер", () => {
    expect(proxyOptions(config, "/api").target).toBe(SERVER_URL);
  });

  test("/ws идёт на сервер с поддержкой WebSocket", () => {
    const options = proxyOptions(config, "/ws");

    expect(options.target).toBe(SERVER_URL);
    expect(options.ws).toBe(true);
  });

  test("проксируются только /api и /ws", () => {
    expect(Object.keys(config.server?.proxy ?? {}).sort()).toEqual([
      "/api",
      "/ws",
    ]);
  });
});

// Сквозной тест поднимает свой сервер на свободном порту, не трогая сервер на 8000.
describe("порт сервера для прокси", () => {
  test.each(["/api", "/ws"])(
    "%s идёт на порт из DND_SERVER_PORT",
    async (route) => {
      const overridden = await configWithServerPort("8123");

      expect(proxyOptions(overridden, route).target).toBe(
        "http://127.0.0.1:8123",
      );
    },
  );

  test("пустая переменная — порт по умолчанию", async () => {
    const unset = await configWithServerPort("");

    expect(proxyOptions(unset, "/api").target).toBe(SERVER_URL);
  });

  test.each([
    "abc",
    "0",
    "65536",
    "-1",
    "80.5",
    " 8000",
    "8000 ",
    "1e3",
    "0x50",
  ])("значение %j не порт: ошибка вместо молчаливого 8000", async (value) => {
    await expect(configWithServerPort(value)).rejects.toThrow(
      /DND_SERVER_PORT/,
    );
  });

  test("адрес всегда 127.0.0.1: хост окружением не меняется", async () => {
    vi.stubEnv("DND_SERVER_HOST", "evil.example");
    vi.stubEnv("DND_SERVER_URL", "http://evil.example:80");
    vi.stubEnv("SERVER_HOST", "evil.example");

    const overridden = await configWithServerPort("8123");

    expect(proxyOptions(overridden, "/api").target).toBe(
      "http://127.0.0.1:8123",
    );
    expect(proxyOptions(overridden, "/ws").target).toBe(
      "http://127.0.0.1:8123",
    );
  });
});

// Ключ прокси Vite — просто префикс адреса: «/api» совпал бы и с «/apixyz», а слова вроде
// «/api/../health» дошли бы до сервера в сыром виде. Решает bypass по нормализованному пути.
describe("какие пути уходят на сервер", () => {
  const allowed: ["/api" | "/ws", string][] = [
    ["/api", "/api"],
    ["/api", "/api/"],
    ["/api", "/api/me"],
    ["/api", "/api/games/join"],
    ["/api", "/api/games/ab12cd/characters/random-preview"],
    ["/api", "/api/games?limit=5"],
    ["/api", "/api/games/x%20y"],
    ["/ws", "/ws"],
    ["/ws", "/ws/"],
    ["/ws", "/ws/games/ab12cd?token=abc.def-ghi_jkl"],
  ];

  test.each(allowed)("%s: %s идёт на сервер", async (route, url) => {
    expect(await bypassOf(route, url)).toBeUndefined();
  });

  const refused: ["/api" | "/ws", string, string][] = [
    ["/api", "/apixyz", "чужое имя с тем же началом"],
    ["/api", "/api-docs", "чужое имя через дефис"],
    ["/api", "/api.json", "чужое имя через точку"],
    ["/ws", "/wsx", "чужое имя с тем же началом"],
    ["/ws", "/ws.js", "файл с тем же началом"],
    ["/api", "/api/../health", "выход из /api точками"],
    ["/api", "/api/../../x", "двойной выход"],
    ["/api", "/api/./me", "точка-сегмент"],
    ["/api", "/api/%2e%2e/health", "точки в процентной записи"],
    ["/api", "/api/%2E%2E/health", "точки в процентной записи, заглавные"],
    ["/api", "/api/.%2e/health", "половина точек в процентной записи"],
    ["/api", "/api/%2e/me", "точка-сегмент в процентной записи"],
    ["/api", "/api/..%2fhealth", "слэш в процентной записи"],
    ["/api", "/api/%252e%252e/health", "двойное кодирование"],
    ["/api", "/api/%zz", "битая процентная запись"],
    ["/api", "/api\\..\\health", "обратная косая черта"],
    ["/api", "/api/..\\health", "обратная косая в конце"],
    ["/api", "/api%5c..%5chealth", "обратная косая в процентной записи"],
    ["/api", "/api;/me", "матричный параметр после корня"],
    ["/api", "/api/me;x=1", "матричный параметр"],
    ["/api", "/api/me%3Bx=1", "матричный параметр в процентной записи"],
    ["/api", "//api/me", "двойной слэш в начале"],
    ["/api", "/api//me", "пустой сегмент"],
    ["/api", "/api/me\n", "управляющий знак"],
    ["/api", "/api/me%0a", "управляющий знак в процентной записи"],
    ["/ws", "/ws/../health", "выход из /ws точками"],
    ["/ws", "/ws;x", "матричный параметр у сокета"],
    ["/api", "http://evil.example/api/me", "полный адрес вместо пути"],
    ["/api", "api/me", "путь без начального слэша"],
    ["/api", "", "пустой адрес"],
  ];

  test.each(refused)("%s: %j не идёт на сервер (%s)", async (route, url) => {
    expect(await bypassOf(route, url)).toBe(false);
  });

  test("адреса нет — не идёт на сервер", async () => {
    expect(await bypassOf("/api", undefined)).toBe(false);
  });

  test("/api и /ws закрывают одни и те же пути", async () => {
    const urls = allowed.map(([, url]) => url);
    const refusedUrls = refused.map(([, url]) => url);

    for (const route of ["/api", "/ws"] as const) {
      const options = proxyOptions(config, route);
      for (const url of urls.filter((item) => item.startsWith(route))) {
        expect(
          options.bypass?.({ url } as IncomingMessage, undefined, options),
        ).toBeUndefined();
      }
      for (const url of refusedUrls.filter((item) => item.startsWith(route))) {
        expect(
          options.bypass?.({ url } as IncomingMessage, undefined, options),
        ).toBe(false);
      }
    }
  });
});

describe("dev-сервер", () => {
  test("порт занят — Vite останавливается, а не уходит на другой", () => {
    // Адрес dev-сервера попадает в WEBAPP_PUBLIC_URL и в команду cloudflared.
    expect(config.server?.strictPort).toBe(true);
  });

  test("файлы базы, данные и артефакты Playwright не раздаются", () => {
    expect(config.server?.fs?.deny).toEqual(
      expect.arrayContaining([
        "**/*.sqlite3*",
        "**/data/**",
        "**/playwright-report/**",
        "**/test-results/**",
      ]),
    );
  });

  // Своё значение deny заменяет список Vite, а не дополняет его: защита .env и .git
  // должна остаться, и при обновлении Vite отсутствующая запись видна сразу.
  test("встроенные запреты Vite сохранены", async () => {
    const vanilla = await resolveConfig(
      { root: WEBAPP_DIR, configFile: false, logLevel: "silent" },
      "serve",
    );

    expect(config.server?.fs?.deny).toEqual(
      expect.arrayContaining(vanilla.server.fs.deny),
    );
  });

  // Переменные из .env читает Vite только из папки webapp и отдаёт клиенту только
  // с префиксом VITE_. Корневой .env репозитория (токен бота, ключи) в сборку не попадает.
  test("env читается из папки webapp, а клиенту отдаётся только VITE_", async () => {
    const resolved = await resolveConfig(
      {
        root: WEBAPP_DIR,
        configFile: path.join(WEBAPP_DIR, "vite.config.ts"),
        logLevel: "silent",
      },
      "serve",
    );

    expect(resolved.envDir).toBe(WEBAPP_DIR);
    expect(resolveEnvPrefix({ envPrefix: resolved.envPrefix })).toEqual([
      "VITE_",
    ]);
  });

  test("редактор по /__open-in-editor из туннеля не открывается", () => {
    const guard = namedPlugins(config.plugins).find(
      (plugin) => plugin.name === "dnd:close-open-in-editor",
    );
    if (!guard) throw new Error("В plugins нет защиты /__open-in-editor");

    const use = vi.fn();
    (guard.configureServer as (server: unknown) => void)({
      middlewares: { use },
    });

    expect(use).toHaveBeenCalledTimes(1);
    const [route, handler] = use.mock.calls[0] as [
      string,
      (req: unknown, res: { statusCode: number; end: () => void }) => void,
    ];
    expect(route).toBe("/__open-in-editor");
    const res = { statusCode: 200, end: vi.fn() };
    handler({ url: "/?file=/etc/passwd" }, res);
    expect(res.statusCode).toBe(404);
    expect(res.end).toHaveBeenCalledTimes(1);
  });
});

describe("vitest", () => {
  test("сквозные тесты Playwright (e2e/) vitest не запускает", () => {
    // Файлы *.spec.ts в e2e/ vitest иначе принял бы за свои и упал на импорте Playwright.
    expect(config.test?.exclude).toEqual(
      expect.arrayContaining(["e2e/**", "**/node_modules/**"]),
    );
  });
});
