import { Button } from "../../ui";

interface LockToggleProps {
  /** Что закрепляется: `<вид>:<id>` сущности или `step:<шаг>` раздела целиком. */
  lockRef: string;
  locks: readonly string[];
  onLock: (ref: string, locked: boolean) => void;
  label?: string;
  /** Название сущности: входит в доступное имя кнопки, чтобы различать их в списке. */
  name?: string;
  /** Закрыто, пока в форме есть несохранённые правки: закрепилась бы версия сервера. */
  disabled?: boolean;
}

/** «Закрепить»: закреплённое генерация не меняет. Нажатая кнопка — закреплено. */
export function LockToggle({
  lockRef,
  locks,
  onLock,
  label = "Закрепить",
  name,
  disabled = false,
}: LockToggleProps) {
  const locked = locks.includes(lockRef);
  return (
    <Button
      variant={locked ? "primary" : "secondary"}
      icon="seal"
      aria-pressed={locked}
      disabled={disabled}
      aria-label={name === undefined ? undefined : `${label}: ${name}`}
      onClick={() => onLock(lockRef, !locked)}
    >
      {label}
    </Button>
  );
}
