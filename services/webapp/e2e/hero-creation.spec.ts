import type {
  Browser,
  BrowserContext,
  Locator,
  Page,
  Request,
  Response,
} from "@playwright/test";
import { carousel, expectPicked, pickInCarousel, shownCard } from "./hero.ts";
import { expectFitsPhone, layoutProblems } from "./layout.ts";
import { expect, test } from "./server.ts";

// Создание героя на настоящем сервере (финальное покрытие B8, решения владельца R1, R4, R9, R10;
// «Создание героя 2.0» — блок 7 DND-2, спека §6–§7). Юнит-тесты экранов идут на заглушках
// сервера и держат в руках форму ответов, описанную в плане. Здесь те же экраны разговаривают
// с настоящим `dnd-server` (dev, `FakeProvider`, временная база) через прокси Vite: если форма
// ответа сервера разойдётся с типами клиента, тест это покажет. Подробные карточки сверяются с
// `details` из того же ответа правил создания, который получил экран.
// Игры не начинаются: сценарий мастера рассчитан на шестерых игроков (см. six-players.spec.ts),
// а для героев мастер нужен только как автор описания (`character.flavor`, `character.backstory`).

const ABILITIES = [
  "Сила",
  "Ловкость",
  "Телосложение",
  "Интеллект",
  "Мудрость",
  "Харизма",
] as const;

// Цена очка покупки по правилам (SRD); тест сверяет с ней раскладку сервера, а не строит свою.
const POINT_COST: Record<number, number> = {
  8: 0,
  9: 1,
  10: 2,
  11: 3,
  12: 4,
  13: 5,
  14: 7,
  15: 9,
};

// Метки, которые должны дойти строго до своего адресата. Нарочно необычные.
const HOST_NAME = "Торгрим Железный";
const HOST_CONCEPT = "Ветеран пограничных войн ЗАМЫСЕЛ-ТОРГРИМА";
const FRIEND_CONCEPT = "Карманник с добрым сердцем ЗАМЫСЕЛ-ПЛУТА";
// Ответ `character.backstory` в сценарии мастера (e2e_full.yaml): «Помоги написать» кладёт его в поле.
const BACKSTORY_FROM_MASTER =
  /^Вырос в портовом городе, где каждый второй чей-то должник\./;
// Имя из ответа `character.flavor` в том же сценарии: так мастер называет героев без имени.
const NAME_FROM_MASTER = "Эльвира Вайс";

interface Player {
  login: string;
  context: BrowserContext;
  page: Page;
  /** Кадры сокета, которые сервер прислал странице. */
  frames: string[];
  /** Тела ответов REST, которые сервер прислал странице. */
  bodies: string[];
}

async function openPlayer(
  browser: Browser,
  baseURL: string,
  login: string,
): Promise<Player> {
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    colorScheme: "dark",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  const frames: string[] = [];
  const bodies: string[] = [];
  page.on("websocket", (socket) => {
    socket.on("framereceived", (frame) => frames.push(String(frame.payload)));
  });
  page.on("response", (response) => {
    // Только ответы API: исходники страницы и шрифты в проверке тайн не нужны.
    if (!new URL(response.url()).pathname.startsWith("/api/")) return;
    // Тело читается, когда ответ готов; сбой чтения (закрытая страница, редирект) не важен.
    void response
      .text()
      .then((text) => bodies.push(text))
      .catch(() => {});
  });
  await page.goto(`/?dev=${login}`);
  return { login, context, page, frames, bodies };
}

const button = (page: Page, name: string) =>
  page.getByRole("button", { name, exact: true });

/** Ответ сервера на запрос, который выполнит `action`. */
async function responseTo(
  page: Page,
  method: string,
  pathEnd: string,
  action: () => Promise<void>,
): Promise<Response> {
  const matching = page.waitForResponse(
    (response) =>
      response.request().method() === method &&
      new URL(response.url()).pathname.endsWith(pathEnd),
  );
  await action();
  return matching;
}

/** Запрос клиента, который выполнит `action`: по нему видно, что именно клиент прислал серверу. */
async function requestOf(
  page: Page,
  method: string,
  pathEnd: string,
  action: () => Promise<void>,
): Promise<Request> {
  const matching = page.waitForRequest(
    (request) =>
      request.method() === method &&
      new URL(request.url()).pathname.endsWith(pathEnd),
  );
  await action();
  return matching;
}

// --- правила создания с сервера: `details` у вариантов (контракт блока 7) ---

interface Named {
  id: string;
  name: string;
}

/** Запись раздела карточки: черта расы, особенность класса. */
interface EntryJson {
  name: string;
  summary: string;
  mechanical: boolean;
  level?: number;
}

interface SpeciesJson extends Named {
  details: {
    description: string;
    size: string;
    speed_text: string;
    traits: EntryJson[];
  };
}

interface SpellJson extends Named {
  details: {
    school: string;
    casting_time: string;
    range_text: string;
    duration: string;
    summary: string;
  };
}

interface ClassJson extends Named {
  skill_options: string[];
  fighting_styles: string[];
  details: { hit_die: number; features: (EntryJson & { level: number })[] };
  spellcasting: {
    cantrip_options: SpellJson[];
    spell_options: SpellJson[];
  } | null;
}

interface BackgroundJson extends Named {
  skills: string[];
  details: { tool: string; feat: { name: string; summary: string } };
}

interface SkillJson extends Named {
  details: { ability: string; summary: string };
}

/** Черта или боевой стиль: `details` — только описание. */
interface DescribedJson extends Named {
  details: { summary: string };
}

interface CreationOptionsJson {
  abilities: Named[];
  skills: SkillJson[];
  species: SpeciesJson[];
  classes: ClassJson[];
  backgrounds: BackgroundJson[];
  fighting_styles: DescribedJson[];
}

/** Вариант по id из ответа сервера; без него тест падает с понятной причиной. */
function byId<T extends Named>(items: readonly T[], id: string): T {
  const item = items.find((candidate) => candidate.id === id);
  if (item === undefined) throw new Error(`в правилах сервера нет «${id}»`);
  return item;
}

/** «Создать персонажа»: экран сам загружает правила создания; тест берёт тот же ответ. */
async function openCreation(page: Page): Promise<CreationOptionsJson> {
  const answer = await responseTo(
    page,
    "GET",
    "/content/creation-options",
    () => button(page, "Создать персонажа").click(),
  );
  expect(answer.status()).toBe(200);
  return (await answer.json()) as CreationOptionsJson;
}

