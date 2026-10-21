import type { ReactNode } from "react";
import { classNames } from "./classNames";
import type { IconName } from "./icon-names";
import { TitledSection } from "./Section";

export interface PanelProps {
  title?: string;
  icon?: IconName;
  headingLevel?: 2 | 3;
  /** Дерево — для групп управления, сукно — для второстепенных плашек. */
  tone?: "wood" | "cloth";
  className?: string;
  children?: ReactNode;
}

/** Панель: деревянная рама или зелёное сукно вокруг группы элементов. */
export function Panel({
  tone = "wood",
  className,
  children,
  ...section
}: PanelProps) {
  return (
    <TitledSection
      className={classNames("ui-panel", `ui-panel--${tone}`, className)}
      {...section}
    >
      {children}
    </TitledSection>
  );
}
