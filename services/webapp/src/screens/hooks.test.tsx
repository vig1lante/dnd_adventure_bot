import { act, renderHook, waitFor } from "@testing-library/react";
import { ApiError } from "../api/client";
import { useAction, useLoaded } from "./hooks";

/** Обещание, которое тест выполняет сам и в нужный момент. */
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (reason: unknown) => void = () => {};
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

describe("useLoaded", () => {
  test("сначала загрузка, затем данные", async () => {
    const answer = deferred<string>();
    const load = () => answer.promise;
    const { result } = renderHook(() => useLoaded(load));
    expect(result.current[0]).toEqual({ status: "loading" });

    await act(async () => answer.resolve("список"));

    expect(result.current[0]).toEqual({ status: "ready", data: "список" });
  });

  test("ошибка запроса — русский текст сервера", async () => {
    const load = () => Promise.reject(new ApiError(500, "База недоступна."));
    const { result } = renderHook(() => useLoaded(load));

    await waitFor(() =>
      expect(result.current[0]).toEqual({
        status: "error",
        message: "База недоступна.",
      }),
    );
  });

  test("сбой без текста сервера — общая фраза", async () => {
    const load = () => Promise.reject(new TypeError("boom"));
    const { result } = renderHook(() => useLoaded(load));

    await waitFor(() => expect(result.current[0].status).toBe("error"));
    expect(result.current[0]).toMatchObject({
      message: "Что-то пошло не так. Попробуйте ещё раз.",
    });
  });

  test("повтор: снова «загрузка», затем новые данные", async () => {
    let calls = 0;
    const load = () => Promise.resolve(`ответ ${++calls}`);
    const { result } = renderHook(() => useLoaded(load));
    await waitFor(() =>
      expect(result.current[0]).toEqual({ status: "ready", data: "ответ 1" }),
    );

    act(() => result.current[1]());

    expect(result.current[0]).toEqual({ status: "loading" });
    await waitFor(() =>
      expect(result.current[0]).toEqual({ status: "ready", data: "ответ 2" }),
    );
  });

  test("запоздавший ответ прежней загрузки не затирает новый", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const answers = [first, second];
    let calls = 0;
    const load = () => answers[calls++].promise;
    const { result } = renderHook(() => useLoaded(load));

    act(() => result.current[1]());
    await act(async () => second.resolve("новый"));
    await act(async () => first.resolve("старый"));

    expect(result.current[0]).toEqual({ status: "ready", data: "новый" });
  });

  test("ответ после ухода с экрана ничего не меняет", async () => {
    const answer = deferred<string>();
    const load = () => answer.promise;
    const { result, unmount } = renderHook(() => useLoaded(load));

    unmount();
    await act(async () => answer.resolve("поздно"));

    expect(result.current[0]).toEqual({ status: "loading" });
  });

  test("новая функция загрузки запускает загрузку заново", async () => {
    const { result, rerender } = renderHook(({ load }) => useLoaded(load), {
      initialProps: { load: () => Promise.resolve("первая") },
    });
    await waitFor(() =>
      expect(result.current[0]).toEqual({ status: "ready", data: "первая" }),
    );

    rerender({ load: () => Promise.resolve("вторая") });

    expect(result.current[0]).toEqual({ status: "loading" });
    await waitFor(() =>
      expect(result.current[0]).toEqual({ status: "ready", data: "вторая" }),
    );
  });
});

describe("useAction", () => {
  test("пока действие идёт — занято, после успеха — свободно, ошибки нет", async () => {
    const answer = deferred<void>();
    const { result } = renderHook(() => useAction());

    let outcome: Promise<boolean> = Promise.resolve(false);
    act(() => {
      outcome = result.current.run(() => answer.promise);
    });
    expect(result.current.busy).toBe(true);
    await act(async () => answer.resolve());

    await expect(outcome).resolves.toBe(true);
    expect(result.current).toMatchObject({
      busy: false,
      error: null,
      problems: [],
    });
  });

  test("отказ сервера: текст и подробности правил, действие не удалось", async () => {
    const { result } = renderHook(() => useAction());

    let outcome = true;
    await act(async () => {
      outcome = await result.current.run(() =>
        Promise.reject(new ApiError(422, "Нельзя.", ["Первая", "Вторая"])),
      );
    });

    expect(outcome).toBe(false);
    expect(result.current).toMatchObject({
      busy: false,
      error: "Нельзя.",
      problems: ["Первая", "Вторая"],
    });
  });

  test("сбой без текста сервера: общая фраза и без подробностей", async () => {
    const { result } = renderHook(() => useAction());

    await act(async () => {
      await result.current.run(() => Promise.reject(new TypeError("boom")));
    });

    expect(result.current.error).toBe(
      "Что-то пошло не так. Попробуйте ещё раз.",
    );
    expect(result.current.problems).toEqual([]);
  });

  test("следующее действие стирает прежнюю ошибку", async () => {
    const { result } = renderHook(() => useAction());
    await act(async () => {
      await result.current.run(() =>
        Promise.reject(new ApiError(409, "Занято.", undefined)),
      );
    });
    expect(result.current.error).toBe("Занято.");

    await act(async () => {
      await result.current.run(() => Promise.resolve());
    });

    expect(result.current.error).toBeNull();
  });
});
