import { fireEvent, render, screen, within } from "@testing-library/react";
import type { JournalEntry } from "../api/types";
import { journalEntry, startJournal } from "../components/fixtures";
import { OUTDATED_WORDS } from "./character/fixtures";
import { Journal } from "./Journal";

interface JournalProps {
  gameId: string;
  userId: string;
  entries: JournalEntry[];
  active: boolean;
}

const SEEN_KEY = "dnd-journal-seen:g1:u1";

/** Дневник на экране; `update` перерисовывает его с новыми свойствами, как это делает экран игры. */
function renderJournal(overrides: Partial<JournalProps> = {}) {
  let props: JournalProps = {
    gameId: "g1",
    userId: "u1",
    entries: startJournal(),
    active: true,
    ...overrides,
  };
  const view = render(<Journal {...props} />);
  const update = (next: Partial<JournalProps>) => {
    props = { ...props, ...next };
    view.rerender(<Journal {...props} />);
  };
  return { ...view, update };
}

const section = (name: string) =>
  screen.getByRole("tab", { name: new RegExp(`^${name} \\d+$`) });
const open = (name: string) => fireEvent.click(section(name));
const card = (title: string) => screen.getByRole("region", { name: title });
const isNew = (title: string) =>
  within(card(title)).queryByText("новое") !== null;
const storedSeen = (key = SEEN_KEY): unknown => {
  const raw = window.localStorage.getItem(key);
  return raw === null ? null : JSON.parse(raw);
};

describe("разделы дневника", () => {
  test("девять разделов в порядке показа, у каждого — число записей", () => {
    renderJournal();

    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Главное 1",
      "Места 2",
      "Люди 0",
      "Боги 1",
      "Фракции 0",
      "Мир 0",
      "Слухи 0",
      "Улики 0",
      "Предметы 0",
    ]);
  });

  test("сначала открыт «Главное»; выбор раздела показывает только его записи", () => {
    renderJournal();
    expect(section("Главное")).toHaveAttribute("aria-selected", "true");
    expect(card("Главное")).toBeVisible();

    open("Места");

    expect(section("Места")).toHaveAttribute("aria-selected", "true");
    expect(section("Главное")).toHaveAttribute("aria-selected", "false");
    expect(card("Деревня Мирра")).toBeVisible();
    expect(card("Старое кладбище")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Главное" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Келемвор" })).toBeNull();
  });

  test("записи раздела — в порядке появления", () => {
    renderJournal();

    open("Места");

    const titles = within(screen.getByRole("tabpanel"))
      .getAllByRole("heading")
      .map((heading) => heading.textContent);
    expect(titles).toEqual(["Деревня Мирра", "Старое кладбище"]);
  });

  test("открытый раздел связан со своей вкладкой", () => {
    renderJournal();

    open("Боги");

    const panel = screen.getByRole("tabpanel", { name: "Боги 1" });
    expect(section("Боги")).toHaveAttribute("aria-controls", panel.id);
    expect(
      within(panel).getByText("Бог смерти, страж покоя усопших."),
    ).toBeVisible();
  });

  test("пустой раздел — «Пока ничего не известно»", () => {
    renderJournal();

    open("Фракции");

    expect(
      within(screen.getByRole("tabpanel")).getByText("Пока ничего не известно"),
    ).toBeVisible();
  });

  test("пустой дневник: в каждом разделе ноль и подсказка", () => {
    renderJournal({ entries: [] });

    expect(section("Главное")).toHaveTextContent("Главное 0");
    expect(screen.getByText("Пока ничего не известно")).toBeVisible();
  });

  test("запись неизвестного раздела не ломает дневник и нигде не показана", () => {
    const strange = {
      ...journalEntry("location:tower", { title: "Башня" }),
      section: "secrets",
    } as unknown as JournalEntry;

    renderJournal({ entries: [...startJournal(), strange] });

    expect(section("Места")).toHaveTextContent("Места 2");
    expect(screen.queryByText("Башня")).toBeNull();
  });
});

