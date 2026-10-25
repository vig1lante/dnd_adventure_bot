import type { Locator, Page, Response } from "@playwright/test";
import { expect, test } from "./server.ts";

// Конструктор приключений на настоящем сервере (финальное покрытие блоков 4 и 5 DND-2). Юнит-тесты
// редактора идут на заглушках по контракту блока; здесь тот же редактор говорит с `dnd-server`
// (dev, `FakeProvider` по `e2e_full.yaml`, временная база) через прокси Vite: черновик, разделы с
// версией, пожелание, задания генерации с опросом и закрепление проходят по настоящему API.
// Ответы шагов генератора — раздел `json` сценария (`generator.concept` … `generator.relations`):
// каждое задание собирает свой провайдер и отвечает сценарием с начала, поэтому повторная
// генерация богов возвращает те же титулы — так видно, что закреплённый бог остался, а ручная
// правка незакреплённого заменена ответом шага. Второй тест — сценарий «Проверок блока 5»: стык
// клиента и сервера по разделам карты, предметов, персонажей и связей, справочнику ссылок и
// перегенерации одной сущности.

const LOGIN = "dev-2";
const STEPS = ["Замысел", "Мир", "Боги", "Фракции", "Сюжет"] as const;
// Экраны блока 5: «Карта» и «Наполнение» правят один раздел locations, шаги генерации у них свои.
const MAP_STEPS = ["Карта", "Наполнение", "Персонажи", "Связи"] as const;
type StepLabel = (typeof STEPS)[number] | (typeof MAP_STEPS)[number];

/** Сущность раздела из ответа сервера: тест сравнивает её целиком и правит отдельные поля. */
type EntityJson = Record<string, unknown>;

/** Локация, как её отдаёт сервер (модель движка целиком). */
interface LocationJson extends EntityJson {
  id: string;
  read_aloud: string;
  exits: EntityJson[];
  features: EntityJson[];
}

/** Тело черновика из ответа сервера: тесту нужны только эти поля. */
interface DraftJson {
  id: string;
  version: number;
  title: string;
  adventure_id: string | null;
  world: unknown;
  plot: unknown;
  locations: LocationJson[];
  start_location: string | null;
  finale_location: string | null;
  npcs: EntityJson[];
  relations: EntityJson[];
  items: EntityJson[];
  locks: string[];
  steps: Record<string, { status: string; updated_at: string | null }>;
  job: unknown;
}

interface JobJson {
  id: string;
  step: string;
  target: string | null;
  status: string;
  error: string | null;
  finished_at: string | null;
}

/** Тело `POST .../generate`: шаг и, у перегенерации одной сущности, цель `<вид>:<id>`. */
interface GenerateJson {
  step: string;
  target?: string;
}

const button = (scope: Page | Locator, name: string) =>
  scope.getByRole("button", { name, exact: true });
const textbox = (scope: Locator, name: string) =>
  scope.getByRole("textbox", { name, exact: true });
const group = (scope: Locator, name: string) =>
  scope.getByRole("group", { name, exact: true });

const stepButton = (page: Page, label: StepLabel) =>
  page
    .getByRole("navigation", { name: "Шаги приключения" })
    .getByRole("button", { name: new RegExp(`^${label}`) });
/** Форма шага: скрытые шаги в дереве доступности не видны, поэтому регион один. */
const stepPanel = (page: Page, label: StepLabel) =>
  page.getByRole("region", { name: label, exact: true });

/** Ответ сервера на запрос конструктора: метод и путь после `/api/drafts`. */
function draftsResponse(
  page: Page,
  method: string,
  tail: RegExp,
): Promise<Response> {
  return page.waitForResponse((response) => {
    const { pathname } = new URL(response.url());
    return (
      response.request().method() === method &&
      pathname.startsWith("/api/drafts") &&
      tail.test(pathname.slice("/api/drafts".length))
    );
  });
}

/**
 * Задание генерации на открытом шаге: нажатие `start` ставит его (202) с телом `body`, пока оно
 * идёт — «Генерирую…» и правка закрыта, после `done` клиент перечитывает черновик и форма открыта.
 * Возвращает поставленное задание и черновик, который клиент получил после `done`.
 */
