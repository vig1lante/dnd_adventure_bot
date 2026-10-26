import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  choose,
  draftOf,
  encounterOf,
  exitOf,
  featureOf,
  group,
  itemOf,
  jobOf,
  locationOf,
  npcOf,
  openStep,
  optionValues,
  press,
  secretOf,
  typeIn,
} from "./fixtures";

const LOCATIONS = "/api/drafts/d1/sections/locations";
const ITEMS = "/api/drafts/d1/sections/items";
const LOCKS = "/api/drafts/d1/locks";

const HALL = locationOf({
  id: "hall",
  name: "Зал",
  read_aloud: "Пыльный зал.",
});
const COFFIN = featureOf({ id: "coffin", name: "Гроб" });
const CRYPT = locationOf({ id: "crypt", name: "Склеп", features: [COFFIN] });

/** Черновик с картой из зала и склепа: старт и финал заданы на «Карте». */
const mapped = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 4,
    locations: [HALL, CRYPT],
    start_location: "hall",
    finale_location: "crypt",
    ...overrides,
  });

/** Заглушки записей: каждая отвечает черновиком со следующей версией. */
function writes(draft: Draft): Routes {
  return {
    [`PUT ${ITEMS}`]: { json: { ...draft, version: draft.version + 1 } },
    [`PUT ${LOCATIONS}`]: { json: { ...draft, version: draft.version + 2 } },
  };
}

async function openContents(draft: Draft, routes: Routes = {}) {
  const view = await openStep(draft, "Наполнение", {
    ...writes(draft),
    ...routes,
  });
  return view;
}

/** Открывает в «Наполнении» нужную локацию. */
function pick(form: HTMLElement, id: string) {
  choose(form, "Локация", id);
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");
const puts = (server: { calls: { method: string; path: string }[] }) =>
  server.calls.filter((call) => call.method === "PUT").map((call) => call.path);

describe("шаг «Наполнение»: локация на выбор", () => {
  test("без локаций — подсказка начать с «Карты»", async () => {
    const { form } = await openContents(draftOf());

    expect(form).toHaveTextContent("Сначала добавьте локации на шаге «Карта»");
    expect(
      within(form).queryByRole("combobox", { name: "Локация" }),
    ).toBeNull();
  });

  test("сначала открыта первая локация; выбор показывает поля другой", async () => {
    const { form } = await openContents(mapped());

    expect(
      within(form).getByRole("textbox", { name: "Текст вслух" }),
    ).toHaveValue("Пыльный зал.");
    expect(optionValues(form, "Локация")).toEqual(["hall", "crypt"]);

    pick(form, "crypt");

    expect(
      within(form).getByRole("textbox", { name: "Текст вслух" }),
    ).toHaveValue("Холодный склеп под часовней.");
    expect(
      within(group(form, "Детали и тайники")).getByRole("group", {
        name: "Гроб",
      }),
    ).toBeInTheDocument();
  });

  test("несохранённая правка одной локации переживает выбор другой", async () => {
    const { form } = await openContents(mapped());

    typeIn(form, "Заметки мастера", "Пыль скрывает следы");
    pick(form, "crypt");
    pick(form, "hall");

    expect(
      within(form).getByRole("textbox", { name: "Заметки мастера" }),
    ).toHaveValue("Пыль скрывает следы");
  });
});

describe("шаг «Наполнение»: выбранная локация после записи", () => {
  test("после «Сохранить раздел» открыта та же локация с данными сервера", async () => {
    const draft = mapped();
    const saved = {
      ...draft,
      version: 5,
      locations: [HALL, { ...CRYPT, read_aloud: "Сырой склеп." }],
    };
    const { server, form } = await openContents(draft, {
      [`PUT ${LOCATIONS}`]: { json: saved },
    });
    pick(form, "crypt");

    typeIn(form, "Текст вслух", "Сырой склеп.");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", { name: "Закрепить: Склеп" }),
      ).toBeEnabled(),
    );
    expect(within(form).getByRole("combobox", { name: "Локация" })).toHaveValue(
      "crypt",
    );
    expect(
      within(form).getByRole("textbox", { name: "Текст вслух" }),
    ).toHaveValue("Сырой склеп.");
  });
});

