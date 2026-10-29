import { act, fireEvent, render, screen } from "@testing-library/react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type {
  CombatView,
  GamePhase,
  GameSummary,
  RestView,
  TurnPhase,
} from "../api/types";
import { holdRequests } from "../screens/character/fixtures";
import { NO_LIVE, sessionToken, useGame } from "../store/game";
import type { GameView } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import {
  combatOf,
  combatWith,
  gameView,
  heroFighter,
  monsterFighter,
  phaseOf,
} from "./fixtures";
import { HostPanel } from "./HostPanel";

// Подменяются только таймеры: React и промисы работают как обычно.
const FAKE_TIMERS: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval"];

const HOST = "u1";

const PHASE_REPLY: FakeReply = {
  json: {
    phase: "playing",
    turn_phase: "collecting",
    deadline_at: null,
    remaining_seconds: null,
    scene: null,
  },
};

const HOST_ROUTES = Object.fromEntries(
  [
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
    "rest",
    "rest-extend",
    "rest-end",
  ].map((action) => [`POST /api/games/g1/host/${action}`, PHASE_REPLY]),
);

interface Setup {
  turnPhase?: TurnPhase | null;
  phase?: GamePhase;
  combat?: CombatView | null;
  /** Срок идущего отсчёта; по умолчанию его нет. */
  deadline?: string | null;
  /** Привал из снимка (контракт B9, §3); по умолчанию его нет. */
  rest?: RestView | null;
  /** Поправки к сводке игры (сроки привалов и др.). */
  game?: Partial<GameSummary>;
  /**
   * Признак сервера «можно перегенерировать» (контракт B11, §1 п. 11). По умолчанию — как решает
   * сервер: пока игроки обсуждают следующий ход (обсуждение, ход в бою, привал мастера).
   */
  canRegenerate?: boolean;
  /** Серверное начало фазы хода (`phase.phase_since`); по умолчанию его нет. */
  phaseSince?: string | null;
  user?: string;
  routes?: Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;
  hold?: string;
}

const REGENERATES_IN: ReadonlySet<TurnPhase | null> = new Set([
  "discussion",
  "combat_wait",
  "camp",
]);

function viewFor({
  turnPhase = "discussion",
  phase = "playing",
  combat = null,
  deadline = null,
  rest = null,
  game = {},
  canRegenerate = phase === "playing" && REGENERATES_IN.has(turnPhase),
  phaseSince = null,
}: Setup): GameView {
  const base = gameView();
  return {
    ...base,
    game: { ...base.game, ...game },
    phase: phaseOf(turnPhase, {
      phase,
      deadline_at: deadline,
      can_regenerate: canRegenerate,
      phase_since: phaseSince,
    }),
    combat,
    rest,
  };
}

// Срок привала: на привале сервер всегда ставит отсчёт.
const CAMP_DEADLINE = "2026-10-20T12:05:00+00:00";

function renderPanel(setup: Setup = {}) {
  const { user = HOST, routes, hold } = setup;
  signInAs({ id: user, name: "Игрок" });
  const server = fakeServer({ ...HOST_ROUTES, ...routes });
  const gated = holdRequests(server.fetch);
  const release = hold === undefined ? () => {} : gated.hold(hold);
  const api = createApi(gated.fetch, sessionToken);
  const tree = (view: GameView) => (
    <ApiContext value={api}>
      <HostPanel gameId="g1" view={view} userId={user} />
    </ApiContext>
  );
  const result = render(tree(viewFor(setup)));
  return {
    server,
    release,
    container: result.container,
    unmount: result.unmount,
    show: (next: Setup) => result.rerender(tree(viewFor(next))),
  };
}

const buttons = () =>
  screen.queryAllByRole("button").map((node) => node.textContent);
const button = (name: string) => screen.getByRole("button", { name });
const noButton = (name: string | RegExp) =>
  expect(screen.queryByRole("button", { name })).toBeNull();

