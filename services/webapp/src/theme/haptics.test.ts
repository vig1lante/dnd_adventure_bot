import { hapticFeedback, mockTelegramEnv } from "@tma.js/sdk-react";
import { setupTelegram } from "../auth/telegram";
import {
  VIBRATION_KEY,
  buzz,
  restoreSavedVibration,
  setVibrationEnabled,
  vibrationEnabled,
} from "./haptics";

const INIT_DATA =
  "query_id=AAHdF6IQ&user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Lira%22%7D" +
  "&auth_date=1662771648&signature=sig&hash=abc123";

// Порядок важен: сначала страница вне Telegram, потом — внутри (SDK запускается один раз за
// жизнь страницы, как в приложении).

describe("вне Telegram", () => {
  test("вибрации нет и ошибок нет: SDK не запущен", () => {
    const vibrate = vi.spyOn(hapticFeedback, "notificationOccurred");

    expect(() => buzz("your-turn")).not.toThrow();

    expect(vibrate).not.toHaveBeenCalled();
  });
});

describe("внутри Telegram", () => {
  const haptics: unknown[] = [];

  beforeAll(() => {
    mockTelegramEnv({
      launchParams: {
        tgWebAppData: INIT_DATA,
        tgWebAppVersion: "8.0",
        tgWebAppPlatform: "tdesktop",
      },
      onEvent: (event) => {
        if (event.name === "web_app_trigger_haptic_feedback") {
          haptics.push(event.params);
        }
      },
    });
    setupTelegram();
  });

  beforeEach(() => {
    haptics.length = 0;
  });

  // Выбор вибрации живёт в памяти страницы; каждый тест начинает как новая страница.
  afterEach(() => {
    restoreSavedVibration();
  });

  test.each([
    ["your-turn", "warning"],
    ["collecting", "warning"],
    ["rest", "success"],
  ] as const)("событие %s — короткая вибрация «%s»", (event, type) => {
    buzz(event);

    expect(haptics).toEqual([
      { type: "notification", notification_type: type },
    ]);
  });

  test("по умолчанию вибрация включена", () => {
    expect(vibrationEnabled()).toBe(true);
  });

  test("выключена настройкой — вибрации нет; включили снова — есть", () => {
    setVibrationEnabled(false);
    expect(window.localStorage.getItem(VIBRATION_KEY)).toBe("off");
    buzz("your-turn");
    expect(haptics).toEqual([]);

    setVibrationEnabled(true);
    expect(window.localStorage.getItem(VIBRATION_KEY)).toBeNull();
    buzz("your-turn");
    expect(haptics).toHaveLength(1);
  });

  test("хранилище недоступно (частный режим): по умолчанию вибрация включена, выбор действует до перезагрузки, без ошибок", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("запрещено", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("запрещено", "SecurityError");
    });

    expect(vibrationEnabled()).toBe(true);
    expect(() => setVibrationEnabled(false)).not.toThrow();
    buzz("rest");
    expect(haptics).toEqual([]);
  });

  test("старый клиент без вибрации: тихо пропускаем, игра не ломается", () => {
    vi.spyOn(hapticFeedback, "isSupported").mockReturnValue(false);

    expect(() => buzz("your-turn")).not.toThrow();
    expect(haptics).toEqual([]);
  });

  test("сбой SDK при вызове не выходит наружу и не пишется в консоль", () => {
    vi.spyOn(hapticFeedback, "notificationOccurred").mockImplementation(() => {
      throw new Error("initData=секрет");
    });
    const log = vi.spyOn(console, "error");
    const warn = vi.spyOn(console, "warn");

    expect(() => buzz("your-turn")).not.toThrow();
    expect(log).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  test.each(["OFF", "0", "false", "", "off "])(
    "чужое значение ключа «%s» — как по умолчанию: вибрация включена (выключает только «off»)",
    (value) => {
      window.localStorage.setItem(VIBRATION_KEY, value);

      expect(vibrationEnabled()).toBe(true);
      buzz("rest");
      expect(haptics).toHaveLength(1);
    },
  );

  test("включение без хранилища (removeItem бросает) не выходит наружу", () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("запрещено", "SecurityError");
    });

    expect(() => setVibrationEnabled(true)).not.toThrow();
  });

  // Бывший дефект: у вибрации не было состояния в памяти страницы, и при недоступном хранилище
  // выключенная вибрация всё равно звучала.
  test("запись в хранилище не удалась, а чтение работает (старый Safari в частном режиме): действует выбор игрока", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("переполнено", "QuotaExceededError");
    });

    setVibrationEnabled(false);
    buzz("your-turn");

    expect(window.localStorage.getItem(VIBRATION_KEY)).toBeNull();
    expect(vibrationEnabled()).toBe(false);
    expect(haptics).toEqual([]);
  });

  test("новая страница (запуск приложения): действует сохранённый выбор", () => {
    window.localStorage.setItem(VIBRATION_KEY, "off");

    restoreSavedVibration();

    expect(vibrationEnabled()).toBe(false);
  });

  test("хранилище недоступно: выключенная в настройках вибрация молчит до перезагрузки", () => {
    for (const method of ["getItem", "setItem", "removeItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("запрещено", "SecurityError");
      });
    }

    setVibrationEnabled(false);
    buzz("your-turn");

    expect(vibrationEnabled()).toBe(false);
    expect(haptics).toEqual([]);
  });
});
