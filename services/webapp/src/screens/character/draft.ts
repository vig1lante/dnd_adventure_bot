import type {
  Ability,
  AbilityMethod,
  BackgroundOption,
  CharacterChoices,
  ClassOption,
  CreationOptions,
  Persona,
  Skill,
  SpeciesOption,
} from "../../api/types";

// Черновик подробного создания героя и чистые функции вокруг него. Всё здесь —
// подсказки игроку (что можно выбрать, чего ещё не хватает): правила проверяет
// сервер, числа героя считает движок. Клиент не бросает и не считает за сервер.

/** Пределы длины текстов — как в схемах сервера (characters/flavor.py); проверка там. */
export const LIMITS = {
  name: 40,
  concept: 200,
  appearance: 400,
  short: 300,
  backstory: 1500,
} as const;

export type Scores = Record<Ability, number>;
export type Assignment = Record<Ability, number | null>;

export type StepId =
  | "species"
  | "class"
  | "background"
  | "abilities"
  | "bonus"
  | "skills"
  | "spells"
  | "equipment"
  | "persona"
  | "backstory";

export interface Draft {
  species: string | null;
  classId: string | null;
  background: string | null;
  method: AbilityMethod;
  pointBuy: Scores;
  standard: Assignment;
  /** Индекс формы бонуса в `background_bonus_shapes`. */
  bonusShape: number;
  /** Характеристика для каждого числа формы бонуса, по порядку. */
  bonusPicks: (Ability | null)[];
  skills: Skill[];
  extraSkill: Skill | null;
  expertise: Skill[];
  fightingStyle: string | null;
  cantrips: string[];
  spells: string[];
  name: string;
  persona: Persona;
}

export const EMPTY_PERSONA: Persona = {
  concept: "",
  appearance: "",
  traits: "",
  ideals: "",
  bonds: "",
  flaws: "",
  backstory: "",
};

// --- поиск записей правил ---

const classOf = (draft: Draft, options: CreationOptions): ClassOption | null =>
  options.classes.find((item) => item.id === draft.classId) ?? null;

const speciesOf = (
  draft: Draft,
  options: CreationOptions,
): SpeciesOption | null =>
  options.species.find((item) => item.id === draft.species) ?? null;

const backgroundOf = (
  draft: Draft,
  options: CreationOptions,
): BackgroundOption | null =>
  options.backgrounds.find((item) => item.id === draft.background) ?? null;

const unique = <T>(items: readonly T[]): T[] => [...new Set(items)];

function filled<T>(options: CreationOptions, value: T): Record<Ability, T> {
  return Object.fromEntries(
    options.abilities.map((ability) => [ability.id, value]),
  ) as Record<Ability, T>;
}

/** Раздача, в которой ни одной характеристике ещё не отдано значение. */
export const emptyAssignment = (options: CreationOptions): Assignment =>
  filled(options, null);

// --- покупка очков ---

function pointBuyRange(options: CreationOptions) {
  const values = Object.keys(options.point_buy.costs).map(Number);
  return { min: Math.min(...values), max: Math.max(...values) };
}

const costOf = (score: number, options: CreationOptions): number | undefined =>
  options.point_buy.costs[String(score)];

export function pointBuySpent(scores: Scores, options: CreationOptions) {
  return Object.values(scores).reduce(
    (sum, score) => sum + (costOf(score, options) ?? 0),
    0,
  );
}

export function pointBuyLeft(scores: Scores, options: CreationOptions) {
  return options.point_buy.budget - pointBuySpent(scores, options);
}

/** Можно ли поднять значение: следующее есть в таблице и очков хватает. */
export function canRaise(
  scores: Scores,
  ability: Ability,
  options: CreationOptions,
): boolean {
  const current = scores[ability];
  const nextCost = costOf(current + 1, options);
  if (nextCost === undefined) return false;
  return (
    pointBuyLeft(scores, options) >= nextCost - (costOf(current, options) ?? 0)
  );
}