describe("шаг «Наполнение»: старт и финал вне карты", () => {
  // Fix-раунд 1 B5 (§9, minor 1 webapp): «Наполнение» шлёт старт и финал как сохранены, а правятся
  // они только на «Карте» — автору нужно сказать, где исправить, иначе запись не пройдёт.
  test.each([
    ["старт", { start_location: "ghost" }],
    ["финал", { finale_location: "ghost" }],
  ] as const)(
    "%s не на карте — подсказка править его на «Карте»",
    async (_name, ends) => {
      const { form } = await openContents(mapped(ends));

      expect(form).toHaveTextContent(
        "Старт или финал не на карте: исправьте их на шаге «Карта», иначе раздел не сохранится.",
      );
    },
  );

  test("старт и финал на карте — подсказки нет", async () => {
    const { form } = await openContents(mapped());

    expect(form).not.toHaveTextContent("Старт или финал не на карте");
  });
});

describe("шаг «Наполнение»: запись раздела locations", () => {
  test("текст вслух, заметки, зоны и бой уходят разделом locations с версией; карта и старт с финалом как были", async () => {
    const draft = mapped({ encounters: [encounterOf({ id: "crypt-ghouls" })] });
    const { server, form } = await openContents(draft);
    pick(form, "crypt");

    typeIn(form, "Текст вслух", "Сырой склеп, пахнет ладаном.");
    typeIn(form, "Заметки мастера", "Гули ждут за алтарём.");
    const zones = group(form, "Зоны");
    press(zones, "Добавить зону");
    const altar = group(zones, "Зона 2");
    typeIn(altar, "Идентификатор", "altar");
    typeIn(altar, "Название", "у алтаря");
    await vi.waitFor(() =>
      expect(optionValues(form, "Бой")).toEqual(["", "encounter:crypt-ghouls"]),
    );
    choose(form, "Бой", "encounter:crypt-ghouls");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toEqual({
      version: 4,
      data: {
        locations: [
          HALL,
          {
            ...CRYPT,
            read_aloud: "Сырой склеп, пахнет ладаном.",
            dm_notes: "Гули ждут за алтарём.",
            zones: [
              { id: "here", name: "здесь" },
              { id: "altar", name: "у алтаря" },
            ],
            encounter_id: "crypt-ghouls",
          },
        ],
        start_location: "hall",
        finale_location: "crypt",
      },
    });
  });

  test("бой «нет» уходит как null; связи зон сохраняются как были", async () => {
    const fought = locationOf({
      id: "crypt",
      name: "Склеп",
      encounter_id: "crypt-ghouls",
      links: [{ a: "here", b: "altar", distance: "far" }],
    });
    const { server, form } = await openContents(
      mapped({
        locations: [fought],
        encounters: [encounterOf({ id: "crypt-ghouls" })],
      }),
    );

    choose(form, "Бой", "");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: { locations: [{ ...fought, encounter_id: null }] },
    });
  });

  test("деталь: проверка (характеристика, навык, Сл из таблицы), скрытность, замок и что раскрывает", async () => {
    const { server, form } = await openContents(mapped());
    pick(form, "crypt");

    const features = group(form, "Детали и тайники");
    press(features, "Добавить деталь");
    const altar = group(features, "Деталь 2");
    typeIn(altar, "Идентификатор", "altar");
    typeIn(altar, "Название", "Алтарь");
    typeIn(altar, "Описание", "Чёрный камень в копоти");
    fireEvent.click(
      within(altar).getByRole("switch", { name: "Скрытая деталь" }),
    );
    typeIn(altar, "Замок", "flag:altar-lit");
    fireEvent.click(
      within(altar).getByRole("switch", { name: "Нужна проверка" }),
    );
    choose(altar, "Характеристика", "int");
    choose(altar, "Навык", "investigation");
    choose(altar, "Сл", "20");
    const found = group(altar, "Что раскрывает");
    press(found, "Добавить находку");
    const finding = group(found, "Находка 1");
    choose(finding, "Что", "clue");
    typeIn(finding, "Идентификатор", "ash-note");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: {
        locations: [
          HALL,
          {
            features: [
              COFFIN,
              {
                id: "altar",
                name: "Алтарь",
                description: "Чёрный камень в копоти",
                check: { ability: "int", skill: "investigation", dc: 20 },
                reveals: [{ kind: "clue", id: "ash-note", value: null }],
                hidden: true,
                locked_by: "flag:altar-lit",
              },
            ],
          },
        ],
      },
    });
  });

  test("Сл проверки — только из таблицы; навык необязателен; снятая проверка уходит null", async () => {
    const checked = featureOf({
      check: { ability: "wis", skill: "perception", dc: 15 },
    });
    const { server, form } = await openContents(
      mapped({ locations: [locationOf({ features: [checked] })] }),
    );
    const coffin = group(group(form, "Детали и тайники"), "Гроб");

    expect(optionValues(coffin, "Сл")).toEqual([
      "5",
      "10",
      "15",
      "20",
      "25",
      "30",
    ]);
    expect(optionValues(coffin, "Навык")[0]).toBe("");
    fireEvent.click(
      within(coffin).getByRole("switch", { name: "Нужна проверка" }),
    );
    expect(within(coffin).queryByRole("combobox", { name: "Сл" })).toBeNull();
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: { locations: [{ features: [{ id: "coffin", check: null }] }] },
    });
  });

  test("секрет места: текст, проверка и что раскрывает (улика, предмет, флаг, выход)", async () => {
    const { server, form } = await openContents(
      mapped({ locations: [locationOf({ secrets: [secretOf()] })] }),
    );

    const secrets = group(form, "Секреты");
    press(secrets, "Добавить секрет");
    const fresh = group(secrets, "Секрет 2");
    typeIn(fresh, "Идентификатор", "hidden-door");
    typeIn(fresh, "Тайна", "За саркофагом есть лаз");
    fireEvent.click(
      within(fresh).getByRole("switch", { name: "Нужна проверка" }),
    );
    choose(fresh, "Характеристика", "wis");
    choose(fresh, "Навык", "perception");
    choose(fresh, "Сл", "15");
    const found = group(fresh, "Что раскрывает");
    press(found, "Добавить находку");
    const finding = group(found, "Находка 1");
    expect(optionValues(finding, "Что")).toEqual([
      "clue",
      "item",
      "flag",
      "exit",
    ]);
    choose(finding, "Что", "exit");
    typeIn(finding, "Идентификатор", "crawl");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: {
        locations: [
          {
            secrets: [
              secretOf(),
              {
                id: "hidden-door",
                text: "За саркофагом есть лаз",
                check: { ability: "wis", skill: "perception", dc: 15 },
                reveals: [{ kind: "exit", id: "crawl", value: null }],
              },
            ],
          },
        ],
      },
    });
  });
});

