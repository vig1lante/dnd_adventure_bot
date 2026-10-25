import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  choose,
  deityOf,
  draftOf,
  factionOf,
  group,
  itemOf,
  jobOf,
  locationOf,
  npcOf,
  openStep,
  optionValues,
  press,
  relationOf,
  typeIn,
} from "./fixtures";

const PATH = "/api/drafts/d1/sections/relations";
const LOCKS = "/api/drafts/d1/locks";

const DEBT = relationOf({
  id: "fabian-debtor-marta",
  a: "npc:fabian",
  b: "npc:marta",
  kind: "debtor",
});
const FAITH = relationOf({
  id: "marta-worships-umberlee",
  a: "npc:marta",
  b: "deity:umberlee",
  kind: "worships",
  secret: true,
});
const FEUD = relationOf({
  id: "guild-enemy-fabian",
  a: "faction:guild",
  b: "npc:fabian",
  kind: "enemy",
});

const world = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 6,
    deities: [deityOf({ id: "umberlee", name: "Амберли" })],
    factions: [factionOf({ id: "guild", name: "Гильдия теней" })],
    npcs: [
      npcOf({ id: "marta", name: "Марта" }),
      npcOf({ id: "fabian", name: "Фабиан" }),
    ],
    locations: [locationOf({ id: "lighthouse", name: "Маяк" })],
    items: [itemOf()],
    relations: [DEBT, FAITH, FEUD],
    ...overrides,
  });

async function openRelations(draft: Draft, routes: Routes = {}) {
  const view = await openStep(draft, "Связи", {
    [`PUT ${PATH}`]: { json: { ...draft, version: draft.version + 1 } },
    ...routes,
  });
  // Подписи связей берут имена из справочника: ждём, пока он загрузится.
  await within(view.form).findByRole("group", {
    name: "Фабиан — должник — Марта",
  });
  return view;
}

const visibleRelations = (form: HTMLElement) =>
  within(group(form, "Связи"))
    .queryAllByRole("group")
    .filter((item) => item.classList.contains("authoring-entity"))
    .map((item) => item.querySelector("legend")?.textContent);
const save = (form: HTMLElement) => press(form, "Сохранить раздел");

