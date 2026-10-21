import { errorText } from "../api/client";
import type { Api } from "../api/client";
import type { DevLogin, LoginResult } from "../api/types";
import { useGame } from "../store/game";
import { devLoginFromSearch } from "./dev";

/** Что нужно для входа: клиент API и откуда брать приглашение (запасной параметр запуска и адрес). */
export interface LoginContext {
  api: Api;
  /** `tgWebAppStartParam` из параметров запуска Telegram. */
  launchStartParam?: string | null;
  /** `location.search` страницы. */
  search: string;
}

export interface StartupEnv extends LoginContext {
  /** Строка initData; `null` — страница открыта вне Telegram. */
  initData: string | null;
}

/** Чем закончилась попытка входа без участия игрока. */
export type Startup = "signed-in" | "choose-dev" | "telegram-required";

const JOIN_CODE = /^[A-Za-z0-9]{6}$/;

/**
 * Код приглашения (6 символов A–Z и 0–9, регистр и пробелы по краям не важны) или `null`.
 * Неверные коды на сервер не уходят: каждая неудачная попытка входа по коду там
 * считается и после десятой блокирует вход по кодам на 10 минут.
 */
export function parseJoinCode(value: string | null | undefined): string | null {
  const code = value?.trim() ?? "";
  return JOIN_CODE.test(code) ? code.toUpperCase() : null;
}

/**
 * Первый подходящий код: параметр запуска из ответа сервера (он подписан Telegram),
 * затем из параметров запуска SDK, затем `?join=` адреса (нужен Playwright и dev).
 */
function inviteCode(login: LoginResult, context: LoginContext): string | null {
  return (
    parseJoinCode(login.start_param) ??
    parseJoinCode(context.launchStartParam) ??
    parseJoinCode(new URLSearchParams(context.search).get("join"))
  );
}

/**
 * Принимает ответ входа: кладёт сессию в память и, если есть приглашение, входит
 * в игру. Отказ по приглашению вход не отменяет: игрок попадает в «Мои игры»
 * с сообщением сервера.
 */
async function finishLogin(
  context: LoginContext,
  login: LoginResult,
): Promise<void> {
  const store = useGame.getState();
  store.setSession({ token: login.token, user: login.user });
  const code = inviteCode(login, context);
  if (code === null) {
    store.show({ screen: "games" });
    return;
  }
  try {
    const game = await context.api.joinGame(code);
    // В начале игры (weaving) экран вплетения живёт в лобби, как и в «Мои игры».
    const inLobby = game.phase === "lobby" || game.phase === "weaving";
    store.show({ screen: inLobby ? "lobby" : "game", gameId: game.id });
  } catch (error) {
    store.show({ screen: "games" });
    store.setNotice(errorText(error));
  }
}

/** Вход выбранным dev-игроком (экран выбора). */
export async function signInAsDev(
  context: LoginContext,
  login: DevLogin,
): Promise<void> {
  await finishLogin(context, await context.api.loginDev(login));
}

/**
 * Вход при открытии приложения. В Telegram — по initData; иначе, если сервер открыл
 * dev-вход, — по `?dev=dev-N` или после выбора игрока; иначе нужна подсказка
 * «Откройте приложение из Telegram». Ошибка запроса уходит вызывающему.
 */
export async function startSession(env: StartupEnv): Promise<Startup> {
  const { api, initData } = env;
  if (initData !== null) {
    // Отказ входа в Telegram не заменяется dev-входом: dev включает только сервер.
    await finishLogin(env, await api.loginTelegram(initData));
    return "signed-in";
  }
  const config = await api.getConfig();
  if (!config.dev_login) return "telegram-required";
  const login = devLoginFromSearch(env.search);
  if (login === null) return "choose-dev";
  await signInAsDev(env, login);
  return "signed-in";
}
