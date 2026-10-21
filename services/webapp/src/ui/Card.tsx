import type { ReactNode } from "react";
import { useMotionClass } from "../theme/motion";
import { classNames } from "./classNames";
import type { IconName } from "./icon-names";
import { TitledSection } from "./Section";

export interface CardProps {
  title?: string;
  icon?: IconName;
  headingLevel?: 2 | 3;
  /** Мягкое появление (для новых записей ленты); при reduced motion не применяется. */
  appear?: boolean;
  className?: string;
  children?: ReactNode;
}

/** Карточка-пергамент: основной блок содержимого. */
export function Card({
  appear = false,
  className,
  children,
  ...section
}: CardProps) {
  const appearClass = useMotionClass("ui-anim-appear");
  return (
    <TitledSection
      className={classNames("ui-card", appear && appearClass, className)}
      {...section}
    >
      {children}
    </TitledSection>
  );
}
