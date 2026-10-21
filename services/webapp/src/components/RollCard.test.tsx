import { render, screen, within } from "@testing-library/react";
import { feedEntry, rollEntry } from "./fixtures";
import { RollCard } from "./RollCard";

const card = (name: string | RegExp) => screen.getByRole("article", { name });

describe("карточка броска собирается из данных сервера", () => {
  test("проверка навыка: имя, название, формула, итог и Сл", () => {
    render(<RollCard item={rollEntry(1)} />);

    const roll = card(
      "Лира. Ловкость (Акробатика): 14 + 3 = 17 против Сл 15 ✓",
    );
    expect(within(roll).getByText("Лира")).toBeInTheDocument();
    expect(within(roll).getByText("Ловкость (Акробатика)")).toBeInTheDocument();
    expect(within(roll).getByText("14 + 3")).toBeInTheDocument();
    expect(within(roll).getByText("17")).toBeInTheDocument();
    expect(within(roll).getByText("против Сл 15")).toBeInTheDocument();
    expect(within(roll).getByText("Успех")).toBeInTheDocument();
  });

  test("грань кубика показывает выпавшее на к20", () => {
    const { container } = render(<RollCard item={rollEntry(1)} />);

    expect(container.querySelector(".ui-roll__face")).toHaveTextContent("14");
  });

  test("неудача: сургучная метка и ✗", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          natural: 6,
          kept: [6],
          total: 9,
          success: false,
        })}
      />,
    );

    expect(
      card("Лира. Ловкость (Акробатика): 6 + 3 = 9 против Сл 15 ✗"),
    ).toBeInTheDocument();
    expect(screen.getByText("Неудача")).toBeInTheDocument();
  });

  test("атака монстра по герою: «против КД N» из данных сервера", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          actor: "Скелет",
          label: "Атака: Короткий меч",
          natural: 12,
          kept: [12],
          modifier: 5,
          total: 17,
          dc: null,
          success: true,
          target_ac: 15,
        })}
      />,
    );

    expect(
      card("Скелет. Атака: Короткий меч: 12 + 5 = 17 против КД 15 ✓"),
    ).toBeInTheDocument();
    expect(screen.getByText("против КД 15")).toBeInTheDocument();
    expect(screen.getByText("Попадание")).toBeInTheDocument();
  });

  test("атака героя по монстру: КД монстра нигде не показан", () => {
    const { container } = render(
      <RollCard
        item={rollEntry(1, {
          label: "Атака: Длинный меч",
          natural: 12,
          kept: [12],
          modifier: 5,
          total: 17,
          dc: null,
          success: false,
        })}
      />,
    );

    expect(container).not.toHaveTextContent("КД");
    expect(screen.getByText("Промах")).toBeInTheDocument();
  });

  test("урон: «Урон: 1к8+3 = 9» и выпавшие кости", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          label: "Урон",
          expr: "2d6+3",
          rolls: [4, 2],
          kept: [4, 2],
          natural: null,
          total: 9,
          dc: null,
          success: null,
        })}
      />,
    );

    expect(card("Лира. Урон: 2к6+3 = 9")).toBeInTheDocument();
    expect(screen.getByText("Выпало: 4, 2")).toBeInTheDocument();
    expect(screen.queryByText("Успех")).toBeNull();
  });

  test("кость Благословения: к20 + бонус + кость = итог", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          expr: "1d20+3+1d4",
          rolls: [14, 2],
          kept: [14, 2],
          total: 19,
        })}
      />,
    );

    expect(
      card("Лира. Ловкость (Акробатика): 14 + 3 + 2 = 19 против Сл 15 ✓"),
    ).toBeInTheDocument();
    expect(screen.getByText("14 + 3 + 2")).toBeInTheDocument();
    // обе кости уже в формуле: отдельная строка «Выпало» только повторяла бы её
    expect(screen.queryByText(/^Выпало/)).toBeNull();
  });

  test("преимущество: метка и обе выпавшие кости", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          rolls: [3, 14],
          kept: [14],
          advantage: "advantage",
        })}
      />,
    );

    expect(screen.getByText("Преимущество")).toBeInTheDocument();
    expect(screen.getByText("Выпало: 3, 14")).toBeInTheDocument();
  });

  test("помеха: метка", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          rolls: [14, 3],
          kept: [3],
          natural: 3,
          total: 6,
          advantage: "disadvantage",
          success: false,
        })}
      />,
    );

    expect(screen.getByText("Помеха")).toBeInTheDocument();
  });

  test("подпись берётся из label и actor, а не из reason движка", () => {
    const { container } = render(
      <RollCard
        item={rollEntry(1, {
          actor: "Кто-то невидимый",
          label: "Ловкость (Скрытность)",
          reason: "Скрытность: Фанатик культа",
        })}
      />,
    );

    expect(container).toHaveTextContent("Кто-то невидимый");
    expect(container).not.toHaveTextContent("Фанатик");
  });

  test("карточка без выпавшего к20 (лечение) обходится без грани кубика", () => {
    const { container } = render(
      <RollCard
        item={rollEntry(1, {
          label: "Лечение: Лечение ран",
          expr: "1d8+2",
          rolls: [5],
          kept: [5],
          modifier: 2,
          natural: null,
          total: 7,
          dc: null,
          success: null,
        })}
      />,
    );

    expect(container.querySelector(".ui-roll__face")).toBeNull();
    expect(card("Лира. Лечение: Лечение ран: 1к8+2 = 7")).toBeInTheDocument();
  });
});

