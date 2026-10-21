import { useId } from "react";
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { Alert } from "../ui";
import { classNames } from "../ui/classNames";
import "./screens.css";

// Поля форм строятся из токенов (screens.css), как и всё остальное: своих цветов нет.

/** Идентификаторы поля, его подсказки и ошибки; `describedBy` читается вслух вместе с полем. */
function useFieldIds(
  hint: string | undefined,
  error: string | null | undefined,
) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const described = [hint !== undefined ? hintId : null, error ? errorId : null]
    .filter((value) => value !== null)
    .join(" ");
  return { id, hintId, errorId, describedBy: described || undefined };
}

interface FieldNotesProps {
  hint?: string;
  hintId: string;
  error?: string | null;
  errorId: string;
}

/** Подсказка и ошибка под полем. */
function FieldNotes({ hint, hintId, error, errorId }: FieldNotesProps) {
  return (
    <>
      {hint !== undefined && (
        <p id={hintId} className="form-field__hint">
          {hint}
        </p>
      )}
      {error ? (
        <p id={errorId} role="alert" className="form-field__error">
          {error}
        </p>
      ) : null}
    </>
  );
}

interface TextFieldProps extends Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "id" | "className"
> {
  label: string;
  hint?: string;
  /** Текст ошибки: поле помечается недопустимым, текст читается вслух. */
  error?: string | null;
}

/** Текстовое поле с подписью, подсказкой и ошибкой; всё связано с самим полем. */
export function TextField({ label, hint, error, ...input }: TextFieldProps) {
  const { id, describedBy, ...ids } = useFieldIds(hint, error);
  return (
    <div className="form-field">
      <label htmlFor={id} className="form-field__label">
        {label}
      </label>
      <input
        {...input}
        id={id}
        className="form-field__input"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      />
      <FieldNotes hint={hint} error={error} {...ids} />
    </div>
  );
}

interface TextAreaFieldProps extends Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  "id" | "className"
> {
  label: string;
  hint?: string;
  error?: string | null;
}

/** Многострочное поле: те же подпись, подсказка и ошибка, что у `TextField`. */
export function TextAreaField({
  label,
  hint,
  error,
  rows = 4,
  ...input
}: TextAreaFieldProps) {
  const { id, describedBy, ...ids } = useFieldIds(hint, error);
  return (
    <div className="form-field">
      <label htmlFor={id} className="form-field__label">
        {label}
      </label>
      <textarea
        {...input}
        id={id}
        rows={rows}
        className="form-field__input form-field__input--area"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      />
      <FieldNotes hint={hint} error={error} {...ids} />
    </div>
  );
}

interface SelectFieldProps extends Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  "id" | "className" | "value" | "onChange" | "children"
> {
  label: string;
  hint?: string;
  error?: string | null;
  options: readonly { value: string; label: string }[];
  /** Подпись пустого варианта (значение `""`); без неё пустого варианта нет. */
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
}

/** Выпадающий список с подписью; пустое значение — `""`. */
export function SelectField({
  label,
  hint,
  error,
  options,
  placeholder,
  value,
  onChange,
  ...select
}: SelectFieldProps) {
  const { id, describedBy, ...ids } = useFieldIds(hint, error);
  return (
    <div className="form-field">
      <label htmlFor={id} className="form-field__label">
        {label}
      </label>
      <select
        {...select}
        id={id}
        className="form-field__input"
        value={value}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
      >
        {placeholder !== undefined && <option value="">{placeholder}</option>}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <FieldNotes hint={hint} error={error} {...ids} />
    </div>
  );
}

export interface Choice {
  value: string;
  label: string;
  /** Пояснение под названием; входит в доступное имя варианта. */
  description?: ReactNode;
  disabled?: boolean;
}

interface ChoiceListProps {
  legend: string;
  name: string;
  choices: readonly Choice[];
  value: string | null;
  onChange: (value: string) => void;
}

function ChoiceBody({ choice }: { choice: Choice }) {
  return (
    <span className="form-choice__body">
      <span className="form-choice__title">{choice.label}</span>
      {choice.description !== undefined && (
        <span className="form-choice__text">{choice.description}</span>
      )}
    </span>
  );
}

