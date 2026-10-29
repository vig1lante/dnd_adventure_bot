import type {
  AdventureSummary,
  AppConfig,
  Character,
  ClassOption,
  CombatView,
  CreationOptions,
  Draft,
  DraftSummary,
  FeedItem,
  GameSummary,
  JournalEntry,
  PartyMember,
  PhaseView,
  PlayerSummary,
  RollData,
  SkillOption,
  Snapshot,
  SpeciesOption,
  User,
} from "../api/types";

// Данные макетов B10.1: вечерняя игра четырёх друзей в тех формах, в каких её присылает
// сервер (api/types.ts). Только данные, без тестового кода: страница превью открывается
// в браузере. Числа выдуманы — настоящие считает движок, клиент их только показывает.

/** «Сейчас» по часам сервера: от него отсчитываются сроки фаз. */
export const SERVER_TIME = "2026-10-29T18:30:00+00:00";

/** Срок через `seconds` секунд после SERVER_TIME (ISO, как в снимке). */
const deadlineIn = (seconds: number) =>
  new Date(Date.parse(SERVER_TIME) + seconds * 1000).toISOString();

// --- люди и игры ---

/** Игрок, глазами которого показаны экраны: хост и автор приключений. */
export const PREVIEW_USER: User = {
  id: "u1",
  name: "Вика",
  can_create_games: true,
};

export const APP_CONFIG: AppConfig = {
  env: "dev",
  dev_login: false,
  bot_username: "dnd_table_bot",
};

const player = (
  user_id: string,
  name: string,
  rest: Partial<PlayerSummary> = {},
): PlayerSummary => ({
  user_id,
  name,
  is_host: false,
  ready: true,
  has_character: true,
  ...rest,
});

const TABLE: PlayerSummary[] = [
  player("u1", "Вика", { is_host: true }),
  player("u2", "Борис"),
  player("u3", "Мила"),
  player("u4", "Артём"),
];

export const ADVENTURE_TITLE = "Маяк над Солёной бухтой";

export const ADVENTURES: AdventureSummary[] = [
  {
    id: "salt-lighthouse",
    title: ADVENTURE_TITLE,
    pitch:
      "В ночь шторма маяк погас, а смотритель пропал. Рыбаки шепчутся о голосах из-под воды.",
    target_hours: 3,
    score: 86,
    blocking: false,
  },
  {
    id: "honey-ford",
    title: "Ярмарка в Медовом Броде",
    pitch:
      "На ярмарке пропадают дети, а в шатре гадалки каждую ночь горит зелёный огонь.",
    target_hours: 2,
    score: 78,
    blocking: false,
  },
];

const game = (
  id: string,
  rest: Partial<GameSummary> & Pick<GameSummary, "phase">,
): GameSummary => ({
  id,
  code: "SALT42",
  adventure_id: "salt-lighthouse",
  adventure_title: ADVENTURE_TITLE,
  turn_phase: null,
  provider: null,
  model: null,
  collect_seconds: 120,
  hurry_seconds: 60,
  camp_seconds: 300,
  short_rest_seconds: 120,
  profanity: false,
  players: TABLE,
  ...rest,
});

export const LOBBY_GAME = game("g-lobby", {
  phase: "lobby",
  adventure_id: "honey-ford",
  adventure_title: "Ярмарка в Медовом Броде",
  code: "HONEY7",
  players: [
    player("u1", "Вика", { is_host: true }),
    player("u2", "Борис"),
    player("u3", "Мила", { ready: false, has_character: false }),
    player("u4", "Артём", { ready: false }),
  ],
});

/** «Мои игры»: идущая игра, лобби и прошлая игра. */
export const MY_GAMES: GameSummary[] = [
  game("g-story", { phase: "playing", turn_phase: "collecting" }),
  LOBBY_GAME,
  game("g-crypt", {
    phase: "finished",
    adventure_id: "demo-crypt",
    adventure_title: "Склеп забытого короля",
  }),
];

// --- герой и отряд ---

