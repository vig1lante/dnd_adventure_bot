import { fireEvent, screen, within } from "@testing-library/react";
import type { Draft } from "../../api/types";
import { formatMeters } from "../../ui/format";
import type { Routes } from "./fixtures";
import {
  actionOf,
  callsTo,
  choose,
  draftOf,
  encounterOf,
  group,
  jobOf,
  locationOf,
  monsterGroupOf,
  monsterOf,
  openStep,
  optionValues,
  press,
  renderEditor,
  srdMonsterOf,
  typeIn,
} from "./fixtures";

const ENCOUNTERS = "/api/drafts/d1/sections/encounters";
const MONSTERS = "/api/drafts/d1/sections/monsters";
const LOCKS = "/api/drafts/d1/locks";
const SRD = "/api/srd/monsters";

const CRYPT = locationOf({
  id: "crypt",
  name: "Склеп",
  zones: [
    { id: "here", name: "здесь" },
    { id: "altar", name: "у алтаря" },
  ],
  encounter_id: "crypt-ghouls",
});
const HALL = locationOf({ id: "hall", name: "Зал" });
const GHOULS = encounterOf({
  id: "crypt-ghouls",
  location_id: "crypt",
  trigger: "Кто-то открывает гроб",
  tactics: "Окружают самого слабого",
  monsters: [monsterGroupOf({ monster_id: "ghoul", count: 2, zone: "altar" })],
  pc_zone: "here",
});
const HORROR = monsterOf({
  id: "crypt-horror",
  name: "Ужас склепа",
  resistances: ["necrotic"],
  skills: { stealth: 4 },
});
const SKELETON = srdMonsterOf();
const GHOUL = srdMonsterOf({ id: "ghoul", name: "Гуль", cr: 1, xp: 200 });

const world = (overrides: Partial<Draft> = {}) =>
  draftOf({
    version: 3,
    locations: [CRYPT, HALL],
    encounters: [GHOULS],
    monsters: [HORROR],
    ...overrides,
  });

function openEncounters(draft: Draft, routes: Routes = {}) {
  return openStep(draft, "Бои и боссы", {
    [`GET ${SRD}`]: { json: [SKELETON, GHOUL] },
    [`PUT ${MONSTERS}`]: { json: { ...draft, version: draft.version + 1 } },
    [`PUT ${ENCOUNTERS}`]: ({ body }) => ({
      json: {
        ...draft,
        version: (body as { version: number }).version + 1,
      },
    }),
    ...routes,
  });
}

const save = (form: HTMLElement) => press(form, "Сохранить раздел");
const puts = (server: { calls: { method: string; path: string }[] }) =>
  server.calls
    .filter((call) => call.method === "PUT" && call.path.includes("/sections/"))
    .map((call) => call.path);
/** Ждёт, пока справочник SRD попадёт в выбор монстра (скелета нет ни в одном бою черновика). */
const srdLoaded = (scope: HTMLElement) =>
  vi.waitFor(() => expect(optionValues(scope, "Монстр")).toContain("skeleton"));

