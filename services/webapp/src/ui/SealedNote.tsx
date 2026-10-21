import type { ReactNode } from "react";
import { Icon } from "./icons";

export interface SealedNoteProps {
  children: ReactNode;
}

/** Личное сообщение мастера: запечатанное письмо «Только тебе». */
export function SealedNote({ children }: SealedNoteProps) {
  return (
    <div role="note" aria-label="Только тебе" className="ui-sealed">
      <span className="ui-sealed__seal" aria-hidden="true">
        <Icon name="seal" size={26} />
      </span>
      <p className="ui-sealed__label">Только тебе</p>
      <div className="ui-sealed__body">{children}</div>
    </div>
  );
}
