import type { Ability, FeatureOption } from "../../api/types";
import { ChoiceList, SelectField } from "../fields";
import "../screens.css";
import "./character.css";
import { isBonusFixed } from "./draft";
import type { StepProps } from "./steps";

const abilityName = (options: StepProps["options"], id: Ability) =>
  options.abilities.find((ability) => ability.id === id)?.name ?? id;

/** Особенности выбранного варианта: название и суть, чтобы выбирать осознанно. */
function Features({ items }: { items: readonly FeatureOption[] }) {
  return (
    <ul className="char-list">
      {items.map((item) => (
        <li key={item.name}>
          <strong>{item.name}.</strong> {item.summary}
        </li>
      ))}
    </ul>
  );
}

export function SpeciesStep({ draft, options, update }: StepProps) {
  const chosen = options.species.find((item) => item.id === draft.species);
  return (
    <>
      <ChoiceList
        legend="Раса"
        name="species"
        value={draft.species}
        choices={options.species.map((item) => ({
          value: item.id,
          label: item.name,
          description: item.traits.map((trait) => trait.name).join(", "),
        }))}
        onChange={(species) => update({ species })}
      />
      {chosen !== undefined && <Features items={chosen.traits} />}
    </>
  );
}

export function ClassStep({ draft, options, update }: StepProps) {
  const chosen = options.classes.find((item) => item.id === draft.classId);
  return (
    <>
      <ChoiceList
        legend="Класс"
        name="class"
        value={draft.classId}
        choices={options.classes.map((item) => ({
          value: item.id,
          label: item.name,
          description: (
            <>
              <span>{`Кость хитов к${item.hit_die} · Спасброски: ${item.saving_throws.map((id) => abilityName(options, id)).join(", ")}`}</span>
              <span>
                {item.features.map((feature) => feature.name).join(", ")}
              </span>
            </>
          ),
        }))}
        onChange={(classId) => update({ classId })}
      />
      {chosen !== undefined && <Features items={chosen.features} />}
    </>
  );
}

export function BackgroundStep({ draft, options, update }: StepProps) {
  const skillName = (id: string) =>
    options.skills.find((skill) => skill.id === id)?.name ?? id;
  const featName = (id: string) =>
    options.feats.find((feat) => feat.id === id)?.name ?? id;
  return (
    <ChoiceList
      legend="Предыстория"
      name="background"
      value={draft.background}
      choices={options.backgrounds.map((item) => ({
        value: item.id,
        label: item.name,
        description: (
          <>
            <span>{`Навыки: ${item.skills.map(skillName).join(", ")}`}</span>
            <span>{`Черта: ${featName(item.feat)}`}</span>
            <span>{`Бонус к: ${item.ability_options.map((id) => abilityName(options, id)).join(", ")}`}</span>
          </>
        ),
      }))}
      onChange={(background) => update({ background })}
    />
  );
}

/** Метка числа в форме бонуса: у одинаковых чисел добавляется номер. */
function bonusLabel(shape: readonly number[], index: number): string {
  const same = shape.every((amount) => amount === shape[0]);
  return `Бонус +${shape[index]}${same ? ` (${index + 1})` : ""}`;
}

export function BonusStep({ draft, options, update }: StepProps) {
  const background = options.backgrounds.find(
    (item) => item.id === draft.background,
  );
  const shape = options.background_bonus_shapes[draft.bonusShape] ?? [];
  const allowed = background?.ability_options ?? [];

  const pick = (index: number, value: string) =>
    update({
      bonusPicks: draft.bonusPicks.map((current, position) =>
        position === index ? ((value || null) as Ability | null) : current,
      ),
    });

  return (
    <div className="screen-stack">
      <p>
        {`Предыстория «${background?.name ?? ""}» даёт бонус к характеристикам из своего списка.`}
      </p>
      <ChoiceList
        legend="Форма бонуса"
        name="bonus-shape"
        value={String(draft.bonusShape)}
        choices={options.background_bonus_shapes.map((item, index) => ({
          value: String(index),
          label: item.map((amount) => `+${amount}`).join(", "),
        }))}
        onChange={(value) => update({ bonusShape: Number(value) })}
      />
      {isBonusFixed(draft, options) ? (
        <p>{`Бонус +${shape[0]} к: ${allowed.map((id) => abilityName(options, id)).join(", ")}.`}</p>
      ) : (
        shape.map((_, index) => (
          <SelectField
            // Позиция в форме — единственный стабильный ключ: числа в ней повторяются.
            key={index}
            label={bonusLabel(shape, index)}
            placeholder="—"
            value={draft.bonusPicks[index] ?? ""}
            options={allowed
              .filter(
                (id) =>
                  id === draft.bonusPicks[index] ||
                  !draft.bonusPicks.includes(id),
              )
              .map((id) => ({ value: id, label: abilityName(options, id) }))}
            onChange={(value) => pick(index, value)}
          />
        ))
      )}
    </div>
  );
}
