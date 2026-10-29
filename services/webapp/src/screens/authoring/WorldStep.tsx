import { useState } from "react";
import type { Region, Rumor, WorldInfo } from "../../api/types";
import { Switch, TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { JournalFieldsEditor, SectionForm, sameData } from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";
import { ID_HINT } from "./labels";

const EMPTY_WORLD: WorldInfo = { history: "", regions: [], rumors: [] };

const newRegion = (): Region => ({
  id: "",
  name: "",
  description: "",
  public: "",
  known_at_start: false,
});

const newRumor = (): Rumor => ({
  text: "",
  true: false,
  public: "",
  known_at_start: false,
});

/** Шаг «Мир»: история, края и слухи. Закрепляется только целиком (`step:world`). */
export function WorldStep({
  draft,
  onSave,
  onLock,
  generate,
  onDirtyChange,
}: StepFormProps) {
  const [world, setWorld] = useState(draft.world ?? EMPTY_WORLD);
  const dirty = !sameData(world, draft.world ?? EMPTY_WORLD);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(world)}
      dirty={dirty}
      generate={generate(dirty)}
      lock={
        <LockToggle
          lockRef="step:world"
          locks={draft.locks}
          onLock={onLock}
          label="Закрепить раздел"
          disabled={dirty}
        />
      }
    >
      <TextAreaField
        label="История мира"
        rows={5}
        value={world.history}
        onChange={(event) =>
          setWorld({ ...world, history: event.target.value })
        }
      />
      <EntityList
        legend="Края"
        items={world.regions}
        onChange={(regions) => setWorld({ ...world, regions })}
        create={newRegion}
        addLabel="Добавить край"
        itemLabel={(region, index) => region.name || `Край ${index + 1}`}
        refOf={(region) => `region:${region.id}`}
      >
        {(region, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={ID_HINT}
              autoComplete="off"
              value={region.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Название"
              value={region.name}
              onChange={(event) => update({ name: event.target.value })}
            />
            <TextAreaField
              label="Описание"
              rows={3}
              value={region.description}
              onChange={(event) => update({ description: event.target.value })}
            />
            <JournalFieldsEditor value={region} onChange={update} />
          </>
        )}
      </EntityList>
      <EntityList
        legend="Слухи"
        items={world.rumors}
        onChange={(rumors) => setWorld({ ...world, rumors })}
        create={newRumor}
        addLabel="Добавить слух"
        itemLabel={(_rumor, index) => `Слух ${index + 1}`}
      >
        {(rumor, update) => (
          <>
            <TextAreaField
              label="Слух"
              rows={2}
              value={rumor.text}
              onChange={(event) => update({ text: event.target.value })}
            />
            <Switch
              label="Слух правдив"
              hint="Знает только мастер."
              checked={rumor.true}
              onChange={(checked) => update({ true: checked })}
            />
            <JournalFieldsEditor value={rumor} onChange={update} />
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
