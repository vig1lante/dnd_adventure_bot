import indexHtml from "../../index.html?raw";
import packageJson from "../../package.json";
import { readSource } from "./read-source";

const tokensCss = await readSource("./tokens.css");
const themeCss = await readSource("./theme.css");
const fontsCss = await readSource("./fonts.css");
const globalCss = await readSource("./global.css");
const uiCss = await readSource("./ui.css");

// --- разбор CSS: ровно столько, сколько нужно проверкам ---

const withoutComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** Объявления `--имя: значение;` из тела правила. */
function customProperties(body: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const [, name, value] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    result.set(name, value.trim());
  }
  return result;
}

function ruleBody(css: string, selector: RegExp): string {
  const match = withoutComments(css).match(selector);
  if (match === null) throw new Error(`Нет правила ${String(selector)}`);
  return match[1];
}

const darkTokens = customProperties(
  ruleBody(tokensCss, /:root,\s*\[data-theme="dark"\]\s*\{([^}]*)\}/),
);
const lightTokens = customProperties(
  ruleBody(tokensCss, /\[data-theme="light"\]\s*\{([^}]*)\}/),
);
// Запасной вариант без data-theme: до запуска JS и если тему не выбрали.
const systemLightTokens = customProperties(
  ruleBody(
    tokensCss,
    /@media \(prefers-color-scheme: light\)\s*\{\s*:root:not\(\[data-theme\]\)\s*\{([^}]*)\}/,
  ),
);

// --- контраст по WCAG 2.x ---

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel(n >> 16) +
    0.7152 * channel((n >> 8) & 255) +
    0.0722 * channel(n & 255)
  );
}

function contrast(foreground: string, background: string): number {
  const [light, dark] = [luminance(foreground), luminance(background)].sort(
    (a, b) => b - a,
  );
  return (light + 0.05) / (dark + 0.05);
}

const SURFACES = [
  "--color-bg",
  "--color-surface",
  "--color-surface-alt",
  "--color-surface-sunken",
] as const;

/** Пары «текст — фон» (WCAG AA, обычный текст: не ниже 4.5:1). */
const TEXT_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ...SURFACES.map((bg) => ["--color-text", bg] as const),
  ...SURFACES.map((bg) => ["--color-text-muted", bg] as const),
  ["--color-text", "--color-header"],
  ["--color-heading", "--color-bg"],
  ["--color-heading", "--color-surface"],
  ["--color-heading", "--color-header"],
  ["--color-accent-text", "--color-bg"],
  ["--color-accent-text", "--color-surface"],
  ["--color-seal-text", "--color-bg"],
  ["--color-seal-text", "--color-surface"],
  // Нижняя панель вкладок, день в строке статуса и тост (B10.2).
  ["--color-text-muted", "--color-header"],
  ["--color-accent-text", "--color-header"],
  ["--color-accent-text", "--color-surface-sunken"],
  ["--color-text", "--color-surface-raised"],
  ["--color-text-muted", "--color-surface-raised"],
  ["--color-heading", "--color-surface-raised"],
  ["--color-on-accent", "--color-accent"],
  ["--color-on-cloth", "--color-cloth"],
  ["--color-on-seal", "--color-seal"],
  ...[1, 2, 3, 4, 5, 6].map(
    (n) => ["--color-on-tone", `--color-tone-${n}`] as const,
  ),
];

/** Не текст: фокус, границы кнопок и полоса здоровья (WCAG 1.4.11: не ниже 3:1). */
const NON_TEXT_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ["--color-focus", "--color-bg"],
  ["--color-focus", "--color-surface"],
  ["--color-border-strong", "--color-bg"],
  ["--color-border-strong", "--color-surface"],
  ["--color-hp-high", "--color-hp-track"],
  ["--color-hp-mid", "--color-hp-track"],
  ["--color-hp-low", "--color-hp-track"],
];

const THEMES = [
  ["тёмная", darkTokens],
  ["светлая", lightTokens],
] as const;

