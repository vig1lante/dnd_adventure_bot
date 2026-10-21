import type { CreatedCharacter, CreationOptions } from "../../api/types";
import { Alert, Button, Card } from "../../ui";
import "./character.css";

/** Что получают все способы создания героя от экрана выбора способа. */
export interface CharacterFormProps {
  gameId: string;
  options: CreationOptions;
  /** К выбору способа. */
  onBack: () => void;
  /** Герой создан: экран возвращает игрока в лобби. */
  onCreated: () => void;
}

interface CreatedNoticeProps {
  created: CreatedCharacter;
  onDone: () => void;
}

/**
 * Герой сохранён, но описание от мастера не получилось (`flavor_error`): игрок
 * должен прочитать причину, поэтому в лобби его ведёт кнопка, а не сам экран.
 */
export function CreatedNotice({ created, onDone }: CreatedNoticeProps) {
  return (
    <Card title="Герой создан" icon="shield">
      <div className="screen-stack">
        <p>{created.character.name}</p>
        {created.flavor_error !== null && <Alert>{created.flavor_error}</Alert>}
        <div className="screen-actions">
          <Button onClick={onDone}>В лобби</Button>
        </div>
      </div>
    </Card>
  );
}
