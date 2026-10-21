// Данные и помощники ТОЛЬКО для тестов экранов создания героя. Правила здесь
// урезаны до нужного тестам: настоящие списки отдаёт сервер (creation-options).
import type {
  Character,
  CharacterChoices,
  ClassOption,
  CreationOptions,
  MyCharacter,
  QuickChoices,
  SkillOption,
  SpeciesOption,
} from "../../api/types";

const skill = (
  id: SkillOption["id"],
  name: string,
  ability: SkillOption["ability"],
): SkillOption => ({ id, name, ability });

const ALL_SKILLS: SkillOption[] = [
  skill("athletics", "Атлетика", "str"),
  skill("acrobatics", "Акробатика", "dex"),
  skill("sleight_of_hand", "Ловкость рук", "dex"),
  skill("stealth", "Скрытность", "dex"),
  skill("arcana", "Магия", "int"),
  skill("history", "История", "int"),
  skill("investigation", "Анализ", "int"),
  skill("nature", "Природа", "int"),
  skill("religion", "Религия", "int"),
  skill("animal_handling", "Уход за животными", "wis"),
  skill("insight", "Проницательность", "wis"),
  skill("medicine", "Медицина", "wis"),
  skill("perception", "Внимательность", "wis"),
  skill("survival", "Выживание", "wis"),
  skill("deception", "Обман", "cha"),
  skill("intimidation", "Запугивание", "cha"),
  skill("performance", "Выступление", "cha"),
  skill("persuasion", "Убеждение", "cha"),
];

const FIGHTER: ClassOption = {
  id: "fighter",
  name: "Воин",
  hit_die: 10,
  saving_throws: ["str", "con"],
  skill_choices: 2,
  skill_options: [
    "acrobatics",
    "athletics",
    "history",
    "insight",
    "perception",
    "survival",
  ],
  expertise_picks: 0,
  fighting_styles: ["defense", "archery"],
  starting_equipment: [
    { id: "chain_mail", name: "Кольчуга", qty: 1 },
    { id: "javelin", name: "Метательное копьё", qty: 4 },
  ],
  features: [
    { name: "Боевой стиль", summary: "Один стиль на выбор." },
    { name: "Второе дыхание", summary: "Восстанавливает здоровье." },
  ],
  spellcasting: null,
};

const ROGUE: ClassOption = {
  id: "rogue",
  name: "Плут",
  hit_die: 8,
  saving_throws: ["dex", "int"],
  skill_choices: 4,
  skill_options: [
    "acrobatics",
    "athletics",
    "deception",
    "insight",
    "investigation",
    "perception",
    "persuasion",
    "sleight_of_hand",
    "stealth",
  ],
  expertise_picks: 2,
  fighting_styles: [],
  starting_equipment: [
    { id: "dagger", name: "Кинжал", qty: 2 },
    { id: "thieves_tools", name: "Воровские инструменты", qty: 1 },
  ],
  features: [{ name: "Скрытая атака", summary: "Дополнительный урон." }],
  spellcasting: null,
};

const WIZARD: ClassOption = {
  id: "wizard",
  name: "Волшебник",
  hit_die: 6,
  saving_throws: ["int", "wis"],
  skill_choices: 2,
  skill_options: ["arcana", "history", "insight", "investigation", "religion"],
  expertise_picks: 0,
  fighting_styles: [],
  starting_equipment: [
    { id: "quarterstaff", name: "Боевой посох", qty: 1 },
    { id: "spellbook", name: "Книга заклинаний", qty: 1 },
  ],
  features: [{ name: "Колдовство", summary: "Заклинания из книги." }],
  spellcasting: {
    ability: "int",
    cantrips: 2,
    prepared: 2,
    cantrip_options: [
      { id: "fire_bolt", name: "Огненная стрела", level: 0, summary: "Огонь." },
      { id: "light", name: "Свет", level: 0, summary: "Светит." },
      { id: "mage_hand", name: "Волшебная рука", level: 0, summary: "Рука." },
    ],
    spell_options: [
      { id: "shield", name: "Щит", level: 1, summary: "Защита." },
      { id: "sleep", name: "Сон", level: 1, summary: "Усыпляет." },
      {
        id: "magic_missile",
        name: "Магическая стрела",
        level: 1,
        summary: "Урон.",
      },
    ],
  },
};

