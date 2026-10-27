import { fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import type { QuickChoices } from "../../api/types";
import {
  OPTIONS,
  OUTDATED_WORDS,
  STORED_QUICK,
  holdRequests,
  previewHero,
} from "./fixtures";
import { Quick } from "./Quick";

const CREATE = "POST /api/games/g1/characters";

type Route = FakeReply | ((call: RecordedCall) => FakeReply);

const created = (flavor_error: string | null = null): FakeReply => ({
  status: 201,
  json: {
    character: previewHero({ name: "Тень", class_id: "rogue" }),
    flavor_error,
  },
});

function renderQuick(route: Route = created(), initial?: QuickChoices) {
  signInAs();
  const server = fakeServer({ [CREATE]: route });
  const gated = holdRequests(server.fetch);
  const onBack = vi.fn();
  const onCreated = vi.fn();
  render(
    <ApiContext value={createApi(gated.fetch, sessionToken)}>
      <Quick
        gameId="g1"
        options={OPTIONS}
        initial={initial}
        onBack={onBack}
        onCreated={onCreated}
      />
    </ApiContext>,
  );
  return { server, gated, onBack, onCreated };
}

const carousel = (label: "Класс" | "Раса") =>
  screen.getByRole("group", { name: label });

/** Выбор в карусели: точкой к варианту, затем «Выбрать» на его карточке. */
function choose(label: "Класс" | "Раса", name: string) {
  fireEvent.click(within(carousel(label)).getByRole("button", { name }));
  fireEvent.click(
    within(carousel(label)).getByRole("button", { name: /^Выбра(ть|но)$/ }),
  );
}

/** Показанная карточка карусели и состояние её кнопки выбора. */
const shown = (label: "Класс" | "Раса") =>
  within(carousel(label)).getByRole("article");
const chosenIn = (label: "Класс" | "Раса") =>
  within(carousel(label)).queryByRole("button", { name: "Выбрано" });

const typeInto = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const submitButton = () =>
  screen.getByRole("button", { name: "Создать героя" });

function fillForm(concept = "Сбежавший из цирка акробат") {
  choose("Класс", "Плут");
  choose("Раса", "Человек");
  typeInto("Опиши героя одной строкой", concept);
}

const posts = (server: { calls: RecordedCall[] }) =>
  server.calls.filter((call) => call.method === "POST");

describe("форма быстрого героя", () => {
  test("класс и раса — карусели карточек из правил сервера, по-русски", () => {
    renderQuick();

    for (const label of ["Класс", "Раса"] as const) {
      expect(carousel(label)).toHaveAttribute(
        "aria-roledescription",
        "карусель",
      );
    }
    for (const name of ["Воин", "Плут", "Волшебник", "Следопыт"]) {
      expect(
        within(carousel("Класс")).getByRole("button", { name }),
      ).toBeInTheDocument();
    }
    expect(within(carousel("Класс")).getByText("1 из 4")).toBeInTheDocument();
    expect(shown("Класс")).toHaveAccessibleName("Воин");
    expect(shown("Класс")).toHaveTextContent(/Кость хитов\s*к10/);
    expect(shown("Раса")).toHaveAccessibleName("Человек");
    expect(shown("Раса")).toHaveTextContent(/Скорость\s*9 м/);
    expect(
      within(carousel("Раса")).getByRole("button", { name: "Эльф" }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Опиши героя одной строкой"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Имя (необязательно)")).toBeInTheDocument();
  });

  test("над каруселями видны подписи «Класс» и «Раса»: на экране их две", () => {
    renderQuick();

    for (const label of ["Класс", "Раса"] as const) {
      const caption = screen.getByText(label, { selector: "p" });
      expect(caption.nextElementSibling).toBe(carousel(label));
    }
  });

  test("«Создать героя» неактивна, пока нет класса, расы и строки о герое", () => {
    renderQuick();
    expect(submitButton()).toBeDisabled();

    choose("Класс", "Плут");
    expect(submitButton()).toBeDisabled();
    choose("Раса", "Человек");
    expect(submitButton()).toBeDisabled();
    typeInto("Опиши героя одной строкой", "   ");
    expect(submitButton()).toBeDisabled();
    typeInto("Опиши героя одной строкой", "Акробат");
    expect(submitButton()).toBeEnabled();
  });

  test("выбор в карусели отмечает показанную карточку «Выбрано»", () => {
    renderQuick();
    expect(chosenIn("Раса")).toBeNull();

    choose("Раса", "Дварф");

    expect(shown("Раса")).toHaveAccessibleName("Дварф");
    expect(chosenIn("Раса")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(
      within(carousel("Раса")).getByRole("button", { name: "Следующая" }),
    );
    expect(chosenIn("Раса")).toBeNull();
  });

  test("в форме «Раса», а не «Вид»: устаревших слов и футов нет ни на одной карточке", () => {
    renderQuick();

    for (let i = 0; i < 4; i++) {
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
      fireEvent.click(
        within(carousel("Раса")).getByRole("button", { name: "Следующая" }),
      );
      fireEvent.click(
        within(carousel("Класс")).getByRole("button", { name: "Следующая" }),
      );
    }
    expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
  });

  test("«Назад» возвращает к выбору способа", () => {
    const { onBack } = renderQuick();

    fireEvent.click(screen.getByRole("button", { name: "Назад" }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });
});

describe("отправка формы клавишей Enter", () => {
  test("форма не готова — запроса нет", () => {
    const { server } = renderQuick();
    choose("Класс", "Плут");

    fireEvent.submit(submitButton().closest("form") as HTMLFormElement);

    expect(posts(server)).toHaveLength(0);
  });

  test("форма готова — запрос уходит, а повторная отправка, пока он идёт, второго не шлёт", async () => {
    const { gated, server, onCreated } = renderQuick();
    const release = gated.hold("/api/games/g1/characters");
    fillForm();
    const form = submitButton().closest("form") as HTMLFormElement;

    fireEvent.submit(form);
    await screen.findByRole("status");
    fireEvent.submit(form);
    release();

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)).toHaveLength(1);
  });
});

describe("отправка", () => {
  test("уходит mode quick с классом, расой и замыслом; пустое имя не шлётся", async () => {
    const { server, onCreated } = renderQuick();
    fillForm("  Сбежавший из цирка акробат ");

    fireEvent.click(submitButton());

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)).toHaveLength(1);
    expect(posts(server)[0].path).toBe("/api/games/g1/characters");
    expect(posts(server)[0].headers.get("Authorization")).toBe("Bearer tok-u1");
    expect(posts(server)[0].body).toEqual({
      mode: "quick",
      class_id: "rogue",
      species: "human",
      concept: "Сбежавший из цирка акробат",
    });
  });

  test("имя, если игрок его дал, уходит вместе с замыслом", async () => {
    const { server, onCreated } = renderQuick();
    fillForm();
    typeInto("Имя (необязательно)", " Тень ");

    fireEvent.click(submitButton());

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(posts(server)[0].body).toMatchObject({ name: "Тень" });
  });

  test("пока мастер придумывает героя, кнопки заблокированы и второй запрос не уходит", async () => {
    const { gated, server, onCreated } = renderQuick();
    const release = gated.hold("/api/games/g1/characters");
    fillForm();

    fireEvent.click(submitButton());

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Мастер придумывает героя",
    );
    expect(submitButton()).toBeDisabled();
    expect(screen.getByRole("button", { name: "Назад" })).toBeDisabled();
    fireEvent.click(submitButton());
    expect(
      gated.started.filter((line) => line.startsWith("POST")),
    ).toHaveLength(1);

    release();
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)).toHaveLength(1);
  });

  test("герой создан без описания: показан текст сервера, в лобби — по кнопке", async () => {
    const { onCreated } = renderQuick(
      created(
        "Нейронка не ответила. Персонаж сохранён: описание можно написать самому.",
      ),
    );
    fillForm();

    fireEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Персонаж сохранён: описание можно написать самому.",
    );
    expect(screen.getByText("Тень")).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "В лобби" }));
    expect(onCreated).toHaveBeenCalledTimes(1);
  });
});

