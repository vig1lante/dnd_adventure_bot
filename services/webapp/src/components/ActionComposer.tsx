import type { PlayerAction } from "../api/types";
import { TextAreaField, Switch } from "../screens/fields";
import { useGame } from "../store/game";
import type { GameView } from "../store/game";
import { Alert, Badge, Button, Card } from "../ui";
import { composerAccess } from "./access";
import "./game.css";
import { useDraft } from "./useDraft";

// Длина хода: столько принимает сервер (ACTION_MAX_CHARS в turns/api.py).
const ACTION_MAX_CHARS = 3000;

/** Ход уже не черновик: сдан («Готов») или спасован («Пас»), игроком или по таймеру. */
function sealedLabel(action: PlayerAction | null): string | null {
  switch (action?.status) {
    case "submitted":
    case "auto_submitted":
      return "Ход сдан";
    case "pass":
    case "auto_pass":
      return "Вы пасуете";
    default:
      return null;
  }
}

export interface ActionComposerProps {
  gameId: string;
  view: GameView;
}

/**
 * Поле хода внизу вкладки «Рассказ»: «Что делает твой персонаж?», «Тайно», «Вдохновение»
 * (если оно есть) и кнопки «Готов», «Пас», «Изменить». Открыто только когда ход можно писать
 * (см. `composerAccess`), в том числе пока есть связь с игрой: при обрыве оно закрыто с
 * пояснением и открывается само, когда связь вернётся. Черновик сам сохраняется на сервере
 * через 800 мс после ввода.
 */
export function ActionComposer({ gameId, view }: ActionComposerProps) {
  const character = view.character;
  const characterId = character?.id ?? null;
  const connection = useGame((state) => state.connection);
  const access = composerAccess(
    view.phase,
    view.combat,
    characterId,
    connection,
  );
  const hasInspiration = character?.inspiration === true;
  const draft = useDraft({
    gameId,
    saved: view.action,
    hasInspiration,
    // Черновик уходит по REST, сокет ему не нужен: набранное перед самым обрывом не теряется.
    // Закрытым для черновика поле считает только игра (пауза, разбор, чужой ход), не связь.
    open: composerAccess(view.phase, view.combat, characterId).open,
  });
  if (character === null) return null;

  const sealed = sealedLabel(view.action);
  // Сервер правит сданный ход или пас, пока сбор открыт; закрытый им же (auto_*) — нет.
  const canChange =
    access.open &&
    (view.action?.status === "submitted" || view.action?.status === "pass");

  if (sealed !== null && view.action !== null) {
    return (
      <Card title={sealed} icon="scroll" className="composer">
        <div className="composer__stack">
          {view.action.secret && view.action.text !== "" && (
            <p>
              <Badge tone="seal" icon="seal">
                Тайно
              </Badge>
            </p>
          )}
          {view.action.text !== "" && (
            <p className="composer__sealed-text">{view.action.text}</p>
          )}
          {draft.error !== null && <Alert>{draft.error}</Alert>}
          {canChange && (
            <div className="composer__actions">
              <Button
                variant="secondary"
                busy={draft.busy}
                onClick={() => void draft.edit()}
              >
                Изменить
              </Button>
            </div>
          )}
        </div>
      </Card>
    );
  }

  const { values } = draft;
  const locked = !access.open;
  return (
    <Card title="Твой ход" icon="d20" className="composer">
      <div className="composer__stack">
        <TextAreaField
          label="Что делает твой персонаж?"
          hint="Можно надиктовать с клавиатуры"
          value={values.text}
          maxLength={ACTION_MAX_CHARS}
          disabled={locked}
          onChange={(event) => draft.change({ text: event.target.value })}
        />
        <Switch
          label="Тайно"
          hint="Текст хода увидит только мастер."
          checked={values.secret}
          disabled={locked}
          onChange={(secret) => draft.change({ secret })}
        />
        {hasInspiration && (
          <Switch
            label="Вдохновение"
            hint="Потратить вдохновение на этот ход."
            checked={values.inspiration}
            disabled={locked}
            onChange={(inspiration) => draft.change({ inspiration })}
          />
        )}
        {access.note !== null && (
          <p role="status" className="composer__note">
            {access.note}
          </p>
        )}
        {draft.error !== null && <Alert>{draft.error}</Alert>}
        <div className="composer__actions">
          <Button
            busy={draft.busy}
            disabled={locked || values.text.trim() === ""}
            onClick={() => void draft.submit()}
          >
            Готов
          </Button>
          <Button
            variant="secondary"
            busy={draft.busy}
            disabled={locked}
            onClick={() => void draft.pass()}
          >
            Пас
          </Button>
        </div>
      </div>
    </Card>
  );
}