const HUMAN: SpeciesOption = {
  id: "human",
  name: "Человек",
  traits: [
    { name: "Находчивость", summary: "Вдохновение после отдыха." },
    { name: "Умелость", summary: "Один навык на выбор." },
  ],
  extra_skill: true,
  granted_skills: [],
};

const ELF: SpeciesOption = {
  id: "elf",
  name: "Эльф",
  traits: [{ name: "Острые чувства", summary: "Внимательность." }],
  extra_skill: false,
  granted_skills: ["perception"],
};

const DWARF: SpeciesOption = {
  id: "dwarf",
  name: "Дварф",
  traits: [{ name: "Тёмное зрение", summary: "Видит в темноте." }],
  extra_skill: false,
  granted_skills: [],
};

/** Правила создания, какими их отдаёт сервер, в уменьшенном виде. */
export const OPTIONS: CreationOptions = {
  level: 3,
  abilities: [
    { id: "str", name: "Сила" },
    { id: "dex", name: "Ловкость" },
    { id: "con", name: "Телосложение" },
    { id: "int", name: "Интеллект" },
    { id: "wis", name: "Мудрость" },
    { id: "cha", name: "Харизма" },
  ],
  skills: ALL_SKILLS,
  standard_array: [15, 14, 13, 12, 10, 8],
  point_buy: {
    budget: 27,
    costs: {
      "8": 0,
      "9": 1,
      "10": 2,
      "11": 3,
      "12": 4,
      "13": 5,
      "14": 7,
      "15": 9,
    },
  },
  background_bonus_shapes: [
    [2, 1],
    [1, 1, 1],
  ],
  classes: [FIGHTER, ROGUE, WIZARD],
  species: [HUMAN, ELF, DWARF],
  backgrounds: [
    {
      id: "criminal",
      name: "Преступник",
      ability_options: ["dex", "con", "int"],
      skills: ["sleight_of_hand", "stealth"],
      feat: "alert",
      equipment: [{ id: "dagger", name: "Кинжал", qty: 1 }],
    },
    {
      id: "acolyte",
      name: "Прислужник",
      ability_options: ["int", "wis", "cha"],
      skills: ["insight", "religion"],
      feat: "magic_initiate",
      equipment: [{ id: "holy_symbol", name: "Священный символ", qty: 1 }],
    },
  ],
  feats: [
    { id: "alert", name: "Бдительный", summary: "Бонус к инициативе." },
    { id: "magic_initiate", name: "Посвящённый в магию", summary: "Магия." },
  ],
  fighting_styles: [
    { id: "defense", name: "Оборона", summary: "+1 к КД." },
    { id: "archery", name: "Стрельба", summary: "+2 к дальним атакам." },
  ],
};

/** Герой из предпросмотра: имени и описания ещё нет, их пишет мастер после «Беру». */
export function previewHero(overrides: Partial<Character> = {}): Character {
  return {
    id: "pc_1111111111111111",
    name: "Безымянный странник",
    level: 3,
    species: "elf",
    class_id: "rogue",
    background: "criminal",
    scores: { str: 8, dex: 16, con: 12, int: 13, wis: 12, cha: 10 },
    skill_proficiencies: ["perception", "stealth", "sleight_of_hand"],
    expertise: ["stealth", "perception"],
    save_proficiencies: ["dex", "int"],
    fighting_style: null,
    hp_max: 21,
    hp: 21,
    temp_hp: 0,
    hit_dice_max: 3,
    hit_dice: 3,
    spell_slots_max: {},
    spell_slots: {},
    cantrips: [],
    prepared_spells: [],
    concentration: null,
    inventory: [
      { item_id: "dagger", qty: 3, equipped: true },
      { item_id: "thieves_tools", qty: 1, equipped: false },
    ],
    conditions: [],
    buffs: [],
    resources: {},
    inspiration: false,
    death_saves: { successes: 0, failures: 0, stable: false },
    persona: {
      concept: "",
      appearance: "",
      traits: "",
      ideals: "",
      bonds: "",
      flaws: "",
      backstory: "",
    },
    player_id: "u1",
    ...overrides,
  };
}

