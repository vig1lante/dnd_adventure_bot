import { useState } from "react";
import type { Clue, Location, Revelation } from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { SAVE_FIRST_REGENERATE, SectionForm } from "./fields";
import type { StepFormProps } from "./fields";
import { sameData } from "./fieldValues";
import type { Option } from "./fieldValues";
import { LockToggle } from "./LockToggle";
import { RefPicker } from "./RefPicker";
import { ID_HINT } from "./labels";

const newRevelation = (): Revelation => ({ id: "", statement: "", clues: [] });

const newClue = (): Clue => ({ id: "", description: "", where: "" });

/**
 * Детали локаций черновика для «Где лежит» улики: в справочнике ссылок сервера деталей нет,
 * а id деталей уникальны во всём приключении.
 */
const featureOptions = (locations: readonly Location[]): Option[] =>
  locations.flatMap((location) =>
    location.features
      .filter((feature) => feature.id !== "")
      .map((feature) => ({
        value: `feature:${feature.id}`,
        label: `${feature.name || feature.id} (деталь, ${location.name || location.id})`,
      })),
  );

/**
 * Шаг «Улики»: откровения и их улики (раздел `revelations`). Улик на откровение нужно не меньше
 * трёх: экран подсказывает, а ошибку даёт проверка сервера.
 */
export function CluesStep({
  draft,
  refs,
  onSave,
  onLock,
  generate,
  regenerate,
  onDirtyChange,
}: StepFormProps) {
  const [revelations, setRevelations] = useState(draft.revelations);
  // Закрепить и перегенерировать можно только откровение, которое знает сервер.
  const savedIds = new Set(draft.revelations.map(({ id }) => id));
  const dirty = !sameData(revelations, draft.revelations);
  const features = featureOptions(draft.locations);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(revelations)}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        Откровение — вывод, к которому должны прийти игроки. Дайте каждому не
        меньше трёх улик в разных местах: так отряд доберётся до вывода, даже
        если пропустит одну-две. «Перегенерировать» пересоздаёт улики одного
        откровения с пожеланием из поля «Что хочу увидеть».
      </p>
      <p className="form-field__hint">
        Откровения пишет и генерация Замысла: незакреплённые она заменит вместе
        с уликами — закрепите нужные.
      </p>
      <EntityList
        legend="Откровения"
        items={revelations}
        onChange={setRevelations}
        create={newRevelation}
        addLabel="Добавить откровение"
        itemLabel={(revelation, index) =>
          `Откровение ${revelation.id || index + 1}`
        }
        refOf={(revelation) => `revelation:${revelation.id}`}
        actions={(revelation, label) =>
          savedIds.has(revelation.id) && (
            <>
              <LockToggle
                lockRef={`revelation:${revelation.id}`}
                locks={draft.locks}
                onLock={onLock}
                name={label}
                disabled={dirty}
              />
              {regenerate(`revelation:${revelation.id}`, label, dirty)}
            </>
          )
        }
      >
        {(revelation, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={ID_HINT}
              autoComplete="off"
              value={revelation.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextAreaField
              label="Вывод"
              hint="Что должны понять игроки. Знает только мастер."
              rows={2}
              value={revelation.statement}
              onChange={(event) => update({ statement: event.target.value })}
            />
            {revelation.clues.length < 3 ? (
              <p className="authoring-warning">
                Улик меньше трёх: проверка не пропустит публикацию.
              </p>
            ) : (
              <p className="form-field__hint">
                Улик: {revelation.clues.length}.
              </p>
            )}
            <EntityList
              legend="Улики"
              items={revelation.clues}
              onChange={(clues) => update({ clues })}
              create={newClue}
              addLabel="Добавить улику"
              itemLabel={(clue, index) => `Улика ${clue.id || index + 1}`}
              refOf={(clue) => `revelation:${revelation.id}/clue:${clue.id}`}
            >
              {(clue, change) => (
                <>
                  <TextField
                    label="Идентификатор улики"
                    hint={ID_HINT}
                    autoComplete="off"
                    value={clue.id}
                    onChange={(event) => change({ id: event.target.value })}
                  />
                  <TextAreaField
                    label="Описание"
                    rows={2}
                    value={clue.description}
                    onChange={(event) =>
                      change({ description: event.target.value })
                    }
                  />
                  <RefPicker
                    label="Где лежит"
                    hint="Деталь локации, персонаж или предмет."
                    placeholder="— выберите —"
                    kinds={["npc", "item"]}
                    refs={refs}
                    extra={features}
                    value={clue.where}
                    onChange={(where) => change({ where })}
                  />
                </>
              )}
            </EntityList>
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
