import fs from "node:fs";
import path from "node:path";
import type { Browser, BrowserContext, Page } from "@playwright/test";
import { pickQuickHero } from "./hero.ts";
import { expectFitsPhone } from "./layout.ts";
import { expect, REPO_ROOT, SCENARIOS_DIR, test } from "./server.ts";

// Сквозной тест привалов (DND-2, блок B9): трое игроков на телефонах проходят короткий и долгий
// привал, которые хост начинает кнопкой, без хода мастера. Сервер настоящий (dev, `FakeProvider`
// по `e2e_rests.yaml`, своя чистая база), кость хитов бросает сервер (`SystemRng`). Всё идёт
// через экраны Mini App, а провод (кадры сокета и ответы REST) проверяется отдельно: личная идея
// для рассказа не должна уйти чужому игроку даже невидимыми данными.

const SCENARIO = "e2e_rests.yaml";
const SCENARIO_FILE = path.join(REPO_ROOT, SCENARIOS_DIR, SCENARIO);

// Свой сценарий — свой рабочий процесс Playwright: свой сервер и чистая база (см. `server.ts`).
test.use({ scenario: SCENARIO });

interface Hero {
  login: string;
  name: string;
  classId: string;
  species: string;
  concept: string;
}

// Порядок входа в игру — порядок списка: герои становятся `$pc1…$pc3` сценария. Алису (хост)
// задевает черепица в открытии: ей есть что лечить костью хитов, и она в фокусе рассказа.
const HEROES: readonly Hero[] = [
  {
    login: "dev-1",
    name: "Алиса",
    classId: "fighter",
    species: "human",
    concept: "бывшая стражница, которая не умеет врать",
  },
  {
    login: "dev-2",
    name: "Борис",
    classId: "rogue",
    species: "halfling",
    concept: "карманник с добрым сердцем",
  },
  {
    login: "dev-3",
    name: "Вера",
    classId: "ranger",
    species: "elf",
    concept: "молчаливая охотница, ищущая пропавшего брата",
  },
];

const CAMP_LINE = "Подброшу веток и спою, чтобы не так тянуло сыростью.";
const HIT_DIE_LABEL = "Кость хитов";

interface Player {
  hero: Hero;
  context: BrowserContext;
  page: Page;
  /** Кадры сокета, которые сервер прислал странице. */
  frames: string[];
  /** Тела ответов REST (`/api/…`), которые сервер прислал странице. */
  bodies: string[];
}

// --- сценарий мастера ---

/**
 * Идеи ответов `campfire.ideas` сценария по порядку привалов: в каждом ответе текст на каждого
 * героя по его номеру (`$pc1` — индекс 0). Единственный источник текстов — сам сценарий.
 */
function scenarioIdeas(): string[][] {
  const text = fs.readFileSync(SCENARIO_FILE, "utf8");
  const block = text.slice(text.indexOf("campfire.ideas:"));
  const idea = /character_id: "\$pc(\d)", text: "([^"]+)"/g;
  return block
    .split("- ideas:")
    .slice(1)
    .map((answer) => {
      const ideas: string[] = [];
      for (const match of answer.matchAll(idea)) {
        ideas[Number(match[1]) - 1] = match[2];
      }
      return ideas;
    });
}

// --- экраны ---

// Точное имя у строки; у регулярного выражения Playwright `exact` не учитывает.
const button = (page: Page, name: string | RegExp) =>
  page.getByRole("button", { name, exact: true });
const tabOf = (page: Page, name: string) =>
  page.getByRole("tab", { name, exact: true });
const feedOf = (page: Page) => page.getByRole("list", { name: "Лента" });
const campOf = (page: Page) =>
  page.getByRole("tabpanel", { name: "Привал", exact: true });
const ideaCard = (page: Page) =>
  campOf(page).getByRole("region", { name: "Идея для рассказа" });
const hitDieButton = (page: Page) =>
  campOf(page).getByRole("button", { name: /^Потратить кость хитов/ });
