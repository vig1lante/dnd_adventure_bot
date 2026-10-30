import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import type { Page } from "@playwright/test";
import { artifactsDir, expect, test } from "./server.ts";

// Прокси dev-сервера Vite: наружу, через туннель, пропускается только API игры. Юнит-тесты
// `vite-config.test.ts` проверяют функцию-фильтр на списке адресов; здесь фильтр проверен в
// работающем Vite перед настоящим сервером игры: то, что запрещено, не доходит до сервера, а
// запрос игрока идёт насквозь.

/**
 * Запрос с адресом ровно в том виде, как он записан. Клиенты вроде `fetch` разбирают адрес и
 * сворачивают `%2e%2e` в `..`, так что до Vite доходил бы уже другой путь; чужой клиент так не
 * церемонится.
 */
function rawGet(
  baseURL: string,
  path: string,
): Promise<{ status: number; body: string }> {
  const { hostname, port } = new URL(baseURL);
  return new Promise((resolve, reject) => {
    const request = http.request(
      { host: hostname, port: Number(port), path, method: "GET" },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk: string) => (body += chunk));
        response.on("end", () =>
          resolve({ status: response.statusCode ?? 0, body }),
        );
      },
    );
    request.once("error", reject);
    request.end();
  });
}

test("через Vite доступны только /api и /ws, служебные адреса сервера закрыты", async ({
  baseURL,
  request,
  server,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  // Контроль: сервер игры работает, и то, что закрыто через Vite, у него самого открыто.
  const direct = await fetch(`http://127.0.0.1:${server.port}/health`);
  expect(direct.status).toBe(200);
  expect(await direct.json()).toMatchObject({ status: "ok" });

  const config = await request.get("/api/config");
  expect(config.status()).toBe(200);
  expect(await config.json()).toMatchObject({ env: "dev", dev_login: true });

  // Адреса вне /api и /ws в сервер не идут вовсе: Vite отвечает страницей приложения.
  for (const path of [
    "/health",
    "/docs",
    "/openapi.json",
    "/internal/dm-tools/anything",
  ]) {
    const { body } = await rawGet(baseURL, path);
    expect(body, path).toContain('<div id="root">');
    expect(body, path).not.toContain('"status":"ok"');
    expect(body, path).not.toContain("swagger");
  }

  // Адреса, похожие на /api, но ведущие мимо него, закрыты отказом, а не пропущены к серверу.
  for (const path of [
    "/api/%2e%2e/health", // выход из /api через закодированные точки
    "/api/%2E%2E/health",
    "/api/..%2fhealth",
    "/api%2f..%2fhealth",
    "/api/..;/health",
    "/api\\..\\health",
    "/apixyz", // не /api и не его вложенные адреса
    "/__open-in-editor?file=/etc/hosts", // запуск редактора на машине разработчика
  ]) {
    const { status, body } = await rawGet(baseURL, path);
    expect(status, path).toBe(404);
    expect(body, path).not.toContain('"status":"ok"');
  }
});

/**
 * Открывает в странице сокет по адресу `path` (через Vite) с подпротоколами `protocols` и ждёт
 * первого кадра или закрытия. Код страницы — строка: в типах e2e нет DOM, а выполняет её браузер.
 */
const SOCKET_PROBE = `(path, protocols) => new Promise((resolve) => {
  const socket = new WebSocket("ws://" + location.host + path, protocols);
  socket.onmessage = (event) => {
    resolve({ protocol: socket.protocol, first: JSON.parse(event.data).type, code: null });
    socket.close();
  };
  socket.onclose = (event) => resolve({ protocol: socket.protocol, first: null, code: event.code });
})`;

/** Что увидел сокет: выбранный сервером подпротокол, тип первого кадра, код закрытия. */
interface SocketProbe {
  protocol: string;
  first: string | null;
  code: number | null;
}

async function probeSocket(
  page: Page,
  path: string,
  protocols: string[],
): Promise<SocketProbe> {
  const call = `(${SOCKET_PROBE})(${JSON.stringify(path)}, ${JSON.stringify(protocols)})`;
  return (await page.evaluate(call)) as SocketProbe;
}

test("сокет игры через Vite: токен идёт подпротоколом, а не в адресе; токен в адресе не принимается", async ({
  page,
  request,
}) => {
  const login = await request.post("/api/auth/dev", {
    data: { login: "dev-1" },
  });
  expect(login.ok()).toBe(true);
  const { token } = (await login.json()) as { token: string };
  const created = await request.post("/api/games", {
    headers: { Authorization: `Bearer ${token}` },
    data: { adventure_id: "demo-crypt" },
  });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  await page.goto("/");
  const socketPath = `/ws/games/${encodeURIComponent(id)}`;

  // Как открывает клиент: адрес без токена, подпротоколы [имя, токен]. Прокси Vite передаёт их
  // серверу, а сервер в ответ называет только имя — токен назад не возвращается.
  expect(await probeSocket(page, socketPath, ["dnd-game.v1", token])).toEqual({
    protocol: "dnd-game.v1",
    first: "snapshot",
    code: null,
  });

  // Прежний способ — токен в адресе — больше не пускает: 4401 и ни одного кадра.
  expect(
    await probeSocket(
      page,
      `${socketPath}?token=${encodeURIComponent(token)}`,
      [],
    ),
  ).toEqual({ protocol: "", first: null, code: 4401 });
});

/** Рукопожатие сокета глазами сети браузера: адрес, предложенные подпротоколы и ответ сервера. */
interface Handshake {
  url: string;
  offered?: string;
  status?: number;
  chosen?: string;
}

/** Значение заголовка без учёта регистра имени: браузер отдаёт имена как придётся. */
function headerOf(headers: Record<string, string>, name: string) {
  const found = Object.entries(headers).find(
    ([key]) => key.toLowerCase() === name,
  );
  return found?.[1];
}

test("Mini App открывает сокет игры без токена в адресе, и после перезапуска сервера тоже: токен только в Sec-WebSocket-Protocol, его нет в журналах и консоли", async ({
  page,
  request,
  baseURL,
  server,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const hostLogin = await request.post("/api/auth/dev", {
    data: { login: "dev-1" },
  });
  expect(hostLogin.ok()).toBe(true);
  const { token: hostToken } = (await hostLogin.json()) as { token: string };
  const created = await request.post("/api/games", {
    headers: { Authorization: `Bearer ${hostToken}` },
    data: { adventure_id: "demo-crypt" },
  });
  expect(created.status()).toBe(201);
  const { id, code } = (await created.json()) as { id: string; code: string };
  const socketUrl = `ws://${new URL(baseURL).host}/ws/games/${encodeURIComponent(id)}`;

  // Сеть браузера (протокол отладки Chromium): что настоящий клиент шлёт при открытии сокета.
  // Сокет Vite для горячей замены модулей тоже здесь — его отсекает адрес.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  const handshakes = new Map<string, Handshake>();
  cdp.on("Network.webSocketCreated", ({ requestId, url }) => {
    handshakes.set(requestId, { url });
  });
  cdp.on(
    "Network.webSocketWillSendHandshakeRequest",
    ({ requestId, request: sent }) => {
      const shake = handshakes.get(requestId);
      if (shake !== undefined) {
        shake.offered = headerOf(sent.headers, "sec-websocket-protocol");
      }
    },
  );
  cdp.on(
    "Network.webSocketHandshakeResponseReceived",
    ({ requestId, response }) => {
      const shake = handshakes.get(requestId);
      if (shake !== undefined) {
        shake.status = response.status;
        shake.chosen = headerOf(response.headers, "sec-websocket-protocol");
      }
    },
  );
  const printed: string[] = [];
  page.on("console", (message) => printed.push(message.text()));
  page.on("pageerror", (error) => printed.push(String(error)));
  const gameShakes = () =>
    [...handshakes.values()].filter((shake) =>
      new URL(shake.url).pathname.startsWith("/ws/"),
    );
  const accepted = () =>
    gameShakes().filter((shake) => shake.status === 101).length;

  // Mini App входит dev-игроком по коду приглашения и открывает лобби.
  const loggedIn = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/auth/dev",
  );
  await page.goto(`/?dev=dev-2&join=${code}`);
  const { token } = (await (await loggedIn).json()) as { token: string };
  await expect(page.getByText("Ваш герой")).toBeVisible();
  await expect.poll(accepted).toBe(1);

  // Сервер перезапустился: клиент сам открывает сокет заново тем же способом.
  await server.restart();
  await expect.poll(accepted, { timeout: 60_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("Ваш герой")).toBeVisible();

  // Сводка без самого токена: в сообщение об ошибке теста он попасть не должен. Попытка, оборванная
  // во время перезапуска, может не успеть показать заголовки рукопожатия — тогда их нет вовсе.
  const offeredKind = (offered: string | undefined) => {
    if (offered === undefined) return "нет заголовка";
    const parts = offered.split(",").map((part) => part.trim());
    return parts.length === 2 &&
      parts[0] === "dnd-game.v1" &&
      parts[1] === token
      ? "имя и токен"
      : "другое";
  };
  const summary = gameShakes().map((shake) => ({
    status: shake.status ?? null,
    offered: offeredKind(shake.offered),
    chosen: shake.chosen ?? null,
  }));
  const described = JSON.stringify(summary);
  for (const shake of gameShakes()) {
    expect(shake.url, "адрес сокета без токена и query").toBe(socketUrl);
  }
  expect(
    summary.every(({ offered }) => offered !== "другое"),
    described,
  ).toBe(true);
  const opened = summary.filter(({ status }) => status === 101);
  expect(opened.length, described).toBeGreaterThanOrEqual(2);
  expect(
    opened.every(
      ({ offered, chosen }) =>
        offered === "имя и токен" && chosen === "dnd-game.v1",
    ),
    described,
  ).toBe(true);
  // Токен не попал ни в журнал сервера, ни в журнал Vite, ни в консоль страницы.
  for (const log of ["server.log", "vite.log"]) {
    const text = fs.readFileSync(path.join(artifactsDir(), log), "utf8");
    expect(text.includes(token), log).toBe(false);
  }
  expect(printed.some((line) => line.includes(token))).toBe(false);
});
