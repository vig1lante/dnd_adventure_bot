import { miniApp, retrieveLaunchParams, themeParams } from "@tma.js/sdk-react";

/** Тёмная — «вечер у свечи», светлая — «пергамент днём». Сами цвета — в styles/tokens.css. */
export type ThemeName = "dark" | "light";

const DEFAULT_THEME: ThemeName = "dark";
const LIGHT_SCHEME_QUERY = "(prefers-color-scheme: light)";

/**
 * Светлая или тёмная схема по цвету фона клиента Telegram (`#rgb` или `#rrggbb`).
 * Остальные цвета темы Telegram нам не нужны: палитра у приложения своя.
 * Не цвет — `null`.
 */
export function schemeOfBackground(
  color: string | null | undefined,
): ThemeName | null {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color ?? "")?.[1];
  if (hex === undefined) return null;
  const full =
    hex.length === 3 ? Array.from(hex, (digit) => digit + digit).join("") : hex;
  const [red, green, blue] = [0, 2, 4].map((at) =>
    Number.parseInt(full.slice(at, at + 2), 16),
  );
  // Воспринимаемая яркость (ITU-R BT.601): середина шкалы делит схемы пополам.
  return 0.299 * red + 0.587 * green + 0.114 * blue < 128 ? "dark" : "light";
}

/** Тема клиента Telegram важнее системной настройки; без обеих — тёмная. */
export function chooseTheme(
  telegram: ThemeName | null,
  system: ThemeName | null,
): ThemeName {
  return telegram ?? system ?? DEFAULT_THEME;
}

/** Системная схема (`prefers-color-scheme`); без matchMedia (jsdom, старые WebView) — `null`. */
export function systemTheme(): ThemeName | null {
  if (typeof window.matchMedia !== "function") return null;
  return window.matchMedia(LIGHT_SCHEME_QUERY).matches ? "light" : "dark";
}

/**
 * Тема из параметров запуска Telegram: они есть сразу, до запуска SDK. Вне Telegram
 * и без цвета фона — `null`. Ошибки SDK не пишем в журнал: в них бывает initData.
 */
export function launchTheme(): ThemeName | null {
  try {
    return schemeOfBackground(
      retrieveLaunchParams().tgWebAppThemeParams?.bg_color,
    );
  } catch {
    return null;
  }
}

type HexColor = `#${string}`;
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

// Что красим в Telegram и каким токеном палитры.
const MINI_APP_PAINTS: ReadonlyArray<
  readonly [string, string, (color: HexColor) => void]
> = [
  ["шапка", "--color-header", (color) => miniApp.setHeaderColor(color)],
  [
    "нижняя панель",
    "--color-header",
    (color) => miniApp.setBottomBarColor(color),
  ],
  ["фон", "--color-bg", (color) => miniApp.setBgColor(color)],
];

// Шапка и фон Mini App красятся в цвета выбранной палитры. Это оформление: на старом
// клиенте часть вызовов недоступна, и на игру это не влияет. Текст ошибки SDK в журнал
// не пишем: в нём бывают параметры запуска, то есть initData.
function paintMiniApp(): void {
  if (!miniApp.isMounted()) return;
  const styles = getComputedStyle(document.documentElement);
  for (const [name, token, paint] of MINI_APP_PAINTS) {
    const color = styles.getPropertyValue(token).trim();
    if (!HEX_COLOR.test(color)) continue;
    try {
      paint(color as HexColor);
    } catch {
      console.warn(`Telegram SDK: цвет «${name}» не задан.`);
    }
  }
}

/** Включает палитру: атрибут `data-theme` на странице и цвета шапки Telegram. */
export function applyTheme(theme: ThemeName): void {
  document.documentElement.dataset.theme = theme;
  paintMiniApp();
}

/**
 * Выбирает палитру при запуске приложения. В Telegram дальше тему ведёт клиент
 * (`watchTelegramTheme`), вне Telegram — системная настройка. Возвращает функцию,
 * снимающую слежение.
 */
export function startTheme(): () => void {
  const fromTelegram = launchTheme();
  applyTheme(chooseTheme(fromTelegram, systemTheme()));
  if (fromTelegram !== null || typeof window.matchMedia !== "function") {
    return () => {};
  }
  const media = window.matchMedia(LIGHT_SCHEME_QUERY);
  const onChange = () => applyTheme(chooseTheme(null, systemTheme()));
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

/**
 * Следит за темой клиента Telegram (игрок может переключить её, не закрывая игру) и
 * красит шапку. Вызывается после `setupTelegram`; пока тема SDK не подключена, ничего не делает.
 */
export function watchTelegramTheme(): void {
  if (!themeParams.isMounted()) return;
  try {
    miniApp.mount();
  } catch {
    console.warn("Telegram SDK: шаг «шапка» недоступен.");
  }
  const follow = () => {
    const scheme = schemeOfBackground(themeParams.bgColor());
    if (scheme !== null) applyTheme(scheme);
  };
  themeParams.bgColor.sub(follow);
  follow();
}