describe("токены цвета", () => {
  test("в обеих темах одинаковый состав", () => {
    expect([...lightTokens.keys()].sort()).toEqual(
      [...darkTokens.keys()].sort(),
    );
  });

  test("запасной вариант без data-theme совпадает со светлой темой", () => {
    expect(Object.fromEntries(systemLightTokens)).toEqual(
      Object.fromEntries(lightTokens),
    );
  });

  test.each(THEMES)(
    "%s тема: пары цветов заданы шестизначным hex",
    (_, tokens) => {
      const names = new Set([...TEXT_PAIRS, ...NON_TEXT_PAIRS].flat());
      for (const name of names) {
        expect(tokens.get(name), name).toMatch(/^#[0-9a-f]{6}$/i);
      }
    },
  );

  test("тёмная тема темнее светлой: фон и текст не перепутаны", () => {
    expect(luminance(darkTokens.get("--color-bg")!)).toBeLessThan(
      luminance(lightTokens.get("--color-bg")!),
    );
    expect(luminance(darkTokens.get("--color-text")!)).toBeGreaterThan(
      luminance(lightTokens.get("--color-text")!),
    );
  });

  test.each(THEMES)(
    "%s тема: текст читается (контраст ≥ 4.5:1)",
    (_, tokens) => {
      for (const [foreground, background] of TEXT_PAIRS) {
        const ratio = contrast(
          tokens.get(foreground)!,
          tokens.get(background)!,
        );
        expect(ratio, `${foreground} на ${background}`).toBeGreaterThanOrEqual(
          4.5,
        );
      }
    },
  );

  test.each(THEMES)(
    "%s тема: фокус, границы и полоса здоровья различимы (≥ 3:1)",
    (_, tokens) => {
      for (const [foreground, background] of NON_TEXT_PAIRS) {
        const ratio = contrast(
          tokens.get(foreground)!,
          tokens.get(background)!,
        );
        expect(ratio, `${foreground} на ${background}`).toBeGreaterThanOrEqual(
          3,
        );
      }
    },
  );

  test("тема подсказывает браузеру color-scheme", () => {
    const css = withoutComments(tokensCss);
    expect(css).toMatch(/color-scheme:\s*dark/);
    expect(css).toMatch(/color-scheme:\s*light/);
  });
});

// --- настройки интерфейса (B10.3): размер текста и высокий контраст — атрибуты на корне ---

/** Тело правила с ровно таким списком селекторов (пробелы не важны). */
function ruleOf(css: string, selectors: readonly string[]): string {
  const escaped = selectors
    .map((selector) => selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(",\\s*");
  return ruleBody(css, new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`));
}

const highContrast = {
  тёмная: customProperties(
    ruleOf(tokensCss, [
      '[data-contrast="high"][data-theme="dark"]',
      '[data-contrast="high"] [data-theme="dark"]',
    ]),
  ),
  светлая: customProperties(
    ruleOf(tokensCss, [
      '[data-contrast="high"][data-theme="light"]',
      '[data-contrast="high"] [data-theme="light"]',
    ]),
  ),
};

/** Палитра темы с включённым высоким контрастом: переопределения поверх обычных пар. */
const withHighContrast = (theme: keyof typeof highContrast) =>
  new Map([
    ...(theme === "тёмная" ? darkTokens : lightTokens),
    ...highContrast[theme],
  ]);

const ratioIn = (tokens: Map<string, string>, [fg, bg]: readonly string[]) =>
  contrast(tokens.get(fg)!, tokens.get(bg)!);

const READING_SURFACES = [
  ...SURFACES,
  "--color-header",
  "--color-surface-raised",
] as const;

/** Текст на фонах: при высоком контрасте — не ниже 7:1 (WCAG AAA). */
const HIGH_TEXT_PAIRS = [
  "--color-text",
  "--color-text-muted",
  "--color-heading",
  "--color-accent-text",
  "--color-seal-text",
].flatMap((fg) => READING_SURFACES.map((bg) => [fg, bg] as const));

const HIGH_THEMES = [
  ["тёмная", darkTokens],
  ["светлая", lightTokens],
] as const;

describe("высокий контраст (data-contrast=high)", () => {
  test("обе темы переопределяют один и тот же набор токенов", () => {
    expect([...highContrast.светлая.keys()].sort()).toEqual(
      [...highContrast.тёмная.keys()].sort(),
    );
    expect(highContrast.тёмная.size).toBeGreaterThan(0);
  });

  test.each(HIGH_THEMES)(
    "%s тема: меняются только текст, линии и фокус — фоны и акценты прежние (третьей палитры нет)",
    (theme) => {
      for (const [name, value] of highContrast[theme]) {
        expect(name).toMatch(
          /^--color-(text|heading|accent-text|seal-text|border|focus)/,
        );
        expect(value, name).toMatch(/^#[0-9a-f]{6}$/i);
      }
    },
  );

  test.each(HIGH_THEMES)(
    "%s тема: ни одна пара не стала слабее, а переопределённые — сильнее",
    (theme, tokens) => {
      const high = withHighContrast(theme);
      for (const pair of [...TEXT_PAIRS, ...NON_TEXT_PAIRS]) {
        const before = ratioIn(tokens, pair);
        const after = ratioIn(high, pair);
        expect(after, pair.join(" на ")).toBeGreaterThanOrEqual(before);
        if (highContrast[theme].has(pair[0])) {
          expect(after, pair.join(" на ")).toBeGreaterThan(before);
        }
      }
    },
  );

  test.each(HIGH_THEMES)(
    "%s тема: текст на любом фоне — не ниже 7:1 (WCAG AAA)",
    (theme) => {
      const high = withHighContrast(theme);
      for (const pair of HIGH_TEXT_PAIRS) {
        expect(ratioIn(high, pair), pair.join(" на ")).toBeGreaterThanOrEqual(
          7,
        );
      }
    },
  );

  test.each(HIGH_THEMES)(
    "%s тема: тонкие линии видны (≥ 3:1), контуры и фокус — ≥ 4.5:1",
    (theme) => {
      const high = withHighContrast(theme);
      for (const bg of READING_SURFACES) {
        expect(
          ratioIn(high, ["--color-border", bg]),
          bg,
        ).toBeGreaterThanOrEqual(3);
        for (const fg of ["--color-border-strong", "--color-focus"]) {
          expect(
            ratioIn(high, [fg, bg]),
            `${fg} на ${bg}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    },
  );
});

