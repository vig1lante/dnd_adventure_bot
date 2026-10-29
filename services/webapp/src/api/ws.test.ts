import { fakeWebSocket } from "../test-setup";
import type { FakeSocket } from "../test-setup";
import { connectGame, gameSocketUrl } from "./ws";
import type { ConnectionStatus, Rejection } from "./ws";
import type { WsEvent } from "./types";

const TOKEN = "tok-secret-123";
const SNAPSHOT = { type: "snapshot", server_time: "2026-10-20T12:00:00+00:00" };

// Подключения слушают окно и документ (online, visibilitychange): тест, не закрывший своё,
// получал бы события чужих тестов. Поэтому всё открытое закрывается после каждого теста.
const openConnections: (() => void)[] = [];

afterEach(() => {
  while (openConnections.length > 0) openConnections.pop()?.();
});

/** Подключение с записью всего, что клиент сообщил наружу. */
function connect(gameId = "g1") {
  const sockets = fakeWebSocket();
  const events: WsEvent[] = [];
  const statuses: ConnectionStatus[] = [];
  const rejections: Rejection[] = [];
  const close = connectGame(gameId, TOKEN, (event) => events.push(event), {
    onStatus: (status) => statuses.push(status),
    onRejected: (reason) => rejections.push(reason),
  });
  openConnections.push(close);
  return { ...sockets, events, statuses, rejections, close };
}

/** Оборвать связь и дождаться нового сокета через `delayMs`. */
function reconnectAfter(
  connection: ReturnType<typeof connect>,
  delayMs: number,
  code = 1006,
): FakeSocket {
  const before = connection.sockets.length;
  connection.last().drop(code);
  vi.advanceTimersByTime(delayMs - 1);
  expect(connection.sockets).toHaveLength(before);
  vi.advanceTimersByTime(1);
  expect(connection.sockets).toHaveLength(before + 1);
  return connection.last();
}

beforeEach(() => {
  vi.useFakeTimers();
});

describe("gameSocketUrl: адрес сокета", () => {
  test("http даёт ws, а токен идёт в query", () => {
    expect(
      gameSocketUrl("g1", "tok", { protocol: "http:", host: "localhost:5173" }),
    ).toBe("ws://localhost:5173/ws/games/g1?token=tok");
  });

  test("https даёт wss: Mini App в Telegram открывается по https", () => {
    expect(
      gameSocketUrl("g1", "tok", { protocol: "https:", host: "app.example" }),
    ).toBe("wss://app.example/ws/games/g1?token=tok");
  });

  test("id игры и токен экранируются", () => {
    const url = gameSocketUrl("a/b", "t&x=1 2", {
      protocol: "http:",
      host: "h",
    });

    expect(url).toBe("ws://h/ws/games/a%2Fb?token=t%26x%3D1%202");
  });

  test("по умолчанию берётся адрес страницы", () => {
    expect(gameSocketUrl("g1", "tok")).toBe(
      `${window.location.protocol === "https:" ? "wss:" : "ws:"}//` +
        `${window.location.host}/ws/games/g1?token=tok`,
    );
  });
});

describe("connectGame: подключение", () => {
  test("сразу открывает сокет игры с токеном в адресе", () => {
    const { sockets, last } = connect("g7");

    expect(sockets).toHaveLength(1);
    expect(last().url).toBe(gameSocketUrl("g7", TOKEN));
    expect(last().url).toContain(`/ws/games/g7?token=${TOKEN}`);
  });

  test("события сервера по порядку доходят до onEvent разобранными", () => {
    const { last, events } = connect();
    last().open();

    last().serve(SNAPSHOT);
    last().serve({ type: "phase", phase: { phase: "playing" } });

    expect(events.map((event) => event.type)).toEqual(["snapshot", "phase"]);
    expect(events[1]).toEqual({ type: "phase", phase: { phase: "playing" } });
  });

  test("pong — ответ на ping: до onEvent он не доходит", () => {
    const { last, events } = connect();
    last().open();

    last().serve({ type: "pong" });

    expect(events).toEqual([]);
  });

  test("непонятные кадры пропускаются, не ломают связь и не попадают в журнал", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const { last, events } = connect();
    last().open();

    last().serve("это не JSON, а секрет tok-in-frame");
    last().serve("[1, 2]");
    last().serve('{"no_type": true}');
    last().serve('{"type": 5}');
    last().onmessage?.({ data: new ArrayBuffer(4) });
    last().serve({ type: "party", party: [] });

    expect(events).toEqual([{ type: "party", party: [] }]);
    const logged = JSON.stringify([...warn.mock.calls, ...error.mock.calls]);
    expect(warn).toHaveBeenCalled();
    expect(logged).not.toContain("tok-in-frame");
    expect(logged).not.toContain(TOKEN);
  });

  test("статус: open при открытии сокета", () => {
    const { last, statuses } = connect();

    expect(statuses).toEqual([]);
    last().open();

    expect(statuses).toEqual(["open"]);
  });
});