async function runJob(
  page: Page,
  label: StepLabel,
  start: Locator,
  body: GenerateJson,
): Promise<{ job: JobJson; draft: DraftJson }> {
  const panel = stepPanel(page, label);
  const queued = draftsResponse(page, "POST", /^\/[0-9a-f]+\/generate$/);
  const fresh = draftsResponse(page, "GET", /^\/[0-9a-f]+$/);
  await start.click();
  const response = await queued;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toEqual(body);
  const job = (await response.json()) as JobJson;
  expect(job).toMatchObject({
    step: body.step,
    target: body.target ?? null,
    status: "queued",
  });

  await expect(page.getByText("Генерирую…")).toBeVisible();
  await expect(button(panel, "Сохранить раздел")).toBeDisabled();
  await expect(page.getByText("Генерирую…")).toHaveCount(0);
  await expect(page.getByText(/Не удалось сгенерировать шаг/)).toHaveCount(0);
  await expect(button(panel, "Сохранить раздел")).toBeEnabled();
  const draft = (await (await fresh).json()) as DraftJson;
  return { job, draft };
}

/** «Сгенерировать шаг» на открытом шаге: задание шага целиком, после `done` шаг «готово». */
async function generateStep(
  page: Page,
  label: StepLabel,
  step: string,
): Promise<JobJson> {
  const panel = stepPanel(page, label);
  const { job } = await runJob(
    page,
    label,
    button(panel, "Сгенерировать шаг"),
    { step },
  );
  await expect(stepButton(page, label)).toContainText("готово");
  return job;
}

/** Ответ сервера на «Сохранить раздел» открытого шага: запись раздела `section`. */
async function saveSection(
  page: Page,
  label: StepLabel,
  section: string,
): Promise<{ sent: { version: number; data: unknown }; draft: DraftJson }> {
  const saved = draftsResponse(
    page,
    "PUT",
    new RegExp(`^/[0-9a-f]+/sections/${section}$`),
  );
  await button(stepPanel(page, label), "Сохранить раздел").click();
  const response = await saved;
  expect(response.status()).toBe(200);
  return {
    sent: response.request().postDataJSON() as {
      version: number;
      data: unknown;
    },
    draft: (await response.json()) as DraftJson,
  };
}

