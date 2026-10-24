import {
  closingBehavior,
  mockTelegramEnv,
  themeParams,
} from "@tma.js/sdk-react";
import { ApiError, createApi, errorText } from "../api/client";
import type { Api } from "../api/client";
import type { GameSummary, HostAction } from "../api/types";
import { sessionToken, useGame } from "../store/game";
import { fakeServer } from "../test-setup";
import type { FakeReply } from "../test-setup";
import { DEV_LOGINS, devLoginFromSearch } from "./dev";
import { parseJoinCode, signInAsDev, startSession } from "./session";
import { readInitData, readStartParam, setupTelegram } from "./telegram";

// Выдуманный initData: сервер его здесь не проверяет, нужна только узнаваемая строка,
// чтобы искать её в адресах, хранилищах и журналах.
const INIT_DATA =
  "query_id=AAHdF6IQ&user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Lira%22%7D" +
  "&auth_date=1662771648&signature=sig&hash=abc123";

const LOGIN = {
  token: "tok-secret-123",
  user: { id: "u1", name: "dev-3", can_create_games: true },
  start_param: null,
};

function gameSummary(overrides: Partial<GameSummary> = {}): GameSummary {
  return {
    id: "g1",
    code: "ABC123",
    adventure_id: "demo-crypt",
    adventure_title: "Склеп",
    phase: "lobby",
    turn_phase: null,
    provider: null,
    model: null,
    collect_seconds: 120,
    hurry_seconds: 60,
    camp_seconds: 300,
    profanity: true,
    players: [],
    ...overrides,
  };
}

// Клиент, подключённый к хранилищу так же, как в приложении.
function apiFor(server: { fetch: typeof fetch }): Api {
  return createApi(server.fetch, sessionToken);
}

describe("ApiError: русский текст по коду статуса", () => {
  async function failure(reply: FakeReply) {
    const server = fakeServer({ "GET /api/me": reply });
    return createApi(server.fetch)
      .getMe()
      .then(
        () => {
          throw new Error("запрос должен был завершиться ошибкой");
        },
        (error: unknown) => error,
      );
  }

  test("несёт код, текст и сообщение об ошибке", async () => {
    const error = await failure({
      status: 409,
      json: { detail: "Игра уже началась." },
    });

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({
      status: 409,
      detail: "Игра уже началась.",
      message: "Игра уже началась.",
    });
  });

  test("русский detail сервера из JSON показывается как есть", async () => {
    const error = await failure({
      status: 404,
      json: { detail: "Игра с таким кодом не найдена." },
    });

    expect((error as ApiError).detail).toBe("Игра с таким кодом не найдена.");
  });

  test.each([
    // 404 и 405 отдаёт сам Starlette: у них английский detail.
    [404, { json: { detail: "Not Found" } }, "Не найдено."],
    [
      405,
      { json: { detail: "Method Not Allowed" } },
      "Сервер не поддерживает это действие.",
    ],
    // 400 от TrustedHost — обычный текст, не JSON.
    [
      400,
      { text: "Invalid host header" },
      "Сервер не принял запрос: проверьте адрес приложения.",
    ],
    [
      500,
      { text: "Internal Server Error" },
      "Внутренняя ошибка сервера. Попробуйте ещё раз немного позже.",
    ],
    // 502 и 530 отдаёт туннель или прокси страницей HTML.
    [
      502,
      {
        text: "<html><body>Bad Gateway</body></html>",
        contentType: "text/html",
      },
      "Сервер временно недоступен. Попробуйте ещё раз немного позже.",
    ],
    [
      530,
      { text: "<html>error code: 1033</html>", contentType: "text/html" },
      "Сервер временно недоступен. Попробуйте ещё раз немного позже.",
    ],
    [418, { text: "" }, "Не удалось выполнить запрос (код 418)."],
  ] satisfies [number, FakeReply, string][])(
    "код %i без русского detail даёт русский текст по статусу",
    async (status, reply, expected) => {
      const error = await failure({ ...reply, status });

      expect(error).toMatchObject({ status, detail: expected });
    },
  );

  test("detail не строкой тоже заменяется текстом по статусу", async () => {
    const error = await failure({
      status: 422,
      json: { detail: [{ msg: "field required", loc: ["body", "code"] }] },
    });

    expect((error as ApiError).detail).toBe("Некорректные данные запроса.");
  });

  test("problems у 422 — список строк, остальное отбрасывается", async () => {
    const error = await failure({
      status: 422,
      json: {
        detail: "Выбор персонажа недопустим.",
        problems: ["Класс уже занят.", 42, null, "Не хватает навыка."],
      },
    });

    expect((error as ApiError).problems).toEqual([
      "Класс уже занят.",
      "Не хватает навыка.",
    ]);
  });

  test("problems вне 422 игнорируются", async () => {
    const error = await failure({
      status: 409,
      json: { detail: "Игра уже началась.", problems: ["что-то"] },
    });

    expect((error as ApiError).problems).toBeUndefined();
  });

  test("обрыв связи — ошибка с кодом 0 и русским текстом", async () => {
    const offline = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    const error = await createApi(offline as unknown as typeof fetch)
      .getMe()
      .catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      status: 0,
      detail: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    });
  });

  test("успешный ответ не в JSON — ошибка, а не пустой результат", async () => {
    const error = await failure({ status: 200, text: "<html>tunnel</html>" });

    expect(error).toMatchObject({
      status: 200,
      detail: "Сервер вернул непонятный ответ.",
    });
  });

  test("errorText: текст ApiError или общая фраза для чужой ошибки", () => {
    expect(errorText(new ApiError(409, "Игра уже началась."))).toBe(
      "Игра уже началась.",
    );
    expect(errorText(new RangeError("boom"))).toBe(
      "Что-то пошло не так. Попробуйте ещё раз.",
    );
  });
});

