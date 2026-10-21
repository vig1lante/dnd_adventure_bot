import { act, screen } from "@testing-library/react";
import indexHtml from "../index.html?raw";
import { fakeServer } from "./test-setup";

// Разбирает index.html так, как его увидит браузер.
function parseIndexHtml(): Document {
  return new DOMParser().parseFromString(indexHtml, "text/html");
}

test("index.html: русский язык, название и точка входа", () => {
  const page = parseIndexHtml();

  expect(page.documentElement.lang).toBe("ru");
  expect(page.title).toBe("DnD Adventure Bot");
  expect(page.querySelector('script[type="module"]')?.getAttribute("src")).toBe(
    "/src/main.tsx",
  );
});

// Стык index.html → main.tsx → App: точка входа находит корневой элемент
// страницы и рисует в нём приложение. Сервер подменён заглушкой: приложение
// сразу идёт за конфигурацией, а вне Telegram и без dev-входа показывает подсказку.
test("main.tsx монтирует приложение в корневой элемент из index.html", async () => {
  const server = fakeServer({
    "GET /api/config": {
      json: { env: "prod", dev_login: false, bot_username: null },
    },
  });
  vi.stubGlobal("fetch", server.fetch);
  const root = parseIndexHtml().getElementById("root");
  expect(root).not.toBeNull();
  const container = document.importNode(root!, true);
  document.body.append(container);
  // Узел добавлен в обход render, поэтому авто-очистка Testing Library его не
  // уберёт: без этого приложение останется в документе для соседних тестов
  // (заметно при isolate: false).
  onTestFinished(() => container.remove());

  // act нужен, чтобы React успел отрисовать корень до проверок.
  await act(async () => {
    await import("./main");
  });

  expect(
    screen.getByRole("heading", { level: 1, name: "DnD Adventure Bot" }),
  ).toBeInTheDocument();
  // Приложение не только нарисовало заголовок, но и дошло до экрана входа.
  expect(
    await screen.findByText("Откройте приложение из Telegram"),
  ).toBeInTheDocument();
});
