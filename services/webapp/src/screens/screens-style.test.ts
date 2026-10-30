import { readSource } from "../styles/read-source";

// Экраны строятся из токенов и элементов src/ui: своих цветов, шрифтов и анимаций
// в стилях экранов быть не должно (решение владельца о стиле).
const STYLE_FILES = [
  "../screens/screens.css",
  "../screens/character/character.css",
  "../screens/authoring/authoring.css",
  "../screens/journal.css",
] as const;

const withoutComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

const styles = await Promise.all(
  STYLE_FILES.map(
    async (file) => [file, withoutComments(await readSource(file))] as const,
  ),
);

describe.each(styles)("стили экранов (%s): только токены", (_file, css) => {
  test("файл не пуст", () => {
    expect(css.trim().length).toBeGreaterThan(0);
  });

  test("нет цветов, заданных напрямую", () => {
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(css).not.toMatch(/\b(rgb|rgba|hsl|hsla|oklch|lab|color-mix)\(/i);
  });

  test("цвет — только через var(--color-*) и ключевые слова", () => {
    const colorDeclarations = [
      ...css.matchAll(
        /(?:^|[;{\s])(color|background(?:-color)?|border(?:-[a-z]+)?-color|outline-color|accent-color|fill|stroke)\s*:\s*([^;}]+)/g,
      ),
    ];
    expect(colorDeclarations.length).toBeGreaterThan(0);
    for (const [, property, value] of colorDeclarations) {
      expect(value, `${property}: ${value}`).toMatch(
        /var\(--color-|transparent|inherit|currentColor|none/,
      );
    }
  });

  test("шрифты — только токены", () => {
    for (const [, value] of css.matchAll(/font-family\s*:\s*([^;}]+)/g)) {
      expect(value).toMatch(/^var\(--font-/);
    }
    expect(css).not.toMatch(/@font-face/);
  });

  test("нет собственных анимаций: движение задают классы ui-anim-*", () => {
    expect(css).not.toMatch(/@keyframes/);
    expect(css).not.toMatch(/\banimation(-name)?\s*:/);
  });

  test("нет внешних адресов", () => {
    expect(css).not.toMatch(/https?:\/\//);
  });

  test("элементы управления не меньше области нажатия", () => {
    expect(css).toMatch(/min-height:\s*var\(--tap-size\)/);
  });
});

// Строка атрибуции SRD живёт на всех экранах и обязана быть мелкой, но читаемой: кегль и цвет
// берутся из токенов (контраст приглушённого цвета с фоном проверяет styles.test.ts), а длинный
// адрес переносится и не раздвигает страницу на 360 px.
describe("строка атрибуции SRD (.app-attribution)", () => {
  const screensCss = Object.fromEntries(styles)["../screens/screens.css"] ?? "";
  const rule = /\.app-attribution\s*\{([^}]*)\}/.exec(screensCss)?.[1] ?? "";

  test("правило есть", () => {
    expect(rule.trim().length).toBeGreaterThan(0);
  });

  test("мелкий кегль и приглушённый цвет — токены", () => {
    expect(rule).toMatch(/font-size:\s*var\(--text-xs\)/);
    expect(rule).toMatch(/color:\s*var\(--color-text-muted\)/);
  });

  test("длинный адрес переносится", () => {
    expect(rule).toMatch(/overflow-wrap:\s*anywhere/);
  });
});

// Список подробностей отказа правил (ErrorAlert из fields.tsx) общий для всех форм, поэтому его
// правило лежит в общем файле экранов, а не в стилях одного способа создания героя.
describe("список подробностей отказа (.form-problems)", () => {
  const byFile = Object.fromEntries(styles);
  const ruleIn = (file: string) =>
    /\.form-problems\s*\{([^}]*)\}/.exec(byFile[file] ?? "")?.[1];

  test("правило в общем screens.css: отступы из токенов", () => {
    const rule = ruleIn("../screens/screens.css") ?? "";

    expect(rule.trim().length).toBeGreaterThan(0);
    expect(rule).toMatch(/margin:\s*var\(--space-2\)\s+0\s+0/);
    expect(rule).toMatch(/padding-left:\s*var\(--space-4\)/);
  });

  test("в стилях создания героя его нет: одно правило на всех", () => {
    expect(ruleIn("../screens/character/character.css")).toBeUndefined();
  });
});

// Плашка «Переподключаемся…» в лобби: строки статуса там нет, поэтому прилипает сама панель.
// Движения у неё нет: ни анимации, ни перехода (иначе пришлось бы гасить его при reduced motion).
describe("липкая плашка обрыва связи (.screen-sticky)", () => {
  const screensCss = Object.fromEntries(styles)["../screens/screens.css"] ?? "";
  const rule =
    /\.screen-sticky\.ui-panel\s*\{([^}]*)\}/.exec(screensCss)?.[1] ?? "";

  test("правило есть и прилипает к верху окна над лентой карточек", () => {
    expect(rule.trim().length).toBeGreaterThan(0);
    expect(rule).toMatch(/position:\s*sticky/);
    expect(rule).toMatch(/top:\s*0\s*;/);
    expect(rule).toMatch(/z-index:\s*\d+/);
  });

  test("отступы и тень — токены, движения нет", () => {
    expect(rule).toMatch(/padding:\s*var\(--space-/);
    expect(rule).toMatch(/var\(--color-shadow\)/);
    expect(rule).not.toMatch(/(?<![-\w])(transition|animation)(-[a-z]+)?\s*:/);
  });
});

// Лента для чтения вслух и поле хода над клавиатурой: правила в общем screens.css.
describe("лента и поле хода на телефоне", () => {
  const screensCss = Object.fromEntries(styles)["../screens/screens.css"] ?? "";
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return (
      new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(
        screensCss,
      )?.[1] ?? ""
    );
  };

  test("имя героя в ленте — кнопка не меньше области нажатия в обе стороны", () => {
    const body = rule(".feed-hero");
    expect(body).toMatch(/min-height:\s*var\(--tap-size\)/);
    expect(body).toMatch(/min-width:\s*var\(--tap-size\)/);
  });

  test("подчёркнуто только имя, не двоеточие; содержимое кнопки прижато к началу (без зазора перед «:»)", () => {
    expect(rule(".feed-hero__name")).toMatch(
      /text-decoration:\s*underline dotted/,
    );
    expect(rule(".feed-hero")).not.toMatch(/text-decoration:\s*underline/);
    expect(rule(".feed-hero")).not.toMatch(/justify-content:\s*center/);
  });

  test("поле хода, пока игрок пишет, липнет к верхнему краю клавиатуры", () => {
    const body = rule(".composer-dock--on");
    expect(body).toMatch(/position:\s*sticky/);
    expect(body).toMatch(/bottom:\s*var\(--keyboard-inset,\s*0px\)/);
    expect(body).toMatch(/z-index:\s*\d+/);
    // Движения нет: поле не «едет» за клавиатурой, а стоит над ней.
    expect(body).not.toMatch(/(?<![-\w])(transition|animation)(-[a-z]+)?\s*:/);
  });

  test("при «меньше движения» переход к листу героя не плавный: прокрутка задаётся в коде", () => {
    expect(screensCss).not.toMatch(/scroll-behavior:\s*smooth/);
  });
});