// --- экраны мастера подробного создания ---

/** Полоска мастера: «Шаг 2 из 10: Класс». */
const progress = (page: Page) => page.getByText(/^Шаг \d+ из \d+: /);

/** Раздел «Итога» по названию шага. */
const section = (page: Page, name: string) =>
  page.getByRole("region", { name, exact: true });

/** Строки раздела «Итога»: что выбрано на шаге. */
const summaryLines = (page: Page, name: string) =>
  section(page, name).getByRole("listitem");

/** «Изменить» у раздела «Итога»: переход на шаг этого раздела. */
const editSection = (page: Page, name: string) =>
  section(page, name).getByRole("button", { name: "Изменить" }).click();

/** Счётчик карусели «N из M». */
const counterOf = (group: Locator) => group.getByText(/^\d+ из \d+$/);

/** Значение строки подробной карточки «факт — значение» по её подписи. */
const fact = (card: Locator, label: string) =>
  card
    .locator(".ui-detail__fact")
    .filter({
      has: card.page().locator("dt", { hasText: new RegExp(`^${label}$`) }),
    })
    .locator("dd");

const IN_COMBAT = "действует в бою";

/** Скорость и дистанции приходят с сервера текстом в метрах: «9 м», «10,5 м». */
const METERS = /^\d+(,\d+)? м$/;

/** На экране нет футов: расстояния только в метрах (спека §6). */
async function expectNoFeet(scope: Locator, screenName: string) {
  expect(
    await scope.innerText(),
    `${screenName}: расстояния только в метрах`,
  ).not.toMatch(/фут/i);
}

/** Записи разделов карточки — ровно данные сервера по порядку, с уровнем и пометкой боя. */
async function expectEntries(card: Locator, entries: readonly EntryJson[]) {
  const shown = card.getByRole("listitem");
  await expect(shown).toHaveCount(entries.length);
  for (const [index, entry] of entries.entries()) {
    const item = shown.nth(index);
    await expect(item).toContainText(entry.name);
    await expect(item).toContainText(entry.summary);
    if (entry.level !== undefined) {
      await expect(item).toContainText(`${entry.level}-й уровень`);
    }
    // Механическое правило считает движок: карточка помечает его.
    if (entry.mechanical) await expect(item).toContainText(IN_COMBAT);
    else await expect(item).not.toContainText(IN_COMBAT);
  }
}

async function expectSpeciesCard(card: Locator, species: SpeciesJson) {
  const { details } = species;
  await expect(card).toContainText(details.description);
  await expect(fact(card, "Размер")).toHaveText(details.size);
  // Метры ровно как у сервера: клиент футы не переводит.
  expect(details.speed_text).toMatch(METERS);
  await expect(fact(card, "Скорость")).toHaveText(details.speed_text);
  await expectEntries(card, details.traits);
}

async function expectClassCard(card: Locator, cls: ClassJson) {
  await expect(fact(card, "Кость хитов")).toHaveText(`к${cls.details.hit_die}`);
  // Герой третьего уровня: особенности 1–3 уровня, у каждой — свой уровень.
  expect(new Set(cls.details.features.map(({ level }) => level))).toEqual(
    new Set([1, 2, 3]),
  );
  await expectEntries(card, cls.details.features);
}

async function expectBackgroundCard(card: Locator, background: BackgroundJson) {
  await expect(fact(card, "Инструмент")).toHaveText(background.details.tool);
  await expectEntries(card, [
    { ...background.details.feat, mechanical: false },
  ]);
}

async function expectSpellCard(card: Locator, spell: SpellJson) {
  const { details } = spell;
  await expect(fact(card, "Школа")).toHaveText(details.school);
  await expect(fact(card, "Время сотворения")).toHaveText(details.casting_time);
  await expect(fact(card, "Дистанция")).toHaveText(details.range_text);
  await expect(fact(card, "Длительность")).toHaveText(details.duration);
  await expect(card).toContainText(details.summary);
}

/** «Подробнее» у каждого заклинания списка: раскрытая карточка — данные сервера. */
async function expectAllSpellCards(group: Locator, list: readonly SpellJson[]) {
  await expect(group.getByRole("checkbox")).toHaveCount(list.length);
  for (const spell of list) {
    const more = group.getByRole("button", {
      name: `Подробнее: ${spell.name}`,
      exact: true,
    });
    await more.click();
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expectSpellCard(
      group.getByRole("article", { name: spell.name, exact: true }),
      spell,
    );
  }
}

/**
 * Листает карусель стрелкой «Следующая» от первой карточки до последней и сверяет каждую с
 * данными сервера. Пока ничего не выбрано, карусель начинается с первого варианта.
 */
async function browseAll<T extends Named>(
  group: Locator,
  items: readonly T[],
  check: (card: Locator, item: T) => Promise<void>,
) {
  const next = group.getByRole("button", { name: "Следующая" });
  // Листание без зацикливания: с первой карточки назад некуда, с последней — вперёд.
  await expect(
    group.getByRole("button", { name: "Предыдущая" }),
  ).toBeDisabled();
  for (const [index, item] of items.entries()) {
    if (index > 0) await next.click();
    await expect(counterOf(group)).toHaveText(
      `${index + 1} из ${items.length}`,
    );
    const card = shownCard(group);
    await expect(card).toHaveAccessibleName(item.name);
    await expect(
      card.getByRole("button", { name: "Выбрать", pressed: false }),
    ).toBeVisible();
    await check(card, item);
    await expectNoFeet(card, `карточка «${item.name}»`);
  }
  await expect(next).toBeDisabled();
}

/** Свайп мышью по показанной карточке: влево — к следующей, вправо — к предыдущей. */
async function swipe(group: Locator, direction: "left" | "right") {
  const card = shownCard(group);
  // Жест начинается на видимой части карточки — на строке заголовка.
  const heading = card.getByRole("heading", { level: 3 });
  await heading.scrollIntoViewIfNeeded();
  const cardBox = await card.boundingBox();
  const headingBox = await heading.boundingBox();
  if (cardBox === null || headingBox === null) {
    throw new Error("карточка карусели не видна");
  }
  const y = headingBox.y + headingBox.height / 2;
  const left = cardBox.x + cardBox.width * 0.2;
  const right = cardBox.x + cardBox.width * 0.8;
  const [from, to] = direction === "left" ? [right, left] : [left, right];
  const { mouse } = group.page();
  await mouse.move(from, y);
  await mouse.down();
  await mouse.move(to, y, { steps: 8 });
  await mouse.up();
}

