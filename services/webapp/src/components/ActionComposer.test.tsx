import { act, fireEvent, render, screen } from "@testing-library/react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type { ActionBody, PlayerAction, TurnPhase } from "../api/types";
import { holdRequests } from "../screens/character/fixtures";
import { NO_LIVE, sessionToken, useGame } from "../store/game";
import type { GameView, LiveConnection } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { ActionComposer } from "./ActionComposer";
import { combatOf, gameView, myAction, phaseOf, sheetHero } from "./fixtures";

const DRAFT = "PUT /api/games/g1/action";
const SUBMIT = "POST /api/games/g1/action/submit";
const PASS = "POST /api/games/g1/action/pass";

// Подменяются только таймеры: React и промисы работают как обычно.
const FAKE_TIMERS: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval"];

/** Сервер хода в миниатюре: хранит строку хода игрока и отвечает как настоящий. */
function turnRoutes(initial: PlayerAction | null = null) {
  let row = initial;
  return {
    [DRAFT]: (call: RecordedCall): FakeReply => {
      const body = call.body as ActionBody;
      row = {
        text: body.text,
        secret: body.secret ?? false,
        inspiration: body.inspiration ?? false,
        status: "draft",
      };
      return { json: row };
    },
    [SUBMIT]: (): FakeReply => {
      if (row === null || row.text.trim() === "") {
        return {
          status: 422,
          json: { detail: "Напишите ход или нажмите «Пас»." },
        };
      }
      row = { ...row, status: "submitted" };
      return { json: row };
    },
    [PASS]: (): FakeReply => {
      row = { ...(row ?? myAction()), status: "pass" };
      return { json: row };
    },
  };
}

function Harness() {
  const view = useGame((state) => state.live.view);
  const turn = useGame((state) => state.live.turn);
  return view === null ? null : (
    <ActionComposer key={turn} gameId="g1" view={view} />
  );
}

interface Setup {
  view?: GameView;
  routes?: Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;
  /** Держать запрос на адрес с таким окончанием, пока тест не отпустит. */
  hold?: string;
}

function renderComposer({ view = gameView(), routes, hold }: Setup = {}) {
  signInAs({ id: "u1", name: "Вика" });
  const server = fakeServer({
    ...turnRoutes(view.action),
    ...routes,
  });
  const gated = holdRequests(server.fetch);
  const release = hold === undefined ? () => {} : gated.hold(hold);
  useGame.setState({ live: { ...NO_LIVE, view } });
  render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Harness />
    </ApiContext>,
  );
  return { server, release };
}

const field = () => screen.getByLabelText("Что делает твой персонаж?");
const button = (name: string) => screen.getByRole("button", { name });
const noButton = (name: string) =>
  expect(screen.queryByRole("button", { name })).toBeNull();
const type = (text: string) =>
  fireEvent.change(field(), { target: { value: text } });
const sent = (server: { calls: RecordedCall[] }) =>
  server.calls.map((call) => `${call.method} ${call.path}`);
const bodies = (server: { calls: RecordedCall[] }, route: string) =>
  server.calls
    .filter((call) => `${call.method} ${call.path}` === route)
    .map((call) => call.body);

/** Дать промисам запросов закончиться, не двигая время. */
const settle = async () => {
  for (let round = 0; round < 4; round += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
};
const wait = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
};
const press = async (name: string) => {
  fireEvent.click(button(name));
  await settle();
};

const viewIn = (turnPhase: TurnPhase | null, extra: Partial<GameView> = {}) =>
  gameView({ phase: phaseOf(turnPhase), ...extra });

