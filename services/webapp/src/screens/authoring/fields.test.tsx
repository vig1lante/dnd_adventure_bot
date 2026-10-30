import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { AlertDialog } from "./fields";
import { dropFocus } from "./fixtures";

/**
 * Экран с вопросом: кнопка, которая его задаёт, поле для ввода и кнопки, которые закрывают
 * вопрос со стороны экрана или убирают саму кнопку-источник.
 */
function Screen() {
  const [asking, setAsking] = useState(false);
  const [opener, setOpener] = useState(true);
  return (
    <>
      {opener && <button onClick={() => setAsking(true)}>Спросить</button>}
      <input aria-label="Поле" />
      <button onClick={() => setOpener(false)}>Убрать «Спросить»</button>
      <button onClick={() => setAsking(false)}>
        Закрыть со стороны экрана
      </button>
      {asking && (
        <AlertDialog
          title="Точно?"
          actions={<button onClick={() => setAsking(false)}>Нет</button>}
        >
          <p>Пояснение.</p>
        </AlertDialog>
      )}
    </>
  );
}

const button = (name: string) => screen.getByRole("button", { name });
const question = () => screen.getByRole("alertdialog", { name: "Точно?" });

/** Нажатие с фокусом, как в браузере: в jsdom щелчок сам фокус не переносит. */
function press(name: string) {
  button(name).focus();
  fireEvent.click(button(name));
}

describe("AlertDialog: фокус после ответа", () => {
  test("ответ на вопрос возвращает фокус на кнопку, которая его задала", () => {
    render(<Screen />);
    press("Спросить");
    expect(question()).toHaveFocus();

    press("Нет");

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(button("Спросить")).toHaveFocus();
  });

  test("кнопки-источника уже нет — фокус никуда не переносится и ошибок нет", () => {
    const error = vi.spyOn(console, "error");
    render(<Screen />);
    press("Спросить");
    // Источник исчез, пока вопрос открыт (экран перестроился).
    fireEvent.click(button("Убрать «Спросить»"));

    press("Нет");

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.body).toHaveFocus();
    expect(error).not.toHaveBeenCalled();
  });

  test("автор сам ушёл в другое поле — закрытие вопроса фокус у него не забирает", () => {
    render(<Screen />);
    press("Спросить");
    screen.getByRole("textbox", { name: "Поле" }).focus();

    fireEvent.click(button("Закрыть со стороны экрана"));

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Поле" })).toHaveFocus();
  });

  test("до вопроса фокуса не было — после ответа его тоже нет", () => {
    render(<Screen />);
    fireEvent.click(button("Спросить"));
    expect(question()).toHaveFocus();

    press("Нет");

    expect(document.body).toHaveFocus();
  });
});

/**
 * Экран с действием через сервер: «Сохранить» выключается на время запроса (как форма
 * редактора), и только потом появляется вопрос. Источник вопроса экран передаёт явно.
 */
function SavingScreen() {
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState<{ from: HTMLElement | null } | null>(null);
  return (
    <>
      <button
        disabled={busy}
        onClick={(event) => {
          setAsked(null);
          setBusy(true);
          // Источник фиксируется до запроса: выключенная кнопка фокус потеряет.
          const from = event.currentTarget;
          setTimeout(() => {
            setBusy(false);
            setAsked({ from });
          }, 0);
        }}
      >
        Сохранить
      </button>
      <button onClick={() => setBusy(true)}>Выключить «Сохранить»</button>
      <button onClick={() => setBusy(false)}>Включить «Сохранить»</button>
      <input aria-label="Поле" />
      {asked !== null && (
        <AlertDialog
          title="Точно?"
          returnFocus={asked.from}
          actions={<button onClick={() => setAsked(null)}>Нет</button>}
        >
          <p>Пояснение.</p>
        </AlertDialog>
      )}
    </>
  );
}

describe("AlertDialog: источник, который потерял фокус на время запроса", () => {
  /** «Сохранить» с фокусом; пока запрос в пути, кнопка выключена и, как в браузере, без фокуса. */
  async function askAfterSave() {
    render(<SavingScreen />);
    button("Сохранить").focus();
    fireEvent.click(button("Сохранить"));
    expect(button("Сохранить")).toBeDisabled();
    dropFocus();
    // Вопрос появляется после ответа «сервера» (таймер), его эффект — следом.
    await vi.waitFor(() => expect(question()).toHaveFocus());
  }

  test("ответ возвращает фокус на источник, хотя к появлению вопроса фокуса у него уже не было", async () => {
    await askAfterSave();

    press("Нет");

    expect(button("Сохранить")).toHaveFocus();
  });

  test("источник выключен в момент ответа — фокус вернётся, как только его включат", async () => {
    await askAfterSave();
    fireEvent.click(button("Выключить «Сохранить»"));

    press("Нет");
    expect(document.body).toHaveFocus();
    fireEvent.click(button("Включить «Сохранить»"));

    await vi.waitFor(() => expect(button("Сохранить")).toHaveFocus());
  });

  test("пока источник выключен, автор ушёл в поле — фокус у него не забирают", async () => {
    await askAfterSave();
    fireEvent.click(button("Выключить «Сохранить»"));
    press("Нет");

    screen.getByRole("textbox", { name: "Поле" }).focus();
    fireEvent.click(button("Включить «Сохранить»"));
    await Promise.resolve();

    expect(screen.getByRole("textbox", { name: "Поле" })).toHaveFocus();
  });
});
