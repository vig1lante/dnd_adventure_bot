// Типы ответов и тел запросов сервера. Источник правды — код сервера (auth/api.py,
// games/schemas.py, characters/*, turns/api.py, realtime/views.py), а не спека.
// Клиент не проверяет форму ответов во время работы: он им верит, как контракту.
// Числа, броски и HP клиент только показывает — считает их сервер.

// --- вход и конфигурация ---

export type DevLogin =
  "dev-1" | "dev-2" | "dev-3" | "dev-4" | "dev-5" | "dev-6";

export interface User {
  id: string;
  name: string;
  /** Подсказка для интерфейса: право создавать игры проверяет сервер (403). */
  can_create_games: boolean;
}

export interface LoginResult {
  token: string;
  user: User;
  /** Параметр запуска Mini App (`startapp=<код игры>`); при dev-входе его нет. */
  start_param: string | null;
}

export interface AppConfig {
  env: "dev" | "prod";
  dev_login: boolean;
  bot_username: string | null;
}

// --- игры и лобби ---

export type GamePhase = "lobby" | "weaving" | "playing" | "paused" | "finished";

export type TurnPhase =
  | "discussion"
  | "collecting"
  | "resolving"
  | "dm_failed"
  | "combat_wait"
  | "camp";

export interface PlayerSummary {
  user_id: string;
  name: string;
  is_host: boolean;
  ready: boolean;
  has_character: boolean;
}

export interface GameSummary {
  id: string;
  code: string;
  adventure_id: string;
  adventure_title: string;
  phase: GamePhase;
  turn_phase: TurnPhase | null;
  /** Пусто — значения по умолчанию из env сервера. */
  provider: string | null;
  model: string | null;
  collect_seconds: number;
  hurry_seconds: number;
  camp_seconds: number;
  players: PlayerSummary[];
}

/** Лобби — та же сводка игры: её же присылает событие `lobby`. */
export type Lobby = GameSummary;

export interface AdventureSummary {
  id: string;
  title: string;
  pitch: string;
  target_hours: number;
  score: number;
  /** Блокирующие ошибки: игру на таком приключении создать нельзя. */
  blocking: boolean;
}

export interface CreateGameBody {
  adventure_id: string;
  provider?: string;
  model?: string;
  collect_seconds?: number;
  hurry_seconds?: number;
  camp_seconds?: number;
}

/** Вариант нейронки для игры из `GET /api/providers` (только создателям игр; секретов в нём нет). */
export interface ProviderOption {
  /** Значение поля `provider` в `CreateGameBody`. */
  id: string;
  title: string;
  /** Игру с этим провайдером можно создать прямо сейчас (настроены ключи и адреса). */
  available: boolean;
  default_model: string | null;
  models: string[];
  /** Учитывает ли провайдер поле `model`; иначе модель задаёт он сам. */
  accepts_model: boolean;
  /** Этим провайдером ведётся игра без переопределения (настройка сервера). */
  is_default: boolean;
}

export interface ProvidersList {
  providers: ProviderOption[];
}

// --- персонаж ---

export type Ability = "str" | "dex" | "con" | "int" | "wis" | "cha";

export type Skill =
  | "athletics"
  | "acrobatics"
  | "sleight_of_hand"
  | "stealth"
  | "arcana"
  | "history"
  | "investigation"
  | "nature"
  | "religion"
  | "animal_handling"
  | "insight"
  | "medicine"
  | "perception"
  | "survival"
  | "deception"
  | "intimidation"
  | "performance"
  | "persuasion";

/** Состояние или эффект в белом списке сервера: без источника и Сл. */
export interface EffectView {
  id: string;
  name: string;
  rounds_left: number | null;
}

export interface Persona {
  concept: string;
  appearance: string;
  traits: string;
  ideals: string;
  bonds: string;
  flaws: string;
  backstory: string;
}

export interface InventoryItem {
  item_id: string;
  qty: number;
  equipped: boolean;
  /** Название предмета приключения; у предметов SRD и неизвестных id ключа нет вовсе (не `null`). */
  name?: string;
}

