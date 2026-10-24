import { useCallback } from "react";
import type { ReactNode } from "react";
import { useApi } from "../api/context";
import type {
  Character,
  CreationOptions,
  DerivedAttack,
  EffectView,
  InventoryItem,
} from "../api/types";
import { useLoaded } from "../screens/hooks";
import { Badge, Card, EmptyState, HpBar } from "../ui";
import {
  damageTypeName,
  effectLabel,
  rangeName,
  ruDice,
  signed,
} from "./format";
import "./game.css";

// Лист героя. Все числа правил (КД, бонусы, Сл, атаки) приходят готовыми в `character.derived`
// и показываются как есть. Русские названия класса, расы, заклинаний и снаряжения берутся из
// правил создания (creation-options); неизвестный id остаётся как есть. Предмет приключения
// приходит уже с названием (`name`), оно главнее: в правилах создания его нет.

type Named = readonly { id: string; name: string }[];

const nameIn = (items: Named | undefined, id: string): string =>
  items?.find((item) => item.id === id)?.name ?? id;

function spellsOf(options: CreationOptions | null): Named {
  return (options?.classes ?? []).flatMap((cls) => [
    ...(cls.spellcasting?.cantrip_options ?? []),
    ...(cls.spellcasting?.spell_options ?? []),
  ]);
}

function itemsOf(options: CreationOptions | null): Named {
  return [
    ...(options?.classes ?? []).flatMap((cls) => cls.starting_equipment),
    ...(options?.backgrounds ?? []).flatMap((item) => item.equipment),
  ];
}

