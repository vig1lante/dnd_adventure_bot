import type {
  Ability,
  ActionRange,
  MonsterActionKind,
  RefKind,
  RelationKind,
  RevealKind,
  Skill,
} from "../../api/types";
import { damageTypeName, rangeName } from "../../components/format";

// Шаблон id сущностей движка (`ID_PATTERN`: `^[a-z0-9][a-z0-9_-]{0,63}$`, дополнение B6 §2)
// словами: одно правило на все поля id, которые сервер проверяет этим шаблоном.
const ID_RULE =
  "Строчная латиница, цифры, «-» и «_»; начинается с буквы или цифры";

/** Подсказка поля id сущности: правило шаблона сервера. */
export const ID_HINT = `${ID_RULE}.`;

/** Подсказка поля id приключения: то же правило и его длина. */
export const ADVENTURE_ID_HINT = `${ID_RULE}; до 64 знаков.`;

// Подписи значений схемы приключения по-русски (названия — как в движке, `abilities.py`). Клиент
// только предлагает выбор из этих значений; проверяет и считает сервер.

export const ABILITY_OPTIONS: readonly { value: Ability; label: string }[] = [
  { value: "str", label: "Сила" },
  { value: "dex", label: "Ловкость" },
  { value: "con", label: "Телосложение" },
  { value: "int", label: "Интеллект" },
  { value: "wis", label: "Мудрость" },
  { value: "cha", label: "Харизма" },
];

export const SKILL_OPTIONS: readonly { value: Skill; label: string }[] = [
  { value: "athletics", label: "Атлетика" },
  { value: "acrobatics", label: "Акробатика" },
  { value: "sleight_of_hand", label: "Ловкость рук" },
  { value: "stealth", label: "Скрытность" },
  { value: "arcana", label: "Магия" },
  { value: "history", label: "История" },
  { value: "investigation", label: "Анализ" },
  { value: "nature", label: "Природа" },
  { value: "religion", label: "Религия" },
  { value: "animal_handling", label: "Уход за животными" },
  { value: "insight", label: "Проницательность" },
  { value: "medicine", label: "Медицина" },
  { value: "perception", label: "Внимательность" },
  { value: "survival", label: "Выживание" },
  { value: "deception", label: "Обман" },
  { value: "intimidation", label: "Запугивание" },
  { value: "performance", label: "Выступление" },
  { value: "persuasion", label: "Убеждение" },
];

/** Сл — только из таблицы 5/10/15/20/25/30 (инвариант проекта); значения — строки для списка. */
export const DC_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "5", label: "5 — очень легко" },
  { value: "10", label: "10 — легко" },
  { value: "15", label: "15 — средне" },
  { value: "20", label: "20 — трудно" },
  { value: "25", label: "25 — очень трудно" },
  { value: "30", label: "30 — почти невозможно" },
];

/** Вид связи читается «кто — <вид> — кому»: «Фабиан — должник — Марта». */
export const RELATION_KINDS: Record<RelationKind, string> = {
  kin: "родня",
  spouse: "супруг",
  lover: "любовник",
  friend: "друг",
  ally: "союзник",
  rival: "соперник",
  enemy: "враг",
  debtor: "должник",
  patron: "покровитель",
  servant: "слуга",
  worships: "поклоняется",
  betrayed: "предал",
  seeks: "ищет",
  other: "другое",
};

export const REVEAL_KINDS: Record<RevealKind, string> = {
  clue: "улика",
  item: "предмет",
  flag: "флаг",
  exit: "выход",
};

export const REF_KINDS: Record<RefKind, string> = {
  deity: "бог",
  faction: "фракция",
  npc: "персонаж",
  location: "локация",
  item: "предмет",
  encounter: "бой",
};

/** Виды действий статблока. */
export const ACTION_KINDS: readonly {
  value: MonsterActionKind;
  label: string;
}[] = [
  { value: "attack", label: "атака" },
  { value: "save", label: "спасбросок" },
  { value: "multiattack", label: "мультиатака" },
  { value: "other", label: "другое" },
];

/** Дальность действия статблока; подписи — общие, как у оружия героя. */
export const RANGE_OPTIONS: readonly { value: ActionRange; label: string }[] = (
  ["melee", "near", "far"] as const
).map((value) => ({ value, label: rangeName(value) }));

/** Тринадцать типов урона SRD (id — как в движке); подписи — общие, как в листе героя. */
export const DAMAGE_TYPE_OPTIONS: readonly { value: string; label: string }[] =
  [
    "acid",
    "bludgeoning",
    "cold",
    "fire",
    "force",
    "lightning",
    "necrotic",
    "piercing",
    "poison",
    "psychic",
    "radiant",
    "slashing",
    "thunder",
  ].map((value) => ({ value, label: damageTypeName(value) }));

// Дробные опасности монстров в записи D&D.
const CR_FRACTIONS: Record<number, string> = {
  0.125: "1/8",
  0.25: "1/4",
  0.5: "1/2",
};

/** Опасность монстра (cr) записью D&D: 0,25 → «1/4». Только запись числа сервера. */
export const crText = (cr: number): string =>
  CR_FRACTIONS[cr] ?? String(cr).replace(".", ",");

/** Виды частей ссылки находки проверки (`location:crypt/exit:down`) и ссылка `adventure`. */
export const FINDING_KINDS: Record<string, string> = {
  adventure: "приключение",
  location: "локация",
  exit: "выход",
  feature: "деталь",
  secret: "секрет",
  npc: "персонаж",
  item: "предмет",
  encounter: "бой",
  monster: "монстр",
  action: "действие",
  revelation: "откровение",
  clue: "улика",
  deity: "бог",
  faction: "фракция",
  relation: "связь",
  region: "регион",
};
