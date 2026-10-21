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
  monsterFighter,
  myAction,
  phaseOf,
  rollEntry,
  sheetHero,
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
import { Game } from "./Game";

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
  test("три вкладки; открыта «Рассказ»", async () => {
    await openGame();

    expect(screen.getAllByRole("tab").map((node) => node.textContent)).toEqual([
      "Рассказ",
      "Персонаж",
      "Отряд",
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
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowLeft" });
    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
  });

  test("выбранная вкладка в порядке табуляции, остальные нет; она же подсвечена", async () => {
    await openGame();

    expect(tab("Рассказ")).toHaveAttribute("tabindex", "0");
    expect(tab("Рассказ")).toHaveClass("game-tab--active");
    for (const other of ["Персонаж", "Отряд"]) {
      expect(tab(other)).toHaveAttribute("tabindex", "-1");
      expect(tab(other)).not.toHaveClass("game-tab--active");
    }

    fireEvent.click(tab("Отряд"));

    expect(tab("Отряд")).toHaveAttribute("tabindex", "0");
    expect(tab("Отряд")).toHaveClass("game-tab--active");
    expect(tab("Рассказ")).toHaveAttribute("tabindex", "-1");
    expect(tab("Рассказ")).not.toHaveClass("game-tab--active");
  });

  test.each(["Enter", "a", "ArrowDown", "Home", "Tab"])(
    "клавиша «%s» вкладки не переключает: только стрелки вправо и влево",
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
    expect(screen.getByText("Мастер думает…")).toBeVisible();

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

    expect(screen.getByText(label)).toBeInTheDocument();
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

/** Привал в снимке: тема мастера и две реплики у костра. */
function campSnapshot(overrides: Partial<Snapshot> = {}) {
  const feed: FeedItem[] = [
    feedEntry(1, { kind: "narration", text: "Вы разбиваете лагерь у реки." }),
    feedEntry(2, {
      kind: "camp_line",
      text: "Подкину дров.",
      data: { actor_id: "pc_boris", actor: "Борин" },
    }),
  ];
  return snapshotWith({
    phase: phaseOf("camp", { deadline_at: CAMP_DEADLINE }),
    feed,
    ...overrides,
  });
}

const PHASE_REPLY: FakeReply = { json: phaseOf("camp") };

describe("вкладка «Бой»", () => {
  test("без боя вкладки нет", async () => {
    await openGame();

    expect(tabNames()).toEqual(["Рассказ", "Персонаж", "Отряд"]);
  });

  test("бой в снимке: вкладка «Бой» второй, после «Рассказа»", async () => {
    await openGame({ snapshot: combatSnapshot() });

    expect(tabNames()).toEqual(["Рассказ", "Бой", "Персонаж", "Отряд"]);
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

    expect(tabNames()).toEqual(["Рассказ", "Персонаж", "Отряд"]);
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

  test("стрелки обходят четыре вкладки по кругу", async () => {
    await openGame({ snapshot: combatSnapshot() });

    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowRight" });
    expect(tab("Бой")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Бой"), { key: "ArrowRight" });
    expect(tab("Персонаж")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Персонаж"), { key: "ArrowRight" });
    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Отряд"), { key: "ArrowRight" });
    expect(tab("Рассказ")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tab("Рассказ"), { key: "ArrowLeft" });
    expect(tab("Отряд")).toHaveAttribute("aria-selected", "true");
  });

  test("при четырёх вкладках полоса обычная", async () => {
    await openGame({ snapshot: combatSnapshot() });
    expect(screen.getByRole("tablist")).not.toHaveClass("game-tabs--dense");
  });

  test("при пяти вкладках полоса вкладок плотная, чтобы названия не слипались", async () => {
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
    ]);
    expect(screen.getByRole("tablist")).toHaveClass("game-tabs--dense");
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

    expect(tabNames()).toEqual(["Рассказ", "Привал", "Персонаж", "Отряд"]);
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
        "POST /api/games/g1/host/camp-extend": PHASE_REPLY,
        "POST /api/games/g1/host/camp-end": PHASE_REPLY,
      },
    });

    fireEvent.click(button("+1 мин"));
    await vi.waitFor(() =>
      expect(
        server.calls.some(
          (call) => call.path === "/api/games/g1/host/camp-extend",
        ),
      ).toBe(true),
    );
    await vi.waitFor(() => expect(button("Закончить привал")).toBeEnabled());
    fireEvent.click(button("Закончить привал"));

    await vi.waitFor(() =>
      expect(
        server.calls.some(
          (call) => call.path === "/api/games/g1/host/camp-end",
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
