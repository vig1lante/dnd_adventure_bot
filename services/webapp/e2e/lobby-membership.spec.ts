import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
} from "@playwright/test";
import { pickQuickHero } from "./hero.ts";
import { expectFitsPhone } from "./layout.ts";
import { expect, test } from "./server.ts";

// Состав лобби на настоящем сервере: выход игрока, повторный вход по коду и исключение хостом.
// Юнит-тесты экранов проверяют это на заглушках сокета; здесь сервер сам рвёт сокет кодом 4403
// (игрок больше не участник), и клиент должен отреагировать так: выходящему не мигает «Игра
// недоступна.», исключённому показывается «Хост исключил вас.» (кадр `kicked` перед 4403), герой
// исчезает вместе с игроком, а чужие данные игры перестают быть доступными.

interface Player {
  login: string;
  context: BrowserContext;
  page: Page;
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
  await page.goto(`/?dev=${login}`);
  await expect(page.getByText(`Вы вошли как ${login}.`)).toBeVisible();
  return { login, context, page };
}

const button = (page: Page, name: string) =>
  page.getByRole("button", { name, exact: true });

// Время в поле `created_at`: ISO 8601 в UTC с явным `+00:00`.
const ISO_UTC = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$/;
// Часовой пояс второго телефона хоста: круглый год UTC+5, без перехода на летнее время.
const PHONE_TIMEZONE = "Asia/Yekaterinburg";
const PHONE_OFFSET_MS = 5 * 60 * 60 * 1000;
const MONTHS = [
  "января",
  "февраля",
  "марта",
  "апреля",
  "мая",
  "июня",
  "июля",
  "августа",
  "сентября",
  "октября",
  "ноября",
  "декабря",
];

/** Момент сервера по часам телефона в `PHONE_TIMEZONE`: «29 октября 2026, 19:05». */
function phoneMoment(iso: string): string {
  const local = new Date(Date.parse(iso) + PHONE_OFFSET_MS);
  const time = [local.getUTCHours(), local.getUTCMinutes()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return `${local.getUTCDate()} ${MONTHS[local.getUTCMonth()]} ${local.getUTCFullYear()}, ${time}`;
}

async function tokenOf(request: APIRequestContext, login: string) {
  const response = await request.post("/api/auth/dev", { data: { login } });
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { token: string }).token;
}

