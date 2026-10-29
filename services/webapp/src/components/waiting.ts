import { useEffect, useState } from "react";
import type { PhaseView } from "../api/types";

/**
 * Сколько хост ждёт мастера, прежде чем ему предложат «Повторить», когда сервер молчит:
 * разбор идёт долго (в начале игры — две нейронки подряд), и кнопка сразу после «Начать»
 * или «Собрать ходы» только мешала бы.
 */
export const RETRY_PATIENCE_MS = 60_000;

/**
 * Когда началась текущая фаза хода, по часам браузера: серверное `phase.phase_since` минус сдвиг
 * часов (`live.clockOffsetMs`). `undefined` — начало неизвестно: игра шла до B11, сервер до B11 поля
 * не присылает или строка не читается. Тогда ожидание считается, как раньше, с показа экрана.
 */
export function phaseStartedAt(
  phaseSince: PhaseView["phase_since"],
  clockOffsetMs: number,
): number | undefined {
  const since = Date.parse(phaseSince ?? "");
  return Number.isNaN(since) ? undefined : since - clockOffsetMs;
}

/**
 * Ключ держится без изменений дольше `ms` — для «подождали и пора предложить кнопку».
 * Сменился ключ или стал `null` — ожидание начинается заново. `startedAt` — начало ожидания по
 * часам браузера (`phaseStartedAt`): после перезагрузки страницы срок не начинается заново. Без
 * него время считает таймер браузера с того мига, как экран увидел ключ.
 */
export function useStaysFor(
  key: string | null,
  ms: number,
  startedAt?: number,
): boolean {
  const [elapsedKey, setElapsedKey] = useState<string | null>(null);
  useEffect(() => {
    if (key === null) return;
    const delay =
      startedAt === undefined ? ms : Math.max(0, startedAt + ms - Date.now());
    const timer = setTimeout(() => setElapsedKey(key), delay);
    return () => clearTimeout(timer);
  }, [key, ms, startedAt]);
  return key !== null && elapsedKey === key;
}

/** Одна формулировка долгого ожидания мастера — в лобби и в игре. */
export const THINKING_TITLE = "Мастер думает…";

/**
 * Нейтральные фразы ожидания: они о самом мастере, а не о событиях игры, — ничего не обещают и
 * не раскрывают. Выбор идёт по времени, без случайности (no-client-rules.test.ts).
 */
export const THINKING_PHRASES = [
  "Листает свои заметки…",
  "Перечитывает ваши ходы…",
  "Сверяется с картой…",
  "Подбирает слова…",
  "Точит перо…",
] as const;

const PHRASE_SECONDS = 8;

/** Фраза для `seconds` секунд ожидания: меняется раз в 8 секунд и идёт по кругу. */
export function thinkingPhrase(seconds: number): string {
  const step = Math.max(0, Math.floor(seconds / PHRASE_SECONDS));
  return THINKING_PHRASES[step % THINKING_PHRASES.length];
}

/**
 * Сколько секунд ключ держится без изменений: счётчик «Ждём 0:25». Сменился ключ — отсчёт с
 * нуля, `null` — ожидания нет. Как и у `useStaysFor`, `startedAt` — начало по часам браузера из
 * серверного начала фазы; без него счёт идёт с того мига, как экран увидел ключ.
 */
export function useWaitedSeconds(
  key: string | null,
  startedAt?: number,
): number {
  const [waited, setWaited] = useState<{ key: string; seconds: number } | null>(
    null,
  );
  useEffect(() => {
    if (key === null) return;
    const since = startedAt ?? Date.now();
    const timer = setInterval(() => {
      setWaited({
        key,
        seconds: Math.max(0, Math.floor((Date.now() - since) / 1000)),
      });
    }, 1000);
    // Ожидание кончилось: тот же ключ в следующий раз (новый ход) считается с нуля.
    return () => {
      clearInterval(timer);
      setWaited(null);
    };
  }, [key, startedAt]);
  return key !== null && waited?.key === key ? waited.seconds : 0;
}

/** Всё для карточки «Мастер думает…» (`Waiting` из ui): заголовок, фраза по времени и секунды. */
export function useThinking(key: string | null, startedAt?: number) {
  const seconds = useWaitedSeconds(key, startedAt);
  return { title: THINKING_TITLE, phrase: thinkingPhrase(seconds), seconds };
}
