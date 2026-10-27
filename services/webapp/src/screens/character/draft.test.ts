import {
  EMPTY_PERSONA,
  availableValues,
  baseScores,
  canLower,
  canRaise,
  createDraft,
  draftFromChoices,
  grantedSkills,
  normalize,
  pointBuyLeft,
  pointBuySpent,
  proficientSkills,
  stepComplete,
  stepsOf,
  toChoices,
} from "./draft";
import type { Assignment, Draft, Scores } from "./draft";
import { OPTIONS, STORED_DETAILED } from "./fixtures";
import type { CharacterChoices } from "../../api/types";

const scores = (overrides: Partial<Scores> = {}): Scores => ({
  str: 8,
  dex: 8,
  con: 8,
  int: 8,
  wis: 8,
  cha: 8,
  ...overrides,
});

const assigned = (overrides: Partial<Assignment> = {}): Assignment => ({
  str: null,
  dex: null,
  con: null,
  int: null,
  wis: null,
  cha: null,
  ...overrides,
});

/** Полностью заполненный черновик: эльф-плут с предысторией «Преступник». */
function rogueDraft(overrides: Partial<Draft> = {}): Draft {
  return normalize(
    {
      ...createDraft(OPTIONS),
      species: "elf",
      classId: "rogue",
      background: "criminal",
      method: "standard_array",
      standard: assigned({
        str: 8,
        dex: 15,
        con: 14,
        int: 13,
        wis: 10,
        cha: 12,
      }),
      bonusShape: 0,
      bonusPicks: ["dex", "con"],
      skills: ["acrobatics", "deception", "insight", "persuasion"],
      expertise: ["stealth", "perception"],
      name: "  Лира  ",
      ...overrides,
    },
    OPTIONS,
  );
}

describe("покупка очков", () => {
  test("новый черновик: все характеристики по 8, потрачено 0 из 27", () => {
    const draft = createDraft(OPTIONS);

    expect(draft.method).toBe("point_buy");
    expect(draft.pointBuy).toEqual(scores());
    expect(pointBuySpent(draft.pointBuy, OPTIONS)).toBe(0);
    expect(pointBuyLeft(draft.pointBuy, OPTIONS)).toBe(27);
  });

  test("стоимость берётся из таблицы сервера", () => {
    const spent = pointBuySpent(scores({ str: 15, dex: 14, con: 13 }), OPTIONS);

    expect(spent).toBe(9 + 7 + 5);
    expect(pointBuyLeft(scores({ str: 15, dex: 14, con: 13 }), OPTIONS)).toBe(
      6,
    );
  });

  test("поднять нельзя, если очков не хватает: превышение заблокировано", () => {
    const full = scores({ str: 15, dex: 15, con: 15 }); // 27 из 27

    expect(pointBuyLeft(full, OPTIONS)).toBe(0);
    expect(canRaise(full, "int", OPTIONS)).toBe(false);
    expect(canLower(full, "str", OPTIONS)).toBe(true);
  });

  test("следующее значение дороже остатка — нельзя, дешёвое — можно", () => {
    // Осталось 1 очко: 13 -> 14 стоит 2 (7 - 5), 8 -> 9 стоит 1.
    const nearly = scores({ str: 15, dex: 15, con: 13, int: 11 }); // 9+9+5+3 = 26
    expect(pointBuyLeft(nearly, OPTIONS)).toBe(1);

    expect(canRaise(nearly, "con", OPTIONS)).toBe(false);
    expect(canRaise(nearly, "wis", OPTIONS)).toBe(true);
  });

  test("границы таблицы: выше максимума и ниже минимума не двигается", () => {
    expect(canRaise(scores({ str: 15 }), "str", OPTIONS)).toBe(false);
    expect(canLower(scores(), "str", OPTIONS)).toBe(false);
  });
});

describe("распределение значений", () => {
  const ARRAY = [15, 14, 13, 12, 10, 8];

  test("доступны ещё не розданные значения и собственное значение", () => {
    const state = assigned({ str: 15, dex: 14 });

    expect(availableValues(ARRAY, state, "con")).toEqual([13, 12, 10, 8]);
    expect(availableValues(ARRAY, state, "str")).toEqual([15, 13, 12, 10, 8]);
  });

  test("одинаковые значения выдаются столько раз, сколько их в наборе", () => {
    const values = [14, 14, 12, 11, 10, 9];

    expect(availableValues(values, assigned({ str: 14 }), "dex")).toContain(14);
    expect(
      availableValues(values, assigned({ str: 14, dex: 14 }), "con"),
    ).not.toContain(14);
    expect(
      availableValues(values, assigned({ str: 14, dex: 14 }), "dex"),
    ).toContain(14);
  });

  test("значения по убыванию и без повторов", () => {
    expect(availableValues([10, 14, 14, 8], assigned(), "str")).toEqual([
      14, 10, 8,
    ]);
  });
});

