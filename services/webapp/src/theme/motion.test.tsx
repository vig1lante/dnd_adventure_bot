import { act, render, screen } from "@testing-library/react";
import { motionClass, useMotionClass, useReducedMotion } from "./motion";

// Системная настройка «меньше движения»; её можно переключить, пока компонент на экране.
function stubReducedMotion(initial: boolean) {
  let reduce = initial;
  const listeners = new Set<() => void>();
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      media: query,
      get matches() {
        return query === "(prefers-reduced-motion: reduce)" && reduce;
      },
      addEventListener: (_: string, listener: () => void) =>
        listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) =>
        listeners.delete(listener),
    })),
  );
  return {
    listeners,
    set(next: boolean) {
      reduce = next;
      listeners.forEach((listener) => listener());
    },
  };
}

function Probe() {
  const reduced = useReducedMotion();
  const className = useMotionClass("ui-anim-appear");
  return (
    <p data-testid="probe" className={className}>
      {reduced ? "reduced" : "full"}
    </p>
  );
}

describe("motionClass", () => {
  test("класс анимации применяется, если движение разрешено", () => {
    expect(motionClass("ui-anim-roll", false)).toBe("ui-anim-roll");
  });

  test("при reduced motion класса нет", () => {
    expect(motionClass("ui-anim-roll", true)).toBe("");
  });
});

describe("useReducedMotion / useMotionClass", () => {
  test("без matchMedia (jsdom) движение разрешено", () => {
    render(<Probe />);

    expect(screen.getByTestId("probe")).toHaveTextContent("full");
    expect(screen.getByTestId("probe")).toHaveClass("ui-anim-appear");
  });

  test("prefers-reduced-motion: reduce — классов анимации нет", () => {
    stubReducedMotion(true);

    render(<Probe />);

    expect(screen.getByTestId("probe")).toHaveTextContent("reduced");
    expect(screen.getByTestId("probe")).not.toHaveClass("ui-anim-appear");
  });

  test("настройку можно сменить на лету; слушатель снимается при размонтировании", () => {
    const motion = stubReducedMotion(false);
    const { unmount } = render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveClass("ui-anim-appear");

    act(() => motion.set(true));
    expect(screen.getByTestId("probe")).not.toHaveClass("ui-anim-appear");

    act(() => motion.set(false));
    expect(screen.getByTestId("probe")).toHaveClass("ui-anim-appear");

    unmount();
    expect(motion.listeners.size).toBe(0);
  });
});
