import { readSource } from "../styles/read-source";

// Экран игры строится из токенов и элементов src/ui: своих цветов, шрифтов и анимаций
// в его стилях быть не должно (решение владельца о стиле, B8.1a).
const STYLE_FILE = "../components/game.css";

const COMPONENTS = [
  "../screens/Game.tsx",
  "../screens/Combat.tsx",
  "../screens/Camp.tsx",
  "../components/Feed.tsx",
  "../components/RollCard.tsx",
  "../components/StatusBar.tsx",
  "../components/Countdown.tsx",
  "../components/ActionComposer.tsx",
  "../components/access.ts",
  "../components/HostPanel.tsx",
  "../components/CharacterSheet.tsx",
  "../components/PartyList.tsx",
  "../components/InitiativeList.tsx",
  "../components/ZoneView.tsx",
] as const;

const withoutComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

const css = withoutComments(await readSource(STYLE_FILE));

describe("стили экрана игры: только токены", () => {
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

/** Тело правила со всеми объявлениями; правило должно быть в файле. */
function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(
    new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`),
  );
  expect(match, `правило ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}

describe("обрыв связи виден там, где играет игрок", () => {
  test("строка статуса прилипает к верху окна", () => {
    const body = ruleBody(".game-status.ui-panel");

    expect(body).toMatch(/position:\s*sticky/);
    expect(body).toMatch(/top:\s*0\s*;/);
  });

  test("плашка «Переподключаемся…» живёт в строке статуса: своего положения, движения и перехода у неё нет", () => {
    const body = ruleBody(".game-reconnecting");

    expect(body).not.toMatch(/(?<![-\w])(position|top|bottom|float)\s*:/);
    expect(body).not.toMatch(/\b(transition|animation)(-[a-z]+)?\s*:/);
  });
});

describe("тексты мастера и игроков выводятся только как текст React", () => {
  test.each(COMPONENTS)(
    "%s: нет dangerouslySetInnerHTML и innerHTML",
    async (file) => {
      const source = await readSource(file);

      expect(source).not.toMatch(
        /dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/,
      );
    },
  );

  test.each(COMPONENTS)(
    "%s: нет разбора markdown и вставки HTML библиотеками",
    async (file) => {
      const source = await readSource(file);

      expect(source).not.toMatch(
        /from ["'](marked|markdown-it|showdown|remark|dompurify|html-react-parser)/,
      );
    },
  );
});