describe("поле хода", () => {
  test("обсуждение: поле с подписью и подсказкой про диктовку, кнопки «Готов» и «Пас»", () => {
    renderComposer();

    expect(field()).toBeEnabled();
    expect(field()).toHaveAccessibleDescription(
      "Можно надиктовать с клавиатуры",
    );
    expect(button("Готов")).toBeDisabled(); // писать пока нечего
    expect(button("Пас")).toBeEnabled();
    noButton("Изменить");
  });

  test("сбор ходов: поле тоже открыто", () => {
    renderComposer({ view: viewIn("collecting") });

    expect(field()).toBeEnabled();
  });

  test.each([
    ["resolving", /Мастер думает/],
    ["dm_failed", /Мастер не справился/],
    ["camp", /Привал/],
    [null, /пока не принимаются/],
  ] as const)(
    "фаза хода %s: поле закрыто, причина названа",
    (turnPhase, note) => {
      renderComposer({ view: viewIn(turnPhase) });

      expect(field()).toBeDisabled();
      expect(button("Готов")).toBeDisabled();
      expect(button("Пас")).toBeDisabled();
      expect(screen.getByRole("status")).toHaveTextContent(note);
    },
  );

  test.each(["paused", "lobby", "weaving", "finished"] as const)(
    "фаза игры %s: поле закрыто",
    (phase) => {
      renderComposer({
        view: gameView({ phase: phaseOf("discussion", { phase }) }),
      });

      expect(field()).toBeDisabled();
      expect(button("Пас")).toBeDisabled();
    },
  );

  test("бой, свой ход: поле открыто", () => {
    renderComposer({
      view: viewIn("combat_wait", { combat: combatOf("pc_lira") }),
    });

    expect(field()).toBeEnabled();
  });

  test("бой, ход скелета: поле закрыто, сказано, кто ходит", () => {
    renderComposer({
      view: viewIn("combat_wait", { combat: combatOf("skeleton-1") }),
    });

    expect(field()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Ходит Скелет.");
  });

  test("в закрытом поле виден уже написанный черновик", () => {
    renderComposer({
      view: viewIn("resolving", {
        action: myAction({ text: "Осматриваю дверь" }),
      }),
    });

    expect(field()).toHaveValue("Осматриваю дверь");
    expect(field()).toBeDisabled();
  });

  test("черновик из снимка подставлен в поле", () => {
    renderComposer({
      view: gameView({ action: myAction({ text: "Осматриваю дверь" }) }),
    });

    expect(field()).toHaveValue("Осматриваю дверь");
  });

  test("у игрока нет героя — поля нет", () => {
    const { server } = renderComposer({ view: gameView({ character: null }) });

    expect(screen.queryByLabelText("Что делает твой персонаж?")).toBeNull();
    expect(server.calls).toHaveLength(0);
  });

  test("поле не длиннее, чем принимает сервер (3000 знаков)", () => {
    renderComposer();

    expect(field()).toHaveAttribute("maxlength", "3000");
  });
});

describe("«Тайно» и «Вдохновение»", () => {
  test("«Тайно» всегда на месте и выключено", () => {
    renderComposer();

    expect(screen.getByRole("switch", { name: "Тайно" })).not.toBeChecked();
  });

  test("«Вдохновение» есть только у героя с вдохновением", () => {
    renderComposer();
    expect(screen.queryByRole("switch", { name: "Вдохновение" })).toBeNull();
  });

  test("с вдохновением переключатель появляется", () => {
    renderComposer({
      view: gameView({ character: sheetHero({ inspiration: true }) }),
    });

    expect(
      screen.getByRole("switch", { name: "Вдохновение" }),
    ).not.toBeChecked();
  });

  test("переключатели переключаются и уходят в черновик", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { server } = renderComposer({
      view: gameView({ character: sheetHero({ inspiration: true }) }),
    });

    fireEvent.click(screen.getByRole("switch", { name: "Тайно" }));
    fireEvent.click(screen.getByRole("switch", { name: "Вдохновение" }));
    type("Подкрадываюсь");
    await wait(800);

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Подкрадываюсь", secret: true, inspiration: true },
    ]);
  });

  test("вдохновения у героя уже нет — на сервер оно не уходит, даже если было в черновике", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { server } = renderComposer({
      view: gameView({
        action: myAction({ text: "Старый ход", inspiration: true }),
      }),
    });

    type("Новый ход");
    await wait(800);

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Новый ход", secret: false, inspiration: false },
    ]);
  });
});

