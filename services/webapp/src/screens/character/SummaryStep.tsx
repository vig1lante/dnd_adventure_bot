import { useId } from "react";
import type { Ability, Persona } from "../../api/types";
import { Button } from "../../ui";
import "../screens.css";
import "./character.css";
import {
  backgroundBonus,
  baseScores,
  grantedSkills,
  stepComplete,
} from "./draft";
import type { Draft, StepId } from "./draft";
import { nameIn, namesIn } from "./names";
import { METHOD_LABELS, STEP_TITLES } from "./steps";
import type { StepProps } from "./steps";

// «Итог» подробного создания: выбор по разделам шагов, у каждого — «Изменить». Только
// показ черновика: числа героя (хиты, бонусы) считает сервер после создания.

/** Подписи полей описания героя — как на шаге «Характер и внешность». */
const PERSONA_LABELS: [keyof Persona, string][] = [
  ["concept", "Замысел"],
  ["appearance", "Внешность"],
  ["traits", "Черты характера"],
  ["ideals", "Идеалы"],
  ["bonds", "Привязанности"],
  ["flaws", "Слабости"],
];

/** Строка «Подпись: a, b» или ничего, если список пуст. */
const listLine = (label: string, names: readonly string[]) =>
  names.length > 0 ? [`${label}: ${names.join(", ")}`] : [];

/** Одна строка через запятую или ничего, если список пуст. */
const joinedLine = (names: readonly string[]) =>
  names.length > 0 ? [names.join(", ")] : [];

/** Что выбрано на шаге — строками для раздела итога; пустой список — ничего не выбрано. */
function summaryLines(
  step: StepId,
  draft: Draft,
  options: StepProps["options"],
): string[] {
  const species = options.species.find((item) => item.id === draft.species);
  const cls = options.classes.find((item) => item.id === draft.classId);
  const background = options.backgrounds.find(
    (item) => item.id === draft.background,
  );

  switch (step) {
    case "species":
      return species === undefined ? [] : [species.name];
    case "class":
      return cls === undefined ? [] : [cls.name];
    case "background":
      return background === undefined ? [] : [background.name];
    case "abilities": {
      const scores = baseScores(draft);
      if (scores === null) return [];
      return [
        METHOD_LABELS[draft.method],
        options.abilities
          .map(({ id, name }) => `${name} ${scores[id]}`)
          .join(", "),
      ];
    }
    case "bonus": {
      const bonus = backgroundBonus(draft, options) ?? {};
      return joinedLine(
        (Object.entries(bonus) as [Ability, number][]).map(
          ([id, amount]) => `${nameIn(options.abilities, id)} +${amount}`,
        ),
      );
    }
    case "skills": {
      const extra = draft.extraSkill === null ? [] : [draft.extraSkill];
      const style = draft.fightingStyle === null ? [] : [draft.fightingStyle];
      return [
        ...listLine("Навыки класса", namesIn(options.skills, draft.skills)),
        ...listLine("Навык на выбор", namesIn(options.skills, extra)),
        ...listLine(
          "От предыстории и расы",
          namesIn(options.skills, grantedSkills(draft, options)),
        ),
        ...listLine("Компетентность", namesIn(options.skills, draft.expertise)),
        ...listLine("Боевой стиль", namesIn(options.fighting_styles, style)),
      ];
    }
    case "spells": {
      const casting = cls?.spellcasting ?? null;
      if (casting === null) return [];
      return [
        ...listLine(
          "Заговоры",
          namesIn(casting.cantrip_options, draft.cantrips),
        ),
        ...listLine("Заклинания", namesIn(casting.spell_options, draft.spells)),
      ];
    }
    case "equipment":
      return joinedLine(
        [
          ...(cls?.starting_equipment ?? []),
          ...(background?.equipment ?? []),
        ].map((item) => `${item.name} × ${item.qty}`),
      );
    case "persona": {
      const name = draft.name.trim();
      return [
        ...(name === "" ? [] : [`Имя: ${name}`]),
        ...PERSONA_LABELS.filter(
          ([key]) => draft.persona[key].trim() !== "",
        ).map(([key, label]) => `${label}: ${draft.persona[key].trim()}`),
      ];
    }
    case "backstory": {
      const text = draft.persona.backstory.trim();
      return text === "" ? [] : [text];
    }
    case "summary":
      return [];
  }
}

interface SectionProps {
  title: string;
  lines: readonly string[];
  complete: boolean;
  onEdit: () => void;
}

function SummarySection({ title, lines, complete, onEdit }: SectionProps) {
  const headingId = useId();
  return (
    <section className="char-summary" aria-labelledby={headingId}>
      <div className="char-summary__head">
        <h3 id={headingId} className="char-summary__title">
          {title}
        </h3>
        {/* Видимо «Изменить», а диктору — с разделом: в списке кнопок они различимы. */}
        <Button
          variant="secondary"
          aria-label={`Изменить: ${title}`}
          onClick={onEdit}
        >
          Изменить
        </Button>
      </div>
      {lines.length === 0 ? (
        <p className="char-note">Пока пусто.</p>
      ) : (
        <ul className="char-list">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {!complete && (
        <p className="char-note">Выбор на этом шаге не закончен.</p>
      )}
    </section>
  );
}

export function SummaryStep({
  draft,
  options,
  steps,
  onEdit,
}: Omit<StepProps, "update"> & {
  /** Шаги мастера: у каждого, кроме самого итога, — свой раздел. */
  steps: readonly StepId[];
  /** Переход на шаг раздела. */
  onEdit: (step: StepId) => void;
}) {
  return (
    <div className="screen-stack">
      {steps
        .filter((step) => step !== "summary")
        .map((step) => (
          <SummarySection
            key={step}
            title={STEP_TITLES[step]}
            lines={summaryLines(step, draft, options)}
            complete={stepComplete(step, draft, options)}
            onEdit={() => onEdit(step)}
          />
        ))}
    </div>
  );
}