/**
 * Шаг «Раса» (спека §7): все карточки стрелками, затем свайп, точки и клавиши; выбор — кнопкой
 * «Выбрать» на карточке.
 */
async function browseAndPickRace(
  page: Page,
  species: readonly SpeciesJson[],
  choice: string,
) {
  const races = carousel(page, "Раса");
  const counter = counterOf(races);
  const total = species.length;
  expect(total, "рас в правилах сервера").toBe(9);
  await expect(button(page, "Далее")).toBeDisabled();
  await browseAll(races, species, expectSpeciesCard);

  // Свайп: вправо — к предыдущей и влево — обратно; дальше последней не листается.
  await swipe(races, "right");
  await expect(counter).toHaveText(`${total - 1} из ${total}`);
  await expect(shownCard(races)).toHaveAccessibleName(species[total - 2].name);
  await swipe(races, "left");
  await expect(counter).toHaveText(`${total} из ${total}`);
  await swipe(races, "left");
  await expect(counter).toHaveText(`${total} из ${total}`);

  // Точки названы расами; текущая отмечена.
  const position = species.findIndex(({ name }) => name === choice) + 1;
  const dot = races.getByRole("button", { name: choice, exact: true });
  await dot.click();
  await expect(counter).toHaveText(`${position} из ${total}`);
  await expect(dot).toHaveAttribute("aria-current", "true");
  // Клавиши ←/→ листают, пока фокус внутри карусели (сейчас он на точке).
  await page.keyboard.press("ArrowRight");
  await expect(counter).toHaveText(`${position + 1} из ${total}`);
  await page.keyboard.press("ArrowLeft");
  await expect(counter).toHaveText(`${position} из ${total}`);

  await shownCard(races).getByRole("button", { name: "Выбрать" }).click();
  await expectPicked(page, "Раса", choice);
  // «Выбрано» только у выбранной расы: соседняя карточка по-прежнему предлагает «Выбрать».
  await races.getByRole("button", { name: "Следующая" }).click();
  await expect(
    shownCard(races).getByRole("button", { name: "Выбрать", pressed: false }),
  ).toBeVisible();
  await races.getByRole("button", { name: "Предыдущая" }).click();
  await expectPicked(page, "Раса", choice);
  await expect(button(page, "Далее")).toBeEnabled();
}

async function pointBuyScores(page: Page): Promise<Record<string, number>> {
  const scores: Record<string, number> = {};
  for (const name of ABILITIES) {
    const group = page.getByRole("group", { name, exact: true });
    scores[name] = Number(
      await group.locator(".char-point__value").innerText(),
    );
  }
  return scores;
}

/** Значения отмеченных флажков группы в порядке списка. */
async function checkedValues(group: Locator): Promise<string[]> {
  const values: string[] = [];
  for (const box of await group
    .getByRole("checkbox", { checked: true })
    .all()) {
    values.push(await box.inputValue());
  }
  return values;
}

const pointsSpent = (scores: Record<string, number>) =>
  Object.values(scores).reduce((sum, score) => sum + POINT_COST[score], 0);

/** Лист предпросмотра случайного героя: значения характеристик в порядке строк листа. */
async function sheetScores(page: Page): Promise<number[]> {
  const values = await page
    .getByRole("region", { name: "Лист героя" })
    .locator(".char-score dd")
    .allInnerTexts();
  return values.map(Number);
}

const SCORE_KEYS = ["str", "dex", "con", "int", "wis", "cha"] as const;

interface HeroJson {
  id: string;
  class_id: string;
  scores: Record<(typeof SCORE_KEYS)[number], number>;
  name: string;
}

async function createGameAndInvite(host: Player): Promise<string> {
  const { page } = host;
  await expect(page.getByText("Вы вошли как dev-1.")).toBeVisible();
  await button(page, "Создать игру").click();

  // R4: форма предлагает нейронку мастера из списка сервера. Сервер работает в dev без ключей:
  // провайдеры по API помечены «Не настроен», заглушка по сценарию доступна и выбрана заранее.
  // «Claude Code на этом компьютере» остаётся в списке (фабрика путь к `claude` не проверяет), но
  // запустить `claude` стенд не может: `CLAUDE_CLI_PATH` фикстуры указывает на несуществующий файл.
  const providers = page.getByRole("radiogroup", {
    name: "Нейронка для мастера",
  });
  await expect(providers).toBeVisible();
  const fake = providers.getByRole("radio", { name: /Заглушка по сценарию/ });
  await expect(fake).toBeChecked();
  await expect(fake).toBeEnabled();
  const anthropic = providers.getByRole("radio", {
    name: /Claude через API Anthropic/,
  });
  await expect(anthropic).toBeDisabled();
  await expect(providers).toContainText("Не настроен на сервере.");
  // Живая модель по подписке владельца заранее не выбирается и подписана: выбрать её можно
  // только самому.
  const live = providers.getByRole("radio", {
    name: /Claude Code на этом компьютере/,
  });
  await expect(live).not.toBeChecked();
  await expect(live).toHaveAccessibleName(
    /Живая модель по подписке на этом компьютере \(только dev\)/,
  );
  await expect(
    page.getByText("Этот вариант сам выбирает модель."),
  ).toBeVisible();
  // Контроль измерителя: с заведомо невыполнимым порогом он находит нажимаемые элементы.
  expect(await layoutProblems(page, 10_000)).not.toEqual([]);
  await expectFitsPhone(page, "форма новой игры");

  await button(page, "Создать").click();
  await expect(page.getByText("Позовите друзей")).toBeVisible();
  await expectFitsPhone(page, "лобби хоста");
  const invite = await page.getByText(/Код приглашения:/).textContent();
  const code = /Код приглашения:\s*([A-Z0-9]{6})/.exec(invite ?? "")?.[1] ?? "";
  expect(code, "код приглашения в лобби").toHaveLength(6);
  return code;
}

/** Что выбрал хост в подробном создании: по этому сверяется «Итог». */
interface HostChoice {
  scores: Record<string, number>;
  /** Названия навыков класса в порядке списка. */
  skills: string[];
}

