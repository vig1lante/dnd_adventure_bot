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

/**
 * Идущий отсчёт: оставшееся время считается от серверного срока и часов, а не по числу тиков.
 * Воск тает от большего из настроенного срока и остатка в миг, когда пришёл этот срок: после
 * «+1 мин» остаток больше настроенного, и свеча полна и тает до самого конца (контракт B11,
 * §1 п. 16), а не стоит полной, пока остаток не опустится до прежнего срока.
 */
function Running({
  phase,
  clockOffsetMs,
  totalSeconds,
  label,
}: CountdownProps) {
  const [now, setNow] = useState(() => Date.now());
  const left = secondsLeft(phase, clockOffsetMs, now) ?? 0;
  const [leftAtStart] = useState(left);
  const ticking = left > 0;

  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [ticking]);

  return (
    <CandleCountdown
      remainingSeconds={left}
      totalSeconds={
        totalSeconds === undefined
          ? undefined
          : Math.max(totalSeconds, leftAtStart)
      }
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
