import { useId } from "react";
import type { ReactNode } from "react";
import { Icon } from "./icons";
import type { IconName } from "./icon-names";

export interface TitledSectionProps {
  className: string;
  title?: string;
  icon?: IconName;
  headingLevel?: 2 | 3;
  children?: ReactNode;
}

/** Общая основа Card и Panel: с заголовком — именованный раздел, без него — обычный блок. */
export function TitledSection({
  className,
  title,
  icon,
  headingLevel = 2,
  children,
}: TitledSectionProps) {
  const headingId = useId();
  if (title === undefined) return <div className={className}>{children}</div>;
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <section className={className} aria-labelledby={headingId}>
      <div className="ui-section__header">
        {icon !== undefined && <Icon name={icon} />}
        <Heading id={headingId} className="ui-section__title">
          {title}
        </Heading>
      </div>
      {children}
    </section>
  );
}