describe("автосохранение черновика", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("через 800 мс после ввода уходит один PUT, раньше — ничего", async () => {
    const { server } = renderComposer();

    type("Осматриваю дверь");
    await wait(799);
    expect(sent(server)).toEqual([]);
    await wait(1);

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Осматриваю дверь", secret: false, inspiration: false },
    ]);
  });

  test("каждый новый ввод откладывает сохранение: уходит только последний текст", async () => {
    const { server } = renderComposer();

    type("Осм");
    await wait(500);
    type("Осматриваю");
    await wait(500);
    type("Осматриваю дверь");
    await wait(799);
    expect(sent(server)).toEqual([]);
    await wait(1);

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Осматриваю дверь", secret: false, inspiration: false },
    ]);
  });

  test("ответ сервера кладётся в игру: ход игрока обновлён из REST", async () => {
    renderComposer();

    type("Осматриваю дверь");
    await wait(800);

    expect(useGame.getState().live.view?.action).toEqual({
      text: "Осматриваю дверь",
      secret: false,
      inspiration: false,
      status: "draft",
    });
  });

  test("сохранённый и неизменённый черновик повторно не отправляется", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await wait(800);

    await wait(10_000);

    expect(bodies(server, DRAFT)).toHaveLength(1);
  });

  test("возврат к уже сохранённому тексту нового запроса не требует", async () => {
    const { server } = renderComposer({
      view: gameView({ action: myAction({ text: "Осматриваю дверь" }) }),
    });

    type("Осматриваю дверь!");
    type("Осматриваю дверь");
    await wait(800);

    expect(bodies(server, DRAFT)).toHaveLength(0);
  });

  test("без ввода никаких запросов нет", async () => {
    const { server } = renderComposer();

    await wait(5000);

    expect(sent(server)).toEqual([]);
  });

  test("правка, пока идёт сохранение, уходит следом и не теряется", async () => {
    const { server, release } = renderComposer({ hold: "/action" });
    type("Один");
    await wait(800);
    expect(bodies(server, DRAFT)).toHaveLength(0); // запрос ещё в пути

    type("Один два");
    await wait(800);
    release();
    await settle();
    await wait(800);

    expect(
      bodies(server, DRAFT).map((body) => (body as ActionBody).text),
    ).toEqual(["Один", "Один два"]);
    expect(field()).toHaveValue("Один два");
  });

  test("отказ сохранения показан, ввод остаётся, следующая правка пробует снова", async () => {
    let attempts = 0;
    const { server } = renderComposer({
      routes: {
        [DRAFT]: (call) =>
          ++attempts === 1
            ? { status: 409, json: { detail: "Сбор ходов закрыт." } }
            : { json: { ...(call.body as object), status: "draft" } },
      },
    });

    type("Осматриваю");
    await wait(800);
    expect(screen.getByRole("alert")).toHaveTextContent("Сбор ходов закрыт.");
    expect(field()).toHaveValue("Осматриваю");

    type("Осматриваю дверь");
    await wait(800);

    expect(bodies(server, DRAFT)).toHaveLength(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("ход закрыт, пока текст ждал сохранения: черновик на сервер уже не уходит", async () => {
    const { server } = renderComposer();
    type("Не успел");

    act(() =>
      useGame
        .getState()
        .receive({ type: "phase", phase: phaseOf("resolving") }),
    );
    await wait(5000);

    expect(sent(server)).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("при уходе вкладки в фон несохранённый черновик отправляется сразу", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    Reflect.deleteProperty(document, "visibilityState");

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Осматриваю дверь", secret: false, inspiration: false },
    ]);
  });
});

describe("возврат на вкладку", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("вкладка снова видна — набранное не уходит раньше срока автосохранения", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    Reflect.deleteProperty(document, "visibilityState");

    expect(bodies(server, DRAFT)).toEqual([]);
    await wait(800);
    expect(bodies(server, DRAFT)).toHaveLength(1);
  });
});

