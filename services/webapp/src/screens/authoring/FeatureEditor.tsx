import { useId } from "react";
import type { Feature, Item } from "../../api/types";
import { CheckList, Switch, TextAreaField, TextField } from "../fields";
import { orNull } from "./fieldValues";
import { CheckField, RevealsField } from "./FindingFields";
import { ID_HINT } from "./labels";

interface FeatureEditorProps {
  feature: Feature;
  update: (patch: Partial<Feature>) => void;
  /** Предметы этой локации (`holder: location:<id>`), которые можно положить в тайник. */
  items: readonly Item[];
  /** id предметов, уже спрятанных в других деталях этой локации: предмет лежит в одном месте. */
  hiddenElsewhere: ReadonlySet<string>;
}

/**
 * Деталь локации: описание, скрытность, замок, проверка, что раскрывает и предметы в тайнике.
 * Предмет «в тайнике» — это `reveals: [{kind: "item", id}]` детали при держателе-локации
 * (формат DND-1, проверка движка `_check_revealed_items`).
 */
export function FeatureEditor({
  feature,
  update,
  items,
  hiddenElsewhere,
}: FeatureEditorProps) {
  const name = useId();
  const hidden = feature.reveals
    .filter((reveal) => reveal.kind === "item")
    .map((reveal) => reveal.id);
  const known = new Set(items.map((item) => item.id));
  // Предмет из тайника, которого нет среди предметов локации (его ещё создаст шаг «Предметы»
  // или его удалили), остаётся отмеченным: так его видно и его можно убрать из тайника.
  const choices = [
    ...items.map((item) => ({
      value: item.id,
      label: item.name || item.id,
      disabled: hiddenElsewhere.has(item.id) && !hidden.includes(item.id),
    })),
    ...hidden
      .filter((id) => !known.has(id))
      .map((id) => ({ value: id, label: `${id} — предмета здесь нет` })),
  ];
  const setHidden = (ids: string[]) =>
    update({
      reveals: [
        ...feature.reveals.filter(
          (reveal) => reveal.kind !== "item" || ids.includes(reveal.id),
        ),
        ...ids
          .filter((id) => !hidden.includes(id))
          .map((id) => ({ kind: "item" as const, id, value: null })),
      ],
    });

  return (
    <>
      <TextField
        label="Идентификатор"
        hint={`${ID_HINT} Уникален во всём приключении.`}
        autoComplete="off"
        value={feature.id}
        onChange={(event) => update({ id: event.target.value })}
      />
      <TextField
        label="Название"
        value={feature.name}
        onChange={(event) => update({ name: event.target.value })}
      />
      <TextAreaField
        label="Описание"
        rows={2}
        value={feature.description}
        onChange={(event) => update({ description: event.target.value })}
      />
      <Switch
        label="Скрытая деталь"
        hint="Игроки замечают её только после проверки или события."
        checked={feature.hidden}
        onChange={(checked) => update({ hidden: checked })}
      />
      <TextField
        label="Замок"
        hint="item:<id ключа> или flag:<имя>. Пусто — не заперто."
        autoComplete="off"
        value={feature.locked_by ?? ""}
        onChange={(event) => update({ locked_by: orNull(event.target.value) })}
      />
      <CheckField
        value={feature.check}
        onChange={(check) => update({ check })}
      />
      <RevealsField
        value={feature.reveals}
        kinds={["clue", "flag", "exit"]}
        onChange={(reveals) => update({ reveals })}
      />
      {choices.length > 0 ? (
        <CheckList
          legend="Предметы в тайнике"
          name={name}
          choices={choices}
          values={hidden}
          onChange={setHidden}
        />
      ) : (
        <p className="form-field__hint">
          Чтобы спрятать предмет в тайник, добавьте его в «Предметы» этой
          локации.
        </p>
      )}
    </>
  );
}
