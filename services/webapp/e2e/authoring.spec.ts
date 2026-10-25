import type { Locator, Page, Response } from "@playwright/test";
import { expect, test } from "./server.ts";

// Конструктор приключений на настоящем сервере (финальное покрытие блока 4 DND-2). Юнит-тесты
// редактора идут на заглушках по контракту блока; здесь тот же редактор говорит с `dnd-server`
// (dev, `FakeProvider` по `e2e_full.yaml`, временная база) через прокси Vite: черновик, разделы с
// версией, пожелание, задания генерации с опросом и закрепление проходят по настоящему API.
// Ответы шагов генератора — раздел `json` сценария (`generator.concept` … `generator.plot`): каждое
// задание собирает свой провайдер и получает тот же ответ, поэтому повторная генерация богов
// возвращает те же титулы — так видно, что закреплённый бог остался, а ручная правка
// незакреплённого заменена ответом шага.

const LOGIN = "dev-2";
const STEPS = ["Замысел", "Мир", "Боги", "Фракции", "Сюжет"] as const;
type StepLabel = (typeof STEPS)[number];

/** Тело черновика из ответа сервера: тесту нужны только эти поля. */
interface DraftJson {
  id: string;
  version: number;
  title: string;
  adventure_id: string | null;
  world: unknown;
  plot: unknown;
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
 * «Сгенерировать шаг» на открытом шаге: задание ставится (202), пока оно идёт — «Генерирую…» и
 * правка закрыта, после `done` форма открыта и шаг «готово». Возвращает поставленное задание.
 */
async function generateStep(
  page: Page,
  label: StepLabel,
  step: string,
): Promise<JobJson> {
  const panel = stepPanel(page, label);
  const queued = draftsResponse(page, "POST", /^\/[0-9a-f]+\/generate$/);
  await button(panel, "Сгенерировать шаг").click();
  const response = await queued;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toEqual({ step });
  const job = (await response.json()) as JobJson;
  expect(job).toMatchObject({ step, target: null, status: "queued" });

  await expect(page.getByText("Генерирую…")).toBeVisible();
  await expect(button(panel, "Сохранить раздел")).toBeDisabled();
  await expect(page.getByText("Генерирую…")).toHaveCount(0);
  await expect(page.getByText(/Не удалось сгенерировать шаг/)).toHaveCount(0);
  await expect(button(panel, "Сохранить раздел")).toBeEnabled();
  await expect(stepButton(page, label)).toContainText("готово");
  return job;
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
