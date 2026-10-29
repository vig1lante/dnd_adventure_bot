import type { ThemeName } from "../theme/telegram";

/** Просят ли страницу превью стиля (`/?preview=style`). В прод-сборку превью не попадает: см. main.tsx. */
export function wantsStylePreview(search: string): boolean {
  return new URLSearchParams(search).get("preview") === "style";
}

/** Просят ли макеты экранов (`/?preview=screens`, B10.1). Как и превью стиля — только в dev. */
export function wantsScreensPreview(search: string): boolean {
  return new URLSearchParams(search).get("preview") === "screens";
}

/**
 * Экраны макетов. Суффикс — состояние того же экрана («games-loading» — «Мои игры» во время
 * загрузки) или его вариант («story-a» — рассказ с порядком вкладок А).
 */
export const PREVIEW_SCREENS = [
  "login",
  "games",
  "games-loading",
  "games-empty",
  "games-error",
  "lobby",
  "weaving",
  "create",
  "story",
  "story-a",
  "thinking",
  "composer",
  "combat",
  "combat-toast",
  "camp",
  "journal",
  "settings",
  "authoring",
] as const;

export type PreviewScreen = (typeof PREVIEW_SCREENS)[number];

/** «Как есть» — настоящий экран текущего кода; «как будет» — черновик предложения. */
export type PreviewVariant = "now" | "next";

export interface ScreensPreviewParams {
  /** `null` — экран не указан или незнаком: страница показывает оглавление. */
  screen: PreviewScreen | null;
  variant: PreviewVariant;
  theme: ThemeName;
}

const isPreviewScreen = (value: string | null): value is PreviewScreen =>
  (PREVIEW_SCREENS as readonly (string | null)[]).includes(value);

/**
 * Параметры макетов из адреса: `?preview=screens&screen=<имя>&variant=now|next&theme=light|dark`.
 * Незнакомые значения не ломают страницу: вариант — «как есть», тема — тёмная.
 */
export function screensPreviewOf(search: string): ScreensPreviewParams | null {
  const params = new URLSearchParams(search);
  if (params.get("preview") !== "screens") return null;
  const screen = params.get("screen");
  return {
    screen: isPreviewScreen(screen) ? screen : null,
    variant: params.get("variant") === "next" ? "next" : "now",
    theme: params.get("theme") === "light" ? "light" : "dark",
  };
}