describe("шаг «Бои и боссы»: бои", () => {
  test("бой из черновика: локация, триггер, тактика, зона героев, босс и группы монстров", async () => {
    const { form } = await openEncounters(world());

    const fight = group(form, "Бой crypt-ghouls");
    await vi.waitFor(() =>
      expect(
        within(fight).getByRole("combobox", { name: "Локация" }),
      ).toHaveValue("location:crypt"),
    );
    expect(
      within(fight).getByRole("textbox", { name: "Что запускает бой" }),
    ).toHaveValue("Кто-то открывает гроб");
    expect(within(fight).getByRole("textbox", { name: "Тактика" })).toHaveValue(
      "Окружают самого слабого",
    );
    expect(
      within(fight).getByRole("combobox", { name: "Где стоят герои" }),
    ).toHaveValue("here");
    expect(
      within(fight).getByRole("switch", { name: "Босс" }),
    ).not.toBeChecked();
    const pack = group(fight, "Группа 1");
    expect(within(pack).getByRole("combobox", { name: "Монстр" })).toHaveValue(
      "ghoul",
    );
    expect(within(pack).getByRole("combobox", { name: "Сколько" })).toHaveValue(
      "2",
    );
    expect(within(pack).getByRole("combobox", { name: "Зона" })).toHaveValue(
      "altar",
    );
  });

  test("монстр группы — из справочника SRD и своих статблоков; зоны — зоны локации боя", async () => {
    const { form } = await openEncounters(world());
    const pack = group(group(form, "Бой crypt-ghouls"), "Группа 1");

    await srdLoaded(pack);
    expect(optionValues(pack, "Монстр")).toEqual([
      "",
      "skeleton",
      "ghoul",
      "crypt-horror",
    ]);
    expect(
      within(pack).getByRole("option", { name: /Гуль/ }),
    ).toHaveTextContent("SRD");
    expect(
      within(pack).getByRole("option", { name: /Ужас склепа/ }),
    ).toHaveTextContent("свой");
    expect(optionValues(pack, "Зона")).toEqual(["here", "altar"]);
    expect(optionValues(pack, "Сколько")).toEqual(
      Array.from({ length: 20 }, (_, index) => String(index + 1)),
    );
  });

  test("новый бой с монстром SRD: все поля боя уходят списком encounters с версией", async () => {
    const { server, form } = await openEncounters(world());

    press(form, "Добавить бой");
    const fight = group(form, "Бой 2");
    typeIn(fight, "Идентификатор", "hall-guards");
    await vi.waitFor(() =>
      expect(optionValues(fight, "Локация")).toContain("location:hall"),
    );
    choose(fight, "Локация", "location:hall");
    typeIn(fight, "Что запускает бой", "Отряд входит в зал");
    typeIn(fight, "Тактика", "Бьют лучников");
    fireEvent.click(within(fight).getByRole("switch", { name: "Босс" }));
    press(fight, "Добавить монстров");
    const pack = group(fight, "Группа 1");
    await srdLoaded(pack);
    choose(pack, "Монстр", "skeleton");
    choose(pack, "Сколько", "3");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([ENCOUNTERS]));
    expect(callsTo(server, "PUT", ENCOUNTERS)[0].body).toEqual({
      version: 3,
      data: [
        GHOULS,
        {
          id: "hall-guards",
          location_id: "hall",
          trigger: "Отряд входит в зал",
          tactics: "Бьют лучников",
          monsters: [{ monster_id: "skeleton", count: 3, zone: "here" }],
          pc_zone: "here",
          boss: true,
        },
      ],
    });
  });

  test("удаление боя и группы монстров уходит списком без них", async () => {
    const twoPacks = encounterOf({
      ...GHOULS,
      monsters: [
        monsterGroupOf({ monster_id: "ghoul", count: 2, zone: "altar" }),
        monsterGroupOf({ monster_id: "crypt-horror" }),
      ],
    });
    const other = encounterOf({ id: "hall-rats", location_id: "hall" });
    const { server, form } = await openEncounters(
      world({ encounters: [twoPacks, other] }),
    );

    press(form, "Удалить: Бой hall-rats");
    press(group(form, "Бой crypt-ghouls"), "Удалить: Группа 1");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", ENCOUNTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", ENCOUNTERS)[0].body).toEqual({
      version: 3,
      data: [
        {
          ...GHOULS,
          monsters: [monsterGroupOf({ monster_id: "crypt-horror" })],
        },
      ],
    });
  });

  // Fix-раунд 1 B6 (§11, R9): сборка сервера сама привязывает бой к незакреплённой локации без
  // боя (первый бой этой локации, `assemble._linked`). Подсказка нужна, только когда привязки не
  // будет: локация закреплена, ссылается на другой бой или её привязку забирает первый бой.
  const UNLINKED = "Локация не ссылается на этот бой";

  test("незакреплённая локация без боя — подсказки нет: бой привяжет сборка; общий текст об этом", async () => {
    const lonely = encounterOf({ id: "hall-rats", location_id: "hall" });
    const { form } = await openEncounters(
      world({ encounters: [GHOULS, lonely] }),
    );

    expect(group(form, "Бой hall-rats")).not.toHaveTextContent(UNLINKED);
    expect(group(form, "Бой crypt-ghouls")).not.toHaveTextContent(UNLINKED);
    expect(form).toHaveTextContent(
      "Бой без ссылки локации сборка привяжет сама, если локация не закреплена.",
    );
  });

  test("закреплённая локация без ссылки на бой — подсказка выбрать его на «Наполнении»", async () => {
    const lonely = encounterOf({ id: "hall-rats", location_id: "hall" });
    const { form } = await openEncounters(
      world({ encounters: [GHOULS, lonely], locks: ["location:hall"] }),
    );

    expect(group(form, "Бой hall-rats")).toHaveTextContent(
      "Локация не ссылается на этот бой: выберите его в поле «Бой» на шаге «Наполнение».",
    );
  });

  test("локация ссылается на другой бой — подсказка", async () => {
    const second = encounterOf({ id: "crypt-rats", location_id: "crypt" });
    const { form } = await openEncounters(
      world({ encounters: [GHOULS, second] }),
    );

    expect(group(form, "Бой crypt-rats")).toHaveTextContent(UNLINKED);
    expect(group(form, "Бой crypt-ghouls")).not.toHaveTextContent(UNLINKED);
  });

  test("два боя незакреплённой локации без боя — сборка привяжет первый, у второго подсказка", async () => {
    const first = encounterOf({ id: "hall-rats", location_id: "hall" });
    const second = encounterOf({ id: "hall-bats", location_id: "hall" });
    const { form } = await openEncounters(
      world({ encounters: [first, second] }),
    );

    expect(group(form, "Бой hall-rats")).not.toHaveTextContent(UNLINKED);
    expect(group(form, "Бой hall-bats")).toHaveTextContent(UNLINKED);
  });

  test("сохранённый бой можно закрепить (encounter:<id>) и перегенерировать (шаг encounters с целью)", async () => {
    const { server, form } = await openEncounters(world(), {
      [`PUT ${LOCKS}`]: {
        json: world({ version: 4, locks: ["encounter:crypt-ghouls"] }),
      },
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "encounters", target: "encounter:crypt-ghouls" }),
      },
    });

    press(form, "Закрепить: Бой crypt-ghouls");
    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 3,
      locks: ["encounter:crypt-ghouls"],
    });

    // Закреплённое генерация не меняет: кнопка закрыта.
    await vi.waitFor(() =>
      expect(
        within(form).getByRole("button", {
          name: "Перегенерировать: Бой crypt-ghouls",
        }),
      ).toBeDisabled(),
    );
  });

  test("«Перегенерировать» бой ставит задание {step: encounters, target}", async () => {
    const { server, form } = await openEncounters(world(), {
      "POST /api/drafts/d1/generate": {
        status: 202,
        json: jobOf({ step: "encounters", target: "encounter:crypt-ghouls" }),
      },
    });

    press(form, "Перегенерировать: Бой crypt-ghouls");

    await vi.waitFor(() =>
      expect(callsTo(server, "POST", "/api/drafts/d1/generate")).toHaveLength(
        1,
      ),
    );
    expect(callsTo(server, "POST", "/api/drafts/d1/generate")[0].body).toEqual({
      step: "encounters",
      target: "encounter:crypt-ghouls",
    });
  });

  test("при несохранённых правках закрепить и перегенерировать бой нельзя", async () => {
    const { form } = await openEncounters(world());

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");

    expect(
      within(form).getByRole("button", { name: "Закрепить: Бой crypt-ghouls" }),
    ).toBeDisabled();
    expect(
      within(form).getByRole("button", {
        name: "Перегенерировать: Бой crypt-ghouls",
      }),
    ).toBeDisabled();
  });

  test("справочник SRD грузится, только когда открыт экран боёв, и один раз", async () => {
    const draft = world();
    const { server } = renderEditor(draft, {
      [`GET ${SRD}`]: { json: [SKELETON, GHOUL] },
    });
    await screen.findByRole("navigation", { name: "Шаги приключения" });
    const nav = () =>
      within(screen.getByRole("navigation", { name: "Шаги приключения" }));

    expect(callsTo(server, "GET", SRD)).toHaveLength(0);
    fireEvent.click(nav().getByRole("button", { name: /Бои и боссы/ }));
    await vi.waitFor(() => expect(callsTo(server, "GET", SRD)).toHaveLength(1));
    fireEvent.click(nav().getByRole("button", { name: /Карта/ }));
    fireEvent.click(nav().getByRole("button", { name: /Бои и боссы/ }));

    expect(callsTo(server, "GET", SRD)).toHaveLength(1);
  });

  test("сбой справочника SRD — текст у выбора монстра, свои статблоки на месте", async () => {
    const { form } = await openEncounters(world(), {
      [`GET ${SRD}`]: {
        status: 500,
        json: { detail: "Справочник недоступен." },
      },
    });
    const pack = group(group(form, "Бой crypt-ghouls"), "Группа 1");

    expect(
      await within(pack).findByText(
        "Справочник монстров не загрузился: Справочник недоступен.",
      ),
    ).toBeInTheDocument();
    expect(optionValues(pack, "Монстр")).toContain("crypt-horror");
  });
});

