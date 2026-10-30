import type { WsEvent } from "./types";

// Коды закрытия сокета игры (realtime/ws.py). Остальные коды — обрыв или
// перегрузка (1011, 1013, 1006…): их лечит переподключение.
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_NOT_A_MEMBER = 4403;
// Причина закрытия 4403 у исключённого хостом.
const KICKED_REASON = "kicked";

const PING_INTERVAL_MS = 25_000;
// Сколько ждём хоть какой-нибудь кадр после ping. Смена Wi-Fi на мобильную сеть оставляет
// сокет «открытым», но мёртвым, и без этого срока браузер не заметит обрыва минутами.
const PONG_TIMEOUT_MS = 10_000;
const FIRST_RETRY_DELAY_MS = 1_000;
const MAX_RETRY_DELAY_MS = 15_000;
const PING = JSON.stringify({ type: "ping" });

/** Состояние связи: `open` — сокет открыт, `reconnecting` — оборвался и будет открыт снова. */
export type ConnectionStatus = "open" | "reconnecting";

/**
 * Почему сервер отказал в доступе: переподключаться бесполезно. `kicked` — игрок больше не участник,
 * потому что его исключил хост.
 */
export type Rejection = "unauthorized" | "not-a-member" | "kicked";

const REJECTIONS: Partial<Record<number, Rejection>> = {
  [CLOSE_UNAUTHORIZED]: "unauthorized",
  [CLOSE_NOT_A_MEMBER]: "not-a-member",
};

export interface GameConnectionHandlers {
  onStatus?: (status: ConnectionStatus) => void;
  /** Сервер закрыл сокет кодом 4401 или 4403; после этого подключение остановлено. */
  onRejected?: (reason: Rejection) => void;
}

/**
 * Отказ по коду закрытия. 4403 — исключение хостом, если об этом сказала причина закрытия или
 * кадр `kicked` (он мог прийти и на прежнем сокете, а 4403 — уже на новом).
 */
function rejectionOf(
  code: number,
  reason: string,
  kicked: boolean,
): Rejection | undefined {
  const rejection = REJECTIONS[code];
  return rejection === "not-a-member" && (kicked || reason === KICKED_REASON)
    ? "kicked"
    : rejection;
}

/**
 * Подпротокол сокета игры. Браузер не умеет слать свои заголовки при открытии сокета, кроме
 * `Sec-WebSocket-Protocol`, поэтому токен сессии идёт вторым подпротоколом после этого имени.
 * Сервер отвечает только именем: браузер требует в ответе один из предложенных подпротоколов,
 * а токен назад не возвращается.
 */
const GAME_PROTOCOL = "dnd-game.v1";

/**
 * Адрес сокета игры — без токена: адрес попадает в журналы прокси и туннеля, а токен идёт
 * подпротоколом (`GAME_PROTOCOL`).
 */
