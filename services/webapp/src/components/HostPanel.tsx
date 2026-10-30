import { useState } from "react";
import { useApi } from "../api/context";
import type {
  CombatView,
  GameSummary,
  HostAction,
  RestKind,
} from "../api/types";
import { useAction } from "../screens/hooks";
import { useGame } from "../store/game";
import type { GameView } from "../store/game";
import { Alert, Button, Panel } from "../ui";
import { cannotAct, currentFighter } from "./access";
import "./game.css";
import { restLabel } from "./phases";
import { RETRY_PATIENCE_MS, phaseStartedAt, useStaysFor } from "./waiting";

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

/** Срок для подписи кнопки: целые минуты — минутами, иначе секундами. */
function durationText(seconds: number): string {
  return seconds % 60 === 0 ? `${seconds / 60} мин` : `${seconds} сек`;
}

/** «Короткий привал (2 мин)»: срок — из настроек игры. */
function restButtonLabel(kind: RestKind, game: GameSummary): string {
  const seconds =
    kind === "short" ? game.short_rest_seconds : game.camp_seconds;
  return `${restLabel(kind)} (${durationText(seconds)})`;
}

// Привал по кнопке превращает открытый раунд в ход «конец привала»: черновики игроков мастер
// не увидит, поэтому хост узнаёт об этом до того, как согласится.
const REST_DRAFTS_WARNING =
  "Черновики ходов этого раунда до мастера не дойдут — после привала игрокам придётся написать их заново.";

const REST_QUESTIONS: Record<RestKind, string> = {
  short: `Короткий привал: герои переводят дух около часа, тратят кости хитов и говорят у костра. В конце мастер коротко расскажет, что изменилось. ${REST_DRAFTS_WARNING}`,
  long: `Долгий привал: отряд ночует у костра. Утром герои полностью отдохнут, а мастер расскажет, каким выдалось утро. ${REST_DRAFTS_WARNING}`,
};

const REGENERATE_QUESTION =
  "Ответ мастера будет отменён, и он ответит заново с новыми бросками. Это снимет черновики и сданные ходы игроков в следующем ходе — им придётся написать их заново.";

/** Действие, перед которым панель переспрашивает хоста: его не отменить. */
type Question = "regenerate" | RestKind;

interface ConfirmProps {
  label: string;
  text: string;
  yes: string;
  variant: "primary" | "danger";
  busy: boolean;
  onYes: () => void;
  onCancel: () => void;
}

/** Вопрос перед действием хоста: пояснение, «Да, …» и «Отмена». */
function Confirm({
  label,
  text,
  yes,
  variant,
  busy,
  onYes,
  onCancel,
}: ConfirmProps) {
  return (
    <div role="group" aria-label={label} className="host-panel__confirm">
      <p>{text}</p>
      <div className="host-panel__buttons">
        <Button variant={variant} busy={busy} onClick={onYes}>
          {yes}
        </Button>
        <Button variant="secondary" onClick={onCancel}>
          Отмена
        </Button>
      </div>
    </div>
  );
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
  const [asking, setAsking] = useState<Question | null>(null);
  const clockOffsetMs = useGame((state) => state.live.clockOffsetMs);
  const { phase, combat, game } = view;
  // Минута до «Повторить» идёт от серверного начала разбора: перезагрузка страницы её не
  // перезапускает. Сервер его не знает (игра началась до обновления сервера) — минута с показа
  // «Мастер думает».
  const waitedLong = useStaysFor(
    isResolving ? `resolving@${phase.phase_since ?? ""}` : null,
    RETRY_PATIENCE_MS,
    phaseStartedAt(phase.phase_since, clockOffsetMs),
  );

  const turn = phase.turn_phase;
  const playing = phase.phase === "playing";
  const paused = phase.phase === "paused";
  const can = {
    collect: playing && turn === "discussion",
    // Привал по кнопке сервер начинает только из обсуждения и вне боя.
    rest: playing && turn === "discussion" && combat === null,
    extend: playing && turn === "collecting",
    close: playing && turn === "collecting",
    // «Поторопить» сервер принимает, пока думает герой и отсчёта ещё нет (turns/flow.py::hurry).
    hurry:
      playing &&
      turn === "combat_wait" &&
      heroMoves(combat) &&
      phase.deadline_at === null,
    // Привал любого вида идёт по отсчёту сервера: продлить можно только идущий срок,
    // закончить — всегда.
    restExtend: playing && turn === "camp" && phase.deadline_at !== null,
    restEnd: playing && turn === "camp",
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
    // «Перегенерировать» — по признаку сервера: привал по кнопке хоста перегенерировать нельзя,
    // привал мастера — можно. Старый сервер признака не шлёт — кнопки нет.
    regenerate: playing && phase.can_regenerate === true,
  };

  // Лобби, начало игры и конец игры: хосту в этой панели делать нечего.
  if (!Object.values(can).some(Boolean)) return null;

  const press = (action: HostAction) =>
    run(() => api.hostAction(gameId, action));
  const startRest = (kind: RestKind) => run(() => api.startRest(gameId, kind));
  // Вопрос открыт, пока его действие ещё доступно в этой фазе.
  const question =
    asking === null || !(asking === "regenerate" ? can.regenerate : can.rest)
      ? null
      : asking;
  const askButton = (topic: Question, label: string) =>
    question === null && (
      <Button variant="secondary" busy={busy} onClick={() => setAsking(topic)}>
        {label}
      </Button>
    );
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
          {can.rest && askButton("short", restButtonLabel("short", game))}
          {can.rest && askButton("long", restButtonLabel("long", game))}
          {can.extend && button("extend", "+1 мин")}
          {can.close && button("close", "Закрыть сейчас")}
          {can.hurry && button("hurry", "Поторопить", "primary")}
          {can.restExtend && button("rest-extend", "+1 мин")}
          {can.restEnd && button("rest-end", "Закончить привал")}
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
          {can.regenerate &&
            askButton("regenerate", "Перегенерировать ответ мастера")}
        </div>
        {can.pause && isResolving && (
          <p className="host-panel__hint">
            Мастер думает: паузу можно поставить, когда он закончит.
          </p>
        )}
        {question === "regenerate" && (
          <Confirm
            label="Перегенерировать ответ мастера?"
            text={REGENERATE_QUESTION}
            yes="Да, перегенерировать"
            variant="danger"
            busy={busy}
            onYes={() => {
              setAsking(null);
              void press("regenerate");
            }}
            onCancel={() => setAsking(null)}
          />
        )}
        {(question === "short" || question === "long") && (
          <Confirm
            label="Начать привал?"
            text={REST_QUESTIONS[question]}
            yes="Да, начать привал"
            variant="primary"
            busy={busy}
            onYes={() => {
              setAsking(null);
              void startRest(question);
            }}
            onCancel={() => setAsking(null)}
          />
        )}
      </div>
    </Panel>
  );
}

/**
 * Кнопки хоста: «Собрать ходы» и начало короткого или долгого привала (в обсуждении вне боя, с
 * вопросом), «+1 мин», «Закрыть сейчас», «Поторопить» (в бою), «+1 мин» и «Закончить привал»
 * (на привале любого вида), «Пауза»/«Продолжить», «Повторить» и «Перегенерировать ответ
 * мастера» (с вопросом). Не-хост панели не получает вовсе; хост видит только то, что сервер
 * примет в этой фазе, и с любой вкладки. Панель начинается заново при каждой смене фазы:
 * вопрос, предупреждение и ошибка прежней фазы новой не нужны.
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
