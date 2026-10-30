import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft, DraftReport, Finding } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  clueOf,
  draftOf,
  exitOf,
  featureOf,
  findingOf,
  itemOf,
  locationOf,
  monsterOf,
  npcOf,
  openStep,
  relationOf,
  reportOf,
  revelationOf,
} from "./fixtures";

const VALIDATE = "/api/drafts/d1/validate";

const CRYPT = locationOf({ id: "crypt", name: "Склеп" });
const draft = (overrides: Partial<Draft> = {}) =>
  draftOf({ version: 5, locations: [CRYPT], ...overrides });

const REPORT = reportOf({
  errors: [
    findingOf(),
    findingOf({
      code: "dangling_ref",
      message: "to: локации «cellar» нет",
      ref: "location:crypt/exit:down",
      step: "locations",
    }),
    findingOf({
      code: "too_few_clues",
      message: "у откровения меньше трёх улик",
      ref: "revelation:smuggler",
      step: "clues",
    }),
  ],
  warnings: [
    findingOf({
      code: "duration_mismatch",
      message: "оценка времени расходится с целью",
      ref: "adventure",
      step: null,
    }),
  ],
  score: 67,
  estimated_minutes: 150,
  readiness_percent: 71,
});

function openReview(report: DraftReport = REPORT, routes: Routes = {}) {
  return openStep(draft(), "Проверка", {
    [`POST ${VALIDATE}`]: { json: report },
    ...routes,
  });
}

const nav = () =>
  within(screen.getByRole("navigation", { name: "Шаги приключения" }));