/** Лист Лиры — эльфийки-плута третьего уровня, со всеми числами «от сервера». */
export const HERO: Character = {
  id: "pc_lira",
  name: "Лира",
  level: 3,
  species: "elf",
  class_id: "rogue",
  background: "criminal",
  scores: { str: 8, dex: 17, con: 12, int: 13, wis: 12, cha: 10 },
  skill_proficiencies: [
    "stealth",
    "sleight_of_hand",
    "perception",
    "acrobatics",
  ],
  expertise: ["stealth", "sleight_of_hand"],
  save_proficiencies: ["dex", "int"],
  fighting_style: null,
  hp_max: 21,
  hp: 14,
  temp_hp: 0,
  hit_dice_max: 3,
  hit_dice: 2,
  spell_slots_max: {},
  spell_slots: {},
  cantrips: [],
  prepared_spells: [],
  concentration: null,
  inventory: [
    { item_id: "dagger", qty: 2, equipped: true },
    { item_id: "shortbow", qty: 1, equipped: false },
    { item_id: "thieves_tools", qty: 1, equipped: false },
  ],
  conditions: [],
  buffs: [],
  resources: {},
  inspiration: true,
  death_saves: { successes: 0, failures: 0, stable: false },
  persona: {
    concept: "Бывшая воровка, которая ищет брата",
    appearance: "Тонкая, с серебряной серьгой и шрамом на ладони.",
    traits: "Шутит, когда страшно.",
    ideals: "Свобода.",
    bonds: "Брат пропал в этих краях три года назад.",
    flaws: "Не может пройти мимо запертой двери.",
    backstory:
      "Лира выросла в портовых трущобах и рано научилась открывать чужие замки.",
  },
  player_id: "u1",
  derived: {
    armor_class: 14,
    proficiency_bonus: 2,
    passive_perception: 13,
    abilities: [
      { id: "str", name: "Сила", modifier: -1, save_bonus: -1 },
      { id: "dex", name: "Ловкость", modifier: 3, save_bonus: 5 },
      { id: "con", name: "Телосложение", modifier: 1, save_bonus: 1 },
      { id: "int", name: "Интеллект", modifier: 1, save_bonus: 3 },
      { id: "wis", name: "Мудрость", modifier: 1, save_bonus: 1 },
      { id: "cha", name: "Харизма", modifier: 0, save_bonus: 0 },
    ],
    skills: [
      { id: "acrobatics", name: "Акробатика", ability: "dex", bonus: 5 },
      { id: "sleight_of_hand", name: "Ловкость рук", ability: "dex", bonus: 7 },
      { id: "stealth", name: "Скрытность", ability: "dex", bonus: 7 },
      { id: "perception", name: "Внимательность", ability: "wis", bonus: 3 },
      { id: "investigation", name: "Анализ", ability: "int", bonus: 1 },
      { id: "insight", name: "Проницательность", ability: "wis", bonus: 1 },
    ],
    spell_save_dc: null,
    spell_attack_bonus: null,
    attacks: [
      {
        item_id: "dagger",
        name: "Кинжал",
        equipped: true,
        bonus: 5,
        damage: "1d4+3",
        damage_type: "piercing",
        range: "melee",
        ability: "dex",
      },
      {
        item_id: "shortbow",
        name: "Короткий лук",
        equipped: false,
        bonus: 5,
        damage: "1d6+3",
        damage_type: "piercing",
        range: "far",
        ability: "dex",
      },
    ],
  },
};

const member = (
  id: string,
  name: string,
  player_id: string,
  hp: number,
  hp_max: number,
): PartyMember => ({
  id,
  name,
  player_id,
  hp,
  hp_max,
  temp_hp: 0,
  conditions: [],
  effects: [],
});

export const PARTY: PartyMember[] = [
  member("pc_lira", "Лира", "u1", 14, 21),
  member("pc_borin", "Борин", "u2", 27, 31),
  member("pc_kai", "Кай", "u3", 9, 17),
  member("pc_elsa", "Эльза", "u4", 24, 24),
];

