// Ключи объектов по порядку: сервер и форма могут перечислять одни и те же поля по-разному.
const sortedKeys = (_key: string, value: unknown) =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(
        Object.entries(value).sort(([a], [b]) => (a < b ? -1 : 1)),
      )
    : value;

/** Одинаковы ли данные раздела по содержимому (порядок полей объектов не важен). */
export const sameData = (a: unknown, b: unknown) =>
  JSON.stringify(a, sortedKeys) === JSON.stringify(b, sortedKeys);

/** Необязательная ссылка или id: пустое поле — `null`, как ждёт сервер. */
export const orNull = (text: string): string | null =>
  text.trim() === "" ? null : text;

export interface Option {
  value: string;
  label: string;
}

/**
 * Варианты выбора и текущее значение, даже если его среди них нет (сущность удалили или ещё не
 * сохранили): список показывает правду, а не молча подставляет другой вариант.
 */
export const withCurrent = (
  options: readonly Option[],
  value: string,
): Option[] =>
  value === "" || options.some((option) => option.value === value)
    ? [...options]
    : [...options, { value, label: `${value} — нет в списке` }];

/** Число из поля ввода (запятая или точка); не число или дробь там, где нужно целое, — `null`. */
export function parseNumber(text: string, integer: boolean): number | null {
  const trimmed = text.trim();
  const value = Number(trimmed.replace(",", "."));
  if (trimmed === "" || !Number.isFinite(value)) return null;
  return integer && !Number.isInteger(value) ? null : value;
}

/** Есть ли в данных раздела неверное число: `NaN` ставит поле, в котором набрано не число. */
export function hasBadNumber(value: unknown): boolean {
  if (typeof value === "number") return Number.isNaN(value);
  return (
    value !== null &&
    typeof value === "object" &&
    Object.values(value).some(hasBadNumber)
  );
}