test("лобби: выход, возврат по коду и исключение хостом", async ({
  browser,
  baseURL,
  request,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-3");
    const friend = await openPlayer(browser, baseURL, "dev-4");
    players.push(host, friend);

    await button(host.page, "Создать игру").click();
    await button(host.page, "Создать").click();
    await expect(host.page.getByText("Позовите друзей")).toBeVisible();
    const invite = await host.page.getByText(/Код приглашения:/).textContent();
    const code =
      /Код приглашения:\s*([A-Z0-9]{6})/.exec(invite ?? "")?.[1] ?? "";
    expect(code, "код приглашения").toHaveLength(6);

    const join = async () => {
      await friend.page.getByLabel("Код приглашения").fill(code);
      await button(friend.page, "Войти").click();
      await expect(friend.page.getByText("Ваш герой")).toBeVisible();
    };

    await test.step("Друг входит по коду и создаёт героя", async () => {
      await join();
      await expect(host.page.getByText("Игроки (2)")).toBeVisible();
      await button(friend.page, "Создать персонажа").click();
      await button(friend.page, "Быстро").click();
      await pickQuickHero(friend.page, {
        classId: "rogue",
        species: "halfling",
      });
      await friend.page
        .getByLabel("Опиши героя одной строкой")
        .fill("Карманник, который мечтает о честной жизни");
      await button(friend.page, "Создать героя").click();
      await expect(
        friend.page.getByRole("switch", { name: "Готов" }),
      ).toBeEnabled();
      await expect(host.page.getByText("Персонаж создан")).toBeVisible();
    });

    const gameId = await test.step("Игра известна обоим по API", async () => {
      const hostToken = await tokenOf(request, "dev-3");
      const games = await request.get("/api/games", {
        headers: { Authorization: `Bearer ${hostToken}` },
      });
      const list = (await games.json()) as { id: string; code: string }[];
      const found = list.find((game) => game.code === code);
      expect(found, "игра хоста в списке").toBeDefined();
      return found?.id ?? "";
    });

    // Дата создания приходит от сервера в UTC, а строка «Мои игры» показывает её по часам
    // телефона.
    await test.step("«Мои игры» на втором телефоне хоста: дата создания игры по его часовому поясу", async () => {
      const hostToken = await tokenOf(request, "dev-3");
      const games = await request.get("/api/games", {
        headers: { Authorization: `Bearer ${hostToken}` },
      });
      const game = (
        (await games.json()) as {
          id: string;
          adventure_title: string;
          created_at: string;
        }[]
      ).find((item) => item.id === gameId);
      if (game === undefined) throw new Error("игры хоста нет в списке");
      expect(game.created_at, "дата создания в ответе").toMatch(ISO_UTC);

      const context = await browser.newContext({
        baseURL,
        locale: "ru-RU",
        timezoneId: PHONE_TIMEZONE,
        viewport: { width: 390, height: 844 },
      });
      try {
        const page = await context.newPage();
        await page.goto("/?dev=dev-3");
        await expect(page.getByText("Вы вошли как dev-3.")).toBeVisible();

        const created = `создана ${phoneMoment(game.created_at)}`;
        const row = page.getByRole("listitem").filter({ hasText: created });
        await expect(row).toHaveCount(1);
        await expect(
          row.getByRole("button", { name: `Открыть: ${game.adventure_title}` }),
        ).toHaveAccessibleDescription(new RegExp(created));
        await expectFitsPhone(page, "«Мои игры»: строка игры с датой создания");
      } finally {
        await context.close();
      }
    });

    await test.step("Друг выходит из лобби: «Игра недоступна» не мигает, герой исчезает", async () => {
      await button(friend.page, "Выйти из лобби").click();
      await expect(
        friend.page.getByText("Выйти из лобби? Ваш герой будет удалён."),
      ).toBeVisible();
      await expectFitsPhone(friend.page, "лобби: подтверждение выхода");
      await button(friend.page, "Да, выйти").click();
      await expect(
        friend.page.getByRole("heading", { name: "Мои игры" }),
      ).toBeVisible();
      await expect(host.page.getByText("Игроки (1)")).toBeVisible();
      // Сервер рвёт сокет выходящего раньше ответа REST: если бы клиент не учитывал выход,
      // сообщение появилось бы через мгновение, поэтому ждём, а не проверяем сразу.
      await friend.page.waitForTimeout(1_500);
      await expect(friend.page.getByText("Игра недоступна.")).toHaveCount(0);
      await expect(friend.page.getByText("Пока нет игр")).toBeVisible();
    });

    await test.step("Друг возвращается по коду: героя нет, он создаёт нового", async () => {
      await join();
      await expect(host.page.getByText("Игроки (2)")).toBeVisible();
      await expect(button(friend.page, "Создать персонажа")).toBeVisible();
    });

    await test.step("Хост исключает друга: тот видит «Хост исключил вас.», доступ к игре закрыт", async () => {
      await host.page
        .getByRole("button", { name: "Исключить dev-4", exact: true })
        .click();
      await expect(
        host.page.getByText(/Исключить игрока dev-4\?/),
      ).toBeVisible();
      await expectFitsPhone(host.page, "лобби: подтверждение исключения");
      await button(host.page, "Да, исключить").click();
      await expect(host.page.getByText("Игроки (1)")).toBeVisible();

      // Сервер прислал кадр `kicked` и закрыл сокет кодом 4403: клиент ведёт в «Мои игры» и
      // объясняет почему.
      await expect(
        friend.page.getByRole("heading", { name: "Мои игры" }),
      ).toBeVisible();
      await expect(friend.page.getByText("Хост исключил вас.")).toBeVisible();
      await expect(friend.page.getByText("Игра недоступна.")).toHaveCount(0);

      // Для исключённого игры больше нет: ни сводки, ни героя, ни предпросмотра.
      const friendToken = await tokenOf(request, "dev-4");
      const headers = { Authorization: `Bearer ${friendToken}` };
      for (const path of [
        `/api/games/${gameId}`,
        `/api/games/${gameId}/characters/mine`,
      ]) {
        const response = await request.get(path, { headers });
        expect(response.status(), path).toBe(404);
      }
      const preview = await request.post(
        `/api/games/${gameId}/characters/random-preview`,
        { headers },
      );
      expect(preview.status()).toBe(404);
      const list = await request.get("/api/games", { headers });
      expect(await list.json()).toEqual([]);
    });
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});
