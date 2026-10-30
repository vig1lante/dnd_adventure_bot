import type { Locator, Page } from "@playwright/test";
import { expect } from "@playwright/test";

// Выбор в каруселях создания героя: раса, класс и предыстория листаются подробными карточками.
// Сквозные тесты выбирают так же, как игрок: точкой к варианту, затем «Выбрать» на его карточке.
// Точка карусели названа вариантом, поэтому помощнику нужны названия.

// Названия классов и рас SRD, какими их отдаёт сервер (`GET /api/content/creation-options`).
const CLASS_NAMES: Readonly<Record<string, string>> = {
  barbarian: "Варвар",
  fighter: "Воин",
  rogue: "Плут",
  ranger: "Следопыт",
  cleric: "Жрец",
  wizard: "Волшебник",
};

const SPECIES_NAMES: Readonly<Record<string, string>> = {
  dragonborn: "Драконорождённый",
  dwarf: "Дварф",
  elf: "Эльф",
  gnome: "Гном",
  goliath: "Голиаф",
  halfling: "Полурослик",
  human: "Человек",
  orc: "Орк",
  tiefling: "Тифлинг",
};

function nameIn(
  table: Readonly<Record<string, string>>,
  id: string,
  what: string,
): string {
  const name = table[id];
  if (name === undefined) throw new Error(`неизвестный ${what}: ${id}`);
  return name;
}

/** Название класса по id. */
export const className = (id: string) => nameIn(CLASS_NAMES, id, "класс");

/** Название расы по id. */
export const speciesName = (id: string) => nameIn(SPECIES_NAMES, id, "раса");

/**
 * Карусель по имени: «Раса», «Класс», «Предыстория». Имя сверяется целиком: иначе «Класс»
 * совпал бы с группой «Навыки класса: выберите N».
 */
export const carousel = (page: Page, label: string): Locator =>
  page.getByRole("group", { name: label, exact: true });

/** Подробная карточка варианта, показанная в карусели (на экране всегда одна). */
export const shownCard = (group: Locator): Locator =>
  group.getByRole("article");

/** Карусель показывает выбранный вариант: его карточку с нажатой «Выбрано». */
export async function expectPicked(
  page: Page,
  label: string,
  option: string,
): Promise<void> {
  const group = carousel(page, label);
  await expect(shownCard(group)).toHaveAccessibleName(option);
  await expect(
    group.getByRole("button", { name: "Выбрано", pressed: true }),
  ).toBeVisible();
}

/** Выбор в карусели: точкой к варианту, затем «Выбрать» на его карточке. */
export async function pickInCarousel(
  page: Page,
  label: string,
  option: string,
): Promise<void> {
  const group = carousel(page, label);
  await group.getByRole("button", { name: option, exact: true }).click();
  await expect(shownCard(group)).toHaveAccessibleName(option);
  await group.getByRole("button", { name: /^Выбра(ть|но)$/ }).click();
  await expectPicked(page, label, option);
}

/** Быстрый герой: класс и раса каруселями формы «Быстро». */
export async function pickQuickHero(
  page: Page,
  hero: { classId: string; species: string },
): Promise<void> {
  await pickInCarousel(page, "Класс", className(hero.classId));
  await pickInCarousel(page, "Раса", speciesName(hero.species));
}
