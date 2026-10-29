import { Fragment } from "react";
import type { Skill, SpellOption } from "../../api/types";
import { CheckList, ChoiceList, SelectField } from "../fields";
import "../screens.css";
import "./character.css";
import { grantedSkills, proficientSkills } from "./draft";
import { nameIn } from "./names";
import { MoreDetails, SkillCard, SpellCard, SummaryCard } from "./OptionCards";
import type { StepProps } from "./steps";

// Выбор нескольких вариантов — прежние списки с отметками; у каждого варианта
// раскрываемая подробная карточка «Подробнее» (решение блока 7: множественный выбор
// каруселью на телефоне неудобен).

/** Навыки, выборы класса: навыки, компетентность, боевой стиль. */
export function SkillsStep({ draft, options, update }: StepProps) {
  const cls = options.classes.find((item) => item.id === draft.classId);
  const species = options.species.find((item) => item.id === draft.species);
  if (cls === undefined || species === undefined) return null;

  const skillName = (id: string) => nameIn(options.skills, id);
  const granted = grantedSkills(draft, options);
  const skillOption = (id: string) => ({ value: id, label: skillName(id) });
  /** «Подробнее» навыка — у списков с отметками и у навыка на выбор. */
  const skillMore = (id: string) => {
    const skill = options.skills.find((item) => item.id === id);
    return (
      skill !== undefined && (
        <MoreDetails name={skill.name}>
          <SkillCard skill={skill} options={options} />
        </MoreDetails>
      )
    );
  };
  const skillChoice = (id: string) => ({
    ...skillOption(id),
    more: skillMore(id),
  });

  return (
    <div className="screen-stack">
      {granted.length > 0 && (
        <p className="char-note">{`Уже есть: ${granted.map(skillName).join(", ")}. Их дают предыстория и раса.`}</p>
      )}
      <CheckList
        legend={`Навыки класса: выберите ${cls.skill_choices}`}
        name="skills"
        choices={cls.skill_options
          .filter((id) => !granted.includes(id))
          .map(skillChoice)}
        values={draft.skills}
        max={cls.skill_choices}
        onChange={(skills) => update({ skills: skills as Skill[] })}
      />
      <p className="char-note">{`Выбрано ${draft.skills.length} из ${cls.skill_choices}`}</p>

      {species.extra_skill && (
        <>
          <SelectField
            label="Навык на выбор (Умелость)"
            placeholder="Выберите навык"
            value={draft.extraSkill ?? ""}
            options={options.skills
              .filter(
                (skill) =>
                  skill.id === draft.extraSkill ||
                  (!granted.includes(skill.id) &&
                    !draft.skills.includes(skill.id)),
              )
              .map((skill) => skillOption(skill.id))}
            onChange={(value) =>
              update({ extraSkill: (value || null) as Skill | null })
            }
          />
          {/* Ключ — навык: карточка другого навыка открывается свёрнутой. */}
          {draft.extraSkill !== null && (
            <Fragment key={draft.extraSkill}>
              {skillMore(draft.extraSkill)}
            </Fragment>
          )}
        </>
      )}

      {cls.expertise_picks > 0 && (
        <CheckList
          legend={`Компетентность: выберите ${cls.expertise_picks}`}
          name="expertise"
          choices={proficientSkills(draft, options).map(skillChoice)}
          values={draft.expertise}
          max={cls.expertise_picks}
          onChange={(expertise) => update({ expertise: expertise as Skill[] })}
        />
      )}

      {cls.fighting_styles.length > 0 && (
        <ChoiceList
          legend="Боевой стиль"
          name="fighting-style"
          value={draft.fightingStyle}
          choices={cls.fighting_styles.map((id) => {
            const style = options.fighting_styles.find(
              (item) => item.id === id,
            );
            // Описание стиля — только в «Подробнее»: под названием оно повторялось бы слово в слово.
            return {
              value: id,
              label: style?.name ?? id,
              more: style !== undefined && (
                <MoreDetails name={style.name}>
                  <SummaryCard item={style} />
                </MoreDetails>
              ),
            };
          })}
          onChange={(fightingStyle) => update({ fightingStyle })}
        />
      )}
    </div>
  );
}

export function SpellsStep({ draft, options, update }: StepProps) {
  const casting =
    options.classes.find((item) => item.id === draft.classId)?.spellcasting ??
    null;
  if (casting === null) return null;

  const spellChoice = (spell: SpellOption) => ({
    value: spell.id,
    label: spell.name,
    description:
      spell.level === 0
        ? spell.summary
        : `${spell.level}-й уровень. ${spell.summary}`,
    more: (
      <MoreDetails name={spell.name}>
        <SpellCard spell={spell} />
      </MoreDetails>
    ),
  });

  return (
    <div className="screen-stack">
      {casting.cantrips > 0 && (
        <CheckList
          legend={`Заговоры: выберите ${casting.cantrips}`}
          name="cantrips"
          choices={casting.cantrip_options.map(spellChoice)}
          values={draft.cantrips}
          max={casting.cantrips}
          onChange={(cantrips) => update({ cantrips })}
        />
      )}
      {casting.prepared > 0 && (
        <CheckList
          legend={`Заклинания: выберите ${casting.prepared}`}
          name="spells"
          choices={casting.spell_options.map(spellChoice)}
          values={draft.spells}
          max={casting.prepared}
          onChange={(spells) => update({ spells })}
        />
      )}
    </div>
  );
}

function ItemList({
  title,
  items,
}: {
  title: string;
  items: readonly { id: string; name: string; qty: number }[];
}) {
  return (
    <section className="screen-stack" aria-label={title}>
      <h3>{title}</h3>
      <ul className="char-list">
        {items.map((item) => (
          <li key={item.id}>{`${item.name} × ${item.qty}`}</li>
        ))}
      </ul>
    </section>
  );
}

/** Стартовый набор класса и предыстории: только показ, выбирать здесь нечего. */
export function EquipmentStep({ draft, options }: StepProps) {
  const cls = options.classes.find((item) => item.id === draft.classId);
  const background = options.backgrounds.find(
    (item) => item.id === draft.background,
  );
  return (
    <div className="screen-stack">
      <p className="char-note">
        Стартовый набор выдаётся сразу, менять его нельзя.
      </p>
      {cls !== undefined && (
        <ItemList title={`Класс: ${cls.name}`} items={cls.starting_equipment} />
      )}
      {background !== undefined && (
        <ItemList
          title={`Предыстория: ${background.name}`}
          items={background.equipment}
        />
      )}
    </div>
  );
}