describe("«Готов»", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("несохранённый черновик досылается перед сдачей: сначала PUT, потом submit", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");

    await press("Готов");

    expect(sent(server)).toEqual([DRAFT, SUBMIT]);
    expect(bodies(server, DRAFT)).toEqual([
      { text: "Осматриваю дверь", secret: false, inspiration: false },
    ]);
  });

  test("отложенное автосохранение после сдачи не срабатывает: ход не вернётся в черновик", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await press("Готов");

    await wait(5000);

    expect(sent(server)).toEqual([DRAFT, SUBMIT]);
  });

  test("уже сохранённый черновик не отправляется второй раз: только submit", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await wait(800);

    await press("Готов");

    expect(sent(server)).toEqual([DRAFT, SUBMIT]);
    expect(bodies(server, DRAFT)).toHaveLength(1);
  });

  test("дописали последние слова за секунду до «Готов» — на сервере они тоже", async () => {
    const { server } = renderComposer();
    type("Осматриваю");
    await wait(800);
    type("Осматриваю дверь");

    await press("Готов");

    expect(
      bodies(server, DRAFT).map((body) => (body as ActionBody).text),
    ).toEqual(["Осматриваю", "Осматриваю дверь"]);
    expect(sent(server).at(-1)).toBe(SUBMIT);
  });

  test("пока идёт автосохранение, «Готов» ждёт его и не обгоняет", async () => {
    const { server, release } = renderComposer({ hold: "/action" });
    type("Осматриваю дверь");
    await wait(800);

    fireEvent.click(button("Готов"));
    await settle();
    expect(sent(server)).not.toContain(SUBMIT);
    release();
    await settle();
    await settle();

    expect(sent(server)).toEqual([DRAFT, SUBMIT]);
  });

  test("без текста «Готов» неактивен, пробелы не считаются", () => {
    renderComposer();
    expect(button("Готов")).toBeDisabled();

    type("   ");

    expect(button("Готов")).toBeDisabled();
  });

  test("после сдачи — «Ход сдан», текст и «Изменить»; поля ввода нет", async () => {
    renderComposer();
    type("Осматриваю дверь");

    await press("Готов");

    expect(screen.getByText("Ход сдан")).toBeInTheDocument();
    expect(screen.getByText("Осматриваю дверь")).toBeInTheDocument();
    expect(button("Изменить")).toBeEnabled();
    expect(screen.queryByLabelText("Что делает твой персонаж?")).toBeNull();
    noButton("Готов");
    noButton("Пас");
    expect(useGame.getState().live.view?.action?.status).toBe("submitted");
  });

  test("сданный тайный ход помечен «Тайно»", async () => {
    renderComposer();
    fireEvent.click(screen.getByRole("switch", { name: "Тайно" }));
    type("Прячу письмо");

    await press("Готов");

    expect(screen.getByText("Тайно")).toBeInTheDocument();
  });

  test("отказ сервера при сдаче показан, поле остаётся", async () => {
    renderComposer({
      routes: {
        [SUBMIT]: { status: 409, json: { detail: "Сбор ходов закрыт." } },
      },
    });
    type("Осматриваю дверь");

    await press("Готов");

    expect(screen.getByRole("alert")).toHaveTextContent("Сбор ходов закрыт.");
    expect(field()).toHaveValue("Осматриваю дверь");
  });

  test("если черновик не сохранился, сдача не отправляется", async () => {
    const { server } = renderComposer({
      routes: {
        [DRAFT]: { status: 409, json: { detail: "Сбор ходов закрыт." } },
      },
    });
    type("Осматриваю дверь");

    await press("Готов");

    expect(sent(server)).toEqual([DRAFT]);
    expect(screen.getByRole("alert")).toHaveTextContent("Сбор ходов закрыт.");
  });

  test("два нажатия подряд до перерисовки шлют одну сдачу", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await wait(800);

    act(() => {
      button("Готов").click();
      button("Готов").click();
    });
    await settle();

    expect(sent(server).filter((call) => call === SUBMIT)).toHaveLength(1);
  });

  test("повторное нажатие, пока идёт сдача, ничего не шлёт", async () => {
    const { server, release } = renderComposer({ hold: "/submit" });
    type("Осматриваю дверь");
    await wait(800);

    fireEvent.click(button("Готов"));
    await settle();
    expect(button("Готов")).toBeDisabled();
    fireEvent.click(button("Готов"));
    release();
    await settle();

    expect(sent(server).filter((call) => call === SUBMIT)).toHaveLength(1);
  });
});

