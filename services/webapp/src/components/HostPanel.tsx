import { useState } from "react";
import { useApi } from "../api/context";
import type { CombatView, HostAction } from "../api/types";
import { useAction } from "../screens/hooks";
import type { GameView } from "../store/game";
import { Alert, Button, Panel } from "../ui";
import { cannotAct, currentFighter } from "./access";
import "./game.css";
import { RETRY_PATIENCE_MS, useStaysFor } from "./waiting";

/**
 * Ходит монстр, спрятанный монстр или герой, который не может действовать: ждать игрока некого,
 * дальше идёт мастер (так решает и сервер: `waiting_hero`).
 */
function masterMoves(combat: CombatView | null): boolean {
  if (combat === null) return false;
  if (combat.current_id === null) return true;
  const fighter = currentFighter(combat);
  return fighter?.kind === "monster" || cannotAct(fighter);
}

/** Ходит герой, который может действовать: его можно поторопить. Кого нет в порядке хода, того не торопят. */
function heroMoves(combat: CombatView | null): boolean {
  const fighter = currentFighter(combat);
  return fighter?.kind === "pc" && !cannotAct(fighter);
}

export interface HostPanelProps {
  gameId: string;
  view: GameView;
  /** Кто смотрит экран: панель получает только хост. */
  userId: string;
}

interface Controls extends HostPanelProps {
  isResolving: boolean;
}

function HostControls({ gameId, view, isResolving }: Controls) {
  const api = useApi();
  const { busy, error, run } = useAction();
  const [confirming, setConfirming] = useState(false);
  // Сервер не говорит, когда начался разбор: ждём минуту с того, как экран увидел «Мастер думает».
  const waitedLong = useStaysFor(
    isResolving ? "resolving" : null,
    RETRY_PATIENCE_MS,
  );

  const { phase, combat } = view;
  const turn = phase.turn_phase;
  const playing = phase.phase === "playing";
  const paused = phase.phase === "paused";
  const can = {
    collect: playing && turn === "discussion",
    extend: playing && turn === "collecting",
    close: playing && turn === "collecting",
    // «Поторопить» сервер принимает, пока думает герой и отсчёта ещё нет (turns/flow.py::hurry).
    hurry:
      playing &&
      turn === "combat_wait" &&
      heroMoves(combat) &&
      phase.deadline_at === null,
    // Привал идёт по отсчёту сервера: продлить можно только идущий срок, закончить — всегда.
    campExtend: playing && turn === "camp" && phase.deadline_at !== null,
    campEnd: playing && turn === "camp",
    // «Повторить»: сбой мастера, застрявший разбор (через минуту) или встала цепочка ходов
    // монстров в бою, в том числе перед героем, который не может действовать, — всё, что
    // сервер примет (turns/flow.py::retry).
    retry:
      playing &&
      (turn === "dm_failed" ||
        (isResolving && waitedLong) ||
        (turn === "combat_wait" && masterMoves(combat))),
    pause: playing,
    resume: paused,
    // «Перегенерировать» сервер принимает, пока игроки обсуждают следующий ход.
    regenerate:
      playing &&
      (turn === "discussion" || turn === "combat_wait" || turn === "camp"),
  };

  // Лобби, начало игры и конец игры: хосту в этой панели делать нечего.
  if (!Object.values(can).some(Boolean)) return null;

  const press = (action: HostAction) =>
    run(() => api.hostAction(gameId, action));
  const button = (
    action: HostAction,
    label: string,
    variant: "primary" | "secondary" = "secondary",
  ) => (
    <Button variant={variant} busy={busy} onClick={() => void press(action)}>
      {label}
    </Button>
  );

  return (
    <Panel title="Кнопки хоста" icon="shield" className="host-panel">
      <div className="host-panel__stack">
        {error !== null && <Alert>{error}</Alert>}
        <div className="host-panel__buttons">
          {can.collect && button("collect", "Собрать ходы", "primary")}
          {can.extend && button("extend", "+1 мин")}
          {can.close && button("close", "Закрыть сейчас")}
          {can.hurry && button("hurry", "Поторопить", "primary")}
          {can.campExtend && button("camp-extend", "+1 мин")}
          {can.campEnd && button("camp-end", "Закончить привал")}
          {can.retry && button("retry", "Повторить", "primary")}
          {can.resume && button("resume", "Продолжить", "primary")}
          {can.pause && (
            <Button
              variant="secondary"
              busy={busy}
              disabled={isResolving}
              onClick={() => void press("pause")}
            >
              Пауза
            </Button>
          )}
          {can.regenerate && !confirming && (
            <Button
              variant="secondary"
              busy={busy}
              onClick={() => setConfirming(true)}
            >
              Перегенерировать ответ мастера
            </Button>
          )}
        </div>
        {can.pause && isResolving && (
          <p className="host-panel__hint">
            Мастер думает: паузу можно поставить, когда он закончит.
          </p>
        )}
        {can.regenerate && confirming && (
          <div className="host-panel__confirm">
            <p>
              Ответ мастера будет отменён, и он ответит заново с новыми
              бросками. Это снимет черновики и сданные ходы игроков в следующем
              ходе — им придётся написать их заново.
            </p>
            <div className="host-panel__buttons">
              <Button
                variant="danger"
                busy={busy}
                onClick={() => {
                  setConfirming(false);
                  void press("regenerate");
                }}
              >
                Да, перегенерировать
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)}>
                Отмена
              </Button>
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

/**
 * Кнопки хоста: «Собрать ходы», «+1 мин», «Закрыть сейчас», «Поторопить» (в бою), «+1 мин» и
 * «Закончить привал» (на привале), «Пауза»/«Продолжить», «Повторить» и «Перегенерировать ответ
 * мастера». Не-хост панели не получает вовсе; хост видит только то, что сервер примет в этой
 * фазе, и с любой вкладки. Панель начинается заново при каждой смене фазы: предупреждение и
 * ошибка прежней фазы новой не нужны.
 */
export function HostPanel(props: HostPanelProps) {
  const { view, userId } = props;
  if (view.host_user_id !== userId) return null;
  const { phase } = view;
  const isResolving =
    phase.phase === "playing" && phase.turn_phase === "resolving";
  return (
    <HostControls
      key={`${phase.phase}:${phase.turn_phase}`}
      {...props}
      isResolving={isResolving}
    />
  );
}
