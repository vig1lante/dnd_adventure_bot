import { act, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import type { ReactElement } from "react";
import App from "./App";
import type { AppDeps } from "./App";
import { OPTIONS } from "./screens/character/fixtures";
import { useGame } from "./store/game";
import {
  fakeServer,
  fakeWebSocket,
  gameSummary,
  lobbyPlayer,
  snapshotOf,
} from "./test-setup";
import type { FakeReply, RecordedCall } from "./test-setup";

const INIT_DATA = "query_id=AAHdF6IQ&auth_date=1662771648&hash=abc123";
const HINT = "Откройте приложение из Telegram";

// Токен сервера — ASCII (заголовки HTTP других не принимают), имя игрока — любое.
const login = (name: string) => ({
  token: `tok-${encodeURIComponent(name)}`,
  user: { id: `id-${encodeURIComponent(name)}`, name, can_create_games: true },
  start_param: null,
});

const config = (dev_login: boolean): FakeReply => ({
  json: { env: dev_login ? "dev" : "prod", dev_login, bot_username: null },
});

// Вход как dev-N: ответ сервера зависит от присланного логина.
const devLogin = (call: RecordedCall): FakeReply => ({
  json: login((call.body as { login: string }).login),
});

const lobbyGame = {
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
  short_rest_seconds: 120,
  players: [],
};

function renderApp(
  server: { fetch: typeof fetch },
  deps: Partial<AppDeps> = {},
  wrap: (app: ReactElement) => ReactElement = (app) => app,
) {
  return render(
    wrap(
      <App
        deps={{
          fetchImpl: server.fetch,
          readInitData: () => null,
          readStartParam: () => null,
          setupTelegram: () => {},
          ...deps,
        }}
      />,
    ),
  );
}

function openUrl(search: string) {
  window.history.replaceState(null, "", `/${search}`);
}

// «Мои игры» при открытии просят список игр; в большинстве тестов он пуст.
const NO_GAMES: FakeReply = { json: [] };

const promptServer = () => fakeServer({ "GET /api/config": config(false) });

test("показывает название приложения", async () => {
  renderApp(promptServer());
  await screen.findByText(HINT);

  expect(screen.getByText("DnD Adventure Bot")).toBeInTheDocument();
});

test("название приложения — заголовок первого уровня", async () => {
  renderApp(promptServer());
  await screen.findByText(HINT);

  expect(
    screen.getByRole("heading", { level: 1, name: "DnD Adventure Bot" }),
  ).toBeInTheDocument();
});

// Testing Library сам чистит DOM после каждого теста, только если test/expect
// глобальные (test.globals в vite.config.ts). Тест рендерит App сам и смотрит
// на DOM уже после очистки: onTestFinished срабатывает позже afterEach-хуков,
// поэтому результат не зависит от соседних тестов, порядка и фильтра -t.
test("DOM после теста очищается автоматически", async () => {
  renderApp(promptServer());
  await screen.findByText(HINT);
  expect(screen.getByRole("heading")).toBeInTheDocument();

  onTestFinished(() => {
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});

describe("вход вне Telegram", () => {
  test("без Telegram и без dev — экран-подсказка и ни одного запроса входа", async () => {
    const server = promptServer();
    openUrl("?dev=dev-3");

    renderApp(server);

    expect(await screen.findByText(HINT)).toBeInTheDocument();
    expect(server.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /api/config",
    ]);
    expect(screen.queryByText("Мои игры")).not.toBeInTheDocument();
  });

  test("при dev_login — выбор dev-1…dev-6, вход по нажатию", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
    });
    renderApp(server);

    expect(
      await screen.findByRole("heading", { name: "Выберите игрока" }),
    ).toBeInTheDocument();
    for (const name of ["dev-1", "dev-2", "dev-3", "dev-4", "dev-5", "dev-6"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: "dev-2" }));

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/dev-2/)).toBeInTheDocument();
    expect(server.calls.find((c) => c.path === "/api/auth/dev")).toMatchObject({
      method: "POST",
      body: { login: "dev-2" },
    });
  });

  test("?dev=dev-3 входит сразу, без выбора", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
    });
    openUrl("?dev=dev-3");

    renderApp(server);

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "dev-1" })).toBeNull();
    expect(server.calls.find((c) => c.path === "/api/auth/dev")?.body).toEqual({
      login: "dev-3",
    });
  });

  test("?join=ABC123 после входа вызывает join и открывает лобби", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
      "POST /api/games/join": { json: lobbyGame },
    });
    openUrl("?dev=dev-3&join=ABC123");
    fakeWebSocket();

    renderApp(server);

    // Снимка игры ещё нет: лобби показывает загрузку и уже подключается к сокету.
    expect(
      await screen.findByText("Подключаемся к лобби…"),
    ).toBeInTheDocument();
    expect(useGame.getState()).toMatchObject({ screen: "lobby", gameId: "g1" });
    const join = server.calls.find((c) => c.path === "/api/games/join");
    expect(join?.body).toEqual({ code: "ABC123" });
    expect(join?.headers.get("Authorization")).toBe("Bearer tok-dev-3");
  });

  test("отказ join показан на экране «Мои игры»", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
      "POST /api/games/join": {
        status: 404,
        json: { detail: "Игра с таким кодом не найдена." },
      },
    });
    openUrl("?dev=dev-3&join=ABC123");

    renderApp(server);

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Игра с таким кодом не найдена.",
    );
  });

  test("в StrictMode вход выполняется один раз", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
    });
    openUrl("?dev=dev-3");

    renderApp(server, {}, (app) => <StrictMode>{app}</StrictMode>);

    await screen.findByRole("heading", { name: "Мои игры" });
    expect(server.calls.filter((c) => c.path === "/api/auth/dev")).toHaveLength(
      1,
    );
  });

  test("ошибка связи на входе: сообщение и «Повторить»", async () => {
    let online = false;
    const server = fakeServer({ "GET /api/config": () => config(false) });
    const flaky = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      online
        ? server.fetch(input, init)
        : Promise.reject(new TypeError("Failed to fetch")),
    );
    renderApp({ fetch: flaky as unknown as typeof fetch });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    );

    online = true;
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText(HINT)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("сбой самого приложения при входе", () => {
  test("игроку — общий текст с «Повторить», в консоли — только имя ошибки", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    renderApp(promptServer(), {
      readStartParam: () => {
        throw new TypeError(`сбой с данными ${INIT_DATA}`);
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Что-то пошло не так. Попробуйте ещё раз.",
    );
    expect(screen.getByRole("button", { name: "Повторить" })).toBeVisible();
    expect(error).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(error.mock.calls)).toContain("TypeError");
    expect(JSON.stringify(error.mock.calls)).not.toContain("query_id");
  });
});

