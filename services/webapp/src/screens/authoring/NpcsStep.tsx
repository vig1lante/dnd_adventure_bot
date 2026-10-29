import { useState } from "react";
import type { Npc } from "../../api/types";
import { SelectField, TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { ATTITUDES } from "./FactionsStep";
import {
  JournalFieldsEditor,
  LinesField,
  SAVE_FIRST_REGENERATE,
  SectionForm,
  orNull,
  sameData,
} from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";
import { RefPicker } from "./RefPicker";
import { ID_HINT } from "./labels";

const LOCATION = "location:";

const newNpc = (): Npc => ({
  id: "",
  name: "",
  role: "",
  appearance: "",
  manner: "",
  motive: "",
  secret: "",
  knows: [],
  attitude: 0,
  relations: [],
  location_id: "",
  statblock: null,
  public: "",
  known_at_start: false,
});

/** Шаг «Персонажи»: раздел `npcs`; сохранённого NPC можно закрепить и перегенерировать. */
export function NpcsStep({
  draft,
  refs,
  onSave,
  onLock,
  generate,
  regenerate,
  onDirtyChange,
}: StepFormProps) {
  const [npcs, setNpcs] = useState(draft.npcs);
  // Закрепить и перегенерировать можно только NPC, которого знает сервер.
  const savedIds = new Set(draft.npcs.map((npc) => npc.id));
  const dirty = !sameData(npcs, draft.npcs);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(npcs)}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        Связи персонажей — на шаге «Связи». «Перегенерировать» пересоздаёт
        одного персонажа с пожеланием из поля «Что хочу увидеть».
      </p>
      <EntityList
        legend="Персонажи"
        items={npcs}
        onChange={setNpcs}
        create={newNpc}
        addLabel="Добавить персонажа"
        itemLabel={(npc, index) => npc.name || `Персонаж ${index + 1}`}
        refOf={(npc) => `npc:${npc.id}`}
        actions={(npc, label) =>
          savedIds.has(npc.id) && (
            <>
              <LockToggle
                lockRef={`npc:${npc.id}`}
                locks={draft.locks}
                onLock={onLock}
                name={label}
                disabled={dirty}
              />
              {regenerate(`npc:${npc.id}`, label, dirty)}
            </>
          )
        }
      >
        {(npc, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={`${ID_HINT} По нему ссылаются на персонажа.`}
              autoComplete="off"
              value={npc.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Имя"
              value={npc.name}
              onChange={(event) => update({ name: event.target.value })}
            />
            <TextField
              label="Роль"
              value={npc.role}
              onChange={(event) => update({ role: event.target.value })}
            />
            <TextField
              label="Внешность"
              value={npc.appearance}
              onChange={(event) => update({ appearance: event.target.value })}
            />
            <TextField
              label="Манера"
              value={npc.manner}
              onChange={(event) => update({ manner: event.target.value })}
            />
            <TextField
              label="Мотив"
              value={npc.motive}
              onChange={(event) => update({ motive: event.target.value })}
            />
            <TextAreaField
              label="Тайна"
              hint="Знает только мастер."
              rows={2}
              value={npc.secret}
              onChange={(event) => update({ secret: event.target.value })}
            />
            <LinesField
              label="Что знает"
              hint="По одному на строку."
              value={npc.knows}
              onChange={(knows) => update({ knows })}
            />
            <SelectField
              label="Отношение к отряду"
              options={ATTITUDES}
              value={String(npc.attitude)}
              onChange={(value) => update({ attitude: Number(value) })}
            />
            <RefPicker
              label="Где находится"
              placeholder="— выберите —"
              kinds={["location"]}
              refs={refs}
              value={
                npc.location_id === "" ? "" : `${LOCATION}${npc.location_id}`
              }
              onChange={(ref) =>
                update({ location_id: ref.slice(LOCATION.length) })
              }
            />
            <TextField
              label="Статблок — id"
              hint="Если персонаж дерётся: монстр SRD или свой статблок. Пусто — не дерётся."
              autoComplete="off"
              value={npc.statblock ?? ""}
              onChange={(event) =>
                update({ statblock: orNull(event.target.value) })
              }
            />
            <JournalFieldsEditor value={npc} onChange={update} />
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
