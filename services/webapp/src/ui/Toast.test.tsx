import { act, fireEvent, render, screen } from "@testing-library/react";
import { Button } from "./Button";
import { Toast } from "./Toast";
import type { ToastMessage } from "./Toast";

const TURN: ToastMessage = {
  title: "Твой ход",
  text: "Бой идёт: открой вкладку «Бой».",
};

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      media: query,
      matches: query === "(prefers-reduced-motion: reduce)" && reduce,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

describe("Toast: уведомление о событии", () => {
  test("вежливая живая область с заголовком и пояснением", () => {
    render(<Toast message={TURN} onDismiss={() => {}} />);

    const toast = screen.getByRole("status", { name: "Твой ход" });
    expect(toast).toHaveTextContent("Бой идёт: открой вкладку «Бой».");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("без сообщения живая область остаётся на месте пустой: следующее объявится", () => {
    const { rerender } = render(<Toast message={null} onDismiss={() => {}} />);
    const region = screen.getByRole("status");
    expect(region).toBeEmptyDOMElement();

    rerender(<Toast message={TURN} onDismiss={() => {}} />);

    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent("Твой ход");
  });

  test("действие уведомления — обычная кнопка", () => {
    const go = vi.fn();
    render(
      <Toast
        message={TURN}
        onDismiss={() => {}}
        action={
          <Button variant="secondary" onClick={go}>
            К бою
          </Button>
        }
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "К бою" }));

    expect(go).toHaveBeenCalledTimes(1);
  });

  test("уходит само через заданное время, новое сообщение отсчитывает заново", () => {
    vi.useFakeTimers();
    const dismiss = vi.fn();
    const { rerender } = render(
      <Toast message={TURN} onDismiss={dismiss} duration={5000} />,
    );

    act(() => vi.advanceTimersByTime(4000));
    rerender(
      <Toast
        message={{ title: "Привал", text: "Отряд у костра." }}
        onDismiss={dismiss}
        duration={5000}
      />,
    );
    act(() => vi.advanceTimersByTime(4000));
    expect(dismiss).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1000));
    expect(dismiss).toHaveBeenCalledTimes(1);
  });

  test("текст сообщения с разметкой остаётся текстом", () => {
    render(
      <Toast
        message={{ title: "<b>Твой ход</b>", text: "<img src=x>" }}
        onDismiss={() => {}}
      />,
    );

    expect(screen.getByText("<b>Твой ход</b>")).toBeInTheDocument();
    expect(document.querySelector(".ui-toast b, .ui-toast img")).toBeNull();
  });

  test.each([
    [false, true],
    [true, false],
  ])("меньше движения: %s — плавное появление: %s", (reduce, animated) => {
    stubReducedMotion(reduce);
    render(<Toast message={TURN} onDismiss={() => {}} />);

    const toast = screen.getByText("Твой ход").closest(".ui-toast");
    expect(toast?.classList.contains("ui-anim-appear")).toBe(animated);
  });
});

// --- B10.3.fix1 (R10): тост можно убрать, не дожидаясь, пока он уйдёт сам ---

describe("кнопка «Скрыть»", () => {
  test("у тоста есть «Скрыть»: общая кнопка (область нажатия ≥ 44 px), нажатие убирает тост", () => {
    const dismiss = vi.fn();
    render(<Toast message={TURN} onDismiss={dismiss} />);

    const hide = screen.getByRole("button", { name: "Скрыть" });
    // Общая кнопка, но квадратная (стиль .ui-toast__close в ui.css).
    expect(hide).toHaveClass("ui-button", "ui-toast__close");
    fireEvent.click(hide);

    expect(dismiss).toHaveBeenCalledTimes(1);
  });

  test("«Скрыть» стоит рядом с действием и не мешает ему", () => {
    const go = vi.fn();
    const dismiss = vi.fn();
    render(
      <Toast
        message={TURN}
        onDismiss={dismiss}
        action={
          <Button variant="secondary" onClick={go}>
            К бою
          </Button>
        }
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "К бою" }));

    expect(go).toHaveBeenCalledTimes(1);
    expect(dismiss).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Скрыть" })).toBeInTheDocument();
  });

  test("без сообщения кнопки нет", () => {
    render(<Toast message={null} onDismiss={() => {}} />);

    expect(screen.queryByRole("button", { name: "Скрыть" })).toBeNull();
  });
});
