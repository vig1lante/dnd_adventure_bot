import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { RollCard as GameRollCard } from "../components/RollCard";
import { fakeServer } from "../test-setup";
import { wantsStylePreview } from "./gate";
import { ROLL_SAMPLES } from "./rollSamples";
import StylePreview from "./StylePreview";

const PREVIEW_TITLE = /ламповые посиделки/i;

afterEach(() => {
  vi.unstubAllEnvs();
  document.documentElement.removeAttribute("data-theme");
});

describe("wantsStylePreview", () => {
  test.each([
    ["?preview=style", true],
    ["?dev=dev-1&preview=style", true],
    ["", false],
    ["?preview=other", false],
    ["?preview", false],
    ["?style=preview", false],
  ])("%j → %s", (search, expected) => {
    expect(wantsStylePreview(search)).toBe(expected);
  });
});

describe("страница превью", () => {
  test("заголовок и разделы со всеми элементами", () => {
    render(<StylePreview />);

    expect(
      screen.getByRole("heading", { level: 1, name: PREVIEW_TITLE }),
    ).toBeInTheDocument();
    for (const name of [
      "Цвета",
      "Шрифты",
      "Кнопки",
      "Плашки",
      "Карточка и панель",
      "Рассказ мастера",
      "Отсчёт",
      "Бросок",
      "Только тебе",
      "Здоровье",
      "Герои",
      "Пусто, загрузка, ошибка",
      "Иконки",
    ]) {
      expect(
        screen.getByRole("heading", { level: 2, name }),
        name,
      ).toBeInTheDocument();
    }
  });

  test("показаны готовые элементы: кнопки трёх видов, отсчёт, бросок, письмо, здоровье, жетоны", () => {
    const { container } = render(<StylePreview />);

    for (const name of ["Основная", "Второстепенная", "Опасная"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("timer").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByRole("article").length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByRole("note", { name: "Только тебе" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("meter").length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByRole("img").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // Все иконки набора на странице.
    expect(container.querySelectorAll(".preview-icons svg")).toHaveLength(7);
  });

  test("карточки броска — те же, что рисует игра: разметка совпадает до знака", () => {
    render(<StylePreview />);
    const section = screen
      .getByRole("heading", { level: 2, name: "Бросок" })
      .closest("section") as HTMLElement;

    const cards = within(section).getAllByRole("article");

    expect(cards).toHaveLength(ROLL_SAMPLES.length);
    ROLL_SAMPLES.forEach((item, index) => {
      const game = render(<GameRollCard item={item} />);
      expect(cards[index].outerHTML, item.text).toBe(game.container.innerHTML);
      game.unmount();
    });
  });

  test("без запросов к серверу", () => {
    const server = fakeServer({});
    vi.stubGlobal("fetch", server.fetch);

    render(<StylePreview />);

    expect(server.calls).toHaveLength(0);
  });
});

describe("переключатель темы", () => {
  test("по умолчанию — текущая палитра страницы (тёмная)", () => {
    render(<StylePreview />);

    expect(screen.getByRole("button", { name: "Тёмная" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("«Светлая» и «Тёмная» переключают палитру всей страницы", () => {
    render(<StylePreview />);

    fireEvent.click(screen.getByRole("button", { name: "Светлая" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("button", { name: "Светлая" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Тёмная" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    fireEvent.click(screen.getByRole("button", { name: "Тёмная" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("?theme=light открывает превью в светлой теме", () => {
    window.history.replaceState(null, "", "/?preview=style&theme=light");

    render(<StylePreview />);

    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("button", { name: "Светлая" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  test("«Обе рядом»: обе палитры одновременно", () => {
    const { container } = render(<StylePreview />);

    fireEvent.click(screen.getByRole("button", { name: "Обе рядом" }));

    const scopes = container.querySelectorAll<HTMLElement>(".preview-scope");
    expect([...scopes].map((scope) => scope.dataset.theme)).toEqual([
      "dark",
      "light",
    ]);
    for (const scope of scopes) {
      expect(
        within(scope).getByRole("button", { name: "Основная" }),
      ).toBeInTheDocument();
    }
  });
});

describe("превью только в dev-сборке", () => {
  async function openApp(search: string) {
    window.history.replaceState(null, "", `/${search}`);
    const root = document.createElement("div");
    root.id = "root";
    document.body.append(root);
    // Контейнер добавлен в обход render: авто-очистка Testing Library его не уберёт.
    onTestFinished(() => root.remove());
    vi.resetModules();
    await act(async () => {
      await import("../main");
    });
  }

  const server = () =>
    fakeServer({
      "GET /api/config": {
        json: { env: "prod", dev_login: false, bot_username: null },
      },
    });

  test("dev и ?preview=style: открывается превью, сервер не нужен", async () => {
    const stub = server();
    vi.stubGlobal("fetch", stub.fetch);

    await openApp("?preview=style");

    expect(
      await screen.findByRole("heading", { level: 1, name: PREVIEW_TITLE }),
    ).toBeInTheDocument();
    expect(stub.calls).toHaveLength(0);
  });

  test("dev без параметра: обычное приложение", async () => {
    const stub = server();
    vi.stubGlobal("fetch", stub.fetch);

    await openApp("");

    expect(
      await screen.findByText("Откройте приложение из Telegram"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: PREVIEW_TITLE })).toBeNull();
  });

  test("не dev (прод-сборка) с ?preview=style: превью нет, приложение работает", async () => {
    vi.stubEnv("DEV", false);
    const stub = server();
    vi.stubGlobal("fetch", stub.fetch);

    await openApp("?preview=style");

    expect(
      await screen.findByText("Откройте приложение из Telegram"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: PREVIEW_TITLE })).toBeNull();
    expect(stub.calls.map((call) => call.path)).toContain("/api/config");
  });
});
