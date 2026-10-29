import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type {
  ActionBody,
  CombatView,
  FeedItem,
  Snapshot,
  TurnPhase,
} from "../api/types";
import { CONNECTION_LOST_NOTE } from "../components/access";
import {
  combatOf,
  combatWith,
  feedEntry,
  gameView,
  heroFighter,
  journalEntry,
  monsterFighter,
  myAction,
  phaseOf,
  rollEntry,
  sheetHero,
  startJournal,
} from "../components/fixtures";
import { OPTIONS } from "./character/fixtures";
import { sessionToken, useGame } from "../store/game";
import {
  fakeServer,
  fakeWebSocket,
  gameSummary,
  signInAs,
  snapshotOf,
} from "../test-setup";
import type { FakeReply, FakeSocket, RecordedCall } from "../test-setup";
import { buzz } from "../theme/haptics";
import { Game } from "./Game";

// Вибрация Telegram подменена: тесты экрана проверяют, на какие события она зовётся; сам вызов
// SDK (только внутри Telegram и по настройке) проверяет theme/haptics.test.ts.
vi.mock("../theme/haptics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../theme/haptics")>()),
  buzz: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(buzz).mockClear();
});

// Подменяются только таймеры сокета и экрана: React и промисы работают как обычно.
const FAKE_TIMERS: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval"];

const DRAFT = "PUT /api/games/g1/action";
const SUBMIT = "POST /api/games/g1/action/submit";

type Routes = Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;

/** Снимок игры в обсуждении хода; блоки переопределяются точечно. */
function snapshotWith(overrides: Partial<Snapshot> = {}): Snapshot {
  const view = gameView();
  return snapshotOf(view.game, {
    phase: view.phase,
    statuses: view.statuses,
    character: view.character,
    party: view.party,
    feed: [
      feedEntry(1, { text: "Дверь склепа поддаётся с тихим вздохом." }),
      rollEntry(2),
    ],
    ...overrides,
  });
}

interface OpenOptions {
  as?: "u1" | "u2";
  routes?: Routes;
  /** Первый снимок; `null` — сокет пока молчит. */
  snapshot?: Snapshot | null;
  strict?: boolean;
}

async function openGame({
  as = "u1",
  routes = {},
  snapshot = snapshotWith(),
  strict = false,
}: OpenOptions = {}) {
  const ws = fakeWebSocket();
  signInAs({ id: as, name: as === "u1" ? "Вика" : "Борис" });
  useGame.getState().show({ screen: "game", gameId: "g1" });
  let draft: ActionBody | null = null;
  const server = fakeServer({
    "GET /api/content/creation-options": { json: OPTIONS },
    [DRAFT]: (call): FakeReply => {
      draft = call.body as ActionBody;
      return { json: { ...draft, status: "draft" } };
    },
    [SUBMIT]: (): FakeReply => ({
      json: { ...(draft as ActionBody), status: "submitted" },
    }),
    "POST /api/games/g1/host/collect": {
      json: phaseOf("collecting"),
    },
    ...routes,
  });
  const tree = (
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <Game />
    </ApiContext>
  );
  const view = render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  if (snapshot !== null) await connect(ws.last(), snapshot);
  return { ws, server, ...view };
}

async function connect(socket: FakeSocket, snapshot: Snapshot) {
  await act(async () => {
    socket.open();
    socket.serve(snapshot);
  });
}

const serve = (socket: FakeSocket, frame: unknown) =>
  act(() => socket.serve(frame));

const tab = (name: string) => screen.getByRole("tab", { name });
const button = (name: string | RegExp) => screen.getByRole("button", { name });
const noButton = (name: string | RegExp) =>
  expect(screen.queryByRole("button", { name })).toBeNull();

describe("подключение", () => {
  test("экран сам слушает сокет игры с токеном сессии", async () => {
    const { ws } = await openGame({ snapshot: null });

    expect(ws.sockets).toHaveLength(1);
    expect(ws.last().url).toMatch(/\/ws\/games\/g1\?token=tok-u1$/);
  });

  test("пока снимка нет — загрузка", async () => {
    await openGame({ snapshot: null });

    expect(screen.getByRole("status")).toHaveTextContent("Подключаемся к игре");
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  test("StrictMode: прежний сокет закрыт, живёт один", async () => {
    const { ws } = await openGame({ strict: true, snapshot: null });

    const open = ws.sockets.filter((socket) => socket.clientCloseCode === null);
    expect(open).toHaveLength(1);
  });

  test("уход с экрана закрывает сокет", async () => {
    const { ws, unmount } = await openGame();

    unmount();

    expect(ws.last().clientCloseCode).not.toBeNull();
  });

  test("игры не выбрано — сообщение и возврат к играм", () => {
    signInAs({ id: "u1", name: "Вика" });
    const server = fakeServer({});
    render(
      <ApiContext value={createApi(server.fetch, sessionToken)}>
        <Game />
      </ApiContext>,
    );

    expect(screen.getByText("Игра не выбрана")).toBeInTheDocument();
    fireEvent.click(button("К моим играм"));
    expect(useGame.getState().screen).toBe("games");
  });

  test("«К моим играм» ведёт к списку игр", async () => {
    await openGame();

    fireEvent.click(button("К моим играм"));

    expect(useGame.getState().screen).toBe("games");
  });

  test("обрыв связи: «Переподключаемся…», после нового снимка пропадает", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame();

    act(() => ws.last().drop(1006));
    expect(screen.getByText(/Переподключаемся/)).toBeInTheDocument();
    expect(tab("Рассказ")).toBeInTheDocument(); // игра на экране остаётся

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      ws.last().open();
      ws.last().serve(snapshotWith());
    });

    expect(screen.queryByText(/Переподключаемся/)).toBeNull();
  });

  test("зависшая связь (нет pong) тоже даёт «Переподключаемся…»", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    await openGame();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000 + 10_000);
    });

    expect(screen.getByText(/Переподключаемся/)).toBeInTheDocument();
  });

  test("событие error показано, игра остаётся", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "error",
      detail: "Не удалось загрузить игру.",
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Не удалось загрузить игру.",
    );
    expect(tab("Рассказ")).toBeInTheDocument();
  });

  test("сокет закрыт кодом 4403 — «Мои игры» с пояснением", async () => {
    const { ws } = await openGame();

    act(() => ws.last().drop(4403));

    expect(useGame.getState()).toMatchObject({
      screen: "games",
      notice: "Игра недоступна.",
    });
  });
});

describe("обрыв связи на экране игры", () => {
  const BANNER = /Переподключаемся/;
  const COMPOSER = "Что делает твой персонаж?";

  /** Связь вернулась: сокет открыт заново и прислал снимок (его строка хода — как у сервера). */
  async function reconnect(
    ws: { last: () => FakeSocket },
    snapshot: Snapshot = snapshotWith(),
  ) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      ws.last().open();
      ws.last().serve(snapshot);
    });
  }

  test("плашка стоит внутри прилипающей строки статуса и одна", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame();

    act(() => ws.last().drop(1006));

    expect(screen.getAllByText(BANNER)).toHaveLength(1);
    const bar = screen.getByText(BANNER).closest(".game-status");
    expect(bar).not.toBeNull();
    expect(bar).toContainElement(screen.getByText("Обсуждение"));
  });

  test("плашка видна на любой вкладке: она в строке статуса над ними, а не в панели", async () => {
    const { ws } = await openGame();
    act(() => ws.last().drop(1006));

    fireEvent.click(tab("Персонаж"));
    await screen.findByRole("heading", { name: "Лира" });
    fireEvent.click(tab("Отряд"));

    expect(screen.getByText(BANNER)).toBeVisible();
    expect(screen.getByText(BANNER).closest("[role='tabpanel']")).toBeNull();
  });

  test.each([
    ["Бой", () => combatSnapshot("pc_lira")],
    ["Привал", () => campSnapshot()],
  ])(
    "плашка есть и на вкладке «%s», в той же строке статуса",
    async (name, snapshot) => {
      const { ws } = await openGame({ snapshot: snapshot() });
      fireEvent.click(tab(name));

      act(() => ws.last().drop(1006));

      expect(screen.getByText(BANNER)).toBeVisible();
      expect(screen.getByText(BANNER).closest(".game-status")).not.toBeNull();
    },
  );

  test("поле хода при обрыве закрыто с пояснением, а после возврата связи открывается само", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame();
    fireEvent.change(screen.getByLabelText(COMPOSER), {
      target: { value: "Осматриваю дверь" },
    });
    expect(screen.getByLabelText(COMPOSER)).toBeEnabled();

    act(() => ws.last().drop(1006));

    expect(screen.getByLabelText(COMPOSER)).toBeDisabled();
    expect(button("Готов")).toBeDisabled();
    expect(button("Пас")).toBeDisabled();
    expect(screen.getByText(/^Нет связи с игрой/)).toBeInTheDocument();

    // За время обрыва черновик ушёл на сервер (REST), и снимок возвращает его же.
    await reconnect(
      ws,
      snapshotWith({ action: myAction({ text: "Осматриваю дверь" }) }),
    );

    expect(screen.queryByText(BANNER)).toBeNull();
    expect(screen.queryByText(/^Нет связи с игрой/)).toBeNull();
    expect(screen.getByLabelText(COMPOSER)).toBeEnabled();
    expect(screen.getByLabelText(COMPOSER)).toHaveValue("Осматриваю дверь");
  });

  test("в свой ход в бою поле на вкладке «Бой» при обрыве закрыто, потом открывается", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });
    fireEvent.click(tab("Бой"));
    expect(screen.getByLabelText(COMPOSER)).toBeEnabled();

    act(() => ws.last().drop(1006));
    expect(screen.getByLabelText(COMPOSER)).toBeDisabled();

    await reconnect(ws, combatSnapshot("pc_lira"));
    expect(screen.getByLabelText(COMPOSER)).toBeEnabled();
  });

  test("привал: «Сказать у костра» при обрыве закрыто тем же пояснением и открывается после возврата", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Привал"));
    expect(screen.getByLabelText("Сказать у костра")).toBeEnabled();

    act(() => ws.last().drop(1006));

    expect(screen.getByLabelText("Сказать у костра")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Сказать" })).toBeDisabled();
    expect(screen.getByText(CONNECTION_LOST_NOTE)).toBeInTheDocument();

    await reconnect(ws, campSnapshot());

    expect(screen.getByLabelText("Сказать у костра")).toBeEnabled();
    expect(screen.queryByText(CONNECTION_LOST_NOTE)).toBeNull();
  });

  test("обрыв не сбрасывает дневник: раздел и отметки остаются, снимок после возврата обновляет записи", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({
      snapshot: snapshotWith({ journal: startJournal() }),
    });
    const section = (name: string) =>
      screen.getByRole("tab", { name: new RegExp(`^${name} \\d+$`) });
    const card = (name: string) => within(screen.getByRole("region", { name }));
    fireEvent.click(tab("Дневник"));
    fireEvent.click(section("Места"));
    fireEvent.click(section("Главное"));
    fireEvent.click(section("Места"));
    expect(card("Старое кладбище").queryByText("новое")).toBeNull();

    act(() => ws.last().drop(1006));
    // Снимок после возврата: за время обрыва отряд пришёл на кладбище.
    const journal = startJournal();
    journal[2] = { ...journal[2], revision: 2, visited_turn: 3 };
    await reconnect(ws, snapshotWith({ journal }));

    expect(section("Места")).toHaveAttribute("aria-selected", "true");
    expect(
      card("Старое кладбище").getByText("были здесь: раунд 3"),
    ).toBeVisible();
    expect(card("Старое кладбище").getByText("новое")).toBeVisible();
    expect(card("Деревня Мирра").queryByText("новое")).toBeNull();
  });

  test("связь пропала до первого снимка: плашка остаётся и без строки статуса", async () => {
    const { ws } = await openGame({ snapshot: null });

    act(() => ws.last().drop(1006));

    expect(screen.getAllByText(BANNER)).toHaveLength(1);
    expect(screen.getByText(/Подключаемся к игре/)).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  test("зависшая связь (нет pong): поле хода закрывается так же, как при обрыве", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    await openGame();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000 + 10_000);
    });

    expect(screen.getByText(BANNER)).toBeInTheDocument();
    expect(screen.getByLabelText(COMPOSER)).toBeDisabled();
  });

  test("плашка без движения: ни при каком выборе «меньше движения» классов анимации нет", async () => {
    for (const reduce of [false, true]) {
      vi.stubGlobal("matchMedia", (query: string) => ({
        matches: query.includes("prefers-reduced-motion") && reduce,
        addEventListener: () => {},
        removeEventListener: () => {},
      }));
      const { ws, unmount } = await openGame();

      act(() => ws.last().drop(1006));

      expect(screen.getByText(BANNER)).toBeInTheDocument();
      expect(screen.getByText(BANNER).closest(".game-status")).not.toBeNull();
      expect(
        screen
          .getByText(BANNER)
          .closest(".game-status")
          ?.querySelector("[class*='ui-anim']"),
      ).toBeNull();
      unmount();
    }
  });
});

