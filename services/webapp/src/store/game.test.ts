import type {
  Character,
  CombatView,
  FeedItem,
  GameSummary,
  PartyMember,
  PhaseView,
  PlayerAction,
  Snapshot,
  WsEvent,
} from "../api/types";
import { fakeWebSocket } from "../test-setup";
import {
  MAX_FEED_ITEMS,
  NO_LIVE,
  applyAction,
  applyEvent,
  followGame,
  gameStage,
  leavingGame,
  secondsLeft,
  serverNow,
  useGame,
} from "./game";
import type { LiveState } from "./game";

// --- данные ---

const SERVER_TIME = "2026-10-20T12:00:00+00:00";
const SERVER_MS = Date.parse(SERVER_TIME);
const MINUTE = 60_000;

function gameSummary(overrides: Partial<GameSummary> = {}): GameSummary {
  return {
    id: "g1",
    code: "ABC123",
    adventure_id: "demo-crypt",
    adventure_title: "Склеп",
    phase: "playing",
    turn_phase: "collecting",
    provider: null,
    model: null,
    collect_seconds: 120,
    hurry_seconds: 60,
    camp_seconds: 300,
    profanity: true,
    players: [
      {
        user_id: "u1",
        name: "Вика",
        is_host: true,
        ready: true,
        has_character: true,
      },
    ],
    ...overrides,
  };
}

function phaseView(overrides: Partial<PhaseView> = {}): PhaseView {
  return {
    phase: "playing",
    turn_phase: "collecting",
    deadline_at: "2026-10-20T12:02:00+00:00", // через 120 с по серверу
    remaining_seconds: null,
    scene: { type: "exploration", day: 1, part: "morning" },
    ...overrides,
  };
}

function feedItem(id: number, text = `запись ${id}`): FeedItem {
  return {
    id,
    kind: "narration",
    text,
    data: null,
    private: false,
    created_at: SERVER_TIME,
  };
}

function member(id: string, hp = 10): PartyMember {
  return {
    id,
    name: `Герой ${id}`,
    player_id: "u1",
    hp,
    hp_max: 12,
    temp_hp: 0,
    conditions: [],
    effects: [],
  };
}

function hero(overrides: Partial<Character> = {}): Character {
  return {
    id: "c1",
    name: "Лира",
    level: 1,
    species: "human",
    class_id: "rogue",
    background: "urchin",
    scores: { str: 10, dex: 16, con: 12, int: 12, wis: 10, cha: 14 },
    skill_proficiencies: [],
    expertise: [],
    save_proficiencies: [],
    fighting_style: null,
    hp_max: 10,
    hp: 10,
    temp_hp: 0,
    hit_dice_max: 1,
    hit_dice: 1,
    spell_slots_max: {},
    spell_slots: {},
    cantrips: [],
    prepared_spells: [],
    concentration: null,
    inventory: [],
    conditions: [],
    buffs: [],
    resources: {},
    inspiration: false,
    death_saves: { successes: 0, failures: 0, stable: false },
    persona: {
      concept: "",
      appearance: "",
      traits: "",
      ideals: "",
      bonds: "",
      flaws: "",
      backstory: "",
    },
    player_id: "u1",
    ...overrides,
  };
}

function combatView(round = 1): CombatView {
  return {
    round,
    current_id: "pc-1",
    order: [],
    zones: [{ id: "z1", name: "Зал", combatants: [] }],
  };
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    type: "snapshot",
    server_time: SERVER_TIME,
    game: gameSummary(),
    host_user_id: "u1",
    phase: phaseView(),
    statuses: { u1: "writing", u2: "ready" },
    character: hero(),
    action: null,
    party: [member("c1")],
    combat: null,
    feed: [feedItem(1), feedItem(2)],
    ...overrides,
  };
}

/** Состояние после снимка; часы браузера совпадают с серверными. */
function started(overrides: Partial<Snapshot> = {}): LiveState {
  return applyEvent(NO_LIVE, snapshot(overrides), SERVER_MS);
}

/** Применить события по порядку к состоянию после снимка. */
function after(...events: WsEvent[]): LiveState {
  return events.reduce(
    (state, event) => applyEvent(state, event, SERVER_MS),
    started(),
  );
}

function viewOf(state: LiveState) {
  if (state.view === null) throw new Error("снимка ещё не было");
  return state.view;
}

// --- редьюсер ---

