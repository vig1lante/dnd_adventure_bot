// Данные и помощники ТОЛЬКО для тестов экранов создания героя. Правила здесь
// урезаны до нужного тестам: настоящие списки отдаёт сервер (creation-options).
import type {
  Character,
  CharacterChoices,
  ClassOption,
  CreationOptions,
  Described,
  MyCharacter,
  QuickChoices,
  SkillOption,
  SpeciesOption,
  SpellDetails,
  SpellOption,
} from "../../api/types";

// Подробности (`details`) — строго как у сервера: подписи кодов, скорость и дистанции текстом
// в метрах. Есть все виды вариантов: раса средняя и маленькая, класс без магии, заклинатель
// с заговорами и заклинатель без них.

const skill = (
  id: SkillOption["id"],
  name: string,
  ability: SkillOption["ability"],
  summary: string,
): SkillOption => ({ id, name, ability, details: { ability, summary } });

const ALL_SKILLS: SkillOption[] = [
  skill("athletics", "Атлетика", "str", "Сила в деле: лазать, прыгать, плыть."),
  skill(
    "acrobatics",
    "Акробатика",
    "dex",
    "Удержать равновесие на карнизе, кувырок.",
  ),
  skill(
    "sleight_of_hand",
    "Ловкость рук",
    "dex",
    "Незаметно стащить кошелёк, спрятать нож.",
  ),
  skill("stealth", "Скрытность", "dex", "Подкрасться к часовому, спрятаться."),
  skill("arcana", "Магия", "int", "Узнать заклинание, понять знак на двери."),
  skill("history", "История", "int", "Вспомнить старую войну и род королей."),
  skill("investigation", "Анализ", "int", "Найти тайник, понять, где ловушка."),
  skill("nature", "Природа", "int", "Знать травы, зверей и погоду."),
  skill("religion", "Религия", "int", "Узнать бога по символу, обряд храма."),
  skill(
    "animal_handling",
    "Уход за животными",
    "wis",
    "Успокоить коня, приручить пса.",
  ),
  skill("insight", "Проницательность", "wis", "Понять, что собеседник лжёт."),
  skill("medicine", "Медицина", "wis", "Перевязать рану, узнать болезнь."),
  skill(
    "perception",
    "Внимательность",
    "wis",
    "Заметить засаду, услышать шаги.",
  ),
  skill("survival", "Выживание", "wis", "Идти по следу, найти воду в пустыне."),
  skill(
    "deception",
    "Обман",
    "cha",
    "Соврать стражнику, выдать себя за купца.",
  ),
  skill("intimidation", "Запугивание", "cha", "Заставить бандита отступить."),
  skill("performance", "Выступление", "cha", "Спеть в таверне, сыграть роль."),
  skill("persuasion", "Убеждение", "cha", "Уговорить старосту помочь отряду."),
];

const spell = (
  id: string,
  name: string,
  level: number,
  details: Omit<SpellDetails, "summary"> & { summary: string },
): SpellOption => ({ id, name, level, summary: details.summary, details });

const described = (id: string, name: string, summary: string): Described => ({
  id,
  name,
  summary,
  details: { summary },
});

const ARMOR = {
  light: "Лёгкие доспехи",
  medium: "Средние доспехи",
  heavy: "Тяжёлые доспехи",
  shield: "Щиты",
} as const;

const SIMPLE = "Простое оружие";
const MARTIAL = "Воинское оружие";

const FIGHTER_SKILLS: SkillOption["id"][] = [
  "acrobatics",
  "athletics",
  "history",
  "insight",
  "perception",
  "survival",
];

const FIGHTER: ClassOption = {
  id: "fighter",
  name: "Воин",
  hit_die: 10,
  saving_throws: ["str", "con"],
  skill_choices: 2,
  skill_options: FIGHTER_SKILLS,
  expertise_picks: 0,
  fighting_styles: ["defense", "archery"],
  starting_equipment: [
    { id: "chain_mail", name: "Кольчуга", qty: 1 },
    { id: "javelin", name: "Метательное копьё", qty: 4 },
  ],
  features: [
    { name: "Боевой стиль", summary: "Один стиль на выбор." },
    { name: "Второе дыхание", summary: "Восстанавливает здоровье." },
    { name: "Всплеск действий", summary: "Ещё одно действие в свой ход." },
  ],
  spellcasting: null,
  details: {
    hit_die: 10,
    primary: "str",
    saves: ["str", "con"],
    armor: [ARMOR.light, ARMOR.medium, ARMOR.heavy, ARMOR.shield],
    weapons: [SIMPLE, MARTIAL],
    skill_choices: { count: 2, from: FIGHTER_SKILLS },
    features: [
      {
        level: 1,
        name: "Боевой стиль",
        summary: "Один стиль на выбор.",
        mechanical: true,
      },
      {
        level: 1,
        name: "Второе дыхание",
        summary: "Восстанавливает здоровье.",
        mechanical: true,
      },
      {
        level: 2,
        name: "Всплеск действий",
        summary: "Ещё одно действие в свой ход.",
        mechanical: true,
      },
    ],
    spellcasting: null,
  },
};