describe("вкладки", () => {
  test("четыре вкладки в порядке «В» (решение владельца): «Дневник» последней; открыта «Рассказ»", async () => {
    await openGame();

    expect(screen.getAllByRole("tab").map((node) => node.textContent)).toEqual([
      "Рассказ",
      "Персонаж",
      "Отряд",
      "Дневник",
    ]);
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "false");
  });

  test("«Рассказ»: лента с карточкой броска и поле хода", async () => {
    await openGame();

    expect(
      screen.getByText("Дверь склепа поддаётся с тихим вздохом."),
    ).toBeVisible();
    expect(screen.getByRole("article")).toBeVisible();
    expect(screen.getByLabelText("Что делает твой персонаж?")).toBeVisible();
  });

  test("«Персонаж» показывает лист героя, лента скрыта", async () => {
    await openGame();

    fireEvent.click(tab("Персонаж"));

    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "Лира" })).toBeVisible();
    expect(screen.getByText("Класс доспеха")).toBeVisible();
    expect(
      screen.getByText("Дверь склепа поддаётся с тихим вздохом."),
    ).not.toBeVisible();
  });

  test("«Отряд» показывает здоровье героев", async () => {
    await openGame();

    fireEvent.click(tab("Отряд"));

    expect(screen.getByRole("meter", { name: "Здоровье: Лира" })).toBeVisible();
    expect(
      screen.getByRole("meter", { name: "Здоровье: Борин" }),
    ).toBeVisible();
  });

  test("возврат на «Рассказ» не теряет написанное в поле", async () => {
    await openGame();
    const field = () => screen.getByLabelText("Что делает твой персонаж?");
    fireEvent.change(field(), { target: { value: "Осматриваю дверь" } });

    fireEvent.click(tab("Отряд"));
    fireEvent.click(tab("Рассказ"));

    expect(field()).toHaveValue("Осматриваю дверь");
  });

  test("стрелки переключают вкладки", async () => {
    await openGame();

    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowRight" });
    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Персонаж"), { key: "ArrowRight" });
    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Отряд"), { key: "ArrowRight" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Дневник"), { key: "ArrowRight" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowLeft" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
  });

  test("Home и End ведут к первой и последней вкладке, фокус идёт за выбором", async () => {
    await openGame();
    fireEvent.click(tab("Персонаж"));

    fireEvent.keyDown(tab("Персонаж"), { key: "End" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
    expect(tab("Дневник")).toHaveFocus();

    fireEvent.keyDown(tab("Дневник"), { key: "Home" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tab("Рассказ")).toHaveFocus();
  });

  test("выбранная вкладка в порядке табуляции, остальные нет", async () => {
    await openGame();

    expect(tab("Рассказ")).toHaveAttribute("tabindex", "0");
    for (const other of ["Дневник", "Персонаж", "Отряд"]) {
      expect(tab(other)).toHaveAttribute("tabindex", "-1");
    }

    fireEvent.click(tab("Отряд"));

    expect(tab("Отряд")).toHaveAttribute("tabindex", "0");
    expect(tab("Рассказ")).toHaveAttribute("tabindex", "-1");
  });

  test.each(["Enter", "a", "ArrowDown", "Tab"])(
    "клавиша «%s» вкладки не переключает: только стрелки, Home и End",
    async (key) => {
      await openGame();
      fireEvent.click(tab("Персонаж"));

      fireEvent.keyDown(tab("Персонаж"), { key });

      expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    },
  );

  test("панели связаны с вкладками", async () => {
    await openGame();

    const panel = screen.getByRole("tabpanel", { name: "Рассказ" });
    expect(panel).toBeVisible();
    expect(tab("Рассказ")).toHaveAttribute("aria-controls", panel.id);
  });

  test("строка статуса видна на любой вкладке", async () => {
    await openGame();

    fireEvent.click(tab("Отряд"));

    expect(screen.getByText("Обсуждение")).toBeVisible();
  });
});

describe("имя героя в ленте открывает его лист (B10.3)", () => {
  /** Лента с ходами своего героя (Лира, u1) и чужого (Борин, u2). */
  const withActions = () =>
    snapshotWith({
      feed: [
        feedEntry(1, { text: "Дверь склепа поддаётся с тихим вздохом." }),
        feedEntry(2, {
          kind: "action",
          text: "осматривает дверь",
          data: { actor_id: "pc_lira", actor: "Лира", secret: false },
        }),
        feedEntry(3, {
          kind: "action",
          text: "держит факел",
          data: { actor_id: "pc_boris", actor: "Борин", secret: false },
        }),
      ],
    });
  const heroName = (name: string) =>
    within(screen.getByRole("list", { name: "Лента" })).getByRole("button", {
      name,
    });

  test("своё имя — вкладка «Персонаж» с листом, фокус на вкладке", async () => {
    await openGame({ snapshot: withActions() });

    fireEvent.click(heroName("Лира"));

    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    expect(tab("Персонаж")).toHaveFocus();
    expect(await screen.findByRole("heading", { name: "Лира" })).toBeVisible();
  });

  test("чужое имя — «Отряд», карточка этого героя выводится в видимость", async () => {
    // В jsdom прокрутки нет: подменяем её и запоминаем, что выводилось в видимость.
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    onTestFinished(() => {
      Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    });
    await openGame({ snapshot: withActions() });
    scrollIntoView.mockClear();

    fireEvent.click(heroName("Борин"));

    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
    expect(tab("Отряд")).toHaveFocus();
    const meter = screen.getByRole("meter", { name: "Здоровье: Борин" });
    expect(meter).toBeVisible();
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    const card = scrollIntoView.mock.contexts[0] as HTMLElement;
    expect(card).toContainElement(meter);
    expect(card).not.toHaveTextContent("Лира");
  });

  test("имя монстра или NPC — не кнопка: листа у них нет", async () => {
    await openGame({
      snapshot: snapshotWith({
        feed: [
          feedEntry(1, {
            kind: "action",
            text: "рычит",
            data: { actor_id: "m_wolf", actor: "Волк" },
          }),
        ],
      }),
    });

    expect(
      within(screen.getByRole("list", { name: "Лента" })).queryByRole("button"),
    ).toBeNull();
  });
});

describe("строка статуса и панель хоста", () => {
  test("фаза и значки игроков из снимка", async () => {
    await openGame();

    expect(screen.getByText("Обсуждение")).toBeInTheDocument();
    expect(screen.getAllByText("Пишет")).toHaveLength(3);
  });

  test("событие фазы обновляет подпись, а player_status — значок", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });
    await serve(ws.last(), {
      type: "player_status",
      user_id: "u2",
      status: "ready",
    });

    expect(screen.getByText("Сбор ходов")).toBeInTheDocument();
    // «Готов» есть и на кнопке хода; значок игрока — плашка
    expect(
      screen.getByText("Готов", { selector: ".ui-badge" }),
    ).toBeInTheDocument();
  });

  test("хост видит кнопки, остальные — нет", async () => {
    await openGame({ as: "u1" });
    expect(button("Собрать ходы")).toBeInTheDocument();
  });

  test("не хост панели не получает", async () => {
    await openGame({ as: "u2" });

    noButton("Собрать ходы");
    noButton("Пауза");
  });

  test("«Собрать ходы» шлёт запрос хоста", async () => {
    const { server } = await openGame();

    fireEvent.click(button("Собрать ходы"));

    await vi.waitFor(() =>
      expect(
        server.calls.some((call) => call.path === "/api/games/g1/host/collect"),
      ).toBe(true),
    );
  });
});

