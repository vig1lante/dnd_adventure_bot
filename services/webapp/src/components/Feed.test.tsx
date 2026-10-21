import { render, screen, within } from "@testing-library/react";
import type { FeedItem } from "../api/types";
import { feedEntry, rollEntry } from "./fixtures";
import { Feed } from "./Feed";

const narration = (id: number, text: string) =>
  feedEntry(id, { kind: "narration", text });

describe("рассказ мастера", () => {
  test("читается крупным шрифтом, абзацы раздельны", () => {
    const { container } = render(
      <Feed
        items={[narration(1, "Дверь поддалась.\n\nЗа ней темно.\nИ тихо.")]}
      />,
    );

    const story = container.querySelector(".ui-narration");
    expect(story).not.toBeNull();
    expect(story?.querySelectorAll("p")).toHaveLength(3);
    expect(screen.getByText("Дверь поддалась.")).toBeInTheDocument();
    expect(screen.getByText("За ней темно.")).toBeInTheDocument();
    expect(screen.getByText("И тихо.")).toBeInTheDocument();
  });

  test("записи идут в порядке ленты", () => {
    render(
      <Feed
        items={[
          narration(1, "Первое."),
          rollEntry(2),
          feedEntry(3, { kind: "system", text: "Начался бой." }),
          narration(4, "Четвёртое."),
        ]}
      />,
    );

    const entries = screen.getAllByRole("listitem");
    expect(entries).toHaveLength(4);
    expect(entries[0]).toHaveTextContent("Первое.");
    expect(within(entries[1]).getByRole("article")).toBeInTheDocument();
    expect(entries[2]).toHaveTextContent("Начался бой.");
    expect(entries[3]).toHaveTextContent("Четвёртое.");
  });
});

describe("виды записей", () => {
  test("бросок — карточка броска", () => {
    render(<Feed items={[rollEntry(1)]} />);

    expect(
      screen.getByRole("article", {
        name: "Лира. Ловкость (Акробатика): 14 + 3 = 17 против Сл 15 ✓",
      }),
    ).toBeInTheDocument();
  });

  test("личное сообщение выделено «Только тебе»", () => {
    render(
      <Feed
        items={[
          feedEntry(1, {
            kind: "private",
            private: true,
            text: "Стражник заметил письмо в твоём рукаве.",
          }),
        ]}
      />,
    );

    const note = screen.getByRole("note", { name: "Только тебе" });
    expect(note).toHaveTextContent("Стражник заметил письмо в твоём рукаве.");
    expect(screen.getAllByText("Только тебе").length).toBeGreaterThan(0);
  });

  test("системная строка набрана мелко", () => {
    const { container } = render(
      <Feed items={[feedEntry(1, { kind: "system", text: "Бой окончен." })]} />,
    );

    expect(container.querySelector(".feed-system")).toHaveTextContent(
      "Бой окончен.",
    );
    expect(screen.queryByRole("note")).toBeNull();
  });

  test("ход игрока: «Лира: крадётся к двери»", () => {
    render(
      <Feed
        items={[
          feedEntry(1, {
            kind: "action",
            text: "крадётся к двери",
            data: { actor_id: "pc_lira", actor: "Лира", secret: false },
          }),
        ]}
      />,
    );

    expect(screen.getByText("Лира:")).toBeInTheDocument();
    expect(screen.getByText(/крадётся к двери/)).toBeInTheDocument();
    expect(screen.getByRole("listitem")).toHaveTextContent(
      "Лира: крадётся к двери",
    );
  });

  test("ход без имени автора в данных — просто текст", () => {
    render(
      <Feed
        items={[feedEntry(1, { kind: "action", text: "ждёт", data: null })]}
      />,
    );

    expect(screen.getByRole("listitem")).toHaveTextContent("ждёт");
  });

  test("реплика у костра: «Лира: «Холодает…»»", () => {
    render(
      <Feed
        items={[
          feedEntry(1, {
            kind: "camp_line",
            text: "Холодает, подкиньте дров.",
            data: { actor_id: "pc_lira", actor: "Лира" },
          }),
        ]}
      />,
    );

    expect(screen.getByRole("listitem")).toHaveTextContent(
      "Лира: «Холодает, подкиньте дров.»",
    );
  });

  test("запись неизвестного вида показывается текстом, а не пропадает", () => {
    const odd = feedEntry(1, { text: "Что-то новое." });
    (odd as { kind: string }).kind = "future_kind";

    render(<Feed items={[odd]} />);

    expect(screen.getByText("Что-то новое.")).toBeInTheDocument();
  });
});

