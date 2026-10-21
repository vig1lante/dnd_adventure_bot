import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

// Мобильная вёрстка (план B8.1a): основная ширина 360–430 px, область нажатия не меньше 44 px,
// горизонтальной прокрутки нет. Юнит-тесты проверяют только, что в стилях есть нужные правила
// (`min-height: var(--tap-size)`); здесь страница измеряется в настоящем браузере.

export const PHONE_WIDTHS = [360, 390, 430] as const;

/** Меньше этой стороны нажимаемый элемент считается слишком мелким для пальца. */
export const MIN_TAP_PX = 44;

// Измерение выполняется внутри браузера. Оно записано обычным JavaScript в строке: сборка
// e2e идёт с типами Node без DOM (tsconfig.node.json), и ссылки на document здесь не прошли бы.
const MEASURE = `(minTap) => {
  const problems = [];
  const root = document.documentElement;
  if (root.scrollWidth > root.clientWidth) {
    problems.push("горизонтальная прокрутка страницы: " + root.scrollWidth + " > " + root.clientWidth);
  }
  const visible = (element) => {
    if (element.closest("[hidden], [aria-hidden='true']") !== null) return null;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden") return null;
    const box = element.getBoundingClientRect();
    return box.width === 0 || box.height === 0 ? null : box;
  };
  const describe = (element) => {
    const text = (element.textContent || "").trim().slice(0, 30);
    return "<" + element.tagName.toLowerCase() + "> «" + text + "»";
  };
  // Ничего не выходит за правый край окна, даже если прокрутку скрыли стилем.
  for (const element of document.querySelectorAll("main *")) {
    const box = visible(element);
    if (box !== null && box.right > root.clientWidth + 1) {
      problems.push("за правым краем: " + describe(element) + " (" + Math.round(box.right) + ")");
    }
  }
  // Область нажатия: у радио и флажка это вся карточка-подпись, у остальных — сам элемент.
  const controls = document.querySelectorAll("button, [role='tab'], select, textarea, a[href], input");
  for (const control of controls) {
    const isChoice = control instanceof HTMLInputElement && (control.type === "radio" || control.type === "checkbox");
    const target = isChoice ? (control.closest("label") || control) : control;
    const box = visible(target);
    if (box === null) continue;
    if (box.height < minTap - 0.5) {
      problems.push("низкая область нажатия: " + describe(target) + " (" + Math.round(box.height) + " px)");
    }
    // Кнопки и вкладки: и узкая область нажатия промахивается мимо пальца.
    if (target.matches("button, [role='tab']") && box.width < minTap - 0.5) {
      problems.push("узкая область нажатия: " + describe(target) + " (" + Math.round(box.width) + " px)");
    }
  }
  return problems;
}`;

/** Измеряет страницу: что выходит за край окна и какие нажимаемые элементы ниже `minTap`. */
export async function layoutProblems(
  page: Page,
  minTap = MIN_TAP_PX,
): Promise<string[]> {
  return (await page.evaluate(`(${MEASURE})(${minTap})`)) as string[];
}

/** Страница помещается на телефоне любой из основных ширин: без выхода за край и мелких кнопок. */
export async function expectFitsPhone(
  page: Page,
  screenName: string,
): Promise<void> {
  const original = page.viewportSize();
  try {
    for (const width of PHONE_WIDTHS) {
      await page.setViewportSize({ width, height: original?.height ?? 844 });
      expect(
        await layoutProblems(page),
        `${screenName}: ширина ${width} px`,
      ).toEqual([]);
    }
  } finally {
    if (original !== null) await page.setViewportSize(original);
  }
}
