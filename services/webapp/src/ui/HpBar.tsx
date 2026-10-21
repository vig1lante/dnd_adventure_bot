import { Badge } from "./Badge";
import { classNames } from "./classNames";
import { hpTone } from "./format";
import { Icon } from "./icons";

export interface HpBarProps {
  current: number;
  max: number;
  /** Временные хиты: отдельная метка, на полосу не влияют. */
  temp?: number;
  label?: string;
}

/** Шкала здоровья. Числа приходят с сервера; шкала их только показывает. */
export function HpBar({
  current,
  max,
  temp = 0,
  label = "Здоровье",
}: HpBarProps) {
  const safeMax = Math.max(0, max);
  const value = Math.min(Math.max(current, 0), safeMax);
  const percent = safeMax > 0 ? Math.round((value / safeMax) * 10000) / 100 : 0;
  const spoken =
    temp > 0
      ? `${value} из ${safeMax}, временных ${temp}`
      : `${value} из ${safeMax}`;

  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      aria-valuenow={value}
      aria-valuetext={spoken}
      className={classNames("ui-hp", `ui-hp--${hpTone(value, safeMax)}`)}
    >
      <Icon name="heart" size={20} className="ui-hp__icon" />
      <div className="ui-hp__track">
        <div className="ui-hp__fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="ui-hp__numbers">
        <span className="ui-hp__text">{`${value} / ${safeMax}`}</span>
        {temp > 0 && <Badge tone="amber">{`+${temp}`}</Badge>}
      </span>
    </div>
  );
}