// --- лента ---

// Id записей ленты идут по порядку объявления, как у сервера.
let nextFeedId = 100;

const entry = (
  kind: FeedItem["kind"],
  text: string,
  rest: Partial<FeedItem> = {},
): FeedItem => ({
  id: nextFeedId++,
  kind,
  text,
  data: null,
  private: false,
  created_at: SERVER_TIME,
  ...rest,
});

const said = (actor: string, text: string, kind: FeedItem["kind"] = "action") =>
  entry(kind, text, { data: { actor } });

const roll = (text: string, data: RollData) =>
  entry("roll", text, { data: { ...data } });

/** Утро второго дня: отряд у двери погасшего маяка. */
export const STORY_FEED: FeedItem[] = [
  entry(
    "narration",
    "Утро выдалось серым и солёным. Тропа вдоль обрыва вывела вас к маяку: башня из тёмного камня, окна в потёках соли, а дверь заперта изнутри.\nГде-то внизу, у воды, глухо бьётся о сваи брошенная лодка.",
  ),
  said("Лира", "Осматриваю замок на двери и пробую его вскрыть."),
  said("Борин", "Держу фонарь повыше и прикрываю Лиру щитом."),
  said("Кай", "Слушаю, нет ли кого внутри."),
  roll("Лира: Ловкость рук — 19 против Сл 15: успех", {
    actor_id: "pc_lira",
    actor: "Лира",
    label: "Ловкость рук",
    reason: "Вскрыть замок маяка",
    expr: "1d20+7",
    rolls: [12],
    kept: [12],
    modifier: 7,
    total: 19,
    natural: 12,
    advantage: "none",
    dc: 15,
    success: true,
  }),
  roll("Кай: Внимательность — 8 против Сл 15: неудача", {
    actor_id: "pc_kai",
    actor: "Кай",
    label: "Внимательность",
    reason: "Услышать, есть ли кто внутри",
    expr: "1d20+1",
    rolls: [7],
    kept: [7],
    modifier: 1,
    total: 8,
    natural: 7,
    advantage: "none",
    dc: 15,
    success: false,
  }),
  entry(
    "narration",
    "Замок щёлкает, и дверь со скрипом отходит. Изнутри тянет холодом и мокрой золой. На винтовой лестнице — следы босых ног, и они ведут не наверх, к фонарю, а вниз, в подвал.\nЧто вы делаете?",
  ),
  entry(
    "private",
    "На дверной ручке ты замечаешь свежую соль: кто-то выходил отсюда совсем недавно — со стороны моря.",
    { private: true },
  ),
  entry("system", "Хост открыл сбор ходов: на ход две минуты."),
];

/** Вечер второго дня: бой на причале, ход Лиры. */
export const COMBAT_FEED: FeedItem[] = [
  entry(
    "narration",
    "Из воды у причала поднимаются двое утопленников: водоросли в волосах, пустые белые глаза. Первый уже тянет к Борину руки.",
  ),
  roll("Утопленник: Атака: Удар — 12 против КД 18: промах", {
    actor: "Утопленник",
    label: "Атака: Удар",
    reason: "",
    expr: "1d20+4",
    rolls: [8],
    kept: [8],
    modifier: 4,
    total: 12,
    natural: 8,
    advantage: "none",
    dc: null,
    success: false,
    target_ac: 18,
  }),
  roll("Борин: Атака: Боевой топор — 21 против КД 9: попадание", {
    actor_id: "pc_borin",
    actor: "Борин",
    label: "Атака: Боевой топор",
    reason: "",
    expr: "1d20+5",
    rolls: [16],
    kept: [16],
    modifier: 5,
    total: 21,
    natural: 16,
    advantage: "none",
    dc: null,
    success: true,
    target_ac: 9,
  }),
  roll("Борин: Урон топором — 9", {
    actor_id: "pc_borin",
    actor: "Борин",
    label: "Урон топором",
    reason: "",
    expr: "1d8+3",
    rolls: [6],
    kept: [6],
    modifier: 3,
    total: 9,
    natural: null,
    advantage: "none",
    dc: null,
    success: null,
  }),
  entry(
    "narration",
    "Топор Борина входит в мокрое плечо, и утопленник шатается. Второй обходит вас по краю причала, к Лире.",
  ),
  entry("system", "Раунд 2. Ход Лиры."),
];