describe("«Повторить», пока мастер думает (решение владельца R3)", () => {
  const RETRY = "POST /api/games/g1/host/retry";
  const MINUTE = 60_000;
  const wait = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  const resolving = () => snapshotWith({ phase: phaseOf("resolving") });

  test("хосту кнопка появляется ровно через минуту ожидания и шлёт host/retry", async () => {
    const { server } = await openGame({
      snapshot: resolving(),
      routes: { [RETRY]: { json: phaseOf("resolving") } },
    });
    expect(
      screen.getByRole("status", { name: "Мастер думает…" }),
    ).toBeVisible();

    await wait(MINUTE - 1000);
    noButton("Повторить");
    await wait(1000);

    expect(button("Повторить")).toBeVisible();
    fireEvent.click(button("Повторить"));
    await wait(0);
    expect(
      server.calls.filter((call) => `${call.method} ${call.path}` === RETRY),
    ).toHaveLength(1);
  });

  test("игроку, не хосту, кнопки нет и через пять минут", async () => {
    await openGame({ as: "u2", snapshot: resolving() });

    await wait(5 * MINUTE);

    noButton("Повторить");
  });

  test("мастер ответил раньше минуты: кнопка не появляется", async () => {
    const { ws } = await openGame({ snapshot: resolving() });
    await wait(MINUTE - 5000);

    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });
    await wait(2 * MINUTE);

    noButton("Повторить");
  });

  test("в «Мастер не справился» кнопка есть сразу, без ожидания", async () => {
    await openGame({ snapshot: snapshotWith({ phase: phaseOf("dm_failed") }) });

    expect(button("Повторить")).toBeVisible();
  });
});

describe("лента живёт по событиям", () => {
  test("новая запись появляется в конце, повтор не дублируется", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(3, { text: "Из темноты донёсся шорох." }),
    });
    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(3, { text: "Из темноты донёсся шорох." }),
    });

    expect(screen.getAllByText("Из темноты донёсся шорох.")).toHaveLength(1);
    expect(screen.getAllByRole("listitem").at(-1)).toHaveTextContent(
      "Из темноты донёсся шорох.",
    );
  });

  test("надгробие убирает запись (после «Перегенерировать»)", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "feed_item",
      item: { id: 1, superseded: true },
    });

    expect(
      screen.queryByText("Дверь склепа поддаётся с тихим вздохом."),
    ).toBeNull();
  });

  test("личное сообщение — письмо «Только тебе»", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(3, {
        kind: "private",
        private: true,
        text: "Стражник заметил письмо.",
      }),
    });

    expect(screen.getByRole("note", { name: "Только тебе" })).toHaveTextContent(
      "Стражник заметил письмо.",
    );
  });
});

describe("ход игрока на экране игры", () => {
  test("написать и сдать: черновик уходит перед «Готов», затем «Ход сдан»", async () => {
    const { server } = await openGame();
    fireEvent.change(screen.getByLabelText("Что делает твой персонаж?"), {
      target: { value: "Осматриваю дверь" },
    });

    fireEvent.click(button("Готов"));

    expect(await screen.findByText("Ход сдан")).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      DRAFT,
      SUBMIT,
    ]);
  });

  test.each<[TurnPhase, string]>([
    ["resolving", "Мастер думает…"],
    ["dm_failed", "Мастер не справился"],
  ])("фаза %s: поле хода закрыто на экране", async (turnPhase, label) => {
    const { ws } = await openGame();

    await serve(ws.last(), { type: "phase", phase: phaseOf(turnPhase) });

    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Что делает твой персонаж?")).toBeDisabled();
  });

  test("недописанный текст прежнего хода в новый не переходит", async () => {
    const { ws } = await openGame();
    fireEvent.change(screen.getByLabelText("Что делает твой персонаж?"), {
      target: { value: "Не успел дописать" },
    });

    await serve(ws.last(), { type: "phase", phase: phaseOf("resolving") });
    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });

    expect(screen.getByLabelText("Что делает твой персонаж?")).toHaveValue("");
  });

  test("мастер закончил — новый ход начинается с пустого поля", async () => {
    const { ws } = await openGame();
    fireEvent.change(screen.getByLabelText("Что делает твой персонаж?"), {
      target: { value: "Осматриваю дверь" },
    });
    fireEvent.click(button("Готов"));
    await screen.findByText("Ход сдан");

    await serve(ws.last(), { type: "phase", phase: phaseOf("resolving") });
    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });

    expect(screen.getByLabelText("Что делает твой персонаж?")).toHaveValue("");
    expect(screen.queryByText("Ход сдан")).toBeNull();
  });
});

describe("игра ещё не пошла", () => {
  test.each(["lobby", "weaving"] as const)(
    "фаза %s: вместо игры открывается лобби",
    async (phase) => {
      const game = gameSummary({
        phase,
        players: gameView().game.players,
      });
      await openGame({
        snapshot: snapshotOf(game, { phase: phaseOf(null, { phase }) }),
      });

      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
      });
    },
  );

  test("игра окончена: экран остаётся, подпись «Игра завершена», поле закрыто", async () => {
    const game = gameSummary({ phase: "finished" });
    await openGame({
      snapshot: snapshotWith({
        game,
        phase: phaseOf(null, { phase: "finished" }),
      }),
    });

    expect(screen.getByText("Игра завершена")).toBeInTheDocument();
    expect(screen.getByLabelText("Что делает твой персонаж?")).toBeDisabled();
    expect(useGame.getState().screen).toBe("game");
  });
});

// --- бой и привал ---

const FIELD = "Что делает твой персонаж?";
// Срок привала уже вышел по серверным часам снимка: отсчёт не тикает и не шумит предупреждениями act.
const CAMP_DEADLINE = "2026-10-20T11:59:00+00:00";

const tabNames = () =>
  screen.getAllByRole("tab").map((node) => node.textContent);
// Открытая панель по названию вкладки; скрытые панели Testing Library не видит.
const panel = (name: string) => screen.getByRole("tabpanel", { name });

/** Бой в снимке: ход `current`, фаза `combat_wait`. */
function combatSnapshot(
  current: string | null = "pc_lira",
  overrides: Partial<Snapshot> = {},
) {
  return snapshotWith({
    phase: phaseOf("combat_wait"),
    combat: combatOf(current),
    ...overrides,
  });
}

/** Привал в снимке: тема мастера и реплика у костра; сервер описывает привал блоком `rest` (B9, §3). */
function campSnapshot(overrides: Partial<Snapshot> = {}) {
  const line = feedEntry(2, {
    kind: "camp_line",
    text: "Подкину дров.",
    data: { actor_id: "pc_boris", actor: "Борин" },
  });
  const feed: FeedItem[] = [
    feedEntry(1, { kind: "narration", text: "Вы разбиваете лагерь у реки." }),
    line,
  ];
  return snapshotWith({
    phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    feed,
    rest: {
      kind: "long",
      deadline_at: CAMP_DEADLINE,
      my_ideas: [],
      lines: [line],
    },
    ...overrides,
  });
}

const PHASE_REPLY: FakeReply = { json: phaseOf("camp") };

describe("вкладка «Бой»", () => {
  test("без боя вкладки нет", async () => {
    await openGame();

    expect(tabNames()).toEqual(["Рассказ", "Персонаж", "Отряд", "Дневник"]);
  });

  test("бой в снимке: вкладка «Бой» сразу после «Рассказа»", async () => {
    await openGame({ snapshot: combatSnapshot() });

    expect(tabNames()).toEqual([
      "Рассказ",
      "Бой",
      "Персонаж",
      "Отряд",
      "Дневник",
    ]);
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
  });

  test("события боя добавляют и убирают вкладку", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    expect(tabNames()).toContain("Бой");

    await serve(ws.last(), { type: "combat", combat: null });
    expect(tabNames()).not.toContain("Бой");
  });

  test("порядок хода, зоны и раунд — на вкладке «Бой»", async () => {
    await openGame({ snapshot: combatSnapshot("skeleton-1") });

    fireEvent.click(tab("Бой"));

    const combat = within(panel("Бой"));
    expect(panel("Бой")).toBeVisible();
    expect(combat.getByRole("list", { name: "Порядок хода" })).toBeVisible();
    expect(combat.getByRole("list", { name: "Зоны боя" })).toBeVisible();
    expect(combat.getByText(/раунд 1/)).toBeVisible();
    expect(combat.getByText("Ходит Скелет.")).toBeVisible();
  });

  test("ход бойца обновляется событием combat", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });
    fireEvent.click(tab("Бой"));
    expect(screen.getByLabelText(FIELD)).toBeEnabled();

    await serve(ws.last(), { type: "combat", combat: combatOf("skeleton-1") });

    expect(screen.queryByLabelText(FIELD)).toBeNull();
    expect(within(panel("Бой")).getByText("Ходит Скелет.")).toBeVisible();
  });

  test("ходит мой герой: поле хода на вкладке «Бой» открыто", async () => {
    await openGame({ snapshot: combatSnapshot("pc_lira") });

    fireEvent.click(tab("Бой"));

    expect(screen.getByLabelText(FIELD)).toBeVisible();
    expect(screen.getByLabelText(FIELD)).toBeEnabled();
  });

  test("ходит чужой герой: на «Бой» поля нет, видно «Ходит Борин.»", async () => {
    const combat = combatWith("pc_boris", [
      heroFighter("pc_lira", "Лира"),
      heroFighter("pc_boris", "Борин"),
      monsterFighter("skeleton-1", "Скелет"),
    ]);
    await openGame({
      snapshot: combatSnapshot("pc_boris", { combat }),
    });

    fireEvent.click(tab("Бой"));

    expect(screen.queryByLabelText(FIELD)).toBeNull();
    expect(within(panel("Бой")).getByText("Ходит Борин.")).toBeVisible();
  });

  test("поле хода в бою живёт только на вкладке «Бой», а не в двух местах сразу", async () => {
    await openGame({ snapshot: combatSnapshot("pc_lira") });

    expect(screen.getAllByLabelText(FIELD)).toHaveLength(1);
    expect(screen.getByLabelText(FIELD)).not.toBeVisible(); // «Рассказ» открыта
  });

  test("на «Рассказе» в бою — подсказка и «К бою»; свой ход назван", async () => {
    await openGame({ snapshot: combatSnapshot("pc_lira") });

    const story = within(panel("Рассказ"));
    expect(story.getByText(/Сейчас ваш ход/)).toBeVisible();

    fireEvent.click(story.getByRole("button", { name: "К бою" }));

    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText(FIELD)).toBeVisible();
  });

  test("«К бою» основная кнопка в мой ход и второстепенная в чужой", async () => {
    const mine = await openGame({ snapshot: combatSnapshot("pc_lira") });
    expect(
      within(panel("Рассказ")).getByRole("button", { name: "К бою" }),
    ).toHaveClass("ui-button--primary");
    mine.unmount();

    await openGame({ snapshot: combatSnapshot("skeleton-1") });

    expect(
      within(panel("Рассказ")).getByRole("button", { name: "К бою" }),
    ).toHaveClass("ui-button--secondary");
  });

  test("на «Рассказе» в бою сказано, кто ходит, когда не мой ход", async () => {
    await openGame({ snapshot: combatSnapshot("skeleton-1") });

    const story = within(panel("Рассказ"));
    expect(story.getByText("Ходит Скелет.")).toBeVisible();
    expect(story.queryByText(/Сейчас ваш ход/)).toBeNull();
  });

  test("бой кончился: поле хода возвращается на «Рассказ», открытая «Бой» уступает ей место", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });
    fireEvent.click(tab("Бой"));

    await serve(ws.last(), { type: "combat", combat: null });
    await serve(ws.last(), { type: "phase", phase: phaseOf("resolving") });
    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });

    expect(tabNames()).toEqual(["Рассказ", "Персонаж", "Отряд", "Дневник"]);
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText(FIELD)).toBeVisible();
  });

  test("новый бой не открывает вкладку «Бой» сам, если игрок её уже покидал", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });
    fireEvent.click(tab("Бой"));
    await serve(ws.last(), { type: "combat", combat: null });

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tab("Бой")).toHaveAttribute("aria-selected", "false");
  });

  test("стрелки обходят пять вкладок по кругу", async () => {
    await openGame({ snapshot: combatSnapshot() });

    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowRight" });
    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Бой"), { key: "ArrowRight" });
    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Персонаж"), { key: "ArrowRight" });
    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Отряд"), { key: "ArrowRight" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Дневник"), { key: "ArrowRight" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowLeft" });
    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
  });

  test("при шести вкладках (бой и привал) оба раздела идут сразу за «Рассказом»", async () => {
    await openGame({
      snapshot: combatSnapshot("pc_lira", {
        phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
      }),
    });

    expect(tabNames()).toEqual([
      "Рассказ",
      "Бой",
      "Привал",
      "Персонаж",
      "Отряд",
      "Дневник",
    ]);
  });

  test("поле хода и «Готов» в бою шлют ход", async () => {
    const { server } = await openGame({ snapshot: combatSnapshot("pc_lira") });
    fireEvent.click(tab("Бой"));
    fireEvent.change(screen.getByLabelText(FIELD), {
      target: { value: "Бью скелета мечом" },
    });

    fireEvent.click(button("Готов"));

    expect(await screen.findByText("Ход сдан")).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      DRAFT,
      SUBMIT,
    ]);
  });
});

