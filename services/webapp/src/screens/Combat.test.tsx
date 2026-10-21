import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type { ActionBody, TurnPhase } from "../api/types";
import {
  combatOf,
  combatWith,
  gameView,
  heroFighter,
  monsterFighter,
  myAction,
  phaseOf,
} from "../components/fixtures";
import { NO_LIVE, sessionToken, useGame } from "../store/game";
import type { GameView } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { Combat } from "./Combat";

const DRAFT = "PUT /api/games/g1/action";
const SUBMIT = "POST /api/games/g1/action/submit";

const FIELD = "Что делает твой персонаж?";

interface Setup {
  /** Бой; по умолчанию — Лира и скелет, ходит `current`. */
  combat?: GameView["combat"];
  current?: string | null;
  turnPhase?: TurnPhase;
  view?: Partial<GameView>;
  turn?: number;
  routes?: Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;
}

function viewFor({
  combat,
  current = "pc_lira",
  turnPhase = "combat_wait",
  view = {},
}: Setup): GameView {
  return gameView({
    phase: phaseOf(turnPhase),
    combat: combat === undefined ? combatOf(current) : combat,
    ...view,
  });
}

/** Игра и счётчик ходов берутся из хранилища, как на экране игры: ответы сервера доходят до поля. */
function Harness() {
  const view = useGame((state) => state.live.view);
  const turn = useGame((state) => state.live.turn);
  return view === null ? null : <Combat gameId="g1" view={view} turn={turn} />;
}

const showInStore = (setup: Setup) =>
  useGame.setState({
    live: { ...NO_LIVE, view: viewFor(setup), turn: setup.turn ?? 0 },
  });

/** Экран боя глазами игрока Лиры (`u1`, герой `pc_lira`). */
function renderCombat(setup: Setup = {}) {
  signInAs({ id: "u1", name: "Вика" });
  let draft: ActionBody | null = null;
  const server = fakeServer({
    [DRAFT]: (call): FakeReply => {
      draft = call.body as ActionBody;
      return { json: { ...draft, status: "draft" } };
    },
    [SUBMIT]: (): FakeReply => ({
      json: { ...(draft as ActionBody), status: "submitted" },
    }),
    ...setup.routes,
  });
  showInStore(setup);
  const result = render(
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <Harness />
    </ApiContext>,
  );
  return {
    server,
    container: result.container,
    show: (next: Setup) => act(() => showInStore(next)),
  };
}

const field = () => screen.queryByLabelText(FIELD);
const initiative = () => screen.getByRole("list", { name: "Порядок хода" });
const turnNote = () => screen.queryByRole("status");

