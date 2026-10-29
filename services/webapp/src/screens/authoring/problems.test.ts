import {
  actionOf,
  draftOf,
  encounterOf,
  exitOf,
  locationOf,
  monsterOf,
} from "./fixtures";
import { formProblem, publishProblem } from "./problems";

// Отказ сервера называет поле путём схемы («<путь>: <сообщение>», контракт B4 §4). Автор видит
// тот же путь словами формы: подписи разделов и полей, имя или id сущности, номер по порядку.

describe("путь поля в отказе — словами формы", () => {
  test.each([
    ["settings.party_size: не больше 6", "Замысел → Игроков: не больше 6"],
    [
      "settings.adventure_id: только строчная латиница, цифры, «-» и «_», до 64 знаков",
      "Замысел → id приключения: только строчная латиница, цифры, «-» и «_», до 64 знаков",
    ],
    ["monsters: не больше 6 элементов", "Свои монстры: не больше 6 элементов"],
    ["encounters: не больше 8 элементов", "Бои: не больше 8 элементов"],
    ["locations.start_location: нужна строка", "Локации → Старт: нужна строка"],
    [
      "world.regions.0.name: нужна строка",
      "Мир → Края → № 1 → Название: нужна строка",
    ],
  ])("%s", (line, expected) => {
    expect(formProblem(line)).toBe(expected);
  });

  test("сущность по номеру — её имя или id из записанных данных", () => {
    const draft = draftOf({
      encounters: [encounterOf({ id: "crypt-ghouls" })],
      monsters: [monsterOf({ name: "Страж", actions: [actionOf()] })],
      locations: [
        locationOf({ id: "crypt", name: "Склеп", exits: [exitOf()] }),
      ],
    });

    expect(
      formProblem("encounters.0.monsters: не больше 4 элементов", draft),
    ).toBe("Бои → «crypt-ghouls» → Монстры: не больше 4 элементов");
    expect(
      formProblem("monsters.0.actions.1.damage: нужна строка", draft),
    ).toBe("Свои монстры → «Страж» → Действия → № 2 → Урон: нужна строка");
    // Раздел «Карты» — объект {locations, start_location, finale_location}: путь сервера
    // повторяет «locations», форма — нет.
    expect(
      formProblem("locations.locations.0.exits.0.to: нужна строка", draft),
    ).toBe("Локации → «Склеп» → Выходы → «down» → Куда: нужна строка");
  });

  test("без данных — номер по порядку с единицы", () => {
    expect(formProblem("encounters.2.monsters.0.count: не больше 20")).toBe(
      "Бои → № 3 → Монстры → № 1 → Сколько: не больше 20",
    );
  });

  test("незнакомое поле — как прислал сервер; строка без пути — как есть", () => {
    expect(formProblem("deities.0.mystery: лишнее поле")).toBe(
      "Боги → № 1 → mystery: лишнее поле",
    );
    expect(formProblem("…и ещё 3")).toBe("…и ещё 3");
    expect(formProblem("Старт не выбран")).toBe("Старт не выбран");
    expect(formProblem("Нет связи: попробуйте позже")).toBe(
      "Нет связи: попробуйте позже",
    );
  });
});

describe("строка отказа публикации «<шаг>: <текст>»", () => {
  test.each([
    ["locations: Старт не выбран", "Карта: Старт не выбран"],
    [
      "location_contents: в локации нет текста вслух",
      "Наполнение: в локации нет текста вслух",
    ],
    [
      "encounters: encounters.0.monsters: не больше 4 элементов",
      "Бои и боссы: Бои → № 1 → Монстры: не больше 4 элементов",
    ],
    [
      "clues: у откровения меньше трёх улик",
      "Улики: у откровения меньше трёх улик",
    ],
    // Находка без шага-владельца: так её подписал сервер.
    [
      "общее: приключение слишком короткое",
      "общее: приключение слишком короткое",
    ],
    ["…и ещё 4", "…и ещё 4"],
  ])("%s", (line, expected) => {
    expect(publishProblem(line)).toBe(expected);
  });

  test("ошибка сборки приключения без шага — путь словами", () => {
    expect(publishProblem("npcs.0.name: нужна строка")).toBe(
      "Персонажи → № 1 → Имя: нужна строка",
    );
  });
});