describe("кнопки по фазам", () => {
  test("обсуждение: «Собрать ходы», два привала, «Пауза», «Перегенерировать ответ мастера»", () => {
    renderPanel({ turnPhase: "discussion" });

    expect(buttons()).toEqual([
      "Собрать ходы",
      "Короткий привал (2 мин)",
      "Долгий привал (5 мин)",
      "Пауза",
      "Перегенерировать ответ мастера",
    ]);
  });

  test("сбор ходов: «+1 мин», «Закрыть сейчас», «Пауза»", () => {
    renderPanel({ turnPhase: "collecting" });

    expect(buttons()).toEqual(["+1 мин", "Закрыть сейчас", "Пауза"]);
  });

  test("мастер думает: «Пауза» есть, но неактивна — сервер её не примет", () => {
    renderPanel({ turnPhase: "resolving" });

    expect(buttons()).toEqual(["Пауза"]);
    expect(button("Пауза")).toBeDisabled();
    expect(
      screen.getByText(/паузу можно поставить, когда он закончит/),
    ).toBeInTheDocument();
  });

  test("мастер не справился: «Повторить» сразу, «Пауза» доступна", () => {
    renderPanel({ turnPhase: "dm_failed" });

    expect(buttons()).toEqual(["Повторить", "Пауза"]);
    expect(button("Пауза")).toBeEnabled();
  });

  test("привал: «+1 мин», «Закончить привал», «Пауза», «Перегенерировать»", () => {
    renderPanel({ turnPhase: "camp", deadline: CAMP_DEADLINE });

    expect(buttons()).toEqual([
      "+1 мин",
      "Закончить привал",
      "Пауза",
      "Перегенерировать ответ мастера",
    ]);
  });

  test("бой, ход героя без срока: «Поторопить», «Пауза», «Перегенерировать»; «Повторить» не нужна", () => {
    renderPanel({ turnPhase: "combat_wait", combat: combatOf("pc_lira") });

    expect(buttons()).toEqual([
      "Поторопить",
      "Пауза",
      "Перегенерировать ответ мастера",
    ]);
  });

  test("бой, ходит монстр: «Повторить» показана сразу (цепочка ходов мастера могла встать)", () => {
    renderPanel({ turnPhase: "combat_wait", combat: combatOf("skeleton-1") });

    expect(buttons()).toContain("Повторить");
  });

  test("бой, ход спрятанного монстра (current_id пуст): «Повторить» тоже", () => {
    renderPanel({ turnPhase: "combat_wait", combat: combatOf(null) });

    expect(buttons()).toContain("Повторить");
  });

  test("бой, боя в данных нет: «Повторить» не предлагается", () => {
    renderPanel({ turnPhase: "combat_wait", combat: null });

    noButton("Повторить");
  });

  test("пауза: одна кнопка «Продолжить»", () => {
    renderPanel({ turnPhase: "collecting", phase: "paused" });

    expect(buttons()).toEqual(["Продолжить"]);
  });

  test.each(["lobby", "weaving", "finished"] as const)(
    "игра в фазе %s: панель пуста",
    (phase) => {
      const { container } = renderPanel({ turnPhase: null, phase });

      expect(container).toBeEmptyDOMElement();
    },
  );
});

describe("только у хоста", () => {
  test("не хост — панели нет вовсе", () => {
    const { container, server } = renderPanel({ user: "u2" });

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button")).toBeNull();
    expect(server.calls).toHaveLength(0);
  });

  test("хост определяется по данным игры, а не по имени", () => {
    renderPanel({ user: "u1" });

    expect(button("Собрать ходы")).toBeInTheDocument();
  });
});

