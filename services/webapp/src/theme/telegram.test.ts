import {
  emitEvent,
  miniApp,
  mockTelegramEnv,
  themeParams,
} from "@tma.js/sdk-react";
import { setupTelegram } from "../auth/telegram";
import {
  applyTheme,
  chooseTheme,
  launchTheme,
  schemeOfBackground,
  startTheme,
  systemTheme,
  watchTelegramTheme,
} from "./telegram";

const INIT_DATA =
  "query_id=AAHdF6IQ&user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Lira%22%7D" +
  "&auth_date=1662771648&signature=sig&hash=abc123";

const currentTheme = () => document.documentElement.dataset.theme;

// Имитация системной настройки: slot для слушателей «change», чтобы менять её на лету.
function stubSystemScheme(initial: "light" | "dark") {
  let scheme = initial;
  const listeners = new Set<() => void>();
  const matchMedia = vi.fn((query: string) => ({
    media: query,
    get matches() {
      return query === "(prefers-color-scheme: light)"
        ? scheme === "light"
        : scheme === "dark";
    },
    addEventListener: (_: string, listener: () => void) =>
      listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) =>
      listeners.delete(listener),
  }));
  vi.stubGlobal("matchMedia", matchMedia);
  return {
    listeners,
    change(next: "light" | "dark") {
      scheme = next;
      listeners.forEach((listener) => listener());
    },
  };
}

function openInTelegram(themeParamsOfClient: Record<string, string> | null) {
  mockTelegramEnv({
    launchParams: {
      tgWebAppData: INIT_DATA,
      tgWebAppVersion: "8.0",
      tgWebAppPlatform: "tdesktop",
      ...(themeParamsOfClient === null
        ? {}
        : { tgWebAppThemeParams: themeParamsOfClient }),
    },
  });
}

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  miniApp.unmount();
  themeParams.unmount();
});

describe("schemeOfBackground: светлая или тёмная схема по цвету фона", () => {
  test.each([
    ["#17212b", "dark"], // тёмная тема Telegram (десктоп)
    ["#212121", "dark"], // тёмная тема Telegram (Android)
    ["#000000", "dark"],
    ["#ffffff", "light"], // светлая тема Telegram
    ["#f4f4f5", "light"],
    ["#FFFFFF", "light"], // регистр не важен
    ["#fff", "light"], // короткая запись
    ["#111", "dark"],
  ] as const)("%s → %s", (color, scheme) => {
    expect(schemeOfBackground(color)).toBe(scheme);
  });

  // Яркость считается по весам каналов (ITU-R BT.601): зелёный светлее красного и синего.
  test.each([
    ["#ff0000", "dark"],
    ["#00ff00", "light"],
    ["#0000ff", "dark"],
    ["#ff00ff", "dark"],
    ["#ffff00", "light"],
    ["#00ffff", "light"],
    ["#f00", "dark"],
    ["#0f0", "light"],
    ["#00f", "dark"],
    ["#777", "dark"], // короткая запись раскрывается удвоением цифры: 0x77
    ["#888", "light"], // 0x88, а не 0x08
    ["#808081", "light"], // яркость 128,1: уже выше середины
    ["#7f7f7f", "dark"], // чуть ниже середины шкалы
    ["#838383", "light"], // чуть выше середины шкалы
  ] as const)("%s → %s", (color, scheme) => {
    expect(schemeOfBackground(color)).toBe(scheme);
  });

  test.each([undefined, null, "", "red", "#12", "#gggggg", "rgb(1,2,3)"])(
    "%j — не цвет, схемы нет",
    (color) => {
      expect(schemeOfBackground(color)).toBeNull();
    },
  );
});

describe("chooseTheme: Telegram важнее системной настройки", () => {
  test.each([
    ["dark", "light", "dark"],
    ["light", "dark", "light"],
    ["light", null, "light"],
    [null, "light", "light"],
    [null, "dark", "dark"],
    [null, null, "dark"], // без подсказок — тёмная «вечер у свечи»
  ] as const)("Telegram %s, система %s → %s", (telegram, system, expected) => {
    expect(chooseTheme(telegram, system)).toBe(expected);
  });
});

describe("systemTheme: prefers-color-scheme", () => {
  test("светлая и тёмная схемы системы", () => {
    stubSystemScheme("light");
    expect(systemTheme()).toBe("light");

    stubSystemScheme("dark");
    expect(systemTheme()).toBe("dark");
  });

  test("окружение без matchMedia (jsdom, старые WebView) — подсказки нет", () => {
    expect(window.matchMedia).toBeUndefined();
    expect(systemTheme()).toBeNull();
  });
});

describe("launchTheme: тема из параметров запуска Telegram", () => {
  test("светлый и тёмный фон клиента", () => {
    openInTelegram({ bg_color: "#ffffff", text_color: "#000000" });
    expect(launchTheme()).toBe("light");

    openInTelegram({ bg_color: "#17212b", text_color: "#f5f5f5" });
    expect(launchTheme()).toBe("dark");
  });

  test("в Telegram, но без цвета фона — подсказки нет", () => {
    openInTelegram(null);
    expect(launchTheme()).toBeNull();

    openInTelegram({ text_color: "#000000" });
    expect(launchTheme()).toBeNull();
  });

  test("вне Telegram — подсказки нет", () => {
    expect(launchTheme()).toBeNull();
  });
});

