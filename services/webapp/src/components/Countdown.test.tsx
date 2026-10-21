import { act, render, screen } from "@testing-library/react";
import { SERVER_TIME, phaseOf } from "./fixtures";
import { Countdown } from "./Countdown";

const NOW = Date.parse(SERVER_TIME);
const at = (secondsFromNow: number) =>
  new Date(NOW + secondsFromNow * 1000).toISOString();

const timer = () => screen.getByRole("timer");
const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

describe("показ времени", () => {
  test("считает от серверного срока: «1:30»", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(90) })}
        clockOffsetMs={0}
        label="До конца сбора"
      />,
    );

    expect(timer()).toHaveAccessibleName("До конца сбора 1:30");
    expect(timer()).toHaveTextContent("1:30");
  });

  test("тикает раз в секунду и не уходит ниже нуля", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(3) })}
        clockOffsetMs={0}
      />,
    );
    expect(timer()).toHaveTextContent("0:03");

    tick(1000);
    expect(timer()).toHaveTextContent("0:02");
    tick(2000);
    expect(timer()).toHaveTextContent("0:00");
    tick(10_000);
    expect(timer()).toHaveTextContent("0:00");
  });

  test("часы телефона отстают на 3 минуты: срок по серверу всё равно верный", () => {
    // Сервер впереди часов браузера на 3 минуты; по его часам до срока 2 минуты.
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(300) })}
        clockOffsetMs={180_000}
      />,
    );

    expect(timer()).toHaveTextContent("2:00");
  });

  test("часы телефона спешат на 7 минут", () => {
    // Сервер позади часов браузера на 7 минут; по его часам до срока минута.
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(-360) })}
        clockOffsetMs={-7 * 60_000}
      />,
    );

    expect(timer()).toHaveTextContent("1:00");
  });

  test("время считается по часам, а не по числу тиков: после «заморозки» вкладки верно", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(90) })}
        clockOffsetMs={0}
      />,
    );

    vi.setSystemTime(NOW + 30_000); // вкладка спала, таймеры не сработали
    tick(250);

    expect(timer()).toHaveTextContent("1:00");
  });

  test("новый срок считается от текущих часов, даже если тик ещё не сработал", () => {
    const { rerender } = render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(90) })}
        clockOffsetMs={0}
      />,
    );

    vi.setSystemTime(NOW + 10_000); // время прошло, а таймер не успел сработать
    rerender(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(10 + 150) })}
        clockOffsetMs={0}
      />,
    );

    expect(timer()).toHaveTextContent("2:30");
  });

  test("новый срок («+1 минута») применяется сразу", () => {
    const { rerender } = render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(90) })}
        clockOffsetMs={0}
      />,
    );

    rerender(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(150) })}
        clockOffsetMs={0}
      />,
    );

    expect(timer()).toHaveTextContent("2:30");
  });
});

describe("пауза и отсутствие отсчёта", () => {
  test("на паузе стоит остаток и горит метка «Пауза»", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", {
          phase: "paused",
          deadline_at: null,
          remaining_seconds: 75,
        })}
        clockOffsetMs={0}
      />,
    );

    expect(timer()).toHaveTextContent("1:15");
    expect(screen.getByText("Пауза")).toBeInTheDocument();
    tick(30_000);
    expect(timer()).toHaveTextContent("1:15");
  });

  test("на паузе таймеры не заводятся", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", {
          phase: "paused",
          deadline_at: null,
          remaining_seconds: 75,
        })}
        clockOffsetMs={0}
      />,
    );

    expect(vi.getTimerCount()).toBe(0);
  });

  test("ни срока, ни остатка — ничего не рисуется и таймеров нет", () => {
    const { container } = render(
      <Countdown phase={phaseOf("discussion")} clockOffsetMs={0} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(vi.getTimerCount()).toBe(0);
  });

  test("после «Продолжить» остаток снова бежит от нового срока", () => {
    const paused = phaseOf("collecting", {
      phase: "paused",
      deadline_at: null,
      remaining_seconds: 75,
    });
    const { rerender } = render(<Countdown phase={paused} clockOffsetMs={0} />);

    rerender(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(75) })}
        clockOffsetMs={0}
      />,
    );
    tick(5000);

    expect(timer()).toHaveTextContent("1:10");
    expect(screen.queryByText("Пауза")).toBeNull();
  });
});

describe("воск и очистка", () => {
  test("воск тает по доле от полного срока из игры", () => {
    const { container } = render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(60) })}
        clockOffsetMs={0}
        totalSeconds={120}
      />,
    );

    const wax = container.querySelector(".ui-countdown__wax > span");
    expect((wax as HTMLElement).style.width).toBe("50%");
  });

  test("без полного срока полосы воска нет", () => {
    const { container } = render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(60) })}
        clockOffsetMs={0}
      />,
    );

    expect(container.querySelector(".ui-countdown__wax")).toBeNull();
  });

  test("при уходе с экрана таймер снимается", () => {
    const { unmount } = render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(60) })}
        clockOffsetMs={0}
      />,
    );
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  test("когда срок вышел, таймер не крутится вхолостую", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(2) })}
        clockOffsetMs={0}
      />,
    );

    tick(5000);

    expect(timer()).toHaveTextContent("0:00");
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("последняя секунда", () => {
  test("отсчёт сам дотикивает с «0:01» до «0:00», а не замирает на единице", () => {
    render(
      <Countdown
        phase={phaseOf("collecting", { deadline_at: at(2) })}
        clockOffsetMs={0}
      />,
    );
    expect(timer()).toHaveTextContent("0:02");

    tick(1000);
    expect(timer()).toHaveTextContent("0:01");
    tick(1000);

    expect(timer()).toHaveTextContent("0:00");
  });
});
