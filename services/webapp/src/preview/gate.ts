/** Просят ли страницу превью стиля (`/?preview=style`). В прод-сборку превью не попадает: см. main.tsx. */
export function wantsStylePreview(search: string): boolean {
  return new URLSearchParams(search).get("preview") === "style";
}
