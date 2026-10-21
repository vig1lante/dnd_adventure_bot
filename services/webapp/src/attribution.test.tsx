import { render, screen } from "@testing-library/react";
import App from "./App";
import { SRD_ATTRIBUTION } from "./attribution";
import { readSource } from "./styles/read-source";
import { fakeServer } from "./test-setup";

// Спека §2: «В приложении обязательна строка атрибуции» SRD 5.2.1 (лицензия CC BY 4.0), а
// мета блока B8 уточняет: текст один на README и Mini App, точный текст — со страницы
// «Legal Information». Тесты закрывают требование: источник текста (`attribution.ts`)
// совпадает с цитатой README, а строка видна игроку и на экране входа, и на «Мои игры».

const README_FILE = "../../../../README.md";
const INDEX_HTML = "../../index.html";

/** Текст атрибуции из README: цитата под заголовком «Лицензия и атрибуция SRD». */
async function attributionOfReadme(): Promise<string> {
  const readme = await readSource(README_FILE);
  const section = readme.split(/^## Лицензия и атрибуция SRD.*$/m)[1] ?? "";
  const quote = section
    .split("\n")
    .filter((line) => line.startsWith(">"))
    .map((line) => line.replace(/^>\s?/, ""))
    .join(" ");
  return quote.replace(/\s+/g, " ").trim();
}

const oneLine = (text: string | null) => (text ?? "").replace(/\s+/g, " ");

/**
 * Всё, что игрок видит на главном экране: страница приложения и статичная разметка вокруг неё
 * (`index.html`: строку можно положить и туда, она видна на любом экране). Комментарии и скрипты
 * в счёт не идут.
 */
async function visibleText(): Promise<string> {
  const shell = (await readSource(INDEX_HTML))
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, " ")
    .replace(/<[^>]+>/g, " ");
  return oneLine(`${document.body.textContent} ${shell}`);
}

async function openMyGames() {
  window.history.replaceState(null, "", "/?dev=dev-1");
  const server = fakeServer({
    "GET /api/config": {
      json: { env: "dev", dev_login: true, bot_username: null },
    },
    "POST /api/auth/dev": {
      json: {
        token: "tok-dev-1",
        user: { id: "u1", name: "dev-1", can_create_games: false },
        start_param: null,
      },
    },
    "GET /api/games": { json: [] },
  });
  render(
    <App
      deps={{
        fetchImpl: server.fetch,
        readInitData: () => null,
        readStartParam: () => null,
        setupTelegram: () => {},
      }}
    />,
  );
  await screen.findByRole("heading", { name: "Мои игры" });
}

test("в README есть текст атрибуции SRD 5.2.1 (опора для сверки со строкой в приложении)", async () => {
  const text = await attributionOfReadme();

  expect(text).toContain("System Reference Document 5.2.1");
  expect(text).toContain("Wizards of the Coast LLC");
  expect(text).toContain("Creative Commons Attribution 4.0 International");
});

test("источник текста в приложении слово в слово совпадает с цитатой README", async () => {
  expect(SRD_ATTRIBUTION).toBe(await attributionOfReadme());
});

describe("строка атрибуции SRD 5.2.1 в приложении (спека §2)", () => {
  test("на главном экране после входа есть строка атрибуции", async () => {
    await openMyGames();

    const shown = await visibleText();
    expect(shown).toContain("System Reference Document 5.2.1");
    expect(shown).toContain("Creative Commons Attribution 4.0");
  });

  test("текст в приложении тот же, что в README (один источник)", async () => {
    const expected = await attributionOfReadme();
    await openMyGames();

    expect(await visibleText()).toContain(expected);
  });

  test("строка одна на экране и помечена английским языком (для чтения вслух)", async () => {
    await openMyGames();

    const line = screen.getByText(SRD_ATTRIBUTION);
    expect(line).toHaveAttribute("lang", "en");
  });

  test("строка видна и на экране входа, до выхода в игру", async () => {
    window.history.replaceState(null, "", "/");
    const server = fakeServer({
      "GET /api/config": {
        json: { env: "prod", dev_login: false, bot_username: null },
      },
    });
    render(
      <App
        deps={{
          fetchImpl: server.fetch,
          readInitData: () => null,
          readStartParam: () => null,
          setupTelegram: () => {},
        }}
      />,
    );
    await screen.findByText("Откройте приложение из Telegram");

    expect(screen.getByText(SRD_ATTRIBUTION)).toBeInTheDocument();
  });
});
