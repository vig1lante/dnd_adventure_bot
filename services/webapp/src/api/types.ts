// Типы ответов и тел запросов сервера. Источник правды — код сервера (auth/api.py,
// games/schemas.py, characters/*, turns/api.py, realtime/views.py).
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
  /**
   * Имя героя игрока; `null` — героя ещё нет. Его видят все. Старый сервер поля не присылает —
   * тогда имени просто нет.
   */
  hero_name?: string | null;
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
  /** Срок долгого привала (и прежнего привала), с. */
  camp_seconds: number;
  /** Срок короткого привала, с (`GameOut.short_rest_seconds`). */
  short_rest_seconds: number;
  /** Флажок «Мат и грубая речь»: его видят мастер и служебные роли нейронки. */
  profanity: boolean;
  players: PlayerSummary[];
  /** Когда создана игра, ISO с `+00:00`; старый сервер его не присылает. */
  created_at?: string;
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
  short_rest_seconds?: number;
  /** Без поля сервер включает мат (по умолчанию `true`). */
  profanity?: boolean;
}

/** Вариант нейронки для игры из `GET /api/providers` (только создателям игр; секретов в нём нет). */
export interface ProviderOption {
  /** Значение поля `provider` в `CreateGameBody`. */
  id: string;
  title: string;
  /** Игру с этим провайдером можно создать прямо сейчас (настроены ключи и адреса). */
  available: boolean;
  default_model: string | null;
  /** Известные модели; у `claude_code_cli` сервер принимает `model` только из этого списка. */
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

// --- подробности вариантов для карточек (`details`) ---
// Верхние поля вариантов — для логики формы, `details` — для подробной карточки: часть данных
// нарочно повторяется. Коды характеристик и навыков названия не несут: их берут из
// `CreationOptions.abilities` и `.skills`. Тексты — по-русски, расстояния — только в метрах.

/** Черта расы. `mechanical` — действует в бою (её учитывает движок). */
export interface TraitDetail {
  name: string;
  summary: string;
  mechanical: boolean;
}

export interface SpeciesDetails {
  /** Английское название расы. */
  name_en: string;
  /** 2–3 предложения о расе. */
  description: string;
  /** Подпись сервера: «Средний» или «Маленький». */
  size: string;
  /** Футы, число правил: игроку не показывается. */
  speed: number;
  /** Скорость текстом в метрах («9 м»): единственное, что видит игрок. */
  speed_text: string;
  traits: TraitDetail[];
}

/** Особенность класса до уровня героя (`CreationOptions.level`). */
export interface ClassFeatureDetail {
  level: number;
  name: string;
  summary: string;
  mechanical: boolean;
}

export interface ClassDetails {
  hit_die: number;
  /** Основная характеристика класса. */
  primary: Ability;
  saves: Ability[];
  /** Русские подписи; пустой список — класс без доспехов. */
  armor: string[];
  weapons: string[];
  skill_choices: { count: number; from: Skill[] };
  features: ClassFeatureDetail[];
  /** Числа на уровне героя; `null` — класс без магии. */
  spellcasting: { ability: Ability; cantrips: number; prepared: number } | null;
}

export interface BackgroundDetails {
  /** Три характеристики для прибавки. */
  abilities: Ability[];
  skills: Skill[];
  /** Владение инструментом, русский текст. */
  tool: string;
  feat: { id: string; name: string; summary: string };
  equipment: ItemLine[];
}

export interface SkillDetails {
  ability: Ability;
  /** Что проверяет навык и примеры применения. */
  summary: string;
}

export interface SpellDetails {
  /** Подпись школы магии. */
  school: string;
  casting_time: string;
  /** Дистанция текстом в метрах. */
  range_text: string;
  duration: string;
  summary: string;
}

/** Подробности черты и боевого стиля. */
export interface SummaryDetails {
  summary: string;
}

export interface SkillOption {
  id: Skill;
  name: string;
  ability: Ability;
  details: SkillDetails;
}

/** Черта предыстории или боевой стиль. */
export interface Described {
  id: string;
  name: string;
  summary: string;
  details: SummaryDetails;
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
  details: SpellDetails;
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
  details: ClassDetails;
}

export interface SpeciesOption {
  id: string;
  name: string;
  traits: FeatureOption[];
  /** Нужен навык на выбор (Умелость человека). */
  extra_skill: boolean;
  granted_skills: Skill[];
  details: SpeciesDetails;
}

export interface BackgroundOption {
  id: string;
  name: string;
  ability_options: Ability[];
  skills: Skill[];
  /** Id черты из `feats`. */
  feat: string;
  equipment: ItemLine[];
  details: BackgroundDetails;
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
  | "camp-end"
  // Те же «+1 мин» и «Закончить» для привала любого вида.
  | "rest-extend"
  | "rest-end";

/** Вид привала: короткий (час, кости хитов) или долгий (до утра). */
export type RestKind = "short" | "long";

/** Ответ на «Потратить кость хитов»: остаток костей и хиты героя считает сервер. */
export interface HitDieResult {
  hit_dice: number;
  hp: number;
  hp_max: number;
}

// --- снимок и события сокета ---

/**
 * Статус хода для всех: что в ходе написано и тайный ли он, статус не говорит. `idle` — ход ещё не
 * начат (строки хода нет или в черновике нет видимого текста), `writing` — черновик с текстом,
 * `ready` — сдан (вручную или по таймеру), `pass` — пас.
 */
export type PlayerStatus = "idle" | "writing" | "ready" | "pass";

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
   * КД героя-цели. Сервер кладёт его только в атаку монстра по герою (решение владельца):
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
  /**
   * Серверное время начала текущей фазы хода, ISO с `+00:00`. `null` — игра началась на старом
   * сервере, и он его не знает; у старого сервера поля нет вовсе.
   */
  phase_since?: string | null;
  /**
   * Примет ли сервер «Перегенерировать ответ мастера» сейчас (то же правило, что у сервера).
   * Без поля (старый сервер) кнопки нет.
   */
  can_regenerate?: boolean;
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

/** Раздел дневника. */
export type JournalSection =
  | "main"
  | "places"
  | "people"
  | "gods"
  | "factions"
  | "world"
  | "rumors"
  | "clues"
  | "items";

/**
 * Запись дневника отряда. Сервер присылает только то, что игроку можно видеть: общие записи
 * и личные записи его героя; тайн в тексте нет.
 */
export interface JournalEntry {
  /** `<ref>@<audience>`: одна запись на сущность и адресата. */
  id: string;
  /** Ссылка на сущность: `intro`, `location:<id>`, `npc:<id>`, `rumor:<n>`, `world` и т.п. */
  ref: string;
  section: JournalSection;
  title: string;
  /** Дополнения идут новыми абзацами через пустую строку. */
  text: string;
  /** `"all"` — весь отряд, иначе id героя, которому запись адресована. */
  audience: string;
  /** Раунд, когда запись появилась; 0 — начало игры. */
  turn: number;
  /** 1 при создании, растёт с каждым дополнением и с первым приходом отряда в место. */
  revision: number;
  /** Только у мест: раунд, когда отряд впервые пришёл сюда; иначе `null`. */
  visited_turn: number | null;
  /**
   * Раунд последнего дополнения текста; первый приход в место его не ставит. `null` — текст не
   * дополняли; у старого сервера поля нет.
   */
  updated_turn?: number | null;
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
  /** Дневник глазами игрока, в порядке появления записей. */
  journal: JournalEntry[];
  /** Идущий привал; вне фазы хода `camp` — `null`. */
  rest: RestView | null;
}

/**
 * Привал глазами игрока. Есть только в фазе хода `camp`, иначе `null`. Тайн в нём нет: идеи —
 * только героя смотрящего.
 */
export interface RestView {
  kind: RestKind;
  /** Срок привала; на паузе `null`, остаток — в `phase.remaining_seconds`. */
  deadline_at: string | null;
  /** Идеи «расскажи о себе» для своего героя; их видит только адресат. */
  my_ideas: string[];
  /** Реплики у костра этого привала — записи ленты `camp_line`. */
  lines: FeedItem[];
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
  /** Новая или дополненная запись дневника: заменяет запись с тем же `id` или добавляется. */
  | { type: "journal_entry"; entry: JournalEntry }
  /** Дневник целиком (откат хода, восстановление): заменяет прежний. */
  | { type: "journal"; entries: JournalEntry[] }
  /** Начался привал (всем): вид и срок. Идеи и реплики приходят своими кадрами. */
  | { type: "rest_started"; kind: RestKind; deadline_at: string | null }
  /** Идея для рассказа — только игроку героя-адресата. */
  | { type: "rest_idea"; text: string }
  /** Привал кончился (всем), мастер рассказал его итог. */
  | { type: "rest_ended"; kind: RestKind }
  /** Черновик автора сдан по таймеру — только автору, без текста. */
  | { type: "action_auto_submitted" }
  /**
   * Хост исключил игрока — только его соединениям, следом сокет закрывается кодом 4403 с причиной
   * `kicked`. Его разбирает подключение (`api/ws.ts`), до игры он не доходит.
   */
  | { type: "kicked" }
  | { type: "error"; detail: string }
  | { type: "pong" };

// --- конструктор приключений ---

/** Шаги генератора по порядку; они же ключи `steps`, `wishes` и замков `step:<шаг>`. */
export type AuthoringStep =
  | "concept"
  | "world"
  | "pantheon"
  | "factions"
  | "plot"
  | "locations"
  | "location_contents"
  | "npcs"
  | "relations"
  | "encounters"
  | "items"
  | "clues";

/**
 * Разделы, которые принимает `PUT /api/drafts/{id}/sections/{section}`: все 12.
 */
export type DraftSection =
  | "settings"
  | "world"
  | "deities"
  | "factions"
  | "plot"
  | "locations"
  | "npcs"
  | "relations"
  | "items"
  | "encounters"
  | "monsters"
  | "revelations";

/** Статус шага считает сервер: «пусто / черновик / готово». */
export type StepStatus = "empty" | "draft" | "ready";

export interface StepState {
  status: StepStatus;
  /** Последняя запись раздела этого шага (руками или генерацией); `null` — не было. */
  updated_at: string | null;
}

export type JobStatus = "queued" | "running" | "done" | "failed";

/** Фоновое задание генерации шага. */
export interface Job {
  id: string;
  step: AuthoringStep;
  /** Ссылка на одну сущность (`<вид>:<id>`); `null` — шаг целиком. */
  target: string | null;
  status: JobStatus;
  /** Безопасный текст сбоя по-русски; только у `failed`. */
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

/** Поля дневника отряда у сущностей: что знают игроки и известно ли с начала. */
export interface JournalFields {
  public: string;
  known_at_start: boolean;
}

export interface Region extends JournalFields {
  id: string;
  name: string;
  description: string;
}

export interface Rumor extends JournalFields {
  text: string;
  /** Правдив ли слух: знает только мастер. */
  true: boolean;
}

export interface WorldInfo {
  history: string;
  regions: Region[];
  rumors: Rumor[];
}

export interface Deity extends JournalFields {
  id: string;
  name: string;
  title: string;
  domains: string[];
  symbol: string;
  dogma: string;
  appearance: string;
  attitude_to_mortals: string;
  secret: string;
  /** id статблока аватара (монстр SRD или свой); `null` — аватара нет. */
  avatar_statblock: string | null;
}

export interface Faction extends JournalFields {
  id: string;
  name: string;
  goal: string;
  methods: string;
  /** Ссылка на NPC-лидера: `npc:<id>`. */
  leader: string | null;
  /** id локации-базы. */
  base_location: string | null;
  secret: string;
  /** Отношение к отряду, ступени от −3 до 3. */
  attitude_to_party: number;
}

export interface Act {
  id: string;
  title: string;
  summary: string;
  key_scenes: string[];
}

export interface Plot {
  acts: Act[];
  endings: string[];
  /** Злодей сюжета: `npc:<id>` или `deity:<id>`. */
  villain: string | null;
}

/** Проверка, которой достают находку места или раскрывают тайну: Сл — только из таблицы. */
export interface CheckSpec {
  ability: Ability;
  skill: Skill | null;
  dc: number;
}

export type RevealKind = "clue" | "item" | "flag" | "exit";

/** Что раскрывает находка: улика, предмет (тайник), флаг или выход. */
export interface RevealRef {
  kind: RevealKind;
  id: string;
  value: string | number | boolean | null;
}

/** Деталь локации; с `reveals: item` — тайник, в котором лежит предмет этой локации. */
export interface Feature {
  id: string;
  name: string;
  description: string;
  check: CheckSpec | null;
  reveals: RevealRef[];
  hidden: boolean;
  /** Замок: `item:<id>` или `flag:<имя>`. */
  locked_by: string | null;
}

export interface Exit {
  id: string;
  /** id локации, куда ведёт выход. */
  to: string;
  direction: string;
  description: string;
  /** Замок: `item:<id>` или `flag:<имя>`. */
  locked_by: string | null;
  /** Сл поиска скрытого выхода (из таблицы); `null` — выход не скрыт. */
  hidden_dc: number | null;
}

/** Тайна места для мастера. */
export interface LocationSecret {
  id: string;
  text: string;
  check: CheckSpec | null;
  reveals: RevealRef[];
}

export interface Zone {
  id: string;
  name: string;
}

export interface ZoneLink {
  a: string;
  b: string;
  distance: "near" | "far";
}

export interface Location extends JournalFields {
  id: string;
  name: string;
  region: string;
  read_aloud: string;
  dm_notes: string;
  exits: Exit[];
  features: Feature[];
  secrets: LocationSecret[];
  /** id боя черновика; `null` — боя нет. */
  encounter_id: string | null;
  zones: Zone[];
  links: ZoneLink[];
}

/** Данные раздела `locations`: локации и два поля верхнего уровня черновика. */
export interface LocationsSection {
  locations: Location[];
  start_location: string | null;
  finale_location: string | null;
}

export interface Npc extends JournalFields {
  id: string;
  name: string;
  role: string;
  appearance: string;
  manner: string;
  motive: string;
  secret: string;
  knows: string[];
  /** Отношение к отряду, ступени от −3 до 3. */
  attitude: number;
  /** Связи формата v1: в конструкторе всегда пусто, связи — в разделе `relations`. */
  relations: unknown[];
  location_id: string;
  /** id статблока (монстр SRD или свой); `null` — не дерётся. */
  statblock: string | null;
}

/** Вид связи; `other` — с пояснением в `note`. */
export type RelationKind =
  | "kin"
  | "spouse"
  | "lover"
  | "friend"
  | "ally"
  | "rival"
  | "enemy"
  | "debtor"
  | "patron"
  | "servant"
  | "worships"
  | "betrayed"
  | "seeks"
  | "other";

/** Связь графа «a — <вид> для b»: концы — `npc:`, `faction:`, `deity:` или `location:<id>`. */
export interface Relation {
  id: string;
  a: string;
  b: string;
  kind: RelationKind;
  note: string;
  /** Игроки о связи не знают. */
  secret: boolean;
  /** Связь верна и в обратную сторону. */
  mutual: boolean;
}

export interface Item extends JournalFields {
  id: string;
  name: string;
  description: string;
  /** id предмета SRD. */
  base_item: string | null;
  /** Где лежит: `location:<id>`, `npc:<id>`… */
  holder: string;
}

/** Группа монстров боя: монстр SRD или свой статблок, сколько их и в какой зоне локации. */
export interface MonsterGroup {
  monster_id: string;
  count: number;
  zone: string;
}

/** Бой черновика (`Encounter` движка). */
export interface Encounter {
  id: string;
  /** id локации, где идёт бой. */
  location_id: string;
  /** Что запускает бой: знает только мастер. */
  trigger: string;
  tactics: string;
  monsters: MonsterGroup[];
  /** Зона локации, где бой застаёт героев. */
  pc_zone: string;
  /** Финальный или ключевой бой. */
  boss: boolean;
}

export type MonsterActionKind = "attack" | "save" | "multiattack" | "other";

export type ActionRange = "melee" | "near" | "far";

/** Действие статблока (`MonsterAction` движка): данные статблока; бросает и считает сервер. */
export interface MonsterAction {
  id: string;
  name: string;
  kind: MonsterActionKind;
  attack_bonus: number | null;
  /** Запись костей урона, например «1d6+2». */
  damage: string | null;
  damage_type: string | null;
  range: ActionRange;
  save: Ability | null;
  dc: number | null;
  half_on_save: boolean;
  targets: number;
  condition: string | null;
  /** id своих действий, которые мультиатака делает подряд. */
  multiattack: string[];
  summary: string;
  reach: boolean;
  secondary_damage: string | null;
  secondary_damage_type: string | null;
}

/** Свой статблок (`MonsterDef` движка). Скорость — в футах, как в правилах; экран показывает метры. */
export interface MonsterDef {
  id: string;
  name: string;
  name_en: string;
  cr: number;
  xp: number;
  ac: number;
  hp: number;
  /** Запись костей хитов, например «2d8+4». */
  hp_dice: string;
  speed: number;
  scores: Record<Ability, number>;
  saves: Partial<Record<Ability, number>>;
  skills: Partial<Record<Skill, number>>;
  resistances: string[];
  vulnerabilities: string[];
  immunities: string[];
  actions: MonsterAction[];
  traits: string[];
  tags: string[];
}

export interface Clue {
  id: string;
  description: string;
  /** Где лежит: `feature:<id>`, `npc:<id>` или `item:<id>`. */
  where: string;
}

/** Откровение: вывод, к которому игроки приходят по уликам. */
export interface Revelation {
  id: string;
  statement: string;
  clues: Clue[];
}

/** Строка справочника `GET /api/srd/monsters`. */
export interface SrdMonster {
  id: string;
  name: string;
  cr: number;
  xp: number;
  tags: string[];
}

/** Находка проверки черновика: `ref` — где найдена, `step` — шаг, который за неё отвечает. */
export interface Finding {
  code: string;
  message: string;
  ref: string;
  step: AuthoringStep | null;
}

/** Ответ `POST /api/drafts/{id}/validate`. */
export interface DraftReport {
  errors: Finding[];
  warnings: Finding[];
  score: number;
  estimated_minutes: number;
  readiness_percent: number;
}

/** Ответ `POST /api/drafts/{id}/publish`: 201 — первая публикация, 200 — повторная. */
export interface PublishResult {
  adventure_id: string;
  published_at: string;
}

export type RefKind =
  "deity" | "faction" | "npc" | "location" | "item" | "encounter";

/** Строка справочника ссылок `GET /api/drafts/{id}/refs`, без тайн. */
export interface DraftRef {
  /** `<вид>:<id>`, как у закреплений. */
  ref: string;
  kind: RefKind;
  name: string;
}

/** Заказ автора: завязка, тон, длина игры, отряд, модули правил и флажок мата. */
export interface DraftSettings {
  pitch: string;
  tone: string;
  target_hours: number;
  party_size: number;
  party_level: number;
  modules: string[];
  profanity: boolean;
}

/** Данные раздела `settings`: настройки и три поля верхнего уровня черновика. */
export interface SettingsSection extends DraftSettings {
  title: string;
  journal_intro: string;
  /** id приключения; `null` — не задан. После публикации сервер его не меняет. */
  adventure_id: string | null;
}

/** Черновик приключения. */
export interface Draft {
  /** id записи черновика, не slug приключения. */
  id: string;
  version: number;
  title: string;
  /** slug приключения: его даёт генерация замысла или автор; `null` — не задан. */
  adventure_id: string | null;
  journal_intro: string;
  settings: DraftSettings;
  world: WorldInfo | null;
  deities: Deity[];
  factions: Faction[];
  plot: Plot | null;
  locations: Location[];
  npcs: Npc[];
  relations: Relation[];
  encounters: Encounter[];
  /** Свои статблоки черновика. */
  monsters: MonsterDef[];
  items: Item[];
  revelations: Revelation[];
  start_location: string | null;
  finale_location: string | null;
  /** Закрепления: `<вид>:<id>` или `step:<шаг>`. */
  locks: string[];
  wishes: Partial<Record<AuthoringStep, string>>;
  steps: Record<AuthoringStep, StepState>;
  /** Активное или последнее задание черновика. */
  job: Job | null;
}

/** Строка списка черновиков (`GET /api/drafts`), новые сверху. */
export interface DraftSummary {
  id: string;
  title: string;
  adventure_id: string | null;
  version: number;
  updated_at: string;
  /**
   * id приключения первой публикации черновика и её время, ISO; `null` — черновик не
   * публиковали. У старого сервера полей нет.
   */
  published_id?: string | null;
  published_at?: string | null;
}

export interface GenerateBody {
  step: AuthoringStep;
  wish?: string;
  target?: string;
}
