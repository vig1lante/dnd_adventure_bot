import type {
  Ability,
  RefKind,
  RelationKind,
  RevealKind,
  Skill,
} from "../../api/types";

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