const ROGUE_SKILLS: SkillOption["id"][] = [
  "acrobatics",
  "athletics",
  "deception",
  "insight",
  "investigation",
  "perception",
  "persuasion",
  "sleight_of_hand",
  "stealth",
];

const ROGUE: ClassOption = {
  id: "rogue",
  name: "Плут",
  hit_die: 8,
  saving_throws: ["dex", "int"],
  skill_choices: 4,
  skill_options: ROGUE_SKILLS,
  expertise_picks: 2,
  fighting_styles: [],
  starting_equipment: [
    { id: "dagger", name: "Кинжал", qty: 2 },
    { id: "thieves_tools", name: "Воровские инструменты", qty: 1 },
  ],
  features: [
    { name: "Скрытая атака", summary: "Дополнительный урон." },
    { name: "Воровской жаргон", summary: "Тайный язык воров." },
  ],
  spellcasting: null,
  details: {
    hit_die: 8,
    primary: "dex",
    saves: ["dex", "int"],
    armor: [ARMOR.light],
    weapons: [SIMPLE],
    skill_choices: { count: 4, from: ROGUE_SKILLS },
    features: [
      {
        level: 1,
        name: "Скрытая атака",
        summary: "Дополнительный урон.",
        mechanical: true,
      },
      {
        level: 1,
        name: "Воровской жаргон",
        summary: "Тайный язык воров.",
        mechanical: false,
      },
    ],
    spellcasting: null,
  },
};

const WIZARD_SKILLS: SkillOption["id"][] = [
  "arcana",
  "history",
  "insight",
  "investigation",
  "religion",
];

const WIZARD: ClassOption = {
  id: "wizard",
  name: "Волшебник",
  hit_die: 6,
  saving_throws: ["int", "wis"],
  skill_choices: 2,
  skill_options: WIZARD_SKILLS,
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
      spell("fire_bolt", "Огненная стрела", 0, {
        school: "Воплощение",
        casting_time: "Действие",
        range_text: "Далеко, дальше 18 м",
        duration: "Мгновенно",
        summary: "Огонь.",
      }),
      spell("light", "Свет", 0, {
        school: "Воплощение",
        casting_time: "Действие",
        range_text: "Касание",
        duration: "1 час",
        summary: "Светит.",
      }),
      spell("mage_hand", "Волшебная рука", 0, {
        school: "Вызов",
        casting_time: "Действие",
        range_text: "Близко, до 18 м",
        duration: "1 минута",
        summary: "Рука.",
      }),
    ],
    spell_options: [
      spell("shield", "Щит", 1, {
        school: "Ограждение",
        casting_time: "Бонусное действие",
        range_text: "На себя",
        duration: "1 раунд",
        summary: "Защита.",
      }),
      spell("sleep", "Сон", 1, {
        school: "Очарование",
        casting_time: "Действие",
        range_text: "Близко, до 18 м",
        duration: "1 минута, концентрация",
        summary: "Усыпляет.",
      }),
      spell("magic_missile", "Магическая стрела", 1, {
        school: "Воплощение",
        casting_time: "Действие",
        range_text: "Далеко, дальше 18 м",
        duration: "Мгновенно",
        summary: "Урон.",
      }),
    ],
  },
  details: {
    hit_die: 6,
    primary: "int",
    saves: ["int", "wis"],
    armor: [],
    weapons: [SIMPLE],
    skill_choices: { count: 2, from: WIZARD_SKILLS },
    features: [
      {
        level: 1,
        name: "Колдовство",
        summary: "Заклинания из книги.",
        mechanical: false,
      },
    ],
    spellcasting: { ability: "int", cantrips: 2, prepared: 2 },
  },
};

const RANGER_SKILLS: SkillOption["id"][] = [
  "athletics",
  "insight",
  "nature",
  "perception",
  "stealth",
  "survival",
];

