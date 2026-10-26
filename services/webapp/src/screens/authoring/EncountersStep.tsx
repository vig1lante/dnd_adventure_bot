import { useState } from "react";
import type { Encounter, MonsterGroup } from "../../api/types";
import { SelectField, Switch, TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import {
  SAVE_FIRST_REGENERATE,
  SectionForm,
  hasBadNumber,
  sameData,
  withCurrent,
} from "./fields";
import type { SectionWrite, StepFormProps } from "./fields";
import { ID_HINT, crText } from "./labels";
import { LockToggle } from "./LockToggle";
import { MonsterEditor, newMonster } from "./MonsterEditor";
import { RefPicker } from "./RefPicker";

const LOCATION = "location:";

const newEncounter = (): Encounter => ({
  id: "",
  location_id: "",
  trigger: "",
  tactics: "",
  monsters: [],
  pc_zone: "here",
  boss: false,
});

const newGroup = (): MonsterGroup => ({
  monster_id: "",
  count: 1,
  zone: "here",
});

/** Сколько монстров в группе: от 1 до 20 (дополнение B6, §5). */
const COUNTS = Array.from({ length: 20 }, (_, index) => ({
  value: String(index + 1),
  label: String(index + 1),
}));

/**
 * Шаг «Бои и боссы»: бои (раздел `encounters`) и свои статблоки (раздел `monsters`). Монстр
 * группы — из справочника SRD или свой статблок. Статблоки пишутся первыми: на них ссылаются бои.
 */
export function EncountersStep({
  draft,
  refs,
  srd,
  onSaveSections,
  onLock,
  generate,
  regenerate,
  onDirtyChange,
}: StepFormProps) {
  const [encounters, setEncounters] = useState(draft.encounters);
  const [monsters, setMonsters] = useState(draft.monsters);
  const encountersDirty = !sameData(encounters, draft.encounters);
  const monstersDirty = !sameData(monsters, draft.monsters);
  const dirty = encountersDirty || monstersDirty;
  // Закрепить и перегенерировать можно только то, что знает сервер.
  const savedEncounters = new Set(draft.encounters.map(({ id }) => id));
  const savedMonsters = new Set(draft.monsters.map(({ id }) => id));

  const monsterOptions = [
    ...srd.list.map((monster) => ({
      value: monster.id,
      label: `${monster.name} (SRD, опасность ${crText(monster.cr)})`,
    })),
    ...monsters
      .filter((monster) => monster.id !== "")
      .map((monster) => ({
        value: monster.id,
        label: `${monster.name || monster.id} (свой)`,
      })),
  ];
  const locationOf = (encounter: Encounter) =>
    draft.locations.find((location) => location.id === encounter.location_id);
  /**
   * Останется ли бой без ссылки своей локации. Сборка сервера (`assemble._linked`) сама ставит
   * незакреплённой локации без боя первый бой этой локации; иначе бой выбирают на «Наполнении».
   */
  const unlinked = (encounter: Encounter) => {
    const location = locationOf(encounter);
    if (location === undefined || location.encounter_id === encounter.id) {
      return false;
    }
    return (
      draft.locks.includes(`location:${location.id}`) ||
      location.encounter_id !== null ||
      encounters.find((other) => other.location_id === location.id) !==
        encounter
    );
  };
  const zonesOf = (encounter: Encounter) =>
    (locationOf(encounter)?.zones ?? []).map((zone) => ({
      value: zone.id,
      label: zone.name || zone.id,
    }));

  const save = () => {
    const writes: SectionWrite[] = [];
    if (monstersDirty) writes.push({ section: "monsters", data: monsters });
    if (encountersDirty || writes.length === 0) {
      writes.push({ section: "encounters", data: encounters });
    }
    onSaveSections(writes);
  };

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={save}
      canSave={!hasBadNumber(monsters)}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        Бой без ссылки локации сборка привяжет сама, если локация не закреплена.
        Закреплённой локации или локации с другим боем бой выбирают в поле «Бой»
        на шаге «Наполнение». «Перегенерировать» пересоздаёт один бой с
        пожеланием из поля «Что хочу увидеть».
      </p>
      <EntityList
        legend="Бои"
        items={encounters}
        onChange={setEncounters}
        create={newEncounter}
        addLabel="Добавить бой"
        itemLabel={(encounter, index) => `Бой ${encounter.id || index + 1}`}
        actions={(encounter, label) =>
          savedEncounters.has(encounter.id) && (
            <>
              <LockToggle
                lockRef={`encounter:${encounter.id}`}
                locks={draft.locks}
                onLock={onLock}
                name={label}
                disabled={dirty}
              />
              {regenerate(`encounter:${encounter.id}`, label, dirty)}
            </>
          )
        }
      >
        {(encounter, update) => {
          const zones = zonesOf(encounter);
          return (
            <>
              <TextField
                label="Идентификатор"
                hint={`${ID_HINT} По нему локация ссылается на бой.`}
                autoComplete="off"
                value={encounter.id}
                onChange={(event) => update({ id: event.target.value })}
              />
              <RefPicker
                label="Локация"
                placeholder="— выберите —"
                kinds={["location"]}
                refs={refs}
                value={
                  encounter.location_id === ""
                    ? ""
                    : `${LOCATION}${encounter.location_id}`
                }
                onChange={(ref) =>
                  update({ location_id: ref.slice(LOCATION.length) })
                }
              />
              {unlinked(encounter) && (
                <p className="form-field__hint">
                  Локация не ссылается на этот бой: выберите его в поле «Бой» на
                  шаге «Наполнение».
                </p>
              )}
              <TextAreaField
                label="Что запускает бой"
                hint="Знает только мастер."
                rows={2}
                value={encounter.trigger}
                onChange={(event) => update({ trigger: event.target.value })}
              />
              <TextAreaField
                label="Тактика"
                rows={2}
                value={encounter.tactics}
                onChange={(event) => update({ tactics: event.target.value })}
              />
              <SelectField
                label="Где стоят герои"
                hint="Зона локации, где бой застаёт отряд."
                options={withCurrent(zones, encounter.pc_zone)}
                value={encounter.pc_zone}
                onChange={(pc_zone) => update({ pc_zone })}
              />
              <Switch
                label="Босс"
                hint="Финальный или ключевой бой: проверка ждёт от него высокой сложности."
                checked={encounter.boss}
                onChange={(boss) => update({ boss })}
              />
              <EntityList
                legend="Монстры"
                items={encounter.monsters}
                onChange={(groups) => update({ monsters: groups })}
                create={newGroup}
                addLabel="Добавить монстров"
                itemLabel={(_group, index) => `Группа ${index + 1}`}
              >
                {(group, change) => (
                  <>
                    <SelectField
                      label="Монстр"
                      placeholder="— выберите —"
                      error={
                        srd.error === null
                          ? null
                          : `Справочник монстров не загрузился: ${srd.error}`
                      }
                      options={withCurrent(monsterOptions, group.monster_id)}
                      value={group.monster_id}
                      onChange={(monster_id) => change({ monster_id })}
                    />
                    <SelectField
                      label="Сколько"
                      options={COUNTS}
                      value={String(group.count)}
                      onChange={(count) => change({ count: Number(count) })}
                    />
                    <SelectField
                      label="Зона"
                      options={withCurrent(zones, group.zone)}
                      value={group.zone}
                      onChange={(zone) => change({ zone })}
                    />
                  </>
                )}
              </EntityList>
            </>
          );
        }}
      </EntityList>
      <EntityList
        legend="Свои монстры"
        items={monsters}
        onChange={setMonsters}
        create={() => newMonster(srd.list)}
        addLabel="Добавить своего монстра"
        itemLabel={(monster, index) =>
          monster.name || `Свой монстр ${index + 1}`
        }
        actions={(monster, label) =>
          savedMonsters.has(monster.id) && (
            <LockToggle
              lockRef={`monster:${monster.id}`}
              locks={draft.locks}
              onLock={onLock}
              name={label}
              disabled={dirty}
            />
          )
        }
      >
        {(monster, update) => (
          <MonsterEditor monster={monster} update={update} srd={srd.list} />
        )}
      </EntityList>
    </SectionForm>
  );
}