export function gameSocketUrl(
  gameId: string,
  page: Pick<Location, "protocol" | "host"> = window.location,
): string {
  const scheme = page.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${page.host}/ws/games/${encodeURIComponent(gameId)}`;
}

// Кадр без type или не JSON — сбой сервера или прокси. Содержимое в журнал не пишем:
// в кадрах бывают личные сообщения игрока.
function parseEvent(data: unknown): WsEvent | null {
  if (typeof data === "string") {
    try {
      const parsed: unknown = JSON.parse(data);
      if (
        typeof parsed === "object" &&
        parsed !== null &&
        typeof (parsed as { type?: unknown }).type === "string"
      ) {
        return parsed as WsEvent;
      }
    } catch {
      // разбор не удался — ниже тот же отказ, что и для кадра без type
    }
  }
  console.warn("Сокет игры прислал непонятный кадр.");
  return null;
}

/**
 * Подключает игрока к сокету игры и держит связь: после обрыва открывает новый сокет
 * через 1, 2, 4… секунд (не дольше 15), раз в 25 секунд шлёт ping. Первым на любом
 * соединении приходит полный снимок, поэтому потерянные за обрыв события не нужны.
 *
 * Если за 10 секунд после ping не пришло ни одного кадра, соединение считается мёртвым:
 * сокет закрывается и открывается заново по той же лестнице задержек. Возврат сети (`online`)
 * и возврат на вкладку (`visibilitychange`) не ждут задержки: ожидающий повтор выполняется
 * сразу, а живое соединение проверяется ping.
 *
 * Задержка сбрасывается, когда пришёл снимок, а не когда сокет открылся: сервер
 * принимает рукопожатие и лишь потом может закрыть сокет (1011), и сброс по открытию
 * превратил бы это в повторы раз в секунду.
 *
 * Возвращает `close()`: останавливает связь и повторы; его можно отдать как очистку
 * эффекта React.
 */
export function connectGame(
  gameId: string,
  token: string,
  onEvent: (event: WsEvent) => void,
  handlers: GameConnectionHandlers = {},
): () => void {
  const url = gameSocketUrl(gameId);
  const protocols = [GAME_PROTOCOL, token];
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let pingTimer: ReturnType<typeof setInterval> | undefined;
  let pongTimer: ReturnType<typeof setTimeout> | undefined;
  let failures = 0; // обрывов подряд без снимка
  let kicked = false; // сервер прислал кадр `kicked`: следующий 4403 — исключение

  const stopWatching = () => {
    clearInterval(pingTimer);
    clearTimeout(pongTimer);
    pongTimer = undefined;
  };

  // Закрытие сокета само вызывает onclose: у сокета, который отпустили, слушать некому.
  const release = (released: WebSocket) => {
    released.onopen = null;
    released.onmessage = null;
    released.onclose = null;
    released.close();
  };

  const scheduleRetry = () => {
    handlers.onStatus?.("reconnecting");
    const delay = Math.min(
      FIRST_RETRY_DELAY_MS * 2 ** failures,
      MAX_RETRY_DELAY_MS,
    );
    failures += 1;
    retryTimer = setTimeout(() => {
      retryTimer = undefined;
      open();
    }, delay);
  };

  const ping = (current: WebSocket) => {
    current.send(PING);
    // Срок идёт с первого ping без ответа: следующий его не продлевает.
    pongTimer ??= setTimeout(() => {
      pongTimer = undefined;
      stopWatching();
      release(current);
      scheduleRetry();
    }, PONG_TIMEOUT_MS);
  };

  // Сеть или вкладка вернулись: не ждём задержки повтора, а живое соединение проверяем.
  const recheck = () => {
    if (retryTimer !== undefined) {
      clearTimeout(retryTimer);
      retryTimer = undefined;
      open();
    } else if (socket?.readyState === WebSocket.OPEN) {
      ping(socket);
    }
  };
  const onOnline = () => recheck();
  const onVisibility = () => {
    if (document.visibilityState === "visible") recheck();
  };
  window.addEventListener("online", onOnline);
  document.addEventListener("visibilitychange", onVisibility);

  const close = () => {
    window.removeEventListener("online", onOnline);
    document.removeEventListener("visibilitychange", onVisibility);
    clearTimeout(retryTimer);
    retryTimer = undefined;
    stopWatching();
    if (socket === null) return;
    release(socket);
    socket = null;
  };

  const open = () => {
    const current = new WebSocket(url, protocols);
    socket = current;
    current.onopen = () => {
      handlers.onStatus?.("open");
      pingTimer = setInterval(() => ping(current), PING_INTERVAL_MS);
    };
    current.onmessage = ({ data }) => {
      // Любой кадр говорит, что связь жива, а не только pong.
      clearTimeout(pongTimer);
      pongTimer = undefined;
      const event = parseEvent(data);
      if (event === null || event.type === "pong") return;
      if (event.type === "kicked") {
        kicked = true;
        return;
      }
      if (event.type === "snapshot") failures = 0;
      onEvent(event);
    };
    current.onclose = ({ code, reason }) => {
      stopWatching();
      const rejection = rejectionOf(code, reason, kicked);
      if (rejection !== undefined) {
        close();
        handlers.onRejected?.(rejection);
        return;
      }
      scheduleRetry();
    };
  };

  open();
  return close;
}
