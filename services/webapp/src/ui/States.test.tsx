import { fireEvent, render, screen, within } from "@testing-library/react";
import { Button } from "./Button";
import { ErrorState } from "./ErrorState";
import { Skeleton } from "./Skeleton";
import { EmptyState, Waiting } from "./States";

// Единые состояния экранов: загрузка — скелетон, пусто — с кнопкой следующего шага,
// ошибка — с «Повторить», долгое ожидание мастера — «Мастер думает…».

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

describe("Skeleton: загрузка", () => {
  test("статус с подписью для читалки, сами строки скрыты от неё", () => {
    render(<Skeleton label="Загружаем игры…" />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Загружаем игры…");
    const rows = status.querySelectorAll(".ui-skeleton__row");
    expect(rows).toHaveLength(3);
    for (const row of rows) expect(row).toHaveAttribute("aria-hidden", "true");
  });

  test("число строк задаётся", () => {
    render(<Skeleton label="Подключаемся к игре…" rows={2} />);

    expect(document.querySelectorAll(".ui-skeleton__row")).toHaveLength(2);
  });

  test("без движения: скелетон не мигает ни при какой настройке", () => {
    for (const reduce of [false, true]) {
      stubReducedMotion(reduce);
      const { unmount } = render(<Skeleton label="Загружаем…" />);
      expect(document.querySelector("[class*='ui-anim']")).toBeNull();
      unmount();
    }
  });
});

describe("ErrorState: ошибка с «Повторить»", () => {
  test("оповещение: заголовок и что делать; «Повторить» вызывает запрос снова", () => {
    const retry = vi.fn();
    render(
      <ErrorState title="Не удалось загрузить игры" onRetry={retry}>
        Нет связи с сервером.
      </ErrorState>,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Не удалось загрузить игры");
    expect(alert).toHaveTextContent("Нет связи с сервером.");
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  test("пока повтор идёт, кнопка занята и второй запрос не уходит", () => {
    const retry = vi.fn();
    render(
      <ErrorState title="Не удалось загрузить игры" onRetry={retry} busy>
        Нет связи.
      </ErrorState>,
    );

    const button = screen.getByRole("button", { name: "Повторить" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  test("значок — украшение: в имени и тексте оповещения его нет", () => {
    render(
      <ErrorState title="Не получилось войти" onRetry={() => {}}>
        Проверьте интернет.
      </ErrorState>,
    );

    const icon = screen.getByRole("alert").querySelector("svg");
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  test("текст ошибки с разметкой остаётся текстом", () => {
    render(
      <ErrorState title="Сбой" onRetry={() => {}}>
        {"<img src=x onerror=alert(1)>"}
      </ErrorState>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "<img src=x onerror=alert(1)>",
    );
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("EmptyState: пусто, но понятно, что делать дальше", () => {
  test("кнопка следующего шага стоит под пояснением", () => {
    const next = vi.fn();
    render(
      <EmptyState
        icon="candle"
        title="Пока нет игр"
        action={
          <Button variant="secondary" onClick={next}>
            Ввести код приглашения
          </Button>
        }
      >
        Войдите в игру друзей по коду приглашения.
      </EmptyState>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Ввести код приглашения" }),
    );
    expect(next).toHaveBeenCalledTimes(1);
  });
});

describe("Waiting: «Мастер думает…»", () => {
  test("статус с именем по заголовку, фраза и время ожидания", () => {
    render(
      <Waiting title="Мастер думает…" phrase="Подбирает слова…" seconds={25} />,
    );

    const card = screen.getByRole("status", { name: "Мастер думает…" });
    expect(within(card).getByText("Подбирает слова…")).toBeInTheDocument();
    expect(card).toHaveTextContent("Ждём 0:25");
  });

  test("счётчик не зачитывается каждую секунду: он скрыт от читалки", () => {
    render(<Waiting title="Мастер думает…" phrase="Точит перо…" seconds={3} />);

    expect(screen.getByText("Ждём 0:03")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  test("действия и пояснения — внутри карточки", () => {
    render(
      <Waiting title="Мастер думает…" phrase="Точит перо…" seconds={61}>
        <Button>Повторить</Button>
      </Waiting>,
    );

    expect(
      within(screen.getByRole("status", { name: "Мастер думает…" })).getByRole(
        "button",
        { name: "Повторить" },
      ),
    ).toBeInTheDocument();
  });

  test.each([
    [false, true],
    [true, false],
  ])("меньше движения: %s — свеча мерцает: %s", (reduce, animated) => {
    stubReducedMotion(reduce);
    render(<Waiting title="Мастер думает…" phrase="Точит перо…" seconds={0} />);

    expect(document.querySelector(".ui-anim-flicker") !== null).toBe(animated);
  });
});