describe("карточка записи", () => {
  test("название, абзацы текста и раунд, когда узнали", () => {
    renderJournal({
      entries: [
        journalEntry("npc:elder", {
          title: "Староста Хорн",
          text: "Седой и упрямый.\n\nБоится склепа.",
          turn: 3,
        }),
      ],
    });

    open("Люди");

    const elder = within(card("Староста Хорн"));
    expect(elder.getByText("Седой и упрямый.")).toBeVisible();
    expect(elder.getByText("Боится склепа.")).toBeVisible();
    expect(elder.getByText("узнали в раунде 3")).toBeVisible();
  });

  test("короткий слух: текст совпадает с названием — фраза не стоит в карточке дважды", () => {
    const rumor = "Граф Мортен велел похоронить с собой всё своё золото.";
    renderJournal({
      entries: [journalEntry("rumor:1", { title: rumor, text: rumor })],
    });

    open("Слухи");

    expect(screen.getAllByText(rumor)).toHaveLength(1);
    expect(
      within(card(rumor)).getByText("известно с начала игры"),
    ).toBeVisible();
  });

  test("совпадение с названием сверяется без лишних пробелов, а дополнения остаются", () => {
    renderJournal({
      entries: [
        journalEntry("clue:mud", {
          title: "Следы грязи на пороге.",
          text: "Следы  грязи\nна пороге.\n\nГрязь — с болота за кладбищем.",
          turn: 2,
        }),
      ],
    });

    open("Улики");

    const clue = within(card("Следы грязи на пороге."));
    expect(
      clue.queryByText(/Следы\s+грязи\s+на/, { selector: "p" }),
    ).toBeNull();
    expect(clue.getByText("Грязь — с болота за кладбищем.")).toBeVisible();
  });

  test("длинная запись с началом-названием («…») показывает текст целиком", () => {
    renderJournal({
      entries: [
        journalEntry("rumor:2", {
          title: "В старом склепе по ночам горит свет, и…",
          text: "В старом склепе по ночам горит свет, и сторож боится туда ходить.",
        }),
      ],
    });

    open("Слухи");

    expect(
      screen.getByText(
        "В старом склепе по ночам горит свет, и сторож боится туда ходить.",
      ),
    ).toBeVisible();
  });

  test("известное с начала игры так и подписано, без «раунда 0»", () => {
    renderJournal();

    expect(
      within(card("Главное")).getByText("известно с начала игры"),
    ).toBeVisible();
    expect(screen.queryByText(/раунде 0/)).toBeNull();
  });

  test("место, где отряд уже был: «были здесь: раунд N» вместо «узнали»", () => {
    renderJournal({
      entries: [
        journalEntry("location:cemetery", {
          title: "Старое кладбище",
          revision: 2,
          visited_turn: 2,
        }),
        journalEntry("location:crypt", {
          title: "Склеп",
          turn: 4,
          visited_turn: 4,
        }),
      ],
    });

    open("Места");

    const cemetery = within(card("Старое кладбище"));
    expect(cemetery.getByText("были здесь: раунд 2")).toBeVisible();
    expect(cemetery.queryByText(/узнали|известно с начала/)).toBeNull();
    expect(
      within(card("Склеп")).getByText("были здесь: раунд 4"),
    ).toBeVisible();
  });

  test("место, где отряд был с самого начала, — без «раунда 0»", () => {
    renderJournal({
      entries: [
        journalEntry("location:village", {
          title: "Деревня Мирра",
          visited_turn: 0,
        }),
      ],
    });

    open("Места");

    expect(
      within(card("Деревня Мирра")).getByText("были здесь с начала игры"),
    ).toBeVisible();
    expect(screen.queryByText(/раунд 0/)).toBeNull();
  });

  test("дополненная запись: под «узнали» — «дополнено в раунде N»", () => {
    renderJournal({
      entries: [
        journalEntry("npc:elder", {
          title: "Староста Хорн",
          text: "Седой и упрямый.\n\nБоится склепа.",
          turn: 1,
          revision: 2,
          updated_turn: 4,
        }),
        journalEntry("location:crypt", {
          title: "Склеп",
          turn: 2,
          revision: 3,
          visited_turn: 3,
          updated_turn: 5,
        }),
      ],
    });

    open("Люди");
    const elder = within(card("Староста Хорн"));
    expect(elder.getByText("узнали в раунде 1")).toBeVisible();
    expect(elder.getByText("дополнено в раунде 4")).toBeVisible();

    open("Места");
    const crypt = within(card("Склеп"));
    expect(crypt.getByText("были здесь: раунд 3")).toBeVisible();
    expect(crypt.getByText("дополнено в раунде 5")).toBeVisible();
  });

  test("дополнение в самом начале игры — без «раунда 0»", () => {
    renderJournal({
      entries: [
        journalEntry("intro", {
          title: "Главное",
          revision: 2,
          updated_turn: 0,
        }),
      ],
    });

    expect(
      within(card("Главное")).getByText("дополнено в начале игры"),
    ).toBeVisible();
    expect(screen.queryByText(/раунд(е)? 0/)).toBeNull();
  });

  test("не дополнялась (null) или старый сервер без поля — строки «дополнено» нет", () => {
    const old = journalEntry("npc:smith", { title: "Кузнец", revision: 2 });
    delete old.updated_turn;
    renderJournal({
      entries: [
        journalEntry("npc:elder", { title: "Староста Хорн", revision: 2 }),
        old,
      ],
    });

    open("Люди");

    expect(screen.queryByText(/дополнено/)).toBeNull();
    expect(
      within(card("Кузнец")).getByText("известно с начала игры"),
    ).toBeVisible();
  });

  test("место, где отряд ещё не был, подписано как обычная запись", () => {
    renderJournal();

    open("Места");

    expect(
      within(card("Деревня Мирра")).getByText("известно с начала игры"),
    ).toBeVisible();
    expect(screen.queryByText(/были здесь/)).toBeNull();
  });

  test("личная запись героя показана как обычная: отбор адресата — дело сервера", () => {
    renderJournal({
      entries: [
        journalEntry("npc:elder", {
          title: "Староста Хорн",
          text: "Прячет ключ от склепа.",
          audience: "pc_lira",
          turn: 2,
        }),
      ],
    });

    open("Люди");

    const elder = within(card("Староста Хорн"));
    expect(elder.getByText("Прячет ключ от склепа.")).toBeVisible();
    expect(elder.getByText("узнали в раунде 2")).toBeVisible();
    expect(elder.getByText("новое")).toBeVisible();
  });

  test("разметка в названии и тексте остаётся текстом", () => {
    renderJournal({
      entries: [
        journalEntry("npc:elder", {
          title: "<b>Хорн</b>",
          text: "<img src=x onerror=alert(1)>",
        }),
      ],
    });

    open("Люди");

    expect(card("<b>Хорн</b>")).toBeVisible();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
    expect(document.querySelector("img, b")).toBeNull();
  });
});

