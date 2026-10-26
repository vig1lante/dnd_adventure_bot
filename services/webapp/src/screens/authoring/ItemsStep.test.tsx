import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  choose,
  draftOf,
  featureOf,
  group,
  itemOf,
  jobOf,
  locationOf,
  npcOf,
  openStep,
  optionValues,
  press,
  typeIn,
} from "./fixtures";

const ITEMS = "/api/drafts/d1/sections/items";
const LOCKS = "/api/drafts/d1/locks";

const CRYPT = locationOf({
  id: "crypt",
  name: "Склеп",
  features: [
    featureOf({
      id: "coffin",
      name: "Гроб",
      reveals: [{ kind: "item", id: "ring", value: null }],
    }),
  ],
});
const MARTA = npcOf({ id: "marta", name: "Марта" });
const KEY = itemOf({ id: "crypt-key", name: "Ключ от склепа" });
const RING = itemOf({
  id: "ring",
  name: "Кольцо",
  description: "Серебряное кольцо",
  base_item: "ring",
});
const LETTER = itemOf({
  id: "letter",
  name: "Письмо",
  holder: "npc:marta",
  public: "Письмо Марты",
  known_at_start: true,
});

const world = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 3,
    locations: [CRYPT],
    npcs: [MARTA],
    items: [KEY, RING, LETTER],
    ...overrides,
  });

function openItems(draft: Draft, routes: Routes = {}) {
  return openStep(draft, "Предметы и награды", {
    [`PUT ${ITEMS}`]: { json: { ...draft, version: draft.version + 1 } },
    ...routes,
  });
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");
const goTo = (step: string) =>
  fireEvent.click(
    within(
      screen.getByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: new RegExp(step) }),
  );

describe("шаг «Предметы и награды»", () => {
  test("предметы черновика: держатель, база SRD, «Что знают игроки», «Известно с начала», где лежат", async () => {
    const { form } = await openItems(world());

    const letter = group(form, "Письмо");
    await vi.waitFor(() =>
      expect(
        within(letter).getByRole("combobox", { name: "Где лежит" }),
      ).toHaveValue("npc:marta"),
    );
    expect(
      within(letter).getByRole("textbox", { name: "Что знают игроки" }),
    ).toHaveValue("Письмо Марты");
    expect(
      within(letter).getByRole("switch", { name: "Известно с начала" }),
    ).toBeChecked();
    expect(
      within(group(form, "Кольцо")).getByRole("textbox", {
        name: "Предмет SRD",
      }),
    ).toHaveValue("ring");
    expect(group(form, "Ключ от склепа")).toHaveTextContent("Лежит открыто");
    expect(group(form, "Кольцо")).toHaveTextContent("В тайнике «Гроб»");
    expect(letter).not.toHaveTextContent("Лежит открыто");
  });

  test("«Где лежит» — только локации и персонажи из справочника черновика", async () => {
    const { form } = await openItems(world());

    await vi.waitFor(() =>
      expect(optionValues(group(form, "Ключ от склепа"), "Где лежит")).toEqual([
        "",
        "npc:marta",
        "location:crypt",
      ]),
    );
  });

  test("правка, добавление и удаление уходят списком items с версией", async () => {
    const { server, form } = await openItems(world());

    press(form, "Удалить: Кольцо");
    const letter = group(form, "Письмо");
    await vi.waitFor(() =>
      expect(optionValues(letter, "Где лежит")).toContain("location:crypt"),
    );
    choose(letter, "Где лежит", "location:crypt");
    press(form, "Добавить предмет");
    const fresh = group(form, "Предмет 3");
    typeIn(fresh, "Идентификатор", "gold");
    typeIn(fresh, "Название", "Кошель золота");
    typeIn(fresh, "Описание", "Сто золотых");
    choose(fresh, "Где лежит", "npc:marta");
    typeIn(fresh, "Предмет SRD", "");
    typeIn(fresh, "Что знают игроки", "Награда за маяк");
    fireEvent.click(
      within(fresh).getByRole("switch", { name: "Известно с начала" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", ITEMS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", ITEMS)[0].body).toEqual({
      version: 3,
      data: [
        KEY,
        { ...LETTER, holder: "location:crypt" },
        {
          public: "Награда за маяк",
          known_at_start: true,
          id: "gold",
          name: "Кошель золота",
          description: "Сто золотых",
          base_item: null,
          holder: "npc:marta",
        },
      ],
    });
  });

  test("сохранённый предмет: «Закрепить» — item:<id>, «Перегенерировать» — шаг items с целью", async () => {
    const { server, form } = await openItems(world(), {
      [`PUT ${LOCKS}`]: { json: world({ version: 4 }) },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "items", target: "item:letter" }),
      },
    });

    press(form, "Закрепить: Письмо");
    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 3,
      locks: ["item:letter"],
    });

    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", { name: "Перегенерировать: Письмо" }),
      ).toBeEnabled(),
    );
    press(form, "Перегенерировать: Письмо");
    await vi.waitFor(() =>
      expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(
        1,
      ),
    );
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "items",
      target: "item:letter",
    });
  });

  test("новый несохранённый предмет закрепить и перегенерировать нельзя: кнопок нет", async () => {
    const { form } = await openItems(world());

    press(form, "Добавить предмет");
    const fresh = group(form, "Предмет 4");

    expect(
      within(fresh).queryByRole("button", { name: /Закрепить/ }),
    ).toBeNull();
    expect(
      within(fresh).queryByRole("button", { name: /Перегенерировать/ }),
    ).toBeNull();
  });

  test("409 version_conflict: диалог «Черновик изменился, обновить?»", async () => {
    const { form } = await openItems(world(), {
      [`PUT ${ITEMS}`]: {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });

    typeIn(group(form, "Письмо"), "Описание", "Мятое письмо");
    save(form);

    expect(
      await screen.findByRole("alertdialog", {
        name: "Черновик изменился, обновить?",
      }),
    ).toBeInTheDocument();
  });

  test("422 invalid_data: текст сервера и подробности списком", async () => {
    const { form } = await openItems(world(), {
      [`PUT ${ITEMS}`]: {
        status: 422,
        json: {
          detail: "Раздел не прошёл проверку.",
          code: "invalid_data",
          problems: ["2.holder: держатель — локация или персонаж"],
        },
      },
    });

    typeIn(group(form, "Письмо"), "Описание", "Мятое письмо");
    save(form);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Раздел не прошёл проверку.",
    );
    expect(
      screen.getByText("2.holder: держатель — локация или персонаж"),
    ).toBeInTheDocument();
  });
});

