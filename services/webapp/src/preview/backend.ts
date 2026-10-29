import type { Snapshot } from "../api/types";

// Сервер для макетов B10.1: экраны превью работают на данных screenData.ts и в сеть не ходят.
// REST отвечает таблица маршрутов (её получает клиент API через fetchImpl), а сокет игры
// подменяется так, что сразу присылает снимок нужной игры. Только для dev-страницы превью.

/** Ответ маршрута: JSON с кодом, обрыв связи или запрос, который никогда не закончится (загрузка). */
export type PreviewReply =
  { status?: number; json: unknown } | "offline" | "pending";

/** Маршруты вида `"GET /api/games"`. */
export type PreviewRoutes = Record<string, PreviewReply>;

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/**
 * `fetch` превью по таблице маршрутов. Незнакомый маршрут отвечает как Starlette — 404, чтобы
 * забытый ответ было видно на экране, а не по зависшей загрузке.
 */
export function previewFetch(routes: PreviewRoutes): typeof fetch {
  return async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const path = new URL(String(input), window.location.origin).pathname;
    const reply = routes[`${method} ${path}`];
    if (reply === "offline") throw new TypeError("Failed to fetch");
    if (reply === "pending") return new Promise<Response>(() => {});
    if (reply === undefined) return jsonResponse(404, { detail: "Not Found" });
    return jsonResponse(reply.status ?? 200, reply.json);
  };
}

const GAME_SOCKET = /\/ws\/games\/([^/?]+)/;
// Код закрытия «игры нет или игрок не участник» (как у сервера, api/ws.ts).
const CLOSE_NOT_A_MEMBER = 4403;

let served: Record<string, Snapshot> = {};
let previewSocket: unknown = null;

/**
 * Подменяет глобальный `WebSocket`: сокет игры открывается сам и первым кадром присылает снимок
 * этой игры из `snapshots`; незнакомая игра закрывается кодом 4403. Остальные сокеты (например,
 * горячая перезагрузка Vite) идут в настоящий `WebSocket`. Повторный вызов только меняет снимки.
 */
export function installPreviewSocket(
  snapshots: Record<string, Snapshot>,
): void {
  served = snapshots;
  if (previewSocket !== null && window.WebSocket === previewSocket) return;
  const RealSocket = window.WebSocket;

  class PreviewSocket {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;

    readyState = PreviewSocket.CONNECTING;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;

    constructor(url: string | URL, protocols?: string | string[]) {
      const gameId = GAME_SOCKET.exec(String(url))?.[1];
      if (gameId === undefined) {
        // Конструктор вправе вернуть другой объект: чужой сокет работает как обычно.
        return new RealSocket(url, protocols) as unknown as PreviewSocket;
      }
      // Как у браузера: обработчики вешают после конструктора, поэтому кадры — следующей задачей.
      setTimeout(() => this.serve(decodeURIComponent(gameId)), 0);
    }

    send() {
      // ping сокета игры: превью отвечать на него незачем, связь «жива» всегда.
    }

    close() {
      this.readyState = PreviewSocket.CLOSED;
    }

    private serve(gameId: string) {
      if (this.readyState === PreviewSocket.CLOSED) return;
      const snapshot = served[gameId];
      if (snapshot === undefined) {
        this.readyState = PreviewSocket.CLOSED;
        this.onclose?.({ code: CLOSE_NOT_A_MEMBER });
        return;
      }
      this.readyState = PreviewSocket.OPEN;
      this.onopen?.();
      this.onmessage?.({ data: JSON.stringify(snapshot) });
    }
  }

  previewSocket = PreviewSocket;
  window.WebSocket = PreviewSocket as unknown as typeof WebSocket;
}