describe("характеристики по способу", () => {
  test("покупка очков всегда полна", () => {
    const draft = createDraft(OPTIONS);

    expect(baseScores(draft)).toEqual(scores());
  });

  test("стандартный набор: пока не розданы все шесть значений — нет результата", () => {
    const draft = {
      ...createDraft(OPTIONS),
      method: "standard_array" as const,
      standard: assigned({ str: 15, dex: 14 }),
    };

    expect(baseScores(draft)).toBeNull();
    expect(stepComplete("abilities", draft, OPTIONS)).toBe(false);
  });
});

describe("навыки", () => {
  test("предыстория и вид дают навыки сами", () => {
    const draft = rogueDraft();

    expect(grantedSkills(draft, OPTIONS)).toEqual([
      "sleight_of_hand",
      "stealth",
      "perception",
    ]);
  });

  test("владения: данные автоматически, выбор класса и навык на выбор", () => {
    const draft = rogueDraft({ species: "human", extraSkill: "history" });

    expect(proficientSkills(draft, OPTIONS)).toEqual([
      "sleight_of_hand",
      "stealth",
      "acrobatics",
      "deception",
      "insight",
      "persuasion",
      "history",
    ]);
  });
});

describe("зачистка выбора после смены (normalize)", () => {
  test("смена класса сбрасывает выбор прежнего класса", () => {
    const fighter = normalize(
      {
        ...rogueDraft(),
        classId: "fighter",
        fightingStyle: "archery",
      },
      OPTIONS,
    );
    // Навыки плута, которых нет у воина, ушли; общие остались; компетентности у воина нет.
    expect(fighter.skills).toEqual(["acrobatics", "insight"]);
    expect(fighter.expertise).toEqual([]);
    expect(fighter.fightingStyle).toBe("archery");

    const wizard = normalize({ ...fighter, classId: "wizard" }, OPTIONS);
    expect(wizard.fightingStyle).toBeNull();
    expect(wizard.skills).toEqual(["insight"]);
  });

  test("навык, который уже даёт предыстория, из выбора класса убирается", () => {
    const draft = normalize(
      { ...rogueDraft(), skills: ["stealth", "acrobatics"] },
      OPTIONS,
    );

    expect(draft.skills).toEqual(["acrobatics"]);
  });

  test("лишние выборы сверх нормы отрезаются", () => {
    const draft = normalize(
      {
        ...createDraft(OPTIONS),
        classId: "fighter",
        species: "dwarf",
        background: "criminal",
        skills: ["acrobatics", "athletics", "history"],
      },
      OPTIONS,
    );

    expect(draft.skills).toEqual(["acrobatics", "athletics"]);
  });

  test("навык на выбор есть только у вида с такой чертой", () => {
    const human = normalize(
      { ...rogueDraft(), species: "human", extraSkill: "history" },
      OPTIONS,
    );
    expect(human.extraSkill).toBe("history");

    const elf = normalize({ ...human, species: "elf" }, OPTIONS);
    expect(elf.extraSkill).toBeNull();
  });

  test("компетентность держится только на навыках, которыми герой владеет", () => {
    const draft = normalize(
      { ...rogueDraft(), expertise: ["stealth", "history"] },
      OPTIONS,
    );

    expect(draft.expertise).toEqual(["stealth"]);
  });

  test("компетентность держится и на навыке на выбор (Умелость человека)", () => {
    const draft = normalize(
      {
        ...rogueDraft(),
        species: "human",
        extraSkill: "history",
        expertise: ["stealth", "history"],
      },
      OPTIONS,
    );

    expect(draft.expertise).toEqual(["stealth", "history"]);
  });

  test.each([2, 7])(
    "формы бонуса №%i в правилах нет: берётся первая",
    (bonusShape) => {
      expect(OPTIONS.background_bonus_shapes).toHaveLength(2);

      const draft = normalize({ ...rogueDraft(), bonusShape }, OPTIONS);

      expect(draft.bonusShape).toBe(0);
      expect(draft.bonusPicks).toHaveLength(
        OPTIONS.background_bonus_shapes[0].length,
      );
    },
  );

  test("последняя форма бонуса из правил остаётся как есть", () => {
    const last = OPTIONS.background_bonus_shapes.length - 1;

    expect(
      normalize({ ...rogueDraft(), bonusShape: last }, OPTIONS).bonusShape,
    ).toBe(last);
  });

  test("смена предыстории убирает бонус к характеристикам, которых в ней нет", () => {
    const draft = normalize(
      { ...rogueDraft(), background: "acolyte" },
      OPTIONS,
    );

    // У «Прислужника» бонус возможен к Интеллекту, Мудрости и Харизме.
    expect(draft.bonusPicks).toEqual([null, null]);
  });

  test("смена формы бонуса меняет число выборов", () => {
    const draft = normalize({ ...rogueDraft(), bonusShape: 1 }, OPTIONS);

    expect(draft.bonusPicks).toHaveLength(3);
  });

  test("в предыстории ровно три характеристики и форма +1/+1/+1: выбор известен сам", () => {
    const draft = normalize({ ...rogueDraft(), bonusShape: 1 }, OPTIONS);

    expect(draft.bonusPicks).toEqual(["dex", "con", "int"]);
  });

  test("заклинания, которых нет у класса, убираются; заговоры и заклинания отрезаются по норме", () => {
    const wizard = normalize(
      {
        ...createDraft(OPTIONS),
        classId: "wizard",
        cantrips: ["fire_bolt", "light", "mage_hand", "nonexistent"],
        spells: ["shield", "sleep", "magic_missile"],
      },
      OPTIONS,
    );
    expect(wizard.cantrips).toEqual(["fire_bolt", "light"]);
    expect(wizard.spells).toEqual(["shield", "sleep"]);

    const rogue = normalize({ ...wizard, classId: "rogue" }, OPTIONS);
    expect(rogue.cantrips).toEqual([]);
    expect(rogue.spells).toEqual([]);
  });

  test("повторная зачистка ничего не меняет", () => {
    const once = rogueDraft();

    expect(normalize(once, OPTIONS)).toEqual(once);
  });
});

