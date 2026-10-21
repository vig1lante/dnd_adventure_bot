import type {
  Browser,
  BrowserContext,
  Locator,
  Page,
  Request,
  Response,
} from "@playwright/test";
import { expectFitsPhone, layoutProblems } from "./layout.ts";
import { expect, test } from "./server.ts";

// Создание героя на настоящем сервере (финальное покрытие B8, решения владельца R1, R4, R9, R10).
// Юнит-тесты экранов идут на заглушках сервера и держат в руках форму ответов, описанную в плане.
// Здесь те же экраны разговаривают с настоящим `dnd-server` (dev, `FakeProvider`, временная база)
// через прокси Vite: если форма ответа сервера разойдётся с типами клиента, тест это покажет.
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

/** Выбор карточки-радио: сама кнопка скрыта стилем, щёлкают по подписи. */
async function pickCard(page: Page, group: string, label: RegExp) {
  const radiogroup = page.getByRole("radiogroup", { name: group });
  await radiogroup.locator("label", { hasText: label }).first().click();
  await expect(
    radiogroup.getByRole("radio", { name: label, checked: true }),
  ).toHaveCount(1);
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

/** Подробный герой: дварф, варвар, солдат; характеристики раскладывает сервер. */
async function createDetailedHero(host: Player) {
  const { page } = host;
  await button(page, "Создать персонажа").click();
  await expect(page.getByText("Создание героя")).toBeVisible();
  await expectFitsPhone(page, "выбор способа создания героя");
  await button(page, "Подробно").click();

  await pickCard(page, "Вид", /Дварф/);
  await expectFitsPhone(page, "шаг «Вид»");
  await button(page, "Далее").click();
  await pickCard(page, "Класс", /Варвар/);
  await expectFitsPhone(page, "шаг «Класс»");
  await button(page, "Далее").click();
  await pickCard(page, "Предыстория", /Солдат/);
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

  // Навыки класса: варвар выбирает два.
  const skills = page.getByRole("group", { name: /Навыки класса/ });
  await skills.getByRole("checkbox").nth(0).check({ force: true });
  await skills.getByRole("checkbox").nth(1).check({ force: true });
  await expectFitsPhone(page, "шаг «Навыки»");
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

  const sent = await requestOf(page, "POST", "/characters", () =>
    button(page, "Создать героя").click(),
  );
  const created = await (await sent.response())?.json();
  await expect(page.getByText("Ваш герой")).toBeVisible();
  return { sent, created: created as { character: HeroJson }, scores };
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

    const { sent, created, scores } =
      await test.step("Подробно: раскладка очков от сервера, «Помоги написать», создание", () =>
        createDetailedHero(host));
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

    await test.step("«Изменить героя»: форма открывается с прежним выбором (R10)", async () => {
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
      await expect(
        page
          .getByRole("radiogroup", { name: "Вид" })
          .getByRole("radio", { name: /Дварф/ }),
      ).toBeChecked();
      await button(page, "Далее").click();
      await expect(
        page
          .getByRole("radiogroup", { name: "Класс" })
          .getByRole("radio", { name: /Варвар/ }),
      ).toBeChecked();
      await button(page, "Далее").click();
      await expect(
        page
          .getByRole("radiogroup", { name: "Предыстория" })
          .getByRole("radio", { name: /Солдат/ }),
      ).toBeChecked();
      await button(page, "Далее").click();
      // Те же очки, которые разложил сервер при создании.
      await expect(page.getByText("Осталось 0 из 27")).toBeVisible();
      expect(await pointBuyScores(page)).toEqual(scores);
      await button(page, "Далее").click();
      await button(page, "Далее").click();
      await button(page, "Далее").click();
      await button(page, "Далее").click();
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

      await button(page, "Быстро").click();
      await page.getByLabel("Класс", { exact: true }).selectOption("rogue");
      await page.getByLabel("Вид", { exact: true }).selectOption("halfling");
      await page.getByLabel("Опиши героя одной строкой").fill(FRIEND_CONCEPT);
      await button(page, "Создать героя").click();
      await expect(page.getByText("Ваш герой")).toBeVisible();

      // Теперь выбор есть: форма «Быстро» открывается заполненной (имя не давали — пусто).
      await button(page, "Изменить").click();
      await button(page, "Изменить").click();
      await expect(page.getByLabel("Опиши героя одной строкой")).toHaveValue(
        FRIEND_CONCEPT,
      );
      await expect(page.getByLabel("Класс", { exact: true })).toHaveValue(
        "rogue",
      );
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
    await button(page, "Создать персонажа").click();
    await button(page, "Подробно").click();

    await pickCard(page, "Вид", /Эльф/);
    await button(page, "Далее").click();
    await pickCard(page, "Класс", /Волшебник/);
    await button(page, "Далее").click();
    await pickCard(page, "Предыстория", /Мудрец/);
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

    // Шаг заклинаний строится из данных сервера: три заговора и шесть заклинаний.
    await expect(page.getByText(/Шаг 7 из 10: Заклинания/)).toBeVisible();
    const cantrips = page.getByRole("group", { name: "Заговоры: выберите 3" });
    const spells = page.getByRole("group", { name: "Заклинания: выберите 6" });
    await expect(button(page, "Далее")).toBeDisabled();
    for (let index = 0; index < 3; index += 1) {
      await cantrips.getByRole("checkbox").nth(index).check({ force: true });
    }
    // Набрав норму, остальные заговоры нельзя отметить.
    await expect(cantrips.getByRole("checkbox").nth(3)).toBeDisabled();
    for (let index = 0; index < 6; index += 1) {
      await spells.getByRole("checkbox").nth(index).check({ force: true });
    }
    await expectFitsPhone(page, "шаг «Заклинания»");
    const chosenCantrips = await checkedValues(cantrips);
    const chosenSpells = await checkedValues(spells);
    expect(chosenCantrips).toHaveLength(3);
    expect(chosenSpells).toHaveLength(6);
    await button(page, "Далее").click(); // снаряжение
    await button(page, "Далее").click(); // характер
    await page.getByLabel("Имя", { exact: true }).fill("Мирель");
    await button(page, "Далее").click(); // история

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

    // «Изменить героя»: выбранные заклинания вернулись в форму из ответа `characters/mine`.
    await button(page, "Изменить").click();
    await button(page, "Изменить").click();
    for (let step = 0; step < 6; step += 1) {
      await button(page, "Далее").click();
    }
    await expect(page.getByText(/Шаг 7 из 10: Заклинания/)).toBeVisible();
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