describe("клиент API: контракт сервера", () => {
  const draft = { text: "Крадусь к двери", secret: true };
  const quick = {
    mode: "quick",
    class_id: "rogue",
    species: "human",
    concept: "Вор-неудачник",
  } as const;
  const help = { class_id: "rogue", species: "human", concept: "", draft: "" };
  const hostActions: HostAction[] = [
    "collect",
    "extend",
    "close",
    "pause",
    "resume",
    "retry",
    "regenerate",
    "hurry",
    "camp-extend",
    "camp-end",
  ];

  const calls: [
    string,
    (api: Api) => Promise<unknown>,
    string,
    string,
    unknown?,
  ][] = [
    ["getMe", (a) => a.getMe(), "GET", "/api/me"],
    ["listAdventures", (a) => a.listAdventures(), "GET", "/api/adventures"],
    ["listGames", (a) => a.listGames(), "GET", "/api/games"],
    [
      "createGame",
      (a) => a.createGame({ adventure_id: "demo-crypt", collect_seconds: 90 }),
      "POST",
      "/api/games",
      { adventure_id: "demo-crypt", collect_seconds: 90 },
    ],
    [
      "joinGame",
      (a) => a.joinGame("ABC123"),
      "POST",
      "/api/games/join",
      { code: "ABC123" },
    ],
    ["getGame", (a) => a.getGame("g1"), "GET", "/api/games/g1"],
    [
      "setReady",
      (a) => a.setReady("g1", true),
      "POST",
      "/api/games/g1/ready",
      { ready: true },
    ],
    ["startGame", (a) => a.startGame("g1"), "POST", "/api/games/g1/start"],
    ["leaveGame", (a) => a.leaveGame("g1"), "POST", "/api/games/g1/leave"],
    [
      "kickPlayer",
      (a) => a.kickPlayer("g1", "u2"),
      "POST",
      "/api/games/g1/players/u2/kick",
    ],
    [
      "getCreationOptions",
      (a) => a.getCreationOptions(),
      "GET",
      "/api/content/creation-options",
    ],
    [
      "randomAbilities",
      (a) => a.randomAbilities("g1", "wizard"),
      "POST",
      "/api/games/g1/characters/random-abilities",
      { class_id: "wizard" },
    ],
    [
      "myCharacter",
      (a) => a.myCharacter("g1"),
      "GET",
      "/api/games/g1/characters/mine",
    ],
    [
      "randomPreview",
      (a) => a.randomPreview("g1"),
      "POST",
      "/api/games/g1/characters/random-preview",
    ],
    [
      "createCharacter",
      (a) => a.createCharacter("g1", quick),
      "POST",
      "/api/games/g1/characters",
      quick,
    ],
    [
      "backstoryHelp",
      (a) => a.backstoryHelp("g1", help),
      "POST",
      "/api/games/g1/characters/backstory-help",
      help,
    ],
    [
      "saveDraft",
      (a) => a.saveDraft("g1", draft),
      "PUT",
      "/api/games/g1/action",
      draft,
    ],
    [
      "submitAction",
      (a) => a.submitAction("g1"),
      "POST",
      "/api/games/g1/action/submit",
    ],
    [
      "passAction",
      (a) => a.passAction("g1"),
      "POST",
      "/api/games/g1/action/pass",
    ],
    [
      "sendCampLine",
      (a) => a.sendCampLine("g1", "Холодает"),
      "POST",
      "/api/games/g1/camp/line",
      { text: "Холодает" },
    ],
    ...hostActions.map(
      (action) =>
        [
          `hostAction(${action})`,
          (a: Api) => a.hostAction("g1", action),
          "POST",
          `/api/games/g1/host/${action}`,
        ] as [string, (a: Api) => Promise<unknown>, string, string],
    ),
  ];

  test.each(calls)(
    "%s: метод, путь, тело, Bearer и адрес без query",
    async (_name, call, method, path, body) => {
      const server = fakeServer({ [`${method} ${path}`]: { json: {} } });
      useGame.getState().setSession({ token: LOGIN.token, user: LOGIN.user });

      await call(apiFor(server));

      expect(server.calls).toHaveLength(1);
      const sent = server.calls[0];
      expect(sent.method).toBe(method);
      expect(sent.path).toBe(path);
      expect(sent.body).toEqual(body);
      expect(sent.headers.get("Authorization")).toBe(`Bearer ${LOGIN.token}`);
      // Токен и initData никогда не попадают в адрес: Vite пишет его в журнал.
      expect(sent.url).toBe(path);
    },
  );

  test("метода броска характеристик в клиенте нет: раскладку очков даёт только сервер", () => {
    const api = createApi(fakeServer({}).fetch);

    expect(Object.keys(api)).not.toContain("rollAbilities");
    expect(Object.keys(api)).toContain("randomAbilities");
  });

  test("запросы без токена уходят без заголовка Authorization", async () => {
    const server = fakeServer({ "GET /api/config": { json: {} } });

    await createApi(server.fetch).getConfig();

    expect(server.calls[0].headers.has("Authorization")).toBe(false);
  });

  test("запрос игрока без сессии не шлёт «Bearer null»: заголовка нет вовсе", async () => {
    const server = fakeServer({
      "GET /api/me": { status: 401, json: { detail: "Нужно войти." } },
    });

    await expect(createApi(server.fetch).getMe()).rejects.toMatchObject({
      status: 401,
    });

    expect(server.calls[0].headers.has("Authorization")).toBe(false);
  });

  test("запрос без тела уходит без Content-Type, с телом — с JSON", async () => {
    const server = fakeServer({
      "GET /api/me": { json: LOGIN.user },
      "POST /api/games/join": { json: {} },
    });
    useGame.getState().setSession({ token: LOGIN.token, user: LOGIN.user });
    const api = apiFor(server);

    await api.getMe();
    await api.joinGame("ABC123");

    expect(server.calls[0].headers.has("Content-Type")).toBe(false);
    expect(server.calls[1].headers.get("Content-Type")).toBe(
      "application/json",
    );
  });

  test("getConfig не отправляет токен, даже если он есть", async () => {
    const server = fakeServer({ "GET /api/config": { json: {} } });
    useGame.getState().setSession({ token: LOGIN.token, user: LOGIN.user });

    await apiFor(server).getConfig();

    expect(server.calls[0].headers.has("Authorization")).toBe(false);
  });

  test("вход Telegram: заголовок «tma <initData>», без Bearer и тела", async () => {
    const server = fakeServer({ "POST /api/auth/telegram": { json: LOGIN } });
    useGame.getState().setSession({ token: "old-token", user: LOGIN.user });

    const result = await apiFor(server).loginTelegram(INIT_DATA);

    expect(result).toEqual(LOGIN);
    const sent = server.calls[0];
    expect(sent.headers.get("Authorization")).toBe(`tma ${INIT_DATA}`);
    expect(sent.body).toBeUndefined();
    expect(sent.url).toBe("/api/auth/telegram");
  });

  test("dev-вход: POST /api/auth/dev с {login} в теле и без Authorization", async () => {
    const server = fakeServer({ "POST /api/auth/dev": { json: LOGIN } });
    useGame.getState().setSession({ token: "old-token", user: LOGIN.user });

    await apiFor(server).loginDev("dev-3");

    const sent = server.calls[0];
    expect(sent.body).toEqual({ login: "dev-3" });
    expect(sent.headers.get("Content-Type")).toBe("application/json");
    expect(sent.headers.has("Authorization")).toBe(false);
  });

  test("идентификаторы в пути экранируются", async () => {
    const server = fakeServer({ "GET /api/games/a%2Fb%3Fc": { json: {} } });

    await createApi(server.fetch).getGame("a/b?c");

    expect(server.calls[0].url).toBe("/api/games/a%2Fb%3Fc");
  });

  test("ответ 204 (выход из лобби) даёт undefined", async () => {
    const server = fakeServer({ "POST /api/games/g1/leave": { status: 204 } });

    await expect(
      createApi(server.fetch).leaveGame("g1"),
    ).resolves.toBeUndefined();
  });

  test("fetch берётся из глобального объекта в момент вызова", async () => {
    const server = fakeServer({
      "GET /api/config": { json: { dev_login: true } },
    });
    const api = createApi();
    vi.stubGlobal("fetch", server.fetch);

    await expect(api.getConfig()).resolves.toEqual({ dev_login: true });
  });
});

