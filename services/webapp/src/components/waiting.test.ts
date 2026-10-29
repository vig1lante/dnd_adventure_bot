import { act, renderHook } from "@testing-library/react";
import {
  THINKING_PHRASES,
  THINKING_TITLE,
  phaseStartedAt,
  thinkingPhrase,
  useStaysFor,
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

// --- серверное начало фазы (контракт B11, §2: `phase.phase_since`) ---

const NOW = Date.parse("2026-10-20T12:00:00+00:00");
/** Время по часам браузера `seconds` секунд назад. */
const ago = (seconds: number) => NOW - seconds * 1000;

describe("phaseStartedAt: начало фазы по часам браузера", () => {
  test("серверное время начала минус сдвиг часов", () => {
    // Сервер впереди браузера на 3 минуты: его 12:00:00 — это 11:57:00 браузера.
    const since = "2026-10-20T12:00:00+00:00";

    expect(phaseStartedAt(since, 0)).toBe(NOW);
    expect(phaseStartedAt(since, 180_000)).toBe(NOW - 180_000);
  });

  test("начала нет (игра шла до B11), сервер до B11 без поля или строка не читается — неизвестно", () => {
    expect(phaseStartedAt(null, 0)).toBeUndefined();
    expect(phaseStartedAt(undefined, 0)).toBeUndefined();
    expect(phaseStartedAt("вчера", 0)).toBeUndefined();
  });
});

describe("useStaysFor: ждать от серверного начала фазы", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_CLOCK });
    vi.setSystemTime(NOW);
  });

  test("без начала — срок с того мига, как экран увидел ключ (как раньше)", () => {
    const { result } = renderHook(() => useStaysFor("resolving", 60_000));

    tick(59_999);
    expect(result.current).toBe(false);
    tick(1);
    expect(result.current).toBe(true);
  });

  test("фаза началась 40 с назад (перезагрузка): до минуты осталось 20 с, а не снова минута", () => {
    const { result } = renderHook(() =>
      useStaysFor("resolving", 60_000, ago(40)),
    );

    tick(19_999);
    expect(result.current).toBe(false);
    tick(1);
    expect(result.current).toBe(true);
  });

  test("фаза началась больше минуты назад: срок уже прошёл", () => {
    const { result } = renderHook(() =>
      useStaysFor("resolving", 60_000, ago(90)),
    );

    tick(0);

    expect(result.current).toBe(true);
  });

  test("уточнилось начало (новый снимок с другим сдвигом часов) — срок пересчитан, прошедший не гаснет", () => {
    const { result, rerender } = renderHook(
      ({ since }: { since: number }) => useStaysFor("resolving", 60_000, since),
      { initialProps: { since: ago(90) } },
    );
    tick(0);
    expect(result.current).toBe(true);

    rerender({ since: ago(89) });

    expect(result.current).toBe(true);
  });
});

describe("useWaitedSeconds: счёт от серверного начала фазы", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: FAKE_CLOCK });
    vi.setSystemTime(NOW);
  });

  test("фаза началась 40 с назад: счётчик продолжает с 40, а не с нуля", () => {
    const { result } = renderHook(() => useWaitedSeconds("resolving", ago(40)));

    tick(1000);

    expect(result.current).toBe(41);
  });

  // Финальное покрытие B11: сдвиг часов оценивается по снимку и может чуть ошибиться.
  test("начало фазы чуть впереди часов браузера — счётчик стоит на нуле, а не уходит в минус", () => {
    const { result } = renderHook(() =>
      useWaitedSeconds("resolving", NOW + 5_000),
    );

    tick(1000);
    expect(result.current).toBe(0);
    tick(6000);
    expect(result.current).toBe(2);
  });

  test("useThinking тоже считает от начала фазы", () => {
    const { result } = renderHook(() => useThinking("resolving", ago(30)));

    tick(2000);

    expect(result.current.seconds).toBe(32);
  });
});
