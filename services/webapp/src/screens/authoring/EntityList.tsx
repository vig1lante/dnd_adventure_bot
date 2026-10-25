import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { Button } from "../../ui";
import "./authoring.css";

interface EntityListProps<T extends object> {
  legend: string;
  items: readonly T[];
  onChange: (items: T[]) => void;
  /** Пустая сущность для «Добавить». */
  create: () => T;
  addLabel: string;
  /** Подпись сущности: заголовок её группы полей и часть имён её кнопок. */
  itemLabel: (item: T, index: number) => string;
  /** Кнопки сущности рядом с «Удалить» (например, «Закрепить»). */
  actions?: (item: T, label: string) => ReactNode;
  /** Поля сущности; `update` меняет только переданные поля. */
  children: (item: T, update: (patch: Partial<T>) => void) => ReactNode;
}

/** Список сущностей раздела: группа полей на каждую, «Добавить» и «Удалить». */
export function EntityList<T extends object>({
  legend,
  items,
  onChange,
  create,
  addLabel,
  itemLabel,
  actions,
  children,
}: EntityListProps<T>) {
  // Ключи строк живут здесь: у новой сущности id ещё нет, а без своего ключа после удаления
  // соседняя строка унаследовала бы чужое состояние полей.
  const nextKey = useRef(items.length);
  const [keys, setKeys] = useState(() => items.map((_, index) => index));

  const update = (index: number, patch: Partial<T>) =>
    onChange(
      items.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  const remove = (index: number) => {
    setKeys(keys.filter((_, i) => i !== index));
    onChange(items.filter((_, i) => i !== index));
  };
  const add = () => {
    setKeys([...keys, nextKey.current++]);
    onChange([...items, create()]);
  };

  return (
    <fieldset className="form-group">
      <legend className="form-legend">{legend}</legend>
      {items.length === 0 && <p className="screen-muted">Пока пусто.</p>}
      {items.length > 0 && (
        <ul className="authoring-entities">
          {items.map((item, index) => {
            const label = itemLabel(item, index);
            return (
              <li key={keys[index]}>
                <fieldset className="authoring-entity">
                  <legend className="authoring-entity__title">{label}</legend>
                  <div className="authoring-entity__actions">
                    {actions?.(item, label)}
                    <Button
                      variant="danger"
                      aria-label={`Удалить: ${label}`}
                      onClick={() => remove(index)}
                    >
                      Удалить
                    </Button>
                  </div>
                  {children(item, (patch) => update(index, patch))}
                </fieldset>
              </li>
            );
          })}
        </ul>
      )}
      <div>
        <Button variant="secondary" onClick={add}>
          {addLabel}
        </Button>
      </div>
    </fieldset>
  );
}