describe("connectGame: переподключение", () => {
  test("при обрыве задержка растёт 1, 2, 4, 8 с и упирается в 15 с", () => {
    const connection = connect();
    connection.last().open();

    const delays = [1000, 2000, 4000, 8000, 15000, 15000, 15000];
    for (const delay of delays) {
      const socket = reconnectAfter(connection, delay);
      socket.open(); // сокет открылся, но снимка не было: задержка не сбрасывается
    }
  });

  test("после снимка задержка снова начинается с 1 с", () => {
    const connection = connect();
    connection.last().open();
    reconnectAfter(connection, 1000).open();
    reconnectAfter(connection, 2000).open();

    connection.last().serve(SNAPSHOT);

    reconnectAfter(connection, 1000);
  });

  test.each([
    ["ошибка сервера", { type: "error", message: "Сбой." }],
    ["событие фазы", { type: "phase", phase: { phase: "playing" } }],
    ["запись ленты", { type: "feed_item", item: { id: 1 } }],
  ])(
    "кадр «%s» без снимка задержку не сбрасывает: только снимок говорит, что игра подключена",
    (_name, frame) => {
      const connection = connect();
      connection.last().open();
      reconnectAfter(connection, 1000).open();

      connection.last().serve(frame);

      reconnectAfter(connection, 2000);
    },
  );

  test("сервер, закрывающий сокет сразу после открытия, не получает частых повторов", () => {
    const connection = connect();

    // Открытие без снимка (например, код 1011) задержку не сбрасывает.
    reconnectAfter(connection, 1000, 1011).open();
    reconnectAfter(connection, 2000, 1011).open();
    reconnectAfter(connection, 4000, 1013);
  });

  test("обрыв до открытия сокета (сервер недоступен) тоже повторяется", () => {
    const connection = connect();

    reconnectAfter(connection, 1000);
    reconnectAfter(connection, 2000);
  });

  test.each([1011, 1013, 1006, 1001, 1000])(
    "код закрытия %i — переподключение, а не отказ",
    (code) => {
      const connection = connect();
      connection.last().open();

      reconnectAfter(connection, 1000, code);

      expect(connection.rejections).toEqual([]);
    },
  );

  test("статусы: open, затем reconnecting на каждый обрыв", () => {
    const connection = connect();
    connection.last().open();

    reconnectAfter(connection, 1000).open();

    expect(connection.statuses).toEqual(["open", "reconnecting", "open"]);
  });

  test("повторное подключение идёт на тот же адрес с тем же токеном", () => {
    const connection = connect("g9");
    connection.last().open();

    const again = reconnectAfter(connection, 1000);

    expect(again.url).toBe(gameSocketUrl("g9", TOKEN));
  });

  test("события нового сокета доходят до onEvent, как и прежнего", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve(SNAPSHOT);

    const again = reconnectAfter(connection, 1000);
    again.open();
    again.serve({ ...SNAPSHOT, server_time: "2026-10-20T12:05:00+00:00" });

    expect(connection.events).toHaveLength(2);
  });
});

