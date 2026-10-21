import { create } from "zustand";
import type {
  FeedItem,
  FeedWithdrawal,
  GamePhase,
  PhaseView,
  PlayerAction,
  Snapshot,
  TurnPhase,
  User,
  WsEvent,
} from "../api/types";
import { connectGame } from "../api/ws";
import type { ConnectionStatus } from "../api/ws";

export type Screen = "login" | "games" | "lobby" | "create" | "game";

/** Сессия живёт только в памяти: перезагрузка страницы повторяет вход. */
export interface Session {
  token: string;
  user: User;
}

/** Экран, на который можно перейти после входа; лобби, созданию героя и игре нужен id игры. */
export type Destination =
  { screen: "games" } | { screen: "lobby" | "create" | "game"; gameId: string };

// --- игра из сокета ---

/** Игра глазами игрока: блоки снимка без служебных полей кадра. */
export type GameView = Omit<Snapshot, "type" | "server_time">;

export interface LiveState {
  /** `null`, пока не пришёл первый снимок. */
  view: GameView | null;
  /** Серверное время снимка минус часы браузера, мс: по нему считаются отсчёты. */
  clockOffsetMs: number;
  /** Последнее событие `error`; новый снимок его снимает. */
  error: string | null;
  /**
   * Сколько ходов началось на глазах клиента. Меняется, когда мастер закончил разбор и игроки
   * снова пишут ход: экран хода по этому числу начинает с чистого листа. Снимок его не трогает.
   */
  turn: number;
}

export const NO_LIVE: LiveState = {
  view: null,
  clockOffsetMs: 0,
  error: null,
  turn: 0,
};

/** Сколько записей ленты держим в памяти: сервер в снимке отдаёт 200, остальное нарастает за вечер. */
export const MAX_FEED_ITEMS = 500;

/** Запись ленты снята надгробием `{id, superseded}`: убираем её по id. */
function withFeedItem(
  feed: FeedItem[],
  item: FeedItem | FeedWithdrawal,
): FeedItem[] {
  if ("superseded" in item) return feed.filter((entry) => entry.id !== item.id);
  // Событие, пришедшее вслед за снимком, уже могло войти в его ленту.
  if (feed.some((entry) => entry.id === item.id)) return feed;
  return [...feed, item].slice(-MAX_FEED_ITEMS);
}

// Фазы, в которые игра приходит после «Мастер думает», когда разбор удался. «Мастер не
// справился» ход не заканчивает: ходы игроков сохранены и после «Повторить» разбираются снова.
const AFTER_RESOLVING: ReadonlySet<TurnPhase | null> = new Set([
  "discussion",
  "combat_wait",
  "camp",
]);

/** Мастер закончил разбор, и начался следующий ход. */
function startsNewTurn(before: PhaseView, after: PhaseView): boolean {
  return (
    before.turn_phase === "resolving" && AFTER_RESOLVING.has(after.turn_phase)
  );
}

function updateView(view: GameView, event: WsEvent): GameView {
  switch (event.type) {
    case "phase":
      return { ...view, phase: event.phase };
    case "player_status":
      return {
        ...view,
        statuses: { ...view.statuses, [event.user_id]: event.status },
      };
    case "feed_item":
      return { ...view, feed: withFeedItem(view.feed, event.item) };
    case "party":
      return { ...view, party: event.party };
    case "combat":
      return { ...view, combat: event.combat };
    case "character":
      return { ...view, character: event.character };
    case "lobby":
      return { ...view, game: event.game };
    default:
      return view; // pong и события, которых клиент не знает
  }
}

/**
 * Применяет событие сокета к живой игре и возвращает новое состояние. Снимок заменяет
 * игру целиком (и после обрыва тоже), остальные события заменяют каждое свой блок;
 * `receivedAt` — часы браузера в миг получения кадра.
 */
export function applyEvent(
  state: LiveState,
  event: WsEvent,
  receivedAt: number = Date.now(),
): LiveState {
  if (event.type === "snapshot") {
    return {
      view: {
        game: event.game,
        host_user_id: event.host_user_id,
        phase: event.phase,
        statuses: event.statuses,
        character: event.character,
        action: event.action,
        party: event.party,
        combat: event.combat,
        feed: event.feed,
      },
      clockOffsetMs: Date.parse(event.server_time) - receivedAt,
      error: null,
      turn: state.turn,
    };
  }
  if (event.type === "error") return { ...state, error: event.detail };
  // Остальные события обновляют блоки снимка: без него им не к чему примениться.
  if (state.view === null) return state;
  if (event.type === "phase" && startsNewTurn(state.view.phase, event.phase)) {
    // Сервер событием «ход обновлён» не шлёт: прежний ход игрока к новому не относится.
    return {
      ...state,
      view: { ...state.view, phase: event.phase, action: null },
      turn: state.turn + 1,
    };
  }
  const view = updateView(state.view, event);
  return view === state.view ? state : { ...state, view };
}

/**
 * Кладёт в игру ход игрока из ответа сервера на черновик, «Готов» или «Пас»: событий о
 * собственном ходе у сервера нет, он приходит только в снимке и в этих ответах.
 */
export function applyAction(state: LiveState, action: PlayerAction): LiveState {
  if (state.view === null) return state;
  return { ...state, view: { ...state.view, action } };
}

/** Игра ещё в лобби, идёт вплетение героев или игра пошла (в том числе пауза и конец). */
export type GameStage = "lobby" | "weaving" | "game";

const PHASE_RANK: Record<GamePhase, number> = {
  lobby: 0,
  weaving: 1,
  playing: 2,
  paused: 2,
  finished: 2,
};