/** Строка характеристики из блока `derived`: модификатор и бонус спасброска уже посчитаны движком. */
export interface DerivedAbility {
  id: Ability;
  name: string;
  modifier: number;
  save_bonus: number;
}

/** Строка навыка из блока `derived`: бонус включает мастерство и компетентность. */
export interface DerivedSkill {
  id: Skill;
  name: string;
  ability: Ability;
  bonus: number;
}

/** Дальность оружия: вплотную, рядом, далеко. */
export type WeaponRange = "melee" | "near" | "far";

/** Атака оружием из инвентаря; `equipped` отличает надетое от запасного. */
export interface DerivedAttack {
  item_id: string;
  name: string;
  equipped: boolean;
  bonus: number;
  /** Кости урона записью движка, например `2d6+3`. */
  damage: string;
  /** Один из 13 английских id типов урона; русскую подпись даёт клиент. */
  damage_type: string;
  range: WeaponRange;
  ability: Ability;
}

/**
 * Числа листа, которые считает движок: клиент их только показывает (инвариант «нейронка
 * рассказывает, код считает»). Блок есть у листа владельца в снимке и в событии `character`;
 * в предпросмотре случайного героя, в отряде и у чужих героев его нет.
 */
export interface CharacterDerived {
  armor_class: number;
  proficiency_bonus: number;
  passive_perception: number;
  /** Шесть строк в порядке str, dex, con, int, wis, cha. */
  abilities: DerivedAbility[];
  /** Восемнадцать навыков в порядке движка. */
  skills: DerivedSkill[];
  /** У героя без заклинаний — `null`. */
  spell_save_dc: number | null;
  spell_attack_bonus: number | null;
  attacks: DerivedAttack[];
}

/** Лист персонажа: его видит только владелец; числа правил лежат в `derived`. */
export interface Character {
  id: string;
  name: string;
  level: number;
  species: string;
  class_id: string;
  background: string;
  scores: Record<Ability, number>;
  skill_proficiencies: Skill[];
  expertise: Skill[];
  save_proficiencies: Ability[];
  fighting_style: string | null;
  hp_max: number;
  hp: number;
  temp_hp: number;
  hit_dice_max: number;
  hit_dice: number;
  /** Ключи — уровни заклинаний строками ("1", "2"): так их отдаёт JSON. */
  spell_slots_max: Record<string, number>;
  spell_slots: Record<string, number>;
  cantrips: string[];
  prepared_spells: string[];
  concentration: string | null;
  inventory: InventoryItem[];
  conditions: EffectView[];
  buffs: EffectView[];
  resources: Record<string, number>;
  inspiration: boolean;
  death_saves: { successes: number; failures: number; stable: boolean };
  persona: Persona;
  player_id: string | null;
  derived?: CharacterDerived;
}

export interface AbilityOption {
  id: Ability;
  name: string;
}

export interface SkillOption {
  id: Skill;
  name: string;
  ability: Ability;
}

export interface Described {
  id: string;
  name: string;
  summary: string;
}

export interface FeatureOption {
  name: string;
  summary: string;
}

export interface ItemLine {
  id: string;
  name: string;
  qty: number;
}

export interface SpellOption {
  id: string;
  name: string;
  level: number;
  summary: string;
}

export interface SpellcastingOption {
  ability: Ability;
  /** Сколько заговоров выбрать. */
  cantrips: number;
  /** Сколько заклинаний выбрать. */
  prepared: number;
  cantrip_options: SpellOption[];
  spell_options: SpellOption[];
}

export interface ClassOption {
  id: string;
  name: string;
  hit_die: number;
  saving_throws: Ability[];
  skill_choices: number;
  skill_options: Skill[];
  /** 0 — у класса нет компетентности. */
  expertise_picks: number;
  fighting_styles: string[];
  starting_equipment: ItemLine[];
  features: FeatureOption[];
  spellcasting: SpellcastingOption | null;
}

export interface SpeciesOption {
  id: string;
  name: string;
  traits: FeatureOption[];
  /** Нужен навык на выбор (Умелость человека). */
  extra_skill: boolean;
  granted_skills: Skill[];
}

