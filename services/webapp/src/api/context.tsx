import { createContext, useContext } from "react";
import type { Api } from "./client";

/**
 * Клиент API текущего приложения. Его создаёт `App` один раз, а экраны берут отсюда,
 * чтобы не тащить клиент через каждый уровень разметки. Тесты экранов подставляют
 * клиент на заглушке сервера.
 */
export const ApiContext = createContext<Api | null>(null);

/** Клиент API для экрана; вне `ApiContext` — ошибка разработчика, а не игрока. */
export function useApi(): Api {
  const api = useContext(ApiContext);
  if (api === null) throw new Error("useApi вызван вне ApiContext.");
  return api;
}