describe("applyEvent: снимок", () => {
  test("заменяет состояние игры целиком, а не дополняет его", () => {
    const old = applyEvent(
      NO_LIVE,
      snapshot({
        game: gameSummary({ id: "old", code: "OLD111" }),
        feed: [feedItem(9, "старое"), feedItem(10)],
        party: [member("old-hero")],
        combat: combatView(3),
        statuses: { gone: "pass" },
      }),
      SERVER_MS,
    );

    const fresh = snapshot({
      feed: [feedItem(1)],
      party: [member("c1")],
      combat: null,
      statuses: { u1: "ready" },
    });
    const state = applyEvent(old, fresh, SERVER_MS);

    expect(state.view).toEqual({
      game: fresh.game,
      host_user_id: fresh.host_user_id,
      phase: fresh.phase,
      statuses: { u1: "ready" },
      character: fresh.character,
      action: null,
      party: fresh.party,
      combat: null,
      feed: [feedItem(1)],
    });
  });

  test("в состояние не попадают служебные поля кадра", () => {
    const view = viewOf(started());

    expect(view).not.toHaveProperty("type");
    expect(view).not.toHaveProperty("server_time");
  });

  test("снимок после обрыва убирает прежнюю ошибку", () => {
    const failed = applyEvent(
      started(),
      { type: "error", detail: "Не удалось загрузить игру." },
      SERVER_MS,
    );

    const state = applyEvent(failed, snapshot(), SERVER_MS);

    expect(state.error).toBeNull();
  });

  test("ход игрока и лист героя берутся из снимка как есть", () => {
    const action = {
      text: "Иду к двери",
      secret: false,
      inspiration: false,
      status: "draft" as const,
    };

    const view = viewOf(started({ action, character: null }));

    expect(view.action).toEqual(action);
    expect(view.character).toBeNull();
  });
});

describe("applyEvent: лента", () => {
  test("feed_item добавляет запись в конец", () => {
    const state = after({ type: "feed_item", item: feedItem(3) });

    expect(viewOf(state).feed.map((item) => item.id)).toEqual([1, 2, 3]);
  });

  test("запись с тем же id не добавляется второй раз", () => {
    const state = after(
      { type: "feed_item", item: feedItem(3) },
      { type: "feed_item", item: feedItem(3) },
      // Событие, пришедшее вслед за снимком, уже могло войти в его ленту.
      { type: "feed_item", item: feedItem(2, "повтор") },
    );

    const feed = viewOf(state).feed;
    expect(feed.map((item) => item.id)).toEqual([1, 2, 3]);
    expect(feed[1].text).toBe("запись 2");
  });

  test("надгробие {id, superseded} убирает запись по id", () => {
    const state = after({
      type: "feed_item",
      item: { id: 1, superseded: true },
    });

    expect(viewOf(state).feed.map((item) => item.id)).toEqual([2]);
  });

  test("надгробие убирает и запись, пришедшую событием, не трогая остальные", () => {
    const state = after(
      { type: "feed_item", item: feedItem(3) },
      { type: "feed_item", item: feedItem(4) },
      { type: "feed_item", item: { id: 3, superseded: true } },
    );

    expect(viewOf(state).feed.map((item) => item.id)).toEqual([1, 2, 4]);
  });

  test("надгробие записи, которой нет в ленте, ничего не меняет", () => {
    const before = started();

    const state = applyEvent(
      before,
      { type: "feed_item", item: { id: 99, superseded: true } },
      SERVER_MS,
    );

    expect(state).toEqual(before);
  });
});

describe("applyEvent: фаза и отсчёт", () => {
  test("phase обновляет срок и фазу хода", () => {
    const state = after({
      type: "phase",
      phase: phaseView({
        turn_phase: "resolving",
        deadline_at: "2026-10-20T12:03:00+00:00",
      }),
    });

    expect(viewOf(state).phase).toMatchObject({
      turn_phase: "resolving",
      deadline_at: "2026-10-20T12:03:00+00:00",
    });
  });

  test("пауза: срока нет, остаток стоит на месте", () => {
    const state = after({
      type: "phase",
      phase: phaseView({
        phase: "paused",
        deadline_at: null,
        remaining_seconds: 75,
      }),
    });

    const phase = viewOf(state).phase;
    expect(phase.phase).toBe("paused");
    expect(phase.deadline_at).toBeNull();
    expect(phase.remaining_seconds).toBe(75);
  });

  test("после «Продолжить» приходит новый срок, остатка нет", () => {
    const paused = after({
      type: "phase",
      phase: phaseView({
        phase: "paused",
        deadline_at: null,
        remaining_seconds: 75,
      }),
    });

    const state = applyEvent(
      paused,
      {
        type: "phase",
        phase: phaseView({
          deadline_at: "2026-10-20T12:01:15+00:00",
          remaining_seconds: null,
        }),
      },
      SERVER_MS,
    );

    expect(viewOf(state).phase).toMatchObject({
      phase: "playing",
      deadline_at: "2026-10-20T12:01:15+00:00",
      remaining_seconds: null,
    });
  });

  test("phase заменяет блок целиком: прежняя сцена не остаётся", () => {
    const state = after({ type: "phase", phase: phaseView({ scene: null }) });

    expect(viewOf(state).phase.scene).toBeNull();
  });
});

