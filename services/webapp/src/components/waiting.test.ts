import { act, renderHook } from "@testing-library/react";
import {
  THINKING_PHRASES,
  THINKING_TITLE,
  thinkingPhrase,
  useThinking,
  useWaitedSeconds,
} from "./waiting";

// Часы браузера подменяются вместе с таймерами: счётчик ожидания считает по ним.
const FAKE_CLOCK: (
  "setTimeout" | "clearTimeout" | "setInterval" | "clearInterval" | "Date"
)[] = ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"];

const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));

describe("фразы ожидания мастера", () => {
  test("одна формулировка заголовка", () => {
    expect(THINKING_TITLE).toBe("Мастер думает…");
  });

  test("фраза зависит только от прошедшего времени: те же секунды — та же фраза", () => {
    const random = vi.spyOn(Math, "random");

    for (const seconds of [0, 7, 8, 25, 39, 40, 1000]) {
      expect(thinkingPhrase(seconds)).toBe(thinkingPhrase(seconds));
    }
    expect(random).not.toHaveBeenCalled();
  });

  test("фраза меняется раз в 8 секунд и идёт по кругу", () => {
    expect(thinkingPhrase(0)).toBe(THINKING_PHRASES[0]);
    expect(thinkingPhrase(7)).toBe(THINKING_PHRASES[0]);
    expect(thinkingPhrase(8)).toBe(THINKING_PHRASES[1]);
    expect(thinkingPhrase(8 * THINKING_PHRASES.length)).toBe(
      THINKING_PHRASES[0],
    );
  });

  test("фразы нейтральные и короткие: про мастера, без событий игры, бросков и чисел", () => {
    expect(THINKING_PHRASES.length).toBeGreaterThanOrEqual(3);
    for (const phrase of THINKING_PHRASES) {
      expect(phrase).not.toMatch(/\d/);
      expect(phrase).not.toMatch(/бро|урон|попад|умер|убит|ловушк|тайн/i);
      expect(phrase.length).toBeLessThan(40);
    }
  });

  test("отрицательное и дробное время не ломают выбор", () => {
    expect(THINKING_PHRASES).toContain(thinkingPhrase(-5));
    expect(THINKING_PHRASES).toContain(thinkingPhrase(12.7));
  });
});

describe("useWaitedSeconds: сколько уже ждём", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_CLOCK });
  });

  test("с показа ключа секунды растут", () => {
    const { result } = renderHook(() => useWaitedSeconds("resolving"));
    expect(result.current).toBe(0);

    tick(25_000);

    expect(result.current).toBe(25);
  });

  test("новый ключ — отсчёт заново; null — ожидания нет", () => {
    const { result, rerender } = renderHook(
      ({ key }: { key: string | null }) => useWaitedSeconds(key),
      { initialProps: { key: "resolving" as string | null } },
    );
    tick(30_000);

    rerender({ key: "none" });
    expect(result.current).toBe(0);
    tick(3_000);
    expect(result.current).toBe(3);

    rerender({ key: null });
    expect(result.current).toBe(0);
  });

  test("тот же ключ после перерыва (следующий ход) считается с нуля, а не с прошлого раза", () => {
    const { result, rerender } = renderHook(
      ({ key }: { key: string | null }) => useWaitedSeconds(key),
      { initialProps: { key: "resolving" as string | null } },
    );
    tick(40_000);

    rerender({ key: null });
    rerender({ key: "resolving" });

    expect(result.current).toBe(0);
  });

  test("useThinking: заголовок, фраза по времени и секунды вместе", () => {
    const { result } = renderHook(() => useThinking("resolving"));
    tick(9_000);

    expect(result.current).toEqual({
      title: THINKING_TITLE,
      phrase: THINKING_PHRASES[1],
      seconds: 9,
    });
  });
});