describe("шаг «Наполнение»: предметы «лежит здесь» и «в тайнике»", () => {
  test("предмет «лежит здесь» — одна запись items: держатель location:<id>, с версией", async () => {
    const elsewhere = itemOf({
      id: "lamp",
      name: "Лампа",
      holder: "location:hall",
    });
    const { server, form } = await openContents(mapped({ items: [elsewhere] }));
    pick(form, "crypt");

    const items = group(form, "Предметы");
    // Предмет другой локации здесь не показан.
    expect(within(items).queryByRole("group", { name: "Лампа" })).toBeNull();
    press(items, "Добавить предмет");
    // Номер новой строки — среди предметов этой локации, а не всего черновика (§9, nit).
    const fresh = group(items, "Предмет 1");
    typeIn(fresh, "Идентификатор", "candle");
    typeIn(fresh, "Название", "Свеча");
    typeIn(fresh, "Описание", "Огарок чёрного воска");
    typeIn(fresh, "Предмет SRD", "candle");
    typeIn(fresh, "Что знают игроки", "Огарок свечи");
    expect(fresh).toHaveTextContent("Лежит открыто");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([ITEMS]));
    expect(callsTo(server, "PUT", ITEMS)[0].body).toEqual({
      version: 4,
      data: [
        elsewhere,
        {
          public: "Огарок свечи",
          known_at_start: false,
          id: "candle",
          name: "Свеча",
          description: "Огарок чёрного воска",
          base_item: "candle",
          holder: "location:crypt",
        },
      ],
    });
  });

  test("предмет в тайник: сначала items с держателем, затем locations с деталью reveals item — каждая с текущей версией", async () => {
    const draft = mapped();
    const { server, form } = await openContents(draft);
    pick(form, "crypt");

    const items = group(form, "Предметы");
    press(items, "Добавить предмет");
    const key = group(items, "Предмет 1");
    typeIn(key, "Идентификатор", "crypt-key");
    typeIn(key, "Название", "Ключ от склепа");
    typeIn(key, "Описание", "Тяжёлый железный ключ");
    const coffin = group(group(form, "Детали и тайники"), "Гроб");
    fireEvent.click(
      within(group(coffin, "Предметы в тайнике")).getByRole("checkbox", {
        name: "Ключ от склепа",
      }),
    );
    expect(key).toHaveTextContent("В тайнике «Гроб»");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([ITEMS, LOCATIONS]));
    expect(callsTo(server, "PUT", ITEMS)[0].body).toEqual({
      version: 4,
      data: [itemOf({ id: "crypt-key", holder: "location:crypt" })],
    });
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toEqual({
      version: 5,
      data: {
        locations: [
          HALL,
          {
            ...CRYPT,
            features: [
              {
                ...COFFIN,
                reveals: [{ kind: "item", id: "crypt-key", value: null }],
              },
            ],
          },
        ],
        start_location: "hall",
        finale_location: "crypt",
      },
    });
  });

  test("предмет из тайника — лежит открыто: правится только деталь, раздел items не пишется", async () => {
    const hidden = locationOf({
      features: [
        featureOf({
          reveals: [{ kind: "item", id: "crypt-key", value: null }],
        }),
      ],
    });
    const { server, form } = await openContents(
      mapped({ locations: [hidden], items: [itemOf()] }),
    );

    const key = group(group(form, "Предметы"), "Ключ от склепа");
    expect(key).toHaveTextContent("В тайнике «Гроб»");
    fireEvent.click(
      within(group(form, "Предметы в тайнике")).getByRole("checkbox", {
        name: "Ключ от склепа",
      }),
    );
    expect(key).toHaveTextContent("Лежит открыто");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: { locations: [{ features: [{ id: "coffin", reveals: [] }] }] },
    });
  });

  test("предмет, спрятанный в одной детали, в другую не положить; тайник без такого предмета виден", async () => {
    const twoPlaces = locationOf({
      features: [
        featureOf({
          id: "coffin",
          name: "Гроб",
          reveals: [
            { kind: "item", id: "crypt-key", value: null },
            { kind: "item", id: "ring", value: null },
          ],
        }),
        featureOf({ id: "urn", name: "Урна" }),
      ],
    });
    const { form } = await openContents(
      mapped({ locations: [twoPlaces], items: [itemOf()] }),
    );
    const places = group(form, "Детали и тайники");

    const urn = group(group(places, "Урна"), "Предметы в тайнике");
    expect(
      within(urn).getByRole("checkbox", { name: "Ключ от склепа" }),
    ).toBeDisabled();
    // Предмет, которого ещё нет (его создаст шаг «Предметы» блока 6), остаётся в тайнике.
    const coffin = group(group(places, "Гроб"), "Предметы в тайнике");
    expect(
      within(coffin).getByRole("checkbox", {
        name: /ring — предмета здесь нет/,
      }),
    ).toBeChecked();
  });

  test("сбой второй записи: предметы сохранены — понятный текст и «Обновить»", async () => {
    const draft = mapped();
    const { server, form } = await openContents(draft, {
      [`PUT ${LOCATIONS}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: ["locations.1.features.0.id: неверный id"],
        },
      },
    });
    pick(form, "crypt");
    press(group(form, "Предметы"), "Добавить предмет");
    const key = group(group(form, "Предметы"), "Предмет 1");
    typeIn(key, "Идентификатор", "crypt-key");
    typeIn(key, "Название", "Ключ");
    fireEvent.click(
      within(group(form, "Предметы в тайнике")).getByRole("checkbox", {
        name: "Ключ",
      }),
    );
    save(form);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Данные раздела не прошли проверку.");
    expect(alert).toHaveTextContent("locations.1.features.0.id: неверный id");
    expect(
      screen.getByText(
        "Сохранено: предметы. Не сохранено: локации — обновите черновик и повторите правку.",
      ),
    ).toBeInTheDocument();
    expect(puts(server)).toEqual([ITEMS, LOCATIONS]);

    fireEvent.click(screen.getByRole("button", { name: "Обновить" }));

    await vi.waitFor(() =>
      expect(callsTo(server, "GET", "/api/drafts/d1")).toHaveLength(2),
    );
  });
});

describe("шаг «Наполнение»: перегенерация и закрепление локации", () => {
  test("«Перегенерировать» локацию: новое пожелание шага сохраняется, затем задание location_contents с целью", async () => {
    const draft = mapped();
    const { server, form } = await openContents(draft, {
      "PUT /api/drafts/d1/wishes/location_contents": {
        json: {
          ...draft,
          version: 5,
          wishes: { location_contents: "Больше костей" },
        },
      },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "location_contents", target: "location:crypt" }),
      },
    });
    pick(form, "crypt");

    typeIn(form, "Что хочу увидеть", "Больше костей");
    press(form, "Перегенерировать: Склеп");

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(
      callsTo(server, "PUT", "/api/drafts/d1/wishes/location_contents")[0].body,
    ).toEqual({
      version: 4,
      text: "Больше костей",
    });
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "location_contents",
      target: "location:crypt",
    });
  });

  test("закреплённую локацию и шаг, закреплённый целиком, не перегенерировать", async () => {
    const { form } = await openContents(mapped({ locks: ["location:crypt"] }));

    expect(
      within(form).getByRole("button", { name: "Перегенерировать: Зал" }),
    ).toBeEnabled();
    pick(form, "crypt");
    expect(
      within(form).getByRole("button", { name: "Перегенерировать: Склеп" }),
    ).toBeDisabled();
  });

  test("«Закрепить» шлёт location:<id> с версией; при несохранённых правках закрыт", async () => {
    const draft = mapped({ version: 7 });
    const { server, form } = await openContents(draft, {
      [`PUT ${LOCKS}`]: {
        json: { ...draft, version: 8, locks: ["location:hall"] },
      },
    });
    const lock = () =>
      within(form).getByRole("button", { name: "Закрепить: Зал" });

    typeIn(form, "Текст вслух", "Другой текст");
    expect(lock()).toBeDisabled();
    typeIn(form, "Текст вслух", "Пыльный зал.");
    expect(lock()).toBeEnabled();
    fireEvent.click(lock());

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 7,
      locks: ["location:hall"],
    });
  });
});

// --- финальное покрытие блока 5 ---

/** Ключ из «Предметов» склепа, спрятанный в гроб: правка и items, и locations. */
function hideNewKey(form: HTMLElement) {
  pick(form, "crypt");
  press(group(form, "Предметы"), "Добавить предмет");
  const key = group(group(form, "Предметы"), "Предмет 1");
  typeIn(key, "Идентификатор", "crypt-key");
  typeIn(key, "Название", "Ключ");
  fireEvent.click(
    within(group(form, "Предметы в тайнике")).getByRole("checkbox", {
      name: "Ключ",
    }),
  );
}

describe("шаг «Наполнение»: сбои двух записей подряд", () => {
  // Fix-раунд 1 B5 (§9, security I2): держатель предмета уже записан, а тайник — нет; одного
  // диалога конфликта мало — автор должен знать, что предметы сохранены.
  test("409 на второй записи — диалог «Черновик изменился, обновить?» и текст «Сохранено: предметы…»", async () => {
    const { server, form } = await openContents(mapped(), {
      [`PUT ${LOCATIONS}`]: {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });
    hideNewKey(form);
    save(form);

    expect(
      await screen.findByRole("alertdialog", {
        name: "Черновик изменился, обновить?",
      }),
    ).toBeInTheDocument();
    expect(puts(server)).toEqual([ITEMS, LOCATIONS]);
    expect(
      screen.getByText(
        "Сохранено: предметы. Не сохранено: локации — обновите черновик и повторите правку.",
      ),
    ).toBeInTheDocument();
  });

  test("сбой первой записи: locations не пишется, текста о частичном сохранении нет, правка в форме", async () => {
    const { server, form } = await openContents(mapped(), {
      [`PUT ${ITEMS}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: ["items.0.id: неверный id"],
        },
      },
    });
    hideNewKey(form);
    save(form);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "items.0.id: неверный id",
    );
    expect(puts(server)).toEqual([ITEMS]);
    expect(screen.queryByText(/Сохранено:/)).toBeNull();
    expect(group(group(form, "Предметы"), "Ключ")).toHaveTextContent(
      "В тайнике «Гроб»",
    );
  });
});