/** Название строки снаряжения: от сервера (предмет приключения), затем из правил, затем id. */
const itemName = (line: InventoryItem, items: Named): string =>
  line.name ?? nameIn(items, line.item_id);

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="sheet-stat">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Attacks({ attacks }: { attacks: readonly DerivedAttack[] }) {
  // Надетое оружие первым; внутри каждой группы порядок инвентаря сохраняется.
  const ordered = [
    ...attacks.filter((attack) => attack.equipped),
    ...attacks.filter((attack) => !attack.equipped),
  ];
  return (
    <Card title="Атаки" icon="shield" headingLevel={3}>
      <ul className="sheet-attacks" aria-label="Атаки">
        {ordered.map((attack) => (
          <li key={attack.item_id} className="sheet-attack">
            <p className="sheet-attack__head">
              <strong>{attack.name}</strong>
              <Badge tone={attack.equipped ? "amber" : "neutral"}>
                {attack.equipped ? "Надето" : "В запасе"}
              </Badge>
            </p>
            <p className="sheet-attack__line">
              <span>{`${signed(attack.bonus)} к атаке`}</span>
              <span>{`${ruDice(attack.damage)} ${damageTypeName(attack.damage_type)}`}</span>
              <span>{rangeName(attack.range)}</span>
            </p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

const PERSONA_FIELDS = [
  ["concept", "Замысел"],
  ["appearance", "Внешность"],
  ["traits", "Черты"],
  ["ideals", "Идеалы"],
  ["bonds", "Привязанности"],
  ["flaws", "Слабости"],
  ["backstory", "История"],
] as const;

function Spells({
  character,
  options,
}: {
  character: Character;
  options: CreationOptions | null;
}) {
  const { derived } = character;
  const spells = spellsOf(options);
  const levels = Object.entries(character.spell_slots_max)
    .filter(([, max]) => max > 0)
    .sort(([a], [b]) => Number(a) - Number(b));
  return (
    <Card title="Заклинания" icon="candle" headingLevel={3}>
      <div className="sheet-stack">
        <dl className="sheet-stats">
          {derived?.spell_save_dc != null && (
            <Stat label="Сл спасброска заклинаний">
              {derived.spell_save_dc}
            </Stat>
          )}
          {derived?.spell_attack_bonus != null && (
            <Stat label="Бонус атаки заклинанием">
              {signed(derived.spell_attack_bonus)}
            </Stat>
          )}
          {character.concentration !== null && (
            <Stat label="Концентрация">
              {nameIn(spells, character.concentration)}
            </Stat>
          )}
        </dl>
        {levels.length > 0 && (
          <ul className="sheet-list" aria-label="Ячейки заклинаний">
            {levels.map(([level, max]) => (
              <li key={level}>
                {`${level} уровень: ${character.spell_slots[level] ?? 0} из ${max}`}
              </li>
            ))}
          </ul>
        )}
        {character.cantrips.length > 0 && (
          <ul className="sheet-list" aria-label="Заговоры">
            {character.cantrips.map((id) => (
              <li key={id}>{nameIn(spells, id)}</li>
            ))}
          </ul>
        )}
        {character.prepared_spells.length > 0 && (
          <ul className="sheet-list" aria-label="Подготовленные заклинания">
            {character.prepared_spells.map((id) => (
              <li key={id}>{nameIn(spells, id)}</li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function Effects({
  items,
  tone,
}: {
  items: EffectView[];
  tone: "seal" | "amber";
}) {
  return items.map((item) => (
    <Badge key={item.id} tone={tone}>
      {effectLabel(item)}
    </Badge>
  ));
}

function Sheet({ character }: { character: Character }) {
  const api = useApi();
  const loadOptions = useCallback(() => api.getCreationOptions(), [api]);
  const [loaded] = useLoaded(loadOptions);
  // Правила нужны только для русских названий: без них лист остаётся с id.
  const options = loaded.status === "ready" ? loaded.data : null;
  const { derived } = character;
  const items = itemsOf(options);
  const origin = [
    nameIn(options?.classes, character.class_id),
    nameIn(options?.species, character.species),
    nameIn(options?.backgrounds, character.background),
    `уровень ${character.level}`,
  ].join(" · ");
  const hasSpells =
    derived?.spell_save_dc != null ||
    character.cantrips.length > 0 ||
    character.prepared_spells.length > 0;
  const down =
    character.hp <= 0 ||
    character.death_saves.successes > 0 ||
    character.death_saves.failures > 0;
  const persona = PERSONA_FIELDS.filter(
    ([key]) => character.persona[key].trim() !== "",
  );

  return (
    <div className="sheet">
      <Card title={character.name} icon="heart">
        <div className="sheet-stack">
          <p className="sheet-origin">{origin}</p>
          <HpBar
            current={character.hp}
            max={character.hp_max}
            temp={character.temp_hp}
          />
          <dl className="sheet-stats">
            {derived !== undefined && (
              <>
                <Stat label="Класс доспеха">{derived.armor_class}</Stat>
                <Stat label="Бонус мастерства">
                  {signed(derived.proficiency_bonus)}
                </Stat>
                <Stat label="Пассивная внимательность">
                  {derived.passive_perception}
                </Stat>
              </>
            )}
            <Stat label="Кости хитов">
              {`${character.hit_dice} из ${character.hit_dice_max}`}
            </Stat>
            <Stat label="Вдохновение">
              {character.inspiration ? (
                <Badge tone="amber" icon="candle">
                  Есть
                </Badge>
              ) : (
                "Нет"
              )}
            </Stat>
            {down && (
              <Stat label="Спасброски от смерти">
                {`успехи ${character.death_saves.successes}, провалы ${character.death_saves.failures}${character.death_saves.stable ? ", стабилен" : ""}`}
              </Stat>
            )}
          </dl>
          {(character.conditions.length > 0 || character.buffs.length > 0) && (
            <p className="sheet-effects">
              <Effects items={character.conditions} tone="seal" />
              <Effects items={character.buffs} tone="amber" />
            </p>
          )}
        </div>
      </Card>

      {derived !== undefined && (
        <Card title="Характеристики" icon="d20" headingLevel={3}>
          <ul className="sheet-abilities" aria-label="Характеристики">
            {derived.abilities.map((ability) => (
              <li key={ability.id} className="sheet-ability">
                <span className="sheet-ability__name">{ability.name}</span>
                <span className="sheet-ability__mod">
                  {signed(ability.modifier)}
                </span>
                <span className="sheet-ability__score">
                  {character.scores[ability.id]}
                </span>
                <span className="sheet-ability__save">
                  {`Спасбросок ${signed(ability.save_bonus)}`}
                  {character.save_proficiencies.includes(ability.id) &&
                    " · мастерство"}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {derived !== undefined && (
        <Card title="Навыки" icon="scroll" headingLevel={3}>
          <ul className="sheet-skills" aria-label="Навыки">
            {derived.skills.map((skill) => {
              const mark = character.expertise.includes(skill.id)
                ? "компетентность"
                : character.skill_proficiencies.includes(skill.id)
                  ? "мастерство"
                  : null;
              return (
                <li key={skill.id} className="sheet-skill">
                  <span className="sheet-skill__bonus">
                    {signed(skill.bonus)}
                  </span>
                  <span className="sheet-skill__name">{skill.name}</span>
                  {mark !== null && (
                    <span className="sheet-skill__mark">{mark}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </Card>
      )}

      {derived !== undefined && derived.attacks.length > 0 && (
        <Attacks attacks={derived.attacks} />
      )}

      {hasSpells && <Spells character={character} options={options} />}

      {character.inventory.length > 0 && (
        <Card title="Снаряжение" icon="shield" headingLevel={3}>
          <ul className="sheet-list" aria-label="Снаряжение">
            {character.inventory.map((line) => (
              <li key={line.item_id} className="sheet-item">
                <span>
                  {line.qty > 1
                    ? `${itemName(line, items)} × ${line.qty}`
                    : itemName(line, items)}
                </span>
                {line.equipped && <Badge tone="amber">Надето</Badge>}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {persona.length > 0 && (
        <Card title="Характер и история" icon="scroll" headingLevel={3}>
          <dl className="sheet-persona">
            {persona.map(([key, label]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{character.persona[key]}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  );
}

/**
 * Лист героя вкладки «Персонаж»: здоровье, числа правил из `character.derived`, характеристики,
 * навыки, атаки, заклинания, снаряжение и вдохновение. Клиент ничего не считает.
 */
export function CharacterSheet({ character }: { character: Character | null }) {
  if (character === null) {
    return (
      <EmptyState icon="shield" title="У вас нет героя в этой игре">
        Лист появится, когда герой будет создан.
      </EmptyState>
    );
  }
  return <Sheet character={character} />;
}
