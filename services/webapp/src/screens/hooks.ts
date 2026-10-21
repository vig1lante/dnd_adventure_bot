import { useCallback, useEffect, useState } from "react";
import { ApiError, errorText } from "../api/client";

/** Результат загрузки: идёт, готов или ошибка с текстом для игрока. */
export type Loaded<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

const LOADING: Loaded<never> = { status: "loading" };

/**
 * Загружает данные при показе экрана и по `reload()`. `load` должен быть стабильным
 * (`useCallback`): новая функция запускает загрузку заново. Ответ, пришедший после
 * ухода с экрана или после новой загрузки, отбрасывается.
 */
export function useLoaded<T>(load: () => Promise<T>): [Loaded<T>, () => void] {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{
    load: () => Promise<T>;
    attempt: number;
    value: Loaded<T>;
  } | null>(null);

  useEffect(() => {
    let outdated = false;
    const settle = (value: Loaded<T>) => {
      if (!outdated) setResult({ load, attempt, value });
    };
    load().then(
      (data) => settle({ status: "ready", data }),
      (error: unknown) =>
        settle({ status: "error", message: errorText(error) }),
    );
    return () => {
      outdated = true;
    };
  }, [load, attempt]);

  // Результат прежней загрузки показывать нельзя: пока идёт новая, экран «загружается».
  const current =
    result !== null && result.load === load && result.attempt === attempt
      ? result.value
      : LOADING;
  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  return [current, reload];
}

/**
 * Одно действие игрока за раз: пока запрос идёт, повторное нажатие не уходит на сервер,
 * а отказ сервера остаётся текстом для экрана (и подробностями отказа правил, если они
 * есть). `run` возвращает, удалось ли действие.
 */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);

  const run = useCallback(
    async (action: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true);
      setError(null);
      setProblems([]);
      try {
        await action();
        return true;
      } catch (failure) {
        setError(errorText(failure));
        setProblems(
          failure instanceof ApiError ? (failure.problems ?? []) : [],
        );
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );
  return { busy, error, problems, run };
}