/** «Итог» подробного героя хоста: выбор по разделам шагов, всё закончено. */
async function expectHostSummary(page: Page, { scores, skills }: HostChoice) {
  await expect(progress(page)).toHaveText("Шаг 10 из 10: Итог");
  await expect(summaryLines(page, "Раса")).toHaveText(["Дварф"]);
  await expect(summaryLines(page, "Класс")).toHaveText(["Варвар"]);
  await expect(summaryLines(page, "Предыстория")).toHaveText(["Солдат"]);
  await expect(summaryLines(page, "Характеристики")).toHaveText([
    "Покупка очков",
    ABILITIES.map((name) => `${name} ${scores[name]}`).join(", "),
  ]);
  await expect(summaryLines(page, "Бонус предыстории")).toHaveText([
    "Сила +2, Телосложение +1",
  ]);
  await expect(section(page, "Навыки и выборы класса")).toContainText(
    `Навыки класса: ${skills.join(", ")}`,
  );
  await expect(summaryLines(page, "Характер и внешность")).toHaveText([
    `Имя: ${HOST_NAME}`,
    `Замысел: ${HOST_CONCEPT}`,
  ]);
  await expect(summaryLines(page, "История героя")).toHaveText([
    BACKSTORY_FROM_MASTER,
  ]);
  await expect(page.getByText("Выбор на этом шаге не закончен.")).toHaveCount(
    0,
  );
  await expect(button(page, "Создать героя")).toBeEnabled();
}

/**
 * Подробный герой — мастер по шагам: дварф, варвар, солдат листанием карточек, характеристики
 * раскладывает сервер, «Подробнее» у навыков, «Итог» с «Изменить» и создание.
 */
async function createDetailedHero(host: Player) {
  const { page } = host;
  const options = await openCreation(page);
  const barbarian = byId(options.classes, "barbarian");
  const soldier = byId(options.backgrounds, "soldier");
  await expect(page.getByText("Создание героя")).toBeVisible();
  await expectFitsPhone(page, "выбор способа создания героя");
  await button(page, "Подробно").click();

  await expect(progress(page)).toHaveText("Шаг 1 из 10: Раса");
  await browseAndPickRace(page, options.species, "Дварф");
  await expectFitsPhone(page, "шаг «Раса»");
  await button(page, "Далее").click();

  await expect(progress(page)).toHaveText("Шаг 2 из 10: Класс");
  await browseAll(carousel(page, "Класс"), options.classes, expectClassCard);
  await pickInCarousel(page, "Класс", barbarian.name);
  await expectFitsPhone(page, "шаг «Класс»");
  await button(page, "Далее").click();

  await expect(progress(page)).toHaveText("Шаг 3 из 10: Предыстория");
  await browseAll(
    carousel(page, "Предыстория"),
    options.backgrounds,
    expectBackgroundCard,
  );
  await pickInCarousel(page, "Предыстория", soldier.name);
  await expectFitsPhone(page, "шаг «Предыстория»");
  await button(page, "Далее").click();

  // R9: «Распределить случайно» — ровно 27 очков, 8–15, нажимать можно сколько угодно.
  await expect(page.getByText("Осталось 27 из 27")).toBeVisible();
  const layouts: Record<string, number>[] = [];
  for (let press = 0; press < 3; press += 1) {
    const answer = await responseTo(
      page,
      "POST",
      "/characters/random-abilities",
      () => button(page, "Распределить случайно").click(),
    );
    expect(answer.status()).toBe(200);
    const body = (await answer.json()) as {
      method: string;
      scores: Record<string, number>;
    };
    expect(body.method).toBe("point_buy");
    // Экран показывает ровно числа сервера: клиент ничего не бросает и не меняет. Ответ уже пришёл,
    // а экран перерисуется следом, поэтому ждём совпадения, а не читаем сразу.
    const served = Object.fromEntries(
      ABILITIES.map((name, index) => [name, body.scores[SCORE_KEYS[index]]]),
    );
    await expect.poll(() => pointBuyScores(page)).toEqual(served);
    await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
    const shown = await pointBuyScores(page);
    expect(pointsSpent(shown), "очки покупки").toBe(27);
    for (const score of Object.values(shown)) {
      expect(score).toBeGreaterThanOrEqual(8);
      expect(score).toBeLessThanOrEqual(15);
    }
    // Ключевая характеристика варвара — Сила: она получает больше всех.
    expect(shown["Сила"]).toBe(Math.max(...Object.values(shown)));
    expect(shown["Сила"]).toBeGreaterThanOrEqual(14);
    layouts.push(shown);
  }
  const scores = layouts[2];
  await expectFitsPhone(page, "шаг «Характеристики»");
  await button(page, "Далее").click();

  // Бонус предыстории: +2 и +1 к характеристикам из списка «Солдата».
  await page.getByLabel("Бонус +2", { exact: true }).selectOption("str");
  await page.getByLabel("Бонус +1", { exact: true }).selectOption("con");
  await expectFitsPhone(page, "шаг «Бонус предыстории»");
  await button(page, "Далее").click();

  // Навыки класса: варвар выбирает два из тех, что не дала предыстория. У каждого навыка —
  // раскрываемая подробная карточка «Подробнее».
  const skills = page.getByRole("group", { name: /Навыки класса/ });
  const offered = barbarian.skill_options
    .filter((id) => !soldier.skills.includes(id))
    .map((id) => byId(options.skills, id));
  await expect(skills.getByRole("checkbox")).toHaveCount(offered.length);
  const chosenSkills = offered.slice(0, 2);
  const [skill] = chosenSkills;
  const more = skills.getByRole("button", {
    name: `Подробнее: ${skill.name}`,
    exact: true,
  });
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await more.click();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  const skillCard = skills.getByRole("article", {
    name: skill.name,
    exact: true,
  });
  await expect(fact(skillCard, "Характеристика")).toHaveText(
    byId(options.abilities, skill.details.ability).name,
  );
  await expect(skillCard).toContainText(skill.details.summary);
  await expectNoFeet(skillCard, `карточка навыка «${skill.name}»`);
  for (const { name } of chosenSkills) {
    await skills
      .getByRole("checkbox", { name, exact: true })
      .check({ force: true });
  }
  await expectFitsPhone(page, "шаг «Навыки» с раскрытой карточкой навыка");
  await button(page, "Далее").click();

  await expect(page.getByText("Стартовый набор выдаётся сразу")).toBeVisible();
  await expectFitsPhone(page, "шаг «Снаряжение»");
  await button(page, "Далее").click();

  await page.getByLabel("Имя", { exact: true }).fill(HOST_NAME);
  await page
    .getByLabel("Замысел одной строкой (необязательно)")
    .fill(HOST_CONCEPT);
  await expectFitsPhone(page, "шаг «Характер и внешность»");
  await button(page, "Далее").click();

  // «Помоги написать»: текст пишет мастер (сценарий), поле заполняется им.
  await button(page, "Помоги написать").click();
  await expect(page.getByLabel("Предыстория героя")).toHaveValue(
    BACKSTORY_FROM_MASTER,
  );
  await expectFitsPhone(page, "шаг «История героя»");
  // Кнопка создания — только на «Итоге»: до него ещё один шаг.
  await expect(button(page, "Создать героя")).toHaveCount(0);
  await button(page, "Далее").click();

  const choice: HostChoice = {
    scores,
    skills: chosenSkills.map(({ name }) => name),
  };
  await expectHostSummary(page, choice);
  // «Изменить» у раздела ведёт на его шаг: карусель открыта на прежнем выборе.
  await editSection(page, "Класс");
  await expect(progress(page)).toHaveText("Шаг 2 из 10: Класс");
  await expectPicked(page, "Класс", barbarian.name);
  // Вперёд к «Итогу» — ничего не потерялось по дороге.
  for (let step = 2; step < 10; step += 1) {
    await button(page, "Далее").click();
  }
  await expectHostSummary(page, choice);
  await expectFitsPhone(page, "шаг «Итог»");
  await expectNoFeet(page.locator("main"), "шаг «Итог»");

  const sent = await requestOf(page, "POST", "/characters", () =>
    button(page, "Создать героя").click(),
  );
  const created = await (await sent.response())?.json();
  await expect(page.getByText("Ваш герой")).toBeVisible();
  return { sent, created: created as { character: HeroJson }, choice };
}

