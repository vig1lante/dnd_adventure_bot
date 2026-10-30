import type { Ability } from "../../api/types";
import { ChoiceList, SelectField } from "../fields";
import "../screens.css";
import "./character.css";
import { isBonusFixed } from "./draft";
import { nameIn } from "./names";
import { BackgroundCarousel, ClassCarousel, RaceCarousel } from "./OptionCards";
import type { StepProps } from "./steps";

const abilityName = (options: StepProps["options"], id: Ability) =>
  nameIn(options.abilities, id);

// Раса, класс и предыстория выбираются листанием подробных карточек.

export function SpeciesStep({ draft, options, update }: StepProps) {
  return (
    <RaceCarousel
      options={options}
      value={draft.species}
      onChange={(species) => update({ species })}
    />
  );
}

export function ClassStep({ draft, options, update }: StepProps) {
  return (
    <ClassCarousel
      options={options}
      value={draft.classId}
      onChange={(classId) => update({ classId })}
    />
  );
}

export function BackgroundStep({ draft, options, update }: StepProps) {
  return (
    <BackgroundCarousel
      options={options}
      value={draft.background}
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
