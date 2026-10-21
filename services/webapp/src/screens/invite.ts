import { shareURL } from "@tma.js/sdk-react";

// Правила Telegram и сервера для имени бота и кода игры: в ссылку не попадает ничего лишнего.
const BOT_USERNAME = /^[A-Za-z][A-Za-z0-9_]{4,31}$/;
const GAME_CODE = /^[A-Z0-9]{6}$/;

/**
 * Ссылка-приглашение: Telegram откроет Mini App бота и передаст код игры как `start_param`.
 * Без настроенного бота (или при неверном имени и коде) ссылки нет — `null`.
 */
export function inviteLink(
  botUsername: string | null,
  code: string,
): string | null {
  if (botUsername === null || !BOT_USERNAME.test(botUsername)) return null;
  if (!GAME_CODE.test(code)) return null;
  return `https://t.me/${botUsername}?startapp=${code}`;
}

/** Копирует текст в буфер обмена; `false` — браузер не дал (нет буфера, нет разрешения). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Можно ли отправить ссылку через выбор чата Telegram (только внутри Mini App). */
export function canShareInTelegram(): boolean {
  return shareURL.isAvailable();
}

/**
 * Открывает выбор чата Telegram со ссылкой и текстом. Telegram закрывает Mini App
 * при открытии такой ссылки, поэтому основной способ — копирование; это запасной.
 * Бросает ошибку, если клиент Telegram не поддерживает вызов.
 */
export function shareInTelegram(link: string, text: string): void {
  shareURL(link, text);
}
