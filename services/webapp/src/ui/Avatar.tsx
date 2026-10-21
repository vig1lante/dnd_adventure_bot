import { classNames } from "./classNames";
import { avatarTone, initials } from "./format";

export interface AvatarProps {
  name: string;
  size?: "sm" | "md" | "lg";
  /** Имя уже написано рядом текстом: жетон — украшение. */
  decorative?: boolean;
}

/** Круглый жетон с инициалами героя; цвет зависит только от имени. */
export function Avatar({ name, size = "md", decorative = false }: AvatarProps) {
  return (
    <span
      className={classNames(
        "ui-avatar",
        `ui-avatar--${size}`,
        `ui-avatar--tone-${avatarTone(name)}`,
      )}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : name}
      aria-hidden={decorative ? true : undefined}
    >
      {initials(name)}
    </span>
  );
}