describe("connectGame: отказ в доступе", () => {
  test("4401 — сессия недействительна: отказ и ни одной попытки повтора", () => {
    const connection = connect();
    connection.last().open();

    connection.last().drop(4401);
    vi.advanceTimersByTime(10 * 60 * 1000);

    expect(connection.rejections).toEqual(["unauthorized"]);
    expect(connection.sockets).toHaveLength(1);
    expect(connection.statuses).toEqual(["open"]);
  });

  test("4403 — игрок не участник: отказ и ни одной попытки повтора", () => {
    const connection = connect();
    connection.last().open();

    connection.last().drop(4403);
    vi.advanceTimersByTime(10 * 60 * 1000);

    expect(connection.rejections).toEqual(["not-a-member"]);
    expect(connection.sockets).toHaveLength(1);
  });

  test("отказ приходит и при повторном подключении (игрока исключили)", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve(SNAPSHOT);
    // Сервер снял соединение исключённого игрока, а на новом сокете сказал 4403.
    const again = reconnectAfter(connection, 1000, 1013);
    again.open();

    again.drop(4403);
    vi.advanceTimersByTime(60 * 1000);

    expect(connection.rejections).toEqual(["not-a-member"]);
    expect(connection.sockets).toHaveLength(2);
  });

  test("исключение хостом: кадр kicked, затем 4403 — отказ «kicked», кадр до onEvent не доходит", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve(SNAPSHOT);

    connection.last().serve({ type: "kicked" });
    connection.last().drop(4403, "kicked");
    vi.advanceTimersByTime(10 * 60 * 1000);

    expect(connection.rejections).toEqual(["kicked"]);
    expect(connection.events.map((event) => event.type)).toEqual(["snapshot"]);
    expect(connection.sockets).toHaveLength(1);
  });

  test("4403 с причиной kicked, хотя кадр потерялся, — тоже исключение", () => {
    const connection = connect();
    connection.last().open();

    connection.last().drop(4403, "kicked");

    expect(connection.rejections).toEqual(["kicked"]);
  });

  test("кадр kicked, обрыв, а 4403 уже на новом сокете — всё равно исключение", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve(SNAPSHOT);
    connection.last().serve({ type: "kicked" });
    const again = reconnectAfter(connection, 1000, 1006);
    again.open();

    again.drop(4403);

    expect(connection.rejections).toEqual(["kicked"]);
  });

  // Финальное покрытие B11: признак исключения уточняет только 4403.
  test("кадр kicked, а сокет закрыт 4401 — отказ «unauthorized»: недействительная сессия важнее", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve({ type: "kicked" });

    connection.last().drop(4401, "kicked");

    expect(connection.rejections).toEqual(["unauthorized"]);
  });

  test("кадр kicked, а сокет просто оборвался — переподключение, как при любом обрыве", () => {
    const connection = connect();
    connection.last().open();
    connection.last().serve(SNAPSHOT);
    connection.last().serve({ type: "kicked" });

    reconnectAfter(connection, 1000, 1006);

    expect(connection.rejections).toEqual([]);
    expect(connection.events.map((event) => event.type)).toEqual(["snapshot"]);
  });

  test("4403 с другой причиной и без кадра kicked — прежний отказ «not-a-member»", () => {
    const connection = connect();
    connection.last().open();

    connection.last().drop(4403, "not a member");

    expect(connection.rejections).toEqual(["not-a-member"]);
  });

  test("отказ без обработчика не ломает клиента", () => {
    const { sockets, last } = fakeWebSocket();
    openConnections.push(connectGame("g1", TOKEN, () => {}));
    last().open();

    expect(() => last().drop(4401)).not.toThrow();
    vi.advanceTimersByTime(60 * 1000);
    expect(sockets).toHaveLength(1);
  });
});

