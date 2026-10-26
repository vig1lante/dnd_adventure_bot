import type { RefKind } from "../../api/types";
import { SelectField } from "../fields";
import { withCurrent } from "./fields";
import type { DraftRefs, Option } from "./fields";
import { REF_KINDS } from "./labels";

interface RefPickerProps {
  label: string;
  hint?: string;
  /** Подпись пустого варианта: «нет», «— выберите —», «все». */
  placeholder: string;
  /** Какие виды сущностей подходят полю (дополнение B5, §6). */
  kinds: readonly RefKind[];
  refs: DraftRefs;
  /** Варианты сверх справочника сервера, со своими подписями (детали локаций для улики). */
  extra?: readonly Option[];
  /** Ссылка `<вид>:<id>`; `""` — не выбрано. */
  value: string;
  onChange: (ref: string) => void;
}

/** Выбор ссылки на сущность черновика из справочника сервера, только допустимых видов. */
export function RefPicker({
  label,
  hint,
  placeholder,
  kinds,
  refs,
  extra = [],
  value,
  onChange,
}: RefPickerProps) {
  // У поля одного вида вид в подписи лишний; у смешанного списка он отличает тёзок.
  const options = [
    ...refs.list
      .filter((entry) => kinds.includes(entry.kind))
      .map((entry) => ({
        value: entry.ref,
        label:
          kinds.length > 1
            ? `${entry.name} (${REF_KINDS[entry.kind]})`
            : entry.name,
      })),
    ...extra,
  ];
  return (
    <SelectField
      label={label}
      hint={hint}
      error={
        refs.error === null
          ? null
          : `Список ссылок не загрузился: ${refs.error}`
      }
      placeholder={placeholder}
      options={withCurrent(options, value)}
      value={value}
      onChange={onChange}
    />
  );
}
