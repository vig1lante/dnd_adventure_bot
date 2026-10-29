import { useEffect, useState } from "react";

/**
 * Сколько хост ждёт мастера, прежде чем ему предложат «Повторить», когда сервер молчит:
 * разбор идёт долго (в начале игры — две нейронки подряд), и кнопка сразу после «Начать»
 * или «Собрать ходы» только мешала бы.
 */
export const RETRY_PATIENCE_MS = 60_000;

/**
 * Ключ держится без изменений дольше `ms` — для «подождали и пора предложить кнопку».
 * Сменился ключ или стал `null` — ожидание начинается заново. Время считает таймер
 * браузера с того мига, как экран увидел ключ: сервер момент начала разбора не присылает.
 */
export function useStaysFor(key: string | null, ms: number): boolean {
  const [elapsedKey, setElapsedKey] = useState<string | null>(null);
  useEffect(() => {
    if (key === null) return;
    const timer = setTimeout(() => setElapsedKey(key), ms);
    return () => clearTimeout(timer);
  }, [key, ms]);
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
 * нуля, `null` — ожидания нет. Как и `useStaysFor`, время считает браузер с того мига, как экран
 * увидел ключ: сервер момент начала разбора не присылает.
 */
export function useWaitedSeconds(key: string | null): number {
  const [waited, setWaited] = useState<{ key: string; seconds: number } | null>(
    null,
  );
  useEffect(() => {
    if (key === null) return;
    const startedAt = Date.now();
    const timer = setInterval(() => {
      setWaited({ key, seconds: Math.floor((Date.now() - startedAt) / 1000) });
    }, 1000);
    // Ожидание кончилось: тот же ключ в следующий раз (новый ход) считается с нуля.
    return () => {
      clearInterval(timer);
      setWaited(null);
    };
  }, [key]);
  return key !== null && waited?.key === key ? waited.seconds : 0;
}

/** Всё для карточки «Мастер думает…» (`Waiting` из ui): заголовок, фраза по времени и секунды. */
export function useThinking(key: string | null) {
  const seconds = useWaitedSeconds(key);
  return { title: THINKING_TITLE, phrase: thinkingPhrase(seconds), seconds };
}
