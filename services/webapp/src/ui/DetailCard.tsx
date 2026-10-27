import { useId } from "react";
import type { ReactNode } from "react";
import { Badge } from "./Badge";

/** Строка «факт — значение»: «Скорость — 9 м». */
export interface DetailFact {
  label: string;
  value: ReactNode;
}

/** Запись раздела: черта, особенность класса, черта предыстории. */
export interface DetailEntry {
  name: string;
  summary: string;
  /** Уровень особенности класса. */
  level?: number;
  /** Действует в бою: правило учитывает движок. */
  mechanical?: boolean;
}

export interface DetailSection {
  title: string;
  entries: readonly DetailEntry[];
}

export interface DetailCardProps {
  title: string;
  /** Английское название, если есть. */
  subtitle?: string;
  description?: string;
  facts?: readonly DetailFact[];
  sections?: readonly DetailSection[];
  /** Действия карточки: «Выбрать» и т. п. */
  actions?: ReactNode;
}

function Entry({ entry }: { entry: DetailEntry }) {
  return (
    <li className="ui-detail__entry">
      <span className="ui-detail__entry-head">
        <strong>{entry.name}</strong>
        {entry.level !== undefined && (
          <span className="ui-detail__level">{`${entry.level}-й уровень`}</span>
        )}
        {entry.mechanical === true && (
          <Badge tone="amber">действует в бою</Badge>
        )}
      </span>
      <span>{entry.summary}</span>
    </li>
  );
}

function Section({ section }: { section: DetailSection }) {
  const headingId = useId();
  return (
    <section className="ui-detail__section" aria-labelledby={headingId}>
      <h4 id={headingId} className="ui-detail__section-title">
        {section.title}
      </h4>
      <ul className="ui-detail__entries">
        {section.entries.map((entry) => (
          <Entry key={entry.name} entry={entry} />
        ))}
      </ul>
    </section>
  );
}

/** Подробная карточка варианта: что даёт выбор. Только показ; числа приходят с сервера. */
export function DetailCard({
  title,
  subtitle,
  description,
  facts = [],
  sections = [],
  actions,
}: DetailCardProps) {
  const headingId = useId();
  return (
    <article className="ui-detail" aria-labelledby={headingId}>
      <div className="ui-detail__header">
        <h3 id={headingId} className="ui-detail__title">
          {title}
        </h3>
        {subtitle !== undefined && (
          <p className="ui-detail__subtitle" lang="en">
            {subtitle}
          </p>
        )}
      </div>
      {description !== undefined && (
        <p className="ui-detail__text">{description}</p>
      )}
      {facts.length > 0 && (
        <dl className="ui-detail__facts">
          {facts.map((fact) => (
            <div key={fact.label} className="ui-detail__fact">
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {sections
        .filter((section) => section.entries.length > 0)
        .map((section) => (
          <Section key={section.title} section={section} />
        ))}
      {actions !== undefined && (
        <div className="ui-detail__actions">{actions}</div>
      )}
    </article>
  );
}