export interface BackgroundOption {
  id: string;
  name: string;
  ability_options: Ability[];
  skills: Skill[];
  /** Id черты из `feats`. */
  feat: string;
  equipment: ItemLine[];
}

export interface CreationOptions {
  level: number;
  abilities: AbilityOption[];
  skills: SkillOption[];
  standard_array: number[];
  point_buy: {
    budget: number;
    /** Значение характеристики (строкой) -> цена в очках. */
    costs: Record<string, number>;
  };
  background_bonus_shapes: number[][];
  classes: ClassOption[];
  species: SpeciesOption[];
  backgrounds: BackgroundOption[];
  feats: Described[];
  fighting_styles: Described[];
}

/** Способы задать характеристики. Броска нет: решение владельца, все способы — ровно 27 очков. */
export type AbilityMethod = "point_buy" | "standard_array";

/** Выбор игрока в подробном режиме: числа персонажа из него считает сервер. */
export interface CharacterChoices {
  name: string;
  species: string;
  class_id: string;
  background: string;
  method: AbilityMethod;
  base_scores: Record<Ability, number>;
  background_bonus: Partial<Record<Ability, number>>;
  skills: Skill[];
  extra_skill?: Skill | null;
  expertise?: Skill[];
  fighting_style?: string | null;
  cantrips?: string[];
  prepared_spells?: string[];
  persona?: Partial<Persona>;
}

export type CreateCharacterBody =
  | { mode: "detailed"; choices: CharacterChoices }
  | {
      mode: "quick";
      class_id: string;
      species: string;
      concept: string;
      name?: string;
    }
  | {
      mode: "random";
      /** Id предпросмотра («Беру»): создаётся ровно показанный герой, без нового броска. */
      preview_id?: string;
    };

/**
 * Предпросмотр случайного героя (`POST .../characters/random-preview`): сервер бросает
 * и запоминает героя, в базу ничего не пишет. Имя и описание в нём ещё пустые — их пишет
 * нейронка после «Беру». Русские названия клиент берёт из `CreationOptions`.
 */
export interface RandomPreview {
  preview_id: string;
  character: Character;
}

/**
 * Ответ `POST .../characters/random-abilities` («Распределить случайно»): сервер случайно
 * раскладывает те же 27 очков по классу героя. Клиент кладёт `scores` в покупку очков как есть.
 */
export interface RandomAbilities {
  method: "point_buy";
  /** Значения 8–15 до прибавки предыстории. */
  scores: Record<Ability, number>;
}

/** Выбор игрока в быстром режиме, как его хранит сервер; `name: null` — имя не давали. */
export interface QuickChoices {
  class_id: string;
  species: string;
  concept: string;
  name: string | null;
}

/**
 * Ответ `GET .../characters/mine`: свой герой и выбор, по которому он создан («Изменить героя»).
 * У случайного героя выбора нет (`choices: null`), у героя из игры до миграции нет и способа
 * (`mode: null`): форма тогда открывается пустой.
 */
export type MyCharacter = { character: Character } & (
  | { mode: "detailed"; choices: CharacterChoices }
  | { mode: "quick"; choices: QuickChoices }
  | { mode: "random" | null; choices: null }
);

export interface CreatedCharacter {
  character: Character;
  /** Русский текст, если описание от модели не получилось; персонаж при этом сохранён. */
  flavor_error: string | null;
}

export interface BackstoryHelpBody {
  class_id: string;
  species: string;
  concept?: string;
  draft?: string;
}

// --- ход игрока и кнопки хоста ---

export type ActionStatus =
  "draft" | "submitted" | "pass" | "auto_pass" | "auto_submitted";

export interface ActionBody {
  text: string;
  secret?: boolean;
  inspiration?: boolean;
}

/** Ход игрока, как его видит автор (тот же вид, что `action` снимка). */
export interface PlayerAction {
  text: string;
  secret: boolean;
  inspiration: boolean;
  status: ActionStatus;
}

export type HostAction =
  | "collect"
  | "extend"
  | "close"
  | "pause"
  | "resume"
  | "retry"
  | "regenerate"
  | "hurry"
  | "camp-extend"
  | "camp-end";

