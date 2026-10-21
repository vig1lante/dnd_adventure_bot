import type { ReactNode } from "react";
import type { Character, CreationOptions } from "../../api/types";
import { HpBar } from "../../ui";
import "../screens.css";
import "./character.css";

// Лист героя из предпросмотра. Числа приходят от сервера и показываются как есть;
// русские названия берутся из правил создания (creation-options). Неизвестный id
// остаётся как есть: лучше показать id, чем потерять строку листа.

const nameIn = (items: readonly { id: string; name: string }[], id: string) =>
  items.find((item) => item.id === id)?.name ?? id;

function itemName(options: CreationOptions, id: string): string {
  const lines = [
    ...options.classes.flatMap((cls) => cls.starting_equipment),
    ...options.backgrounds.flatMap((background) => background.equipment),
  ];
  return nameIn(lines, id);
}

function spellName(options: CreationOptions, id: string): string {
  const spells = options.classes.flatMap((cls) => [
    ...(cls.spellcasting?.cantrip_options ?? []),
    ...(cls.spellcasting?.spell_options ?? []),
  ]);
  return nameIn(spells, id);
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="char-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

interface HeroSheetProps {
  character: Character;
  options: CreationOptions;
}

/** Лист героя без имени: в предпросмотре имя служебное, его придумает мастер. */
export function HeroSheet({ character, options }: HeroSheetProps) {
  const skills = character.skill_proficiencies.map((skill) => {
    const name = nameIn(options.skills, skill);
    return character.expertise.includes(skill)
      ? `${name} (компетентность)`
      : name;
  });
  const facts: [string, string[]][] = [
    [
      "Спасброски",
      character.save_proficiencies.map((id) => nameIn(options.abilities, id)),
    ],
    ["Навыки", skills],
    [
      "Боевой стиль",
      character.fighting_style === null
        ? []
        : [nameIn(options.fighting_styles, character.fighting_style)],
    ],
    ["Заговоры", character.cantrips.map((id) => spellName(options, id))],
    [
      "Заклинания",
      character.prepared_spells.map((id) => spellName(options, id)),
    ],
    [
      "Снаряжение",
      character.inventory.map(
        (line) => `${itemName(options, line.item_id)} × ${line.qty}`,
      ),
    ],
  ];

  return (
    <section className="char-sheet" aria-label="Лист героя">
      <div>
        <h3 className="char-sheet__title">
          {nameIn(options.classes, character.class_id)}
        </h3>
        <p className="char-sheet__origin">
          {`${nameIn(options.species, character.species)} · ${nameIn(options.backgrounds, character.background)} · уровень ${character.level}`}
        </p>
      </div>
      <HpBar
        current={character.hp}
        max={character.hp_max}
        temp={character.temp_hp}
      />
      <dl className="char-scores">
        {options.abilities.map(({ id, name }) => (
          <div key={id} className="char-score">
            <dt>{name}</dt>
            <dd>{character.scores[id]}</dd>
          </div>
        ))}
      </dl>
      <dl className="char-facts">
        {facts
          .filter(([, values]) => values.length > 0)
          .map(([label, values]) => (
            <Fact key={label} label={label}>
              {values.join(", ")}
            </Fact>
          ))}
      </dl>
      <p className="char-note">
        Имя и описание придумает мастер, когда вы возьмёте героя.
      </p>
    </section>
  );
}
