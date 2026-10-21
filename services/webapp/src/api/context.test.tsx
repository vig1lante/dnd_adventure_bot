import { render, renderHook, screen } from "@testing-library/react";
import { createApi } from "./client";
import { ApiContext, useApi } from "./context";
import { MyGames } from "../screens/MyGames";
import { signInAs } from "../test-setup";

// Клиент API раздаётся экранам через контекст (B8.3). Забытая обёртка — ошибка разработчика,
// а не игрока: она должна падать сразу и понятным текстом, а не уходить в `fetch` вхолостую.

describe("клиент API для экранов", () => {
  test("вне контекста useApi бросает понятную ошибку", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});

    expect(() => renderHook(() => useApi())).toThrow(
      "useApi вызван вне ApiContext.",
    );
  });

  test("внутри контекста экран получает тот самый клиент, что создал App", () => {
    const api = createApi(vi.fn() as unknown as typeof fetch, () => null);

    const { result } = renderHook(() => useApi(), {
      wrapper: ({ children }) => (
        <ApiContext value={api}>{children}</ApiContext>
      ),
    });

    expect(result.current).toBe(api);
  });

  test("экран без обёртки не рисуется и запросов не шлёт", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchImpl = vi.fn();
    vi.stubGlobal("fetch", fetchImpl);
    signInAs();

    expect(() => render(<MyGames />)).toThrow("useApi вызван вне ApiContext.");

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading")).toBeNull();
  });
});
