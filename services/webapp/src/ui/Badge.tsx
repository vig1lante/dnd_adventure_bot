import type { ReactNode } from "react";
import { classNames } from "./classNames";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";

export interface BadgeProps {
  tone?: "neutral" | "amber" | "cloth" | "seal";
  icon?: IconName;
  children: ReactNode;
}

/** Плашка-метка: статус, счётчик, исход. */
export function Badge({ tone = "neutral", icon, children }: BadgeProps) {
  return (
    <span className={classNames("ui-badge", `ui-badge--${tone}`)}>
      {icon !== undefined && <Icon name={icon} size={16} />}
      {children}
    </span>
  );
}
