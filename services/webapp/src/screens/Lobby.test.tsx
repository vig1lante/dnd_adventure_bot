import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { ApiContext } from "../api/context";
import { createApi } from "../api/client";
import type {
  AdventureSummary,
  GameSummary,
  PlayerSummary,
} from "../api/types";
import { sessionToken, useGame } from "../store/game";
import {
  fakeServer,
  fakeWebSocket,
  gameSummary,
  lobbyPlayer,
  signInAs,
  snapshotOf,
} from "../test-setup";
import type { FakeReply, FakeSocket, RecordedCall } from "../test-setup";
import { holdRequests } from "./character/fixtures";
import { canShareInTelegram, shareInTelegram } from "./invite";
import { Lobby } from "./Lobby";

// Доступность отправки через Telegram зависит от SDK; экрану важно лишь «есть или нет».
vi.mock("./invite", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./invite")>()),
  canShareInTelegram: vi.fn(() => false),
  shareInTelegram: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(canShareInTelegram).mockReturnValue(false);
  vi.mocked(shareInTelegram).mockReset();
});

// --- данные ---

const PITCH = "Отряд спускается в старый склеп под деревней.";
const ADVENTURES: AdventureSummary[] = [
  {
    id: "demo-crypt",
    title: "Склеп забытого короля",
    pitch: PITCH,
    target_hours: 2,
    score: 92,
    blocking: false,
  },
];
const INVITE_LINK = "https://t.me/dnd_table_bot?startapp=ABC123";

const host = (overrides: Partial<PlayerSummary> = {}) =>
  lobbyPlayer({
    user_id: "u1",
    name: "Вика",
    is_host: true,
    has_character: true,
    ready: true,
    ...overrides,
  });
const boris = (overrides: Partial<PlayerSummary> = {}) =>
  lobbyPlayer({
    user_id: "u2",
    name: "Борис",
    has_character: true,
    ready: true,
    ...overrides,
  });
const lira = (overrides: Partial<PlayerSummary> = {}) =>
  lobbyPlayer({ user_id: "u3", name: "Лира", ...overrides });
const readyLira = () => lira({ has_character: true, ready: true });

const lobbyOf = (
  players: PlayerSummary[],
  overrides: Partial<GameSummary> = {},
) =>
  gameSummary({
    adventure_title: "Склеп забытого короля",
    players,
    ...overrides,
  });

/** Событие `phase` сокета: фаза игры и фаза хода. */
const phaseEvent = (
  phase: GameSummary["phase"],
  turn_phase: GameSummary["turn_phase"],
) => ({
  type: "phase",
  phase: {
    phase,
    turn_phase,
    deadline_at: null,
    remaining_seconds: null,
    scene: null,
  },
});

const NO_BOT = {
  "GET /api/config": {
    json: { env: "dev", dev_login: true, bot_username: null },
  },
};

type Routes = Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;

interface OpenOptions {
  /** Кто смотрит лобби. */
  as?: "u1" | "u2" | "u3";
  routes?: Routes;
  /** Первый снимок; `null` — сокет пока молчит. */
  game?: GameSummary | null;
  strict?: boolean;
  /** Держать запрос на адрес с таким окончанием, пока тест не вызовет `release`. */
  hold?: string;
}

const NAMES = { u1: "Вика", u2: "Борис", u3: "Лира" } as const;

// Подменяются только таймеры сокета и экрана: React и промисы работают как обычно.
const FAKE_TIMERS: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval"];

/** Открывает лобби игры g1 глазами игрока и подключает сокет. */
async function openLobby({
  as = "u1",
  routes = {},
  game = lobbyOf([host(), boris(), lira()]),
  strict = false,
  hold,
}: OpenOptions = {}) {
  const ws = fakeWebSocket();
  signInAs({ id: as, name: NAMES[as] });
  useGame.getState().show({ screen: "lobby", gameId: "g1" });
  const server = fakeServer({
    "GET /api/adventures": { json: ADVENTURES },
    "GET /api/config": {
      json: { env: "dev", dev_login: true, bot_username: "dnd_table_bot" },
    },
    ...routes,
  });
  const gated = holdRequests(server.fetch);
  const release = hold === undefined ? () => {} : gated.hold(hold);
  const tree = (
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Lobby />
    </ApiContext>
  );
  const { unmount } = render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  if (game !== null) await connect(ws.last(), game);
  return { ws, server, release, unmount };
}

/** Сервер принял сокет и прислал снимок. */
async function connect(socket: FakeSocket, game: GameSummary) {
  await act(async () => {
    socket.open();
    socket.serve(snapshotOf(game));
  });
}

const serve = (socket: FakeSocket, frame: unknown) =>
  act(() => socket.serve(frame));

const callsTo = (
  server: { calls: RecordedCall[] },
  method: string,
  path: string,
) =>
  server.calls.filter((call) => call.method === method && call.path === path);

const rowOf = (name: string): HTMLElement => {
  const row = screen
    .getByText(name, { selector: ".lobby-player__name" })
    .closest("li");
  if (row === null) throw new Error(`Нет строки игрока ${name}`);
  return row;
};

const button = (name: string | RegExp) => screen.getByRole("button", { name });
const noButton = (name: string | RegExp) =>
  expect(screen.queryByRole("button", { name })).toBeNull();