describe("applyEvent: остальные блоки", () => {
  test("player_status меняет статус одного игрока и добавляет нового", () => {
    const state = after(
      { type: "player_status", user_id: "u1", status: "ready" },
      { type: "player_status", user_id: "u3", status: "pass" },
    );

    expect(viewOf(state).statuses).toEqual({
      u1: "ready",
      u2: "ready",
      u3: "pass",
    });
  });

  test("party заменяет отряд целиком", () => {
    const state = after({
      type: "party",
      party: [member("c1", 4), member("c2", 9)],
    });

    expect(viewOf(state).party.map((hero) => [hero.id, hero.hp])).toEqual([
      ["c1", 4],
      ["c2", 9],
    ]);
  });

  test("combat заменяет бой, а null означает конец боя", () => {
    const fighting = after({ type: "combat", combat: combatView(2) });
    expect(viewOf(fighting).combat?.round).toBe(2);

    const over = applyEvent(
      fighting,
      { type: "combat", combat: null },
      SERVER_MS,
    );
    expect(viewOf(over).combat).toBeNull();
  });

  test("character заменяет лист героя", () => {
    const state = after({
      type: "character",
      character: hero({ hp: 3, name: "Лира Быстрая" }),
    });

    expect(viewOf(state).character).toMatchObject({
      hp: 3,
      name: "Лира Быстрая",
    });
  });

  test("lobby заменяет сводку игры и не трогает остальные блоки", () => {
    const before = started();
    const summary = gameSummary({
      players: [
        {
          user_id: "u1",
          name: "Вика",
          is_host: true,
          ready: true,
          has_character: true,
        },
        {
          user_id: "u2",
          name: "Саша",
          is_host: false,
          ready: false,
          has_character: false,
        },
      ],
    });

    const state = applyEvent(
      before,
      { type: "lobby", game: summary },
      SERVER_MS,
    );

    expect(viewOf(state).game).toEqual(summary);
    expect({ ...viewOf(state), game: null }).toEqual({
      ...viewOf(before),
      game: null,
    });
  });
});

describe("applyEvent: ошибки и лишнее", () => {
  test("error попадает в состояние ошибки и не стирает игру", () => {
    const before = started();

    const state = applyEvent(
      before,
      { type: "error", detail: "Не удалось загрузить игру." },
      SERVER_MS,
    );

    expect(state.error).toBe("Не удалось загрузить игру.");
    expect(state.view).toEqual(before.view);
  });

  test("error до первого снимка: игры ещё нет, ошибка есть", () => {
    const state = applyEvent(
      NO_LIVE,
      { type: "error", detail: "Не удалось загрузить игру." },
      SERVER_MS,
    );

    expect(state).toEqual({
      ...NO_LIVE,
      error: "Не удалось загрузить игру.",
    });
  });

  test("события блоков до первого снимка игнорируются: применять их не к чему", () => {
    const events: WsEvent[] = [
      { type: "phase", phase: phaseView() },
      { type: "player_status", user_id: "u1", status: "ready" },
      { type: "feed_item", item: feedItem(1) },
      { type: "feed_item", item: { id: 1, superseded: true } },
      { type: "party", party: [member("c1")] },
      { type: "combat", combat: combatView() },
      { type: "character", character: hero() },
      { type: "lobby", game: gameSummary() },
    ];

    for (const event of events) {
      expect(applyEvent(NO_LIVE, event, SERVER_MS)).toEqual(NO_LIVE);
    }
  });

  test("pong и неизвестное событие состояние не меняют", () => {
    const before = started();

    const events = [
      { type: "pong" },
      { type: "from_the_future", payload: 1 },
    ] as unknown as WsEvent[];
    for (const event of events) {
      expect(applyEvent(before, event, SERVER_MS)).toEqual(before);
    }
  });

  test("прежнее состояние не изменяется: редьюсер чистый", () => {
    const deepFreeze = <T>(value: T): T => {
      if (typeof value === "object" && value !== null) {
        Object.values(value).forEach(deepFreeze);
        Object.freeze(value);
      }
      return value;
    };
    const frozen = deepFreeze(started());
    const events: WsEvent[] = [
      { type: "phase", phase: phaseView({ deadline_at: null }) },
      { type: "player_status", user_id: "u1", status: "pass" },
      { type: "feed_item", item: feedItem(3) },
      { type: "feed_item", item: { id: 1, superseded: true } },
      { type: "party", party: [] },
      { type: "combat", combat: combatView() },
      { type: "character", character: hero({ hp: 1 }) },
      { type: "lobby", game: gameSummary({ code: "ZZZ999" }) },
      { type: "error", detail: "Сбой." },
      snapshot(),
    ];

    for (const event of events) {
      expect(() => applyEvent(frozen, event, SERVER_MS)).not.toThrow();
    }
  });
});

