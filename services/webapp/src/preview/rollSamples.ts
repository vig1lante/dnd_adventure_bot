import type { FeedItem, RollData } from "../api/types";

// Образцы для раздела «Бросок» страницы стиля. Это записи ленты в том виде, в каком их шлёт
// сервер, а рисует их та же карточка, что и в игре: превью не должно показывать другую.
// Числа выдуманы, настоящие считает движок.

function rollItem(
  id: number,
  text: string,
  data: Partial<RollData> & Pick<RollData, "actor" | "label" | "total">,
): FeedItem {
  const roll: RollData = {
    reason: "",
    expr: "1d20+3",
    rolls: [14],
    kept: [14],
    modifier: 3,
    natural: 14,
    advantage: "none",
    dc: null,
    success: null,
    ...data,
  };
  return {
    id,
    kind: "roll",
    text,
    data: { ...roll },
    private: false,
    created_at: "2026-10-20T12:00:00+00:00",
  };
}

export const ROLL_SAMPLES: readonly FeedItem[] = [
  rollItem(1, "Лира: Скрытность — 17 против Сл 15: успех", {
    actor: "Лира",
    label: "Скрытность",
    total: 17,
    dc: 15,
    success: true,
  }),
  rollItem(2, "Борин: Взлом замка — 7 против Сл 15: неудача", {
    actor: "Борин",
    label: "Взлом замка",
    expr: "1d20+1",
    rolls: [6],
    kept: [6],
    modifier: 1,
    natural: 6,
    total: 7,
    dc: 15,
    success: false,
  }),
  rollItem(3, "Кай: Восприятие — 20 против Сл 12: успех", {
    actor: "Кай",
    label: "Восприятие",
    expr: "1d20+2",
    rolls: [18, 7],
    kept: [18],
    modifier: 2,
    natural: 18,
    total: 20,
    advantage: "advantage",
    dc: 12,
    success: true,
  }),
  rollItem(4, "Лира: Скрытность — 19 против Сл 15: успех", {
    actor: "Лира",
    label: "Скрытность",
    expr: "1d20+3+1d4",
    rolls: [14, 2],
    kept: [14, 2],
    total: 19,
    dc: 15,
    success: true,
  }),
  rollItem(5, "Скелет: Атака: Короткий меч — 17 против КД 15: попадание", {
    actor: "Скелет",
    label: "Атака: Короткий меч",
    expr: "1d20+5",
    rolls: [12],
    kept: [12],
    modifier: 5,
    natural: 12,
    total: 17,
    success: true,
    target_ac: 15,
  }),
  rollItem(6, "Борин: Урон мечом — 8", {
    actor: "Борин",
    label: "Урон мечом",
    expr: "1d8+3",
    rolls: [5],
    kept: [5],
    modifier: 3,
    natural: null,
    total: 8,
  }),
];
