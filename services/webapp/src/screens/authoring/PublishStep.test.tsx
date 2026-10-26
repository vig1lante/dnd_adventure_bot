import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft, DraftReport } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  deityOf,
  draftOf,
  factionOf,
  featureOf,
  findingOf,
  itemOf,
  locationOf,
  npcOf,
  openStep,
  regionOf,
  reportOf,
  rumorOf,
  secretOf,
} from "./fixtures";

const VALIDATE = "/api/drafts/d1/validate";
const PUBLISH = "/api/drafts/d1/publish";
const EXPORT = "/api/drafts/d1/export";

const CRYPT = locationOf({
  id: "crypt",
  name: "Склеп",
  public: "Старый склеп под часовней",
  features: [
    featureOf({
      id: "coffin",
      name: "Гроб",
      reveals: [{ kind: "item", id: "ring", value: null }],
    }),
  ],
});
const MARTA = npcOf({ id: "marta", name: "Марта", public: "Смотритель маяка" });
const KEY = itemOf({ id: "crypt-key", name: "Ключ от склепа" });
const RING = itemOf({ id: "ring", name: "Кольцо" });
const LETTER = itemOf({ id: "letter", name: "Письмо", holder: "npc:marta" });

const ready = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 9,
    title: "Туманная гавань",
    adventure_id: "foggy-harbor",
    journal_intro: "Найти корабль до шторма.",
    settings: { ...draftOf().settings, pitch: "Корабль исчез в тумане" },
    locations: [CRYPT],
    npcs: [MARTA],
    items: [RING, LETTER],
    ...overrides,
  });

const PUBLISHED = {
  adventure_id: "foggy-harbor",
  published_at: "2026-10-26T18:00:00+00:00",
};

function openPublish(
  draft: Draft = ready(),
  report: DraftReport = reportOf(),
  routes: Routes = {},
) {
  return openStep(draft, "Публикация", {
    [`POST ${VALIDATE}`]: { json: report },
    [`POST ${PUBLISH}`]: { status: 201, json: PUBLISHED },
    ...routes,
  });
}

const publishButton = (form: HTMLElement) =>
  within(form).getByRole("button", { name: "Опубликовать" });
/** Ждёт отчёта проверки: пока его нет, публикация закрыта. */
const checked = (form: HTMLElement) =>
  vi.waitFor(() => expect(publishButton(form)).toBeEnabled());

