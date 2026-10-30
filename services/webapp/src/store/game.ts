import { create } from "zustand";
import type {
  FeedItem,
  FeedWithdrawal,
  GamePhase,
  HitDieResult,
  JournalEntry,
  PhaseView,
  PlayerAction,
  RestView,
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

/**
 * Запись дневника пришла событием: новая встаёт в конец, а дополненная (тот же `id`) заменяет
 * прежнюю на её месте — порядок дневника остаётся порядком появления записей.
 */
function withJournalEntry(
  journal: JournalEntry[],
  entry: JournalEntry,
): JournalEntry[] {
  if (!journal.some((item) => item.id === entry.id)) return [...journal, entry];
  return journal.map((item) => (item.id === entry.id ? entry : item));
}

/** Начался привал: вид и срок — из кадра; идеи и реплики остаются, если это тот же привал. */
function restStarted(
  rest: RestView | null,
  kind: RestView["kind"],
  deadline_at: string | null,
): RestView {
  return rest === null
    ? { kind, deadline_at, my_ideas: [], lines: [] }
    : { ...rest, kind, deadline_at };
}

/** Идея для рассказа пришла кадром: без привала ей некуда лечь, повтор не добавляется. */
function withIdea(rest: RestView | null, text: string): RestView | null {
  if (rest === null || rest.my_ideas.includes(text)) return rest;
  return { ...rest, my_ideas: [...rest.my_ideas, text] };
}

/** Реплики привала следуют за лентой: новая реплика у костра добавляется, снятая запись уходит. */
function withRestLine(
  rest: RestView | null,
  item: FeedItem | FeedWithdrawal,
): RestView | null {
  if (rest === null) return rest;
  if ("superseded" in item) {
    return rest.lines.some((line) => line.id === item.id)
      ? { ...rest, lines: rest.lines.filter((line) => line.id !== item.id) }
      : rest;
  }
  const known = rest.lines.some((line) => line.id === item.id);
  if (item.kind !== "camp_line" || known) return rest;
  return { ...rest, lines: [...rest.lines, item] };
}

/**
 * Новая фаза и привал. Привал живёт, пока фаза хода — `camp` (так его отдаёт и снимок сервера):
 * откат «Перегенерировать» или сбой мастера кончают привал без кадра `rest_ended`, и его идеи
 * не должны перейти в следующий привал.
 */
function withPhase(view: GameView, phase: PhaseView): GameView {
  const rest = phase.turn_phase === "camp" ? view.rest : null;
  return { ...view, phase, rest };
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

/**
 * Статусы нового хода: ход ещё никто не начал (`idle`, значка нет). Клиент сбрасывает
 * их вместе с фазой, не дожидаясь кадров `player_status`, которые сервер шлёт следом: иначе между
 * кадрами горели бы «Готов», «Пас» и «Пишет» прошлого раунда. Так же статусы нового хода отдаёт
 * снимок сервера; «Пишет» загорится кадром сервера, когда игрок наберёт черновик с текстом.
 */
const nobodyStarted = (view: GameView): GameView["statuses"] =>
  Object.fromEntries(
    view.game.players.map((player) => [player.user_id, "idle" as const]),
  );

/**
 * Сервер сдал черновик автора по таймеру (кадр приходит только автору и только с текстом). Сданный
 * или спасованный вручную ход не трогаем; черновика, ещё не дошедшего до клиента, текст неизвестен.
 */
function autoSubmitted(action: PlayerAction | null): PlayerAction | null {
  if (action === null) {
    return {
      text: "",
      secret: false,
      inspiration: false,
      status: "auto_submitted",
    };
  }
  return action.status === "draft"
    ? { ...action, status: "auto_submitted" }
    : action;
}

function updateView(view: GameView, event: WsEvent): GameView {
  switch (event.type) {
    case "phase":
      return withPhase(view, event.phase);
    case "player_status":
      return {
        ...view,
        statuses: { ...view.statuses, [event.user_id]: event.status },
      };
    case "feed_item":
      return {
        ...view,
        feed: withFeedItem(view.feed, event.item),
        rest: withRestLine(view.rest, event.item),
      };
    case "party":
      return { ...view, party: event.party };
    case "combat":
      return { ...view, combat: event.combat };
    case "character":
      return { ...view, character: event.character };
    case "lobby":
      return { ...view, game: event.game };
    case "journal_entry":
      return { ...view, journal: withJournalEntry(view.journal, event.entry) };
    case "journal":
      return { ...view, journal: event.entries };
    case "rest_started":
      return {
        ...view,
        rest: restStarted(view.rest, event.kind, event.deadline_at),
      };
    case "rest_idea": {
      const rest = withIdea(view.rest, event.text);
      return rest === view.rest ? view : { ...view, rest };
    }
    case "rest_ended":
      return view.rest === null ? view : { ...view, rest: null };
    case "action_auto_submitted": {
      const action = autoSubmitted(view.action);
      return action === view.action ? view : { ...view, action };
    }
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
        journal: event.journal,
        // Сервер до блока привалов поля не присылает: без него экран привала упал бы.
        rest: event.rest ?? null,
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
      view: {
        ...withPhase(state.view, event.phase),
        action: null,
        statuses: nobodyStarted(state.view),
      },
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

/**
 * Кладёт в лист героя остаток костей хитов и хиты из ответа сервера на «Потратить кость хитов»:
 * кнопка показывает новый остаток сразу, не дожидаясь события `character`.
 */
export function applyHitDie(state: LiveState, result: HitDieResult): LiveState {
  const character = state.view?.character ?? null;
  if (state.view === null || character === null) return state;
  const { hit_dice, hp, hp_max } = result;
  return {
    ...state,
    view: { ...state.view, character: { ...character, hit_dice, hp, hp_max } },
  };
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
  /** Кладёт в лист героя ответ REST на «Потратить кость хитов». */
  setHitDie: (result: HitDieResult) => void;
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
  setHitDie: (result) =>
    set((state) => ({ live: applyHitDie(state.live, result) })),
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
const KICKED = "Хост исключил вас.";

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
 * возврат на вход; 4403 — игры нет или игрок в ней не участник, «Мои игры» с пояснением
 * («Хост исключил вас», если его исключил хост).
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
          setNotice(reason === "kicked" ? KICKED : GAME_UNAVAILABLE);
        }
      },
    },
  );
  stopCurrent = stop;
  return stop;
}
