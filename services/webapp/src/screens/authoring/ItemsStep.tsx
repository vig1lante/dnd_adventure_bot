import { useState } from "react";
import type { Draft, Feature, Item, Location } from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import {
  JournalFieldsEditor,
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

const newItem = (): Item => ({
  id: "",
  name: "",
  description: "",
  base_item: null,
  holder: "",
  public: "",
  known_at_start: false,
});

/** Деталь-тайник, которая раскрывает предмет (`reveals: item`); нет — предмет не спрятан. */
export const hidingFeature = (
  item: Item,
  locations: readonly Location[],
): Feature | undefined =>
  locations
    .flatMap((location) => location.features)
    .find((feature) =>
      feature.reveals.some(
        (reveal) => reveal.kind === "item" && reveal.id === item.id,
      ),
    );

/**
 * Предметы, которые лежат открыто: держатель — локация, и ни одна деталь их не прячет (B6 §6).
 * `place` — название локации для людей.
 */
export function openItems(draft: Draft): { item: Item; place: string }[] {
  return draft.items.flatMap((item) => {
    if (
      !item.holder.startsWith(LOCATION) ||
      hidingFeature(item, draft.locations) !== undefined
    ) {
      return [];
    }
    const id = item.holder.slice(LOCATION.length);
    const location = draft.locations.find((entry) => entry.id === id);
    return [{ item, place: location?.name || id }];
  });
}

/**
 * Шаг «Предметы и награды»: все предметы черновика (раздел `items`). Тот же раздел правит
 * «Наполнение» (предметы локации и тайники), поэтому экраны предупреждают друг о друге.
 */
export function ItemsStep({
  draft,
  refs,
  onSave,
  onLock,
  generate,
  regenerate,
  onDirtyChange,
  unsavedElsewhere,
}: StepFormProps) {
  const [items, setItems] = useState(draft.items);
  // Закрепить и перегенерировать можно только предмет, который знает сервер.
  const savedIds = new Set(draft.items.map(({ id }) => id));
  const dirty = !sameData(items, draft.items);

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(items)}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      warning={unsavedElsewhere}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        Все предметы приключения: награды, ключи, вещи-улики. Спрятать предмет в
        тайник — отметкой у детали на шаге «Наполнение». «Перегенерировать»
        пересоздаёт один предмет с пожеланием из поля «Что хочу увидеть».
      </p>
      <EntityList
        legend="Предметы"
        items={items}
        onChange={setItems}
        create={newItem}
        addLabel="Добавить предмет"
        itemLabel={(item, index) => item.name || `Предмет ${index + 1}`}
        actions={(item, label) =>
          savedIds.has(item.id) && (
            <>
              <LockToggle
                lockRef={`item:${item.id}`}
                locks={draft.locks}
                onLock={onLock}
                name={label}
                disabled={dirty}
              />
              {regenerate(`item:${item.id}`, label, dirty)}
            </>
          )
        }
      >
        {(item, update) => {
          const cache = hidingFeature(item, draft.locations);
          return (
            <>
              <TextField
                label="Идентификатор"
                hint={ID_HINT}
                autoComplete="off"
                value={item.id}
                onChange={(event) => update({ id: event.target.value })}
              />
              <TextField
                label="Название"
                value={item.name}
                onChange={(event) => update({ name: event.target.value })}
              />
              <TextAreaField
                label="Описание"
                rows={2}
                value={item.description}
                onChange={(event) =>
                  update({ description: event.target.value })
                }
              />
              <RefPicker
                label="Где лежит"
                hint="Локация — предмет лежит в ней (открыто или в тайнике); персонаж — предмет у него."
                placeholder="— выберите —"
                kinds={["location", "npc"]}
                refs={refs}
                value={item.holder}
                onChange={(holder) => update({ holder })}
              />
              {item.holder.startsWith(LOCATION) && (
                <p className="form-field__hint">
                  {cache === undefined
                    ? "Лежит открыто."
                    : `В тайнике «${cache.name || cache.id}».`}
                </p>
              )}
              <TextField
                label="Предмет SRD"
                hint="id обычного предмета SRD, если это он. Можно пусто."
                autoComplete="off"
                value={item.base_item ?? ""}
                onChange={(event) =>
                  update({ base_item: orNull(event.target.value) })
                }
              />
              <JournalFieldsEditor value={item} onChange={update} />
            </>
          );
        }}
      </EntityList>
    </SectionForm>
  );
}