describe("кнопки шлют свои запросы", () => {
  const callPaths = (server: { calls: RecordedCall[] }) =>
    server.calls.map((call) => `${call.method} ${call.path}`);

  test.each([
    [{ turnPhase: "discussion" }, "Собрать ходы", "collect"],
    [{ turnPhase: "collecting" }, "+1 мин", "extend"],
    [{ turnPhase: "collecting" }, "Закрыть сейчас", "close"],
    [{ turnPhase: "discussion" }, "Пауза", "pause"],
    [{ turnPhase: "collecting", phase: "paused" }, "Продолжить", "resume"],
    [{ turnPhase: "dm_failed" }, "Повторить", "retry"],
    [
      { turnPhase: "combat_wait", combat: combatOf("pc_lira") },
      "Поторопить",
      "hurry",
    ],
    [{ turnPhase: "camp", deadline: CAMP_DEADLINE }, "+1 мин", "rest-extend"],
    [
      { turnPhase: "camp", deadline: CAMP_DEADLINE },
      "Закончить привал",
      "rest-end",
    ],
  ] as const)("%j: «%s» → host/%s", async (setup, label, action) => {
    const { server } = renderPanel(setup);

    fireEvent.click(button(label));

    await vi.waitFor(() =>
      expect(callPaths(server)).toEqual([`POST /api/games/g1/host/${action}`]),
    );
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(server.calls[0].body).toBeUndefined();
  });

  test("отказ сервера показан текстом, кнопки остаются", async () => {
    renderPanel({
      turnPhase: "discussion",
      routes: {
        "POST /api/games/g1/host/collect": {
          status: 409,
          json: { detail: "Сбор ходов уже идёт." },
        },
      },
    });

    fireEvent.click(button("Собрать ходы"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сбор ходов уже идёт.",
    );
    expect(button("Собрать ходы")).toBeEnabled();
  });

  test("пока запрос идёт, кнопки недоступны и второй запрос не уходит", async () => {
    const { server, release } = renderPanel({
      turnPhase: "discussion",
      hold: "/host/collect",
    });

    fireEvent.click(button("Собрать ходы"));
    fireEvent.click(button("Собрать ходы"));

    expect(button("Собрать ходы")).toBeDisabled();
    expect(button("Пауза")).toBeDisabled();
    release();
    await vi.waitFor(() => expect(button("Собрать ходы")).toBeEnabled());
    expect(server.calls).toHaveLength(1);
  });

  test("старая ошибка исчезает при следующем нажатии", async () => {
    let attempts = 0;
    renderPanel({
      turnPhase: "discussion",
      routes: {
        "POST /api/games/g1/host/collect": () =>
          ++attempts === 1
            ? { status: 409, json: { detail: "Сбор ходов уже идёт." } }
            : PHASE_REPLY,
      },
    });
    fireEvent.click(button("Собрать ходы"));
    await screen.findByRole("alert");

    fireEvent.click(button("Собрать ходы"));

    await vi.waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});

describe("«Поторопить» в бою", () => {
  const waiting = (overrides: Setup = {}): Setup => ({
    turnPhase: "combat_wait",
    combat: combatOf("pc_lira"),
    ...overrides,
  });

  test("ходит герой, срока нет — кнопка есть у хоста", () => {
    renderPanel(waiting());

    expect(button("Поторопить")).toBeEnabled();
  });

  test("ходит чужой герой — тоже: торопят того, кто думает", () => {
    const combat = combatWith("pc_boris", [
      heroFighter("pc_lira", "Лира"),
      heroFighter("pc_boris", "Борин"),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

    renderPanel(waiting({ combat }));

    expect(button("Поторопить")).toBeEnabled();
  });

  test("отсчёт уже идёт — кнопки нет", () => {
    renderPanel(waiting({ deadline: "2026-10-20T12:01:00+00:00" }));

    noButton("Поторопить");
  });

  test("пришёл срок («Поторопить» сработала) — кнопка исчезает", () => {
    const { show } = renderPanel(waiting());
    expect(button("Поторопить")).toBeInTheDocument();

    show(waiting({ deadline: "2026-10-20T12:01:00+00:00" }));

    noButton("Поторопить");
  });

  test("ходит монстр — торопить некого, кнопки нет (есть «Повторить»)", () => {
    renderPanel(waiting({ combat: combatOf("skeleton-1") }));

    noButton("Поторопить");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("ходит спрятанный монстр (current_id пуст) — кнопки нет", () => {
    renderPanel(waiting({ combat: combatOf(null) }));

    noButton("Поторопить");
  });

  test("current_id не из порядка хода — кнопки нет", () => {
    renderPanel(waiting({ combat: combatOf("ghost") }));

    noButton("Поторопить");
  });

  test("боя в данных нет — кнопки нет", () => {
    renderPanel(waiting({ combat: null }));

    noButton("Поторопить");
  });

  test.each([
    "discussion",
    "collecting",
    "resolving",
    "dm_failed",
    "camp",
  ] as const)(
    "фаза хода %s — кнопки нет, даже если бой в данных ещё есть",
    (turnPhase) => {
      renderPanel({ turnPhase, combat: combatOf("pc_lira") });

      noButton("Поторопить");
    },
  );

  test("игра на паузе — вместо неё «Продолжить»", () => {
    renderPanel(waiting({ phase: "paused" }));

    noButton("Поторопить");
    expect(buttons()).toEqual(["Продолжить"]);
  });

  test("не хост кнопки не видит", () => {
    renderPanel(waiting({ user: "u2" }));

    noButton("Поторопить");
  });

  test("отказ сервера показан его текстом", async () => {
    renderPanel(
      waiting({
        routes: {
          "POST /api/games/g1/host/hurry": {
            status: 409,
            json: { detail: "Отсчёт уже идёт." },
          },
        },
      }),
    );

    fireEvent.click(button("Поторопить"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Отсчёт уже идёт.",
    );
    expect(button("Поторопить")).toBeEnabled();
  });
});

describe("недееспособный герой на очереди (can_act: false)", () => {
  const RETRY = "POST /api/games/g1/host/retry";

  /** Лира и Борин — герои, Скелет — монстр; у кого `can_act: false`, тот не может действовать. */
  const waitingFor = (
    current: string,
    canAct: { pc_lira?: boolean; pc_boris?: boolean },
    overrides: Setup = {},
  ): Setup => ({
    turnPhase: "combat_wait",
    combat: combatWith(current, [
      heroFighter("pc_lira", "Лира", { can_act: canAct.pc_lira }),
      heroFighter("pc_boris", "Борин", { can_act: canAct.pc_boris }),
      monsterFighter("skeleton-1", "Скелет"),
    ]),
    ...overrides,
  });

  test("свой герой хоста не может действовать: «Повторить» сразу, «Поторопить» нет", () => {
    renderPanel(waitingFor("pc_lira", { pc_lira: false }));

    noButton("Поторопить");
    expect(buttons()).toEqual([
      "Повторить",
      "Пауза",
      "Перегенерировать ответ мастера",
    ]);
  });

  test("чужой герой не может действовать — то же самое", () => {
    renderPanel(waitingFor("pc_boris", { pc_boris: false }));

    noButton("Поторопить");
    expect(button("Повторить")).toBeEnabled();
  });

  test("«Повторить» шлёт host/retry — это и снимает затор", async () => {
    const { server } = renderPanel(waitingFor("pc_lira", { pc_lira: false }));

    fireEvent.click(button("Повторить"));

    await vi.waitFor(() =>
      expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual(
        [RETRY],
      ),
    );
  });

  test("отсчёт «Поторопить» ещё идёт, а герой уже не может действовать — «Повторить» по-прежнему есть", () => {
    renderPanel(
      waitingFor(
        "pc_lira",
        { pc_lira: false },
        { deadline: "2026-10-20T12:01:00+00:00" },
      ),
    );

    noButton("Поторопить");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("герой может действовать (can_act: true): «Поторопить» есть, «Повторить» не нужна", () => {
    renderPanel(waitingFor("pc_lira", { pc_lira: true }));

    expect(buttons()).toEqual([
      "Поторопить",
      "Пауза",
      "Перегенерировать ответ мастера",
    ]);
  });

  test("недееспособен другой герой, а ходит способный — торопить можно", () => {
    renderPanel(waitingFor("pc_lira", { pc_lira: true, pc_boris: false }));

    expect(button("Поторопить")).toBeEnabled();
    noButton("Повторить");
  });

  test("ходит монстр (признака can_act у него нет): прежнее поведение — «Повторить» без «Поторопить»", () => {
    renderPanel(waitingFor("skeleton-1", { pc_lira: false }));

    noButton("Поторопить");
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("у недееспособного героя на паузе — только «Продолжить»", () => {
    renderPanel(waitingFor("pc_lira", { pc_lira: false }, { phase: "paused" }));

    expect(buttons()).toEqual(["Продолжить"]);
  });

  test("не хост панели не получает вовсе", () => {
    const { container } = renderPanel(
      waitingFor("pc_lira", { pc_lira: false }, { user: "u2" }),
    );

    expect(container).toBeEmptyDOMElement();
  });

  test("очередь перешла к способному герою — «Повторить» исчезает, «Поторопить» появляется", () => {
    const { show } = renderPanel(waitingFor("pc_lira", { pc_lira: false }));
    expect(button("Повторить")).toBeInTheDocument();

    show(waitingFor("pc_boris", { pc_lira: false, pc_boris: true }));

    noButton("Повторить");
    expect(button("Поторопить")).toBeEnabled();
  });
});

describe("кнопки привала", () => {
  const camping = (overrides: Setup = {}): Setup => ({
    turnPhase: "camp",
    deadline: CAMP_DEADLINE,
    ...overrides,
  });

  test("не хост их не видит", () => {
    const { container } = renderPanel(camping({ user: "u2" }));

    expect(container).toBeEmptyDOMElement();
  });

  test("срока привала нет — продлить нечего, но закончить можно", () => {
    renderPanel(camping({ deadline: null }));

    noButton("+1 мин");
    expect(button("Закончить привал")).toBeEnabled();
  });

  test("игра на паузе — вместо них «Продолжить»", () => {
    renderPanel(camping({ phase: "paused", deadline: null }));

    expect(buttons()).toEqual(["Продолжить"]);
  });

  test("в других фазах их нет", () => {
    renderPanel({ turnPhase: "discussion" });

    noButton("Закончить привал");
    expect(buttons()).not.toContain("+1 мин");
  });

  test("отказ сервера показан его текстом", async () => {
    renderPanel(
      camping({
        routes: {
          "POST /api/games/g1/host/rest-end": {
            status: 409,
            json: { detail: "Привал не идёт." },
          },
        },
      }),
    );

    fireEvent.click(button("Закончить привал"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Привал не идёт.",
    );
  });

  test("пока запрос идёт, обе кнопки недоступны и второй запрос не уходит", async () => {
    const { server, release } = renderPanel(
      camping({ hold: "/host/rest-extend" }),
    );

    fireEvent.click(button("+1 мин"));
    fireEvent.click(button("+1 мин"));

    expect(button("+1 мин")).toBeDisabled();
    expect(button("Закончить привал")).toBeDisabled();
    release();
    await vi.waitFor(() => expect(button("+1 мин")).toBeEnabled());
    expect(server.calls).toHaveLength(1);
  });
});

describe("на привале любого вида (контракт B9, §4)", () => {
  const rest = (kind: RestView["kind"]): RestView => ({
    kind,
    deadline_at: CAMP_DEADLINE,
    my_ideas: [],
    lines: [],
  });

  test.each(["short", "long"] as const)(
    "привал %s: «+1 мин» и «Закончить привал» шлют host/rest-extend и host/rest-end",
    async (kind) => {
      const { server } = renderPanel({
        turnPhase: "camp",
        deadline: CAMP_DEADLINE,
        rest: rest(kind),
      });

      fireEvent.click(button("+1 мин"));
      await vi.waitFor(() => expect(button("Закончить привал")).toBeEnabled());
      fireEvent.click(button("Закончить привал"));

      await vi.waitFor(() =>
        expect(
          server.calls.map((call) => `${call.method} ${call.path}`),
        ).toEqual([
          "POST /api/games/g1/host/rest-extend",
          "POST /api/games/g1/host/rest-end",
        ]),
      );
    },
  );

  test("на привале начать новый привал нельзя", () => {
    renderPanel({
      turnPhase: "camp",
      deadline: CAMP_DEADLINE,
      rest: rest("short"),
    });

    noButton(/привал \(/);
  });
});

describe("начать привал: только хосту, только в обсуждении вне боя и паузы", () => {
  const REST = "POST /api/games/g1/host/rest";
  const SHORT = "Короткий привал (2 мин)";
  const LONG = "Долгий привал (5 мин)";
  const posted = (server: { calls: RecordedCall[] }) =>
    server.calls.filter((call) => `${call.method} ${call.path}` === REST);

  test("сроки — из настроек игры: целые минуты минутами, иначе секундами", () => {
    renderPanel({ game: { short_rest_seconds: 90, camp_seconds: 600 } });

    expect(button("Короткий привал (90 сек)")).toBeEnabled();
    expect(button("Долгий привал (10 мин)")).toBeEnabled();
  });

  test.each([
    "collecting",
    "resolving",
    "dm_failed",
    "combat_wait",
    "camp",
  ] as const)("фаза хода %s — кнопок привала нет", (turnPhase) => {
    renderPanel({ turnPhase, deadline: CAMP_DEADLINE });

    noButton(SHORT);
    noButton(LONG);
  });

  test("обсуждение во время боя — кнопок привала нет", () => {
    renderPanel({ turnPhase: "discussion", combat: combatOf("pc_lira") });

    noButton(SHORT);
    noButton(LONG);
    expect(button("Собрать ходы")).toBeInTheDocument();
  });

  test("игра на паузе — кнопок привала нет", () => {
    renderPanel({ turnPhase: "discussion", phase: "paused" });

    expect(buttons()).toEqual(["Продолжить"]);
  });

  test.each(["lobby", "weaving", "finished"] as const)(
    "игра в фазе %s — кнопок привала нет",
    (phase) => {
      renderPanel({ turnPhase: "discussion", phase });

      noButton(SHORT);
      noButton(LONG);
    },
  );

  test("не хост кнопок привала не видит и ничего не шлёт", () => {
    const { container, server } = renderPanel({ user: "u2" });

    expect(container).toBeEmptyDOMElement();
    expect(server.calls).toHaveLength(0);
  });

  test.each([
    [SHORT, "short", /короткий привал/i],
    [LONG, "long", /долгий привал/i],
  ] as const)("«%s»: сначала вопрос, запроса ещё нет", (label, _kind, text) => {
    const { server } = renderPanel();

    fireEvent.click(button(label));

    const question = screen.getByRole("group", { name: "Начать привал?" });
    expect(question).toHaveTextContent(text);
    expect(button("Да, начать привал")).toBeEnabled();
    expect(server.calls).toHaveLength(0);
  });

  test.each([SHORT, LONG])(
    "«%s»: вопрос предупреждает, что черновики раунда до мастера не дойдут",
    (label) => {
      renderPanel();

      fireEvent.click(button(label));

      // Привал по кнопке превращает открытый раунд в ход «конец привала» (контракт B9, §11 п. 6).
      expect(
        screen.getByRole("group", { name: "Начать привал?" }),
      ).toHaveTextContent(
        "Черновики ходов этого раунда до мастера не дойдут — после привала игрокам придётся написать их заново.",
      );
    },
  );

  test.each([
    [SHORT, "short"],
    [LONG, "long"],
  ] as const)(
    "подтверждение «%s» шлёт host/rest {kind: %s}",
    async (label, kind) => {
      const { server } = renderPanel();
      fireEvent.click(button(label));

      fireEvent.click(button("Да, начать привал"));

      await vi.waitFor(() => expect(posted(server)).toHaveLength(1));
      expect(posted(server)[0].body).toEqual({ kind });
      expect(posted(server)[0].headers.get("Authorization")).toBe(
        "Bearer tok-u1",
      );
      expect(server.calls).toHaveLength(1);
      await vi.waitFor(() =>
        expect(
          screen.queryByRole("group", { name: "Начать привал?" }),
        ).toBeNull(),
      );
    },
  );

  test("«Отмена» убирает вопрос и ничего не шлёт", () => {
    const { server } = renderPanel();
    fireEvent.click(button(SHORT));

    fireEvent.click(button("Отмена"));

    expect(screen.queryByRole("group", { name: "Начать привал?" })).toBeNull();
    expect(button(SHORT)).toBeInTheDocument();
    expect(server.calls).toHaveLength(0);
  });

  test("пока открыт вопрос, второй вопрос не открывается поверх: кнопки-вопросы скрыты", () => {
    renderPanel();

    fireEvent.click(button(LONG));

    noButton(SHORT);
    noButton(LONG);
    noButton("Перегенерировать ответ мастера");
    expect(button("Собрать ходы")).toBeEnabled();
  });

  test("отказ сервера показан его текстом", async () => {
    const refusal = "Игроки уже отправили ходы: сначала разберите раунд.";
    renderPanel({
      routes: { [REST]: { status: 409, json: { detail: refusal } } },
    });
    fireEvent.click(button(SHORT));

    fireEvent.click(button("Да, начать привал"));

    expect(await screen.findByRole("alert")).toHaveTextContent(refusal);
    expect(button(SHORT)).toBeEnabled();
  });

  test("пока запрос идёт, кнопки недоступны и второй запрос не уходит", async () => {
    const { server, release } = renderPanel({ hold: "/host/rest" });
    fireEvent.click(button(SHORT));
    fireEvent.click(button("Да, начать привал"));

    expect(button(SHORT)).toBeDisabled();
    expect(button(LONG)).toBeDisabled();
    fireEvent.click(button(SHORT));
    release();
    await vi.waitFor(() => expect(button(SHORT)).toBeEnabled());
    expect(posted(server)).toHaveLength(1);
  });

  test("смена фазы убирает вопрос", () => {
    const { show } = renderPanel();
    fireEvent.click(button(SHORT));

    show({ turnPhase: "collecting" });

    expect(screen.queryByRole("group", { name: "Начать привал?" })).toBeNull();
  });
});

// Финальное покрытие B9: каждый отказ сервера (тексты B9.3, «Контракт для B9.4/B9.5») хост видит
// дословно — клиент не подменяет их своими словами и не прячет за общим «ошибка».
describe("привалы: отказы сервера дословно (финальное покрытие B9)", () => {
  const REST = "POST /api/games/g1/host/rest";

  test.each([
    "Игра на паузе: хост должен нажать «Продолжить».",
    "Игра сейчас не идёт.",
    "Привал уже идёт: второй начать нельзя.",
    "В бою привал невозможен: сначала закончите бой.",
    "Начать привал можно, пока игроки обсуждают ход.",
    "Игроки уже отправили ходы: сначала разберите раунд.",
    "Между долгими привалами допустимо не больше 2 коротких, а прошло уже 2: нужен долгий привал.",
  ])("host/rest → 409 «%s»", async (detail) => {
    renderPanel({ routes: { [REST]: { status: 409, json: { detail } } } });
    fireEvent.click(button("Долгий привал (5 мин)"));

    fireEvent.click(button("Да, начать привал"));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    // Вопрос закрыт, а кнопки привалов снова доступны: хост может выбрать другой.
    expect(screen.queryByRole("group", { name: "Начать привал?" })).toBeNull();
    expect(button("Короткий привал (2 мин)")).toBeEnabled();
    expect(button("Долгий привал (5 мин)")).toBeEnabled();
  });

  test("«+1 мин» на кончившемся привале: отказ сервера дословно", async () => {
    const detail = "Продлить можно только идущий привал.";
    renderPanel({
      turnPhase: "camp",
      deadline: CAMP_DEADLINE,
      routes: {
        "POST /api/games/g1/host/rest-extend": {
          status: 409,
          json: { detail },
        },
      },
    });

    fireEvent.click(button("+1 мин"));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  test("«Перегенерировать» после привала по кнопке: отказ сервера дословно", async () => {
    const detail =
      "После хода мастера игра уже изменилась: перегенерировать нельзя.";
    renderPanel({
      turnPhase: "camp",
      deadline: CAMP_DEADLINE,
      routes: {
        "POST /api/games/g1/host/regenerate": { status: 409, json: { detail } },
      },
    });
    fireEvent.click(button("Перегенерировать ответ мастера"));

    fireEvent.click(button("Да, перегенерировать"));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });
});

describe("«Перегенерировать ответ мастера»", () => {
  const REGENERATE = "POST /api/games/g1/host/regenerate";

  test("сначала предупреждение, запроса ещё нет", () => {
    const { server } = renderPanel({ turnPhase: "discussion" });

    fireEvent.click(button("Перегенерировать ответ мастера"));

    expect(
      screen.getByText(/снимет черновики и сданные ходы игроков/),
    ).toBeInTheDocument();
    expect(server.calls).toHaveLength(0);
    expect(button("Да, перегенерировать")).toBeEnabled();
  });

  test("«Отмена» убирает предупреждение и ничего не шлёт", () => {
    const { server } = renderPanel({ turnPhase: "discussion" });
    fireEvent.click(button("Перегенерировать ответ мастера"));

    fireEvent.click(button("Отмена"));

    expect(screen.queryByText(/снимет черновики/)).toBeNull();
    expect(button("Перегенерировать ответ мастера")).toBeInTheDocument();
    expect(server.calls).toHaveLength(0);
  });

  test("подтверждение шлёт regenerate", async () => {
    const { server } = renderPanel({ turnPhase: "discussion" });
    fireEvent.click(button("Перегенерировать ответ мастера"));

    fireEvent.click(button("Да, перегенерировать"));

    await vi.waitFor(() =>
      expect(server.calls.map((c) => `${c.method} ${c.path}`)).toEqual([
        REGENERATE,
      ]),
    );
    await vi.waitFor(() =>
      expect(screen.queryByText(/снимет черновики/)).toBeNull(),
    );
  });

  test("отказ сервера после подтверждения показан", async () => {
    renderPanel({
      turnPhase: "discussion",
      routes: {
        [REGENERATE]: {
          status: 409,
          json: { detail: "Следующий ход уже начат: перегенерировать нельзя." },
        },
      },
    });
    fireEvent.click(button("Перегенерировать ответ мастера"));
    fireEvent.click(button("Да, перегенерировать"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "перегенерировать нельзя",
    );
  });

  test("смена фазы убирает предупреждение", () => {
    const { show } = renderPanel({ turnPhase: "discussion" });
    fireEvent.click(button("Перегенерировать ответ мастера"));

    show({ turnPhase: "resolving" });

    expect(screen.queryByText(/снимет черновики/)).toBeNull();
  });
});

describe("«Перегенерировать» — только когда сервер разрешает (контракт B11, §1 п. 11)", () => {
  test("привал по кнопке хоста: сервер не разрешает — кнопки нет, «+1 мин» и «Закончить привал» на месте", () => {
    renderPanel({
      turnPhase: "camp",
      deadline: CAMP_DEADLINE,
      canRegenerate: false,
    });

    expect(buttons()).toEqual(["+1 мин", "Закончить привал", "Пауза"]);
  });

  test("привал мастера: сервер разрешает — кнопка есть", () => {
    renderPanel({
      turnPhase: "camp",
      deadline: CAMP_DEADLINE,
      canRegenerate: true,
    });

    expect(button("Перегенерировать ответ мастера")).toBeEnabled();
  });

  test("обсуждение, а сервер не разрешает (например, нечего перегенерировать) — кнопки нет", () => {
    renderPanel({ turnPhase: "discussion", canRegenerate: false });

    noButton("Перегенерировать ответ мастера");
    expect(button("Собрать ходы")).toBeInTheDocument();
  });

  test("сервер до B11 без признака — кнопки нет, остальные кнопки на месте", () => {
    const view = viewFor({ turnPhase: "discussion" });
    const { can_regenerate: _dropped, ...old } = view.phase;
    signInAs({ id: HOST, name: "Игрок" });
    const api = createApi(fakeServer(HOST_ROUTES).fetch, sessionToken);
    render(
      <ApiContext value={api}>
        <HostPanel gameId="g1" view={{ ...view, phase: old }} userId={HOST} />
      </ApiContext>,
    );

    noButton("Перегенерировать ответ мастера");
    expect(button("Собрать ходы")).toBeInTheDocument();
  });

  // Финальное покрытие B11: признак сервера — необходимое условие, но не достаточное.
  test.each(["paused", "finished"] as const)(
    "игра %s, а признак сервера true — кнопки нет: перегенерировать можно только в идущей игре",
    (phase) => {
      renderPanel({ turnPhase: "discussion", phase, canRegenerate: true });

      noButton("Перегенерировать ответ мастера");
    },
  );

  test("признак пропал, пока открыт вопрос, — вопрос закрывается", () => {
    const { show } = renderPanel({ turnPhase: "discussion" });
    fireEvent.click(button("Перегенерировать ответ мастера"));

    show({ turnPhase: "discussion", canRegenerate: false });

    expect(
      screen.queryByRole("group", { name: "Перегенерировать ответ мастера?" }),
    ).toBeNull();
  });
});

describe("«Повторить» в «Мастер думает»: только через минуту ожидания", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  const wait = (ms: number) => act(() => vi.advanceTimersByTime(ms));

  test("в первую минуту кнопки нет, на 60-й секунде появляется", () => {
    renderPanel({ turnPhase: "resolving" });

    wait(59_999);
    noButton("Повторить");
    wait(1);

    expect(button("Повторить")).toBeEnabled();
  });

  test("мастер ответил раньше срока — ожидание сбрасывается, кнопка не появится", () => {
    const { show } = renderPanel({ turnPhase: "resolving" });
    wait(40_000);

    show({ turnPhase: "discussion" });
    wait(60_000);

    noButton("Повторить");
  });

  test("новое «Мастер думает» отсчитывает минуту заново", () => {
    const { show } = renderPanel({ turnPhase: "resolving" });
    wait(70_000);
    expect(button("Повторить")).toBeInTheDocument();

    show({ turnPhase: "discussion" });
    show({ turnPhase: "resolving" });

    noButton("Повторить");
    wait(60_000);
    expect(button("Повторить")).toBeInTheDocument();
  });

  test("сбой мастера: «Повторить» появляется сразу, без ожидания", () => {
    const { show } = renderPanel({ turnPhase: "resolving" });
    wait(10_000);

    show({ turnPhase: "dm_failed" });

    expect(button("Повторить")).toBeEnabled();
  });

  test("не хост минуты не ждёт и кнопки не получает", () => {
    renderPanel({ turnPhase: "resolving", user: "u2" });

    wait(120_000);

    noButton("Повторить");
  });

  test("после ухода с экрана таймер снимается", () => {
    const { unmount } = renderPanel({ turnPhase: "resolving" });
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("«Повторить» по серверному началу разбора (phase_since, контракт B11, §2)", () => {
  const NOW = Date.parse("2026-10-20T12:00:00+00:00");
  /** Серверное время `seconds` секунд назад. */
  const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

  beforeEach(() => {
    vi.useFakeTimers({ toFake: [...FAKE_TIMERS, "Date"] });
    vi.setSystemTime(NOW);
  });

  const wait = (ms: number) => act(() => vi.advanceTimersByTime(ms));

  test("перезагрузка на 50-й секунде разбора: кнопка через 10 с, а не через минуту", () => {
    renderPanel({ turnPhase: "resolving", phaseSince: ago(50) });

    wait(9_999);
    noButton("Повторить");
    wait(1);

    expect(button("Повторить")).toBeEnabled();
  });

  test("разбор идёт дольше минуты: кнопка сразу после перезагрузки", () => {
    renderPanel({ turnPhase: "resolving", phaseSince: ago(75) });

    wait(0);

    expect(button("Повторить")).toBeEnabled();
  });

  test("часы телефона отстают на 3 минуты: срок всё равно по серверу", () => {
    useGame.setState({ live: { ...NO_LIVE, clockOffsetMs: 180_000 } });
    // По часам сервера разбор идёт 50 с; по часам браузера начало было 3 мин 50 с назад.
    renderPanel({
      turnPhase: "resolving",
      phaseSince: new Date(NOW + 180_000 - 50_000).toISOString(),
    });

    wait(9_999);
    noButton("Повторить");
    wait(1);

    expect(button("Повторить")).toBeEnabled();
  });

  test("новый разбор с новым началом отсчитывает минуту от него", () => {
    const { show } = renderPanel({
      turnPhase: "resolving",
      phaseSince: ago(90),
    });
    wait(0);
    expect(button("Повторить")).toBeInTheDocument();

    show({ turnPhase: "resolving", phaseSince: ago(0) });
    wait(0);

    noButton("Повторить");
    wait(60_000);
    expect(button("Повторить")).toBeInTheDocument();
  });
});