describe("шаг «Бои и боссы»: свои статблоки", () => {
  test("скорость в метрах на экране — в данных футы: 30 → «9 м», выбор «12 м» сохраняет 40", async () => {
    const { server, form } = await openEncounters(world());
    const horror = group(form, "Ужас склепа");
    const speed = within(horror).getByRole("combobox", { name: "Скорость" });

    expect(speed).toHaveValue("30");
    expect(within(speed).getByRole("option", { name: "9 м" })).toHaveValue(
      "30",
    );
    expect(within(speed).getByRole("option", { name: "12 м" })).toHaveValue(
      "40",
    );
    expect(horror).not.toHaveTextContent(/фут/);
    choose(horror, "Скорость", "40");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([MONSTERS]));
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [{ ...HORROR, speed: 40 }],
    });
  });

  test("скорость вне шага 1,5 м из данных не теряется: показана в метрах", async () => {
    const { form } = await openEncounters(
      world({ monsters: [monsterOf({ ...HORROR, speed: 33 })] }),
    );
    const speed = within(group(form, "Ужас склепа")).getByRole("combobox", {
      name: "Скорость",
    });

    expect(speed).toHaveValue("33");
    expect(within(speed).getByRole("option", { name: "9,9 м" })).toHaveValue(
      "33",
    );
  });

  test("новый свой статблок: все поля MonsterDef уходят разделом monsters", async () => {
    const { server, form } = await openEncounters(world());
    await vi.waitFor(() =>
      expect(
        optionValues(group(form, "Ужас склепа"), "Опасность (cr)"),
      ).toEqual(["0.25", "1"]),
    );

    press(form, "Добавить своего монстра");
    const king = group(form, "Свой монстр 2");
    typeIn(king, "Идентификатор", "bone-king");
    typeIn(king, "Название", "Костяной король");
    typeIn(king, "Название по-английски", "Bone King");
    choose(king, "Опасность (cr)", "1");
    typeIn(king, "Опыт (xp)", "200");
    typeIn(king, "Класс доспеха (КД)", "15");
    typeIn(king, "Хиты (HP)", "45");
    typeIn(king, "Кость хитов", "6d10+12");
    choose(king, "Скорость", "40");
    typeIn(king, "Сила", "18");
    press(king, "Добавить действие");
    const slam = group(king, "Действие 1");
    typeIn(slam, "Идентификатор действия", "slam");
    typeIn(slam, "Название действия", "Удар");
    typeIn(slam, "Бонус атаки", "5");
    typeIn(slam, "Урон", "2d6+3");
    choose(slam, "Тип урона", "bludgeoning");
    typeIn(slam, "Описание", "Тяжёлый удар");
    typeIn(king, "Особенности", "Не знает страха");
    save(form);

    await vi.waitFor(() => expect(puts(server)).toEqual([MONSTERS]));
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [
        HORROR,
        {
          id: "bone-king",
          name: "Костяной король",
          name_en: "Bone King",
          cr: 1,
          xp: 200,
          ac: 15,
          hp: 45,
          hp_dice: "6d10+12",
          speed: 40,
          scores: { str: 18, dex: 10, con: 10, int: 10, wis: 10, cha: 10 },
          saves: {},
          skills: {},
          resistances: [],
          vulnerabilities: [],
          immunities: [],
          actions: [
            {
              id: "slam",
              name: "Удар",
              kind: "attack",
              attack_bonus: 5,
              damage: "2d6+3",
              damage_type: "bludgeoning",
              range: "melee",
              save: null,
              dc: null,
              half_on_save: false,
              targets: 1,
              condition: null,
              multiattack: [],
              summary: "Тяжёлый удар",
              reach: false,
              secondary_damage: null,
              secondary_damage_type: null,
            },
          ],
          traits: ["Не знает страха"],
          tags: [],
        },
      ],
    });
  });

  test("новый статблок начинает с опасности и опыта первого монстра справочника", async () => {
    const { form } = await openEncounters(world());
    await vi.waitFor(() =>
      expect(
        optionValues(group(form, "Ужас склепа"), "Опасность (cr)"),
      ).toEqual(["0.25", "1"]),
    );

    press(form, "Добавить своего монстра");
    const fresh = group(form, "Свой монстр 2");

    expect(
      within(fresh).getByRole("combobox", { name: "Опасность (cr)" }),
    ).toHaveValue("0.25");
    expect(
      within(fresh).getByRole("textbox", { name: "Опыт (xp)" }),
    ).toHaveValue("50");
  });

  test("опыт: подсказка из справочника — пары «опасность — опыт», правило проверяет сервер", async () => {
    const { form } = await openEncounters(world());
    const horror = group(form, "Ужас склепа");

    await vi.waitFor(() =>
      expect(horror).toHaveTextContent(
        "Опыт — как у монстров SRD той же опасности: 1/4 — 50, 1 — 200.",
      ),
    );
    expect(within(horror).getByRole("option", { name: "1/4" })).toHaveValue(
      "0.25",
    );
  });

  test("действие со спасброском и мультиатака: свои поля по виду действия", async () => {
    const breath = actionOf({
      id: "breath",
      name: "Дыхание",
      kind: "save",
      attack_bonus: null,
      damage: "2d6",
      damage_type: "cold",
      save: "dex",
      dc: 12,
      half_on_save: true,
      targets: 3,
    });
    const flurry = actionOf({
      id: "flurry",
      name: "Шквал",
      kind: "multiattack",
      attack_bonus: null,
      damage: null,
      damage_type: null,
      multiattack: ["claw", "claw"],
    });
    const { server, form } = await openEncounters(
      world({
        monsters: [
          monsterOf({ ...HORROR, actions: [actionOf(), breath, flurry] }),
        ],
      }),
    );
    const horror = group(form, "Ужас склепа");

    const save1 = group(horror, "Дыхание");
    expect(
      within(save1).getByRole("combobox", { name: "Спасбросок" }),
    ).toHaveValue("dex");
    expect(
      within(save1).getByRole("textbox", { name: "Сл спасброска" }),
    ).toHaveValue("12");
    expect(
      within(save1).getByRole("switch", { name: "Половина урона при успехе" }),
    ).toBeChecked();
    expect(
      within(save1).queryByRole("textbox", { name: "Бонус атаки" }),
    ).toBeNull();
    typeIn(save1, "Целей", "4");
    const multi = group(horror, "Шквал");
    expect(
      within(multi).getByRole("textbox", { name: "Действия по очереди" }),
    ).toHaveValue("claw\nclaw");
    typeIn(multi, "Действия по очереди", "claw\nbreath");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [
        {
          ...HORROR,
          actions: [
            actionOf(),
            { ...breath, targets: 4 },
            { ...flurry, multiattack: ["claw", "breath"] },
          ],
        },
      ],
    });
  });

  test("поля статблока, которых нет в форме (сопротивления, навыки, метки), сохраняются как были", async () => {
    const { server, form } = await openEncounters(world());

    typeIn(group(form, "Ужас склепа"), "Название", "Страж склепа");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [{ ...HORROR, name: "Страж склепа" }],
    });
  });

  test("не число в статблоке — ошибка у поля, «Сохранить раздел» закрыт", async () => {
    const { form } = await openEncounters(world());
    const horror = group(form, "Ужас склепа");

    typeIn(horror, "Класс доспеха (КД)", "много");

    expect(within(horror).getByText("Нужно целое число.")).toBeInTheDocument();
    expect(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    ).toBeDisabled();
    typeIn(horror, "Класс доспеха (КД)", "14");
    expect(
      within(form).getByRole("button", { name: "Сохранить раздел" }),
    ).toBeEnabled();
  });

  test("сохранённый свой монстр закрепляется ссылкой monster:<id>", async () => {
    const { server, form } = await openEncounters(world(), {
      [`PUT ${LOCKS}`]: { json: world({ version: 4 }) },
    });

    press(form, "Закрепить: Ужас склепа");

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", LOCKS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", LOCKS)[0].body).toEqual({
      version: 3,
      locks: ["monster:crypt-horror"],
    });
  });
});

