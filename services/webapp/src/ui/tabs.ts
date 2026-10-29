import type { KeyboardEvent } from "react";

/** Куда ведёт клавиша в наборе вкладок: стрелки — по кругу, Home и End — к краям; иначе `null`. */
export function tabKeyStep(
  key: string,
  index: number,
  count: number,
): number | null {
  switch (key) {
    case "ArrowRight":
      return (index + 1) % count;
    case "ArrowLeft":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

/**
 * Клавиша на вкладке `index` из `count`: в порядке табуляции одна выбранная вкладка, к остальным
 * ведут стрелки, Home и End; фокус идёт за выбором к соседней вкладке того же набора. Прокрутку
 * страницы эти клавиши не делают; остальные клавиши не трогаем.
 */
export function onTabKeyDown(
  event: KeyboardEvent<HTMLElement>,
  index: number,
  count: number,
  select: (index: number) => void,
): void {
  const next = tabKeyStep(event.key, index, count);
  if (next === null) return;
  event.preventDefault();
  select(next);
  event.currentTarget
    .closest("[role='tablist']")
    ?.querySelectorAll<HTMLElement>("[role='tab']")
    [next]?.focus();
}

/** id вкладки и её панели: так вкладка и панель ссылаются друг на друга. */
export function tabIds(idBase: string, id: string) {
  return { tab: `${idBase}-tab-${id}`, panel: `${idBase}-panel-${id}` };
}
