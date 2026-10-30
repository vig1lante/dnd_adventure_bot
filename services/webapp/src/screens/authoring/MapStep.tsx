import { useState } from "react";
import type { Exit, Location, LocationsSection } from "../../api/types";
import { SelectField, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { JournalFieldsEditor, SectionForm } from "./fields";
import type { StepFormProps } from "./fields";
import { orNull, sameData, withCurrent } from "./fieldValues";
import { DC_OPTIONS, ID_HINT } from "./labels";
import { LockToggle } from "./LockToggle";

// Новая локация — с полями по умолчанию сервера: одна зона «здесь», без связей зон и боя.
const newLocation = (): Location => ({
  id: "",
  name: "",
  region: "",
  read_aloud: "",
  dm_notes: "",
  exits: [],
  features: [],
  secrets: [],
  encounter_id: null,
  zones: [{ id: "here", name: "здесь" }],
  links: [],
  public: "",
  known_at_start: false,
});

const newExit = (): Exit => ({
  id: "",
  to: "",
  direction: "",
  description: "",
  locked_by: null,
  hidden_dc: null,
});

// Сервер принимает старт и финал только из этой же карты.
const OFF_MAP = "Такой локации нет на карте: выберите другую или «не выбран».";

const sectionOf = (draft: StepFormProps["draft"]): LocationsSection => ({
  locations: draft.locations,
  start_location: draft.start_location,
  finale_location: draft.finale_location,
});

/**
 * Шаг «Карта»: локации списком, выходы «откуда → куда», старт и финал. Раздел `locations` —
 * объект `{locations, start_location, finale_location}`; наполнение локаций
 * правит шаг «Наполнение», здесь оно сохраняется как было.
 */
export function MapStep({
  draft,
  onSave,
  onLock,
  generate,
  onDirtyChange,
  unsavedElsewhere,
}: StepFormProps) {
  const [map, setMap] = useState(() => sectionOf(draft));
  // Закрепить можно только локацию, которую знает сервер.
  const savedIds = new Set(draft.locations.map((location) => location.id));
  const dirty = !sameData(map, sectionOf(draft));
  // «Куда», старт и финал выбираются из локаций формы: выход можно провести и в несохранённую.
  const places = map.locations
    .filter((location) => location.id !== "")
    .map((location) => ({
      value: location.id,
      label: location.name || location.id,
    }));
  const offMap = (id: string | null) =>
    id !== null && !places.some((place) => place.value === id);
  const placeName = (id: string) =>
    places.find((place) => place.value === id)?.label ?? id;
  const exitLabel = (exit: Exit, index: number) =>
    exit.direction === "" && exit.to === ""
      ? `Выход ${index + 1}`
      : `${exit.direction || "выход"} → ${placeName(exit.to) || "…"}`;

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(map)}
      canSave={!offMap(map.start_location) && !offMap(map.finale_location)}
      dirty={dirty}
      warning={unsavedElsewhere}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        «Карта» и «Наполнение» правят один раздел: сохраните правки здесь,
        прежде чем наполнять локации.
      </p>
      <EntityList
        legend="Локации"
        items={map.locations}
        onChange={(locations) => setMap({ ...map, locations })}
        create={newLocation}
        addLabel="Добавить локацию"
        itemLabel={(location, index) => location.name || `Локация ${index + 1}`}
        refOf={(location) => `location:${location.id}`}
        actions={(location, label) =>
          savedIds.has(location.id) && (
            <LockToggle
              lockRef={`location:${location.id}`}
              locks={draft.locks}
              onLock={onLock}
              name={label}
              disabled={dirty}
            />
          )
        }
      >
        {(location, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={`${ID_HINT} По нему ведут выходы.`}
              autoComplete="off"
              value={location.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <TextField
              label="Название"
              value={location.name}
              onChange={(event) => update({ name: event.target.value })}
            />
            <TextField
              label="Регион"
              hint="id края с шага «Мир». Можно пусто."
              autoComplete="off"
              value={location.region}
              onChange={(event) => update({ region: event.target.value })}
            />
            <JournalFieldsEditor value={location} onChange={update} />
            <EntityList
              legend="Выходы"
              items={location.exits}
              onChange={(exits) => update({ exits })}
              create={newExit}
              addLabel="Добавить выход"
              itemLabel={exitLabel}
              refOf={(exit) => `location:${location.id}/exit:${exit.id}`}
            >
              {(exit, change) => (
                <>
                  <TextField
                    label="Идентификатор"
                    hint={`${ID_HINT} Уникален внутри локации.`}
                    autoComplete="off"
                    value={exit.id}
                    onChange={(event) => change({ id: event.target.value })}
                  />
                  <SelectField
                    label="Куда"
                    placeholder="— выберите —"
                    options={withCurrent(places, exit.to)}
                    value={exit.to}
                    onChange={(to) => change({ to })}
                  />
                  <TextField
                    label="Направление"
                    hint="Север, вниз, в дверь…"
                    value={exit.direction}
                    onChange={(event) =>
                      change({ direction: event.target.value })
                    }
                  />
                  <TextField
                    label="Описание"
                    value={exit.description}
                    onChange={(event) =>
                      change({ description: event.target.value })
                    }
                  />
                  <TextField
                    label="Замок"
                    hint="item:<id ключа> или flag:<имя>. Пусто — не заперт."
                    autoComplete="off"
                    value={exit.locked_by ?? ""}
                    onChange={(event) =>
                      change({ locked_by: orNull(event.target.value) })
                    }
                  />
                  <SelectField
                    label="Скрытность"
                    hint="Скрытый выход ищут Внимательностью или Анализом; Сл — из таблицы."
                    placeholder="не скрыт"
                    options={DC_OPTIONS}
                    value={
                      exit.hidden_dc === null ? "" : String(exit.hidden_dc)
                    }
                    onChange={(dc) =>
                      change({ hidden_dc: dc === "" ? null : Number(dc) })
                    }
                  />
                </>
              )}
            </EntityList>
          </>
        )}
      </EntityList>
      <SelectField
        label="Старт"
        hint="Где отряд начинает игру."
        error={offMap(map.start_location) ? OFF_MAP : null}
        placeholder="не выбран"
        options={withCurrent(places, map.start_location ?? "")}
        value={map.start_location ?? ""}
        onChange={(id) => setMap({ ...map, start_location: orNull(id) })}
      />
      <SelectField
        label="Финал"
        hint="Где развязка приключения."
        error={offMap(map.finale_location) ? OFF_MAP : null}
        placeholder="не выбран"
        options={withCurrent(places, map.finale_location ?? "")}
        value={map.finale_location ?? ""}
        onChange={(id) => setMap({ ...map, finale_location: orNull(id) })}
      />
    </SectionForm>
  );
}