// --- снимок и события сокета ---

/** Статус хода для всех: что в ходе написано и тайный ли он, статус не говорит. */
export type PlayerStatus = "writing" | "ready" | "pass";

export type FeedKind =
  "narration" | "roll" | "private" | "system" | "camp_line" | "action";

export interface FeedItem {
  id: number;
  kind: FeedKind;
  text: string;
  /** Данные карточки (для `roll` — `RollData`); `null` у обычных записей. */
  data: Record<string, unknown> | null;
  /** Личное сообщение: адресата клиент не получает, только этот признак. */
  private: boolean;
  created_at: string;
}

/** Данные карточки броска (`FeedItem.data` у `kind: "roll"`). */
export interface RollData {
  /** Нет у NPC и у монстра, спрятанного от отряда. */
  actor_id?: string;
  actor: string;
  label: string;
  reason: string;
  expr: string;
  rolls: number[];
  kept: number[];
  modifier: number;
  total: number;
  natural: number | null;
  advantage: "none" | "advantage" | "disadvantage";
  dc: number | null;
  success: boolean | null;
  /**
   * КД героя-цели. Сервер кладёт его только в атаку монстра по герою (решение владельца R2):
   * КД монстров не передаётся никогда.
   */
  target_ac?: number;
}

export interface SceneView {
  type: "exploration" | "social" | "combat" | "camp";
  day: number;
  part: "morning" | "day" | "evening" | "night";
}

/** Фаза игры и отсчёт. На паузе срока нет, а остаток лежит в `remaining_seconds`. */
export interface PhaseView {
  phase: GamePhase;
  turn_phase: TurnPhase | null;
  deadline_at: string | null;
  remaining_seconds: number | null;
  scene: SceneView | null;
}

export interface PartyMember {
  id: string;
  name: string;
  player_id: string | null;
  hp: number;
  hp_max: number;
  temp_hp: number;
  conditions: EffectView[];
  effects: EffectView[];
}

interface FighterBase {
  id: string;
  name: string;
  side: "party" | "enemies";
  zone_id: string;
  conditions: EffectView[];
}

export interface HeroFighter extends FighterBase {
  kind: "pc";
  character_id: string;
  hp: number | null;
  hp_max: number | null;
  /**
   * Может ли герой действовать (не парализован, не без сознания и т.п.) — решение движка. Сервер
   * ждёт ход игрока, только если он `true`; ключа может не быть у монстров и в старых снимках.
   */
  can_act?: boolean;
}

/** Монстр описан словами: точные HP игрокам не показываются. */
export interface MonsterFighter extends FighterBase {
  kind: "monster";
  health: "цел" | "ранен" | "тяжело ранен" | "повержен";
}

/** Боец порядка хода: герой или монстр. */
export type Fighter = HeroFighter | MonsterFighter;

export interface CombatView {
  round: number;
  /** Пусто, если сейчас ход спрятанного монстра. */
  current_id: string | null;
  order: Fighter[];
  zones: { id: string; name: string; combatants: string[] }[];
}

export interface Snapshot {
  type: "snapshot";
  /** Серверное время: отсчёты считаются от него, а не от часов телефона. */
  server_time: string;
  game: GameSummary;
  host_user_id: string;
  phase: PhaseView;
  statuses: Record<string, PlayerStatus>;
  character: Character | null;
  action: PlayerAction | null;
  party: PartyMember[];
  combat: CombatView | null;
  feed: FeedItem[];
}

/** Запись ленты снята («Перегенерировать»): клиент убирает её по `id`. */
export interface FeedWithdrawal {
  id: number;
  superseded: true;
}

export type WsEvent =
  | Snapshot
  | { type: "phase"; phase: PhaseView }
  | { type: "player_status"; user_id: string; status: PlayerStatus }
  | { type: "feed_item"; item: FeedItem | FeedWithdrawal }
  | { type: "party"; party: PartyMember[] }
  | { type: "combat"; combat: CombatView | null }
  | { type: "character"; character: Character }
  | { type: "lobby"; game: GameSummary }
  | { type: "error"; detail: string }
  | { type: "pong" };