describe("шаг «Бои и боссы»: запись двух разделов", () => {
  test("правки статблоков и боёв: сначала monsters, затем encounters — каждая со своей версией", async () => {
    const { server, form } = await openEncounters(world());

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут к выходу");
    typeIn(group(form, "Ужас склепа"), "Название", "Страж склепа");
    save(form);

    await vi.waitFor(() =>
      expect(puts(server)).toEqual([MONSTERS, ENCOUNTERS]),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toMatchObject({
      version: 3,
    });
    expect(callsTo(server, "PUT", ENCOUNTERS)[0].body).toEqual({
      version: 4,
      data: [{ ...GHOULS, tactics: "Бегут к выходу" }],
    });
  });

  test("409 version_conflict на боях после записанных монстров: диалог и текст «Сохранено: свои монстры…»", async () => {
    const { form } = await openEncounters(world(), {
      [`PUT ${ENCOUNTERS}`]: {
        status: 409,
        json: { detail: "Черновик изменился.", code: "version_conflict" },
      },
    });

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");
    typeIn(group(form, "Ужас склепа"), "Название", "Страж");
    save(form);

    expect(
      await screen.findByRole("alertdialog", {
        name: "Черновик изменился, обновить?",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Сохранено: свои монстры. Не сохранено: бои — обновите черновик и повторите правку.",
      ),
    ).toBeInTheDocument();
  });

  test("422 invalid_data: текст сервера и подробности, правка на месте", async () => {
    const { form } = await openEncounters(world(), {
      [`PUT ${ENCOUNTERS}`]: {
        status: 422,
        json: {
          detail: "Раздел не прошёл проверку.",
          code: "invalid_data",
          problems: ["0.monsters.0.count: не больше 20"],
        },
      },
    });

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");
    save(form);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Раздел не прошёл проверку.",
    );
    expect(
      screen.getByText("0.monsters.0.count: не больше 20"),
    ).toBeInTheDocument();
    expect(
      within(group(form, "Бой crypt-ghouls")).getByRole("textbox", {
        name: "Тактика",
      }),
    ).toHaveValue("Бегут");
  });

  test("409 job_running: текст сервера без диалога", async () => {
    const { form } = await openEncounters(world(), {
      [`PUT ${ENCOUNTERS}`]: {
        status: 409,
        json: { detail: "Идёт генерация: подождите.", code: "job_running" },
      },
    });

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");
    save(form);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Идёт генерация: подождите.",
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});

// --- финальное покрытие блока 6 ---

describe("скорость своего монстра: метры на экране ↔ футы в данных (Р3)", () => {
  test("все варианты — шаг 5 футов до 120: подпись — formatMeters, подписи не повторяются", async () => {
    const { form } = await openEncounters(world());
    const speed = within(group(form, "Ужас склепа")).getByRole("combobox", {
      name: "Скорость",
    });

    const options = within(speed)
      .getAllByRole("option")
      .map((option) => [
        (option as HTMLOptionElement).value,
        option.textContent,
      ]);
    const feet = Array.from({ length: 25 }, (_, index) => index * 5);
    expect(options).toEqual(
      feet.map((value) => [String(value), formatMeters(value)]),
    );
    // Разным футам — разные метры: выбор по подписи однозначно даёт число футов.
    expect(new Set(options.map(([, label]) => label)).size).toBe(feet.length);
    expect(options[1]).toEqual(["5", "1,5 м"]);
    expect(options.at(-1)).toEqual(["120", "36 м"]);
  });

  test.each([
    ["0 м", 0],
    ["1,5 м", 5],
    ["10,5 м", 35],
    ["36 м", 120],
  ] as const)(
    "выбор «%s» уходит целыми футами %i без округления",
    async (meters, feet) => {
      const { server, form } = await openEncounters(world());
      const speed = within(group(form, "Ужас склепа")).getByRole("combobox", {
        name: "Скорость",
      });

      fireEvent.change(speed, {
        target: {
          value: within(speed)
            .getByRole("option", { name: meters })
            .getAttribute("value"),
        },
      });
      save(form);

      await vi.waitFor(() =>
        expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
      );
      expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
        version: 3,
        data: [{ ...HORROR, speed: feet }],
      });
    },
  );

  test("скорость вне шага (33 фута) не дрейфует: правка другого поля уходит с теми же 33", async () => {
    const odd = monsterOf({ ...HORROR, speed: 33 });
    const { server, form } = await openEncounters(world({ monsters: [odd] }));

    typeIn(group(form, "Ужас склепа"), "Хиты (HP)", "30");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [{ ...odd, hp: 30 }],
    });
  });
});

