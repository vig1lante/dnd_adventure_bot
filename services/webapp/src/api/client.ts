import { authoringApi } from "./authoring";
import type {
  ActionBody,
  AdventureSummary,
  AppConfig,
  BackstoryHelpBody,
  CreateCharacterBody,
  CreatedCharacter,
  CreateGameBody,
  CreationOptions,
  DevLogin,
  FeedItem,
  GameSummary,
  HitDieResult,
  HostAction,
  LoginResult,
  MyCharacter,
  PhaseView,
  PlayerAction,
  ProvidersList,
  RandomAbilities,
  RandomPreview,
  RestKind,
  User,
} from "./types";

/** Ошибка запроса: код ответа и текст по-русски, который можно показать игроку. */
export class ApiError extends Error {
  /** Код ответа; 0 — до сервера запрос не дошёл. */
  readonly status: number;
  readonly detail: string;
  /** Подробности отказа правил (только у 422): список строк по-русски. */
  readonly problems?: string[];
  /**
   * Машинный код отказа, если сервер его прислал (конструктор: `version_conflict`,
   * `job_running` и др.). По нему экран различает отказы с одинаковым статусом.
   */
  readonly code?: string;

  constructor(
    status: number,
    detail: string,
    problems?: string[],
    code?: string,
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.problems = problems;
    this.code = code;
  }
}

const NO_CONNECTION =
  "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.";
const UNEXPECTED_ANSWER = "Сервер вернул непонятный ответ.";
const UNAVAILABLE =
  "Сервер временно недоступен. Попробуйте ещё раз немного позже.";

// Тексты по коду для ответов, у которых нет русского detail: 400 от TrustedHost
// (обычный текст), 404 и 405 от Starlette (английский detail), страницы HTML от
// туннеля и прокси (502, 530), ответ 500 без JSON.
const TEXT_BY_STATUS: Partial<Record<number, string>> = {
  400: "Сервер не принял запрос: проверьте адрес приложения.",
  401: "Нужно войти заново.",
  403: "Это действие вам недоступно.",
  404: "Не найдено.",
  405: "Сервер не поддерживает это действие.",
  409: "Сейчас это действие недоступно.",
  422: "Некорректные данные запроса.",
  429: "Слишком много запросов. Подождите немного.",
  500: "Внутренняя ошибка сервера. Попробуйте ещё раз немного позже.",
};

function textForStatus(status: number): string {
  return (
    TEXT_BY_STATUS[status] ??
    (status >= 500
      ? UNAVAILABLE
      : `Не удалось выполнить запрос (код ${status}).`)
  );
}

// Русские тексты сервера всегда содержат кириллицу. Всё остальное — не наш
// текст (Starlette, прокси), игроку его не показываем.
function isRussianText(value: unknown): value is string {
  return typeof value === "string" && /[А-Яа-яЁё]/.test(value);
}

async function readJsonObject(
  response: Response,
): Promise<Record<string, unknown> | undefined> {
  try {
    const parsed: unknown = JSON.parse(await response.text());
    return typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : undefined;
  } catch {
    return undefined;
  }
}

async function errorFrom(response: Response): Promise<ApiError> {
  const body = await readJsonObject(response);
  const detail = isRussianText(body?.detail)
    ? body.detail
    : textForStatus(response.status);
  const problems =
    response.status === 422 && Array.isArray(body?.problems)
      ? body.problems.filter((item): item is string => typeof item === "string")
      : [];
  return new ApiError(
    response.status,
    detail,
    problems.length > 0 ? problems : undefined,
    typeof body?.code === "string" ? body.code : undefined,
  );
}

/** Текст для экрана по любой пойманной ошибке. */
export function errorText(error: unknown): string {
  return error instanceof ApiError
    ? error.detail
    : "Что-то пошло не так. Попробуйте ещё раз.";
}

export interface RequestOptions {
  body?: unknown;
  /** Сырое тело (архив zip импорта): уходит как есть со своим типом, без JSON. */
  raw?: { data: Blob; type: string };
  /** Ответ — файл (`Blob`, архив экспорта), а не JSON. Ошибки — те же, по-русски. */
  blob?: true;
  /**
   * Заголовок Authorization. Не задан — `Bearer <токен сессии>`, если токен есть;
   * `null` — без заголовка (конфигурация и dev-вход).
   */
  authorization?: string | null;
}

/** Общий запрос клиента: методы конструктора (authoring.ts) строятся поверх него. */
export type Request = <T>(
  method: string,
  path: string,
  options?: RequestOptions,
) => Promise<T>;

/**
 * Клиент REST API сервера. `getToken` отдаёт токен текущей сессии (он живёт только
 * в памяти приложения). Токен и initData идут исключительно в заголовке
 * Authorization: в адрес запроса их класть нельзя, Vite пишет адрес в журнал.
 */