describe("экран «Публикация»", () => {
  test("при ошибках проверки «Опубликовать» закрыта, запрос не уходит; есть переход к проверке", async () => {
    const { server, form } = await openPublish(
      ready(),
      reportOf({ errors: [findingOf()], readiness_percent: 99 }),
    );

    expect(
      await within(form).findByText("Сначала исправьте ошибки проверки: 1."),
    ).toBeInTheDocument();
    expect(publishButton(form)).toBeDisabled();
    fireEvent.click(publishButton(form));
    expect(callsTo(server, "POST", PUBLISH)).toHaveLength(0);

    fireEvent.click(
      within(form).getByRole("button", { name: "Перейти к шагу «Проверка»" }),
    );
    expect(
      screen.getByRole("region", { name: "Проверка" }),
    ).toBeInTheDocument();
  });

  test("предупреждения проверки публикацию не закрывают", async () => {
    const { form } = await openPublish(
      ready(),
      reportOf({ warnings: [findingOf({ step: null })] }),
    );

    await checked(form);
  });

  test("«Это увидят игроки»: название, завязка, главное для дневника и «Что знают игроки» сущностей; предупреждение о тайнах", async () => {
    const { form } = await openPublish();

    const visible = within(form).getByRole("list", {
      name: "Это увидят игроки",
    });
    expect(visible).toHaveTextContent("Название: Туманная гавань");
    expect(visible).toHaveTextContent("Завязка: Корабль исчез в тумане");
    expect(visible).toHaveTextContent(
      "Главное для дневника: Найти корабль до шторма.",
    );
    expect(visible).toHaveTextContent(
      "Локация «Склеп»: Старый склеп под часовней",
    );
    expect(visible).toHaveTextContent("Персонаж «Марта»: Смотритель маяка");
    expect(form).toHaveTextContent(
      "Не пишите в эти поля тайн: после публикации их увидят все игроки.",
    );
  });

  test("публикация без открытых предметов: POST /publish с {}; 201 — id и подсказка «создать игру на нём»", async () => {
    const { server, form } = await openPublish();
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(
      await within(form).findByText(
        "Приключение опубликовано под id «foggy-harbor».",
      ),
    ).toBeInTheDocument();
    expect(form).toHaveTextContent(
      "Создайте игру на нём: вкладка «Мои игры» → «Создать игру».",
    );
    expect(callsTo(server, "POST", PUBLISH)[0].body).toEqual({});
  });

  test("повторная публикация (200) — тот же понятный текст: начатые игры не меняются", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: { status: 200, json: PUBLISHED },
    });
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(
      await within(form).findByText(
        "Приключение опубликовано под id «foggy-harbor».",
      ),
    ).toBeInTheDocument();
    expect(form).toHaveTextContent("Начатые игры публикация не меняет.");
  });

  test("предметы «лежат открыто» — подтверждение: «Отмена» не публикует, «Опубликовать всё равно» публикует", async () => {
    const { server, form } = await openPublish(
      ready({ items: [KEY, RING, LETTER] }),
    );
    await checked(form);

    fireEvent.click(publishButton(form));
    const confirm = within(form).getByRole("alertdialog", {
      name: "Предметы лежат открыто",
    });
    // Ключ — у локации без тайника; кольцо — в тайнике, письмо — у персонажа.
    expect(confirm).toHaveTextContent("Ключ от склепа (Склеп)");
    expect(confirm).not.toHaveTextContent("Кольцо");
    expect(confirm).not.toHaveTextContent("Письмо");
    fireEvent.click(within(confirm).getByRole("button", { name: "Отмена" }));
    expect(within(form).queryByRole("alertdialog")).toBeNull();
    expect(callsTo(server, "POST", PUBLISH)).toHaveLength(0);

    fireEvent.click(publishButton(form));
    fireEvent.click(
      within(form).getByRole("button", { name: "Опубликовать всё равно" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", PUBLISH)).toHaveLength(1),
    );
  });

  test("422 blocking_errors: текст сервера и список проблем", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: {
        status: 422,
        json: {
          detail: "Есть ошибки, которые мешают игре.",
          code: "blocking_errors",
          problems: ["encounters: в приключении нет ни одного боя"],
        },
      },
    });
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Есть ошибки, которые мешают игре.",
    );
    expect(form).toHaveTextContent(
      "encounters: в приключении нет ни одного боя",
    );
  });

  test("409 id_taken: текст сервера и переход к «Замыслу», где меняют id", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: {
        status: 409,
        json: {
          detail: "Этот id уже занят: выберите другой",
          code: "id_taken",
        },
      },
    });
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Этот id уже занят: выберите другой",
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Перейти к шагу «Замысел»" }),
    );
    expect(screen.getByRole("region", { name: "Замысел" })).toBeInTheDocument();
  });

  test("409 job_running: текст сервера, без перехода к «Замыслу»", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: {
        status: 409,
        json: { detail: "Идёт генерация: подождите.", code: "job_running" },
      },
    });
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Идёт генерация: подождите.",
    );
    expect(
      within(form).queryByRole("button", { name: "Перейти к шагу «Замысел»" }),
    ).toBeNull();
  });

  test("id приключения не задан — публикация и архив закрыты, подсказка и переход к «Замыслу»", async () => {
    const { form } = await openPublish(ready({ adventure_id: null }));

    expect(
      await within(form).findByText(
        "Сначала задайте id приключения на шаге «Замысел».",
      ),
    ).toBeInTheDocument();
    await vi.waitFor(() =>
      expect(within(form).queryByText(/Проверяем/)).toBeNull(),
    );
    expect(publishButton(form)).toBeDisabled();
    expect(
      within(form).getByRole("button", { name: "Скачать архив" }),
    ).toBeDisabled();
    expect(
      within(form).getByRole("button", { name: "Перейти к шагу «Замысел»" }),
    ).toBeInTheDocument();
  });
});