test("три способа создать героя на настоящем сервере", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-1");
    players.push(host);
    const code = await createGameAndInvite(host);

    const friend = await openPlayer(browser, baseURL, "dev-2");
    players.push(friend);
    await expect(friend.page.getByText("Вы вошли как dev-2.")).toBeVisible();
    await friend.page.getByLabel("Код приглашения").fill(code);
    await button(friend.page, "Войти").click();
    await expect(friend.page.getByText("Ваш герой")).toBeVisible();
    await expect(host.page.getByText("Игроки (2)")).toBeVisible();

    const { sent, created, choice } =
      await test.step("Подробно: мастер по шагам, карточки, раскладка очков от сервера, «Итог», создание", () =>
        createDetailedHero(host));
    const { scores } = choice;
    const hostHero = created.character;
    // Клиент прислал выбор игрока, а не броски и не итоговые числа персонажа.
    const body = sent.postDataJSON() as {
      mode: string;
      choices: Record<string, unknown>;
    };
    expect(body.mode).toBe("detailed");
    expect(body.choices).toMatchObject({
      species: "dwarf",
      class_id: "barbarian",
      background: "soldier",
      method: "point_buy",
      name: HOST_NAME,
    });
    expect(Object.values(body.choices.base_scores as object)).toEqual(
      Object.values(scores),
    );
    expect(body).not.toHaveProperty("scores");
    expect(body).not.toHaveProperty("seed");
    expect(hostHero.name).toBe(HOST_NAME);
    expect(hostHero.class_id).toBe("barbarian");

    await test.step("«Изменить героя»: мастер открывается на «Итоге» с прежним выбором (R10)", async () => {
      const { page } = host;
      const stored = await responseTo(page, "GET", "/characters/mine", () =>
        button(page, "Изменить").click(),
      );
      const mine = (await stored.json()) as {
        mode: string;
        choices: { name: string; base_scores: Record<string, number> };
      };
      expect(mine.mode).toBe("detailed");
      expect(mine.choices.name).toBe(HOST_NAME);
      await expect(page.getByText("Изменить героя")).toBeVisible();
      await button(page, "Изменить").click();
      await expect(page.getByText("Подробный герой")).toBeVisible();
      await expectHostSummary(page, choice);
      await expectFitsPhone(page, "«Итог» при правке героя");

      // Раздел «Раса» ведёт на шаг расы: карусель открыта на дварфе, выбор цел.
      await editSection(page, "Раса");
      await expect(progress(page)).toHaveText("Шаг 1 из 10: Раса");
      await expectPicked(page, "Раса", "Дварф");
      await expect(counterOf(carousel(page, "Раса"))).toHaveText("2 из 9");
      await button(page, "Далее").click();
      await expectPicked(page, "Класс", "Варвар");
      await button(page, "Далее").click();
      await expectPicked(page, "Предыстория", "Солдат");
      await button(page, "Далее").click();
      // Те же очки, которые разложил сервер при создании.
      await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
      expect(await pointBuyScores(page)).toEqual(scores);
      // «Назад» выбор не теряет.
      await button(page, "Назад").click();
      await expectPicked(page, "Предыстория", "Солдат");
      await button(page, "Далее").click();
      await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
      for (let step = 4; step < 8; step += 1) {
        await button(page, "Далее").click();
      }
      await expect(page.getByLabel("Имя", { exact: true })).toHaveValue(
        HOST_NAME,
      );
      await expect(
        page.getByLabel("Замысел одной строкой (необязательно)"),
      ).toHaveValue(HOST_CONCEPT);
      await button(page, "Далее").click();
      await expect(page.getByLabel("Предыстория героя")).toHaveValue(
        BACKSTORY_FROM_MASTER,
      );
      await button(page, "Далее").click();
      await expectHostSummary(page, choice);
      // Правка не отправлена: герой в лобби прежний.
      await button(page, "Назад в лобби").click();
      await expect(page.getByText("Ваш герой")).toBeVisible();
    });

    const friendHero =
      await test.step("Случайно: предпросмотр, «Ещё раз», «Беру» (R1)", async () => {
        const { page } = friend;
        await button(page, "Создать персонажа").click();
        await button(page, "Случайно").click();

        const first = await responseTo(
          page,
          "POST",
          "/characters/random-preview",
          () => button(page, "Бросить судьбу").click(),
        );
        const firstPreview = (await first.json()) as {
          preview_id: string;
          character: HeroJson;
        };
        expect(firstPreview.preview_id).toMatch(/^rp_[0-9a-f]{16}$/);
        await expect(
          page.getByRole("region", { name: "Лист героя" }),
        ).toBeVisible();
        await expectFitsPhone(page, "предпросмотр случайного героя");
        await expectNoFeet(
          page.locator("main"),
          "предпросмотр случайного героя",
        );
        // Лист показывает числа сервера как есть.
        expect(await sheetScores(page)).toEqual(
          SCORE_KEYS.map((key) => firstPreview.character.scores[key]),
        );

        const second = await responseTo(
          page,
          "POST",
          "/characters/random-preview",
          () => button(page, "Ещё раз").click(),
        );
        const secondPreview = (await second.json()) as {
          preview_id: string;
          character: HeroJson;
        };
        expect(secondPreview.preview_id).not.toBe(firstPreview.preview_id);
        expect(secondPreview.character.id).not.toBe(firstPreview.character.id);
        await expect
          .poll(() => sheetScores(page))
          .toEqual(
            SCORE_KEYS.map((key) => secondPreview.character.scores[key]),
          );

        // «Беру» присылает только id показанного героя: ни чисел, ни броска.
        const take = await requestOf(page, "POST", "/characters", () =>
          button(page, "Беру").click(),
        );
        expect(take.postDataJSON()).toEqual({
          mode: "random",
          preview_id: secondPreview.preview_id,
        });
        const taken = (await (await take.response())?.json()) as {
          character: HeroJson;
          flavor_error: string | null;
        };
        // Создан именно показанный герой: тот же id и те же числа; имя придумал мастер.
        expect(taken.flavor_error).toBeNull();
        expect(taken.character.id).toBe(secondPreview.character.id);
        expect(taken.character.scores).toEqual(secondPreview.character.scores);
        expect(taken.character.class_id).toBe(secondPreview.character.class_id);
        expect(taken.character.name).toBe(NAME_FROM_MASTER);
        await expect(page.getByText("Ваш герой")).toBeVisible();
        return taken.character;
      });

    await test.step("Случайный герой выбора не хранит: «Изменить» объясняет это, потом быстрый герой", async () => {
      const { page } = friend;
      const stored = await responseTo(page, "GET", "/characters/mine", () =>
        button(page, "Изменить").click(),
      );
      expect(await stored.json()).toMatchObject({
        mode: "random",
        choices: null,
        character: { id: friendHero.id },
      });
      await expect(
        page.getByText(/создан случайно, поэтому прежнего выбора нет/),
      ).toBeVisible();
      await expect(button(page, "Создать заново")).toHaveCount(0);

      // Быстрый герой: класс и раса — листанием карточек, как в подробном создании.
      await button(page, "Быстро").click();
      await pickInCarousel(page, "Класс", "Плут");
      await pickInCarousel(page, "Раса", "Полурослик");
      await page.getByLabel("Опиши героя одной строкой").fill(FRIEND_CONCEPT);
      await expectFitsPhone(page, "форма «Быстро»");
      await expectNoFeet(page.locator("main"), "форма «Быстро»");
      // С карточек ушли id выбранных вариантов: карусель выбирает то же, что прежний список.
      const quick = await requestOf(page, "POST", "/characters", () =>
        button(page, "Создать героя").click(),
      );
      expect(quick.postDataJSON()).toEqual({
        mode: "quick",
        class_id: "rogue",
        species: "halfling",
        concept: FRIEND_CONCEPT,
      });
      await expect(page.getByText("Ваш герой")).toBeVisible();

      // Теперь выбор есть: форма «Быстро» открывается заполненной (имя не давали — пусто),
      // карусели — на прежних классе и расе.
      await button(page, "Изменить").click();
      await button(page, "Изменить").click();
      await expect(page.getByLabel("Опиши героя одной строкой")).toHaveValue(
        FRIEND_CONCEPT,
      );
      await expectPicked(page, "Класс", "Плут");
      await expectPicked(page, "Раса", "Полурослик");
      await expect(page.getByLabel("Имя (необязательно)")).toHaveValue("");
      await button(page, "Назад в лобби").click();
    });

    await test.step("Тайны выбора: чужой выбор и чужой лист не доходят ни по сокету, ни по REST", async () => {
      const wireOf = (player: Player) =>
        [...player.frames, ...player.bodies].join("\n");
      const mineOf = (player: Player) =>
        player.bodies.filter((text) => text.includes('"choices"'));

      // Замысел и имя одного игрока не встречаются ни в одном ответе другому.
      expect(wireOf(friend)).not.toContain("ЗАМЫСЕЛ-ТОРГРИМА");
      expect(wireOf(friend)).not.toContain(HOST_NAME);
      expect(wireOf(host)).not.toContain("ЗАМЫСЕЛ-ПЛУТА");
      expect(wireOf(host)).not.toContain(NAME_FROM_MASTER);
      // Выбор приходит только в ответе `characters/mine` и только свой.
      expect(mineOf(host).length).toBeGreaterThan(0);
      expect(mineOf(friend).length).toBeGreaterThan(0);
      for (const text of mineOf(host)) expect(text).not.toContain("ПЛУТА");
      for (const text of mineOf(friend)) expect(text).not.toContain("ТОРГРИМА");
      // По сокету выбор не ходит вовсе: ни `choices`, ни базовых значений покупки очков.
      for (const player of players) {
        for (const frame of player.frames) {
          expect(frame, `${player.login}: кадр сокета`).not.toMatch(
            /"choices"|"base_scores"|"background_bonus"/,
          );
        }
      }
      // Чужой герой до начала игры не уходит другому игроку. Контроль: свои герои в ответах есть,
      // иначе проверка «чужого нет» ничего бы не доказывала.
      expect(host.frames.length).toBeGreaterThan(0);
      expect(wireOf(host)).toContain(hostHero.id);
      expect(wireOf(friend)).toContain(friendHero.id);
      expect(wireOf(host)).not.toContain(friendHero.id);
      expect(wireOf(friend)).not.toContain(hostHero.id);
    });
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});

