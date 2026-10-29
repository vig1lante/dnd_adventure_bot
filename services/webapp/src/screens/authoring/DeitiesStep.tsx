import { useState } from "react";
import type { Deity } from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import {
  JournalFieldsEditor,
  LinesField,
  SectionForm,
  orNull,
  sameData,
} from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";
import { ID_HINT } from "./labels";

const newDeity = (): Deity => ({
  id: "",
  name: "",
  title: "",
  domains: [],
  symbol: "",
  dogma: "",
  appearance: "",
  attitude_to_mortals: "",
  secret: "",
  avatar_statblock: null,
  public: "",
  known_at_start: false,
});

/** Шаг «Боги»: раздел `deities`; каждого сохранённого бога можно закрепить (`deity:<id>`). */
export function DeitiesStep({
  draft,
  onSave,
  onLock,
  generate,
  onDirtyChange,
}: StepFormProps) {
  const [deities, setDeities] = useState(draft.deities);
  // Закрепить можно только бога, которого знает сервер: у нового ещё нет записанного id.
  const savedIds = new Set(draft.deities.map((deity) => deity.id));
  const dirty = !sameData(deities, draft.deities);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(deities)}
      dirty={dirty}
      generate={generate(dirty)}
    >
      <EntityList
        legend="Боги"
        items={deities}
        onChange={setDeities}
        create={newDeity}
        addLabel="Добавить бога"
        itemLabel={(deity, index) => deity.name || `Бог ${index + 1}`}
        refOf={(deity) => `deity:${deity.id}`}
        actions={(deity, label) =>
          savedIds.has(deity.id) && (
            <LockToggle
              lockRef={`deity:${deity.id}`}
              locks={draft.locks}
              onLock={onLock}
              name={label}
              disabled={dirty}
            />
          )
        }
      >
        {(deity, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={`${ID_HINT} По нему ссылаются на бога.`}
              autoComplete="off"
              value={deity.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Имя"
              value={deity.name}
              onChange={(event) => update({ name: event.target.value })}
            />
            <TextField
              label="Титул"
              value={deity.title}
              onChange={(event) => update({ title: event.target.value })}
            />
            <LinesField
              label="Домены"
              hint="По одному на строку."
              value={deity.domains}
              onChange={(domains) => update({ domains })}
            />
            <TextField
              label="Символ"
              value={deity.symbol}
              onChange={(event) => update({ symbol: event.target.value })}
            />
            <TextAreaField
              label="Догма"
              rows={2}
              value={deity.dogma}
              onChange={(event) => update({ dogma: event.target.value })}
            />
            <TextAreaField
              label="Облик"
              rows={2}
              value={deity.appearance}
              onChange={(event) => update({ appearance: event.target.value })}
            />
            <TextField
              label="Отношение к смертным"
              value={deity.attitude_to_mortals}
              onChange={(event) =>
                update({ attitude_to_mortals: event.target.value })
              }
            />
            <TextAreaField
              label="Тайна"
              hint="Знает только мастер."
              rows={2}
              value={deity.secret}
              onChange={(event) => update({ secret: event.target.value })}
            />
            <TextField
              label="Аватар — id статблока"
              hint="Необязательно: монстр SRD или свой статблок."
              autoComplete="off"
              value={deity.avatar_statblock ?? ""}
              onChange={(event) =>
                update({ avatar_statblock: orNull(event.target.value) })
              }
            />
            <JournalFieldsEditor value={deity} onChange={update} />
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
