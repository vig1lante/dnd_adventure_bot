import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
} from "@playwright/test";
import { expectFitsPhone } from "./layout.ts";
import { expect, test } from "./server.ts";

// Состав лобби на настоящем сервере: выход игрока, повторный вход по коду и исключение хостом.
// Юнит-тесты экранов проверяют это на заглушках сокета; здесь сервер сам рвёт сокет кодом 4403
// (игрок больше не участник), и клиент должен отреагировать так, как описано в плане B8.3 и B8.5:
// выходящему не мигает «Игра недоступна.», исключённому она показывается, герой исчезает вместе
// с игроком, а чужие данные игры перестают быть доступными.

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
      await friend.page
        .getByLabel("Класс", { exact: true })
        .selectOption("rogue");
      await friend.page
        .getByLabel("Вид", { exact: true })
        .selectOption("halfling");
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

    await test.step("Хост исключает друга: тот видит «Игра недоступна.», доступ к игре закрыт", async () => {
      await host.page
        .getByRole("button", { name: "Исключить dev-4", exact: true })
        .click();
      await expect(
        host.page.getByText(/Исключить игрока dev-4\?/),
      ).toBeVisible();
      await expectFitsPhone(host.page, "лобби: подтверждение исключения");
      await button(host.page, "Да, исключить").click();
      await expect(host.page.getByText("Игроки (1)")).toBeVisible();

      // Сервер закрыл сокет кодом 4403: клиент ведёт в «Мои игры» и объясняет почему.
      await expect(
        friend.page.getByRole("heading", { name: "Мои игры" }),
      ).toBeVisible();
      await expect(friend.page.getByText("Игра недоступна.")).toBeVisible();

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
