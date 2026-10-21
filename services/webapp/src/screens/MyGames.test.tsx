import { fireEvent, render, screen, within } from "@testing-library/react";
import { ApiContext } from "../api/context";
import { createApi } from "../api/client";
import type { GameSummary } from "../api/types";
import { sessionToken, useGame } from "../store/game";
import { fakeServer, gameSummary, lobbyPlayer, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { MyGames } from "./MyGames";

type Routes = Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;

interface Setup {
  games?: GameSummary[];
  canCreate?: boolean;
  routes?: Routes;
}

function renderMyGames({
  games = [],
  canCreate = false,
  routes = {},
}: Setup = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: canCreate });
  useGame.getState().show({ screen: "games" });
  const server = fakeServer({ "GET /api/games": { json: games }, ...routes });
  render(
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <MyGames />
    </ApiContext>,
  );
  return server;
}

const callsTo = (
  server: { calls: RecordedCall[] },
  method: string,
  path: string,
) =>
  server.calls.filter((call) => call.method === method && call.path === path);

const CRYPT = gameSummary({
  id: "g1",
  code: "ABC123",
  adventure_title: "Склеп забытого короля",
  phase: "lobby",
  players: [
    lobbyPlayer({ user_id: "u1", name: "Вика", is_host: true }),
    lobbyPlayer({ user_id: "u2", name: "Борис" }),
    lobbyPlayer({ user_id: "u3", name: "Лира" }),
  ],
});