describe("тайное видно как тайное", () => {
  test("свой тайный ход и тайный бросок помечены «Тайно»", () => {
    render(
      <Feed
        items={[
          feedEntry(1, {
            kind: "action",
            private: true,
            text: "прячет письмо",
            data: { actor: "Лира", secret: true },
          }),
          rollEntry(2, {}, { private: true }),
        ]}
      />,
    );

    expect(screen.getAllByText("Тайно")).toHaveLength(2);
  });

  test("общие записи без метки «Тайно»", () => {
    render(<Feed items={[rollEntry(1), narration(2, "Тихо.")]} />);

    expect(screen.queryByText("Тайно")).toBeNull();
  });

  test("личное сообщение помечено один раз: письмом «Только тебе», без лишней метки", () => {
    render(
      <Feed
        items={[feedEntry(1, { kind: "private", private: true, text: "Тс." })]}
      />,
    );

    expect(screen.queryByText("Тайно")).toBeNull();
  });
});

describe("текст только как текст", () => {
  const hostile = '<img src=x onerror="alert(1)"> <script>boom()</script>';

  test.each<[string, FeedItem]>([
    ["рассказ", feedEntry(1, { kind: "narration", text: hostile })],
    ["личное", feedEntry(2, { kind: "private", private: true, text: hostile })],
    ["система", feedEntry(3, { kind: "system", text: hostile })],
    [
      "ход игрока",
      feedEntry(4, {
        kind: "action",
        text: hostile,
        data: { actor: hostile },
      }),
    ],
    [
      "реплика",
      feedEntry(5, {
        kind: "camp_line",
        text: hostile,
        data: { actor: hostile },
      }),
    ],
  ])("%s: разметка выводится буквами", (_name, item) => {
    const { container } = render(<Feed items={[item]} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent("<img src=x");
    expect(container).toHaveTextContent("<script>boom()</script>");
  });

  test("в разметке ленты нет внедрения HTML", () => {
    const { container } = render(
      <Feed items={[narration(1, "<b>жирный</b>")]} />,
    );

    expect(container.querySelector("b")).toBeNull();
  });
});

describe("пустая лента", () => {
  test("пока мастер не заговорил — пояснение", () => {
    render(<Feed items={[]} />);

    expect(screen.getByText("Рассказ ещё не начался")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).toBeNull();
  });
});

describe("движение записей", () => {
  test("записи, что уже были на экране, появляются спокойно; новые — с движением", () => {
    const { container, rerender } = render(
      <Feed items={[narration(1, "Старое."), rollEntry(2)]} />,
    );
    expect(container.querySelector(".ui-anim-appear")).toBeNull();
    expect(container.querySelector(".ui-anim-roll")).toBeNull();

    rerender(
      <Feed
        items={[
          narration(1, "Старое."),
          rollEntry(2),
          narration(3, "Новое."),
          rollEntry(4),
        ]}
      />,
    );

    const entries = screen.getAllByRole("listitem");
    expect(entries[0].querySelector(".ui-anim-appear")).toBeNull();
    expect(entries[1].querySelector(".ui-anim-roll")).toBeNull();
    expect(entries[2].querySelector(".ui-anim-appear")).not.toBeNull();
    expect(entries[3].querySelector(".ui-anim-roll")).not.toBeNull();
  });
});

describe("автор записи", () => {
  test.each([
    ["число", 5],
    ["объект", { name: "Лира" }],
    ["пустое значение", null],
  ])(
    "актёр не строкой (%s): имени нет, текст записи показан",
    (_name, actor) => {
      const { container } = render(
        <Feed
          items={[
            feedEntry(1, {
              kind: "action",
              text: "крадётся к двери",
              data: { actor },
            }),
          ]}
        />,
      );

      expect(container.querySelector("strong")).toBeNull();
      expect(container).toHaveTextContent("крадётся к двери");
      expect(container).not.toHaveTextContent("[object Object]");
    },
  );
});

describe("прокрутка к последней записи", () => {
  const scrollIntoView = vi.fn();

  beforeEach(() => {
    scrollIntoView.mockReset();
    Element.prototype.scrollIntoView = scrollIntoView;
  });
  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });

  test("при показе и при каждой новой записи последняя запись выводится в видимость", () => {
    const { rerender } = render(<Feed items={[narration(1, "Первое.")]} />);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    rerender(
      <Feed items={[narration(1, "Первое."), narration(2, "Второе.")]} />,
    );

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
    const target = scrollIntoView.mock.contexts.at(-1) as HTMLElement;
    expect(target).toHaveTextContent("Второе.");
  });

  test("перерисовка без новых записей лист не двигает", () => {
    const items = [narration(1, "Первое.")];
    const { rerender } = render(<Feed items={items} />);
    scrollIntoView.mockClear();

    rerender(<Feed items={[...items]} />);

    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  test("прокрутка плавная и до ближайшего края; при «меньше движения» — без плавности", () => {
    const { unmount } = render(<Feed items={[narration(1, "Первое.")]} />);
    expect(scrollIntoView).toHaveBeenLastCalledWith({
      block: "nearest",
      behavior: "smooth",
    });
    unmount();

    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    scrollIntoView.mockClear();
    const reduced = render(<Feed items={[narration(1, "Первое.")]} />);
    reduced.rerender(
      <Feed items={[narration(1, "Первое."), narration(2, "Второе.")]} />,
    );

    expect(scrollIntoView).toHaveBeenLastCalledWith({
      block: "nearest",
      behavior: "auto",
    });
  });

  describe("не мешает игроку", () => {
    /** Нижний край записи в окне, px от верха; записи вне списка — у верхнего края. */
    function placeEntries(bottoms: Record<string, number>) {
      vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
        function (this: Element) {
          const found = Object.entries(bottoms).find(([text]) =>
            this.textContent?.includes(text),
          );
          const bottom = found?.[1] ?? 0;
          return { top: bottom - 100, bottom, height: 100 } as DOMRect;
        },
      );
    }
    const first = [narration(1, "Первое.")];
    const both = [...first, narration(2, "Второе.")];

    test("игрок читает выше: новая запись лист не двигает", () => {
      placeEntries({ "Первое.": window.innerHeight + 900 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();

      rerender(<Feed items={both} />);

      expect(scrollIntoView).not.toHaveBeenCalled();
    });

    test("игрок у конца ленты: лента прокручивается к новой записи", () => {
      placeEntries({ "Первое.": window.innerHeight - 20 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();

      rerender(<Feed items={both} />);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("Второе.");
    });

    test("конец ленты чуть ниже края окна (в пределах запаса) — ещё «у конца»", () => {
      placeEntries({ "Первое.": window.innerHeight + 30 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();

      rerender(<Feed items={both} />);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });

    test("игрок ушёл дальше ленты (к полю хода): конец ленты выше окна — тоже «у конца»", () => {
      placeEntries({ "Первое.": -300 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();

      rerender(<Feed items={both} />);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });

    test("несколько записей за раз: решает положение прежней последней, прокрутка одна", () => {
      placeEntries({ "Первое.": window.innerHeight - 20 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();

      rerender(
        <Feed
          items={[...both, narration(3, "Третье."), narration(4, "Четвёртое.")]}
        />,
      );

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent(
        "Четвёртое.",
      );
    });

    test.each([
      ["поле ввода", <input key="f" aria-label="Поле" />],
      ["многострочное поле", <textarea key="f" aria-label="Поле" />],
      [
        "выпадающий список",
        <select key="f" aria-label="Поле">
          <option>1</option>
        </select>,
      ],
    ])(
      "фокус в поле формы (%s): лист не двигается даже у конца ленты",
      (_name, field) => {
        placeEntries({ "Первое.": window.innerHeight - 20 });
        const { rerender } = render(
          <>
            {field}
            <Feed items={first} />
          </>,
        );
        screen.getByLabelText("Поле").focus();
        scrollIntoView.mockClear();

        rerender(
          <>
            {field}
            <Feed items={both} />
          </>,
        );

        expect(scrollIntoView).not.toHaveBeenCalled();
      },
    );

    test("фокус на кнопке — это не ввод текста: лента прокручивается", () => {
      placeEntries({ "Первое.": window.innerHeight - 20 });
      const { rerender } = render(
        <>
          <button type="button">Пас</button>
          <Feed items={first} />
        </>,
      );
      screen.getByRole("button", { name: "Пас" }).focus();
      scrollIntoView.mockClear();

      rerender(
        <>
          <button type="button">Пас</button>
          <Feed items={both} />
        </>,
      );

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });

    test("при показе экрана с фокусом в поле лента тоже стоит на месте", () => {
      const field = document.createElement("textarea");
      document.body.append(field);
      onTestFinished(() => field.remove());
      field.focus();

      render(<Feed items={first} />);

      expect(scrollIntoView).not.toHaveBeenCalled();
    });

    test("после того как игрок вернулся к концу, следующая запись снова ведёт его за собой", () => {
      placeEntries({ "Первое.": window.innerHeight + 900 });
      const { rerender } = render(<Feed items={first} />);
      scrollIntoView.mockClear();
      rerender(<Feed items={both} />);
      expect(scrollIntoView).not.toHaveBeenCalled();

      placeEntries({
        "Первое.": window.innerHeight - 300,
        "Второе.": window.innerHeight - 20,
      });
      rerender(<Feed items={[...both, narration(3, "Третье.")]} />);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("Третье.");
    });

    test("последняя запись заменена новой: прежней уже нет, лента следует за новой как раньше", () => {
      placeEntries({ "Второе.": window.innerHeight + 900 });
      const { rerender } = render(<Feed items={both} />);
      scrollIntoView.mockClear();

      rerender(<Feed items={[...first, narration(3, "Третье.")]} />);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("Третье.");
    });
  });

  test("без scrollIntoView (старый WebView) экран не ломается", () => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");

    expect(() =>
      render(<Feed items={[narration(1, "Первое.")]} />),
    ).not.toThrow();
  });
});
