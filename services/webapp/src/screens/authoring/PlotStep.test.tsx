import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import { actOf, callsTo, draftOf, renderEditor } from "./fixtures";

const PATH = "/api/drafts/d1/sections/plot";

async function openPlot(draft: Draft) {
  const view = renderEditor(draft, {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
    "PUT /api/drafts/d1/locks": {
      json: draftOf({ ...draft, locks: ["step:plot"] }),
    },
  });
  fireEvent.click(
    within(
      await screen.findByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: /Сюжет/ }),
  );
  return { ...view, form: screen.getByRole("region", { name: "Сюжет" }) };
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

describe("шаг «Сюжет»", () => {
  test("пустой сюжет: акт со сценами, концовки и злодей уходят разделом plot с версией", async () => {
    const { server, form } = await openPlot(draftOf({ version: 6 }));

    fireEvent.click(within(form).getByRole("button", { name: "Добавить акт" }));
    const act = group(form, "Акт 1");
    type(act, "Идентификатор", "arrival");
    type(act, "Название", "Прибытие");
    type(act, "Кратко", "Отряд приплывает в гавань");
    type(act, "Ключевые сцены", "Пирс\nТаверна");
    type(form, "Концовки", "Корабль найден\nГавань сгорела");
    type(form, "Злодей", "npc:otto");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 6,
      data: {
        acts: [
          {
            id: "arrival",
            title: "Прибытие",
            summary: "Отряд приплывает в гавань",
            key_scenes: ["Пирс", "Таверна"],
          },
        ],
        endings: ["Корабль найден", "Гавань сгорела"],
        villain: "npc:otto",
      },
    });
  });

  test("существующий сюжет: удалить акт, очистить злодея — null", async () => {
    const { server, form } = await openPlot(
      draftOf({
        plot: {
          acts: [
            actOf({ id: "a1", title: "Прибытие" }),
            actOf({ id: "a2", title: "Шторм" }),
          ],
          endings: ["Победа"],
          villain: "deity:umberlee",
        },
      }),
    );
    expect(within(form).getByRole("textbox", { name: "Концовки" })).toHaveValue(
      "Победа",
    );

    fireEvent.click(
      within(form).getByRole("button", { name: "Удалить: Прибытие" }),
    );
    type(form, "Злодей", "");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 1,
      data: {
        acts: [actOf({ id: "a2", title: "Шторм" })],
        endings: ["Победа"],
        villain: null,
      },
    });
  });

  test("«Закрепить раздел» шлёт step:plot", async () => {
    const { server, form } = await openPlot(draftOf());

    fireEvent.click(
      within(form).getByRole("button", { name: "Закрепить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", "/api/drafts/d1/locks")).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", "/api/drafts/d1/locks")[0].body).toEqual({
      version: 1,
      locks: ["step:plot"],
    });
  });
});