const campLine = (actor: string, text: string) =>
  said(actor, text, "camp_line");

const CAMP_LINES: FeedItem[] = [
  campLine("Борин", "Кто-нибудь ещё слышал пение из-под воды?"),
  campLine("Кай", "Слышал. И мне это совсем не нравится."),
  campLine("Лира", "Я посторожу первой. Всё равно не усну."),
];

/** Ночь второго дня: долгий привал в сторожке у маяка. */
export const CAMP_FEED: FeedItem[] = [
  entry(
    "narration",
    "Вы разводите огонь в сторожке у подножия маяка. Ветер воет в трубе, а за окном шумит прилив.",
  ),
  entry("system", "Хост объявил долгий привал."),
  ...CAMP_LINES,
];

// --- дневник ---

const journal = (
  ref: string,
  section: JournalEntry["section"],
  title: string,
  text: string,
  turn = 0,
): JournalEntry => ({
  id: `${ref}@all`,
  ref,
  section,
  title,
  text,
  audience: "all",
  turn,
  revision: 1,
  visited_turn: null,
});

export const JOURNAL: JournalEntry[] = [
  journal(
    "intro",
    "main",
    "Главное",
    "Староста Солёной бухты просит узнать, почему погас маяк и куда пропал смотритель Огден.",
  ),
  journal(
    "location:village",
    "places",
    "Солёная бухта",
    "Рыбацкая деревня под обрывом. Пахнет дымом и сетями.",
  ),
  journal(
    "location:lighthouse",
    "places",
    "Маяк",
    "Башня из тёмного камня на скале. Дверь была заперта изнутри.",
    2,
  ),
  journal(
    "npc:ogden",
    "people",
    "Смотритель Огден",
    "Старик, тридцать лет зажигал маяк. Пропал в ночь шторма.",
  ),
  journal(
    "npc:marta",
    "people",
    "Марта из «Кривого якоря»",
    "Хозяйка таверны. Знает всех в деревне и всё, что о них говорят.",
    1,
  ),
  journal(
    "deity:umberlee",
    "gods",
    "Умберли",
    "Богиня моря и штормов. Рыбаки бросают ей монеты перед выходом в море.",
  ),
  journal(
    "rumor:voices",
    "rumors",
    "Голоса из-под воды",
    "По ночам у причала слышат пение. Говорят, так зовут утопленники.",
    2,
  ),
  journal(
    "clue:salt",
    "clues",
    "Соль на следах",
    "Босые следы на лестнице маяка ведут в подвал, а не к фонарю.",
    3,
  ),
];

// --- фазы и снимки ---

const phase = (
  rest: Partial<PhaseView> & Pick<PhaseView, "turn_phase">,
): PhaseView => ({
  phase: "playing",
  deadline_at: null,
  remaining_seconds: null,
  scene: { type: "exploration", day: 2, part: "morning" },
  ...rest,
});

const snapshot = (
  summary: GameSummary,
  rest: Partial<Snapshot> & Pick<Snapshot, "phase">,
): Snapshot => ({
  type: "snapshot",
  server_time: SERVER_TIME,
  game: summary,
  host_user_id: "u1",
  statuses: {},
  character: HERO,
  action: null,
  party: PARTY,
  combat: null,
  feed: STORY_FEED,
  journal: JOURNAL,
  rest: null,
  ...rest,
});