describe("следы в консоли при сбое входа", () => {
  test("отказ сервера (ApiError) — это не сбой приложения, в консоль ничего не пишется", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": {
        status: 401,
        json: { detail: "Сессия недействительна." },
      },
    });
    openUrl("?dev=dev-3");

    renderApp(server);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сессия недействительна.",
    );
    expect(error).not.toHaveBeenCalled();
  });

  test("сбой не Error (например, брошена строка): в консоли только её тип", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    renderApp(promptServer(), {
      readStartParam: () => {
        throw `секрет ${INIT_DATA}`;
      },
    });

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(JSON.stringify(error.mock.calls)).toContain("string");
    expect(JSON.stringify(error.mock.calls)).not.toContain("query_id");
  });
});

describe("вход в Telegram", () => {
  test("настраивает SDK и входит по initData с заголовком tma", async () => {
    const server = fakeServer({
      "POST /api/auth/telegram": { json: login("Лира") },
      "GET /api/games": NO_GAMES,
    });
    const setupTelegram = vi.fn();

    renderApp(server, { readInitData: () => INIT_DATA, setupTelegram });

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(setupTelegram).toHaveBeenCalledTimes(1);
    // Вход — единственный запрос без токена сессии: дальше «Мои игры» просят список игр.
    expect(
      server.calls.filter((c) => c.path === "/api/auth/telegram"),
    ).toHaveLength(1);
    expect(server.calls[0].headers.get("Authorization")).toBe(
      `tma ${INIT_DATA}`,
    );
  });

  test("SDK запускается один раз, даже в StrictMode и после выхода", async () => {
    const server = fakeServer({
      "POST /api/auth/telegram": { json: login("Лира") },
      "GET /api/games": NO_GAMES,
    });
    const setupTelegram = vi.fn();

    renderApp(
      server,
      { readInitData: () => INIT_DATA, setupTelegram },
      (app) => <StrictMode>{app}</StrictMode>,
    );
    await screen.findByRole("heading", { name: "Мои игры" });
    act(() => useGame.getState().signOut());
    await screen.findByRole("heading", { name: "Мои игры" });

    expect(setupTelegram).toHaveBeenCalledTimes(1);
  });

  test("вне Telegram SDK не настраивается", async () => {
    const setupTelegram = vi.fn();

    renderApp(promptServer(), { setupTelegram });
    await screen.findByText(HINT);

    expect(setupTelegram).not.toHaveBeenCalled();
  });

  test("start_param ведёт в лобби", async () => {
    const server = fakeServer({
      "POST /api/auth/telegram": {
        json: { ...login("Лира"), start_param: "ABC123" },
      },
      "POST /api/games/join": { json: lobbyGame },
    });
    fakeWebSocket();

    renderApp(server, { readInitData: () => INIT_DATA });

    expect(
      await screen.findByText("Подключаемся к лобби…"),
    ).toBeInTheDocument();
    expect(useGame.getState().screen).toBe("lobby");
  });

  test("отказ входа показан с «Повторить», dev-выбора нет", async () => {
    let attempts = 0;
    const server = fakeServer({
      "GET /api/games": NO_GAMES,
      "POST /api/auth/telegram": () =>
        ++attempts === 1
          ? {
              status: 401,
              json: { detail: "Не удалось проверить вход через Telegram." },
            }
          : { json: login("Лира") },
    });
    renderApp(server, { readInitData: () => INIT_DATA });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось проверить вход через Telegram.",
    );
    expect(screen.queryByRole("button", { name: "dev-1" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
  });
});

describe("каркас экранов", () => {
  async function signedIn(routes: Record<string, FakeReply> = {}) {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
      ...routes,
    });
    openUrl("?dev=dev-3");
    renderApp(server);
    await screen.findByRole("heading", { name: "Мои игры" });
    return server;
  }

  test("«Мои игры» берут клиент API из App: список игр запрашивается с токеном", async () => {
    const server = await signedIn();

    const list = server.calls.find((c) => c.path === "/api/games");
    expect(list?.headers.get("Authorization")).toBe("Bearer tok-dev-3");
    expect(await screen.findByText("Пока нет игр")).toBeInTheDocument();
  });

  test("лобби открывается экраном лобби и слушает сокет игры", async () => {
    const ws = fakeWebSocket();
    await signedIn();

    act(() => useGame.getState().show({ screen: "lobby", gameId: "g1" }));

    expect(screen.getByRole("status")).toHaveTextContent(
      "Подключаемся к лобби",
    );
    expect(ws.last().url).toContain("/ws/games/g1");
  });

  test("лобби → создание героя → лобби: экран получает игру, назад возвращает в то же лобби", async () => {
    const ws = fakeWebSocket();
    const game = gameSummary({
      players: [
        lobbyPlayer({ user_id: "id-dev-3", name: "dev-3", is_host: true }),
      ],
    });
    await signedIn({
      "GET /api/content/creation-options": { json: OPTIONS },
      "GET /api/games/g1": { json: game },
    });
    act(() => useGame.getState().show({ screen: "lobby", gameId: "g1" }));
    act(() => {
      ws.last().open();
      ws.last().serve(snapshotOf(game));
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "Создать персонажа" }),
    );

    expect(useGame.getState()).toMatchObject({
      screen: "create",
      gameId: "g1",
    });
    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
    for (const mode of ["Подробно", "Быстро", "Случайно"]) {
      expect(screen.getByRole("button", { name: mode })).toBeInTheDocument();
    }
    expect(screen.queryByText(/появится в одном из следующих/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Назад в лобби" }));

    expect(useGame.getState()).toMatchObject({ screen: "lobby", gameId: "g1" });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Подключаемся к лобби",
    );
  });

  test("экран игры настоящий: слушает сокет игры и ждёт снимка", async () => {
    const ws = fakeWebSocket();
    await signedIn();

    act(() => useGame.getState().show({ screen: "game", gameId: "g1" }));

    expect(screen.getByRole("status")).toHaveTextContent("Подключаемся к игре");
    expect(ws.last().url).toContain("/ws/games/g1?token=tok-dev-3");
    expect(screen.queryByRole("heading", { name: "Игра" })).toBeNull();
  });

  test("снимок открывает вкладки игры внутри приложения", async () => {
    const ws = fakeWebSocket();
    await signedIn({ "GET /api/content/creation-options": { json: OPTIONS } });
    act(() => useGame.getState().show({ screen: "game", gameId: "g1" }));

    act(() => {
      ws.last().open();
      ws.last().serve(
        snapshotOf(
          gameSummary({
            phase: "playing",
            turn_phase: "discussion",
            players: [
              lobbyPlayer({
                user_id: "id-dev-3",
                name: "dev-3",
                is_host: true,
              }),
            ],
          }),
          {
            phase: {
              phase: "playing",
              turn_phase: "discussion",
              deadline_at: null,
              remaining_seconds: null,
              scene: null,
            },
          },
        ),
      );
    });

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Рассказ",
      "Дневник",
      "Персонаж",
      "Отряд",
    ]);
    expect(screen.getByText("Обсуждение")).toBeInTheDocument();
  });

  test("лобби → игра: начало игры открывает экран игры с новым подключением к сокету", async () => {
    const ws = fakeWebSocket();
    const players = [
      lobbyPlayer({ user_id: "id-dev-3", name: "dev-3", is_host: true }),
    ];
    await signedIn();
    act(() => useGame.getState().show({ screen: "lobby", gameId: "g1" }));
    act(() => {
      ws.last().open();
      ws.last().serve(snapshotOf(gameSummary({ players })));
    });
    expect(await screen.findByText("Игроки (1)")).toBeInTheDocument();

    // Игра пошла: приходит событие фазы, лобби уступает место игре.
    act(() => {
      ws.last().serve({
        type: "phase",
        phase: {
          phase: "playing",
          turn_phase: "discussion",
          deadline_at: null,
          remaining_seconds: null,
          scene: null,
        },
      });
    });
    await vi.waitFor(() => expect(useGame.getState().screen).toBe("game"));
    expect(ws.sockets).toHaveLength(2);
    expect(ws.sockets[0].clientCloseCode).not.toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Подключаемся к игре");

    act(() => {
      ws.last().open();
      ws.last().serve(
        snapshotOf(
          gameSummary({
            phase: "playing",
            turn_phase: "discussion",
            players,
          }),
          {
            phase: {
              phase: "playing",
              turn_phase: "discussion",
              deadline_at: null,
              remaining_seconds: null,
              scene: null,
            },
          },
        ),
      );
    });

    expect(screen.getByRole("tab", { name: "Рассказ" })).toBeInTheDocument();
    expect(screen.getByText("Обсуждение")).toBeInTheDocument();
  });

  test("после выхода снова экран входа", async () => {
    const server = fakeServer({
      "GET /api/config": config(true),
      "POST /api/auth/dev": devLogin,
      "GET /api/games": NO_GAMES,
    });
    renderApp(server);
    fireEvent.click(await screen.findByRole("button", { name: "dev-4" }));
    await screen.findByRole("heading", { name: "Мои игры" });

    act(() => useGame.getState().signOut());

    expect(
      await screen.findByRole("heading", { name: "Выберите игрока" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Мои игры" })).toBeNull();
  });
});

describe("хранилище", () => {
  test("до входа экраны, кроме входа, недоступны", () => {
    useGame.getState().show({ screen: "lobby", gameId: "g1" });

    expect(useGame.getState()).toMatchObject({ screen: "login", gameId: null });
  });

  test("выход очищает сессию и возвращает на вход", () => {
    const { setSession, show, signOut } = useGame.getState();
    setSession({ token: "t", user: login("dev-1").user });
    show({ screen: "game", gameId: "g1" });

    signOut();

    expect(useGame.getState()).toMatchObject({
      session: null,
      screen: "login",
      gameId: null,
    });
  });

  test("смена экрана убирает прежнее сообщение", () => {
    const { setSession, show, setNotice } = useGame.getState();
    setSession({ token: "t", user: login("dev-1").user });
    show({ screen: "games" });
    setNotice("Игра с таким кодом не найдена.");

    show({ screen: "create", gameId: "g1" });

    expect(useGame.getState().notice).toBeNull();
  });
});