// --- часы сервера и отсчёты ---

describe("часы сервера", () => {
  test("сдвиг — серверное время снимка минус часы браузера в момент его получения", () => {
    // Часы телефона отстают от сервера на 3 минуты.
    const receivedAt = SERVER_MS - 3 * MINUTE;

    const state = applyEvent(NO_LIVE, snapshot(), receivedAt);

    expect(state.clockOffsetMs).toBe(3 * MINUTE);
  });

  test("serverNow: часы браузера плюс сдвиг", () => {
    expect(serverNow(3 * MINUTE, 1_000_000)).toBe(1_000_000 + 3 * MINUTE);
    expect(serverNow(-2 * MINUTE, 1_000_000)).toBe(1_000_000 - 2 * MINUTE);
  });

  test("serverNow без второго аргумента берёт текущие часы браузера", () => {
    vi.useFakeTimers();
    vi.setSystemTime(SERVER_MS);

    expect(serverNow(5_000)).toBe(SERVER_MS + 5_000);
  });

  test("новый снимок пересчитывает сдвиг: после обрыва часы могли разойтись иначе", () => {
    const first = applyEvent(NO_LIVE, snapshot(), SERVER_MS - 3 * MINUTE);

    const second = applyEvent(first, snapshot(), SERVER_MS + 10_000);

    expect(second.clockOffsetMs).toBe(-10_000);
  });

  test("события блоков сдвиг не меняют", () => {
    const first = applyEvent(NO_LIVE, snapshot(), SERVER_MS - 3 * MINUTE);

    const state = applyEvent(
      first,
      { type: "phase", phase: phaseView() },
      SERVER_MS + 99 * MINUTE,
    );

    expect(state.clockOffsetMs).toBe(3 * MINUTE);
  });
});

describe("secondsLeft: сколько осталось до конца отсчёта", () => {
  const deadline = phaseView({ deadline_at: "2026-10-20T12:02:00+00:00" });

  test("считает от серверного времени: отстающие часы телефона не удлиняют отсчёт", () => {
    const receivedAt = SERVER_MS - 3 * MINUTE;
    const { clockOffsetMs } = applyEvent(NO_LIVE, snapshot(), receivedAt);

    // Срок через 120 с по серверу, и телефон показывает то же самое.
    expect(secondsLeft(deadline, clockOffsetMs, receivedAt)).toBe(120);
    expect(secondsLeft(deadline, clockOffsetMs, receivedAt + 30_000)).toBe(90);
  });

  test("спешащие часы телефона не сокращают отсчёт", () => {
    const receivedAt = SERVER_MS + 7 * MINUTE;
    const { clockOffsetMs } = applyEvent(NO_LIVE, snapshot(), receivedAt);

    expect(secondsLeft(deadline, clockOffsetMs, receivedAt + 60_000)).toBe(60);
  });

  test("дробные секунды округляются вверх, как остаток на паузе у сервера", () => {
    expect(secondsLeft(deadline, 0, SERVER_MS + 119_001)).toBe(1);
    expect(secondsLeft(deadline, 0, SERVER_MS + 119_999)).toBe(1);
    expect(secondsLeft(deadline, 0, SERVER_MS + 120_000)).toBe(0);
  });

  test("после срока — ноль, а не отрицательное число", () => {
    expect(secondsLeft(deadline, 0, SERVER_MS + 5 * MINUTE)).toBe(0);
  });

  test("на паузе остаток стоит, сколько бы времени ни прошло", () => {
    const paused = phaseView({
      phase: "paused",
      deadline_at: null,
      remaining_seconds: 75,
    });

    expect(secondsLeft(paused, 3 * MINUTE, SERVER_MS)).toBe(75);
    expect(secondsLeft(paused, 3 * MINUTE, SERVER_MS + 10 * MINUTE)).toBe(75);
  });

  test("отсчёта нет: ни срока, ни остатка", () => {
    const none = phaseView({
      deadline_at: null,
      remaining_seconds: null,
      turn_phase: "resolving",
    });

    expect(secondsLeft(none, 0, SERVER_MS)).toBeNull();
  });

  test("без явных часов берёт текущее время браузера", () => {
    vi.useFakeTimers();
    vi.setSystemTime(SERVER_MS + 20_000);

    expect(secondsLeft(deadline, 0)).toBe(100);
  });
});