describe("экран «Проверка»", () => {
  test("при входе — проверка черновика: готовность полоской, балл и время игры", async () => {
    const { server, form } = await openReview();

    const bar = await within(form).findByRole("progressbar", {
      name: "Готовность",
    });
    expect(bar).toHaveAttribute("aria-valuenow", "71");
    expect(form).toHaveTextContent("Готовность: 71%");
    expect(form).toHaveTextContent("Балл: 67 из 100");
    expect(form).toHaveTextContent("Время игры: около 2 ч 30 мин");
    expect(callsTo(server, "POST", VALIDATE)).toHaveLength(1);
  });

  test("ошибки и предупреждения: текст сервера как есть, сущность и шаг", async () => {
    const { form } = await openReview();

    const errors = await within(form).findByRole("list", { name: "Ошибки" });
    const items = within(errors).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    // «Финал не выбран» — текст сервера, клиент его не переписывает.
    expect(items[0]).toHaveTextContent("Финал не выбран");
    expect(items[0]).toHaveTextContent("приключение");
    expect(items[1]).toHaveTextContent("to: локации «cellar» нет");
    expect(items[1]).toHaveTextContent("локация «Склеп» → выход «down»");
    expect(items[2]).toHaveTextContent("откровение «smuggler»");
    expect(
      within(items[2]).getByRole("button", { name: "Перейти к шагу «Улики»" }),
    ).toBeInTheDocument();

    const warnings = within(form).getByRole("list", { name: "Предупреждения" });
    expect(warnings).toHaveTextContent("оценка времени расходится с целью");
    // Находка без шага — без ссылки.
    expect(within(warnings).queryByRole("button")).toBeNull();
  });

  test("ссылка ошибки ведёт на её шаг: «Перейти к шагу «Карта»» открывает «Карту»", async () => {
    const { form } = await openReview();

    const errors = await within(form).findByRole("list", { name: "Ошибки" });
    fireEvent.click(
      within(errors).getAllByRole("button", {
        name: "Перейти к шагу «Карта»",
      })[0],
    );

    expect(screen.getByRole("region", { name: "Карта" })).toBeInTheDocument();
    expect(nav().getByRole("button", { name: /Карта/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  test("нет находок — «Ошибок нет» и «Предупреждений нет»", async () => {
    const { form } = await openReview(reportOf());

    expect(await within(form).findByText("Ошибок нет.")).toBeInTheDocument();
    expect(within(form).getByText("Предупреждений нет.")).toBeInTheDocument();
    expect(form).toHaveTextContent("Время игры: около 2 ч 30 мин");
  });

  test("«Проверить снова» и каждый вход на экран просят свежий отчёт", async () => {
    const { server, form } = await openReview();
    await within(form).findByRole("list", { name: "Ошибки" });

    fireEvent.click(
      within(form).getByRole("button", { name: "Проверить снова" }),
    );
    await vi.waitFor(() =>
      expect(callsTo(server, "POST", VALIDATE)).toHaveLength(2),
    );
    fireEvent.click(nav().getByRole("button", { name: /Карта/ }));
    fireEvent.click(nav().getByRole("button", { name: /Проверка/ }));

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", VALIDATE)).toHaveLength(3),
    );
  });

  test("сбой проверки — текст сервера и «Повторить»", async () => {
    let attempt = 0;
    const { form } = await openReview(REPORT, {
      [`POST ${VALIDATE}`]: () =>
        attempt++ === 0
          ? { status: 500, json: { detail: "Проверка недоступна." } }
          : { json: REPORT },
    });

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Проверка недоступна.",
    );
    fireEvent.click(within(form).getByRole("button", { name: "Повторить" }));

    expect(
      await within(form).findByRole("list", { name: "Ошибки" }),
    ).toBeInTheDocument();
  });

  test("короткая и ровная оценка времени: «около 45 мин», «около 2 ч»", async () => {
    const short = await openReview(reportOf({ estimated_minutes: 45 }));
    expect(
      await within(short.form).findByText("Время игры: около 45 мин"),
    ).toBeInTheDocument();
  });

  test("ровные часы — без минут", async () => {
    const { form } = await openReview(reportOf({ estimated_minutes: 120 }));

    expect(
      await within(form).findByText("Время игры: около 2 ч"),
    ).toBeInTheDocument();
  });
});

// Шаг находки (`step` отчёта) → экран редактора: все двенадцать шагов генерации.
describe("ссылка находки ведёт на экран её шага", () => {
  test.each([
    ["concept", "Замысел"],
    ["world", "Мир"],
    ["pantheon", "Боги"],
    ["factions", "Фракции"],
    ["plot", "Сюжет"],
    ["locations", "Карта"],
    ["location_contents", "Наполнение"],
    ["npcs", "Персонажи"],
    ["relations", "Связи"],
    ["encounters", "Бои и боссы"],
    ["items", "Предметы и награды"],
    ["clues", "Улики"],
  ] as const)("ошибка шага %s → экран «%s»", async (step, label) => {
    const { form } = await openReview(
      reportOf({
        errors: [findingOf({ code: "some_error", message: "ошибка", step })],
      }),
    );

    const errors = await within(form).findByRole("list", { name: "Ошибки" });
    fireEvent.click(
      within(errors).getByRole("button", {
        name: `Перейти к шагу «${label}»`,
      }),
    );

    expect(screen.getByRole("region", { name: label })).toBeInTheDocument();
    expect(
      nav().getByRole("button", { name: new RegExp(`^${label}`) }),
    ).toHaveAttribute("aria-current", "step");
    expect(screen.queryByRole("region", { name: "Проверка" })).toBeNull();
  });

  test("ссылка предупреждения тоже ведёт на шаг: «нет интересных мест» → «Наполнение»", async () => {
    const { form } = await openReview(
      reportOf({
        warnings: [
          findingOf({
            code: "no_features",
            message: "в локации нет ни одного интересного места",
            ref: "location:crypt",
            step: "location_contents",
          }),
        ],
      }),
    );

    const warnings = await within(form).findByRole("list", {
      name: "Предупреждения",
    });
    expect(warnings).toHaveTextContent("локация «Склеп»");
    fireEvent.click(
      within(warnings).getByRole("button", {
        name: "Перейти к шагу «Наполнение»",
      }),
    );

    expect(
      screen.getByRole("region", { name: "Наполнение" }),
    ).toBeInTheDocument();
  });
});

describe("где найдено: сущность словами по ссылке находки", () => {
  test("свой монстр, персонаж, предмет и улика откровения; «Старт не выбран» — как прислал сервер", async () => {
    const named = draft({
      monsters: [monsterOf({ id: "crypt-horror", name: "Ужас склепа" })],
      items: [itemOf({ id: "crypt-key", name: "Ключ от склепа" })],
      npcs: [npcOf({ id: "marta", name: "Марта" })],
    });
    const { form } = await openStep(named, "Проверка", {
      [`POST ${VALIDATE}`]: {
        json: reportOf({
          errors: [
            findingOf({ message: "Старт не выбран", step: "locations" }),
            findingOf({
              code: "custom_monster_xp",
              message:
                "опыт своего монстра должен соответствовать его cr: допустимы cr и опыт 0.25 → 50, 1 → 200",
              ref: "monster:crypt-horror",
              step: "encounters",
            }),
            findingOf({
              code: "too_few_clues",
              message: "у откровения меньше трёх улик",
              ref: "revelation:smuggler/clue:seal",
              step: "clues",
            }),
          ],
          warnings: [
            findingOf({
              code: "dangling_ref",
              message: "держатель предмета не найден",
              ref: "item:crypt-key",
              step: "items",
            }),
            findingOf({
              code: "npc_no_location",
              message: "персонаж нигде не находится",
              ref: "npc:marta",
              step: "npcs",
            }),
          ],
        }),
      },
    });

    const errors = await within(form).findByRole("list", { name: "Ошибки" });
    const [start, xp, clue] = within(errors).getAllByRole("listitem");
    expect(start).toHaveTextContent("Старт не выбран");
    expect(
      within(start).getByRole("button", { name: "Перейти к шагу «Карта»" }),
    ).toBeInTheDocument();
    expect(xp).toHaveTextContent("монстр «Ужас склепа»");
    expect(
      within(xp).getByRole("button", {
        name: "Перейти к шагу «Бои и боссы»",
      }),
    ).toBeInTheDocument();
    expect(clue).toHaveTextContent("откровение «smuggler» → улика «seal»");

    const [key, marta] = within(
      within(form).getByRole("list", { name: "Предупреждения" }),
    ).getAllByRole("listitem");
    expect(key).toHaveTextContent("предмет «Ключ от склепа»");
    expect(marta).toHaveTextContent("персонаж «Марта»");
  });
});

// Ссылка находки ведёт не только на шаг, но и к сущности — экран раскрывает её (на «Наполнении» —
// выбирает локацию, на «Связях» — снимает фильтр) и прокручивает к ней, фокус — на её группе полей.
describe("ссылка находки раскрывает и прокручивает к сущности", () => {
  let scrolled: Element[] = [];

  beforeEach(() => {
    scrolled = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
  });

  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });

  const HALL = locationOf({ id: "hall", name: "Зал" });

  async function follow(
    target: Draft,
    finding: { ref: string; step: Finding["step"] },
    label: string,
  ) {
    const { form } = await openStep(target, "Проверка", {
      [`POST ${VALIDATE}`]: {
        json: reportOf({
          errors: [
            findingOf({ code: "some_error", message: "ошибка", ...finding }),
          ],
        }),
      },
    });
    const errors = await within(form).findByRole("list", { name: "Ошибки" });
    fireEvent.click(
      within(errors).getByRole("button", { name: `Перейти к шагу «${label}»` }),
    );
    return screen.getByRole("region", { name: label });
  }

  test("выход локации на «Карте»: прокрутка и фокус — на группе этого выхода", async () => {
    const panel = await follow(
      draftOf({
        locations: [
          HALL,
          locationOf({
            id: "crypt",
            name: "Склеп",
            exits: [exitOf({ id: "down", direction: "вниз", to: "hall" })],
          }),
        ],
      }),
      { ref: "location:crypt/exit:down", step: "locations" },
      "Карта",
    );

    const exit = within(
      within(panel).getByRole("group", { name: "Склеп" }),
    ).getByRole("group", { name: "вниз → Зал" });
    expect(document.activeElement).toBe(exit);
    expect(scrolled.at(-1)).toBe(exit);
  });

  test("деталь второй локации на «Наполнении»: экран выбирает её локацию и ведёт к детали", async () => {
    const panel = await follow(
      draftOf({
        locations: [
          HALL,
          locationOf({
            id: "crypt",
            name: "Склеп",
            features: [featureOf({ id: "coffin", name: "Гроб" })],
          }),
        ],
      }),
      { ref: "location:crypt/feature:coffin", step: "location_contents" },
      "Наполнение",
    );

    expect(
      within(panel).getByRole("combobox", { name: "Локация" }),
    ).toHaveValue("crypt");
    const coffin = within(panel).getByRole("group", { name: "Гроб" });
    expect(document.activeElement).toBe(coffin);
    expect(scrolled.at(-1)).toBe(coffin);
  });

  test("улика откровения на «Уликах»: фокус на улике внутри своего откровения", async () => {
    const panel = await follow(
      draftOf({
        revelations: [
          revelationOf({ id: "first", clues: [clueOf({ id: "ash" })] }),
          revelationOf({
            id: "smuggler",
            clues: [clueOf({ id: "torn-letter" }), clueOf({ id: "seal" })],
          }),
        ],
      }),
      { ref: "revelation:smuggler/clue:seal", step: "clues" },
      "Улики",
    );

    const seal = within(
      within(panel).getByRole("group", { name: "Откровение smuggler" }),
    ).getByRole("group", { name: "Улика seal" });
    expect(document.activeElement).toBe(seal);
  });

  test("связь, скрытая фильтром «Связей», снова видна и в фокусе", async () => {
    const target = draftOf({
      npcs: [
        npcOf({ id: "marta", name: "Марта" }),
        npcOf({ id: "fabian", name: "Фабиан" }),
        npcOf({ id: "olaf", name: "Олаф" }),
      ],
      relations: [relationOf({ id: "debt", a: "npc:fabian", b: "npc:marta" })],
    });
    const { form } = await openStep(target, "Связи", {
      [`POST ${VALIDATE}`]: {
        json: reportOf({
          errors: [
            findingOf({
              ref: "relation:debt",
              step: "relations",
              message: "ошибка",
            }),
          ],
        }),
      },
    });
    const filter = () =>
      within(form).getByRole("combobox", { name: "Показать связи" });
    // Фильтр по Олафу прячет связь Фабиана и Марты.
    await within(filter()).findByRole("option", { name: "Олаф (персонаж)" });
    fireEvent.change(filter(), { target: { value: "npc:olaf" } });
    const debt = () => document.querySelector('[data-ref="relation:debt"]');
    expect(debt()?.closest("[hidden]")).not.toBeNull();

    fireEvent.click(nav().getByRole("button", { name: /^Проверка/ }));
    const errors = await screen.findByRole("list", { name: "Ошибки" });
    fireEvent.click(
      within(errors).getByRole("button", { name: "Перейти к шагу «Связи»" }),
    );

    expect(filter()).toHaveValue("");
    expect(debt()?.closest("[hidden]")).toBeNull();
    expect(document.activeElement).toBe(debt());
    expect(scrolled.at(-1)).toBe(debt());
  });

  test("сущности на экране нет — прокрутка к началу экрана шага, фокус на нём", async () => {
    const panel = await follow(
      draftOf({ locations: [HALL] }),
      { ref: "location:gone/exit:down", step: "locations" },
      "Карта",
    );

    expect(document.activeElement).toBe(panel);
    expect(scrolled.at(-1)).toBe(panel);
  });
});
