import { useState } from "react";
import type { Faction } from "../../api/types";
import { SelectField, TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { JournalFieldsEditor, SectionForm, orNull, sameData } from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";
import { ID_HINT } from "./labels";

// Ступени отношения к отряду, общие у фракций и NPC: от −3 до 3.
export const ATTITUDES = [
  { value: "-3", label: "−3 — враги" },
  { value: "-2", label: "−2" },
  { value: "-1", label: "−1" },
  { value: "0", label: "0 — безразличны" },
  { value: "1", label: "+1" },
  { value: "2", label: "+2" },
  { value: "3", label: "+3 — союзники" },
];

const newFaction = (): Faction => ({
  id: "",
  name: "",
  goal: "",
  methods: "",
  leader: null,
  base_location: null,
  secret: "",
  attitude_to_party: 0,
  public: "",
  known_at_start: false,
});

/** Шаг «Фракции»: раздел `factions`; лидер — ссылка на NPC, база — id локации. */
export function FactionsStep({
  draft,
  onSave,
  onLock,
  generate,
  onDirtyChange,
}: StepFormProps) {
  const [factions, setFactions] = useState(draft.factions);
  // Закрепить можно только фракцию, которую знает сервер.
  const savedIds = new Set(draft.factions.map((faction) => faction.id));
  const dirty = !sameData(factions, draft.factions);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(factions)}
      dirty={dirty}
      generate={generate(dirty)}
    >
      <EntityList
        legend="Фракции"
        items={factions}
        onChange={setFactions}
        create={newFaction}
        addLabel="Добавить фракцию"
        itemLabel={(faction, index) => faction.name || `Фракция ${index + 1}`}
        refOf={(faction) => `faction:${faction.id}`}
        actions={(faction, label) =>
          savedIds.has(faction.id) && (
            <LockToggle
              lockRef={`faction:${faction.id}`}
              locks={draft.locks}
              onLock={onLock}
              name={label}
              disabled={dirty}
            />
          )
        }
      >
        {(faction, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={ID_HINT}
              autoComplete="off"
              value={faction.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Название"
              value={faction.name}
              onChange={(event) => update({ name: event.target.value })}
            />
            <TextAreaField
              label="Цель"
              rows={2}
              value={faction.goal}
              onChange={(event) => update({ goal: event.target.value })}
            />
            <TextAreaField
              label="Методы"
              rows={2}
              value={faction.methods}
              onChange={(event) => update({ methods: event.target.value })}
            />
            <TextField
              label="Лидер"
              hint="Ссылка на персонажа: npc:<id>. Пусто — без лидера."
              autoComplete="off"
              value={faction.leader ?? ""}
              onChange={(event) =>
                update({ leader: orNull(event.target.value) })
              }
            />
            <TextField
              label="База — id локации"
              hint="Пусто — без базы."
              autoComplete="off"
              value={faction.base_location ?? ""}
              onChange={(event) =>
                update({ base_location: orNull(event.target.value) })
              }
            />
            <TextAreaField
              label="Тайна"
              hint="Знает только мастер."
              rows={2}
              value={faction.secret}
              onChange={(event) => update({ secret: event.target.value })}
            />
            <SelectField
              label="Отношение к отряду"
              options={ATTITUDES}
              value={String(faction.attitude_to_party)}
              onChange={(value) => update({ attitude_to_party: Number(value) })}
            />
            <JournalFieldsEditor value={faction} onChange={update} />
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