const COMBAT: CombatView = {
  round: 2,
  current_id: "pc_lira",
  order: [
    {
      id: "pc_lira",
      name: "Лира",
      kind: "pc",
      side: "party",
      zone_id: "pier",
      conditions: [],
      character_id: "pc_lira",
      hp: 14,
      hp_max: 21,
    },
    {
      id: "drowned-1",
      name: "Утопленник",
      kind: "monster",
      side: "enemies",
      zone_id: "pier",
      conditions: [],
      health: "тяжело ранен",
    },
    {
      id: "pc_borin",
      name: "Борин",
      kind: "pc",
      side: "party",
      zone_id: "pier",
      conditions: [],
      character_id: "pc_borin",
      hp: 27,
      hp_max: 31,
    },
    {
      id: "pc_kai",
      name: "Кай",
      kind: "pc",
      side: "party",
      zone_id: "stairs",
      conditions: [{ id: "frightened", name: "Испуган", rounds_left: 1 }],
      character_id: "pc_kai",
      hp: 9,
      hp_max: 17,
    },
    {
      id: "drowned-2",
      name: "Утопленник",
      kind: "monster",
      side: "enemies",
      zone_id: "pier",
      conditions: [],
      health: "цел",
    },
  ],
  zones: [
    {
      id: "pier",
      name: "Причал",
      combatants: ["pc_lira", "drowned-1", "pc_borin", "drowned-2"],
    },
    { id: "stairs", name: "Лестница к маяку", combatants: ["pc_kai"] },
  ],
};

const playing = (turn_phase: GameSummary["turn_phase"]) =>
  game("g-story", { phase: "playing", turn_phase });

/** Снимки игр по id: подмена сокета превью отдаёт снимок той игры, к которой подключились. */
export const SNAPSHOTS: Record<string, Snapshot> = {
  // Сбор ходов: игроки пишут, у Лиры черновик хода.
  "g-story": snapshot(playing("collecting"), {
    phase: phase({ turn_phase: "collecting", deadline_at: deadlineIn(87) }),
    statuses: { u1: "writing", u2: "ready", u3: "writing", u4: "pass" },
    action: {
      text: "Спускаюсь по лестнице первой, тихо, держа кинжал наготове.",
      secret: false,
      inspiration: false,
      status: "draft",
    },
  }),
  // Мастер разбирает ходы.
  "g-think": snapshot(
    { ...playing("resolving"), id: "g-think" },
    {
      phase: phase({ turn_phase: "resolving" }),
      statuses: { u1: "ready", u2: "ready", u3: "ready", u4: "ready" },
      action: {
        text: "Спускаюсь по лестнице первой, тихо, держа кинжал наготове.",
        secret: false,
        inspiration: false,
        status: "submitted",
      },
    },
  ),
  // Бой: сейчас ход Лиры.
  "g-combat": snapshot(
    { ...playing("combat_wait"), id: "g-combat" },
    {
      phase: phase({
        turn_phase: "combat_wait",
        deadline_at: deadlineIn(46),
        scene: { type: "combat", day: 2, part: "evening" },
      }),
      combat: COMBAT,
      feed: [...STORY_FEED, ...COMBAT_FEED],
    },
  ),
  // Долгий привал ночью.
  "g-camp": snapshot(
    { ...playing("camp"), id: "g-camp" },
    {
      phase: phase({
        turn_phase: "camp",
        deadline_at: deadlineIn(242),
        scene: { type: "camp", day: 2, part: "night" },
      }),
      feed: [...STORY_FEED, ...CAMP_FEED],
      rest: {
        kind: "long",
        deadline_at: deadlineIn(242),
        my_ideas: [
          "Лира рассказывает, откуда у неё шрам на ладони.",
          "Спросить Кая, что он видел в воде у причала.",
        ],
        lines: CAMP_LINES,
      },
    },
  ),
  "g-lobby": snapshot(LOBBY_GAME, {
    phase: phase({ phase: "lobby", turn_phase: null, scene: null }),
    character: null,
    party: [],
    feed: [],
    journal: [],
  }),
  // Начало игры: мастер вплетает героев в мир.
  "g-weave": snapshot(
    { ...LOBBY_GAME, id: "g-weave", phase: "weaving", turn_phase: "resolving" },
    {
      phase: phase({ phase: "weaving", turn_phase: "resolving", scene: null }),
      character: null,
      party: [],
      feed: [],
      journal: [],
    },
  ),
};