describe("Telegram SDK", () => {
  function openInTelegram(startParam?: string) {
    mockTelegramEnv({
      launchParams: {
        tgWebAppData: INIT_DATA,
        tgWebAppVersion: "8.0",
        tgWebAppPlatform: "tdesktop",
        tgWebAppThemeParams: { bg_color: "#17212b", text_color: "#f5f5f5" },
        tgWebAppStartParam: startParam,
      },
    });
  }

  test("вне Telegram initData и start_param нет (SDK бросает — мы отдаём null)", () => {
    expect(readInitData()).toBeNull();
    expect(readStartParam()).toBeNull();
  });

  test("внутри Telegram initData отдаётся строкой без изменений", () => {
    openInTelegram();

    expect(readInitData()).toBe(INIT_DATA);
    expect(readStartParam()).toBeNull();
  });

  test("start_param берётся из параметров запуска (tgWebAppStartParam)", () => {
    openInTelegram("ABC123");

    expect(readStartParam()).toBe("ABC123");
  });

  test("setupTelegram: SDK запущен, тема подключена, подтверждение закрытия включено", () => {
    openInTelegram();

    setupTelegram();

    expect(themeParams.isMounted()).toBe(true);
    expect(themeParams.isDark()).toBe(true);
    expect(
      document.documentElement.style.getPropertyValue("--tg-theme-bg-color"),
    ).toBe("#17212b");
    expect(closingBehavior.isConfirmationEnabled()).toBe(true);
  });

  test("сбой шага SDK не ломает запуск и не пишет текст ошибки в журнал", () => {
    openInTelegram();
    // В тексте ошибок SDK бывают параметры запуска целиком, то есть initData.
    vi.spyOn(themeParams, "mount").mockImplementation(() => {
      throw new Error(`Invalid launch params: ${INIT_DATA}`);
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const enableClosingConfirmation = vi.spyOn(
      closingBehavior,
      "enableConfirmation",
    );

    expect(() => setupTelegram()).not.toThrow();

    expect(warn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(warn.mock.calls)).not.toContain("query_id");
    // Остальные шаги выполнены, несмотря на сбой темы.
    expect(enableClosingConfirmation).toHaveBeenCalledTimes(1);
  });

  test("setupTelegram вне Telegram не падает, а в журнале нет initData", () => {
    const logs = (["log", "info", "warn", "error", "debug"] as const).map(
      (method) => vi.spyOn(console, method).mockImplementation(() => {}),
    );

    expect(() => setupTelegram()).not.toThrow();

    for (const log of logs) {
      expect(JSON.stringify(log.mock.calls)).not.toContain("query_id");
    }
  });
});

describe("dev-вход: ?dev=", () => {
  test("шесть логинов dev-1…dev-6", () => {
    expect(DEV_LOGINS).toEqual([
      "dev-1",
      "dev-2",
      "dev-3",
      "dev-4",
      "dev-5",
      "dev-6",
    ]);
  });

  test.each([
    ["?dev=dev-3", "dev-3"],
    ["?x=1&dev=dev-6", "dev-6"],
    ["?dev=dev-7", null],
    ["?dev=dev-0", null],
    ["?dev=DEV-3", null],
    ["?dev=dev-3%20", null],
    ["?dev=", null],
    ["", null],
  ])("devLoginFromSearch(%j) -> %j", (search, expected) => {
    expect(devLoginFromSearch(search)).toBe(expected);
  });
});

describe("parseJoinCode: формат кода приглашения", () => {
  test.each([
    ["ABC123", "ABC123"],
    [" abc123 ", "ABC123"],
    ["a1b2c3", "A1B2C3"],
    ["AB12", null],
    ["ABC1234", null],
    ["ABC 12", null],
    ["ABC-12", null],
    ["ABCДЕ1", null],
    ["ıııııı", null],
    ["", null],
    [null, null],
    [undefined, null],
  ])("parseJoinCode(%j) -> %j", (value, expected) => {
    expect(parseJoinCode(value)).toBe(expected);
  });
});

describe("вход в приложение (startSession)", () => {
  const config = (dev_login: boolean): FakeReply => ({
    json: { env: dev_login ? "dev" : "prod", dev_login, bot_username: null },
  });

  test("в Telegram: POST /api/auth/telegram с «tma <initData>», без запроса конфигурации", async () => {
    const server = fakeServer({ "POST /api/auth/telegram": { json: LOGIN } });

    const result = await startSession({
      api: apiFor(server),
      initData: INIT_DATA,
      launchStartParam: null,
      search: "",
    });

    expect(result).toBe("signed-in");
    expect(server.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "POST /api/auth/telegram",
    ]);
    expect(server.calls[0].headers.get("Authorization")).toBe(
      `tma ${INIT_DATA}`,
    );
    expect(useGame.getState()).toMatchObject({
      screen: "games",
      gameId: null,
      session: { token: LOGIN.token, user: LOGIN.user },
    });
  });

  test("в Telegram отказ входа — ошибка наружу, без отката на dev-вход", async () => {
    const server = fakeServer({
      "POST /api/auth/telegram": {
        status: 401,
        json: { detail: "Не удалось проверить вход через Telegram." },
      },
    });

    await expect(
      startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: null,
        search: "?dev=dev-3",
      }),
    ).rejects.toMatchObject({ status: 401 });

    expect(server.calls).toHaveLength(1);
    expect(useGame.getState().session).toBeNull();
    expect(useGame.getState().screen).toBe("login");
  });

  test("вне Telegram при dev_login — выбор логина, вход не выполняется", async () => {
    const server = fakeServer({ "GET /api/config": config(true) });

    const result = await startSession({
      api: apiFor(server),
      initData: null,
      launchStartParam: null,
      search: "",
    });

    expect(result).toBe("choose-dev");
    expect(server.calls.map((c) => c.path)).toEqual(["/api/config"]);
    expect(useGame.getState().session).toBeNull();
  });

  test("?dev=dev-3 входит сразу", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": { json: LOGIN },
    });

    const result = await startSession({
      api: apiFor(server),
      initData: null,
      launchStartParam: null,
      search: "?dev=dev-3",
    });

    expect(result).toBe("signed-in");
    expect(server.calls[1]).toMatchObject({
      method: "POST",
      path: "/api/auth/dev",
      body: { login: "dev-3" },
    });
    expect(useGame.getState().screen).toBe("games");
  });

  test("?dev= с неизвестным логином не отправляется: остаётся выбор", async () => {
    const server = fakeServer({ "GET /api/config": config(true) });

    const result = await startSession({
      api: apiFor(server),
      initData: null,
      launchStartParam: null,
      search: "?dev=dev-9",
    });

    expect(result).toBe("choose-dev");
    expect(server.calls).toHaveLength(1);
  });

  test("без Telegram и без dev_login — подсказка, даже если в адресе ?dev=", async () => {
    const server = fakeServer({ "GET /api/config": config(false) });

    const result = await startSession({
      api: apiFor(server),
      initData: null,
      launchStartParam: null,
      search: "?dev=dev-3",
    });

    expect(result).toBe("telegram-required");
    expect(server.calls.map((c) => c.path)).toEqual(["/api/config"]);
    expect(useGame.getState().session).toBeNull();
  });

  test("ошибка запроса конфигурации приходит наружу (экран повторит попытку)", async () => {
    const server = fakeServer({
      "GET /api/config": { status: 502, text: "<html>Bad Gateway</html>" },
    });

    await expect(
      startSession({
        api: apiFor(server),
        initData: null,
        launchStartParam: null,
        search: "",
      }),
    ).rejects.toMatchObject({ status: 502 });
  });

  test("signInAsDev выбирает игрока из списка и входит как он", async () => {
    const server = fakeServer({ "POST /api/auth/dev": { json: LOGIN } });

    await signInAsDev({ api: apiFor(server), search: "" }, "dev-2");

    expect(server.calls[0].body).toEqual({ login: "dev-2" });
    expect(useGame.getState().session?.token).toBe(LOGIN.token);
  });

  describe("после входа: приглашение", () => {
    const joined = (overrides: Partial<GameSummary> = {}): FakeReply => ({
      json: gameSummary(overrides),
    });

    async function devStart(search: string, routes: Record<string, FakeReply>) {
      const server = fakeServer({
        "GET /api/config": config(true),
        "POST /api/auth/dev": { json: LOGIN },
        ...routes,
      });
      await startSession({
        api: apiFor(server),
        initData: null,
        launchStartParam: null,
        search,
      });
      return server;
    }

    test("?join=ABC123 вызывает join с токеном и открывает лобби", async () => {
      const server = await devStart("?dev=dev-3&join=ABC123", {
        "POST /api/games/join": joined(),
      });

      const join = server.calls.find((c) => c.path === "/api/games/join");
      expect(join?.body).toEqual({ code: "ABC123" });
      expect(join?.headers.get("Authorization")).toBe(`Bearer ${LOGIN.token}`);
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
        notice: null,
      });
    });

    test("код нормализуется: пробелы и регистр не важны", async () => {
      const server = await devStart("?dev=dev-3&join=%20abc123", {
        "POST /api/games/join": joined(),
      });

      expect(
        server.calls.find((c) => c.path === "/api/games/join")?.body,
      ).toEqual({ code: "ABC123" });
    });

    test("?join= неверного вида не отправляется на сервер", async () => {
      const server = await devStart("?dev=dev-3&join=bad", {});

      expect(server.calls.some((c) => c.path === "/api/games/join")).toBe(
        false,
      );
      expect(useGame.getState().screen).toBe("games");
    });

    test("игра, которая уже идёт, открывается экраном игры", async () => {
      await devStart("?dev=dev-3&join=ABC123", {
        "POST /api/games/join": joined({ phase: "playing" }),
      });

      expect(useGame.getState()).toMatchObject({
        screen: "game",
        gameId: "g1",
      });
    });

    test("игра в начале (weaving) открывается лобби: там экран вплетения, как из «Мои игры»", async () => {
      await devStart("?dev=dev-3&join=ABC123", {
        "POST /api/games/join": joined({ phase: "weaving" }),
      });

      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
      });
    });

    test.each(["paused", "finished"] as const)(
      "игра в фазе %s открывается экраном игры",
      async (phase) => {
        await devStart("?dev=dev-3&join=ABC123", {
          "POST /api/games/join": joined({ phase }),
        });

        expect(useGame.getState()).toMatchObject({
          screen: "game",
          gameId: "g1",
        });
      },
    );

    test("отказ join: вход состоялся, на экране «Мои игры» сообщение сервера", async () => {
      await devStart("?dev=dev-3&join=ABC123", {
        "POST /api/games/join": {
          status: 404,
          json: { detail: "Игра с таким кодом не найдена." },
        },
      });

      expect(useGame.getState()).toMatchObject({
        screen: "games",
        notice: "Игра с таким кодом не найдена.",
        session: { token: LOGIN.token },
      });
    });

    test("start_param из ответа сервера открывает лобби (Telegram)", async () => {
      const server = fakeServer({
        "POST /api/auth/telegram": {
          json: { ...LOGIN, start_param: "ABC123" },
        },
        "POST /api/games/join": joined(),
      });

      await startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: null,
        search: "",
      });

      expect(
        server.calls.find((c) => c.path === "/api/games/join")?.body,
      ).toEqual({ code: "ABC123" });
      expect(useGame.getState().screen).toBe("lobby");
    });

    test("start_param из параметров запуска SDK — запасной источник", async () => {
      const server = fakeServer({
        "POST /api/auth/telegram": { json: LOGIN },
        "POST /api/games/join": joined(),
      });

      await startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: "zzz999",
        search: "",
      });

      expect(
        server.calls.find((c) => c.path === "/api/games/join")?.body,
      ).toEqual({ code: "ZZZ999" });
    });

    test("start_param, не похожий на код, игнорируется", async () => {
      const server = fakeServer({
        "POST /api/auth/telegram": {
          json: { ...LOGIN, start_param: "promo_2026" },
        },
      });

      await startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: null,
        search: "",
      });

      expect(server.calls).toHaveLength(1);
      expect(useGame.getState().screen).toBe("games");
    });

    test("выбор dev-логина тоже принимает ?join=", async () => {
      const server = fakeServer({
        "POST /api/auth/dev": { json: LOGIN },
        "POST /api/games/join": joined(),
      });

      await signInAsDev(
        { api: apiFor(server), search: "?join=ABC123" },
        "dev-5",
      );

      expect(useGame.getState().screen).toBe("lobby");
    });
  });

  describe("секреты", () => {
    test("токен и initData не попадают в хранилища браузера и журнал консоли", async () => {
      const logs = (["log", "info", "warn", "error", "debug"] as const).map(
        (method) => vi.spyOn(console, method).mockImplementation(() => {}),
      );
      const server = fakeServer({
        "POST /api/auth/telegram": {
          json: { ...LOGIN, start_param: "ABC123" },
        },
        "POST /api/games/join": {
          status: 404,
          json: { detail: "Игра с таким кодом не найдена." },
        },
      });

      await startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: null,
        search: "",
      });

      expect(window.localStorage).toHaveLength(0);
      expect(window.sessionStorage).toHaveLength(0);
      const logged = JSON.stringify(logs.map((log) => log.mock.calls));
      expect(logged).not.toContain(LOGIN.token);
      expect(logged).not.toContain("query_id");
    });

    test("в адресах запросов нет токена и initData", async () => {
      const server = fakeServer({
        "POST /api/auth/telegram": {
          json: { ...LOGIN, start_param: "ABC123" },
        },
        "POST /api/games/join": { json: gameSummary() },
      });

      await startSession({
        api: apiFor(server),
        initData: INIT_DATA,
        launchStartParam: null,
        search: "",
      });

      for (const call of server.calls) {
        expect(call.url).not.toContain("?");
        expect(call.url).not.toContain(LOGIN.token);
        expect(call.url).not.toContain("query_id");
      }
    });
  });
});
