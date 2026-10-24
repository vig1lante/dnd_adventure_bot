/** Оставшееся время как «м:сс» (или «ч:мм:сс»); дробные секунды округляются вверх. */
export function formatClock(totalSeconds: number): string {
  const seconds = Number.isFinite(totalSeconds)
    ? Math.max(0, Math.ceil(totalSeconds))
    : 0;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}`
    : `${minutes}:${rest}`;
}

/** Инициалы для жетона: первые буквы двух первых слов; без имени — «?». */
export function initials(name: string): string {
  const letters = name
    .split(/[\s\-_]+/)
    .filter((word) => word !== "")
    .slice(0, 2)
    .map((word) => Array.from(word)[0].toUpperCase());
  return letters.length === 0 ? "?" : letters.join("");
}

/** Число оттенков жетона героя (см. --color-tone-* в tokens.css). */
export const AVATAR_TONES = 6;

/** Оттенок жетона по имени: одно имя всегда получает один цвет. */
export function avatarTone(name: string): number {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)!) >>> 0;
  return hash % AVATAR_TONES;
}

export type HpTone = "high" | "mid" | "low";

/** Цвет полосы здоровья по доле: больше половины, до четверти, меньше. Только вид, не правило. */
export function hpTone(current: number, max: number): HpTone {
  const ratio = max > 0 ? current / max : 0;
  if (ratio > 0.5) return "high";
  if (ratio > 0.25) return "mid";
  return "low";
}

/**
 * Расстояние из футов сервера в метрах для игрока, как units.meters_text в движке: 5 футов = 1,5 м,
 * дробь через запятую, без «,0» и без округления до десятых: 30 → «9 м», 2,5 → «0,75 м».
 * Только запись, не правило.
 */
export function formatMeters(feet: number): string {
  // × 3 / 10, а не × 0,3, и не больше шести значащих цифр — как формат `:g` движка: так хвост
  // float (0,1 фт → 0,030000000000000006) не попадает в текст, а тексты двух сервисов совпадают.
  const meters = Number(((feet * 3) / 10).toPrecision(6));
  return `${String(meters).replace(".", ",")} м`;
}
