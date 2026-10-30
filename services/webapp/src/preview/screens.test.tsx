import { act, render, screen, waitFor, within } from "@testing-library/react";
import { useGame } from "../store/game";
import { readSource } from "../styles/read-source";
import { PREVIEW_SCREENS, screensPreviewOf, wantsScreensPreview } from "./gate";
import type { PreviewScreen } from "./gate";
import ScreensPreview from "./ScreensPreview";

// Макеты экранов: страница `/?preview=screens` показывает настоящие экраны на данных
// screenData.ts («как есть») и черновики новых элементов («как будет»). Сервер не нужен:
// запросы и сокет игры обслуживает подмена самого превью, в сеть ничего не уходит.

describe("гейт превью экранов", () => {
  test.each([
    ["?preview=screens", true],
    ["?dev=dev-1&preview=screens&screen=story", true],
    ["?preview=style", false],
    ["", false],
    ["?screens=preview", false],
  ])("wantsScreensPreview(%j) → %s", (search, expected) => {
    expect(wantsScreensPreview(search)).toBe(expected);
  });

  test("разбирает экран, вариант и тему", () => {
    expect(
      screensPreviewOf(
        "?preview=screens&screen=combat&variant=next&theme=light",
      ),
    ).toEqual({ screen: "combat", variant: "next", theme: "light" });
  });

  test("по умолчанию — «как есть» в тёмной теме", () => {
    expect(screensPreviewOf("?preview=screens&screen=lobby")).toEqual({
      screen: "lobby",
      variant: "now",
      theme: "dark",
    });
  });

  test("незнакомые значения не ломают страницу: экран — оглавление, остальное — по умолчанию", () => {
    expect(
      screensPreviewOf("?preview=screens&screen=nope&variant=later&theme=blue"),
    ).toEqual({ screen: null, variant: "now", theme: "dark" });
    expect(screensPreviewOf("?preview=screens")).toEqual({
      screen: null,
      variant: "now",
      theme: "dark",
    });
  });

  test("без preview=screens — не превью экранов", () => {
    expect(screensPreviewOf("?screen=story&variant=next")).toBeNull();
    expect(screensPreviewOf("?preview=style&screen=story")).toBeNull();
  });

  test("в списке есть все ключевые экраны", () => {
    expect(PREVIEW_SCREENS).toEqual(
      expect.arrayContaining([
        "login",
        "games",
        "lobby",
        "create",
        "story",
        "combat",
        "camp",
        "journal",
        "authoring",
        "settings",
      ]),
    );
  });
});

// --- данные превью не тянут тестовый код ---

type Fs = {
  readFileSync(path: string, encoding: "utf8"): string;
  existsSync(path: string): boolean;
};
type Path = {
  resolve(...parts: string[]): string;
  dirname(path: string): string;
};

async function node() {
  // Динамический импорт по имени из переменной: статический потребовал бы типов Node в
  // tsconfig.app.json, а клиентский код видеть process и Buffer не должен.
  const load = (name: string) =>
    import(/* @vite-ignore */ name) as Promise<unknown>;
  return {
    fs: (await load("node:fs")) as Fs,
    path: (await load("node:path")) as Path,
  };
}

