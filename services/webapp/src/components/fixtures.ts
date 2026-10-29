// Данные ТОЛЬКО для тестов экрана игры. Числа листа здесь выдуманы: настоящие считает движок.
import type {
  Character,
  CharacterDerived,
  CombatView,
  DerivedSkill,
  FeedItem,
  HeroFighter,
  JournalEntry,
  JournalSection,
  MonsterFighter,
  PartyMember,
  PhaseView,
  PlayerAction,
  RollData,
  Skill,
} from "../api/types";
import { previewHero } from "../screens/character/fixtures";
import { NO_LIVE, applyEvent } from "../store/game";
import type { GameView } from "../store/game";
import { gameSummary, lobbyPlayer, snapshotOf } from "../test-setup";

export const SERVER_TIME = "2026-10-20T12:00:00+00:00";

// --- лента ---

/** Запись ленты; по умолчанию — рассказ мастера. */
export function feedEntry(
  id: number,
  overrides: Partial<FeedItem> = {},
): FeedItem {
  return {
    id,
    kind: "narration",
    text: `Запись ${id}`,
    data: null,
    private: false,
    created_at: SERVER_TIME,
    ...overrides,
  };
}

/** Данные карточки броска; по умолчанию — проверка «Ловкость (Акробатика)»: 14 + 3 = 17 против Сл 15. */
export function rollData(overrides: Partial<RollData> = {}): RollData {
  return {
    actor_id: "pc_lira",
    actor: "Лира",
    label: "Ловкость (Акробатика)",
    reason: "Перепрыгнуть яму",
    expr: "1d20+3",
    rolls: [14],
    kept: [14],
    modifier: 3,
    total: 17,
    natural: 14,
    advantage: "none",
    dc: 15,
    success: true,
    ...overrides,
  };
}

/** Запись ленты с карточкой броска; `text` — запасной текст сервера. */
export function rollEntry(
  id: number,
  data: Partial<RollData> = {},
  overrides: Partial<FeedItem> = {},
): FeedItem {
  return feedEntry(id, {
    kind: "roll",
    text: "Лира: Ловкость (Акробатика) — 17 против Сл 15: успех",
    data: { ...rollData(data) },
    ...overrides,
  });
}

// --- фаза и бой ---

export function phaseOf(
  turn_phase: PhaseView["turn_phase"],
  overrides: Partial<PhaseView> = {},
): PhaseView {
  return {
    phase: "playing",
    turn_phase,
    deadline_at: null,
    remaining_seconds: null,
    scene: { type: "exploration", day: 1, part: "morning" },
    phase_since: null,
    can_regenerate: false,
    ...overrides,
  };
}

/** Герой в порядке хода: цел, в зале; id бойца совпадает с id героя. */
export function heroFighter(
  id: string,
  name: string,
  overrides: Partial<HeroFighter> = {},
): HeroFighter {
  return {
    id,
    name,
    kind: "pc",
    side: "party",
    zone_id: "hall",
    conditions: [],
    character_id: id,
    hp: 10,
    hp_max: 10,
    ...overrides,
  };
}

/** Монстр в порядке хода: цел, в зале; игрокам он описан словами, без чисел. */
export function monsterFighter(
  id: string,
  name: string,
  overrides: Partial<MonsterFighter> = {},
): MonsterFighter {
  return {
    id,
    name,
    kind: "monster",
    side: "enemies",
    zone_id: "hall",
    conditions: [],
    health: "цел",
    ...overrides,
  };
}

/** Бой из бойцов `order`, где сейчас ход `current_id`; зона по умолчанию одна — «Зал» со всеми. */
export function combatWith(
  current_id: string | null,
  order: CombatView["order"],
  overrides: Partial<CombatView> = {},
): CombatView {
  return {
    round: 1,
    current_id,
    order,
    zones: [
      {
        id: "hall",
        name: "Зал",
        combatants: order.map((fighter) => fighter.id),
      },
    ],
    ...overrides,
  };
}

/** Бой, где сейчас ход `current_id`: Лира (герой) и скелет (монстр). */
export function combatOf(current_id: string | null): CombatView {
  return combatWith(current_id, [
    heroFighter("pc_lira", "Лира", { hp: 9 }),
    monsterFighter("skeleton-1", "Скелет"),
  ]);
}

// --- герой ---

const SKILLS: [Skill, string, DerivedSkill["ability"], number][] = [
  ["athletics", "Атлетика", "str", -1],
  ["acrobatics", "Акробатика", "dex", 3],
  ["sleight_of_hand", "Ловкость рук", "dex", 5],
  ["stealth", "Скрытность", "dex", 7],
  ["arcana", "Магия", "int", 1],
  ["history", "История", "int", 1],
  ["investigation", "Анализ", "int", 1],
  ["nature", "Природа", "int", 1],
  ["religion", "Религия", "int", 1],
  ["animal_handling", "Уход за животными", "wis", 0],
  ["insight", "Проницательность", "wis", 0],
  ["medicine", "Медицина", "wis", 0],
  ["perception", "Внимательность", "wis", 2],
  ["survival", "Выживание", "wis", 0],
  ["deception", "Обман", "cha", 2],
  ["intimidation", "Запугивание", "cha", 2],
  ["performance", "Выступление", "cha", 2],
  ["persuasion", "Убеждение", "cha", 2],
];