describe("connectGame: ping", () => {
  const PING = JSON.stringify({ type: "ping" });

  test("каждые 25 с после открытия, и не раньше", () => {
    const { last } = connect();
    last().open();

    vi.advanceTimersByTime(24_999);
    expect(last().sent).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(last().sent).toEqual([PING]);
    last().serve({ type: "pong" });
    vi.advanceTimersByTime(25_000);
    expect(last().sent).toEqual([PING, PING]);
    last().serve({ type: "pong" });
    vi.advanceTimersByTime(25_000);
    expect(last().sent).toEqual([PING, PING, PING]);
  });

  test("пока сокет не открыт, ping не шлётся", () => {
    const { last } = connect();

    expect(() => vi.advanceTimersByTime(100_000)).not.toThrow();
    expect(last().sent).toEqual([]);
  });

  test("после обрыва ping прекращается, на новом сокете начинается заново", () => {
    const connection = connect();
    const first = connection.last();
    first.open();
    vi.advanceTimersByTime(25_000);
    expect(first.sent).toEqual([PING]);

    first.drop(1006);
    expect(vi.getTimerCount()).toBe(1); // только ожидание переподключения
    vi.advanceTimersByTime(100_000);
    expect(first.sent).toEqual([PING]);

    const second = connection.last();
    second.open();
    vi.advanceTimersByTime(24_999);
    expect(second.sent).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(second.sent).toEqual([PING]);
  });

  test("после отказа 4401 ping не идёт", () => {
    const { last } = connect();
    const socket = last();
    socket.open();

    socket.drop(4401);
    vi.advanceTimersByTime(100_000);

    expect(socket.sent).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("connectGame: ожидание pong", () => {
  const PING = JSON.stringify({ type: "ping" });
  const PONG_WAIT = 10_000;

  /** Открытый сокет, которому только что отправили ping. */
  function pinged() {
    const connection = connect();
    const socket = connection.last();
    socket.open();
    vi.advanceTimersByTime(25_000);
    expect(socket.sent).toEqual([PING]);
    return { connection, socket };
  }

  test("pong вовремя: соединение живёт, нового сокета нет", () => {
    const { connection, socket } = pinged();

    vi.advanceTimersByTime(PONG_WAIT - 1);
    socket.serve({ type: "pong" });
    vi.advanceTimersByTime(PONG_WAIT);

    expect(connection.sockets).toHaveLength(1);
    expect(connection.statuses).toEqual(["open"]);
    expect(socket.clientCloseCode).toBeNull();
  });

  test("за 10 с без ответа ещё ждём, не раньше", () => {
    const { connection } = pinged();

    vi.advanceTimersByTime(PONG_WAIT - 1);

    expect(connection.statuses).toEqual(["open"]);
  });

  test("pong не пришёл за 10 с: сокет закрыт, статус «переподключение», через 1 с новый сокет", () => {
    const { connection, socket } = pinged();

    vi.advanceTimersByTime(PONG_WAIT);

    expect(socket.clientCloseCode).not.toBeNull();
    expect(connection.statuses).toEqual(["open", "reconnecting"]);
    expect(connection.sockets).toHaveLength(1);
    vi.advanceTimersByTime(999);
    expect(connection.sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(connection.sockets).toHaveLength(2);
    expect(connection.last().url).toBe(gameSocketUrl("g1", TOKEN));
  });

  test("зависшее соединение не отказ: обработчик отказа не вызывается", () => {
    const { connection } = pinged();

    vi.advanceTimersByTime(PONG_WAIT + 60_000);

    expect(connection.rejections).toEqual([]);
  });

  test("второй ping (проверка по online) срок первого не продлевает", () => {
    const { connection, socket } = pinged(); // первый ping в 25 с, срок до 35 с

    vi.advanceTimersByTime(5000);
    window.dispatchEvent(new Event("online")); // второй ping в 30 с, без ответа
    expect(socket.sent).toEqual([PING, PING]);
    vi.advanceTimersByTime(PONG_WAIT - 5000 - 1);
    expect(connection.statuses).toEqual(["open"]);
    vi.advanceTimersByTime(1);

    expect(connection.statuses).toEqual(["open", "reconnecting"]);
    // Зависание обработано один раз: здоровый новый сокет второй срок не трогает.
    vi.advanceTimersByTime(1000);
    connection.last().open();
    connection.last().serve(SNAPSHOT);
    vi.advanceTimersByTime(10_000);
    expect(connection.sockets).toHaveLength(2);
    expect(connection.statuses).toEqual(["open", "reconnecting", "open"]);
  });

  test("любой кадр сервера, не только pong, говорит, что связь жива", () => {
    const { connection, socket } = pinged();

    vi.advanceTimersByTime(PONG_WAIT - 1);
    socket.serve({ type: "party", party: [] });
    vi.advanceTimersByTime(PONG_WAIT);

    expect(connection.sockets).toHaveLength(1);
    expect(connection.statuses).toEqual(["open"]);
  });

  test("закрытый как мёртвый сокет ничего не меняет, даже если очнётся", () => {
    const { connection, socket } = pinged();
    vi.advanceTimersByTime(PONG_WAIT);

    socket.serve(SNAPSHOT);
    socket.drop(1006);
    socket.drop(4401);
    vi.advanceTimersByTime(1000);

    expect(connection.events).toEqual([]);
    expect(connection.rejections).toEqual([]);
    expect(connection.statuses).toEqual(["open", "reconnecting"]);
    expect(connection.sockets).toHaveLength(2);
  });

  test("повторные зависания растут по обычной лестнице задержек", () => {
    const { connection } = pinged();
    vi.advanceTimersByTime(PONG_WAIT);
    vi.advanceTimersByTime(1000);
    const second = connection.last();
    second.open(); // снимка нет: задержка не сбрасывается
    vi.advanceTimersByTime(25_000 + PONG_WAIT);

    expect(connection.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1999);
    expect(connection.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(connection.sockets).toHaveLength(3);
  });

  test("после снимка зависание снова начинается с 1 с", () => {
    const { connection } = pinged();
    vi.advanceTimersByTime(PONG_WAIT); // первое зависание: следующая задержка была бы 2 с
    vi.advanceTimersByTime(1000);
    const second = connection.last();
    second.open();
    second.serve(SNAPSHOT); // снимок сбросил лестницу задержек
    vi.advanceTimersByTime(25_000 + PONG_WAIT); // второе зависание

    vi.advanceTimersByTime(999);
    expect(connection.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(connection.sockets).toHaveLength(3);
  });

  test("обрыв сокета снимает ожидание pong: таймера не остаётся", () => {
    const { connection, socket } = pinged();

    socket.drop(1006);

    expect(vi.getTimerCount()).toBe(1); // только ожидание переподключения
    vi.advanceTimersByTime(PONG_WAIT);
    expect(connection.statuses).toEqual(["open", "reconnecting"]);
  });

  test("close() снимает ожидание pong", () => {
    const { connection, socket } = pinged();

    connection.close();

    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(PONG_WAIT * 3);
    expect(connection.sockets).toHaveLength(1);
    expect(socket.sent).toEqual([PING]);
  });
});

describe("connectGame: возврат связи (online и visibilitychange)", () => {
  const PING = JSON.stringify({ type: "ping" });

  function setVisibility(state: DocumentVisibilityState) {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => state,
    });
  }
  const becomeVisible = () => {
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
  };

  afterEach(() => {
    // Свойство задано на самом document: убираем, чтобы вернулось значение jsdom.
    Reflect.deleteProperty(document, "visibilityState");
  });

  test("online в ожидании повтора: новый сокет сразу, не дожидаясь задержки", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006);
    expect(connection.sockets).toHaveLength(1);

    window.dispatchEvent(new Event("online"));

    expect(connection.sockets).toHaveLength(2);
    vi.advanceTimersByTime(60_000);
    expect(connection.sockets).toHaveLength(2); // старый таймер повтора снят
  });

  test("сокет, открытый по online, живёт как обычный: события доходят", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006);
    window.dispatchEvent(new Event("online"));

    connection.last().open();
    connection.last().serve(SNAPSHOT);

    expect(connection.events).toHaveLength(1);
    expect(connection.statuses).toEqual(["open", "reconnecting", "open"]);
  });

  test("возврат на вкладку в ожидании повтора тоже открывает сокет сразу", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006);

    becomeVisible();

    expect(connection.sockets).toHaveLength(2);
  });

  test("быстрые повторы по online не сбрасывают лестницу задержек", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006); // задержка 1 с
    window.dispatchEvent(new Event("online"));
    connection.last().drop(1006); // второй обрыв подряд: 2 с

    vi.advanceTimersByTime(1999);
    expect(connection.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(connection.sockets).toHaveLength(3);
  });

  test("online при живом соединении: сразу ping, а без ответа за 10 с — переподключение", () => {
    const connection = connect();
    const socket = connection.last();
    socket.open();

    window.dispatchEvent(new Event("online"));

    expect(socket.sent).toEqual([PING]);
    expect(connection.sockets).toHaveLength(1);
    vi.advanceTimersByTime(10_000);
    expect(socket.clientCloseCode).not.toBeNull();
    expect(connection.statuses).toEqual(["open", "reconnecting"]);
  });

  test("возврат на вкладку при живом соединении: ping, и ответ pong оставляет связь", () => {
    const connection = connect();
    const socket = connection.last();
    socket.open();

    becomeVisible();
    expect(socket.sent).toEqual([PING]);
    socket.serve({ type: "pong" });
    vi.advanceTimersByTime(20_000); // до очередного ping по расписанию

    expect(connection.sockets).toHaveLength(1);
    expect(socket.clientCloseCode).toBeNull();
  });

  test("уход со вкладки ничего не делает", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006);

    setVisibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));

    expect(connection.sockets).toHaveLength(1);
    expect(connection.last().sent).toEqual([]);
  });

  test("пока сокет подключается, online не открывает второй", () => {
    const connection = connect();

    window.dispatchEvent(new Event("online"));
    becomeVisible();

    expect(connection.sockets).toHaveLength(1);
    expect(connection.last().sent).toEqual([]);
  });

  test("после отказа 4401 события возврата связи ничего не открывают", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(4401);

    window.dispatchEvent(new Event("online"));
    becomeVisible();

    expect(connection.sockets).toHaveLength(1);
  });

  test("после close() события возврата связи ничего не делают", () => {
    const connection = connect();
    connection.last().open();
    connection.last().drop(1006);
    connection.close();

    window.dispatchEvent(new Event("online"));
    becomeVisible();
    vi.advanceTimersByTime(60_000);

    expect(connection.sockets).toHaveLength(1);
  });

  test("close() отвязывает слушатели окна и документа", () => {
    const removed: string[] = [];
    vi.spyOn(window, "removeEventListener").mockImplementation((type) => {
      removed.push(String(type));
    });
    vi.spyOn(document, "removeEventListener").mockImplementation((type) => {
      removed.push(String(type));
    });
    const connection = connect();

    connection.close();

    expect(removed).toEqual(
      expect.arrayContaining(["online", "visibilitychange"]),
    );
  });
});

