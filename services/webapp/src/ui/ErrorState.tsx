import type { ReactNode } from "react";
import { Button } from "./Button";

export interface ErrorStateProps {
  /** Что не получилось («Не удалось загрузить игры»). */
  title: string;
  /** Что случилось и что делать: текст сервера и подсказка. */
  children?: ReactNode;
  /** «Повторить» — тот же запрос ещё раз. */
  onRetry: () => void;
  /** Повтор уже идёт: кнопка занята, второй запрос не уходит. */
  busy?: boolean;
}

/**
 * Ошибка одного вида на всех экранах: значок, что не получилось, что делать и «Повторить».
 * Значок рисуется здесь же: в наборе иконок его нет, а он нужен только этому состоянию.
 */
export function ErrorState({
  title,
  children,
  onRetry,
  busy = false,
}: ErrorStateProps) {
  return (
    <div className="ui-error">
      <div role="alert" className="ui-error__body">
        <svg
          className="ui-error__icon"
          width={40}
          height={40}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v5.5M12 16.5v.01" />
        </svg>
        <p className="ui-error__title">{title}</p>
        {children !== undefined && (
          <div className="ui-error__text">{children}</div>
        )}
      </div>
      <Button icon="d20" busy={busy} onClick={onRetry}>
        Повторить
      </Button>
    </div>
  );
}