describe("вкладка «Дневник»", () => {
  // Разделы дневника — тоже вкладки, но в своём наборе; ищем их по названию со счётчиком.
  const section = (name: string) =>
    screen.getByRole("tab", { name: new RegExp(`^${name} \\d+$`) });
  const journal = () => within(panel("Дневник"));

  test("дневник из снимка: разделы со счётчиками и записи «Главного»", async () => {
    await openGame({ snapshot: snapshotWith({ journal: startJournal() }) });

    fireEvent.click(tab("Дневник"));

    expect(tab("Дневник")).toHaveAttribute("aria-selected", "true");
    expect(section("Места")).toHaveTextContent("Места 2");
    expect(
      journal().getByText(
        "Староста Мирры просит узнать, кто тревожит покой склепа.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText("Дверь склепа поддаётся с тихим вздохом."),
    ).not.toBeVisible();
  });

  test("journal_entry добавляет новую запись и дополняет прежнюю", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({ journal: startJournal() }),
    });
    fireEvent.click(tab("Дневник"));
    fireEvent.click(section("Места"));

    await serve(ws.last(), {
      type: "journal_entry",
      entry: journalEntry("location:crypt", {
        title: "Склеп",
        text: "Тяжёлая дверь в холме.",
        turn: 2,
        visited_turn: 2,
      }),
    });
    await serve(ws.last(), {
      type: "journal_entry",
      entry: journalEntry("location:cemetery", {
        title: "Старое кладбище",
        text: "За деревней, у холма со склепом.\n\nНочью здесь бродят огни.",
        revision: 3,
        visited_turn: 2,
      }),
    });

    expect(section("Места")).toHaveTextContent("Места 3");
    const crypt = within(journal().getByRole("region", { name: "Склеп" }));
    expect(crypt.getByText("были здесь: раунд 2")).toBeVisible();
    expect(
      journal().getAllByRole("region", { name: "Старое кладбище" }),
    ).toHaveLength(1);
    expect(journal().getByText("Ночью здесь бродят огни.")).toBeVisible();
  });

  test("journal заменяет дневник целиком: записи отката пропадают", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({ journal: startJournal() }),
    });
    fireEvent.click(tab("Дневник"));

    await serve(ws.last(), {
      type: "journal",
      entries: [startJournal()[0]],
    });
    fireEvent.click(section("Места"));

    expect(section("Места")).toHaveTextContent("Места 0");
    expect(journal().getByText("Пока ничего не известно")).toBeVisible();
  });

  test("«новое» гаснет, когда игрок вернулся в дневник после другой вкладки", async () => {
    await openGame({ snapshot: snapshotWith({ journal: startJournal() }) });

    fireEvent.click(tab("Дневник"));
    const intro = () => journal().getByRole("region", { name: "Главное" });
    expect(within(intro()).getByText("новое")).toBeVisible();

    fireEvent.click(tab("Рассказ"));
    fireEvent.click(tab("Дневник"));

    expect(within(intro()).queryByText("новое")).toBeNull();
    expect(
      JSON.parse(window.localStorage.getItem("dnd-journal-seen:g1:u1") ?? "{}"),
    ).toEqual({ "intro@all": 1 });
  });

  test("пока игрок не открывал дневник, ничего не помечено увиденным", async () => {
    await openGame({ snapshot: snapshotWith({ journal: startJournal() }) });

    fireEvent.click(tab("Отряд"));

    expect(window.localStorage.getItem("dnd-journal-seen:g1:u1")).toBeNull();
  });

  test("в бою дневник открывается так же", async () => {
    await openGame({
      snapshot: combatSnapshot("pc_lira", { journal: startJournal() }),
    });

    fireEvent.click(tab("Дневник"));

    expect(journal().getByRole("region", { name: "Главное" })).toBeVisible();
  });

  test.each([
    ["обсуждение", () => snapshotWith({ phase: phaseOf("discussion") })],
    ["сбор ходов", () => snapshotWith({ phase: phaseOf("collecting") })],
    ["мастер думает", () => snapshotWith({ phase: phaseOf("resolving") })],
    [
      "мастер не справился",
      () => snapshotWith({ phase: phaseOf("dm_failed") }),
    ],
    [
      "пауза",
      () =>
        snapshotWith({
          phase: phaseOf("discussion", {
            phase: "paused",
            remaining_seconds: 40,
          }),
        }),
    ],
    [
      "игра окончена",
      () =>
        snapshotWith({
          game: gameSummary({ phase: "finished" }),
          phase: phaseOf(null, { phase: "finished" }),
        }),
    ],
    ["бой", () => combatSnapshot()],
    ["привал", () => campSnapshot()],
  ])(
    "%s: вкладка «Дневник» последней, дневник открывается",
    async (_name, make) => {
      await openGame({ snapshot: { ...make(), journal: startJournal() } });

      expect(tabNames().at(-1)).toBe("Дневник");
      fireEvent.click(tab("Дневник"));

      expect(section("Места")).toHaveTextContent("Места 2");
      expect(journal().getByRole("region", { name: "Главное" })).toBeVisible();
    },
  );

  test("привал без боя: пять вкладок, «Привал» сразу после «Рассказа»", async () => {
    await openGame({ snapshot: campSnapshot() });

    expect(tabNames()).toEqual([
      "Рассказ",
      "Привал",
      "Персонаж",
      "Отряд",
      "Дневник",
    ]);
  });

  test("дневник монтируется лениво и, открытый однажды, помнит выбранный раздел", async () => {
    await openGame({ snapshot: snapshotWith({ journal: startJournal() }) });
    const sections = () =>
      screen.queryByRole("tablist", { name: "Разделы дневника", hidden: true });

    // До первого открытия дневника нет даже в скрытой панели.
    expect(sections()).toBeNull();
    fireEvent.click(tab("Персонаж"));
    expect(sections()).toBeNull();

    fireEvent.click(tab("Дневник"));
    fireEvent.click(section("Места"));
    fireEvent.click(tab("Рассказ"));

    // Ушли на другую вкладку — дневник остался в скрытой панели.
    expect(sections()).not.toBeNull();
    expect(sections()).not.toBeVisible();
    fireEvent.click(tab("Дневник"));
    expect(section("Места")).toHaveAttribute("aria-selected", "true");
  });

  test("запись, пришедшая, пока игрок на другой вкладке, при возвращении помечена «новое»", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({ journal: startJournal() }),
    });
    fireEvent.click(tab("Дневник"));
    fireEvent.click(section("Места"));
    fireEvent.click(tab("Рассказ"));

    await serve(ws.last(), {
      type: "journal_entry",
      entry: journalEntry("location:crypt", {
        title: "Склеп",
        turn: 2,
        visited_turn: 2,
      }),
    });
    expect(
      JSON.parse(window.localStorage.getItem("dnd-journal-seen:g1:u1") ?? "{}"),
    ).not.toHaveProperty(["location:crypt@all"]);
    fireEvent.click(tab("Дневник"));

    const card = (name: string) =>
      within(journal().getByRole("region", { name }));
    expect(section("Места")).toHaveTextContent("Места 3");
    expect(card("Склеп").getByText("новое")).toBeVisible();
    expect(card("Деревня Мирра").queryByText("новое")).toBeNull();
  });

  test("личная запись героя на экране как обычная: из снимка и событием", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({
        journal: [
          ...startJournal(),
          journalEntry("location:village", {
            title: "Деревня Мирра",
            text: "Лира знает тайный ход под мельницей.",
            audience: "pc_lira",
            turn: 2,
          }),
        ],
      }),
    });
    fireEvent.click(tab("Дневник"));

    await serve(ws.last(), {
      type: "journal_entry",
      entry: journalEntry("npc:elder", {
        title: "Староста Хорн",
        text: "Шепнул Лире, где ключ.",
        audience: "pc_lira",
        turn: 3,
      }),
    });

    fireEvent.click(section("Места"));
    expect(section("Места")).toHaveTextContent("Места 3");
    expect(
      journal().getAllByRole("region", { name: "Деревня Мирра" }),
    ).toHaveLength(2);
    expect(
      journal().getByText("Лира знает тайный ход под мельницей."),
    ).toBeVisible();
    fireEvent.click(section("Люди"));
    const elder = within(
      journal().getByRole("region", { name: "Староста Хорн" }),
    );
    expect(elder.getByText("Шепнул Лире, где ключ.")).toBeVisible();
    expect(elder.getByText("узнали в раунде 3")).toBeVisible();
    expect(elder.getByText("новое")).toBeVisible();
  });
});