describe("порядок хода и зоны на экране боя", () => {
  test("раунд, порядок хода и зоны на месте", () => {
    renderCombat({ combat: { ...combatOf("pc_lira"), round: 3 } });

    expect(
      screen.getByRole("heading", { name: /Бой.*раунд 3/ }),
    ).toBeInTheDocument();
    expect(within(initiative()).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("list", { name: "Зоны боя" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Зал" })).toBeInTheDocument();
  });

  test("текущий боец выделен в порядке хода", () => {
    renderCombat({ current: "skeleton-1" });

    const [lira, skeleton] = within(initiative()).getAllByRole("listitem");
    expect(skeleton).toHaveAttribute("aria-current", "true");
    expect(lira).not.toHaveAttribute("aria-current");
  });

  test("монстр с именем NPC называется этим именем и в порядке, и в зонах, и в подписи хода", () => {
    const combat = combatWith("cultist_fanatic-1", [
      heroFighter("pc_lira", "Лира"),
      monsterFighter("cultist_fanatic-1", "Фабиан"),
    ]);

    renderCombat({ combat });

    expect(screen.getAllByText("Фабиан")).toHaveLength(2); // порядок хода и зона
    expect(turnNote()).toHaveTextContent("Ходит Фабиан.");
    expect(screen.queryByText(/Фанатик культа|cultist_fanatic/)).toBeNull();
  });

  test("спрятанный монстр: ни в порядке хода, ни в зонах; ход его — «Ходит мастер.»", () => {
    // Сервер убирает спрятанного монстра из боя целиком, а current_id делает пустым.
    const combat = combatWith(null, [
      heroFighter("pc_lira", "Лира"),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

    const { container } = renderCombat({ combat });

    expect(within(initiative()).getAllByRole("listitem")).toHaveLength(2);
    expect(container).not.toHaveTextContent(/Тень|shade/);
    expect(container.querySelector("[aria-current]")).toBeNull();
    expect(turnNote()).toHaveTextContent("Ходит мастер.");
    expect(field()).toBeNull();
  });

  test("тексты боя выводятся только как текст", () => {
    const combat = combatWith("skeleton-1", [
      heroFighter("pc_lira", "Лира"),
      monsterFighter("skeleton-1", "<img src=x onerror=alert(1)>Скелет"),
    ]);

    const { container } = renderCombat({ combat });

    expect(container.querySelector("img")).toBeNull();
    expect(turnNote()).toHaveTextContent(
      "Ходит <img src=x onerror=alert(1)>Скелет.",
    );
  });
});

describe("поле хода — только у текущего героя", () => {
  test("ходит мой герой: поле открыто и можно сдать ход", () => {
    renderCombat({ current: "pc_lira" });

    expect(field()).toBeEnabled();
    expect(screen.getByRole("button", { name: "Пас" })).toBeEnabled();
    expect(turnNote()).toBeNull();
  });

  test("ходит чужой герой: поля нет, видно «Ходит Борин.»", () => {
    const combat = combatWith("pc_boris", [
      heroFighter("pc_lira", "Лира"),
      heroFighter("pc_boris", "Борин"),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

    renderCombat({ combat });

    expect(field()).toBeNull();
    expect(screen.queryByRole("button", { name: "Готов" })).toBeNull();
    expect(turnNote()).toHaveTextContent("Ходит Борин.");
  });

  test("ходит монстр: поля нет, видно «Ходит Скелет.»", () => {
    renderCombat({ current: "skeleton-1" });

    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent("Ходит Скелет.");
  });

  test("сдать ход: черновик уходит перед «Готов», ответ «Ход сдан»", async () => {
    const { server } = renderCombat({ current: "pc_lira" });
    fireEvent.change(field() as HTMLElement, {
      target: { value: "Бью скелета мечом" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Готов" }));

    expect(await screen.findByText("Ход сдан")).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      DRAFT,
      SUBMIT,
    ]);
    expect(server.calls[0].body).toMatchObject({ text: "Бью скелета мечом" });
  });

  test("мой ход уже сдан: карточка «Ход сдан» остаётся, пока мастер разбирает", () => {
    renderCombat({
      turnPhase: "resolving",
      view: { action: myAction({ text: "Бью скелета", status: "submitted" }) },
    });

    expect(screen.getByText("Ход сдан")).toBeInTheDocument();
    expect(field()).toBeNull();
  });

  test("мастер думает, ход мой и не сдан: поле закрыто с пояснением", () => {
    renderCombat({ turnPhase: "resolving" });

    expect(field()).toBeDisabled();
    expect(screen.getByText(/Мастер думает/)).toBeInTheDocument();
  });

  test("игра на паузе: поле хода закрыто", () => {
    renderCombat({
      view: {
        phase: phaseOf("combat_wait", {
          phase: "paused",
          remaining_seconds: 30,
        }),
      },
    });

    expect(field()).toBeDisabled();
    expect(screen.getByText(/Игра на паузе/)).toBeInTheDocument();
  });

  test("мастер не справился: чужому игроку видно, что ходы сохранены", () => {
    renderCombat({ current: "skeleton-1", turnPhase: "dm_failed" });

    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent(/Мастер не справился/);
  });

  test("новый ход начинается с пустого поля", () => {
    const { show } = renderCombat({ current: "pc_lira", turn: 0 });
    fireEvent.change(field() as HTMLElement, {
      target: { value: "Не успел дописать" },
    });
    expect(field()).toHaveValue("Не успел дописать");

    show({ current: "pc_lira", turn: 1 });

    expect(field()).toHaveValue("");
  });

  test("у игрока без героя поля нет, а ход читается по порядку", () => {
    renderCombat({ view: { character: null } });

    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent("Ходит Лира.");
  });
});

describe("герой на очереди не может действовать (can_act: false)", () => {
  /** Лира и Борин — герои, Скелет — монстр; у кого `can_act: false`, тот парализован. */
  const withDisabled = (
    current: string,
    canAct: { pc_lira?: boolean; pc_boris?: boolean },
  ) =>
    combatWith(current, [
      heroFighter("pc_lira", "Лира", { can_act: canAct.pc_lira }),
      heroFighter("pc_boris", "Борин", { can_act: canAct.pc_boris }),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

  test("мой герой: поля нет и «Готов» нет, вместо «Ходит Лира.» — «Лира не может действовать.»", () => {
    renderCombat({ combat: withDisabled("pc_lira", { pc_lira: false }) });

    expect(field()).toBeNull();
    expect(screen.queryByRole("button", { name: "Готов" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Пас" })).toBeNull();
    expect(turnNote()).toHaveTextContent("Лира не может действовать.");
    expect(screen.queryByText("Ходит Лира.")).toBeNull();
  });

  test("чужой герой: то же пояснение с его именем", () => {
    renderCombat({ combat: withDisabled("pc_boris", { pc_boris: false }) });

    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent("Борин не может действовать.");
    expect(screen.queryByText("Ходит Борин.")).toBeNull();
  });

  test("в порядке хода такой герой помечен", () => {
    renderCombat({ combat: withDisabled("pc_lira", { pc_lira: false }) });

    const [lira, boris] = within(initiative()).getAllByRole("listitem");
    expect(within(lira).getByText("Не может действовать")).toBeInTheDocument();
    expect(within(boris).queryByText("Не может действовать")).toBeNull();
  });

  test("герой может действовать (can_act: true): поле открыто, как прежде", () => {
    renderCombat({ combat: withDisabled("pc_lira", { pc_lira: true }) });

    expect(field()).toBeEnabled();
    expect(turnNote()).toBeNull();
    expect(screen.queryByText("Не может действовать")).toBeNull();
  });

  test("герой без признака (старый сервер): поле открыто", () => {
    renderCombat({ combat: withDisabled("pc_lira", {}) });

    expect(field()).toBeEnabled();
  });

  test("ходит монстр, герой лежит: пояснение про монстра, пометка у героя остаётся", () => {
    renderCombat({ combat: withDisabled("skeleton-1", { pc_lira: false }) });

    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent("Ходит Скелет.");
    expect(screen.getAllByText("Не может действовать")).toHaveLength(1);
  });

  test("очередь дошла до недееспособного героя и прошла дальше: поле закрывается и открывается снова", () => {
    const { show } = renderCombat({
      combat: withDisabled("pc_boris", { pc_lira: false }),
    });
    expect(turnNote()).toHaveTextContent("Ходит Борин.");

    show({ combat: withDisabled("pc_lira", { pc_lira: false }) });
    expect(field()).toBeNull();
    expect(turnNote()).toHaveTextContent("Лира не может действовать.");

    show({ combat: withDisabled("pc_lira", { pc_lira: true }), turn: 1 });
    expect(field()).toBeEnabled();
    expect(turnNote()).toBeNull();
  });

  test("пауза важнее: на паузе сказано про паузу", () => {
    renderCombat({
      combat: withDisabled("pc_lira", { pc_lira: false }),
      view: { phase: phaseOf("combat_wait", { phase: "paused" }) },
    });

    expect(turnNote()).toHaveTextContent(/Игра на паузе/);
    expect(screen.queryByText(/не может действовать\./)).toBeNull();
  });

  test("имя героя выводится только как текст", () => {
    const combat = combatWith("pc_boris", [
      heroFighter("pc_lira", "Лира"),
      heroFighter("pc_boris", "<img src=x onerror=alert(1)>Борин", {
        can_act: false,
      }),
    ]);

    const { container } = renderCombat({ combat });

    expect(container.querySelector("img")).toBeNull();
    expect(turnNote()).toHaveTextContent(
      "<img src=x onerror=alert(1)>Борин не может действовать.",
    );
  });
});

describe("боя в данных нет", () => {
  test("экран боя пуст: ни порядка хода, ни зон, ни поля", () => {
    const { container } = renderCombat({ combat: null });

    expect(container).toBeEmptyDOMElement();
  });
});
