import { rollData } from "./fixtures";
import { damageTypeName, rangeName, ruDice, signed } from "./format";
import {
  againstLabel,
  outcomeLabel,
  parseRoll,
  rollFormula,
  rollSummary,
} from "./rolls";

describe("signed: знак числа в листе и формуле", () => {
  test.each([
    [3, "+3"],
    [0, "+0"],
    [-1, "−1"],
    [12, "+12"],
  ])("%i → %s", (value, text) => {
    expect(signed(value)).toBe(text);
  });
});

describe("ruDice: запись костей по-русски", () => {
  test.each([
    ["1d8+3", "1к8+3"],
    ["2d6", "2к6"],
    ["1d20+3+1d4", "1к20+3+1к4"],
    ["1d20-1", "1к20-1"],
  ])("%s → %s", (expr, text) => {
    expect(ruDice(expr)).toBe(text);
  });

  test("слова с буквой d не трогает", () => {
    expect(ruDice("Удар (dagger)")).toBe("Удар (dagger)");
  });
});

describe("подписи типов урона и дальности", () => {
  test.each([
    ["acid", "кислотный"],
    ["bludgeoning", "дробящий"],
    ["cold", "холодный"],
    ["fire", "огненный"],
    ["force", "силовой"],
    ["lightning", "электрический"],
    ["necrotic", "некротический"],
    ["piercing", "колющий"],
    ["poison", "ядовитый"],
    ["psychic", "психический"],
    ["radiant", "световой"],
    ["slashing", "рубящий"],
    ["thunder", "звуковой"],
  ])("тип урона %s → %s", (id, name) => {
    expect(damageTypeName(id)).toBe(name);
  });

  test("неизвестный тип урона остаётся как есть: строку лучше показать, чем потерять", () => {
    expect(damageTypeName("sonic_boom")).toBe("sonic_boom");
  });

  test.each([
    ["melee", "ближний бой"],
    ["near", "ближняя дистанция"],
    ["far", "дальняя дистанция"],
  ] as const)("дальность %s → %s", (range, name) => {
    expect(rangeName(range)).toBe(name);
  });
});

describe("parseRoll: данные карточки с сервера", () => {
  test("полные данные разбираются как есть", () => {
    const data = rollData({ target_ac: 15 });

    expect(parseRoll({ ...data })).toEqual({
      actor: data.actor,
      label: data.label,
      expr: data.expr,
      rolls: data.rolls,
      kept: data.kept,
      modifier: data.modifier,
      total: data.total,
      natural: data.natural,
      advantage: data.advantage,
      dc: data.dc,
      success: data.success,
      target_ac: 15,
    });
  });

  test("причина броска от движка (reason) дальше парсера не идёт: она бывает с именем статблока", () => {
    expect(parseRoll({ ...rollData() })).not.toHaveProperty("reason");
  });

  test.each([
    ["нет данных", null],
    ["пустой объект", {}],
    ["итог не числом", { ...rollData(), total: "17" }],
    ["кости не списком", { ...rollData(), kept: "14" }],
    ["в костях не число", { ...rollData(), rolls: [14, "x"] }],
    ["нет имени бросающего", { ...rollData(), actor: undefined }],
    ["нет названия броска", { ...rollData(), label: undefined }],
    ["нет формулы", { ...rollData(), expr: 5 }],
    ["неизвестное преимущество", { ...rollData(), advantage: "maybe" }],
    ["Сл не числом", { ...rollData(), dc: "15" }],
    ["исход не логическим", { ...rollData(), success: "yes" }],
    ["КД не числом", { ...rollData(), target_ac: "15" }],
  ])("%s → null: карточка заменится запасным текстом", (_name, data) => {
    expect(parseRoll(data as Record<string, unknown> | null)).toBeNull();
  });

  test("необязательные поля могут отсутствовать", () => {
    const data: Record<string, unknown> = { ...rollData() };
    delete data.dc;
    delete data.success;
    delete data.actor_id;
    delete data.reason;

    expect(parseRoll(data)).toMatchObject({ dc: null, success: null });
  });
});

describe("rollFormula: из чего сложился итог", () => {
  test("к20 и бонус: «14 + 3»", () => {
    expect(rollFormula(rollData())).toBe("14 + 3");
  });

  test("отрицательный бонус вычитается: «14 − 1»", () => {
    expect(rollFormula(rollData({ modifier: -1, total: 13 }))).toBe("14 − 1");
  });

  test("бонус ноль не показывается", () => {
    expect(rollFormula(rollData({ modifier: 0, total: 14 }))).toBe("14");
  });

  test("кость эффекта (Благословение) берётся из kept после к20: «к20 + бонус + кость»", () => {
    const roll = rollData({
      expr: "1d20+3+1d4",
      rolls: [14, 2],
      kept: [14, 2],
      total: 19,
    });

    expect(rollFormula(roll)).toBe("14 + 3 + 2");
  });

  test("при преимуществе в формуле остаётся оставленный к20", () => {
    const roll = rollData({
      rolls: [3, 14],
      kept: [14],
      advantage: "advantage",
    });

    expect(rollFormula(roll)).toBe("14 + 3");
  });

  test("не к20 (урон): запись костей по-русски, бонус внутри записи", () => {
    const damage = rollData({
      label: "Урон",
      expr: "1d8+3",
      rolls: [6],
      kept: [6],
      natural: null,
      total: 9,
      dc: null,
      success: null,
    });

    expect(rollFormula(damage)).toBe("1к8+3");
  });
});