describe("«Предметы и награды» и «Наполнение» правят один раздел items", () => {
  const fromItems =
    "На экране «Предметы и награды» есть несохранённые правки: они пропадут.";
  const fromContents =
    "На экране «Наполнение» есть несохранённые правки: они пропадут.";

  test("правка предметов — «Наполнение» предупреждает; правка «Наполнения» — предупреждают «Предметы»", async () => {
    const { form } = await openItems(world());

    typeIn(group(form, "Ключ от склепа"), "Описание", "Ржавый ключ");
    goTo("Наполнение");
    const contents = screen.getByRole("region", { name: "Наполнение" });
    expect(contents).toHaveTextContent(fromItems);

    // Правка, возвращённая к сохранённому, предупреждение снимает.
    goTo("Предметы и награды");
    typeIn(group(form, "Ключ от склепа"), "Описание", KEY.description);
    goTo("Наполнение");
    expect(contents).not.toHaveTextContent(fromItems);

    typeIn(contents, "Заметки мастера", "Пахнет сыростью");
    goTo("Предметы и награды");
    expect(form).toHaveTextContent(fromContents);
  });

  test("подсказка: тайники — на «Наполнении»", async () => {
    const { form } = await openItems(world());

    expect(form).toHaveTextContent(
      "Спрятать предмет в тайник — отметкой у детали на шаге «Наполнение».",
    );
  });
});

// --- финальное покрытие блока 6 ---

describe("«Предметы и награды»: висящий держатель", () => {
  test("держатель, которого нет в справочнике, виден как «npc:ghost — нет в списке» и уходит без подмены", async () => {
    const lost = itemOf({ id: "lamp", name: "Лампа", holder: "npc:ghost" });
    const { server, form } = await openItems(
      world({ items: [KEY, RING, LETTER, lost] }),
    );
    const lamp = group(form, "Лампа");

    await vi.waitFor(() =>
      expect(optionValues(lamp, "Где лежит")).toContain("npc:marta"),
    );
    expect(
      within(lamp).getByRole("combobox", { name: "Где лежит" }),
    ).toHaveValue("npc:ghost");
    expect(
      within(lamp).getByRole("option", { name: "npc:ghost — нет в списке" }),
    ).toBeInTheDocument();
    // Держатель не локация — подписи «Лежит открыто» нет.
    expect(lamp).not.toHaveTextContent("Лежит открыто");
    typeIn(lamp, "Описание", "Медная лампа");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", ITEMS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", ITEMS)[0].body).toEqual({
      version: 3,
      data: [KEY, RING, LETTER, { ...lost, description: "Медная лампа" }],
    });
  });
});
