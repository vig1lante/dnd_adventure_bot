import { useState } from "react";
import type { Act, Plot } from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { LinesField, SectionForm, orNull, sameData } from "./fields";
import type { StepFormProps } from "./fields";
import { LockToggle } from "./LockToggle";
import { ID_HINT } from "./labels";

const EMPTY_PLOT: Plot = { acts: [], endings: [], villain: null };

const newAct = (): Act => ({ id: "", title: "", summary: "", key_scenes: [] });

/** Шаг «Сюжет»: акты со сценами, концовки и злодей. Закрепляется целиком (`step:plot`). */
export function PlotStep({
  draft,
  onSave,
  onLock,
  generate,
  onDirtyChange,
}: StepFormProps) {
  const [plot, setPlot] = useState(draft.plot ?? EMPTY_PLOT);
  const dirty = !sameData(plot, draft.plot ?? EMPTY_PLOT);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(plot)}
      dirty={dirty}
      generate={generate(dirty)}
      lock={
        <LockToggle
          lockRef="step:plot"
          locks={draft.locks}
          onLock={onLock}
          label="Закрепить раздел"
          disabled={dirty}
        />
      }
    >
      <EntityList
        legend="Акты"
        items={plot.acts}
        onChange={(acts) => setPlot({ ...plot, acts })}
        create={newAct}
        addLabel="Добавить акт"
        itemLabel={(act, index) => act.title || `Акт ${index + 1}`}
      >
        {(act, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={ID_HINT}
              autoComplete="off"
              value={act.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Название"
              value={act.title}
              onChange={(event) => update({ title: event.target.value })}
            />
            <TextAreaField
              label="Кратко"
              rows={3}
              value={act.summary}
              onChange={(event) => update({ summary: event.target.value })}
            />
            <LinesField
              label="Ключевые сцены"
              hint="По одной на строку."
              value={act.key_scenes}
              onChange={(key_scenes) => update({ key_scenes })}
            />
          </>
        )}
      </EntityList>
      <LinesField
        label="Концовки"
        hint="По одной на строку."
        value={plot.endings}
        onChange={(endings) => setPlot({ ...plot, endings })}
      />
      <TextField
        label="Злодей"
        hint="Ссылка на персонажа или бога: npc:<id> или deity:<id>. Пусто — без злодея."
        autoComplete="off"
        value={plot.villain ?? ""}
        onChange={(event) =>
          setPlot({ ...plot, villain: orNull(event.target.value) })
        }
      />
    </SectionForm>
  );
}
