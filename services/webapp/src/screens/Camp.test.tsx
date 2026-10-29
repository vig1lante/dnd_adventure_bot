import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type { FeedItem, RestView } from "../api/types";
import { CONNECTION_LOST_NOTE } from "../components/access";
import {
  feedEntry,
  gameView,
  phaseOf,
  sheetHero,
} from "../components/fixtures";
import { holdRequests } from "./character/fixtures";
import { NO_LIVE, sessionToken, useGame } from "../store/game";
import type { GameView, LiveConnection } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { Camp } from "./Camp";

const LINE = "POST /api/games/g1/camp/line";
const FIELD = "Сказать у костра";

const narration = (id: number, text: string) =>
  feedEntry(id, { kind: "narration", text });

/** Реплика у костра, как её присылает сервер: автор — в `data`. */
const spoken = (
  id: number,
  text: string,
  actor: { id: string; name: string } = { id: "pc_lira", name: "Лира" },
) =>
  feedEntry(id, {
    kind: "camp_line",
    text,
    data: { actor_id: actor.id, actor: actor.name },
  });

const BORIN = { id: "pc_boris", name: "Борин" };

const OTHER_CAMP_AND_THIS: FeedItem[] = [
  narration(1, "Первый привал: огонь едва тлеет."),
  spoken(2, "Старая реплика с первого привала."),
  narration(3, "Вы разбиваете лагерь у реки.\n\nСтемнело, ветер стих."),
  feedEntry(4, { kind: "system", text: "Начался привал." }),
  spoken(5, "Подкину дров.", BORIN),
  spoken(6, "Я постою в дозоре."),
];

interface Setup {
  feed?: FeedItem[];
  view?: Partial<GameView>;
  routes?: Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>;
  /** Держать запрос на адрес с таким окончанием, пока тест не отпустит. */
  hold?: string;
}

function Harness() {
  const view = useGame((state) => state.live.view);
  return view === null ? null : <Camp gameId="g1" view={view} />;
}

/** Привал глазами игрока Лиры (`u1`, герой `pc_lira`); ответ сервера на реплику получает следующий id. */
function renderCamp({
  feed = OTHER_CAMP_AND_THIS,
  view = {},
  routes,
  hold,
}: Setup = {}) {
  signInAs({ id: "u1", name: "Вика" });
  let nextId = 100;
  const server = fakeServer({
    [LINE]: (call): FakeReply => ({
      json: spoken(nextId++, (call.body as { text: string }).text),
    }),
    ...routes,
  });
  const gated = holdRequests(server.fetch);
  const release = hold === undefined ? () => {} : gated.hold(hold);
  useGame.setState({
    live: {
      ...NO_LIVE,
      view: gameView({
        phase: phaseOf("camp", { deadline_at: "2026-10-20T12:05:00+00:00" }),
        feed,
        ...view,
      }),
    },
  });
  const result = render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Harness />
    </ApiContext>,
  );
  return { server, release, container: result.container };
}

const field = () => screen.getByLabelText(FIELD);
const say = () => screen.getByRole("button", { name: "Сказать" });
const type = (text: string) =>
  fireEvent.change(field(), { target: { value: text } });
const lines = () =>
  within(screen.getByRole("list", { name: "Реплики у костра" })).getAllByRole(
    "listitem",
  );
const serve = (item: FeedItem) =>
  act(() => useGame.getState().receive({ type: "feed_item", item }));

