import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import { Detailed } from "./Detailed";
import type { CharacterChoices } from "../../api/types";
import {
  OPTIONS,
  OUTDATED_WORDS,
  STORED_DETAILED,
  holdRequests,
  previewHero,
} from "./fixtures";

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
  initial?: CharacterChoices,
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
        initial={initial}
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

/** Выбор в карусели: точкой к варианту, затем «Выбрать» на его карточке. */
function pickCard(carousel: string, option: string | RegExp) {
  const group = screen.getByRole("group", { name: carousel });
  fireEvent.click(within(group).getByRole("button", { name: option }));
  fireEvent.click(
    within(group).getByRole("button", { name: /^Выбра(ть|но)$/ }),
  );
}

/** Карточка, показанная в карусели. */
const shownCard = (carousel: string) =>
  within(screen.getByRole("group", { name: carousel })).getByRole("article");

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
  pickCard("Раса", species);
  next();
  pickCard("Класс", klass);
  next();
  pickCard("Предыстория", background);
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

/** Доходит до «Итога»: там кнопка «Создать героя». */
function reachSummary() {
  reachBackstory();
  next();
}

const posts = (server: { calls: RecordedCall[] }, route: string) =>
  server.calls.filter((call) => `${call.method} ${call.path}` === route);

// --- шаги выбора ---