describe("applyTheme", () => {
  test.each(["dark", "light"] as const)("ставит data-theme=%s", (theme) => {
    applyTheme(theme);

    expect(currentTheme()).toBe(theme);
  });

  test("вне Telegram цвета шапки не трогает и не пишет в журнал", () => {
    const logs = (["warn", "error"] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => {}),
    );

    expect(() => applyTheme("light")).not.toThrow();

    for (const log of logs) expect(log).not.toHaveBeenCalled();
  });
});

describe("startTheme: выбор при запуске", () => {
  test("вне Telegram — по системной настройке", () => {
    stubSystemScheme("light");

    startTheme();

    expect(currentTheme()).toBe("light");
  });

  test("без подсказок — тёмная по умолчанию", () => {
    startTheme();

    expect(currentTheme()).toBe("dark");
  });

  test("вне Telegram следует за сменой системной схемы; stop снимает слушателя", () => {
    const system = stubSystemScheme("dark");

    const stop = startTheme();
    system.change("light");
    expect(currentTheme()).toBe("light");

    stop();
    expect(system.listeners.size).toBe(0);
    system.change("dark");
    expect(currentTheme()).toBe("light");
  });

  test("в Telegram тема клиента важнее системной, системную не слушаем", () => {
    const system = stubSystemScheme("dark");
    openInTelegram({ bg_color: "#ffffff" });

    startTheme();

    expect(currentTheme()).toBe("light");
    expect(system.listeners.size).toBe(0);
  });
});

describe("watchTelegramTheme: слежение за темой клиента Telegram", () => {
  // Цвета шапки берутся из токенов текущей темы; в jsdom таблиц стилей нет — подставляем.
  function stubHeaderColors(colors: Record<"dark" | "light", string>) {
    vi.spyOn(window, "getComputedStyle").mockImplementation(
      () =>
        ({
          getPropertyValue: (name: string) =>
            name === "--color-header" ||
            name === "--color-bg" ||
            name === "--color-surface-sunken"
              ? colors[currentTheme() === "light" ? "light" : "dark"]
              : "",
        }) as CSSStyleDeclaration,
    );
  }

  test("тема Telegram меняется — меняется и палитра", () => {
    openInTelegram({ bg_color: "#ffffff" });
    setupTelegram();
    watchTelegramTheme();
    expect(currentTheme()).toBe("light");

    emitEvent("theme_changed", { theme_params: { bg_color: "#17212b" } });

    expect(currentTheme()).toBe("dark");
  });

  test("шапка и фон Mini App красятся в цвета палитры, в том числе после смены темы", () => {
    stubHeaderColors({ dark: "#221a15", light: "#e6d8b3" });
    openInTelegram({ bg_color: "#ffffff" });
    setupTelegram();

    watchTelegramTheme();

    expect(miniApp.isMounted()).toBe(true);
    expect(miniApp.headerColor()).toBe("#e6d8b3");
    expect(miniApp.bgColor()).toBe("#e6d8b3");

    emitEvent("theme_changed", { theme_params: { bg_color: "#17212b" } });

    expect(miniApp.headerColor()).toBe("#221a15");
    expect(miniApp.bgColor()).toBe("#221a15");
  });

  test("тема клиента пришла без цвета фона: палитра остаётся прежней", () => {
    openInTelegram({ bg_color: "#ffffff" });
    setupTelegram();
    watchTelegramTheme();
    expect(currentTheme()).toBe("light");

    emitEvent("theme_changed", { theme_params: { text_color: "#000000" } });

    expect(currentTheme()).toBe("light");
  });

  test("цвет токена не в виде #rrggbb: шапку и фон не красим, остальное работает", () => {
    stubHeaderColors({ dark: "rgb(34, 26, 21)", light: "oklch(0.9 0.05 90)" });
    openInTelegram({ bg_color: "#ffffff" });
    setupTelegram();
    const header = vi.spyOn(miniApp, "setHeaderColor");
    const background = vi.spyOn(miniApp, "setBgColor");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    watchTelegramTheme();

    expect(currentTheme()).toBe("light");
    expect(header).not.toHaveBeenCalled();
    expect(background).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  test("Mini App не запущен (вне Telegram): палитра включается, SDK не трогаем", () => {
    stubHeaderColors({ dark: "#221a15", light: "#e6d8b3" });
    const header = vi.spyOn(miniApp, "setHeaderColor");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    applyTheme("light");

    expect(currentTheme()).toBe("light");
    expect(header).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  test("без запущенного SDK ничего не делает и не падает", () => {
    openInTelegram({ bg_color: "#ffffff" });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(() => watchTelegramTheme()).not.toThrow();

    expect(currentTheme()).toBeUndefined();
    expect(warn).not.toHaveBeenCalled();
  });

  test("сбой покраски шапки не ломает тему и не пишет текст ошибки в журнал", () => {
    openInTelegram({ bg_color: "#ffffff" });
    setupTelegram();
    stubHeaderColors({ dark: "#221a15", light: "#e6d8b3" });
    // В тексте ошибок SDK бывают параметры запуска целиком, то есть initData.
    vi.spyOn(miniApp, "mount").mockImplementation(() => {
      throw new Error(`Invalid launch params: ${INIT_DATA}`);
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(() => watchTelegramTheme()).not.toThrow();

    expect(currentTheme()).toBe("light");
    expect(JSON.stringify(warn.mock.calls)).not.toContain("query_id");
  });
});