describe("«Поторопить» в бою", () => {
  const HURRY = "POST /api/games/g1/host/hurry";

  test("хост видит кнопку, пока ходит герой и срока нет", async () => {
    await openGame({ as: "u1", snapshot: combatSnapshot("pc_lira") });

    expect(button("Поторопить")).toBeInTheDocument();
  });

  test("кнопка доступна с любой вкладки и шлёт host/hurry", async () => {
    const { server } = await openGame({
      snapshot: combatSnapshot("pc_lira"),
      routes: { [HURRY]: { json: phaseOf("combat_wait") } },
    });
    fireEvent.click(tab("Отряд"));

    fireEvent.click(button("Поторопить"));

    await vi.waitFor(() =>
      expect(
        server.calls.some((call) => call.path === "/api/games/g1/host/hurry"),
      ).toBe(true),
    );
  });

  test("не хост кнопки не получает", async () => {
    await openGame({ as: "u2", snapshot: combatSnapshot("pc_lira") });

    noButton("Поторопить");
  });

  test("пошёл отсчёт (событие phase со сроком) — кнопка исчезает", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });

    await serve(ws.last(), {
      type: "phase",
      phase: phaseOf("combat_wait", {
        deadline_at: "2026-10-20T12:01:00+00:00",
      }),
    });

    noButton("Поторопить");
    expect(screen.getByText("Время на ход")).toBeInTheDocument();
  });

  test("ход перешёл к монстру — кнопка исчезает", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("pc_lira") });

    await serve(ws.last(), { type: "combat", combat: combatOf("skeleton-1") });

    noButton("Поторопить");
  });
});

describe("герой на очереди не может действовать (can_act: false)", () => {
  const RETRY = "POST /api/games/g1/host/retry";

  /** Лира (герой игрока), Борин и Скелет; ходит `current`, `canAct` — признаки героев. */
  const duel = (
    current: string,
    canAct: { pc_lira?: boolean; pc_boris?: boolean },
  ): CombatView =>
    combatWith(current, [
      heroFighter("pc_lira", "Лира", { can_act: canAct.pc_lira }),
      heroFighter("pc_boris", "Борин", { can_act: canAct.pc_boris }),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

  const open = (
    current: string,
    canAct: Parameters<typeof duel>[1],
    as?: "u1" | "u2",
  ) =>
    openGame({
      as,
      snapshot: combatSnapshot(current, { combat: duel(current, canAct) }),
    });

  test("мой герой: на «Бой» поля нет, пояснение вместо «Ходит Лира.»", async () => {
    await open("pc_lira", { pc_lira: false });

    fireEvent.click(tab("Бой"));

    expect(screen.queryByLabelText(FIELD)).toBeNull();
    const combat = within(panel("Бой"));
    expect(combat.getByText("Лира не может действовать.")).toBeVisible();
    expect(combat.queryByText("Ходит Лира.")).toBeNull();
  });

  test("мой герой: на «Рассказе» сказано то же, «Сейчас ваш ход» нет, «К бою» ведёт на бой", async () => {
    await open("pc_lira", { pc_lira: false });

    const story = within(panel("Рассказ"));
    expect(story.getByText("Лира не может действовать.")).toBeVisible();
    expect(story.queryByText(/Сейчас ваш ход/)).toBeNull();

    fireEvent.click(story.getByRole("button", { name: "К бою" }));

    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
  });

  test("чужой герой: то же пояснение с его именем, поля нет", async () => {
    await open("pc_boris", { pc_boris: false });

    expect(
      within(panel("Рассказ")).getByText("Борин не может действовать."),
    ).toBeVisible();

    fireEvent.click(tab("Бой"));

    expect(screen.queryByLabelText(FIELD)).toBeNull();
    expect(
      within(panel("Бой")).getByText("Борин не может действовать."),
    ).toBeVisible();
  });

  test("хост: «Поторопить» нет, «Повторить» есть и шлёт host/retry", async () => {
    const { server } = await openGame({
      as: "u1",
      snapshot: combatSnapshot("pc_lira", {
        combat: duel("pc_lira", { pc_lira: false }),
      }),
      routes: { [RETRY]: { json: phaseOf("combat_wait") } },
    });

    noButton("Поторопить");
    fireEvent.click(button("Повторить"));

    await vi.waitFor(() =>
      expect(
        server.calls.some((call) => call.path.endsWith("/host/retry")),
      ).toBe(true),
    );
  });

  test("хост и чужой недееспособный герой: то же", async () => {
    await open("pc_boris", { pc_boris: false }, "u1");

    noButton("Поторопить");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("не хост «Повторить» не видит", async () => {
    await open("pc_lira", { pc_lira: false }, "u2");

    noButton("Повторить");
    noButton("Поторопить");
  });

  test("герой может действовать (can_act: true) — прежнее поведение", async () => {
    await open("pc_lira", { pc_lira: true });

    expect(within(panel("Рассказ")).getByText(/Сейчас ваш ход/)).toBeVisible();
    expect(button("Поторопить")).toBeInTheDocument();
    noButton("Повторить");
    fireEvent.click(tab("Бой"));
    expect(screen.getByLabelText(FIELD)).toBeEnabled();
  });

  test("событие combat: героя парализовало — поле уходит, «Поторопить» меняется на «Повторить»; очередь прошла — всё возвращается", async () => {
    const { ws } = await open("pc_lira", { pc_lira: true });
    fireEvent.click(tab("Бой"));
    expect(screen.getByLabelText(FIELD)).toBeEnabled();

    await serve(ws.last(), {
      type: "combat",
      combat: duel("pc_lira", { pc_lira: false }),
    });

    expect(screen.queryByLabelText(FIELD)).toBeNull();
    expect(
      within(panel("Бой")).getByText("Лира не может действовать."),
    ).toBeVisible();
    noButton("Поторопить");
    expect(button("Повторить")).toBeInTheDocument();

    await serve(ws.last(), {
      type: "combat",
      combat: duel("pc_boris", { pc_lira: false, pc_boris: true }),
    });

    expect(within(panel("Бой")).getByText("Ходит Борин.")).toBeVisible();
    noButton("Повторить");
    expect(button("Поторопить")).toBeInTheDocument();
  });
});

describe("вкладка «Привал»", () => {
  test("вне привала вкладки нет", async () => {
    await openGame();

    expect(tabNames()).not.toContain("Привал");
  });

  test("привал в снимке: вкладка «Привал» после «Рассказа», с отсчётом в строке статуса", async () => {
    await openGame({ snapshot: campSnapshot() });

    expect(tabNames()).toEqual([
      "Рассказ",
      "Привал",
      "Персонаж",
      "Отряд",
      "Дневник",
    ]);
    expect(screen.getByText("До конца привала")).toBeInTheDocument();
  });

  test("событие phase начинает и кончает привал", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });
    expect(tabNames()).toContain("Привал");

    await serve(ws.last(), { type: "phase", phase: phaseOf("resolving") });
    expect(tabNames()).not.toContain("Привал");
  });

  test("тема мастера и реплики — на вкладке «Привал»", async () => {
    await openGame({ snapshot: campSnapshot() });

    fireEvent.click(tab("Привал"));

    const camp = within(panel("Привал"));
    expect(camp.getByText("Вы разбиваете лагерь у реки.")).toBeVisible();
    expect(camp.getByText(/Подкину дров/)).toBeVisible();
    expect(camp.getByLabelText("Сказать у костра")).toBeEnabled();
  });

  test("на «Рассказе» в привале поле хода не нужно: подсказка и «К костру»", async () => {
    await openGame({ snapshot: campSnapshot() });

    const story = within(panel("Рассказ"));
    expect(story.queryByLabelText(FIELD)).toBeNull();
    expect(screen.queryByLabelText(FIELD)).toBeNull();

    fireEvent.click(story.getByRole("button", { name: "К костру" }));

    expect(tab("Привал")).toHaveAttribute("aria-selected", "true");
  });

  test("реплика уходит на сервер, а в ленту попадает по событию", async () => {
    const { server, ws } = await openGame({
      snapshot: campSnapshot(),
      routes: {
        "POST /api/games/g1/camp/line": (call): FakeReply => ({
          json: feedEntry(10, {
            kind: "camp_line",
            text: (call.body as { text: string }).text,
            data: { actor_id: "pc_lira", actor: "Лира" },
          }),
        }),
      },
    });
    fireEvent.click(tab("Привал"));
    fireEvent.change(screen.getByLabelText("Сказать у костра"), {
      target: { value: "Греюсь у огня" },
    });

    fireEvent.click(button("Сказать"));

    await vi.waitFor(() =>
      expect(screen.getByLabelText("Сказать у костра")).toHaveValue(""),
    );
    const sent = server.calls.find(
      (call) => call.path === "/api/games/g1/camp/line",
    );
    expect(sent?.body).toEqual({ text: "Греюсь у огня" });
    // Запись одна и в списке привала, и в общей ленте «Рассказа»; ответ и событие её не удваивают.
    expect(within(panel("Привал")).getAllByText(/Греюсь у огня/)).toHaveLength(
      1,
    );
    expect(screen.getAllByText(/Греюсь у огня/)).toHaveLength(2);

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(11, {
        kind: "camp_line",
        text: "Кто-то слышит шорох?",
        data: { actor_id: "pc_boris", actor: "Борин" },
      }),
    });
    expect(
      within(panel("Привал")).getByText(/Кто-то слышит шорох/),
    ).toBeVisible();
  });

  test("предел сервера показан его текстом", async () => {
    const limit = "Вы уже сказали у костра достаточно: подождите утра.";
    await openGame({
      snapshot: campSnapshot(),
      routes: {
        "POST /api/games/g1/camp/line": {
          status: 409,
          json: { detail: limit },
        },
      },
    });
    fireEvent.click(tab("Привал"));
    fireEvent.change(screen.getByLabelText("Сказать у костра"), {
      target: { value: "Ещё слово" },
    });

    fireEvent.click(button("Сказать"));

    expect(await screen.findByRole("alert")).toHaveTextContent(limit);
  });

  test("привал кончился, пока игрок был на нём: открыт «Рассказ» с полем хода", async () => {
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Привал"));

    await serve(ws.last(), { type: "phase", phase: phaseOf("resolving") });
    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });

    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    expect(tabNames()).not.toContain("Привал");
    expect(screen.getByLabelText(FIELD)).toBeVisible();
  });

  test("хост: «+1 мин» и «Закончить привал»; запросы уходят", async () => {
    const { server } = await openGame({
      as: "u1",
      snapshot: campSnapshot(),
      routes: {
        "POST /api/games/g1/host/rest-extend": PHASE_REPLY,
        "POST /api/games/g1/host/rest-end": PHASE_REPLY,
      },
    });

    fireEvent.click(button("+1 мин"));
    await vi.waitFor(() =>
      expect(
        server.calls.some(
          (call) => call.path === "/api/games/g1/host/rest-extend",
        ),
      ).toBe(true),
    );
    await vi.waitFor(() => expect(button("Закончить привал")).toBeEnabled());
    fireEvent.click(button("Закончить привал"));

    await vi.waitFor(() =>
      expect(
        server.calls.some(
          (call) => call.path === "/api/games/g1/host/rest-end",
        ),
      ).toBe(true),
    );
  });

  test("не хост этих кнопок не получает, но сказать у костра может", async () => {
    await openGame({ as: "u2", snapshot: campSnapshot() });

    noButton("+1 мин");
    noButton("Закончить привал");
    fireEvent.click(tab("Привал"));
    expect(screen.getByLabelText("Сказать у костра")).toBeEnabled();
  });

  test.each([
    ["short", "Короткий привал"],
    ["long", "Долгий привал"],
    [null, "Привал"],
  ] as const)(
    "подсказка на «Рассказе» подписана по виду привала (%s): «%s»",
    async (kind, title) => {
      await openGame({
        snapshot: campSnapshot({
          rest:
            kind === null
              ? null
              : {
                  kind,
                  deadline_at: CAMP_DEADLINE,
                  my_ideas: [],
                  lines: [],
                },
        }),
      });

      const story = within(panel("Рассказ"));
      expect(story.getByRole("heading", { name: title })).toBeInTheDocument();
      expect(story.getByRole("button", { name: "К костру" })).toBeEnabled();
    },
  );

  test("идея для рассказа приходит кадром и видна на вкладке «Привал», а не в ленте", async () => {
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Привал"));

    await serve(ws.last(), {
      type: "rest_idea",
      text: "Расскажи, кто научил тебя разводить огонь.",
    });

    expect(
      within(panel("Привал")).getByText(
        "Расскажи, кто научил тебя разводить огонь.",
      ),
    ).toBeVisible();
    // Одна карточка у адресата: в ленту «Рассказа» идея не попадает.
    expect(screen.getAllByText(/кто научил тебя/)).toHaveLength(1);
  });

  // Финальное покрытие B9: кадры сокета в порядке сервера (B9.3) проводят экран через короткий
  // привал по кнопке и следующий за ним долгий привал хода мастера.
  test("кадры привалов в порядке сервера: короткий по кнопке, затем долгий хода мастера", async () => {
    const IDEA = "Расскажи, кто научил тебя разводить огонь.";
    const { ws } = await openGame();
    const socket = ws.last();
    const restCard = (title: string) =>
      within(panel("Привал")).getByRole("heading", { name: title });

    // Начало кнопкой хоста: фаза привала, затем вид и срок.
    await serve(socket, {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });
    await serve(socket, {
      type: "rest_started",
      kind: "short",
      deadline_at: CAMP_DEADLINE,
    });
    await serve(socket, { type: "rest_idea", text: IDEA });

    expect(
      within(panel("Рассказ")).getByRole("heading", {
        name: "Короткий привал",
      }),
    ).toBeInTheDocument();
    noButton(/^Короткий привал \(/);
    fireEvent.click(tab("Привал"));
    expect(restCard("Короткий привал")).toBeInTheDocument();
    expect(
      within(panel("Привал")).getByRole("button", {
        name: /^Потратить кость хитов/,
      }),
    ).toBeInTheDocument();
    expect(within(panel("Привал")).getByText(IDEA)).toBeVisible();

    // Конец: «Мастер думает», рассказ, обсуждение и последним rest_ended.
    await serve(socket, { type: "phase", phase: phaseOf("resolving") });
    expect(tabNames()).not.toContain("Привал");
    await serve(socket, {
      type: "feed_item",
      item: feedEntry(10, { kind: "narration", text: "Прошёл час." }),
    });
    await serve(socket, { type: "phase", phase: phaseOf("discussion") });
    await serve(socket, { type: "rest_ended", kind: "short" });

    expect(tabNames()).not.toContain("Привал");
    expect(button(/^Короткий привал \(/)).toBeEnabled();
    expect(screen.queryByText(IDEA)).toBeNull();

    // Долгий привал хода мастера (start_camp): фаза нового хода, затем rest_started.
    await serve(socket, { type: "phase", phase: phaseOf("resolving") });
    await serve(socket, {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });
    await serve(socket, {
      type: "rest_started",
      kind: "long",
      deadline_at: CAMP_DEADLINE,
    });

    fireEvent.click(tab("Привал"));
    expect(restCard("Долгий привал")).toBeInTheDocument();
    expect(
      within(panel("Привал")).queryByRole("button", { name: /кость хитов/ }),
    ).toBeNull();
    // Идея короткого привала в новый привал не перешла.
    expect(screen.queryByText(IDEA)).toBeNull();
    expect(
      within(panel("Привал")).queryByRole("heading", {
        name: "Идея для рассказа",
      }),
    ).toBeNull();
  });

  test("на паузе привала реплика закрыта", async () => {
    await openGame({
      snapshot: campSnapshot({
        phase: phaseOf("camp", { phase: "paused", remaining_seconds: 120 }),
      }),
    });
    fireEvent.click(tab("Привал"));

    expect(screen.getByLabelText("Сказать у костра")).toBeDisabled();
  });
});

