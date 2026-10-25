import type {
  Ability,
  CheckSpec,
  RevealKind,
  RevealRef,
  Skill,
} from "../../api/types";
import { SelectField, Switch, TextField } from "../fields";
import { EntityList } from "./EntityList";
import {
  ABILITY_OPTIONS,
  DC_OPTIONS,
  REVEAL_KINDS,
  SKILL_OPTIONS,
} from "./labels";

// Поля находок места, общие у детали и секрета локации: проверка и что раскрывается.

// Проверка, с которой поле начинает, когда автор её включает; дальше он выбирает сам.
const NEW_CHECK: CheckSpec = { ability: "wis", skill: "perception", dc: 15 };

interface CheckFieldProps {
  value: CheckSpec | null;
  onChange: (value: CheckSpec | null) => void;
}

/** Проверка находки: характеристика, необязательный навык и Сл только из таблицы. */
export function CheckField({ value, onChange }: CheckFieldProps) {
  return (
    <>
      <Switch
        label="Нужна проверка"
        hint="Без проверки находку получают, просто осмотрев место."
        checked={value !== null}
        onChange={(checked) => onChange(checked ? NEW_CHECK : null)}
      />
      {value !== null && (
        <>
          <SelectField
            label="Характеристика"
            options={ABILITY_OPTIONS}
            value={value.ability}
            onChange={(ability) =>
              onChange({ ...value, ability: ability as Ability })
            }
          />
          <SelectField
            label="Навык"
            placeholder="без навыка"
            options={SKILL_OPTIONS}
            value={value.skill ?? ""}
            onChange={(skill) =>
              onChange({
                ...value,
                skill: skill === "" ? null : (skill as Skill),
              })
            }
          />
          <SelectField
            label="Сл"
            hint="Только из таблицы: 5, 10, 15, 20, 25 или 30."
            options={DC_OPTIONS}
            value={String(value.dc)}
            onChange={(dc) => onChange({ ...value, dc: Number(dc) })}
          />
        </>
      )}
    </>
  );
}

interface RevealsFieldProps {
  value: readonly RevealRef[];
  /**
   * Какие виды правятся здесь. Остальные остаются на своих местах как есть: у детали предметы
   * тайника отмечаются отдельно, и список не должен меняться у себя за спиной.
   */
  kinds: readonly RevealKind[];
  onChange: (reveals: RevealRef[]) => void;
}

/** Правленые находки встают на места прежних того же вида, новые — в конец: порядок не скачет. */
function mergeShown(
  all: readonly RevealRef[],
  shown: (reveal: RevealRef) => boolean,
  next: readonly RevealRef[],
): RevealRef[] {
  const rest = [...next];
  const merged = all.flatMap((reveal) => {
    if (!shown(reveal)) return [reveal];
    const replacement = rest.shift();
    return replacement === undefined ? [] : [replacement];
  });
  return [...merged, ...rest];
}

/** «Что раскрывает»: улики, предметы, флаги и выходы, которые даёт находка. */
export function RevealsField({ value, kinds, onChange }: RevealsFieldProps) {
  const shown = (reveal: RevealRef) => kinds.includes(reveal.kind);
  return (
    <EntityList
      legend="Что раскрывает"
      items={value.filter(shown)}
      onChange={(next) => onChange(mergeShown(value, shown, next))}
      create={() => ({ kind: kinds[0], id: "", value: null })}
      addLabel="Добавить находку"
      itemLabel={(reveal, index) =>
        reveal.id === ""
          ? `Находка ${index + 1}`
          : `${REVEAL_KINDS[reveal.kind]}: ${reveal.id}`
      }
    >
      {(reveal, update) => (
        <>
          <SelectField
            label="Что"
            options={kinds.map((kind) => ({
              value: kind,
              label: REVEAL_KINDS[kind],
            }))}
            value={reveal.kind}
            onChange={(kind) => update({ kind: kind as RevealKind })}
          />
          <TextField
            label="Идентификатор"
            hint="id улики, предмета, флага или выхода."
            autoComplete="off"
            value={reveal.id}
            onChange={(event) => update({ id: event.target.value })}
          />
        </>
      )}
    </EntityList>
  );
}
