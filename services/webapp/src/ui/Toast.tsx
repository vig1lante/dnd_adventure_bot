import { useEffect, useId } from "react";
import type { ReactNode } from "react";
import { useMotionClass } from "../theme/motion";
import { Button } from "./Button";
import { classNames } from "./classNames";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";

/** Уведомление: заголовок и одна строка пояснения. Только факт события, без текстов игроков. */
export interface ToastMessage {
  title: string;
  text: string;
}

export interface ToastProps {
  /** Что показать; `null` — пусто. */
  message: ToastMessage | null;
  icon?: IconName;
  /** Действие к уведомлению, например «К бою». */
  action?: ReactNode;
  /** Уведомление ушло само или игрок нажал «Скрыть»; экран забывает его. */
  onDismiss: () => void;
  /**
   * Игрок нажал «Скрыть»: кнопка исчезает вместе с тостом, и экран сам решает, куда поставить
   * фокус. Нет — просто `onDismiss`.
   */
  onHide?: () => void;
  /** Через сколько миллисекунд уведомление уходит само. */
  duration?: number;
}

const TOAST_MS = 6000;

/** Крестик кнопки «Скрыть» в стиле набора src/ui/icons. */
function CloseGlyph() {
  return (
    <svg
      width={20}
      height={20}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

/**
 * Тост над нижней панелью: о событии, которое игрок мог пропустить на другой вкладке. Живая
 * область вежливая и стоит на месте всегда, даже пустая: так читалка объявит и первое
 * уведомление. Через несколько секунд тост уходит сам; новое сообщение отсчитывает заново.
 * Тост закрывает низ экрана, поэтому его можно убрать и сразу — кнопкой «Скрыть».
 */
export function Toast({
  message,
  icon = "d20",
  action,
  onDismiss,
  onHide = onDismiss,
  duration = TOAST_MS,
}: ToastProps) {
  const titleId = useId();
  const appear = useMotionClass("ui-anim-appear");

  useEffect(() => {
    if (message === null) return;
    const timer = setTimeout(onDismiss, duration);
    return () => clearTimeout(timer);
  }, [message, onDismiss, duration]);

  return (
    <div
      role="status"
      aria-labelledby={message === null ? undefined : titleId}
      className="ui-toast-region"
    >
      {message !== null && (
        <div className={classNames("ui-toast", appear)}>
          <Icon name={icon} size={28} className="ui-toast__icon" />
          <div className="ui-toast__text">
            <p id={titleId} className="ui-toast__title">
              {message.title}
            </p>
            <p>{message.text}</p>
          </div>
          {action}
          <Button
            variant="secondary"
            className="ui-toast__close"
            aria-label="Скрыть"
            title="Скрыть"
            onClick={onHide}
          >
            <CloseGlyph />
          </Button>
        </div>
      )}
    </div>
  );
}