test("предпросмотр случайного героя не переживает перезапуск сервера, и экран это объясняет", async ({
  browser,
  baseURL,
  server,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-5");
    players.push(host);
    const { page } = host;
    await expect(page.getByText("Вы вошли как dev-5.")).toBeVisible();
    await button(page, "Создать игру").click();
    await button(page, "Создать").click();
    await expect(page.getByText("Позовите друзей")).toBeVisible();
    await button(page, "Создать персонажа").click();
    await button(page, "Случайно").click();
    await button(page, "Бросить судьбу").click();
    await expect(
      page.getByRole("region", { name: "Лист героя" }),
    ).toBeVisible();

    // Предпросмотры лежат в памяти сервера (решение B8.s0): после перезапуска id неизвестен.
    // Фикстура вешает на выход рабочего процесса страховку «убить сервер»; обработчик остановленного
    // сервера не должен оставаться: он целился бы в группу процессов, номер которой мог занять чужой.
    const exitHandlers = process.listenerCount("exit");
    await server.restart();
    expect(process.listenerCount("exit")).toBe(exitHandlers);
    const take = await requestOf(page, "POST", "/characters", () =>
      button(page, "Беру").click(),
    );
    const refused = await take.response();
    expect(refused?.status()).toBe(409);
    await expect(page.getByRole("alert")).toContainText(
      "Этот герой больше не доступен",
    );
    // Устаревшего героя взять нельзя; основной стала «Ещё раз», и она работает.
    await expect(button(page, "Беру")).toBeDisabled();
    await expect(button(page, "Ещё раз")).toHaveClass(/ui-button--primary/);

    await button(page, "Ещё раз").click();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(button(page, "Беру")).toBeEnabled();
    await button(page, "Беру").click();
    await expect(page.getByText("Ваш герой")).toBeVisible();
    await expect(page.getByRole("switch", { name: "Готов" })).toBeEnabled();
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});

test("подробный волшебник: заклинания из настоящих правил сервера и возврат к ним при правке", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-6");
    players.push(host);
    const { page } = host;
    await expect(page.getByText("Вы вошли как dev-6.")).toBeVisible();
    await button(page, "Создать игру").click();
    await button(page, "Создать").click();
    await expect(page.getByText("Позовите друзей")).toBeVisible();
    const options = await openCreation(page);
    const casting = byId(options.classes, "wizard").spellcasting;
    if (casting === null) throw new Error("у волшебника на сервере нет магии");
    await button(page, "Подробно").click();

    await pickInCarousel(page, "Раса", "Эльф");
    await button(page, "Далее").click();
    await expect(progress(page)).toHaveText("Шаг 2 из 10: Класс");
    await pickInCarousel(page, "Класс", "Волшебник");
    // У заклинателя в мастере появляется шаг «Заклинания».
    await expect(progress(page)).toHaveText("Шаг 2 из 11: Класс");
    await button(page, "Далее").click();
    await pickInCarousel(page, "Предыстория", "Мудрец");
    await button(page, "Далее").click();

    // Ключевая характеристика волшебника — Интеллект: сервер даёт ему больше всех.
    await button(page, "Распределить случайно").click();
    await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
    const scores = await pointBuyScores(page);
    expect(scores["Интеллект"]).toBe(Math.max(...Object.values(scores)));
    expect(scores["Интеллект"]).toBeGreaterThanOrEqual(14);
    await button(page, "Далее").click();

    await page.getByLabel("Бонус +2", { exact: true }).selectOption("int");
    await page.getByLabel("Бонус +1", { exact: true }).selectOption("con");
    await button(page, "Далее").click();

    // Мудрец уже владеет магией и историей: из навыков класса остаются остальные.
    const skills = page.getByRole("group", { name: /Навыки класса/ });
    await expect(skills.getByRole("checkbox")).toHaveCount(5);
    await skills.getByRole("checkbox").nth(0).check({ force: true });
    await skills.getByRole("checkbox").nth(1).check({ force: true });
    await button(page, "Далее").click();

    // Шаг заклинаний строится из данных сервера: три заговора и шесть заклинаний. У каждого
    // варианта «Подробнее» раскрывает карточку: школа, время, дистанция в метрах, длительность.
    await expect(progress(page)).toHaveText("Шаг 7 из 11: Заклинания");
    const cantrips = page.getByRole("group", { name: "Заговоры: выберите 3" });
    const spells = page.getByRole("group", { name: "Заклинания: выберите 6" });
    await expect(button(page, "Далее")).toBeDisabled();
    await expectAllSpellCards(cantrips, casting.cantrip_options);
    await expectAllSpellCards(spells, casting.spell_options);
    await expectNoFeet(
      page.locator("main"),
      "шаг «Заклинания» с раскрытыми карточками",
    );
    for (let index = 0; index < 3; index += 1) {
      await cantrips.getByRole("checkbox").nth(index).check({ force: true });
    }
    // Набрав норму, остальные заговоры нельзя отметить.
    await expect(cantrips.getByRole("checkbox").nth(3)).toBeDisabled();
    for (let index = 0; index < 6; index += 1) {
      await spells.getByRole("checkbox").nth(index).check({ force: true });
    }
    await expectFitsPhone(page, "шаг «Заклинания» с раскрытыми карточками");
    const chosenCantrips = await checkedValues(cantrips);
    const chosenSpells = await checkedValues(spells);
    expect(chosenCantrips).toHaveLength(3);
    expect(chosenSpells).toHaveLength(6);
    // В «Итоге» заклинания названы так же, как в правилах сервера.
    const spellLines = [
      `Заговоры: ${chosenCantrips.map((id) => byId(casting.cantrip_options, id).name).join(", ")}`,
      `Заклинания: ${chosenSpells.map((id) => byId(casting.spell_options, id).name).join(", ")}`,
    ];
    await button(page, "Далее").click(); // снаряжение
    await button(page, "Далее").click(); // характер
    await page.getByLabel("Имя", { exact: true }).fill("Мирель");
    await button(page, "Далее").click(); // история
    await button(page, "Далее").click(); // итог
    await expect(progress(page)).toHaveText("Шаг 11 из 11: Итог");
    await expect(summaryLines(page, "Заклинания")).toHaveText(spellLines);

    const sent = await requestOf(page, "POST", "/characters", () =>
      button(page, "Создать героя").click(),
    );
    const body = sent.postDataJSON() as { choices: Record<string, unknown> };
    expect(body.choices).toMatchObject({
      class_id: "wizard",
      cantrips: chosenCantrips,
      prepared_spells: chosenSpells,
    });
    await expect(page.getByText("Ваш герой")).toBeVisible();

    // «Изменить героя»: мастер открывается на «Итоге», выбранные заклинания вернулись в форму из
    // ответа `characters/mine`; «Изменить» у раздела ведёт на шаг заклинаний.
    await button(page, "Изменить").click();
    await button(page, "Изменить").click();
    await expect(progress(page)).toHaveText("Шаг 11 из 11: Итог");
    await expect(summaryLines(page, "Заклинания")).toHaveText(spellLines);
    await editSection(page, "Заклинания");
    await expect(progress(page)).toHaveText("Шаг 7 из 11: Заклинания");
    expect(
      await checkedValues(
        page.getByRole("group", { name: "Заговоры: выберите 3" }),
      ),
    ).toEqual(chosenCantrips);
    expect(
      await checkedValues(
        page.getByRole("group", { name: "Заклинания: выберите 6" }),
      ),
    ).toEqual(chosenSpells);
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});