/** Число rem из значения токена кегля. */
const rem = (value: string | undefined) =>
  Number(/^([\d.]+)rem$/.exec(value ?? "")?.[1]);

describe("размер текста (data-text-size)", () => {
  const base = customProperties(ruleBody(themeCss, /:root\s*\{([^}]*)\}/));
  const large = customProperties(
    ruleOf(themeCss, [':root[data-text-size="large"]']),
  );
  const huge = customProperties(
    ruleOf(themeCss, [':root[data-text-size="huge"]']),
  );

  test("«Крупный» и «Очень крупный» меняют одни и те же токены --text-*, включая базу и рассказ", () => {
    expect([...large.keys()].sort()).toEqual([...huge.keys()].sort());
    expect([...large.keys()]).toEqual(
      expect.arrayContaining(["--text-base", "--text-narration"]),
    );
    for (const name of large.keys()) expect(name).toMatch(/^--text-/);
  });

  test("каждая ступень крупнее предыдущей", () => {
    for (const name of large.keys()) {
      expect(rem(large.get(name)), name).toBeGreaterThan(rem(base.get(name)));
      expect(rem(huge.get(name)), name).toBeGreaterThan(rem(large.get(name)));
    }
  });

  test("область нажатия от размера текста не зависит: --tap-size не переопределён", () => {
    expect(large.has("--tap-size")).toBe(false);
    expect(huge.has("--tap-size")).toBe(false);
  });
});

describe("шрифты", () => {
  const faces = [
    ...withoutComments(fontsCss).matchAll(/@font-face\s*\{([^}]*)\}/g),
  ]
    .map(([, body]) => body)
    .map((body) => {
      const property = (name: string) =>
        body
          .match(new RegExp(`${name}:\\s*([^;]+);`))?.[1]
          .replace(/\s+/g, " ")
          .trim() ?? "";
      return {
        family: property("font-family").replaceAll('"', ""),
        weight: property("font-weight"),
        style: property("font-style"),
        display: property("font-display"),
        range: property("unicode-range"),
        src: property("src"),
      };
    });

  // Пути к woff2 в установленных пакетах @fontsource.
  const installed = Object.keys(
    import.meta.glob("../../node_modules/@fontsource/*/files/*.woff2", {
      query: "?url",
    }),
  );

  test("заголовки и текст: Cormorant Garamond и Alegreya", () => {
    expect([...new Set(faces.map((face) => face.family))].sort()).toEqual([
      "Alegreya",
      "Cormorant Garamond",
    ]);
    expect(Object.keys(packageJson.dependencies)).toEqual(
      expect.arrayContaining([
        "@fontsource/alegreya",
        "@fontsource/cormorant-garamond",
      ]),
    );
  });

  test("у каждого начертания есть кириллица и латиница", () => {
    const variants = new Set(
      faces.map((face) => `${face.family}|${face.weight}|${face.style}`),
    );
    expect(variants.size).toBeGreaterThanOrEqual(4);
    for (const variant of variants) {
      const [family, weight, style] = variant.split("|");
      const ranges = faces
        .filter(
          (f) =>
            f.family === family && f.weight === weight && f.style === style,
        )
        .map((face) => face.range);
      expect(
        ranges.some((r) => r.includes("U+0400-045F")),
        variant,
      ).toBe(true);
      expect(
        ranges.some((r) => r.includes("U+0000-00FF")),
        variant,
      ).toBe(true);
    }
  });

  test("шрифты подгружаются с подменой (swap) и только в woff2", () => {
    for (const face of faces) {
      expect(face.display).toBe("swap");
      expect(face.src).toMatch(
        /^url\("@fontsource\/[^"]+\.woff2"\) format\("woff2"\)$/,
      );
    }
  });

  test("файлы шрифтов лежат в установленных пакетах", () => {
    for (const face of faces) {
      const path = face.src.match(/url\("([^"]+)"\)/)![1];
      expect(
        installed.some((file) => file.endsWith(`/${path}`)),
        path,
      ).toBe(true);
    }
  });
});