describe("опасность (cr) и опыт (xp) своего монстра", () => {
  const RAT = srdMonsterOf({
    id: "giant_rat",
    name: "Гигантская крыса",
    cr: 0.125,
    xp: 25,
  });

  test("дробная опасность «1/8» уходит числом 0.125, опыт — числом", async () => {
    const { server, form } = await openEncounters(world(), {
      [`GET ${SRD}`]: { json: [RAT, SKELETON, GHOUL] },
    });
    const horror = group(form, "Ужас склепа");
    await vi.waitFor(() =>
      expect(optionValues(horror, "Опасность (cr)")).toEqual([
        "0.125",
        "0.25",
        "1",
      ]),
    );
    expect(within(horror).getByRole("option", { name: "1/8" })).toHaveValue(
      "0.125",
    );

    choose(horror, "Опасность (cr)", "0.125");
    typeIn(horror, "Опыт (xp)", "25");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [{ ...HORROR, cr: 0.125, xp: 25 }],
    });
  });

  test("опасность из данных вне справочника видна как есть и уходит без подмены", async () => {
    const strong = monsterOf({ ...HORROR, cr: 5, xp: 1800 });
    const { server, form } = await openEncounters(
      world({ monsters: [strong] }),
    );
    const horror = group(form, "Ужас склепа");
    await vi.waitFor(() =>
      expect(optionValues(horror, "Опасность (cr)")).toEqual([
        "0.25",
        "1",
        "5",
      ]),
    );
    expect(
      within(horror).getByRole("combobox", { name: "Опасность (cr)" }),
    ).toHaveValue("5");
    expect(
      within(horror).getByRole("option", { name: "5 — нет в списке" }),
    ).toBeInTheDocument();

    typeIn(horror, "Название", "Страж");
    save(form);

    await vi.waitFor(() =>
      expect(callsTo(server, "PUT", MONSTERS)).toHaveLength(1),
    );
    expect(callsTo(server, "PUT", MONSTERS)[0].body).toEqual({
      version: 3,
      data: [{ ...strong, name: "Страж" }],
    });
  });
});

