import type {
  Feature,
  Item,
  Location,
  LocationSecret,
  Zone,
} from "../../api/types";
import { TextAreaField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { FeatureEditor } from "./FeatureEditor";
import { JournalFieldsEditor } from "./fields";
import type { DraftRefs } from "./fields";
import { orNull } from "./fieldValues";
import { RefPicker } from "./RefPicker";
import { SecretEditor } from "./SecretEditor";
import { ID_HINT } from "./labels";

const ENCOUNTER = "encounter:";

const newFeature = (): Feature => ({
  id: "",
  name: "",
  description: "",
  check: null,
  reveals: [],
  hidden: false,
  locked_by: null,
});

const newSecret = (): LocationSecret => ({
  id: "",
  text: "",
  check: null,
  reveals: [],
});

/** id предметов, которые прячут детали локации, кроме детали `except`. */
const hiddenIn = (features: readonly Feature[], except: Feature) =>
  new Set(
    features
      .filter((feature) => feature !== except)
      .flatMap((feature) => feature.reveals)
      .filter((reveal) => reveal.kind === "item")
      .map((reveal) => reveal.id),
  );

interface LocationEditorProps {
  location: Location;
  /** Все предметы черновика: здесь видны и правятся только лежащие в этой локации. */
  items: readonly Item[];
  refs: DraftRefs;
  onChange: (location: Location) => void;
  onItemsChange: (items: Item[]) => void;
}

/**
 * Наполнение одной локации: текст вслух, заметки мастера, зоны, детали и тайники, секреты,
 * предметы «лежит здесь» (держатель — эта локация) и бой.
 */
export function LocationEditor({
  location,
  items,
  refs,
  onChange,
  onItemsChange,
}: LocationEditorProps) {
  const update = (patch: Partial<Location>) =>
    onChange({ ...location, ...patch });
  const holder = `location:${location.id}`;
  const here = items.filter((item) => item.holder === holder);
  const placeOf = (item: Item) =>
    location.features.find((feature) =>
      feature.reveals.some(
        (reveal) => reveal.kind === "item" && reveal.id === item.id,
      ),
    );

  return (
    <>
      <TextAreaField
        label="Текст вслух"
        hint="Без тайн: мастер читает его игрокам."
        rows={4}
        value={location.read_aloud}
        onChange={(event) => update({ read_aloud: event.target.value })}
      />
      <TextAreaField
        label="Заметки мастера"
        hint="Знает только мастер."
        rows={3}
        value={location.dm_notes}
        onChange={(event) => update({ dm_notes: event.target.value })}
      />
      <EntityList<Zone>
        legend="Зоны"
        items={location.zones}
        onChange={(zones) => update({ zones })}
        create={() => ({ id: "", name: "" })}
        addLabel="Добавить зону"
        itemLabel={(zone, index) => zone.name || `Зона ${index + 1}`}
        refOf={(zone) => `location:${location.id}/zone:${zone.id}`}
      >
        {(zone, change) => (
          <>
            <TextField
              label="Идентификатор"
              autoComplete="off"
              value={zone.id}
              onChange={(event) => change({ id: event.target.value })}
            />
            <TextField
              label="Название"
              value={zone.name}
              onChange={(event) => change({ name: event.target.value })}
            />
          </>
        )}
      </EntityList>
      <EntityList
        legend="Детали и тайники"
        items={location.features}
        onChange={(features) => update({ features })}
        create={newFeature}
        addLabel="Добавить деталь"
        itemLabel={(feature, index) => feature.name || `Деталь ${index + 1}`}
        refOf={(feature) => `location:${location.id}/feature:${feature.id}`}
      >
        {(feature, change) => (
          <FeatureEditor
            feature={feature}
            update={change}
            items={here.filter((item) => item.id !== "")}
            hiddenElsewhere={hiddenIn(location.features, feature)}
          />
        )}
      </EntityList>
      <EntityList
        legend="Секреты"
        items={location.secrets}
        onChange={(secrets) => update({ secrets })}
        create={newSecret}
        addLabel="Добавить секрет"
        itemLabel={(_secret, index) => `Секрет ${index + 1}`}
        refOf={(secret) => `location:${location.id}/secret:${secret.id}`}
      >
        {(secret, change) => <SecretEditor secret={secret} update={change} />}
      </EntityList>
      <EntityList
        legend="Предметы"
        items={items}
        onChange={onItemsChange}
        create={() => ({
          id: "",
          name: "",
          description: "",
          base_item: null,
          holder,
          public: "",
          known_at_start: false,
        })}
        addLabel="Добавить предмет"
        // Номер — среди предметов этой локации: общий список черновика автору не виден.
        itemLabel={(item) => item.name || `Предмет ${here.indexOf(item) + 1}`}
        visible={(item) => item.holder === holder}
      >
        {(item, change) => {
          const place = placeOf(item);
          return (
            <>
              <TextField
                label="Идентификатор"
                hint={ID_HINT}
                autoComplete="off"
                value={item.id}
                onChange={(event) => change({ id: event.target.value })}
              />
              <TextField
                label="Название"
                value={item.name}
                onChange={(event) => change({ name: event.target.value })}
              />
              <TextAreaField
                label="Описание"
                rows={2}
                value={item.description}
                onChange={(event) =>
                  change({ description: event.target.value })
                }
              />
              <TextField
                label="Предмет SRD"
                hint="id обычного предмета SRD, если это он. Можно пусто."
                autoComplete="off"
                value={item.base_item ?? ""}
                onChange={(event) =>
                  change({ base_item: orNull(event.target.value) })
                }
              />
              <p className="form-field__hint">
                {place === undefined
                  ? "Лежит открыто"
                  : `В тайнике «${place.name || place.id}»`}
                . Спрятать в тайник или достать — отметкой у детали выше.
              </p>
              <JournalFieldsEditor value={item} onChange={change} />
            </>
          );
        }}
      </EntityList>
      <RefPicker
        label="Бой"
        hint="Бой локации из боёв черновика (их создают на шаге «Бои и боссы»)."
        placeholder="нет"
        kinds={["encounter"]}
        refs={refs}
        value={
          location.encounter_id === null
            ? ""
            : `${ENCOUNTER}${location.encounter_id}`
        }
        onChange={(ref) =>
          update({ encounter_id: orNull(ref.slice(ENCOUNTER.length)) })
        }
      />
    </>
  );
}
