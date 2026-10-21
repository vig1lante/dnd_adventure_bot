import type { ReactNode } from "react";
import { classNames } from "./classNames";
import type { IconName } from "./icon-names";

// Линейные иконки 24×24: цвет берут у текста (currentColor), поэтому темы и акценты
// не требуют отдельных картинок.
const SHAPES: Record<IconName, ReactNode> = {
  d20: (
    <>
      <path d="M12 2.5 20.5 7.25v9.5L12 21.5l-8.5-4.75v-9.5z" />
      <path d="M12 6.5 17.5 16h-11z" />
      <path d="M12 6.5v-4M12 6.5 3.5 7.25M12 6.5l8.5.75M6.5 16l-3-8.75M6.5 16l-3 .75M6.5 16 12 21.5M17.5 16l3-8.75M17.5 16l3 .75M17.5 16 12 21.5" />
    </>
  ),
  candle: (
    <>
      <path d="M12 2.5c1.6 2 3 3.4 3 5.2a3 3 0 0 1-6 0c0-1.8 1.4-3.2 3-5.2z" />
      <path d="M12 11v1.5" />
      <rect x="8.5" y="12.5" width="7" height="8" rx="1" />
      <path d="M6 21.5h12" />
    </>
  ),
  scroll: (
    <>
      <rect x="4.5" y="3" width="15" height="3.5" rx="1.75" />
      <rect x="4.5" y="17.5" width="15" height="3.5" rx="1.75" />
      <path d="M6.5 6.5v11M17.5 6.5v11M9.5 10h5M9.5 13.5h5" />
    </>
  ),
  shield: (
    <>
      <path d="M12 2.8 19.5 5.5v6c0 4.7-3.1 7.9-7.5 9.7-4.4-1.8-7.5-5-7.5-9.7v-6z" />
      <path d="M12 7v9M8.5 10.5h7" />
    </>
  ),
  heart: (
    <path d="M12 20.5S3.5 15.4 3.5 9.2A4.7 4.7 0 0 1 12 6.6a4.7 4.7 0 0 1 8.5 2.6c0 6.2-8.5 11.3-8.5 11.3z" />
  ),
  hourglass: (
    <path d="M6.5 3h11M6.5 21h11M7.5 3c0 4.5 3 6 4.5 9-1.5 3-4.5 4.5-4.5 9M16.5 3c0 4.5-3 6-4.5 9 1.5 3 4.5 4.5 4.5 9" />
  ),
  seal: (
    <>
      <circle cx="12" cy="10" r="7.5" />
      <circle cx="12" cy="10" r="4.3" />
      <path d="M12 8.2v3.6M10.2 10h3.6M8.5 16.2 7 21.5l3.2-1.6M15.5 16.2 17 21.5l-3.2-1.6" />
    </>
  ),
};

export interface IconProps {
  name: IconName;
  /** Сторона в пикселях. */
  size?: number;
  /** Толщина линии в долях сетки 24×24; у крупных иконок её уменьшают, иначе линии сливаются. */
  strokeWidth?: number;
  /** Подпись: без неё иконка — украшение и скрыта от экранных читалок. */
  title?: string;
  className?: string;
}

export function Icon({
  name,
  size = 24,
  strokeWidth = 1.6,
  title,
  className,
}: IconProps) {
  return (
    <svg
      className={classNames("ui-icon", className)}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
      aria-hidden={title === undefined ? true : undefined}
      role={title === undefined ? undefined : "img"}
      aria-label={title}
    >
      {SHAPES[name]}
    </svg>
  );
}