test("конструктор: новый черновик, пять шагов генерации, закреплённый бог переживает перегенерацию", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  // Опросы задания по порядку: по ним видно, что клиент спрашивает сервер и перестаёт на done.
  const polls: JobJson[] = [];
  page.on("response", (response) => {
    const { pathname } = new URL(response.url());
    if (/^\/api\/drafts\/[0-9a-f]+\/jobs\//.test(pathname)) {
      // Тело может быть уже недоступно, если страницу закрыли: такой ответ тесту не нужен.
      void response.json().then(
        (job: JobJson) => polls.push(job),
        () => undefined,
      );
    }
  });
  const pollsOf = (jobId: string) => polls.filter((job) => job.id === jobId);

  try {
    await page.goto(`/?dev=${LOGIN}`);
    await expect(page.getByText(`Вы вошли как ${LOGIN}.`)).toBeVisible();

    // Вкладка «Приключения» есть у создателя игр (в dev — у каждого dev-входа).
    const adventures = page.getByRole("tab", {
      name: "Приключения",
      exact: true,
    });
    await adventures.click();
    await expect(adventures).toHaveAttribute("aria-selected", "true");

    // Новый черновик: пустое тело запроса, ответ — черновик контракта блока 4.
    const created = draftsResponse(page, "POST", /^$/);
    await button(page, "Новое приключение").click();
    const createdResponse = await created;
    expect(createdResponse.status()).toBe(201);
    expect(createdResponse.request().postDataJSON()).toEqual({});
    const draft = (await createdResponse.json()) as DraftJson;
    expect(draft).toMatchObject({
      version: 1,
      adventure_id: null,
      world: null,
      plot: null,
      locks: [],
      job: null,
    });
    expect(Object.keys(draft.steps)).toHaveLength(12);
    for (const label of STEPS) {
      await expect(stepButton(page, label)).toContainText("пусто");
    }

    // Замысел вручную: раздел settings уходит с версией, ответ поднимает её.
    const concept = stepPanel(page, "Замысел");
    await textbox(concept, "Название").fill("Маяк");
    await textbox(concept, "Завязка").fill("Маяк погас, смотритель пропал.");
    await textbox(concept, "Тон").fill("мрачный");
    await textbox(concept, "Игроков").fill("4");
    await textbox(concept, "Главное для дневника").fill(
      "Зажечь маяк и найти смотрителя.",
    );
    // Пожелание набрано до «Сохранить раздел»: сохранение раздела его не стирает.
    await textbox(concept, "Что хочу увидеть").fill("Морская готика");
    const settingsSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/sections\/settings$/,
    );
    await button(concept, "Сохранить раздел").click();
    const settingsResponse = await settingsSaved;
    expect(settingsResponse.status()).toBe(200);
    expect(settingsResponse.request().postDataJSON()).toEqual({
      version: 1,
      data: {
        title: "Маяк",
        journal_intro: "Зажечь маяк и найти смотрителя.",
        pitch: "Маяк погас, смотритель пропал.",
        tone: "мрачный",
        target_hours: 3,
        party_size: 4,
        party_level: 3,
        modules: [],
        profanity: true,
      },
    });
    expect(await settingsResponse.json()).toMatchObject({
      version: 2,
      title: "Маяк",
      settings: { party_size: 4 },
      steps: { concept: { status: "draft" }, world: { status: "empty" } },
    });
    // Статус — по содержимому раздела: ручной Замысел без id приключения — «черновик».
    await expect(stepButton(page, "Замысел")).toContainText("черновик");
    await expect(stepButton(page, "Мир")).toContainText("пусто");

    // Генерация Замысла с пожеланием: сначала пожелание с версией, потом задание без лишних полей.
    await expect(textbox(concept, "Что хочу увидеть")).toHaveValue(
      "Морская готика",
    );
    const wishSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/wishes\/concept$/,
    );
    const conceptJob = await generateStep(page, "Замысел", "concept");
    const wishResponse = await wishSaved;
    expect(wishResponse.request().postDataJSON()).toEqual({
      version: 2,
      text: "Морская готика",
    });
    expect(wishResponse.status()).toBe(200);
    // Ответ шага заменил незакреплённые тексты замысла; число игроков шаг не трогает.
    await expect(textbox(concept, "Название")).toHaveValue(
      "Маяк на Вороньем мысу",
    );
    await expect(concept).toContainText("id приключения: crow-lighthouse");
    await expect(textbox(concept, "Игроков")).toHaveValue("4");
    await expect(textbox(concept, "Что хочу увидеть")).toHaveValue(
      "Морская готика",
    );
    // Опрос задания кончился на done и больше не идёт.
    await expect
      .poll(() => pollsOf(conceptJob.id).at(-1))
      .toMatchObject({ status: "done", error: null });
    const pollsAtDone = pollsOf(conceptJob.id).length;
    await page.waitForTimeout(2_500);
    expect(pollsOf(conceptJob.id)).toHaveLength(pollsAtDone);

    // Мир: история, край и слухи из ответа шага.
    await stepButton(page, "Мир").click();
    await generateStep(page, "Мир", "world");
    const world = stepPanel(page, "Мир");
    await expect(textbox(world, "История мира")).toHaveValue(/^Вороний мыс/);
    await expect(group(world, "Вороний мыс")).toBeVisible();
    await expect(group(world, "Слух 2")).toBeVisible();

    // Боги: два бога из ответа шага, у каждого — «Закрепить».
    await stepButton(page, "Боги").click();
    await generateStep(page, "Боги", "pantheon");
    const gods = stepPanel(page, "Боги");
    const selmaraTitle = textbox(group(gods, "Сельмара"), "Титул");
    const ashenTitle = textbox(group(gods, "Пепельный"), "Титул");
    await expect(selmaraTitle).toHaveValue("Хозяйка приливов");
    await expect(ashenTitle).toHaveValue("Тот, кто ест огни");

    // Фракции и Сюжет.
    await stepButton(page, "Фракции").click();
    await generateStep(page, "Фракции", "factions");
    const factions = stepPanel(page, "Фракции");
    await expect(group(factions, "Стража гавани")).toBeVisible();
    await expect(group(factions, "Пепельный круг")).toBeVisible();

    await stepButton(page, "Сюжет").click();
    await generateStep(page, "Сюжет", "plot");
    const plot = stepPanel(page, "Сюжет");
    await expect(group(plot, "Тёмный маяк")).toBeVisible();
    await expect(textbox(plot, "Злодей")).toHaveValue("deity:ashen-one");

    // Ручные правки обоих богов, затем закрепление только Сельмары. Пока правки не сохранены,
    // «Закрепить» закрыт, а у генерации — предупреждение: закрепилась бы версия сервера.
    await stepButton(page, "Боги").click();
    await selmaraTitle.fill("Хранительница маяка");
    await ashenTitle.fill("Ручная правка без замка");
    await expect(button(gods, "Закрепить: Сельмара")).toBeDisabled();
    await expect(gods).toContainText(
      "Сначала сохраните раздел: закрепить можно только сохранённое.",
    );
    await expect(gods).toContainText(
      "В шаге есть несохранённые правки: генерация их заменит.",
    );
    const godsSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/sections\/deities$/,
    );
    await button(gods, "Сохранить раздел").click();
    const godsResponse = await godsSaved;
    expect(godsResponse.status()).toBe(200);
    const afterGods = (await godsResponse.json()) as DraftJson;

    const lock = button(gods, "Закрепить: Сельмара");
    await expect(lock).toBeEnabled();
    await expect(lock).toHaveAttribute("aria-pressed", "false");
    const locked = draftsResponse(page, "PUT", /^\/[0-9a-f]+\/locks$/);
    await lock.click();
    const lockResponse = await locked;
    expect(lockResponse.status()).toBe(200);
    expect(lockResponse.request().postDataJSON()).toEqual({
      version: afterGods.version,
      locks: ["deity:selmara"],
    });
    expect(await lockResponse.json()).toMatchObject({
      version: afterGods.version + 1,
      locks: ["deity:selmara"],
    });
    await expect(lock).toHaveAttribute("aria-pressed", "true");

    // Перегенерация богов: закреплённая Сельмара осталась с ручной правкой, Пепельный — из ответа.
    await generateStep(page, "Боги", "pantheon");
    await expect(selmaraTitle).toHaveValue("Хранительница маяка");
    await expect(ashenTitle).toHaveValue("Тот, кто ест огни");
    await expect(lock).toHaveAttribute("aria-pressed", "true");

    for (const label of STEPS) {
      await expect(stepButton(page, label)).toContainText("готово");
    }

    // Черновик сохранён на сервере: в списке название и id из замысла, при открытии — замок.
    await button(page, "К списку приключений").click();
    const reopen = button(page, "Открыть: Маяк на Вороньем мысу");
    await expect(reopen).toBeVisible();
    await expect(page.getByText("crow-lighthouse")).toBeVisible();
    await reopen.click();
    await stepButton(page, "Боги").click();
    await expect(button(gods, "Закрепить: Сельмара")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(selmaraTitle).toHaveValue("Хранительница маяка");
  } finally {
    await context.close();
  }
});

