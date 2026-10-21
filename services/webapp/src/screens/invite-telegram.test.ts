import { init, mockTelegramEnv } from "@tma.js/sdk-react";
import { canShareInTelegram, shareInTelegram } from "./invite";

const INIT_DATA =
  "query_id=AAHdF6IQ&user=%7B%22id%22%3A279058397%2C%22first_name%22%3A%22Lira%22%7D" +
  "&auth_date=1662771648&signature=sig&hash=abc123";

// Отдельный файл: SDK запоминает, в Telegram ли он, при первом вопросе, и среда «вне
// Telegram» проверяется в invite.test.ts.
test("внутри Telegram отправка есть и открывает выбор чата со ссылкой и текстом", () => {
  const posted: { name: string; params: unknown }[] = [];
  mockTelegramEnv({
    launchParams: {
      tgWebAppData: INIT_DATA,
      tgWebAppVersion: "8.0",
      tgWebAppPlatform: "tdesktop",
    },
    onEvent: (event, next) => {
      posted.push(event);
      next();
    },
  });
  init();

  expect(canShareInTelegram()).toBe(true);
  shareInTelegram(
    "https://t.me/dnd_table_bot?startapp=ABC123",
    "Играем в «Склеп»",
  );

  const share = posted.find((event) => event.name === "web_app_open_tg_link");
  const path = (share?.params as { path_full: string }).path_full;
  const query = new URLSearchParams(path.split("?")[1]);
  expect(path.startsWith("/share/url?")).toBe(true);
  expect(query.get("url")).toBe("https://t.me/dnd_table_bot?startapp=ABC123");
  expect(query.get("text")).toBe("Играем в «Склеп»");
});
