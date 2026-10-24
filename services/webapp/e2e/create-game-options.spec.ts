import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
  Request,
} from "@playwright/test";
import { expectFitsPhone } from "./layout.ts";
import { expect, test } from "./server.ts";

// Форма новой игры на настоящем сервере (финальное покрытие B1): флажок «Мат и грубая речь» и
// модель «Claude Code на этом компьютере». Юнит-тесты формы и лобби идут на заглушках по контракту
// блока; здесь те же экраны говорят с `dnd-server` (dev, `FakeProvider`, временная база): тело
// запроса доходит до сервера, сводка игры и событие лобби несут флажок, список моделей и отказ 422
// приходят от сервера. Игры ведёт заглушка: живую модель стенд не запускает (см. `server.ts`).
// Входят dev-5 и dev-6: у dev-4 в конце `lobby-membership.spec.ts` не должно быть игр.

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

const profanitySwitch = (page: Page) =>
  page.getByRole("switch", { name: "Мат и грубая речь" });

/** Выбор нейронки мастера: сама радиокнопка скрыта стилем, щёлкают по подписи. */
async function pickProvider(page: Page, label: RegExp) {
  const providers = page.getByRole("radiogroup", {
    name: "Нейронка для мастера",
  });
  await providers.locator("label", { hasText: label }).click();
  await expect(providers.getByRole("radio", { name: label })).toBeChecked();
}

/** Запрос «Создать» — по нему видно, что именно форма прислала серверу. */
async function submitForm(page: Page): Promise<Request> {
  const sent = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/games",
  );
  await button(page, "Создать").click();
  return sent;
}

async function inviteCode(page: Page): Promise<string> {
  await expect(page.getByText("Позовите друзей")).toBeVisible();
  const invite = await page.getByText(/Код приглашения:/).textContent();
  const code = /Код приглашения:\s*([A-Z0-9]{6})/.exec(invite ?? "")?.[1] ?? "";
  expect(code, "код приглашения в лобби").toHaveLength(6);
  return code;
}

async function joinByCode(player: Player, code: string) {
  await player.page.getByLabel("Код приглашения").fill(code);
  await button(player.page, "Войти").click();
  await expect(player.page.getByText("Ваш герой")).toBeVisible();
}

/** Id игр игрока по API: сравниваются списки до и после, в базе есть игры других тестов. */
async function gameIdsOf(request: APIRequestContext, login: string) {
  const auth = await request.post("/api/auth/dev", { data: { login } });
  expect(auth.ok()).toBe(true);
  const { token } = (await auth.json()) as { token: string };
  const games = await request.get("/api/games", {
    headers: { Authorization: `Bearer ${token}` },
  });
  return ((await games.json()) as { id: string }[]).map((game) => game.id);
}

test("мат включён по умолчанию: форма его не шлёт, сервер включает, лобби показывает хосту и игроку", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-5");
    const friend = await openPlayer(browser, baseURL, "dev-6");
    players.push(host, friend);

    await button(host.page, "Создать игру").click();
    await expect(profanitySwitch(host.page)).toBeChecked();
    const sent = await submitForm(host.page);

    // Включённый флажок — значение сервера по умолчанию, форма его не шлёт; сервер его ставит.
    expect(sent.postDataJSON()).not.toHaveProperty("profanity");
    const created = await sent.response();
    expect(created?.status()).toBe(201);
    expect(await created?.json()).toMatchObject({ profanity: true });
    await expect(host.page.getByText("Мат: включён")).toBeVisible();

    const code = await inviteCode(host.page);
    await joinByCode(friend, code);
    await expect(friend.page.getByText("Мат: включён")).toBeVisible();
    // Хост узнал о новом игроке из события лобби: флажок пришёл и в этой сводке.
    await expect(host.page.getByText("Игроки (2)")).toBeVisible();
    await expect(host.page.getByText("Мат: включён")).toBeVisible();
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});

