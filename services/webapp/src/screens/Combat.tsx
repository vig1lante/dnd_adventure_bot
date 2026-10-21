import { ActionComposer } from "../components/ActionComposer";
import { composerAccess, isHeroTurn } from "../components/access";
import { InitiativeList } from "../components/InitiativeList";
import { ZoneView } from "../components/ZoneView";
import type { GameView } from "../store/game";
import { Card } from "../ui";
import "../components/game.css";

export interface CombatProps {
  gameId: string;
  view: GameView;
  /** Сколько ходов началось на глазах клиента: новый ход начинает поле хода с чистого листа. */
  turn: number;
}

/**
 * Экран боя: кто ходит, порядок хода и зоны. Поле хода есть только у игрока, чей герой ходит
 * сейчас и может действовать (`isHeroTurn`); остальные видят строку «Ходит Лира.» или «Лира не
 * может действовать.» — какое поле открыто и что сказать вместо него, решает `composerAccess`,
 * как и на вкладке «Рассказ». Клиент ничего не считает: порядок, раунд, здоровье и зоны
 * приходят от сервера как есть.
 */
export function Combat({ gameId, view, turn }: CombatProps) {
  const { combat, character } = view;
  if (combat === null) return null;

  const note = composerAccess(view.phase, combat, character?.id ?? null).note;
  return (
    <>
      {isHeroTurn(combat, character?.id ?? null) ? (
        <ActionComposer key={turn} gameId={gameId} view={view} />
      ) : (
        note !== null && (
          <p role="status" className="combat-turn">
            {note}
          </p>
        )
      )}
      <Card title={`Бой, раунд ${combat.round}`} icon="d20">
        <InitiativeList combat={combat} />
      </Card>
      <Card title="Поле боя" icon="shield">
        <ZoneView combat={combat} />
      </Card>
    </>
  );
}