describe("экспорт архива", () => {
  let created: Blob[];
  let clicked: string[];

  beforeEach(() => {
    created = [];
    clicked = [];
    // В jsdom нет createObjectURL: подменяется на время теста, ссылка запоминает имя файла.
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: (blob: Blob) => {
        created.push(blob);
        return "blob:archive";
      },
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: () => undefined,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this.download);
    });
  });

  afterEach(() => {
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  test("«Скачать архив» — GET /export, файл <adventure_id>.zip", async () => {
    const { server, form } = await openPublish(ready(), reportOf(), {
      [`GET ${EXPORT}`]: { text: "PK-архив", contentType: "application/zip" },
    });

    fireEvent.click(
      within(form).getByRole("button", { name: "Скачать архив" }),
    );

    await vi.waitFor(() => expect(clicked).toEqual(["foggy-harbor.zip"]));
    await expect(created[0].text()).resolves.toBe("PK-архив");
    expect(callsTo(server, "GET", EXPORT)).toHaveLength(1);
  });

  test("отказ экспорта 422 export_invalid — текст и проблемы, файла нет", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`GET ${EXPORT}`]: {
        status: 422,
        json: {
          detail: "Черновик не собирается в приключение.",
          code: "export_invalid",
          problems: ["locations: нет ни одной локации"],
        },
      },
    });

    fireEvent.click(
      within(form).getByRole("button", { name: "Скачать архив" }),
    );

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Черновик не собирается в приключение.",
    );
    expect(form).toHaveTextContent("locations: нет ни одной локации");
    expect(clicked).toEqual([]);
  });
});

// --- финальное покрытие блока 6 ---

describe("«Публикация»: отчёт проверки и отказы сервера", () => {
  test("проверка не пришла — «Опубликовать» закрыта; «Повторить» с чистым отчётом её открывает", async () => {
    let attempt = 0;
    const { server, form } = await openPublish(ready(), reportOf(), {
      [`POST ${VALIDATE}`]: () =>
        attempt++ === 0
          ? { status: 500, json: { detail: "Проверка недоступна." } }
          : { json: reportOf() },
    });

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Проверка недоступна.",
    );
    expect(publishButton(form)).toBeDisabled();
    fireEvent.click(publishButton(form));
    expect(callsTo(server, "POST", PUBLISH)).toHaveLength(0);

    fireEvent.click(within(form).getByRole("button", { name: "Повторить" }));

    await checked(form);
    expect(callsTo(server, "POST", VALIDATE)).toHaveLength(2);
  });

  // Код отказа → нужен ли переход к «Замыслу» (там задают и меняют id, B6 §6).
  test.each([
    {
      status: 422,
      code: "no_adventure_id",
      detail: "Сначала задайте id приключения.",
      toConcept: true,
    },
    {
      status: 422,
      code: "blocking_errors",
      detail: "В приключении есть ошибки: исправьте их, потом публикуйте.",
      toConcept: false,
    },
  ])(
    "$status $code: текст сервера; переход к «Замыслу» — $toConcept",
    async ({ status, code, detail, toConcept }) => {
      const { form } = await openPublish(ready(), reportOf(), {
        [`POST ${PUBLISH}`]: { status, json: { detail, code } },
      });
      await checked(form);

      fireEvent.click(publishButton(form));

      expect(await within(form).findByRole("alert")).toHaveTextContent(detail);
      expect(
        within(form).queryByRole("button", {
          name: "Перейти к шагу «Замысел»",
        }) !== null,
      ).toBe(toConcept);
      expect(form).not.toHaveTextContent("Приключение опубликовано");
    },
  );

  test("обрыв связи при публикации — текст про связь, успеха нет", async () => {
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    await checked(form);

    fireEvent.click(publishButton(form));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    );
    expect(form).not.toHaveTextContent("Приключение опубликовано");
  });

  test("отказ после удачной публикации: прежний текст успеха убран, виден отказ", async () => {
    let attempt = 0;
    const { form } = await openPublish(ready(), reportOf(), {
      [`POST ${PUBLISH}`]: () =>
        attempt++ === 0
          ? { status: 201, json: PUBLISHED }
          : {
              status: 409,
              json: {
                detail: "Идёт генерация шага: дождитесь её окончания.",
                code: "job_running",
              },
            },
    });
    await checked(form);
    fireEvent.click(publishButton(form));
    expect(
      await within(form).findByText(
        "Приключение опубликовано под id «foggy-harbor».",
      ),
    ).toBeInTheDocument();

    fireEvent.click(publishButton(form));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Идёт генерация шага: дождитесь её окончания.",
    );
    expect(form).not.toHaveTextContent("Приключение опубликовано");
  });
});