/** Заклинатель без заговоров: в правилах так у следопыта. */
const RANGER: ClassOption = {
  id: "ranger",
  name: "Следопыт",
  hit_die: 10,
  saving_throws: ["str", "dex"],
  skill_choices: 3,
  skill_options: RANGER_SKILLS,
  expertise_picks: 0,
  fighting_styles: [],
  starting_equipment: [{ id: "longbow", name: "Длинный лук", qty: 1 }],
  features: [{ name: "Избранный враг", summary: "Метка охотника без ячеек." }],
  spellcasting: {
    ability: "wis",
    cantrips: 0,
    prepared: 2,
    cantrip_options: [],
    spell_options: [
      spell("hunters_mark", "Метка охотника", 1, {
        school: "Прорицание",
        casting_time: "Бонусное действие",
        range_text: "Далеко, дальше 18 м",
        duration: "1 час, концентрация",
        summary: "Отмечает добычу.",
      }),
      spell("cure_wounds", "Лечение ран", 1, {
        school: "Ограждение",
        casting_time: "Действие",
        range_text: "Касание",
        duration: "Мгновенно",
        summary: "Лечит.",
      }),
      spell("alarm", "Тревога", 1, {
        school: "Ограждение",
        casting_time: "Несколько минут, только вне боя",
        range_text: "Близко, до 18 м",
        duration: "8 часов",
        summary: "Сторожит лагерь.",
      }),
    ],
  },
  details: {
    hit_die: 10,
    primary: "dex",
    saves: ["str", "dex"],
    armor: [ARMOR.light, ARMOR.medium, ARMOR.shield],
    weapons: [SIMPLE, MARTIAL],
    skill_choices: { count: 3, from: RANGER_SKILLS },
    features: [
      {
        level: 1,
        name: "Избранный враг",
        summary: "Метка охотника без ячеек.",
        mechanical: true,
      },
    ],
    spellcasting: { ability: "wis", cantrips: 0, prepared: 2 },
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
  details: {
    name_en: "Human",
    description:
      "Люди живут почти везде и быстро учатся новому. Их сила — в упорстве.",
    size: "Средний",
    speed: 30,
    speed_text: "9 м",
    traits: [
      {
        name: "Находчивость",
        summary: "Вдохновение после отдыха.",
        mechanical: true,
      },
      { name: "Умелость", summary: "Один навык на выбор.", mechanical: false },
    ],
  },
};

const ELF: SpeciesOption = {
  id: "elf",
  name: "Эльф",
  traits: [{ name: "Острые чувства", summary: "Внимательность." }],
  extra_skill: false,
  granted_skills: ["perception"],
  details: {
    name_en: "Elf",
    description: "Эльфы живут веками и помнят то, что другие давно забыли.",
    size: "Средний",
    speed: 35,
    speed_text: "10,5 м",
    traits: [
      {
        name: "Острые чувства",
        summary: "Внимательность.",
        mechanical: false,
      },
    ],
  },
};

const DWARF: SpeciesOption = {
  id: "dwarf",
  name: "Дварф",
  traits: [{ name: "Тёмное зрение", summary: "Видит в темноте." }],
  extra_skill: false,
  granted_skills: [],
  details: {
    name_en: "Dwarf",
    description: "Дварфы крепки, как камень их гор, и не забывают обид.",
    size: "Средний",
    speed: 30,
    speed_text: "9 м",
    traits: [
      { name: "Тёмное зрение", summary: "Видит в темноте.", mechanical: true },
    ],
  },
};

const HALFLING: SpeciesOption = {
  id: "halfling",
  name: "Полурослик",
  traits: [{ name: "Везучий", summary: "Перебрасывает единицу на к20." }],
  extra_skill: false,
  granted_skills: [],
  details: {
    name_en: "Halfling",
    description: "Полурослики малы ростом, но удачливы и храбры.",
    size: "Маленький",
    speed: 30,
    speed_text: "9 м",
    traits: [
      {
        name: "Везучий",
        summary: "Перебрасывает единицу на к20.",
        mechanical: true,
      },
    ],
  },
};

/**
 * Слова, которых игрок не должен видеть: «вид» вместо «раса» и футы вместо метров. «Вид»
 * ищется целым словом, чтобы не ловить «видит» в описании черты.
 */
export const OUTDATED_WORDS = /(?<![а-яё])вид(?:а|у|ом|е|ы)?(?![а-яё])|фут/iu;

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
  classes: [FIGHTER, ROGUE, WIZARD, RANGER],
  species: [HUMAN, ELF, DWARF, HALFLING],
  backgrounds: [
    {
      id: "criminal",
      name: "Преступник",
      ability_options: ["dex", "con", "int"],
      skills: ["sleight_of_hand", "stealth"],
      feat: "alert",
      equipment: [{ id: "dagger", name: "Кинжал", qty: 1 }],
      details: {
        abilities: ["dex", "con", "int"],
        skills: ["sleight_of_hand", "stealth"],
        tool: "Воровские инструменты",
        feat: {
          id: "alert",
          name: "Бдительный",
          summary: "Бонус к инициативе.",
        },
        equipment: [{ id: "dagger", name: "Кинжал", qty: 1 }],
      },
    },
    {
      id: "acolyte",
      name: "Прислужник",
      ability_options: ["int", "wis", "cha"],
      skills: ["insight", "religion"],
      feat: "magic_initiate",
      equipment: [{ id: "holy_symbol", name: "Священный символ", qty: 1 }],
      details: {
        abilities: ["int", "wis", "cha"],
        skills: ["insight", "religion"],
        tool: "Набор каллиграфа",
        feat: {
          id: "magic_initiate",
          name: "Посвящённый в магию",
          summary: "Магия.",
        },
        equipment: [{ id: "holy_symbol", name: "Священный символ", qty: 1 }],
      },
    },
  ],
  feats: [
    described("alert", "Бдительный", "Бонус к инициативе."),
    described("magic_initiate", "Посвящённый в магию", "Магия."),
  ],
  fighting_styles: [
    described("defense", "Оборона", "+1 к КД."),
    described("archery", "Стрельба", "+2 к дальним атакам."),
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
