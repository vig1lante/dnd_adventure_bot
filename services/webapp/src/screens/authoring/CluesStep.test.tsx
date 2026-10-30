import { screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import type { Routes } from "./fixtures";
import {
  callsTo,
  choose,
  clueOf,
  draftOf,
  featureOf,
  findingOf,
  group,
  itemOf,
  jobOf,
  locationOf,
  npcOf,
  openStep,
  optionValues,
  press,
  reportOf,
  revelationOf,
  typeIn,
} from "./fixtures";

const REVELATIONS = "/api/drafts/d1/sections/revelations";
const LOCKS = "/api/drafts/d1/locks";

const CRYPT = locationOf({
  id: "crypt",
  name: "Склеп",
  features: [featureOf({ id: "coffin", name: "Гроб" })],
});
const HALL = locationOf({
  id: "hall",
  name: "Зал",
  features: [featureOf({ id: "ledger", name: "" })],
});
const MARTA = npcOf({ id: "marta", name: "Марта" });
const LETTER = itemOf({ id: "letter", name: "Письмо", holder: "npc:marta" });
const SMUGGLER = revelationOf({
  id: "smuggler",
  statement: "Фабиан — контрабандист",
  clues: [
    clueOf({ id: "torn-letter", where: "feature:coffin" }),
    clueOf({
      id: "marta-word",
      description: "Марта видела лодку ночью",
      where: "npc:marta",
    }),
    clueOf({
      id: "seal",
      description: "Печать гильдии на письме",
      where: "item:letter",
    }),
  ],
});

const world = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 3,
    locations: [CRYPT, HALL],
    npcs: [MARTA],
    items: [LETTER],
    revelations: [SMUGGLER],
    ...overrides,
  });

function openClues(draft: Draft, routes: Routes = {}) {
  return openStep(draft, "Улики", {
    [`PUT ${REVELATIONS}`]: {
      json: { ...draft, version: draft.version + 1 },
    },
    ...routes,
  });
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");

describe("шаг «Улики»", () => {
  test("откровения черновика: вывод и улики с местом", async () => {
    const { form } = await openClues(world());

    const smuggler = group(form, "Откровение smuggler");
    expect(
      within(smuggler).getByRole("textbox", { name: "Вывод" }),
    ).toHaveValue("Фабиан — контрабандист");
    const letter = group(smuggler, "Улика torn-letter");
    expect(
      within(letter).getByRole("textbox", { name: "Описание" }),
    ).toHaveValue("Обрывок письма с печатью гильдии");
    await vi.waitFor(() =>
      expect(
        within(group(smuggler, "Улика marta-word")).getByRole("combobox", {
          name: "Где лежит",
        }),
      ).toHaveValue("npc:marta"),
    );
    expect(
      within(letter).getByRole("combobox", { name: "Где лежит" }),
    ).toHaveValue("feature:coffin");
  });

  test("«Где лежит» — персонажи и предметы из справочника и детали локаций черновика", async () => {
    const { form } = await openClues(world());
    const clue = group(group(form, "Откровение smuggler"), "Улика seal");

    await vi.waitFor(() =>
      expect(optionValues(clue, "Где лежит")).toEqual([
        "",
        "npc:marta",
        "item:letter",
        "feature:coffin",
        "feature:ledger",
      ]),
    );
    expect(
      within(clue).getByRole("option", { name: "Гроб (деталь, Склеп)" }),
    ).toHaveValue("feature:coffin");
    // Деталь без названия подписана своим id.
    expect(
      within(clue).getByRole("option", { name: "ledger (деталь, Зал)" }),
    ).toHaveValue("feature:ledger");
  });

  test("новое откровение с уликами уходит списком revelations с версией", async () => {
    const { server, form } = await openClues(world());

    press(form, "Добавить откровение");
    const fresh = group(form, "Откровение 2");
    typeIn(fresh, "Идентификатор", "lighthouse-keeper");
    typeIn(fresh, "Вывод", "Маяк гасит сама Марта");
    press(fresh, "Добавить улику");
    const clue = group(fresh, "Улика 1");
    typeIn(clue, "Идентификатор улики", "wet-boots");
    typeIn(clue, "Описание", "Мокрые сапоги у двери маяка");
    await vi.waitFor(() =>
      expect(optionValues(clue, "Где лежит")).toContain("npc:marta"),
    );
    choose(clue, "Где лежит", "feature:ledger");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", REVELATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", REVELATIONS)[0].body).toEqual({
      version: 3,
      data: [
        SMUGGLER,
        {
          id: "lighthouse-keeper",
          statement: "Маяк гасит сама Марта",
          clues: [
            {
              id: "wet-boots",
              description: "Мокрые сапоги у двери маяка",
              where: "feature:ledger",
            },
          ],
        },
      ],
    });
  });

  test("меньше трёх улик — подсказка, сохранить можно: ошибку даёт проверка сервера", async () => {
    const { server, form } = await openClues(world());
    const smuggler = group(form, "Откровение smuggler");
    expect(smuggler).not.toHaveTextContent("меньше трёх");

    press(smuggler, "Удалить: Улика seal");

    expect(smuggler).toHaveTextContent(
      "Улик меньше трёх: проверка не пропустит публикацию.",
    );
    save(form);
    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", REVELATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", REVELATIONS)[0].body).toEqual({
      version: 3,
      data: [{ ...SMUGGLER, clues: SMUGGLER.clues.slice(0, 2) }],
    });
  });

  test("сохранённое откровение: «Закрепить» — revelation:<id>, «Перегенерировать» — шаг clues с целью", async () => {
    const { server, form } = await openClues(world(), {
      [`PUT ${LOCKS}`]: { json: world({ version: 4 }) },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "clues", target: "revelation:smuggler" }),
      },
    });

    press(form, "Закрепить: Откровение smuggler");
    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 3,
      locks: ["revelation:smuggler"],
    });

    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", {
          name: "Перегенерировать: Откровение smuggler",
        }),
      ).toBeEnabled(),
    );
    press(form, "Перегенерировать: Откровение smuggler");
    await vi.waitFor(() =>
      expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(
        1,
      ),
    );
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "clues",
      target: "revelation:smuggler",
    });
  });

  test("409 version_conflict: диалог; 422 — подробности", async () => {
    let reply = 0;
    const { form } = await openClues(world(), {
      [`PUT ${REVELATIONS}`]: () =>
        reply++ === 0
          ? {
              status: 422,
              json: {
                detail: "Раздел не прошёл проверку.",
                code: "invalid_data",
                problems: [
                  "revelations.0.clues.0.where: деталь, персонаж или предмет",
                ],
              },
            }
          : {
              status: 409,
              json: { detail: "Черновик изменился.", code: "version_conflict" },
            },
    });

    typeIn(group(form, "Откровение smuggler"), "Вывод", "Фабиан лжёт");
    save(form);
    // Путь поля — словами формы: раздел, откровение, улика и подпись поля.
    expect(
      await screen.findByText(
        /^Откровения → «smuggler» → Улики → .+ → Где лежит: деталь, персонаж или предмет$/,
      ),
    ).toBeInTheDocument();

    save(form);
    expect(
      await screen.findByRole("alertdialog", {
        name: "Черновик изменился, обновить?",
      }),
    ).toBeInTheDocument();
  });
});