export function canLower(
  scores: Scores,
  ability: Ability,
  options: CreationOptions,
): boolean {
  return costOf(scores[ability] - 1, options) !== undefined;
}

// --- распределение готовых значений ---

/**
 * Значения, которые можно отдать характеристике: ещё не розданные другим и её
 * собственное. Одинаковые числа выдаются столько раз, сколько их в наборе.
 */
export function availableValues(
  values: readonly number[],
  assigned: Assignment,
  ability: Ability,
): number[] {
  const usedElsewhere = new Map<number, number>();
  for (const [other, value] of Object.entries(assigned)) {
    if (other !== ability && value !== null) {
      usedElsewhere.set(value, (usedElsewhere.get(value) ?? 0) + 1);
    }
  }
  const total = (value: number) =>
    values.filter((item) => item === value).length;
  return unique(values)
    .filter((value) => total(value) - (usedElsewhere.get(value) ?? 0) > 0)
    .sort((a, b) => b - a);
}

function assignedScores(assigned: Assignment): Scores | null {
  const entries = Object.entries(assigned);
  return entries.every(([, value]) => value !== null)
    ? (Object.fromEntries(entries) as Scores)
    : null;
}

/** Исходные значения по выбранному способу; `null` — ещё не всё распределено. */
export function baseScores(draft: Draft): Scores | null {
  switch (draft.method) {
    case "point_buy":
      return draft.pointBuy;
    case "standard_array":
      return assignedScores(draft.standard);
  }
}

// --- бонус предыстории ---

const bonusShape = (draft: Draft, options: CreationOptions): number[] =>
  options.background_bonus_shapes[draft.bonusShape] ?? [];

/**
 * Выбор не нужен, если форма — «по одному очку» и у предыстории ровно столько
 * характеристик: бонус достаётся всем. В форме «+2 и +1» выбирать нужно всегда.
 */
export function isBonusFixed(draft: Draft, options: CreationOptions): boolean {
  const shape = bonusShape(draft, options);
  const background = backgroundOf(draft, options);
  return (
    background !== null &&
    shape.length > 0 &&
    shape.every((value) => value === shape[0]) &&
    background.ability_options.length === shape.length
  );
}

/** Прибавки по характеристикам; `null` — выбор неполный. */
export function backgroundBonus(
  draft: Draft,
  options: CreationOptions,
): Partial<Record<Ability, number>> | null {
  const shape = bonusShape(draft, options);
  const bonus: Partial<Record<Ability, number>> = {};
  for (const [index, amount] of shape.entries()) {
    const pick = draft.bonusPicks[index];
    if (pick === null || pick === undefined || pick in bonus) return null;
    bonus[pick] = amount;
  }
  return shape.length === 0 ? null : bonus;
}

// --- навыки ---

/** Навыки, которые герой получает сам (предыстория и вид). */
export function grantedSkills(draft: Draft, options: CreationOptions): Skill[] {
  return unique([
    ...(backgroundOf(draft, options)?.skills ?? []),
    ...(speciesOf(draft, options)?.granted_skills ?? []),
  ]);
}

/** Все навыки с владением: данные, выбор класса и навык на выбор. */
export function proficientSkills(
  draft: Draft,
  options: CreationOptions,
): Skill[] {
  return unique([
    ...grantedSkills(draft, options),
    ...draft.skills,
    ...(draft.extraSkill === null ? [] : [draft.extraSkill]),
  ]);
}

// --- зачистка выбора после смены ---

function bonusPicksFor(
  draft: Draft,
  options: CreationOptions,
  shapeIndex: number,
): (Ability | null)[] {
  const shape = options.background_bonus_shapes[shapeIndex] ?? [];
  const allowed = backgroundOf(draft, options)?.ability_options ?? [];
  if (isBonusFixed({ ...draft, bonusShape: shapeIndex }, options)) {
    return [...allowed];
  }
  const picks: (Ability | null)[] = [];
  for (let index = 0; index < shape.length; index++) {
    const pick = draft.bonusPicks[index] ?? null;
    picks.push(
      pick !== null && allowed.includes(pick) && !picks.includes(pick)
        ? pick
        : null,
    );
  }
  return picks;
}

