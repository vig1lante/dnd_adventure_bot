import type { DraftSection } from "../../api/types";
import { EDITOR_STEPS } from "./steps";

// Отказ сервера называет поле путём схемы: «encounters.0.monsters: не больше 4 элементов»
// («<путь>: <сообщение>»). Автору он показывается словами формы: подписи
// разделов и полей, имя или id сущности, номер по порядку. Сообщение сервера — как есть.

// Разделы черновика — подписями списков на экранах.
const SECTION_WORDS: Record<DraftSection, string> = {
  settings: "Замысел",
  world: "Мир",
  deities: "Боги",
  factions: "Фракции",
  plot: "Сюжет",
  locations: "Локации",
  npcs: "Персонажи",
  relations: "Связи",
  items: "Предметы",
  encounters: "Бои",
  monsters: "Свои монстры",
  revelations: "Откровения",
};

// Поля схемы — подписями полей и списков форм. Одно имя поля у разных сущностей обычно
// подписано одинаково; исключения — в `FIELD_WORDS_IN`.
const FIELD_WORDS: Record<string, string> = {
  // Замысел
  title: "Название",
  adventure_id: "id приключения",
  pitch: "Завязка",
  tone: "Тон",
  target_hours: "Длина игры, часов",
  party_size: "Игроков",
  party_level: "Уровень героев",
  modules: "Модули правил",
  profanity: "Мат и грубая речь",
  journal_intro: "Главное для дневника",
  // Общие поля сущностей
  id: "Идентификатор",
  name: "Название",
  description: "Описание",
  secret: "Тайна",
  public: "Что знают игроки",
  known_at_start: "Известно с начала",
  // Мир, боги, фракции, сюжет
  history: "История мира",
  regions: "Края",
  rumors: "Слухи",
  text: "Текст",
  true: "Слух правдив",
  domains: "Домены",
  symbol: "Символ",
  dogma: "Догма",
  appearance: "Облик",
  attitude_to_mortals: "Отношение к смертным",
  avatar_statblock: "Аватар — id статблока",
  goal: "Цель",
  methods: "Методы",
  leader: "Лидер",
  base_location: "База — id локации",
  attitude_to_party: "Отношение к отряду",
  acts: "Акты",
  summary: "Кратко",
  key_scenes: "Ключевые сцены",
  endings: "Концовки",
  villain: "Злодей",
  // Карта и наполнение
  locations: "Локации",
  start_location: "Старт",
  finale_location: "Финал",
  region: "Регион",
  read_aloud: "Текст вслух",
  dm_notes: "Заметки мастера",
  exits: "Выходы",
  to: "Куда",
  direction: "Направление",
  locked_by: "Замок",
  hidden_dc: "Скрытность",
  features: "Детали и тайники",
  hidden: "Скрытая деталь",
  check: "Проверка",
  ability: "Характеристика",
  skill: "Навык",
  dc: "Сл",
  reveals: "Что раскрывает",
  kind: "Что",
  secrets: "Секреты",
  zones: "Зоны",
  links: "Связи зон",
  encounter_id: "Бой",
  // Персонажи и связи
  role: "Роль",
  manner: "Манера",
  motive: "Мотив",
  knows: "Что знает",
  attitude: "Отношение к отряду",
  location_id: "Локация",
  statblock: "Статблок — id",
  a: "Кто",
  b: "Кому",
  note: "Пояснение",
  mutual: "Взаимная",
  // Предметы, бои, монстры, улики
  holder: "Где лежит",
  base_item: "Предмет SRD",
  trigger: "Что запускает бой",
  tactics: "Тактика",
  monsters: "Монстры",
  monster_id: "Монстр",
  count: "Сколько",
  zone: "Зона",
  pc_zone: "Где стоят герои",
  boss: "Босс",
  name_en: "Название по-английски",
  cr: "Опасность (cr)",
  xp: "Опыт (xp)",
  ac: "Класс доспеха (КД)",
  hp: "Хиты (HP)",
  hp_dice: "Кость хитов",
  speed: "Скорость",
  actions: "Действия",
  attack_bonus: "Бонус атаки",
  damage: "Урон",
  damage_type: "Тип урона",
  range: "Дальность",
  save: "Спасбросок",
  half_on_save: "Половина урона при успехе",
  targets: "Целей",
  multiattack: "Действия по очереди",
  traits: "Особенности",
  statement: "Вывод",
  clues: "Улики",
  where: "Где лежит",
};

