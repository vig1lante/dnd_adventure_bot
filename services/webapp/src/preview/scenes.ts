import { useGame } from "../store/game";
import type { Destination } from "../store/game";
import type { PreviewRoutes } from "./backend";
import type { PreviewScreen } from "./gate";
import {
  ADVENTURES,
  APP_CONFIG,
  CREATION_OPTIONS,
  DRAFT,
  DRAFT_SUMMARIES,
  LOBBY_GAME,
  MY_GAMES,
  PREVIEW_USER,
  SNAPSHOTS,
} from "./screenData";

// Сцена макета: где в приложении стоит игрок (экран и игра) и чем отвечает «сервер». Один и тот
// же экран в разных состояниях («Мои игры» в загрузке, пустые, с ошибкой) — разные ответы.

const ROUTES: PreviewRoutes = {
  "GET /api/config": { json: APP_CONFIG },
  "GET /api/me": { json: PREVIEW_USER },
  "GET /api/games": { json: MY_GAMES },
  "GET /api/adventures": { json: ADVENTURES },
  "GET /api/games/g-lobby": { json: LOBBY_GAME },
  "GET /api/games/g-lobby/characters/mine": {
    status: 404,
    json: { detail: "Героя ещё нет." },
  },
  "GET /api/content/creation-options": { json: CREATION_OPTIONS },
  "PUT /api/games/g-story/action": { json: SNAPSHOTS["g-story"].action },
  "GET /api/drafts": { json: DRAFT_SUMMARIES },
  "GET /api/drafts/d-salt": { json: DRAFT },
  "GET /api/drafts/d-salt/refs": { json: [] },
  "GET /api/srd/monsters": { json: [] },
};

interface Scene {
  /** Куда перешёл игрок; `null` — экран входа (сессии ещё нет). */
  place: Destination | null;
  /** Ответы, которые меняют обычные маршруты этой сцены. */
  routes?: PreviewRoutes;
}

const inGame = (gameId: string): Scene => ({
  place: { screen: "game", gameId },
});

const SCENES: Record<PreviewScreen, Scene> = {
  login: { place: null, routes: { "GET /api/config": "offline" } },
  games: { place: { screen: "games" } },
  "games-loading": {
    place: { screen: "games" },
    routes: { "GET /api/games": "pending" },
  },
  "games-empty": {
    place: { screen: "games" },
    routes: { "GET /api/games": { json: [] } },
  },
  "games-error": {
    place: { screen: "games" },
    routes: { "GET /api/games": "offline" },
  },
  lobby: { place: { screen: "lobby", gameId: "g-lobby" } },
  weaving: { place: { screen: "lobby", gameId: "g-weave" } },
  create: { place: { screen: "create", gameId: "g-lobby" } },
  story: inGame("g-story"),
  "story-a": inGame("g-story"),
  thinking: inGame("g-think"),
  composer: inGame("g-story"),
  combat: inGame("g-combat"),
  "combat-toast": inGame("g-combat"),
  camp: inGame("g-camp"),
  journal: inGame("g-story"),
  settings: { place: { screen: "games" } },
  authoring: { place: { screen: "games" } },
};

/** Маршруты «сервера» для экрана: обычные с поправками сцены. */
export function routesFor(screen: PreviewScreen): PreviewRoutes {
  return { ...ROUTES, ...SCENES[screen].routes };
}

/** Id игры сцены (`null` — сцена не в игре и не в лобби). */
export function gameIdOf(screen: PreviewScreen): string | null {
  const { place } = SCENES[screen];
  return place !== null && "gameId" in place ? place.gameId : null;
}

/**
 * Кладёт в хранилище приложения сессию и экран сцены — как после входа и перехода. Вызывать до
 * первого показа экранов: они читают хранилище при первом же рендере.
 */
export function enterScene(screen: PreviewScreen): void {
  const { place } = SCENES[screen];
  if (place === null) {
    useGame.setState({ session: null, screen: "login", gameId: null });
    return;
  }
  useGame.setState({
    session: { token: "preview", user: PREVIEW_USER },
    screen: place.screen,
    gameId: "gameId" in place ? place.gameId : null,
    notice: null,
  });
}
