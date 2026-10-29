import type { AuthoringStep } from "../../api/types";

/**
 * Экраны-шаги редактора по порядку генератора; каждый — шаг генерации, его статус и замок
 * `step:<шаг>`. «Карта» и «Наполнение» правят один раздел `locations`, но шаги у них разные
 * (дополнение B5, §2).
 */
export const EDITOR_STEPS = [
  { step: "concept", label: "Замысел" },
  { step: "world", label: "Мир" },
  { step: "pantheon", label: "Боги" },
  { step: "factions", label: "Фракции" },
  { step: "plot", label: "Сюжет" },
  { step: "locations", label: "Карта" },
  { step: "location_contents", label: "Наполнение" },
  { step: "npcs", label: "Персонажи" },
  { step: "relations", label: "Связи" },
  { step: "encounters", label: "Бои и боссы" },
  { step: "items", label: "Предметы и награды" },
  { step: "clues", label: "Улики" },
] as const satisfies readonly { step: AuthoringStep; label: string }[];

/** Экраны после шагов: у проверки и публикации нет ни статуса, ни замка. */
export const FINAL_SCREENS = [
  { screen: "review", label: "Проверка" },
  { screen: "publish", label: "Публикация" },
] as const;

export type EditorStep = (typeof EDITOR_STEPS)[number]["step"];

/** Экран редактора: шаг или проверка и публикация. */
export type EditorScreen =
  EditorStep | (typeof FINAL_SCREENS)[number]["screen"];

/** Название шага для людей. */
export const stepLabel = (step: AuthoringStep): string =>
  EDITOR_STEPS.find((item) => item.step === step)?.label ?? step;

/** Название любого экрана редактора, в том числе проверки и публикации. */
export const screenLabel = (screen: EditorScreen): string =>
  FINAL_SCREENS.find((item) => item.screen === screen)?.label ??
  stepLabel(screen as AuthoringStep);
