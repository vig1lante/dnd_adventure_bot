import { fireEvent, render, screen } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import type { GameSummary, MyCharacter } from "../../api/types";
import { sessionToken, useGame } from "../../store/game";
import {
  fakeServer,
  gameSummary,
  lobbyPlayer,
  signInAs,
} from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import {
  OPTIONS,
  STORED_DETAILED,
  STORED_QUICK,
  holdRequests,
  mineDetailed,
  mineLegacy,
  mineQuick,
  mineRandom,
  previewHero,
} from "./fixtures";
import { ModePicker } from "./ModePicker";

const OPTIONS_ROUTE = "GET /api/content/creation-options";
const GAME_ROUTE = "GET /api/games/g1";
const MINE_ROUTE = "GET /api/games/g1/characters/mine";
const CREATE_ROUTE = "POST /api/games/g1/characters";

type Route = FakeReply | ((call: RecordedCall) => FakeReply);

const lobbyGame = (): GameSummary =>
  gameSummary({
    players: [
      lobbyPlayer({ user_id: "u1", is_host: true }),
      lobbyPlayer({ user_id: "u2", name: "Борис", has_character: true }),
    ],
  });

/** Ответ сервера, когда у игрока ещё нет героя. */
const NO_HERO: FakeReply = {
  status: 404,
  json: { detail: "У вас ещё нет героя в этой игре." },
};

interface Setup {
  routes?: Record<string, Route>;
  /** Что отдаёт `GET .../characters/mine`; не задан — героя нет (404). */
  hero?: MyCharacter;
  /** Не открывать экран игры: проверка экрана без выбранной игры. */
  noGame?: boolean;
  /** Держать запрос правил, пока тест его не отпустит. */
  holdOptions?: boolean;
  /** Держать запрос героя, пока тест его не отпустит. */
  holdMine?: boolean;
}

function renderPicker({
  routes = {},
  hero,
  noGame = false,
  holdOptions = false,
  holdMine = false,
}: Setup = {}) {
  signInAs({ id: "u1", name: "Вика" });
  if (!noGame) useGame.getState().show({ screen: "create", gameId: "g1" });
  const server = fakeServer({
    [OPTIONS_ROUTE]: { json: OPTIONS },
    [GAME_ROUTE]: { json: lobbyGame() },
    [MINE_ROUTE]: hero === undefined ? NO_HERO : { json: hero },
    ...routes,
  });
  const gated = holdRequests(server.fetch);
  const releaseOptions = holdOptions ? gated.hold("/creation-options") : null;
  const releaseMine = holdMine ? gated.hold("/characters/mine") : null;
  render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <ModePicker />
    </ApiContext>,
  );
  return { server, releaseOptions, releaseMine };
}

const button = (name: string) => screen.getByRole("button", { name });
const click = (name: string) => fireEvent.click(button(name));

const optionRequests = (server: { calls: RecordedCall[] }) =>
  server.calls.filter(
    (call) => `${call.method} ${call.path}` === OPTIONS_ROUTE,
  );

describe("выбор способа", () => {
  test("три способа и возврат в лобби", async () => {
    renderPicker();

    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
    for (const name of ["Подробно", "Быстро", "Случайно", "Назад в лобби"]) {
      expect(button(name)).toBeEnabled();
    }
  });

  test("у каждого способа есть пояснение", async () => {
    renderPicker();

    expect(await screen.findByText(/Выберите всё сами/)).toBeInTheDocument();
    expect(
      screen.getByText(/Имя и описание придумает мастер/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Судьба бросит кубики/)).toBeInTheDocument();
  });

  test("«Назад в лобби» открывает лобби этой игры", async () => {
    renderPicker();
    await screen.findByRole("heading", { name: "Создание героя" });

    click("Назад в лобби");

    expect(useGame.getState()).toMatchObject({ screen: "lobby", gameId: "g1" });
  });

  test("правила создания запрашиваются один раз, с токеном и без query", async () => {
    const { server } = renderPicker();
    await screen.findByRole("heading", { name: "Создание героя" });

    click("Быстро");
    await screen.findByRole("heading", { name: "Быстрый герой" });
    click("Назад");
    click("Случайно");
    await screen.findByRole("heading", { name: "Случайный герой" });

    expect(optionRequests(server)).toHaveLength(1);
    expect(optionRequests(server)[0].headers.get("Authorization")).toBe(
      "Bearer tok-u1",
    );
    expect(optionRequests(server)[0].url).toBe("/api/content/creation-options");
  });
});

