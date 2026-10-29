import type { TextSize } from "../theme/settings";

// Черновик настроек для макетов: меняет только атрибуты страницы и ничего не сохраняет.

/** Размер текста и контраст ставятся атрибутами на корень страницы: они меняют токены темы. */
export function applyTextSize(size: TextSize): void {
  if (size === "normal") delete document.documentElement.dataset.textSize;
  else document.documentElement.dataset.textSize = size;
}

export function applyContrast(high: boolean): void {
  if (high) document.documentElement.dataset.contrast = "high";
  else delete document.documentElement.dataset.contrast;
}
