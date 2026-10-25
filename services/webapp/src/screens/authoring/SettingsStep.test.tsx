import { fireEvent, screen, within } from "@testing-library/react";
import { callsTo, draftOf, renderEditor, stepsOf } from "./fixtures";

const PATH = "/api/drafts/d1/sections/settings";

const FILLED = draftOf({
  version: 7,
  title: "Туманная гавань",
  adventure_id: "foggy-harbor",
  journal_intro: "Отряд ищет пропавший корабль.",
  settings: {
    pitch: "Корабль исчез в тумане",
    tone: "мрачный",
    target_hours: 3.5,
    party_size: 4,
    party_level: 3,
    modules: ["bg3_flavor"],
    profanity: true,
  },
});

async function openConcept(draft = FILLED) {
  const view = renderEditor(draft, {
    [`PUT ${PATH}`]: {
      json: draftOf({ ...draft, version: draft.version + 1 }),
    },
  });
  await screen.findByRole("navigation", { name: "Шаги приключения" });
  return { ...view, form: screen.getByRole("region", { name: "Замысел" }) };
}

const textbox = (form: HTMLElement, name: string) =>
  within(form).getByRole("textbox", { name });

describe("шаг «Замысел»", () => {
  test("поля показаны из черновика; slug приключения только для чтения", async () => {
    const { form } = await openConcept();

    expect(textbox(form, "Название")).toHaveValue("Туманная гавань");
    expect(textbox(form, "Завязка")).toHaveValue("Корабль исчез в тумане");
    expect(textbox(form, "Тон")).toHaveValue("мрачный");
    expect(textbox(form, "Длина игры, часов")).toHaveValue("3,5");
    expect(textbox(form, "Игроков")).toHaveValue("4");
    expect(textbox(form, "Уровень героев")).toHaveValue("3");
    expect(textbox(form, "Главное для дневника")).toHaveValue(
      "Отряд ищет пропавший корабль.",
    );
    expect(
      within(form).getByRole("checkbox", { name: "Специи BG3" }),
    ).toBeChecked();
    expect(
      within(form).getByRole("checkbox", { name: "Привал у костра" }),
    ).not.toBeChecked();
    expect(
      within(form).getByRole("switch", { name: "Мат и грубая речь" }),
    ).toBeChecked();
    expect(form).toHaveTextContent("foggy-harbor");
    expect(
      within(form).queryByRole("textbox", { name: /slug|id приключения/ }),
    ).toBeNull();
  });

  test("сохранение шлёт весь раздел settings с версией: числа — числами", async () => {
    const { server, form } = await openConcept();

    fireEvent.change(textbox(form, "Название"), {
      target: { value: "Гавань теней" },
    });
    fireEvent.change(textbox(form, "Длина игры, часов"), {
      target: { value: "4,5" },
    });
    fireEvent.change(textbox(form, "Игроков"), { target: { value: "5" } });
    fireEvent.change(textbox(form, "Уровень героев"), {
      target: { value: "2" },
    });
    fireEvent.change(textbox(form, "Главное для дневника"), {
      target: { value: "Найти корабль до шторма." },
    });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Специи BG3" }));
    fireEvent.click(
      within(form).getByRole("checkbox", { name: "Привал у костра" }),
    );
    fireEvent.click(
      within(form).getByRole("switch", { name: "Мат и грубая речь" }),
    );
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toEqual({
      version: 7,
      data: {
        title: "Гавань теней",
        journal_intro: "Найти корабль до шторма.",
        pitch: "Корабль исчез в тумане",
        tone: "мрачный",
        target_hours: 4.5,
        party_size: 5,
        party_level: 2,
        modules: ["campfire"],
        profanity: false,
      },
    });
  });

  test("не число в числовом поле — подсказка у поля, запрос не уходит", async () => {
    const { server, form } = await openConcept();

    fireEvent.change(textbox(form, "Игроков"), { target: { value: "пять" } });
    fireEvent.change(textbox(form, "Уровень героев"), {
      target: { value: "2,5" },
    });

    expect(textbox(form, "Игроков")).toBeInvalid();
    expect(textbox(form, "Уровень героев")).toBeInvalid();
    const save = within(form).getByRole("button", { name: "Сохранить раздел" });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(callsTo(server, "PUT", PATH)).toHaveLength(0);
  });

  // Статус шага сервер считает по содержимому раздела (contract-addendum §3): Замысел, заполненный
  // руками, без id приключения — «черновик»; «готово» он станет после генерации (id даёт concept).
  test("ручной Замысел после «Сохранить раздел» — «черновик», подсказка про id остаётся", async () => {
    const draft = draftOf({ version: 1 });
    renderEditor(draft, {
      [`PUT ${PATH}`]: {
        json: draftOf({
          version: 2,
          title: "Маяк",
          steps: stepsOf({
            concept: { status: "draft", updated_at: "2026-10-25T10:00:00Z" },
          }),
        }),
      },
    });
    const nav = await screen.findByRole("navigation", {
      name: "Шаги приключения",
    });
    const concept = () => within(nav).getByRole("button", { name: /Замысел/ });
    const form = screen.getByRole("region", { name: "Замысел" });
    expect(concept()).toHaveTextContent("пусто");

    fireEvent.change(textbox(form, "Название"), { target: { value: "Маяк" } });
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() => expect(concept()).toHaveTextContent("черновик"));
    expect(form).toHaveTextContent(
      "id приключения появится после генерации замысла.",
    );
  });

  test("пустой черновик: slug появится после генерации замысла", async () => {
    const { form } = await openConcept(draftOf());

    expect(form).toHaveTextContent(/появится после генерации замысла/);
  });
});

// --- финальное покрытие блока 4 ---

describe("шаг «Замысел»: модули и закрепление", () => {
  test("незнакомый клиенту модуль черновика показан флажком и уходит обратно при сохранении", async () => {
    const { server, form } = await openConcept(
      draftOf({
        version: 2,
        settings: { ...FILLED.settings, modules: ["campfire", "homebrew"] },
      }),
    );

    expect(
      within(form).getByRole("checkbox", { name: "homebrew" }),
    ).toBeChecked();
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      version: 2,
      data: { modules: ["campfire", "homebrew"] },
    });
  });

  test("закреплённый Замысел: кнопка нажата, генерация шага недоступна", async () => {
    const { form } = await openConcept(draftOf({ locks: ["step:concept"] }));

    expect(
      within(form).getByRole("button", { name: "Закрепить раздел" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      within(form).getByRole("button", { name: "Сгенерировать шаг" }),
    ).toBeDisabled();
  });
});
