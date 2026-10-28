import type { PhaseView, RestKind, TurnPhase } from "../api/types";

const TURN_LABELS: Record<TurnPhase, string> = {
  discussion: "Обсуждение",
  collecting: "Сбор ходов",
  resolving: "Мастер думает…",
  dm_failed: "Мастер не справился",
  combat_wait: "Бой",
  camp: "Привал",
};

const WEAVING_LABEL = "Мастер связывает ваши истории с миром…";

/**
 * Подпись текущей фазы для строки статуса. Пауза и начало игры важнее фазы хода: на паузе
 * неважно, что обсуждали, а в начале игры «Мастер думает…» — это вплетение героев.
 */
export function phaseLabel(phase: PhaseView): string {
  switch (phase.phase) {
    case "paused":
      return "Пауза";
    case "lobby":
      return "Ожидание начала игры";
    case "finished":
      return "Игра завершена";
    case "weaving":
      return phase.turn_phase === "dm_failed"
        ? "Мастер не справился с началом игры"
        : WEAVING_LABEL;
    case "playing":
      return phase.turn_phase === null
        ? "Игра идёт"
        : TURN_LABELS[phase.turn_phase];
  }
}

const REST_LABELS: Record<RestKind, string> = {
  short: "Короткий привал",
  long: "Долгий привал",
};

/**
 * Название привала по виду. `null` — вид ещё не пришёл (кадр `rest_started` в пути): тогда
 * просто «Привал», как у фазы хода.
 */
export function restLabel(kind: RestKind | null): string {
  return kind === null ? TURN_LABELS.camp : REST_LABELS[kind];
}