describe("темы от мастера", () => {
  test("показан последний рассказ мастера, прежние — нет", () => {
    renderCamp();

    expect(
      screen.getByRole("heading", { name: "Последний рассказ мастера" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Вы разбиваете лагерь у реки.")).toBeVisible();
    expect(screen.getByText("Стемнело, ветер стих.")).toBeVisible();
    expect(screen.queryByText(/Первый привал/)).toBeNull();
  });

  test("привал без рассказа: темы нет, сказать можно", () => {
    renderCamp({ feed: [] });

    expect(screen.queryByText(/Последний рассказ/)).toBeNull();
    expect(field()).toBeEnabled();
  });

  test("реплики есть, а рассказа мастера в ленте нет: темы нет, реплики на месте", () => {
    renderCamp({ feed: [spoken(1, "Подкину дров.")] });

    expect(screen.queryByText(/Последний рассказ/)).toBeNull();
    const lines = screen.getByRole("list", { name: "Реплики у костра" });
    expect(within(lines).getByText(/Подкину дров/)).toBeVisible();
  });

  test("рассказ первым в ленте — он и есть тема, реплики идут после него", () => {
    renderCamp({
      feed: [narration(1, "Вы разбиваете лагерь."), spoken(2, "Подкину дров.")],
    });

    expect(
      screen.getByRole("heading", { name: "Последний рассказ мастера" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Вы разбиваете лагерь.")).toBeVisible();
    const lines = screen.getByRole("list", { name: "Реплики у костра" });
    expect(within(lines).getAllByRole("listitem")).toHaveLength(1);
  });

  test("рассказ выводится только как текст", () => {
    const { container } = renderCamp({
      feed: [narration(1, '<img src=x onerror="alert(1)">Лагерь')],
    });

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x/)).toBeInTheDocument();
  });
});

describe("реплики у костра", () => {
  test("видны реплики этого привала по порядку; чужой привал и не-реплики — нет", () => {
    renderCamp();

    expect(lines().map((line) => line.textContent)).toEqual([
      "Борин: «Подкину дров.»",
      "Лира: «Я постою в дозоре.»",
    ]);
    expect(screen.queryByText(/Старая реплика/)).toBeNull();
    expect(screen.queryByText(/Начался привал/)).toBeNull();
  });

  test("реплик ещё нет — тихая подсказка вместо пустого списка", () => {
    renderCamp({ feed: [narration(1, "Лагерь разбит.")] });

    expect(screen.queryByRole("list", { name: "Реплики у костра" })).toBeNull();
    expect(screen.getByText(/Пока тихо/)).toBeInTheDocument();
  });

  test("реплика другого игрока появляется из события", () => {
    renderCamp();

    serve(spoken(7, "Кто-нибудь слышит шорох?", BORIN));

    expect(lines().at(-1)).toHaveTextContent(
      "Борин: «Кто-нибудь слышит шорох?»",
    );
    expect(lines()).toHaveLength(3);
  });

  test("повтор события не дублирует реплику", () => {
    renderCamp();

    serve(spoken(7, "Кто-нибудь слышит шорох?", BORIN));
    serve(spoken(7, "Кто-нибудь слышит шорох?", BORIN));

    expect(lines()).toHaveLength(3);
  });

  test("реплики выводятся только как текст", () => {
    const { container } = renderCamp({
      feed: [
        narration(1, "Лагерь."),
        spoken(2, "<img src=x onerror=alert(1)> привет"),
      ],
    });

    expect(container.querySelector("img")).toBeNull();
    expect(lines()[0]).toHaveTextContent("<img src=x onerror=alert(1)> привет");
  });
});

describe("«Сказать у костра»", () => {
  test("поле с подписью, подсказкой и пределом; «Сказать» ждёт текста", () => {
    renderCamp();

    expect(field()).toBeEnabled();
    expect(field()).toHaveAttribute("maxlength", "400");
    expect(field()).toHaveAccessibleDescription(/Реплику услышат все/);
    expect(say()).toBeDisabled();
    type("   ");
    expect(say()).toBeDisabled();
    type("Греюсь у огня");
    expect(say()).toBeEnabled();
  });

  test.each([
    ["short", /ответит на неё в конце привала/],
    ["long", /ответит на неё утром/],
  ] as const)(
    "подсказка у поля по виду привала: %s — когда ответит мастер",
    (kind, hint) => {
      renderCamp({ view: { rest: restOf(kind) } });

      expect(field()).toHaveAccessibleDescription(hint);
    },
  );

  test("вид привала ещё не пришёл — подсказка не обещает «утро»", () => {
    renderCamp({ view: { rest: null } });

    expect(field()).toHaveAccessibleDescription(/в конце привала/);
    expect(field()).not.toHaveAccessibleDescription(/утром/);
  });

  test("счётчик знаков и реплик героя", () => {
    renderCamp();

    expect(screen.getByText(/Знаков: 0 из 400/)).toBeInTheDocument();
    expect(screen.getByText(/Ваших реплик: 1 из 10/)).toBeInTheDocument();
    type("Греюсь");
    expect(screen.getByText(/Знаков: 6 из 400/)).toBeInTheDocument();
  });

  test("реплика уходит без лишних пробелов, поле очищается, реплика видна один раз", async () => {
    const { server } = renderCamp();
    type("  Греюсь у огня  ");

    fireEvent.click(say());

    await vi.waitFor(() => expect(field()).toHaveValue(""));
    expect(server.calls).toHaveLength(1);
    expect(`${server.calls[0].method} ${server.calls[0].path}`).toBe(LINE);
    expect(server.calls[0].body).toEqual({ text: "Греюсь у огня" });
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(lines().at(-1)).toHaveTextContent("Лира: «Греюсь у огня»");

    // То же сообщение приходит и событием по сокету: дубля быть не должно.
    serve(spoken(100, "Греюсь у огня"));
    expect(lines()).toHaveLength(3);
  });

  test("отказ сервера (предел реплик) показан его текстом, написанное не пропадает", async () => {
    const limit = "Вы уже сказали у костра достаточно: подождите утра.";
    renderCamp({
      routes: { [LINE]: { status: 409, json: { detail: limit } } },
    });
    type("Ещё одна реплика");

    fireEvent.click(say());

    expect(await screen.findByRole("alert")).toHaveTextContent(limit);
    expect(field()).toHaveValue("Ещё одна реплика");
    expect(say()).toBeEnabled();
    expect(lines()).toHaveLength(2);
  });

  test("привал уже кончился: ответ сервера показан, игра идёт дальше", async () => {
    renderCamp({
      routes: {
        [LINE]: {
          status: 409,
          json: {
            detail: "Сейчас нет привала: реплики у костра не принимаются.",
          },
        },
      },
    });
    type("Поздно");

    fireEvent.click(say());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Сейчас нет привала",
    );
  });

  test("старая ошибка исчезает при следующей отправке", async () => {
    let attempts = 0;
    renderCamp({
      routes: {
        [LINE]: (call): FakeReply =>
          ++attempts === 1
            ? { status: 409, json: { detail: "Сервер занят." } }
            : { json: spoken(100, (call.body as { text: string }).text) },
      },
    });
    type("Раз");
    fireEvent.click(say());
    await screen.findByRole("alert");

    fireEvent.click(say());

    await vi.waitFor(() => expect(field()).toHaveValue(""));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("пока запрос идёт, кнопка недоступна и второй запрос не уходит", async () => {
    const { server, release } = renderCamp({ hold: "/camp/line" });
    type("Греюсь");

    fireEvent.click(say());
    fireEvent.click(say());

    expect(say()).toBeDisabled();
    release();
    await vi.waitFor(() => expect(field()).toHaveValue(""));
    expect(server.calls).toHaveLength(1);
  });
});

describe("реплика, пока идёт запрос", () => {
  test("следующая реплика, начатая за это время, не стирается ответом", async () => {
    const { release } = renderCamp({ hold: "/camp/line" });
    type("Первая");
    fireEvent.click(say());

    type("Вторая, уже начал");
    release();

    await vi.waitFor(() => expect(lines()).toHaveLength(3));
    expect(field()).toHaveValue("Вторая, уже начал");
  });
});

describe("предел реплик на героя", () => {
  const own = (from: number, count: number) =>
    Array.from({ length: count }, (_, index) =>
      spoken(from + index, `Реплика ${index + 1}`),
    );

  test("десять своих реплик — предел: поле и кнопка закрыты", () => {
    renderCamp({ feed: [narration(1, "Привал."), ...own(2, 10)] });

    expect(field()).toBeDisabled();
    expect(say()).toBeDisabled();
    expect(
      screen.getByText("Вы сказали у костра достаточно: подождите утра."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Ваших реплик: 10 из 10/)).toBeInTheDocument();
  });

  test("девять реплик — поле ещё открыто", () => {
    renderCamp({ feed: [narration(1, "Привал."), ...own(2, 9)] });

    expect(field()).toBeEnabled();
    expect(screen.getByText(/Ваших реплик: 9 из 10/)).toBeInTheDocument();
  });

  test("реплики других героев и прежних привалов в предел не идут", () => {
    const feed = [
      ...own(1, 10), // прежний привал
      narration(20, "Новый привал."),
      ...Array.from({ length: 10 }, (_, index) =>
        spoken(21 + index, `Борин ${index}`, BORIN),
      ),
    ];

    renderCamp({ feed });

    expect(field()).toBeEnabled();
    expect(screen.getByText(/Ваших реплик: 0 из 10/)).toBeInTheDocument();
  });

  test("новая реплика из события довела до предела — поле закрывается", () => {
    renderCamp({ feed: [narration(1, "Привал."), ...own(2, 9)] });

    serve(spoken(50, "Десятая"));

    expect(field()).toBeDisabled();
  });
});

describe("когда сказать нельзя", () => {
  test("игра на паузе: поле закрыто с пояснением", () => {
    renderCamp({
      view: {
        phase: phaseOf("camp", {
          phase: "paused",
          remaining_seconds: 120,
        }),
      },
    });

    expect(field()).toBeDisabled();
    expect(say()).toBeDisabled();
    expect(screen.getByText(/Игра на паузе/)).toBeInTheDocument();
  });

  test.each(["lobby", "weaving", "finished"] as const)(
    "игра в фазе %s: привал не идёт, поле закрыто с пояснением",
    (phase) => {
      renderCamp({ view: { phase: phaseOf("camp", { phase }) } });

      expect(field()).toBeDisabled();
      expect(say()).toBeDisabled();
      expect(screen.getByText("Игра сейчас не идёт.")).toBeInTheDocument();
    },
  );

  test("у игрока нет героя: поле закрыто", () => {
    renderCamp({ view: { character: null } });

    expect(field()).toBeDisabled();
    expect(
      screen.getByText("У вас нет героя в этой игре."),
    ).toBeInTheDocument();
  });
});

describe("обрыв связи", () => {
  const setConnection = (connection: LiveConnection) =>
    act(() => useGame.setState({ connection }));

  test("обрыв: поле и «Сказать» закрыты тем же пояснением, что и поле хода", () => {
    renderCamp();
    expect(field()).toBeEnabled();

    setConnection("reconnecting");

    expect(field()).toBeDisabled();
    expect(say()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(CONNECTION_LOST_NOTE);
    expect(screen.getByRole("status").children).toHaveLength(0);
  });

  test("набранное не пропадает: связь вернулась — поле открыто, текст на месте, пояснения нет", () => {
    renderCamp();
    type("Подкину дров");
    setConnection("reconnecting");
    expect(field()).toBeDisabled();
    expect(field()).toHaveValue("Подкину дров");

    setConnection("open");

    expect(field()).toBeEnabled();
    expect(field()).toHaveValue("Подкину дров");
    expect(say()).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("при обрыве кнопка запросов не шлёт", async () => {
    const { server } = renderCamp();
    type("Подкину дров");
    setConnection("reconnecting");

    fireEvent.click(say());
    await act(async () => {});

    expect(server.calls).toHaveLength(0);
  });

  test.each([
    ["пауза", { phase: phaseOf("camp", { phase: "paused" }) }],
    ["нет героя", { character: null }],
    ["игра не идёт", { phase: phaseOf("camp", { phase: "finished" }) }],
  ] as const)(
    "при обрыве причина — связь, а не «%s»: у клиента она могла устареть",
    (_name, view) => {
      renderCamp({ view });

      setConnection("reconnecting");

      expect(screen.getByRole("status")).toHaveTextContent(
        CONNECTION_LOST_NOTE,
      );
    },
  );

  test("предел реплик и обрыв: причина — связь", () => {
    const own = Array.from({ length: 10 }, (_, index) =>
      spoken(2 + index, `Реплика ${index + 1}`),
    );
    renderCamp({ feed: [narration(1, "Привал."), ...own] });

    setConnection("reconnecting");

    expect(screen.getByRole("status")).toHaveTextContent(CONNECTION_LOST_NOTE);
  });

  test.each(["idle", "connecting"] as const)(
    "связь %s — не обрыв: поле открыто по правилам привала",
    (connection) => {
      renderCamp();

      setConnection(connection);

      expect(field()).toBeEnabled();
      expect(screen.queryByRole("status")).toBeNull();
    },
  );
});

describe("движение", () => {
  const reduceMotion = () =>
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));

  test("новая реплика появляется мягко, уже бывшие — сразу", () => {
    renderCamp();
    expect(document.querySelectorAll(".feed-line.ui-anim-appear")).toHaveLength(
      0,
    );

    serve(spoken(7, "Новая", BORIN));

    expect(document.querySelectorAll(".feed-line.ui-anim-appear")).toHaveLength(
      1,
    );
    expect(lines().at(-1)).toHaveClass("ui-anim-appear");
  });

  test("при prefers-reduced-motion анимаций нет", () => {
    reduceMotion();
    renderCamp();

    serve(spoken(7, "Новая", BORIN));

    expect(document.querySelector("[class*='ui-anim-']")).toBeNull();
    expect(lines()).toHaveLength(3);
  });
});

// --- привалы по кнопке хоста (дополнение к контракту B9, §4) ---

const HIT_DIE = "POST /api/games/g1/rest/hit-dice";
const DEADLINE = "2026-10-20T12:05:00+00:00";

/** Привал по контракту сервера (§3): вид, срок, свои идеи и реплики этого привала. */
const restOf = (
  kind: RestView["kind"],
  overrides: Partial<RestView> = {},
): RestView => ({
  kind,
  deadline_at: DEADLINE,
  my_ideas: [],
  lines: [],
  ...overrides,
});

const hitDieButton = () =>
  screen.getByRole("button", { name: /Потратить кость хитов/ });

describe("заголовок по виду привала", () => {
  test.each([
    ["short", "Короткий привал", /кости хитов/],
    ["long", "Долгий привал", /утром/i],
  ] as const)("%s: «%s» и что даёт этот привал", (kind, title, note) => {
    renderCamp({ view: { rest: restOf(kind) } });

    const heading = screen.getByRole("heading", { name: title });
    const card = heading.closest("section");
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent(note);
  });

  test("вид привала ещё не пришёл — экран DND-1: тема, реплики, поле", () => {
    renderCamp({ view: { rest: null } });

    expect(
      screen.queryByRole("heading", { name: /Короткий привал|Долгий привал/ }),
    ).toBeNull();
    expect(field()).toBeEnabled();
  });

  test("реплики этого привала берутся из привала сервера, а не из ленты", () => {
    renderCamp({
      feed: [narration(1, "Лагерь.")],
      view: {
        rest: restOf("long", { lines: [spoken(5, "Подкину дров.", BORIN)] }),
      },
    });

    expect(lines().map((line) => line.textContent)).toEqual([
      "Борин: «Подкину дров.»",
    ]);
  });

  test("новая реплика из события появляется и на привале сервера", () => {
    renderCamp({
      view: {
        phase: phaseOf("camp", { deadline_at: DEADLINE }),
        rest: restOf("short", { lines: [spoken(5, "Подкину дров.", BORIN)] }),
      },
    });

    serve(spoken(7, "Кто здесь?", BORIN));

    expect(lines()).toHaveLength(2);
  });
});

describe("«Потратить кость хитов» на коротком привале", () => {
  interface HitDieSetup {
    hitDice?: number;
    failures?: number;
    reply?: FakeReply;
    hold?: boolean;
  }

  function renderShortRest({
    hitDice = 2,
    failures = 0,
    reply = { json: { hit_dice: hitDice - 1, hp: 10, hp_max: 10 } },
    hold = false,
  }: HitDieSetup = {}) {
    return renderCamp({
      view: {
        rest: restOf("short"),
        character: sheetHero({
          hp: 4,
          hp_max: 10,
          hit_dice: hitDice,
          hit_dice_max: 2,
          death_saves: { successes: 0, failures, stable: false },
        }),
      },
      routes: { [HIT_DIE]: reply },
      hold: hold ? "/rest/hit-dice" : undefined,
    });
  }

  test("кнопка с остатком костей и хитами героя", () => {
    renderShortRest({ hitDice: 2 });

    expect(hitDieButton()).toHaveAccessibleName(
      "Потратить кость хитов (осталось 2)",
    );
    expect(hitDieButton()).toBeEnabled();
    expect(screen.getByText("Хиты: 4 из 10.")).toBeInTheDocument();
  });

  test("нажатие шлёт запрос без тела; ответ сервера — новый остаток и хиты", async () => {
    const { server } = renderShortRest({
      hitDice: 2,
      reply: { json: { hit_dice: 1, hp: 9, hp_max: 10 } },
    });

    fireEvent.click(hitDieButton());

    await vi.waitFor(() =>
      expect(hitDieButton()).toHaveAccessibleName(
        "Потратить кость хитов (осталось 1)",
      ),
    );
    expect(screen.getByText("Хиты: 9 из 10.")).toBeInTheDocument();
    expect(server.calls).toHaveLength(1);
    expect(`${server.calls[0].method} ${server.calls[0].path}`).toBe(HIT_DIE);
    expect(server.calls[0].body).toBeUndefined();
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(hitDieButton()).toBeEnabled();
  });

  test("последняя кость потрачена — кнопка выключена с пояснением", async () => {
    renderShortRest({
      hitDice: 1,
      reply: { json: { hit_dice: 0, hp: 8, hp_max: 10 } },
    });

    fireEvent.click(hitDieButton());

    // Во время запроса кнопка тоже выключена: ждём именно ответа сервера.
    await vi.waitFor(() =>
      expect(hitDieButton()).toHaveAccessibleName(
        "Потратить кость хитов (осталось 0)",
      ),
    );
    expect(hitDieButton()).toBeDisabled();
    expect(hitDieButton()).toHaveAccessibleDescription(
      /Костей хитов не осталось/,
    );
  });

  test("костей нет с самого начала — кнопка выключена, запроса нет", () => {
    const { server } = renderShortRest({ hitDice: 0 });

    fireEvent.click(hitDieButton());

    expect(hitDieButton()).toBeDisabled();
    expect(server.calls).toHaveLength(0);
  });

  test("мёртвый герой кость не тратит: кнопка выключена с пояснением", () => {
    const { server } = renderShortRest({ hitDice: 2, failures: 3 });

    fireEvent.click(hitDieButton());

    expect(hitDieButton()).toBeDisabled();
    expect(hitDieButton()).toHaveAccessibleDescription(/погиб/);
    expect(server.calls).toHaveLength(0);
  });

  test("два провала спасбросков — герой ещё жив, кость потратить можно", () => {
    renderShortRest({ hitDice: 2, failures: 2 });

    expect(hitDieButton()).toBeEnabled();
  });

  test("пока запрос идёт, кнопка выключена и второй запрос не уходит", async () => {
    const { server, release } = renderShortRest({ hold: true });

    fireEvent.click(hitDieButton());
    fireEvent.click(hitDieButton());

    expect(hitDieButton()).toBeDisabled();
    release();
    await vi.waitFor(() =>
      expect(hitDieButton()).toHaveAccessibleName(
        "Потратить кость хитов (осталось 1)",
      ),
    );
    expect(server.calls).toHaveLength(1);
  });

  test("отказ сервера показан его текстом, остаток прежний", async () => {
    const detail = "Сейчас не короткий привал: кости хитов не тратятся.";
    renderShortRest({ reply: { status: 409, json: { detail } } });

    fireEvent.click(hitDieButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(hitDieButton()).toHaveAccessibleName(
      "Потратить кость хитов (осталось 2)",
    );
    expect(hitDieButton()).toBeEnabled();
  });

  test("остаток следует за листом героя от сервера (событие character)", () => {
    renderShortRest({ hitDice: 2 });

    act(() =>
      useGame.getState().receive({
        type: "character",
        character: sheetHero({ hit_dice: 0, hp: 10, hp_max: 10 }),
      }),
    );

    expect(hitDieButton()).toHaveAccessibleName(
      "Потратить кость хитов (осталось 0)",
    );
    expect(hitDieButton()).toBeDisabled();
  });

  test("на долгом привале кнопки нет", () => {
    renderCamp({ view: { rest: restOf("long") } });

    expect(screen.queryByRole("button", { name: /кость хитов/ })).toBeNull();
  });

  test("у игрока нет героя — кнопки нет", () => {
    renderCamp({ view: { rest: restOf("short"), character: null } });

    expect(screen.queryByRole("button", { name: /кость хитов/ })).toBeNull();
  });
});

describe("«Идея для рассказа»: только своя", () => {
  const IDEA = "Расскажи, почему ты боишься воды.";

  test("своя идея из привала сервера видна в карточке «Только тебе»", () => {
    renderCamp({ view: { rest: restOf("long", { my_ideas: [IDEA] }) } });

    const card = screen
      .getByRole("heading", { name: "Идея для рассказа" })
      .closest("section");
    expect(card).not.toBeNull();
    expect(
      within(card as HTMLElement).getByRole("note", { name: "Только тебе" }),
    ).toHaveTextContent(IDEA);
  });

  test("идей нет — карточки нет", () => {
    renderCamp({ view: { rest: restOf("long") } });

    expect(
      screen.queryByRole("heading", { name: "Идея для рассказа" }),
    ).toBeNull();
  });

  test("идея из кадра rest_idea появляется у адресата, повтор кадра не плодит дубль", () => {
    renderCamp({
      view: {
        phase: phaseOf("camp", { deadline_at: DEADLINE }),
        rest: restOf("short"),
      },
    });

    act(() => useGame.getState().receive({ type: "rest_idea", text: IDEA }));
    act(() => useGame.getState().receive({ type: "rest_idea", text: IDEA }));

    expect(screen.getAllByText(IDEA)).toHaveLength(1);
  });

  test("идея — не реплика: в реплики у костра и в ленту она не попадает", () => {
    renderCamp({
      view: {
        rest: restOf("long", {
          my_ideas: [IDEA],
          lines: [spoken(5, "Подкину дров.", BORIN)],
        }),
      },
    });

    const spokenList = screen.getByRole("list", { name: "Реплики у костра" });
    expect(within(spokenList).queryByText(IDEA)).toBeNull();
    expect(
      useGame.getState().live.view?.feed.some((item) => item.text === IDEA),
    ).toBe(false);
  });

  test("идея выводится только как текст", () => {
    const { container } = renderCamp({
      view: {
        rest: restOf("long", {
          my_ideas: ["<img src=x onerror=alert(1)>Идея"],
        }),
      },
    });

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x/)).toBeInTheDocument();
  });
});

// --- финальное покрытие B9: отказы сервера, числа сервера как есть, свои реплики и идеи ---

describe("кость хитов: сервер решает и считает (финальное покрытие B9)", () => {
  /** Короткий привал Лиры: 4 из 10 хитов, кости хитов есть; ответ сервера — `reply`. */
  function renderHitDie(reply: FakeReply, view: Partial<GameView> = {}) {
    return renderCamp({
      view: {
        rest: restOf("short"),
        character: sheetHero({
          hp: 4,
          hp_max: 10,
          hit_dice: 3,
          hit_dice_max: 3,
        }),
        ...view,
      },
      routes: { [HIT_DIE]: reply },
    });
  }

  // Тексты 409 сервера (B9.3, «Контракт для B9.4/B9.5»), включая общие паузу и «игра не идёт».
  test.each([
    "Кости хитов тратят только на коротком привале.",
    "У вас нет героя в этой игре.",
    "Погибший герой не может тратить кости хитов.",
    "Костей хитов не осталось: их возвращает долгий отдых.",
    "Игра на паузе: хост должен нажать «Продолжить».",
    "Игра сейчас не идёт.",
  ])("409 «%s» показан дословно, остаток и хиты прежние", async (detail) => {
    renderHitDie({ status: 409, json: { detail } });

    fireEvent.click(hitDieButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(hitDieButton()).toHaveAccessibleName(
      "Потратить кость хитов (осталось 3)",
    );
    expect(screen.getByText("Хиты: 4 из 10.")).toBeInTheDocument();
    expect(hitDieButton()).toBeEnabled();
  });

  test("клиент не досчитывает: остаток и хиты — ровно числа из ответа сервера", async () => {
    // Числа нарочно не следуют из «минус одна кость»: экран не должен их поправлять.
    renderHitDie({ json: { hit_dice: 1, hp: 10, hp_max: 12 } });

    fireEvent.click(hitDieButton());

    await vi.waitFor(() =>
      expect(hitDieButton()).toHaveAccessibleName(
        "Потратить кость хитов (осталось 1)",
      ),
    );
    expect(screen.getByText("Хиты: 10 из 12.")).toBeInTheDocument();
    expect(useGame.getState().live.view?.character).toMatchObject({
      hit_dice: 1,
      hp: 10,
      hp_max: 12,
    });
  });

  test("на паузе кнопка не выключена (контракт §4 выключает её только при 0 костей, гибели и запросе): отказ даёт сервер", async () => {
    const detail = "Игра на паузе: хост должен нажать «Продолжить».";
    const { server } = renderHitDie(
      { status: 409, json: { detail } },
      {
        phase: phaseOf("camp", {
          phase: "paused",
          deadline_at: null,
          remaining_seconds: 60,
        }),
      },
    );

    expect(hitDieButton()).toBeEnabled();
    fireEvent.click(hitDieButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(server.calls).toHaveLength(1);
  });
});

describe("реплики и идеи привала сервера (финальное покрытие B9)", () => {
  test("«Ваших реплик» считает реплики своего героя из привала сервера, а не из ленты", () => {
    // В ленте после рассказа одна реплика Лиры, а в привале сервера — две её и одна Борина.
    renderCamp({
      view: {
        rest: restOf("short", {
          lines: [
            spoken(10, "Перевяжу руку."),
            spoken(11, "Подкину дров.", BORIN),
            spoken(12, "Ещё немного отдохнём."),
          ],
        }),
      },
    });

    expect(screen.getByText(/Ваших реплик: 2 из 10/)).toBeInTheDocument();
    expect(lines()).toHaveLength(3);
  });

  test("десять своих реплик в привале сервера закрывают поле", () => {
    renderCamp({
      view: {
        rest: restOf("long", {
          lines: Array.from({ length: 10 }, (_, index) =>
            spoken(20 + index, `Реплика ${index + 1}.`),
          ),
        }),
      },
    });

    expect(field()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Вы сказали у костра достаточно: подождите утра.",
    );
  });

  test("две разные идеи — обе в одной карточке «Только тебе», в порядке прихода", () => {
    renderCamp({
      view: {
        phase: phaseOf("camp", { deadline_at: DEADLINE }),
        rest: restOf("long", { my_ideas: ["Вспомни дом."] }),
      },
    });

    act(() =>
      useGame
        .getState()
        .receive({ type: "rest_idea", text: "Расскажи о наставнике." }),
    );

    const note = screen.getByRole("note", { name: "Только тебе" });
    // Обе идеи в одной записке, прежняя — первой.
    expect(note).toHaveTextContent(/Вспомни дом\..*Расскажи о наставнике\./);
    expect(
      screen.getAllByRole("heading", { name: "Идея для рассказа" }),
    ).toHaveLength(1);
  });
});
