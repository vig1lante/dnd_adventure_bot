import { useState } from "react";
import type { RefKind, Relation, RelationKind } from "../../api/types";
import { SelectField, Switch, TextField } from "../fields";
import { EntityList } from "./EntityList";
import { SAVE_FIRST_REGENERATE, SectionForm } from "./fields";
import type { StepFormProps } from "./fields";
import { sameData } from "./fieldValues";
import { ID_HINT, RELATION_KINDS } from "./labels";
import { LockToggle } from "./LockToggle";
import { RefPicker } from "./RefPicker";

/** Концы связи — NPC, фракции, боги и места (спека §8, дополнение B5 §6). */
const ENDS: readonly RefKind[] = ["npc", "faction", "deity", "location"];

const KIND_OPTIONS = (Object.keys(RELATION_KINDS) as RelationKind[]).map(
  (kind) => ({ value: kind, label: RELATION_KINDS[kind] }),
);

const needsNote = (relation: Relation) =>
  relation.kind === "other" && relation.note.trim() === "";

/**
 * Шаг «Связи»: граф «кто — кем — кому» (`a — <вид> для b`), фильтр по сущности, тайные связи.
 * Сохранённую связь можно закрепить и перегенерировать.
 *
 * Фильтр живёт над формой: после записи форма начинается заново с данными сервера, а фильтр
 * остаётся. Он только показывает: скрытые им связи сохраняются как есть. Ссылка находки
 * «Проверки» снимает фильтр: связь, к которой она ведёт, должна быть видна.
 */
export function RelationsStep(props: StepFormProps) {
  const { focus = null } = props;
  const [filter, setFilter] = useState("");
  const [followed, setFollowed] = useState(focus);
  if (focus !== followed) {
    setFollowed(focus);
    if (focus !== null) setFilter("");
  }
  return (
    <RelationsForm
      key={props.contentKey}
      {...props}
      filter={filter}
      onFilter={setFilter}
    />
  );
}

interface RelationsFormProps extends StepFormProps {
  /** Ссылка сущности, чьи связи видны; `""` — все. */
  filter: string;
  onFilter: (ref: string) => void;
}

function RelationsForm({
  draft,
  refs,
  onSave,
  onLock,
  generate,
  regenerate,
  filter,
  onFilter,
  onDirtyChange,
}: RelationsFormProps) {
  const [relations, setRelations] = useState(draft.relations);
  const savedIds = new Set(draft.relations.map((relation) => relation.id));
  const dirty = !sameData(relations, draft.relations);
  const shown = (relation: Relation) =>
    filter === "" || relation.a === filter || relation.b === filter;
  // Связь «другое» без пояснения не даёт сохранить; если её прячет фильтр, причину надо назвать.
  const blocked = relations.filter(needsNote);
  const nameOf = (ref: string) =>
    refs.list.find((entry) => entry.ref === ref)?.name ?? ref;
  const sentence = (relation: Relation, index: number) =>
    relation.a === "" && relation.b === ""
      ? `Связь ${index + 1}`
      : `${nameOf(relation.a) || "…"} — ${RELATION_KINDS[relation.kind]} — ${nameOf(relation.b) || "…"}`;

  return (
    <SectionForm
      onDirtyChange={onDirtyChange}
      onSubmit={() => onSave(relations)}
      canSave={blocked.length === 0}
      dirty={dirty}
      dirtyHint={SAVE_FIRST_REGENERATE}
      generate={generate(dirty)}
    >
      <p className="form-field__hint">
        Связь читается «кто — кем — кому»: «Фабиан — должник — Марта».
        «Перегенерировать» пересоздаёт одну связь с пожеланием из поля «Что хочу
        увидеть».
      </p>
      <RefPicker
        label="Показать связи"
        hint="Только связи выбранной сущности."
        placeholder="все"
        kinds={ENDS}
        refs={refs}
        value={filter}
        onChange={onFilter}
      />
      {!blocked.every(shown) && (
        <p className="form-field__error">
          Есть связь «другое» без пояснения: снимите фильтр, чтобы её найти.
        </p>
      )}
      <EntityList
        legend="Связи"
        items={relations}
        onChange={setRelations}
        // Новая связь начинается с выбранной в фильтре сущности: так она видна сразу.
        create={(): Relation => ({
          id: "",
          a: filter,
          b: "",
          kind: "ally",
          note: "",
          secret: false,
          mutual: true,
        })}
        addLabel="Добавить связь"
        itemLabel={sentence}
        refOf={(relation) => `relation:${relation.id}`}
        visible={shown}
        actions={(relation, label) =>
          savedIds.has(relation.id) && (
            <>
              <LockToggle
                lockRef={`relation:${relation.id}`}
                locks={draft.locks}
                onLock={onLock}
                name={label}
                disabled={dirty}
              />
              {regenerate(`relation:${relation.id}`, label, dirty)}
            </>
          )
        }
      >
        {(relation, update) => (
          <>
            <TextField
              label="Идентификатор"
              hint={ID_HINT}
              autoComplete="off"
              value={relation.id}
              onChange={(event) => update({ id: event.target.value })}
            />
            <RefPicker
              label="Кто"
              placeholder="— выберите —"
              kinds={ENDS}
              refs={refs}
              value={relation.a}
              onChange={(a) => update({ a })}
            />
            <SelectField
              label="Кем"
              options={KIND_OPTIONS}
              value={relation.kind}
              onChange={(kind) => update({ kind: kind as RelationKind })}
            />
            <RefPicker
              label="Кому"
              placeholder="— выберите —"
              kinds={ENDS}
              refs={refs}
              value={relation.b}
              onChange={(b) => update({ b })}
            />
            <TextField
              label="Пояснение"
              hint="Для вида «другое» — обязательно."
              error={
                needsNote(relation)
                  ? "У связи «другое» пояснение обязательно."
                  : null
              }
              value={relation.note}
              onChange={(event) => update({ note: event.target.value })}
            />
            <Switch
              label="Тайная связь"
              hint="Игроки о ней не знают."
              checked={relation.secret}
              onChange={(secret) => update({ secret })}
            />
            <Switch
              label="Взаимная"
              hint="Верна и в обратную сторону."
              checked={relation.mutual}
              onChange={(mutual) => update({ mutual })}
            />
          </>
        )}
      </EntityList>
    </SectionForm>
  );
}
