import type { ReactNode } from "react";
import { useMotionClass } from "../theme/motion";
import { classNames } from "./classNames";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";

export interface EmptyStateProps {
  icon?: IconName;
  title: string;
  /** По умолчанию название — абзац, чтобы не ломать заголовки страницы; экран может сделать его заголовком. */
  titleAs?: "p" | "h2" | "h3";
  action?: ReactNode;
  children?: ReactNode;
}

/** Пустое состояние: иконка, название, пояснение и, если нужно, действие. */
export function EmptyState({
  icon = "scroll",
  title,
  titleAs: Title = "p",
  action,
  children,
}: EmptyStateProps) {
  return (
    <div className="ui-empty">
      <Icon name={icon} size={56} className="ui-empty__icon" />
      <Title className="ui-empty__title">{title}</Title>
      {children !== undefined && (
        <div className="ui-empty__text">{children}</div>
      )}
      {action !== undefined && <div className="ui-empty__action">{action}</div>}
    </div>
  );
}

/** Загрузка: кубик слегка покачивается (при reduced motion стоит). */
export function Loading({ label = "Загрузка…" }: { label?: string }) {
  const wobble = useMotionClass("ui-anim-wobble");
  return (
    <div role="status" className="ui-loading">
      <span
        className={classNames("ui-loading__icon", wobble)}
        aria-hidden="true"
      >
        <Icon name="d20" size={32} />
      </span>
      <span>{label}</span>
    </div>
  );
}

/** Сообщение об ошибке: сургучная метка слева. */
export function Alert({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="ui-alert">
      <Icon name="seal" size={22} className="ui-alert__icon" />
      <div>{children}</div>
    </div>
  );
}
