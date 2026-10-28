// Подключает матчеры jest-dom (toBeInTheDocument и др.) к expect из vitest.
import "@testing-library/jest-dom/vitest";
import type { GameSummary, PlayerSummary, Snapshot, User } from "./api/types";
import { useGame } from "./store/game";

// Каждый тест начинает с чистого окружения: без подмен глобальных объектов,
// адреса с чужими query-параметрами, данных в хранилищах браузера и сессии.
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
  window.sessionStorage.clear();
  useGame.setState(useGame.getInitialState(), true);
});

/** Один запрос, дошедший до заглушки сервера. */
export interface RecordedCall {
  method: string;
  /** Путь без query: query в REST-запросах быть не должно (см. тесты клиента). */
  path: string;
  /** Адрес целиком, как его передали в fetch. */
  url: string;
  headers: Headers;
  body: unknown;
}

/** Ответ заглушки: JSON, обычный текст (как отдают прокси и туннели) или пустое тело. */
export interface FakeReply {
  status?: number;
  json?: unknown;
  text?: string;
  contentType?: string;
}

type Route = FakeReply | ((call: RecordedCall) => FakeReply);

/**
 * Заглушка сервера для `fetch`: маршруты вида `"POST /api/games/join"`. Неизвестный
 * маршрут отвечает как Starlette — 404 с английским detail, чтобы забытую заглушку
 * было видно по ошибке, а не по зависшему тесту.
 */
export function fakeServer(routes: Record<string, Route>) {
  const calls: RecordedCall[] = [];

  const stub = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const path = new URL(url, "http://localhost").pathname;
      const rawBody = typeof init?.body === "string" ? init.body : undefined;
      const call: RecordedCall = {
        method,
        path,
        url,
        headers: new Headers(init?.headers),
        body: rawBody === undefined ? undefined : JSON.parse(rawBody),
      };
      calls.push(call);

      const route = routes[`${method} ${path}`];
      const reply: FakeReply =
        route === undefined
          ? { status: 404, json: { detail: "Not Found" } }
          : typeof route === "function"
            ? route(call)
            : route;
      const status = reply.status ?? 200;
      if (reply.json !== undefined) {
        return new Response(JSON.stringify(reply.json), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(status === 204 ? null : (reply.text ?? ""), {
        status,
        headers: { "Content-Type": reply.contentType ?? "text/plain" },
      });
    },
  );

  return { fetch: stub as unknown as typeof fetch, calls };
}

/**
 * Заглушка сокета браузера. Клиент видит обычный `WebSocket`, а тест играет за сервер:
 * `open`, `serve`, `drop`. Сокет не шлёт событий сам, поэтому порядок кадров задаёт тест.
 */
export class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState: number = FakeSocket.CONNECTING;
  /** Всё, что клиент отправил серверу. */
  readonly sent: string[] = [];
  /** Код, с которым клиент закрыл сокет сам; `null` — не закрывал. */
  clientCloseCode: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
  }

  send(data: string) {
    // Как в браузере: до открытия сокета отправка бросает исключение.
    if (this.readyState === FakeSocket.CONNECTING) {
      throw new Error("Сокет ещё не открыт.");
    }
    this.sent.push(data);
  }

  close(code = 1000) {
    this.readyState = FakeSocket.CLOSED;
    this.clientCloseCode = code;
  }

  /** Сервер принял рукопожатие. */
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  /** Сервер прислал кадр: объект уходит как JSON, строка — как есть. */
  serve(frame: unknown) {
    const data = typeof frame === "string" ? frame : JSON.stringify(frame);
    this.onmessage?.({ data });
  }

  /** Связь оборвалась или сервер закрыл сокет с кодом. */
  drop(code = 1006) {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code });
  }
}

/** Подменяет глобальный `WebSocket`; `sockets` — все созданные клиентом сокеты по порядку. */
export function fakeWebSocket() {
  const sockets: FakeSocket[] = [];
  class StubSocket extends FakeSocket {
    constructor(url: string) {
      super(url);
      sockets.push(this);
    }
  }
  vi.stubGlobal("WebSocket", StubSocket);

  const last = (): FakeSocket => {
    const socket = sockets.at(-1);
    if (socket === undefined) {
      throw new Error("Клиент не создал ни одного сокета.");
    }
    return socket;
  };
  return { sockets, last };
}

// --- готовые данные для тестов экранов ---

/** Игра в лобби с настройками по умолчанию; поля переопределяются точечно. */
export function gameSummary(overrides: Partial<GameSummary> = {}): GameSummary {
  return {
    id: "g1",
    code: "ABC123",
    adventure_id: "demo-crypt",
    adventure_title: "Склеп",
    phase: "lobby",
    turn_phase: null,
    provider: null,
    model: null,
    collect_seconds: 120,
    hurry_seconds: 60,
    camp_seconds: 300,
    short_rest_seconds: 120,
    profanity: true,
    players: [],
    ...overrides,
  };
}

/** Игрок лобби: без персонажа и не готов. */
export function lobbyPlayer(
  overrides: Partial<PlayerSummary> = {},
): PlayerSummary {
  return {
    user_id: "u1",
    name: "Лира",
    is_host: false,
    ready: false,
    has_character: false,
    ...overrides,
  };
}

/** Снимок игры, каким его присылает сокет сразу после подключения. */
export function snapshotOf(
  game: GameSummary,
  overrides: Partial<Snapshot> = {},
): Snapshot {
  return {
    type: "snapshot",
    server_time: "2026-10-20T12:00:00+00:00",
    game,
    host_user_id: game.players.find((p) => p.is_host)?.user_id ?? "u1",
    phase: {
      phase: game.phase,
      turn_phase: game.turn_phase,
      deadline_at: null,
      remaining_seconds: null,
      scene: null,
    },
    statuses: {},
    character: null,
    action: null,
    party: [],
    combat: null,
    feed: [],
    journal: [],
    rest: null,
    ...overrides,
  };
}

/** Кладёт сессию в хранилище: экраны, кроме входа, без неё не открываются. */
export function signInAs(user: Partial<User> = {}): User {
  const resolved: User = {
    id: "u1",
    name: "Лира",
    can_create_games: false,
    ...user,
  };
  useGame
    .getState()
    .setSession({ token: `tok-${resolved.id}`, user: resolved });
  return resolved;
}