describe("«Пас»", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("без текста: сразу пас, без черновика", async () => {
    const { server } = renderComposer();

    await press("Пас");

    expect(sent(server)).toEqual([PASS]);
    expect(screen.getByText("Вы пасуете")).toBeInTheDocument();
    expect(button("Изменить")).toBeEnabled();
    expect(useGame.getState().live.view?.action?.status).toBe("pass");
  });

  test("с несохранённым текстом: черновик уходит перед пасом, чтобы «Изменить» вернул его", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");

    await press("Пас");

    expect(sent(server)).toEqual([DRAFT, PASS]);
  });

  test("отложенное автосохранение пас не отменяет: поздний PUT вернул бы ход в черновик", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await press("Пас");

    await wait(5000);

    expect(sent(server)).toEqual([DRAFT, PASS]);
    expect(screen.getByText("Вы пасуете")).toBeInTheDocument();
  });

  test("отказ сервера при пасе показан", async () => {
    renderComposer({
      routes: {
        [PASS]: { status: 409, json: { detail: "Сбор ходов закрыт." } },
      },
    });

    await press("Пас");

    expect(screen.getByRole("alert")).toHaveTextContent("Сбор ходов закрыт.");
    expect(button("Пас")).toBeEnabled();
  });
});

describe("«Изменить»", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("сданный ход возвращается в черновик с прежним текстом", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    await press("Готов");

    await press("Изменить");

    expect(field()).toHaveValue("Осматриваю дверь");
    expect(field()).toBeEnabled();
    expect(sent(server).slice(-1)).toEqual([DRAFT]);
    expect(bodies(server, DRAFT).at(-1)).toEqual({
      text: "Осматриваю дверь",
      secret: false,
      inspiration: false,
    });
    expect(useGame.getState().live.view?.action?.status).toBe("draft");
    expect(button("Готов")).toBeEnabled();
  });

  test("после паса «Изменить» тоже возвращает в черновик", async () => {
    renderComposer();
    type("Осматриваю дверь");
    await press("Пас");

    await press("Изменить");

    expect(field()).toHaveValue("Осматриваю дверь");
    expect(button("Пас")).toBeEnabled();
  });

  test("из снимка со сданным ходом можно изменить его, не перепечатывая", async () => {
    const { server } = renderComposer({
      view: gameView({
        action: myAction({ text: "Осматриваю дверь", status: "submitted" }),
      }),
    });
    expect(screen.getByText("Ход сдан")).toBeInTheDocument();

    await press("Изменить");

    expect(field()).toHaveValue("Осматриваю дверь");
    expect(sent(server)).toEqual([DRAFT]);
  });

  test("отказ сервера: остаётся сданный ход, ошибка показана", async () => {
    renderComposer({
      view: gameView({
        action: myAction({ text: "Осматриваю дверь", status: "submitted" }),
      }),
      routes: {
        [DRAFT]: { status: 409, json: { detail: "Сбор ходов закрыт." } },
      },
    });

    await press("Изменить");

    expect(screen.getByRole("alert")).toHaveTextContent("Сбор ходов закрыт.");
    expect(screen.getByText("Ход сдан")).toBeInTheDocument();
  });

  test.each(["auto_submitted", "auto_pass"] as const)(
    "ход закрыт сервером (%s): «Изменить» нет",
    (status) => {
      renderComposer({
        view: gameView({ action: myAction({ text: "Ход", status }) }),
      });

      noButton("Изменить");
    },
  );

  test("пока мастер думает, у сданного хода «Изменить» нет", () => {
    renderComposer({
      view: viewIn("resolving", {
        action: myAction({ text: "Ход", status: "submitted" }),
      }),
    });

    expect(screen.getByText("Ход сдан")).toBeInTheDocument();
    noButton("Изменить");
  });
});

