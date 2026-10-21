import http from "node:http";
import { expect, test } from "./server.ts";

// Прокси dev-сервера Vite (B1.security, B8.7): наружу, через туннель, пропускается только API
// игры. Юнит-тесты `vite-config.test.ts` проверяют функцию-фильтр на списке адресов; здесь
// фильтр проверен в работающем Vite перед настоящим сервером игры: то, что запрещено, не доходит
// до сервера, а запрос игрока идёт насквозь.

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
