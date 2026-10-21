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