/** Выбор одного из вариантов: радиокнопки в виде карточек; недоступные варианты не выбрать. */
export function ChoiceList({
  legend,
  name,
  choices,
  value,
  onChange,
}: ChoiceListProps) {
  return (
    <fieldset className="form-choices" role="radiogroup">
      <legend className="form-legend">{legend}</legend>
      {choices.map((choice) => (
        <label
          key={choice.value}
          className={classNames(
            "form-choice",
            choice.disabled && "form-choice--disabled",
          )}
        >
          <input
            type="radio"
            name={name}
            value={choice.value}
            checked={value === choice.value}
            disabled={choice.disabled}
            onChange={() => {
              // Браузер недоступному варианту событий не шлёт; защита от программных щелчков.
              if (!choice.disabled) onChange(choice.value);
            }}
          />
          <ChoiceBody choice={choice} />
        </label>
      ))}
    </fieldset>
  );
}

interface CheckListProps {
  legend: string;
  name: string;
  choices: readonly Choice[];
  values: readonly string[];
  /** Сколько вариантов можно отметить; когда набрано, остальные становятся неактивными. */
  max?: number;
  /** Отмеченные значения в порядке списка вариантов. */
  onChange: (values: string[]) => void;
}

/** Выбор нескольких вариантов: флажки в виде карточек, с пределом числа отметок. */
export function CheckList({
  legend,
  name,
  choices,
  values,
  max,
  onChange,
}: CheckListProps) {
  const chosen = new Set(values);
  const full = max !== undefined && chosen.size >= max;

  const toggle = (value: string, checked: boolean) => {
    const next = new Set(chosen);
    if (checked) next.add(value);
    else next.delete(value);
    onChange(
      choices.filter((choice) => next.has(choice.value)).map((c) => c.value),
    );
  };

  return (
    <fieldset className="form-choices">
      <legend className="form-legend">{legend}</legend>
      {choices.map((choice) => {
        const checked = chosen.has(choice.value);
        const disabled = choice.disabled === true || (!checked && full);
        return (
          <label
            key={choice.value}
            className={classNames(
              "form-choice",
              disabled && "form-choice--disabled",
            )}
          >
            <input
              type="checkbox"
              name={name}
              value={choice.value}
              checked={checked}
              disabled={disabled}
              onChange={(event) => {
                // Как у ChoiceList: недоступный вариант не отмечается и программным щелчком.
                if (!disabled) toggle(choice.value, event.target.checked);
              }}
            />
            <ChoiceBody choice={choice} />
          </label>
        );
      })}
    </fieldset>
  );
}

interface SwitchProps {
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}

/** Переключатель «включено / выключено» с подписью и подсказкой. */
export function Switch({
  label,
  hint,
  checked,
  disabled = false,
  onChange,
}: SwitchProps) {
  const hintId = useId();
  return (
    <div className="form-switch">
      <label className="form-switch__row">
        <input
          type="checkbox"
          role="switch"
          className="form-switch__input"
          checked={checked}
          disabled={disabled}
          aria-describedby={hint !== undefined ? hintId : undefined}
          onChange={(event) => {
            if (!disabled) onChange(event.target.checked);
          }}
        />
        <span className="form-switch__label">{label}</span>
      </label>
      {hint !== undefined && (
        <p id={hintId} className="form-field__hint">
          {hint}
        </p>
      )}
    </div>
  );
}

interface ErrorAlertProps {
  error: string | null;
  /** Подробности отказа правил (422): каждая строка — отдельная проблема. */
  problems: readonly string[];
}

/** Отказ сервера: текст и, если сервер перечислил проблемы правил, список. */
export function ErrorAlert({ error, problems }: ErrorAlertProps) {
  if (error === null) return null;
  return (
    <Alert>
      <p>{error}</p>
      {problems.length > 0 && (
        <ul className="form-problems">
          {problems.map((problem, index) => (
            // Сервер может прислать одинаковые строки: номер делает ключ уникальным.
            <li key={`${index}:${problem}`}>{problem}</li>
          ))}
        </ul>
      )}
    </Alert>
  );
}