describe("шаг «Наполнение»: клиент не теряет то, чего не правит", () => {
  test("отметка тайника без такого предмета снимается: деталь уходит без неё, прочие находки — на местах и с value", async () => {
    const coffin = featureOf({
      reveals: [
        { kind: "clue", id: "scratches", value: null },
        { kind: "item", id: "ring", value: null },
        { kind: "flag", id: "coffin-open", value: true },
        { kind: "item", id: "crypt-key", value: null },
      ],
    });
    const { server, form } = await openContents(
      mapped({
        locations: [locationOf({ features: [coffin] })],
        items: [itemOf()],
      }),
    );
    const cache = group(
      group(group(form, "Детали и тайники"), "Гроб"),
      "Предметы в тайнике",
    );

    fireEvent.click(
      within(cache).getByRole("checkbox", {
        name: /ring — предмета здесь нет/,
      }),
    );
    // Снятая висящая отметка больше не предлагается: предмета ring в локации нет.
    expect(within(cache).queryByRole("checkbox", { name: /ring/ })).toBeNull();
    expect(
      within(cache).getByRole("checkbox", { name: "Ключ от склепа" }),
    ).toBeChecked();
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: {
        locations: [
          {
            features: [
              {
                ...coffin,
                reveals: [
                  { kind: "clue", id: "scratches", value: null },
                  { kind: "flag", id: "coffin-open", value: true },
                  { kind: "item", id: "crypt-key", value: null },
                ],
              },
            ],
          },
        ],
      },
    });
  });

  test("правка находки детали не трогает предметы тайника, порядок и value", async () => {
    const coffin = featureOf({
      reveals: [
        { kind: "item", id: "crypt-key", value: null },
        { kind: "flag", id: "lid-moved", value: "half" },
      ],
    });
    const { server, form } = await openContents(
      mapped({
        locations: [locationOf({ features: [coffin] })],
        items: [itemOf()],
      }),
    );
    const found = group(
      group(group(form, "Детали и тайники"), "Гроб"),
      "Что раскрывает",
    );

    // Предмет тайника отмечается отдельно, поэтому в «Что раскрывает» его нет.
    expect(
      within(found).queryByRole("group", { name: /crypt-key/ }),
    ).toBeNull();
    typeIn(group(found, "флаг: lid-moved"), "Идентификатор", "lid-open");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: {
        locations: [
          {
            features: [
              {
                ...coffin,
                reveals: [
                  { kind: "item", id: "crypt-key", value: null },
                  { kind: "flag", id: "lid-open", value: "half" },
                ],
              },
            ],
          },
        ],
      },
    });
  });

  test("всё, что сервер отдал, уходит назад как было: зоны, связи зон, висящий бой, скрытая деталь, value, предметы других держателей", async () => {
    const vault = locationOf({
      public: "Подвал часовни",
      known_at_start: true,
      id: "vault",
      name: "Подвал",
      region: "chapel",
      read_aloud: "Сырой подвал.",
      dm_notes: "Под плитой лаз.",
      exits: [exitOf({ id: "up", to: "hall", hidden_dc: 20 })],
      features: [
        featureOf({
          id: "slab",
          name: "Плита",
          check: { ability: "str", skill: "athletics", dc: 15 },
          reveals: [{ kind: "exit", id: "crawl", value: 1 }],
          hidden: true,
          locked_by: "flag:slab-loose",
        }),
      ],
      secrets: [
        secretOf({
          check: { ability: "int", skill: null, dc: 25 },
          reveals: [{ kind: "flag", id: "true-tomb", value: false }],
        }),
      ],
      // Бой, которого в черновике нет (удалён или будет в блоке 6), не подменяется молча.
      encounter_id: "old-fight",
      zones: [
        { id: "here", name: "здесь" },
        { id: "pit", name: "у ямы" },
      ],
      links: [{ a: "here", b: "pit", distance: "far" }],
    });
    const others = [
      itemOf({ id: "lamp", name: "Лампа", holder: "location:hall" }),
      itemOf({ id: "ring", name: "Кольцо", holder: "npc:marta" }),
      itemOf({ id: "coin", name: "Монета", holder: "gone" }),
      itemOf({
        id: "torch",
        name: "Факел",
        base_item: "torch",
        holder: "location:vault",
        public: "Обычный факел",
        known_at_start: true,
      }),
    ];
    const draft = mapped({
      locations: [HALL, vault],
      items: others,
      finale_location: "vault",
    });
    const { server, form } = await openContents(draft);
    pick(form, "vault");

    const fight = within(form).getByRole("combobox", { name: "Бой" });
    expect(fight).toHaveValue("encounter:old-fight");
    expect(
      within(fight).getByRole("option", {
        name: "encounter:old-fight — нет в списке",
      }),
    ).toBeInTheDocument();
    typeIn(form, "Текст вслух", "Сырой подвал, капает вода.");
    const items = group(form, "Предметы");
    press(items, "Добавить предмет");
    // Подпись новой строки — номер среди предметов подвала (там уже лежит факел), не всего черновика.
    const key = group(items, "Предмет 2");
    typeIn(key, "Идентификатор", "vault-key");
    typeIn(key, "Название", "Ключ от подвала");
    const slab = group(group(form, "Детали и тайники"), "Плита");
    fireEvent.click(
      within(group(slab, "Предметы в тайнике")).getByRole("checkbox", {
        name: "Ключ от подвала",
      }),
    );
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([ITEMS, LOCATIONS]));
    expect(callsTo(server, "PUT", ITEMS)[0].body).toEqual({
      version: 4,
      data: [
        ...others,
        itemOf({
          id: "vault-key",
          name: "Ключ от подвала",
          description: "",
          holder: "location:vault",
        }),
      ],
    });
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toEqual({
      version: 5,
      data: {
        locations: [
          HALL,
          {
            ...vault,
            read_aloud: "Сырой подвал, капает вода.",
            features: [
              {
                ...vault.features[0],
                reveals: [
                  { kind: "exit", id: "crawl", value: 1 },
                  { kind: "item", id: "vault-key", value: null },
                ],
              },
            ],
          },
        ],
        start_location: "hall",
        finale_location: "vault",
      },
    });
  });
});

