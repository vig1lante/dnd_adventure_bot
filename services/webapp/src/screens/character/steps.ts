import type { AbilityMethod, CreationOptions } from "../../api/types";
import type { Draft, StepId } from "./draft";

/** Что получает каждый шаг подробного создания. */
export interface StepProps {
  draft: Draft;
  options: CreationOptions;
  /** Меняет часть черновика; выбор, ставший невозможным, зачищается сам. */
  update: (patch: Partial<Draft>) => void;
}

export const STEP_TITLES: Record<StepId, string> = {
  species: "Раса",
  class: "Класс",
  background: "Предыстория",
  abilities: "Характеристики",
  bonus: "Бонус предыстории",
  skills: "Навыки и выборы класса",
  spells: "Заклинания",
  equipment: "Снаряжение",
  persona: "Характер и внешность",
  backstory: "История героя",
  summary: "Итог",
};

export const METHOD_LABELS: Record<AbilityMethod, string> = {
  point_buy: "Покупка очков",
  standard_array: "Стандартный набор",
};