describe("«Улики»: висящее место и путь от ошибки проверки", () => {
  test("улика у удалённой детали видна как «feature:gone — нет в списке» и уходит без подмены", async () => {
    const lost = revelationOf({
      ...SMUGGLER,
      clues: [...SMUGGLER.clues, clueOf({ id: "ash", where: "feature:gone" })],
    });
    const { server, form } = await openClues(world({ revelations: [lost] }));
    const clue = group(group(form, "Откровение smuggler"), "Улика ash");

    expect(
      within(clue).getByRole("combobox", { name: "Где лежит" }),
    ).toHaveValue("feature:gone");
    expect(
      within(clue).getByRole("option", { name: "feature:gone — нет в списке" }),
    ).toBeInTheDocument();
    typeIn(group(form, "Откровение smuggler"), "Вывод", "Фабиан лжёт");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", REVELATIONS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", REVELATIONS)[0].body).toEqual({
      version: 3,
      data: [{ ...lost, statement: "Фабиан лжёт" }],
    });
  });

  test("меньше трёх улик: «Публикация» закрыта → «Проверка» → ссылка ведёт на «Улики»", async () => {
    const two = revelationOf({
      ...SMUGGLER,
      clues: SMUGGLER.clues.slice(0, 2),
    });
    const draft = world({ revelations: [two], adventure_id: "foggy-harbor" });
    const report = reportOf({
      errors: [
        findingOf({
          code: "too_few_clues",
          message: "у откровения «smuggler» меньше трёх улик",
          ref: "revelation:smuggler",
          step: "clues",
        }),
      ],
      readiness_percent: 99,
    });
    const { server } = await openStep(draft, "Публикация", {
      "POST /api/drafts/d1/validate": { json: report },
    });
    const publish = screen.getByRole("region", { name: "Публикация" });

    expect(
      await within(publish).findByText("Сначала исправьте ошибки проверки: 1."),
    ).toBeInTheDocument();
    expect(
      within(publish).getByRole("button", { name: "Опубликовать" }),
    ).toBeDisabled();
    press(publish, "Перейти к шагу «Проверка»");
    const review = screen.getByRole("region", { name: "Проверка" });
    const errors = await within(review).findByRole("list", { name: "Ошибки" });
    expect(errors).toHaveTextContent(
      "у откровения «smuggler» меньше трёх улик",
    );
    press(errors, "Перейти к шагу «Улики»");

    const clues = screen.getByRole("region", { name: "Улики" });
    expect(group(clues, "Откровение smuggler")).toHaveTextContent(
      "Улик меньше трёх: проверка не пропустит публикацию.",
    );
    expect(callsTo(server, "POST", "/api/drafts/d1/publish")).toHaveLength(0);
  });
});
