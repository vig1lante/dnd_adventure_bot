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
  test("поля показаны из черновика; id приключения — поле Замысла", async () => {
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
    expect(textbox(form, "id приключения")).toHaveValue("foggy-harbor");
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
        adventure_id: "foggy-harbor",
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

  // Статус шага сервер считает по содержимому раздела: Замысел, заполненный руками, без id
  // приключения — «черновик»; «готово» он станет, когда id задан.
  test("ручной Замысел без id после «Сохранить раздел» — «черновик», id уходит пустым (null)", async () => {
    const draft = draftOf({ version: 1 });
    const { server } = renderEditor(draft, {
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
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      data: { title: "Маяк", adventure_id: null },
    });
  });

  test("пустой черновик: поле id пусто, подсказка — шаблон, генерация и неизменность после публикации", async () => {
    const { form } = await openConcept(draftOf());

    expect(textbox(form, "id приключения")).toHaveValue("");
    expect(form).toHaveTextContent(
      "Строчная латиница, цифры, «-» и «_»; начинается с буквы или цифры; до 64 знаков. Пусто — id даст генерация замысла. После публикации id не меняется.",
    );
  });

  test("id задаёт автор: уходит в settings; стёртое поле — null", async () => {
    const { server, form } = await openConcept();

    fireEvent.change(textbox(form, "id приключения"), {
      target: { value: "harbor-of-shadows" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );
    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      version: 7,
      data: { adventure_id: "harbor-of-shadows" },
    });
  });

  test("стёртый id уходит как null", async () => {
    const { server, form } = await openConcept();

    fireEvent.change(textbox(form, "id приключения"), {
      target: { value: "  " },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", PATH)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", PATH)[0].body).toMatchObject({
      data: { adventure_id: null },
    });
  });

  test("422 adventure_id_locked после публикации: текст сервера, правка на месте", async () => {
    renderEditor(FILLED, {
      [`PUT ${PATH}`]: {
        status: 422,
        json: {
          detail: "Приключение уже опубликовано под этим id",
          code: "adventure_id_locked",
        },
      },
    });
    await screen.findByRole("navigation", { name: "Шаги приключения" });
    const form = screen.getByRole("region", { name: "Замысел" });

    fireEvent.change(textbox(form, "id приключения"), {
      target: { value: "other-id" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Приключение уже опубликовано под этим id",
    );
    expect(textbox(form, "id приключения")).toHaveValue("other-id");
  });
});

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

// --- id приключения ---

describe("шаг «Замысел»: формат id приключения", () => {
  test("id не по шаблону: 422 invalid_data — текст сервера и правило списком, правка на месте", async () => {
    renderEditor(FILLED, {
      [`PUT ${PATH}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: [
            "settings.adventure_id: только строчная латиница, цифры, «-» и «_», до 64 знаков",
          ],
        },
      },
    });
    await screen.findByRole("navigation", { name: "Шаги приключения" });
    const form = screen.getByRole("region", { name: "Замысел" });

    fireEvent.change(textbox(form, "id приключения"), {
      target: { value: "Foggy-Harbor" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Данные раздела не прошли проверку.",
    );
    expect(
      screen.getByText(
        "Замысел → id приключения: только строчная латиница, цифры, «-» и «_», до 64 знаков",
      ),
    ).toBeInTheDocument();
    expect(textbox(form, "id приключения")).toHaveValue("Foggy-Harbor");
  });

  // По контракту id — шаблон EntityId движка, только СТРОЧНАЯ латиница, первый знак — буква или
  // цифра. Подсказка поля называет это правило, чтобы автор не узнавал его только из отказа
  // сервера.
  test("подсказка поля называет правило сервера: строчная латиница", async () => {
    const { form } = await openConcept(draftOf());

    expect(form).toHaveTextContent(/строчн/i);
  });
});
