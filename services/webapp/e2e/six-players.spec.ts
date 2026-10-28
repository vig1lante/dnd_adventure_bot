import fs from "node:fs";
import path from "node:path";
import type { Browser, BrowserContext, Locator, Page } from "@playwright/test";
import { className, expectPicked, pickQuickHero } from "./hero.ts";
import { expectFitsPhone } from "./layout.ts";
import { artifactsDir, expect, test } from "./server.ts";

// Сквозной сценарий «6 игроков» (план B8.7): шесть окон браузера, как шестеро друзей за столом,
// проходят вечер от создания игры до утра после привала и перезапуска сервера. Сервер настоящий
// (dev, `FakeProvider` по `e2e_full.yaml`, временная база), кубы настоящие. Тест ничего не
// подделывает: всё идёт через экраны Mini App, а входят игроки по `?dev=dev-N`.

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
const SCENARIO_FILE = path.join(
  REPO_ROOT,
  "services/server/tests/scenarios/e2e_full.yaml",
);

interface Hero {
  login: string;
  name: string;
  classId: string;
  species: string;
  concept: string;
}

// Имена героев задают игроки сами: тогда рассказ и «Ходит …» называют их предсказуемо.
// Порядок входа в игру (а значит, `$pc1…$pc6` сценария) — порядок этого списка.
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
  {
    login: "dev-4",
    name: "Глеб",
    classId: "cleric",
    species: "dwarf",
    concept: "бродячий лекарь с тяжёлой булавой",
  },
  {
    login: "dev-5",
    name: "Дина",
    classId: "wizard",
    species: "gnome",
    concept: "ученица, сбежавшая из башни с чужой книгой",
  },
  {
    login: "dev-6",
    name: "Егор",
    classId: "barbarian",
    species: "orc",
    concept: "силач, который боится кладбищ",
  },
];

// Тексты, которые должны дойти строго до своего адресата. Метки нарочно необычные.
const SECRET_ACTION = "Тихо подменить письмо в сумке стражника КРАСНЫЙ-ФОНАРЬ";
const DRAFT_ACTION = "Осмотреть доску объявлений у входа в трактир";
const CAMP_LINE = "Подкину дров и расскажу, как мы выросли у воды.";

const COMPOSER = "Что делает твой персонаж?";

// Дневник отряда (блок B8 DND-2). Разделы по порядку: раздел записи → подпись вкладки.
const JOURNAL_SECTIONS = [
  ["main", "Главное"],
  ["places", "Места"],
  ["people", "Люди"],
  ["gods", "Боги"],
  ["factions", "Фракции"],
  ["world", "Мир"],
  ["rumors", "Слухи"],
  ["clues", "Улики"],
  ["items", "Предметы"],
] as const;
// Стартовый дневник «Склепа должника»: завязка, всё, что отряд знает с начала игры, и деревня,
// где отряд стоит. Названия и тексты записей тест берёт из снимка, а не повторяет здесь.
const START_JOURNAL_REFS = [
  "intro",
  "location:cemetery",
  "location:village",
  "npc:marta",
  "deity:ossmar",
  "deity:veyla",
  "region:marshland",
  "region:barrow-hill",
  "rumor:1",
];
const JOURNAL_INTRO = /Пропал смотритель кладбища Ульрих/;
// Поля записи дневника по контракту: ничего сверх них (тайн сущности, заметок мастера) нет.
const JOURNAL_ENTRY_KEYS = [
  "audience",
  "id",
  "ref",
  "revision",
  "section",
  "text",
  "title",
  "turn",
  "visited_turn",
];

interface Player {
  hero: Hero;
  context: BrowserContext;
  page: Page;
  /** Цель героя из ответа вплетения в сценарии (личная зацепка, «Только тебе»). */
  goal: string;
  /** Кадры сокета, которые сервер прислал странице за весь вечер (включая перезапуск). */
  frames: string[];
  /** Тела ответов REST (`/api/…`), которые сервер прислал странице. */
  bodies: string[];
}

// --- сценарий мастера ---

/** Цели героев берутся прямо из сценария: единственный источник, расхождения не будет. */
function scenarioGoals(): string[] {
  const text = fs.readFileSync(SCENARIO_FILE, "utf8");
  return [...text.matchAll(/personal_goal:\s*"([^"]+)"/g)].map(
    (match) => match[1],
  );
}

// --- мелочи ---

