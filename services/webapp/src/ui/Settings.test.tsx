import { fireEvent, render, screen } from "@testing-library/react";
import { VIBRATION_KEY, restoreSavedVibration } from "../theme/haptics";
import {
  CONTRAST_KEY,
  TEXT_SIZE_KEY,
  applySavedSettings,
} from "../theme/settings";
import { Settings, SettingsButton } from ".";

// Настройки интерфейса: размер текста, высокий контраст и вибрация. Хранятся только на этом
// телефоне; звука в приложении нет, «меньше движения» берётся из системы.

const root = document.documentElement;

afterEach(() => {
  delete root.dataset.textSize;
  delete root.dataset.contrast;
  // Выбор вибрации живёт в памяти страницы; каждый тест — как новая страница.
  restoreSavedVibration();
});

const renderSettings = (onClose = vi.fn()) => {
  render(<Settings onClose={onClose} />);
  return { onClose };
};

const size = (name: string) => screen.getByRole("radio", { name });
const toggle = (name: string) => screen.getByRole("switch", { name });

describe("панель настроек", () => {
  test("раздел «Настройки»: размер текста, контраст, вибрация; звука нет", () => {
    renderSettings();

    expect(
      screen.getByRole("region", { name: "Настройки" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("radiogroup", { name: "Размер текста" }),
    ).toBeInTheDocument();
    for (const name of ["Обычный", "Крупный", "Очень крупный"]) {
      expect(size(name)).toBeInTheDocument();
    }
    expect(toggle("Высокий контраст")).toBeInTheDocument();
    expect(toggle("Вибрация")).toBeInTheDocument();
    expect(screen.queryByText(/звук/i)).toBeNull();
  });

  test("«меньше движения» объяснено словами: оно по настройке телефона, переключателя нет", () => {
    renderSettings();

    expect(
      screen.getByText(/Меньше движения — по настройке телефона/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /движени/i })).toBeNull();
  });

  test("по умолчанию: обычный текст, контраст выключен, вибрация включена", () => {
    renderSettings();

    expect(size("Обычный")).toBeChecked();
    expect(toggle("Высокий контраст")).not.toBeChecked();
    expect(toggle("Вибрация")).toBeChecked();
  });

  test("показывает сохранённое: после запуска приложения панель открывается с прежним выбором", () => {
    window.localStorage.setItem(TEXT_SIZE_KEY, "huge");
    window.localStorage.setItem(CONTRAST_KEY, "high");
    window.localStorage.setItem(VIBRATION_KEY, "off");
    applySavedSettings(); // так делает App при запуске

    renderSettings();

    expect(size("Очень крупный")).toBeChecked();
    expect(toggle("Высокий контраст")).toBeChecked();
    expect(toggle("Вибрация")).not.toBeChecked();
  });
});

describe("размер текста", () => {
  test("выбор меняет атрибут на корне (по нему меняются токены --text-*) и запоминается", () => {
    renderSettings();

    fireEvent.click(size("Крупный"));

    expect(size("Крупный")).toBeChecked();
    expect(root.dataset.textSize).toBe("large");
    expect(window.localStorage.getItem(TEXT_SIZE_KEY)).toBe("large");

    fireEvent.click(size("Обычный"));

    expect(root.dataset.textSize).toBeUndefined();
    expect(window.localStorage.getItem(TEXT_SIZE_KEY)).toBeNull();
  });

  test("образец рассказа мастера набран как рассказ: видно, каким станет текст", () => {
    const { container } = render(<Settings onClose={() => {}} />);

    expect(container.querySelector(".ui-narration")).toHaveTextContent(
      /рассказ мастера/,
    );
  });
});

describe("высокий контраст и вибрация", () => {
  test("«Высокий контраст» ставит data-contrast=high и запоминается", () => {
    renderSettings();

    fireEvent.click(toggle("Высокий контраст"));

    expect(toggle("Высокий контраст")).toBeChecked();
    expect(root.dataset.contrast).toBe("high");
    expect(window.localStorage.getItem(CONTRAST_KEY)).toBe("high");

    fireEvent.click(toggle("Высокий контраст"));

    expect(root.dataset.contrast).toBeUndefined();
  });

  test("«Вибрация» выключается и включается через общий ключ вибрации", () => {
    renderSettings();

    fireEvent.click(toggle("Вибрация"));

    expect(toggle("Вибрация")).not.toBeChecked();
    expect(window.localStorage.getItem(VIBRATION_KEY)).toBe("off");

    fireEvent.click(toggle("Вибрация"));

    expect(window.localStorage.getItem(VIBRATION_KEY)).toBeNull();
  });

  test("у переключателей есть пояснения, связанные с ними", () => {
    renderSettings();

    expect(toggle("Высокий контраст")).toHaveAccessibleDescription(
      /Текст ярче, рамки и кнопки чётче/,
    );
    expect(toggle("Вибрация")).toHaveAccessibleDescription(/Только в Telegram/);
  });

  test("подсказка «Вибрация» зовёт по делу: «когда пора сдать ход», без обещания хосту", () => {
    renderSettings();

    expect(toggle("Вибрация")).toHaveAccessibleDescription(
      /когда пора сдать ход/,
    );
    expect(toggle("Вибрация")).not.toHaveAccessibleDescription(/хост/);
  });
});

describe("закрытие", () => {
  test("при открытии фокус на панели: читалка объявит «Настройки»", () => {
    renderSettings();

    expect(screen.getByRole("region", { name: "Настройки" })).toHaveFocus();
  });

  test("«Готово» и Escape закрывают панель", () => {
    const { onClose } = renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Готово" }));
    fireEvent.keyDown(screen.getByRole("region", { name: "Настройки" }), {
      key: "Escape",
    });

    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("кнопка настроек в шапке", () => {
  test("имя «Настройки», состояние раскрытия и ссылка на панель", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <SettingsButton expanded={false} controls="settings" onClick={onClick} />,
    );
    const button = screen.getByRole("button", { name: "Настройки" });

    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAttribute("aria-controls", "settings");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(<SettingsButton expanded controls="settings" onClick={onClick} />);
    expect(button).toHaveAttribute("aria-expanded", "true");
  });

  test("общая кнопка: область нажатия не меньше --tap-size", () => {
    render(
      <SettingsButton
        expanded={false}
        controls="settings"
        onClick={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Настройки" })).toHaveClass(
      "ui-button",
    );
  });
});

// --- финальное покрытие блока B10 (B10.coverage): сбой хранилища не ломает панель ---

describe("хранилище недоступно (частный режим)", () => {
  function breakStorage() {
    for (const method of ["getItem", "setItem", "removeItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("запрещено", "SecurityError");
      });
    }
  }

  test("панель открывается с умолчаниями, размер и контраст действуют на странице, ошибок нет", () => {
    breakStorage();
    const error = vi.spyOn(console, "error");
    const warn = vi.spyOn(console, "warn");

    renderSettings();
    expect(size("Обычный")).toBeChecked();
    expect(toggle("Высокий контраст")).not.toBeChecked();
    expect(toggle("Вибрация")).toBeChecked();

    fireEvent.click(size("Очень крупный"));
    fireEvent.click(toggle("Высокий контраст"));

    expect(size("Очень крупный")).toBeChecked();
    expect(root.dataset.textSize).toBe("huge");
    expect(toggle("Высокий контраст")).toBeChecked();
    expect(root.dataset.contrast).toBe("high");
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });
});

// --- B10.3.fix1 (R1 = D1 = S1): панель показывает то, что действует на странице ---

describe("панель показывает действующий выбор, а не хранилище", () => {
  function breakStorage() {
    for (const method of ["getItem", "setItem", "removeItem"] as const) {
      vi.spyOn(Storage.prototype, method).mockImplementation(() => {
        throw new DOMException("запрещено", "SecurityError");
      });
    }
  }

  test("хранилище недоступно: при повторном открытии панель показывает выбранное", () => {
    breakStorage();
    const first = render(<Settings onClose={() => {}} />);
    fireEvent.click(size("Очень крупный"));
    fireEvent.click(toggle("Высокий контраст"));
    fireEvent.click(toggle("Вибрация"));
    first.unmount();

    renderSettings();

    expect(size("Очень крупный")).toBeChecked();
    expect(toggle("Высокий контраст")).toBeChecked();
    expect(toggle("Вибрация")).not.toBeChecked();
  });

  test("хранилище недоступно: «Обычный» после «Очень крупного» снова возвращает обычный размер", () => {
    breakStorage();
    const first = render(<Settings onClose={() => {}} />);
    fireEvent.click(size("Очень крупный"));
    first.unmount();
    renderSettings();

    fireEvent.click(size("Обычный"));

    expect(size("Обычный")).toBeChecked();
    expect(root.dataset.textSize).toBeUndefined();
  });

  test("на странице действует не то, что в хранилище: панель верит странице", () => {
    window.localStorage.setItem(TEXT_SIZE_KEY, "huge");
    root.dataset.textSize = "large";

    renderSettings();

    expect(size("Крупный")).toBeChecked();
  });
});
