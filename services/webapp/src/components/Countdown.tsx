import { useEffect, useState } from "react";
import type { PhaseView } from "../api/types";
import { secondsLeft } from "../store/game";
import { Countdown as CandleCountdown } from "../ui";

// Секунды обновляются четыре раза в секунду: так отсчёт не пропускает цифр и не отстаёт.
const TICK_MS = 250;

export interface CountdownProps {
  phase: PhaseView;
  /** Серверное время минус часы браузера, мс (`live.clockOffsetMs`). */
  clockOffsetMs: number;
  /** Весь срок, с: по нему тает воск. */
  totalSeconds?: number;
  label?: string;
}

/** Идущий отсчёт: оставшееся время считается от серверного срока и часов, а не по числу тиков. */
function Running({
  phase,
  clockOffsetMs,
  totalSeconds,
  label,
}: CountdownProps) {
  const [now, setNow] = useState(() => Date.now());
  const left = secondsLeft(phase, clockOffsetMs, now) ?? 0;
  const ticking = left > 0;

  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [ticking]);

  return (
    <CandleCountdown
      remainingSeconds={left}
      totalSeconds={totalSeconds}
      label={label}
    />
  );
}

/**
 * Отсчёт фазы до `deadline_at` по серверному времени. На паузе срока нет: стоит остаток
 * `remaining_seconds` с меткой «Пауза». Нет ни срока, ни остатка — ничего не рисуется.
 */
export function Countdown(props: CountdownProps) {
  const { phase, totalSeconds, label } = props;
  if (phase.deadline_at !== null) {
    // Новый срок («+1 минута», «Продолжить») начинает с свежих часов, а не с устаревших.
    return <Running key={phase.deadline_at} {...props} />;
  }
  if (phase.remaining_seconds !== null) {
    return (
      <CandleCountdown
        remainingSeconds={phase.remaining_seconds}
        totalSeconds={totalSeconds}
        label={label}
        paused
      />
    );
  }
  return null;
}