describe("проверка детали и секрета: только выбор из таблицы, без расчётов", () => {
  test.each([
    {
      where: "деталь",
      location: locationOf({
        features: [
          featureOf({ check: { ability: "wis", skill: "perception", dc: 15 } }),
        ],
      }),
      open: (form: HTMLElement) =>
        group(group(form, "Детали и тайники"), "Гроб"),
      saved: { features: [{ check: { ability: "dex", skill: null, dc: 25 } }] },
    },
    {
      where: "секрет",
      location: locationOf({
        secrets: [
          secretOf({ check: { ability: "int", skill: "history", dc: 10 } }),
        ],
      }),
      open: (form: HTMLElement) => group(group(form, "Секреты"), "Секрет 1"),
      saved: { secrets: [{ check: { ability: "dex", skill: null, dc: 25 } }] },
    },
  ])(
    "$where: «без навыка» уходит null, Сл — числом из таблицы",
    async ({ location, open, saved }) => {
      const { server, form } = await openContents(
        mapped({ locations: [location] }),
      );
      const finding = open(form);

      expect(optionValues(finding, "Сл")).toEqual([
        "5",
        "10",
        "15",
        "20",
        "25",
        "30",
      ]);
      choose(finding, "Характеристика", "dex");
      choose(finding, "Навык", "");
      choose(finding, "Сл", "25");
      save(form);

      await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
      expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
        data: { locations: [saved] },
      });
    },
  );
});