describe("«новое»", () => {
  test("до открытия раздела записи помечены «новое»", () => {
    renderJournal();

    expect(isNew("Главное")).toBe(true);
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(true);
    expect(isNew("Старое кладбище")).toBe(true);
  });

  test("пометка видна, пока игрок в разделе, и гаснет, когда он вернётся в него снова", () => {
    renderJournal();

    open("Места");
    open("Главное");

    expect(isNew("Главное")).toBe(false);
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);
    expect(isNew("Старое кладбище")).toBe(false);
  });

  test("неоткрытый раздел остаётся новым", () => {
    renderJournal();

    open("Места");
    open("Главное");
    open("Боги");

    expect(isNew("Келемвор")).toBe(true);
  });

  test("счётчик раздела с новыми записями выделен, пока они новые", () => {
    renderJournal();
    const count = (name: string) => within(section(name)).getByText(/^\d+$/);

    expect(count("Места")).toHaveClass("journal-section__count--new");
    expect(count("Фракции")).not.toHaveClass("journal-section__count--new");

    open("Места");
    open("Главное");

    expect(count("Места")).not.toHaveClass("journal-section__count--new");
    expect(count("Боги")).toHaveClass("journal-section__count--new");
  });

  test("увиденное хранится в localStorage: карта id записи → ревизия", () => {
    renderJournal();

    open("Места");

    expect(storedSeen()).toEqual({
      "intro@all": 1,
      "location:village@all": 1,
      "location:cemetery@all": 1,
    });
  });

  test("после перезагрузки увиденное не помечено, неувиденное — помечено", () => {
    const first = renderJournal();
    open("Места");
    first.unmount();

    renderJournal();

    expect(isNew("Главное")).toBe(false);
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);
    open("Боги");
    expect(isNew("Келемвор")).toBe(true);
  });

  test("дополнение записи (рост revision) снова помечает её «новое»", () => {
    const { update } = renderJournal();
    open("Места");
    open("Главное");
    expect(isNew("Главное")).toBe(false);
    open("Места");

    const entries = startJournal();
    entries[0] = {
      ...entries[0],
      text: `${entries[0].text}\n\nСтароста обещал награду.`,
      revision: 2,
    };
    update({ entries });
    open("Главное");

    expect(isNew("Главное")).toBe(true);
    expect(
      within(card("Главное")).getByText("Староста обещал награду."),
    ).toBeVisible();
    expect(storedSeen()).toMatchObject({ "intro@all": 2 });
  });

  test("запись, пришедшая в открытый раздел, помечена, пока игрок не ушёл из раздела", () => {
    const { update } = renderJournal();
    open("Места");

    update({
      entries: [
        ...startJournal(),
        journalEntry("location:crypt", {
          title: "Склеп",
          turn: 2,
          visited_turn: 2,
        }),
      ],
    });

    expect(isNew("Склеп")).toBe(true);
    expect(storedSeen()).toMatchObject({ "location:crypt@all": 1 });
    open("Люди");
    open("Места");
    expect(isNew("Склеп")).toBe(false);
  });

  test("пока вкладка дневника закрыта, ничего не отмечается", () => {
    const { update } = renderJournal({ active: false });

    expect(storedSeen()).toBeNull();

    update({ active: true });
    expect(isNew("Главное")).toBe(true);
    expect(storedSeen()).toEqual({ "intro@all": 1 });

    // Игрок ушёл на другую вкладку игры и вернулся: открытый раздел уже увиден.
    update({ active: false });
    update({ active: true });
    expect(isNew("Главное")).toBe(false);
  });

  test("увиденное у каждой игры и у каждого игрока своё", () => {
    const first = renderJournal();
    open("Места");
    first.unmount();

    const other = renderJournal({ userId: "u2" });
    expect(isNew("Главное")).toBe(true);
    other.unmount();

    renderJournal({ gameId: "g2" });
    expect(isNew("Главное")).toBe(true);
    expect(storedSeen("dnd-journal-seen:g2:u1")).toEqual({ "intro@all": 1 });
    expect(storedSeen("dnd-journal-seen:g1:u2")).toEqual({ "intro@all": 1 });
  });

  test("смена игры на том же экране не переносит отметки в чужую игру", () => {
    const { update } = renderJournal();
    open("Места");

    update({ gameId: "g2" });

    expect(isNew("Главное")).toBe(true);
    expect(storedSeen("dnd-journal-seen:g2:u1")).toEqual({ "intro@all": 1 });
  });

  test.each([
    ["не JSON", "{не json"],
    ["не карта", "[1, 2]"],
    ["null", "null"],
    ["число", "5"],
    ["строка", '"intro@all"'],
    ["логическое", "true"],
  ])("испорченная запись в хранилище (%s) — всё снова новое", (_name, raw) => {
    window.localStorage.setItem(SEEN_KEY, raw);

    renderJournal();

    expect(isNew("Главное")).toBe(true);
    expect(storedSeen()).toEqual({ "intro@all": 1 });
  });

  test("в карте берутся только числовые ревизии", () => {
    window.localStorage.setItem(
      SEEN_KEY,
      JSON.stringify({ "intro@all": "1", "location:village@all": 1 }),
    );

    renderJournal();
    expect(isNew("Главное")).toBe(true);
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);
    expect(isNew("Старое кладбище")).toBe(true);
  });

  test("хранилище недоступно (частный режим): дневник работает, всё новое", () => {
    const denied = () => {
      throw new DOMException("Доступ запрещён", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);

    renderJournal();
    open("Места");

    expect(isNew("Деревня Мирра")).toBe(true);
    expect(section("Главное")).toBeInTheDocument();
  });
});

