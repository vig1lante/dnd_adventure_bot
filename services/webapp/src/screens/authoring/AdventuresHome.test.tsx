import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createApi } from "../../api/client";
import { ApiContext } from "../../api/context";
import type { AdventureSummary, DraftSummary } from "../../api/types";
import { sessionToken } from "../../store/game";
import { fakeServer, signInAs } from "../../test-setup";
import { AdventuresHome } from "./AdventuresHome";
import { callsTo, draftOf, jobOf, summaryOf } from "./fixtures";
import type { Routes } from "./fixtures";

function renderHome(drafts: DraftSummary[] = [], routes: Routes = {}) {
  signInAs({ id: "u1", name: "Вика", can_create_games: true });
  const server = fakeServer({
    "GET /api/drafts": { json: drafts },
    "GET /api/adventures": { json: [] },
    ...routes,
  });
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

    expect(screen.getByText("Загружаем черновики…")).toBeInTheDocument();
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

// --- блок 6: импорт архива и копия приключения каталога (дополнение B6 §7, §8) ---

const IMPORT = "/api/drafts/import";

/** Что клиент передал в fetch по адресу `path`: тело как есть (заглушка разбирает только JSON). */
const sentInit = (server: ReturnType<typeof renderHome>, path: string) => {
  const calls = vi.mocked(server.fetch).mock.calls;
  const found = calls.find(([url]) => String(url) === path);
  return found?.[1] as RequestInit;
};

const pickArchive = (file: File) =>
  fireEvent.change(screen.getByLabelText("Импорт архива (.zip)"), {
    target: { files: [file] },
  });

describe("импорт архива", () => {
  test("выбранный архив уходит сырым телом application/zip; новый черновик открывается", async () => {
    const imported = draftOf({ id: "d7", adventure_id: "foggy-harbor" });
    const server = renderHome([], {
      [`POST ${IMPORT}`]: { status: 201, json: imported },
      "GET /api/drafts/d7": { json: imported },
    });
    await screen.findByText("Пока нет приключений");
    const file = new File(["PK-архив"], "foggy-harbor.zip", {
      type: "application/zip",
    });

    pickArchive(file);

    expect(await editorOpened()).toBeInTheDocument();
    expect(sentInit(server, IMPORT).body).toBe(file);
    expect(callsTo(server, "POST", IMPORT)[0].headers.get("Content-Type")).toBe(
      "application/zip",
    );
  });

  test("импорт с ошибками показывает problems по-русски; список остаётся", async () => {
    renderHome([summaryOf({ title: "Гавань" })], {
      [`POST ${IMPORT}`]: {
        status: 422,
        json: {
          detail: "Архив не подходит.",
          code: "archive_invalid",
          problems: [
            "путь выходит за папку приключения",
            "в архиве ссылка на файл",
          ],
        },
      },
    });
    await screen.findByText("Гавань");

    pickArchive(new File(["PK"], "bad.zip"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Архив не подходит.");
    expect(
      within(alert)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["путь выходит за папку приключения", "в архиве ссылка на файл"]);
    expect(screen.getByText("Гавань")).toBeInTheDocument();
  });

  test("413 too_large — текст сервера; тот же файл можно выбрать снова", async () => {
    let attempt = 0;
    const server = renderHome([], {
      [`POST ${IMPORT}`]: () =>
        attempt++ === 0
          ? {
              status: 413,
              json: { detail: "Архив больше 2 МиБ.", code: "too_large" },
            }
          : {
              status: 422,
              json: { detail: "Архив не подходит.", code: "archive_invalid" },
            },
    });
    await screen.findByText("Пока нет приключений");
    const file = new File(["PK"], "big.zip");

    pickArchive(file);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Архив больше 2 МиБ.",
    );
    expect(screen.getByLabelText("Импорт архива (.zip)")).toHaveValue("");
    pickArchive(file);

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", IMPORT)).toHaveLength(2),
    );
  });
});

describe("готовые приключения: копия для правки", () => {
  const CATALOG: AdventureSummary[] = [
    {
      id: "demo-crypt",
      title: "Склеп забытого короля",
      pitch: "Старый склеп",
      target_hours: 3,
      score: 94,
      blocking: false,
    },
  ];

  test("список каталога; «Сделать копию» создаёт черновик (POST /api/adventures/{id}/draft) и открывает его", async () => {
    const copy = draftOf({ id: "d8", adventure_id: "demo-crypt" });
    const server = renderHome([], {
      "GET /api/adventures": { json: CATALOG },
      "POST /api/adventures/demo-crypt/draft": { status: 201, json: copy },
      "GET /api/drafts/d8": { json: copy },
    });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Сделать копию: Склеп забытого короля",
      }),
    );

    expect(await editorOpened()).toBeInTheDocument();
    expect(
      callsTo(server, "POST", "/api/adventures/demo-crypt/draft"),
    ).toHaveLength(1);
  });

  test("отказ копии — текст сервера", async () => {
    renderHome([], {
      "GET /api/adventures": { json: CATALOG },
      "POST /api/adventures/demo-crypt/draft": {
        status: 404,
        json: { detail: "Приключение не найдено." },
      },
    });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Сделать копию: Склеп забытого короля",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Приключение не найдено.",
    );
  });

  test("сбой загрузки каталога — текст и «Повторить», черновики на месте", async () => {
    let attempt = 0;
    renderHome([summaryOf({ title: "Гавань" })], {
      "GET /api/adventures": () =>
        attempt++ === 0
          ? { status: 500, json: { detail: "Каталог недоступен." } }
          : { json: CATALOG },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Каталог недоступен.",
    );
    expect(screen.getByText("Гавань")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(
      await screen.findByText("Склеп забытого короля"),
    ).toBeInTheDocument();
  });
});