// --- хранилище и подключение ---

const SESSION = {
  token: "tok-secret-123",
  user: { id: "u1", name: "Вика", can_create_games: true },
};

function signedIn() {
  useGame.getState().setSession(SESSION);
  useGame.getState().show({ screen: "game", gameId: "g1" });
}

describe("хранилище: экран создания героя", () => {
  test("экран create помнит игру, для которой создаётся герой", () => {
    useGame.getState().setSession(SESSION);

    useGame.getState().show({ screen: "create", gameId: "g1" });

    expect(useGame.getState()).toMatchObject({
      screen: "create",
      gameId: "g1",
    });
  });

  test("возврат в лобби не теряет игру", () => {
    useGame.getState().setSession(SESSION);
    useGame.getState().show({ screen: "create", gameId: "g1" });

    useGame.getState().show({ screen: "lobby", gameId: "g1" });

    expect(useGame.getState()).toMatchObject({
      screen: "lobby",
      gameId: "g1",
    });
  });

  test("без сессии экран создания героя не открывается", () => {
    useGame.getState().show({ screen: "create", gameId: "g1" });

    expect(useGame.getState()).toMatchObject({ screen: "login", gameId: null });
  });
});

describe("хранилище: события сокета", () => {
  test("в начале игры нет, соединения нет", () => {
    const state = useGame.getState();

    expect(state.live).toEqual(NO_LIVE);
    expect(state.connection).toBe("idle");
  });

  test("receive применяет событие к живому состоянию", () => {
    vi.useFakeTimers();
    vi.setSystemTime(SERVER_MS - MINUTE);

    useGame.getState().receive(snapshot());
    useGame.getState().receive({ type: "feed_item", item: feedItem(3) });

    const { live } = useGame.getState();
    expect(viewOf(live).feed.map((item) => item.id)).toEqual([1, 2, 3]);
    expect(live.clockOffsetMs).toBe(MINUTE);
  });

  test("выход из аккаунта сбрасывает живую игру", () => {
    signedIn();
    useGame.getState().receive(snapshot());

    useGame.getState().signOut();

    expect(useGame.getState().live).toEqual(NO_LIVE);
    expect(useGame.getState().connection).toBe("idle");
  });
});

