import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import type {
  Draft,
  DraftRef,
  DraftSection,
  JournalFields,
  SrdMonster,
} from "../../api/types";
import { Button } from "../../ui";
import { Switch, TextAreaField, TextField } from "../fields";
import { parseNumber } from "./fieldValues";

/** Справочник ссылок черновика; `error` — текст сбоя загрузки, список тогда прежний или пуст. */
export interface DraftRefs {
  list: readonly DraftRef[];
  error: string | null;
}

/** Справочник монстров SRD; `error` — текст сбоя загрузки, список тогда пуст. */
export interface SrdMonsters {
  list: readonly SrdMonster[];
  error: string | null;
}

/** Одна запись раздела: `PUT .../sections/{section}` с `data`. */
export interface SectionWrite {
  section: DraftSection;
  data: unknown;
}

/** Что форма шага получает от редактора. */
export interface StepFormProps {
  draft: Draft;
  /**
   * Ключ данных разделов экрана. Правка начинается заново с черновиком сервера, когда он
   * меняется; после частичной записи (B6 R7) он прежний, и несохранённое остаётся в форме.
   */
  contentKey: string;
  /** Справочник ссылок; грузится только для экранов, где есть выбор ссылок. */
  refs: DraftRefs;
  /** Справочник монстров SRD; грузится, когда его впервые открывает экран боёв. */
  srd: SrdMonsters;
  /** Сохранить раздел шага целиком; `data` — форма раздела по контракту. */
  onSave: (data: unknown) => void;
  /** Записать несколько разделов по очереди, каждый с версией после предыдущей записи. */
  onSaveSections: (writes: SectionWrite[]) => void;
  onLock: (ref: string, locked: boolean) => void;
  /** Панель генерации шага; ей важно, есть ли в форме несохранённые правки. */
  generate: (dirty: boolean) => ReactNode;
  /**
   * «Перегенерировать» одну сущность шага (`target` — `<вид>:<id>`) с пожеланием шага. При
   * несохранённых правках формы (`dirty`) кнопка закрыта: генерация начала бы форму заново.
   */
  regenerate: (target: string, name: string, dirty: boolean) => ReactNode;
  /**
   * Сообщить редактору, есть ли в форме несохранённые правки (её передают в `SectionForm`):
   * запись или генерация другого экрана может начать форму заново, а проверка и публикация
   * видят только сохранённое.
   */
  onDirtyChange: (dirty: boolean) => void;
  /** Предупреждение о несохранённых правках другого экрана того же раздела; `null` — их нет. */
  unsavedElsewhere: string | null;
  /**
   * Сущность, к которой ведёт ссылка находки «Проверки» (только у экрана её шага). Экран, у
   * которого есть состояние вида (выбор локации, фильтр связей), раскрывает её; прокрутку и
   * фокус делает редактор. Новый объект — новый переход, даже к той же сущности.
   */
  focus?: FocusTarget | null;
}

/** Подсказка формы с «Перегенерировать» при несохранённых правках. */
export const SAVE_FIRST_REGENERATE =
  "Сначала сохраните раздел: закрепить и перегенерировать можно только сохранённое.";

interface SectionFormProps {
  onSubmit: () => void;
  /** Можно ли сохранять: поля формы без ошибок ввода. */
  canSave?: boolean;
  /** В форме есть несохранённые правки: закреплять пока нечего. */
  dirty: boolean;
  /** Сообщить редактору о несохранённых правках формы (`StepFormProps.onDirtyChange`). */
  onDirtyChange: (dirty: boolean) => void;
  /** Подсказка при несохранённых правках: что закрыто, пока раздел не сохранён. */
  dirtyHint?: string;
  /** Предупреждение у «Сохранить раздел»: запись заденет чужие несохранённые правки. */
  warning?: string | null;
  /** «Закрепить раздел» рядом с сохранением (у разделов-одиночек). */
  lock?: ReactNode;
  /** Панель генерации шага: идёт сразу после формы. */
  generate: ReactNode;
  children: ReactNode;
}

/** Форма раздела: поля, «Сохранить раздел» и, после формы, генерация шага. */
export function SectionForm({
  onSubmit,
  canSave = true,
  dirty,
  onDirtyChange,
  dirtyHint = "Сначала сохраните раздел: закрепить можно только сохранённое.",
  warning = null,
  lock,
  generate,
  children,
}: SectionFormProps) {
  // Редактор знает о несохранённых правках каждого экрана: о них предупреждают экраны с общими
  // разделами, проверка и публикация. Ушедшая форма правок больше не держит.
  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);
  return (
    <>
      <form
        className="screen-stack"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSave) onSubmit();
        }}
      >
        {children}
        <div className="screen-actions">
          <Button type="submit" disabled={!canSave}>
            Сохранить раздел
          </Button>
          {lock}
        </div>
        {warning !== null && <p className="authoring-warning">{warning}</p>}
        {dirty && <p className="form-field__hint">{dirtyHint}</p>}
      </form>
      {generate}
    </>
  );
}