describe("путь по шагам", () => {
  test("начинается с расы; «Далее» ждёт выбора, «Назад» на первом шаге выходит к способам", () => {
    const { onBack } = renderDetailed();

    expect(progress()).toHaveTextContent("Шаг 1 из 10: Раса");
    expect(button("Далее")).toBeDisabled();
    pickCard("Раса", /Эльф/);
    expect(button("Далее")).toBeEnabled();
    click("Назад");

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  test("раса, класс и предыстория — карусели карточек с подробностями сервера", () => {
    renderDetailed();

    const species = screen.getByRole("group", { name: "Раса" });
    expect(species).toHaveAttribute("aria-roledescription", "карусель");
    expect(within(species).getByText("1 из 4")).toBeInTheDocument();
    pickCard("Раса", /Эльф/);
    expect(shownCard("Раса")).toHaveTextContent("Острые чувства");
    expect(shownCard("Раса")).toHaveTextContent("10,5 м");
    next();
    expect(progress()).toHaveTextContent("Шаг 2 из 10: Класс");
    pickCard("Класс", /Плут/);
    expect(shownCard("Класс")).toHaveTextContent(/Кость хитов\s*к8/);
    expect(shownCard("Класс")).toHaveTextContent("Скрытая атака");
    next();
    expect(progress()).toHaveTextContent("Шаг 3 из 10: Предыстория");
    pickCard("Предыстория", /Преступник/);
    expect(shownCard("Предыстория")).toHaveTextContent(
      /Инструмент\s*Воровские инструменты/,
    );
    expect(shownCard("Предыстория")).toHaveTextContent("Бдительный");
  });

  test("выбор расы из карусели: «Выбрать» отмечает показанную карточку", () => {
    renderDetailed();
    const species = screen.getByRole("group", { name: "Раса" });

    fireEvent.click(within(species).getByRole("button", { name: "Следующая" }));
    fireEvent.click(within(species).getByRole("button", { name: "Следующая" }));
    expect(shownCard("Раса")).toHaveAccessibleName("Дварф");
    fireEvent.click(within(species).getByRole("button", { name: "Выбрать" }));

    expect(
      within(species).getByRole("button", { name: "Выбрано" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(button("Далее")).toBeEnabled();
  });

  test("карточка класса: особенности с уровнем и кость хитов", () => {
    renderDetailed();
    pickCard("Раса", /Эльф/);
    next();

    pickCard("Класс", /Воин/);

    const fighter = shownCard("Класс");
    expect(fighter).toHaveAccessibleName("Воин");
    expect(fighter).toHaveTextContent(/Кость хитов\s*к10/);
    const features = within(
      within(fighter).getByRole("region", { name: "Особенности" }),
    ).getAllByRole("listitem");
    expect(features[0]).toHaveTextContent(/Боевой стиль.*1-й уровень/);
    expect(features[2]).toHaveTextContent(/Всплеск действий.*2-й уровень/);
    expect(features[2]).toHaveTextContent("действует в бою");
  });

  test("первый шаг называется «Раса»: устаревших слов на нём нет", () => {
    renderDetailed();
    pickCard("Раса", /Дварф/);

    expect(screen.getByRole("group", { name: "Раса" })).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("весь путь волшебника, все одиннадцать шагов: нигде нет ни «вид», ни футов", () => {
    renderDetailed();
    const shown: string[] = [];
    // Шаг проверяется после выбора на нём: выбор открывает пояснения и особенности.
    const checkStep = () => {
      shown.push(progress().textContent ?? "");
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
    };

    pickCard("Раса", /Эльф/);
    checkStep();
    next();
    pickCard("Класс", /Волшебник/);
    checkStep();
    next();
    pickCard("Предыстория", /Преступник/);
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
    next();
    checkStep(); // итог

    // Пока класс не выбран, шага заклинаний нет: шагов десять.
    expect(shown).toEqual([
      "Шаг 1 из 10: Раса",
      "Шаг 2 из 11: Класс",
      "Шаг 3 из 11: Предыстория",
      "Шаг 4 из 11: Характеристики",
      "Шаг 5 из 11: Бонус предыстории",
      "Шаг 6 из 11: Навыки и выборы класса",
      "Шаг 7 из 11: Заклинания",
      "Шаг 8 из 11: Снаряжение",
      "Шаг 9 из 11: Характер и внешность",
      "Шаг 10 из 11: История героя",
      "Шаг 11 из 11: Итог",
    ]);
  });

  test("«Назад» возвращает на прошлый шаг, выбор сохраняется", () => {
    renderDetailed();
    pickCard("Раса", /Дварф/);
    next();

    click("Назад");

    expect(progress()).toHaveTextContent("Шаг 1 из 10: Раса");
    // Карусель открывается на выбранной карточке, и она отмечена.
    expect(shownCard("Раса")).toHaveAccessibleName("Дварф");
    expect(
      within(shownCard("Раса")).getByRole("button", { name: "Выбрано" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  test("переход шагов сохраняет черновик: назад к расе и классу и снова вперёд", () => {
    renderDetailed();
    chooseOrigin(/Дварф/, /Волшебник/, /Прислужник/);
    expect(progress()).toHaveTextContent("Характеристики");

    click("Назад");
    expect(shownCard("Предыстория")).toHaveAccessibleName("Прислужник");
    click("Назад");
    expect(shownCard("Класс")).toHaveAccessibleName("Волшебник");
    click("Назад");
    expect(shownCard("Раса")).toHaveAccessibleName("Дварф");
    next();
    next();
    next();

    expect(progress()).toHaveTextContent("Шаг 4 из 11: Характеристики");
  });

  test("пока шаг не закончен, под кнопками подсказка, что делать", () => {
    renderDetailed();

    expect(
      screen.getByText(/закончите выбор на этом шаге/),
    ).toBeInTheDocument();
    pickCard("Раса", /Эльф/);
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
    pickCard("Раса", /Эльф/);

    next();
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    click("Назад");

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });

  test("шаг заклинаний появляется только у заклинателя", () => {
    renderDetailed();
    pickCard("Раса", /Эльф/);
    next();

    pickCard("Класс", /Волшебник/);
    expect(progress()).toHaveTextContent("Шаг 2 из 11");
    pickCard("Класс", /Плут/);
    expect(progress()).toHaveTextContent("Шаг 2 из 10");
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
    pickCard("Класс", /Воин/);
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
    next(); // итог

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
    next(); // итог

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
    next(); // итог

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

  test("у навыка на выбор (Умелость) есть «Подробнее» выбранного навыка, как у списков с отметками", () => {
    // Воин: компетентности нет, а «Медицины» нет среди навыков класса — кнопка только у Умелости.
    openSkills(/Человек/, /Воин/);
    expect(
      screen.queryByRole("button", { name: /^Подробнее: Медицина/ }),
    ).toBeNull();

    fireEvent.change(screen.getByLabelText(/Навык на выбор/), {
      target: { value: "medicine" },
    });
    const more = screen.getAllByRole("button", { name: "Подробнее: Медицина" });
    expect(more).toHaveLength(1);
    fireEvent.click(more[0]);

    const card = screen.getByRole("article", { name: "Медицина" });
    expect(card).toHaveTextContent("Перевязать рану, узнать болезнь.");
    expect(card).toHaveTextContent(/Характеристика\s*Мудрость/);
  });

  test("боевой стиль: подпись варианта — только название, описание одно — в «Подробнее»", () => {
    openSkills(/Эльф/, /Воин/);
    const styles = screen.getByRole("radiogroup", { name: "Боевой стиль" });
    // Описание не повторяется: до «Подробнее» его на экране нет.
    expect(styles).not.toHaveTextContent("+2 к дальним атакам.");

    fireEvent.click(
      within(styles).getByRole("button", { name: "Подробнее: Стрельба" }),
    );

    expect(within(styles).getAllByText("+2 к дальним атакам.")).toHaveLength(1);
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
    next(); // итог

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

// --- подробные карточки вариантов списка ---

describe("«Подробнее» у вариантов списка", () => {
  test("навык класса: характеристика и примеры; нажатие не отмечает флажок", () => {
    renderDetailed();
    chooseOrigin();
    standardArray();
    bonusDexCon();
    const skills = screen.getByRole("group", { name: /Навыки класса/ });

    const more = within(skills).getByRole("button", {
      name: "Подробнее: Акробатика",
    });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(within(skills).queryByRole("article")).toBeNull();
    fireEvent.click(more);

    const card = within(skills).getByRole("article", { name: "Акробатика" });
    expect(card).toHaveTextContent(/Характеристика\s*Ловкость/);
    expect(card).toHaveTextContent("Удержать равновесие на карнизе, кувырок.");
    expect(classSkill(/Акробатика/)).not.toBeChecked();
    expect(classSkill(/Акробатика/)).toHaveAccessibleName(/^Акробатика$/);
  });

  test("заклинание: школа, время, дистанция в метрах, длительность и описание", () => {
    renderDetailed();
    chooseOrigin(/Эльф/, /Волшебник/);
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    next();
    const spells = screen.getByRole("group", { name: /^Заклинания/ });

    fireEvent.click(
      within(spells).getByRole("button", { name: "Подробнее: Сон" }),
    );

    const card = within(spells).getByRole("article", { name: "Сон" });
    expect(card).toHaveTextContent(/Школа\s*Очарование/);
    expect(card).toHaveTextContent(/Время сотворения\s*Действие/);
    expect(card).toHaveTextContent(/Дистанция\s*Близко, до 18 м/);
    expect(card).toHaveTextContent(/Длительность\s*1 минута, концентрация/);
    expect(card).toHaveTextContent("Усыпляет.");
  });

  test("все карточки заговоров и заклинаний открыты — нигде нет футов", () => {
    renderDetailed();
    chooseOrigin(/Эльф/, /Волшебник/);
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Магия/);
    tick(/Навыки класса/, /История/);
    next();

    for (const more of screen.getAllByRole("button", { name: /^Подробнее/ })) {
      fireEvent.click(more);
    }

    expect(screen.getAllByRole("article")).toHaveLength(6);
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("боевой стиль: описание из подробностей; выбор стиля не меняется", () => {
    renderDetailed();
    chooseOrigin(/Эльф/, /Воин/);
    standardArray();
    bonusDexCon();
    const styles = screen.getByRole("radiogroup", { name: "Боевой стиль" });

    fireEvent.click(
      within(styles).getByRole("button", { name: "Подробнее: Стрельба" }),
    );

    expect(
      within(styles).getByRole("article", { name: "Стрельба" }),
    ).toHaveTextContent("+2 к дальним атакам.");
    expect(
      within(styles).getByRole("radio", { name: /Стрельба/ }),
    ).not.toBeChecked();
  });
});

// --- итог ---

const section = (name: string) => screen.getByRole("region", { name });
/** «Изменить» раздела: видимый текст «Изменить», имя для чтения с экрана — с разделом. */
const editIn = (name: string) =>
  fireEvent.click(
    within(section(name)).getByRole("button", { name: `Изменить: ${name}` }),
  );

const ROGUE_SECTIONS = [
  "Раса",
  "Класс",
  "Предыстория",
  "Характеристики",
  "Бонус предыстории",
  "Навыки и выборы класса",
  "Снаряжение",
  "Характер и внешность",
  "История героя",
];

describe("итог", () => {
  test("после истории героя — «Итог»: выбор по разделам, у каждого «Изменить»", () => {
    renderDetailed();
    reachSummary();

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(section("Раса")).toHaveTextContent("Эльф");
    expect(section("Класс")).toHaveTextContent("Плут");
    expect(section("Предыстория")).toHaveTextContent("Преступник");
    expect(section("Характеристики")).toHaveTextContent("Стандартный набор");
    expect(section("Характеристики")).toHaveTextContent(
      "Сила 8, Ловкость 15, Телосложение 14, Интеллект 13, Мудрость 10, Харизма 12",
    );
    expect(section("Бонус предыстории")).toHaveTextContent(
      "Ловкость +2, Телосложение +1",
    );
    const skills = section("Навыки и выборы класса");
    expect(skills).toHaveTextContent(
      "Навыки класса: Акробатика, Обман, Проницательность, Убеждение",
    );
    expect(skills).toHaveTextContent(
      "От предыстории и расы: Ловкость рук, Скрытность, Внимательность",
    );
    expect(skills).toHaveTextContent(
      "Компетентность: Скрытность, Внимательность",
    );
    expect(section("Снаряжение")).toHaveTextContent(
      "Кинжал × 2, Воровские инструменты × 1, Кинжал × 1",
    );
    expect(section("Характер и внешность")).toHaveTextContent("Имя: Лира");
    expect(section("История героя")).toHaveTextContent("Пока пусто.");
    for (const name of ROGUE_SECTIONS) {
      const edit = within(section(name)).getByRole("button", {
        name: `Изменить: ${name}`,
      });
      expect(edit, name).toHaveTextContent(/^Изменить$/);
      expect(edit, name).toBeEnabled();
      // Общая кнопка: её область нажатия не меньше --tap-size (Carousel.test, e2e/layout.ts).
      expect(edit, name).toHaveClass("ui-button");
    }
    // Имена кнопок различаются: диктор читает их списком без повторов.
    const names = screen
      .getAllByRole("button", { name: /^Изменить/ })
      .map((edit) => edit.getAttribute("aria-label"));
    expect(new Set(names).size).toBe(ROGUE_SECTIONS.length);
    // У плута нет магии: раздела заклинаний нет.
    expect(screen.queryByRole("region", { name: "Заклинания" })).toBeNull();
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("«Создать героя» только на «Итоге», «Далее» там нет", () => {
    renderDetailed();
    reachBackstory();
    expect(screen.queryByRole("button", { name: "Создать героя" })).toBeNull();

    next();

    expect(button("Создать героя")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Далее" })).toBeNull();
  });

  test("у волшебника в итоге заговоры и заклинания; описание героя — заполненными полями", () => {
    renderDetailed();
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
    typeInto("Внешность", "Седая прядь.");
    next();
    typeInto("Предыстория героя", "Ученица мага.");
    next();

    expect(section("Заклинания")).toHaveTextContent(
      "Заговоры: Огненная стрела, Свет",
    );
    expect(section("Заклинания")).toHaveTextContent("Заклинания: Щит, Сон");
    const persona = section("Характер и внешность");
    expect(persona).toHaveTextContent("Имя: Мирра");
    expect(persona).toHaveTextContent("Внешность: Седая прядь.");
    expect(persona).not.toHaveTextContent("Идеалы");
    expect(section("История героя")).toHaveTextContent("Ученица мага.");
  });

  test("«Изменить» ведёт на шаг раздела; выбор цел, «Далее» снова доводит до итога", () => {
    renderDetailed();
    reachSummary();

    editIn("Раса");

    expect(progress()).toHaveTextContent("Шаг 1 из 10: Раса");
    expect(shownCard("Раса")).toHaveAccessibleName("Эльф");
    for (let i = 0; i < 9; i++) next();
    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(section("Характер и внешность")).toHaveTextContent("Имя: Лира");
    expect(button("Создать героя")).toBeEnabled();
  });

  test("после «Изменить» — короткий путь «К итогу»: не нужно проходить все шаги заново", () => {
    renderDetailed();
    reachSummary();

    editIn("Предыстория");
    expect(progress()).toHaveTextContent("Шаг 3 из 10: Предыстория");
    click("К итогу");

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(section("Характер и внешность")).toHaveTextContent("Имя: Лира");
    expect(button("Создать героя")).toBeEnabled();
  });

  test("«К итогу» ждёт законченного шага, как «Далее»", () => {
    renderDetailed();
    reachSummary();

    editIn("Навыки и выборы класса");
    expect(button("К итогу")).toBeEnabled();
    tick(/Навыки класса/, /Акробатика/); // снял отметку: навыков меньше нормы

    expect(button("К итогу")).toBeDisabled();
    expect(button("Далее")).toBeDisabled();
  });

  test("при первом проходе «К итогу» нет: итога ещё не было", () => {
    renderDetailed();
    chooseOrigin();

    expect(screen.queryByRole("button", { name: "К итогу" })).toBeNull();
  });

  test("«Изменить героя»: с «Итога» в раздел и сразу обратно", () => {
    renderDetailed({}, OPTIONS, STORED_DETAILED);

    editIn("Раса");
    click("К итогу");

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(section("Раса")).toHaveTextContent("Эльф");
  });

  test("правка через «Изменить» уходит на сервер", async () => {
    const { server, onCreated } = renderDetailed();
    reachSummary();

    editIn("Характер и внешность");
    expect(progress()).toHaveTextContent("Характер и внешность");
    typeInto("Имя", "Лирой");
    next();
    next();
    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toMatchObject({
      choices: { name: "Лирой", species: "elf", class_id: "rogue" },
    });
  });
});

describe("«Изменить героя»: мастер с прежним выбором", () => {
  test("открывается сразу на «Итоге» с прежним выбором", () => {
    renderDetailed({}, OPTIONS, STORED_DETAILED);

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(section("Раса")).toHaveTextContent("Эльф");
    expect(section("Класс")).toHaveTextContent("Плут");
    expect(section("Характеристики")).toHaveTextContent("Покупка очков");
    expect(section("Характеристики")).toHaveTextContent(
      "Сила 8, Ловкость 15, Телосложение 14, Интеллект 13, Мудрость 10, Харизма 12",
    );
    expect(section("Характер и внешность")).toHaveTextContent(
      "Замысел: Вор-неудачник",
    );
    expect(section("История героя")).toHaveTextContent("Родилась в порту.");
    expect(button("Создать героя")).toBeEnabled();
  });

  test("отправка без правок шлёт тот же выбор", async () => {
    const { server, onCreated } = renderDetailed({}, OPTIONS, STORED_DETAILED);

    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toEqual({
      mode: "detailed",
      choices: {
        ...STORED_DETAILED,
        persona: { concept: "Вор-неудачник", backstory: "Родилась в порту." },
      },
    });
  });

  test("«Назад» с «Итога» идёт по шагам и ничего не теряет", () => {
    const { onBack } = renderDetailed({}, OPTIONS, STORED_DETAILED);

    click("Назад");
    expect(progress()).toHaveTextContent("История героя");
    expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
      "Родилась в порту.",
    );
    click("Назад");
    expect(screen.getByLabelText("Имя")).toHaveValue("Лира");
    for (let i = 0; i < 6; i++) click("Назад");
    expect(progress()).toHaveTextContent("Шаг 2 из 10: Класс");
    expect(shownCard("Класс")).toHaveAccessibleName("Плут");
    click("Назад");
    expect(shownCard("Раса")).toHaveAccessibleName("Эльф");
    expect(onBack).not.toHaveBeenCalled();
  });

  test("выбор, которого правила уже не допускают: раздел помечен, создать нельзя", () => {
    renderDetailed({}, OPTIONS, { ...STORED_DETAILED, class_id: "bard" });

    expect(progress()).toHaveTextContent("Итог");
    expect(section("Класс")).toHaveTextContent("Пока пусто.");
    expect(section("Класс")).toHaveTextContent(
      "Выбор на этом шаге не закончен.",
    );
    expect(section("Раса")).not.toHaveTextContent("не закончен");
    expect(button("Создать героя")).toBeDisabled();
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
    next(); // итог

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
    reachSummary();

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
    click("Назад");
    expect(screen.getByLabelText("Имя")).toHaveValue("Лира");
    next();
    next();
    click("Создать героя");

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)).toHaveLength(2);
  });

  test("пока идёт отправка, кнопка заблокирована и второй запрос не уходит", async () => {
    const { gated, server, onCreated } = renderDetailed();
    reachSummary();
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
    reachSummary();

    click("Создать героя");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Игра уже началась");
    // Списка проблем нет; строки «Итога» — не он.
    expect(within(alert).queryByRole("listitem")).toBeNull();
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
    // К «Итогу» и созданию не уйти, пока мастер пишет.
    expect(button("Далее")).toBeDisabled();
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

// --- «Итог», «Изменить» и смена класса ---

/** Строки раздела «Итога» по порядку. */
const linesOf = (name: string) =>
  within(section(name))
    .queryAllByRole("listitem")
    .map((item) => item.textContent);

/** Прежний выбор волшебника: эльф, преступник, два заговора и два заклинания. */
const STORED_WIZARD: CharacterChoices = {
  ...STORED_DETAILED,
  name: "Мирра",
  class_id: "wizard",
  skills: ["arcana", "history"],
  expertise: [],
  cantrips: ["fire_bolt", "light"],
  prepared_spells: ["shield", "sleep"],
};

describe("итог: «Изменить» у каждого раздела", () => {
  test.each(ROGUE_SECTIONS.map((name, index) => [name, index + 1] as const))(
    "«Изменить» у «%s» ведёт на шаг %i из 10",
    (name, number) => {
      renderDetailed({}, OPTIONS, STORED_DETAILED);

      editIn(name);

      expect(progress()).toHaveTextContent(`Шаг ${number} из 10: ${name}`);
    },
  );

  test("у волшебника «Изменить» у «Заклинания» — на шаг 7 из 11, отметки прежние", () => {
    renderDetailed({}, OPTIONS, STORED_WIZARD);
    expect(progress()).toHaveTextContent("Шаг 11 из 11: Итог");
    expect(linesOf("Заклинания")).toEqual([
      "Заговоры: Огненная стрела, Свет",
      "Заклинания: Щит, Сон",
    ]);

    editIn("Заклинания");

    expect(progress()).toHaveTextContent("Шаг 7 из 11: Заклинания");
    for (const name of [/Огненная стрела/, /Свет/]) {
      expect(
        within(screen.getByRole("group", { name: /^Заговоры/ })).getByRole(
          "checkbox",
          { name },
        ),
      ).toBeChecked();
    }
    for (const name of [/Щит/, /Сон/]) {
      expect(
        within(screen.getByRole("group", { name: /^Заклинания/ })).getByRole(
          "checkbox",
          { name },
        ),
      ).toBeChecked();
    }
  });

  test("«Назад» с «Итога» волшебника доходит до заклинаний и ничего не теряет", () => {
    renderDetailed({}, OPTIONS, STORED_WIZARD);

    for (let i = 0; i < 4; i++) click("Назад");

    expect(progress()).toHaveTextContent("Шаг 7 из 11: Заклинания");
    expect(screen.getAllByRole("checkbox", { checked: true })).toHaveLength(4);
    for (let i = 0; i < 4; i++) next();
    expect(progress()).toHaveTextContent("Шаг 11 из 11: Итог");
    expect(button("Создать героя")).toBeEnabled();
  });
});

describe("итог: строки разделов для разных героев", () => {
  test("человек-воин: навык на выбор и боевой стиль — отдельными строками", () => {
    renderDetailed({}, OPTIONS, {
      ...STORED_DETAILED,
      species: "human",
      class_id: "fighter",
      skills: ["athletics", "history"],
      extra_skill: "medicine",
      expertise: [],
      fighting_style: "defense",
    });

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(linesOf("Раса")).toEqual(["Человек"]);
    expect(linesOf("Класс")).toEqual(["Воин"]);
    expect(linesOf("Навыки и выборы класса")).toEqual([
      "Навыки класса: Атлетика, История",
      "Навык на выбор: Медицина",
      "От предыстории и расы: Ловкость рук, Скрытность",
      "Боевой стиль: Оборона",
    ]);
    expect(screen.queryByText("Выбор на этом шаге не закончен.")).toBeNull();
    expect(button("Создать героя")).toBeEnabled();
  });

  test("бонус +1/+1/+1 — три характеристики по единице", () => {
    renderDetailed({}, OPTIONS, {
      ...STORED_DETAILED,
      background_bonus: { dex: 1, con: 1, int: 1 },
    });

    expect(linesOf("Бонус предыстории")).toEqual([
      "Ловкость +1, Телосложение +1, Интеллект +1",
    ]);
    expect(button("Создать героя")).toBeEnabled();
  });

  test("заклинатель без заговоров: в разделе «Заклинания» только заклинания", () => {
    renderDetailed({}, OPTIONS, {
      ...STORED_DETAILED,
      class_id: "ranger",
      skills: ["athletics", "insight", "nature"],
      expertise: [],
      cantrips: [],
      prepared_spells: ["hunters_mark", "cure_wounds"],
    });

    expect(progress()).toHaveTextContent("Шаг 11 из 11: Итог");
    expect(linesOf("Заклинания")).toEqual([
      "Заклинания: Метка охотника, Лечение ран",
    ]);
    expect(button("Создать героя")).toBeEnabled();
  });
});

describe("смена класса через «Изменить» на «Итоге»", () => {
  test("плут → волшебник: шагов становится 11, позиция на шаге класса цела, заклинания встают седьмым шагом", async () => {
    const { server, onCreated } = renderDetailed({}, OPTIONS, STORED_DETAILED);
    editIn("Класс");
    expect(progress()).toHaveTextContent("Шаг 2 из 10: Класс");

    pickCard("Класс", /Волшебник/);
    expect(progress()).toHaveTextContent("Шаг 2 из 11: Класс");
    pickCard("Класс", /Плут/);
    expect(progress()).toHaveTextContent("Шаг 2 из 10: Класс");
    pickCard("Класс", /Волшебник/);
    expect(progress()).toHaveTextContent("Шаг 2 из 11: Класс");

    next();
    expect(progress()).toHaveTextContent("Шаг 3 из 11: Предыстория");
    expect(shownCard("Предыстория")).toHaveAccessibleName("Преступник");
    next();
    next();
    next();
    expect(progress()).toHaveTextContent("Шаг 6 из 11: Навыки и выборы класса");
    // Навык плута, которого у волшебника нет, снят; общий (Проницательность) остался.
    expect(classSkill(/Проницательность/)).toBeChecked();
    tick(/Навыки класса/, /История/);
    next();
    expect(progress()).toHaveTextContent("Шаг 7 из 11: Заклинания");
    tick(/Заговоры/, /Огненная стрела/);
    tick(/Заговоры/, /Свет/);
    tick(/Заклинания/, /Щит/);
    tick(/Заклинания/, /Сон/);
    next();
    expect(progress()).toHaveTextContent("Шаг 8 из 11: Снаряжение");
    next();
    expect(screen.getByLabelText("Имя")).toHaveValue("Лира");
    next();
    expect(screen.getByLabelText("Предыстория героя")).toHaveValue(
      "Родилась в порту.",
    );
    next();

    expect(progress()).toHaveTextContent("Шаг 11 из 11: Итог");
    expect(linesOf("Класс")).toEqual(["Волшебник"]);
    expect(linesOf("Заклинания")).toEqual([
      "Заговоры: Огненная стрела, Свет",
      "Заклинания: Щит, Сон",
    ]);
    expect(screen.queryByText("Выбор на этом шаге не закончен.")).toBeNull();
    click("Создать героя");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toMatchObject({
      mode: "detailed",
      choices: {
        name: "Лира",
        class_id: "wizard",
        skills: ["history", "insight"],
        expertise: [],
        cantrips: ["fire_bolt", "light"],
        prepared_spells: ["shield", "sleep"],
      },
    });
  });

  test("волшебник → воин: шаг заклинаний пропадает без пропуска номеров, заклинания на сервер не уходят", async () => {
    const { server, onCreated } = renderDetailed({}, OPTIONS, STORED_WIZARD);
    editIn("Класс");
    expect(progress()).toHaveTextContent("Шаг 2 из 11: Класс");

    pickCard("Класс", /Воин/);
    expect(progress()).toHaveTextContent("Шаг 2 из 10: Класс");
    for (let i = 0; i < 4; i++) next();
    expect(progress()).toHaveTextContent("Шаг 6 из 10: Навыки и выборы класса");
    tick(/Навыки класса/, /Атлетика/);
    pick("Боевой стиль", /Оборона/);
    next();
    expect(progress()).toHaveTextContent("Шаг 7 из 10: Снаряжение");
    for (let i = 0; i < 3; i++) next();

    expect(progress()).toHaveTextContent("Шаг 10 из 10: Итог");
    expect(screen.queryByRole("region", { name: "Заклинания" })).toBeNull();
    expect(linesOf("Навыки и выборы класса")).toContain(
      "Боевой стиль: Оборона",
    );
    click("Создать героя");
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server, CREATE)[0].body).toMatchObject({
      choices: {
        name: "Мирра",
        class_id: "fighter",
        skills: ["athletics", "history"],
        fighting_style: "defense",
        cantrips: [],
        prepared_spells: [],
      },
    });
  });
});

describe("«Подробнее» у недоступных вариантов и у компетентности", () => {
  test("навыков набрано до предела: у недоступного навыка «Подробнее» всё равно раскрывается", () => {
    renderDetailed();
    chooseOrigin();
    standardArray();
    bonusDexCon();
    for (const name of [
      /Акробатика/,
      /Обман/,
      /Проницательность/,
      /Убеждение/,
    ]) {
      tick(/Навыки класса/, name);
    }
    expect(classSkill(/Анализ/)).toBeDisabled();
    const skills = screen.getByRole("group", { name: /Навыки класса/ });

    fireEvent.click(
      within(skills).getByRole("button", { name: "Подробнее: Анализ" }),
    );

    expect(
      within(skills).getByRole("article", { name: "Анализ" }),
    ).toHaveTextContent(/Характеристика\s*Интеллект/);
    expect(classSkill(/Анализ/)).not.toBeChecked();
  });

  test("компетентность: у каждого навыка героя своя карточка «Подробнее»", () => {
    renderDetailed();
    chooseOrigin();
    standardArray();
    bonusDexCon();
    tick(/Навыки класса/, /Акробатика/);
    const expertise = screen.getByRole("group", { name: /Компетентность/ });

    fireEvent.click(
      within(expertise).getByRole("button", { name: "Подробнее: Скрытность" }),
    );

    expect(
      within(expertise).getByRole("article", { name: "Скрытность" }),
    ).toHaveTextContent("Подкрасться к часовому, спрятаться.");
    expect(
      within(expertise).getByRole("checkbox", { name: /Скрытность/ }),
    ).not.toBeChecked();
  });
});