describe("«Карта» и «Наполнение» правят один раздел locations", () => {
  const SHARED_HINT = "«Карта» и «Наполнение» правят один раздел";
  const goTo = (step: string) =>
    fireEvent.click(
      within(
        screen.getByRole("navigation", { name: "Шаги приключения" }),
      ).getByRole("button", { name: new RegExp(step) }),
    );
  // Скрытые шаги в дереве доступности не видны: форма шага ищется после перехода на него.
  const stepForm = (step: string) => screen.getByRole("region", { name: step });

  // Fix-раунд 1 B5 (§9, minor 3): потеря несохранённого больше не тихая. Редактор знает о правках
  // обоих экранов раздела, и другой экран предупреждает у «Сохранить раздел», «Сгенерировать шаг»
  // и «Перегенерировать»; кнопки доступны. Само поведение после записи прежнее: экран с правками
  // начинается заново с черновиком сервера (решение dispatcher — предупреждать, а не запрещать).
  const fromContents =
    "На экране «Наполнение» есть несохранённые правки: они пропадут.";
  const fromMap = "На экране «Карта» есть несохранённые правки: они пропадут.";

  test("правка «Наполнения» — «Карта» предупреждает у записи и генерации; после записи «Карты» правка «Наполнения» пропадает", async () => {
    const draft = mapped();
    const renamed = { ...HALL, name: "Большой зал" };
    const { server, form } = await openContents(draft, {
      [`PUT ${LOCATIONS}`]: {
        json: { ...draft, version: 5, locations: [renamed, CRYPT] },
      },
    });
    expect(form).toHaveTextContent(SHARED_HINT);
    typeIn(form, "Заметки мастера", "Несохранённая заметка");

    goTo("Карта");
    const map = stepForm("Карта");
    expect(map).toHaveTextContent(SHARED_HINT);
    // У «Сохранить раздел» и у «Сгенерировать шаг».
    expect(within(map).getAllByText(fromContents)).toHaveLength(2);
    expect(
      within(map).getByRole("button", { name: "Сгенерировать шаг" }),
    ).toBeEnabled();
    typeIn(group(map, "Зал"), "Название", "Большой зал");
    press(map, "Сохранить раздел");

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toEqual({
      version: 4,
      data: {
        locations: [renamed, CRYPT],
        start_location: "hall",
        finale_location: "crypt",
      },
    });
    goTo("Наполнение");
    const contents = stepForm("Наполнение");
    await within(contents).findByRole("option", { name: "Большой зал" });
    expect(
      within(contents).getByRole("textbox", { name: "Заметки мастера" }),
    ).toHaveValue("");
    // «Наполнение» начато заново — предупреждать «Карте» больше не о чем.
    goTo("Карта");
    expect(within(stepForm("Карта")).queryByText(fromContents)).toBeNull();
  });

  test("правка «Карты» — «Наполнение» предупреждает у записи, генерации и «Перегенерировать»; после записи «Наполнения» правка «Карты» пропадает", async () => {
    const draft = mapped();
    const dusty = { ...HALL, read_aloud: "Зал в пыли." };
    const { server, form: map } = await openStep(draft, "Карта", {
      ...writes(draft),
      [`PUT ${LOCATIONS}`]: {
        json: { ...draft, version: 5, locations: [dusty, CRYPT] },
      },
    });
    typeIn(group(map, "Зал"), "Регион", "cape");

    goTo("Наполнение");
    const contents = stepForm("Наполнение");
    expect(within(contents).getAllByText(fromMap)).toHaveLength(3);
    // Своих правок у «Наполнения» нет: перегенерация открыта, предупреждение — о «Карте».
    expect(
      within(contents).getByRole("button", { name: "Перегенерировать: Зал" }),
    ).toBeEnabled();
    typeIn(contents, "Текст вслух", "Зал в пыли.");
    press(contents, "Сохранить раздел");

    await vi.waitFor(() => expect(puts(server)).toEqual([LOCATIONS]));
    expect(callsTo(server, "PUT", LOCATIONS)[0].body).toMatchObject({
      data: { locations: [dusty, CRYPT] },
    });
    await vi.waitFor(() =>
      expect(
        within(contents).getByRole("button", { name: "Закрепить: Зал" }),
      ).toBeEnabled(),
    );
    goTo("Карта");
    expect(
      within(group(stepForm("Карта"), "Зал")).getByRole("textbox", {
        name: "Регион",
      }),
    ).toHaveValue("");
  });

  test("без правок на другом экране предупреждения нет; правка, возвращённая к сохранённому, его снимает", async () => {
    const { form } = await openContents(mapped());
    expect(form).not.toHaveTextContent(fromMap);

    goTo("Карта");
    const map = stepForm("Карта");
    expect(map).not.toHaveTextContent(fromContents);
    typeIn(group(map, "Зал"), "Регион", "cape");
    goTo("Наполнение");
    expect(stepForm("Наполнение")).toHaveTextContent(fromMap);

    goTo("Карта");
    typeIn(group(stepForm("Карта"), "Зал"), "Регион", "");
    goTo("Наполнение");
    expect(stepForm("Наполнение")).not.toHaveTextContent(fromMap);
  });

  test("запись другого раздела (персонажи) не трогает несохранённое «Наполнение»", async () => {
    const draft = mapped({ npcs: [npcOf()] });
    const { server, form } = await openContents(draft, {
      "PUT /api/drafts/d1/sections/npcs": {
        json: {
          ...draft,
          version: 5,
          npcs: [npcOf({ motive: "уехать с мыса" })],
        },
      },
    });
    typeIn(form, "Заметки мастера", "Несохранённая заметка");

    goTo("Персонажи");
    const people = stepForm("Персонажи");
    typeIn(group(people, "Марта"), "Мотив", "уехать с мыса");
    press(people, "Сохранить раздел");
    await vi.waitFor(() =>
      expect(
        callsTo(server, "PUT", "/api/drafts/d1/sections/npcs"),
      ).toHaveLength(1),
    );
    await vi.waitFor(() =>
      expect(
        within(people).getByRole("button", { name: "Закрепить: Марта" }),
      ).toBeEnabled(),
    );

    goTo("Наполнение");
    expect(
      within(stepForm("Наполнение")).getByRole("textbox", {
        name: "Заметки мастера",
      }),
    ).toHaveValue("Несохранённая заметка");
  });
});