// Финальное покрытие блока 7: то, чего не проходят герои выше, — навык на выбор человека, боевой
// стиль с «Подробнее» (`fighting_styles[].details` сервера) и заклинатель без заговоров.
test("подробный следопыт-человек: навык на выбор, «Подробнее» у боевых стилей и заклинания без заговоров", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    // dev-3 и dev-4 нельзя: lobby-membership ждёт, что у них ещё нет игр (база стенда общая).
    const host = await openPlayer(browser, baseURL, "dev-6");
    players.push(host);
    const { page } = host;
    await expect(page.getByText("Вы вошли как dev-6.")).toBeVisible();
    await button(page, "Создать игру").click();
    await button(page, "Создать").click();
    await expect(page.getByText("Позовите друзей")).toBeVisible();
    const options = await openCreation(page);
    const ranger = byId(options.classes, "ranger");
    const soldier = byId(options.backgrounds, "soldier");
    const casting = ranger.spellcasting;
    if (casting === null) throw new Error("у следопыта на сервере нет магии");
    expect(casting.cantrip_options, "заговоров у следопыта нет").toEqual([]);
    await button(page, "Подробно").click();

    await pickInCarousel(page, "Раса", "Человек");
    await button(page, "Далее").click();
    await pickInCarousel(page, "Класс", ranger.name);
    // Заклинатель и без заговоров: шаг «Заклинания» в мастере есть.
    await expect(progress(page)).toHaveText("Шаг 2 из 11: Класс");
    await button(page, "Далее").click();
    await pickInCarousel(page, "Предыстория", soldier.name);
    await button(page, "Далее").click();

    await button(page, "Распределить случайно").click();
    await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
    await button(page, "Далее").click();
    await page.getByLabel("Бонус +2", { exact: true }).selectOption("dex");
    await page.getByLabel("Бонус +1", { exact: true }).selectOption("con");
    await button(page, "Далее").click();

    // Навыки: три из списка следопыта без навыков солдата; человеку — ещё навык на выбор.
    await expect(progress(page)).toHaveText(
      "Шаг 6 из 11: Навыки и выборы класса",
    );
    const skills = page.getByRole("group", { name: /Навыки класса/ });
    const offered = ranger.skill_options.filter(
      (id) => !soldier.skills.includes(id),
    );
    await expect(skills.getByRole("checkbox")).toHaveCount(offered.length);
    for (let index = 0; index < 3; index += 1) {
      await skills.getByRole("checkbox").nth(index).check({ force: true });
    }
    await page
      .getByLabel("Навык на выбор (Умелость)", { exact: true })
      .selectOption("medicine");

    // Боевой стиль: у каждого «Подробнее» раскрывает карточку с описанием сервера, выбор не меняя.
    const styles = page.getByRole("radiogroup", { name: "Боевой стиль" });
    await expect(styles.getByRole("radio")).toHaveCount(
      ranger.fighting_styles.length,
    );
    for (const id of ranger.fighting_styles) {
      const style = byId(options.fighting_styles, id);
      const more = styles.getByRole("button", {
        name: `Подробнее: ${style.name}`,
        exact: true,
      });
      await expect(more).toHaveAttribute("aria-expanded", "false");
      await more.click();
      await expect(more).toHaveAttribute("aria-expanded", "true");
      const card = styles.getByRole("article", {
        name: style.name,
        exact: true,
      });
      await expect(card).toContainText(style.details.summary);
      await expectNoFeet(card, `карточка стиля «${style.name}»`);
    }
    await expect(styles.getByRole("radio", { checked: true })).toHaveCount(0);
    const archery = byId(options.fighting_styles, "archery");
    await styles
      .getByRole("radio", { name: new RegExp(`^${archery.name}`) })
      .check({ force: true });
    await expectFitsPhone(
      page,
      "шаг «Навыки» с раскрытыми карточками боевых стилей",
    );
    await button(page, "Далее").click();

    // Заклинания: группы заговоров нет, у каждого заклинания — карточка с данными сервера.
    await expect(progress(page)).toHaveText("Шаг 7 из 11: Заклинания");
    await expect(page.getByRole("group", { name: /^Заговоры/ })).toHaveCount(0);
    const spells = page.getByRole("group", { name: /^Заклинания: выберите / });
    await expectAllSpellCards(spells, casting.spell_options);
    await expectNoFeet(page.locator("main"), "шаг «Заклинания» следопыта");
    const prepared = Number(
      /выберите (\d+)/.exec(await spells.locator("legend").innerText())?.[1],
    );
    expect(prepared, "заклинаний на выбор").toBeGreaterThan(0);
    for (let index = 0; index < prepared; index += 1) {
      await spells.getByRole("checkbox").nth(index).check({ force: true });
    }
    await expectFitsPhone(page, "шаг «Заклинания» следопыта");
    const chosenSpells = await checkedValues(spells);
    await button(page, "Далее").click(); // снаряжение
    await button(page, "Далее").click(); // характер
    await page.getByLabel("Имя", { exact: true }).fill("Ярина");
    await button(page, "Далее").click(); // история
    await button(page, "Далее").click(); // итог

    await expect(progress(page)).toHaveText("Шаг 11 из 11: Итог");
    const skillLines = summaryLines(page, "Навыки и выборы класса");
    await expect(
      skillLines.filter({ hasText: /^Навык на выбор: / }),
    ).toHaveText(`Навык на выбор: ${byId(options.skills, "medicine").name}`);
    await expect(skillLines.filter({ hasText: /^Боевой стиль: / })).toHaveText(
      `Боевой стиль: ${archery.name}`,
    );
    await expect(summaryLines(page, "Заклинания")).toHaveText([
      `Заклинания: ${chosenSpells.map((id) => byId(casting.spell_options, id).name).join(", ")}`,
    ]);
    await expect(page.getByText("Выбор на этом шаге не закончен.")).toHaveCount(
      0,
    );

    const sent = await requestOf(page, "POST", "/characters", () =>
      button(page, "Создать героя").click(),
    );
    expect(sent.postDataJSON()).toMatchObject({
      mode: "detailed",
      choices: {
        species: "human",
        class_id: "ranger",
        background: "soldier",
        extra_skill: "medicine",
        fighting_style: "archery",
        cantrips: [],
        prepared_spells: chosenSpells,
      },
    });
    expect((await sent.response())?.status()).toBe(201);
    await expect(page.getByText("Ваш герой")).toBeVisible();
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});