describe("вкладки бой и привал: безопасность текстов", () => {
  test("имена и реплики с разметкой остаются текстом", async () => {
    const combat: CombatView = combatWith("skeleton-1", [
      heroFighter("pc_lira", "Лира"),
      monsterFighter("skeleton-1", "<img src=x onerror=alert(1)>"),
    ]);
    const { container } = await openGame({
      snapshot: combatSnapshot("skeleton-1", { combat }),
    });
    fireEvent.click(tab("Бой"));

    expect(container.querySelector("img")).toBeNull();
  });

  test("поле хода открыто владельцу того героя, который сейчас ходит", async () => {
    const boris = sheetHero({ id: "pc_boris", name: "Борин" });
    const combat = combatWith("pc_boris", [
      heroFighter("pc_lira", "Лира"),
      heroFighter("pc_boris", "Борин"),
    ]);
    await openGame({
      as: "u2",
      snapshot: combatSnapshot("pc_boris", { combat, character: boris }),
    });

    fireEvent.click(tab("Бой"));

    expect(screen.getByLabelText(FIELD)).toBeEnabled();
  });
});

describe("бой: очередь моего героя, но ходы сейчас не принимаются", () => {
  // Герой сдал ход, и мастер разбирает бой: очередь всё ещё его, но «Сейчас ваш ход» было бы
  // неправдой. Карточка на «Рассказе» называет причину закрытого поля, а не приглашает ходить.
  test.each([
    [
      "мастер думает",
      phaseOf("resolving"),
      "Мастер думает: ходы сейчас не принимаются.",
    ],
    [
      "игра на паузе",
      phaseOf("combat_wait", { phase: "paused", remaining_seconds: 30 }),
      "Игра на паузе: хост должен нажать «Продолжить».",
    ],
  ])(
    "%s: на «Рассказе» причина вместо «Сейчас ваш ход»",
    async (_name, phase, note) => {
      await openGame({ snapshot: combatSnapshot("pc_lira", { phase }) });

      const story = within(panel("Рассказ"));
      expect(story.getByText(note)).toBeVisible();
      expect(story.queryByText(/Сейчас ваш ход/)).toBeNull();
    },
  );
});

// --- нижняя панель: бейджи, уведомления, «Мастер думает…» (B10.2) ---

/** Отметка нового у вкладки: она стоит рядом с кнопкой вкладки и скрыта от читалки. */
const badgeOf = (name: string): string | null => {
  const cell = tab(name).parentElement;
  const badge = cell?.querySelector("[aria-hidden='true'].ui-tabbar__badge");
  return badge?.textContent ?? null;
};

const toastRegion = () =>
  document.querySelector<HTMLElement>(".ui-toast-region");

