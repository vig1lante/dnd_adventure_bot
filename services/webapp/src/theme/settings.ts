import { restoreSavedVibration } from "./haptics";

/**
 * Настройки интерфейса этого телефона: размер текста и высокий контраст. Хранятся в
 * localStorage и действуют атрибутами на корне страницы — по ним стили меняют токены кегля
 * (theme.css) и пары цветов (tokens.css). Действующая настройка — атрибут, а не хранилище:
 * оно бывает недоступно (частный режим), и тогда выбор действует до перезагрузки. Вибрация —
 * в theme/haptics.ts, «меньше движения» берётся из системы (theme/motion.ts), звука нет.
 */

export type TextSize = "normal" | "large" | "huge";

/** Ключ размера текста: `"large"` или `"huge"`; нет ключа — обычный. */
export const TEXT_SIZE_KEY = "dnd-ui-text-size";
/** Ключ контраста: `"high"` — высокий; нет ключа — обычный. */
export const CONTRAST_KEY = "dnd-ui-contrast";

const LARGER_SIZES: readonly TextSize[] = ["large", "huge"];

// Хранилище бывает недоступно (частный режим, запрет сайта): тогда настройка как по умолчанию.
function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Не запомнили — на этой странице настройка всё равно действует, игре это не мешает.
  }
}

/** Размер текста по строке; чужое значение — обычный. */
const sizeOf = (value: string | null | undefined): TextSize =>
  LARGER_SIZES.find((size) => size === value) ?? "normal";

/** Сохранённый размер текста; чужое значение в хранилище — обычный. */
export function savedTextSize(): TextSize {
  return sizeOf(read(TEXT_SIZE_KEY));
}

export function savedHighContrast(): boolean {
  return read(CONTRAST_KEY) === "high";
}

/** Размер текста, который действует на странице сейчас (атрибут корня). */
export function currentTextSize(): TextSize {
  return sizeOf(document.documentElement.dataset.textSize);
}

/** Действует ли сейчас высокий контраст (атрибут корня). */
export function currentHighContrast(): boolean {
  return document.documentElement.dataset.contrast === "high";
}

function applyTextSize(size: TextSize): void {
  const root = document.documentElement;
  if (size === "normal") delete root.dataset.textSize;
  else root.dataset.textSize = size;
}

function applyHighContrast(on: boolean): void {
  const root = document.documentElement;
  if (on) root.dataset.contrast = "high";
  else delete root.dataset.contrast;
}

/** Меняет размер текста на странице и запоминает выбор. */
export function setTextSize(size: TextSize): void {
  applyTextSize(size);
  write(TEXT_SIZE_KEY, size === "normal" ? null : size);
}

/** Включает или выключает высокий контраст и запоминает выбор. */
export function setHighContrast(on: boolean): void {
  applyHighContrast(on);
  write(CONTRAST_KEY, on ? "high" : null);
}

/** При запуске приложения: действуют сохранённые настройки — на корне страницы и вибрация. */
export function applySavedSettings(): void {
  applyTextSize(savedTextSize());
  applyHighContrast(savedHighContrast());
  restoreSavedVibration();
}
