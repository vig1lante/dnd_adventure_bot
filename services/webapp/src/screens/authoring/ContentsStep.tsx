import { useState } from "react";
import type { LocationsSection } from "../../api/types";
import { SelectField } from "../fields";
import { SAVE_FIRST_REGENERATE, SectionForm, sameData } from "./fields";
import type { SectionWrite, StepFormProps } from "./fields";
import { LocationEditor } from "./LocationEditor";
import { LockToggle } from "./LockToggle";

/**
 * Шаг «Наполнение»: локации карты по одной. Правит раздел `locations` (детали, тайники, секреты)
 * и раздел `items` (предметы этой локации). Предмет в тайнике — две записи: сначала `items`
 * (держатель), потом `locations` (деталь с `reveals: item`), каждая с текущей версией (B5 §7).
 *
 * Выбор локации живёт над формой: после записи форма начинается заново с данными сервера, а
 * открытой остаётся та же локация. Локации здесь не добавляются, не удаляются и не
 * переименовываются (это «Карта»), а id сохранённых локаций не повторяются: выбор — по id.
 * Ссылка находки «Проверки» (`location:<id>/…`) открывает локацию своей сущности.
 */
export function ContentsStep(props: StepFormProps) {
  const { draft, focus = null } = props;
  const [selected, setSelected] = useState(draft.locations[0]?.id ?? "");
  // Новый переход по ссылке выбирает локацию в том же рендере: редактор ищет сущность сразу
  // после него.
  const [followed, setFollowed] = useState(focus);
  if (focus !== followed) {
    setFollowed(focus);
    const [kind, id] = focus?.ref.split("/")[0].split(":") ?? [];
    if (kind === "location" && draft.locations.some((item) => item.id === id)) {
      setSelected(id);
    }
  }
  return (
    <ContentsForm
      key={props.contentKey}
      {...props}
      selected={selected}
      onSelect={setSelected}
    />
  );
}

interface ContentsFormProps extends StepFormProps {
  /** id открытой локации; нет такой — открыта первая. */
  selected: string;
  onSelect: (id: string) => void;
}

function ContentsForm({
  draft,
  refs,
  onSaveSections,
  onLock,
  generate,
  regenerate,
  onDirtyChange,
  unsavedElsewhere,
  selected,
  onSelect,
}: ContentsFormProps) {
  const [locations, setLocations] = useState(draft.locations);
  const [items, setItems] = useState(draft.items);
  const itemsDirty = !sameData(items, draft.items);
  const locationsDirty = !sameData(locations, draft.locations);
  const dirty = itemsDirty || locationsDirty;

  if (locations.length === 0) {
    return (
      <>
        <p className="screen-muted">
          Сначала добавьте локации на шаге «Карта» и сохраните их — потом их
          можно наполнить.
        </p>
        {generate(false)}
      </>
    );
  }

  const location =
    locations.find((item) => item.id === selected) ?? locations[0];
  // Старт и финал уходят здесь такими, какие сохранены, а правятся только на «Карте».
  const endsOffMap = [draft.start_location, draft.finale_location].some(
    (id) => id !== null && !draft.locations.some((item) => item.id === id),
  );
  const label = location.name || location.id;
  const save = () => {
    const writes: SectionWrite[] = [];
    if (itemsDirty) writes.push({ section: "items", data: items });
    if (locationsDirty || writes.length === 0) {
      // Старт и финал правит «Карта»: здесь они уходят такими, какие сохранены.
      const data: LocationsSection = {
        locations,
        start_location: draft.start_location,
        finale_location: draft.finale_location,
      };
      writes.push({ section: "locations", data });
    }
    onSaveSections(writes);
  };

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={save}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      warning={unsavedElsewhere}
      generate={generate(dirty)}
    >
      {endsOffMap && (
        <p className="authoring-warning">
          Старт или финал не на карте: исправьте их на шаге «Карта», иначе
          раздел не сохранится.
        </p>
      )}
      <p className="form-field__hint">
        «Перегенерировать» пересоздаёт наполнение одной локации с пожеланием из
        поля «Что хочу увидеть». «Сгенерировать шаг» наполнит все незакреплённые
        локации: закрепите те, что менять не нужно. «Карта» и «Наполнение»
        правят один раздел — сохраните правки на одном экране, прежде чем
        перейти к другому.
      </p>
      <SelectField
        label="Локация"
        options={locations.map((item) => ({
          value: item.id,
          label: item.name || item.id,
        }))}
        value={location.id}
        onChange={onSelect}
      />
      <div className="authoring-entity__actions">
        <LockToggle
          lockRef={`location:${location.id}`}
          locks={draft.locks}
          onLock={onLock}
          name={label}
          disabled={dirty}
        />
        {regenerate(`location:${location.id}`, label, dirty)}
      </div>
      <LocationEditor
        key={location.id}
        location={location}
        items={items}
        refs={refs}
        onChange={(next) =>
          setLocations(
            locations.map((item) => (item === location ? next : item)),
          )
        }
        onItemsChange={setItems}
      />
    </SectionForm>
  );
}