describe("тексты дневника", () => {
  test("нет «фут» и «вид» ни в одном разделе", () => {
    renderJournal();

    for (const tab of screen.getAllByRole("tab")) {
      fireEvent.click(tab);
      expect(document.body.textContent).not.toMatch(OUTDATED_WORDS);
    }
  });

  test("нет «фут» и «вид» ни в одной подписи: раунды, места, пустой раздел, «новое» и подписи доступности", () => {
    renderJournal({
      entries: [
        journalEntry("intro", { title: "Главное" }),
        journalEntry("location:village", { title: "Деревня", visited_turn: 0 }),
        journalEntry("location:crypt", {
          title: "Склеп",
          turn: 3,
          visited_turn: 5,
        }),
        journalEntry("npc:elder", { title: "Староста", turn: 4 }),
      ],
    });

    let shown = "";
    for (const tab of screen.getAllByRole("tab")) {
      fireEvent.click(tab);
      shown += document.body.textContent ?? "";
      for (const node of document.querySelectorAll("[aria-label]")) {
        shown += node.getAttribute("aria-label") ?? "";
      }
    }

    // Проверка видела все варианты подписей, а не только часть.
    for (const line of [
      "известно с начала игры",
      "были здесь с начала игры",
      "были здесь: раунд 5",
      "узнали в раунде 4",
      "Пока ничего не известно",
      "новое",
      "Разделы дневника",
    ]) {
      expect(shown).toContain(line);
    }
    expect(shown).not.toMatch(OUTDATED_WORDS);
  });
});