describe("«Бои и боссы»: новый свой монстр в новом бою", () => {
  test("группа может выбрать ещё не сохранённый статблок: monsters с ним уходят первыми, бой ссылается на него", async () => {
    const { server, form } = await openEncounters(world());

    press(form, "Добавить своего монстра");
    const king = group(form, "Свой монстр 2");
    typeIn(king, "Идентификатор", "bone-king");
    typeIn(king, "Название", "Костяной король");
    press(form, "Добавить бой");
    const fight = group(form, "Бой 2");
    typeIn(fight, "Идентификатор", "throne");
    await vi.waitFor(() =>
      expect(optionValues(fight, "Локация")).toContain("location:hall"),
    );
    choose(fight, "Локация", "location:hall");
    fireEvent.click(within(fight).getByRole("switch", { name: "Босс" }));
    press(fight, "Добавить монстров");
    const pack = group(fight, "Группа 1");
    expect(
      within(pack).getByRole("option", { name: "Костяной король (свой)" }),
    ).toHaveValue("bone-king");
    choose(pack, "Монстр", "bone-king");
    save(form);

    await vi.waitFor(() =>
      expect(puts(server)).toEqual([MONSTERS, ENCOUNTERS]),
    );
    const [monsters] = callsTo(server, "PUT", MONSTERS);
    const [encounters] = callsTo(server, "PUT", ENCOUNTERS);
    expect(monsters.body).toMatchObject({
      version: 3,
      data: [HORROR, { id: "bone-king", name: "Костяной король" }],
    });
    expect(encounters.body).toEqual({
      version: 4,
      data: [
        GHOULS,
        {
          id: "throne",
          location_id: "hall",
          trigger: "",
          tactics: "",
          monsters: [{ monster_id: "bone-king", count: 1, zone: "here" }],
          pc_zone: "here",
          boss: true,
        },
      ],
    });
  });
});