describe("подключение и состав лобби", () => {
  test("пока снимка нет — загрузка", async () => {
    await openLobby({ game: null });

    expect(screen.getByRole("status")).toHaveTextContent("Подключаемся");
    // Загрузка — скелетон экрана, а не одинокий кубик.
    expect(
      screen.getByRole("status").querySelectorAll(".ui-skeleton__row").length,
    ).toBeGreaterThan(0);
    noButton("Начать");
  });

  test("лобби слушает сокет игры с токеном сессии", async () => {
    const { ws } = await openLobby({ game: null });

    expect(ws.sockets).toHaveLength(1);
    expect(ws.last().url).toMatch(/\/ws\/games\/g1\?token=tok-u1$/);
  });

  test("название приключения и завязка из каталога по adventure_id", async () => {
    await openLobby();

    expect(
      screen.getByRole("heading", { name: "Склеп забытого короля" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(PITCH)).toBeInTheDocument();
  });

  test.each([
    [true, "Мат: включён"],
    [false, "Мат: выключен"],
  ] as const)(
    "флажок мата игры (profanity: %s) — «%s»",
    async (profanity, text) => {
      await openLobby({
        game: lobbyOf([host(), boris(), lira()], { profanity }),
      });

      expect(screen.getByText(text)).toBeInTheDocument();
    },
  );

  test("флажок мата виден и игроку, а не только хосту", async () => {
    await openLobby({
      as: "u2",
      game: lobbyOf([host(), boris(), lira()], { profanity: false }),
    });

    expect(screen.getByText("Мат: выключен")).toBeInTheDocument();
  });

  test("каталог не ответил: название на месте, завязки нет, ошибки нет", async () => {
    await openLobby({
      routes: {
        "GET /api/adventures": { status: 500, json: { detail: "Сбой." } },
      },
    });

    expect(
      screen.getByRole("heading", { name: "Склеп забытого короля" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(PITCH)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("приключения нет в каталоге: завязки нет", async () => {
    await openLobby({ routes: { "GET /api/adventures": { json: [] } } });

    expect(screen.queryByText(PITCH)).toBeNull();
  });

  test("игроки со статусами: создаёт персонажа, персонаж создан, готов", async () => {
    await openLobby({
      game: lobbyOf([
        host(),
        boris({ ready: false }),
        lira({ has_character: false }),
      ]),
    });

    expect(within(rowOf("Вика")).getByText("Готов")).toBeInTheDocument();
    expect(
      within(rowOf("Борис")).getByText("Персонаж создан"),
    ).toBeInTheDocument();
    expect(
      within(rowOf("Лира")).getByText("Создаёт персонажа"),
    ).toBeInTheDocument();
  });

  test("у игрока с героем видно имя героя — и хосту, и другим игрокам (B11.3)", async () => {
    const game = lobbyOf([
      host({ hero_name: "Торин" }),
      boris({ hero_name: "Мила Тень" }),
      lira(),
    ]);
    await openLobby({ as: "u3", game });

    expect(within(rowOf("Вика")).getByText("Герой: Торин")).toBeVisible();
    expect(within(rowOf("Борис")).getByText("Герой: Мила Тень")).toBeVisible();
    expect(within(rowOf("Лира")).queryByText(/Герой:/)).toBeNull();
    // Значок статуса остаётся рядом: на его тексты («Персонаж создан») опираются e2e.
    expect(
      within(rowOf("Борис")).getByText("Готов", { selector: ".ui-badge" }),
    ).toBeInTheDocument();
  });

  test("своё имя героя видно в своей строке и обновляется событием lobby", async () => {
    const { ws } = await openLobby({
      as: "u2",
      game: lobbyOf([host(), boris({ hero_name: "Мила" })]),
    });
    expect(within(rowOf("Борис")).getByText("Герой: Мила")).toBeVisible();

    await serve(ws.last(), {
      type: "lobby",
      game: lobbyOf([host(), boris({ hero_name: "Мила Тень" })]),
    });

    expect(within(rowOf("Борис")).getByText("Герой: Мила Тень")).toBeVisible();
  });

  test("имя героя — только текст: разметка не исполняется", async () => {
    const markup = "<img src=x onerror=alert(1)>";
    await openLobby({ game: lobbyOf([host({ hero_name: markup })]) });

    expect(within(rowOf("Вика")).getByText(`Герой: ${markup}`)).toBeVisible();
    expect(document.querySelector("img[src='x']")).toBeNull();
  });

  test("сервер без hero_name (до B11) — строка игрока как раньше", async () => {
    const old = boris();
    delete old.hero_name;
    await openLobby({ game: lobbyOf([host(), old]) });

    expect(screen.queryByText(/Герой:/)).toBeNull();
    expect(within(rowOf("Борис")).getByText("Готов")).toBeInTheDocument();
  });

  test("хост помечен, текущий игрок отмечен «вы»", async () => {
    await openLobby({ as: "u2" });

    expect(within(rowOf("Вика")).getByText("Хост")).toBeInTheDocument();
    expect(within(rowOf("Борис")).getByText(/вы/)).toBeInTheDocument();
    expect(within(rowOf("Лира")).queryByText(/\(вы\)/)).toBeNull();
    expect(within(rowOf("Борис")).queryByText("Хост")).toBeNull();
  });

  test("число игроков в заголовке списка", async () => {
    await openLobby();

    expect(screen.getByRole("heading", { name: /Игроки/ })).toHaveTextContent(
      "3",
    );
  });

  test("«К моим играм» возвращает на список и не выходит из игры", async () => {
    const { server } = await openLobby();

    fireEvent.click(button("К моим играм"));

    expect(useGame.getState().screen).toBe("games");
    expect(callsTo(server, "POST", "/api/games/g1/leave")).toHaveLength(0);
  });

  test("при уходе с экрана сокет закрывается", async () => {
    const { ws } = await openLobby();

    fireEvent.click(button("К моим играм"));

    expect(ws.last().clientCloseCode).not.toBeNull();
  });

  test("в StrictMode остаётся одно живое подключение", async () => {
    const { ws } = await openLobby({ game: null, strict: true });

    const alive = ws.sockets.filter(
      (socket) => socket.clientCloseCode === null,
    );
    expect(alive).toHaveLength(1);
  });
});

describe("лобби обновляется событием lobby", () => {
  test("новый игрок появляется в списке", async () => {
    const { ws } = await openLobby({ game: lobbyOf([host(), boris()]) });
    expect(screen.queryByText("Лира")).toBeNull();

    await serve(ws.last(), {
      type: "lobby",
      game: lobbyOf([host(), boris(), lira()]),
    });

    expect(rowOf("Лира")).toBeInTheDocument();
  });

  test("статус игрока меняется", async () => {
    const { ws } = await openLobby();
    expect(
      within(rowOf("Лира")).getByText("Создаёт персонажа"),
    ).toBeInTheDocument();

    await serve(ws.last(), {
      type: "lobby",
      game: lobbyOf([host(), boris(), readyLira()]),
    });

    expect(within(rowOf("Лира")).getByText("Готов")).toBeInTheDocument();
  });

  test("игрок вышел — пропадает из списка", async () => {
    const { ws } = await openLobby();

    await serve(ws.last(), { type: "lobby", game: lobbyOf([host(), boris()]) });

    expect(screen.queryByText("Лира")).toBeNull();
  });

  test("«Начать» оживает, когда последний игрок отметил «Готов»", async () => {
    const { ws } = await openLobby();
    expect(button("Начать")).toBeDisabled();

    await serve(ws.last(), {
      type: "lobby",
      game: lobbyOf([host(), boris(), readyLira()]),
    });

    expect(button("Начать")).toBeEnabled();
  });

  test("сообщение сервера об ошибке показано, игра остаётся на экране", async () => {
    const { ws } = await openLobby();

    await serve(ws.last(), {
      type: "error",
      detail: "Не удалось загрузить игру.",
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Не удалось загрузить игру.",
    );
    expect(rowOf("Вика")).toBeInTheDocument();
  });

  test("обрыв связи: «Переподключаемся…», после нового снимка пропадает", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openLobby();

    act(() => ws.last().drop(1006));
    expect(screen.getByText(/Переподключаемся/)).toBeInTheDocument();
    expect(rowOf("Вика")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(ws.sockets).toHaveLength(2);
    await connect(ws.last(), lobbyOf([host(), boris()]));

    expect(screen.queryByText(/Переподключаемся/)).toBeNull();
    expect(screen.queryByText("Лира")).toBeNull();
  });

  test("плашка обрыва прилипает к верху: стоит в липкой панели и одна", async () => {
    const { ws } = await openLobby();

    act(() => ws.last().drop(1006));

    expect(screen.getAllByText(/Переподключаемся/)).toHaveLength(1);
    expect(
      screen.getByText(/Переподключаемся/).closest(".screen-sticky"),
    ).not.toBeNull();
    expect(screen.getByText(/Переподключаемся/)).toHaveAttribute(
      "role",
      "status",
    );
  });

  test("липкой панели нет, пока связь есть", async () => {
    await openLobby();

    expect(document.querySelector(".screen-sticky")).toBeNull();
  });

  test("вплетение (начало игры): плашка обрыва тоже в липкой панели", async () => {
    const { ws } = await openLobby({
      game: lobbyOf([host(), boris(), lira()], {
        phase: "weaving",
        turn_phase: "resolving",
      }),
    });

    act(() => ws.last().drop(1006));

    expect(
      screen.getByText(/Переподключаемся/).closest(".screen-sticky"),
    ).not.toBeNull();
  });

  test("плашка без движения: при «меньше движения» и без него классов анимации нет", async () => {
    for (const reduce of [false, true]) {
      vi.stubGlobal("matchMedia", (query: string) => ({
        matches: query.includes("prefers-reduced-motion") && reduce,
        addEventListener: () => {},
        removeEventListener: () => {},
      }));
      const { ws, unmount } = await openLobby();

      act(() => ws.last().drop(1006));

      const panel = screen
        .getByText(/Переподключаемся/)
        .closest(".screen-sticky");
      expect(panel).not.toBeNull();
      expect(panel?.querySelector("[class*='ui-anim']")).toBeNull();
      expect(panel?.className).not.toMatch(/ui-anim/);
      unmount();
    }
  });

  test("сокет закрыт кодом 4403 — «Мои игры» с пояснением", async () => {
    const { ws } = await openLobby();

    act(() => ws.last().drop(4403));

    expect(useGame.getState()).toMatchObject({
      screen: "games",
      notice: "Игра недоступна.",
    });
  });

  test("хост исключил игрока (кадр kicked и 4403) — «Мои игры» с «Хост исключил вас.»", async () => {
    const { ws } = await openLobby({ as: "u3" });

    act(() => {
      ws.last().serve({ type: "kicked" });
      ws.last().drop(4403, "kicked");
    });

    expect(useGame.getState()).toMatchObject({
      screen: "games",
      gameId: null,
      notice: "Хост исключил вас.",
    });
  });

  test("сокет закрыт кодом 4401 — экран входа", async () => {
    const { ws } = await openLobby();

    act(() => ws.last().drop(4401));

    expect(useGame.getState()).toMatchObject({
      screen: "login",
      session: null,
    });
  });
});

describe("«Начать» — только хост, только когда все готовы", () => {
  test("не все готовы: кнопка неактивна, названо, кого ждём", async () => {
    await openLobby();

    expect(button("Начать")).toBeDisabled();
    expect(screen.getByText(/Ждём готовности: Лира/)).toBeInTheDocument();
  });

  test("ждём нескольких — названы все", async () => {
    await openLobby({
      game: lobbyOf([host(), boris({ ready: false }), lira()]),
    });

    expect(
      screen.getByText(/Ждём готовности: Борис, Лира/),
    ).toBeInTheDocument();
  });

  test("все готовы: кнопка активна, подсказки об ожидании нет", async () => {
    await openLobby({ game: lobbyOf([host(), boris(), readyLira()]) });

    expect(button("Начать")).toBeEnabled();
    expect(screen.queryByText(/Ждём готовности/)).toBeNull();
  });

  test("игрок с отметкой «Готов», но без персонажа, готовым не считается", async () => {
    await openLobby({
      game: lobbyOf([host(), boris({ has_character: false, ready: true })]),
    });

    expect(button("Начать")).toBeDisabled();
  });

  test("хост один и готов: можно начинать", async () => {
    await openLobby({ game: lobbyOf([host()]) });

    expect(button("Начать")).toBeEnabled();
  });

  test("хост один и не готов: нельзя", async () => {
    await openLobby({ game: lobbyOf([host({ ready: false })]) });

    expect(button("Начать")).toBeDisabled();
  });

  test("нажатие шлёт start с токеном сессии", async () => {
    const { server } = await openLobby({
      game: lobbyOf([host(), boris()]),
      routes: {
        "POST /api/games/g1/start": { json: gameSummary({ phase: "weaving" }) },
      },
    });

    fireEvent.click(button("Начать"));

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", "/api/games/g1/start")).toHaveLength(1),
    );
    const [call] = callsTo(server, "POST", "/api/games/g1/start");
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
  });

  test("отказ сервера показан его текстом", async () => {
    await openLobby({
      game: lobbyOf([host(), boris()]),
      routes: {
        "POST /api/games/g1/start": {
          status: 409,
          json: {
            detail:
              "Не все игроки готовы: у каждого должен быть персонаж и отметка «Готов».",
          },
        },
      },
    });

    fireEvent.click(button("Начать"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не все игроки готовы",
    );
  });

  test("не хосту кнопки «Начать» нет", async () => {
    await openLobby({ as: "u2" });

    noButton("Начать");
    expect(screen.queryByText(/Ждём готовности/)).toBeNull();
  });
});

describe("«Пригласить» — только хост", () => {
  const writeText = vi.fn();

  beforeEach(() => {
    writeText.mockReset().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
  });

  test("у хоста кнопка есть", async () => {
    await openLobby({ as: "u1" });

    expect(button("Пригласить")).toBeInTheDocument();
  });

  test("у не-хоста кнопки и кода приглашения нет", async () => {
    const { server } = await openLobby({ as: "u2" });

    noButton("Пригласить");
    expect(screen.queryByText(/ABC123/)).toBeNull();
    expect(callsTo(server, "GET", "/api/config")).toHaveLength(0);
  });

  test("хост видит код и ссылку на бота из /api/config", async () => {
    await openLobby();

    expect(await screen.findByText(INVITE_LINK)).toBeInTheDocument();
    expect(screen.getByText("ABC123")).toBeInTheDocument();
  });

  test("нажатие копирует ссылку и подтверждает это", async () => {
    await openLobby();
    await screen.findByText(INVITE_LINK);

    fireEvent.click(button("Пригласить"));

    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith(INVITE_LINK));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Ссылка скопирована",
    );
  });

  test("бот не настроен: ссылки нет, копируется код", async () => {
    await openLobby({ routes: NO_BOT });
    await vi.waitFor(() => expect(button("Пригласить")).toBeEnabled());

    fireEvent.click(button("Пригласить"));

    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("ABC123"));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Код скопирован",
    );
    expect(screen.queryByText(/t\.me/)).toBeNull();
  });

  test("/api/config не ответил: тоже копируется код, лобби не ломается", async () => {
    await openLobby({
      routes: {
        "GET /api/config": { status: 502, text: "<html>Bad gateway</html>" },
      },
    });
    await vi.waitFor(() => expect(button("Пригласить")).toBeEnabled());

    fireEvent.click(button("Пригласить"));

    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("ABC123"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("буфер обмена отказал: подсказка выделить ссылку вручную", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    await openLobby();
    await screen.findByText(INVITE_LINK);

    fireEvent.click(button("Пригласить"));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Не удалось скопировать",
    );
    expect(screen.getByText(INVITE_LINK)).toBeInTheDocument();
  });

  test("вне Telegram кнопки «Отправить в Telegram» нет", async () => {
    await openLobby();
    await screen.findByText(INVITE_LINK);

    noButton("Отправить в Telegram");
  });

  test("в Telegram есть «Отправить в Telegram»: SDK получает ссылку и текст", async () => {
    vi.mocked(canShareInTelegram).mockReturnValue(true);
    await openLobby();
    await screen.findByText(INVITE_LINK);

    fireEvent.click(button("Отправить в Telegram"));

    expect(shareInTelegram).toHaveBeenCalledTimes(1);
    const [link, text] = vi.mocked(shareInTelegram).mock.calls[0];
    expect(link).toBe(INVITE_LINK);
    expect(text).toContain("Склеп забытого короля");
  });

  test("в Telegram без ссылки на бота отправки через SDK нет", async () => {
    vi.mocked(canShareInTelegram).mockReturnValue(true);
    await openLobby({ routes: NO_BOT });
    await vi.waitFor(() => expect(button("Пригласить")).toBeEnabled());

    noButton("Отправить в Telegram");
  });

  test("сбой SDK при отправке показан понятным текстом", async () => {
    vi.mocked(canShareInTelegram).mockReturnValue(true);
    vi.mocked(shareInTelegram).mockImplementation(() => {
      throw new Error("не поддерживается");
    });
    await openLobby();
    await screen.findByText(INVITE_LINK);

    fireEvent.click(button("Отправить в Telegram"));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Не удалось открыть Telegram",
    );
  });
});

describe("«Исключить» — только хост, только в лобби", () => {
  const KICK = "/api/games/g1/players/u2/kick";

  test("у хоста — по кнопке на каждого другого игрока, на себя нет", async () => {
    await openLobby({ as: "u1" });

    expect(button("Исключить Борис")).toBeInTheDocument();
    expect(button("Исключить Лира")).toBeInTheDocument();
    noButton("Исключить Вика");
  });

  test("у игрока кнопок «Исключить» нет", async () => {
    await openLobby({ as: "u2" });

    expect(screen.queryAllByRole("button", { name: /Исключить/ })).toHaveLength(
      0,
    );
  });

  test("нажатие просит подтверждения и ничего не шлёт", async () => {
    const { server } = await openLobby();

    fireEvent.click(button("Исключить Борис"));

    expect(screen.getByText(/Исключить игрока Борис\?/)).toBeInTheDocument();
    expect(callsTo(server, "POST", KICK)).toHaveLength(0);
  });

  test("«Отмена» убирает подтверждение, запроса нет", async () => {
    const { server } = await openLobby();
    fireEvent.click(button("Исключить Борис"));

    fireEvent.click(button("Отмена"));

    expect(screen.queryByText(/Исключить игрока Борис\?/)).toBeNull();
    expect(button("Исключить Борис")).toBeInTheDocument();
    expect(callsTo(server, "POST", KICK)).toHaveLength(0);
  });

  test("подтверждение шлёт kick именно этому игроку", async () => {
    const { server } = await openLobby({
      routes: { [`POST ${KICK}`]: { json: lobbyOf([host(), lira()]) } },
    });
    fireEvent.click(button("Исключить Борис"));

    fireEvent.click(button("Да, исключить"));

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", KICK)).toHaveLength(1),
    );
    const [call] = callsTo(server, "POST", KICK);
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
  });

  test("после исключения игрок уходит из списка по событию lobby", async () => {
    const { ws } = await openLobby({
      routes: { [`POST ${KICK}`]: { json: lobbyOf([host(), lira()]) } },
    });
    fireEvent.click(button("Исключить Борис"));
    fireEvent.click(button("Да, исключить"));

    await serve(ws.last(), { type: "lobby", game: lobbyOf([host(), lira()]) });

    expect(screen.queryByText("Борис")).toBeNull();
    expect(screen.queryByText(/Исключить игрока Борис\?/)).toBeNull();
  });

  test("отказ сервера показан его текстом", async () => {
    await openLobby({
      routes: {
        [`POST ${KICK}`]: {
          status: 404,
          json: { detail: "Такого игрока нет в этой игре." },
        },
      },
    });
    fireEvent.click(button("Исключить Борис"));
    fireEvent.click(button("Да, исключить"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Такого игрока нет в этой игре.",
    );
  });

  test("если игрок исчез, пока шло подтверждение, подтверждение не остаётся висеть", async () => {
    const { ws } = await openLobby();
    fireEvent.click(button("Исключить Борис"));

    await serve(ws.last(), { type: "lobby", game: lobbyOf([host(), lira()]) });

    noButton("Да, исключить");
  });
});

describe("«Выйти из лобби» — не хост", () => {
  const LEAVE = "/api/games/g1/leave";

  test("у хоста кнопки нет", async () => {
    await openLobby({ as: "u1" });

    noButton(/Выйти/);
  });

  test("у игрока кнопка есть", async () => {
    await openLobby({ as: "u2" });

    expect(button("Выйти из лобби")).toBeInTheDocument();
  });

  test("нажатие просит подтверждения и ничего не шлёт", async () => {
    const { server } = await openLobby({ as: "u2" });

    fireEvent.click(button("Выйти из лобби"));

    expect(screen.getByText(/Выйти из лобби\?/)).toBeInTheDocument();
    expect(callsTo(server, "POST", LEAVE)).toHaveLength(0);
  });

  test("«Остаться» отменяет выход", async () => {
    const { server } = await openLobby({ as: "u2" });
    fireEvent.click(button("Выйти из лобби"));

    fireEvent.click(button("Остаться"));

    expect(screen.queryByText(/Выйти из лобби\?/)).toBeNull();
    expect(button("Выйти из лобби")).toBeInTheDocument();
    expect(callsTo(server, "POST", LEAVE)).toHaveLength(0);
  });

  test("подтверждение шлёт leave и открывает «Мои игры» без лишних сообщений", async () => {
    const { server, ws } = await openLobby({
      as: "u2",
      routes: { [`POST ${LEAVE}`]: { status: 204 } },
    });
    fireEvent.click(button("Выйти из лобби"));

    fireEvent.click(button("Да, выйти"));

    await vi.waitFor(() => expect(useGame.getState().screen).toBe("games"));
    expect(callsTo(server, "POST", LEAVE)).toHaveLength(1);
    expect(useGame.getState().notice).toBeNull();
    expect(ws.last().clientCloseCode).not.toBeNull();
  });

  test("сервер закрыл сокет раньше ответа на выход: «Игра недоступна» не мигает", async () => {
    const { ws, release } = await openLobby({
      as: "u2",
      routes: { [`POST ${LEAVE}`]: { status: 204 } },
      hold: LEAVE,
    });
    fireEvent.click(button("Выйти из лобби"));
    fireEvent.click(button("Да, выйти"));

    act(() => ws.last().drop(4403)); // игрока уже нет в игре, а ответ REST в пути

    expect(useGame.getState().notice).toBeNull();
    expect(screen.queryByText("Игра недоступна.")).toBeNull();
    expect(useGame.getState().screen).toBe("lobby");
    release();
    await vi.waitFor(() => expect(useGame.getState().screen).toBe("games"));
    expect(useGame.getState().notice).toBeNull();
  });

  test("повтор сокета после выхода, закончившийся отказом, тоже молчит", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws, release } = await openLobby({
      as: "u2",
      routes: { [`POST ${LEAVE}`]: { status: 204 } },
      hold: LEAVE,
    });
    fireEvent.click(button("Выйти из лобби"));
    fireEvent.click(button("Да, выйти"));
    act(() => ws.last().drop(1013));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(ws.sockets).toHaveLength(2);

    act(() => ws.last().drop(4403));

    expect(useGame.getState().notice).toBeNull();
    release();
    await vi.waitFor(() => expect(useGame.getState().screen).toBe("games"));
    expect(useGame.getState().notice).toBeNull();
  });

  test("отказ выхода: игрока исключил хост, а не он сам, — сообщение остаётся", async () => {
    const { ws } = await openLobby({ as: "u2" });

    act(() => ws.last().drop(4403)); // выход не нажимали

    expect(useGame.getState()).toMatchObject({
      screen: "games",
      notice: "Игра недоступна.",
    });
  });

  test("отказ сервера показан, игрок остаётся в лобби", async () => {
    await openLobby({
      as: "u2",
      routes: {
        [`POST ${LEAVE}`]: {
          status: 409,
          json: { detail: "Игра уже началась: выйти можно только до старта." },
        },
      },
    });
    fireEvent.click(button("Выйти из лобби"));
    fireEvent.click(button("Да, выйти"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Игра уже началась",
    );
    expect(useGame.getState().screen).toBe("lobby");
  });
});

describe("герой и отметка «Готов»", () => {
  const READY = "/api/games/g1/ready";
  const readySwitch = () => screen.getByRole("switch", { name: "Готов" });

  test("без персонажа: «Создать персонажа», «Готов» неактивен с пояснением", async () => {
    await openLobby({ as: "u3" });

    expect(button("Создать персонажа")).toBeInTheDocument();
    noButton("Изменить");
    expect(readySwitch()).toBeDisabled();
    expect(readySwitch()).toHaveAccessibleDescription(/Сначала создайте героя/);
  });

  test("с персонажем: «Изменить», «Готов» доступен", async () => {
    await openLobby({ as: "u2" });

    expect(button("Изменить")).toBeInTheDocument();
    noButton("Создать персонажа");
    expect(readySwitch()).toBeEnabled();
    expect(screen.queryByText("Сначала создайте героя.")).toBeNull();
  });

  test.each([
    ["Создать персонажа", "u3"],
    ["Изменить", "u2"],
  ] as const)(
    "«%s» открывает экран создания героя этой игры",
    async (name, as) => {
      await openLobby({ as });

      fireEvent.click(button(name));

      expect(useGame.getState()).toMatchObject({
        screen: "create",
        gameId: "g1",
      });
    },
  );

  test("хост тоже создаёт героя и отмечает «Готов»", async () => {
    await openLobby({
      as: "u1",
      game: lobbyOf([host({ has_character: false, ready: false })]),
    });

    expect(button("Создать персонажа")).toBeInTheDocument();
    expect(readySwitch()).toBeDisabled();
  });

  test("отметка отражает состояние игрока на сервере", async () => {
    await openLobby({ as: "u2" });

    expect(readySwitch()).toBeChecked();
  });

  test("включение шлёт ready: true", async () => {
    const { server } = await openLobby({
      as: "u2",
      game: lobbyOf([host(), boris({ ready: false })]),
      routes: { [`POST ${READY}`]: { json: lobbyOf([host(), boris()]) } },
    });

    fireEvent.click(readySwitch());

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", READY)).toHaveLength(1),
    );
    const [call] = callsTo(server, "POST", READY);
    expect(call.body).toEqual({ ready: true });
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u2");
  });

  test("выключение шлёт ready: false", async () => {
    const { server } = await openLobby({
      as: "u2",
      routes: {
        [`POST ${READY}`]: { json: lobbyOf([host(), boris({ ready: false })]) },
      },
    });

    fireEvent.click(readySwitch());

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", READY)).toHaveLength(1),
    );
    expect(callsTo(server, "POST", READY)[0].body).toEqual({ ready: false });
  });

  test("переключатель следует за событием lobby", async () => {
    const { ws } = await openLobby({
      as: "u2",
      game: lobbyOf([host(), boris({ ready: false })]),
    });
    expect(readySwitch()).not.toBeChecked();

    await serve(ws.last(), {
      type: "lobby",
      game: lobbyOf([host(), boris({ ready: true })]),
    });

    expect(readySwitch()).toBeChecked();
  });

  test("отказ сервера показан его текстом", async () => {
    await openLobby({
      as: "u2",
      game: lobbyOf([host(), boris({ ready: false })]),
      routes: {
        [`POST ${READY}`]: {
          status: 409,
          json: { detail: "Сначала создайте персонажа." },
        },
      },
    });

    fireEvent.click(readySwitch());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сначала создайте персонажа.",
    );
  });
});

