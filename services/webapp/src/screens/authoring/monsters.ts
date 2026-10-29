import type { MonsterDef, SrdMonster } from "../../api/types";

/** Новый свой статблок; опасность и опыт — как у первого монстра справочника (такую пару примет проверка). */
export function newMonster(srd: readonly SrdMonster[]): MonsterDef {
  const first = srd.at(0);
  return {
    id: "",
    name: "",
    name_en: "",
    cr: first?.cr ?? 0,
    xp: first?.xp ?? 0,
    ac: 10,
    hp: 1,
    hp_dice: "1d8",
    speed: 30,
    scores: { str: 10, dex: 10, con: 10, int: 10, wis: 10, cha: 10 },
    saves: {},
    skills: {},
    resistances: [],
    vulnerabilities: [],
    immunities: [],
    actions: [],
    traits: [],
    tags: [],
  };
}