describe("followGame: игра из сокета в хранилище", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(SERVER_MS);
  });

  test("открывает сокет игры с токеном сессии и ждёт соединения", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();

    const stop = followGame("g1");

    expect(sockets).toHaveLength(1);
    expect(last().url).toContain("/ws/games/g1?token=tok-secret-123");
    expect(useGame.getState().connection).toBe("connecting");
    stop();
  });

  test("события сервера попадают в хранилище, статус следует за сокетом", () => {
    const { last } = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");

    last().open();
    expect(useGame.getState().connection).toBe("open");
    last().serve(snapshot());
    last().serve({ type: "feed_item", item: feedItem(3) });
    last().serve({
      type: "phase",
      phase: phaseView({
        phase: "paused",
        deadline_at: null,
        remaining_seconds: 40,
      }),
    });

    const { live } = useGame.getState();
    expect(viewOf(live).feed.map((item) => item.id)).toEqual([1, 2, 3]);
    expect(viewOf(live).phase.remaining_seconds).toBe(40);
    stop();
  });

  test("без сессии сокет не открывается, а stop безопасен", () => {
    const { sockets } = fakeWebSocket();

    const stop = followGame("g1");

    expect(sockets).toHaveLength(0);
    expect(() => stop()).not.toThrow();
    expect(useGame.getState().connection).toBe("idle");
  });

  test("обрыв: статус «переподключение», игра остаётся, новый снимок заменяет её целиком", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");
    last().open();
    last().serve(snapshot({ feed: [feedItem(1), feedItem(2)] }));

    last().drop(1006);
    expect(useGame.getState().connection).toBe("reconnecting");
    expect(viewOf(useGame.getState().live).feed).toHaveLength(2);

    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
    last().open();
    last().serve(
      snapshot({ feed: [feedItem(2), feedItem(5, "после обрыва")] }),
    );

    expect(useGame.getState().connection).toBe("open");
    expect(viewOf(useGame.getState().live).feed.map((item) => item.id)).toEqual(
      [2, 5],
    );
    stop();
  });

  test("ошибка снимка (1011): текст виден, потом сокет подключается снова", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");
    last().open();

    last().serve({ type: "error", detail: "Не удалось загрузить игру." });
    last().drop(1011);
    expect(useGame.getState().live.error).toBe("Не удалось загрузить игру.");

    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
    last().open();
    last().serve(snapshot());
    expect(useGame.getState().live.error).toBeNull();
    stop();
  });

  test("4401: выход на экран входа без повторных подключений", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();
    followGame("g1");
    last().open();
    last().serve(snapshot());

    last().drop(4401);
    vi.advanceTimersByTime(10 * MINUTE);

    const state = useGame.getState();
    expect(state.session).toBeNull();
    expect(state.screen).toBe("login");
    expect(state.live).toEqual(NO_LIVE);
    expect(state.connection).toBe("idle");
    expect(sockets).toHaveLength(1);
  });

  test("4403: «Игра недоступна» на экране «Мои игры», сессия цела, без повторов", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();
    followGame("g1");
    last().open();
    last().serve(snapshot());

    last().drop(4403);
    vi.advanceTimersByTime(10 * MINUTE);

    const state = useGame.getState();
    expect(state.session).toEqual(SESSION);
    expect(state.screen).toBe("games");
    expect(state.gameId).toBeNull();
    expect(state.notice).toBe("Игра недоступна.");
    expect(state.live).toEqual(NO_LIVE);
    expect(state.connection).toBe("idle");
    expect(sockets).toHaveLength(1);
  });

  test("stop закрывает сокет, сбрасывает игру и глушит поздние события", () => {
    const { sockets, last } = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");
    const socket = last();
    socket.open();
    socket.serve(snapshot());

    stop();
    socket.serve({ type: "feed_item", item: feedItem(7) });
    socket.drop(1006);
    vi.advanceTimersByTime(MINUTE);

    expect(socket.clientCloseCode).not.toBeNull();
    expect(useGame.getState().live).toEqual(NO_LIVE);
    expect(useGame.getState().connection).toBe("idle");
    expect(sockets).toHaveLength(1);
  });

  test("новая игра закрывает прежнюю, а запоздалый stop прежней новую не трогает", () => {
    const { sockets } = fakeWebSocket();
    signedIn();
    const stopFirst = followGame("g1");
    sockets[0].open();
    sockets[0].serve(snapshot());

    const stopSecond = followGame("g2");
    sockets[1].open();
    sockets[1].serve(snapshot({ game: gameSummary({ id: "g2" }) }));
    stopFirst(); // например, очистка эффекта React после запуска нового

    expect(sockets[0].clientCloseCode).not.toBeNull();
    expect(sockets[1].clientCloseCode).toBeNull();
    expect(viewOf(useGame.getState().live).game.id).toBe("g2");
    expect(useGame.getState().connection).toBe("open");
    stopSecond();
  });

  test("токен остаётся только в адресе сокета: ни в журнале, ни в хранилищах браузера", () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const { last } = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");
    last().open();
    last().serve("мусор вместо JSON");
    last().drop(1006);
    vi.advanceTimersByTime(1000);
    last().drop(4403);

    const output = JSON.stringify([
      ...log.mock.calls,
      ...warn.mock.calls,
      ...error.mock.calls,
    ]);
    expect(output).not.toContain(SESSION.token);
    const stored = [
      ...Object.entries(window.localStorage),
      ...Object.entries(window.sessionStorage),
    ];
    expect(JSON.stringify(stored)).not.toContain(SESSION.token);
    stop();
  });
});

// --- ход игрока, смена хода и предел ленты ---

const draftAction = (overrides: Partial<PlayerAction> = {}): PlayerAction => ({
  text: "Крадусь к двери",
  secret: false,
  inspiration: false,
  status: "draft",
  ...overrides,
});

const phaseEvent = (turn_phase: PhaseView["turn_phase"]): WsEvent => ({
  type: "phase",
  phase: phaseView({ turn_phase, deadline_at: null }),
});