/**
 * Убирает из черновика выбор, который перестал быть возможным после смены вида,
 * класса или предыстории (навык класса, выданный уже предысторией; заклинание не
 * своего класса). Повторный вызов ничего не меняет.
 */
export function normalize(draft: Draft, options: CreationOptions): Draft {
  const cls = classOf(draft, options);
  const species = speciesOf(draft, options);
  const granted = grantedSkills(draft, options);

  const skills =
    cls === null
      ? []
      : unique(draft.skills)
          .filter(
            (skill) =>
              cls.skill_options.includes(skill) && !granted.includes(skill),
          )
          .slice(0, cls.skill_choices);
  const extraSkill =
    species?.extra_skill === true &&
    draft.extraSkill !== null &&
    !granted.includes(draft.extraSkill) &&
    !skills.includes(draft.extraSkill)
      ? draft.extraSkill
      : null;
  const proficient = unique([
    ...granted,
    ...skills,
    ...(extraSkill === null ? [] : [extraSkill]),
  ]);
  const expertise =
    cls !== null && cls.expertise_picks > 0
      ? unique(draft.expertise)
          .filter((skill) => proficient.includes(skill))
          .slice(0, cls.expertise_picks)
      : [];
  const fightingStyle =
    draft.fightingStyle !== null &&
    cls?.fighting_styles.includes(draft.fightingStyle)
      ? draft.fightingStyle
      : null;

  const casting = cls?.spellcasting ?? null;
  const cantrips =
    casting === null
      ? []
      : unique(draft.cantrips)
          .filter((id) =>
            casting.cantrip_options.some((spell) => spell.id === id),
          )
          .slice(0, casting.cantrips);
  const spells =
    casting === null
      ? []
      : unique(draft.spells)
          .filter((id) =>
            casting.spell_options.some((spell) => spell.id === id),
          )
          .slice(0, casting.prepared);

  const shapeIndex =
    draft.bonusShape < options.background_bonus_shapes.length
      ? draft.bonusShape
      : 0;

  return {
    ...draft,
    skills,
    extraSkill,
    expertise,
    fightingStyle,
    cantrips,
    spells,
    bonusShape: shapeIndex,
    bonusPicks: bonusPicksFor(draft, options, shapeIndex),
  };
}

/** Пустой черновик: ничего не выбрано, очки куплены на нули. */
export function createDraft(options: CreationOptions): Draft {
  return normalize(
    {
      species: null,
      classId: null,
      background: null,
      method: "point_buy",
      pointBuy: filled(options, pointBuyRange(options).min),
      standard: emptyAssignment(options),
      bonusShape: 0,
      bonusPicks: [],
      skills: [],
      extraSkill: null,
      expertise: [],
      fightingStyle: null,
      cantrips: [],
      spells: [],
      name: "",
      persona: EMPTY_PERSONA,
    },
    options,
  );
}

/**
 * Черновик по выбору, сохранённому сервером («Изменить героя»): форма открывается заполненной
 * прежним выбором. Значения лежат только в том способе, которым их задали. Выбор, которого
 * правила уже не допускают, зачищается как при смене класса: сервер проверит остальное.
 */
