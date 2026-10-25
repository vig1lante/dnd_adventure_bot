import type { AuthoringStep, Draft, StepStatus } from "../../api/types";
import { Badge } from "../../ui";
import type { BadgeProps } from "../../ui";
import { classNames } from "../../ui/classNames";
import "./authoring.css";

/**
 * Экраны редактора по порядку генератора; каждый — шаг генерации, его статус и замок
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
] as const;

export type EditorStep = (typeof EDITOR_STEPS)[number]["step"];

/** Название шага для людей; шаги следующих блоков пока показываются своим именем. */
export const stepLabel = (step: AuthoringStep): string =>
  EDITOR_STEPS.find((item) => item.step === step)?.label ?? step;

// Статус шага считает сервер (по проверке черновика); клиент только подписывает его.
const STATUS: Record<StepStatus, { label: string; tone: BadgeProps["tone"] }> =
  {
    empty: { label: "пусто", tone: "neutral" },
    draft: { label: "черновик", tone: "amber" },
    ready: { label: "готово", tone: "cloth" },
  };

interface StepNavProps {
  draft: Draft;
  current: EditorStep;
  onSelect: (step: EditorStep) => void;
}

/** Полоска шагов: статус каждого шага и замок, если шаг закреплён целиком. */
export function StepNav({ draft, current, onSelect }: StepNavProps) {
  return (
    <nav aria-label="Шаги приключения">
      <ol className="authoring-steps">
        {EDITOR_STEPS.map(({ step, label }) => {
          const status = STATUS[draft.steps[step].status];
          return (
            <li key={step}>
              <button
                type="button"
                className={classNames(
                  "authoring-step",
                  step === current && "authoring-step--current",
                )}
                aria-current={step === current ? "step" : undefined}
                onClick={() => onSelect(step)}
              >
                <span className="authoring-step__label">{label}</span>
                <Badge tone={status.tone}>{status.label}</Badge>
                {draft.locks.includes(`step:${step}`) && (
                  <Badge tone="seal" icon="seal">
                    закреплён
                  </Badge>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
