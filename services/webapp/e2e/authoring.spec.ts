import fs from "node:fs";
import type {
  APIRequestContext,
  Locator,
  Page,
  Response,
} from "@playwright/test";
import { pickQuickHero } from "./hero.ts";
import { expectFitsPhone } from "./layout.ts";
import { expect, test } from "./server.ts";

// Конструктор приключений на настоящем сервере (финальное покрытие блоков 4 и 5 DND-2 и сквозной
// тест B6.3). Юнит-тесты редактора идут на заглушках по контракту блока; здесь тот же редактор
// говорит с `dnd-server` (dev, `FakeProvider` по `e2e_full.yaml`, временная база) через прокси
// Vite: черновик, разделы с версией, пожелание, задания генерации с опросом и закрепление проходят
// по настоящему API. Ответы шагов генератора — раздел `json` сценария (`generator.concept` …
// `generator.clues`): каждое задание собирает свой провайдер и отвечает сценарием с начала,
// поэтому повторная генерация богов возвращает те же титулы — так видно, что закреплённый бог
// остался, а ручная правка незакреплённого заменена ответом шага. Второй тест — сценарий «Проверок
// блока 5»: стык клиента и сервера по разделам карты, предметов, персонажей и связей, справочнику
// ссылок и перегенерации одной сущности. Третий — весь путь автора (B6.3): двенадцать шагов
// генерации, «Проверка» без ошибок, «Опубликовать», игра на опубликованном приключении без
// перезапуска сервера и раунд мастера в ней. Последние четыре — финальное покрытие блока 6 на
// копии встроенного приключения: ссылка ошибки на шаг и закрытая публикация, занятый id, 201/200 и
// запрет смены id, архив туда и обратно с отказами импорта, публикация пустого черновика и пределы
// боя. На ключевых шагах редактор измеряется на ширине телефона (B10.4, `expectFitsPhone`): все
// шаги видны плашками, бейджи не обрезаны, горизонтальной прокрутки нет.

const LOGIN = "dev-2";
const STEPS = ["Замысел", "Мир", "Боги", "Фракции", "Сюжет"] as const;
// Экраны блока 5: «Карта» и «Наполнение» правят один раздел locations, шаги генерации у них свои.
const MAP_STEPS = ["Карта", "Наполнение", "Персонажи", "Связи"] as const;
// Экраны блока 6 со статусом шага; «Проверка» и «Публикация» идут после них, без статуса.
const FIGHT_STEPS = ["Бои и боссы", "Предметы и награды", "Улики"] as const;
type StepLabel =
  | (typeof STEPS)[number]
  | (typeof MAP_STEPS)[number]
  | (typeof FIGHT_STEPS)[number];
type ScreenLabel = StepLabel | "Проверка" | "Публикация";

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
  settings: EntityJson;
  world: unknown;
  plot: unknown;
  locations: LocationJson[];
  start_location: string | null;
  finale_location: string | null;
  npcs: EntityJson[];
  relations: EntityJson[];
  items: EntityJson[];
  encounters: EntityJson[];
  monsters: EntityJson[];
  revelations: EntityJson[];
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

const stepButton = (page: Page, label: ScreenLabel) =>
  page
    .getByRole("navigation", { name: "Шаги приключения" })
    .getByRole("button", { name: new RegExp(`^${label}`) });
/** Подписи экранов редактора по id шага: так отказ публикации называет шаг на экране. */
const STEP_LABELS: Record<string, string> = {
  concept: "Замысел",
  world: "Мир",
  pantheon: "Боги",
  factions: "Фракции",
  plot: "Сюжет",
  locations: "Карта",
  location_contents: "Наполнение",
  npcs: "Персонажи",
  relations: "Связи",
  encounters: "Бои и боссы",
  items: "Предметы и награды",
  clues: "Улики",
};

const escapeRegExp = (text: string) =>
  text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Форма шага: скрытые шаги в дереве доступности не видны, поэтому регион один. */