describe("rollSummary: текст карточки одной строкой", () => {
  test("проверка навыка: «Ловкость (Акробатика): 14 + 3 = 17 против Сл 15 ✓»", () => {
    expect(rollSummary(rollData())).toBe(
      "Ловкость (Акробатика): 14 + 3 = 17 против Сл 15 ✓",
    );
  });

  test("проваленная проверка отмечена ✗", () => {
    const roll = rollData({ natural: 8, kept: [8], total: 11, success: false });

    expect(rollSummary(roll)).toBe(
      "Ловкость (Акробатика): 8 + 3 = 11 против Сл 15 ✗",
    );
  });

  test("атака монстра по герою: КД героя из данных сервера", () => {
    const roll = rollData({
      actor: "Скелет",
      label: "Атака: Короткий меч",
      natural: 12,
      kept: [12],
      modifier: 5,
      total: 17,
      dc: null,
      success: true,
      target_ac: 15,
    });

    expect(rollSummary(roll)).toBe(
      "Атака: Короткий меч: 12 + 5 = 17 против КД 15 ✓",
    );
  });

  test("атака героя по монстру: КД монстра не называется, только исход", () => {
    const roll = rollData({
      label: "Атака: Длинный меч",
      natural: 12,
      kept: [12],
      modifier: 5,
      total: 17,
      dc: null,
      success: true,
    });

    expect(rollSummary(roll)).toBe("Атака: Длинный меч: 12 + 5 = 17 ✓");
    expect(rollSummary(roll)).not.toMatch(/КД|Сл/);
  });

  test("урон: «Урон: 1к8+3 = 9»", () => {
    const roll = rollData({
      label: "Урон",
      expr: "1d8+3",
      rolls: [6],
      kept: [6],
      natural: null,
      total: 9,
      dc: null,
      success: null,
    });

    expect(rollSummary(roll)).toBe("Урон: 1к8+3 = 9");
  });

  test("к20 без бонуса: «= итог» не повторяет то же число", () => {
    const roll = rollData({ modifier: 0, total: 14 });

    expect(rollSummary(roll)).toBe("Ловкость (Акробатика): 14 против Сл 15 ✓");
  });

  test("название берётся из label, а не из reason: имя статблока не просачивается", () => {
    const roll = rollData({
      label: "Ловкость (Скрытность)",
      reason: "Скрытность: Фанатик культа",
    });

    expect(rollSummary(roll)).not.toContain("Фанатик");
    expect(rollSummary(roll).startsWith("Ловкость (Скрытность):")).toBe(true);
  });
});

describe("againstLabel и outcomeLabel", () => {
  test("Сл важнее: проверка и спасбросок идут против Сл", () => {
    expect(againstLabel(rollData())).toBe("против Сл 15");
  });

  test("если пришли и Сл, и КД, называется Сл: так читается проверка", () => {
    expect(againstLabel(rollData({ dc: 15, target_ac: 12 }))).toBe(
      "против Сл 15",
    );
  });

  test("КД называется только когда сервер его прислал", () => {
    expect(
      againstLabel(rollData({ dc: null, target_ac: 15, success: true })),
    ).toBe("против КД 15");
    expect(againstLabel(rollData({ dc: null, success: true }))).toBeNull();
  });

  test("исход: проверка — успех или неудача", () => {
    expect(outcomeLabel(rollData({ success: true }))).toBe("Успех");
    expect(outcomeLabel(rollData({ success: false }))).toBe("Неудача");
    expect(outcomeLabel(rollData({ success: null }))).toBeNull();
  });

  test("исход: атака — попадание или промах (по КД героя и по названию атаки)", () => {
    const byAc = rollData({
      label: "Копьё",
      dc: null,
      target_ac: 12,
      success: false,
    });
    const byName = rollData({
      label: "Атака: Меч",
      dc: null,
      success: true,
    });
    const byReadied = rollData({
      label: "Подготовленная атака: Лук",
      dc: null,
      success: false,
    });

    expect(outcomeLabel(byAc)).toBe("Промах");
    expect(outcomeLabel(byName)).toBe("Попадание");
    expect(outcomeLabel(byReadied)).toBe("Промах");
  });
});

describe("parseRoll: числа в данных — настоящие числа", () => {
  test.each([
    ["rolls", { rolls: ["14"] }],
    ["kept", { kept: ["14"] }],
    ["total", { total: "17" }],
    ["modifier", { modifier: "3" }],
    ["total — не конечное число", { total: Number.POSITIVE_INFINITY }],
    ["rolls — не конечное число", { rolls: [Number.NaN] }],
  ])("%s: карточка не собирается, остаётся текст сервера", (_name, patch) => {
    expect(parseRoll({ ...rollData(), ...patch })).toBeNull();
  });
});
