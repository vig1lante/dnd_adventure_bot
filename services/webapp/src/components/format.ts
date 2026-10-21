import type { EffectView, WeaponRange } from "../api/types";

// Подписи для чисел и названий, которые считает или называет сервер. Здесь нет правил игры:
// только русские слова вместо английских id и привычная запись.

/** Число со знаком для листа и формул: «+3», «−1» (настоящий минус), ноль — «+0». */
export function signed(value: number): string {
  return value < 0 ? `−${Math.abs(value)}` : `+${value}`;
}

/** Запись костей по-русски: «1d8+3» → «1к8+3». Слова с буквой d не трогает. */
export function ruDice(expr: string): string {
  return expr.replace(/(\d)d(\d)/g, "$1к$2");
}

// Тринадцать типов урона SRD (id приходят английскими из движка, русской таблицы там нет).
const DAMAGE_TYPES: Record<string, string> = {
  acid: "кислотный",
  bludgeoning: "дробящий",
  cold: "холодный",
  fire: "огненный",
  force: "силовой",
  lightning: "электрический",
  necrotic: "некротический",
  piercing: "колющий",
  poison: "ядовитый",
  psychic: "психический",
  radiant: "световой",
  slashing: "рубящий",
  thunder: "звуковой",
};

/** Русское название типа урона; неизвестный id остаётся как есть. */
export function damageTypeName(id: string): string {
  return DAMAGE_TYPES[id] ?? id;
}

const RANGES: Record<WeaponRange, string> = {
  melee: "ближний бой",
  near: "ближняя дистанция",
  far: "дальняя дистанция",
};

/** Русская подпись дальности оружия; неизвестное значение остаётся как есть. */
export function rangeName(range: WeaponRange): string {
  return RANGES[range] ?? range;
}

/** Состояние или эффект: название от сервера и срок, если он есть: «Благословение (3 р.)». */
export function effectLabel(effect: EffectView): string {
  return effect.rounds_left === null
    ? effect.name
    : `${effect.name} (${effect.rounds_left} р.)`;
}