describe("разделы дневника: границы", () => {
  test("пустой дневник целиком: в каждом из девяти разделов 0 и подсказка, пометок нет, хранилище не тронуто", () => {
    renderJournal({ entries: [] });

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(9);
    for (const tab of tabs) {
      fireEvent.click(tab);
      expect(tab).toHaveTextContent(/^\S+ 0$/);
      expect(
        within(screen.getByRole("tabpanel")).getByText(
          "Пока ничего не известно",
        ),
      ).toBeVisible();
    }
    expect(document.querySelector(".journal-section__count--new")).toBeNull();
    expect(screen.queryByText("новое")).toBeNull();
    // Отмечать нечего — пустую карту в хранилище не пишем.
    expect(storedSeen()).toBeNull();
  });

  test("запись неизвестного раздела не входит ни в один счётчик и не попадает в отметки", () => {
    const strange = {
      ...journalEntry("location:tower", { title: "Башня" }),
      section: "secrets",
    } as unknown as JournalEntry;

    renderJournal({ entries: [strange, ...startJournal()] });

    const total = screen
      .getAllByRole("tab")
      .reduce(
        (sum, tab) => sum + Number(/(\d+)$/.exec(tab.textContent ?? "")?.[1]),
        0,
      );
    expect(total).toBe(startJournal().length);
    for (const tab of screen.getAllByRole("tab")) {
      fireEvent.click(tab);
      expect(screen.queryByText("Башня")).toBeNull();
    }
    expect(storedSeen()).not.toHaveProperty(["location:tower@all"]);
    expect(storedSeen()).toMatchObject({ "deity:kelemvor@all": 1 });
  });

  test("счётчики следуют за дневником: новая запись, дополнение, личная запись, откат", () => {
    const { update } = renderJournal();
    const label = (name: string) => section(name).textContent;
    const crypt = journalEntry("location:crypt", {
      title: "Склеп",
      turn: 2,
      visited_turn: 2,
    });

    // Новая запись — счётчик растёт.
    update({ entries: [...startJournal(), crypt] });
    expect(label("Места")).toBe("Места 3");

    // Дополнение той же записи (тот же id) — это не новая запись.
    const grown = startJournal();
    grown[1] = {
      ...grown[1],
      text: `${grown[1].text}\n\nНа площади повесили объявление.`,
      revision: 2,
    };
    update({ entries: [...grown, crypt] });
    expect(label("Места")).toBe("Места 3");

    // Личная запись о той же деревне — отдельная запись с тем же названием.
    const personal = journalEntry("location:village", {
      title: "Деревня Мирра",
      text: "Лира знает тайный ход под мельницей.",
      audience: "pc_lira",
      turn: 2,
    });
    update({ entries: [...grown, crypt, personal] });
    expect(label("Места")).toBe("Места 4");
    open("Места");
    expect(
      screen.getAllByRole("region", { name: "Деревня Мирра" }),
    ).toHaveLength(2);
    expect(
      screen.getByText("Лира знает тайный ход под мельницей."),
    ).toBeVisible();

    // Откат хода убрал места: счёт падает до нуля, раздел пуст.
    update({
      entries: startJournal().filter((entry) => entry.section !== "places"),
    });
    expect(label("Места")).toBe("Места 0");
    expect(
      within(screen.getByRole("tabpanel")).getByText("Пока ничего не известно"),
    ).toBeVisible();
    expect(label("Главное")).toBe("Главное 1");
  });
});

describe("разделы дневника: доступность", () => {
  test("набор «Разделы дневника» — девять вкладок-кнопок; Tab ведёт только на выбранную, к остальным — стрелки", () => {
    renderJournal();

    const tabs = within(
      screen.getByRole("tablist", { name: "Разделы дневника" }),
    ).getAllByRole("tab");
    expect(tabs).toHaveLength(9);
    for (const tab of tabs) {
      // Обычная кнопка: Enter и пробел нажимают её сами.
      expect(tab.tagName).toBe("BUTTON");
      expect(tab).toHaveAttribute("type", "button");
      expect(tab).toBeEnabled();
    }
    // «Бегущий» tabindex, как у вкладок игры: в порядке табуляции один выбранный раздел.
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([
      0, -1, -1, -1, -1, -1, -1, -1, -1,
    ]);
  });

  test("выбор раздела оставляет фокус на нажатой вкладке", () => {
    renderJournal();

    section("Боги").focus();
    fireEvent.click(section("Боги"));

    expect(section("Боги")).toHaveFocus();
    expect(section("Боги")).toHaveAttribute("aria-selected", "true");
  });

  test("в любом разделе выбрана ровно одна вкладка, панель подписана ею и управляется только ею", () => {
    renderJournal();

    for (const tab of screen.getAllByRole("tab")) {
      fireEvent.click(tab);

      const all = screen.getAllByRole("tab");
      const panel = screen.getByRole("tabpanel");
      expect(
        all.filter((node) => node.getAttribute("aria-selected") === "true"),
      ).toEqual([tab]);
      expect(all.filter((node) => node.hasAttribute("aria-controls"))).toEqual([
        tab,
      ]);
      expect(tab).toHaveAttribute("aria-controls", panel.id);
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
      expect(panel).toHaveAccessibleName(tab.textContent ?? "");
    }
  });
});