describe("бейджи вкладок", () => {
  test("при входе в игру отметок нет: снимок — не событие", async () => {
    await openGame({ snapshot: snapshotWith({ journal: startJournal() }) });

    for (const name of tabNames()) expect(badgeOf(String(name))).toBeNull();
  });

  test("запись ленты, пока игрок на другой вкладке: отметка у «Рассказа», при открытии гаснет", async () => {
    const { ws } = await openGame();
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), { type: "feed_item", item: feedEntry(3) });
    await serve(ws.last(), { type: "feed_item", item: feedEntry(4) });

    expect(badgeOf("Рассказ")).toBe("2");
    expect(tab("Рассказ")).toHaveAccessibleName("Рассказ");
    expect(tab("Рассказ")).toHaveAccessibleDescription("есть новое");

    fireEvent.click(tab("Рассказ"));

    expect(badgeOf("Рассказ")).toBeNull();
    expect(tab("Рассказ")).not.toHaveAccessibleDescription("есть новое");
  });

  test("на открытой вкладке отметки нет: запись пришла, пока игрок читает рассказ", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), { type: "feed_item", item: feedEntry(3) });

    expect(badgeOf("Рассказ")).toBeNull();
  });

  test("запись дневника — отметка у «Дневника»; дополнение прежней записи тоже считается", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({ journal: startJournal() }),
    });

    await serve(ws.last(), {
      type: "journal_entry",
      entry: journalEntry("location:crypt", { title: "Склеп", turn: 2 }),
    });
    const [first] = startJournal();
    await serve(ws.last(), {
      type: "journal_entry",
      entry: { ...first, revision: first.revision + 1 },
    });

    expect(badgeOf("Дневник")).toBe("2");
    fireEvent.click(tab("Дневник"));
    expect(badgeOf("Дневник")).toBeNull();
  });

  test("тайное письмо: отметка — только число, текста нет ни у вкладки, ни в уведомлении", async () => {
    const { ws } = await openGame();
    fireEvent.click(tab("Персонаж"));

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(3, {
        kind: "private",
        private: true,
        text: "Ключ спрятан под третьей плитой.",
      }),
    });

    expect(badgeOf("Рассказ")).toBe("1");
    const bar = screen.getByRole("tablist", { name: "Разделы игры" });
    expect(bar.parentElement).not.toHaveTextContent(/Ключ спрятан/);
    expect(toastRegion()).not.toHaveTextContent(/Ключ спрятан/);
  });

  test("реплики у костра не считаются новым в рассказе: они на вкладке «Привал»", async () => {
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(9, {
        kind: "camp_line",
        text: "Спою песню.",
        data: { actor_id: "pc_boris", actor: "Борин" },
      }),
    });

    expect(badgeOf("Рассказ")).toBeNull();
  });

  test("мой ход в бою, пока игрок не на «Бою»: отметка «!» у «Боя», открыл — погасла", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(badgeOf("Бой")).toBe("!");
    expect(tab("Бой")).toHaveAccessibleName("Бой");
    fireEvent.click(tab("Бой"));
    expect(badgeOf("Бой")).toBeNull();
  });

  test("бой кончился — отметка боя не переходит в следующий бой", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    await serve(ws.last(), { type: "combat", combat: null });
    await serve(ws.last(), { type: "combat", combat: combatOf("skeleton-1") });

    expect(badgeOf("Бой")).toBeNull();
  });
});

describe("уведомления и вибрация", () => {
  test("«Твой ход» в бою, пока игрок читает рассказ: тост с «К бою» и вибрация", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    expect(buzz).not.toHaveBeenCalled();

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    const toast = screen.getByRole("status", { name: "Твой ход" });
    expect(toast).toHaveTextContent("Бой идёт: открой вкладку «Бой».");
    expect(buzz).toHaveBeenCalledWith("your-turn");
    expect(buzz).toHaveBeenCalledTimes(1);

    fireEvent.click(within(toast).getByRole("button", { name: "К бою" }));

    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
    expect(toastRegion()).toBeEmptyDOMElement();
  });

  test("игрок уже на «Бою»: тоста нет, вибрация есть", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    fireEvent.click(tab("Бой"));

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(buzz).toHaveBeenCalledWith("your-turn");
  });

  test("тост «Твой ход» гаснет, если игрок сам открыл «Бой»", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    fireEvent.click(tab("Бой"));

    expect(toastRegion()).toBeEmptyDOMElement();
  });

  test("тост уходит сам через несколько секунд", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    expect(toastRegion()).toHaveTextContent("Твой ход");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });

    expect(toastRegion()).toBeEmptyDOMElement();
  });

  test("ход сдан и мастер думает — «Твой ход» не повторяется на каждом кадре боя", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(buzz).toHaveBeenCalledTimes(1);
  });

  test("«Хост собирает ходы»: игроку на другой вкладке — тост и вибрация", async () => {
    const { ws } = await openGame({ as: "u2" });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });

    expect(
      screen.getByRole("status", { name: "Хост собирает ходы" }),
    ).toBeInTheDocument();
    expect(buzz).toHaveBeenCalledWith("collecting");
  });

  test("«Хост собирает ходы»: на «Рассказе» поле хода и отсчёт и так видны — только вибрация", async () => {
    const { ws } = await openGame({ as: "u2" });

    await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(buzz).toHaveBeenCalledWith("collecting");
  });

  test.each(["submitted", "auto_submitted", "pass", "auto_pass"] as const)(
    "«Хост собирает ходы», а ход уже сдан (%s): ни тоста, ни вибрации — звать не к чему",
    async (status) => {
      const { ws } = await openGame({
        as: "u2",
        snapshot: snapshotWith({ action: myAction({ status, text: "Жду." }) }),
      });
      fireEvent.click(tab("Отряд"));

      await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });

      expect(toastRegion()).toBeEmptyDOMElement();
      expect(buzz).not.toHaveBeenCalled();
    },
  );

  test("«Хост собирает ходы», а ход ещё черновик: тост и вибрация", async () => {
    const { ws } = await openGame({
      as: "u2",
      snapshot: snapshotWith({
        action: myAction({ status: "draft", text: "Думаю…" }),
      }),
    });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });

    expect(
      screen.getByRole("status", { name: "Хост собирает ходы" }),
    ).toBeInTheDocument();
    expect(buzz).toHaveBeenCalledWith("collecting");
  });

  test("мой ход в бою прошёл без открытия «Боя» (время вышло): «!» и тост «Твой ход» гаснут", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    expect(badgeOf("Бой")).toBe("!");
    expect(toastRegion()).toHaveTextContent("Твой ход");

    await serve(ws.last(), { type: "combat", combat: combatOf("skeleton-1") });

    expect(badgeOf("Бой")).toBeNull();
    expect(toastRegion()).toBeEmptyDOMElement();
  });

  test("после пропущенного хода следующий мой ход снова зажигает «!» и зовёт тостом", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    await serve(ws.last(), { type: "combat", combat: combatOf("skeleton-1") });

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(badgeOf("Бой")).toBe("!");
    expect(toastRegion()).toHaveTextContent("Твой ход");
    expect(buzz).toHaveBeenCalledTimes(2);
  });

  test("мой ход кончился тем же кадром, что начался привал: виден тост «Привал», а не «Твой ход»", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    await serve(ws.last(), {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });

    expect(toastRegion()).toHaveTextContent("Привал");
    expect(toastRegion()).not.toHaveTextContent("Твой ход");
    expect(badgeOf("Бой")).toBeNull();
  });

  test("хост сам нажал «Собрать ходы»: ни тоста, ни вибрации", async () => {
    const { ws } = await openGame({ as: "u1" });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), { type: "phase", phase: phaseOf("collecting") });

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(buzz).not.toHaveBeenCalled();
  });

  test("«Привал»: тост, вибрация и отметка у новой вкладки", async () => {
    const { ws } = await openGame();

    await serve(ws.last(), {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });

    expect(screen.getByRole("status", { name: "Привал" })).toHaveTextContent(
      "«Привал»",
    );
    expect(buzz).toHaveBeenCalledWith("rest");
    expect(badgeOf("Привал")).toBe("1");
  });

  test("уведомление не пересказывает тайный ход игрока", async () => {
    const { ws } = await openGame({
      snapshot: combatSnapshot("skeleton-1", {
        action: myAction({
          status: "submitted",
          secret: true,
          text: "Тайно режу верёвку моста.",
        }),
      }),
    });

    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    expect(toastRegion()).toHaveTextContent("Твой ход");
    expect(toastRegion()).not.toHaveTextContent(/верёвку/);
  });
});

describe("«Мастер думает…» на «Рассказе»", () => {
  test("одна формулировка, нейтральная фраза и сколько уже ждём", async () => {
    await openGame({ snapshot: snapshotWith({ phase: phaseOf("resolving") }) });

    const card = within(panel("Рассказ")).getByRole("status", {
      name: "Мастер думает…",
    });
    expect(card).toHaveTextContent(/Ждём 0:00/);
    expect(card.textContent).toMatch(/…/);
  });

  test("время ожидания идёт, фраза меняется по времени", async () => {
    vi.useFakeTimers({ toFake: [...FAKE_TIMERS, "Date"] });
    await openGame({ snapshot: snapshotWith({ phase: phaseOf("resolving") }) });
    const card = () => screen.getByRole("status", { name: "Мастер думает…" });
    const first = card().textContent;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(card()).toHaveTextContent("Ждём 0:09");
    expect(card().textContent).not.toBe(first);
  });

  test("мастер ответил — карточки нет", async () => {
    const { ws } = await openGame({
      snapshot: snapshotWith({ phase: phaseOf("resolving") }),
    });

    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });

    expect(screen.queryByRole("status", { name: "Мастер думает…" })).toBeNull();
  });

  test("в обсуждении карточки нет", async () => {
    await openGame();

    expect(screen.queryByRole("status", { name: "Мастер думает…" })).toBeNull();
  });
});

describe("клавиатура телефона прячет нижнюю панель", () => {
  function stubTouchScreen() {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        media: query,
        matches: query === "(pointer: coarse)",
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    );
  }

  test("поле хода в фокусе на сенсорном экране — панели не видно, ушёл фокус — вернулась", async () => {
    stubTouchScreen();
    await openGame();
    const field = screen.getByLabelText(FIELD);

    act(() => field.focus());
    expect(
      screen.getByRole("tablist", { name: "Разделы игры", hidden: true }),
    ).not.toBeVisible();

    act(() => field.blur());
    expect(screen.getByRole("tablist", { name: "Разделы игры" })).toBeVisible();
  });

  // B10.3.fix2 (M1): панель возвращается после касания, а не посреди него — иначе кнопка поля
  // хода уезжает из-под пальца и первый тап теряется.
  test("тап по «Готов», пока пишешь: панель не появляется посреди касания, ход уходит с первого тапа", async () => {
    stubTouchScreen();
    const { server } = await openGame();
    const field = screen.getByLabelText(FIELD);
    act(() => field.focus());
    fireEvent.change(field, { target: { value: "Осматриваю дверь" } });
    const ready = button("Готов");

    fireEvent.pointerDown(ready);
    act(() => ready.focus());
    expect(
      screen.getByRole("tablist", { name: "Разделы игры", hidden: true }),
    ).not.toBeVisible();
    fireEvent.click(ready);

    expect(await screen.findByText("Ход сдан")).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Разделы игры" })).toBeVisible();
    expect(
      server.calls.filter((call) => `${call.method} ${call.path}` === SUBMIT),
    ).toHaveLength(1);
  });
});