const rollCard = (page: Page, heroName: string) =>
  feedOf(page).getByRole("article", {
    name: new RegExp(`^${heroName}\\. ${HIT_DIE_LABEL}:`),
  });
// Кнопки хоста называют срок привала из настроек игры: «Короткий привал (20 сек)».
const SHORT_REST_BUTTON = /^Короткий привал \(/;
const LONG_REST_BUTTON = /^Долгий привал \(/;
// Адресаты идей короткого привала (индексы HEROES): двое дольше всех вне фокуса рассказа, а
// Алису черепица в открытии поставила в фокус.
const SHORT_REST_ADDRESSEES = new Set([1, 2]);

/** Окно игрока: свой контекст браузера на ширине телефона, как отдельный телефон. */
async function openPlayer(
  browser: Browser,
  baseURL: string,
  hero: Hero,
): Promise<Player> {
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    colorScheme: "dark",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  // Что сервер прислал странице, видно только на проводе: DOM показывает лишь часть.
  const frames: string[] = [];
  const bodies: string[] = [];
  page.on("websocket", (socket) => {
    socket.on("framereceived", (frame) => frames.push(String(frame.payload)));
  });
  page.on("response", (response) => {
    if (!new URL(response.url()).pathname.startsWith("/api/")) return;
    // Тело читается, когда ответ готов; сбой чтения (закрытая страница) не важен.
    void response
      .text()
      .then((text) => bodies.push(text))
      .catch(() => {});
  });
  await page.goto(`/?dev=${hero.login}`);
  await expect(page.getByText(`Вы вошли как ${hero.login}.`)).toBeVisible();
  return { hero, context, page, frames, bodies };
}

/** Быстрый герой: класс и раса каруселями, строка о герое и имя. */
async function createQuickHero(player: Player): Promise<void> {
  const { page, hero } = player;
  await button(page, "Создать персонажа").click();
  await button(page, "Быстро").click();
  await pickQuickHero(page, hero);
  await page.getByLabel("Опиши героя одной строкой").fill(hero.concept);
  await page.getByLabel("Имя (необязательно)").fill(hero.name);
  await button(page, "Создать героя").click();
  const ready = page.getByRole("switch", { name: "Готов" });
  await expect(ready).toBeEnabled();
  // Отметка приходит от сервера (событие лобби), поэтому проверяется после щелчка.
  await ready.click();
  await expect(ready).toBeChecked();
}

/** Здоровье героя на вкладке «Отряд»: текущее и наибольшее; затем снова «Рассказ». */
async function hpOf(page: Page, heroName: string) {
  await tabOf(page, "Отряд").click();
  const meter = page.getByRole("meter", { name: `Здоровье: ${heroName}` });
  await expect(meter).toBeVisible();
  const now = Number(await meter.getAttribute("aria-valuenow"));
  const max = Number(await meter.getAttribute("aria-valuemax"));
  await tabOf(page, "Рассказ").click();
  return { now, max };
}

/** Хост начинает привал: кнопка вида, вопрос «Начать привал?» и «Да, начать привал». */
async function startRest(
  host: Player,
  kind: RegExp,
  layoutName?: string,
): Promise<void> {
  await button(host.page, kind).click();
  const question = host.page.getByRole("group", { name: "Начать привал?" });
  // Вопрос с пояснением и двумя кнопками тоже должен помещаться на телефоне (финальное
  // покрытие B9): его меряют до ответа, пока привал ещё не начался и отсчёт не идёт.
  if (layoutName !== undefined) {
    await expect(question).toBeVisible();
    await expectFitsPhone(host.page, layoutName);
  }
  await question.getByRole("button", { name: "Да, начать привал" }).click();
}

/** У всех вкладка «Привал» и отсчёт; экран привала открыт и называет вид привала. */
async function expectCampOpen(players: Player[], title: string) {
  for (const { page } of players) {
    await expect(tabOf(page, "Привал")).toBeVisible();
    await expect(
      page.getByRole("timer", { name: /До конца привала/ }),
    ).toBeVisible();
    await tabOf(page, "Привал").click();
    await expect(
      campOf(page).getByRole("region", { name: title, exact: true }),
    ).toBeVisible();
  }
}

/** Привал кончился: рассказ мастера и строка отдыха в ленте, вкладки «Привал» нет. */
async function expectCampClosed(
  players: Player[],
  story: RegExp,
  restLine: string,
) {
  for (const { page } of players) {
    await expect(feedOf(page).getByText(story)).toBeVisible({
      timeout: 60_000,
    });
    await expect(feedOf(page).getByText(restLine)).toBeVisible();
    await expect(tabOf(page, "Привал")).toHaveCount(0);
    await expect(page.getByText("Обсуждение", { exact: true })).toBeVisible();
  }
}

// --- провод ---

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Кадры сокета как объекты; служебные (не JSON) пропускаются. */
function framesOf(player: Player): JsonObject[] {
  const parsed: JsonObject[] = [];
  for (const frame of player.frames) {
    try {
      const value: unknown = JSON.parse(frame);
      if (isObject(value)) parsed.push(value);
    } catch {
      // не JSON: не наш случай
    }
  }
  return parsed;
}

/** Всё, что сервер прислал игроку: кадры сокета и ответы REST. */
const wireOf = (player: Player) =>
  [...player.frames, ...player.bodies].join("\n");

/** Тексты кадров `rest_idea`, которые получил игрок, по порядку. */
const ideaFrames = (player: Player) =>
  framesOf(player)
    .filter((frame) => frame.type === "rest_idea")
    .map((frame) => frame.text);

/** Виды привалов из кадров `rest_started` или `rest_ended`, по порядку. */
const restFrames = (player: Player, type: "rest_started" | "rest_ended") =>
  framesOf(player)
    .filter((frame) => frame.type === type)
    .map((frame) => frame.kind);

/** Фаза из последнего снимка или кадра `phase`: так её видит страница. */
function lastPhase(player: Player): JsonObject {
  for (const frame of framesOf(player).reverse()) {
    if (frame.type !== "phase" && frame.type !== "snapshot") continue;
    if (isObject(frame.phase)) return frame.phase;
  }
  throw new Error(`${player.hero.login}: фазы в кадрах нет`);
}

/** Время игры: день и часть суток. */
interface GameTime {
  day: unknown;
  part: unknown;
}

/** Время игры в последней фазе. */
function sceneOf(player: Player): GameTime {
  const scene = lastPhase(player).scene;
  if (!isObject(scene)) throw new Error("сцены в фазе нет");
  return { day: scene.day, part: scene.part };
}

/** Срок идущего привала в последней фазе, мс; `null` — сейчас не привал или срока нет. */
function campDeadline(player: Player): number | null {
  const phase = lastPhase(player);
  if (phase.turn_phase !== "camp" || typeof phase.deadline_at !== "string") {
    return null;
  }
  return Date.parse(phase.deadline_at);
}

/** Лист героя игрока в последнем снимке или кадре `character`. */
function lastSheet(player: Player): JsonObject {
  for (const frame of framesOf(player).reverse()) {
    if (frame.type !== "snapshot" && frame.type !== "character") continue;
    if (isObject(frame.character)) return frame.character;
  }
  throw new Error(`${player.hero.login}: листа героя в кадрах нет`);
}

/** Итог броска «Кость хитов» в последней карточке ленты, что пришла игроку; `null` — её нет. */
function hitDieTotal(player: Player): number | null {
  for (const frame of framesOf(player).reverse()) {
    const item = frame.type === "feed_item" ? frame.item : undefined;
    if (!isObject(item) || item.kind !== "roll" || !isObject(item.data)) {
      continue;
    }
    if (item.data.label === HIT_DIE_LABEL) return Number(item.data.total);
  }
  return null;
}

test("привалы по кнопке хоста: короткий с костью хитов и идеей, долгий до утра", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const [shortIdeas, longIdeas] = scenarioIdeas();
  for (const ideas of [shortIdeas, longIdeas]) {
    expect(ideas, "идеи сценария на каждого героя").toHaveLength(HEROES.length);
  }

  const players: Player[] = [];
  const pageErrors: string[] = [];
  try {
    await test.step("1. Хост создаёт игру, двое входят по коду, все создают героев и начинают", async () => {
      for (const hero of HEROES) {
        const player = await openPlayer(browser, baseURL, hero);
        player.page.on("pageerror", (error) => pageErrors.push(error.message));
        players.push(player);
      }
      const [host, ...friends] = players;
      await button(host.page, "Создать игру").click();
      await expect(
        host.page.getByRole("radio", { name: /Склеп должника/ }),
      ).toBeChecked();
      await button(host.page, "Создать").click();
      const invite = await host.page
        .getByText(/Код приглашения:/)
        .textContent();
      const code = /Код приглашения:\s*([A-Z0-9]{6})/.exec(invite ?? "")?.[1];
      expect(code, "код приглашения в лобби").toHaveLength(6);
      // Входят по очереди: порядок входа задаёт, чьи id стоят в сценарии как $pc1…$pc3.
      for (const [index, friend] of friends.entries()) {
        await friend.page.getByLabel("Код приглашения").fill(code ?? "");
        await button(friend.page, "Войти").click();
        await expect(friend.page.getByText("Ваш герой")).toBeVisible();
        await expect(
          host.page.getByText(`Игроки (${index + 2})`),
        ).toBeVisible();
      }
      await Promise.all(players.map(createQuickHero));
      await expect(button(host.page, "Начать")).toBeEnabled();
      await button(host.page, "Начать").click();
    });

    const [alisa, boris, vera] = players;
    const friends = [boris, vera];
    let wounded = { now: 0, max: 0 };
    let sceneBefore: GameTime = { day: null, part: null };
    await test.step("2. Открытие: черепица задела Алису; кнопки привалов только у хоста", async () => {
      for (const { page } of players) {
        await expect(
          feedOf(page).getByText(/Сумерки над Мшистым Бродом/),
        ).toBeVisible();
        await expect(
          page.getByText("Обсуждение", { exact: true }),
        ).toBeVisible();
      }
      wounded = await hpOf(alisa.page, alisa.hero.name);
      expect(wounded.now, "хиты Алисы после черепицы").toBeLessThan(
        wounded.max,
      );
      sceneBefore = sceneOf(alisa);

      await expect(button(alisa.page, SHORT_REST_BUTTON)).toBeVisible();
      await expect(button(alisa.page, LONG_REST_BUTTON)).toBeVisible();
      for (const { page } of friends) {
        await expect(button(page, SHORT_REST_BUTTON)).toHaveCount(0);
        await expect(button(page, LONG_REST_BUTTON)).toHaveCount(0);
      }
      // Панель хоста в обсуждении с двумя новыми кнопками привалов (финальное покрытие B9).
      await expectFitsPhone(alisa.page, "обсуждение: кнопки привалов у хоста");
    });

    await test.step("3. Короткий привал по кнопке хоста: у всех экран привала, «+1 мин» сдвигает срок", async () => {
      await startRest(
        alisa,
        SHORT_REST_BUTTON,
        "вопрос «Начать привал?» у хоста",
      );
      for (const player of players) {
        await expect
          .poll(() => restFrames(player, "rest_started"))
          .toEqual(["short"]);
      }
      // Борис идёт к костру с «Рассказа», остальные — вкладкой.
      const sideLink = boris.page.getByRole("region", {
        name: "Короткий привал",
        exact: true,
      });
      await sideLink.getByRole("button", { name: "К костру" }).click();
      await expect(tabOf(boris.page, "Привал")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expectCampOpen(players, "Короткий привал");

      await expect.poll(() => campDeadline(alisa)).not.toBeNull();
      const deadline = campDeadline(alisa) ?? 0;
      await button(alisa.page, "+1 мин").click();
      await expect
        .poll(() => (campDeadline(alisa) ?? 0) - deadline)
        .toBe(60_000);
    });

    await test.step("4. Идея для рассказа только адресатам: Борису и Вере, Алиса в фокусе — без идеи", async () => {
      const [, borisIdea, veraIdea] = shortIdeas;
      await expect(ideaCard(boris.page)).toContainText(borisIdea);
      await expect(ideaCard(vera.page)).toContainText(veraIdea);
      for (const { page } of friends) {
        await expect(
          ideaCard(page).getByRole("note", { name: "Только тебе" }),
        ).toBeVisible();
      }
      // Идеи адресатам уходят вместе, под одной блокировкой: Алисе уже нечего ждать.
      await expect(ideaCard(alisa.page)).toHaveCount(0);

      expect(ideaFrames(alisa), "кадры rest_idea Алисы").toEqual([]);
      expect(ideaFrames(boris), "кадры rest_idea Бориса").toEqual([borisIdea]);
      expect(ideaFrames(vera), "кадры rest_idea Веры").toEqual([veraIdea]);
      // Чужой идеи нет нигде; идею Алисы сервер не отдал никому, и ей самой тоже.
      for (const [index, player] of players.entries()) {
        const html = await player.page.content();
        const wire = wireOf(player);
        for (const [other, idea] of shortIdeas.entries()) {
          if (other === index && SHORT_REST_ADDRESSEES.has(other)) continue;
          const whose = `${player.hero.login}: идея ${HEROES[other].login}`;
          expect(html, `${whose} на экране`).not.toContain(idea);
          expect(wire, `${whose} на проводе`).not.toContain(idea);
        }
      }
    });

    let hitDiceBefore = 0;
    await test.step("5. Алиса тратит кость хитов: бросает сервер, карточку видят все, остаток меньше, хиты больше", async () => {
      const camp = campOf(alisa.page);
      const hpLine = (hp: number, max: number) =>
        camp.getByText(`Хиты: ${hp} из ${max}.`, { exact: true });
      await expect(hpLine(wounded.now, wounded.max)).toBeVisible();
      const label = (await hitDieButton(alisa.page).textContent()) ?? "";
      hitDiceBefore = Number(/осталось (\d+)/.exec(label)?.[1]);
      expect(hitDiceBefore, "кости хитов Алисы").toBeGreaterThan(0);

      const answer = alisa.page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith("/rest/hit-dice"),
      );
      await hitDieButton(alisa.page).click();
      const response = await answer;
      expect(response.status(), "ответ на кость хитов").toBe(200);
      const spent = (await response.json()) as {
        hit_dice: number;
        hp: number;
        hp_max: number;
      };
      expect(spent.hit_dice).toBe(hitDiceBefore - 1);
      expect(spent.hp_max).toBe(wounded.max);

      // Лечит итог броска сервера, но не меньше 1 и не выше максимума: так считает движок.
      await expect.poll(() => hitDieTotal(alisa)).not.toBeNull();
      const total = hitDieTotal(alisa) ?? 0;
      const healed = Math.min(wounded.max, wounded.now + Math.max(1, total));
      expect(spent.hp, "хиты после кости").toBe(healed);
      expect(spent.hp).toBeGreaterThan(wounded.now);
      await expect(hitDieButton(alisa.page)).toHaveAccessibleName(
        `Потратить кость хитов (осталось ${spent.hit_dice})`,
      );
      await expect(hpLine(spent.hp, spent.hp_max)).toBeVisible();
      // Бросок для всех: одна карточка в ленте у каждого, у друзей — и новые хиты Алисы в «Отряде».
      for (const player of players) {
        const { page } = player;
        await tabOf(page, "Рассказ").click();
        await expect(rollCard(page, alisa.hero.name)).toHaveCount(1);
        await expect(rollCard(page, alisa.hero.name)).toBeVisible();
        if (player !== alisa) {
          const hp = await hpOf(page, alisa.hero.name);
          expect(hp, `${player.hero.login}: хиты Алисы`).toEqual({
            now: spent.hp,
            max: spent.hp_max,
          });
        }
        await tabOf(page, "Привал").click();
      }
    });

    await test.step("6. Реплика у костра: Вера говорит, слышат все; экран привала помещается на телефоне", async () => {
      const camp = campOf(vera.page);
      await camp.getByLabel("Сказать у костра").fill(CAMP_LINE);
      await button(vera.page, "Сказать").click();
      for (const { page } of players) {
        await expect(
          campOf(page).getByRole("list", { name: "Реплики у костра" }),
        ).toContainText(CAMP_LINE);
      }
      await expectFitsPhone(alisa.page, "короткий привал: хост, кость хитов");
      await expectFitsPhone(boris.page, "короткий привал: идея для рассказа");
    });

    await test.step("7. Хост заканчивает короткий привал: рассказ мастера, привал закрыт, время то же", async () => {
      await button(alisa.page, "Закончить привал").click();
      await expectCampClosed(
        players,
        /Прошёл час/,
        "Короткий отдых: отряд перевёл дух.",
      );
      for (const player of players) {
        await expect
          .poll(() => restFrames(player, "rest_ended"))
          .toEqual(["short"]);
        // Короткий привал время суток не сдвигает: о часе отдыха говорит только мастер.
        expect(sceneOf(player), `${player.hero.login}: время игры`).toEqual(
          sceneBefore,
        );
      }
    });

    await test.step("8. Долгий привал по кнопке хоста: идея каждому, кости хитов не тратят", async () => {
      await startRest(alisa, LONG_REST_BUTTON);
      for (const player of players) {
        await expect
          .poll(() => restFrames(player, "rest_started"))
          .toEqual(["short", "long"]);
      }
      await expectCampOpen(players, "Долгий привал");
      await expect(hitDieButton(alisa.page)).toHaveCount(0);
      // Экран долгого привала у хоста: вид привала, идея и панель хоста (финальное покрытие B9).
      await expect(ideaCard(alisa.page)).toBeVisible();
      await expectFitsPhone(
        alisa.page,
        "долгий привал: хост, идея для рассказа",
      );

      // На долгом привале идею получает каждый, и только свою; идея прошлого привала ушла.
      for (const [index, player] of players.entries()) {
        await expect(ideaCard(player.page)).toContainText(longIdeas[index]);
        await expect(ideaCard(player.page)).not.toContainText(
          shortIdeas[index],
        );
      }
      for (const [index, player] of players.entries()) {
        const html = await player.page.content();
        const wire = wireOf(player);
        expect(ideaFrames(player).at(-1)).toBe(longIdeas[index]);
        for (const [other, idea] of longIdeas.entries()) {
          if (other === index) continue;
          const whose = `${player.hero.login}: идея ${HEROES[other].login}`;
          expect(html, `${whose} на экране`).not.toContain(idea);
          expect(wire, `${whose} на проводе`).not.toContain(idea);
        }
      }
    });

    await test.step("9. По отсчёту наступает утро следующего дня: рассказ, длинный отдых, кости хитов вернулись", async () => {
      await expectCampClosed(
        players,
        /Утро нового дня/,
        "Длинный отдых: отряд восстановил силы.",
      );
      for (const player of players) {
        await expect
          .poll(() => restFrames(player, "rest_ended"))
          .toEqual(["short", "long"]);
        expect(sceneOf(player), `${player.hero.login}: время игры`).toEqual({
          day: Number(sceneBefore.day) + 1,
          part: "morning",
        });
      }
      // Лист Алисы приходит её странице кадром после хода «конец привала».
      await expect
        .poll(() => lastSheet(alisa).hit_dice, { message: "кости хитов утром" })
        .toBe(hitDiceBefore);
      const sheet = lastSheet(alisa);
      expect(sheet.hp, "хиты Алисы утром").toBe(sheet.hp_max);
      await tabOf(boris.page, "Отряд").click();
      const meters = boris.page.getByRole("meter");
      await expect(meters).toHaveCount(HEROES.length);
      for (const meter of await meters.all()) {
        expect(await meter.getAttribute("aria-valuenow")).toBe(
          await meter.getAttribute("aria-valuemax"),
        );
      }
    });

    expect(pageErrors, "необработанные ошибки страниц").toEqual([]);
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});
