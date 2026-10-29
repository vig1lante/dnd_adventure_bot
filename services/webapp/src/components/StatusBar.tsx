import type { PlayerStatus, SceneView, TurnPhase } from "../api/types";
import type { GameView, LiveConnection } from "../store/game";
import { Avatar, Badge, Icon, Panel } from "../ui";
import type { BadgeProps } from "../ui";
import { connectionLost } from "./access";
import { Countdown } from "./Countdown";
import "./game.css";
import { phaseLabel } from "./phases";

// У `idle` (ход ещё не начат) значка нет: «Пишет» горит, только пока у игрока черновик с текстом
// (решение D17). Незнакомый статус тоже остаётся без значка.
const STATUS_BADGES: Partial<
  Record<PlayerStatus, { label: string; tone: BadgeProps["tone"] }>
> = {
  writing: { label: "Пишет", tone: "amber" },
  ready: { label: "Готов", tone: "cloth" },
  pass: { label: "Пас", tone: "neutral" },
};

// Значки «пишет, готов, пас» говорят о ходе, который игроки ещё пишут или только что сдали.
const SHOWS_STATUSES: ReadonlySet<TurnPhase | null> = new Set([
  "discussion",
  "collecting",
  "dm_failed",
]);

// Для фазы хода с отсчётом: его подпись и поле сводки игры, где лежит полный срок (по нему тает воск).
const COUNTDOWNS: Partial<
  Record<
    TurnPhase,
    {
      label: string;
      seconds: "collect_seconds" | "hurry_seconds" | "camp_seconds";
    }
  >
> = {
  collecting: { label: "До конца сбора", seconds: "collect_seconds" },
  combat_wait: { label: "Время на ход", seconds: "hurry_seconds" },
  camp: { label: "До конца привала", seconds: "camp_seconds" },
};

/**
 * Полный срок отсчёта фазы, с. У привала он зависит от вида: короткий тает от своего срока, а
 * долгий и привал, вид которого ещё не пришёл (старые игры, кадр в пути), — от `camp_seconds`.
 */
function totalSecondsOf(view: GameView): number | undefined {
  const { phase, game, rest } = view;
  if (phase.turn_phase === "camp" && rest?.kind === "short") {
    return game.short_rest_seconds;
  }
  const countdown =
    phase.turn_phase === null ? undefined : COUNTDOWNS[phase.turn_phase];
  return countdown === undefined ? undefined : game[countdown.seconds];
}

// «Полдень», а не «день»: иначе вышло бы «День 2, день» (решение B10.1.gate).
const PARTS_OF_DAY: Record<SceneView["part"], string> = {
  morning: "утро",
  day: "полдень",
  evening: "вечер",
  night: "ночь",
};

/** «День 2, утро» из сцены снимка; без сцены (старый снимок) — ничего. */
function dayPartOf(scene: SceneView | null): string | null {
  return scene === null
    ? null
    : `День ${scene.day}, ${PARTS_OF_DAY[scene.part]}`;
}

/** Связь с игрой оборвалась и вернётся сама. Показывается и там, где строки статуса ещё нет. */
export function ReconnectingNote() {
  return (
    <p role="status" className="game-reconnecting">
      Переподключаемся…
    </p>
  );
}

export interface StatusBarProps {
  view: GameView;
  clockOffsetMs: number;
  connection: LiveConnection;
}

/**
 * Строка статуса: фаза игры, день и часть суток и отсчёт до срока по серверным часам (в том
 * числе «До конца привала»); она прилипает к верху экрана. День стоит вне живой области фазы:
 * его не нужно зачитывать при каждой смене фазы. При обрыве связи «Переподключаемся…» стоит в
 * ней же: игрок, прокрутивший ленту вниз, видит обрыв там, где играет. Значки игроков («пишет»,
 * «готов», «пас») идут отдельным рядом под ней.
 */
export function StatusBar({ view, clockOffsetMs, connection }: StatusBarProps) {
  const { phase, game, statuses } = view;
  const countdown =
    phase.turn_phase === null ? undefined : COUNTDOWNS[phase.turn_phase];
  const day = dayPartOf(phase.scene);

  return (
    <>
      <Panel className="game-status">
        <div className="game-status__head">
          <p className="game-status__phase" role="status">
            <Icon name="hourglass" size={22} />
            <span>{phaseLabel(phase)}</span>
          </p>
          {day !== null && <span className="game-status__day">{day}</span>}
        </div>
        <Countdown
          phase={phase}
          clockOffsetMs={clockOffsetMs}
          totalSeconds={totalSecondsOf(view)}
          label={countdown?.label}
        />
        {connectionLost(connection) && <ReconnectingNote />}
      </Panel>
      {SHOWS_STATUSES.has(phase.turn_phase) && (
        <ul className="game-players" aria-label="Игроки">
          {game.players.map((player) => {
            const status = statuses[player.user_id];
            const badge =
              status === undefined ? null : (STATUS_BADGES[status] ?? null);
            return (
              <li key={player.user_id} className="game-players__item">
                <Avatar name={player.name} size="sm" decorative />
                <span className="game-players__name">{player.name}</span>
                {badge !== null && (
                  <Badge tone={badge.tone}>{badge.label}</Badge>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
