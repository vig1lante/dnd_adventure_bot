import type {
  Ability,
  ActionRange,
  MonsterAction,
  MonsterActionKind,
  MonsterDef,
  SrdMonster,
} from "../../api/types";
import { formatMeters } from "../../ui/format";
import { SelectField, Switch, TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { LinesField, NumberField } from "./fields";
import { orNull, withCurrent } from "./fieldValues";
import {
  ABILITY_OPTIONS,
  ACTION_KINDS,
  DAMAGE_TYPE_OPTIONS,
  ID_HINT,
  RANGE_OPTIONS,
  crText,
} from "./labels";

// Свой статблок — `MonsterDef` движка. Числа статблока автор задаёт сам; бросает и считает по ним
// только сервер, а правило «опыт по опасности» проверяет его проверка (B6 §4).

/** Скорости на выбор: шаг 5 футов (1,5 м) до 120 футов. В данных — футы, на экране — метры. */
const SPEEDS = Array.from({ length: 25 }, (_, index) => index * 5);

/** Варианты скорости в метрах; скорость из данных вне шага остаётся в списке на своём месте. */
const speedOptions = (current: number) =>
  [...new Set([...SPEEDS, current])]
    .sort((a, b) => a - b)
    .map((feet) => ({ value: String(feet), label: formatMeters(feet) }));

/** Опасности монстров справочника SRD: проверка сервера примет только их. */
const crOptions = (srd: readonly SrdMonster[]) =>
  [...new Set(srd.map((monster) => monster.cr))]
    .sort((a, b) => a - b)
    .map((cr) => ({ value: String(cr), label: crText(cr) }));

/** Подсказка опыта: пары «опасность — опыт» из справочника SRD, как их отдал сервер. */
function xpHint(srd: readonly SrdMonster[]): string {
  const pairs = [...new Map(srd.map((m) => [m.cr, m.xp])).entries()]
    .sort(([a], [b]) => a - b)
    .map(([cr, xp]) => `${crText(cr)} — ${xp}`);
  const intro = "Опыт — как у монстров SRD той же опасности";
  return pairs.length === 0 ? `${intro}.` : `${intro}: ${pairs.join(", ")}.`;
}

const newAction = (): MonsterAction => ({
  id: "",
  name: "",
  kind: "attack",
  attack_bonus: null,
  damage: null,
  damage_type: null,
  range: "melee",
  save: null,
  dc: null,
  half_on_save: false,
  targets: 1,
  condition: null,
  multiattack: [],
  summary: "",
  reach: false,
  secondary_damage: null,
  secondary_damage_type: null,
});

interface ActionFieldsProps {
  action: MonsterAction;
  update: (patch: Partial<MonsterAction>) => void;
}

/** Урон действия: запись костей и тип; пусто — без урона. */
function DamageFields({ action, update }: ActionFieldsProps) {
  return (
    <>
      <TextField
        label="Урон"
        hint="Запись костей, например 1d6+2. Пусто — без урона."
        autoComplete="off"
        value={action.damage ?? ""}
        onChange={(event) => update({ damage: orNull(event.target.value) })}
      />
      <SelectField
        label="Тип урона"
        placeholder="—"
        options={withCurrent(DAMAGE_TYPE_OPTIONS, action.damage_type ?? "")}
        value={action.damage_type ?? ""}
        onChange={(type) => update({ damage_type: orNull(type) })}
      />
    </>
  );
}

/** Поля действия по его виду: атака, спасбросок, мультиатака или другое. */
function ActionFields({ action, update }: ActionFieldsProps) {
  return (
    <>
      <TextField
        label="Идентификатор действия"
        hint="По нему мультиатака называет действие."
        autoComplete="off"
        value={action.id}
        onChange={(event) => update({ id: event.target.value })}
      />
      <TextField
        label="Название действия"
        value={action.name}
        onChange={(event) => update({ name: event.target.value })}
      />
      <SelectField
        label="Вид"
        options={ACTION_KINDS}
        value={action.kind}
        onChange={(kind) => update({ kind: kind as MonsterActionKind })}
      />
      {action.kind === "attack" && (
        <>
          <NumberField
            optional
            label="Бонус атаки"
            value={action.attack_bonus}
            onChange={(attack_bonus) => update({ attack_bonus })}
          />
          <DamageFields action={action} update={update} />
          <SelectField
            label="Дальность"
            options={RANGE_OPTIONS}
            value={action.range}
            onChange={(range) => update({ range: range as ActionRange })}
          />
        </>
      )}
      {action.kind === "save" && (
        <>
          <SelectField
            label="Спасбросок"
            placeholder="— выберите —"
            options={ABILITY_OPTIONS}
            value={action.save ?? ""}
            onChange={(save) =>
              update({ save: save === "" ? null : (save as Ability) })
            }
          />
          <NumberField
            optional
            label="Сл спасброска"
            value={action.dc}
            onChange={(dc) => update({ dc })}
          />
          <DamageFields action={action} update={update} />
          <Switch
            label="Половина урона при успехе"
            checked={action.half_on_save}
            onChange={(half_on_save) => update({ half_on_save })}
          />
          <NumberField
            label="Целей"
            hint="Сколько существ захватывает действие."
            value={action.targets}
            onChange={(targets) => update({ targets })}
          />
        </>
      )}
      {action.kind === "multiattack" && (
        <LinesField
          label="Действия по очереди"
          hint="id своих действий, по одному на строку."
          value={action.multiattack}
          onChange={(multiattack) => update({ multiattack })}
        />
      )}
      <TextAreaField
        label="Описание"
        rows={2}
        value={action.summary}
        onChange={(event) => update({ summary: event.target.value })}
      />
    </>
  );
}

interface MonsterEditorProps {
  monster: MonsterDef;
  update: (patch: Partial<MonsterDef>) => void;
  /** Справочник SRD: опасности на выбор и подсказка опыта. */
  srd: readonly SrdMonster[];
}

/**
 * Свой статблок: КД, хиты и кость хитов, скорость в метрах, характеристики, действия, особенности,
 * опасность и опыт. Поля, которых здесь нет (спасброски, навыки, сопротивления, метки), уходят на
 * сервер как были.
 */
export function MonsterEditor({ monster, update, srd }: MonsterEditorProps) {
  return (
    <>
      <TextField
        label="Идентификатор"
        hint={`${ID_HINT} По нему бой ссылается на монстра.`}
        autoComplete="off"
        value={monster.id}
        onChange={(event) => update({ id: event.target.value })}
      />
      <TextField
        label="Название"
        value={monster.name}
        onChange={(event) => update({ name: event.target.value })}
      />
      <TextField
        label="Название по-английски"
        value={monster.name_en}
        onChange={(event) => update({ name_en: event.target.value })}
      />
      <SelectField
        label="Опасность (cr)"
        options={withCurrent(crOptions(srd), String(monster.cr))}
        value={String(monster.cr)}
        onChange={(cr) => update({ cr: Number(cr) })}
      />
      <NumberField
        label="Опыт (xp)"
        hint={xpHint(srd)}
        value={monster.xp}
        onChange={(xp) => update({ xp })}
      />
      <NumberField
        label="Класс доспеха (КД)"
        value={monster.ac}
        onChange={(ac) => update({ ac })}
      />
      <NumberField
        label="Хиты (HP)"
        value={monster.hp}
        onChange={(hp) => update({ hp })}
      />
      <TextField
        label="Кость хитов"
        hint="Запись костей, например 4d8+4."
        autoComplete="off"
        value={monster.hp_dice}
        onChange={(event) => update({ hp_dice: event.target.value })}
      />
      <SelectField
        label="Скорость"
        hint="Скорость ходьбы."
        options={speedOptions(monster.speed)}
        value={String(monster.speed)}
        onChange={(feet) => update({ speed: Number(feet) })}
      />
      <fieldset className="form-group">
        <legend className="form-legend">Характеристики</legend>
        {ABILITY_OPTIONS.map(({ value: ability, label }) => (
          <NumberField
            key={ability}
            label={label}
            value={monster.scores[ability]}
            onChange={(score) =>
              update({ scores: { ...monster.scores, [ability]: score } })
            }
          />
        ))}
      </fieldset>
      <EntityList
        legend="Действия"
        items={monster.actions}
        onChange={(actions) => update({ actions })}
        create={newAction}
        addLabel="Добавить действие"
        itemLabel={(action, index) => action.name || `Действие ${index + 1}`}
        refOf={(action) => `monster:${monster.id}/action:${action.id}`}
      >
        {(action, change) => <ActionFields action={action} update={change} />}
      </EntityList>
      <LinesField
        label="Особенности"
        hint="По одной на строку: текст для мастера."
        value={monster.traits}
        onChange={(traits) => update({ traits })}
      />
    </>
  );
}