describe("«Это увидят игроки»: только «Что знают игроки», без тайн мастера", () => {
  test("регион, слух, бог, фракция и предмет с «Что знают игроки» — в списке; пустое поле — нет", async () => {
    const world = {
      history: "Тайная история гавани",
      regions: [
        regionOf({ name: "Гавань", public: "Порт в тумане" }),
        regionOf({ id: "hills", name: "Холмы", public: "" }),
      ],
      rumors: [
        rumorOf({ text: "В маяке живёт призрак", public: "Слухи о призраке" }),
      ],
    };
    const { form } = await openPublish(
      ready({
        world,
        deities: [deityOf({ public: "Богиня драконов" })],
        factions: [factionOf({ public: "Торговцы ночью" })],
        items: [
          LETTER,
          itemOf({
            id: "ring",
            name: "Кольцо",
            holder: "npc:marta",
            public: "Серебряное кольцо",
          }),
        ],
      }),
    );

    const rows = within(
      within(form).getByRole("list", { name: "Это увидят игроки" }),
    )
      .getAllByRole("listitem")
      .map((row) => row.textContent);
    expect(rows).toEqual([
      "Название: Туманная гавань",
      "Завязка: Корабль исчез в тумане",
      "Главное для дневника: Найти корабль до шторма.",
      "Регион «Гавань»: Порт в тумане",
      "Слух «В маяке живёт призрак»: Слухи о призраке",
      "Бог «Тиамат»: Богиня драконов",
      "Фракция «Гильдия теней»: Торговцы ночью",
      "Локация «Склеп»: Старый склеп под часовней",
      "Персонаж «Марта»: Смотритель маяка",
      "Предмет «Кольцо»: Серебряное кольцо",
    ]);
  });

  test("тайны мастера (секреты, заметки, правдивость слуха) в список не попадают", async () => {
    const { form } = await openPublish(
      ready({
        world: {
          history: "Гавань построили контрабандисты",
          regions: [],
          rumors: [rumorOf({ true: true, public: "Слухи о призраке" })],
        },
        deities: [
          deityOf({ secret: "Тиамат спит под маяком", public: "Богиня" }),
        ],
        factions: [
          factionOf({ secret: "Глава гильдии — Марта", public: "Гильдия" }),
        ],
        locations: [
          locationOf({
            ...CRYPT,
            dm_notes: "Под гробом ход в море",
            secrets: [secretOf({ text: "В склепе похоронен не тот" })],
          }),
        ],
        npcs: [npcOf({ ...MARTA, secret: "Марта и есть контрабандист" })],
        journal_intro: "",
      }),
    );

    const visible = within(form).getByRole("list", {
      name: "Это увидят игроки",
    });
    for (const secret of [
      "контрабандисты",
      "Тиамат спит под маяком",
      "Глава гильдии",
      "Под гробом ход в море",
      "похоронен не тот",
      "Марта и есть контрабандист",
    ]) {
      expect(visible).not.toHaveTextContent(secret);
    }
    // Пустое «Главное для дневника» — игроки увидят завязку.
    expect(visible).toHaveTextContent(
      "Главное для дневника: возьмётся завязка",
    );
  });
});

describe("«Лежат открыто»: держатель — локация, и ни одна деталь не прячет предмет", () => {
  test("тайник в другой локации прячет предмет; держатель без локации на карте показан своим id", async () => {
    const HALL = locationOf({
      id: "hall",
      name: "Зал",
      features: [
        featureOf({
          id: "stash",
          name: "Тайник",
          reveals: [{ kind: "item", id: "crypt-key", value: null }],
        }),
      ],
    });
    const LAMP = itemOf({
      id: "lamp",
      name: "Лампа",
      holder: "location:tower",
    });
    const COIN = itemOf({ id: "coin", name: "", holder: "location:crypt" });
    const { server, form } = await openPublish(
      ready({ locations: [CRYPT, HALL], items: [KEY, LAMP, COIN, LETTER] }),
    );
    await checked(form);

    fireEvent.click(publishButton(form));

    const confirm = within(form).getByRole("alertdialog", {
      name: "Предметы лежат открыто",
    });
    expect(
      within(confirm)
        .getAllByRole("listitem")
        .map((row) => row.textContent),
    ).toEqual(["Лампа (tower)", "coin (Склеп)"]);
    expect(callsTo(server, "POST", PUBLISH)).toHaveLength(0);
  });
});

// --- fix-раунд 1 блока 6 (дополнение B6, §11, R8) ---

describe("«Публикация»: проверка видит только заполненные шаги", () => {
  const PARTIAL =
    "Проверка показывает находки только заполненных шагов; при публикации сервер проверит всё.";

  test("готовность меньше 100% — подсказка, что публикация проверит всё; кнопка открыта", async () => {
    const { form } = await openPublish(
      ready(),
      reportOf({ readiness_percent: 71 }),
    );
    await checked(form);

    expect(form).toHaveTextContent(PARTIAL);
  });

  test("готовность 100% — подсказки нет", async () => {
    const { form } = await openPublish(ready(), reportOf());
    await checked(form);

    expect(form).not.toHaveTextContent(PARTIAL);
  });
});
