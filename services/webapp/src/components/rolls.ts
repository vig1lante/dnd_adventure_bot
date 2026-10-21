import type { RollData } from "../api/types";
import { ruDice } from "./format";

/**
 * Бросок, как его показывает карточка. Причины (`reason`) здесь нет нарочно: движок
 * пишет её как попало и в ней бывает имя статблока («Скрытность: Фанатик культа»),
 * а карточка подписывается по `label` и `actor`.
 */
export type Roll = Omit<RollData, "reason" | "actor_id">;

const ADVANTAGES = new Set(["none", "advantage", "disadvantage"]);

const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const isNumbers = (value: unknown): value is number[] =>
  Array.isArray(value) && value.every(isNumber);
const isNullable = <T>(
  value: unknown,
  check: (item: unknown) => item is T,
): value is T | null | undefined =>
  value === null || value === undefined || check(value);
const isBoolean = (value: unknown): value is boolean =>
  typeof value === "boolean";

/**
 * Разбирает `data` записи ленты в бросок. Сервер — доверенный источник, но запись может быть
 * старой или чужой формы: тогда `null`, и вместо карточки показывается запасной текст.
 */
export function parseRoll(data: Record<string, unknown> | null): Roll | null {
  if (data === null) return null;
  const { actor, label, expr, rolls, kept, modifier, total } = data;
  if (
    typeof actor !== "string" ||
    typeof label !== "string" ||
    typeof expr !== "string" ||
    !isNumbers(rolls) ||
    !isNumbers(kept) ||
    !isNumber(modifier) ||
    !isNumber(total) ||
    !isNullable(data.natural, isNumber) ||
    !isNullable(data.dc, isNumber) ||
    !isNullable(data.success, isBoolean) ||
    !(data.target_ac === undefined || isNumber(data.target_ac)) ||
    !(data.advantage === undefined || ADVANTAGES.has(String(data.advantage)))
  ) {
    return null;
  }
  return {
    actor,
    label,
    expr,
    rolls,
    kept,
    modifier,
    total,
    natural: (data.natural as number | null | undefined) ?? null,
    advantage: (data.advantage as Roll["advantage"] | undefined) ?? "none",
    dc: (data.dc as number | null | undefined) ?? null,
    success: (data.success as boolean | null | undefined) ?? null,
    target_ac: data.target_ac as number | undefined,
  };
}

/**
 * Из чего сложился итог. К20: выпавшее, бонус, затем кости эффектов (Благословение, Указание)
 * из `kept` после к20 — «14 + 3 + 2». Остальные броски (урон, лечение) — запись костей по-русски,
 * бонус внутри неё: «1к8+3».
 */
export function rollFormula(roll: Roll): string {
  if (roll.natural === null) return ruDice(roll.expr);
  const terms = [
    roll.natural,
    ...(roll.modifier === 0 ? [] : [roll.modifier]),
    ...roll.kept.slice(1),
  ];
  return terms
    .map((term, index) =>
      index === 0 ? String(term) : `${term < 0 ? "−" : "+"} ${Math.abs(term)}`,
    )
    .join(" ");
}

/** Слагаемых больше одного — тогда запись «= итог» что-то добавляет. */
function hasSum(roll: Roll): boolean {
  return roll.natural === null || rollFormula(roll).includes(" ");
}

/** «против Сл 15» (проверка, спасбросок) или «против КД 15» (атака монстра по герою). */
export function againstLabel(roll: Roll): string | null {
  if (roll.dc !== null) return `против Сл ${roll.dc}`;
  if (roll.target_ac !== undefined) return `против КД ${roll.target_ac}`;
  return null;
}

/** Атака: КД героя-цели в данных (монстр) или «атака» в названии (герой, КД монстра скрыт). */
function isAttack(roll: Roll): boolean {
  return roll.target_ac !== undefined || /атак/i.test(roll.label);
}

/** «Успех», «Неудача», «Попадание», «Промах»; бросок без исхода (урон, лечение) — `null`. */
export function outcomeLabel(roll: Roll): string | null {
  if (roll.success === null) return null;
  if (isAttack(roll)) return roll.success ? "Попадание" : "Промах";
  return roll.success ? "Успех" : "Неудача";
}

/** Карточка одной строкой: «Ловкость (Акробатика): 14 + 3 = 17 против Сл 15 ✓». */
export function rollSummary(roll: Roll): string {
  const formula = rollFormula(roll);
  const against = againstLabel(roll);
  const mark = roll.success === null ? null : roll.success ? "✓" : "✗";
  const sum = hasSum(roll) ? `${formula} = ${roll.total}` : formula;
  return [`${roll.label}: ${sum}`, against, mark]
    .filter((part) => part !== null)
    .join(" ");
}