describe("applyEvent: лента в памяти ограничена", () => {
  test("после предела старые записи уходят, новые остаются в порядке прихода", () => {
    const events: WsEvent[] = Array.from(
      { length: MAX_FEED_ITEMS + 50 },
      (_, index) => ({ type: "feed_item", item: feedItem(100 + index) }),
    );

    const feed = viewOf(after(...events)).feed;

    expect(feed).toHaveLength(MAX_FEED_ITEMS);
    expect(feed.at(-1)?.id).toBe(100 + MAX_FEED_ITEMS + 49);
    expect(feed[0].id).toBe(150); // две записи снимка и 50 первых событий ушли
  });

  test("в пределах лимита ничего не теряется", () => {
    const feed = viewOf(after({ type: "feed_item", item: feedItem(3) })).feed;

    expect(feed.map((item) => item.id)).toEqual([1, 2, 3]);
  });
});

describe("applyEvent: новый ход начинается без хода игрока", () => {
  const withAction = (status: PlayerAction["status"] = "submitted") =>
    started({ action: draftAction({ status }) });

  test("в начале счётчик ходов нулевой, снимок его не меняет", () => {
    expect(started().turn).toBe(0);
    const later = applyEvent(
      after(phaseEvent("resolving"), phaseEvent("discussion")),
      snapshot(),
      SERVER_MS,
    );
    expect(later.turn).toBe(1);
  });

  test("мастер закончил (resolving → discussion): ход игрока сброшен, счётчик вырос", () => {
    const state = [phaseEvent("resolving"), phaseEvent("discussion")].reduce(
      (current, event) => applyEvent(current, event, SERVER_MS),
      withAction(),
    );

    expect(viewOf(state).action).toBeNull();
    expect(state.turn).toBe(1);
  });

  test.each(["discussion", "combat_wait", "camp"] as const)(
    "после resolving фаза %s — это новый ход",
    (next) => {
      const state = [phaseEvent("resolving"), phaseEvent(next)].reduce(
        (current, event) => applyEvent(current, event, SERVER_MS),
        withAction(),
      );

      expect(viewOf(state).action).toBeNull();
      expect(state.turn).toBe(1);
    },
  );

  test("пока мастер думает, ход игрока ещё виден", () => {
    const state = applyEvent(withAction(), phaseEvent("resolving"), SERVER_MS);

    expect(viewOf(state).action?.text).toBe("Крадусь к двери");
    expect(state.turn).toBe(0);
  });

  test("сбой мастера (resolving → dm_failed) ход не завершает: ходы сохранены", () => {
    const state = [phaseEvent("resolving"), phaseEvent("dm_failed")].reduce(
      (current, event) => applyEvent(current, event, SERVER_MS),
      withAction(),
    );

    expect(viewOf(state).action?.status).toBe("submitted");
    expect(state.turn).toBe(0);
  });

  test("«Повторить»: dm_failed → resolving → discussion — всё же один новый ход", () => {
    const state = [
      phaseEvent("resolving"),
      phaseEvent("dm_failed"),
      phaseEvent("resolving"),
      phaseEvent("discussion"),
    ].reduce(
      (current, event) => applyEvent(current, event, SERVER_MS),
      withAction(),
    );

    expect(viewOf(state).action).toBeNull();
    expect(state.turn).toBe(1);
  });

  test("обычные смены фазы (обсуждение → сбор) ход игрока не трогают", () => {
    const state = [phaseEvent("collecting"), phaseEvent("discussion")].reduce(
      (current, event) => applyEvent(current, event, SERVER_MS),
      withAction("draft"),
    );

    expect(viewOf(state).action?.status).toBe("draft");
    expect(state.turn).toBe(0);
  });

  test("начало игры (weaving: resolving → discussion) тоже открывает новый ход", () => {
    const opening = started({
      phase: phaseView({ phase: "weaving", turn_phase: "resolving" }),
    });

    const state = applyEvent(opening, phaseEvent("discussion"), SERVER_MS);

    expect(state.turn).toBe(1);
  });

  test("прежнее состояние не изменяется", () => {
    const before = withAction();
    const frozen = structuredClone(before);

    applyEvent(
      applyEvent(before, phaseEvent("resolving"), SERVER_MS),
      phaseEvent("discussion"),
      SERVER_MS,
    );

    expect(before).toEqual(frozen);
  });
});

