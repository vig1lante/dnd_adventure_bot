import {
  restoreSavedVibration,
  setVibrationEnabled,
  vibrationEnabled,
} from "./haptics";
import {
  CONTRAST_KEY,
  TEXT_SIZE_KEY,
  applySavedSettings,
  currentHighContrast,
  currentTextSize,
  savedHighContrast,
  savedTextSize,
  setHighContrast,
  setTextSize,
} from "./settings";

// Настройки интерфейса живут в localStorage и действуют атрибутами на корне страницы: стили
// (theme.css, tokens.css) по ним меняют токены кегля и пары цветов.

const root = document.documentElement;

afterEach(() => {
  delete root.dataset.textSize;
  delete root.dataset.contrast;
  restoreSavedVibration();
});

/** Хранилище недоступно (частный режим, запрет сайта): любое обращение бросает. */
function breakStorage() {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("SecurityError");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("SecurityError");
  });
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
    throw new Error("SecurityError");
  });
}

describe("размер текста", () => {
  test("по умолчанию обычный, атрибута нет", () => {
    expect(savedTextSize()).toBe("normal");
    applySavedSettings();
    expect(root.dataset.textSize).toBeUndefined();
  });

  test.each(["large", "huge"] as const)(
    "«%s»: атрибут на корне и запись в хранилище",
    (size) => {
      setTextSize(size);

      expect(root.dataset.textSize).toBe(size);
      expect(window.localStorage.getItem(TEXT_SIZE_KEY)).toBe(size);
      expect(savedTextSize()).toBe(size);
    },
  );

  test("обычный снимает атрибут и забывает выбор", () => {
    setTextSize("huge");

    setTextSize("normal");

    expect(root.dataset.textSize).toBeUndefined();
    expect(window.localStorage.getItem(TEXT_SIZE_KEY)).toBeNull();
  });

  test("переживает перезагрузку: при запуске выбор из хранилища снова на корне", () => {
    setTextSize("large");
    delete root.dataset.textSize; // новая страница: атрибутов ещё нет

    applySavedSettings();

    expect(root.dataset.textSize).toBe("large");
  });

  test("чужое значение в хранилище не попадает в атрибут: размер обычный", () => {
    window.localStorage.setItem(TEXT_SIZE_KEY, '"><img src=x>');

    applySavedSettings();

    expect(savedTextSize()).toBe("normal");
    expect(root.dataset.textSize).toBeUndefined();
  });
});

describe("высокий контраст", () => {
  test("по умолчанию выключен", () => {
    expect(savedHighContrast()).toBe(false);
    applySavedSettings();
    expect(root.dataset.contrast).toBeUndefined();
  });

  test("включение: атрибут data-contrast=high и запись; выключение снимает оба", () => {
    setHighContrast(true);
    expect(root.dataset.contrast).toBe("high");
    expect(window.localStorage.getItem(CONTRAST_KEY)).toBe("high");

    setHighContrast(false);
    expect(root.dataset.contrast).toBeUndefined();
    expect(window.localStorage.getItem(CONTRAST_KEY)).toBeNull();
  });

  test("переживает перезагрузку", () => {
    setHighContrast(true);
    delete root.dataset.contrast;

    applySavedSettings();

    expect(root.dataset.contrast).toBe("high");
  });
});

describe("хранилище недоступно", () => {
  test("чтение — значения по умолчанию, без ошибок", () => {
    breakStorage();

    expect(savedTextSize()).toBe("normal");
    expect(savedHighContrast()).toBe(false);
    expect(() => applySavedSettings()).not.toThrow();
  });

  test("запись не удалась — настройка всё равно действует на этой странице", () => {
    breakStorage();

    expect(() => setTextSize("huge")).not.toThrow();
    expect(() => setHighContrast(true)).not.toThrow();

    expect(root.dataset.textSize).toBe("huge");
    expect(root.dataset.contrast).toBe("high");
  });
});

// --- белый список значений хранилища ---

describe("значение из хранилища сверяется с белым списком", () => {
  test.each(["normal", "LARGE", "huge ", "small", "", "large; color:red"])(
    "размер текста «%s» — обычный, атрибута нет",
    (value) => {
      window.localStorage.setItem(TEXT_SIZE_KEY, value);

      applySavedSettings();

      expect(savedTextSize()).toBe("normal");
      expect(root.dataset.textSize).toBeUndefined();
    },
  );

  test.each(["HIGH", "true", "1", " high", "high\n", '"><img src=x>'])(
    "контраст «%s» — обычный, атрибута нет",
    (value) => {
      window.localStorage.setItem(CONTRAST_KEY, value);

      applySavedSettings();

      expect(savedHighContrast()).toBe(false);
      expect(root.dataset.contrast).toBeUndefined();
    },
  );

  test("чужое значение при запуске снимает атрибут, оставшийся от прежнего выбора", () => {
    setTextSize("huge");
    setHighContrast(true);
    window.localStorage.setItem(TEXT_SIZE_KEY, "giant");
    window.localStorage.setItem(CONTRAST_KEY, "max");

    applySavedSettings();

    expect(root.dataset.textSize).toBeUndefined();
    expect(root.dataset.contrast).toBeUndefined();
  });
});

// --- источник правды — память страницы, а не хранилище ---

describe("действующие настройки этой страницы", () => {
  test("размер и контраст читаются из атрибутов корня: сломанное хранилище их не сбивает", () => {
    breakStorage();

    setTextSize("huge");
    setHighContrast(true);

    expect(savedTextSize()).toBe("normal");
    expect(savedHighContrast()).toBe(false);
    expect(currentTextSize()).toBe("huge");
    expect(currentHighContrast()).toBe(true);
  });

  test("без атрибутов — обычный размер и обычный контраст", () => {
    expect(currentTextSize()).toBe("normal");
    expect(currentHighContrast()).toBe(false);
  });

  test("чужое значение атрибута — обычный размер", () => {
    root.dataset.textSize = "gigantic";

    expect(currentTextSize()).toBe("normal");
  });

  test("запуск приложения возвращает и вибрацию к сохранённому выбору", () => {
    setVibrationEnabled(false);
    window.localStorage.removeItem("dnd-ui-vibration");
    expect(vibrationEnabled()).toBe(false); // выбор этой страницы

    applySavedSettings();

    expect(vibrationEnabled()).toBe(true);
  });
});