// Подписи полей, которые зависят от списка сущности (`<список>.<поле>`).
const FIELD_WORDS_IN: Record<string, string> = {
  "deities.name": "Имя",
  "deities.title": "Титул",
  "npcs.name": "Имя",
  "npcs.location_id": "Где находится",
  "relations.kind": "Кем",
  "relations.secret": "Тайная связь",
  "actions.summary": "Описание",
  "actions.kind": "Вид",
  "actions.dc": "Сл спасброска",
  "rumors.text": "Слух",
};

// Путь схемы: имена полей и номера через точку, без пробелов.
const PATH = /^[a-z_][a-z0-9_]*(?:\.[A-Za-z0-9_-]+)*$/;

const isSection = (part: string): part is DraftSection =>
  Object.hasOwn(SECTION_WORDS, part);

/**
 * Своё значение по ключу из строки сервера. Обычный доступ по скобкам нашёл бы и свойства
 * Object.prototype («constructor», «toString», «__proto__») — такое имя приходит в отказе о
 * лишнем поле и должно остаться как прислал сервер.
 */
const own = <T>(record: Record<string, T>, key: string): T | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

/** Подпись сущности по номеру: её имя, иначе id, иначе номер по порядку с единицы. */
function entityWords(entity: unknown, index: number): string {
  if (entity !== null && typeof entity === "object") {
    const { name, id } = entity as { name?: unknown; id?: unknown };
    for (const value of [name, id]) {
      if (typeof value === "string" && value.trim() !== "") return `«${value}»`;
    }
  }
  return `№ ${index + 1}`;
}

/** Путь схемы словами: «Бои → «crypt-ghouls» → Монстры». `data` — черновик для имён сущностей. */
function pathWords(path: string, data: unknown): string {
  const parts = path.split(".");
  // Раздел «Карты» — объект {locations, start_location, finale_location}: путь сервера
  // повторяет «locations», а в форме это одни «Локации».
  if (parts[0] === "locations" && parts[1] === "locations") parts.shift();
  const words: string[] = [];
  let node: unknown = data;
  let list = "";
  for (const [position, part] of parts.entries()) {
    const value =
      node !== null && typeof node === "object"
        ? own(node as Record<string, unknown>, part)
        : undefined;
    if (/^\d+$/.test(part)) {
      words.push(entityWords(value, Number(part)));
    } else if (position === 0 && isSection(part)) {
      words.push(SECTION_WORDS[part]);
      list = part;
    } else {
      words.push(
        own(FIELD_WORDS_IN, `${list}.${part}`) ??
          own(FIELD_WORDS, part) ??
          part,
      );
      list = part;
    }
    node = value;
  }
  return words.join(" → ");
}

/**
 * Строка отказа записи словами формы: «encounters.0.monsters: не больше 4 элементов» →
 * «Бои → «crypt-ghouls» → Монстры: не больше 4 элементов». `data` — черновик в форме ответа
 * сервера с записанными данными: по нему номер сущности становится её именем. Строка без
 * пути («…и ещё 3», «Старт не выбран») остаётся как есть.
 */
export function formProblem(line: string, data?: unknown): string {
  const colon = line.indexOf(": ");
  if (colon < 0) return line;
  const path = line.slice(0, colon);
  if (!PATH.test(path)) return line;
  return `${pathWords(path, data)}${line.slice(colon)}`;
}

const STEP_LABELS = new Map<string, string>(
  EDITOR_STEPS.map(({ step, label }) => [step, label]),
);

/**
 * Строка отказа публикации «<шаг>: <текст>»: подпись экрана вместо id шага, путь поля
 * в тексте — словами формы. Строка без шага (ошибка сборки приключения) — тоже словами формы.
 */
export function publishProblem(line: string): string {
  const colon = line.indexOf(": ");
  const label = colon < 0 ? undefined : STEP_LABELS.get(line.slice(0, colon));
  if (label === undefined) return formProblem(line);
  return `${label}: ${formProblem(line.slice(colon + 2))}`;
}