describe("отказы сервера", () => {
  test("422: подробности отказа правил показаны списком, введённое осталось", async () => {
    const { onCreated } = renderQuick({
      status: 422,
      json: {
        detail: "Персонаж не прошёл проверку правил.",
        problems: ["неизвестный класс: 'bard'", "неизвестная раса: 'orcish'"],
      },
    });
    fillForm();

    fireEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Персонаж не прошёл проверку правил.");
    const items = within(alert).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "неизвестный класс: 'bard'",
      "неизвестная раса: 'orcish'",
    ]);
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue(
      "Сбежавший из цирка акробат",
    );
    expect(submitButton()).toBeEnabled();
  });

  test("429 показан текстом сервера, повторить можно", async () => {
    const { onCreated } = renderQuick({
      status: 429,
      json: { detail: "Нейронка ещё отвечает на ваш прошлый запрос." },
    });
    fillForm();

    fireEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нейронка ещё отвечает на ваш прошлый запрос.",
    );
    expect(onCreated).not.toHaveBeenCalled();
    expect(submitButton()).toBeEnabled();
  });

  test("нет связи: понятный текст и возможность повторить", async () => {
    signInAs();
    const failing = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    render(
      <ApiContext value={createApi(failing, sessionToken)}>
        <Quick
          gameId="g1"
          options={OPTIONS}
          onBack={() => {}}
          onCreated={() => {}}
        />
      </ApiContext>,
    );
    fillForm();

    fireEvent.click(submitButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("Нет связи");
    expect(submitButton()).toBeEnabled();
  });
});