describe("начало игры: вплетение", () => {
  const WEAVING = "Мастер вплетает ваших героев в мир. Игра начнётся сама.";
  const thinking = () =>
    screen.queryByRole("status", { name: "Мастер думает…" });
  const RETRY = "/api/games/g1/host/retry";
  const weavingGame = (turn_phase: GameSummary["turn_phase"] = null) =>
    lobbyOf([host(), boris()], { phase: "weaving", turn_phase });

  test("в фазе weaving — надпись о вплетении вместо списка и кнопок лобби", async () => {
    await openLobby({ game: weavingGame("resolving") });

    expect(screen.getByText(WEAVING)).toBeInTheDocument();
    noButton("Начать");
    noButton("Пригласить");
    expect(screen.queryByRole("switch")).toBeNull();
    noButton(/Исключить/);
  });

  test("после «Начать» лобби само переходит к вплетению по событию lobby", async () => {
    const { ws } = await openLobby({ game: lobbyOf([host(), boris()]) });
    expect(screen.queryByText(WEAVING)).toBeNull();

    await serve(ws.last(), { type: "lobby", game: weavingGame() });

    expect(screen.getByText(WEAVING)).toBeInTheDocument();
  });

  test("когда мастер закончил вплетение (фаза playing), открывается экран игры", async () => {
    const { ws } = await openLobby({ game: weavingGame("resolving") });

    await serve(ws.last(), phaseEvent("playing", "discussion"));

    expect(useGame.getState()).toMatchObject({ screen: "game", gameId: "g1" });
  });

  test("если игра уже идёт к моменту подключения, открывается экран игры", async () => {
    await openLobby({
      game: lobbyOf([host(), boris()], {
        phase: "playing",
        turn_phase: "discussion",
      }),
    });

    expect(useGame.getState()).toMatchObject({ screen: "game", gameId: "g1" });
  });

  test("событие phase в фазе weaving не мешает показу вплетения", async () => {
    const { ws } = await openLobby({ game: weavingGame() });

    await serve(ws.last(), phaseEvent("weaving", "resolving"));

    expect(screen.getByText(WEAVING)).toBeInTheDocument();
    expect(useGame.getState().screen).toBe("lobby");
  });

  test("пока мастер вплетает героев — единое «Мастер думает…» с нейтральной фразой и временем", async () => {
    await openLobby({ game: weavingGame("resolving") });

    const card = thinking();
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent(/Ждём 0:00/);
    expect(screen.queryByText(/связывает ваши истории/)).toBeNull();
  });

  test("мастер не справился — «Мастер думает…» не показывается", async () => {
    await openLobby({ as: "u2", game: weavingGame("dm_failed") });

    expect(thinking()).toBeNull();
  });

  test("мастер не справился: хост видит сообщение и «Повторить»", async () => {
    await openLobby({ as: "u1", game: weavingGame("dm_failed") });

    expect(screen.getByRole("alert")).toHaveTextContent("Мастер не справился");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("хосту сообщение предлагает попробовать ещё раз, игроку — ждать хоста", async () => {
    await openLobby({ as: "u1", game: weavingGame("dm_failed") });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Мастер не справился с началом игры. Попробуйте ещё раз.",
    );
    expect(screen.queryByText(/Хост может повторить/)).toBeNull();
  });

  test("сбой пришёл только событием lobby, до события phase: сообщение и «Повторить» уже есть", async () => {
    const { ws } = await openLobby({ game: lobbyOf([host(), boris()]) });

    await serve(ws.last(), { type: "lobby", game: weavingGame("dm_failed") });

    expect(screen.getByRole("alert")).toHaveTextContent("Мастер не справился");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("мастер не справился: игроку — пояснение, кнопки нет", async () => {
    await openLobby({ as: "u2", game: weavingGame("dm_failed") });

    expect(screen.getByRole("alert")).toHaveTextContent("Мастер не справился");
    expect(screen.getByText(/Хост может повторить/)).toBeInTheDocument();
    noButton("Повторить");
  });

  test("«Повторить» шлёт host/retry", async () => {
    const { server } = await openLobby({
      game: weavingGame("dm_failed"),
      routes: {
        [`POST ${RETRY}`]: { json: phaseEvent("weaving", "resolving").phase },
      },
    });

    fireEvent.click(button("Повторить"));

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", RETRY)).toHaveLength(1),
    );
  });

  test("отказ повтора показан его текстом", async () => {
    await openLobby({
      game: weavingGame("dm_failed"),
      routes: {
        [`POST ${RETRY}`]: {
          status: 409,
          json: { detail: "Мастер ещё думает: дождитесь ответа." },
        },
      },
    });

    fireEvent.click(button("Повторить"));

    expect(await screen.findByText(/Мастер ещё думает/)).toBeInTheDocument();
  });

  test("после dm_failed → resolving кнопка и сообщение об ошибке пропадают", async () => {
    const { ws } = await openLobby({ game: weavingGame("dm_failed") });

    await serve(ws.last(), phaseEvent("weaving", "resolving"));

    noButton("Повторить");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  describe("долгое ожидание", () => {
    const MINUTE = 60_000;

    // Таймеры подменяются до открытия лобби, поэтому здесь нет findBy: обновления
    // приходят из act и продвижения часов.
    async function openWithFakeTimers(
      turn_phase: GameSummary["turn_phase"],
      as: "u1" | "u2",
    ) {
      vi.useFakeTimers({ toFake: FAKE_TIMERS });
      return openLobby({ as, game: weavingGame(turn_phase) });
    }

    const wait = (ms: number) =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });

    test("пока мастер думает меньше минуты, хосту «Повторить» не нужна", async () => {
      await openWithFakeTimers("resolving", "u1");

      await wait(MINUTE - 1000);

      noButton("Повторить");
    });

    test("через минуту без результата хост получает «Повторить»", async () => {
      await openWithFakeTimers("resolving", "u1");

      await wait(MINUTE);

      expect(button("Повторить")).toBeInTheDocument();
    });

    test("время ожидания идёт в карточке «Мастер думает…», фраза меняется по времени", async () => {
      vi.useFakeTimers({ toFake: [...FAKE_TIMERS, "Date"] });
      await openLobby({ as: "u2", game: weavingGame("resolving") });
      const first = thinking()?.textContent;

      await wait(25_000);

      expect(thinking()).toHaveTextContent("Ждём 0:25");
      expect(thinking()?.textContent).not.toBe(first);
    });

    test("через минуту «Повторить» у хоста — в карточке «Мастер думает…»", async () => {
      await openWithFakeTimers("resolving", "u1");

      await wait(MINUTE);

      expect(
        within(thinking() as HTMLElement).getByRole("button", {
          name: "Повторить",
        }),
      ).toBeInTheDocument();
    });

    test("через минуту хост видит и пояснение «Мастер думает дольше обычного»", async () => {
      await openWithFakeTimers("resolving", "u1");
      expect(screen.queryByText(/дольше обычного/)).toBeNull();

      await wait(MINUTE);

      expect(screen.getByText("Мастер думает дольше обычного.")).toBeVisible();
    });

    test("игроку пояснения о долгом ожидании нет", async () => {
      await openWithFakeTimers("resolving", "u2");

      await wait(5 * MINUTE);

      expect(screen.queryByText(/дольше обычного/)).toBeNull();
    });

    test("после сбоя вместо «дольше обычного» — сообщение о сбое", async () => {
      await openWithFakeTimers("dm_failed", "u1");

      await wait(5 * MINUTE);

      expect(screen.queryByText(/дольше обычного/)).toBeNull();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Мастер не справился",
      );
    });

    test("игроку кнопка не появляется и через минуту", async () => {
      await openWithFakeTimers("resolving", "u2");

      await wait(5 * MINUTE);

      noButton("Повторить");
    });

    test("игра в weaving без фазы хода (после перезапуска сервера) — тоже через минуту", async () => {
      await openWithFakeTimers(null, "u1");
      noButton("Повторить");

      await wait(MINUTE);

      expect(button("Повторить")).toBeInTheDocument();
    });

    describe("от серверного начала вплетения (phase_since, контракт B11, §2)", () => {
      const NOW = Date.parse("2026-10-20T12:00:00+00:00");
      const ago = (seconds: number) =>
        new Date(NOW - seconds * 1000).toISOString();

      /** Лобби открыто посреди вплетения: снимок, затем фаза с серверным началом. */
      async function reopenedAfter(seconds: number, as: "u1" | "u2" = "u1") {
        vi.useFakeTimers({ toFake: [...FAKE_TIMERS, "Date"] });
        vi.setSystemTime(NOW);
        const opened = await openLobby({ as, game: weavingGame("resolving") });
        await serve(opened.ws.last(), {
          type: "phase",
          phase: {
            ...phaseEvent("weaving", "resolving").phase,
            phase_since: ago(seconds),
          },
        });
        return opened;
      }

      test("перезагрузка на 50-й секунде: «Повторить» через 10 с, а не через минуту", async () => {
        await reopenedAfter(50);

        await wait(9_000);
        noButton("Повторить");
        await wait(1_000);

        expect(button("Повторить")).toBeInTheDocument();
        expect(
          screen.getByText("Мастер думает дольше обычного."),
        ).toBeVisible();
      });

      test("счётчик «Ждём» продолжает с серверного начала", async () => {
        await reopenedAfter(50, "u2");

        await wait(1_000);

        expect(thinking()).toHaveTextContent("Ждём 0:51");
      });

      test("игроку кнопки нет и при серверном начале", async () => {
        await reopenedAfter(120, "u2");

        await wait(1_000);

        noButton("Повторить");
      });

      // Финальное покрытие B11: после перезагрузки начало вплетения приходит в самом снимке,
      // а сдвиг часов стор берёт из его `server_time`.
      test("перезагрузка: снимок с началом вплетения, часы телефона отстают на 3 минуты — «Повторить» через 10 с", async () => {
        vi.useFakeTimers({ toFake: [...FAKE_TIMERS, "Date"] });
        vi.setSystemTime(NOW);
        const serverNow = NOW + 180_000;
        const game = weavingGame("resolving");
        const { ws } = await openLobby({ as: "u1", game: null });
        await act(async () => {
          ws.last().open();
          ws.last().serve(
            snapshotOf(game, {
              server_time: new Date(serverNow).toISOString(),
              phase: {
                phase: "weaving",
                turn_phase: "resolving",
                deadline_at: null,
                remaining_seconds: null,
                scene: null,
                // По часам сервера вплетение идёт 50 с.
                phase_since: new Date(serverNow - 50_000).toISOString(),
                can_regenerate: false,
              },
            }),
          );
        });

        await wait(1_000);
        expect(thinking()).toHaveTextContent("Ждём 0:51");
        await wait(8_000);
        noButton("Повторить");
        await wait(1_000);

        expect(button("Повторить")).toBeInTheDocument();
      });
    });

    test("смена фазы хода отсчитывает минуту заново", async () => {
      const { ws } = await openWithFakeTimers("resolving", "u1");
      await wait(MINUTE - 1000);

      await serve(ws.last(), phaseEvent("weaving", null));
      await wait(MINUTE - 1000);

      noButton("Повторить");
    });
  });
});