// B10.3.fix2 (N2): после кнопок перехода (тост, подсказка на «Рассказе») фокус не падает на
// страницу: нажатая кнопка исчезает вместе с тостом или скрытой панелью.
describe("фокус после кнопок перехода", () => {
  test("«К бою» в подсказке на «Рассказе» — фокус на вкладке «Бой»", async () => {
    await openGame({ snapshot: combatSnapshot("skeleton-1") });

    fireEvent.click(
      within(screen.getByRole("region", { name: "Идёт бой" })).getByRole(
        "button",
        { name: "К бою" },
      ),
    );

    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
    expect(tab("Бой")).toHaveFocus();
  });

  test("«К бою» — фокус на вкладке «Бой»", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    const toast = screen.getByRole("status", { name: "Твой ход" });

    fireEvent.click(within(toast).getByRole("button", { name: "К бою" }));

    expect(tab("Бой")).toHaveFocus();
  });

  test("«Скрыть» — фокус на открытой вкладке", async () => {
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });
    const toast = screen.getByRole("status", { name: "Твой ход" });

    fireEvent.click(within(toast).getByRole("button", { name: "Скрыть" }));

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(tab("Рассказ")).toHaveFocus();
  });

  test("тост ушёл сам — фокус никуда не прыгает", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(7000);
    });

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(tab("Рассказ")).not.toHaveFocus();
  });
});

// --- финальное покрытие блока B10 (B10.coverage): пределы отметок, привал, вход и переподключение,
// тайное в панели и тосте, переход к листу при «меньше движения» ---

describe("отметки нового: пределы, привал и исчезнувшая вкладка", () => {
  test("до девяти новых — число, с десятой — «9+»; имя вкладки остаётся словом", async () => {
    const { ws } = await openGame();
    fireEvent.click(tab("Отряд"));

    for (let id = 3; id <= 11; id++) {
      await serve(ws.last(), { type: "feed_item", item: feedEntry(id) });
    }
    expect(badgeOf("Рассказ")).toBe("9");

    await serve(ws.last(), { type: "feed_item", item: feedEntry(12) });
    expect(badgeOf("Рассказ")).toBe("9+");
    expect(tab("Рассказ")).toHaveAccessibleName("Рассказ");
  });

  test("на привале новые реплики у костра и свои идеи — отметка у «Привала», а не у «Рассказа»", async () => {
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(9, {
        kind: "camp_line",
        text: "Спою песню.",
        data: { actor_id: "pc_boris", actor: "Борин" },
      }),
    });
    await serve(ws.last(), { type: "rest_idea", text: "Вспомни дом." });

    expect(badgeOf("Привал")).toBe("2");
    expect(badgeOf("Рассказ")).toBeNull();
    fireEvent.click(tab("Привал"));
    expect(badgeOf("Привал")).toBeNull();
  });

  test("привал кончился — его отметка не переходит в следующий: там только начало нового привала", async () => {
    const { ws } = await openGame({ snapshot: campSnapshot() });
    fireEvent.click(tab("Отряд"));
    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(9, {
        kind: "camp_line",
        text: "Спою песню.",
        data: { actor_id: "pc_boris", actor: "Борин" },
      }),
    });
    expect(badgeOf("Привал")).toBe("1");

    await serve(ws.last(), { type: "phase", phase: phaseOf("discussion") });
    expect(tabNames()).not.toContain("Привал");
    await serve(ws.last(), {
      type: "phase",
      phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    });

    expect(badgeOf("Привал")).toBe("1");
  });
});

describe("когда уведомлений нет: вход и переподключение", () => {
  test("вход в игру, когда мой ход уже идёт: ни тоста, ни вибрации, ни отметки — снимок не событие", async () => {
    await openGame({ snapshot: combatSnapshot("pc_lira") });

    expect(toastRegion()).toBeEmptyDOMElement();
    expect(buzz).not.toHaveBeenCalled();
    expect(badgeOf("Бой")).toBeNull();
  });

  test("переподключение с тем же снимком: ни отметок, ни тоста, ни вибрации", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { ws } = await openGame({ snapshot: combatSnapshot("skeleton-1") });
    fireEvent.click(tab("Отряд"));

    act(() => ws.last().drop(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await connect(ws.last(), combatSnapshot("skeleton-1"));

    for (const name of tabNames()) expect(badgeOf(String(name))).toBeNull();
    expect(toastRegion()).toBeEmptyDOMElement();
    expect(buzz).not.toHaveBeenCalled();
  });
});

/** Имена вкладок игры: ровно эти слова, без чисел и текстов событий. */
const TABS_WORDS = ["Рассказ", "Бой", "Привал", "Персонаж", "Отряд", "Дневник"];

describe("тайное не выходит в панель вкладок и уведомления", () => {
  const SECRETS = [
    "Ключ спрятан под третьей плитой",
    "Тайно режу верёвку моста",
    "Шёпот жреца у алтаря",
  ];

  /** Тексты и все атрибуты узла и его потомков: в них не должно быть тайного. */
  const everything = (node: Element | null): string =>
    node === null
      ? ""
      : [node, ...node.querySelectorAll("*")]
          .flatMap((element) => [
            element.textContent ?? "",
            ...element
              .getAttributeNames()
              .map((name) => element.getAttribute(name) ?? ""),
          ])
          .join("\n");

  test("личное письмо, тайный бросок и свой тайный ход: у вкладок только число или «!» и «есть новое»", async () => {
    const { ws } = await openGame({
      snapshot: combatSnapshot("skeleton-1", {
        action: myAction({
          status: "submitted",
          secret: true,
          text: SECRETS[1],
        }),
      }),
    });
    fireEvent.click(tab("Отряд"));

    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(3, { kind: "private", private: true, text: SECRETS[0] }),
    });
    await serve(ws.last(), {
      type: "feed_item",
      item: rollEntry(4, { reason: SECRETS[2] }, { private: true }),
    });
    await serve(ws.last(), {
      type: "feed_item",
      item: feedEntry(5, {
        kind: "action",
        private: true,
        text: SECRETS[1],
        data: { actor_id: "pc_lira", actor: "Лира", secret: true },
      }),
    });
    await serve(ws.last(), { type: "combat", combat: combatOf("pc_lira") });

    const bar = screen.getByRole("tablist", { name: "Разделы игры" });
    const region = toastRegion();
    expect(region).toHaveTextContent("Твой ход");
    for (const node of within(bar).getAllByRole("tab")) {
      expect(TABS_WORDS).toContain(node.textContent);
      expect(node).toHaveAccessibleName(node.textContent ?? "");
      // Описание — только «есть новое» (у вкладок с отметкой) или никакого.
      expect(node).toHaveAccessibleDescription(/^(есть новое)?$/);
    }
    for (const name of tabNames()) {
      const badge = badgeOf(String(name));
      if (badge !== null) expect(badge).toMatch(/^(\d|9\+|!)$/);
    }
    // Отметки есть: три новые записи ленты и мой ход — проверка выше не пустая.
    expect(badgeOf("Рассказ")).toBe("3");
    expect(badgeOf("Бой")).toBe("!");
    const shown = `${everything(bar.parentElement)}\n${everything(region)}`;
    for (const secret of SECRETS) expect(shown).not.toContain(secret);
    expect(
      screen.getByRole("status", { name: "Твой ход" }),
    ).toBeInTheDocument();
  });
});

describe("имя героя → лист: прокрутка и записи без героя", () => {
  /** «Меньше движения» в системе: только этот медиазапрос срабатывает. */
  function stubReducedMotion(reduce: boolean) {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        media: query,
        matches: query === "(prefers-reduced-motion: reduce)" && reduce,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    );
  }

  /** В jsdom прокрутки нет: подменяем её и запоминаем, что и как выводилось в видимость. */
  function spyScroll() {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    onTestFinished(() => {
      Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    });
    return scrollIntoView;
  }

  const withActions = () =>
    snapshotWith({
      feed: [
        feedEntry(1, {
          kind: "action",
          text: "осматривает дверь",
          data: { actor_id: "pc_lira", actor: "Лира", secret: false },
        }),
        feedEntry(2, {
          kind: "action",
          text: "держит факел",
          data: { actor_id: "pc_boris", actor: "Борин", secret: false },
        }),
      ],
    });
  const heroName = (name: string) =>
    within(screen.getByRole("list", { name: "Лента" })).getByRole("button", {
      name,
    });

  test.each([
    [true, "auto"],
    [false, "smooth"],
  ] as const)(
    "«меньше движения»: %s — свой лист к началу панели «Персонаж», прокрутка %s",
    async (reduce, behavior) => {
      stubReducedMotion(reduce);
      const scrollIntoView = spyScroll();
      await openGame({ snapshot: withActions() });
      scrollIntoView.mockClear();

      fireEvent.click(heroName("Лира"));

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts[0]).toBe(panel("Персонаж"));
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior });
    },
  );

  test("«меньше движения»: карточка чужого героя выводится в центр без плавной прокрутки", async () => {
    stubReducedMotion(true);
    const scrollIntoView = spyScroll();
    await openGame({ snapshot: withActions() });
    scrollIntoView.mockClear();

    fireEvent.click(heroName("Борин"));

    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "center",
      behavior: "auto",
    });
  });

  test("ход без actor_id (старая запись) — имя просто текст, кнопки нет", async () => {
    await openGame({
      snapshot: snapshotWith({
        feed: [
          feedEntry(1, {
            kind: "action",
            text: "ждёт у двери",
            data: { actor: "Лира" },
          }),
        ],
      }),
    });

    const feed = screen.getByRole("list", { name: "Лента" });
    expect(within(feed).queryByRole("button")).toBeNull();
    expect(within(feed).getByText("Лира:")).toBeInTheDocument();
  });
});