describe("шаги", () => {
  test("шаг заклинаний есть только у заклинателя", () => {
    expect(stepsOf(rogueDraft(), OPTIONS)).not.toContain("spells");
    expect(stepsOf(rogueDraft({ classId: "wizard" }), OPTIONS)).toContain(
      "spells",
    );
  });

  test("порядок шагов как в задании", () => {
    expect(stepsOf(rogueDraft({ classId: "wizard" }), OPTIONS)).toEqual([
      "species",
      "class",
      "background",
      "abilities",
      "bonus",
      "skills",
      "spells",
      "equipment",
      "persona",
      "backstory",
      "summary",
    ]);
  });

  test("последний шаг — «Итог»: идти дальше не нужно, он закончен всегда", () => {
    expect(stepsOf(rogueDraft(), OPTIONS).at(-1)).toBe("summary");
    expect(stepComplete("summary", createDraft(OPTIONS), OPTIONS)).toBe(true);
  });

  test("вид, класс и предыстория выбраны — шаги завершены", () => {
    const empty = createDraft(OPTIONS);
    for (const step of ["species", "class", "background"] as const) {
      expect(stepComplete(step, empty, OPTIONS), step).toBe(false);
    }
    const draft = rogueDraft();
    for (const step of ["species", "class", "background"] as const) {
      expect(stepComplete(step, draft, OPTIONS), step).toBe(true);
    }
  });

  test("бонус: нужны все выборы, и они разные", () => {
    expect(stepComplete("bonus", rogueDraft(), OPTIONS)).toBe(true);
    expect(
      stepComplete("bonus", rogueDraft({ bonusPicks: ["dex", null] }), OPTIONS),
    ).toBe(false);
  });

  test("навыки: нужное число выборов класса, навык на выбор, компетентность и стиль", () => {
    expect(stepComplete("skills", rogueDraft(), OPTIONS)).toBe(true);
    expect(
      stepComplete("skills", rogueDraft({ skills: ["acrobatics"] }), OPTIONS),
    ).toBe(false);
    expect(
      stepComplete("skills", rogueDraft({ expertise: ["stealth"] }), OPTIONS),
    ).toBe(false);
    // Человеку нужен ещё навык на выбор (компетентность при этом уже полна).
    const human = rogueDraft({
      species: "human",
      expertise: ["stealth", "sleight_of_hand"],
    });
    expect(stepComplete("skills", human, OPTIONS)).toBe(false);
    expect(
      stepComplete("skills", { ...human, extraSkill: "history" }, OPTIONS),
    ).toBe(true);
    // Воину нужен боевой стиль.
    expect(
      stepComplete(
        "skills",
        rogueDraft({ classId: "fighter", skills: ["acrobatics", "insight"] }),
        OPTIONS,
      ),
    ).toBe(false);
  });

  test("заклинания: нужное число заговоров и заклинаний", () => {
    const wizard = rogueDraft({ classId: "wizard" });
    expect(stepComplete("spells", wizard, OPTIONS)).toBe(false);

    const picked = normalize(
      {
        ...wizard,
        cantrips: ["fire_bolt", "light"],
        spells: ["shield", "sleep"],
      },
      OPTIONS,
    );
    expect(stepComplete("spells", picked, OPTIONS)).toBe(true);
  });

  test("имя обязательно, снаряжение и предыстория — нет", () => {
    expect(stepComplete("persona", rogueDraft(), OPTIONS)).toBe(true);
    expect(stepComplete("persona", rogueDraft({ name: "   " }), OPTIONS)).toBe(
      false,
    );
    expect(stepComplete("equipment", createDraft(OPTIONS), OPTIONS)).toBe(true);
    expect(stepComplete("backstory", createDraft(OPTIONS), OPTIONS)).toBe(true);
  });
});