// --- финальное покрытие блока B10 (B10.coverage) ---

describe("подписи полей по списку и сущности без имени", () => {
  test.each([
    ["deities.0.name: нужна строка", "Боги → № 1 → Имя: нужна строка"],
    ["deities.0.title: нужна строка", "Боги → № 1 → Титул: нужна строка"],
    [
      "npcs.0.location_id: нет такой локации",
      "Персонажи → № 1 → Где находится: нет такой локации",
    ],
    ["relations.0.kind: нужна строка", "Связи → № 1 → Кем: нужна строка"],
    [
      "relations.0.secret: нужно да или нет",
      "Связи → № 1 → Тайная связь: нужно да или нет",
    ],
    [
      "monsters.0.actions.0.dc: из таблицы 5/10/15/20/25/30",
      "Свои монстры → № 1 → Действия → № 1 → Сл спасброска: из таблицы 5/10/15/20/25/30",
    ],
    [
      "world.rumors.2.text: нужна строка",
      "Мир → Слухи → № 3 → Слух: нужна строка",
    ],
    // Подпись по списку действует только внутри своего списка: «name» у фракции — «Название».
    ["factions.0.name: нужна строка", "Фракции → № 1 → Название: нужна строка"],
    // Раздел узнаётся только первым в пути: «acts» внутри сюжета — поле, а не раздел.
    [
      "plot.acts.0.summary: нужна строка",
      "Сюжет → Акты → № 1 → Кратко: нужна строка",
    ],
  ])("%s", (line, expected) => {
    expect(formProblem(line)).toBe(expected);
  });

  test("имя из одних пробелов — подпись по id; ни имени, ни id — номер по порядку", () => {
    const data = {
      deities: [
        { name: "   ", id: "sun-god" },
        { name: "", id: "" },
        "не объект",
      ],
    };

    expect(formProblem("deities.0.domains: нужен домен", data)).toBe(
      "Боги → «sun-god» → Домены: нужен домен",
    );
    expect(formProblem("deities.1.domains: нужен домен", data)).toBe(
      "Боги → № 2 → Домены: нужен домен",
    );
    expect(formProblem("deities.2.domains: нужен домен", data)).toBe(
      "Боги → № 3 → Домены: нужен домен",
    );
  });

  test("имя сущности с разметкой попадает в строку буквами, как есть", () => {
    const data = { npcs: [{ name: '<img src=x onerror="alert(1)">' }] };

    expect(formProblem("npcs.0.role: нужна строка", data)).toBe(
      'Персонажи → «<img src=x onerror="alert(1)">» → Роль: нужна строка',
    );
  });

  test("пробелы или заглавные в начале пути — это не путь схемы: строка как есть", () => {
    expect(formProblem("Settings.title: нужна строка")).toBe(
      "Settings.title: нужна строка",
    );
    expect(formProblem("settings title: нужна строка")).toBe(
      "settings title: нужна строка",
    );
  });

  // Бывший дефект B10.coverage (D2): подписи искались в обычных объектах, и имя поля, совпадающее
  // со свойством Object.prototype, доставало его значение. Исправлено в B10.3.fix1 (Object.hasOwn).
  test("служебные имена Object.prototype в пути — как прислал сервер", () => {
    expect(formProblem("deities.0.constructor: лишнее поле")).toBe(
      "Боги → № 1 → constructor: лишнее поле",
    );
    expect(formProblem("npcs.0.toString: лишнее поле")).toBe(
      "Персонажи → № 1 → toString: лишнее поле",
    );
    expect(formProblem("deities.0.__proto__: лишнее поле")).toBe(
      "Боги → № 1 → __proto__: лишнее поле",
    );
    expect(publishProblem("constructor: лишнее поле")).toBe(
      "constructor: лишнее поле",
    );
    // Служебное имя после подписанного поля — тоже как прислал сервер.
    expect(formProblem("encounters.0.monsters.__proto__: лишнее поле")).toBe(
      "Бои → № 1 → Монстры → __proto__: лишнее поле",
    );
  });
});
