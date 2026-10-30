import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import { callsTo, draftOf, factionOf, renderEditor } from "./fixtures";

const PATH = "/api/drafts/d1/sections/factions";
const LOCKS = "/api/drafts/d1/locks";

async function openFactions(draft: Draft, lockedReply?: Draft) {
  const view = renderEditor(draft, {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
    [`PUT ${LOCKS}`]: { json: lockedReply ?? draft },
  });
  fireEvent.click(
    within(
      await screen.findByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: /Фракции/ }),
  );
  return { ...view, form: screen.getByRole("region", { name: "Фракции" }) };
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

describe("шаг «Фракции»", () => {
  test("новая фракция: лидер ссылкой, база по id, отношение числом", async () => {
    const { server, form } = await openFactions(draftOf({ version: 2 }));

    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить фракцию" }),
    );
    const faction = group(form, "Фракция 1");
    type(faction, "Идентификатор", "guild");
    type(faction, "Название", "Гильдия теней");
    type(faction, "Цель", "Власть над портом");
    type(faction, "Методы", "Подкуп");
    type(faction, "Лидер", "npc:otto");
    type(faction, "База — id локации", "tavern");
    type(faction, "Тайна", "Лидер — двойник");
    fireEvent.change(
      within(faction).getByRole("combobox", { name: "Отношение к отряду" }),
      { target: { value: "-2" } },
    );
    type(faction, "Что знают игроки", "Воры порта");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 2,
      data: [
        {
          id: "guild",
          name: "Гильдия теней",
          goal: "Власть над портом",
          methods: "Подкуп",
          leader: "npc:otto",
          base_location: "tavern",
          secret: "Лидер — двойник",
          attitude_to_party: -2,
          public: "Воры порта",
          known_at_start: false,
        },
      ],
    });
  });

  test("пустые лидер и база уходят как null", async () => {
    const { server, form } = await openFactions(
      draftOf({
        factions: [factionOf({ leader: "npc:otto", base_location: "tavern" })],
      }),
    );
    const faction = group(form, "Гильдия теней");
    expect(within(faction).getByRole("textbox", { name: "Лидер" })).toHaveValue(
      "npc:otto",
    );

    type(faction, "Лидер", "");
    type(faction, "База — id локации", "  ");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      data: [{ leader: null, base_location: null }],
    });
  });

  test("«Закрепить» у фракции шлёт faction:<id>", async () => {
    const { server, form } = await openFactions(
      draftOf({ version: 4, factions: [factionOf()] }),
      draftOf({
        version: 5,
        factions: [factionOf()],
        locks: ["faction:guild"],
      }),
    );

    fireEvent.click(
      within(form).getByRole("button", { name: "Закрепить: Гильдия теней" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 4,
      locks: ["faction:guild"],
    });
  });

  test("удаление фракции", async () => {
    const { server, form } = await openFactions(
      draftOf({ factions: [factionOf()] }),
    );

    fireEvent.click(
      within(form).getByRole("button", { name: "Удалить: Гильдия теней" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 1,
      data: [],
    });
  });
});

describe("шаг «Фракции»: поля дневника", () => {
  test("«Известно с начала» и «Что знают игроки» у существующей фракции уходят с версией", async () => {
    const { server, form } = await openFactions(
      draftOf({
        version: 3,
        factions: [factionOf({ attitude_to_party: 2 })],
      }),
    );
    const faction = group(form, "Гильдия теней");
    expect(
      within(faction).getByRole("combobox", { name: "Отношение к отряду" }),
    ).toHaveValue("2");

    type(faction, "Что знают игроки", "Торговцы порта");
    fireEvent.click(
      within(faction).getByRole("switch", { name: "Известно с начала" }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 3,
      data: [
        factionOf({
          attitude_to_party: 2,
          public: "Торговцы порта",
          known_at_start: true,
        }),
      ],
    });
  });
});