describe("«Бои и боссы»: пределы сервера (B6 §5) — отказ 422 с problems", () => {
  // Сервер отвечает как настоящий: в черновике — то, что прислал клиент.
  const echo =
    (section: "monsters" | "encounters") =>
    ({ body }: { body: unknown }) => {
      const { version, data } = body as { version: number; data: unknown };
      return { json: { ...world(), version: version + 1, [section]: data } };
    };

  test("отказ первой записи (своих монстров больше 6): бои не пишутся, правки на месте, текста о частичном сохранении нет", async () => {
    const { server, form } = await openEncounters(world(), {
      [`PUT ${MONSTERS}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems: ["monsters: не больше 6 элементов"],
        },
      },
    });

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");
    typeIn(group(form, "Ужас склепа"), "Название", "Страж");
    save(form);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Данные раздела не прошли проверку.",
    );
    expect(
      screen.getByText("monsters: не больше 6 элементов"),
    ).toBeInTheDocument();
    expect(puts(server)).toEqual([MONSTERS]);
    expect(screen.queryByText(/Сохранено:/)).toBeNull();
    expect(
      within(group(form, "Бой crypt-ghouls")).getByRole("textbox", {
        name: "Тактика",
      }),
    ).toHaveValue("Бегут");
    expect(
      within(group(form, "Страж")).getByRole("textbox", { name: "Название" }),
    ).toHaveValue("Страж");
  });

  test("отказ второй записи (пределы боя): все строки problems и текст «Сохранено: свои монстры…» с «Обновить»", async () => {
    const problems = [
      "encounters.0.monsters: не больше 4 элементов",
      "encounters.0.monsters: всего монстров в бою — не больше 20",
      "encounters: не больше 8 элементов",
    ];
    const { server, form } = await openEncounters(world(), {
      [`PUT ${MONSTERS}`]: echo("monsters"),
      [`PUT ${ENCOUNTERS}`]: {
        status: 422,
        json: {
          detail: "Данные раздела не прошли проверку.",
          code: "invalid_data",
          problems,
        },
      },
    });

    typeIn(group(form, "Бой crypt-ghouls"), "Тактика", "Бегут");
    typeIn(group(form, "Ужас склепа"), "Название", "Страж");
    save(form);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Данные раздела не прошли проверку.");
    expect(
      within(alert)
        .getAllByRole("listitem")
        .map((row) => row.textContent),
    ).toEqual(problems);
    expect(puts(server)).toEqual([MONSTERS, ENCOUNTERS]);
    expect(
      screen.getByText(
        "Сохранено: свои монстры. Не сохранено: бои — обновите черновик и повторите правку.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Обновить" }),
    ).toBeInTheDocument();
    // Диалога конфликта нет: это отказ данных, а не чужая версия.
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});
