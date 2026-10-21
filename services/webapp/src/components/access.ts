import type { CombatView, Fighter, PhaseView } from "../api/types";
import type { LiveConnection } from "../store/game";

export interface ComposerAccess {
  /** Можно ли сейчас писать, сдавать и менять ход. */
  open: boolean;
  /** Почему закрыто; у открытого поля пояснения нет. */
  note: string | null;
}

const OPEN: ComposerAccess = { open: true, note: null };
const closed = (note: string): ComposerAccess => ({ open: false, note });

/**
 * Связь с игрой оборвалась: экран держит последнее, что успел получить, и обновится, когда
 * сокет вернётся. Первое подключение (`connecting`) и экран без игры (`idle`) не обрыв:
 * снимка там ещё нет, показывать и закрывать нечего.
 */
export function connectionLost(connection: LiveConnection): boolean {
  return connection === "reconnecting";
}

/** Пояснение закрытых из-за обрыва полей отправки: поле хода и «Сказать у костра». */
export const CONNECTION_LOST_NOTE =
  "Нет связи с игрой: пока она не вернётся, отправить нельзя.";

/**
 * Боец, чей сейчас ход. Пусто, если боя нет, ходит спрятанный монстр (`current_id` пуст) или
 * id не из порядка хода: такого бойца игрокам не показывают.
 */
export function currentFighter(combat: CombatView | null): Fighter | null {
  if (combat === null || combat.current_id === null) return null;
  return (
    combat.order.find((fighter) => fighter.id === combat.current_id) ?? null
  );
}

/**
 * Герой не может действовать (парализован, без сознания и т.п.): так решил движок, клиент лишь
 * читает `can_act`. У монстров признака нет, а у героя без него (старый снимок) считаем, что он
 * действует: ход всё равно принимает или отклоняет сервер.
 */
export function cannotAct(fighter: Fighter | null): boolean {
  return fighter?.kind === "pc" && fighter.can_act === false;
}

/**
 * Сервер ждёт ход этого игрока: сейчас ходит его герой (id бойца в бою совпадает с id
 * персонажа) и герой может действовать. Недееспособного героя сервер не ждёт, за него ходит
 * мастер, поэтому поле хода ему не нужно.
 */
export function isHeroTurn(
  combat: CombatView | null,
  characterId: string | null,
): boolean {
  return (
    characterId !== null &&
    combat?.current_id === characterId &&
    !cannotAct(currentFighter(combat))
  );
}

/** Кто ходит в бою: по порядку инициативы, а если его там нет — осторожное «не ваш ход». */
function combatNote(combat: CombatView | null): string {
  if (combat === null) return "Сейчас не ваш ход.";
  if (combat.current_id === null) return "Ходит мастер.";
  const fighter = currentFighter(combat);
  if (fighter === null) return "Сейчас не ваш ход.";
  return cannotAct(fighter)
    ? `${fighter.name} не может действовать.`
    : `Ходит ${fighter.name}.`;
}

/**
 * Доступно ли игроку поле хода. Это подсказка интерфейсу: ход принимает или отклоняет
 * сервер (409), а клиент лишь не предлагает того, что заведомо не примут. Поле открыто,
 * пока игра идёт (не пауза) и игроки обсуждают ход или мастер собирает их; в бою — только
 * в ход своего героя по порядку инициативы, если он может действовать (`cannotAct`).
 *
 * При обрыве связи поле закрыто, и причиной названа связь, а не фаза: за время обрыва игра
 * могла уйти вперёд, и фаза на экране устарела. Связь по умолчанию есть — вызовы, которым
 * обрыв безразличен (подсказка «Ходит Борин.»), её не передают.
 */
export function composerAccess(
  phase: PhaseView,
  combat: CombatView | null,
  characterId: string | null,
  connection: LiveConnection = "open",
): ComposerAccess {
  if (connectionLost(connection)) {
    return closed(CONNECTION_LOST_NOTE);
  }
  switch (phase.phase) {
    case "paused":
      return closed("Игра на паузе: хост должен нажать «Продолжить».");
    case "lobby":
    case "weaving":
      return closed("Игра ещё не началась.");
    case "finished":
      return closed("Игра окончена.");
    case "playing":
      break;
  }
  switch (phase.turn_phase) {
    case "discussion":
    case "collecting":
      return OPEN;
    case "resolving":
      return closed("Мастер думает: ходы сейчас не принимаются.");
    case "dm_failed":
      return closed(
        "Мастер не справился: ходы сохранены, хост может нажать «Повторить».",
      );
    case "camp":
      return closed("Привал: ходы не принимаются.");
    case "combat_wait":
      return isHeroTurn(combat, characterId)
        ? OPEN
        : closed(combatNote(combat));
    case null:
      return closed("Ходы пока не принимаются.");
  }
}
