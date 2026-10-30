import { ApiError, createApi } from "./client";
import { fakeServer } from "../test-setup";
import type { FakeReply, RecordedCall } from "../test-setup";
import {
  draftOf,
  findingOf,
  jobOf,
  locationOf,
  reportOf,
  srdMonsterOf,
  summaryOf,
} from "../screens/authoring/fixtures";

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

  test("удаление черновика — DELETE /api/drafts/{id} без тела, id кодируется, ответ 204", async () => {
    const { api, server } = apiWith({
      "DELETE /api/drafts/a%2Fb": { status: 204 },
    });

    await expect(api.deleteDraft("a/b")).resolves.toBeUndefined();
    expect(server.calls[0]).toMatchObject({
      method: "DELETE",
      path: "/api/drafts/a%2Fb",
      body: undefined,
    });
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("удаление идущей генерации — 409 job_running с текстом сервера", async () => {
    const { api } = apiWith({
      "DELETE /api/drafts/d1": {
        status: 409,
        json: {
          detail: "Идёт генерация: дождитесь её окончания.",
          code: "job_running",
        },
      },
    });

    await expect(api.deleteDraft("d1")).rejects.toMatchObject({
      status: 409,
      code: "job_running",
      detail: "Идёт генерация: дождитесь её окончания.",
    });
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

  test("справочник ссылок — GET .../refs, список {ref, kind, name} как есть", async () => {
    const refs = [
      { ref: "deity:umberlee", kind: "deity", name: "Амберли" },
      { ref: "npc:marta", kind: "npc", name: "Марта" },
    ];
    const { api, server } = apiWith({
      "GET /api/drafts/a%2Fb/refs": { json: refs },
    });

    await expect(api.listRefs("a/b")).resolves.toEqual(refs);
    expect(server.calls[0]).toMatchObject({
      method: "GET",
      path: "/api/drafts/a%2Fb/refs",
    });
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("раздел карты — объект: локации, старт и финал", async () => {
    const data = {
      locations: [locationOf()],
      start_location: "crypt",
      finale_location: null,
    };
    const { api, server } = apiWith({
      "PUT /api/drafts/d1/sections/locations": {
        json: draftOf({ version: 8 }),
      },
    });

    await api.saveSection("d1", 7, "locations", data);

    expect(server.calls[0].body).toEqual({ version: 7, data });
  });

  test("перегенерация одной сущности — шаг и цель в теле", async () => {
    const { api, server } = apiWith({
      "POST /api/drafts/d1/generate": { status: 202, json: jobOf() },
    });

    await api.generate("d1", { step: "npcs", target: "npc:marta" });

    expect(server.calls[0].body).toEqual({
      step: "npcs",
      target: "npc:marta",
    });
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

  test("ответ без кода — кода нет (прежние ошибки игр не меняются)", async () => {
    const { api } = apiWith({
      "GET /api/drafts": { status: 403, json: { detail: "Нет доступа." } },
    });

    const error = await api.listDrafts().catch((e: unknown) => e);

    expect(error).toMatchObject({ status: 403, code: undefined });
  });
});

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

// --- проверка, публикация, архив, копия, справочник SRD ---

/** Что клиент передал в fetch: тело и заголовки как есть (заглушка разбирает только JSON). */
const sent = (server: ReturnType<typeof fakeServer>, index = 0) =>
  vi.mocked(server.fetch).mock.calls[index][1] as RequestInit;

describe("клиент конструктора: проверка и публикация", () => {
  test("проверка — POST .../validate, отчёт как есть", async () => {
    const report = reportOf({
      errors: [findingOf()],
      score: 90,
      readiness_percent: 99,
    });
    const { api, server } = apiWith({
      "POST /api/drafts/a%2Fb/validate": { json: report },
    });

    await expect(api.validate("a/b")).resolves.toEqual(report);
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("публикация — POST .../publish с телом {}; 201 — id и время", async () => {
    const result = {
      adventure_id: "foggy-harbor",
      published_at: "2026-10-26T18:00:00+00:00",
    };
    const { api, server } = apiWith({
      "POST /api/drafts/d1/publish": { status: 201, json: result },
    });

    await expect(api.publish("d1")).resolves.toEqual(result);
    expect(server.calls[0].body).toEqual({});
  });

  test("отказы публикации: 422 blocking_errors с problems, 409 id_taken и job_running с кодом", async () => {
    let reply = 0;
    const replies = [
      {
        status: 422,
        json: {
          detail: "Есть ошибки проверки.",
          code: "blocking_errors",
          problems: ["clues: улик меньше трёх"],
        },
      },
      {
        status: 409,
        json: { detail: "Этот id уже занят.", code: "id_taken" },
      },
      {
        status: 409,
        json: { detail: "Идёт генерация.", code: "job_running" },
      },
    ];
    const { api } = apiWith({
      "POST /api/drafts/d1/publish": () => replies[reply++],
    });

    const errors = [
      await api.publish("d1").catch((e: unknown) => e),
      await api.publish("d1").catch((e: unknown) => e),
      await api.publish("d1").catch((e: unknown) => e),
    ];

    expect(errors[0]).toMatchObject({
      status: 422,
      code: "blocking_errors",
      problems: ["clues: улик меньше трёх"],
    });
    expect(errors[1]).toMatchObject({
      status: 409,
      code: "id_taken",
      detail: "Этот id уже занят.",
    });
    expect(errors[2]).toMatchObject({ status: 409, code: "job_running" });
  });
});

describe("клиент конструктора: архив zip", () => {
  test("экспорт — GET .../export с токеном, ответ — файл (Blob), а не JSON", async () => {
    const { api, server } = apiWith({
      "GET /api/drafts/d1/export": {
        text: "PK-архив",
        contentType: "application/zip",
      },
    });

    const archive = await api.exportArchive("d1");

    expect(archive).toBeInstanceOf(Blob);
    await expect(archive.text()).resolves.toBe("PK-архив");
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("отказ экспорта — те же русские ошибки: 422 no_adventure_id и export_invalid с problems", async () => {
    let reply = 0;
    const replies = [
      {
        status: 422,
        json: {
          detail: "Сначала задайте id приключения.",
          code: "no_adventure_id",
        },
      },
      {
        status: 422,
        json: {
          detail: "Черновик не собирается в приключение.",
          code: "export_invalid",
          problems: ["locations: нет ни одной локации"],
        },
      },
    ];
    const { api } = apiWith({
      "GET /api/drafts/d1/export": () => replies[reply++],
    });

    const noId = await api.exportArchive("d1").catch((e: unknown) => e);
    const invalid = await api.exportArchive("d1").catch((e: unknown) => e);

    expect(noId).toBeInstanceOf(ApiError);
    expect(noId).toMatchObject({
      code: "no_adventure_id",
      detail: "Сначала задайте id приключения.",
    });
    expect(invalid).toMatchObject({
      code: "export_invalid",
      problems: ["locations: нет ни одной локации"],
    });
  });

  test("импорт — POST /api/drafts/import: сырое тело архива с типом application/zip, ответ 201 Draft", async () => {
    const draft = draftOf({ id: "d7", adventure_id: "foggy-harbor" });
    const { api, server } = apiWith({
      "POST /api/drafts/import": { status: 201, json: draft },
    });
    const file = new File(["PK-архив"], "foggy-harbor.zip", {
      type: "application/zip",
    });

    await expect(api.importArchive(file)).resolves.toEqual(draft);

    const init = sent(server);
    // Тело — сам файл, без JSON и без multipart.
    expect(init.body).toBe(file);
    expect(server.calls[0].headers.get("Content-Type")).toBe("application/zip");
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });

  test("тип тела — всегда application/zip, даже если браузер не узнал тип файла", async () => {
    const { api, server } = apiWith({
      "POST /api/drafts/import": { status: 201, json: draftOf() },
    });

    await api.importArchive(new File(["PK"], "adventure.zip"));

    expect(server.calls[0].headers.get("Content-Type")).toBe("application/zip");
  });

  test("отказы импорта: 422 archive_invalid с problems, 413 too_large и 415 unsupported_media с текстом сервера", async () => {
    let reply = 0;
    const replies = [
      {
        status: 422,
        json: {
          detail: "Архив не подходит.",
          code: "archive_invalid",
          problems: ["путь выходит за папку приключения"],
        },
      },
      {
        status: 413,
        json: { detail: "Архив больше 2 МиБ.", code: "too_large" },
      },
      {
        status: 415,
        json: { detail: "Нужен архив zip.", code: "unsupported_media" },
      },
    ];
    const { api } = apiWith({
      "POST /api/drafts/import": () => replies[reply++],
    });
    const file = new File(["PK"], "x.zip");

    const errors = [
      await api.importArchive(file).catch((e: unknown) => e),
      await api.importArchive(file).catch((e: unknown) => e),
      await api.importArchive(file).catch((e: unknown) => e),
    ];

    expect(errors[0]).toMatchObject({
      status: 422,
      code: "archive_invalid",
      problems: ["путь выходит за папку приключения"],
    });
    expect(errors[1]).toMatchObject({
      status: 413,
      code: "too_large",
      detail: "Архив больше 2 МиБ.",
    });
    expect(errors[2]).toMatchObject({
      status: 415,
      code: "unsupported_media",
      detail: "Нужен архив zip.",
    });
  });

  test("обрыв связи при импорте — статус 0 и текст про связь", async () => {
    const { api } = apiWith({
      "POST /api/drafts/import": () => {
        throw new TypeError("Failed to fetch");
      },
    });

    const error = await api
      .importArchive(new File(["PK"], "x.zip"))
      .catch((e: unknown) => e);

    expect(error).toMatchObject({
      status: 0,
      detail: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    });
  });
});

describe("клиент конструктора: копия и справочник SRD", () => {
  test("копия приключения каталога — POST /api/adventures/{id}/draft, id кодируется, ответ 201 Draft", async () => {
    const draft = draftOf({ id: "d8", adventure_id: "demo-crypt" });
    const { api, server } = apiWith({
      "POST /api/adventures/demo%2Fcrypt/draft": { status: 201, json: draft },
    });

    await expect(api.copyAdventure("demo/crypt")).resolves.toEqual(draft);
    expect(server.calls[0]).toMatchObject({
      method: "POST",
      path: "/api/adventures/demo%2Fcrypt/draft",
    });
  });

  test("справочник монстров SRD — GET /api/srd/monsters, список как есть", async () => {
    const monsters = [
      srdMonsterOf(),
      srdMonsterOf({ id: "ghoul", name: "Гуль", cr: 1, xp: 200 }),
    ];
    const { api, server } = apiWith({
      "GET /api/srd/monsters": { json: monsters },
    });

    await expect(api.listSrdMonsters()).resolves.toEqual(monsters);
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok");
  });
});

// --- файл в ответе и сырое тело — те же русские ошибки ---

describe("клиент: архив — сбои связи и чужие ответы по-русски", () => {
  test("экспорт: обрыв связи — статус 0 и текст про связь", async () => {
    const { api } = apiWith({
      "GET /api/drafts/d1/export": () => {
        throw new TypeError("Failed to fetch");
      },
    });

    await expect(api.exportArchive("d1")).rejects.toMatchObject({
      status: 0,
      detail: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    });
  });

  test.each([
    [502, "Сервер временно недоступен. Попробуйте ещё раз немного позже."],
    [404, "Не найдено."],
  ])(
    "экспорт: %i от прокси без JSON — русский текст по коду, не тело страницы",
    async (status, detail) => {
      const { api } = apiWith({
        "GET /api/drafts/d1/export": {
          status,
          text: "<html>Bad Gateway</html>",
          contentType: "text/html",
        },
      });

      const error = await api.exportArchive("d1").catch((e: unknown) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ status, detail, problems: undefined });
    },
  );

  test("экспорт: тело успешного ответа не прочиталось — «Сервер вернул непонятный ответ.»", async () => {
    const broken = new Response("PK", { status: 200 });
    Object.defineProperty(broken, "blob", {
      value: () => Promise.reject(new TypeError("body stream already read")),
    });
    const api = createApi(
      vi.fn(async () => broken) as unknown as typeof fetch,
      () => "tok",
    );

    await expect(api.exportArchive("d1")).rejects.toMatchObject({
      status: 200,
      detail: "Сервер вернул непонятный ответ.",
    });
  });

  test("экспорт: GET без тела и без Content-Type; ответ zip отдаётся файлом даже с Accept JSON", async () => {
    const { api, server } = apiWith({
      "GET /api/drafts/d1/export": {
        text: "PK\u0003\u0004",
        contentType: "application/zip",
      },
    });

    const archive = await api.exportArchive("d1");

    expect(archive.type).toBe("application/zip");
    expect(sent(server).body).toBeUndefined();
    expect(server.calls[0].headers.get("Content-Type")).toBeNull();
  });

  test("импорт: 502 прокси без JSON — русский текст по коду; тело запроса — сам файл", async () => {
    const { api, server } = apiWith({
      "POST /api/drafts/import": {
        status: 502,
        text: "<html>Bad Gateway</html>",
        contentType: "text/html",
      },
    });
    const file = new File(["PK"], "x.zip");

    await expect(api.importArchive(file)).rejects.toMatchObject({
      status: 502,
      detail: "Сервер временно недоступен. Попробуйте ещё раз немного позже.",
    });
    expect(sent(server).body).toBe(file);
  });

  test("импорт: problems читаются только у 422 — у 413 их нет, даже если сервер прислал", async () => {
    const { api } = apiWith({
      "POST /api/drafts/import": {
        status: 413,
        json: {
          detail: "Архив больше 2 МиБ.",
          code: "too_large",
          problems: ["лишнее"],
        },
      },
    });

    await expect(
      api.importArchive(new File(["PK"], "x.zip")),
    ).rejects.toMatchObject({
      status: 413,
      code: "too_large",
      detail: "Архив больше 2 МиБ.",
      problems: undefined,
    });
  });
});