describe("без внешних запросов во время работы", () => {
  const sources = {
    "index.html": indexHtml,
    "tokens.css": tokensCss,
    "theme.css": themeCss,
    "fonts.css": fontsCss,
    "global.css": globalCss,
    "ui.css": uiCss,
  };

  test.each(Object.entries(sources))("%s: нет адресов http(s)", (_, text) => {
    // xmlns встроенных SVG — не запрос.
    const withoutNamespaces = text.replaceAll("http://www.w3.org/2000/svg", "");
    expect(withoutNamespaces).not.toMatch(/https?:\/\//);
    expect(withoutNamespaces).not.toMatch(/fonts\.(googleapis|gstatic)/);
  });

  test("страница рассчитана на мобильный экран и вырез (safe area)", () => {
    expect(indexHtml).toMatch(/viewport-fit=cover/);
  });
});

describe("движение", () => {
  test("при prefers-reduced-motion анимации и переходы выключены", () => {
    const css = withoutComments(globalCss);
    const block = css.match(
      /@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*)\}\s*$/m,
    );
    expect(block?.[1]).toMatch(/animation:\s*none\s*!important/);
    expect(block?.[1]).toMatch(/transition:\s*none\s*!important/);
  });

  test("анимацию задают только классы ui-anim-*: их же снимает JS при reduced motion", () => {
    const rules = [globalCss, uiCss]
      .map(withoutComments)
      // keyframes и блок reduced motion содержат вложенные скобки
      .map((css) =>
        css
          .replace(/@keyframes[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "")
          .replace(
            /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*\}\s*$/m,
            "",
          ),
      )
      .join("\n");
    const animated = [...rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .filter(([, , body]) => /\banimation(-name)?\s*:/.test(body))
      .map(([, selector]) => selector.trim());

    expect(animated.length).toBeGreaterThan(0);
    for (const selector of animated) {
      expect(selector, selector).toMatch(/^\.ui-anim-[\w-]+/);
    }
  });
});

describe("мобильная вёрстка", () => {
  const ui = withoutComments(uiCss);
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return (
      ui.match(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`))?.[1] ??
      ""
    );
  };

  test("вкладка нижней панели не меньше области нажатия, панель учитывает вырез снизу", () => {
    expect(rule(".ui-tabbar__tab")).toMatch(/min-width:\s*var\(--tap-size\)/);
    expect(rule(".ui-tabbar__tab")).toMatch(/min-height:\s*3\.5rem/);
    expect(rule(".ui-tabbar")).toMatch(/position:\s*fixed/);
    expect(rule(".ui-tabbar")).toMatch(/env\(safe-area-inset-bottom/);
  });

  test("под нижней панелью страница оставляет место: последний блок не прячется за ней", () => {
    expect(ui).toMatch(
      /\.app:has\(\.ui-tabbar:not\(\[hidden\]\)\)\s*\{[^}]*padding-bottom:/,
    );
  });

  test("настройки: вариант размера текста и переключатель — не меньше области нажатия", () => {
    expect(rule(".ui-settings__size-option")).toMatch(
      /min-height:\s*var\(--tap-size\)/,
    );
    expect(rule(".ui-settings__switch")).toMatch(
      /min-height:\s*var\(--tap-size\)/,
    );
    expect(rule(".ui-settings-button.ui-button")).toMatch(
      /width:\s*var\(--tap-size\)/,
    );
  });

  test("«Скрыть» у тоста — квадрат области нажатия: тексту уведомления остаётся больше места", () => {
    const close = rule(".ui-toast__close.ui-button");
    expect(close).toMatch(/width:\s*var\(--tap-size\)/);
    expect(close).toMatch(/padding:\s*0\s*;/);
    // Высота — от общей кнопки (.ui-button: min-height var(--tap-size)), её не уменьшаем.
    expect(close).not.toMatch(/(?:min-)?height:/);
  });

  test("бейдж вкладки не перехватывает нажатия", () => {
    expect(rule(".ui-tabbar__badge")).toMatch(/pointer-events:\s*none/);
  });

  test("область нажатия не меньше 44 px", () => {
    const tap = themeCss.match(/--tap-size:\s*([\d.]+)(px|rem)/);
    const pixels = Number(tap?.[1]) * (tap?.[2] === "rem" ? 16 : 1);
    expect(pixels).toBeGreaterThanOrEqual(44);
    expect(withoutComments(uiCss)).toMatch(
      /\.ui-button\s*\{[^}]*min-height:\s*var\(--tap-size\)/,
    );
  });
});
