import { useState } from "react";
import type { ReactNode } from "react";
import type { Draft, JournalFields } from "../../api/types";
import { Button } from "../../ui";
import { Switch, TextAreaField } from "../fields";

/** Что форма шага получает от редактора. */
export interface StepFormProps {
  draft: Draft;
  /** Сохранить раздел целиком; `data` — форма раздела по контракту. */
  onSave: (data: unknown) => void;
  onLock: (ref: string, locked: boolean) => void;
  /** Панель генерации шага; ей важно, есть ли в форме несохранённые правки. */
  generate: (dirty: boolean) => ReactNode;
}

// Ключи объектов по порядку: сервер и форма могут перечислять одни и те же поля по-разному.
const sortedKeys = (_key: string, value: unknown) =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(
        Object.entries(value).sort(([a], [b]) => (a < b ? -1 : 1)),
      )
    : value;

/** Одинаковы ли данные раздела по содержимому (порядок полей объектов не важен). */
export const sameData = (a: unknown, b: unknown) =>
  JSON.stringify(a, sortedKeys) === JSON.stringify(b, sortedKeys);

interface SectionFormProps {
  onSubmit: () => void;
  /** Можно ли сохранять: поля формы без ошибок ввода. */
  canSave?: boolean;
  /** В форме есть несохранённые правки: закреплять пока нечего. */
  dirty: boolean;
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
  lock,
  generate,
  children,
}: SectionFormProps) {
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
        {dirty && (
          <p className="form-field__hint">
            Сначала сохраните раздел: закрепить можно только сохранённое.
          </p>
        )}
      </form>
      {generate}
    </>
  );
}

/** Необязательная ссылка или id: пустое поле — `null`, как ждёт сервер. */
export const orNull = (text: string): string | null =>
  text.trim() === "" ? null : text;

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