function shotsDir(): string {
  const dir =
    process.env.E2E_SCREENSHOTS_DIR ?? path.join(artifactsDir(), "screenshots");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Снимок экрана целиком в тёмной и светлой теме: смотреть его будет владелец. */
async function shoot(page: Page, name: string): Promise<void> {
  const dir = shotsDir();
  for (const scheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
    // Липкая строка статуса при снимке целиком иначе остаётся посреди страницы.
    await page.evaluate("window.scrollTo(0, 0)");
    await page.screenshot({
      path: path.join(dir, `${name}-${scheme}.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
}

/** Ждёт, пока `read` вернёт значение (не `null`); так тест следит за случайным порядком боя. */
async function waitFor<T>(
  what: string,
  read: () => Promise<T | null>,
  timeoutMs = 60_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (value !== null) return value;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Не дождались: ${what}`);
}

const feedOf = (page: Page) => page.getByRole("list", { name: "Лента" });
const tabOf = (page: Page, name: string) =>
  page.getByRole("tab", { name, exact: true });
const composerOf = (page: Page) => page.getByLabel(COMPOSER);
const combatPanel = (page: Page) => page.getByRole("tabpanel", { name: "Бой" });
const combatComposerOf = (page: Page) => combatPanel(page).getByLabel(COMPOSER);
const button = (page: Page, name: string) =>
  page.getByRole("button", { name, exact: true });
const hpMeter = (page: Page, heroName: string) =>
  page.getByRole("meter", { name: `Здоровье: ${heroName}` });
const journalOf = (page: Page) =>
  page.getByRole("tabpanel", { name: "Дневник", exact: true });
/** Вкладка раздела дневника: её имя — подпись и число записей («Места 2»). */
const journalSection = (page: Page, label: string) =>
  journalOf(page).getByRole("tab", { name: new RegExp(`^${label} \\d+$`) });
/** Карточка записи в открытом разделе дневника: её заголовок — название записи. */
const journalCard = (page: Page, title: string) =>
  journalOf(page).getByRole("region", { name: title, exact: true });
const newMark = (card: Locator) => card.getByText("новое", { exact: true });

/** Окно игрока: свой контекст браузера, как отдельный телефон. */
async function openPlayer(
  browser: Browser,
  baseURL: string,
  hero: Hero,
  goal: string,
  query = "",
): Promise<Player> {
  const context = await browser.newContext({
    baseURL,
    locale: "ru-RU",
    colorScheme: "dark",
    reducedMotion: "reduce",
    viewport: { width: 390, height: 844 },
    // Хост копирует приглашение в буфер обмена, а тест проверяет, что там лежит.
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  // Что именно сервер прислал странице, видно только на проводе: DOM показывает лишь часть.
  const frames: string[] = [];
  const bodies: string[] = [];
  page.on("websocket", (socket) => {
    socket.on("framereceived", (frame) => frames.push(String(frame.payload)));
  });
  page.on("response", (response) => {
    if (!new URL(response.url()).pathname.startsWith("/api/")) return;
    // Тело читается, когда ответ готов; сбой чтения (закрытая страница, редирект) не важен.
    void response
      .text()
      .then((text) => bodies.push(text))
      .catch(() => {});
  });
  await page.goto(`/?dev=${hero.login}${query}`);
  return { hero, context, page, goal, frames, bodies };
}

/** Здоровье героя на вкладке «Отряд»: текущее и наибольшее. */
async function hpOf(page: Page, heroName: string) {
  await tabOf(page, "Отряд").click();
  const meter = hpMeter(page, heroName);
  await expect(meter).toBeVisible();
  const now = Number(await meter.getAttribute("aria-valuenow"));
  const max = Number(await meter.getAttribute("aria-valuemax"));
  await tabOf(page, "Рассказ").click();
  return { now, max };
}

// --- проверки на проводе ---

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type JsonObject = { [key: string]: Json };

const isObject = (value: Json): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Кадры сокета как объекты; служебные (не JSON) пропускаются. */
function parsedFrames(player: Player): JsonObject[] {
  const parsed: JsonObject[] = [];
  for (const frame of player.frames) {
    try {
      const value = JSON.parse(frame) as Json;
      if (isObject(value)) parsed.push(value);
    } catch {
      // не JSON: не наш случай
    }
  }
  return parsed;
}

/** Все объекты внутри значения вместе с цепочкой имён ключей, по которой до них дошли. */
function* objectsIn(
  value: Json,
  path: string[] = [],
): Generator<{ node: JsonObject; path: string[] }> {
  if (Array.isArray(value)) {
    for (const item of value) yield* objectsIn(item, path);
  } else if (isObject(value)) {
    yield { node: value, path };
    for (const [key, child] of Object.entries(value)) {
      yield* objectsIn(child, [...path, key]);
    }
  }
}

/** Лист героя игрока в последнем снимке или событии `character`: так сервер описал его герою. */
function lastSheet(player: Player): JsonObject {
  for (const frame of parsedFrames(player).reverse()) {
    if (frame.type !== "snapshot" && frame.type !== "character") continue;
    const sheet = frame.character;
    if (sheet !== undefined && sheet !== null && isObject(sheet)) return sheet;
  }
  throw new Error(`${player.hero.login}: листа героя в кадрах нет`);
}

/** Дневник из последнего снимка игры: с ним страница открыла экран игры. */
function snapshotJournal(player: Player): JsonObject[] {
  for (const frame of parsedFrames(player).reverse()) {
    if (frame.type === "snapshot" && Array.isArray(frame.journal)) {
      return frame.journal.filter(isObject);
    }
  }
  throw new Error(`${player.hero.login}: дневника в снимке нет`);
}

/** Все записи дневника, которые сервер прислал игроку: в снимках, по одной и целым дневником. */
function journalOnWire(player: Player): JsonObject[] {
  const entries: JsonObject[] = [];
  for (const frame of parsedFrames(player)) {
    const batch =
      frame.type === "snapshot"
        ? frame.journal
        : frame.type === "journal"
          ? frame.entries
          : frame.type === "journal_entry"
            ? [frame.entry]
            : [];
    if (Array.isArray(batch)) entries.push(...batch.filter(isObject));
  }
  return entries;
}

/** Общая запись дневника о `ref`; без неё тесту дальше проверять нечего. */
function commonEntry(journal: JsonObject[], ref: string): JsonObject {
  const entry = journal.find((item) => item.id === `${ref}@all`);
  if (entry === undefined) throw new Error(`в дневнике нет записи ${ref}`);
  return entry;
}

/** Id героев, которых игрок создал сам: они приходят в ответах на создание и `characters/mine`. */
function ownHeroIds(player: Player): string[] {
  const ids: string[] = [];
  for (const body of player.bodies) {
    try {
      const value = JSON.parse(body) as Json;
      const hero = isObject(value) ? value.character : undefined;
      if (hero !== undefined && hero !== null && isObject(hero)) {
        if (typeof hero.id === "string") ids.push(hero.id);
      }
    } catch {
      // не JSON
    }
  }
  return ids;
}

// Ключи бойца-монстра в порядке хода: ни здоровья в числах, ни КД, ни ссылок на статблок.
const MONSTER_KEYS = [
  "conditions",
  "health",
  "id",
  "kind",
  "name",
  "side",
  "zone_id",
];

// Ключи бойца-героя: числа здоровья героя публичны (отряд видит друг друга), плюс `can_act`.
const HERO_FIGHTER_KEYS = [
  "can_act",
  "character_id",
  "conditions",
  "hp",
  "hp_max",
  "id",
  "kind",
  "name",
  "side",
  "zone_id",
];

// --- шаги вечера ---

/** Форма «Быстро» (с экрана выбора способа): класс, вид, строка о герое и имя. */
async function fillQuickForm(player: Player): Promise<void> {
  const { page, hero } = player;
  await button(page, "Быстро").click();
  await pickQuickHero(page, hero);
  await page.getByLabel("Опиши героя одной строкой").fill(hero.concept);
  await page.getByLabel("Имя (необязательно)").fill(hero.name);
  await button(page, "Создать героя").click();
  await expect(page.getByRole("switch", { name: "Готов" })).toBeEnabled();
}

async function markReady(player: Player): Promise<void> {
  const ready = player.page.getByRole("switch", { name: "Готов" });
  // Отметка приходит от сервера (событие лобби), поэтому проверяется после щелчка, а не в нём.
  await ready.click();
  await expect(ready).toBeChecked();
}

/** Игрок пишет ход и сдаёт его: карточка «Ход сдан» остаётся, пока мастер не разберёт раунд. */
async function submitAction(
  page: Page,
  text: string,
  options: { secret?: boolean } = {},
): Promise<void> {
  await composerOf(page).fill(text);
  if (options.secret === true) {
    await page.getByRole("switch", { name: "Тайно" }).check();
  }
  await button(page, "Готов").click();
  await expect(page.getByText("Ход сдан")).toBeVisible();
}

/**
 * Ход героя в бою. Мастер отвечает так быстро, что карточка «Ход сдан» успевает смениться
 * полем следующего хода или исчезнуть вместе с боем, поэтому ждут только, чтобы поле хода на
 * вкладке «Бой» закрылось: иначе тест принял бы сдавшего за ходящего ещё раз.
 */
async function submitCombatAction(page: Page, text: string): Promise<void> {
  await combatComposerOf(page).fill(text);
  await combatPanel(page)
    .getByRole("button", { name: "Готов", exact: true })
    .click();
  await expect(combatComposerOf(page)).toHaveCount(0);
}

/** Новый раунд: все сдают ход или пасуют, хост собирает его, и мастер отвечает сразу. */
async function playQuickRound(
  players: Player[],
  actionText: string,
): Promise<void> {
  const [host, ...rest] = players;
  await submitAction(host.page, actionText);
  for (const player of rest) {
    await expect(composerOf(player.page)).toBeEnabled();
    await button(player.page, "Пас").click();
    await expect(player.page.getByText("Вы пасуете")).toBeVisible();
  }
  await button(host.page, "Собрать ходы").click();
}

/** Кто в бою ходит сейчас: игрок с открытым полем хода, `ended` — боя уже нет, `null` — ждём. */
async function combatTurn(players: Player[]): Promise<Player | "ended" | null> {
  const acting: Player[] = [];
  let withoutCombat = 0;
  for (const player of players) {
    const tab = tabOf(player.page, "Бой");
    if (!(await tab.isVisible())) {
      withoutCombat += 1;
      continue;
    }
    try {
      // Короткий срок вместо бесконечного ожидания: бой мог кончиться между проверкой и чтением.
      if (
        (await tab.getAttribute("aria-selected", { timeout: 2_000 })) !== "true"
      )
        await tab.click({ timeout: 2_000 });
    } catch (failure) {
      if (await tab.isVisible()) throw failure; // вкладка на месте: сбой не из-за конца боя
      withoutCombat += 1;
      continue;
    }
    const composer = combatComposerOf(player.page);
    if ((await composer.isVisible()) && (await composer.isEnabled())) {
      acting.push(player);
    }
  }
  if (withoutCombat === players.length) return "ended";
  return acting.length === 1 ? acting[0] : null;
}

test("шестеро друзей проходят вечер: игра, раунды, бой, привал и перезапуск сервера", async ({
  browser,
  baseURL,
  server,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const goals = scenarioGoals();
  expect(goals, "цели героев в сценарии").toHaveLength(HEROES.length);

  const players: Player[] = [];
  const pageErrors: string[] = [];
  const open = async (index: number, query = "") => {
    const player = await openPlayer(
      browser,
      baseURL,
      HEROES[index],
      goals[index],
      query,
    );
    player.page.on("pageerror", (error) => pageErrors.push(error.message));
    players.push(player);
    return player;
  };

  try {
    await test.step("0. Вход: без ?dev=… приложение предлагает выбрать игрока", async () => {
      const visitor = await browser.newContext({
        baseURL,
        locale: "ru-RU",
        colorScheme: "dark",
        reducedMotion: "reduce",
        viewport: { width: 390, height: 844 },
      });
      try {
        const page = await visitor.newPage();
        await page.goto("/");
        await expect(page.getByText("Выберите игрока")).toBeVisible();
        for (const hero of HEROES) {
          await expect(button(page, hero.login)).toBeVisible();
        }
        await shoot(page, "01-vhod");
      } finally {
        await visitor.close();
      }
    });

    let code = "";
    await test.step("1. Хост создаёт игру «Склеп должника» (demo-crypt), остальные входят по коду", async () => {
      const host = await open(0);
      await expect(host.page.getByText("Вы вошли как dev-1.")).toBeVisible();
      await shoot(host.page, "02-moi-igry");

      await button(host.page, "Создать игру").click();
      await expect(
        host.page.getByRole("radio", { name: /Склеп должника/ }),
      ).toBeChecked();
      await button(host.page, "Создать").click();
      await expect(host.page.getByText("Позовите друзей")).toBeVisible();

      const invite = await host.page
        .getByText(/Код приглашения:/)
        .textContent();
      code = /Код приглашения:\s*([A-Z0-9]{6})/.exec(invite ?? "")?.[1] ?? "";
      expect(code, "код приглашения в лобби").toHaveLength(6);

      // «Пригласить» копирует приглашение: без настроенного бота это сам код.
      await button(host.page, "Пригласить").click();
      await expect(
        host.page.getByText("Код скопирован. Отправьте его друзьям."),
      ).toBeVisible();
      expect(await host.page.evaluate("navigator.clipboard.readText()")).toBe(
        code,
      );

      // Входят по очереди: порядок входа задаёт, чьи id стоят в сценарии как $pc1…$pc6.
      for (const index of [1, 2, 3, 4, 5]) {
        let player: Player;
        if (index <= 2) {
          // Через форму «Войти по коду».
          player = await open(index);
          await expect(
            player.page.getByText(`Вы вошли как ${player.hero.login}.`),
          ).toBeVisible();
          await player.page.getByLabel("Код приглашения").fill(code);
          await button(player.page, "Войти").click();
        } else {
          // Через ссылку-приглашение (?join=…).
          player = await open(index, `&join=${code}`);
        }
        await expect(player.page.getByText("Ваш герой")).toBeVisible();
        await expect(
          host.page.getByText(`Игроки (${index + 1})`),
        ).toBeVisible();
      }
      await shoot(host.page, "03-lobbi");
      await expectFitsPhone(host.page, "лобби на шестерых");
    });

    await test.step("2. Быстрые герои, «Готов» у всех, хост жмёт «Начать»", async () => {
      const [host, ...others] = players;

      // Хост создаёт героя первым, чтобы снять экран создания: дальше остальные идут вместе.
      await button(host.page, "Создать персонажа").click();
      await expect(host.page.getByText("Создание героя")).toBeVisible();
      await shoot(host.page, "04-sozdanie-geroya");
      await fillQuickForm(host);

      await Promise.all(
        others.map(async (player) => {
          await button(player.page, "Создать персонажа").click();
          await fillQuickForm(player);
        }),
      );

      // Егор передумал: «Изменить героя» открывает форму с его прежним выбором.
      const egor = players[5];
      await egor.page
        .getByRole("button", { name: "Изменить", exact: true })
        .click();
      await expect(egor.page.getByText("Изменить героя")).toBeVisible();
      await egor.page
        .getByRole("button", { name: "Изменить", exact: true })
        .click();
      await expect(
        egor.page.getByLabel("Опиши героя одной строкой"),
      ).toHaveValue(egor.hero.concept);
      await expectPicked(egor.page, "Класс", className(egor.hero.classId));
      await button(egor.page, "Создать героя").click();
      await expect(
        egor.page.getByRole("switch", { name: "Готов" }),
      ).toBeEnabled();

      // «Начать» ждёт всех: пока Егор не готов, кнопка закрыта и названо, кого ждём.
      for (const player of players.slice(0, 5)) await markReady(player);
      await expect(button(host.page, "Начать")).toBeDisabled();
      await expect(
        host.page.getByText("Ждём готовности: dev-6."),
      ).toBeVisible();
      await markReady(egor);
      await expect(button(host.page, "Начать")).toBeEnabled();
      await button(host.page, "Начать").click();
    });

    await test.step("3. Все видят личную зацепку и первый рассказ", async () => {
      for (const player of players) {
        const { page } = player;
        await expect(
          feedOf(page).getByText(/Вечер в Мшистом Броде/),
        ).toBeVisible();
        const note = page.getByRole("note", { name: "Только тебе" });
        await expect(note).toContainText(`Твоя зацепка, ${player.hero.name}.`);
        await expect(note).toContainText(player.goal);
      }
      // Чужая зацепка в страницу не попадает ни скрытым текстом, ни в данных.
      for (const player of players) {
        const html = await player.page.content();
        for (const other of players.filter((item) => item !== player)) {
          expect(
            html,
            `${player.hero.login} видит цель ${other.hero.login}`,
          ).not.toContain(other.goal);
        }
      }
      await expect(
        players[0].page.getByText("Обсуждение", { exact: true }),
      ).toBeVisible();
      await expectFitsPhone(players[1].page, "экран игры: рассказ и поле хода");
      await shoot(players[1].page, "05-igra");
    });

    await test.step("3б. Вкладка «Персонаж»: числа листа — ровно те, что посчитал сервер", async () => {
      for (const player of players) {
        const { page } = player;
        const sheet = lastSheet(player);
        const derived = sheet.derived as JsonObject;
        expect(
          derived,
          `${player.hero.login}: блок derived в листе`,
        ).toBeTruthy();

        await tabOf(page, "Персонаж").click();
        // Свой герой и только он: имя на листе — имя героя игрока.
        await expect(
          page.getByRole("heading", { name: player.hero.name, exact: true }),
        ).toBeVisible();
        const stat = (label: string) =>
          page.locator(".sheet-stat", { hasText: label }).locator("dd");
        await expect(stat("Класс доспеха")).toHaveText(
          String(derived.armor_class),
        );
        await expect(stat("Пассивная внимательность")).toHaveText(
          String(derived.passive_perception),
        );
        await expect(
          page
            .getByRole("list", { name: "Характеристики" })
            .getByRole("listitem"),
        ).toHaveCount((derived.abilities as Json[]).length);
        await expect(
          page.getByRole("list", { name: "Навыки" }).getByRole("listitem"),
        ).toHaveCount((derived.skills as Json[]).length);
        const attacks = derived.attacks as Json[];
        if (attacks.length > 0) {
          await expect(
            page.getByRole("list", { name: "Атаки" }).getByRole("listitem"),
          ).toHaveCount(attacks.length);
        }
        await expectFitsPhone(page, `лист героя ${player.hero.name}`);
        // Чужих героев на листе нет: имён других игроков в заголовках вкладки не видно.
        for (const other of players.filter((item) => item !== player)) {
          await expect(
            page.getByRole("heading", { name: other.hero.name, exact: true }),
          ).toHaveCount(0);
        }
        await tabOf(page, "Рассказ").click();
      }
    });

    const [alisa, boris, vera] = players;
    await test.step("3в. Дневник в начале игры: завязка, известные места и «новое» до первого открытия", async () => {
      const { page } = boris;
      const start = snapshotJournal(boris);
      expect(
        start.map((entry) => entry.ref).sort(),
        "стартовый дневник «Склепа должника»",
      ).toEqual([...START_JOURNAL_REFS].sort());
      for (const entry of start) {
        expect(
          [entry.audience, entry.turn, entry.revision],
          `${String(entry.ref)}: общая, с начала игры, первая версия`,
        ).toEqual(["all", 0, 1]);
      }
      // Отряд стоит в деревне с начала игры, а о кладбище только знает.
      const village = commonEntry(start, "location:village");
      const cemetery = commonEntry(start, "location:cemetery");
      expect(village.visited_turn, "деревня: были здесь").toBe(0);
      expect(cemetery.visited_turn, "кладбище: ещё не были").toBeNull();

      // «Дневник» стоит сразу после «Рассказа».
      await expect(
        page.getByRole("tablist", { name: "Разделы игры" }).getByRole("tab"),
      ).toHaveText(["Рассказ", "Дневник", "Персонаж", "Отряд"]);
      await tabOf(page, "Дневник").click();
      const journal = journalOf(page);
      await expect(
        journal
          .getByRole("tablist", { name: "Разделы дневника" })
          .getByRole("tab"),
      ).toHaveText(
        JOURNAL_SECTIONS.map(([, label]) => new RegExp(`^${label} \\d+$`)),
      );
      await expect(journalSection(page, "Главное")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expect(journalCard(page, "Главное")).toContainText(JOURNAL_INTRO);

      // Каждый раздел показывает ровно присланное сервером, и до первого открытия всё «новое».
      for (const [section, label] of JOURNAL_SECTIONS) {
        const entries = start.filter((entry) => entry.section === section);
        const tab = journalSection(page, label);
        await expect(tab).toHaveAccessibleName(`${label} ${entries.length}`);
        await tab.click();
        await expect(tab).toHaveAttribute("aria-selected", "true");
        const shown = journal.getByRole("tabpanel");
        if (entries.length === 0) {
          await expect(shown).toContainText("Пока ничего не известно");
          continue;
        }
        await expect(shown.getByRole("region")).toHaveCount(entries.length);
        for (const entry of entries) {
          const card = journalCard(page, String(entry.title));
          await expect(card).toContainText(String(entry.text));
          await expect(card).toContainText(
            entry.visited_turn === 0
              ? "были здесь с начала игры"
              : "известно с начала игры",
          );
          await expect(newMark(card)).toBeVisible();
        }
      }

      // Игрок ушёл из раздела и вернулся: записи прочитаны, пометки «новое» больше нет.
      await journalSection(page, "Места").click();
      const villageCard = journalCard(page, String(village.title));
      const cemeteryCard = journalCard(page, String(cemetery.title));
      await expect(villageCard).toContainText("были здесь с начала игры");
      await expect(cemeteryCard).toContainText("известно с начала игры");
      await expect(newMark(villageCard)).toHaveCount(0);
      await expect(newMark(cemeteryCard)).toHaveCount(0);
      await expectFitsPhone(page, "вкладка «Дневник»: места");
      await shoot(page, "05b-dnevnik");
      await journalSection(page, "Главное").click();
      await expect(newMark(journalCard(page, "Главное"))).toHaveCount(0);
      await tabOf(page, "Рассказ").click();
    });

    await test.step("4. Тайный ход Веры, черновик Бориса и сбор ходов с отсчётом", async () => {
      // Вера сдаёт ход «Тайно».
      await submitAction(vera.page, SECRET_ACTION, { secret: true });
      await expect(
        vera.page.getByText("Тайно", { exact: true }).first(),
      ).toBeVisible();
      // Статус игрока виден всем («Готов»), а текст хода — только Вере.
      const veraStatus = boris.page
        .getByRole("list", { name: "Игроки" })
        .getByRole("listitem")
        .filter({ hasText: "dev-3" });
      await expect(veraStatus).toContainText("Готов");

      // Борис пишет черновик, но не сдаёт; ждём, пока он дойдёт до сервера (автосохранение 800 мс).
      const saved = boris.page.waitForResponse(
        (response) =>
          response.request().method() === "PUT" &&
          response.url().endsWith("/action") &&
          response.ok(),
      );
      await composerOf(boris.page).fill(DRAFT_ACTION);
      await saved;

      // Пока черновик не сдан, его не видит никто, кроме Бориса.
      for (const player of players.filter((item) => item !== boris)) {
        expect(await player.page.content()).not.toContain(DRAFT_ACTION);
      }

      // Хост собирает ходы: идёт отсчёт, по его концу черновик Бориса сдаётся сам, и все видят
      // его ход в ленте (черновики и несданные ходы туда не попадают).
      await button(alisa.page, "Собрать ходы").click();
      await expect(
        alisa.page.getByText("Сбор ходов", { exact: true }),
      ).toBeVisible();
      await expect(
        alisa.page.getByRole("timer", { name: /До конца сбора/ }),
      ).toBeVisible();
      await expectFitsPhone(alisa.page, "экран хоста: отсчёт и панель кнопок");
      for (const player of players) {
        await expect(feedOf(player.page).getByText(DRAFT_ACTION)).toBeVisible();
      }
    });

    await test.step("5. Карточки бросков и рассказ у всех; тайный бросок только у Веры", async () => {
      const story = /лихо перепрыгивает лужу/;
      const borisCheck = { name: /Борис\. Ловкость \(Акробатика\)/ };
      const veraCheck = { name: /Вера\. Ловкость \(Скрытность\)/ };
      for (const player of players) {
        await expect(feedOf(player.page).getByText(story)).toBeVisible();
        await expect(
          player.page.getByRole("article", borisCheck),
        ).toBeVisible();
      }
      // Бросок тайного хода и сам ход — только у автора.
      await expect(vera.page.getByRole("article", veraCheck)).toBeVisible();
      await expect(feedOf(vera.page).getByText(SECRET_ACTION)).toBeVisible();
      for (const player of players.filter((item) => item !== vera)) {
        await expect(player.page.getByRole("article", veraCheck)).toHaveCount(
          0,
        );
        const html = await player.page.content();
        expect(
          html,
          `${player.hero.login} видит тайный ход Веры`,
        ).not.toContain(SECRET_ACTION);
        expect(html).not.toContain("КРАСНЫЙ-ФОНАРЬ");
      }
      // Сервер вычел урон Борису: до привала его здоровье не полное.
      const wounded = await hpOf(alisa.page, boris.hero.name);
      expect(wounded.now).toBeLessThan(wounded.max);
    });

    await test.step("6. Бой: ходит один герой, остальные видят «Ходит …», бой заканчивается", async () => {
      await playQuickRound(players, "Идём на кладбище.");
      for (const player of players) {
        await expect(
          feedOf(player.page).getByText("Начался бой."),
        ).toBeVisible();
        await expect(tabOf(player.page, "Бой")).toBeVisible();
      }

      await test.step("6а. Отряд пришёл на кладбище: в дневнике «были здесь» и снова «новое»", async () => {
        const { page } = boris;
        // Приход отряда дополнил запись о кладбище: сервер прислал её новую версию.
        const visit = await waitFor(
          "запись дневника о приходе на кладбище",
          async () => {
            const entry = journalOnWire(boris).findLast(
              (item) => item.id === "location:cemetery@all",
            );
            return typeof entry?.visited_turn === "number" ? entry : null;
          },
        );
        expect(visit.revision, "кладбище: версия после прихода").toBe(2);
        const round = Number(visit.visited_turn);
        expect(round, "кладбище: раунд прихода").toBeGreaterThan(0);
        const village = commonEntry(snapshotJournal(boris), "location:village");

        // Вкладок в бою больше, а «Дневник» всё так же открывается и помещается на телефоне.
        await tabOf(page, "Дневник").click();
        await journalSection(page, "Места").click();
        const cemeteryCard = journalCard(page, String(visit.title));
        const villageCard = journalCard(page, String(village.title));
        await expect(cemeteryCard).toContainText(`были здесь: раунд ${round}`);
        await expect(newMark(cemeteryCard)).toBeVisible();
        // Деревня не менялась и осталась прочитанной.
        await expect(villageCard).toContainText("были здесь с начала игры");
        await expect(newMark(villageCard)).toHaveCount(0);
        await expectFitsPhone(page, "вкладка «Дневник» в бою");

        await journalSection(page, "Главное").click();
        await journalSection(page, "Места").click();
        await expect(cemeteryCard).toContainText(`были здесь: раунд ${round}`);
        await expect(newMark(cemeteryCard)).toHaveCount(0);
        await tabOf(page, "Бой").click();
      });

      // Порядок хода случайный (кубы настоящие), поэтому ходы разбираются по ходу боя.
      let heroTurns = 0;
      let shot = false;
      for (;;) {
        const turn = await waitFor("ход героя в бою или конец боя", () =>
          combatTurn(players),
        );
        if (turn === "ended") break;
        heroTurns += 1;
        expect(heroTurns, "ходов героев в бою").toBeLessThanOrEqual(2);
        for (const other of players.filter((item) => item !== turn)) {
          await expect(
            combatPanel(other.page).getByText(`Ходит ${turn.hero.name}.`, {
              exact: true,
            }),
          ).toBeVisible();
          await expect(combatComposerOf(other.page)).toHaveCount(0);
        }
        const order = turn.page.getByRole("list", { name: "Порядок хода" });
        await expect(order).toContainText("Скелет");
        await expect(order).toContainText("Враг");
        if (!shot) {
          // Вкладка «Бой» показывает то, что прислал сервер: полоски здоровья героев, слово о
          // здоровье монстра (чисел у него нет) и зоны, где стоят все семеро бойцов.
          await expect(order.getByRole("meter")).toHaveCount(HEROES.length);
          const zones = turn.page.getByRole("list", { name: "Зоны боя" });
          for (const hero of HEROES) {
            await expect(zones).toContainText(hero.name);
          }
          await expect(zones).toContainText("Скелет");
          await expectFitsPhone(turn.page, "вкладка «Бой»");
          await shoot(turn.page, "06-boy");
          shot = true;
        }
        await submitCombatAction(
          turn.page,
          `${turn.hero.name} уклоняется от меча.`,
        );
      }
      expect(heroTurns, "ходов героев в бою").toBeGreaterThanOrEqual(1);
      // Один ход героя — скелет ходил первым (его ход разыграл сервер), два — первым ходил герой.
      test.info().annotations.push({
        type: "ходов героев в бою",
        description: String(heroTurns),
      });
      for (const player of players) {
        await expect(
          feedOf(player.page).getByText(/Скелет разваливается на груду костей/),
        ).toBeVisible();
        await expect(
          feedOf(player.page).getByText("Бой окончен."),
        ).toBeVisible();
        await expect(tabOf(player.page, "Бой")).toHaveCount(0);
      }
    });

    await test.step("7. Привал: реплика у костра видна всем, по таймеру наступает утро, здоровье полное", async () => {
      await expect(composerOf(alisa.page)).toBeEnabled();
      await playQuickRound(players, "Разбиваем лагерь у сторожки.");

      // Борис оставляет реплику в первые секунды привала.
      for (const player of players) {
        await expect(tabOf(player.page, "Привал")).toBeVisible();
      }
      await expect(
        alisa.page.getByRole("timer", { name: /До конца привала/ }),
      ).toBeVisible();
      await tabOf(boris.page, "Привал").click();
      await boris.page.getByLabel("Сказать у костра").fill(CAMP_LINE);
      await button(boris.page, "Сказать").click();
      for (const player of players.filter((item) => item !== boris)) {
        await expect(feedOf(player.page).getByText(CAMP_LINE)).toBeVisible();
      }
      await expect(
        boris.page.getByRole("list", { name: "Реплики у костра" }),
      ).toContainText(CAMP_LINE);
      await expectFitsPhone(boris.page, "вкладка «Привал»");
      await shoot(boris.page, "07-privat");

      // Отсчёт привала кончается сам: мастер рассказывает утро, сервер проводит длинный отдых.
      for (const player of players) {
        await expect(
          feedOf(player.page).getByText(/Утро\. Туман отступил/),
        ).toBeVisible({ timeout: 60_000 });
        await expect(
          feedOf(player.page).getByText(
            "Длинный отдых: отряд восстановил силы.",
          ),
        ).toBeVisible();
        await expect(tabOf(player.page, "Привал")).toHaveCount(0);
        await expect(
          player.page.getByText("Обсуждение", { exact: true }),
        ).toBeVisible();
      }
      for (const player of players) {
        await tabOf(player.page, "Отряд").click();
        if (player === alisa)
          await expectFitsPhone(player.page, "вкладка «Отряд»");
        const meters = player.page.getByRole("meter");
        await expect(meters).toHaveCount(HEROES.length);
        for (const meter of await meters.all()) {
          expect(await meter.getAttribute("aria-valuenow")).toBe(
            await meter.getAttribute("aria-valuemax"),
          );
        }
        await tabOf(player.page, "Рассказ").click();
      }
    });

    await test.step("8. Перезапуск сервера: страницы переподключаются, лента и фаза на месте", async () => {
      // Перезапуск вне хода мастера: игра в обсуждении, разбора нет.
      await expect(
        alisa.page.getByText("Обсуждение", { exact: true }),
      ).toBeVisible();

      // Обрыв виден игроку там, где он играет: страница прокручена к полю хода, а плашка стоит
      // в прилипающей строке статуса и потому в окне. Поле хода на время обрыва закрыто.
      const reader = players[3].page;
      await expect(composerOf(reader)).toBeEnabled();
      await reader.evaluate("window.scrollTo(0, document.body.scrollHeight)");
      expect(
        await reader.evaluate("window.scrollY"),
        "лента длинная: страницу есть куда прокручивать",
      ).toBeGreaterThan(0);
      const restarting = server.restart();
      try {
        await expect(
          reader.locator(".game-status").getByText("Переподключаемся…"),
        ).toBeInViewport({ ratio: 1 });
        await expect(composerOf(reader)).toBeDisabled();
        await expect(reader.getByText(/^Нет связи с игрой/)).toBeVisible();
      } finally {
        await restarting;
      }

      // Сокет каждой страницы вернулся, если событие нового хода доходит до остальных.
      await expect(composerOf(players[3].page)).toBeEnabled({
        timeout: 60_000,
      });
      await button(players[3].page, "Пас").click();
      for (const player of players.filter((item) => item !== players[3])) {
        await expect(
          player.page
            .getByRole("list", { name: "Игроки" })
            .getByRole("listitem")
            .filter({ hasText: "dev-4" }),
        ).toContainText("Пас", { timeout: 60_000 });
      }
      for (const player of players) {
        await expect(player.page.getByText("Переподключаемся…")).toHaveCount(0);
        await expect(
          player.page.getByText("Обсуждение", { exact: true }),
        ).toBeVisible();
        await expect(
          feedOf(player.page).getByText(/Утро\. Туман отступил/),
        ).toBeVisible();
        await expect(feedOf(player.page).getByText(DRAFT_ACTION)).toBeVisible();
        await expect(
          player.page.getByRole("note", { name: "Только тебе" }),
        ).toContainText(player.goal);
      }
      // После перезапуска снимок по-прежнему отдаёт тайное только автору.
      await expect(
        vera.page.getByRole("article", {
          name: /Вера\. Ловкость \(Скрытность\)/,
        }),
      ).toBeVisible();
      for (const player of players.filter((item) => item !== vera)) {
        await expect(
          player.page.getByRole("article", { name: /Вера\. Ловкость/ }),
        ).toHaveCount(0);
        expect(await player.page.content()).not.toContain("КРАСНЫЙ-ФОНАРЬ");
      }

      // Дневник пережил перезапуск: снимок после него несёт приход на кладбище из сохранённой
      // игры, а прочитанное до обрыва не стало снова «новым» у переподключившейся страницы.
      const cemetery = commonEntry(snapshotJournal(boris), "location:cemetery");
      expect(cemetery.revision, "кладбище в снимке после перезапуска").toBe(2);
      await tabOf(boris.page, "Дневник").click();
      await journalSection(boris.page, "Места").click();
      const cemeteryCard = journalCard(boris.page, String(cemetery.title));
      await expect(cemeteryCard).toContainText(
        `были здесь: раунд ${Number(cemetery.visited_turn)}`,
      );
      await expect(newMark(cemeteryCard)).toHaveCount(0);
      await tabOf(boris.page, "Рассказ").click();
    });

    await test.step("9. Тайны на проводе: что сервер прислал каждому, за весь вечер и после перезапуска", async () => {
      const wireOf = (player: Player) =>
        [...player.frames, ...player.bodies].join("\n");
      const veraWire = wireOf(vera);
      expect(veraWire).toContain("КРАСНЫЙ-ФОНАРЬ"); // контроль: у автора тайный ход есть

      for (const player of players) {
        const login = player.hero.login;
        const wire = wireOf(player);
        const frames = parsedFrames(player);
        expect(frames.length, `${login}: кадры сокета`).toBeGreaterThan(10);

        // Своя зацепка пришла, чужих нет; тайный ход Веры — только у Веры.
        expect(wire, `${login}: своя цель`).toContain(player.goal);
        for (const other of players.filter((item) => item !== player)) {
          expect(
            wire,
            `${login} получил цель ${other.hero.login}`,
          ).not.toContain(other.goal);
        }
        if (player !== vera) {
          expect(wire, `${login} получил тайный ход Веры`).not.toContain(
            "КРАСНЫЙ-ФОНАРЬ",
          );
        }

        // Служебное и тайное приключения и выбор игрока по сокету и REST не ходят вообще.
        expect(wire, `${login}: служебные поля`).not.toMatch(
          /"(dm_notes|adventure_data|pc_hooks|dm_turn_logs|base_scores|background_bonus|preview_id)"/,
        );
        for (const frame of player.frames) {
          expect(frame, `${login}: выбор игрока в кадре сокета`).not.toContain(
            '"choices"',
          );
        }
        // Выбор приходит только в ответе `characters/mine` и только свой.
        for (const body of player.bodies.filter((text) =>
          text.includes('"choices"'),
        )) {
          expect(body, `${login}: свой выбор`).toContain(player.hero.concept);
          for (const other of players.filter((item) => item !== player)) {
            expect(
              body,
              `${login} получил выбор ${other.hero.login}`,
            ).not.toContain(other.hero.concept);
          }
        }

        // Числа правил (derived) приходят только в листе самого игрока. Героев у игрока мог быть
        // не один (Егор правил своего: прежний заменён), поэтому «свои» — все, кого создал он сам.
        const ownIds = new Set(ownHeroIds(player));
        expect(ownIds.size, `${login}: id своих героев`).toBeGreaterThan(0);

        // Дневник: у записи только поля контракта, и каждая — общая или про своего героя.
        // Личных записей сценарий не делает; что чужая личная не уходит, проверяют тесты сервера.
        const journal = journalOnWire(player);
        expect(journal.length, `${login}: записи дневника`).toBeGreaterThan(0);
        for (const entry of journal) {
          expect(
            Object.keys(entry).sort(),
            `${login}: запись дневника`,
          ).toEqual(JOURNAL_ENTRY_KEYS);
          expect(
            entry.audience === "all" || ownIds.has(String(entry.audience)),
            `${login}: чужая личная запись ${String(entry.id)}`,
          ).toBe(true);
        }
        let sheets = 0;
        for (const frame of frames) {
          for (const { node, path } of objectsIn(frame)) {
            if (!("derived" in node)) continue;
            sheets += 1;
            expect(
              ownIds.has(node.id as string),
              `${login}: derived не у своего героя (${String(node.id)})`,
            ).toBe(true);
            expect(path.at(-1), `${login}: derived не в листе героя`).toBe(
              "character",
            );
          }
        }
        expect(sheets, `${login}: листы со своими числами`).toBeGreaterThan(0);

        // КД монстров не передаётся нигде: ключ `ac` не встречается, а `armor_class` только в
        // derived самого героя; у бойца-монстра ровно публичные поля, без чисел здоровья.
        let monsters = 0;
        let heroes = 0;
        for (const frame of frames) {
          for (const { node, path } of objectsIn(frame)) {
            expect("ac" in node, `${login}: ключ ac`).toBe(false);
            if ("armor_class" in node) {
              expect(path.at(-1), `${login}: armor_class вне derived`).toBe(
                "derived",
              );
            }
            if (node.kind === "monster" && path.at(-1) === "order") {
              monsters += 1;
              expect(Object.keys(node).sort(), `${login}: боец-монстр`).toEqual(
                MONSTER_KEYS,
              );
            }
            if (node.kind === "pc" && path.at(-1) === "order") {
              heroes += 1;
              expect(Object.keys(node).sort(), `${login}: боец-герой`).toEqual(
                HERO_FIGHTER_KEYS,
              );
              // Может ли герой действовать, решает движок; клиент получает готовое да или нет.
              expect(typeof node.can_act, `${login}: can_act героя`).toBe(
                "boolean",
              );
            }
          }
        }
        expect(monsters, `${login}: скелет в порядке хода`).toBeGreaterThan(0);
        expect(heroes, `${login}: герои в порядке хода`).toBeGreaterThan(0);
      }
    });

    expect(pageErrors, "необработанные ошибки страниц").toEqual([]);
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});
