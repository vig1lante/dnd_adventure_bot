import { render, screen, within } from "@testing-library/react";
import { createApi } from "../api/client";
import { ApiContext } from "../api/context";
import type { Character, DerivedAbility } from "../api/types";
import { OPTIONS } from "../screens/character/fixtures";
import { sessionToken } from "../store/game";
import { fakeServer, signInAs } from "../test-setup";
import type { FakeReply } from "../test-setup";
import { derivedOf, sheetHero } from "./fixtures";
import { CharacterSheet } from "./CharacterSheet";

const OPTIONS_ROUTE = "GET /api/content/creation-options";

function renderSheet(
  character: Character | null,
  reply: FakeReply = { json: OPTIONS },
) {
  signInAs({ id: "u1", name: "Вика" });
  const server = fakeServer({ [OPTIONS_ROUTE]: reply });
  render(
    <ApiContext value={createApi(server.fetch, sessionToken)}>
      <CharacterSheet character={character} />
    </ApiContext>,
  );
  return server;
}

/** Плитка листа «подпись — значение»: ищется по подписи. */
function stat(label: string): HTMLElement {
  const term = screen.getByText(label, { selector: "dt" });
  const tile = term.closest("div");
  if (tile === null) throw new Error(`Нет плитки «${label}»`);
  return tile;
}

function listItem(text: string, list = "listitem"): HTMLElement {
  const items = screen.getAllByRole(list as "listitem");
  const found = items.find((item) => item.textContent?.includes(text));
  if (found === undefined) throw new Error(`Нет строки «${text}»`);
  return found;
}