export function createApi(
  fetchImpl: typeof fetch = (input, init) => fetch(input, init),
  getToken: () => string | null = () => null,
) {
  async function request<T>(
    method: string,
    path: string,
    { body, raw, blob, authorization }: RequestOptions = {},
  ): Promise<T> {
    const token = getToken();
    const credentials =
      authorization === undefined
        ? token === null
          ? null
          : `Bearer ${token}`
        : authorization;
    const headers: Record<string, string> = { Accept: "application/json" };
    if (credentials !== null) headers.Authorization = credentials;
    if (raw !== undefined) headers["Content-Type"] = raw.type;
    else if (body !== undefined) headers["Content-Type"] = "application/json";

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers,
        body:
          raw !== undefined
            ? raw.data
            : body === undefined
              ? undefined
              : JSON.stringify(body),
      });
    } catch {
      // Причину обрыва (TypeError без подробностей) игроку знать незачем.
      throw new ApiError(0, NO_CONNECTION);
    }
    if (!response.ok) throw await errorFrom(response);
    if (response.status === 204) return undefined as T;
    try {
      return (blob ? await response.blob() : await response.json()) as T;
    } catch {
      throw new ApiError(response.status, UNEXPECTED_ANSWER);
    }
  }

  const get = <T>(path: string) => request<T>("GET", path);
  const post = <T>(path: string, body?: unknown) =>
    request<T>("POST", path, { body });
  const gamePath = (gameId: string) =>
    `/api/games/${encodeURIComponent(gameId)}`;

  return {
    // --- вход и сведения о пользователе ---
    getConfig: () =>
      request<AppConfig>("GET", "/api/config", { authorization: null }),
    loginTelegram: (initData: string) =>
      request<LoginResult>("POST", "/api/auth/telegram", {
        authorization: `tma ${initData}`,
      }),
    loginDev: (login: DevLogin) =>
      request<LoginResult>("POST", "/api/auth/dev", {
        body: { login },
        authorization: null,
      }),
    getMe: () => get<User>("/api/me"),

    // --- игры и лобби ---
    listAdventures: () => get<AdventureSummary[]>("/api/adventures"),
    listProviders: () => get<ProvidersList>("/api/providers"),
    listGames: () => get<GameSummary[]>("/api/games"),
    createGame: (body: CreateGameBody) => post<GameSummary>("/api/games", body),
    joinGame: (code: string) => post<GameSummary>("/api/games/join", { code }),
    getGame: (gameId: string) => get<GameSummary>(gamePath(gameId)),
    setReady: (gameId: string, ready: boolean) =>
      post<GameSummary>(`${gamePath(gameId)}/ready`, { ready }),
    startGame: (gameId: string) =>
      post<GameSummary>(`${gamePath(gameId)}/start`),
    leaveGame: (gameId: string) => post<void>(`${gamePath(gameId)}/leave`),
    kickPlayer: (gameId: string, userId: string) =>
      post<GameSummary>(
        `${gamePath(gameId)}/players/${encodeURIComponent(userId)}/kick`,
      ),

    // --- персонаж ---
    getCreationOptions: () =>
      get<CreationOptions>("/api/content/creation-options"),
    // Раскладка очков для класса героя; ничего не пишет и не рассылает, повторять можно.
    randomAbilities: (gameId: string, classId: string) =>
      post<RandomAbilities>(`${gamePath(gameId)}/characters/random-abilities`, {
        class_id: classId,
      }),
    // Свой герой и его прежний выбор; 404, пока героя нет.
    myCharacter: (gameId: string) =>
      get<MyCharacter>(`${gamePath(gameId)}/characters/mine`),
    randomPreview: (gameId: string) =>
      post<RandomPreview>(`${gamePath(gameId)}/characters/random-preview`),
    createCharacter: (gameId: string, body: CreateCharacterBody) =>
      post<CreatedCharacter>(`${gamePath(gameId)}/characters`, body),
    backstoryHelp: (gameId: string, body: BackstoryHelpBody) =>
      post<{ backstory: string }>(
        `${gamePath(gameId)}/characters/backstory-help`,
        body,
      ),

    // --- ход игрока ---
    saveDraft: (gameId: string, body: ActionBody) =>
      request<PlayerAction>("PUT", `${gamePath(gameId)}/action`, { body }),
    submitAction: (gameId: string) =>
      post<PlayerAction>(`${gamePath(gameId)}/action/submit`),
    passAction: (gameId: string) =>
      post<PlayerAction>(`${gamePath(gameId)}/action/pass`),
    sendCampLine: (gameId: string, text: string) =>
      post<FeedItem>(`${gamePath(gameId)}/camp/line`, { text }),
    // Кость бросает сервер; ответ — остаток костей и хиты героя после лечения.
    spendHitDie: (gameId: string) =>
      post<HitDieResult>(`${gamePath(gameId)}/rest/hit-dice`),

    // --- кнопки хоста: ответ — фаза игры, как в снимке ---
    hostAction: (gameId: string, action: HostAction) =>
      post<PhaseView>(`${gamePath(gameId)}/host/${action}`),
    startRest: (gameId: string, kind: RestKind) =>
      post<PhaseView>(`${gamePath(gameId)}/host/rest`, { kind }),

    // --- конструктор приключений (только создателям игр) ---
    ...authoringApi(request),
  };
}

export type Api = ReturnType<typeof createApi>;
