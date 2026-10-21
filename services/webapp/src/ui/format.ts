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