describe("новый ход", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  const resolve = (...next: TurnPhase[]) => {
    for (const turnPhase of next) {
      act(() =>
        useGame
          .getState()
          .receive({ type: "phase", phase: phaseOf(turnPhase) }),
      );
    }
  };

  test("мастер закончил — поле снова пустое, а прежний сданный ход не висит", async () => {
    renderComposer();
    type("Осматриваю дверь");
    await press("Готов");
    expect(screen.getByText("Ход сдан")).toBeInTheDocument();

    resolve("resolving", "discussion");

    expect(field()).toHaveValue("");
    expect(field()).toBeEnabled();
    expect(screen.queryByText("Ход сдан")).toBeNull();
  });

  test("недописанный текст прежнего хода в новый не попадает и не уходит на сервер", async () => {
    const { server } = renderComposer();
    type("Не успел дописать");

    resolve("resolving", "discussion");
    await wait(5000);

    expect(field()).toHaveValue("");
    expect(sent(server)).toEqual([]);
  });

  test("сбой мастера и «Повторить»: сданный ход остаётся виден, пока не пойдёт новый", async () => {
    renderComposer();
    type("Осматриваю дверь");
    await press("Готов");

    resolve("resolving", "dm_failed");
    expect(screen.getByText("Осматриваю дверь")).toBeInTheDocument();
    resolve("resolving", "discussion");

    expect(field()).toHaveValue("");
  });
});

describe("запоздалый ответ прежнего хода", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  const advanceTurn = () => {
    for (const turnPhase of ["resolving", "discussion"] as const) {
      act(() =>
        useGame
          .getState()
          .receive({ type: "phase", phase: phaseOf(turnPhase) }),
      );
    }
  };

  test("сохранение, ответ на которое пришёл уже в новом ходу, нового хода не касается", async () => {
    const { release } = renderComposer({ hold: "/action" });
    type("Осматриваю дверь");
    await wait(800); // PUT ушёл и висит

    advanceTurn();
    release();
    await settle();

    expect(useGame.getState().live.view?.action).toBeNull();
    expect(field()).toHaveValue("");
  });

  test("сдача, ответ на которую пришёл уже в новом ходу, тоже не применяется", async () => {
    const { release } = renderComposer({ hold: "/submit" });
    type("Осматриваю дверь");
    fireEvent.click(button("Готов"));
    await settle(); // PUT прошёл, submit висит

    advanceTurn();
    release();
    await settle();

    expect(useGame.getState().live.view?.action).toBeNull();
    expect(screen.queryByText("Ход сдан")).toBeNull();
  });
});

describe("«Изменить»: запоздалый ответ и вдохновение", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  const submitted = (extra: Partial<PlayerAction> = {}) =>
    myAction({ text: "Осматриваю дверь", status: "submitted", ...extra });

  test("ответ на «Изменить», пришедший уже в новом ходу, нового хода не касается", async () => {
    const { release } = renderComposer({
      view: gameView({ action: submitted() }),
      hold: "/action",
    });
    fireEvent.click(button("Изменить"));
    await settle(); // PUT ушёл и висит

    for (const turnPhase of ["resolving", "discussion"] as const) {
      act(() =>
        useGame
          .getState()
          .receive({ type: "phase", phase: phaseOf(turnPhase) }),
      );
    }
    release();
    await settle();

    expect(useGame.getState().live.view?.action).toBeNull();
    expect(field()).toHaveValue("");
  });

  test.each([
    ["в сданном ходе вдохновения не было, у героя оно есть", false, true],
    ["в сданном ходе оно было, у героя его уже нет", true, false],
  ])(
    "вдохновение при «Изменить» не берётся из воздуха: %s",
    async (_name, inRow, onHero) => {
      const { server } = renderComposer({
        view: gameView({
          character: sheetHero({ inspiration: onHero }),
          action: submitted({ inspiration: inRow }),
        }),
      });

      await press("Изменить");

      expect(bodies(server, DRAFT)).toEqual([
        { text: "Осматриваю дверь", secret: false, inspiration: false },
      ]);
    },
  );
});

describe("текст только как текст", () => {
  const hostile = '<img src=x onerror="alert(1)">';

  test("в поле и в сданном ходе разметка остаётся буквами", async () => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
    const { server } = renderComposer();
    type(hostile);

    await press("Готов");

    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(hostile)).toBeInTheDocument();
    expect(bodies(server, DRAFT)).toEqual([
      { text: hostile, secret: false, inspiration: false },
    ]);
  });
});