export function draftFromChoices(
  choices: CharacterChoices,
  options: CreationOptions,
): Draft {
  const empty = createDraft(options);
  // Прибавки по убыванию: так они стоят в форме бонуса («+2, +1»), по ним же определяется форма.
  const bonus = (
    Object.entries(choices.background_bonus) as [Ability, number][]
  ).sort(([, a], [, b]) => b - a);
  const amounts = bonus.map(([, amount]) => amount);
  const shapeIndex = options.background_bonus_shapes.findIndex(
    (shape) =>
      shape.length === amounts.length &&
      shape.every((amount, index) => amount === amounts[index]),
  );
  return normalize(
    {
      ...empty,
      species: choices.species,
      classId: choices.class_id,
      background: choices.background,
      method: choices.method,
      ...(choices.method === "standard_array"
        ? { standard: choices.base_scores }
        : { pointBuy: { ...empty.pointBuy, ...choices.base_scores } }),
      bonusShape: Math.max(shapeIndex, 0),
      bonusPicks: bonus.map(([ability]) => ability),
      skills: choices.skills,
      extraSkill: choices.extra_skill ?? null,
      expertise: choices.expertise ?? [],
      fightingStyle: choices.fighting_style ?? null,
      cantrips: choices.cantrips ?? [],
      spells: choices.prepared_spells ?? [],
      name: choices.name,
      persona: { ...EMPTY_PERSONA, ...choices.persona },
    },
    options,
  );
}

// --- шаги ---

export function stepsOf(draft: Draft, options: CreationOptions): StepId[] {
  const casting = classOf(draft, options)?.spellcasting ?? null;
  const casts = casting !== null && casting.cantrips + casting.prepared > 0;
  return [
    "species",
    "class",
    "background",
    "abilities",
    "bonus",
    "skills",
    ...(casts ? (["spells"] as const) : []),
    "equipment",
    "persona",
    "backstory",
  ];
}

/** Хватает ли выбора на шаге, чтобы идти дальше; полную проверку делает сервер. */
export function stepComplete(
  step: StepId,
  draft: Draft,
  options: CreationOptions,
): boolean {
  switch (step) {
    case "species":
      return speciesOf(draft, options) !== null;
    case "class":
      return classOf(draft, options) !== null;
    case "background":
      return backgroundOf(draft, options) !== null;
    case "abilities":
      return baseScores(draft) !== null;
    case "bonus":
      return backgroundBonus(draft, options) !== null;
    case "skills": {
      const cls = classOf(draft, options);
      const species = speciesOf(draft, options);
      if (cls === null || species === null) return false;
      return (
        draft.skills.length === cls.skill_choices &&
        (!species.extra_skill || draft.extraSkill !== null) &&
        draft.expertise.length === cls.expertise_picks &&
        (cls.fighting_styles.length === 0 || draft.fightingStyle !== null)
      );
    }
    case "spells": {
      const casting = classOf(draft, options)?.spellcasting ?? null;
      return (
        casting === null ||
        (draft.cantrips.length === casting.cantrips &&
          draft.spells.length === casting.prepared)
      );
    }
    case "persona":
      return draft.name.trim() !== "";
    case "equipment":
    case "backstory":
      return true;
  }
}

// --- выбор для сервера ---

function filledPersona(persona: Persona): Partial<Persona> {
  const result: Partial<Persona> = {};
  for (const key of Object.keys(persona) as (keyof Persona)[]) {
    const text = persona[key].trim();
    if (text !== "") result[key] = text;
  }
  return result;
}

/** Выбор игрока для `mode: detailed`; `null`, пока что-то не выбрано. */
export function toChoices(
  draft: Draft,
  options: CreationOptions,
): CharacterChoices | null {
  const cls = classOf(draft, options);
  const species = speciesOf(draft, options);
  const scores = baseScores(draft);
  const bonus = backgroundBonus(draft, options);
  const complete = stepsOf(draft, options).every((step) =>
    stepComplete(step, draft, options),
  );
  if (!complete || cls === null || species === null || scores === null) {
    return null;
  }
  if (bonus === null || draft.background === null) return null;
  return {
    name: draft.name.trim(),
    species: species.id,
    class_id: cls.id,
    background: draft.background,
    method: draft.method,
    base_scores: scores,
    background_bonus: bonus,
    skills: draft.skills,
    extra_skill: species.extra_skill ? draft.extraSkill : null,
    expertise: draft.expertise,
    fighting_style: cls.fighting_styles.length > 0 ? draft.fightingStyle : null,
    cantrips: draft.cantrips,
    prepared_spells: draft.spells,
    persona: filledPersona(draft.persona),
  };
}
