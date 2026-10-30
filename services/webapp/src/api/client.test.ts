import { sessionToken } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import { createApi } from "./client";
import type { HostAction, RestKind, RestView } from "./types";

// Привалы по кнопке хоста: пути, тела и ответы сервера.

const PHASE = {
  phase: "playing",
  turn_phase: "camp",
  deadline_at: "2026-10-20T12:02:00+00:00",
  remaining_seconds: null,
  scene: null,
};

function apiWith(routes: Parameters<typeof fakeServer>[0]) {
  signInAs({ id: "u1", name: "Вика" });
  const server = fakeServer(routes);
  return { api: createApi(server.fetch, sessionToken), server };
}

describe("клиент API: привалы", () => {
  test.each(["short", "long"] as const)(
    "startRest(%s): POST host/rest с видом привала, ответ — фаза игры",
    async (kind) => {
      const { api, server } = apiWith({
        "POST /api/games/g1/host/rest": { json: PHASE },
      });

      const phase = await api.startRest("g1", kind);

      expect(phase).toEqual(PHASE);
      expect(server.calls).toHaveLength(1);
      expect(server.calls[0].method).toBe("POST");
      expect(server.calls[0].url).toBe("/api/games/g1/host/rest");
      expect(server.calls[0].body).toEqual({ kind });
      expect(server.calls[0].headers.get("Authorization")).toBe(
        "Bearer tok-u1",
      );
    },
  );

  test("spendHitDie: POST rest/hit-dice без тела, ответ — остаток костей и хиты сервера", async () => {
    const result = { hit_dice: 1, hp: 9, hp_max: 12 };
    const { api, server } = apiWith({
      "POST /api/games/g1/rest/hit-dice": { json: result },
    });

    await expect(api.spendHitDie("g1")).resolves.toEqual(result);
    expect(server.calls[0].url).toBe("/api/games/g1/rest/hit-dice");
    expect(server.calls[0].body).toBeUndefined();
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok-u1");
  });

  test("отказ сервера (409) приходит его текстом", async () => {
    const detail = "Сейчас не короткий привал: кости хитов не тратятся.";
    const { api } = apiWith({
      "POST /api/games/g1/rest/hit-dice": { status: 409, json: { detail } },
    });

    await expect(api.spendHitDie("g1")).rejects.toMatchObject({
      status: 409,
      detail,
    });
  });

  // Отказ начать привал — тоже текстом сервера (409 и 403).
  test.each([
    [409, "Привал уже идёт: второй начать нельзя."],
    [403, "Эта кнопка доступна только хосту."],
  ])(
    "startRest: отказ сервера (%i) приходит его текстом",
    async (status, detail) => {
      const { api } = apiWith({
        "POST /api/games/g1/host/rest": { status, json: { detail } },
      });

      await expect(api.startRest("g1", "short")).rejects.toMatchObject({
        status,
        detail,
      });
    },
  );

  test.each<HostAction>(["rest-extend", "rest-end", "camp-extend", "camp-end"])(
    "hostAction(%s): новые пути привала и прежние пути работают",
    async (action) => {
      const { api, server } = apiWith({
        [`POST /api/games/g1/host/${action}`]: { json: PHASE },
      });

      await api.hostAction("g1", action);

      expect(server.calls[0].url).toBe(`/api/games/g1/host/${action}`);
      expect(server.calls[0].body).toBeUndefined();
    },
  );

  test("id игры экранируется в пути", async () => {
    const { api, server } = apiWith({
      "POST /api/games/a%2Fb/rest/hit-dice": {
        json: { hit_dice: 0, hp: 1, hp_max: 1 },
      },
    });

    await api.spendHitDie("a/b");

    expect(server.calls[0].url).toBe("/api/games/a%2Fb/rest/hit-dice");
  });
});

// Сверка клиент–сервер на стороне сервера (tests/flows/test_client_routes.py)
// сравнивает поля кадров и верхний уровень снимка, но не поля блока `rest` внутри снимка. Сервер
// закрепляет их набор в tests/realtime/test_views.py; здесь тот же набор закреплён у клиента:
// переименование поля в types.ts без сервера ломает и `tsc -b` (satisfies), и этот тест.
describe("типы привала совпадают с контрактом сервера", () => {
  test("блок rest снимка — ровно kind, deadline_at, my_ideas, lines", () => {
    const fields = {
      kind: true,
      deadline_at: true,
      my_ideas: true,
      lines: true,
    } satisfies Record<keyof RestView, true>;

    expect(Object.keys(fields).sort()).toEqual([
      "deadline_at",
      "kind",
      "lines",
      "my_ideas",
    ]);
  });

  test("вид привала — только short и long, как kind в теле host/rest и в кадрах", () => {
    const kinds = { short: true, long: true } satisfies Record<RestKind, true>;

    expect(Object.keys(kinds)).toEqual(["short", "long"]);
  });
});