describe("форма с прежним выбором («Изменить героя»)", () => {
  test("поля заполнены прежним выбором", () => {
    renderQuick(created(), { ...STORED_QUICK, name: "Тень" });

    // Карусели открыты на прежнем выборе, и он отмечен.
    expect(shown("Класс")).toHaveAccessibleName("Плут");
    expect(chosenIn("Класс")).toHaveAttribute("aria-pressed", "true");
    expect(shown("Раса")).toHaveAccessibleName("Человек");
    expect(chosenIn("Раса")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue(
      "Вор-неудачник",
    );
    expect(screen.getByLabelText("Имя (необязательно)")).toHaveValue("Тень");
    expect(submitButton()).toBeEnabled();
  });

  test("без имени в выборе поле имени пустое", () => {
    renderQuick(created(), STORED_QUICK);

    expect(screen.getByLabelText("Имя (необязательно)")).toHaveValue("");
  });

  test("отправка без правок шлёт тот же выбор", async () => {
    const { server, onCreated } = renderQuick(created(), {
      ...STORED_QUICK,
      name: "Тень",
    });

    fireEvent.click(submitButton());

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)[0].body).toEqual({
      mode: "quick",
      class_id: "rogue",
      species: "human",
      concept: "Вор-неудачник",
      name: "Тень",
    });
  });

  test("поля можно править: уходит исправленное", async () => {
    const { server, onCreated } = renderQuick(created(), STORED_QUICK);

    choose("Класс", "Воин");
    typeInto("Опиши героя одной строкой", "Бывший стражник");
    typeInto("Имя (необязательно)", "  Борис ");
    fireEvent.click(submitButton());

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)[0].body).toEqual({
      mode: "quick",
      class_id: "fighter",
      species: "human",
      concept: "Бывший стражник",
      name: "Борис",
    });
  });

  test("без прежнего выбора форма пустая, как раньше", () => {
    renderQuick();

    expect(chosenIn("Класс")).toBeNull();
    expect(chosenIn("Раса")).toBeNull();
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue("");
    expect(submitButton()).toBeDisabled();
  });
});

// Финальное покрытие блока 7: карусели внутри формы быстрого героя.
describe("карусели в форме", () => {
  test("«Изменить героя»: листание без «Выбрать» выбор не меняет — уходит прежний", async () => {
    const { server, onCreated } = renderQuick(created(), STORED_QUICK);

    fireEvent.click(
      within(carousel("Класс")).getByRole("button", { name: "Волшебник" }),
    );
    fireEvent.click(
      within(carousel("Раса")).getByRole("button", { name: "Следующая" }),
    );
    expect(shown("Класс")).toHaveAccessibleName("Волшебник");
    expect(chosenIn("Класс")).toBeNull();
    fireEvent.click(submitButton());

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)[0].body).toMatchObject({
      class_id: "rogue",
      species: "human",
    });
  });

  test("стрелки, точки, клавиши и «Выбрать» в готовой форме её не отправляют", async () => {
    const { server, onCreated } = renderQuick();
    fillForm();
    expect(submitButton()).toBeEnabled();
    const classes = carousel("Класс");

    fireEvent.click(within(classes).getByRole("button", { name: "Следующая" }));
    fireEvent.click(within(classes).getByRole("button", { name: "Воин" }));
    fireEvent.keyDown(classes, { key: "ArrowRight" });
    fireEvent.keyDown(classes, { key: "ArrowLeft" });
    fireEvent.click(within(classes).getByRole("button", { name: "Выбрать" }));

    expect(posts(server)).toHaveLength(0);
    expect(chosenIn("Класс")).toHaveAttribute("aria-pressed", "true");
    expect(shown("Класс")).toHaveAccessibleName("Воин");
    fireEvent.click(submitButton());
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(posts(server)).toHaveLength(1);
    expect(posts(server)[0].body).toMatchObject({ class_id: "fighter" });
  });

  test("карусели независимы: клавиши и свайп листают только свою", () => {
    renderQuick();

    fireEvent.keyDown(carousel("Класс"), { key: "ArrowRight" });
    expect(shown("Класс")).toHaveAccessibleName("Плут");
    expect(shown("Раса")).toHaveAccessibleName("Человек");

    const races = carousel("Раса");
    // Свайп основным указателем — левой кнопкой мыши (в jsdom isPrimary по умолчанию false).
    const mouse = { pointerId: 1, isPrimary: true, pointerType: "mouse" };
    fireEvent.pointerDown(races, { ...mouse, clientX: 300, clientY: 100 });
    fireEvent.pointerUp(races, { ...mouse, clientX: 100, clientY: 100 });
    expect(shown("Раса")).toHaveAccessibleName("Эльф");
    expect(shown("Класс")).toHaveAccessibleName("Плут");
  });

  test("выбор класса не трогает выбор расы и наоборот", () => {
    renderQuick();

    choose("Раса", "Полурослик");
    choose("Класс", "Следопыт");
    choose("Класс", "Воин");

    expect(shown("Раса")).toHaveAccessibleName("Полурослик");
    expect(chosenIn("Раса")).toHaveAttribute("aria-pressed", "true");
    expect(shown("Класс")).toHaveAccessibleName("Воин");
    expect(chosenIn("Класс")).toHaveAttribute("aria-pressed", "true");
  });
});
