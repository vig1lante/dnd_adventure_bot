import { render, screen, within } from "@testing-library/react";
import App from "../App";
import type { AppDeps } from "../App";
import { fakeServer } from "../test-setup";
import type { FakeReply } from "../test-setup";

const HINT = "Откройте приложение из Telegram";

const config = (dev_login: boolean): FakeReply => ({
  json: { env: dev_login ? "dev" : "prod", dev_login, bot_username: null },
});

function renderApp(
  server: { fetch: typeof fetch },
  deps: Partial<AppDeps> = {},
) {
  return render(
    <App
      deps={{
        fetchImpl: server.fetch,
        readInitData: () => null,
        readStartParam: () => null,
        setupTelegram: () => {},
        ...deps,
      }}
    />,
  );
}

// Логика входа проверена в App.test.tsx; здесь — что экраны оформлены элементами
// src/ui, а не голыми тегами.
describe("экраны входа в новом стиле", () => {
  test("шапка: иконка d20 рядом с названием", async () => {
    const { container } = renderApp(
      fakeServer({ "GET /api/config": config(false) }),
    );
    await screen.findByText(HINT);

    const header = container.querySelector(".app-header");
    expect(header?.querySelector("svg")).not.toBeNull();
    expect(
      within(header as HTMLElement).getByRole("heading", { level: 1 }),
    ).toHaveTextContent("DnD Adventure Bot");
  });

  test("подсказка «Откройте приложение из Telegram» — пустое состояние со свечой", async () => {
    const { container } = renderApp(
      fakeServer({ "GET /api/config": config(false) }),
    );

    const hint = await screen.findByText(HINT);

    expect(hint.closest(".ui-empty")).not.toBeNull();
    expect(container.querySelector(".ui-empty svg")).not.toBeNull();
    expect(screen.getAllByRole("heading")).toHaveLength(1); // только название приложения
  });

  test("загрузка: статус «Входим…»", () => {
    const pending = vi.fn(() => new Promise<Response>(() => {}));

    renderApp({ fetch: pending as unknown as typeof fetch });

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Входим…");
    expect(status).toHaveClass("ui-loading");
  });

  test("выбор игрока: карточка и шесть крупных кнопок", async () => {
    renderApp(fakeServer({ "GET /api/config": config(true) }));

    const card = await screen.findByRole("region", { name: "Выберите игрока" });

    expect(card).toHaveClass("ui-card");
    const buttons = within(card).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual([
      "dev-1",
      "dev-2",
      "dev-3",
      "dev-4",
      "dev-5",
      "dev-6",
    ]);
    for (const button of buttons) expect(button).toHaveClass("ui-button");
  });

  test("ошибка: оповещение и «Повторить» в стиле приложения", async () => {
    const offline = vi.fn(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    );

    renderApp({ fetch: offline as unknown as typeof fetch });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveClass("ui-alert");
    expect(screen.getByRole("button", { name: "Повторить" })).toHaveClass(
      "ui-button",
    );
  });
});