const stepPanel = (page: Page, label: ScreenLabel) =>
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
  // Упавшее задание черновик не перечитывает: тест падает на проверке ниже и закрывает окно, а
  // ожидание ответа отклоняется позже. Причиной должна остаться та проверка, а не это ожидание.
  fresh.catch(() => undefined);
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
    // id приключения в Замысле пуст — клиент шлёт `null`, сервер его не задаёт (дополнение B6, §2).
    expect(settingsResponse.request().postDataJSON()).toEqual({
      version: 1,
      data: {
        title: "Маяк",
        journal_intro: "Зажечь маяк и найти смотрителя.",
        adventure_id: null,
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
    await expect(textbox(concept, "id приключения")).toHaveValue(
      "crow-lighthouse",
    );
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

    // B10.4 (manual B4 nit): на 390 и 360 px все шаги видны плашками, бейдж «закреплён» шага,
    // закреплённого целиком, не обрезан, горизонтальной прокрутки нет.
    await stepButton(page, "Сюжет").click();
    const plotLocked = draftsResponse(page, "PUT", /^\/[0-9a-f]+\/locks$/);
    await button(plot, "Закрепить раздел").click();
    expect((await plotLocked).status()).toBe(200);
    await expect(stepButton(page, "Сюжет")).toContainText("закреплён");
    await expectFitsPhone(page, "редактор: полоска шагов с бейджами");

    // Черновик сохранён на сервере: в списке название и id из замысла, при открытии — замок.
    await button(page, "К списку приключений").click();
    const reopen = button(page, "Открыть: Маяк на Вороньем мысу");
    await expect(reopen).toBeVisible();
    await expect(page.getByText("crow-lighthouse")).toBeVisible();
    // Строка черновика — с временем правки: копии одного приключения различимы.
    await expect(reopen).toHaveAccessibleDescription(
      /изменён \d+ [а-я]+ \d{4}, \d\d:\d\d/,
    );
    await expectFitsPhone(page, "список приключений");
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
    await textbox(group(path, "выход → Фонарная"), "Направление").fill("вверх");
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
    // Карта с выходами, замками и выбором «Куда» — на ширине телефона (финальное покрытие B10).
    await expectFitsPhone(page, "редактор: «Карта» с выходами");

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
    await expectFitsPhone(page, "редактор: «Наполнение»");
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
      group(group(places, "Сундук смотрителя"), "Предметы в тайнике").getByRole(
        "checkbox",
        { name: "Ключ от фонарной", exact: true },
      ),
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
    // Связи с длинными подписями «кто — кем — кому» и фильтром (финальное покрытие B10).
    await expectFitsPhone(page, "редактор: «Связи» с фильтром");
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

// --- блок 6: бои, предметы, улики, проверка, публикация и игра на опубликованном ---

const AUTHOR = "dev-1";
const ADVENTURE_ID = "crow-lighthouse";
const ADVENTURE_TITLE = "Маяк на Вороньем мысу";
// Шаги после Замысла по порядку полоски: экран и шаг его задания.
const LATER_STEPS = [
  ["Мир", "world"],
  ["Боги", "pantheon"],
  ["Фракции", "factions"],
  ["Сюжет", "plot"],
  ["Карта", "locations"],
  ["Наполнение", "location_contents"],
  ["Персонажи", "npcs"],
  ["Связи", "relations"],
] as const satisfies readonly (readonly [StepLabel, string])[];

// Отряд раунда. Ответ вплетения в сценарии называет шестерых героев ($pc1…$pc6, по порядку входа
// в игру), поэтому играют все шесть dev-входов. Хост — автор приключения, он идёт через экраны;
// друзья входят, создают героев и пасуют через API клиента: их экраны лобби, создания героя и хода
// проверяет `six-players.spec.ts`. Борис входит первым из друзей: он герой 2, и первый раунд
// сценария бросает его Акробатику.
const HOST_HERO = {
  name: "Алиса",
  classId: "fighter",
  species: "human",
  concept: "бывшая стражница, которая не умеет врать",
};
// Герой друга — тело быстрого создания героя (`POST .../characters`, режим quick) без режима.
const FRIENDS = [
  ["dev-2", { name: "Борис", class_id: "rogue", species: "halfling" }],
  ["dev-3", { name: "Вера", class_id: "ranger", species: "elf" }],
  ["dev-4", { name: "Глеб", class_id: "cleric", species: "dwarf" }],
  ["dev-5", { name: "Дина", class_id: "wizard", species: "gnome" }],
  ["dev-6", { name: "Егор", class_id: "barbarian", species: "orc" }],
] as const;
const FRIEND_CONCEPT = "старый друг хоста из рыбацкой деревни";
// Цель героя 1 из ответа вплетения и текст первого раунда мастера в `e2e_full.yaml`.
const HOST_GOAL = "найти пропавшего смотрителя Ульриха";
const OPENING = /Вечер в Мшистом Броде/;
const FIRST_ROUND = /лихо перепрыгивает лужу/;

/** Игрок без окна браузера: заголовок с его токеном dev-входа. */
async function authHeaders(
  request: APIRequestContext,
  login: string,
): Promise<Record<string, string>> {
  const response = await request.post("/api/auth/dev", { data: { login } });
  expect(response.ok(), `вход ${login}`).toBe(true);
  const { token } = (await response.json()) as { token: string };
  return { Authorization: `Bearer ${token}` };
}

/** POST к API игры от имени игрока; ответ сервера обязан быть успешным. */
async function postAs(
  request: APIRequestContext,
  headers: Record<string, string>,
  url: string,
  data?: unknown,
): Promise<void> {
  const response = await request.post(url, { headers, data });
  expect(response.ok(), `POST ${url}: ${await response.text()}`).toBe(true);
}

test("конструктор III: двенадцать шагов на заглушке, проверка без ошибок, публикация и раунд игры на опубликованном приключении без перезапуска сервера", async ({
  browser,
  baseURL,
  request,
  server,
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
    await test.step("1. Замысел: генерация даёт id приключения, правка длины игры уходит с ним", async () => {
      await page.goto(`/?dev=${AUTHOR}`);
      await expect(page.getByText(`Вы вошли как ${AUTHOR}.`)).toBeVisible();
      await page.getByRole("tab", { name: "Приключения", exact: true }).click();
      const created = draftsResponse(page, "POST", /^$/);
      await button(page, "Новое приключение").click();
      expect((await created).status()).toBe(201);

      const concept = stepPanel(page, "Замысел");
      await generateStep(page, "Замысел", "concept");
      await expect(textbox(concept, "id приключения")).toHaveValue(
        ADVENTURE_ID,
      );
      // Длина игры под оценку сервера (две локации, бой, открытие, двое NPC — около 1 ч 15 мин):
      // так «Проверка» не предупреждает о времени. Id из генерации клиент шлёт назад как есть.
      await textbox(concept, "Длина игры, часов").fill("1,5");
      const { sent, draft } = await saveSection(page, "Замысел", "settings");
      expect(sent.data).toMatchObject({
        title: ADVENTURE_TITLE,
        adventure_id: ADVENTURE_ID,
        target_hours: 1.5,
        party_size: 6,
        party_level: 3,
      });
      expect(draft).toMatchObject({
        adventure_id: ADVENTURE_ID,
        settings: { target_hours: 1.5 },
      });
      await expect(stepButton(page, "Замысел")).toContainText("готово");
    });

    await test.step("2. Мир, боги, фракции, сюжет, карта, наполнение, персонажи и связи", async () => {
      for (const [label, step] of LATER_STEPS) {
        await stepButton(page, label).click();
        await generateStep(page, label, step);
      }
    });

    await test.step("3. Бои и боссы: бой с боссом на тропе и свой статблок с опытом по cr", async () => {
      await stepButton(page, "Бои и боссы").click();
      const fights = stepPanel(page, "Бои и боссы");
      const { draft } = await runJob(
        page,
        "Бои и боссы",
        button(fights, "Сгенерировать шаг"),
        { step: "encounters" },
      );
      expect(draft.encounters).toEqual([
        expect.objectContaining({
          id: "ash-rite",
          location_id: "cliff-path",
          boss: true,
          pc_zone: "here",
          monsters: [
            { monster_id: "ash-priest", count: 1, zone: "here" },
            { monster_id: "cultist_fanatic", count: 2, zone: "here" },
            { monster_id: "ghoul", count: 5, zone: "here" },
          ],
        }),
      ]);
      // Опыт своего монстра — опыт монстра SRD того же cr (дополнение B6, §4): иначе публикация
      // получила бы ошибку `custom_monster_xp`.
      expect(draft.monsters).toEqual([
        expect.objectContaining({ id: "ash-priest", cr: 3, xp: 700 }),
      ]);
      await expect(stepButton(page, "Бои и боссы")).toContainText("готово");
      await expect(toggle(group(fights, "Бой ash-rite"), "Босс")).toBeChecked();
      const priest = group(fights, "Пепельный жрец");
      await expect(combobox(priest, "Опасность (cr)")).toHaveValue("3");
      await expect(textbox(priest, "Опыт (xp)")).toHaveValue("700");
      await expectFitsPhone(page, "редактор: «Бои и боссы»");
    });

    await test.step("4. Предметы и награды: оба предмета у персонажей, а не в локациях", async () => {
      await stepButton(page, "Предметы и награды").click();
      const loot = stepPanel(page, "Предметы и награды");
      const { draft } = await runJob(
        page,
        "Предметы и награды",
        button(loot, "Сгенерировать шаг"),
        { step: "items" },
      );
      expect(
        draft.items.map(({ id, holder, base_item }) => ({
          id,
          holder,
          base_item,
        })),
      ).toEqual([
        { id: "harbor-ledger", holder: "npc:ilsa", base_item: null },
        {
          id: "healing-draught",
          holder: "npc:bram",
          base_item: "potion_of_healing",
        },
      ]);
      await expect(stepButton(page, "Предметы и награды")).toContainText(
        "готово",
      );
      await expect(
        combobox(group(loot, "Книга гавани"), "Где лежит"),
      ).toHaveValue("npc:ilsa");
    });

    await test.step("5. Улики: три улики откровения замысла в разных местах", async () => {
      await stepButton(page, "Улики").click();
      const clues = stepPanel(page, "Улики");
      const { draft } = await runJob(
        page,
        "Улики",
        button(clues, "Сгенерировать шаг"),
        { step: "clues" },
      );
      const [truth] = draft.revelations as {
        id: string;
        clues: { id: string; where: string }[];
      }[];
      expect(truth.id).toBe("ash-truth");
      expect(truth.clues.map(({ id, where }) => ({ id, where }))).toEqual([
        { id: "clue-ash-marks", where: "feature:ash-marks" },
        { id: "clue-bram", where: "npc:bram" },
        { id: "clue-ilsa", where: "npc:ilsa" },
      ]);
      const revelation = group(clues, "Откровение ash-truth");
      await expect(
        combobox(group(revelation, "Улика clue-bram"), "Где лежит"),
      ).toHaveValue("npc:bram");
      // Откровение с вложенными уликами (финальное покрытие B10).
      await expectFitsPhone(page, "редактор: «Улики» откровения");
      for (const label of [...STEPS, ...MAP_STEPS, ...FIGHT_STEPS]) {
        await expect(stepButton(page, label)).toContainText("готово");
      }
    });

    await test.step("6. Проверка: ошибок нет, готовность 100%, одно предупреждение со ссылкой на шаг", async () => {
      const validated = draftsResponse(page, "POST", /^\/[0-9a-f]+\/validate$/);
      await stepButton(page, "Проверка").click();
      const response = await validated;
      expect(response.status()).toBe(200);
      const report = (await response.json()) as {
        errors: unknown[];
        warnings: unknown[];
        score: number;
        readiness_percent: number;
      };
      // У гавани (ответ первой локации без деталей) нет интересных мест — это предупреждение,
      // публикацию оно не закрывает.
      expect(report).toMatchObject({
        errors: [],
        warnings: [
          {
            code: "no_features",
            message: "в локации нет ни одного интересного места",
            ref: "location:harbor",
            step: "location_contents",
          },
        ],
        readiness_percent: 100,
      });
      const review = stepPanel(page, "Проверка");
      await expect(review).toContainText("Готовность: 100%");
      await expect(
        review.getByRole("progressbar", { name: "Готовность" }),
      ).toHaveAttribute("aria-valuenow", "100");
      await expect(review).toContainText(`Балл: ${report.score} из 100`);
      await expect(review.getByText("Ошибок нет.")).toBeVisible();
      await expect(
        review.getByText("в локации нет ни одного интересного места"),
      ).toBeVisible();
      await expectFitsPhone(page, "редактор: «Проверка»");
      // Ссылка находки ведёт к сущности (ревью B6 R6): «Наполнение» открывает гавань.
      await button(review, "Перейти к шагу «Наполнение»").click();
      const contents = stepPanel(page, "Наполнение");
      await expect(combobox(contents, "Локация")).toHaveValue("harbor");
      await expect(contents).toBeFocused();
    });

    await test.step("7. Публикация: что увидят игроки, «Опубликовать» и ответ 201", async () => {
      const checked = draftsResponse(page, "POST", /^\/[0-9a-f]+\/validate$/);
      await stepButton(page, "Публикация").click();
      expect((await checked).status()).toBe(200);
      const panel = stepPanel(page, "Публикация");
      await expect(panel).toContainText(`Название: ${ADVENTURE_TITLE}`);
      await expect(panel).toContainText(
        "Предмет «Книга гавани»: Книга, в которую начальница гавани записывает корабли.",
      );
      await expect(panel).toContainText(`id приключения: ${ADVENTURE_ID}`);
      await expectFitsPhone(page, "редактор: «Публикация»");
      // Предметы лежат у персонажей, поэтому подтверждения «лежат открыто» нет.
      const publish = button(panel, "Опубликовать");
      await expect(publish).toBeEnabled();
      const published = draftsResponse(page, "POST", /^\/[0-9a-f]+\/publish$/);
      await publish.click();
      const response = await published;
      expect(response.status()).toBe(201);
      expect(response.request().postDataJSON()).toEqual({});
      expect(await response.json()).toMatchObject({
        adventure_id: ADVENTURE_ID,
      });
      await expect(panel).toContainText(
        `Приключение опубликовано под id «${ADVENTURE_ID}».`,
      );
    });

    const game =
      await test.step("8. Игра на опубликованном: каталог уже знает его, сервер не перезапускался", async () => {
        await button(page, "К списку приключений").click();
        await page.getByRole("tab", { name: "Мои игры", exact: true }).click();
        const catalog = page.waitForResponse(
          (response) =>
            response.request().method() === "GET" &&
            new URL(response.url()).pathname === "/api/adventures",
        );
        await button(page, "Создать игру").click();
        expect(await (await catalog).json()).toContainEqual(
          expect.objectContaining({
            id: ADVENTURE_ID,
            title: ADVENTURE_TITLE,
            blocking: false,
          }),
        );
        const adventures = page.getByRole("radiogroup", {
          name: "Приключение",
        });
        await adventures.locator("label", { hasText: ADVENTURE_TITLE }).click();
        await expect(
          adventures.getByRole("radio", { name: new RegExp(ADVENTURE_TITLE) }),
        ).toBeChecked();
        const sent = page.waitForResponse(
          (response) =>
            response.request().method() === "POST" &&
            new URL(response.url()).pathname === "/api/games",
        );
        await button(page, "Создать").click();
        const response = await sent;
        expect(response.status()).toBe(201);
        expect(response.request().postDataJSON()).toEqual({
          adventure_id: ADVENTURE_ID,
        });
        const created = (await response.json()) as { id: string; code: string };
        expect(created).toMatchObject({
          adventure_id: ADVENTURE_ID,
          adventure_title: ADVENTURE_TITLE,
        });
        await expect(page.getByText("Позовите друзей")).toBeVisible();
        await expect(
          page.getByRole("region", { name: ADVENTURE_TITLE, exact: true }),
        ).toBeVisible();
        return created;
      });

    const friends =
      await test.step("9. Отряд из шести: друзья входят по коду через API, хост создаёт героя и начинает", async () => {
        const headersOf: Record<string, string>[] = [];
        for (const [index, [login, hero]] of FRIENDS.entries()) {
          const headers = await authHeaders(request, login);
          await postAs(request, headers, "/api/games/join", {
            code: game.code,
          });
          await postAs(request, headers, `/api/games/${game.id}/characters`, {
            mode: "quick",
            concept: FRIEND_CONCEPT,
            ...hero,
          });
          await postAs(request, headers, `/api/games/${game.id}/ready`, {
            ready: true,
          });
          // Хост узнаёт о каждом входе из события лобби.
          await expect(page.getByText(`Игроки (${index + 2})`)).toBeVisible();
          headersOf.push(headers);
        }

        await button(page, "Создать персонажа").click();
        await button(page, "Быстро").click();
        await pickQuickHero(page, HOST_HERO);
        await page
          .getByLabel("Опиши героя одной строкой")
          .fill(HOST_HERO.concept);
        await page.getByLabel("Имя (необязательно)").fill(HOST_HERO.name);
        await button(page, "Создать героя").click();
        const ready = page.getByRole("switch", { name: "Готов" });
        await expect(ready).toBeEnabled();
        await ready.click();
        await expect(ready).toBeChecked();
        await expect(button(page, "Начать")).toBeEnabled();
        await button(page, "Начать").click();
        return headersOf;
      });

    const feed = page.getByRole("list", { name: "Лента" });
    await test.step("10. Вплетение и открытие на приключении из конструктора", async () => {
      await expect(feed.getByText(OPENING)).toBeVisible();
      // Зацепка сведена с опубликованным приключением: цель и спутник остались, а знакомые и
      // места из ответа сценария (NPC и локации демо-склепа) в маяке не нашлись и отброшены.
      const note = page.getByRole("note", { name: "Только тебе" });
      await expect(note).toContainText(`Твоя зацепка, ${HOST_HERO.name}.`);
      await expect(note).toContainText(HOST_GOAL);
      await expect(note).toContainText("Спутники: Борис — старый друг");
      await expect(note).not.toContainText("Знакомые");
      await expect(note).not.toContainText("Места");
      await expect(page.getByText("Обсуждение", { exact: true })).toBeVisible();
    });

    await test.step("11. Раунд: хост сдаёт ход, друзья пасуют, мастер отвечает бросками и рассказом", async () => {
      for (const headers of friends) {
        await postAs(request, headers, `/api/games/${game.id}/action/pass`);
      }
      await page
        .getByLabel("Что делает твой персонаж?")
        .fill("Расспросить Ильзу о смотрителе маяка");
      await button(page, "Готов").click();
      await expect(page.getByText("Ход сдан")).toBeVisible();
      await button(page, "Собрать ходы").click();

      await expect(feed.getByText(FIRST_ROUND)).toBeVisible();
      await expect(
        page.getByRole("article", {
          name: /Борис\. Ловкость \(Акробатика\)/,
        }),
      ).toBeVisible();
    });
  } finally {
    await context.close();
    // Игра на шестерых (в ней и dev-4, у которого к концу `lobby-membership.spec.ts` не должно
    // быть игр) и опубликованное приключение не должны попасть в следующие сценарии.
    await server.resetDatabase();
  }
});

// --- финальное покрытие блока 6: копия каталога, публикация с отказами, архив туда и обратно ---

// Встроенное приключение папки контента: его копия — полный черновик без генерации.
const DEMO_ID = "demo-crypt";
const DEMO_TITLE = "Склеп должника";

/** Разделы и настройки черновика: то, что переносит архив (дополнение B6, §7). */
const sectionsOf = (draft: DraftJson) => ({
  title: draft.title,
  adventure_id: draft.adventure_id,
  settings: draft.settings,
  world: draft.world,
  plot: draft.plot,
  locations: draft.locations,
  start_location: draft.start_location,
  finale_location: draft.finale_location,
  npcs: draft.npcs,
  relations: draft.relations,
  items: draft.items,
  encounters: draft.encounters,
  monsters: draft.monsters,
  revelations: draft.revelations,
});

/** Вход dev-игроком и вкладка «Приключения». */
async function openAdventures(page: Page, login: string): Promise<void> {
  await page.goto(`/?dev=${login}`);
  await expect(page.getByText(`Вы вошли как ${login}.`)).toBeVisible();
  await page.getByRole("tab", { name: "Приключения", exact: true }).click();
}

/** «Сделать копию» приключения каталога: ответ 201 — новый черновик, редактор открыт на нём. */
async function copyFromCatalog(page: Page, title: string): Promise<DraftJson> {
  const copied = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /^\/api\/adventures\/[^/]+\/draft$/.test(
        new URL(response.url()).pathname,
      ),
  );
  await button(page, `Сделать копию: ${title}`).click();
  const response = await copied;
  expect(response.status()).toBe(201);
  const draft = (await response.json()) as DraftJson;
  await expect(stepPanel(page, "Замысел")).toBeVisible();
  return draft;
}

/** Ответ проверки при входе на «Проверку» или «Публикацию». */
async function openValidated(
  page: Page,
  label: "Проверка" | "Публикация",
): Promise<{
  errors: { message: string; step: string | null }[];
  readiness_percent: number;
}> {
  const validated = draftsResponse(page, "POST", /^\/[0-9a-f]+\/validate$/);
  await stepButton(page, label).click();
  const response = await validated;
  expect(response.status()).toBe(200);
  return (await response.json()) as {
    errors: { message: string; step: string | null }[];
    readiness_percent: number;
  };
}

/** «Опубликовать» на открытой «Публикации»: код ответа и тело. */
async function publishNow(
  page: Page,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const published = draftsResponse(page, "POST", /^\/[0-9a-f]+\/publish$/);
  await button(stepPanel(page, "Публикация"), "Опубликовать").click();
  const response = await published;
  expect(response.request().postDataJSON()).toEqual({});
  return {
    status: response.status(),
    body: (await response.json()) as Record<string, unknown>,
  };
}

test("конструктор III: копия каталога — ошибка проверки ведёт на «Карту», занятый id, публикация 201 и 200, id после публикации не меняется", async ({
  browser,
  baseURL,
  server,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  const COPY_ID = "demo-crypt-copy";

  try {
    await test.step("1. Копия встроенного приключения открывается своим замыслом", async () => {
      await openAdventures(page, "dev-5");
      const copy = await copyFromCatalog(page, DEMO_TITLE);
      expect(copy).toMatchObject({
        title: DEMO_TITLE,
        adventure_id: DEMO_ID,
        start_location: "village",
        finale_location: "crypt",
      });
      const concept = stepPanel(page, "Замысел");
      await expect(textbox(concept, "Название")).toHaveValue(DEMO_TITLE);
      await expect(textbox(concept, "id приключения")).toHaveValue(DEMO_ID);
    });

    await test.step("2. Старт снят: «Проверка» даёт «Старт не выбран», ссылка ведёт на «Карту», «Опубликовать» закрыта", async () => {
      await stepButton(page, "Карта").click();
      const map = stepPanel(page, "Карта");
      await combobox(map, "Старт").selectOption("");
      const { sent } = await saveSection(page, "Карта", "locations");
      expect(sent.data).toMatchObject({ start_location: null });

      const report = await openValidated(page, "Проверка");
      expect(report.errors).toContainEqual(
        expect.objectContaining({
          message: "Старт не выбран",
          step: "locations",
        }),
      );
      const review = stepPanel(page, "Проверка");
      const errors = review.getByRole("list", { name: "Ошибки" });
      const start = errors
        .getByRole("listitem")
        .filter({ hasText: "Старт не выбран" });
      await start
        .getByRole("button", { name: "Перейти к шагу «Карта»" })
        .click();
      await expect(stepPanel(page, "Карта")).toBeVisible();
      await expect(stepButton(page, "Карта")).toHaveAttribute(
        "aria-current",
        "step",
      );

      const blocked = await openValidated(page, "Публикация");
      const panel = stepPanel(page, "Публикация");
      await expect(panel).toContainText(
        `Сначала исправьте ошибки проверки: ${blocked.errors.length}.`,
      );
      await expect(button(panel, "Опубликовать")).toBeDisabled();
    });

    await test.step("3. Старт вернули: ошибок нет, но id занят папкой — 409 и переход к «Замыслу»", async () => {
      await stepButton(page, "Карта").click();
      await combobox(stepPanel(page, "Карта"), "Старт").selectOption("village");
      await saveSection(page, "Карта", "locations");

      const report = await openValidated(page, "Публикация");
      expect(report.errors).toEqual([]);
      const panel = stepPanel(page, "Публикация");
      await expect(button(panel, "Опубликовать")).toBeEnabled();
      const taken = await publishNow(page);
      expect(taken).toEqual({
        status: 409,
        body: {
          detail: "Этот id уже занят: выберите другой.",
          code: "id_taken",
        },
      });
      await expect(panel.getByRole("alert")).toContainText(
        "Этот id уже занят: выберите другой.",
      );
      await button(panel, "Перейти к шагу «Замысел»").click();
      await expect(stepPanel(page, "Замысел")).toBeVisible();
    });

    await test.step("4. Свой id в «Замысле», публикация 201, повторная — 200", async () => {
      const concept = stepPanel(page, "Замысел");
      await textbox(concept, "id приключения").fill(COPY_ID);
      const { sent, draft } = await saveSection(page, "Замысел", "settings");
      expect(sent.data).toMatchObject({ adventure_id: COPY_ID });
      expect(draft.adventure_id).toBe(COPY_ID);

      await openValidated(page, "Публикация");
      const panel = stepPanel(page, "Публикация");
      await expect(panel).toContainText(`id приключения: ${COPY_ID}`);
      const first = await publishNow(page);
      expect(first.status).toBe(201);
      expect(first.body).toMatchObject({ adventure_id: COPY_ID });
      await expect(panel).toContainText(
        `Приключение опубликовано под id «${COPY_ID}».`,
      );
      const again = await publishNow(page);
      expect(again.status).toBe(200);
      expect(again.body).toMatchObject({ adventure_id: COPY_ID });
      await expect(panel).toContainText(
        `Приключение опубликовано под id «${COPY_ID}».`,
      );
      await expect(panel.getByRole("alert")).toHaveCount(0);
    });

    await test.step("5. После публикации id не меняется: 422 adventure_id_locked, правка на месте", async () => {
      await stepButton(page, "Замысел").click();
      const concept = stepPanel(page, "Замысел");
      await textbox(concept, "id приключения").fill("demo-crypt-renamed");
      const refused = draftsResponse(
        page,
        "PUT",
        /^\/[0-9a-f]+\/sections\/settings$/,
      );
      await button(concept, "Сохранить раздел").click();
      const response = await refused;
      expect(response.status()).toBe(422);
      expect(await response.json()).toEqual({
        detail: "Приключение уже опубликовано под этим id.",
        code: "adventure_id_locked",
      });
      await expect(page.getByRole("alert")).toContainText(
        "Приключение уже опубликовано под этим id.",
      );
      await expect(textbox(concept, "id приключения")).toHaveValue(
        "demo-crypt-renamed",
      );
    });

    await test.step("6. В «Готовых приключениях» — и папка, и опубликованная копия", async () => {
      await button(page, "К списку приключений").click();
      const catalog = page.getByRole("region", { name: "Готовые приключения" });
      await expect(catalog.getByText(COPY_ID, { exact: true })).toBeVisible();
      await expect(catalog.getByText(DEMO_ID, { exact: true })).toBeVisible();
    });
  } finally {
    await context.close();
    // Опубликованная копия не должна попасть в каталог следующих сценариев.
    await server.resetDatabase();
  }
});

test("конструктор III: «Скачать архив» и импорт того же файла дают тот же черновик; не zip и больше 2 МиБ — отказ с текстом сервера", async ({
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
  const importField = () => page.getByLabel("Импорт архива (.zip)");
  const importResponse = () => draftsResponse(page, "POST", /^\/import$/);

  try {
    const copy =
      await test.step("1. Копия каталога и «Скачать архив»: zip под именем <id>.zip", async () => {
        await openAdventures(page, "dev-6");
        const draft = await copyFromCatalog(page, DEMO_TITLE);
        await openValidated(page, "Публикация");
        const exported = draftsResponse(page, "GET", /^\/[0-9a-f]+\/export$/);
        const downloaded = page.waitForEvent("download");
        await button(stepPanel(page, "Публикация"), "Скачать архив").click();
        const response = await exported;
        expect(response.status()).toBe(200);
        expect(response.headers()["content-type"]).toBe("application/zip");
        expect(response.headers()["content-disposition"]).toBe(
          `attachment; filename="${DEMO_ID}.zip"`,
        );
        const download = await downloaded;
        expect(download.suggestedFilename()).toBe(`${DEMO_ID}.zip`);
        const file = await download.path();
        return { draft, archive: fs.readFileSync(file) };
      });

    await test.step("2. Импорт скачанного файла: сырое тело application/zip, 201 — тот же черновик", async () => {
      // Архив zip начинается с «PK».
      expect(copy.archive.subarray(0, 2).toString("latin1")).toBe("PK");
      await button(page, "К списку приключений").click();
      const imported = importResponse();
      // Тип файла браузер может не знать: клиент всё равно шлёт application/zip.
      await importField().setInputFiles({
        name: `${DEMO_ID}.zip`,
        mimeType: "application/octet-stream",
        buffer: copy.archive,
      });
      const response = await imported;
      expect(response.status()).toBe(201);
      const request = response.request();
      expect(await request.headerValue("content-type")).toBe("application/zip");
      // Тело-файл (Blob) Chromium в запросе не показывает: что ушёл именно скачанный архив, видно по
      // ответу — тот же черновик.
      const draft = (await response.json()) as DraftJson;
      expect(draft.id).not.toBe(copy.draft.id);
      expect(sectionsOf(draft)).toEqual(sectionsOf(copy.draft));
      await expect(
        textbox(stepPanel(page, "Замысел"), "id приключения"),
      ).toHaveValue(DEMO_ID);
    });

    await test.step("3. Файл не zip — 422 archive_invalid: текст и проблема по-русски, черновика нет", async () => {
      await button(page, "К списку приключений").click();
      const refused = importResponse();
      await importField().setInputFiles({
        name: "notes.zip",
        mimeType: "application/zip",
        buffer: Buffer.from("это не архив, а заметки"),
      });
      const response = await refused;
      expect(response.status()).toBe(422);
      expect(await response.json()).toEqual({
        detail: "Архив не подходит для импорта.",
        code: "archive_invalid",
        problems: ["архив: это не zip-архив или он повреждён"],
      });
      const alert = page.getByRole("alert");
      await expect(alert).toContainText("Архив не подходит для импорта.");
      await expect(alert).toContainText(
        "архив: это не zip-архив или он повреждён",
      );
      await expect(
        page.getByRole("navigation", { name: "Шаги приключения" }),
      ).toHaveCount(0);
    });

    await test.step("4. Больше 2 МиБ — 413 too_large: текст сервера, черновика нет", async () => {
      const refused = importResponse();
      await importField().setInputFiles({
        name: "big.zip",
        mimeType: "application/zip",
        buffer: Buffer.alloc(2 * 1024 * 1024 + 1, 0x50),
      });
      const response = await refused;
      expect(response.status()).toBe(413);
      expect(await response.json()).toEqual({
        detail: "Архив больше 2 МиБ.",
        code: "too_large",
      });
      await expect(page.getByRole("alert")).toContainText(
        "Архив больше 2 МиБ.",
      );
      await expect(
        page.getByRole("navigation", { name: "Шаги приключения" }),
      ).toHaveCount(0);
    });
  } finally {
    await context.close();
  }
});

test("конструктор III: пустой черновик с id — «Проверка» без ошибок пустых шагов, а публикация отвечает 422 blocking_errors со списком", async ({
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
    await openAdventures(page, "dev-3");
    const created = draftsResponse(page, "POST", /^$/);
    await button(page, "Новое приключение").click();
    expect((await created).status()).toBe(201);
    const concept = stepPanel(page, "Замысел");
    await textbox(concept, "Название").fill("Пустышка");
    await textbox(concept, "id приключения").fill("empty-draft");
    const { draft } = await saveSection(page, "Замысел", "settings");
    expect(draft.adventure_id).toBe("empty-draft");

    // Пустые шаги проверка не судит (B4 §8): ошибок нет, готовность — только за «Замысел».
    const report = await openValidated(page, "Проверка");
    expect(report).toMatchObject({ errors: [], readiness_percent: 14 });
    const review = stepPanel(page, "Проверка");
    await expect(review).toContainText("Готовность: 14%");
    await expect(review.getByText("Ошибок нет.")).toBeVisible();

    await openValidated(page, "Публикация");
    const panel = stepPanel(page, "Публикация");
    await expect(button(panel, "Опубликовать")).toBeEnabled();
    const refused = await publishNow(page);
    expect(refused.status).toBe(422);
    expect(refused.body).toMatchObject({
      detail: "В приключении есть ошибки: исправьте их, потом публикуйте.",
      code: "blocking_errors",
    });
    const problems = refused.body.problems as string[];
    expect(problems.length).toBeGreaterThan(0);
    const alert = panel.getByRole("alert");
    await expect(alert).toContainText(
      "В приключении есть ошибки: исправьте их, потом публикуйте.",
    );
    await expect(alert.getByRole("listitem")).toHaveCount(problems.length);
    // Строка отказа «<шаг>: <текст>» — с подписью экрана вместо id шага (manual B6, nit 3).
    const colon = problems[0].indexOf(": ");
    const step = problems[0].slice(0, colon);
    const first = alert.getByRole("listitem").first();
    await expect(first).toHaveText(
      new RegExp(`^${escapeRegExp(STEP_LABELS[step] ?? step)}: `),
    );
    await expect(first).toContainText(
      problems[0].slice(problems[0].lastIndexOf(": ") + 2),
    );
    await expect(panel).not.toContainText("Приключение опубликовано");
  } finally {
    await context.close();
  }
});

test("конструктор III: пределы боя на настоящем сервере — больше 20 монстров и больше 4 групп: 422 с problems, правка на месте", async ({
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

  /** «Сохранить раздел» на «Боях и боссах»: ответ сервера на запись боёв. */
  const saveRefused = async (): Promise<string[]> => {
    const refused = draftsResponse(
      page,
      "PUT",
      /^\/[0-9a-f]+\/sections\/encounters$/,
    );
    await button(stepPanel(page, "Бои и боссы"), "Сохранить раздел").click();
    const response = await refused;
    expect(response.status()).toBe(422);
    const body = (await response.json()) as {
      detail: string;
      code: string;
      problems: string[];
    };
    expect(body).toMatchObject({
      detail: "Данные раздела не прошли проверку.",
      code: "invalid_data",
    });
    const alert = page.getByRole("alert");
    await expect(alert).toContainText("Данные раздела не прошли проверку.");
    // Путь поля — словами формы (manual B4 nit): сообщение сервера как есть, без пути схемы.
    for (const problem of body.problems)
      await expect(alert).toContainText(
        problem.slice(problem.indexOf(": ") + 2),
      );
    await expect(alert).toContainText("Бои → «fabian-fight» → Монстры");
    await expect(alert).not.toContainText("encounters.0");
    return body.problems;
  };

  try {
    await openAdventures(page, "dev-2");
    await copyFromCatalog(page, DEMO_TITLE);
    await stepButton(page, "Бои и боссы").click();
    const fight = group(stepPanel(page, "Бои и боссы"), "Бой fabian-fight");
    const first = group(fight, "Группа 1");
    // Справочник SRD пришёл: в выборе монстра есть скелет.
    await expect(
      combobox(first, "Монстр").locator("option[value='skeleton']"),
    ).toHaveCount(1);

    // Фанатик ×20 и скелеты ×2 — 22 монстра в бою (предел 20, B6 §5).
    await combobox(first, "Сколько").selectOption("20");
    const total = await saveRefused();
    expect(total).toContainEqual(
      expect.stringContaining("всего монстров в бою — не больше 20"),
    );
    await expect(combobox(first, "Сколько")).toHaveValue("20");

    // Пять групп по одному монстру (предел 4 группы).
    await combobox(first, "Сколько").selectOption("1");
    for (const index of [3, 4, 5]) {
      await button(fight, "Добавить монстров").click();
      await combobox(group(fight, `Группа ${index}`), "Монстр").selectOption(
        "skeleton",
      );
    }
    const groups = await saveRefused();
    expect(groups).toContainEqual(
      expect.stringMatching(/monsters: не больше 4/),
    );
    await expect(group(fight, "Группа 5")).toBeVisible();
  } finally {
    await context.close();
  }
});