/** Блок `derived` героя Лиры: плут с кинжалом (надет) и запасным луком. */
export function derivedOf(
  overrides: Partial<CharacterDerived> = {},
): CharacterDerived {
  return {
    armor_class: 14,
    proficiency_bonus: 2,
    passive_perception: 13,
    abilities: [
      { id: "str", name: "Сила", modifier: -1, save_bonus: -1 },
      { id: "dex", name: "Ловкость", modifier: 3, save_bonus: 5 },
      { id: "con", name: "Телосложение", modifier: 1, save_bonus: 1 },
      { id: "int", name: "Интеллект", modifier: 1, save_bonus: 3 },
      { id: "wis", name: "Мудрость", modifier: 0, save_bonus: 0 },
      { id: "cha", name: "Харизма", modifier: 2, save_bonus: 2 },
    ],
    skills: SKILLS.map(([id, name, ability, bonus]) => ({
      id,
      name,
      ability,
      bonus,
    })),
    spell_save_dc: null,
    spell_attack_bonus: null,
    attacks: [
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
    ],
    ...overrides,
  };
}

/** Лист игрока в игре: Лира, плут-эльф, 1 уровень, со всеми числами от «сервера». */
export function sheetHero(overrides: Partial<Character> = {}): Character {
  return previewHero({
    id: "pc_lira",
    name: "Лира",
    level: 1,
    hp: 9,
    hp_max: 10,
    hit_dice: 1,
    hit_dice_max: 1,
    skill_proficiencies: ["stealth", "sleight_of_hand", "perception"],
    expertise: ["stealth"],
    save_proficiencies: ["dex", "int"],
    inventory: [
      { item_id: "dagger", qty: 2, equipped: true },
      { item_id: "thieves_tools", qty: 1, equipped: false },
    ],
    derived: derivedOf(),
    ...overrides,
  });
}

export function partyMember(
  id: string,
  name: string,
  overrides: Partial<PartyMember> = {},
): PartyMember {
  return {
    id,
    name,
    player_id: null,
    hp: 10,
    hp_max: 10,
    temp_hp: 0,
    conditions: [],
    effects: [],
    ...overrides,
  };
}

export function myAction(overrides: Partial<PlayerAction> = {}): PlayerAction {
  return {
    text: "",
    secret: false,
    inspiration: false,
    status: "draft",
    ...overrides,
  };
}

// --- дневник ---

// Раздел по виду ссылки, как в таблице дополнения к контракту B8 (§1). Нужен только для удобства
// заглушек: на экране раздел всегда берётся из записи сервера.
const SECTION_OF_REF: Record<string, JournalSection> = {
  intro: "main",
  location: "places",
  npc: "people",
  deity: "gods",
  faction: "factions",
  region: "world",
  world: "world",
  rumor: "rumors",
  clue: "clues",
  item: "items",
};

/**
 * Запись дневника по контракту B8 (§2): по умолчанию общая, известная с начала игры, первой
 * ревизии. `id` собирается из `ref` и адресата, как у движка.
 */
export function journalEntry(
  ref: string,
  overrides: Partial<Omit<JournalEntry, "id" | "ref">> = {},
): JournalEntry {
  const audience = overrides.audience ?? "all";
  return {
    id: `${ref}@${audience}`,
    ref,
    section: SECTION_OF_REF[ref.split(":")[0]],
    title: `Запись ${ref}`,
    text: `Что известно о ${ref}.`,
    audience,
    turn: 0,
    revision: 1,
    visited_turn: null,
    updated_turn: null,
    ...overrides,
  };
}

/** Дневник в начале «Склепа»: «Главное», деревня и кладбище (известны с начала) и бог. */
export function startJournal(): JournalEntry[] {
  return [
    journalEntry("intro", {
      title: "Главное",
      text: "Староста Мирры просит узнать, кто тревожит покой склепа.",
    }),
    journalEntry("location:village", {
      title: "Деревня Мирра",
      text: "Тихая деревня у старого тракта.",
    }),
    journalEntry("location:cemetery", {
      title: "Старое кладбище",
      text: "За деревней, у холма со склепом.",
    }),
    journalEntry("deity:kelemvor", {
      title: "Келемвор",
      text: "Бог смерти, страж покоя усопших.",
    }),
  ];
}

// --- игра глазами игрока ---

/**
 * Игра в обсуждении хода: три игрока, хост — Вика (`u1`); герой Лира принадлежит `u1`.
 * Поля переопределяются точечно.
 */
export function gameView(overrides: Partial<GameView> = {}): GameView {
  const game = gameSummary({
    phase: "playing",
    turn_phase: "discussion",
    adventure_title: "Склеп забытого короля",
    players: [
      lobbyPlayer({
        user_id: "u1",
        name: "Вика",
        is_host: true,
        has_character: true,
        ready: true,
      }),
      lobbyPlayer({
        user_id: "u2",
        name: "Борис",
        has_character: true,
        ready: true,
      }),
      lobbyPlayer({
        user_id: "u3",
        name: "Мила",
        has_character: true,
        ready: true,
      }),
    ],
  });
  // Игру собирает тот же редьюсер, что и в приложении: тест видит ровно то, что видит экран.
  const { view } = applyEvent(
    NO_LIVE,
    snapshotOf(game, {
      phase: phaseOf("discussion"),
      statuses: { u1: "writing", u2: "writing", u3: "writing" },
      character: sheetHero(),
      party: [
        partyMember("pc_lira", "Лира", { player_id: "u1", hp: 9 }),
        partyMember("pc_boris", "Борин", { player_id: "u2" }),
      ],
    }),
    Date.parse(SERVER_TIME),
  );
  if (view === null) throw new Error("снимок не собрал игру");
  return { ...view, ...overrides };
}
