import { fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import type { FakeReply, RecordedCall } from "../../test-setup";
import type { QuickChoices } from "../../api/types";
import { OPTIONS, STORED_QUICK, holdRequests, previewHero } from "./fixtures";
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

const choose = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const typeInto = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const submitButton = () =>
  screen.getByRole("button", { name: "Создать героя" });

function fillForm(concept = "Сбежавший из цирка акробат") {
  choose("Класс", "rogue");
  choose("Вид", "human");
  typeInto("Опиши героя одной строкой", concept);
}

const posts = (server: { calls: RecordedCall[] }) =>
  server.calls.filter((call) => call.method === "POST");

describe("форма быстрого героя", () => {
  test("класс и вид выбираются из правил сервера, названия по-русски", () => {
    renderQuick();

    const classes = within(screen.getByLabelText("Класс"));
    expect(classes.getByRole("option", { name: "Воин" })).toBeInTheDocument();
    expect(classes.getByRole("option", { name: "Плут" })).toBeInTheDocument();
    expect(
      classes.getByRole("option", { name: "Волшебник" }),
    ).toBeInTheDocument();
    const species = within(screen.getByLabelText("Вид"));
    expect(species.getByRole("option", { name: "Эльф" })).toBeInTheDocument();
    expect(
      screen.getByLabelText("Опиши героя одной строкой"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Имя (необязательно)")).toBeInTheDocument();
  });

  test("«Создать героя» неактивна, пока нет класса, вида и строки о герое", () => {
    renderQuick();
    expect(submitButton()).toBeDisabled();

    choose("Класс", "rogue");
    expect(submitButton()).toBeDisabled();
    choose("Вид", "human");
    expect(submitButton()).toBeDisabled();
    typeInto("Опиши героя одной строкой", "   ");
    expect(submitButton()).toBeDisabled();
    typeInto("Опиши героя одной строкой", "Акробат");
    expect(submitButton()).toBeEnabled();
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
    choose("Класс", "rogue");

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
  test("уходит mode quick с классом, видом и замыслом; пустое имя не шлётся", async () => {
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
        problems: ["неизвестный класс: 'bard'", "неизвестный вид: 'orcish'"],
      },
    });
    fillForm();

    fireEvent.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Персонаж не прошёл проверку правил.");
    const items = within(alert).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "неизвестный класс: 'bard'",
      "неизвестный вид: 'orcish'",
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

    expect(screen.getByLabelText("Класс")).toHaveValue("rogue");
    expect(screen.getByLabelText("Вид")).toHaveValue("human");
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

    choose("Класс", "fighter");
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

    expect(screen.getByLabelText("Класс")).toHaveValue("");
    expect(screen.getByLabelText("Опиши героя одной строкой")).toHaveValue("");
    expect(submitButton()).toBeDisabled();
  });
});