/**
 * На каком экране должна быть игра. Фаза живёт в двух блоках снимка: событие `lobby`
 * обновляет `game`, событие `phase` — `phase`. Они приходят независимо, а игра идёт только
 * вперёд (лобби, начало, игра), поэтому верна более поздняя из двух.
 */
export function gameStage(view: GameView): GameStage {
  const rank = Math.max(
    PHASE_RANK[view.game.phase],
    PHASE_RANK[view.phase.phase],
  );
  return rank === 0 ? "lobby" : rank === 1 ? "weaving" : "game";
}

/** Сейчас по часам сервера, мс с начала эпохи. */
export function serverNow(
  clockOffsetMs: number,
  browserNow: number = Date.now(),
): number {
  return browserNow + clockOffsetMs;
}

/**
 * Сколько секунд осталось до конца отсчёта фазы; `null` — отсчёта нет. Идущий отсчёт
 * считается от серверного времени (часы телефона врут), а на паузе срока нет и
 * остаток стоит на месте. Секунды округляются вверх, как остаток на паузе у сервера.
 */
export function secondsLeft(
  phase: PhaseView,
  clockOffsetMs: number,
  browserNow: number = Date.now(),
): number | null {
  if (phase.deadline_at === null) return phase.remaining_seconds;
  const left =
    Date.parse(phase.deadline_at) - serverNow(clockOffsetMs, browserNow);
  return Math.max(0, Math.ceil(left / 1000));
}

/** `idle` — игру никто не слушает; `connecting` — первое подключение. */
export type LiveConnection = "idle" | "connecting" | ConnectionStatus;

interface GameStore {
  session: Session | null;
  screen: Screen;
  gameId: string | null;
  /** Сообщение для игрока (например, отказ по приглашению); смена экрана его убирает. */
  notice: string | null;
  /** Игра из сокета; наполняется, пока открыт `followGame`. */
  live: LiveState;
  connection: LiveConnection;
  setSession: (session: Session) => void;
  show: (destination: Destination) => void;
  setNotice: (text: string) => void;
  /** Применяет событие сокета к `live`. */
  receive: (event: WsEvent) => void;
  /** Кладёт в `live` ход игрока из ответа REST (черновик, «Готов», «Пас»). */
  setAction: (action: PlayerAction) => void;
  signOut: () => void;
}

const NOT_FOLLOWING: Pick<GameStore, "live" | "connection"> = {
  live: NO_LIVE,
  connection: "idle",
};

export const useGame = create<GameStore>()((set, get) => ({
  session: null,
  screen: "login",
  gameId: null,
  notice: null,
  ...NOT_FOLLOWING,
  // Экран не меняет: после входа игрок может ещё идти по приглашению, и показывать
  // «Мои игры» на это время незачем.
  setSession: (session) => set({ session }),
  show: (destination) => {
    // Без сессии любой экран, кроме входа, пуст: запросы ушли бы без токена.
    if (get().session === null) return;
    set({
      screen: destination.screen,
      gameId: "gameId" in destination ? destination.gameId : null,
      notice: null,
    });
  },
  setNotice: (text) => set({ notice: text }),
  receive: (event) => set((state) => ({ live: applyEvent(state.live, event) })),
  setAction: (action) =>
    set((state) => ({ live: applyAction(state.live, action) })),
  signOut: () =>
    set({
      session: null,
      screen: "login",
      gameId: null,
      notice: null,
      ...NOT_FOLLOWING,
    }),
}));

/** Токен текущей сессии для клиента API. */
export const sessionToken = (): string | null =>
  useGame.getState().session?.token ?? null;

const GAME_UNAVAILABLE = "Игра недоступна.";

let stopCurrent: (() => void) | null = null;

// Игра, из которой игрок выходит сам. Сервер рвёт сокет выходящего (4403) раньше, чем
// приходит ответ на запрос выхода, и без этой отметки игрок увидел бы «Игра недоступна».
let leaving: string | null = null;

/**
 * Выход игрока из игры `gameId`: пока `leave` не закончился, закрытие сокета этой игры
 * кодом 4403 — ожидаемое, экран сам перейдёт после ответа сервера. Результат и ошибка
 * `leave` возвращаются как есть.
 */
export async function leavingGame<T>(
  gameId: string,
  leave: () => Promise<T>,
): Promise<T> {
  leaving = gameId;
  try {
    return await leave();
  } finally {
    if (leaving === gameId) leaving = null;
  }
}

/**
 * Подключает игру к хранилищу: события сокета попадают в `live`, `connection` следует
 * за связью. Возвращает остановку (очистку эффекта экрана игры). Одновременно слушается
 * одна игра: новый вызов останавливает прежний.
 *
 * Отказы сервера уводят с экрана игры без повторов: 4401 — сессия недействительна,
 * возврат на вход; 4403 — игры нет или игрок в ней не участник, «Мои игры» с пояснением.
 */
export function followGame(gameId: string): () => void {
  stopCurrent?.();
  const token = sessionToken();
  if (token === null) return () => {};

  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (stopCurrent === stop) stopCurrent = null;
    closeConnection();
    useGame.setState(NOT_FOLLOWING);
  };

  useGame.setState({ live: NO_LIVE, connection: "connecting" });
  const closeConnection = connectGame(
    gameId,
    token,
    (event) => useGame.getState().receive(event),
    {
      onStatus: (connection) => useGame.setState({ connection }),
      onRejected: (reason) => {
        // Игрок сам выходит: экран останется как есть, а переход сделает ответ на выход.
        if (reason === "not-a-member" && leaving === gameId) return;
        stop();
        const { signOut, show, setNotice } = useGame.getState();
        if (reason === "unauthorized") {
          signOut();
        } else {
          show({ screen: "games" });
          setNotice(GAME_UNAVAILABLE);
        }
      },
    },
  );
  stopCurrent = stop;
  return stop;
}
