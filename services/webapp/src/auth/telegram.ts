import {
  closingBehavior,
  init,
  retrieveLaunchParams,
  retrieveRawInitData,
  themeParams,
} from "@tma.js/sdk-react";

// Ошибки SDK в журнал не пишем: в тексте InvalidLaunchParamsError лежат параметры
// запуска целиком, то есть initData.

/**
 * Строка initData ровно в том виде, как её передал Telegram (проверяет её сервер).
 * Вне Telegram SDK бросает ошибку, а без данных входа (например, запуск из меню
 * вложений) отдаёт `undefined`: в обоих случаях входа через Telegram нет — `null`.
 * Из `initDataUnsafe` ничего не берём: это непроверенные данные.
 */
export function readInitData(): string | null {
  try {
    return retrieveRawInitData() || null;
  } catch {
    return null;
  }
}

/** Параметр запуска `startapp=<код игры>` (tgWebAppStartParam); вне Telegram — `null`. */
export function readStartParam(): string | null {
  try {
    return retrieveLaunchParams().tgWebAppStartParam ?? null;
  } catch {
    return null;
  }
}

// Оформление не критично для игры: на старом клиенте Telegram часть вызовов недоступна,
// и вход от этого ломаться не должен.
function bestEffort(step: () => void, name: string): void {
  try {
    step();
  } catch {
    console.warn(`Telegram SDK: шаг «${name}» недоступен.`);
  }
}

/**
 * Запускает SDK внутри Telegram: тема доступна как CSS-переменные `--tg-theme-*`
 * (светлую или тёмную палитру выбирают по `themeParams.isDark()`), а при
 * закрытии Mini App Telegram спрашивает подтверждение, чтобы не потерять черновик хода.
 */
export function setupTelegram(): void {
  try {
    init();
  } catch {
    console.warn("Telegram SDK не запущен: приложение открыто вне Telegram.");
    return;
  }
  bestEffort(() => {
    themeParams.mount();
    themeParams.bindCssVars();
  }, "тема");
  bestEffort(() => {
    closingBehavior.mount();
    closingBehavior.enableConfirmation();
  }, "подтверждение закрытия");
}
