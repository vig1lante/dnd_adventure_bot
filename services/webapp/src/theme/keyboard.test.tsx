import { act, fireEvent, render, screen } from "@testing-library/react";
import { useOnScreenKeyboard, useOnScreenKeyboardField } from "./keyboard";

function stubPointer(coarse: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      media: query,
      matches: query === "(pointer: coarse)" && coarse,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

function Probe() {
  const open = useOnScreenKeyboard();
  return (
    <>
      <p>{open ? "клавиатура открыта" : "клавиатуры нет"}</p>
      <textarea aria-label="Ход" />
      <input aria-label="Код" />
      <input aria-label="Флажок" type="checkbox" />
      <button type="button">Готов</button>
    </>
  );
}

const focus = (element: HTMLElement) => act(() => element.focus());
const blur = (element: HTMLElement) => act(() => element.blur());

describe("useOnScreenKeyboard: открыта ли клавиатура телефона", () => {
  test("сенсорный экран: поле ввода в фокусе — клавиатура открыта, ушёл фокус — нет", () => {
    stubPointer(true);
    render(<Probe />);
    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();

    focus(screen.getByLabelText("Ход"));
    expect(screen.getByText("клавиатура открыта")).toBeInTheDocument();

    blur(screen.getByLabelText("Ход"));
    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();

    focus(screen.getByLabelText("Код"));
    expect(screen.getByText("клавиатура открыта")).toBeInTheDocument();
  });

  test("кнопка и флажок клавиатуру не открывают", () => {
    stubPointer(true);
    render(<Probe />);

    focus(screen.getByRole("button", { name: "Готов" }));
    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
    focus(screen.getByLabelText("Флажок"));
    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });

  test("мышь и настоящая клавиатура (компьютер): панель не прячется", () => {
    stubPointer(false);
    render(<Probe />);

    focus(screen.getByLabelText("Ход"));

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });

  test("без matchMedia (старый WebView, jsdom) клавиатура не угадывается", () => {
    render(<Probe />);

    focus(screen.getByLabelText("Ход"));

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });
});

// --- раскладка не меняется посреди касания ---
//
// На телефоне тап по кнопке под полем ввода забирает у поля фокус ещё до того, как палец
// отпущен. Если в этот миг вернуть нижнюю панель и вывести поле из компактного вида, кнопка
// уезжает из-под пальца и нажатие теряется. Поэтому, пока идёт касание, состояние клавиатуры
// не меняется, а обновляется после его конца (щелчка), отмены или по сроку.

describe("useOnScreenKeyboard: касание пальцем", () => {
  const field = () => screen.getByLabelText("Ход");
  const ready = () => screen.getByRole("button", { name: "Готов" });

  test("тап по кнопке при открытой клавиатуре: до щелчка клавиатура «открыта», после — закрыта", () => {
    stubPointer(true);
    render(<Probe />);
    focus(field());

    fireEvent.pointerDown(ready());
    focus(ready()); // фокус ушёл из поля посреди нажатия
    expect(screen.getByText("клавиатура открыта")).toBeInTheDocument();

    fireEvent.click(ready());

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });

  test("тап по полю: клавиатура «открывается» после щелчка, а не посреди касания", () => {
    stubPointer(true);
    render(<Probe />);

    fireEvent.pointerDown(field());
    focus(field());
    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();

    fireEvent.click(field());

    expect(screen.getByText("клавиатура открыта")).toBeInTheDocument();
  });

  test("касание отменилось (началась прокрутка): состояние обновляется сразу", () => {
    stubPointer(true);
    render(<Probe />);
    focus(field());

    fireEvent.pointerDown(ready());
    focus(ready());
    fireEvent.pointerCancel(ready());

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });

  test("щелчка так и не было (долгое нажатие): состояние обновляется по сроку", () => {
    vi.useFakeTimers();
    stubPointer(true);
    render(<Probe />);
    focus(field());

    fireEvent.pointerDown(ready());
    focus(ready());
    act(() => vi.advanceTimersByTime(1500));

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });

  test("без касания (фокус с клавиатуры или из кода) — сразу, как раньше", () => {
    stubPointer(true);
    render(<Probe />);
    focus(field());

    focus(ready());

    expect(screen.getByText("клавиатуры нет")).toBeInTheDocument();
  });
});

describe("useOnScreenKeyboardField: какое поле открыло клавиатуру", () => {
  function FieldProbe() {
    const field = useOnScreenKeyboardField();
    return (
      <>
        <p>
          {field === null
            ? "нет поля"
            : `поле: ${field.getAttribute("aria-label")}`}
        </p>
        <textarea aria-label="Ход" />
        <input aria-label="Код" />
      </>
    );
  }

  test("называет поле в фокусе; без поля — null", () => {
    stubPointer(true);
    render(<FieldProbe />);
    expect(screen.getByText("нет поля")).toBeInTheDocument();

    focus(screen.getByLabelText("Код"));
    expect(screen.getByText("поле: Код")).toBeInTheDocument();

    focus(screen.getByLabelText("Ход"));
    expect(screen.getByText("поле: Ход")).toBeInTheDocument();

    blur(screen.getByLabelText("Ход"));
    expect(screen.getByText("нет поля")).toBeInTheDocument();
  });
});
