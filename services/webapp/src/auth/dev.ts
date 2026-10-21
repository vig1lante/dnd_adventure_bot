import type { DevLogin } from "../api/types";

/** Игроки dev-режима: шесть вкладок браузера с разными `dev-N` заменяют шестерых друзей. */
export const DEV_LOGINS: readonly DevLogin[] = [
  "dev-1",
  "dev-2",
  "dev-3",
  "dev-4",
  "dev-5",
  "dev-6",
];

/** Логин из `?dev=dev-3`; неизвестное значение игнорируется, на сервер оно не уходит. */
export function devLoginFromSearch(search: string): DevLogin | null {
  const requested = new URLSearchParams(search).get("dev");
  return DEV_LOGINS.find((login) => login === requested) ?? null;
}