describe("вне фазы ничего не отправляется", () => {
  test("в закрытом поле кнопки не шлют запросов", () => {
    const { server } = renderComposer({ view: viewIn("resolving") });

    fireEvent.click(button("Пас"));
    fireEvent.click(button("Готов"));

    expect(server.calls).toHaveLength(0);
  });
});

describe("связь с игрой", () => {
  const LOST = /^Нет связи с игрой/;
  const setConnection = (connection: LiveConnection) =>
    act(() => useGame.setState({ connection }));

  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_TIMERS });
  });

  test("обрыв: поле, переключатели и кнопки закрыты, причина названа", () => {
    renderComposer({
      view: gameView({ character: sheetHero({ inspiration: true }) }),
    });
    expect(field()).toBeEnabled();

    setConnection("reconnecting");

    expect(field()).toBeDisabled();
    expect(screen.getByRole("switch", { name: /Тайно/ })).toBeDisabled();
    expect(screen.getByRole("switch", { name: /Вдохновение/ })).toBeDisabled();
    expect(button("Готов")).toBeDisabled();
    expect(button("Пас")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(LOST);
  });

  test("набранный текст при обрыве не пропадает, видна пометка о связи", () => {
    renderComposer();
    type("Осматриваю дверь");

    setConnection("reconnecting");

    expect(field()).toHaveValue("Осматриваю дверь");
    expect(field()).toBeDisabled();
  });

  test("связь вернулась — всё возвращается само: поле открыто, текст на месте, пометки нет", () => {
    renderComposer();
    type("Осматриваю дверь");
    setConnection("reconnecting");
    expect(field()).toBeDisabled();

    setConnection("open");

    expect(field()).toBeEnabled();
    expect(field()).toHaveValue("Осматриваю дверь");
    expect(button("Готов")).toBeEnabled();
    expect(button("Пас")).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("обрыв закрывает поле и в свой ход в бою", () => {
    renderComposer({
      view: viewIn("combat_wait", { combat: combatOf("pc_lira") }),
    });
    expect(field()).toBeEnabled();

    setConnection("reconnecting");

    expect(field()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(LOST);
  });

  test("обрыв, а фаза у клиента закрыта: причина — связь", () => {
    renderComposer({ view: viewIn("resolving") });

    setConnection("reconnecting");

    expect(screen.getByRole("status")).toHaveTextContent(LOST);
  });

  test("при обрыве кнопки не шлют запросов", async () => {
    const { server } = renderComposer({
      view: gameView({ action: myAction({ text: "Осматриваю дверь" }) }),
    });
    expect(button("Готов")).toBeEnabled();
    setConnection("reconnecting");

    fireEvent.click(button("Пас"));
    fireEvent.click(button("Готов"));
    await wait(5000);

    expect(server.calls).toHaveLength(0);
  });

  test("набранное перед самым обрывом всё равно уходит черновиком: терять его незачем", async () => {
    const { server } = renderComposer();
    type("Осматриваю дверь");
    setConnection("reconnecting");

    await wait(800);

    expect(bodies(server, DRAFT)).toEqual([
      { text: "Осматриваю дверь", secret: false, inspiration: false },
    ]);
  });

  test("сданный ход при обрыве: «Изменить» не предлагается, после возврата связи — снова", () => {
    renderComposer({
      view: gameView({
        action: myAction({ text: "Осматриваю дверь", status: "submitted" }),
      }),
    });
    button("Изменить");

    setConnection("reconnecting");
    noButton("Изменить");
    expect(screen.getByText("Осматриваю дверь")).toBeInTheDocument();

    setConnection("open");
    button("Изменить");
  });

  test.each(["idle", "connecting"] as const)(
    "связь %s — не обрыв: поле открыто по фазе",
    (connection) => {
      renderComposer();

      setConnection(connection);

      expect(field()).toBeEnabled();
      expect(screen.queryByRole("status")).toBeNull();
    },
  );

  test("пометка о связи — обычный текст", () => {
    renderComposer();

    setConnection("reconnecting");

    expect(screen.getByRole("status").children).toHaveLength(0);
  });
});