// --- создание героя ---

const skill = (
  id: SkillOption["id"],
  name: string,
  ability: SkillOption["ability"],
  summary: string,
): SkillOption => ({ id, name, ability, details: { ability, summary } });

const SKILLS: SkillOption[] = [
  skill("athletics", "Атлетика", "str", "Лазать, прыгать, плыть."),
  skill("acrobatics", "Акробатика", "dex", "Равновесие на карнизе, кувырок."),
  skill(
    "sleight_of_hand",
    "Ловкость рук",
    "dex",
    "Стащить кошелёк, вскрыть замок.",
  ),
  skill("stealth", "Скрытность", "dex", "Подкрасться к часовому."),
  skill("arcana", "Магия", "int", "Узнать заклинание по знаку."),
  skill("history", "История", "int", "Вспомнить старую войну."),
  skill("investigation", "Анализ", "int", "Найти тайник и ловушку."),
  skill("religion", "Религия", "int", "Узнать бога по символу."),
  skill("insight", "Проницательность", "wis", "Понять, что собеседник лжёт."),
  skill("perception", "Внимательность", "wis", "Заметить засаду."),
  skill("survival", "Выживание", "wis", "Идти по следу."),
  skill("persuasion", "Убеждение", "cha", "Уговорить старосту помочь."),
];

const classOption = (
  id: string,
  name: string,
  hit_die: number,
  primary: ClassOption["details"]["primary"],
  saves: ClassOption["saving_throws"],
  skills: ClassOption["skill_options"],
  features: [string, string][],
  armor: string[],
): ClassOption => ({
  id,
  name,
  hit_die,
  saving_throws: saves,
  skill_choices: 2,
  skill_options: skills,
  expertise_picks: 0,
  fighting_styles: [],
  starting_equipment: [],
  features: features.map(([featureName, summary]) => ({
    name: featureName,
    summary,
  })),
  spellcasting: null,
  details: {
    hit_die,
    primary,
    saves,
    armor,
    weapons: ["Простое оружие"],
    skill_choices: { count: 2, from: skills },
    features: features.map(([featureName, summary]) => ({
      level: 1,
      name: featureName,
      summary,
      mechanical: true,
    })),
    spellcasting: null,
  },
});

const speciesOption = (
  id: string,
  name: string,
  name_en: string,
  description: string,
  speed_text: string,
  trait: [string, string],
): SpeciesOption => ({
  id,
  name,
  traits: [{ name: trait[0], summary: trait[1] }],
  extra_skill: false,
  granted_skills: [],
  details: {
    name_en,
    description,
    size: "Средний",
    speed: 30,
    speed_text,
    traits: [{ name: trait[0], summary: trait[1], mechanical: true }],
  },
});

