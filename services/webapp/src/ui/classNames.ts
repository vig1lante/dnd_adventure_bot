/** Склеивает имена классов, пропуская пустые значения. */
export function classNames(
  ...parts: ReadonlyArray<string | false | null | undefined>
): string {
  return parts.filter(Boolean).join(" ");
}