describe("карточка вместо запасного текста", () => {
  test("данные не разобрались — показывается text сервера", () => {
    render(
      <RollCard
        item={feedEntry(1, {
          kind: "roll",
          text: "Лира: Скрытность — 17 против Сл 15: успех",
          data: { total: "много" },
        })}
      />,
    );

    expect(screen.queryByRole("article")).toBeNull();
    expect(
      screen.getByText("Лира: Скрытность — 17 против Сл 15: успех"),
    ).toBeInTheDocument();
  });

  test("данных нет вовсе — тоже text сервера", () => {
    render(
      <RollCard
        item={feedEntry(1, { kind: "roll", text: "Бросок.", data: null })}
      />,
    );

    expect(screen.getByText("Бросок.")).toBeInTheDocument();
  });
});

describe("текст только как текст", () => {
  test("имя и название с разметкой выводятся буквами, а не элементами", () => {
    const { container } = render(
      <RollCard
        item={rollEntry(1, {
          actor: "<img src=x onerror=alert(1)>",
          label: "<b>Сила</b>",
        })}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(container).toHaveTextContent("<b>Сила</b>");
  });

  test("запасной текст с разметкой тоже остаётся буквами", () => {
    const { container } = render(
      <RollCard
        item={feedEntry(1, { kind: "roll", text: "<script>x</script>" })}
      />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent("<script>x</script>");
  });
});

describe("движение", () => {
  test("новая запись перекатывает кубик, старая стоит спокойно", () => {
    const { container, rerender } = render(<RollCard item={rollEntry(1)} />);
    expect(container.querySelector(".ui-anim-roll")).not.toBeNull();

    rerender(<RollCard item={rollEntry(1)} animate={false} />);

    expect(container.querySelector(".ui-anim-roll")).toBeNull();
  });

  test("при prefers-reduced-motion кубик не катится", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));

    const { container } = render(<RollCard item={rollEntry(1)} />);

    expect(container.querySelector(".ui-anim-roll")).toBeNull();
  });
});

describe("цвет исхода броска", () => {
  const badge = (text: string) => screen.getByText(text).closest(".ui-badge");

  test.each([
    ["Успех", { success: true }, "cloth"],
    ["Неудача", { success: false }, "seal"],
    ["Попадание", { label: "Атака: меч", dc: null, success: true }, "cloth"],
    ["Промах", { label: "Атака: меч", dc: null, success: false }, "seal"],
  ])("«%s»: метка тона %s", (text, overrides, tone) => {
    render(<RollCard item={rollEntry(1, overrides)} />);

    expect(badge(text)).toHaveClass(`ui-badge--${tone}`);
  });
});

describe("строка «Выпало»", () => {
  test("урон одной костью: отдельной строки нет, значение уже в формуле", () => {
    render(
      <RollCard
        item={rollEntry(1, {
          label: "Урон",
          expr: "1d8+3",
          rolls: [6],
          kept: [6],
          natural: null,
          total: 9,
          dc: null,
          success: null,
        })}
      />,
    );

    expect(card("Лира. Урон: 1к8+3 = 9")).toBeInTheDocument();
    expect(screen.queryByText(/^Выпало/)).toBeNull();
  });
});
