import { render, screen, within } from "@testing-library/react";
import type {
  GamePhase,
  GameSummary,
  PhaseView,
  PlayerStatus,
  RestView,
  TurnPhase,
} from "../api/types";
import type { LiveConnection } from "../store/game";
import { SERVER_TIME, gameView, phaseOf } from "./fixtures";
import { StatusBar } from "./StatusBar";

const NOW = Date.parse(SERVER_TIME);
const at = (seconds: number) => new Date(NOW + seconds * 1000).toISOString();

function renderBar(
  turnPhase: TurnPhase | null,
  options: {
    game?: GamePhase;
    phase?: GamePhase;
    deadline?: string | null;
    remaining?: number | null;
    statuses?: Record<string, PlayerStatus>;
    connection?: LiveConnection;
    /** Привал из снимка (контракт B9, §3); по умолчанию его нет. */
    rest?: RestView | null;
    /** Поправки к сводке игры (сроки отсчётов). */
    summary?: Partial<GameSummary>;
  } = {},
) {
  const { game = "playing", phase = game, deadline = null } = options;
  const view = gameView({
    phase: phaseOf(turnPhase, {
      phase,
      deadline_at: deadline,
      remaining_seconds: options.remaining ?? null,
    }),
    ...(options.statuses === undefined ? {} : { statuses: options.statuses }),
    rest: options.rest ?? null,
  });
  view.game = {
    ...view.game,
    ...options.summary,
    phase: game,
    turn_phase: turnPhase,
  };
  return render(
    <StatusBar
      view={view}
      clockOffsetMs={0}
      connection={options.connection ?? "open"}
    />,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

describe("подпись фазы", () => {
  test.each([
    ["playing", "discussion", "Обсуждение"],
    ["playing", "collecting", "Сбор ходов"],
    ["playing", "resolving", "Мастер думает…"],
    ["playing", "dm_failed", "Мастер не справился"],
    ["playing", "combat_wait", "Бой"],
    ["playing", "camp", "Привал"],
    ["playing", null, "Игра идёт"],
    // пауза важнее фазы хода
    ["paused", "discussion", "Пауза"],
    ["paused", "collecting", "Пауза"],
    ["paused", "camp", "Пауза"],
    // начало игры: та же единая формулировка ожидания, что в лобби и при разборе хода
    ["weaving", null, "Мастер думает…"],
    ["weaving", "resolving", "Мастер думает…"],
    ["weaving", "dm_failed", "Мастер не справился с началом игры"],
    ["lobby", null, "Ожидание начала игры"],
    ["finished", null, "Игра завершена"],
  ] as const)("%s / %s → «%s»", (game, turnPhase, label) => {
    renderBar(turnPhase, { game });

    expect(screen.getByText(label)).toBeInTheDocument();
  });

  test("подпись берётся из блока phase, а не из сводки игры: она обновляется своим событием", () => {
    const view = gameView({ phase: phaseOf("collecting") });
    view.game = { ...view.game, turn_phase: "discussion" };

    render(<StatusBar view={view} clockOffsetMs={0} connection="open" />);

    expect(screen.getByText("Сбор ходов")).toBeInTheDocument();
    expect(screen.queryByText("Обсуждение")).toBeNull();
  });
});

describe("значки игроков", () => {
  const statuses = { u1: "writing", u2: "ready", u3: "pass" } as const;

  const chip = (name: string): HTMLElement => {
    const item = screen.getByText(name).closest("li");
    if (item === null) throw new Error(`Нет значка игрока ${name}`);
    return item;
  };

  test("обсуждение: у каждого игрока «Пишет», «Готов» или «Пас»", () => {
    renderBar("discussion", { statuses });

    expect(within(chip("Вика")).getByText("Пишет")).toBeInTheDocument();
    expect(within(chip("Борис")).getByText("Готов")).toBeInTheDocument();
    expect(within(chip("Мила")).getByText("Пас")).toBeInTheDocument();
  });

  test.each(["collecting", "dm_failed"] as const)(
    "в фазе %s значки на месте",
    (turnPhase) => {
      renderBar(turnPhase, { statuses });

      expect(screen.getAllByRole("listitem")).toHaveLength(3);
    },
  );

  test.each(["resolving", "combat_wait", "camp"] as const)(
    "в фазе %s значков нет: чьи-то «готов» уже не про этот момент",
    (turnPhase) => {
      renderBar(turnPhase, { statuses });

      expect(screen.queryByRole("listitem")).toBeNull();
    },
  );

  test("«idle» — ход ещё не начат (D17): у игрока имя без значка, «Пишет» только у набирающего черновик", () => {
    renderBar("discussion", {
      statuses: { u1: "idle", u2: "writing", u3: "idle" },
    });

    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(within(chip("Вика")).queryByText(/Пишет|Готов|Пас/)).toBeNull();
    expect(within(chip("Мила")).queryByText(/Пишет|Готов|Пас/)).toBeNull();
    expect(within(chip("Борис")).getByText("Пишет")).toBeInTheDocument();
    expect(screen.getAllByText("Пишет")).toHaveLength(1);
  });

  test("начало раунда: у всех «idle» — «Пишет» не горит ни у кого", () => {
    renderBar("discussion", {
      statuses: { u1: "idle", u2: "idle", u3: "idle" },
    });

    expect(screen.queryByText(/Пишет|Готов|Пас/)).toBeNull();
    expect(screen.queryByText("idle")).toBeNull();
  });

  test("игрок без статуса — имя без значка статуса", () => {
    renderBar("discussion", { statuses: { u1: "ready" } });

    expect(within(chip("Борис")).queryByText(/Пишет|Готов|Пас/)).toBeNull();
    expect(within(chip("Вика")).getByText("Готов")).toBeInTheDocument();
  });

  test("имя игрока выводится как текст", () => {
    const view = gameView();
    view.game = {
      ...view.game,
      players: [{ ...view.game.players[0], name: "<b>Вика</b>" }],
    };

    const { container } = render(
      <StatusBar view={view} clockOffsetMs={0} connection="open" />,
    );

    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("<b>Вика</b>");
  });
});

describe("отсчёт в строке статуса", () => {
  test("сбор ходов: время до срока по серверным часам, воск от collect_seconds", () => {
    const { container } = renderBar("collecting", { deadline: at(60) });

    expect(screen.getByRole("timer")).toHaveAccessibleName(
      "До конца сбора 1:00",
    );
    const wax = container.querySelector(".ui-countdown__wax > span");
    expect((wax as HTMLElement).style.width).toBe("50%"); // 60 из 120
  });

  test("бой («Поторопить»): воск от hurry_seconds", () => {
    const { container } = renderBar("combat_wait", { deadline: at(30) });

    expect(screen.getByRole("timer")).toHaveAccessibleName("Время на ход 0:30");
    const wax = container.querySelector(".ui-countdown__wax > span");
    expect((wax as HTMLElement).style.width).toBe("50%"); // 30 из 60
  });

  test("привал: воск от camp_seconds", () => {
    const { container } = renderBar("camp", { deadline: at(75) });

    expect(screen.getByRole("timer")).toHaveAccessibleName(
      "До конца привала 1:15",
    );
    const wax = container.querySelector(".ui-countdown__wax > span");
    expect((wax as HTMLElement).style.width).toBe("25%"); // 75 из 300
  });

  describe("привал: воск от срока его вида (контракт B9, §9 п. 5)", () => {
    const rest = (kind: RestView["kind"]): RestView => ({
      kind,
      deadline_at: null,
      my_ideas: [],
      lines: [],
    });
    const waxOf = (container: HTMLElement) =>
      (container.querySelector(".ui-countdown__wax > span") as HTMLElement)
        .style.width;

    test("короткий: от short_rest_seconds", () => {
      const { container } = renderBar("camp", {
        deadline: at(60),
        rest: rest("short"),
      });

      expect(screen.getByRole("timer")).toHaveAccessibleName(
        "До конца привала 1:00",
      );
      expect(waxOf(container)).toBe("50%"); // 60 из 120
    });

    test("короткий только начался: свеча полная, а не от срока долгого", () => {
      const { container } = renderBar("camp", {
        deadline: at(90),
        rest: rest("short"),
        summary: { short_rest_seconds: 90, camp_seconds: 300 },
      });

      expect(waxOf(container)).toBe("100%");
    });

    test("долгий: от camp_seconds", () => {
      const { container } = renderBar("camp", {
        deadline: at(150),
        rest: rest("long"),
      });

      expect(waxOf(container)).toBe("50%"); // 150 из 300
    });

    test("вид ещё не пришёл (rest: null) — как долгий, от camp_seconds", () => {
      const { container } = renderBar("camp", {
        deadline: at(150),
        rest: null,
      });

      expect(waxOf(container)).toBe("50%");
    });

    test("пауза короткого привала: остаток стоит, воск от short_rest_seconds", () => {
      const { container } = renderBar("camp", {
        game: "paused",
        remaining: 30,
        rest: rest("short"),
      });

      expect(screen.getByRole("timer")).toHaveTextContent("0:30");
      expect(waxOf(container)).toBe("25%"); // 30 из 120
    });
  });

  test("обсуждение без срока: отсчёта нет", () => {
    renderBar("discussion");

    expect(screen.queryByRole("timer")).toBeNull();
  });

  test("пауза: остаток стоит, есть метка", () => {
    renderBar("collecting", {
      game: "paused",
      deadline: null,
      remaining: 42,
    });

    expect(screen.getByRole("timer")).toHaveTextContent("0:42");
    expect(screen.getAllByText("Пауза").length).toBeGreaterThanOrEqual(2);
  });

  test("мастер думает: отсчёта нет", () => {
    renderBar("resolving");

    expect(screen.queryByRole("timer")).toBeNull();
  });
});

describe("связь с игрой", () => {
  const banner = () => screen.getByText("Переподключаемся…");

  test("обрыв: «Переподключаемся…» внутри самой строки статуса, а не рядом с ней", () => {
    const { container } = renderBar("discussion", {
      connection: "reconnecting",
    });

    const bar = container.querySelector(".game-status");
    expect(bar).not.toBeNull();
    expect(bar).toContainElement(banner());
    // Прилипает именно эта строка (position: sticky в game.css): плашка едет вместе с ней.
    expect(bar).toContainElement(screen.getByText("Обсуждение"));
  });

  test("плашка — статус для читалки экрана, обычный текст без разметки", () => {
    renderBar("discussion", { connection: "reconnecting" });

    expect(banner()).toHaveAttribute("role", "status");
    expect(banner().children).toHaveLength(0);
    expect(screen.getAllByText("Переподключаемся…")).toHaveLength(1);
  });

  test.each(["open", "idle", "connecting"] as const)(
    "связь %s: плашки нет",
    (connection) => {
      renderBar("discussion", { connection });

      expect(screen.queryByText("Переподключаемся…")).toBeNull();
    },
  );

  test("плашка не зависит от фазы: она и на сборе ходов, и в бою, и на привале, и на паузе", () => {
    for (const [turnPhase, game] of [
      ["collecting", "playing"],
      ["combat_wait", "playing"],
      ["camp", "playing"],
      ["discussion", "paused"],
    ] as const) {
      const { unmount } = renderBar(turnPhase, {
        game,
        connection: "reconnecting",
      });

      expect(banner()).toBeInTheDocument();
      unmount();
    }
  });

  test("вернулась связь — плашка уходит, строка статуса остаётся", () => {
    const view = gameView({ phase: phaseOf("collecting") });
    const { rerender } = render(
      <StatusBar view={view} clockOffsetMs={0} connection="reconnecting" />,
    );
    expect(banner()).toBeInTheDocument();

    rerender(<StatusBar view={view} clockOffsetMs={0} connection="open" />);

    expect(screen.queryByText("Переподключаемся…")).toBeNull();
    expect(screen.getByText("Сбор ходов")).toBeInTheDocument();
  });

  test("плашка без движения: при «меньше движения» и без него в ней нет классов анимации", () => {
    for (const reduce of [false, true]) {
      vi.stubGlobal("matchMedia", (query: string) => ({
        matches: query.includes("prefers-reduced-motion") && reduce,
        addEventListener: () => {},
        removeEventListener: () => {},
      }));
      const { container, unmount } = renderBar("discussion", {
        connection: "reconnecting",
      });

      expect(banner()).toBeInTheDocument();
      expect(container.querySelector("[class*='ui-anim']")).toBeNull();
      unmount();
    }
  });
});

describe("день и часть суток (контракт B10, §1 п. 3)", () => {
  const withScene = (scene: PhaseView["scene"], turnPhase: TurnPhase | null) =>
    render(
      <StatusBar
        view={gameView({ phase: phaseOf(turnPhase, { scene }) })}
        clockOffsetMs={0}
        connection="open"
      />,
    );

  test.each([
    ["morning", "День 2, утро"],
    ["day", "День 2, полдень"],
    ["evening", "День 2, вечер"],
    ["night", "День 2, ночь"],
  ] as const)("%s → «%s» рядом с фазой", (part, text) => {
    const { container } = withScene(
      { type: "exploration", day: 2, part },
      "discussion",
    );

    const bar = container.querySelector(".game-status");
    expect(bar).toContainElement(screen.getByText(text));
    expect(bar).toContainElement(screen.getByText("Обсуждение"));
  });

  test("без сцены (старый снимок) — только фаза", () => {
    const { container } = withScene(null, "discussion");

    expect(screen.getByText("Обсуждение")).toBeInTheDocument();
    expect(container).not.toHaveTextContent(/День \d/);
  });

  test("день не читается вслух при каждой смене фазы: он вне живой области фазы", () => {
    withScene({ type: "camp", day: 3, part: "night" }, "camp");

    const phase = screen.getByText("Привал").closest("[role='status']");
    expect(phase).not.toBeNull();
    expect(phase).not.toHaveTextContent("День 3, ночь");
  });

  test("на привале таймер «До конца привала» остаётся в строке статуса рядом с днём", () => {
    const view = gameView({
      phase: phaseOf("camp", {
        deadline_at: at(75),
        scene: { type: "camp", day: 3, part: "night" },
      }),
    });

    const { container } = render(
      <StatusBar view={view} clockOffsetMs={0} connection="open" />,
    );

    const bar = container.querySelector(".game-status");
    expect(bar).toContainElement(screen.getByRole("timer"));
    expect(screen.getByRole("timer")).toHaveAccessibleName(
      "До конца привала 1:15",
    );
    expect(bar).toContainElement(screen.getByText("День 3, ночь"));
  });
});
