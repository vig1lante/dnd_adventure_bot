import { ApiError, createApi } from "./client";
import { fakeServer } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import { draftOf, jobOf, summaryOf } from "../screens/authoring/fixtures";

// Методы конструктора — часть общего клиента: тот же токен сессии, те же ошибки по-русски.
function apiWith(
  routes: Record<string, FakeReply | ((call: RecordedCall) => FakeReply)>,
) {
  const server = fakeServer(routes);
  return { api: createApi(server.fetch, () => "tok"), server };
}

describe("клиент конструктора: пути, методы и тела по контракту", () => {
  test("список черновиков — GET /api/drafts с токеном сессии", async () => {
    const drafts = [summaryOf({ id: "d2" }), summaryOf({ id: "d1" })];
    const { api, server } = apiWith({ "GET /api/drafts": { json: drafts } });

    await expect(api.listDrafts()).resolves.toEqual(drafts);
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("новый черновик — POST /api/drafts с пустым телом, ответ 201", async () => {
    const draft = draftOf({ id: "d9" });
    const { api, server } = apiWith({
      "POST /api/drafts": { status: 201, json: draft },
    });

    await expect(api.createDraft()).resolves.toEqual(draft);
    expect(server.calls[0].body).toEqual({});
  });

  test("черновик по id — GET /api/drafts/{id}, id кодируется", async () => {
    const { api, server } = apiWith({
      "GET /api/drafts/a%2Fb": { json: draftOf() },
    });

    await api.getDraft("a/b");
    expect(server.calls[0].path).toBe("/api/drafts/a%2Fb");
  });

  test("раздел — PUT .../sections/{section} с версией и данными", async () => {
    const { api, server } = apiWith({
      "PUT /api/drafts/d1/sections/deities": { json: draftOf({ version: 4 }) },
    });

    const saved = await api.saveSection("d1", 3, "deities", []);

    expect(saved.version).toBe(4);
    expect(server.calls[0]).toMatchObject({
      method: "PUT",
      body: { version: 3, data: [] },
    });
  });

  test("закрепления — PUT .../locks с версией и полным списком", async () => {
    const { api, server } = apiWith({
      "PUT /api/drafts/d1/locks": { json: draftOf() },
    });

    await api.setLocks("d1", 5, ["deity:tiamat", "step:world"]);
    expect(server.calls[0].body).toEqual({
      version: 5,
      locks: ["deity:tiamat", "step:world"],
    });
  });

  test("пожелание шага — PUT .../wishes/{step} с версией и текстом", async () => {
    const { api, server } = apiWith({
      "PUT /api/drafts/d1/wishes/pantheon": { json: draftOf() },
    });

    await api.setWish("d1", 2, "pantheon", "Боги моря");
    expect(server.calls[0].body).toEqual({ version: 2, text: "Боги моря" });
  });

  test("генерация — POST .../generate, ответ 202 с заданием", async () => {
    const job = jobOf({ id: "j1", step: "pantheon" });
    const { api, server } = apiWith({
      "POST /api/drafts/d1/generate": { status: 202, json: job },
    });

    await expect(api.generate("d1", { step: "pantheon" })).resolves.toEqual(
      job,
    );
    expect(server.calls[0].body).toEqual({ step: "pantheon" });
  });

  test("задание — GET .../jobs/{job_id}", async () => {
    const job = jobOf({ id: "j1", status: "done" });
    const { api } = apiWith({ "GET /api/drafts/d1/jobs/j1": { json: job } });

    await expect(api.getJob("d1", "j1")).resolves.toEqual(job);
  });
});

describe("ошибки конструктора несут код из ответа", () => {
  test("409 version_conflict: код, статус и текст сервера", async () => {
    const { api } = apiWith({
      "PUT /api/drafts/d1/locks": {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });

    const error = await api.setLocks("d1", 1, []).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "version_conflict",
      detail: "Черновик изменился.",
    });
  });

  test("422 invalid_data: код и подробности", async () => {
    const { api } = apiWith({
      "PUT /api/drafts/d1/sections/world": {
        status: 422,
        json: {
          detail: "Раздел не прошёл проверку.",
          problems: ["regions.0.id: неверный id"],
          code: "invalid_data",
        },
      },
    });

    const error = await api
      .saveSection("d1", 1, "world", {})
      .catch((e: unknown) => e);

    expect(error).toMatchObject({
      code: "invalid_data",
      problems: ["regions.0.id: неверный id"],
    });
  });

  test("ответ без кода — кода нет (ошибки игр DND-1 не меняются)", async () => {
    const { api } = apiWith({
      "GET /api/drafts": { status: 403, json: { detail: "Нет доступа." } },
    });

    const error = await api.listDrafts().catch((e: unknown) => e);

    expect(error).toMatchObject({ status: 403, code: undefined });
  });
});

// --- финальное покрытие блока 4 ---

describe("ошибки конструктора: что видит автор", () => {
  test("detail не по-русски (FastAPI, прокси) не показывается: текст по статусу, код остаётся", async () => {
    const { api } = apiWith({
      "PUT /api/drafts/d1/sections/plot": {
        status: 422,
        json: {
          detail: "Unprocessable Entity",
          code: "invalid_data",
          problems: ["plot.acts: List should have at least 1 item"],
        },
      },
      "POST /api/drafts/d1/generate": {
        status: 409,
        json: { detail: "Conflict", code: "job_running" },
      },
    });

    const invalid = await api
      .saveSection("d1", 1, "plot", { acts: [], endings: [], villain: null })
      .catch((e: unknown) => e);
    const busy = await api
      .generate("d1", { step: "plot" })
      .catch((e: unknown) => e);

    expect(invalid).toMatchObject({
      status: 422,
      detail: "Некорректные данные запроса.",
      code: "invalid_data",
      problems: ["plot.acts: List should have at least 1 item"],
    });
    expect(busy).toMatchObject({
      status: 409,
      detail: "Сейчас это действие недоступно.",
      code: "job_running",
    });
  });

  test("problems читаются только у 422: у 409 их нет", async () => {
    const { api } = apiWith({
      "PUT /api/drafts/d1/wishes/plot": {
        status: 409,
        json: {
          detail: "Черновик изменился: обновите его и повторите правку.",
          code: "version_conflict",
          problems: ["лишнее"],
        },
      },
    });

    const error = await api
      .setWish("d1", 1, "plot", "Злодей — бог")
      .catch((e: unknown) => e);

    expect(error).toMatchObject({ status: 409, code: "version_conflict" });
    expect((error as ApiError).problems).toBeUndefined();
  });

  test("обрыв связи при постановке задания: статус 0 и текст про связь", async () => {
    const { api } = apiWith({
      "POST /api/drafts/d1/generate": () => {
        throw new TypeError("Failed to fetch");
      },
    });

    const error = await api
      .generate("d1", { step: "concept" })
      .catch((e: unknown) => e);

    expect(error).toMatchObject({
      status: 0,
      detail: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    });
  });

  test("id задания кодируется в пути опроса", async () => {
    const { api, server } = apiWith({
      "GET /api/drafts/d1/jobs/j%2F1": { json: jobOf({ id: "j/1" }) },
    });

    await api.getJob("d1", "j/1");
    expect(server.calls[0].path).toBe("/api/drafts/d1/jobs/j%2F1");
  });
});