describe("разделы дневника: клавиатура", () => {
  const press = (name: string, key: string) =>
    fireEvent.keyDown(section(name), { key });
  const expectOpen = (name: string) => {
    expect(section(name)).toHaveAttribute("aria-selected", "true");
    expect(section(name)).toHaveFocus();
    expect(section(name)).toHaveAttribute("tabindex", "0");
  };

  test("в порядке табуляции только выбранный раздел; щелчок переносит его", () => {
    renderJournal();

    fireEvent.click(section("Боги"));

    expect(section("Боги")).toHaveAttribute("tabindex", "0");
    expect(section("Главное")).toHaveAttribute("tabindex", "-1");
    expect(
      screen
        .getAllByRole("tab")
        .filter((tab) => tab.getAttribute("tabindex") === "0"),
    ).toEqual([section("Боги")]);
  });

  test("стрелки вправо и влево переключают разделы по кругу и переносят фокус", () => {
    renderJournal();
    section("Главное").focus();

    press("Главное", "ArrowRight");
    expectOpen("Места");
    press("Места", "ArrowLeft");
    expectOpen("Главное");
    press("Главное", "ArrowLeft");
    expectOpen("Предметы");
    press("Предметы", "ArrowRight");
    expectOpen("Главное");
  });

  test("Home — к первому разделу, End — к последнему", () => {
    renderJournal();
    fireEvent.click(section("Люди"));

    press("Люди", "End");
    expectOpen("Предметы");
    press("Предметы", "Home");
    expectOpen("Главное");
  });

  test("свои клавиши не прокручивают страницу, чужие остаются браузеру", () => {
    renderJournal();

    // fireEvent возвращает false, если обработчик отменил действие браузера.
    expect(press("Главное", "End")).toBe(false);
    expect(press("Предметы", "Home")).toBe(false);
    expect(press("Главное", "ArrowRight")).toBe(false);
    expect(press("Места", "ArrowDown")).toBe(true);
  });

  test.each(["Enter", " ", "a", "ArrowDown", "ArrowUp", "Tab"])(
    "клавиша «%s» раздел не переключает",
    (key) => {
      renderJournal();
      fireEvent.click(section("Люди"));

      press("Люди", key);

      expect(section("Люди")).toHaveAttribute("aria-selected", "true");
    },
  );

  test("раздел, открытый стрелкой, становится увиденным так же, как по щелчку", () => {
    renderJournal();

    press("Главное", "ArrowRight");

    expect(isNew("Деревня Мирра")).toBe(true);
    expect(storedSeen()).toMatchObject({
      "location:village@all": 1,
      "location:cemetery@all": 1,
    });
    press("Места", "ArrowLeft");
    press("Главное", "ArrowRight");
    expect(isNew("Деревня Мирра")).toBe(false);
  });
});

describe("разделы дневника: «есть новое» для читалки", () => {
  test("раздел с новыми записями описан «есть новое», а имя остаётся «<Раздел> <N>»", () => {
    renderJournal();

    expect(section("Места")).toHaveAccessibleName("Места 2");
    expect(section("Места")).toHaveAccessibleDescription("есть новое");
    expect(section("Боги")).toHaveAccessibleDescription("есть новое");
    expect(section("Фракции")).toHaveAccessibleDescription("");
    expect(section("Фракции")).not.toHaveAttribute("aria-describedby");
  });

  test("увиденный раздел описание теряет вместе с подсветкой счётчика", () => {
    renderJournal();

    fireEvent.click(section("Места"));
    fireEvent.click(section("Главное"));

    expect(section("Места")).toHaveAccessibleDescription("");
    expect(section("Главное")).toHaveAccessibleDescription("");
    expect(section("Боги")).toHaveAccessibleDescription("есть новое");
  });

  test("подсказка — только для читалки: на экране её не видно", () => {
    renderJournal();

    const id = section("Места").getAttribute("aria-describedby") ?? "";
    const hint = document.getElementById(id);
    expect(hint).toHaveTextContent("есть новое");
    expect(hint).not.toBeVisible();
    expect(hint?.closest("[role='tablist']")).toBeNull();
  });
});