describe("список игр", () => {
  test("заголовок и приветствие с именем игрока", async () => {
    renderMyGames();

    expect(
      await screen.findByRole("heading", { name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Вы вошли как Вика/)).toBeInTheDocument();
  });

  test("пока список идёт — загрузка, запрос с токеном сессии", async () => {
    const server = renderMyGames({ games: [CRYPT] });

    expect(screen.getByRole("status")).toHaveTextContent("Загружаем");
    await screen.findByText("Склеп забытого короля");
    expect(screen.queryByRole("status")).toBeNull();
    const [call] = callsTo(server, "GET", "/api/games");
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
  });

  test("игра в списке: название, фаза и число игроков", async () => {
    renderMyGames({ games: [CRYPT] });

    const title = await screen.findByText("Склеп забытого короля");
    const row = title.closest("li");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("Лобби")).toBeInTheDocument();
    expect(within(row!).getByText(/Игроков: 3/)).toBeInTheDocument();
  });

  test.each([
    ["lobby", "Лобби"],
    ["weaving", "Начало игры"],
    ["playing", "Идёт игра"],
    ["paused", "Пауза"],
    ["finished", "Завершена"],
  ] as const)("фаза %s подписана «%s»", async (phase, label) => {
    renderMyGames({ games: [gameSummary({ phase })] });

    const row = (await screen.findByText("Склеп")).closest("li");
    expect(within(row!).getByText(label)).toBeInTheDocument();
  });

  test("несколько игр идут в порядке сервера", async () => {
    renderMyGames({
      games: [
        gameSummary({ id: "g2", adventure_title: "Новая" }),
        gameSummary({ id: "g1", adventure_title: "Старая" }),
      ],
    });

    await screen.findByText("Новая");
    const items = screen
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(items[0]).toContain("Новая");
    expect(items[1]).toContain("Старая");
  });

  test("игр нет — пустое состояние с подсказкой", async () => {
    renderMyGames({ games: [] });

    expect(await screen.findByText("Пока нет игр")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });

  test.each([
    [true, "Создайте игру или войдите по коду приглашения."],
    [false, "Войдите в игру друзей по коду приглашения."],
  ])("игр нет, создатель: %s — подсказка «%s»", async (canCreate, hint) => {
    renderMyGames({ games: [], canCreate });

    await screen.findByText("Пока нет игр");
    expect(screen.getByText(hint)).toBeInTheDocument();
  });

  test("сбой запроса: текст сервера и «Повторить», который запрашивает заново", async () => {
    let attempts = 0;
    const server = renderMyGames({
      routes: {
        "GET /api/games": () =>
          ++attempts === 1
            ? { status: 500, json: { detail: "База недоступна." } }
            : { json: [CRYPT] },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "База недоступна.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(
      await screen.findByText("Склеп забытого короля"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(callsTo(server, "GET", "/api/games")).toHaveLength(2);
  });

  test("обрыв связи показан понятным текстом", async () => {
    signInAs({ id: "u1", name: "Вика" });
    const api = createApi(
      (() => Promise.reject(new TypeError("Failed to fetch"))) as typeof fetch,
      sessionToken,
    );
    render(
      <ApiContext value={api}>
        <MyGames />
      </ApiContext>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нет связи с сервером",
    );
  });

  test("сообщение из хранилища (например, отказ по приглашению) показано сверху", async () => {
    signInAs();
    useGame.getState().show({ screen: "games" });
    useGame.getState().setNotice("Игра недоступна.");
    const server = fakeServer({ "GET /api/games": { json: [] } });
    render(
      <ApiContext value={createApi(server.fetch, sessionToken)}>
        <MyGames />
      </ApiContext>,
    );

    expect(await screen.findByText("Пока нет игр")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Игра недоступна.");
  });
});

describe("открытие игры из списка", () => {
  test.each([
    ["lobby", "lobby"],
    ["weaving", "lobby"],
    ["playing", "game"],
    ["paused", "game"],
    ["finished", "game"],
  ] as const)("игра в фазе %s открывает экран «%s»", async (phase, target) => {
    renderMyGames({ games: [gameSummary({ id: "g7", phase })] });

    fireEvent.click(await screen.findByRole("button", { name: /Открыть/ }));

    expect(useGame.getState()).toMatchObject({ screen: target, gameId: "g7" });
  });

  test("у каждой игры своя кнопка с названием игры", async () => {
    renderMyGames({
      games: [
        gameSummary({ id: "g1", adventure_title: "Склеп" }),
        gameSummary({ id: "g2", adventure_title: "Башня" }),
      ],
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "Открыть: Башня" }),
    );

    expect(useGame.getState().gameId).toBe("g2");
  });
});

describe("вход по коду приглашения", () => {
  const codeField = () => screen.getByLabelText("Код приглашения");
  const enter = (value: string) => {
    fireEvent.change(codeField(), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  };
  const joined = (game: GameSummary): Routes => ({
    "POST /api/games/join": { json: game },
  });

  test("поле «Код приглашения» и кнопка «Войти»", async () => {
    renderMyGames();

    await screen.findByText("Пока нет игр");
    expect(codeField()).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Войти" })).toBeInTheDocument();
  });

  test.each([
    ["пустое поле", ""],
    ["короткий код", "ABC12"],
    ["длинный код", "ABC1234"],
    ["знаки вне A–Z и 0–9", "ABC-12"],
    ["кириллица", "АБВГДЕ"],
    ["пробелы внутри", "AB C12"],
  ] as const)(
    "%s: сообщение о формате, запроса на сервер нет",
    async (_, value) => {
      const server = renderMyGames();
      await screen.findByText("Пока нет игр");

      enter(value);

      expect(screen.getByRole("alert")).toHaveTextContent("6 символов");
      expect(codeField()).toBeInvalid();
      expect(callsTo(server, "POST", "/api/games/join")).toHaveLength(0);
    },
  );

  test("верный код уходит на сервер, регистр и пробелы по краям не важны", async () => {
    const server = renderMyGames({
      routes: joined(gameSummary({ id: "g9" })),
    });
    await screen.findByText("Пока нет игр");

    enter("  abc123 ");

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", "/api/games/join")).toHaveLength(1),
    );
    const [call] = callsTo(server, "POST", "/api/games/join");
    expect(call.body).toEqual({ code: "ABC123" });
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
  });

  // Игра в начале открывается лобби: там экран вплетения.
  test.each([
    ["lobby", "lobby"],
    ["playing", "game"],
    ["weaving", "lobby"],
  ] as const)(
    "вход в игру в фазе %s открывает экран «%s»",
    async (phase, target) => {
      renderMyGames({ routes: joined(gameSummary({ id: "g9", phase })) });
      await screen.findByText("Пока нет игр");

      enter("ABC123");

      await vi.waitFor(() =>
        expect(useGame.getState()).toMatchObject({
          screen: target,
          gameId: "g9",
        }),
      );
    },
  );

  test("отказ сервера показан его текстом, поле и экран остаются", async () => {
    renderMyGames({
      routes: {
        "POST /api/games/join": {
          status: 404,
          json: { detail: "Игра с таким кодом не найдена." },
        },
      },
    });
    await screen.findByText("Пока нет игр");

    enter("ZZZ999");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Игра с таким кодом не найдена.",
    );
    expect(useGame.getState().screen).toBe("games");
    expect(codeField()).toHaveValue("ZZZ999");
  });

  test("слишком много попыток: текст сервера", async () => {
    renderMyGames({
      routes: {
        "POST /api/games/join": {
          status: 429,
          json: {
            detail: "Слишком много неудачных кодов. Подождите 10 минут.",
          },
        },
      },
    });
    await screen.findByText("Пока нет игр");

    enter("ZZZ999");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Подождите 10 минут",
    );
  });

  test("пока запрос идёт, повторное нажатие не шлёт второй запрос", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    signInAs();
    const server = fakeServer({ "GET /api/games": { json: [] } });
    const slow = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        await gate;
        return new Response(JSON.stringify(gameSummary()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return server.fetch(input, init);
    });
    render(
      <ApiContext
        value={createApi(slow as unknown as typeof fetch, sessionToken)}
      >
        <MyGames />
      </ApiContext>,
    );
    await screen.findByText("Пока нет игр");

    enter("ABC123");
    const button = screen.getByRole("button", { name: "Войти" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    release();
    await vi.waitFor(() => expect(useGame.getState().screen).toBe("lobby"));

    expect(
      slow.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
  });

  test("после исправления кода сообщение о формате уходит", async () => {
    renderMyGames({ routes: joined(gameSummary()) });
    await screen.findByText("Пока нет игр");

    enter("AB");
    expect(screen.getByRole("alert")).toBeInTheDocument();
    enter("ABC123");

    await vi.waitFor(() => expect(useGame.getState().screen).toBe("lobby"));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("создание игры: только для создателей", () => {
  const formRoutes: Routes = {
    "GET /api/adventures": { json: [] },
    "GET /api/providers": { json: { providers: [] } },
  };

  test("создателю показана кнопка «Создать игру»", async () => {
    renderMyGames({ canCreate: true });

    expect(
      await screen.findByRole("button", { name: "Создать игру" }),
    ).toBeInTheDocument();
  });

  test("не создателю кнопки нет и списки для формы не запрашиваются", async () => {
    const server = renderMyGames({ canCreate: false });
    await screen.findByText("Пока нет игр");

    expect(screen.queryByRole("button", { name: "Создать игру" })).toBeNull();
    expect(server.calls.map((call) => call.path)).toEqual(["/api/games"]);
  });

  test("форма не открыта, пока создатель не нажал кнопку", async () => {
    const server = renderMyGames({ canCreate: true });
    await screen.findByText("Пока нет игр");

    expect(screen.queryByRole("heading", { name: "Новая игра" })).toBeNull();
    expect(server.calls.map((call) => call.path)).toEqual(["/api/games"]);
  });

  test("по нажатию открывается форма и запрашивает приключения и провайдеров", async () => {
    const server = renderMyGames({ canCreate: true, routes: formRoutes });

    fireEvent.click(
      await screen.findByRole("button", { name: "Создать игру" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Новая игра" }),
    ).toBeInTheDocument();
    await vi.waitFor(() => {
      const paths = server.calls.map((call) => call.path);
      expect(paths).toContain("/api/adventures");
      expect(paths).toContain("/api/providers");
    });
  });

  test("пока форма открыта, кнопки «Создать игру» нет; «Отмена» возвращает её", async () => {
    renderMyGames({ canCreate: true, routes: formRoutes });

    fireEvent.click(
      await screen.findByRole("button", { name: "Создать игру" }),
    );
    await screen.findByRole("heading", { name: "Новая игра" });
    expect(screen.queryByRole("button", { name: "Создать игру" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Отмена" }));

    expect(screen.queryByRole("heading", { name: "Новая игра" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Создать игру" }),
    ).toBeInTheDocument();
  });
});
