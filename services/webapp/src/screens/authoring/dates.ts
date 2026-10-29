const MONTHS = [
  "января",
  "февраля",
  "марта",
  "апреля",
  "мая",
  "июня",
  "июля",
  "августа",
  "сентября",
  "октября",
  "ноября",
  "декабря",
];

/**
 * Момент по часам телефона: «29 октября 2026, 07:05»; непонятная строка — пусто. Им же «Мои игры»
 * подписывают дату создания игры.
 */
export function momentText(iso: string): string {
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return "";
  const time = [moment.getHours(), moment.getMinutes()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return `${moment.getDate()} ${MONTHS[moment.getMonth()]} ${moment.getFullYear()}, ${time}`;
}