describe("applyAction: ответ сервера на черновик, «Готов» и «Пас»", () => {
  test("кладёт ход игрока в игру", () => {
    const state = applyAction(started(), draftAction({ status: "submitted" }));

    expect(viewOf(state).action).toEqual(draftAction({ status: "submitted" }));
  });

  test("не трогает остальные блоки и счётчик ходов", () => {
    const base = started();

    const state = applyAction(base, draftAction());

    expect(viewOf(state).feed).toEqual(viewOf(base).feed);
    expect(viewOf(state).phase).toEqual(viewOf(base).phase);
    expect(state.turn).toBe(base.turn);
  });

  test("до первого снимка применять не к чему", () => {
    expect(applyAction(NO_LIVE, draftAction())).toBe(NO_LIVE);
  });

  test("в хранилище: setAction обновляет игру на экране", () => {
    useGame.getState().receive(snapshot());

    useGame.getState().setAction(draftAction({ status: "pass" }));

    expect(viewOf(useGame.getState().live).action?.status).toBe("pass");
  });
});

describe("gameStage: на каком экране должна быть игра", () => {
  const view = (game: GameSummary["phase"], phase: PhaseView["phase"]) =>
    viewOf(
      started({
        game: gameSummary({ phase: game }),
        phase: phaseView({ phase }),
      }),
    );

  test.each([
    ["lobby", "lobby", "lobby"],
    ["weaving", "weaving", "weaving"],
    ["playing", "playing", "game"],
    ["paused", "paused", "game"],
    ["finished", "finished", "game"],
    // события lobby и phase приходят независимо: верна более поздняя фаза
    ["lobby", "weaving", "weaving"],
    ["weaving", "playing", "game"],
    ["playing", "lobby", "game"],
  ] as const)("game=%s, phase=%s → %s", (game, phase, stage) => {
    expect(gameStage(view(game, phase))).toBe(stage);
  });
});

describe("leavingGame: игрок сам выходит из игры", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(SERVER_MS);
  });

  /** Игрок в игре g1 с открытым сокетом. */
  function inGame() {
    const sockets = fakeWebSocket();
    signedIn();
    const stop = followGame("g1");
    sockets.last().open();
    sockets.last().serve(snapshot());
    return { ...sockets, stop };
  }

  test("пока выход не завершён, закрытие сокета 4403 не показывает «Игра недоступна»", async () => {
    const { last, stop } = inGame();
    let finish: () => void = () => {};
    const leaving = leavingGame(
      "g1",
      () => new Promise<void>((resolve) => (finish = resolve)),
    );

    last().drop(4403); // сервер закрыл сокет раньше ответа REST

    const state = useGame.getState();
    expect(state.notice).toBeNull();
    expect(state.screen).toBe("game");
    expect(viewOf(state.live).game.id).toBe("g1");
    finish();
    await leaving;
    stop();
  });

  test("повторное подключение после обрыва, закончившееся отказом, тоже без сообщения", async () => {
    const { last, stop } = inGame();
    let finish: () => void = () => {};
    const leaving = leavingGame(
      "g1",
      () => new Promise<void>((resolve) => (finish = resolve)),
    );
    last().drop(1013);
    vi.advanceTimersByTime(1000);
    last().open();

    last().drop(4403);

    expect(useGame.getState().notice).toBeNull();
    finish();
    await leaving;
    stop();
  });

  test("после выхода обычное закрытие 4403 снова даёт «Игра недоступна»", async () => {
    const { last } = inGame();
    await leavingGame("g1", () => Promise.resolve());

    last().drop(4403);

    expect(useGame.getState()).toMatchObject({
      screen: "games",
      notice: "Игра недоступна.",
    });
  });

  test("если выход не удался, флаг снимается: исключение не теряется", async () => {
    const { last } = inGame();
    const failure = new Error("нет связи");

    await expect(leavingGame("g1", () => Promise.reject(failure))).rejects.toBe(
      failure,
    );
    last().drop(4403);

    expect(useGame.getState().notice).toBe("Игра недоступна.");
  });

  test("выход из другой игры 4403 этой игры не глушит", async () => {
    const { last, stop } = inGame();
    let finish: () => void = () => {};
    const leaving = leavingGame(
      "g2",
      () => new Promise<void>((resolve) => (finish = resolve)),
    );

    last().drop(4403);

    expect(useGame.getState().notice).toBe("Игра недоступна.");
    finish();
    await leaving;
    stop();
  });

  test("4401 во время выхода всё равно ведёт на вход", async () => {
    const { last } = inGame();
    let finish: () => void = () => {};
    const leaving = leavingGame(
      "g1",
      () => new Promise<void>((resolve) => (finish = resolve)),
    );

    last().drop(4401);

    expect(useGame.getState().screen).toBe("login");
    finish();
    await leaving;
  });

  test("результат действия возвращается вызывающему", async () => {
    await expect(leavingGame("g1", () => Promise.resolve(7))).resolves.toBe(7);
  });
});
