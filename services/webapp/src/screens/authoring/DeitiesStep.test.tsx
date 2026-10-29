import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import { callsTo, deityOf, draftOf, renderEditor } from "./fixtures";

const PATH = "/api/drafts/d1/sections/deities";
const LOCKS = "/api/drafts/d1/locks";

const TIAMAT = deityOf({
  id: "tiamat",
  name: "Тиамат",
  domains: ["обман", "война"],
});
const BAHAMUT = deityOf({
  id: "bahamut",
  name: "Бахамут",
  title: "Платиновый дракон",
  domains: ["жизнь", "свет"],
});

async function openDeities(draft: Draft, lockedReply?: Draft) {
  const view = renderEditor(draft, {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
    [`PUT ${LOCKS}`]: { json: lockedReply ?? draft },
  });
  fireEvent.click(
    within(
      await screen.findByRole("navigation", { name: "Шаги приключения" }),
    ).getByRole("button", { name: /Боги/ }),
  );
  return { ...view, form: screen.getByRole("region", { name: "Боги" }) };
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

describe("шаг «Боги»", () => {
  test("боги из черновика: поля и домены по строке", async () => {
    const { form } = await openDeities(draftOf({ deities: [TIAMAT] }));

    const tiamat = group(form, "Тиамат");
    expect(within(tiamat).getByRole("textbox", { name: "Имя" })).toHaveValue(
      "Тиамат",
    );
    expect(within(tiamat).getByRole("textbox", { name: "Домены" })).toHaveValue(
      "обман\nвойна",
    );
  });

  test("правка, добавление и удаление богов уходят списком с версией", async () => {
    const { server, form } = await openDeities(
      draftOf({ version: 3, deities: [TIAMAT, BAHAMUT] }),
    );

    fireEvent.click(
      within(form).getByRole("button", { name: "Удалить: Тиамат" }),
    );
    // После удаления первого бога поля второго остаются его полями.
    const bahamut = group(form, "Бахамут");
    expect(
      within(bahamut).getByRole("textbox", { name: "Домены" }),
    ).toHaveValue("жизнь\nсвет");
    type(bahamut, "Что знают игроки", "Добрый дракон-покровитель");
    fireEvent.click(
      within(bahamut).getByRole("switch", { name: "Известно с начала" }),
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить бога" }),
    );
    const fresh = group(form, "Бог 2");
    type(fresh, "Идентификатор", "umberlee");
    type(fresh, "Имя", "Амберли");
    type(fresh, "Титул", "Королева волн");
    type(fresh, "Домены", "море\n\n  буря  \n");
    type(fresh, "Символ", "волна");
    type(fresh, "Догма", "Море берёт своё.");
    type(fresh, "Облик", "Женщина из пены");
    type(fresh, "Отношение к смертным", "капризна");
    type(fresh, "Тайна", "Ей служит маяк");
    type(fresh, "Аватар — id статблока", "water-elemental");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 3,
      data: [
        {
          ...BAHAMUT,
          public: "Добрый дракон-покровитель",
          known_at_start: true,
        },
        {
          id: "umberlee",
          name: "Амберли",
          title: "Королева волн",
          domains: ["море", "буря"],
          symbol: "волна",
          dogma: "Море берёт своё.",
          appearance: "Женщина из пены",
          attitude_to_mortals: "капризна",
          secret: "Ей служит маяк",
          avatar_statblock: "water-elemental",
          public: "",
          known_at_start: false,
        },
      ],
    });
  });

  test("пустой аватар уходит как null", async () => {
    const { server, form } = await openDeities(
      draftOf({ deities: [deityOf({ avatar_statblock: "dragon" })] }),
    );

    type(group(form, "Тиамат"), "Аватар — id статблока", "");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      data: [{ avatar_statblock: null }],
    });
  });

  test("«Закрепить» у сохранённого бога шлёт deity:<id>", async () => {
    const { server, form } = await openDeities(
      draftOf({ version: 5, deities: [TIAMAT], locks: ["step:world"] }),
      draftOf({
        version: 6,
        deities: [TIAMAT],
        locks: ["step:world", "deity:tiamat"],
      }),
    );
    const lock = within(form).getByRole("button", {
      name: "Закрепить: Тиамат",
    });
    fireEvent.click(lock);

    await vi.waitFor(() =>
      expect(lock).toHaveAttribute("aria-pressed", "true"),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 5,
      locks: ["step:world", "deity:tiamat"],
    });
  });

  test("у нового бога кнопки «Закрепить» нет; пока он не сохранён, «Закрепить» закрыт с подсказкой; после сохранения — открыт", async () => {
    const { form } = await openDeities(
      draftOf({ version: 5, deities: [TIAMAT] }),
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить бога" }),
    );

    expect(
      within(group(form, "Бог 2")).queryByRole("button", { name: /Закрепить/ }),
    ).toBeNull();
    expect(
      within(form).getByRole("button", { name: "Закрепить: Тиамат" }),
    ).toBeDisabled();
    expect(
      within(form).getByText(
        "Сначала сохраните раздел: закрепить можно только сохранённое.",
      ),
    ).toBeInTheDocument();
  });

  test("после «Сохранить раздел» правленый и новый бог закрепляются", async () => {
    const edited = { ...TIAMAT, symbol: "чешуя" };
    const UMBERLEE = deityOf({ id: "umberlee", name: "Амберли" });
    const view = renderEditor(draftOf({ version: 5, deities: [TIAMAT] }), {
      [`PUT ${PATH}`]: {
        json: draftOf({ version: 6, deities: [edited, UMBERLEE] }),
      },
    });
    fireEvent.click(
      within(
        await screen.findByRole("navigation", { name: "Шаги приключения" }),
      ).getByRole("button", { name: /Боги/ }),
    );
    const form = screen.getByRole("region", { name: "Боги" });
    type(group(form, "Тиамат"), "Символ", "чешуя");
    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить бога" }),
    );
    type(group(form, "Бог 2"), "Идентификатор", "umberlee");
    type(group(form, "Бог 2"), "Имя", "Амберли");
    expect(
      within(form).getByRole("button", { name: "Закрепить: Тиамат" }),
    ).toBeDisabled();

    save(form);

    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", { name: "Закрепить: Амберли" }),
      ).toBeEnabled(),
    );
    expect(
      within(form).getByRole("button", { name: "Закрепить: Тиамат" }),
    ).toBeEnabled();
    expect(
      within(group(form, "Тиамат")).getByRole("textbox", { name: "Символ" }),
    ).toHaveValue("чешуя");
    expect(callsTo(view.server, "PUT", PATH)[0].body).toMatchObject({
      version: 5,
    });
  });
});