/**
 * Подробный выбор, каким его хранит сервер (`GET .../characters/mine`): поля по умолчанию
 * заполнены, в описании есть и пустые строки. Эльф-плут с предысторией «Преступник».
 */
export const STORED_DETAILED: CharacterChoices = {
  name: "Лира",
  species: "elf",
  class_id: "rogue",
  background: "criminal",
  method: "point_buy",
  base_scores: { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 },
  background_bonus: { dex: 2, con: 1 },
  skills: ["acrobatics", "deception", "insight", "persuasion"],
  extra_skill: null,
  expertise: ["stealth", "perception"],
  fighting_style: null,
  cantrips: [],
  prepared_spells: [],
  persona: {
    concept: "Вор-неудачник",
    appearance: "",
    traits: "",
    ideals: "",
    bonds: "",
    flaws: "",
    backstory: "Родилась в порту.",
  },
};

/** Быстрый выбор, каким его хранит сервер: имя не давали — `name: null`. */
export const STORED_QUICK: QuickChoices = {
  class_id: "rogue",
  species: "human",
  concept: "Вор-неудачник",
  name: null,
};

/** Ответ `GET .../characters/mine` для героя, созданного подробным способом. */
export const mineDetailed = (
  choices: CharacterChoices = STORED_DETAILED,
): MyCharacter => ({
  mode: "detailed",
  character: previewHero({ name: choices.name }),
  choices,
});

/** Ответ `GET .../characters/mine` для героя, созданного быстрым способом. */
export const mineQuick = (
  choices: QuickChoices = STORED_QUICK,
): MyCharacter => ({
  mode: "quick",
  character: previewHero({ name: choices.name ?? "Тень" }),
  choices,
});

/** Случайный герой: прежнего выбора у него нет. */
export const mineRandom: MyCharacter = {
  mode: "random",
  character: previewHero({ name: "Бродяга" }),
  choices: null,
};

/** Герой, созданный до того, как сервер стал хранить выбор: ни способа, ни выбора. */
export const mineLegacy: MyCharacter = {
  mode: null,
  character: previewHero({ name: "Старожил" }),
  choices: null,
};

/**
 * Обёртка над `fetch`, которая держит выбранные запросы, пока тест их не отпустит:
 * так проверяется поведение экрана, пока ответ сервера ещё в пути. `started` — все
 * запросы в порядке отправки, в том числе ещё не отпущенные.
 */
export function holdRequests(inner: typeof fetch) {
  const holds: { suffix: string; released: Promise<void> }[] = [];
  const started: string[] = [];

  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    started.push(`${(init?.method ?? "GET").toUpperCase()} ${url}`);
    const index = holds.findIndex((hold) => url.endsWith(hold.suffix));
    if (index >= 0) {
      const [hold] = holds.splice(index, 1);
      await hold.released;
    }
    return inner(input, init);
  }) as typeof fetch;

  /** Держит следующий запрос на адрес с таким окончанием; возвращает «отпустить». */
  const hold = (suffix: string): (() => void) => {
    let release: () => void = () => {};
    const released = new Promise<void>((resolve) => {
      release = resolve;
    });
    holds.push({ suffix, released });
    return release;
  };
  return { fetch: fetchImpl, hold, started };
}
