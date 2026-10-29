import type { ReactNode } from "react";
import type { Draft, StepStatus } from "../../api/types";
import { Badge } from "../../ui";
import type { BadgeProps } from "../../ui";
import { classNames } from "../../ui/classNames";
import { EDITOR_STEPS, FINAL_SCREENS } from "./steps";
import type { EditorScreen } from "./steps";
import "./authoring.css";

// Статус шага считает сервер (по проверке черновика); клиент только подписывает его.
const STATUS: Record<StepStatus, { label: string; tone: BadgeProps["tone"] }> =
  {
    empty: { label: "пусто", tone: "neutral" },
    draft: { label: "черновик", tone: "amber" },
    ready: { label: "готово", tone: "cloth" },
  };

interface ScreenButtonProps {
  label: string;
  current: boolean;
  onClick: () => void;
  /** Метки экрана: статус шага и замок. */
  children?: ReactNode;
}

function ScreenButton({
  label,
  current,
  onClick,
  children,
}: ScreenButtonProps) {
  return (
    <li>
      <button
        type="button"
        className={classNames(
          "authoring-step",
          current && "authoring-step--current",
        )}
        aria-current={current ? "step" : undefined}
        onClick={onClick}
      >
        <span className="authoring-step__label">{label}</span>
        {children}
      </button>
    </li>
  );
}

interface StepNavProps {
  draft: Draft;
  current: EditorScreen;
  onSelect: (screen: EditorScreen) => void;
}

/** Полоска экранов: статус каждого шага и замок, если шаг закреплён целиком; затем проверка и публикация. */
export function StepNav({ draft, current, onSelect }: StepNavProps) {
  return (
    <nav aria-label="Шаги приключения">
      <ol className="authoring-steps">
        {EDITOR_STEPS.map(({ step, label }) => {
          const status = STATUS[draft.steps[step].status];
          return (
            <ScreenButton
              key={step}
              label={label}
              current={step === current}
              onClick={() => onSelect(step)}
            >
              <Badge tone={status.tone}>{status.label}</Badge>
              {draft.locks.includes(`step:${step}`) && (
                <Badge tone="seal" icon="seal">
                  закреплён
                </Badge>
              )}
            </ScreenButton>
          );
        })}
        {FINAL_SCREENS.map(({ screen, label }) => (
          <ScreenButton
            key={screen}
            label={label}
            current={screen === current}
            onClick={() => onSelect(screen)}
          />
        ))}
      </ol>
    </nav>
  );
}