test("мат выключен, а модель Claude Code — из списка сервера: флажок доходит до лобби хоста и игрока", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-6");
    const friend = await openPlayer(browser, baseURL, "dev-5");
    players.push(host, friend);
    const { page } = host;

    await button(page, "Создать игру").click();

    // Модель подписки стенда не задана (`CLAUDE_CLI_MODEL` пуст): сервер предлагает только
    // псевдонимы CLI, а пустой вариант оставляет выбор самому CLI.
    await pickProvider(page, /Claude Code на этом компьютере/);
    const model = page.getByRole("combobox", { name: "Модель" });
    await expect(model.getByRole("option")).toHaveText([
      "По умолчанию",
      "opus",
      "sonnet",
      "haiku",
    ]);
    await expect(model).toHaveValue("");
    await expectFitsPhone(page, "форма новой игры: модель Claude Code");

    // Игру ведёт заглушка: у неё модели нет, списка тоже.
    await pickProvider(page, /Заглушка по сценарию/);
    await expect(model).toHaveCount(0);
    await expect(
      page.getByText("Этот вариант сам выбирает модель."),
    ).toBeVisible();

    await profanitySwitch(page).click();
    await expect(profanitySwitch(page)).not.toBeChecked();
    const sent = await submitForm(page);

    const body = sent.postDataJSON() as Record<string, unknown>;
    expect(body).toMatchObject({ profanity: false });
    expect(body).not.toHaveProperty("model");
    expect(body).not.toHaveProperty("provider");
    const created = await sent.response();
    expect(created?.status()).toBe(201);
    expect(await created?.json()).toMatchObject({ profanity: false });
    await expect(page.getByText("Мат: выключен")).toBeVisible();

    const code = await inviteCode(page);
    await joinByCode(friend, code);
    await expect(friend.page.getByText("Мат: выключен")).toBeVisible();
    await expect(page.getByText("Игроки (2)")).toBeVisible();
    await expect(page.getByText("Мат: выключен")).toBeVisible();
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});

test("модель не из списка сервера: настоящий отказ 422 показан у формы, игра не создана", async ({
  browser,
  baseURL,
  request,
}) => {
  if (baseURL === undefined) throw new Error("baseURL не задан");
  const players: Player[] = [];
  try {
    const host = await openPlayer(browser, baseURL, "dev-5");
    players.push(host);
    const { page } = host;
    // Список моделей устарел: форма получила его, когда у сервера был CLAUDE_CLI_MODEL=sonnet[1m],
    // а теперь сервер принимает только псевдонимы. Подменяется лишь ответ списка в браузере;
    // запрос создания идёт на настоящий сервер.
    await page.route("**/api/providers", async (route) => {
      const response = await route.fetch();
      const json = (await response.json()) as {
        providers: { id: string; models: string[] }[];
      };
      for (const provider of json.providers) {
        if (provider.id === "claude_code_cli")
          provider.models.push("sonnet[1m]");
      }
      await route.fulfill({ response, json });
    });
    const before = await gameIdsOf(request, "dev-5");

    await button(page, "Создать игру").click();
    await pickProvider(page, /Claude Code на этом компьютере/);
    await page
      .getByRole("combobox", { name: "Модель" })
      .selectOption("sonnet[1m]");
    const sent = await submitForm(page);

    // Скобки схема запроса пропускает (псевдонимы CLI), отказывает проверка списка моделей.
    expect(sent.postDataJSON()).toMatchObject({
      provider: "claude_code_cli",
      model: "sonnet[1m]",
    });
    expect((await sent.response())?.status()).toBe(422);
    await expect(page.getByRole("alert")).toContainText(
      "Модель для Claude Code — одна из списка: opus, sonnet, haiku.",
    );
    // Форма на месте и готова к другому выбору; игры не прибавилось.
    await expect(button(page, "Создать")).toBeEnabled();
    await expect(page.getByText("Позовите друзей")).toHaveCount(0);
    expect(await gameIdsOf(request, "dev-5")).toEqual(before);
  } finally {
    await Promise.all(players.map((player) => player.context.close()));
  }
});