// --- финальное покрытие блока 4 ---

describe("шаг «Боги»: пределы и снятие закрепления", () => {
  test("седьмой бог: клиент список не режет, отказ сервера 422 с problems показан, введённое остаётся", async () => {
    const six = ["a", "b", "c", "d", "e", "f"].map((letter, index) =>
      deityOf({ id: `god-${letter}`, name: `Бог-${index + 1}` }),
    );
    const draft = draftOf({ version: 2, deities: six });
    const server = renderEditor(draft, {
      [`PUT ${PATH}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: [
            "deities: List should have at most 6 items after validation, not 7",
          ],
        },
      },
    }).server;
    fireEvent.click(
      within(
        await screen.findByRole("navigation", { name: "Шаги приключения" }),
      ).getByRole("button", { name: /Боги/ }),
    );
    const form = screen.getByRole("region", { name: "Боги" });

    fireEvent.click(
      within(form).getByRole("button", { name: "Добавить бога" }),
    );
    type(group(form, "Бог 7"), "Имя", "Седьмой");
    save(form);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Данные раздела не прошли проверку.");
    expect(alert).toHaveTextContent("Боги: List should have at most 6");
    const sent = callsTo(server, "PUT", PATH)[0].body as { data: unknown[] };
    expect(sent.data).toHaveLength(7);
    expect(
      within(group(form, "Седьмой")).getByRole("textbox", { name: "Имя" }),
    ).toHaveValue("Седьмой");
  });

  test("повторное нажатие «Закрепить» у бога снимает deity:<id>, остальные замки на месте", async () => {
    const { server, form } = await openDeities(
      draftOf({
        version: 4,
        deities: [TIAMAT, BAHAMUT],
        locks: ["deity:bahamut", "deity:tiamat", "step:concept"],
      }),
      draftOf({
        version: 5,
        deities: [TIAMAT, BAHAMUT],
        locks: ["deity:bahamut", "step:concept"],
      }),
    );
    const lock = within(form).getByRole("button", {
      name: "Закрепить: Тиамат",
    });
    expect(lock).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(lock);

    await vi.waitFor(() =>
      expect(lock).toHaveAttribute("aria-pressed", "false"),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 4,
      locks: ["deity:bahamut", "step:concept"],
    });
    expect(
      within(form).getByRole("button", { name: "Закрепить: Бахамут" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
});