describe("выбор для сервера (toChoices)", () => {
  test("собирает выбор: числа игрок не присылает, только исходные значения", () => {
    const choices = toChoices(rogueDraft(), OPTIONS);

    expect(choices).toEqual({
      name: "Лира",
      species: "elf",
      class_id: "rogue",
      background: "criminal",
      method: "standard_array",
      base_scores: { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 },
      background_bonus: { dex: 2, con: 1 },
      skills: ["acrobatics", "deception", "insight", "persuasion"],
      extra_skill: null,
      expertise: ["stealth", "perception"],
      fighting_style: null,
      cantrips: [],
      prepared_spells: [],
      persona: {},
    });
  });

  test("форма +1/+1/+1 даёт по единице трём характеристикам", () => {
    const choices = toChoices(rogueDraft({ bonusShape: 1 }), OPTIONS);

    expect(choices?.background_bonus).toEqual({ dex: 1, con: 1, int: 1 });
  });

  test("описание уходит только заполненными полями и без пробелов по краям", () => {
    const draft = rogueDraft({
      persona: {
        concept: " Вор ",
        appearance: "",
        traits: "Смешлив",
        ideals: "  ",
        bonds: "",
        flaws: "",
        backstory: "Жил-был.",
      },
    });

    expect(toChoices(draft, OPTIONS)?.persona).toEqual({
      concept: "Вор",
      traits: "Смешлив",
      backstory: "Жил-был.",
    });
  });

  test("человек: навык на выбор уходит, боевой стиль воина тоже", () => {
    const draft = rogueDraft({
      species: "human",
      classId: "fighter",
      skills: ["acrobatics", "insight"],
      extraSkill: "history",
      fightingStyle: "defense",
    });

    const choices = toChoices(draft, OPTIONS);

    expect(choices).toMatchObject({
      extra_skill: "history",
      fighting_style: "defense",
      expertise: [],
    });
  });

  test("неполный выбор не отправляется", () => {
    expect(toChoices(createDraft(OPTIONS), OPTIONS)).toBeNull();
    expect(
      toChoices(rogueDraft({ bonusPicks: [null, null] }), OPTIONS),
    ).toBeNull();
  });
});

