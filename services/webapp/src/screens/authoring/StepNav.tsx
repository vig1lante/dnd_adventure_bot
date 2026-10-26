import type { ReactNode } from "react";
import type { AuthoringStep, Draft, StepStatus } from "../../api/types";
import { Badge } from "../../ui";
import type { BadgeProps } from "../../ui";
import { classNames } from "../../ui/classNames";
import "./authoring.css";

/**
 * Экраны-шаги редактора по порядку генератора; каждый — шаг генерации, его статус и замок
 * `step:<шаг>`. «Карта» и «Наполнение» правят один раздел `locations`, но шаги у них разные
 * (дополнение B5, §2).
 */
export const EDITOR_STEPS = [
  { step: "concept", label: "Замысел" },
  { step: "world", label: "Мир" },
  { step: "pantheon", label: "Боги" },
  { step: "factions", label: "Фракции" },
  { step: "plot", label: "Сюжет" },
  { step: "locations", label: "Карта" },
  { step: "location_contents", label: "Наполнение" },
  { step: "npcs", label: "Персонажи" },
  { step: "relations", label: "Связи" },
  { step: "encounters", label: "Бои и боссы" },
  { step: "items", label: "Предметы и награды" },
  { step: "clues", label: "Улики" },
] as const satisfies readonly { step: AuthoringStep; label: string }[];

/** Экраны после шагов: у проверки и публикации нет ни статуса, ни замка. */
export const FINAL_SCREENS = [
  { screen: "review", label: "Проверка" },
  { screen: "publish", label: "Публикация" },
] as const;

export type EditorStep = (typeof EDITOR_STEPS)[number]["step"];

/** Экран редактора: шаг или проверка и публикация. */
export type EditorScreen =
  EditorStep | (typeof FINAL_SCREENS)[number]["screen"];

/** Название шага для людей. */
export const stepLabel = (step: AuthoringStep): string =>
  EDITOR_STEPS.find((item) => item.step === step)?.label ?? step;

/** Название любого экрана редактора, в том числе проверки и публикации. */
export const screenLabel = (screen: EditorScreen): string =>
  FINAL_SCREENS.find((item) => item.screen === screen)?.label ??
  stepLabel(screen as AuthoringStep);

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
