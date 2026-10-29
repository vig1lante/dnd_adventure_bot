import { readSource } from "../../styles/read-source";

// Конструктор на узком экране (B10.4, макет «как будет»): все шаги видны компактными плашками,
// которые переносятся на новые строки, а не уезжают вбок; рамка редактора тоньше, поля шире.
// Измерение в настоящем браузере на 360–430 px — e2e (expectFitsPhone в authoring.spec.ts).

const css = (await readSource("../screens/authoring/authoring.css")).replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

/** Тело правила с ровно этим селектором. */
const rule = (selector: string) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? ""
  );
};

describe("полоска шагов на телефоне", () => {
  test("плашки переносятся на новые строки, полоска не прокручивается вбок", () => {
    const steps = rule(".authoring-steps");

    expect(steps).toMatch(/display:\s*flex/);
    expect(steps).toMatch(/flex-wrap:\s*wrap/);
    expect(steps).not.toMatch(/overflow-x:\s*auto/);
  });

  test("плашка: подпись и бейджи в строку, бейджи переносятся внутри плашки; область нажатия — токен", () => {
    const step = rule(".authoring-step");

    expect(step).toMatch(/flex-direction:\s*row/);
    expect(step).toMatch(/flex-wrap:\s*wrap/);
    expect(step).toMatch(/min-height:\s*var\(--tap-size\)/);
    expect(step).toMatch(/min-width:\s*var\(--tap-size\)/);
    expect(step).toMatch(/max-width:\s*100%/);
  });

  test("подпись шага — мелкий кегль из токенов и переносится", () => {
    const label = rule(".authoring-step__label");

    expect(label).toMatch(/font-size:\s*var\(--text-sm\)/);
    expect(label).toMatch(/overflow-wrap:\s*anywhere/);
  });

  test("текущий шаг виден не только рамкой, фокус с клавиатуры — заметный контур", () => {
    expect(rule(".authoring-step--current")).toMatch(
      /background:\s*var\(--color-/,
    );
    // Общий контур фокуса (как :focus-visible в global.css): его усиливает «Высокий контраст».
    expect(rule(".authoring-step:focus-visible")).toMatch(
      /outline:\s*3px solid var\(--color-focus\)/,
    );
  });
});

describe("рамка редактора на узком экране", () => {
  test("внешняя карточка — узкие поля по бокам, вложенные карточки — поля тоньше обычных", () => {
    expect(rule(".ui-card.authoring-card")).toMatch(
      /padding-inline:\s*var\(--space-2\)/,
    );
    expect(rule(".authoring-card .ui-card")).toMatch(
      /padding:\s*var\(--space-3\)/,
    );
  });

  test("сущность, к которой ведёт ссылка находки, видна в фокусе", () => {
    expect(rule(".authoring-entity:focus")).toMatch(
      /outline:\s*3px solid var\(--color-focus\)/,
    );
  });
});