// --- финальное покрытие блока 6: отказы импорта на экране и копия каталога ---

describe("импорт архива: что видит автор при отказе", () => {
  test.each([
    {
      what: "файл не zip — 422 archive_invalid с проблемой",
      reply: {
        status: 422,
        json: {
          detail: "Архив не подходит для импорта.",
          code: "archive_invalid",
          problems: ["архив: это не zip-архив или он повреждён"],
        },
      },
      texts: [
        "Архив не подходит для импорта.",
        "архив: это не zip-архив или он повреждён",
      ],
    },
    {
      what: "415 unsupported_media — текст сервера",
      reply: {
        status: 415,
        json: {
          detail: "Импорт принимает только zip-архив.",
          code: "unsupported_media",
        },
      },
      texts: ["Импорт принимает только zip-архив."],
    },
    {
      what: "502 прокси без JSON — общий русский текст",
      reply: { status: 502, text: "<html>Bad Gateway</html>" },
      texts: ["Сервер временно недоступен. Попробуйте ещё раз немного позже."],
    },
  ])("$what", async ({ reply, texts }) => {
    const server = renderHome([summaryOf({ title: "Гавань" })], {
      [`POST ${IMPORT}`]: reply,
    });
    await screen.findByText("Гавань");

    pickArchive(new File(["not a zip"], "notes.txt"));

    const alert = await screen.findByRole("alert");
    for (const text of texts) expect(alert).toHaveTextContent(text);
    expect(alert).not.toHaveTextContent(/Bad Gateway|html/);
    // Редактор не открылся, список на месте, поле снова принимает файл.
    expect(
      screen.queryByRole("navigation", { name: "Шаги приключения" }),
    ).toBeNull();
    expect(screen.getByText("Гавань")).toBeInTheDocument();
    expect(screen.getByLabelText("Импорт архива (.zip)")).toBeEnabled();
    expect(callsTo(server, "POST", IMPORT)).toHaveLength(1);
  });

  test("обрыв связи при импорте — текст про связь", async () => {
    renderHome([], {
      [`POST ${IMPORT}`]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    await screen.findByText("Пока нет приключений");

    pickArchive(new File(["PK"], "x.zip"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    );
  });

  test("поле выбора предлагает только zip-архивы", async () => {
    renderHome();
    await screen.findByText("Пока нет приключений");

    expect(screen.getByLabelText("Импорт архива (.zip)")).toHaveAttribute(
      "accept",
      ".zip,application/zip",
    );
    expect(
      screen.getByLabelText("Импорт архива (.zip)"),
    ).toHaveAccessibleDescription(
      "Архив из «Скачать архив» или папка приключения в zip, не больше 2 МиБ. Станет новым черновиком.",
    );
  });
});

describe("готовые приключения: копия открывается своим содержимым", () => {
  test("копия опубликованного приключения: редактор показывает её название и id, список — id каталога", async () => {
    const published: AdventureSummary = {
      id: "crow-lighthouse",
      title: "Маяк на Вороньем мысу",
      pitch: "Маяк погас",
      target_hours: 1.5,
      score: 100,
      blocking: false,
    };
    const copy = draftOf({
      id: "d9",
      title: "Маяк на Вороньем мысу",
      adventure_id: "crow-lighthouse",
    });
    const server = renderHome([], {
      "GET /api/adventures": { json: [published] },
      "POST /api/adventures/crow-lighthouse/draft": {
        status: 201,
        json: copy,
      },
      "GET /api/drafts/d9": { json: copy },
    });
    expect(await screen.findByText("crow-lighthouse")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Сделать копию: Маяк на Вороньем мысу",
      }),
    );

    await editorOpened();
    const concept = screen.getByRole("region", { name: "Замысел" });
    expect(
      within(concept).getByRole("textbox", { name: "Название" }),
    ).toHaveValue("Маяк на Вороньем мысу");
    expect(
      within(concept).getByRole("textbox", { name: "id приключения" }),
    ).toHaveValue("crow-lighthouse");
    // Копия — POST без тела (B6 §8), открыт новый черновик d9.
    const [call] = callsTo(
      server,
      "POST",
      "/api/adventures/crow-lighthouse/draft",
    );
    expect(call.body).toBeUndefined();
    expect(callsTo(server, "GET", "/api/drafts/d9")).toHaveLength(1);
  });
});

// --- B10.4: список черновиков и отказы импорта словами автора ---

/** Время в часовом поясе теста: строка списка показывает местное время. */
const localIso = (month: number, day: number, hours: number, minutes: number) =>
  new Date(2026, month - 1, day, hours, minutes).toISOString();

describe("список черновиков: дата правки и различимые копии", () => {
  test("у строки — дата и время последней правки; кнопка «Открыть» описана ими", async () => {
    renderHome([
      summaryOf({
        id: "d1",
        title: "Гавань",
        updated_at: localIso(10, 29, 7, 5),
      }),
    ]);

    expect(
      await screen.findByText("изменён 29 октября 2026, 07:05"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Открыть: Гавань" }),
    ).toHaveAccessibleDescription(/изменён 29 октября 2026, 07:05/);
  });

  test("две копии одного приключения различаются датой правки", async () => {
    renderHome([
      summaryOf({
        id: "d2",
        title: "Маяк на Вороньем мысу",
        adventure_id: "crow-lighthouse",
        updated_at: localIso(10, 28, 21, 40),
      }),
      summaryOf({
        id: "d1",
        title: "Маяк на Вороньем мысу",
        adventure_id: "crow-lighthouse",
        updated_at: localIso(10, 27, 9, 3),
      }),
    ]);

    await screen.findAllByText("Маяк на Вороньем мысу");
    const rows = within(
      screen.getByRole("list", { name: "Черновики" }),
    ).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("изменён 28 октября 2026, 21:40"),
      expect.stringContaining("изменён 27 октября 2026, 09:03"),
    ]);
    const opens = screen.getAllByRole("button", {
      name: "Открыть: Маяк на Вороньем мысу",
    });
    expect(opens[0]).toHaveAccessibleDescription(/28 октября 2026, 21:40/);
    expect(opens[1]).toHaveAccessibleDescription(/27 октября 2026, 09:03/);
  });

  test("сбой загрузки — единая ошибка «Не удалось загрузить черновики»", async () => {
    renderHome([], {
      "GET /api/drafts": { status: 500, json: { detail: "База недоступна." } },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Не удалось загрузить черновики");
    expect(alert).toHaveTextContent("База недоступна.");
    expect(document.querySelector(".ui-error")).not.toBeNull();
  });
});

/** Архив zip из центрального каталога с этими именами (данных записей читатель не смотрит). */
function zipFile(names: readonly string[]): File {
  const encoded = names.map((name) => new TextEncoder().encode(name));
  const size = encoded.reduce((sum, name) => sum + 46 + name.length, 0);
  const bytes = new Uint8Array(size + 22);
  const view = new DataView(bytes.buffer);
  let at = 0;
  for (const name of encoded) {
    view.setUint32(at, 0x02014b50, true);
    view.setUint16(at + 8, 0x800, true);
    view.setUint16(at + 28, name.length, true);
    bytes.set(name, at + 46);
    at += 46 + name.length;
  }
  view.setUint32(at, 0x06054b50, true);
  view.setUint16(at + 8, names.length, true);
  view.setUint16(at + 10, names.length, true);
  view.setUint32(at + 12, size, true);
  return new File([bytes], "crow.zip", { type: "application/zip" });
}

describe("отказ импорта: одна строка на причину, номер записи вместо пути файла", () => {
  test("18 одинаковых причин архива — одна строка", async () => {
    renderHome([], {
      [`POST ${IMPORT}`]: {
        status: 422,
        json: {
          detail: "Архив не подходит для импорта.",
          code: "archive_invalid",
          problems: Array.from(
            { length: 18 },
            (_, index) =>
              `архив, запись ${index + 1}: метод сжатия не поддерживается`,
          ),
        },
      },
    });
    await screen.findByText("Пока нет приключений");

    pickArchive(new File(["PK"], "bzip.zip"));

    const alert = await screen.findByRole("alert");
    expect(
      within(alert)
        .getAllByRole("listitem")
        .map((row) => row.textContent),
    ).toEqual(["архив, записи 1–18: метод сжатия не поддерживается"]);
  });

  test("путь файла из архива в отказе загрузчика — номер записи и вид сущности", async () => {
    renderHome([], {
      [`POST ${IMPORT}`]: {
        status: 422,
        json: {
          detail: "Архив не подходит для импорта.",
          code: "archive_invalid",
          problems: [
            "items/Тайна Мастера zzz.yaml: id «…» не совпадает с именем файла",
          ],
        },
      },
    });
    await screen.findByText("Пока нет приключений");

    pickArchive(
      zipFile([
        "crow/",
        "crow/adventure.yaml",
        "crow/items/",
        "crow/items/Тайна Мастера zzz.yaml",
      ]),
    );

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("listitem")).toHaveTextContent(
      /^архив, запись 4 \(предмет\): id «…» не совпадает с именем файла$/,
    );
    expect(alert).not.toHaveTextContent("Тайна Мастера");
  });
});