describe("шаг «Связи»", () => {
  test("связи читаются «кто — кем — кому»; тайная помечена", async () => {
    const { form } = await openRelations(world());

    expect(visibleRelations(form)).toEqual([
      "Фабиан — должник — Марта",
      "Марта — поклоняется — Амберли",
      "Гильдия теней — враг — Фабиан",
    ]);
    expect(
      within(group(form, "Марта — поклоняется — Амберли")).getByRole("switch", {
        name: "Тайная связь",
      }),
    ).toBeChecked();
  });

  test("фильтр по сущности оставляет только её связи; «все» возвращает список", async () => {
    const { form } = await openRelations(world());

    choose(form, "Показать связи", "npc:marta");
    expect(visibleRelations(form)).toEqual([
      "Фабиан — должник — Марта",
      "Марта — поклоняется — Амберли",
    ]);

    choose(form, "Показать связи", "faction:guild");
    expect(visibleRelations(form)).toEqual(["Гильдия теней — враг — Фабиан"]);

    choose(form, "Показать связи", "");
    expect(visibleRelations(form)).toHaveLength(3);
  });

  test("фильтр переживает «Сохранить раздел»", async () => {
    const draft = world();
    const { server, form } = await openRelations(draft, {
      [`PUT ${PATH}`]: {
        json: {
          ...draft,
          version: 7,
          relations: [{ ...DEBT, secret: true }, FAITH, FEUD],
        },
      },
    });

    choose(form, "Показать связи", "npc:marta");
    fireEvent.click(
      within(group(form, "Фабиан — должник — Марта")).getByRole("switch", {
        name: "Тайная связь",
      }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", {
          name: "Закрепить: Фабиан — должник — Марта",
        }),
      ).toBeEnabled(),
    );
    expect(
      within(form).getByRole("combobox", { name: "Показать связи" }),
    ).toHaveValue("npc:marta");
    expect(visibleRelations(form)).toEqual([
      "Фабиан — должник — Марта",
      "Марта — поклоняется — Амберли",
    ]);
  });

  test("скрытые фильтром связи сохраняются как были", async () => {
    const { server, form } = await openRelations(world());

    choose(form, "Показать связи", "deity:umberlee");
    fireEvent.click(
      within(group(form, "Марта — поклоняется — Амберли")).getByRole("switch", {
        name: "Тайная связь",
      }),
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 6,
      data: [DEBT, { ...FAITH, secret: false }, FEUD],
    });
  });

  test("добавление через RefPicker: концы — персонажи, фракции, боги, локации; уходит списком с версией", async () => {
    const { server, form } = await openRelations(world({ relations: [DEBT] }));

    press(form, "Добавить связь");
    const fresh = group(form, "Связь 2");
    expect(optionValues(fresh, "Кто")).toEqual([
      "",
      "deity:umberlee",
      "faction:guild",
      "npc:marta",
      "npc:fabian",
      "location:lighthouse",
    ]);
    typeIn(fresh, "Идентификатор", "lighthouse-patron-marta");
    choose(fresh, "Кто", "location:lighthouse");
    choose(fresh, "Кем", "patron");
    choose(fresh, "Кому", "npc:marta");
    fireEvent.click(
      within(fresh).getByRole("switch", { name: "Тайная связь" }),
    );
    fireEvent.click(within(fresh).getByRole("switch", { name: "Взаимная" }));
    expect(fresh.querySelector("legend")).toHaveTextContent(
      "Маяк — покровитель — Марта",
    );
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 6,
      data: [
        DEBT,
        {
          id: "lighthouse-patron-marta",
          a: "location:lighthouse",
          b: "npc:marta",
          kind: "patron",
          note: "",
          secret: true,
          mutual: false,
        },
      ],
    });
  });

  test("при фильтре новая связь начинается с выбранной сущности и остаётся видна", async () => {
    const { form } = await openRelations(world());

    choose(form, "Показать связи", "faction:guild");
    press(form, "Добавить связь");

    const fresh = group(form, "Гильдия теней — союзник — …");
    expect(within(fresh).getByRole("combobox", { name: "Кто" })).toHaveValue(
      "faction:guild",
    );
    expect(visibleRelations(form)).toEqual([
      "Гильдия теней — враг — Фабиан",
      "Гильдия теней — союзник — …",
    ]);
  });

  test("вид «другое» требует пояснения: без него сохранить нельзя", async () => {
    const { server, form } = await openRelations(world({ relations: [DEBT] }));
    const debt = group(form, "Фабиан — должник — Марта");

    choose(debt, "Кем", "other");
    expect(within(debt).getByRole("alert")).toHaveTextContent(
      "У связи «другое» пояснение обязательно.",
    );
    expect(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    ).toBeDisabled();

    typeIn(debt, "Пояснение", "должен за спасение");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      data: [{ kind: "other", note: "должен за спасение" }],
    });
  });

  test("связь «другое» без пояснения, скрытая фильтром, — подсказка, почему сохранить нельзя", async () => {
    const { form } = await openRelations(world());

    choose(group(form, "Фабиан — должник — Марта"), "Кем", "other");
    choose(form, "Показать связи", "faction:guild");

    expect(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    ).toBeDisabled();
    expect(form).toHaveTextContent(
      "Есть связь «другое» без пояснения: снимите фильтр, чтобы её найти.",
    );
  });

  test("«Перегенерировать» связь: задание relations с целью relation:<id>", async () => {
    const { server, form } = await openRelations(world(), {
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({
          step: "relations",
          target: "relation:guild-enemy-fabian",
        }),
      },
    });

    press(form, "Перегенерировать: Гильдия теней — враг — Фабиан");

    expect(await screen.findByText("Генерирую…")).toBeInTheDocument();
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "relations",
      target: "relation:guild-enemy-fabian",
    });
  });

  test("«Закрепить» шлёт relation:<id> с версией; при несохранённых правках закрыт", async () => {
    const draft = world();
    const { server, form } = await openRelations(draft, {
      [`PUT ${LOCKS}`]: {
        json: { ...draft, version: 7, locks: ["relation:fabian-debtor-marta"] },
      },
    });
    const lock = () =>
      within(form).getByRole("button", {
        name: "Закрепить: Фабиан — должник — Марта",
      });

    typeIn(group(form, "Фабиан — должник — Марта"), "Пояснение", "долг чести");
    expect(lock()).toBeDisabled();
    typeIn(group(form, "Фабиан — должник — Марта"), "Пояснение", "");
    fireEvent.click(lock());

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 6,
      locks: ["relation:fabian-debtor-marta"],
    });
  });
});

// --- финальное покрытие блока 5 ---

test("у новой связи нет «Закрепить» и «Перегенерировать»: их получает только сохранённая", async () => {
  const { form } = await openRelations(world({ relations: [DEBT] }));

  press(form, "Добавить связь");

  const fresh = group(form, "Связь 2");
  expect(within(fresh).queryByRole("button", { name: /Закрепить/ })).toBeNull();
  expect(
    within(fresh).queryByRole("button", { name: /Перегенерировать/ }),
  ).toBeNull();
  // Кнопка у сохранённой связи есть, но новая строка — несохранённая правка: пока раздел не
  // сохранён, перегенерация закрыта (fix-раунд 1 B5, §9 minor 2).
  expect(
    within(form).getByRole("button", {
      name: "Перегенерировать: Фабиан — должник — Марта",
    }),
  ).toBeDisabled();
});

test("конец связи, которого нет в справочнике, виден как есть и уходит без подмены", async () => {
  const ghost = relationOf({
    id: "ghost-enemy-marta",
    a: "npc:ghost",
    b: "npc:marta",
    kind: "enemy",
  });
  const { server, form } = await openRelations(
    world({ relations: [DEBT, ghost] }),
  );

  const row = group(form, "npc:ghost — враг — Марта");
  const who = within(row).getByRole("combobox", { name: "Кто" });
  expect(who).toHaveValue("npc:ghost");
  expect(
    within(who).getByRole("option", { name: "npc:ghost — нет в списке" }),
  ).toBeInTheDocument();
  typeIn(row, "Пояснение", "мстит за брата");
  save(form);

  await vi.waitFor(() => expect(callsTo(server, "PUT", PATH)).toHaveLength(1));
  expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
    version: 6,
    data: [DEBT, { ...ghost, note: "мстит за брата" }],
  });
});