describe("отметки после отката", () => {
  test("запись, убранная откатом, вернувшись, снова «новая»", () => {
    const { update } = renderJournal();
    open("Места");
    open("Главное");
    open("Места");
    expect(isNew("Старое кладбище")).toBe(false);

    update({
      entries: startJournal().filter(
        (entry) => entry.ref !== "location:cemetery",
      ),
    });
    expect(storedSeen()).not.toHaveProperty(["location:cemetery@all"]);

    // «Перегенерировать» снова открыл кладбище — той же первой ревизии, но это другая находка.
    update({ entries: startJournal() });
    expect(isNew("Старое кладбище")).toBe(true);
    expect(isNew("Деревня Мирра")).toBe(false);
  });

  test("откат к меньшей ревизии: увиденное опускается до неё, и следующее дополнение снова «новое»", () => {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify({ "intro@all": 2 }));
    const { update } = renderJournal();

    expect(isNew("Главное")).toBe(false);
    expect(storedSeen()).toEqual({ "intro@all": 1 });

    open("Места");
    const entries = startJournal();
    entries[0] = {
      ...entries[0],
      text: `${entries[0].text}\n\nСтароста передумал.`,
      revision: 2,
    };
    update({ entries });
    open("Главное");

    expect(isNew("Главное")).toBe(true);
    expect(storedSeen()).toMatchObject({ "intro@all": 2 });
  });

  test("карта подрезается и при закрытой вкладке дневника", () => {
    window.localStorage.setItem(
      SEEN_KEY,
      JSON.stringify({ "intro@all": 1, "location:gone@all": 3 }),
    );

    renderJournal({ active: false });

    expect(storedSeen()).toEqual({ "intro@all": 1 });
  });

  test("подрезать нечего — хранилище не переписывается", () => {
    window.localStorage.setItem(
      SEEN_KEY,
      JSON.stringify({ "intro@all": 1, "location:village@all": 1 }),
    );
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    const { update } = renderJournal({ active: false });
    update({ entries: [...startJournal()] });

    expect(setItem).not.toHaveBeenCalled();
  });
});

describe("карточка записи: длинные и необычные тексты", () => {
  test("длинная запись из многих дополнений: все абзацы по порядку, пустых абзацев нет", () => {
    const additions = Array.from(
      { length: 8 },
      (_, index) => `Дополнение ${index + 1}: ${"слово ".repeat(30).trim()}`,
    );
    const longWord = "Ы".repeat(150);
    // Лишние пустые строки и строка из пробелов между дополнениями абзацев не дают.
    const text = `${additions.slice(0, 4).join("\n\n")}\n\n\n\n${additions
      .slice(4)
      .join("\n\n")}\n\n  \n\n${longWord}`;
    renderJournal({
      entries: [
        journalEntry("npc:elder", {
          title: "Староста Хорн",
          text,
          revision: 9,
          turn: 7,
        }),
      ],
    });

    open("Люди");

    const paragraphs = Array.from(
      card("Староста Хорн").querySelectorAll(".journal-entry__text"),
      (node) => node.textContent,
    );
    expect(paragraphs).toEqual([...additions, longWord]);
    expect(
      within(card("Староста Хорн")).getByText("узнали в раунде 7"),
    ).toBeVisible();
  });

  test("одиночный перенос строки не делит абзац", () => {
    renderJournal({
      entries: [
        journalEntry("rumor:1", {
          title: "Слух о мельнице",
          text: "Говорят:\nмельник не спит по ночам.",
        }),
      ],
    });

    open("Слухи");

    const paragraphs = card("Слух о мельнице").querySelectorAll(
      ".journal-entry__text",
    );
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0].textContent).toBe(
      "Говорят:\nмельник не спит по ночам.",
    );
  });

  test.each([
    ["скрипт", "<script>window.hacked = true</script>"],
    ["ссылка javascript:", '<a href="javascript:alert(1)">ключ</a>'],
    ["обработчик события", '<svg onload="alert(1)"></svg>'],
    ["markdown", "**жирный** и [ссылка](https://example.com)"],
  ])(
    "разметка (%s) в названии и в дополнении остаётся текстом",
    (_name, markup) => {
      renderJournal({
        entries: [
          journalEntry("clue:ring", {
            title: markup,
            text: `Нашли кольцо.\n\n${markup}`,
          }),
        ],
      });

      open("Улики");

      const panel = screen.getByRole("tabpanel");
      expect(within(panel).getAllByText(markup)).toHaveLength(2);
      expect(
        panel.querySelector("script, a, strong, em, [onload], [href]"),
      ).toBeNull();
      expect(window).not.toHaveProperty("hacked");
    },
  );
});