describe("черновик из сохранённого выбора («Изменить героя»)", () => {
  /** Выбор, как его хранит сервер, с точечными отличиями. */
  const stored = (
    overrides: Partial<CharacterChoices> = {},
  ): CharacterChoices => ({
    ...STORED_DETAILED,
    ...overrides,
  });

  /** То, что клиент отправит из восстановленного черновика: пустые поля описания не уходят. */
  const resent = (choices: CharacterChoices) =>
    toChoices(draftFromChoices(choices, OPTIONS), OPTIONS);

  test("покупка очков: выбор вида, класса, предыстории, значений и описания восстановлен", () => {
    const draft = draftFromChoices(stored(), OPTIONS);

    expect(draft).toMatchObject({
      species: "elf",
      classId: "rogue",
      background: "criminal",
      method: "point_buy",
      pointBuy: { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 },
      bonusShape: 0,
      bonusPicks: ["dex", "con"],
      skills: ["acrobatics", "deception", "insight", "persuasion"],
      expertise: ["stealth", "perception"],
      name: "Лира",
    });
    expect(draft.persona).toEqual(STORED_DETAILED.persona);
    expect(pointBuyLeft(draft.pointBuy, OPTIONS)).toBe(0);
  });

  test("все шаги закончены: «Далее» не потребует выбирать заново", () => {
    const draft = draftFromChoices(stored(), OPTIONS);

    for (const step of stepsOf(draft, OPTIONS)) {
      expect(stepComplete(step, draft, OPTIONS), step).toBe(true);
    }
  });

  test("отправляется тот же выбор; из описания уходят только заполненные поля", () => {
    expect(resent(stored())).toEqual({
      ...STORED_DETAILED,
      persona: { concept: "Вор-неудачник", backstory: "Родилась в порту." },
    });
  });

  test("стандартный набор: раздача восстановлена, покупка очков осталась нетронутой", () => {
    const choices = stored({
      method: "standard_array",
      base_scores: { str: 8, dex: 15, con: 14, int: 13, wis: 10, cha: 12 },
    });

    const draft = draftFromChoices(choices, OPTIONS);

    expect(draft.method).toBe("standard_array");
    expect(draft.standard).toEqual(choices.base_scores);
    expect(draft.pointBuy).toEqual(createDraft(OPTIONS).pointBuy);
    expect(resent(choices)?.method).toBe("standard_array");
    expect(resent(choices)?.base_scores).toEqual(choices.base_scores);
  });

  test("бонус «по одному очку»: форма определена по размерам прибавок", () => {
    const choices = stored({ background_bonus: { dex: 1, con: 1, int: 1 } });

    const draft = draftFromChoices(choices, OPTIONS);

    expect(draft.bonusShape).toBe(1);
    expect(resent(choices)?.background_bonus).toEqual({
      dex: 1,
      con: 1,
      int: 1,
    });
  });

  test("прибавки в любом порядке ключей: форма «+2, +1» и выбор по убыванию", () => {
    const draft = draftFromChoices(
      stored({ background_bonus: { con: 1, dex: 2 } }),
      OPTIONS,
    );

    expect(draft.bonusShape).toBe(0);
    expect(draft.bonusPicks).toEqual(["dex", "con"]);
  });

  test("человек и воин: навык на выбор и боевой стиль", () => {
    const choices = stored({
      species: "human",
      class_id: "fighter",
      skills: ["acrobatics", "insight"],
      extra_skill: "history",
      expertise: [],
      fighting_style: "defense",
      background_bonus: { dex: 2, con: 1 },
    });

    expect(resent(choices)).toMatchObject({
      species: "human",
      class_id: "fighter",
      extra_skill: "history",
      fighting_style: "defense",
    });
  });

  test("волшебник: заговоры и заклинания восстановлены", () => {
    const choices = stored({
      class_id: "wizard",
      skills: ["arcana", "history"],
      expertise: [],
      cantrips: ["fire_bolt", "light"],
      prepared_spells: ["shield", "sleep"],
    });

    expect(resent(choices)).toMatchObject({
      class_id: "wizard",
      cantrips: ["fire_bolt", "light"],
      prepared_spells: ["shield", "sleep"],
    });
  });

  test("поле описания, которого нет в выборе, пустое", () => {
    const draft = draftFromChoices(
      stored({ persona: { concept: "Вор" } }),
      OPTIONS,
    );

    expect(draft.persona).toEqual({ ...EMPTY_PERSONA, concept: "Вор" });
  });

  test("выбор, которого правила больше не допускают, зачищается, как при смене класса", () => {
    // Навык «Выживание» плуту не предлагается: сервер правил уже не принял бы такой выбор.
    const draft = draftFromChoices(
      stored({ skills: ["acrobatics", "survival"] }),
      OPTIONS,
    );

    expect(draft.skills).toEqual(["acrobatics"]);
  });
});