describe("connectGame: close()", () => {
  test("закрывает открытый сокет и останавливает ping", () => {
    const { last, close } = connect();
    const socket = last();
    socket.open();

    close();
    vi.advanceTimersByTime(100_000);

    expect(socket.clientCloseCode).not.toBeNull();
    expect(socket.sent).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  test("отменяет ожидающее переподключение", () => {
    const { sockets, last, close } = connect();
    last().open();
    last().drop(1006);

    close();
    vi.advanceTimersByTime(60_000);

    expect(sockets).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  test("закрывает и сокет, который ещё подключается", () => {
    const { sockets, last, close } = connect();
    const socket = last();

    close();
    vi.advanceTimersByTime(60_000);

    expect(socket.clientCloseCode).not.toBeNull();
    expect(sockets).toHaveLength(1);
  });

  test("поздние кадры и обрыв закрытого сокета ничего не делают", () => {
    const { sockets, last, events, statuses, rejections, close } = connect();
    const socket = last();
    socket.open();
    close();

    socket.serve(SNAPSHOT);
    socket.drop(4401);
    socket.drop(1006);
    vi.advanceTimersByTime(60_000);

    expect(events).toEqual([]);
    expect(rejections).toEqual([]);
    expect(statuses).toEqual(["open"]);
    expect(sockets).toHaveLength(1);
  });

  test("повторный вызов безопасен", () => {
    const { last, close } = connect();
    last().open();

    close();

    expect(() => close()).not.toThrow();
  });
});