describe("«новое»: границы", () => {
  const NEW_COUNT = "journal-section__count--new";
  const count = (name: string) => within(section(name)).getByText(/^\d+$/);
  const crypt = () =>
    journalEntry("location:crypt", {
      title: "Склеп",
      turn: 2,
      visited_turn: 2,
    });

  test("дополнение записи в открытом разделе помечено сразу и гаснет после ухода из раздела", () => {
    const { update } = renderJournal();
    open("Места");
    open("Главное");
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);

    const entries = startJournal();
    entries[1] = {
      ...entries[1],
      text: `${entries[1].text}\n\nНа площади повесили объявление.`,
      revision: 2,
    };
    update({ entries });

    expect(isNew("Деревня Мирра")).toBe(true);
    expect(isNew("Старое кладбище")).toBe(false);
    expect(storedSeen()).toMatchObject({ "location:village@all": 2 });
    open("Главное");
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);
  });

  test("запись, пришедшая в другой раздел, подсвечивает его счётчик, а в разделе помечена «новое»", () => {
    const { update } = renderJournal();
    expect(count("Люди")).not.toHaveClass(NEW_COUNT);

    update({
      entries: [
        ...startJournal(),
        journalEntry("npc:elder", { title: "Староста Хорн", turn: 2 }),
      ],
    });

    expect(count("Люди")).toHaveClass(NEW_COUNT);
    expect(storedSeen()).not.toHaveProperty(["npc:elder@all"]);
    open("Люди");
    expect(isNew("Староста Хорн")).toBe(true);
  });

  test("запись, пришедшая при закрытой вкладке дневника, ждёт игрока «новой» в том же разделе", () => {
    const { update } = renderJournal();
    open("Места");

    update({ active: false });
    update({ entries: [...startJournal(), crypt()] });
    expect(storedSeen()).not.toHaveProperty(["location:crypt@all"]);

    update({ active: true });
    expect(section("Места")).toHaveAttribute("aria-selected", "true");
    expect(isNew("Склеп")).toBe(true);
    expect(isNew("Деревня Мирра")).toBe(false);
    expect(storedSeen()).toMatchObject({ "location:crypt@all": 1 });
  });

  test("откат вернул запись к уже виденной ревизии — она не «новая»", () => {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify({ "intro@all": 2 }));

    renderJournal();

    expect(isNew("Главное")).toBe(false);
    expect(count("Главное")).not.toHaveClass(NEW_COUNT);
  });

  test("хранилище не принимает запись (переполнено): отметки живут в памяти до перезагрузки", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Нет места", "QuotaExceededError");
    });

    const first = renderJournal();
    open("Места");
    open("Главное");
    expect(isNew("Главное")).toBe(false);
    open("Места");
    expect(isNew("Деревня Мирра")).toBe(false);
    expect(storedSeen()).toBeNull();
    first.unmount();

    renderJournal();
    expect(isNew("Главное")).toBe(true);
  });

  test("обращение к localStorage запрещено целиком: дневник работает, отметки — в памяти", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("Доступ запрещён", "SecurityError");
    });
    expect(() => window.localStorage).toThrow(DOMException);

    renderJournal();
    expect(isNew("Главное")).toBe(true);
    open("Места");
    open("Главное");

    expect(isNew("Главное")).toBe(false);
    expect(section("Места")).toHaveTextContent("Места 2");
  });

  test("смена игрока на том же экране: дневник с начала — «Главное» и свои отметки", () => {
    const { update } = renderJournal();
    open("Места");

    update({ userId: "u2" });

    expect(section("Главное")).toHaveAttribute("aria-selected", "true");
    expect(isNew("Главное")).toBe(true);
    expect(storedSeen("dnd-journal-seen:g1:u2")).toEqual({ "intro@all": 1 });
    expect(storedSeen()).toEqual({
      "intro@all": 1,
      "location:village@all": 1,
      "location:cemetery@all": 1,
    });
  });
});