const mineRequests = (server: { calls: RecordedCall[] }) =>
  server.calls.filter((call) => `${call.method} ${call.path}` === MINE_ROUTE);

const posts = (server: { calls: RecordedCall[] }, route: string) =>
  server.calls.filter((call) => `${call.method} ${call.path}` === route);

describe("свой герой: запрос и состояния", () => {
  test("запрос героя один: GET с токеном, без тела и query", async () => {
    const { server } = renderPicker({ hero: mineDetailed() });
    await screen.findByRole("heading", { name: "Изменить героя" });

    expect(mineRequests(server)).toHaveLength(1);
    const [call] = mineRequests(server);
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(call.body).toBeUndefined();
    expect(call.url).toBe("/api/games/g1/characters/mine");
  });

  test("пока герой не пришёл, способы не показываются: идёт загрузка", async () => {
    const { releaseMine } = renderPicker({ holdMine: true });

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Загружаем вашего героя",
    );
    expect(screen.queryByRole("button", { name: "Подробно" })).toBeNull();
    releaseMine?.();
    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
  });

  test("404 «героя ещё нет» — обычное создание: «Создание героя», три способа, без предупреждения", async () => {
    renderPicker();

    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
    for (const name of ["Подробно", "Быстро", "Случайно"]) {
      expect(button(name)).toBeEnabled();
    }
    expect(screen.queryByText(/заменит/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Изменить" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("сбой запроса героя: текст сервера, «Повторить» и три способа с пустыми формами", async () => {
    renderPicker({
      routes: {
        [MINE_ROUTE]: { status: 500, json: { detail: "Сервер сломался." } },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сервер сломался.",
    );
    expect(screen.getByText(/формы откроются пустыми/i)).toBeInTheDocument();
    expect(button("Повторить")).toBeEnabled();
    expect(button("Назад в лобби")).toBeEnabled();
    for (const name of ["Подробно", "Быстро", "Случайно"]) {
      expect(button(name)).toBeEnabled();
    }
    // Прежнего выбора не знаем, поэтому «Изменить» и «Создать заново» не предлагаются.
    expect(screen.queryByRole("button", { name: "Изменить" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Создать заново" })).toBeNull();
  });

  test("после сбоя героя быстрый способ открывается пустым", async () => {
    const { server } = renderPicker({
      routes: {
        [MINE_ROUTE]: { status: 500, json: { detail: "Сервер сломался." } },
      },
    });
    await screen.findByRole("alert");

    click("Быстро");

    await screen.findByRole("heading", { name: "Быстрый герой" });
    expect(screen.getByLabelText("Класс")).toHaveValue("");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue("");
    // Сообщение о сбое относится к стартовому экрану, у формы его нет.
    expect(screen.queryByText("Сервер сломался.")).toBeNull();
    expect(posts(server, CREATE_ROUTE)).toHaveLength(0);
  });

  test("«Повторить» после сбоя: герой пришёл — «Изменить героя» вместо сообщения", async () => {
    let attempts = 0;
    renderPicker({
      routes: {
        [MINE_ROUTE]: () =>
          ++attempts === 1
            ? { status: 500, json: { detail: "Сервер сломался." } }
            : { json: mineDetailed() },
      },
    });
    await screen.findByRole("alert");

    click("Повторить");

    expect(
      await screen.findByRole("heading", { name: "Изменить героя" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(button("Изменить")).toBeEnabled();
  });

  test("сведения об игре не пришли — экран не ломается, способы доступны", async () => {
    renderPicker({
      routes: { [GAME_ROUTE]: { status: 500, json: { detail: "Сбой." } } },
    });

    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(button("Случайно")).toBeEnabled();
  });
});

describe("изменить героя: прежний выбор", () => {
  /** Идёт по шагам подробной формы, ничего не меняя, до кнопки «Создать героя». */
  function walkToTheEnd() {
    // Предел нужен, чтобы закрытый шаг (неактивная «Далее») ронял тест, а не вешал его.
    for (let i = 0; i < 12; i++) {
      if (screen.queryByRole("button", { name: "Далее" }) === null) return;
      click("Далее");
    }
  }

  test("у героя с выбором — «Изменить героя», предупреждение о замене и два пути", async () => {
    renderPicker({ hero: mineDetailed() });

    expect(
      await screen.findByRole("heading", { name: "Изменить героя" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Новый заменит его/)).toHaveTextContent(
      "отметка «Готов» снимется",
    );
    expect(button("Изменить")).toBeEnabled();
    expect(button("Создать заново")).toBeEnabled();
    // Способы появятся только у «Создать заново».
    for (const name of ["Подробно", "Быстро", "Случайно"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  test("подпись «Изменить» называет прежний способ", async () => {
    renderPicker({ hero: mineDetailed() });
    await screen.findByRole("button", { name: "Изменить" });
    expect(screen.getByText(/прежним выбором/)).toHaveTextContent("Подробно");
  });

  test("detailed: «Изменить» открывает подробную форму с прежним выбором, отправка шлёт его", async () => {
    const { server } = renderPicker({
      hero: mineDetailed(),
      routes: {
        [CREATE_ROUTE]: {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Изменить героя" });

    click("Изменить");

    expect(
      await screen.findByRole("heading", { name: "Подробный герой" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Шаг 1 из 9/)).toHaveTextContent("Вид");
    expect(screen.getByRole("radio", { name: /Эльф/ })).toBeChecked();
    walkToTheEnd();
    expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
      "Родилась в порту.",
    );
    click("Создать героя");

    await vi.waitFor(() =>
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
      }),
    );
    expect(posts(server, CREATE_ROUTE)).toHaveLength(1);
    expect(posts(server, CREATE_ROUTE)[0].body).toEqual({
      mode: "detailed",
      choices: {
        ...STORED_DETAILED,
        // Пустые поля описания не отправляются: сервер подставляет те же пустые строки.
        persona: { concept: "Вор-неудачник", backstory: "Родилась в порту." },
      },
    });
  });

  test("detailed: правка прежнего выбора уходит на сервер вместо прежнего", async () => {
    const { server } = renderPicker({
      hero: mineDetailed(),
      routes: {
        [CREATE_ROUTE]: {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Изменить");
    await screen.findByRole("heading", { name: "Подробный герой" });
    walkToTheEnd();
    click("Назад"); // на шаг «Характер и внешность»
    fireEvent.change(screen.getByLabelText("Имя"), {
      target: { value: "Лирой" },
    });
    click("Далее");

    click("Создать героя");

    await vi.waitFor(() => expect(posts(server, CREATE_ROUTE)).toHaveLength(1));
    expect(posts(server, CREATE_ROUTE)[0].body).toMatchObject({
      mode: "detailed",
      choices: { name: "Лирой", class_id: "rogue" },
    });
  });

  test("quick: «Изменить» открывает быструю форму с прежними полями, отправка шлёт их", async () => {
    const { server } = renderPicker({
      hero: mineQuick({ ...STORED_QUICK, name: "Тень" }),
      routes: {
        [CREATE_ROUTE]: {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Изменить героя" });
    expect(screen.getByText(/прежним выбором/)).toHaveTextContent("Быстро");

    click("Изменить");

    expect(
      await screen.findByRole("heading", { name: "Быстрый герой" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Класс")).toHaveValue("rogue");
    expect(screen.getByLabelText("Вид")).toHaveValue("human");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue(
      "Вор-неудачник",
    );
    expect(screen.getByLabelText("Имя (необязательно)")).toHaveValue("Тень");
    click("Создать героя");

    await vi.waitFor(() => expect(posts(server, CREATE_ROUTE)).toHaveLength(1));
    expect(posts(server, CREATE_ROUTE)[0].body).toEqual({
      mode: "quick",
      class_id: "rogue",
      species: "human",
      concept: "Вор-неудачник",
      name: "Тень",
    });
  });

  test("quick без имени: поле имени пустое, имя в запрос не попадает", async () => {
    const { server } = renderPicker({
      hero: mineQuick(),
      routes: {
        [CREATE_ROUTE]: {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Изменить");
    await screen.findByRole("heading", { name: "Быстрый герой" });

    expect(screen.getByLabelText("Имя (необязательно)")).toHaveValue("");
    click("Создать героя");

    await vi.waitFor(() => expect(posts(server, CREATE_ROUTE)).toHaveLength(1));
    expect(posts(server, CREATE_ROUTE)[0].body).toEqual({
      mode: "quick",
      class_id: "rogue",
      species: "human",
      concept: "Вор-неудачник",
    });
  });

  test("«Назад» из формы с прежним выбором возвращает к «Изменить» и «Создать заново»", async () => {
    renderPicker({ hero: mineQuick() });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Изменить");
    await screen.findByRole("heading", { name: "Быстрый герой" });

    click("Назад");

    expect(
      await screen.findByRole("heading", { name: "Изменить героя" }),
    ).toBeInTheDocument();
    expect(button("Изменить")).toBeEnabled();
    expect(button("Создать заново")).toBeEnabled();
  });
});

describe("изменить героя: создать заново", () => {
  test("«Создать заново» — три способа, как у нового игрока", async () => {
    renderPicker({ hero: mineDetailed() });
    await screen.findByRole("heading", { name: "Изменить героя" });

    click("Создать заново");

    for (const name of ["Подробно", "Быстро", "Случайно"]) {
      expect(button(name)).toBeEnabled();
    }
    expect(screen.queryByRole("button", { name: "Изменить" })).toBeNull();
    // Предупреждение о замене не пропадает: новый герой всё равно заменит прежнего.
    expect(screen.getByText(/Новый заменит его/)).toBeInTheDocument();
  });

  test("подробный способ после «Создать заново» открывается пустым", async () => {
    renderPicker({ hero: mineDetailed() });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Создать заново");

    click("Подробно");

    await screen.findByRole("heading", { name: "Подробный герой" });
    expect(screen.getByText(/^Шаг 1 из 9/)).toHaveTextContent("Вид");
    expect(screen.queryByRole("radio", { checked: true })).toBeNull();
    expect(button("Далее")).toBeDisabled();
  });

  test("быстрый способ после «Создать заново» открывается пустым", async () => {
    renderPicker({ hero: mineQuick() });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Создать заново");

    click("Быстро");

    await screen.findByRole("heading", { name: "Быстрый герой" });
    expect(screen.getByLabelText("Класс")).toHaveValue("");
    expect(screen.getByLabelText("Вид")).toHaveValue("");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue("");
    expect(screen.getByLabelText("Имя (необязательно)")).toHaveValue("");
  });

  test("«Назад» в способах возвращает к выбору «Изменить» / «Создать заново»", async () => {
    renderPicker({ hero: mineDetailed() });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Создать заново");

    click("Назад");

    expect(button("Изменить")).toBeEnabled();
    expect(button("Создать заново")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Подробно" })).toBeNull();
  });

  test("после «Создать заново» подробный герой уходит обычным телом, без прежнего выбора", async () => {
    const { server } = renderPicker({
      hero: mineQuick(),
      routes: {
        [CREATE_ROUTE]: {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Изменить героя" });
    click("Создать заново");
    click("Быстро");
    await screen.findByRole("heading", { name: "Быстрый герой" });
    fireEvent.change(screen.getByLabelText("Класс"), {
      target: { value: "fighter" },
    });
    fireEvent.change(screen.getByLabelText("Вид"), {
      target: { value: "dwarf" },
    });
    fireEvent.change(screen.getByLabelText("Опиши героя одной строкой"), {
      target: { value: "Кузнец" },
    });

    click("Создать героя");

    await vi.waitFor(() => expect(posts(server, CREATE_ROUTE)).toHaveLength(1));
    expect(posts(server, CREATE_ROUTE)[0].body).toEqual({
      mode: "quick",
      class_id: "fighter",
      species: "dwarf",
      concept: "Кузнец",
    });
  });
});

describe("изменить героя: прежнего выбора нет", () => {
  test.each([
    ["случайный герой", mineRandom, /создан случайно/],
    ["герой из старой игры", mineLegacy, /создан до обновления/],
  ])("%s: пояснение, три способа и пустые формы", async (_name, hero, note) => {
    renderPicker({ hero });

    expect(
      await screen.findByRole("heading", { name: "Изменить героя" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Новый заменит его/)).toBeInTheDocument();
    expect(screen.getByText(note)).toHaveTextContent("формы откроются пустыми");
    expect(screen.queryByRole("button", { name: "Изменить" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Создать заново" })).toBeNull();
    for (const name of ["Подробно", "Быстро", "Случайно"]) {
      expect(button(name)).toBeEnabled();
    }
  });

  test.each([
    ["случайный герой", mineRandom],
    ["герой из старой игры", mineLegacy],
  ])("%s: быстрая форма пустая", async (_name, hero) => {
    renderPicker({ hero });
    await screen.findByRole("heading", { name: "Изменить героя" });

    click("Быстро");

    await screen.findByRole("heading", { name: "Быстрый герой" });
    expect(screen.getByLabelText("Класс")).toHaveValue("");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue("");
  });

  test("подробная форма пустая: ничего не выбрано", async () => {
    renderPicker({ hero: mineLegacy });
    await screen.findByRole("heading", { name: "Изменить героя" });

    click("Подробно");

    await screen.findByRole("heading", { name: "Подробный герой" });
    expect(screen.queryByRole("radio", { checked: true })).toBeNull();
    expect(button("Далее")).toBeDisabled();
  });

  test("без героя пояснения про прежний выбор нет", async () => {
    renderPicker();
    await screen.findByRole("heading", { name: "Создание героя" });

    expect(screen.queryByText(/формы откроются пустыми/)).toBeNull();
  });
});

describe("игра уже началась", () => {
  const started = (phase: GameSummary["phase"]) =>
    gameSummary({
      phase,
      players: [
        lobbyPlayer({
          user_id: "u1",
          is_host: true,
          has_character: true,
          ready: true,
        }),
      ],
    });
  const renderStarted = (phase: GameSummary["phase"]) =>
    renderPicker({ routes: { [GAME_ROUTE]: { json: started(phase) } } });

  test.each(["weaving", "playing", "paused", "finished"] as const)(
    "в фазе %s вместо способов — «Игра уже началась», а не отказ после десяти шагов",
    async (phase) => {
      renderStarted(phase);

      expect(
        await screen.findByRole("heading", { name: "Игра уже началась" }),
      ).toBeInTheDocument();
      for (const name of ["Подробно", "Быстро", "Случайно"]) {
        expect(screen.queryByRole("button", { name })).toBeNull();
      }
      expect(
        screen.queryByRole("heading", {
          name: /Создание героя|Изменить героя/,
        }),
      ).toBeNull();
    },
  );

  test("в начале игры ведёт в лобби: там экран вплетения", async () => {
    renderStarted("weaving");
    await screen.findByRole("heading", { name: "Игра уже началась" });

    click("В лобби");

    expect(useGame.getState()).toMatchObject({ screen: "lobby", gameId: "g1" });
  });

  test.each(["playing", "paused", "finished"] as const)(
    "в фазе %s ведёт к экрану игры",
    async (phase) => {
      renderStarted(phase);
      await screen.findByRole("heading", { name: "Игра уже началась" });

      click("К игре");

      expect(useGame.getState()).toMatchObject({
        screen: "game",
        gameId: "g1",
      });
    },
  );

  test("лишней кнопки «Назад в лобби» рядом нет: путь один", async () => {
    renderStarted("playing");
    await screen.findByRole("heading", { name: "Игра уже началась" });

    expect(screen.queryByRole("button", { name: "Назад в лобби" })).toBeNull();
  });

  test("в лобби способы на месте, как раньше", async () => {
    renderStarted("lobby");

    expect(
      await screen.findByRole("heading", { name: "Создание героя" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Игра уже началась")).toBeNull();
  });
});

describe("правила создания", () => {
  test("пока правила идут, выбранный способ показывает загрузку, затем форму", async () => {
    const { releaseOptions } = renderPicker({ holdOptions: true });
    await screen.findByRole("heading", { name: "Создание героя" });

    click("Быстро");

    expect(screen.getByRole("status")).toHaveTextContent("Загружаем правила");
    expect(screen.queryByRole("heading", { name: "Быстрый герой" })).toBeNull();
    releaseOptions?.();
    expect(
      await screen.findByRole("heading", { name: "Быстрый герой" }),
    ).toBeInTheDocument();
  });

  test("ошибка правил: текст, «Повторить» загружает заново", async () => {
    let attempts = 0;
    renderPicker({
      routes: {
        [OPTIONS_ROUTE]: () =>
          ++attempts === 1
            ? { status: 500, json: { detail: "Сервер сломался." } }
            : { json: OPTIONS },
      },
    });
    await screen.findByRole("heading", { name: "Создание героя" });

    click("Быстро");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сервер сломался.",
    );
    click("Повторить");

    expect(
      await screen.findByRole("heading", { name: "Быстрый герой" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("после создания", () => {
  test("быстрый герой создан — лобби этой игры", async () => {
    renderPicker({
      routes: {
        "POST /api/games/g1/characters": {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Создание героя" });
    click("Быстро");
    await screen.findByRole("heading", { name: "Быстрый герой" });
    fireEvent.change(screen.getByLabelText("Класс"), {
      target: { value: "rogue" },
    });
    fireEvent.change(screen.getByLabelText("Вид"), {
      target: { value: "elf" },
    });
    fireEvent.change(screen.getByLabelText("Опиши героя одной строкой"), {
      target: { value: "Акробат" },
    });

    click("Создать героя");

    await vi.waitFor(() =>
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
      }),
    );
  });

  test("случайный герой взят — лобби этой игры", async () => {
    renderPicker({
      routes: {
        "POST /api/games/g1/characters/random-preview": {
          json: { preview_id: "rp_aaaaaaaaaaaaaaaa", character: previewHero() },
        },
        "POST /api/games/g1/characters": {
          status: 201,
          json: { character: previewHero(), flavor_error: null },
        },
      },
    });
    await screen.findByRole("heading", { name: "Создание героя" });
    click("Случайно");
    await screen.findByRole("heading", { name: "Случайный герой" });
    click("Бросить судьбу");
    await screen.findByRole("region", { name: "Лист героя" });

    click("Беру");

    await vi.waitFor(() =>
      expect(useGame.getState()).toMatchObject({
        screen: "lobby",
        gameId: "g1",
      }),
    );
  });
});

describe("без выбранной игры", () => {
  test("сообщение и возврат к играм, запросов нет", () => {
    const { server } = renderPicker({ noGame: true });

    expect(screen.getByText("Игра не выбрана")).toBeInTheDocument();
    expect(server.calls).toHaveLength(0);
    click("К моим играм");
    expect(useGame.getState().screen).toBe("games");
  });
});