describe("шапка и здоровье", () => {
  test("имя, а после загрузки правил — русские названия класса, вида и предыстории", async () => {
    renderSheet(sheetHero());

    expect(screen.getByRole("heading", { name: "Лира" })).toBeInTheDocument();
    expect(
      await screen.findByText("Плут · Эльф · Преступник · уровень 1"),
    ).toBeInTheDocument();
  });

  test("правила не пришли — лист всё равно показан, с id вместо названий", async () => {
    renderSheet(sheetHero(), { status: 500, json: { detail: "Сбой." } });

    expect(
      await screen.findByText("rogue · elf · criminal · уровень 1"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("здоровье берётся из листа: 9 из 10", () => {
    renderSheet(sheetHero({ hp: 9, hp_max: 10, temp_hp: 2 }));

    const meter = screen.getByRole("meter", { name: "Здоровье" });
    expect(meter).toHaveAttribute("aria-valuenow", "9");
    expect(meter).toHaveAttribute("aria-valuemax", "10");
    expect(within(meter).getByText("+2")).toBeInTheDocument();
  });

  test("героя нет — пояснение", () => {
    renderSheet(null);

    expect(screen.getByText("У вас нет героя в этой игре")).toBeInTheDocument();
  });

  test("имя героя выводится как текст", () => {
    const { container } = render(
      <ApiContext
        value={createApi(
          fakeServer({ [OPTIONS_ROUTE]: { json: OPTIONS } }).fetch,
          sessionToken,
        )}
      >
        <CharacterSheet character={sheetHero({ name: "<b>Лира</b>" })} />
      </ApiContext>,
    );

    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("<b>Лира</b>");
  });
});

describe("числа листа приходят от сервера", () => {
  test("класс доспеха, мастерство, пассивная внимательность", () => {
    renderSheet(sheetHero());

    expect(stat("Класс доспеха")).toHaveTextContent("14");
    expect(stat("Бонус мастерства")).toHaveTextContent("+2");
    expect(stat("Пассивная внимательность")).toHaveTextContent("13");
  });

  test("клиент ничего не считает: несогласованные числа показываются как есть", () => {
    const derived = derivedOf({
      armor_class: 99,
      proficiency_bonus: 7,
      passive_perception: 41,
      abilities: derivedOf().abilities.map((line): DerivedAbility =>
        line.id === "dex" ? { ...line, modifier: 7, save_bonus: -4 } : line,
      ),
    });
    // Ловкость 8: по правилам это −1, но формулы клиенту неизвестны.
    renderSheet(
      sheetHero({
        scores: { str: 10, dex: 8, con: 10, int: 10, wis: 10, cha: 10 },
        derived,
      }),
    );

    expect(stat("Класс доспеха")).toHaveTextContent("99");
    expect(stat("Бонус мастерства")).toHaveTextContent("+7");
    expect(stat("Пассивная внимательность")).toHaveTextContent("41");
    const dex = listItem("Ловкость");
    expect(dex).toHaveTextContent("+7");
    expect(dex).toHaveTextContent("Спасбросок −4");
    expect(dex).toHaveTextContent("8");
  });

  test("кости хитов и вдохновение", () => {
    renderSheet(sheetHero({ hit_dice: 1, hit_dice_max: 3, inspiration: true }));

    expect(stat("Кости хитов")).toHaveTextContent("1 из 3");
    expect(stat("Вдохновение")).toHaveTextContent("Есть");
  });

  test("вдохновения нет — так и сказано", () => {
    renderSheet(sheetHero({ inspiration: false }));

    expect(stat("Вдохновение")).toHaveTextContent("Нет");
  });

  test("без блока derived чисел правил нет, но лист не ломается", () => {
    renderSheet(sheetHero({ derived: undefined }));

    expect(screen.getByRole("heading", { name: "Лира" })).toBeInTheDocument();
    expect(screen.queryByText("Класс доспеха")).toBeNull();
    expect(screen.queryByText("Навыки")).toBeNull();
  });
});

describe("характеристики и навыки", () => {
  test("шесть характеристик: название, значение, модификатор и спасбросок", () => {
    renderSheet(sheetHero());

    const dex = listItem("Ловкость");
    expect(dex).toHaveTextContent("+3");
    expect(dex).toHaveTextContent("16");
    expect(dex).toHaveTextContent("Спасбросок +5");
    const str = listItem("Сила");
    expect(str).toHaveTextContent("−1");
    expect(str).toHaveTextContent("Спасбросок −1");
  });

  test("владение спасброском отмечено словом", () => {
    renderSheet(sheetHero());

    expect(listItem("Ловкость")).toHaveTextContent("мастерство");
    expect(listItem("Сила")).not.toHaveTextContent("мастерство");
  });

  test("все восемнадцать навыков с бонусами сервера", () => {
    renderSheet(sheetHero());

    const skills = within(
      screen.getByRole("list", { name: "Навыки" }),
    ).getAllByRole("listitem");
    expect(skills).toHaveLength(18);
    expect(listItem("Скрытность")).toHaveTextContent("+7");
    expect(listItem("Атлетика")).toHaveTextContent("−1");
  });

  test("мастерство и компетентность названы словами", () => {
    renderSheet(sheetHero());

    expect(listItem("Скрытность")).toHaveTextContent("компетентность");
    expect(listItem("Внимательность")).toHaveTextContent("мастерство");
    expect(listItem("Атлетика")).not.toHaveTextContent(
      /мастерство|компетентность/,
    );
  });
});

describe("атаки", () => {
  test("надетое оружие идёт первым, запасное следом", () => {
    renderSheet(sheetHero());

    const attacks = within(
      screen.getByRole("list", { name: "Атаки" }),
    ).getAllByRole("listitem");
    expect(
      attacks.map((item) => item.querySelector("strong")?.textContent),
    ).toEqual(["Кинжал", "Короткий лук"]);
    expect(attacks[0]).toHaveTextContent("Надето");
    expect(attacks[1]).toHaveTextContent("В запасе");
  });

  test("бонус атаки, кости урона по-русски, тип урона и дальность подписаны по-русски", () => {
    renderSheet(sheetHero());

    const dagger = listItem("Кинжал");
    expect(dagger).toHaveTextContent("+5 к атаке");
    expect(dagger).toHaveTextContent("1к4+3");
    expect(dagger).toHaveTextContent("колющий");
    expect(dagger).toHaveTextContent("ближний бой");
    expect(listItem("Короткий лук")).toHaveTextContent("дальняя дистанция");
  });

  test("неизвестный тип урона остаётся id", () => {
    renderSheet(
      sheetHero({
        derived: derivedOf({
          attacks: [
            {
              item_id: "wand",
              name: "Жезл",
              equipped: true,
              bonus: 1,
              damage: "1d4",
              damage_type: "sonic_boom",
              range: "near",
              ability: "int",
            },
          ],
        }),
      }),
    );

    expect(listItem("Жезл")).toHaveTextContent("sonic_boom");
  });

  test("порядок внутри надетых и внутри запасных сохраняется", () => {
    const base = derivedOf().attacks[1];
    renderSheet(
      sheetHero({
        derived: derivedOf({
          attacks: [
            { ...base, item_id: "a", name: "Запасной А", equipped: false },
            { ...base, item_id: "b", name: "Надетый Б", equipped: true },
            { ...base, item_id: "c", name: "Запасной В", equipped: false },
            { ...base, item_id: "d", name: "Надетый Г", equipped: true },
          ],
        }),
      }),
    );

    const names = within(screen.getByRole("list", { name: "Атаки" }))
      .getAllByRole("listitem")
      .map((item) => item.querySelector("strong")?.textContent);
    expect(names).toEqual([
      "Надетый Б",
      "Надетый Г",
      "Запасной А",
      "Запасной В",
    ]);
  });

  test("оружия нет — раздела атак нет", () => {
    renderSheet(sheetHero({ derived: derivedOf({ attacks: [] }) }));

    expect(screen.queryByRole("list", { name: "Атаки" })).toBeNull();
  });
});

describe("заклинания", () => {
  const caster = () =>
    sheetHero({
      class_id: "wizard",
      derived: derivedOf({ spell_save_dc: 13, spell_attack_bonus: 5 }),
      spell_slots_max: { "1": 2, "2": 1, "3": 0 },
      spell_slots: { "1": 1, "2": 1, "3": 0 },
      cantrips: ["fire_bolt", "unknown_cantrip"],
      prepared_spells: ["shield", "magic_missile"],
      concentration: "shield",
    });

  test("Сл и бонус атаки заклинаниями — числа сервера", () => {
    renderSheet(caster());

    expect(stat("Сл спасброска заклинаний")).toHaveTextContent("13");
    expect(stat("Бонус атаки заклинанием")).toHaveTextContent("+5");
  });

  test("ячейки по уровням: сколько осталось из скольких; пустые уровни не показываются", () => {
    renderSheet(caster());

    expect(listItem("1 уровень")).toHaveTextContent("1 из 2");
    expect(listItem("2 уровень")).toHaveTextContent("1 из 1");
    expect(screen.queryByText(/3 уровень/)).toBeNull();
  });

  test("заговоры и заклинания названы по правилам создания; неизвестные — id", async () => {
    renderSheet(caster());

    expect(await screen.findByText("Огненная стрела")).toBeInTheDocument();
    expect(screen.getByText("unknown_cantrip")).toBeInTheDocument();
    expect(screen.getByText("Магическая стрела")).toBeInTheDocument();
    expect(stat("Концентрация")).toHaveTextContent("Щит");
  });

  test("у героя без заклинаний раздела нет", () => {
    renderSheet(sheetHero());

    expect(screen.queryByText("Сл спасброска заклинаний")).toBeNull();
    expect(screen.queryByText("Заклинания")).toBeNull();
  });
});

describe("снаряжение, состояния, характер", () => {
  test("снаряжение: названия, количество, надетое помечено", async () => {
    renderSheet(sheetHero());

    const dagger = await screen.findByText("Кинжал × 2");
    expect(dagger.closest("li")).toHaveTextContent("Надето");
    expect(screen.getByText("Воровские инструменты")).toBeInTheDocument();
  });

  test("предмет, которого нет в правилах создания (находка приключения), показан по id", () => {
    renderSheet(
      sheetHero({
        inventory: [{ item_id: "silver_key", qty: 1, equipped: false }],
      }),
    );

    expect(screen.getByText("silver_key")).toBeInTheDocument();
  });

  test("предмет приключения показан названием от сервера, а не id", () => {
    renderSheet(
      sheetHero({
        inventory: [
          {
            item_id: "chest-key",
            qty: 1,
            equipped: false,
            name: "Запасной ключ Ульриха",
          },
        ],
      }),
    );

    expect(screen.getByText("Запасной ключ Ульриха")).toBeInTheDocument();
    expect(screen.queryByText("chest-key")).toBeNull();
  });

  test("название предмета приключения — и с количеством, и с пометкой «Надето»", () => {
    renderSheet(
      sheetHero({
        inventory: [
          {
            item_id: "healing-potion",
            qty: 2,
            equipped: false,
            name: "Зелье лечения",
          },
          {
            item_id: "ulrich-ring",
            qty: 1,
            equipped: true,
            name: "Перстень Ульриха",
          },
        ],
      }),
    );

    expect(screen.getByText("Зелье лечения × 2")).toBeInTheDocument();
    expect(
      screen.getByText("Перстень Ульриха").closest("li"),
    ).toHaveTextContent("Надето");
  });

  test("название от сервера сильнее названия из правил создания", async () => {
    renderSheet(
      sheetHero({
        inventory: [
          {
            item_id: "dagger",
            qty: 1,
            equipped: true,
            name: "Кинжал Ульриха",
          },
        ],
      }),
    );
    // Правила уже загружены: заголовок листа получил русские названия.
    await screen.findByText("Плут · Эльф · Преступник · уровень 1");

    // «Кинжал» без Ульриха остаётся в карточке атак, поэтому смотрим только снаряжение.
    const gear = within(screen.getByRole("list", { name: "Снаряжение" }));
    expect(gear.getByText("Кинжал Ульриха")).toBeInTheDocument();
    expect(gear.queryByText("Кинжал")).toBeNull();
  });

  test("в одном снаряжении: приключение — по name, SRD — по правилам, неизвестный — по id", async () => {
    renderSheet(
      sheetHero({
        inventory: [
          { item_id: "dagger", qty: 2, equipped: true },
          { item_id: "chest-key", qty: 1, equipped: false, name: "Ключ" },
          { item_id: "mystery_item", qty: 1, equipped: false },
        ],
      }),
    );

    expect(await screen.findByText("Кинжал × 2")).toBeInTheDocument();
    expect(screen.getByText("Ключ")).toBeInTheDocument();
    expect(screen.getByText("mystery_item")).toBeInTheDocument();
  });

  test("правила создания не пришли: предмет приключения всё равно назван, SRD остаётся id", async () => {
    renderSheet(
      sheetHero({
        inventory: [
          { item_id: "dagger", qty: 1, equipped: false },
          { item_id: "chest-key", qty: 1, equipped: false, name: "Ключ" },
        ],
      }),
      { status: 500, json: { detail: "Сбой." } },
    );

    expect(
      await screen.findByText("rogue · elf · criminal · уровень 1"),
    ).toBeInTheDocument();
    expect(screen.getByText("Ключ")).toBeInTheDocument();
    expect(screen.getByText("dagger")).toBeInTheDocument();
  });

  test("название предмета выводится только как текст", () => {
    const name = "<img src=x onerror=alert(1)><b>Ключ</b>";
    const { container } = render(
      <ApiContext
        value={createApi(
          fakeServer({ [OPTIONS_ROUTE]: { json: OPTIONS } }).fetch,
          sessionToken,
        )}
      >
        <CharacterSheet
          character={sheetHero({
            inventory: [
              { item_id: "chest-key", qty: 1, equipped: false, name },
            ],
          })}
        />
      </ApiContext>,
    );

    expect(screen.getByText(name)).toBeInTheDocument();
    expect(container.querySelector("img, b")).toBeNull();
  });

  test("состояния и эффекты с названием и сроком", () => {
    renderSheet(
      sheetHero({
        conditions: [{ id: "poisoned", name: "Отравлен", rounds_left: null }],
        buffs: [{ id: "bless", name: "Благословение", rounds_left: 4 }],
      }),
    );

    expect(screen.getByText("Отравлен")).toBeInTheDocument();
    expect(screen.getByText("Благословение (4 р.)")).toBeInTheDocument();
  });

  test("спасброски от смерти показаны, когда герой на нуле", () => {
    renderSheet(
      sheetHero({
        hp: 0,
        death_saves: { successes: 1, failures: 2, stable: false },
      }),
    );

    expect(stat("Спасброски от смерти")).toHaveTextContent("успехи 1");
    expect(stat("Спасброски от смерти")).toHaveTextContent("провалы 2");
  });

  test("у здорового героя спасбросков от смерти нет", () => {
    renderSheet(sheetHero());

    expect(screen.queryByText("Спасброски от смерти")).toBeNull();
  });

  test("характер и история выводятся как текст и только если заполнены", () => {
    const { container } = render(
      <ApiContext
        value={createApi(
          fakeServer({ [OPTIONS_ROUTE]: { json: OPTIONS } }).fetch,
          sessionToken,
        )}
      >
        <CharacterSheet
          character={sheetHero({
            persona: {
              concept: "Вор-неудачник",
              appearance: "",
              traits: "<i>Хитра</i>",
              ideals: "",
              bonds: "",
              flaws: "",
              backstory: "Выросла в порту.",
            },
          })}
        />
      </ApiContext>,
    );

    expect(screen.getByText("Вор-неудачник")).toBeInTheDocument();
    expect(screen.getByText("Выросла в порту.")).toBeInTheDocument();
    expect(container.querySelector("i")).toBeNull();
    expect(container).toHaveTextContent("<i>Хитра</i>");
    expect(screen.queryByText("Внешность")).toBeNull();
  });
});

describe("правила создания грузятся один раз", () => {
  test("один запрос на показ листа, с токеном", async () => {
    const server = renderSheet(sheetHero());
    await screen.findByText("Плут · Эльф · Преступник · уровень 1");

    expect(server.calls).toHaveLength(1);
    expect(server.calls[0].headers.get("Authorization")).toBe("Bearer tok-u1");
  });
});

describe("пустые разделы и структура листа", () => {
  test("нет оружия, снаряжения и описания — этих разделов на листе нет", () => {
    renderSheet(
      sheetHero({
        derived: derivedOf({ attacks: [] }),
        inventory: [],
      }),
    );

    expect(screen.queryByRole("list", { name: "Атаки" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Снаряжение" })).toBeNull();
    expect(screen.queryByText("Характер и история")).toBeNull();
    // Остальное на месте: пустой раздел не должен утащить за собой лист.
    expect(screen.getByRole("list", { name: "Навыки" })).toBeInTheDocument();
  });

  test("разделы листа — заголовки третьего уровня под именем героя", () => {
    renderSheet(
      sheetHero({
        derived: derivedOf({ spell_save_dc: 13, spell_attack_bonus: 5 }),
        cantrips: ["fire_bolt"],
        persona: { ...sheetHero().persona, backstory: "Вырос у моря." },
      }),
    );

    expect(
      screen.getByRole("heading", { level: 2, name: "Лира" }),
    ).toBeInTheDocument();
    for (const title of [
      "Характеристики",
      "Навыки",
      "Атаки",
      "Заклинания",
      "Снаряжение",
      "Характер и история",
    ]) {
      expect(
        screen.getByRole("heading", { level: 3, name: title }),
      ).toBeInTheDocument();
    }
  });
});

describe("спасброски от смерти и раздел заклинаний: когда они появляются", () => {
  const SAVES = "Спасброски от смерти";
  const saves = (successes: number, failures: number, stable = false) => ({
    successes,
    failures,
    stable,
  });

  test("на ногах и без отметок раздела нет", () => {
    renderSheet(sheetHero({ hp: 1, death_saves: saves(0, 0) }));

    expect(screen.queryByText(SAVES)).toBeNull();
  });

  test.each([
    [
      "герой на 0 здоровья",
      { hp: 0, death_saves: saves(0, 0) },
      "успехи 0, провалы 0",
    ],
    [
      "уже есть успех",
      { hp: 3, death_saves: saves(1, 0) },
      "успехи 1, провалы 0",
    ],
    [
      "уже есть провал",
      { hp: 3, death_saves: saves(0, 2) },
      "успехи 0, провалы 2",
    ],
  ])("%s: раздел виден с числами сервера", (_name, patch, line) => {
    renderSheet(sheetHero(patch));

    expect(stat(SAVES)).toHaveTextContent(line);
  });

  test("стабилен — только когда сервер так сказал", () => {
    const { unmount } = render(
      <ApiContext
        value={createApi(
          fakeServer({ [OPTIONS_ROUTE]: { json: OPTIONS } }).fetch,
          sessionToken,
        )}
      >
        <CharacterSheet
          character={sheetHero({ hp: 0, death_saves: saves(1, 1, true) })}
        />
      </ApiContext>,
    );
    expect(stat(SAVES)).toHaveTextContent("успехи 1, провалы 1, стабилен");
    unmount();

    renderSheet(sheetHero({ hp: 0, death_saves: saves(1, 1, false) }));

    expect(stat(SAVES)).not.toHaveTextContent("стабилен");
  });

  test.each([
    ["только заговоры", { cantrips: ["fire_bolt"] }],
    ["только заготовленные заклинания", { prepared_spells: ["shield"] }],
    ["только Сл заклинаний", { derived: derivedOf({ spell_save_dc: 12 }) }],
  ])("%s: раздел «Заклинания» есть", (_name, patch) => {
    renderSheet(sheetHero(patch));

    expect(
      screen.getByRole("heading", { level: 3, name: "Заклинания" }),
    ).toBeInTheDocument();
  });

  test("надетое оружие помечено янтарём, запасное — нейтрально", () => {
    renderSheet(sheetHero());

    const [worn, spare] = within(
      screen.getByRole("list", { name: "Атаки" }),
    ).getAllByRole("listitem");
    expect(within(worn).getByText("Надето")).toHaveClass("ui-badge--amber");
    expect(within(spare).getByText("В запасе")).toHaveClass(
      "ui-badge--neutral",
    );
  });
});

describe("лист: мелкие границы показа", () => {
  const SAVES = "Спасброски от смерти";

  test("один провал при здоровье выше нуля уже виден", () => {
    renderSheet(
      sheetHero({
        hp: 4,
        death_saves: { successes: 0, failures: 1, stable: false },
      }),
    );

    expect(stat(SAVES)).toHaveTextContent("успехи 0, провалы 1");
  });

  test.each([
    [
      "только состояния",
      {
        conditions: [{ id: "poisoned", name: "Отравлен", rounds_left: null }],
        buffs: [],
      },
      ["Отравлен"],
    ],
    [
      "только эффекты",
      {
        conditions: [],
        buffs: [{ id: "bless", name: "Благословение", rounds_left: 3 }],
      },
      ["Благословение (3 р.)"],
    ],
  ])("%s: метки видны", (_name, patch, labels) => {
    renderSheet(sheetHero(patch));

    for (const label of labels) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  test("ячейка уровня, которой нет в остатке, показана как «0 из N»", () => {
    renderSheet(
      sheetHero({
        class_id: "wizard",
        derived: derivedOf({ spell_save_dc: 13, spell_attack_bonus: 5 }),
        spell_slots_max: { "1": 2, "2": 1 },
        spell_slots: { "1": 2 },
      }),
    );

    expect(listItem("2 уровень")).toHaveTextContent("0 из 1");
  });

  test("только заготовленные заклинания: списка заговоров нет, и наоборот", () => {
    const { unmount } = render(
      <ApiContext
        value={createApi(
          fakeServer({ [OPTIONS_ROUTE]: { json: OPTIONS } }).fetch,
          sessionToken,
        )}
      >
        <CharacterSheet
          character={sheetHero({ prepared_spells: ["shield"], cantrips: [] })}
        />
      </ApiContext>,
    );
    expect(
      screen.getByRole("list", { name: "Подготовленные заклинания" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Заговоры" })).toBeNull();
    unmount();

    renderSheet(sheetHero({ prepared_spells: [], cantrips: ["fire_bolt"] }));

    expect(screen.getByRole("list", { name: "Заговоры" })).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "Подготовленные заклинания" }),
    ).toBeNull();
  });
});
