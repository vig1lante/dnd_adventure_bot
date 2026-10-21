import type { ButtonHTMLAttributes } from "react";
import { classNames } from "./classNames";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Основная — свеча, второстепенная — контур, опасная — сургуч. */
  variant?: "primary" | "secondary" | "danger";
  icon?: IconName;
  /** Идёт действие: кнопка недоступна и сообщает об этом. */
  busy?: boolean;
  block?: boolean;
}

export function Button({
  variant = "primary",
  icon,
  busy = false,
  block = false,
  type = "button",
  className,
  disabled,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      className={classNames(
        "ui-button",
        `ui-button--${variant}`,
        block && "ui-button--block",
        className,
      )}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
    >
      {icon !== undefined && <Icon name={icon} size={20} />}
      {children}
    </button>
  );
}