/** Правила создания героя в уменьшенном виде: три класса, три расы, две предыстории. */
export const CREATION_OPTIONS: CreationOptions = {
  level: 3,
  abilities: [
    { id: "str", name: "Сила" },
    { id: "dex", name: "Ловкость" },
    { id: "con", name: "Телосложение" },
    { id: "int", name: "Интеллект" },
    { id: "wis", name: "Мудрость" },
    { id: "cha", name: "Харизма" },
  ],
  skills: SKILLS,
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
  classes: [
    classOption(
      "fighter",
      "Воин",
      10,
      "str",
      ["str", "con"],
      ["athletics", "acrobatics", "perception", "survival"],
      [
        [
          "Боевой стиль",
          "Один приём боя на выбор: оборона, стрельба или дуэль.",
        ],
        ["Второе дыхание", "Раз за привал воин сам залечивает часть ран."],
      ],
      ["Лёгкие доспехи", "Средние доспехи", "Тяжёлые доспехи", "Щиты"],
    ),
    classOption(
      "rogue",
      "Плут",
      8,
      "dex",
      ["dex", "int"],
      ["acrobatics", "sleight_of_hand", "stealth", "perception", "insight"],
      [
        ["Скрытая атака", "Бьёт больнее, когда враг отвлечён."],
        ["Воровской жаргон", "Тайный язык и знаки воров."],
      ],
      ["Лёгкие доспехи"],
    ),
    classOption(
      "wizard",
      "Волшебник",
      6,
      "int",
      ["int", "wis"],
      ["arcana", "history", "investigation", "religion"],
      [["Книга заклинаний", "Учит новые заклинания из найденных свитков."]],
      [],
    ),
  ],
  species: [
    speciesOption(
      "human",
      "Человек",
      "Human",
      "Люди живут почти везде и быстро учатся новому. Их сила — в упорстве.",
      "9 м",
      ["Находчивость", "Вдохновение после долгого привала."],
    ),
    speciesOption(
      "elf",
      "Эльф",
      "Elf",
      "Эльфы живут веками и помнят то, что другие давно забыли.",
      "9 м",
      ["Острые чувства", "Внимательность — навык с рождения."],
    ),
    speciesOption(
      "dwarf",
      "Дварф",
      "Dwarf",
      "Дварфы крепки, как камень их гор, и не забывают обид.",
      "9 м",
      ["Тёмное зрение", "Видит в темноте на 18 м."],
    ),
  ],
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
      id: "sailor",
      name: "Моряк",
      ability_options: ["str", "dex", "wis"],
      skills: ["athletics", "perception"],
      feat: "alert",
      equipment: [{ id: "rope", name: "Верёвка", qty: 1 }],
      details: {
        abilities: ["str", "dex", "wis"],
        skills: ["athletics", "perception"],
        tool: "Навигационные инструменты",
        feat: {
          id: "alert",
          name: "Бдительный",
          summary: "Бонус к инициативе.",
        },
        equipment: [{ id: "rope", name: "Верёвка", qty: 1 }],
      },
    },
  ],
  feats: [
    {
      id: "alert",
      name: "Бдительный",
      summary: "Бонус к инициативе.",
      details: { summary: "Бонус к инициативе." },
    },
  ],
  fighting_styles: [],
};

// --- конструктор ---

export const DRAFT_SUMMARIES: DraftSummary[] = [
  {
    id: "d-salt",
    title: ADVENTURE_TITLE,
    adventure_id: null,
    version: 7,
    updated_at: SERVER_TIME,
  },
];

const step = (status: Draft["steps"]["concept"]["status"]) => ({
  status,
  updated_at: status === "empty" ? null : SERVER_TIME,
});

/** Черновик приключения: замысел и мир готовы, боги в работе, остальное пусто. */
export const DRAFT: Draft = {
  id: "d-salt",
  version: 7,
  title: ADVENTURE_TITLE,
  adventure_id: null,
  journal_intro:
    "Староста Солёной бухты просит узнать, почему погас маяк и куда пропал смотритель.",
  settings: {
    pitch:
      "В ночь шторма маяк погас, а смотритель пропал. Рыбаки шепчутся о голосах из-под воды.",
    tone: "Мрачная морская сказка с юмором у костра",
    target_hours: 3,
    party_size: 4,
    party_level: 3,
    modules: [],
    profanity: false,
  },
  world: null,
  deities: [],
  factions: [],
  plot: null,
  locations: [],
  npcs: [],
  relations: [],
  encounters: [],
  monsters: [],
  items: [],
  revelations: [],
  start_location: null,
  finale_location: null,
  locks: ["step:concept"],
  wishes: {},
  steps: {
    concept: step("ready"),
    world: step("ready"),
    pantheon: step("draft"),
    factions: step("empty"),
    plot: step("empty"),
    locations: step("empty"),
    location_contents: step("empty"),
    npcs: step("empty"),
    relations: step("empty"),
    encounters: step("empty"),
    items: step("empty"),
    clues: step("empty"),
  },
  job: null,
};