// --- блок 5: карта, наполнение, персонажи, связи ---

const MAP_LOGIN = "dev-3";
// Тексты ответов шагов из `e2e_full.yaml`, которые тест видит в форме.
const FIRST_CONTENTS = /^Ветер гонит по камням серый пепел/;
const LAMP_ROOM_CONTENTS =
  "Фонарная залита серым светом. Линзы закопчены, а в чаше вместо огня тлеет пепел.";
const HARBOR_TEXT = "Лодки скрипят у причала, пахнет рыбой и дёгтем.";

const combobox = (scope: Locator, name: string) =>
  scope.getByRole("combobox", { name, exact: true });
const toggle = (scope: Locator, name: string) =>
  scope.getByRole("switch", { name, exact: true });

test("конструктор II: карта из трёх локаций, предмет в тайнике, связи двух NPC и бога, перегенерация одной локации", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();

  try {
    await page.goto(`/?dev=${MAP_LOGIN}`);
    await expect(page.getByText(`Вы вошли как ${MAP_LOGIN}.`)).toBeVisible();
    await page.getByRole("tab", { name: "Приключения", exact: true }).click();
    const created = draftsResponse(page, "POST", /^$/);
    await button(page, "Новое приключение").click();
    expect((await created).status()).toBe(201);
    for (const label of MAP_STEPS) {
      await expect(stepButton(page, label)).toContainText("пусто");
    }

    // Боги нужны концу связи: Сельмара из ответа шага pantheon.
    await stepButton(page, "Боги").click();
    await generateStep(page, "Боги", "pantheon");

    // Карта: шаг locations даёт гавань и тропу с выходами, старт и финал.
    await stepButton(page, "Карта").click();
    const map = stepPanel(page, "Карта");
    const { draft: generated } = await runJob(
      page,
      "Карта",
      button(map, "Сгенерировать шаг"),
      { step: "locations" },
    );
    expect(generated.locations.map(({ id }) => id)).toEqual([
      "harbor",
      "cliff-path",
    ]);
    const [harbor, cliffPath] = generated.locations;
    await expect(
      group(group(map, "Тропа над обрывом"), "вниз → Гавань Вороньего мыса"),
    ).toBeVisible();
    await expect(combobox(map, "Старт")).toHaveValue("harbor");
    await expect(combobox(map, "Финал")).toHaveValue("cliff-path");

    // Третья локация — вручную: подпись группы меняется с названием, поэтому оно вводится последним.
    await button(map, "Добавить локацию").click();
    const fresh = group(map, "Локация 3");
    await textbox(fresh, "Идентификатор").fill("lamp-room");
    await button(fresh, "Добавить выход").click();
    await textbox(group(fresh, "Выход 1"), "Идентификатор").fill("down");
    await textbox(group(fresh, "Выход 1"), "Описание").fill(
      "Лестница к тропе.",
    );
    await combobox(group(fresh, "Выход 1"), "Куда").selectOption("cliff-path");
    await textbox(
      group(fresh, "выход → Тропа над обрывом"),
      "Направление",
    ).fill("вниз");
    await textbox(fresh, "Название").fill("Фонарная");
    // Выход с тропы в фонарную: заперт ключом и скрыт (Сл из таблицы).
    const path = group(map, "Тропа над обрывом");
    await button(path, "Добавить выход").click();
    const upstairs = group(path, "Выход 2");
    await textbox(upstairs, "Идентификатор").fill("to-lamp-room");
    await textbox(upstairs, "Описание").fill("Винтовая лестница за скалой.");
    await textbox(upstairs, "Замок").fill("item:lamp-key");
    await combobox(upstairs, "Скрытность").selectOption("15");
    await combobox(upstairs, "Куда").selectOption("lamp-room");
    await textbox(group(path, "выход → Фонарная"), "Направление").fill(
      "вверх",
    );
    await combobox(map, "Финал").selectOption("lamp-room");

    // Раздел карты — объект; сгенерированное уходит назад как есть, новое — с полями по умолчанию
    // сервера, и сервер возвращает ровно то, что принял.
    const mapSaved = await saveSection(page, "Карта", "locations");
    const lampRoom: LocationJson = {
      public: "",
      known_at_start: false,
      id: "lamp-room",
      name: "Фонарная",
      region: "",
      read_aloud: "",
      dm_notes: "",
      exits: [
        {
          id: "down",
          to: "cliff-path",
          direction: "вниз",
          description: "Лестница к тропе.",
          locked_by: null,
          hidden_dc: null,
        },
      ],
      features: [],
      secrets: [],
      encounter_id: null,
      zones: [{ id: "here", name: "здесь" }],
      links: [],
    };
    expect(mapSaved.sent).toEqual({
      version: generated.version,
      data: {
        locations: [
          harbor,
          {
            ...cliffPath,
            exits: [
              ...cliffPath.exits,
              {
                id: "to-lamp-room",
                to: "lamp-room",
                direction: "вверх",
                description: "Винтовая лестница за скалой.",
                locked_by: "item:lamp-key",
                hidden_dc: 15,
              },
            ],
          },
          lampRoom,
        ],
        start_location: "harbor",
        finale_location: "lamp-room",
      },
    });
    expect(mapSaved.draft).toMatchObject({
      version: generated.version + 1,
      start_location: "harbor",
      finale_location: "lamp-room",
    });
    expect(mapSaved.draft.locations).toEqual(
      (mapSaved.sent.data as { locations: LocationJson[] }).locations,
    );
    await expect(stepButton(page, "Карта")).toContainText("готово");
    await expect(stepButton(page, "Наполнение")).toContainText("пусто");

    // Наполнение всех трёх локаций шагом location_contents: по ответу на локацию в порядке карты.
    await stepButton(page, "Наполнение").click();
    const contents = stepPanel(page, "Наполнение");
    const { draft: filled } = await runJob(
      page,
      "Наполнение",
      button(contents, "Сгенерировать шаг"),
      { step: "location_contents" },
    );
    expect(filled.locations[2].read_aloud).toBe(LAMP_ROOM_CONTENTS);
    await expect(combobox(contents, "Локация")).toHaveValue("harbor");
    await expect(textbox(contents, "Текст вслух")).toHaveValue(FIRST_CONTENTS);
    // Проверку детали из ответа сервера форма показывает как есть: навык и Сл из таблицы.
    await combobox(contents, "Локация").selectOption("cliff-path");
    const marks = group(group(contents, "Детали и тайники"), "Пепельные следы");
    await expect(combobox(marks, "Характеристика")).toHaveValue("wis");
    await expect(combobox(marks, "Навык")).toHaveValue("survival");
    await expect(combobox(marks, "Сл")).toHaveValue("10");

    // Гавань: свой текст, ключ в «Предметах» и сундук, в тайнике которого этот ключ.
    await combobox(contents, "Локация").selectOption("harbor");
    await textbox(contents, "Текст вслух").fill(HARBOR_TEXT);
    const items = group(contents, "Предметы");
    await button(items, "Добавить предмет").click();
    await textbox(group(items, "Предмет 1"), "Идентификатор").fill("lamp-key");
    await textbox(group(items, "Предмет 1"), "Описание").fill(
      "Тяжёлый бронзовый ключ с якорем.",
    );
    await textbox(group(items, "Предмет 1"), "Название").fill(
      "Ключ от фонарной",
    );
    const places = group(contents, "Детали и тайники");
    await button(places, "Добавить деталь").click();
    const chest = group(places, "Деталь 1");
    await textbox(chest, "Идентификатор").fill("harbor-chest");
    await textbox(chest, "Описание").fill("Окованный сундук под навесом.");
    await group(chest, "Предметы в тайнике")
      .getByRole("checkbox", { name: "Ключ от фонарной", exact: true })
      .check();
    await textbox(chest, "Название").fill("Сундук смотрителя");
    await expect(group(items, "Ключ от фонарной")).toContainText(
      "В тайнике «Сундук смотрителя»",
    );

    // Предмет в тайнике — две записи по очереди: items с держателем, затем locations с деталью
    // `reveals: item`; вторая идёт с версией из ответа первой.
    const itemsSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/sections\/items$/,
    );
    const placesSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/sections\/locations$/,
    );
    await button(contents, "Сохранить раздел").click();
    const itemsResponse = await itemsSaved;
    expect(itemsResponse.status()).toBe(200);
    expect(itemsResponse.request().postDataJSON()).toEqual({
      version: filled.version,
      data: [
        {
          public: "",
          known_at_start: false,
          id: "lamp-key",
          name: "Ключ от фонарной",
          description: "Тяжёлый бронзовый ключ с якорем.",
          base_item: null,
          holder: "location:harbor",
        },
      ],
    });
    const afterItems = (await itemsResponse.json()) as DraftJson;
    // Сервер возвращает предмет ровно таким, каким его прислал клиент (Item движка целиком).
    expect(afterItems.items).toEqual(
      (itemsResponse.request().postDataJSON() as { data: EntityJson[] }).data,
    );
    const placesResponse = await placesSaved;
    expect(placesResponse.status()).toBe(200);
    const chestJson = {
      id: "harbor-chest",
      name: "Сундук смотрителя",
      description: "Окованный сундук под навесом.",
      check: null,
      reveals: [{ kind: "item", id: "lamp-key", value: null }],
      hidden: false,
      locked_by: null,
    };
    expect(placesResponse.request().postDataJSON()).toEqual({
      version: afterItems.version,
      data: {
        locations: [
          {
            ...filled.locations[0],
            read_aloud: HARBOR_TEXT,
            features: [chestJson],
          },
          filled.locations[1],
          filled.locations[2],
        ],
        start_location: "harbor",
        finale_location: "lamp-room",
      },
    });
    const stocked = (await placesResponse.json()) as DraftJson;
    expect(stocked.version).toBe(afterItems.version + 1);
    expect(stocked.locations[0].features).toEqual([chestJson]);
    expect(stocked.items).toEqual(afterItems.items);
    await expect(stepButton(page, "Наполнение")).toContainText("готово");

    // Гавань закреплена: её перегенерировать нельзя.
    const harborLock = button(contents, "Закрепить: Гавань Вороньего мыса");
    const locked = draftsResponse(page, "PUT", /^\/[0-9a-f]+\/locks$/);
    await harborLock.click();
    const lockResponse = await locked;
    expect(lockResponse.request().postDataJSON()).toEqual({
      version: stocked.version,
      locks: ["location:harbor"],
    });
    await expect(harborLock).toHaveAttribute("aria-pressed", "true");
    await expect(
      button(contents, "Перегенерировать: Гавань Вороньего мыса"),
    ).toBeDisabled();
    const beforeRegeneration = (await lockResponse.json()) as DraftJson;

    // Перегенерация одной тропы с пожеланием: сначала пожелание шага, затем задание с целью.
    await combobox(contents, "Локация").selectOption("cliff-path");
    await textbox(contents, "Что хочу увидеть").fill("Больше пепла");
    const wishSaved = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/wishes\/location_contents$/,
    );
    const { draft: regenerated } = await runJob(
      page,
      "Наполнение",
      button(contents, "Перегенерировать: Тропа над обрывом"),
      { step: "location_contents", target: "location:cliff-path" },
    );
    expect((await wishSaved).request().postDataJSON()).toEqual({
      version: beforeRegeneration.version,
      text: "Больше пепла",
    });
    // Поменялась только тропа: закреплённая гавань с тайником и фонарная — как были.
    const [harborAfter, pathAfter, lampRoomAfter] = regenerated.locations;
    expect(pathAfter.read_aloud).toMatch(FIRST_CONTENTS);
    expect(pathAfter.features).toEqual([]);
    expect(pathAfter.exits).toEqual(beforeRegeneration.locations[1].exits);
    expect(harborAfter).toEqual(beforeRegeneration.locations[0]);
    expect(lampRoomAfter).toEqual(beforeRegeneration.locations[2]);
    expect(regenerated.items).toEqual(beforeRegeneration.items);
    expect(regenerated.locks).toEqual(["location:harbor"]);
    await expect(textbox(contents, "Текст вслух")).toHaveValue(FIRST_CONTENTS);
    await combobox(contents, "Локация").selectOption("harbor");
    await expect(textbox(contents, "Текст вслух")).toHaveValue(HARBOR_TEXT);
    await expect(
      group(
        group(places, "Сундук смотрителя"),
        "Предметы в тайнике",
      ).getByRole("checkbox", { name: "Ключ от фонарной", exact: true }),
    ).toBeChecked();

    // Персонажи из ответа шага: NPC движка целиком, связи формата v1 пусты; «Где находится» —
    // локация из справочника сервера, по имени.
    await stepButton(page, "Персонажи").click();
    const people = stepPanel(page, "Персонажи");
    const { draft: peopled } = await runJob(
      page,
      "Персонажи",
      button(people, "Сгенерировать шаг"),
      { step: "npcs" },
    );
    expect(peopled.npcs[1]).toEqual({
      public: "",
      known_at_start: false,
      id: "bram",
      name: "Брам",
      role: "контрабандист",
      appearance: "Жилистый моряк с серьгой и сломанным носом.",
      manner: "Шутит, когда врёт.",
      motive: "Расплатиться с долгами и вернуть себе лодку.",
      secret: "",
      knows: [],
      attitude: 0,
      relations: [],
      location_id: "cliff-path",
      statblock: null,
    });
    await expect(stepButton(page, "Персонажи")).toContainText("готово");
    const ilsaPlace = combobox(group(people, "Ильза"), "Где находится");
    await expect(ilsaPlace).toHaveValue("location:harbor");
    await expect(ilsaPlace.locator("option:checked")).toHaveText(
      "Гавань Вороньего мыса",
    );
    await expect(combobox(group(people, "Брам"), "Где находится")).toHaveValue(
      "location:cliff-path",
    );

    // Связи вручную через RefPicker: два NPC и NPC с богом. Подпись — «кто — кем — кому».
    await stepButton(page, "Связи").click();
    const graph = stepPanel(page, "Связи");
    await button(graph, "Добавить связь").click();
    await textbox(group(graph, "Связь 1"), "Идентификатор").fill(
      "bram-rival-ilsa",
    );
    await combobox(group(graph, "Связь 1"), "Кем").selectOption("rival");
    await combobox(group(graph, "Связь 1"), "Кто").selectOption("npc:bram");
    await combobox(group(graph, "Брам — соперник — …"), "Кому").selectOption(
      "npc:ilsa",
    );
    await button(graph, "Добавить связь").click();
    const faith = group(graph, "Связь 2");
    await textbox(faith, "Идентификатор").fill("ilsa-worships-selmara");
    await combobox(faith, "Кем").selectOption("worships");
    await toggle(faith, "Тайная связь").click();
    await toggle(faith, "Взаимная").click();
    await combobox(faith, "Кто").selectOption("npc:ilsa");
    await combobox(
      group(graph, "Ильза — поклоняется — …"),
      "Кому",
    ).selectOption("deity:selmara");
    await expect(group(graph, "Брам — соперник — Ильза")).toBeVisible();
    await expect(group(graph, "Ильза — поклоняется — Сельмара")).toBeVisible();

    // После записи справочник ссылок перечитывается: виды по порядку, внутри вида — порядок
    // раздела, только ссылка, вид и имя (дополнение B5, §6).
    const refsLoaded = draftsResponse(page, "GET", /^\/[0-9a-f]+\/refs$/);
    const graphSaved = await saveSection(page, "Связи", "relations");
    expect(await (await refsLoaded).json()).toEqual([
      { ref: "deity:selmara", kind: "deity", name: "Сельмара" },
      { ref: "deity:ashen-one", kind: "deity", name: "Пепельный" },
      { ref: "npc:ilsa", kind: "npc", name: "Ильза" },
      { ref: "npc:bram", kind: "npc", name: "Брам" },
      {
        ref: "location:harbor",
        kind: "location",
        name: "Гавань Вороньего мыса",
      },
      {
        ref: "location:cliff-path",
        kind: "location",
        name: "Тропа над обрывом",
      },
      { ref: "location:lamp-room", kind: "location", name: "Фонарная" },
      { ref: "item:lamp-key", kind: "item", name: "Ключ от фонарной" },
    ]);
    const worship = {
      id: "ilsa-worships-selmara",
      a: "npc:ilsa",
      b: "deity:selmara",
      kind: "worships",
      note: "",
      secret: true,
      mutual: false,
    };
    expect(graphSaved.sent.data).toEqual([
      {
        id: "bram-rival-ilsa",
        a: "npc:bram",
        b: "npc:ilsa",
        kind: "rival",
        note: "",
        secret: false,
        mutual: true,
      },
      worship,
    ]);
    expect(graphSaved.draft.relations).toEqual(graphSaved.sent.data);
    await expect(stepButton(page, "Связи")).toContainText("готово");

    // Фильтр по богу оставляет только его связь.
    await combobox(graph, "Показать связи").selectOption("deity:selmara");
    await expect(group(graph, "Ильза — поклоняется — Сельмара")).toBeVisible();
    await expect(group(graph, "Брам — соперник — Ильза")).toBeHidden();
    await combobox(graph, "Показать связи").selectOption("");

    // Перегенерация одной связи: пояснение из ответа шага, тайная связь с богом не тронута.
    const { draft: rewoven } = await runJob(
      page,
      "Связи",
      button(graph, "Перегенерировать: Брам — соперник — Ильза"),
      { step: "relations", target: "relation:bram-rival-ilsa" },
    );
    expect(rewoven.relations).toEqual([
      {
        id: "bram-rival-ilsa",
        a: "npc:bram",
        b: "npc:ilsa",
        kind: "rival",
        note: "Ильза грозит отобрать у него причал.",
        secret: false,
        mutual: true,
      },
      worship,
    ]);
    await expect(
      textbox(group(graph, "Брам — соперник — Ильза"), "Пояснение"),
    ).toHaveValue("Ильза грозит отобрать у него причал.");

    for (const label of MAP_STEPS) {
      await expect(stepButton(page, label)).toContainText("готово");
    }
  } finally {
    await context.close();
  }
});
