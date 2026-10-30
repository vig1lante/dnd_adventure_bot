import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  choose,
  deityOf,
  draftOf,
  group,
  jobOf,
  locationOf,
  npcOf,
  openStep,
  optionValues,
  press,
  typeIn,
} from "./fixtures";

const PATH = "/api/drafts/d1/sections/npcs";
const LOCKS = "/api/drafts/d1/locks";

const LIGHTHOUSE = locationOf({ id: "lighthouse", name: "Маяк" });
const CRYPT = locationOf({ id: "crypt", name: "Склеп" });
const MARTA = npcOf({ id: "marta", name: "Марта", location_id: "lighthouse" });
const FABIAN = npcOf({
  id: "fabian",
  name: "Фабиан",
  role: "контрабандист",
  location_id: "crypt",
  statblock: "bandit",
});

const world = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 3,
    locations: [LIGHTHOUSE, CRYPT],
    deities: [deityOf()],
    npcs: [MARTA, FABIAN],
    ...overrides,
  });

function openNpcs(draft: Draft, routes: Routes = {}) {
  return openStep(draft, "Персонажи", {
    [`PUT ${PATH}`]: { json: { ...draft, version: draft.version + 1 } },
    ...routes,
  });
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");

describe("шаг «Персонажи»", () => {
  test("персонажи из черновика: поля, где находится и статблок", async () => {
    const { form } = await openNpcs(world());

    const fabian = group(form, "Фабиан");
    expect(within(fabian).getByRole("textbox", { name: "Роль" })).toHaveValue(
      "контрабандист",
    );
    expect(
      within(fabian).getByRole("combobox", { name: "Где находится" }),
    ).toHaveValue("location:crypt");
    expect(
      within(fabian).getByRole("textbox", { name: "Статблок — id" }),
    ).toHaveValue("bandit");
  });

  test("«Где находится» — только локации из справочника черновика", async () => {
    const { form } = await openNpcs(world());

    await vi.waitFor(() =>
      expect(optionValues(group(form, "Марта"), "Где находится")).toEqual([
        "",
        "location:lighthouse",
        "location:crypt",
      ]),
    );
  });

  test("правка, добавление и удаление персонажей уходят списком с версией", async () => {
    const { server, form } = await openNpcs(world());

    press(form, "Удалить: Марта");
    typeIn(group(form, "Фабиан"), "Статблок — id", "");
    press(form, "Добавить персонажа");
    const fresh = group(form, "Персонаж 2");
    typeIn(fresh, "Идентификатор", "otto");
    typeIn(fresh, "Имя", "Отто");
    typeIn(fresh, "Роль", "трактирщик");
    typeIn(fresh, "Внешность", "толстый, в фартуке");
    typeIn(fresh, "Манера", "шутит невпопад");
    typeIn(fresh, "Мотив", "расплатиться с долгами");
    typeIn(fresh, "Тайна", "Должен Фабиану");
    typeIn(fresh, "Что знает", "Где спрятан ключ\n\n  Кто зажигает маяк  ");
    choose(fresh, "Отношение к отряду", "1");
    await vi.waitFor(() =>
      expect(optionValues(fresh, "Где находится")).toContain(
        "location:lighthouse",
      ),
    );
    choose(fresh, "Где находится", "location:lighthouse");
    typeIn(fresh, "Статблок — id", "commoner");
    typeIn(fresh, "Что знают игроки", "Хозяин таверны");
    fireEvent.click(
      within(fresh).getByRole("switch", { name: "Известно с начала" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 3,
      data: [
        { ...FABIAN, statblock: null },
        {
          public: "Хозяин таверны",
          known_at_start: true,
          id: "otto",
          name: "Отто",
          role: "трактирщик",
          appearance: "толстый, в фартуке",
          manner: "шутит невпопад",
          motive: "расплатиться с долгами",
          secret: "Должен Фабиану",
          knows: ["Где спрятан ключ", "Кто зажигает маяк"],
          attitude: 1,
          relations: [],
          location_id: "lighthouse",
          statblock: "commoner",
        },
      ],
    });
  });

  test("«Перегенерировать» персонажа: задание npcs с целью npc:<id>", async () => {
    const draft = world();
    const { server, form } = await openNpcs(draft, {
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "npcs", target: "npc:fabian" }),
      },
    });

    press(form, "Перегенерировать: Фабиан");

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "npcs",
      target: "npc:fabian",
    });
    expect(screen.getByText(/Шаг «Персонажи»/)).toBeInTheDocument();
  });

  test("у нового персонажа нет «Закрепить» и «Перегенерировать»; закреплённого не перегенерировать", async () => {
    const { form } = await openNpcs(world({ locks: ["npc:marta"] }));

    expect(
      within(form).getByRole("button", { name: "Перегенерировать: Марта" }),
    ).toBeDisabled();
    expect(
      within(form).getByRole("button", { name: "Перегенерировать: Фабиан" }),
    ).toBeEnabled();
    press(form, "Добавить персонажа");
    const fresh = group(form, "Персонаж 3");
    expect(
      within(fresh).queryByRole("button", { name: /Закрепить/ }),
    ).toBeNull();
    expect(
      within(fresh).queryByRole("button", { name: /Перегенерировать/ }),
    ).toBeNull();
  });

  test("«Закрепить» шлёт npc:<id> с версией; при несохранённых правках закрыт", async () => {
    const draft = world();
    const { server, form } = await openNpcs(draft, {
      [`PUT ${LOCKS}`]: {
        json: { ...draft, version: 4, locks: ["npc:marta"] },
      },
    });
    const lock = () =>
      within(form).getByRole("button", { name: "Закрепить: Марта" });

    typeIn(group(form, "Марта"), "Мотив", "уехать с мыса");
    expect(lock()).toBeDisabled();
    expect(form).toHaveTextContent(
      "Сначала сохраните раздел: закрепить и перегенерировать можно только сохранённое.",
    );
    typeIn(group(form, "Марта"), "Мотив", "сберечь маяк");
    fireEvent.click(lock());

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 3,
      locks: ["npc:marta"],
    });
  });
});

test("«Где находится» в локации, которой нет в справочнике: видна как есть и уходит голым id без подмены", async () => {
  const lost = npcOf({ id: "otto", name: "Отто", location_id: "old-mill" });
  const { server, form } = await openNpcs(world({ npcs: [lost] }));

  const place = within(group(form, "Отто")).getByRole("combobox", {
    name: "Где находится",
  });
  await vi.waitFor(() =>
    expect(optionValues(group(form, "Отто"), "Где находится")).toContain(
      "location:lighthouse",
    ),
  );
  expect(place).toHaveValue("location:old-mill");
  expect(
    within(place).getByRole("option", {
      name: "location:old-mill — нет в списке",
    }),
  ).toBeInTheDocument();
  typeIn(group(form, "Отто"), "Мотив", "найти мельницу");
  save(form);

  await vi.waitFor(() => expect(callsTo(server, "PUT", PATH)).toHaveLength(1));
  expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
    version: 3,
    data: [{ ...lost, motive: "найти мельницу" }],
  });
});
