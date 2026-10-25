import { fireEvent, screen, within } from "@testing-library/react";
import { callsTo, draftOf, regionOf, renderEditor, rumorOf } from "./fixtures";

const PATH = "/api/drafts/d1/sections/world";

async function openWorld(draft = draftOf()) {
  const view = renderEditor(draft, {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
    "PUT /api/drafts/d1/locks": {
      json: draftOf({ ...draft, locks: ["step:world"] }),
    },
  });
  fireEvent.click(
    within(
      await screen.findByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: /Мир/ }),
  );
  return { ...view, form: screen.getByRole("region", { name: "Мир" }) };
}

const group = (scope: HTMLElement, name: string) =>
  within(scope).getByRole("group", { name });
const type = (scope: HTMLElement, name: string, value: string) =>
  fireEvent.change(within(scope).getByRole("textbox", { name }), {
    target: { value },
  });
const save = (form: HTMLElement) =>
  fireEvent.click(
    within(form).getByRole("button", { name: "Сохранить раздел" }),
  );

describe("шаг «Мир»", () => {
  test("пустой мир: история, края и слухи; добавить край и слух, сохранить с версией", async () => {
    const { server, form } = await openWorld(draftOf({ version: 4 }));

    type(form, "История мира", "Город стоит на костях дракона.");
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить край" }),
    );
    const region = group(form, "Край 1");
    type(region, "Идентификатор", "harbor");
    type(region, "Название", "Гавань");
    type(region, "Описание", "Порт в тумане");
    type(region, "Что знают игроки", "Порт, где пропал корабль");
    fireEvent.click(
      within(region).getByRole("switch", { name: "Известно с начала" }),
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить слух" }),
    );
    const rumor = group(form, "Слух 1");
    type(rumor, "Слух", "В маяке живёт призрак");
    fireEvent.click(
      within(rumor).getByRole("switch", { name: "Слух правдив" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 4,
      data: {
        history: "Город стоит на костях дракона.",
        regions: [
          {
            id: "harbor",
            name: "Гавань",
            description: "Порт в тумане",
            public: "Порт, где пропал корабль",
            known_at_start: true,
          },
        ],
        rumors: [
          {
            text: "В маяке живёт призрак",
            true: true,
            public: "",
            known_at_start: false,
          },
        ],
      },
    });
  });

  test("правка и удаление существующих краёв и слухов", async () => {
    const { server, form } = await openWorld(
      draftOf({
        version: 2,
        world: {
          history: "Давно",
          regions: [
            regionOf({ id: "harbor", name: "Гавань" }),
            regionOf({ id: "cliffs", name: "Утёсы" }),
          ],
          rumors: [rumorOf({ text: "Слух о маяке" })],
        },
      }),
    );

    fireEvent.click(
      within(form).getByRole("button", { name: "Удалить: Гавань" }),
    );
    type(group(form, "Утёсы"), "Описание", "Скалы над морем");
    fireEvent.click(
      within(form).getByRole("button", { name: "Удалить: Слух 1" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 2,
      data: {
        history: "Давно",
        regions: [
          regionOf({
            id: "cliffs",
            name: "Утёсы",
            description: "Скалы над морем",
          }),
        ],
        rumors: [],
      },
    });
  });

  test("«Закрепить раздел» шлёт step:world", async () => {
    const { server, form } = await openWorld();

    fireEvent.click(
      within(form).getByRole("button", { name: "Закрепить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", "/api/drafts/d1/locks")).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", "/api/drafts/d1/locks")[0].body).toEqual({
      version: 1,
      locks: ["step:world"],
    });
  });
});

// --- финальное покрытие блока 4 ---

describe("шаг «Мир»: поля дневника у слуха", () => {
  test("«Что знают игроки» и «Известно с начала» у слуха уходят в разделе world", async () => {
    const { server, form } = await openWorld(
      draftOf({
        version: 3,
        world: {
          history: "Давно",
          regions: [],
          rumors: [rumorOf({ text: "Слух о маяке", true: true })],
        },
      }),
    );
    const rumor = group(form, "Слух 1");
    expect(
      within(rumor).getByRole("switch", { name: "Слух правдив" }),
    ).toBeChecked();

    type(rumor, "Что знают игроки", "Про маяк шепчутся в таверне");
    fireEvent.click(
      within(rumor).getByRole("switch", { name: "Известно с начала" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 3,
      data: {
        history: "Давно",
        regions: [],
        rumors: [
          {
            text: "Слух о маяке",
            true: true,
            public: "Про маяк шепчутся в таверне",
            known_at_start: true,
          },
        ],
      },
    });
  });
});
