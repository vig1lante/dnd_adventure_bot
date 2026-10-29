import { hapticFeedback } from "@tma.js/sdk-react";
import type { NotificationHapticFeedbackType } from "@tma.js/sdk-react";

/**
 * Ключ настройки «Вибрация» в localStorage: `"off"` — выключена, иначе включена. Переключатель —
 * в настройках интерфейса (`ui/Settings`); хранится только на этом телефоне.
 */
export const VIBRATION_KEY = "dnd-ui-vibration";

/** Понятные события, на которые телефон коротко вибрирует. */
export type HapticEvent = "your-turn" | "collecting" | "rest";

// Внимание («warning») — когда игрока ждут; «success» — спокойное событие для всех.
const VIBRATIONS: Record<HapticEvent, NotificationHapticFeedbackType> = {
  "your-turn": "warning",
  collecting: "warning",
  rest: "success",
};

/**
 * Выбор игрока на этой странице. Он и есть действующая настройка: хранилище бывает недоступно
 * (частный режим, запрет сайта, переполнение), и тогда выбор не запомнится, но до перезагрузки
 * действует. `null` — игрок на этой странице ничего не менял: действует сохранённое.
 */
let chosen: boolean | null = null;

// Сохранённый выбор; хранилище недоступно — вибрация как по умолчанию (включена).
function savedVibration(): boolean {
  try {
    return window.localStorage.getItem(VIBRATION_KEY) !== "off";
  } catch {
    return true;
  }
}

/** Действует ли вибрация сейчас: выбор этой страницы, иначе сохранённый. */
export function vibrationEnabled(): boolean {
  return chosen ?? savedVibration();
}

/** Включает или выключает вибрацию сразу и запоминает выбор, если хранилище доступно. */
export function setVibrationEnabled(on: boolean): void {
  chosen = on;
  try {
    if (on) window.localStorage.removeItem(VIBRATION_KEY);
    else window.localStorage.setItem(VIBRATION_KEY, "off");
  } catch {
    // Не запомнили — выбор действует до перезагрузки страницы; игре это не мешает.
  }
}

/**
 * Запуск приложения: действует сохранённый выбор (зовёт `applySavedSettings` из
 * theme/settings, как и для размера текста и контраста).
 */
export function restoreSavedVibration(): void {
  chosen = null;
}

/**
 * Короткая вибрация Telegram на событие. Только внутри Telegram (вне его SDK сообщает, что
 * вибрации нет) и если игрок её не выключил. Вибрация — удобство: сбой SDK игру не ломает, а
 * текст его ошибки в консоль не пишем — в нём бывают параметры запуска, то есть initData.
 */
export function buzz(event: HapticEvent): void {
  if (!vibrationEnabled()) return;
  try {
    if (!hapticFeedback.isSupported()) return;
    hapticFeedback.notificationOccurred(VIBRATIONS[event]);
  } catch {
    // Старый клиент или страница вне Mini App: молча без вибрации.
  }
}