const IMPORT = /(?:^|\n)\s*(?:import|export)[^"';]*?from\s*["']([^"']+)["']/g;

/** Все модули, которые тянет файл, включая модули его относительных импортов. */
async function importGraph(entry: string): Promise<string[]> {
  const { fs, path } = await node();
  const seen = new Set<string>();
  const specifiers: string[] = [];
  const visit = (file: string) => {
    if (seen.has(file)) return;
    seen.add(file);
    const code = fs.readFileSync(file, "utf8");
    for (const [, specifier] of code.matchAll(IMPORT)) {
      specifiers.push(specifier);
      if (!specifier.startsWith(".")) continue;
      const base = path.resolve(path.dirname(file), specifier);
      const resolved = [".ts", ".tsx", "/index.ts"]
        .map((suffix) => base + suffix)
        .find((candidate) => fs.existsSync(candidate));
      if (resolved !== undefined) visit(resolved);
    }
  };
  visit(entry);
  return specifiers;
}

describe("screenData.ts — чистые данные", () => {
  test("ни прямо, ни через свои импорты не тянет тестовых модулей", async () => {
    // Первый аргумент — не литерал: Vite заменяет `new URL("…", import.meta.url)` адресом
    // dev-сервера, а нужен путь на диске.
    const here = "./screenData.ts";
    const entry = new URL(here, import.meta.url).pathname;

    const specifiers = await importGraph(entry);

    expect(specifiers.length).toBeGreaterThan(0);
    const forbidden = specifiers.filter((specifier) =>
      /test-setup|fixtures|@testing-library|vitest|\.test(\.tsx?)?$/.test(
        specifier,
      ),
    );
    expect(forbidden).toEqual([]);
  });
});

// --- страница превью ---

const realWebSocket = window.WebSocket;

afterEach(() => {
  window.WebSocket = realWebSocket;
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-text-size");
});

/** Открывает превью экрана; настоящий fetch подменён шпионом: превью не должно ходить в сеть. */
function openPreview(screenName: PreviewScreen, variant: "now" | "next") {
  window.history.replaceState(
    null,
    "",
    `/?preview=screens&screen=${screenName}&variant=${variant}&theme=light`,
  );
  const network = vi.fn(() => Promise.reject(new Error("сеть недоступна")));
  vi.stubGlobal("fetch", network);
  render(<ScreensPreview />);
  return network;
}

/** Вкладки панели идут ровно в этом порядке и называются ровно так (без чисел бейджей). */
function expectTabs(bar: HTMLElement, names: string[]) {
  const named = names.map((name) => within(bar).getByRole("tab", { name }));
  expect(within(bar).getAllByRole("tab")).toEqual(named);
}

describe("страница превью экранов", () => {
  test.each(
    PREVIEW_SCREENS.flatMap((name) => [
      [name, "now"],
      [name, "next"],
    ]) as [PreviewScreen, "now" | "next"][],
  )("%s (%s): экран не пуст и обходится без сети", async (name, variant) => {
    const network = openPreview(name, variant);

    const main = await screen.findByRole("main");
    await waitFor(() =>
      expect(main.textContent?.trim().length ?? 0).toBeGreaterThan(40),
    );
    expect(network).not.toHaveBeenCalled();
  });

  test("тема из адреса применяется ко всей странице", async () => {
    openPreview("games", "now");

    await screen.findByRole("main");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  test("оглавление: ссылки на каждый экран в обоих вариантах", () => {
    window.history.replaceState(null, "", "/?preview=screens");
    render(<ScreensPreview />);

    for (const name of PREVIEW_SCREENS) {
      for (const variant of ["now", "next"]) {
        expect(
          document.querySelector(
            `a[href*="screen=${name}&variant=${variant}"]`,
          ),
          `${name} ${variant}`,
        ).not.toBeNull();
      }
    }
  });

  test("«как есть»: настоящий экран игры — вкладки сверху и лента рассказа", async () => {
    openPreview("story", "now");

    expect(
      await screen.findByRole("list", { name: "Лента" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tablist", { name: "Разделы игры" }),
    ).toBeInTheDocument();
    expect(useGame.getState().live.view?.feed.length).toBeGreaterThan(3);
  });

  test("«как будет», вариант В: вкладки внизу — Рассказ, Бой, Персонаж, Отряд, Дневник", async () => {
    openPreview("combat", "next");

    const bar = await screen.findByRole("tablist", { name: "Разделы игры" });
    expectTabs(bar, ["Рассказ", "Бой", "Персонаж", "Отряд", "Дневник"]);
  });

  test("«как будет», вариант А: текущий порядок вкладок, но внизу", async () => {
    openPreview("story-a", "next");

    const bar = await screen.findByRole("tablist", { name: "Разделы игры" });
    expectTabs(bar, ["Рассказ", "Дневник", "Персонаж", "Отряд"]);
  });

  test("бейдж не входит в имя вкладки, тост «Твой ход» — вежливый статус", async () => {
    openPreview("combat-toast", "next");

    const bar = await screen.findByRole("tablist", { name: "Разделы игры" });
    // Имена вкладок без чисел и отметок: на точные имена опираются сквозные тесты.
    expect(within(bar).getByRole("tab", { name: "Бой" })).toBeInTheDocument();
    const toast = screen.getByRole("status", { name: /Твой ход/ });
    expect(toast).toHaveTextContent("Твой ход");
  });

  test("строка статуса «как будет» показывает день и часть суток", async () => {
    openPreview("story", "next");

    expect(await screen.findByText("День 2, утро")).toBeInTheDocument();
  });

  test("«Мастер думает…»: одна формулировка, нейтральная фраза и время ожидания", async () => {
    openPreview("thinking", "next");

    const card = await screen.findByRole("status", { name: "Мастер думает…" });
    expect(card).toHaveTextContent(/Ждём \d+:\d\d/);
  });

  test("настройки: шрифт, высокий контраст и вибрация; переключателя звука нет", async () => {
    openPreview("settings", "next");

    expect(
      await screen.findByRole("radiogroup", { name: "Размер текста" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Высокий контраст" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Вибрация" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /Звук/ })).toBeNull();
  });

  // Настройки теперь настоящие: «как есть» больше не говорит, что их нет.
  test("настройки «как есть» — настоящая панель настроек приложения", async () => {
    openPreview("settings", "now");

    expect(
      await screen.findByRole("radiogroup", { name: "Размер текста" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Высокий контраст" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Настроек сейчас нет")).toBeNull();
  });

  test("стили превью не переопределяют настоящие токены размера текста и контраста", async () => {
    const css = await readSource("../preview/screens-preview.css");

    expect(css).not.toMatch(/data-text-size/);
    expect(css).not.toMatch(/data-contrast/);
  });

  test("ошибка «как будет» — единый вид с «Повторить»", async () => {
    openPreview("games-error", "next");

    expect(
      await screen.findByRole("button", { name: "Повторить" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  test("в превью нет настоящего сокета: игра приходит из подмены", async () => {
    openPreview("camp", "now");

    await screen.findByRole("tablist", { name: "Разделы игры" });
    await act(async () => {});
    expect(window.WebSocket).not.toBe(realWebSocket);
  });
});