const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");

interface LinesFieldProps {
  label: string;
  hint?: string;
  value: readonly string[];
  onChange: (value: string[]) => void;
}

/**
 * Список строк в многострочном поле: элемент на строку, пустые строки в список не идут.
 * Набранный текст живёт в самом поле: иначе пустая строка в конце пропадала бы сразу и
 * новую строку нельзя было бы начать.
 */
export function LinesField({ label, hint, value, onChange }: LinesFieldProps) {
  const [text, setText] = useState(() => value.join("\n"));
  return (
    <TextAreaField
      label={label}
      hint={hint}
      rows={3}
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(lines(event.target.value));
      }}
    />
  );
}

interface JournalFieldsEditorProps {
  value: JournalFields;
  onChange: (patch: Partial<JournalFields>) => void;
}

/** Поля дневника отряда у сущности (спека §8): что знают игроки и известно ли с начала. */
export function JournalFieldsEditor({
  value,
  onChange,
}: JournalFieldsEditorProps) {
  return (
    <>
      <TextAreaField
        label="Что знают игроки"
        hint="Без тайн. Пусто — запись в дневнике строится из названия."
        rows={2}
        value={value.public}
        onChange={(event) => onChange({ public: event.target.value })}
      />
      <Switch
        label="Известно с начала"
        hint="Отряд знает об этом с первой сцены."
        checked={value.known_at_start}
        onChange={(checked) => onChange({ known_at_start: checked })}
      />
    </>
  );
}

type NumberFieldProps = { label: string; hint?: string } & (
  | { optional?: false; value: number; onChange: (value: number) => void }
  | {
      /** Пустое поле — `null`: числа нет. */
      optional: true;
      value: number | null;
      onChange: (value: number | null) => void;
    }
);

/**
 * Целое число в поле ввода. Набранный текст живёт в самом поле (как у `LinesField`); пока в нём
 * не целое число, форма получает `NaN` и не сохраняется (`hasBadNumber`). Пределы проверяет сервер.
 */
export function NumberField(props: NumberFieldProps) {
  const { label, hint, value } = props;
  const [text, setText] = useState(() => String(value ?? ""));
  const read = (raw: string) =>
    props.optional && raw.trim() === ""
      ? null
      : (parseNumber(raw, true) ?? Number.NaN);
  return (
    <TextField
      label={label}
      hint={hint}
      inputMode="numeric"
      autoComplete="off"
      value={text}
      error={Number.isNaN(read(text)) ? "Нужно целое число." : null}
      onChange={(event) => {
        setText(event.target.value);
        const next = read(event.target.value);
        if (props.optional) props.onChange(next);
        else props.onChange(next ?? Number.NaN);
      }}
    />
  );
}

interface AlertDialogProps {
  title: string;
  children: ReactNode;
  /** Кнопки диалога. */
  actions: ReactNode;
}

/** Диалог-вопрос внутри экрана: заголовок, пояснение и кнопки ответа. */
export function AlertDialog({ title, children, actions }: AlertDialogProps) {
  const titleId = useId();
  return (
    <div
      role="alertdialog"
      aria-labelledby={titleId}
      className="authoring-dialog"
    >
      <p id={titleId} className="authoring-dialog__title">
        {title}
      </p>
      {children}
      <div className="screen-actions">{actions}</div>
    </div>
  );
}

interface ConflictDialogProps {
  onRefresh: () => void;
  onCancel: () => void;
}

/**
 * «Черновик изменился, обновить?» — ответ на 409 `version_conflict`: записи раздела, замка,
 * пожелания и публикации. «Обновить» перечитывает черновик, «Отмена» оставляет правки.
 */
export function ConflictDialog({ onRefresh, onCancel }: ConflictDialogProps) {
  return (
    <AlertDialog
      title="Черновик изменился, обновить?"
      actions={
        <>
          <Button onClick={onRefresh}>Обновить</Button>
          <Button variant="secondary" onClick={onCancel}>
            Отмена
          </Button>
        </>
      }
    >
      <p className="screen-muted">
        Его успели поменять в другой вкладке или генерацией. Обновление заменит
        несохранённые правки.
      </p>
    </AlertDialog>
  );
}

/** Куда ведёт ссылка находки «Проверки»: экран шага и сущность на нём (`<вид>:<id>/…`). */
export interface FocusTarget {
  ref: string;
}
