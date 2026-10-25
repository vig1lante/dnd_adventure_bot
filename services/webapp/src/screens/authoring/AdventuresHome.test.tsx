import { act, fireEvent, render, screen } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import type { DraftSummary } from "../../api/types";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import { AdventuresHome } from "./AdventuresHome";
import { callsTo, draftOf, jobOf, summaryOf } from "./fixtures";
import type { Routes } from "./fixtures";

function renderHome(drafts: DraftSummary[] = [], routes: Routes = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: true });
  const server = fakeServer({ "GET /api/drafts": { json: drafts }, ...routes });
  render(
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <AdventuresHome />
    </ApiContext>,
  );
  return server;
}

const editorOpened = () =>
  screen.findByRole("navigation", { name: "Шаги приключения" });

describe("список черновиков", () => {
  test("пока список идёт — загрузка; потом черновики в порядке сервера", async () => {
    renderHome([
      summaryOf({ id: "d2", title: "Новая гавань" }),
      summaryOf({ id: "d1", title: "Старый склеп" }),
    ]);

    expect(screen.getByRole("status")).toHaveTextContent("Загружаем");
    await screen.findByText("Новая гавань");
    const items = screen
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(items[0]).toContain("Новая гавань");
    expect(items[1]).toContain("Старый склеп");
  });

  test("черновик без названия подписан «Без названия», slug показан, если есть", async () => {
    renderHome([
      summaryOf({ id: "d1", title: "", adventure_id: null }),
      summaryOf({ id: "d2", title: "Гавань", adventure_id: "foggy-harbor" }),
    ]);

    expect(await screen.findByText("Без названия")).toBeInTheDocument();
    expect(screen.getByText("foggy-harbor")).toBeInTheDocument();
  });

  test("черновиков нет — пустое состояние", async () => {
    renderHome([]);

    expect(await screen.findByText("Пока нет приключений")).toBeInTheDocument();
  });

  test("сбой загрузки: текст сервера и «Повторить»", async () => {
    let attempts = 0;
    const server = renderHome([], {
      "GET /api/drafts": () =>
        ++attempts === 1
          ? { status: 500, json: { detail: "База недоступна." } }
          : { json: [summaryOf({ title: "Гавань" })] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "База недоступна.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("Гавань")).toBeInTheDocument();
    expect(callsTo(server, "GET", "/api/drafts")).toHaveLength(2);
  });
});

describe("новый и существующий черновик", () => {
  test("«Новое приключение» создаёт черновик и открывает его редактор", async () => {
    const server = renderHome([], {
      "POST /api/drafts": { status: 201, json: draftOf({ id: "d9" }) },
      "GET /api/drafts/d9": { json: draftOf({ id: "d9" }) },
    });
    await screen.findByText("Пока нет приключений");

    fireEvent.click(screen.getByRole("button", { name: "Новое приключение" }));

    expect(await editorOpened()).toBeInTheDocument();
    expect(callsTo(server, "POST", "/api/drafts")).toHaveLength(1);
    expect(callsTo(server, "GET", "/api/drafts/d9")).toHaveLength(1);
  });

  test("отказ создания показан текстом сервера, список остаётся", async () => {
    renderHome([], {
      "POST /api/drafts": {
        status: 403,
        json: { detail: "Создавать приключения могут только создатели игр." },
      },
    });
    await screen.findByText("Пока нет приключений");

    fireEvent.click(screen.getByRole("button", { name: "Новое приключение" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "только создатели игр",
    );
    expect(screen.getByText("Пока нет приключений")).toBeInTheDocument();
  });

  test("«Открыть» ведёт в редактор; «К списку приключений» возвращает и обновляет список", async () => {
    const server = renderHome([summaryOf({ id: "d1", title: "Гавань" })], {
      "GET /api/drafts/d1": { json: draftOf({ id: "d1", title: "Гавань" }) },
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "Открыть: Гавань" }),
    );
    await editorOpened();
    fireEvent.click(
      screen.getByRole("button", { name: "К списку приключений" }),
    );

    expect(
      await screen.findByRole("button", { name: "Открыть: Гавань" }),
    ).toBeInTheDocument();
    expect(callsTo(server, "GET", "/api/drafts")).toHaveLength(2);
  });
});

// --- финальное покрытие блока 4 ---

describe("уход из редактора", () => {
  // Подменяются только таймеры: опрос задания идёт через setInterval.
  const tick = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  test("«К списку приключений» во время генерации останавливает опрос задания", async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    const running = jobOf({ id: "j1", step: "world", status: "running" });
    const server = renderHome([summaryOf({ id: "d1", title: "Гавань" })], {
      "GET /api/drafts/d1": {
        json: draftOf({ id: "d1", title: "Гавань", job: running }),
      },
      "GET /api/drafts/d1/jobs/j1": { json: running },
    });
    const polls = () => callsTo(server, "GET", "/api/drafts/d1/jobs/j1");
    await tick(0);
    fireEvent.click(screen.getByRole("button", { name: "Открыть: Гавань" }));
    await tick(0);
    expect(screen.getByText("Генерирую…")).toBeInTheDocument();
    await tick(2000);
    expect(polls()).toHaveLength(1);

    fireEvent.click(
      screen.getByRole("button", { name: "К списку приключений" }),
    );
    await tick(0);
    await tick(10_000);

    expect(polls()).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "Открыть: Гавань" }),
    ).toBeInTheDocument();
  });
});
