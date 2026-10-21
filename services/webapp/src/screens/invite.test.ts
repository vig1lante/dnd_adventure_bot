import { canShareInTelegram, copyText, inviteLink } from "./invite";

describe("inviteLink: ссылка на Mini App бота с кодом игры", () => {
  test("https://t.me/<бот>?startapp=<код>", () => {
    expect(inviteLink("dnd_table_bot", "ABC123")).toBe(
      "https://t.me/dnd_table_bot?startapp=ABC123",
    );
  });

  test.each([
    ["бот не настроен", null, "ABC123"],
    ["пустое имя бота", "", "ABC123"],
    ["имя с пробелом", "dnd table", "ABC123"],
    ["имя с символами адреса", "bot/../evil?x=1", "ABC123"],
    ["слишком короткое имя", "bot", "ABC123"],
    ["имя с «@»", "@dnd_table_bot", "ABC123"],
    ["код не из 6 знаков", "dnd_table_bot", "ABC12"],
    ["код с символами адреса", "dnd_table_bot", "AB&C12"],
    ["код в нижнем регистре", "dnd_table_bot", "abc123"],
  ] as const)("%s — ссылки нет", (_, bot, code) => {
    expect(inviteLink(bot, code)).toBeNull();
  });
});

describe("copyText: копирование в буфер обмена", () => {
  test("текст уходит в буфер, копирование удалось", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyText("https://t.me/x")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("https://t.me/x");
  });

  test("браузер отказал — копирование не удалось, исключения нет", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyText("x")).resolves.toBe(false);
  });

  test("буфера обмена нет (небезопасный контекст) — копирование не удалось", async () => {
    vi.stubGlobal("navigator", {});

    await expect(copyText("x")).resolves.toBe(false);
  });
});

// Доступность SDK считается один раз за жизнь процесса теста, поэтому проверка «внутри
// Telegram» лежит в отдельном файле (invite-telegram.test.ts): в одном файле честно
// проверить можно только одну среду.
describe("отправка через Telegram", () => {
  test("вне Telegram отправки нет", () => {
    expect(canShareInTelegram()).toBe(false);
  });
});
