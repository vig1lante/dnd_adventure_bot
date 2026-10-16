import config from "./vite.config.ts";

const SERVER_URL = "http://127.0.0.1:8000";

// Vite принимает прокси строкой-сокращением или объектом; приводим к объекту.
function proxyOptions(path: string) {
  const entry = config.server?.proxy?.[path];
  if (entry === undefined) {
    throw new Error(`В server.proxy нет правила для ${path}`);
  }
  return typeof entry === "string" ? { target: entry } : entry;
}

// Клиент ходит только на относительные /api/... и /ws; в dev их должен
// проксировать Vite, иначе запросы не дойдут до сервера.
describe("прокси dev-сервера", () => {
  test("/api идёт на сервер", () => {
    expect(proxyOptions("/api").target).toBe(SERVER_URL);
  });

  test("/ws идёт на сервер с поддержкой WebSocket", () => {
    const options = proxyOptions("/ws");

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
