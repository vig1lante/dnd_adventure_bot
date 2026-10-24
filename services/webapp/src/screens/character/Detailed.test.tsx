import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import { Detailed } from "./Detailed";
import { OPTIONS, OUTDATED_WORDS, holdRequests, previewHero } from "./fixtures";

const CREATE = "POST /api/games/g1/characters";
const RANDOM = "POST /api/games/g1/characters/random-abilities";
const HELP = "POST /api/games/g1/characters/backstory-help";

type Route = FakeReply | ((call: RecordedCall) => FakeReply);

const CREATED: FakeReply = {
  status: 201,
  json: { character: previewHero({ name: "Лира" }), flavor_error: null },
};

function renderDetailed(
  routes: Record<string, Route> = {},
  options: typeof OPTIONS = OPTIONS,
) {
  signInAs();
  const server = fakeServer({ [CREATE]: CREATED, ...routes });
  const gated = holdRequests(server.fetch);
  const onBack = vi.fn();
  const onCreated = vi.fn();
  render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Detailed
        gameId="g1"
        options={options}
        onBack={onBack}
        onCreated={onCreated}
      />
    </ApiContext>,
  );
  return { server, gated, onBack, onCreated };
}

// --- помощники прохода по шагам ---

const button = (name: string | RegExp) => screen.getByRole("button", { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const next = () => click("Далее");
const progress = () => screen.getByText(/^Шаг \d+ из \d+/);

const pick = (group: string | RegExp, option: string | RegExp) =>
  fireEvent.click(
    within(screen.getByRole("radiogroup", { name: group })).getByRole("radio", {
      name: option,
    }),
  );

const tick = (group: string | RegExp, option: string | RegExp) =>
  fireEvent.click(
    within(screen.getByRole("group", { name: group })).getByRole("checkbox", {
      name: option,
    }),
  );

const setSelect = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const typeInto = (label: string | RegExp, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

/** Флажок среди навыков класса (в компетентности те же навыки встречаются снова). */
const classSkill = (name: RegExp) =>
  within(screen.getByRole("group", { name: /Навыки класса/ })).getByRole(
    "checkbox",
    { name },
  );

const raise = (ability: string) => click(`Увеличить ${ability}`);
const lower = (ability: string) => click(`Уменьшить ${ability}`);

const ABILITY_NAMES = {
  str: "Сила",
  dex: "Ловкость",
  con: "Телосложение",
  int: "Интеллект",
  wis: "Мудрость",
  cha: "Харизма",
} as const;

function assignAll(values: Record<keyof typeof ABILITY_NAMES, number>) {
  for (const [id, name] of Object.entries(ABILITY_NAMES)) {
    setSelect(name, String(values[id as keyof typeof ABILITY_NAMES]));
  }
}

const SET = { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 } as const;

/** Шаги 1–3: раса, класс, предыстория. */
function chooseOrigin(
  species = /Эльф/,
  klass = /Плут/,
  background = /Преступник/,
) {
  pick("Раса", species);
  next();
  pick("Класс", klass);
  next();
  pick("Предыстория", background);
  next();
}

/** Шаг 4 стандартным набором и переход к шагу 5. */
function standardArray() {
  click("Стандартный набор");
  assignAll(SET);
  next();
}

/** Шаг 5: +2 к Ловкости, +1 к Телосложению. */
function bonusDexCon() {
  setSelect("Бонус +2", "dex");
  setSelect("Бонус +1", "con");
  next();
}

/** Шаг 6 для эльфа-плута с предысторией «Преступник». */
function rogueSkills() {
  tick(/Навыки класса/, /Акробатика/);
  tick(/Навыки класса/, /Обман/);
  tick(/Навыки класса/, /Проницательность/);
  tick(/Навыки класса/, /Убеждение/);
  tick(/Компетентность/, /Скрытность/);
  tick(/Компетентность/, /Внимательность/);
  next();
}

/** Доходит до шага «Характер и внешность» эльфом-плутом. */
function reachPersona() {
  chooseOrigin();
  standardArray();
  bonusDexCon();
  rogueSkills();
  next(); // снаряжение
}

/** Доходит до последнего шага. */
function reachBackstory() {
  reachPersona();
  typeInto("Имя", "Лира");
  next();
}

const posts = (server: { calls: RecordedCall[] }, route: string) =>
  server.calls.filter((call) => `${call.method} ${call.path}` === route);

// --- шаги выбора ---

describe("путь по шагам", () => {
  test("начинается с расы; «Далее» ждёт выбора, «Назад» на первом шаге выходит к способам", () => {
    const { onBack } = renderDetailed();

    expect(progress()).toHaveTextContent("Шаг 1 из 9: Раса");
    expect(button("Далее")).toBeDisabled();
    pick("Раса", /Эльф/);
    expect(button("Далее")).toBeEnabled();
    click("Назад");

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  test("раса, класс и предыстория показаны названиями сервера с пояснениями", () => {
    renderDetailed();

    const species = screen.getByRole("radiogroup", { name: "Раса" });
    expect(within(species).getAllByRole("radio")).toHaveLength(3);
    expect(
      within(species).getByRole("radio", { name: /Острые чувства/ }),
    ).toBeInTheDocument();
    pick("Раса", /Эльф/);
    next();
    expect(progress()).toHaveTextContent("Шаг 2 из 9: Класс");
    expect(
      screen.getByRole("radio", { name: /Плут.*к8.*Скрытая атака/ }),
    ).toBeInTheDocument();
    pick("Класс", /Плут/);
    next();
    expect(progress()).toHaveTextContent("Шаг 3 из 9: Предыстория");
    expect(
      screen.getByRole("radio", { name: /Преступник.*Бдительный/ }),
    ).toBeInTheDocument();
  });

  test("под списком — особенности именно выбранной расы и класса, а не соседних", () => {
    renderDetailed();
    expect(screen.queryByText(/Внимательность\./)).toBeNull();

    pick("Раса", /Эльф/);
    const elfTrait = screen.getByText(/Острые чувства\./, {
      selector: "strong",
    });
    expect(elfTrait.closest("li")).toHaveTextContent("Внимательность.");
    expect(
      screen.queryByText(/Тёмное зрение\./, { selector: "strong" }),
    ).toBeNull();

    pick("Раса", /Дварф/);
    expect(
      screen.getByText(/Тёмное зрение\./, { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Острые чувства\./, { selector: "strong" }),
    ).toBeNull();

    next();
    pick("Класс", /Плут/);
    expect(
      screen.getByText(/Скрытая атака\./, { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Колдовство\./, { selector: "strong" }),
    ).toBeNull();
  });

  test("первый шаг называется «Раса»: устаревших слов на нём нет", () => {
    renderDetailed();
    pick("Раса", /Дварф/);

    expect(
      screen.getByRole("radiogroup", { name: "Раса" }),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("весь путь волшебника, все десять шагов: нигде нет ни «вид», ни футов", () => {
    renderDetailed();
    const shown: string[] = [];
    // Шаг проверяется после выбора на нём: выбор открывает пояснения и особенности.
    const checkStep = () => {
      shown.push(progress().textContent ?? "");
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
    };

    pick("Раса", /Эльф/);
    checkStep();
    next();
    pick("Класс", /Волшебник/);
    checkStep();
    next();
    pick("Предыстория", /Преступник/);
    checkStep();
    next();
    click("Стандартный набор");
    assignAll(SET);
    checkStep();
    next();
    setSelect("Бонус +2", "dex");
    setSelect("Бонус +1", "con");
    checkStep();
    next();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    checkStep();
    next();
    tick(/Заговоры/, /Огненная стрела/);
    tick(/Заговоры/, /Свет/);
    tick(/Заклинания/, /Щит/);
    tick(/Заклинания/, /Сон/);
    checkStep();
    next();
    checkStep(); // снаряжение
    next();
    typeInto("Имя", "Мирра");
    checkStep();
    next();
    checkStep(); // история героя

    // Пока класс не выбран, шага заклинаний нет: шагов девять.
    expect(shown).toEqual([
      "Шаг 1 из 9: Раса",
      "Шаг 2 из 10: Класс",
      "Шаг 3 из 10: Предыстория",
      "Шаг 4 из 10: Характеристики",
      "Шаг 5 из 10: Бонус предыстории",
      "Шаг 6 из 10: Навыки и выборы класса",
      "Шаг 7 из 10: Заклинания",
      "Шаг 8 из 10: Снаряжение",
      "Шаг 9 из 10: Характер и внешность",
      "Шаг 10 из 10: История героя",
    ]);
  });

  test("«Назад» возвращает на прошлый шаг, выбор сохраняется", () => {
    renderDetailed();
    pick("Раса", /Дварф/);
    next();

    click("Назад");

    expect(progress()).toHaveTextContent("Шаг 1 из 9: Раса");
    expect(screen.getByRole("radio", { name: /Дварф/ })).toBeChecked();
  });

  test("пока шаг не закончен, под кнопками подсказка, что делать", () => {
    renderDetailed();

    expect(
      screen.getByText(/закончите выбор на этом шаге/),
    ).toBeInTheDocument();
    pick("Раса", /Эльф/);
    expect(screen.queryByText(/закончите выбор на этом шаге/)).toBeNull();
  });

  test("новый шаг прокручивается к началу, а при первом показе прокрутки нет", () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(window.HTMLElement.prototype, "scrollIntoView", {
      value: scrollIntoView,
      configurable: true,
      writable: true,
    });
    onTestFinished(() => {
      Reflect.deleteProperty(window.HTMLElement.prototype, "scrollIntoView");
    });
    renderDetailed();
    expect(scrollIntoView).not.toHaveBeenCalled();
    pick("Раса", /Эльф/);

    next();
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    click("Назад");

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });

  test("шаг заклинаний появляется только у заклинателя", () => {
    renderDetailed();
    pick("Раса", /Эльф/);
    next();

    pick("Класс", /Волшебник/);
    expect(progress()).toHaveTextContent("Шаг 2 из 10");
    pick("Класс", /Плут/);
    expect(progress()).toHaveTextContent("Шаг 2 из 9");
  });

  test("смена класса после выбора навыков не оставляет прежний выбор", () => {
    renderDetailed();
    chooseOrigin();
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Акробатика/);
    expect(classSkill(/Акробатика/)).toBeChecked();
    click("Назад");
    click("Назад");
    click("Назад");
    click("Назад");
    pick("Класс", /Воин/);
    next();
    next();
    next();
    next();

    expect(progress()).toHaveTextContent("Навыки и выборы класса");
    // Акробатика есть в списке воина, поэтому осталась отмеченной; навыки плута — нет.
    expect(classSkill(/Акробатика/)).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /Обман/ })).toBeNull();
  });
});

// --- характеристики ---

describe("характеристики: покупка очков", () => {
  function openAbilities() {
    const rendered = renderDetailed();
    chooseOrigin();
    return rendered;
  }

  test("счётчик «осталось N из 27» меняется с покупкой", () => {
    openAbilities();

    expect(progress()).toHaveTextContent("Характеристики");
    expect(screen.getByText("Осталось 27 из 27")).toBeInTheDocument();
    for (let i = 0; i < 7; i++) raise("Сила"); // 8 -> 15 стоит 9
    expect(screen.getByText("Осталось 18 из 27")).toBeInTheDocument();
    lower("Сила");
    expect(screen.getByText("Осталось 20 из 27")).toBeInTheDocument();
  });

  test("превышение 27 заблокировано: кнопка «+» неактивна, когда очков не хватает", () => {
    openAbilities();
    for (const name of ["Сила", "Ловкость", "Телосложение"]) {
      for (let i = 0; i < 7; i++) raise(name);
    }

    expect(screen.getByText("Осталось 0 из 27")).toBeInTheDocument();
    expect(button("Увеличить Интеллект")).toBeDisabled();
    expect(button("Увеличить Мудрость")).toBeDisabled();
    // Освободили два очка — снова можно купить дешёвое повышение.
    lower("Сила");
    expect(screen.getByText("Осталось 2 из 27")).toBeInTheDocument();
    expect(button("Увеличить Интеллект")).toBeEnabled();
  });

  test("предел значения: выше 15 и ниже 8 не двигается", () => {
    openAbilities();

    expect(button("Уменьшить Сила")).toBeDisabled();
    for (let i = 0; i < 7; i++) raise("Сила");
    expect(button("Увеличить Сила")).toBeDisabled();
    expect(screen.getByRole("group", { name: "Сила" })).toHaveTextContent("15");
  });

  test("у значений видна цена по таблице сервера", () => {
    openAbilities();
    for (let i = 0; i < 6; i++) raise("Ловкость"); // 14

    expect(screen.getByRole("group", { name: "Ловкость" })).toHaveTextContent(
      "7 оч.",
    );
  });

  test("покупка очков уходит на сервер исходными значениями", async () => {
    const { server, onCreated } = renderDetailed();
    chooseOrigin();
    for (let i = 0; i < 7; i++) raise("Ловкость");
    for (let i = 0; i < 6; i++) raise("Телосложение");
    next();
    bonusDexCon();
    rogueSkills();
    next();
    typeInto("Имя", "Лира");
    next();

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toMatchObject({
      mode: "detailed",
      choices: {
        method: "point_buy",
        base_scores: { str: 8, dex: 15, con: 14, int: 8, wis: 8, cha: 8 },
      },
    });
  });
});

describe("характеристики: стандартный набор", () => {
  function openArray() {
    const rendered = renderDetailed();
    chooseOrigin();
    click("Стандартный набор");
    return rendered;
  }

  test("«Далее» ждёт, пока розданы все шесть значений", () => {
    openArray();
    expect(button("Далее")).toBeDisabled();

    assignAll({ ...SET, cha: 12 });

    expect(button("Далее")).toBeEnabled();
  });

  test("значение, отданное одной характеристике, другим не предлагается", () => {
    openArray();

    setSelect("Сила", "15");

    const dexterity = within(screen.getByLabelText("Ловкость"));
    expect(dexterity.queryByRole("option", { name: "15" })).toBeNull();
    expect(dexterity.getByRole("option", { name: "14" })).toBeInTheDocument();
    // У самой Силы своё значение остаётся.
    expect(
      within(screen.getByLabelText("Сила")).getByRole("option", { name: "15" }),
    ).toBeInTheDocument();
  });

  test("пока ничего не роздано, везде выбрано «—»", () => {
    openArray();

    for (const name of Object.values(ABILITY_NAMES)) {
      expect(screen.getByLabelText(name)).toHaveValue("");
    }
  });

  test("выбор «—» возвращает значение в набор: другим оно снова предлагается", () => {
    openArray();
    setSelect("Сила", "15");
    expect(
      within(screen.getByLabelText("Ловкость")).queryByRole("option", {
        name: "15",
      }),
    ).toBeNull();

    setSelect("Сила", "");

    expect(screen.getByLabelText("Сила")).toHaveValue("");
    expect(
      within(screen.getByLabelText("Ловкость")).getByRole("option", {
        name: "15",
      }),
    ).toBeInTheDocument();
    expect(button("Далее")).toBeDisabled();
  });

  test("розданы все шесть, одну вернули в набор — «Далее» снова закрыта", () => {
    openArray();
    assignAll({ ...SET, cha: 12 });
    expect(button("Далее")).toBeEnabled();

    setSelect("Харизма", "");

    expect(screen.getByLabelText("Харизма")).toHaveValue("");
    expect(button("Далее")).toBeDisabled();
  });

  test("предлагаются ровно значения набора с сервера", () => {
    openArray();

    const options = within(screen.getByLabelText("Сила"))
      .getAllByRole("option")
      .map((option) => option.textContent);

    expect(options).toEqual(["—", "15", "14", "13", "12", "10", "8"]);
  });
});

describe("характеристики: распределить случайно", () => {
  // Раскладка на 27 очков, какую отдаёт сервер для плута: ловкость — основная.
  const LAYOUT = { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 };
  const OTHER = { str: 10, dex: 15, con: 12, int: 8, wis: 13, cha: 14 };
  const answer = (scores: Record<string, number>): FakeReply => ({
    json: { method: "point_buy", scores },
  });

  function openAbilities(routes: Record<string, Route> = {}) {
    const rendered = renderDetailed({ [RANDOM]: answer(LAYOUT), ...routes });
    chooseOrigin();
    return rendered;
  }

  const scoreOf = (ability: string) =>
    within(screen.getByRole("group", { name: ability })).getByText(/^\d+$/)
      .textContent;
  const allScores = () =>
    Object.fromEntries(
      Object.entries(ABILITY_NAMES).map(([id, name]) => [
        id,
        Number(scoreOf(name)),
      ]),
    );

  test("три способа: покупка очков, стандартный набор, распределить случайно; «Броска» нет", () => {
    openAbilities();

    for (const name of [
      "Покупка очков",
      "Стандартный набор",
      "Распределить случайно",
    ]) {
      expect(button(name)).toBeEnabled();
    }
    expect(screen.queryByRole("button", { name: /Бросок|Бросить/ })).toBeNull();
    expect(button("Покупка очков")).toHaveAttribute("aria-pressed", "true");
    // Подсказка берёт число очков из правил сервера, а не из клиента.
    expect(screen.getByText(/раскладывает те же 27 очков/)).toBeInTheDocument();
  });

  test("запрос уходит с class_id выбранного класса и токеном, без query", async () => {
    const { server } = openAbilities();

    click("Распределить случайно");

    await vi.waitFor(() => expect(posts(server, RANDOM)).toHaveLength(1));
    const [call] = posts(server, RANDOM);
    expect(call.body).toEqual({ class_id: "rogue" });
    expect(call.headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(call.url).toBe("/api/games/g1/characters/random-abilities");
  });

  test("для другого класса уходит его class_id", async () => {
    const rendered = renderDetailed({ [RANDOM]: answer(LAYOUT) });
    chooseOrigin(/Эльф/, /Волшебник/);

    click("Распределить случайно");

    await vi.waitFor(() =>
      expect(posts(rendered.server, RANDOM)[0]?.body).toEqual({
        class_id: "wizard",
      }),
    );
  });

  test("значения сервера ложатся в покупку очков: ровно они, счётчик «Осталось 0 из 27»", async () => {
    const random = vi.spyOn(Math, "random");
    openAbilities();

    click("Распределить случайно");

    expect(await screen.findByText("Осталось 0 из 27")).toBeInTheDocument();
    expect(allScores()).toEqual(LAYOUT);
    expect(button("Покупка очков")).toHaveAttribute("aria-pressed", "true");
    expect(button("Далее")).toBeEnabled();
    // Клиент ничего не бросает и не считает: числа только от сервера.
    expect(random).not.toHaveBeenCalled();
  });

  test("с вкладки «Стандартный набор» раскладка тоже открывает покупку очков", async () => {
    openAbilities();
    click("Стандартный набор");

    click("Распределить случайно");

    expect(await screen.findByText("Осталось 0 из 27")).toBeInTheDocument();
    expect(button("Покупка очков")).toHaveAttribute("aria-pressed", "true");
    expect(button("Стандартный набор")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("повторное нажатие заменяет раскладку: нажимать можно сколько угодно", async () => {
    const layouts = [LAYOUT, OTHER, LAYOUT];
    let calls = 0;
    const { server } = openAbilities({
      [RANDOM]: () => answer(layouts[calls++]),
    });

    click("Распределить случайно");
    await vi.waitFor(() => expect(scoreOf("Мудрость")).toBe("10"));
    click("Распределить случайно");
    await vi.waitFor(() => expect(scoreOf("Мудрость")).toBe("13"));
    expect(scoreOf("Интеллект")).toBe("8");
    click("Распределить случайно");
    await vi.waitFor(() => expect(scoreOf("Мудрость")).toBe("10"));

    expect(posts(server, RANDOM)).toHaveLength(3);
    expect(button("Распределить случайно")).toBeEnabled();
  });

  test("раскладку можно поправить вручную кнопками «+» и «−»", async () => {
    openAbilities();
    click("Распределить случайно");
    await screen.findByText("Осталось 0 из 27");

    lower("Ловкость"); // 15 -> 14 возвращает 2 очка
    expect(screen.getByText("Осталось 2 из 27")).toBeInTheDocument();
    expect(scoreOf("Ловкость")).toBe("14");
    expect(button("Уменьшить Сила")).toBeDisabled(); // уже 8: ниже нельзя
    raise("Интеллект"); // 13 -> 14 стоит 2
    expect(screen.getByText("Осталось 0 из 27")).toBeInTheDocument();
    expect(scoreOf("Интеллект")).toBe("14");
    expect(button("Увеличить Мудрость")).toBeDisabled();
  });

  test("после ручной правки новое нажатие снова ставит раскладку сервера", async () => {
    openAbilities();
    click("Распределить случайно");
    await screen.findByText("Осталось 0 из 27");
    lower("Ловкость");
    expect(scoreOf("Ловкость")).toBe("14");

    click("Распределить случайно");

    await vi.waitFor(() => expect(scoreOf("Ловкость")).toBe("15"));
    expect(screen.getByText("Осталось 0 из 27")).toBeInTheDocument();
  });

  test("значения других способов не теряются: стандартный набор остаётся раздан", async () => {
    openAbilities();
    click("Стандартный набор");
    assignAll(SET);
    click("Распределить случайно");
    await screen.findByText("Осталось 0 из 27");

    click("Стандартный набор");

    expect(screen.getByLabelText("Сила")).toHaveValue("8");
    expect(screen.getByLabelText("Ловкость")).toHaveValue("15");
  });

  test("раскладка уходит на сервер как method point_buy с исходными значениями", async () => {
    const { server, onCreated } = openAbilities();
    click("Распределить случайно");
    await screen.findByText("Осталось 0 из 27");
    next();
    bonusDexCon();
    rogueSkills();
    next();
    typeInto("Имя", "Лира");
    next();

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toMatchObject({
      mode: "detailed",
      choices: { method: "point_buy", base_scores: LAYOUT },
    });
  });

  test("пока сервер раскладывает, кнопка заблокирована и второй запрос не уходит", async () => {
    const { gated, server } = openAbilities();
    const release = gated.hold("/random-abilities");

    click("Распределить случайно");

    expect(await screen.findByRole("status")).toHaveTextContent("раскладывает");
    expect(button("Распределить случайно")).toBeDisabled();
    // Ответ не должен застать игрока на другой вкладке: переключатели тоже ждут.
    expect(button("Покупка очков")).toBeDisabled();
    expect(button("Стандартный набор")).toBeDisabled();
    fireEvent.click(button("Распределить случайно"));
    expect(
      gated.started.filter((line) => line.endsWith("/random-abilities")),
    ).toHaveLength(1);
    release();
    await screen.findByText("Осталось 0 из 27");
    expect(posts(server, RANDOM)).toHaveLength(1);
    // Запрос закончился — кнопка снова доступна.
    expect(button("Распределить случайно")).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("ответ, пришедший после ухода с шага, раскладку не меняет", async () => {
    const { gated, server } = openAbilities();
    const release = gated.hold("/random-abilities");
    click("Распределить случайно");
    await screen.findByRole("status");
    next(); // игрок ушёл на шаг бонуса, пока сервер думал

    release();
    await vi.waitFor(() => expect(posts(server, RANDOM)).toHaveLength(1));
    await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    click("Назад");

    expect(screen.getByText("Осталось 27 из 27")).toBeInTheDocument();
  });

  test("отказ сервера: текст сервера, прежние значения не тронуты, можно нажать снова", async () => {
    let calls = 0;
    openAbilities({
      [RANDOM]: () =>
        ++calls === 1
          ? {
              status: 409,
              json: {
                detail: "Игра уже началась: персонажа больше нельзя менять.",
              },
            }
          : answer(LAYOUT),
    });
    raise("Сила");

    click("Распределить случайно");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Игра уже началась",
    );
    expect(scoreOf("Сила")).toBe("9");
    expect(button("Распределить случайно")).toBeEnabled();
    click("Распределить случайно");
    await screen.findByText("Осталось 0 из 27");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("422: список problems показан под кнопкой", async () => {
    openAbilities({
      [RANDOM]: {
        status: 422,
        json: {
          detail: "Неизвестный класс.",
          problems: ["неизвестный класс: 'rogue'"],
        },
      },
    });

    click("Распределить случайно");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Неизвестный класс.");
    expect(within(alert).getByRole("listitem")).toHaveTextContent(
      "неизвестный класс: 'rogue'",
    );
  });
});

// --- бонус предыстории ---

describe("бонус предыстории", () => {
  function openBonus() {
    renderDetailed();
    chooseOrigin();
    standardArray();
  }

  test("форма +2/+1: два выбора из характеристик предыстории, повторять нельзя", () => {
    openBonus();

    expect(progress()).toHaveTextContent("Бонус предыстории");
    const major = within(screen.getByLabelText("Бонус +2"));
    expect(major.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "—",
      "Ловкость",
      "Телосложение",
      "Интеллект",
    ]);
    expect(button("Далее")).toBeDisabled();
    setSelect("Бонус +2", "dex");
    const minor = within(screen.getByLabelText("Бонус +1"));
    expect(minor.queryByRole("option", { name: "Ловкость" })).toBeNull();
    setSelect("Бонус +1", "con");
    expect(button("Далее")).toBeEnabled();
  });

  test("форма +1/+1/+1: у предыстории три характеристики, выбор известен сам", () => {
    openBonus();

    pick("Форма бонуса", /\+1, \+1, \+1/);

    expect(screen.queryByLabelText("Бонус +1")).toBeNull();
    expect(
      screen.getByText(/Ловкость, Телосложение, Интеллект/),
    ).toBeInTheDocument();
    expect(button("Далее")).toBeEnabled();
  });

  test("в тело запроса бонус уходит картой «характеристика — прибавка»", async () => {
    const { server, onCreated } = renderDetailed();
    chooseOrigin();
    standardArray();
    pick("Форма бонуса", /\+1, \+1, \+1/);
    next();
    rogueSkills();
    next();
    typeInto("Имя", "Лира");
    next();

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(posts(server, CREATE)[0].body).toMatchObject({
      choices: { background_bonus: { dex: 1, con: 1, int: 1 } },
    });
  });
});

// --- навыки и выборы класса ---

describe("навыки и выборы класса", () => {
  function openSkills(species = /Эльф/, klass = /Плут/) {
    renderDetailed();
    chooseOrigin(species, klass);
    standardArray();
    bonusDexCon();
  }

  test("то, что даёт предыстория и раса, показано отдельно и не выбирается", () => {
    openSkills();

    expect(screen.getByText(/Уже есть/)).toHaveTextContent(
      "Ловкость рук, Скрытность, Внимательность",
    );
    expect(screen.getByText(/Уже есть/)).toHaveTextContent(
      "Их дают предыстория и раса.",
    );
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
    const group = screen.getByRole("group", { name: /Навыки класса/ });
    expect(
      within(group).queryByRole("checkbox", { name: /Скрытность/ }),
    ).toBeNull();
    expect(within(group).getAllByRole("checkbox")).toHaveLength(6);
  });

  test("можно выбрать ровно столько навыков, сколько даёт класс", () => {
    openSkills();

    for (const name of [
      /Акробатика/,
      /Обман/,
      /Проницательность/,
      /Убеждение/,
    ]) {
      tick(/Навыки класса/, name);
    }

    expect(screen.getByText("Выбрано 4 из 4")).toBeInTheDocument();
    expect(classSkill(/Атлетика/)).toBeDisabled();
    expect(classSkill(/Акробатика/)).toBeEnabled();
  });

  test("компетентность плута: два навыка из тех, которыми владеет герой", () => {
    openSkills();
    tick(/Навыки класса/, /Акробатика/);

    const group = screen.getByRole("group", { name: /Компетентность/ });
    expect(
      within(group)
        .getAllByRole("checkbox")
        .map((box) => box.closest("label")?.textContent),
    ).toEqual(["Ловкость рук", "Скрытность", "Внимательность", "Акробатика"]);
    expect(button("Далее")).toBeDisabled();
  });

  test("человеку нужен навык на выбор, эльфу — нет", () => {
    openSkills(/Человек/);

    expect(screen.getByLabelText(/Навык на выбор/)).toBeInTheDocument();
  });

  test("навык на выбор: нет того, чем герой уже владеет и что выбрал класс; свой выбор остаётся", () => {
    openSkills(/Человек/);
    tick(/Навыки класса/, /Акробатика/);

    const select = () => screen.getByLabelText(/Навык на выбор/);
    const names = () =>
      within(select())
        .getAllByRole("option")
        .map((option) => option.textContent);
    expect(names()[0]).toBe("Выберите навык");
    expect(names()).not.toContain("Скрытность"); // даёт предыстория
    expect(names()).not.toContain("Ловкость рук"); // тоже предыстория
    expect(names()).not.toContain("Акробатика"); // выбран классом
    expect(names()).toContain("История");

    fireEvent.change(select(), { target: { value: "history" } });

    expect(select()).toHaveValue("history");
    expect(names()).toContain("История");
    // Выбранный навык — тоже владение: на нём можно держать компетентность плута.
    const expertise = screen.getByRole("group", { name: /Компетентность/ });
    expect(
      within(expertise).getByRole("checkbox", { name: /История/ }),
    ).toBeEnabled();
  });

  test("у эльфа поля навыка на выбор нет", () => {
    openSkills();

    expect(screen.queryByLabelText(/Навык на выбор/)).toBeNull();
  });

  test("воину нужен боевой стиль; у плута его нет", () => {
    openSkills(/Эльф/, /Воин/);

    expect(
      screen.getByRole("radiogroup", { name: "Боевой стиль" }),
    ).toBeVisible();
    expect(screen.queryByRole("group", { name: /Компетентность/ })).toBeNull();
  });
});

// --- заклинания и снаряжение ---

describe("заклинания", () => {
  function openSpells() {
    renderDetailed();
    chooseOrigin(/Эльф/, /Волшебник/);
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    next();
  }

  test("волшебник выбирает заговоры и заклинания по норме класса", () => {
    openSpells();

    expect(progress()).toHaveTextContent("Заклинания");
    tick(/Заговоры/, /Огненная стрела/);
    tick(/Заговоры/, /Свет/);
    expect(
      screen.getByRole("checkbox", { name: /Волшебная рука/ }),
    ).toBeDisabled();
    expect(button("Далее")).toBeDisabled();
    tick(/Заклинания/, /Щит/);
    tick(/Заклинания/, /Сон/);

    expect(button("Далее")).toBeEnabled();
  });

  test("заклинатель без заговоров (как следопыт): группы заговоров нет, шаг идёт по заклинаниям", () => {
    const withoutCantrips: typeof OPTIONS = {
      ...OPTIONS,
      classes: OPTIONS.classes.map((cls) =>
        cls.id === "wizard" && cls.spellcasting !== null
          ? {
              ...cls,
              spellcasting: {
                ...cls.spellcasting,
                cantrips: 0,
                cantrip_options: [],
              },
            }
          : cls,
      ),
    };
    renderDetailed({}, withoutCantrips);
    chooseOrigin(/Эльф/, /Волшебник/);
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    next();

    expect(progress()).toHaveTextContent("Заклинания");
    expect(screen.queryByRole("group", { name: /Заговоры/ })).toBeNull();
    tick(/Заклинания/, /Щит/);
    tick(/Заклинания/, /Сон/);
    expect(button("Далее")).toBeEnabled();
  });

  test("у заклинания видно, какого оно уровня; у заговора — только суть", () => {
    openSpells();

    const cantrip = screen.getByRole("checkbox", { name: /Огненная стрела/ });
    expect(cantrip).toHaveAccessibleName(/Огонь\.$/);
    expect(cantrip).not.toHaveAccessibleName(/уровень/);
    expect(screen.getByRole("checkbox", { name: /Щит/ })).toHaveAccessibleName(
      /Щит\s*1-й уровень\. Защита\.$/,
    );
  });

  test("выбранные заклинания уходят на сервер", async () => {
    const { server, onCreated } = renderDetailed();
    chooseOrigin(/Эльф/, /Волшебник/);
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    next();
    tick(/Заговоры/, /Огненная стрела/);
    tick(/Заговоры/, /Свет/);
    tick(/Заклинания/, /Щит/);
    tick(/Заклинания/, /Сон/);
    next();
    next(); // снаряжение
    typeInto("Имя", "Мирра");
    next();

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(posts(server, CREATE)[0].body).toMatchObject({
      choices: {
        class_id: "wizard",
        cantrips: ["fire_bolt", "light"],
        prepared_spells: ["shield", "sleep"],
      },
    });
  });
});

describe("снаряжение", () => {
  test("стартовый набор класса и предыстории только показан", () => {
    renderDetailed();
    chooseOrigin();
    standardArray();
    bonusDexCon();
    rogueSkills();

    expect(progress()).toHaveTextContent("Снаряжение");
    expect(screen.getByText("Кинжал × 2")).toBeInTheDocument();
    expect(screen.getByText("Воровские инструменты × 1")).toBeInTheDocument();
    expect(screen.getByText("Кинжал × 1")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(button("Далее")).toBeEnabled();
  });
});

// --- характер, предыстория, отправка ---

describe("характер и внешность", () => {
  test("имя обязательно: без него «Далее» неактивна", () => {
    renderDetailed();
    reachPersona();

    expect(progress()).toHaveTextContent("Характер и внешность");
    expect(button("Далее")).toBeDisabled();
    typeInto("Имя", "   ");
    expect(button("Далее")).toBeDisabled();
    typeInto("Имя", "Лира");
    expect(button("Далее")).toBeEnabled();
  });

  test("поля описания имеют пределы длины сервера", () => {
    renderDetailed();
    reachPersona();

    expect(screen.getByLabelText("Имя")).toHaveAttribute("maxlength", "40");
    expect(screen.getByLabelText(/Замысел/)).toHaveAttribute(
      "maxlength",
      "200",
    );
    expect(screen.getByLabelText("Внешность")).toHaveAttribute(
      "maxlength",
      "400",
    );
    expect(screen.getByLabelText("Черты характера")).toHaveAttribute(
      "maxlength",
      "300",
    );
  });
});

describe("отправка и отказы", () => {
  test("полный выбор уходит как mode detailed; числа клиент не присылает", async () => {
    const { server, onCreated } = renderDetailed();
    reachPersona();
    typeInto("Имя", "  Лира ");
    typeInto(/Замысел/, "Вор-неудачник");
    typeInto("Внешность", "Худая, быстрая.");
    next();
    typeInto("Предыстория героя", "Родилась в порту.");

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)).toHaveLength(1);
    expect(posts(server, CREATE)[0].headers.get("Authorization")).toBe(
      "Bearer tok-u1",
    );
    expect(posts(server, CREATE)[0].body).toEqual({
      mode: "detailed",
      choices: {
        name: "Лира",
        species: "elf",
        class_id: "rogue",
        background: "criminal",
        method: "standard_array",
        base_scores: SET,
        background_bonus: { dex: 2, con: 1 },
        skills: ["acrobatics", "deception", "insight", "persuasion"],
        extra_skill: null,
        expertise: ["stealth", "perception"],
        fighting_style: null,
        cantrips: [],
        prepared_spells: [],
        persona: {
          concept: "Вор-неудачник",
          appearance: "Худая, быстрая.",
          backstory: "Родилась в порту.",
        },
      },
    });
  });

  test("422: список problems показан у формы, выбор сохранён, можно исправить и отправить снова", async () => {
    let attempts = 0;
    const { server, onCreated } = renderDetailed({
      [CREATE]: () =>
        ++attempts === 1
          ? {
              status: 422,
              json: {
                detail: "Персонаж не прошёл проверку правил.",
                problems: [
                  "навык «Скрытность» уже даёт предыстория «Преступник»; выберите другой",
                  "компетентность: нужно выбрать навыков ровно 2, выбрано 1",
                ],
              },
            }
          : CREATED,
    });
    reachBackstory();

    click("Создать героя");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Персонаж не прошёл проверку правил.");
    expect(
      within(alert)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "навык «Скрытность» уже даёт предыстория «Преступник»; выберите другой",
      "компетентность: нужно выбрать навыков ровно 2, выбрано 1",
    ]);
    expect(onCreated).not.toHaveBeenCalled();
    expect(button("Создать героя")).toBeEnabled();

    // Возврат назад ничего не стёр.
    click("Назад");
    expect(screen.getByLabelText("Имя")).toHaveValue("Лира");
    next();
    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)).toHaveLength(2);
  });

  test("пока идёт отправка, кнопка заблокирована и второй запрос не уходит", async () => {
    const { gated, server, onCreated } = renderDetailed();
    reachBackstory();
    const release = gated.hold("/api/games/g1/characters");

    click("Создать героя");

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(button("Создать героя")).toBeDisabled();
    fireEvent.click(button("Создать героя"));
    expect(
      gated.started.filter((line) => line === "POST /api/games/g1/characters"),
    ).toHaveLength(1);
    release();
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)).toHaveLength(1);
  });

  test("отказ без подробностей показан текстом сервера", async () => {
    renderDetailed({
      [CREATE]: {
        status: 409,
        json: { detail: "Игра уже началась: персонажа больше нельзя менять." },
      },
    });
    reachBackstory();

    click("Создать героя");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Игра уже началась",
    );
    expect(screen.queryByRole("listitem")).toBeNull();
    expect(document.querySelector(".form-problems")).toBeNull();
  });
});

describe("«Помоги написать»", () => {
  const helpBody = {
    class_id: "rogue",
    species: "elf",
    concept: "Вор-неудачник",
    draft: "Родилась в порту.",
  };

  function openBackstory(routes: Record<string, Route> = {}) {
    const rendered = renderDetailed({
      [HELP]: {
        json: { backstory: "Она выросла в порту и украла первый кошелёк." },
      },
      ...routes,
    });
    reachPersona();
    typeInto("Имя", "Лира");
    typeInto(/Замысел/, "Вор-неудачник");
    next();
    typeInto("Предыстория героя", "Родилась в порту.");
    return rendered;
  }

  test("подставляет в поле текст от мастера и отправляет класс, расу, замысел и черновик", async () => {
    const { server } = openBackstory();

    click("Помоги написать");

    await vi.waitFor(() =>
      expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
        "Она выросла в порту и украла первый кошелёк.",
      ),
    );
    expect(posts(server, HELP)).toHaveLength(1);
    expect(posts(server, HELP)[0].body).toEqual(helpBody);
  });

  test("пустые замысел и черновик в запрос не попадают", async () => {
    const { server } = renderDetailed({
      [HELP]: { json: { backstory: "Текст." } },
    });
    reachBackstory();

    click("Помоги написать");

    await vi.waitFor(() => expect(posts(server, HELP)).toHaveLength(1));
    expect(posts(server, HELP)[0].body).toEqual({
      class_id: "rogue",
      species: "elf",
    });
  });

  test("своего текста не было — «Вернуть мой текст» не предлагается: возвращать нечего", async () => {
    renderDetailed({ [HELP]: { json: { backstory: "Текст мастера." } } });
    reachBackstory();

    click("Помоги написать");

    await vi.waitFor(() =>
      expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
        "Текст мастера.",
      ),
    );
    expect(
      screen.queryByRole("button", { name: "Вернуть мой текст" }),
    ).toBeNull();
  });

  test("пока мастер пишет, кнопка заблокирована, поле и создание ждут", async () => {
    const { gated } = openBackstory();
    const release = gated.hold("/backstory-help");

    click("Помоги написать");

    expect(await screen.findByRole("status")).toHaveTextContent("Мастер пишет");
    expect(button("Помоги написать")).toBeDisabled();
    expect(button("Создать героя")).toBeDisabled();
    fireEvent.click(button("Помоги написать"));
    expect(
      gated.started.filter((line) => line.endsWith("/backstory-help")),
    ).toHaveLength(1);
    release();
    await vi.waitFor(() => expect(button("Помоги написать")).toBeEnabled());
  });

  test("«Вернуть мой текст» возвращает черновик, который был до подстановки", async () => {
    openBackstory();
    click("Помоги написать");
    await vi.waitFor(() =>
      expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
        "Она выросла в порту и украла первый кошелёк.",
      ),
    );

    click("Вернуть мой текст");

    expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
      "Родилась в порту.",
    );
    expect(
      screen.queryByRole("button", { name: "Вернуть мой текст" }),
    ).toBeNull();
  });

  test("отказ мастера показан текстом сервера, свой текст остаётся", async () => {
    openBackstory({
      [HELP]: {
        status: 503,
        json: {
          detail:
            "Нейронка не ответила. Попробуйте позже или напишите предысторию сами.",
        },
      },
    });

    click("Помоги написать");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "напишите предысторию сами",
    );
    expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
      "Родилась в порту.",
    );
    expect(button("Помоги написать")).toBeEnabled();
  });
});
