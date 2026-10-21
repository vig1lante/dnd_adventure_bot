import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { configDefaults, defineConfig } from "vitest/config";

// Клиент ходит на относительные /api/... и /ws; в dev их проксирует Vite.
const SERVER_HOST = "127.0.0.1";
const DEFAULT_SERVER_PORT = 8000;

/**
 * Порт сервера из `DND_SERVER_PORT`: сквозной тест поднимает свой сервер на свободном порту
 * (`dnd-server --port`), не трогая сервер на 8000. Пусто — порт по умолчанию. Мусор —
 * ошибка при старте Vite, а не молчаливый возврат к 8000: прокси ушёл бы на чужой сервер.
 */
export function serverPort(value: string | undefined): number {
  if (value === undefined || value === "") return DEFAULT_SERVER_PORT;
  const port = /^\d{1,5}$/.test(value) ? Number(value) : 0;
  if (port < 1 || port > 65535) {
    throw new Error(
      `DND_SERVER_PORT: нужен порт от 1 до 65535, получено ${JSON.stringify(value)}`,
    );
  }
  return port;
}

// Адрес всегда 127.0.0.1: переопределяется только порт, чтобы прокси не мог уйти на чужой хост.
const SERVER_URL = `http://${SERVER_HOST}:${serverPort(process.env.DND_SERVER_PORT)}`;

const PROXIED_ROOTS = new Set(["api", "ws"]);

/**
 * Идёт ли адрес запроса на сервер: ровно `/api`, `/ws` и всё под ними. Ключ прокси Vite —
 * просто начало адреса (`/api` совпал бы и с `/apixyz`), а сервер получает путь в сыром виде:
 * точки-сегменты, `%2e%2e`, обратная косая, `;` и двойное кодирование позволили бы дойти
 * до адресов, которые через Vite открывать не должны (`/health`, `/docs`, `/internal/…`).
 * Поэтому путь декодируется и проверяется целиком; сомнительное не пропускается.
 */
export function isProxiedPath(url: string | undefined): boolean {
  if (url === undefined) return false;
  let path: string;
  try {
    path = decodeURIComponent(url.split(/[?#]/, 1)[0]);
  } catch {
    return false; // битая процентная запись
  }
  // Знак «%» после декодирования — двойное кодирование; остальное — обход через `\`, `;`
  // и управляющие знаки.
  // eslint-disable-next-line no-control-regex
  if (!path.startsWith("/") || /[%\\;\u0000-\u001f\u007f]/.test(path)) {
    return false;
  }
  const segments = path.slice(1).split("/");
  if (segments.at(-1) === "") segments.pop(); // завершающий слэш
  return (
    PROXIED_ROOTS.has(segments[0]) &&
    segments.every((part) => part !== "" && part !== "." && part !== "..")
  );
}

// Прокси отвечает 404 на всё, что не /api и не /ws; Vite зовёт bypass и для WebSocket.
const bypass = (req: { url?: string }) =>
  isProxiedPath(req.url) ? undefined : false;

/**
 * Dev-сервер Vite, выставленный наружу туннелем, по `/__open-in-editor?file=…` запускает
 * редактор на этой машине. Туннелю это ни к чему: путь закрыт раньше встроенного обработчика.
 */
export function closeOpenInEditor(): Plugin {
  return {
    name: "dnd:close-open-in-editor",
    configureServer(server) {
      server.middlewares.use("/__open-in-editor", (_req, res) => {
        res.statusCode = 404;
        res.end();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), closeOpenInEditor()],
  server: {
    // Адрес dev-сервера идёт в WEBAPP_PUBLIC_URL и в команду cloudflared: если порт занят,
    // Vite должен остановиться, а не уйти молча на соседний.
    strictPort: true,
    fs: {
      // Свой список заменяет встроенный список Vite, поэтому его записи повторены здесь
      // (тест сверяет их с настоящими). Добавлены база SQLite, папка данных и артефакты
      // Playwright: их раздавать через dev-сервер нельзя.
      deny: [
        ".env",
        ".env.*",
        "*.{crt,pem,key,p12,pfx,cer,der}",
        ".npmrc",
        ".yarnrc.yml",
        "**/.git/**",
        "**/*.sqlite3*",
        "**/data/**",
        "**/playwright-report/**",
        "**/test-results/**",
      ],
    },
    proxy: {
      "/api": { target: SERVER_URL, changeOrigin: true, bypass },
      "/ws": { target: SERVER_URL, ws: true, bypass },
    },
  },
  test: {
    // Глобальные test/expect нужны, чтобы Testing Library сам чистил DOM
    // после каждого теста (он вешает cleanup на глобальный afterEach).
    globals: true,
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    // Сквозные тесты Playwright (e2e/*.spec.ts) запускает `npm run e2e`, а не vitest.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
