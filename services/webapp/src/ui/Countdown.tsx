import { useMotionClass } from "../theme/motion";
import { Badge } from "./Badge";
import { classNames } from "./classNames";
import { formatClock } from "./format";
import { Icon } from "./icons";

export interface CountdownProps {
  /** Сколько осталось; считает и обновляет вызывающий экран, а не сам элемент. */
  remainingSeconds: number;
  /** Весь срок: по нему тает воск. Без него полосы нет. */
  totalSeconds?: number;
  label?: string;
  /** На паузе срока нет, остаток стоит. */
  paused?: boolean;
}

// В последние секунды цифры краснеют — мягкий намёк, без мигания.
const URGENT_SECONDS = 10;

/** Основа отсчёта: свеча с мерцающим огоньком и воском, который тает. */
export function Countdown({
  remainingSeconds,
  totalSeconds,
  label = "Осталось",
  paused = false,
}: CountdownProps) {
  const flicker = useMotionClass("ui-anim-flicker");
  const clock = formatClock(remainingSeconds);
  const urgent = !paused && Math.ceil(remainingSeconds) <= URGENT_SECONDS;
  const waxPercent =
    totalSeconds === undefined
      ? null
      : totalSeconds > 0
        ? Math.round(
            Math.min(Math.max(remainingSeconds / totalSeconds, 0), 1) * 10000,
          ) / 100
        : 0;

  return (
    <div
      role="timer"
      aria-label={`${label} ${clock}`}
      className={classNames(
        "ui-countdown",
        paused && "ui-countdown--paused",
        urgent && "ui-countdown--urgent",
      )}
    >
      <span
        className={classNames("ui-countdown__flame", !paused && flicker)}
        aria-hidden="true"
      >
        <Icon name="candle" size={40} />
      </span>
      <div>
        <p className="ui-countdown__label">
          {label}
          {paused && <Badge>Пауза</Badge>}
        </p>
        <p className="ui-countdown__time">{clock}</p>
      </div>
      {waxPercent !== null && (
        <div className="ui-countdown__wax" aria-hidden="true">
          <span style={{ width: `${waxPercent}%` }} />
        </div>
      )}
    </div>
  );
}
